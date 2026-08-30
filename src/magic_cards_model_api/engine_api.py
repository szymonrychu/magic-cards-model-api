"""The bridge to the engine: harness flows shaped as request-sized functions.

Every legality answer here comes from the engine itself, one of two ways:

- a `why_cannot_*` method returning a reason string, or
- a `ValueError` the engine raises at the point of action, caught here.

This module never invents a refusal of its own except where marked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from magic_cards_model.machines import Casting, Step
from magic_cards_model.machines.targeting import profile_for_text, targeting_profile
from magic_cards_model.models import CardState, Color, Game, Zone
from magic_cards_model.models.card_state import Counter, counter_state_based_actions
from pydantic import ValidationError

from .schemas import (
    ActivateRequest,
    AttackRequest,
    CastRequest,
    ManaSource,
    PlayLandRequest,
    TargetRequest,
)

SHARED_ZONES = (Zone.BATTLEFIELD, Zone.STACK, Zone.EXILE, Zone.COMMAND)


@dataclass
class Outcome:
    """What an action attempt came to."""

    allowed: bool
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason, **self.detail}


def refused(reason: str | Exception) -> Outcome:
    """An action was refused - `reason` becomes the message a player and the
    game log see.

    An engine model can raise a plain `ValueError` from inside a pydantic
    validator - `Casting.model_post_init` does, to refuse an illegal cast -
    and pydantic wraps that into a `ValidationError` (itself a `ValueError`
    subclass) before it reaches a caller. `ValidationError.__str__` is
    pydantic's own multi-line report - "1 validation error for Casting",
    the object repr of `input_value`, and a docs URL - not the sentence the
    engine actually raised, so passing an exception straight through here
    unwraps that shape rather than exposing it. This is general on purpose:
    every `except ValueError as exc: return refused(exc)` in this module
    goes through it, whether or not that particular exception happens to be
    pydantic-wrapped.
    """
    return Outcome(False, _engine_message(reason))


def _engine_message(reason: str | Exception) -> str:
    """The plain message a refusal should show - see `refused`."""
    if isinstance(reason, ValidationError):
        return "; ".join(_without_pydantic_prefix(error["msg"]) for error in reason.errors())
    return str(reason)


def _without_pydantic_prefix(message: str) -> str:
    """Pydantic prefixes a validator's `ValueError` message with "Value error, "
    (see `pydantic_core.ErrorDetails`'s `msg` field) - strip it back off so the
    refusal reads as the engine wrote it, not as pydantic reports it.
    """
    prefix = "Value error, "
    return message.removeprefix(prefix)


# -- lookups ---------------------------------------------------------------


def find_card(game: Game, card_id: UUID) -> CardState:
    state = game.find_by_id(card_id)
    if state is None:
        raise KeyError(f"no card with id {card_id} in this game")
    return state


def mana_from(source: CardState) -> list[Color]:
    """What tapping this source produces, straight off the parsed abilities."""
    from magic_cards_model.machines import parse_mana_abilities

    abilities = parse_mana_abilities(source.card)
    if not abilities:
        raise ValueError(f"{source.card.name} has no mana ability")
    return abilities[0].produces()


def mana_colors(source: CardState, colors: list[str] | None) -> list[Color]:
    if not colors:
        return mana_from(source)
    try:
        return [Color(color.strip().upper()) for color in colors]
    except ValueError as exc:
        raise ValueError(f"unknown mana color in {colors}: use W, U, B, R, G or C") from exc


def target_thing(game: Game, token: str) -> Any:
    """A target named as a card id or a player name."""
    if token in {player.username for player in game.players}:
        return token  # the engine accepts a username string anywhere a player goes
    try:
        card_id = UUID(token)
    except ValueError as exc:
        raise ValueError(f"target {token!r} is neither a player name nor a card id") from exc
    state = game.find_by_id(card_id)
    if state is None:
        raise ValueError(f"target {token!r} is neither a player name nor a card id")
    return state


def pay_with(
    payment: Any,
    sources: list[UUID | ManaSource],
    game: Game,
) -> None:
    """Tap the given sources into the payment, then leave the rest to `pay()`.

    The sources are the mana abilities this app may activate on the player's
    behalf - the caller is the player's agent here, not the rules. Two kinds
    of entry are told apart:

    - a `ManaSource` naming a colour (`entry.colors`) is a player's explicit
      choice - Sunbaked Canyon should make {R} and not {W} - and is tapped for
      exactly that colour, taking precedence over anything the engine would
      have picked;
    - a bare card id, or a `ManaSource` with no colour, names *which* source
      to consider but leaves *what colour it makes* to the engine. This is
      the common case: `seat_cli`'s default (every untapped source, no
      colour) and an explicit `--sources <id>` naming a dual both land here,
      and both used to crash straight out of `ManaAbility.resolve` the moment
      the named source was a dual, a shockland, or any other choice ability -
      "this ability offers [['W'], ['U']]; pass the colour chosen" - with no
      chance to try a different colour or a different source. `auto_tap`
      (`machines/cost.py`) is where that got fixed: given the exact candidate
      set named here, it finds a legal assignment of colours (or refuses with
      a clear reason if there is none), never taps more of them than the cost
      needs, and never invents a cost the player did not name - a source with
      an additional cost or an unhonourable CR 106.6 spend restriction is
      skipped by `auto_tap` itself even when explicitly named.
    """
    unpinned: list[CardState] = []
    for entry in sources:
        if isinstance(entry, ManaSource) and entry.colors:
            source = find_card(game, entry.card_id)
            payment.tap_for_mana(source, mana_colors(source, entry.colors))
        else:
            card_id = entry.card_id if isinstance(entry, ManaSource) else entry
            unpinned.append(find_card(game, card_id))

    if unpinned and payment.shortfall:
        payment.auto_tap(unpinned)


def shortfall_reason(payment: Any) -> str:
    """The engine's own wording for an unpaid cost, composed from its parts."""
    if payment.unpayable:
        return payment.unpayable
    pool = {color.value: count for color, count in payment.pool.floating.items()}
    return f"pool {pool} cannot pay {payment.cost}"


# -- casting ---------------------------------------------------------------


def locate_castable(game: Game, req: CastRequest) -> CardState:
    """The card to cast: by id, or the first name match in `from_zone`."""
    if req.card_id is not None:
        return find_card(game, req.card_id)
    if not req.card:
        raise ValueError("give either card_id or card (a name)")
    zone = Zone(req.from_zone)
    if zone in SHARED_ZONES:
        for state in game.shared_zones[zone]:
            if isinstance(state, CardState) and state.card.name == req.card:
                return state
        raise ValueError(f"no {req.card} in the {zone.value}")
    player = game.player(req.player)
    for state in player.cards_in(zone):
        if state.card.name == req.card:
            return state
    raise ValueError(f"no {req.card} in {req.player}'s {zone.value}")


def cast_card(game: Game, req: CastRequest) -> Outcome:
    """Cast a card: the engine is asked at every step and refuses with its text."""
    try:
        spell = locate_castable(game, req)
    except (KeyError, ValueError) as exc:
        return refused(exc)
    try:
        casting = Casting(spell=spell, caster=req.player, origin_zone=spell.zone, game=game)
    except ValueError as exc:
        return refused(exc)

    try:
        if req.modes:
            casting.choose_modes(req.modes, x_value=req.x_value)
        if req.targets:
            casting.choose_targets([target_thing(game, token) for token in req.targets])
    except ValueError as exc:
        return refused(exc)

    payment = None
    try:
        payment = casting.determine_cost(x_value=req.x_value)
        pay_with(payment, list(req.mana_sources or []), game)
        if req.dry_run:
            # The offered sources are activated into the payment's working
            # copy - the engine's own affordability answer, CR 601.2g and all -
            # and the whole thing is rewound, so a dry run leaves no trace.
            if payment.unpayable or payment.shortfall:
                reason = shortfall_reason(payment)
                payment.rewind()
                return Outcome(False, reason, {"cost": str(payment.cost)})
            payment.rewind()
            return Outcome(True, detail={"cost": str(payment.cost)})
        payment.pay()
    except ValueError as exc:
        if payment is not None:
            payment.rewind()
        return refused(exc)

    state = casting.cast()
    game.put_on_stack(state, casting.caster)
    game.retain_priority(casting.caster)
    return Outcome(True, detail={"spell_id": str(state.id), "cost": str(payment.cost)})


# -- targeting -------------------------------------------------------------


def check_target(game: Game, req: TargetRequest) -> Outcome:
    """May this spell or ability target that?

    A spell on the stack is answered with the engine's own recheck. A card in
    hand, or an ability line on a permanent, is answered through the same
    targeting parser the engine uses at announcement, without touching state.
    """
    try:
        target = target_thing(game, req.target)
    except ValueError as exc:
        return refused(exc)

    spell = game.find_by_id(req.source_id)
    if spell is None:
        raise ValueError(f"no card with id {req.source_id} in this game")

    controller = _controller_of(game, spell)
    if req.ability_index is not None:
        try:
            ability = game.activated_abilities(spell)[req.ability_index]
        except IndexError as exc:
            raise ValueError(f"{spell.name} has no ability {req.ability_index}") from exc
        profile = profile_for_text(ability.text)
        if profile.understood and profile.requirements:
            try:
                profile.check([target], controller, game)
            except ValueError as exc:
                return refused(exc)
        return Outcome(True, detail={"ability": ability.text})

    if spell.zone is Zone.STACK:
        thing = game.legal_target(spell, target)
        if thing is not None:
            return Outcome(True)
        reason = game.why_cannot_target(spell, target)
        return refused(reason or "no longer a legal target")

    profile = targeting_profile(spell.card)
    if profile.understood and profile.requirements:
        try:
            profile.check([target], controller, game)
        except ValueError as exc:
            return refused(exc)
    return Outcome(True)


def _controller_of(game: Game, state: CardState) -> str:
    return game.controller_of(state).username


# -- activating ------------------------------------------------------------


def activate_ability(game: Game, req: ActivateRequest) -> Outcome:
    """Activate an ability: engine refusal, then payment, then the stack.

    A basic land, a shockland, or a Ravnica dual has no printed activated
    ability of its own - the engine's `Game.activated_abilities` (and so
    `why_cannot_activate`/`begin_activating`/`activate_ability`) already
    synthesises one from the land's basic land type(s), so there is nothing
    special-cased here for it: it is just another ability at index 0.
    """
    permanent = find_card(game, req.card_id)

    try:
        color = Color(req.color.strip().upper()) if req.color else None
    except ValueError:
        return refused(f"unknown mana color {req.color!r}: use W, U, B, R, G or C")

    refusal = game.why_cannot_activate(permanent, req.ability_index, req.player)
    if refusal is not None:
        return refused(refusal)

    def resolve_states(ids: list[UUID] | None) -> list[CardState] | None:
        return [find_card(game, card_id) for card_id in ids] if ids else None

    payment = None
    try:
        payment = game.begin_activating(
            permanent,
            req.ability_index,
            req.player,
            sacrificing=resolve_states([req.sacrificing] if req.sacrificing else None),
            discarding=resolve_states([req.discarding] if req.discarding else None),
        )
        pay_with(payment, list(req.mana_sources or []), game)
        if req.dry_run:
            # As with casting: the offered sources are evaluated through the
            # engine's payment and the attempt is rewound without a trace.
            if payment.unpayable or payment.shortfall:
                reason = shortfall_reason(payment)
                payment.rewind()
                return Outcome(False, reason, {"cost": str(payment.cost)})
            payment.rewind()
            return Outcome(True, detail={"cost": str(payment.cost)})
        payment.pay()
    except ValueError as exc:
        if payment is not None:
            payment.rewind()
        return refused(exc)

    try:
        targets = [target_thing(game, token) for token in req.targets or []]
        ability = game.activate_ability(
            permanent, req.ability_index, req.player, targets=targets, color=color
        )
    except ValueError as exc:
        if payment is not None:
            payment.rewind()
        return refused(exc)

    game.retain_priority(req.player)
    if ability is None:
        return Outcome(True, detail={"mana_ability": True, "cost": str(payment.cost)})
    return Outcome(True, detail={"ability_id": str(ability.id), "cost": str(payment.cost)})


# -- attacking -------------------------------------------------------------


def declare_attack(game: Game, req: AttackRequest) -> Outcome:
    """Declare attackers, one engine refusal at a time."""
    pairs: list[tuple[CardState, Any]] = []
    for entry in req.attackers:
        creature = find_card(game, entry.card_id)
        defender = target_thing(game, entry.defender)
        reason = game.why_cannot_attack(creature, defender)
        if reason is not None:
            return refused(reason)
        pairs.append((creature, defender))

    if req.dry_run:
        return Outcome(True, detail={"attackers": len(pairs)})
    try:
        game.declare_attackers(pairs)
    except ValueError as exc:
        return refused(exc)
    return Outcome(True, detail={"attackers": len(pairs)})


# -- the rest of the flow --------------------------------------------------


def locate_land(game: Game, req: PlayLandRequest) -> CardState:
    """The card to play: by id, or the first *land* name match in hand.

    Mirrors `locate_castable`: a name that matches nothing raises `ValueError`
    rather than looking like a 404 or a bare validation error, so a caller
    that resolves the card by name gets exactly the same refusal shape as one
    that resolves it by id - see `play_land` below, which is what turns that
    into an audited `Outcome(False, ...)` instead of an exception the endpoint
    would otherwise have to raise before any audit entry is written.
    """
    if req.card_id is not None:
        return find_card(game, req.card_id)
    if not req.card:
        raise ValueError("give either card_id or card (a name)")
    for state in game.player(req.player).hand:
        if state.card.name == req.card and state.card.is_land:
            return state
    raise ValueError(f"{req.player} has no land named {req.card} in hand")


def play_land(game: Game, req: PlayLandRequest) -> Outcome:
    """Play a land: locating the card and playing it are one refusal path.

    A name that resolves to nothing is exactly as much a refusal as the
    engine saying no once the card is found - both must come back as
    `Outcome(False, ...)`, so `action_play_land` can audit either one and
    `run_action` recognises it as `allowed: False` rather than falling
    through to releasing priority as if the play had gone through.
    """
    try:
        land = locate_land(game, req)
    except (KeyError, ValueError) as exc:
        return refused(exc)
    try:
        played = game.play_land(land, req.player)
    except ValueError as exc:
        return refused(exc)
    return Outcome(True, detail={"card_id": str(played.id)})


def run_state_based_actions(game: Game) -> dict[str, Any]:
    """Counter-then-check, the same order the harness uses."""
    leaving = counter_state_based_actions(game.battlefield)
    destroyed = [state.name for state in leaving]
    for state in leaving:
        game.destroy(state)
    changed = game.check_state_based_actions()
    return {
        "destroyed": destroyed,
        "changed": [state.name for state in changed],
    }


def resolve_top(game: Game) -> dict[str, Any]:
    try:
        resolved = game.resolve_top()
    except ValueError as exc:
        return {"resolved": None, "reason": str(exc)}
    if isinstance(resolved, CardState):
        return {"resolved": resolved.name, "zone": resolved.zone.value}
    return {"resolved": f"ability from {resolved.source_name}", "zone": None}


def set_step(game: Game, step: Step) -> None:
    """Set the step. Forward goes through the engine; backward is raw.

    A forward move crosses step boundaries, and CR 500.5 empties mana pools as
    each of them ends - `Game.advance_to` performs that. A backward move is
    board setup, rewinding the turn marker on purpose, so no rules apply to it.
    """
    assert game.turn is not None
    if game.turn.step is Step.UNTAP and step is not Step.UNTAP:
        game.untap_step()
    try:
        game.advance_to(step)
    except ValueError:
        game.turn.step = step  # raw setup: rewinding the turn marker on purpose


def _mana_ability_text(ability: Any) -> str:
    """A display line for an intrinsic mana ability, read off its parsed shape.

    These abilities are reminder text on the card (a basic land's whole oracle
    text is `({T}: Add {G}.)`), so there is no printed ability text to show.
    """
    if ability.any_color:
        return "{T}: Add one mana of any colour"
    if len(ability.options) == 1:
        return "{T}: Add " + "".join("{" + color.value + "}" for color in ability.options[0])
    options = " or ".join(
        "{" + "".join(color.value for color in option) + "}" for option in ability.options
    )
    return "{T}: Add " + options


def list_abilities(game: Game, state: CardState) -> list[dict[str, Any]]:
    """The activated abilities the engine would let a caller index into.

    `Game.activated_abilities` already synthesises a basic land's (or a
    shockland's, or a Ravnica dual's) intrinsic mana ability at index 0 when
    the card prints no activated ability of its own (`intrinsic_land_ability`,
    CR 305.6), so the normal loop below covers it directly - the `activate`
    endpoint no longer needs a separate mana-ability index range for it.

    The `parse_mana_abilities` fallback loop still runs for the one shape that
    is not covered by that: a card whose printed activated abilities are all
    non-mana, so `Game.activated_abilities` never reaches the intrinsic
    fallback, but which still carries a basic land type. Nothing in the pool
    is shaped like that today.
    """
    from magic_cards_model.machines import parse_mana_abilities

    try:
        abilities = game.activated_abilities(state)
    except Exception:
        return []
    listed = []
    for index, ability in enumerate(abilities):
        listed.append(
            {
                "index": index,
                "text": ability.text,
                "mana": getattr(ability, "mana", None) is not None,
            }
        )
    if any(entry["mana"] for entry in listed):
        return listed
    for offset, ability in enumerate(parse_mana_abilities(state.card)):
        listed.append(
            {
                "index": len(abilities) + offset,
                "text": _mana_ability_text(ability),
                "mana": True,
            }
        )
    return listed


def move_card(
    game: Game,
    state: CardState,
    to_zone: Zone,
    player_name: str | None,
    controller: str | None,
    position: str | None,
    tapped: bool | None,
    face_up: bool | None,
) -> Outcome:
    """Move a card between zones. Tokens that may not move are refused."""
    from magic_cards_model.models.token import move_refused

    refusal = move_refused(state, to_zone)
    if refusal is not None:
        return refused(refusal)

    lift_card(game, state)
    if to_zone in SHARED_ZONES:
        if controller:
            state.controller = controller
        state.move_to(to_zone)
        game.shared_zones[to_zone].append(state)
    else:
        if not player_name:
            return refused(f"the {to_zone.value} needs a player")
        target = game.player(player_name)
        target.add(state, to_zone)
        if to_zone is Zone.LIBRARY and position != "bottom":
            # index 0 is the top of the library: draws come off the front
            target.library.remove(state)
            target.library.insert(0, state)

    if tapped is not None:
        state.tapped = tapped
    if face_up is not None:
        state.face_up = face_up
    return Outcome(True)


def lift_card(game: Game, state: CardState) -> None:
    """Take a card out of every zone it might be sitting in."""
    for zone_cards in game.shared_zones.values():
        if state in zone_cards:
            zone_cards.remove(state)
    for player in game.players:
        for zone_cards in player.zones.values():
            if state in zone_cards:
                zone_cards.remove(state)


def put_card_in_zone(
    game: Game,
    card: CardState,
    player_name: str,
    zone: Zone,
    position: str | None,
) -> None:
    """Set a card into a zone, the way the harness stages a scenario."""
    if zone in SHARED_ZONES:
        game.shared_zones[zone].append(card)
        return
    player = game.player(player_name)
    player.add(card, zone)
    if zone is Zone.LIBRARY and position != "bottom":
        player.library.remove(card)
        player.library.insert(0, card)


def parse_counter_kind(kind: str) -> Counter:
    try:
        return Counter(kind)
    except ValueError as exc:
        known = ", ".join(value for value in Counter)
        raise ValueError(f"unknown counter kind {kind!r}; known kinds: {known}") from exc
