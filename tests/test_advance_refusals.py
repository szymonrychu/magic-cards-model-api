"""`POST /advance` refusing rather than performing: a non-empty stack in a
step that grants priority (CR 500.2, 117.4), and a caller who is not the
active player or their CR 723.5 controller. Both are the engine's own
refusal, surfaced through the transport exactly like any other rules
refusal (`pass-priority`, `declare-blockers`, ...) - `allowed: False` and a
`reason`, not an HTTP error and not a silent no-op.

The scenario driving this - Flooded Strand's fetch ability stranded on the
stack when `advance` used to be allowed to skip past it - is proven at the
engine level in `magic-cards-model`'s
`tests/scenarios/test_advancing_the_turn.py`; this file only checks that the
API/transport layer reports the refusal instead of swallowing it."""

from __future__ import annotations

from conftest import add_card, state

from magic_cards_model_api.store import store


def test_advance_is_refused_while_the_stack_is_non_empty(client, game):
    """Bob's exact reported shape, one seat: activate an ability, refuse the
    advance that used to strand it, then resolve it the way it should.

    The fetch's own "did you find one?" question (CR 701.23a's search) now
    pauses `resolve-top` instead of blocking a thread for a live-decision
    provider's answer (`test_decisions.py`'s module docstring) - so this
    drives it the same way: read `pending_decision` off the paused response
    and resume with a real `POST .../decide` naming the Plains found.
    """
    uids = {name: uid for uid, name in store.seat_uids(game).items()}
    strand = add_card(client, game, "Flooded Strand", "alice", "battlefield")["added"]["card_id"]
    add_card(client, game, "Plains", "alice", "library")

    activated = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": strand},
    ).json()
    assert activated["allowed"] is True, activated

    response = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    body = response.json()

    assert body["allowed"] is False
    assert "non-empty" in body["reason"]
    assert "pass priority" in body["reason"]
    assert state(client, game)["step"] == "precombat_main"
    assert len(state(client, game)["stack"]) == 1

    # the way out the refusal names actually works, and nothing was lost
    resolved = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]}).json()
    assert resolved["paused"] is True, resolved
    pending = resolved["pending_decision"]
    assert pending["kind"] == "choose_cards"
    assert pending["source"] == "Flooded Strand"

    decided = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [pending["cards"][0]]},
    ).json()
    assert decided["accepted"] is True, decided

    assert state(client, game)["stack"] == []
    assert any(c["name"] == "Plains" for c in state(client, game)["battlefield"])


def test_advance_is_refused_for_a_non_active_player(client, game):
    response = client.post(f"/api/games/{game}/advance", json={"player": "bob"})
    body = response.json()

    assert body["allowed"] is False
    assert "may not advance the turn" in body["reason"]
    assert state(client, game)["step"] == "precombat_main"


def test_advance_still_works_for_the_active_player_with_an_empty_stack(client, game):
    response = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    body = response.json()

    assert body["allowed"] is True
    assert body["advanced_to"] == "beginning_of_combat"
