"""Sandbox slot ownership: caps, idle TTL, call timeout (one worker child per sandbox).

Sandbox ids are unguessable uuids and act as capabilities: whoever holds one may use it.
The slot is global, not per caller (see MEMORY).
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from magic_cards_model_api.sim.config import SimLimits
from magic_cards_model_api.sim.handle import WorkerDied, WorkerError, WorkerHandle, WorkerTimeout
from magic_cards_model_api.sim.match import aggregate, resolve_deck

logger = logging.getLogger(__name__)

_SHUTTING_DOWN = "The sandbox service is shutting down. Try again later."
_SCRIPT_MARGIN_S = 3.0  # scenario budget = call cap left after start-up, minus this
_BUSY = "The sandbox is busy with another call; retry in a few seconds."


class SimRefused(Exception):
    """A user-facing refusal; the message is meant to be shown to the caller as is."""


@dataclass
class _Sandbox:
    handle: WorkerHandle
    last_used: float
    busy: threading.Lock = field(default_factory=threading.Lock)


class SandboxManager:
    def __init__(
        self,
        limits: SimLimits,
        handle_factory: Callable[[], WorkerHandle] = WorkerHandle,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits
        self._factory = handle_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._sandboxes: dict[str, _Sandbox] = {}
        self._closed = False
        self._starting = 0  # slots reserved by a `new` still in flight

    def new(self, spec: dict[str, Any], *, mode: str = "board") -> dict[str, Any]:
        return self._open("new", lambda _remaining: spec, mode=mode)

    def run_script(self, script: dict[str, Any], keep: bool = False) -> dict[str, Any]:
        """Take the slot, run a scenario in a fresh worker, end the sandbox unless `keep`."""

        def args(remaining: float) -> dict[str, Any]:
            # Leave room under the call cap for the reply; start-up already used part of it.
            return {"script": script, "budget_s": max(0.5, remaining - _SCRIPT_MARGIN_S)}

        return self._open("run_script", args, mode="board", keep=keep)

    def _open(
        self,
        op: str,
        make_args: Callable[[float], dict[str, Any]],
        *,
        mode: str,
        keep: bool = True,
    ) -> dict[str, Any]:
        # Worker start and the first request share one deadline: the call cap.
        cap = self._limits.call_timeout_s
        deadline = time.monotonic() + cap
        with self._lock:
            if self._closed:
                raise SimRefused(_SHUTTING_DOWN)
            if len(self._sandboxes) + self._starting >= self._limits.max_sandboxes:
                raise SimRefused(
                    f"The sandbox slot is in use (frees in {self._frees_in()} s unless used)."
                    " Retry later, or end your own sandbox with sim_end."
                )
            self._starting += 1
        handle: WorkerHandle | None = None
        sandbox_id = f"sim-{uuid.uuid4().hex}"
        try:
            handle = self._factory()
            handle.start(cap)
            remaining = max(0.0, deadline - time.monotonic())
            answer = handle.request(op, make_args(remaining), remaining)
            with self._lock:
                if self._closed:
                    raise SimRefused(_SHUTTING_DOWN)
                if keep:
                    self._sandboxes[sandbox_id] = _Sandbox(handle, self._clock())
        except WorkerError as exc:
            self._abort_new(handle)
            raise SimRefused(str(exc)) from exc
        except WorkerTimeout as exc:
            self._abort_new(handle)
            what = "Running the script" if op == "run_script" else "Starting the sandbox"
            raise SimRefused(f"{what} exceeded the {cap:g} s call limit. Try again.") from exc
        except WorkerDied as exc:
            self._abort_new(handle)
            raise SimRefused(f"The sandbox worker stopped: {exc}. Try again.") from exc
        except SimRefused:
            self._abort_new(handle)
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. OSError from spawn; the slot must be freed
            self._abort_new(handle)
            raise SimRefused(f"The sandbox could not be started: {exc}. Try again.") from exc
        except BaseException:
            self._abort_new(handle)
            raise
        if not keep:
            handle.kill()
        with self._lock:
            self._starting -= 1
        if not keep:
            return answer
        return {
            **answer,
            "sandbox": {
                "id": sandbox_id,
                "expires_in_s": int(self._limits.sandbox_idle_ttl_s),
                "mode": mode,
            },
        }

    def _abort_new(self, handle: WorkerHandle | None) -> None:
        if handle is not None:
            handle.kill()
        with self._lock:
            self._starting -= 1

    def call(self, sandbox_id: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
        cap = self._limits.call_timeout_s
        deadline = time.monotonic() + cap
        with self._lock:
            sandbox = self._sandboxes.get(sandbox_id)
            if sandbox is None:
                raise SimRefused(
                    f"No sandbox {sandbox_id}; it may have expired after"
                    f" {self._limits.sandbox_idle_ttl_s:g} s idle. Start a new one with sim_new."
                )
            sandbox.last_used = self._clock()
        # The handle serialises requests without counting the wait toward its timeout, so the
        # wait is bounded here and only the remaining budget is passed on.
        if not sandbox.busy.acquire(timeout=cap):
            raise SimRefused(_BUSY)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SimRefused(_BUSY)
            return sandbox.handle.request(op, args, remaining)
        except WorkerError as exc:
            return {"allowed": False, "reason": str(exc)}
        except WorkerTimeout as exc:
            self._drop(sandbox_id, kill=True)
            raise SimRefused(
                f"The call exceeded {cap:g} s, so the sandbox was ended. Start a new one."
            ) from exc
        except WorkerDied as exc:
            self._drop(sandbox_id, kill=True)
            raise SimRefused(f"The sandbox worker stopped: {exc}. Start a new one.") from exc
        finally:
            sandbox.busy.release()

    def end(self, sandbox_id: str) -> dict[str, Any]:
        self._drop(sandbox_id, kill=True)
        return {"ended": True}

    def reap(self) -> list[str]:
        now = self._clock()
        with self._lock:
            idle = [
                sid
                for sid, sb in self._sandboxes.items()
                if now - sb.last_used > self._limits.sandbox_idle_ttl_s
            ]
        for sid in idle:
            self._drop(sid, kill=True)
        return idle

    def reopen(self) -> None:
        """Accept calls again after `shutdown` (each app lifespan entry reopens the singleton)."""
        with self._lock:
            self._closed = False

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            ids = list(self._sandboxes)
        for sid in ids:
            self._drop(sid, kill=True)

    def _drop(self, sandbox_id: str, *, kill: bool) -> None:
        with self._lock:
            sandbox = self._sandboxes.pop(sandbox_id, None)
        if sandbox is not None and kill:
            sandbox.handle.kill()

    def _frees_in(self) -> int:
        """Seconds until the least recently used sandbox expires (caller holds the lock)."""
        if not self._sandboxes:  # only in-flight `new` calls hold the slot
            return int(self._limits.sandbox_idle_ttl_s)
        now = self._clock()
        oldest = min(sb.last_used for sb in self._sandboxes.values())
        return max(0, math.ceil(self._limits.sandbox_idle_ttl_s - (now - oldest)))


_START_TIMEOUT_S = 60.0  # worker start-up for a batch; not bound by the tool call cap


@dataclass
class _Batch:
    batch_id: str
    labels: tuple[str, str]
    texts: tuple[str, str]
    requested: int
    seed: int
    created: float
    handle: WorkerHandle | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    status: str = "running"
    error: str | None = None
    play_started: float | None = None  # time.monotonic() when the worker op was sent
    finished: float | None = None  # manager clock
    done: threading.Event = field(default_factory=threading.Event)


class BatchManager:
    """Engine-only baseline batches: one background worker per batch, polled by id.

    Batches never use the sandbox slot. The wall clock runs from the moment the match op is
    sent; at the cap the worker is killed and the games finished so far are kept (`capped`).
    """

    def __init__(
        self,
        limits: SimLimits,
        handle_factory: Callable[[], WorkerHandle] = WorkerHandle,
        clock: Callable[[], float] = time.monotonic,
        op: str = "match",
    ) -> None:
        self._limits = limits
        self._factory = handle_factory
        self._clock = clock
        self._op = op  # tests swap in a debug worker op
        self._lock = threading.Lock()
        self._batches: dict[str, _Batch] = {}
        self._closed = False

    def start(self, deck_a: str, deck_b: str, games: int, seed: int | None) -> str:
        lim = self._limits
        if games < 1:
            raise SimRefused("games must be at least 1.")
        if games > lim.max_match_games:
            raise SimRefused(
                f"games={games} is above the cap of {lim.max_match_games}"
                " (MTG_SIM_MAX_MATCH_GAMES). Ask for fewer games."
            )
        try:
            label_a, text_a = resolve_deck(deck_a)
            label_b, text_b = resolve_deck(deck_b)
        except ValueError as exc:
            raise SimRefused(str(exc)) from exc
        if seed is None:
            seed = random.SystemRandom().randrange(2**31)
        batch = _Batch(
            f"batch-{uuid.uuid4().hex}", (label_a, label_b), (text_a, text_b), games, seed,
            self._clock(),
        )
        with self._lock:
            if self._closed:
                raise SimRefused(_SHUTTING_DOWN)
            running = [b for b in self._batches.values() if b.status == "running"]
            if len(running) >= lim.max_batches:
                left = max(0, math.ceil(self._wall_left(running[0])))
                raise SimRefused(
                    f"A batch is already running (about {left} s left of its"
                    f" {lim.match_wall_s:g} s cap). Poll it or retry later."
                )
            self._batches[batch.batch_id] = batch
        args = {"deck_a": text_a, "deck_b": text_b, "games": games, "seed": seed}
        threading.Thread(
            target=self._run, args=(batch, args), daemon=True, name=batch.batch_id
        ).start()
        return batch.batch_id

    def _wall_left(self, batch: _Batch) -> float:
        wall = self._limits.match_wall_s
        if batch.play_started is None:
            return wall
        return wall - (time.monotonic() - batch.play_started)

    def _run(self, batch: _Batch, args: dict[str, Any]) -> None:
        status, error = "done", None
        handle: WorkerHandle | None = None
        try:
            handle = self._factory()
            batch.handle = handle
            self._refuse_if_closed()  # shutdown ran before the worker existed
            handle.start(_START_TIMEOUT_S)
            self._refuse_if_closed()  # shutdown ran during start-up
            handle.on_progress = batch.rows.append  # after start: `request` clears it
            batch.play_started = time.monotonic()
            handle.request(self._op, args, self._limits.match_wall_s)
        except WorkerTimeout:
            status = "capped"
        except (WorkerError, SimRefused) as exc:
            status, error = "failed", str(exc)
        except WorkerDied as exc:
            status, error = "failed", f"The batch worker stopped: {exc}"
        except Exception as exc:  # noqa: BLE001 - e.g. OSError from spawn
            status, error = "failed", f"The batch could not run: {exc}"
        finally:
            if handle is not None:
                handle.kill()
            with self._lock:
                batch.error = error
                batch.status = status
                batch.finished = self._clock()
            batch.done.set()

    def _refuse_if_closed(self) -> None:
        with self._lock:
            closed = self._closed
        if closed:
            raise SimRefused(_SHUTTING_DOWN)

    def status(self, batch_id: str, wait_s: float) -> dict[str, Any]:
        with self._lock:
            batch = self._batches.get(batch_id)
        if batch is None:
            raise SimRefused(
                f"No batch {batch_id}; finished batches are kept"
                f" {self._limits.result_ttl_s:g} s. Start one with sim_match."
            )
        if wait_s > 0:
            batch.done.wait(timeout=wait_s)
        with self._lock:
            status, error, finished = batch.status, batch.error, batch.finished
            rows = list(batch.rows)
        end = finished if finished is not None else self._clock()
        out = aggregate(
            batch.labels[0], batch.labels[1], rows, batch.requested, status,
            batch.texts[0], batch.texts[1],
        )
        out.update(
            batch_id=batch.batch_id, seed=batch.seed, elapsed_s=round(end - batch.created, 1)
        )
        if status == "running":
            out["wall_left_s"] = max(0, math.ceil(self._wall_left(batch)))
        if status == "capped":
            out["capped_note"] = (
                f"Stopped at the {self._limits.match_wall_s:g} s wall-clock cap with"
                f" {out['finished_games']} of {batch.requested} games finished; the results"
                " cover the finished games only."
            )
        if error is not None:
            out["error"] = error
        return out

    def reap(self) -> list[str]:
        now = self._clock()
        with self._lock:
            old = [
                bid
                for bid, b in self._batches.items()
                if b.finished is not None and now - b.finished > self._limits.result_ttl_s
            ]
            for bid in old:
                del self._batches[bid]
        return old

    def reopen(self) -> None:
        with self._lock:
            self._closed = False

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            running = [b for b in self._batches.values() if b.status == "running"]
        for batch in running:
            if batch.handle is not None:
                batch.handle.kill()
            batch.done.wait(timeout=10)


class Reaper:
    """Daemon thread that calls every registered `reap` callable each `interval_s`."""

    def __init__(self, interval_s: float = 30.0) -> None:
        self._interval_s = interval_s
        self._fns: list[Callable[[], Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, fn: Callable[[], Any]) -> None:
        self._fns.append(fn)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sim-reaper")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            for fn in list(self._fns):
                try:
                    fn()
                except Exception:  # noqa: BLE001 - one failing reap must not stop the thread
                    logger.exception("sim reaper callable failed")
