"""Double-faced card views, and the face a selector reads in each zone.

A modal double-faced printing (Scryfall layout `modal_dfc`) leaves the
top-level `oracle_text` null and puts each face's text on `card_faces`, so a
view that copies the top-level field hands a seat `oracle_text: null` and
every `oracle_has` selector silently misses the card (battle 1, D02).

CR 712.8a: "While a double-faced card is outside the game or in a zone other
than the battlefield or stack, it has only the characteristics of its front
face." CR 712.8f: "While a modal double-faced spell is on the stack or a modal
double-faced permanent is on the battlefield, it has only the characteristics
of the face that's up." So Witch Enchanter in a hand is a Creature and not a
Land, and Witch-Blessed Meadow on the battlefield is a Land and not a Creature
(battle 1, D03). Both rule texts were read from the Comprehensive Rules
(effective August 7, 2026) in the session that wrote this file.

Every case drives the real engine through `TestClient`; Valakut Awakening is
here so the fix is proven on a second modal DFC, not only the reported one.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, force_turn_position, state
from magic_cards_model.machines import Step

from magic_cards_model_api.playscripts.selectors import matches
from magic_cards_model_api.store import store

WITCH_FRONT_TEXT = (
    "When this creature enters, destroy target artifact or enchantment an opponent controls."
)
MEADOW_TEXT = "As this land enters, you may pay 3 life."


def _uid(game: str, player: str) -> str:
    return next(uid for uid, name in store.seat_uids(game).items() if name == player)


def _card(cards: list[dict[str, Any]], card_name: str) -> dict[str, Any]:
    return next(card for card in cards if card["card_name"] == card_name)


def test_an_mdfc_in_hand_carries_its_front_face_text_and_both_faces(client, game):
    add_card(client, game, "Witch Enchanter", "alice", "hand")

    witch = _card(state(client, game)["hands"]["alice"], "Witch Enchanter // Witch-Blessed Meadow")

    assert witch["oracle_text"] == WITCH_FRONT_TEXT
    assert [face["name"] for face in witch["faces"]] == ["Witch Enchanter", "Witch-Blessed Meadow"]
    assert witch["faces"][0]["type_line"] == "Creature — Human Warlock"
    assert witch["faces"][0]["oracle_text"] == WITCH_FRONT_TEXT
    assert witch["faces"][0]["mana_cost"] == "{3}{W}"
    assert witch["faces"][1]["type_line"] == "Land"
    assert witch["faces"][1]["oracle_text"].startswith(MEADOW_TEXT)


def test_a_single_faced_card_carries_no_faces(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")

    bolt = _card(state(client, game)["hands"]["alice"], "Lightning Bolt")

    assert "faces" not in bolt
    assert "Lightning Bolt deals 3 damage" in bolt["oracle_text"]


def test_a_selector_reads_only_the_front_face_of_an_mdfc_in_hand(client, game):
    add_card(client, game, "Witch Enchanter", "alice", "hand")
    add_card(client, game, "Valakut Awakening", "alice", "hand")
    hand = state(client, game)["hands"]["alice"]

    for card_name, front_type in (
        ("Witch Enchanter // Witch-Blessed Meadow", "Creature"),
        ("Valakut Awakening // Valakut Stoneforge", "Instant"),
    ):
        card = _card(hand, card_name)
        assert not matches({"types_any": ["Land"]}, card), card_name
        assert matches({"types_any": [front_type]}, card), card_name

    witch = _card(hand, "Witch Enchanter // Witch-Blessed Meadow")
    assert matches({"oracle_has": "destroy target artifact or enchantment"}, witch)
    assert matches({"subtypes_any": ["Warlock"]}, witch)
    assert not matches({"oracle_has": "you may pay 3 life"}, witch)


def test_a_selector_reads_the_land_face_an_mdfc_was_played_as(client, game):
    add_card(client, game, "Witch Enchanter", "alice", "hand")
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    witch_id = _card(
        state(client, game)["hands"]["alice"], "Witch Enchanter // Witch-Blessed Meadow"
    )["id"]

    played = client.post(
        f"/api/games/{game}/actions/play-land", json={"player": "alice", "card_id": witch_id}
    )
    assert played.status_code == 200, played.text
    body = played.json()
    while body.get("paused"):
        pending = body["pending_decision"]
        answered = client.post(
            f"/api/games/{game}/decide",
            params={"uid": _uid(game, pending["player"])},
            json={"player": pending["player"], "accept": False},
        )
        assert answered.status_code == 200, answered.text
        body = answered.json()

    # CR 400.7: the land that entered is a new object, so it is found by card, not id.
    meadow = _card(state(client, game)["battlefield"], "Witch Enchanter // Witch-Blessed Meadow")
    assert meadow["name"] == "Witch-Blessed Meadow"
    assert meadow["oracle_text"].startswith(MEADOW_TEXT)
    assert matches({"types_any": ["Land"]}, meadow)
    assert not matches({"types_any": ["Creature"]}, meadow)
    assert matches({"oracle_has": "you may pay 3 life"}, meadow)
    assert not matches({"oracle_has": "destroy target artifact or enchantment"}, meadow)


def test_an_mdfc_put_onto_the_battlefield_front_face_up_is_its_front_face(client, game):
    add_card(client, game, "Witch Enchanter", "alice", "battlefield")

    witch = _card(state(client, game)["battlefield"], "Witch Enchanter // Witch-Blessed Meadow")

    assert witch["oracle_text"] == WITCH_FRONT_TEXT
    assert matches({"types_any": ["Creature"]}, witch)
    assert not matches({"types_any": ["Land"]}, witch)


def test_an_mdfc_in_the_library_contents_reads_its_front_face(client, game):
    add_card(client, game, "Witch Enchanter", "alice", "library")
    uid = _uid(game, "alice")

    response = client.get(f"/api/games/{game}", params={"uid": uid, "include": "contents"})
    assert response.status_code == 200, response.text
    entry = next(
        entry
        for entry in response.json()["library_contents"]["alice"]
        if entry["card"]["card_name"] == "Witch Enchanter // Witch-Blessed Meadow"
    )

    assert entry["card"]["oracle_text"] == WITCH_FRONT_TEXT
    assert not matches({"types_any": ["Land"]}, entry["card"])
    assert matches({"oracle_has": "destroy target artifact or enchantment"}, entry["card"])
