"""OM1: one build of a finished game's review at a time, and the last few
finished reviews kept.

`GET /api/games/{id}/review` is a sync route, so FastAPI runs it in its
threadpool: a seat's post-game review session, its opponent's, and the
orchestrator polling every few minutes all overlap on one game. Before this,
each rebuilt the whole review from the ledger and every recorded state - and
two overlapping builds of a long game were what took prod past its 1Gi
limit (2026-09-27).

`ReviewCache.building(game_id)` serializes builds per game (a caller for a
different game never waits), and `get`/`put` keep the finished payload per
`(game_id, viewer)` under a caller-supplied `version` - a fingerprint of
everything the payload was built from, so a grown or changed ledger is a
miss, never a stale answer. Bounded by entry count, least recently used
evicted first.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: How many finished reviews stay cached. One game's review is read by its
#: two seats and the operator, so this covers two games' worth; a 250-state
#: review is ~0.6 MB of JSON, a few MB as Python objects.
DEFAULT_MAX_ENTRIES = 6


class ReviewCache:
    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        self._guard = threading.Lock()
        self._entries: OrderedDict[tuple[str, str | None], tuple[str, dict[str, Any]]] = (
            OrderedDict()
        )
        #: game_id -> [its build lock, how many callers hold or wait on it];
        #: dropped when the last one leaves, so this never outgrows the
        #: builds actually in flight.
        self._locks: dict[str, list[Any]] = {}

    @contextmanager
    def building(self, game_id: str) -> Iterator[None]:
        """Held around one game's lookup-then-build, so a second caller for
        the same game waits for the first and then finds its payload."""
        with self._guard:
            slot = self._locks.setdefault(game_id, [threading.Lock(), 0])
            slot[1] += 1
        try:
            with slot[0]:
                yield
        finally:
            with self._guard:
                slot[1] -= 1
                if slot[1] == 0:
                    del self._locks[game_id]

    def get(self, game_id: str, viewer: str | None, version: str) -> dict[str, Any] | None:
        with self._guard:
            key = (game_id, viewer)
            cached = self._entries.get(key)
            if cached is None or cached[0] != version:
                return None
            self._entries.move_to_end(key)
            return cached[1]

    def put(self, game_id: str, viewer: str | None, version: str, payload: dict[str, Any]) -> None:
        with self._guard:
            key = (game_id, viewer)
            self._entries[key] = (version, payload)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._guard:
            self._entries.clear()

    def __len__(self) -> int:
        with self._guard:
            return len(self._entries)

    def in_flight(self) -> int:
        """How many games have a build lock right now - 0 when idle."""
        with self._guard:
            return len(self._locks)
