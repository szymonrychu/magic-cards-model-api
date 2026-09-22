"""One player making another player's decisions (CR 723), as the api sees it.

The engine keeps the effect (`TurnMachine.player_control`, Emrakul, the
Promised End's cast trigger) and enforces it on priority: a controlled
player's own pass is refused with "X is making Y's decisions this turn (CR
723.5)" and the controller's pass is Y's pass. Everything else a seat does
arrives here named by a request field, so this module answers the rest of
CR 723.5 at the api's door:

- the controller acting with the controlled player's objects - "all choices
  and decisions the controlled player is allowed to make ... includes choices
  and decisions about what to play" - while CR 723.3 keeps every object with
  its normal controller, so the action is run as the controlled player's;
- the controlled player's own seat reaching for any of those decisions is
  refused with the engine's own words for it;
- CR 723.6 leaves conceding with the controlled player, so it is never
  gated here.
"""

from __future__ import annotations

from typing import Any

from magic_cards_model.models import CardState, Game, Zone

from .engine_api import SHARED_ZONES, answers_to_card_name

#: The engine gates these two itself (`PriorityMachine.require`, `Game.advance`):
#: the controller sends its own name, and a controlled player's own attempt is
#: refused with CR 723.5's text already.
ENGINE_GATED = frozenset({"pass_priority", "advance"})

#: CR 723.6: "A player may concede the game at any time, even if they are
#: controlled by another player."
NEVER_GATED = frozenset({"concede"})


def controllers(game: Game) -> dict[str, str]:
    """Controlled player -> the player making their decisions, for every player
    somebody else is deciding for right now (CR 723.1: for that whole turn)."""
    if game.turn is None:
        return {}
    return {
        controlled: controller
        for controlled, controller in game.turn.player_control.items()
        if controlled != controller
    }


def decider(game: Game, player: str) -> str:
    """Who makes `player`'s decisions right now - themselves unless controlled."""
    return controllers(game).get(player, player)


def deciding_for(game: Game, seat: str) -> list[str]:
    """Every other player `seat` is making decisions for right now."""
    return sorted(controlled for controlled, who in controllers(game).items() if who == seat)


def refusal(controller: str, player: str) -> str:
    """The engine's own CR 723.5 refusal (`PriorityMachine.require`), so every
    door a controlled player is turned away at says the same sentence."""
    return f"{controller} is making {player}'s decisions this turn (CR 723.5)"


def decision_refusal(game: Game, seat: str | None, player: str) -> str | None:
    """Why `seat` may not answer a decision addressed to `player`, or None.

    CR 723.5: while `player` is controlled their controller answers, and they
    do not. `None` for a seat that is simply not involved - that refusal is the
    caller's own ("that uid does not open the deciding player's seat")."""
    controller = decider(game, player)
    if controller != player and seat == player:
        return refusal(controller, player)
    return None


def may_decide_for(game: Game, seat: str | None, player: str) -> bool:
    """Whether `seat` is the one who answers `player`'s decisions right now."""
    return seat is not None and decider(game, player) == seat


def _owner_of(game: Game, card: CardState) -> str | None:
    """The player whose object this is for an action: a permanent's controller
    (CR 723.3 leaves it with them), otherwise whoever holds it."""
    battlefield = game.shared_zones.get(Zone.BATTLEFIELD, [])
    if any(state is card for state in battlefield):
        return game.controller_of(card).username
    for player in game.players:
        for cards in player.zones.values():
            if any(state is card for state in cards):
                return player.username
    return card.owner


def _holder_by_name(game: Game, actor: str, name: str, zone: str, candidates: list[str]) -> str:
    """Which of `actor` and the players it decides for holds a card named
    `name` in their own `zone` - `actor` first, so its own card always wins."""
    try:
        wanted = Zone(zone)
    except ValueError:
        return actor
    if wanted in SHARED_ZONES:
        return actor
    for username in (actor, *candidates):
        if any(answers_to_card_name(card, name) for card in game.player(username).cards_in(wanted)):
            return username
    return actor


def acting_as(game: Game, kind: str, request: Any, seat: str | None) -> tuple[Any, str | None]:
    """The request the engine should run for `seat`, and a refusal if any.

    `request.player` names who the seat says is acting. Three cases change it:

    - it names a controlled player and `seat` *is* that player: refused,
      CR 723.5, the engine's own sentence - except for a kind the engine
      gates itself (a pass, an advance), which it refuses in the same words;
    - it names a controlled player and `seat` is their controller, for a kind
      the engine gates itself: run as the controller, which is how the
      engine takes the controlled player's pass;
    - it names `seat`, `seat` is deciding for somebody, and the card the
      request is about is that player's (on their battlefield, in their
      hand): run as that player - CR 723.5's "what to play" is theirs to be
      decided, and CR 723.3 left the object theirs.

    Anything else is handed back untouched, which is every request made while
    no control effect is in force."""
    player = getattr(request, "player", None)
    if kind in NEVER_GATED or not player or game.turn is None:
        return request, None
    control = controllers(game)
    if not control:
        return request, None
    controller = control.get(player)
    if controller is not None:
        if kind in ENGINE_GATED:
            if seat == controller:
                return request.model_copy(update={"player": controller}), None
            return request, None
        if seat == player:
            return request, refusal(controller, player)
        return request, None
    if kind in ENGINE_GATED:
        return request, None
    theirs = deciding_for(game, player)
    if not theirs:
        return request, None
    card_id = getattr(request, "card_id", None)
    holder: str | None = None
    if card_id is not None:
        card = game.find_by_id(card_id)
        holder = _owner_of(game, card) if card is not None else None
    elif getattr(request, "card", None):
        zone = getattr(request, "from_zone", "hand") or "hand"
        holder = _holder_by_name(game, player, request.card, zone, theirs)
    if holder in theirs:
        return request.model_copy(update={"player": holder}), None
    return request, None
