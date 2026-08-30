"""The reported defect: `cast`/`activate` with no `--sources` refused every
spell paid from a dual or a shockland, quoting `ManaAbility.resolve`'s
internal "this ability offers [['W'], ['U']]; pass the colour chosen"
straight out of `machines/abilities.py`. `pay_with` (`engine_api.py`) used to
hand every offered source straight to `mana_from`, which raises the moment a
choice ability is asked to produce mana with no colour picked; it now defers
any source with no explicit colour to the engine's `CostPayment.auto_tap`
(`machines/cost.py`), which solves the colour assignment itself.
"""

from conftest import add_card, find, state


def _untapped_ids(board: dict, *names: str) -> list[str]:
    """Every untapped permanent id on the battlefield matching `names` - the
    same set `seat_cli.sources_or_all` sends as `mana_sources` when a player
    gives no `--sources`, which is where the reported defect actually lives:
    the raw HTTP API only auto-solves the sources it is *given*, and it is
    the CLI's default that gives it every untapped source with no colour
    named."""
    return [card["id"] for card in board["battlefield"] if card["name"] in names]


def test_casting_off_a_single_dual_with_no_colour_given(client, game):
    """The minimal repro: one Sacred Foundry offered with no colour chosen -
    exactly what `seat_cli`'s default sends."""
    add_card(client, game, "Sacred Foundry", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    sources = _untapped_ids(state(client, game), "Sacred Foundry")

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Shock",
            "targets": ["bob"],
            "mana_sources": sources,
        },
    )
    assert response.json()["allowed"] is True, response.text

    board = state(client, game)
    assert find(board["battlefield"], "Sacred Foundry")["tapped"] is True


def test_casting_a_two_colour_spell_off_two_duals_with_no_colour_given(client, game):
    """Lightning Helix ({R}{W}) off two Sacred Foundries, each of which could
    make either colour, both offered with no colour chosen - the exact shape
    of the live playtest's failure."""
    add_card(client, game, "Sacred Foundry", "alice", "battlefield")
    add_card(client, game, "Sacred Foundry", "alice", "battlefield")
    add_card(client, game, "Lightning Helix", "alice", "hand")
    sources = _untapped_ids(state(client, game), "Sacred Foundry")

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Lightning Helix",
            "targets": ["bob"],
            "mana_sources": sources,
        },
    )
    assert response.json()["allowed"] is True, response.text

    board = state(client, game)
    foundries = [card for card in board["battlefield"] if card["name"] == "Sacred Foundry"]
    assert all(card["tapped"] for card in foundries)
    pool = next(p for p in board["players"] if p["username"] == "alice")["mana_pool"]
    assert pool["floating"] == {}


def test_a_hand_of_blink_spells_off_a_shockland_and_dual_manabase(client, game):
    """The reported scenario: a manabase that is almost entirely shocklands
    and Ravnica duals, casting several spells in a row with every untapped
    source offered and no colour named for any of them - the shape of
    `seat_cli`'s default - each one succeeding on its own."""
    for land in ["Sacred Foundry", "Hallowed Fountain", "Sacred Foundry", "Hallowed Fountain"]:
        add_card(client, game, land, "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    add_card(client, game, "Lightning Helix", "alice", "hand")

    board = state(client, game)
    sources = _untapped_ids(board, "Sacred Foundry", "Hallowed Fountain")
    first = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Shock",
            "targets": ["bob"],
            "mana_sources": sources,
        },
    )
    assert first.json()["allowed"] is True, first.text

    board = state(client, game)
    sources = _untapped_ids(board, "Sacred Foundry", "Hallowed Fountain")
    second = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Lightning Helix",
            "targets": ["bob"],
            "mana_sources": sources,
        },
    )
    assert second.json()["allowed"] is True, second.text


def test_a_cost_that_truly_cannot_be_paid_is_refused_clearly(client, game):
    """Two Sacred Foundries (R/W only) cannot make Preordain's blue, even
    offered with no colour named - refused, and the reason names the cost
    rather than quoting a list of lists."""
    add_card(client, game, "Sacred Foundry", "alice", "battlefield")
    add_card(client, game, "Sacred Foundry", "alice", "battlefield")
    add_card(client, game, "Preordain", "alice", "hand")
    sources = _untapped_ids(state(client, game), "Sacred Foundry")

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": "Preordain", "mana_sources": sources},
    )
    body = response.json()
    assert body["allowed"] is False
    assert "{U}" in body["reason"]
    assert "[[" not in body["reason"]

    board = state(client, game)
    assert all(not card["tapped"] for card in board["battlefield"])


def test_explicit_sources_still_take_precedence_over_the_solver(client, game):
    """Naming a specific land keeps only that land tapped, even though a
    Mountain sitting right next to it would have been the solver's own
    pick."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Sacred Foundry", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    board = state(client, game)
    foundry = find(board["battlefield"], "Sacred Foundry")

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Shock",
            "targets": ["bob"],
            "mana_sources": [foundry["id"]],
        },
    )
    assert response.json()["allowed"] is True, response.text

    board = state(client, game)
    assert find(board["battlefield"], "Sacred Foundry")["tapped"] is True
    assert find(board["battlefield"], "Mountain")["tapped"] is False


def test_eldrazi_temple_restricted_ability_is_not_offered_automatically(client, game):
    """Eldrazi Temple's second ability carries a CR 106.6 spend restriction
    this engine cannot verify, so the solver may only use its unrestricted
    `{T}: Add {C}.` - one mana, not two - and a {2} cost is refused."""
    add_card(client, game, "Eldrazi Temple", "alice", "battlefield")
    add_card(client, game, "Mind Stone", "alice", "hand")
    sources = _untapped_ids(state(client, game), "Eldrazi Temple")

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": "Mind Stone", "mana_sources": sources},
    )
    body = response.json()
    assert body["allowed"] is False

    board = state(client, game)
    assert find(board["battlefield"], "Eldrazi Temple")["tapped"] is False


def test_sunbaked_canyons_life_payment_is_never_spent_automatically(client, game):
    """Sunbaked Canyon's only mana ability costs `{T}, Pay 1 life`. The
    solver never spends a cost the player did not agree to, so it leaves the
    land untapped and the cast is refused rather than quietly paying life."""
    add_card(client, game, "Sunbaked Canyon", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    sources = _untapped_ids(state(client, game), "Sunbaked Canyon")

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Shock",
            "targets": ["bob"],
            "mana_sources": sources,
        },
    )
    assert response.json()["allowed"] is False

    board = state(client, game)
    assert find(board["battlefield"], "Sunbaked Canyon")["tapped"] is False
    alice = next(p for p in board["players"] if p["username"] == "alice")
    assert alice["life"] == 20


def test_activate_color_still_chooses_what_the_activated_ability_itself_makes(client, game):
    """`--color` on `activate` is a different choice from the payment solver:
    it is what the *activated* mana ability itself produces, not how its own
    (free) activation cost gets paid. Directly activating a Sacred Foundry
    for white must still honour an explicit `color`."""
    add_card(client, game, "Sacred Foundry", "alice", "battlefield")
    board = state(client, game)
    foundry = find(board["battlefield"], "Sacred Foundry")

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": foundry["id"],
            "ability_index": 0,
            "color": "W",
        },
    )
    assert response.json()["allowed"] is True, response.text

    board = state(client, game)
    pool = next(p for p in board["players"] if p["username"] == "alice")["mana_pool"]
    assert pool["floating"] == {"W": 1}
