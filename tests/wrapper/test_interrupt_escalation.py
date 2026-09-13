"""An interrupted turn always reaches turn-done.

Claude Code runs no Stop hook for an interrupted turn ("`Stop` hooks fire
whenever Claude finishes responding ... They don't fire on user interrupts",
code.claude.com/docs/en/hooks-guide), and on a live session the Stop hook's
`/internal/turn-complete` is the only thing that reaches `Manager.complete`,
the one call that frees the turn slot. So the ESC `Manager.interrupt` wrote
into a turn blocked in a long MCP call cancelled that call (live, game
995daaf7: `wait_for_priority` rejected 0.3 s after the ESC) and left
`_current` set for good: every later `submit` was a `BusyError`, the driver's
handover push got `409 Conflict` 5523 times, and no handover turn ran.
"""

from __future__ import annotations

import logging
import time

from mtg_agent.wrapper.session import State
from mtg_agent.wrapper.turns import TurnState
from tests.wrapper.test_session import FakeProc, fast_settings, ready_manager

ESC = b"\x1b"


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def settled(**overrides):
    return fast_settings(INTERRUPT_SETTLE_SECONDS="0.3", TURN_TIMEOUT_SECONDS="60", **overrides)


def test_an_interrupted_turn_with_no_stop_hook_reaches_turn_done(monkeypatch):
    proc = FakeProc(b"ready")
    delivered: list = []
    manager = ready_manager(monkeypatch, proc, settled())
    manager.on_turn_done = delivered.append
    try:
        turn_id = manager.submit("play the game")
        manager.interrupt()
        # The fake claude, like the real one, cancels and goes quiet: no Stop
        # hook ever posts a result for this turn.
        wait_for(lambda: delivered)
        record = manager.store.get(turn_id)
        assert record.state is TurnState.FAILED
        assert "interrupted" in record.error
        assert [r.id for r in delivered] == [turn_id]
        assert manager.state is State.READY
        assert manager.current_turn_id is None
        assert manager.submit("hand over now")
    finally:
        manager.shutdown()


def test_escalation_waits_the_settle_time_then_interrupts_again_and_logs_one_line(
    monkeypatch, caplog
):
    caplog.set_level(logging.WARNING, logger="wrapper.session")
    proc = FakeProc(b"ready")
    delivered: list = []
    manager = ready_manager(monkeypatch, proc, settled())
    manager.on_turn_done = delivered.append
    try:
        turn_id = manager.submit("play the game")
        proc.writes.clear()
        manager.interrupt()
        time.sleep(0.1)
        assert proc.writes == [ESC]
        assert manager.state is State.BUSY
        wait_for(lambda: delivered)
        assert proc.writes == [ESC, ESC]
        lines = [r.getMessage() for r in caplog.records if "escalat" in r.getMessage()]
        assert len(lines) == 1
        assert turn_id in lines[0]
    finally:
        manager.shutdown()


def test_a_turn_that_ends_on_its_own_after_the_interrupt_is_not_escalated(monkeypatch):
    proc = FakeProc(b"ready")
    delivered: list = []
    manager = ready_manager(monkeypatch, proc, settled())
    manager.on_turn_done = delivered.append
    try:
        turn_id = manager.submit("play the game")
        proc.writes.clear()
        manager.interrupt()
        manager.complete({"finalText": "stopped", "stopReason": "end_turn"})
        time.sleep(0.5)
        assert manager.store.get(turn_id).state is TurnState.COMPLETE
        assert proc.writes == [ESC]
        assert [r.id for r in delivered] == [turn_id]
    finally:
        manager.shutdown()


def test_an_interrupt_with_no_turn_in_flight_escalates_nothing(monkeypatch):
    proc = FakeProc(b"ready")
    delivered: list = []
    manager = ready_manager(monkeypatch, proc, settled())
    manager.on_turn_done = delivered.append
    try:
        manager.interrupt()
        time.sleep(0.5)
        assert proc.writes == [ESC]
        assert delivered == []
        assert manager.state is State.READY
    finally:
        manager.shutdown()


def test_the_settle_time_defaults_to_twenty_seconds():
    assert fast_settings().interrupt_settle == 20.0
