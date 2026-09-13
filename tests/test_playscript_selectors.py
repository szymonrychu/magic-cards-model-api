"""Selectors: a pure match of D2's characteristics against one CardView.

Every field is a characteristic or a template phrase; none takes a card name
(D3). Type fields read `type_line`, `colors_any` and `mana_value` read the
engine's own `colors`/`mana_value` on the view, text fields are
case-insensitive substrings.
"""

from __future__ import annotations

import pytest
from playscript_testkit import FETCH_TEXT, bolt, card, fetchland, mountain

from magic_cards_model_api.playscripts.schema import Selector
from magic_cards_model_api.playscripts.selectors import matches, parse_type_line


def test_parse_type_line_splits_supertypes_types_and_subtypes():
    assert parse_type_line("Legendary Creature — Human Wizard") == (
        {"legendary"},
        {"creature"},
        {"human", "wizard"},
    )
    assert parse_type_line("Basic Snow Land — Forest") == ({"basic", "snow"}, {"land"}, {"forest"})
    assert parse_type_line("Artifact Creature — Golem") == (
        set(),
        {"artifact", "creature"},
        {"golem"},
    )
    assert parse_type_line("Instant") == (set(), {"instant"}, set())
    assert parse_type_line("") == (set(), set(), set())
    assert parse_type_line(None) == (set(), set(), set())


def test_parse_type_line_reads_every_face_of_a_split_card():
    assert parse_type_line("Instant // Sorcery — Adventure") == (
        set(),
        {"instant", "sorcery"},
        {"adventure"},
    )


def test_an_empty_selector_matches_every_card():
    for view in (bolt(), fetchland(), mountain(), card(type_line="")):
        assert matches({}, view)


def test_types_all_needs_every_type_and_types_any_needs_one():
    golem = card("Golem", type_line="Artifact Creature — Golem", power=3, toughness=3)
    bear = card("Bear", type_line="Creature — Bear", power=2, toughness=2)
    assert matches({"types_all": ["Artifact", "Creature"]}, golem)
    assert not matches({"types_all": ["Artifact", "Creature"]}, bear)
    assert matches({"types_any": ["Artifact", "Instant"]}, golem)
    assert not matches({"types_any": ["Artifact", "Instant"]}, bear)
    assert matches({"types_all": ["creature"]}, bear), "type words compare case-insensitively"


def test_subtypes_any_and_supertypes_any():
    foundry = card("Foundry", type_line="Land — Mountain Plains")
    basic = mountain()
    legend = card("Legend", type_line="Legendary Creature — Human Wizard")
    assert matches({"subtypes_any": ["Mountain"]}, foundry)
    assert matches({"subtypes_any": ["Mountain"]}, basic)
    assert not matches({"subtypes_any": ["Mountain"]}, fetchland())
    assert matches({"supertypes_any": ["Basic"]}, basic)
    assert not matches({"supertypes_any": ["Basic"]}, foundry)
    assert matches({"supertypes_any": ["Legendary"]}, legend)
    assert not matches({"types_any": ["Legendary"]}, legend), "a supertype is not a card type"
    assert not matches({"subtypes_any": ["Creature"]}, legend), "a card type is not a subtype"


def test_colors_any_reads_the_views_colors_by_letter_or_name():
    helix = card("Helix", type_line="Instant", colors=("R", "W"), mana_value=2.0)
    assert matches({"colors_any": ["R"]}, helix)
    assert matches({"colors_any": ["white"]}, helix)
    assert matches({"colors_any": ["u", "w"]}, helix)
    assert not matches({"colors_any": ["G"]}, helix)
    assert not matches({"colors_any": ["R"]}, fetchland()), "a land with no colors matches none"


@pytest.mark.parametrize(
    ("op", "n", "expected"),
    [
        (">=", 1, True),
        ("<=", 1, True),
        ("==", 1, True),
        ("!=", 1, False),
        (">", 0, True),
        ("<", 1, False),
    ],
)
def test_mana_value_compares(op, n, expected):
    assert matches({"mana_value": {"op": op, "n": n}}, bolt()) is expected


def test_mana_value_missing_from_the_view_never_matches():
    view = bolt()
    del view["mana_value"]
    assert not matches({"mana_value": {"op": ">=", "n": 0}}, view)


def test_power_and_toughness_compare_and_a_non_creature_never_matches():
    bear = card("Bear", type_line="Creature — Bear", power=2, toughness=3)
    assert matches({"power": {"op": "==", "n": 2}, "toughness": {"op": ">=", "n": 3}}, bear)
    assert not matches({"power": {"op": ">", "n": 2}}, bear)
    assert not matches({"toughness": {"op": "<", "n": 3}}, bear)
    assert not matches({"power": {"op": ">=", "n": 0}}, bolt())


def test_keywords_any_is_case_insensitive():
    sewers = card("Sewers", type_line="Land — Island Swamp", keywords=("Surveil",))
    assert matches({"keywords_any": ["surveil"]}, sewers)
    assert matches({"keywords_any": ["Flying", "Surveil"]}, sewers)
    assert not matches({"keywords_any": ["Flying"]}, sewers)


def test_tapped():
    assert matches({"tapped": False}, fetchland())
    assert not matches({"tapped": True}, fetchland())
    assert matches({"tapped": True}, fetchland(tapped=True))


def test_counters_compare_by_counter_kind_and_an_absent_kind_is_zero():
    grown = card("Grown", type_line="Creature — Hydra", counters={"+1/+1": 2}, power=2, toughness=2)
    assert matches({"counters": {"name": "+1/+1", "op": ">=", "n": 2}}, grown)
    assert not matches({"counters": {"name": "+1/+1", "op": ">", "n": 2}}, grown)
    assert matches({"counters": {"name": "time", "op": "==", "n": 0}}, grown)
    assert not matches({"counters": {"name": "time", "op": ">=", "n": 1}}, grown)


def test_ability_text_has_reads_ability_text_not_oracle_text():
    assert matches({"ability_text_has": "search your LIBRARY for"}, fetchland())
    no_abilities = card("Fetch", oracle_text=FETCH_TEXT)
    assert not matches({"ability_text_has": "Search your library for"}, no_abilities)


def test_ability_text_has_reads_a_stack_abilitys_own_text():
    trigger = {
        "kind": "ability",
        "id": "x",
        "name": "Sewers",
        "controller": "alice",
        "text": "When this land enters, surveil 1.",
        "targets": [],
        "target_refs": [],
        "zone": "stack",
    }
    assert matches({"ability_text_has": "surveil 1"}, trigger)
    assert not matches({"types_any": ["Land"]}, trigger), "an ability on the stack has no type line"


def test_oracle_has_reads_oracle_text_case_insensitively():
    shock = card(
        "Shock land",
        type_line="Land — Mountain Plains",
        oracle_text="As this land enters, you may pay 2 life.",
    )
    assert matches({"oracle_has": "YOU MAY PAY 2 LIFE"}, shock)
    assert not matches({"oracle_has": "pay 3 life"}, shock)


def test_a_card_name_is_never_read_by_any_field():
    mesa = card("Arid Mesa", oracle_text=FETCH_TEXT, abilities=(FETCH_TEXT,))
    assert not matches({"oracle_has": "Arid Mesa"}, mesa)
    assert not matches({"ability_text_has": "Arid Mesa"}, mesa)
    assert not matches({"keywords_any": ["Arid Mesa"]}, mesa)
    assert not matches({"types_any": ["Arid Mesa"]}, mesa)


def test_fields_combine_with_and():
    selector = {
        "types_all": ["Land"],
        "tapped": False,
        "ability_text_has": "Search your library for",
    }
    assert matches(selector, fetchland())
    assert not matches(selector, fetchland(tapped=True))
    assert not matches(selector, mountain())


def test_a_selector_model_matches_like_its_dict():
    selector = Selector.model_validate(
        {"types_all": ["Land"], "counters": {"name": "time", "op": "==", "n": 0}}
    )
    assert matches(selector, fetchland())
    assert not matches(selector, bolt())


def test_matching_does_not_mutate_the_card():
    view = bolt()
    before = repr(view)
    matches({"types_any": ["Instant"], "colors_any": ["R"], "oracle_has": "damage"}, view)
    assert repr(view) == before


# -- double-faced cards: the face a card has in its zone (CR 712.8a, 712.8f) ------

WITCH_FACES = [
    {
        "name": "Witch Enchanter",
        "mana_cost": "{3}{W}",
        "type_line": "Creature — Human Warlock",
        "oracle_text": "When this creature enters, destroy target artifact or enchantment.",
    },
    {
        "name": "Witch-Blessed Meadow",
        "mana_cost": "",
        "type_line": "Land",
        "oracle_text": "As this land enters, you may pay 3 life.",
    },
]


def _dfc(zone: str, name: str, *, type_line: str = "Creature — Human Warlock // Land") -> dict:
    view = card(name, type_line=type_line, zone=zone, oracle_text=None)
    view["card_name"] = "Witch Enchanter // Witch-Blessed Meadow"
    view["faces"] = [dict(face) for face in WITCH_FACES]
    return view


@pytest.mark.parametrize("zone", ["hand", "library", "graveyard", "exile", "sideboard"])
def test_a_double_faced_card_off_the_battlefield_and_stack_is_only_its_front_face(zone):
    witch = _dfc(zone, "Witch Enchanter")
    assert matches({"types_any": ["Creature"]}, witch)
    assert not matches({"types_any": ["Land"]}, witch)
    assert matches({"oracle_has": "destroy target artifact"}, witch)
    assert not matches({"oracle_has": "pay 3 life"}, witch)


def test_a_double_faced_spell_on_the_stack_is_the_face_it_was_cast_as():
    front = _dfc("stack", "Witch Enchanter")
    back = _dfc("stack", "Witch-Blessed Meadow")
    assert matches({"types_any": ["Creature"]}, front)
    assert not matches({"types_any": ["Land"]}, front)
    assert matches({"types_any": ["Land"]}, back)
    assert matches({"oracle_has": "pay 3 life"}, back)
    assert not matches({"oracle_has": "destroy target artifact"}, back)


def test_a_double_faced_permanent_reads_its_computed_type_line_and_up_face_text():
    meadow = _dfc("battlefield", "Witch-Blessed Meadow", type_line="Land")
    meadow["oracle_text"] = WITCH_FACES[1]["oracle_text"]
    assert matches({"types_any": ["Land"]}, meadow)
    assert not matches({"types_any": ["Creature"]}, meadow)
    assert matches({"oracle_has": "pay 3 life"}, meadow)
