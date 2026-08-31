"""What the agent container prints about a turn that went wrong.

A turn killed by claude dying was recorded on the turn and delivered over the
callback, but the agent container itself said nothing about it - so someone
reading `docker compose logs` saw the session die and never learned which turn
went down with it.
"""

from __future__ import annotations

import logging
import time

from mtg_agent.wrapper.turns import TurnState
from tests.wrapper.test_session import FakeProc, ready_manager


def test_a_turn_that_dies_with_claude_says_so_in_the_log(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="wrapper.session")
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    try:
        turn_id = manager.submit("play a land")
        proc.die()
        deadline = time.monotonic() + 5
        while manager.store.get(turn_id).state is TurnState.RUNNING:
            assert time.monotonic() < deadline, "the turn never failed"
            time.sleep(0.01)
    finally:
        manager.shutdown()

    failures = [
        r for r in caplog.records if r.levelno >= logging.ERROR and "turn failed" in r.getMessage()
    ]
    assert failures, caplog.text
    assert turn_id in failures[0].getMessage()


def test_a_completed_turn_reports_its_stop_reason_and_size(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="wrapper.session")
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    try:
        turn_id = manager.submit("play a land")
        manager.complete({"finalText": "attacked with the Frog", "stopReason": "end_turn"})
    finally:
        manager.shutdown()

    (line,) = [r.getMessage() for r in caplog.records if "turn complete" in r.getMessage()]
    assert turn_id in line
    assert "stop_reason=end_turn" in line
    assert "final_text_chars=22" in line
