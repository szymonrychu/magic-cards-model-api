"""The FastAPI app: session setup, state, legality questions, and the frontend.

Everything lives under /api; / serves this repo's own play UI (`ui/`), and the
sibling frontend repo, when it is checked out next to this one, sits at
/frontend. /play/ - where the play UI used to live - redirects to /. There is
deliberately no CORS: the UI is served by this app itself, so there is exactly
one origin.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from magic_cards_model.machines import Step
from magic_cards_model.machines.decisions import Answer, DecisionKind
from magic_cards_model.machines.decisions import PendingDecision as PendingDecisionEngine
from magic_cards_model.machines.legal_actions import legal_actions as compute_legal_actions
from magic_cards_model.machines.priority import PriorityOutcome
from magic_cards_model.models import Card, CardState, Color, Game, Player, Zone
from starlette.convertors import Convertor, register_url_convertor
from starlette.responses import Response

from . import (
    affordances,
    ai_seat_api,
    api_decks,
    assets,
    audit,
    commands,
    decisions,
    decks_api,
    engine_api,
    game_access,
    oidc,
    opening,
    protocol,
    records,
    review,
    scryfall,
    serialize,
    symbology,
    visibility,
)
from .repository import RECENT_RECORDS_WINDOW, ArchivedGame, GameUnloadable
from .repository_postgres import VersionConflict
from .schemas import (
    ActionOutcome,
    ActivateRequest,
    ActorRequest,
    AdvanceRequest,
    AdvanceResponse,
    AffordancesResponse,
    AttackRequest,
    CardLookupResponse,
    CardNamesResponse,
    CastRequest,
    ConcedeRequest,
    ConcedeResponse,
    CrewRequest,
    CycleRequest,
    DecideRequest,
    DecideResponse,
    DeclareBlockersRequest,
    DeclaredPairRequest,
    DeclarePlanIn,
    DeclarePlanResponse,
    DeleteGameResponse,
    GameLogResponse,
    GameReviewResponse,
    GameStateView,
    GameSummariesResponse,
    HealthResponse,
    LegalActionsResponse,
    ListGamesResponse,
    ManaPoolUpdate,
    MintSeatRequest,
    MintSeatResponse,
    MoveCard,
    MoveCardSuccessResponse,
    MulliganRequest,
    MulliganResponse,
    NewGame,
    NewGameResponse,
    OpeningHandActionRequest,
    OpeningHandActionResponse,
    OpeningHandFinishResponse,
    PassPriorityRequest,
    PassPriorityResponse,
    PatchCard,
    PatchCardResponse,
    PatchPlayer,
    PlayLandRequest,
    PregameSettleRequest,
    PregameSettleResponse,
    PutCard,
    PutCardResponse,
    ReflectionIn,
    ReflectionResponse,
    ReportProblemRequest,
    ReportProblemResponse,
    ResolveTopResponse,
    SeatLookupResponse,
    SeatResponse,
    SetStepResponse,
    SpecialActionRequest,
    StateBasedActionsResponse,
    StepUpdate,
    TapCardResponse,
    TargetRequest,
)
from .serialize import state_view
from .store import known_players, new_seat_uids, player_for_advisor_uid, store

app = FastAPI(title="magic-cards-model-api", version="0.1.0")
# `FastAPI.__init__` has no `route_class` parameter of its own (it silently
# swallows an unknown keyword into `**extra` rather than raising, which is
# its own trap) - the router it builds internally is what actually carries
# one, and every `@app.get`/`@app.post`/... decorator below reads
# `app.router.route_class` fresh at the moment it runs, not at `FastAPI()`
# construction time. Setting it here, immediately, before any route is
# declared, is what makes `game_access.GameAccessRoute` apply to every
# `{game_id}` route this module defines directly.
app.router.route_class = game_access.GameAccessRoute
app.include_router(decks_api.router)
app.include_router(api_decks.router)
app.include_router(ai_seat_api.router)
app.include_router(symbology.router)
app.include_router(assets.router)


# -- error mapping ---------------------------------------------------------


@app.exception_handler(scryfall.CardNotFound)
def _card_not_found(_: Request, exc: scryfall.CardNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(scryfall.ScryfallError)
def _scryfall_error(_: Request, exc: scryfall.ScryfallError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(KeyError)
def _key_error(_: Request, exc: KeyError) -> JSONResponse:
    return JSONResponse(
        status_code=404, content={"detail": exc.args[0] if exc.args else "not found"}
    )


@app.exception_handler(opening.PregameOverruled)
def _pregame_overruled(_: Request, exc: opening.PregameOverruled) -> JSONResponse:
    """409, not 400. Registered above the `ValueError` handler because
    `PregameOverruled` is one, and the more specific handler must win.

    The seat's request was perfectly well formed - it lost a race to the
    backstop that moves for a seat gone quiet, and the difference matters to
    the caller: a 400 invites an agent to fix its request and retry, which
    cannot work, while this says what happened and that the decision is gone.
    See `opening.PregameOverruled` and `ai_pregame.py`.
    """
    return JSONResponse(status_code=409, content={"detail": str(exc), "overruled": True})


@app.exception_handler(records.MissingReasoning)
def _missing_reasoning(_: Request, exc: records.MissingReasoning) -> JSONResponse:
    """422, shaped like `RequestValidationError`'s own body
    (`detail: [{"loc", "msg", "type"}]`) - naming the missing field the same
    way a genuinely absent required field would, rather than the bare 400 a
    `ValueError` gets everywhere else in this api. This is not the request
    being malformed; it is a well-formed request an agent seat is not
    allowed to make without saying why (T10)."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"loc": ["body", "reasoning"], "msg": str(exc), "type": "missing"},
            ]
        },
    )


@app.exception_handler(records.MissingPlan)
def _missing_plan(_: Request, exc: records.MissingPlan) -> JSONResponse:
    """422, the same shape `MissingReasoning` gets, naming `plan` as the
    missing field.

    Deliberately reusing that shape rather than inventing a second one: to a
    seat these are the same kind of refusal - a well-formed request an agent
    is not allowed to make yet, with the name of the thing it owes first -
    and `seatkit.Seat.call_json` already normalises this shape into
    `allowed: False` with the message as the reason (see
    `tests/test_non_2xx_outcomes.py`). A new shape would have been a second
    thing every client has to learn."""
    return JSONResponse(
        status_code=422,
        content={"detail": [{"loc": ["body", "plan"], "msg": str(exc), "type": "missing"}]},
    )


@app.exception_handler(records.BoardMoved)
def _board_moved(request: Request, exc: records.BoardMoved) -> JSONResponse:
    """409: this seat decided against a board that has since moved, and the
    action was refused before the engine ran - see `records.BoardMoved`.

    409 for the same reason `VersionConflict` and `CommandDivergenceError`
    are: nothing about the request is malformed, and the caller cannot fix
    it by sending something else - it has to re-read first. Not the 200
    `allowed: False` an engine refusal takes, because this is not the engine
    speaking; a status of its own is what lets a client tell the two apart
    even before it looks at the body.

    `allowed: False` and `reason` are there so `seatkit._normalise_non_2xx`
    hands it on unchanged (a body carrying its own `allowed` is trusted as
    already-refusal-shaped) and every existing caller reads it as the
    refusal it is. `stale: True` is the discriminator - the same
    key-you-can-branch-on convention `PregameOverruled`'s `overruled` and
    `GameUnloadable`'s `unloadable` already set - and `moved_from`/
    `moved_to` say by how much.

    **`since` is the ledger, re-read through `GET .../log` itself** rather
    than through a second, differently-redacted read of the same rows: the
    same function, with the same `uid` this request already carried on its
    query string (exactly how `_version_conflict` above resolves its own
    viewer). A caller is therefore handed precisely what a log read of its
    own would have given it and never a byte more - the redaction argument
    for this body is `game_log`'s argument, unchanged - and it is handed it
    without spending the round trip.
    """
    uid = request.query_params.get("uid")
    since = game_log(exc.game_id, uid=uid, since=exc.expected_seq)
    # Bounded at `current_seq`, the position the board had actually moved to
    # when this call was refused - which excludes the refusal's own row,
    # written a moment before this handler ran. "What moved while I was
    # thinking" is the question; the caller is holding the answer to "what
    # happened to my call" already.
    moved = [entry for entry in since["log"] if entry["seq"] <= exc.current_seq]
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "game_id": exc.game_id,
            "allowed": False,
            "reason": str(exc),
            "stale": True,
            "moved_from": exc.expected_seq,
            "moved_to": exc.current_seq,
            "since": moved,
            "last_seq": exc.current_seq,
        },
    )


@app.exception_handler(VersionConflict)
def _version_conflict(request: Request, exc: VersionConflict) -> JSONResponse:
    """409: another writer's `replace()` committed a newer version first
    (`repository_postgres.py`'s own module docstring - optimistic
    concurrency, no server-side retry). The body carries the game's current
    state, redacted exactly the way `GET /api/games/{id}` redacts it for the
    same `uid` this request itself carried - the loser is never handed more
    than it was already entitled to see - so the loser can re-read and
    recompute its attempt against what is actually committed, rather than
    retrying blind against the state it already knows is stale."""
    uid = request.query_params.get("uid")
    viewer = visibility.resolve_viewer(exc.game_id, uid)
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "game_id": exc.game_id,
            "current_state": visibility.redact_view(
                state_view(exc.current_game, exc.game_id), viewer
            ),
        },
    )


@app.exception_handler(GameUnloadable)
def _game_unloadable(_: Request, exc: GameUnloadable) -> JSONResponse:
    """409: this game's row is there, but this build cannot decode the
    snapshot in it - see `repository.GameUnloadable`. The case it exists for
    is an engine schema bump (`Game.CURRENT_SCHEMA_VERSION` moved past what
    the row was written at) and the body carries the engine's own refusal
    verbatim, because that refusal already says exactly what is wrong and
    what the options are; nothing here paraphrases it.

    409 rather than 500, which is what this used to be and is the whole
    reason this handler exists: nothing about the request is malformed and
    nothing here is unexpected. It is a conflict between the state that is
    committed and the build being asked to read it - the same family as
    `VersionConflict` and `PregameOverruled` above, which is why it answers
    with their status.

    Not 404 (`KeyError`'s): the game exists, and telling a seat it does not
    would send it off to start a new one rather than report the real cause.
    Not 400 (`ValueError`'s): the caller did nothing wrong and cannot fix
    this by sending something else.

    `unloadable: true` is a flag a client can key on without matching on
    prose - `ui/render/lobby.js` marks the row unopenable from it - the same
    convention `PregameOverruled`'s `overruled: True` already set.
    """
    return JSONResponse(
        status_code=409,
        content={"detail": exc.reason, "game_id": exc.game_id, "unloadable": True},
    )


@app.exception_handler(commands.CommandDivergenceError)
def _command_divergence(_: Request, exc: commands.CommandDivergenceError) -> JSONResponse:
    """409: the stored pause is stale, not this request malformed.

    `resume_command`'s replay-divergence guard tripped - replaying the paused
    command from its committed snapshot with the tape as it stood before this
    answer no longer reproduces the exact question that was stored (see
    `commands.CommandDivergenceError`'s own docstring). That is a genuine
    conflict a caller can only resolve by reading fresh state and trying
    again, the same shape of problem a 409 already means everywhere else in
    this api - `PregameOverruled` (below) is the same status for the same
    reason.
    """
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(commands.DecisionInProgressError)
def _decision_in_progress(_: Request, exc: commands.DecisionInProgressError) -> JSONResponse:
    """409: this game is part-way through another seat's command and is
    waiting on a decision only that seat can answer, so nobody has priority
    to take this action with - see `commands.DecisionInProgressError` for
    the rules that make it a refusal rather than a lock.

    Registered above the `ValueError` handler with the other 409s. It is a
    `RuntimeError` on purpose and not a `ValueError`: `pass-priority`'s and
    `advance`'s own `except ValueError` blocks turn an engine refusal into a
    200 with `allowed: False`, and this must not be swallowed into one of
    those - the whole point is that the command did not run.

    `allowed: False` and `reason` are what `seatkit._refused` reads, so a
    seat sees "nothing changed, still your move" rather than an error it has
    no handling for. Deliberately no `paused: True`: `seatkit._paused` keys
    off exactly that, and would send this seat off to answer a decision that
    is not its own. Deliberately no `pending_decision` either - the question
    standing here belongs to the other seat, and its candidates can be cards
    out of a hidden library (CR 400.2), which the refused caller is not
    entitled to see.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc), "allowed": False, "reason": str(exc)},
    )


@app.exception_handler(ValueError)
def _value_error(_: Request, exc: ValueError) -> JSONResponse:
    """Setup requests that name an unknown zone, counter, color, or card."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(RequestValidationError)
def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # No `include_url`/`include_input` kwargs: pydantic 2.13 removed them from
    # `errors()`, and calling them turned every malformed request body into a
    # raw 500 instead of this 422.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {key: error.get(key) for key in ("loc", "msg", "type")} for error in exc.errors()
            ]
        },
    )


def game_or_404(game_id: str) -> Game:
    game = store.get(game_id)
    if game is None:
        raise KeyError(f"no game {game_id}")
    return game


def card_or_404(game: Game, card_id: UUID) -> CardState:
    return engine_api.find_card(game, card_id)


def audit_outcome(
    game_id: str,
    action: str,
    outcome: engine_api.Outcome,
    *,
    dry_run: bool = False,
    reasoning: str | None = None,
    legal_actions_before: records.LegalActionsBefore | None = None,
    seq_hint: int | None = None,
    **keys: Any,
) -> dict[str, Any]:
    """Record an action attempt and hand the engine's answer to the caller.

    `log_seq` is the audit entry's own monotonic sequence number
    (`audit.record`'s return value) - a cheap, already-existing idempotency
    marker: a caller unsure whether a timed-out call actually landed can
    compare the `log_seq` on a later, successful call (or a `GET .../log`
    read) against what it saw before, rather than guessing from a retry.

    `dry_run=True` skips `audit.record` entirely rather than logging the
    probe: `seatkit.available_actions` issues one such dry-run `cast`/`activate`
    call per card in hand and per ability on the battlefield, every time a
    seat's blocked call wakes, and every one of them - refused or not -
    changes nothing. Auditing them anyway is how one traced game's log grew to
    2410 entries with 1355 of them `cast`, 956 of those refusal noise from
    probing cards with no flash - real plays buried in attempts nobody
    actually made. A caller of a dry run already has its own legality answer
    in `outcome`/the HTTP response; `log_seq` is `None` here because there is
    no log entry for it to identify.

    **T10**: `reasoning` and `legal_actions_before` ride straight through to
    `audit.record` unchanged - this function does not compute either, only
    passes along what `_run_command`/`_respond` (or a direct caller such as
    `move_card`/`tap_card`) already worked out. `seq_hint`, when given, is
    the `records` row `store.replace` already wrote for this exact commit
    (T9's same-transaction guarantee) - passing it through as `audit.
    record`'s `_fill_seq` fills that row instead of appending a second,
    orphaned one for the same action.

    **`commands.run_command`'s notices ride `outcome.detail["notices"]`**
    (`commands._attach_notices`), which `outcome.body()` already splats into
    the raw response - this is the one generic place that (a) also logs
    them, since `audit.record` is only ever given `**keys`, never
    `outcome.detail` itself, and (b) redacts the *response* copy for
    whichever player this route already trusts as its actor. None of the
    routes this serves carry a `uid` - `keys.get("player")` is the same
    self-declared identity `records.require_reasoning` and everything else
    in this file already trusts for these routes, so redacting against it is
    not a new trust boundary, only the first thing that needed one. The
    persisted log keeps the *raw* notices regardless of viewer - `GET
    .../log` redacts its own copy per-reader later, through
    `visibility.redact_log_entry`.
    """
    viewer = keys.get("player")
    body = outcome.body()
    notices = outcome.detail.get("notices")
    if notices:
        body = {**body, "notices": visibility.redact_notices(notices, viewer)}
    if dry_run:
        return {**body, "log_seq": None}
    log_keys = {**keys, "notices": notices} if notices else keys
    seq = audit.record(
        game_id,
        action,
        allowed=outcome.allowed,
        reason=outcome.reason,
        reasoning=reasoning,
        legal_actions_before=legal_actions_before,
        _fill_seq=seq_hint,
        **log_keys,
    )
    return {**body, "log_seq": seq}


def _pause_slot_guard(
    game_id: str, game: Game, command: commands.Command
) -> tuple[commands.Command, tuple[Answer, ...], PendingDecisionEngine] | None:
    """Settle what a stored pause means for `command`, before `command` runs.

    Returns the stored `(command, tape, pending)` triple when the standing
    pause is live and is `command`'s own - the caller hands that straight
    back as its result instead of re-running anything. Returns `None` when
    nothing stands in this command's way, either because the slot was empty
    or because the pause in it turned out to be stale and was cleared.
    Raises `commands.DecisionInProgressError` when the pause is live and
    belongs to a different command.

    **Liveness is tested, not assumed** (`commands.pause_is_live`): the
    stored command is replayed from the committed `game` with the stored
    tape, and only a replay landing on the identical `PendingDecision`
    counts. Bare "first pause wins" would wedge a game forever on a
    question no seat can answer any more - a board-setup route such as
    `POST .../cards` writes straight to the committed game without going
    through `_run_command` at all, and is now the only real way the state
    can move out from under a stored pause.

    **The identical command is a retry, not a competitor.** `seatkit`'s
    autopass re-posts its own `pass-priority` on every `paused: true` it
    reads (`seatkit.py`), so the command in the slot is very often the next
    one to arrive; that must get the question that is actually standing,
    with its tape intact, rather than a 409 it can only escalate over or a
    fresh run from an empty tape.
    """
    paused = store.paused_command(game_id)
    if paused is None:
        return None
    stored_command, stored_tape, stored_pending = paused
    if not commands.pause_is_live(game, stored_command, stored_tape, stored_pending):
        store.clear_paused_command(game_id)
        return None
    if stored_command != command:
        raise commands.DecisionInProgressError(stored_command, stored_pending, command)
    return paused


def _run_command(
    game_id: str, kind: str, request: Any
) -> tuple[commands.RunResult, Game, records.LegalActionsBefore | None, int | None]:
    """Run `kind` fresh against `game_id`'s current committed state
    (`commands.run_command`), and settle the result into the store: commit
    the new game on `RunStatus.COMPLETED`, or stash `(command, tape,
    pending)` for `POST .../decide` (and its `actions/attack`/
    `actions/declare-blockers` aliases) to resume on `PAUSED`.

    Returns `(result, committed, legal_actions_before, seq)`. `committed` is
    the game exactly as it stood before this attempt (the `Game` `store.
    locked` handed this request), which a few callers (`pass-priority`'s
    before/after resolution diff) still need after `run_command` returns its
    own, separate working copy.

    **T10**: `legal_actions_before` is `records.legal_actions_before` read
    against that same pre-mutation `committed` game and this game's paused
    decision (if any), for `request.player` - computed here, inside the
    `with store.locked(game_id):` block, *before* `commands.run_command` is
    even called, which is what guarantees "before, not after" (`records.py`'s
    own module docstring). `seq` is the `records` row `store.replace` wrote
    for this commit (`None` on `PAUSED`, since nothing was replaced) - a
    caller threads it into `_respond`/`audit_outcome` as `seq_hint` so the
    row gets filled in rather than duplicated once the outcome is known.

    **A live pause blocks every other command.** Before `commands
    .run_command` is called at all, this reads the stored `(command, tape,
    pending)` slot and, when something is there, hands it to
    `_pause_slot_guard`: a pause that no longer describes the committed
    game is cleared and this request carries on, a live one that is *this
    very command* is handed straight back un-re-run, and a live one
    belonging to anything else raises `commands.DecisionInProgressError`
    (409) rather than being overwritten.

    This is the pause-slot clobber, closed. `_run_command` used to run every
    fresh command from the committed snapshot with an empty tape and then
    store `(command, tape, pending)` unconditionally, so any of the fourteen
    mutating routes could write its own empty tape over a live pause that
    already carried answers - which is how a seat's fetchland search answer
    was deleted by the opponent's ordinary autopass 227 ms later, and the
    fetch resolved as a decline (`tests/test_pause_slot_clobber.py`).

    Two commands are exempt. A `dry_run` commits nothing and never touches
    the slot in either direction (`seatkit.available_actions` probes every
    card in hand and every ability on the battlefield this way on every
    wake, and none of those probes is an action anyone took), and `concede`
    is legal "at any time" - CR 104.3a - which includes part-way through
    somebody else's command.

    `store.clear_paused_command` still runs on every `COMPLETED` result, and
    the guard above is what makes that safe: the only slot it can now find
    is one this same call already proved stale, or one a `concede`/`dry_run`
    exemption deliberately walked past. The argument it used to rest on -
    that a paused command was never applied to the committed game, so
    nothing is thrown away but a *question* - holds only while the tape is
    empty. The moment the pause carries answers, clearing the slot deletes
    them, and no guard can see it afterwards: a deleted tape leaves no
    trace. Staleness is handled by testing it (`commands.pause_is_live`)
    rather than by assuming it, so refusing here still cannot wedge a game
    on a pause nobody can answer.

    **A `dry_run` request commits nothing (T10).** `request.dry_run`, when
    the request shape carries one, never reaches `store.replace`/`store.
    store_paused_command` at all - a dry run's own engine-level probe
    (`engine_api.dry_run_copy`) already never mutates the real game, but
    `commands.run_command` still returns `RunStatus.COMPLETED` for it (a
    dry run "asks nothing unscripted, so it always completes" - `cast`'s own
    docstring), and `store.replace` does not know or care that the `Game` it
    is handed is state-identical to what is already committed - it would
    bump `version` and write a fresh `records` row regardless. Before this
    guard, that row's payload never got filled in (`_respond`'s `dry_run`
    branch skips `audit.record` entirely, on purpose - see its own
    docstring), so every dry-run probe left an orphaned, empty `records` row
    behind it, which `audit.entries` then choked on. Skipping persistence
    outright is what "changes nothing" was always supposed to mean.
    """
    command = commands.Command.build(kind, request)
    player = getattr(request, "player", None)
    dry_run = getattr(request, "dry_run", False)
    with store.locked(game_id) as game:
        if not dry_run and kind != "concede":
            standing = _pause_slot_guard(game_id, game, command)
            if standing is not None:
                _, standing_tape, standing_pending = standing
                return (
                    commands.RunResult(
                        status=commands.RunStatus.PAUSED,
                        pending=standing_pending,
                        tape=standing_tape,
                    ),
                    game,
                    records.legal_actions_before(game, player, standing_pending),
                    None,
                )
        # Read *after* the guard: a stale slot it just cleared must not be
        # handed to `legal_actions_before` as if it were still standing.
        paused = store.paused_command(game_id)
        # Not for a dry run. The enumeration exists to be written into this
        # action's `records` row and read back out of it by the one seat it
        # was computed for (`visibility.redact_log_entry`); it is in no
        # response body. A dry run writes no row at all - `_run_command`
        # skips `store.replace`, `audit_outcome` returns before
        # `audit.record` - so the whole thing was computed and dropped, once
        # per probe, inside the lock every other seat's real action is
        # queued behind. It is not a cheap thing to drop: a mana-candidate
        # scan of the battlefield, cast/land/marked-exile/off-zone
        # enumeration for every card in hand, graveyard and shared exile,
        # then `computed_board()` for the full CR 613 layer stack with a
        # per-permanent ability probe. The UI fires one dry run per
        # candidate before every real cast. Nothing observable changes:
        # `tests/test_dry_run_cost.py` records the ledger-consumer audit
        # this rests on.
        legal_before = (
            None
            if dry_run
            else records.legal_actions_before(game, player, paused[2] if paused else None)
        )
        result = commands.run_command(game, command)
        seq: int | None = None
        if dry_run:
            pass
        elif result.status is commands.RunStatus.COMPLETED:
            assert result.game is not None
            seq = store.replace(game_id, result.game)
            store.clear_paused_command(game_id)
        else:
            assert result.pending is not None
            store.store_paused_command(game_id, command, result.tape, result.pending)
    # Outside the lock, and after `store.replace` has committed: the ending
    # is appended as its own row (seq `n + 1`) while the action's own row
    # (`seq`) is still a placeholder its caller fills in - so the ledger
    # reads "the action, then the ending", in that order. See
    # `_record_game_over`.
    if not dry_run and result.status is commands.RunStatus.COMPLETED:
        assert result.game is not None
        _record_game_over(game_id, result.game)
    return result, game, legal_before, seq


def _record_game_over(game_id: str, game: Game) -> None:
    """Write this game's one `game_over` row, the first time a committed
    command leaves it finished.

    CR 104.1 - looked up in session, `mise run rules:find 104.1` - reads "A
    game ends immediately when a player wins, when the game is a draw, or
    when the game is restarted", and `Game.is_over` is the engine's own
    reading of the first two (`winner is not None or drawn`; the engine
    models no restart).

    Why the ledger needed this at all: the live corpus's distinct actions
    were `ai_seat, attack, cast, create_game, decide, game_reset,
    mulligan_declared, opening_hand_action, opening_hand_done, pass_priority,
    play_land, report_problem` - not one of them a terminal outcome. A game
    ending to lethal damage left the result to be inferred from the final
    snapshot's life totals, which is precisely the inference a review meant
    to convince a sceptic must not ask for.

    Called from `_run_command`/`_resume` after their `store.replace`, and
    from outside the `store.locked` block so this append never runs while a
    game's mutation lock is held. A finished game still accepts calls (the
    engine refuses them, and a refusal is still a row), so the "once" is
    enforced by looking: the same bounded window `records.require_plan`
    reads, scanned for an ending already written. Bounded for the same
    reason - this is on the action path too.
    """
    if not game.is_over:
        return
    window = store.recent_records(game_id, RECENT_RECORDS_WINDOW)
    if any(row["payload"].get("action") == audit.GAME_OVER_ACTION for row in window):
        return
    audit.record(
        game_id,
        audit.GAME_OVER_ACTION,
        winner=game.winner,
        drawn=game.drawn,
        note=(f"{game.winner} wins" if game.winner else "the game is a draw"),
    )


def _paused_on(result: commands.RunResult) -> dict[str, str]:
    """`audit.record`'s `paused_on` detail for a `PAUSED` `RunResult` - whose
    decision it is waiting on, and what kind. `pending_decision` already
    carries this in every paused HTTP response; this is the same two fields,
    threaded into the persisted `records` row (and so the console line) so a
    reader of the log - live or replayed - can see what a pause is actually
    waiting on rather than just that it is waiting. See `audit._console_line`.
    """
    assert result.pending is not None
    return {"player": result.pending.decision.player, "kind": result.pending.decision.kind.value}


def _respond(
    game_id: str,
    action: str,
    result: commands.RunResult,
    *,
    dry_run: bool = False,
    reasoning: str | None = None,
    legal_actions_before: records.LegalActionsBefore | None = None,
    seq_hint: int | None = None,
    **keys: Any,
) -> dict[str, Any]:
    """Turn a `_run_command` attempt into this route's JSON response,
    auditing it either way - refused, allowed, or paused waiting on a
    decision (`allowed: None`) are all real outcomes worth a log entry, the
    same way a refusal already was. `keys` are the action-specific detail
    `audit.record` already took for this route (the card cast, the targets
    offered, ...) before this task, unchanged by which of the three this
    attempt came to. `reasoning`/`legal_actions_before`/`seq_hint` are T10's
    additions - see `_run_command`'s and `audit_outcome`'s own docstrings.

    For every route this serves, `PAUSED` was unreachable before this task -
    the request blocked instead, inside `Game.ask`, until an answer arrived
    or the provider's timeout defaulted one. `allowed: None` is a new value
    no existing caller has seen from these routes; `paused: True` and
    `pending_decision` are new keys - see this module's own report on this
    task for why that is the intended, minimal shape rather than a full
    state view merged in, matching every one of these routes' existing,
    state-free response shape.
    """
    if result.status is commands.RunStatus.PAUSED:
        assert result.pending is not None
        pending = decisions.PendingDecision.from_engine(
            result.pending.decision, store.pause_generation(game_id)
        )
        body = {
            "allowed": None,
            "reason": None,
            "paused": True,
            "pending_decision": pending.to_view(),
        }
        if dry_run:
            return {**body, "log_seq": None}
        # No `seq_hint` here: PAUSED means `_run_command` never called
        # `store.replace` at all - there is no row to fill in, so this is a
        # fresh standalone append, the same as any refusal caught before a
        # command ever ran.
        seq = audit.record(
            game_id,
            action,
            allowed=None,
            reason=None,
            paused=True,
            paused_on=_paused_on(result),
            reasoning=reasoning,
            legal_actions_before=legal_actions_before,
            **keys,
        )
        return {**body, "log_seq": seq}
    assert result.outcome is not None
    return audit_outcome(
        game_id,
        action,
        result.outcome,
        dry_run=dry_run,
        reasoning=reasoning,
        legal_actions_before=legal_actions_before,
        seq_hint=seq_hint,
        **keys,
    )


def _resume(
    game_id: str,
    command: commands.Command,
    tape: tuple[Answer, ...],
    pending: PendingDecisionEngine,
    answer: Answer,
    *,
    player: str | None = None,
) -> tuple[commands.RunResult, records.LegalActionsBefore | None, int | None]:
    """Resume `game_id`'s paused command with `answer` appended to `tape`
    (`commands.resume_command`), and settle the result into the store
    exactly like `_run_command` does for a fresh command: commit on
    `RunStatus.COMPLETED`, restash `(command, tape, pending)` on `PAUSED`.

    Returns `(result, legal_actions_before, seq)` - see `_run_command`'s own
    docstring for what each means; `player` here is the caller's own
    resolved actor (`_answer_declaration`'s `engine_decision.player`, or
    `decide`'s `req.player`), since a paused command carries no live request
    object of its own to read `.player` off.

    Raises `ValueError` when the engine itself refuses `answer` once the
    command actually replays with it (the same propagation `run_command`
    already gives `_run_command`'s callers), and
    `commands.CommandDivergenceError` when the stored pause turns out to be
    stale (`resume_command`'s own replay-divergence guard) - neither is
    caught here; `POST .../decide` and its `actions/attack`/
    `actions/declare-blockers` aliases each turn the first into their own
    synchronous `{"accepted": False, ...}` refusal, and the second reaches
    this api's registered 409 handler unchanged.
    """
    with store.locked(game_id) as game:
        legal_before = records.legal_actions_before(game, player, pending)
        result = commands.resume_command(game, command, tape, pending, answer)
        seq: int | None = None
        if result.status is commands.RunStatus.COMPLETED:
            assert result.game is not None
            seq = store.replace(game_id, result.game)
            store.clear_paused_command(game_id)
        else:
            assert result.pending is not None
            store.store_paused_command(game_id, command, result.tape, result.pending)
    # Same as `_run_command`: outside the lock, after the commit, once - see
    # `_record_game_over`. A resumed command can end a game just as a fresh
    # one can (a decision answered into lethal damage, a concede that paused
    # on a leaves-the-battlefield trigger).
    if result.status is commands.RunStatus.COMPLETED:
        assert result.game is not None
        _record_game_over(game_id, result.game)
    return result, legal_before, seq


# -- landing page when the frontend repo is absent --------------------------

_LANDING = """<!doctype html>
<html>
<head><title>magic-cards-model-api</title></head>
<body style="font-family: monospace; background: #1b1b1b; color: #cfcfcf; padding: 2em">
<h1>magic-cards-model-api</h1>
<p>The API is up. The frontend repo (magic-cards-model-test-frontend) was not found
next to this repo, so there is nothing to look at here.</p>
<p>Interactive docs: <a style="color:#7fbf7f" href="/docs">/docs</a></p>
</body>
</html>"""


def frontend_dir() -> Path | None:
    env = os.environ.get("MCM_FRONTEND_DIR")
    candidates = [Path(env)] if env else []
    here = Path(__file__).resolve().parents[2]
    candidates.append(here.parent / "magic-cards-model-test-frontend")
    for candidate in candidates:
        if (candidate / "index.html").exists():
            return candidate
    return None


# -- sessions ---------------------------------------------------------------


@app.get("/api/health", response_model=HealthResponse, response_model_exclude_unset=True)
def health() -> dict[str, Any]:
    """`protocol_version` is what `mcp.py`'s startup self-check compares
    against its own build of `seatkit.py` - see that constant's docstring for
    why a long-running MCP server process needs this.

    No `decision_timeout_seconds` any more: that field existed only to size a
    `Seat`'s HTTP read timeout against a mutating request's own worst-case
    block inside `Game.ask` (`decisions.py`, `seatkit.resolve_client_timeout`)
    - a bound this API no longer needs, because no mutating request blocks on
    a live decision at all now. A question with no scripted answer pauses and
    returns immediately instead (`commands.py`); see this task's report on
    what replaced the two constants this field used to expose.
    """
    return {
        "status": "ok",
        "protocol_version": protocol.PROTOCOL_VERSION,
    }


@app.post("/api/games", response_model=NewGameResponse, response_model_exclude_unset=True)
def create_game(req: NewGame, request: Request) -> dict[str, Any]:
    user = oidc.current_user(request)
    names = [player.username for player in req.players]
    if len(set(names)) != len(names):
        raise ValueError("player names must be unique")
    # The engine gained a required `schema_version` so a state written by one
    # engine cannot be decoded by another without a signal. Stamp it when the
    # engine we are built against has it, so this works either side of that
    # change rather than pinning the two repos to one release.
    stamp = getattr(Game, "CURRENT_SCHEMA_VERSION", None)
    game = Game(
        players=[Player(username=player.username, life=player.life) for player in req.players],
        **({"schema_version": stamp} if stamp is not None else {}),
    )
    active = req.active_player or names[0]
    index = names.index(active)
    assert game.turn is not None and game.priority is not None
    game.turn.active_index = index
    game.priority.reset(index)
    game_id = store.create(game, owner_user_id=user.sub if user is not None else None)
    uids = new_seat_uids(game)
    store.register_seats(game_id, uids)
    engine_api.set_step(game, Step(req.step))
    # T10: this game's seat kind, recorded once here rather than invented as
    # a second marker - `records.require_reasoning` reads the same
    # `ai_players` set `decks_api.start_game_from_decklists` already
    # populates for a decklist-booted game.
    agents = [player.username for player in req.players if player.is_agent]
    if agents:
        store.register_ai_players(game_id, agents)
    audit.record(game_id, "create_game", players=names, active_player=active, step=req.step)
    # The creator gets the seat uids AND the admin key in this one response -
    # already holding the keys to every seat, an unredacted view here tells
    # it nothing a filtered one would have hidden.
    return {
        **state_view(game, game_id),
        "seat_uids": {name: uid for uid, name in uids.items()},
        "admin_key": store.admin_key(game_id),
    }


@app.get(
    "/api/games/{game_id}/seat", response_model=SeatResponse, response_model_exclude_unset=True
)
def whose_seat(game_id: str, uid: str) -> dict[str, Any]:
    """Whose seat this uid opens. A uid that is not in the game is nobody's."""
    game_or_404(game_id)
    player = store.player_for_uid(game_id, uid)
    if player is None:
        raise KeyError("that uid is no seat in this game")
    return {"player": player}


@app.post(
    "/api/games/{game_id}/seats",
    response_model=MintSeatResponse,
    response_model_exclude_unset=True,
)
def mint_seat(game_id: str, req: MintSeatRequest) -> dict[str, Any]:
    """A fresh seat credential for an already-registered player - 2b's T8
    (map's "seat back door"), the mint half. Replaces the tests'/mcp's habit
    of reading `store.seat_uids` straight out of this process, which no
    out-of-process caller can do: a test harness or a shared mcp server (M2)
    can call this instead and get a uid it can actually use. Refuses (404) a
    player this game does not have.

    Read-modify-write under this game's own mutation lock
    (`store.locked`), since `register_seats` replaces the whole uid map
    rather than merging into it - the lock is what keeps two concurrent
    mints from racing each other's read of the existing uids.
    """
    with store.locked(game_id) as game:
        if req.player not in {p.username for p in game.players}:
            raise KeyError(f"{req.player!r} is no player in this game")
        uid = uuid4().hex
        store.register_seats(game_id, {**store.seat_uids(game_id), uid: req.player})
    return {"uid": uid, "player": req.player}


@app.get("/api/seats/{uid}", response_model=SeatLookupResponse, response_model_exclude_unset=True)
def find_seat(uid: str) -> dict[str, Any]:
    """Resolve a uid to its game, its player and what kind of credential it
    is, with no game id known in advance - 2b's T8 (map's "seat back door"),
    the lookup half. `whose_seat` above already answers this once the caller
    knows the game; this is the reverse direction `store.seat_uids` gave an
    in-process caller for free. 404 on a uid no live game recognises.

    **`role` is why this also resolves advisors** (`repository.Advisor`).
    One mcp process holds several uids and is handed no game pin with any of
    them, so this route is the only place it can learn what a uid *is*
    before using it - and an advisor driven as if it were a seat would spend
    its whole life being refused by `game_access.py`, one action at a time,
    with nothing to say why. `player` means the same thing for both: the
    seat whose view that uid reads.
    """
    for game_id in store.ids():
        player = store.player_for_uid(game_id, uid)
        if player is not None:
            return {"game_id": game_id, "player": player, "role": "seat"}
        advised = player_for_advisor_uid(game_id, uid)
        if advised is not None:
            return {"game_id": game_id, "player": advised, "role": "advisor"}
    raise KeyError(f"no seat {uid!r}")


@app.get(
    "/api/games/summary", response_model=GameSummariesResponse, response_model_exclude_unset=True
)
async def game_summaries_route(ids: str, request: Request) -> dict[str, Any]:
    """2b's T8 (map's S2): the scalars a lobby list needs, for every id in
    one call - `ids` comma-separated. Never the full `state_view` per game
    `list_lobby_games` used to build just to read four fields back out of
    it; unknown ids are silently skipped, the same way that loop already
    skipped a `store.get` miss.

    A game this caller may not reach (`game_access.can_access_game` -
    someone else's owned game) is skipped exactly the same way - not just
    unactionable, absent, so a caller cannot even learn *that much* about a
    game that is not theirs by asking for its scalars directly.

    A game this build cannot *decode* is the one case that is neither
    answered nor silently skipped: `serialize.unloadable_game_summary` puts
    it in the answer with `unloadable` set. Skipping it here would be worse
    than useless - `decks_api.list_lobby_games` reads this route's result to
    build the lobby, and a missing row there is exactly how a game the user
    played would appear to have vanished."""
    game_ids = [i for i in ids.split(",") if i]
    summaries = []
    for game_id in game_ids:
        if not await game_access.can_access_game(request, game_id):
            continue
        try:
            game = store.get(game_id)
        except GameUnloadable as exc:
            summaries.append(
                serialize.unloadable_game_summary(
                    game_id, exc.reason, seat_count=len(known_players(game_id))
                )
            )
            continue
        if game is None:
            continue
        summaries.append(serialize.game_summary(game, game_id))
    return {"summaries": summaries}


@app.get("/api/games", response_model=ListGamesResponse, response_model_exclude_unset=True)
async def list_games(request: Request) -> dict[str, Any]:
    """Every game this caller may reach.

    One row that will not decode must never take this whole route down -
    the outage this shape exists for. `store.ids()` names the rows without
    decoding any of them; the decode happens here, per game, and a game that
    refuses is listed with `unloadable` carrying the decoder's own words
    instead of being dropped (a game that vanishes from a list is a worse
    lie than a game that says why it cannot be opened) and instead of
    raising (which 500'd the listing for every *other* game as well).
    """
    games = []
    for game_id in store.ids():
        if not await game_access.can_access_game(request, game_id):
            continue
        try:
            game = game_or_404(game_id)
        except GameUnloadable as exc:
            games.append(
                {
                    "game_id": game_id,
                    "players": known_players(game_id),
                    "step": None,
                    "unloadable": exc.reason,
                }
            )
            continue
        games.append(
            {
                "game_id": game_id,
                "players": [player.username for player in game.players],
                "step": game.turn.step.value if game.turn else None,
            }
        )
    return {"games": games}


@app.get("/api/games/{game_id}", response_model=GameStateView, response_model_exclude_unset=True)
def get_game(game_id: str, uid: str | None = None) -> dict[str, Any]:
    """The board, filtered to what `uid` may see (`visibility.py`).

    No uid, or one this game does not recognise, sees nothing hidden - the
    same treatment as any other caller talking to the API directly instead
    of through a dispatched agent's seat. A valid seat uid sees its own hand
    and both public boards; the game's admin key sees everything.

    Used to also mark this seat as watching/delivered on the game's decision
    mailbox, so a mutating request parked in `Game.ask` could tell a live
    seat apart from an unattended one. That whole mechanism is gone - a
    mutating request never blocks on a decision any more, so there is
    nothing here to be evidence for.
    """
    game = game_or_404(game_id)
    viewer = visibility.resolve_viewer(game_id, uid)
    return visibility.redact_view(state_view(game, game_id), viewer)


@app.get(
    "/api/games/{game_id}/log", response_model=GameLogResponse, response_model_exclude_unset=True
)
def game_log(game_id: str, uid: str | None = None, since: int = 0) -> dict[str, Any]:
    """The action log.

    Unlike `/api/games/{game_id}` the *snapshots* default to full: a bare
    call (no uid, or an unrecognised one) is treated as the debugger's eye
    that `audit.py` already documents its snapshots for. A real seat uid
    instead gets its own filtered view, because this is also the log
    mcp.py's `game_log` tool hands a live player - and that must not read
    the opponent's hand any more than the state endpoint does.

    `legal_actions_before` is the one thing no unauthenticated read ever
    gets, however full the rest is: it is the same hidden information
    `GET .../legal-actions` refuses to hand anyone but the named seat and
    the admin key, so it is dropped for every viewer except the seat it was
    enumerated for and `ADMIN` - see `visibility.redact_log_entry`.

    `since` is the last `seq` the caller already has; only newer entries
    come back, and `last_seq` says where to resume next time.

    **`since` is a read bound, not a filter.** This route used to load the
    whole ledger and drop everything at or below `since` in Python, which
    made a poll cost the entire game's history - every row a full
    `snapshot(game)` plus a `legal_actions_before` enumeration, 11,538 bytes
    of it on the 28-permanent board `tests/test_action_answer_size.py`
    measures - to hand back the one row that was new. It is now a `seq >
    since` read (`audit.entries_since`), so nothing is fetched to be
    discarded.

    `last_seq` is deliberately *not* computed from those rows: a caught-up
    poller matches none of them, and answering 0 (or its own `since` back)
    would either send it to the start of the game on its next tick or freeze
    its watermark. It comes from `audit.last_seq`, the index-served maximum,
    and is taken from the rows only when they are newer still - a row
    appended between the two reads is returned *and* counted, so a client
    never resumes from a watermark past an entry it was not given.
    """
    game_or_404(game_id)  # 404 for an unknown game, like every other endpoint
    viewer = visibility.resolve_viewer(game_id, uid)
    last_seq = audit.last_seq(game_id)
    log = audit.entries_since(game_id, since) if last_seq > since else []
    if log:
        last_seq = log[-1]["seq"]
    if viewer is not visibility.ADMIN:
        # Every non-admin viewer, a `uid`-less one included - not only a real
        # seat. `redact_log_entry` applies the seat-shaped snapshot filtering
        # only for a real seat uid (so the debugger's eye below is unchanged),
        # but it drops `legal_actions_before` for anyone who is not the seat it
        # was enumerated for. That field is hidden information of exactly the
        # kind `GET .../legal-actions` refuses to hand out, and a caller who
        # presents nothing must never see more than one who presents garbage.
        log = [visibility.redact_log_entry(entry, viewer) for entry in log]
    return {"log": log, "last_seq": last_seq}


def _gate_entries(archived: ArchivedGame) -> list[dict[str, Any]] | None:
    """The ledger `_post_game` needs to answer this game, or `None` when it
    can be answered without one.

    Reading the ledger is not free - every row carries a full snapshot, and a
    long game's is megabytes - so it is loaded for exactly one case: a board
    this build cannot decode, on a game nothing has ended, where the
    `game_over` row is the only remaining witness. A decodable board answers
    from `Game.is_over` and an ended game from its own stamp, both as before.
    `game_review` hands whatever this returns straight on to
    `review.build_review`, so the gate never costs a second read.
    """
    if archived.unloadable is None or archived.deleted_at:
        return None
    return audit.entries(archived.game_id)


def _post_game(archived: ArchivedGame, entries: list[dict[str, Any]] | None) -> bool:
    """Whether this game is over (CR 104.1 - looked up in session: "A game
    ends immediately when a player wins, when the game is a draw, or when the
    game is restarted") or has been ended, which is the gate both
    `game_review` and `declare_reflection` sit behind.

    Unchanged for a game whose board this build can read: `Game.is_over`, or
    the soft delete's own stamp. For one it cannot, `is_over` is a fact about
    a board nobody here can look at, so the ledger's `game_over` row - written
    when it happened, by the build that could read the board - stands in as
    the witness. Its *absence* is not evidence of the opposite: a game with
    no such row and no `deleted_at` is refused, which is the same answer this
    gate gave before and the only one that cannot leak a live game's plans.
    """
    if archived.deleted_at:
        return True
    if archived.game is not None:
        return archived.game.is_over
    return review.ending_of(entries or []) is not None


@app.get(
    "/api/games/{game_id}/review",
    response_model=GameReviewResponse,
    response_model_exclude_unset=True,
)
def game_review(game_id: str, uid: str | None = None, key: str | None = None) -> dict[str, Any]:
    """The post-game review: every declaration this game's seats made,
    paired with what actually happened next, over the whole ledger.

    **Deliberately not `game_or_404`.** That call is exactly what stranded
    the live deployment's only substantively played game: `records` carries
    no foreign key and its rows outlive the game they describe, but
    `GET .../log` checks the game exists before it ever reads them, so 144
    rows became unreachable the moment the game was ended. This route reads
    `store.archived`, which sees past the soft delete, and 404s only when no
    such game was ever created.

    **Never open, and never mid-game.** Two gates, in this order:

    - the caller must hold this game's admin key or one of its seat uids
      (either may be passed as `uid` or as `key`; both are checked against
      the credentials the *archived* row kept, so they still work after the
      game has ended). Anything else is a 404 - the same answer `DELETE
      /api/games/{id}` gives, and one that does not confirm which uids
      exist. This is not `visibility.resolve_viewer`: that path treats "no
      uid" as a viewer in its own right, which is right for a debugger's
      view of a live board and wrong for a credential check.
    - the game must be over (`Game.is_over` - CR 104.1, "A game ends
      immediately when a player wins, when the game is a draw, or when the
      game is restarted") or ended (`archived.deleted_at`). Otherwise 409.
      A review shows both seats' plans and reasoning side by side, which is
      the entire point after a game and would be handing one seat the
      other's plan during one. Refusing outright is the only version of this
      that cannot leak: a redacted mid-game review would be a second,
      subtler copy of the same rule to keep correct. See `_post_game` for
      what stands in for `Game.is_over` when the board cannot be decoded -
      and note that neither gate is loosened by that case: an undecodable
      game with nothing recording an ending is refused exactly as a live one
      is.

    **Never 409 for a snapshot this build cannot decode.** That is what
    stranded a 191-action game the day the engine's schema moved: the whole
    review is built from the ledger's own frames, none of which needs the
    engine to decode anything, so it is rebuilt from them and marked
    `reconstructed` rather than refused. The engine's refusal is untouched -
    it rides along in `unloadable`, and no `Game` is decoded from that
    snapshot anywhere.

    A seat and the admin key see the same thing here. Post-game, so does
    everyone else who could be shown it - there is no hidden information
    left to protect and pretending otherwise would make the review useless
    to the person it is for.
    """
    archived = store.archived(game_id)
    if archived is None:
        raise KeyError(f"no game {game_id}")
    presented = {value for value in (uid, key) if value}
    allowed = bool(presented) and (
        (archived.admin_key is not None and archived.admin_key in presented)
        or bool(presented & set(archived.seat_uids))
    )
    if not allowed:
        raise KeyError("that uid is no seat in this game")
    entries = _gate_entries(archived)
    if not _post_game(archived, entries):
        raise HTTPException(
            status_code=409,
            detail=(
                "this game is still in progress - a review shows both seats' "
                "plans and reasoning, so it is only readable once the game is "
                "over or has been ended"
            ),
        )
    return review.build_review(archived, entries if entries is not None else audit.entries(game_id))


@app.delete(
    "/api/games/{game_id}", response_model=DeleteGameResponse, response_model_exclude_unset=True
)
def delete_game(game_id: str, uid: str | None = None, key: str | None = None) -> dict[str, Any]:
    """Delete a game outright. `uid` must be a seat uid of this game, or `key`
    this game's admin key - the same guard `decks_api.end_lobby_game` uses,
    mirrored here because this route deletes *any* game (not only a
    lobby-booted one) and `GET /api/ui/games` publishes every lobby-booted
    game's id with no credential required to read the list itself; without
    this check, reading the list would be enough to delete a game nobody
    handed the caller a seat or an admin key for."""
    game = store.get(game_id)
    if game is None:
        raise KeyError(f"no game {game_id}")
    allowed = (uid is not None and store.player_for_uid(game_id, uid) is not None) or (
        key is not None and store.is_admin(game_id, key)
    )
    if not allowed:
        raise KeyError("that uid is no seat in this game")
    audit.record(game_id, "delete_game")  # before the game leaves the store
    store.delete(game_id)
    return {"deleted": game_id}


# -- cards ------------------------------------------------------------------


@app.get("/api/card-names", response_model=CardNamesResponse, response_model_exclude_unset=True)
def card_names(q: str = "") -> dict[str, Any]:
    return {"suggestions": scryfall.autocomplete(q)}


def _proxied_lookup(name: str) -> dict[str, Any]:
    """`scryfall.lookup_card`, with `images` rewritten to this api's own
    asset-proxy URLs (`assets.proxy_image_links`) before it reaches the
    browser - this route is a direct browser call (`ui/render/review.js`'s
    `loadCatalogue`), so a raw `images["normal"]` here would be exactly the
    direct-to-Scryfall request the proxy exists to remove."""
    result = scryfall.lookup_card(name)
    if result.get("images"):
        result["images"] = assets.proxy_image_links(result.get("name") or name, result["images"])
    return result


@app.get("/api/cards", response_model=CardLookupResponse, response_model_exclude_unset=True)
def lookup_card_query(name: str) -> dict[str, Any]:
    return _proxied_lookup(name)


@app.get("/api/cards/{name}", response_model=CardLookupResponse, response_model_exclude_unset=True)
def lookup_card(name: str) -> dict[str, Any]:
    return _proxied_lookup(name)


# -- setting the board up ---------------------------------------------------


@app.post(
    "/api/games/{game_id}/cards", response_model=PutCardResponse, response_model_exclude_unset=True
)
def put_card(game_id: str, req: PutCard) -> dict[str, Any]:
    with store.locked(game_id) as game:
        game.player(req.player)  # 404 when unknown
        zone = Zone(req.zone)
        raw = scryfall.fetch_card(req.card)
        try:
            card = Card.model_validate(raw)
        except Exception as exc:
            raise ValueError(f"card data for {req.card} does not fit the engine: {exc}") from exc

        if zone in engine_api.SHARED_ZONES:
            state = CardState(card=card, zone=zone, controller=req.player, owner=req.player)
            engine_api.put_card_in_zone(game, state, req.player, zone, req.position)
        else:
            state = CardState(card=card, controller=req.player)
            engine_api.put_card_in_zone(game, state, req.player, zone, req.position)
        state.tapped = req.tapped
        state.face_up = req.face_up

        modern = scryfall.modern_status(raw)
        audit.record(
            game_id,
            "add_card",
            player=req.player,
            card=card.name,
            card_id=str(state.id),
            zone=req.zone,
        )
        view = state_view(game, game_id)
        view["added"] = {
            "card_id": str(state.id),
            "name": card.name,
            "modern_status": modern,
            "warning": None if modern == "legal" else f"{card.name} is {modern} in Modern",
        }
        return view


@app.post(
    "/api/games/{game_id}/cards/{card_id}/move",
    response_model=ActionOutcome | MoveCardSuccessResponse,
    response_model_exclude_unset=True,
)
def move_card(game_id: str, card_id: UUID, req: MoveCard) -> dict[str, Any]:
    with store.locked(game_id) as game:
        state = card_or_404(game, card_id)
        outcome = engine_api.move_card(
            game,
            state,
            Zone(req.to_zone),
            req.player,
            req.controller,
            req.position,
            req.tapped,
            req.face_up,
        )
        if not outcome.allowed:
            return audit_outcome(
                game_id, "move_card", outcome, card_id=str(card_id), to_zone=req.to_zone
            )
        audit_outcome(
            game_id,
            "move_card",
            outcome,
            card_id=str(card_id),
            card=state.card.name,
            to_zone=req.to_zone,
            player=req.player,
            controller=req.controller,
        )
        return {**state_view(game, game_id), **outcome.body()}


@app.post(
    "/api/games/{game_id}/cards/{card_id}/tap",
    response_model=TapCardResponse,
    response_model_exclude_unset=True,
)
def tap_card(game_id: str, card_id: UUID, req: dict[str, bool]) -> dict[str, Any]:
    with store.locked(game_id) as game:
        state = card_or_404(game, card_id)
        tapped = req.get("tapped", True)
        keys = dict(card_id=str(card_id), card=state.card.name, tapped=tapped)
        try:
            if tapped:
                state.tap()
            else:
                state.untap(exert_applies=False)  # a setup untap is not the untap step
        except ValueError as exc:
            return audit_outcome(game_id, "tap", engine_api.refused(exc), **keys)
        outcome = engine_api.Outcome(True, detail={"tapped": state.tapped})
        return audit_outcome(game_id, "tap", outcome, **keys)


@app.patch(
    "/api/games/{game_id}/cards/{card_id}",
    response_model=PatchCardResponse,
    response_model_exclude_unset=True,
)
def patch_card(game_id: str, card_id: UUID, req: PatchCard) -> dict[str, Any]:
    with store.locked(game_id) as game:
        state = card_or_404(game, card_id)
        if req.face_up is not None:
            state.face_up = req.face_up
        if req.damage is not None:
            state.damage = req.damage
        if req.summoning_sick is not None:
            state.summoning_sick = req.summoning_sick
        if req.counters:
            for kind, amount in req.counters.items():
                state.counters.set(engine_api.parse_counter_kind(kind), amount)
        changed = {key: value for key, value in req.model_dump().items() if value is not None}
        audit.record(game_id, "patch_card", card_id=str(card_id), card=state.card.name, **changed)
        view = state_view(game, game_id)
        view["patched"] = str(state.id)
        return view


@app.patch(
    "/api/games/{game_id}/players/{username}",
    response_model=GameStateView,
    response_model_exclude_unset=True,
)
def patch_player(game_id: str, username: str, req: PatchPlayer) -> dict[str, Any]:
    with store.locked(game_id) as game:
        player = game.player(username)
        if req.life is not None:
            player.life = req.life
        if req.counters:
            for kind, amount in req.counters.items():
                player.counters.set(engine_api.parse_counter_kind(kind), amount)
        changed = {key: value for key, value in req.model_dump().items() if value is not None}
        audit.record(game_id, "patch_player", player=username, **changed)
        return state_view(game, game_id)


@app.put(
    "/api/games/{game_id}/players/{username}/mana-pool",
    response_model=GameStateView,
    response_model_exclude_unset=True,
)
def set_mana_pool(game_id: str, username: str, req: ManaPoolUpdate) -> dict[str, Any]:
    with store.locked(game_id) as game:
        player = game.player(username)
        pool = player.mana_pool

        def colors(mapping: dict[str, int]) -> dict[Any, int]:
            try:
                return {Color(k.strip().upper()): v for k, v in mapping.items()}
            except ValueError as exc:
                raise ValueError("unknown mana color: use W, U, B, R, G or C") from exc

        if req.empty:
            pool.empty()
        if req.set is not None:
            pool.floating = colors(req.set)
        if req.add:
            for color, amount in colors(req.add).items():
                pool.add(color, amount)
        audit.record(
            game_id, "mana_pool", player=username, empty=req.empty, add=req.add, set=req.set
        )
        return state_view(game, game_id)


@app.put(
    "/api/games/{game_id}/step", response_model=SetStepResponse, response_model_exclude_unset=True
)
def set_step(game_id: str, req: StepUpdate, uid: str | None = None) -> dict[str, Any]:
    """Jump the turn marker to a named step. The harness's own way to stage a
    board, and deliberately NOT a seat verb: no rule puts the turn marker
    anywhere, so there is no MCP tool and no `seat_cli` subcommand for this.

    Gated all the same (T10): a caller that names an agent seat owes the same
    stated reason it owes for an attack, even though this only moves the
    turn marker (`Game.advance_to`) and runs none of `Game.advance`'s own
    beginning-of-step turn-based actions or triggers - CR 702.62a's suspend
    countdown among them, the same as a draw or a combat declaration.
    """
    records.require_current_board(
        game_id, "step", req.expected_seq, player=req.player, reasoning=req.reasoning
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    with store.locked(game_id) as game:
        before = game.turn.step.value if game.turn is not None else None
        # Read before the marker moves, like every other route's own
        # `legal_actions_before` - see `records.py`'s module docstring.
        legal_before = records.legal_actions_before(
            game, req.player, (store.paused_command(game_id) or (None, None, None))[2]
        )
        engine_api.set_step(game, Step(req.step))
        audit.record(
            game_id,
            "step",
            player=req.player,
            reasoning=req.reasoning,
            legal_actions_before=legal_before,
            **{"from": before, "to": req.step},
        )
        viewer = visibility.resolve_viewer(game_id, uid)
        return visibility.redact_view(state_view(game, game_id), viewer)


# -- the heart: legality questions ------------------------------------------


@app.post(
    "/api/games/{game_id}/actions/cast",
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_cast(game_id: str, req: CastRequest) -> dict[str, Any]:
    """Cast a card. `dry_run` never asks anything unscripted (it builds and
    rewinds its `Casting` against an isolated probe copy of the game - see
    `engine_api.dry_run_copy` - never through the pause/resume tape), so it
    always completes; a real cast can pause on any CR 601.2 choice this
    engine still asks interactively (`choose_targets`, `choose_a_mode`, ...)."""
    records.require_current_board(
        game_id,
        "cast",
        req.expected_seq,
        player=req.player,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    result, _, legal_before, seq = _run_command(game_id, "cast", req)
    return _respond(
        game_id,
        "cast",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card=req.card or str(req.card_id),
        from_zone=req.from_zone,
        targets=req.targets,
        mana_sources=[str(source) for source in req.mana_sources or []],
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        seq_hint=seq,
    )


@app.post(
    "/api/games/{game_id}/actions/suspend",
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_suspend(game_id: str, req: SpecialActionRequest) -> dict[str, Any]:
    """CR 116.2f/702.62a: exile a card with suspend from hand, paying its
    suspend cost, with N time counters. The free cast once the last counter
    comes off is picked up automatically by `actions/cast` - see `advance`."""
    records.require_current_board(
        game_id,
        "suspend",
        req.expected_seq,
        player=req.player,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    result, _, legal_before, seq = _run_command(game_id, "suspend", req)
    return _respond(
        game_id,
        "suspend",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card=req.card or str(req.card_id),
        mana_sources=[str(source) for source in req.mana_sources or []],
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        seq_hint=seq,
    )


@app.post(
    "/api/games/{game_id}/actions/plot",
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_plot(game_id: str, req: SpecialActionRequest) -> dict[str, Any]:
    """CR 116.2k/702.170b: exile a card with plot from hand, paying its plot
    cost. Cast it later with `actions/cast`, `keyword: "Plot"`, on a later turn."""
    records.require_current_board(
        game_id,
        "plot",
        req.expected_seq,
        player=req.player,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    result, _, legal_before, seq = _run_command(game_id, "plot", req)
    return _respond(
        game_id,
        "plot",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card=req.card or str(req.card_id),
        mana_sources=[str(source) for source in req.mana_sources or []],
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        seq_hint=seq,
    )


@app.post(
    "/api/games/{game_id}/actions/foretell",
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_foretell(game_id: str, req: SpecialActionRequest) -> dict[str, Any]:
    """CR 116.2h/702.143b: pay {2} and exile a card with foretell from hand,
    face down. Cast it later with `actions/cast`, `keyword: "Foretell"`, on a
    later turn, for its printed foretell cost."""
    records.require_current_board(
        game_id,
        "foretell",
        req.expected_seq,
        player=req.player,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    result, _, legal_before, seq = _run_command(game_id, "foretell", req)
    return _respond(
        game_id,
        "foretell",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card=req.card or str(req.card_id),
        mana_sources=[str(source) for source in req.mana_sources or []],
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        seq_hint=seq,
    )


@app.post(
    "/api/games/{game_id}/actions/cycle",
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_cycle(game_id: str, req: CycleRequest) -> dict[str, Any]:
    """CR 702.29a: discard a card with cycling from hand, paying its cycling
    cost, to draw a card (or search for a typecycled one). Puts the ability on
    the stack, same as `actions/activate` - resolve it with `resolve-top`."""
    records.require_current_board(
        game_id,
        "cycle",
        req.expected_seq,
        player=req.player,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    result, _, legal_before, seq = _run_command(game_id, "cycle", req)
    return _respond(
        game_id,
        "cycle",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card=req.card or str(req.card_id),
        ability_index=req.ability_index,
        mana_sources=[str(source) for source in req.mana_sources or []],
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        seq_hint=seq,
    )


@app.post(
    "/api/games/{game_id}/actions/target",
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_target(game_id: str, req: TargetRequest) -> dict[str, Any]:
    game = game_or_404(game_id)
    outcome = engine_api.check_target(game, req)
    return audit_outcome(
        game_id,
        "target",
        outcome,
        source_id=str(req.source_id),
        target=req.target,
        ability_index=req.ability_index,
        mode_index=req.mode_index,
    )


@app.get(
    "/api/games/{game_id}/affordances/{card_id}",
    response_model=AffordancesResponse,
    response_model_exclude_unset=True,
)
def get_affordances(
    game_id: str,
    card_id: UUID,
    player: str,
    ability_index: int | None = None,
    modes: str | None = None,
    x_value: int | None = None,
) -> dict[str, Any]:
    """Which objects this announcement could legally target right now.

    The expensive half of `affordances.py` - it walks every object in the
    public zones against every requirement - so it is asked for on demand,
    when the player picks a card up, rather than folded into the 1.2s state
    poll for the whole board. The cheap half (does it target, how many, is
    there an X, is it modal) already rides along on each card view.

    `modes` is a comma-separated list of 1-based mode numbers: a modal
    spell's targeting is unanswerable until the modes are chosen (CR 700.2f),
    so the UI asks again with them once the player has picked.

    Nothing here is hidden information: every candidate is an object in a
    public zone (`affordances.CANDIDATE_ZONES`) or a player, so this needs no
    `visibility.redact_view` pass of its own.
    """
    game = game_or_404(game_id)
    try:
        state = affordances.find_source(game, card_id)
    except ValueError as exc:
        raise KeyError(str(exc)) from exc
    mode_numbers = [int(part) for part in modes.split(",") if part.strip()] if modes else None
    result = affordances.candidates(
        game,
        state,
        player,
        ability_index=ability_index,
        modes=mode_numbers,
        x_value=x_value,
    )
    if ability_index is None:
        # CR 118.9's alternative costs, for a cast only - an ability has no
        # such thing. Included here rather than on the state view because
        # discovering one means building a `Casting`, which is per-card work
        # not worth doing for every card on the board on every poll.
        result["alternative_costs"] = affordances.alternative_costs(game, state, player)
    # Convoke, delve and crew: which objects could be spent on this card's
    # cost. Absent keys mean the card offers none of them.
    object_costs = affordances.object_costs(game, state, player)
    if object_costs:
        result["object_costs"] = object_costs
    return result


def _attach_target_candidates(
    game: Game, player: str, actions: Sequence[Any], views: list[dict[str, Any]]
) -> None:
    """Fold each targeting action's legal candidates into its own entry.

    `Action.target_slots` is the *shape* of a targeting requirement and never
    its candidates - the engine's own `TargetSlot` docstring says so and
    points at `Game.legal_targets` for the objects. That left a seat holding a
    legal, affordable, targeting action with no idea what it could name, one
    round trip - and so one whole inference cycle, which is what a seat's turn
    latency is actually made of - short of being able to act. The candidates
    are computed here, in the request that already enumerated the actions,
    rather than in a second call per action.

    **One board walk for the whole request.** `affordances.all_targetables` is
    hoisted before the loop and handed to every `target_candidates` call, so
    the cost of adding this is one traversal of the public zones plus the
    `rejects` predicate per (object, requirement) pair - never one traversal
    per action, which on a busy board with a hand full of removal would be the
    N-calls shape T11 removed one layer up, reintroduced one layer down.
    `tests/test_legal_actions_endpoint.py` pins both the single walk and the
    time budget.

    Two kinds of row are skipped, and each for its own reason:

    - **nothing to enumerate.** An empty `target_slots` means this action does
      not target (or its phrase was unreadable, which `Action` cannot tell
      apart - `affordances`' own module docstring), so there is no slot to
      fill and no key worth the bytes.
    - **`affordable == "no"`.** CR 118.6 keeps the row - an unpayable cast is
      still a legal one and is still listed - but a candidate list for a spell
      that cannot be cast is a list the seat can do nothing with. Said once,
      in `seatkit._actions_view`'s envelope, rather than as a key on every
      such row.

    `unknown` is emphatically not `no` and is enriched: an X spell before its
    X is announced (CR 107.3a) may well be castable, and a seat weighing it
    needs to know what it could point at.
    """
    targetables: list[Any] | None = None
    for action, view in zip(actions, views, strict=True):
        if not action.target_slots or view.get("affordable") == "no":
            continue
        if action.card_id is None:
            continue
        try:
            source = affordances.find_source(game, action.card_id)
        except ValueError:
            # An action naming an object this game no longer holds is the
            # enumeration's problem to answer for, not this decoration's to
            # raise on: the row stays, without candidates.
            continue
        if targetables is None:
            targetables = affordances.all_targetables(game)
        view["targets"] = affordances.target_candidates(
            game,
            source,
            player,
            ability_index=action.ability_index,
            targetables=targetables,
        )


@app.get(
    "/api/games/{game_id}/legal-actions",
    response_model=LegalActionsResponse,
    response_model_exclude_unset=True,
)
def get_legal_actions(
    game_id: str, player: str, uid: str | None = None, candidates: bool = False
) -> dict[str, Any]:
    """Every action `player` could legally take right now
    (`machines.legal_actions.legal_actions`, Phase 1) - the api's own door
    onto the engine's enumeration surface, and what `seatkit.available_actions`
    calls instead of brute-forcing one HTTP `dry_run` per candidate (T11).

    Each entry carries the engine's own `affordable` verdict
    (`"yes"`/`"no"`/`"unknown"`) alongside its legality - CR 118.6: an
    unpayable cast is still a legal one, so it is still listed, never dropped.
    `"unknown"` is a real, distinct answer (a modal or X spell before its
    parameters are announced) and is reported as such rather than folded into
    `"no"`, which is the whole reason this replaces the old probe: a dry run
    can only ever say yes or no, never "I cannot tell yet".

    Reads whichever decision is currently paused for this game straight off
    the repository's own paused-command slot (`store.paused_command`, the
    same source `serialize.state_view`'s own `pending_decision` reads from)
    and hands it to `legal_actions` as `pending` - when one is outstanding,
    the answer is that decision's own legal answers and nothing else, for
    the player it addresses only (`legal_actions`'s own module docstring).

    **`uid` must open `player`'s own seat, or be this game's admin key, or
    be an advisor on that seat** (`repository.Advisor` - it resolves to the
    same username through `visibility.resolve_viewer`, and an advisor is
    allowed to know exactly what its human is allowed to know).
    Every action here is `player`'s own by construction - hand cards,
    controlled permanents, that player's own graveyard/exile cast options -
    and that is exactly why this is hidden information rather than why it is
    not: the answer names each card in the seat's hand by id, its exact cost,
    the zone it would be cast from, what it would target, and whether that
    seat can pay for it. `player` is a bare query parameter with nothing
    authenticating it, so without this check naming the opponent was enough
    to read their hand out of the answer - while `GET /api/games/{game_id}`,
    one route up, correctly hands the same caller `hands.<opponent> == []`.
    `visibility.py` is the rule that binds both: "a caller that presents
    nothing must never see more than one that presents garbage, and neither
    may see more than one that presents someone else's seat".

    **`candidates=true` folds each targeting action's legal targets into its
    own entry** (`_attach_target_candidates`), so a seat can choose AND issue
    its next action out of one answer. Opt-in, and measured rather than
    guessed: on a 28-permanent board with three targeting spells in hand the
    enumeration costs ~106 ms on top of the ~350 ms the enumeration itself
    already takes, because `TargetRequirement.rejects` reaches
    `Game.characteristics_of` and `Game.computed_board` is deliberately
    uncached in the engine (CR 613.5 - "continually and automatically
    performed", so an anthem that left the battlefield a moment ago is
    already not applying). That is a good trade for a seat, which pays it
    once per action and saves a whole round trip - and so a whole inference
    cycle, seconds rather than milliseconds - and a bad one for the browser,
    which asks this on a 1.2s poll and already has the whole board on screen
    to pick targets from. So `seatkit.available_actions` passes it and
    `ui/app.js` does not, and the default is off: a caller that says nothing
    pays nothing.

    `get_affordances` needs no such guard and is not a precedent for
    skipping one: every candidate it names is an object in a public zone
    (CR 400.2) or a player, so there is nothing hidden in its answer to
    begin with.

    Refused rather than answered empty, and this is the deliberate half of
    the choice: `{"actions": []}` is a real answer this endpoint gives (a
    seat has nothing legal while a decision addressed to the *other* player
    stands - see `legal_actions`'s own docstring), so redacting to an empty
    list would make "you may not ask that" indistinguishable from "there is
    nothing you can do", which is precisely the ambiguity
    `seatkit.available_actions` must be able to tell apart. The refusal is
    the api's own existing shape for a credential that does not open what it
    names - `ValueError` -> 400, the same as `pregame_settle`'s admin-key
    check - rather than a 403, which appears nowhere else in this api.
    """
    game = game_or_404(game_id)
    viewer = visibility.resolve_viewer(game_id, uid)
    if viewer is not visibility.ADMIN and viewer != player:
        raise ValueError(
            f"that uid does not open {player}'s seat - these are that player's own "
            "legal actions, and naming them is not enough to read them"
        )
    paused = store.paused_command(game_id)
    pending = paused[2] if paused is not None else None
    actions = compute_legal_actions(game, player, pending=pending)
    views = [action.model_dump(mode="json") for action in actions]
    if candidates:
        _attach_target_candidates(game, player, actions, views)
    return {"actions": views}


@app.post(
    "/api/games/{game_id}/actions/concede",
    response_model=ConcedeResponse,
    response_model_exclude_unset=True,
)
def action_concede(game_id: str, req: ConcedeRequest) -> dict[str, Any]:
    """Concede (CR 104.3a). A player may do this at any time, and leaves the
    game immediately - there is no priority check and nothing to refuse, which
    is why this takes no `Outcome` the way the other actions do. `Game.concede`
    asks nothing of its own, but this still goes through the pause machinery
    like every other command, on the chance a state-based check it triggers
    ever does (a legend rule, a leaves-the-battlefield trigger's targets)."""
    # No `records.require_plan` here, and this is the one route where that is
    # a rule rather than an oversight: CR 104.3a lets a player concede at any
    # time, so a gate that could refuse a concession would be a gate that can
    # trap a seat in a game it has already given up.
    records.require_current_board(
        game_id, "concede", req.expected_seq, player=req.player, reasoning=req.reasoning
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    result, _, legal_before, seq = _run_command(game_id, "concede", req)
    if result.status is commands.RunStatus.PAUSED:
        assert result.pending is not None
        pending = decisions.PendingDecision.from_engine(
            result.pending.decision, store.pause_generation(game_id)
        )
        logged = audit.record(
            game_id,
            "concede",
            player=req.player,
            allowed=None,
            paused=True,
            paused_on={"player": pending.player, "kind": pending.kind},
            reasoning=req.reasoning,
            legal_actions_before=legal_before,
        )
        return {
            "allowed": None,
            "paused": True,
            "pending_decision": pending.to_view(),
            "log_seq": logged,
        }
    assert isinstance(result.outcome, dict)
    logged = audit.record(
        game_id,
        "concede",
        player=req.player,
        allowed=True,
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        _fill_seq=seq,
    )
    return {"allowed": True, "winner": result.outcome["winner"], "log_seq": logged}


@app.post(
    "/api/games/{game_id}/actions/activate",
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_activate(game_id: str, req: ActivateRequest) -> dict[str, Any]:
    records.require_current_board(
        game_id,
        "activate",
        req.expected_seq,
        player=req.player,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    result, _, legal_before, seq = _run_command(game_id, "activate_ability", req)
    return _respond(
        game_id,
        "activate",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card_id=str(req.card_id),
        ability_index=req.ability_index,
        targets=req.targets,
        mana_sources=[str(source) for source in req.mana_sources or []],
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        seq_hint=seq,
    )


@app.post(
    "/api/games/{game_id}/actions/crew",
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_crew(game_id: str, req: CrewRequest) -> dict[str, Any]:
    """Crew a Vehicle (CR 702.122a). Its own endpoint rather than an
    `activate` against an ability index: the cost is the creatures tapped,
    not mana, and crew is the Vehicle's ability rather than theirs."""
    records.require_current_board(
        game_id,
        "crew",
        req.expected_seq,
        player=req.player,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    result, _, legal_before, seq = _run_command(game_id, "crew", req)
    return _respond(
        game_id,
        "crew",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card_id=str(req.card_id),
        crewing=[str(card_id) for card_id in req.crewing],
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        seq_hint=seq,
    )


def _answer_declaration(
    game_id: str,
    expected_kind: DecisionKind,
    pairs: list[DeclaredPairRequest],
    *,
    action: str,
    uid: str | None = None,
    reasoning: str | None = None,
    **audit_keys: Any,
) -> dict[str, Any]:
    """The shared body of `actions/attack`/`actions/declare-blockers`: both
    are thin aliases over the standing `DECLARE_ATTACKERS`/`DECLARE_BLOCKERS`
    decision `POST .../advance` paused on, built from each route's own
    request shape into `decisions.declared_pairs_answer`'s pair shape and
    resumed exactly the way `POST .../decide` resumes any other kind
    (`_resume`) - not a second transport around the tape, which is what
    these two routes used to be (calling `Game.declare_attackers`/
    `declare_blockers` directly, bypassing `ask_attackers`/`ask_blockers`
    entirely). See this task's own report for the full reasoning.

    Refuses (`allowed: False`, 200) rather than raising when there is no
    such decision pending, or the pending one is a different kind (the
    caller is at the wrong step) - the same shape every other refusal in
    this api takes, and it leaves the pending decision untouched either way.

    `uid` gates exactly the way `decide`'s own does (CR 608.2d's "the player
    announces"): it must open the *deciding* player's own seat, not the
    admin key and not anyone else's - `visibility.resolve_viewer` returns
    the `ADMIN` sentinel for the admin key, which is not a `str` and so
    never satisfies this either. These two routes are aliases over the same
    paused tape `decide` resumes (not a second transport around it - see
    above), so the two doors have to agree on who may walk through; before
    this they did not - `decide` enforced this and `actions/attack`/
    `actions/declare-blockers` enforced nothing at all.
    """

    def refuse(
        reason: str, legal_before: records.LegalActionsBefore | None = None
    ) -> dict[str, Any]:
        seq = audit.record(
            game_id,
            action,
            allowed=False,
            reason=reason,
            reasoning=reasoning,
            legal_actions_before=legal_before,
            **audit_keys,
        )
        return {"allowed": False, "reason": reason, "log_seq": seq}

    game = game_or_404(game_id)
    paused = store.paused_command(game_id)
    if paused is None:
        return refuse(f"no pending {expected_kind.value} decision - call POST .../advance first")
    command, tape, pending = paused
    engine_decision = pending.decision
    # `AttackRequest` carries no `player` field at all - only
    # `DeclareBlockersRequest` does, and passes it into `audit_keys` itself
    # (see `action_declare_blockers`) - so `attack`'s rows were missing
    # `detail["player"]` outright, and a review frame for one could not say
    # whose declaration it was. `setdefault` fills it in from the engine's
    # own resolved deciding seat - the identity `uid` is about to be checked
    # against, below - without overriding whatever a caller (declare_
    # blockers) already supplied.
    audit_keys.setdefault("player", engine_decision.player)
    if engine_decision.kind is not expected_kind:
        return refuse(
            f"the pending decision is {engine_decision.kind.value!r}, not "
            f"{expected_kind.value!r} - answer that one first (POST .../decide, "
            f"or the matching action)"
        )
    # The acting seat, not the viewing one - see `decide` above and
    # `game_access.seat_for_uid`. The admin key is refused here as it always
    # was (it opens no seat, and CR 608.2d's "the player announces" is about
    # a player), and an advisor uid now is too.
    viewer = game_access.seat_for_uid(game_id, uid)
    if viewer != engine_decision.player:
        # No enumeration on this branch, deliberately: the caller has just
        # been told this is not their decision, and "what else the deciding
        # seat could have done" is that seat's own hidden information.
        # Computing it here at all is how it first leaked - the row was
        # attributed to `detail["player"]`, straight off the request body,
        # so a refused caller who named themselves read it back out of the
        # log.
        return refuse("that uid does not open the deciding player's seat")
    records.require_reasoning(game_id, engine_decision.player, reasoning)
    # Same subject as the reasoning gate: the seat the server resolved, never
    # `req.player`. `require_plan` self-exempts when this is not that seat's
    # own turn, so a blocker declaration on the opponent's turn passes
    # straight through it - see `records.require_plan`.
    records.require_plan(game_id, engine_decision.player, game=game)
    # T10: computed once the deciding seat is the *resolved* viewer, and
    # reused for every branch below (including a later refusal) - always the
    # *pre*-mutation enumeration, since nothing here has touched `game` yet.
    legal_before = records.legal_actions_before(game, engine_decision.player, pending)
    try:
        value = decisions.declared_pairs_answer(pairs)
        decisions.validate_answer(engine_decision, value)
    except ValueError as exc:
        return refuse(str(exc), legal_before)
    answer = Answer(
        seq=pending.seq, kind=engine_decision.kind, player=engine_decision.player, pairs=value
    )
    try:
        result, _, seq_hint = _resume(
            game_id, command, tape, pending, answer, player=engine_decision.player
        )
    except ValueError as exc:
        return refuse(str(exc), legal_before)
    seq = audit.record(
        game_id,
        action,
        allowed=True,
        reason=None,
        reasoning=reasoning,
        legal_actions_before=legal_before,
        _fill_seq=seq_hint,
        **audit_keys,
    )
    if result.status is commands.RunStatus.PAUSED:
        assert result.pending is not None
        next_pending = decisions.PendingDecision.from_engine(
            result.pending.decision, store.pause_generation(game_id)
        )
        return {
            "allowed": True,
            "paused": True,
            "pending_decision": next_pending.to_view(),
            "log_seq": seq,
        }
    return {"allowed": True, "log_seq": seq}


@app.post(
    "/api/games/{game_id}/actions/attack",
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_attack(game_id: str, req: AttackRequest, uid: str | None = None) -> dict[str, Any]:
    """Declare attackers (CR 508.1) - see `_answer_declaration`. `dry_run`
    answers without touching the pending decision at all: it only checks
    each pair's legality (`Game.why_cannot_attack`) against the game as it
    stands right now, the same probe it always was - so it needs no `uid`
    gate any more than any other dry run does; only a real declaration does.
    """
    # `AttackRequest` carries no `player` of its own (see `_answer_declaration`),
    # so the row this gate may write is attributed to the seat `uid` actually
    # opens - the same resolved identity `_answer_declaration` checks against
    # the deciding player, never a name off the request body.
    attacker_viewer = visibility.resolve_viewer(game_id, uid)
    records.require_current_board(
        game_id,
        "attack",
        req.expected_seq,
        player=attacker_viewer if isinstance(attacker_viewer, str) else None,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    attackers_detail = [
        {"card": str(pair.card_id), "defender": pair.defender} for pair in req.attackers
    ]
    if req.dry_run:
        game = game_or_404(game_id)
        outcome = engine_api.declare_attack(game, req)
        return audit_outcome(game_id, "attack", outcome, attackers=attackers_detail, dry_run=True)
    pairs = [
        DeclaredPairRequest(creature=pair.card_id, defender=pair.defender) for pair in req.attackers
    ]
    return _answer_declaration(
        game_id,
        DecisionKind.DECLARE_ATTACKERS,
        pairs,
        action="attack",
        uid=uid,
        reasoning=req.reasoning,
        attackers=attackers_detail,
        dry_run=False,
    )


@app.post(
    "/api/games/{game_id}/actions/play-land",
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_play_land(game_id: str, req: PlayLandRequest) -> dict[str, Any]:
    records.require_current_board(
        game_id,
        "play_land",
        req.expected_seq,
        player=req.player,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    result, _, legal_before, seq = _run_command(game_id, "play_land", req)
    return _respond(
        game_id,
        "play_land",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card=req.card or str(req.card_id),
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        seq_hint=seq,
    )


@app.post(
    "/api/games/{game_id}/actions/pass-priority",
    response_model=PassPriorityResponse,
    response_model_exclude_unset=True,
)
def action_pass_priority(game_id: str, req: PassPriorityRequest) -> dict[str, Any]:
    """Pass priority - "I am done for now". `outcome` tells the caller what
    the pass caused: `resolve_top` (the stack's top resolved, priority goes
    round again) or `end_step` (everyone passed on an empty stack, so the
    step ended and the next has already begun - CR 117.4, 405.5). Resolving
    the stack's top is exactly where a spell or ability's own CR 608.2d
    question is asked, so this can pause the same way `resolve-top` can.

    If that next step is one in which no player receives priority,
    `commands._run_pass_priority` does not stop there: it runs the game on to
    the first step somebody can act in (CR 500.3) as part of this same
    command, and `settled_through` lists the steps crossed. A player who has
    passed has said "I am done for now", and nothing in CR 500.3's untap or
    cleanup steps is waiting on them - so handing the pass back with
    `priority_holder: null` would be handing back a state no client, human or
    otherwise, is allowed to move."""
    # `pass_shaped=True`: this is the one action whose stated reason is
    # always the same sentence, and `seatkit.pass_until` posts to this very
    # route for every pass a run makes - see `records.require_reasoning`.
    # The field is still accepted and still audited below.
    records.require_current_board(
        game_id, "pass_priority", req.expected_seq, player=req.player, reasoning=req.reasoning
    )
    records.require_reasoning(game_id, req.player, req.reasoning, pass_shaped=True)
    # Exempt from `reasoning`, NOT from the plan: passing can perfectly well
    # be the first thing a seat does on its own turn, and "I had nothing to
    # do this turn" is a claim a reviewer should be able to check against a
    # plan that was declared before it.
    records.require_plan(game_id, req.player)
    try:
        result, _, legal_before, seq_hint = _run_command(game_id, "pass_priority", req)
    except ValueError as exc:  # not this player's decision to make
        # No `_run_command` attempt ever ran (the engine refused before
        # `commands.run_command` returned), so there is no `game` handed
        # back to read "what else was legal" off - fetched fresh here,
        # exactly as it stood the instant this request was refused.
        game = game_or_404(game_id)
        legal_before = records.legal_actions_before(
            game, req.player, (store.paused_command(game_id) or (None, None, None))[2]
        )
        seq = audit.record(
            game_id,
            "pass_priority",
            player=req.player,
            allowed=False,
            reason=str(exc),
            reasoning=req.reasoning,
            legal_actions_before=legal_before,
        )
        return {"allowed": False, "reason": str(exc), "outcome": None, "log_seq": seq}
    if result.status is commands.RunStatus.PAUSED:
        assert result.pending is not None
        pending = decisions.PendingDecision.from_engine(
            result.pending.decision, store.pause_generation(game_id)
        )
        seq = audit.record(
            game_id,
            "pass_priority",
            player=req.player,
            allowed=None,
            paused=True,
            paused_on={"player": pending.player, "kind": pending.kind},
            reasoning=req.reasoning,
            legal_actions_before=legal_before,
        )
        return {
            "allowed": None,
            "paused": True,
            "pending_decision": pending.to_view(),
            "outcome": None,
            "log_seq": seq,
        }
    assert isinstance(result.outcome, dict)
    outcome_detail = dict(result.outcome)
    priority_outcome: PriorityOutcome = outcome_detail.pop("outcome")
    settled = outcome_detail.pop("settled_through", None) or []
    audit_detail: dict[str, Any] = {"outcome": priority_outcome.value, **outcome_detail}
    if settled:
        audit_detail["settled_through"] = settled
    seq = audit.record(
        game_id,
        "pass_priority",
        player=req.player,
        allowed=True,
        reason=None,
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        _fill_seq=seq_hint,
        **audit_detail,
    )
    # Logged raw just above (`audit_detail["notices"]`, when this resolution
    # raised any); the response's own copy is redacted for `req.player` -
    # this route takes no `uid`, so that self-declared identity is the same
    # trust boundary `audit_outcome`'s own docstring explains. Omitted
    # entirely, like every other route, when there is nothing to attach -
    # see `commands._attach_notices`'s own docstring for why.
    body = {
        "allowed": True,
        "reason": None,
        "outcome": priority_outcome.value,
        "settled_through": settled,
        "log_seq": seq,
    }
    notices = outcome_detail.get("notices")
    if notices:
        body["notices"] = visibility.redact_notices(notices, req.player)
    return body


@app.post(
    "/api/games/{game_id}/actions/declare-blockers",
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_declare_blockers(
    game_id: str, req: DeclareBlockersRequest, uid: str | None = None
) -> dict[str, Any]:
    """Declare blockers (CR 509.1) - see `_answer_declaration`."""
    records.require_current_board(
        game_id, "declare_blockers", req.expected_seq, player=req.player, reasoning=req.reasoning
    )
    blocks_detail = [{"blocker": str(b.blocker), "blocked": str(b.blocked)} for b in req.blocks]
    pairs = [DeclaredPairRequest(creature=b.blocker, defender=str(b.blocked)) for b in req.blocks]
    return _answer_declaration(
        game_id,
        DecisionKind.DECLARE_BLOCKERS,
        pairs,
        action="declare_blockers",
        uid=uid,
        reasoning=req.reasoning,
        player=req.player,
        blocks=blocks_detail,
    )


# -- running the game forward ------------------------------------------------


@app.post(
    "/api/games/{game_id}/state-based-actions",
    response_model=StateBasedActionsResponse,
    response_model_exclude_unset=True,
)
def state_based_actions(game_id: str, req: ActorRequest | None = None) -> dict[str, Any]:
    """Check and perform this game's state-based actions right now.

    The body is optional and carries only who asked (`player`) and why
    (`reasoning`) - the same shape `resolve-top` takes. Nobody *takes* a
    state-based action, the game performs them; but somebody asked for this
    check, and once an agent seat can ask, it owes the same stated reason it
    owes for every other move it makes. Called with no body at all (the
    harness's own poke) there is no seat to hold to anything, and nothing is
    required.

    Before T10's audit this route took no body at all, which left two
    documents wrong about it - `README.md` said it took an optional
    `reasoning` and `MEMORY.md` said it was gated. Giving it the body they
    both described was cheaper, and more honest, than editing them to
    describe a hole.
    """
    req = req or ActorRequest()
    records.require_current_board(
        game_id, "state_based_actions", req.expected_seq, player=req.player, reasoning=req.reasoning
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    result, _, legal_before, seq_hint = _run_command(game_id, "state_based_actions", req)
    if result.status is commands.RunStatus.PAUSED:
        seq = audit.record(
            game_id,
            "state_based_actions",
            player=req.player,
            allowed=None,
            paused=True,
            paused_on=_paused_on(result),
            reasoning=req.reasoning,
            legal_actions_before=legal_before,
        )
        game = game_or_404(game_id)
        return {**state_view(game, game_id), "allowed": None, "paused": True, "log_seq": seq}
    assert result.game is not None and isinstance(result.outcome, dict)
    notices = result.outcome.get("notices")
    audit.record(
        game_id,
        "state_based_actions",
        player=req.player,
        destroyed=result.outcome["destroyed"],
        changed=result.outcome["changed"],
        **({"notices": notices} if notices else {}),
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        _fill_seq=seq_hint,
    )
    # Logged raw above (when there was anything to log); the response's own
    # copy is redacted for this route's caller (`req.player` - no `uid` is
    # taken here, the same trust boundary `audit_outcome`'s own docstring
    # explains for the routes that go through it instead of this one). Left
    # out of the response entirely when empty, like every other route - see
    # `commands._attach_notices`'s own docstring.
    outcome = result.outcome
    if notices:
        outcome = {**result.outcome, "notices": visibility.redact_notices(notices, req.player)}
    return {**state_view(result.game, game_id), **outcome}


@app.post(
    "/api/games/{game_id}/resolve-top",
    response_model=ResolveTopResponse,
    response_model_exclude_unset=True,
)
def resolve_top(
    game_id: str, req: ActorRequest | None = None, uid: str | None = None
) -> dict[str, Any]:
    if req is not None:
        records.require_current_board(
            game_id, "resolve_top", req.expected_seq, player=req.player, reasoning=req.reasoning
        )
        records.require_reasoning(game_id, req.player, req.reasoning)
        records.require_plan(game_id, req.player)
    result, _, legal_before, seq_hint = _run_command(game_id, "resolve_top", req or ActorRequest())
    viewer = visibility.resolve_viewer(game_id, uid)
    reasoning = req.reasoning if req else None
    # `req.player` alone left every real caller's row unattributed: nothing
    # resolving the stack's top ever sends a body naming one (`scripts.
    # generate_payload_fixtures`'s own calls carry `uid` and no body at
    # all), even though `uid` already named a real seat. `viewer` is the
    # same resolved identity `redact_view` below is about to key on - a
    # `str` only for a `uid` that actually opens a seat, never for the
    # admin key (`visibility.ADMIN`) or no `uid` at all - so this never
    # attributes a row to someone the caller did not actually identify.
    player = (req.player if req else None) or (viewer if isinstance(viewer, str) else None)
    if result.status is commands.RunStatus.PAUSED:
        seq = audit.record(
            game_id,
            "resolve_top",
            player=player,
            allowed=None,
            paused=True,
            paused_on=_paused_on(result),
            reasoning=reasoning,
            legal_actions_before=legal_before,
        )
        game = game_or_404(game_id)
        return {
            **visibility.redact_view(state_view(game, game_id), viewer),
            "resolved": None,
            "zone": None,
            "allowed": None,
            "paused": True,
            "log_seq": seq,
        }
    assert result.game is not None and isinstance(result.outcome, dict)
    seq = audit.record(
        game_id,
        "resolve_top",
        player=player,
        reasoning=reasoning,
        legal_actions_before=legal_before,
        _fill_seq=seq_hint,
        **result.outcome,
    )
    # Logged raw just above (`**result.outcome` already omits `notices`
    # entirely when there is none - `commands._attach_notices`'s own
    # docstring); the response's own copy is redacted for `viewer`, the same
    # resolved identity `redact_view` already keys on.
    outcome = result.outcome
    raw_notices = result.outcome.get("notices")
    if raw_notices:
        outcome = {**result.outcome, "notices": visibility.redact_notices(raw_notices, viewer)}
    return {
        **visibility.redact_view(state_view(result.game, game_id), viewer),
        **outcome,
        "log_seq": seq,
    }


@app.post(
    "/api/games/{game_id}/advance",
    response_model=ActionOutcome | AdvanceResponse,
    response_model_exclude_unset=True,
)
def advance(game_id: str, req: AdvanceRequest, uid: str | None = None) -> dict[str, Any]:
    """Move the turn marker forward. Refused (`allowed: False`) rather than
    performed when the stack is non-empty in a step that grants priority
    (CR 500.2, 117.4) - the engine's own refusal, not a transport error, so
    the caller's next move is `pass-priority`, not a retry - or when the
    caller is not the active player or their controller (CR 117.3a, 723.5).

    Entering a step whose turn-based action asks something with no scripted
    answer - the cleanup discard, CR 508.1's/CR 509.1's combat declarations -
    pauses instead of silently defaulting: the committed game is left exactly
    as it was (this response's board reflects the step `advance` was called
    from, not the one it was trying to enter), with the question attached as
    `pending_decision`, answerable through `POST .../decide` or its
    `actions/attack`/`actions/declare-blockers` aliases.
    """
    records.require_current_board(
        game_id, "advance", req.expected_seq, player=req.player, reasoning=req.reasoning
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    try:
        result, committed, legal_before, seq_hint = _run_command(game_id, "advance", req)
    except ValueError as exc:  # the engine's own refusal text
        # No `_run_command` attempt ever ran - fetched fresh, exactly as it
        # stood the instant this request was refused.
        game = game_or_404(game_id)
        pending_now = store.paused_command(game_id)
        legal_before = records.legal_actions_before(
            game, req.player, pending_now[2] if pending_now else None
        )
        seq = audit.record(
            game_id,
            "advance",
            player=req.player,
            allowed=False,
            reason=str(exc),
            reasoning=req.reasoning,
            legal_actions_before=legal_before,
        )
        return {"allowed": False, "reason": str(exc), "log_seq": seq}
    before = committed.turn.step.value if committed.turn is not None else None
    viewer = visibility.resolve_viewer(game_id, uid)
    if result.status is commands.RunStatus.PAUSED:
        seq = audit.record(
            game_id,
            "advance",
            player=req.player,
            allowed=None,
            paused=True,
            paused_on=_paused_on(result),
            reasoning=req.reasoning,
            legal_actions_before=legal_before,
        )
        return {
            **visibility.redact_view(state_view(committed, game_id), viewer),
            "allowed": None,
            "paused": True,
            "log_seq": seq,
        }
    assert result.game is not None and isinstance(result.outcome, dict)
    step_value = result.outcome["advanced_to"]
    notices = result.outcome.get("notices")
    seq = audit.record(
        game_id,
        "advance",
        player=req.player,
        allowed=True,
        **({"notices": notices} if notices else {}),
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        _fill_seq=seq_hint,
        **{"from": before, "to": step_value},
    )
    # Logged raw just above (when there was anything to log); the response's
    # own copy is redacted for `viewer`, the same resolved identity
    # `redact_view` already keys on. Left out of the response entirely when
    # empty - see `commands._attach_notices`'s own docstring.
    body = {
        **visibility.redact_view(state_view(result.game, game_id), viewer),
        "advanced_to": step_value,
        "allowed": True,
        "log_seq": seq,
    }
    if notices:
        body["notices"] = visibility.redact_notices(notices, viewer)
    return body


@app.post(
    "/api/games/{game_id}/report",
    response_model=ReportProblemResponse,
    response_model_exclude_unset=True,
)
def report_problem(game_id: str, req: ReportProblemRequest) -> dict[str, Any]:
    """A player files a problem report straight into the game log."""
    game = game_or_404(game_id)
    game.player(req.player)
    audit.record(game_id, "report_problem", player=req.player, text=req.text)
    return {"recorded": True}


@app.post(
    "/api/games/{game_id}/plan",
    response_model=DeclarePlanResponse,
    response_model_exclude_unset=True,
)
def declare_plan(game_id: str, req: DeclarePlanIn) -> dict[str, Any]:
    """Declare, before acting, how this seat reads the turn it is about to
    take: what it sees, its own role in the matchup, what it intends to do,
    and what it expects the opponent to do and why (`schemas.DeclarePlanIn`).

    **This mutates no `Game`.** It calls `audit.record` and nothing else - no
    `store.replace`, no compare-and-swap, no version bump - because a
    declaration is a statement *about* the game rather than a move in it. A
    route that quietly committed one would make every plan something the
    opponent's client has to notice, and would put a CAS conflict in the way
    of an agent that is only trying to think out loud.

    `audit.record` stamps the game's own turn and step from the live game
    (its own docstring), and that stamping is what makes the post-game
    pairing in `review.py` possible: the declaration and the opponent's
    later rows are ordered by the same `seq`, from the same ledger, with no
    second clock to reconcile. `turns_taken` is written into the detail
    beside them because `turn_number` counts rounds of the table rather than
    player-turns - see `records.require_plan`.

    Authorised on the seat's own uid, carried in the body: a plan is hidden
    information about how a seat means to play, so nobody may write one into
    another seat's name. An unrecognised uid, or one that opens a different
    seat, is a 404 - the same answer every other seat-scoped route gives,
    and one that does not confirm which uids exist.

    Nothing stops a human seat declaring a plan; the gate only *requires* one
    of an agent seat (`records.require_plan`).
    """
    game = game_or_404(game_id)
    if store.player_for_uid(game_id, req.uid) != req.player:
        raise KeyError("that uid is not that seat in this game")
    game.player(req.player)  # 400 for a player who is not in this game at all
    turn = game.turn
    seq = audit.record(
        game_id,
        audit.PLAN_ACTION,
        player=req.player,
        turns_taken=turn.turns_taken if turn is not None else None,
        board_read=req.board_read,
        my_role=req.my_role,
        plan=req.plan,
        prediction=req.prediction.model_dump(),
    )
    return {
        "declared": True,
        "log_seq": seq,
        "player": req.player,
        "turn_number": turn.turn_number if turn is not None else None,
    }


@app.post(
    "/api/games/{game_id}/reflection",
    response_model=ReflectionResponse,
    response_model_exclude_unset=True,
)
def declare_reflection(game_id: str, req: ReflectionIn) -> dict[str, Any]:
    """File this seat's own post-game write-up (`mtg_agent.driver.app`'s
    `turn_done` posts the driver's reflection turn here, the moment it
    completes).

    **This mutates no `Game`, exactly like `declare_plan`.** It calls
    `audit.record` under `audit.REFLECTION_ACTION` and nothing else - no
    `store.replace`, no version bump - a reflection is a statement made
    *after* play, not a move in it.

    **Post-game only, unlike `declare_plan`.** A reflection is written once
    the result is already known (CR 104.1's game-ending conditions), so this
    refuses with 409 before that - the same gate `game_review` enforces on
    reading it, mirrored here on writing it, since a reflection filed for a
    game still in progress could only be about a result nobody has yet.

    Authorised the same way `game_review` reads are: against `store.archived`
    rather than the live game, so this still works in the ordinary case where
    the driver posts a game's reflection after the lobby has already ended
    (and so soft-deleted) that game. `uid` must open `player`'s own seat -
    the same hidden-information rule `declare_plan` enforces, even though a
    reflection carries nothing hidden by the time it is legal to write one:
    the rule is about who may speak in a seat's name, not about what is said.
    """
    archived = store.archived(game_id)
    if archived is None:
        raise KeyError(f"no game {game_id}")
    if archived.seat_uids.get(req.uid) != req.player:
        raise KeyError("that uid is not that seat in this game")
    if not _post_game(archived, _gate_entries(archived)):
        raise HTTPException(
            status_code=409,
            detail=(
                "this game is still in progress - a reflection is written "
                "after the result is known, so it cannot be filed for a "
                "game that has not ended"
            ),
        )
    seq = audit.record(
        game_id,
        audit.REFLECTION_ACTION,
        player=req.player,
        text=req.text,
    )
    return {"recorded": True, "log_seq": seq, "player": req.player}


@app.post(
    "/api/games/{game_id}/decide", response_model=DecideResponse, response_model_exclude_unset=True
)
def decide(game_id: str, req: DecideRequest, uid: str | None = None) -> dict[str, Any]:
    """Answer this game's pending decision - most kinds CR 608.2d,
    `choose_targets` CR 603.3d/601.2c, `order_triggers` CR 603.3b, and
    `declare_attackers`/`declare_blockers` CR 508.1/509.1 instead - such as
    the cleanup discard's choice of which card to put in the graveyard.

    An ordinary mutating request now, like any other: it loads the game's
    paused command (`store.paused_command`), resumes it with this answer
    appended to the tape (`commands.resume_command`, via `_resume`), and
    commits whatever that produces. There is no longer a resolving thread
    parked in `Game.ask` for this to avoid racing, and no lock it must avoid
    taking - see `commands.py`'s module docstring and `repository.py`'s
    `InMemoryRepository` docstring for what replaced that invariant.

    `uid` must open the deciding player's own seat - not the opponent's, not
    a bystander's - matching CR 608.2d's "the player announces": nobody else
    may make this choice for them.
    """

    def refuse(reason: str, **detail: Any) -> dict[str, Any]:
        seq = audit.record(
            game_id,
            "decide",
            player=req.player,
            allowed=False,
            reason=reason,
            reasoning=req.reasoning,
            legal_actions_before=legal_before,
            **detail,
        )
        return {"accepted": False, "reason": reason, "log_seq": seq}

    records.require_current_board(
        game_id, "decide", req.expected_seq, player=req.player, reasoning=req.reasoning
    )
    game = game_or_404(game_id)
    legal_before: records.LegalActionsBefore | None = None
    # `game_access.seat_for_uid`, never `visibility.resolve_viewer`: this is
    # an authorization question ("who is acting"), and `resolve_viewer`
    # answers the visibility one ("what may this uid see"), which resolves an
    # advisor uid to the human it watches. Asking the read filter who is
    # acting is what let an advisor credential answer its human's decisions.
    player = game_access.seat_for_uid(game_id, uid)
    if player != req.player:
        # Refused before anything is enumerated: `req.player` is a request
        # field, and enumerating a seat the caller has merely *named* is
        # what put another seat's hidden information into a row the caller
        # could then read back.
        return refuse("that uid does not open the deciding player's seat")
    # T10: computed once the caller's uid has been agreed to open `player`'s
    # own seat, from that seat's side of the board before this answer runs,
    # and reused for every refusal below and the eventual success - `game`
    # here is never touched by anything until `_resume` takes its own lock
    # and works against its own copy.
    legal_before = records.legal_actions_before(
        game, player, (store.paused_command(game_id) or (None, None, None))[2]
    )
    paused = store.paused_command(game_id)
    if paused is None or paused[2].decision.player != req.player:
        return refuse("no pending decision for you")
    # Which question this answer is for, when the caller said
    # (`DecideRequest.generation`). `_pause_slot_guard` already makes it
    # impossible for one command to overwrite another's live pause, so an
    # answer can never be applied to a question it was not offered for - but
    # a resolution asking two decisions replaces the standing question the
    # instant the first is accepted, and a client that read the earlier one
    # would post its answer against a question it never saw. Refused here,
    # with the pause left exactly where it is, so the seat re-reads and
    # answers what is actually standing.
    generation = store.pause_generation(game_id)
    if req.generation is not None and req.generation != generation:
        return refuse(
            f"that answer names generation {req.generation}, but this game's standing "
            f"question is generation {generation}: the decision you read has already "
            "been answered and the resolution has moved on. Re-read pending_decision "
            "and answer the question standing now."
        )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    command, tape, pending = paused
    engine_decision = pending.decision
    kind = engine_decision.kind
    detail: dict[str, Any] = {"kind": kind.value, "prompt": engine_decision.prompt}
    if kind is DecisionKind.CHOOSE_CARDS:
        try:
            answer_value: Any = decisions.card_ids_answer(req.card_ids)
        except ValueError as exc:
            return refuse(f"bad card id: {exc}", **detail)
        detail["card_ids"] = req.card_ids
    elif kind is DecisionKind.CHOOSE_TARGETS:
        try:
            answer_value = decisions.targets_answer(req.targets)
        except ValueError as exc:
            return refuse(f"bad target: {exc}", **detail)
        detail["targets"] = req.targets
    elif kind is DecisionKind.ORDER_TRIGGERS:
        # CR 603.3b: "each player, in APNAP order, puts each triggered ability
        # they control on the stack in any order they choose". The answer is
        # an order, not a selection - a permutation of the ability ids in
        # `pending_decision.cards`, bottom of the stack first, so the last id
        # given is the ability that resolves first. Its own field because
        # those ids are `AbilityOnStack` ids, not card ids; the permutation
        # itself is checked by `validate_answer` below, against the same
        # `Decision` `ask_trigger_order` will check it against.
        try:
            answer_value = decisions.order_answer(req.order)
        except ValueError as exc:
            return refuse(f"bad ability id: {exc}", **detail)
        detail["order"] = req.order
    elif kind in (
        DecisionKind.MAY,
        DecisionKind.PAY_LIFE,
        DecisionKind.PAY_ENERGY,
        DecisionKind.PAY_MANA,
        # CR 103.6's "may take any such actions": the same yes/no shape.
        DecisionKind.OPENING_HAND_ACTION,
        # CR 103.5's declaration, "will you take a mulligan" - only
        # `pregame_settle` (`Game.settle_pregame`) ever pauses on this one,
        # and only when called with `use_defaults=False` (its default): a
        # seat declaring for itself instead uses `POST .../pregame/
        # mulligan`, which pauses on nothing. Yes/no like the rest of this
        # tuple; the *bottoming* that follows a kept hand is a separate
        # CHOOSE_CARDS question and needs nothing here.
        DecisionKind.MULLIGAN,
    ):
        try:
            answer_value = decisions.bool_answer(req.accept)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["accept"] = req.accept
    elif kind is DecisionKind.PAY_ENERGY_AMOUNT:
        try:
            answer_value = decisions.energy_amount_answer(req.energy_amount)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["energy_amount"] = req.energy_amount
    elif kind is DecisionKind.PAY_LIFE_AMOUNT:
        try:
            answer_value = decisions.life_amount_answer(req.life_amount)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["life_amount"] = req.life_amount
    elif kind is DecisionKind.CHOOSE_A_QUALITY:
        try:
            answer_value = decisions.quality_answer(req.choice)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["choice"] = req.choice
    elif kind is DecisionKind.CHOOSE_A_MODE:
        try:
            answer_value = decisions.mode_answer(req.choice)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["choice"] = req.choice
    elif kind is DecisionKind.PUT_ON_TOP_OR_BOTTOM:
        try:
            answer_value = decisions.top_or_bottom_answer(req.top)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["top"] = req.top
    elif kind in (DecisionKind.SCRY, DecisionKind.SURVEIL):
        try:
            answer_value = decisions.card_split_answer(req.moved, req.top_order)
        except ValueError as exc:
            return refuse(f"bad card id: {exc}", **detail)
        detail["moved"] = req.moved
        detail["top_order"] = req.top_order
    elif kind in (DecisionKind.DECLARE_ATTACKERS, DecisionKind.DECLARE_BLOCKERS):
        # CR 508.1/509.1's combat declarations - answerable here directly
        # (the same pair shape `actions/attack`/`actions/declare-blockers`
        # build from their own request shape, see those routes) as well as
        # through the two aliases.
        try:
            answer_value = decisions.declared_pairs_answer(req.pairs)
        except ValueError as exc:
            return refuse(f"bad pair: {exc}", **detail)
        detail["pairs"] = [{"creature": str(p.creature), "defender": p.defender} for p in req.pairs]
    else:  # pragma: no cover - unreachable: ANSWERABLE_KINDS == every DecisionKind
        return refuse(
            f"this endpoint does not know how to answer a {kind.value!r} decision", **detail
        )
    # The last gate before this answer is trusted: everything the engine's
    # own asker for this kind would check (the right number of cards, a
    # target actually offered, an amount in range, ...) runs here first,
    # against the same `Decision` the engine raised when it asked the
    # question. An answer that fails this is refused right here,
    # synchronously, to the seat that gave it - never appended to the tape,
    # never logged `allowed: true`, and the decision stays pending so the
    # same seat can answer again. See `decisions.validate_answer`'s
    # docstring for the rest of this reasoning.
    try:
        decisions.validate_answer(engine_decision, answer_value)
    except ValueError as exc:
        return refuse(str(exc), **detail)
    field_name = decisions.ANSWER_FIELD_FOR_KIND[kind]
    answer = Answer(seq=pending.seq, kind=kind, player=req.player, **{field_name: answer_value})
    try:
        result, _, seq_hint = _resume(game_id, command, tape, pending, answer, player=req.player)
    except ValueError as exc:
        # The engine itself refused this answer once the command actually
        # replayed with it - reached, in practice, only by the two
        # declaration kinds (`validate_answer`'s own docstring), since every
        # other kind's shape check above already mirrors what the engine
        # would check. Refused the same synchronous way as every check above.
        return refuse(str(exc), **detail)
    seq = audit.record(
        game_id,
        "decide",
        player=req.player,
        allowed=True,
        reason=None,
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        _fill_seq=seq_hint,
        **detail,
    )
    if result.status is commands.RunStatus.PAUSED:
        assert result.pending is not None
        next_pending = decisions.PendingDecision.from_engine(
            result.pending.decision, store.pause_generation(game_id)
        )
        return {
            "accepted": True,
            "paused": True,
            "pending_decision": next_pending.to_view(),
            "log_seq": seq,
        }
    return {"accepted": True, "log_seq": seq}


# -- the pregame: CR 103.5 and CR 103.6 ---------------------------------------
#
# A game booted from decklists *rests* in `Step.MULLIGAN` and then in
# `Step.PREGAME_ACTIONS` before CR 103.8's first turn begins. Nothing in this
# API settles either one on the seats' behalf: each of the three endpoints
# below is one seat's own move, told an answer already made, and none of them
# blocks - which is the whole reason the engine turned the pregame into steps
# a game rests in. See `opening.py`.
#
# Authorization is the same rule `decide` applies to CR 608.2d's "the player
# announces": the `uid` must open the acting player's own seat. Nobody else
# declares a mulligan for them (CR 103.5's declaration is each player's own),
# and nobody else takes a CR 103.6 action with a card in their opening hand.


def _pregame_seat(game_id: str, uid: str | None, player: str) -> None:
    """Refuse unless `uid` opens `player`'s own seat, or is the admin key.

    The admin key stands for whoever created the game - the same operator
    trust level `visibility.ADMIN` already grants the full board - which is
    what lets a harness or a boot script drive a seat it owns.
    """
    if uid and store.is_admin(game_id, uid):
        return
    if game_access.seat_for_uid(game_id, uid) != player:
        raise ValueError(f"that uid does not open {player}'s seat")


def _settled(game: Game, result: dict[str, Any]) -> dict[str, Any]:
    """Run the game past any step nobody receives priority in, and correct the
    `step` this pregame response reports to where it actually ended up.

    CR 103.8's first turn begins at the untap step, which grants nobody
    priority (CR 502.4) and which CR 500.3 ends of its own accord. Without
    this, the last pregame call hands back `untap` with `priority_holder:
    null` - the state the user reported as a frozen page, with no move any
    client is allowed to make. See `engine_api.settle_priorityless_steps`."""
    crossed = engine_api.settle_priorityless_steps(game)
    if crossed and game.turn is not None:
        result = {**result, "step": game.turn.step.value, "settled_through": crossed}
    return result


@app.post(
    "/api/games/{game_id}/pregame/mulligan",
    response_model=MulliganResponse,
    response_model_exclude_unset=True,
)
def pregame_mulligan(game_id: str, req: MulliganRequest, uid: str | None = None) -> dict[str, Any]:
    """Declare whether this seat takes a mulligan (CR 103.5).

    "First, the starting player declares whether they will take a mulligan.
    Then each other player in turn order does the same." Who still owes a
    declaration is `pregame.awaiting_mulligan` on the state view, and the round
    resolves - every player who declared a mulligan taking it simultaneously -
    the moment the last of them has spoken.

    `bottom` names the cards a keeping seat puts on the bottom of its library,
    one per counted mulligan; it is pre-provided to the engine rather than
    asked back through `decide`, so this request never blocks. See
    `opening.py`.

    Gated by `records.require_reasoning` like every other seat action (T10).
    An earlier pass exempted the three pregame routes on the grounds that
    `ai_pregame.py`'s backstop makes this declaration *for* a quiet agent
    seat with no reasoning to give - which is not true of this route:
    `ai_pregame.settle_now` calls `opening.declare_mulligan` directly, in
    process, and never travels through here, so the gate it would have to
    pass is one it never reaches. Keeping the hand or taking a mulligan is a
    real choice with a real why - the first one an agent makes in a game -
    and there is no reason it alone should be unexplained.
    """
    _pregame_seat(game_id, uid, req.player)
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    with store.locked(game_id) as game:
        game.player(req.player)  # 404 for a player who is not in this game
        return opening.declare_mulligan(
            game_id, game, req.player, req.take, req.bottom, reasoning=req.reasoning
        )


@app.post(
    "/api/games/{game_id}/pregame/opening-hand/action",
    response_model=OpeningHandActionResponse,
    response_model_exclude_unset=True,
)
def pregame_opening_hand_action(
    game_id: str, req: OpeningHandActionRequest, uid: str | None = None
) -> dict[str, Any]:
    """Take one CR 103.6 action with a card in this seat's opening hand.

    "Once the mulligan process (see rule 103.5) is complete, the starting
    player may take any such actions in any order." What is on offer, and to
    whom right now, is `pregame.opening_hand_options` / `pregame.
    opening_hand_actor` on the state view; a seat that wants none of them calls
    the `finish` endpoint below instead.

    Gated by `records.require_reasoning` - see `pregame_mulligan`'s
    docstring.
    """
    _pregame_seat(game_id, uid, req.player)
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    with store.locked(game_id) as game:
        game.player(req.player)
        return opening.take_opening_hand_action(
            game_id, game, req.player, req.card_id, reasoning=req.reasoning
        )


@app.post(
    "/api/games/{game_id}/pregame/opening-hand/finish",
    response_model=OpeningHandFinishResponse,
    response_model_exclude_unset=True,
)
def pregame_opening_hand_finish(
    game_id: str, req: ActorRequest, uid: str | None = None
) -> dict[str, Any]:
    """This seat is done taking CR 103.6 actions.

    The next seat is up; when the last one finishes, CR 103.8's first turn
    begins - at its untap step, in which no player receives priority (CR
    502.4). Nobody has to be asked to leave that step (CR 500.3), so the
    response's `step` is the first one somebody can act in: `upkeep`. See
    `engine_api.settle_priorityless_steps`.

    Gated by `records.require_reasoning` - see `pregame_mulligan`'s
    docstring.
    """
    if not req.player:
        raise ValueError("finishing CR 103.6's actions needs the player who is finishing")
    _pregame_seat(game_id, uid, req.player)
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    with store.locked(game_id) as game:
        game.player(req.player)
        result = opening.finish_opening_hand_actions(
            game_id, game, req.player, reasoning=req.reasoning
        )
        return _settled(game, result)


@app.post(
    "/api/games/{game_id}/pregame/settle",
    response_model=PregameSettleResponse,
    response_model_exclude_unset=True,
)
def pregame_settle(
    game_id: str, req: PregameSettleRequest | None = None, uid: str | None = None
) -> dict[str, Any]:
    """Run the whole pregame (CR 103.5's mulligan process, CR 103.6's
    opening-hand actions) forward, over every seat at once.

    An ordinary command now (`commands.run_command`/`_run_command`), like
    `advance`: it pauses on the first `mulligan`/`opening_hand_action`/
    bottoming `choose_cards` question the engine asks with no scripted
    answer, and a live seat answers it through `POST .../decide` from there
    - see `PregameSettleRequest.use_defaults` for the headless alternative,
    which pre-fills every one of those with the engine's own default instead
    (`commands.run_command_to_completion`) and so still returns a fully
    settled, playable game from this one call, exactly as this endpoint
    always did before pausing existed.

    Admin-key only: it makes every seat's CR 103.5 declaration at once (or,
    with `use_defaults=False`, opens the whole pregame up to be answered),
    which is the operator's call over a game they created, never one seat's
    over the other's.
    """
    req = req or PregameSettleRequest()
    if not (uid and store.is_admin(game_id, uid)):
        raise ValueError("settling the whole pregame for every seat needs this game's admin key")
    # P4, and after the admin gate rather than before it (the one place this
    # guard is not the very first thing a route does): the refusal writes a
    # ledger row, and a caller who has not shown the admin key has no
    # business writing one into a game they cannot otherwise touch. Both
    # branches below are covered by this single call - `use_defaults=True`
    # runs the whole pregame to completion in its own lock without going
    # through `_run_command` at all, so it has no other guard anywhere.
    records.require_current_board(game_id, "pregame_settle", req.expected_seq)
    viewer = visibility.resolve_viewer(game_id, uid)
    seq_hint: int | None = None
    if req.use_defaults:
        command = commands.Command.build("pregame_settle", req)
        with store.locked(game_id) as game:
            # `_run_command`'s pause-slot guard, which this branch bypasses
            # entirely: `run_command_to_completion` answers every question
            # with the engine's own default, so it never pauses and never
            # consults the slot. Deliberately keyed on the stored command's
            # *kind* rather than on `_pause_slot_guard`'s exact-command
            # equality, because `use_defaults` is itself part of the payload:
            # the pause this call is most likely to meet is the one a
            # `use_defaults=False` settle left behind, and overriding exactly
            # that is what an operator reaches for when a seat has gone quiet
            # mid-pregame. Making every seat's CR 103.5 declaration at once
            # is what this admin-key endpoint was asked to do (see
            # `PregameSettleRequest.use_defaults`), so walking over a
            # `pregame_settle` question is an override by design, recorded
            # here rather than left implicit. Any *other* live command's
            # pause is still refused - a seat part-way through a resolution
            # is not the operator's to overrule - and a stale pause is
            # cleared, exactly as anywhere else.
            paused = store.paused_command(game_id)
            if paused is not None:
                stored_command, stored_tape, stored_pending = paused
                if not commands.pause_is_live(game, stored_command, stored_tape, stored_pending):
                    store.clear_paused_command(game_id)
                elif stored_command.kind != "pregame_settle":
                    raise commands.DecisionInProgressError(stored_command, stored_pending, command)
            result = commands.run_command_to_completion(game, command)
            assert result.status is commands.RunStatus.COMPLETED
            assert result.game is not None
            seq_hint = store.replace(game_id, result.game)
            store.clear_paused_command(game_id)
    else:
        result, _, _, seq_hint = _run_command(game_id, "pregame_settle", req)
    if result.status is commands.RunStatus.PAUSED:
        assert result.pending is not None
        seq = audit.record(
            game_id, "pregame_settle", allowed=None, paused=True, paused_on=_paused_on(result)
        )
        game = game_or_404(game_id)
        return {
            **visibility.redact_view(state_view(game, game_id), viewer),
            "allowed": None,
            "paused": True,
            "log_seq": seq,
        }
    assert result.game is not None and isinstance(result.outcome, dict)
    hands = ", ".join(f"{player.username}={len(player.hand)}" for player in result.game.players)
    mulligans = ", ".join(
        f"{player.username}={player.mulligans_taken}" for player in result.game.players
    )
    seq = audit.record(
        game_id,
        "pregame_settled",
        _fill_seq=seq_hint,
        hands=hands,
        mulligans=mulligans,
        # The opposite of `declare_mulligan`'s entry: `use_defaults=True`
        # asked nobody, so every declaration is the engine's default rather
        # than a seat's own choice. `use_defaults=False` genuinely opened
        # every question up to a live `POST .../decide` answer along the
        # way (even where nothing was actually pending to ask).
        live=not req.use_defaults,
        step=result.outcome["step"],
    )
    return {
        **visibility.redact_view(state_view(result.game, game_id), viewer),
        "allowed": True,
        "log_seq": seq,
    }


# -- the play-vs-AI UI --------------------------------------------------------
#
# Server-rendered HTML plus plain JS, served from this repo (`ui/`) - no
# build step, no npm. The lobby is the front door of this deployment, so it is
# mounted at /, and the sibling `magic-cards-model-test-frontend` repo (a
# developer tool, present only when that repo is checked out next to this one)
# moves to /frontend rather than holding the root.
#
# Mount order is what keeps this safe. A StaticFiles mount at / matches every
# otherwise-unclaimed path, so it must be the LAST thing registered: every
# /api route, /docs, and the /play redirects below are already on the router by
# the time it is added, and a route that is already registered always wins.


class _GameIdConvertor(Convertor[str]):
    """A game id is a bare `uuid4().hex` - exactly 32 lowercase hex
    characters (`repository.py`, `repository_postgres.py` both mint it that
    way; nothing on this api's HTTP surface lets a caller supply another
    shape). T7 puts a bare `/<uuid>/` at the root, alongside every static
    asset that root also serves (`app.js`, `style.css`, `config.js`,
    `render/ribbon.js`, `favicon.ico`, ...) - a plain `/{game_id}` path
    parameter would swallow every one of them, since each is itself a single
    unclaimed path segment. Registering this as its own path convertor
    (rather than checking the shape inside the handler) resolves that
    ambiguity at routing time: the game routes below simply never match an
    asset name, because no asset name matches this regex, so a request for
    `/app.js` falls through to the static mount exactly as before. A
    malformed id - wrong length, uppercase, non-hex - falls through the same
    way and 404s there, rather than being treated as a game."""

    regex = "[0-9a-f]{32}"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("game_id", _GameIdConvertor())

_ui_dir = Path(__file__).resolve().parents[2] / "ui"
_frontend = frontend_dir()

if _frontend is not None:
    app.mount("/frontend", StaticFiles(directory=_frontend, html=True), name="frontend")

if _ui_dir.is_dir():
    # /play/ was where this UI lived until the lobby moved to /. Redirected
    # rather than 404ed, because the people most likely to hit it are the ones
    # who used it - a bookmark, an open tab, a link in a note. 307 and not 308:
    # the redirect is permanent in intent, but a browser caches a 308 for the
    # host indefinitely, and that is an expensive thing to be wrong about for a
    # path this deployment might want back. The whole subpath is carried over,
    # so /play/app.js lands on /app.js.
    @app.get("/play", include_in_schema=False)
    @app.get("/play/{rest:path}", include_in_schema=False)
    def play_moved(rest: str = "") -> RedirectResponse:
        return RedirectResponse(url=f"/{rest}", status_code=307)

    def _safe_ui_rest(rest: str) -> str:
        """`rest` if it resolves inside `_ui_dir` without walking out of it
        (`../../pyproject.toml`), `""` otherwise - regardless of whether a
        real file exists at that path (an unmatched-but-safe subpath is a
        normal "fall back to the page" case, handled where this is used, not
        an escape). Shared by the canonical bare route below and the `/g/`
        compatibility redirect, so a traversal attempt is recognised the
        same way in both, and so a `%2e%2e` escape decoded into a literal
        `..` by the ASGI layer never gets carried into a `Location` header -
        that would just hand the same escape back to the browser's own
        dot-segment resolution one hop later instead of refusing it."""
        if not rest:
            return ""
        candidate = (_ui_dir / rest).resolve()
        if candidate.is_relative_to(_ui_dir.resolve()):
            return rest
        return ""

    # T7 - the owner's own ask: a game gets a bare, bookmarkable
    # `/<uuid>/` address at the root, not `/g/<uuid>/` - "when I start a game
    # I want to be redirected to https://<base url>/<game uuid>". This is now
    # canonical; `/g/<uuid>/` (T4) becomes a redirect to it below, kept
    # working because it is live and already linked/bookmarked. Canonical
    # was chosen as the bare form, not `/g/`, because it is the literal shape
    # asked for, and because a shorter permanent address is the one worth
    # bookmarking - `/g/` only existed as an implementation detail of how T4
    # first carved the address space up.
    #
    # `document.baseURI` is still how the play modules tell which game they
    # are looking at (not `store`), so this only has to decide which file
    # answers each request - same as `/g/<uuid>/` always did. No trailing
    # slash redirects to one (`/<id>` -> `/<id>/`) for the same reason
    # `/play` did above: a relative `app.js` resolved against a base with no
    # trailing slash drops the last segment - the game id itself - and asks
    # for `/app.js` instead, which the static mount happily answers with the
    # wrong content instead of erroring.
    #
    # `{game_id:game_id}` (the `_GameIdConvertor` registered above) is what
    # keeps this from swallowing every asset the static mount below also
    # serves - see that class's own docstring for why a shape check alone,
    # done inside the handler, would not be enough: the route itself must
    # never match `app.js` in the first place.
    @app.get("/{game_id:game_id}", include_in_schema=False)
    def bare_game_path_no_slash(game_id: str) -> RedirectResponse:
        return RedirectResponse(url=f"/{game_id}/", status_code=307)

    @app.get("/{game_id:game_id}/{rest:path}", include_in_schema=False)
    def bare_game_path(game_id: str, rest: str = "") -> Response:
        safe_rest = _safe_ui_rest(rest)
        if safe_rest:
            candidate = (_ui_dir / safe_rest).resolve()
            if candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(_ui_dir / "index.html")

    # `/g/<uuid>/` - T4's original address - is kept working, but only as a
    # redirect to the now-canonical bare form above: it is live, tested, and
    # may already be linked or bookmarked, but two addresses for the same
    # game is exactly the ambiguity the owner's two-tabs use case cannot
    # afford (a saved seat keyed by whichever address a tab happens to be
    # on). `game_id` here stays a plain string, not `{game_id:game_id}`: it
    # only has to redirect somewhere, never to serve a file itself, so an
    # id that turns out not to be a real 32-hex game id simply redirects to
    # a bare path that 404s the same way `/g/<bad-id>/` would have 404ed by
    # falling through the static mount before this route existed at all.
    @app.get("/g/{game_id}", include_in_schema=False)
    def game_path_no_slash(game_id: str) -> RedirectResponse:
        # One hop straight to the real canonical address, not to
        # `/g/<id>/` first - that slashed form is itself only a redirect
        # now, and bouncing through it would just add a pointless second
        # round trip for every bookmark still shaped `/g/<id>` with no
        # trailing slash.
        return RedirectResponse(url=f"/{game_id}/", status_code=307)

    @app.get("/g/{game_id}/{rest:path}", include_in_schema=False)
    def game_path(game_id: str, rest: str = "") -> RedirectResponse:
        safe_rest = _safe_ui_rest(rest)
        target = f"/{game_id}/{safe_rest}" if safe_rest else f"/{game_id}/"
        return RedirectResponse(url=target, status_code=307)

    app.mount("/", StaticFiles(directory=_ui_dir, html=True), name="play-ui")
elif _frontend is not None:
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend-root")
else:

    @app.get("/", include_in_schema=False)
    def landing() -> HTMLResponse:
        return HTMLResponse(_LANDING)
