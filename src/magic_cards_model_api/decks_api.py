"""HTTP endpoints the browser UI uses to boot and manage a lobby-booted game.

`serve.py` only boots games from `--deck`/`--match` command-line arguments,
read once at process start. A UI needs to boot a game *at request time*, from
a bundled `decks/*.txt` file or from text a human pasted or uploaded - so
`POST /api/ui/games` calls the exact same engine path
(`decks.load_deck_into_game`, CR 103's whole start-of-game sequence) that
`serve.py`'s `_boot_from_spec` already uses, just from an HTTP body instead
of argv.

Deck resolution and legality themselves - `_resolve_all`,
`validate_decklist_text`, `GET /api/decks*`, `POST /api/decks/validate` -
moved to `api_decks.py` (2b's T5, map Finding 1): they need the engine and
`scryfall.py`, dependencies the lobby side of the eventual cut must not
carry. This module reaches that endpoint through `api_client.ApiClient`
rather than importing its function - the seam the cut later swaps for
`httpx` - so a decklist is still validated before either game is registered,
just not by importing the api component's internals to do it.

This module never touches game rules itself - every actual rule (construction
limits, CR 103's start-of-game sequence) is enforced by the engine through
`decks.py`. It only adapts an HTTP body to those existing calls.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import ai_pregame, ai_seat, audit, lobby, serialize
from .agent_settings import AgentSettings
from .ai_seat_api import _api_base
from .api_client import ApiClient, InProcessApiClient
from .decks import load_deck_into_game
from .store import new_seat_uids, store

router = APIRouter(prefix="/api/ui", tags=["ui"])

#: The lobby's one dependency on the api component, for now an in-process
#: call (`api_client.py`'s own docstring). Module-level so a test can swap it
#: the way `store.py`'s singleton is swapped, without every call site taking
#: a client argument it has no other use for.
api_client: ApiClient = InProcessApiClient()


def max_live_games() -> int:
    """How many lobby-booted games may be live at once, `MAX_LIVE_GAMES` in
    the environment (default 4). Enforced by `start_game_from_decklists`
    against `store.live_ui_games()`; a finished game (`Game.is_over`, CR
    104.1) does not count against it, only End game frees up a live one."""
    return int(os.environ.get("MAX_LIVE_GAMES", "4") or 4)


class NewUiPlayer(BaseModel):
    username: str = Field(min_length=1)
    decklist_text: str = Field(min_length=1)
    ai: bool = False
    """Whether this seat is meant to be driven by the AI-seat runner - a UI
    hint only (`ai_seat.py` is a separate, opt-in call); carried here so the
    response can tell the UI which uid to auto-start without a second lookup."""
    agent: AgentSettings | None = None
    """How the AI seat runs (model, effort, thinking). Only with `ai: true`;
    absent, the server's defaults apply (`AgentSettings.default`)."""


class NewUiGame(BaseModel):
    players: list[NewUiPlayer] = Field(min_length=2, max_length=2)
    seed: int | None = None


class NewUiGameResponse(BaseModel):
    game_id: str
    seat_uids: dict[str, str]
    admin_key: str | None = None
    ai_players: list[str]
    agents: dict[str, AgentSettings]
    """Every AI seat's chosen (or defaulted) model, effort and thinking
    budget - `AgentSettings.default` when the request left it unset."""


@router.post("/games", response_model=NewUiGameResponse, response_model_exclude_unset=True)
def start_game_from_decklists(req: NewUiGame, request: Request) -> dict[str, Any]:
    """Boot a new game from two decklists, right now, from an HTTP body -
    the request-time counterpart to `serve.py`'s `--deck`/`--match`, sharing
    its exact boot path (`decks.load_deck_into_game`): CR 103's registration,
    legality check, sideboard set-aside, seeded shuffles, and opening hands.

    Refuses (400, via the `ValueError`/`DeckIllegal` handlers already
    registered in `app.py`) with the same per-card detail
    `api_client.validate_decklist` reports, rather than booting a broken
    game - both decks are validated before either is registered.

    Games the lobby boots live side by side - starting one never ends
    another; `DELETE /api/ui/games/{id}` (End game) is the only reset. Refuses
    (409) once `MAX_LIVE_GAMES` lobby games are already live, so the table
    does not grow without bound; ending one from the lobby frees a slot.
    """
    problems: dict[str, Any] = {}
    for player in req.players:
        result = api_client.validate_decklist(player.decklist_text)
        if not result["valid"]:
            problems[player.username] = result["errors"]
    if problems:
        raise ValueError(f"decklist(s) failed validation: {problems}")

    for player in req.players:
        if player.agent is not None and not player.ai:
            raise ValueError(f"{player.username}: agent settings are only for an ai seat")
    agents = {
        player.username: player.agent or AgentSettings.default(os.environ)
        for player in req.players
        if player.ai
    }

    # A fast, non-atomic pre-check: cheap, and saves `load_deck_into_game`'s
    # real work (card resolution among it) in the common case of a lobby
    # already at the cap. It is NOT what actually enforces the cap - two
    # concurrent requests could both pass it - so `store.create_if_under`
    # below is the one call that has the final, race-safe word.
    live = store.live_ui_games()
    if len(live) >= max_live_games():
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(live)} games are already live, the cap is {max_live_games()}; "
                "end one from the lobby first"
            ),
        )

    match = load_deck_into_game(
        [(player.username, player.decklist_text) for player in req.players],
        seed=req.seed,
    )
    game = match.games[-1]
    game_id = store.create_if_under(game, max_live_games())
    if game_id is None:
        # The cap was crossed by a concurrent request between the pre-check
        # above and here - `create_if_under` checked the live count and
        # created atomically under one lock, so this is the race actually
        # closing rather than a duplicate of the same message.
        live = store.live_ui_games()
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(live)} games are already live, the cap is {max_live_games()}; "
                "end one from the lobby first"
            ),
        )
    uids = new_seat_uids(game)
    store.register_seats(game_id, uids)
    store.register_decklists(
        game_id, {player.username: player.decklist_text for player in req.players}
    )
    store.register_agent_settings(game_id, agents)
    # Nothing runs CR 103.5 or CR 103.6 here. `Match.start_game` has already
    # left the game resting in `Step.MULLIGAN` (turn 0), and the seats drive it
    # from there through `POST /api/games/{id}/pregame/...` - see `opening.py`
    # for why this used to be a background thread and why it must not be one.
    audit.record(
        game_id,
        "create_game",
        players=[player.username for player in req.players],
        source="ui",
        seed=str(match.seed),
    )
    seat_uids = {name: uid for uid, name in uids.items()}
    ai_players = [player.username for player in req.players if player.ai]
    # The bot seats' pregame backstop. CR 103.5 needs every seat's declaration
    # and CR 103.6 hands the window to one seat at a time, so a bot seat that
    # goes quiet - and the headless `claude` subprocess demonstrably does,
    # exiting once it decides the pregame is "nothing to act on" - strands the
    # human at a panel no button of theirs can move. See `ai_pregame.py`.
    ai_pregame.start(game_id, ai_players)
    # Start each AI seat right away rather than making a human click a second
    # "start" button for a seat they already flagged `ai: true` - the
    # separate `POST .../ai-seat/start` route stays, both for a seat that
    # failed to auto-start and for a lobby rejoining an already-live game.
    # Guarded by `claude_available()` in subprocess mode (the same check
    # `AiSeatRegistry.start` makes internally) so a test posting here with
    # `ai: true` never spawns a real `claude` process just because one
    # happens to be on this machine's PATH.
    mode = ai_seat.ai_seat_mode()
    api_base = _api_base(request)
    for player in req.players:
        if not player.ai:
            continue
        if mode == "subprocess" and not ai_seat.claude_available():
            continue
        ai_seat.registry.start(
            uid=seat_uids[player.username],
            game_id=game_id,
            player=player.username,
            api_base=api_base,
            agent=agents[player.username],
        )
    return {
        "game_id": game_id,
        "seat_uids": seat_uids,
        "admin_key": store.admin_key(game_id),
        "ai_players": [player.username for player in req.players if player.ai],
        "agents": {name: settings.model_dump() for name, settings in agents.items()},
    }


class LobbyGameSummary(BaseModel):
    game_id: str
    players: list[str]
    turn_number: int | None = None
    step: str
    winner: str | None = None
    drawn: bool
    agents: dict[str, AgentSettings]
    seats: dict[str, str]
    created_at: str | None = None


class LobbyGamesResponse(BaseModel):
    games: list[LobbyGameSummary]


@router.get("/games", response_model=LobbyGamesResponse, response_model_exclude_unset=True)
def list_lobby_games(uids: str | None = None) -> dict[str, Any]:
    """Every lobby-booted game (`store.ui_games()` - not filtered to
    `live_ui_games()`, so a finished-but-not-yet-ended game still shows up for
    End game to clear), oldest first, for the lobby's game list.

    `uids` is a comma-separated list of seat uids the caller holds, across
    every game it has ever seen - each game's `seats` in the response is
    filtered down to only the uids from that list which are actually seats of
    that game, so a browser never learns a uid that is not already its own.
    """
    mine = {u for u in (uids or "").split(",") if u}
    games = []
    for game_id in store.ui_games():
        game = store.get(game_id)
        if game is None:
            continue
        seats = {name: uid for uid, name in store.seat_uids(game_id).items() if uid in mine}
        view = serialize.state_view(game, game_id)
        games.append(
            {
                "game_id": game_id,
                "players": [p.username for p in game.players],
                "turn_number": view["turn_number"],
                "step": view["step"],
                "winner": view["winner"],
                "drawn": view["drawn"],
                "agents": {
                    name: settings.model_dump()
                    for name, settings in store.agent_settings(game_id).items()
                },
                "seats": seats,
                "created_at": store.created_at(game_id),
            }
        )
    games.sort(key=lambda entry: entry["created_at"] or "")
    return {"games": games}


class EndGameResponse(BaseModel):
    game_id: str
    players: list[str]
    seats_invalidated: int
    watcher_stopped: bool
    ai_seats_released: list[dict[str, Any]]
    """Per-uid release detail from `ai_seat.registry.release` - shape varies
    by mechanism (`subprocess`/`wrapper`/`kubernetes`), and `lobby.py` (out
    of this task's file list) is what would need to change to make it a
    named component; left as the same honest `dict[str, Any]` T1 allowed for
    a genuinely dynamically-keyed field rather than forcing a wrong shape."""
    deleted: bool


@router.delete(
    "/games/{game_id}", response_model=EndGameResponse, response_model_exclude_unset=True
)
def end_lobby_game(game_id: str, uid: str | None = None, key: str | None = None) -> dict[str, Any]:
    """End game: the only reset there is now. `uid` must be a seat uid of
    this game, or `key` this game's admin key - anything else is refused (404)
    rather than letting an arbitrary caller end somebody else's game."""
    if store.get(game_id) is None:
        raise KeyError(f"no game {game_id}")
    allowed = (uid is not None and store.player_for_uid(game_id, uid) is not None) or (
        key is not None and store.is_admin(game_id, key)
    )
    if not allowed:
        raise KeyError("that uid is no seat in this game")
    return lobby.reset_game(game_id, reason="ended from the lobby")
