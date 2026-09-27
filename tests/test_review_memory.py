"""OM1: `GET /api/games/{id}/review` must not hold every recorded engine
state of a long game decoded at once.

The prod OOM (2026-09-27): a 250-decision game's review decoded all ~250
recorded states into full `Game` objects before turning each into a small
board view - ~1.7 MB of heap per state, ~440 MB per call, and two
overlapping calls passed the pod's 1Gi limit. Streaming the decode (one
`Game` alive at a time) keeps one call's peak to the board views plus one
state.

Measured with `tracemalloc`, which counts this process's own Python
allocations only - so the number is the same whatever the other `-n 6`
workers are doing, unlike RSS.
"""

from __future__ import annotations

import gc
import tracemalloc
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from postgres_store_testkit import postgres_store

from magic_cards_model_api import audit
from magic_cards_model_api.store import store

DECKS = Path(__file__).resolve().parents[1] / "decks"

#: The number of recorded states the long game carries - the size of the
#: prod game that OOMed, and where the old path measured +440 MB per call.
STATES = 250

#: One `/review` call's allowed peak of added Python allocation.
PEAK_BUDGET = 64 * 2**20


def long_recorded_game(client: TestClient, states: int = STATES) -> dict[str, Any]:
    """A finished two-deck game (real 60-card lists, so each recorded state
    is prod-sized) with at least `states` recorded engine states, every one
    of them a real `audit.record` row carrying its own exact state - the
    same writer every live action goes through. Only the filler rows are
    synthetic: allowed priority passes by alternating seats, recorded
    without advancing the game, which is the cheapest way to reach a
    prod-length ledger deterministically."""
    created = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {
                    "username": "prowess",
                    "decklist_text": (DECKS / "izzet_prowess.txt").read_text(),
                },
                {"username": "boros", "decklist_text": (DECKS / "boros_aggro.txt").read_text()},
            ]
        },
    )
    assert created.status_code == 200, created.text
    info = created.json()
    game_id = info["game_id"]
    uids = info["seat_uids"]
    for player, uid in uids.items():
        kept = client.post(
            f"/api/games/{game_id}/pregame/mulligan",
            params={"uid": uid},
            json={"player": player, "take": False, "reasoning": "keep"},
        )
        assert kept.status_code == 200, kept.text
    for player, uid in uids.items():
        finished = client.post(
            f"/api/games/{game_id}/pregame/opening-hand/finish",
            params={"uid": uid},
            json={"player": player, "reasoning": "go"},
        )
        assert finished.status_code == 200, finished.text
    seats = list(uids)
    index = 0
    while len(store.record_state_seqs(game_id)) < states:
        audit.record(
            game_id,
            "pass_priority",
            reasoning="filler",
            player=seats[index % 2],
            allowed=True,
        )
        index += 1
    conceded = client.post(
        f"/api/games/{game_id}/actions/concede",
        params={"uid": uids["boros"]},
        json={"player": "boros", "reasoning": "done"},
    )
    assert conceded.status_code == 200, conceded.text
    return info


def _peak_of(call: Any) -> tuple[Any, int]:
    """`call()`'s result and the peak Python allocation it added."""
    gc.collect()
    tracemalloc.start()
    try:
        base, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        result = call()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak - base


@pytest.fixture(params=["in_memory", "postgres"])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Both stores: `postgres` streams its rows through a server-side cursor
    (`yield_per`) rather than `InMemoryRepository`'s held blobs, and is
    skipped under plain `mise run test`, required under `mise run test:pg`
    (`postgres_store_testkit`)."""
    scope = postgres_store(monkeypatch) if request.param == "postgres" else nullcontext()
    with scope:
        yield request.param


def test_one_review_of_a_250_state_game_stays_under_64mb(backend: str, client: TestClient) -> None:
    info = long_recorded_game(client)
    game_id = info["game_id"]
    key = info["admin_key"]
    assert len(store.record_state_seqs(game_id)) >= STATES

    response, peak = _peak_of(
        lambda: client.get(f"/api/games/{game_id}/review", params={"key": key})
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["exact_frames"]) >= STATES
    boards = [row for rows in body["decisions"].values() for row in rows if row["board"]]
    assert len(boards) >= STATES - 10, "every filler decision still carries its board"
    assert peak < PEAK_BUDGET, f"one /review added {peak / 2**20:.0f} MB at peak"
