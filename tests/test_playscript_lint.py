"""`plays check`: the corpus rules over a guides root, one tmp negative per code.

The codes, the input vocabulary and the conflict warnings are a contract
shared verbatim with mtg-decks' `scripts/check_plays.py` (M1b). Every case
here builds a tmp root that differs from a clean one by exactly the shape
under test, so the owner's live checkout is never read.
"""

from __future__ import annotations

import copy
import json
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
)


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
    assert CODES[14:17] == ("unknown-input", "invalid-input-value", "malformed-inputs")
    assert CODES[17:] == (
        "unknown-decision-kind",
        "unknown-cast-cost",
        "duplicate-step",
        "wildcard-with-keys",
    )
    assert len(CODES) == 21
    assert WARNING_CODES == ("play-conflict", "next-conflict")
    assert EXCLUSIVITY_DIMENSIONS == (
        "whose_turn",
        "steps",
        "opposing_archetypes",
        "stack",
        "input",
        "true_false",
        "numeric_range",
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


def test_too_many_plays(root):
    for i in range(13):
        put(root, play(id=f"p{i}", priority=0))
    assert error_codes(root) == ["too-many-plays"]


def test_inputs_json_is_not_a_play(root):
    for i in range(12):
        put(root, play(id=f"p{i}", priority=0))
    put_inputs(root, {"schema_version": 1, "inputs": []})
    assert check_report(root)[0] == []


def test_too_many_active_for_a_wildcard(root):
    for i in range(7):
        put(
            root,
            play(
                id=f"p{i}",
                priority=10,
                when={"whose_turn": "mine", "steps": ["upkeep"], "stack": "empty"},
            ),
        )
    assert "too-many-active-for-archetype" in error_codes(root)


def test_a_wildcard_play_counts_toward_every_named_key(root):
    for i in range(4):
        put(root, play(id=f"star{i}", priority=10))
    for i in range(3):
        put(root, play(id=f"frog{i}", priority=10, opposing_archetypes=["dimir-frog"]))
    errors = [e for e in check_report(root)[0] if e.code == "too-many-active-for-archetype"]
    assert len(errors) == 1 and "'dimir-frog'" in errors[0].message


def test_a_disabled_play_does_not_count_as_active(root):
    for i in range(6):
        put(root, play(id=f"p{i}", priority=10))
    put(root, play(id="off", priority=0))
    assert check_report(root)[0] == []


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
        "true_false",
        "numeric_range",
    ]


def test_a_disabled_play_is_never_compared(root):
    put(root, play(id="a"))
    put(root, play(id="b", priority=0))
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
