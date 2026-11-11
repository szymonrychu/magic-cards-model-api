"""Sandbox slot ownership: caps, idle TTL, call timeout (one worker child per sandbox).

Sandbox ids are unguessable uuids and act as capabilities: whoever holds one may use it.
The slot is global, not per caller (see MEMORY).
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from magic_cards_model_api.sim.config import SimLimits
from magic_cards_model_api.sim.handle import WorkerDied, WorkerError, WorkerHandle, WorkerTimeout

logger = logging.getLogger(__name__)

_SHUTTING_DOWN = "The sandbox service is shutting down. Try again later."
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
        # Worker start and the `new` request share one deadline: the call cap.
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
            answer = handle.request("new", spec, max(0.0, deadline - time.monotonic()))
            with self._lock:
                if self._closed:
                    raise SimRefused(_SHUTTING_DOWN)
                self._sandboxes[sandbox_id] = _Sandbox(handle, self._clock())
        except WorkerError as exc:
            self._abort_new(handle)
            raise SimRefused(str(exc)) from exc
        except WorkerTimeout as exc:
            self._abort_new(handle)
            raise SimRefused(
                f"Starting the sandbox exceeded the {cap:g} s call limit. Try again."
            ) from exc
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
        with self._lock:
            self._starting -= 1
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
