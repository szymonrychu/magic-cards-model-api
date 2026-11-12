"""Seeded engine-only batch play (baseline vs baseline) and win-rate aggregation.

`play_match` runs inside the sim worker; `aggregate` and `resolve_deck` are pure helpers the
parent side reuses. Games never carry admin keys or seat uids out: results hold names only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from magic_cards_model_api import api_decks
from magic_cards_model_api.decks import parse_decklist
from magic_cards_model_api.rates import beta_interval
from magic_cards_model_api.sim.redact import support_block, support_unknown

logger = logging.getLogger(__name__)
INLINE_LABEL = "inline deck"
NOTE = (
    "Both seats are the zero-LLM baseline policy, which plays weakly; treat win rates as a "
    "rough signal about the lists under naive play, not about real matchups."
)
_MAX_NAMES_LISTED = 10


def resolve_deck(deck: str) -> tuple[str, str]:
    """`(label, decklist text)` for a bundled deck name or decklist text (has a newline)."""
    if "\n" in deck:
        return INLINE_LABEL, deck
    names = api_decks.list_bundled_decks()["decks"]
    name = deck.strip()
    if name not in names:
        shown = ", ".join(names[:_MAX_NAMES_LISTED])
        extra = len(names) - _MAX_NAMES_LISTED
        more = f" (and {extra} more)" if extra > 0 else ""
        raise ValueError(
            f"Unknown deck {deck!r}: pass a bundled deck name or the decklist text."
            f" Bundled decks include: {shown}{more}."
        )
    return name, (api_decks.DECKS_DIR / name).read_text()


def boot_baseline_game(
    client: Any, lists: list[tuple[str, str]], seed: int
) -> tuple[str, dict[str, str], Any]:
    """Boot one game from `(name, decklist text)` pairs in seat order and register its seats.

    The `serve._boot_from_spec` sequence, from text instead of files. Returns the game id,
    the seat uid by player name and the seat API seam over `client`. The caller deletes the
    game from the store when it is done.
    """
    from magic_cards_model_api.api_client import HttpSeatApiClient
    from magic_cards_model_api.decks import load_deck_into_game
    from magic_cards_model_api.store import new_seat_uids, store

    game = load_deck_into_game(lists, seed=seed).games[-1]
    game_id = store.create(game)
    try:
        uids = new_seat_uids(game)
        store.register_seats(game_id, uids)
    except BaseException:
        store.delete(game_id)
        raise
    return game_id, {name: uid for uid, name in uids.items()}, HttpSeatApiClient(http=client)


def _play_one(
    client: Any, deck_a: str, deck_b: str, seed: int, index: int, max_steps: int | None
) -> dict[str, Any]:
    from magic_cards_model_api.baseline_seat import BaselineSeat, play_out
    from magic_cards_model_api.store import store

    a_first = index % 2 == 0
    lists = [("a", deck_a), ("b", deck_b)]
    if not a_first:
        lists.reverse()
    started = time.monotonic()
    game_id, by_name, seam = boot_baseline_game(client, lists, seed + index)
    try:
        seats = [BaselineSeat(seam, game_id, name, by_name[name]) for name, _ in lists]
        outcome = play_out(seats) if max_steps is None else play_out(seats, max_steps=max_steps)
    finally:
        store.delete(game_id)
    return {
        "game": index,
        "seed": seed + index,
        "a_seat": 1 if a_first else 2,
        "winner": outcome.get("winner"),
        "drawn": bool(outcome.get("drawn")),
        "turn": outcome.get("turn"),
        "steps": outcome.get("steps"),
        "ended_by": outcome.get("ended_by"),
        "seconds": round(time.monotonic() - started, 3),
    }


def play_match(
    deck_a: str,
    deck_b: str,
    games: int,
    seed: int,
    emit: Callable[[dict[str, Any]], None],
    max_steps: int | None = None,
) -> None:
    """Play `games` baseline games, emitting each result; deck A leads on even game indexes.

    `max_steps` caps the steps of every game (a capped game ends undecided with
    `ended_by` set by the engine loop). Only tests use it, to keep worker tests fast; the
    sim_match tool never passes it, so real batches play to the baseline's own bound.
    """
    from fastapi.testclient import TestClient

    from magic_cards_model_api.main import app

    # One portal for the whole batch: a bare TestClient builds and tears down an event loop
    # per request, which costs ~0.1 s each and made a single game take minutes.
    with TestClient(app) as client:
        for i in range(games):
            emit(_play_one(client, deck_a, deck_b, seed, i, max_steps))


def deck_support(deck_a_text: str, deck_b_text: str) -> dict[str, Any]:
    """The support block (`support_status`, `support_warnings`, `support_note`) for both decks.

    This resolves cards through Scryfall, so callers compute it once per batch, never per poll.
    """
    names: set[str] = set()
    for text in (deck_a_text, deck_b_text):
        main, side = parse_decklist(text)
        names.update(name for _, name in main + side)
    return {"support_status": "ok", **support_block(names)}


def checked_support(deck_a_text: str, deck_b_text: str) -> dict[str, Any]:
    """`deck_support`, or the `unavailable` block when the lookup raises: advisory, never fatal."""
    try:
        return deck_support(deck_a_text, deck_b_text)
    except Exception as exc:  # noqa: BLE001
        # Type only: the message may carry card text or a request URL.
        logger.warning("sim support lookup failed (%s)", type(exc).__name__)
        return support_unknown("unavailable")


def aggregate(
    deck_a_label: str,
    deck_b_label: str,
    per_game: list[dict[str, Any]],
    requested: int,
    status: str,
    deck_a_text: str,
    deck_b_text: str,
    support: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Win counts and 95% intervals over the decided games, with the per-game rows.

    The support block (cards the engine cannot play) comes from `support` when given, else it
    is computed from the deck texts, which may look cards up over the network.
    """
    wins_a = sum(1 for g in per_game if g.get("winner") == "a")
    wins_b = sum(1 for g in per_game if g.get("winner") == "b")
    draws = sum(1 for g in per_game if g.get("drawn") and not g.get("winner"))
    unfinished = sum(1 for g in per_game if not g.get("winner") and not g.get("drawn"))
    decided = wins_a + wins_b
    if support is None:
        support = deck_support(deck_a_text, deck_b_text)
    return {
        "status": status,
        "requested_games": requested,
        "finished_games": len(per_game) - unfinished,
        "deck_a": {
            "label": deck_a_label,
            "wins": wins_a,
            "win_rate": beta_interval(wins_a, decided),
        },
        "deck_b": {
            "label": deck_b_label,
            "wins": wins_b,
            "win_rate": beta_interval(wins_b, decided),
        },
        "draws": draws,
        "unfinished": unfinished,
        "games": per_game,
        "note": NOTE,
        **support,
    }
