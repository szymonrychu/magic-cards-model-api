"""Can a player plan around a mana source the auto-tapper will not spend
without consent, purely from the state view - no `resolve_cards` lookup?

Nurturing Peatland's only mana ability is `{T}, Pay 1 life: Add {B} or {G}.`
- a cost beyond a bare `{T}`, so `auto_tap` (engine `machines/cost.py`) will
never spend it without the player naming it explicitly (see the engine's
`tests/scenarios/test_auto_tap.py`, which pins that refusal and is not
touched here). The open question was whether the seat-facing state exposes
the life cost at all, so a player can *choose* to name that source, or has to
go outside the API (`resolve_cards`) to find out it exists.

It does: `list_abilities` (`engine_api.py`) reads the ability straight off
`Game.activated_abilities`, whose `.text` is CR 602.1's whole printed line -
cost and all, not just the mana it produces - and `_card_view`
(`serialize.py`) puts that on every battlefield card's `abilities` list.
Battlefield is a public zone, so `redact_view` (`visibility.py`) does not
touch it: the opponent sees the identical `abilities` entry a player sees on
their own permanent. These tests pin that finding for both the admin/owner
view and a seat that does not control the card.
"""

from conftest import add_card, find, state


def test_nurturing_peatlands_life_cost_is_visible_on_the_battlefield(client, game):
    add_card(client, game, "Nurturing Peatland", "alice", "battlefield")
    peatland = find(state(client, game)["battlefield"], "Nurturing Peatland")

    assert peatland["abilities"] == [
        {"index": 0, "text": "{T}, Pay 1 life: Add {B} or {G}.", "mana": True},
        {"index": 1, "text": "{1}, {T}, Sacrifice this land: Draw a card.", "mana": False},
    ]
    # The full printed line is there too, life cost included, so a player
    # need not call `resolve_cards` to learn what the source will cost.
    assert "Pay 1 life" in peatland["oracle_text"]


def test_the_life_cost_is_visible_to_a_seat_that_does_not_control_it(client, game):
    """Battlefield is a public zone (CR 400.2): the opponent gets the same
    `abilities` entry a player sees on their own permanent, not a reduced
    view - so an opponent can plan around it exactly as its controller can."""
    add_card(client, game, "Nurturing Peatland", "alice", "battlefield")
    peatland_for_alice = find(state(client, game, uid="alice")["battlefield"], "Nurturing Peatland")
    peatland_for_bob = find(state(client, game, uid="bob")["battlefield"], "Nurturing Peatland")

    expected = [
        {"index": 0, "text": "{T}, Pay 1 life: Add {B} or {G}.", "mana": True},
        {"index": 1, "text": "{1}, {T}, Sacrifice this land: Draw a card.", "mana": False},
    ]
    assert peatland_for_alice["abilities"] == expected
    assert peatland_for_bob["abilities"] == expected
