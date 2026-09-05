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
    assert run_module("mana.js", "m.pin([], 'm1', null)") == [{"card_id": "m1", "color": None}]


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
