"""The live decision provider: a real seat, outside the engine, answering a
CR 608.2d question that a mutating request is blocked on.

The engine side (`magic_cards_model.machines.decisions`, `Game.ask`) tries a
scripted answer, then `Game.decisions.provider`, then its own default. This
API attaches a `GameDecisionProvider` (`magic_cards_model_api/decisions.py`)
to every game at creation, so the second branch is live here: a request that
crosses a CR 608.2d question - the cleanup discard, in this slice - blocks
inside `Game.ask` until a seat answers through `POST .../decide`, or the
provider's timeout elapses and the engine's own default is taken instead.

The one mechanic wired up this slice is the cleanup discard (CR 514.1,
701.9b): the active player chooses which card(s) to discard down to maximum
hand size, instead of the engine silently taking the back of the hand.

Getting there needs `POST .../advance`, one step at a time, not
`PUT .../step`: `Game.advance_to` (what the step-jump endpoint calls) crosses
step boundaries without running any turn-based action, cleanup's discard
included, while `Game.advance` (what `/advance` calls, one step per call)
runs the turn-based actions of whatever step it just entered - which is
exactly where this decision is asked. The call that enters cleanup is
therefore the one that blocks, so it is always run on a background thread.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from conftest import add_card, state
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def set_short_timeout(game_id: str, seconds: float) -> None:
    """Shrink this game's live provider's wait, so a timeout test does not
    take the production default of 60 seconds. White-box: reaches into the
    real provider object the store already built, rather than faking one."""
    provider = store.get(game_id).decisions.provider
    assert provider is not None
    provider.timeout_seconds = seconds


def stuff_hand(client: TestClient, game_id: str, player: str, count: int) -> None:
    for _ in range(count):
        add_card(client, game_id, "Mountain", player, "hand")


def advance_into_cleanup(game_id: str, player: str = "alice") -> threading.Thread | None:
    """Advance one step at a time until the call that enters cleanup, which
    blocks on the discard decision. Returns that still-running thread, or
    `None` if cleanup was entered without ever blocking (nothing to discard).
    """
    for _ in range(20):
        results: list[Any] = []

        def call(results: list[Any] = results) -> None:
            stepping_client = TestClient(app)
            results.append(
                stepping_client.post(f"/api/games/{game_id}/advance", json={"player": player})
            )

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=0.5)
        if thread.is_alive():
            return thread  # blocked entering a step that asked something
        response = results[0]
        assert response.status_code == 200, response.text
        if response.json()["advanced_to"] == "cleanup":
            return None
    raise AssertionError("never reached cleanup")


def wait_for_pending(client: TestClient, game_id: str, uid: str, tries: int = 300) -> dict:
    """Poll until the state view shows a pending decision for `uid`."""
    for _ in range(tries):
        view = state(client, game_id, uid=uid)
        if view.get("pending_decision") is not None:
            return view
        time.sleep(0.01)
    raise AssertionError("no pending decision showed up in time")


def test_a_seat_sees_and_answers_its_pending_decision(client, game):
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    mountains = [card["id"] for card in board["hands"]["alice"]]
    to_discard = mountains[3]  # an arbitrary one, to prove it is a real choice

    thread = advance_into_cleanup(game)
    assert thread is not None, "the discard should have asked, not defaulted silently"
    try:
        pending_view = wait_for_pending(client, game, uids["alice"])
        pending = pending_view["pending_decision"]
        assert pending["player"] == "alice"
        assert pending["kind"] == "choose_cards"
        assert pending["amount"] == 1
        assert to_discard in pending["cards"]

        answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "card_ids": [to_discard]},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive(), "the advancing request never returned"

    final = state(client, game, uid=uids["alice"])
    assert final["step"] == "cleanup"
    assert len(final["hands"]["alice"]) == 7
    discarded_ids = [card["id"] for card in final["graveyards"]["alice"]]
    assert discarded_ids == [to_discard]
    assert final.get("pending_decision") is None, "answered decisions do not linger"


def test_an_engine_refused_answer_is_refused_synchronously_not_recorded_allowed(client, game):
    """The defect this regresses: a `choose_cards` decision that needs
    exactly 1 card (CR 514.1's cleanup discard, `decision.exact`), answered
    with an empty `card_ids: []`.

    `Game.ask` (`magic_cards_model/models/game.py`) checks that count and
    raises "needs 1 card(s) chosen, 0 given" - but only on the resolving
    thread, which is not this request. Before `decisions.validate_answer`
    existed, `decide` never looked at the count, so it posted the empty
    answer to the mailbox, logged `allowed: true`, and returned `accepted:
    true` to the seat - the engine's own refusal only surfaced later,
    misattributed to whatever unrelated request next drove the stack
    forward. This asserts the fix: the refusal reaches this very response,
    carries the engine's own count-mismatch wording, the audit log records
    it as a refusal rather than an acceptance, the decision is still
    pending afterwards, and the same seat can then answer correctly and the
    game proceeds.
    """
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    mountains = [card["id"] for card in board["hands"]["alice"]]
    to_discard = mountains[3]

    thread = advance_into_cleanup(game)
    assert thread is not None, "the discard should have asked, not defaulted silently"
    try:
        pending_view = wait_for_pending(client, game, uids["alice"])
        pending = pending_view["pending_decision"]
        assert pending["kind"] == "choose_cards"
        assert pending["amount"] == 1

        bad_answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "card_ids": []},
        )
        assert bad_answer.status_code == 200
        body = bad_answer.json()
        assert body["accepted"] is False, "an answer the engine refuses must not be accepted"
        assert "needs 1 card(s) chosen, 0 given" in body["reason"], body["reason"]

        # The audit log's `decide` entry for this answer must record the
        # refusal, never `allowed: true` standing in as if it were accepted.
        log = client.get(f"/api/games/{game}/log", params={"uid": uids["alice"]}).json()["log"]
        decide_entries = [entry for entry in log if entry["action"] == "decide"]
        assert decide_entries, "the refused answer must still leave an audit entry"
        last = decide_entries[-1]
        assert last["detail"]["allowed"] is False
        assert last["detail"]["reason"] is not None
        assert not any(
            entry["detail"].get("allowed") is True and entry["detail"].get("card_ids") == []
            for entry in decide_entries
        ), "no decide entry may claim the empty answer was allowed"

        # The decision must still be pending - the empty answer never
        # reached the mailbox at all.
        still_pending = state(client, game, uid=uids["alice"])["pending_decision"]
        assert still_pending is not None
        assert still_pending["kind"] == "choose_cards"
        assert to_discard in still_pending["cards"]

        # The same seat can now answer correctly and the game proceeds.
        good_answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "card_ids": [to_discard]},
        )
        assert good_answer.status_code == 200, good_answer.text
        assert good_answer.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive(), "the advancing request never returned"

    final = state(client, game, uid=uids["alice"])
    assert final["step"] == "cleanup"
    assert len(final["hands"]["alice"]) == 7
    discarded_ids = [card["id"] for card in final["graveyards"]["alice"]]
    assert discarded_ids == [to_discard]
    assert final.get("pending_decision") is None


def test_the_opponent_cannot_see_the_decisions_card_identities(client, game):
    """Security test: bob may see that alice is choosing something, and what
    kind of choice and why, but never which cards are in play for it - CR
    101.4a lets a hidden-zone choice "remain face down as it's chosen"."""
    stuff_hand(client, game, "alice", 8)
    uids = seats_for(client, game)

    thread = advance_into_cleanup(game)
    assert thread is not None
    try:
        wait_for_pending(client, game, uids["alice"])
        bobs_view = state(client, game, uid=uids["bob"])
        pending = bobs_view["pending_decision"]
        assert pending is not None, "bob should still see that a decision is pending"
        assert pending["player"] == "alice"
        assert pending["kind"] == "choose_cards"
        assert "prompt" in pending
        assert "cards" not in pending, "bob must never see the candidate card identities"
        assert "amount" not in pending, "the count is derived from the (hidden) candidates"

        alices_view = state(client, game, uid=uids["alice"])
        alices_pending = alices_view["pending_decision"]
        assert alices_pending["cards"], "alice, the deciding player, does see her own candidates"

        # Answer it so the background thread does not linger past the test.
        client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "card_ids": [alices_pending["cards"][0]]},
        )
    finally:
        thread.join(timeout=10)


def test_the_opponent_cannot_answer_the_decision(client, game):
    stuff_hand(client, game, "alice", 8)
    uids = seats_for(client, game)

    thread = advance_into_cleanup(game)
    assert thread is not None
    try:
        pending_view = wait_for_pending(client, game, uids["alice"])
        a_card_id = pending_view["pending_decision"]["cards"][0]

        refused = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["bob"]},
            json={"player": "bob", "card_ids": [a_card_id]},
        )
        assert refused.status_code == 200
        assert refused.json()["accepted"] is False

        # Claiming to be alice with bob's uid must not work either: the
        # server resolves the player from the uid, not the request body.
        impersonating = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["bob"]},
            json={"player": "alice", "card_ids": [a_card_id]},
        )
        assert impersonating.json()["accepted"] is False

        still_pending = state(client, game, uid=uids["alice"])
        assert still_pending.get("pending_decision") is not None, "bob's attempt changed nothing"

        # Let the real player answer so the thread can finish.
        client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "card_ids": [a_card_id]},
        )
    finally:
        thread.join(timeout=10)


def test_timeout_falls_back_to_the_default_and_logs_it(client, game):
    stuff_hand(client, game, "alice", 8)
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    hand_ids = [card["id"] for card in board["hands"]["alice"]]
    default_choice = hand_ids[-1]  # the back of the hand: the documented default

    set_short_timeout(game, 0.2)

    # The provider's own timeout (0.2s) is shorter than `advance_into_cleanup`'s
    # per-call poll (0.5s), so the request that enters cleanup returns *within*
    # that poll, already defaulted - `advance_into_cleanup` reports that as
    # "returned, no thread left running" rather than "still blocked".
    thread = advance_into_cleanup(game)
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive(), "the timeout should have released the request by now"

    final = state(client, game, uid=uids["alice"])
    discarded_ids = [card["id"] for card in final["graveyards"]["alice"]]
    assert discarded_ids == [default_choice]

    log = client.get(f"/api/games/{game}/log", params={"uid": uids["alice"]}).json()["log"]
    timeouts = [entry for entry in log if entry["action"] == "decision_timeout"]
    assert timeouts, "a timed-out decision must leave a log line, never be silent"
    entry = timeouts[-1]
    assert entry["detail"]["player"] == "alice"
    assert entry["detail"]["kind"] == "choose_cards"
    assert default_choice in entry["detail"]["defaulted_to"]
    assert "rule" in entry["detail"] and entry["detail"]["rule"]


def test_answering_the_decision_does_not_mutate_the_game() -> None:
    """The invariant `decisions.py` and `store.locked` depend on: the request
    that answers a pending decision must never touch the `Game`. Proven here
    by holding the game's own mutation lock for the whole test and answering
    the decision anyway - if `decide` tried to acquire that lock too, this
    test would deadlock and time out rather than pass.
    """
    client = TestClient(app)
    response = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    info = response.json()
    game_id = info["game_id"]
    uids = info["seat_uids"]  # already username -> uid
    try:
        stuff_hand(client, game_id, "alice", 8)

        thread = advance_into_cleanup(game_id)
        assert thread is not None
        try:
            pending_view = wait_for_pending(client, game_id, uids["alice"])
            card_id = pending_view["pending_decision"]["cards"][0]

            # The background thread above is itself the proof this test is
            # for: it reached `Game.ask` through `store.locked(game_id)`, so
            # it is holding this game's mutation lock for as long as it is
            # blocked waiting on the mailbox - which is right now. `decide`
            # answers in well under the provider's 60-second timeout anyway,
            # because it never asks for that lock: if it did, this call would
            # queue behind the very thread it exists to unblock, and only the
            # timeout - not this assertion - would ever end the wait.
            started = time.monotonic()
            answered = client.post(
                f"/api/games/{game_id}/decide",
                params={"uid": uids["alice"]},
                json={"player": "alice", "card_ids": [card_id]},
            )
            assert time.monotonic() - started < 5, "decide waited as if it needed the game lock"
            assert answered.status_code == 200
            assert answered.json()["accepted"] is True
        finally:
            thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        client.delete(f"/api/games/{game_id}")


def test_the_seat_cli_flow(monkeypatch, capsys) -> None:
    """The whole thing through the seat CLI, as a player would actually do
    it: `wait` blocks and wakes with the pending decision named in its
    `wake_reason`, then `decide` answers it and the discard actually happens.
    """
    import httpx

    from magic_cards_model_api import seat_cli

    # `seat_cli`'s `Seat`/`Seats` each build their own `httpx.Client()`, meant
    # to reach a real server over the network; redirected here, at every call
    # site, to `TestClient`, which is an `httpx.Client` subclass that already
    # knows how to drive this `app` in-process.
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    http = TestClient(app)
    try:
        created = http.post(
            "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
        ).json()
        game_id = created["game_id"]
        uids = created["seat_uids"]  # already username -> uid
        try:
            for _ in range(8):
                http.post(
                    f"/api/games/{game_id}/cards",
                    json={"card": "Mountain", "player": "alice", "zone": "hand"},
                )

            thread = advance_into_cleanup(game_id)
            assert thread is not None
            try:
                deadline = time.monotonic() + 5
                pending = None
                while time.monotonic() < deadline:
                    pending = http.get(
                        f"/api/games/{game_id}", params={"uid": uids["alice"]}
                    ).json()["pending_decision"]
                    if pending is not None:
                        break
                    time.sleep(0.01)
                assert pending is not None, "cleanup never asked alice anything"
                card_id = pending["cards"][2]

                # `wait` returns as soon as it sees the decision - it does not
                # need to poll for long, since one is already pending.
                rc = seat_cli.run(
                    ["--uid", uids["alice"], "--game", game_id, "wait", "--max-seconds", "3"]
                )
                assert rc == 0
                printed = capsys.readouterr().out
                assert "a decision is waiting for you" in printed

                # `--hold`: `decide` now blocks for this seat's next moment to
                # act unless told to hold (the API-wide "one call, one
                # action" rework) - held here because there is no opponent
                # thread in this test to wake it, and answering the decision
                # is all this assertion cares about.
                rc = seat_cli.run(
                    ["--uid", uids["alice"], "--game", game_id, "decide", card_id, "--hold"]
                )
                assert rc == 0
            finally:
                thread.join(timeout=10)
            assert not thread.is_alive()

            final = http.get(f"/api/games/{game_id}", params={"uid": uids["alice"]}).json()
            discarded = [c["id"] for c in final["graveyards"]["alice"]]
            assert discarded == [card_id]
        finally:
            http.delete(f"/api/games/{game_id}")
    finally:
        http.close()


# -- who is actually behind the seat (decisions.DecisionMailbox) --------------
#
# `wait_for_answer` used to give its *delivery* phase the same budget as its
# *thinking* phase, so a game nobody was watching paid `2 x timeout_seconds`
# per question - the multiplier that turned a two-seat pregame into a 1200s
# block. The two phases measure different things, and `delivery_bound` is
# what tells them apart, from the one piece of evidence there is: whether any
# seat has actually read this game's state (`app.py`'s `get_game` records it).


def test_a_watching_seat_is_still_given_its_full_thinking_time(client, game) -> None:
    """The three states of "is anybody there", read straight off the mailbox.

    An unproven seat gets the one-off presence probe; a seat that has read the
    game gets the (much shorter than thinking time, but real) delivery bound;
    and a seat that has not is still unproven - one seat watching does not
    vouch for the other."""
    mailbox = store.mailbox(game)
    assert mailbox is not None
    assert mailbox.delivery_bound("alice", delivery=10.0, presence=3.0) == 3.0
    mailbox.mark_watching("alice")
    assert mailbox.is_watching("alice") is True
    assert mailbox.delivery_bound("alice", delivery=10.0, presence=3.0) == 10.0
    assert mailbox.delivery_bound("bob", delivery=10.0, presence=3.0) == 3.0


def test_a_seat_that_shows_up_late_is_no_longer_written_off(client, game) -> None:
    """The absent latch is a latch, not a verdict: an unattended game pays one
    probe rather than one per question, but the moment that seat really does
    read the game it is present again."""
    from magic_cards_model.machines.decisions import Decision, DecisionKind

    from magic_cards_model_api.decisions import PendingDecision

    mailbox = store.mailbox(game)
    assert mailbox is not None
    decision = Decision(kind=DecisionKind.MAY, player="bob", prompt="you may")
    mailbox.publish(PendingDecision.from_engine(decision), decision)
    mailbox.wait_for_answer(0.05, 0.05)
    assert mailbox.delivery_bound("bob", delivery=10.0, presence=3.0) == 0.0
    mailbox.mark_watching("bob")
    assert mailbox.delivery_bound("bob", delivery=10.0, presence=3.0) == 10.0


def test_a_state_read_is_what_proves_a_seat_is_there(client, game) -> None:
    """End to end through the transport, not the mailbox directly: a seat's own
    `GET /api/games/{id}` is the evidence, and it counts whether or not a
    decision happens to be pending for that seat at the time."""
    mailbox = store.mailbox(game)
    uids = seats_for(client, game)
    assert mailbox.is_watching("alice") is False
    client.get(f"/api/games/{game}", params={"uid": uids["alice"]})
    assert mailbox.is_watching("alice") is True
    assert mailbox.is_watching("bob") is False, "one seat's poll does not vouch for the other"
