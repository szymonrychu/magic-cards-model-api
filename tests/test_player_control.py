"""One seat controlling another player's turn (CR 723), through the API.

Live game 0cf08288 (2026-09-22): claude cast Emrakul, the Promised End and
controlled "you"'s turn 7. The engine had it right - "you"'s own pass was
refused with "claude is making you's decisions this turn (CR 723.5)" and
claude's pass went through - but nothing in the state said claude was
deciding for "you", so every seat-side check that reads `priority_holder`
(pass_until, the wait, arming) decided claude had nothing to do, the attack
and decide routes refused claude's uid as "not the deciding player's seat",
and claude's activation of "you"'s Karn went to the engine as claude's own
("claude does not control Karn"). The game froze.

The rules, read whole with `mise run rules:find 723` in the session that
wrote this (the spellslinger MCP server was not reachable from it):

- CR 723.1 - the effect applies to the affected player's next turn, for the
  whole turn.
- CR 723.3 - "Only control of the player changes. All objects are controlled
  by their normal controllers. A player who's being controlled during their
  turn is still the active player."
- CR 723.4 - "If information about an object in the game would be visible to
  the player being controlled, it's visible to both that player and the
  controller of the player. If information about cards outside the game would
  be visible to the player being controlled, it's visible only to that
  player, not the controller of the player."
- CR 723.5 - the controller "makes all choices and decisions the controlled
  player is allowed to make or is told to make by the rules or by any
  objects. This includes choices and decisions about what to play".
- CR 723.6 - "The controller of another player can't make that player
  concede."

The control effect itself is set up with the engine's own
`Game.gain_control_of_player` (Emrakul's cast trigger calls exactly that),
reached straight into the live store the way `conftest.force_turn_position`
already does; everything after that goes through the API.
"""

from __future__ import annotations

from typing import Any

import httpx
from conftest import add_card, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step

from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import (
    NOTHING_NEW_YET,
    Seats,
    autopass_stop_reason,
    block_until_wake,
    compute_wake_reason,
    pass_until,
)
from magic_cards_model_api.store import store

CR_723_5 = "alice is making bob's decisions this turn (CR 723.5)"


def uids(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def post(client: TestClient, game_id: str, path: str, body: dict[str, Any], uid: str) -> dict:
    response = client.post(f"/api/games/{game_id}/{path}", params={"uid": uid}, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def stock_libraries(client: TestClient, game_id: str) -> None:
    for player, land in (("alice", "Forest"), ("bob", "Island")):
        for _ in range(6):
            add_card(client, game_id, land, player, "library")


def into_bobs_controlled_turn(client: TestClient, game_id: str) -> None:
    """Alice gains control of bob's next turn (CR 723.1) at the end of her
    own, then both pass it out; the pass settles through cleanup and bob's
    untap step into his upkeep, the first step anyone holds priority in."""
    stock_libraries(client, game_id)
    force_turn_position(game_id, Step.END_STEP, "alice")
    live = store.get(game_id)
    assert live is not None
    live.gain_control_of_player("alice", "bob")
    seat = uids(game_id)
    assert post(client, game_id, "actions/pass-priority", {"player": "alice"}, seat["alice"])[
        "allowed"
    ]
    assert post(client, game_id, "actions/pass-priority", {"player": "bob"}, seat["bob"])["allowed"]
    board = state(client, game_id)
    assert (board["active_player"], board["step"], board["priority_holder"]) == (
        "bob",
        "upkeep",
        "bob",
    )


def wired(monkeypatch, game_id: str) -> Seats:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id)


def player_block(board: dict, username: str) -> dict:
    return next(p for p in board["players"] if p["username"] == username)


# -- the state says who is deciding for whom --------------------------------


def test_the_state_names_who_makes_the_controlled_players_decisions(client, game):
    into_bobs_controlled_turn(client, game)
    seat = uids(game)

    full = state(client, game)
    assert player_block(full, "bob")["decisions_made_by"] == "alice"
    assert player_block(full, "alice")["decisions_made_by"] is None
    assert full["player_control"] == {"bob": "alice"}

    alices = state(client, game, seat["alice"])
    assert alices["deciding_for"] == ["bob"]
    bobs = state(client, game, seat["bob"])
    assert bobs["deciding_for"] == []
    assert player_block(bobs, "bob")["decisions_made_by"] == "alice"


def test_no_control_effect_means_nobody_decides_for_anybody(client, game):
    stock_libraries(client, game)
    board = state(client, game, uids(game)["alice"])
    assert board["player_control"] == {}
    assert board["deciding_for"] == []
    assert all(p["decisions_made_by"] is None for p in board["players"])


# -- CR 723.4: what the controller sees --------------------------------------


def test_the_controller_sees_the_controlled_players_hand(client, game):
    into_bobs_controlled_turn(client, game)
    add_card(client, game, "Grizzly Bears", "bob", "hand")
    add_card(client, game, "Llanowar Elves", "alice", "hand")
    seat = uids(game)

    alices = state(client, game, seat["alice"])
    assert [c["name"] for c in alices["hands"]["bob"]] == ["Grizzly Bears"]
    bobs = state(client, game, seat["bob"])
    assert bobs["hands"]["alice"] == [], "control runs one way: bob sees nothing new"


def test_the_controller_never_sees_the_controlled_players_cards_outside_the_game(client, game):
    """CR 723.4's second sentence: a sideboard is outside the game."""
    into_bobs_controlled_turn(client, game)
    alices = state(client, game, uids(game)["alice"])
    assert "sideboards" not in alices
    assert "decklist" not in player_block(alices, "bob")


# -- priority: CR 723.5 --------------------------------------------------------


def test_the_controlled_players_own_pass_is_refused_with_the_rule(client, game):
    into_bobs_controlled_turn(client, game)
    refused = post(client, game, "actions/pass-priority", {"player": "bob"}, uids(game)["bob"])
    assert refused["allowed"] is False
    assert CR_723_5 in refused["reason"]


def test_wait_for_priority_wakes_the_controller_and_not_the_controlled(client, game, monkeypatch):
    into_bobs_controlled_turn(client, game)
    seats = wired(monkeypatch, game)
    seat = uids(game)

    woken = block_until_wake(seats.seat_for(seat["alice"]), before=None)
    assert "priority" in woken["wake_reason"], woken["wake_reason"]

    bob = block_until_wake(seats.seat_for(seat["bob"]), before=None)
    assert bob["wake_reason"] == NOTHING_NEW_YET, bob["wake_reason"]


def test_pass_until_by_the_controller_runs_through_the_controlled_turn(client, game, monkeypatch):
    """The live deadlock: the controller must pass bob's priorities itself.
    Nobody else touches the game here - alice decides for both players all
    turn, including bob's own declare-attackers, which the run answers with
    nobody on bob's behalf (CR 508.1, "if any")."""
    into_bobs_controlled_turn(client, game)
    seats = wired(monkeypatch, game)
    alice = seats.seat_for(uids(game)["alice"])

    resume = None
    for _ in range(20):
        result = pass_until(alice, until_step="upkeep", whose_turn="mine", resume=resume)
        if result["finished"]:
            break
        resume = result["resume"]
    assert result["finished"], result["stopped_because"]
    assert result["stopped_because"] == "reached your upkeep", result["stopped_because"]
    board = state(client, game)
    assert (board["active_player"], board["step"]) == ("alice", "upkeep")
    assert board["player_control"] == {}, "CR 723.1: the effect ended with bob's turn"


def test_the_legal_actions_of_the_controller_include_the_controlled_players(client, game):
    into_bobs_controlled_turn(client, game)
    seat = uids(game)
    response = client.get(
        f"/api/games/{game}/legal-actions", params={"player": "alice", "uid": seat["alice"]}
    )
    assert response.status_code == 200, response.text
    rows = response.json()["actions"]
    for_bob = [row for row in rows if row.get("acting_for") == "bob"]
    assert for_bob, rows
    assert all(row["player"] == "bob" for row in for_bob)
    assert "pass_priority" in {row["kind"] for row in for_bob}
    assert "concede" not in {row["kind"] for row in for_bob}, "CR 723.6"


def test_the_controller_may_read_the_controlled_players_legal_actions_by_name(client, game):
    into_bobs_controlled_turn(client, game)
    response = client.get(
        f"/api/games/{game}/legal-actions", params={"player": "bob", "uid": uids(game)["alice"]}
    )
    assert response.status_code == 200, response.text


# -- acting with the controlled player's objects: CR 723.5 -------------------


def test_the_controller_activates_the_controlled_players_permanent(client, game):
    """The live `claude does not control Karn` refusal: alice names herself,
    as every seat tool does, and the action is bob's (CR 723.3 - the Forest is
    still bob's; CR 723.5 - the choice to use it is alice's)."""
    into_bobs_controlled_turn(client, game)
    forest = add_card(client, game, "Forest", "bob", "battlefield")["added"]["card_id"]
    seat = uids(game)
    outcome = post(
        client,
        game,
        "actions/activate",
        {"player": "alice", "card_id": forest, "ability_index": 0},
        seat["alice"],
    )
    assert outcome["allowed"] is True, outcome
    bob = player_block(state(client, game), "bob")
    assert bob["mana_pool"]["floating"].get("G") == 1


def test_the_controlled_players_own_activation_is_refused(client, game):
    into_bobs_controlled_turn(client, game)
    forest = add_card(client, game, "Forest", "bob", "battlefield")["added"]["card_id"]
    outcome = post(
        client,
        game,
        "actions/activate",
        {"player": "bob", "card_id": forest, "ability_index": 0},
        uids(game)["bob"],
    )
    assert outcome["allowed"] is False
    assert CR_723_5 in outcome["reason"]


def test_the_controller_plays_the_controlled_players_land(client, game):
    into_bobs_controlled_turn(client, game)
    force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")
    island = add_card(client, game, "Island", "bob", "hand")["added"]["card_id"]
    outcome = post(
        client,
        game,
        "actions/play-land",
        {"player": "alice", "card_id": island},
        uids(game)["alice"],
    )
    assert outcome["allowed"] is True, outcome
    battlefield = state(client, game)["battlefield"]
    assert any(c["id"] == island and c["controller"] == "bob" for c in battlefield)


def test_the_controlled_player_may_still_concede(client, game):
    """CR 723.6: conceding is the one decision that is never handed over."""
    into_bobs_controlled_turn(client, game)
    outcome = post(client, game, "actions/concede", {"player": "bob"}, uids(game)["bob"])
    assert outcome["allowed"] is True, outcome


# -- decisions addressed to the controlled player -----------------------------


def into_bobs_declare_attackers(client: TestClient, game_id: str) -> dict:
    """Bob's creature, bob's combat, alice deciding: alice passes bob's
    priority and then her own, and the step ends into CR 508.1's question."""
    # On the battlefield before bob's turn begins, so it is not summoning sick.
    bears = add_card(client, game_id, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    into_bobs_controlled_turn(client, game_id)
    force_turn_position(game_id, Step.BEGINNING_OF_COMBAT, "bob")
    alice = uids(game_id)["alice"]
    post(client, game_id, "actions/pass-priority", {"player": "alice"}, alice)
    paused = post(client, game_id, "actions/pass-priority", {"player": "alice"}, alice)
    assert paused.get("paused") is True, paused
    assert paused["pending_decision"]["kind"] == "declare_attackers"
    assert paused["pending_decision"]["player"] == "bob"
    return {"bears": bears}


def test_the_controller_declares_the_controlled_players_attackers(client, game):
    ids = into_bobs_declare_attackers(client, game)
    seat = uids(game)
    alices = state(client, game, seat["alice"])
    assert alices["pending_decision"].get("generation") is not None, (
        "the controller is the one answering, so it is shown the whole question"
    )
    answer = post(
        client,
        game,
        "actions/attack",
        {"attackers": [{"card_id": ids["bears"], "defender": "alice"}]},
        seat["alice"],
    )
    assert answer["allowed"] is True, answer
    attacks = state(client, game)["combat"]["attacks"]
    assert [a["attacker"] for a in attacks] == [ids["bears"]]


def test_the_controlled_players_own_declaration_is_refused(client, game):
    into_bobs_declare_attackers(client, game)
    refused = post(client, game, "actions/attack", {"attackers": []}, uids(game)["bob"])
    assert refused["allowed"] is False
    assert CR_723_5 in refused["reason"]


def test_pass_until_declares_nobody_for_the_controlled_player(client, game, monkeypatch):
    """The second live freeze: pass_until said the game was "frozen on you's
    own declare_attackers ... only they can answer it"."""
    into_bobs_declare_attackers(client, game)
    seats = wired(monkeypatch, game)
    alice = seats.seat_for(uids(game)["alice"])
    result = pass_until(alice, until_step="postcombat_main", whose_turn="opponent")
    assert result["finished"], result["stopped_because"]
    assert result["stopped_because"] == "reached your opponent's postcombat main"


def controlled_board(**overrides: Any) -> dict:
    """A seat's view of bob's controlled turn, as `autopass_stop_reason` and
    `compute_wake_reason` read it."""
    return {
        "you": "alice",
        "winner": None,
        "drawn": False,
        "pregame": None,
        "turn_number": 4,
        "step": "beginning_of_combat",
        "active_player": "bob",
        "priority_holder": "bob",
        "stack": [],
        "combat": {"attacks": []},
        "pending_decision": None,
        "players": [
            {"username": "alice", "decisions_made_by": None},
            {"username": "bob", "decisions_made_by": "alice"},
        ],
    } | overrides


def test_autopass_treats_the_controlled_players_attack_as_the_controllers():
    board = controlled_board(
        pending_decision={"player": "bob", "kind": "declare_attackers", "prompt": "attack?"}
    )
    run = {"step": "end_step", "whose_turn": "opponent", "started_step": None}
    reason = autopass_stop_reason(board, run, "alice", stop_when_you_can_attack=True)
    assert reason is not None and "declare-attackers" in reason, reason
    assert autopass_stop_reason(board, run, "bob", stop_when_you_can_attack=True) is None


def test_autopass_stops_for_any_other_decision_the_controller_must_make():
    board = controlled_board(
        pending_decision={"player": "bob", "kind": "choose_cards", "prompt": "discard"}
    )
    run = {"step": "end_step", "whose_turn": "opponent", "started_step": None}
    assert autopass_stop_reason(board, run, "alice") == "a decision is waiting for you: discard"


def test_the_destination_stays_relative_to_the_seats_own_player():
    """ "mine" is still alice's own turn, even while she decides for bob."""
    board = controlled_board(step="end_step")
    mine = {"step": "end_step", "whose_turn": "mine", "started_step": None}
    theirs = {"step": "end_step", "whose_turn": "opponent", "started_step": None}
    assert autopass_stop_reason(board, mine, "alice") is None
    assert autopass_stop_reason(board, theirs, "alice") == "reached your opponent's end step"


def test_the_controller_holds_the_controlled_players_priority_for_a_wake():
    board = controlled_board()
    assert compute_wake_reason(board, None, "alice") is not None
    assert compute_wake_reason(board, None, "bob") is None


def into_bobs_cleanup_discard(client: TestClient, game_id: str) -> dict:
    """Eight cards in bob's hand at the end of his controlled turn: CR 514.1's
    discard is bob's choice, so alice makes it (CR 723.5)."""
    into_bobs_controlled_turn(client, game_id)
    cards = [
        add_card(client, game_id, "Island", "bob", "hand")["added"]["card_id"] for _ in range(8)
    ]
    force_turn_position(game_id, Step.END_STEP, "bob")
    alice = uids(game_id)["alice"]
    post(client, game_id, "actions/pass-priority", {"player": "alice"}, alice)
    paused = post(client, game_id, "actions/pass-priority", {"player": "alice"}, alice)
    assert paused.get("paused") is True, paused
    assert paused["pending_decision"]["kind"] == "choose_cards"
    assert paused["pending_decision"]["player"] == "bob"
    return {"cards": cards}


def test_the_controller_answers_the_controlled_players_decision(client, game):
    ids = into_bobs_cleanup_discard(client, game)
    answer = post(
        client,
        game,
        "decide",
        {"player": "alice", "card_ids": ids["cards"][:1]},
        uids(game)["alice"],
    )
    assert answer["accepted"] is True, answer
    assert len(state(client, game)["hands"]["bob"]) == 7


def test_the_controlled_players_own_answer_is_refused(client, game):
    ids = into_bobs_cleanup_discard(client, game)
    answer = post(
        client,
        game,
        "decide",
        {"player": "bob", "card_ids": ids["cards"][:1]},
        uids(game)["bob"],
    )
    assert answer["accepted"] is False
    assert CR_723_5 in answer["reason"]


def test_a_decision_addressed_to_the_controlled_player_wakes_the_controller(
    client, game, monkeypatch
):
    into_bobs_cleanup_discard(client, game)
    seats = wired(monkeypatch, game)
    woken = block_until_wake(seats.seat_for(uids(game)["alice"]), before=None)
    assert woken["wake_reason"].startswith("a decision is waiting for you"), woken


def into_bobs_declare_blockers_under_alices_control(client: TestClient, game_id: str) -> None:
    """CR 723.2's limited-duration effects (Word of Command, Opposition Agent)
    can leave a player controlled outside their own turn; the engine models
    only CR 723.1's whole-turn shape, so this puts bob under alice's control on
    alice's own turn directly, as test setup, to reach CR 509.1's question
    addressed to a controlled defender."""
    stock_libraries(client, game_id)
    attacker = add_card(client, game_id, "Grizzly Bears", "alice", "battlefield")
    patched = client.patch(
        f"/api/games/{game_id}/cards/{attacker['added']['card_id']}",
        json={"summoning_sick": False},
    )
    assert patched.status_code == 200, patched.text
    add_card(client, game_id, "Llanowar Elves", "bob", "battlefield")
    force_turn_position(game_id, Step.BEGINNING_OF_COMBAT, "alice")
    alice = uids(game_id)["alice"]
    bob = uids(game_id)["bob"]
    post(client, game_id, "actions/pass-priority", {"player": "alice"}, alice)
    paused = post(client, game_id, "actions/pass-priority", {"player": "bob"}, bob)
    assert paused["pending_decision"]["kind"] == "declare_attackers"
    bears = next(c for c in state(client, game_id)["battlefield"] if c["name"] == "Grizzly Bears")
    assert post(
        client,
        game_id,
        "actions/attack",
        {"attackers": [{"card_id": bears["id"], "defender": "bob"}]},
        alice,
    )["allowed"]
    live = store.get(game_id)
    assert live is not None and live.turn is not None
    live.turn.player_control = {"bob": "alice"}
    live._sync_player_control()
    post(client, game_id, "actions/pass-priority", {"player": "alice"}, alice)
    paused = post(client, game_id, "actions/pass-priority", {"player": "alice"}, alice)
    assert paused["pending_decision"]["kind"] == "declare_blockers", paused
    assert paused["pending_decision"]["player"] == "bob"


def test_the_controller_declares_the_controlled_players_blockers(client, game):
    into_bobs_declare_blockers_under_alices_control(client, game)
    seat = uids(game)
    refused = post(
        client, game, "actions/declare-blockers", {"player": "bob", "blocks": []}, seat["bob"]
    )
    assert refused["allowed"] is False
    assert CR_723_5 in refused["reason"]
    answer = post(
        client, game, "actions/declare-blockers", {"player": "alice", "blocks": []}, seat["alice"]
    )
    assert answer["allowed"] is True, answer


def test_armed_scripts_judge_only_for_the_player_deciding():
    """Arming (`_arming_moment`) fires on the controller's moment, never on
    the controlled seat's - whose priority is not its own to act on."""
    from magic_cards_model_api.seatkit import _arming_moment

    board = controlled_board()
    assert _arming_moment(board, "alice") is True
    assert _arming_moment(board, "bob") is False
    pending = {"player": "bob", "kind": "choose_cards", "prompt": "discard"}
    assert _arming_moment(controlled_board(pending_decision=pending), "alice") is True
    assert _arming_moment(controlled_board(pending_decision=pending), "bob") is False
