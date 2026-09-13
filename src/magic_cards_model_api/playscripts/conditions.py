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
  `untapped_mana` (`seatkit.untapped_mana_sources`) read the public board.
- `input` reads the caller's `inputs`. A missing required input makes the
  script unevaluable (`needs_input`); a missing optional one does not hold.
  Every supplied input is first checked against the catalog: a name it does
  not hold, or a value outside the input's enum, bool or int range, makes the
  script unevaluable (`invalid_input`) rather than silently not holding.

Side `opponent` is every player but the viewer; `any` is every player. A
count or size sums over them; `life` holds when any of them satisfies it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..seatkit import untapped_mana_sources
from .inputs import input_catalog, supplied_input_problem
from .selectors import compare, matches

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


def needs_contents(script: BaseModel | dict[str, Any]) -> bool:
    """Must the caller fetch the view with `include=contents` to evaluate this?"""
    return any(
        condition.get("kind") == "count"
        and condition.get("zone") in HIDDEN_CONTENTS
        and condition.get("side") in ("me", "any")
        and not condition.get("known_only")
        for _, _, condition in _conditions(_as_dict(script))
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


def _count(condition: dict[str, Any], view: dict[str, Any], viewer: str) -> int:
    zone, select = condition["zone"], condition.get("select") or {}
    names = _names(view, viewer, condition["side"])
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
        return _count_hidden_zone(condition, view, viewer, names, others)
    return sum(1 for card in cards if matches(select, card))


def _known_zone_cards(view: dict[str, Any], viewer: str, zone: str) -> list[dict[str, Any]]:
    records = _known(view, viewer).get(zone) or []
    if zone == "library":
        return [
            record["card"] for record in records if isinstance(record, dict) and "card" in record
        ]
    return list(records)


def _count_hidden_zone(
    condition: dict[str, Any],
    view: dict[str, Any],
    viewer: str,
    names: list[str],
    others: list[str],
) -> int:
    zone, select = condition["zone"], condition.get("select") or {}
    known = _known_zone_cards(view, viewer, zone)
    total = sum(1 for card in known if _owner(card) in others and matches(select, card))
    if viewer not in names:
        return total
    if condition.get("known_only"):
        return total + sum(1 for card in known if _owner(card) == viewer and matches(select, card))
    contents = (view.get(HIDDEN_CONTENTS[zone]) or {}).get(viewer)
    if contents is None:
        raise ContentsRequired(
            f"a count over your own {zone} reads {HIDDEN_CONTENTS[zone]}; "
            "fetch the state with include=contents"
        )
    return total + sum(entry["count"] for entry in contents if matches(select, entry["card"]))


def _zone_size(condition: dict[str, Any], view: dict[str, Any], viewer: str) -> int:
    names = _names(view, viewer, condition["side"])
    if condition["zone"] == "exile":
        return sum(1 for card in view.get("exile") or [] if _owner(card) in names)
    key = f"{condition['zone']}_count"
    return sum(
        player.get(key, 0) for player in view.get("players") or [] if player["username"] in names
    )


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
        return whose and matches(condition.get("select") or {}, top), seen, None
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
        seen = len(untapped_mana_sources(view, viewer))
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
