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
  `mana_value` on the view, so no rule about either is re-derived here. The
  color vocabulary is `COLOR_LETTERS`: the five colors and `colorless`, by
  name or letter (`C` for colorless), any case. `colorless` matches a view
  whose `colors` is empty (CR 105.2c: "A colorless object has no color") and
  never a colored one; an unknown word matches nothing;
- `ability_text_has` reads each `AbilityView.text` (and a stack ability's own
  `text`), `oracle_has` reads `oracle_text` (a double-faced card's face
  text, as above), both as case-insensitive substrings;
- `searches_for_any` (O3) is parsed from a "search your library for a/an T
  (or T) card" template in either of those texts, narrowed to the five basic
  land types (`BASIC_LAND_TYPES`): a fetchland's printed oracle text, or a
  landcycling card's engine-synthesized ability text - carried on `abilities`
  even in hand (`off_zone_abilities`). A word the template names outside the
  five (Expedition Map's "a land card") contributes nothing, so a generic
  land search never matches it;
- `land_face` (O5) is true when any face the view carries (`faces`, present
  regardless of zone for a modal double-faced card) types as a Land - a
  creature-fronted MDFC land matches it in hand even though a plain
  `types_all: [Land]` never does, since that reads only the up face. A
  single-faced card is read on its own type line, so a plain land matches
  too;
- `missing_from {zones, side, by}` compares the candidate with the cards
  already on that side in those zones, read through `present` (the board
  `conditions.presence` builds from the same redacted view). `by: subtype`
  compares the candidate's subtypes that the selector's own `subtypes_any`
  lists (all its subtypes when there is no `subtypes_any`), `by: name` its
  view's `name`; the candidate matches only when no card there shares any of
  them. A candidate with nothing to compare (no such subtype, no name) never
  matches. The candidate itself counts when it sits in one of the zones.

An empty list constrains nothing. A number the card does not have (a
non-creature's power, a view with no `mana_value`) never matches.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from pydantic import BaseModel

SUPERTYPES = frozenset({"basic", "legendary", "ongoing", "snow", "world"})
#: The five basic land types `searches_for_any` (O3) names.
BASIC_LAND_TYPES = ("Plains", "Island", "Swamp", "Mountain", "Forest")
_BASIC_LAND_TYPES_FOLDED = frozenset(word.casefold() for word in BASIC_LAND_TYPES)
#: The "search your library for a/an T (or T) card" template (only one
#: article, before the first type named): fetchlands ("an Island or Swamp
#: card") and typecycling/landcycling's synthesized ability text ("a Forest
#: card", CR 702.29e) both match it. A word this captures that is not one of
#: the five basic land types (Expedition Map's "a land card") contributes
#: nothing to the matched set.
SEARCH_TEMPLATE = re.compile(r"search your library for an? (\w+)(?: or (\w+))? card", re.I)
COLOR_LETTERS = {
    "white": "w",
    "blue": "u",
    "black": "b",
    "red": "r",
    "green": "g",
    "colorless": "c",
}
COLORLESS = COLOR_LETTERS["colorless"]
#: The cards on `side` in `zones` a `missing_from` compares against.
Present = Callable[[Sequence[str], str], Iterable[dict[str, Any]]]

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


def _searched_basic_types(card: dict[str, Any], oracle_text: str) -> set[str]:
    """The basic land types (casefolded) a "search your library for a/an T
    (or T) card" template in this card's ability or oracle text names -
    O3's `searches_for_any`. Scanning `_texts` (ability text, which carries
    the engine's synthesized Landcycling/typecycling text even in hand -
    `off_zone_abilities`) as well as `oracle_text` (the printed template on a
    fetchland) covers both shapes."""
    found: set[str] = set()
    for text in (*_texts(card), oracle_text):
        for match in SEARCH_TEMPLATE.finditer(text):
            for word in match.groups():
                if word and word.casefold() in _BASIC_LAND_TYPES_FOLDED:
                    found.add(word.casefold())
    return found


def _has_land_face(card: dict[str, Any]) -> bool:
    """O5's `land_face`: does this card have a face that types as a Land? A
    modal double-faced card carries `faces` (`serialize._faces_view`)
    regardless of its current zone or chosen face, so a creature-fronted
    MDFC whose back is a land (Witch Enchanter // Witch-Blessed Meadow)
    matches this in hand even though its up face does not have the Land
    type - `play_land` plays that face by the card's own id (`Game.
    play_land`'s `Card.land_face` mirror read), never a separate id. A
    single-faced card carries no `faces`, so its own type line is read
    instead - a plain land is its own land face."""
    faces = [face for face in card.get("faces") or [] if isinstance(face, dict)]
    if faces:
        return any("land" in parse_type_line(face.get("type_line"))[1] for face in faces)
    return "land" in parse_type_line(card.get("type_line"))[1]


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


def _compared(card: dict[str, Any], by: str, subtypes_any: list[str] | None) -> set[str]:
    """What `missing_from` compares on `card`: its name, or its subtypes
    (narrowed to `subtypes_any` when given)."""
    if by == "name":
        return {str(card["name"])} if card.get("name") else set()
    subtypes = parse_type_line((_face(card) or card).get("type_line"))[2]
    return subtypes & _folded(subtypes_any) if subtypes_any else subtypes


def _missing(wanted: dict[str, Any], card: dict[str, Any], present: Present) -> bool:
    missing = wanted["missing_from"]
    compared = _compared(card, missing["by"], wanted.get("subtypes_any"))
    return bool(compared) and not any(
        compared & _compared(other, missing["by"], None)
        for other in present(missing["zones"], missing["side"])
    )


def matches(
    selector: BaseModel | dict[str, Any], card: dict[str, Any], present: Present | None = None
) -> bool:
    """Does `card` have every characteristic `selector` names? A selector
    with `missing_from` also needs `present`, the board it compares against."""
    wanted = selector.model_dump(exclude_none=True) if isinstance(selector, BaseModel) else selector
    if "missing_from" in wanted and present is None:
        raise ValueError("a selector with missing_from reads the board: pass present")
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
        have = {_color(str(color)) for color in card.get("colors") or []} or {COLORLESS}
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
    if "land_face" in wanted and _has_land_face(card) is not wanted["land_face"]:
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
    if "oracle_has" in wanted and wanted["oracle_has"].casefold() not in oracle:
        return False
    if wanted.get("searches_for_any") and not _folded(wanted["searches_for_any"]) & (
        _searched_basic_types(card, oracle)
    ):
        return False
    return "missing_from" not in wanted or _missing(wanted, card, present)
