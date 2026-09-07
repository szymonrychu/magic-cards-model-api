"""Answering a paused decision: a real seat, outside the engine's call stack,
resuming a command that paused on a CR 608.2d question through
`POST .../decide`.

`Game.ask` (`magic_cards_model.machines.decisions`) tries a scripted answer,
then the replay tape, then - with `pause_when_unanswered` set
(`commands.run_command`) - raises `DecisionPending` rather than blocking a
thread. The mutating request that crossed the question (`advance`, most of
the tests below, since it is what walks the turn into cleanup) returns
*immediately*, `paused: True`, with `pending_decision` attached - there is no
longer a background thread to wait on, so every helper below that used to
spawn one is now a synchronous, in-request check instead.

The one mechanic wired up this slice is the cleanup discard (CR 514.1,
701.9b): the active player chooses which card(s) to discard down to maximum
hand size, instead of the engine silently taking the back of the hand.
`advance_into_cleanup` walks `POST .../advance` one step at a time - not
`PUT .../step` (`Game.advance_to`, which crosses step boundaries without
running any turn-based action, cleanup's discard included) - until the call
that enters cleanup pauses on it.
"""

from __future__ import annotations

import time
from typing import Any

from conftest import add_card, state
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def stuff_hand(client: TestClient, game_id: str, player: str, count: int) -> None:
    for _ in range(count):
        add_card(client, game_id, "Mountain", player, "hand")


def advance_into_cleanup(game_id: str, player: str = "alice") -> dict[str, Any] | None:
    """Advance one step at a time until the call that enters cleanup, which
    now pauses on the discard decision instead of blocking a thread for it.

    A `declare_attackers`/`declare_blockers` pause met on the way is not the
    discard this helper is walking toward - CR 508.1/CR 509.1 both pause on
    their own now (there is no more silent "declares nobody" default), so
    this answers each with nobody attacking/blocking (always legal, "if
    any") and keeps walking, the same way a fresh, creatureless board always
    behaved before this task.

    Returns the discard's own paused response (`paused: True`,
    `pending_decision` set), or `None` if cleanup was entered without ever
    pausing (nothing to discard).
    """
    client = TestClient(app)
    for _ in range(30):
        response = client.post(f"/api/games/{game_id}/advance", json={"player": player})
        assert response.status_code == 200, response.text
        body = response.json()
        if body.get("paused"):
            pending = body["pending_decision"]
            if pending["kind"] in ("declare_attackers", "declare_blockers"):
                uid = seats_for(client, game_id)[pending["player"]]
                decided = client.post(
                    f"/api/games/{game_id}/decide",
                    params={"uid": uid},
                    json={"player": pending["player"], "pairs": []},
                )
                assert decided.status_code == 200, decided.text
                assert decided.json()["accepted"] is True, decided.json()
                continue
            return body
        if body["advanced_to"] == "cleanup":
            return None
    raise AssertionError("never reached cleanup")


def wait_for_pending(client: TestClient, game_id: str, uid: str, tries: int = 10) -> dict:
    """The state view showing a pending decision for `uid` - a thin wrapper
    over one read, kept only so call sites read the same as they used to.
    Nothing here actually waits any more: `advance_into_cleanup`'s own
    response already carries the pause, so by the time this is called the
    decision is already sitting in the store - this is a same-request
    confirmation, not a poll loop.
    """
    for _ in range(tries):
        view = state(client, game_id, uid=uid)
        if view.get("pending_decision") is not None:
            return view
        time.sleep(0.01)
    raise AssertionError("no pending decision showed up")


def test_a_seat_sees_and_answers_its_pending_decision(client, game):
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    mountains = [card["id"] for card in board["hands"]["alice"]]
    to_discard = mountains[3]  # an arbitrary one, to prove it is a real choice

    paused = advance_into_cleanup(game)
    assert paused is not None, "the discard should have asked, not defaulted silently"
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

    `decisions.validate_answer` catches this before it ever reaches the
    engine: the refusal reaches this very response, carries the engine's own
    count-mismatch wording, the audit log records it as a refusal rather than
    an acceptance, the decision is still pending afterwards, and the same
    seat can then answer correctly and the game proceeds.
    """
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    mountains = [card["id"] for card in board["hands"]["alice"]]
    to_discard = mountains[3]

    paused = advance_into_cleanup(game)
    assert paused is not None, "the discard should have asked, not defaulted silently"
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
    # was accepted onto the tape at all.
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

    paused = advance_into_cleanup(game)
    assert paused is not None
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

    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [alices_pending["cards"][0]]},
    )


def test_the_opponent_cannot_answer_the_decision(client, game):
    stuff_hand(client, game, "alice", 8)
    uids = seats_for(client, game)

    paused = advance_into_cleanup(game)
    assert paused is not None
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

    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [a_card_id]},
    )


def test_answering_the_decision_takes_the_game_lock_like_any_other_mutating_request(
    client, game
) -> None:
    """`decide` is an ordinary mutating request now - it takes
    `store.locked(game_id)` for the whole resume, the same as `cast` or
    `advance`. There is no longer a resolving thread parked in `Game.ask`
    for it to avoid racing (`commands.py`'s module docstring): the command
    that paused already returned, and this call replays it synchronously
    within its own request.
    """
    stuff_hand(client, game, "alice", 8)
    uids = seats_for(client, game)

    paused = advance_into_cleanup(game)
    assert paused is not None
    pending_view = wait_for_pending(client, game, uids["alice"])
    card_id = pending_view["pending_decision"]["cards"][0]

    started = time.monotonic()
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [card_id]},
    )
    assert time.monotonic() - started < 5, "decide should resolve promptly"
    assert answered.status_code == 200
    assert answered.json()["accepted"] is True


def test_the_seat_cli_flow(monkeypatch, capsys) -> None:
    """The whole thing through the seat CLI, as a player would actually do
    it: `wait` sees the pending decision named in its `wake_reason` right
    away (the pause already happened, synchronously, inside the advance
    call), then `decide` answers it and the discard actually happens.
    """
    import httpx

    from magic_cards_model_api import seat_cli

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

            paused = advance_into_cleanup(game_id)
            assert paused is not None
            pending = http.get(f"/api/games/{game_id}", params={"uid": uids["alice"]}).json()[
                "pending_decision"
            ]
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

            rc = seat_cli.run(
                ["--uid", uids["alice"], "--game", game_id, "decide", card_id, "--hold"]
            )
            assert rc == 0

            final = http.get(f"/api/games/{game_id}", params={"uid": uids["alice"]}).json()
            discarded = [c["id"] for c in final["graveyards"]["alice"]]
            assert discarded == [card_id]
        finally:
            http.delete(f"/api/games/{game_id}", params={"key": created["admin_key"]})
    finally:
        http.close()
