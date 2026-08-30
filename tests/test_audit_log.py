"""The audit log: every mutating action lands in it, refusals included, with
the engine's own reason verbatim - and the console logger sees one line each."""

from __future__ import annotations

from datetime import datetime

from conftest import add_card, find, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import PriorityOutcome


def log_of(client: TestClient, game: str) -> list[dict]:
    response = client.get(f"/api/games/{game}/log")
    assert response.status_code == 200, response.text
    return response.json()["log"]


def entry_of(log: list[dict], action: str) -> dict:
    matches = [entry for entry in log if entry["action"] == action]
    assert matches, f"no {action} in {[entry['action'] for entry in log]}"
    return matches[-1]


def test_successful_cast_is_recorded_with_step_and_order(client, game):
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    board = state(client, game)
    forest = find(board["battlefield"], "Forest")
    mountain = find(board["battlefield"], "Mountain")

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Grizzly Bears",
            "mana_sources": [{"card_id": mountain["id"]}, {"card_id": forest["id"]}],
        },
    )
    assert cast.json()["allowed"] is True

    log = log_of(client, game)
    entry = entry_of(log, "cast")
    assert entry["detail"]["player"] == "alice"
    assert entry["detail"]["allowed"] is True
    assert entry["detail"]["card"] == "Grizzly Bears"
    assert entry["step"] == "precombat_main"
    assert entry["phase"] == "precombat_main"
    assert entry["turn"] == 1
    # the spell is on the stack as the entry is written
    assert entry["stack_size"] == 1
    # a real timestamp, and the whole log is a numbered sequence, oldest first
    assert datetime.fromisoformat(entry["ts"])
    assert [e["seq"] for e in log] == list(range(1, len(log) + 1))
    assert log[0]["action"] == "create_game"


def test_refused_cast_records_the_engine_reason_verbatim(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = state(client, game)["hands"]["alice"][0]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": bolt["id"], "targets": ["bob"]},
    )
    body = cast.json()
    assert body["allowed"] is False

    entry = entry_of(log_of(client, game), "cast")
    assert entry["detail"]["allowed"] is False
    assert entry["detail"]["reason"] == body["reason"]
    assert body["reason"] == "pool {} cannot pay {R}"


def test_play_land_by_an_unresolvable_name_is_still_audited(client, game):
    """The regression for the silent-pass bug: a `play-land` naming a card
    that is not in hand used to raise before `action_play_land` ever reached
    `audit_outcome`, so nothing landed in the log at all - a play a player
    believed happened left no trace to contradict them. It must now be
    refused and audited exactly like any other illegal `play-land`.
    """
    response = client.post(
        f"/api/games/{game}/actions/play-land",
        json={"player": "alice", "card": "Not A Real Card"},
    )
    assert response.json()["allowed"] is False

    entry = entry_of(log_of(client, game), "play_land")
    assert entry["detail"]["allowed"] is False
    assert entry["detail"]["reason"] == response.json()["reason"]
    assert entry["detail"]["card"] == "Not A Real Card"


def test_step_put_records_from_and_to(client, game):
    response = client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    assert response.status_code == 200

    entry = entry_of(log_of(client, game), "step")
    assert entry["detail"]["from"] == "precombat_main"
    assert entry["detail"]["to"] == "beginning_of_combat"
    assert entry["detail"]["player"] is None, "an anonymous caller is logged as such"


def test_advance_records_from_and_to(client, game):
    advanced = client.post(f"/api/games/{game}/advance", json={"player": "alice"}).json()
    assert advanced["advanced_to"] == "beginning_of_combat"

    entry = entry_of(log_of(client, game), "advance")
    assert entry["detail"]["from"] == "precombat_main"
    assert entry["detail"]["to"] == "beginning_of_combat"

    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    entry = entry_of(log_of(client, game), "advance")
    assert entry["detail"]["player"] == "alice", "the actor is logged when given"


def test_log_endpoint_order_and_unknown_game_404(client, game):
    add_card(client, game, "Mountain", "alice", "hand")
    log = log_of(client, game)
    assert [entry["seq"] for entry in log] == list(range(1, len(log) + 1))
    assert [entry["action"] for entry in log] == ["create_game", "add_card"]

    missing = client.get("/api/games/no-such-game/log")
    assert missing.status_code == 404


def test_pass_priority_outcomes(client, game):
    # alice holds priority at game start; bob passing first is refused with the
    # engine's own wording, and the refusal is logged
    refused = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert refused.json()["allowed"] is False
    assert "does not have priority" in refused.json()["reason"]

    first = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    first_body = first.json()
    assert isinstance(first_body.pop("log_seq"), int)  # the audit entry's own idempotency marker
    assert first_body == {
        "allowed": True,
        "reason": None,
        "outcome": PriorityOutcome.PASSED.value,
    }
    assert state(client, game)["priority_holder"] == "bob"

    # two passes in succession on an empty stack end the step - and the game
    # rolls to the next one on its own (CR 117.4, 405.5); nobody calls advance
    second = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    second_body = second.json()
    assert isinstance(second_body.pop("log_seq"), int)
    assert second_body == {
        "allowed": True,
        "reason": None,
        "outcome": PriorityOutcome.END_STEP.value,
    }
    assert state(client, game)["step"] == "beginning_of_combat"

    log = log_of(client, game)
    passes = [entry for entry in log if entry["action"] == "pass_priority"]
    assert passes[0]["detail"]["allowed"] is False
    assert passes[0]["detail"]["reason"] == refused.json()["reason"]
    assert passes[1]["detail"]["outcome"] == PriorityOutcome.PASSED.value
    assert passes[2]["detail"]["outcome"] == PriorityOutcome.END_STEP.value


def test_declare_blockers(client, game):
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    bob_bears = add_card(client, game, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    board = state(client, game)
    forest = find(board["battlefield"], "Forest")
    mountain = find(board["battlefield"], "Mountain")

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Grizzly Bears",
            "mana_sources": [{"card_id": mountain["id"]}, {"card_id": forest["id"]}],
        },
    )
    assert cast.json()["allowed"] is True
    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "Grizzly Bears"
    alice_bears = cast.json()["spell_id"]  # two bears on the battlefield: use ids, not names

    client.patch(f"/api/games/{game}/cards/{alice_bears}", json={"summoning_sick": False})
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})  # declare_attackers
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        json={"attackers": [{"card_id": alice_bears, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})  # declare_blockers

    blocked = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        json={"player": "bob", "blocks": [{"blocker": bob_bears, "blocked": alice_bears}]},
    )
    blocked_body = blocked.json()
    assert isinstance(blocked_body.pop("log_seq"), int)
    assert blocked_body == {"allowed": True, "reason": None, "blocks": 1}
    combat = state(client, game)["combat"]["attacks"][0]
    assert combat["attacker"] == alice_bears
    assert combat["blockers"] == [bob_bears]

    # blocking something that is not attacking is refused in the engine's words
    not_attacking = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        json={"player": "bob", "blocks": [{"blocker": bob_bears, "blocked": bob_bears}]},
    )
    assert not_attacking.json()["allowed"] is False
    assert not_attacking.json()["reason"] == "Grizzly Bears is not attacking"

    log = log_of(client, game)
    entries = [entry for entry in log if entry["action"] == "declare_blockers"]
    assert entries[0]["detail"]["allowed"] is True
    assert entries[0]["detail"]["player"] == "bob"
    assert entries[0]["detail"]["blocks"] == [{"blocker": bob_bears, "blocked": alice_bears}]
    assert entries[0]["step"] == "declare_blockers"
    assert entries[1]["detail"]["allowed"] is False
    assert entries[1]["detail"]["reason"] == "Grizzly Bears is not attacking"


def test_a_resolution_records_what_resolved_and_whether_it_changed_anything(client, game):
    """A pass that resolves the stack says what resolved and whether it did anything.

    The engine keys some effects by card name, so a card it does not know
    resolves and leaves the game untouched. `outcome: resolve_top` alone
    cannot tell that apart from a spell that worked, which is the whole
    signal a playtest needs, so the entry carries both.
    """
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    board = state(client, game)
    sources = [
        {"card_id": find(board["battlefield"], "Mountain")["id"]},
        {"card_id": find(board["battlefield"], "Forest")["id"]},
    ]
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": "Grizzly Bears", "mana_sources": sources},
    )
    assert cast.json()["allowed"] is True, cast.text

    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    resolving = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert resolving.json()["outcome"] == PriorityOutcome.RESOLVE_TOP.value

    entry = entry_of(log_of(client, game), "pass_priority")
    assert entry["detail"]["resolved"] == "Grizzly Bears"
    # a creature spell resolving puts a permanent onto the battlefield
    assert entry["detail"]["changed"] is True


def test_a_pass_that_resolves_nothing_records_no_resolution(client, game):
    """A pass on an empty stack carries no resolution fields to misread."""
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    entry = entry_of(log_of(client, game), "pass_priority")
    assert "resolved" not in entry["detail"]
    assert "changed" not in entry["detail"]
