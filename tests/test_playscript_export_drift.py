"""Drift guard for `contract/playscript.schema.json`: `mise run playscript:export`
writes it from `playscripts.schema.Play` (`scripts/export_playscript_schema.py`).
Nothing stops the model changing without the export being re-run, so this
regenerates the schema in memory and asserts it is byte-identical to the
committed file - the same check `mise run playscript:export` run twice would
show as a clean `git status`. Modelled on `test_tools_export_drift.py`.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from playscript_testkit import FETCH_SHOCK, FETCH_SURVEIL

from scripts.export_playscript_schema import OUTPUT, build_schema, render_schema

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_committed_schema_matches_a_fresh_export():
    committed = OUTPUT.read_text()
    fresh = render_schema(build_schema())
    assert fresh == committed, (
        "contract/playscript.schema.json is stale - run `mise run playscript:export` "
        "and commit the result"
    )


def test_export_is_deterministic():
    assert render_schema(build_schema()) == render_schema(build_schema())


def test_output_is_valid_json_on_disk():
    json.loads(OUTPUT.read_text())


def test_the_output_is_where_the_task_says():
    assert OUTPUT == REPO_ROOT / "contract" / "playscript.schema.json"


def test_the_mise_task_runs_the_export_through_uv():
    tasks = tomllib.loads((REPO_ROOT / "mise.toml").read_text())["tasks"]
    task = tasks["playscript:export"]
    assert task["description"].strip()
    assert task["run"] == "uv run python scripts/export_playscript_schema.py"


def test_the_exported_schema_accepts_both_worked_examples_and_refuses_an_unknown_field():
    import jsonschema

    schema = json.loads(OUTPUT.read_text())
    for example in (FETCH_SHOCK, FETCH_SURVEIL):
        jsonschema.validate(example, schema)
    refused = dict(FETCH_SHOCK, repeat=3)
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(refused))


# -- T6: what the committed contract must actually carry ----------
#
# The drift test above proves the file matches the model. It cannot prove the
# MODEL still carries a field: a regeneration that dropped one would leave
# both sides agreeing about its absence and this file green. These pin the
# selector vocabulary a non-Python reader - an editor, a reviewer, mtg-decks'
# stdlib-only `scripts/check_plays.py` - reads out of the contract, so the
# mirror chain has something to mirror.


def _selector_properties() -> dict:
    return json.loads(OUTPUT.read_text())["$defs"]["Selector"]["properties"]


def test_the_contract_carries_payable_any_with_its_four_value_vocabulary():
    field = _selector_properties()["payable_any"]
    assert field["type"] == "array"
    assert field["items"]["enum"] == ["now", "with_a_land", "next_turn", "no"]
    assert field["maxItems"] == len(field["items"]["enum"])


def test_the_contract_carries_unlocks_next_as_a_boolean():
    assert _selector_properties()["unlocks_next"]["type"] == "boolean"


def test_the_contract_is_generated_output_and_carries_no_hand_edit():
    """`render_schema` is the only thing that writes this file, so a fresh
    render is byte-identical. A hand edit - a reordered key, a comment, a
    changed indent - shows up here as a difference, which is what lets a
    reviewer regenerate the file instead of reading it."""
    assert render_schema(build_schema()) == OUTPUT.read_text()
    assert OUTPUT.read_text().endswith("\n")
