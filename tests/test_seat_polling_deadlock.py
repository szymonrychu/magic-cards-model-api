"""The second deadlock: a decision addressed to the very seat whose own
in-flight request raised it.

`test_deadlock.py` fixed a decision addressed to a *different* seat than the
one parked in `Game.ask` (`available_actions` no longer contends for the
lock that parked seat holds). This file is the other half: when the decision
recipient IS the seat whose own call raised it - the cleanup discard closing
its own turn, an ETB trigger it must target its own permanent's ability
for - `seatkit.block_until_wake`'s lock-free polling cannot save it, because
there is no separate in-flight call of that seat's own left to notice
anything: a real MCP client sends one tool call at a time (`mcp.py`'s module
docstring) and that one call *is* the request parked in `Game.ask`. Before
the fix in `seatkit._call_watching_for_self_decision`, this seat's decision
could only ever be answered by racing its own decide() against the server's
own `decision_timeout_seconds` default - exactly the traced symptom: a
`decision_timeout` log line, then the seat's own `decide` arriving just
after and refused, every time.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_decisions import advance_into_cleanup, seats_for, stuff_hand, wait_for_pending

from magic_cards_model_api import seatkit
from magic_cards_model_api.main import app


def test_a_different_seats_request_that_raises_this_seats_decision_is_seen_at_once(
    client, game, monkeypatch
) -> None:
    """Regression guard for the *first* fix, not the second: when it is a
    genuinely different request (bob's) that raises alice's own decision, her
    own check sees it at once - `block_until_wake` is a plain read and never
    takes `store.locked`, so it cannot be queued behind whatever lock bob's
    own mutating request holds while it runs. T11 made this simpler still:
    there is no longer an internal poll loop whose promptness needs proving,
    only one read, made after bob's request has actually landed - the same
    thing a real caller does. Kept here so a future change to
    `block_until_wake`/`available_actions` cannot silently regress this half
    while "fixing" the self case below.
    """
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard: a real choice
    uids = seats_for(client, game)

    put = client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    assert put.status_code == 200, put.text
    assert put.json()["priority_holder"] == "alice", put.json()

    # Alice passes first: only she has passed so far, so this returns at
    # once with priority now at bob - it does not touch cleanup.
    first_pass = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    assert first_pass.status_code == 200, first_pass.text
    assert first_pass.json()["outcome"] != "end_step", (
        "alice's own pass must not be the one that ends the step - bob's must be, "
        "for this to reproduce the 'different requester' shape"
    )

    # Bob's own pass - a genuinely different requester's mutation - is what
    # closes the round and crosses into alice's own cleanup discard.
    bobs_pass = TestClient(app).post(
        f"/api/games/{game}/actions/pass-priority", json={"player": "bob"}
    )
    assert bobs_pass.status_code == 200, bobs_pass.text

    alice_seat = seatkit.Seat("alice", uids["alice"], "http://testserver", game)
    started = time.monotonic()
    woken = seatkit.block_until_wake(alice_seat, before=None)
    elapsed = time.monotonic() - started

    assert "decision is waiting" in woken["wake_reason"]
    assert elapsed < 2.0, f"alice's own check took {elapsed:.1f}s - it must never block"


def test_a_seats_own_call_that_raises_its_own_decision_no_longer_stalls(
    client, game, monkeypatch
) -> None:
    """The shape that was still broken: alice's own `pass_priority` is what
    closes the priority round and walks the turn into cleanup, raising
    *her own* discard decision from inside her own in-flight call.

    Before T7b's pause-and-resume rework, `run_action` (via `mcp.py`'s
    `pass_priority` tool) issued that call directly and blocked on its HTTP
    response - which would not arrive until the decision was answered, and
    nothing in this same synchronous caller could ever send that answer, so
    it always rode the decision out to the server's own timeout and then
    lost the race answering it (the exact traced symptom).

    After the rework, `perform()` itself (`commands.py`'s pause-and-resume)
    usually returns almost instantly with `paused: True` and the decision
    already attached, rather than blocking - so `seatkit._paused` is now the
    primary way `run_action` notices its own action raised its own decision.
    Confirmed empirically here (see the comment at the call site below):
    `_call_watching_for_self_decision`'s background thread finishes in low
    single-digit milliseconds, well under `POLL_SECONDS` (0.2s), so its
    polling loop never gets a chance to fire even once and it returns the
    paused outcome directly rather than `SELF_DECISION_IN_FLIGHT`. The
    background-thread race is still there as a defensive fallback for the
    rarer case where a decision surfaces some other way mid-flight, but it
    is not what fires in this scenario any more, so this asserts the
    contract that actually matters to a caller - `run_action` itself
    reporting `in_flight_action: True` with the right prompt - rather than
    which of the two internal paths happened to notice it.
    """
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    stuff_hand(client, game, "alice", 8)
    uids = seats_for(client, game)

    put = client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    assert put.status_code == 200, put.text
    assert put.json()["priority_holder"] == "alice"

    # Board setup, not a rules move: hand priority to bob first (nobody has
    # passed yet), so bob's pass is legal and moves priority to alice - then
    # ALICE's own pass is the one that closes the round and walks into
    # cleanup, from inside her own request.
    from magic_cards_model_api.store import store

    real_game = store.get(game)
    assert real_game.priority is not None
    real_game.priority.holder_index = real_game.priority.players.index("bob")
    real_game.priority.passes = 0
    bobs_first_pass = client.post(
        f"/api/games/{game}/actions/pass-priority", json={"player": "bob"}
    )
    assert bobs_first_pass.status_code == 200, bobs_first_pass.text
    assert bobs_first_pass.json()["outcome"] != "end_step"

    alice_seat = seatkit.Seat("alice", uids["alice"], "http://testserver", game)

    def perform() -> Any:
        return alice_seat.call_json(
            "POST",
            f"/api/games/{game}/actions/pass-priority",
            {"player": "alice"},
        )

    # Empirically (run directly against the primitive with a debug print
    # before this rewrite): `perform()`'s own pause beats the poll loop by
    # roughly two orders of magnitude (~1-2ms elapsed against a 0.2s
    # `POLL_SECONDS`), so `_call_watching_for_self_decision` returns
    # `(paused_outcome, None)` here, not `(SELF_DECISION_IN_FLIGHT,
    # pending)`. `run_action` (below) is written to treat both shapes
    # identically via `_paused`, which is the behaviour this test actually
    # guards.
    started = time.monotonic()
    result = seatkit.run_action(alice_seat, perform)
    elapsed = time.monotonic() - started

    assert result["in_flight_action"] is True, (
        "alice's own pass should have surfaced her own decision"
    )
    assert result["outcome"] is None, "the action's own outcome is unknown until decide() answers"
    pending = result["state"]["pending_decision"]
    assert pending is not None
    assert pending["kind"] == "choose_cards"
    assert pending["player"] == "alice"
    assert "discard" in result["wake_reason"]
    # The deadlock rode the call out to `seatkit.CLIENT_TIMEOUT_SECONDS` (30s),
    # so 10s still separates the two by a wide margin on a loaded CI runner
    # where 1.0s was crossed (1.1s).
    assert elapsed < 10.0, (
        f"took {elapsed:.1f}s to surface alice's own decision - it rode a "
        "timeout out instead of noticing the pause, the deadlock is back"
    )

    # Prove it is a live game, not a silent default: answer it for real, from
    # a genuinely separate call.
    card_id = pending["cards"][0]
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [card_id]},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True

    # `decide` is an ordinary synchronous mutating request now
    # (`commands.py`'s pause-and-resume): it resumes and completes alice's
    # original pass-priority within its own call, so the effect is already
    # settled by the time it returns - CR 514.1's discard happened, so alice
    # is down to seven. The step it lands in is no longer `cleanup`: that
    # pass settles on past every step nobody receives priority in (CR 500.3,
    # `engine_api.settle_priorityless_steps`), so what the board shows is
    # the next turn's upkeep, with a priority holder.
    board = client.get(f"/api/games/{game}", params={"uid": uids["alice"]}).json()
    assert board["step"] == "upkeep", board["step"]
    assert len(board["hands"]["alice"]) == 7
    assert board["priority_holder"] is not None


def test_available_actions_returns_this_early_shape_for_own_pending_decision(
    client, game, monkeypatch
) -> None:
    """The other half of the original `test_deadlock.py::
    test_available_actions_does_not_deadlock_on_own_pending_decision`, which
    this file's own docstring already explains T7b made half moot: the
    *deadlock* it proved is gone by construction now, because pausing means
    the request that raised the decision has already returned - and
    released `store.locked(game_id)` - by the time any caller can ever see
    this seat's own `pending_decision` at all. There is no longer a
    background thread to hold a lock this call could contend for, so there
    is nothing left to time.

    The *shape* half is not moot, and had no replacement: since T11,
    `available_actions` is one call into `GET .../legal-actions`, which
    itself asks the engine's `legal_actions(game, player, pending=...)` -
    while a decision is outstanding, that enumeration answers *only* its own
    legal answers (`answer_decision`-kind actions) for the player it
    addresses, nothing else (`legal_actions`'s own module docstring).
    `attackers_requested`/`blockers_requested` still read straight off
    `pending["kind"]` (the one case where the decision itself names the
    single legal thing), and `pending_decision` is still echoed back. This
    pins exactly that dict, synchronously, with no thread needed."""
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard
    uids = seats_for(client, game)

    paused = advance_into_cleanup(game)
    assert paused is not None, "the discard should have asked, not defaulted silently"
    pending_view = wait_for_pending(client, game, uids["alice"])
    pending = pending_view["pending_decision"]
    assert pending["kind"] == "choose_cards"

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seat = seatkit.Seat("alice", uids["alice"], "http://testserver", game)
    actions = seatkit.available_actions(seat, pending_view)

    assert actions["attackers_requested"] is False
    assert actions["blockers_requested"] is False
    assert actions["pending_decision"] == pending
    assert actions["actions"], "the pending decision's own answers should still be offered"
    assert all(entry["kind"] == "answer_decision" for entry in actions["actions"])
