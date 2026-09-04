"""A client can tell what a spell on the stack is aimed at by id, not only by name."""

from __future__ import annotations

from conftest import card_id
from fastapi.testclient import TestClient


def test_bolt_on_the_stack_names_bob_by_id(client: TestClient, game: str) -> None:
    bolt = card_id(client, game, "Lightning Bolt", "alice", "hand")
    mountain = card_id(client, game, "Mountain", "alice", "battlefield")
    res = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": bolt, "targets": ["bob"], "mana_sources": [mountain]},
    ).json()
    assert res["allowed"], res
    (spell,) = client.get(f"/api/games/{game}").json()["stack"]
    assert spell["targets"] == ["bob"]
    assert spell["target_refs"] == [{"kind": "player", "id": "bob", "name": "bob"}]
