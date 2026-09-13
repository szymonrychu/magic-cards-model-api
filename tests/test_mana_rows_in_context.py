"""Mana rows say what a source makes on this board, and a cast payable through
a manually activated mana ability is not called unpayable.

Battle 2, B2-14. With Urza's Mine, Urza's Power-Plant and Urza's Tower all
controlled, every Tron row said `produces "{C}"`, while Tower actually added
`{C}{C}{C}`. CR 205.3i makes "Urza's", "Mine", "Power-Plant" and "Tower" land
types, and each piece's "If you control an Urza's Mine and an Urza's
Power-Plant, add {C}{C}{C} instead" reads them off the board - the engine
already resolves exactly that on activation (`ManaAbility.effective`); the
row only repeated the printed first sentence.

The second half: a cast the engine's auto-tap cannot pay because the mana it
needs sits behind a mana ability costing more than `{T}` (CR 118.3c:
"Activating mana abilities is not mandatory, even if paying a cost is") was
labelled "no combination of the available mana sources can pay", although
activating that ability by hand first pays it. Darkwater Catacombs'
`{1}, {T}: Add {U}{B}.` is a real filter land with no bare `{T}` ability, the
same shape the battle hit.
"""

from __future__ import annotations

from conftest import add_card, find, state
from test_legal_actions_endpoint import legal_actions

from magic_cards_model_api import baseline_seat


def battlefield_ids(client, game: str) -> dict[str, str]:
    return {card["name"]: card["id"] for card in state(client, game)["battlefield"]}


def mana_row(client, game: str, card_id: str, index: int = 0) -> dict:
    (row,) = [
        row
        for row in legal_actions(client, game)
        if row["kind"] == "activate_mana"
        and row["card_id"] == card_id
        and row["ability_index"] == index
    ]
    return row


def assemble_tron(client, game: str) -> dict[str, str]:
    for name in ("Urza's Tower", "Urza's Mine", "Urza's Power Plant"):
        add_card(client, game, name, "alice", "battlefield")
    return battlefield_ids(client, game)


# -- Tron -------------------------------------------------------------------------


def test_tron_rows_state_what_each_piece_makes_with_tron_assembled(client, game):
    ids = assemble_tron(client, game)

    tower = mana_row(client, game, ids["Urza's Tower"])
    assert tower["produces"] == "{C}{C}{C}"
    assert tower["printed_produces"] == "{C}"
    for piece in ("Urza's Mine", "Urza's Power Plant"):
        row = mana_row(client, game, ids[piece])
        assert row["produces"] == "{C}{C}", row
        assert row["printed_produces"] == "{C}"


def test_a_lone_tower_row_is_what_it_prints(client, game):
    add_card(client, game, "Urza's Tower", "alice", "battlefield")
    row = mana_row(client, game, battlefield_ids(client, game)["Urza's Tower"])
    assert row["produces"] == "{C}"
    assert "printed_produces" not in row


def test_the_tower_row_matches_what_activating_it_adds(client, game):
    ids = assemble_tron(client, game)
    assert mana_row(client, game, ids["Urza's Tower"])["produces"] == "{C}{C}{C}"

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": ids["Urza's Tower"], "ability_index": 0},
    )
    assert response.json()["allowed"] is True, response.json()
    alice = next(p for p in state(client, game)["players"] if p["username"] == "alice")
    assert alice["mana_pool"]["floating"] == {"C": 3}


def test_naming_tron_pieces_by_ability_pays_with_their_real_output(client, game):
    """`mana_sources` naming an ability resolves it on the board too: Tower's
    `{C}{C}{C}` and Mine's `{C}{C}` pay Thought-Knot Seer's `{3}{C}`."""
    ids = assemble_tron(client, game)
    add_card(client, game, "Thought-Knot Seer", "alice", "hand")
    seer = find(state(client, game)["hands"]["alice"], "Thought-Knot Seer")["id"]

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": seer,
            "dry_run": True,
            "mana_sources": [
                {"card_id": ids["Urza's Tower"], "ability_index": 0},
                {"card_id": ids["Urza's Mine"], "ability_index": 0},
            ],
        },
    )
    assert response.json()["allowed"] is True, response.json()


# -- a filter land ------------------------------------------------------------------


def filter_board(client, game: str, *, with_filter: bool = True) -> dict[str, str]:
    add_card(client, game, "Island", "alice", "battlefield")
    if with_filter:
        add_card(client, game, "Darkwater Catacombs", "alice", "battlefield")
    add_card(client, game, "Thoughtseize", "alice", "hand")
    ids = battlefield_ids(client, game)
    ids["Thoughtseize"] = find(state(client, game)["hands"]["alice"], "Thoughtseize")["id"]
    return ids


def thoughtseize_row(client, game: str, card_id: str) -> dict:
    (row,) = [
        row
        for row in legal_actions(client, game)
        if row["kind"] == "cast" and row["card_id"] == card_id
    ]
    return row


def test_a_cast_payable_only_through_a_filter_land_is_not_cannot_pay(client, game):
    ids = filter_board(client, game)

    row = thoughtseize_row(client, game, ids["Thoughtseize"])
    assert row["affordable"] == "yes", row
    assert row.get("unaffordable_reason") is None
    assert row["activate_first"] == [
        {
            "card_id": ids["Darkwater Catacombs"],
            "name": "Darkwater Catacombs",
            "ability_index": 0,
            "cost": "{1}, {T}",
            "adds": "{U}{B}",
        }
    ]


def test_the_manual_path_the_row_names_actually_pays(client, game):
    ids = filter_board(client, game)

    activated = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": ids["Darkwater Catacombs"],
            "ability_index": 0,
            "mana_sources": [ids["Island"]],
        },
    )
    assert activated.json()["allowed"] is True, activated.json()
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": ids["Thoughtseize"], "targets": ["bob"]},
    )
    assert cast.json()["allowed"] is True, cast.json()


def test_without_the_filter_land_the_row_is_still_cannot_pay(client, game):
    ids = filter_board(client, game, with_filter=False)

    row = thoughtseize_row(client, game, ids["Thoughtseize"])
    assert row["affordable"] == "no"
    assert "activate_first" not in row


def test_the_baseline_seat_does_not_take_a_row_that_needs_a_manual_activation():
    """Its policy casts a row as it stands; a row asking for an activation
    first would be refused if cast straight away."""
    row = {"kind": "cast", "affordable": "yes", "activate_first": [{"card_id": "x"}]}
    assert baseline_seat._plain_cast(row) is False
