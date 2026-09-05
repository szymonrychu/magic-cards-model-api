"""A dry run says which sources auto-pay would tap, so the UI can preview
them before the cast fires (CR 601.2g: mana abilities are activated while
paying)."""

from __future__ import annotations

from conftest import add_card, card_id, find, state
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


def test_dry_run_cycle_reports_tapped_sources(client: TestClient, game: str) -> None:
    """CR 702.29a: cycling is an activated ability that functions only in
    hand, so it goes through its own `actions/cycle` endpoint
    (`engine_api.cycle_card`) rather than `actions/activate` - and its
    dry-run branch used to leave `tapped` off the response entirely, unlike
    `cast_card`/`activate_ability`. Angelsong (`{2}, Discard this card:
    Draw a card`) is the fixture `test_special_actions.py` already cycles for
    real; an Island and a Mountain (two different names, so `conftest.py`'s
    `card_id` - which finds a card by name and does not track which one it
    already handed back - resolves each to a distinct card) pay its generic
    `{2}`."""
    angelsong = card_id(client, game, "Angelsong", "alice", "hand")
    island = card_id(client, game, "Island", "alice", "battlefield")
    mountain = card_id(client, game, "Mountain", "alice", "battlefield")
    res = client.post(
        f"/api/games/{game}/actions/cycle",
        json={
            "player": "alice",
            "card_id": angelsong,
            "mana_sources": [island, mountain],
            "dry_run": True,
        },
    ).json()
    assert res["allowed"] is True, res
    assert {t["card_id"] for t in res["tapped"]} == {island, mountain}
    # A dry run leaves no trace (CR 733.1): Angelsong is still in hand, and
    # both lands are still untapped.
    board = client.get(f"/api/games/{game}").json()
    assert all(not c["tapped"] for c in board["battlefield"])
    assert any(c["name"] == "Angelsong" for c in state(client, game)["hands"]["alice"])


def test_dry_run_crew_reports_tapped_sources(client: TestClient, game: str) -> None:
    """CR 702.122a's cost is paid by tapping creatures, never mana - so
    `tapped` here is always `[]` - but `crew_vehicle`'s dry-run branch used to
    omit the field entirely (unlike `cast_card`/`activate_ability`), leaving
    the UI's own `preview.tapped` read (`ui/app.js`'s `previewAndMaybeFire`)
    with nothing to read for a crew action at all. This pins that the field
    is present and shaped the same way regardless."""
    add_card(client, game, "Smuggler's Copter", "alice", "battlefield")
    add_card(client, game, "Savannah Lions", "alice", "battlefield")
    board = state(client, game)["battlefield"]
    copter, lions = find(board, "Smuggler's Copter"), find(board, "Savannah Lions")
    res = client.post(
        f"/api/games/{game}/actions/crew",
        json={
            "player": "alice",
            "card_id": copter["id"],
            "crewing": [lions["id"]],
            "dry_run": True,
        },
    ).json()
    assert res["allowed"] is True, res
    assert res["tapped"] == []
    # A dry run leaves no trace (CR 733.1's own rewind) - the crewing
    # creature is still untapped and the Vehicle is still not a creature.
    after = state(client, game)["battlefield"]
    assert find(after, "Savannah Lions")["tapped"] is False
    assert "Creature" not in find(after, "Smuggler's Copter")["type_line"]
