"""Selectors: a pure match of one `Selector` against one CardView dict.

Every field reads a characteristic the redacted view already carries, never a
card name (design D3):

- type words come from `type_line`, split into supertypes, card types and
  subtypes on the dash, every face of a split card included;
- a double-faced card (a view carrying `faces`) is read as the one face it
  has in its zone: the front face in every zone but the battlefield and the
  stack (CR 712.8a), the face it was cast as on the stack (the view's `name`),
  and on the battlefield the engine's computed `type_line` and up-face
  `oracle_text` (CR 712.8d-f);
- `colors_any` and `mana_value` read the engine's own `colors` and
  `mana_value` on the view, so no rule about either is re-derived here;
- `ability_text_has` reads each `AbilityView.text` (and a stack ability's own
  `text`), `oracle_has` reads `oracle_text` (a double-faced card's face
  text, as above), both as case-insensitive substrings.

An empty list constrains nothing. A number the card does not have (a
non-creature's power, a view with no `mana_value`) never matches.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel

SUPERTYPES = frozenset({"basic", "legendary", "ongoing", "snow", "world"})
COLOR_LETTERS = {
    "white": "w",
    "blue": "u",
    "black": "b",
    "red": "r",
    "green": "g",
    "colorless": "c",
}

_COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    "<": operator.lt,
}


def compare(value: Any, op: str, n: Any) -> bool:
    """`value op n` for a real number; anything else never compares."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return _COMPARE[op](value, n)


def parse_type_line(type_line: str | None) -> tuple[set[str], set[str], set[str]]:
    """(supertypes, card types, subtypes), casefolded, across every face."""
    supertypes: set[str] = set()
    types: set[str] = set()
    subtypes: set[str] = set()
    for face in (type_line or "").replace(" - ", " — ").split("//"):
        left, _, right = face.partition("—")
        for word in left.split():
            folded = word.casefold()
            (supertypes if folded in SUPERTYPES else types).add(folded)
        subtypes.update(word.casefold() for word in right.split())
    return supertypes, types, subtypes


def _folded(values: Iterable[Any]) -> set[str]:
    return {str(value).casefold() for value in values}


def _color(value: str) -> str:
    folded = value.casefold()
    return COLOR_LETTERS.get(folded, folded)


def _texts(card: dict[str, Any]) -> list[str]:
    texts = [str(ability.get("text") or "") for ability in card.get("abilities") or []]
    if isinstance(card.get("text"), str):
        texts.append(card["text"])
    return texts


def _face(card: dict[str, Any]) -> dict[str, Any] | None:
    """The face a double-faced card view has in its zone, or None when the
    view's own `type_line`/`oracle_text` already are its characteristics (a
    single-faced card, or a permanent the engine computed)."""
    faces = [face for face in card.get("faces") or [] if isinstance(face, dict)]
    if not faces or card.get("zone") == "battlefield":
        return None
    if card.get("zone") == "stack":
        return next((face for face in faces if face.get("name") == card.get("name")), faces[0])
    return faces[0]


def matches(selector: BaseModel | dict[str, Any], card: dict[str, Any]) -> bool:
    """Does `card` have every characteristic `selector` names?"""
    wanted = selector.model_dump(exclude_none=True) if isinstance(selector, BaseModel) else selector
    face = _face(card)
    printed = face if face is not None else card
    supertypes, types, subtypes = parse_type_line(printed.get("type_line"))

    if wanted.get("types_all") and not _folded(wanted["types_all"]) <= types:
        return False
    if wanted.get("types_any") and not _folded(wanted["types_any"]) & types:
        return False
    if wanted.get("subtypes_any") and not _folded(wanted["subtypes_any"]) & subtypes:
        return False
    if wanted.get("supertypes_any") and not _folded(wanted["supertypes_any"]) & supertypes:
        return False
    if wanted.get("colors_any"):
        have = {_color(str(color)) for color in card.get("colors") or []}
        if not {_color(color) for color in wanted["colors_any"]} & have:
            return False
    for field in ("mana_value", "power", "toughness"):
        if field in wanted and not compare(
            card.get(field), wanted[field]["op"], wanted[field]["n"]
        ):
            return False
    if wanted.get("keywords_any") and not _folded(wanted["keywords_any"]) & _folded(
        card.get("keywords") or []
    ):
        return False
    if "tapped" in wanted and card.get("tapped") is not wanted["tapped"]:
        return False
    if "counters" in wanted:
        kind = wanted["counters"]["name"].casefold()
        held = sum(
            amount
            for name, amount in (card.get("counters") or {}).items()
            if str(name).casefold() == kind
        )
        if not compare(held, wanted["counters"]["op"], wanted["counters"]["n"]):
            return False
    if "ability_text_has" in wanted:
        phrase = wanted["ability_text_has"].casefold()
        if not any(phrase in text.casefold() for text in _texts(card)):
            return False
    oracle = str(printed.get("oracle_text") or "").casefold()
    return "oracle_has" not in wanted or wanted["oracle_has"].casefold() in oracle
