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
- `land_face` (O5) is true when a *modal* double-faced card's face is marked
  `land_face` (`serialize._faces_view`, present regardless of zone) - a
  creature-fronted MDFC land matches it in hand even though a plain
  `types_all: [Land]` never does, since that reads only the up face. A
  transform or meld card carries `faces` too but never a `land_face` mark:
  its land back is reached only by transforming or melding (CR 712.2), never
  a land drop, so it never matches even though one of its faces types as a
  Land. A single-faced card is read on its own type line, so a plain land
  matches too;
- `missing_from {zones, side, by}` compares the candidate with the cards
  already on that side in those zones, read through `present` (the board
  `conditions.presence` builds from the same redacted view). `by: subtype`
  compares the candidate's subtypes that the selector's own `subtypes_any`
  lists (all its subtypes when there is no `subtypes_any`), `by: name` its
  view's `name`; the candidate matches only when no card there shares any of
  them. A candidate with nothing to compare (no such subtype, no name) never
  matches. The candidate itself counts when it sits in one of the zones.
- `enters_any` (O4), `payable_any` and `unlocks_next` (T5), `unlocks`,
  `unlocks_by_search`, `unlocks_by_basic_search`
  (O7) and `affordable` (item 3) read `hypo`, the engine-computed facts keyed
  by card id (`hypothetical_facts.py`), never the card view itself - the view
  carries no `enters`/`payable_any`/`unlocks`/`castable_now` field of its own. `hypo=None`
  (the default, no facts fetched at all) raises `HypotheticalRequired`; a
  `hypo` given but with no entry for this card's id means the same as any
  other absent characteristic and never matches, either value. `payable_any`
  matches when the card's own `payable_any` fact is one of the words asked
  for; a card the engine answers no payability for (an X cost, whose cost is
  not a number until it is announced) carries no such fact and so matches
  nothing at all - it fails closed rather than reading as `no`, because "the
  engine cannot say" and "the engine says it cannot be paid" are different
  answers and only the second is a reason to skip the card. `unlocks_next`
  reads its own bool by identity, exactly as `unlocks` does. `affordable
  {extra}` compares the card's own `mana_value` with `untapped_mana + extra`
  (`conditions.untapped_mana`, a plain view computation) *and* reads
  `castable_now`, since the mana value alone ignores whether the card has a
  legal target at all - `legal_actions` still offers a cast row for a
  target-less spell (Fatal Push, no creature anywhere), just with an empty
  target candidate list, so a mana-only comparison used to read it as
  affordable. `untapped_mana=None` (not given) never matches rather than
  raising, since every real caller has a view in scope to compute it from.

An empty list constrains nothing. A number the card does not have (a
non-creature's power, a view with no `mana_value`) never matches.
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
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
#: Engine-computed per-card facts (`hypothetical_facts.py`), keyed by card id:
#: `enters` (an `EntersState` value), `payable_any` (a `PAYABLE_STATES` word),
#: `unlocks`, `unlocks_next`, `unlocks_by_search` and
#: `unlocks_by_basic_search` (bool). A card with no entry carries no facts,
#: the same as any other characteristic the view does not have.
Hypo = Mapping[str, Mapping[str, Any]]
#: The selector fields `matches` cannot answer without `hypo` - see the
#: module docstring. `affordable` joined this list at item 3: a mana_value
#: comparison alone ignores whether the card has a legal target at all
#: (`legal_actions` still offers a cast row for a target-less spell, just
#: with empty target candidates), so `affordable` now also reads
#: `hypothetical_facts.py`'s `castable_now`.
_HYPO_FIELDS = (
    "enters_any",
    "payable_any",
    "unlocks",
    "unlocks_next",
    "unlocks_by_search",
    "unlocks_by_basic_search",
    "affordable",
)


class HypotheticalRequired(ValueError):
    """A selector reads one of `_HYPO_FIELDS` - `enters_any`, `payable_any`,
    `unlocks`, `unlocks_next`, `unlocks_by_search`, `unlocks_by_basic_search`
    or `affordable` - and `matches` was called with no `hypo` at all: the
    caller never fetched the engine-computed facts
    (`hypothetical_facts.py`) this selector needs."""


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
    """O5's `land_face`: does this card have a modal-double-faced land face
    (CR 712.12)? A modal double-faced card carries `faces`
    (`serialize._faces_view`) regardless of its current zone or chosen face,
    each marked `land_face: True` only on the one engine's own `Card.
    land_face` names - a creature-fronted MDFC whose back is a land (Witch
    Enchanter // Witch-Blessed Meadow) matches this in hand even though its
    up face does not have the Land type, and `play_land` plays that face by
    the card's own id (`Game.play_land`'s `Card.land_face` mirror read),
    never a separate id. A transform or meld card also carries `faces` (the
    UI shows both), but never one marked `land_face`: its land back (Treasure
    Map // Treasure Cove) is reached only by transforming or melding
    (CR 712.2), never a land drop, so this never matches it even though a
    face of it plainly types as a Land. A single-faced card carries no
    `faces`, so its own type line is read instead - a plain land is its own
    land face."""
    faces = [face for face in card.get("faces") or [] if isinstance(face, dict)]
    if faces:
        return any(face.get("land_face") is True for face in faces)
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
    selector: BaseModel | dict[str, Any],
    card: dict[str, Any],
    present: Present | None = None,
    *,
    untapped_mana: int | None = None,
    hypo: Hypo | None = None,
) -> bool:
    """Does `card` have every characteristic `selector` names? A selector
    with `missing_from` also needs `present`, the board it compares against.
    `enters_any`/`payable_any`/`unlocks`/`unlocks_next`/`unlocks_by_search`/
    `unlocks_by_basic_search`/
    `affordable` (item 3) need `hypo`, the engine-computed facts keyed by
    card id (`HypotheticalRequired` when it is not given at all);
    `affordable` also reads `untapped_mana`, which never raises for a
    missing one on its own - see the module docstring."""
    wanted = selector.model_dump(exclude_none=True) if isinstance(selector, BaseModel) else selector
    if "missing_from" in wanted and present is None:
        raise ValueError("a selector with missing_from reads the board: pass present")
    if hypo is None and any(field in wanted for field in _HYPO_FIELDS):
        raise HypotheticalRequired(
            "a selector reading enters_any/payable_any/unlocks/unlocks_next/"
            "unlocks_by_search/unlocks_by_basic_search/affordable needs hypo: "
            "see hypothetical_facts.py"
        )
    facts = (hypo or {}).get(card.get("id")) or {}
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
    if wanted.get("enters_any") and facts.get("enters") not in set(wanted["enters_any"]):
        return False
    if wanted.get("payable_any") and facts.get("payable_any") not in set(wanted["payable_any"]):
        return False
    if "unlocks" in wanted and facts.get("unlocks") is not wanted["unlocks"]:
        return False
    if "unlocks_next" in wanted and facts.get("unlocks_next") is not wanted["unlocks_next"]:
        return False
    if (
        "unlocks_by_search" in wanted
        and facts.get("unlocks_by_search") is not wanted["unlocks_by_search"]
    ):
        return False
    if (
        "unlocks_by_basic_search" in wanted
        and facts.get("unlocks_by_basic_search") is not wanted["unlocks_by_basic_search"]
    ):
        return False
    if "affordable" in wanted:
        mana_value = card.get("mana_value")
        extra = wanted["affordable"]["extra"]
        if untapped_mana is None or mana_value is None:
            return False
        if not compare(mana_value, "<=", untapped_mana + extra):
            return False
        # Item 3: mana value alone ignores targets - `legal_actions` still
        # offers a cast row for a spell with a mandatory target and none
        # legal (Fatal Push, no creature anywhere), just with an empty
        # candidate list, so a mana-only comparison read a target-less 1-drop
        # as affordable. `castable_now` (`hypothetical_facts.py`) is the read
        # that actually checks target availability.
        if not facts.get("castable_now", False):
            return False
    return "missing_from" not in wanted or _missing(wanted, card, present)
