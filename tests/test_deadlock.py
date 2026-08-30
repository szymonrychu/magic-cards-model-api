"""Regression for the traced harness deadlock.

The trace (an opus adjudicator's, verified against a live game log with 18
decision timeouts): one player's mutating request resolves something inside
`store.locked(game_id)`, which raises a decision and parks - still holding
that lock - inside `Game.ask` waiting for an answer. The seat the decision is
addressed to wakes correctly (`seatkit.block_until_wake`, which never takes
the lock), but `run_action` then called `seatkit.available_actions`
unconditionally on every return path, and that function issues one
`POST .../actions/cast?dry_run=true` per hand card (and `.../activate` per
ability) - each of which takes `store.locked(game_id)` (`app.py`). That lock
is held by the very request that raised the decision, so the seat's own call
queued behind the decision it existed to let it answer, for the full decision
timeout - a deadlock, not a wait.

This file reproduces the mechanical shape with the same background-thread
technique `test_decisions.py` already uses for the cleanup discard: a
still-running thread genuinely holding `store.locked(game_id)`, parked in
`Game.ask`, while this test's own thread calls the seat-facing functions that
used to contend for that same lock. Which *player* the background request
belongs to is not what makes this a deadlock - contention is on the lock, not
on identity - so the cleanup discard (asked of the active player, whose own
`advance` call is what is parked) reproduces it exactly: the traced case
merely had the parked request and the decision recipient be different
players, which is not a necessary ingredient of the bug.
"""

from __future__ import annotations

import time

import httpx
from fastapi.testclient import TestClient
from test_decisions import (
    advance_into_cleanup,
    seats_for,
    set_short_timeout,
    stuff_hand,
    wait_for_pending,
)

from magic_cards_model_api import seatkit
from magic_cards_model_api.main import app


def test_available_actions_does_not_deadlock_on_own_pending_decision(
    client, game, monkeypatch
) -> None:
    """Before the fix: this call would block for roughly the decision's full
    timeout (each of the 8 dry-run `cast` probes queuing behind the parked
    thread's lock), then finally succeed once that thread's own decision
    timed out and released it. After the fix: `available_actions` recognises
    the pending decision is this seat's own and returns immediately without
    touching the lock at all.
    """
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    stuff_hand(client, game, "alice", 8)
    uids = seats_for(client, game)
    set_short_timeout(game, 2.0)  # keeps a would-be deadlock's failure fast

    thread = advance_into_cleanup(game)
    assert thread is not None, "the discard should have asked, not defaulted silently"
    try:
        pending_view = wait_for_pending(client, game, uids["alice"])
        pending = pending_view["pending_decision"]
        assert pending["kind"] == "choose_cards"

        seat = seatkit.Seat("alice", uids["alice"], "http://testserver", game)

        started = time.monotonic()
        actions = seatkit.available_actions(seat, pending_view)
        elapsed = time.monotonic() - started

        # The old code would have contended for a lock held by `thread` for
        # up to its own decision timeout (2s, doubled to ~4s by the
        # delivery-then-answer wait `decisions.DecisionMailbox.wait_for_answer`
        # now does) before ever returning. The fix never touches that lock.
        assert elapsed < 1.0, (
            "available_actions blocked on the game lock instead of skipping "
            "the dry runs for a seat with its own pending decision - the "
            "deadlock is back"
        )
        assert actions == {
            "castable": [],
            "activatable": [],
            "attackers_requested": False,
            "blockers_requested": False,
            "pending_decision": pending,
        }

        # The seat can still answer it well within the timeout - proving this
        # is a live game, not one silently defaulted around the deadlock.
        answered = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "card_ids": [pending["cards"][0]]},
        )
        assert answered.status_code == 200
        assert answered.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive(), "the advancing request never returned"


def test_decide_never_races_the_resolving_thread_for_the_audit_snapshot(client, game) -> None:
    """`app.py`'s `decide` now audits the accepted answer *before* calling
    `mailbox.answer_if`, specifically so that read of the `Game` object can
    never race the resolving thread `answer_if` is about to wake back into
    mutating that same object. This does not (and cannot, without adding
    artificial delay) prove the absence of a race directly, but it does prove
    the endpoint still behaves correctly and quickly under the exact
    concurrent conditions where such a race would live - the resolving thread
    genuinely parked, then genuinely woken by this call.
    """
    stuff_hand(client, game, "alice", 8)
    uids = seats_for(client, game)

    thread = advance_into_cleanup(game)
    assert thread is not None
    try:
        pending_view = wait_for_pending(client, game, uids["alice"])
        card_id = pending_view["pending_decision"]["cards"][0]

        answered = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "card_ids": [card_id]},
        )
        assert answered.status_code == 200
        assert answered.json()["accepted"] is True
        assert answered.json()["log_seq"] is not None
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

    log = client.get(f"/api/games/{game}/log", params={"uid": uids["alice"]}).json()["log"]
    decides = [entry for entry in log if entry["action"] == "decide"]
    assert decides, "answering a decision must leave an audit entry"
    entry = decides[-1]
    assert entry["detail"]["player"] == "alice"
    assert entry["detail"]["allowed"] is True
    assert entry["detail"]["kind"] == "choose_cards"
    assert entry["detail"]["card_ids"] == [card_id]


def test_dry_run_cast_is_not_audited(client, game) -> None:
    """A `dry_run` cast probe - what `seatkit.available_actions` issues per
    hand card - must never appear in the game log: it changes nothing, and
    auditing it anyway is how one traced game's log grew to 2410 entries with
    956 of them refusal noise from exactly this kind of probe."""
    board = stuff_hand_returns(client, game)
    mountain_id = board["hands"]["alice"][0]["id"]

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": mountain_id, "dry_run": True},
    )
    assert response.status_code == 200
    assert response.json()["log_seq"] is None

    log = client.get(f"/api/games/{game}/log").json()["log"]
    casts = [entry for entry in log if entry["action"] == "cast"]
    assert casts == [], "a dry-run cast must never be written to the audit log"


def stuff_hand_returns(client: TestClient, game_id: str) -> dict:
    from conftest import add_card, state

    add_card(client, game_id, "Mountain", "alice", "hand")
    return state(client, game_id)
