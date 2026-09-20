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

#: The post-game AI review (`POST /api/games/{id}/ai-review`), filed once
#: per game by whichever seat's driver generates it first
#: (`mtg_agent.driver.review`) - a separate, Opus-generated critique built
#: from the seat's own reflection (the "handoff") plus the game's own
#: review record (`review.build_review`'s declarations/advice), never a
#: seat's own account of what it did (that is `REFLECTION_ACTION`). Gated
#: post-game exactly like a reflection (`app.declare_ai_review` mirrors
#: `app.declare_reflection`'s gate). Not deduplicated server-side: in an
#: AIvAI game both seats' drivers could in principle reach `turn_done`'s
#: reflection branch and each generate one - deliberately accepted (see
#: `mtg_agent.driver.review`'s own docstring), the same way multiple
#: `REFLECTION_ACTION` rows already are, and for the same reason: the UI
#: reads the last one, and a second review is not a defect, only a rare
#: extra API call.
AI_REVIEW_ACTION = "ai_review"

#: An advisor's proposal: one action it says its human should take, validated
#: against the engine where the request shape allowed it, recorded but never
#: performed (`POST /api/games/{id}/advice`, `schemas.AdviceIn`). The row
#: carries the suggested request (`method`/`path`/`body`), the server-derived
#: `summary` of it (card names resolved from ids, targets, cost), the agent's
#: own `reasoning` for proposing it, `based_on_seq` (the ledger position the
#: proposal was made against), and `validated` (whether the shape could be
#: probed with `dry_run` before ever reaching the human - `False` for a
#: request shape with no `dry_run` support, per
#: `game_access.DRY_RUN_ROUTES`).
#:
#: The advisor credential is read-only by construction (`game_access.py`), so
#: this row is the *only* way a buddy pod reaches the game at all - and,
#: because the api is multi-replica and `AiSeatRegistry`/`K8sSeats._seats`
#: are process-local caches, the ledger is the only place a proposal can live
#: and still be visible to the replica the human's next poll happens to hit.
ADVICE_ACTION = "advice"

#: The human saying yes to one `ADVICE_ACTION` row, by its `seq`
#: (`POST /api/games/{id}/advice/{seq}/approve`). `detail` carries
#: `advice_seq` - the approved row's own `seq`, which is how `review.py` pairs
#: the two (by seq, never by comparing bodies: `mana_sources` and similar
#: fields legitimately differ between what was proposed and what the engine
#: actually taps). Recorded *before* the action itself is sent, so the
#: retrospective can pair a proposal with the move it actually produced
#: rather than guessing from adjacency. Like `ADVICE_ACTION`, mutates no game
#: state - the approval is a marker; the real attempt that follows, if any, is
#: its own ordinary action row with its own `allowed`.
ADVICE_APPROVED_ACTION = "advice_approved"

#: A seat's play-script runner starting one script (`POST .../script-runs`
#: with `phase: start`). Written so a second start of the same script at the
#: same turn and step can be refused from the ledger alone - durable across
#: replicas, like every other fact `records.require_plan` reads back.
SCRIPT_RUN_STARTED_ACTION = "script_run_started"

#: One finished script run (`phase: finish`): which script, against which
#: guides revision, over which ledger span, how far it got and why it
#: stopped. Mutates no `Game` - the moves themselves went through the action
#: routes and are rows of their own.
SCRIPT_RUN_ACTION = "script_run"

#: Both script-run rows. A run names the seat's own cards, plans and guides,
#: so `visibility.redact_log_entry` drops these rows outright for every
#: viewer but that seat, its advisor and the admin key.
SCRIPT_RUN_ACTIONS = (SCRIPT_RUN_STARTED_ACTION, SCRIPT_RUN_ACTION)

#: The actions that get a banner at WARNING rather than one compact line.
LOUD_ACTIONS = (PROBLEM_ACTION, OVERRULE_ACTION, PLAN_ACTION, ADVICE_ACTION)

#: The heading each loud action's banner carries, preposition included: one
#: is something the seat did, the next something done to it, the third
#: something the seat said it was about to do, the last something somebody
#: else is suggesting it do.
_LOUD_HEADINGS = {
    PROBLEM_ACTION: "PROBLEM REPORTED by",
    OVERRULE_ACTION: "PREGAME DECISION OVERRULED for",
    PLAN_ACTION: "PLAN DECLARED by",
    ADVICE_ACTION: "ADVICE PROPOSED for",
}

logger = logging.getLogger("mcm.audit")
if not logger.handlers:  # import-guarded: no duplicate handlers on re-import
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def _with_stale(detail: dict[str, Any], named_a_current_board: bool) -> dict[str, Any]:
    """`detail` with `stale: False` added when this row is a refusal that the
    engine actually ruled on, against a board the caller had named and that
    was current.

    Three conditions, each carrying its own weight:

    - **`allowed is False`.** Only a refusal has a race to be mistaken for.
      An allowed action and a pause (`allowed: None`, a question the engine
      asked part way through) are not refusals at all, and
      `scripts/eval_report.py` never reads `stale` on either.
    - **`stale` not already present.** `records.require_current_board` writes
      its own `stale: True` and must win: that row is the refusal that says
      the board moved, recorded from inside the very guard this flag reads.
    - **the caller named a board position that was current.** Absent that,
      the row gets no `stale` key and stays in the report's `unknown` bucket
      - see `records.named_a_current_board` for why that third state is kept
      rather than folded into `False`.

    Done here, once, rather than at each of the six places this api writes a
    refusal row (enumerated in `records._NAMED_A_CURRENT_BOARD`), because the
    version of this feature that left each site to pass the flag got `False`
    written by none of them: the bucket existed and was structurally always
    zero while every rate in the report still read healthy
    (MEMORY.md 2026-09-12).
    """
    if not named_a_current_board:
        return detail
    if detail.get("allowed") is not False or "stale" in detail:
        return detail
    return {**detail, "stale": False}


def _with_offered(detail: dict[str, Any], offered: bool | None) -> dict[str, Any]:
    """`detail` with `offered` added when this row is a refusal and this api
    knows whether its own enumeration had offered the action being refused
    (`records.offered_action`).

    The other half of the same split `_with_stale` starts. `stale` says
    whether the engine was asked at all; this says whether what it was asked
    about was something this api had itself just listed as legal for this
    seat. Only a refusal of an action that was *never offered* is
    unambiguously the seat's own misreading, and that is the only thing
    `scripts/eval_report.py` counts as a rules error.

    Written here, once, for the same reason the `stale` flag is: six places
    in this api record a refusal row (`records._NAMED_A_CURRENT_BOARD`
    enumerates them), and a flag each of them has to remember to pass is a
    flag that ends up written by none of them.

    `offered is None` means there was nothing to know - an action shape no
    enumeration lists as a row, or a refusal recorded before any command was
    built - and gets no key, so the report can say which rows it could not
    check rather than counting an unchecked one as a defect.
    """
    if offered is None:
        return detail
    if detail.get("allowed") is not False or "offered" in detail:
        return detail
    return {**detail, "offered": offered}


def record(
    game_id: str,
    action: str,
    /,
    *,
    reasoning: str | None = None,
    legal_actions_before: LegalActionsBefore | None = None,
    _fill_seq: int | None = None,
    **detail: Any,
) -> int:
    """Append one entry for `action` on `game_id` (or, when `_fill_seq` is
    given, fill in the payload of the `records` row `store.replace` already
    wrote at that `seq`), then print one console line.

    The entry carries the game's turn number, step, phase, stack size,
    active player and priority holder at the moment of recording, plus a
    full-information snapshot: board, both hands, stack with targets,
    graveyards, exile, life, pools. A game that is no longer in the store (or
    was never in it) still gets its entry, with those fields None.

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
    from .records import (  # lazy, with `store` below
        action_board,
        named_a_current_board,
        offered_action,
    )
    from .repository import GameUnloadable  # lazy, with `store` below
    from .serialize import snapshot  # lazy: keep module import light
    from .store import store  # lazy: audit imports nothing of the app at load

    detail = _with_offered(_with_stale(detail, named_a_current_board()), offered_action())
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
    turn_number = turn.turn_number if turn is not None else None
    step = turn.step if turn is not None else None
    # S2 A7: whose turn this row was written in, who held priority the
    # moment after it landed, and how many turns had actually begun - all
    # three read off the live `game` at write time, exactly like `turn_
    # number`/`step` above, so they are as honest as those already are about
    # a game this build cannot decode (`None`, same as every other
    # board-derived field on such a row).
    #
    # `active_player` is what `scripts/eval_report.py`'s `own_turn_first_
    # action_ms` groups rows by: a plan-ahead row (`declare_plan`,
    # `for_turn: "next"`) is written *during the opponent's turn* for a
    # *future* turn of the declaring seat, and this stamps the row with the
    # turn it was actually written in - never the turn `detail.turns_taken`
    # names it is *about* - which is exactly what keeps that row out of the
    # seat's own-turn bucket it is not yet in.
    #
    # `turns_taken` (`TurnMachine.turns_taken`) is the count that goes up
    # once per turn that actually begins, extra turns (CR 500.7) included -
    # unlike `turn_number`, which CR 500.7 leaves alone for one. A seat's
    # ordinary turn and an extra turn granted to that same seat right after
    # it therefore share one `(turn_number, active_player)` pair but never
    # one `turns_taken`, which is exactly the signal `eval_report._turn_
    # instances` needs to keep the two apart instead of collapsing them into
    # a single instance (and losing whatever happened at the real start of
    # the extra turn - the HIGH defect a review of this change's first
    # version found: a 30s stall at an extra turn's start read back as 0ms).
    # Stamped from the live game the same as `active_player`, so a
    # plan-ahead row reads the *write-time* value here too, same reasoning
    # as `active_player` above: it is never mistaken for its target turn.
    #
    # `priority_holder` is the sound signal `advice.latency_ms` measures
    # from: rather than guessing "the human's window" from an action's name
    # and outcome (a pass, a resolution, ...), each row already carries the
    # one fact that answers it directly, the same way `turn`/`step` already
    # do for the turn position. The row whose own `priority_holder` first
    # reads as a given player - after a row where it read otherwise, or the
    # game's first row - is the row that gave them the window an advice row
    # is timed from.
    #
    # All three are `None` on every row recorded before this change, exactly
    # like `stale`/`offered` before P4/T11 added those: a reader must treat
    # a missing key as "not known", never as "no active player" - see
    # `_UNRECORDED_DEFAULTS` and `eval_report.py`'s own fallback.
    active_player = turn.players[turn.active_index] if turn is not None else None
    turns_taken = turn.turns_taken if turn is not None else None
    priority_holder = (
        game.priority.holder if game is not None and game.priority is not None else None
    )
    # The step the action was taken in, not the one the committed board has
    # moved on to since (`records.note_action_board`, battle 3 B3-11).
    noted = action_board(game_id)
    if noted is not None:
        turn_number, step = noted
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "action": action,
        "detail": dict(detail),
        "turn": turn_number,
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
        "active_player": active_player,
        "priority_holder": priority_holder,
        "turns_taken": turns_taken,
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
    "active_player": None,
    "priority_holder": None,
    "turns_taken": None,
    "reasoning": None,
    "legal_actions_before": None,
    "legal_actions_for": None,
}


def _entry(row: dict[str, Any]) -> dict[str, Any]:
    """One `records` row as a well-formed entry - the `_UNRECORDED_DEFAULTS`
    fill-in every reader of this module depends on, in one place."""
    return {**_UNRECORDED_DEFAULTS, **row["payload"], "seq": row["seq"]}


def entries(game_id: str) -> list[dict[str, Any]]:
    """Every entry recorded for this game, oldest first.

    The whole ledger, and priced like it: each row carries a full snapshot
    and the acting seat's legal-action enumeration. This is right for a
    review, which runs once when a game is over; a caller that only wants
    what is new wants `entries_since` instead."""
    from .store import store  # lazy: audit imports nothing of the app at load

    return [_entry(row) for row in store.records(game_id)]


def entries_since(game_id: str, since: int) -> list[dict[str, Any]]:
    """The entries recorded for this game after `seq` `since`, oldest first
    - the whole ledger for `since=0`, and empty once the caller is level with
    it. Reads only the rows it returns; see `GameRepository.records_since`."""
    from .store import store  # lazy: audit imports nothing of the app at load

    return [_entry(row) for row in store.records_since(game_id, since)]


def last_seq(game_id: str) -> int:
    """The `seq` of this game's newest entry, `0` when it has none.

    The watermark a polling reader resumes from, answered without reading a
    single payload - which is what lets `entries_since` skip the rows the
    caller already has without also losing the ability to tell it where the
    ledger has got to."""
    from .store import store  # lazy: audit imports nothing of the app at load

    return store.max_record_seq(game_id)


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
    text = (
        str(detail.get("text") or _plan_text(detail) or _advice_text(entry) or "").strip()
        or "(no text)"
    )
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


def _advice_text(entry: dict[str, Any]) -> str | None:
    """An `ADVICE_ACTION` entry rendered for the console banner - `None` for
    any other entry, so `_loud_block`'s ordinary `text` path is untouched.

    Both halves, on purpose. `summary` is what the proposal *is*, derived
    server-side from the request body so it cannot disagree with it
    (`records.advice_summary`); `reasoning` is the advisor's own words, which
    is the half a person watching the log actually wants to judge. Printing
    only one of them would either hide what was proposed or hide why.

    Takes the whole `entry`, not only its `detail`, because those two halves
    live in different places: the summary is detail this route wrote, while
    the reasoning is `record`'s own top-level `reasoning` field - the same one
    every acting route's reasoning lands in, so an advisor's words are
    redacted, reviewed and read back by exactly the mechanisms a seat's
    already are rather than by a second set written for this row alone."""
    detail = entry.get("detail") or {}
    if entry.get("action") != ADVICE_ACTION:
        return None
    validated = "validated" if detail.get("validated") else "NOT validated"
    return "\n".join(
        [
            f"proposes: {detail.get('summary') or '?'} ({validated})",
            f"because : {entry.get('reasoning') or '?'}",
        ]
    )


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
