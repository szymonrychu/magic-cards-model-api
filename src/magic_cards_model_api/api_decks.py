"""Deck resolution and legality: the api side of the boundary.

2b's T5 (map Finding 1) moves this out of `decks_api.py`, where it used to
sit beside the lobby's UI-facing routes. Card-by-card resolution needs
`magic_cards_model.models.{Card, Deck, DeckIllegal}` (the engine) and
`scryfall.fetch_card`/`modern_status` (the 281-line Scryfall client and its
cache) - dependencies the lobby side of the eventual six-way cut must not
carry, because the Scryfall cache PVC is `ReadWriteOnce` and two Deployments
cannot share it. So these routes are mounted under plain `/api`, the same
namespace `app.py`'s engine-facing routes use, rather than under the lobby's
`/api/ui` - that split in the URL space is deliberate and already matches
where each side of the eventual cut will actually live.

`decks_api.start_game_from_decklists` (still lobby-owned) no longer imports
`validate_decklist_text` from here directly - it goes through
`api_client.ApiClient` instead, which today's `InProcessApiClient` still
implements by calling straight into this module, but which the cut can later
swap for an `httpx` call with no change to the caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from magic_cards_model.models import Card, Deck, DeckIllegal
from pydantic import BaseModel, Field

from . import scryfall
from .decks import deck_support_warnings_from_cards, parse_decklist
from .schemas import DeckSupportWarning, RequestModel

router = APIRouter(prefix="/api", tags=["decks"])


#: `decks/*.txt`, the same bundled decklists `serve.py --deck`/`--match` read
#: from, sitting at the repo root next to `pyproject.toml` (three parents up
#: from this file: `src/magic_cards_model_api/api_decks.py`).
DECKS_DIR = Path(__file__).resolve().parents[2] / "decks"


class BundledDecksResponse(BaseModel):
    decks: list[str]


class BundledDeckText(BaseModel):
    name: str
    text: str


class ValidateRequest(RequestModel):
    text: str = Field(min_length=1)


class DeckProblem(BaseModel):
    card: str | None = None
    message: str


class DeckValidation(BaseModel):
    valid: bool
    errors: list[DeckProblem]
    warnings: list[DeckProblem]
    mainboard_size: int | None = None
    """Only present once the text parsed far enough to count cards - absent
    (not zero) on a syntax refusal, the same "key genuinely missing"
    distinction `response_model_exclude_unset=True` preserves on every route
    in this repo since T1."""
    sideboard_size: int | None = None
    support_warnings: list[DeckSupportWarning] = []
    """Flow 6 (W2): every distinct card this decklist names that the engine
    does not fully support (`decks.deck_support_warnings_from_cards`),
    worst status first - present once every name resolved to a real
    printing (mirrors `mainboard_size`'s own "far enough" rule), empty for a
    fully supported deck. Legality (`valid`/`errors`) is computed
    separately and never gated on this: a card the engine does not fully
    support can still be a perfectly legal, playable card."""


@router.get("/decks", response_model=BundledDecksResponse, response_model_exclude_unset=True)
def list_bundled_decks() -> dict[str, Any]:
    """The decklist files already in the repo, for a picker in the UI."""
    if not DECKS_DIR.is_dir():
        return {"decks": []}
    return {"decks": sorted(p.name for p in DECKS_DIR.glob("*.txt"))}


@router.get("/decks/{name}", response_model=BundledDeckText, response_model_exclude_unset=True)
def bundled_deck_text(name: str) -> dict[str, Any]:
    """One bundled decklist's raw text, so the UI can preview or edit it
    before starting a game. `name` is resolved strictly inside `DECKS_DIR` -
    no `..` escape - since this is a bare filename off a URL path."""
    candidate = (DECKS_DIR / name).resolve()
    if candidate.parent != DECKS_DIR.resolve() or not candidate.is_file():
        raise KeyError(f"no bundled deck {name!r}")
    return {"name": name, "text": candidate.read_text()}


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

    Shared by `POST /decks/validate` and `api_client.InProcessApiClient`
    (the latter is what `decks_api.start_game_from_decklists` calls, rather
    than importing this function - the former refuses to boot a game from an
    invalid list rather than let `load_deck_into_game` raise deep inside a
    request with no per-card detail).
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

    # Flow 6 (W2): every card resolved to a real printing, so it is
    # classifiable regardless of whether the deck goes on to be legal -
    # support and legality are two separate questions about the same cards.
    support_warnings = deck_support_warnings_from_cards(resolved)

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
            "support_warnings": support_warnings,
        }
    return {
        "valid": True,
        "errors": [],
        "warnings": warnings,
        "mainboard_size": mainboard_size,
        "sideboard_size": sideboard_size,
        "support_warnings": support_warnings,
    }


@router.post("/decks/validate", response_model=DeckValidation, response_model_exclude_unset=True)
def validate_decklist(req: ValidateRequest) -> dict[str, Any]:
    return validate_decklist_text(req.text)
