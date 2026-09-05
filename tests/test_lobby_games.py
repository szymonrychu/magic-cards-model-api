from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_game_agent_settings import create


def test_a_second_game_does_not_end_the_first(client: TestClient) -> None:
    first = create(client)
    second = create(client)
    try:
        assert client.get(f"/api/games/{first['game_id']}").status_code == 200
        assert client.get(f"/api/games/{second['game_id']}").status_code == 200
        assert "reset" not in second
    finally:
        client.delete(
            f"/api/ui/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )
        client.delete(
            f"/api/ui/games/{second['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )


def test_lobby_lists_live_games_with_only_your_uids(client: TestClient) -> None:
    first = create(client)
    second = create(client)
    try:
        my_uid = first["seat_uids"]["you"]
        listing = client.get("/api/ui/games", params={"uids": my_uid}).json()["games"]
        by_id = {g["game_id"]: g for g in listing}
        assert set(by_id) == {first["game_id"], second["game_id"]}
        assert by_id[first["game_id"]]["seats"] == {"you": my_uid}
        assert by_id[second["game_id"]]["seats"] == {}
        assert by_id[first["game_id"]]["agents"]["bot"]["model"] == "sonnet"
    finally:
        client.delete(
            f"/api/ui/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )
        client.delete(
            f"/api/ui/games/{second['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )


def test_end_game_deletes_it(client: TestClient) -> None:
    created = create(client)
    response = client.delete(
        f"/api/ui/games/{created['game_id']}", params={"uid": created["seat_uids"]["you"]}
    )
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert client.get(f"/api/games/{created['game_id']}").status_code == 404
    assert client.get("/api/ui/games").json()["games"] == []


def test_end_game_needs_a_seat_of_that_game(client: TestClient) -> None:
    first = create(client)
    second = create(client)
    try:
        response = client.delete(
            f"/api/ui/games/{first['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )
        assert response.status_code == 404
    finally:
        client.delete(
            f"/api/ui/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )
        client.delete(
            f"/api/ui/games/{second['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )


def test_live_game_cap(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_LIVE_GAMES", "2")
    first = create(client)
    second = create(client)
    try:
        bot = {"username": "bot", "decklist_text": create.__globals__["DECK_B"], "ai": True}
        response = client.post(
            "/api/ui/games",
            json={
                "players": [
                    {"username": "you", "decklist_text": create.__globals__["DECK_A"]},
                    bot,
                ]
            },
        )
        assert response.status_code == 409
        assert "2" in response.json()["detail"]
    finally:
        client.delete(
            f"/api/ui/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )
        client.delete(
            f"/api/ui/games/{second['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )
