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
