"""Rebound (CR 702.88a), end to end through the real API: the same
`Game.cast_during_resolution` funnel cascade's hit and suspend's free cast go
through (battle 8), reached this time by a delayed trigger at the caster's own
next upkeep rather than by a spell trigger's immediate resolution.

Ephemerate (Marvel Universe printing, verified with `resolve_cards`): "Exile
target creature you control, then return it to the battlefield under its
owner's control. Rebound. (If you cast this spell from your hand, exile it as
it resolves. At the beginning of your next upkeep, you may cast this card
from exile without paying its mana cost.)" Cast from hand, it resolves into
exile instead of the graveyard; at alice's next upkeep, the delayed trigger
asks the same `may` any other cast-during-resolution effect asks, and
accepting it recasts Ephemerate immediately, asking its own new target
(CR 601.2c) the same way any other mid-resolution cast does - no separate
`actions/cast`, and nothing left in `play_permissions` to drive by hand.
"""

from __future__ import annotations

from conftest import add_card, find, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step

from magic_cards_model_api.store import store


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def test_ephemerates_rebound_recasts_at_the_next_upkeep_through_decide(client, game):
    add_card(client, game, "Ephemerate", "alice", "hand")
    add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    uids = seats_for(client, game)
    board = state(client, game)
    plains = find(board["battlefield"], "Plains")["id"]
    bears = find(board["battlefield"], "Grizzly Bears")["id"]
    ephemerate = state(client, game)["hands"]["alice"][0]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": ephemerate["id"],
            "mana_sources": [plains],
            "targets": [bears],
        },
    )
    assert cast.status_code == 200, cast.text
    assert cast.json()["allowed"] is True, cast.json()

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "Ephemerate"

    after_first = state(client, game)
    exiled = find(after_first["exile"], "Ephemerate")
    assert exiled["zone"] == "exile", "rebound exiles it instead of the graveyard (CR 702.88a)"
    assert after_first["graveyards"]["alice"] == []
    new_bears = find(after_first["battlefield"], "Grizzly Bears")
    assert after_first["play_permissions"]["alice"] == [], (
        "rebound's own recast is a delayed trigger, never a standing PlayPermission"
    )

    # Alice's own next upkeep: the same "force to UNTAP, then advance" idiom
    # `test_special_actions.py`'s suspend test uses to reach a real upkeep
    # without replaying the turn between.
    force_turn_position(game, Step.UNTAP, "alice")
    advanced = client.post(f"/api/games/{game}/advance", json={"player": "alice"}).json()
    assert advanced["advanced_to"] == "upkeep", advanced

    top = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]}).json()
    assert top.get("paused") is True, "rebound's own may pauses (CR 608.2g)"
    pending = top["pending_decision"]
    assert pending["kind"] == "may"
    assert pending["source"] == "Ephemerate"

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    body = answer.json()
    assert body["accepted"] is True and body.get("paused") is True, body
    pending = body["pending_decision"]
    assert pending["kind"] == "choose_targets", "the recast announces its own new target"

    retarget = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "targets": [new_bears["id"]]},
    )
    body = retarget.json()
    assert body["accepted"] is True
    assert body.get("paused") is not True, body

    mid = state(client, game)
    assert mid["exile"] == [], "no permission and no card left lying around after the recast"
    assert mid["play_permissions"]["alice"] == []
    assert [entry["name"] for entry in mid["stack"]] == ["Ephemerate"]

    resolved_again = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved_again["resolved"] == "Ephemerate"

    final = state(client, game)
    # Cast this second time from exile, not from hand, so CR 702.88a's own
    # "if you cast this spell from your hand" replacement never applies -
    # it lands in the graveyard for good, and does not rebound forever.
    assert find(final["graveyards"]["alice"], "Ephemerate")["zone"] == "graveyard"
    assert final["exile"] == []
    twice_blinked = find(final["battlefield"], "Grizzly Bears")
    assert twice_blinked["controller"] == "alice"
