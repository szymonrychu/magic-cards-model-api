"""`GameStore.create_if_under` - the atomic MAX_LIVE_GAMES cap check.

`start_game_from_decklists` used to call `store.live_ui_games()` and
`store.create()` as two separately-locked steps: two concurrent requests
could both read the live count just under the cap and both go on to create,
letting the live table grow past the limit `MAX_LIVE_GAMES` exists to
enforce. `create_if_under` closes that window by taking the store's lock
once around both the count and the create - and, by default, the
`_ui_games` registration too, so a game can never exist in `_games` without
yet counting against the cap in `_ui_games`.
"""

from __future__ import annotations

import threading

from test_decks_api import LEGAL_DECK, LEGAL_DECK_B

from magic_cards_model_api.decks import load_deck_into_game
from magic_cards_model_api.store import GameStore


def _real_game():
    """A real engine `Game`, from a real (if minimal) legal decklist pair -
    exactly what `load_deck_into_game` builds for `POST /api/lobby/games`, no
    hand-built model."""
    match = load_deck_into_game([("you", LEGAL_DECK), ("bot", LEGAL_DECK_B)])
    return match.games[-1]


def test_create_if_under_refuses_once_the_cap_is_reached() -> None:
    store = GameStore()
    first = store.create_if_under(_real_game(), cap=1)
    assert first is not None

    second = store.create_if_under(_real_game(), cap=1)
    assert second is None


def test_create_if_under_ignores_a_finished_game_against_the_cap() -> None:
    store = GameStore()
    first = store.create_if_under(_real_game(), cap=1)
    game = store.get(first)
    game.winner = "you"  # ended - no longer counts against the cap

    second = store.create_if_under(_real_game(), cap=1)
    assert second is not None


def test_create_if_under_is_atomic_under_concurrent_callers() -> None:
    """Two threads racing the same cap=1 check: exactly one must win, not
    both (the bug this method exists to close) and not neither."""
    store = GameStore()
    results: list[str | None] = []
    lock = threading.Lock()
    ready = threading.Barrier(2)

    def attempt() -> None:
        ready.wait(timeout=5)
        game_id = store.create_if_under(_real_game(), cap=1)
        with lock:
            results.append(game_id)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == 1
    assert len(store.live_ui_games()) == 1
