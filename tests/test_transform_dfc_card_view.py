"""Transforming double-faced cards read as the face they have (battle 2, B2-12).

Battle 2 (on an api build before F2v) counted an exiled Ajani, Nacatl Pariah
as a Planeswalker, and showed the transformed Ajani, Nacatl Avenger with
`oracle_text: null`. The rules, read from the Comprehensive Rules file in the
engine tree in the session that wrote this file:

- CR 712.8a: "While a double-faced card is outside the game or in a zone other
  than the battlefield or stack, it has only the characteristics of its front
  face."
- CR 712.8d: "While a double-faced permanent has its front face up, it has only
  the characteristics of its front face."
- CR 712.8e: "While a nonmodal double-faced permanent has its back face up, it
  has only the characteristics of its back face. However, its mana value is
  calculated using the mana cost of its front face."

So Ajani in a hand, graveyard, exile or library is a Creature - Cat Warrior and
never a Planeswalker, and transformed on the battlefield he is a Planeswalker
whose view carries the Avenger's text. Delver of Secrets proves the same on a
second transforming card that is not the reported one.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import add_card, state
from playscript_testkit import play

from magic_cards_model_api.playscripts.conditions import evaluate
from magic_cards_model_api.playscripts.selectors import matches
from magic_cards_model_api.store import store

AJANI = "Ajani, Nacatl Pariah // Ajani, Nacatl Avenger"
PARIAH_TEXT = "When Ajani enters, create a 2/1 white Cat Warrior creature token."
AVENGER_TEXT = "+2: Put a +1/+1 counter on each Cat you control."
DELVER = "Delver of Secrets // Insectile Aberration"


def _uid(game: str, player: str) -> str:
    return next(uid for uid, who in store.seat_uids(game).items() if who == player)


def _in(view: dict[str, Any], zone: str, card_name: str) -> dict[str, Any]:
    cards = {
        "hand": view["hands"]["alice"],
        "graveyard": view["graveyards"]["alice"],
        "exile": view["exile"],
        "battlefield": view["battlefield"],
    }[zone]
    return next(card for card in cards if card["card_name"] == card_name)


def _transform(game: str, card_name: str) -> None:
    live = store.get(game)
    permanent = next(c for c in live.battlefield if c.card.name == card_name)
    permanent.transform()
    assert permanent.transformed


@pytest.mark.parametrize("zone", ["hand", "graveyard", "exile"])
def test_ajani_off_the_battlefield_is_only_his_front_face(client, game, zone):
    add_card(client, game, "Ajani, Nacatl Pariah", "alice", zone)
    ajani = _in(state(client, game, _uid(game, "alice")), zone, AJANI)

    assert ajani["oracle_text"].startswith(PARIAH_TEXT)
    assert [face["name"] for face in ajani["faces"]] == [
        "Ajani, Nacatl Pariah",
        "Ajani, Nacatl Avenger",
    ]
    assert ajani["faces"][0]["oracle_text"].startswith(PARIAH_TEXT)
    assert ajani["faces"][1]["oracle_text"].startswith(AVENGER_TEXT)
    assert ajani["faces"][1]["type_line"] == "Legendary Planeswalker — Ajani"
    assert not matches({"types_any": ["Planeswalker"]}, ajani)
    assert not matches({"subtypes_any": ["Ajani"]}, ajani)
    assert matches({"types_all": ["Creature"], "subtypes_any": ["Cat"]}, ajani)
    assert not matches({"oracle_has": "+1/+1 counter on each Cat"}, ajani)
    assert not matches({"colors_any": ["red"]}, ajani)


def test_the_reported_exile_count_sees_no_planeswalker(client, game):
    add_card(client, game, "Ajani, Nacatl Pariah", "alice", "exile")
    view = state(client, game, _uid(game, "alice"))
    walkers = {
        "kind": "count",
        "zone": "exile",
        "side": "me",
        "op": ">=",
        "n": 1,
        "select": {"types_any": ["Planeswalker"]},
    }
    cats = {**walkers, "select": {"subtypes_any": ["Cat"]}}
    result = evaluate(play(true=[walkers], false=[]), view, "alice")
    assert (result["applies"], result["seen"]) == (False, 0)
    assert evaluate(play(true=[cats], false=[]), view, "alice")["applies"] is True


def test_transformed_ajani_is_the_avenger_with_his_text(client, game):
    add_card(client, game, "Ajani, Nacatl Pariah", "alice", "battlefield")
    _transform(game, AJANI)
    avenger = _in(state(client, game, _uid(game, "alice")), "battlefield", AJANI)

    assert avenger["name"] == "Ajani, Nacatl Avenger"
    assert avenger["oracle_text"] is not None
    assert avenger["oracle_text"].startswith(AVENGER_TEXT)
    assert len(avenger["faces"]) == 2
    assert matches({"types_any": ["Planeswalker"], "subtypes_any": ["Ajani"]}, avenger)
    assert not matches({"types_any": ["Creature"]}, avenger)
    assert matches({"oracle_has": "+1/+1 counter on each Cat"}, avenger)
    assert not matches({"oracle_has": "When Ajani enters"}, avenger), "the Pariah's text is gone"
    assert matches({"colors_any": ["red"]}, avenger)
    assert avenger["mana_value"] == 2, "CR 712.8e: the front face's mana cost"


def test_ajani_front_face_up_on_the_battlefield_is_the_pariah(client, game):
    add_card(client, game, "Ajani, Nacatl Pariah", "alice", "battlefield")
    pariah = _in(state(client, game, _uid(game, "alice")), "battlefield", AJANI)

    assert pariah["oracle_text"].startswith(PARIAH_TEXT)
    assert matches({"types_any": ["Creature"]}, pariah)
    assert not matches({"types_any": ["Planeswalker"]}, pariah)


def test_ajani_in_the_library_contents_reads_his_front_face(client, game):
    add_card(client, game, "Ajani, Nacatl Pariah", "alice", "library")
    response = client.get(
        f"/api/games/{game}", params={"uid": _uid(game, "alice"), "include": "contents"}
    )
    assert response.status_code == 200, response.text
    entry = next(
        entry
        for entry in response.json()["library_contents"]["alice"]
        if entry["card"]["card_name"] == AJANI
    )
    assert entry["card"]["oracle_text"].startswith(PARIAH_TEXT)
    assert not matches({"types_any": ["Planeswalker"]}, entry["card"])


def test_a_second_transforming_card_reads_its_face_in_each_zone(client, game):
    add_card(client, game, "Delver of Secrets", "alice", "exile")
    add_card(client, game, "Delver of Secrets", "alice", "battlefield")
    _transform(game, DELVER)
    view = state(client, game, _uid(game, "alice"))

    exiled = _in(view, "exile", DELVER)
    assert matches({"subtypes_any": ["Wizard"]}, exiled)
    assert not matches({"subtypes_any": ["Insect"]}, exiled)
    assert not matches({"keywords_any": ["Flying"], "oracle_has": "Flying"}, exiled)

    aberration = _in(view, "battlefield", DELVER)
    assert aberration["name"] == "Insectile Aberration"
    assert aberration["oracle_text"] == "Flying"
    assert matches({"subtypes_any": ["Insect"], "oracle_has": "flying"}, aberration)
    assert not matches({"subtypes_any": ["Wizard"]}, aberration)
