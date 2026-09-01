"""The turn that hung forever because claude printed an error and sat there.

Reproduced from a live freeze in the docker-compose stack. A human was
playing the UI seat against the wrapper-mode AI seat; the agent container's
`ANTHROPIC_API_KEY` was rejected, so claude printed

    Please run /login · API Error: 401 API key is invalid.

into its TUI three minutes into turn zero and stopped. It did not exit, so
`Manager._watch` never fired; no Stop hook ran, so `Manager.complete` never
fired. Half an hour later `GET /v1/session` still read
`state: busy, currentTurnId: turn-..., stallSuspectedSince: null`, because
the only timer on a turn is `mark_stall_suspected` - which by its own
docstring reports and re-arms rather than failing anything. The driver's
callback was never called, so its `/status` still said
`pending: false, lastError: null`, and the human's browser showed
"AI (claude): dispatched" with an opponent that would never move again.

The fix is `Manager.fail_on_fatal_output`: the PTY reader scans for the
errors claude prints and then idles on, and fails the in-flight turn with the
reason, which is what puts the reason on the driver's callback and eventually
in front of the human.
"""

from __future__ import annotations

import time

import pytest
from test_session import FakeProc, fast_settings, make

from mtg_agent.wrapper.session import FATAL_NEEDLES, State
from mtg_agent.wrapper.turns import TurnState


@pytest.fixture
def ready(monkeypatch):
    """A booted manager over a FakeProc whose output the test writes."""
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.05)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 0.1)
    proc = FakeProc(b"welcome")
    done: list = []
    # A long turn timeout, so nothing here can pass by accident on the stall
    # timer: only the fatal scan can end these turns.
    manager = make(proc, fast_settings(TURN_TIMEOUT_SECONDS="600"), on_turn_done=done.append)
    manager.start()
    assert manager.state is State.READY
    try:
        yield manager, proc, done
    finally:
        manager.shutdown()


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_401_fails_the_turn_instead_of_hanging_it(ready):
    manager, proc, done = ready
    turn_id = manager.submit("your turn")
    assert manager.state is State.BUSY

    proc.feed(b"\x1b[2K Please run /login \xc2\xb7 API Error: 401 API key is invalid.\r\n")

    assert wait_for(lambda: manager.current_turn_id is None), (
        "the turn was never failed - this is the live freeze: busy forever"
    )
    assert manager.state is State.READY
    assert len(done) == 1
    record = done[0]
    assert record.id == turn_id
    assert record.state is TurnState.FAILED
    assert "401" in record.error


def test_the_failure_reason_names_what_claude_printed(ready):
    manager, proc, done = ready
    manager.submit("your turn")
    proc.feed(b"Credit balance is too low\r\n")
    assert wait_for(lambda: bool(done))
    assert "Credit balance is too low" in done[0].error


def test_the_session_snapshot_reports_the_fatal_error(ready):
    manager, proc, done = ready
    manager.submit("your turn")
    proc.feed(b"API Error: 401 API key is invalid.\r\n")
    assert wait_for(lambda: bool(done))
    snapshot = manager.snapshot()
    assert snapshot["state"] == "ready"
    assert snapshot["currentTurnId"] is None
    assert snapshot["lastFatal"] == "API Error: 401"


def test_a_needle_split_across_two_pty_reads_is_still_caught(ready):
    """The scan reads the ring, not one chunk: 4 KiB boundaries fall wherever
    the kernel puts them, and an error message straddling one must not be the
    difference between a reported failure and a 30-minute hang."""
    manager, proc, done = ready
    manager.submit("your turn")
    proc.feed(b"API Err")
    time.sleep(0.1)
    assert not done, "half a needle must not fail a turn"
    proc.feed(b"or: 401 invalid\r\n")
    assert wait_for(lambda: bool(done))
    assert done[0].state is TurnState.FAILED


def test_ordinary_output_never_fails_a_turn(ready):
    manager, proc, done = ready
    manager.submit("your turn")
    proc.feed(b"I will cast Lightning Bolt targeting the opponent.\r\n")
    time.sleep(0.2)
    assert done == []
    assert manager.state is State.BUSY
    assert manager.current_turn_id is not None


def test_output_before_any_turn_is_not_attributed_to_one(ready):
    manager, proc, done = ready
    proc.feed(b"API Error: 401\r\n")
    time.sleep(0.2)
    assert done == [], "there was no turn in flight to fail"


def test_the_needles_are_error_text_not_words_a_game_produces():
    """A false positive here ends a legitimate turn, so every needle has to
    be something only a broken session prints - never a word that could turn
    up in Magic prose the agent writes into its own terminal."""
    for needle in FATAL_NEEDLES:
        assert len(needle) >= 12, needle
        assert any(mark in needle for mark in ("API Error", "/login", "balance", "token")), needle
