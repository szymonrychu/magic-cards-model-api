"""The board view shows the *computed* board, not the printed cards.

`serialize._card_view` used to read `state.card.type_line`/`.power`/
`.toughness` straight off the Scryfall printing. That is right for a card
that has no computed characteristics to begin with (a card in hand has none -
"permanents exist only on the battlefield", CR 403.3), but wrong for a permanent, whose actual
type, power and toughness are whatever the layer system currently makes them
(CR 613.5). A player watching the API's view of the board could not see an
animated land, a double-faced creature's real power, an anthem, or a granted
keyword - they saw the printing, not the game.

This file proves the fix with two real, engine-driven cases: earthbend
animating a Forest (a type change no printed field could ever reflect), and a
double-faced creature whose top-level printed power is null.
"""

from __future__ import annotations

import threading
import time

from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def wait_for_pending(client: TestClient, game_id: str, uid: str, tries: int = 300) -> dict:
    for _ in range(tries):
        view = state(client, game_id, uid=uid)
        if view.get("pending_decision") is not None:
            return view
        time.sleep(0.01)
    raise AssertionError("no pending decision showed up in time")


def test_earthbend_animated_land_shows_its_computed_board(client, game):
    """Cast Badgermole Cub, earthbend a Forest, and check the *board view* -
    not the engine directly - reports what the Forest now is: a 1/1 land
    creature with haste, not the basic land Scryfall printed.
    """
    uids = seats_for(client, game)
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Badgermole Cub", "alice", "hand")
    client.put(f"/api/games/{game}/step", json={"step": "precombat_main"})

    board = state(client, game)
    forests = [card for card in board["battlefield"] if card["name"] == "Forest"]
    cub = board["hands"]["alice"][0]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": cub["id"],
            "mana_sources": [forests[0]["id"], forests[1]["id"]],
        },
    )
    assert cast.json()["allowed"] is True

    # Resolving the creature spell puts its ETB trigger on the stack and asks
    # for earthbend's target, so - the same shape test_target_decisions.py
    # uses for a cast trigger - the resolve call is the one that blocks.
    def resolve() -> None:
        stepping_client = TestClient(app)
        stepping_client.post(f"/api/games/{game}/resolve-top")

    thread = threading.Thread(target=resolve)
    thread.start()
    try:
        pending_view = wait_for_pending(client, game, uids["alice"])
        pending = pending_view["pending_decision"]
        assert pending["kind"] == "choose_targets"
        target_id = forests[0]["id"]
        answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "targets": [target_id]},
        )
        assert answer.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

    client.post(f"/api/games/{game}/resolve-top")  # the earthbend ability itself

    animated = find(state(client, game)["battlefield"], "Forest")
    assert animated["id"] == target_id

    # The computed board (CR 613.5): the land is now a creature too, a 1/1
    # (0/0 base plus its own +1/+1 counter), with haste.
    assert animated["type_line"] == "Basic Land Creature — Forest"
    assert animated["power"] == 1
    assert animated["toughness"] == 1
    assert "haste" in animated["keywords"]

    # And it still says what it printed, so a player can see it is animated
    # rather than just seeing a different number with no explanation.
    assert animated["printed_type_line"] == "Basic Land — Forest"
    assert animated["printed_power"] is None
    assert animated["printed_toughness"] is None

    other_forest = next(
        card
        for card in state(client, game)["battlefield"]
        if card["id"] != target_id and card["name"] == "Forest"
    )
    assert other_forest["type_line"] == "Basic Land — Forest"
    assert "printed_type_line" not in other_forest, (
        "an untouched permanent carries no redundant printed_* fields"
    )


def test_double_faced_creatures_printed_power_is_null_but_the_board_shows_the_real_one(
    client, game
):
    """Delver of Secrets is a `transform` card: Scryfall leaves the top-level
    `power`/`toughness` fields null and puts each face's own value on
    `card_faces` instead. `state.card.power` is therefore `None` for this
    creature even the moment it hits the battlefield - reading it straight
    would show a permanent with no power at all, not the 1/1 it actually is.
    """
    add_card(client, game, "Delver of Secrets", "alice", "battlefield")
    delver = find(state(client, game)["battlefield"], "Delver of Secrets")
    assert delver["power"] == 1
    assert delver["toughness"] == 1
    assert delver["type_line"] == "Creature — Human Wizard"
