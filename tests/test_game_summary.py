"""`GET /api/games/summary?ids=...` - S2 (2b's T8, map's hot N+1 fix): the
scalars a lobby list needs (id, is_over, turn number, active player, phase,
seat count), for every id a caller asks about in one call, rather than the
full `serialize.state_view` `list_lobby_games` used to build per game.
`tests/test_lobby_games.py` covers the lobby's own use of this through
`api_client.game_summaries`; this file is the route's own contract.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _boot(client: TestClient) -> dict:
    response = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_summary_reports_a_fresh_games_scalars(client: TestClient) -> None:
    info = _boot(client)
    game_id = info["game_id"]
    try:
        response = client.get("/api/games/summary", params={"ids": game_id})
        assert response.status_code == 200, response.text
        summaries = response.json()["summaries"]
        assert len(summaries) == 1
        row = summaries[0]
        assert row["game_id"] == game_id
        assert row["is_over"] is False
        assert row["turn_number"] == 1
        assert row["active_player"] == "alice"
        assert row["seat_count"] == 2
        # A fresh `POST /api/games` board starts in `precombat_main` (no
        # pregame), which falls under the `beginning`... no: `PHASE_OF`'s own
        # mapping is what this checks indirectly - the field is present and
        # a string, the exact value is `test_summary_matches_the_real_step`'s
        # job below.
        assert row["phase"] is not None
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_summary_matches_the_real_state_views_turn_and_phase(client: TestClient) -> None:
    info = _boot(client)
    game_id = info["game_id"]
    try:
        client.put(f"/api/games/{game_id}/step", json={"step": "declare_attackers"})
        state = client.get(f"/api/games/{game_id}", params={"uid": info["admin_key"]}).json()

        summaries = client.get("/api/games/summary", params={"ids": game_id}).json()["summaries"]
        row = summaries[0]
        assert row["turn_number"] == state["turn_number"]
        assert row["phase"] == state["phase"]
        assert row["active_player"] == state["active_player"]
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_summary_reports_is_over_for_a_conceded_game(client: TestClient) -> None:
    info = _boot(client)
    game_id = info["game_id"]
    try:
        conceded = client.post(f"/api/games/{game_id}/actions/concede", json={"player": "alice"})
        assert conceded.status_code == 200, conceded.text

        summaries = client.get("/api/games/summary", params={"ids": game_id}).json()["summaries"]
        assert summaries[0]["is_over"] is True
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_summary_answers_several_ids_in_one_call_and_skips_unknown_ones(
    client: TestClient,
) -> None:
    first = _boot(client)
    second = _boot(client)
    try:
        response = client.get(
            "/api/games/summary",
            params={"ids": f"{first['game_id']},no-such-game,{second['game_id']}"},
        )
        assert response.status_code == 200, response.text
        ids = {row["game_id"] for row in response.json()["summaries"]}
        assert ids == {first["game_id"], second["game_id"]}
    finally:
        client.delete(f"/api/games/{first['game_id']}", params={"key": first["admin_key"]})
        client.delete(f"/api/games/{second['game_id']}", params={"key": second["admin_key"]})


def test_summary_with_no_known_ids_answers_an_empty_list(client: TestClient) -> None:
    response = client.get("/api/games/summary", params={"ids": "nope,also-nope"})
    assert response.status_code == 200, response.text
    assert response.json()["summaries"] == []
