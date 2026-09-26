"""`plays check`: the corpus rules over a guides root, one tmp negative per code.

The codes, the input vocabulary and the conflict warnings are a contract
shared verbatim with mtg-decks' `scripts/check_plays.py` (M1b). Every case
here builds a tmp root that differs from a clean one by exactly the shape
under test, so the owner's live checkout is never read.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest
from playscript_testkit import (
    CARDS,
    FETCH_SHOCK,
    FETCH_SURVEIL,
    SLUG,
    make_root,
    play,
    put,
    put_inputs,
)

from magic_cards_model_api import seat_cli
from magic_cards_model_api.playscripts.lint import (
    CODES,
    EXCLUSIVITY_DIMENSIONS,
    WARNING_CODES,
    check_report,
    conflict_dimensions,
    exclusivity,
    run_check,
)


def advice(id: str, **over: Any) -> dict[str, Any]:
    """An advice-only play: `moves` empty, so it commits nothing."""
    over.setdefault("priority", 0)
    return play(id=id, moves=[], **over)


def move_bearing(id: str, **over: Any) -> dict[str, Any]:
    """A play with a move, priority disabled so it never enters an active count."""
    over.setdefault("priority", 0)
    return play(id=id, **over)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return make_root(tmp_path)


def error_codes(root: Path, decks: list[str] | None = None) -> list[str]:
    return sorted(e.code for e in check_report(root, decks)[0])


def warnings_of(root: Path) -> list[dict[str, Any]]:
    return [w.to_dict() for w in check_report(root)[1]]


def cli(*argv: str) -> int:
    return seat_cli.run(["plays", "check", *argv])


def test_codes_and_warning_codes_are_the_shared_contract():
    assert CODES[12:15] == (
        "too-many-active-for-archetype",
        "too-many-advice",
        "advice-only-chained",
    )
    assert CODES[16:19] == ("unknown-input", "invalid-input-value", "malformed-inputs")
    assert CODES[19:24] == (
        "unknown-decision-kind",
        "unknown-cast-cost",
        "duplicate-step",
        "wildcard-with-keys",
        "answer-shape-mismatch",
    )
    assert CODES[24:30] == (
        "pass-not-last",
        "combat-move-not-first",
        "activate-ability-ref",
        "all-of-outside-false",
        "use-combat-move",
        "answer-decision-mismatch",
    )
    assert CODES[30:32] == ("mulligan-wrong-step", "never-applicable")
    assert len(CODES) == 32
    assert WARNING_CODES == ("play-conflict", "next-conflict")
    assert EXCLUSIVITY_DIMENSIONS == (
        "whose_turn",
        "steps",
        "opposing_archetypes",
        "stack",
        "pending_decision",
        "input",
        "true_false",
        "numeric_range",
        "pass_fallback",
        "move_selector",
    )


def test_the_worked_examples_clear_the_gate(root):
    put(root, FETCH_SHOCK)
    put(root, FETCH_SURVEIL)
    assert check_report(root) == ([], [])


def test_a_deck_without_plays_is_clean(tmp_path):
    root = make_root(tmp_path)
    (root / "decks" / SLUG / "plays").rmdir()
    assert check_report(root) == ([], [])


# -- one negative per error code ---------------------------------------------


def test_malformed_play_for_unreadable_json_and_for_a_bad_shape(root):
    put(root, "{not json", stem="broken")
    put(root, play(id="nameless", name=""))
    assert error_codes(root) == ["malformed-play", "malformed-play"]


def test_unknown_field(root):
    put(root, play(repeat=3))
    assert error_codes(root) == ["unknown-field"]


def test_id_mismatch(root):
    put(root, FETCH_SHOCK, stem="another-stem")
    (error,) = check_report(root)[0]
    assert error.code == "id-mismatch"
    assert error.path == f"decks/{SLUG}/plays/another-stem.json"


def test_bounds_exceeded(root):
    put(root, play(priority=101))
    assert error_codes(root) == ["bounds-exceeded"]


def test_forbidden_kind(root):
    put(root, play(moves=[{"kind": "attack"}]))
    assert error_codes(root) == ["forbidden-kind"]


def test_hidden_zone_side_any(root):
    condition = {
        "kind": "count",
        "zone": "library",
        "side": "any",
        "op": ">=",
        "n": 1,
        "select": {},
    }
    put(root, play(true=[condition]))
    assert error_codes(root) == ["hidden-zone-side-any"]


def test_card_name_in_any_machine_read_field(root):
    doc = play()
    doc["false"][1]["select"] = {"keywords_any": ["Lightning Bolt"]}
    doc["moves"][0]["answers"][1]["decision"] = "arid mesa"
    put(root, doc)
    assert error_codes(root) == [
        "card-name-in-field",
        "card-name-in-field",
        "unknown-decision-kind",
    ]


def test_card_names_are_allowed_in_prose(root):
    put(root, play(name="Crack Arid Mesa for Sacred Foundry", intent="Lightning Bolt later."))
    assert check_report(root) == ([], [])


def test_note_may_name_a_card_at_the_top_level(root):
    # PROSE_KEYS: `note` is exempt exactly like `name` and `intent`.
    put(root, play(note="Lightning Bolt"))
    assert check_report(root) == ([], [])


def test_a_nested_field_named_note_naming_a_card_is_still_card_name_in_field(root):
    # F10: the prose exemption is top-level only. A machine-read field that
    # happens to be named "note" anywhere below the top level gets no
    # exemption at all - it is judged the same as any other machine field.
    doc = play()
    doc["false"][1]["select"] = {"note": "Lightning Bolt"}
    put(root, doc)
    assert "card-name-in-field" in error_codes(root)


def test_a_type_line_word_on_three_cards_is_not_a_card_name_in_a_type_field(root):
    doc = play()
    doc["false"][1]["select"] = {"subtypes_any": ["Mountain"], "types_any": ["Land"]}
    put(root, doc)
    assert check_report(root) == ([], [])
    doc["false"][1]["select"] = {"keywords_any": ["Mountain"]}
    put(root, doc)
    assert error_codes(root) == ["card-name-in-field"]


def test_a_card_name_in_a_type_field_is_refused_when_it_is_not_a_common_type_word(tmp_path):
    cards = {k: v for k, v in CARDS.items() if k not in ("Blood Crypt", "Stomping Ground")}
    root = make_root(tmp_path, cards=cards)
    doc = play()
    doc["false"][1]["select"] = {"subtypes_any": ["Mountain"]}
    put(root, doc)
    assert "card-name-in-field" in error_codes(root)


def test_a_basic_land_type_word_on_three_cards_is_not_a_card_name_in_searches_for_any(root):
    # "Mountain" is a type-line word on 4 cached cards (Mountain, Sacred
    # Foundry, Blood Crypt, Stomping Ground), the same exemption O3's
    # searches_for_any gets that types_any/subtypes_any already had.
    doc = play()
    doc["false"][1]["select"] = {"searches_for_any": ["Mountain"]}
    put(root, doc)
    assert check_report(root) == ([], [])


def test_a_basic_land_type_word_in_searches_for_any_is_refused_when_not_a_common_type_word(
    tmp_path,
):
    cards = {k: v for k, v in CARDS.items() if k not in ("Blood Crypt", "Stomping Ground")}
    root = make_root(tmp_path, cards=cards)
    doc = play()
    doc["false"][1]["select"] = {"searches_for_any": ["Mountain"]}
    put(root, doc)
    assert "card-name-in-field" in error_codes(root)


# -- O6: Answer.choice --------------------------------------------------------


def test_a_type_line_word_in_choice_is_not_a_card_name(tmp_path):
    # A creature type such as Eldrazi (a type-line word on >= 3 cached cards)
    # is allowed in an answer's choice, the same type-line-word exemption
    # types_any/subtypes_any/searches_for_any already get.
    cards = dict(CARDS)
    cards["Thought-Knot Seer"] = {
        "type_line": "Creature — Eldrazi",
        "oracle_text": "When Thought-Knot Seer enters, target opponent reveals their hand.",
    }
    cards["Reality Smasher"] = {
        "type_line": "Creature — Eldrazi",
        "oracle_text": "Reality Smasher can't be blocked by more than one creature.",
    }
    cards["Endless One"] = {
        "type_line": "Creature — Eldrazi",
        "oracle_text": "Endless One enters with X +1/+1 counters on it.",
    }
    root = make_root(tmp_path, cards=cards)
    doc = play(
        moves=[
            {"kind": "answer", "answers": [{"decision": "choose_a_quality", "choice": "Eldrazi"}]}
        ]
    )
    put(root, doc)
    assert check_report(root) == ([], [])


def test_a_choice_that_is_a_card_name_and_not_a_common_type_word_is_card_name_in_field(root):
    doc = play(
        moves=[
            {"kind": "answer", "answers": [{"decision": "choose_a_quality", "choice": "Arid Mesa"}]}
        ]
    )
    put(root, doc)
    assert "card-name-in-field" in error_codes(root)


def test_phrase_too_narrow(root):
    doc = play()
    doc["false"][1]["select"] = {"oracle_has": "deals 3 damage to any target"}
    put(root, doc)
    (error,) = check_report(root)[0]
    assert error.code == "phrase-too-narrow"
    assert "1 cached card" in error.message


def test_a_phrase_matches_case_insensitively(root):
    doc = play()
    doc["false"][1]["select"] = {"oracle_has": "SEARCH YOUR LIBRARY FOR"}
    put(root, doc)
    assert check_report(root)[0] == []


def test_unknown_next(root):
    put(root, play(next=["nope"]))
    assert error_codes(root) == ["unknown-next"]


def test_next_cycle_including_a_self_edge(root, tmp_path):
    put(root, play(id="a", next=["b"], priority=0))
    put(root, play(id="b", next=["a"], priority=0))
    assert error_codes(root) == ["next-cycle", "next-cycle"]
    other = make_root(tmp_path / "self")
    put(other, play(id="a", next=["a"]))
    assert error_codes(other) == ["next-cycle"]


def test_chain_too_deep(root):
    put(root, play(id="a", next=["b"], priority=0))
    put(root, play(id="b", next=["c"], priority=0))
    put(root, play(id="c", next=["d"], priority=0))
    put(root, play(id="d", priority=0))
    assert error_codes(root) == ["chain-too-deep"]


def test_a_chain_of_three_is_fine(root):
    put(root, play(id="a", next=["b"], priority=0))
    put(root, play(id="b", next=["c"], priority=0))
    put(root, play(id="c", priority=0))
    assert check_report(root)[0] == []


# -- play-count caps removed (owner, 2026-09-24): a deck may carry any
# -- number of move-bearing, advice-only or archetype-active plays --------


def test_seventeen_move_bearing_plays_raise_no_count_error(root):
    for i in range(17):
        put(root, move_bearing(f"p{i}"))
    assert error_codes(root) == []


def test_sixteen_move_bearing_and_six_advice_only_plays_are_clean(root):
    for i in range(16):
        put(root, move_bearing(f"m{i}"))
    for i in range(6):
        put(root, advice(f"a{i}"))
    assert check_report(root) == ([], [])


def test_seven_advice_only_plays_raise_no_count_error(root):
    for i in range(7):
        put(root, advice(f"a{i}"))
    assert error_codes(root) == []


def test_inputs_json_is_not_a_play(root):
    for i in range(12):
        put(root, play(id=f"p{i}", priority=0))
    put_inputs(root, {"schema_version": 1, "inputs": []})
    assert check_report(root)[0] == []


def test_seven_plays_live_together_at_one_archetype_raise_no_count_error(root):
    for i in range(7):
        put(
            root,
            play(
                id=f"p{i}",
                priority=10,
                when={"whose_turn": "mine", "steps": ["upkeep"], "stack": "empty"},
            ),
        )
    assert error_codes(root) == []


def test_a_wildcard_play_beside_a_named_key_raises_no_count_error(root):
    for i in range(4):
        put(root, play(id=f"star{i}", priority=10))
    for i in range(3):
        put(root, play(id=f"frog{i}", priority=10, opposing_archetypes=["dimir-frog"]))
    assert error_codes(root) == []


def test_a_large_deck_all_pairwise_non_conflicting_clears_check_deck_under_3s(root):
    """The play-count caps (`too-many-plays`, `too-many-advice` and
    `too-many-active-for-archetype`/O8) are removed (owner, 2026-09-24): a
    deck may carry any number of plays of each kind. What `check_deck` still
    proves in bounded time is pairwise non-overlap: `exclusivity` and
    `_conflict_warnings` are an O(n^2) pass over the deck's enabled,
    move-bearing plays, never the exponential live-together search the old
    per-archetype cap used to gate against (`_max_live_together`,
    Bron-Kerbosch without a pivot, removed along with the cap it bounded).
    60 move-bearing plays (30 of them sharing one archetype key), each on
    its own turn so every pair is provably exclusive by `numeric_range`,
    plus 20 advice-only plays, clear `check_deck` well under 3s and raise no
    cap error at all."""
    for i in range(60):
        put(
            root,
            play(
                id=f"m{i}",
                priority=10,
                opposing_archetypes=["dimir-frog"] if i < 30 else ["*"],
                true=[{"kind": "turn", "op": "==", "n": i + 1}],
                false=[],
            ),
        )
    for i in range(20):
        put(root, advice(f"a{i}", priority=10))
    start = time.monotonic()
    errors, warnings = check_report(root)
    elapsed = time.monotonic() - start
    assert errors == []
    assert warnings == []
    assert elapsed < 3.0, f"took {elapsed}s"


def test_unknown_archetype_key_reads_the_newest_analysis_run(tmp_path):
    root = make_root(tmp_path)
    older = (
        root
        / "decks"
        / SLUG
        / "analysis"
        / f"{SLUG}-modern-2026-08-01"
        / "domain3-matchup-threat"
        / "matchups"
    )
    older.mkdir(parents=True)
    (older / "affinity.json").write_text("{}", encoding="utf-8")
    put(root, play(opposing_archetypes=["affinity"]))
    assert error_codes(root) == ["unknown-archetype-key"]
    put(root, play(opposing_archetypes=["living-end"]))
    assert check_report(root)[0] == []
    put(root, play(opposing_archetypes=["*", "living-end"]))
    assert error_codes(root) == ["wildcard-with-keys"]


def test_a_cycle_does_not_mask_chain_depth_elsewhere(root):
    # battle 1 D09: deck bt-graph, the cycle g-a/g-b beside the chain g-d..g-g
    put(root, play(id="g-a", next=["g-b"], priority=0))
    put(root, play(id="g-b", next=["g-a"], priority=0))
    put(root, play(id="g-d", next=["g-e"], priority=0))
    put(root, play(id="g-e", next=["g-f"], priority=0))
    put(root, play(id="g-f", next=["g-g"], priority=0))
    put(root, play(id="g-g", priority=0))
    found = sorted((e.code, e.path.rsplit("/", 1)[-1]) for e in check_report(root)[0])
    assert found == [
        ("chain-too-deep", "g-d.json"),
        ("next-cycle", "g-a.json"),
        ("next-cycle", "g-b.json"),
    ]


def test_chain_depth_leaves_the_cyclic_plays_out(root):
    put(root, play(id="a", next=["b"], priority=0))
    put(root, play(id="b", next=["a"], priority=0))
    put(root, play(id="x", next=["y"], priority=0))
    put(root, play(id="y", next=["z", "a"], priority=0))
    put(root, play(id="z", priority=0))
    assert error_codes(root) == ["next-cycle", "next-cycle"]


def test_a_pattern_invalid_archetype_key_is_only_unknown_archetype_key(root):
    # battle 1 D06: bt-struct/s12-archetype-underscore, the stdlib gate's code
    put(root, play(opposing_archetypes=["esper_blink"]))
    (error,) = check_report(root)[0]
    assert error.code == "unknown-archetype-key"
    assert "'esper_blink'" in error.message


def test_a_blank_archetype_key_is_malformed_and_unknown(root):
    put(root, play(opposing_archetypes=["  "]))
    assert error_codes(root) == ["malformed-play", "unknown-archetype-key"]


# -- inputs.json and input conditions (M1b's three codes) ----------------------

INPUTS = {
    "schema_version": 1,
    "inputs": [
        {
            "name": "plan",
            "type": "enum",
            "values": ["aggro", "control"],
            "description": "The line.",
            "required": False,
        },
        {"name": "hold_up", "type": "bool", "description": "Keep mana open.", "required": False},
        {
            "name": "clock",
            "type": "int",
            "min": 0,
            "max": 10,
            "description": "Turns to die.",
            "required": True,
        },
    ],
}


def with_input(name: str, op: str, value: Any, *, where: str = "true") -> dict[str, Any]:
    doc = play()
    doc[where] = [*doc[where], {"kind": "input", "name": name, "op": op, "value": value}]
    return doc


def test_unknown_input(root):
    put(root, with_input("plan", "eq", "aggro"))
    (error,) = check_report(root)[0]
    assert error.code == "unknown-input" and "'plan'" in error.message


def test_the_built_in_opposing_archetype_needs_no_declaration(root):
    put(root, with_input("opposing_archetype", "in", ["*", "dimir-frog"]))
    assert check_report(root)[0] == []


def test_declared_inputs_are_known(root):
    put_inputs(root, INPUTS)
    put(root, with_input("plan", "eq", "aggro", where="false"))
    assert check_report(root)[0] == []


@pytest.mark.parametrize(
    ("name", "op", "value"),
    [
        ("opposing_archetype", "eq", "affinity"),
        ("opposing_archetype", "<", "dimir-frog"),
        ("plan", "eq", "midrange"),
        ("plan", "in", ["aggro", "tempo"]),
        ("plan", "in", []),
        ("plan", "eq", ["aggro"]),
        ("plan", ">=", "aggro"),
        ("hold_up", "eq", "yes"),
        ("hold_up", "<=", True),
        ("clock", "eq", 11),
        ("clock", ">", -1),
        ("clock", "in", [1, 12]),
        ("clock", "not_in", "3"),
        ("clock", "eq", True),
    ],
)
def test_invalid_input_value(root, name, op, value):
    put_inputs(root, INPUTS)
    put(root, with_input(name, op, value))
    assert error_codes(root) == ["invalid-input-value"]


@pytest.mark.parametrize("op", ["eq", "ne", "in", "not_in", "<", "<=", ">", ">="])
def test_an_int_input_takes_all_eight_ops(root, op):
    put_inputs(root, INPUTS)
    value = [0, 10] if op in ("in", "not_in") else 5
    put(root, with_input("clock", op, value))
    assert check_report(root)[0] == []


@pytest.mark.parametrize("op", ["eq", "ne", "in", "not_in"])
def test_enum_and_bool_inputs_take_the_four_equality_ops(root, op):
    put_inputs(root, INPUTS)
    put(root, with_input("plan", op, ["aggro"] if op in ("in", "not_in") else "aggro"))
    put(
        root,
        play(
            id="other",
            priority=0,
            true=[
                {
                    "kind": "input",
                    "name": "hold_up",
                    "op": op,
                    "value": [True] if op in ("in", "not_in") else False,
                }
            ],
        ),
    )
    assert check_report(root)[0] == []


def _inputs_with(**entry: Any) -> dict[str, Any]:
    base = {"name": "plan", "type": "enum", "values": ["a"], "description": "d", "required": False}
    base.update(entry)
    return {"schema_version": 1, "inputs": [{k: v for k, v in base.items() if v is not None}]}


MALFORMED_INPUTS = [
    ("not-json", "{nope"),
    ("not-an-object", json.dumps([1])),
    ("schema-version-2", {"schema_version": 2, "inputs": []}),
    ("extra-top-level-key", {"schema_version": 1, "inputs": [], "defaults": {}}),
    ("inputs-not-a-list", {"schema_version": 1, "inputs": {}}),
    ("entry-not-an-object", {"schema_version": 1, "inputs": ["plan"]}),
    ("redeclares-the-built-in", _inputs_with(name="opposing_archetype")),
    ("duplicate-name", {"schema_version": 1, "inputs": _inputs_with()["inputs"] * 2}),
    ("blank-name", _inputs_with(name=" ")),
    ("unknown-type", _inputs_with(type="float")),
    ("enum-without-values", _inputs_with(values=None)),
    ("enum-empty-values", _inputs_with(values=[])),
    ("enum-repeated-values", _inputs_with(values=["a", "a"])),
    ("enum-blank-value", _inputs_with(values=["a", ""])),
    ("values-on-a-bool", _inputs_with(type="bool", values=["a"])),
    ("min-on-an-enum", _inputs_with(min=0)),
    ("int-min-above-max", _inputs_with(type="int", values=None, min=5, max=1)),
    ("int-bound-not-an-integer", _inputs_with(type="int", values=None, max=2.5)),
    ("blank-description", _inputs_with(description="")),
    ("required-not-a-bool", _inputs_with(required="yes")),
    ("unknown-entry-key", _inputs_with(default="a")),
]


@pytest.mark.parametrize(
    "doc", [d for _, d in MALFORMED_INPUTS], ids=[n for n, _ in MALFORMED_INPUTS]
)
def test_malformed_inputs(root, doc):
    put_inputs(root, doc)
    put(root, FETCH_SHOCK)
    codes = set(error_codes(root))
    assert codes == {"malformed-inputs"}


def test_malformed_inputs_suppresses_unknown_input(root):
    put_inputs(root, "{nope")
    put(root, with_input("plan", "eq", "aggro"))
    assert error_codes(root) == ["malformed-inputs"]


def test_the_built_in_still_resolves_beside_a_malformed_inputs_json(root):
    put_inputs(root, "{nope")
    put(root, with_input("opposing_archetype", "eq", "affinity"))
    assert error_codes(root) == ["invalid-input-value", "malformed-inputs"]


def test_malformed_inputs_is_reported_for_a_deck_with_no_plays(root):
    put_inputs(root, _inputs_with(name="opposing_archetype"))
    assert error_codes(root) == ["malformed-inputs"]


# -- conflict warnings (never an error, never the exit code) -------------------


def when(
    whose_turn: str = "mine", steps: tuple[str, ...] = ("precombat_main",), stack: str = "empty"
) -> dict[str, Any]:
    return {"whose_turn": whose_turn, "steps": list(steps), "stack": stack}


def test_identical_plays_conflict_and_the_warning_names_both(root):
    put(root, play(id="a"))
    put(root, play(id="b"))
    errors, warnings = check_report(root)
    assert errors == []
    (warning,) = [w.to_dict() for w in warnings]
    assert sorted(warning) == ["code", "dimensions", "message", "path", "plays"]
    assert warning["code"] == "play-conflict"
    assert warning["plays"] == ["a", "b"]
    assert warning["path"] == f"decks/{SLUG}/plays/a.json"
    assert warning["dimensions"] == [
        "whose_turn",
        "steps",
        "opposing_archetypes",
        "stack",
        "pending_decision",
        "true_false",
        "numeric_range",
    ]


def test_a_disabled_play_is_never_compared(root):
    put(root, play(id="a"))
    put(root, play(id="b", priority=0))
    assert check_report(root) == ([], [])


def test_two_advice_only_plays_that_would_conflict_on_every_dimension_raise_no_warning(root):
    # Requirement 4: an advice-only play is never one half of a play-conflict
    # - it commits nothing, so there is no decision point for it to conflict
    # at, priority > 0 or not.
    put(root, advice("a", priority=60))
    put(root, advice("b", priority=60))
    assert check_report(root) == ([], [])


EXCLUSIVE_PAIRS = [
    ("whose_turn", {"when": when("mine")}, {"when": when("theirs")}),
    ("steps", {"when": when(steps=("upkeep",))}, {"when": when(steps=("end_step",))}),
    (
        "opposing_archetypes",
        {"opposing_archetypes": ["dimir-frog"]},
        {"opposing_archetypes": ["living-end"]},
    ),
    (
        "stack",
        {"when": when(stack="empty"), "true": [], "false": []},
        {
            "when": when(stack="any"),
            "true": [{"kind": "stack_top", "controller": "opponent", "select": {}}],
            "false": [],
        },
    ),
    (
        "pending_decision",
        {"true": [{"kind": "pending_decision", "decision": "scry"}], "false": []},
        {"true": [{"kind": "pending_decision", "decision": "surveil"}], "false": []},
    ),
    (
        "input",
        {"true": [{"kind": "input", "name": "plan", "op": "eq", "value": "aggro"}], "false": []},
        {"true": [{"kind": "input", "name": "plan", "op": "eq", "value": "control"}], "false": []},
    ),
    (
        "true_false",
        {"true": [{"kind": "land_drop_available", "value": True}], "false": []},
        {"true": [], "false": [{"kind": "land_drop_available", "value": True}]},
    ),
    (
        "numeric_range",
        {"true": [{"kind": "life", "side": "me", "op": ">=", "n": 8}], "false": []},
        {"true": [{"kind": "life", "side": "me", "op": "<=", "n": 5}], "false": []},
    ),
    (
        "pass_fallback",
        {"moves": [{"kind": "pass"}]},
        {
            "moves": [
                {"kind": "cast", "card": {"zone": "hand", "select": {"types_any": ["Instant"]}}}
            ]
        },
    ),
    (
        "move_selector",
        {
            "moves": [
                {"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Creature"]}}}
            ]
        },
        {
            "moves": [
                {
                    "kind": "cast",
                    "card": {"zone": "graveyard", "select": {"types_all": ["Creature"]}},
                }
            ]
        },
    ),
]


@pytest.mark.parametrize(
    ("dimension", "a", "b"), EXCLUSIVE_PAIRS, ids=[p[0] for p in EXCLUSIVE_PAIRS]
)
def test_each_exclusivity_dimension_silences_the_conflict(root, dimension, a, b):
    put_inputs(root, INPUTS)
    put(root, play(id="a", **a))
    put(root, play(id="b", **b))
    assert check_report(root) == ([], [])


@pytest.mark.parametrize(
    ("dimension", "a", "b"), EXCLUSIVE_PAIRS, ids=[p[0] for p in EXCLUSIVE_PAIRS]
)
def test_each_dimension_overlapping_is_a_conflict(root, dimension, a, b):
    put_inputs(root, INPUTS)
    put(root, play(id="a", **a))
    put(root, play(id="b", **copy.deepcopy(a)))
    (warning,) = warnings_of(root)
    assert warning["code"] == "play-conflict"
    assert warning["dimensions"][:4] == ["whose_turn", "steps", "opposing_archetypes", "stack"]


# -- FD2 item 7: move-selector dimension, pass-only fallback, ability_index --


def test_two_plays_whose_selectors_overlap_still_conflict(root):
    """FD2 item 7b's negative case: `types_any: [Instant, Creature]` and
    `types_all: [Creature]` are NOT disjoint - a Creature could match either
    - so `move_selector` must not fire, and the pair still conflicts."""
    put_inputs(root, INPUTS)
    put(
        root,
        play(
            id="a",
            moves=[
                {
                    "kind": "cast",
                    "card": {"zone": "hand", "select": {"types_any": ["Instant", "Creature"]}},
                }
            ],
        ),
    )
    put(
        root,
        play(
            id="b",
            moves=[
                {"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Creature"]}}}
            ],
        ),
    )
    (warning,) = warnings_of(root)
    assert warning["code"] == "play-conflict"
    a_fragment = {
        "moves": [
            {
                "kind": "cast",
                "card": {"zone": "hand", "select": {"types_any": ["Instant", "Creature"]}},
            }
        ]
    }
    b_fragment = {
        "moves": [{"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Creature"]}}}]
    }
    assert exclusivity(a_fragment, b_fragment, {}) == []


def test_two_permanent_type_sets_never_prove_move_selector_disjoint(root):
    """Review finding (FD2 7b): an artifact creature matches both a
    types_all: [Artifact] selector and a types_all: [Creature] one, so two
    disjoint PERMANENT type sets never prove the plays' targets cannot
    coincide - only a non-permanent type (Instant/Sorcery) against a
    permanent one is provable."""
    a_fragment = {
        "moves": [{"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Artifact"]}}}]
    }
    b_fragment = {
        "moves": [{"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Creature"]}}}]
    }
    assert exclusivity(a_fragment, b_fragment, {}) == []


def test_disjoint_keywords_any_never_proves_move_selector_disjoint(root):
    """Review finding (FD2 7b): a creature can carry both Flying and Trample
    at once, so a disjoint keywords_any set never proves the two selectors'
    targets cannot coincide."""
    a_fragment = {
        "moves": [
            {
                "kind": "cast",
                "card": {"zone": "hand", "select": {"keywords_any": ["Flying"]}},
            }
        ]
    }
    b_fragment = {
        "moves": [
            {
                "kind": "cast",
                "card": {"zone": "hand", "select": {"keywords_any": ["Trample"]}},
            }
        ]
    }
    assert exclusivity(a_fragment, b_fragment, {}) == []


def test_nonpermanent_type_against_permanent_type_still_proves_move_selector(root):
    """The one type pairing CR 300.1's list can prove disjoint for every
    printed card: Instant/Sorcery (never permanents) against a permanent
    type - this must stay provable after narrowing the rule."""
    a_fragment = {
        "moves": [{"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Instant"]}}}]
    }
    b_fragment = {
        "moves": [{"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Creature"]}}}]
    }
    assert exclusivity(a_fragment, b_fragment, {}) == ["move_selector"]


def test_types_all_instant_against_types_any_including_instant_is_not_disjoint(root):
    """Review finding (FD2 7b): `types_all: [Instant]` requires only the
    Instant type, and any plain Instant already satisfies
    `types_any: [Instant, Creature]` through its first alternative - so the
    two selectors overlap (a bare Instant matches both) and must not be
    proven disjoint just because `Creature` is a permanent type."""
    a_fragment = {
        "moves": [{"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Instant"]}}}]
    }
    b_fragment = {
        "moves": [
            {
                "kind": "cast",
                "card": {"zone": "hand", "select": {"types_any": ["Instant", "Creature"]}},
            }
        ]
    }
    assert exclusivity(a_fragment, b_fragment, {}) == []


def test_kindred_type_never_proves_move_selector_disjoint(root):
    """Review finding (FD2 7b): Kindred (CR 308) is printed on instants and
    sorceries too (Crib Swap is a Kindred Instant), so it is never
    permanent-only and must not count toward a `types_all`/`types_any`
    permanent-type proof against Instant/Sorcery."""
    a_fragment = {
        "moves": [{"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Instant"]}}}]
    }
    b_fragment = {
        "moves": [{"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Kindred"]}}}]
    }
    assert exclusivity(a_fragment, b_fragment, {}) == []


def test_pass_only_never_conflicts_with_a_move_bearing_play(root):
    """FD2 item 7b: a play whose only move is `pass` is a fallback."""
    put(root, play(id="a", moves=[{"kind": "pass"}]))
    put(root, play(id="b"))
    assert check_report(root) == ([], [])


def test_two_pass_only_plays_are_not_covered_by_the_fallback_rule():
    """The fallback exemption names exactly one side pass-only; two pass-only
    plays with nothing else distinguishing them are not exempted."""
    a = {"moves": [{"kind": "pass"}]}
    assert exclusivity(a, a, {}) == []


def test_an_ability_index_activate_passes_lint(root):
    """FD2 item 7a: an activation naming its ability by `ability_index`
    (rather than `ability_text_has`) is exempt from `phrase-too-narrow` on any
    phrase inside that same move - here the source-locating selector's own
    `ability_text_has`, which names no other cached card."""
    doc = play(
        moves=[
            {
                "kind": "activate",
                "source": {
                    "zone": "battlefield",
                    "select": {"ability_text_has": "This exact narrow phrase names one card"},
                },
                "ability_index": 0,
            }
        ]
    )
    put(root, doc)
    assert check_report(root) == ([], [])


def test_a_hand_written_ability_text_has_still_keeps_the_three_card_rule(root):
    """The exemption is scoped to `ability_index`: an activation that still
    names its ability by `ability_text_has` keeps the 3-card generality
    rule."""
    doc = play(
        moves=[
            {
                "kind": "activate",
                "source": {"zone": "battlefield", "select": {}},
                "ability_text_has": "This exact narrow phrase names one card",
            }
        ]
    )
    put(root, doc)
    assert error_codes(root) == ["phrase-too-narrow"]


@pytest.mark.parametrize(
    "mutate_doc",
    [
        lambda doc: doc["moves"][0].update(face={"ability_text_has": "Lightning Bolt"}),
        lambda doc: doc["moves"][0].update(
            sacrifice={"select": {"ability_text_has": "Lightning Bolt"}}
        ),
    ],
    ids=["face", "sacrifice-pick"],
)
def test_a_card_name_in_a_new_cast_field_is_card_name_in_field(root, mutate_doc):
    doc = play(
        moves=[{"kind": "cast", "card": {"zone": "hand", "select": {"types_any": ["Instant"]}}}]
    )
    mutate_doc(doc)
    put(root, doc)
    assert "card-name-in-field" in error_codes(root)


def test_a_card_name_in_keyword_action_card_is_card_name_in_field(root):
    doc = play(
        moves=[
            {
                "kind": "keyword_action",
                "action": "cycle",
                "card": {"zone": "hand", "select": {"ability_text_has": "Lightning Bolt"}},
            }
        ]
    )
    put(root, doc)
    assert "card-name-in-field" in error_codes(root)


def test_a_card_name_in_choice_from_is_card_name_in_field(root):
    doc = play(
        moves=[
            {
                "kind": "answer",
                "answers": [
                    {
                        "decision": "choose_a_quality",
                        "choice_from": {
                            "zone": "battlefield",
                            "side": "me",
                            "select": {"ability_text_has": "Lightning Bolt"},
                        },
                    }
                ],
            }
        ]
    )
    put(root, doc)
    assert "card-name-in-field" in error_codes(root)


def test_next_conflict_names_the_play_and_both_entries(root):
    put(root, play(id="a", priority=0))
    put(root, play(id="b", priority=0))
    put(root, play(id="c", priority=0, when=when("theirs"), next=["a", "b"]))
    errors, warnings = check_report(root)
    assert errors == []
    (warning,) = [w.to_dict() for w in warnings]
    assert warning["code"] == "next-conflict"
    assert warning["plays"] == ["c", "a", "b"]
    assert warning["path"] == f"decks/{SLUG}/plays/c.json"


def test_exclusive_next_entries_are_no_conflict(root):
    put(root, play(id="a", priority=0, when=when("mine")))
    put(root, play(id="b", priority=0, when=when("theirs")))
    put(root, play(id="c", priority=0, next=["a", "b"]))
    assert check_report(root) == ([], [])


def test_an_unreadable_play_is_left_out_of_conflicts(root):
    put(root, play(id="a"))
    put(root, play(id="b", moves=[{"kind": "attack"}]))
    errors, warnings = check_report(root)
    assert [e.code for e in errors] == ["forbidden-kind"]
    assert warnings == []


def test_a_play_with_any_error_is_left_out_of_conflicts(root):
    # battle 1 D06: the errored play raised 25 extra warnings in the stdlib gate
    put(root, play(id="a"))
    put(root, play(id="b"))
    put(root, play(id="s12", opposing_archetypes=["esper_blink"]))
    put(root, {**with_input("plan", "eq", "aggro"), "id": "unknown-input"})
    errors, warnings = check_report(root)
    assert sorted(e.code for e in errors) == ["unknown-archetype-key", "unknown-input"]
    assert [w.plays for w in warnings] == [["a", "b"]]


def test_an_errored_play_is_left_out_of_next_conflicts(root):
    put(root, play(id="a", priority=0))
    put(root, {**with_input("plan", "eq", "aggro"), "id": "bad", "priority": 0})
    put(root, play(id="c", priority=0, when=when("theirs"), next=["a", "bad"]))
    errors, warnings = check_report(root)
    assert [e.code for e in errors] == ["unknown-input"]
    assert warnings == []


# -- F8: advice-only-chained --------------------------------------------------


def test_advice_only_chained_when_the_advice_only_play_itself_has_a_next(root):
    put(root, play(id="a", priority=0))
    put(root, advice("b", next=["a"]))
    (error,) = [e for e in check_report(root)[0] if e.code == "advice-only-chained"]
    assert error.path == f"decks/{SLUG}/plays/b.json"
    assert "'b'" in error.message and "'a'" in error.message


def test_advice_only_chained_when_a_play_names_an_advice_only_play_in_next(root):
    put(root, advice("a"))
    put(root, play(id="b", priority=0, next=["a"]))
    (error,) = [e for e in check_report(root)[0] if e.code == "advice-only-chained"]
    assert error.path == f"decks/{SLUG}/plays/b.json"
    assert "'b'" in error.message and "'a'" in error.message


def test_a_next_chain_of_move_bearing_plays_is_not_advice_only_chained(root):
    put(root, play(id="a", next=["b"], priority=0))
    put(root, play(id="b", priority=0))
    assert check_report(root)[0] == []


def test_an_advice_only_play_with_no_next_is_not_advice_only_chained(root):
    put(root, advice("a"))
    put(root, play(id="b", priority=0))
    assert check_report(root)[0] == []


def test_advice_only_chained_leaves_the_naming_play_out_of_next_conflicts(root):
    # The naming play itself carries the advice-only-chained error, so it is
    # left out of clean docs the same as any other errored play.
    put(root, advice("a"))
    put(root, play(id="c", next=["a"], priority=0))
    errors, warnings = check_report(root)
    assert [e.code for e in errors] == ["advice-only-chained"]
    assert warnings == []


# -- never-applicable: a play whose own conditions contradict ------------------

STACK_TOP = {"kind": "stack_top", "controller": "opponent", "select": {}}


def test_never_applicable_for_an_empty_stack_and_a_required_stack_top(root):
    # battle 1 D10: bt-struct/s26
    put(
        root,
        play(when=when(stack="empty"), true=[FETCH_SHOCK["true"][1], STACK_TOP], false=[]),
    )
    (error,) = check_report(root)[0]
    assert error.code == "never-applicable"
    assert "when.stack" in error.message and "true[1]" in error.message


@pytest.mark.parametrize(
    "over",
    [
        {"when": when(stack="any"), "true": [STACK_TOP], "false": []},
        {"when": when(stack="empty"), "true": [], "false": [STACK_TOP]},
    ],
    ids=["stack-any", "stack-top-in-false"],
)
def test_a_stack_top_can_apply_on_any_stack_or_when_it_must_not_hold(root, over):
    put(root, play(**over))
    assert check_report(root)[0] == []


def test_never_applicable_for_one_condition_in_true_and_false(root):
    life = {"kind": "life", "side": "me", "op": ">=", "n": 8}
    reordered = {"n": 8, "op": ">=", "side": "me", "kind": "life"}
    put(root, play(true=[life], false=[FETCH_SHOCK["false"][0], reordered]))
    (error,) = check_report(root)[0]
    assert error.code == "never-applicable"
    assert "true[0]" in error.message and "false[1]" in error.message


def test_never_applicable_for_a_land_drop_true_and_false_in_one_list(root):
    drops = [
        {"kind": "land_drop_available", "value": True},
        {"kind": "land_drop_available", "value": False},
    ]
    put(root, play(true=drops, false=[]))
    (error,) = check_report(root)[0]
    assert error.code == "never-applicable"
    assert "true[0]" in error.message and "true[1]" in error.message


def test_different_conditions_in_true_and_false_can_apply(root):
    put(
        root,
        play(
            true=[{"kind": "life", "side": "me", "op": ">=", "n": 8}],
            false=[{"kind": "life", "side": "opponent", "op": ">=", "n": 8}],
        ),
    )
    assert check_report(root)[0] == []


# -- FB2 C1/C2/C3: pending_decision, compare, all_of --------------------------------


def pending(decision: str) -> dict[str, Any]:
    return {"kind": "pending_decision", "decision": decision}


def test_pending_decision_dimension_proves_different_pending_kinds_exclusive():
    a = play(true=[pending("scry")], false=[])
    b = play(true=[pending("surveil")], false=[])
    assert exclusivity(a, b, {}) == ["pending_decision"]


def test_pending_decision_dimension_does_not_fire_for_the_same_kind():
    a = play(true=[pending("scry")], false=[])
    b = play(true=[pending("scry")], false=[])
    assert exclusivity(a, b, {}) == []
    assert "pending_decision" in conflict_dimensions(a, b, {})


def test_declare_attackers_and_declare_blockers_plays_are_exclusive_by_pending_decision(root):
    # Combat moves: a declare_attackers play and a declare_blockers play are
    # exclusive by decision (FB2 requirement 2) - the runtime only relaxes
    # the priority gate for a first move whose kind matches the seat's own
    # pending decision, so these two first-move kinds can never both be the
    # one decision pending for a seat at once.
    put_inputs(root, INPUTS)
    put(root, play(id="a", moves=[{"kind": "declare_attackers", "attackers": []}]))
    put(root, play(id="b", moves=[{"kind": "declare_blockers", "blocks": []}]))
    assert check_report(root) == ([], [])


def test_never_applicable_for_two_different_pending_decisions_in_one_true(root):
    put(root, play(true=[pending("scry"), pending("surveil")], false=[]))
    (error,) = check_report(root)[0]
    assert error.code == "never-applicable"
    assert "true[0]" in error.message and "true[1]" in error.message


def test_the_same_pending_decision_twice_in_true_is_not_never_applicable(root):
    put(root, play(true=[pending("scry"), pending("scry")], false=[]))
    assert check_report(root)[0] == []


def test_two_different_pending_decisions_across_true_and_false_is_not_never_applicable(root):
    # Only a contradiction within one play's own true is provable (a seat can
    # hold only one pending decision at once); scry required true and
    # surveil excluded in false do not contradict each other at all.
    put(root, play(true=[pending("scry")], false=[pending("surveil")]))
    assert check_report(root)[0] == []


def test_a_compare_condition_in_true_and_its_identical_twin_in_false_are_exclusive(root):
    cmp = {
        "kind": "compare",
        "left": {"of": "count", "select": {"types_any": ["Creature"]}, "side": "me"},
        "op": ">=",
        "right": {"of": "n", "n": 3},
    }
    put(root, play(id="a", true=[cmp], false=[]))
    put(root, play(id="b", true=[], false=[cmp]))
    assert check_report(root) == ([], [])


def test_two_different_compare_conditions_that_can_overlap_are_not_falsely_exclusive(root):
    left_cmp = {
        "kind": "compare",
        "left": {"of": "power_sum", "select": {"types_any": ["Creature"]}, "side": "me"},
        "op": ">=",
        "right": {"of": "life", "side": "opponent"},
    }
    right_cmp = {
        "kind": "compare",
        "left": {"of": "count", "select": {"types_any": ["Land"]}, "side": "me"},
        "op": ">=",
        "right": {"of": "n", "n": 4},
    }
    a = play(id="a", true=[left_cmp], false=[])
    b = play(id="b", true=[right_cmp], false=[])
    assert exclusivity(a, b, {}) == []
    assert "numeric_range" not in conflict_dimensions(a, b, {})
    put(root, a)
    put(root, b)
    (warning,) = warnings_of(root)
    assert warning["code"] == "play-conflict"
    assert "numeric_range" not in warning["dimensions"]


def test_all_of_is_one_literal_never_decomposed_into_its_members_for_true_false(root):
    # "compare and all_of are canonicalised as single literals for
    # true_false" (FB2 requirement 2): a bare member of an all_of never
    # matches the all_of itself, so no exclusivity is falsely claimed
    # between them.
    member = {"kind": "life", "side": "me", "op": ">=", "n": 8}
    allof = {
        "kind": "all_of",
        "conditions": [member, {"kind": "life", "side": "opponent", "op": "<=", "n": 5}],
    }
    a = play(id="a", true=[member], false=[])
    b = play(id="b", true=[], false=[allof])
    assert exclusivity(a, b, {}) == []
    put(root, a)
    put(root, b)
    (warning,) = warnings_of(root)
    assert warning["code"] == "play-conflict"
    assert "true_false" not in warning["dimensions"]


def test_a_play_conflict_warning_disappears_once_the_complement_condition_is_added(root):
    # An all_of-false play (a) excludes itself whenever every one of its
    # members holds. `all_of` is refused outside `false` (schema), so its
    # complement can only ever be expressed as the other play's own `true`
    # requiring every one of those same members - proven only once ALL of
    # them are present, never from one member alone (that stays the
    # never-decomposed partial-match case covered by
    # test_all_of_is_one_literal_never_decomposed_into_its_members_for_true_false
    # above, so no false exclusivity is ever claimed from a partial match).
    x = {"kind": "life", "side": "me", "op": ">=", "n": 8}
    y = {"kind": "life", "side": "opponent", "op": "<=", "n": 5}
    allof = {"kind": "all_of", "conditions": [x, y]}
    a = play(id="a", true=[], false=[allof])
    b_empty = play(id="b", true=[], false=[])
    put(root, a)
    put(root, b_empty)
    (warning,) = warnings_of(root)
    assert warning["code"] == "play-conflict"
    assert exclusivity(a, b_empty, {}) == []

    b_one_member = play(id="b", true=[x], false=[])
    put(root, b_one_member)
    (warning,) = warnings_of(root)
    assert warning["code"] == "play-conflict"
    assert exclusivity(a, b_one_member, {}) == []

    b_both_members = play(id="b", true=[x, y], false=[])
    put(root, b_both_members)
    assert check_report(root) == ([], [])
    assert exclusivity(a, b_both_members, {}) == ["true_false"]


def life(side: str, op: str, n: int) -> dict[str, Any]:
    return {"kind": "life", "side": side, "op": op, "n": n}


def mana(op: str, n: int) -> dict[str, Any]:
    return {"kind": "untapped_mana", "op": op, "n": n}


def turn(op: str, n: int) -> dict[str, Any]:
    return {"kind": "turn", "op": op, "n": n}


def clock(op: str, value: Any) -> dict[str, Any]:
    return {"kind": "input", "name": "clock", "op": op, "value": value}


def lands(op: str, n: int, *, reordered: bool = False) -> dict[str, Any]:
    select = {"types_all": ["Land"], "tapped": False}
    condition = {
        "kind": "count",
        "zone": "battlefield",
        "side": "me",
        "op": op,
        "n": n,
        "select": select,
    }
    if reordered:
        condition = {
            k: dict(reversed(v.items())) if k == "select" else v for k, v in condition.items()
        }
        condition = dict(reversed(condition.items()))
    return condition


@pytest.mark.parametrize(
    ("true", "false", "first", "second"),
    [
        ([life("me", ">=", 10), life("me", "<=", 5)], [], "true[0]", "true[1]"),
        ([mana(">=", 4), mana("<", 2)], [], "true[0]", "true[1]"),
        ([clock("eq", 3), clock(">=", 5)], [], "true[0]", "true[1]"),
        ([life("me", ">=", 10)], [life("me", ">", 5)], "true[0]", "false[0]"),
        ([lands(">=", 3)], [lands(">", 1, reordered=True)], "true[0]", "false[0]"),
        ([clock("in", [1, 2])], [clock("<", 4)], "true[0]", "false[0]"),
        ([], [mana("<", 4), mana(">=", 2)], "false[0]", "false[1]"),
        ([turn("<=", 2), turn(">=", 3)], [], "true[0]", "true[1]"),
    ],
    ids=[
        "life-ge-and-le",
        "untapped-mana-ge-and-lt",
        "int-input-eq-and-ge",
        "false-list-negated",
        "count-same-selector-reordered",
        "int-input-in-and-negated-lt",
        "two-false-conditions",
        "turn-le-2-and-ge-3",
    ],
)
def test_never_applicable_for_numeric_ranges_that_cannot_overlap(root, true, false, first, second):
    # battle 4 B4-08
    put_inputs(root, INPUTS)
    put(root, play(true=true, false=false))
    (error,) = check_report(root)[0]
    assert error.code == "never-applicable"
    assert f"{first} and {second}" in error.message
    placed = {f"true[{i}]": c for i, c in enumerate(true)}
    placed |= {f"false[{i}]": c for i, c in enumerate(false)}
    for where in (first, second):
        assert json.dumps(placed[where], sort_keys=True) in error.message
    assert "ranges cannot overlap" in error.message


@pytest.mark.parametrize(
    ("true", "false"),
    [
        ([life("me", ">=", 5), life("me", "<=", 10)], []),
        ([life("me", ">=", 10), life("opponent", "<=", 5)], []),
        ([mana(">=", 2)], [mana(">=", 4)]),
        ([clock(">=", 3), clock("<=", 5)], []),
        ([lands(">=", 3)], [{**lands(">", 1), "select": {"types_all": ["Creature"]}}]),
        ([life("me", ">=", 5), life("me", "<=", 5)], []),
        ([turn("<=", 2)], [turn(">=", 2)]),
    ],
    ids=[
        "life-5-to-10",
        "different-sides",
        "untapped-mana-2-to-3",
        "int-input-3-to-5",
        "different-selectors",
        "one-point-range",
        "turn-touching",
    ],
)
def test_numeric_ranges_that_overlap_can_apply(root, true, false):
    put_inputs(root, INPUTS)
    put(root, play(true=true, false=false))
    assert check_report(root)[0] == []


def test_a_never_applicable_play_is_left_out_of_conflicts(root):
    put(root, play(id="a", when=when(stack="any")))
    put(root, play(id="b", when=when(stack="empty"), true=[STACK_TOP], false=[]))
    errors, warnings = check_report(root)
    assert [e.code for e in errors] == ["never-applicable"]
    assert warnings == []


# -- opposing_archetypes and the built-in opposing_archetype: one dimension ----


def archetype_input(op: str, value: Any) -> dict[str, Any]:
    return {"kind": "input", "name": "opposing_archetype", "op": op, "value": value}


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (
            {"opposing_archetypes": ["dimir-frog"]},
            {
                "opposing_archetypes": ["*"],
                "true": [*FETCH_SHOCK["true"], archetype_input("eq", "living-end")],
            },
        ),
        (
            {"opposing_archetypes": ["dimir-frog"]},
            {
                "opposing_archetypes": ["*"],
                "false": [*FETCH_SHOCK["false"], archetype_input("eq", "dimir-frog")],
            },
        ),
        (
            {
                "opposing_archetypes": ["dimir-frog", "living-end"],
                "true": [*FETCH_SHOCK["true"], archetype_input("ne", "living-end")],
            },
            {"opposing_archetypes": ["living-end"]},
        ),
    ],
    ids=["list-vs-input", "list-vs-negated-false-input", "list-narrowed-by-own-input"],
)
def test_an_archetype_list_and_the_built_in_input_are_one_dimension(root, a, b):
    # battle 1 D11: deck cx-archetype-vs-input
    put(root, play(id="a", **a))
    put(root, play(id="b", **b))
    assert check_report(root) == ([], [])


def test_an_archetype_list_overlapping_the_built_in_input_still_conflicts(root):
    put(root, play(id="a", opposing_archetypes=["dimir-frog"]))
    put(
        root,
        play(
            id="b",
            opposing_archetypes=["*"],
            true=[*FETCH_SHOCK["true"], archetype_input("eq", "dimir-frog")],
        ),
    )
    errors, warnings = check_report(root)
    assert errors == []
    (warning,) = [w.to_dict() for w in warnings]
    assert warning["plays"] == ["a", "b"]
    assert warning["dimensions"] == [
        "whose_turn",
        "steps",
        "opposing_archetypes",
        "stack",
        "pending_decision",
        "true_false",
        "numeric_range",
    ]


# -- the CLI: mcp-seat plays check ---------------------------------------------


@pytest.fixture
def no_api(monkeypatch):
    """`plays check` is offline: it must never build a seat or reach an API."""

    def refuse(*args, **kwargs):
        raise AssertionError("plays check reached for the API")

    monkeypatch.setattr(seat_cli, "Seats", refuse)
    monkeypatch.setattr(seat_cli, "api_trouble", refuse)


def test_cli_exits_zero_on_a_clean_root_without_a_uid(root, no_api, capsys):
    put(root, FETCH_SHOCK)
    assert cli("--root", str(root)) == 0
    assert "OK: 1 play(s) across 1 deck(s)" in capsys.readouterr().out


def test_cli_json_exits_zero_on_warnings_alone_and_one_with_an_error(root, no_api, capsys):
    put(root, play(id="a"))
    put(root, play(id="b"))
    assert cli("--root", str(root), "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["errors"] == [] and report["plays"] == 2 and report["decks"] == 1
    assert [w["code"] for w in report["warnings"]] == ["play-conflict"]
    assert report["warnings"][0]["plays"] == ["a", "b"]

    put(root, play(id="c", priority=0, repeat=3))
    assert cli("--root", str(root), "--json") == 1
    report = json.loads(capsys.readouterr().out)
    assert [e["code"] for e in report["errors"]] == ["unknown-field"]
    assert sorted(report["errors"][0]) == ["code", "message", "path"]
    assert [w["code"] for w in report["warnings"]] == ["play-conflict"]


def test_cli_text_mode_prints_coded_errors_to_stderr(root, no_api, capsys):
    put(root, play(priority=101))
    assert cli("--root", str(root)) == 1
    err = capsys.readouterr().err
    assert f"bounds-exceeded decks/{SLUG}/plays/fetch-untapped-shock.json:" in err
    assert "1 play error(s)" in err


def test_cli_deck_filter(root, no_api, capsys, tmp_path):
    put(root, play(priority=101))
    other = make_root(root, slug="dimir-frog")
    put(other, FETCH_SURVEIL, slug="dimir-frog")
    assert cli("--root", str(root), "--deck", "dimir-frog", "--json") == 0
    assert json.loads(capsys.readouterr().out)["decks"] == 1
    assert cli("--root", str(root), "--deck", "no-such-deck") == 1
    assert "no-such-deck" in capsys.readouterr().err


def test_cli_fails_when_the_card_cache_cannot_be_read(root, no_api, capsys):
    (root / "meta" / "card-data.json").unlink()
    put(root, FETCH_SHOCK)
    assert cli("--root", str(root)) == 1
    assert "card-data.json" in capsys.readouterr().err


def test_every_other_command_still_needs_a_uid(capsys):
    with pytest.raises(SystemExit) as raised:
        seat_cli.run(["state"])
    assert raised.value.code == 2
    assert "--uid" in capsys.readouterr().err


# -- FD2 item 6/7: the real mtg-decks corpus still clears the gate ------------

MTG_DECKS_ROOT = Path("/Users/szymonri/Documents/mtg-decks")


def test_every_mtg_decks_play_still_clears_the_lint_gate():
    """FD2's additions - the ability_index phrase-too-narrow exemption, the
    move-selector and pass-fallback exclusivity dimensions, and every new
    schema field - are additive: the real corpus's own errors and warnings
    are unchanged (both empty, at the time of writing)."""
    if not (MTG_DECKS_ROOT / "decks").exists():
        pytest.skip(f"mtg-decks checkout not present at {MTG_DECKS_ROOT}")
    errors, warnings, plays, decks = run_check(MTG_DECKS_ROOT)
    assert plays > 0 and decks > 0
    assert errors == []
    assert warnings == []
