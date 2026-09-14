"""Selectors: a pure match of D2's characteristics against one CardView.

Every field is a characteristic or a template phrase; none takes a card name
(D3). Type fields read `type_line`, `colors_any` and `mana_value` read the
engine's own `colors`/`mana_value` on the view, text fields are
case-insensitive substrings.
"""

from __future__ import annotations

from typing import Any

import pytest
from playscript_testkit import FETCH_TEXT, bolt, card, contents, fetchland, mountain, state_view

from magic_cards_model_api.playscripts.conditions import ContentsRequired, presence
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


def test_colorless_matches_a_card_with_no_color_and_never_a_colored_one():
    # CR 105.2c: "A colorless object has no color." The view's `colors` is empty.
    spawner = card("Spawner", type_line="Creature — Eldrazi Drone", colors=(), mana_value=4.0)
    helix = card("Helix", type_line="Instant", colors=("R", "W"), mana_value=2.0)
    for word in ("colorless", "Colorless", "C", "c"):
        assert matches({"colors_any": [word]}, spawner), word
        assert matches({"colors_any": [word]}, fetchland()), word
        assert not matches({"colors_any": [word]}, helix), word
    assert matches({"colors_any": ["colorless", "red"]}, helix), "any of them: red matches"
    assert matches({"colors_any": ["colorless", "red"]}, spawner), "any of them: colorless"
    assert not matches({"colors_any": ["red"]}, spawner)


def test_the_color_vocabulary_is_the_five_colors_and_colorless():
    from magic_cards_model_api.playscripts.selectors import COLOR_LETTERS

    assert COLOR_LETTERS == {
        "white": "w",
        "blue": "u",
        "black": "b",
        "red": "r",
        "green": "g",
        "colorless": "c",
    }


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
        "land_face": True,
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


# -- land_face (O5) ----------------------------------------------------------

#: Jwari Disruption // Jwari Ruins (ZNR modal double-faced card), real
#: printed text (`magic-cards-model`'s `data/printings/jwari_disruption.
#: json`) - O3/O5's selector tests take card text from the engine's real
#: printings rather than invent it, so a fixture reads exactly what the
#: engine would serialize.
JWARI_FACES = [
    {
        "name": "Jwari Disruption",
        "mana_cost": "{1}{U}",
        "type_line": "Instant",
        "oracle_text": "Counter target spell unless its controller pays {1}.",
    },
    {
        "name": "Jwari Ruins",
        "mana_cost": "",
        "type_line": "Land",
        "oracle_text": "This land enters tapped.\n{T}: Add {U}.",
        "land_face": True,
    },
]


def _jwari(zone: str) -> dict:
    view = card("Jwari Disruption", type_line="Instant // Land", zone=zone, oracle_text=None)
    view["card_name"] = "Jwari Disruption // Jwari Ruins"
    view["faces"] = [dict(face) for face in JWARI_FACES]
    return view


def test_land_face_matches_an_mdfc_with_a_land_back_face_in_hand():
    assert matches({"land_face": True}, _jwari("hand"))
    assert matches({"land_face": True}, _dfc("hand", "Witch Enchanter"))


def test_land_face_false_matches_a_card_with_no_land_face():
    assert matches({"land_face": False}, bolt())
    assert not matches({"land_face": True}, bolt())


def test_a_plain_types_all_land_does_not_match_an_mdfc_whose_front_face_is_not_a_land():
    assert not matches({"types_all": ["Land"]}, _jwari("hand"))
    assert not matches({"types_all": ["Land"]}, _dfc("hand", "Witch Enchanter"))
    selector = {"land_face": True, "types_all": ["Land"]}
    assert not matches(selector, _jwari("hand")), "types_all still reads the up face"


def test_land_face_matches_a_plain_land_too():
    assert matches({"land_face": True}, fetchland())
    assert matches({"land_face": True}, mountain())


def test_land_face_does_not_match_a_transform_cards_land_back():
    """O5's fix: only a modal double-faced card's face is marked `land_face`
    - a transform card's land back (Treasure Map // Treasure Cove, real
    printed text from `data/printings/treasure_map.json`) is reached only by
    transforming (CR 712.2), never a land drop, so it must never match even
    though its own face plainly types as a Land."""
    view = card("Treasure Map", type_line="Artifact // Land", zone="hand", oracle_text=None)
    view["card_name"] = "Treasure Map // Treasure Cove"
    view["faces"] = [
        {
            "name": "Treasure Map",
            "mana_cost": "{2}",
            "type_line": "Artifact",
            "oracle_text": (
                "{1}, {T}: Scry 1. Put a landmark counter on this artifact. Then if there "
                "are three or more landmark counters on it, remove those counters, "
                "transform this artifact, and create three Treasure tokens. (They're "
                'artifacts with "{T}, Sacrifice this token: Add one mana of any color.")'
            ),
        },
        {
            "name": "Treasure Cove",
            "mana_cost": "",
            "type_line": "Land",
            "oracle_text": (
                "(Transforms from Treasure Map.)\n{T}: Add {C}.\n"
                "{T}, Sacrifice a Treasure: Draw a card."
            ),
        },
    ]
    assert not matches({"land_face": True}, view)
    assert matches({"land_face": False}, view)


# -- searches_for_any (O3) --------------------------------------------------------

FETCH_TEMPLATE = (
    "{T}, Pay 1 life, Sacrifice this land: Search your library for %s card, "
    "put it onto the battlefield, then shuffle."
)


#: The machine-readable ability text `machines/keywords.py`'s
#: `off_zone_abilities`/`_synthetic_ability` actually synthesizes for a
#: typecycling keyword - not the printed reminder text, the expansion CR
#: 702.29e means: `<cost>, Discard this card: Search your library for a
#: <type> card, reveal it, and put it into your hand. Then shuffle.`
def typecycling_ability(cost: str, type_name: str) -> str:
    return (
        f"{cost}, Discard this card: Search your library for a {type_name} card, "
        f"reveal it, and put it into your hand. Then shuffle."
    )


def strand() -> dict[str, Any]:
    # Flooded Strand: search for a Plains or Island card.
    return card(
        "Flooded Strand", type_line="Land", oracle_text=FETCH_TEMPLATE % "a Plains or Island"
    )


def delta() -> dict[str, Any]:
    # Polluted Delta: search for an Island or Swamp card.
    return card(
        "Polluted Delta", type_line="Land", oracle_text=FETCH_TEMPLATE % "an Island or Swamp"
    )


def tarn() -> dict[str, Any]:
    # Scalding Tarn: search for an Island or Mountain card.
    return card(
        "Scalding Tarn", type_line="Land", oracle_text=FETCH_TEMPLATE % "an Island or Mountain"
    )


def generous_ent() -> dict[str, Any]:
    # Generous Ent's real printed text (magic-cards-model's data/printings/
    # generous_ent.json): Reach, a Food-token ETB, and Forestcycling {1} -
    # no attack trigger at all. The typecycling ability searches for a
    # Forest card in hand; the engine synthesizes the machine-readable
    # ability off the printed Forestcycling keyword
    # (machines/keywords.py off_zone_abilities), so it is on `abilities`.
    reach = "Reach"
    food = (
        "When this creature enters, create a Food token. (It's an artifact with "
        '"{2}, {T}, Sacrifice this token: You gain 3 life.")'
    )
    reminder = (
        "Forestcycling {1} ({1}, Discard this card: Search your library for a "
        "Forest card, reveal it, put it into your hand, then shuffle.)"
    )
    ability = typecycling_ability("{1}", "Forest")
    return card(
        "Generous Ent",
        type_line="Creature — Treefolk",
        zone="hand",
        oracle_text=f"{reach}\n{food}\n{reminder}",
        abilities=(ability,),
    )


def expedition_map() -> dict[str, Any]:
    # No basic land type is named at all - "a land card" is generic.
    return card(
        "Expedition Map",
        type_line="Artifact",
        oracle_text=(
            "{2}, {T}, Sacrifice Expedition Map: Search your library for a land card, "
            "reveal it, put it into your hand, then shuffle."
        ),
    )


def test_searches_for_any_matches_dual_fetches():
    assert matches({"searches_for_any": ["Plains", "Island"]}, strand())
    assert matches({"searches_for_any": ["Island"]}, strand())
    assert not matches({"searches_for_any": ["Forest"]}, strand())
    assert matches({"searches_for_any": ["Swamp"]}, delta())
    assert matches({"searches_for_any": ["Mountain"]}, tarn())


def test_searches_for_any_matches_a_landcycling_ability_in_hand():
    assert matches({"searches_for_any": ["Forest"]}, generous_ent())
    assert not matches({"searches_for_any": ["Mountain"]}, generous_ent())


def test_searches_for_any_does_not_match_a_generic_land_search():
    ex_map = expedition_map()
    for word in ("Plains", "Island", "Swamp", "Mountain", "Forest"):
        assert not matches({"searches_for_any": [word]}, ex_map)


def test_searches_for_any_combines_with_other_fields():
    selector = {"types_all": ["Land"], "searches_for_any": ["Island"]}
    assert matches(selector, strand())
    assert not matches(selector, generous_ent()), "Generous Ent's front face is a Creature"


# -- missing_from ----------------------------------------------------------------

TRON = ["Tower", "Mine", "Power-Plant"]


def tron(piece: str, **kw: Any) -> dict[str, Any]:
    """An Urza's land view, typed as the printing is: `Land — Urza's <piece>`."""
    return card(f"Urza's {piece.replace('-', ' ')}", type_line=f"Land — Urza's {piece}", **kw)


def missing(
    by: str = "subtype", zones: tuple[str, ...] = ("battlefield", "hand"), side: str = "me", **sel
) -> dict[str, Any]:
    return {**sel, "missing_from": {"zones": list(zones), "side": side, "by": by}}


def library() -> list[dict[str, Any]]:
    return [tron(piece, zone="library") for piece in TRON]


def picked(selector: dict[str, Any], candidates: list[dict[str, Any]], view: dict[str, Any]):
    present = presence(view, "alice")
    return [c["name"] for c in candidates if matches(selector, c, present)]


def test_missing_from_by_subtype_compares_only_the_selectors_own_subtypes():
    view = state_view(battlefield=[tron("Tower")], hands={"alice": [tron("Mine", zone="hand")]})
    assert picked(missing(subtypes_any=TRON), library(), view) == ["Urza's Power Plant"]
    # Without subtypes_any every subtype is compared, and all three share Urza's.
    assert picked(missing(), library(), view) == []


def test_missing_from_by_subtype_on_a_multi_subtype_candidate():
    foundry = card("Foundry", type_line="Land — Mountain Plains", zone="library")
    plains = state_view(battlefield=[card("Plains", type_line="Basic Land — Plains")])
    assert not matches(
        missing(subtypes_any=["Mountain", "Plains"]), foundry, presence(plains, "alice")
    )
    assert matches(missing(subtypes_any=["Mountain"]), foundry, presence(plains, "alice"))
    assert not matches(missing(), foundry, presence(plains, "alice"))
    assert matches(missing(), foundry, presence(state_view(), "alice"))


def test_a_candidate_with_no_compared_subtype_never_matches_missing_from():
    assert not matches(missing(), bolt(zone="library"), presence(state_view(), "alice"))
    wastes = card("Wastes", type_line="Basic Land", zone="library")
    assert not matches(missing(types_any=["Land"]), wastes, presence(state_view(), "alice"))


def test_a_characteristic_present_only_in_an_excluded_zone_is_still_missing():
    view = state_view(
        graveyards={"alice": [tron("Tower", zone="graveyard")]},
        hands={"alice": [tron("Mine", zone="hand"), tron("Power-Plant", zone="hand")]},
    )
    assert picked(missing(subtypes_any=TRON), library(), view) == ["Urza's Tower"]
    with_graveyard = missing(subtypes_any=TRON, zones=("battlefield", "hand", "graveyard"))
    assert picked(with_graveyard, library(), view) == []


def test_missing_from_by_name_compares_names_between_objects():
    view = state_view(battlefield=[mountain()], hands={"alice": [bolt()]})
    shock = card("Shock", type_line="Instant", zone="library")
    assert not matches(missing(by="name"), bolt(zone="library"), presence(view, "alice"))
    assert matches(missing(by="name"), shock, presence(view, "alice"))
    assert not matches(
        missing(by="name", zones=("battlefield",)), mountain(), presence(view, "alice")
    )
    assert matches(missing(by="name", zones=("hand",)), mountain(), presence(view, "alice"))
    foundry = card("Foundry", type_line="Land — Mountain Plains", zone="library")
    assert matches(missing(by="name"), foundry, presence(view, "alice")), "names, not subtypes"


def test_missing_from_reads_the_side_it_names():
    view = state_view(battlefield=[tron("Tower", controller="bob")])
    theirs = missing(subtypes_any=TRON, zones=("battlefield",), side="opponent")
    assert matches(
        missing(subtypes_any=TRON), tron("Tower", zone="library"), presence(view, "alice")
    )
    assert not matches(theirs, tron("Tower", zone="library"), presence(view, "alice"))


def test_missing_from_over_the_opponents_hand_reads_only_known_cards():
    theirs = missing(subtypes_any=TRON, zones=("hand",), side="opponent")
    unknown = state_view(hand_count={"bob": 7})
    known = state_view(known={"hand": [tron("Tower", zone="hand", controller="bob")]})
    assert matches(theirs, tron("Tower"), presence(unknown, "alice"))
    assert not matches(theirs, tron("Tower"), presence(known, "alice"))


def test_missing_from_over_the_opponents_library_reads_only_known_records():
    theirs = missing(subtypes_any=TRON, zones=("library",), side="opponent")
    record = {"index_from_top": 0, "card": tron("Mine", zone="library", controller="bob")}
    view = state_view(known={"library": [record]})
    assert picked(theirs, library(), view) == ["Urza's Tower", "Urza's Power Plant"]


def test_missing_from_over_my_own_library_reads_the_contents_and_needs_them():
    mine = missing(subtypes_any=TRON, zones=("library",))
    view = state_view(library_contents=contents((tron("Tower", zone="library"), 2)))
    assert picked(mine, [tron(piece, zone="hand") for piece in TRON], view) == [
        "Urza's Mine",
        "Urza's Power Plant",
    ]
    with pytest.raises(ContentsRequired):
        matches(mine, tron("Tower"), presence(state_view(), "alice"))


def test_a_selector_with_missing_from_cannot_be_matched_without_the_board():
    with pytest.raises(ValueError, match="missing_from"):
        matches(missing(), mountain())


# -- enters_any (O4) --------------------------------------------------------------


def test_enters_any_matches_the_hypothetical_facts_state():
    land = mountain()
    hypo = {land["id"]: {"enters": "untapped"}}
    assert matches({"enters_any": ["untapped"]}, land, hypo=hypo)
    assert not matches({"enters_any": ["tapped"]}, land, hypo=hypo)
    assert matches({"enters_any": ["tapped", "untapped"]}, land, hypo=hypo)


def test_enters_any_never_matches_a_card_absent_from_hypothetical_facts():
    land = mountain()
    assert not matches({"enters_any": ["untapped"]}, land, hypo={})


def test_enters_any_without_hypothetical_facts_raises():
    from magic_cards_model_api.playscripts.selectors import HypotheticalRequired

    with pytest.raises(HypotheticalRequired):
        matches({"enters_any": ["untapped"]}, mountain())


# -- unlocks, unlocks_by_search (O7) -----------------------------------------------


def test_unlocks_matches_the_hypothetical_facts_bool():
    land = mountain()
    assert matches({"unlocks": True}, land, hypo={land["id"]: {"unlocks": True}})
    assert not matches({"unlocks": True}, land, hypo={land["id"]: {"unlocks": False}})
    assert matches({"unlocks": False}, land, hypo={land["id"]: {"unlocks": False}})
    assert not matches({"unlocks": True}, land, hypo={})


def test_unlocks_by_search_matches_the_hypothetical_facts_bool():
    spell = bolt()
    hot = {spell["id"]: {"unlocks_by_search": True}}
    cold = {spell["id"]: {"unlocks_by_search": False}}
    assert matches({"unlocks_by_search": True}, spell, hypo=hot)
    assert not matches({"unlocks_by_search": True}, spell, hypo=cold)
    assert not matches({"unlocks_by_search": True}, spell, hypo={})


def test_unlocks_by_basic_search_matches_the_hypothetical_facts_bool():
    spell = bolt()
    hot = {spell["id"]: {"unlocks_by_search": True, "unlocks_by_basic_search": True}}
    shock_only = {spell["id"]: {"unlocks_by_search": True, "unlocks_by_basic_search": False}}
    assert matches({"unlocks_by_basic_search": True}, spell, hypo=hot)
    assert not matches({"unlocks_by_basic_search": True}, spell, hypo=shock_only)
    assert not matches({"unlocks_by_basic_search": True}, spell, hypo={})


def test_unlocks_without_hypothetical_facts_raises():
    from magic_cards_model_api.playscripts.selectors import HypotheticalRequired

    with pytest.raises(HypotheticalRequired):
        matches({"unlocks": True}, mountain())
    with pytest.raises(HypotheticalRequired):
        matches({"unlocks_by_search": True}, bolt())
    with pytest.raises(HypotheticalRequired):
        matches({"unlocks_by_basic_search": True}, bolt())


# -- affordable (O7) ---------------------------------------------------------------


def test_affordable_compares_mana_value_to_untapped_mana_plus_extra():
    spell = bolt()  # mana_value 1.0
    assert matches({"affordable": {"extra": 0}}, spell, untapped_mana=1)
    assert not matches({"affordable": {"extra": 0}}, spell, untapped_mana=0)
    assert matches({"affordable": {"extra": 1}}, spell, untapped_mana=0)


def test_affordable_without_a_mana_value_never_matches():
    land = mountain()  # mana_value 0.0 by the testkit default, but a land has none in play
    no_mv = dict(land, mana_value=None)
    assert not matches({"affordable": {"extra": 5}}, no_mv, untapped_mana=5)


def test_affordable_with_no_untapped_mana_given_never_matches():
    spell = bolt()
    assert not matches({"affordable": {"extra": 5}}, spell)
