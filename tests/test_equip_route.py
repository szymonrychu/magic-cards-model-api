"""Battle 3, B3-01: equip is usable through the API.

CR 702.6a: "Equip is an activated ability of Equipment cards. 'Equip [cost]'
means '[Cost]: Attach this permanent to target creature you control. Activate
only as a sorcery.'" CR 301.5b: an Equipment enters attached to nothing, so
without this ability every Equipment in the pool is inert.

The engine has built the activation all along (`machines.keywords.
equip_activation`, the object its own `tests/scenarios/test_equip.py` builds),
but nothing in the api reached it: `Game.activated_abilities` does not list a
keyword ability, so Blade of the Bloodchief had no legal-action row, and
`actions/activate <blade> 0` was refused "has no activated ability number 0".
bloodchief-combo's whole deck-tech loop could never start.

CR 307.5 ("only as a sorcery": priority, own main phase, empty stack) and
CR 301.5 ("It can't legally be attached to anything that isn't a creature")
were read from the Comprehensive Rules text in the session that wrote this.
Every card is a real Scryfall printing, every board played through the api.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from conftest import add_card, find, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_legal_actions_endpoint import seat_uid

from magic_cards_model_api import seat_cli
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats


def equip_board(client, game, equipment: str = "Blade of the Bloodchief") -> dict:
    add_card(client, game, equipment, "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Watchwolf", "bob", "battlefield")
    board = state(client, game)
    return {
        "equipment": find(board["battlefield"], equipment)["id"],
        "bears": find(board["battlefield"], "Grizzly Bears")["id"],
        "wolf": find(board["battlefield"], "Watchwolf")["id"],
        "mountains": [c["id"] for c in board["battlefield"] if c["name"] == "Mountain"],
    }


def read_rows(client, game, player: str = "alice") -> dict:
    response = client.get(
        f"/api/games/{game}/legal-actions",
        params={"player": player, "uid": seat_uid(game, player), "candidates": True},
    )
    assert response.status_code == 200, response.text
    return response.json()


def equip(client, game, rig: dict, target: str, **extra) -> dict:
    response = client.post(
        f"/api/games/{game}/actions/equip",
        json={
            "player": "alice",
            "card_id": rig["equipment"],
            "target": target,
            "mana_sources": rig["mountains"],
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def equip_rows_in_log(client, game) -> list[dict]:
    log = client.get(f"/api/games/{game}/log").json()["log"]
    return [entry for entry in log if entry["action"] == "equip"]


def test_the_equip_activation_is_a_legal_action_row_with_its_candidates(client, game):
    rig = equip_board(client, game)

    rows = [row for row in read_rows(client, game)["actions"] if row["card_id"] == rig["equipment"]]
    (row,) = rows
    assert row["kind"] == "equip", row
    assert row["rule"] == "CR 702.6a", row
    assert row["keyword"] == "Equip", row
    assert row["cost"] == "{1}", row
    assert row["affordable"] == "yes", row
    (slot,) = row["target_slots"]
    assert slot["minimum"] == 1 and slot["maximum"] == 1, slot
    (candidates,) = row["targets"]["slots"]
    assert any(rig["bears"] in line for line in candidates["candidates"]), candidates
    assert not any(rig["wolf"] in line for line in candidates["candidates"]), candidates


def test_the_row_generalises_to_any_equipment_and_its_own_cost(client, game):
    rig = equip_board(client, game, "Cori-Steel Cutter")

    (row,) = [
        row for row in read_rows(client, game)["actions"] if row["card_id"] == rig["equipment"]
    ]
    assert row["kind"] == "equip"
    assert row["cost"] == "{1}{R}", row


def test_equip_attaches_through_the_route_naming_the_board(client, game):
    rig = equip_board(client, game)
    seq = read_rows(client, game)["last_seq"]

    answer = equip(client, game, rig, rig["bears"], expected_seq=seq)
    assert answer["allowed"] is True, answer
    assert answer["cost"] == "{1}", answer
    assert "ability_id" in answer, answer

    for player in ("alice", "bob"):
        passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": player})
        assert passed.json()["allowed"] is True, passed.json()
    blade = find(state(client, game)["battlefield"], "Blade of the Bloodchief")
    assert blade["attached_to"]["id"] == rig["bears"], blade

    log = client.get(f"/api/games/{game}/log").json()["log"]
    (row,) = [entry for entry in log if entry["action"] == "equip"]
    assert row["detail"]["allowed"] is True
    assert row["detail"]["target"] == rig["bears"]


def test_a_dry_run_equip_changes_nothing(client, game):
    rig = equip_board(client, game)

    answer = equip(client, game, rig, rig["bears"], dry_run=True)
    assert answer["allowed"] is True, answer
    assert answer["cost"] == "{1}"
    board = state(client, game)
    assert board["stack"] == []
    assert not any(card["tapped"] for card in board["battlefield"])


def test_equip_at_instant_speed_is_refused_with_the_engines_reason(client, game):
    rig = equip_board(client, game)
    force_turn_position(game, Step.BEGINNING_OF_COMBAT, "alice")

    rows = [row for row in read_rows(client, game)["actions"] if row["card_id"] == rig["equipment"]]
    assert rows == [], rows
    answer = equip(client, game, rig, rig["bears"])
    assert answer["allowed"] is False, answer
    assert "only be activated as a sorcery" in answer["reason"], answer
    assert "CR 702.6a" in answer["reason"], answer
    assert state(client, game)["stack"] == []
    (row,) = equip_rows_in_log(client, game)
    assert row["detail"]["offered"] is False, row["detail"]


def test_equip_onto_a_creature_you_do_not_control_is_refused(client, game):
    rig = equip_board(client, game)

    answer = equip(client, game, rig, rig["wolf"])
    assert answer["allowed"] is False, answer
    assert "Watchwolf" in answer["reason"], answer
    (row,) = equip_rows_in_log(client, game)
    assert row["detail"]["offered"] is True, "the row was on offer; the target was the misread"
    board = state(client, game)
    assert board["stack"] == []
    assert not any(card["tapped"] for card in board["battlefield"])


def test_equip_onto_a_noncreature_is_refused(client, game):
    rig = equip_board(client, game)

    answer = equip(client, game, rig, rig["mountains"][0])
    assert answer["allowed"] is False, answer
    assert "Mountain" in answer["reason"], answer


def test_both_front_ends_equip_with_the_same_arguments(client, game, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    rig = equip_board(client, game)
    uid = seat_uid(game, "alice")
    assert (
        seat_cli.run(
            [
                "--uid",
                uid,
                "--game",
                game,
                "equip",
                rig["equipment"],
                "--target",
                rig["bears"],
                "--hold",
            ]
        )
        == 0
    )
    cli_answer = json.loads(capsys.readouterr().out)
    assert cli_answer["outcome"]["allowed"] is True, cli_answer

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    try:
        rig2 = equip_board(client, second)
        server = build_server(Seats(api="http://testserver", game_id=second))
        answer = asyncio.run(
            server.call_tool(
                "equip",
                {
                    "player_uid": seat_uid(second, "alice"),
                    "card_id": rig2["equipment"],
                    "target": rig2["bears"],
                    "hold_priority": True,
                },
            )
        )
        mcp_answer = json.loads(answer.content[0].text)
        assert mcp_answer["outcome"]["allowed"] is True, mcp_answer
    finally:
        client.delete(f"/api/games/{second}")
