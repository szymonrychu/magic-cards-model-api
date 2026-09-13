"""U2 (battle 7 review): a target naming an object on the stack.

CR 115.1 makes targets "object(s) and/or player(s)", and CR 115.2 (b) lets a
spell or ability target "an object that can't exist on the battlefield, such
as a spell or ability" (both read whole with `mise run rules:find` in the
session that wrote this file). The engine models an ability on the stack as
an `AbilityOnStack` with its own id, looked up by `Game.find_ability_by_id`;
a spell on the stack is a `CardState` found by `Game.find_by_id`.
`engine_api.target_thing` only ever asked the second, so Consign to Memory
("Counter target triggered ability or colorless spell") could be offered the
trigger as a legal-action candidate and then refused it at the route as
"neither a player name nor a card id".

Every card is a real Scryfall printing: Kitchen Finks, Consign to Memory,
Karn, the Great Creator, Walking Ballista, Snow-Covered Forest from the
engine's pool; Rimewind Cryomancer recorded into tests/fixtures/scryfall.
"""

from __future__ import annotations

import asyncio
import json

from conftest import add_card, find, state
from test_front_end_parity import uid_for, wire

from magic_cards_model_api import seat_cli
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats


def _life(board: dict, player: str) -> int:
    return next(p for p in board["players"] if p["username"] == player)["life"]


def _kitchen_finks_trigger(client, game) -> dict:
    """Alice casts Kitchen Finks and both pass, so it resolves and its "When
    this creature enters, you gain 2 life." trigger is on the stack with
    alice holding priority. Returns that trigger's stack view."""
    for _ in range(3):
        add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Kitchen Finks", "alice", "hand")
    board = state(client, game)
    plains = [c["id"] for c in board["battlefield"] if c["name"] == "Plains"]
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": "Kitchen Finks", "mana_sources": plains},
    )
    assert cast.json()["allowed"] is True, cast.text
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    (trigger,) = state(client, game)["stack"]
    assert trigger["kind"] == "ability" and trigger["name"] == "Kitchen Finks"
    return trigger


def _islands(client, game, count: int) -> list[str]:
    for _ in range(count):
        add_card(client, game, "Island", "alice", "battlefield")
    return [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Island"]


def _cast_consign(client, game, target: str, islands: list[str]):
    return client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Consign to Memory",
            "targets": [target],
            "mana_sources": islands,
        },
    )


def test_consign_to_memory_counters_a_trigger_named_by_its_stack_id(client, game):
    add_card(client, game, "Consign to Memory", "alice", "hand")
    islands = _islands(client, game, 1)
    trigger = _kitchen_finks_trigger(client, game)
    assert _life(state(client, game), "alice") == 20

    cast = _cast_consign(client, game, trigger["id"], islands)
    assert cast.json()["allowed"] is True, cast.text
    on_stack = state(client, game)["stack"]
    assert on_stack[0]["name"] == "Consign to Memory"
    assert on_stack[0]["target_refs"] == [
        {"kind": "ability", "id": trigger["id"], "name": "ability from Kitchen Finks"}
    ]

    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    after = state(client, game)
    assert after["stack"] == [], "the countered trigger leaves the stack (CR 701.6a)"
    assert _life(after, "alice") == 20, "a countered trigger gains no life"


def test_the_legal_action_row_lists_the_trigger_as_a_candidate(client, game):
    add_card(client, game, "Consign to Memory", "alice", "hand")
    _islands(client, game, 1)
    trigger = _kitchen_finks_trigger(client, game)
    consign = find(state(client, game)["hands"]["alice"], "Consign to Memory")
    rows = client.get(
        f"/api/games/{game}/legal-actions",
        params={"player": "alice", "uid": uid_for(game, "alice"), "candidates": "true"},
    ).json()["actions"]
    (row,) = [r for r in rows if r["kind"] == "cast" and r["card_id"] == consign["id"]]
    candidates = row["targets"]["slots"][0]["candidates"]
    assert any(line.startswith(trigger["id"]) for line in candidates), candidates


def test_consign_to_memory_refuses_an_activated_ability_with_the_engines_reason(client, game):
    """Karn, the Great Creator's +1 is an activated (loyalty) ability, not a
    triggered one, so Consign to Memory cannot target it."""
    add_card(client, game, "Consign to Memory", "alice", "hand")
    islands = _islands(client, game, 1)
    add_card(client, game, "Karn, the Great Creator", "alice", "battlefield")
    karn = find(state(client, game)["battlefield"], "Karn, the Great Creator")
    activated = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": karn["id"], "ability_index": 0, "targets": []},
    )
    assert activated.json()["allowed"] is True, activated.text
    (ability,) = state(client, game)["stack"]
    assert ability["kind"] == "ability"

    refused = _cast_consign(client, game, ability["id"], islands).json()
    assert refused["allowed"] is False
    assert "which is an activated ability" in refused["reason"], refused["reason"]
    assert find(state(client, game)["hands"]["alice"], "Consign to Memory")


def test_a_colorless_spell_on_the_stack_is_a_candidate_and_a_target(client, game):
    """The spell half of the same slot: a spell on the stack is named by its
    own card id, which the engine already found; this pins it beside the
    ability half so the two stay one rule."""
    add_card(client, game, "Consign to Memory", "alice", "hand")
    add_card(client, game, "Walking Ballista", "alice", "hand")
    islands = _islands(client, game, 3)
    board = state(client, game)
    ballista = find(board["hands"]["alice"], "Walking Ballista")
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": ballista["id"],
            "x_value": 1,
            "mana_sources": islands[:2],
        },
    )
    assert cast.json()["allowed"] is True, cast.text
    countered = _cast_consign(client, game, ballista["id"], islands[2:])
    assert countered.json()["allowed"] is True, countered.text


def test_rimewind_cryomancer_activates_targeting_an_activated_ability(client, game):
    """The activate route: "{1}, {T}: Counter target activated ability."
    against Karn's +1 on the stack, paid from four Snow-Covered Forests
    (its activation restriction), and refused against a triggered one."""
    for _ in range(4):
        add_card(client, game, "Snow-Covered Forest", "alice", "battlefield")
    add_card(client, game, "Rimewind Cryomancer", "alice", "battlefield")
    add_card(client, game, "Karn, the Great Creator", "alice", "battlefield")
    board = state(client, game)
    karn = find(board["battlefield"], "Karn, the Great Creator")
    cryomancer = find(board["battlefield"], "Rimewind Cryomancer")
    forest = find(board["battlefield"], "Snow-Covered Forest")
    client.patch(f"/api/games/{game}/cards/{cryomancer['id']}", json={"summoning_sick": False})
    activated = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": karn["id"], "ability_index": 0, "targets": []},
    )
    assert activated.json()["allowed"] is True, activated.text
    (karn_ability,) = state(client, game)["stack"]

    stifle = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": cryomancer["id"],
            "ability_index": 0,
            "targets": [karn_ability["id"]],
            "mana_sources": [forest["id"]],
        },
    )
    assert stifle.json()["allowed"] is True, stifle.text
    stack = state(client, game)["stack"]
    assert stack[0]["name"] == "Rimewind Cryomancer"
    assert stack[0]["target_refs"][0]["id"] == karn_ability["id"]


def test_rimewind_cryomancer_refuses_a_triggered_ability(client, game):
    for _ in range(4):
        add_card(client, game, "Snow-Covered Forest", "alice", "battlefield")
    add_card(client, game, "Rimewind Cryomancer", "alice", "battlefield")
    trigger = _kitchen_finks_trigger(client, game)
    board = state(client, game)
    cryomancer = find(board["battlefield"], "Rimewind Cryomancer")
    forest = find(board["battlefield"], "Snow-Covered Forest")
    client.patch(f"/api/games/{game}/cards/{cryomancer['id']}", json={"summoning_sick": False})
    refused = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": cryomancer["id"],
            "ability_index": 0,
            "targets": [trigger["id"]],
            "mana_sources": [forest["id"]],
        },
    ).json()
    assert refused["allowed"] is False
    assert "which is a triggered ability" in refused["reason"], refused["reason"]


def test_an_unknown_id_names_the_stack_in_its_refusal(client, game):
    add_card(client, game, "Consign to Memory", "alice", "hand")
    islands = _islands(client, game, 1)
    _kitchen_finks_trigger(client, game)
    refused = _cast_consign(client, game, "00000000-0000-4000-8000-000000000000", islands).json()
    assert refused["allowed"] is False
    assert "ability on the stack" in refused["reason"], refused["reason"]


def test_both_front_ends_cast_consign_to_memory_at_a_trigger(client, game, monkeypatch, capsys):
    """Parity: `seat_cli cast --targets <ability id>` and the MCP `cast` tool
    both reach the route with the stack id and both are allowed."""
    wire(monkeypatch)

    add_card(client, game, "Consign to Memory", "alice", "hand")
    islands = _islands(client, game, 1)
    trigger = _kitchen_finks_trigger(client, game)
    uid = uid_for(game, "alice")
    code = seat_cli.run(
        [
            "--uid",
            uid,
            "--game",
            game,
            "cast",
            "Consign to Memory",
            "--target",
            trigger["id"],
            "--sources",
            islands[0],
            "--hold",
        ]
    )
    cli_answer = json.loads(capsys.readouterr().out)
    assert code == 0, cli_answer
    assert cli_answer["outcome"]["allowed"] is True, cli_answer

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    try:
        add_card(client, second, "Consign to Memory", "alice", "hand")
        islands2 = _islands(client, second, 1)
        trigger2 = _kitchen_finks_trigger(client, second)
        server = build_server(Seats(api="http://testserver", game_id=second))
        answer = asyncio.run(
            server.call_tool(
                "cast",
                {
                    "player_uid": uid_for(second, "alice"),
                    "card": "Consign to Memory",
                    "targets": trigger2["id"],
                    "mana_sources": islands2[0],
                    "hold_priority": True,
                },
            )
        )
        mcp_answer = json.loads(answer.content[0].text)
        assert mcp_answer["outcome"]["allowed"] is True, mcp_answer
    finally:
        client.delete(f"/api/games/{second}")
