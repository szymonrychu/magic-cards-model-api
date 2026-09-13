"""Conditions: does a script apply, judged over the REDACTED view only?

`evaluate(script, view, viewer)` reads nothing but what
`visibility.redact_view` hands `viewer`, so a script cannot leak hidden
information by construction:

- `count` over the battlefield and the stack reads the controller by side;
  over graveyards and exile it reads the owner.
- A hand: side `me` is the viewer's own hand; side `opponent` is only the
  cards of that player's hand the viewer knows (`known_cards[viewer].hand`,
  the viewer's entry being the only one redaction keeps).
- A library or sideboard: side `me` is the viewer's own
  `library_contents`/`sideboard_contents` multiset (`include=contents`), or
  with `known_only` just the viewer's known records of it; side `opponent` is
  only the viewer's known records of that player's zone. Neither the multiset
  nor a known record's position is order, and nothing here reads one.
- `zone_size` reads the public `*_count`; `life`, `stack_top`,
  `land_drop_available` (the viewer's turn, a land play left) and
  `untapped_mana` (`untapped_mana` below) read the public board.
- `input` reads the caller's `inputs`. A missing required input makes the
  script unevaluable (`needs_input`); a missing optional one does not hold.
  Every supplied input is first checked against the catalog: a name it does
  not hold, or a value outside the input's enum, bool or int range, makes the
  script unevaluable (`invalid_input`) rather than silently not holding.

Side `opponent` is every player but the viewer; `any` is every player. A
count or size sums over them; `life` holds when any of them satisfies it.

A selector's `missing_from` reads its zones exactly as a `count` does
(`presence`), so it too sees only known information: over the opponent's
hand, library or sideboard only the cards the viewer knows are compared, and
a card the viewer does not know about is treated as absent - "missing" there
means "not known to be there". Over the viewer's own library or sideboard it
reads the contents multiset, which needs `include=contents`.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from ..seatkit import untapped_mana_sources
from .inputs import input_catalog, supplied_input_problem
from .selectors import Present, compare, matches

HIDDEN_CONTENTS = {"library": "library_contents", "sideboard": "sideboard_contents"}


class ContentsRequired(ValueError):
    """A condition reads the viewer's own library or sideboard contents, and
    the view was fetched without `include=contents` (see `needs_contents`)."""


def _as_dict(script: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(script, BaseModel):
        return script.model_dump(mode="json", exclude_none=True)
    return script


def _conditions(doc: dict[str, Any]):
    for polarity in ("true", "false"):
        for index, condition in enumerate(doc.get(polarity) or []):
            yield polarity, index, condition


def _missing_froms(node: Any):
    """Every `missing_from` object anywhere in a play, a move or a selector."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "missing_from" and isinstance(value, dict):
                yield value
            else:
                yield from _missing_froms(value)
    elif isinstance(node, list):
        for value in node:
            yield from _missing_froms(value)


def needs_contents(script: BaseModel | dict[str, Any]) -> bool:
    """Must the caller fetch the view with `include=contents` to evaluate
    this play (or build this move)? A count over the viewer's own library or
    sideboard, or a `missing_from` reading either on side `me`, needs it."""
    doc = _as_dict(script)
    return any(
        condition.get("kind") == "count"
        and condition.get("zone") in HIDDEN_CONTENTS
        and condition.get("side") in ("me", "any")
        and not condition.get("known_only")
        for _, _, condition in _conditions(doc)
    ) or any(
        missing.get("side") == "me" and set(missing.get("zones") or []) & set(HIDDEN_CONTENTS)
        for missing in _missing_froms(doc)
    )


def _names(view: dict[str, Any], viewer: str, side: str) -> list[str]:
    players = [player["username"] for player in view.get("players") or []]
    if side == "me":
        return [viewer]
    if side == "opponent":
        return [name for name in players if name != viewer]
    return players


def _known(view: dict[str, Any], viewer: str) -> dict[str, Any]:
    return ((view.get("known_cards") or {}).get(viewer)) or {}


def _owner(card: dict[str, Any]) -> str | None:
    return card.get("owner") or card.get("controller")


def _entries(
    view: dict[str, Any], viewer: str, zone: str, side: str, *, known_only: bool = False
) -> list[tuple[dict[str, Any], int]]:
    """(card, how many) for every card `viewer` can see on `side` in `zone`."""
    names = _names(view, viewer, side)
    others = [name for name in names if name != viewer]
    if zone in ("battlefield", "stack"):
        cards = [card for card in view.get(zone) or [] if card.get("controller") in names]
    elif zone == "exile":
        cards = [card for card in view.get("exile") or [] if _owner(card) in names]
    elif zone == "graveyard":
        graveyards = view.get("graveyards") or {}
        cards = [card for name in names for card in graveyards.get(name) or []]
    elif zone == "hand":
        cards = list((view.get("hands") or {}).get(viewer) or []) if viewer in names else []
        cards += [card for card in _known(view, viewer).get("hand") or [] if _owner(card) in others]
    else:
        return _hidden_zone_entries(view, viewer, zone, names, others, known_only)
    return [(card, 1) for card in cards]


def presence(view: dict[str, Any], viewer: str) -> Present:
    """The board a selector's `missing_from` compares against: the cards on
    a side in some zones, read as `count` reads them."""
    seen: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]] = {}

    def present(zones: Sequence[str], side: str) -> list[dict[str, Any]]:
        key = (tuple(zones), side)
        if key not in seen:
            seen[key] = [card for zone in zones for card, _ in _entries(view, viewer, zone, side)]
        return seen[key]

    return present


def _count(condition: dict[str, Any], view: dict[str, Any], viewer: str) -> int:
    select, present = condition.get("select") or {}, presence(view, viewer)
    entries = _entries(
        view,
        viewer,
        condition["zone"],
        condition["side"],
        known_only=bool(condition.get("known_only")),
    )
    return sum(count for card, count in entries if matches(select, card, present))


def _known_zone_cards(view: dict[str, Any], viewer: str, zone: str) -> list[dict[str, Any]]:
    records = _known(view, viewer).get(zone) or []
    if zone == "library":
        return [
            record["card"] for record in records if isinstance(record, dict) and "card" in record
        ]
    return list(records)


def _hidden_zone_entries(
    view: dict[str, Any],
    viewer: str,
    zone: str,
    names: list[str],
    others: list[str],
    known_only: bool,
) -> list[tuple[dict[str, Any], int]]:
    known = _known_zone_cards(view, viewer, zone)
    entries = [(card, 1) for card in known if _owner(card) in others]
    if viewer not in names:
        return entries
    if known_only:
        return entries + [(card, 1) for card in known if _owner(card) == viewer]
    contents = (view.get(HIDDEN_CONTENTS[zone]) or {}).get(viewer)
    if contents is None:
        raise ContentsRequired(
            f"a count over your own {zone} reads {HIDDEN_CONTENTS[zone]}; "
            "fetch the state with include=contents"
        )
    return entries + [(entry["card"], entry["count"]) for entry in contents]


def _zone_size(condition: dict[str, Any], view: dict[str, Any], viewer: str) -> int:
    names = _names(view, viewer, condition["side"])
    if condition["zone"] == "exile":
        return sum(1 for card in view.get("exile") or [] if _owner(card) in names)
    key = f"{condition['zone']}_count"
    return sum(
        player.get(key, 0) for player in view.get("players") or [] if player["username"] in names
    )


def _mana_options(view: dict[str, Any], viewer: str) -> list[list[tuple[int, int]]]:
    """Per untapped mana source the viewer controls, each mana ability's
    `(mana_amount, mana_cost_value)` that adds anything now and whose mana
    carries no spending restriction (`restriction` is None)."""
    by_id = {card["id"]: card for card in view.get("battlefield") or []}
    sources = []
    for card_id in untapped_mana_sources(view, viewer):
        options = [
            (ability["mana_amount"], ability.get("mana_cost_value") or 0)
            for ability in by_id[card_id]["abilities"]
            if ability.get("mana")
            and (ability.get("mana_amount") or 0) > 0
            and ability.get("restriction") is None
        ]
        if options:
            sources.append(options)
    return sources


def untapped_mana(view: dict[str, Any], viewer: str) -> int:
    """The most mana `viewer` can make from their untapped sources right now.

    Each source (`seatkit.untapped_mana_sources`) is activated at most once,
    for one of its mana abilities, adding that ability's `mana_amount` (the
    engine's board-resolved count, 0 when it cannot be activated now). An
    ability whose view carries a `restriction` (the engine's CR 106.6
    `spend_restriction` over its own effect text, read or unread) is never
    counted: the condition names no spell, so only mana spendable on anything
    is mana a play's move can surely spend - Eldrazi Temple counts its plain
    `{C}`, not its `{C}{C}`. A free ability (`mana_cost_value` 0) adds
    to the pool outright. A filter consumes `mana_cost_value` of the mana
    already made, so it is activated only when the pool holds its cost; filters
    are activated after every free ability, in ascending cost order. The answer
    is the largest pool over every choice of one ability, or none, per source.
    Filter cost colors are not judged.

    Only the choices that can change that answer are enumerated: a source's
    best free ability is never worse than none, nor than a filter netting no
    more than it, so a source without a better filter simply adds its best free
    amount, and only sources with one are tried both ways.
    """
    pool = 0
    choices: list[list[tuple[int, int]]] = []
    for options in _mana_options(view, viewer):
        free = max((amount for amount, cost in options if cost == 0), default=0)
        filters = [(amount, cost) for amount, cost in options if cost and amount - cost > free]
        if filters:
            choices.append([(free, 0), *filters])
        else:
            pool += free
    best = pool
    for picked in itertools.product(*choices):
        made = pool + sum(amount for amount, cost in picked if cost == 0)
        for amount, cost in sorted((p for p in picked if p[1]), key=lambda p: p[1]):
            if made < cost:
                break
            made += amount - cost
        else:
            best = max(best, made)
    return best


def _player(view: dict[str, Any], name: str) -> dict[str, Any]:
    return next((p for p in view.get("players") or [] if p["username"] == name), {})


def _input_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    return a == b


def _input_holds(op: str, expected: Any, actual: Any) -> bool:
    if op == "eq":
        return _input_equal(actual, expected)
    if op == "ne":
        return not _input_equal(actual, expected)
    if op in ("in", "not_in"):
        found = isinstance(expected, list) and any(_input_equal(actual, v) for v in expected)
        return found if op == "in" else not found
    if isinstance(expected, bool) or not isinstance(expected, int):
        return False
    return compare(actual, op, expected)


def _check(
    condition: dict[str, Any],
    view: dict[str, Any],
    viewer: str,
    inputs: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
) -> tuple[bool, Any, str | None]:
    """(holds, seen, needs_input) for one condition."""
    kind = condition["kind"]
    if kind == "count":
        seen = _count(condition, view, viewer)
        return compare(seen, condition["op"], condition["n"]), seen, None
    if kind == "zone_size":
        seen = _zone_size(condition, view, viewer)
        return compare(seen, condition["op"], condition["n"]), seen, None
    if kind == "life":
        lives = {
            name: _player(view, name).get("life")
            for name in _names(view, viewer, condition["side"])
        }
        holds = any(compare(life, condition["op"], condition["n"]) for life in lives.values())
        seen = next(iter(lives.values())) if len(lives) == 1 else lives
        return holds, seen, None
    if kind == "stack_top":
        stack = view.get("stack") or []
        if not stack:
            return False, None, None
        top = stack[0]
        controller = top.get("controller")
        mine = controller == viewer
        whose = mine if condition["controller"] == "me" else (controller is not None and not mine)
        seen = {"name": top.get("name"), "controller": controller, "kind": top.get("kind")}
        select = condition.get("select") or {}
        return whose and matches(select, top, presence(view, viewer)), seen, None
    if kind == "land_drop_available":
        me = _player(view, viewer)
        available = (
            view.get("active_player") == viewer
            and isinstance(me.get("lands_played"), int)
            and isinstance(me.get("land_allowance"), int)
            and me["lands_played"] < me["land_allowance"]
        )
        return available is condition["value"], available, None
    if kind == "untapped_mana":
        seen = untapped_mana(view, viewer)
        return compare(seen, condition["op"], condition["n"]), seen, None
    if kind == "input":
        name = condition["name"]
        if name not in inputs:
            spec = catalog.get(name) or {}
            return False, None, name if spec.get("required") is True else None
        actual = inputs[name]
        return _input_holds(condition["op"], condition["value"], actual), actual, None
    raise ValueError(f"condition kind {kind!r} is not in the closed vocabulary")


def _result(
    *,
    polarity: str | None = None,
    index: int | None = None,
    seen: Any = None,
    needs_input: str | None = None,
    invalid_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "applies": polarity is None and invalid_input is None,
        "failing_index": index,
        "failing_list": polarity,
        "seen": seen,
        "needs_input": needs_input,
        "invalid_input": invalid_input,
    }


def evaluate(
    script: BaseModel | dict[str, Any],
    view: dict[str, Any],
    viewer: str,
    inputs: dict[str, Any] | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Does `script` apply for `viewer` on this redacted `view`?

    `applies` is true when every `true` condition holds and no `false` one
    does. Otherwise `failing_list`/`failing_index` name the first condition
    that stopped it, `true` before `false`, with what it `seen`. `needs_input`
    names a required input `inputs` lacks; `catalog` (`inputs.input_catalog`)
    says which inputs exist and which are required, and defaults to the
    built-in alone. `invalid_input` is `{name, value, reason}` for the first
    supplied input the catalog does not hold or whose value does not fit it;
    it is checked before any condition, and nothing is judged past it.
    Raises `ContentsRequired` when a condition needs `include=contents`.
    """
    doc = _as_dict(script)
    supplied = inputs or {}
    known = catalog if catalog is not None else input_catalog([], ())
    for name, value in supplied.items():
        reason = supplied_input_problem(name, value, known)
        if reason is not None:
            return _result(invalid_input={"name": name, "value": value, "reason": reason})
    for polarity, index, condition in _conditions(doc):
        holds, seen, needs_input = _check(condition, view, viewer, supplied, known)
        if needs_input is not None:
            return _result(polarity=polarity, index=index, needs_input=needs_input)
        if holds is not (polarity == "true"):
            return _result(polarity=polarity, index=index, seen=seen)
    return _result()
