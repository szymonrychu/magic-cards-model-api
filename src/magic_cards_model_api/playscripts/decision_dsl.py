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
script-format-design.md`, "Not in the vocabulary": mulligan, concede, assign
combat damage, and any iteration field - FB2's B4 moved attack, block and
pass INTO the vocabulary, as `declare_attackers`/`declare_blockers`/`pass`
moves). This module reaches `mulligan` (`mulligan_declared`), `concede`
(`app.action_concede`'s own ledger action, `"concede"` - CR 104.3a lets a
player concede at any time, and the vocabulary has no move for it) off the
rows it is asked to convert, plus a third of this module's own, `pregame`: a
CR 103.6 `opening_hand_action` row carries no `turn`/`step` at all
(`opening.py`'s own `audit.record` call - pregame has no turn yet), so no
`when` block can be built without inventing one out of nothing, which
`never a fabricated move` forbids exactly as much as inventing the move
itself would. These three never become expressible, however much board data
a row carries.

`attack` (`declare_attackers`'s own ledger action, `"attack"`), `block`
(`declare_blockers`) and `pass`/`pass_until` (`pass_priority` - the same
ledger action for both; `pass_until` only *threads one reason across several
of them*, see `seatkit.pass_until`, so there is nothing here for it to
convert differently) are real moves now: `pass` needs no board data at all
(`{"kind": "pass"}`), an empty `attackers`/`blocks` list needs none either
(`declare_attackers`/`declare_blockers` with an empty list is itself a legal
"no attackers"/"no blocks" declaration), and a non-empty one is written with
card-name-free selectors the same way `cast`/`play_land`/`activate` already
are - grouped by shared defender (attackers) or shared blocked attacker
(blockers) into one `AttackGroup`/`Block` apiece. Without a `state_view` that
resolves every card an entry names, or with more distinct groups than the
schema's own `MAX_ATTACK_GROUPS`/`MAX_BLOCKS`, the row falls back to the
plain `"attack"`/`"block"` label - never a half-built move standing in for
one it could not finish honestly.

A `decide` row can also miss the vocabulary at a *finer* grain, one
`DecisionKind` at a time - `order_triggers` has no answer shape in the
schema at all (its ordering is not one `Answer` field can carry), so it stays
`unexpressible: "decide:order_triggers"` unconditionally. `scry`/`surveil`
(`to_bottom`/`to_graveyard` + `top_order`), `put_on_top_or_bottom`
(`position`), `choose_cards` look-and-keep (`pick`) and `choose_targets`
(`targets`, a card or a player) are built from board data the same way the
combat moves are, and fall back to `"decide:<kind>"` only when a card the
row names cannot be resolved in the attached `state_view` (`put_on_top_or_bottom`
needs no board data at all - `position` is written straight off the row's own
`top` flag). A `decide` row whose own kind is one of the two structurally
forbidden ones (`schema.FORBIDDEN_DECISION_KINDS`: `assign_combat_damage`,
`mulligan`) reuses the plain `mulligan`/`decide:assign_combat_damage` label
instead, so `unexpressible` never disagrees with itself about what a
`mulligan` decision is, wherever in the ledger it shows up.

**Off-priority answers.** A `decide` row answered while this seat did not
hold priority (`state_view["priority_holder"] != seat` - a decision raised
mid-resolution, not at an ordinary priority window `when.steps` alone could
predict) is written as a *standalone* pending-decision answer: its `true`
carries one `pending_decision` condition naming the same `DecisionKind` the
answer itself names (`schema.PendingDecisionCondition`,
`Play._standalone_pending_answer_names_the_pending_decision`), so the play
only ever runs as a direct answer to a decision already on the table, not on
`when` timing that happened to match this one historical row. A row with no
`state_view` at all is read as having held priority (nothing here to say
otherwise), the same "least specific but honest" default every other board
read in this module takes.

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
    MAX_ABILITY_INDEX,
    MAX_ATTACK_GROUPS,
    MAX_BLOCKS,
    MAX_SELECTOR_WORDS,
    MAX_TARGETS,
    MAX_TOP_ORDER,
    STEPS,
    TARGET_ZONES,
)
from .selectors import parse_type_line

#: A defender/target token that named a card, but one this module could not
#: resolve in the attached `state_view` - distinct from `None`, which for a
#: `Defender` token means "the opponent player, so the field is simply left
#: out" (see `_defender_for_token`).
_UNRESOLVED = object()

#: The upper bound `compact_board` stays at or under, in characters of its
#: `json.dumps(..., sort_keys=True)` form - a review prompt's own budget
#: line, not a byte count (contrast `report.TABLE_LIMIT_BYTES`).
COMPACT_BOARD_CHARS = 600

#: The ledger action recorded for `pass`/`pass_until` alike - see the module
#: docstring.
_PASS_ACTION = "pass_priority"

#: `unexpressible` labels for the six vocabulary gaps this module reaches
#: directly off a row's own action (`"pregame"` is this module's own name for
#: the CR 103.6 gap; the other five are the spec's own words).
_ATTACK = "attack"
_BLOCK = "block"
_MULLIGAN = "mulligan"
_CONCEDE = "concede"
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
    "concede": "concede",
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
    card in at all, plus this seat's own `known_cards` record of a hidden
    card it has looked at (CR 701.20e - a scry/surveil/look-and-keep
    decision's own candidates live only there, never on the public board).
    `None` when no view was given or nothing in it matches; this is the only
    place a view's own cards are read, so every selector this module writes
    traces back to here."""
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
    known_cards = state_view.get("known_cards")
    if isinstance(known_cards, Mapping):
        for known in known_cards.values():
            if not isinstance(known, Mapping):
                continue
            for zone_name in ("hand", "sideboard"):
                pools.append((zone_name, known.get(zone_name) or ()))
            library = [
                entry.get("card")
                for entry in known.get("library") or ()
                if isinstance(entry, Mapping) and isinstance(entry.get("card"), Mapping)
            ]
            pools.append(("library", library))
    for zone, cards in pools:
        for card in cards:
            if not isinstance(card, Mapping):
                continue
            if wanted_id is not None and str(card.get("id")) == wanted_id:
                return dict(card), zone
            if wanted_id is None and wanted_name is not None and card.get("name") == wanted_name:
                return dict(card), zone
    return None


def _selector_for_cards(cards: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A `Selector` matching every one of `cards` by characteristic alone -
    type-line words (`selectors.parse_type_line`, already casefolded and
    never a card's own `name`) plus keywords - never a name, which is what
    keeps this inside the card-name ban every selector in this schema is
    written under.

    `types_all` is the *intersection* of every card's own types: the
    selector's AND semantics still admit each card, since each one carries
    at least those shared types. `supertypes_any`/`subtypes_any`/
    `keywords_any` are each the *union* of every card's own words: the
    selector's OR-within-the-field semantics are then guaranteed to admit
    each card by at least one of its own words. One card is the trivial case
    of this - an intersection or union of one set is that set - so this is
    also `_card_selector`'s own reading."""
    type_sets: list[set[str]] = []
    supertypes: set[str] = set()
    subtypes: set[str] = set()
    keywords: set[str] = set()
    for card in cards:
        card_supertypes, card_types, card_subtypes = parse_type_line(card.get("type_line"))
        type_sets.append(card_types)
        supertypes |= card_supertypes
        subtypes |= card_subtypes
        keywords |= {str(k) for k in (card.get("keywords") or ()) if str(k).strip()}
    common_types = set.intersection(*type_sets) if type_sets else set()
    selector: dict[str, Any] = {}
    if common_types:
        selector["types_all"] = sorted(common_types)[:MAX_SELECTOR_WORDS]
    if supertypes:
        selector["supertypes_any"] = sorted(supertypes)[:MAX_SELECTOR_WORDS]
    if subtypes:
        selector["subtypes_any"] = sorted(subtypes)[:MAX_SELECTOR_WORDS]
    if keywords:
        selector["keywords_any"] = sorted(keywords)[:MAX_SELECTOR_WORDS]
    return selector


def _card_selector(card: Mapping[str, Any]) -> dict[str, Any]:
    """A `Selector` for `card` alone - `_selector_for_cards`'s one-card
    case."""
    return _selector_for_cards([card])


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
    """An `ActivateMove` for this row, or `None` when there is no honest one
    to write. With no board data at all there is no `source` to select and
    no ability to name either way - `unexpressible`, never a bare empty
    selector the way `cast`/`play_land` fall back to (a `source` naming
    nothing is not "whichever permanent", it is no permanent). With the
    card found, its ability named by `ability_index` (FB2 M4) when that
    ability's own text is blank or missing - nothing in the schema's own
    `ability_text_has` can name an ability with no text to match a phrase
    against - and by `ability_text_has` (the real oracle text, a phrase)
    otherwise."""
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
    move: dict[str, Any] = {
        "kind": "activate",
        "source": {
            "zone": zone if zone in COUNT_ZONES else "battlefield",
            "select": _card_selector(card),
        },
    }
    if isinstance(text, str) and text.strip():
        move["ability_text_has"] = text
    elif index <= MAX_ABILITY_INDEX:
        move["ability_index"] = index
    else:
        return None
    return move


# -- declare_attackers / declare_blockers: FB2's B4 combat moves ----------------


def _defender_for_token(token: str, state_view: Mapping[str, Any] | None) -> Any:
    """The `Defender` FB2's `AttackGroup` names for `token` (a player's
    username or a card id - `decisions.declared_pairs_answer`'s own token
    shape for a `defender`): `None` when it names the opponent player (the
    schema's own default for a left-out `defender`, so nothing is written at
    all), a `DefenderCard` selector when it resolves to one of their
    permanents, or the module's own `_UNRESOLVED` sentinel when no board
    data can ground either reading honestly."""
    if not isinstance(state_view, Mapping):
        return _UNRESOLVED
    players = {
        p.get("username") for p in (state_view.get("players") or ()) if isinstance(p, Mapping)
    }
    if token in players:
        return None
    found = _find_card(state_view, token, None)
    if found is None:
        return _UNRESOLVED
    card, _zone = found
    return {"select": _card_selector(card), "side": "opponent"}


def _declare_attackers(
    detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """A `DeclareAttackersMove` for this row's own `attackers` (CR 508.1),
    grouped by shared defender into one `AttackGroup` apiece - "an attack of
    two creatures at the player" is one group, `count: 2`, not two. `[]`
    needs no board data at all (still a legal "no attackers" declaration);
    a non-empty list needs every attacker and every card-shaped defender
    resolved in `state_view`, or this is `None` (the caller's `_ATTACK`
    fallback)."""
    attackers = detail.get("attackers")
    if not isinstance(attackers, list):
        return None
    if not attackers:
        return {"kind": "declare_attackers", "attackers": []}
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for entry in attackers:
        if not isinstance(entry, Mapping):
            return None
        found = _find_card(state_view, entry.get("card"), None)
        if found is None:
            return None
        card, _zone = found
        key = str(entry.get("defender"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(card)
    if len(order) > MAX_ATTACK_GROUPS:
        return None
    attack_groups: list[dict[str, Any]] = []
    for key in order:
        cards = groups[key]
        group: dict[str, Any] = {"select": _selector_for_cards(cards), "count": len(cards)}
        defender = _defender_for_token(key, state_view)
        if defender is _UNRESOLVED:
            return None
        if defender is not None:
            group["defender"] = defender
        attack_groups.append(group)
    return {"kind": "declare_attackers", "attackers": attack_groups}


def _declare_blockers(
    detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """A `DeclareBlockersMove` for this row's own `blocks` (CR 509.1),
    grouped by shared attacker into one `Block` apiece (a double block is one
    `Block`, `blocker.count: 2`). `[]` needs no board data at all; a
    non-empty list needs every blocker and every attacker resolved in
    `state_view`, or this is `None` (the caller's `_BLOCK` fallback)."""
    blocks = detail.get("blocks")
    if not isinstance(blocks, list):
        return None
    if not blocks:
        return {"kind": "declare_blockers", "blocks": []}
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for entry in blocks:
        if not isinstance(entry, Mapping):
            return None
        found = _find_card(state_view, entry.get("blocker"), None)
        if found is None:
            return None
        blocker_card, _zone = found
        key = str(entry.get("blocked"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(blocker_card)
    if len(order) > MAX_BLOCKS:
        return None
    result: list[dict[str, Any]] = []
    for key in order:
        attacker_found = _find_card(state_view, key, None)
        if attacker_found is None:
            return None
        attacker_card, _zone = attacker_found
        blocker_cards = groups[key]
        result.append(
            {
                "blocker": {
                    "select": _selector_for_cards(blocker_cards),
                    "count": len(blocker_cards),
                },
                "attacker": {"select": _card_selector(attacker_card)},
            }
        )
    return {"kind": "declare_blockers", "blocks": result}


# -- decide/answer ---------------------------------------------------------------


def _target_for_token(
    token: str, seat: str, state_view: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """One `Target` for a `choose_targets` token (a player's username or a
    card id - `decisions.targets_answer`'s own token shape): a
    `PlayerTarget` when the token names a player of the game, a `CardTarget`
    selector when it resolves to a card in `state_view` (`zone` set only
    when the card's own zone is one `CardTarget.zone` can name - FB2 T1;
    left out, a target still resolves over battlefield and stack), or `None`
    when neither reading can be grounded."""
    if not isinstance(state_view, Mapping):
        return None
    players = {
        p.get("username") for p in (state_view.get("players") or ()) if isinstance(p, Mapping)
    }
    if token in players:
        return {"player": "me"} if token == seat else {"player": "opponent"}
    found = _find_card(state_view, token, None)
    if found is None:
        return None
    card, zone = found
    target: dict[str, Any] = {
        "select": _card_selector(card),
        "side": "me" if card.get("controller") == seat else "opponent",
    }
    if zone in TARGET_ZONES:
        target["zone"] = zone
    return target


def _scry_or_surveil_answer(
    kind: str, detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """`to_bottom` (scry, CR 701.22a) or `to_graveyard` (surveil, CR 701.25a)
    plus `top_order` (FB2 A1) for a `scry`/`surveil` row's own `moved`/
    `top_order` ids. An empty `moved` with no `top_order` is the bare "keep
    everything on top, unchanged" answer, needing no board data at all - the
    same reading the module already gave it. A non-empty one needs every
    named card resolved in `state_view`, and `top_order` at or under
    `MAX_TOP_ORDER` tiers (one card's own selector per tier, in the order
    the row itself gives - the finest-grained honest reading of a fixed
    permutation), or this is `(None, "decide:<kind>")`."""
    moved = detail.get("moved")
    top_order = detail.get("top_order")
    if not isinstance(moved, list):
        return None, f"decide:{kind}"
    if not moved and not top_order:
        return {"decision": kind}, None
    answer: dict[str, Any] = {"decision": kind}
    if moved:
        moved_found = [_find_card(state_view, card_id, None) for card_id in moved]
        if any(found is None for found in moved_found):
            return None, f"decide:{kind}"
        field = "to_bottom" if kind == "scry" else "to_graveyard"
        answer[field] = _selector_for_cards([found[0] for found in moved_found])
    if top_order:
        if len(top_order) > MAX_TOP_ORDER:
            return None, f"decide:{kind}"
        order_found = [_find_card(state_view, card_id, None) for card_id in top_order]
        if any(found is None for found in order_found):
            return None, f"decide:{kind}"
        answer["top_order"] = [_card_selector(found[0]) for found in order_found]
    return answer, None


def _choose_cards_answer(
    detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """A `pick` answer (a "look and keep" `choose_cards` row's own
    `card_ids`, e.g. a fetch's search, CR 401.2): every named card resolved
    in `state_view` and merged into one selector, `count` the number chosen
    - or `(None, "decide:choose_cards")` when the ids are empty or any one
    of them cannot be resolved."""
    card_ids = detail.get("card_ids")
    if not isinstance(card_ids, list) or not card_ids:
        return None, "decide:choose_cards"
    found = [_find_card(state_view, card_id, None) for card_id in card_ids]
    if any(entry is None for entry in found):
        return None, "decide:choose_cards"
    cards = [entry[0] for entry in found if entry is not None]
    return {
        "decision": "choose_cards",
        "pick": {"select": _selector_for_cards(cards), "count": len(cards)},
    }, None


def _choose_targets_answer(
    detail: Mapping[str, Any], seat: str, state_view: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """A `targets` answer (FB2 A2) for a `choose_targets` row's own
    `targets` tokens - card or player, `_target_for_token` each - or
    `(None, "decide:choose_targets")` when the tokens are empty, exceed
    `MAX_TARGETS`, or any one of them cannot be resolved."""
    tokens = detail.get("targets")
    if not isinstance(tokens, list) or not tokens or len(tokens) > MAX_TARGETS:
        return None, "decide:choose_targets"
    targets: list[dict[str, Any]] = []
    for token in tokens:
        target = _target_for_token(str(token), seat, state_view)
        if target is None:
            return None, "decide:choose_targets"
        targets.append(target)
    return {"decision": "choose_targets", "targets": targets}, None


def _answer_for_decide(
    decide_kind: Any, detail: Mapping[str, Any], seat: str, state_view: Mapping[str, Any] | None
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
    if kind == "put_on_top_or_bottom":
        top = detail.get("top")
        if not isinstance(top, bool):
            return None, f"decide:{kind}"
        return {"decision": kind, "position": "top" if top else "bottom"}, None
    if kind in ("surveil", "scry"):
        return _scry_or_surveil_answer(kind, detail, state_view)
    if kind == "choose_cards":
        return _choose_cards_answer(detail, state_view)
    if kind == "choose_targets":
        return _choose_targets_answer(detail, seat, state_view)
    if kind == "choose_players":
        usernames = detail.get("usernames")
        if not isinstance(usernames, list):
            return None, f"decide:{kind}"
        players = sorted({"me" if name == seat else "opponent" for name in usernames})
        if not players:
            return None, f"decide:{kind}"
        return {"decision": kind, "players": players}, None
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
    if kind == "mulligan":
        return None, _MULLIGAN
    if kind == "concede":
        return None, _CONCEDE
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
    if kind == "pass":
        return _play("decision-pass", when, [{"kind": "pass"}]), None
    if kind == "declare_attackers":
        move = _declare_attackers(detail, state_view)
        if move is None:
            return None, _ATTACK
        return _play("decision-declare-attackers", when, [move]), None
    if kind == "declare_blockers":
        move = _declare_blockers(detail, state_view)
        if move is None:
            return None, _BLOCK
        return _play("decision-declare-blockers", when, [move]), None
    if kind == "decide":
        answer, unexpressible = _answer_for_decide(detail.get("kind"), detail, seat, state_view)
        if answer is None:
            return None, unexpressible
        move = {"kind": "answer", "answers": [answer]}
        play = _play("decision-decide", when, [move])
        # See the module docstring's "Off-priority answers": a decision
        # answered without this seat holding priority is a standalone
        # pending-decision answer, not a `when`-timed one.
        if isinstance(state_view, Mapping) and state_view.get("priority_holder") != seat:
            play["true"] = [{"kind": "pending_decision", "decision": str(detail.get("kind"))}]
        return play, None
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
    3. the fullest player's (most battlefield permanents) own battlefield
       list, alphabetically-last name first, one permanent at a time, until
       that player's own list is empty - then the next-fullest, and so on
       until every player's battlefield list is empty;
    4. the stack's own names, bottom of stack (oldest, least likely to be
       what this decision is about) first, one at a time, until it fits or
       the stack is empty too.

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
    # Stage 3: trim battlefield lists, fullest player first, one permanent
    # at a time, until every player's battlefield list is empty. Stage 4:
    # once there is nothing left to trim there, the stack's own names,
    # oldest (bottom of stack) first - see the docstring's own numbered
    # order. `board["stack"]` alone can push a board over the cap (a long
    # stack with an empty battlefield), so this loop cannot stop at an empty
    # battlefield the way it used to.
    while _size(board) > COMPACT_BOARD_CHARS:
        fullest = max(
            board["players"].values(),
            key=lambda entry: len(entry["battlefield"]),
            default=None,
        )
        if fullest is not None and fullest["battlefield"]:
            fullest["battlefield"].pop()
            continue
        if board["stack"]:
            board["stack"].pop(0)
            continue
        break
    return _sorted(board)
