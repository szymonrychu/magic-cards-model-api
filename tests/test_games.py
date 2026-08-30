"""The game-session endpoints: create, read, list, delete."""


def test_create_game(client, game):
    state = client.get(f"/api/games/{game}").json()
    assert [player["username"] for player in state["players"]] == ["alice", "bob"]
    assert state["step"] == "precombat_main"
    assert state["active_player"] == "alice"
    assert state["priority_holder"] == "alice"
    assert state["turn_number"] == 1


def test_create_game_with_custom_start(client):
    response = client.post(
        "/api/games",
        json={
            "players": [{"username": "yugi", "life": 25}, {"username": "kaiba", "life": 8000}],
            "active_player": "kaiba",
            "step": "postcombat_main",
        },
    )
    assert response.status_code == 200, response.text
    game_id = response.json()["game_id"]
    try:
        state = response.json()
        assert state["active_player"] == "kaiba"
        assert state["step"] == "postcombat_main"
        lives = {player["username"]: player["life"] for player in state["players"]}
        assert lives == {"yugi": 25, "kaiba": 8000}
    finally:
        client.delete(f"/api/games/{game_id}")


def test_unknown_game_is_404(client):
    assert client.get("/api/games/nope").status_code == 404


def test_list_and_delete(client, game):
    games = client.get("/api/games").json()["games"]
    assert game in [entry["game_id"] for entry in games]
    assert client.delete(f"/api/games/{game}").status_code == 200
    assert client.get(f"/api/games/{game}").status_code == 404
