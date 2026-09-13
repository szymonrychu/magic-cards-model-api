"""The conflict corpus shared with mtg-decks M1b, run against this repo's gate.

`tests/fixtures/conflict_corpus.json` is a byte-identical copy of mtg-decks'
`scripts/tests/fixtures/conflict_corpus.json` (branch `script-dsl`, commit
d67c7c4). Each case is two play fragments tagged exclusive (with every
dimension that proves it) or conflicting (with the dimensions the pair
overlaps on). mtg-decks asserts those verdicts against `check_plays.py`; this
file asserts the same verdicts and the same dimension arrays against
`playscripts.lint` and through `mcp-seat plays check`, which is what proves
the two implementations agree on every case. A7 refreshes the copy (and the
hash below) whenever mtg-decks moves.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from playscript_testkit import make_root, play, put, put_inputs

from magic_cards_model_api import seat_cli
from magic_cards_model_api.playscripts.inputs import input_catalog
from magic_cards_model_api.playscripts.lint import (
    EXCLUSIVITY_DIMENSIONS,
    conflict_dimensions,
    exclusivity,
)

CORPUS_PATH = Path(__file__).resolve().parent / "fixtures" / "conflict_corpus.json"
UPSTREAM_SHA256 = "d2b2f1a35aa50d2b79469a0c765f68f4769936d08808236a7ab5079c4eb9c03c"
CORPUS = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
CATALOG = input_catalog(CORPUS["inputs"], CORPUS["matchup_keys"])
CASES = CORPUS["cases"]


def test_the_fixture_is_the_unedited_mtg_decks_copy():
    assert hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest() == UPSTREAM_SHA256


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
            assert proven == case["proven_by"]
        else:
            assert case["verdict"] == "conflicting"
            assert proven == []
            assert conflict_dimensions(a, b, CATALOG) == case["dimensions"]


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

    assert seat_cli.run(["plays", "check", "--root", str(root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["errors"] == [], report["errors"]
    if case["verdict"] == "exclusive":
        assert report["warnings"] == []
    else:
        (warning,) = report["warnings"]
        assert warning["code"] == "play-conflict"
        assert warning["plays"] == ["a", "b"]
        assert warning["dimensions"] == case["dimensions"]
