"""`play-land` can be asked without being done, like every other action."""

from __future__ import annotations

from conftest import card_id, state
from fastapi.testclient import TestClient


def boot(client: TestClient, game: str) -> str:
    return card_id(client, game, "Mountain", "alice", "hand")


def test_dry_run_allows_without_playing(client: TestClient, game: str) -> None:
    land = boot(client, game)
    res = client.post(
        f"/api/games/{game}/actions/play-land",
        json={"player": "alice", "card_id": land, "dry_run": True},
    ).json()
    assert res["allowed"] is True, res
    board = state(client, game)
    assert board["battlefield"] == []
    assert len(board["hands"]["alice"]) == 1


def test_dry_run_refuses_a_second_land_with_the_engines_reason(
    client: TestClient, game: str
) -> None:
    land = boot(client, game)
    second = card_id(client, game, "Mountain", "alice", "hand")
    assert client.post(
        f"/api/games/{game}/actions/play-land", json={"player": "alice", "card_id": land}
    ).json()["allowed"]
    res = client.post(
        f"/api/games/{game}/actions/play-land",
        json={"player": "alice", "card_id": second, "dry_run": True},
    ).json()
    assert res["allowed"] is False
    assert res["reason"]
    board = client.get(f"/api/games/{game}").json()
    assert len(board["battlefield"]) == 1
