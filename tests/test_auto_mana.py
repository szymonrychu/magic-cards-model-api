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


# -- a source the player pinned is still only activated if the cost needs it --
#
# CR 601.2g gives the player a chance to activate mana abilities "if the total
# cost includes a mana payment", and CR 118.3c says activating them is never
# mandatory. Both were looked up from the Comprehensive Rules text in the
# session that wrote these tests (`mise run rules:find`).
#
# The defect: `engine_api.pay_with` gated only *bare id* sources on
# `payment.shortfall` and tapped a `ManaSource` carrying a colour or an
# ability index unconditionally. The browser pins a clicked land as exactly
# that shape (`ui/mana.js`'s `manaSource`), so clicking lands before a cast
# that owes nothing - or owes less than was clicked - tapped them for mana
# that paid for nothing.


def test_a_pinned_source_is_not_tapped_for_a_cost_that_owes_no_mana(client, game):
    """A free cast through a zone permission (plot, CR 702.170b/d) with lands
    already pinned: the total cost has no mana payment in it, so no mana
    ability is activated for it and every pinned land stays untapped.

    One land is pinned as `{card_id, colors}` and the other as `{card_id,
    ability_index}` - the two shapes `pay_with` used to tap unconditionally,
    both of which a browser can send (`ui/mana.js`'s `manaSource`)."""
    from test_special_actions import _bump_turn

    add_card(client, game, "Slickshot Show-Off", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Mountain", "alice", "battlefield")
    mountains = [c["id"] for c in state(client, game)["battlefield"]]

    plotted = client.post(
        f"/api/games/{game}/actions/plot",
        json={"player": "alice", "card": "Slickshot Show-Off", "mana_sources": mountains},
    ).json()
    assert plotted["allowed"] is True, plotted

    board = state(client, game)
    spare = [c["id"] for c in board["battlefield"] if c["name"] == "Mountain" and not c["tapped"]]
    assert len(spare) == 2, "the plot cost took two of the four"
    exiled = find(board["exile"], "Slickshot Show-Off")

    _bump_turn(game)
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": exiled["id"],
            "from_zone": "exile",
            "keyword": "Plot",
            "mana_sources": [
                {"card_id": spare[0], "colors": ["R"]},
                {"card_id": spare[1], "ability_index": 0},
            ],
        },
    ).json()
    assert cast["allowed"] is True, cast
    assert cast["cost"] == ""
    assert cast["tapped"] == [], cast

    board = state(client, game)
    assert [c["name"] for c in board["stack"]] == ["Slickshot Show-Off"]
    still_untapped = [c["id"] for c in board["battlefield"] if not c["tapped"]]
    assert sorted(still_untapped) == sorted(spare), "a free cast taps nothing"
    pool = next(p for p in board["players"] if p["username"] == "alice")["mana_pool"]
    assert pool["floating"] == {}


def test_pinning_more_sources_than_the_cost_needs_taps_only_what_it_needs(client, game):
    """The same rule on the far commoner shape: four Forests clicked for a
    {1}{G} spell. Two of them pay it; the other two are never activated."""
    forests, bears = four_forests_and_bears(client, game)

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Grizzly Bears",
            "mana_sources": [{"card_id": forest["id"], "colors": ["G"]} for forest in forests],
        },
    )
    assert response.json()["allowed"] is True, response.text

    board = state(client, game)
    forests = [card for card in board["battlefield"] if card["name"] == "Forest"]
    assert sum(1 for forest in forests if forest["tapped"]) == 2, "a {1}{G} spell taps two"
    assert sum(1 for forest in forests if not forest["tapped"]) == 2
    pool = next(p for p in board["players"] if p["username"] == "alice")["mana_pool"]
    assert pool["floating"] == {}, "nothing was made that the cost did not need"


def test_a_pinned_source_still_pays_a_phyrexian_cost_the_life_could_have_paid(client, game):
    """The gate asks whether the pool covers the cost *out of mana alone*, not
    `CostPayment.shortfall`, which counts the payer's life as available for a
    Phyrexian symbol (CR 107.4f). A player who pinned a land to pay {G/P} gets
    the land tapped and the mana spent - not a payment that read as already
    affordable because the life was there and quietly took 2 life instead."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Mutagenic Growth", "alice", "hand")
    board = state(client, game)
    forest = find(board["battlefield"], "Forest")
    bears = find(board["battlefield"], "Grizzly Bears")
    life_before = next(p for p in board["players"] if p["username"] == "alice")["life"]

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Mutagenic Growth",
            "targets": [bears["id"]],
            "mana_sources": [{"card_id": forest["id"], "colors": ["G"]}],
        },
    )
    assert response.json()["allowed"] is True, response.text

    board = state(client, game)
    assert find(board["battlefield"], "Forest")["tapped"] is True, "the pinned land paid"
    assert next(p for p in board["players"] if p["username"] == "alice")["life"] == life_before
