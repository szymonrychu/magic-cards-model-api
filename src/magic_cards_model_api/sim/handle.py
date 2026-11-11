"""Parent-side handle of one sim worker child (JSON-lines over pipes, thread-based).

Threads, not asyncio subprocess transports: tests call tools through `asyncio.run(...)`
with a fresh loop per call, so a transport bound to one loop cannot be reused. Tools
call `await asyncio.to_thread(handle.request, ...)`.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from magic_cards_model_api.sim.worker import worker_env


class WorkerTimeout(Exception):
    """The worker did not answer in time; the caller should kill the handle."""


class WorkerError(Exception):
    """The worker is fine but refused or failed the request (e.g. `new` with a bad spec)."""


class WorkerDied(Exception):
    """The worker exited or reported that it must exit."""


class WorkerHandle:
    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._next_id = 0
        self._dead_reason: str | None = None
        self._reader: threading.Thread | None = None
        self.on_progress: Callable[[dict[str, Any]], None] | None = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int:
        if self._proc is None:
            raise RuntimeError("worker not started")
        return self._proc.pid

    def start(self, timeout: float) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "magic_cards_model_api.sim.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=worker_env(os.environ),
        )
        self._reader = threading.Thread(target=self._read, args=(self._proc,), daemon=True)
        self._reader.start()
        try:
            self.request("ping", {}, timeout)
        except BaseException:
            self.kill()
            raise

    def _read(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
            if isinstance(msg, dict):
                self._lines.put(msg)
            else:
                print(f"sim worker: ignoring non-protocol line {line[:200]!r}", file=sys.stderr)
        self._lines.put(None)

    def request(self, op: str, args: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._lock:
            if self._dead_reason is not None:
                raise WorkerDied(self._dead_reason)
            if not self.alive:
                raise self._died(self._exit_reason())
            assert self._proc is not None and self._proc.stdin is not None
            self._next_id += 1
            req_id = self._next_id
            try:
                self._proc.stdin.write(json.dumps({"id": req_id, "op": op, "args": args}) + "\n")
                self._proc.stdin.flush()
            except OSError as exc:
                raise self._died(f"worker pipe closed: {exc}") from exc
            return self._await(req_id, timeout)

    def _await(self, req_id: int, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                msg = self._lines.get(timeout=max(deadline - time.monotonic(), 0))
            except queue.Empty:
                raise WorkerTimeout(f"no answer within {timeout}s") from None
            if msg is None:
                code = None
                if self._proc is not None:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        code = self._proc.wait(timeout=max(deadline - time.monotonic(), 0))
                raise self._died(f"worker exited with code {code}")
            if msg.get("id") is None and not msg.get("ok", True):
                raise self._died(str(msg.get("error", "worker failed")))
            if msg.get("id") != req_id:
                continue  # stale reply of a timed-out request
            if "progress" in msg:
                if self.on_progress is not None:
                    self.on_progress(msg["progress"])
                continue
            if not msg.get("ok"):
                raise WorkerError(str(msg.get("error", "")))
            return msg["result"]

    def _exit_reason(self) -> str:
        """Why the worker is gone: its last id-null report if unread, else its exit code."""
        if self._reader is not None:
            self._reader.join(timeout=2)
        reason = None
        while True:
            try:
                msg = self._lines.get_nowait()
            except queue.Empty:
                break
            if msg is not None and msg.get("id") is None and not msg.get("ok", True):
                reason = str(msg.get("error", "worker failed"))
        if reason is not None:
            return reason
        code = self._proc.poll() if self._proc else None
        return f"worker is not running (exit code {code})"

    def _died(self, reason: str) -> WorkerDied:
        self._dead_reason = reason
        return WorkerDied(reason)

    def kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        for pipe in (proc.stdin, proc.stdout):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass
