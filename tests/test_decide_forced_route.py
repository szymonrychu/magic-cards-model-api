"""`POST /api/games/{game_id}/decide-forced` - the speed-seat route that
answers a pending decision in-process, only when `forced_decisions` proves
it has exactly one legal answer, and through the exact same `decide` code
path `POST .../decide` itself uses.

Real boards through the real API, real Scryfall printings - the same
scenarios `test_forced_decisions.py` already proved forced/not-forced for,
reused here so this route is checked against known-good cases rather than
re-litigating what counts as forced.
"""

from __future__ import annotations

from conftest import add_card, state
from fastapi.testclient import TestClient
from test_autopass import a_bear_that_can_attack
from test_forced_decisions import (
    cast_devourer,
    logged_reasonings,
    pause_on_declare_attackers,
    play_steam_vents,
)

from magic_cards_model_api.forced_decisions import AUTO_REASONING_PREFIX
from magic_cards_model_api.store import store


def uid_for(game_id: str, player: str) -> str:
    uids = {name: uid for uid, name in store.seat_uids(game_id).items()}
    return uids[player]


def pending_generation(client: TestClient, game_id: str, uid: str) -> int:
    pending = state(client, game_id, uid)["pending_decision"]
    assert pending is not None, "expected a pending decision"
    return pending["generation"]


def decide_forced(client: TestClient, game_id: str, uid: str, player: str, generation: int):
    response = client.post(
        f"/api/games/{game_id}/decide-forced",
        params={"uid": uid},
        json={"player": player, "generation": generation},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_declare_attackers_with_nothing_able_to_attack_is_auto_answered(client, game):
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")  # summoning sick
    pause_on_declare_attackers(client, game)
    alice_uid = uid_for(game, "alice")
    generation = pending_generation(client, game, alice_uid)

    body = decide_forced(client, game, alice_uid, "alice", generation)

    assert body["forced"] is True, body
    assert body["accepted"] is True, body
    assert body["auto_answered"] == {
        "kind": "declare_attackers",
        "answer": {"pairs": []},
        "why": "no creature can legally attack, so declaring nobody",
    }
    after = state(client, game, alice_uid)
    assert (after.get("pending_decision") or {}).get("kind") != "declare_attackers"
    assert any(
        text.startswith(AUTO_REASONING_PREFIX) and "no creature can legally attack" in text
        for text in logged_reasonings(client, game, alice_uid)
    ), logged_reasonings(client, game, alice_uid)


def test_pay_life_that_cannot_be_paid_is_auto_answered_no(client, game):
    play_steam_vents(client, game, life=1)
    alice_uid = uid_for(game, "alice")
    generation = pending_generation(client, game, alice_uid)

    body = decide_forced(client, game, alice_uid, "alice", generation)

    assert body["forced"] is True, body
    assert body["accepted"] is True, body
    assert body["auto_answered"]["kind"] == "pay_life"
    assert body["auto_answered"]["answer"] == {"accept": False}
    after = state(client, game, alice_uid)
    assert (after.get("pending_decision") or {}) == {} or after["pending_decision"] is None


def test_a_trigger_with_one_legal_target_is_auto_answered(client, game):
    (bear,) = cast_devourer(client, game, colored_targets=1)
    alice_uid = uid_for(game, "alice")
    generation = pending_generation(client, game, alice_uid)

    body = decide_forced(client, game, alice_uid, "alice", generation)

    assert body["forced"] is True, body
    assert body["accepted"] is True, body
    assert body["auto_answered"]["kind"] == "choose_targets"
    assert body["auto_answered"]["answer"] == {"targets": [bear]}
    assert body["auto_answered"]["why"].startswith("exactly one legal choice")


def test_a_real_choice_is_not_forced_and_changes_nothing(client, game):
    a_bear_that_can_attack(client, game, "alice")
    pause_on_declare_attackers(client, game)
    alice_uid = uid_for(game, "alice")
    generation = pending_generation(client, game, alice_uid)
    before = state(client, game, alice_uid)

    body = decide_forced(client, game, alice_uid, "alice", generation)

    assert body == {"forced": False}
    after = state(client, game, alice_uid)
    assert after["pending_decision"] == before["pending_decision"]
    assert pending_generation(client, game, alice_uid) == generation


def test_a_trigger_with_two_legal_targets_is_not_forced(client, game):
    cast_devourer(client, game, colored_targets=2)
    alice_uid = uid_for(game, "alice")
    generation = pending_generation(client, game, alice_uid)

    body = decide_forced(client, game, alice_uid, "alice", generation)

    assert body == {"forced": False}


def test_stale_generation_is_refused_and_changes_nothing(client, game):
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    pause_on_declare_attackers(client, game)
    alice_uid = uid_for(game, "alice")
    generation = pending_generation(client, game, alice_uid)

    body = decide_forced(client, game, alice_uid, "alice", generation + 1)

    assert body["forced"] is False
    assert "generation" in (body.get("reason") or "")
    after = state(client, game, alice_uid)
    assert after["pending_decision"]["kind"] == "declare_attackers"
    assert after["pending_decision"]["generation"] == generation


def test_no_pending_decision_is_not_forced(client, game):
    alice_uid = uid_for(game, "alice")

    body = decide_forced(client, game, alice_uid, "alice", 0)

    assert body["forced"] is False
    assert body.get("reason") is not None


def test_wrong_uid_is_refused_without_leaking_anything(client, game):
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    pause_on_declare_attackers(client, game)
    alice_uid = uid_for(game, "alice")
    bob_uid = uid_for(game, "bob")
    generation = pending_generation(client, game, alice_uid)

    body = decide_forced(client, game, bob_uid, "alice", generation)

    assert body["forced"] is False
    assert "does not open" in (body.get("reason") or "")
    after = state(client, game, alice_uid)
    assert after["pending_decision"]["kind"] == "declare_attackers"
