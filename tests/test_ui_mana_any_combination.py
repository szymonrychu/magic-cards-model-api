"""The play UI offering the split of "Add N mana in any combination of
<colours>" (CR 106.1a): `AbilityView.mana_choice_count` colours out of
`mana_options`, one per mana, as a pin's `ManaSource.colors` or an
activation's `colors`."""

from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

VIVI = {
    "id": "v1",
    "controller": "alice",
    "tapped": False,
    "zone": "battlefield",
    "abilities": [
        {
            "index": 0,
            "text": "{0}: Add X mana in any combination of {U} and/or {R}, ...",
            "is_mana_ability": True,
            "mana_options": ["R", "U"],
            "mana_choice_count": 2,
        }
    ],
}
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
            "mana_options": ["R", "W"],
            "mana_choice_count": 1,
        }
    ],
}


def test_a_mixture_offers_every_split() -> None:
    assert run_module("mana.js", "m.colorChoices(vivi, 0)", vivi=VIVI) == ["R,R", "R,U", "U,U"]


def test_a_single_choice_offers_the_colours() -> None:
    assert run_module("mana.js", "m.colorChoices(dual, 0)", dual=DUAL) == ["R", "W"]


def test_a_pinned_split_goes_out_one_colour_per_mana() -> None:
    assert run_module("mana.js", "m.manaSources(m.pin([], 'v1', 'R,U', 0), [])") == [
        {"card_id": "v1", "colors": ["R", "U"], "ability_index": 0}
    ]


def test_an_activation_with_a_split_sends_colors() -> None:
    body = run_module(
        "actions.js",
        "m.bodyFor({kind: 'activate', cardId: 'v1', abilityIndex: 0, color: 'U,R', "
        "pinned: []}, 'alice', [])",
    )
    assert body["colors"] == ["U", "R"], body
    assert "color" not in body, body


def test_a_split_is_labelled_by_its_symbols() -> None:
    assert run_module("menus.js", "m.manaMenuItems(['R,U'])[0].label") == "Tap for {R}{U}"
