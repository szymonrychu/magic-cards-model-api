"""FB2 B4's schema additions: `pass`/`declare_attackers`/`declare_blockers`
moves, scry `top_order`, `position` for `put_on_top_or_bottom`,
`choose_targets` `targets`, `CardTarget` `prefer`/`ties`/`zone`,
`pending_decision`, `compare` and `all_of` conditions, and
`activate.ability_index`.

This is the vocabulary FC1, FC5, FC6, FC7 and FC8 build on, so every shape,
bound and code here is exactly what
`docs/superpowers/specs/2026-09-13-play-script-format-design.md` and
`playscripts/schema.py` say (`tests/test_playscript_format_doc.py` pins the
doc side; `tests/test_playscript_battle2_schema.py`/`test_playscript_schema.py`
cover the vocabulary this one builds on)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from playscript_testkit import play
from pydantic import ValidationError

from magic_cards_model_api.playscripts.schema import (
    CODES,
    COMBAT_DECISION_KINDS,
    MAX_ABILITY_INDEX,
    MAX_ALL_OF_CONDITIONS,
    MAX_ATTACK_GROUPS,
    MAX_BLOCKS,
    MAX_PREFER,
    MAX_TARGETS,
    MAX_TOP_ORDER,
    MIN_ALL_OF_CONDITIONS,
    OPERAND_KINDS,
    POSITION_DECISION_KINDS,
    TARGET_ZONES,
    TOP_ORDER_DECISION_KINDS,
    Play,
    play_errors,
)


def codes(doc: Any) -> list[str]:
    return sorted({error.code for error in play_errors(doc)})


def cast_move(**over: Any) -> dict[str, Any]:
    move: dict[str, Any] = {
        "kind": "cast",
        "card": {"zone": "hand", "select": {"types_any": ["Creature"]}},
    }
    move.update(over)
    return move


def answer_move(*answers: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "answer", "answers": list(answers)}


# == M1: pass ==================================================================


def test_pass_alone_is_a_legal_do_nothing_play():
    doc = play(moves=[{"kind": "pass"}])
    assert play_errors(doc) == []
    model = Play.model_validate(doc)
    assert model.moves[0].kind == "pass"


def test_pass_after_another_move_is_legal_when_last():
    doc = play(moves=[cast_move(), {"kind": "pass"}])
    assert play_errors(doc) == []


def test_pass_not_last_is_refused():
    doc = play(moves=[{"kind": "pass"}, cast_move()])
    assert codes(doc) == ["pass-not-last"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_pass_takes_no_answers_field():
    doc = play(moves=[{"kind": "pass", "answers": []}])
    assert codes(doc) == ["unknown-field"]


# == M2: declare_attackers =====================================================


def test_attackers_empty_list_is_a_legal_declare_attackers_move():
    doc = play(moves=[{"kind": "declare_attackers", "attackers": []}])
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].attackers == []


def test_attackers_field_defaults_to_empty_when_left_out():
    doc = play(moves=[{"kind": "declare_attackers"}])
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].attackers == []


def test_an_attack_group_validates_with_every_field():
    doc = play(
        moves=[
            {
                "kind": "declare_attackers",
                "attackers": [
                    {
                        "select": {"types_any": ["Creature"]},
                        "count": 2,
                        "prefer": [{"power": {"op": ">=", "n": 3}}],
                        "ties": "any",
                        "defender": {"select": {"types_any": ["Planeswalker"]}, "side": "opponent"},
                    }
                ],
            }
        ]
    )
    assert play_errors(doc) == []
    group = Play.model_validate(doc).moves[0].attackers[0]
    assert group.count == 2 and group.ties == "any"
    assert group.defender.side == "opponent"


def test_an_attack_group_with_no_defender_is_valid_and_means_the_opponent():
    doc = play(moves=[{"kind": "declare_attackers", "attackers": [{"select": {}}]}])
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].attackers[0].defender is None


def test_an_attack_group_defender_player_shape_validates():
    doc = play(
        moves=[
            {
                "kind": "declare_attackers",
                "attackers": [{"select": {}, "defender": {"player": "opponent"}}],
            }
        ]
    )
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].attackers[0].defender.player == "opponent"


def test_an_attack_group_count_takes_all():
    doc = play(moves=[{"kind": "declare_attackers", "attackers": [{"select": {}, "count": "all"}]}])
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].attackers[0].count == "all"


def test_an_attack_group_count_zero_is_bounds_exceeded():
    doc = play(moves=[{"kind": "declare_attackers", "attackers": [{"select": {}, "count": 0}]}])
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_an_attack_group_count_outside_all_or_int_is_malformed():
    doc = play(
        moves=[{"kind": "declare_attackers", "attackers": [{"select": {}, "count": "some"}]}]
    )
    assert codes(doc) == ["malformed-play"]


def test_attack_groups_are_bounded_at_six():
    group = {"select": {}}
    at_bound = play(moves=[{"kind": "declare_attackers", "attackers": [group] * MAX_ATTACK_GROUPS}])
    assert play_errors(at_bound) == []
    over = play(
        moves=[{"kind": "declare_attackers", "attackers": [group] * (MAX_ATTACK_GROUPS + 1)}]
    )
    assert codes(over) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(over)


def test_attack_group_prefer_is_bounded_at_four():
    at_bound = play(
        moves=[
            {
                "kind": "declare_attackers",
                "attackers": [{"select": {}, "prefer": [{}] * MAX_PREFER}],
            }
        ]
    )
    assert play_errors(at_bound) == []
    over = play(
        moves=[
            {
                "kind": "declare_attackers",
                "attackers": [{"select": {}, "prefer": [{}] * (MAX_PREFER + 1)}],
            }
        ]
    )
    assert codes(over) == ["bounds-exceeded"]


def test_declare_attackers_must_be_the_first_move():
    doc = play(moves=[cast_move(), {"kind": "declare_attackers", "attackers": []}])
    assert codes(doc) == ["combat-move-not-first"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_declare_attackers_as_the_only_first_move_is_fine():
    doc = play(moves=[{"kind": "declare_attackers", "attackers": []}])
    assert play_errors(doc) == []


# == M3: declare_blockers =======================================================


def test_blocks_empty_list_is_a_legal_declare_blockers_move():
    doc = play(moves=[{"kind": "declare_blockers", "blocks": []}])
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].blocks == []


def test_blocks_field_defaults_to_empty_when_left_out():
    doc = play(moves=[{"kind": "declare_blockers"}])
    assert play_errors(doc) == []


def test_a_block_validates_with_every_field():
    doc = play(
        moves=[
            {
                "kind": "declare_blockers",
                "blocks": [
                    {
                        "blocker": {
                            "select": {"types_any": ["Creature"]},
                            "count": 2,
                            "prefer": [{"power": {"op": ">=", "n": 3}}],
                            "ties": "any",
                        },
                        "attacker": {
                            "select": {"power": {"op": ">=", "n": 4}},
                            "prefer": [{"keywords_any": ["Trample"]}],
                            "ties": "any",
                        },
                    }
                ],
            }
        ]
    )
    assert play_errors(doc) == []
    block = Play.model_validate(doc).moves[0].blocks[0]
    assert block.blocker.count == 2 and block.attacker.ties == "any"


def test_a_block_blocker_count_zero_now_validates():
    """FD2 item 6: `Pick.count` (which `BlockerPick` extends) now allows 0 -
    "choose none" - so a block naming zero blockers is a legal, if pointless,
    shape rather than a bound violation."""
    doc = play(
        moves=[
            {
                "kind": "declare_blockers",
                "blocks": [{"blocker": {"select": {}, "count": 0}, "attacker": {"select": {}}}],
            }
        ]
    )
    assert play_errors(doc) == []


def test_a_block_attacker_takes_no_count():
    doc = play(
        moves=[
            {
                "kind": "declare_blockers",
                "blocks": [{"blocker": {"select": {}}, "attacker": {"select": {}, "count": 1}}],
            }
        ]
    )
    assert codes(doc) == ["unknown-field"]


def test_blocks_are_bounded_at_six():
    block = {"blocker": {"select": {}}, "attacker": {"select": {}}}
    at_bound = play(moves=[{"kind": "declare_blockers", "blocks": [block] * MAX_BLOCKS}])
    assert play_errors(at_bound) == []
    over = play(moves=[{"kind": "declare_blockers", "blocks": [block] * (MAX_BLOCKS + 1)}])
    assert codes(over) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(over)


def test_declare_blockers_must_be_the_first_move():
    doc = play(moves=[cast_move(), {"kind": "declare_blockers", "blocks": []}])
    assert codes(doc) == ["combat-move-not-first"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_at_most_one_combat_move_per_play():
    """A second combat move can never also be first, so
    `combat-move-not-first` alone already proves at most one per play."""
    doc = play(
        moves=[
            {"kind": "declare_attackers", "attackers": []},
            {"kind": "declare_blockers", "blocks": []},
        ]
    )
    assert codes(doc) == ["combat-move-not-first"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


# == M4: activate.ability_index =================================================


def activate(**over: Any) -> dict[str, Any]:
    move: dict[str, Any] = {
        "kind": "activate",
        "source": {"zone": "battlefield", "select": {"types_any": ["Enchantment"]}},
    }
    move.update(over)
    return move


def test_ability_index_alone_validates():
    doc = play(moves=[activate(ability_index=0)])
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].ability_index == 0


def test_ability_text_has_alone_still_validates():
    doc = play(moves=[activate(ability_text_has="Sacrifice a creature")])
    assert play_errors(doc) == []


def test_both_ability_text_has_and_ability_index_is_activate_ability_ref():
    doc = play(moves=[activate(ability_text_has="Sacrifice a creature", ability_index=0)])
    assert codes(doc) == ["activate-ability-ref"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_neither_ability_text_has_nor_ability_index_is_activate_ability_ref():
    doc = play(moves=[activate()])
    assert codes(doc) == ["activate-ability-ref"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


@pytest.mark.parametrize("index", [0, MAX_ABILITY_INDEX])
def test_ability_index_at_its_bounds_validates(index):
    doc = play(moves=[activate(ability_index=index)])
    assert play_errors(doc) == []


@pytest.mark.parametrize("index", [-1, MAX_ABILITY_INDEX + 1])
def test_ability_index_past_its_bounds_is_bounds_exceeded(index):
    doc = play(moves=[activate(ability_index=index)])
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


# == A1: top_order, position ====================================================


@pytest.mark.parametrize("decision", TOP_ORDER_DECISION_KINDS)
def test_top_order_validates_for_scry_and_surveil(decision):
    doc = play(
        moves=[
            answer_move(
                {
                    "decision": decision,
                    "top_order": [{"types_any": ["Land"]}, {"colors_any": ["R"]}],
                }
            )
        ]
    )
    assert play_errors(doc) == []
    top_order = Play.model_validate(doc).moves[0].answers[0].top_order
    assert len(top_order) == 2


def test_top_order_on_a_non_scry_surveil_kind_is_answer_shape_mismatch():
    doc = play(moves=[answer_move({"decision": "may", "top_order": [{}]})])
    assert codes(doc) == ["answer-shape-mismatch"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_top_order_is_bounded_at_four_tiers():
    at_bound = play(moves=[answer_move({"decision": "scry", "top_order": [{}] * MAX_TOP_ORDER})])
    assert play_errors(at_bound) == []
    over = play(moves=[answer_move({"decision": "scry", "top_order": [{}] * (MAX_TOP_ORDER + 1)})])
    assert codes(over) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(over)


@pytest.mark.parametrize("position", ["top", "bottom"])
def test_position_validates_for_put_on_top_or_bottom(position):
    doc = play(moves=[answer_move({"decision": "put_on_top_or_bottom", "position": position})])
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].answers[0].position == position


def test_position_on_a_kind_other_than_put_on_top_or_bottom_is_answer_shape_mismatch():
    doc = play(moves=[answer_move({"decision": "scry", "position": "top"})])
    assert codes(doc) == ["answer-shape-mismatch"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_position_beside_to_bottom_is_answer_shape_mismatch():
    doc = play(
        moves=[
            answer_move(
                {
                    "decision": "put_on_top_or_bottom",
                    "position": "top",
                    "to_bottom": {"types_any": ["Land"]},
                }
            )
        ]
    )
    assert codes(doc) == ["answer-shape-mismatch"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_position_outside_top_or_bottom_is_malformed():
    doc = play(moves=[answer_move({"decision": "put_on_top_or_bottom", "position": "middle"})])
    assert codes(doc) == ["malformed-play"]


def test_position_decision_kinds_is_exactly_put_on_top_or_bottom():
    assert POSITION_DECISION_KINDS == ("put_on_top_or_bottom",)


# == A2: choose_targets.targets ==================================================


def test_choose_targets_answer_names_a_player_target():
    doc = play(
        moves=[answer_move({"decision": "choose_targets", "targets": [{"player": "opponent"}]})]
    )
    assert play_errors(doc) == []
    targets = Play.model_validate(doc).moves[0].answers[0].targets
    assert targets[0].player == "opponent"


def test_choose_targets_answer_names_a_card_target():
    doc = play(
        moves=[
            answer_move(
                {
                    "decision": "choose_targets",
                    "targets": [{"select": {"types_any": ["Creature"]}, "side": "opponent"}],
                }
            )
        ]
    )
    assert play_errors(doc) == []


def test_targets_on_a_kind_other_than_choose_targets_is_answer_shape_mismatch():
    doc = play(moves=[answer_move({"decision": "may", "targets": [{"player": "me"}]})])
    assert codes(doc) == ["answer-shape-mismatch"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_targets_beside_pick_is_answer_shape_mismatch():
    doc = play(
        moves=[
            answer_move(
                {
                    "decision": "choose_targets",
                    "targets": [{"player": "opponent"}],
                    "pick": {"select": {}},
                }
            )
        ]
    )
    assert codes(doc) == ["answer-shape-mismatch"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_choose_targets_targets_is_bounded_zero_to_six():
    """FD2 item 6: `targets` now allows 0 - "choose none"."""
    one = play(moves=[answer_move({"decision": "choose_targets", "targets": [{"player": "me"}]})])
    assert play_errors(one) == []
    six = play(
        moves=[
            answer_move({"decision": "choose_targets", "targets": [{"player": "me"}] * MAX_TARGETS})
        ]
    )
    assert play_errors(six) == []
    empty = play(moves=[answer_move({"decision": "choose_targets", "targets": []})])
    assert play_errors(empty) == []
    seven = play(
        moves=[
            answer_move(
                {
                    "decision": "choose_targets",
                    "targets": [{"player": "me"}] * (MAX_TARGETS + 1),
                }
            )
        ]
    )
    assert codes(seven) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(seven)


# == T1: CardTarget prefer/ties/zone =============================================


def test_a_card_target_takes_prefer_ties_and_zone():
    doc = play(
        moves=[
            cast_move(
                targets=[
                    {
                        "select": {"types_any": ["Creature"]},
                        "side": "opponent",
                        "prefer": [{"power": {"op": ">=", "n": 3}}],
                        "ties": "any",
                        "zone": "graveyard",
                    }
                ]
            )
        ]
    )
    assert play_errors(doc) == []
    target = Play.model_validate(doc).moves[0].targets[0]
    assert target.zone == "graveyard" and target.ties == "any"


@pytest.mark.parametrize("zone", TARGET_ZONES)
def test_every_target_zone_validates(zone):
    doc = play(moves=[cast_move(targets=[{"select": {}, "side": "me", "zone": zone}])])
    assert play_errors(doc) == []


def test_a_target_zone_outside_the_closed_set_is_malformed():
    doc = play(moves=[cast_move(targets=[{"select": {}, "side": "me", "zone": "hand"}])])
    assert codes(doc) == ["malformed-play"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_a_target_left_with_no_zone_still_validates():
    doc = play(moves=[cast_move(targets=[{"select": {}, "side": "opponent"}])])
    assert play_errors(doc) == []
    assert Play.model_validate(doc).moves[0].targets[0].zone is None


# == C1: pending_decision =======================================================


def test_pending_decision_condition_validates():
    doc = play(true=[{"kind": "pending_decision", "decision": "scry"}], false=[])
    assert play_errors(doc) == []
    assert Play.model_validate(doc).true[0].decision == "scry"


def test_pending_decision_may_name_a_combat_decision_kind():
    """Checking WHAT is pending is not the same as answering it - a
    `pending_decision` may name `declare_attackers`/`declare_blockers` even
    though an answer item never may (FB2 D1)."""
    doc = play(true=[{"kind": "pending_decision", "decision": "declare_attackers"}], false=[])
    assert play_errors(doc) == []


def test_pending_decision_unknown_kind_is_unknown_decision_kind():
    doc = play(true=[{"kind": "pending_decision", "decision": "choose_cardz"}], false=[])
    assert codes(doc) == ["unknown-decision-kind"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


# == C2: compare =================================================================


def test_compare_count_operand_validates_and_defaults_zone_to_battlefield():
    doc = play(
        true=[
            {
                "kind": "compare",
                "left": {"of": "count", "select": {"types_any": ["Creature"]}, "side": "me"},
                "op": ">=",
                "right": {"of": "n", "n": 3},
            }
        ],
        false=[],
    )
    assert play_errors(doc) == []
    left = Play.model_validate(doc).true[0].left
    assert left.zone == "battlefield"


@pytest.mark.parametrize(
    "operand",
    [
        {"of": "count", "select": {}, "zone": "graveyard", "side": "me"},
        {"of": "zone_size", "zone": "hand", "side": "me"},
        {"of": "life", "side": "opponent"},
        {"of": "untapped_mana", "side": "me"},
        {"of": "power_sum", "select": {"types_any": ["Creature"]}, "side": "me"},
        {"of": "toughness_sum", "select": {"types_any": ["Creature"]}, "side": "opponent"},
        {"of": "n", "n": 5},
    ],
)
def test_every_operand_kind_validates_on_both_sides(operand):
    doc = play(
        true=[{"kind": "compare", "left": operand, "op": ">=", "right": {"of": "n", "n": 0}}],
        false=[],
    )
    assert play_errors(doc) == []


def test_operand_kinds_are_exactly_seven():
    assert OPERAND_KINDS == (
        "count",
        "zone_size",
        "life",
        "untapped_mana",
        "power_sum",
        "toughness_sum",
        "n",
    )


def test_power_sum_covers_board_power_against_life():
    """The motivating case: my board's summed power against the opponent's
    life total, in one condition."""
    doc = play(
        true=[
            {
                "kind": "compare",
                "left": {"of": "power_sum", "select": {"types_any": ["Creature"]}, "side": "me"},
                "op": ">=",
                "right": {"of": "life", "side": "opponent"},
            }
        ],
        false=[],
    )
    assert play_errors(doc) == []


@pytest.mark.parametrize("zone", ["hand", "library", "sideboard"])
def test_a_count_operand_over_a_hidden_zone_must_name_a_side(zone):
    doc = play(
        true=[
            {
                "kind": "compare",
                "left": {"of": "count", "select": {}, "zone": zone, "side": "any"},
                "op": ">=",
                "right": {"of": "n", "n": 0},
            }
        ],
        false=[],
    )
    assert codes(doc) == ["hidden-zone-side-any"]
    with pytest.raises(ValidationError) as raised:
        Play.model_validate(doc)
    assert "hidden_zone_side_any" in {e["type"] for e in raised.value.errors()}


@pytest.mark.parametrize("zone", ["hand", "library", "sideboard"])
@pytest.mark.parametrize("side", ["me", "opponent"])
def test_a_count_operand_over_a_hidden_zone_with_a_side_is_fine(zone, side):
    doc = play(
        true=[
            {
                "kind": "compare",
                "left": {"of": "count", "select": {}, "zone": zone, "side": side},
                "op": ">=",
                "right": {"of": "n", "n": 0},
            }
        ],
        false=[],
    )
    assert play_errors(doc) == []


def test_a_zone_size_operand_never_takes_side_any():
    doc = play(
        true=[
            {
                "kind": "compare",
                "left": {"of": "zone_size", "zone": "hand", "side": "any"},
                "op": ">=",
                "right": {"of": "n", "n": 0},
            }
        ],
        false=[],
    )
    assert codes(doc) == ["malformed-play"]


def test_an_operand_of_outside_the_vocabulary_is_refused():
    doc = play(
        true=[
            {
                "kind": "compare",
                "left": {"of": "mana_value", "n": 3},
                "op": ">=",
                "right": {"of": "n", "n": 0},
            }
        ],
        false=[],
    )
    assert codes(doc) == ["forbidden-kind"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_compare_unknown_field_is_refused():
    doc = play(
        true=[
            {
                "kind": "compare",
                "left": {"of": "n", "n": 1},
                "op": ">=",
                "right": {"of": "n", "n": 0},
                "extra": True,
            }
        ],
        false=[],
    )
    assert codes(doc) == ["unknown-field"]


# == C3: all_of ===================================================================


LEAF_A = {"kind": "life", "side": "me", "op": ">=", "n": 1}
LEAF_B = {"kind": "life", "side": "opponent", "op": "<=", "n": 20}
LEAF_C = {"kind": "turn", "op": ">=", "n": 1}
LEAF_D = {"kind": "land_drop_available", "value": True}


def test_all_of_in_false_with_two_conditions_validates():
    doc = play(true=[], false=[{"kind": "all_of", "conditions": [LEAF_A, LEAF_B]}])
    assert play_errors(doc) == []


def test_all_of_in_false_with_four_conditions_validates():
    doc = play(true=[], false=[{"kind": "all_of", "conditions": [LEAF_A, LEAF_B, LEAF_C, LEAF_D]}])
    assert play_errors(doc) == []
    assert MAX_ALL_OF_CONDITIONS == 4 and MIN_ALL_OF_CONDITIONS == 2


def test_all_of_with_one_condition_is_bounds_exceeded():
    doc = play(true=[], false=[{"kind": "all_of", "conditions": [LEAF_A]}])
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_all_of_with_five_conditions_is_bounds_exceeded():
    doc = play(
        true=[],
        false=[{"kind": "all_of", "conditions": [LEAF_A, LEAF_B, LEAF_C, LEAF_D, LEAF_A]}],
    )
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_all_of_in_true_is_all_of_outside_false():
    doc = play(true=[{"kind": "all_of", "conditions": [LEAF_A, LEAF_B]}], false=[])
    assert codes(doc) == ["all-of-outside-false"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_a_nested_all_of_is_refused():
    inner = {"kind": "all_of", "conditions": [LEAF_A, LEAF_B]}
    doc = play(true=[], false=[{"kind": "all_of", "conditions": [inner, LEAF_C]}])
    assert codes(doc) == ["forbidden-kind"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_all_of_may_hold_a_pending_decision_or_compare_member():
    doc = play(
        true=[],
        false=[
            {
                "kind": "all_of",
                "conditions": [
                    {"kind": "pending_decision", "decision": "scry"},
                    {
                        "kind": "compare",
                        "left": {"of": "n", "n": 1},
                        "op": ">=",
                        "right": {"of": "n", "n": 0},
                    },
                ],
            }
        ],
    )
    assert play_errors(doc) == []


# == D2: the standalone pending-decision answer ==================================


def test_standalone_pending_answer_matching_the_condition_validates():
    doc = play(
        true=[{"kind": "pending_decision", "decision": "scry"}],
        false=[],
        moves=[answer_move({"decision": "scry", "to_bottom": {"types_any": ["Land"]}})],
    )
    assert play_errors(doc) == []


def test_standalone_pending_answer_naming_a_different_decision_is_a_mismatch():
    doc = play(
        true=[{"kind": "pending_decision", "decision": "scry"}],
        false=[],
        moves=[answer_move({"decision": "surveil", "to_graveyard": {"types_any": ["Land"]}})],
    )
    assert codes(doc) == ["answer-decision-mismatch"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_pending_decision_condition_with_a_non_standalone_play_is_not_checked():
    """The decision-match rule only fires for a play whose *only* move is
    `answer` - a play that names a `pending_decision` condition but does
    something else (a plain `cast`) is not held to it at all."""
    doc = play(
        true=[{"kind": "pending_decision", "decision": "scry"}],
        false=[],
        moves=[cast_move()],
    )
    assert play_errors(doc) == []


def test_every_answer_in_a_standalone_move_must_match():
    doc = play(
        true=[{"kind": "pending_decision", "decision": "scry"}],
        false=[],
        moves=[
            answer_move(
                {"decision": "scry", "to_bottom": {"types_any": ["Land"]}},
                {"decision": "may", "accept": True},
            )
        ],
    )
    assert codes(doc) == ["answer-decision-mismatch"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


# == combat decisions are refused with use-combat-move, not forbidden-kind ======


def test_combat_decision_kinds_constant():
    assert COMBAT_DECISION_KINDS == ("declare_attackers", "declare_blockers")


@pytest.mark.parametrize("kind", COMBAT_DECISION_KINDS)
def test_answering_a_combat_decision_names_the_move_to_use(kind):
    doc = play(moves=[answer_move({"decision": kind, "accept": True})])
    assert codes(doc) == ["use-combat-move"]
    (error,) = play_errors(doc)
    assert kind in error.message


# == codes ==========================================================================


def test_the_new_fb2_codes_are_in_the_shared_contract():
    for code in (
        "pass-not-last",
        "combat-move-not-first",
        "activate-ability-ref",
        "all-of-outside-false",
        "use-combat-move",
        "answer-decision-mismatch",
    ):
        assert code in CODES


# == the mtg-decks corpus still validates unchanged =================================

MTG_DECKS_DECKS = Path("/Users/szymonri/Documents/mtg-decks/decks")


def test_every_mtg_decks_play_still_validates():
    if not MTG_DECKS_DECKS.exists():
        pytest.skip("mtg-decks checkout not present at /Users/szymonri/Documents/mtg-decks")
    paths = sorted(
        path for path in MTG_DECKS_DECKS.glob("*/plays/*.json") if path.name != "inputs.json"
    )
    assert paths, "expected at least one play under mtg-decks/decks/*/plays/*.json"
    failures = []
    for path in paths:
        doc = json.loads(path.read_text())
        errors = play_errors(doc)
        if errors:
            failures.append((str(path), [error.to_dict() for error in errors]))
    assert failures == []
