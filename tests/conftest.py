"""Shared fixtures. Card data comes only from the recorded Scryfall fixtures
in tests/fixtures/scryfall - the network is never touched."""

from __future__ import annotations

import os
from pathlib import Path

os.environ["MCM_CACHE_DIR"] = str(Path(__file__).parent / "fixtures" / "scryfall")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from magic_cards_model_api import lobby  # noqa: E402
from magic_cards_model_api.main import app  # noqa: E402
from magic_cards_model_api.store import store  # noqa: E402


def add_card(client: TestClient, game_id: str, name: str, player: str, zone: str, **kwargs):
    response = client.post(
        f"/api/games/{game_id}/cards",
        json={"card": name, "player": player, "zone": zone, **kwargs},
    )
    assert response.status_code == 200, response.text
    return response.json()


_ADMIN_KEYS: dict[str, str] = {}  # game_id -> admin key, stashed by the `game` fixture


def state(client: TestClient, game_id: str, uid: str | None = None) -> dict:
    """The board, filtered exactly as the API filters it for `uid`.

    With no `uid`, this reaches for the admin key the `game` fixture stashed
    at creation - the harness built this board, so it gets the harness's own
    full view, the same as any other operator holding that key. Tests that
    want the *filtered*, seat-scoped view (what a real player sees) pass a
    real seat uid explicitly.
    """
    if uid is None:
        uid = _ADMIN_KEYS.get(game_id)
    response = client.get(f"/api/games/{game_id}", params={"uid": uid} if uid else None)
    assert response.status_code == 200, response.text
    return response.json()


def find(cards: list[dict], name: str) -> dict:
    matches = [card for card in cards if card["name"] == name]
    assert matches, f"no {name} among {[card['name'] for card in cards]}"
    return matches[0]


def card_id(client: TestClient, game_id: str, name: str, player: str, zone: str) -> str:
    """Add `name` to `player`'s `zone` and return the id it was dealt.

    `hands` and `graveyards` in `state()` are per-player views; `battlefield`
    and `exile` are shared across both players, so only the former two are
    looked up by `player`.
    """
    add_card(client, game_id, name, player, zone)
    board = state(client, game_id)
    per_player = {"hand": board["hands"], "graveyard": board["graveyards"]}
    pool = per_player[zone][player] if zone in per_player else board[zone]
    return find(pool, name)["id"]


def life_of(state_dict: dict, username: str) -> int:
    return next(player for player in state_dict["players"] if player["username"] == username)[
        "life"
    ]


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client
    # `store` is a module-level singleton shared by the whole pytest process,
    # not reset between tests just because `TestClient`'s context manager
    # exited. Since `POST /api/ui/games` no longer resets a previous lobby
    # game on its own (`lobby.py` - End game is the only reset now), a test
    # that boots one and forgets to end or delete it would otherwise leak a
    # live slot into every test that runs after it: `MAX_LIVE_GAMES`
    # eventually refuses a perfectly innocent later test's boot, and
    # `Seats`-style "the one live game" discovery starts pointing at a game
    # nobody in the current test created (`test_mcp_resilience.py`). This is
    # the safety net a well-behaved test does not need (it already deletes
    # what it created), and the one a forgetful test cannot do without: every
    # game still in the store when a test ends is cleared before the next one
    # starts, lobby-booted games through `lobby.reset_game` (so their
    # watcher threads and AI seats are released too, not just deleted out from
    # under them), everything else through a plain `store.delete`.
    for game_id in store.ui_games():
        lobby.reset_game(game_id, reason="test cleanup")
    for game_id in store.ids():
        store.delete(game_id)


@pytest.fixture()
def game(client: TestClient) -> str:
    response = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    assert response.status_code == 200, response.text
    info = response.json()
    game_id = info["game_id"]
    _ADMIN_KEYS[game_id] = info["admin_key"]
    yield game_id
    _ADMIN_KEYS.pop(game_id, None)
    client.delete(f"/api/games/{game_id}")
