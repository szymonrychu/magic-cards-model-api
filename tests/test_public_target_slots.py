"""Every target slot a row shows is the engine's own, candidates included.

Engine M1e: `machines.legal_actions` builds `TargetSlot(phrase, minimum,
maximum, candidates, player_candidates)` for every cast and every activation
- battlefield, loyalty, equip (its own `ACTIVATE` row, `keyword: Equip`) and
off-zone - from the public `Game.announcement_profile`. CR 602.2b applies CR
601.2c's target announcement to an activation, and the engine now announces
and checks those targets before the cost is paid (CR 601.2h), so a candidate
it lists is one it accepts.

The api used to fill activation slots itself (`_Rows.with_target_slots`,
reading the private `Game._announcement_profile`) and to build a second equip
row with the Equipment standing in as its own target (`_Rows.equip_rows`).
Both were the api re-deriving what the engine reports; this pins that it no
longer does. Every CR number here was read with `mise run rules:find` in the
session that wrote this file (601.2c, 601.2h, 602.2b, 608.2b, 702.6a).
"""

from __future__ import annotations

from pathlib import Path

from conftest import add_card, find, state
from test_legal_actions_endpoint import legal_actions

from magic_cards_model_api import action_rows
from magic_cards_model_api.app import game_or_404


def rows_for(client, game, card_id: str) -> list[dict]:
    return [
        row
        for row in legal_actions(client, game, candidates="true")
        if row.get("card_id") == card_id
    ]


def test_a_row_slot_carries_the_engines_candidates_and_player_candidates(client, game):
    add_card(client, game, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Llanowar Elves", "bob", "battlefield")
    board = state(client, game)
    bombardment = find(board["battlefield"], "Goblin Bombardment")["id"]
    elves = find(board["battlefield"], "Llanowar Elves")["id"]

    (row,) = rows_for(client, game, bombardment)

    (slot,) = row["target_slots"]
    assert elves in slot["candidates"], slot
    assert set(slot["player_candidates"]) == {"alice", "bob"}, slot


def test_hexmage_is_its_own_candidate_and_the_activation_accepts_it(client, game):
    """Vampire Hexmage: "Sacrifice this creature: Remove all counters from
    target permanent." Announced before the sacrifice is paid, it may name
    itself; CR 608.2b then finds it gone and the ability does not resolve."""
    add_card(client, game, "Vampire Hexmage", "alice", "battlefield")
    hexmage = find(state(client, game)["battlefield"], "Vampire Hexmage")["id"]

    (row,) = rows_for(client, game, hexmage)
    (slot,) = row["target_slots"]
    assert hexmage in slot["candidates"], slot
    assert any(line.startswith(hexmage) for line in row["targets"]["slots"][0]["candidates"])

    answer = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": hexmage, "ability_index": 0, "targets": [hexmage]},
    ).json()
    assert answer["allowed"] is True, answer

    for player in ("alice", "bob"):
        passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": player})
        assert passed.json()["allowed"] is True, passed.json()
    assert answer["ability_id"] in {str(fizzled) for fizzled in game_or_404(game).fizzled}


def test_an_illegal_activation_target_is_refused_before_the_cost_is_paid(client, game):
    add_card(client, game, "Vampire Hexmage", "alice", "battlefield")
    hexmage = find(state(client, game)["battlefield"], "Vampire Hexmage")["id"]

    answer = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": hexmage, "ability_index": 0, "targets": ["bob"]},
    ).json()

    assert answer["allowed"] is False, answer
    assert "target permanent" in answer["reason"], answer
    assert find(state(client, game)["battlefield"], "Vampire Hexmage")["id"] == hexmage


def test_each_equipment_has_one_priced_equip_row_with_the_engines_candidates(client, game):
    add_card(client, game, "Blade of the Bloodchief", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Watchwolf", "bob", "battlefield")
    board = state(client, game)
    blade = find(board["battlefield"], "Blade of the Bloodchief")["id"]
    bears = find(board["battlefield"], "Grizzly Bears")["id"]

    (row,) = rows_for(client, game, blade)

    assert row["kind"] == "equip", row
    assert row["keyword"] == "Equip", row
    assert row["affordable"] == "yes", row
    (slot,) = row["target_slots"]
    assert slot["candidates"] == [bears], slot
    assert slot["player_candidates"] == [], slot
    (listed,) = row["targets"]["slots"]
    assert [line.split(" ", 1)[0] for line in listed["candidates"]] == [bears], listed


def test_the_api_builds_no_target_slot_of_its_own():
    source = Path(action_rows.__file__).parent
    for path in source.glob("*.py"):
        assert "_announcement_profile" not in path.read_text(), path.name
    assert not hasattr(action_rows._Rows, "with_target_slots")
    assert not hasattr(action_rows._Rows, "equip_rows")
    assert "TargetSlot(" not in Path(action_rows.__file__).read_text()


def test_the_seat_and_mcp_renders_keep_the_slot_candidates(client, game):
    """What a seat reads (`seatkit._actions_view`) and what the MCP answer
    compacts it to (`mcp._compact_actions`) both keep each slot's engine
    candidates: compaction drops empty keys of an entry, never inside a slot."""
    from magic_cards_model_api import mcp, seatkit

    add_card(client, game, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Llanowar Elves", "bob", "battlefield")
    board = state(client, game)
    bombardment = find(board["battlefield"], "Goblin Bombardment")["id"]
    elves = find(board["battlefield"], "Llanowar Elves")["id"]

    view = seatkit._actions_view(board, legal_actions(client, game, candidates="true"), None)
    for rendered in (view, mcp._compact_actions(view)):
        (row,) = [row for row in rendered["actions"] if row.get("card_id") == bombardment]
        (slot,) = row["target_slots"]
        assert elves in slot["candidates"], slot
        assert set(slot["player_candidates"]) == {"alice", "bob"}, slot
