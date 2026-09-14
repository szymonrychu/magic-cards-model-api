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
    AMOUNT_DECISION_KINDS,
    BASIC_LAND_TYPES,
    CAST_COSTS,
    CODES,
    CONDITION_KINDS,
    DECISION_KINDS,
    FORBIDDEN_DECISION_KINDS,
    MAX_ARCHETYPES,
    MAX_INPUT_VALUES,
    MAX_PREFER,
    MAX_SELECTOR_WORDS,
    MAX_TARGETS,
    MISSING_FROM_BY,
    MISSING_FROM_SIDES,
    MISSING_FROM_ZONES,
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


def test_the_vocabulary_is_closed_at_eight_conditions_and_four_moves():
    assert CONDITION_KINDS == (
        "count",
        "zone_size",
        "life",
        "stack_top",
        "land_drop_available",
        "untapped_mana",
        "turn",
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
        "unknown-decision-kind",
        "unknown-cast-cost",
        "duplicate-step",
        "wildcard-with-keys",
        "answer-shape-mismatch",
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
def test_a_condition_outside_the_closed_eight_is_forbidden(kind):
    assert codes(mutate("true", [{"kind": kind}])) == ["forbidden-kind"]


def test_a_turn_condition_validates():
    condition = {"kind": "turn", "op": "<=", "n": 2}
    doc = play(true=[condition], false=[])
    assert play_errors(doc) == []
    model = Play.model_validate(doc)
    assert model.true[0].op == "<=" and model.true[0].n == 2


def test_a_turn_condition_negative_n_is_bounds_exceeded():
    doc = mutate("true", [{"kind": "turn", "op": ">=", "n": -1}])
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


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
        "searches_for_any",
        "missing_from",
    }


# -- O3: searches_for_any -----------------------------------------------------


def test_searches_for_any_is_the_five_basic_land_types():
    assert BASIC_LAND_TYPES == ("Plains", "Island", "Swamp", "Mountain", "Forest")


def test_searches_for_any_validates_with_basic_land_type_words():
    doc = mutate("true.0.select", {"searches_for_any": ["Island", "Swamp"]})
    assert play_errors(doc) == []
    model = Play.model_validate(doc)
    assert model.true[0].select.searches_for_any == ["Island", "Swamp"]


@pytest.mark.parametrize("word", ["Elf", "Wastes", "Eldrazi", "plains"])
def test_a_word_outside_the_five_basic_land_types_is_malformed(word):
    doc = mutate("true.0.select", {"searches_for_any": [word]})
    assert codes(doc) == ["malformed-play"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_searches_for_any_is_bounded_at_the_five_basic_land_types():
    doc = mutate("true.0.select", {"searches_for_any": list(BASIC_LAND_TYPES)})
    assert play_errors(doc) == []
    over = mutate("true.0.select", {"searches_for_any": [*BASIC_LAND_TYPES, "Plains"]})
    assert codes(over) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(over)


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


# -- O2: prefer (up to 4) and ties: "any" on play_land.card, cast.card, -----------
# -- activate.source, Pick and SacrificeChoice -------------------------------


def test_play_land_card_takes_prefer_and_ties():
    doc = play(
        moves=[
            {
                "kind": "play_land",
                "card": {
                    "select": {"types_all": ["Land"]},
                    "prefer": [{"subtypes_any": ["Mountain"]}],
                    "ties": "any",
                },
            }
        ]
    )
    assert play_errors(doc) == []
    move = Play.model_validate(doc).moves[0]
    assert move.card.ties == "any" and len(move.card.prefer) == 1


def test_cast_card_takes_prefer_and_ties():
    doc = play(
        moves=[
            cast_move(
                card={
                    "zone": "hand",
                    "select": {"types_any": ["Creature"]},
                    "prefer": [{"colors_any": ["R"]}],
                    "ties": "any",
                }
            )
        ]
    )
    assert play_errors(doc) == []
    move = Play.model_validate(doc).moves[0]
    assert move.card.ties == "any"


def test_activate_source_takes_prefer_and_ties():
    doc = play(
        moves=[
            {
                "kind": "activate",
                "source": {
                    "zone": "battlefield",
                    "select": {"types_all": ["Land"]},
                    "prefer": [{"subtypes_any": ["Island"]}],
                    "ties": "any",
                },
                "ability_text_has": "Search your library for",
            }
        ]
    )
    assert play_errors(doc) == []
    move = Play.model_validate(doc).moves[0]
    assert move.source.ties == "any"


def test_pick_and_sacrifice_choice_take_ties():
    doc = mutate("moves.0.answers.0.pick.ties", "any")
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].answers[0].pick.ties == "any"
    sac_doc = mutate("moves.0.sacrifice", {"select": {"types_all": ["Creature"]}, "ties": "any"})
    assert play_errors(sac_doc) == []


def test_ties_outside_any_is_malformed_on_play_land_card():
    doc = play(moves=[{"kind": "play_land", "card": {"select": {}, "ties": "all"}}])
    assert codes(doc) == ["malformed-play"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_ties_outside_any_is_malformed_on_activate_source():
    doc = play(
        moves=[
            {
                "kind": "activate",
                "source": {"zone": "battlefield", "select": {}, "ties": "all"},
                "ability_text_has": "Search your library for",
            }
        ]
    )
    assert codes(doc) == ["malformed-play"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_ties_outside_any_is_malformed_on_pick():
    doc = mutate("moves.0.answers.0.pick.ties", "all")
    assert codes(doc) == ["malformed-play"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_land_card_and_zoned_card_prefer_are_bounded():
    over = [{}] * (MAX_PREFER + 1)
    play_land_doc = play(moves=[{"kind": "play_land", "card": {"select": {}, "prefer": over}}])
    assert codes(play_land_doc) == ["bounds-exceeded"]
    activate_doc = play(
        moves=[
            {
                "kind": "activate",
                "source": {"zone": "battlefield", "select": {}, "prefer": over},
                "ability_text_has": "Search your library for",
            }
        ]
    )
    assert codes(activate_doc) == ["bounds-exceeded"]


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


# -- D05: the cost a cast is announced under -------------------------------------


def cast_move(**over: Any) -> dict[str, Any]:
    move: dict[str, Any] = {
        "kind": "cast",
        "card": {"zone": "hand", "select": {"types_any": ["Creature"]}},
    }
    move.update(over)
    return move


def test_the_cast_cost_vocabulary_names_normal_evoke_warp_and_impending():
    assert {"normal", "evoke", "warp", "impending"} <= set(CAST_COSTS)
    assert len(set(CAST_COSTS)) == len(CAST_COSTS)


def test_the_cast_costs_are_the_engines_cast_option_kinds():
    """Every cast option kind a card view's `cast_options` can carry - the
    keywords `_option_from_keyword`, `keyword_cast_options` and
    `marker_cast_options` hand out - plus the card's own printed CR 118.9
    alternative cost (`AlternativeCostSource.PRINTED`), the printed mana cost
    (`normal`) and impending, which the engine does not offer yet. Read off
    the engine so a keyword it learns fails this test until the vocabulary
    names it."""
    import inspect
    import re

    from magic_cards_model.machines import casting

    source = inspect.getsource(casting._option_from_keyword)
    keywords = set(re.findall(r'canonical == "([a-z-]+)"', source))
    for group in re.findall(r"canonical in \(([^)]*)\)", source):
        keywords |= set(re.findall(r'"([a-z-]+)"', group))
    for function in (casting.keyword_cast_options, casting.marker_cast_options):
        keywords |= {
            word.lower()
            for word in re.findall(r'keyword="([A-Za-z-]+)"', inspect.getsource(function))
        }
    assert {"evoke", "warp", "adventure", "airbend"} <= keywords
    keywords.add(casting.AlternativeCostSource.PRINTED.value)
    assert set(CAST_COSTS) == {"normal", "impending"} | keywords


@pytest.mark.parametrize("cost", CAST_COSTS)
def test_every_cast_cost_validates(cost):
    doc = play(moves=[cast_move(cost=cost)])
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].cost == cost


def test_a_cast_may_leave_the_cost_out():
    doc = play(moves=[cast_move()])
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].cost is None


@pytest.mark.parametrize("cost", ["Evoke", "kicker", "overload", "", None, 3, ["evoke"]])
def test_a_cost_outside_the_vocabulary_is_unknown_cast_cost(cost):
    doc = play(moves=[cast_move(cost=cost)])
    assert codes(doc) == ["unknown-cast-cost"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_only_a_cast_takes_a_cost():
    doc = mutate("moves.0.cost", "evoke")
    assert codes(doc) == ["unknown-field"]


# -- D07: an answer names a decision kind the api can pend -----------------------


def test_the_decision_kinds_are_the_apis_pending_decision_kinds():
    from magic_cards_model_api.decisions import ANSWERABLE_KINDS

    assert set(DECISION_KINDS) == {kind.value for kind in ANSWERABLE_KINDS}
    assert set(FORBIDDEN_DECISION_KINDS) == {
        "assign_combat_damage",
        "declare_attackers",
        "declare_blockers",
        "mulligan",
    }


@pytest.mark.parametrize(
    "kind", sorted(set(DECISION_KINDS) - set(FORBIDDEN_DECISION_KINDS) - set(AMOUNT_DECISION_KINDS))
)
def test_every_answerable_decision_kind_validates(kind):
    assert play_errors(mutate("moves.0.answers.1.decision", kind)) == []


@pytest.mark.parametrize("kind", ["choose_cardz", "Choose_Cards", "pay life", "", 7, None])
def test_a_decision_kind_outside_the_set_is_unknown_decision_kind(kind):
    doc = mutate("moves.0.answers.0.decision", kind)
    assert codes(doc) == ["unknown-decision-kind"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_an_answer_move_is_judged_the_same_way():
    doc = mutate("moves", [{"kind": "answer", "answers": [{"decision": "choose_cardz"}]}])
    assert codes(doc) == ["unknown-decision-kind"]


def test_a_decision_kind_typo_past_a_too_long_answers_list_is_still_judged():
    answers = [ANSWER] * 3 + [{"decision": "choose_cardz"}]
    assert codes(mutate("moves.0.answers", answers)) == [
        "bounds-exceeded",
        "unknown-decision-kind",
    ]


@pytest.mark.parametrize(
    "kind", ["assign_combat_damage", "declare_attackers", "declare_blockers", "mulligan"]
)
def test_answering_a_combat_declaration_or_a_mulligan_is_forbidden(kind):
    assert codes(mutate("moves.0.answers.0.decision", kind)) == ["forbidden-kind"]


# -- D08: every list bounded, no duplicate step, no wildcard beside keys ----------


def _selector_list(field: str):
    return lambda n: mutate("true.0.select", {field: ["Land"] * n})


BOUNDED_LISTS = [
    (
        "cast-targets",
        lambda n: play(moves=[cast_move(targets=[{"select": {}, "side": "opponent"}] * n)]),
        MAX_TARGETS,
    ),
    ("pick-prefer", lambda n: mutate("moves.0.answers.0.pick.prefer", [{}] * n), MAX_PREFER),
    (
        "opposing-archetypes",
        lambda n: mutate("opposing_archetypes", [f"deck-{index}" for index in range(n)]),
        MAX_ARCHETYPES,
    ),
    (
        "input-value-list",
        lambda n: play(
            true=[{"kind": "input", "name": "plan", "op": "in", "value": ["a"] * n}], false=[]
        ),
        MAX_INPUT_VALUES,
    ),
    *[
        (f"selector-{field}", _selector_list(field), MAX_SELECTOR_WORDS)
        for field in (
            "types_all",
            "types_any",
            "subtypes_any",
            "supertypes_any",
            "colors_any",
            "keywords_any",
        )
    ],
]


@pytest.mark.parametrize(
    ("build", "bound"),
    [(build, bound) for _, build, bound in BOUNDED_LISTS],
    ids=[name for name, _, _ in BOUNDED_LISTS],
)
def test_a_list_at_its_bound_is_fine_and_one_past_it_is_bounds_exceeded(build, bound):
    assert play_errors(build(bound)) == []
    assert codes(build(bound + 1)) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(build(bound + 1))


def test_the_battle_report_shapes_are_refused():
    assert codes(play(moves=[cast_move(targets=[{"select": {}, "side": "opponent"}] * 40)])) == [
        "bounds-exceeded"
    ]
    assert codes(mutate("moves.0.answers.0.pick.prefer", [{}] * 50)) == ["bounds-exceeded"]


def _arrays(schema: Any, where: str = "") -> list[tuple[str, dict[str, Any]]]:
    found = []
    if isinstance(schema, dict):
        if schema.get("type") == "array":
            found.append((where, schema))
        for key, value in schema.items():
            found += _arrays(value, f"{where}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            found += _arrays(value, f"{where}[{index}]")
    return found


def test_every_list_in_the_json_schema_has_max_items():
    arrays = _arrays(Play.model_json_schema())
    assert arrays
    unbounded = [where for where, node in arrays if "maxItems" not in node]
    assert unbounded == []


def test_a_repeated_step_is_duplicate_step():
    doc = mutate("when.steps", ["end_step", "end_step"])
    assert codes(doc) == ["duplicate-step"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)
    assert play_errors(mutate("when.steps", ["upkeep", "end_step"])) == []


@pytest.mark.parametrize(
    "keys", [["*", "esper-blink"], ["esper-blink", "*"], ["*", "*"], ["*", "a", "b"]]
)
def test_the_wildcard_beside_any_other_key_is_wildcard_with_keys(keys):
    doc = mutate("opposing_archetypes", keys)
    assert codes(doc) == ["wildcard-with-keys"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_the_wildcard_alone_and_named_keys_alone_are_fine():
    assert play_errors(mutate("opposing_archetypes", ["*"])) == []
    assert play_errors(mutate("opposing_archetypes", ["esper-blink", "dimir-frog"])) == []


# -- missing_from ----------------------------------------------------------------

MISSING = {"zones": ["battlefield", "hand"], "side": "me", "by": "subtype"}


def with_missing(missing: Any) -> dict[str, Any]:
    """The fetch-shock example whose first count selector carries `missing`."""
    return mutate("true.0.select", {"subtypes_any": ["Tower"], "missing_from": missing})


def test_the_missing_from_vocabularies_are_closed():
    assert MISSING_FROM_ZONES == (
        "battlefield",
        "graveyard",
        "exile",
        "hand",
        "stack",
        "library",
        "sideboard",
    )
    assert MISSING_FROM_SIDES == ("me", "opponent")
    assert MISSING_FROM_BY == ("subtype", "name")


@pytest.mark.parametrize("zone", MISSING_FROM_ZONES)
@pytest.mark.parametrize("side", MISSING_FROM_SIDES)
@pytest.mark.parametrize("by", MISSING_FROM_BY)
def test_every_missing_from_zone_side_and_by_validates(zone, side, by):
    doc = with_missing({"zones": [zone], "side": side, "by": by})
    assert play_errors(doc) == []
    Play.model_validate(doc)


def test_missing_from_is_a_selector_field_in_picks_prefer_and_conditions():
    doc = with_missing(MISSING)
    pick = doc["moves"][0]["answers"][0]["pick"]
    pick["select"]["missing_from"] = {**MISSING, "by": "name"}
    pick["prefer"] = [{"missing_from": {"zones": ["graveyard"], "side": "opponent", "by": "name"}}]
    assert play_errors(doc) == []


MALFORMED_MISSING = [
    ("no-zones", {**MISSING, "zones": []}, "bounds-exceeded"),
    ("eight-zones", {**MISSING, "zones": ["hand"] * 8}, "bounds-exceeded"),
    ("unknown-zone", {**MISSING, "zones": ["deck"]}, "malformed-play"),
    ("zones-not-a-list", {**MISSING, "zones": "hand"}, "malformed-play"),
    ("side-any", {**MISSING, "side": "any"}, "malformed-play"),
    ("by-type", {**MISSING, "by": "type"}, "malformed-play"),
    ("by-a-card-name", {**MISSING, "by": "Urza's Tower"}, "malformed-play"),
    ("no-by", {"zones": ["hand"], "side": "me"}, "malformed-play"),
    ("no-side", {"zones": ["hand"], "by": "name"}, "malformed-play"),
    ("extra-key", {**MISSING, "name": "Urza's Tower"}, "unknown-field"),
    ("not-an-object", "battlefield", "malformed-play"),
]


@pytest.mark.parametrize(
    "missing, code",
    [(missing, code) for _, missing, code in MALFORMED_MISSING],
    ids=[name for name, _, _ in MALFORMED_MISSING],
)
def test_a_malformed_missing_from_is_refused_with_its_code(missing, code):
    assert codes(with_missing(missing)) == [code]


def test_a_zone_past_a_too_long_missing_from_zones_list_is_still_judged():
    doc = with_missing({**MISSING, "zones": ["hand"] * 7 + ["deck"]})
    assert codes(doc) == ["bounds-exceeded", "malformed-play"]
