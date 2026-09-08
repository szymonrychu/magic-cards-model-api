"""2b T11: `Seats` can resolve a seat uid without knowing its game up front.

The stdio transport is spawned once per playtest game (`--game <id>`, or
`None` to auto-discover "the one live game" - `Seats.game_id()`), so a single
process only ever needs to resolve uids inside one game. The new http
transport is a long-running service any game's agent can call, so it may be
serving several games at once - "the one live game" stops being a coherent
idea. `resolve_by_uid=True` switches `Seats.seat_for` to `GET
/api/seats/{uid}` (T8's seat-lookup endpoint), which answers a uid's game and
player with no game id given at all, so each call finds its own game
independent of every other live one.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


def make_game(client: TestClient) -> tuple[str, dict[str, str]]:
    created = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    game_id = created["game_id"]
    uids = {name: uid for uid, name in store.seat_uids(game_id).items()}
    return game_id, uids


def test_resolve_by_uid_finds_each_seats_own_game_among_several_live_ones(
    monkeypatch, client: TestClient
) -> None:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=None, resolve_by_uid=True)

    game1, uids1 = make_game(client)
    game2, uids2 = make_game(client)

    seat1 = seats.seat_for(uids1["alice"])
    seat2 = seats.seat_for(uids2["bob"])

    assert seat1 is not None
    assert seat1.player == "alice"
    assert seat1.game_id() == game1

    assert seat2 is not None
    assert seat2.player == "bob"
    assert seat2.game_id() == game2

    # neither seat crossed into the other game
    assert seats.seat_for(uids1["alice"]).game_id() != game2  # type: ignore[union-attr]


def test_resolve_by_uid_returns_none_for_an_unknown_uid(monkeypatch, client: TestClient) -> None:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=None, resolve_by_uid=True)
    make_game(client)

    assert seats.seat_for("not-a-real-uid") is None


def test_resolve_by_uid_is_off_by_default(monkeypatch, client: TestClient) -> None:
    """The existing single/pinned-game behaviour must not change for a
    caller that never opts in - `mcp.py --game <id>` and the stdio default
    both rely on this."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=None)
    game_id, uids = make_game(client)

    seat = seats.seat_for(uids["alice"])
    assert seat is not None
    assert seat.game_id() == game_id
