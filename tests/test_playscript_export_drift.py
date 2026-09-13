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
