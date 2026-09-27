"""The FastAPI app: session setup, state, legality questions, and the frontend.

Everything lives under /api; / serves this repo's own play UI (`ui/`), and the
sibling frontend repo, when it is checked out next to this one, sits at
/frontend. /play/ - where the play UI used to live - redirects to /. There is
deliberately no CORS: the UI is served by this app itself, so there is exactly
one origin.
"""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import mimetypes
import os
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from magic_cards_model.machines import Step
from magic_cards_model.machines.decisions import Answer, DecisionKind
from magic_cards_model.machines.decisions import PendingDecision as PendingDecisionEngine
from magic_cards_model.machines.legal_actions import blocked_casts
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
    handoff,
    lobby,
    oidc,
    opening,
    player_control,
    protocol,
    records,
    review,
    review_cache,
    scryfall,
    serialize,
    symbology,
    visibility,
)
from .action_rows import legal_action_rows
from .board_scope import read_on_one_board
from .playscripts import decision_dsl
from .playscripts.hypothetical_facts import build_hypothetical_facts
from .playscripts.report import (
    development_candidates,
    hand_played_summary,
    offer_table,
    script_run_table,
)
from .repository import RECENT_RECORDS_WINDOW, ArchivedGame, GameUnloadable
from .repository_postgres import VersionConflict
from .schemas import (
    ActionOutcome,
    ActivateRequest,
    ActorRequest,
    AdhocScriptRequest,
    AdhocScriptResponse,
    AdvanceRequest,
    AdvanceResponse,
    AdviceApprovedResponse,
    AdviceApproveIn,
    AdviceIn,
    AdviceResponse,
    AffordancesResponse,
    AiReviewFailureIn,
    AiReviewFailureResponse,
    AiReviewIn,
    AiReviewResponse,
    AttackRequest,
    CardLookupResponse,
    CardNamesResponse,
    CastRequest,
    CompanionRequest,
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
    EquipRequest,
    GameLogResponse,
    GameReviewResponse,
    GameStateView,
    GameSummariesResponse,
    HealthResponse,
    HypotheticalFactsResponse,
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
    OpsDslCoverageResponse,
    PassPriorityRequest,
    PassPriorityResponse,
    PatchCard,
    PatchCardResponse,
    PatchPlayer,
    PlayLandRequest,
    PlaysOutcomeIn,
    PlaysOutcomeResponse,
    PregameSettleRequest,
    PregameSettleResponse,
    PutCard,
    PutCardResponse,
    ReflectionIn,
    ReflectionResponse,
    ReportProblemRequest,
    ReportProblemResponse,
    ResolveTopResponse,
    ScriptRunIn,
    ScriptRunResponse,
    ScriptsCorpusResponse,
    ScriptsRegisteredResponse,
    ScriptsRegisterIn,
    SeatLookupResponse,
    SeatResponse,
    SetStepResponse,
    SpecialActionRequest,
    StateBasedActionsResponse,
    StepUpdate,
    TapCardResponse,
    TargetRequest,
    TurnFaceUpRequest,
)
from .serialize import FIRST_STRIKE_DAMAGE, state_view, step_position
from .store import known_players, new_seat_uids, player_for_advisor_uid, store

logger = logging.getLogger(__name__)

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


@app.exception_handler(oidc.BearerTokenRejected)
async def _bearer_token_rejected(request: Request, exc: oidc.BearerTokenRejected) -> Response:
    """A page load whose token expired or failed goes to login, not to a raw
    JSON 401 in the browser: `/<game_id>/` is gated by `GameAccessRoute` like
    any `{game_id}` route. `/api/*` and non-HTML callers keep the JSON 401."""
    if oidc.is_page_navigation(request):
        return oidc.login_redirect(request)
    return await http_exception_handler(request, exc)


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


@app.exception_handler(records.MissingRole)
def _missing_role(_: Request, exc: records.MissingRole) -> JSONResponse:
    """422, the same shape `MissingPlan` gets, naming `my_role` as the
    missing field - a seat's very first declaration this game is the one
    with nothing earlier to carry it forward from."""
    return JSONResponse(
        status_code=422,
        content={"detail": [{"loc": ["body", "my_role"], "msg": str(exc), "type": "missing"}]},
    )


@app.exception_handler(records.NoPriorDeclaration)
def _no_prior_declaration(_: Request, exc: records.NoPriorDeclaration) -> JSONResponse:
    """422 naming `reuse_previous`: it was set, but this seat has no earlier
    declaration in this game to copy `plan`/`prediction` forward from."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": [{"loc": ["body", "reuse_previous"], "msg": str(exc), "type": "value_error"}]
        },
    )


@app.exception_handler(records.OwnTurnInProgress)
def _own_turn_in_progress(_: Request, exc: records.OwnTurnInProgress) -> JSONResponse:
    """409 with `code: own_turn_in_progress`: `for_turn: "next"` was given
    while the declaring seat's own turn is already under way. The same
    coded-409 shape `ScriptsDisabled` gets - `allowed`/`reason` are the
    refusal shape `seatkit._normalise_non_2xx` hands on unchanged, and `code`
    is what a runner branches on rather than this sentence."""
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "allowed": False, "reason": str(exc), "code": exc.code},
    )


@app.exception_handler(records.ScriptsDisabled)
def _scripts_disabled(_: Request, exc: records.ScriptsDisabled) -> JSONResponse:
    """409 with `code: scripts_disabled`: the operator's `SCRIPTS_ENABLED`
    kill switch is off. Nothing about the request is wrong and resending it
    will not help until the switch flips back - the code says so without the
    runner reading prose. `allowed`/`reason` are the refusal shape
    `seatkit._normalise_non_2xx` hands on unchanged."""
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "allowed": False, "reason": str(exc), "code": exc.code},
    )


@app.exception_handler(records.InvalidScripts)
def _invalid_scripts(_: Request, exc: records.InvalidScripts) -> JSONResponse:
    """422 with `code: invalid_scripts` and `errors`, the `PlaysError` codes
    `playscripts.schema` and mtg-decks' checker share - a runner fixes a
    script from the code, not from a pydantic trace."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": str(exc),
            "allowed": False,
            "reason": str(exc),
            "code": exc.code,
            "errors": exc.errors,
        },
    )


@app.exception_handler(records.UnknownScript)
def _unknown_script(_: Request, exc: records.UnknownScript) -> JSONResponse:
    """404 with `code: unknown_script` and `script_id`: the start named a
    script not registered under the uid that runs it, and nothing was
    recorded. Registering a corpus that holds it is the only fix."""
    return JSONResponse(
        status_code=404,
        content={
            "detail": str(exc),
            "allowed": False,
            "reason": str(exc),
            "code": exc.code,
            "script_id": exc.script_id,
        },
    )


@app.exception_handler(records.ScriptAlreadyRan)
def _script_already_ran(_: Request, exc: records.ScriptAlreadyRan) -> JSONResponse:
    """409 with `exit: already_ran`: this seat already ran this script at
    this turn and step, and nothing was recorded."""
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "allowed": False,
            "reason": str(exc),
            "exit": exc.exit,
            "prior_run_id": exc.prior_run_id,
        },
    )


@app.exception_handler(records.BoardMoved)
async def _board_moved(request: Request, exc: records.BoardMoved) -> JSONResponse:
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
    same function, with the same credential this request actually carried. A
    caller is therefore handed precisely what a log read of its own would
    have given it and never a byte more - the redaction argument for this
    body is `game_log`'s argument, unchanged - and it is handed it without
    spending the round trip.

    "The same credential" is `game_access._credentials`, the one function
    that already knows every slot a credential arrives in - the query
    string first, then the JSON body's own `uid`/`key` for the routes that
    carry theirs there. Reading `request.query_params["uid"]` by hand, which
    is what this did, sees neither a body-borne uid nor a `key=`, and the
    consequence here is not under-redaction but the *maximum*: `game_log`'s
    uid-less default is deliberately the debugger's eye, so a credential
    this handler failed to see bought both players' hands rather than
    neither (`serialize.state_view`'s `"hands"`). The reason that was
    reachable at all was a mutating route answering a caller who presented
    nothing, which `game_access.py`'s positive rule now refuses outright -
    this is the second of the two locks, and the one that keeps holding if
    a credential ever arrives somewhere new.
    """
    uid, key = await game_access._credentials(request)
    since = game_log(exc.game_id, uid=uid or key, since=exc.expected_seq)
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
async def _version_conflict(request: Request, exc: VersionConflict) -> JSONResponse:
    """409: another writer's `replace()` committed a newer version first
    (`repository_postgres.py`'s own module docstring - optimistic
    concurrency, no server-side retry). The body carries the game's current
    state, redacted exactly the way `GET /api/games/{id}` redacts it for the
    same `uid` this request itself carried - the loser is never handed more
    than it was already entitled to see - so the loser can re-read and
    recompute its attempt against what is actually committed, rather than
    retrying blind against the state it already knows is stale.

    The credential is resolved through `game_access._credentials` for the
    same reason `_board_moved` above does it: every slot one can arrive in,
    in one place. This handler already failed *closed* on a credential it
    could not see (`visibility.resolve_viewer(None)` is the viewer that sees
    nothing hidden), so there was nothing leaking here - but the two
    handlers answering the same question two ways is how one of them ends up
    wrong."""
    uid, key = await game_access._credentials(request)
    viewer = visibility.resolve_viewer(exc.game_id, uid or key)
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

    **The question expired, and the body says so in a stable shape.** By the
    time this answers, `_resume` has cleared the stale pause, so the game is
    never left stuck on a question nothing can answer (a deploy that changes
    how a replay mints ids makes every stored pause diverge). `detail` and
    `reason` are `commands.QUESTION_EXPIRED`, telling the client to re-issue
    the original action; `expired: True` is the key a client branches on to
    refresh rather than re-send; `allowed`/`accepted: False` make every seat
    and browser caller read it as the refusal it is. The replay's own
    comparison stays in the server log (`CommandDivergenceError` logs it).
    """
    return JSONResponse(
        status_code=409,
        content={
            "detail": commands.QUESTION_EXPIRED,
            "reason": commands.QUESTION_EXPIRED,
            "expired": True,
            "allowed": False,
            "accepted": False,
        },
    )


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


@app.exception_handler(commands.GameOverError)
def _game_over(_: Request, exc: commands.GameOverError) -> JSONResponse:
    """409: the game has ended (CR 104.1), so no action, decision or dry run
    is taken in it - see `commands.GameOverError`.

    Registered above the `ValueError` handler with the other 409s. `type:
    game_over` names the refusal for a client that switches on it;
    `allowed: False`/`reason` are what `seatkit._refused` reads for an
    action, and `accepted: False` is `decide`'s own refusal key. `winner`
    and `drawn` are the ending, so a seat need not read the board to stop.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": str(exc),
            "type": "game_over",
            "allowed": False,
            "accepted": False,
            "reason": str(exc),
            "winner": exc.winner,
            "drawn": exc.drawn,
        },
    )


@app.exception_handler(ValueError)
def _value_error(_: Request, exc: ValueError) -> JSONResponse:
    """Setup requests that name an unknown zone, counter, color, or card."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


#: pydantic's error types for a value that is not a UUID. Every `UUID` field a
#: request carries (body or path) is a card or ability id.
_UUID_ERROR_TYPES = frozenset({"uuid_parsing", "uuid_type", "uuid_version"})


@app.exception_handler(RequestValidationError)
def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # No `include_url`/`include_input` kwargs: pydantic 2.13 removed them from
    # `errors()`, and calling them turned every malformed request body into a
    # raw 500 instead of this 422.
    errors = exc.errors()
    if not any(error.get("type") in _UUID_ERROR_TYPES for error in errors):
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {key: error.get(key) for key in ("loc", "msg", "type")} for error in errors
                ]
            },
        )
    return JSONResponse(status_code=422, content=_malformed_id_refusal(errors))


def _malformed_id_refusal(errors: Sequence[Any]) -> dict[str, Any]:
    """D15: a bad id is refused as one, naming the value and the full form
    (`decisions.malformed_id_reason`), instead of pydantic's length complaint.

    The same `detail` list as every other 422, each id error rewritten to
    `type: malformed_card_id`, plus `allowed: False` and `reason` so a seat
    reads it as the typed refusal every action answer already is. A
    `UUID | ManaSource` union reports one bad string once per branch
    (`(..., 0, 'uuid')` and `(..., 0, 'ManaSource')`); only the id branch is
    what a string meant, so the siblings at that position are dropped and
    the id error is reported at the position itself."""
    union_positions = {
        tuple(error["loc"][:-1])
        for error in errors
        if error.get("type") in _UUID_ERROR_TYPES and error["loc"] and error["loc"][-1] == "uuid"
    }
    detail: list[dict[str, Any]] = []
    for error in errors:
        loc = tuple(error.get("loc") or ())
        if error.get("type") in _UUID_ERROR_TYPES:
            position = loc[:-1] if tuple(loc[:-1]) in union_positions else loc
            detail.append(
                {
                    "loc": list(position),
                    "msg": decisions.malformed_id_reason(error.get("input")),
                    "type": "malformed_card_id",
                }
            )
        elif not any(loc[: len(position)] == position for position in union_positions):
            detail.append({key: error.get(key) for key in ("loc", "msg", "type")})
    return {
        "detail": detail,
        "allowed": False,
        "reason": "; ".join(str(entry["msg"]) for entry in detail),
    }


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
    if outcome.allowed and "mana_sources" in log_keys and "tapped" in outcome.detail:
        # The permanents that paid, not the ones the request offered: a
        # source offered to a cost with no mana in it paid nothing (B5-05).
        paid = [entry["card_id"] for entry in outcome.detail["tapped"]]
        log_keys = {**log_keys, "mana_sources": paid}
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


def _refuse_after_game_over(request: Request, game_id: str) -> None:
    """Every action route's first gate: a finished game (CR 104.1) is refused
    with `commands.GameOverError` before anything else is asked of the
    request - its body, its `expected_seq`, its reasoning.

    First, because every other gate is wrong about a finished game. A seat
    that decided before the ending names a ledger position the `game_over`
    row has since moved past, and `records.BoardMoved` would send it to
    re-read and try again; `decide` would answer "no pending decision for
    you". Declared as a route dependency, so FastAPI runs it before the body
    is validated. `_run_command` and `_resume` ask again under the game's
    lock, which is what refuses a command racing the one that ended it.
    """
    try:
        game = game_or_404(game_id)
    except KeyError:
        return  # the route's own lookup answers the 404
    template = getattr(request.scope.get("route"), "path", "") or ""
    commands.refuse_if_over(game, template.rsplit("{game_id}/", 1)[-1] or "this action")


#: Declared on every route that takes an action in a game - see
#: `_refuse_after_game_over`.
REFUSED_AFTER_GAME_OVER = [Depends(_refuse_after_game_over)]


def _pause_slot_guard(
    game_id: str, game: Game, command: commands.Command, *, clear_stale: bool = True
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
        if clear_stale:
            store.clear_paused_command(game_id)
        return None
    if stored_command != command:
        raise commands.DecisionInProgressError(stored_command, stored_pending, command)
    return paused


def _acting_seat(game_id: str, player: str, uid: str | None) -> str:
    """Who the reasoning and plan gates hold to account for an action naming
    `player`: the seat `uid` opens when that seat is making `player`'s
    decisions this turn (CR 723.5) - an agent controlling another player
    answers for its own plan, never theirs - and otherwise `player`."""
    seat = game_access.seat_for_uid(game_id, uid)
    if seat is None or seat == player:
        return player
    game = store.get(game_id)
    if game is not None and player_control.may_decide_for(game, seat, player):
        return seat
    return player


def _acting_as[Request](
    game_id: str, kind: str, action: str, req: Request, uid: str | None
) -> tuple[Request, dict[str, Any] | None]:
    """`req` as the engine should run it while one player controls another
    (`player_control.acting_as`, CR 723.5), or the refusal a controlled
    player's own seat gets for reaching for a decision that is not theirs
    this turn - recorded like any other refusal, and not at all for a dry
    run, which is never logged (`audit_outcome`).

    A no-op whenever no control effect is in force, which is almost always,
    so every route keeps behaving exactly as it did outside a controlled turn.
    """
    game = game_or_404(game_id)
    seat = game_access.seat_for_uid(game_id, uid)
    acting, reason = player_control.acting_as(game, kind, req, seat)
    if reason is None:
        return acting, None
    if getattr(req, "dry_run", False):
        return req, {"allowed": False, "reason": reason, "log_seq": None}
    seq = audit.record(
        game_id,
        action,
        player=getattr(req, "player", None),
        allowed=False,
        reason=reason,
        reasoning=getattr(req, "reasoning", None),
    )
    return req, {"allowed": False, "reason": reason, "log_seq": seq}


def _run_command(
    game_id: str, kind: str, request: Any, *, acting: tuple[Any, str | None] | None = None
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

    `concede` is exempt: it is legal "at any time" - CR 104.3a - which
    includes part-way through somebody else's command. A `dry_run` is not:
    it is asked the same question and given the same verdict - refused with
    the same `DecisionInProgressError`, or handed the standing pause when it
    names the paused command itself - because a probe that answers "yes" to
    a move the real call refuses is a wrong answer (battle 6, B6-09: a mana
    ability dry run allowed while a `pay_mana` decision stood). It still
    never writes the slot in either direction: a stale pause it finds is left
    for the next real command to clear.

    **A finished game refuses everything, `concede` and dry runs included**
    (`commands.GameOverError`, CR 104.1) - asked here under the lock as well
    as up front by `_refuse_after_game_over`.

    `store.clear_paused_command` still runs on every `COMPLETED` result, and
    the guard above is what makes that safe: the only slot it can now find
    is one this same call already proved stale, or one a `concede`
    deliberately walked past. The argument it used to rest on -
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
        commands.refuse_if_over(game, kind)
        if acting is not None:
            # `acting` is `(the request as the caller sent it, its uid)`. The
            # CR 723 rewrite (`_acting_as`) read the control effect before this
            # lock; CR 723.1 ends that effect at the turn boundary, so it is
            # asked again here, atomically with the command, and a request
            # whose answer changed in between is refused rather than run as a
            # player the caller no longer decides for.
            sent, uid = acting
            seat = game_access.seat_for_uid(game_id, uid)
            named = getattr(sent, "player", None)
            if (
                seat is not None
                and named
                and named != seat
                and not player_control.may_decide_for(game, seat, named)
            ):
                raise ValueError(
                    f"{seat} is no longer making {named}'s decisions - the effect ended "
                    "at the turn boundary (CR 723.1); read the board and act again"
                )
            now, reason = player_control.acting_as(game, kind, sent, seat)
            if reason is not None or getattr(now, "player", None) != getattr(
                request, "player", None
            ):
                raise ValueError(
                    reason
                    or "who is making this player's decisions changed while the request "
                    "was in flight (CR 723.1) - read the board and act again"
                )
        if kind != "concede":
            # A dry run is compared as the real command it asks about, and
            # never clears a stale slot - see this function's docstring.
            asked = (
                commands.Command(kind=kind, payload={**command.payload, "dry_run": False})
                if dry_run
                else command
            )
            standing = _pause_slot_guard(game_id, game, asked, clear_stale=not dry_run)
            if standing is not None:
                _, standing_tape, standing_pending = standing
                return (
                    commands.RunResult(
                        status=commands.RunStatus.PAUSED,
                        pending=standing_pending,
                        tape=standing_tape,
                    ),
                    game,
                    None
                    if dry_run
                    else records.legal_actions_before(game, player, standing_pending),
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
        # Both halves of "was this on offer" are in hand exactly here, and
        # nowhere else: the command as the request spelled it, and the
        # enumeration this api would have handed the same seat for the same
        # board, a line above the engine being asked anything. Whatever row
        # this request goes on to record is stamped from it
        # (`audit._with_offered`), which is what lets the scorecard count a
        # refusal of something never offered as the seat's own misreading and
        # a refusal of something offered as anything but.
        records.note_offered_action(command.kind, command.payload, legal_before)
        records.note_action_board(game_id, game)
        result = commands.run_command(game, command)
        seq: int | None = None
        if dry_run:
            pass
        elif result.status is commands.RunStatus.COMPLETED:
            assert result.game is not None
            engine_api.stamp_exile_turns(game, result.game)
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

    **And the game retires here too** (`lobby.retire_game`), which stops
    the `ai_pregame` watcher - a finished game has no pregame obligation
    left to settle - and releases nothing else. The AI seat's reflection
    turn, its Opus review and its handover are post-game work done on the
    seat's own uid, so each seat and buddy uid stays valid and each runner
    keeps running until the driver ends its own pod; releasing them here
    (as this once did) sent the driver down its no-reflection handover and
    failed the review of game 23868a95. `lobby.POSTGAME_RELEASE_SECONDS` is
    the bound on that work, and End game stays the hard stop. It hangs off
    this function rather than off `action_concede` because this is the one
    place every committed ending funnels through, so a lethal-damage sweep
    and a draw retire on the same line a concession does. The game is not
    deleted - see `lobby.retire_game`.
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
    lobby.retire_game(game_id)


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


def _paused_pending_view(
    game_id: str, pending: decisions.PendingDecision, viewer: visibility.Viewer
) -> dict[str, Any]:
    """`pending` as an acting route's paused response may carry it back to
    `viewer`, the actor this route already trusts (`req.player`, or the seat
    the uid resolved to for `decide` and the declaration aliases) - see
    `visibility.redact_paused_pending`. A resumed or fresh command can pause
    on a question for the *other* seat, and handing it back whole leaked
    that seat's candidates and the id of the card asking."""
    return visibility.redact_paused_pending(
        pending.to_view(), state_view(game_or_404(game_id), game_id), viewer
    )


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
            "pending_decision": _paused_pending_view(game_id, pending, keys.get("player")),
        }
        if dry_run:
            return {**body, "log_seq": None}
        # No `seq_hint` here: PAUSED means `_run_command` never called
        # `store.replace` at all - there is no row to fill in, so this is a
        # fresh standalone append, the same as any refusal caught before a
        # command ever ran.
        #
        # FD5 (3): `allowed=True` here, not `None` - a `PAUSED` result only
        # ever happens once the command has run far enough to be asked a
        # real question with no scripted answer, so it is a committing row
        # (`decision_dsl.decision_rows` reads `allowed is True` as its own
        # commitment test) rather than one that gets skipped as noise. The
        # HTTP response `body` above keeps its own `allowed: None` - this is
        # the persisted ledger row's shape only.
        seq = audit.record(
            game_id,
            action,
            allowed=True,
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
    stale (`resume_command`'s own replay-divergence guard). The first is not
    caught here; `POST .../decide` and its `actions/attack`/
    `actions/declare-blockers` aliases each turn it into their own
    synchronous `{"accepted": False, ...}` refusal. The second clears the
    stale pause first - a question that no longer replays can never be
    answered, and leaving it would 409 every answer forever - and then
    reaches this api's registered 409 handler, which tells the client the
    question expired and to re-issue the original action.
    """
    with store.locked(game_id) as game:
        commands.refuse_if_over(game, "decide")
        legal_before = records.legal_actions_before(game, player, pending)
        try:
            result = commands.resume_command(game, command, tape, pending, answer)
        except commands.CommandDivergenceError:
            # The stored question no longer replays, so no answer can ever be
            # accepted for it: cleared here, under the lock, so the game is not
            # stuck on it, and the 409 tells the client to re-issue the action.
            store.clear_paused_command(game_id)
            raise
        # The answer is taken on the board its question was asked on (B3-11).
        records.note_action_board(game_id, result.asked_on or game)
        seq: int | None = None
        if result.status is commands.RunStatus.COMPLETED:
            assert result.game is not None
            engine_api.stamp_exile_turns(game, result.game)
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
    if req.step == FIRST_STRIKE_DAMAGE:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{FIRST_STRIKE_DAMAGE}' is not a step a game can start in - it is an "
                "occurrence of 'combat_damage' (CR 510.4/506.1: the engine keeps one "
                "Step.COMBAT_DAMAGE for both the first-strike and the regular combat damage "
                "step), never a step of its own. Start in 'combat_damage' instead"
            ),
        )
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
                "step": step_position(game),
            }
        )
    return {"games": games}


@app.get("/api/games/{game_id}", response_model=GameStateView, response_model_exclude_unset=True)
def get_game(game_id: str, uid: str | None = None, include: str | None = None) -> dict[str, Any]:
    """The board, filtered to what `uid` may see (`visibility.py`).

    `include` is a comma-separated list of extra blocks. The only one is
    `contents`: `library_contents` and `sideboard_contents`, the caller's own
    library and sideboard as unordered `{card, count}` lists (CR 401.2 keeps
    the order from everyone). They are left off by default because the
    board is polled constantly and nothing on that path needs them. A value
    this route does not know is refused by name rather than ignored.

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
    included = {part.strip() for part in (include or "").split(",") if part.strip()}
    unknown = sorted(included - {"contents"})
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown include value(s) {unknown}; the only one is 'contents'",
        )

    def read() -> dict[str, Any]:
        game = game_or_404(game_id)
        viewer = visibility.resolve_viewer(game_id, uid)
        view = state_view(game, game_id, include_contents="contents" in included)
        return visibility.redact_view(view, viewer)

    # Never inside the pregame's last move. This route takes no game lock by
    # design (`tests/test_action_flow.py`'s
    # `test_no_deadlock_block_until_wake_never_holds_the_game_lock`), and
    # `opening.finish_opening_hand_actions` edits the stored `Game` in place -
    # so without this a poll could read it after the pregame ended and before
    # CR 103.8's untap step was settled, and be handed the frozen page the
    # settling exists to prevent.
    #
    # The whole read runs inside `handoff.stable_read`, not merely a wait
    # before it: every line of `read` above runs against the live `Game` the
    # store hands back by reference, so a check made before it starts says
    # nothing about a handoff that opens while it is still building. The
    # view is checked afterwards too, and rebuilt if one did - see
    # `handoff.py`.
    #
    # This is not the game lock: it costs nothing unless that one mutation
    # is actually running, so `POST /advance` and `PUT /step` still observe
    # a priorityless step on purpose, and holding `store.locked` alone gates
    # nothing.
    return handoff.stable_read(game_id, read)


@app.get(
    "/api/games/{game_id}/playscripts/hypothetical-facts",
    response_model=HypotheticalFactsResponse,
    response_model_exclude_unset=True,
)
def get_hypothetical_facts(
    game_id: str, uid: str | None = None, horizon: str | None = None
) -> dict[str, Any]:
    """O4's `enters_any` and O7's `unlocks`/`unlocks_by_search`/
    `unlocks_by_basic_search`: everything a play's selectors can ask about
    `uid`'s own hand and library lands and cards, computed once against the
    live game (`hypothetical_facts.build_hypothetical_facts`) rather than
    read off a polled view.

    `horizon` is T4's on-demand second pass: unset (the default), the answer
    is byte for byte what this route always returned. `horizon=next_turn`
    additionally runs `build_hypothetical_facts(..., next_turn=True)`, adding
    T5's `payable_any` (every hand card) and `unlocks_next` (the same lands
    `unlocks` is already offered on) - the board with `uid`'s own untap step
    already taken, and nothing else about the turn. Any other value is
    refused with a 422 rather than silently ignored, the same posture
    `get_game`'s own `include` takes. `conditions.needs_next_turn` tells a
    caller when a play's selectors actually need this second pass at all.

    Only a real seat may ask - `uid` must resolve to one of this game's own
    players, never the admin key and never nobody, since a fact here is
    always "what would happen for this specific player", and every engine
    call this makes (`predict_enters`/`payability`/`payability_matrix`, in
    `hypothetical_facts.py`) already refuses a card outside that player's own
    hand or library, so nothing about an opponent's cards is ever computed
    or returned - not merely redacted after the fact, as `get_game`'s view
    is, but never asked about in the first place.

    A pending `choose_cards` decision for `uid` itself (a fetch's own search,
    CR 701.23a) gets one thing more: `enters`/`unlocks` facts for every id
    that decision's own `cards` offers, library lands included - never for
    any other library card, and never anything about the library's order.
    Read straight off the live paused command (`store.paused_command`, the
    same source `serialize.state_view`'s own `pending_decision` reads) rather
    than off a fetched view, so this never asks for a decision belonging to
    another player or of any other kind. With no such decision pending, or
    one pending for someone else, this withholds every library card's facts
    exactly as before.

    Meant to be called sparingly: `conditions.needs_hypothetical` tells a
    caller when a play's conditions need this at all, and the runtime caches
    the answer until the board changes rather than calling this once per
    judgement of every script it checks."""
    if horizon is not None and horizon != "next_turn":
        raise HTTPException(
            status_code=422,
            detail=f"unknown horizon value {horizon!r}; the only one is 'next_turn'",
        )
    game = game_or_404(game_id)
    viewer = visibility.resolve_viewer(game_id, uid)
    if not isinstance(viewer, str):
        raise KeyError("that uid is no seat in this game")
    candidate_ids = None
    paused = store.paused_command(game_id)
    if paused is not None:
        decision = paused[2].decision
        if decision.player == viewer and decision.kind is DecisionKind.CHOOSE_CARDS:
            candidate_ids = frozenset(str(c) for c in decision.cards)
    return build_hypothetical_facts(
        game, viewer, candidate_ids=candidate_ids, next_turn=horizon == "next_turn"
    )


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

    **An ended game's log still reads for its own credentials.** End game
    and Forget soft-delete the game, but the ledger outlives it, and a
    seat's driver reads its own script runs off this route for its
    handover. So a game that is gone from the live store is answered to
    its admin key and its seat uids from the archive
    (`_ended_game_log_viewer`), and is a 404 to anyone else.

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
    viewer = (
        visibility.resolve_viewer(game_id, uid)
        if store.get(game_id) is not None
        else _ended_game_log_viewer(game_id, uid)
    )
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
        # `None` is a row this viewer may not know exists (a script run), so
        # it is left out; `last_seq` above already counts it, so omitting it
        # never moves the caller's watermark back.
        log = [
            redacted
            for entry in log
            if (redacted := visibility.redact_log_entry(entry, viewer)) is not None
        ]
    # A `report_problem` row shows its own `category` (`engine`/`dsl`/`ux`)
    # here, normalized for a row written before that field existed
    # (`audit.normalize_problem_category`) - never left to whatever the
    # stored payload happens to carry, admin viewer included.
    log = [audit.normalize_problem_category(entry) for entry in log]
    return {"log": log, "last_seq": last_seq}


def _ended_game_log_viewer(game_id: str, uid: str | None) -> visibility.Viewer:
    """Who `uid` is in a game End game (or Forget) has already soft-deleted,
    for `game_log` - or a 404, exactly as for an unknown game.

    The ledger outlives the game on purpose (`repository.py`'s `delete`), and
    the seat's driver reads its own script runs off this route for its
    handover. Before this an ended game 404'd here, so a handover that read
    the log after an End or a Forget found no script runs at all (A3 clause
    0, `tests/test_post_game_log.py`). Authorised against the credentials the
    archive kept, as `game_review` and `declare_reflection` are: the game's
    admin key or one of its seat uids, and nobody else - a caller presenting
    nothing learns no more about an ended game than about one that never
    existed."""
    archived = store.archived(game_id)
    if archived is None or not uid:
        raise KeyError(f"no game {game_id}")
    if archived.admin_key is not None and uid == archived.admin_key:
        return visibility.ADMIN
    player = archived.seat_uids.get(uid)
    if player is None:
        raise KeyError(f"no game {game_id}")
    return player


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


def _review_access(
    game_id: str, uid: str | None, key: str | None
) -> tuple[ArchivedGame, visibility.Viewer, list[dict[str, Any]]]:
    """The credential + post-game gate every review-gated route sits behind
    (R2): `game_review` and `game_review_frame` both call this and only
    this, so the two routes can never drift into checking the gate two
    slightly different ways.

    Extracted verbatim from what `game_review` always did, in the same
    order, with the same messages:

    - 404 (`KeyError`, same text) for a game that was never created, or for
      a `uid`/`key` that is neither this game's admin key nor one of its
      seat uids - checked against the *archived* row's own credentials, so
      this still works after the game has ended. See `game_review`'s own
      docstring for why this is deliberately not `game_or_404`.
    - 409 (same detail) while the game is neither over (`Game.is_over`) nor
      ended (`archived.deleted_at`) - a review shows both seats' plans and
      reasoning side by side, which is only ever safe post-game.
    - the `SCRIPT_RUN_ACTIONS` filter (`visibility.hides_script_run`): a
      play-script run row never reaches anyone but the admin key and the
      seat it ran for, post-game exactly as it never does live.

    Returns `(archived, viewer, ledger)` - the archived row, who the
    presented credential resolves to (`visibility.ADMIN` or a seat
    username), and the ledger already filtered by the script-run rule. Every
    other post-game field (`declarations`, `advice`, a review's own head) is
    built from `ledger`, never from `audit.entries` again.
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
    viewer: visibility.Viewer = (
        visibility.ADMIN
        if archived.admin_key is not None and archived.admin_key in presented
        else next(archived.seat_uids[value] for value in presented if value in archived.seat_uids)
    )
    ledger = entries if entries is not None else audit.entries(game_id)
    ledger = [entry for entry in ledger if not visibility.hides_script_run(entry, viewer)]
    return archived, viewer, ledger


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
    to the person it is for. This is a deliberate decision, stated explicitly
    because it is easy to make by accident otherwise: `review.build_review`'s
    `advice` stream (`review.ReviewAdvice`) carries the mtg-buddy advisor's
    `summary`/`reasoning`/`body` for a human seat, which can name cards that
    were in that seat's own hand at the time it was proposed - and there is
    no redaction of it here, same as `declarations`. The opponent reads it
    once the game is over, exactly as the advised seat does (see `test_review
    .py`'s `test_the_opponent_reads_the_same_advice_the_human_seat_does`).

    **The one exception is a play-script run row** (`audit.SCRIPT_RUN_ACTIONS`),
    dropped from `entries` for every caller but the admin key and the seat the
    run played for (`visibility.hides_script_run`). A run names the seat's
    guides revision, the archetype it believed it faced and its refusals; it
    is a fact about the runner, not about the game, and never reaches the
    opponent, during the game or after it.
    """
    archived, viewer, ledger = _review_access(game_id, uid, key)
    # A `report_problem` row shows its own `category` here too, normalized
    # for a row written before the field existed - same helper, same reason,
    # as `game_log` above (`audit.normalize_problem_category`).
    ledger = [audit.normalize_problem_category(entry) for entry in ledger]
    seat = viewer if isinstance(viewer, str) else None
    # OM1: one build per game at a time (the second caller waits, then reads
    # the first one's payload), cached under a fingerprint of everything the
    # payload is built from - see `review_cache`.
    with _REVIEW_CACHE.building(game_id):
        exact_frames = _exact_frames(game_id, archived, ledger)
        corpora = _review_corpora(game_id, archived, list(archived.seat_uids.values()))
        version = _review_version(archived, ledger, exact_frames, corpora)
        cached = _REVIEW_CACHE.get(game_id, seat, version)
        if cached is not None:
            return cached
        built = review.build_review(
            archived,
            ledger,
            exact_frames=exact_frames,
            viewer=seat,
            frame_views=_review_frame_views(game_id, exact_frames),
            corpora=corpora,
        )
        _REVIEW_CACHE.put(game_id, seat, version, built)
        return built


#: OM1: the per-game build lock and the finished-review LRU `game_review`
#: reads through.
_REVIEW_CACHE = review_cache.ReviewCache()


def clear_review_cache() -> None:
    """Forget every cached review - for a test that must see a fresh build."""
    _REVIEW_CACHE.clear()


def _exact_frames(game_id: str, archived: ArchivedGame, ledger: list[dict[str, Any]]) -> list[int]:
    """R2: which of this review's own (already filtered) rows also have an
    exact engine state recorded - `[]` on a `reconstructed` review (there is
    no `archived.game` to have been read back from when any of those rows
    were written, so nothing before the schema bump was ever
    `put_record_state`-able) and on any older game nothing ever recorded a
    state for. Computed here, not inside `review.py`, so that module reads
    no store at all (see `review.build_review`'s own docstring)."""
    if archived.game is None:
        return []
    ledger_seqs = {entry["seq"] for entry in ledger}
    return sorted(seq for seq in store.record_state_seqs(game_id) if seq in ledger_seqs)


def _review_version(
    archived: ArchivedGame,
    ledger: list[dict[str, Any]],
    exact_frames: list[int],
    corpora: Mapping[str, Any],
) -> str:
    """OM1: a digest of every input `review.build_review` reads, so a cached
    review is served only while none of them changed - a row appended or
    filled in, a state recorded, the game ended, a corpus registered. Hashed
    row by row rather than dumped whole, so a long ledger is never one big
    string in memory."""
    game = archived.game
    digest = hashlib.blake2b(digest_size=16)

    def feed(value: Any) -> None:
        digest.update(json.dumps(value, sort_keys=True, default=str).encode())

    feed(
        [
            archived.game_id,
            archived.created_at,
            archived.deleted_at,
            archived.unloadable,
            archived.seat_uids,
            exact_frames,
            corpora,
            None
            if game is None
            else [
                [player.username for player in game.players],
                game.is_over,
                game.winner,
                game.drawn,
            ],
        ]
    )
    for entry in ledger:
        feed(entry)
    return digest.hexdigest()


def _review_frame_views(game_id: str, exact_frames: list[int]) -> dict[int, dict[str, Any]]:
    """DR2: every `seq` in `exact_frames`, decoded and turned into the
    omniscient board `GET .../review/frames/{seq}` itself serves
    (`serialize.frame_view`) - **one** batched store read
    (`store.record_states_for_seqs`), never one `store.record_state` per
    `seq`, so a long game's review costs this route one round trip rather
    than one per decision. Skips whatever that batched read itself skips (an
    unrecorded or undecodable `seq` - see its own docstring); the review
    still renders, with `board`/`board_by_perspective: None` for those rows
    only.

    OM1: that read yields one decoded `Game` at a time, and each is dropped
    as soon as its board view exists - only the small views accumulate,
    never the states (~1.7 MB of heap each; all of a 250-decision game's at
    once was the prod OOM)."""
    if not exact_frames:
        return {}
    views: dict[int, dict[str, Any]] = {}
    states = iter(store.record_states_for_seqs(game_id, exact_frames))
    while True:
        with _one_state_at_a_time():
            item = next(states, None)
            if item is None:
                return views
            seq, game = item
            views[seq] = serialize.frame_view(game, game_id, you=None)
            del item, game


_GC_PAUSE_LOCK = threading.Lock()
#: [how many `_one_state_at_a_time` blocks are open, whether automatic
#: collection was on when the first of them opened].
_GC_PAUSE = [0, False]


@contextmanager
def _one_state_at_a_time() -> Iterator[None]:
    """OM1: free one decoded state before the next is read.

    A decoded `Game` is cyclic (its players and continuous effects point
    back at it), so `del` alone frees nothing: it waits for the cyclic
    collector, and the thousands of objects a decode allocates trigger young
    collections that promote the still-live state to generation 2 - which a
    long-lived process collects rarely (CPython defers a full collection
    until a quarter of its long-lived heap is new). Streaming then bought
    nothing: prod-like runs measured +226 MB for a 250-state review.

    So automatic collection is paused for the block - nothing allocated in
    it is promoted - and a generation-0 collection on the way out frees the
    dropped state. That scan covers only what is young, so it stays cheap
    however large the heap. Nesting and concurrent callers share one pause;
    the last one out restores collection as the first one found it."""
    with _GC_PAUSE_LOCK:
        if _GC_PAUSE[0] == 0:
            _GC_PAUSE[1] = gc.isenabled()
            gc.disable()
        _GC_PAUSE[0] += 1
    try:
        yield
    finally:
        gc.collect(0)
        with _GC_PAUSE_LOCK:
            _GC_PAUSE[0] -= 1
            if _GC_PAUSE[0] == 0 and _GC_PAUSE[1]:
                gc.enable()


def _review_corpora(
    game_id: str, archived: ArchivedGame, players: list[str]
) -> dict[str, dict[str, Any]]:
    """DR2: `seat -> its own registered play id -> play doc mapping`, for
    every player named in this review's head - the seat's own uid corpus
    (`store.scripts`), never an advisor's, and `{}` for a seat that never
    registered one (a human seat, or one this build predates `SCRIPTS_
    ENABLED` for)."""
    corpora: dict[str, dict[str, Any]] = {}
    for player in players:
        uid = next((u for u, name in archived.seat_uids.items() if name == player), None)
        registered = store.scripts(game_id, uid) if uid is not None else None
        corpora[player] = (registered or {}).get("scripts") or {}
    return corpora


@app.get(
    "/api/games/{game_id}/review/frames/{seq}",
    response_model=GameStateView,
    response_model_exclude_unset=True,
)
def game_review_frame(
    game_id: str, seq: int, uid: str | None = None, key: str | None = None
) -> dict[str, Any]:
    """R2: one exact, byte-accurate board from a finished game's ledger -
    `seq` of a `GET .../review` row, not a live board's turn number.

    Behind the exact same gate `game_review` is: `_review_access` (404 for
    an unknown game or a credential that is neither the admin key nor one of
    this game's own seat uids, 409 while the game is neither over nor ended,
    the `script_run` filter). `seq` must be one of the *filtered* ledger's
    own rows - a row `visibility.hides_script_run` would have dropped from
    the opponent's own `entries` is a 404 for that opponent here too, same
    as it is missing from their `entries` list - and it must have a state
    `audit.record` actually recorded (`store.put_record_state` is
    best-effort; a row it failed for, or one older than R2, has none). Both
    are answered the same way: `KeyError` -> this api's own 404.

    A stored state this build cannot decode is not softened here any more
    than it is at `GET /api/games/{id}` - `store.record_state` raises
    `GameUnloadable`, and the app-wide handler answers 409 with the
    decoder's own reason verbatim.

    The board itself is `serialize.frame_view` - see that function's own
    docstring for why it is omniscient (both hands, no redaction) and why it
    needs no paused-command lookup.
    """
    archived, viewer, ledger = _review_access(game_id, uid, key)
    if seq not in {entry["seq"] for entry in ledger}:
        raise KeyError(f"no frame {seq} for game {game_id}")
    game = store.record_state(game_id, seq)
    if game is None:
        raise KeyError(f"no recorded state at seq {seq} for game {game_id}")
    return serialize.frame_view(game, game_id, you=viewer if isinstance(viewer, str) else None)


_ADMIT_FIELDS = ("added", "updated", "reprioritized")


def _ops_game_review(game_id: str) -> dict[str, Any] | None:
    """One game's own review, built the same way `game_review` does but for
    `GET /api/ops/dsl-coverage`'s already-authorized-across-every-game
    caller: no per-game credential to present (`records.
    authorized_ops_caller` gated the whole request once, at the route), and
    always the admin's own full, unredacted view (`visibility.ADMIN`) -
    aggregate coverage needs every seat's rows, not any one seat's own
    filtered read. `None` for a game still in progress (`_post_game` false)
    or one this store never actually held (should not happen for an id
    `store.ids_since` itself returned, but never trusted blindly)."""
    archived = store.archived(game_id)
    if archived is None:
        return None
    entries = _gate_entries(archived)
    if not _post_game(archived, entries):
        return None
    ledger = entries if entries is not None else audit.entries(game_id)
    ledger = [entry for entry in ledger if not visibility.hides_script_run(entry, visibility.ADMIN)]
    ledger = [audit.normalize_problem_category(entry) for entry in ledger]
    exact_frames = _exact_frames(game_id, archived, ledger)
    corpora = _review_corpora(game_id, archived, list(archived.seat_uids.values()))
    return review.build_review(
        archived,
        ledger,
        exact_frames=exact_frames,
        viewer=None,
        frame_views=_coverage_frame_views(game_id, archived, ledger, exact_frames),
        corpora=corpora,
        with_boards=False,
    )


def _coverage_frame_views(
    game_id: str, archived: ArchivedGame, ledger: list[dict[str, Any]], exact_frames: list[int]
) -> dict[int, dict[str, Any]]:
    """OM1: only the frames coverage's conversions actually read
    (`decision_dsl.frame_seqs_read`), streamed like `_review_frame_views`
    and never turned into a board. A frame that turns out undecodable is
    dropped from what is `available` and the needed set recomputed - the
    conversion then falls back to an earlier frame exactly as it does when
    every frame is attached - until nothing new is needed."""
    if archived.game is None or not exact_frames:
        return {}
    players = [player.username for player in archived.game.players]
    available = set(exact_frames)
    views: dict[int, dict[str, Any]] = {}
    while True:
        needed = decision_dsl.frame_seqs_read(ledger, players, available)
        missing = sorted(needed - views.keys())
        if not missing:
            return views
        read = _review_frame_views(game_id, missing)
        views.update(read)
        available -= set(missing) - read.keys()


def _admit_totals(play_stats: Mapping[str, Any], *, reviewer: bool) -> dict[str, int]:
    """`{added, updated, reprioritized}`, summed across every seat's own
    `ReviewPlayStats` in `play_stats` - or, with `reviewer=True`, across
    each one's own nested `reviewer` stats instead (FD5 (6))."""
    totals = {field: 0 for field in _ADMIT_FIELDS}
    for seat_stats in play_stats.values():
        source = (seat_stats or {}).get("reviewer", {}) if reviewer else (seat_stats or {})
        for field in _ADMIT_FIELDS:
            totals[field] += int(source.get(field) or 0)
    return totals


def _fold_coverage(
    into: dict[str, dict[str, Any]], coverage: Mapping[str, Mapping[str, Any]]
) -> None:
    """Fold one game's `review.dsl_coverage` result into a running
    aggregate, in place - `count`/`unexpressible` summed, `labels` unioned."""
    for kind, entry in coverage.items():
        bucket = into.setdefault(kind, {"count": 0, "unexpressible": 0, "labels": set()})
        bucket["count"] += entry.get("count", 0)
        bucket["unexpressible"] += entry.get("unexpressible", 0)
        bucket["labels"] = set(bucket["labels"]) | set(entry.get("labels") or ())


@app.get(
    "/api/ops/dsl-coverage",
    response_model=OpsDslCoverageResponse,
    response_model_exclude_unset=True,
)
def ops_dsl_coverage(
    since: str | None = None, limit: int = 200, key: str | None = None
) -> dict[str, Any]:
    """FD5 (6): per-game and aggregated `playscripts` DSL coverage across
    every game since `since` (an ISO 8601 timestamp; omitted reads every
    game this store has ever held), using the same `review.dsl_coverage`
    function `GET /api/games/{id}/review` already calls once per game -
    `{kind: {count, unexpressible, labels}}` - folded together here across
    every finished game, plus each game's own reviewer/handover admit
    counts (`_admit_totals`).

    This is what makes prod coverage measurable without a per-game admin
    key: every existing "admin" credential in this codebase is scoped to
    one game (`store.admin_key`), and there is no cross-game listing route
    that reads past the soft delete the way this one has to (most finished
    games are archived, not live, by the time anyone asks) - see `store.
    ids_since`'s own docstring.

    Behind `key`, checked against this deployment's own `OPS_TOKEN`
    (`records.authorized_ops_caller`) - an env-configured operator
    credential, never a per-game one, and refused (404, naming no game and
    confirming nothing) for any caller that does not present it, including
    an unconfigured deployment (`OPS_TOKEN` unset refuses everyone rather
    than falling open).

    `limit` bounds how many games (oldest-first, per `ids_since`) this one
    call will build a full review for - each one is real ledger work, and
    this route is meant for a periodic operator sweep, not a hot path.
    """
    if not records.authorized_ops_caller(key):
        raise KeyError("no ops token presented, or it does not match this deployment's own")
    game_ids = store.ids_since(since)[:limit]
    games: list[dict[str, Any]] = []
    aggregated: dict[str, dict[str, Any]] = {}
    handover_totals = {field: 0 for field in _ADMIT_FIELDS}
    reviewer_totals = {field: 0 for field in _ADMIT_FIELDS}
    for game_id in game_ids:
        built = _ops_game_review(game_id)
        if built is None:
            continue
        coverage = built.get("dsl_coverage") or {}
        play_stats = built.get("play_stats") or {}
        handover = _admit_totals(play_stats, reviewer=False)
        reviewer = _admit_totals(play_stats, reviewer=True)
        _fold_coverage(aggregated, coverage)
        for field in _ADMIT_FIELDS:
            handover_totals[field] += handover[field]
            reviewer_totals[field] += reviewer[field]
        games.append(
            {
                "game_id": game_id,
                "created_at": built.get("created_at"),
                "dsl_coverage": coverage,
                "handover_admits": handover,
                "reviewer_admits": reviewer,
            }
        )
    return {
        "since": since,
        "games": games,
        "aggregated": {
            kind: {**entry, "labels": sorted(entry["labels"])} for kind, entry in aggregated.items()
        },
        "handover_admits": handover_totals,
        "reviewer_admits": reviewer_totals,
    }


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
    handed the caller a seat or an admin key for.

    The deletion itself is `lobby.reset_game`, the same release End game
    runs, under this route's own audit action - see `lobby.py` for why
    deleting without releasing left a watcher polling, a seat process alive,
    a registry answering for uids of a game that no longer existed, and (in
    `wrapper` mode) a driver that 409s every later dispatch for the life of
    its container."""
    game = store.get(game_id)
    if game is None:
        raise KeyError(f"no game {game_id}")
    allowed = (uid is not None and store.player_for_uid(game_id, uid) is not None) or (
        key is not None and store.is_admin(game_id, key)
    )
    if not allowed:
        raise KeyError("that uid is no seat in this game")
    lobby.reset_game(game_id, reason="deleted outright", action="delete_game")
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
    if req.step == FIRST_STRIKE_DAMAGE:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{FIRST_STRIKE_DAMAGE}' is not a step to move to - it is an occurrence of "
                "'combat_damage' (CR 510.4/506.1: the engine keeps one Step.COMBAT_DAMAGE for "
                "both the first-strike and the regular combat damage step). Move to "
                "'combat_damage' instead; whether that lands you in the first-strike occurrence "
                "is CR 510.4's own doing, not this route's, and the state view's "
                "'first_strike_damage_step' flag says which one you are in"
            ),
        )
    records.require_current_board(
        game_id, "step", req.expected_seq, player=req.player, reasoning=req.reasoning
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    with store.locked(game_id) as game:
        before = step_position(game)
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
    dependencies=REFUSED_AFTER_GAME_OVER,
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_cast(game_id: str, req: CastRequest, uid: str | None = None) -> dict[str, Any]:
    """Cast a card. `dry_run` never asks anything unscripted (it builds and
    rewinds its `Casting` against an isolated probe copy of the game - see
    `engine_api.dry_run_copy` - never through the pause/resume tape), so it
    always completes; a real cast can pause on any CR 601.2 choice this
    engine still asks interactively (`choose_targets`, `choose_a_mode`, ...)."""
    sent = req
    actor = _acting_seat(game_id, req.player, uid)
    req, refused = _acting_as(game_id, "cast", "cast", req, uid)
    if refused is not None:
        return refused
    records.require_current_board(
        game_id,
        "cast",
        req.expected_seq,
        player=actor,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, actor, req.reasoning)
    records.require_plan(game_id, actor)
    result, committed, legal_before, seq = _run_command(game_id, "cast", req, acting=(sent, uid))
    return _respond(
        game_id,
        "cast",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card=req.card or str(req.card_id),
        # Where the card was, not the request's `hand` default (B3-15).
        from_zone=engine_api.cast_origin_zone(committed, req),
        spell_face=req.spell_face,
        targets=req.targets,
        # FD5 (1): every other `CastRequest` field the announcement can
        # carry, `mana_sources` alone excepted (on purpose - see the plan).
        keyword=req.keyword,
        alternative_cost=req.alternative_cost,
        modes=req.modes,
        x_value=req.x_value,
        shares=req.shares,
        optional_costs=[cost.model_dump() for cost in req.optional_costs or []],
        convoke=[
            {"card_id": str(choice.card_id), "color": choice.color} for choice in req.convoke or []
        ],
        delve=[str(card_id) for card_id in req.delve or []],
        improvise=[str(card_id) for card_id in req.improvise or []],
        choosing=[str(card_id) for card_id in req.choosing or []],
        sacrificing=[str(card_id) for card_id in req.sacrificing or []],
        discarding=[str(card_id) for card_id in req.discarding or []],
        exiling=[str(card_id) for card_id in req.exiling or []],
        tapping=[str(card_id) for card_id in req.tapping or []],
        mana_sources=[str(source) for source in req.mana_sources or []],
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        seq_hint=seq,
    )


@app.post(
    "/api/games/{game_id}/actions/suspend",
    dependencies=REFUSED_AFTER_GAME_OVER,
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_suspend(
    game_id: str, req: SpecialActionRequest, uid: str | None = None
) -> dict[str, Any]:
    """CR 116.2f/702.62a: exile a card with suspend from hand, paying its
    suspend cost, with N time counters. The free cast once the last counter
    comes off is picked up automatically by `actions/cast` - see `advance`."""
    sent = req
    actor = _acting_seat(game_id, req.player, uid)
    req, refused = _acting_as(game_id, "suspend", "suspend", req, uid)
    if refused is not None:
        return refused
    records.require_current_board(
        game_id,
        "suspend",
        req.expected_seq,
        player=actor,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, actor, req.reasoning)
    records.require_plan(game_id, actor)
    result, _, legal_before, seq = _run_command(game_id, "suspend", req, acting=(sent, uid))
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
    dependencies=REFUSED_AFTER_GAME_OVER,
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_plot(game_id: str, req: SpecialActionRequest, uid: str | None = None) -> dict[str, Any]:
    """CR 116.2k/702.170b: exile a card with plot from hand, paying its plot
    cost. Cast it later with `actions/cast`, `keyword: "Plot"`, on a later turn."""
    sent = req
    actor = _acting_seat(game_id, req.player, uid)
    req, refused = _acting_as(game_id, "plot", "plot", req, uid)
    if refused is not None:
        return refused
    records.require_current_board(
        game_id,
        "plot",
        req.expected_seq,
        player=actor,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, actor, req.reasoning)
    records.require_plan(game_id, actor)
    result, _, legal_before, seq = _run_command(game_id, "plot", req, acting=(sent, uid))
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
    dependencies=REFUSED_AFTER_GAME_OVER,
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_foretell(
    game_id: str, req: SpecialActionRequest, uid: str | None = None
) -> dict[str, Any]:
    """CR 116.2h/702.143b: pay {2} and exile a card with foretell from hand,
    face down. Cast it later with `actions/cast`, `keyword: "Foretell"`, on a
    later turn, for its printed foretell cost."""
    sent = req
    actor = _acting_seat(game_id, req.player, uid)
    req, refused = _acting_as(game_id, "foretell", "foretell", req, uid)
    if refused is not None:
        return refused
    records.require_current_board(
        game_id,
        "foretell",
        req.expected_seq,
        player=actor,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, actor, req.reasoning)
    records.require_plan(game_id, actor)
    result, _, legal_before, seq = _run_command(game_id, "foretell", req, acting=(sent, uid))
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
    dependencies=REFUSED_AFTER_GAME_OVER,
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_cycle(game_id: str, req: CycleRequest, uid: str | None = None) -> dict[str, Any]:
    """CR 702.29a: discard a card with cycling from hand, paying its cycling
    cost, to draw a card (or search for a typecycled one). Puts the ability on
    the stack, same as `actions/activate` - resolve it with `resolve-top`."""
    sent = req
    actor = _acting_seat(game_id, req.player, uid)
    req, refused = _acting_as(game_id, "cycle", "cycle", req, uid)
    if refused is not None:
        return refused
    records.require_current_board(
        game_id,
        "cycle",
        req.expected_seq,
        player=actor,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, actor, req.reasoning)
    records.require_plan(game_id, actor)
    result, _, legal_before, seq = _run_command(game_id, "cycle", req, acting=(sent, uid))
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
    dependencies=REFUSED_AFTER_GAME_OVER,
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
    spell_face: str | None = None,
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

    `spell_face` is the half being cast, for a split or adventurer card (CR
    709.3, CR 715.3) - the same string `Action.spell_face` publishes and
    `CastRequest.spell_face` takes back. CR 709.3b makes the chosen half's
    own text the one the announcement is read against, so asking without it
    answers the front half's targeting for both halves: Wear // Tear offered
    Wear's "target artifact" while Tear was being cast.

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
    return read_on_one_board(
        lambda: _affordances_of(
            game, state, player, ability_index, mode_numbers, x_value, spell_face
        ),
        game,
    )


def _affordances_of(
    game: Game,
    state: CardState,
    player: str,
    ability_index: int | None,
    mode_numbers: list[int] | None,
    x_value: int | None,
    spell_face: str | None = None,
) -> dict[str, Any]:
    """`get_affordances`' answer, on one layered board."""
    result = affordances.candidates(
        game,
        state,
        player,
        ability_index=ability_index,
        modes=mode_numbers,
        x_value=x_value,
        spell_face=spell_face,
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
    game: Game, actions: Sequence[Any], views: list[dict[str, Any]]
) -> None:
    """Fold each targeting action's legal candidates into its own entry.

    The candidates are the engine's own: `legal_actions` puts them on every
    `Action.target_slots` entry (`TargetSlot.candidates` and
    `player_candidates`) from the predicate CR 601.2c's announcement checks
    with, so a list here cannot disagree with the announcement that follows.
    `affordances.slot_candidates` only writes them as the strings a seat
    hands back, capped - in the request that already enumerated the actions,
    never a second call per action.

    Two kinds of row are skipped, and each for its own reason:

    - **nothing to enumerate.** An empty `target_slots` means this action does
      not target (or its phrase was unreadable, which `Action` cannot tell
      apart), so there is no slot to fill and no key worth the bytes.
    - **`affordable == "no"`.** CR 118.6 keeps the row - an unpayable cast is
      still a legal one and is still listed - but a candidate list for a spell
      that cannot be cast is a list the seat can do nothing with. Said once,
      in `seatkit._actions_view`'s envelope, rather than as a key on every
      such row.

    `unknown` is emphatically not `no` and is enriched: an X spell before its
    X is announced (CR 107.3a) may well be castable.
    """
    for action, view in zip(actions, views, strict=True):
        if not action.target_slots or view.get("affordable") == "no":
            continue
        view["targets"] = affordances.slot_candidates(game, action.target_slots)


#: The pauses whose rows are enumerated on the paused board (`get_legal_actions`).
_DECLARATION_KINDS = frozenset({DecisionKind.DECLARE_ATTACKERS, DecisionKind.DECLARE_BLOCKERS})


def _rows_while_controlling(
    game: Game, player: str, pending: PendingDecisionEngine | None
) -> tuple[list[Any], list[dict[str, Any]]]:
    """`legal_action_rows` for `player`, as one player controlling another
    changes them (CR 723).

    - `player` is being controlled: CR 723.5 leaves them no choice of their
      own but CR 723.6's concession, so that is all they are offered.
    - `player` is controlling somebody: that player's rows are added after
      `player`'s own, each stamped `acting_for` with their name. Their
      `player` field names the controlled player, and every route takes the
      row as it stands from the controller's uid. Conceding is left out -
      CR 723.6: "The controller of another player can't make that player
      concede."

    Exactly `legal_action_rows` whenever nobody is controlled."""
    actions, views = legal_action_rows(game, player, pending)
    if player_control.decider(game, player) != player:
        kept = [
            (action, view)
            for action, view in zip(actions, views, strict=True)
            if view.get("kind") == "concede"
        ]
        return [action for action, _ in kept], [view for _, view in kept]
    for controlled in player_control.deciding_for(game, player):
        theirs, their_views = legal_action_rows(game, controlled, pending)
        for action, view in zip(theirs, their_views, strict=True):
            if view.get("kind") == "concede":
                continue
            actions.append(action)
            views.append({**view, "acting_for": controlled})
    return actions, views


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
    its next action out of one answer. Opt-in: the enumeration and its
    candidates share one layered board (`board_scope.read_on_one_board`), so
    the candidates no longer rebuild it per target check, but walking every
    public object against every slot is still work the browser - which asks
    this on a 1.2s poll and already has the whole board on screen to pick
    targets from - has no use for. A seat pays it once per action and saves a
    whole round trip, and so a whole inference cycle. So
    `seatkit.available_actions` passes it and `ui/app.js` does not, and the
    default is off: a caller that says nothing pays nothing.

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
    # Read *before* the board, never after. This number goes back to the
    # caller as the position this answer describes, and a caller names it as
    # `expected_seq` on the action it decides to take - so it has to be a
    # position the board this enumeration was computed from is at least as
    # new as. Read afterwards, a write landing in between would hand a seat a
    # number newer than the board it actually read, and
    # `require_current_board` would then stamp its refusal `stale: False` -
    # "decided against a current board" - about a board that had already
    # moved. Failing the other way costs a re-read and says so.
    at_seq = audit.last_seq(game_id)
    game = game_or_404(game_id)
    viewer = visibility.resolve_viewer(game_id, uid)
    if (
        viewer is not visibility.ADMIN
        and viewer != player
        # CR 723.4/723.5: the player `viewer` is controlling right now - their
        # hand is `viewer`'s to see and their choices `viewer`'s to make.
        and not (isinstance(viewer, str) and player_control.may_decide_for(game, viewer, player))
    ):
        raise ValueError(
            f"that uid does not open {player}'s seat - these are that player's own "
            "legal actions, and naming them is not enough to read them"
        )
    paused = store.paused_command(game_id)
    pending = paused[2] if paused is not None else None
    board = game
    if pending is not None and pending.decision.kind in _DECLARATION_KINDS:
        # CR 508.1/509.1's question is raised in the declare step, but a pause
        # is never committed, so `game` is still in the step before it and the
        # engine enumerates no `declare_attacker`/`declare_blocker` pair for a
        # board outside that step. The board the question was raised on is
        # what the rows describe (U1: a stolen, hasty creature's attack row).
        board = commands.board_at_pause(game, paused[0], paused[1]) or game

    def rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        actions, views = _rows_while_controlling(board, player, pending)
        if candidates:
            _attach_target_candidates(board, actions, views)
        # F1: the companion enumeration - every cast blocked on timing/
        # prohibition alone, never legal (so never in `views`/`actions`),
        # on this exact same board and for this exact same player. Computed
        # inside the same `read_on_one_board` layering `views` itself is, so
        # a `not_now` row can never describe a board `actions` does not.
        not_now = [row.model_dump(mode="json") for row in blocked_casts(board, player, pending)]
        return views, not_now

    # One layered board for the enumeration, its target candidates, and
    # `not_now` all three.
    views, not_now = read_on_one_board(rows, board)
    # The ledger position this enumeration describes, handed back with it
    # (`LegalActionsResponse.last_seq`): a seat names it as `expected_seq` on
    # whatever it decides to do, and that is what makes its refusals
    # splittable into races and rules errors at all. See `at_seq` above for
    # why it is read before the board rather than here.
    return {"actions": views, "not_now": not_now, "last_seq": at_seq}


@app.post(
    "/api/games/{game_id}/actions/concede",
    dependencies=REFUSED_AFTER_GAME_OVER,
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
            "pending_decision": _paused_pending_view(game_id, pending, req.player),
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
    dependencies=REFUSED_AFTER_GAME_OVER,
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_activate(game_id: str, req: ActivateRequest, uid: str | None = None) -> dict[str, Any]:
    sent = req
    actor = _acting_seat(game_id, req.player, uid)
    req, refused = _acting_as(game_id, "activate_ability", "activate", req, uid)
    if refused is not None:
        return refused
    records.require_current_board(
        game_id,
        "activate",
        req.expected_seq,
        player=actor,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, actor, req.reasoning)
    records.require_plan(game_id, actor)
    result, _, legal_before, seq = _run_command(
        game_id, "activate_ability", req, acting=(sent, uid)
    )
    return _respond(
        game_id,
        "activate",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card_id=str(req.card_id),
        ability_index=req.ability_index,
        targets=req.targets,
        # FD5 (1): every other `ActivateRequest` field, `mana_sources` alone
        # excepted (see the cast route's own note).
        sacrificing=[str(card_id) for card_id in req.sacrificing or []],
        discarding=[str(card_id) for card_id in req.discarding or []],
        exiling=[str(card_id) for card_id in req.exiling or []],
        x_value=req.x_value,
        color=req.color,
        mana_sources=[str(source) for source in req.mana_sources or []],
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        seq_hint=seq,
    )


@app.post(
    "/api/games/{game_id}/actions/crew",
    dependencies=REFUSED_AFTER_GAME_OVER,
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_crew(game_id: str, req: CrewRequest, uid: str | None = None) -> dict[str, Any]:
    """Crew a Vehicle (CR 702.122a). Its own endpoint rather than an
    `activate` against an ability index: the cost is the creatures tapped,
    not mana, and crew is the Vehicle's ability rather than theirs."""
    sent = req
    actor = _acting_seat(game_id, req.player, uid)
    req, refused = _acting_as(game_id, "crew", "crew", req, uid)
    if refused is not None:
        return refused
    records.require_current_board(
        game_id,
        "crew",
        req.expected_seq,
        player=actor,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, actor, req.reasoning)
    records.require_plan(game_id, actor)
    result, _, legal_before, seq = _run_command(game_id, "crew", req, acting=(sent, uid))
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


@app.post(
    "/api/games/{game_id}/actions/equip",
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_equip(game_id: str, req: EquipRequest, uid: str | None = None) -> dict[str, Any]:
    """Activate an Equipment's equip ability (CR 702.6a): "[Cost]: Attach this
    permanent to target creature you control. Activate only as a sorcery."

    Its own endpoint rather than an `activate` against an ability index, the
    same as crew and cycling: equip is a keyword ability the engine builds
    (`machines.keywords.equip_activation`), not one of the Equipment's
    `activated_abilities`, so there is no index to name. The legal-action row
    for it is `kind: equip`."""
    sent = req
    actor = _acting_seat(game_id, req.player, uid)
    req, refused = _acting_as(game_id, "equip", "equip", req, uid)
    if refused is not None:
        return refused
    records.require_current_board(
        game_id,
        "equip",
        req.expected_seq,
        player=actor,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, actor, req.reasoning)
    records.require_plan(game_id, actor)
    result, _, legal_before, seq = _run_command(game_id, "equip", req, acting=(sent, uid))
    return _respond(
        game_id,
        "equip",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card_id=str(req.card_id),
        target=str(req.target),
        mana_sources=[str(source) for source in req.mana_sources or []],
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        seq_hint=seq,
    )


@app.post(
    "/api/games/{game_id}/actions/turn-face-up",
    dependencies=REFUSED_AFTER_GAME_OVER,
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_turn_face_up(
    game_id: str, req: TurnFaceUpRequest, uid: str | None = None
) -> dict[str, Any]:
    """FD5 (5)/FD1: CR 702.37e/701.40b-d - turn a face-down permanent (a
    morph, megamorph, disguise, or manifested permanent) face up, paying
    whatever cost that reveals."""
    sent = req
    actor = _acting_seat(game_id, req.player, uid)
    req, refused = _acting_as(game_id, "turn_face_up", "turn_face_up", req, uid)
    if refused is not None:
        return refused
    records.require_current_board(
        game_id,
        "turn_face_up",
        req.expected_seq,
        player=actor,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, actor, req.reasoning)
    records.require_plan(game_id, actor)
    result, _, legal_before, seq = _run_command(game_id, "turn_face_up", req, acting=(sent, uid))
    return _respond(
        game_id,
        "turn_face_up",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card_id=str(req.card_id),
        procedure=req.procedure,
        mana_sources=[str(source) for source in req.mana_sources or []],
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        seq_hint=seq,
    )


@app.post(
    "/api/games/{game_id}/actions/companion",
    dependencies=REFUSED_AFTER_GAME_OVER,
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_companion(game_id: str, req: CompanionRequest, uid: str | None = None) -> dict[str, Any]:
    """FD5 (5)/FD1: CR 116.2g - pay {3} to bring your chosen companion from
    outside the game into your hand."""
    sent = req
    actor = _acting_seat(game_id, req.player, uid)
    req, refused = _acting_as(game_id, "companion", "companion", req, uid)
    if refused is not None:
        return refused
    records.require_current_board(
        game_id,
        "companion",
        req.expected_seq,
        player=actor,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, actor, req.reasoning)
    records.require_plan(game_id, actor)
    result, _, legal_before, seq = _run_command(game_id, "companion", req, acting=(sent, uid))
    return _respond(
        game_id,
        "companion",
        result,
        dry_run=req.dry_run,
        player=req.player,
        mana_sources=[str(source) for source in req.mana_sources or []],
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
    controlled = player_control.decision_refusal(game, viewer, engine_decision.player)
    if controlled is not None:
        return refuse(controlled)
    if not player_control.may_decide_for(game, viewer, engine_decision.player):
        # No enumeration on this branch, deliberately: the caller has just
        # been told this is not their decision, and "what else the deciding
        # seat could have done" is that seat's own hidden information.
        # Computing it here at all is how it first leaked - the row was
        # attributed to `detail["player"]`, straight off the request body,
        # so a refused caller who named themselves read it back out of the
        # log.
        return refuse("that uid does not open the deciding player's seat")
    # The seat the server resolved - the controller, when it is answering for
    # a player it controls (CR 723.5) - never `req.player`.
    records.require_reasoning(game_id, viewer, reasoning)
    # Same subject as the reasoning gate. `require_plan` self-exempts when
    # this is not that seat's own turn, so a blocker declaration on the
    # opponent's turn passes straight through it - see `records.require_plan`.
    records.require_plan(game_id, viewer, game=game)
    # T10: computed once the deciding seat is the *resolved* viewer, and
    # reused for every branch below (including a later refusal) - always the
    # *pre*-mutation enumeration, since nothing here has touched `game` yet.
    legal_before = records.legal_actions_before(game, engine_decision.player, pending)
    try:
        value = decisions.declared_pairs_answer(pairs, {p.username for p in game.players})
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
            "pending_decision": _paused_pending_view(game_id, next_pending, engine_decision.player),
            "log_seq": seq,
        }
    return {"allowed": True, "log_seq": seq}


def _dry_run_declaration(
    game: Game,
    paused: tuple[commands.Command, tuple[Answer, ...], PendingDecisionEngine],
    pairs: list[DeclaredPairRequest],
) -> engine_api.Outcome:
    """A dry run of the standing declaration, answered the way
    `_answer_declaration` answers the real one: the same answer validation and
    the same replay of the paused command with the answer appended, on the
    board the question was raised on (CR 508.1). `commands.resume_command`
    works on a copy and nothing is committed or restashed, so the question
    still stands afterwards and the dry run and the real call agree.

    Raises `commands.CommandDivergenceError` when the stored pause no longer
    replays; the caller holds the game lock and clears that pause before the
    409, exactly as the real declaration does."""
    command, tape, pending = paused
    decision = pending.decision
    try:
        value = decisions.declared_pairs_answer(pairs, {p.username for p in game.players})
        decisions.validate_answer(decision, value)
        answer = Answer(seq=pending.seq, kind=decision.kind, player=decision.player, pairs=value)
        commands.resume_command(game, command, tape, pending, answer)
    except ValueError as exc:
        return engine_api.refused(exc)
    return engine_api.Outcome(True, detail={"attackers": len(pairs)})


@app.post(
    "/api/games/{game_id}/actions/attack",
    dependencies=REFUSED_AFTER_GAME_OVER,
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
    pairs = [
        DeclaredPairRequest(creature=pair.card_id, defender=pair.defender) for pair in req.attackers
    ]
    if req.dry_run:
        with store.locked(game_id) as game:
            paused = store.paused_command(game_id)
            if paused is not None and paused[2].decision.kind is DecisionKind.DECLARE_ATTACKERS:
                try:
                    outcome = _dry_run_declaration(game, paused, pairs)
                except commands.CommandDivergenceError:
                    # Same as `_resume_paused`: a question that no longer
                    # replays can never be answered, so it is cleared here,
                    # under the lock that read it, and the 409 says so.
                    store.clear_paused_command(game_id)
                    raise
            else:
                outcome = engine_api.declare_attack(game, req)
        return audit_outcome(game_id, "attack", outcome, attackers=attackers_detail, dry_run=True)
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
    dependencies=REFUSED_AFTER_GAME_OVER,
    response_model=ActionOutcome,
    response_model_exclude_unset=True,
)
def action_play_land(game_id: str, req: PlayLandRequest, uid: str | None = None) -> dict[str, Any]:
    sent = req
    actor = _acting_seat(game_id, req.player, uid)
    req, refused = _acting_as(game_id, "play_land", "play_land", req, uid)
    if refused is not None:
        return refused
    records.require_current_board(
        game_id,
        "play_land",
        req.expected_seq,
        player=actor,
        reasoning=req.reasoning,
        dry_run=req.dry_run,
    )
    records.require_reasoning(game_id, actor, req.reasoning)
    records.require_plan(game_id, actor)
    result, _, legal_before, seq = _run_command(game_id, "play_land", req, acting=(sent, uid))
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
    dependencies=REFUSED_AFTER_GAME_OVER,
    response_model=PassPriorityResponse,
    response_model_exclude_unset=True,
)
def action_pass_priority(
    game_id: str, req: PassPriorityRequest, uid: str | None = None
) -> dict[str, Any]:
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
    # A controller passing a controlled player's priority by that player's
    # name is the controller's own pass to the engine (CR 723.5).
    sent = req
    req, _ = _acting_as(game_id, "pass_priority", "pass_priority", req, uid)
    # `pass_shaped=True`: this is the one action whose stated reason is
    # always the same sentence, and `seatkit.pass_until` posts to this very
    # route for every pass a run makes - see `records.require_reasoning`.
    # The field is still accepted and still audited below.
    records.require_current_board(
        game_id, "pass_priority", req.expected_seq, player=req.player, reasoning=req.reasoning
    )
    records.require_reasoning(game_id, req.player, req.reasoning, pass_shaped=True)
    # Exempt from `reasoning` always, and from the plan itself ONLY in the
    # beginning phase (`pass_shaped=True` - upkeep and draw, see `records.
    # require_plan`'s own docstring): passing in a main phase, combat, or
    # the end step can perfectly well be the first thing a seat does on its
    # own turn, and "I had nothing to do this turn" is a claim a reviewer
    # should be able to check against a plan that was declared before it.
    records.require_plan(game_id, req.player, pass_shaped=True)
    try:
        result, _, legal_before, seq_hint = _run_command(
            game_id, "pass_priority", req, acting=(sent, uid)
        )
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
        # FD5 (3): `allowed=True`, a committing row - see `_respond`'s own
        # PAUSED branch for why. The response body below keeps `allowed:
        # None`.
        seq = audit.record(
            game_id,
            "pass_priority",
            player=req.player,
            allowed=True,
            paused=True,
            paused_on={"player": pending.player, "kind": pending.kind},
            reasoning=req.reasoning,
            legal_actions_before=legal_before,
        )
        return {
            "allowed": None,
            "paused": True,
            "pending_decision": _paused_pending_view(game_id, pending, req.player),
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
    dependencies=REFUSED_AFTER_GAME_OVER,
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
    dependencies=REFUSED_AFTER_GAME_OVER,
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
    dependencies=REFUSED_AFTER_GAME_OVER,
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
    dependencies=REFUSED_AFTER_GAME_OVER,
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
    sent = req
    req, _ = _acting_as(game_id, "advance", "advance", req, uid)
    records.require_current_board(
        game_id, "advance", req.expected_seq, player=req.player, reasoning=req.reasoning
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    try:
        result, committed, legal_before, seq_hint = _run_command(
            game_id, "advance", req, acting=(sent, uid)
        )
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
    before = step_position(committed)
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
def report_problem(
    game_id: str, req: ReportProblemRequest, uid: str | None = None
) -> dict[str, Any]:
    """A player files a problem report straight into the game log.

    **Attributed to the credential, never to `req.player`.** This is one of
    the two POSTs an advisor uid may make (`game_access.
    ADVISOR_POST_SUFFIXES`), and a row keyed on a plain request field let
    any caller write into the log in another seat's name - an advisor of
    alice filing "bob is cheating, signed bob" and the ledger agreeing. A
    seat uid resolves to its own seat; an advisor uid resolves to the human
    it watches, which is the one seat it is entitled to speak about.

    `req.player` is still honoured for the admin key, which opens no seat
    and so resolves to nobody: that is the operator filing on a seat's
    behalf, and the only caller the field was ever really for.

    **`category`** (DR2, `schemas.ReportProblemCategory`) sorts the report
    into one of three kinds - `engine` (default): a rules/engine/api bug;
    `dsl`: a play the play-script DSL could not express, or a DSL feature
    that is missing; `ux`: friction with the MCP tools themselves. Any other
    value is refused with a 422 before this route ever runs (pydantic's own
    `Literal` validation on `req`). Recorded in the row alongside `player`/
    `text`; a row written before this field existed carries no `category`
    key at all and reads back as `engine` (`audit.problem_category`).
    """
    game = game_or_404(game_id)
    player = (
        game_access.seat_for_uid(game_id, uid)
        or (player_for_advisor_uid(game_id, uid) if uid else None)
        or req.player
    )
    game.player(player)  # 400 for a player who is not in this game at all
    audit.record(game_id, "report_problem", player=player, text=req.text, category=req.category)
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

    **`for_turn: "next"` plans ahead** (`schemas.DeclarePlanIn.for_turn`):
    called while it is NOT this seat's own turn, the row is written against
    the turn `records.next_own_turn` computes - this seat's own next turn,
    CR 500.7 extra turns skipped over exactly as a real game would skip
    them - rather than the turn under way right now. `declared_turns_taken`
    records what `turns_taken` stood at the moment of declaring, beside the
    `turns_taken` the row is actually filed under, so the ledger shows both
    without ambiguity. `for_turn: "next"` while this seat's own turn is
    already in progress is refused - `records.OwnTurnInProgress`, 409.

    **F3: three ways to make this cheaper to write, none of them touching
    `prediction`.** `records.latest_declaration` is this seat's own most
    recent row, read once and reused for all three:

    - `board_read` left off fills `board_read_auto` in instead -
      `playscripts.decision_dsl.compact_board` of this seat's own redacted
      view at this exact moment (`visibility.redact_view`, the same
      function `review.py` itself reads a board through), objective and
      free to produce.
    - `my_role` left off carries the seat's last declared role forward
      (`my_role_carried: true` on the row) - refused, `records.MissingRole`
      (422), on this seat's very first declaration, which has nothing to
      carry.
    - `reuse_previous: true` (with `still_because`) copies `plan` and
      `prediction` forward from that same latest declaration verbatim,
      marking the row `reused_from_seq` - refused, `records.
      NoPriorDeclaration` (422), with nothing to copy from.
    """
    game = game_or_404(game_id)
    if store.player_for_uid(game_id, req.uid) != req.player:
        raise KeyError("that uid is not that seat in this game")
    game.player(req.player)  # 400 for a player who is not in this game at all
    turn = game.turn
    declared_at = turn.turns_taken if turn is not None else None
    if req.for_turn == "next":
        target = records.next_own_turn(game, req.player)  # OwnTurnInProgress -> 409
        plan_turns_taken: int | None = target.turns_taken
        response_turn_number: int | None = target.turn_number
    else:
        plan_turns_taken = declared_at
        response_turn_number = turn.turn_number if turn is not None else None

    previous = records.latest_declaration(game_id, req.player)

    board_read: str | None = req.board_read
    board_read_auto: dict[str, Any] | None = None
    if board_read is None:
        raw_view = state_view(game, game_id)
        redacted = visibility.redact_view(raw_view, req.player)
        board_read_auto = decision_dsl.compact_board(redacted)

    my_role_carried = False
    my_role = req.my_role
    if my_role is None:
        if previous is None:
            raise records.MissingRole(req.player)
        my_role = previous[1].get("my_role")
        my_role_carried = True

    reused_from_seq: int | None = None
    if req.reuse_previous:
        if previous is None:
            raise records.NoPriorDeclaration(req.player)
        reused_from_seq = previous[0]
        plan = previous[1].get("plan")
        prediction = previous[1].get("prediction")
    else:
        # `schemas.DeclarePlanIn._shape` already refused a fresh declaration
        # missing either field - `plan`/`prediction` are never `None` here.
        assert req.plan is not None and req.prediction is not None
        plan = req.plan
        prediction = req.prediction.model_dump()

    seq = audit.record(
        game_id,
        audit.PLAN_ACTION,
        player=req.player,
        turns_taken=plan_turns_taken,
        # The turn this row is ABOUT, never the turn it was written in - the
        # two diverge exactly for `for_turn: "next"`. `audit.record` still
        # stamps the row's own top-level `turn`/`step`/`phase` from the live
        # board, honestly recording when the declaration was made; this is
        # the separate fact `review.py`'s `_declarations` and `ui/review_
        # model.js`'s `reviewTurns` read to attribute the row to its target
        # turn wherever it is displayed or grouped, in a 3+ player game where
        # `turn_number` can otherwise land the row in a different player's
        # turn entirely (a live-game repro, 2026-09-14).
        turn_number=response_turn_number,
        for_turn=req.for_turn,
        declared_turns_taken=declared_at,
        board_read=board_read,
        board_read_auto=board_read_auto,
        my_role=my_role,
        my_role_carried=my_role_carried,
        plan=plan,
        prediction=prediction,
        reused_from_seq=reused_from_seq,
        still_because=req.still_because,
    )
    return {
        "declared": True,
        "log_seq": seq,
        "player": req.player,
        "turn_number": response_turn_number,
        "for_turn": req.for_turn,
    }


@app.post(
    "/api/games/{game_id}/advice", response_model=AdviceResponse, response_model_exclude_unset=True
)
def record_advice(game_id: str, req: AdviceIn, uid: str | None = None) -> dict[str, Any]:
    """Record one action an advisor thinks `req.player` should take
    (`schemas.AdviceIn`), and record nothing else.

    **This mutates no `Game`, exactly like `declare_plan`** - which is the
    prior art this is modelled on, down to the shape of this docstring. It
    calls `audit.record` and nothing else: no `store.replace`, no
    compare-and-swap, no version bump. A proposal is a statement *about* the
    game, and the whole point of advise mode is that the advisor never moves
    it; a route that quietly committed one would hand a read-only credential
    the authority its own gate exists to deny.

    **The summary is derived here, not taken from the caller.**
    `records.advice_summary` reads `req.body` against the live game - card
    ids resolved to names, targets and cost spelled out - so what the human
    reads can never disagree with the request it describes. `schemas.AdviceIn`
    has no `summary` field at all, so an advisor cannot try.

    **`based_on_seq` is read immediately before the row is written, under no
    lock.** A command landing in between makes this proposal born-stale, and
    that is the safe direction: the human's own action carries its own
    `expected_seq` (`records.require_current_board`) and is refused against a
    board that moved, so a stale proposal costs a refusal rather than a wrong
    move. Taking the game's lock here would instead let a buddy pod's
    thinking block the seat it is advising.

    `legal_actions_before` is computed the same way every acting route
    computes it, for `req.player` - what *else* was legal at the moment the
    advice was given, which is the comparison a retrospective needs and
    cannot reconstruct afterwards. It is hidden information (it names cards
    in that player's hand), which is why it rides in the row rather than the
    response, and `visibility.redact_log_entry` keeps it - and the whole
    proposal - out of the opponent's read of the same log.

    Authorised on whoever may already *see* `req.player`'s hand: this game's
    advisor for that seat (`visibility.resolve_viewer` resolves an advisor
    uid to its human's username), the seat itself, or the admin key. A
    proposal names hidden information, so nobody else may write one into that
    seat's name - the same rule, and the same 404, `declare_plan` enforces.

    `req.drafted_at_seq` (S2 A6) rides straight into `detail` unexamined - it
    is a metrics-only marker of when a staged draft was captured, and nothing
    here reads it back.
    """
    game = game_or_404(game_id)
    viewer = visibility.resolve_viewer(game_id, uid)
    if viewer is not visibility.ADMIN and viewer != req.player:
        raise KeyError("that uid does not advise that seat in this game")
    game.player(req.player)  # 400 for a player who is not in this game at all
    legal_before = records.legal_actions_before(
        game, req.player, (store.paused_command(game_id) or (None, None, None))[2]
    )
    summary = records.advice_summary(game, req.method, req.path, req.body, legal_before)
    based_on_seq = audit.last_seq(game_id)
    seq = audit.record(
        game_id,
        audit.ADVICE_ACTION,
        player=req.player,
        method=req.method,
        path=req.path,
        body=req.body,
        summary=summary,
        reasoning=req.reasoning,
        based_on_seq=based_on_seq,
        validated=req.validated,
        legal_actions_before=legal_before,
        drafted_at_seq=req.drafted_at_seq,
    )
    return {
        "proposed": True,
        "log_seq": seq,
        "player": req.player,
        "summary": summary,
        "validated": req.validated,
        "based_on_seq": based_on_seq,
    }


@app.post(
    "/api/games/{game_id}/advice/{seq}/approve",
    response_model=AdviceApprovedResponse,
    response_model_exclude_unset=True,
)
def approve_advice(
    game_id: str, seq: int, req: AdviceApproveIn, uid: str | None = None
) -> dict[str, Any]:
    """Record that the advised human said yes to the `advice` row at `seq`.

    **Mutates no `Game`** - one more `audit.record` call, for the same
    reasons `record_advice` above makes none.

    **The human's client calls this BEFORE sending the action itself**, and
    must not send the action if this fails. That ordering is the whole
    purpose of the row: an `advice_approved` row sitting immediately before
    the action in the same ledger is what lets a retrospective say "this move
    came from the buddy" rather than inferring it from adjacency, and a
    refused approval leaves no row at all, so an action taken anyway is
    visibly unpaired. (The browser half of that is a later task; the contract
    it must honour is pinned by this repo's own tests now, not then.)

    **Human uid only, checked twice.** `game_access.py` already refuses an
    advisor uid here - this path is neither `/advice` nor `/report`, and the
    body claims no `dry_run` - and that is correct and deliberate: an advisor
    approving its own advice would make the human's agreement a formality.
    This route checks it again itself, against `store.player_for_uid` (a
    *seat* credential, never `resolve_viewer`, which an advisor uid passes by
    design), because a rule this load-bearing should not rest on one gate
    remembering to run.
    """
    game_or_404(game_id)
    advised = _advice_player_at(game_id, seq)
    if advised is None:
        raise KeyError(f"no advice at {seq} in this game")
    if advised != req.player:
        raise KeyError("that advice was not given to that player")
    if not uid or (
        store.player_for_uid(game_id, uid) != req.player and not store.is_admin(game_id, uid)
    ):
        raise KeyError("that uid is not that seat in this game")
    approved_seq = audit.record(
        game_id,
        audit.ADVICE_APPROVED_ACTION,
        player=req.player,
        advice_seq=seq,
    )
    return {
        "recorded": True,
        "log_seq": approved_seq,
        "advice_seq": seq,
        "player": req.player,
    }


def _advice_player_at(game_id: str, seq: int) -> str | None:
    """Whom the `advice` row at `seq` was given to, or `None` when `seq` is
    not an `advice` row of this game at all.

    Reads from `seq` forward (`audit.entries_since(game_id, seq - 1)`) rather
    than the whole ledger: the approval follows its proposal within seconds
    in the one flow that calls this, so the tail it walks is short, and the
    ledger's full-snapshot rows make reading all of them genuinely expensive
    (see `audit.entries`' own docstring)."""
    for entry in audit.entries_since(game_id, seq - 1):
        if entry["seq"] == seq:
            if entry["action"] != audit.ADVICE_ACTION:
                return None
            return (entry.get("detail") or {}).get("player")
    return None


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
    "/api/games/{game_id}/ai-review",
    response_model=AiReviewResponse,
    response_model_exclude_unset=True,
)
def declare_ai_review(game_id: str, req: AiReviewIn) -> dict[str, Any]:
    """File the post-game AI review (`mtg_agent.driver.review`, run on Opus
    from a seat's own reflection plus the game's review record) -
    `audit.AI_REVIEW_ACTION`, a row entirely separate from
    `audit.REFLECTION_ACTION`.

    Mirrors `declare_reflection` exactly: same gate (post-game only, against
    `store.archived` so a soft-deleted lobby game still accepts one), same
    uid-owns-seat check, same "mutates no `Game`" shape. See that route's
    docstring for the reasoning; nothing here differs beyond which action
    the row is filed under and whose words it carries.

    **One check `declare_reflection` does not need: `req.player` must be a
    registered AI seat of this game (`archived.ai_players`) - UNLESS the
    request carries `req.review_key`, this game's admin key.** A reflection
    is legitimately a human's own words about their own game; an AI review
    is never that - it is only ever the output of a driver's own review run
    critiquing that seat's play, so a human's own uid (which genuinely opens
    their own seat, and would otherwise pass the check above) must not be
    able to file one in anybody's name. Without this a human player could
    forge a review of themselves or of their opponent, since the uid check
    alone only proves "this credential speaks for this seat", not "this
    seat is the kind that ever calls this route" - the same distinction
    `AiReviewIn`'s docstring draws in words.

    **FD7 gap 15 widens "the kind that ever calls this route" to a human
    seat too, but only through its own review-only Job.** A human seat is
    never in `ai_players` and never will be, so the AI-seat check alone
    would refuse its review forever - `k8s_seats.build_review_job` dispatches
    as the human seat's OWN uid (there is no other credential to act as that
    seat with), so the uid-owns-seat check above cannot tell that dispatch
    apart from the seat calling this route on its own. `review_key` is what
    can: it is minted once per game and handed only to whoever created it,
    never to a seat, and `k8s_seats` puts it in the review Job's assignment
    alone. `store.is_admin` is the same check `game_access`'s own admin-key
    credential uses everywhere else, so this reuses that trust boundary
    rather than minting a parallel one."""
    archived = store.archived(game_id)
    if archived is None:
        raise KeyError(f"no game {game_id}")
    if archived.seat_uids.get(req.uid) != req.player:
        raise KeyError("that uid is not that seat in this game")
    if req.player not in archived.ai_players and not (
        req.review_key is not None and store.is_admin(game_id, req.review_key)
    ):
        raise KeyError("that seat is not an AI seat in this game")
    if not _post_game(archived, _gate_entries(archived)):
        raise HTTPException(
            status_code=409,
            detail=(
                "this game is still in progress - the AI review is written "
                "after the result is known, so it cannot be filed for a "
                "game that has not ended"
            ),
        )
    seq = audit.record(
        game_id,
        audit.AI_REVIEW_ACTION,
        player=req.player,
        text=req.text,
        turning_points=req.turning_points,
    )
    return {"recorded": True, "log_seq": seq, "player": req.player}


@app.post(
    "/api/games/{game_id}/ai-review-failure",
    response_model=AiReviewFailureResponse,
    response_model_exclude_unset=True,
)
def declare_ai_review_failure(game_id: str, req: AiReviewFailureIn) -> dict[str, Any]:
    """Record that the post-game AI review could NOT be produced, and why -
    `audit.AI_REVIEW_FAILED_ACTION`.

    Gated identically to `declare_ai_review` beside it, down to the AI-seat
    check and its `review_key` exception (FD7 gap 15): only an AI seat's own
    driver, or a human seat's own review Job presenting this game's admin
    key, ever generates a review, so only they can report that one failed -
    a human's own uid alone must not be able to write "your review failed"
    into anybody's ledger.

    **Why a route at all.** The driver is a separate process from this api
    and reaches it only over HTTP, so a durable signal it can write has to be
    a route. The alternative - leaving the four failures in the driver's own
    `log.warning` calls - is what made "the reviewer failed" and "the
    reviewer was never invoked" indistinguishable once a pod's logs rolled.
    See `audit.AI_REVIEW_FAILED_ACTION` for why this row is not loud."""
    archived = store.archived(game_id)
    if archived is None:
        raise KeyError(f"no game {game_id}")
    if archived.seat_uids.get(req.uid) != req.player:
        raise KeyError("that uid is not that seat in this game")
    if req.player not in archived.ai_players and not (
        req.review_key is not None and store.is_admin(game_id, req.review_key)
    ):
        raise KeyError("that seat is not an AI seat in this game")
    if not _post_game(archived, _gate_entries(archived)):
        raise HTTPException(
            status_code=409,
            detail=(
                "this game is still in progress - an AI review failure is "
                "recorded after the result is known, so it cannot be filed "
                "for a game that has not ended"
            ),
        )
    seq = audit.record(
        game_id,
        audit.AI_REVIEW_FAILED_ACTION,
        player=req.player,
        stage=req.stage,
        reason=req.reason,
        invoked=req.invoked,
    )
    return {"recorded": True, "log_seq": seq, "player": req.player}


@app.post(
    "/api/games/{game_id}/plays-outcome",
    response_model=PlaysOutcomeResponse,
    response_model_exclude_unset=True,
)
def declare_plays_outcome(game_id: str, req: PlaysOutcomeIn) -> dict[str, Any]:
    """Record what one handover did to the DSL - `audit.PLAYS_OUTCOME_ACTION`,
    filed by the seat's or the advisor's own driver after the handover turn
    (`schemas.PlaysOutcomeIn`).

    Gated like `declare_ai_review`: against `store.archived`, so a game the
    lobby has already ended still takes one; post-game only (409 before);
    and the uid binds to exactly one player. A seat uid speaks for its own
    seat only, and only for an AI seat (`archived.ai_players`) UNLESS it
    files as `role: reviewer` - the post-game DR3 review, which FD7 gap 15
    also gives a human seat, so a `reviewer` row is never held to the AI-only
    rule a `seat` row (the handover itself) is. An advisor uid speaks for
    the human it watches; its lookup reads the live game, so it is refused
    once the game is soft-deleted (the driver files before it retires).
    `game_access`'s `OWN_PLAYER_ROUTES` refuses a seat uid naming another
    player before this runs; the checks here are the same binding, restated
    where the row is written. The role must match the credential (400), so
    the ledger cannot claim an advisor's handover was a seat's - a seat uid
    may file as `seat` or `reviewer`, an advisor uid only ever as `advisor`.
    The uid itself is never recorded.

    **An `unfinished` row schedules F1's follow-up** - unless it is itself
    a follow-up's (`followup: true`), which is what makes a second one
    impossible. `lobby.schedule_followup` dedupes it per (game, uid),
    schedules the Job in kubernetes mode (or records it `unavailable`), and
    the answer's `followup` says which. The follow-up runs as this same
    `uid`, so it can only ever speak for the player this route just bound
    it to. Its assignment is built here, from the ledger, at schedule time
    (`_followup_payload`)."""
    archived = store.archived(game_id)
    if archived is None:
        raise KeyError(f"no game {game_id}")
    seat = archived.seat_uids.get(req.uid)
    advised = player_for_advisor_uid(game_id, req.uid) if seat is None else None
    if (seat or advised) != req.player:
        raise KeyError("that uid is not that seat in this game")
    role = "seat" if seat is not None else "advisor"
    if req.role == "reviewer":
        # review fix (gap 15): the seat's own post-game DR3 review, filed
        # under the same uid a second time - never held to the AI-only rule
        # a `seat` row (the handover itself) is, since FD7 gap 15 gives a
        # human seat a review Job too. An advisor watches, it does not
        # itself get reviewed.
        if seat is None:
            raise ValueError("an advisor uid never files a reviewer row")
    elif req.role != role:
        raise ValueError(f"a {role} uid files its outcome as role {role!r}, not {req.role!r}")
    elif seat is not None and req.player not in archived.ai_players:
        raise KeyError("that seat is not an AI seat in this game")
    if not _post_game(archived, _gate_entries(archived)):
        raise HTTPException(
            status_code=409,
            detail=(
                "this game is still in progress - a plays outcome records the "
                "post-game handover, so it cannot be filed for a game that has "
                "not ended"
            ),
        )
    seq = audit.record(
        game_id,
        audit.PLAYS_OUTCOME_ACTION,
        **req.model_dump(exclude={"uid"}),
    )
    answer: dict[str, Any] = {"recorded": True, "log_seq": seq, "player": req.player}
    # review fix (gap 15): a `reviewer` row is the DR3 review, not the
    # handover's own development step - it never schedules F1's follow-up,
    # whatever `state` it happens to carry.
    if req.role != "reviewer" and req.state == "unfinished" and not req.followup:
        try:
            answer["followup"] = lobby.schedule_followup(
                game_id,
                uid=req.uid,
                player=req.player,
                role=role,
                payload=lambda: _followup_payload(game_id, req, archived),
            )
        except Exception as exc:  # noqa: BLE001 - the outcome row is already recorded
            logger.warning(
                "follow-up for %s in game %s not scheduled: %s", req.player, game_id, exc
            )
            answer["followup"] = "failed"
    return answer


def _followup_payload(game_id: str, req: PlaysOutcomeIn, archived: ArchivedGame) -> dict[str, Any]:
    """F1's follow-up assignment, built from the ledger when the follow-up
    is scheduled - so a later archive, or a Forget, costs it nothing it
    needs.

    The tables are the ones the handover itself read: `script_run_table`,
    `offer_table`, `hand_played_summary` and the development candidates,
    computed over this game's log exactly as `req.uid` reads it
    (`game_log` - the seat's own redacted view, never more). The decklists
    are the ones A2 reveals to this game's own credentials once it is over;
    the follow-up identifies the opponent from the list with its own guides
    corpus, and falls back to the identification the handover recorded in
    the row (`opponent`). `deck` is the row's, else the deck the seat
    registered its scripts under."""
    player = req.player
    rows = game_log(game_id, req.uid)["log"]
    decklists = store.decklists(game_id)
    game = archived.game
    names = [p.username for p in game.players] if game is not None else list(decklists)
    opponent = next((name for name in names if name != player), "")
    registered = store.scripts(game_id, req.uid) or {}
    candidates = development_candidates(rows, game_id, player)
    if game is not None and game.winner:
        outcome = f"{game.winner} won"
    elif game is not None and game.drawn:
        outcome = "the game was a draw"
    else:
        outcome = "the game is over"
    return {
        "deck": req.deck or registered.get("deck_slug"),
        "decklist": decklists.get(player, ""),
        "opponent": opponent,
        "opponent_decklist": decklists.get(opponent) or None,
        "opponent_identification": (
            req.opponent.model_dump() if req.opponent is not None else None
        ),
        "outcome": outcome,
        "run_table": script_run_table(rows, game_id, player),
        "offers": offer_table(rows, game_id, player),
        "hand_played": hand_played_summary(rows, game_id, player),
        "candidates": {"plays": list(candidates.plays), "lines": list(candidates.lines)},
        "unfinished_reasons": list(req.unfinished_reasons),
    }


# -- play scripts -----------------------------------------------------------


def _query_credentials(request: Request) -> tuple[str | None, str | None]:
    """`(uid, key)` off the query string, where the seat driver sends its
    credential on every call (`api_client.HttpApiClient`). The scripts path
    carries a `{uid}` of its own, so the caller's credential cannot also be a
    declared `uid` parameter of these routes."""
    return request.query_params.get("uid"), request.query_params.get("key")


def _presents_admin_key(game_id: str, uid: str | None, key: str | None) -> bool:
    return any(store.is_admin(game_id, candidate) for candidate in (key, uid) if candidate)


@app.put(
    "/api/games/{game_id}/seats/{uid}/scripts",
    response_model=ScriptsRegisteredResponse,
    response_model_exclude_unset=True,
)
def register_scripts(
    game_id: str, uid: str, req: ScriptsRegisterIn, request: Request
) -> dict[str, Any]:
    """Register the play scripts a seat Job runs, under the uid it runs with
    (a seat uid, or an advisor uid for a buddy), replacing that uid's
    previous corpus.

    **The credential is the driver's own**: `?uid=` equal to the path's
    `{uid}`, or this game's admin key. Anything else is a 404, the same
    answer `declare_plan` gives for writing into another seat's name. An
    advisor uid gets past `game_access.py` for this one PUT only, and only
    for its own corpus (`ADVISOR_OWN_PUT_ROUTES`).

    **Every script is judged before anything is stored**
    (`records.script_errors`): one refusal and the whole corpus is refused
    with `code: invalid_scripts` and each `PlaysError`, so a runner never
    runs half of what it meant to register. `inputs`, the deck's declared
    inputs, is judged the same way and refused as `malformed-inputs`.

    **Behind the kill switch**: with `SCRIPTS_ENABLED` off this refuses with
    `code: scripts_disabled` (`records.require_scripts_enabled`).

    Mutates no `Game` and writes no ledger row. The corpus lives in the
    repository (`games.scripts` on postgres), so it survives a restart and
    reads the same on every replica.
    """
    game_or_404(game_id)
    caller, key = _query_credentials(request)
    if caller != uid and not _presents_admin_key(game_id, caller, key):
        raise KeyError("that credential does not register scripts under that uid")
    if store.player_for_uid(game_id, uid) is None and player_for_advisor_uid(game_id, uid) is None:
        raise KeyError(f"no seat or advisor {uid} in this game")
    records.require_scripts_enabled("registering scripts")
    errors = records.script_errors(req.scripts) + records.inputs_errors(req.inputs)
    if errors:
        raise records.InvalidScripts(errors)
    registered_at = datetime.now(UTC).isoformat()
    store.register_scripts(
        game_id,
        uid,
        {
            "deck_slug": req.deck_slug,
            "guides_revision": req.guides_revision,
            "scripts": req.scripts,
            "inputs": req.inputs,
            "registered_at": registered_at,
        },
    )
    return {
        "registered": True,
        "uid": uid,
        "deck_slug": req.deck_slug,
        "guides_revision": req.guides_revision,
        "count": len(req.scripts),
        "registered_at": registered_at,
    }


@app.get(
    "/api/games/{game_id}/seats/{uid}/scripts",
    response_model=ScriptsCorpusResponse,
    response_model_exclude_unset=True,
)
def read_scripts(game_id: str, uid: str, request: Request) -> dict[str, Any]:
    """The corpus registered under `uid`, to three readers only: that uid
    itself, an advisor of the seat that uid opens, and the admin key.

    A corpus is hidden information - the lines a seat means to take and the
    archetypes it plans around - so anyone else is refused the way
    `get_legal_actions` refuses a caller who does not open the seat it names
    (`ValueError`, 400), before the corpus is even looked up, so the answer
    does not say whether one exists. 404 only once the caller is entitled
    and nothing is registered.
    """
    game_or_404(game_id)
    caller, key = _query_credentials(request)
    seat = store.player_for_uid(game_id, uid)
    entitled = (
        (caller is not None and caller == uid)
        or _presents_admin_key(game_id, caller, key)
        or (
            seat is not None
            and caller is not None
            and store.player_for_uid(game_id, caller) is None
            and player_for_advisor_uid(game_id, caller) == seat
        )
    )
    if not entitled:
        raise ValueError(
            "that uid does not open these scripts - a corpus is read by the uid it is "
            "registered under, that seat's advisor, and the admin key"
        )
    corpus = store.scripts(game_id, uid)
    if corpus is None:
        raise KeyError("no scripts are registered under that uid in this game")
    # A corpus registered before declared inputs existed carries no key.
    return {"uid": uid, "inputs": [], **corpus}


@app.post(
    "/api/games/{game_id}/seats/{uid}/scripts/adhoc",
    response_model=AdhocScriptResponse,
    response_model_exclude_unset=True,
)
def stage_adhoc_script(
    game_id: str, uid: str, req: AdhocScriptRequest, request: Request
) -> dict[str, Any]:
    """FD5 (7): stage a mid-game ad-hoc play script under `uid`'s own
    corpus (`records.stage_adhoc_script`) - a play written and staged
    without registering it into the corpus `PUT .../scripts` builds first,
    for a multi-step or repetitive sequence a seat is facing right now
    (FD9's `repeat` covers the loop itself).

    **The credential is the driver's own**, exactly `register_scripts`'
    own rule: `?uid=` equal to the path's `{uid}`, or this game's admin key.
    Anything else is a 404, confirming nothing about whether that uid or
    game exists.

    **Behind the kill switch**: with `SCRIPTS_ENABLED` off this refuses
    with `code: scripts_disabled`. **Every refusal is `code: invalid_
    scripts`** with each `PlaysError` - a malformed play, a card-name-ban
    hit, a conflict with the corpus... - `records.stage_adhoc_script`'s
    own docstring has the rest.
    """
    game_or_404(game_id)
    caller, key = _query_credentials(request)
    if caller != uid and not _presents_admin_key(game_id, caller, key):
        raise KeyError("that credential does not stage a script under that uid")
    if store.player_for_uid(game_id, uid) is None and player_for_advisor_uid(game_id, uid) is None:
        raise KeyError(f"no seat or advisor {uid} in this game")
    records.require_reasoning(game_id, uid, req.reasoning)
    return records.stage_adhoc_script(game_id, uid, req.play, req.reasoning)


def _uids_running(game_id: str, player: str, role: str) -> list[str]:
    """The uids whose corpora a `role` run for `player` may start from: that
    seat's uids for `seat`, that seat's advisors' uids for `advisor`."""
    if role == "seat":
        return [uid for uid, name in store.seat_uids(game_id).items() if name == player]
    return [uid for uid, advisor in store.advisor_uids(game_id).items() if advisor.player == player]


@app.post(
    "/api/games/{game_id}/script-runs",
    response_model=ScriptRunResponse,
    response_model_exclude_unset=True,
    dependencies=REFUSED_AFTER_GAME_OVER,
)
def record_script_run(game_id: str, req: ScriptRunIn, request: Request) -> dict[str, Any]:
    """Report one play-script run: `phase: start` as it begins, `phase:
    finish` when it ends, or `phase: offer` for a play the runner offered
    this seat without running it (`schemas.ScriptRunIn`).

    B8-06: refused the same way every action route is (CR 104.1,
    `REFUSED_AFTER_GAME_OVER`/`commands.GameOverError`) once the game has
    ended - both phases, unlike the scripts-disabled kill switch below, which
    deliberately lets an in-flight run's own `finish` land. A finished game
    has nothing left for either phase to record.

    **This mutates no `Game`, exactly like `declare_plan`.** The moves a
    script makes go through the action routes, which run every gate on them;
    this only records that a run happened. So it calls none of
    `records.require_current_board`, `require_reasoning` or `require_plan`,
    and an advisor may reach it (`game_access.ADVISOR_POST_SUFFIXES`).

    Authorised on `?uid=`: a seat uid reports runs for its own seat as role
    `seat`, an advisor uid for the human it watches as role `advisor`, and
    the admin key for anyone. Another seat's name is a 404; the wrong role
    for the credential is a 400, so the ledger cannot claim an advisor's
    advice was a seat's own play.

    `start` is behind the kill switch (`code: scripts_disabled`), refuses a
    script not registered under the uid that runs it with `code:
    unknown_script` (`records.require_registered_script`: a seat uid's own
    corpus, an advisor uid's own, or for the admin key the named seat's uids
    for role `seat` and its advisors' for role `advisor`), and refuses a
    second start of the same script by the same seat at the same turn and
    step with exit `already_ran` (`records.record_script_run_start`).
    `finish` writes one `script_run` row with every field of the request and
    the `game_id`, and is recorded even with the switch off. All three rows
    are omitted from every log read but that seat's, its advisor's and the
    admin key's (`visibility.redact_log_entry`).

    `offer` (API-5) records that a play was offered to this seat at this
    turn and step, out of band, so a later read can pair up whether the seat
    took it - a `script_run_offered` row, never a run of its own
    (`records.record_script_run_offer`). It sits behind the same kill switch
    a `start` does (`code: scripts_disabled` with the switch off - an offer
    arriving then is a stale process, since the hint that would have made it
    never ran) and the same `unknown_script` check (a play offered outside
    the registered corpus is a bug worth a 4xx). It is deliberately **not**
    behind `already_ran`: an offer does not hold the once-per-turn-and-step
    slot a `start` does, so it neither collides with one nor blocks the very
    `start` it invited (`audit.SCRIPT_RUN_SLOT_ACTIONS`).
    """
    game = game_or_404(game_id)
    caller, key = _query_credentials(request)
    if _presents_admin_key(game_id, caller, key):
        runners = _uids_running(game_id, req.seat, req.role)
    else:
        seat = store.player_for_uid(game_id, caller) if caller else None
        advised = player_for_advisor_uid(game_id, caller) if caller and seat is None else None
        if (seat or advised) != req.seat:
            raise KeyError("that uid does not run that seat in this game")
        role = "seat" if seat is not None else "advisor"
        if req.role != role:
            raise ValueError(f"a {role} uid reports its runs as role {role!r}, not {req.role!r}")
        runners = [caller] if caller else []
    game.player(req.seat)  # 400 for a player who is not in this game at all
    detail = {**req.model_dump(exclude={"phase"}), "game_id": game_id}
    if req.phase == "start":
        records.require_scripts_enabled("starting a script run")
        records.require_registered_script(game_id, runners, detail)
        seq = records.record_script_run_start(game_id, detail)
    elif req.phase == "offer":
        records.require_scripts_enabled("offering a script run")
        records.require_registered_script(game_id, runners, detail)
        seq = records.record_script_run_offer(game_id, detail)
    else:
        seq = records.record_script_run_finish(game_id, detail)
    return {"recorded": True, "phase": req.phase, "run_id": req.run_id, "log_seq": seq}


@app.post(
    "/api/games/{game_id}/decide",
    dependencies=REFUSED_AFTER_GAME_OVER,
    response_model=DecideResponse,
    response_model_exclude_unset=True,
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
        game_id,
        "decide",
        req.expected_seq,
        player=req.player,
        reasoning=req.reasoning,
        decision_generation=req.generation,
    )
    game = game_or_404(game_id)
    legal_before: records.LegalActionsBefore | None = None
    # `game_access.seat_for_uid`, never `visibility.resolve_viewer`: this is
    # an authorization question ("who is acting"), and `resolve_viewer`
    # answers the visibility one ("what may this uid see"), which resolves an
    # advisor uid to the human it watches. Asking the read filter who is
    # acting is what let an advisor credential answer its human's decisions.
    player = game_access.seat_for_uid(game_id, uid)
    if player is None or (
        player != req.player and not player_control.may_decide_for(game, player, req.player)
    ):
        # Refused before anything is enumerated: `req.player` is a request
        # field, and enumerating a seat the caller has merely *named* is
        # what put another seat's hidden information into a row the caller
        # could then read back. Naming a player this seat is controlling is
        # not merely naming them (CR 723.5).
        return refuse("that uid does not open the deciding player's seat")
    paused = store.paused_command(game_id)
    # CR 723.5: a decision addressed to a player somebody is controlling is
    # that controller's to answer, and not the controlled player's own.
    deciding = paused[2].decision.player if paused is not None else req.player
    # T10: computed once the caller's uid has been agreed to open `player`'s
    # own seat (or to be deciding for the player the question names), from
    # that side of the board before this answer runs, and reused for every
    # refusal below and the eventual success - `game` here is never touched by
    # anything until `_resume` takes its own lock and works against its own
    # copy.
    legal_before = records.legal_actions_before(
        game,
        deciding if player_control.may_decide_for(game, player, deciding) else player,
        paused[2] if paused is not None else None,
    )
    controlled = player_control.decision_refusal(game, player, deciding)
    if controlled is not None:
        return refuse(controlled)
    if paused is None or not player_control.may_decide_for(game, player, deciding):
        return refuse("no pending decision for you")
    if req.player != deciding:
        req = req.model_copy(update={"player": deciding})
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
    records.require_reasoning(game_id, player, req.reasoning)
    records.require_plan(game_id, player)
    command, tape, pending = paused
    engine_decision = pending.decision
    kind = engine_decision.kind
    detail: dict[str, Any] = {"kind": kind.value, "prompt": engine_decision.prompt}
    if kind is DecisionKind.CHOOSE_CARDS:
        try:
            answer_value: Any = decisions.card_ids_answer(req.card_ids)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["card_ids"] = req.card_ids
    elif kind is DecisionKind.CHOOSE_TARGETS:
        try:
            answer_value = decisions.targets_answer(req.targets, {p.username for p in game.players})
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
            return refuse(str(exc), **detail)
        detail["order"] = req.order
    elif kind is DecisionKind.PAY_MANA:
        # CR 608.2g / CR 605.3a: a bare yes/no, or - naming which offered
        # sources to activate (engine PR #17, B6-08) - `accept: true` plus
        # `mana_sources`. Its own branch, split out of the plain-bool group
        # below, because this is the one kind whose answer can be either
        # shape; `pay_mana_answer` decides which.
        try:
            answer_value = decisions.pay_mana_answer(req.accept, req.mana_sources)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["accept"] = req.accept
        if req.mana_sources is not None:
            detail["mana_sources"] = req.mana_sources
    elif kind in (
        DecisionKind.MAY,
        DecisionKind.PAY_LIFE,
        DecisionKind.PAY_ENERGY,
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
    elif kind is DecisionKind.PUT_INTO_HAND_OR_GRAVEYARD:
        # Dina's Guidance's "put it into your hand or graveyard" (CR 608.2d):
        # `True` for the hand, `False` for the graveyard.
        try:
            answer_value = decisions.hand_or_graveyard_answer(req.to_hand)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["to_hand"] = req.to_hand
    elif kind is DecisionKind.CHOOSE_PILE:
        # CR 700.3's pile primitive - a plain boolean, the same shape
        # `PUT_ON_TOP_OR_BOTTOM` already is: `True` for pile one
        # (`pending_decision.cards`), `False` for its complement.
        try:
            answer_value = decisions.pile_answer(req.pile_one)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["pile_one"] = req.pile_one
    elif kind is DecisionKind.CHOOSE_PLAYERS:
        # CR 701.34a's own Proliferate player half - `CHOOSE_CARDS`'s own
        # twin over usernames rather than card ids.
        answer_value = decisions.usernames_answer(req.usernames)
        detail["usernames"] = req.usernames
    elif kind in (DecisionKind.SCRY, DecisionKind.SURVEIL):
        try:
            answer_value = decisions.card_split_answer(req.moved, req.top_order)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["moved"] = req.moved
        detail["top_order"] = req.top_order
    elif kind in (DecisionKind.DECLARE_ATTACKERS, DecisionKind.DECLARE_BLOCKERS):
        # CR 508.1/509.1's combat declarations - answerable here directly
        # (the same pair shape `actions/attack`/`actions/declare-blockers`
        # build from their own request shape, see those routes) as well as
        # through the two aliases.
        try:
            answer_value = decisions.declared_pairs_answer(
                req.pairs, {p.username for p in game.players}
            )
        except ValueError as exc:
            return refuse(f"bad pair: {exc}", **detail)
        detail["pairs"] = [{"creature": str(p.creature), "defender": p.defender} for p in req.pairs]
    elif kind is DecisionKind.ASSIGN_COMBAT_DAMAGE:
        # CR 510.1c/702.19b: how the attacking creature's controller divides
        # its combat damage among its blockers and, with trample, the
        # defending player or planeswalker past them.
        try:
            answer_value = decisions.damage_answer(req.damage, {p.username for p in game.players})
        except ValueError as exc:
            return refuse(f"bad damage assignment: {exc}", **detail)
        detail["damage"] = [{"target": d.target, "amount": d.amount} for d in req.damage]
    elif kind is DecisionKind.DREDGE:
        # CR 702.52a: replace a draw by dredging one graveyard card, or
        # decline (an empty `card_ids`) and draw normally. Same request
        # field and coercion as `CHOOSE_CARDS` - `validate_answer` above
        # caps it at one card and checks it against `decision.cards`.
        try:
            answer_value = decisions.card_ids_answer(req.card_ids)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["card_ids"] = req.card_ids
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
    if kind is DecisionKind.PAY_MANA and isinstance(answer_value, tuple):
        # The engine's own `Answer` shape for this one: `card_ids` names the
        # mana sources to activate, and rides alongside `accept=True` rather
        # than replacing it (`Answer._check_one_value_field` refuses
        # `card_ids` without `accept=True` - see `pay_mana_answer`'s own
        # docstring for why declining and naming sources are mutually
        # exclusive already).
        answer = Answer(
            seq=pending.seq, kind=kind, player=req.player, accept=True, card_ids=answer_value
        )
    else:
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
    # This answer may complete the command it resumes (`commands.
    # _attach_notices` files any fail-closed notice the rest of the
    # resolution raised onto `result.outcome`, generically, the moment it
    # reaches `RunStatus.COMPLETED`) - engine 41's "you may shuffle" is the
    # case that made this matter: a `MAY` decision that used to resolve
    # inline inside whichever route raised it (`pass_priority`, most often)
    # now pauses *this* route for the answer instead, so the notice that
    # follows (Ponder's own unread-clause record, say) is filed on the
    # `RunResult` this call produces, not on the earlier paused one. `decide`
    # never read `outcome.get("notices")` at all before this - every sibling
    # mutating route already does (`audit_outcome`, `state_based_actions`,
    # `pass_priority`, ...) - so a notice raised only on the resuming half of
    # a paused command was silently dropped from both this response and the
    # persisted log row. `result.outcome` is either an `engine_api.Outcome`
    # (a paused cast/activate resumed to completion) or a plain `dict`
    # (`pass_priority`/`advance`/`state_based_actions`/...), the same
    # duality `commands._attach_notices` itself already handles.
    notices: list[dict[str, Any]] | None = None
    if result.status is commands.RunStatus.COMPLETED:
        outcome_detail = (
            result.outcome.detail
            if isinstance(result.outcome, engine_api.Outcome)
            else result.outcome
        )
        notices = outcome_detail.get("notices") if isinstance(outcome_detail, dict) else None
    seq = audit.record(
        game_id,
        "decide",
        player=req.player,
        allowed=True,
        reason=None,
        reasoning=req.reasoning,
        legal_actions_before=legal_before,
        _fill_seq=seq_hint,
        **({"notices": notices} if notices else {}),
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
            "pending_decision": _paused_pending_view(game_id, next_pending, req.player),
            "log_seq": seq,
        }
    response: dict[str, Any] = {"accepted": True, "log_seq": seq}
    if notices:
        response["notices"] = visibility.redact_notices(notices, req.player)
    return response


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
        result = {**result, "step": step_position(game), "settled_through": crossed}
    return result


@app.post(
    "/api/games/{game_id}/pregame/mulligan",
    dependencies=REFUSED_AFTER_GAME_OVER,
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
    records.require_current_board(
        game_id, "mulligan_declared", req.expected_seq, player=req.player, reasoning=req.reasoning
    )
    records.require_reasoning(game_id, req.player, req.reasoning)
    records.require_plan(game_id, req.player)
    with store.locked(game_id) as game:
        game.player(req.player)  # 404 for a player who is not in this game
        return opening.declare_mulligan(
            game_id, game, req.player, req.take, req.bottom, reasoning=req.reasoning
        )


@app.post(
    "/api/games/{game_id}/pregame/opening-hand/action",
    dependencies=REFUSED_AFTER_GAME_OVER,
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
    dependencies=REFUSED_AFTER_GAME_OVER,
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
    # P4, and the one route it was missing. This takes `schemas.ActorRequest`
    # and so has accepted `expected_seq` (via `BoardRead`) since the field
    # existed, while never comparing it to anything: a seat could name the
    # ledger position it decided against here and be allowed to act on a
    # board that had already moved, with no `stale` refusal and no row to say
    # it happened - the single guarantee this guard is documented to make
    # ("called first in every mutating route") broken on the one route
    # nobody had listed. CR 103.6's pass is as raceable as any other move:
    # the other seat's own mulligan or pass resolves the round, and this one
    # arriving after that is exactly the race.
    #
    # After `_pregame_seat` rather than before it, for the reason
    # `pregame_settle` states: the refusal writes a ledger row, and a caller
    # who cannot open this seat has no business writing one.
    records.require_current_board(
        game_id, "opening_hand_done", req.expected_seq, player=req.player, reasoning=req.reasoning
    )
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
    dependencies=REFUSED_AFTER_GAME_OVER,
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
# `ui/manifest.webmanifest` must answer as a manifest. Python's own table says
# so, but a host's /etc/mime.types is read over it, so pin it for every image.
mimetypes.add_type("application/manifest+json", ".webmanifest")
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
