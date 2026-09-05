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


# ---- restart: the per-game model/effort pass-through relaunches claude ----


def test_restart_relaunches_claude_with_the_new_settings(monkeypatch):
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.01)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 0.05)
    spawned: list[tuple[Settings, FakeProc]] = []

    def spawn(settings: Settings, resume: bool) -> FakeProc:
        proc = FakeProc(b"ready")
        spawned.append((settings, proc))
        return proc

    store = Store()
    manager = Manager(fast_settings(MODEL="sonnet"), store, Metrics(), spawn=spawn)
    manager.start()
    try:
        assert manager.state is State.READY
        assert len(spawned) == 1
        first_proc = spawned[0][1]

        manager.restart(fast_settings(MODEL="opus", EFFORT="max"))

        assert manager.state is State.READY
        assert len(spawned) == 2, "a fresh claude process must be spawned"
        assert first_proc._closed, "the old pty is closed before the new one boots"
        assert manager.settings.model == "opus" and manager.settings.effort == "max"
        assert manager.snapshot()["model"] == "opus"
    finally:
        manager.shutdown()


def test_restart_refuses_with_busy_error_while_a_turn_is_in_flight(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    try:
        manager.submit("a whole match's worth of turns")
        with pytest.raises(BusyError):
            manager.restart(fast_settings(MODEL="opus"))
        # Refused, so the old settings and the old process are untouched.
        assert manager.settings.model != "opus"
        assert not proc._closed
    finally:
        manager.shutdown()


def test_restart_after_death_raises_dead(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    proc.die()
    deadline = time.monotonic() + 5
    while manager.state is not State.DEAD and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.state is State.DEAD
    with pytest.raises(DeadError):
        manager.restart(fast_settings(MODEL="opus"))


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


def test_a_dead_session_asks_the_process_to_exit(monkeypatch):
    """A session that has died can never serve another turn. Staying up would
    leave a container that is 'running' and useless; exiting hands the next
    match a fresh one."""
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.01)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 0.05)
    exits: list[str] = []
    proc = FakeProc(b"ready")
    manager = make(proc, on_dead=lambda: exits.append("exit"))
    manager.start()
    proc.die()
    deadline = time.monotonic() + 5
    while not exits and time.monotonic() < deadline:
        time.sleep(0.01)
    assert exits == ["exit"]
    assert manager.state is State.DEAD


def test_a_deliberate_shutdown_does_not_fire_the_exit_hook_twice(monkeypatch):
    """`DELETE /v1/session` already exits by its own route. The watcher must
    not treat that closure as a crash and ask a second time."""
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.01)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 0.05)
    exits: list[str] = []
    proc = FakeProc(b"ready")
    manager = make(proc, on_dead=lambda: exits.append("exit"))
    manager.start()
    manager.shutdown()
    time.sleep(0.2)
    assert exits == []


def test_shutdown_is_deliberate_and_does_not_mark_dead_by_crash(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    manager.shutdown()
    assert proc._closed
    assert manager.metrics.claude_restarts_total._value.get() == 0
