# Continuous MTG Agent Implementation Plan - Part 2 (Tasks 7-12)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Read part 1 (`2026-08-31-continuous-mtg-agent.md`) first - its Global Constraints apply to every task here.

---

### Task 7: The session manager

The core of the port. Boot-dialog navigation, quiescence detection, the
two-write submit, completion, and the death/restart path.

**Files:**
- Create: `src/mtg_agent/wrapper/session.py`
- Test: `tests/wrapper/test_session.py`

**Interfaces:**
- Consumes: `Settings` (T1), `Store`/`TurnState` (T4), `Ring` (T5), `ClaudeProcess`/`spawn_claude` (T6).
- Produces:
  - `SubmitSequence` dataclass: `paste_start: str = "\x1b[200~"`, `paste_end: str = "\x1b[201~"`, `submit: str = "\r"`. Module constant `DEFAULT_SUBMIT_SEQ`.
  - `State` StrEnum: `BOOTING="booting"`, `READY="ready"`, `BUSY="busy"`, `DEAD="dead"`.
  - `BusyError`, `NotReadyError`, `DeadError` exceptions.
  - `BYPASS_NEEDLE = "Bypass Permissions mode"`, `MIN_BOOT = 4.0`, `IDLE_READY = 1.5`, `BOOT_POLL = 0.15`.
  - `Manager(settings, store, metrics, *, now=time.monotonic, new_id=..., spawn=spawn_claude, on_turn_done=None)` with:
    `start() -> None`, `boot_wait(proc) -> None`, `submit(text, callback_url="") -> str`,
    `complete(result: dict) -> None`, `mark_stall_suspected(turn_id) -> None`,
    `snapshot() -> dict`, `shutdown() -> None`, `write_raw(s: str) -> None`,
    `state` property, `current_turn_id` property, `ring` attribute, `transcript_path` attribute.
  - `new_turn_id() -> str` producing `turn-<base36 of time.time_ns()>`.

- [ ] **Step 1: Write the failing test**

`tests/wrapper/test_session.py`:

```python
import threading
import time

import pytest

from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.metrics import Metrics
from mtg_agent.wrapper.session import (
    BusyError,
    DeadError,
    Manager,
    NotReadyError,
    State,
    new_turn_id,
)
from mtg_agent.wrapper.turns import Store, TurnState


class FakeProc:
    """A ClaudeProcess whose output and exit the test drives."""

    def __init__(self, script: bytes = b"") -> None:
        self.pid = 1234
        self.writes: list[bytes] = []
        self.write_times: list[float] = []
        self._out = bytearray(script)
        self._exit = threading.Event()
        self._closed = False
        self.closed_at: float | None = None

    def feed(self, data: bytes) -> None:
        self._out.extend(data)

    def read(self, n: int) -> bytes:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._out:
                chunk = bytes(self._out[:n])
                del self._out[:n]
                return chunk
            if self._exit.is_set():
                return b""
            time.sleep(0.005)
        return b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        self.write_times.append(time.monotonic())
        return len(data)

    def wait(self) -> int:
        self._exit.wait(30)
        return 0

    def close(self) -> None:
        self._closed = True
        self.closed_at = time.monotonic()
        self._exit.set()

    def die(self) -> None:
        self._exit.set()


def fast_settings(**overrides) -> Settings:
    env = {
        "BOOT_TIMEOUT_SECONDS": "10",
        "SUBMIT_DELAY_SECONDS": "0.05",
        "TURN_TIMEOUT_SECONDS": "0.2",
    } | overrides
    return Settings.from_env(env)


def make(proc: FakeProc, settings: Settings | None = None, **kwargs) -> Manager:
    store = Store()
    manager = Manager(
        settings or fast_settings(),
        store,
        Metrics(),
        spawn=lambda _settings, resume: proc,
        **kwargs,
    )
    manager.store = store
    return manager


def test_new_turn_id_is_unique_and_prefixed():
    ids = {new_turn_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(i.startswith("turn-") for i in ids)


def test_boot_accepts_the_bypass_warning_then_goes_ready(monkeypatch):
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.05)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 0.1)
    proc = FakeProc(b"\x1b[2J Bypass\x1b[3C Permissions   mode \x1b[0m")
    manager = make(proc)
    manager.start()
    try:
        assert manager.state is State.READY
        assert proc.writes[0] == b"\x1b[B", "down arrow selects 'Yes, I accept'"
        assert proc.writes[1] == b"\r"
    finally:
        manager.shutdown()


def test_boot_without_the_dialog_still_goes_ready(monkeypatch):
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.05)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 0.1)
    proc = FakeProc(b"welcome to claude")
    manager = make(proc)
    manager.start()
    try:
        assert manager.state is State.READY
        assert proc.writes == []
    finally:
        manager.shutdown()


def test_boot_waits_for_quiescence_not_a_fixed_delay(monkeypatch):
    """claude paints its first frame in ~2s but is still initialising.
    Ready must not be declared while bytes are still arriving."""
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.05)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 0.3)
    proc = FakeProc(b"frame one")
    manager = make(proc)

    def keep_painting() -> None:
        for _ in range(10):
            proc.feed(b".")
            time.sleep(0.05)

    painter = threading.Thread(target=keep_painting)
    painter.start()
    started = time.monotonic()
    manager.start()
    elapsed = time.monotonic() - started
    painter.join()
    try:
        assert manager.state is State.READY
        assert elapsed >= 0.5, "went ready while output was still arriving"
    finally:
        manager.shutdown()


def test_boot_gives_up_at_the_boot_timeout(monkeypatch):
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.05)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 30.0)
    proc = FakeProc(b"hello")
    manager = make(proc, fast_settings(BOOT_TIMEOUT_SECONDS="0.4"))
    started = time.monotonic()
    manager.start()
    try:
        assert manager.state is State.READY
        assert time.monotonic() - started < 3.0
    finally:
        manager.shutdown()


def ready_manager(monkeypatch, proc: FakeProc, settings: Settings | None = None) -> Manager:
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.01)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 0.05)
    manager = make(proc, settings)
    manager.start()
    proc.writes.clear()
    proc.write_times.clear()
    return manager


def test_submit_is_a_bracketed_paste_then_a_separate_carriage_return(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    try:
        turn_id = manager.submit("play a land")
        assert proc.writes == [b"\x1b[200~play a land\x1b[201~", b"\r"]
        gap = proc.write_times[1] - proc.write_times[0]
        assert gap >= 0.05, "one write does not submit; the pause is required"
        assert manager.state is State.BUSY
        assert manager.store.get(turn_id).state is TurnState.RUNNING
    finally:
        manager.shutdown()


def test_a_second_submit_while_busy_raises_busy(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    try:
        manager.submit("first")
        with pytest.raises(BusyError):
            manager.submit("second")
    finally:
        manager.shutdown()


def test_submit_before_ready_raises_not_ready():
    proc = FakeProc(b"")
    manager = make(proc)
    with pytest.raises(NotReadyError):
        manager.submit("too early")


def test_submit_after_death_raises_dead(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    proc.die()
    deadline = time.monotonic() + 5
    while manager.state is not State.DEAD and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.state is State.DEAD
    with pytest.raises(DeadError):
        manager.submit("nobody home")


def test_complete_records_the_result_and_returns_to_ready(monkeypatch):
    proc = FakeProc(b"ready")
    delivered: list = []
    manager = ready_manager(monkeypatch, proc)
    manager.on_turn_done = delivered.append
    try:
        turn_id = manager.submit("go")
        manager.complete(
            {
                "sessionId": "s1",
                "finalText": "PONG",
                "transcriptPath": "/workspace/t.jsonl",
                "usage": {"output_tokens": 3},
                "stopReason": "end_turn",
                "turnTokens": [{"model": "sonnet", "output": 3}],
                "resultJson": {"total_cost_usd": 0.5},
            }
        )
        record = manager.store.get(turn_id)
        assert record.state is TurnState.COMPLETE
        assert record.final_text == "PONG"
        assert record.result_json == {"total_cost_usd": 0.5}
        assert manager.state is State.READY
        assert manager.transcript_path == "/workspace/t.jsonl"
        assert [r.id for r in delivered] == [turn_id]
    finally:
        manager.shutdown()


def test_complete_with_no_turn_in_flight_is_ignored(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    try:
        manager.complete({"finalText": "orphan"})
        assert manager.state is State.READY
        assert manager.store.list() == []
    finally:
        manager.shutdown()


def test_the_turn_timer_reports_a_stall_and_never_fails_the_turn(monkeypatch):
    """A game turn legitimately sits for minutes waiting on the human. The
    wrapper cannot tell a hang from work it cannot see, so it only reports."""
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc, fast_settings(TURN_TIMEOUT_SECONDS="0.1"))
    try:
        turn_id = manager.submit("go")
        time.sleep(0.35)
        assert manager.store.get(turn_id).state is TurnState.RUNNING
        assert manager.state is State.BUSY
        assert manager.snapshot()["stallSuspectedSince"] is not None
        manager.complete({"finalText": "late but fine"})
        assert manager.store.get(turn_id).state is TurnState.COMPLETE
    finally:
        manager.shutdown()


def test_snapshot_reports_state_counts_and_model(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc, fast_settings(MODEL="sonnet"))
    try:
        turn_id = manager.submit("go")
        manager.complete({"finalText": "ok"})
        snapshot = manager.snapshot()
        assert snapshot["state"] == "ready"
        assert snapshot["turnsCompleted"] == 1
        assert snapshot["turnsFinished"] == 1
        assert snapshot["model"] == "sonnet"
        assert snapshot["currentTurnId"] is None
        assert turn_id
    finally:
        manager.shutdown()


def test_death_mid_turn_fails_the_in_flight_turn(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    turn_id = manager.submit("go")
    proc.die()
    deadline = time.monotonic() + 5
    while manager.state is not State.DEAD and time.monotonic() < deadline:
        time.sleep(0.01)
    record = manager.store.get(turn_id)
    assert manager.state is State.DEAD
    assert record.state is TurnState.FAILED
    assert "exited" in record.error


def test_shutdown_is_deliberate_and_does_not_mark_dead_by_crash(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    manager.shutdown()
    assert proc._closed
    assert manager.metrics.claude_restarts_total._value.get() == 0
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/wrapper/test_session.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.wrapper.session` (Task 10 supplies `metrics`; implement a minimal `Metrics` there first if the import order bites - see Step 3's note).

- [ ] **Step 3: Implement session.py**

Note: this imports `Metrics` from Task 10. Implement `metrics.py` first
(Task 10's Step 3 code is self-contained and has no dependency on this file),
or stub it and let Task 10 replace it. Prefer doing Task 10 before this one if
you are executing strictly in order.

```python
"""The supervised interactive claude process, and the turn state machine.

    booting -> ready -> busy -> ready -> ... -> dead

One process handles one turn at a time, so a second submit while busy is a
409. Everything here that looks like a magic number came from running the
real binary, not from taste - see the module comments at each one.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .config import Settings
from .metrics import Metrics
from .pty_proc import ClaudeProcess, spawn_claude
from .ring import Ring
from .turns import Record, Store

log = logging.getLogger("wrapper.session")

#: Wrap the message in bracketed paste, then submit with a bare CR. ONE write
#: does not submit - the CR must be a separate write after a pause.
DEFAULT_PASTE_START = "\x1b[200~"
DEFAULT_PASTE_END = "\x1b[201~"
DEFAULT_SUBMIT = "\r"

#: The bypass-permissions warning is not seedable and appears on every boot.
BYPASS_NEEDLE = "Bypass Permissions mode"

#: Boot is declared over on output quiescence, never on a fixed delay: claude
#: paints its first frame in ~2s but is still initialising, and submitting
#: then exits it.
MIN_BOOT = 4.0
IDLE_READY = 1.5
BOOT_POLL = 0.15


@dataclass(frozen=True)
class SubmitSequence:
    paste_start: str = DEFAULT_PASTE_START
    paste_end: str = DEFAULT_PASTE_END
    submit: str = DEFAULT_SUBMIT


DEFAULT_SUBMIT_SEQ = SubmitSequence()


class State(StrEnum):
    BOOTING = "booting"
    READY = "ready"
    BUSY = "busy"
    DEAD = "dead"


class BusyError(RuntimeError):
    """A turn is already in flight."""


class NotReadyError(RuntimeError):
    """The session is still booting."""


class DeadError(RuntimeError):
    """The supervised claude process is gone."""


def new_turn_id() -> str:
    nanos = time.time_ns()
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while nanos:
        nanos, remainder = divmod(nanos, 36)
        out = digits[remainder] + out
    return f"turn-{out}"


class Manager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        metrics: Metrics,
        *,
        now: Callable[[], float] = time.time,
        new_id: Callable[[], str] = new_turn_id,
        spawn: Callable[[Settings, bool], ClaudeProcess] = spawn_claude,
        on_turn_done: Callable[[Record], None] | None = None,
        submit_seq: SubmitSequence = DEFAULT_SUBMIT_SEQ,
    ) -> None:
        self.settings = settings
        self.store = store
        self.metrics = metrics
        self.now = now
        self.new_id = new_id
        self.spawn = spawn
        self.on_turn_done = on_turn_done
        self.submit_seq = submit_seq

        self.ring = Ring()
        self.transcript_path = ""
        self._lock = threading.RLock()
        self._pty_lock = threading.Lock()
        self._state = State.BOOTING
        self._proc: ClaudeProcess | None = None
        self._current: str | None = None
        self._current_started = 0.0
        self._timer: threading.Timer | None = None
        self._stall_suspected_since: float | None = None
        self._stopping = False
        self._threads: list[threading.Thread] = []

    # ---- lifecycle -------------------------------------------------------

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    @property
    def current_turn_id(self) -> str | None:
        with self._lock:
            return self._current

    def start(self) -> None:
        proc = self.spawn(self.settings, False)
        with self._lock:
            self._proc = proc
        for target in (self._read_pty, self._watch):
            thread = threading.Thread(target=target, args=(proc,), daemon=True)
            thread.start()
            self._threads.append(thread)
        self.boot_wait(proc)

    def _read_pty(self, proc: ClaudeProcess) -> None:
        """Copy the TUI's output into the ring. The only window into the
        session: boot_wait reads it for dialogs, _watch logs its tail."""
        while True:
            try:
                chunk = proc.read(4096)
            except OSError:
                return
            if not chunk:
                return
            self.ring.write(chunk)

    def boot_wait(self, proc: ClaudeProcess) -> None:
        """Accept the bypass warning, then wait for the TUI to settle.

        Without accepting the warning, the first turn's submit keystroke
        lands on the dialog and exits claude.
        """
        start = time.monotonic()
        deadline = start + self.settings.boot_timeout
        last_written = self.ring.written()
        last_change = start
        accepted = False

        while True:
            if self._is_dead() or not self._is_active(proc) or self._stopping:
                return
            now = time.monotonic()
            if now >= deadline:
                break
            if not accepted and self.ring.contains(BYPASS_NEEDLE):
                time.sleep(0.6)
                self.write_raw("\x1b[B")  # down arrow -> "Yes, I accept"
                time.sleep(0.25)
                self.write_raw("\r")
                accepted = True
                log.info("accepted bypass-permissions warning")
                time.sleep(0.5)
                continue
            written = self.ring.written()
            if written != last_written:
                last_written, last_change = written, now
            if now - start >= MIN_BOOT and now - last_change >= IDLE_READY:
                break
            time.sleep(BOOT_POLL)

        with self._lock:
            if self._state is State.BOOTING and self._proc is proc:
                self._state = State.READY
        log.info(
            "session ready boot_ms=%d accepted_bypass=%s",
            int((time.monotonic() - start) * 1000),
            accepted,
        )

    def _watch(self, proc: ClaudeProcess) -> None:
        """Block until claude exits, then mark the session dead and fail any
        turn that was in flight. Nothing recovers here: the container is
        restarted by its restart policy, and the transcript survives on the
        workspace volume."""
        try:
            code = proc.wait()
        except ChildProcessError:
            code = -1
        if self._stopping:
            return
        tail = self.ring.tail(800)
        log.error("claude exited code=%s pty_tail=%r", code, tail)
        self.metrics.claude_restarts_total.inc()
        with self._lock:
            self._state = State.DEAD
            turn_id = self._current
            self._current = None
            self._cancel_timer_locked()
        if turn_id:
            self._finish_failed(turn_id, f"claude exited with code {code}")

    def shutdown(self) -> None:
        self._stopping = True
        with self._lock:
            proc = self._proc
            self._cancel_timer_locked()
        if proc is not None:
            proc.close()

    def _is_dead(self) -> bool:
        return self.state is State.DEAD

    def _is_active(self, proc: ClaudeProcess) -> bool:
        with self._lock:
            return self._proc is proc

    # ---- writing ---------------------------------------------------------

    def write_raw(self, text: str) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.write(text.encode())
            except OSError as exc:
                log.warning("raw write failed: %s", exc)

    # ---- turns -----------------------------------------------------------

    def submit(self, text: str, callback_url: str = "") -> str:
        with self._lock:
            if self._state is State.DEAD:
                raise DeadError("session dead")
            if self._state is State.BOOTING:
                raise NotReadyError("session not ready")
            if self._current is not None:
                raise BusyError("session busy")
            turn_id = self.new_id()
            started = self.now()
            self.store.create(turn_id, text, callback_url, started)
            # Reserve the slot before the writes, so a concurrent submit sees
            # BusyError even while we sleep between the paste and the CR.
            self._current, self._current_started = turn_id, started
            self._state = State.BUSY
            proc = self._proc
            self._stall_suspected_since = None
        self.metrics.turn_in_flight.set(1)

        seq = self.submit_seq
        # Serialise against any other PTY writer (probe, interrupt) so nothing
        # lands inside the paste block or the delay window.
        with self._pty_lock:
            try:
                proc.write((seq.paste_start + text + seq.paste_end).encode())
                time.sleep(self.settings.submit_delay)
                proc.write(seq.submit.encode())
            except OSError as exc:
                self._finish_failed(turn_id, f"write to pty failed: {exc}")
                with self._lock:
                    self._current = None
                    self._state = State.READY
                raise

        with self._lock:
            self._timer = threading.Timer(
                self.settings.turn_timeout, self.mark_stall_suspected, args=(turn_id,)
            )
            self._timer.daemon = True
            self._timer.start()
        log.info("turn submitted turn_id=%s", turn_id)
        return turn_id

    def mark_stall_suspected(self, turn_id: str) -> None:
        """The inactivity timer fired. This is a REPORT, not a verdict: a
        wrapper inside one container cannot tell a hang from work it cannot
        see, and a Magic turn waits on a human. Re-arm and carry on."""
        with self._lock:
            if self._current != turn_id:
                return
            if self._stall_suspected_since is None:
                self._stall_suspected_since = self.now()
            self.metrics.turn_stall_suspected_total.inc()
            self._timer = threading.Timer(
                self.settings.turn_timeout, self.mark_stall_suspected, args=(turn_id,)
            )
            self._timer.daemon = True
            self._timer.start()
        log.warning("turn stall suspected turn_id=%s", turn_id)

    def complete(self, result: dict[str, Any]) -> None:
        with self._lock:
            turn_id = self._current
            if turn_id is None:
                log.warning("turn-complete with no turn in flight, ignoring")
                return
            started = self._current_started
            self._cancel_timer_locked()
            self._current = None
            self._stall_suspected_since = None
            if self._state is not State.DEAD:
                self._state = State.READY
            path = result.get("transcriptPath") or ""
            if path:
                self.transcript_path = path

        record = self.store.complete(
            turn_id,
            final_text=result.get("finalText") or "",
            result_json=result.get("resultJson"),
            usage=result.get("usage"),
            turn_tokens=result.get("turnTokens") or [],
            stop_reason=result.get("stopReason") or "",
            now=self.now(),
        )
        self.metrics.turn_in_flight.set(0)
        self.metrics.hook_received_total.inc()
        self.metrics.turns_total.labels(result="complete").inc()
        self.metrics.turn_duration_seconds.observe(max(0.0, self.now() - started))
        self.metrics.record_tokens(record.turn_tokens)
        self.metrics.record_cost(record.result_json)
        log.info("turn complete turn_id=%s", turn_id)
        self._deliver(record)

    def _finish_failed(self, turn_id: str, reason: str) -> None:
        record = self.store.fail(turn_id, reason, self.now())
        self.metrics.turn_in_flight.set(0)
        self.metrics.turns_total.labels(result="failed").inc()
        self._deliver(record)

    def _deliver(self, record: Record) -> None:
        """Fire the completion callback OUTSIDE the lock."""
        if self.on_turn_done is None:
            return
        try:
            self.on_turn_done(record)
        except Exception as exc:  # noqa: BLE001 - delivery must not break a turn
            log.error("on_turn_done raised: %s", exc)

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    # ---- reporting -------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        completed, finished = self.store.counts()
        with self._lock:
            return {
                "state": str(self._state),
                "turnsCompleted": completed,
                "turnsFinished": finished,
                "model": self.settings.model,
                "currentTurnId": self._current,
                "stallSuspectedSince": self._stall_suspected_since,
                "transcriptPath": self.transcript_path,
            }
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/wrapper/test_session.py -v`
Expected: 14 passed. If `test_boot_waits_for_quiescence_not_a_fixed_delay` is
flaky on a loaded machine, that is a real signal about `BOOT_POLL`, not a
reason to loosen the assert - raise the painter's sleep instead.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src/mtg_agent tests/wrapper && uv run ruff check src/mtg_agent tests/wrapper
git add src/mtg_agent/wrapper/session.py tests/wrapper/test_session.py
git commit -m "feat(wrapper): the session manager - boot dialog, quiescence, two-write submit"
```

---

### Task 8: Probe and interrupt

**Files:**
- Create: `src/mtg_agent/wrapper/probe.py`
- Modify: `src/mtg_agent/wrapper/session.py` (add `probe`, `probe_status`, `interrupt`)
- Test: `tests/wrapper/test_probe.py`

**Interfaces:**
- Consumes: `Manager` (T7).
- Produces: `ProbeState` StrEnum `PENDING="pending"`, `DELIVERED="delivered"`, `ANSWERED="answered"`. `ProbeTracker` with `start(probe_id, now) -> None`, `mark_delivered(now) -> None`, `mark_answered(now) -> None`, `status(probe_id) -> dict | None`. `ANSWER_MARKER = "MTG-ALIVE"`. `DEFAULT_PROBE_TEXT`. `ProbeUnavailableError`. On `Manager`: `probe(text: str = "") -> str`, `probe_status(probe_id: str) -> dict | None`, `interrupt() -> str | None`.

- [ ] **Step 1: Write the failing test**

`tests/wrapper/test_probe.py`:

```python
import time

import pytest

from mtg_agent.wrapper.probe import (
    ANSWER_MARKER,
    DEFAULT_PROBE_TEXT,
    ProbeState,
    ProbeTracker,
    ProbeUnavailableError,
)
from mtg_agent.wrapper.session import State
from tests.wrapper.test_session import FakeProc, fast_settings, make, ready_manager


def test_default_probe_text_asks_for_the_marker_without_stopping_work():
    assert ANSWER_MARKER in DEFAULT_PROBE_TEXT
    assert "Do not stop" in DEFAULT_PROBE_TEXT


def test_tracker_walks_pending_to_delivered_to_answered():
    tracker = ProbeTracker()
    tracker.start("p1", 1.0)
    assert tracker.status("p1")["state"] == ProbeState.PENDING
    tracker.mark_delivered(2.0)
    assert tracker.status("p1")["state"] == ProbeState.DELIVERED
    tracker.mark_answered(3.0)
    status = tracker.status("p1")
    assert status["state"] == ProbeState.ANSWERED
    assert status["answerLatency"] == 2.0


def test_a_superseded_probe_id_is_forgotten():
    tracker = ProbeTracker()
    tracker.start("p1", 1.0)
    tracker.start("p2", 2.0)
    assert tracker.status("p1") is None
    assert tracker.status("p2")["state"] == ProbeState.PENDING


def test_status_of_an_unknown_probe_is_none():
    assert ProbeTracker().status("nope") is None


def test_probe_writes_mid_turn_and_never_reports_busy(monkeypatch):
    """409-on-busy is exactly the situation a probe exists to work inside."""
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    try:
        manager.submit("a long turn")
        proc.writes.clear()
        probe_id = manager.probe()
        assert probe_id
        assert manager.state is State.BUSY, "a probe takes no turn slot"
        joined = b"".join(proc.writes)
        assert ANSWER_MARKER.encode() in joined
        assert joined.endswith(b"\r")
        assert manager.probe_status(probe_id)["state"] == ProbeState.PENDING
    finally:
        manager.shutdown()


def test_probe_does_not_consume_a_turn_slot(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    try:
        turn_id = manager.submit("go")
        manager.probe()
        manager.complete({"finalText": "done"})
        assert manager.store.get(turn_id).final_text == "done"
        assert manager.state is State.READY
    finally:
        manager.shutdown()


def test_probe_without_a_live_pty_raises_unavailable():
    manager = make(FakeProc(b""), fast_settings())
    with pytest.raises(ProbeUnavailableError):
        manager.probe()


def test_interrupt_writes_exactly_one_escape_byte(monkeypatch):
    """Unlike pasted text, which the CLI only consumes at the next tool-call
    boundary, ESC cancels synchronously even mid tool call."""
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    try:
        turn_id = manager.submit("go")
        proc.writes.clear()
        assert manager.interrupt() == turn_id
        assert proc.writes == [b"\x1b"]
    finally:
        manager.shutdown()


def test_interrupt_with_no_turn_in_flight_returns_none(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    try:
        assert manager.interrupt() is None
    finally:
        manager.shutdown()


def test_interrupt_without_a_live_pty_raises_unavailable():
    manager = make(FakeProc(b""), fast_settings())
    manager._proc = None
    with pytest.raises(ProbeUnavailableError):
        manager.interrupt()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/wrapper/test_probe.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.wrapper.probe`.

- [ ] **Step 3: Implement probe.py**

```python
"""Asking a possibly-stuck agent what it is doing, and cancelling it.

`POST /v1/messages` answers 409 for the whole duration of a turn. Without a
probe there is no way to ask a maybe-hung agent anything, only to kill it. A
probe is NOT a turn: it takes no turn slot, resets no deadline, and consumes
no turn budget.

The three states are three different diagnoses:
  pending   - the CLI took the text but never handed it to the model: the
              turn is blocked inside a single long tool call
  delivered - the model has it and is reaching tool-call boundaries
  answered  - a reply starting with the marker came back
"""

from __future__ import annotations

import threading
from enum import StrEnum

ANSWER_MARKER = "MTG-ALIVE"

DEFAULT_PROBE_TEXT = (
    "Do not stop what you are doing. Reply with exactly "
    f"{ANSWER_MARKER} <one sentence about what you are doing right now>."
)


class ProbeState(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    ANSWERED = "answered"


class ProbeUnavailableError(RuntimeError):
    """No live PTY to write to. Maps to 503, never 409."""


class ProbeTracker:
    """The single in-flight probe. Starting a new one supersedes the old,
    which is then reported as unknown rather than as a stale answer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._id: str | None = None
        self._state = ProbeState.PENDING
        self._started = 0.0
        self._answer_latency: float | None = None

    def start(self, probe_id: str, now: float) -> None:
        with self._lock:
            self._id = probe_id
            self._state = ProbeState.PENDING
            self._started = now
            self._answer_latency = None

    def mark_delivered(self, now: float) -> None:
        with self._lock:
            if self._id and self._state is ProbeState.PENDING:
                self._state = ProbeState.DELIVERED

    def mark_answered(self, now: float) -> None:
        with self._lock:
            if self._id and self._state is not ProbeState.ANSWERED:
                self._state = ProbeState.ANSWERED
                self._answer_latency = now - self._started

    def status(self, probe_id: str) -> dict | None:
        with self._lock:
            if probe_id != self._id:
                return None
            return {
                "probeId": self._id,
                "state": self._state,
                "startedAt": self._started,
                "answerLatency": self._answer_latency,
            }
```

- [ ] **Step 4: Add probe and interrupt to the Manager**

Append these imports and methods to `session.py`:

```python
from .probe import DEFAULT_PROBE_TEXT, ProbeTracker, ProbeUnavailableError
```

In `Manager.__init__`, after `self.ring = Ring()`:

```python
        self.probes = ProbeTracker()
        self._probe_seq = 0
```

And these methods on `Manager`:

```python
    def probe(self, text: str = "") -> str:
        """Write text straight into the PTY mid-turn. Never raises BusyError:
        working while a turn is in flight is the entire point."""
        with self._lock:
            proc = self._proc
            if proc is None or self._state in (State.DEAD, State.BOOTING):
                raise ProbeUnavailableError("probe unavailable: no live session")
            self._probe_seq += 1
            probe_id = f"probe-{self._probe_seq}"
        self.probes.start(probe_id, self.now())
        seq = self.submit_seq
        body = text or DEFAULT_PROBE_TEXT
        with self._pty_lock:
            try:
                proc.write((seq.paste_start + body + seq.paste_end).encode())
                time.sleep(self.settings.submit_delay)
                proc.write(seq.submit.encode())
            except OSError as exc:
                raise ProbeUnavailableError(f"probe write failed: {exc}") from exc
        self.metrics.probe_total.labels(result="written").inc()
        log.info("probe written probe_id=%s", probe_id)
        return probe_id

    def probe_status(self, probe_id: str) -> dict | None:
        return self.probes.status(probe_id)

    def interrupt(self) -> str | None:
        """One ESC byte. Pasted text is only consumed at the next tool-call
        boundary; ESC cancels synchronously even inside a running tool call,
        which is the one failure mode worth interrupting. The session, its
        id, transcript and context all survive."""
        with self._lock:
            proc = self._proc
            if proc is None or self._state is State.DEAD:
                raise ProbeUnavailableError("interrupt unavailable: no live session")
            turn_id = self._current
        with self._pty_lock:
            try:
                proc.write(b"\x1b")
            except OSError as exc:
                raise ProbeUnavailableError(f"interrupt write failed: {exc}") from exc
        log.warning("interrupt written turn_id=%s", turn_id)
        return turn_id
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/wrapper/test_probe.py tests/wrapper/test_session.py -v`
Expected: 10 + 14 passed.

- [ ] **Step 6: Commit**

```bash
uv run ruff format src/mtg_agent tests/wrapper && uv run ruff check src/mtg_agent tests/wrapper
git add src/mtg_agent/wrapper/probe.py src/mtg_agent/wrapper/session.py tests/wrapper/test_probe.py
git commit -m "feat(wrapper): mid-turn probe and single-byte ESC interrupt"
```

---

### Task 9: Bootstrap - every file claude reads at startup

**Files:**
- Create: `src/mtg_agent/wrapper/bootstrap.py`
- Test: `tests/wrapper/test_bootstrap.py`

**Interfaces:**
- Consumes: `Settings` (T1).
- Produces: `render(settings: Settings) -> None` and the individual renderers
  `write_claude_json(settings)`, `write_settings_json(settings)`,
  `merge_mcp(settings)`, `write_claude_md(settings)`, `copy_skills(settings)`.

- [ ] **Step 1: Write the failing test**

`tests/wrapper/test_bootstrap.py`:

```python
import json
import stat

from mtg_agent.wrapper import bootstrap
from mtg_agent.wrapper.config import Settings


def settings_in(tmp_path, **overrides) -> Settings:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    return Settings.from_env(
        {"WORKSPACE": str(workspace), "HOME_DIR": str(home)} | overrides
    )


def test_claude_json_seeds_every_dialog_suppressor(tmp_path):
    s = settings_in(tmp_path, ANTHROPIC_API_KEY="sk-ant-0123456789abcdefghijKLMNOP")
    bootstrap.write_claude_json(s)
    doc = json.loads((s.home_dir / ".claude.json").read_text())
    assert doc["hasCompletedOnboarding"] is True
    assert doc["projects"][str(s.workspace)]["hasTrustDialogAccepted"] is True
    assert doc["customApiKeyResponses"]["approved"] == ["456789abcdefghijKLMNOP"[-20:]]
    assert doc["customApiKeyResponses"]["rejected"] == []


def test_claude_json_is_0600(tmp_path):
    s = settings_in(tmp_path, ANTHROPIC_API_KEY="sk-ant-secret")
    bootstrap.write_claude_json(s)
    mode = stat.S_IMODE((s.home_dir / ".claude.json").stat().st_mode)
    assert mode == 0o600


def test_claude_json_merges_into_an_existing_file(tmp_path):
    s = settings_in(tmp_path, ANTHROPIC_API_KEY="sk-ant-key")
    path = s.home_dir / ".claude.json"
    path.write_text(json.dumps({"keepMe": 1, "projects": {"/other": {"x": True}}}))
    bootstrap.write_claude_json(s)
    doc = json.loads(path.read_text())
    assert doc["keepMe"] == 1
    assert doc["projects"]["/other"] == {"x": True}
    assert doc["projects"][str(s.workspace)]["hasTrustDialogAccepted"] is True


def test_claude_json_treats_a_corrupt_existing_file_as_absent(tmp_path):
    s = settings_in(tmp_path)
    (s.home_dir / ".claude.json").write_text("{not json")
    bootstrap.write_claude_json(s)
    assert json.loads((s.home_dir / ".claude.json").read_text())["hasCompletedOnboarding"]


def test_claude_json_without_an_api_key_has_no_key_responses(tmp_path):
    s = settings_in(tmp_path)
    bootstrap.write_claude_json(s)
    assert "customApiKeyResponses" not in json.loads((s.home_dir / ".claude.json").read_text())


def test_settings_json_installs_the_stop_hook_and_bypass_mode(tmp_path):
    s = settings_in(tmp_path, HOOK_COMMAND="/usr/local/bin/cc-stop-hook")
    bootstrap.write_settings_json(s)
    doc = json.loads((s.home_dir / ".claude" / "settings.json").read_text())
    stop = doc["hooks"]["Stop"][0]
    assert stop["matcher"] == ""
    assert stop["hooks"] == [{"type": "command", "command": "/usr/local/bin/cc-stop-hook"}]
    assert doc["permissions"]["defaultMode"] == "bypassPermissions"
    assert doc["enableAllProjectMcpServers"] is True


def test_settings_json_denies_the_interactive_tools_a_headless_agent_cannot_use(tmp_path):
    s = settings_in(tmp_path)
    bootstrap.write_settings_json(s)
    deny = json.loads((s.home_dir / ".claude" / "settings.json").read_text())["permissions"]["deny"]
    assert "AskUserQuestion" in deny
    assert "ExitPlanMode" in deny
    assert "EnterPlanMode" in deny


def test_mcp_merge_unions_base_and_overlays_with_overlays_winning(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(
        json.dumps({"mcpServers": {"mtg": {"command": "old"}, "keep": {"type": "http"}}})
    )
    overlay_dir = tmp_path / "mcp.d"
    overlay_dir.mkdir()
    (overlay_dir / "10-mtg.json").write_text(
        json.dumps({"mcpServers": {"mtg": {"command": "new"}}})
    )
    (overlay_dir / "20-extra.json").write_text(
        json.dumps({"mcpServers": {"spellslinger": {"type": "http", "url": "https://x"}}})
    )
    (overlay_dir / "notes.txt").write_text("ignored")
    s = settings_in(tmp_path, BASE_MCP_PATH=str(base), MCP_OVERLAY_DIR=str(overlay_dir))
    bootstrap.merge_mcp(s)
    servers = json.loads((s.workspace / ".mcp.json").read_text())["mcpServers"]
    assert servers["mtg"] == {"command": "new"}
    assert servers["keep"] == {"type": "http"}
    assert servers["spellslinger"]["url"] == "https://x"


def test_mcp_json_is_0600(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"mcpServers": {}}))
    s = settings_in(tmp_path, BASE_MCP_PATH=str(base))
    bootstrap.merge_mcp(s)
    assert stat.S_IMODE((s.workspace / ".mcp.json").stat().st_mode) == 0o600


def test_mcp_merge_with_no_base_and_no_overlay_writes_an_empty_doc(tmp_path):
    s = settings_in(tmp_path)
    bootstrap.merge_mcp(s)
    assert json.loads((s.workspace / ".mcp.json").read_text()) == {"mcpServers": {}}


def test_claude_md_files_are_copied_to_both_locations(tmp_path):
    project = tmp_path / "project.md"
    project.write_text("# project rules")
    glob = tmp_path / "global.md"
    glob.write_text("# global rules")
    s = settings_in(
        tmp_path, PROJECT_CLAUDE_MD_PATH=str(project), GLOBAL_CLAUDE_MD_PATH=str(glob)
    )
    bootstrap.write_claude_md(s)
    assert (s.workspace / "CLAUDE.md").read_text() == "# project rules"
    assert (s.home_dir / ".claude" / "CLAUDE.md").read_text() == "# global rules"


def test_missing_claude_md_sources_are_skipped_silently(tmp_path):
    s = settings_in(tmp_path, PROJECT_CLAUDE_MD_PATH=str(tmp_path / "gone.md"))
    bootstrap.write_claude_md(s)
    assert not (s.workspace / "CLAUDE.md").exists()


def test_skills_are_copied_in_order_with_later_dirs_winning(tmp_path):
    baked = tmp_path / "baked"
    (baked / "play").mkdir(parents=True)
    (baked / "play" / "SKILL.md").write_text("baked")
    custom = tmp_path / "custom"
    (custom / "play").mkdir(parents=True)
    (custom / "play" / "SKILL.md").write_text("custom")
    (custom / "extra").mkdir()
    (custom / "extra" / "SKILL.md").write_text("extra")
    s = settings_in(tmp_path, SKILLS_DIRS=f"{baked}:{custom}")
    bootstrap.copy_skills(s)
    root = s.workspace / ".claude" / "skills"
    assert (root / "play" / "SKILL.md").read_text() == "custom"
    assert (root / "extra" / "SKILL.md").read_text() == "extra"


def test_render_produces_every_file_in_one_call(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"mcpServers": {"mtg": {"command": "x"}}}))
    s = settings_in(tmp_path, BASE_MCP_PATH=str(base), ANTHROPIC_API_KEY="sk-ant-abc")
    bootstrap.render(s)
    assert (s.home_dir / ".claude.json").exists()
    assert (s.home_dir / ".claude" / "settings.json").exists()
    assert (s.workspace / ".mcp.json").exists()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/wrapper/test_bootstrap.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.wrapper.bootstrap`.

- [ ] **Step 3: Implement bootstrap.py**

```python
"""Render every file claude reads at startup, before it is spawned.

A fresh HOME otherwise walks into several interactive dialogs that a
headless container has nobody to answer. Onboarding, folder trust and the
custom-API-key prompt are all suppressed by seeding ~/.claude.json. The
bypass-permissions warning is NOT seedable and is handled at boot by
session.boot_wait instead.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .config import Settings

log = logging.getLogger("wrapper.bootstrap")

#: Interactive tools a headless agent cannot use: a picker with nobody at the
#: terminal can only stall the turn. Deny wins even under bypassPermissions.
DENY_TOOLS = ["AskUserQuestion", "ExitPlanMode", "EnterPlanMode"]


def _write_json(path: Path, doc: Any, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    path.chmod(mode)


def write_claude_json(settings: Settings) -> None:
    """The no-dialog seed. Merged into any existing file rather than
    overwriting, so claude-written state from a prior run survives."""
    path = settings.home_dir / ".claude.json"
    doc: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict):
            doc = loaded
    except (OSError, ValueError):
        pass  # absent or corrupt: treat as empty

    doc["hasCompletedOnboarding"] = True
    doc["autoUpdates"] = True

    projects = doc.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    project = projects.get(str(settings.workspace))
    if not isinstance(project, dict):
        project = {}
    project["hasTrustDialogAccepted"] = True
    projects[str(settings.workspace)] = project
    doc["projects"] = projects

    if settings.anthropic_api_key:
        suffix = settings.anthropic_api_key[-20:]
        responses = doc.get("customApiKeyResponses")
        if not isinstance(responses, dict):
            responses = {}
        approved = [a for a in responses.get("approved", []) if isinstance(a, str)]
        if suffix not in approved:
            approved.append(suffix)
        responses["approved"] = approved
        responses.setdefault("rejected", [])
        doc["customApiKeyResponses"] = responses

    _write_json(path, doc, 0o600)


def write_settings_json(settings: Settings) -> None:
    doc: dict[str, Any] = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": settings.hook_command}],
                }
            ]
        },
        "enableAllProjectMcpServers": settings.enable_all_mcp,
        "permissions": {"deny": list(DENY_TOOLS)},
    }
    if settings.permission_mode:
        doc["permissions"]["defaultMode"] = settings.permission_mode
    _write_json(settings.home_dir / ".claude" / "settings.json", doc, 0o644)


def merge_mcp(settings: Settings) -> None:
    """Base config unioned with every *.json in the overlay dir. Overlay keys
    win. 0600 because an overlay can carry a URL with a token in it."""
    servers: dict[str, Any] = {}
    if settings.base_mcp_path is not None:
        try:
            base = json.loads(settings.base_mcp_path.read_text())
            servers.update(base.get("mcpServers") or {})
        except (OSError, ValueError) as exc:
            log.warning("base mcp unreadable, ignoring: %s", exc)
    if settings.mcp_overlay_dir is not None and settings.mcp_overlay_dir.is_dir():
        for entry in sorted(settings.mcp_overlay_dir.iterdir()):
            if entry.is_dir() or entry.suffix != ".json":
                continue
            try:
                fragment = json.loads(entry.read_text())
            except (OSError, ValueError) as exc:
                log.warning("mcp overlay %s unreadable, ignoring: %s", entry.name, exc)
                continue
            servers.update(fragment.get("mcpServers") or {})
    _write_json(settings.workspace / ".mcp.json", {"mcpServers": servers}, 0o600)


def write_claude_md(settings: Settings) -> None:
    pairs = [
        (settings.project_claude_md_path, settings.workspace / "CLAUDE.md"),
        (settings.global_claude_md_path, settings.home_dir / ".claude" / "CLAUDE.md"),
    ]
    for source, target in pairs:
        if source is None or not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def copy_skills(settings: Settings) -> None:
    """Later directories win, so a mounted custom skill overrides a baked one
    of the same name."""
    root = settings.workspace / ".claude" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    for source in settings.skills_dirs:
        if not source.is_dir():
            continue
        shutil.copytree(source, root, dirs_exist_ok=True)


def render(settings: Settings) -> None:
    settings.workspace.mkdir(parents=True, exist_ok=True)
    settings.home_dir.mkdir(parents=True, exist_ok=True)
    write_claude_md(settings)
    merge_mcp(settings)
    write_settings_json(settings)
    write_claude_json(settings)
    copy_skills(settings)
    log.info("bootstrap rendered workspace=%s home=%s", settings.workspace, settings.home_dir)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/wrapper/test_bootstrap.py -v`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src/mtg_agent tests/wrapper && uv run ruff check src/mtg_agent tests/wrapper
git add src/mtg_agent/wrapper/bootstrap.py tests/wrapper/test_bootstrap.py
git commit -m "feat(wrapper): render the claude.json seed, settings, MCP config and skills"
```

---

### Task 10: Metrics and webhook delivery

**Files:**
- Create: `src/mtg_agent/wrapper/metrics.py`, `src/mtg_agent/wrapper/webhook.py`
- Test: `tests/wrapper/test_webhook.py`

**Interfaces:**
- Consumes: `Record` (T4).
- Produces:
  - `Metrics(registry=None)` with attributes `turns_total` (Counter, label `result`), `turn_duration_seconds` (Histogram), `turn_in_flight` (Gauge), `claude_restarts_total` (Counter), `webhook_delivery_total` (Counter, label `result`), `hook_received_total` (Counter), `turn_tokens_total` (Counter, labels `type`,`model`), `turn_cost_usd_total` (Counter), `turn_stall_suspected_total` (Counter), `probe_total` (Counter, label `result`), `registry`. Methods `record_tokens(list[dict])`, `record_cost(result_json)`, `render() -> tuple[bytes, str]`.
  - `Deliverer(metrics, *, retries=3, backoff=0.5, timeout=10.0, default_url="")` with `deliver(record: Record) -> None` (synchronous, used from a thread) and `deliver_async(record)`.

- [ ] **Step 1: Write the failing test**

`tests/wrapper/test_webhook.py`:

```python
import pytest

from mtg_agent.wrapper.metrics import Metrics
from mtg_agent.wrapper.turns import Record, TurnState
from mtg_agent.wrapper.webhook import Deliverer


def a_record(callback_url: str = "https://cb.example/x") -> Record:
    return Record(
        id="turn-1",
        state=TurnState.COMPLETE,
        text="go",
        callback_url=callback_url,
        started_at=1.0,
        completed_at=2.0,
        final_text="PONG",
    )


def test_metrics_expose_the_ccw_names(tmp_path):
    metrics = Metrics()
    metrics.turns_total.labels(result="complete").inc()
    body, content_type = metrics.render()
    assert b"ccw_turns_total" in body
    assert b"ccw_turn_in_flight" in body
    assert "text/plain" in content_type


def test_record_tokens_counts_every_type_and_model():
    metrics = Metrics()
    metrics.record_tokens(
        [{"model": "sonnet", "input": 10, "output": 3, "cache_read": 5, "cache_creation": 1}]
    )
    body = metrics.render()[0].decode()
    assert 'ccw_turn_tokens_total{model="sonnet",type="input"} 10.0' in body
    assert 'ccw_turn_tokens_total{model="sonnet",type="cache_read"} 5.0' in body


def test_record_cost_only_counts_when_the_agent_wrote_one():
    metrics = Metrics()
    metrics.record_cost(None)
    metrics.record_cost({"nothing": 1})
    assert metrics.turn_cost_usd_total._value.get() == 0
    metrics.record_cost({"total_cost_usd": 0.25})
    assert metrics.turn_cost_usd_total._value.get() == pytest.approx(0.25)


def test_delivery_posts_the_full_record(monkeypatch):
    posted = []
    metrics = Metrics()
    deliverer = Deliverer(metrics)
    monkeypatch.setattr(
        deliverer, "_post", lambda url, body: posted.append((url, body))
    )
    deliverer.deliver(a_record())
    url, body = posted[0]
    assert url == "https://cb.example/x"
    assert body["turnId"] == "turn-1"
    assert body["finalText"] == "PONG"
    assert metrics.webhook_delivery_total.labels(result="ok")._value.get() == 1


def test_delivery_retries_then_drops_and_counts_it(monkeypatch):
    attempts = []
    metrics = Metrics()
    deliverer = Deliverer(metrics, retries=3, backoff=0)

    def always_fail(url, body):
        attempts.append(url)
        raise OSError("refused")

    monkeypatch.setattr(deliverer, "_post", always_fail)
    deliverer.deliver(a_record())
    assert len(attempts) == 3
    assert metrics.webhook_delivery_total.labels(result="dropped")._value.get() == 1


def test_a_record_without_a_callback_url_falls_back_to_the_default(monkeypatch):
    posted = []
    deliverer = Deliverer(Metrics(), default_url="https://default.example/hook")
    monkeypatch.setattr(deliverer, "_post", lambda url, body: posted.append(url))
    deliverer.deliver(a_record(callback_url=""))
    assert posted == ["https://default.example/hook"]


def test_no_callback_url_at_all_is_a_silent_no_op(monkeypatch):
    posted = []
    deliverer = Deliverer(Metrics())
    monkeypatch.setattr(deliverer, "_post", lambda url, body: posted.append(url))
    deliverer.deliver(a_record(callback_url=""))
    assert posted == []
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/wrapper/test_webhook.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.wrapper.metrics`.

- [ ] **Step 3: Implement metrics.py**

```python
"""The ccw_* Prometheus collectors, keeping the Go original's metric names.

A private registry rather than the process-global default, so tests can build
as many Metrics as they like without duplicate-timeseries errors.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.turns_total = Counter(
            "ccw_turns_total", "Turns by result", ["result"], registry=r
        )
        self.turn_duration_seconds = Histogram(
            "ccw_turn_duration_seconds", "Turn duration", registry=r
        )
        self.turn_in_flight = Gauge(
            "ccw_turn_in_flight", "1 while a turn is running", registry=r
        )
        self.claude_restarts_total = Counter(
            "ccw_claude_restarts_total", "claude process exits", registry=r
        )
        self.webhook_delivery_total = Counter(
            "ccw_webhook_delivery_total", "Callback deliveries", ["result"], registry=r
        )
        self.hook_received_total = Counter(
            "ccw_hook_received_total", "Stop-hook callbacks received", registry=r
        )
        self.turn_tokens_total = Counter(
            "ccw_turn_tokens_total", "Tokens by type", ["type", "model"], registry=r
        )
        self.turn_cost_usd_total = Counter(
            "ccw_turn_cost_usd_total", "Cumulative turn cost", registry=r
        )
        self.turn_stall_suspected_total = Counter(
            "ccw_turn_stall_suspected_total", "Inactivity timer firings", registry=r
        )
        self.probe_total = Counter(
            "ccw_probe_total", "Probes by result", ["result"], registry=r
        )

    def record_tokens(self, turn_tokens: list[dict[str, Any]]) -> None:
        for entry in turn_tokens or []:
            model = entry.get("model") or ""
            for key in ("input", "output", "cache_read", "cache_creation"):
                value = entry.get(key) or 0
                if value:
                    self.turn_tokens_total.labels(type=key, model=model).inc(value)

    def record_cost(self, result_json: Any) -> None:
        if not isinstance(result_json, dict):
            return
        cost = result_json.get("total_cost_usd")
        if isinstance(cost, int | float):
            self.turn_cost_usd_total.inc(float(cost))

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
```

- [ ] **Step 4: Implement webhook.py**

```python
"""Deliver a finished turn to its callback URL, retrying.

Delivery is best-effort by design: the result is ALWAYS also retrievable
from GET /v1/messages/{turnId}, so a dropped callback costs a poll, not a
turn.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from .metrics import Metrics
from .turns import Record

log = logging.getLogger("wrapper.webhook")


class Deliverer:
    def __init__(
        self,
        metrics: Metrics,
        *,
        retries: int = 3,
        backoff: float = 0.5,
        timeout: float = 10.0,
        default_url: str = "",
    ) -> None:
        self.metrics = metrics
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.default_url = default_url

    def _post(self, url: str, body: dict) -> None:
        httpx.post(url, json=body, timeout=self.timeout).raise_for_status()

    def deliver(self, record: Record) -> None:
        url = record.callback_url or self.default_url
        if not url:
            return
        body = record.full()
        for attempt in range(self.retries):
            if attempt:
                time.sleep(self.backoff * (2 ** (attempt - 1)))
            try:
                self._post(url, body)
                self.metrics.webhook_delivery_total.labels(result="ok").inc()
                return
            except Exception as exc:  # noqa: BLE001 - every failure retries alike
                log.warning(
                    "webhook attempt %d/%d to %s failed: %s",
                    attempt + 1,
                    self.retries,
                    url,
                    exc,
                )
        self.metrics.webhook_delivery_total.labels(result="dropped").inc()
        log.error("webhook dropped turn_id=%s url=%s", record.id, url)

    def deliver_async(self, record: Record) -> None:
        threading.Thread(target=self.deliver, args=(record,), daemon=True).start()
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/wrapper/test_webhook.py -v`
Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
uv run ruff format src/mtg_agent tests/wrapper && uv run ruff check src/mtg_agent tests/wrapper
git add src/mtg_agent/wrapper/metrics.py src/mtg_agent/wrapper/webhook.py tests/wrapper/test_webhook.py
git commit -m "feat(wrapper): ccw_* metrics and retrying webhook delivery"
```

---

### Task 11: The HTTP API and process wiring

**Files:**
- Create: `src/mtg_agent/wrapper/api.py`, `src/mtg_agent/wrapper/__main__.py`
- Test: `tests/wrapper/test_api.py`

**Interfaces:**
- Consumes: `Manager` (T7/T8), `Store` (T4), `Metrics` (T10), `Settings` (T1).
- Produces: `build_public_app(manager, settings) -> FastAPI` and
  `build_internal_app(manager) -> FastAPI`. Public routes exactly as the spec's
  table; internal route `POST /internal/turn-complete`.

- [ ] **Step 1: Write the failing test**

`tests/wrapper/test_api.py`:

```python
import pytest
from fastapi.testclient import TestClient

from mtg_agent.wrapper.api import build_internal_app, build_public_app
from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.session import State
from tests.wrapper.test_session import FakeProc, fast_settings, ready_manager


@pytest.fixture
def wired(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    settings = manager.settings
    public = TestClient(build_public_app(manager, settings))
    internal = TestClient(build_internal_app(manager))
    yield manager, proc, public, internal
    manager.shutdown()


def test_post_messages_starts_a_turn(wired):
    _, proc, public, _ = wired
    response = public.post("/v1/messages", json={"text": "play a land"})
    assert response.status_code == 202
    assert response.json()["turnId"].startswith("turn-")
    assert b"".join(proc.writes).endswith(b"\r")


def test_post_messages_while_busy_is_409(wired):
    _, _, public, _ = wired
    public.post("/v1/messages", json={"text": "first"})
    assert public.post("/v1/messages", json={"text": "second"}).status_code == 409


def test_post_messages_without_text_is_400(wired):
    _, _, public, _ = wired
    assert public.post("/v1/messages", json={"text": "  "}).status_code == 400


def test_post_messages_when_dead_is_503(wired):
    manager, proc, public, _ = wired
    manager._state = State.DEAD
    assert public.post("/v1/messages", json={"text": "x"}).status_code == 503


def test_the_full_turn_round_trip_through_the_internal_endpoint(wired):
    _, _, public, internal = wired
    turn_id = public.post("/v1/messages", json={"text": "go"}).json()["turnId"]
    posted = internal.post(
        "/internal/turn-complete",
        json={
            "sessionId": "s1",
            "finalText": "I attack with the Frog",
            "transcriptPath": "/workspace/t.jsonl",
            "usage": {"output_tokens": 9},
            "stopReason": "end_turn",
            "turnTokens": [],
            "resultJson": None,
        },
    )
    assert posted.status_code == 202
    record = public.get(f"/v1/messages/{turn_id}").json()
    assert record["state"] == "complete"
    assert record["finalText"] == "I attack with the Frog"


def test_get_unknown_turn_is_404(wired):
    _, _, public, _ = wired
    assert public.get("/v1/messages/turn-nope").status_code == 404


def test_get_messages_lists_summaries_only(wired):
    _, _, public, internal = wired
    public.post("/v1/messages", json={"text": "go"})
    internal.post("/internal/turn-complete", json={"finalText": "done"})
    listed = public.get("/v1/messages").json()
    assert len(listed) == 1
    assert set(listed[0]) == {"turnId", "state", "startedAt", "completedAt", "lastActivityAt"}


def test_get_session_reports_the_snapshot(wired):
    _, _, public, _ = wired
    body = public.get("/v1/session").json()
    assert body["state"] == "ready"
    assert body["turnsCompleted"] == 0


def test_transcript_is_404_before_the_first_turn(wired):
    _, _, public, _ = wired
    assert public.get("/v1/transcript").status_code == 404


def test_transcript_is_served_once_a_turn_recorded_one(wired, tmp_path):
    manager, _, public, _ = wired
    path = tmp_path / "t.jsonl"
    path.write_text('{"type":"assistant"}\n')
    manager.transcript_path = str(path)
    response = public.get("/v1/transcript")
    assert response.status_code == 200
    assert '"type":"assistant"' in response.text


def test_probe_never_returns_409_while_busy(wired):
    _, _, public, _ = wired
    public.post("/v1/messages", json={"text": "long turn"})
    response = public.post("/v1/probe", json={})
    assert response.status_code == 202
    probe_id = response.json()["probeId"]
    assert public.get(f"/v1/probe/{probe_id}").json()["state"] == "pending"


def test_probe_without_a_live_session_is_503(wired):
    manager, _, public, _ = wired
    manager._state = State.DEAD
    assert public.post("/v1/probe", json={}).status_code == 503


def test_unknown_probe_is_404(wired):
    _, _, public, _ = wired
    assert public.get("/v1/probe/probe-999").status_code == 404


def test_interrupt_never_returns_409(wired):
    _, proc, public, _ = wired
    public.post("/v1/messages", json={"text": "go"})
    proc.writes.clear()
    response = public.post("/v1/interrupt")
    assert response.status_code == 202
    assert proc.writes == [b"\x1b"]


def test_operator_endpoints_are_open(wired):
    _, _, public, _ = wired
    assert public.get("/healthz").status_code == 200
    assert public.get("/readyz").status_code == 200
    assert b"ccw_turns_total" in public.get("/metrics").content


def test_readyz_is_503_when_the_session_is_dead(wired):
    manager, _, public, _ = wired
    manager._state = State.DEAD
    assert public.get("/readyz").status_code == 503


def test_the_shared_secret_gates_v1_but_not_the_operator_surface(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc, fast_settings(WRAPPER_SHARED_SECRET="hunter2"))
    client = TestClient(build_public_app(manager, manager.settings))
    try:
        assert client.get("/v1/session").status_code == 401
        assert client.get("/healthz").status_code == 200
        ok = client.get("/v1/session", headers={"X-Wrapper-Secret": "hunter2"})
        assert ok.status_code == 200
    finally:
        manager.shutdown()


def test_no_shared_secret_means_open(wired):
    _, _, public, _ = wired
    assert public.get("/v1/session").status_code == 200


def test_settings_default_has_no_secret():
    assert Settings.from_env({}).shared_secret == ""
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/wrapper/test_api.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.wrapper.api`.

- [ ] **Step 3: Implement api.py**

```python
"""Two FastAPI apps on two listeners.

The public one carries /v1 and the operator surface. The internal one carries
exactly one route - the Stop hook's target - and is bound to 127.0.0.1 so it
is unreachable from outside the container. Separate apps, not separate
routers on one app: a single misconfigured bind should not expose the hook
endpoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from .config import Settings
from .probe import ProbeUnavailableError
from .session import BusyError, DeadError, Manager, NotReadyError


class MessageIn(BaseModel):
    text: str = ""
    callbackUrl: str = ""


class ProbeIn(BaseModel):
    text: str = ""


def build_public_app(manager: Manager, settings: Settings) -> FastAPI:
    app = FastAPI(title="mtg-agent-wrapper")

    def require_secret(request: Request) -> None:
        if not settings.shared_secret:
            return
        if request.headers.get("X-Wrapper-Secret") != settings.shared_secret:
            raise HTTPException(status_code=401, detail="bad or missing X-Wrapper-Secret")

    guard = [Depends(require_secret)]

    @app.post("/v1/messages", status_code=202, dependencies=guard)
    def post_message(body: MessageIn) -> dict[str, str]:
        if not body.text.strip():
            raise HTTPException(status_code=400, detail="text is required")
        try:
            turn_id = manager.submit(body.text, body.callbackUrl)
        except BusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (DeadError, NotReadyError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"turnId": turn_id}

    @app.get("/v1/messages", dependencies=guard)
    def list_messages() -> list[dict]:
        return [record.summary() for record in manager.store.list()]

    @app.get("/v1/messages/{turn_id}", dependencies=guard)
    def get_message(turn_id: str) -> dict:
        record = manager.store.get(turn_id)
        if record is None:
            raise HTTPException(status_code=404, detail="no such turn")
        return record.full()

    @app.get("/v1/session", dependencies=guard)
    def get_session() -> dict:
        return manager.snapshot()

    @app.delete("/v1/session", status_code=202, dependencies=guard)
    def delete_session() -> dict:
        manager.shutdown()
        return {"stopping": True}

    @app.get("/v1/transcript", dependencies=guard)
    def get_transcript() -> Response:
        path = Path(manager.transcript_path) if manager.transcript_path else None
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="no transcript yet")
        return Response(content=path.read_text(), media_type="application/jsonl")

    @app.post("/v1/probe", status_code=202, dependencies=guard)
    def post_probe(body: ProbeIn = Body(default=ProbeIn())) -> dict:
        try:
            probe_id = manager.probe(body.text)
        except ProbeUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        status = manager.probe_status(probe_id) or {}
        return {"probeId": probe_id} | {k: v for k, v in status.items() if k != "probeId"}

    @app.get("/v1/probe/{probe_id}", dependencies=guard)
    def get_probe(probe_id: str) -> dict:
        status = manager.probe_status(probe_id)
        if status is None:
            raise HTTPException(status_code=404, detail="unknown or superseded probe")
        return status

    @app.post("/v1/interrupt", status_code=202, dependencies=guard)
    def post_interrupt() -> dict:
        try:
            turn_id = manager.interrupt()
        except ProbeUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"turnId": turn_id}

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    def readyz(response: Response) -> dict:
        snapshot = manager.snapshot()
        if snapshot["state"] in ("dead", "booting"):
            raise HTTPException(status_code=503, detail=snapshot["state"])
        return snapshot

    @app.get("/metrics")
    def metrics() -> Response:
        body, content_type = manager.metrics.render()
        return Response(content=body, media_type=content_type)

    return app


def build_internal_app(manager: Manager) -> FastAPI:
    app = FastAPI(title="mtg-agent-wrapper-internal")

    @app.post("/internal/turn-complete", status_code=202)
    def turn_complete(result: dict[str, Any]) -> dict:
        manager.complete(result)
        return {"accepted": True}

    return app
```

- [ ] **Step 4: Implement `__main__.py`**

```python
"""PID 1: config -> bootstrap -> session -> two servers.

The internal server binds loopback only. The public server is not served
until the session has booted, so a container health check simply retries
while claude is still starting.
"""

from __future__ import annotations

import logging
import os
import signal
import threading

import uvicorn

from . import bootstrap
from .api import build_internal_app, build_public_app
from .config import Settings
from .metrics import Metrics
from .session import Manager
from .turns import Store
from .webhook import Deliverer

log = logging.getLogger("wrapper")


def _split_addr(addr: str, default_host: str) -> tuple[str, int]:
    host, _, port = addr.rpartition(":")
    return (host or default_host), int(port)


def main() -> None:
    settings = Settings.from_env(os.environ)
    logging.basicConfig(level=settings.log_level.upper(), format="%(levelname)s %(name)s %(message)s")

    bootstrap.render(settings)

    metrics = Metrics()
    store = Store()
    deliverer = Deliverer(
        metrics, retries=settings.webhook_retries, default_url=settings.default_callback_url
    )
    manager = Manager(settings, store, metrics, on_turn_done=deliverer.deliver_async)

    internal_host, internal_port = _split_addr(settings.internal_addr, "127.0.0.1")
    internal = uvicorn.Server(
        uvicorn.Config(
            build_internal_app(manager), host=internal_host, port=internal_port, log_level="warning"
        )
    )
    threading.Thread(target=internal.run, daemon=True).start()

    log.info("booting claude")
    manager.start()
    log.info("session state=%s", manager.state)

    public_host, public_port = _split_addr(settings.http_addr, "0.0.0.0")
    server = uvicorn.Server(
        uvicorn.Config(
            build_public_app(manager, settings),
            host=public_host,
            port=public_port,
            log_level=settings.log_level,
        )
    )

    def stop(_signum, _frame) -> None:
        manager.shutdown()
        server.should_exit = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.run()
    manager.shutdown()


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/wrapper -v`
Expected: all wrapper tests pass (roughly 75).

- [ ] **Step 6: Commit**

```bash
uv run ruff format src/mtg_agent tests/wrapper && uv run ruff check src/mtg_agent tests/wrapper
git add src/mtg_agent/wrapper/api.py src/mtg_agent/wrapper/__main__.py tests/wrapper/test_api.py
git commit -m "feat(wrapper): HTTP API on a public listener plus a loopback-only hook endpoint"
```

---

### Task 12: End-to-end integration against a stub claude

Proves the whole chain with no API key and no network: bootstrap seed, PTY
boot, dialog accept, two-write submit, hook callback, result retrievable.

**Files:**
- Create: `tests/testdata/stubclaude.py`, `tests/wrapper/test_integration.py`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing importable.

- [ ] **Step 1: Write the stub claude**

`tests/testdata/stubclaude.py`:

```python
"""A stand-in for the interactive claude binary, for the integration test.

It reproduces only the three behaviours the wrapper actually depends on:

1. it prints the un-seedable "Bypass Permissions mode" warning at boot and
   refuses to accept input until it has seen Down then Enter;
2. it accepts a bracketed-paste block followed by a separate CR;
3. at end of "turn" it runs the Stop hook, feeding it a payload on stdin the
   same shape the real binary uses.

It is a test double for a subprocess, not for the rules engine: it never
touches a game.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"


def main() -> None:
    hook = os.environ["STUB_HOOK_COMMAND"]
    transcript = os.environ.get("STUB_TRANSCRIPT", "")
    print("Bypass Permissions mode", flush=True)
    print("  Yes, I accept / No, exit", flush=True)

    accepted = False
    buffer = ""
    while True:
        char = sys.stdin.read(1)
        if not char:
            return
        buffer += char
        if not accepted:
            if buffer.endswith("\x1b[B\r") or buffer.endswith("\x1b[B\n"):
                accepted = True
                buffer = ""
                print("accepted, ready", flush=True)
            continue
        if PASTE_END in buffer and buffer.rstrip().endswith(("\r", "\n")):
            start = buffer.find(PASTE_START) + len(PASTE_START)
            text = buffer[start : buffer.find(PASTE_END)]
            buffer = ""
            print(f"working on: {text}", flush=True)
            payload = {
                "session_id": "stub-session",
                "transcript_path": transcript,
                "cwd": os.getcwd(),
                "hook_event_name": "Stop",
                "last_assistant_message": f"ACK:{text}",
            }
            subprocess.run(
                hook, shell=True, input=json.dumps(payload).encode(), check=False
            )
            print("turn done", flush=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the failing integration test**

`tests/wrapper/test_integration.py`:

```python
import json
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from mtg_agent.wrapper import bootstrap
from mtg_agent.wrapper.api import build_internal_app, build_public_app
from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.metrics import Metrics
from mtg_agent.wrapper.session import Manager, State
from mtg_agent.wrapper.turns import Store

STUB = Path(__file__).resolve().parents[1] / "testdata" / "stubclaude.py"
SRC = Path(__file__).resolve().parents[2] / "src"


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """The real wrapper, with the stub standing in for the claude binary and
    a real loopback internal server for the hook to POST to."""
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.5)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 0.5)

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    transcript = workspace / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "transcript text"}],
                    "model": "stub-model",
                    "usage": {"input_tokens": 11, "output_tokens": 4},
                    "stop_reason": "end_turn",
                },
            }
        )
        + "\n"
    )

    port = free_port()
    internal_url = f"http://127.0.0.1:{port}/internal/turn-complete"
    hook_command = (
        f"PYTHONPATH={SRC} CCW_INTERNAL_URL={internal_url} "
        f"CCW_RESULT_JSON={workspace / 'result.json'} "
        f"{sys.executable} -m mtg_agent.wrapper.stop_hook"
    )

    settings = Settings.from_env(
        {
            "CLAUDE_PATH": sys.executable,
            "WORKSPACE": str(workspace),
            "HOME_DIR": str(home),
            "BOOT_TIMEOUT_SECONDS": "30",
            "SUBMIT_DELAY_SECONDS": "0.2",
            "TURN_TIMEOUT_SECONDS": "60",
            "ANTHROPIC_API_KEY": "sk-ant-stub-key-0123456789",
        }
    )
    bootstrap.render(settings)

    monkeypatch.setenv("STUB_HOOK_COMMAND", hook_command)
    monkeypatch.setenv("STUB_TRANSCRIPT", str(transcript))
    monkeypatch.setenv("PYTHONPATH", str(SRC))

    manager = Manager(settings, Store(), Metrics())
    original_spawn = manager.spawn
    manager.spawn = lambda s, resume: __import__(
        "mtg_agent.wrapper.pty_proc", fromlist=["spawn_claude"]
    ).spawn_claude(s, resume, extra_args=[str(STUB)])
    assert original_spawn is not None

    server = uvicorn.Server(
        uvicorn.Config(build_internal_app(manager), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)

    manager.start()
    yield manager, settings, workspace
    manager.shutdown()
    server.should_exit = True


def test_the_bootstrap_seed_lands_before_claude_starts(stack):
    _, settings, workspace = stack
    seed = json.loads((settings.home_dir / ".claude.json").read_text())
    assert seed["projects"][str(workspace)]["hasTrustDialogAccepted"] is True
    assert seed["customApiKeyResponses"]["approved"] == ["nt-stub-key-0123456789"]
    assert (workspace / ".mcp.json").exists()


def test_boot_accepts_the_dialog_and_reaches_ready(stack):
    manager, _, _ = stack
    assert manager.state is State.READY
    assert manager.ring.contains("accepted, ready")


def test_a_full_turn_flows_from_submit_to_a_stored_result(stack):
    manager, settings, _ = stack
    client = TestClient(build_public_app(manager, settings))
    turn_id = client.post("/v1/messages", json={"text": "attack with the Frog"}).json()["turnId"]

    deadline = time.monotonic() + 30
    while manager.state is not State.READY and time.monotonic() < deadline:
        time.sleep(0.05)

    record = client.get(f"/v1/messages/{turn_id}").json()
    assert record["state"] == "complete"
    assert record["finalText"] == "ACK:attack with the Frog"
    assert record["stopReason"] == "end_turn"
    assert record["usage"]["output_tokens"] == 4
    assert record["turnTokens"] == [
        {"model": "stub-model", "input": 11, "output": 4, "cache_read": 0, "cache_creation": 0}
    ]


def test_a_second_turn_runs_in_the_same_claude_session(stack):
    """The point of the whole component: the process is long-lived, so turn
    two shares turn one's context."""
    manager, settings, _ = stack
    client = TestClient(build_public_app(manager, settings))
    for text in ("first", "second"):
        client.post("/v1/messages", json={"text": text})
        deadline = time.monotonic() + 30
        while manager.state is not State.READY and time.monotonic() < deadline:
            time.sleep(0.05)
    states = [t["state"] for t in client.get("/v1/messages").json()]
    assert states == ["complete", "complete"]
    assert manager.snapshot()["turnsCompleted"] == 2


def test_the_callback_url_receives_the_finished_turn(stack):
    manager, settings, _ = stack
    received: list[dict] = []
    port = free_port()
    from fastapi import FastAPI

    sink = FastAPI()

    @sink.post("/hook")
    def hook(body: dict) -> dict:
        received.append(body)
        return {"ok": True}

    server = uvicorn.Server(
        uvicorn.Config(sink, host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)

    from mtg_agent.wrapper.webhook import Deliverer

    manager.on_turn_done = Deliverer(manager.metrics, backoff=0).deliver
    client = TestClient(build_public_app(manager, settings))
    client.post(
        "/v1/messages",
        json={"text": "go", "callbackUrl": f"http://127.0.0.1:{port}/hook"},
    )
    deadline = time.monotonic() + 30
    while not received and time.monotonic() < deadline:
        time.sleep(0.05)
    server.should_exit = True
    assert received[0]["finalText"] == "ACK:go"
    assert httpx is not None
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/wrapper/test_integration.py -v`
Expected: FAIL - `tests/testdata/stubclaude.py` not found, or the boot never
reaches READY, until Step 1's stub is in place and every prior task is done.

- [ ] **Step 4: Run it and watch it pass**

Run: `uv run pytest tests/wrapper/test_integration.py -v`
Expected: 5 passed. These are the slowest tests in the suite (roughly 10s
total) because they boot a real pty each time.

- [ ] **Step 5: Run the whole wrapper suite**

Run: `uv run pytest tests/wrapper -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
uv run ruff format src/mtg_agent tests && uv run ruff check src/mtg_agent tests
git add tests/testdata/stubclaude.py tests/wrapper/test_integration.py
git commit -m "test(wrapper): end-to-end turn loop against a stub claude, no API key needed"
```

---

Tasks 13-17 (the driver, the `ai_seat.py` mode, and the containers) continue in
`2026-08-31-continuous-mtg-agent-part-3.md`.
