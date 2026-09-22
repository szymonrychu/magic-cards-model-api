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


# -- review follow-ups ----------------------------------------------------------


def raw_post(client: TestClient, game_id: str, path: str, body: Any, uid: str):
    return client.post(f"/api/games/{game_id}/{path}", params={"uid": uid}, json=body)


def test_the_controller_casts_the_controlled_players_spell_from_their_mana(client, game):
    """CR 723.5a: bob's spell is paid from bob's mana. alice's own Mountain is
    untapped the whole time and never touched."""
    into_bobs_controlled_turn(client, game)
    force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")
    bobs = add_card(client, game, "Mountain", "bob", "battlefield")["added"]["card_id"]
    alices = add_card(client, game, "Mountain", "alice", "battlefield")["added"]["card_id"]
    bolt = add_card(client, game, "Lightning Bolt", "bob", "hand")["added"]["card_id"]
    cast = post(
        client,
        game,
        "actions/cast",
        {"player": "alice", "card_id": bolt, "targets": ["alice"], "mana_sources": [bobs]},
        uids(game)["alice"],
    )
    assert cast["allowed"] is True, cast
    board = state(client, game)
    tapped = {c["id"]: c["tapped"] for c in board["battlefield"]}
    assert tapped[bobs] is True and tapped[alices] is False


def test_a_seat_naming_another_player_is_refused_on_every_action_route(client, game):
    """A uid acts only for its own player, or for one it is deciding for."""
    stock_libraries(client, game)
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    forest = add_card(client, game, "Forest", "alice", "hand")["added"]["card_id"]
    land = add_card(client, game, "Forest", "alice", "battlefield")["added"]["card_id"]
    bob = uids(game)["bob"]
    attempts = [
        ("actions/play-land", {"player": "alice", "card_id": forest}),
        ("actions/pass-priority", {"player": "alice"}),
        ("advance", {"player": "alice"}),
        ("actions/concede", {"player": "alice"}),
        ("resolve-top", {"player": "alice"}),
        (f"cards/{land}/tap", {"tapped": True}),
        (f"cards/{land}/move", {"to_zone": "graveyard"}),
    ]
    for path, body in attempts:
        response = raw_post(client, game, path, body, bob)
        assert response.status_code == 403, (path, response.status_code, response.text)
        assert "seat" in response.json()["detail"], (path, response.text)
    assert state(client, game)["winner"] is None


def test_a_seat_acting_for_itself_is_unchanged(client, game):
    stock_libraries(client, game)
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    forest = add_card(client, game, "Forest", "alice", "hand")["added"]["card_id"]
    alice = uids(game)["alice"]
    played = raw_post(
        client, game, "actions/play-land", {"player": "alice", "card_id": forest}, alice
    )
    assert played.status_code == 200 and played.json()["allowed"] is True, played.text
    passed = raw_post(client, game, "actions/pass-priority", {"player": "alice"}, alice)
    assert passed.status_code == 200 and passed.json()["allowed"] is True, passed.text


def test_the_controller_cannot_make_the_controlled_player_concede(client, game):
    """CR 723.6: "The controller of another player can't make that player
    concede." """
    into_bobs_controlled_turn(client, game)
    response = raw_post(client, game, "actions/concede", {"player": "bob"}, uids(game)["alice"])
    assert response.status_code == 403, response.text
    assert "CR 723.6" in response.json()["detail"]
    assert state(client, game)["winner"] is None


def test_an_ai_controller_is_not_held_to_the_controlled_players_plan(client, game):
    """The plan gate is about the seat acting: alice (an agent seat) acting
    for bob on bob's turn is not bob acting on his own turn."""
    into_bobs_controlled_turn(client, game)
    store.register_ai_players(game, ["alice", "bob"])
    forest = add_card(client, game, "Forest", "bob", "battlefield")["added"]["card_id"]
    response = raw_post(
        client,
        game,
        "actions/activate",
        {"player": "bob", "card_id": forest, "ability_index": 0, "reasoning": "for bob"},
        uids(game)["alice"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is True, response.text


def test_the_controllers_own_permission_on_a_card_bob_owns_stays_the_controllers(client, game):
    """Hostage Taker: alice may cast bob's exiled creature "for as long as it
    remains exiled". Cast by alice during bob's controlled turn, it is alice's
    own cast under her own permission - never rewritten into bob's."""
    stock_libraries(client, game)
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    alice = uids(game)["alice"]
    borrower = add_card(client, game, "Brazen Borrower", "bob", "battlefield")["added"]["card_id"]
    lands = [
        add_card(client, game, name, "alice", "battlefield")["added"]["card_id"]
        for name in ("Island", "Island", "Swamp", "Swamp")
    ]
    taker = add_card(client, game, "Hostage Taker", "alice", "hand")["added"]["card_id"]
    cast = post(
        client,
        game,
        "actions/cast",
        {"player": "alice", "card_id": taker, "mana_sources": lands},
        alice,
    )
    assert cast["allowed"] is not False, cast
    for _ in range(6):
        board = state(client, game)
        pending = board["pending_decision"]
        if pending and pending["kind"] == "choose_targets":
            decided = post(
                client, game, "decide", {"player": "alice", "targets": [borrower]}, alice
            )
            assert decided["accepted"] is True, decided
            continue
        if not board["stack"]:
            break
        passer = board["priority_holder"]
        post(client, game, "actions/pass-priority", {"player": passer}, uids(game)[passer])
    exiled = [c for c in state(client, game)["exile"] if c["id"] == borrower]
    assert exiled, "Hostage Taker exiled Brazen Borrower"

    live = store.get(game)
    assert live is not None
    live.gain_control_of_player("alice", "bob")
    force_turn_position(game, Step.END_STEP, "alice")
    post(client, game, "actions/pass-priority", {"player": "alice"}, alice)
    post(client, game, "actions/pass-priority", {"player": "bob"}, uids(game)["bob"])
    assert state(client, game)["player_control"] == {"bob": "alice"}
    fresh = [
        add_card(client, game, "Island", "alice", "battlefield")["added"]["card_id"]
        for _ in range(3)
    ]
    cast = post(
        client,
        game,
        "actions/cast",
        {"player": "alice", "card_id": borrower, "mana_sources": fresh},
        alice,
    )
    assert cast["allowed"] is True, cast
    assert state(client, game)["stack"][0]["controller"] == "alice"


def test_a_control_effect_ending_mid_request_refuses_rather_than_acting(client, game, monkeypatch):
    """CR 723.1: the effect ends at the next turn's beginning. The rewrite is
    read again under the game lock; a request whose answer changed in between
    (here: the effect gone by the time the lock is held) is refused, and bob's
    land is never tapped as bob's."""
    from magic_cards_model_api import player_control

    into_bobs_controlled_turn(client, game)
    forest = add_card(client, game, "Forest", "bob", "battlefield")["added"]["card_id"]
    real = player_control.acting_as
    calls = {"n": 0}

    def ending(game_obj, kind, request, seat):
        calls["n"] += 1
        if calls["n"] > 1:
            return request, None  # the effect is gone: alice is only alice
        return real(game_obj, kind, request, seat)

    monkeypatch.setattr(player_control, "acting_as", ending)
    response = raw_post(
        client,
        game,
        "actions/activate",
        {"player": "alice", "card_id": forest, "ability_index": 0},
        uids(game)["alice"],
    )
    assert response.status_code == 400, response.text
    assert "CR 723.1" in response.json()["detail"]
    assert (
        next(c for c in state(client, game)["battlefield"] if c["id"] == forest)["tapped"] is False
    )


def test_the_controller_sees_none_of_the_controlled_players_memory_of_a_third():
    """CR 723.4 widens what is visible to the controlled player *now*, about
    objects in the game - their own hidden cards they know. What bob once
    saw of carol's hand is bob's memory, not information visible to him, and
    stays his alone."""
    from magic_cards_model_api import visibility

    def card(card_id: str, owner: str) -> dict:
        return {"id": card_id, "name": card_id, "owner": owner, "controller": owner}

    view = {
        "player_control": {"bob": "alice"},
        "hands": {"alice": [], "bob": [], "carol": []},
        "players": [],
        "known_cards": {
            "bob": {
                "hand": [card("carols-bolt", "carol"), card("bobs-own", "bob")],
                "library": [
                    {"index_from_top": 0, "source": "scry", "card": card("bobs-top", "bob")},
                    {"index_from_top": 0, "source": "look", "card": card("carols-top", "carol")},
                ],
                "sideboard": [card("bobs-side", "bob")],
            }
        },
    }
    seen = visibility.redact_view(view, "alice")["known_cards"]["bob"]
    assert [c["id"] for c in seen["hand"]] == ["bobs-own"]
    assert [r["card"]["id"] for r in seen["library"]] == ["bobs-top"]
    assert seen["sideboard"] == []


# -- security review of the seat binding ---------------------------------------


def test_a_seat_mints_a_credential_only_for_its_own_player(client, game, admin_key):
    alice, bob = uids(game)["alice"], uids(game)["bob"]
    stolen = client.post(f"/api/games/{game}/seats", params={"uid": bob}, json={"player": "alice"})
    assert stolen.status_code == 403, stolen.text
    own = client.post(f"/api/games/{game}/seats", params={"uid": alice}, json={"player": "alice"})
    assert own.status_code == 200 and own.json()["player"] == "alice", own.text
    admin = client.post(
        f"/api/games/{game}/seats", params={"key": admin_key}, json={"player": "bob"}
    )
    assert admin.status_code == 200, admin.text


def test_a_controller_cannot_mint_for_the_controlled_player(client, game):
    into_bobs_controlled_turn(client, game)
    response = client.post(
        f"/api/games/{game}/seats", params={"uid": uids(game)["alice"]}, json={"player": "bob"}
    )
    assert response.status_code == 403, response.text


def test_board_setup_routes_refuse_every_seat_uid(client, game, admin_key):
    """No seat client edits the board: those routes are the harness's and the
    operator's. A controller in particular may not zero the controlled
    player's life (CR 723.6 keeps conceding theirs; CR 723.5 hands over
    decisions, not the board)."""
    into_bobs_controlled_turn(client, game)
    card = add_card(client, game, "Forest", "bob", "battlefield")["added"]["card_id"]
    for seat in ("alice", "bob"):
        uid = uids(game)[seat]
        attempts = [
            ("patch", "players/bob", {"life": 0}),
            ("put", "players/bob/mana-pool", {"add": {"G": 1}}),
            ("post", f"cards/{card}/tap", {"tapped": True}),
            ("post", f"cards/{card}/move", {"to_zone": "graveyard"}),
            ("patch", f"cards/{card}", {"tapped": True}),
            ("post", "cards", {"card": "Island", "player": seat, "zone": "hand"}),
            ("put", "step", {"step": "end_step", "player": seat}),
            ("post", "state-based-actions", None),
        ]
        for method, path, body in attempts:
            response = getattr(client, method)(
                f"/api/games/{game}/{path}", params={"uid": uid}, json=body
            )
            assert response.status_code == 403, (seat, method, path, response.text)
    # resolve-top: alice decides bob's held priority (CR 723.5), bob does not.
    refused = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids(game)["bob"]})
    assert refused.status_code == 403, refused.text
    assert player_block(state(client, game), "bob")["life"] == 20
    admin = client.patch(
        f"/api/games/{game}/players/bob", params={"key": admin_key}, json={"life": 19}
    )
    assert admin.status_code == 200, admin.text


def test_control_ended_before_the_lock_refuses_the_controllers_pass(client, game, monkeypatch):
    """CR 723.1: once the effect has ended, alice is not bob's decider. The
    pass she sent in bob's name is re-checked under the lock and refused."""
    from magic_cards_model_api import player_control

    into_bobs_controlled_turn(client, game)
    real = player_control.may_decide_for
    calls = {"n": 0}

    def ended(game_obj, seat, player):
        calls["n"] += 1
        return real(game_obj, seat, player) if calls["n"] == 1 else False

    monkeypatch.setattr(player_control, "may_decide_for", ended)
    response = raw_post(
        client, game, "actions/pass-priority", {"player": "bob"}, uids(game)["alice"]
    )
    assert response.json()["allowed"] is False, response.text
    assert "CR 723.1" in response.json()["reason"]
    assert state(client, game)["priority_holder"] == "bob"


def test_a_card_in_a_graveyard_is_held_by_that_graveyards_player(client, game):
    """A stored controller outlives zone changes; only on the battlefield or
    the stack does it name who holds the card."""
    from uuid import UUID

    from magic_cards_model_api import game_access

    card = add_card(client, game, "Lightning Bolt", "alice", "graveyard")["added"]["card_id"]
    live = store.get(game)
    assert live is not None
    stale = live.find_by_id(UUID(card))
    assert stale is not None
    stale.stored_controller = "bob"  # left over from bob casting it
    assert game_access._card_holder(live, card) == "alice"
