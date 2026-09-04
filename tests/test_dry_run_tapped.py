"""A dry run says which sources auto-pay would tap, so the UI can preview
them before the cast fires (CR 601.2g: mana abilities are activated while
paying)."""

from __future__ import annotations

from conftest import card_id
from fastapi.testclient import TestClient


def test_dry_run_cast_reports_the_lands_it_would_tap(client: TestClient, game: str) -> None:
    bolt = card_id(client, game, "Lightning Bolt", "alice", "hand")
    mountain = card_id(client, game, "Mountain", "alice", "battlefield")
    island = card_id(client, game, "Island", "alice", "battlefield")
    res = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt,
            "targets": ["bob"],
            "mana_sources": [mountain, island],
            "dry_run": True,
        },
    ).json()
    assert res["allowed"] is True, res
    assert res["tapped"] == [{"card_id": mountain, "name": "Mountain"}]
    board = client.get(f"/api/games/{game}").json()
    assert all(not c["tapped"] for c in board["battlefield"])


def test_dry_run_activate_reports_tapped_sources(client: TestClient, game: str) -> None:
    mountain = card_id(client, game, "Mountain", "alice", "battlefield")
    res = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": mountain, "ability_index": 0, "dry_run": True},
    ).json()
    assert res["allowed"] is True, res
    assert res["tapped"] == []
