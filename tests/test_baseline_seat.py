"""The greedy baseline seat: a deterministic, zero-LLM opponent.

Every test here drives the real API - the same `GET .../legal-actions`
enumeration a live seat reads, and the same mutating routes it posts to -
because the whole claim the baseline makes is "this policy is expressible in
what the engine already offers a seat". A policy asserted against a
hand-written action list would prove nothing about that.

The last test is the one the measuring stick actually rests on: two baselines
play a bounded game and it *ends*. A baseline that can stall is not a
measuring stick, it is a second source of unfinished games.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import add_card, find, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from magic_cards_model.machines.decisions import Decision, DecisionKind
from magic_cards_model.models.card_state import TargetRef

from magic_cards_model_api import baseline_seat
from magic_cards_model_api.api_client import HttpSeatApiClient
from magic_cards_model_api.decisions import PendingDecision
from magic_cards_model_api.store import store

# -- harness -----------------------------------------------------------------


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def api(client: TestClient) -> HttpSeatApiClient:
    """The seat seam (`api_client.py`) pointed at the in-process app."""
    return HttpSeatApiClient(http=client)


def look(client: TestClient, game_id: str, player: str) -> tuple[dict, list[dict]]:
    """What the policy is given: this seat's own filtered state view and its
    own `GET .../legal-actions` enumeration, both read through the seam."""
    uid = seats_for(game_id)[player]
    seam = api(client)
    return seam.game_state(game_id, uid), seam.legal_actions(game_id, player, uid)


def decide(client: TestClient, game_id: str, player: str, **kwargs: Any):
    board, actions = look(client, game_id, player)
    return baseline_seat.choose_move(board, actions, player, **kwargs)


def pending_view(decision: Decision) -> dict[str, Any]:
    """A real engine `Decision` in the exact shape the wire carries it."""
    return PendingDecision.from_engine(decision).to_view()


def battlefield_creature(
    client: TestClient, game_id: str, name: str, controller: str, *, sick: bool = False
) -> str:
    """Add one creature and hand back *its own* id.

    By id and not by name: two copies of one card are a real board, and
    `conftest.find` answers with the first match - so a helper that looked
    the card up by name handed the same id back twice and the second attack
    declaration named a creature that was already attacking.
    """
    before = {card["id"] for card in state(client, game_id)["battlefield"]}
    add_card(client, game_id, name, controller, "battlefield")
    added = [card for card in state(client, game_id)["battlefield"] if card["id"] not in before]
    assert len(added) == 1, added
    client.patch(f"/api/games/{game_id}/cards/{added[0]['id']}", json={"summoning_sick": sick})
    return added[0]["id"]


def into_declare_attackers(client: TestClient, game_id: str, attacker: str) -> dict:
    """Walk `attacker` into CR 508.1's own pause, the real way."""
    force_turn_position(game_id, Step.BEGINNING_OF_COMBAT, attacker)
    paused = client.post(f"/api/games/{game_id}/advance", json={"player": attacker})
    body = paused.json()
    assert body.get("paused") is True, body
    assert body["pending_decision"]["kind"] == "declare_attackers", body
    return body


def into_declare_blockers(
    client: TestClient, game_id: str, attacker: str, pairs: list[dict[str, str]]
) -> dict:
    into_declare_attackers(client, game_id, attacker)
    declared = client.post(
        f"/api/games/{game_id}/actions/attack",
        params={"uid": seats_for(game_id)[attacker]},
        json={"attackers": pairs},
    )
    assert declared.json()["allowed"] is True, declared.json()
    defender = next(p for p in ("alice", "bob") if p != attacker)
    advanced = client.post(f"/api/games/{game_id}/advance", json={"player": attacker})
    body = advanced.json()
    assert body.get("paused") is True, body
    assert body["pending_decision"]["kind"] == "declare_blockers", body
    assert body["pending_decision"]["player"] == defender, body
    return body


def set_life(client: TestClient, game_id: str, player: str, life: int) -> None:
    response = client.patch(f"/api/games/{game_id}/players/{player}", json={"life": life})
    assert response.status_code == 200, response.text


# -- one rule at a time ------------------------------------------------------


def test_plays_a_land_when_one_is_legal(client, game):
    add_card(client, game, "Forest", "alice", "hand")
    forest = state(client, game)["hands"]["alice"][0]["id"]

    move = decide(client, game, "alice")

    assert move is not None
    assert move.label == "play_land"
    assert move.path == "actions/play-land"
    assert move.body["card_id"] == forest


def test_the_land_comes_before_any_cast(client, game):
    """A land drop is free and never comes back this turn; a spell can wait
    for the mana the land just added."""
    for _ in range(2):
        add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "alice", "hand")

    move = decide(client, game, "alice")

    assert move.label == "play_land"


def test_casts_the_highest_cost_affordable_permanent(client, game):
    for _ in range(4):
        add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")  # {1}{G}
    add_card(client, game, "Lupine Harbingers", "alice", "hand")  # {3}{G}

    move = decide(client, game, "alice")

    assert move.label == "cast_permanent"
    assert move.path == "actions/cast"
    hand = state(client, game)["hands"]["alice"]
    assert move.body["card_id"] == find(hand, "Lupine Harbingers")["id"]


def test_skips_a_permanent_it_cannot_pay_for(client, game):
    """CR 118.6 keeps an unpayable cast *legal*, so the enumeration still
    offers it - `affordable` is the only thing that tells the two apart, and
    a greedy policy that read legality alone would burn every turn refused."""
    for _ in range(2):
        add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")  # {1}{G} - payable
    add_card(client, game, "Lupine Harbingers", "alice", "hand")  # {3}{G} - not

    move = decide(client, game, "alice")

    hand = state(client, game)["hands"]["alice"]
    assert move.body["card_id"] == find(hand, "Grizzly Bears")["id"]


def test_permanents_come_before_spells_whatever_they_cost(client, game):
    """The rule is "the highest-cost affordable *permanent*, then the
    highest-cost affordable spell" - a board beats a one-shot even when the
    one-shot is the more expensive card."""
    for _ in range(4):
        add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Savannah Lions", "alice", "hand")  # {W} - unpayable
    add_card(client, game, "Mind Stone", "alice", "hand")  # {2}, a permanent
    add_card(client, game, "Mind Rot", "alice", "hand")  # {2}{B} - unpayable
    add_card(client, game, "Lightning Bolt", "alice", "hand")  # {R}, a spell

    move = decide(client, game, "alice")

    hand = state(client, game)["hands"]["alice"]
    assert move.label == "cast_permanent"
    assert move.body["card_id"] == find(hand, "Mind Stone")["id"]


def test_casts_the_highest_cost_affordable_spell_when_no_permanent_is_left(client, game):
    for _ in range(4):
        add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")  # {R}
    add_card(client, game, "Tablet of Discovery", "alice", "hand")  # {2}{R} permanent

    # With the permanent gone from hand, the spell is the only cast left.
    hand = state(client, game)["hands"]["alice"]
    tablet = find(hand, "Tablet of Discovery")["id"]
    client.post(f"/api/games/{game}/cards/{tablet}/move", json={"to_zone": "graveyard"})

    move = decide(client, game, "alice")

    assert move.label == "cast_spell"
    assert move.body["card_id"] == find(state(client, game)["hands"]["alice"], "Shock")["id"]


def test_passes_priority_when_nothing_is_worth_doing(client, game):
    move = decide(client, game, "alice")

    assert move.label == "pass_priority"
    assert move.path == "actions/pass-priority"
    assert move.body["player"] == "alice"


def test_passes_priority_rather_than_advancing_while_the_stack_is_busy(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    mountain = find(state(client, game)["battlefield"], "Mountain")["id"]
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = state(client, game)["hands"]["alice"][0]["id"]
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt,
            "targets": ["bob"],
            "mana_sources": [mountain],
        },
    )
    assert cast.json()["allowed"] is True, cast.json()

    move = decide(client, game, "alice")

    assert move.label == "pass_priority"


def test_advances_when_the_step_gives_nobody_priority(client, game):
    """CR 703.2's turn-based actions are nobody's move, so a seat waiting for
    priority in untap waits forever - `advance` is the only way out."""
    force_turn_position(game, Step.UNTAP, "alice")

    move = decide(client, game, "alice")

    assert move.label == "advance"
    assert move.path == "advance"
    assert move.body["player"] == "alice"


def test_has_nothing_to_do_on_the_opponents_priority(client, game):
    move = decide(client, game, "bob")

    assert move is None


def test_stops_once_the_game_is_over(client, game):
    client.post(f"/api/games/{game}/actions/concede", json={"player": "bob"})

    assert decide(client, game, "alice") is None


# -- combat ------------------------------------------------------------------


def test_attacks_when_the_attacker_survives_every_block(client, game):
    """4/4 into a 2/2: nothing bob controls both kills it and lives, so the
    attack is free."""
    harbingers = battlefield_creature(client, game, "Lupine Harbingers", "alice")
    battlefield_creature(client, game, "Grizzly Bears", "bob")
    into_declare_attackers(client, game, "alice")

    move = decide(client, game, "alice")

    assert move.label == "attack"
    assert move.path == "actions/attack"
    assert move.body["attackers"] == [{"card_id": harbingers, "defender": "bob"}]


def test_holds_back_an_attacker_the_defender_would_eat(client, game):
    """2/2 into a 4/4: the block kills the attacker and the blocker lives, so
    a greedy-but-not-suicidal policy declares nobody (CR 508.1a's "if any")."""
    battlefield_creature(client, game, "Grizzly Bears", "alice")
    battlefield_creature(client, game, "Lupine Harbingers", "bob")
    into_declare_attackers(client, game, "alice")

    move = decide(client, game, "alice")

    assert move.label == "attack"
    assert move.body["attackers"] == []


def test_a_tapped_blocker_is_not_a_block_to_fear(client, game):
    bears = battlefield_creature(client, game, "Grizzly Bears", "alice")
    big = battlefield_creature(client, game, "Lupine Harbingers", "bob")
    tapped = client.post(f"/api/games/{game}/cards/{big}/tap", json={"tapped": True})
    assert tapped.json()["tapped"] is True, tapped.json()
    into_declare_attackers(client, game, "alice")

    move = decide(client, game, "alice")

    assert move.body["attackers"] == [{"card_id": bears, "defender": "bob"}]


def test_alpha_strikes_when_the_race_is_lethal(client, game):
    """Two 2/2s into a 4/4 blocker is a bad attack card by card - and the
    right one anyway at 3 life, because the damage that gets through wins."""
    first = battlefield_creature(client, game, "Grizzly Bears", "alice")
    second = battlefield_creature(client, game, "Grizzly Bears", "alice")
    battlefield_creature(client, game, "Lupine Harbingers", "bob")
    set_life(client, game, "bob", 3)
    into_declare_attackers(client, game, "alice")

    move = decide(client, game, "alice")

    assert move.label == "attack"
    assert sorted(pair["card_id"] for pair in move.body["attackers"]) == sorted([first, second])


def test_declines_to_block_when_the_hit_is_survivable(client, game):
    """ "Block only to prevent lethal" - at 20 life a 4/4 is not lethal, and a
    2/2 chump for nothing is exactly what the rule forbids."""
    attacker = battlefield_creature(client, game, "Lupine Harbingers", "alice")
    battlefield_creature(client, game, "Grizzly Bears", "bob")
    into_declare_blockers(client, game, "alice", [{"card_id": attacker, "defender": "bob"}])

    move = decide(client, game, "bob")

    assert move.label == "block"
    assert move.path == "actions/declare-blockers"
    assert move.body["blocks"] == []


def test_blocks_to_prevent_lethal(client, game):
    attacker = battlefield_creature(client, game, "Lupine Harbingers", "alice")
    blocker = battlefield_creature(client, game, "Grizzly Bears", "bob")
    set_life(client, game, "bob", 3)
    into_declare_blockers(client, game, "alice", [{"card_id": attacker, "defender": "bob"}])

    move = decide(client, game, "bob")

    assert move.label == "block"
    assert move.body["blocks"] == [{"blocker": blocker, "blocked": attacker}]
    assert move.body["player"] == "bob"


def test_blocks_no_more_than_lethal_demands(client, game):
    """Two 2/2s attacking into 3 life: one block takes the incoming damage
    below the life total, and the second blocker stays home."""
    first = battlefield_creature(client, game, "Grizzly Bears", "alice")
    second = battlefield_creature(client, game, "Grizzly Bears", "alice")
    battlefield_creature(client, game, "Grizzly Bears", "bob")
    battlefield_creature(client, game, "Savannah Lions", "bob")
    set_life(client, game, "bob", 3)
    into_declare_blockers(
        client,
        game,
        "alice",
        [{"card_id": first, "defender": "bob"}, {"card_id": second, "defender": "bob"}],
    )

    move = decide(client, game, "bob")

    assert len(move.body["blocks"]) == 1


# -- the narrowing a refused declaration walks down --------------------------


def test_the_attack_narrowing_is_monotonic_and_bottoms_out_empty(client, game):
    """`drop` is the only thing between a refused declaration and an
    unbounded re-post loop, so the sequence it generates has to be a descent
    that ENDS.

    The arithmetic was `chosen[: len(chosen) - drop]`, which is a negative
    slice the moment `drop` passes `len(chosen)`: with two pairs, `drop == 3`
    asked for `chosen[:-1]` and put a pair BACK. The answers oscillated
    `2, 1, 0, 1, 0, ...` instead of bottoming out, so a seat narrowing past
    the end re-offered a declaration it had already had refused.
    """
    first = battlefield_creature(client, game, "Grizzly Bears", "alice")
    second = battlefield_creature(client, game, "Grizzly Bears", "alice")
    into_declare_attackers(client, game, "alice")
    board, actions = look(client, game, "alice")

    sizes = [
        len(baseline_seat.attack_declaration(board, actions, "alice", drop=drop))
        for drop in range(6)
    ]

    assert sizes[0] == 2, (sizes, first, second)
    assert sizes == sorted(sizes, reverse=True), sizes
    assert sizes[2:] == [0, 0, 0, 0], sizes


def test_the_block_narrowing_is_monotonic_and_bottoms_out_empty(client, game):
    """The same arithmetic, the same defect, on the other declaration."""
    first = battlefield_creature(client, game, "Grizzly Bears", "alice")
    second = battlefield_creature(client, game, "Grizzly Bears", "alice")
    battlefield_creature(client, game, "Grizzly Bears", "bob")
    battlefield_creature(client, game, "Savannah Lions", "bob")
    set_life(client, game, "bob", 2)
    into_declare_blockers(
        client,
        game,
        "alice",
        [{"card_id": first, "defender": "bob"}, {"card_id": second, "defender": "bob"}],
    )
    board, actions = look(client, game, "bob")

    sizes = [
        len(baseline_seat.block_declaration(board, actions, "bob", drop=drop)) for drop in range(6)
    ]

    assert sizes[0] == 2, sizes
    assert sizes == sorted(sizes, reverse=True), sizes
    assert sizes[2:] == [0, 0, 0, 0], sizes


def test_a_refused_declaration_is_narrowed_and_then_dropped_not_re_posted(client, game):
    """The empty declaration is a fixed point, and that is what made the
    narrowing unbounded rather than merely imprecise.

    A blocking requirement (CR 509.1a) is the obvious way for the engine to
    refuse the empty answer, and `drop` cannot answer it: every further step
    re-posts the identical empty body, which the old docstring called "the
    narrowing always terminates". Bounded now - one post per distinct answer
    and no more, and then the seat has nothing left to say, which is what
    lets `play_out`'s stall guard see an idle round at all.
    """
    first = battlefield_creature(client, game, "Grizzly Bears", "alice")
    second = battlefield_creature(client, game, "Grizzly Bears", "alice")
    battlefield_creature(client, game, "Grizzly Bears", "bob")
    set_life(client, game, "bob", 2)
    into_declare_blockers(
        client,
        game,
        "alice",
        [{"card_id": first, "defender": "bob"}, {"card_id": second, "defender": "bob"}],
    )
    seam = RefusingSeam(api(client))
    seat = baseline_seat.BaselineSeat(seam, game, "bob", seats_for(game)["bob"])

    posted = []
    for _ in range(8):
        move = seat.step()
        if move is None:
            break
        posted.append(move.body["blocks"])

    assert [len(blocks) for blocks in posted] == [1, 0], posted
    assert seat.step() is None, "a declaration with nothing left to narrow is not re-posted"
    assert seat.advanced is False


# -- decisions, on the engine's own default_answer terms ----------------------


def _decision(kind: DecisionKind, **fields: Any) -> Decision:
    return Decision(player="alice", kind=kind, prompt="?", **fields)


def test_a_may_question_takes_the_affirmative_when_it_is_legal():
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.MAY, affirmative_is_legal=True)), "alice"
    )
    assert body["accept"] is True


def test_a_may_question_declines_when_the_affirmative_is_illegal():
    """CR 608.2d: `default_answer` never returns an illegal option, and
    neither does this."""
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.PAY_LIFE, affirmative_is_legal=False)), "alice"
    )
    assert body["accept"] is False


def test_a_may_question_with_a_stated_default_takes_it_over_the_generic_one():
    """`machines/decisions.default_answer` (engine schema 41): a `MAY`
    decision whose caller wrote its own deviation down (`Decision
    .default_may` - `Game.may`'s "you may shuffle" clause is the one caller
    that stamps `False`) takes *that* answer, not the engine-wide "take it
    when legal" every other `may` defaults to - even though the affirmative
    is perfectly legal here. `decide_body` claims to follow `default_answer`
    branch for branch; this is the branch a plain `affirmative_is_legal`
    check alone cannot express."""
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.MAY, affirmative_is_legal=True, default_may=False)),
        "alice",
    )
    assert body["accept"] is False


def test_a_may_question_with_no_stated_default_still_takes_the_affirmative():
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.MAY, affirmative_is_legal=True, default_may=None)),
        "alice",
    )
    assert body["accept"] is True


def test_a_mulligan_question_keeps_the_dealt_hand():
    body = baseline_seat.decide_body(pending_view(_decision(DecisionKind.MULLIGAN)), "alice")
    assert body["accept"] is False


def test_an_amount_question_pays_nothing():
    life = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.PAY_LIFE_AMOUNT, amount=7)), "alice"
    )
    energy = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.PAY_ENERGY_AMOUNT, amount=4)), "alice"
    )
    chosen = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.CHOOSE_AN_AMOUNT, amount=3)), "alice"
    )
    assert life["life_amount"] == 0
    assert energy["energy_amount"] == 0
    assert chosen["amount"] == 0


def test_put_into_hand_or_graveyard_takes_the_hand():
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.PUT_INTO_HAND_OR_GRAVEYARD)), "alice"
    )
    assert body["to_hand"] is True


def test_put_on_top_or_bottom_keeps_it_on_top():
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.PUT_ON_TOP_OR_BOTTOM)), "alice"
    )
    assert body["top"] is True


def test_a_scry_keeps_everything_on_top():
    from uuid import uuid4

    cards = (uuid4(), uuid4())
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.SCRY, cards=cards, amount=2)), "alice"
    )
    assert body["moved"] == []
    assert body["top_order"] is None


def test_a_dredge_declines_and_draws_normally():
    """CR 702.52a's "you may instead": the baseline's own default for a
    `DREDGE` decision is an empty `card_ids`, the same "leave the more
    ordinary outcome alone" posture as `SCRY`/`SURVEIL` above - and that
    empty tuple is exactly what `/decide`'s own validator
    (`decisions.py:validate_answer`'s `DREDGE` branch) accepts as "at most
    one card", so the fallback seat never stalls on a dredge decision."""
    from uuid import uuid4

    cards = (uuid4(), uuid4())
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.DREDGE, cards=cards)), "alice"
    )
    assert body["card_ids"] == []


def test_trigger_order_keeps_the_order_they_triggered_in():
    from uuid import uuid4

    cards = (uuid4(), uuid4(), uuid4())
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.ORDER_TRIGGERS, cards=cards)), "alice"
    )
    assert body["order"] == [str(card) for card in cards]


def test_an_exact_choose_cards_takes_the_back_of_the_offered_cards():
    from uuid import uuid4

    cards = tuple(uuid4() for _ in range(4))
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.CHOOSE_CARDS, cards=cards, amount=2, exact=True)),
        "alice",
    )
    assert body["card_ids"] == [str(cards[2]), str(cards[3])]


def test_an_any_number_choose_cards_takes_none():
    from uuid import uuid4

    cards = tuple(uuid4() for _ in range(3))
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.CHOOSE_CARDS, cards=cards, amount=3, exact=False)),
        "alice",
    )
    assert body["card_ids"] == []


def test_a_choose_cards_with_a_stated_default_takes_it():
    """CR 701.23b's declinable search stamps its own default - finding
    nothing would sacrifice a fetchland for no land at all."""
    from uuid import uuid4

    cards = tuple(uuid4() for _ in range(3))
    body = baseline_seat.decide_body(
        pending_view(
            _decision(
                DecisionKind.CHOOSE_CARDS,
                cards=cards,
                amount=1,
                exact=False,
                default_cards=(cards[1],),
            )
        ),
        "alice",
    )
    assert body["card_ids"] == [str(cards[1])]


def test_choose_targets_names_exactly_the_mandatory_minimum():
    from uuid import uuid4

    targets = (TargetRef.to_card(uuid4()), TargetRef.to_player("bob"))
    body = baseline_seat.decide_body(
        pending_view(
            _decision(
                DecisionKind.CHOOSE_TARGETS, targets=targets, target_minimum=1, target_maximum=1
            )
        ),
        "alice",
    )
    assert body["targets"] == [str(targets[0].card_id)]


def test_an_optional_target_requirement_names_nobody():
    from uuid import uuid4

    targets = (TargetRef.to_card(uuid4()),)
    body = baseline_seat.decide_body(
        pending_view(
            _decision(
                DecisionKind.CHOOSE_TARGETS, targets=targets, target_minimum=0, target_maximum=1
            )
        ),
        "alice",
    )
    assert body["targets"] == []


def test_an_enumerated_quality_takes_the_first_printed_option():
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.CHOOSE_A_QUALITY, options=("W", "U", "B", "R", "G"))),
        "alice",
    )
    assert body["choice"] == "W"


def test_an_open_quality_names_the_asking_card():
    """`default_answer` returns `""` for "choose a card name"/"choose a
    creature type", and the wire refuses an empty `choice` outright - so the
    one place the default is unsendable names the card that asked, which is
    a real answer and never a stall."""
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.CHOOSE_A_QUALITY, source="Pithing Needle")), "alice"
    )
    assert body["choice"] == "Pithing Needle"


def test_a_modal_question_takes_the_first_printed_mode():
    body = baseline_seat.decide_body(
        pending_view(_decision(DecisionKind.CHOOSE_A_MODE, options=("draw a card", "gain 3 life"))),
        "alice",
    )
    assert body["choice"] == "draw a card"


def test_every_answerable_decision_kind_has_a_default_body():
    """A kind with no branch here is a kind the baseline stalls on."""
    from magic_cards_model_api.decisions import ANSWER_FIELD_FOR_KIND

    missing = [
        kind.value
        for kind in ANSWER_FIELD_FOR_KIND
        if kind not in (DecisionKind.DECLARE_ATTACKERS, DecisionKind.DECLARE_BLOCKERS)
        and not baseline_seat.answers_kind(kind.value)
    ]
    assert missing == []


def test_the_decision_body_carries_the_generation_it_answers():
    view = pending_view(_decision(DecisionKind.MAY))
    view["generation"] = 4
    body = baseline_seat.decide_body(view, "alice")
    assert body["generation"] == 4


def test_a_decision_addressed_to_the_other_seat_is_not_ours(client, game):
    attacker = battlefield_creature(client, game, "Lupine Harbingers", "alice")
    into_declare_blockers(client, game, "alice", [{"card_id": attacker, "defender": "bob"}])

    assert decide(client, game, "alice") is None


# -- the pregame -------------------------------------------------------------


def test_keeps_the_dealt_hand_in_the_mulligan_step(client):
    """CR 103.5 rests the game before its first turn and nobody has priority
    there - a seat that only knows how to pass would wait out the clock."""
    response = client.post(
        "/api/games",
        json={"players": [{"username": "alice"}, {"username": "bob"}], "step": "mulligan"},
    )
    game_id = response.json()["game_id"]
    try:
        board, actions = look(client, game_id, "alice")
        move = baseline_seat.choose_move(board, actions, "alice")
        assert move is not None
        assert move.label == "mulligan_keep"
        assert move.path == "pregame/mulligan"
        assert move.body["take"] is False
    finally:
        store.delete(game_id)


# -- refusals must not become loops ------------------------------------------


def test_a_refused_action_is_not_offered_again(client, game):
    for _ in range(4):
        add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    add_card(client, game, "Lupine Harbingers", "alice", "hand")

    first = decide(client, game, "alice")
    again = decide(client, game, "alice", refused={first.key})

    assert again.key != first.key
    hand = state(client, game)["hands"]["alice"]
    assert again.body["card_id"] == find(hand, "Grizzly Bears")["id"]


def test_every_option_refused_falls_through_to_passing(client, game):
    add_card(client, game, "Forest", "alice", "hand")
    board, actions = look(client, game, "alice")
    every_key = {baseline_seat.action_key(action) for action in actions}

    move = baseline_seat.choose_move(board, actions, "alice", refused=every_key)

    assert move.label == "pass_priority"


# -- and the whole point: it never stalls ------------------------------------


@pytest.mark.parametrize("seed_life", [3, 20])
def test_two_baselines_play_a_bounded_game_to_an_end_state(client, seed_life):
    """The anti-stall guarantee, exercised rather than asserted.

    An empty library is deliberate: nothing here is trying to be a real game
    of Magic, it is trying to prove the loop terminates. Both seats draw from
    an empty library (CR 104.3c) within a few turns, so the run ends on the
    engine's own terms rather than on the harness's bound - and the bound is
    still there, and still asserted, because a policy that needed it would
    be exactly the defect this test is for.
    """
    response = client.post(
        "/api/games",
        json={
            "players": [
                {"username": "alice", "life": seed_life},
                {"username": "bob", "life": seed_life},
            ],
            "step": "precombat_main",
        },
    )
    info = response.json()
    game_id = info["game_id"]
    try:
        add_card(client, game_id, "Grizzly Bears", "alice", "hand")
        add_card(client, game_id, "Forest", "alice", "hand")
        add_card(client, game_id, "Savannah Lions", "bob", "hand")
        add_card(client, game_id, "Plains", "bob", "hand")
        seam = api(client)
        seats = [
            baseline_seat.BaselineSeat(seam, game_id, player, uid)
            for uid, player in store.seat_uids(game_id).items()
        ]
        outcome = baseline_seat.play_out(seats, max_steps=800)

        assert outcome["over"] is True, outcome
        assert outcome["ended_by"] == "play", outcome
        assert outcome["winner"] or outcome["drawn"], outcome
        assert outcome["steps"] < 800, outcome
        board = state(client, game_id)
        assert board["winner"] or board["drawn"]
    finally:
        store.delete(game_id)


def test_the_stall_guard_ends_a_game_the_policy_cannot_finish(client, game, monkeypatch):
    """The backstop, on its own. With the policy forced to have nothing to
    say, `play_out` must still reach an end state rather than spin: a seat
    that runs out of moves concedes (CR 104.3a), which is a real ending and
    an honest one - it shows up in the scorecard as a loss.
    """
    monkeypatch.setattr(baseline_seat, "choose_move", lambda *a, **k: None)
    seam = api(client)
    seats = [
        baseline_seat.BaselineSeat(seam, game, player, uid)
        for uid, player in store.seat_uids(game).items()
    ]

    outcome = baseline_seat.play_out(seats, max_steps=200)

    assert outcome["over"] is True, outcome
    assert outcome["ended_by"] == "stall_guard", outcome
    assert state(client, game)["winner"] is not None


class RefusingSeam:
    """A `SeatApiClient` whose every *action* comes back refused, and whose
    reads are the real app's.

    A legitimate test double here (this is the api repo, not the engine - see
    `CountingSeam` for the same note). The reads it passes straight through
    are what make the refusals interesting: the board they describe is real
    and, because a refused action changes nothing, unchanged from one round
    to the next - which is exactly the board a stall happens on.

    `actions/concede` is the one path it lets through, because a concession
    is not one of the policy's moves: it is `play_out`'s own backstop, and a
    double that refused it would be asserting nothing about the guard.
    """

    def __init__(self, inner: HttpSeatApiClient) -> None:
        self.inner = inner
        self.attempts: list[str] = []

    def game_state(self, game_id, uid):
        return self.inner.game_state(game_id, uid)

    def legal_actions(self, game_id, player, uid, *, candidates=False):
        return self.inner.legal_actions(game_id, player, uid, candidates=candidates)

    @property
    def board_seq(self):
        # Delegated, not defaulted: `SeatApiClient.board_seq` is what lets a
        # seat name the board it decided against, and a double answering
        # `None` would quietly put every refusal it collected back in the
        # report's `unknown` bucket.
        return self.inner.board_seq

    def act(self, game_id, path, body, uid):
        self.attempts.append(path)
        if path == "actions/concede":
            return self.inner.act(game_id, path, body, uid)
        return {"allowed": False, "error": "refused by RefusingSeam"}


def test_a_refused_move_is_not_progress(client, game):
    """The hole finding 1 lived in. `step` returns the move it made whether
    the api ACCEPTED or REFUSED it, and `play_out` read that return value as
    "this seat acted" - so `idle_rounds` reset every round for as long as a
    seat kept attempting anything, and the concession backstop could never
    fire.

    Progress is what the guard has to count, and a refusal is progress only
    the first time: it narrows what this seat has left to try against this
    board. Once there is nothing left, the round is idle and says so.
    """
    add_card(client, game, "Forest", "alice", "hand")
    accepted = baseline_seat.BaselineSeat(api(client), game, "alice", seats_for(game)["alice"])

    assert accepted.step() is not None
    assert accepted.advanced is True, "a move the api took is progress"

    seam = RefusingSeam(api(client))
    refused = baseline_seat.BaselineSeat(seam, game, "alice", seats_for(game)["alice"])

    move = refused.step()

    assert move is not None, "the policy still had something to try"
    assert refused.refusals == 1
    assert refused.advanced is True, "the first refusal taught this seat something"
    assert refused.step() is None, "and then there is nothing left to try"
    assert refused.advanced is False, "which is an idle round, whatever was attempted before"


def test_a_seat_whose_every_move_is_refused_still_reaches_an_end_state(client, game):
    """The guarantee this module's own prose makes, on the path the suite did
    not cover.

    `test_the_stall_guard_ends_a_game_the_policy_cannot_finish` drives a seat
    with nothing to SAY (`choose_move` forced to `None`), which is the easy
    half. The half that shipped broken is a seat that keeps ATTEMPTING things
    and has every one of them refused: `acted` was true every round, the
    guard never fired, and the run spun until `max_steps` - 100,000 in the
    gauntlet - and was reported as a `timeout`, which is the unfinished game
    the scorecard exists to count.
    """
    add_card(client, game, "Forest", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    seam = RefusingSeam(api(client))
    seats = [
        baseline_seat.BaselineSeat(seam, game, player, uid)
        for uid, player in store.seat_uids(game).items()
    ]

    outcome = baseline_seat.play_out(seats, max_steps=300)

    assert outcome["over"] is True, outcome
    assert outcome["ended_by"] == "stall_guard", outcome
    assert outcome["steps"] < 300, outcome
    assert state(client, game)["winner"] is not None
    assert "actions/concede" in seam.attempts, seam.attempts


def test_a_refused_move_that_no_enumerated_row_backs_is_not_re_posted(client, game):
    """`pass_priority`, `advance`, `decide` and the two pregame declarations
    all carry `key == ""`, so a refusal of one could not be remembered by
    key. What the code did instead was drop `self.signature`, which forced
    the next round to re-derive from scratch - and re-deriving produced the
    identical move, having thrown away this board's refusal memory on the
    way.

    Two posts of one byte-identical body against a board that has not moved
    is the loop. There is one post now, and then nothing to say.
    """
    seam = RefusingSeam(api(client))
    seat = baseline_seat.BaselineSeat(seam, game, "alice", seats_for(game)["alice"])

    first = seat.step()

    assert first is not None and first.label == "pass_priority", first
    assert seat.step() is None, "the same refused pass is not posted a second time"
    assert seam.attempts == ["actions/pass-priority"], seam.attempts


def test_play_out_checks_the_stop_flag_before_every_seat(client, game):
    """`scripts/eval_gauntlet.py` runs this loop in a thread and joins it
    after the watcher's edge fires, so how promptly the loop notices
    `should_stop` is how long that join has to wait. Checked once per round,
    a two-seat round could not return until every seat had taken another turn
    of up to the seam's own 30s http timeout - which is how a `--serial` run
    came to abandon a live driver and start the next game alongside it.
    """
    flag = {"stop": False}

    class ArmOnAct:
        def __init__(self, inner):
            self.inner = inner

        def game_state(self, game_id, uid):
            return self.inner.game_state(game_id, uid)

        def legal_actions(self, game_id, player, uid, *, candidates=False):
            return self.inner.legal_actions(game_id, player, uid, candidates=candidates)

        @property
        def board_seq(self):
            return self.inner.board_seq

        def act(self, game_id, path, body, uid):
            flag["stop"] = True
            return self.inner.act(game_id, path, body, uid)

    seam = ArmOnAct(api(client))
    seats = [
        baseline_seat.BaselineSeat(seam, game, player, uid)
        for uid, player in store.seat_uids(game).items()
    ]

    outcome = baseline_seat.play_out(seats, max_steps=50, should_stop=lambda: flag["stop"])

    assert outcome["ended_by"] == "stopped", outcome
    assert outcome["steps"] == 1, outcome


def test_play_out_stops_when_the_watcher_says_so(client, game):
    """The gauntlet drives one baseline against an agent it does not control,
    and the watcher (`scripts/watch_game.py`) is what decides that game is
    finished with - a report filed, a stall, a timeout. `should_stop` is how
    that decision reaches the seat loop, rather than the loop having a second
    opinion about when a game is over."""
    seam = api(client)
    seats = [
        baseline_seat.BaselineSeat(seam, game, player, uid)
        for uid, player in store.seat_uids(game).items()
    ]
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    outcome = baseline_seat.play_out(seats, max_steps=500, should_stop=stop)

    assert outcome["ended_by"] == "stopped", outcome
    assert outcome["over"] is False
    assert state(client, game)["winner"] is None


def test_a_long_quiet_opponent_does_not_trip_the_stall_guard(client, game, monkeypatch):
    """`stall_rounds` is the knob that makes the guard usable in a mixed
    game: an agent seat thinking for a minute is not a stalled game, and a
    guard tuned for two baselines would concede out from under it."""
    monkeypatch.setattr(baseline_seat, "choose_move", lambda *a, **k: None)
    seam = api(client)
    seats = [
        baseline_seat.BaselineSeat(seam, game, player, uid)
        for uid, player in store.seat_uids(game).items()
    ]

    outcome = baseline_seat.play_out(seats, max_steps=20, stall_rounds=1000)

    assert outcome["ended_by"] == "bound", outcome
    assert state(client, game)["winner"] is None


# -- targets it cannot name --------------------------------------------------


def test_a_cast_whose_targets_cannot_be_filled_is_skipped_not_attempted(client, game):
    """CR 601.2c: a spell needs its targets announced, and `legal_actions`
    lists the cast whether or not the board offers any (CR 118.6 - legality
    is not payability, and it is not targetability either).

    Galvanic Discharge chooses target creature or planeswalker, and an empty
    board offers neither. Attempting it anyway is a refusal, and a run that
    does it every priority is exactly what a live gauntlet produced: 547
    identical "too few targets" rows in one game, because the opponent's own
    move changed the board each round and cleared the memory of the last
    refusal. The seat has to decline in-process instead.
    """
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Galvanic Discharge", "alice", "hand")
    seam = api(client)
    uid = seats_for(game)["alice"]
    seat = baseline_seat.BaselineSeat(seam, game, "alice", uid)

    move = seat.step()

    assert move is not None
    assert move.label == "pass_priority", move
    assert seat.refusals == 0, "nothing was attempted, so nothing was refused"


def test_a_cast_that_must_name_a_creature_reads_the_id_out_of_the_candidate_line(client, game):
    """The other shape a candidate arrives in. The enumeration carries a
    candidate as a *string* rather than an object - measured, see
    `affordances.candidate_line` - so a card candidate reaches this seat as
    `"<uuid> <name> (<zone>, <controller>)"` and `baseline_seat.candidate_of`
    is what turns it back into the id the announcement takes. Galvanic
    Discharge chooses target creature, so there is no player face to fall
    back on: naming the right object is the whole test, and it is the
    opponent's creature rather than this seat's own.
    """
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Galvanic Discharge", "alice", "hand")
    mine = battlefield_creature(client, game, "Grizzly Bears", "alice")
    theirs = battlefield_creature(client, game, "Grizzly Bears", "bob")
    seam = CountingSeam(api(client))
    seat = baseline_seat.BaselineSeat(seam, game, "alice", seats_for(game)["alice"])

    move = seat.step()

    assert move.label == "cast_spell", move
    assert move.body["targets"] == [theirs], (move, {"mine": mine, "theirs": theirs})
    assert seam.calls == ["state", "legal_actions", "act:actions/cast"], seam.calls
    board = state(client, game)
    assert [card["name"] for card in board["stack"]] == ["Galvanic Discharge"]
    # The stack view renders a card target by name (the two Grizzly Bears are
    # indistinguishable there), so the id claim above is the precise one - the
    # engine accepted that id at CR 601.2c, which is the part that matters.


def test_a_cast_whose_targets_can_be_filled_names_the_opponent(client, game):
    """The other half: an "any target" spell always has a legal target - the
    opponent's face - and the greedy policy takes it."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    seam = api(client)
    uid = seats_for(game)["alice"]
    seat = baseline_seat.BaselineSeat(seam, game, "alice", uid)

    move = seat.step()

    assert move.label == "cast_spell", move
    assert move.body["targets"] == ["bob"], move
    assert seat.refusals == 0
    board = state(client, game)
    assert [card["name"] for card in board["stack"]] == ["Lightning Bolt"]
    assert board["stack"][0]["targets"] == ["bob"]


# -- what it costs the server ------------------------------------------------


class RecordingHttp:
    """The `httpx`-shaped client `HttpSeatApiClient` talks through, with every
    request it makes written down.

    One level below `CountingSeam`: that counts what the *policy* asked the
    seam for, this counts what actually went over the wire, which is where a
    round trip is a round trip. Every call still reaches the real app.
    """

    def __init__(self, inner: TestClient) -> None:
        self.inner = inner
        self.requests: list[tuple[str, str, dict]] = []

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, dict(kwargs.get("params") or {})))
        return self.inner.get(url, **kwargs)

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, dict(kwargs.get("params") or {})))
        return self.inner.post(url, **kwargs)


class CountingSeam:
    """A `SeatApiClient` that records what was asked of it.

    A legitimate test double here (this is the api repo, not the engine - see
    the workbench's own rule about where "no mocks" binds): the point is to
    count calls, and every call still goes through to the real app underneath.
    """

    def __init__(self, inner: HttpSeatApiClient) -> None:
        self.inner = inner
        self.calls: list[str] = []

    def game_state(self, game_id, uid):
        self.calls.append("state")
        return self.inner.game_state(game_id, uid)

    def legal_actions(self, game_id, player, uid, *, candidates=False):
        self.calls.append("legal_actions")
        return self.inner.legal_actions(game_id, player, uid, candidates=candidates)

    @property
    def board_seq(self):
        return self.inner.board_seq

    def affordances(self, game_id, card_id, player, uid):
        # Not on `SeatApiClient` any more (see `api_client.py`): a seat reads
        # its candidates off the enumeration. Kept here, and kept counting,
        # so the test that pins "no second round trip per cast" would still
        # SEE the call if the policy ever reached for one again - a fake that
        # dropped the method would fail with an AttributeError somewhere else
        # instead of with the round-trip count that is the actual claim.
        self.calls.append("affordances")
        response = self.inner.http.get(
            f"/api/games/{game_id}/affordances/{card_id}",
            params={"player": player, "uid": uid},
        )
        return dict(response.json())

    def act(self, game_id, path, body, uid):
        self.calls.append(f"act:{path}")
        return self.inner.act(game_id, path, body, uid)


def test_a_cast_names_its_targets_out_of_the_enumeration_it_already_read(client, game):
    """No second round trip per cast. `GET .../legal-actions?candidates=true`
    carries each targeting action's legal candidates (`app.
    _attach_target_candidates`), which is the whole reason that parameter
    exists - a seat chooses AND issues its next action out of one answer. A
    baseline that went back for `GET .../affordances/{card_id}` paid an extra
    round trip, and so an extra inference cycle's worth of latency in a
    gauntlet, for a list it was already holding."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    wire = RecordingHttp(client)
    seam = CountingSeam(HttpSeatApiClient(http=wire))
    seat = baseline_seat.BaselineSeat(seam, game, "alice", seats_for(game)["alice"])

    move = seat.step()

    assert move.label == "cast_spell", move
    assert move.body["targets"] == ["bob"], move
    assert seam.calls == ["state", "legal_actions", "act:actions/cast"], seam.calls
    assert not [call for call in seam.calls if call.startswith("affordances")], seam.calls
    # And the same claim on the wire, which is where a round trip costs
    # something: three requests for a targeted cast, and not one of them the
    # per-card affordances probe this used to need.
    assert [url for _, url, _ in wire.requests if "/affordances/" in url] == [], wire.requests
    assert len(wire.requests) == 3, wire.requests
    (enumeration,) = [request for request in wire.requests if request[1].endswith("/legal-actions")]
    assert enumeration[2].get("candidates") == "true", enumeration


def test_it_asks_for_nothing_it_cannot_act_on(client, game):
    """While the opponent takes their whole turn, a seat that fetched its own
    legal actions every round would pay for an enumeration over its whole hand
    to be told, again, that it is not its move. A ten-game gauntlet is one
    python process serving every one of those reads."""
    seam = CountingSeam(api(client))
    bob = baseline_seat.BaselineSeat(seam, game, "bob", seats_for(game)["bob"])

    assert bob.step() is None
    assert seam.calls == ["state"], seam.calls


def test_it_reads_the_board_once_per_move(client, game):
    """`play_out` already has this seat's state in hand; reading it again
    inside `step` doubled every round's cost for nothing."""
    add_card(client, game, "Forest", "alice", "hand")
    seam = CountingSeam(api(client))
    alice = baseline_seat.BaselineSeat(seam, game, "alice", seats_for(game)["alice"])
    board = seam.game_state(game, alice.uid)
    seam.calls.clear()

    move = alice.step(state=board)

    assert move.label == "play_land"
    assert seam.calls == ["legal_actions", "act:actions/play-land"], seam.calls


def test_has_turn_is_the_one_rule_for_whether_it_is_our_move(client, game):
    board, _ = look(client, game, "alice")
    assert baseline_seat.has_turn(board, "alice") is True
    assert baseline_seat.has_turn(board, "bob") is False


# -- a transport failure is not a refusal ------------------------------------


class FlakyHttp:
    """The `httpx`-shaped client `HttpSeatApiClient` talks through, with a
    chosen number of POSTs to one path answered by a synthetic HTTP failure
    instead of by the app.

    A legitimate test double at exactly one seam - the socket - and nothing
    below it is faked: the board, the enumeration and every call this does
    not intercept are the real app's. Faking the *outcome* instead (a seam
    that answers `{"allowed": False}`) is what could not have caught this
    defect, because the whole question is what `api_client.act` does with a
    status code.
    """

    def __init__(
        self, inner: TestClient, *, fail_on: str, status: int, times: int | None = 1
    ) -> None:
        self.inner = inner
        self.fail_on = fail_on
        self.status = status
        self.times = times
        self.posts: list[str] = []
        self.failed = 0

    class _Response:
        """Just the three members `api_client.act` reads off a response."""

        def __init__(self, status_code: int, body: dict[str, Any]) -> None:
            self.status_code = status_code
            self._body = body
            self.text = json.dumps(body)

        def json(self) -> dict[str, Any]:
            return self._body

    def get(self, url, **kwargs):
        return self.inner.get(url, **kwargs)

    def post(self, url, **kwargs):
        self.posts.append(url)
        if self.fail_on in url and (self.times is None or self.failed < self.times):
            self.failed += 1
            return self._Response(
                self.status,
                {"detail": f"synthetic HTTP {self.status} from FlakyHttp"},
            )
        return self.inner.post(url, **kwargs)


def test_a_transient_5xx_leaves_the_seat_playing_rather_than_silenced(client, game):
    """One 503 on one `pass-priority` used to silence this seat for the rest
    of the game.

    `api_client.act` turned every status >= 400 into `{"allowed": False}`,
    so the anti-stall memory fingerprinted a pseudo-refusal the engine never
    made - and `pass_priority` has no enumerated row to step past, so `_pick`
    answered `None` for every later round against a board that could not move
    while this seat was the one to act.

    A 5xx is not a refusal: nobody knows whether the server acted. So it is
    remembered as nothing, and the next round posts the same move again.
    """
    wire = FlakyHttp(client, fail_on="actions/pass-priority", status=503)
    seat = baseline_seat.BaselineSeat(
        HttpSeatApiClient(http=wire), game, "alice", seats_for(game)["alice"]
    )

    first = seat.step()

    assert first is not None and first.label == "pass_priority", first
    assert wire.failed == 1, wire.posts
    assert seat.refusals == 0, "the engine refused nothing - the transport failed"
    assert seat.advanced is False, "and nothing was learned, so the round was idle"

    second = seat.step()

    assert second is not None and second.label == "pass_priority", second
    assert seat.advanced is True, "the retry went through"


def test_one_transient_5xx_never_fabricates_a_concession(client, game):
    """The cost of the defect, end to end: the ledger recorded a baseline
    concession with no cause, which reads as a harness loss for the baseline
    and a win for whatever it was being compared against. The default
    parallel gauntlet mode is precisely the condition that produces the blip
    - N games driven through one GIL-bound `serve` process - so the arm every
    number is measured against was one 503 away from an invented loss.
    """
    wire = FlakyHttp(client, fail_on="actions/pass-priority", status=503)
    seam = HttpSeatApiClient(http=wire)
    seats = [
        baseline_seat.BaselineSeat(seam, game, player, uid)
        for uid, player in store.seat_uids(game).items()
    ]

    outcome = baseline_seat.play_out(seats, max_steps=40)

    assert outcome["ended_by"] == "play", outcome
    assert [url for url in wire.posts if "concede" in url] == [], wire.posts
    # Ended by the rules, not by the guard: two libraryless baselines pass
    # until one of them draws from an empty library (CR 104.3c). The seat
    # the 503 hit is still the one that won it.
    assert outcome["winner"] == "alice", outcome


def test_a_genuine_refusal_is_still_remembered_and_still_stops_the_retry(client, game):
    """The other half, and the reason the 5xx fix is a gate rather than a
    deletion: a 4xx *is* the api's own answer that this action did not happen
    and will not, so the anti-stall memory must still fingerprint it. That
    memory was itself the fix for an unbounded loop (post, refused,
    re-derive, post the identical body again), and a seat that retried every
    refusal would spin exactly as it used to.
    """
    wire = FlakyHttp(client, fail_on="actions/pass-priority", status=400, times=None)
    seat = baseline_seat.BaselineSeat(
        HttpSeatApiClient(http=wire), game, "alice", seats_for(game)["alice"]
    )

    first = seat.step()

    assert first is not None and first.label == "pass_priority", first
    assert seat.refusals == 1, "a 4xx is the api's own refusal, and it was counted"
    assert seat.advanced is True, "the first refusal taught this seat something"
    assert seat.step() is None, "and the identical body is not posted a second time"
    assert [url for url in wire.posts if "pass-priority" in url] != [], wire.posts
    assert len([url for url in wire.posts if "pass-priority" in url]) == 1, wire.posts


def test_a_server_that_keeps_failing_ends_the_game_unfinished_not_conceded(client, game):
    """The bound on the fix above. A 5xx is retried rather than remembered,
    so a server that never recovers would spin this loop to `max_steps` -
    and the stall guard that catches that concedes, which is the fabricated
    loss all over again, just later.

    So the guard reads why the table went quiet. Quiet because the policy ran
    out of moves is a concession it should be seen to take; quiet because the
    server was unreachable is an unfinished game, scored for nobody, with the
    reason named.
    """
    wire = FlakyHttp(client, fail_on="actions/pass-priority", status=503, times=None)
    seam = HttpSeatApiClient(http=wire)
    seats = [
        baseline_seat.BaselineSeat(seam, game, player, uid)
        for uid, player in store.seat_uids(game).items()
    ]

    outcome = baseline_seat.play_out(seats, max_steps=60)

    assert outcome["ended_by"] == "transport", outcome
    assert outcome["over"] is False, outcome
    assert [url for url in wire.posts if "concede" in url] == [], wire.posts
    assert state(client, game)["winner"] is None, "an unreachable server is nobody's loss"
    assert sum(seat.unknown_outcomes for seat in seats) > 0, "and the count says it happened"


# -- naming the board it decided against -------------------------------------


class BodyRecordingHttp:
    """The `httpx`-shaped client, with every POST body written down. Every
    call still reaches the real app; nothing here answers for it."""

    def __init__(self, inner: TestClient) -> None:
        self.inner = inner
        self.bodies: list[tuple[str, dict[str, Any]]] = []

    def get(self, url, **kwargs):
        return self.inner.get(url, **kwargs)

    def post(self, url, **kwargs):
        self.bodies.append((url, dict(kwargs.get("json") or {})))
        return self.inner.post(url, **kwargs)


def test_every_move_names_the_ledger_position_it_was_chosen_at(client, game):
    """`records.require_current_board` can only measure a seat that names a
    board, and the baseline named none - so the control arm's refusals were
    the one set the scorecard could not split (MEMORY.md 2026-09-12, one
    level up).

    The number comes back on the enumeration itself
    (`schemas.LegalActionsResponse.last_seq`), so naming a board costs this
    seat no extra round trip - see the round-trip test above, which still
    counts three requests for a targeted cast.
    """
    add_card(client, game, "Forest", "alice", "hand")
    wire = BodyRecordingHttp(client)
    seam = HttpSeatApiClient(http=wire)
    seat = baseline_seat.BaselineSeat(seam, game, "alice", seats_for(game)["alice"])

    move = seat.step()

    assert move is not None and move.label == "play_land", move
    assert seat.advanced is True, "and naming the board did not get it refused"
    (url, body), *rest = wire.bodies
    assert rest == [], wire.bodies
    assert "play-land" in url, url
    assert body["expected_seq"] == seam.board_seq, body
    assert isinstance(body["expected_seq"], int) and body["expected_seq"] > 0, body
    # Kept out of `Move.body` itself: `answer_fingerprint` is the body, and a
    # seq that changed every round would make every re-post look like a new
    # answer - which is the anti-stall memory switched off.
    assert "expected_seq" not in move.body, move.body


class RacingSeam:
    """A `SeatApiClient` that moves the ledger once, between the seat's read
    and its post.

    A legitimate double at the seam (this is the api repo), and the race it
    creates is real: the interleaving call goes through the app and writes
    its own `records` row, which is exactly what a second seat acting on the
    same game does.
    """

    def __init__(self, inner: HttpSeatApiClient, client: TestClient) -> None:
        self.inner = inner
        self.client = client
        self.raced = False

    def game_state(self, game_id, uid):
        return self.inner.game_state(game_id, uid)

    def legal_actions(self, game_id, player, uid, *, candidates=False):
        return self.inner.legal_actions(game_id, player, uid, candidates=candidates)

    @property
    def board_seq(self):
        return self.inner.board_seq

    def act(self, game_id, path, body, uid):
        if not self.raced:
            self.raced = True
            add_card(self.client, game_id, "Mountain", "bob", "hand")
        return self.inner.act(game_id, path, body, uid)


def test_a_lost_race_is_neither_a_refusal_nor_the_end_of_this_seat(client, game):
    """Naming a board is what makes a race visible, and a race is not a
    ruling: `records.require_current_board` refuses before the engine is
    asked anything, so there is nothing about the answer to remember. A seat
    that fingerprinted it would have retired a move the engine never judged
    - which is how the 5xx defect worked - and a scorecard that counted it as
    a refusal would report the harness's own interleaving as the policy
    playing badly.
    """
    add_card(client, game, "Forest", "alice", "hand")
    seam = RacingSeam(api(client), client)
    seat = baseline_seat.BaselineSeat(seam, game, "alice", seats_for(game)["alice"])

    first = seat.step()

    assert first is not None and first.label == "play_land", first
    assert seat.races == 1, "the board moved under it"
    assert seat.refusals == 0, "and the engine was never asked"

    second = seat.step()

    assert second is not None and second.label == "play_land", second
    assert seat.advanced is True, "the same move, against the board as it now is"
