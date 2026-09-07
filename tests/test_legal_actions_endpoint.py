"""`GET /api/games/{game_id}/legal-actions`: the api-side door onto Phase 1's
enumeration surface, `machines.legal_actions.legal_actions(game, player)`
(`magic-cards-model`).

T11 replaces `seatkit.available_actions`'s brute-force probe - one HTTP
`dry_run` round trip per hand card, per graveyard/exile card with a cast
option, and per battlefield ability, each collapsed into a bare boolean - with
one call into the engine's own enumeration, which reports an `Affordability`
verdict (`"yes"`/`"no"`/`"unknown"`) alongside every action rather than
hiding the `unknown` case the old probe could never represent (a dry run
either succeeds or it does not).

This file pins the endpoint itself; `test_action_flow.py` and
`test_graveyard_cast_options.py` pin `seatkit.available_actions`'s own new
shape, built on top of it.
"""

from __future__ import annotations

from conftest import add_card, find, state
from test_decisions import advance_into_cleanup, stuff_hand


def legal_actions(client, game_id: str, player: str = "alice") -> list[dict]:
    response = client.get(f"/api/games/{game_id}/legal-actions", params={"player": player})
    assert response.status_code == 200, response.text
    return response.json()["actions"]


def by_kind(actions: list[dict], kind: str) -> list[dict]:
    return [action for action in actions if action["kind"] == kind]


def test_pass_priority_and_concede_are_always_offered_and_affordable(client, game):
    actions = legal_actions(client, game)
    (pass_priority,) = by_kind(actions, "pass_priority")
    (concede,) = by_kind(actions, "concede")
    assert pass_priority["affordable"] == "yes"
    assert concede["affordable"] == "yes"
    assert pass_priority["rule"] == "CR 117.3d"
    assert concede["rule"] == "CR 104.3a"


def test_a_payable_cast_is_reported_affordable(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")

    (cast,) = [
        a for a in by_kind(legal_actions(client, game), "cast") if a["card_id"] == bolt["id"]
    ]
    assert cast["affordable"] == "yes"
    assert cast["mana_cost"] == "{R}"
    assert cast["unaffordable_reason"] is None


def test_an_unpayable_cast_is_reported_not_affordable_with_the_engines_own_reason(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")

    (cast,) = [
        a for a in by_kind(legal_actions(client, game), "cast") if a["card_id"] == bolt["id"]
    ]
    assert cast["affordable"] == "no"
    assert cast["unaffordable_reason"]


def test_an_x_spell_before_x_is_announced_is_reported_unknown_not_flattened_to_no(client, game):
    """CR 107.3a: X is not announced until the spell is actually cast, so an
    enumeration asked before that point cannot say yes or no - it has to say
    it does not know yet. The old dry-run probe had no way to represent this
    at all (a dry run either succeeds or it does not); this is the case T11
    exists to stop hiding."""
    for _ in range(3):
        add_card(client, game, "Wastes", "alice", "battlefield")
    add_card(client, game, "Kozilek's Command", "alice", "hand")
    koz = find(state(client, game)["hands"]["alice"], "Kozilek's Command")

    (cast,) = [a for a in by_kind(legal_actions(client, game), "cast") if a["card_id"] == koz["id"]]
    assert cast["affordable"] == "unknown"
    assert "X" in cast["unaffordable_reason"]


def test_a_pending_decision_narrows_the_pool_to_its_own_answers_for_the_player_it_addresses(
    client, game
):
    """`legal_actions`'s own module docstring: while a `Decision` is
    outstanding, the legal actions ARE its legal answers and nothing else,
    for the player it addresses - and nothing at all for anyone else, since
    nothing else in the engine's own execution can happen until it is
    answered."""
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard: a real choice
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    paused = advance_into_cleanup(game)
    assert paused is not None, "the discard should have asked, not defaulted silently"
    assert paused["pending_decision"]["kind"] == "choose_cards"

    alice_actions = legal_actions(client, game, player="alice")
    assert alice_actions, "alice's own pending decision should offer its answers"
    assert all(a["kind"] == "answer_decision" for a in alice_actions)
    assert all(a["decision_candidates"] for a in alice_actions if a.get("x_range"))

    bob_actions = legal_actions(client, game, player="bob")
    assert bob_actions == [], "nothing else is legal while alice's own decision stands"
