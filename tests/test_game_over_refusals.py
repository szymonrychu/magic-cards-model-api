"""Nothing is taken after the game is over (battle 6, B6-06, the API half).

CR 104.1: "A game ends immediately when a player wins, when the game is a
draw, or when the game is restarted." Read in full with
`mise run rules:find 104.1` in the session that wrote this file.

The battle log had `game_over {"winner": "alice"}` with a spell still on the
stack, then a `pass_priority` that resolved it and paused on its triggers,
then two `decide` calls accepted. Every action route, `decide` and every dry
run now refuse a finished game with one typed 409 naming the game as over,
ahead of every other gate a seat could trip first (a stale `expected_seq`
would otherwise read as "the board moved" and send it off to re-read and try
again). Reads keep working, and no seat is woken to act.

Every board here is a real game finished through the real concede route.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import add_card, find, state
from fastapi.testclient import TestClient
from test_decision_kinds import (
    resolve_top,
    seats_for,
    stage_mystical_dispute_targeting_bears,
)

from magic_cards_model_api import seatkit
from magic_cards_model_api.store import store

#: Every route that takes an action in a game: the action routes, the
#: turn-structure drivers, `decide`, and the seat's pregame moves.
ACTION_ROUTES = (
    "actions/cast",
    "actions/suspend",
    "actions/plot",
    "actions/foretell",
    "actions/cycle",
    "actions/target",
    "actions/concede",
    "actions/activate",
    "actions/crew",
    "actions/station",
    "actions/attack",
    "actions/play-land",
    "actions/pass-priority",
    "actions/declare-blockers",
    "state-based-actions",
    "resolve-top",
    "advance",
    "decide",
    "pregame/mulligan",
    "pregame/opening-hand/action",
    "pregame/opening-hand/finish",
    "pregame/settle",
)


def finish_with_a_spell_on_the_stack(client: TestClient, game: str) -> str:
    """alice casts Grizzly Bears, then bob concedes (CR 104.3a) with it still
    on the stack - the shape battle 6 ended in. Returns the Bears' id."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    board = state(client, game)
    forests = [card["id"] for card in board["battlefield"] if card["name"] == "Forest"]
    bears = find(board["hands"]["alice"], "Grizzly Bears")["id"]
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": bears, "mana_sources": forests},
    )
    assert cast.json()["allowed"] is True, cast.text
    conceded = client.post(f"/api/games/{game}/actions/concede", json={"player": "bob"})
    assert conceded.status_code == 200, conceded.text
    assert conceded.json()["winner"] == "alice"
    return bears


def assert_game_over(response: Any) -> dict[str, Any]:
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["type"] == "game_over", body
    assert body["allowed"] is False
    assert "the game is over" in body["reason"]
    assert "alice won" in body["reason"]
    assert body["winner"] == "alice"
    assert body["drawn"] is False
    return body


@pytest.mark.parametrize("route", ACTION_ROUTES)
def test_every_action_route_refuses_a_finished_game(client, game, route):
    finish_with_a_spell_on_the_stack(client, game)
    params = {"uid": seats_for(client, game)["alice"]} if route == "decide" else None

    response = client.post(f"/api/games/{game}/{route}", json={"player": "alice"}, params=params)

    assert_game_over(response)


def test_a_pass_no_longer_resolves_the_spell_left_on_the_stack(client, game):
    """The reported sequence: after the ending, alice's pass resolved her
    spell. Now it is refused and the stack is exactly as the game ended."""
    finish_with_a_spell_on_the_stack(client, game)

    response = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})

    assert_game_over(response)
    after = state(client, game)
    assert [item["name"] for item in after["stack"]] == ["Grizzly Bears"]
    assert not any(card["name"] == "Grizzly Bears" for card in after["battlefield"])
    assert store.paused_command(game) is None


def test_a_dry_run_is_refused_too(client, game):
    finish_with_a_spell_on_the_stack(client, game)
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Mountain", "alice", "battlefield")
    board = state(client, game)

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": find(board["hands"]["alice"], "Lightning Bolt")["id"],
            "mana_sources": [find(board["battlefield"], "Mountain")["id"]],
            "targets": ["bob"],
            "dry_run": True,
        },
    )

    assert_game_over(response)


def test_the_game_over_refusal_comes_before_a_stale_board_refusal(client, game):
    """A seat that decided before the ending names a ledger position the
    `game_over` row has since moved past. "The board moved" would send it to
    re-read and try again; "the game is over" is the answer that stops it."""
    finish_with_a_spell_on_the_stack(client, game)

    response = client.post(
        f"/api/games/{game}/actions/pass-priority", json={"player": "alice", "expected_seq": 1}
    )

    assert_game_over(response)


def test_a_decision_asked_before_the_ending_cannot_be_answered_after_it(client, game):
    """alice is asked Mystical Dispute's pay_mana, then bob concedes (CR
    104.3a lets him at any time). Her answer arrives after the game ended."""
    uids = seats_for(client, game)
    stage_mystical_dispute_targeting_bears(client, game, {"R": 4})
    assert resolve_top(game)["paused"] is True
    conceded = client.post(f"/api/games/{game}/actions/concede", json={"player": "bob"})
    assert conceded.status_code == 200, conceded.text

    response = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )

    assert_game_over(response)


def test_reads_still_work_and_no_seat_is_woken_to_act(client, game):
    finish_with_a_spell_on_the_stack(client, game)
    uids = seats_for(client, game)

    listing = client.get("/api/games")
    assert listing.status_code == 200, listing.text
    assert game in listing.text

    for player, uid in uids.items():
        view = state(client, game, uid=uid)
        assert view["winner"] == "alice"
        assert view["pending_decision"] is None
        assert seatkit.compute_wake_reason(view, None, player) == "game over"
        legal = client.get(
            f"/api/games/{game}/legal-actions", params={"player": player, "uid": uid}
        )
        assert legal.status_code == 200, legal.text
        assert legal.json()["actions"] == [], legal.json()
