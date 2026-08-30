"""Mana pools empty as each step and phase ends (CR 500.5, 703.4q).

The user's report, end to end through the API: green floated during upkeep was
still spendable in the precombat main. Moving the step forward crosses the
upkeep's end, and the pool must empty there.
"""

from conftest import add_card, find, state


def test_upkeep_mana_is_gone_by_the_precombat_main(client, game):
    for _ in range(2):
        add_card(client, game, "Forest", "alice", "battlefield")
    client.put(f"/api/games/{game}/step", json={"step": "upkeep"})

    forest = find(state(client, game)["battlefield"], "Forest")
    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": forest["id"]},
    )
    assert response.json()["allowed"] is True, response.text
    board = state(client, game)
    assert board["players"][0]["mana_pool"]["floating"] == {"G": 1}

    client.put(f"/api/games/{game}/step", json={"step": "precombat_main"})

    board = state(client, game)
    assert board["players"][0]["mana_pool"]["floating"] == {}, (
        "the upkeep's end emptied the pool (CR 500.5)"
    )


def test_the_advance_endpoint_empties_the_pool_too(client, game):
    """POST /advance is the full funnel; the upkeep mana must not survive it."""
    add_card(client, game, "Forest", "alice", "battlefield")
    client.put(f"/api/games/{game}/step", json={"step": "upkeep"})

    forest = find(state(client, game)["battlefield"], "Forest")
    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": forest["id"]},
    )
    assert response.json()["allowed"] is True, response.text

    client.post(f"/api/games/{game}/advance", json={"player": "alice"})

    board = state(client, game)
    assert board["players"][0]["mana_pool"]["floating"] == {}
    assert board["step"] == "draw"
