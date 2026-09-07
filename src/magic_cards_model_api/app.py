"""The FastAPI app: session setup, state, legality questions, and the frontend.

Everything lives under /api; / serves this repo's own play UI (`ui/`), and the
sibling frontend repo, when it is checked out next to this one, sits at
/frontend. /play/ - where the play UI used to live - redirects to /. There is
deliberately no CORS: the UI is served by this app itself, so there is exactly
one origin.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from magic_cards_model.machines import Step
from magic_cards_model.machines.decisions import Answer, DecisionKind
from magic_cards_model.machines.decisions import PendingDecision as PendingDecisionEngine
from magic_cards_model.machines.legal_actions import legal_actions as compute_legal_actions
from magic_cards_model.machines.priority import PriorityOutcome
from magic_cards_model.models import Card, CardState, Color, Game, Player, Zone

from . import (
    affordances,
    ai_seat_api,
    audit,
    commands,
    decisions,
    decks_api,
    engine_api,
    opening,
    scryfall,
    seatkit,
    symbology,
    visibility,
)
from .schemas import (
    ActivateRequest,
    ActorRequest,
    AdvanceRequest,
    AttackRequest,
    CastRequest,
    ConcedeRequest,
    CrewRequest,
    CycleRequest,
    DecideRequest,
    DeclareBlockersRequest,
    DeclaredPairRequest,
    ManaPoolUpdate,
    MoveCard,
    MulliganRequest,
    NewGame,
    OpeningHandActionRequest,
    PassPriorityRequest,
    PatchCard,
    PatchPlayer,
    PlayLandRequest,
    PregameSettleRequest,
    PutCard,
    ReportProblemRequest,
    SpecialActionRequest,
    StepUpdate,
    TargetRequest,
)
from .serialize import state_view
from .store import new_seat_uids, store

app = FastAPI(title="magic-cards-model-api", version="0.1.0")
app.include_router(decks_api.router)
app.include_router(ai_seat_api.router)
app.include_router(symbology.router)


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
    game_id: str, action: str, outcome: engine_api.Outcome, *, dry_run: bool = False, **keys: Any
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
    """
    if dry_run:
        return {**outcome.body(), "log_seq": None}
    seq = audit.record(game_id, action, allowed=outcome.allowed, reason=outcome.reason, **keys)
    return {**outcome.body(), "log_seq": seq}


def _run_command(game_id: str, kind: str, request: Any) -> tuple[commands.RunResult, Game]:
    """Run `kind` fresh against `game_id`'s current committed state
    (`commands.run_command`), and settle the result into the store: commit
    the new game on `RunStatus.COMPLETED`, or stash `(command, tape,
    pending)` for `POST .../decide` (and its `actions/attack`/
    `actions/declare-blockers` aliases) to resume on `PAUSED`.

    Returns `(result, committed)` - `committed` is the game exactly as it
    stood before this attempt (the `Game` `store.locked` handed this
    request), which a few callers (`pass-priority`'s before/after resolution
    diff) still need after `run_command` returns its own, separate working
    copy.

    **Deliberately clears any *unrelated* stored pause too.** `store
    .clear_paused_command` runs here on every `COMPLETED` result, not only
    one that resumed the paused command it is clearing - so an unrelated
    mutating call that completes while a *different* decision sits paused
    (a spectator's board-setup poke, another seat's own unrelated action)
    discards that stored pause outright rather than leaving it stale
    alongside a game that has since moved on. This is safe because a paused
    command was never applied to the committed game in the first place - see
    `commands.py`'s own module docstring, "no half-applied `Game` kept
    anywhere" - so nothing here is thrown away but a *question*, and the
    exact same question resurfaces, byte-identical, the moment whatever
    would have asked it is attempted again (`run_command`'s replay is
    deterministic). The alternative - clearing only when the completed
    command IS the one that was paused - would leave a genuinely stale
    pause sitting in the store forever once its game state has moved past
    the point that ever asked it, which is worse: a `pending_decision` a
    live seat can never actually answer, since resuming it would replay
    against state that no longer produces the same question at all. See
    `tests/test_decisions.py::
    test_an_unrelated_completed_command_clears_an_unrelated_pause` for the
    pinned behaviour.
    """
    command = commands.Command.build(kind, request)
    with store.locked(game_id) as game:
        result = commands.run_command(game, command)
        if result.status is commands.RunStatus.COMPLETED:
            assert result.game is not None
            store.replace(game_id, result.game)
            store.clear_paused_command(game_id)
        else:
            assert result.pending is not None
            store.store_paused_command(game_id, command, result.tape, result.pending)
        return result, game


def _respond(
    game_id: str, action: str, result: commands.RunResult, *, dry_run: bool = False, **keys: Any
) -> dict[str, Any]:
    """Turn a `_run_command` attempt into this route's JSON response,
    auditing it either way - refused, allowed, or paused waiting on a
    decision (`allowed: None`) are all real outcomes worth a log entry, the
    same way a refusal already was. `keys` are the action-specific detail
    `audit.record` already took for this route (the card cast, the targets
    offered, ...) before this task, unchanged by which of the three this
    attempt came to.

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
        pending = decisions.PendingDecision.from_engine(result.pending.decision)
        body = {
            "allowed": None,
            "reason": None,
            "paused": True,
            "pending_decision": pending.to_view(),
        }
        if dry_run:
            return {**body, "log_seq": None}
        seq = audit.record(game_id, action, allowed=None, reason=None, paused=True, **keys)
        return {**body, "log_seq": seq}
    assert result.outcome is not None
    return audit_outcome(game_id, action, result.outcome, dry_run=dry_run, **keys)


def _resume(
    game_id: str,
    command: commands.Command,
    tape: tuple[Answer, ...],
    pending: PendingDecisionEngine,
    answer: Answer,
) -> commands.RunResult:
    """Resume `game_id`'s paused command with `answer` appended to `tape`
    (`commands.resume_command`), and settle the result into the store
    exactly like `_run_command` does for a fresh command: commit on
    `RunStatus.COMPLETED`, restash `(command, tape, pending)` on `PAUSED`.

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
        result = commands.resume_command(game, command, tape, pending, answer)
        if result.status is commands.RunStatus.COMPLETED:
            assert result.game is not None
            store.replace(game_id, result.game)
            store.clear_paused_command(game_id)
        else:
            assert result.pending is not None
            store.store_paused_command(game_id, command, result.tape, result.pending)
        return result


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


@app.get("/api/health")
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
        "protocol_version": seatkit.PROTOCOL_VERSION,
    }


@app.post("/api/games")
def create_game(req: NewGame) -> dict[str, Any]:
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
    game_id = store.create(game)
    uids = new_seat_uids(game)
    store.register_seats(game_id, uids)
    engine_api.set_step(game, Step(req.step))
    audit.record(game_id, "create_game", players=names, active_player=active, step=req.step)
    # The creator gets the seat uids AND the admin key in this one response -
    # already holding the keys to every seat, an unredacted view here tells
    # it nothing a filtered one would have hidden.
    return {
        **state_view(game, game_id),
        "seat_uids": {name: uid for uid, name in uids.items()},
        "admin_key": store.admin_key(game_id),
    }


@app.get("/api/games/{game_id}/seat")
def whose_seat(game_id: str, uid: str) -> dict[str, Any]:
    """Whose seat this uid opens. A uid that is not in the game is nobody's."""
    game_or_404(game_id)
    player = store.player_for_uid(game_id, uid)
    if player is None:
        raise KeyError("that uid is no seat in this game")
    return {"player": player}


@app.get("/api/games")
def list_games() -> dict[str, Any]:
    games = []
    for game_id in store.ids():
        game = game_or_404(game_id)
        games.append(
            {
                "game_id": game_id,
                "players": [player.username for player in game.players],
                "step": game.turn.step.value if game.turn else None,
            }
        )
    return {"games": games}


@app.get("/api/games/{game_id}")
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


@app.get("/api/games/{game_id}/log")
def game_log(game_id: str, uid: str | None = None, since: int = 0) -> dict[str, Any]:
    """The action log.

    Unlike `/api/games/{game_id}` this defaults to full: a bare call (no
    uid, or an unrecognised one) is treated as the debugger's eye that
    `audit.py` already documents its snapshots for - the same view the admin
    key gets. A real seat uid instead gets its own filtered view, because
    this is also the log mcp.py's `game_log` tool hands a live player - and
    that must not read the opponent's hand any more than the state endpoint
    does.

    `since` is the last `seq` the caller already has; only newer entries
    come back, and `last_seq` says where to resume next time.
    """
    game_or_404(game_id)  # 404 for an unknown game, like every other endpoint
    viewer = visibility.resolve_viewer(game_id, uid)
    log = audit.entries(game_id)
    last_seq = log[-1]["seq"] if log else 0
    log = [entry for entry in log if entry["seq"] > since]
    if isinstance(viewer, str):  # a real seat - not admin, not "nobody"
        log = [visibility.redact_log_entry(entry, viewer) for entry in log]
    return {"log": log, "last_seq": last_seq}


@app.delete("/api/games/{game_id}")
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


@app.get("/api/card-names")
def card_names(q: str = "") -> dict[str, Any]:
    return {"suggestions": scryfall.autocomplete(q)}


@app.get("/api/cards")
def lookup_card_query(name: str) -> dict[str, Any]:
    return scryfall.lookup_card(name)


@app.get("/api/cards/{name}")
def lookup_card(name: str) -> dict[str, Any]:
    return scryfall.lookup_card(name)


# -- setting the board up ---------------------------------------------------


@app.post("/api/games/{game_id}/cards")
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


@app.post("/api/games/{game_id}/cards/{card_id}/move")
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


@app.post("/api/games/{game_id}/cards/{card_id}/tap")
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


@app.patch("/api/games/{game_id}/cards/{card_id}")
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


@app.patch("/api/games/{game_id}/players/{username}")
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


@app.put("/api/games/{game_id}/players/{username}/mana-pool")
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


@app.put("/api/games/{game_id}/step")
def set_step(game_id: str, req: StepUpdate, uid: str | None = None) -> dict[str, Any]:
    with store.locked(game_id) as game:
        before = game.turn.step.value if game.turn is not None else None
        engine_api.set_step(game, Step(req.step))
        suspend_upkeep: list[dict[str, Any]] = []
        if Step(req.step) is Step.UPKEEP and game.turn is not None and req.player:
            # Same hand-driven trigger `advance` runs - a seat stepping
            # straight to its own upkeep (rather than passing priority
            # through to it) must not skip CR 702.62a's counter removal.
            suspend_upkeep = engine_api.drive_suspend_upkeep(game, game.turn.active_player)
        audit.record(
            game_id,
            "step",
            player=req.player,
            suspend_upkeep=suspend_upkeep,
            **{"from": before, "to": req.step},
        )
        viewer = visibility.resolve_viewer(game_id, uid)
        return {
            **visibility.redact_view(state_view(game, game_id), viewer),
            "suspend_upkeep": suspend_upkeep,
        }


# -- the heart: legality questions ------------------------------------------


@app.post("/api/games/{game_id}/actions/cast")
def action_cast(game_id: str, req: CastRequest) -> dict[str, Any]:
    """Cast a card. `dry_run` never asks anything unscripted (it builds and
    rewinds its `Casting` against an isolated probe copy of the game - see
    `engine_api.dry_run_copy` - never through the pause/resume tape), so it
    always completes; a real cast can pause on any CR 601.2 choice this
    engine still asks interactively (`choose_targets`, `choose_a_mode`, ...)."""
    result, _ = _run_command(game_id, "cast", req)
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
    )


@app.post("/api/games/{game_id}/actions/suspend")
def action_suspend(game_id: str, req: SpecialActionRequest) -> dict[str, Any]:
    """CR 116.2f/702.62a: exile a card with suspend from hand, paying its
    suspend cost, with N time counters. The free cast once the last counter
    comes off is picked up automatically by `actions/cast` - see `advance`."""
    result, _ = _run_command(game_id, "suspend", req)
    return _respond(
        game_id,
        "suspend",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card=req.card or str(req.card_id),
        mana_sources=[str(source) for source in req.mana_sources or []],
    )


@app.post("/api/games/{game_id}/actions/plot")
def action_plot(game_id: str, req: SpecialActionRequest) -> dict[str, Any]:
    """CR 116.2k/702.170b: exile a card with plot from hand, paying its plot
    cost. Cast it later with `actions/cast`, `keyword: "Plot"`, on a later turn."""
    result, _ = _run_command(game_id, "plot", req)
    return _respond(
        game_id,
        "plot",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card=req.card or str(req.card_id),
        mana_sources=[str(source) for source in req.mana_sources or []],
    )


@app.post("/api/games/{game_id}/actions/foretell")
def action_foretell(game_id: str, req: SpecialActionRequest) -> dict[str, Any]:
    """CR 116.2h/702.143b: pay {2} and exile a card with foretell from hand,
    face down. Cast it later with `actions/cast`, `keyword: "Foretell"`, on a
    later turn, for its printed foretell cost."""
    result, _ = _run_command(game_id, "foretell", req)
    return _respond(
        game_id,
        "foretell",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card=req.card or str(req.card_id),
        mana_sources=[str(source) for source in req.mana_sources or []],
    )


@app.post("/api/games/{game_id}/actions/cycle")
def action_cycle(game_id: str, req: CycleRequest) -> dict[str, Any]:
    """CR 702.29a: discard a card with cycling from hand, paying its cycling
    cost, to draw a card (or search for a typecycled one). Puts the ability on
    the stack, same as `actions/activate` - resolve it with `resolve-top`."""
    result, _ = _run_command(game_id, "cycle", req)
    return _respond(
        game_id,
        "cycle",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card=req.card or str(req.card_id),
        ability_index=req.ability_index,
        mana_sources=[str(source) for source in req.mana_sources or []],
    )


@app.post("/api/games/{game_id}/actions/target")
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


@app.get("/api/games/{game_id}/affordances/{card_id}")
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


@app.get("/api/games/{game_id}/legal-actions")
def get_legal_actions(game_id: str, player: str) -> dict[str, Any]:
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

    Every action is already `player`'s own by construction - hand cards,
    controlled permanents, this player's own graveyard/exile cast options -
    so, like `get_affordances`, this needs no `visibility.redact_view` pass:
    there is nothing of the opponent's in here to redact.
    """
    game = game_or_404(game_id)
    paused = store.paused_command(game_id)
    pending = paused[2] if paused is not None else None
    actions = compute_legal_actions(game, player, pending=pending)
    return {"actions": [action.model_dump(mode="json") for action in actions]}


@app.post("/api/games/{game_id}/actions/concede")
def action_concede(game_id: str, req: ConcedeRequest) -> dict[str, Any]:
    """Concede (CR 104.3a). A player may do this at any time, and leaves the
    game immediately - there is no priority check and nothing to refuse, which
    is why this takes no `Outcome` the way the other actions do. `Game.concede`
    asks nothing of its own, but this still goes through the pause machinery
    like every other command, on the chance a state-based check it triggers
    ever does (a legend rule, a leaves-the-battlefield trigger's targets)."""
    result, _ = _run_command(game_id, "concede", req)
    if result.status is commands.RunStatus.PAUSED:
        assert result.pending is not None
        pending = decisions.PendingDecision.from_engine(result.pending.decision)
        seq = audit.record(game_id, "concede", player=req.player, allowed=None, paused=True)
        return {
            "allowed": None,
            "paused": True,
            "pending_decision": pending.to_view(),
            "log_seq": seq,
        }
    assert isinstance(result.outcome, dict)
    seq = audit.record(game_id, "concede", player=req.player, allowed=True)
    return {"allowed": True, "winner": result.outcome["winner"], "log_seq": seq}


@app.post("/api/games/{game_id}/actions/activate")
def action_activate(game_id: str, req: ActivateRequest) -> dict[str, Any]:
    result, _ = _run_command(game_id, "activate_ability", req)
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
    )


@app.post("/api/games/{game_id}/actions/crew")
def action_crew(game_id: str, req: CrewRequest) -> dict[str, Any]:
    """Crew a Vehicle (CR 702.122a). Its own endpoint rather than an
    `activate` against an ability index: the cost is the creatures tapped,
    not mana, and crew is the Vehicle's ability rather than theirs."""
    result, _ = _run_command(game_id, "crew", req)
    return _respond(
        game_id,
        "crew",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card_id=str(req.card_id),
        crewing=[str(card_id) for card_id in req.crewing],
    )


def _answer_declaration(
    game_id: str,
    expected_kind: DecisionKind,
    pairs: list[DeclaredPairRequest],
    *,
    action: str,
    uid: str | None = None,
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

    def refuse(reason: str) -> dict[str, Any]:
        seq = audit.record(game_id, action, allowed=False, reason=reason, **audit_keys)
        return {"allowed": False, "reason": reason, "log_seq": seq}

    game_or_404(game_id)
    paused = store.paused_command(game_id)
    if paused is None:
        return refuse(f"no pending {expected_kind.value} decision - call POST .../advance first")
    command, tape, pending = paused
    engine_decision = pending.decision
    if engine_decision.kind is not expected_kind:
        return refuse(
            f"the pending decision is {engine_decision.kind.value!r}, not "
            f"{expected_kind.value!r} - answer that one first (POST .../decide, "
            f"or the matching action)"
        )
    viewer = visibility.resolve_viewer(game_id, uid)
    if viewer != engine_decision.player or not isinstance(viewer, str):
        return refuse("that uid does not open the deciding player's seat")
    try:
        value = decisions.declared_pairs_answer(pairs)
        decisions.validate_answer(engine_decision, value)
    except ValueError as exc:
        return refuse(str(exc))
    answer = Answer(
        seq=pending.seq, kind=engine_decision.kind, player=engine_decision.player, pairs=value
    )
    try:
        result = _resume(game_id, command, tape, pending, answer)
    except ValueError as exc:
        return refuse(str(exc))
    seq = audit.record(game_id, action, allowed=True, reason=None, **audit_keys)
    if result.status is commands.RunStatus.PAUSED:
        assert result.pending is not None
        next_pending = decisions.PendingDecision.from_engine(result.pending.decision)
        return {
            "allowed": True,
            "paused": True,
            "pending_decision": next_pending.to_view(),
            "log_seq": seq,
        }
    return {"allowed": True, "log_seq": seq}


@app.post("/api/games/{game_id}/actions/attack")
def action_attack(game_id: str, req: AttackRequest, uid: str | None = None) -> dict[str, Any]:
    """Declare attackers (CR 508.1) - see `_answer_declaration`. `dry_run`
    answers without touching the pending decision at all: it only checks
    each pair's legality (`Game.why_cannot_attack`) against the game as it
    stands right now, the same probe it always was - so it needs no `uid`
    gate any more than any other dry run does; only a real declaration does.
    """
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
        attackers=attackers_detail,
        dry_run=False,
    )


@app.post("/api/games/{game_id}/actions/play-land")
def action_play_land(game_id: str, req: PlayLandRequest) -> dict[str, Any]:
    result, _ = _run_command(game_id, "play_land", req)
    return _respond(
        game_id,
        "play_land",
        result,
        dry_run=req.dry_run,
        player=req.player,
        card=req.card or str(req.card_id),
    )


@app.post("/api/games/{game_id}/actions/pass-priority")
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
    try:
        result, _ = _run_command(game_id, "pass_priority", req)
    except ValueError as exc:  # not this player's decision to make
        seq = audit.record(
            game_id, "pass_priority", player=req.player, allowed=False, reason=str(exc)
        )
        return {"allowed": False, "reason": str(exc), "outcome": None, "log_seq": seq}
    if result.status is commands.RunStatus.PAUSED:
        assert result.pending is not None
        pending = decisions.PendingDecision.from_engine(result.pending.decision)
        seq = audit.record(game_id, "pass_priority", player=req.player, allowed=None, paused=True)
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
        game_id, "pass_priority", player=req.player, allowed=True, reason=None, **audit_detail
    )
    return {
        "allowed": True,
        "reason": None,
        "outcome": priority_outcome.value,
        "settled_through": settled,
        "log_seq": seq,
    }


@app.post("/api/games/{game_id}/actions/declare-blockers")
def action_declare_blockers(
    game_id: str, req: DeclareBlockersRequest, uid: str | None = None
) -> dict[str, Any]:
    """Declare blockers (CR 509.1) - see `_answer_declaration`."""
    blocks_detail = [{"blocker": str(b.blocker), "blocked": str(b.blocked)} for b in req.blocks]
    pairs = [DeclaredPairRequest(creature=b.blocker, defender=str(b.blocked)) for b in req.blocks]
    return _answer_declaration(
        game_id,
        DecisionKind.DECLARE_BLOCKERS,
        pairs,
        action="declare_blockers",
        uid=uid,
        player=req.player,
        blocks=blocks_detail,
    )


# -- running the game forward ------------------------------------------------


@app.post("/api/games/{game_id}/state-based-actions")
def state_based_actions(game_id: str) -> dict[str, Any]:
    result, _ = _run_command(game_id, "state_based_actions", ActorRequest())
    if result.status is commands.RunStatus.PAUSED:
        seq = audit.record(game_id, "state_based_actions", allowed=None, paused=True)
        game = game_or_404(game_id)
        return {**state_view(game, game_id), "allowed": None, "paused": True, "log_seq": seq}
    assert result.game is not None and isinstance(result.outcome, dict)
    audit.record(
        game_id,
        "state_based_actions",
        destroyed=result.outcome["destroyed"],
        changed=result.outcome["changed"],
    )
    return {**state_view(result.game, game_id), **result.outcome}


@app.post("/api/games/{game_id}/resolve-top")
def resolve_top(
    game_id: str, req: ActorRequest | None = None, uid: str | None = None
) -> dict[str, Any]:
    result, _ = _run_command(game_id, "resolve_top", req or ActorRequest())
    viewer = visibility.resolve_viewer(game_id, uid)
    if result.status is commands.RunStatus.PAUSED:
        seq = audit.record(
            game_id, "resolve_top", player=req.player if req else None, allowed=None, paused=True
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
    seq = audit.record(game_id, "resolve_top", player=req.player if req else None, **result.outcome)
    return {
        **visibility.redact_view(state_view(result.game, game_id), viewer),
        **result.outcome,
        "log_seq": seq,
    }


@app.post("/api/games/{game_id}/advance")
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
    try:
        result, committed = _run_command(game_id, "advance", req)
    except ValueError as exc:  # the engine's own refusal text
        seq = audit.record(game_id, "advance", player=req.player, allowed=False, reason=str(exc))
        return {"allowed": False, "reason": str(exc), "log_seq": seq}
    before = committed.turn.step.value if committed.turn is not None else None
    viewer = visibility.resolve_viewer(game_id, uid)
    if result.status is commands.RunStatus.PAUSED:
        seq = audit.record(game_id, "advance", player=req.player, allowed=None, paused=True)
        return {
            **visibility.redact_view(state_view(committed, game_id), viewer),
            "allowed": None,
            "paused": True,
            "log_seq": seq,
        }
    assert result.game is not None and isinstance(result.outcome, dict)
    step_value = result.outcome["advanced_to"]
    suspend_upkeep = result.outcome["suspend_upkeep"]
    seq = audit.record(
        game_id,
        "advance",
        player=req.player,
        allowed=True,
        suspend_upkeep=suspend_upkeep,
        **{"from": before, "to": step_value},
    )
    return {
        **visibility.redact_view(state_view(result.game, game_id), viewer),
        "advanced_to": step_value,
        "suspend_upkeep": suspend_upkeep,
        "allowed": True,
        "log_seq": seq,
    }


@app.post("/api/games/{game_id}/report")
def report_problem(game_id: str, req: ReportProblemRequest) -> dict[str, Any]:
    """A player files a problem report straight into the game log."""
    game = game_or_404(game_id)
    game.player(req.player)
    audit.record(game_id, "report_problem", player=req.player, text=req.text)
    return {"recorded": True}


@app.post("/api/games/{game_id}/decide")
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
            game_id, "decide", player=req.player, allowed=False, reason=reason, **detail
        )
        return {"accepted": False, "reason": reason, "log_seq": seq}

    game_or_404(game_id)
    player = visibility.resolve_viewer(game_id, uid)
    if player != req.player or not isinstance(player, str):
        return refuse("that uid does not open the deciding player's seat")
    paused = store.paused_command(game_id)
    if paused is None or paused[2].decision.player != req.player:
        return refuse("no pending decision for you")
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
        result = _resume(game_id, command, tape, pending, answer)
    except ValueError as exc:
        # The engine itself refused this answer once the command actually
        # replayed with it - reached, in practice, only by the two
        # declaration kinds (`validate_answer`'s own docstring), since every
        # other kind's shape check above already mirrors what the engine
        # would check. Refused the same synchronous way as every check above.
        return refuse(str(exc), **detail)
    seq = audit.record(game_id, "decide", player=req.player, allowed=True, reason=None, **detail)
    if result.status is commands.RunStatus.PAUSED:
        assert result.pending is not None
        next_pending = decisions.PendingDecision.from_engine(result.pending.decision)
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
    viewer = visibility.resolve_viewer(game_id, uid)
    if viewer is visibility.ADMIN:
        return
    if viewer != player:
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


@app.post("/api/games/{game_id}/pregame/mulligan")
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
    """
    _pregame_seat(game_id, uid, req.player)
    with store.locked(game_id) as game:
        game.player(req.player)  # 404 for a player who is not in this game
        return opening.declare_mulligan(game_id, game, req.player, req.take, req.bottom)


@app.post("/api/games/{game_id}/pregame/opening-hand/action")
def pregame_opening_hand_action(
    game_id: str, req: OpeningHandActionRequest, uid: str | None = None
) -> dict[str, Any]:
    """Take one CR 103.6 action with a card in this seat's opening hand.

    "Once the mulligan process (see rule 103.5) is complete, the starting
    player may take any such actions in any order." What is on offer, and to
    whom right now, is `pregame.opening_hand_options` / `pregame.
    opening_hand_actor` on the state view; a seat that wants none of them calls
    the `finish` endpoint below instead.
    """
    _pregame_seat(game_id, uid, req.player)
    with store.locked(game_id) as game:
        game.player(req.player)
        return opening.take_opening_hand_action(game_id, game, req.player, req.card_id)


@app.post("/api/games/{game_id}/pregame/opening-hand/finish")
def pregame_opening_hand_finish(
    game_id: str, req: ActorRequest, uid: str | None = None
) -> dict[str, Any]:
    """This seat is done taking CR 103.6 actions.

    The next seat is up; when the last one finishes, CR 103.8's first turn
    begins - at its untap step, in which no player receives priority (CR
    502.4). Nobody has to be asked to leave that step (CR 500.3), so the
    response's `step` is the first one somebody can act in: `upkeep`. See
    `engine_api.settle_priorityless_steps`.
    """
    if not req.player:
        raise ValueError("finishing CR 103.6's actions needs the player who is finishing")
    _pregame_seat(game_id, uid, req.player)
    with store.locked(game_id) as game:
        game.player(req.player)
        result = opening.finish_opening_hand_actions(game_id, game, req.player)
        return _settled(game, result)


@app.post("/api/games/{game_id}/pregame/settle")
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
    if visibility.resolve_viewer(game_id, uid) is not visibility.ADMIN:
        raise ValueError("settling the whole pregame for every seat needs this game's admin key")
    viewer = visibility.resolve_viewer(game_id, uid)
    if req.use_defaults:
        command = commands.Command.build("pregame_settle", req)
        with store.locked(game_id) as game:
            result = commands.run_command_to_completion(game, command)
            assert result.status is commands.RunStatus.COMPLETED
            assert result.game is not None
            store.replace(game_id, result.game)
            store.clear_paused_command(game_id)
    else:
        result, _ = _run_command(game_id, "pregame_settle", req)
    if result.status is commands.RunStatus.PAUSED:
        assert result.pending is not None
        seq = audit.record(game_id, "pregame_settle", allowed=None, paused=True)
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

    app.mount("/", StaticFiles(directory=_ui_dir, html=True), name="play-ui")
elif _frontend is not None:
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend-root")
else:

    @app.get("/", include_in_schema=False)
    def landing() -> HTMLResponse:
        return HTMLResponse(_LANDING)
