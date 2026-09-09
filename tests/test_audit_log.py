"""The audit log: every mutating action lands in it, refusals included, with
the engine's own reason verbatim - and the console logger sees one line each."""

from __future__ import annotations

from datetime import datetime

from conftest import add_card, find, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import PriorityOutcome
from magic_cards_model.machines.effects import effect_for as real_effect_for
from magic_cards_model.models import game as game_module

from magic_cards_model_api.store import store  # noqa: E402


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
        # Nothing to settle: `precombat_main` grants priority (CR 500.3's two
        # exceptions are the untap and cleanup steps, and this is neither).
        "settled_through": [],
        # No `notices` key: a plain pass on an empty stack never resolves
        # anything, so `commands._new_notices` found nothing to attach.
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
        "settled_through": [],
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

    uids = {name: uid for uid, name in store.seat_uids(game).items()}
    client.patch(f"/api/games/{game}/cards/{alice_bears}", json={"summoning_sick": False})
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})  # declare_attackers
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": alice_bears, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})  # declare_blockers

    # Blocking something that is not attacking is refused in the engine's
    # words, and - since a refused declaration never touches the standing
    # pending decision (`_answer_declaration`'s own reasoning) - tried first,
    # while `declare_blockers` is still genuinely pending: answering it
    # correctly right after consumes that pending decision for good, so a
    # second, illegal attempt afterward would only ever see "no pending
    # decision", not the engine's real legality refusal.
    not_attacking = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={"player": "bob", "blocks": [{"blocker": bob_bears, "blocked": bob_bears}]},
    )
    assert not_attacking.json()["allowed"] is False
    assert not_attacking.json()["reason"] == "Grizzly Bears is not attacking"

    blocked = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={"player": "bob", "blocks": [{"blocker": bob_bears, "blocked": alice_bears}]},
    )
    blocked_body = blocked.json()
    assert isinstance(blocked_body.pop("log_seq"), int)
    # `_answer_declaration`'s success shape is deliberately thin (no `reason`,
    # no `blocks` echo) now that it is a thin alias over the pause/resume
    # tape rather than its own transport - the full detail (`blocks`, the
    # player, ...) lands in the audit log instead, checked below via
    # `entries[1]["detail"]`.
    assert blocked_body == {"allowed": True}
    combat = state(client, game)["combat"]["attacks"][0]
    assert combat["attacker"] == alice_bears
    assert combat["blockers"] == [bob_bears]

    log = log_of(client, game)
    entries = [entry for entry in log if entry["action"] == "declare_blockers"]
    assert entries[0]["detail"]["allowed"] is False
    assert entries[0]["detail"]["reason"] == "Grizzly Bears is not attacking"
    assert entries[1]["detail"]["allowed"] is True
    assert entries[1]["detail"]["player"] == "bob"
    assert entries[1]["detail"]["blocks"] == [{"blocker": bob_bears, "blocked": alice_bears}]
    assert entries[1]["step"] == "declare_blockers"


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


def test_an_unimplemented_instant_records_changed_false(client, game, monkeypatch):
    """This test pins the `changed: false` half of the marker's job: a spell
    resolves, has no effect, and the audit log must say so.

    Twice already this test named a real card as "the unimplemented one" -
    first Ephemerate, then Mind Rot - and twice the engine caught up and
    implemented it out from under the test (see git history/`docs/`). Any
    specific card is a ticking clock: this project's whole point is to keep
    implementing more of the Comprehensive Rules, so "currently unimplemented"
    is never a stable property of a card name, only of a moment in time. It
    also turned out to be a hang risk: the newly-implemented effect (Mind
    Rot's discard) raises a live decision that nothing in this test answers,
    which parks the request on `GameDecisionProvider`'s real timeout
    (`magic_cards_model_api.decisions.DEFAULT_TIMEOUT_SECONDS`, 300s) instead
    of failing fast.

    So this test no longer trusts any card's implementation status. It still
    casts a real card (Mind Rot, a real Scryfall printing, cast for real mana,
    same as every other scenario in this suite - CLAUDE.md's "no mocks"
    applies here too) but forces the structural precondition the old test was
    hoping to get by luck: it monkeypatches `models.game.effect_for` - the
    exact seam `Game._resolve` calls, `effect = effect_for(spell.name,
    spell.oracle_text, x_value=x_value)` - so that this one card resolves to
    `None` (no effect) regardless of what `machines/effects.py` knows how to
    do with it, this test run or any future one. That is stable by
    construction, not by whichever cards the engine has and has not gotten to
    yet, and it also means no decision is ever raised for this resolution, so
    there is nothing to hang on - the hang and the flaky premise share one fix.
    `test_an_implemented_instant_that_leaves_the_stack_records_changed_true`
    below is the contrast case proving the discount does not swallow a real
    effect, and does not go through this patch."""

    def effect_for_forced_no_op(card_name, effect_text=None, *, x_value=None):
        if card_name == "Mind Rot":
            return None
        return real_effect_for(card_name, effect_text, x_value=x_value)

    monkeypatch.setattr(game_module, "effect_for", effect_for_forced_no_op)

    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Mind Rot", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "hand")
    board = state(client, game)
    swamps = [c["id"] for c in board["battlefield"] if c["name"] == "Swamp"]
    mind_rot = board["hands"]["alice"][0]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": mind_rot["id"],
            "targets": ["bob"],
            "mana_sources": [{"card_id": sid} for sid in swamps],
        },
    )
    assert cast.json()["allowed"] is True, cast.text

    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    resolving = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert resolving.json()["outcome"] == PriorityOutcome.RESOLVE_TOP.value

    entry = entry_of(log_of(client, game), "pass_priority")
    assert entry["detail"]["resolved"] == "Mind Rot"
    assert entry["detail"]["changed"] is False
    # it still went where a sorcery goes - the graveyard move itself is
    # real, it just is not the "effect happened" signal
    assert [card["name"] for card in state(client, game)["graveyards"]["alice"]] == ["Mind Rot"]
    # and bob's hand - the thing Mind Rot claims to empty - is untouched
    assert [card["name"] for card in state(client, game)["hands"]["bob"]] == ["Grizzly Bears"]


def test_a_blink_records_changed_true(client, game):
    """The blind spot `_resolution_state` used to have: Ephemerate exiles
    Grizzly Bears and immediately returns it to the battlefield under its
    owner's control (CR 301.5a is not in play here, this is the plain
    "exile then return" text). The bears keep the same `id`, the same
    controller, the same name, and - since it returns untapped with no
    counters, same as it started - every field the old name-and-flags
    snapshot compared. Only `zone_change_stamp` (CR 400.7's "becomes a new
    object") tells the two incarnations apart, which is why this needs its
    own test: `test_a_resolution_records_what_resolved_and_whether_it_changed_anything`
    above only proves a *new* permanent arriving is seen, not a permanent
    that was already there and round-tripped through exile."""
    add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Ephemerate", "alice", "hand")
    board = state(client, game)
    plains = find(board["battlefield"], "Plains")
    bears = find(board["battlefield"], "Grizzly Bears")

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Ephemerate",
            "targets": [bears["id"]],
            "mana_sources": [{"card_id": plains["id"]}],
        },
    )
    assert cast.json()["allowed"] is True, cast.text

    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    resolving = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert resolving.json()["outcome"] == PriorityOutcome.RESOLVE_TOP.value

    entry = entry_of(log_of(client, game), "pass_priority")
    assert entry["detail"]["resolved"] == "Ephemerate"
    assert entry["detail"]["changed"] is True
    # rebound (CR 702.88a) sends the spell to exile, not the graveyard, as
    # it resolves - Ephemerate's own effect, not the discount, put it there
    assert state(client, game)["graveyards"]["alice"] == []
    assert [card["name"] for card in state(client, game)["exile"]] == ["Ephemerate"]
    # and the bears really are still on the battlefield, under their own id
    still_there = find(state(client, game)["battlefield"], "Grizzly Bears")
    assert still_there["id"] == bears["id"]


def test_an_implemented_instant_that_leaves_the_stack_records_changed_true(client, game):
    """Contrast case: Lightning Bolt also leaves the stack for a graveyard,
    exactly like Ephemerate, but its effect (3 damage to bob) really does
    run - so the graveyard-move discount must not swallow that real change."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Lightning Bolt",
            "targets": ["bob"],
            "mana_sources": [{"card_id": mountain["id"]}],
        },
    )
    assert cast.json()["allowed"] is True, cast.text

    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    resolving = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert resolving.json()["outcome"] == PriorityOutcome.RESOLVE_TOP.value

    entry = entry_of(log_of(client, game), "pass_priority")
    assert entry["detail"]["resolved"] == "Lightning Bolt"
    assert entry["detail"]["changed"] is True


def test_dry_run_cast_is_not_audited(client, game):
    """A `dry_run` cast probe - what `seatkit.available_actions` used to
    issue per hand card, before T11 pointed it at the engine's own
    enumeration instead - must never appear in the game log: it changes
    nothing, and auditing it anyway is how one traced game's log grew to
    2410 entries with 956 of them refusal noise from exactly this kind of
    probe. A dry-run cast is still a real, directly-callable request shape
    (`actions/cast?dry_run=true`), so this still pins the same invariant for
    it. Moved here from the now-deleted `test_deadlock.py` (T7b): the
    deadlock it guarded against cannot occur any more (no mutating request
    blocks on a decision at all - see `commands.py`), but this assertion
    about dry-run auditing is unrelated to that and still holds."""
    add_card(client, game, "Mountain", "alice", "hand")
    board = state(client, game)
    mountain_id = board["hands"]["alice"][0]["id"]

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": mountain_id, "dry_run": True},
    )
    assert response.status_code == 200
    assert response.json()["log_seq"] is None

    log = client.get(f"/api/games/{game}/log").json()["log"]
    casts = [entry for entry in log if entry["action"] == "cast"]
    assert casts == [], "a dry-run cast must never be written to the audit log"
