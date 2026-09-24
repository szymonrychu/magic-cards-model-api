"""The audit log: every mutating action lands in it, refusals included, with
the engine's own reason verbatim - and the console logger sees one line each."""

from __future__ import annotations

from datetime import datetime

from conftest import add_card, find, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import PriorityOutcome
from magic_cards_model.machines.effects import effect_for as real_effect_for
from magic_cards_model.models import game as game_module

from magic_cards_model_api import audit
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


def test_a_legacy_report_problem_row_reads_as_engine_over_the_log_route(client, game):
    """DR2 review fix: `audit.problem_category` existed but no read path
    called it, so a row written before `category` existed (no key at all -
    the exact shape `audit.record` still writes for a caller that omits it)
    read back with no `category` at all over `GET .../log` too. This route
    must fill it in as `engine`, `problem_category`'s own default."""
    audit.record(game, "report_problem", player="alice", text="an old report")
    log = log_of(client, game)
    row = entry_of(log, "report_problem")
    assert row["detail"]["category"] == "engine"


def test_a_categorized_report_problem_row_keeps_its_own_category_over_the_log_route(client, game):
    audit.record(game, "report_problem", player="alice", text="a dsl gap", category="dsl")
    log = log_of(client, game)
    row = entry_of(log, "report_problem")
    assert row["detail"]["category"] == "dsl"


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


def test_an_until_end_of_turn_pump_records_changed_true(client, game):
    """B7-11 (battle-7): `_card_fingerprint` used to carry zone stamp,
    tapped, sickness, damage, counters and attachment only - no power or
    toughness - so an until-end-of-turn pump (Craterhoof Behemoth's trigger,
    a prowess trigger, Mutagenic Growth) read as `changed: false` even though
    every creature on the board grew. Giant Growth is the simplest real card
    with that exact shape: no zone move, no tap, no counter, just power and
    toughness."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Giant Growth", "alice", "hand")
    board = state(client, game)
    forest = find(board["battlefield"], "Forest")
    bears = find(board["battlefield"], "Grizzly Bears")
    assert (bears["power"], bears["toughness"]) == (2, 2)

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Giant Growth",
            "targets": [bears["id"]],
            "mana_sources": [{"card_id": forest["id"]}],
        },
    )
    assert cast.json()["allowed"] is True, cast.text

    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    resolving = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert resolving.json()["outcome"] == PriorityOutcome.RESOLVE_TOP.value

    entry = entry_of(log_of(client, game), "pass_priority")
    assert entry["detail"]["resolved"] == "Giant Growth"
    assert entry["detail"]["changed"] is True
    pumped = find(state(client, game)["battlefield"], "Grizzly Bears")
    assert (pumped["power"], pumped["toughness"]) == (5, 5)


def test_resolve_top_endpoint_reports_changed_for_a_life_gain(client, game):
    """B7-11 (battle-7): the direct `/resolve-top` route - used outside
    `pass-priority`'s own auto-resolve, e.g. by a harness settling a paused
    resolution - never carried a `changed` marker at all: only the
    `pass_priority` path (`_run_pass_priority`) computed one, and
    `engine_api.resolve_top` itself returns only `resolved`/`zone`/`reason`.
    Lightning Helix's life gain is the reported shape: a real change that is
    neither a zone move, a tap, nor a counter."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Lightning Helix", "alice", "hand")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")
    plains = find(board["battlefield"], "Plains")

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Lightning Helix",
            "targets": ["bob"],
            "mana_sources": [{"card_id": mountain["id"]}, {"card_id": plains["id"]}],
        },
    )
    assert cast.json()["allowed"] is True, cast.text

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "Lightning Helix"
    assert resolved["changed"] is True
    alice = next(p for p in resolved["players"] if p["username"] == "alice")
    bob = next(p for p in resolved["players"] if p["username"] == "bob")
    assert alice["life"] == 23
    assert bob["life"] == 17


def test_resolve_top_reports_changed_for_a_control_change_alone(client, game, monkeypatch):
    """A resolution whose only observable effect is who controls a permanent
    is a change: `controller` is on every card view. Threaten would be the
    real shape, but on an untapped hasty creature the API view does not show
    its until-end-of-turn control yet, so this uses the `effect_for` seam
    `test_an_unimplemented_instant_records_changed_false` uses: Shock's
    effect hands Monastery Swiftspear to its caster through the engine's own
    base-controller setter, and nothing else."""

    def effect_for_taking_control(card_name, effect_text=None, *, x_value=None):
        if card_name == "Shock":

            def take_the_swiftspear(engine_game, spell):
                swiftspear = next(
                    card for card in engine_game.battlefield if card.name == "Monastery Swiftspear"
                )
                swiftspear.controller = spell.controller

            return take_the_swiftspear
        return real_effect_for(card_name, effect_text, x_value=x_value)

    monkeypatch.setattr(game_module, "effect_for", effect_for_taking_control)

    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    add_card(client, game, "Monastery Swiftspear", "bob", "battlefield")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")
    assert find(board["battlefield"], "Monastery Swiftspear")["controller"] == "bob"

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Shock",
            "targets": ["bob"],
            "mana_sources": [mountain["id"]],
        },
    )
    assert cast.json()["allowed"] is True, cast.text

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "Shock"
    assert find(resolved["battlefield"], "Monastery Swiftspear")["controller"] == "alice"
    assert [p["life"] for p in resolved["players"]] == [20, 20]
    assert resolved["changed"] is True


def _servant_trigger_on_the_stack(client, game) -> dict:
    """Servant of the Conduit resolved, its enters trigger (you get two
    energy counters) waiting on the stack. Returns the board."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Servant of the Conduit", "alice", "hand")
    board = state(client, game)
    forests = [c["id"] for c in board["battlefield"] if c["name"] == "Forest"]
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": "Servant of the Conduit", "mana_sources": forests},
    )
    assert cast.json()["allowed"] is True, cast.text
    assert (
        client.post(f"/api/games/{game}/resolve-top").json()["resolved"] == "Servant of the Conduit"
    )
    board = state(client, game)
    assert [obj["kind"] for obj in board["stack"]] == ["ability"], board["stack"]
    return board


def test_resolve_top_reports_changed_for_player_counters_alone(client, game):
    """Energy is a counter on a player (CR 122.1), shown in each player's
    `counters`. A trigger whose only effect is two energy counters changed
    something."""
    _servant_trigger_on_the_stack(client, game)

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    alice = next(p for p in resolved["players"] if p["username"] == "alice")
    assert alice["counters"].get("energy") == 2, alice
    assert resolved["changed"] is True


def test_resolve_top_reports_changed_when_only_the_stack_changed(client, game, monkeypatch):
    """A resolution that only puts a new object on the stack changed what a
    seat sees. Copying a spell (CR 707.10) is that shape, but the pool's
    copy effects all ask a target question first, so this uses the same
    `effect_for` seam `test_an_unimplemented_instant_records_changed_false`
    uses: Shock's effect becomes "copy the Lightning Bolt below", run through
    the engine's own `Game.copy_spell`."""

    def effect_for_copying_the_bolt(card_name, effect_text=None, *, x_value=None):
        if card_name == "Shock":

            def copy_the_bolt(engine_game, spell):
                bolt = next(
                    obj
                    for obj in engine_game.stack
                    if getattr(obj, "name", None) == "Lightning Bolt"
                )
                engine_game.copy_spell(bolt)

            return copy_the_bolt
        return real_effect_for(card_name, effect_text, x_value=x_value)

    monkeypatch.setattr(game_module, "effect_for", effect_for_copying_the_bolt)

    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Shock", "alice", "hand")
    board = state(client, game)
    first, second = [c["id"] for c in board["battlefield"] if c["name"] == "Mountain"]
    for card, source in (("Lightning Bolt", first), ("Shock", second)):
        cast = client.post(
            f"/api/games/{game}/actions/cast",
            json={"player": "alice", "card": card, "targets": ["bob"], "mana_sources": [source]},
        )
        assert cast.json()["allowed"] is True, cast.text

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "Shock"
    assert [obj["name"] for obj in resolved["stack"]] == ["Lightning Bolt", "Lightning Bolt"]
    assert [p["life"] for p in resolved["players"]] == [20, 20]
    assert resolved["changed"] is True


def test_entries_carry_active_player_and_priority_holder(client, game):
    """S2 A7: every row is stamped with whose turn it was written in and who
    held priority the moment after - both read straight off the live game,
    so `alice`/`bob` here are what `game` fixture's own seating (alice seat
    0) and CR 103.3's starting priority already say."""
    add_card(client, game, "Mountain", "alice", "hand")
    log = log_of(client, game)
    create = entry_of(log, "create_game")
    assert create["active_player"] == "alice"
    added = entry_of(log, "add_card")
    assert added["active_player"] == "alice"
    assert added["priority_holder"] == "alice"

    passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    assert passed.json()["allowed"] is True

    log = log_of(client, game)
    pass_entry = entry_of(log, "pass_priority")
    assert pass_entry["active_player"] == "alice", "the turn has not changed hands"
    assert pass_entry["priority_holder"] == "bob", "alice's pass handed bob priority"


def test_entries_carry_turns_taken(client, game):
    """Every row is also stamped with `turn.turns_taken` - the count that
    goes up once per turn that actually begins, including an extra turn
    (CR 500.7), unlike `turn_number` (CR 500.1's count of rounds of the
    table), which CR 500.7 leaves alone for one. This is what lets
    `scripts/eval_report.py` tell an extra turn apart from the ordinary
    turn right before it even though both share the same
    `turn`/`active_player`. The `game` fixture starts already on the
    starting player's first turn, so `turns_taken` reads 1 on every row
    here, same as `turn`."""
    add_card(client, game, "Mountain", "alice", "hand")
    log = log_of(client, game)
    added = entry_of(log, "add_card")
    assert added["turns_taken"] == 1

    passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    assert passed.json()["allowed"] is True

    log = log_of(client, game)
    pass_entry = entry_of(log, "pass_priority")
    assert pass_entry["turns_taken"] == 1, "still alice's first turn - turns_taken has not moved"


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


# -- T9: the audit log's own name for CR 510.4's first-strike damage step ----
#
# `audit.record`'s `step` field goes through `serialize.step_position_of`
# (audit.py, "CR 510.4/506.1" comment), not the raw `Step` value, so a row
# written while the game sits in the *first* of the two combat damage steps
# reads `"first_strike_damage"`, and a row written in the second reads the
# engine's own `"combat_damage"` - the same distinction `state_view`'s
# `first_strike_damage_step` flag makes, proven end to end here through the
# real engine (`tests/test_first_strike_step_label.py` is the same boot).


def _boot_first_strike_combat_to_declared(client: TestClient, game: str) -> dict[str, str]:
    """Alice's Precinct Captain (2/2 first strike) attacking, blocked by
    Bob's Grizzly Bears (2/2, no keywords): a real CR 510.4 combat with
    exactly one blocker, so no `assign_combat_damage` question interrupts
    it. Returns with blockers declared, right before the first combat
    damage step - the same boot `test_first_strike_step_label.py` uses."""
    uids = {name: uid for uid, name in store.seat_uids(game).items()}
    captain = add_card(client, game, "Precinct Captain", "alice", "battlefield")["added"]["card_id"]
    client.patch(f"/api/games/{game}/cards/{captain}", json={"summoning_sick": False})
    bears = add_card(client, game, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]

    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": captain, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True, attack.json()
    client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    blocks = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={"player": "bob", "blocks": [{"blocker": bears, "blocked": captain}]},
    )
    assert blocks.json().get("allowed", blocks.json().get("accepted")) is True, blocks.json()
    return {"captain": captain, "bears": bears}


def test_audit_log_names_the_first_combat_damage_step_first_strike_damage(client, game):
    """A row written while the board is in CR 510.4's first combat damage
    step - `first_strike_damage_step: True` on `state_view` - is logged
    `"step": "first_strike_damage"`, never the raw `"combat_damage"` the
    engine's own `Step` carries for both occurrences."""
    _boot_first_strike_combat_to_declared(client, game)
    first = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    assert first.json()["allowed"] is True, first.json()
    board = state(client, game)
    assert board["step"] == "combat_damage" and board["first_strike_damage_step"] is True, board

    passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    assert passed.json()["allowed"] is True, passed.json()

    entry = entry_of(log_of(client, game), "pass_priority")
    assert entry["step"] == "first_strike_damage", entry


def test_audit_log_names_the_second_combat_damage_step_combat_damage(client, game):
    """The same row shape, one occurrence later: `first_strike_damage_step`
    is now `False`, and the logged `step` reads the engine's own
    `"combat_damage"` again."""
    _boot_first_strike_combat_to_declared(client, game)
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    # Empty stack, nothing further to assign: `advance` moves straight into
    # the second occurrence, the same idiom `test_first_strike_step_label.py`
    # uses (Precinct Captain's damage went to a blocking creature, not a
    # player, so its own trigger never put anything on the stack).
    assert state(client, game)["stack"] == []
    second = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    assert second.json()["allowed"] is True, second.json()
    board = state(client, game)
    assert board["step"] == "combat_damage" and board["first_strike_damage_step"] is False, board

    passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    assert passed.json()["allowed"] is True, passed.json()

    entry = entry_of(log_of(client, game), "pass_priority")
    assert entry["step"] == "combat_damage", entry


def test_advance_into_the_first_strike_step_is_named_in_response_and_audit_detail(client, game):
    """`POST .../advance`'s own `advanced_to`, and the `advance` audit row's
    `detail["to"]` (`app.py`'s `step_value = result.outcome["advanced_to"]`
    fed straight into the same `audit.record` call), both name the step
    `"first_strike_damage"` - not `"combat_damage"` - the moment the board
    lands there. The row's `detail["from"]` still reads the raw step the
    advance was made *from* (`declare_blockers`, which has no first-strike
    ambiguity of its own)."""
    _boot_first_strike_combat_to_declared(client, game)
    response = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    assert response.json()["allowed"] is True, response.json()
    assert response.json()["advanced_to"] == "first_strike_damage", response.json()

    entry = entry_of(log_of(client, game), "advance")
    assert entry["detail"]["from"] == "declare_blockers", entry
    assert entry["detail"]["to"] == "first_strike_damage", entry
