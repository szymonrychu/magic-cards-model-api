"""The app acts as the player's agent when it activates mana abilities.

The frontend offers every untapped land as a mana source; the API may tap only
as many as the payment still needs (CR 601.2g activates mana abilities *before*
costs are paid - it never asks for more activations than the cost requires).
Casting a {1}{G} spell with four Forests offered must tap two, leave two
untapped, and leave nothing floating.
"""

from conftest import add_card, find, state


def four_forests_and_bears(client, game):
    for _ in range(4):
        add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    board = state(client, game)
    forests = [card for card in board["battlefield"] if card["name"] == "Forest"]
    bears = find(board["hands"]["alice"], "Grizzly Bears")
    return forests, bears


def test_only_the_sources_the_cost_needs_are_tapped(client, game):
    forests, bears = four_forests_and_bears(client, game)

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Grizzly Bears",
            "mana_sources": [forest["id"] for forest in forests],
        },
    )
    assert response.json()["allowed"] is True, response.text

    board = state(client, game)
    forests = [card for card in board["battlefield"] if card["name"] == "Forest"]
    assert sum(1 for forest in forests if forest["tapped"]) == 2, "a {1}{G} spell taps two sources"
    assert sum(1 for forest in forests if not forest["tapped"]) == 2
    pool = next(p for p in board["players"] if p["username"] == "alice")["mana_pool"]
    assert pool["floating"] == {}, "exactly the cost was made, so nothing floats"


def test_floating_mana_is_spent_before_any_source_is_tapped(client, game):
    forests, bears = four_forests_and_bears(client, game)
    client.put(f"/api/games/{game}/players/alice/mana-pool", json={"set": {"G": 1}})

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Grizzly Bears",
            "mana_sources": [forest["id"] for forest in forests],
        },
    )
    assert response.json()["allowed"] is True, response.text

    board = state(client, game)
    forests = [card for card in board["battlefield"] if card["name"] == "Forest"]
    assert sum(1 for forest in forests if forest["tapped"]) == 1, "the floating G paid one symbol"


def test_unoffered_sources_are_never_tapped(client, game):
    """A refusal when the offered sources cannot cover the cost must not tap
    more than was offered - and the payment's rewind untaps what was."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    board = state(client, game)
    forest = find(board["battlefield"], "Forest")

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": "Grizzly Bears", "mana_sources": [forest["id"]]},
    )
    assert response.json()["allowed"] is False

    board = state(client, game)
    forest = find(board["battlefield"], "Forest")
    assert forest["tapped"] is False, "the refused payment was rewound"


def test_dry_run_cast_evaluates_the_offered_sources(client, game):
    """A dry run answers for the mana_sources it was offered: activating them
    into the engine's payment, checking, and rewinding without a trace."""
    forests, bears = four_forests_and_bears(client, game)

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Grizzly Bears",
            "mana_sources": [forest["id"] for forest in forests[:2]],
            "dry_run": True,
        },
    )
    assert response.json()["allowed"] is True, response.text
    assert response.json()["cost"] == "{1}{G}"

    board = state(client, game)
    assert all(not forest["tapped"] for forest in board["battlefield"]), (
        "the dry run's taps were rewound"
    )
    assert board["players"][0]["mana_pool"]["floating"] == {}
    assert board["stack"] == []


def test_dry_run_cast_refuses_sources_that_cannot_pay(client, game):
    forests, bears = four_forests_and_bears(client, game)

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Grizzly Bears",
            "mana_sources": [forests[0]["id"]],
            "dry_run": True,
        },
    )
    body = response.json()
    assert body["allowed"] is False
    # The engine's auto-tap solver (`CostPayment.auto_tap`) is asked to pay
    # from just the one offered Forest, finds no legal assignment, and
    # refuses before `pay_with` ever gets to the dry-run shortfall check.
    assert body["reason"] == "no combination of the available mana sources can pay {1}{G}"

    board = state(client, game)
    assert all(not forest["tapped"] for forest in board["battlefield"])
