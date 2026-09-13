"""D14 (battle 1): a modal double-faced card whose land face is being played
resolves by name, not only by id.

`play-land "Witch Enchanter"` was refused as "no land named Witch Enchanter in
hand" while the same card's id played Witch-Blessed Meadow. `locate_land`
compared the name against `Card.name` and required `Card.is_land`, which reads
only the front face (CR 712.8a) - so every MDFC with a land back face missed.
The fix reads the mechanic: any card with a land face (`Card.land_face`, the
engine's own CR 712.12 read) answers to its full name or to any of its faces'
names. Proven on the reported card through the seat CLI and on a different
MDFC (Valakut Awakening // Valakut Stoneforge) through the API.
"""

from __future__ import annotations

import httpx
from conftest import add_card, state
from fastapi.testclient import TestClient

from magic_cards_model_api import seat_cli
from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def _hand_card_id(client: TestClient, game: str, uid: str) -> str:
    hand = state(client, game, uid=uid)["hands"]["alice"]
    assert len(hand) == 1, hand
    return hand[0]["id"]


def _uids(game: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game).items()}


def _play_by_name(client: TestClient, game: str, name: str) -> dict:
    response = client.post(
        f"/api/games/{game}/actions/play-land", json={"player": "alice", "card": name}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_an_mdfc_plays_its_land_face_by_the_cards_front_face_name(client, game):
    add_card(client, game, "Valakut Awakening", "alice", "hand")
    uid = _uids(game)["alice"]
    card_id = _hand_card_id(client, game, uid)

    answer = _play_by_name(client, game, "Valakut Awakening")

    assert answer["allowed"] is True, answer
    board = state(client, game, uid=uid)
    assert card_id in [card["id"] for card in board["battlefield"]], board["battlefield"]
    assert board["hands"]["alice"] == []


def test_an_mdfc_plays_by_its_land_faces_own_name(client, game):
    add_card(client, game, "Valakut Awakening", "alice", "hand")
    uid = _uids(game)["alice"]
    card_id = _hand_card_id(client, game, uid)

    answer = _play_by_name(client, game, "Valakut Stoneforge")

    assert answer["allowed"] is True, answer
    assert card_id in [card["id"] for card in state(client, game, uid=uid)["battlefield"]]


def test_an_mdfc_plays_by_its_full_double_faced_name(client, game):
    add_card(client, game, "Valakut Awakening", "alice", "hand")
    uid = _uids(game)["alice"]
    card_id = _hand_card_id(client, game, uid)

    answer = _play_by_name(client, game, "Valakut Awakening // Valakut Stoneforge")

    assert answer["allowed"] is True, answer
    assert card_id in [card["id"] for card in state(client, game, uid=uid)["battlefield"]]


def test_a_name_with_no_land_face_behind_it_is_still_refused(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")

    answer = _play_by_name(client, game, "Lightning Bolt")

    assert answer["allowed"] is False, answer
    assert "no land named Lightning Bolt" in answer["reason"], answer


def test_the_seat_cli_plays_the_reported_mdfc_by_its_card_name(client, game, monkeypatch, capsys):
    """The battle-1 repro, through the command the report ran: Witch-Blessed
    Meadow asks alice to pay 3 life as it enters, so a play that resolved the
    name leaves that question pending for her - a refusal leaves nothing."""
    add_card(client, game, "Witch Enchanter", "alice", "hand")
    uid = _uids(game)["alice"]
    card_id = _hand_card_id(client, game, uid)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    rc = seat_cli.run(["--uid", uid, "--game", game, "play-land", "Witch Enchanter"])

    printed = capsys.readouterr().out
    assert rc == 0, printed
    assert "no land named" not in printed, printed
    pending = state(client, game, uid=uid)["pending_decision"]
    assert pending is not None, printed
    assert (pending["player"], pending["kind"]) == ("alice", "pay_life"), pending
    assert pending["source_card_id"] == card_id, pending
