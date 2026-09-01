"""The pregame: CR 103.5's mulligan process and CR 103.6's opening-hand actions.

The engine used to run both inside one blocking `Match.run_mulligans` call
that put every question through `Game.ask`. Behind this API's decision mailbox
- a provider that blocks the calling thread until a human answers, holding the
game lock the whole time - that cost one answer timeout per question per seat:
a measured 1200 s for a normal two-deck game, during which no other request
could touch the game at all. `tests/test_ui_e2e.py` did not merely run slowly,
it hung forever.

The engine replaced that with two steps the game **rests in**:

- `Step.MULLIGAN` (CR 103.5, "a player who is dissatisfied with their initial
  hand may take a mulligan"). `Match.start_game()` returns a game already
  sitting here, with `turn_number` 0.
- `Step.PREGAME_ACTIONS` (CR 103.6, "some cards allow a player to take actions
  with them from their opening hand ... the starting player may take any such
  actions in any order").
- Then `Step.UNTAP` of CR 103.8's first turn ("the starting player takes their
  first turn").

Neither pregame step is in the engine's `TURN_ORDER` or `PHASE_OF`, so
`TurnMachine.phase` is `None` while the game rests in one and `Game.advance` /
`Game.advance_to` refuse to move a game whose pregame is unfinished.

**So this module no longer runs anything on a thread, and no request ever
blocks in the pregame.** Every function here is a thin, synchronous adapter
between one HTTP call and one engine method that is *told* an answer already
made: `Game.declare_mulligan`, `Game.take_opening_hand_action`,
`Game.finish_opening_hand_actions`. A game booted from decklists is simply
handed back resting in `Step.MULLIGAN`, and the seats drive it from there.

**The one question the engine still asks.** CR 103.5's bottoming choice - "puts
a number of those cards equal to the number of times that player has taken a
mulligan on the bottom of their library" - is a `CHOOSE_CARDS` question the
engine puts through `Game.ask` from inside `declare_mulligan`, at the moment a
seat that has already mulliganed keeps. Left alone it would reach this API's
mailbox and block the declaring seat's own request, holding the game lock,
which is the exact shape the engine change exists to remove.

So this module **pre-provides** it instead: `declare_mulligan` below scripts the
answer into `Game.decisions` *before* calling the engine, so `Game.ask` finds a
scripted answer and never consults the provider at all. Pre-providing rather
than driving it through the mailbox, because:

- the choice is knowable before the declaration is made (it is "which of the
  seven cards I am looking at do I give up"), so there is nothing to gain by
  asking for it later, and
- the round can be resolved by *another* seat's declaration - CR 103.5 has all
  players who kept bottom simultaneously - so the answer has to be in hand when
  this seat declares, not when the round happens to resolve.

A caller that does not name the cards gets the engine's own documented default
for an exact `CHOOSE_CARDS`, "the back of the offered list", which is the same
list `Player.keep_hand(None)` bottoms - computed here explicitly and recorded in
the audit log as `chosen: false`, so a transcript never implies a player picked
what the default picked for them.

Every CR number in this module was verified against the Comprehensive Rules
with `knowledge_search(type="rules")` in the session that wrote it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from magic_cards_model.machines.decisions import DecisionKind, ScriptedAnswer
from magic_cards_model.models import Game

from . import audit, engine_api


def in_pregame(game: Game) -> bool:
    """Whether this game is still before CR 103.8's first turn."""
    return game.in_pregame


def _hand_ids(game: Game, username: str) -> list[str]:
    return [str(state.id) for state in game.player(username).hand]


def pregame_view(game: Game) -> dict[str, Any] | None:
    """What the game is waiting for in the pregame, or `None` once it is over.

    Everything here is the engine's own answer, read fresh: who still owes a
    CR 103.5 declaration, how many cards keeping would cost each seat
    (CR 103.5's "a number of those cards equal to the number of times that
    player has taken a mulligan"), whose CR 103.6 turn it is, and what
    CR 103.6 actions each seat's own opening hand offers.

    `opening_hand_options` names cards in a hidden zone (CR 402.3: "a player
    can't look at the cards in another player's hand"), so `visibility.py`
    strips every seat's entry but the viewer's own.
    """
    if not game.in_pregame:
        return None
    assert game.turn is not None
    return {
        "step": game.turn.step.value,
        "awaiting_mulligan": list(game.awaiting_mulligan_declarations),
        "hand_kept": {player.username: player.hand_kept for player in game.players},
        "mulligans_taken": {player.username: player.mulligans_taken for player in game.players},
        "cards_to_bottom": {player.username: player.cards_to_bottom for player in game.players},
        "can_mulligan": {player.username: player.can_mulligan for player in game.players},
        "opening_hand_actor": game.opening_hand_actor,
        "opening_hand_options": {
            player.username: [
                {
                    "card_id": str(decision.card),
                    "source": decision.source,
                    "prompt": decision.prompt,
                }
                for decision in game.opening_hand_options(player.username)
            ]
            for player in game.players
        },
        "opening_hand_notices": [notice.text for notice in game.opening_hand_notices],
    }


def _bottom_answer(
    game: Game, username: str, bottom: list[UUID] | None
) -> tuple[tuple[UUID, ...], bool] | None:
    """The cards this seat puts on the bottom when it keeps, and whether it
    chose them (CR 103.5).

    `None` when nothing is owed - "a choice among zero cards is not a choice",
    and the engine does not put the question at all in that case.

    An unnamed `bottom` falls back to the back of the hand. That is not a rule
    invented here: it is what `Player.keep_hand(None)` bottoms and what the
    engine's `default_answer` returns for an exact `CHOOSE_CARDS` ("the back of
    the offered list", offered in hand order), so a caller that declines to
    choose gets exactly what it would have got by staying silent - only
    without a request parked for the full decision timeout to find that out.
    """
    player = game.player(username)
    needed = player.cards_to_bottom
    if needed <= 0:
        return None
    hand = list(player.hand)
    if bottom is None:
        return tuple(state.id for state in hand[len(hand) - needed :]), False
    if len(bottom) != needed:
        raise ValueError(
            f"{username} must put {needed} card(s) on the bottom of their library "
            f"when keeping this hand, not {len(bottom)} (CR 103.5)"
        )
    in_hand = {state.id for state in hand}
    stray = [str(card_id) for card_id in bottom if card_id not in in_hand]
    if stray:
        raise ValueError(f"{username} cannot bottom cards that are not in their hand: {stray}")
    if len(set(bottom)) != len(bottom):
        raise ValueError(f"{username} named the same card twice to bottom")
    return tuple(bottom), True


def declare_mulligan(
    game_id: str,
    game: Game,
    username: str,
    take: bool,
    bottom: list[UUID] | None = None,
    *,
    automatic: bool = False,
) -> dict[str, Any]:
    """One seat's CR 103.5 declaration - "first, the starting player declares
    whether they will take a mulligan; then each other player in turn order
    does the same".

    Blocks on nothing. When `take` is False and this seat owes a bottoming, the
    choice is scripted into `Game.decisions` first, so the `CHOOSE_CARDS`
    question the engine puts while the round resolves finds an answer already
    made instead of reaching the mailbox - see the module docstring.
    """
    chose_bottom: bool | None = None
    if not take:
        answer = _bottom_answer(game, username, bottom)
        if answer is not None:
            cards, chose_bottom = answer
            game.decisions.add(
                ScriptedAnswer(
                    kind=DecisionKind.CHOOSE_CARDS,
                    value=cards,
                    player=username,
                    uses=1,
                )
            )
    step = game.declare_mulligan(username, take)
    audit.record(
        game_id,
        "mulligan_declared",
        player=username,
        take=take,
        # `live` is the honest half: a seat really made this declaration
        # through its own request, unlike the engine default the old blocking
        # path recorded. `bottom_chosen` is None when nothing was owed.
        # `automatic` is `ai_pregame.py`'s backstop declaring for a bot seat
        # that did not declare for itself - a real declaration, but not the
        # seat's own choice, so the transcript must not claim it was.
        live=not automatic,
        automatic=automatic,
        bottom_chosen=chose_bottom,
        step=step.value,
    )
    return {"declared": take, "step": step.value, "pregame": pregame_view(game)}


def take_opening_hand_action(
    game_id: str, game: Game, username: str, card_id: UUID
) -> dict[str, Any]:
    """One CR 103.6 action with a card in this seat's own opening hand."""
    card = game.find_by_id(card_id)
    name = card.name if card is not None else str(card_id)
    game.take_opening_hand_action(username, card_id)
    assert game.turn is not None
    audit.record(game_id, "opening_hand_action", player=username, card=name, live=True)
    return {"taken": name, "step": game.turn.step.value, "pregame": pregame_view(game)}


def finish_opening_hand_actions(
    game_id: str, game: Game, username: str, *, automatic: bool = False
) -> dict[str, Any]:
    """This seat is done taking CR 103.6 actions; the next seat is up, and
    when the last one finishes CR 103.8's first turn begins.

    The settling of CR 500.3's priorityless steps happens *here*, not in the
    HTTP handler, because two callers reach this and only one of them is a
    request. `ai_pregame.settle` - the backstop that makes an AI seat's
    pregame moves when its agent does not - calls this directly, so a game
    whose LAST pregame move came from the backstop was left parked at
    CR 103.8's untap step with `priority_holder: None` and no move any client
    is allowed to make. That is a reported freeze, and it was the common case
    rather than the corner one: a seat whose agent is failing is exactly the
    seat the backstop moves for.
    """
    step = game.finish_opening_hand_actions(username)
    audit.record(
        game_id,
        "opening_hand_done",
        player=username,
        step=step.value,
        live=not automatic,
        automatic=automatic,
    )
    result: dict[str, Any] = {"step": step.value, "pregame": pregame_view(game)}
    crossed = engine_api.settle_priorityless_steps(game)
    if crossed and game.turn is not None:
        result["step"] = game.turn.step.value
        result["settled_through"] = crossed
    return result


def settle_pregame(game_id: str, game: Game) -> dict[str, Any]:
    """Walk the whole pregame through the engine's own decision defaults.

    The headless path - `Game.settle_pregame`, unchanged engine behaviour and
    unchanged speed: every declaration keeps the dealt hand and every CR 103.6
    action is declined, immediately and deterministically, because no seat is
    consulted. For a bot harness or a test that wants a game to *play*, not a
    game to start correctly.
    """
    step = game.settle_pregame()
    hands = ", ".join(f"{player.username}={len(player.hand)}" for player in game.players)
    mulligans = ", ".join(f"{player.username}={player.mulligans_taken}" for player in game.players)
    audit.record(
        game_id,
        "pregame_settled",
        hands=hands,
        mulligans=mulligans,
        # The opposite of `declare_mulligan`'s entry, and deliberately so: this
        # path asks nobody, so every declaration here is the engine's default
        # for an unscripted player, not a choice a seat made.
        live=False,
        step=step.value,
    )
    return {"step": step.value, "pregame": pregame_view(game)}


__all__ = [
    "declare_mulligan",
    "finish_opening_hand_actions",
    "in_pregame",
    "pregame_view",
    "settle_pregame",
    "take_opening_hand_action",
]
