"""The engine's tap plan (`Action.pays_with`, `machines/cost.plan_mana_payment`
in `magic-cards-model` at `4ad120c`) reaching an agent seat, not just the raw
HTTP door onto it.

The field reports which sources a cast would tap, which of each source's
mana abilities, what colour, and any CR 106.6 restriction on the mana - the
plan `auto_tap` would execute if the action were taken, so a seat can spend
its own reasoning on "this Bolt eats the Cavern of Souls I wanted for the
counterspell" instead of the mechanics of paying, which the engine already
does for it either way. It is purely informational: nothing here is a
parameter a caller fills in and hands back, and this file never sends
`mana_sources` in response to it.

Two things were unverified before this file: whether `GET
.../legal-actions` actually carries `pays_with` (it does, by construction -
`schemas.ActionWithTargets` subclasses the engine's own `Action` and passes
every field of it through untouched), and whether `mcp._compact_actions`/
`_without_empty`, which drop every empty/null key off an action entry to
keep an MCP answer small, keep a *populated* `pays_with` while still
dropping an empty one. Both already worked; this file is what pins it so
neither silently regresses.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, state
from test_cast_cost_choices import answer
from test_mcp_input_required import call_tool, wired

from magic_cards_model_api import mcp as mcp_module
from magic_cards_model_api.mcp import build_server


def hand_id(client: Any, game: str, name: str, player: str = "alice") -> str:
    return find(state(client, game)["hands"][player], name)["id"]


def test_legal_actions_endpoint_reports_the_tap_plan_for_an_affordable_cast(
    client, game, admin_key
):
    """(a): the HTTP door itself. A single untapped Mountain paying a single
    `{R}` Lightning Bolt names that Mountain, its mana ability, and what it
    adds - the plan `auto_tap` would actually run."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")

    payload = client.get(
        f"/api/games/{game}/legal-actions", params={"uid": admin_key, "player": "alice"}
    ).json()
    (cast_action,) = [a for a in payload["actions"] if a["kind"] == "cast"]
    assert cast_action["affordable"] == "yes", cast_action
    (entry,) = cast_action["pays_with"]
    assert entry["name"] == "Mountain"
    assert entry["ability_index"] == 0
    assert entry["adds"] == "R"
    assert "restriction" not in entry, entry


def test_mcp_action_answer_carries_pays_with_for_a_remaining_affordable_cast(
    client, game, monkeypatch
):
    """(b): the MCP answer path, not the HTTP endpoint alone. Alice has two
    Mountains and two Shocks; casting one Shock (kept, `hold_priority=True`,
    so the same answer already reflects the board after the cast) still
    leaves the second Shock castable off the one Mountain left untapped -
    and that remaining `cast` entry, read straight out of `available_actions`
    in the tool's own answer, carries `pays_with` naming it."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    add_card(client, game, "Shock", "alice", "hand")

    result = call_tool(
        server,
        "cast",
        {
            "player_uid": uids["alice"],
            "card": "Shock",
            "targets": "bob",
            "hold_priority": True,
            "reasoning": "burn",
        },
    )
    payload = answer(result)
    assert payload["outcome"]["allowed"] is True, payload["outcome"]

    remaining = [a for a in payload["available_actions"]["actions"] if a.get("kind") == "cast"]
    assert remaining, payload["available_actions"]
    (cast_action,) = remaining
    assert cast_action["affordable"] == "yes", cast_action
    (entry,) = cast_action["pays_with"]
    assert entry["name"] == "Mountain"
    assert entry["adds"] == "R"
    assert entry["ability_index"] == 0


def test_pays_with_restriction_survives_to_the_mcp_answer(client, game, monkeypatch):
    """The field most likely to be silently dropped: Cavern of Souls's
    CR 106.6 "spend this mana only to cast a creature spell of the chosen
    type" sentence, verbatim, on the `pays_with` entry a still-legal cast
    carries in the same MCP answer shape the other tests here use.

    `add_card` places the land directly on the battlefield rather than
    casting it, so CR 614.12a's "as this enters, choose a creature type"
    replacement never fires - the same gap `test_mana_spend_restrictions.py`
    works around in the engine's own suite. Reaching past the API into the
    live `Game` to set the choice by hand is the same legitimate idiom
    `conftest.force_turn_position` already uses for turn position.

    The board leaves Cavern and the Elves in hand untouched and instead
    reads `available_actions` off an unrelated Plains's own incidental mana
    tap (`_tap_a_mana_source`) - actually casting the Elves would put a
    sorcery-speed spell on the stack, and no *second* sorcery-speed spell
    could legally be cast while it sits there unresolved (CR 601.2b via
    CR 307.1a), which would make "nothing left to cast" a fact about timing
    rather than about this field."""
    from magic_cards_model.models.card_state import Choice, ChoiceKind

    from magic_cards_model_api.store import store

    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    add_card(client, game, "Cavern of Souls", "alice", "battlefield")
    add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Llanowar Elves", "alice", "hand")
    with store.locked(game) as g:
        cavern = next(c for c in g.battlefield if c.card.name == "Cavern of Souls")
        cavern.choice = Choice(kind=ChoiceKind.CREATURE_TYPE, value="Elf")

    payload = _tap_a_mana_source(server, uids, client, game, "Plains")
    remaining = [a for a in payload["available_actions"]["actions"] if a.get("kind") == "cast"]
    (cast_action,) = remaining
    assert cast_action["affordable"] == "yes", cast_action
    (entry,) = cast_action["pays_with"]
    assert entry["name"] == "Cavern of Souls"
    assert entry["restriction"] == (
        "Spend this mana only to cast a creature spell of the chosen type, "
        "and that spell can't be countered."
    )


def _tap_a_mana_source(
    server: Any, uids: dict[str, str], client: Any, game: str, land_name: str
) -> dict:
    """Activate `land_name`'s own mana ability (index 0 on every basic land
    added straight to the battlefield) - a mana ability never uses the stack
    (CR 605.3b) and so never gives up the acting seat's own priority
    (`seatkit.added_mana`'s own docstring), unlike every other action tool
    with `hold_priority` left at its default. That makes it the harmless
    action this file needs: one that returns a real `available_actions`
    answer without releasing alice's own priority."""
    land_id = find(state(client, game)["battlefield"], land_name)["id"]
    result = call_tool(
        server,
        "activate",
        {
            "player_uid": uids["alice"],
            "card_id": land_id,
            "ability_index": 0,
            "reasoning": "floating mana, incidental to this test",
        },
    )
    return answer(result)


def test_pays_with_absent_with_no_mana_cost(client, game, monkeypatch):
    """A land drop owes no mana at all - `mana_cost` is `None` - so there is
    nothing for a tap plan to name, and the MCP answer carries no
    `pays_with` key on that entry rather than an empty list."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "hand")

    payload = _tap_a_mana_source(server, uids, client, game, "Mountain")
    land_drops = [
        a for a in payload["available_actions"]["actions"] if a.get("kind") == "play_land"
    ]
    assert land_drops, payload["available_actions"]
    for action in land_drops:
        assert "pays_with" not in action, action


def test_pays_with_absent_when_not_affordable(client, game, monkeypatch):
    """CR 118.6 keeps an unpayable cast in the enumeration, but a plan for a
    cost that cannot be paid is a plan for something that never happens -
    `affordable != "yes"` carries no `pays_with` either, empty or not."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    # A Forest, not a Mountain: the incidental tap below must not hand alice
    # a floating red source that would make the {R} bolt affordable out from
    # under this test.
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")  # no red source at all

    payload = _tap_a_mana_source(server, uids, client, game, "Forest")
    casts = [a for a in payload["available_actions"]["actions"] if a.get("kind") == "cast"]
    (cast_action,) = casts
    assert cast_action["affordable"] == "no", cast_action
    assert "pays_with" not in cast_action, cast_action


def test_compact_actions_keeps_a_populated_pays_with_and_drops_an_empty_one():
    """Direct pin on `mcp._compact_actions`/`_without_empty`: the function
    that trims every action entry for the token budget must not treat a
    real tap plan the same as the twenty other empty/null fields it exists
    to strip."""
    populated = {
        "kind": "cast",
        "player": "alice",
        "rule": "CR 601.2",
        "affordable": "yes",
        "mana_cost": "{R}",
        "pays_with": [
            {
                "card_id": "11111111-1111-1111-1111-111111111111",
                "name": "Mountain",
                "ability_index": 0,
                "adds": "R",
            },
        ],
        "modes": [],
        "target_slots": [],
    }
    empty = {
        "kind": "pass_priority",
        "player": "alice",
        "rule": "CR 117.3d",
        "affordable": "yes",
        "pays_with": [],
        "modes": [],
    }
    compacted = mcp_module._compact_actions({"actions": [populated, empty]})
    kept, passed = compacted["actions"]
    assert kept["pays_with"] == populated["pays_with"]
    assert "pays_with" not in passed, passed
