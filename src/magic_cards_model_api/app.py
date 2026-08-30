"""The FastAPI app: session setup, state, legality questions, and the frontend.

Everything lives under /api; / serves the sibling frontend repo when it exists.
There is deliberately no CORS: the frontend is served by this app itself, so
there is exactly one origin.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from magic_cards_model.machines import Step
from magic_cards_model.machines.decisions import DecisionKind
from magic_cards_model.machines.priority import PriorityOutcome
from magic_cards_model.models import Card, CardState, Color, Game, Player, Zone

from . import audit, decisions, engine_api, scryfall, seatkit, visibility
from .schemas import (
    ActivateRequest,
    ActorRequest,
    AdvanceRequest,
    AttackRequest,
    CastRequest,
    DecideRequest,
    DeclareBlockersRequest,
    ManaPoolUpdate,
    MoveCard,
    NewGame,
    PassPriorityRequest,
    PatchCard,
    PatchPlayer,
    PlayLandRequest,
    PutCard,
    ReportProblemRequest,
    StepUpdate,
    TargetRequest,
)
from .serialize import snapshot, state_view
from .store import new_seat_uids, store

app = FastAPI(title="magic-cards-model-api", version="0.1.0")


def _resolution_state(game: Game) -> dict[str, Any]:
    """The snapshot a resolution is meant to change, minus the stack itself.

    Resolving always pops the stack, so the stack cannot say whether the
    resolution *did* anything. Everything else can: an effect the engine has
    no implementation for leaves board, hands, zones, life and pools exactly
    as they were.
    """
    return {key: value for key, value in snapshot(game).items() if key != "stack"}


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
    game_id: str, action: str, outcome: engine_api.Outcome, **keys: Any
) -> dict[str, Any]:
    """Record an action attempt and hand the engine's answer to the caller.

    `log_seq` is the audit entry's own monotonic sequence number
    (`audit.record`'s return value) - a cheap, already-existing idempotency
    marker: a caller unsure whether a timed-out call actually landed can
    compare the `log_seq` on a later, successful call (or a `GET .../log`
    read) against what it saw before, rather than guessing from a retry.
    """
    seq = audit.record(game_id, action, allowed=outcome.allowed, reason=outcome.reason, **keys)
    return {**outcome.body(), "log_seq": seq}


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

    `decision_timeout_seconds` is `decisions.DEFAULT_TIMEOUT_SECONDS` - the
    longest a mutating request can legitimately block inside `Game.ask`
    waiting on a live CR 608.2d answer (`decisions.py`'s module docstring).
    `seatkit.resolve_client_timeout` reads this to size a seat's own HTTP
    read timeout so a genuine block never looks like a dead server - the two
    numbers come from this one field rather than being two constants a
    future edit to either repo could quietly desynchronise."""
    return {
        "status": "ok",
        "protocol_version": seatkit.PROTOCOL_VERSION,
        "decision_timeout_seconds": decisions.DEFAULT_TIMEOUT_SECONDS,
    }


@app.post("/api/games")
def create_game(req: NewGame) -> dict[str, Any]:
    names = [player.username for player in req.players]
    if len(set(names)) != len(names):
        raise ValueError("player names must be unique")
    game = Game(
        players=[Player(username=player.username, life=player.life) for player in req.players]
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
    and both public boards; the game's admin key sees everything."""
    game = game_or_404(game_id)
    viewer = visibility.resolve_viewer(game_id, uid)
    return visibility.redact_view(state_view(game, game_id), viewer)


@app.get("/api/games/{game_id}/log")
def game_log(game_id: str, uid: str | None = None) -> dict[str, Any]:
    """The action log.

    Unlike `/api/games/{game_id}` this defaults to full: a bare call (no
    uid, or an unrecognised one) is treated as the debugger's eye that
    `audit.py` already documents its snapshots for - the same view the admin
    key gets. A real seat uid instead gets its own filtered view, because
    this is also the log mcp.py's `game_log` tool hands a live player - and
    that must not read the opponent's hand any more than the state endpoint
    does.
    """
    game_or_404(game_id)  # 404 for an unknown game, like every other endpoint
    viewer = visibility.resolve_viewer(game_id, uid)
    log = audit.entries(game_id)
    if isinstance(viewer, str):  # a real seat - not admin, not "nobody"
        log = [visibility.redact_log_entry(entry, viewer) for entry in log]
    return {"log": log}


@app.delete("/api/games/{game_id}")
def delete_game(game_id: str) -> dict[str, Any]:
    game = store.get(game_id)
    if game is None:
        raise KeyError(f"no game {game_id}")
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
        audit.record(game_id, "step", player=req.player, **{"from": before, "to": req.step})
        viewer = visibility.resolve_viewer(game_id, uid)
        return visibility.redact_view(state_view(game, game_id), viewer)


# -- the heart: legality questions ------------------------------------------


@app.post("/api/games/{game_id}/actions/cast")
def action_cast(game_id: str, req: CastRequest) -> dict[str, Any]:
    with store.locked(game_id) as game:
        game.player(req.player)
        outcome = engine_api.cast_card(game, req)
        return audit_outcome(
            game_id,
            "cast",
            outcome,
            player=req.player,
            card=req.card or str(req.card_id),
            from_zone=req.from_zone,
            targets=req.targets,
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


@app.post("/api/games/{game_id}/actions/activate")
def action_activate(game_id: str, req: ActivateRequest) -> dict[str, Any]:
    with store.locked(game_id) as game:
        game.player(req.player)
        outcome = engine_api.activate_ability(game, req)
        return audit_outcome(
            game_id,
            "activate",
            outcome,
            player=req.player,
            card_id=str(req.card_id),
            ability_index=req.ability_index,
            targets=req.targets,
            mana_sources=[str(source) for source in req.mana_sources or []],
        )


@app.post("/api/games/{game_id}/actions/attack")
def action_attack(game_id: str, req: AttackRequest) -> dict[str, Any]:
    with store.locked(game_id) as game:
        outcome = engine_api.declare_attack(game, req)
        return audit_outcome(
            game_id,
            "attack",
            outcome,
            attackers=[
                {"card": str(pair.card_id), "defender": pair.defender} for pair in req.attackers
            ],
            dry_run=req.dry_run,
        )


@app.post("/api/games/{game_id}/actions/play-land")
def action_play_land(game_id: str, req: PlayLandRequest) -> dict[str, Any]:
    with store.locked(game_id) as game:
        game.player(req.player)
        if req.card_id is not None:
            land = card_or_404(game, req.card_id)
        elif req.card:
            matches = [
                state
                for state in game.player(req.player).hand
                if state.card.name == req.card and state.card.is_land
            ]
            if not matches:
                raise ValueError(f"{req.player} has no land named {req.card} in hand")
            land = matches[0]
        else:
            raise ValueError("give either card_id or card (a name)")
        outcome = engine_api.play_land(game, req.player, land)
        return audit_outcome(game_id, "play_land", outcome, player=req.player, card=land.card.name)


@app.post("/api/games/{game_id}/actions/pass-priority")
def action_pass_priority(game_id: str, req: PassPriorityRequest) -> dict[str, Any]:
    """Pass priority - "I am done for now". `outcome` tells the caller what
    the pass caused: `resolve_top` (the stack's top resolved, priority goes
    round again) or `end_step` (everyone passed on an empty stack, so the
    step ended and the next has already begun - CR 117.4, 405.5)."""
    with store.locked(game_id) as game:
        resolving = game.stack[-1].name if game.stack else None
        before = _resolution_state(game)
        try:
            outcome = game.pass_priority(req.player)
        except ValueError as exc:  # not this player's decision to make
            seq = audit.record(
                game_id, "pass_priority", player=req.player, allowed=False, reason=str(exc)
            )
            return {"allowed": False, "reason": str(exc), "outcome": None, "log_seq": seq}
        detail: dict[str, Any] = {"outcome": outcome.value}
        if outcome is PriorityOutcome.RESOLVE_TOP:
            # What resolved, and whether it changed anything. A spell whose
            # effect the engine does not know resolves and leaves the game
            # untouched, and without this the log cannot tell that apart from
            # one that worked.
            detail["resolved"] = resolving
            detail["changed"] = _resolution_state(game) != before
        seq = audit.record(
            game_id, "pass_priority", player=req.player, allowed=True, reason=None, **detail
        )
        return {"allowed": True, "reason": None, "outcome": outcome.value, "log_seq": seq}


@app.post("/api/games/{game_id}/actions/declare-blockers")
def action_declare_blockers(game_id: str, req: DeclareBlockersRequest) -> dict[str, Any]:
    with store.locked(game_id) as game:
        pairs = [(card_or_404(game, b.blocker), card_or_404(game, b.blocked)) for b in req.blocks]
        keys = dict(
            player=req.player,
            blocks=[{"blocker": str(b.blocker), "blocked": str(b.blocked)} for b in req.blocks],
        )
        try:
            game.declare_blockers(pairs)
        except ValueError as exc:  # the engine's own refusal text
            return audit_outcome(game_id, "declare_blockers", engine_api.refused(exc), **keys)
        outcome = engine_api.Outcome(True, detail={"blocks": len(pairs)})
        return audit_outcome(game_id, "declare_blockers", outcome, **keys)


# -- running the game forward ------------------------------------------------


@app.post("/api/games/{game_id}/state-based-actions")
def state_based_actions(game_id: str) -> dict[str, Any]:
    with store.locked(game_id) as game:
        result = engine_api.run_state_based_actions(game)
        audit.record(
            game_id,
            "state_based_actions",
            destroyed=result["destroyed"],
            changed=result["changed"],
        )
        return {**state_view(game, game_id), **result}


@app.post("/api/games/{game_id}/resolve-top")
def resolve_top(
    game_id: str, req: ActorRequest | None = None, uid: str | None = None
) -> dict[str, Any]:
    with store.locked(game_id) as game:
        result = engine_api.resolve_top(game)
        seq = audit.record(game_id, "resolve_top", player=req.player if req else None, **result)
        viewer = visibility.resolve_viewer(game_id, uid)
        return {
            **visibility.redact_view(state_view(game, game_id), viewer),
            **result,
            "log_seq": seq,
        }


@app.post("/api/games/{game_id}/advance")
def advance(game_id: str, req: AdvanceRequest, uid: str | None = None) -> dict[str, Any]:
    """Move the turn marker forward. Refused (`allowed: False`) rather than
    performed when the stack is non-empty in a step that grants priority
    (CR 500.2, 117.4) - the engine's own refusal, not a transport error, so
    the caller's next move is `pass-priority`, not a retry - or when the
    caller is not the active player or their controller (CR 117.3a, 723.5)."""
    player = req.player
    with store.locked(game_id) as game:
        assert game.turn is not None
        before = game.turn.step.value
        try:
            step = game.advance(player)
        except ValueError as exc:  # the engine's own refusal text
            seq = audit.record(game_id, "advance", player=player, allowed=False, reason=str(exc))
            return {"allowed": False, "reason": str(exc), "log_seq": seq}
        seq = audit.record(
            game_id,
            "advance",
            player=player,
            allowed=True,
            **{"from": before, "to": step.value},
        )
        viewer = visibility.resolve_viewer(game_id, uid)
        return {
            **visibility.redact_view(state_view(game, game_id), viewer),
            "advanced_to": step.value,
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
    """Answer this game's pending decision (CR 608.2d), such as the cleanup
    discard's choice of which card to put in the graveyard.

    Deliberately does not call `store.locked(game_id)` or touch the `Game`
    object at all - only the mailbox. It cannot: the request that published
    this decision is *itself* holding that lock, parked inside `Game.ask`
    waiting for this call to answer the mailbox. Taking the lock here would
    queue this request behind the one it exists to unblock, which is a
    deadlock, not a wait. See `decisions.py`'s module docstring.

    `uid` must open the deciding player's own seat - not the opponent's, not
    a bystander's - matching CR 608.2d's "the player announces": nobody else
    may make this choice for them.
    """
    player = visibility.resolve_viewer(game_id, uid)
    mailbox = store.mailbox(game_id)
    if mailbox is None:
        raise KeyError(f"no game {game_id}")
    if player != req.player or not isinstance(player, str):
        return {"accepted": False, "reason": "that uid does not open the deciding player's seat"}
    pending = mailbox.snapshot()
    if pending is None or pending.player != req.player:
        return {"accepted": False, "reason": "no pending decision for you"}
    try:
        kind = DecisionKind(pending.kind)
    except ValueError:
        kind = None
    if kind is DecisionKind.CHOOSE_CARDS:
        try:
            answer = decisions.card_ids_answer(req.card_ids)
        except ValueError as exc:
            return {"accepted": False, "reason": f"bad card id: {exc}"}
    elif kind is DecisionKind.CHOOSE_TARGETS:
        try:
            answer = decisions.targets_answer(req.targets)
        except ValueError as exc:
            return {"accepted": False, "reason": f"bad target: {exc}"}
    elif kind in (DecisionKind.MAY, DecisionKind.PAY_LIFE):
        try:
            answer = decisions.bool_answer(req.accept)
        except ValueError as exc:
            return {"accepted": False, "reason": str(exc)}
    elif kind in (DecisionKind.SCRY, DecisionKind.SURVEIL):
        try:
            answer = decisions.card_split_answer(req.moved, req.top_order)
        except ValueError as exc:
            return {"accepted": False, "reason": f"bad card id: {exc}"}
    else:
        # `kind` is either unparseable (a future `DecisionKind` this API has
        # not been told about) or one of `MULLIGAN`/`DECLARE_ATTACKERS`/
        # `DECLARE_BLOCKERS`, which never reach this mailbox at all - see
        # `decisions.ANSWERABLE_KINDS`. Either way, refuse by name rather than
        # guessing a shape or silently letting the engine's own default stand
        # in: CR 608.2d's "the player announces" fails loudly here, not quietly
        # in a decision_timeout log 60 seconds from now.
        return {
            "accepted": False,
            "reason": f"this endpoint does not know how to answer a {pending.kind!r} decision",
        }
    accepted = mailbox.answer_if(req.player, answer)
    return {"accepted": accepted}


# -- the frontend ------------------------------------------------------------

_frontend = frontend_dir()
if _frontend is not None:
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
else:

    @app.get("/", include_in_schema=False)
    def landing() -> HTMLResponse:
        return HTMLResponse(_LANDING)
