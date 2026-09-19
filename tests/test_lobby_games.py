from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_game_agent_settings import create

from magic_cards_model_api import decks_api


def test_a_second_game_does_not_end_the_first(client: TestClient) -> None:
    first = create(client)
    second = create(client)
    try:
        assert client.get(f"/api/games/{first['game_id']}").status_code == 200
        assert client.get(f"/api/games/{second['game_id']}").status_code == 200
        assert "reset" not in second
    finally:
        client.delete(
            f"/api/lobby/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )
        client.delete(
            f"/api/lobby/games/{second['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )


def test_lobby_lists_live_games_with_only_your_uids(client: TestClient) -> None:
    first = create(client)
    second = create(client)
    try:
        my_uid = first["seat_uids"]["you"]
        listing = client.get("/api/lobby/games", params={"uids": my_uid}).json()["games"]
        by_id = {g["game_id"]: g for g in listing}
        assert set(by_id) == {first["game_id"], second["game_id"]}
        assert by_id[first["game_id"]]["seats"] == {"you": my_uid}
        assert by_id[second["game_id"]]["seats"] == {}
        assert by_id[first["game_id"]]["agents"]["bot"]["model"] == "claude-sonnet-5"
    finally:
        client.delete(
            f"/api/lobby/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )
        client.delete(
            f"/api/lobby/games/{second['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )


def test_end_game_deletes_it(client: TestClient) -> None:
    created = create(client)
    response = client.delete(
        f"/api/lobby/games/{created['game_id']}", params={"uid": created["seat_uids"]["you"]}
    )
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert client.get(f"/api/games/{created['game_id']}").status_code == 404
    assert client.get("/api/lobby/games").json()["games"] == []


def test_end_game_needs_a_seat_of_that_game(client: TestClient) -> None:
    first = create(client)
    second = create(client)
    try:
        response = client.delete(
            f"/api/lobby/games/{first['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )
        assert response.status_code == 404
    finally:
        client.delete(
            f"/api/lobby/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )
        client.delete(
            f"/api/lobby/games/{second['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )


def test_created_at_is_stamped_and_the_listing_is_newest_first(client: TestClient) -> None:
    """The game a player most likely wants is almost always the one they just
    started, not the oldest one still open - so the listing sorts newest
    first (`decks_api.list_lobby_games`'s own `created_at` sort, reversed)."""
    first = create(client)
    second = create(client)
    try:
        listing = client.get("/api/lobby/games").json()["games"]
        by_id = {g["game_id"]: g for g in listing}
        assert by_id[first["game_id"]]["created_at"] is not None
        assert by_id[second["game_id"]]["created_at"] is not None
        assert by_id[first["game_id"]]["created_at"] <= by_id[second["game_id"]]["created_at"]
        # The listing itself is ordered by it, newest first.
        ids_in_order = [g["game_id"] for g in listing]
        assert ids_in_order.index(second["game_id"]) < ids_in_order.index(first["game_id"])
    finally:
        client.delete(
            f"/api/lobby/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )
        client.delete(
            f"/api/lobby/games/{second['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )


def test_a_conceded_game_does_not_count_against_the_cap(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR 104.3a: a player may concede at any time. A conceded game has a
    winner (`Game.is_over`, CR 104.1) and so no longer counts as live - it
    is finished, only not yet cleared from the lobby - so it must not itself
    block a new game under a cap that would otherwise refuse one."""
    monkeypatch.setenv("MAX_LIVE_GAMES", "1")
    first = create(client)
    try:
        conceded = client.post(
            f"/api/games/{first['game_id']}/actions/concede", json={"player": "you"}
        )
        assert conceded.status_code == 200, conceded.text
        assert conceded.json()["winner"] == "bot"

        # The cap is 1, but the conceded game is not live any more, so a
        # second boot succeeds instead of refusing with 409.
        second = create(client)
        try:
            listing = client.get("/api/lobby/games").json()["games"]
            by_id = {g["game_id"]: g for g in listing}
            assert set(by_id) == {first["game_id"], second["game_id"]}
            # Still listed - finished, not ended - and reported as over, so
            # End game can clear it from the lobby.
            assert by_id[first["game_id"]]["is_over"] is True
        finally:
            client.delete(
                f"/api/lobby/games/{second['game_id']}", params={"uid": second["seat_uids"]["you"]}
            )
    finally:
        client.delete(
            f"/api/lobby/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )


def test_the_lobby_list_calls_the_bulk_summary_once_not_once_per_game(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 (2b's T8, map's hot N+1): `list_lobby_games` used to build a full
    `serialize.state_view` per game just to read `turn_number`/`step`/
    `winner`/`drawn` back out of it - N calls for N games, on the browser's
    1.2s poll. `api_client.game_summaries` is the one bulk call that replaced
    it; this spies on the seam T5 built for exactly this kind of swap and
    proves the lobby calls it exactly once, with every id, rather than once
    per game."""
    first = create(client)
    second = create(client)
    try:
        calls: list[list[str]] = []
        real = decks_api.api_client.game_summaries

        def spy(game_ids: list[str]) -> dict[str, dict]:
            calls.append(list(game_ids))
            return real(game_ids)

        monkeypatch.setattr(decks_api.api_client, "game_summaries", spy)

        listing = client.get("/api/lobby/games").json()["games"]
        assert {g["game_id"] for g in listing} >= {first["game_id"], second["game_id"]}
        assert len(calls) == 1, calls
        assert set(calls[0]) >= {first["game_id"], second["game_id"]}
    finally:
        client.delete(
            f"/api/lobby/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )
        client.delete(
            f"/api/lobby/games/{second['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )


def test_the_lobby_list_reports_turn_and_phase_from_the_bulk_summary(client: TestClient) -> None:
    created = create(client)
    try:
        listing = client.get("/api/lobby/games").json()["games"]
        row = next(g for g in listing if g["game_id"] == created["game_id"])
        # A lobby-booted game rests in CR 103.5's mulligan step (turn 0) with
        # no phase yet (CR 500.1 makes a phase part of a turn, and no turn has
        # begun) - the same values `GET /api/games/{id}` itself reports right
        # after boot (`test_decks_api.py`'s own
        # `test_start_game_boots_a_real_game`), now sourced from the bulk
        # summary instead.
        assert row["turn_number"] == 0
        assert row["phase"] is None
        assert row["is_over"] is False
    finally:
        client.delete(
            f"/api/lobby/games/{created['game_id']}", params={"uid": created["seat_uids"]["you"]}
        )


def test_live_game_cap(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_LIVE_GAMES", "2")
    first = create(client)
    second = create(client)
    try:
        bot = {"username": "bot", "decklist_text": create.__globals__["DECK_B"], "ai": True}
        response = client.post(
            "/api/lobby/games",
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
            f"/api/lobby/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )
        client.delete(
            f"/api/lobby/games/{second['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )


def test_an_ended_game_leaves_the_lobby_but_not_the_database(client: TestClient) -> None:
    """End game is a soft delete now, and the lobby must not notice.

    The row stays so the post-game review has a board to read its ledger
    against ("Games must be persisting in the database even after are
    stopped/finished/conceded"), but every live surface has to keep
    answering exactly what it answered when the row was destroyed - the
    lobby list above all, since a game nobody can join reappearing there is
    the most visible way this could go wrong.
    """
    created = create(client)
    game_id = created["game_id"]
    other = create(client)
    try:
        ended = client.delete(
            f"/api/lobby/games/{game_id}", params={"uid": created["seat_uids"]["you"]}
        )
        assert ended.status_code == 200
        assert ended.json()["deleted"] is True

        listed = {g["game_id"] for g in client.get("/api/lobby/games").json()["games"]}
        assert game_id not in listed
        assert other["game_id"] in listed, "and the game beside it is untouched"

        assert client.get(f"/api/games/{game_id}").status_code == 404
        assert client.get(f"/api/games/{game_id}/log").status_code == 404
        assert game_id not in {g["game_id"] for g in client.get("/api/games").json()["games"]}

        # ...and the row is still there, which is the whole change.
        review = client.get(f"/api/games/{game_id}/review", params={"key": created["admin_key"]})
        assert review.status_code == 200, review.text
        assert review.json()["ended_at"] is not None
        assert sorted(review.json()["players"]) == ["bot", "you"]
    finally:
        client.delete(
            f"/api/lobby/games/{other['game_id']}", params={"uid": other["seat_uids"]["you"]}
        )


def test_ending_a_game_gives_its_live_slot_back(client: TestClient, monkeypatch) -> None:
    """`MAX_LIVE_GAMES` counts `live_ui_games()`. An archived row that kept
    occupying a slot would make End game stop working after the cap, which
    is the other way a soft delete could quietly break the lobby."""
    monkeypatch.setattr(decks_api, "max_live_games", lambda: 1)
    first = create(client)
    try:
        refused = client.post(
            "/api/lobby/games",
            json={
                "players": [
                    {"username": "you", "decklist_text": create.__globals__["DECK_A"]},
                    {"username": "bot", "decklist_text": create.__globals__["DECK_B"]},
                ]
            },
        )
        assert refused.status_code == 409, refused.text

        client.delete(
            f"/api/lobby/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )
        second = create(client)
        client.delete(
            f"/api/lobby/games/{second['game_id']}", params={"uid": second["seat_uids"]["you"]}
        )
    finally:
        client.delete(
            f"/api/lobby/games/{first['game_id']}", params={"uid": first["seat_uids"]["you"]}
        )


def test_the_old_ui_prefixed_lobby_game_routes_are_gone(client: TestClient) -> None:
    """2b's T8 moved `decks_api.py`'s lobby-owned game routes off `/api/ui`
    onto `/api/lobby` - proves the move happened rather than the routes
    being merely duplicated at the new path, the same T5 precedent
    (`test_decks_api.test_the_old_ui_prefixed_deck_routes_are_gone`): 404 for
    a GET, since the catch-all static mount at `/` finds no matching file
    there; 405 for a POST/DELETE, since that mount only serves GET/HEAD."""
    assert client.get("/api/ui/games").status_code == 404
    assert (
        client.post(
            "/api/ui/games",
            json={
                "players": [
                    {"username": "a", "decklist_text": create.__globals__["DECK_A"]},
                    {"username": "b", "decklist_text": create.__globals__["DECK_B"]},
                ]
            },
        ).status_code
        == 405
    )
    assert client.delete("/api/ui/games/no-such-game").status_code == 405
