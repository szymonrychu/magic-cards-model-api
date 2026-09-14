"""`GET /api/games/{game_id}/playscripts/hypothetical-facts`: the server
endpoint a seat calls when its play's selectors need O4/O7 facts a redacted
view alone cannot answer - see `hypothetical_facts.py`."""

from __future__ import annotations

from conftest import add_card

from magic_cards_model_api.store import store


def _uid(game_id: str, player: str) -> str:
    return next(uid for uid, name in store.seat_uids(game_id).items() if name == player)


def test_returns_enters_and_unlocks_for_the_callers_own_hand_and_library(client, game):
    island = add_card(client, game, "Island", "alice", "hand")["added"]["card_id"]
    add_card(client, game, "Preordain", "alice", "hand")
    uid = _uid(game, "alice")

    response = client.get(f"/api/games/{game}/playscripts/hypothetical-facts", params={"uid": uid})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cards"][island]["enters"] == "untapped"
    assert body["cards"][island]["unlocks"] is True


def test_never_computes_anything_about_the_opponents_hand_or_library(client, game):
    add_card(client, game, "Island", "bob", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    uid = _uid(game, "alice")

    body = client.get(
        f"/api/games/{game}/playscripts/hypothetical-facts", params={"uid": uid}
    ).json()
    bob_island = str(next(c.id for c in store.get(game).player("bob").hand if c.name == "Island"))
    assert bob_island not in body["cards"]


def test_refuses_a_uid_with_no_seat(client, game):
    response = client.get(
        f"/api/games/{game}/playscripts/hypothetical-facts", params={"uid": "not-a-real-uid"}
    )
    assert response.status_code == 404


def test_refuses_with_no_uid_at_all(client, game):
    response = client.get(f"/api/games/{game}/playscripts/hypothetical-facts")
    assert response.status_code == 404
