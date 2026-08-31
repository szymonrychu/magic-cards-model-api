"""Paying a cost with objects rather than mana: convoke, delve, and crew.

The engine gained one shared subsystem for these three (`ObjectOffer` in
`machines/objects_as_costs.py`), and none of it was reachable over HTTP: a
Vehicle could not be crewed at all, so a whole Pioneer archetype was
unplayable through this API, and neither convoke nor delve could be offered
against a determined cost.

Two halves are tested here, because a player needs both: *discovery* (which
objects are eligible, from `GET .../affordances/{card_id}`) and *payment*
(the action actually going through). A UI that can pay but cannot discover
is one the player has to already know the answer to use.
"""

from __future__ import annotations

from conftest import add_card, find, state
from fastapi.testclient import TestClient


def offers(client: TestClient, game: str, card_id: str, player: str = "alice") -> dict:
    response = client.get(f"/api/games/{game}/affordances/{card_id}", params={"player": player})
    assert response.status_code == 200, response.text
    return response.json().get("object_costs", {})


def names(offer: dict) -> set[str]:
    return {c["name"] for c in offer["candidates"]}


# -- crew (CR 702.122a) ------------------------------------------------------


def test_a_vehicle_offers_crew_with_its_number_and_the_eligible_creatures(client, game):
    add_card(client, game, "Smuggler's Copter", "alice", "battlefield")
    add_card(client, game, "Savannah Lions", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    copter = find(state(client, game)["battlefield"], "Smuggler's Copter")

    crew = offers(client, game, copter["id"])["crew"]
    assert crew["number"] == 1
    # "other untapped creatures *you control*" - not the opponent's, and not
    # the Vehicle itself.
    assert names(crew) == {"Savannah Lions"}
    assert crew["candidates"][0]["power"] == 2


def test_crewing_taps_the_creature_and_makes_the_vehicle_a_creature(client, game):
    add_card(client, game, "Smuggler's Copter", "alice", "battlefield")
    add_card(client, game, "Savannah Lions", "alice", "battlefield")
    board = state(client, game)["battlefield"]
    copter, lions = find(board, "Smuggler's Copter"), find(board, "Savannah Lions")
    assert "Creature" not in copter["type_line"]

    response = client.post(
        f"/api/games/{game}/actions/crew",
        json={"player": "alice", "card_id": copter["id"], "crewing": [lions["id"]]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is True, response.json()
    assert client.post(f"/api/games/{game}/resolve-top").status_code == 200

    after = state(client, game)["battlefield"]
    assert "Artifact Creature" in find(after, "Smuggler's Copter")["type_line"]
    assert find(after, "Savannah Lions")["tapped"] is True


def test_crewing_with_too_little_power_is_refused_and_taps_nothing(client, game):
    """CR 702.122a's "total power N or greater" is part of the cost, so
    failing it means the ability was never activated - and CR 733.1's rewind
    means the creature that was offered is not left tapped for nothing."""
    add_card(client, game, "Conclave Tribunal", "alice", "battlefield")  # not a Vehicle
    add_card(client, game, "Smuggler's Copter", "alice", "battlefield")
    board = state(client, game)["battlefield"]
    copter = find(board, "Smuggler's Copter")

    response = client.post(
        f"/api/games/{game}/actions/crew",
        json={"player": "alice", "card_id": copter["id"], "crewing": [copter["id"]]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is False
    assert find(state(client, game)["battlefield"], "Smuggler's Copter")["tapped"] is False


# -- convoke (CR 702.51a) ----------------------------------------------------


def test_a_convoke_spell_offers_the_creatures_that_could_tap_for_it(client, game):
    add_card(client, game, "Conclave Tribunal", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    tribunal = find(state(client, game)["hands"]["alice"], "Conclave Tribunal")

    convoke = offers(client, game, tribunal["id"])["convoke"]
    assert names(convoke) == {"Savannah Lions"}


def test_convoke_pays_the_whole_cost_with_creatures_and_no_lands(client, game):
    """CR 702.51b: convoke is neither an additional nor an alternative cost -
    it applies after the total cost is determined. Four creatures cover
    Conclave Tribunal's {3}{W} with no mana spent at all."""
    add_card(client, game, "Conclave Tribunal", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Savannah Lions", "alice", "battlefield")
    tribunal = find(state(client, game)["hands"]["alice"], "Conclave Tribunal")
    lions = [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Savannah Lions"]

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": tribunal["id"],
            # Savannah Lions is white, so it may cover the {W} (CR 702.51a);
            # the other three go against the generic.
            "convoke": [{"card_id": lions[0], "color": "W"}]
            + [{"card_id": card_id} for card_id in lions[1:]],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is True, body
    # The real total cost is still reported - convoke did not change it.
    assert body["cost"] == "{3}{W}"

    after = state(client, game)
    assert all(c["tapped"] for c in after["battlefield"] if c["name"] == "Savannah Lions")
    assert [o["name"] for o in after["stack"]] == ["Conclave Tribunal"]


# -- delve (CR 702.66a) ------------------------------------------------------


def test_a_delve_spell_offers_the_casters_own_graveyard(client, game):
    add_card(client, game, "Treasure Cruise", "alice", "hand")
    add_card(client, game, "Shock", "alice", "graveyard")
    add_card(client, game, "Lightning Bolt", "bob", "graveyard")
    cruise = find(state(client, game)["hands"]["alice"], "Treasure Cruise")

    delve = offers(client, game, cruise["id"])["delve"]
    # Your graveyard, not the opponent's.
    assert names(delve) == {"Shock"}
    # Power is meaningless off the battlefield and is not reported there.
    assert "power" not in delve["candidates"][0]


def test_delve_exiles_one_card_per_generic_and_leaves_the_coloured_symbol(client, game):
    """CR 702.66a is bounded by "each generic mana in this spell's total
    cost": seven cards cover Treasure Cruise's {7}, and the {U} still needs a
    real blue source."""
    add_card(client, game, "Treasure Cruise", "alice", "hand")
    add_card(client, game, "Island", "alice", "battlefield")
    for _ in range(7):
        add_card(client, game, "Shock", "alice", "graveyard")
    cruise = find(state(client, game)["hands"]["alice"], "Treasure Cruise")
    yard = [c["id"] for c in state(client, game)["graveyards"]["alice"]]
    island = [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Island"]

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": cruise["id"],
            "delve": yard,
            "mana_sources": island,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is True, body
    assert body["cost"] == "{7}{U}"

    after = state(client, game)
    assert after["graveyards"]["alice"] == []
    assert len(after["exile"]) == 7
    assert [o["name"] for o in after["stack"]] == ["Treasure Cruise"]


def test_a_card_with_none_of_the_three_reports_no_object_costs(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")
    assert offers(client, game, bolt["id"]) == {}
