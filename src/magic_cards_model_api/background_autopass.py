"""Planning off the clock: keep a seat's `pass_until` run going on the server
while the seat itself is busy writing its next-turn plan.

A `pass_until` call that hands back unfinished because the opponent is still
deciding used to leave nothing passing for the seat: the agent then spent
30-90 s composing `declare_plan(for_turn="next")`, and every priority the
opponent handed it in that time sat waiting on a busy model. `BackgroundRuns`
continues the very same run (same destination, same stop conditions, same
`resume`) on a thread, through `seatkit._pass_until_once(background=True)`,
which additionally never passes on the seat's own turn nor makes the pass
that could end the opponent's turn - so the plan is still on record before
the seat's own turn begins, and nothing is passed that the run itself would
not have passed.

The seat's next `pass_until` call with the same `resume` takes the run back
(`take_over`) and continues from wherever the thread got to; any other
acting tool stops it first (`stop`), so two things never act for one seat at
once. A run that reached a real stop (`finished`) is left exactly where it
stopped: the seat's own call re-detects that stop from the last unfinished
`resume`, since every stop is a pure function of the board and that token.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import seatkit

logger = logging.getLogger("mcm.background_autopass")

#: The longest one background run keeps going with nobody taking it back -
#: a seat that died mid-plan must not keep passing for a whole game.
MAX_BACKGROUND_SECONDS = 900.0


@dataclass
class _Run:
    origin: dict[str, Any]
    latest: dict[str, Any]
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    passes: int = 0
    status: str = "running"
    stopped_because: str = ""


class BackgroundRuns:
    """One background run per seat uid, shared by every tool call of an MCP
    process (`Seats.background`)."""

    def __init__(
        self,
        *,
        poll_seconds: float = seatkit.WAIT_POLL_SECONDS,
        max_seconds: float = MAX_BACKGROUND_SECONDS,
    ) -> None:
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()
        self._poll = poll_seconds
        self._max = max_seconds

    def start(
        self,
        uid: str,
        make_seat: Callable[[], Any],
        kwargs: dict[str, Any],
        resume: dict[str, Any],
    ) -> None:
        """Continue the run `resume` names on a thread. `kwargs` are the
        `_pass_until_once` arguments of the call that handed it back."""
        self.stop(uid)
        run = _Run(origin=dict(resume), latest=dict(resume))
        run.thread = threading.Thread(
            target=self._loop, args=(uid, run, make_seat, dict(kwargs)), daemon=True
        )
        with self._lock:
            self._runs[uid] = run
        run.thread.start()

    def status(self, uid: str) -> str | None:
        with self._lock:
            run = self._runs.get(uid)
        return None if run is None else run.status

    def stop(self, uid: str) -> dict[str, Any] | None:
        """End `uid`'s run, waiting for any pass in flight, and say what it
        did - `None` when there was none."""
        with self._lock:
            run = self._runs.pop(uid, None)
        if run is None:
            return None
        run.stop.set()
        if run.thread is not None and run.thread is not threading.current_thread():
            run.thread.join(timeout=seatkit.CLIENT_TIMEOUT_SECONDS + 5)
        if run.status == "running":
            run.status = "stopped"
        return self._summary(run)

    def take_over(
        self, uid: str, resume: dict[str, Any] | None
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Stop `uid`'s run and hand back the `resume` to continue it from -
        only when `resume` is the token the run started from; otherwise
        `(None, summary)` and the caller's own call stands as it is."""
        with self._lock:
            run = self._runs.get(uid)
        if run is None:
            return None, None
        summary = self.stop(uid)
        if resume is None or resume != run.origin:
            return None, summary
        return dict(run.latest), summary

    @staticmethod
    def _summary(run: _Run) -> dict[str, Any]:
        summary: dict[str, Any] = {"passes": run.passes, "status": run.status}
        if run.stopped_because:
            summary["stopped_because"] = run.stopped_because
        return summary

    def _loop(
        self,
        uid: str,
        run: _Run,
        make_seat: Callable[[], Any],
        kwargs: dict[str, Any],
    ) -> None:
        deadline = time.monotonic() + self._max
        try:
            seat = make_seat()
            if seat is None:
                run.status = "stopped"
                return
            while not run.stop.is_set():
                if time.monotonic() >= deadline:
                    run.status = "expired"
                    return
                result = seatkit._pass_until_once(
                    seat,
                    resume=run.latest,
                    include_actions=False,
                    include_extras=False,
                    background=True,
                    **kwargs,
                )
                run.passes += int(result.get("passes") or 0)
                run.stopped_because = str(result.get("stopped_because") or "")
                if result.get("finished"):
                    run.status = "finished"
                    return
                if isinstance(result.get("resume"), dict):
                    run.latest = result["resume"]
                if not result.get("retryable", False):
                    run.status = "halted"
                    return
                run.stop.wait(self._poll)
        except Exception:  # noqa: BLE001 - a background run must never take the process down
            logger.exception("background pass_until run for %s raised", uid[:8])
            run.status = "error"
        finally:
            logger.info(
                "background pass_until seat=%s status=%s passes=%d",
                uid[:8],
                run.status,
                run.passes,
            )
