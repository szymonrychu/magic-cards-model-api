"""sim_match_claude_wait: one blocking call with progress, heartbeats and the compact log."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from sim_claude_kit import (
    GREEN,
    RED,
    Clock,
    _end_game,
    _finish,
    _game_id,
    _manager,
    _put_row,
    _start,
)
from sim_claude_kit import fake_registry as fixture_registry

fake_registry = fixture_registry  # the fixture, bound under the name tests request


def _run(manager, match_id: str, clock: Clock, *, on_tick=None, **kw: Any):
    """Drive `wait_for_match` on a fake clock; answers (answer, [(progress, message, t)])."""
    from magic_cards_model_api.sim.claude_wait import wait_for_match

    reports: list[tuple[float, str, float]] = []
    t0 = clock()

    async def report(progress: float, message: str) -> None:
        reports.append((progress, message, clock() - t0))

    async def sleep(seconds: float) -> None:
        clock.now += seconds
        if on_tick is not None:
            on_tick(clock.now - t0)
        await asyncio.sleep(0)

    kw.setdefault("heartbeat_s", 60.0)
    kw.setdefault("max_wait_s", 5000.0)  # a broken loop fails the test instead of hanging it
    answer = asyncio.run(wait_for_match(manager, match_id, report, clock=clock, sleep=sleep, **kw))
    return answer, reports


def _once(at: float, action):
    """An `on_tick` that runs `action` once, the first tick at or after `at` seconds."""
    done: list[bool] = []

    def tick(elapsed: float) -> None:
        if not done and elapsed >= at:
            done.append(True)
            action()

    return tick


def test_terminal_match_answers_at_once_with_the_log(fake_registry):
    clock = Clock()
    manager = _manager(clock)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    _finish(manager, match_id)
    answer, reports = _run(manager, match_id, clock)
    assert answer["status"] == "done"
    assert len(reports) == 1 and reports[0][0] == 1
    assert reports[0][1].startswith("waiting: done")
    assert answer["log"]["format"] == "compact-v1"
    assert answer["log"]["truncated"] is False
    assert answer["winner"] == "a" and answer["match_id"] == match_id
    assert set(answer["seats"]["a"]) >= {"deck", "model", "effort", "state"}
    assert "note" not in answer


def test_returns_when_the_match_finishes(fake_registry):
    clock = Clock()
    manager = _manager(clock)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    answer, _ = _run(
        manager, match_id, clock, on_tick=_once(120, lambda: _finish(manager, match_id))
    )
    assert answer["status"] == "done"
    assert answer["winner"] == "a"
    assert clock.now - 1000.0 < 130


def test_heartbeat_every_interval_without_new_rows(fake_registry):
    clock = Clock()
    manager = _manager(clock)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    manager.reap()  # the reaper has seen the boot rows
    answer, reports = _run(
        manager, match_id, clock, on_tick=_once(250, lambda: _finish(manager, match_id))
    )
    assert answer["status"] == "done"
    assert [t for _, _, t in reports] == [0, 60, 120, 180, 240]
    assert [p for p, _, _ in reports] == [1, 2, 3, 4, 5]
    assert reports[0][1].startswith("waiting: running")
    assert all(m.startswith("still running at T") for _, m, _ in reports[1:])


def test_progress_on_new_records_throttled_to_5_s(fake_registry):
    from magic_cards_model_api import audit
    from magic_cards_model_api.store import store

    clock = Clock()
    manager = _manager(clock)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    manager.reap()
    game_id = _game_id(manager, match_id)

    written: list[int] = []

    def write_rows() -> None:
        for i in range(10):
            written.append(
                audit.record(game_id, "play_land", player="a", card=f"Forest{i}", reasoning=f"r{i}")
            )

    ticks = [_once(1, write_rows), _once(12, lambda: _finish(manager, match_id))]
    answer, reports = _run(manager, match_id, clock, on_tick=lambda t: [tick(t) for tick in ticks])
    assert answer["status"] == "done"
    newest = written[-1]
    assert store.max_record_seq(game_id) > newest  # the concede came after
    lines = [(p, m, t) for p, m, t in reports[1:] if not m.startswith("still ")]
    assert len(lines) == 1
    progress, message, at = lines[0]
    assert f"#{newest} " in message and "Forest9" in message
    assert at <= 5.0 + 1e-9
    assert [p for p, _, _ in reports] == list(range(1, len(reports) + 1))


def test_wait_tick_reads_a_single_new_row(fake_registry):
    from magic_cards_model_api import audit

    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(manager, match_id)
    before = manager.wait_tick(match_id, True).last_seq or 0
    seq = audit.record(game_id, "play_land", player="a", card="Forest", reasoning="land")
    tick = manager.wait_tick(match_id, True, before)
    assert tick.last_seq == seq and tick.status == "running"
    assert tick.newest_line is not None and tick.newest_line.startswith(f"#{seq} ")
    assert manager.wait_tick(match_id, True, seq).newest_line is None
    cached = manager.wait_tick(match_id, False)
    assert cached.newest_line is None and cached.status == "running"
    _finish(manager, match_id)


def test_returns_on_stalled_with_reason_and_note(fake_registry):
    clock = Clock()
    manager = _manager(clock)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    manager.reap()  # the baseline seq
    answer, _ = _run(manager, match_id, clock, on_tick=_once(901, manager.reap), tick_s=5.0)
    assert answer["status"] == "stalled"
    assert answer["stalled_reason"].startswith("no action for")
    assert "sim_match_claude_cancel" in answer["note"]
    assert match_id in manager.live_ids()
    assert manager.status(match_id)["status"] == "stalled"
    _finish(manager, match_id)


def _stalled(clock: Clock):
    manager = _manager(clock)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    manager.reap()  # the baseline seq
    clock.now += 901
    manager.reap()
    assert manager.status(match_id)["status"] == "stalled"
    return manager, match_id


def test_wait_on_a_stalled_match_blocks_until_max_wait_s(fake_registry):
    clock = Clock()
    manager, match_id = _stalled(clock)
    start = clock.now
    answer, reports = _run(manager, match_id, clock, max_wait_s=300.0)
    assert clock.now - start >= 300
    assert answer["status"] == "stalled"
    assert answer["note"] == "max_wait_s elapsed; call sim_match_claude_wait(match_id) again"
    assert len(reports) > 1  # heartbeats while it blocked
    _finish(manager, match_id)


def test_wait_on_a_stalled_match_follows_it_back_to_running_until_terminal(fake_registry):
    from magic_cards_model_api import audit

    clock = Clock()
    manager, match_id = _stalled(clock)
    game_id = _game_id(manager, match_id)

    def progress() -> None:
        audit.record(game_id, "pass_priority", player="a", reasoning="back")
        manager.reap()
        assert manager.status(match_id)["status"] == "running"

    ticks = [_once(30, progress), _once(90, lambda: _finish(manager, match_id))]
    answer, _ = _run(manager, match_id, clock, on_tick=lambda t: [tick(t) for tick in ticks])
    assert answer["status"] == "done"
    assert "note" not in answer


def test_wait_on_a_live_row_only_match_answers_at_once_off_the_loop(fake_registry, monkeypatch):
    from magic_cards_model_api.sim.claude import RESTART_ERROR
    from magic_cards_model_api.store import store

    _put_row("claude-orphan-wait", "running", datetime.now(UTC))
    clock = Clock()
    manager = _manager(clock)
    on_loop: list[bool] = []
    read = manager._row

    def row(match_id: str):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return read(match_id)

    monkeypatch.setattr(manager, "_row", row)
    answer, reports = _run(manager, "claude-orphan-wait", clock, max_wait_s=None)
    assert answer["status"] == "failed" and answer["error"] == RESTART_ERROR
    assert len(reports) == 1 and clock.now == 1000.0
    assert on_loop and not any(on_loop)
    manager.flush_persist()
    assert store.get_sim_claude_match("claude-orphan-wait")["status"] == "failed"


def test_max_wait_s_returns_running_with_note(fake_registry):
    clock = Clock()
    manager = _manager(clock)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    answer, reports = _run(manager, match_id, clock, max_wait_s=30.0)
    assert answer["status"] == "running"
    assert answer["waited_s"] >= 30
    assert answer["note"] == "max_wait_s elapsed; call sim_match_claude_wait(match_id) again"
    assert manager.status(match_id)["status"] == "running"
    _finish(manager, match_id)


def test_reissued_wait_is_idempotent(fake_registry):
    clock = Clock()
    manager = _manager(clock)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    _finish(manager, match_id)
    first, _ = _run(manager, match_id, clock)
    clock.now += 50
    second, _ = _run(manager, match_id, clock)
    assert first == second


def test_cancelled_wait_leaves_the_match_alone(fake_registry):
    from magic_cards_model_api.sim.claude_wait import wait_for_match

    manager = _manager()  # a frozen manager clock: nothing transitions on its own
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    before = threading.active_count()
    outcome: dict[str, Any] = {}

    async def main() -> None:
        first = asyncio.Event()

        async def report(progress: float, message: str) -> None:
            first.set()

        task = asyncio.create_task(
            wait_for_match(
                manager,
                match_id,
                report,
                max_wait_s=None,
                heartbeat_s=60.0,
                progress_every_s=0.02,
                tick_s=0.01,
            )
        )
        await asyncio.wait_for(first.wait(), 5)
        await asyncio.sleep(0.1)  # let it run a few progress reads in threads
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            outcome["cancelled"] = True

    asyncio.run(main())
    assert outcome == {"cancelled": True}
    assert manager.status(match_id)["status"] == "running"
    deadline = time.monotonic() + 2
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert threading.active_count() <= before
    # A re-issued wait after the broken one returns the final answer.
    _finish(manager, match_id)
    again, _ = _run(manager, match_id, Clock())
    assert again["status"] == "done"


def test_unknown_match_refused(fake_registry):
    from magic_cards_model_api.sim.manager import SimRefused

    clock = Clock()
    manager = _manager(clock)
    with pytest.raises(SimRefused, match="No Claude match"):
        _run(manager, "claude-nope", clock)


def test_persisted_only_match_answers_from_the_row(fake_registry):
    from magic_cards_model_api.sim.claude import RESTART_ERROR

    now = datetime.now(UTC)
    _put_row("claude-rowonly-wait", "failed", now, now, error=RESTART_ERROR)
    clock = Clock()
    manager = _manager(clock)
    answer, reports = _run(manager, "claude-rowonly-wait", clock)
    assert answer["status"] == "failed"
    assert answer["error"] == RESTART_ERROR
    assert answer["log"]["lines"] == ""
    assert answer["seats"]["a"]["deck"] == "Red"
    assert len(reports) == 1


def test_wait_answer_truncates_head_and_tail(fake_registry):
    from magic_cards_model_api import audit

    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(manager, match_id)
    for i in range(900):
        audit.record(game_id, "play_land", player="a", card=f"F{i}", reasoning="r" * 280)
    log = manager.wait_answer(match_id)["log"]
    assert len(log["lines"]) <= 45_000
    assert log["truncated"] is True
    assert log["next_seq"] is not None
    assert f"sim_match_log(since_seq={log['next_seq']})" in log["lines"]
    assert log["total_rows"] >= 900
    _finish(manager, match_id)


def test_wait_answer_scrubs_seat_uids(fake_registry):
    from magic_cards_model_api import audit
    from magic_cards_model_api.store import store

    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(manager, match_id)
    uids = list(store.seat_uids(game_id))
    audit.record(game_id, "play_land", player="a", card="Forest", reasoning=f"id {uids[0][:8]}")
    _end_game(game_id, "b")
    manager.reap()
    answer = manager.wait_answer(match_id)
    assert "id <redacted>" in answer["log"]["lines"]
    text = repr(answer)
    for uid in uids:
        assert uid not in text and uid[:8] not in text
