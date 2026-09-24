"""DR1: converting one seat's own ledger rows into the play-script DSL.

`decision_rows(entries, seat)` turns every *committing* ledger row of `seat`
- a cast, an activation, a land play, an answer to a pending decision, the
two combat declarations, a mulligan, a CR 103.6 opening-hand action, a
priority pass (`pass`/`pass_until` both post the same `pass_priority` action
- see below), or a script run it started - into one record:

    {seq, turn, step, action, dsl, reasoning, board, unexpressible}

`dsl` is that decision written as a schema-valid `Play` (`schema.play_errors`
sees no error in it) whenever the play-script vocabulary can say it at all;
`unexpressible` names why not when it cannot, and `dsl` is `None` then -
never a fabricated move standing in for one the DSL cannot make. The two
fields are always both present and always exactly one of them is `None`,
except `run_script` (see its own section below), which is a fixed shape of
its own rather than a `Play`.

**Row shape.** One entry is `audit.entries()`'s own shape: `seq`, `action`,
`detail` (the route's own per-action fields, keyed by `player`), and
`turn`/`step`/`active_player`/`reasoning` **stamped by `audit.record` itself,
outside `detail`** - T10 gives every row these four at the position it was
recorded, regardless of what the calling route passed as keyword arguments,
so this module reads them there rather than from `detail` (where an older
reader, or a test fixture built by hand, might also have put a copy - a
`detail["reasoning"]`/`detail["turn"]` is read as a fallback when the
top-level key is absent, never preferred over it). A row not shaped like a
recognized committing action, or not this seat's own **allowed** action
(`detail["allowed"] is True`, `records.require_reasoning`'s own commitment
test - a refusal committed nothing and is skipped, same as an offer, a
reflection, or any other non-action row), contributes no record at all.

**Board.** `compact_board` (below) turns one `GameStateView`-shaped dict -
what `GET /review/frames/{seq}` hands back for a row that has a recorded
state - into the compact board this module's own record carries and, where
one is available, uses to write a real, card-name-free `Selector` for the
move. This module never fetches that view itself (no store access, no
routes: DR2 wires it) - a row that wants one attaches it under its own
`state_view` key before calling `decision_rows`, and a row without one
degrades to the least specific *honest* selector rather than guessing: an
empty `{}` (matches "whichever card", not a name) for `cast`/`play_land`,
and `unexpressible` for `activate`, whose `ability_text_has` field is
required and has no honest empty value - see `_cast_or_play_land`/
`_activate`.

**Not in the vocabulary** (spec `docs/superpowers/specs/2026-09-13-play-
script-format-design.md`, "Not in the vocabulary": attack, block, mulligan,
concede, pass, and any iteration field). This module reaches four of those
five off the rows it is asked to convert - `attack` (the `declare_attackers`
tool's own ledger action, `"attack"`), `block` (`declare_blockers`),
`mulligan`, `pass` (`pass_priority` - `pass` and `pass_until` are the same
ledger action; `pass_until` only *threads one reason across several of
them*, see `seatkit.pass_until`, so there is nothing here for it to convert
differently) - plus a fifth of this module's own, `pregame`: a CR 103.6
`opening_hand_action` row carries no `turn`/`step` at all (`opening.py`'s own
`audit.record` call - pregame has no turn yet), so no `when` block can be
built without inventing one out of nothing, which `never a fabricated move`
forbids exactly as much as inventing the move itself would.

A `decide` row can also miss the vocabulary at a *finer* grain than the five
above, one `DecisionKind` at a time - a real answer schema `Answer` has no
field for (`order_triggers`'s ordering, `put_on_top_or_bottom`'s specific
card with no id in the ledger row to look up at all) or one this module
declines to fabricate without board data to ground it in
(`choose_cards`/`choose_targets`'s own `pick`, a non-empty `surveil`/`scry`).
Those get `unexpressible: "decide:<kind>"` - prefixed, unlike the five above,
because they are the DSL's answer-shape vocabulary falling short of one
`DecisionKind`, not the closed five the spec itself names. A `decide` row
whose own kind *is* one of the five forbidden kinds (`assign_combat_damage`
joins them here, structurally: `schema.FORBIDDEN_DECISION_KINDS`) reuses that
plain label instead, so `unexpressible` never disagrees with itself about
what a `mulligan`/`attack`/`block` decision is, wherever in the ledger it
shows up.

**`run_script`.** Running an existing play is not itself one of the DSL's
four move kinds (`activate`/`cast`/`play_land`/`answer` - `MOVE_KINDS`); it
invokes one that is already written. Its `dsl` is therefore not a `Play` but
a small fixed dict, `{"kind": "run_script", "play_id": ..., "offer_outcome":
...}` - `offer_outcome` is `"taken"` when a `script_run_offered` row for the
same `run_id` appears earlier in `entries`, the same "this offer was
followed" reading `report._offer_outcomes` gives it, and `"unoffered"`
otherwise (the seat, an operator, or arming started it without ever being
offered it).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..audit import SCRIPT_RUN_OFFERED_ACTION, SCRIPT_RUN_STARTED_ACTION
from .schema import (
    AMOUNT_DECISION_KINDS,
    CHOICE_DECISION_KINDS,
    COUNT_ZONES,
    FORBIDDEN_DECISION_KINDS,
    MAX_SELECTOR_WORDS,
    STEPS,
)
from .selectors import parse_type_line

#: The upper bound `compact_board` stays at or under, in characters of its
#: `json.dumps(..., sort_keys=True)` form - a review prompt's own budget
#: line, not a byte count (contrast `report.TABLE_LIMIT_BYTES`).
COMPACT_BOARD_CHARS = 600

#: The ledger action recorded for `pass`/`pass_until` alike - see the module
#: docstring.
_PASS_ACTION = "pass_priority"

#: `unexpressible` labels for the five vocabulary gaps this module reaches
#: directly off a row's own action (`"pregame"` is this module's own name for
#: the CR 103.6 gap; the other four are the spec's own words).
_ATTACK = "attack"
_BLOCK = "block"
_MULLIGAN = "mulligan"
_PASS = "pass"
_PREGAME = "pregame"

#: The ledger action -> this module's own canonical action-kind label, for
#: every committing row `decision_rows` recognizes at all. A raw action not
#: in this table contributes no record.
_ACTION_KIND: dict[str, str] = {
    "cast": "cast",
    "activate": "activate",
    "play_land": "play_land",
    "decide": "decide",
    "attack": "declare_attackers",
    "declare_blockers": "declare_blockers",
    "mulligan_declared": "mulligan",
    "opening_hand_action": "opening_hand_action",
    _PASS_ACTION: "pass",
    SCRIPT_RUN_STARTED_ACTION: "run_script",
}

#: `decide` kinds a real answer schema can write with no card selector at
#: all - see the module docstring's "not in the vocabulary" section.
_ACCEPT_KINDS = frozenset({"may", "pay_life", "pay_energy", "pay_mana"})
_AMOUNT_FIELD = {"pay_energy_amount": "energy_amount", "pay_life_amount": "life_amount"}


def _payload(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    """`entry` itself, or its `payload` when it is a raw `records` row -
    the same duck-typed read `report._payload` uses, so a caller that hands
    either shape through is read the same way."""
    payload = entry.get("payload")
    return payload if isinstance(payload, Mapping) else entry


def _top(entry: Mapping[str, Any], detail: Mapping[str, Any], key: str) -> Any:
    """A field `audit.record` stamps at the top of the row, read there
    first and off `detail` only as a fallback - see the module docstring."""
    value = entry.get(key)
    return value if value is not None else detail.get(key)


def decision_rows(entries: Iterable[Mapping[str, Any]], seat: str) -> list[dict[str, Any]]:
    """Every committing row of `seat` in `entries`, converted - see the
    module docstring for the record shape and what `unexpressible` means."""
    rows = list(entries)
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(rows):
        payload = _payload(entry)
        raw_action = payload.get("action")
        kind = _ACTION_KIND.get(str(raw_action))
        if kind is None:
            continue
        detail = payload.get("detail") or {}
        if detail.get("player") != seat and detail.get("seat") != seat:
            continue
        if kind != "run_script" and detail.get("allowed") is not True:
            continue
        seq = payload.get("seq", entry.get("seq"))
        turn = _top(payload, detail, "turn")
        step = _top(payload, detail, "step")
        active_player = _top(payload, detail, "active_player")
        reasoning = _top(payload, detail, "reasoning")
        state_view = entry.get("state_view")
        board = compact_board(state_view) if isinstance(state_view, Mapping) else None

        if kind == "run_script":
            dsl = _run_script_dsl(rows, index, detail)
            unexpressible = None
        else:
            dsl, unexpressible = _dsl_for(
                kind,
                detail,
                seat=seat,
                turn=turn,
                step=step,
                active_player=active_player,
                state_view=state_view if isinstance(state_view, Mapping) else None,
            )

        out.append(
            {
                "seq": seq,
                "turn": turn,
                "step": step,
                "action": kind,
                "dsl": dsl,
                "reasoning": reasoning,
                "board": board,
                "unexpressible": unexpressible,
            }
        )
    return out


# -- run_script -----------------------------------------------------------------


def _run_script_dsl(
    rows: Sequence[Mapping[str, Any]], started_index: int, detail: Mapping[str, Any]
) -> dict[str, Any]:
    run_id = detail.get("run_id")
    play_id = str(detail.get("script_id"))
    taken = False
    for entry in rows[:started_index]:
        payload = _payload(entry)
        if payload.get("action") != SCRIPT_RUN_OFFERED_ACTION:
            continue
        offer_detail = payload.get("detail") or {}
        if offer_detail.get("run_id") == run_id:
            taken = True
            break
    return {
        "kind": "run_script",
        "play_id": play_id,
        "offer_outcome": "taken" if taken else "unoffered",
    }


# -- the play skeleton every expressible row shares -----------------------------


def _when(seat: str, active_player: Any, step: Any) -> dict[str, Any] | None:
    if step not in STEPS or active_player is None:
        return None
    whose_turn = "mine" if active_player == seat else "theirs"
    return {"whose_turn": whose_turn, "steps": [step], "stack": "any"}


def _play(play_id: str, when: dict[str, Any], moves: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": play_id,
        "name": play_id,
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": when,
        "true": [],
        "false": [],
        "moves": moves,
        "next": [],
    }


# -- cast / play_land / activate: card-name-free selectors ----------------------


def _find_card(
    state_view: Mapping[str, Any] | None, card_id: Any, name: Any
) -> tuple[dict[str, Any], str] | None:
    """The card `card_id` (or, failing that, `name`) names, and the zone it
    was found in - searched across every zone one redacted view can show a
    card in at all. `None` when no view was given or nothing in it matches;
    this is the only place a view's own cards are read, so every selector
    this module writes traces back to here."""
    if not isinstance(state_view, Mapping):
        return None
    wanted_id = str(card_id) if card_id is not None else None
    wanted_name = str(name) if isinstance(name, str) else None
    pools: list[tuple[str, Iterable[Any]]] = [
        ("battlefield", state_view.get("battlefield") or ()),
        ("stack", state_view.get("stack") or ()),
        ("exile", state_view.get("exile") or ()),
    ]
    hands = state_view.get("hands")
    if isinstance(hands, Mapping):
        for cards in hands.values():
            pools.append(("hand", cards or ()))
    graveyards = state_view.get("graveyards")
    if isinstance(graveyards, Mapping):
        for cards in graveyards.values():
            pools.append(("graveyard", cards or ()))
    for zone, cards in pools:
        for card in cards:
            if not isinstance(card, Mapping):
                continue
            if wanted_id is not None and str(card.get("id")) == wanted_id:
                return dict(card), zone
            if wanted_id is None and wanted_name is not None and card.get("name") == wanted_name:
                return dict(card), zone
    return None


def _card_selector(card: Mapping[str, Any]) -> dict[str, Any]:
    """A `Selector` for `card` by characteristic alone - type-line words
    (`selectors.parse_type_line`, already casefolded and never the card's
    own `name`) plus its keywords - never a name, which is what keeps this
    inside the card-name ban every selector in this schema is written
    under."""
    supertypes, types, subtypes = parse_type_line(card.get("type_line"))
    selector: dict[str, Any] = {}
    if types:
        selector["types_all"] = sorted(types)[:MAX_SELECTOR_WORDS]
    if supertypes:
        selector["supertypes_any"] = sorted(supertypes)[:MAX_SELECTOR_WORDS]
    if subtypes:
        selector["subtypes_any"] = sorted(subtypes)[:MAX_SELECTOR_WORDS]
    keywords = sorted({str(k) for k in (card.get("keywords") or ()) if str(k).strip()})
    if keywords:
        selector["keywords_any"] = keywords[:MAX_SELECTOR_WORDS]
    return selector


def _cast_or_play_land(
    kind: str, detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> dict[str, Any]:
    found = _find_card(state_view, detail.get("card_id"), detail.get("card"))
    select = _card_selector(found[0]) if found is not None else {}
    if kind == "cast":
        zone = found[1] if found is not None else str(detail.get("from_zone") or "hand")
        if zone not in COUNT_ZONES:
            zone = "hand"
        move = {"kind": "cast", "card": {"zone": zone, "select": select}, "mana": "auto"}
    else:
        move = {"kind": "play_land", "card": {"select": select}}
    return move


def _activate(
    detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    found = _find_card(state_view, detail.get("card_id"), detail.get("card"))
    if found is None:
        return None
    card, zone = found
    abilities = card.get("abilities") or []
    index = detail.get("ability_index")
    if not isinstance(index, int) or not (0 <= index < len(abilities)):
        return None
    ability = abilities[index]
    text = ability.get("text") if isinstance(ability, Mapping) else None
    if not isinstance(text, str) or not text.strip():
        return None
    return {
        "kind": "activate",
        "source": {
            "zone": zone if zone in COUNT_ZONES else "battlefield",
            "select": _card_selector(card),
        },
        "ability_text_has": text,
    }


# -- decide/answer ---------------------------------------------------------------


def _answer_for_decide(
    decide_kind: Any, detail: Mapping[str, Any], seat: str
) -> tuple[dict[str, Any] | None, str | None]:
    """One `Answer` for a `decide` row's own `detail["kind"]`, or (`None`,
    the `unexpressible` label) - see the module docstring's finer-grained
    "not in the vocabulary" section for what each label means."""
    kind = str(decide_kind)
    if kind in FORBIDDEN_DECISION_KINDS:
        label = {
            "declare_attackers": _ATTACK,
            "declare_blockers": _BLOCK,
            "mulligan": _MULLIGAN,
        }.get(kind, kind)
        return None, label
    if kind in _ACCEPT_KINDS:
        return {"decision": kind, "accept": bool(detail.get("accept"))}, None
    if kind in AMOUNT_DECISION_KINDS:
        field = _AMOUNT_FIELD.get(kind)
        amount = detail.get(field) if field else None
        if not isinstance(amount, int) or isinstance(amount, bool):
            return None, f"decide:{kind}"
        return {"decision": kind, "amount": amount}, None
    if kind in CHOICE_DECISION_KINDS:
        choice = detail.get("choice")
        if not isinstance(choice, str) or not choice.strip():
            return None, f"decide:{kind}"
        return {"decision": kind, "choice": choice}, None
    if kind == "choose_pile":
        pile_one = detail.get("pile_one")
        if not isinstance(pile_one, bool):
            return None, f"decide:{kind}"
        return {"decision": kind, "pile_one": pile_one}, None
    if kind == "choose_players":
        usernames = detail.get("usernames")
        if not isinstance(usernames, list):
            return None, f"decide:{kind}"
        players = sorted({"me" if name == seat else "opponent" for name in usernames})
        if not players:
            return None, f"decide:{kind}"
        return {"decision": kind, "players": players}, None
    if kind in ("surveil", "scry"):
        moved = detail.get("moved")
        if moved == []:
            # A bare answer means "keep everything on top" - schema-valid
            # and exactly what an empty `moved` recorded (runtime.py's own
            # reading of a bare `surveil`/`scry` answer).
            return {"decision": kind}, None
        return None, f"decide:{kind}"
    return None, f"decide:{kind}"


# -- dispatch --------------------------------------------------------------------


def _dsl_for(
    kind: str,
    detail: Mapping[str, Any],
    *,
    seat: str,
    turn: Any,
    step: Any,
    active_player: Any,
    state_view: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    if kind == "declare_attackers":
        return None, _ATTACK
    if kind == "declare_blockers":
        return None, _BLOCK
    if kind == "mulligan":
        return None, _MULLIGAN
    if kind == "pass":
        return None, _PASS
    if kind == "opening_hand_action":
        return None, _PREGAME

    when = _when(seat, active_player, step)
    if when is None:
        return None, "step"

    if kind in ("cast", "play_land"):
        move = _cast_or_play_land(kind, detail, state_view)
        return _play(f"decision-{kind}", when, [move]), None
    if kind == "activate":
        move = _activate(detail, state_view)
        if move is None:
            return None, "activate:no-ability-text"
        return _play("decision-activate", when, [move]), None
    if kind == "decide":
        answer, unexpressible = _answer_for_decide(detail.get("kind"), detail, seat)
        if answer is None:
            return None, unexpressible
        move = {"kind": "answer", "answers": [answer]}
        return _play("decision-decide", when, [move]), None
    return None, kind


# -- compact_board ----------------------------------------------------------------

#: CR 205.4a's own supertypes, plus `snow`/`ongoing` - the words
#: `selectors.SUPERTYPES` already treats as supertypes rather than types.
_LAND_WORD = "land"


def _players_by_name(view: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(player.get("username")): player
        for player in view.get("players") or ()
        if isinstance(player, Mapping)
    }


def _permanent(card: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": card.get("name") or card.get("card_name"),
        "tapped": bool(card.get("tapped")),
    }
    power, toughness = card.get("power"), card.get("toughness")
    if power is not None or toughness is not None:
        out["pt"] = (
            f"{power if power is not None else '*'}/{toughness if toughness is not None else '*'}"
        )
    counters = {k: v for k, v in (card.get("counters") or {}).items() if v}
    if counters:
        out["counters"] = counters
    return out


def _build_board(
    view: Mapping[str, Any], *, with_graveyard: bool, with_counters: bool
) -> dict[str, Any]:
    battlefield = [card for card in view.get("battlefield") or () if isinstance(card, Mapping)]
    exile = [card for card in view.get("exile") or () if isinstance(card, Mapping)]
    players: dict[str, Any] = {}
    for name, player in _players_by_name(view).items():
        own = [card for card in battlefield if card.get("controller") == name]
        lands = [card for card in own if _LAND_WORD in parse_type_line(card.get("type_line"))[1]]
        entry: dict[str, Any] = {
            "life": player.get("life"),
            "hand": player.get("hand_count"),
            "library": player.get("library_count"),
            "lands": {
                "untapped": sum(1 for card in lands if not card.get("tapped")),
                "total": len(lands),
            },
            "battlefield": [
                _permanent(card)
                if with_counters
                else {"name": card.get("name"), "tapped": bool(card.get("tapped"))}
                for card in sorted(own, key=lambda card: str(card.get("name")))
            ],
        }
        if with_graveyard:
            entry["graveyard"] = player.get("graveyard_count")
            entry["exile"] = sum(1 for card in exile if card.get("controller") == name)
        players[name] = entry
    return {
        "turn": view.get("turn_number"),
        "step": view.get("step"),
        "active_player": view.get("active_player"),
        "players": players,
        "stack": [
            card.get("name") for card in (view.get("stack") or ()) if isinstance(card, Mapping)
        ],
    }


def _size(obj: Any) -> int:
    import json

    return len(json.dumps(obj, sort_keys=True))


def _sorted(obj: Any) -> Any:
    """`obj`, recursively rebuilt with every dict's keys in sorted order -
    so the dict `compact_board` returns is already what
    `json.dumps(..., sort_keys=True)` would print, with no reader needing to
    ask for that explicitly."""
    if isinstance(obj, Mapping):
        return {key: _sorted(obj[key]) for key in sorted(obj, key=str)}
    if isinstance(obj, list):
        return [_sorted(item) for item in obj]
    return obj


def compact_board(state_view: Mapping[str, Any]) -> dict[str, Any]:
    """A `GameStateView`-shaped dict (`serialize.state_view`'s own shape, as
    `GET /review/frames/{seq}` hands it back), reduced to the compact board
    one decision record carries: per player, life, hand size, library size,
    graveyard/exile counts, lands untapped/total, and its own battlefield
    permanents as `{name, tapped, pt, counters}`; the stack's own card names,
    turn/step/active player shared at the top. Deterministic (same input,
    same output) and its keys are written in a fixed, sorted order at every
    level, so two calls on the same view - and `json.dumps(...,
    sort_keys=True)` of the result - agree byte for byte.

    Stays at or under `COMPACT_BOARD_CHARS` once serialized, cutting in this
    order until it does:

    1. every player's `graveyard`/`exile` counts, dropped whole;
    2. every battlefield permanent's `pt`/`counters`, down to bare
       `{name, tapped}`;
    3. the lowest-controlled-count player's battlefield permanents, most
       repeated-name-suffix first, one at a time, until the board fits or
       every list is empty.

    Life and the lands untapped/total summary are never cut at any stage -
    a board that has lost everything else still says how much life and how
    much mana each side has, which is the one thing a reviewer must never
    be left guessing at."""
    stages = [
        {"with_graveyard": True, "with_counters": True},
        {"with_graveyard": False, "with_counters": True},
        {"with_graveyard": False, "with_counters": False},
    ]
    board = None
    for stage in stages:
        board = _build_board(state_view, **stage)
        if _size(board) <= COMPACT_BOARD_CHARS:
            return _sorted(board)
    assert board is not None
    # Stage 3: trim battlefield lists, largest first, one permanent at a
    # time, until the board fits or every list is empty.
    while _size(board) > COMPACT_BOARD_CHARS:
        fullest = max(
            board["players"].values(),
            key=lambda entry: len(entry["battlefield"]),
            default=None,
        )
        if fullest is None or not fullest["battlefield"]:
            break
        fullest["battlefield"].pop()
    return _sorted(board)
