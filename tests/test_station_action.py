"""Station (CR 702.184a) over HTTP: `actions/station`, end to end on the real
engine and real cards (Extinguisher Battleship, Craw Wurm).

"Station" means "Tap another untapped creature you control: Put a number of
charge counters on this permanent equal to the tapped creature's power.
Activate only as a sorcery." The engine half is
`machines.keywords.station_activation`; this proves the api reaches it.
"""

from __future__ import annotations

from conftest import add_card, find, force_turn_position, resolve_whole_stack, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step


def _board(client: TestClient, game: str, step: Step = Step.PRECOMBAT_MAIN) -> tuple[dict, dict]:
    add_card(client, game, "Extinguisher Battleship", "alice", "battlefield")
    add_card(client, game, "Craw Wurm", "alice", "battlefield")
    force_turn_position(game, step, "alice")
    board = state(client, game)["battlefield"]
    return find(board, "Extinguisher Battleship"), find(board, "Craw Wurm")


def test_a_station_permanent_offers_station_with_the_eligible_creatures(client, game):
    battleship, wurm = _board(client, game)
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")

    response = client.get(
        f"/api/games/{game}/affordances/{battleship['id']}", params={"player": "alice"}
    )
    assert response.status_code == 200, response.text
    station = response.json()["object_costs"]["station"]
    assert station["rule"] == "CR 702.184a"
    assert "number" not in station
    assert [c["id"] for c in station["candidates"]] == [wurm["id"]]


def test_stationing_puts_the_creatures_power_in_charge_counters(client, game):
    battleship, wurm = _board(client, game)

    response = client.post(
        f"/api/games/{game}/actions/station",
        json={"player": "alice", "card_id": battleship["id"], "tapping": wurm["id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is True, response.json()
    resolve_whole_stack(client, game)

    after = state(client, game)["battlefield"]
    ship = find(after, "Extinguisher Battleship")
    assert ship["counters"]["charge"] == 6
    assert "Creature" in ship["type_line"]
    assert {"flying", "trample"} <= set(ship["keywords"])
    assert find(after, "Craw Wurm")["tapped"] is True


def test_a_station_dry_run_taps_nothing(client, game):
    battleship, wurm = _board(client, game)

    response = client.post(
        f"/api/games/{game}/actions/station",
        json={
            "player": "alice",
            "card_id": battleship["id"],
            "tapping": wurm["id"],
            "dry_run": True,
        },
    )
    assert response.json()["allowed"] is True, response.json()
    after = state(client, game)["battlefield"]
    assert find(after, "Craw Wurm")["tapped"] is False
    assert not find(after, "Extinguisher Battleship").get("counters")


def test_station_in_combat_is_refused_with_the_sorcery_reason(client, game):
    battleship, wurm = _board(client, game, Step.BEGINNING_OF_COMBAT)

    body = client.post(
        f"/api/games/{game}/actions/station",
        json={"player": "alice", "card_id": battleship["id"], "tapping": wurm["id"]},
    ).json()
    assert body["allowed"] is False
    assert "sorcery" in body["reason"]
    assert find(state(client, game)["battlefield"], "Craw Wurm")["tapped"] is False


def test_station_tapping_an_opponents_creature_is_refused(client, game):
    battleship, _ = _board(client, game)
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    bears = find(state(client, game)["battlefield"], "Grizzly Bears")

    body = client.post(
        f"/api/games/{game}/actions/station",
        json={"player": "alice", "card_id": battleship["id"], "tapping": bears["id"]},
    ).json()
    assert body["allowed"] is False
    assert "control" in body["reason"]
    assert find(state(client, game)["battlefield"], "Grizzly Bears")["tapped"] is False
