"""Electrolyze's 2 damage split 1 and 1 (CR 601.2d), announced with the cast."""

from __future__ import annotations

from conftest import add_card, card_id, state
from fastapi.testclient import TestClient


def test_shares_are_announced_and_dealt(client: TestClient, game: str) -> None:
    electrolyze = card_id(client, game, "Electrolyze", "alice", "hand")
    memnite = card_id(client, game, "Memnite", "bob", "battlefield")
    for name in ("Island", "Mountain", "Mountain"):
        add_card(client, game, name, "alice", "battlefield")
    res = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": electrolyze,
            "targets": [memnite, "bob"],
            "shares": [1, 1],
            "mana_sources": [
                c["id"] for c in state(client, game)["battlefield"] if c["controller"] == "alice"
            ],
        },
    ).json()
    assert res["allowed"] is True, res
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    board = client.get(f"/api/games/{game}").json()
    assert all(c["id"] != memnite for c in board["battlefield"])
    bob = next(p for p in board["players"] if p["username"] == "bob")
    assert bob["life"] == 19


def test_shares_that_do_not_add_up_are_refused(client: TestClient, game: str) -> None:
    electrolyze = card_id(client, game, "Electrolyze", "alice", "hand")
    memnite = card_id(client, game, "Memnite", "bob", "battlefield")
    res = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": electrolyze,
            "targets": [memnite, "bob"],
            "shares": [2, 1],
            "dry_run": True,
        },
    ).json()
    assert res["allowed"] is False
    assert "divide" in res["reason"].lower() or "601.2d" in res["reason"]
