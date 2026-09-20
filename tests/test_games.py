"""The game-session endpoints: create, read, list, delete."""

from conftest import _ADMIN_KEYS


def test_create_game(client, game):
    state = client.get(f"/api/games/{game}").json()
    assert [player["username"] for player in state["players"]] == ["alice", "bob"]
    assert state["step"] == "precombat_main"
    assert state["active_player"] == "alice"
    assert state["priority_holder"] == "alice"
    assert state["turn_number"] == 1
    # S1 review fix 3: `turns_taken` counts every turn including extra ones
    # (CR 500.7), unlike `turn_number`, which counts rounds of the table.
    assert state["turns_taken"] == 1


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
    body = response.json()
    game_id = body["game_id"]
    try:
        state = body
        assert state["active_player"] == "kaiba"
        assert state["step"] == "postcombat_main"
        lives = {player["username"]: player["life"] for player in state["players"]}
        assert lives == {"yugi": 25, "kaiba": 8000}
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": body["admin_key"]})


def test_unknown_game_is_404(client):
    assert client.get("/api/games/nope").status_code == 404


def test_list_and_delete(client, game):
    games = client.get("/api/games").json()["games"]
    assert game in [entry["game_id"] for entry in games]
    assert client.delete(f"/api/games/{game}", params={"key": _ADMIN_KEYS[game]}).status_code == 200
    assert client.get(f"/api/games/{game}").status_code == 404


def test_delete_without_credentials_is_refused(client, game):
    """`DELETE /api/games/{id}` requires a seat uid of this game or its admin
    key - the same guard `end_lobby_game` uses - so reading the game's id off
    a listing is never enough on its own to delete it."""
    response = client.delete(f"/api/games/{game}")
    assert response.status_code == 404
    assert client.get(f"/api/games/{game}").status_code == 200
