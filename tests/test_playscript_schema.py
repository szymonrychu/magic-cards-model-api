"""The play-script schema (design D2): a closed vocabulary, bounded, no loops.

`playscripts.schema.Play` is the pydantic model every script in
`decks/<slug>/plays/<id>.json` must validate against, and `play_errors` is the
same judgement expressed as the coded errors mtg-decks' `check_plays.py`
shares verbatim. Every bound in D2/D6 is violated here once and must come
back with its own code.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from playscript_testkit import FETCH_SHOCK, FETCH_SURVEIL, play
from pydantic import ValidationError

from magic_cards_model_api.playscripts.schema import (
    CODES,
    CONDITION_KINDS,
    MOVE_KINDS,
    STEPS,
    Play,
    Selector,
    play_errors,
)


def codes(doc: Any) -> list[str]:
    return sorted({error.code for error in play_errors(doc)})


def test_both_worked_examples_validate():
    for example in (FETCH_SHOCK, FETCH_SURVEIL):
        assert play_errors(example) == []
        model = Play.model_validate(example)
        assert model.id == example["id"]


def test_the_vocabulary_is_closed_at_seven_conditions_and_four_moves():
    assert CONDITION_KINDS == (
        "count",
        "zone_size",
        "life",
        "stack_top",
        "land_drop_available",
        "untapped_mana",
        "input",
    )
    assert MOVE_KINDS == ("activate", "cast", "play_land", "answer")


def test_steps_are_the_autopass_steps_without_the_combat_declarations():
    from magic_cards_model_api.seatkit import AUTOPASS_STEPS

    assert (
        tuple(s for s in AUTOPASS_STEPS if s not in ("declare_attackers", "declare_blockers"))
        == STEPS
    )


def test_the_error_codes_are_the_shared_contract_in_order():
    assert CODES == (
        "malformed-play",
        "unknown-field",
        "id-mismatch",
        "bounds-exceeded",
        "forbidden-kind",
        "hidden-zone-side-any",
        "card-name-in-field",
        "phrase-too-narrow",
        "unknown-next",
        "next-cycle",
        "chain-too-deep",
        "too-many-plays",
        "too-many-active-for-archetype",
        "unknown-archetype-key",
        "unknown-input",
        "invalid-input-value",
        "malformed-inputs",
    )


def mutate(path: str, value: Any) -> dict[str, Any]:
    """The fetch-shock example with one dotted path replaced."""
    doc = copy.deepcopy(FETCH_SHOCK)
    parts = path.split(".")
    node: Any = doc
    for part in parts[:-1]:
        node = node[int(part)] if part.isdigit() else node[part]
    last = parts[-1]
    if last.isdigit():
        node[int(last)] = value
    else:
        node[last] = value
    return doc


COND = {"kind": "life", "side": "me", "op": ">=", "n": 1}
ANSWER = {"decision": "pay_life", "accept": True}
MOVE = FETCH_SHOCK["moves"][0]

BOUNDS = [
    ("priority-above-100", mutate("priority", 101)),
    ("priority-negative", mutate("priority", -1)),
    ("no-moves", mutate("moves", [])),
    ("five-moves", mutate("moves", [copy.deepcopy(MOVE)] * 5)),
    ("four-answers", mutate("moves.0.answers", [ANSWER] * 4)),
    ("four-next", mutate("next", ["a", "b", "c", "d"])),
    ("nine-true", mutate("true", [COND] * 9)),
    ("nine-false", mutate("false", [COND] * 9)),
    ("no-steps", mutate("when.steps", [])),
    (
        "five-steps",
        mutate("when.steps", ["upkeep", "draw", "precombat_main", "end_step", "combat_damage"]),
    ),
    ("answer-move-without-answers", mutate("moves", [{"kind": "answer", "answers": []}])),
    ("answer-move-four-answers", mutate("moves", [{"kind": "answer", "answers": [ANSWER] * 4}])),
    ("pick-count-zero", mutate("moves.0.answers.0.pick.count", 0)),
    ("negative-n", mutate("true.1.n", -1)),
    ("negative-selector-n", mutate("true.0.select", {"power": {"op": ">=", "n": -2}})),
]


@pytest.mark.parametrize("doc", [doc for _, doc in BOUNDS], ids=[name for name, _ in BOUNDS])
def test_every_bound_violated_is_bounds_exceeded(doc):
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


MALFORMED = [
    ("schema-version-2", mutate("schema_version", 2)),
    ("schema-version-true", mutate("schema_version", True)),
    ("priority-true", mutate("priority", True)),
    ("priority-string", mutate("priority", "60")),
    ("no-archetypes", mutate("opposing_archetypes", [])),
    ("archetype-not-a-slug", mutate("opposing_archetypes", ["Dimir Frog"])),
    ("archetype-trailing-dash", mutate("opposing_archetypes", ["dimir-"])),
    ("whose-turn-unknown", mutate("when.whose_turn", "yours")),
    ("stack-unknown", mutate("when.stack", "full")),
    ("step-unknown", mutate("when.steps", ["cleanup"])),
    ("blank-name", mutate("name", "   ")),
    ("id-not-a-string", mutate("id", 7)),
    ("tapped-not-bool", mutate("true.0.select.tapped", 0)),
    ("op-unknown", mutate("true.1.op", "=>")),
    ("condition-without-kind", mutate("true.1", {"side": "me", "op": ">=", "n": 1})),
    ("condition-kind-not-a-string", mutate("true.1", {"kind": 5})),
    ("condition-not-an-object", mutate("true.1", "life >= 8")),
    (
        "mana-not-auto",
        mutate(
            "moves", [{"kind": "cast", "card": {"zone": "hand", "select": {}}, "mana": "manual"}]
        ),
    ),
    ("sacrifice-unknown", mutate("moves.0.sacrifice", "other")),
    (
        "input-op-unknown",
        mutate("true", [{"kind": "input", "name": "plan", "op": "contains", "value": "a"}]),
    ),
]


@pytest.mark.parametrize("doc", [doc for _, doc in MALFORMED], ids=[name for name, _ in MALFORMED])
def test_every_malformed_shape_is_malformed_play(doc):
    assert codes(doc) == ["malformed-play"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_a_missing_required_field_is_malformed_play():
    for key in ("schema_version", "id", "name", "priority", "opposing_archetypes", "when", "moves"):
        doc = copy.deepcopy(FETCH_SHOCK)
        del doc[key]
        assert codes(doc) == ["malformed-play"], key


def test_optional_top_level_fields_may_be_omitted():
    doc = copy.deepcopy(FETCH_SHOCK)
    for key in ("intent", "true", "false", "next"):
        del doc[key]
    assert play_errors(doc) == []


@pytest.mark.parametrize("step", ["declare_attackers", "declare_blockers"])
def test_a_combat_declaration_step_is_forbidden(step):
    assert codes(mutate("when.steps", ["precombat_main", step])) == ["forbidden-kind"]


@pytest.mark.parametrize("kind", ["attack", "block", "mulligan", "concede", "pass", "loop"])
def test_a_move_outside_the_closed_four_is_forbidden(kind):
    assert codes(mutate("moves", [{"kind": kind}])) == ["forbidden-kind"]


@pytest.mark.parametrize("kind", ["graveyard_order", "library_top", "turn_number"])
def test_a_condition_outside_the_closed_seven_is_forbidden(kind):
    assert codes(mutate("true", [{"kind": kind}])) == ["forbidden-kind"]


@pytest.mark.parametrize("zone", ["hand", "library", "sideboard"])
def test_a_count_over_a_hidden_zone_must_name_a_side(zone):
    condition = {"kind": "count", "zone": zone, "side": "any", "op": ">=", "n": 1, "select": {}}
    doc = mutate("true", [condition])
    assert codes(doc) == ["hidden-zone-side-any"]
    with pytest.raises(ValidationError) as raised:
        Play.model_validate(doc)
    assert "hidden_zone_side_any" in {e["type"] for e in raised.value.errors()}


@pytest.mark.parametrize("zone", ["hand", "library", "sideboard"])
@pytest.mark.parametrize("side", ["me", "opponent"])
def test_a_count_over_a_hidden_zone_with_a_side_is_fine(zone, side):
    condition = {"kind": "count", "zone": zone, "side": side, "op": ">=", "n": 1, "select": {}}
    assert play_errors(mutate("false", [condition])) == []


def test_hidden_zone_side_any_is_reported_even_beside_another_error():
    condition = {"kind": "count", "zone": "hand", "side": "any", "op": ">=", "n": -1, "select": {}}
    assert codes(mutate("true", [condition])) == ["bounds-exceeded", "hidden-zone-side-any"]


def test_zone_size_over_a_hidden_zone_may_say_any():
    condition = {"kind": "zone_size", "zone": "hand", "side": "any", "op": ">=", "n": 1}
    assert play_errors(mutate("true", [condition])) == []


def test_count_zone_also_accepts_library_and_sideboard():
    for zone in ("battlefield", "graveyard", "exile", "hand", "stack", "library", "sideboard"):
        condition = {"kind": "count", "zone": zone, "side": "me", "op": ">=", "n": 0, "select": {}}
        assert play_errors(mutate("true", [condition])) == [], zone


@pytest.mark.parametrize(
    "path",
    [
        "",
        "when",
        "true.0",
        "true.0.select",
        "moves.0",
        "moves.0.source",
        "moves.0.answers.0",
        "moves.0.answers.0.pick",
    ],
)
def test_an_unknown_field_is_refused_at_any_depth(path):
    doc = copy.deepcopy(FETCH_SHOCK)
    node: Any = doc
    for part in path.split(".") if path else []:
        node = node[int(part)] if part.isdigit() else node[part]
    node["card_name"] = "Arid Mesa"
    assert codes(doc) == ["unknown-field"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


LOOP_LIKE = ("repeat", "times", "loop", "while", "until", "iterations", "for_each", "each", "again")


@pytest.mark.parametrize("field", LOOP_LIKE)
@pytest.mark.parametrize("where", ["", "moves.0", "moves.0.answers.0", "when"])
def test_loop_like_fields_are_unknown_fields(field, where):
    doc = copy.deepcopy(FETCH_SHOCK)
    node: Any = doc
    for part in where.split(".") if where else []:
        node = node[int(part)] if part.isdigit() else node[part]
    node[field] = 3
    assert codes(doc) == ["unknown-field"]


def _property_names(schema: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                names.update(value)
            names |= _property_names(value)
    elif isinstance(schema, list):
        for value in schema:
            names |= _property_names(value)
    return names


def test_no_field_anywhere_iterates_attacks_blocks_mulligans_concedes_or_passes():
    names = _property_names(Play.model_json_schema())
    forbidden = set(LOOP_LIKE) | {
        "attack",
        "attackers",
        "block",
        "blockers",
        "mulligan",
        "concede",
        "pass",
    }
    assert not names & forbidden, names & forbidden


def test_no_selector_field_takes_a_card_name():
    assert not {"name", "card_name", "card", "names", "in_decklist"} & set(Selector.model_fields)
    assert set(Selector.model_fields) == {
        "types_all",
        "types_any",
        "subtypes_any",
        "supertypes_any",
        "colors_any",
        "mana_value",
        "power",
        "toughness",
        "keywords_any",
        "tapped",
        "counters",
        "ability_text_has",
        "oracle_has",
    }


def test_an_input_condition_is_usable_in_true_and_false():
    for op, value in (
        ("eq", "aggro"),
        ("ne", True),
        ("in", ["a", "b"]),
        ("not_in", [1]),
        ("<", 3),
        ("<=", 3),
        (">", 3),
        (">=", 3),
    ):
        condition = {"kind": "input", "name": "plan", "op": op, "value": value}
        assert play_errors(play(true=[condition], false=[condition])) == [], op


def test_the_answer_item_takes_to_bottom_for_scry():
    doc = mutate(
        "moves",
        [
            {
                "kind": "answer",
                "answers": [{"decision": "scry", "to_bottom": {"types_any": ["Land"]}}],
            }
        ],
    )
    assert play_errors(doc) == []
    model = Play.model_validate(doc)
    assert model.moves[0].answers[0].to_bottom is not None


def test_every_move_kind_validates_in_its_documented_shape():
    moves = [
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_any": ["Instant"]}},
            "targets": [{"select": {}, "side": "opponent"}],
            "mana": "auto",
            "answers": [],
        },
        {"kind": "play_land", "card": {"select": {"types_all": ["Land"]}}},
        {"kind": "answer", "answers": [{"decision": "may", "accept": False}]},
        {
            "kind": "activate",
            "source": {"zone": "battlefield", "select": {}},
            "ability_text_has": "Search your library for",
        },
    ]
    assert play_errors(play(moves=moves)) == []


def test_items_beyond_a_too_long_list_are_still_judged():
    moves = [copy.deepcopy(MOVE)] * 4 + [{"kind": "attack"}]
    assert codes(play(moves=moves)) == ["bounds-exceeded", "forbidden-kind"]


def test_the_error_message_says_where():
    (error,) = play_errors(mutate("moves.0.answers.0.pick.count", 0))
    assert "moves[0].answers[0].pick.count" in error.message
    (error,) = play_errors(mutate("true.1", {"kind": "turn_number"}))
    assert error.code == "forbidden-kind" and "true[1]" in error.message


def test_a_non_object_play_is_malformed():
    assert codes([FETCH_SHOCK]) == ["malformed-play"]
    assert codes("fetch") == ["malformed-play"]
