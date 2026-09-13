"""The conflict corpus shared with mtg-decks M1b, run against this repo's gate.

`tests/fixtures/conflict_corpus.json` is the corpus mtg-decks'
`scripts/tests/fixtures/conflict_corpus.json` shares byte for byte. F2l
(battle 1 D06, D10, D11) moved it ahead here first: the `errored` verdict, the
never-applicable cases and the archetype list vs built-in input cases land in
this copy, and the mtg-decks follow-up copies it back and mirrors the rules in
`check_plays.py`. The hash below pins the copy both repos must hold.

Each case is two play fragments. `exclusive` lists every dimension that proves
it (`proven_by`); `conflicting` proves none and lists the dimensions the pair
overlaps on; `errored` is judged by the pure algorithm like the others, but
fragment `b` carries the `errors` the gate raises against its file, and a play
with an error is left out of conflicts, so the gate warns about nothing. This
file asserts those verdicts and dimension arrays against `playscripts.lint` and
through `mcp-seat plays check`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from playscript_testkit import SLUG, make_root, play, put, put_inputs

from magic_cards_model_api import seat_cli
from magic_cards_model_api.playscripts.inputs import input_catalog
from magic_cards_model_api.playscripts.lint import (
    CODES,
    EXCLUSIVITY_DIMENSIONS,
    conflict_dimensions,
    exclusivity,
)

CORPUS_PATH = Path(__file__).resolve().parent / "fixtures" / "conflict_corpus.json"
CORPUS_SHA256 = "f196d44c8c0f09d25f9b552160ef5af3a11c92220baa788ece2ad77eab8f3e66"
CORPUS = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
CATALOG = input_catalog(CORPUS["inputs"], CORPUS["matchup_keys"])
CASES = CORPUS["cases"]


def test_the_fixture_is_the_pinned_shared_copy():
    assert hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest() == CORPUS_SHA256


def test_the_corpus_tags_both_verdicts_for_every_dimension_and_the_identical_set():
    assert [d["name"] for d in CORPUS["dimensions"]] == list(EXCLUSIVITY_DIMENSIONS)
    tagged = {(case["verdict"], case["dimension"]) for case in CASES}
    for dimension in EXCLUSIVITY_DIMENSIONS:
        assert ("exclusive", dimension) in tagged
        assert ("conflicting", dimension) in tagged
    assert ("conflicting", "identical") in tagged
    names = [case["name"] for case in CASES]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_every_case_is_judged_as_tagged_in_both_orders(case):
    for a, b in ((case["a"], case["b"]), (case["b"], case["a"])):
        proven = exclusivity(a, b, CATALOG)
        if case["verdict"] == "exclusive":
            assert case["dimension"] in proven
        else:
            assert case["verdict"] in ("conflicting", "errored")
        assert proven == case.get("proven_by", [])
        if "dimensions" in case:
            assert conflict_dimensions(a, b, CATALOG) == case["dimensions"]


#: The cases F2l added (battle 1 D06, D10, D11), each carrying both
#: `proven_by` and `dimensions`.
F2L_CASES = {
    "archetypes-list-vs-built-in-input-disjoint": "exclusive",
    "archetypes-list-vs-built-in-input-false-list-negated": "exclusive",
    "archetypes-list-narrowed-by-its-own-built-in-input": "exclusive",
    "archetypes-list-vs-built-in-input-overlapping": "conflicting",
    "errored-unknown-archetype-key-is-left-out-of-conflicts": "errored",
    "errored-never-applicable-empty-stack-and-stack-top": "errored",
    "errored-never-applicable-one-condition-true-and-false": "errored",
}


def test_the_corpus_pins_errored_plays_and_the_archetype_dimension():
    by_name = {case["name"]: case for case in CASES}
    for name, verdict in F2L_CASES.items():
        case = by_name[name]
        assert case["verdict"] == verdict
        assert "proven_by" in case and "dimensions" in case
    errored = [case for case in CASES if case["verdict"] == "errored"]
    assert {code for case in errored for code in case["errors"]} == {
        "unknown-archetype-key",
        "never-applicable",
    }
    assert all(code in CODES for case in errored for code in case["errors"])


def test_the_corpus_pins_a_numeric_range_never_applicable_play():
    # battle 4 B4-08: ranges on one key inside one play, a false condition negated
    by_name = {case["name"]: case for case in CASES}
    case = by_name["errored-never-applicable-numeric-range-in-one-play"]
    assert case["verdict"] == "errored"
    assert case["errors"] == ["never-applicable"]
    assert case["proven_by"] == []
    assert "numeric_range" in case["dimensions"]


def _root_for(tmp_path: Path, first: dict[str, Any], second: dict[str, Any]) -> Path:
    root = make_root(tmp_path, matchups=tuple(CORPUS["matchup_keys"]))
    put_inputs(root, {"schema_version": 1, "inputs": CORPUS["inputs"]})
    put(root, play(id="a", priority=10, **first))
    put(root, play(id="b", priority=10, **second))
    return root


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
@pytest.mark.parametrize("swapped", [False, True], ids=["a-b", "b-a"])
def test_plays_check_reports_every_case_as_tagged(case, swapped, tmp_path, capsys):
    first, second = (case["b"], case["a"]) if swapped else (case["a"], case["b"])
    root = _root_for(tmp_path, first, second)

    exit_code = seat_cli.run(["plays", "check", "--root", str(root), "--json"])
    report = json.loads(capsys.readouterr().out)

    if case["verdict"] == "errored":
        errored = f"decks/{SLUG}/plays/{'a' if swapped else 'b'}.json"
        assert exit_code == 1
        assert sorted((e["code"], e["path"]) for e in report["errors"]) == sorted(
            (code, errored) for code in case["errors"]
        )
        assert report["warnings"] == []
        return
    assert exit_code == 0
    assert report["errors"] == [], report["errors"]
    if case["verdict"] == "exclusive":
        assert report["warnings"] == []
    else:
        (warning,) = report["warnings"]
        assert warning["code"] == "play-conflict"
        assert warning["plays"] == ["a", "b"]
        assert warning["dimensions"] == case["dimensions"]


#: The missing_from cases (B4-10): a selector's `missing_from` is part of the
#: condition's normalized key, never a dimension of its own.
MISSING_FROM_CASES = {
    "missing-from-count-at-least-one-vs-none-same-selector": "exclusive",
    "missing-from-count-at-least-one-vs-below-one-zones-reordered": "exclusive",
    "missing-from-same-condition-true-and-false": "exclusive",
    "missing-from-selectors-differ-only-in-missing-from": "conflicting",
    "missing-from-different-zones": "conflicting",
}


def test_the_corpus_pins_missing_from_as_part_of_the_key_only():
    by_name = {case["name"]: case for case in CASES}
    for name, verdict in MISSING_FROM_CASES.items():
        case = by_name[name]
        assert case["verdict"] == verdict
        assert "proven_by" in case and "dimensions" in case
        assert "missing_from" in json.dumps(case)
