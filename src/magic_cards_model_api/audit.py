"""The game-action audit log: one append-only `records` row per game per
action, plus a console line.

Every mutating endpoint records what was attempted and what came of it - allowed
or refused, with the engine's own reason verbatim - so two agents playing through
the API leave a complete, replayable record behind.

**T10**: this module used to hold the log itself, in a module-global `_logs`
dict (`dict[game_id, list[entry]]`). It now holds none of the storage - every
entry is a row of the repository's own `records` table
(`repository.py`/`repository_postgres.py`), reached through `store.
append_record`/`store.fill_record`/`store.records`, so a postgres-backed
deployment gets a durable, cross-process audit trail instead of one that
resets on every restart and is invisible to a second replica. Every existing
caller (`app.py`, `opening.py`, `lobby.py`, `decks_api.py`, `ai_seat.py`,
`serve.py`) keeps calling `record`/`entries` exactly as before - this module
is still the whole interface, just no longer the storage.

This module must stay importable without the FastAPI wiring (tests read the log
directly), so the session store is imported lazily inside `record`/`entries`.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from magic_cards_model.machines import PHASE_OF

if TYPE_CHECKING:  # keep this module importable without the app's own wiring
    from .records import LegalActionsBefore

#: The audited action a seat's `report_problem` MCP call turns into. Named
#: here because this module treats it specially - see `record`.
PROBLEM_ACTION = "report_problem"

#: A seat's own pregame move refused because something had already made it for
#: that seat (`opening.PregameOverruled`). Treated like a problem report: it
#: means the game took a decision away from a player, and a line at INFO among
#: a hundred routine ones is not how that should surface.
OVERRULE_ACTION = "pregame_overruled"

#: A seat's declaration, before it acts, of what it reads on the board, what
#: its own role in the matchup is, what it plans to do this turn, and what it
#: expects the opponent to do and why (`POST /api/games/{id}/plan`,
#: `schemas.DeclarePlanIn`). One per player-turn - `records.require_plan`
#: refuses an agent seat's first mutating action of its own turn until this
#: row exists for that turn, which is what stops the facility being one
#: nobody uses.
PLAN_ACTION = "plan_declared"

#: The game ended (CR 104.1: "A game ends immediately when a player wins,
#: when the game is a draw, or when the game is restarted"). Written once,
#: the moment `Game.is_over` first reads true after a committed command -
#: concede included. Without it a game ending to lethal damage left nothing
#: in the ledger saying who won, and a reader had to infer the result from
#: the final snapshot's life totals.
GAME_OVER_ACTION = "game_over"

#: A seat's own post-game write-up (`POST /api/games/{id}/reflection`),
#: filed once the result is already known - `mtg_agent.driver.app`'s
#: `turn_done` posts the driver's reflection turn here the moment it
#: completes. Named `"game_reflection"` to match `ui/review_model.js`'s
#: `REFLECTION_ACTIONS`, the shape the post-game review's `reflectionOf`
#: already scans the ledger for - it is matched there ahead of this action
#: ever being written, so the review section appears the day this constant
#: starts reaching the ledger rather than needing the page changed again.
REFLECTION_ACTION = "game_reflection"

#: The actions that get a banner at WARNING rather than one compact line.
LOUD_ACTIONS = (PROBLEM_ACTION, OVERRULE_ACTION, PLAN_ACTION)

#: The heading each loud action's banner carries, preposition included: one
#: is something the seat did, the next something done to it, the last
#: something the seat said it was about to do.
_LOUD_HEADINGS = {
    PROBLEM_ACTION: "PROBLEM REPORTED by",
    OVERRULE_ACTION: "PREGAME DECISION OVERRULED for",
    PLAN_ACTION: "PLAN DECLARED by",
}

logger = logging.getLogger("mcm.audit")
if not logger.handlers:  # import-guarded: no duplicate handlers on re-import
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def record(
    game_id: str,
    action: str,
    *,
    reasoning: str | None = None,
    legal_actions_before: LegalActionsBefore | None = None,
    _fill_seq: int | None = None,
    **detail: Any,
) -> int:
    """Append one entry for `action` on `game_id` (or, when `_fill_seq` is
    given, fill in the payload of the `records` row `store.replace` already
    wrote at that `seq`), then print one console line.

    The entry carries the game's turn number, step, phase, and stack size at the
    moment of recording, plus a full-information snapshot: board, both hands,
    stack with targets, graveyards, exile, life, pools. A game that is no longer
    in the store (or was never in it) still gets its entry, with those fields
    None.

    **T10's two additions**, both optional and both `None` for the many
    callers that have neither: `reasoning` is the acting seat's own stated
    reason for this action - required by `app.py`'s mutating routes for an
    agent seat before this is ever called (`records.require_reasoning`), so
    by the time it reaches here the gate has already passed. `legal_actions_
    before` is `records.legal_actions_before`'s `LegalActionsBefore` -
    the enumeration *and* the one seat it belongs to, computed by the caller
    *before* the action ran and never recomputed here, since by the time
    `record` runs the game this reads (`store.get`) may already be the
    *post*-action state. It lands as two payload keys, `legal_actions_
    before` (the actions) and `legal_actions_for` (the seat), which is what
    lets `visibility.redact_log_entry` decide who may read them without
    consulting anything the caller put in `detail`.

    `_fill_seq`: `app.py`'s `_run_command`/`_resume` call `store.replace`
    before the engine's outcome (allowed/refused, the reason) is known to
    the caller building this entry's `detail` - `replace` already wrote a
    `records` row for the commit (T9's same-transaction guarantee), with an
    empty payload. Passing that row's own `seq` back in here fills it with
    the real payload instead of leaving it a placeholder and inserting a
    second, orphaned row - see `repository.py`'s `fill_record`.

    Returns the entry's own `seq` - a free, already-monotonic idempotency
    marker: a caller who is unsure whether a call that timed out actually
    landed can compare the `log_seq` an action response carries (`app.py`'s
    `audit_outcome` and the `resolve-top`/`advance`/`pass-priority`
    endpoints) against a later read, rather than guessing from a blind retry.
    """
    from .repository import GameUnloadable  # lazy, with `store` below
    from .serialize import snapshot  # lazy: keep module import light
    from .store import store  # lazy: audit imports nothing of the app at load

    # A game whose snapshot this build cannot decode still gets its entry -
    # every board-derived field simply comes out `None`, exactly as it
    # already does for a game that is gone. Raising here would take out the
    # one call `lobby.reset_game` makes to write down that a stale game was
    # ended, which is the entry an operator reading the ledger back most
    # needs to find.
    try:
        game = store.get(game_id)
    except GameUnloadable:
        game = None
    turn = game.turn if game is not None else None
    step = turn.step if turn is not None else None
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "action": action,
        "detail": dict(detail),
        "turn": turn.turn_number if turn is not None else None,
        "step": step.value if step is not None else None,
        # `.get`, not `[...]`: CR 500.1 makes a phase - and so every step in
        # one - part of *a turn*, and the engine's two pregame steps
        # (`Step.MULLIGAN`, `Step.PREGAME_ACTIONS`) happen before CR 103.8's
        # first turn, so they are in no phase at all and are deliberately
        # absent from `PHASE_OF`. Subscripting raised `KeyError` for every
        # entry recorded while a freshly booted game rested in one, which
        # surfaced as a 500 from `GET /api/games/{id}/log` and a 404 from
        # `POST /api/ui/games`.
        "phase": phase.value if step is not None and (phase := PHASE_OF.get(step)) else None,
        "stack_size": len(game.stack) if game is not None else None,
        "snapshot": snapshot(game) if game is not None else None,
        "reasoning": reasoning,
        # Two keys, written from one object or not at all: the actions
        # and the single seat they belong to. `visibility.redact_log_
        # entry` reads `legal_actions_for` to decide who may see them -
        # never `detail["player"]`, which is whatever the request body
        # said. See `records.LegalActionsBefore`.
        "legal_actions_before": (
            legal_actions_before.actions if legal_actions_before is not None else None
        ),
        "legal_actions_for": (
            legal_actions_before.player if legal_actions_before is not None else None
        ),
    }
    if _fill_seq is not None:
        store.fill_record(game_id, _fill_seq, payload)
        seq = _fill_seq
    else:
        seq = store.append_record(game_id, payload)
    entry = {**payload, "seq": seq}
    if action in LOUD_ACTIONS:
        # A seat filing an engine bug is the single most valuable thing in this
        # log, and it used to be indistinguishable from the hundred routine
        # lines around it: the compact line below prints only card/target, so
        # the report's *text* - the whole point of the call - never reached the
        # console at all. It gets its own banner and WARNING level so it stands
        # out in `docker compose logs`.
        logger.warning(_loud_block(entry))
    else:
        logger.info(_console_line(entry))
    return seq


#: The shape every entry `record` writes - defaulted here so a row whose
#: payload never got past `store.replace`'s empty placeholder (a caller that
#: crashed between `replace` and its own `fill_record`, or wrote through the
#: repository directly rather than through this module) still reads back as
#: a well-formed entry instead of a bare `{"seq": ...}` that crashes the
#: first `entry["action"]` any reader does - `GET /api/games/{id}/log` among
#: them. `_run_command`/`_resume`/`pregame_settle` always fill every row
#: they create (see `repository.py`'s `replace` docstring), so this is a
#: defensive fallback for an unrecorded row, never the normal case.
_UNRECORDED_DEFAULTS: dict[str, Any] = {
    "ts": None,
    "action": "(unrecorded)",
    "detail": {},
    "turn": None,
    "step": None,
    "phase": None,
    "stack_size": None,
    "snapshot": None,
    "reasoning": None,
    "legal_actions_before": None,
    "legal_actions_for": None,
}


def entries(game_id: str) -> list[dict[str, Any]]:
    """Every entry recorded for this game, oldest first."""
    from .store import store  # lazy: audit imports nothing of the app at load

    entries_: list[dict[str, Any]] = []
    for row in store.records(game_id):
        entry = {**_UNRECORDED_DEFAULTS, **row["payload"], "seq": row["seq"]}
        entries_.append(entry)
    return entries_


def _loud_block(entry: dict[str, Any]) -> str:
    """The console form of a `LOUD_ACTIONS` entry: a banner, the seat and
    position, and the entry's own `text`, one indented line per line of it.

    Deliberately louder and taller than every other entry. One of these is the
    agent telling us the engine is wrong, live; the other is the server
    telling us it took a decision away from a seat. A person watching the
    container log should not have to be looking for either to notice."""
    detail = entry["detail"]
    turn = entry["turn"] if entry["turn"] is not None else "?"
    step = entry["step"] or "?"
    who = detail.get("player") or "?"
    text = str(detail.get("text") or _plan_text(detail) or "").strip() or "(no text)"
    heading = _LOUD_HEADINGS.get(entry["action"], f"{entry['action'].upper()} by")
    lines = [
        "=" * 72,
        f"{heading} {who} - #{entry['seq']} [turn {turn} / {step}]",
    ]
    lines += [f"  {line}" for line in text.splitlines()]
    lines.append("=" * 72)
    return "\n".join(lines)


def _plan_text(detail: dict[str, Any]) -> str | None:
    """A `plan_declared` entry rendered for the console banner - `None` for
    any other entry, so `_loud_block`'s ordinary `text` path is untouched.

    A declaration has no single free-text field the way a problem report
    does; four fields is what makes it evidence rather than a slogan, and an
    operator watching `docker compose logs` should see all four rather than
    whichever one was picked to stand for the rest.
    """
    if not any(key in detail for key in ("board_read", "my_role", "plan", "prediction")):
        return None
    prediction = detail.get("prediction") or {}
    lines = [
        f"board: {detail.get('board_read') or '?'}",
        f"role : {detail.get('my_role') or '?'}",
        f"plan : {detail.get('plan') or '?'}",
        f"expects: {prediction.get('expecting') or '?'}"
        f" ({prediction.get('confidence') or 'unstated'} confidence)",
        f"because: {prediction.get('because') or '?'}",
    ]
    return "\n".join(lines)


def _console_line(entry: dict[str, Any]) -> str:
    """One compact line, e.g. `#12 [turn 2 / precombat_main] alice cast Bears
    -> allowed | board 3 hands A7/B6 stack 1`."""
    detail = entry["detail"]
    turn = entry["turn"] if entry["turn"] is not None else "?"
    step = entry["step"] or "?"
    head = f"#{entry['seq']} [turn {turn} / {step}]"

    subject = detail.get("card") or detail.get("target") or ""
    if "from" in detail and "to" in detail:
        subject = f"{detail['from']} -> {detail['to']}"
    middle = f"{entry['action']} {subject}".strip()

    who = detail.get("player")
    line = f"{head} {who} {middle}" if who else f"{head} {middle}"
    if "resolved" in detail:
        # "changed nothing" is the signature of an effect the engine has no
        # implementation for, so it is called out rather than left to be
        # inferred from two consecutive snapshots.
        did = "changed the game" if detail.get("changed") else "CHANGED NOTHING"
        line += f" | resolved {detail['resolved']}: {did}"
    if "allowed" in detail:
        allowed = detail["allowed"]
        if allowed is True:
            line += " -> allowed"
        elif allowed is False:
            line += f" -> refused: {detail.get('reason')}"
        else:
            # `allowed` is tri-state (True/False/None) - `None` means the
            # command paused waiting on a decision, which is not a refusal
            # and must never be folded into that arm (`detail.get("reason")`
            # is `None` for a pause, since nothing was refused). `paused_on`
            # is the pending decision's own `{player, kind}` - whose choice
            # this is and what it is about - threaded in by every PAUSED
            # caller precisely so this line can say what it paused *on*
            # rather than just that it paused.
            paused_on = detail.get("paused_on") or {}
            who_decides = paused_on.get("player") or "?"
            kind = paused_on.get("kind") or "?"
            line += f" -> paused: waiting on {who_decides}'s {kind} decision"

    if detail.get("note"):
        # A free-form note for events that are not card actions - a seat being
        # handed to the driver, say - so the console line says something more
        # than the action's bare name.
        line += f" | {detail['note']}"

    snap = entry.get("snapshot")
    if snap:
        hands = snap.get("hands", {})
        hand_counts = "/".join(f"{name[:1].upper()}{len(cards)}" for name, cards in hands.items())
        line += (
            f" | board {len(snap.get('battlefield', []))}"
            f" hands {hand_counts}"
            f" stack {len(snap.get('stack', []))}"
        )
    return line
