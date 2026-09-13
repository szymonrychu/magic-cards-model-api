"""An activated ability's action row says what it targets.

**The defect (battle 5, B5-10).** Goblin Bombardment's `legal-actions` row read
`kind activate, cost "Sacrifice a creature", target_slots: []` - yet the
ability is "Sacrifice a creature: This enchantment deals 1 damage to any
target", and a targeted activation was accepted. `target_slots: []` is what a
row says for an ability that targets nothing, so a seat reading the row could
not learn it had to name a target, and got no candidates to name one from.

CR 602.2b applies CR 601.2c to activating an ability: the targets are
announced as part of it, so a row describing the activation owes the same
slots a cast row carries. The engine's `legal_actions` builds a cast row's
`target_slots` from `Game._announcement_profile` and builds none at all for an
activation; `action_rows.legal_action_rows` fills them from that same call.
The fix targets the shape, not the card: Vampire Hexmage ("Sacrifice this
creature: Remove all counters from target permanent.") is in no deck pool and
proves it on a second sacrifice-cost ability, and Faerie Macabre's "Discard
this card" proves it off the battlefield (CR 113.6j's hand ability). Every CR
number here was looked up with `mise run rules:find` in the session that
wrote this file.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, state
from test_legal_actions_endpoint import legal_actions


def row_for(rows: list[dict[str, Any]], card_id: str, index: int = 0) -> dict[str, Any]:
    matches = [
        row
        for row in rows
        if row.get("card_id") == card_id
        and row.get("ability_index") == index
        and str(row.get("kind", "")).startswith("activate")
    ]
    assert len(matches) == 1, f"one activation row for {card_id}/{index}: {rows}"
    return matches[0]


def candidates_of(row: dict[str, Any], slot: int = 0) -> list[str]:
    targets = row.get("targets")
    assert targets is not None, f"a targeting row carries its candidates: {row}"
    assert targets["understood"] is True, targets
    return targets["slots"][slot]["candidates"]


def test_goblin_bombardment_row_lists_its_any_target_slot(client, game) -> None:
    add_card(client, game, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Llanowar Elves", "bob", "battlefield")
    board = state(client, game)
    bombardment = find(board["battlefield"], "Goblin Bombardment")["id"]
    elves = find(board["battlefield"], "Llanowar Elves")["id"]

    row = row_for(legal_actions(client, game, candidates="true"), bombardment)

    assert row["cost"] and "Sacrifice" in row["cost"], row
    assert len(row["target_slots"]) == 1, row
    slot = row["target_slots"][0]
    assert "any target" in slot["phrase"], slot
    assert (slot["minimum"], slot["maximum"]) == (1, 1), slot
    candidates = candidates_of(row)
    assert "bob (player)" in candidates, candidates
    assert any(line.startswith(elves) for line in candidates), candidates


def test_an_off_pool_sacrifice_ability_lists_its_target_permanent_slot(client, game) -> None:
    """Vampire Hexmage, in no deck pool: the same "Sacrifice ...: ... target"
    shape on a different card and a different target phrase."""
    add_card(client, game, "Vampire Hexmage", "alice", "battlefield")
    add_card(client, game, "Llanowar Elves", "bob", "battlefield")
    board = state(client, game)
    hexmage = find(board["battlefield"], "Vampire Hexmage")["id"]
    elves = find(board["battlefield"], "Llanowar Elves")["id"]

    row = row_for(legal_actions(client, game, candidates="true"), hexmage)

    assert len(row["target_slots"]) == 1, row
    assert "target permanent" in row["target_slots"][0]["phrase"], row
    candidates = candidates_of(row)
    assert any(line.startswith(elves) for line in candidates), candidates
    assert all("(player)" not in line for line in candidates), candidates


def test_an_off_zone_object_cost_ability_lists_its_target_slot(client, game) -> None:
    """Faerie Macabre's "Discard this card: Exile up to two target cards from
    graveyards." - an object cost paid from the hand (CR 113.6j), with CR
    115.6's "up to two" bounds on the slot."""
    add_card(client, game, "Faerie Macabre", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "graveyard")
    board = state(client, game)
    macabre = board["hands"]["alice"][0]["id"]
    bears = find(board["graveyards"]["bob"], "Grizzly Bears")["id"]

    row = row_for(legal_actions(client, game, candidates="true"), macabre)

    assert len(row["target_slots"]) == 1, row
    slot = row["target_slots"][0]
    assert (slot["minimum"], slot["maximum"]) == (0, 2), slot
    candidates = candidates_of(row)
    assert any(line.startswith(bears) for line in candidates), candidates


def test_an_activation_that_targets_nothing_keeps_no_slots(client, game) -> None:
    """Claws of Gix ("{1}, Sacrifice a permanent: You gain 1 life.") - an
    object cost with no target keeps `target_slots: []`, so the empty list
    still means what it says."""
    add_card(client, game, "Claws of Gix", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    board = state(client, game)
    claws = find(board["battlefield"], "Claws of Gix")["id"]

    row = row_for(legal_actions(client, game, candidates="true"), claws)

    assert row["target_slots"] == [], row
    assert "targets" not in row, row
