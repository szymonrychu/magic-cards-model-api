"""The seat back door (2b's T8, map's "seat back door"): `POST
/api/games/{id}/seats` mints a fresh credential for an already-registered
player, and `GET /api/seats/{uid}` resolves a uid to its game with no game id
known in advance. Together they replace the tests'/mcp's habit of reading
`store.seat_uids` straight out of this process, which no out-of-process
caller (a test harness, or a shared mcp server, M2) can do.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _boot(client: TestClient) -> dict:
    response = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_minted_seat_resolves_back_to_its_game_and_player(client: TestClient) -> None:
    info = _boot(client)
    game_id = info["game_id"]
    try:
        minted = client.post(f"/api/games/{game_id}/seats", json={"player": "alice"})
        assert minted.status_code == 200, minted.text
        body = minted.json()
        assert body["player"] == "alice"
        uid = body["uid"]
        assert uid  # a real, non-empty credential

        looked_up = client.get(f"/api/seats/{uid}")
        assert looked_up.status_code == 200, looked_up.text
        assert looked_up.json() == {"game_id": game_id, "player": "alice"}
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_minting_a_seat_does_not_invalidate_the_original_uids(client: TestClient) -> None:
    """`register_seats` replaces the whole uid map rather than merging into
    it - the read-modify-write this route does under `store.locked` is what
    keeps a mint from wiping out every uid the game was actually created
    with. A regression here would still let the mint itself succeed; only
    the *original* uid would silently stop working."""
    info = _boot(client)
    game_id = info["game_id"]
    original_uid = info["seat_uids"]["bob"]
    try:
        client.post(f"/api/games/{game_id}/seats", json={"player": "alice"})

        still_works = client.get(f"/api/seats/{original_uid}")
        assert still_works.status_code == 200, still_works.text
        assert still_works.json() == {"game_id": game_id, "player": "bob"}
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_minting_a_seat_for_an_unknown_player_is_refused(client: TestClient) -> None:
    info = _boot(client)
    game_id = info["game_id"]
    try:
        response = client.post(f"/api/games/{game_id}/seats", json={"player": "carol"})
        assert response.status_code == 404
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_minting_a_seat_for_an_unknown_game_is_refused(client: TestClient) -> None:
    response = client.post("/api/games/no-such-game/seats", json={"player": "alice"})
    assert response.status_code == 404


def test_seat_lookup_404s_for_an_unknown_uid(client: TestClient) -> None:
    response = client.get("/api/seats/not-a-real-uid")
    assert response.status_code == 404


def test_seat_lookup_finds_a_uid_minted_before_the_lookup_knew_the_game(
    client: TestClient,
) -> None:
    """The whole point: the caller passes only a uid, no game id - proven
    across two live games so the lookup cannot be quietly scoped to "the
    one game" by accident."""
    first = _boot(client)
    second = _boot(client)
    try:
        first_uid = first["seat_uids"]["alice"]
        second_uid = second["seat_uids"]["bob"]

        assert client.get(f"/api/seats/{first_uid}").json() == {
            "game_id": first["game_id"],
            "player": "alice",
        }
        assert client.get(f"/api/seats/{second_uid}").json() == {
            "game_id": second["game_id"],
            "player": "bob",
        }
    finally:
        client.delete(f"/api/games/{first['game_id']}", params={"key": first["admin_key"]})
        client.delete(f"/api/games/{second['game_id']}", params={"key": second["admin_key"]})
