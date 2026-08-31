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
