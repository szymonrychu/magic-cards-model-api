"""Prod game 5c0e9a57 (2026-10-04): a human starting player and an AI seat,
stuck in CR 103.6's opening-hand step for 55 minutes.

The human ("you") kept, then the late-arriving AI seat ("claude", holding a
Gemstone Caverns) kept, and the game handed CR 103.6's window to the starting
player. Her opening hand offered no opening-hand action at all, so passing it
was the only legal answer - but nothing passes a human seat's window except
the human clicking Done, and her tab had gone to the background. The AI seat
then called `pregame/opening-hand/action` fourteen times over the hour and was
answered the same 400 every time.

Two halves pinned here:

- a window with exactly one legal answer is passed for a human seat the
  moment it is handed to her (the engine's own asking path asks nobody in
  that case, and `ai_pregame.is_forced` already treats it as no decision),
  and her own late Done is answered as the no-op it is, not refused;
- a seat repeating a pregame call the server has already refused, against a
  pregame that has not moved since, is told so - and what to do instead -
  rather than handed the identical refusal again.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from test_mulligans import (
    FIRST_PLAYABLE_STEP,
    LEYLINE_DECK,
    PLAIN_DECK,
    SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    boot,
    declare,
    delete,
    entries,
    finish_opening_hand,
    view,
)

from magic_cards_model_api.store import store

#: With this seed and alice on sixty Forests, alice is the starting player and
#: bob's (the AI seat's) dealt seven holds one Leyline of the Void - the prod
#: shape: the human first in CR 103.6's order with nothing to do, the AI seat
#: second with a real opening-hand action waiting.
SEED_HUMAN_FIRST_AI_HOLDS_A_LEYLINE = 1


def take_action(client: TestClient, info: dict[str, Any], player: str, card_id: str) -> Any:
    return client.post(
        f"/api/games/{info['game_id']}/pregame/opening-hand/action",
        params={"uid": info["seat_uids"][player]},
        json={"player": player, "card_id": card_id, "reasoning": "begin with it in play"},
    )


def test_a_human_window_with_nothing_in_it_is_passed_and_the_ai_seat_gets_its_turn(
    client: TestClient,
) -> None:
    info = boot(
        client,
        alice_deck=PLAIN_DECK,
        bob_deck=LEYLINE_DECK,
        seed=SEED_HUMAN_FIRST_AI_HOLDS_A_LEYLINE,
        ai_players=("bob",),
    )
    try:
        assert declare(client, info, "alice", take=False).status_code == 200
        assert declare(client, info, "bob", take=False).status_code == 200

        pregame = view(client, info, "bob")["pregame"]
        assert pregame["opening_hand_actor"] == "bob", (
            "the game is still waiting on a human whose CR 103.6 window has one legal answer"
        )
        (offer,) = pregame["opening_hand_options"]["bob"]
        taken = take_action(client, info, "bob", offer["card_id"])
        assert taken.status_code == 200, taken.text
        assert [card.name for card in store.get(info["game_id"]).battlefield] == [
            "Leyline of the Void"
        ]

        (passed,) = [
            e for e in entries(client, info, "opening_hand_done") if e["player"] == "alice"
        ]
        assert passed["automatic"] is True and passed["live"] is False
        assert "one legal answer" in passed["reason"]

        # Her browser's Done, arriving after the pass was made for her, is the
        # same move: answered, not refused.
        late = finish_opening_hand(client, info, "alice")
        assert late.status_code == 200, late.text
        assert late.json()["already_passed"] is True

        done = finish_opening_hand(client, info, "bob")
        assert done.status_code == 200, done.text
        assert done.json()["step"] == FIRST_PLAYABLE_STEP
    finally:
        delete(client, info)


def test_a_human_window_with_a_real_choice_in_it_is_still_hers(client: TestClient) -> None:
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
        ai_players=("bob",),
    )
    try:
        assert declare(client, info, "alice", take=False).status_code == 200
        assert declare(client, info, "bob", take=False).status_code == 200
        pregame = view(client, info, "alice")["pregame"]
        assert pregame["opening_hand_actor"] == "alice"
        assert pregame["opening_hand_options"]["alice"]
    finally:
        delete(client, info)


def test_a_repeated_refused_pregame_call_is_told_to_stop_and_what_to_do(
    client: TestClient,
) -> None:
    """alice holds a real CR 103.6 choice (a Leyline), so the window stays
    with her; bob, the AI seat, keeps sending his own move out of turn."""
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
        ai_players=("bob",),
    )
    try:
        assert declare(client, info, "alice", take=False).status_code == 200
        assert declare(client, info, "bob", take=False).status_code == 200

        first = finish_opening_hand(client, info, "bob")
        assert first.status_code == 400, first.text
        detail = first.json()["detail"]
        assert "alice's turn" in detail
        assert "hidden" in detail and "wait_for_priority" in detail

        again = finish_opening_hand(client, info, "bob")
        assert again.status_code == 409, again.text
        body = again.json()
        assert body["repeated"] is True
        assert "already refused" in body["detail"]
        assert "do not send it again" in body["detail"]
        assert "wait_for_priority" in body["detail"]

        # Once the pregame moves, the same call is judged afresh.
        assert finish_opening_hand(client, info, "alice").status_code == 200
        assert finish_opening_hand(client, info, "bob").json()["step"] == FIRST_PLAYABLE_STEP
    finally:
        delete(client, info)
