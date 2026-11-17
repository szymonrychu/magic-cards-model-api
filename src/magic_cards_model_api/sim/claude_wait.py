"""The blocking delivery of a Claude-seat match: `sim_match_claude_wait`.

MCP delivers a tool result only as the tools/call response, so the caller makes one long call
that returns when the match ends (terminal status), stalls, or `max_wait_s` passes. A wait
issued on a match that is already stalled blocks until it leaves stalled (and then returns on
terminal or on a new stall) or `max_wait_s` passes, so "wait again" really waits. While it
waits it sends progress notifications: the first at once (Claude Code's first-byte timer), the
newest compact log line at most every `progress_every_s` when records are new, and a heartbeat
every `heartbeat_s` otherwise (Claude Code drops a call idle for 5 minutes). The progress value
is the notification count, because MCP progress must increase and a heartbeat has no new seq.

Nothing here holds a lock across a sleep, and `CancelledError` (the client went away) is never
caught: the coroutine ends, the match is untouched, and a re-issued wait answers the same.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from magic_cards_model_api.sim.claude import TERMINAL, ClaudeMatchManager

MAX_WAIT_NOTE = "max_wait_s elapsed; call sim_match_claude_wait(match_id) again"


async def wait_for_match(
    manager: ClaudeMatchManager,
    match_id: str,
    report: Callable[[float, str], Awaitable[None]],
    *,
    max_wait_s: float | None,
    heartbeat_s: float,
    progress_every_s: float = 5.0,
    tick_s: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """Block until the match is terminal or stalled, or `max_wait_s` passes; answer
    `manager.wait_answer`. Every manager call runs in a thread (a row read is DB I/O).
    `SimRefused` (unknown match) propagates."""
    start = clock()
    tick = await asyncio.to_thread(manager.wait_tick, match_id, False)
    n = 1
    await report(n, f"waiting: {tick.status} T{tick.turn} {tick.step}")
    last_report = last_read = start
    seen_seq = tick.last_seq or 0
    timed_out = False
    # A wait issued on a stalled match blocks until it leaves stalled; a stall it sees begin
    # (running before) ends the wait.
    armed = tick.status != "stalled"
    while tick.status not in TERMINAL and not (armed and tick.status == "stalled"):
        if max_wait_s is not None and clock() - start >= max_wait_s:
            timed_out = True
            break
        line = None
        if clock() - last_read >= progress_every_s:
            read = await asyncio.to_thread(manager.wait_tick, match_id, True, seen_seq)
            last_read = clock()
            if read.last_seq is not None:
                seen_seq = max(seen_seq, read.last_seq)
            line = read.newest_line
        if line is not None:
            n += 1
            await report(n, line)
            last_report = clock()
        elif clock() - last_report >= heartbeat_s:
            n += 1
            await report(
                n, f"still {tick.status} at T{tick.turn} {tick.step}; last action #{seen_seq}"
            )
            last_report = clock()
        await sleep(tick_s)
        # In a thread: a match this process no longer holds is answered from its row.
        tick = await asyncio.to_thread(manager.wait_tick, match_id, False)
        armed = armed or tick.status != "stalled"
    answer = await asyncio.to_thread(manager.wait_answer, match_id)
    if timed_out and answer["status"] not in TERMINAL:
        answer["waited_s"] = round(clock() - start, 1)
        answer["note"] = MAX_WAIT_NOTE
    return answer
