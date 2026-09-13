"""B7-13 (battle-7): a spell cast for X, sitting on the stack, must show the
chosen X and a mana value computed with it.

CR 202.3e ("When calculating the mana value of an object with an {X} in its
mana cost, X is treated as 0 while the object is not on the stack, and X is
treated as the number chosen for it while the object is on the stack",
verified with `mise run rules:find 202.3e` in the session that wrote this
file) is the rule; `CardState.mana_value` only ever gives CR 107.3g's off-
stack "X is 0" reading, since it has no notion of the stack at all. The
reported repro (battle-7, Nature's Rhythm harmonized for X=8) showed
`mana_value 2.0` and no X field at all while the spell sat on the stack.

Both cards here are real Scryfall printings already in the engine's fixture
pool (`../magic-cards-model/src/magic_cards_model/data/printings/`), reached
through `conftest.add_card`/`scryfall.load_pooled` - no mocks."""

from __future__ import annotations

from conftest import add_card, find, state


def test_x_spell_on_the_stack_shows_chosen_x_and_mana_value(client, game):
    """Postmortem Lunge, {X}{B/P}, printed mana value 1 (X reads as 0 off
    the stack, CR 107.3g/CR 202.3a). Cast for X=2, its stack view must show
    `x_value: 2` and `mana_value: 3` (CR 202.3e: 1 + one `{X}` at 2)."""
    add_card(client, game, "Postmortem Lunge", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "alice", "graveyard")
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Swamp", "alice", "battlefield")
    board = state(client, game)
    lunge = find(board["hands"]["alice"], "Postmortem Lunge")
    bears = find(board["graveyards"]["alice"], "Grizzly Bears")
    swamps = [c["id"] for c in board["battlefield"] if c["name"] == "Swamp"]
    assert lunge["mana_value"] == 1.0, "off the stack X is 0 (CR 107.3g)"

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": lunge["id"],
            "x_value": 2,
            "targets": [bears["id"]],
            "mana_sources": swamps,
        },
    )
    assert cast.json()["allowed"] is True, cast.text

    on_stack = state(client, game)["stack"][0]
    assert on_stack["name"] == "Postmortem Lunge"
    assert on_stack["x_value"] == 2
    assert on_stack["mana_value"] == 3.0


def test_a_spell_with_two_x_symbols_doubles_the_chosen_x(client, game):
    """Walking Ballista, `{X}{X}`, printed mana value 0. CR 107.3i: "all
    instances of X on an object have the same value at any given time" - so
    cast for X=3, its two `{X}` symbols are each worth 3, not one shared 3
    between them; on the stack that is mana value 6, not 3."""
    add_card(client, game, "Walking Ballista", "alice", "hand")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    board = state(client, game)
    ballista = find(board["hands"]["alice"], "Walking Ballista")
    forests = [c["id"] for c in board["battlefield"] if c["name"] == "Forest"]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": ballista["id"],
            "x_value": 3,
            "mana_sources": forests,
        },
    )
    assert cast.json()["allowed"] is True, cast.text

    on_stack = state(client, game)["stack"][0]
    assert on_stack["name"] == "Walking Ballista"
    assert on_stack["x_value"] == 3
    assert on_stack["mana_value"] == 6.0


def test_a_spell_with_no_x_carries_no_x_value(client, game):
    """A spell whose cost has no `{X}` at all never gets an `x_value` field -
    absence, not a stray `0`, says "this was never announced" (`mana_cost`
    already answers whether an `{X}` exists to announce)."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Mountain", "alice", "battlefield")
    board = state(client, game)
    bolt = find(board["hands"]["alice"], "Lightning Bolt")
    mountain = find(board["battlefield"], "Mountain")

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt["id"],
            "targets": ["bob"],
            "mana_sources": [{"card_id": mountain["id"]}],
        },
    )
    assert cast.json()["allowed"] is True, cast.text

    on_stack = state(client, game)["stack"][0]
    assert on_stack["name"] == "Lightning Bolt"
    assert "x_value" not in on_stack
    assert on_stack["mana_value"] == 1.0
