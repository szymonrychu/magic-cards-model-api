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

import threading
import time
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_decisions import seats_for, set_short_timeout, stuff_hand

from magic_cards_model_api import seatkit
from magic_cards_model_api.main import app


def test_a_seat_not_holding_the_lock_still_wakes_promptly_for_its_own_decision(
    client, game, monkeypatch
) -> None:
    """Regression guard for the *first* fix, not the second: when it is a
    genuinely different in-flight request (bob's) that raises alice's own
    decision, her lock-free poll already notices promptly. Kept here so a
    future change to `block_until_wake`/`available_actions` cannot silently
    regress this half while "fixing" the self case below.
    """
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard: a real choice
    uids = seats_for(client, game)
    set_short_timeout(game, 5.0)  # long enough to see a slow wake, short to fail fast

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

    alice_seat = seatkit.Seat("alice", uids["alice"], "http://testserver", game)
    woken: list[dict[str, Any]] = []

    def poll() -> None:
        woken.append(seatkit.block_until_wake(alice_seat, before=None, max_seconds=20.0))

    poll_thread = threading.Thread(target=poll)
    poll_thread.start()
    time.sleep(0.1)  # let alice's poll loop actually start before bob's pass

    def bobs_pass() -> None:
        TestClient(app).post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})

    bob_thread = threading.Thread(target=bobs_pass)
    started = time.monotonic()
    bob_thread.start()

    poll_thread.join(timeout=10)
    elapsed = time.monotonic() - started

    assert not poll_thread.is_alive(), "alice's own poll never woke for her decision"
    assert woken and "decision is waiting" in woken[0]["wake_reason"]
    assert elapsed < 2.0, f"alice took {elapsed:.1f}s to notice her own pending decision"

    bob_thread.join(timeout=10)
    assert not bob_thread.is_alive()


def test_a_seats_own_call_that_raises_its_own_decision_no_longer_stalls(
    client, game, monkeypatch
) -> None:
    """The shape that was still broken: alice's own `pass_priority` is what
    closes the priority round and walks the turn into cleanup, raising
    *her own* discard decision from inside her own in-flight call.

    Before the fix, `run_action` (via `mcp.py`'s `pass_priority` tool) issued
    that call directly and blocked on its HTTP response - which would not
    arrive until the decision was answered, and nothing in this same
    synchronous caller could ever send that answer, so it always rode the
    decision out to the server's own timeout and then lost the race
    answering it (the exact traced symptom). After the fix
    (`seatkit._call_watching_for_self_decision`), `seat.pass_priority()`
    itself returns almost immediately with `in_flight_action: True` and the
    decision's prompt, well before the timeout, so a caller can answer it
    with a genuinely separate `decide()` call - proven here by actually
    sending one from a second thread once that wake fires.
    """
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    stuff_hand(client, game, "alice", 8)
    uids = seats_for(client, game)
    set_short_timeout(game, 3.0)  # would fail fast if this regresses

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

    started = time.monotonic()
    outcome, pending = seatkit._call_watching_for_self_decision(alice_seat, perform)
    elapsed = time.monotonic() - started

    assert pending is not None, "alice's own pass should have surfaced her own decision"
    assert outcome == seatkit.SELF_DECISION_IN_FLIGHT
    assert pending["kind"] == "choose_cards"
    assert elapsed < 1.0, (
        f"took {elapsed:.1f}s to surface alice's own decision - it rode the decision "
        "timeout out instead of noticing its own poll, the deadlock is back"
    )

    # Prove it is a live game, not a silent default: answer it for real, well
    # within the (short, test-only) timeout, from a genuinely separate call.
    card_id = pending["cards"][0]
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [card_id]},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True

    # The abandoned background call (alice's own original pass-priority)
    # finishes on its own now that it has an answer - nothing left watching
    # it here, but the server-side effect is real: cleanup completed.
    for _ in range(100):
        board = client.get(f"/api/games/{game}", params={"uid": uids["alice"]}).json()
        if board["step"] == "cleanup" and len(board["hands"]["alice"]) == 7:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("alice's own abandoned pass-priority call never completed cleanup")
