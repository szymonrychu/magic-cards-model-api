"""`malformed-inputs` messages carry real values, and every problem is reported (battle 6, B6-01).

Battle 6 saw `inputs[0].name: {name!r} is built in and is never redeclared`
from the scripts PUT route and from `plays check`: `PydanticCustomError`
substitutes `{name}` only, so `{name!r}` went out verbatim. The same linter
stopped at the first problem, while mtg-decks' `scripts/check_plays.py`
reports every one.

`PARITY_DOC` and `PARITY_MESSAGES` are one run of `scripts/check_plays.py
--root <tmp> --json` (mtg-decks 6e402ca, the G3 mirror) over that document,
copied verbatim: the api answers the same codes, messages and order.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from playscript_testkit import SLUG, make_root, put_inputs
from test_playscript_lint import MALFORMED_INPUTS
from test_script_routes import corpus, seat_uid

from magic_cards_model_api import seat_cli
from magic_cards_model_api.playscripts.inputs import inputs_problems
from magic_cards_model_api.playscripts.lint import check_report

SRC = Path(__file__).resolve().parents[1] / "src"

#: A `str.format`-style placeholder left in a message: `{name}`, `{name!r}`,
#: `{index:>3}`. Mana symbols (`{C}`, `{2}`, `{W/U}`) are never lowercase.
RAW_TEMPLATE = re.compile(r"\{[a-z_][a-z0-9_]*(![rsa])?(:[^}]*)?\}")

PARITY_DOC = {
    "schema_version": 1,
    "inputs": [
        {"name": "opposing_archetype", "type": "bool", "description": "d", "required": True},
        {
            "name": "plan",
            "type": "enum",
            "values": ["race", "control"],
            "description": "d",
            "required": False,
        },
        {"name": "plan", "type": "bool", "description": "d", "required": False},
        {
            "name": "turns_left",
            "type": "int",
            "min": 0,
            "max": 20,
            "default": 99,
            "description": "d",
            "required": False,
        },
    ],
}

PARITY_MESSAGES = [
    "inputs[0].name: 'opposing_archetype' is built in and is never redeclared",
    "inputs[2].name: 'plan' is declared twice",
    "inputs[3].default: is not a field of a 'int' input",
]


# -- every message template in the source ------------------------------------------


def _custom_error_calls() -> list[tuple[str, ast.Call]]:
    calls = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "PydanticCustomError"
            ):
                calls.append((f"{path.relative_to(SRC)}:{node.lineno}", node))
    return calls


def test_every_custom_error_template_names_only_context_keys_plainly():
    calls = _custom_error_calls()
    assert calls, "the scan found no PydanticCustomError call at all"
    bad = []
    for where, call in calls:
        template = call.args[1] if len(call.args) > 1 else None
        if not isinstance(template, ast.Constant) or not isinstance(template.value, str):
            continue
        context = call.args[2] if len(call.args) > 2 else None
        keys = (
            {k.value for k in context.keys if isinstance(k, ast.Constant)}
            if isinstance(context, ast.Dict)
            else set()
        )
        for match in re.finditer(r"\{([^{}]*)\}", template.value):
            if match.group(1) not in keys:
                bad.append(f"{where}: {{{match.group(1)}}} in {template.value!r}")
    assert bad == [], "pydantic substitutes a bare {key} from the context only"


def test_no_malformed_inputs_message_carries_a_raw_template():
    for label, doc in [*MALFORMED_INPUTS, ("parity", PARITY_DOC)]:
        if isinstance(doc, str):
            continue
        for message in inputs_problems(doc):
            assert not RAW_TEMPLATE.search(message), (label, message)


def test_no_plays_check_message_carries_a_raw_template(tmp_path):
    for label, doc in [*MALFORMED_INPUTS, ("parity", PARITY_DOC)]:
        root = make_root(tmp_path / label)
        put_inputs(root, doc)
        errors, _ = check_report(root)
        assert errors, label
        for error in errors:
            assert not RAW_TEMPLATE.search(error.message), (label, error.message)


# -- all problems, in check_plays.py's order and words ------------------------------


def test_inputs_problems_reports_every_problem_as_check_plays_does():
    assert inputs_problems(PARITY_DOC) == PARITY_MESSAGES


def test_plays_check_reports_every_malformed_input_as_check_plays_does(tmp_path, capsys):
    root = make_root(tmp_path)
    put_inputs(root, PARITY_DOC)
    errors, _ = check_report(root)
    assert [(e.code, e.path, e.message) for e in errors] == [
        ("malformed-inputs", f"decks/{SLUG}/plays/inputs.json", message)
        for message in PARITY_MESSAGES
    ]

    assert seat_cli.run(["plays", "check", "--root", str(root), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert [(e["code"], e["message"]) for e in report["errors"]] == [
        ("malformed-inputs", message) for message in PARITY_MESSAGES
    ]


def test_the_scripts_route_reports_every_malformed_input_with_its_values(
    client: TestClient, game: str
) -> None:
    alice = seat_uid(game, "alice")
    put = client.put(
        f"/api/games/{game}/seats/{alice}/scripts",
        params={"uid": alice},
        json={**corpus(), "inputs": PARITY_DOC["inputs"]},
    )
    assert put.status_code == 422, put.text
    errors: list[dict[str, Any]] = put.json()["errors"]
    assert [(e["code"], e["path"], e["message"]) for e in errors] == [
        ("malformed-inputs", "inputs", message) for message in PARITY_MESSAGES
    ]
    for error in errors:
        assert not RAW_TEMPLATE.search(error["message"])


def test_the_scripts_route_names_a_redeclared_built_in_and_a_duplicate(
    client: TestClient, game: str
) -> None:
    alice = seat_uid(game, "alice")
    entry = {"type": "bool", "description": "d", "required": False}
    for inputs, message in (
        (
            [{"name": "opposing_archetype", **entry}],
            "inputs[0].name: 'opposing_archetype' is built in and is never redeclared",
        ),
        ([{"name": "a", **entry}, {"name": "a", **entry}], "inputs[1].name: 'a' is declared twice"),
    ):
        put = client.put(
            f"/api/games/{game}/seats/{alice}/scripts",
            params={"uid": alice},
            json={**corpus(), "inputs": inputs},
        )
        assert put.status_code == 422, put.text
        assert [e["message"] for e in put.json()["errors"]] == [message]
