"""The play-script corpus gate behind `mcp-seat plays check`.

Mirrors mtg-decks' stdlib `scripts/check_plays.py` (M1b) rule for rule: the
same error codes, the same input vocabulary, the same exclusivity algorithm
and conflict warnings, proven in sync by the shared
`tests/fixtures/conflict_corpus.json`. The structural half is
`schema.play_errors` (the pydantic model); the rest reads the given guides
root: `meta/card-data.json` for the name and phrase rules, and each deck's
newest analysis run for its archetype keys.

Corpus rules (a deck's plays together), by code:

- `id-mismatch`: the file stem equals `id`.
- `card-name-in-field`: no machine-read string equals a card-data card name.
  A phrase (`ability_text_has`, `oracle_has`) equal to a card name is refused
  too, basic land names included: `oracle_has: "Island"` fails although
  "Island" is in many oracle texts. Select by subtype instead
  (`subtypes_any: ["Island"]`), which is the intended form.
- `phrase-too-narrow`: every `ability_text_has` / `oracle_has` phrase is in the
  oracle text of at least 3 cached cards.
- `unknown-next`: every `next` id is a play of the same deck.
- `next-cycle`: the `next` graph is acyclic, self-edges included.
- `chain-too-deep`: the longest `next` chain is at most 3 plays, measured with
  every play that can reach itself left out, so a cycle is reported once as
  `next-cycle` and never masks the depth of the rest of the graph.
- Play-count caps (`too-many-plays`, `too-many-advice` and
  `too-many-active-for-archetype`/O8) are removed (owner, 2026-09-24): a
  deck may carry any number of move-bearing, advice-only or
  archetype-active plays. The per-decision review submission (one
  generalized play per decision the seat made that game, merged into the
  deck) is what made a fixed ceiling a liability rather than a backstop.
  What `check_deck` still proves in bounded time regardless of play count
  is pairwise non-overlap - see "Conflict warnings" below.
- `advice-only-chained` (F8): an advice-only play's own `next` is non-empty,
  or some play names an advice-only play in its `next` - either way one error
  naming both play ids, since a play with no moves commits nothing and so has
  nothing to chain into or out of.
- `unknown-archetype-key`: every archetype key is `*` or a matchup stem of the
  deck's newest analysis run. A non-blank key the schema's key pattern refuses
  (`esper_blink`) is this code alone, never also `malformed-play`.
- `unknown-input`: an `input` condition names a declared input or
  `opposing_archetype`.
- `invalid-input-value`: an `input` op and value fit the input's type and range.
- `malformed-inputs`: `inputs.json` has its shape and does not redeclare the
  built-in.
- `never-applicable`: a play's own fields do not contradict. `when.stack` is
  `empty` while a `true` condition is a `stack_top` (named: the first such
  `true[i]`), or one normalized condition must both hold and not hold (named:
  the first two places, `true[i]`/`false[j]` or two indexes of one list after
  the `land_drop_available` flip), or two numeric conditions on one key (the
  `numeric_range` key: count, zone_size, life, untapped_mana or turn with the same
  other fields, or one int input) have ranges that cannot overlap, a false
  condition negated first (named: the first such pair, one error per key;
  a pair that is one normalized condition is left to the rule before).

A type-line field may hold a string that is also a card name when it is a
type-line word on >= 3 cached cards (`Mountain`). `name`, `intent` and `note`
are prose and may name cards - the exemption is top-level only, so a
machine-read field nested anywhere below the top level is judged like any
other, its key coinciding with a prose field's name notwithstanding.

Conflict warnings and advice-only-chained's "chained into" half are both
proofs about what can *commit* at one decision point; a play with no moves
commits nothing, so an advice-only play is outside each of those proofs by
construction, not exempted from them by policy. Conflict warnings never
fail the gate, are an O(n^2) pass over the deck's enabled, move-bearing
plays regardless of how many the deck carries, and a play with any error
reported against its own file is left out of them. Two plays are provably exclusive when
at least one dimension holds: `whose_turn` (mine vs theirs), `steps`
(disjoint), `opposing_archetypes` (the archetype sets do not intersect: a
play's set is its list, every archetype when it holds `*`, narrowed by each of
its conditions on the built-in `opposing_archetype`), `stack` (one empty, the
other needs a `stack_top` in `true`), `input` (same input, value sets that
cannot both hold), `true_false` (one normalized condition in one play's
`true` and the other's `false`), `numeric_range` (count, zone_size, life,
untapped_mana, turn or int-input ranges on one key that cannot overlap). A `false`
condition is negated before the input and numeric checks. A selector's `missing_from` is
never a dimension of its own: it is part of the normalized condition, its
`zones` a set like every scalar list, so selectors that differ only in it are
different keys and prove nothing, while one identical selector still proves a
range (`>= 1` against `== 0`) or true_false. `play-conflict` is
raised for two enabled plays that are not exclusive, `next-conflict` for two
entries of one play's `next`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .inputs import (
    BUILTIN_INPUT,
    INPUT_OPS,
    INPUTS_FILE,
    LIST_OPS,
    input_catalog,
    input_value_problem,
    load_inputs,
    newest_matchup_keys,
)
from .schema import CODES as SCHEMA_CODES
from .schema import OPS, WILDCARD, PlaysError, play_errors

__all__ = [
    "CODES",
    "EXCLUSIVITY_DIMENSIONS",
    "WARNING_CODES",
    "CardIndex",
    "CorpusError",
    "PlaysError",
    "PlaysWarning",
    "check_deck",
    "check_play",
    "check_report",
    "conflict_dimensions",
    "exclusivity",
    "run_check",
    "run_cli",
]

#: The schema's codes, then the corpus rules this gate adds on top of them.
CODES = (*SCHEMA_CODES, "never-applicable")
WARNING_CODES = ("play-conflict", "next-conflict")
EXCLUSIVITY_DIMENSIONS = (
    "whose_turn",
    "steps",
    "opposing_archetypes",
    "stack",
    "input",
    "true_false",
    "numeric_range",
)

MAX_CHAIN = 3
PHRASE_MIN_CARDS = 3
PROSE_KEYS = ("name", "intent", "note")
PHRASE_KEYS = ("ability_text_has", "oracle_has")
#: Fields whose values are drawn from a closed, type-line-like vocabulary
#: (card names may coincide with a common word here without meaning the
#: card): the type words, O3's `searches_for_any` land types, and O6's
#: `choice` (a creature type such as Eldrazi is also a type-line word).
TYPE_LINE_KEYS = (
    "types_all",
    "types_any",
    "subtypes_any",
    "supertypes_any",
    "searches_for_any",
    "choice",
)
NUMERIC_KINDS = ("count", "zone_size", "life", "untapped_mana", "turn")
NEGATED = {
    "eq": "ne",
    "ne": "eq",
    "in": "not_in",
    "not_in": "in",
    "==": "!=",
    "!=": "==",
    "<": ">=",
    ">=": "<",
    "<=": ">",
    ">": "<=",
}


class CorpusError(PlaysError):
    """A `PlaysError` whose code may also be one this gate adds to the schema's."""

    def __post_init__(self) -> None:
        if self.code not in CODES:
            raise ValueError(f"unknown play error code {self.code!r}")


class PlaysWarning:
    """A conflict: plays that could both apply. Reported, never failing the gate."""

    def __init__(
        self, code: str, path: str, plays: list[str], dimensions: list[str], message: str
    ) -> None:
        if code not in WARNING_CODES:
            raise ValueError(f"unknown play warning code {code!r}")
        self.code = code
        self.path = path
        self.plays = plays
        self.dimensions = dimensions
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "plays": self.plays,
            "dimensions": self.dimensions,
            "message": self.message,
        }

    def __eq__(self, other: object) -> bool:
        return isinstance(other, PlaysWarning) and self.to_dict() == other.to_dict()

    def __hash__(self) -> int:
        return hash(json.dumps(self.to_dict(), sort_keys=True))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _advice_only(doc: dict[str, Any]) -> bool:
    """A play whose `moves` is absent or empty: it commits nothing to the
    game, so it is outside every proof about what can commit at one decision
    point (play-conflict, next-conflict) by construction, not by an
    exemption on top of them."""
    moves = doc.get("moves")
    return not (isinstance(moves, list) and len(moves) > 0)


def _join(where: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{where}[{key}]"
    return f"{where}.{key}" if where else str(key)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# -- the card cache ------------------------------------------------------------


class CardIndex:
    """`meta/card-data.json` as the two indexes the name and phrase rules read."""

    def __init__(self, cards: dict[str, Any]) -> None:
        self.names: set[str] = set()
        self.oracle: list[str] = []
        self.type_words: dict[str, int] = {}
        for key, card in cards.items():
            self.names.add(key.casefold())
            for face in key.split(" // "):
                self.names.add(face.casefold())
            self.oracle.append(str(card.get("oracle_text") or "").casefold())
            for word in set(str(card.get("type_line") or "").casefold().split()):
                self.type_words[word] = self.type_words.get(word, 0) + 1
        self._phrases: dict[str, int] = {}

    @classmethod
    def load(cls, path: Path) -> CardIndex:
        return cls(json.loads(path.read_text(encoding="utf-8"))["cards"])

    def phrase_matches(self, phrase: str) -> int:
        key = phrase.casefold()
        if key not in self._phrases:
            self._phrases[key] = sum(1 for text in self.oracle if key in text)
        return self._phrases[key]

    def is_name(self, text: str, field: str | None) -> bool:
        key = text.casefold()
        if key not in self.names:
            return False
        return not (field in TYPE_LINE_KEYS and self.type_words.get(key, 0) >= PHRASE_MIN_CARDS)


def _machine_strings(value: Any, where: str = "", field: str | None = None):
    """Every string a machine reads: all of them except top-level prose."""
    if isinstance(value, str):
        yield where, field, value
    elif isinstance(value, list):
        for index, entry in enumerate(value):
            yield from _machine_strings(entry, _join(where, index), field)
    elif isinstance(value, dict):
        for key, entry in value.items():
            if not where and key in PROSE_KEYS:
                continue
            yield from _machine_strings(entry, _join(where, key), key)


def _refused_key_pattern(error: PlaysError, doc: Any) -> bool:
    """The schema refusing a non-blank archetype key by its pattern alone: the
    shared contract judges that key as `unknown-archetype-key`, nothing more."""
    keys = doc.get("opposing_archetypes") if isinstance(doc, dict) else None
    if error.code != "malformed-play" or not isinstance(keys, list):
        return False
    return any(
        isinstance(key, str)
        and bool(key.strip())
        and error.message.startswith(f"opposing_archetypes[{index}]: ")
        for index, key in enumerate(keys)
    )


def check_play(doc: Any, stem: str, rel: str, cards: CardIndex) -> list[PlaysError]:
    """One play's structural, id, card-name and phrase errors."""
    errors = [e for e in play_errors(doc, rel) if not _refused_key_pattern(e, doc)]
    if not isinstance(doc, dict):
        return errors
    if isinstance(doc.get("id"), str) and doc["id"] != stem:
        errors.append(
            PlaysError("id-mismatch", rel, f"id {doc['id']!r} is not the file stem {stem!r}")
        )
    for where, field, text in _machine_strings(doc):
        if cards.is_name(text, field):
            errors.append(
                PlaysError(
                    "card-name-in-field",
                    rel,
                    f"{where}: {text!r} is a card name; select by characteristics",
                )
            )
        if field in PHRASE_KEYS and text.strip():
            hits = cards.phrase_matches(text)
            if hits < PHRASE_MIN_CARDS:
                errors.append(
                    PlaysError(
                        "phrase-too-narrow",
                        rel,
                        f"{where}: {text!r} appears in {hits} cached card(s)' oracle text, "
                        f"needs at least {PHRASE_MIN_CARDS}",
                    )
                )
    return errors


# -- input conditions ------------------------------------------------------------


def _conditions(doc: dict[str, Any], polarity: bool) -> list[dict[str, Any]]:
    found = doc.get("true" if polarity else "false")
    return [c for c in found if isinstance(c, dict)] if isinstance(found, list) else []


def _readable_input(condition: dict[str, Any]) -> bool:
    name = condition.get("name")
    return (
        condition.get("kind") == "input"
        and isinstance(name, str)
        and bool(name.strip())
        and condition.get("op") in INPUT_OPS
        and "value" in condition
    )


def _input_errors(
    doc: dict[str, Any], rel: str, catalog: dict[str, Any], declared_ok: bool
) -> list[PlaysError]:
    errors = []
    for polarity in (True, False):
        for index, condition in enumerate(_conditions(doc, polarity)):
            if not _readable_input(condition):
                continue
            where = f"{'true' if polarity else 'false'}[{index}]"
            spec = catalog.get(condition["name"])
            if spec is None:
                # A malformed inputs.json declares nothing that can be judged.
                if declared_ok:
                    errors.append(
                        PlaysError(
                            "unknown-input",
                            rel,
                            f"{where}: {condition['name']!r} is neither declared in "
                            f"{INPUTS_FILE} nor the built-in {BUILTIN_INPUT!r}",
                        )
                    )
                continue
            problem = input_value_problem(condition, spec)
            if problem:
                errors.append(PlaysError("invalid-input-value", rel, f"{where}: {problem}"))
    return errors


# -- exclusivity: can two plays both apply at one decision point? -----------------


def _canon(value: Any) -> Any:
    """Sorted keys, and every list of scalars sorted and de-duplicated."""
    if isinstance(value, dict):
        return {key: _canon(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        items = [_canon(v) for v in value]
        if any(isinstance(v, dict | list) for v in items):
            return items
        return sorted({json.dumps(v): v for v in items}.values(), key=json.dumps)
    return value


def _literal(condition: dict[str, Any], polarity: bool) -> tuple[bool, str]:
    """(must hold, normalized text): `land_drop_available value false` in one
    list reads as `value true` in the other."""
    condition, holds = _canon(condition), polarity
    if condition.get("kind") == "land_drop_available" and condition.get("value") is False:
        condition, holds = {**condition, "value": True}, not polarity
    return holds, json.dumps(condition, sort_keys=True)


def _literals(doc: dict[str, Any]) -> set[tuple[bool, str]]:
    return {_literal(c, polarity) for polarity in (True, False) for c in _conditions(doc, polarity)}


def _placed(doc: dict[str, Any]):
    """(where, polarity, condition) for every object condition, true then false."""
    for polarity in (True, False):
        found = doc.get("true" if polarity else "false")
        for index, condition in enumerate(found if isinstance(found, list) else []):
            if isinstance(condition, dict):
                yield f"{'true' if polarity else 'false'}[{index}]", polarity, condition


def _never_applicable(doc: dict[str, Any], rel: str, catalog: dict[str, Any]) -> list[PlaysError]:
    """A play whose own fields contradict, so no decision point satisfies it."""
    errors: list[PlaysError] = []
    placed = list(_placed(doc))
    if _when(doc).get("stack") == "empty":
        for where, polarity, condition in placed:
            if polarity and condition.get("kind") == "stack_top":
                errors.append(
                    CorpusError(
                        "never-applicable",
                        rel,
                        f"when.stack is empty but {where} needs a stack_top; "
                        "the play can never apply",
                    )
                )
                break
    first: dict[tuple[bool, str], str] = {}
    reported: set[str] = set()
    for where, polarity, condition in placed:
        holds, text = _literal(condition, polarity)
        other = first.get((not holds, text))
        if other is not None and text not in reported:
            reported.add(text)
            errors.append(
                CorpusError(
                    "never-applicable",
                    rel,
                    f"{other} and {where} contradict: {text} must both hold and not hold, "
                    "so the play can never apply",
                )
            )
        first.setdefault((holds, text), where)
    ranges: list[tuple[str, str, tuple[Any, ...], str]] = []
    ranged: set[str] = set()
    for where, polarity, condition in placed:
        numeric = _constraint(condition, polarity, catalog)[1]
        if numeric is None:
            continue
        key, ints = numeric
        text = json.dumps(_canon(condition), sort_keys=True)
        for other_where, other_key, other_ints, other_text in ranges:
            if (
                key == other_key
                and key not in ranged
                and text != other_text
                and _ints_disjoint(other_ints, ints)
            ):
                ranged.add(key)
                errors.append(
                    CorpusError(
                        "never-applicable",
                        rel,
                        f"{other_where} and {where} contradict: {other_text} and {text} "
                        "are on one key whose ranges cannot overlap (a false condition "
                        "negated first), so the play can never apply",
                    )
                )
                break
        ranges.append((where, key, ints, text))
    return errors


def _tighter(a: Any, b: Any, pick: Any) -> Any:
    if a is None:
        return b
    if b is None:
        return a
    return pick(a, b)


def _in_range(v: int, r: tuple[Any, ...]) -> bool:
    _, lo, hi, excluded = r
    return (lo is None or v >= lo) and (hi is None or v <= hi) and v not in excluded


def _ints(op: str, value: Any, lo: Any, hi: Any) -> tuple[Any, ...]:
    """The integers in lo..hi (None: unbounded) satisfying `x op value`:
    ("set", values) or ("range", lo, hi, excluded)."""
    unbounded = ("range", lo, hi, frozenset())
    if op in ("eq", "=="):
        return ("set", frozenset(v for v in [value] if _in_range(v, unbounded)))
    if op == "in":
        return ("set", frozenset(v for v in value if _in_range(v, unbounded)))
    if op in ("ne", "!="):
        return ("range", lo, hi, frozenset([value]))
    if op == "not_in":
        return ("range", lo, hi, frozenset(value))
    if op in (">=", ">"):
        return ("range", _tighter(lo, value + (op == ">"), max), hi, frozenset())
    return ("range", lo, _tighter(hi, value - (op == "<"), min), frozenset())


def _ints_disjoint(x: tuple[Any, ...], y: tuple[Any, ...]) -> bool:
    if x[0] == "set" and y[0] == "set":
        return not x[1] & y[1]
    if y[0] == "set":
        x, y = y, x
    if x[0] == "set":
        return not any(_in_range(v, y) for v in x[1])
    lo, hi = _tighter(x[1], y[1], max), _tighter(x[2], y[2], min)
    if lo is None or hi is None:
        return False
    excluded = x[3] | y[3]
    if hi - lo + 1 > len(excluded):
        return False
    return all(v in excluded for v in range(lo, hi + 1))


def _constraint(condition: dict[str, Any], polarity: bool, catalog: dict[str, Any]):
    """(input constraint or None, numeric constraint or None) of one condition,
    a false one negated. An int input has both."""
    kind = condition.get("kind")
    if kind == "input" and _readable_input(condition):
        spec = catalog.get(condition["name"])
        if spec is None or input_value_problem(condition, spec):
            return None, None
        op = condition["op"] if polarity else NEGATED[condition["op"]]
        value = condition["value"]
        if spec["type"] == "int":
            ints = _ints(op, value, spec.get("min"), spec.get("max"))
            key = json.dumps({"kind": "input", "name": spec["name"]})
            return (spec["name"], ("ints", ints)), (key, ints)
        domain = frozenset(spec["values"] if spec["type"] == "enum" else (True, False))
        chosen = frozenset(value if op in LIST_OPS else [value])
        allowed = domain & chosen if op in ("eq", "in") else domain - chosen
        return (spec["name"], ("values", allowed)), None
    if kind in NUMERIC_KINDS and condition.get("op") in OPS and _is_int(condition.get("n")):
        op = condition["op"] if polarity else NEGATED[condition["op"]]
        key = json.dumps(
            _canon({k: v for k, v in condition.items() if k not in ("op", "n")}), sort_keys=True
        )
        floor = None if kind == "life" else 0
        return None, (key, _ints(op, condition["n"], floor, None))
    return None, None


def _constraints(doc: dict[str, Any], catalog: dict[str, Any]):
    """(input constraints, numeric constraints) of one play, false conditions
    negated. An int input lands in both."""
    inputs, numeric = [], []
    for _, polarity, condition in _placed(doc):
        found_input, found_numeric = _constraint(condition, polarity, catalog)
        if found_input is not None:
            inputs.append(found_input)
        if found_numeric is not None:
            numeric.append(found_numeric)
    return inputs, numeric


def _values_disjoint(x: tuple[Any, ...], y: tuple[Any, ...]) -> bool:
    if x[0] == "ints" and y[0] == "ints":
        return _ints_disjoint(x[1], y[1])
    if x[0] == "values" and y[0] == "values":
        return not x[1] & y[1]
    return False


def _when(doc: dict[str, Any]) -> dict[str, Any]:
    return doc.get("when") if isinstance(doc.get("when"), dict) else {}


def _string_set(value: Any) -> set[str]:
    return {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()


def _needs_stack_top(doc: dict[str, Any]) -> bool:
    return any(c.get("kind") == "stack_top" for c in _conditions(doc, True))


def _archetypes(doc: dict[str, Any], inputs: list[Any]) -> frozenset[str] | None:
    """The archetypes a play can apply against, None for every one: its
    `opposing_archetypes` (every one when it holds `*`) narrowed by each of its
    constraints on the built-in input."""
    listed = _string_set(doc.get("opposing_archetypes"))
    found = None if not listed or WILDCARD in listed else frozenset(listed)
    for name, (tag, values) in inputs:
        if name == BUILTIN_INPUT and tag == "values":
            found = values if found is None else found & values
    return found


def exclusivity(a: dict[str, Any], b: dict[str, Any], catalog: dict[str, Any]) -> list[str]:
    """The dimensions, in EXCLUSIVITY_DIMENSIONS order, that prove a and b
    exclusive. An empty list means they could both apply: a conflict."""
    proven = []
    if {_when(a).get("whose_turn"), _when(b).get("whose_turn")} == {"mine", "theirs"}:
        proven.append("whose_turn")
    steps_a, steps_b = _string_set(_when(a).get("steps")), _string_set(_when(b).get("steps"))
    if steps_a and steps_b and not steps_a & steps_b:
        proven.append("steps")
    inputs_a, numeric_a = _constraints(a, catalog)
    inputs_b, numeric_b = _constraints(b, catalog)
    arch_a, arch_b = _archetypes(a, inputs_a), _archetypes(b, inputs_b)
    shared = arch_b if arch_a is None else arch_a if arch_b is None else arch_a & arch_b
    if shared is not None and not shared:
        proven.append("opposing_archetypes")
    if (_when(a).get("stack") == "empty" and _needs_stack_top(b)) or (
        _when(b).get("stack") == "empty" and _needs_stack_top(a)
    ):
        proven.append("stack")
    if any(na == nb and _values_disjoint(x, y) for na, x in inputs_a for nb, y in inputs_b):
        proven.append("input")
    literals_b = _literals(b)
    if any((not holds, text) in literals_b for holds, text in _literals(a)):
        proven.append("true_false")
    if any(ka == kb and _ints_disjoint(x, y) for ka, x in numeric_a for kb, y in numeric_b):
        proven.append("numeric_range")
    return proven


def conflict_dimensions(a: dict[str, Any], b: dict[str, Any], catalog: dict[str, Any]) -> list[str]:
    """The dimensions two conflicting plays overlap on, for the warning."""
    dimensions = ["whose_turn", "steps", "opposing_archetypes", "stack"]
    inputs_a, numeric_a = _constraints(a, catalog)
    inputs_b, numeric_b = _constraints(b, catalog)
    if {name for name, _ in inputs_a} & {name for name, _ in inputs_b}:
        dimensions.append("input")
    if _literals(a) & _literals(b):
        dimensions.append("true_false")
    if {key for key, _ in numeric_a} & {key for key, _ in numeric_b}:
        dimensions.append("numeric_range")
    return dimensions


def _conflict_warnings(
    plays: list[tuple[str, str, dict[str, Any]]], catalog: dict[str, Any]
) -> list[PlaysWarning]:
    """play-conflict over enabled plays, next-conflict over each play's next list.

    An advice-only play (`_advice_only`) commits nothing at any decision
    point, so it is never one half of a play-conflict and never one of the
    entries compared for next-conflict, priority or `next` notwithstanding."""
    warnings = []
    active = [
        p
        for p in plays
        if not _advice_only(p[2]) and _is_int(p[2].get("priority")) and p[2]["priority"] > 0
    ]
    for index, (id_a, rel, doc_a) in enumerate(active):
        for id_b, _, doc_b in active[index + 1 :]:
            if exclusivity(doc_a, doc_b, catalog):
                continue
            dimensions = conflict_dimensions(doc_a, doc_b, catalog)
            warnings.append(
                PlaysWarning(
                    "play-conflict",
                    rel,
                    [id_a, id_b],
                    dimensions,
                    f"plays {id_a!r} and {id_b!r} could both apply at one decision point "
                    f"(overlapping on {', '.join(dimensions)}); extend conditions, for "
                    f"example with an input condition such as {BUILTIN_INPUT}, so that at "
                    "most one applies - priority alone does not make scripts deterministic",
                )
            )
    by_id = {pid: doc for pid, _, doc in plays}
    for pid, rel, doc in plays:
        nxt = doc.get("next")
        entries = (
            [n for n in nxt if isinstance(n, str) and n in by_id and not _advice_only(by_id[n])]
            if isinstance(nxt, list)
            else []
        )
        for index, first in enumerate(entries):
            for second in entries[index + 1 :]:
                if exclusivity(by_id[first], by_id[second], catalog):
                    continue
                dimensions = conflict_dimensions(by_id[first], by_id[second], catalog)
                warnings.append(
                    PlaysWarning(
                        "next-conflict",
                        rel,
                        [pid, first, second],
                        dimensions,
                        f"next entries {first!r} and {second!r} of play {pid!r} could both "
                        f"apply (overlapping on {', '.join(dimensions)}); extend their "
                        "conditions so that at most one applies",
                    )
                )
    return warnings


# -- the next graph ----------------------------------------------------------------


def _graph_errors(plays: dict[str, tuple[str, list[str]]]) -> list[PlaysError]:
    errors: list[PlaysError] = []
    edges: dict[str, list[str]] = {}
    for pid, (rel, nxt) in plays.items():
        edges[pid] = []
        for target in nxt:
            if target in plays:
                edges[pid].append(target)
            else:
                errors.append(
                    PlaysError("unknown-next", rel, f"next {target!r} is not a play in this deck")
                )

    def reaches(start: str, goal: str) -> bool:
        seen: set[str] = set()
        stack = list(edges[start])
        while stack:
            node = stack.pop()
            if node == goal:
                return True
            if node not in seen:
                seen.add(node)
                stack.extend(edges[node])
        return False

    cyclic = {pid for pid in plays if reaches(pid, pid)}
    for pid in plays:
        if pid in cyclic:
            errors.append(
                PlaysError(
                    "next-cycle", plays[pid][0], f"play {pid!r} can reach itself through next"
                )
            )

    depth: dict[str, int] = {}

    def longest(pid: str) -> int:
        if pid not in depth:
            depth[pid] = 1 + max((longest(t) for t in edges[pid] if t not in cyclic), default=0)
        return depth[pid]

    for pid in plays:
        if pid not in cyclic and longest(pid) > MAX_CHAIN:
            errors.append(
                PlaysError(
                    "chain-too-deep",
                    plays[pid][0],
                    f"the longest next chain from {pid!r} is {longest(pid)} plays, "
                    f"at most {MAX_CHAIN}",
                )
            )
    return errors


def _advice_chain_errors(docs: list[tuple[str, str, dict[str, Any]]]) -> list[PlaysError]:
    """F8: an advice-only play (`_advice_only`) commits nothing, so it can
    neither chain further nor be chained into - `advice-only-chained` in
    both directions. Needs the whole deck's docs, so it runs beside
    `_graph_errors` rather than inside the per-file loop."""
    errors: list[PlaysError] = []
    by_id = {pid: doc for pid, _, doc in docs}
    advice_ids = {pid for pid, doc in by_id.items() if _advice_only(doc)}
    for pid, rel, doc in docs:
        nxt = doc.get("next")
        targets = (
            [n for n in nxt if isinstance(n, str) and n in by_id] if isinstance(nxt, list) else []
        )
        for target in targets:
            if pid in advice_ids:
                errors.append(
                    PlaysError(
                        "advice-only-chained",
                        rel,
                        f"play {pid!r} is advice-only (no moves) but names {target!r} in "
                        "next; an advice-only play commits nothing and cannot chain",
                    )
                )
            elif target in advice_ids:
                errors.append(
                    PlaysError(
                        "advice-only-chained",
                        rel,
                        f"play {pid!r} names advice-only play {target!r} in next; an "
                        "advice-only play commits nothing and cannot be chained into",
                    )
                )
    return errors


# -- per deck, per root ----------------------------------------------------------------


def _play_files(deck: Path) -> list[Path]:
    return sorted(p for p in (deck / "plays").glob("*.json") if p.name != INPUTS_FILE)


def check_deck(
    deck: Path, root: Path, cards_path: Path, cards: CardIndex | None = None
) -> tuple[list[PlaysError], list[PlaysWarning], int]:
    """One deck's errors, conflict warnings, and play count."""
    declared, errors = load_inputs(deck, root)
    files = _play_files(deck)
    if not files:
        return errors, [], 0
    if cards is None:
        cards = CardIndex.load(cards_path)

    known_keys = newest_matchup_keys(deck)
    catalog = input_catalog(declared or [], known_keys)
    graph: dict[str, tuple[str, list[str]]] = {}
    docs: list[tuple[str, str, dict[str, Any]]] = []
    for path in files:
        rel = _rel(path, root)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(PlaysError("malformed-play", rel, f"not readable JSON: {exc}"))
            continue
        errors.extend(check_play(doc, path.stem, rel, cards))
        if not isinstance(doc, dict):
            continue
        errors.extend(_input_errors(doc, rel, catalog, declared_ok=declared is not None))
        errors.extend(_never_applicable(doc, rel, catalog))
        docs.append((path.stem, rel, doc))
        archetypes = doc.get("opposing_archetypes")
        archetypes = (
            [a for a in archetypes if isinstance(a, str)] if isinstance(archetypes, list) else []
        )
        for key in archetypes:
            if key != WILDCARD and key not in known_keys:
                errors.append(
                    PlaysError(
                        "unknown-archetype-key",
                        rel,
                        f"{key!r} is neither {WILDCARD!r} nor a matchup of this deck's "
                        "newest analysis run",
                    )
                )
        nxt = doc.get("next", [])
        graph[path.stem] = (
            rel,
            [n for n in nxt if isinstance(n, str)] if isinstance(nxt, list) else [],
        )

    errors.extend(_graph_errors(graph))
    errors.extend(_advice_chain_errors(docs))
    errored = {e.path for e in errors}
    clean = [entry for entry in docs if entry[1] not in errored]
    return errors, _conflict_warnings(clean, catalog), len(files)


def _deck_dirs(root: Path, decks: list[str] | None) -> list[Path]:
    all_decks = sorted(p for p in (root / "decks").glob("*") if p.is_dir())
    if not decks:
        return all_decks
    by_slug = {p.name: p for p in all_decks}
    missing = [slug for slug in decks if slug not in by_slug]
    if missing:
        raise FileNotFoundError(f"no deck named {', '.join(missing)} under {root / 'decks'}")
    return [by_slug[slug] for slug in decks]


def run_check(
    root: Path, decks: list[str] | None = None
) -> tuple[list[PlaysError], list[PlaysWarning], int, int]:
    """(errors, warnings, plays checked, decks checked) for a guides root."""
    root = Path(root)
    cards_path = root / "meta" / "card-data.json"
    errors: list[PlaysError] = []
    warnings: list[PlaysWarning] = []
    total = 0
    cards = None
    dirs = _deck_dirs(root, decks)
    for deck in dirs:
        if cards is None and _play_files(deck):
            cards = CardIndex.load(cards_path)
        found, warned, count = check_deck(deck, root, cards_path, cards)
        errors.extend(found)
        warnings.extend(warned)
        total += count
    return errors, warnings, total, len(dirs)


def check_report(
    root: Path, decks: list[str] | None = None
) -> tuple[list[PlaysError], list[PlaysWarning]]:
    """The errors that fail the gate and the conflict warnings that do not."""
    errors, warnings, _, _ = run_check(root, decks)
    return errors, warnings


def run_cli(root: Path, decks: list[str] | None, as_json: bool) -> int:
    """`mcp-seat plays check`: 0 when no errors (warnings allowed), else 1."""
    root = Path(root).resolve()
    try:
        errors, warnings, plays, deck_count = run_check(root, decks)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"plays check: {exc}", file=sys.stderr)
        return 1

    if as_json:
        report = {
            "plays": plays,
            "decks": deck_count,
            "errors": [e.to_dict() for e in errors],
            "warnings": [w.to_dict() for w in warnings],
        }
        print(json.dumps(report, indent=2))
        return 1 if errors else 0
    for item in (*errors, *warnings):
        print(f"{item.code} {item.path}: {item.message}", file=sys.stderr)
    where = f"across {plays} play(s) in {deck_count} deck(s)"
    if errors:
        print(f"{len(errors)} play error(s) {where}", file=sys.stderr)
    if warnings:
        print(
            f"{len(warnings)} play conflict warning(s) {where}; warnings do not fail the gate, "
            "resolve them by extending conditions",
            file=sys.stderr,
        )
    if not errors:
        print(f"OK: {plays} play(s) across {deck_count} deck(s) clear the plays gate")
    return 1 if errors else 0
