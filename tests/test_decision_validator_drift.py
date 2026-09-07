"""Drift guard: `decisions.validate_answer` against the engine's own
`Game.ask` (`magic_cards_model.models.game`).

`validate_answer` exists so a malformed or illegal `decide` answer is refused
synchronously, on the answering seat's own request, before it is ever posted
to `DecisionMailbox` (see that function's docstring for the corruption its
absence used to cause). It does this by re-running, in this API's process,
the exact same per-kind checks `Game.ask` runs on the other side of the
mailbox - which means the two are two independent copies of one set of rules,
kept in sync only by whoever remembers to edit both when either changes.
Nothing enforced that until now.

This file is the enforcement: for every `DecisionKind` this API claims to be
able to answer (`decisions.ANSWERABLE_KINDS`), it builds one real `Decision`
(the engine's own dataclass, referencing real card ids off a real board built
through the API - no hand-written fixtures, no mocks) and throws a handful of
candidate answers - a legal one, and several illegal or malformed ones - at
both `validate_answer` and `Game.ask` (scripted with `ScriptedAnswer`, the
engine's own no-mock staging primitive, so no live seat has to be driven
through a real decision prompt just to exercise the validator). If a future
edit changes one branch's logic, wording, or CR citation without echoing it
into the other, some candidate below starts disagreeing on accept/reject or
on the refusal text, and this test fails naming exactly which kind and which
value drifted.

`Game.ask` never mutates the board on its own - only the *caller* of `ask`
(`ask_cleanup_discard`, `may_pay_life`, cascade's own `game.ask`, etc.) acts on
the returned value - so scripting an answer and calling `game.ask` directly,
outside any real spell resolution, is safe to do repeatedly against the same
board without corrupting it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from conftest import add_card
from fastapi.testclient import TestClient
from magic_cards_model.machines.decisions import (
    CardSplit,
    Decision,
    DecisionKind,
    DeclaredPair,
    ScriptedAnswer,
    ask_attackers,
    ask_blockers,
    ask_mulligan,
    ask_trigger_order,
)
from magic_cards_model.models import Game
from magic_cards_model.models.card_state import TargetRef

from magic_cards_model_api import decisions
from magic_cards_model_api.decisions import ANSWERABLE_KINDS
from magic_cards_model_api.store import store


@pytest.fixture()
def board(client: TestClient, game: str) -> dict[str, Any]:
    """A real board with enough real cards to build every `Decision` below:
    three library cards (scry/surveil/choose_cards candidates), a battlefield
    creature and a player (choose_targets candidates), and two real
    simultaneously triggered abilities (the `order_triggers` batch).

    The triggers are made the way the engine makes them, not written down:
    two Delver of Secrets on the battlefield and one real untap-to-upkeep
    advance, which fires both "at the beginning of your upkeep" abilities at
    once - CR 603.3b's own premise, "multiple abilities have triggered since
    the last time a player received priority". Their real `AbilityOnStack`
    ids and source names are what the `ORDER_TRIGGERS` case below orders.
    """
    library_ids = [
        UUID(add_card(client, game, "Mountain", "alice", "library")["added"]["card_id"])
        for _ in range(3)
    ]
    bears_id = UUID(
        add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    )
    add_card(client, game, "Delver of Secrets", "alice", "battlefield")
    add_card(client, game, "Delver of Secrets", "alice", "battlefield")
    alice_uid = next(u for u, name in store.seat_uids(game).items() if name == "alice")
    client.put(f"/api/games/{game}/step", json={"step": "untap", "player": "alice"})
    advanced = client.post(
        f"/api/games/{game}/advance", params={"uid": alice_uid}, json={"player": "alice"}
    )
    body = advanced.json()
    if body.get("paused"):
        # CR 603.3b: nothing scripts an answer for these two simultaneous
        # triggers, so the advance that puts them on the stack pauses on
        # `order_triggers` instead of completing - resume it (any
        # permutation is legal; the offered order is as good as any) so the
        # triggers are actually on the stack for this fixture to read.
        pending = body["pending_decision"]
        assert pending["kind"] == "order_triggers", pending
        answered = client.post(
            f"/api/games/{game}/decide",
            params={"uid": alice_uid},
            json={"player": "alice", "order": pending["cards"]},
        )
        assert answered.status_code == 200 and answered.json()["accepted"] is True, answered.json()
    triggers = tuple((ability.id, ability.source_name) for ability in _game(game).stack)
    assert len(triggers) == 2, f"expected two simultaneous upkeep triggers, got {triggers}"
    # The wording the engine really used, read off the `Decision` that advance
    # made it build, never written down here. `ask_trigger_order` rewrites the
    # prompt it is handed - it appends each source's printed name, numbered
    # against `cards`, because `Decision.cards` for this kind holds
    # `AbilityOnStack` ids and an ability on the stack is an object that is not
    # a card (CR 113.1c, CR 603.3), so nothing downstream could otherwise name
    # them. A refusal quotes the prompt, so the `ORDER_TRIGGERS` case below has
    # to carry that rewritten wording or the two sides refuse in different
    # words over a difference that is not drift.
    asked = [
        decision
        for decision in _game(game).decisions.asked
        if decision.kind is DecisionKind.ORDER_TRIGGERS
    ]
    assert asked, "the advance should have put CR 603.3b's ordering to alice"
    return {
        "library_ids": library_ids,
        "bears_id": bears_id,
        "triggers": triggers,
        "trigger_prompt": asked[-1].prompt,
    }


def _game(game_id: str) -> Game:
    found = store.get(game_id)
    assert found is not None
    return found


def _engine_ask(engine_game: Game, decision: Decision) -> object:
    """The engine's own validating entry point for `decision`'s kind.

    `Game.ask` for every kind but one. `MULLIGAN` never goes through it: CR
    103.5's declaration is asked by `machines/decisions.py`'s `ask_mulligan`,
    which validates the answer itself (and a `MULLIGAN` handed to `Game.ask`
    would fall past its whole kind dispatch into the trailing `CardSplit`
    branch, refusing a perfectly good `True`). So the parity this file
    enforces is against `ask_mulligan` for that kind - the function that
    really is the other side of the mailbox for it.
    """
    if decision.kind is DecisionKind.MULLIGAN:
        return ask_mulligan(engine_game.decisions, decision.player, prompt=decision.prompt)
    if decision.kind is DecisionKind.ORDER_TRIGGERS:
        # CR 603.3b's ordering is the second kind that never reaches
        # `Game.ask`: `ask_trigger_order` puts it and validates the answer
        # itself, so that is the other side of the mailbox for this kind. It
        # is called with the same `(id, source_name)` pairs the board fixture
        # read straight off the real triggers now on the stack, and with its
        # own default prompt - the one the real advance already used, which
        # the fixture read back off the `Decision` that advance built and put
        # on the case below, so both sides refuse in identical words. Passing
        # `decision.prompt` here instead would re-run `ask_trigger_order`'s
        # own prompt rewrite over an already-rewritten prompt and name every
        # source twice.
        abilities = tuple((ability.id, ability.source_name) for ability in engine_game.stack)
        return ask_trigger_order(engine_game.decisions, decision.player, abilities)
    if decision.kind is DecisionKind.DECLARE_ATTACKERS:
        # CR 508.1's declaration: the third kind whose engine-side authority
        # is not `Game.ask` but its own asker, `ask_attackers` (by way of
        # `_ask_declaration`) - see that function's own docstring for why it
        # deliberately does not consult a live provider the way `Game.ask`
        # does, which is irrelevant to this parity check either way, since
        # both sides here are scripted rather than live.
        return ask_attackers(engine_game.decisions, decision.player, prompt=decision.prompt)
    if decision.kind is DecisionKind.DECLARE_BLOCKERS:
        # CR 509.1's declaration - same shape as `DECLARE_ATTACKERS` above,
        # via the shared `ask_blockers`. `attackers` carries the attacker ids
        # offered as context (`decision.cards`, echoed by `PendingDecision.
        # from_engine`) - the same ids `validate_answer`'s own tests build
        # `decision.cards` from below.
        return ask_blockers(
            engine_game.decisions,
            decision.player,
            prompt=decision.prompt,
            attackers=decision.cards,
        )
    return engine_game.ask(decision)


def _check_parity(engine_game: Game, decision: Decision, value: object) -> None:
    """Feed `value` to both validators; they must agree on accept/reject, and
    on the exact refusal text when both reject."""
    engine_game.decisions.add(
        ScriptedAnswer(kind=decision.kind, value=value, player=decision.player)
    )
    engine_message: str | None = None
    try:
        _engine_ask(engine_game, decision)
        engine_accepted = True
    except ValueError as exc:
        engine_accepted = False
        engine_message = str(exc)

    api_message: str | None = None
    try:
        decisions.validate_answer(decision, value)
        api_accepted = True
    except ValueError as exc:
        api_accepted = False
        api_message = str(exc)

    assert engine_accepted == api_accepted, (
        f"{decision.kind.value} diverged on {value!r}: engine accepted={engine_accepted} "
        f"({engine_message!r}), api accepted={api_accepted} ({api_message!r})"
    )
    if not engine_accepted:
        assert engine_message == api_message, (
            f"{decision.kind.value} refusal text diverged for {value!r}: "
            f"engine said {engine_message!r}, api said {api_message!r}"
        )


def _cases(board: dict[str, Any]) -> dict[DecisionKind, tuple[Decision, list[object]]]:
    """One real `Decision` and a handful of candidate answers per kind - a
    legal one first, then illegal/malformed ones chosen to hit each branch
    `Game.ask`/`validate_answer` actually checks."""
    library_ids = board["library_ids"]
    bears_id = board["bears_id"]
    stray_id = uuid4()

    bool_kinds = (
        DecisionKind.MAY,
        DecisionKind.PAY_LIFE,
        DecisionKind.PAY_ENERGY,
        DecisionKind.PAY_MANA,
    )
    cases: dict[DecisionKind, tuple[Decision, list[object]]] = {}
    for kind in bool_kinds:
        legal = Decision(
            kind=kind, player="alice", prompt=f"{kind.value} test", affirmative_is_legal=True
        )
        cases[kind] = (legal, [True, False, "yes", 1])

    cases[DecisionKind.PAY_ENERGY_AMOUNT] = (
        Decision(
            kind=DecisionKind.PAY_ENERGY_AMOUNT,
            player="alice",
            prompt="pay energy amount",
            amount=5,
        ),
        [3, 0, 5, -1, 6, "3", True],
    )
    cases[DecisionKind.PAY_LIFE_AMOUNT] = (
        Decision(
            kind=DecisionKind.PAY_LIFE_AMOUNT, player="alice", prompt="pay life amount", amount=10
        ),
        [4, 0, 10, -1, 11, "4", True],
    )

    cases[DecisionKind.SCRY] = (
        Decision(
            kind=DecisionKind.SCRY,
            player="alice",
            prompt="scry 3",
            amount=3,
            cards=tuple(library_ids),
        ),
        [
            CardSplit(moved=(library_ids[0],), top_order=(library_ids[1], library_ids[2])),
            CardSplit(),
            CardSplit(moved=(stray_id,)),
            "not a cardsplit",
        ],
    )
    cases[DecisionKind.SURVEIL] = (
        Decision(
            kind=DecisionKind.SURVEIL,
            player="alice",
            prompt="surveil 3",
            amount=3,
            cards=tuple(library_ids),
        ),
        [
            CardSplit(moved=(library_ids[1],), top_order=(library_ids[0], library_ids[2])),
            CardSplit(moved=(stray_id,)),
            42,
        ],
    )

    cases[DecisionKind.CHOOSE_CARDS] = (
        Decision(
            kind=DecisionKind.CHOOSE_CARDS,
            player="alice",
            prompt="choose 2 cards",
            amount=2,
            cards=tuple(library_ids),
            exact=True,
        ),
        [
            (library_ids[0], library_ids[1]),
            (library_ids[0],),
            (library_ids[0], stray_id),
            ["not", "a", "tuple"],
        ],
    )

    cases[DecisionKind.CHOOSE_TARGETS] = (
        Decision(
            kind=DecisionKind.CHOOSE_TARGETS,
            player="alice",
            prompt="choose 1 target",
            targets=(TargetRef.to_card(bears_id), TargetRef.to_player("bob")),
            target_maximum=1,
            target_minimum=1,
        ),
        [
            (TargetRef.to_card(bears_id),),
            (),
            (TargetRef.to_card(bears_id), TargetRef.to_player("bob")),
            (TargetRef.to_card(stray_id),),
            "not a sequence",
        ],
    )

    cases[DecisionKind.CHOOSE_A_QUALITY] = (
        Decision(
            kind=DecisionKind.CHOOSE_A_QUALITY,
            player="alice",
            prompt="choose a color",
            options=("W", "U", "B", "R", "G"),
        ),
        ["R", "X", "", 5],
    )
    cases[DecisionKind.CHOOSE_A_MODE] = (
        Decision(
            kind=DecisionKind.CHOOSE_A_MODE,
            player="alice",
            prompt="choose a mode",
            options=("Mode One", "Mode Two"),
        ),
        ["Mode One", "Mode Three", "", None],
    )

    # CR 103.6's opening-hand action: a yes-or-no with both branches always
    # legal (a card's own restriction stops the question being put at all,
    # rather than making one answer illegal), so unlike the `bool_kinds` above
    # there is no `affirmative_is_legal` to set and no illegal-option branch
    # to hit - only the answer's shape.
    cases[DecisionKind.OPENING_HAND_ACTION] = (
        Decision(
            kind=DecisionKind.OPENING_HAND_ACTION,
            player="alice",
            prompt="begin the game with Leyline of the Void on the battlefield",
            source="Leyline of the Void",
        ),
        [True, False, "yes", 1, None],
    )

    # CR 103.5's declaration: a yes-or-no with both branches always legal -
    # the engine simply does not ask a player who has hit the zero-hand
    # ceiling, rather than offering them an illegal "yes" - so, as with
    # `OPENING_HAND_ACTION`, only the answer's shape is checked. The engine
    # side of this parity is `ask_mulligan`, not `Game.ask`; see `_engine_ask`.
    cases[DecisionKind.MULLIGAN] = (
        Decision(kind=DecisionKind.MULLIGAN, player="alice", prompt="take a mulligan"),
        [True, False, "yes", 1, None],
    )

    # CR 603.3b: a permutation of exactly the abilities offered, bottom of the
    # stack first. The engine side is `ask_trigger_order`, not `Game.ask`; see
    # `_engine_ask`. A source *name* is deliberately not among the candidates:
    # `ask_trigger_order` accepts one (a scenario has to be able to state the
    # order before the abilities exist) and `validate_answer` cannot, because
    # a `Decision` carries the ability ids and nothing carries their names
    # across the mailbox. That one-way strictness is documented in
    # `validate_answer`'s `ORDER_TRIGGERS` branch, and it can only ever refuse
    # an answer the engine would have taken - never accept one it would not.
    first_id, second_id = (ability_id for ability_id, _ in board["triggers"])
    cases[DecisionKind.ORDER_TRIGGERS] = (
        Decision(
            kind=DecisionKind.ORDER_TRIGGERS,
            player="alice",
            prompt=board["trigger_prompt"],
            cards=(first_id, second_id),
        ),
        [
            (second_id, first_id),
            (first_id, second_id),
            (first_id,),
            (first_id, first_id),
            (first_id, second_id, stray_id),
            "not a sequence",
            (5,),
        ],
    )

    cases[DecisionKind.PUT_ON_TOP_OR_BOTTOM] = (
        Decision(kind=DecisionKind.PUT_ON_TOP_OR_BOTTOM, player="alice", prompt="top or bottom"),
        [True, False, "top", None],
    )

    # CR 508.1's declaration. The engine side is `ask_attackers` (by way of
    # `_ask_declaration`), not `Game.ask` - see `_engine_ask`. Only shape is
    # checked on either side (`validate_answer`'s own docstring): full attack
    # legality is `Game.declare_attackers`'s job, downstream of answering
    # this decision at all, so no stray/legality candidate is exercised here
    # the way `CHOOSE_TARGETS` above exercises one.
    cases[DecisionKind.DECLARE_ATTACKERS] = (
        Decision(kind=DecisionKind.DECLARE_ATTACKERS, player="alice", prompt="declare attackers"),
        [
            (DeclaredPair(creature=bears_id, defender_player="bob"),),
            (),
            "not a sequence",
            (DeclaredPair(creature=bears_id, defender_player="bob"), "not a pair"),
        ],
    )
    # CR 509.1's declaration - same shape as `DECLARE_ATTACKERS` above, via
    # the shared `_ask_declaration`. `cards` carries the attacker ids offered
    # as context, echoing what `ask_blockers`'s own `attackers=` argument is
    # built from in `_engine_ask`.
    cases[DecisionKind.DECLARE_BLOCKERS] = (
        Decision(
            kind=DecisionKind.DECLARE_BLOCKERS,
            player="alice",
            prompt="declare blockers",
            cards=(bears_id,),
        ),
        [
            (DeclaredPair(creature=library_ids[0], defender_card=bears_id),),
            (),
            "not a sequence",
            (DeclaredPair(creature=library_ids[0], defender_card=bears_id), "not a pair"),
        ],
    )

    return cases


def test_every_answerable_kind_has_a_drift_case(board: dict[str, Any]) -> None:
    """The case table above must cover every kind the API claims to answer -
    otherwise the parity sweep below would silently skip a kind that joined
    `ANSWERABLE_KINDS` after this file was written, exactly the shape of gap
    `test_decision_kinds.test_every_decision_kind_is_answerable` already
    guards on the other side of that set."""
    cases = _cases(board)
    assert set(cases) == set(ANSWERABLE_KINDS)


@pytest.mark.parametrize("kind", sorted(ANSWERABLE_KINDS, key=lambda k: k.value))
def test_validator_matches_engine_ask(
    client: TestClient, game: str, board: dict[str, Any], kind: DecisionKind
) -> None:
    engine_game = _game(game)
    decision, values = _cases(board)[kind]
    for value in values:
        _check_parity(engine_game, decision, value)


def test_pay_life_below_the_floor_is_illegal_on_both_sides(
    client: TestClient, game: str, board: dict[str, Any]
) -> None:
    """`affirmative_is_legal=False` (CR 119.4's life floor, or an
    energy/mana floor the same shape) removes `True` from both validators'
    legal answers - not just a bare bool-type check, the actual `legal_options`
    gate `Game.ask` applies and `validate_answer` re-derives from the same
    `Decision`."""
    engine_game = _game(game)
    decision = Decision(
        kind=DecisionKind.PAY_LIFE,
        player="alice",
        prompt="pay 2 life at 1 life",
        affirmative_is_legal=False,
    )
    for value in (True, False):
        _check_parity(engine_game, decision, value)


def test_choose_cards_any_number_ceiling_matches(
    client: TestClient, game: str, board: dict[str, Any]
) -> None:
    """`exact=False` (CR 107.1c's "any number of") turns `amount` into a
    ceiling rather than an exact count - a separate branch in both
    validators, exercised here rather than only the `exact=True` case above."""
    engine_game = _game(game)
    library_ids = board["library_ids"]
    decision = Decision(
        kind=DecisionKind.CHOOSE_CARDS,
        player="alice",
        prompt="put any number of cards from your hand",
        amount=2,
        cards=tuple(library_ids),
        exact=False,
    )
    for value in ((), (library_ids[0],), (library_ids[0], library_ids[1]), tuple(library_ids)):
        _check_parity(engine_game, decision, value)
