"""`ai_pregame.watch`'s own store-failure handling.

`store.locked` documents two exceptions a caller must expect: `KeyError` for
a game that no longer exists, and `GameUnloadable` (`repository.py`'s own
module docstring) for a game whose stored snapshot this build cannot decode
(an engine schema bump past what the row was written at). `watch`'s own poll
loop only ever caught `KeyError` - a `GameUnloadable` row would crash the
watcher thread uncaught instead of ending the watch the same clean way a
deleted game already does.

Isolated with a monkeypatched `store.locked` rather than a real Postgres row:
`InMemoryRepository.locked` never raises `GameUnloadable` at all (an
in-memory game is never a stale, undecodable snapshot), so this exception
path is reachable only through a real `PostgresRepository`, which this
worktree must not stand up (docker/test:pg are out of bounds here) - the
point under test is `watch`'s own exception handling, not the decoder that
would raise it in production.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from magic_cards_model_api import ai_pregame
from magic_cards_model_api.repository import GameUnloadable
from magic_cards_model_api.store import store


def test_watch_ends_cleanly_when_the_game_becomes_unloadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def raise_unloadable(game_id: str) -> Iterator[Any]:
        raise GameUnloadable(game_id, "schema mismatch")
        yield  # pragma: no cover - never reached, satisfies the generator shape

    monkeypatch.setattr(store, "locked", raise_unloadable)

    # No exception escaping this call is the whole test: the watcher must
    # end the watch the same way a KeyError (a deleted game) already does,
    # not crash the thread `start()` runs it on.
    ai_pregame.watch(
        "some-unloadable-game",
        {"bob"},
        grace=0.0,
        max_wait=0.0,
        poll=0.01,
        max_lifetime=1.0,
    )
