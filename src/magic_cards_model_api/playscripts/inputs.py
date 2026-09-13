"""Agent inputs: facts a script reads that the board cannot show.

A deck declares its inputs in the optional `decks/<slug>/plays/inputs.json`:

    {"schema_version": 1,
     "inputs": [{name, type: enum|bool|int, values?, min?, max?, description, required}]}

`values` belongs to an enum only (non-empty, distinct, non-blank strings);
`min`/`max` belong to an int only (optional integers, min <= max). Names are
unique, and `opposing_archetype` is built in - an enum of `*` plus the matchup
stems of the deck's newest analysis run - so it is never redeclared. Any of
that missed is `malformed-inputs`, the code shared with mtg-decks M1b.

An `input` condition, `{name, op, value}`, fits its input when the op suits
the type (enum and bool: eq, ne, in, not_in; int: all eight) and every value,
including every entry of an in/not_in list, is one of the enum's values, a
boolean, or an integer within min..max.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .schema import SCHEMA_VERSION, WILDCARD, PlaysError

INPUTS_FILE = "inputs.json"
BUILTIN_INPUT = "opposing_archetype"
INPUT_OPS = ("eq", "ne", "in", "not_in", "<", "<=", ">", ">=")
LIST_OPS = ("in", "not_in")
EQUALITY_OPS = ("eq", "ne", "in", "not_in")
OPS_BY_INPUT_TYPE = {"enum": EQUALITY_OPS, "bool": EQUALITY_OPS, "int": INPUT_OPS}
INPUT_KEYS = ("name", "type", "description", "required")
INPUT_KEYS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "enum": ("values",),
    "bool": (),
    "int": ("min", "max"),
}


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _problems(doc: Any) -> Iterator[str]:
    if not isinstance(doc, dict):
        yield "must be a JSON object"
        return
    for key in doc:
        if key not in ("schema_version", "inputs"):
            yield f"{key}: is not a field of {INPUTS_FILE}"
    if not (_is_int(doc.get("schema_version")) and doc["schema_version"] == SCHEMA_VERSION):
        yield f"schema_version must be {SCHEMA_VERSION}"
    inputs = doc.get("inputs")
    if not isinstance(inputs, list):
        yield "inputs must be a list"
        return
    seen: set[str] = set()
    for i, entry in enumerate(inputs):
        where = f"inputs[{i}]"
        if not isinstance(entry, dict):
            yield f"{where}: must be an object"
            continue
        kind = entry.get("type")
        if kind not in INPUT_KEYS_BY_TYPE:
            yield f"{where}.type: must be one of {', '.join(INPUT_KEYS_BY_TYPE)}"
        allowed = INPUT_KEYS + INPUT_KEYS_BY_TYPE.get(kind, ())
        for key in entry:
            if key not in allowed:
                yield f"{where}.{key}: is not a field of a {kind!r} input"
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            yield f"{where}.name: must be a non-empty string"
        elif name == BUILTIN_INPUT:
            yield f"{where}.name: {BUILTIN_INPUT!r} is built in and is never redeclared"
        elif name in seen:
            yield f"{where}.name: {name!r} is declared twice"
        else:
            seen.add(name)
        if not isinstance(entry.get("description"), str) or not entry["description"].strip():
            yield f"{where}.description: must be a non-empty string"
        if not isinstance(entry.get("required"), bool):
            yield f"{where}.required: must be true or false"
        if kind == "enum":
            values = entry.get("values")
            if not (
                isinstance(values, list)
                and values
                and all(isinstance(v, str) and v.strip() for v in values)
            ):
                yield f"{where}.values: must be a non-empty list of non-empty strings"
            elif len(set(values)) != len(values):
                yield f"{where}.values: must not repeat a value"
        elif kind == "int":
            for bound in ("min", "max"):
                if bound in entry and not _is_int(entry[bound]):
                    yield f"{where}.{bound}: must be an integer"
            if (
                _is_int(entry.get("min"))
                and _is_int(entry.get("max"))
                and entry["min"] > entry["max"]
            ):
                yield f"{where}: min {entry['min']} is above max {entry['max']}"


def inputs_problems(doc: Any) -> list[str]:
    """Every way an inputs.json document misses its shape, as messages - all of
    them, in document order, worded and ordered exactly as mtg-decks'
    `scripts/check_plays.py` `_inputs_problems` reports them."""
    return list(_problems(doc))


def load_inputs(deck: Path, root: Path) -> tuple[list[dict[str, Any]] | None, list[PlaysError]]:
    """The deck's declared inputs, or None with its malformed-inputs errors.

    A deck with no inputs.json declares nothing and is clean."""
    path = deck / "plays" / INPUTS_FILE
    if not path.is_file():
        return [], []
    rel = _rel(path, root)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [PlaysError("malformed-inputs", rel, f"not readable JSON: {exc}")]
    problems = inputs_problems(doc)
    if problems:
        return None, [PlaysError("malformed-inputs", rel, problem) for problem in problems]
    return doc["inputs"], []


def newest_run(deck: Path) -> Path | None:
    """The deck's newest analysis run directory, by the run's date suffix."""
    runs = [p for p in (deck / "analysis").glob("*/") if p.is_dir()]
    if not runs:
        return None

    def order(run: Path) -> tuple[str, str]:
        match = re.search(r"(\d{4}-\d{2}-\d{2})$", run.name)
        return (match.group(1) if match else "", run.name)

    return max(runs, key=order)


def newest_matchup_keys(deck: Path) -> set[str]:
    """Matchup stems of the deck's newest analysis run, by the run's date suffix."""
    newest = newest_run(deck)
    if newest is None:
        return set()
    return {p.stem for p in (newest / "domain3-matchup-threat" / "matchups").glob("*.json")}


def input_catalog(declared: list[dict[str, Any]], matchup_keys: Any) -> dict[str, dict[str, Any]]:
    """Every input a condition may name: the built-in plus the declared ones."""
    builtin = {
        "name": BUILTIN_INPUT,
        "type": "enum",
        "values": [WILDCARD, *sorted(matchup_keys)],
        "description": "The opponent's archetype as the agent asserts it: * or a matchup stem.",
        "required": False,
    }
    return {BUILTIN_INPUT: builtin, **{entry["name"]: entry for entry in declared}}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _input_entry_problem(entry: Any, spec: dict[str, Any]) -> str | None:
    name, kind = spec["name"], spec["type"]
    if kind == "enum":
        if not isinstance(entry, str) or entry not in spec["values"]:
            return f"{entry!r} is not a value of the {name!r} input ({', '.join(spec['values'])})"
    elif kind == "bool":
        if not isinstance(entry, bool):
            return f"{entry!r} is not true or false, as the bool input {name!r} needs"
    else:
        lo, hi = spec.get("min"), spec.get("max")
        if not _is_int(entry):
            return f"{entry!r} is not an integer, as the int input {name!r} needs"
        if (lo is not None and entry < lo) or (hi is not None and entry > hi):
            return f"{entry} is outside the int input {name!r}'s range {lo}..{hi}"
    return None


def supplied_input_problem(name: str, value: Any, catalog: dict[str, dict[str, Any]]) -> str | None:
    """Why one input an agent supplies at run time does not fit `catalog`
    (`input_catalog`), or None: a name the catalog does not hold, or a value
    that is not one of the enum's values, a boolean, or an integer within
    min..max - the same judgement `input_value_problem` gives a condition's
    value."""
    spec = catalog.get(name)
    if spec is None:
        return f"{name!r} is neither declared in {INPUTS_FILE} nor the built-in {BUILTIN_INPUT!r}"
    return _input_entry_problem(value, spec)


def input_value_problem(condition: dict[str, Any], spec: dict[str, Any]) -> str | None:
    """Why an input condition's op or value does not fit its input, or None."""
    op, value, kind = condition["op"], condition["value"], spec["type"]
    if op not in OPS_BY_INPUT_TYPE[kind]:
        return (
            f"op {op!r} does not fit the {kind} input {spec['name']!r}; "
            f"use one of {', '.join(OPS_BY_INPUT_TYPE[kind])}"
        )
    if op in LIST_OPS:
        if not isinstance(value, list) or not value:
            return f"op {op!r} needs a non-empty list of values"
        entries = value
    elif isinstance(value, list):
        return f"op {op!r} takes one value, not a list"
    else:
        entries = [value]
    for entry in entries:
        problem = _input_entry_problem(entry, spec)
        if problem:
            return problem
    return None
