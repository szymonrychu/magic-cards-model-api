"""Activating abilities, mana abilities included, with the engine's refusals."""

from conftest import add_card, find, state


def test_activate_mana_ability_fills_the_pool(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    mountain = find(state(client, game)["battlefield"], "Mountain")

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": mountain["id"]},
    )
    body = response.json()
    assert isinstance(body.pop("log_seq"), int)  # the audit entry's own idempotency marker
    assert body == {
        "allowed": True,
        "reason": None,
        "mana_ability": True,
        "cost": "",
    }
    board = state(client, game)
    assert board["players"][0]["mana_pool"]["floating"] == {"R": 1}
    assert find(board["battlefield"], "Mountain")["tapped"] is True


def test_activate_tapped_source_is_refused(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield", tapped=True)
    mountain = find(state(client, game)["battlefield"], "Mountain")
    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": mountain["id"]},
    )
    body = response.json()
    assert isinstance(body.pop("log_seq"), int)
    assert body == {
        "allowed": False,
        "reason": "Mountain is already tapped, so it cannot be tapped to pay a cost",
    }


def test_activate_other_players_permanent_is_refused(client, game):
    add_card(client, game, "Mountain", "bob", "battlefield")
    mountain = find(state(client, game)["battlefield"], "Mountain")
    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": mountain["id"]},
    )
    assert response.json()["allowed"] is False
    assert response.json()["reason"] == "alice does not control Mountain"


def test_dry_run_activation_does_not_tap(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    mountain = find(state(client, game)["battlefield"], "Mountain")
    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": mountain["id"], "dry_run": True},
    )
    assert response.json()["allowed"] is True
    board = state(client, game)
    assert find(board["battlefield"], "Mountain")["tapped"] is False
    assert board["players"][0]["mana_pool"]["floating"] == {}


def test_activate_a_forest_adds_green_to_the_pool(client, game):
    """A Forest's mana ability is reminder text, so `parse_abilities` sees
    nothing: `Game.activated_abilities` falls back to the land's intrinsic
    ability (`intrinsic_land_ability`, CR 305.6) at index 0, the same as any
    other activated ability."""
    add_card(client, game, "Forest", "alice", "battlefield")
    forest = find(state(client, game)["battlefield"], "Forest")
    assert find(state(client, game)["battlefield"], "Forest")["abilities"] == [
        {"index": 0, "text": "{T}: Add {G}.", "mana": True}
    ]

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": forest["id"], "mana_sources": []},
    )
    body = response.json()
    assert isinstance(body.pop("log_seq"), int)
    assert body == {
        "allowed": True,
        "reason": None,
        "mana_ability": True,
        "cost": "",
    }
    board = state(client, game)
    assert board["players"][0]["mana_pool"]["floating"] == {"G": 1}
    assert find(board["battlefield"], "Forest")["tapped"] is True
