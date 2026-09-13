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
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

from .schema import SCHEMA_VERSION, WILDCARD, NonBlank, PlaysError

INPUTS_FILE = "inputs.json"
BUILTIN_INPUT = "opposing_archetype"
INPUT_OPS = ("eq", "ne", "in", "not_in", "<", "<=", ">", ">=")
LIST_OPS = ("in", "not_in")
EQUALITY_OPS = ("eq", "ne", "in", "not_in")
OPS_BY_INPUT_TYPE = {"enum": EQUALITY_OPS, "bool": EQUALITY_OPS, "int": INPUT_OPS}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Input(_Strict):
    name: NonBlank
    description: NonBlank
    required: bool


class EnumInput(_Input):
    type: Literal["enum"]
    values: list[NonBlank] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def _distinct(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise PydanticCustomError("repeated_value", "must not repeat a value")
        return values


class BoolInput(_Input):
    type: Literal["bool"]


class IntInput(_Input):
    type: Literal["int"]
    min: int = None
    max: int = None

    @model_validator(mode="after")
    def _ordered(self) -> IntInput:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise PydanticCustomError(
                "min_above_max", "min {min} is above max {max}", {"min": self.min, "max": self.max}
            )
        return self


InputSpec = Annotated[EnumInput | BoolInput | IntInput, Field(discriminator="type")]


class InputsFile(_Strict):
    schema_version: Literal[1]
    inputs: list[InputSpec]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version_is_not_a_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise PydanticCustomError("literal_error", f"Input should be {SCHEMA_VERSION}")
        return value

    @model_validator(mode="after")
    def _names(self) -> InputsFile:
        seen: set[str] = set()
        for index, entry in enumerate(self.inputs):
            if entry.name == BUILTIN_INPUT:
                raise PydanticCustomError(
                    "builtin_redeclared",
                    "inputs[{index}].name: {name!r} is built in and is never redeclared",
                    {"index": index, "name": BUILTIN_INPUT},
                )
            if entry.name in seen:
                raise PydanticCustomError(
                    "duplicate_name",
                    "inputs[{index}].name: {name!r} is declared twice",
                    {"index": index, "name": entry.name},
                )
            seen.add(entry.name)
        return self


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _where(loc: tuple[Any, ...]) -> str:
    text = ""
    for part in loc:
        if isinstance(part, int):
            text += f"[{part}]"
        elif part not in ("enum", "bool", "int"):
            text = f"{text}.{part}" if text else str(part)
    return text


def inputs_problems(doc: Any) -> list[str]:
    """Every way an inputs.json document misses its shape, as messages."""
    try:
        InputsFile.model_validate(doc)
    except ValidationError as exc:
        return [
            f"{_where(error['loc'])}: {error['msg']}" if error["loc"] else error["msg"]
            for error in exc.errors()
        ]
    return []


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
    return [
        entry.model_dump(exclude_none=True) for entry in InputsFile.model_validate(doc).inputs
    ], []


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
