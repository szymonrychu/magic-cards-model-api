"""FD2's schema additions: `cast`/`activate` cost and X parameters, the new
`keyword_action` and `mulligan` move kinds, three schema-local steps
(`cleanup`, `mulligan`, `pregame_actions`), the new `order`/`damage`/
`choice_from` answer fields, `pick.count`/`targets` allowing 0, and
`FORBIDDEN_DECISION_KINDS` becoming empty.

This is the vocabulary FD3, FD4 and FD8 build on, so every shape, bound and
code here is exactly what
`docs/superpowers/specs/2026-09-13-play-script-format-design.md` and
`playscripts/schema.py` say (`tests/test_playscript_format_doc.py` pins the
doc side)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from magic_cards_model_api.playscripts.schema import (
    CHOICE_FROM_DECISION_KINDS,
    COLOR_LETTERS_CLOSED,
    DAMAGE_DECISION_KINDS,
    FORBIDDEN_DECISION_KINDS,
    KEYWORD_ACTIONS,
    MAX_DAMAGE_TIERS,
    MAX_MODES,
    MAX_OPTIONAL_COSTS,
    MAX_ORDER,
    MAX_TARGETS,
    MAX_X,
    ORDER_DECISION_KINDS,
    Play,
    play_errors,
)

BASE: dict[str, Any] = {
    "schema_version": 1,
    "id": "x",
    "name": "x",
    "priority": 10,
    "opposing_archetypes": ["*"],
    "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
    "true": [],
    "false": [],
    "moves": [],
    "next": [],
}


def play(**over: Any) -> dict[str, Any]:
    doc = copy.deepcopy(BASE)
    doc.update(over)
    return doc


def codes(doc: Any) -> list[str]:
    return sorted({error.code for error in play_errors(doc)})


def cast_move(**over: Any) -> dict[str, Any]:
    move: dict[str, Any] = {
        "kind": "cast",
        "card": {"zone": "hand", "select": {"types_any": ["Instant"]}},
    }
    move.update(over)
    return move


def activate_move(**over: Any) -> dict[str, Any]:
    move: dict[str, Any] = {
        "kind": "activate",
        "source": {"zone": "battlefield", "select": {}},
        "ability_index": 0,
    }
    move.update(over)
    return move


def answer_move(*answers: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "answer", "answers": list(answers)}


# == CastMove: face, modes, x, shares, optional_costs, cost-object Picks =====


def test_cast_face_is_a_selector():
    """The card-name ban on `face` is a lint-layer (corpus) rule, tested in
    `test_playscript_lint.py` alongside the other FD2 fields - `play_errors`
    alone judges only the pure schema shape."""
    doc = play(moves=[cast_move(face={"types_any": ["Instant"]})])
    assert play_errors(doc) == []


def test_cast_modes_bound_one_to_five():
    doc = play(moves=[cast_move(modes=[{"text_has": "deals damage"}])])
    assert play_errors(doc) == []
    doc = play(moves=[cast_move(modes=[{"text_has": "deals damage"}] * MAX_MODES)])
    assert play_errors(doc) == []
    doc = play(moves=[cast_move(modes=[])])
    assert codes(doc) == ["bounds-exceeded"]
    doc = play(moves=[cast_move(modes=[{"text_has": "deals damage"}] * (MAX_MODES + 1))])
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_cast_x_is_an_int_zero_to_thirty_or_max():
    doc = play(moves=[cast_move(x=0)])
    assert play_errors(doc) == []
    doc = play(moves=[cast_move(x=MAX_X)])
    assert play_errors(doc) == []
    doc = play(moves=[cast_move(x="max")])
    assert play_errors(doc) == []
    doc = play(moves=[cast_move(x=MAX_X + 1)])
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)
    doc = play(moves=[cast_move(x=-1)])
    assert codes(doc) == ["bounds-exceeded"]
    doc = play(moves=[cast_move(x="huge")])
    assert codes(doc) == ["malformed-play"]


def test_activate_x_is_the_same_shape():
    doc = play(moves=[activate_move(x="max")])
    assert play_errors(doc) == []
    doc = play(moves=[activate_move(x=MAX_X + 1)])
    assert codes(doc) == ["bounds-exceeded"]


def test_cast_shares_is_a_bounded_int_list_aligned_with_targets():
    doc = play(
        moves=[
            cast_move(
                targets=[
                    {"select": {}, "side": "opponent"},
                    {"select": {}, "side": "opponent"},
                ],
                shares=[2, 1],
            )
        ]
    )
    assert play_errors(doc) == []
    doc = play(moves=[cast_move(shares=[0] * (MAX_TARGETS + 1))])
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)
    doc = play(moves=[cast_move(shares=[-1])])
    assert codes(doc) == ["bounds-exceeded"]


def test_cast_optional_costs_bound_one_to_four():
    doc = play(moves=[cast_move(optional_costs=[{"text_has": "kicker"}])])
    assert play_errors(doc) == []
    doc = play(moves=[cast_move(optional_costs=[{"text_has": "kicker"}] * MAX_OPTIONAL_COSTS)])
    assert play_errors(doc) == []
    doc = play(moves=[cast_move(optional_costs=[])])
    assert codes(doc) == ["bounds-exceeded"]
    doc = play(
        moves=[cast_move(optional_costs=[{"text_has": "kicker"}] * (MAX_OPTIONAL_COSTS + 1))]
    )
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


@pytest.mark.parametrize(
    "field", ["sacrifice", "discard", "exile", "tap", "convoke", "delve", "improvise"]
)
def test_cast_cost_object_and_alt_cost_picks_validate(field):
    doc = play(moves=[cast_move(**{field: {"select": {"types_any": ["Creature"]}, "count": 1}})])
    assert play_errors(doc) == []


def test_activate_exile_is_a_pick():
    doc = play(moves=[activate_move(exile={"select": {"types_any": ["Creature"]}, "count": 1})])
    assert play_errors(doc) == []


def test_activate_color_is_wubrgc():
    for letter in COLOR_LETTERS_CLOSED:
        assert play_errors(play(moves=[activate_move(color=letter)])) == []
    doc = play(moves=[activate_move(color="w")])
    assert codes(doc) == ["malformed-play"]
    doc = play(moves=[activate_move(color="X")])
    assert codes(doc) == ["malformed-play"]


def test_color_letters_closed_are_the_six_colors():
    assert COLOR_LETTERS_CLOSED == ("W", "U", "B", "R", "G", "C")


# == keyword_action ==========================================================


@pytest.mark.parametrize("action", KEYWORD_ACTIONS)
def test_every_keyword_action_validates(action):
    doc = play(
        moves=[
            {
                "kind": "keyword_action",
                "action": action,
                "card": {"zone": "hand", "select": {}},
            }
        ]
    )
    assert play_errors(doc) == []


def test_keyword_action_covers_the_eight_routes():
    assert KEYWORD_ACTIONS == (
        "suspend",
        "plot",
        "foretell",
        "cycle",
        "equip",
        "crew",
        "turn_face_up",
        "companion",
    )


def test_keyword_action_outside_the_vocabulary_is_malformed():
    doc = play(
        moves=[
            {"kind": "keyword_action", "action": "unearth", "card": {"zone": "hand", "select": {}}}
        ]
    )
    assert codes(doc) == ["malformed-play"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_keyword_action_equip_takes_a_card_target():
    doc = play(
        moves=[
            {
                "kind": "keyword_action",
                "action": "equip",
                "card": {"zone": "battlefield", "select": {}},
                "target": {"select": {"types_any": ["Creature"]}, "side": "me"},
            }
        ]
    )
    assert play_errors(doc) == []


def test_keyword_action_crew_takes_crew_with():
    doc = play(
        moves=[
            {
                "kind": "keyword_action",
                "action": "crew",
                "card": {"zone": "battlefield", "select": {}},
                "crew_with": {"select": {"types_any": ["Creature"]}, "count": 1},
            }
        ]
    )
    assert play_errors(doc) == []


# == mulligan =================================================================


def test_mulligan_move_validates_in_the_mulligan_step():
    doc = play(
        when={"whose_turn": "mine", "steps": ["mulligan"], "stack": "empty"},
        moves=[{"kind": "mulligan", "keep": False, "bottom": {"select": {}, "count": 1}}],
    )
    assert play_errors(doc) == []


def test_mulligan_keep_without_bottom_validates():
    doc = play(
        when={"whose_turn": "mine", "steps": ["mulligan"], "stack": "empty"},
        moves=[{"kind": "mulligan", "keep": True}],
    )
    assert play_errors(doc) == []


def test_mulligan_outside_the_mulligan_step_is_refused():
    doc = play(moves=[{"kind": "mulligan", "keep": True}])
    assert codes(doc) == ["mulligan-wrong-step"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_mulligan_with_other_steps_alongside_mulligan_is_still_refused():
    doc = play(
        when={"whose_turn": "mine", "steps": ["mulligan", "precombat_main"], "stack": "empty"},
        moves=[{"kind": "mulligan", "keep": True}],
    )
    assert codes(doc) == ["mulligan-wrong-step"]


def test_mulligan_requires_keep():
    doc = play(
        when={"whose_turn": "mine", "steps": ["mulligan"], "stack": "empty"},
        moves=[{"kind": "mulligan"}],
    )
    assert codes(doc) == ["malformed-play"]


# == schema-local steps =======================================================


@pytest.mark.parametrize("step", ["cleanup", "mulligan", "pregame_actions"])
def test_schema_local_steps_are_accepted_in_when_steps(step):
    doc = play(when={"whose_turn": "mine", "steps": [step], "stack": "empty"})
    assert play_errors(doc) == []


def test_opening_hand_action_answers_under_pregame_actions_step():
    doc = play(
        when={"whose_turn": "mine", "steps": ["pregame_actions"], "stack": "empty"},
        moves=[answer_move({"decision": "opening_hand_action", "accept": True})],
    )
    assert play_errors(doc) == []


# == FORBIDDEN_DECISION_KINDS is empty; mulligan/assign_combat_damage =========


def test_forbidden_decision_kinds_is_empty():
    assert FORBIDDEN_DECISION_KINDS == ()


def test_mulligan_answers_plainly_with_accept():
    doc = play(moves=[answer_move({"decision": "mulligan", "accept": True})])
    assert play_errors(doc) == []


# == Answer.order (order_triggers) ===========================================


def test_order_bound_one_to_six():
    doc = play(moves=[answer_move({"decision": "order_triggers", "order": [{}]})])
    assert play_errors(doc) == []
    doc = play(moves=[answer_move({"decision": "order_triggers", "order": [{}] * MAX_ORDER})])
    assert play_errors(doc) == []
    doc = play(moves=[answer_move({"decision": "order_triggers", "order": []})])
    assert codes(doc) == ["bounds-exceeded"]
    doc = play(moves=[answer_move({"decision": "order_triggers", "order": [{}] * (MAX_ORDER + 1)})])
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_order_direction_is_documented_and_only_order_triggers_takes_it():
    doc = play(moves=[answer_move({"decision": "scry", "order": [{}]})])
    assert codes(doc) == ["answer-shape-mismatch"]


def test_order_decision_kinds_is_order_triggers_alone():
    assert ORDER_DECISION_KINDS == ("order_triggers",)


# == Answer.damage (assign_combat_damage) ====================================


def test_damage_answers_assign_combat_damage_alone_and_is_required():
    doc = play(
        moves=[
            answer_move(
                {
                    "decision": "assign_combat_damage",
                    "damage": {
                        "tiers": [{"types_any": ["Creature"]}],
                        "lethal_first": True,
                        "rest_to": "last_blocker",
                    },
                }
            )
        ]
    )
    assert play_errors(doc) == []
    missing = play(moves=[answer_move({"decision": "assign_combat_damage"})])
    assert codes(missing) == ["answer-shape-mismatch"]
    extra = play(
        moves=[
            answer_move(
                {
                    "decision": "assign_combat_damage",
                    "damage": {"tiers": []},
                    "accept": True,
                }
            )
        ]
    )
    assert codes(extra) == ["answer-shape-mismatch"]


def test_damage_bare_defaults_validate():
    doc = play(moves=[answer_move({"decision": "assign_combat_damage", "damage": {}})])
    assert play_errors(doc) == []


def test_damage_tiers_bound_zero_to_max():
    doc = play(
        moves=[
            answer_move(
                {
                    "decision": "assign_combat_damage",
                    "damage": {"tiers": [{}] * MAX_DAMAGE_TIERS},
                }
            )
        ]
    )
    assert play_errors(doc) == []
    doc = play(
        moves=[
            answer_move(
                {
                    "decision": "assign_combat_damage",
                    "damage": {"tiers": [{}] * (MAX_DAMAGE_TIERS + 1)},
                }
            )
        ]
    )
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_damage_rest_to_outside_the_vocabulary_is_malformed():
    doc = play(
        moves=[
            answer_move({"decision": "assign_combat_damage", "damage": {"rest_to": "graveyard"}})
        ]
    )
    assert codes(doc) == ["malformed-play"]


def test_only_assign_combat_damage_takes_damage():
    doc = play(moves=[answer_move({"decision": "may", "damage": {}})])
    assert codes(doc) == ["answer-shape-mismatch"]


def test_damage_decision_kinds_is_assign_combat_damage_alone():
    assert DAMAGE_DECISION_KINDS == ("assign_combat_damage",)


# == Answer.choice_from (choose_a_quality) ===================================


def test_choice_from_validates_for_choose_a_quality():
    doc = play(
        moves=[
            answer_move(
                {
                    "decision": "choose_a_quality",
                    "choice_from": {
                        "zone": "battlefield",
                        "side": "me",
                        "select": {"types_any": ["Creature"]},
                    },
                }
            )
        ]
    )
    assert play_errors(doc) == []


def test_choice_from_and_choice_are_mutually_exclusive():
    doc = play(
        moves=[
            answer_move(
                {
                    "decision": "choose_a_quality",
                    "choice": "Elf",
                    "choice_from": {"zone": "battlefield", "side": "me", "select": {}},
                }
            )
        ]
    )
    assert codes(doc) == ["answer-shape-mismatch"]


def test_choice_from_on_choose_a_mode_is_a_mismatch():
    doc = play(
        moves=[
            answer_move(
                {
                    "decision": "choose_a_mode",
                    "choice_from": {"zone": "battlefield", "side": "me", "select": {}},
                }
            )
        ]
    )
    assert codes(doc) == ["answer-shape-mismatch"]


def test_choice_from_decision_kinds_is_choose_a_quality_alone():
    assert CHOICE_FROM_DECISION_KINDS == ("choose_a_quality",)


# == choose-zero: pick.count and targets allow 0 =============================


def test_pick_count_zero_validates():
    doc = play(
        moves=[answer_move({"decision": "choose_cards", "pick": {"select": {}, "count": 0}})]
    )
    assert play_errors(doc) == []


def test_pick_count_negative_is_still_bounds_exceeded():
    doc = play(
        moves=[answer_move({"decision": "choose_cards", "pick": {"select": {}, "count": -1}})]
    )
    assert codes(doc) == ["bounds-exceeded"]


def test_choose_targets_zero_targets_validates():
    doc = play(moves=[answer_move({"decision": "choose_targets", "targets": []})])
    assert play_errors(doc) == []


def test_choose_players_zero_players_validates():
    doc = play(moves=[answer_move({"decision": "choose_players", "players": []})])
    assert play_errors(doc) == []
