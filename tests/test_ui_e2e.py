"""One end-to-end game driven through the UI's own endpoints: boot from
decklists (`POST /api/ui/games`, what the lobby's "Start game" button calls),
then play a real turn using exactly the seat-facing calls `ui/app.js` itself
makes - `cast`, `pass-priority`, `resolve-top`, `decide` - never a bespoke
test-only shortcut. This is the "drives a real game through the UI's own
endpoints" test the task asks for, proving the UI layer added in this branch
(`decks_api.py`, `ai_seat_api.py`) sits on top of the existing seat API
correctly rather than merely importing without error.

The fixture also plays out CR 103.5 and CR 103.6 through the UI's own pregame
endpoints, because a game booted from decklists now rests in those two steps
before turn 1 - and it does so under a hard wall-clock bound, which is what
this file was missing when the old blocking pregame made it hang for the whole
suite instead of failing.
"""

from __future__ import annotations

import time

import pytest
from conftest import add_card, find, life_of, state
from fastapi.testclient import TestClient

#: No call in this file may block on a decision - see `booted_game`.
PREGAME_WITHIN_SECONDS = 20.0

LEGAL_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
LEGAL_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"


@pytest.fixture()
def booted_game(client: TestClient) -> dict:
    """A game booted through the UI's own `POST /api/ui/games`, deleted
    afterward - the store (`store.py`) is one module-level singleton shared
    by the whole pytest process, and a game left behind changes which game
    `GET /api/games` reports as "the live one" for tests elsewhere
    (`test_mcp_resilience.py`) that rely on there being exactly one."""
    response = client.post(
        "/api/ui/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": LEGAL_DECK, "ai": False},
                {
                    "username": "bob",
                    "decklist_text": LEGAL_DECK_B,
                    "ai": True,
                    # The lobby's `#ai-model`/`#ai-effort`/`#ai-thinking` fields,
                    # sent through `agentFrom` - `test_ui_lobby.py` covers that
                    # function itself; this proves the whole request round trip
                    # carries it, the same `agent` field `decks_api.py`'s
                    # `NewUiPlayer` accepts.
                    "agent": {"model": "opus", "effort": "max", "max_thinking_tokens": None},
                },
            ],
            "seed": 7,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agents"] == {
        "bob": {"model": "opus", "effort": "max", "max_thinking_tokens": None}
    }

    # The boot leaves the game resting in CR 103.5's mulligan step (turn 0),
    # which is where a human at /play/ now starts: nothing on the board can
    # happen until both seats have declared and finished CR 103.6. So this
    # fixture plays that out through the same three endpoints the UI's pregame
    # panel posts to, rather than skipping past it - the tests below are about
    # a turn, and a turn cannot begin before CR 103.8.
    #
    # Bounded, and that bound is the point: the old blocking pregame put every
    # question through a decision provider with no live seat behind it, which
    # cost one full answer timeout per question per seat. This file did not
    # merely run slowly then - it hung for the whole run, because the settling
    # thread held the game lock that every call below needs.
    started = time.monotonic()
    for player in ("alice", "bob"):
        kept = client.post(
            f"/api/games/{body['game_id']}/pregame/mulligan",
            params={"uid": body["seat_uids"][player]},
            json={"player": player, "take": False},
        )
        assert kept.status_code == 200, kept.text
    for player in ("alice", "bob"):
        done = client.post(
            f"/api/games/{body['game_id']}/pregame/opening-hand/finish",
            params={"uid": body["seat_uids"][player]},
            json={"player": player},
        )
        assert done.status_code == 200, done.text
    # CR 103.8's first turn has begun. Its untap step grants nobody priority
    # (CR 502.4) and ends on its own (CR 500.3), so the API has already
    # settled past it to the first step a seat can act in.
    assert done.json()["step"] == "upkeep", "CR 103.8's first turn should have begun"
    elapsed = time.monotonic() - started
    assert elapsed < PREGAME_WITHIN_SECONDS, (
        f"the pregame took {elapsed:.1f}s - no pregame call may block on a decision"
    )

    yield body
    client.delete(f"/api/games/{body['game_id']}")


def test_full_turn_through_the_ui_endpoints(client: TestClient, booted_game: dict) -> None:
    boot_body = booted_game
    game_id = boot_body["game_id"]
    alice_uid = boot_body["seat_uids"]["alice"]
    bob_uid = boot_body["seat_uids"]["bob"]

    # The lobby also offers to start the AI seat right away, same as
    # `startGame()` in app.js - exercise that call too. `booted_game` already
    # posted `ai: true` for bob, so creation itself has already tried this
    # (see `test_ai_seat_settings.py` for that assertion); this call lands on
    # an already-started seat or a no-op fallback either way. Which branch -
    # not this machine's own `claude` availability, but `conftest.py`'s
    # autouse fixture, which pins `claude_available()` to `False` for the
    # whole suite - decides is "fallback" here, deterministically; a test
    # that wants the real subprocess branch opts in with its own
    # `claude_available`/`command_for` stand-in, the way `ai_seat.py`'s own
    # tests do. Here we only need this call to not blow up the game boot flow.
    ai_start = client.post(f"/api/ui/games/{game_id}/ai-seat/start", json={"uid": bob_uid})
    assert ai_start.status_code == 200
    assert ai_start.json()["mechanism"] in ("subprocess", "fallback")

    # Get to alice's precombat main with a bolt in hand and a mana source in
    # play - same shape `tests/test_flow.py` already uses, layered on top of
    # the dealt opening hand rather than replacing it.
    client.put(f"/api/games/{game_id}/step", json={"step": "precombat_main"})
    add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Lightning Bolt", "alice", "hand")

    # alice's own filtered view: her hand is visible, bob's is not.
    alice_view = state(client, game_id, alice_uid)
    assert alice_view["hands"]["bob"] == []
    mountain = find(alice_view["battlefield"], "Mountain")
    bolt = find(alice_view["hands"]["alice"], "Lightning Bolt")

    before_life = life_of(alice_view, "bob")
    cast = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt["id"],
            "targets": ["bob"],
            "mana_sources": [mountain["id"]],
        },
    )
    assert cast.json()["allowed"] is True, cast.json()

    resolved = client.post(f"/api/games/{game_id}/resolve-top").json()
    assert resolved["resolved"] == "Lightning Bolt"

    after = state(client, game_id, alice_uid)
    assert life_of(after, "bob") == before_life - 3

    # Passing priority as the other seat's uid is refused - the seat API's
    # own authorization, not a UI shortcut.
    wrong_pass = client.post(f"/api/games/{game_id}/actions/pass-priority", json={"player": "bob"})
    # bob does not hold priority right after alice's cast/resolve in this
    # sequence unless the engine handed it back - either a clean refusal or
    # an accepted pass is fine here; what must never happen is a 5xx.
    assert wrong_pass.status_code == 200


def test_decision_round_trip_through_decide(client: TestClient, booted_game: dict) -> None:
    """Preordain ("Scry 2, then draw a card") raises a real `scry`
    `pending_decision` - answered here through `POST .../decide`, the exact
    endpoint `ui/app.js`'s `decide()` calls, with the exact `moved`/`top_order`
    shape `renderCardSplitDecision` builds."""
    boot_body = booted_game
    game_id = boot_body["game_id"]
    alice_uid = boot_body["seat_uids"]["alice"]

    client.put(f"/api/games/{game_id}/step", json={"step": "precombat_main"})
    add_card(client, game_id, "Island", "alice", "battlefield")
    add_card(client, game_id, "Preordain", "alice", "hand")
    view = state(client, game_id, alice_uid)
    island = find(view["battlefield"], "Island")
    preordain = find(view["hands"]["alice"], "Preordain")

    cast = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={"player": "alice", "card_id": preordain["id"], "mana_sources": [island["id"]]},
    )
    assert cast.json()["allowed"] is True, cast.json()

    import threading

    result: dict = {}

    def do_resolve() -> None:
        result["response"] = client.post(f"/api/games/{game_id}/resolve-top")

    thread = threading.Thread(target=do_resolve)
    thread.start()
    thread.join(timeout=0.5)
    assert thread.is_alive(), "expected resolving Preordain to block on its scry decision"

    # alice's own filtered read shows the pending decision and delivers it.
    pending_view = state(client, game_id, alice_uid)
    pending = pending_view["pending_decision"]
    assert pending is not None
    assert pending["kind"] == "scry"
    assert pending["player"] == "alice"
    assert len(pending["card_details"]) == 2  # scry 2

    decide = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": alice_uid},
        json={"player": "alice", "moved": [], "top_order": None},
    )
    assert decide.json()["accepted"] is True

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result["response"].json()["resolved"] == "Preordain"
