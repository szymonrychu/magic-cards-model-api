from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

MOUNTAIN = {
    "id": "m1",
    "controller": "alice",
    "tapped": False,
    "zone": "battlefield",
    "abilities": [
        {"index": 0, "text": "{T}: Add {R}.", "is_mana_ability": True, "mana_options": None}
    ],
}
TAPPED = {**MOUNTAIN, "id": "m2", "tapped": True}
DUAL = {
    "id": "d1",
    "controller": "alice",
    "tapped": False,
    "zone": "battlefield",
    "abilities": [
        {
            "index": 0,
            "text": "{T}: Add {R} or {W}.",
            "is_mana_ability": True,
            "mana_options": [["R"], ["W"]],
        }
    ],
}
THEIRS = {**MOUNTAIN, "id": "m3", "controller": "bob"}
GRIZZLY = {
    "id": "c1",
    "controller": "alice",
    "tapped": False,
    "zone": "battlefield",
    "abilities": [],
}
STATE = {"you": "alice", "battlefield": [MOUNTAIN, TAPPED, DUAL, THEIRS, GRIZZLY]}


def test_untapped_sources_are_yours_untapped_and_mana_producing() -> None:
    assert run_module("mana.js", "m.untappedSources(state)", state=STATE) == ["m1", "d1"]


def test_pinned_sources_go_first_with_their_colour() -> None:
    result = run_module(
        "mana.js",
        "m.manaSources(m.pin([], 'd1', 'W'), m.untappedSources(state))",
        state=STATE,
    )
    assert result == [{"card_id": "d1", "colors": ["W"]}, "m1"]


def test_unpin_restores_the_bare_id() -> None:
    result = run_module(
        "mana.js", "m.manaSources(m.unpin(m.pin([], 'd1', 'W'), 'd1'), ['m1', 'd1'])"
    )
    assert result == ["m1", "d1"]


def test_colour_options_come_from_the_ability() -> None:
    assert run_module(
        "mana.js",
        "[m.colorOptions(dual, 0), m.colorOptions(mountain, 0)]",
        dual=DUAL,
        mountain=MOUNTAIN,
    ) == [["R", "W"], []]


# -- pinning a single-colour source (Task item 2) ---------------------------
#
# `affordances.py`'s `ability_hints` reports `mana_options: null` for a
# one-option mana ability (`colorOptions` then returns `[]`), so pinning that
# source must not invent a colourless "C" choice for a land that never makes
# colourless mana - it pins *which* source and leaves the colour to the
# engine's own `auto_tap`.


def test_pinning_with_no_colour_keeps_a_null_colour() -> None:
    assert run_module("mana.js", "m.pin([], 'm1', null)") == [
        {"card_id": "m1", "color": None, "ability_index": None}
    ]


def test_a_colourless_pin_leads_the_mana_sources_as_a_bare_id() -> None:
    """Unlike a coloured pin (which becomes a `{card_id, colors}` `ManaSource`
    - see `test_pinned_sources_go_first_with_their_colour`), a pin with no
    colour is sent exactly like an ordinary untapped source: a bare id, still
    first in line so the engine considers it before anything else."""
    result = run_module(
        "mana.js",
        "m.manaSources(m.pin([], 'm1', null), ['m1', 'm2'])",
    )
    assert result == ["m1", "m2"]


# -- choosing WHICH mana ability (API2) -------------------------------------
#
# A permanent can have more than one mana ability, and they are not
# interchangeable: Eldrazi Temple taps for {C} (index 0) or for {C}{C} that
# may only be spent on Eldrazi (index 1). Pinning it has to be able to name
# the second one, so the index the player picked reaches the engine as
# `ManaSource.ability_index` rather than being thrown away.

ELDRAZI_TEMPLE = {
    "id": "et1",
    "name": "Eldrazi Temple",
    "controller": "alice",
    "tapped": False,
    "zone": "battlefield",
    "abilities": [
        {
            "index": 0,
            "text": "{T}: Add {C}.",
            "cost": "{T}",
            "is_mana_ability": True,
            "mana_options": None,
        },
        {
            "index": 1,
            "text": "{T}: Add {C}{C}. Spend this mana only to cast Eldrazi spells.",
            "cost": "{T}",
            "is_mana_ability": True,
            "mana_options": None,
        },
    ],
}

# A permanent whose printed activated ability comes first and whose mana
# ability is not at position 0 - the reason `abilityMenuItems` already keys
# on `ability.index` rather than on where the entry happens to sit.
FILTER_LAND = {
    "id": "f1",
    "name": "Cascade Bluffs",
    "controller": "alice",
    "tapped": False,
    "zone": "battlefield",
    "abilities": [
        {"index": 0, "text": "{T}: Add {C}.", "is_mana_ability": True, "mana_options": None},
        {
            "index": 1,
            "text": "{U/R}, {T}: Add {U}{U}, {U}{R}, or {R}{R}.",
            "is_mana_ability": True,
            "mana_options": [["U"], ["R"]],
        },
    ],
}


def test_mana_abilities_lists_every_mana_ability_with_its_own_index() -> None:
    result = run_module(
        "mana.js",
        "m.manaAbilities(temple).map((a) => [a.index, a.text])",
        temple=ELDRAZI_TEMPLE,
    )
    assert result == [
        [0, "{T}: Add {C}."],
        [1, "{T}: Add {C}{C}. Spend this mana only to cast Eldrazi spells."],
    ]


def test_mana_abilities_skips_abilities_that_make_no_mana() -> None:
    creature = {
        "id": "c9",
        "abilities": [
            {"index": 0, "text": "{2}: Draw a card.", "is_mana_ability": False},
            {"index": 1, "text": "{T}: Add {G}.", "is_mana_ability": True},
        ],
    }
    assert run_module("mana.js", "m.manaAbilities(card).map((a) => a.index)", card=creature) == [1]
    assert run_module("mana.js", "m.manaAbilities(card)", card=GRIZZLY) == []


def test_colour_options_are_looked_up_by_the_abilitys_own_index() -> None:
    """`list_abilities` stamps each entry with its own `index`, and a menu
    keyed by that index (`abilityMenuItems`, and now the mana-ability picker)
    hands it straight back - so `colorOptions` has to resolve it as an index,
    not as a position in whatever subset the caller happens to hold."""
    shifted = {
        "id": "s1",
        "abilities": [
            {
                "index": 3,
                "text": "{T}: Add {R} or {W}.",
                "is_mana_ability": True,
                "mana_options": [["R"], ["W"]],
            },
        ],
    }
    assert run_module("mana.js", "m.colorOptions(card, 3)", card=shifted) == ["R", "W"]


def test_a_pin_naming_an_ability_carries_the_index_to_the_wire() -> None:
    """Eldrazi Temple's restricted {C}{C} is index 1. Pinning it names both
    the source and which of its abilities to tap, as
    `ManaSource.ability_index`."""
    result = run_module(
        "mana.js",
        "m.manaSources(m.pin([], 'et1', null, 1), ['et1', 'm1'])",
    )
    assert result == [{"card_id": "et1", "ability_index": 1}, "m1"]


def test_a_pin_naming_both_an_ability_and_a_colour_carries_both() -> None:
    result = run_module(
        "mana.js",
        "m.manaSources(m.pin([], 'f1', 'R', 1), ['f1'])",
    )
    assert result == [{"card_id": "f1", "colors": ["R"], "ability_index": 1}]


def test_an_unnamed_ability_still_serializes_exactly_as_before() -> None:
    """The regression guard for the common case: a source pinned without
    naming an ability (every basic land) goes out in the shape it always did
    - a bare id, or `{card_id, colors}` when a colour was chosen - so the
    engine's own `auto_tap` keeps solving it."""
    result = run_module(
        "mana.js",
        "[m.manaSources(m.pin([], 'm1', null), ['m1', 'm2']),"
        " m.manaSources(m.pin([], 'd1', 'W'), ['d1'])]",
    )
    assert result == [["m1", "m2"], [{"card_id": "d1", "colors": ["W"]}]]


def test_pinning_the_same_source_again_replaces_its_ability_choice() -> None:
    result = run_module("mana.js", "m.pin(m.pin([], 'et1', null, 0), 'et1', null, 1)")
    assert result == [{"card_id": "et1", "color": None, "ability_index": 1}]


ANY_ONE_COLOR = {
    "id": "sc",
    "controller": "alice",
    "tapped": False,
    "zone": "battlefield",
    "abilities": [
        {
            "index": 0,
            "text": "{T}: Add two mana of any one color.",
            "is_mana_ability": True,
            "mana_options": ["W", "U", "B", "R", "G"],
        }
    ],
}


def test_a_pin_names_the_chosen_colour_once_however_much_mana_it_buys():
    """ "Add two mana of any one color" is one choice worth two mana, and the
    pin carries the choice: `{card_id, colors: ["R"]}`, exactly the shape a
    Mountain's pin has.

    Not `["R", "R"]`. How much mana a chosen colour buys is the ability's own
    business and is nowhere on `AbilityView` for the browser to read - the
    server fills it in from the ability itself
    (`engine_api._expanded_to_the_abilitys_own_count`), which is what makes
    this work for the MCP and CLI seats too rather than only for this one.
    Repeating the colour here would be the browser guessing at a count it
    cannot see.
    """
    pinned = run_module("mana.js", "m.pin([], card.id, 'R', null)", card=ANY_ONE_COLOR)
    assert pinned == [{"card_id": "sc", "color": "R", "ability_index": None}]
    assert run_module("mana.js", "m.manaSources(pinned, [])", pinned=pinned) == [
        {"card_id": "sc", "colors": ["R"]}
    ]
