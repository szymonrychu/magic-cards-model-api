"""Which CR 118.9 alternative cost a caster's `alternative_cost` value names.

`legal_actions` advertises a printed pitch offer by its `description` (Force
of Negation's row reads `alternative_cost: "exile a blue card from your
hand"`), while `Casting.use_alternative_cost` only ever matched an option's
`keyword` or `source_name` - so the one label a seat was shown was the one
spelling the cast refused, in a live prod game: "Force of Negation has no
alternative cost named 'exile a blue card from your hand' (CR 118.9)".

This resolves any reasonable spelling to the option's `index` before the
engine is asked, off the engine's own `AlternativeCostOption` list - never a
card-name table: the index, the canonical value (`canonical_value`, the same
string `legal_actions` puts on the row), the keyword, the offering card's
name, and the printed phrase in any case, punctuation, or with its "If ...,"
/ "you may" / "rather than pay this spell's mana cost" framing kept on.
"""

from __future__ import annotations

import re
from typing import Any

#: "If it's not your turn, " / "you may " ahead of the cost itself.
_HEAD = re.compile(r"^(?:if [^,]*,\s*)?(?:you may\s+)?")
#: "... rather than pay this spell's mana cost" after it.
_TAIL = re.compile(
    r"\s*rather than pay(?:ing)?\s+(?:this spell's|its|the|that spell's)\s+mana cost$"
)
#: The "(if <condition>)" a conditional printed offer's description carries.
_CONDITION = re.compile(r"\s*\(if [^)]*\)$")


def normalise(text: str) -> str:
    """Case-, punctuation- and whitespace-insensitive form of one spelling,
    with the printed sentence's framing around the cost itself dropped."""
    folded = text.casefold().replace("’", "'").strip().rstrip(".").strip()
    folded = _TAIL.sub("", _HEAD.sub("", folded))
    words = re.sub(r"[^\w{}]+", " ", folded.replace("'", ""))
    return " ".join(words.split())


def canonical_value(option: Any) -> str:
    """The one `alternative_cost` value a seat should send for `option` - the
    same string `legal_actions` puts on the option's own cast row: the
    keyword for a keyword cost, the printed phrase for a printed offer, and
    the granting card's name otherwise."""
    if option.keyword:
        return str(option.keyword)
    if str(getattr(option.source, "value", option.source)) == "printed":
        return str(option.description)
    return str(option.source_name)


def _spellings(option: Any) -> set[str]:
    raw = {canonical_value(option), option.description, option.source_name}
    if option.keyword:
        raw.add(option.keyword)
        raw.add(f"{option.keyword} {option.description}")
    raw.add(_CONDITION.sub("", option.description))
    return {normalise(text) for text in raw if text}


def accepted_list(options: list[Any]) -> str:
    """Every option as "index or 'canonical value'", with why an unavailable
    one cannot be announced right now."""
    parts = []
    for option in options:
        entry = f"{option.index} or {canonical_value(option)!r}"
        if option.unavailable:
            entry += f" (unavailable: {option.unavailable})"
        parts.append(entry)
    return "; ".join(parts) if parts else "none"


def resolve(spell_name: str, options: list[Any], which: int | str) -> int | str:
    """The `index` of the option `which` names, for
    `Casting.use_alternative_cost`. An int, or a spelling of a card with no
    options at all, is handed back untouched for the engine's own refusal.

    Raises `ValueError` naming the card and listing every accepted value when
    nothing matches, or when the spelling matches more than one available
    option (CR 118.9a: only one may be paid, so the api does not guess)."""
    if isinstance(which, int) or not options:
        return which
    wanted = normalise(which)
    if wanted.isdigit():
        return int(wanted)
    matches = [option for option in options if wanted and wanted in _spellings(option)]
    available = [option for option in matches if option.available]
    if len(available) > 1:
        raise ValueError(
            f"{spell_name}: alternative_cost {which!r} matches more than one alternative cost "
            f"and only one may be paid (CR 118.9a) - send one of: {accepted_list(available)}"
        )
    if matches:
        return (available or matches)[0].index
    raise ValueError(
        f"{spell_name} has no alternative cost matching {which!r} (CR 118.9) - "
        f"accepted alternative_cost values right now: {accepted_list(options)}"
    )
