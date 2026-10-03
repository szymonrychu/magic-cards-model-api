"""S1 defect 3: an stdio MCP server started without `--game` resolves each
call's game from its seat uid (`GET /api/seats/{uid}`), exactly as the http
transport does - never "the first of `/api/games`". With two live games, a
seat of the one that is not listed first must still be served."""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient
from test_mcp_baton import call

from magic_cards_model_api import mcp
from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def test_stdio_without_game_resolves_every_call_by_seat_uid(client, game, monkeypatch):
    created = client.post(
        "/api/games", json={"players": [{"username": "carol"}, {"username": "dave"}]}
    )
    assert created.status_code == 200, created.text
    other = created.json()
    try:
        listed = [entry["game_id"] for entry in client.get("/api/games").json()["games"]]
        assert {game, other["game_id"]} <= set(listed)
        # The seat lives in whichever game `/api/games` does NOT list first.
        target = listed[1] if listed[0] in (game, other["game_id"]) else listed[0]
        assert target != listed[0]
        uid, player = next(iter(store.seat_uids(target).items()))

        monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
        args = mcp._build_parser().parse_args(
            ["--transport", "stdio", "--api", "http://testserver"]
        )
        server = mcp.build_server(mcp.seats_from_args(args))

        text = call(server, "battlefield_state", {"player_uid": uid})
        assert text.startswith("{"), text
        board = json.loads(text)
        assert board["you"] == player
        assert board["game_id"] == target
    finally:
        client.delete(f"/api/games/{other['game_id']}", params={"key": other["admin_key"]})


def test_stdio_with_game_stays_pinned(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    args = mcp._build_parser().parse_args(
        ["--transport", "stdio", "--api", "http://testserver", "--game", "g1"]
    )
    seats = mcp.seats_from_args(args)
    assert seats.game_id() == "g1"
    assert seats._resolve_by_uid is False
