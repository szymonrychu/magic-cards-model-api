"""A double-faced card in hand is cast for its front face, and the api says so.

Battle 2, B2-02 and B2-03. Scryfall leaves a `transform` or `modal_dfc`
printing's top-level `mana_cost` and `oracle_text` null and puts each face's
own values on `card_faces`. CR 712.8a: "While a double-faced card is outside
the game or in a zone other than the battlefield or stack, it has only the
characteristics of its front face." So Ajani, Nacatl Pariah in hand costs
`{1}{W}`, and the engine's own cast (`Casting.base_cost`) already charges
exactly that - but the legal-action row read the blank top-level field and
called the card uncastable under CR 118.6 ("Some objects have no mana cost"),
and casting it by its front face's name found nothing in hand.

Every card here is a real printing from the engine's corpus, and every board
is played through the real API.
"""

from __future__ import annotations

from conftest import add_card, find, state
from test_legal_actions_endpoint import legal_actions

from magic_cards_model_api.store import store


def cast_row(client, game: str, name: str, player: str = "alice") -> dict:
    card = find(state(client, game)["hands"][player], name)
    rows = [
        row
        for row in legal_actions(client, game, player)
        if row["kind"] == "cast" and row["card_id"] == card["id"]
    ]
    assert len(rows) == 1, rows
    return rows[0]


# -- B2-02: the row --------------------------------------------------------


def test_ajani_row_offers_the_front_face_cost_and_is_castable(client, game):
    """The reported card: `{1}{W}` with a Plains and a Mountain untapped."""
    add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Ajani, Nacatl Pariah", "alice", "hand")

    row = cast_row(client, game, "Ajani, Nacatl Pariah")
    assert row["cost"] == "{1}{W}"
    assert row["mana_cost"] == "{1}{W}"
    assert row["affordable"] == "yes", row
    assert row.get("unaffordable_reason") is None
    assert {entry["name"] for entry in row["pays_with"]} == {"Plains", "Mountain"}


def test_a_second_transforming_card_gets_the_same_row(client, game):
    """Not the reported card: Delver of Secrets // Insectile Aberration, `{U}`."""
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Delver of Secrets", "alice", "hand")

    row = cast_row(client, game, "Delver of Secrets")
    assert row["cost"] == "{U}"
    assert row["affordable"] == "yes", row


def test_a_modal_double_faced_spell_front_gets_the_same_row(client, game):
    """The other double-faced layout: Jwari Disruption // Jwari Ruins, `{1}{U}`."""
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Jwari Disruption", "alice", "hand")

    row = cast_row(client, game, "Jwari Disruption")
    assert row["cost"] == "{1}{U}"
    assert row["affordable"] == "yes", row


def test_an_unaffordable_front_face_says_mana_not_no_mana_cost(client, game):
    """With no lands the row is still `no`, for the real reason: the front
    face's cost cannot be paid. CR 118.6 is about objects that truly have no
    mana cost, which Ajani in hand is not."""
    add_card(client, game, "Ajani, Nacatl Pariah", "alice", "hand")

    row = cast_row(client, game, "Ajani, Nacatl Pariah")
    assert row["cost"] == "{1}{W}"
    assert row["affordable"] == "no"
    assert "118.6" not in row["unaffordable_reason"]
    assert "{1}{W}" in row["unaffordable_reason"]


def test_the_hand_card_view_shows_the_front_face_cost_and_text(client, game):
    add_card(client, game, "Ajani, Nacatl Pariah", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Ajani, Nacatl Pariah")
    assert card["mana_cost"] == "{1}{W}"
    assert card["oracle_text"].startswith("When Ajani enters")


# -- B2-03: casting by name ---------------------------------------------------


def cast_by_name(client, game: str, name: str, **extra) -> dict:
    lands = [card["id"] for card in state(client, game)["battlefield"]]
    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": name, "mana_sources": lands, **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_ajani_casts_by_its_front_face_name(client, game):
    add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Ajani, Nacatl Pariah", "alice", "hand")

    body = cast_by_name(client, game, "Ajani, Nacatl Pariah")
    assert body["allowed"] is True, body
    assert body["cost"] == "{1}{W}"
    assert [obj["name"] for obj in state(client, game)["stack"]] == ["Ajani, Nacatl Pariah"]


def test_the_full_printed_name_still_casts(client, game):
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Delver of Secrets", "alice", "hand")

    body = cast_by_name(client, game, "Delver of Secrets // Insectile Aberration", dry_run=True)
    assert body["allowed"] is True, body


def test_a_modal_double_faced_front_face_casts_by_name(client, game):
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Jwari Disruption", "alice", "hand")

    body = cast_by_name(client, game, "Jwari Disruption", dry_run=True)
    assert body["allowed"] is True, body


def test_the_back_face_name_is_not_a_card_in_hand(client, game):
    """CR 712.8a: in hand the card has only its front face's characteristics,
    so its back face's name names nothing there."""
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Delver of Secrets", "alice", "hand")

    body = cast_by_name(client, game, "Insectile Aberration", dry_run=True)
    assert body["allowed"] is False
    assert body["reason"] == "no Insectile Aberration in alice's hand"


def test_a_name_two_different_cards_answer_to_is_refused_as_ambiguous(client, game):
    """No two real printings share a front face name, so the second card is
    made to: a Grizzly Bears renamed in place to Ajani's front face. A name
    that could mean either card is refused with a typed refusal naming both,
    rather than casting whichever came first."""
    add_card(client, game, "Ajani, Nacatl Pariah", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    live = store.get(game)
    assert live is not None
    bears = next(card for card in live.player("alice").hand if card.card.name == "Grizzly Bears")
    bears.card = bears.card.model_copy(update={"name": "Ajani, Nacatl Pariah"})

    body = cast_by_name(client, game, "Ajani, Nacatl Pariah", dry_run=True)
    assert body["allowed"] is False
    assert body["type"] == "ambiguous_card_name"
    assert "card_id" in body["reason"]
    assert sorted(body["candidates"], key=lambda c: c["card_name"]) == sorted(
        [
            {"card_id": str(bears.id), "card_name": "Ajani, Nacatl Pariah"},
            {
                "card_id": next(
                    str(card.id)
                    for card in live.player("alice").hand
                    if card.card.name.startswith("Ajani, Nacatl Pariah //")
                ),
                "card_name": "Ajani, Nacatl Pariah // Ajani, Nacatl Avenger",
            },
        ],
        key=lambda c: c["card_name"],
    )


def test_two_copies_of_one_card_are_not_ambiguous(client, game):
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Delver of Secrets", "alice", "hand")
    add_card(client, game, "Delver of Secrets", "alice", "hand")

    body = cast_by_name(client, game, "Delver of Secrets", dry_run=True)
    assert body["allowed"] is True, body
