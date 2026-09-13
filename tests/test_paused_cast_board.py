"""A seat reading the board mid-cast sees the cast, not the board before it.

**The defect (battle 2, B2-13).** Ugin, Eye of the Storms' cast trigger asked
for its target, and Devourer of Destiny's cast trigger asked for the trigger
order, while every land was still untapped and the stack still empty - "visible
to any seat reading state mid-cast". The engine asks both questions in the
right place (`magic-cards-model`'s `tests/scenarios/test_cast_triggers.py`
pins it with a pause): CR 601.2h pays the total cost, CR 601.2i then makes the
spell cast and fires its cast triggers, and CR 603.3 puts them on the stack
the next time a player would receive priority - CR 603.3b's order question and
CR 603.3d's target announcement come only there.

What went wrong is this api. `commands.run_command` threw the working copy
away on `DecisionPending`, so between the pause and the answer every state
read served the last *committed* game: the card still in hand, the lands
untapped, the stack empty. The questions looked as if they came before the
payment because the board the seat was shown had not been paid yet.

CR 601.2a moves the card to the stack as the first step of casting, and
CR 601.2g-h's mana abilities and payment are real game actions, so the board
at the pause is the one a seat must see. Every CR number here was read with
`mise run rules:find` in the session that wrote this file.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def cast_into_the_trigger(
    client: TestClient, game_id: str, spell: str, lands: list[str], extra: tuple[str, ...] = ()
) -> dict[str, Any]:
    for name in lands:
        add_card(client, game_id, name, "alice", "battlefield")
    for name in extra:
        add_card(client, game_id, name, "alice", "battlefield")
    add_card(client, game_id, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game_id, spell, "alice", "hand")
    board = state(client, game_id)
    sources = [card["id"] for card in board["battlefield"] if card["name"] in set(lands)]
    cast = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={"player": "alice", "card": spell, "mana_sources": sources},
    )
    assert cast.status_code == 200, cast.text
    body = cast.json()
    assert body.get("paused") is True, body
    return body


def assert_the_board_shows_the_cast(board: dict[str, Any], spell: str, lands: list[str]) -> None:
    stack = [entry["name"] for entry in board["stack"] if entry.get("kind") == "spell"]
    assert stack == [spell], f"CR 601.2a: the spell is on the stack at the pause: {board['stack']}"
    tapped = [card for card in board["battlefield"] if card["name"] in set(lands)]
    assert tapped and all(card["tapped"] for card in tapped), (
        f"CR 601.2h: the cost is paid before the cast trigger asks: {tapped}"
    )
    hand = board["hands"].get("alice") or []
    assert all(card.get("name") != spell for card in hand), hand


def test_ugins_cast_trigger_target_is_asked_over_a_paid_board(client, game) -> None:
    lands = ["Mountain"] * 7
    uids = seats_for(game)
    paused = cast_into_the_trigger(client, game, "Ugin, Eye of the Storms", lands)
    assert paused["pending_decision"]["kind"] == "choose_targets", paused

    for seat in ("alice", "bob"):
        assert_the_board_shows_the_cast(
            state(client, game, uid=uids[seat]), "Ugin, Eye of the Storms", lands
        )


def test_a_second_cast_trigger_card_is_asked_over_a_paid_board(client, game) -> None:
    """Petrifying Meddler, in no deck pool: an "up to one target" cast
    trigger on a different cost from Ugin's."""
    lands = ["Island"] * 5
    uids = seats_for(game)
    paused = cast_into_the_trigger(client, game, "Petrifying Meddler", lands)
    assert paused["pending_decision"]["kind"] == "choose_targets", paused

    assert_the_board_shows_the_cast(
        state(client, game, uid=uids["bob"]), "Petrifying Meddler", lands
    )
    assert_the_board_shows_the_cast(state(client, game), "Petrifying Meddler", lands)


def test_the_trigger_order_question_is_asked_over_a_paid_board(client, game) -> None:
    """Devourer of Destiny's own cast trigger beside Ugin's "whenever you
    cast a colorless spell": CR 603.3b's order question, the report's second
    repro."""
    lands = ["Wastes"] * 7
    uids = seats_for(game)
    paused = cast_into_the_trigger(
        client, game, "Devourer of Destiny", lands, extra=["Ugin, Eye of the Storms"]
    )
    assert paused["pending_decision"]["kind"] == "order_triggers", paused

    assert_the_board_shows_the_cast(
        state(client, game, uid=uids["bob"]), "Devourer of Destiny", lands
    )


def test_answering_the_trigger_commits_the_same_board(client, game) -> None:
    """The paused board is a view only: the answer still resumes from the
    committed game, and the committed result is the board the pause showed
    plus the trigger on the stack."""
    lands = ["Island"] * 5
    uids = seats_for(game)
    paused = cast_into_the_trigger(client, game, "Petrifying Meddler", lands)
    pending = state(client, game, uid=uids["alice"])["pending_decision"]
    bears = find(state(client, game)["battlefield"], "Grizzly Bears")["id"]
    assert {"kind": "card", "id": bears} in pending["targets"], pending

    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "targets": [bears], "generation": pending["generation"]},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True, answered.json()

    board = state(client, game)
    assert board["pending_decision"] is None
    assert_the_board_shows_the_cast(board, "Petrifying Meddler", lands)
    assert [entry["kind"] for entry in board["stack"]] == ["ability", "spell"], board["stack"]
    assert paused["pending_decision"]["generation"] == pending["generation"]
