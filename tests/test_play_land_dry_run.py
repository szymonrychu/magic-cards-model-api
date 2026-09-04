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


def test_dry_run_checks_is_a_land_before_the_player_level_reason(
    client: TestClient, game: str
) -> None:
    """`Game.play_land` checks is-a-land before it asks `why_cannot_play_land`
    (which would refuse for the wrong reason - wrong step - once combat has
    started); a dry run must ask in that same order, not player-level first."""
    bolt = card_id(client, game, "Lightning Bolt", "alice", "hand")
    assert (
        client.put(f"/api/games/{game}/step", json={"step": "declare_attackers"}).status_code == 200
    )
    dry = client.post(
        f"/api/games/{game}/actions/play-land",
        json={"player": "alice", "card_id": bolt, "dry_run": True},
    ).json()
    real = client.post(
        f"/api/games/{game}/actions/play-land",
        json={"player": "alice", "card_id": bolt},
    ).json()
    assert dry["allowed"] is False
    assert real["allowed"] is False
    assert dry["reason"] == real["reason"]
    assert "is not a land" in dry["reason"], dry
