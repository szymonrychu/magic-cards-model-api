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
    ScriptedAnswer,
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
    creature and a player (choose_targets candidates)."""
    library_ids = [
        UUID(add_card(client, game, "Mountain", "alice", "library")["added"]["card_id"])
        for _ in range(3)
    ]
    bears_id = UUID(
        add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    )
    return {"library_ids": library_ids, "bears_id": bears_id}


def _game(game_id: str) -> Game:
    found = store.get(game_id)
    assert found is not None
    return found


def _check_parity(engine_game: Game, decision: Decision, value: object) -> None:
    """Feed `value` to both validators; they must agree on accept/reject, and
    on the exact refusal text when both reject."""
    engine_game.decisions.add(
        ScriptedAnswer(kind=decision.kind, value=value, player=decision.player)
    )
    engine_message: str | None = None
    try:
        engine_game.ask(decision)
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

    cases[DecisionKind.PUT_ON_TOP_OR_BOTTOM] = (
        Decision(kind=DecisionKind.PUT_ON_TOP_OR_BOTTOM, player="alice", prompt="top or bottom"),
        [True, False, "top", None],
    )

    return cases


def test_every_answerable_kind_has_a_drift_case(board: dict[str, Any]) -> None:
    """The case table above must cover every kind the API claims to answer -
    otherwise the parity sweep below would silently skip a kind that joined
    `ANSWERABLE_KINDS` after this file was written, exactly the shape of gap
    `test_decision_kinds.test_every_decision_kind_is_answerable_or_served_elsewhere`
    already guards on the other side of that set."""
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
