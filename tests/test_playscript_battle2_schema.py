"""Battle 2's schema gaps (B2-10): amounts, activation targets, sacrifice choices.

Galvanic Discharge pends `pay_energy_amount`, whose answer is an integer (the
api's `ANSWER_FIELD_FOR_KIND` names `amount` for it and for
`pay_life_amount`), yet an answer item could only say `accept` - and both gates
passed `{"decision": "pay_energy_amount", "accept": true}`. Goblin
Bombardment's "Sacrifice a creature: ... any target" could name neither the
creature nor the target. Each new shape is closed and validated, and an answer
whose fields do not fit its decision kind is `answer-shape-mismatch`.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from playscript_testkit import FETCH_SHOCK, play
from pydantic import ValidationError

from magic_cards_model_api.decisions import ANSWER_FIELD_FOR_KIND
from magic_cards_model_api.playscripts.lint import CardIndex, check_play
from magic_cards_model_api.playscripts.schema import (
    AMOUNT_DECISION_KINDS,
    CODES,
    MAX_TARGETS,
    Play,
    json_schema,
    play_errors,
)


def codes(doc: Any) -> list[str]:
    return sorted({error.code for error in play_errors(doc)})


def answer_move(*answers: dict[str, Any]) -> dict[str, Any]:
    return play(moves=[{"kind": "answer", "answers": list(answers)}])


def activate(**over: Any) -> dict[str, Any]:
    move: dict[str, Any] = {
        "kind": "activate",
        "source": {"zone": "battlefield", "select": {"types_any": ["Enchantment"]}},
        "ability_text_has": "Sacrifice a creature",
    }
    move.update(over)
    return play(moves=[move])


# -- amounts ----------------------------------------------------------------------


def test_the_amount_decision_kinds_are_the_ones_the_api_answers_with_an_amount():
    assert set(AMOUNT_DECISION_KINDS) == {
        kind.value for kind, field in ANSWER_FIELD_FOR_KIND.items() if field == "amount"
    }
    assert set(AMOUNT_DECISION_KINDS) == {
        "pay_energy_amount",
        "pay_life_amount",
        "choose_an_amount",
    }


@pytest.mark.parametrize("kind", ["pay_energy_amount", "pay_life_amount", "choose_an_amount"])
@pytest.mark.parametrize("amount", [0, 2, 3, "max"])
def test_an_amount_decision_takes_an_integer_or_max(kind, amount):
    doc = answer_move({"decision": kind, "amount": amount})
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].answers[0].amount == amount


@pytest.mark.parametrize("kind", ["pay_energy_amount", "pay_life_amount", "choose_an_amount"])
def test_accept_on_an_amount_decision_is_answer_shape_mismatch(kind):
    doc = answer_move({"decision": kind, "accept": True})
    assert codes(doc) == ["answer-shape-mismatch"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_accept_beside_an_amount_is_still_refused():
    doc = answer_move({"decision": "pay_energy_amount", "amount": 2, "accept": True})
    assert codes(doc) == ["answer-shape-mismatch"]


@pytest.mark.parametrize(
    "extra",
    [{"pick": {"select": {}}}, {"to_graveyard": {}}, {"to_bottom": {}}],
)
def test_an_amount_decision_takes_no_other_answer_field(extra):
    assert codes(answer_move({"decision": "pay_life_amount", "amount": 1, **extra})) == [
        "answer-shape-mismatch"
    ]


def test_an_amount_decision_without_an_amount_is_answer_shape_mismatch():
    assert codes(answer_move({"decision": "pay_energy_amount"})) == ["answer-shape-mismatch"]


@pytest.mark.parametrize("kind", ["pay_energy", "pay_life", "may", "choose_cards", "scry"])
def test_an_amount_on_any_other_decision_is_answer_shape_mismatch(kind):
    assert codes(answer_move({"decision": kind, "amount": 1})) == ["answer-shape-mismatch"]


def test_a_negative_amount_is_bounds_exceeded():
    assert codes(answer_move({"decision": "pay_energy_amount", "amount": -1})) == [
        "bounds-exceeded"
    ]


@pytest.mark.parametrize("amount", [True, "3", "all", 2.0, None, "min"])
def test_an_amount_that_is_not_an_integer_or_max_is_malformed(amount):
    assert codes(answer_move({"decision": "pay_energy_amount", "amount": amount})) == [
        "malformed-play"
    ]


def test_the_shape_mismatch_is_reported_beside_another_error_in_the_same_answer():
    doc = answer_move({"decision": "pay_energy_amount", "accept": True, "pick": {"count": 0}})
    assert set(codes(doc)) >= {"answer-shape-mismatch"}


def test_the_battle_report_shape_is_refused_by_the_lint_gate_too():
    doc = copy.deepcopy(FETCH_SHOCK)
    doc["id"] = "discharge-planeswalker"
    doc["true"] = [{"kind": "life", "side": "me", "op": ">=", "n": 1}]
    doc["moves"] = [
        {
            "kind": "answer",
            "answers": [{"decision": "pay_energy_amount", "accept": True}],
        }
    ]
    errors = check_play(doc, "discharge-planeswalker", "plays/x.json", CardIndex({}))
    assert [error.code for error in errors] == ["answer-shape-mismatch"]
    assert "answers[0]" in errors[0].message


def test_the_new_code_is_appended_to_the_shared_contract():
    assert "answer-shape-mismatch" in CODES


# -- activation targets -----------------------------------------------------------


def test_an_activation_names_a_card_target_and_a_player_target():
    doc = activate(
        targets=[
            {"select": {"types_any": ["Creature"]}, "side": "opponent"},
            {"player": "opponent"},
        ]
    )
    assert play_errors(doc) == []
    targets = Play.model_validate(doc).moves[0].targets
    assert targets[0].side == "opponent" and targets[1].player == "opponent"


def test_a_cast_names_a_player_target_too():
    doc = play(
        moves=[
            {
                "kind": "cast",
                "card": {"zone": "hand", "select": {"types_any": ["Instant"]}},
                "targets": [{"player": "opponent"}],
            }
        ]
    )
    assert play_errors(doc) == []


@pytest.mark.parametrize(
    "target",
    [
        {"player": "any"},
        {"player": "me", "side": "me"},
        {"player": "opponent", "select": {}},
        {"select": {}},
        {"side": "me"},
        {},
        "opponent",
    ],
)
def test_a_target_is_a_card_with_a_side_or_a_player_and_nothing_else(target):
    assert codes(activate(targets=[target])) in (["malformed-play"], ["unknown-field"])


def test_an_unknown_key_inside_a_card_target_is_unknown_field():
    assert codes(activate(targets=[{"select": {"name": "Bolt"}, "side": "me"}])) == [
        "unknown-field"
    ]


def test_activation_targets_are_bounded_like_cast_targets():
    target = {"player": "opponent"}
    assert play_errors(activate(targets=[target] * MAX_TARGETS)) == []
    assert codes(activate(targets=[target] * (MAX_TARGETS + 1))) == ["bounds-exceeded"]


def test_items_past_a_too_long_target_list_are_still_judged():
    targets = [{"player": "opponent"}] * MAX_TARGETS + [{"select": {"nope": 1}, "side": "me"}]
    assert codes(activate(targets=targets)) == ["bounds-exceeded", "unknown-field"]


# -- sacrifice choices ------------------------------------------------------------


@pytest.mark.parametrize("sacrifice", ["source", "none"])
def test_the_sacrifice_keywords_still_validate(sacrifice):
    assert play_errors(activate(sacrifice=sacrifice)) == []


def test_a_sacrifice_names_what_to_sacrifice_by_selector():
    doc = activate(
        sacrifice={
            "select": {"types_any": ["Creature"], "power": {"op": "<=", "n": 1}},
            "count": 1,
            "prefer": [{"subtypes_any": ["Goblin"]}],
        }
    )
    assert play_errors(doc) == []
    choice = Play.model_validate(doc).moves[0].sacrifice
    assert choice.select.types_any == ["Creature"] and choice.count == 1


@pytest.mark.parametrize(
    ("sacrifice", "code"),
    [
        ("itself", "malformed-play"),
        ({"count": 1}, "malformed-play"),
        ({"select": {}, "count": 0}, "bounds-exceeded"),
        ({"select": {}, "side": "me"}, "unknown-field"),
        ({"select": {"card_name": "x"}}, "unknown-field"),
        ({"select": {}, "prefer": [{}] * 5}, "bounds-exceeded"),
    ],
)
def test_a_malformed_sacrifice_choice_is_refused_with_its_code(sacrifice, code):
    assert codes(activate(sacrifice=sacrifice)) == [code]


def test_the_error_location_names_the_sacrifice_field():
    (error,) = play_errors(activate(sacrifice={"select": {}, "count": 0}))
    assert "moves[0].sacrifice.count" in error.message


# -- the exported JSON Schema carries every new shape ------------------------------


def test_the_json_schema_names_amount_player_targets_and_the_sacrifice_choice():
    text = str(json_schema())
    assert "'amount'" in text and "'player'" in text and "'max'" in text
    defs = json_schema()["$defs"]
    assert "PlayerTarget" in defs and "SacrificeChoice" in defs
