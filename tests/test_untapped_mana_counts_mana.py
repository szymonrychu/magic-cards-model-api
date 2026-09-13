"""`untapped_mana` counts mana, not sources (battle 2, B2-08).

Battle 2 read `untapped_mana` as 4 with Urza's Tower, Urza's Mine and two
`{1}, {T}` filters untapped, when the lands alone made 5 and the filters add
nothing net. The condition used `len(seatkit.untapped_mana_sources)`.

The counting rule (MEMORY.md, the play-script format doc):

- the sources are `seatkit.untapped_mana_sources`: untapped permanents the
  viewer controls with at least one mana ability;
- each source is activated at most once, for one of its mana abilities, and
  each ability adds its view's `mana_amount` - the engine's board-resolved
  count (CR 605.1a's "add X instead" clauses, so Tron's Tower adds 3), 0 when
  CR 602.5 forbids activating it now (CR 302.6's summoning sickness) or its
  type is undefined (CR 106.5);
- restricted mana is not counted (battle 6, B6-07): an ability whose view
  carries a `restriction` (the engine's `spend_restriction` over its own
  effect text, CR 106.6's "restrict how that mana can be spent") adds nothing,
  understood or not. The condition names no spell, so only mana spendable on
  anything is mana a play's move can surely spend. Eldrazi Temple's plain
  `{T}: Add {C}` still counts 1;
- an ability whose cost includes mana (`mana_cost_value` > 0, a filter)
  consumes that much of the mana already made: the count is the largest pool
  reachable, activating filters in ascending cost order, each only when the
  pool already holds its cost. Filter cost colours are not judged.

Rule texts for CR 105.2c, 106.1b, 106.6, 118.3c, 302.6 and 605.1a were read from
the Comprehensive Rules file in the engine tree in the session that wrote this.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, state
from playscript_testkit import card, play, state_view

from magic_cards_model_api.playscripts.conditions import evaluate
from magic_cards_model_api.store import store

MANA = {"kind": "untapped_mana", "op": ">=", "n": 99}


def _uid(game: str, player: str) -> str:
    return next(uid for uid, who in store.seat_uids(game).items() if who == player)


def _seen(view: dict[str, Any], viewer: str = "alice") -> Any:
    result = evaluate(play(true=[MANA], false=[]), view, viewer)
    assert result["applies"] is False and result["failing_list"] == "true"
    return result["seen"]


def _board(client, game: str, *cards: str, player: str = "alice") -> dict[str, Any]:
    for name in cards:
        add_card(client, game, name, player, "battlefield")
    return state(client, game, _uid(game, player))


def _abilities(view: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return find(view["battlefield"], name)["abilities"]


# -- the view: what each mana ability adds right now, and the mana it costs ---------


def test_a_tron_land_view_says_it_adds_three_once_tron_is_assembled(client, game):
    partial = _board(client, game, "Urza's Tower", "Urza's Mine")
    (tower,) = _abilities(partial, "Urza's Tower")
    assert (tower["mana_amount"], tower["mana_cost_value"]) == (1, 0)

    assembled = _board(client, game, "Urza's Power Plant")
    (tower,) = _abilities(assembled, "Urza's Tower")
    (mine,) = _abilities(assembled, "Urza's Mine")
    (plant,) = _abilities(assembled, "Urza's Power Plant")
    assert (tower["mana_amount"], mine["mana_amount"], plant["mana_amount"]) == (3, 2, 2)


def test_a_filter_view_says_what_it_adds_and_the_mana_it_consumes(client, game):
    view = _board(client, game, "Mystic Gate", "Captivating Cave")
    plain, filtered = _abilities(view, "Mystic Gate")
    assert (plain["mana_amount"], plain["mana_cost_value"]) == (1, 0)
    assert (filtered["mana_amount"], filtered["mana_cost_value"]) == (2, 1)
    cave = _abilities(view, "Captivating Cave")
    assert [(a["mana_amount"], a["mana_cost_value"]) for a in cave if a["mana"]] == [
        (1, 0),
        (1, 1),
    ]
    non_mana = [a for a in cave if not a["mana"]]
    assert non_mana and all(
        a["mana_amount"] is None and a["mana_cost_value"] is None for a in non_mana
    )


def test_a_restricted_ability_view_carries_its_full_amount(client, game):
    view = _board(client, game, "Eldrazi Temple")
    plain, restricted = _abilities(view, "Eldrazi Temple")
    assert (plain["mana_amount"], plain["restriction"]) == (1, None)
    assert restricted["mana_amount"] == 2 and restricted["restriction"]["understood"] is True


def test_a_summoning_sick_creatures_mana_ability_adds_nothing_yet(client, game):
    view = _board(client, game, "Llanowar Elves")
    (elves,) = _abilities(view, "Llanowar Elves")
    assert elves["mana_amount"] == 0, "CR 302.6: {T} of a creature not controlled since turn start"
    assert _seen(view) == 0

    live = store.get(game)
    next(c for c in live.battlefield if c.card.name == "Llanowar Elves").summoning_sick = False
    view = state(client, game, _uid(game, "alice"))
    (elves,) = _abilities(view, "Llanowar Elves")
    assert elves["mana_amount"] == 1
    assert _seen(view) == 1


# -- the condition over real boards -------------------------------------------------


def test_tron_assembled_counts_seven_mana_from_three_lands(client, game):
    assert _seen(_board(client, game, "Urza's Tower", "Urza's Mine")) == 2
    assert _seen(_board(client, game, "Urza's Power Plant")) == 7


def test_tron_and_eldrazi_temple_count_only_the_temples_unrestricted_one(client, game):
    view = _board(client, game, "Urza's Tower", "Urza's Mine", "Urza's Power Plant")
    assert _seen(view) == 7
    assert _seen(_board(client, game, "Eldrazi Temple")) == 8


def test_eldrazi_temple_and_a_basic_count_two_not_three(client, game):
    view = _board(client, game, "Eldrazi Temple", "Island")
    temple = _abilities(view, "Eldrazi Temple")
    assert [a["restriction"] is not None for a in temple] == [False, True]
    assert _seen(view) == 2, "Temple's {C} plus Island's {U}; the restricted {C}{C} is not counted"


def test_an_off_pool_restricted_source_counts_only_its_free_mana(client, game):
    view = _board(client, game, "Tablet of Discovery")
    plain, restricted = _abilities(view, "Tablet of Discovery")
    assert (plain["mana_amount"], plain["restriction"]) == (1, None)
    assert restricted["mana_amount"] == 2 and restricted["restriction"]["understood"] is True
    assert _seen(view) == 1, "{R}{R} spendable only on instants and sorceries is not counted"
    assert _seen(_board(client, game, "Island")) == 2


def test_a_filter_land_is_not_double_counted(client, game):
    assert _seen(_board(client, game, "Mystic Gate")) == 1, "alone it taps for {C}"
    assert _seen(_board(client, game, "Island")) == 2, "Island's mana through the Gate makes 2"


def test_two_filter_lands_chain_through_one_island(client, game):
    assert _seen(_board(client, game, "Island", "Mystic Gate", "Mystic Gate")) == 3


def test_an_any_color_filter_adds_nothing_net(client, game):
    assert _seen(_board(client, game, "Captivating Cave", "Island")) == 2


def test_tron_feeding_a_filter_land(client, game):
    view = _board(client, game, "Urza's Tower", "Urza's Mine", "Urza's Power Plant", "Mystic Gate")
    assert _seen(view) == 8


def test_tapped_sources_and_the_opponents_sources_are_not_counted(client, game):
    add_card(client, game, "Urza's Tower", "alice", "battlefield", tapped=True)
    add_card(client, game, "Eldrazi Temple", "bob", "battlefield")
    view = _board(client, game, "Island")
    assert _seen(view) == 1
    assert _seen(state(client, game, _uid(game, "bob")), "bob") == 1, "the Temple's plain {C}"


# -- the arithmetic on a hand-built view: filters need their cost in the pool ----------


def _mana(text: str, amount: int, cost: int) -> dict[str, Any]:
    return {
        "index": 0,
        "text": text,
        "mana": True,
        "cost": text.split(":")[0],
        "needs_x": False,
        "is_mana_ability": True,
        "mana_amount": amount,
        "mana_cost_value": cost,
        "targeting": {"targets_anything": False, "understood": True, "requirements": []},
    }


def _source(*abilities: dict[str, Any], **kw: Any) -> dict[str, Any]:
    built = card(kw.pop("name", "Source"), type_line="Artifact", **kw)
    built["abilities"] = list(abilities)
    return built


PLAIN = _mana("{T}: Add {C}.", 1, 0)
BIG_FILTER = _mana("{2}, {T}: Add {C}{C}{C}.", 3, 2)


def test_a_filter_whose_cost_the_pool_cannot_hold_is_not_activated():
    assert _seen(state_view(battlefield=[_source(PLAIN), _source(BIG_FILTER)])) == 1


def test_a_filter_whose_cost_the_pool_holds_nets_its_gain():
    view = state_view(battlefield=[_source(PLAIN), _source(PLAIN), _source(BIG_FILTER)])
    assert _seen(view) == 3


def test_a_cheap_filter_can_raise_the_pool_for_a_dearer_one():
    small = _mana("{1}, {T}: Add {C}{C}.", 2, 1)
    view = state_view(battlefield=[_source(PLAIN), _source(small), _source(BIG_FILTER)])
    assert _seen(view) == 3, "1, then 1-1+2 = 2, then 2-2+3 = 3"


def test_a_source_with_a_free_and_a_filter_ability_picks_the_better_one():
    gate = _source(PLAIN, _mana("{1}, {T}: Add {C}{C}.", 2, 1))
    assert _seen(state_view(battlefield=[gate])) == 1
    assert _seen(state_view(battlefield=[_source(PLAIN), gate])) == 2


def test_a_mana_ability_that_adds_nothing_now_contributes_nothing():
    assert _seen(state_view(battlefield=[_source(_mana("{T}: Add {G}.", 0, 0))])) == 0


def test_a_tapped_source_contributes_nothing():
    assert _seen(state_view(battlefield=[_source(PLAIN, tapped=True), _source(PLAIN)])) == 1


def _restricted(ability: dict[str, Any], *, understood: bool = True) -> dict[str, Any]:
    return {
        **ability,
        "restriction": {
            "text": "Spend this mana only to cast artifact spells.",
            "understood": understood,
        },
    }


def test_restricted_free_mana_is_not_counted_whatever_the_source_is_called():
    restricted = _source(_restricted(_mana("{T}: Add {C}{C}.", 2, 0)))
    assert _seen(state_view(battlefield=[restricted])) == 0
    assert _seen(state_view(battlefield=[_source(PLAIN), restricted])) == 1
    named = _source(_mana("{T}: Add {C}{C}.", 2, 0), name="Eldrazi Temple")
    assert _seen(state_view(battlefield=[named])) == 2, "the view's restriction decides, not a name"


def test_an_unread_restriction_is_not_counted_either():
    unread = _source(_restricted(_mana("{T}: Add {C}{C}.", 2, 0), understood=False))
    assert _seen(state_view(battlefield=[_source(PLAIN), unread])) == 1


def test_a_source_with_restricted_and_free_abilities_counts_its_free_one():
    temple = _source(PLAIN, _restricted(_mana("{T}: Add {C}{C}.", 2, 0)))
    assert _seen(state_view(battlefield=[temple])) == 1


def test_a_restricted_filter_is_never_activated():
    filt = _source(_restricted(_mana("{1}, {T}: Add {C}{C}{C}.", 3, 1)))
    assert _seen(state_view(battlefield=[_source(PLAIN), _source(PLAIN), filt])) == 2


def test_a_wide_board_is_counted_without_enumerating_every_land():
    lands = [_source(PLAIN) for _ in range(40)]
    gates = [_source(PLAIN, _mana("{1}, {T}: Add {C}{C}{C}.", 3, 1)) for _ in range(4)]
    assert _seen(state_view(battlefield=lands + gates)) == 40 + 4 * 2
