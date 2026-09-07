"""The exact calls `ui/app.js` makes for Kozilek's Command choosing modes 1
and 2 with the caster as both targets (CR 115.3: the same target can't be
chosen twice for one instance of the word "target", but each mode is its own
instance, so alice is a legal target for both mode 1's token maker and mode
2's scry-then-draw).

End to end through the HTTP API, not the engine directly:

    affordance (modes, X)  ->  dry-run cast (tapped preview)  ->  real cast
    ->  target_refs on the stack  ->  both players pass priority  ->  the
    spell resolves for real (an Eldrazi Spawn token, a scry, a draw)

No mocks: a real Kozilek's Command, Wastes, and a library card, all real
Scryfall printings out of the engine's `tests/fixtures/cards` pool, over the
real engine underneath the real API.
"""

from __future__ import annotations

from conftest import add_card, card_id, state
from fastapi.testclient import TestClient

from magic_cards_model_api.store import store

KOZILEK = "Kozilek's Command"


def test_same_player_for_both_modes_casts_and_resolves(client: TestClient, game: str) -> None:
    command = card_id(client, game, KOZILEK, "alice", "hand")
    for _ in range(3):
        add_card(client, game, "Wastes", "alice", "battlefield")
    # The harness boots alice with an empty library, so mode 2's "then draws
    # a card" would draw from nothing and lose her the game at the next
    # state-based check. One library card, on top by default, is both what
    # the scry looks at and what the draw takes.
    add_card(client, game, "Island", "alice", "library")
    lands = [card["id"] for card in state(client, game)["battlefield"] if card["name"] == "Wastes"]
    assert len(lands) == 3

    aff = client.get(
        f"/api/games/{game}/affordances/{command}",
        params={"player": "alice", "modes": "1,2", "x_value": 1},
    ).json()
    assert [r["mode"] for r in aff["requirements"]] == [1, 2]

    body = {
        "player": "alice",
        "card_id": command,
        "targets": ["alice", "alice"],
        "modes": [1, 2],
        "x_value": 1,
        "mana_sources": lands,
    }
    probe = client.post(f"/api/games/{game}/actions/cast", json={**body, "dry_run": True}).json()
    assert probe["allowed"] is True, probe
    assert len(probe["tapped"]) == 3

    res = client.post(f"/api/games/{game}/actions/cast", json=body).json()
    assert res["allowed"] is True, res

    (spell,) = client.get(f"/api/games/{game}").json()["stack"]
    assert spell["target_refs"] == [{"kind": "player", "id": "alice", "name": "alice"}] * 2

    alice_passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    assert alice_passed.json()["allowed"] is True, alice_passed.json()

    # bob's pass is the one that resolves the top of the stack (CR 117.4),
    # and mode 2's scry can raise a `pending_decision` - `pass-priority` now
    # pauses on it and returns immediately, the question attached, rather
    # than blocking a thread inside `Game.ask` for an answer to land on
    # (`test_decisions.py`'s module docstring), so this drives it
    # synchronously, the same way `ui/app.js` does.
    bob_response = client.post(
        f"/api/games/{game}/actions/pass-priority", json={"player": "bob"}
    ).json()

    if bob_response.get("paused"):
        alice_uid = {name: uid for uid, name in store.seat_uids(game).items()}["alice"]
        pending = bob_response["pending_decision"]
        assert pending["kind"] == "scry", pending
        assert pending["player"] == "alice", pending

        decide = client.post(
            f"/api/games/{game}/decide",
            params={"uid": alice_uid},
            json={"player": "alice", "moved": [], "top_order": None},
        )
        assert decide.json()["accepted"] is True, decide.json()
        # `decide()`'s own response carries no `outcome` field to check
        # directly the way `pass_priority`'s own response does in the
        # `else` branch below - resuming a paused command never surfaces
        # the underlying command's outcome over HTTP. The audit log's own
        # entry for *this* call is the equivalent proof: bob's `pass_priority`
        # paused with the stack still holding Kozilek's Command (the log
        # entry above this one), so a stack of 0 and the spell sitting in
        # alice's graveyard on the entry *this* `decide` call itself wrote
        # can only mean this exact call was the one that resolved it via
        # CR 117.4's `resolve_top` - not board state merely observed to be
        # settled by the time some later, unrelated read happens to check it.
        log_entry = client.get(f"/api/games/{game}/log").json()["log"][-1]
        assert log_entry["action"] == "decide", log_entry
        assert log_entry["stack_size"] == 0, log_entry
        assert "Kozilek's Command" in log_entry["snapshot"]["graveyards"]["alice"], log_entry
    else:
        assert bob_response["outcome"] == "resolve_top", bob_response

    board = client.get(f"/api/games/{game}").json()
    assert board["stack"] == [], "the spell resolved - nothing should be left on the stack"

    spawn = [
        c
        for c in board["battlefield"]
        if "Eldrazi Spawn" in (c["name"] or "") and c["controller"] == "alice"
    ]
    assert len(spawn) == 1

    alice = next(p for p in board["players"] if p["username"] == "alice")
    # Hand emptied by the cast, then mode 2's draw put one card back in it.
    assert alice["hand_count"] == 1
