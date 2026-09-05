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

import threading
import time

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
    # and mode 2's scry can raise a `pending_decision` that parks this call
    # inside `Game.ask` - so it runs in the background the way
    # `tests/test_ui_e2e.py` and `tests/test_decision_target_minimum.py` do,
    # and gets answered through the same `POST /decide` `ui/app.js` calls.
    result: dict = {}

    def bob_passes() -> None:
        result["response"] = client.post(
            f"/api/games/{game}/actions/pass-priority", json={"player": "bob"}
        )

    thread = threading.Thread(target=bob_passes, daemon=True)
    thread.start()
    thread.join(timeout=0.5)

    if thread.is_alive():
        alice_uid = {name: uid for uid, name in store.seat_uids(game).items()}["alice"]
        pending = None
        for _ in range(300):
            pending = state(client, game, alice_uid).get("pending_decision")
            if pending is not None:
                break
            time.sleep(0.01)
        assert pending is not None, "bob's pass blocked but no decision showed up"
        assert pending["kind"] == "scry", pending
        assert pending["player"] == "alice", pending

        decide = client.post(
            f"/api/games/{game}/decide",
            params={"uid": alice_uid},
            json={"player": "alice", "moved": [], "top_order": None},
        )
        assert decide.json()["accepted"] is True, decide.json()

        thread.join(timeout=5)

    assert not thread.is_alive(), "bob's pass never returned after the scry was answered"
    assert result["response"].json()["outcome"] == "resolve_top", result["response"].json()

    board = client.get(f"/api/games/{game}").json()
    spawn = [c for c in board["battlefield"] if "Eldrazi Spawn" in (c["name"] or "")]
    assert len(spawn) == 1

    alice = next(p for p in board["players"] if p["username"] == "alice")
    # Hand emptied by the cast, then mode 2's draw put one card back in it.
    assert alice["hand_count"] == 1
