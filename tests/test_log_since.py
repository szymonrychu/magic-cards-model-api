from __future__ import annotations

from fastapi.testclient import TestClient


def test_since_returns_only_newer_entries(client: TestClient, game: str) -> None:
    created = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    game_id = created["game_id"]
    full = client.get(f"/api/games/{game}/log").json()
    last = full["last_seq"]
    assert last == full["log"][-1]["seq"]
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    newer = client.get(f"/api/games/{game}/log", params={"since": last}).json()
    assert newer["log"], newer
    assert all(entry["seq"] > last for entry in newer["log"])
    assert newer["last_seq"] >= last + 1
    client.delete(f"/api/games/{game_id}")
