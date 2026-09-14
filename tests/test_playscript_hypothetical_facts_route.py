"""`GET /api/games/{game_id}/playscripts/hypothetical-facts`: the server
endpoint a seat calls when its play's selectors need O4/O7 facts a redacted
view alone cannot answer - see `hypothetical_facts.py`."""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, state

from magic_cards_model_api.store import store


def _uid(game_id: str, player: str) -> str:
    return next(uid for uid, name in store.seat_uids(game_id).items() if name == player)


def _activate_and_resolve(client: Any, game_id: str, uid: str, card_id: str) -> dict[str, Any]:
    """Activate a battlefield ability and resolve it, same idiom
    `test_fetchland_search_choice.py`'s `activate_and_resolve` uses: the
    search's own `choose_cards` question is raised on resolution (CR
    608.2d), so `resolve-top` is what returns it, `paused: True` and
    `pending_decision` set."""
    activated = client.post(
        f"/api/games/{game_id}/actions/activate",
        json={"player": "alice", "card_id": card_id, "ability_index": 0},
    )
    assert activated.status_code == 200, activated.text
    resolved = client.post(f"/api/games/{game_id}/resolve-top", params={"uid": uid})
    assert resolved.status_code == 200, resolved.text
    return resolved.json()


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


# -- candidate_ids: only a pending choose_cards decision's own offers -------------


def test_a_pending_search_exposes_facts_only_for_its_own_offered_library_lands(client, game):
    """The end-to-end shape of the fix: Arid Mesa's search offers Mountain and
    Sacred Foundry (both "a Mountain or Plains card"), never Island (neither).
    Once that `choose_cards` decision is pending, the route must expose
    `enters`/`unlocks` for the two it offered and nothing for the one it did
    not - even though Island sits in the same library."""
    add_card(client, game, "Arid Mesa", "alice", "battlefield")
    add_card(client, game, "Ragavan, Nimble Pilferer", "alice", "hand")
    add_card(client, game, "Mountain", "alice", "library")
    add_card(client, game, "Sacred Foundry", "alice", "library")
    add_card(client, game, "Island", "alice", "library")
    uid = _uid(game, "alice")
    mesa = find(state(client, game)["battlefield"], "Arid Mesa")["id"]

    resolved = _activate_and_resolve(client, game, uid, mesa)
    assert resolved.get("paused") is True, resolved
    pending = resolved["pending_decision"]
    assert pending["kind"] == "choose_cards", pending
    mountain_id = next(
        card_id
        for card_id, name in zip(pending["cards"], pending["card_labels"], strict=True)
        if name == "Mountain"
    )
    offered = set(pending["cards"])
    library_ids = {str(c.id) for c in store.get(game).player("alice").library}
    unoffered = library_ids - offered
    assert unoffered, "Island must be in the library but never a legal find"

    body = client.get(
        f"/api/games/{game}/playscripts/hypothetical-facts", params={"uid": uid}
    ).json()

    assert mountain_id in body["cards"]
    assert body["cards"][mountain_id]["unlocks"] is True, "Mountain alone pays Ragavan's R"
    for card_id in unoffered:
        assert card_id not in body["cards"], (
            f"{card_id} was never offered by the pending decision and must stay invisible"
        )


def test_no_pending_decision_exposes_no_library_land_facts_at_all(client, game):
    """With nothing pending for the viewer, the route falls back to exactly
    the old behaviour: no library land's facts ever reach the response."""
    add_card(client, game, "Arid Mesa", "alice", "battlefield")
    add_card(client, game, "Ragavan, Nimble Pilferer", "alice", "hand")
    add_card(client, game, "Mountain", "alice", "library")
    uid = _uid(game, "alice")

    body = client.get(
        f"/api/games/{game}/playscripts/hypothetical-facts", params={"uid": uid}
    ).json()

    mountain_id = str(
        next(c.id for c in store.get(game).player("alice").library if c.name == "Mountain")
    )
    assert mountain_id not in body["cards"]


def test_a_pending_decision_for_a_different_kind_exposes_no_library_land_facts(client, game):
    """A pending decision that is not `choose_cards` at all (here: `pay_life`,
    a shockland's own ETB prompt, raised while the search's own find is still
    settling) must not be read as license to expose anything either - only a
    `choose_cards` decision's own `cards` ever seed `candidate_ids`."""
    add_card(client, game, "Arid Mesa", "alice", "battlefield")
    add_card(client, game, "Ragavan, Nimble Pilferer", "alice", "hand")
    add_card(client, game, "Sacred Foundry", "alice", "library")
    uid = _uid(game, "alice")
    mesa = find(state(client, game)["battlefield"], "Arid Mesa")["id"]

    resolved = _activate_and_resolve(client, game, uid, mesa)
    pending = resolved["pending_decision"]
    assert pending["kind"] == "choose_cards", pending
    foundry_id = pending["cards"][0]
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uid},
        json={"player": "alice", "card_ids": [foundry_id]},
    )
    assert answered.status_code == 200, answered.text
    settling = answered.json()
    assert settling.get("paused") is True, settling
    assert settling["pending_decision"]["kind"] == "pay_life", settling["pending_decision"]

    body = client.get(
        f"/api/games/{game}/playscripts/hypothetical-facts", params={"uid": uid}
    ).json()

    assert foundry_id not in body["cards"], (
        "a pay_life decision is not choose_cards, so it must not surface any library facts"
    )
