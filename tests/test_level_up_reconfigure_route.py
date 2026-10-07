"""Level up (CR 702.87a) and reconfigure (CR 702.151a) are usable through the
api.

CR 702.87a: "Level up [cost]" means "[Cost]: Put a level counter on this
permanent. Activate only as a sorcery." CR 702.151a: "Reconfigure [cost]"
means "[Cost]: Attach this permanent to another target creature you control.
Activate only as a sorcery" and "[Cost]: Unattach this permanent. Activate
only if this permanent is attached to a creature and only as a sorcery."

The engine offers all three as `ACTIVATE` rows with a `keyword` and no
`ability_index` (`legal_actions._level_up_action`/`_reconfigure_actions`),
the shape equip already has - so, like equip, none of them can be taken
through `actions/activate`. Every card is a real Scryfall printing, every
board played through the api, every refusal the engine's own words.
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

LORD = "Lord of Shatterskull Pass"
SLING = "Simian Sling"


def level_board(client, game, lands: int = 2) -> dict:
    add_card(client, game, LORD, "alice", "battlefield")
    for _ in range(lands):
        add_card(client, game, "Mountain", "alice", "battlefield")
    board = state(client, game)
    return {
        "lord": find(board["battlefield"], LORD)["id"],
        "mountains": [c["id"] for c in board["battlefield"] if c["name"] == "Mountain"],
    }


def sling_board(client, game, lands: int = 4) -> dict:
    add_card(client, game, SLING, "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    for _ in range(lands):
        add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Watchwolf", "bob", "battlefield")
    board = state(client, game)
    return {
        "sling": find(board["battlefield"], SLING)["id"],
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


def rows_for(client, game, card_id: str) -> list[dict]:
    return [row for row in read_rows(client, game)["actions"] if row["card_id"] == card_id]


def post(client, game, route: str, body: dict) -> dict:
    response = client.post(f"/api/games/{game}/actions/{route}", json={"player": "alice", **body})
    assert response.status_code == 200, response.text
    return response.json()


def resolve_top(client, game) -> None:
    for player in ("alice", "bob"):
        passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": player})
        assert passed.json()["allowed"] is True, passed.json()


def log_rows(client, game, action: str) -> list[dict]:
    log = client.get(f"/api/games/{game}/log").json()["log"]
    return [entry for entry in log if entry["action"] == action]


# -- level up (CR 702.87a) ----------------------------------------------------


def test_the_level_up_activation_is_a_legal_action_row(client, game):
    rig = level_board(client, game)

    (row,) = [row for row in rows_for(client, game, rig["lord"]) if row["kind"] == "level_up"]
    assert row["rule"] == "CR 702.87a", row
    assert row["keyword"] == "Level Up", row
    assert row["cost"] == "{1}{R}", row
    assert row["affordable"] == "yes", row
    assert row.get("ability_index") is None, row
    assert not row.get("target_slots"), row
    # The play UI routes a click on the leveler to its menu off this keyword.
    lord = find(state(client, game)["battlefield"], LORD)
    assert "level up" in [str(k).lower() for k in lord["keywords"]], lord


def test_level_up_puts_a_level_counter_on_the_permanent(client, game):
    rig = level_board(client, game)
    seq = read_rows(client, game)["last_seq"]

    answer = post(
        client,
        game,
        "level-up",
        {"card_id": rig["lord"], "mana_sources": rig["mountains"], "expected_seq": seq},
    )
    assert answer["allowed"] is True, answer
    assert answer["cost"] == "{1}{R}", answer
    assert "ability_id" in answer, answer
    assert find(state(client, game)["battlefield"], LORD)["counters"].get("level", 0) == 0

    resolve_top(client, game)
    lord = find(state(client, game)["battlefield"], LORD)
    assert lord["counters"]["level"] == 1, lord

    (row,) = log_rows(client, game, "level_up")
    assert row["detail"]["allowed"] is True


def test_a_dry_run_level_up_changes_nothing(client, game):
    rig = level_board(client, game)

    answer = post(client, game, "level-up", {"card_id": rig["lord"], "dry_run": True})
    assert answer["allowed"] is True, answer
    assert answer["cost"] == "{1}{R}", answer
    board = state(client, game)
    assert board["stack"] == []
    assert not any(card["tapped"] for card in board["battlefield"])


def test_an_unaffordable_level_up_is_refused_with_the_engines_shortfall(client, game):
    rig = level_board(client, game, lands=1)

    (row,) = [row for row in rows_for(client, game, rig["lord"]) if row["kind"] == "level_up"]
    assert row["affordable"] == "no", row

    answer = post(client, game, "level-up", {"card_id": rig["lord"]})
    assert answer["allowed"] is False, answer
    assert answer["reason"], answer
    board = state(client, game)
    assert board["stack"] == []
    assert not any(card["tapped"] for card in board["battlefield"])


def test_level_up_at_instant_speed_is_refused_with_the_engines_reason(client, game):
    rig = level_board(client, game)
    force_turn_position(game, Step.BEGINNING_OF_COMBAT, "alice")

    assert not [row for row in rows_for(client, game, rig["lord"]) if row["kind"] == "level_up"]
    answer = post(client, game, "level-up", {"card_id": rig["lord"]})
    assert answer["allowed"] is False, answer
    assert "only be activated as a sorcery" in answer["reason"], answer
    assert "CR 702.87a" in answer["reason"], answer
    (row,) = log_rows(client, game, "level_up")
    assert row["detail"]["offered"] is False, row["detail"]


def test_level_up_on_a_card_without_it_is_refused_with_the_engines_reason(client, game):
    rig = sling_board(client, game)

    answer = post(client, game, "level-up", {"card_id": rig["bears"]})
    assert answer["allowed"] is False, answer
    assert "has no level up ability" in answer["reason"], answer


# -- reconfigure (CR 702.151a) ------------------------------------------------


def test_reconfigure_offers_the_attach_row_with_its_candidates_and_no_unattach(client, game):
    rig = sling_board(client, game)

    rows = [row for row in rows_for(client, game, rig["sling"]) if row["kind"] == "reconfigure"]
    (row,) = rows
    assert row["rule"] == "CR 702.151a", row
    assert row["keyword"] == "Reconfigure", row
    assert row["cost"] == "{2}", row
    assert row.get("unattach") is None, row
    sling = find(state(client, game)["battlefield"], SLING)
    assert "reconfigure" in [str(k).lower() for k in sling["keywords"]], sling
    (slot,) = row["target_slots"]
    assert slot["minimum"] == 1 and slot["maximum"] == 1, slot
    (candidates,) = row["targets"]["slots"]
    assert any(rig["bears"] in line for line in candidates["candidates"]), candidates
    assert not any(rig["wolf"] in line for line in candidates["candidates"]), candidates
    assert not any(rig["sling"] in line for line in candidates["candidates"]), candidates


def test_reconfigure_attaches_then_unattaches_through_the_route(client, game):
    rig = sling_board(client, game)
    seq = read_rows(client, game)["last_seq"]

    attached = post(
        client,
        game,
        "reconfigure",
        {
            "card_id": rig["sling"],
            "target": rig["bears"],
            "mana_sources": rig["mountains"][:2],
            "expected_seq": seq,
        },
    )
    assert attached["allowed"] is True, attached
    assert attached["cost"] == "{2}", attached
    resolve_top(client, game)
    sling = find(state(client, game)["battlefield"], SLING)
    assert sling["attached_to"]["id"] == rig["bears"], sling

    rows = [row for row in rows_for(client, game, rig["sling"]) if row["kind"] == "reconfigure"]
    (unattach_row,) = [row for row in rows if row.get("unattach")]
    assert unattach_row["unattach"] is True
    assert not unattach_row.get("target_slots"), unattach_row

    unattached = post(
        client,
        game,
        "reconfigure",
        {"card_id": rig["sling"], "mana_sources": rig["mountains"][2:]},
    )
    assert unattached["allowed"] is True, unattached
    resolve_top(client, game)
    sling = find(state(client, game)["battlefield"], SLING)
    assert not sling.get("attached_to"), sling

    first, second = log_rows(client, game, "reconfigure")
    assert first["detail"]["target"] == rig["bears"]
    assert second["detail"].get("target") is None


def test_unattach_while_unattached_is_refused_with_the_engines_reason(client, game):
    rig = sling_board(client, game)

    answer = post(client, game, "reconfigure", {"card_id": rig["sling"]})
    assert answer["allowed"] is False, answer
    assert "only be activated while it is attached" in answer["reason"], answer
    (row,) = log_rows(client, game, "reconfigure")
    # The attach row for this same card was on offer; the unattach was not.
    assert row["detail"]["offered"] is False, row["detail"]


def test_reconfigure_onto_a_creature_you_do_not_control_is_refused(client, game):
    rig = sling_board(client, game)

    answer = post(client, game, "reconfigure", {"card_id": rig["sling"], "target": rig["wolf"]})
    assert answer["allowed"] is False, answer
    assert "does not control Watchwolf" in answer["reason"], answer
    assert "CR 702.151a" in answer["reason"], answer
    (row,) = log_rows(client, game, "reconfigure")
    assert row["detail"]["offered"] is True, "the attach was on offer; the target was the misread"
    board = state(client, game)
    assert board["stack"] == []
    assert not any(card["tapped"] for card in board["battlefield"])


def test_reconfigure_onto_itself_is_refused(client, game):
    rig = sling_board(client, game)

    answer = post(client, game, "reconfigure", {"card_id": rig["sling"], "target": rig["sling"]})
    assert answer["allowed"] is False, answer
    assert "another creature" in answer["reason"], answer


def test_an_unaffordable_reconfigure_is_refused(client, game):
    rig = sling_board(client, game, lands=1)

    answer = post(client, game, "reconfigure", {"card_id": rig["sling"], "target": rig["bears"]})
    assert answer["allowed"] is False, answer
    assert answer["reason"], answer
    assert state(client, game)["stack"] == []


# -- the CLI and MCP front ends -----------------------------------------------


def test_both_front_ends_level_up_and_reconfigure(client, game, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    rig = level_board(client, game)
    sling = sling_board(client, game, lands=2)
    uid = seat_uid(game, "alice")
    base = ["--uid", uid, "--game", game]
    assert seat_cli.run([*base, "level-up", rig["lord"], "--hold"]) == 0
    cli_answer = json.loads(capsys.readouterr().out)
    assert cli_answer["outcome"]["allowed"] is True, cli_answer
    resolve_top(client, game)
    assert find(state(client, game)["battlefield"], LORD)["counters"]["level"] == 1

    assert (
        seat_cli.run([*base, "reconfigure", sling["sling"], "--target", sling["bears"], "--hold"])
        == 0
    )
    cli_answer = json.loads(capsys.readouterr().out)
    assert cli_answer["outcome"]["allowed"] is True, cli_answer
    resolve_top(client, game)
    attached = find(state(client, game)["battlefield"], SLING)["attached_to"]
    assert attached["id"] == sling["bears"]

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    try:
        rig2 = level_board(client, second)
        sling2 = sling_board(client, second, lands=4)
        server = build_server(Seats(api="http://testserver", game_id=second))
        uid2 = seat_uid(second, "alice")

        def call(tool: str, args: dict) -> dict:
            answer = asyncio.run(
                server.call_tool(tool, {"player_uid": uid2, "hold_priority": True, **args})
            )
            return json.loads(answer.content[0].text)

        mcp_answer = call("level_up", {"card_id": rig2["lord"]})
        assert mcp_answer["outcome"]["allowed"] is True, mcp_answer
        resolve_top(client, second)
        assert find(state(client, second)["battlefield"], LORD)["counters"]["level"] == 1

        mcp_answer = call("reconfigure", {"card_id": sling2["sling"], "target": sling2["bears"]})
        assert mcp_answer["outcome"]["allowed"] is True, mcp_answer
        resolve_top(client, second)
        mcp_answer = call("reconfigure", {"card_id": sling2["sling"]})
        assert mcp_answer["outcome"]["allowed"] is True, mcp_answer
        resolve_top(client, second)
        assert not find(state(client, second)["battlefield"], SLING).get("attached_to")
    finally:
        client.delete(f"/api/games/{second}")


def test_both_routes_refuse_a_finished_game(client, game):
    """CR 104.1: once the game is over no action is taken - the same 409 the
    other action routes answer (`test_game_over_refusals`)."""
    conceded = client.post(f"/api/games/{game}/actions/concede", json={"player": "bob"})
    assert conceded.status_code == 200, conceded.text

    for route in ("level-up", "reconfigure"):
        response = client.post(f"/api/games/{game}/actions/{route}", json={"player": "alice"})
        assert response.status_code == 409, response.text
        assert response.json()["type"] == "game_over", response.json()
