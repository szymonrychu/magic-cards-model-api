"""FD9's schema half: a bounded `repeat` move (`RepeatMove`) and the
`mana_pool` condition/operand it needs to express the owner's own Devoted
Druid + Vizier of Remedies mana-loop example (finding seq 191).

This is the vocabulary FD3 part 7's runtime executes, so every shape, bound
and code here is exactly what
`docs/superpowers/specs/2026-09-13-play-script-format-design.md` says
(`tests/test_playscript_format_doc.py` pins the doc side)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from magic_cards_model_api.playscripts.schema import (
    MAX_CONDITIONS,
    MAX_REPEAT_BODY,
    MAX_REPEAT_ITERATIONS,
    Play,
    play_errors,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "playscripts"

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


def activate_move(**over: Any) -> dict[str, Any]:
    move: dict[str, Any] = {
        "kind": "activate",
        "source": {"zone": "battlefield", "select": {}},
        "ability_index": 0,
    }
    move.update(over)
    return move


def mana_pool_until(**over: Any) -> dict[str, Any]:
    condition: dict[str, Any] = {"kind": "mana_pool", "op": ">=", "n": 8}
    condition.update(over)
    return condition


def repeat_move(**over: Any) -> dict[str, Any]:
    move: dict[str, Any] = {
        "kind": "repeat",
        "moves": [activate_move()],
        "until": [mana_pool_until()],
        "max_iterations": 10,
    }
    move.update(over)
    return move


# -- the basic shape validates ------------------------------------------------


def test_repeat_move_validates():
    doc = play(moves=[repeat_move()])
    assert play_errors(doc) == []
    Play.model_validate(doc)


def test_repeat_counts_as_one_move_toward_max_moves():
    doc = play(moves=[repeat_move()])
    assert len(doc["moves"]) == 1


def test_stop_when_refused_defaults_false_and_can_be_set():
    doc = play(moves=[repeat_move()])
    parsed = Play.model_validate(doc)
    assert parsed.moves[0].stop_when_refused is False
    doc2 = play(moves=[repeat_move(stop_when_refused=True)])
    parsed2 = Play.model_validate(doc2)
    assert parsed2.moves[0].stop_when_refused is True


# -- moves: 1-MAX_REPEAT_BODY, none of them repeat or pass --------------------


def test_repeat_body_bound_one_to_max_repeat_body():
    doc = play(moves=[repeat_move(moves=[activate_move()])])
    assert play_errors(doc) == []
    doc = play(moves=[repeat_move(moves=[activate_move()] * MAX_REPEAT_BODY)])
    assert play_errors(doc) == []
    doc = play(moves=[repeat_move(moves=[])])
    assert codes(doc) == ["bounds-exceeded"]
    doc = play(moves=[repeat_move(moves=[activate_move()] * (MAX_REPEAT_BODY + 1))])
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_repeat_pass_in_body_is_refused():
    doc = play(moves=[repeat_move(moves=[{"kind": "pass"}])])
    assert codes(doc) == ["repeat-pass-in-body"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_repeat_pass_in_body_among_other_moves_is_still_refused():
    doc = play(moves=[repeat_move(moves=[activate_move(), {"kind": "pass"}])])
    assert codes(doc) == ["repeat-pass-in-body"]


def test_nested_repeat_is_refused():
    inner = repeat_move()
    doc = play(moves=[repeat_move(moves=[inner])])
    assert codes(doc) == ["repeat-nested"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_repeat_body_may_hold_every_other_move_kind():
    """Only `repeat` and `pass` are excluded from the body - every other move
    kind (`cast`, `play_land`, `answer`, `declare_attackers`,
    `declare_blockers`, `keyword_action`, `mulligan`) is a legal body move."""
    body_moves = [
        {"kind": "cast", "card": {"zone": "hand", "select": {"types_any": ["Instant"]}}},
        {"kind": "play_land", "card": {"select": {}}},
    ]
    doc = play(moves=[repeat_move(moves=body_moves)])
    assert play_errors(doc) == []


# -- FD9 review: per-move play-level rules apply inside a repeat's body ------


def test_repeat_body_mulligan_needs_the_mulligan_step():
    """A `mulligan` move nested in a repeat's body is still a `mulligan`
    move: `when.steps` must still be exactly `['mulligan']`, the same rule
    `_mulligan_move_needs_the_mulligan_step` applies at the top level."""
    doc = play(
        moves=[repeat_move(moves=[{"kind": "mulligan", "keep": True}])],
    )
    assert codes(doc) == ["mulligan-wrong-step"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_repeat_body_mulligan_with_the_mulligan_step_validates():
    doc = play(
        when={"whose_turn": "mine", "steps": ["mulligan"], "stack": "empty"},
        moves=[repeat_move(moves=[{"kind": "mulligan", "keep": True}])],
    )
    assert play_errors(doc) == []


def test_repeat_body_combat_move_must_be_first_within_the_body():
    doc = play(
        moves=[
            repeat_move(
                moves=[
                    activate_move(),
                    {"kind": "declare_attackers", "attackers": []},
                ]
            )
        ],
    )
    assert codes(doc) == ["combat-move-not-first"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_repeat_body_combat_move_first_in_the_body_validates():
    doc = play(
        moves=[
            repeat_move(
                moves=[{"kind": "declare_blockers", "blocks": []}],
                until=[mana_pool_until()],
            )
        ],
    )
    assert play_errors(doc) == []


def test_repeat_combat_move_must_be_the_plays_first_move_not_just_the_bodys():
    """A repeat whose body opens with a combat move still has to be the
    *play's* first move, not merely body_index 0 within itself - a combat
    move sitting behind an earlier top-level move is exactly as illegal as
    one sitting behind an earlier body move."""
    doc = play(
        moves=[
            activate_move(),
            repeat_move(moves=[{"kind": "declare_attackers", "attackers": []}]),
        ],
    )
    assert codes(doc) == ["combat-move-not-first"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_at_most_one_combat_move_per_play_across_top_level_and_repeat_body():
    """A play may carry at most one combat move total (per the spec's own
    'at most one per play'): a top-level `declare_attackers` plus a second
    combat move nested in a later repeat's body is two, not one, even
    though each individually sits at its own container's index 0."""
    doc = play(
        moves=[
            {"kind": "declare_attackers", "attackers": []},
            repeat_move(moves=[{"kind": "declare_blockers", "blocks": []}]),
        ],
    )
    assert codes(doc) == ["combat-move-not-first"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


# -- until: 1-MAX_CONDITIONS conditions of the existing kinds -----------------


def test_repeat_until_bound_one_to_max_conditions():
    doc = play(moves=[repeat_move(until=[mana_pool_until()])])
    assert play_errors(doc) == []
    doc = play(moves=[repeat_move(until=[mana_pool_until()] * MAX_CONDITIONS)])
    assert play_errors(doc) == []
    doc = play(moves=[repeat_move(until=[mana_pool_until()] * (MAX_CONDITIONS + 1))])
    assert codes(doc) == ["bounds-exceeded"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_repeat_empty_until_is_refused():
    doc = play(moves=[repeat_move(until=[])])
    assert codes(doc) == ["repeat-empty-until"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


# -- max_iterations: required, 1-MAX_REPEAT_ITERATIONS ------------------------


def test_max_iterations_bound_one_to_max_repeat_iterations():
    doc = play(moves=[repeat_move(max_iterations=1)])
    assert play_errors(doc) == []
    doc = play(moves=[repeat_move(max_iterations=MAX_REPEAT_ITERATIONS)])
    assert play_errors(doc) == []


def test_max_iterations_zero_is_repeat_bound():
    doc = play(moves=[repeat_move(max_iterations=0)])
    assert codes(doc) == ["repeat-bound"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_max_iterations_one_past_the_bound_is_repeat_bound():
    doc = play(moves=[repeat_move(max_iterations=MAX_REPEAT_ITERATIONS + 1)])
    assert codes(doc) == ["repeat-bound"]


def test_max_iterations_missing_is_repeat_bound():
    move = repeat_move()
    del move["max_iterations"]
    doc = play(moves=[move])
    assert codes(doc) == ["repeat-bound"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


def test_max_iterations_negative_is_repeat_bound():
    doc = play(moves=[repeat_move(max_iterations=-1)])
    assert codes(doc) == ["repeat-bound"]


def test_max_iterations_bool_is_refused():
    """`True`/`False` are ints in Python, but strict mode's own int/bool
    separation already refuses one as a plain type mismatch before this
    move's own `repeat-bound` check ever runs - `max_iterations` never
    silently accepts a bool."""
    doc = play(moves=[repeat_move(max_iterations=True)])
    assert codes(doc) == ["malformed-play"]
    with pytest.raises(ValidationError):
        Play.model_validate(doc)


# -- schema_version stays 1 (additive) ----------------------------------------


def test_schema_version_is_unaffected_by_repeat():
    doc = play(moves=[repeat_move()])
    assert doc["schema_version"] == 1
    Play.model_validate(doc)


# -- the owner's three example conditions validate ----------------------------


def test_owner_example_activating_until_a_mana_target_is_reached():
    """Owner: "DSL is meant to fix missing repeat-until loops." - the
    Devoted Druid + Vizier of Remedies loop's own stop condition, `n`
    matching finding seq 191's own X of 8."""
    doc = play(
        moves=[
            repeat_move(
                moves=[
                    activate_move(
                        source={
                            "zone": "battlefield",
                            "select": {"types_all": ["Creature"], "subtypes_any": ["Elf"]},
                        },
                        ability_index=0,
                    ),
                    activate_move(
                        source={
                            "zone": "battlefield",
                            "select": {"types_all": ["Creature"], "subtypes_any": ["Elf"]},
                        },
                        ability_index=1,
                    ),
                ],
                until=[{"kind": "mana_pool", "color": "G", "op": ">=", "n": 8}],
            )
        ]
    )
    assert play_errors(doc) == []


def test_owner_example_repeat_untap_loop_over_counters_or_tapped_state():
    """A count/compare over the loop permanent's own counters or tapped
    state - the "repeat-untap loops" example."""
    doc_counters = play(
        moves=[
            repeat_move(
                until=[
                    {
                        "kind": "count",
                        "zone": "battlefield",
                        "side": "me",
                        "op": ">=",
                        "n": 1,
                        "select": {
                            "types_all": ["Creature"],
                            "counters": {"name": "-1/-1", "op": ">=", "n": 3},
                        },
                    }
                ]
            )
        ]
    )
    assert play_errors(doc_counters) == []

    doc_tapped = play(
        moves=[
            repeat_move(
                until=[
                    {
                        "kind": "count",
                        "zone": "battlefield",
                        "side": "me",
                        "op": ">=",
                        "n": 1,
                        "select": {"types_all": ["Creature"], "tapped": True},
                    }
                ]
            )
        ]
    )
    assert play_errors(doc_tapped) == []


def test_owner_example_while_an_opponent_has_untapped_blockers():
    """`until: count battlefield side opponent types_all [Creature] tapped
    false == 0` - the owner's own worked example, verbatim."""
    doc = play(
        moves=[
            repeat_move(
                until=[
                    {
                        "kind": "count",
                        "zone": "battlefield",
                        "side": "opponent",
                        "op": "==",
                        "n": 0,
                        "select": {"types_all": ["Creature"], "tapped": False},
                    }
                ]
            )
        ]
    )
    assert play_errors(doc) == []


# -- mana_pool condition and compare operand ----------------------------------


def test_mana_pool_condition_color_is_optional():
    doc = play(moves=[repeat_move(until=[{"kind": "mana_pool", "op": ">=", "n": 3}])])
    assert play_errors(doc) == []
    doc = play(moves=[repeat_move(until=[{"kind": "mana_pool", "color": "U", "op": ">=", "n": 3}])])
    assert play_errors(doc) == []


def test_mana_pool_condition_rejects_unknown_color():
    doc = play(moves=[repeat_move(until=[{"kind": "mana_pool", "color": "Z", "op": ">=", "n": 3}])])
    assert codes(doc) != []


def test_mana_pool_compare_operand_validates():
    doc = play(
        true=[
            {
                "kind": "compare",
                "left": {"of": "mana_pool", "side": "me"},
                "op": ">=",
                "right": {"of": "n", "n": 4},
            }
        ]
    )
    assert play_errors(doc) == []


# -- FD9's two proof fixtures: schema validity -------------------------------


@pytest.mark.parametrize("name", ["repeat_devoted_druid.json", "repeat_walking_ballista.json"])
def test_proof_fixture_validates(name: str):
    doc = json.loads((FIXTURES / name).read_text())
    assert play_errors(doc) == []
    parsed = Play.model_validate(doc)
    assert any(move.kind == "repeat" for move in parsed.moves)


def test_mana_pool_compare_operand_takes_an_optional_color():
    doc = play(
        true=[
            {
                "kind": "compare",
                "left": {"of": "mana_pool", "side": "opponent", "color": "R"},
                "op": "<",
                "right": {"of": "n", "n": 2},
            }
        ]
    )
    assert play_errors(doc) == []
