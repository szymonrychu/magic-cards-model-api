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
from magic_cards_model.machines.decisions import DecisionKind
from magic_cards_model.machines.priority import PriorityOutcome
from magic_cards_model.models import Card, CardState, Color, Game, Player, Zone

from . import (
    affordances,
    ai_seat_api,
    audit,
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
    ManaPoolUpdate,
    MoveCard,
    MulliganRequest,
    NewGame,
    OpeningHandActionRequest,
    PassPriorityRequest,
    PatchCard,
    PatchPlayer,
    PlayLandRequest,
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


def _card_fingerprint(state: CardState, game: Game) -> tuple[Any, ...]:
    """One card's own observable state, identity included (CR 400.7).

    `zone_change_stamp` is the load-bearing field: "An object that moves from
    one zone to another becomes a new object with no memory of, or relation
    to, its previous existence" (CR 400.7), and the engine mutates one
    `CardState` in place across a zone change rather than replacing it, so
    `id` alone cannot tell a card that never moved apart from one that left
    and came back. `move_to` reassigns `zone_change_stamp` on every call,
    which is exactly the signal a blink needs: Ephemerate exiles a creature
    and returns it to the battlefield in the same resolution, so the `id`,
    name, controller, tapped state, and counters can all come back identical
    while the object itself is a new one CR 400.7's own terms - and only the
    stamp says so.

    `tapped`, `is_summoning_sick`, `damage`, the sorted counters, and
    `attachment` ride along so a mutation that does *not* change zones (an
    aura attaching, a counter added, a tap, a block-time attachment loss)
    still shows up without needing its own special case.
    """
    return (
        str(state.zone_change_stamp),
        state.tapped,
        state.is_summoning_sick(game),
        state.damage,
        tuple(sorted(state.counters.root.items())),
        state.attachment,
    )


def _resolution_state(game: Game) -> dict[str, Any]:
    """A fingerprint of everything a resolution is meant to change, minus the
    stack itself.

    Resolving always pops the stack, so the stack cannot say whether the
    resolution *did* anything. Everything else can - board, hands,
    graveyards, exile, life, and pools - as long as it is compared by each
    card's own identity and observable state (`_card_fingerprint`), not by
    zone-and-name alone: a bare list of names, the shape this used to build
    from `serialize.snapshot`, cannot tell a card that stayed apart from a
    card that left and came back to the exact same zone under the exact same
    name, which is precisely what a blink (Ephemerate) does. Keyed by card
    id so `_without_own_arrival` can discount one specific object rather than
    guessing which of several same-named cards changed.

    This is necessary but not sufficient on its own for a *spell*: every
    spell also leaves the stack somewhere as part of resolving at all
    (CR 608.2m), and that arrival mutates this fingerprint regardless of
    whether the spell's own effect ran. `_without_own_arrival` is what
    discounts that one mandatory move for spells; abilities need no such
    correction, since CR 608.2n removes a resolved ability from the stack
    without putting it anywhere (see `machines.triggers.AbilityOnStack`).

    **What this still cannot see**, honestly: the library, the command zone,
    and the sideboard are not fingerprinted at all (an effect that only
    shuffles the library, or only changes what is in a face-down command-zone
    card, reads as "nothing changed"); a card's own printed text or
    characteristics changed by a continuous effect that touches none of the
    fields above (e.g. a name change with no P/T, tap, or counter change)
    is invisible; and two effects that exactly cancel out within the same
    resolution (a counter added then removed by the same spell) leave no
    trace, the same blind spot a snapshot diff always has.
    """

    def zone_fingerprint(cards: Any) -> dict[str, tuple[Any, ...]]:
        return {str(c.id): _card_fingerprint(c, game) for c in cards if isinstance(c, CardState)}

    return {
        "life": {player.username: player.life for player in game.players},
        "pools": {player.username: dict(player.mana_pool.floating) for player in game.players},
        "battlefield": zone_fingerprint(game.shared_zones.get(Zone.BATTLEFIELD, [])),
        "hands": {player.username: zone_fingerprint(player.hand) for player in game.players},
        "graveyards": {
            player.username: zone_fingerprint(player.graveyard) for player in game.players
        },
        "exile": zone_fingerprint(game.shared_zones.get(Zone.EXILE, [])),
    }


def _without_own_arrival(game: Game, spell_id: UUID, after: dict[str, Any]) -> dict[str, Any]:
    """`after`, with a just-resolved spell's own mandatory landing undone.

    A permanent spell resolving onto the battlefield is not "nothing
    happened" - a new object started existing under a player's control, and
    the `changed` marker should say so. So this only touches spells that did
    *not* land on the battlefield: an instant or sorcery goes to its owner's
    graveyard (CR 608.2m), and a few cards route themselves to exile instead
    (flashback, adventure, foretell). That move is the fixed, mandatory
    conclusion of resolving at all - it happens whether or not the spell's
    own effect executed - so it is subtracted back out before comparing
    against `before`. Anything beyond it (a life total, a hand, a second
    card's zone, the battlefield) is left untouched and is the real signal.

    Removed by the spell's own id, now that `_resolution_state` keys each
    zone by id rather than by a bare list of names - exact, unlike the old
    by-name removal, which could not tell two identically named cards
    landing in the same zone apart.

    A spell that instead ends up in a zone `_resolution_state` does not track
    at all (library, command, sideboard) needs no discount here: `before` and
    `after` already agree on those untracked zones, so no false "changed"
    can arise from that arrival in the first place.
    """
    landed = game.find_by_id(spell_id)
    if landed is None or landed.zone is Zone.BATTLEFIELD:
        return after
    landed_key = str(spell_id)

    if landed.zone is Zone.EXILE:
        return {**after, "exile": {k: v for k, v in after["exile"].items() if k != landed_key}}
    if landed.zone is Zone.GRAVEYARD:
        owner = landed.controller_or_owner
        graveyards = dict(after["graveyards"])
        if owner in graveyards:
            graveyards[owner] = {k: v for k, v in graveyards[owner].items() if k != landed_key}
        return {**after, "graveyards": graveyards}
    if landed.zone is Zone.HAND:
        owner = landed.controller_or_owner
        hands = dict(after["hands"])
        if owner in hands:
            hands[owner] = {k: v for k, v in hands[owner].items() if k != landed_key}
        return {**after, "hands": hands}
    return after


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
    if isinstance(viewer, str):
        # This is the read `Seat.fetch_state` polls (`seatkit.block_until_wake`),
        # so a real seat reaching here has, by definition, just been shown
        # whatever `pending_decision` this response carries - including one
        # addressed to it. Marking that delivered here is what lets
        # `decisions.DecisionMailbox.wait_for_answer` start its real timeout
        # clock from "the seat could have seen this" rather than from
        # `publish()`, when the seat may not have polled yet. A no-op unless
        # this game currently has a decision pending for `viewer` specifically.
        mailbox = store.mailbox(game_id)
        if mailbox is not None:
            # And, separately, that somebody is behind this seat at all -
            # true of this read whether or not a decision happens to be
            # pending for `viewer` right now, which is exactly the evidence
            # `DecisionMailbox.delivery_bound` needs to tell a live player
            # from a game nobody is watching. See `decisions.py`'s
            # `DEFAULT_PRESENCE_SECONDS`.
            mailbox.mark_watching(viewer)
            mailbox.mark_delivered(viewer)
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
    with store.locked(game_id) as game:
        game.player(req.player)
        outcome = engine_api.cast_card(game, req)
        return audit_outcome(
            game_id,
            "cast",
            outcome,
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
    with store.locked(game_id) as game:
        game.player(req.player)
        outcome = engine_api.suspend_card(game, req)
        return audit_outcome(
            game_id,
            "suspend",
            outcome,
            dry_run=req.dry_run,
            player=req.player,
            card=req.card or str(req.card_id),
            mana_sources=[str(source) for source in req.mana_sources or []],
        )


@app.post("/api/games/{game_id}/actions/plot")
def action_plot(game_id: str, req: SpecialActionRequest) -> dict[str, Any]:
    """CR 116.2k/702.170b: exile a card with plot from hand, paying its plot
    cost. Cast it later with `actions/cast`, `keyword: "Plot"`, on a later turn."""
    with store.locked(game_id) as game:
        game.player(req.player)
        outcome = engine_api.plot_card(game, req)
        return audit_outcome(
            game_id,
            "plot",
            outcome,
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
    with store.locked(game_id) as game:
        game.player(req.player)
        outcome = engine_api.foretell_card(game, req)
        return audit_outcome(
            game_id,
            "foretell",
            outcome,
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
    with store.locked(game_id) as game:
        game.player(req.player)
        outcome = engine_api.cycle_card(game, req)
        return audit_outcome(
            game_id,
            "cycle",
            outcome,
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


@app.post("/api/games/{game_id}/actions/concede")
def action_concede(game_id: str, req: ConcedeRequest) -> dict[str, Any]:
    """Concede (CR 104.3a). A player may do this at any time, and leaves the
    game immediately - there is no priority check and nothing to refuse, which
    is why this takes no `Outcome` the way the other actions do."""
    with store.locked(game_id) as game:
        game.player(req.player)
        game.concede(req.player, by=req.player)
        seq = audit.record(game_id, "concede", player=req.player, allowed=True)
        return {"allowed": True, "winner": game.winner, "log_seq": seq}


@app.post("/api/games/{game_id}/actions/activate")
def action_activate(game_id: str, req: ActivateRequest) -> dict[str, Any]:
    with store.locked(game_id) as game:
        game.player(req.player)
        outcome = engine_api.activate_ability(game, req)
        return audit_outcome(
            game_id,
            "activate",
            outcome,
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
    with store.locked(game_id) as game:
        game.player(req.player)
        outcome = engine_api.crew_vehicle(game, req)
        return audit_outcome(
            game_id,
            "crew",
            outcome,
            dry_run=req.dry_run,
            player=req.player,
            card_id=str(req.card_id),
            crewing=[str(card_id) for card_id in req.crewing],
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
        outcome = engine_api.play_land(game, req)
        return audit_outcome(
            game_id,
            "play_land",
            outcome,
            dry_run=req.dry_run,
            player=req.player,
            card=req.card or str(req.card_id),
        )


@app.post("/api/games/{game_id}/actions/pass-priority")
def action_pass_priority(game_id: str, req: PassPriorityRequest) -> dict[str, Any]:
    """Pass priority - "I am done for now". `outcome` tells the caller what
    the pass caused: `resolve_top` (the stack's top resolved, priority goes
    round again) or `end_step` (everyone passed on an empty stack, so the
    step ended and the next has already begun - CR 117.4, 405.5).

    If that next step is one in which no player receives priority, this does
    not stop there: `engine_api.settle_priorityless_steps` runs the game on to
    the first step somebody can act in (CR 500.3), and `settled_through` lists
    the steps crossed. A player who has passed has said "I am done for now",
    and nothing in CR 500.3's untap or cleanup steps is waiting on them - so
    handing the pass back with `priority_holder: null` would be handing back a
    state no client, human or otherwise, is allowed to move."""
    with store.locked(game_id) as game:
        resolving_obj = game.stack[-1] if game.stack else None
        resolving = resolving_obj.name if resolving_obj is not None else None
        resolving_spell_id = resolving_obj.id if isinstance(resolving_obj, CardState) else None
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
            after = _resolution_state(game)
            if resolving_spell_id is not None:
                after = _without_own_arrival(game, resolving_spell_id, after)
            detail["changed"] = after != before
        settled = engine_api.settle_priorityless_steps(game)
        if settled:
            detail["settled_through"] = settled
        seq = audit.record(
            game_id, "pass_priority", player=req.player, allowed=True, reason=None, **detail
        )
        return {
            "allowed": True,
            "reason": None,
            "outcome": outcome.value,
            "settled_through": settled,
            "log_seq": seq,
        }


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
        suspend_upkeep: list[dict[str, Any]] = []
        if step is Step.UPKEEP:
            # CR 702.62a's second/third abilities: nothing in `Game.advance`
            # dispatches this trigger on its own (see `engine_api.
            # drive_suspend_upkeep`), so it is driven by hand here, once,
            # for whoever's upkeep was just entered.
            suspend_upkeep = engine_api.drive_suspend_upkeep(game, game.turn.active_player)
        seq = audit.record(
            game_id,
            "advance",
            player=player,
            allowed=True,
            suspend_upkeep=suspend_upkeep,
            **{"from": before, "to": step.value},
        )
        viewer = visibility.resolve_viewer(game_id, uid)
        return {
            **visibility.redact_view(state_view(game, game_id), viewer),
            "advanced_to": step.value,
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
    """Answer this game's pending decision - most kinds CR 608.2d, `choose_targets`
    CR 603.3d/601.2c and `order_triggers` CR 603.3b instead
    (`decisions.rule_for_kind`) - such as the cleanup discard's choice of
    which card to put in the graveyard.

    Deliberately does not call `store.locked(game_id)` or touch the `Game`
    object at all - only the mailbox. It cannot: the request that published
    this decision is *itself* holding that lock, parked inside `Game.ask`
    waiting for this call to answer the mailbox. Taking the lock here would
    queue this request behind the one it exists to unblock, which is a
    deadlock, not a wait. See `decisions.py`'s module docstring.

    `audit.record` is safe to call here even though the lock above is not:
    it only ever calls `store.get(game_id)` (a plain read of the store's own
    lock, not the per-game one) to pull a snapshot for the log entry - the
    same reason `decisions.GameDecisionProvider._log_timeout` already calls
    it from inside `Game.ask`. Every answer is recorded, accepted or refused,
    with this endpoint's own reason verbatim - this used to be the one
    mutating-shaped endpoint with no audit trail at all, which is why a
    player who answered a decision correctly could still look, from the log
    alone, like they had never answered it.

    The accepted-answer audit entry below is written *before*
    `mailbox.answer_if` is called, not after, and that ordering is load
    bearing, not incidental: `answer_if` is what wakes the resolving request
    parked in `Game.ask`, which then resumes mutating the `Game` object under
    its own lock - a lock this endpoint never holds. Reading a snapshot of
    that same `Game` *after* calling `answer_if` would race a thread that
    might already be back to mutating it; reading it in the instant before,
    while that thread is still provably parked in `DecisionMailbox.wait_for_answer`'s
    condition wait, cannot. See `test_deadlock.py`'s
    `test_decide_never_races_the_resolving_thread_for_the_audit_snapshot`.

    `uid` must open the deciding player's own seat - not the opponent's, not
    a bystander's - matching CR 608.2d's "the player announces": nobody else
    may make this choice for them.
    """

    def refuse(reason: str, **detail: Any) -> dict[str, Any]:
        seq = audit.record(
            game_id, "decide", player=req.player, allowed=False, reason=reason, **detail
        )
        return {"accepted": False, "reason": reason, "log_seq": seq}

    player = visibility.resolve_viewer(game_id, uid)
    mailbox = store.mailbox(game_id)
    if mailbox is None:
        raise KeyError(f"no game {game_id}")
    if player != req.player or not isinstance(player, str):
        return refuse("that uid does not open the deciding player's seat")
    current = mailbox.current()
    if current is None or current[0].player != req.player:
        return refuse("no pending decision for you")
    pending, engine_decision = current
    try:
        kind = DecisionKind(pending.kind)
    except ValueError:
        kind = None
    detail: dict[str, Any] = {"kind": pending.kind, "prompt": pending.prompt}
    if kind is DecisionKind.CHOOSE_CARDS:
        try:
            answer = decisions.card_ids_answer(req.card_ids)
        except ValueError as exc:
            return refuse(f"bad card id: {exc}", **detail)
        detail["card_ids"] = req.card_ids
    elif kind is DecisionKind.CHOOSE_TARGETS:
        try:
            answer = decisions.targets_answer(req.targets)
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
            answer = decisions.order_answer(req.order)
        except ValueError as exc:
            return refuse(f"bad ability id: {exc}", **detail)
        detail["order"] = req.order
    elif kind in (
        DecisionKind.MAY,
        DecisionKind.PAY_LIFE,
        DecisionKind.PAY_ENERGY,
        DecisionKind.PAY_MANA,
        # CR 103.6's "may take any such actions": the same yes/no shape,
        # asked through `Game.ask` - see `decisions.ANSWERABLE_KINDS`.
        DecisionKind.OPENING_HAND_ACTION,
        # CR 103.5's declaration, "will you take a mulligan", which
        # `machines/decisions.py`'s `ask_mulligan` puts to the provider. Only
        # `Game.settle_pregame` asks it that way - a seat declaring for itself
        # uses `POST .../pregame/mulligan`, which blocks on nothing - but that
        # path is real and reaches a live seat, so this branch is not dead.
        # Yes/no like the rest of this tuple; the *bottoming* that follows a
        # kept hand is a separate CHOOSE_CARDS question and needs nothing here.
        DecisionKind.MULLIGAN,
    ):
        try:
            answer = decisions.bool_answer(req.accept)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["accept"] = req.accept
    elif kind is DecisionKind.PAY_ENERGY_AMOUNT:
        try:
            answer = decisions.energy_amount_answer(req.energy_amount)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["energy_amount"] = req.energy_amount
    elif kind is DecisionKind.PAY_LIFE_AMOUNT:
        try:
            answer = decisions.life_amount_answer(req.life_amount)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["life_amount"] = req.life_amount
    elif kind is DecisionKind.CHOOSE_A_QUALITY:
        try:
            answer = decisions.quality_answer(req.choice)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["choice"] = req.choice
    elif kind is DecisionKind.CHOOSE_A_MODE:
        try:
            answer = decisions.mode_answer(req.choice)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["choice"] = req.choice
    elif kind is DecisionKind.PUT_ON_TOP_OR_BOTTOM:
        try:
            answer = decisions.top_or_bottom_answer(req.top)
        except ValueError as exc:
            return refuse(str(exc), **detail)
        detail["top"] = req.top
    elif kind in (DecisionKind.SCRY, DecisionKind.SURVEIL):
        try:
            answer = decisions.card_split_answer(req.moved, req.top_order)
        except ValueError as exc:
            return refuse(f"bad card id: {exc}", **detail)
        detail["moved"] = req.moved
        detail["top_order"] = req.top_order
    else:
        # `kind` is either unparseable (a future `DecisionKind` this API has
        # not been told about) or one of `MULLIGAN`/`DECLARE_ATTACKERS`/
        # `DECLARE_BLOCKERS`, none of which this mailbox ever carries - see
        # `decisions.ANSWERABLE_KINDS`/`decisions.SERVED_ELSEWHERE`. Either
        # way, refuse by name rather than guessing a shape or silently
        # letting the engine's own default stand in: "the player announces"
        # fails loudly here, not quietly in a decision_timeout log
        # `decisions.DEFAULT_TIMEOUT_SECONDS` from now. The reason names how
        # to actually answer it when one is known, rather than leaving a
        # stuck player to guess a fifth argument shape that will never work -
        # see the reported bug this replaced, where a live player tried four
        # shapes against `choose_a_quality` with no path to a fifth that
        # would have worked either, because none existed.
        hint = decisions.SERVED_ELSEWHERE.get(
            kind, "this is a new DecisionKind decide has not been taught yet"
        )
        return refuse(
            f"this endpoint does not know how to answer a {pending.kind!r} decision: {hint}",
            **detail,
        )
    # The last gate before this answer is trusted: everything `Game.ask`
    # itself would check (the right number of cards, a target actually
    # offered, an amount in range, ...) run here first, against the same
    # `Decision` the engine handed this API when it asked the question. An
    # answer that fails this is refused right here, synchronously, to the
    # seat that gave it - never posted to the mailbox, never logged
    # `allowed: true`, and the decision stays pending so the same seat can
    # answer again. See `decisions.validate_answer`'s docstring for why this
    # cannot simply be left to `Game.ask` on the other side of the mailbox.
    try:
        decisions.validate_answer(engine_decision, answer)
    except ValueError as exc:
        return refuse(str(exc), **detail)
    # Recorded before `answer_if`, not after - see the docstring above on why
    # that ordering is what keeps this read of the `Game` race-free.
    seq = audit.record(game_id, "decide", player=req.player, allowed=True, reason=None, **detail)
    accepted = mailbox.answer_if(req.player, answer)
    if not accepted:
        # A genuine race: the pending decision moved on (answered elsewhere,
        # or timed out and defaulted) between `snapshot()` above and this
        # call. The entry just written said "allowed: True" in good faith -
        # correct it with a second entry rather than leave a wrong one
        # standing as the only record of what happened.
        reason = "the pending decision changed before this answer arrived"
        seq = audit.record(
            game_id, "decide", player=req.player, allowed=False, reason=reason, **detail
        )
        return {"accepted": False, "reason": reason, "log_seq": seq}
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
def pregame_settle(game_id: str, uid: str | None = None) -> dict[str, Any]:
    """Run the whole pregame on the engine's own defaults: every seat keeps the
    hand it was dealt and declines every CR 103.6 action.

    The headless path, for a harness or a bot that wants a game to *play*
    rather than a game to start correctly - `Game.settle_pregame`, unchanged
    engine behaviour, immediate and deterministic because it consults nobody.
    Admin-key only: it makes both seats' CR 103.5 declarations at once, which
    is the operator's call over a game they created, never one seat's over the
    other's.
    """
    if visibility.resolve_viewer(game_id, uid) is not visibility.ADMIN:
        raise ValueError("settling the whole pregame for every seat needs this game's admin key")
    with store.locked(game_id) as game:
        result = opening.settle_pregame(game_id, game)
        return _settled(game, result)


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
