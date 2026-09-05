"""HTTP endpoints the browser UI uses to load decklists and boot a game.

Two gaps this closes that `serve.py`/`decks.py` did not cover on their own:

- `serve.py` only boots games from `--deck`/`--match` command-line arguments,
  read once at process start. A UI needs to boot a game *at request time*,
  from a bundled `decks/*.txt` file or from text a human pasted or uploaded -
  so `POST /api/ui/games` calls the exact same engine path
  (`decks.load_deck_into_game`, CR 103's whole start-of-game sequence) that
  `serve.py`'s `_boot_from_spec` already uses, just from an HTTP body instead
  of argv.
- Nothing validates a decklist *before* committing to a game. `POST
  /api/ui/decks/validate` runs the same two checks `serve.py`'s boot would
  hit anyway - `parse_decklist` (syntax) and the engine's own
  `Deck.check_legality` (CR 100.2a's four-copy limit and sixty-card minimum,
  CR 100.4a's fifteen-card sideboard) - and reports every problem with the
  card and the reason, instead of the caller finding out by way of a half
  booted game. Per-card Modern legality (`scryfall.modern_status`, straight
  off the Scryfall printing - not the engine's job) is reported as a
  warning, not an error: this API only ever plays Modern-legal-or-not
  informationally, same as `PutCard` already does for board setup.

This module never touches game rules itself - every actual rule (construction
limits, CR 103's start-of-game sequence) is enforced by the engine through
`decks.py`. It only adapts an HTTP body to those existing calls.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from magic_cards_model.models import Card, Deck, DeckIllegal
from pydantic import BaseModel, Field

from . import ai_pregame, audit, lobby, scryfall
from .agent_settings import AgentSettings
from .decks import load_deck_into_game, parse_decklist
from .store import new_seat_uids, store

router = APIRouter(prefix="/api/ui", tags=["ui"])

#: `decks/*.txt`, the same bundled decklists `serve.py --deck`/`--match` read
#: from, sitting at the repo root next to `pyproject.toml` (three parents up
#: from this file: `src/magic_cards_model_api/decks_api.py`).
DECKS_DIR = Path(__file__).resolve().parents[2] / "decks"


@router.get("/decks")
def list_bundled_decks() -> dict[str, Any]:
    """The decklist files already in the repo, for a picker in the UI."""
    if not DECKS_DIR.is_dir():
        return {"decks": []}
    return {"decks": sorted(p.name for p in DECKS_DIR.glob("*.txt"))}


@router.get("/decks/{name}")
def bundled_deck_text(name: str) -> dict[str, Any]:
    """One bundled decklist's raw text, so the UI can preview or edit it
    before starting a game. `name` is resolved strictly inside `DECKS_DIR` -
    no `..` escape - since this is a bare filename off a URL path."""
    candidate = (DECKS_DIR / name).resolve()
    if candidate.parent != DECKS_DIR.resolve() or not candidate.is_file():
        raise KeyError(f"no bundled deck {name!r}")
    return {"name": name, "text": candidate.read_text()}


class ValidateRequest(BaseModel):
    text: str = Field(min_length=1)


def _resolve_all(names: list[str]) -> tuple[dict[str, Card], list[dict[str, Any]]]:
    """Every distinct card name resolved to a real Scryfall printing.

    Returns (resolved, errors) rather than raising on the first miss: a
    decklist with three typos should say all three, not just the first, the
    same "every problem, not just one" spirit `Deck.problems` already has.
    """
    resolved: dict[str, Card] = {}
    errors: list[dict[str, Any]] = []
    for name in names:
        if name in resolved:
            continue
        try:
            raw = scryfall.fetch_card(name)
            resolved[name] = Card.model_validate(raw)
        except Exception as exc:  # scryfall.ScryfallError, CardNotFound, ValidationError
            errors.append({"card": name, "message": str(exc)})
    return resolved, errors


def validate_decklist_text(text: str) -> dict[str, Any]:
    """The engine's own answer to "is this decklist legal", card-by-card.

    Shared by `POST /decks/validate` and `POST /games` (the latter refuses to
    boot a game from an invalid list rather than let `load_deck_into_game`
    raise deep inside a request with no per-card detail).
    """
    try:
        mainboard, sideboard = parse_decklist(text)
    except ValueError as exc:
        return {"valid": False, "errors": [{"card": None, "message": str(exc)}], "warnings": []}

    names = [name for _, name in [*mainboard, *sideboard]]
    resolved, errors = _resolve_all(names)
    mainboard_size = sum(count for count, _ in mainboard)
    sideboard_size = sum(count for count, _ in sideboard)

    if errors:
        return {
            "valid": False,
            "errors": errors,
            "warnings": [],
            "mainboard_size": mainboard_size,
            "sideboard_size": sideboard_size,
        }

    deck = Deck(
        name="validate",
        mainboard={name: count for count, name in mainboard},
        sideboard={name: count for count, name in sideboard},
    )
    warnings = [
        {"card": name, "message": f"{name} is {status} in Modern"}
        for name in resolved
        if (status := scryfall.modern_status(scryfall.fetch_card(name))) != "legal"
    ]
    try:
        deck.check_legality(lambda name: resolved[name])
    except DeckIllegal as exc:
        return {
            "valid": False,
            "errors": [{"card": None, "message": problem} for problem in exc.problems],
            "warnings": warnings,
            "mainboard_size": mainboard_size,
            "sideboard_size": sideboard_size,
        }
    return {
        "valid": True,
        "errors": [],
        "warnings": warnings,
        "mainboard_size": mainboard_size,
        "sideboard_size": sideboard_size,
    }


@router.post("/decks/validate")
def validate_decklist(req: ValidateRequest) -> dict[str, Any]:
    return validate_decklist_text(req.text)


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


@router.post("/games")
def start_game_from_decklists(req: NewUiGame) -> dict[str, Any]:
    """Boot a new game from two decklists, right now, from an HTTP body -
    the request-time counterpart to `serve.py`'s `--deck`/`--match`, sharing
    its exact boot path (`decks.load_deck_into_game`): CR 103's registration,
    legality check, sideboard set-aside, seeded shuffles, and opening hands.

    Refuses (400, via the `ValueError`/`DeckIllegal` handlers already
    registered in `app.py`) with the same per-card detail
    `validate_decklist_text` reports, rather than booting a broken game -
    both decks are validated before either is registered.

    Starting a game here also *ends* the last one the lobby started - its
    game, its seat uids, its pregame watcher and its AI seat - and says so in
    both games' audit logs and in the `reset` key of this response. See
    `lobby.py`; the continuous agent's notes on disk are not touched.
    """
    problems: dict[str, Any] = {}
    for player in req.players:
        result = validate_decklist_text(player.decklist_text)
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

    # A new game from the lobby is a clean slate: the previous lobby game, its
    # seat uids, its pregame watcher and its AI seat all stop existing here,
    # before anything of the new game is built. After validation on purpose -
    # a decklist typo must not cost a human the game they are still playing -
    # and before `load_deck_into_game`, so the driver is free by the time the
    # new game's seat is dispatched to it. See `lobby.py`; the agent's memory
    # volume is not touched.
    reset = lobby.reset_previous_games()

    match = load_deck_into_game(
        [(player.username, player.decklist_text) for player in req.players],
        seed=req.seed,
    )
    game = match.games[-1]
    game_id = store.create(game)
    store.register_ui_game(game_id)
    lobby.record_reset(game_id, reset)
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
    return {
        "game_id": game_id,
        "seat_uids": seat_uids,
        "admin_key": store.admin_key(game_id),
        "ai_players": [player.username for player in req.players if player.ai],
        # Every AI seat's chosen (or defaulted) model, effort and thinking
        # budget - `AgentSettings.default` when the request left it unset.
        "agents": {name: settings.model_dump() for name, settings in agents.items()},
        # What starting this game reset, so the browser (and anyone reading
        # the response by hand) can see that the previous game was ended
        # rather than left running somewhere.
        "reset": reset,
    }
