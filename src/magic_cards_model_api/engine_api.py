"""The bridge to the engine: harness flows shaped as request-sized functions.

Every legality answer here comes from the engine itself, one of two ways:

- a `why_cannot_*` method returning a reason string, or
- a `ValueError` the engine raises at the point of action, caught here.

This module never invents a refusal of its own except where marked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from magic_cards_model.machines import Casting, Step
from magic_cards_model.machines.casting import (
    MarkedExile,
    PlayAction,
    finish_suspend,
    play_permissions_on,
    remove_suspend_time_counter,
)
from magic_cards_model.machines.targeting import profile_for_text, targeting_profile
from magic_cards_model.models import CardState, Color, Game, Zone
from magic_cards_model.models.card_state import Counter, ExileMarker, counter_state_based_actions
from pydantic import ValidationError

from .schemas import (
    ActivateRequest,
    AttackRequest,
    CastRequest,
    CrewRequest,
    CycleRequest,
    ManaSource,
    PlayLandRequest,
    SpecialActionRequest,
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


def tapped_view(payment: Any) -> list[dict[str, str]]:
    """Which sources this payment has tapped so far, oldest first - read
    before `rewind()` on a dry run, so a client can preview auto-pay.

    `CostPayment.tapped_for_mana` holds ids, not `CardState`s: holding the
    objects made every tapped permanent round-trip as a second copy of itself,
    and untapping those decoded copies left the real battlefield sources tapped
    forever. Older engines still hold the objects, so both shapes are read
    here. An id whose object the game cannot resolve is dropped rather than
    guessed at - the view is a preview, and a wrong name in it would be worse
    than a shorter list.
    """
    game = getattr(payment, "game", None)
    view: list[dict[str, str]] = []
    for entry in getattr(payment, "tapped_for_mana", []):
        source = entry
        if isinstance(entry, UUID):
            source = game.find_by_id(entry) if game is not None else None
            if source is None:
                # A payment opened with no `game` at all keeps the live objects
                # in `CostPayment._tapped_sources` and has nothing to ask.
                source = getattr(payment, "_tapped_sources", {}).get(entry)
            if source is None:
                continue
        view.append({"card_id": str(source.id), "name": source.name})
    return view


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


def rewind_unfinished(casting: Casting) -> None:
    """Rewind a proposal that was never completed (CR 601.6, CR 733.1).

    CR 601.2a moves the card to the stack the moment a cast is *proposed*, so
    an abandoned proposal is not free any more: whoever built the `Casting`
    owns undoing it. CR 733.1 is the undo - "the entire action is reversed and
    any payments already made are canceled" - and `Casting.cancel()` is the
    engine's own implementation of it, restoring the zone, the CR 400.7
    identity stamp and the exile pile the card had. Reaching into
    `game.stack` from here instead would restore none of those.

    `cancel()` is not idempotent - it raises once the casting is settled - and
    the engine's `_refusing` wrapper already cancels for the announcement steps
    that go through it, so the state is asked before the call rather than the
    exception being swallowed.

    What this does *not* restore: `cancel()` puts the card back with
    `Player.add`, which appends to the end of its zone's list rather than
    reinserting at the index it started from - a hand `[A, B, C]` cancelled
    for A comes back `[B, C, A]`, every field on A correct, its position
    wrong. That is precisely the corruption `dry_run_copy` below exists to
    make irrelevant for a probe: this function is still what a genuine
    abandoned cast (a real refusal on the caller's own game) rewinds with,
    since there the move must in fact be undone in place, not thrown away.
    """
    if not casting.is_settled:
        casting.cancel()


def dry_run_copy(game: Game) -> Game:
    """An isolated copy of `game` for a probe to build a `Casting` (or any
    other proposal) against and mutate freely, so answering "would this be
    legal" never touches the game anyone is actually playing.

    CR 601.2a moves a card to the stack the instant a `Casting` is built,
    whether or not the caller ever means to finish the cast, and
    `rewind_unfinished`'s `cancel()` does not put it back where it was - see
    that function's docstring for the exact defect. Rather than audit every
    rewind path for that bug (and the next one like it), a dry run is given
    a throwaway board to wreck instead: nothing needs to be undone correctly
    if nothing real was ever touched.

    `game.decisions.provider` is swapped out before the copy and restored
    right after. It is a live `GameDecisionProvider` (`magic_cards_model_api
    .decisions`) holding a `DecisionMailbox`'s `threading.Lock`/`Condition`,
    and `copy.deepcopy` - which `model_copy(deep=True)` uses internally -
    cannot copy a lock (`TypeError: cannot pickle '_thread.lock' object`);
    left in place, this call would crash every dry run on a real game the
    moment it had a live seat behind it. The copy's own `provider` is set to
    `None` rather than pointed back at the original mailbox: a decision the
    probe somehow triggered must never reach the request a real player is
    parked on, and must never block forever waiting for an answer nobody
    will send for a game nobody is playing. `Game.ask` already treats "no
    provider" as "let the engine's own `default_answer` speak for the
    player" (see that method) - the same deterministic, non-blocking answer
    a scenario test gets, which is the right one for a probe too.

    One further trap, once the engine grows `game_uid`/`objects_created`
    (deterministic per-object ids keyed on `game_uid`, so a game replays
    byte-for-byte): a copy inherits both verbatim, so an id minted while
    probing is exactly the id the real `game` mints later for a genuinely
    new object. Neither field exists on this engine's current `main` yet -
    checked with `git grep game_uid` in `magic-cards-model`, which finds it
    only in two in-flight worktrees - so the `hasattr` guard below is inert
    today and does the right thing the moment that lands, with no further
    change here. Belt and braces regardless: no dry-run branch in this
    module ever puts a freshly minted id in the `Outcome` it returns (only
    pre-existing ids, e.g. `tapped`), and `audit_outcome` never records a
    dry run at all - so nothing minted on a probe copy has anywhere to
    escape to even before that field exists.
    """
    provider = game.decisions.provider
    game.decisions.provider = None
    try:
        probe = game.model_copy(deep=True)
    finally:
        game.decisions.provider = provider
    probe.decisions.provider = None
    if hasattr(probe, "game_uid"):
        probe.game_uid = uuid4()
    return probe


def cast_card(game: Game, req: CastRequest) -> Outcome:
    """Cast a card: the engine is asked at every step and refuses with its text.

    The proposal is driven by `_drive_cast` under a `finally`, because every
    exit that is not a completed `cast()` - a refusal, a `dry_run` probe, or a
    `KeyError` raised half way through announcing - is an action that was
    started and not legally completed, and CR 733.1 reverses all three the
    same way. A dry run in particular is *only* ever a probe: it asks the
    engine what a cast would cost and must leave no trace, which matters
    because `seatkit.available_actions` issues one per card in hand on every
    poll - so a dry run is driven against `dry_run_copy(game)` instead of
    `game` itself, and the real game is never assigned to below this point.
    """
    working_game = dry_run_copy(game) if req.dry_run else game
    try:
        spell = locate_castable(working_game, req)
    except (KeyError, ValueError) as exc:
        return refused(exc)
    origin_zone = spell.zone
    # CR 601.3 is a question about the moment *before* the proposal: whether a
    # rule or effect allows this player to cast this card at all. It has to be
    # asked here, because CR 601.2a moves the card to the stack as the
    # `Casting` is built, and the object it becomes there is a new one
    # (CR 400.7) - `spell.zone` afterwards reads `stack`, and the permission
    # this looks for is recorded against the pre-move CR 400.7 stamp. Asking
    # after the move silently found no permission and refused Living End and
    # every suspend free cast.
    permitted = (
        origin_zone is Zone.EXILE
        and working_game.play_permission_for(spell, req.player) is not None
    )
    try:
        # CR 601.3's checks run before CR 601.2a's move, so a `Casting` that
        # fails to build has moved nothing and leaves nothing to rewind.
        casting = Casting(
            spell=spell, caster=req.player, origin_zone=origin_zone, game=working_game
        )
    except ValueError as exc:
        return refused(exc)

    try:
        return _drive_cast(working_game, req, casting, permitted=permitted)
    finally:
        rewind_unfinished(casting)


def _drive_cast(game: Game, req: CastRequest, casting: Casting, *, permitted: bool) -> Outcome:
    """Announce, pay and cast an already-proposed `Casting` (CR 601.2b-h).

    Every return that is not the final `Outcome` at the bottom leaves the
    casting unsettled on purpose - `cast_card`'s `finally` rewinds it.
    """
    try:
        if req.alternative_cost is not None:
            # CR 118.9, announced at CR 601.2b - before targets are chosen at
            # CR 601.2c, which is why this runs ahead of `choose_targets`
            # below. `use_alternative_cost` is the engine's one front door
            # over all four origins (a printed pitch offer, a keyword, a
            # granted permission, another permanent's static ability), so
            # this needs no branch per origin.
            casting.use_alternative_cost(
                req.alternative_cost,
                choosing=[find_card(game, card_id) for card_id in req.choosing]
                if req.choosing
                else None,
            )
        elif req.keyword:
            # CR 601.2b: plot and foretell are announced this way - the
            # keyword names both the exile pile the card must be in and what
            # is paid instead of the mana cost (`Casting.use_keyword`).
            casting.use_keyword(req.keyword)
        elif permitted:
            # CR 601.3's dynamic half: a live `PlayPermission` covering this
            # exact object, player and action - suspend's own free cast
            # (`finish_suspend`) grants one of these rather than a keyword,
            # so it is picked up automatically here instead of needing a
            # second parameter naming it. `permitted` was read before the
            # proposal moved the card (see `cast_card`); `use_permission`
            # itself looks the permission up again against the announced
            # object, so this only decides *whether* to ask.
            casting.use_permission()
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
        if req.shares is not None:
            # CR 601.2d: announced after targets and after the cost is
            # determined, since a variable (X) division needs `x_value`
            # already settled - `announce_division`'s own docstring says so.
            casting.announce_division(req.shares)
        # CR 702.51b/702.66a: convoke and delve are neither additional nor
        # alternative costs - they "apply only after the total cost ... is
        # determined" - so they are spliced in exactly here, between
        # `determine_cost` and the mana payment, and `payment.cost` still
        # reads as the spell's real total cost afterwards.
        apply_object_costs(game, casting.spell, payment, req)
        pay_with(payment, list(req.mana_sources or []), game)
        if req.dry_run:
            # The offered sources are activated into the payment's working
            # copy - the engine's own affordability answer, CR 601.2g and all -
            # and the whole thing is rewound, so a dry run leaves no trace.
            tapped = tapped_view(payment)
            if payment.unpayable or payment.shortfall:
                reason = shortfall_reason(payment)
                payment.rewind()
                return Outcome(False, reason, {"cost": str(payment.cost), "tapped": tapped})
            payment.rewind()
            return Outcome(True, detail={"cost": str(payment.cost), "tapped": tapped})
        payment.pay()
    except ValueError as exc:
        if payment is not None:
            payment.rewind()
        return refused(exc)

    try:
        # CR 601.2i's last gate, and the only step of CR 601.2 that was ever
        # left unguarded here. It refuses for reasons no earlier step can
        # raise - CR 601.3's permission asked a second time, now that the
        # announcement is complete: a cast from any zone but the hand has to
        # have *claimed* the thing that allows it (a keyword under CR 601.2b,
        # or a live permission), not merely have had one available. Faithful
        # Mending cast from a graveyard with no `keyword` lands here.
        #
        # Without this `except`, the `ValueError` escaped as an uncaught 400
        # whose body is `{"detail": ...}` rather than an `Outcome`. That shape
        # carries no `allowed` key, so `seatkit._refused` reads it as "not a
        # refusal", `run_action` treats the call as having gone through, and
        # the seat auto-passes priority on a spell that was never cast - and
        # nothing about the attempt reaches the game log either.
        state = casting.cast()
    except ValueError as exc:
        return refused(exc)
    game.put_on_stack(state, casting.caster)
    game.retain_priority(casting.caster)
    return Outcome(True, detail={"spell_id": str(state.id), "cost": str(payment.cost)})


def apply_object_costs(game: Game, spell: CardState, payment: Any, req: CastRequest) -> None:
    """Offer convoke creatures and delve cards against an open payment.

    Both keywords pay part of an already-determined cost by spending objects
    rather than mana - tapping an untapped creature you control (CR 702.51a),
    exiling a card from your graveyard (CR 702.66a) - which is why they share
    `machines/objects_as_costs.py` in the engine and share this one hook here.
    The objects are spent when the payment is paid, and given back if it is
    rewound (CR 733.1), so a refusal after this point leaves no trace.
    """
    from magic_cards_model.machines.objects_as_costs import convoke, delve

    for choice in req.convoke or []:
        creature = find_card(game, choice.card_id)
        color = Color(choice.color.strip().upper()) if choice.color else None
        convoke(payment, spell, creature, color)
    if req.delve:
        delve(payment, spell, *[find_card(game, card_id) for card_id in req.delve])


# -- crewing a Vehicle (CR 702.122) ----------------------------------------


def crew_vehicle(game: Game, req: CrewRequest) -> Outcome:
    """CR 702.122a's crew ability: tap creatures with total power N or more,
    and the Vehicle becomes an artifact creature until end of turn.

    Not an `ActivateRequest` against an ability index: crew is an activated
    ability of the *Vehicle*, its cost is the creatures rather than any mana,
    and it carries no `{T}` of its own - so `Game.begin_activating` (which
    only ever opens a mana-or-symbol cost on a permanent's printed ability)
    is not the right door. `machines.keywords.crew_activation` is, the same
    object the engine's own `tests/scenarios/test_crew.py` builds.
    """
    from magic_cards_model.machines.keywords import crew_activation

    try:
        vehicle = find_card(game, req.card_id)
        crewing = [find_card(game, card_id) for card_id in req.crewing]
        activation = crew_activation(game, vehicle, crewing, req.player)
    except (KeyError, ValueError) as exc:
        return refused(exc)

    payment = None
    try:
        payment = activation.begin_paying()
        if req.dry_run:
            tapped = tapped_view(payment)
            if payment.unpayable or payment.shortfall:
                reason = shortfall_reason(payment)
                payment.rewind()
                return Outcome(False, reason, {"cost": str(payment.cost), "tapped": tapped})
            payment.rewind()
            return Outcome(True, detail={"cost": str(payment.cost), "tapped": tapped})
        payment.pay()
    except ValueError as exc:
        if payment is not None:
            payment.rewind()
        return refused(exc)

    on_stack = activation.activate()
    game.retain_priority(req.player)
    return Outcome(True, detail={"ability_id": str(on_stack.id), "cost": str(payment.cost)})


# -- special actions: suspend, plot, foretell (CR 116.2) --------------------


def locate_hand_card(
    game: Game, player: str, card_id: UUID | None, card_name: str | None
) -> CardState:
    """The card a special action names: by id, or the first name match in
    `player`'s hand - the same shape `locate_castable` gives `cast_card`."""
    if card_id is not None:
        return find_card(game, card_id)
    if not card_name:
        raise ValueError("give either card_id or card (a name)")
    for state in game.player(player).hand:
        if state.card.name == card_name:
            return state
    raise ValueError(f"{player} has no {card_name} in hand")


def _take_marked_exile(game: Game, action: MarkedExile, req: SpecialActionRequest) -> Outcome:
    """Pay a `MarkedExile` special action's cost, then take it - the same
    payment/dry-run shape `cast_card`/`activate_ability` already use. Building
    `action` itself (`MarkedExile.suspend`/`.plot`/`.foretell`) never mutates
    the game - only `.take()` moves the card - so a caller that fails before
    reaching this leaves nothing to roll back beyond the payment itself.
    """
    payment = action.begin_paying()
    try:
        pay_with(payment, list(req.mana_sources or []), game)
        if req.dry_run:
            tapped = tapped_view(payment)
            if payment.unpayable or payment.shortfall:
                reason = shortfall_reason(payment)
                payment.rewind()
                return Outcome(False, reason, {"cost": str(payment.cost), "tapped": tapped})
            payment.rewind()
            return Outcome(True, detail={"cost": str(payment.cost), "tapped": tapped})
        payment.pay()
    except ValueError as exc:
        payment.rewind()
        return refused(exc)

    card = action.take()
    detail: dict[str, Any] = {"card_id": str(card.id), "cost": str(payment.cost)}
    if action.time_counters is not None:
        detail["time_counters"] = action.time_counters
    return Outcome(True, detail=detail)


def suspend_card(game: Game, req: SpecialActionRequest) -> Outcome:
    """CR 116.2f/702.62a: exile a card from hand with N time counters, paying
    its suspend cost. The free cast once the last counter is removed is
    `drive_suspend_upkeep`'s job, not this one's."""
    try:
        card = locate_hand_card(game, req.player, req.card_id, req.card)
        action = MarkedExile.suspend(game, card, req.player)
    except (KeyError, ValueError) as exc:
        return refused(exc)
    return _take_marked_exile(game, action, req)


def plot_card(game: Game, req: SpecialActionRequest) -> Outcome:
    """CR 116.2k/702.170b: exile a card with plot from hand, paying its plot cost."""
    try:
        card = locate_hand_card(game, req.player, req.card_id, req.card)
        action = MarkedExile.plot(game, card, req.player)
    except (KeyError, ValueError) as exc:
        return refused(exc)
    return _take_marked_exile(game, action, req)


def foretell_card(game: Game, req: SpecialActionRequest) -> Outcome:
    """CR 116.2h/702.143b: pay {2} and exile a card with foretell from hand,
    face down."""
    try:
        card = locate_hand_card(game, req.player, req.card_id, req.card)
        action = MarkedExile.foretell(game, card, req.player)
    except (KeyError, ValueError) as exc:
        return refused(exc)
    return _take_marked_exile(game, action, req)


def drive_suspend_upkeep(game: Game, player: str) -> list[dict[str, Any]]:
    """CR 702.62a's second and third abilities, hand-driven for `player`'s own
    upkeep: nothing in `Game.advance`/`Game.advance_to` dispatches a per-card
    upkeep trigger any more than it dispatches combat's own declarations (see
    `machines.casting.remove_suspend_time_counter`'s docstring), so this is
    called once from the `advance`/`step` endpoints whenever the turn marker
    lands on `Step.UPKEEP`, the same hand-driven call a scenario test makes
    for each upkeep it cares about.

    For every card `player` owns that is suspended in exile (CR 702.62b):
    removes one time counter, and - if that was the last one - grants the
    CR 702.62a third-ability `PlayPermission` (`finish_suspend`) so a
    following `cast_card` call picks it up automatically. Returns one entry
    per suspended card touched, for the caller's own response/audit detail.
    """
    touched: list[dict[str, Any]] = []
    for card in list(game.shared_zones.get(Zone.EXILE, [])):
        if not isinstance(card, CardState):
            continue
        annotation = card.exile_annotation
        if annotation is None or annotation.marker is not ExileMarker.SUSPENDED:
            continue
        if annotation.owner != player:
            continue
        remaining = remove_suspend_time_counter(game, card)
        entry: dict[str, Any] = {
            "card_id": str(card.id),
            "card": card.name,
            "time_counters": remaining,
        }
        if remaining == 0:
            permission = finish_suspend(game, card)
            entry["cast_permission_granted"] = permission is not None
        touched.append(entry)
    return touched


# -- cycling (CR 702.29): an activated ability that functions only in hand --


def cycle_card(game: Game, req: CycleRequest) -> Outcome:
    """CR 702.29a: "[Cost], Discard this card: Draw a card." (or, typecycled,
    search for a card of the named type.) Unlike `activate_ability`, this has
    no `Game.begin_activating`/`why_cannot_activate` to call through - cycling
    "functions only while the card... is in a player's hand" (CR 702.29b),
    which is exactly the case `Game.begin_activating` never covers (it only
    ever activates a battlefield permanent) - so `machines.keywords.
    cycling_activation` is used directly, the same object the engine's own
    `tests/scenarios/test_cycling.py` builds.

    Puts the ability on the stack rather than resolving it immediately
    (`cycle()`'s own "when nobody responds" shortcut is not used here): the
    same shape `activate_ability` already gives every other activated
    ability, so it can be responded to and is resolved through the ordinary
    `resolve-top`/`pass-priority` flow.
    """
    from magic_cards_model.machines.keywords import cycling_activation

    try:
        card = locate_hand_card(game, req.player, req.card_id, req.card)
        activation = cycling_activation(game, card, req.player, req.ability_index)
    except (KeyError, ValueError) as exc:
        return refused(exc)

    payment = None
    try:
        payment = activation.begin_paying()
        pay_with(payment, list(req.mana_sources or []), game)
        if req.dry_run:
            tapped = tapped_view(payment)
            if payment.unpayable or payment.shortfall:
                reason = shortfall_reason(payment)
                payment.rewind()
                return Outcome(False, reason, {"cost": str(payment.cost), "tapped": tapped})
            payment.rewind()
            return Outcome(True, detail={"cost": str(payment.cost), "tapped": tapped})
        payment.pay()
    except ValueError as exc:
        if payment is not None:
            payment.rewind()
        return refused(exc)

    on_stack = activation.activate()
    game.retain_priority(req.player)
    return Outcome(True, detail={"ability_id": str(on_stack.id), "cost": str(payment.cost)})


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
            tapped = tapped_view(payment)
            if payment.unpayable or payment.shortfall:
                reason = shortfall_reason(payment)
                payment.rewind()
                return Outcome(False, reason, {"cost": str(payment.cost), "tapped": tapped})
            payment.rewind()
            return Outcome(True, detail={"cost": str(payment.cost), "tapped": tapped})
        payment.pay()
    except ValueError as exc:
        if payment is not None:
            payment.rewind()
        return refused(exc)

    try:
        targets = (
            [target_thing(game, token) for token in req.targets]
            if req.targets is not None
            else None
        )
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


def _not_a_land_refusal(land: CardState) -> str | None:
    """The very first thing `Game.play_land` checks, before it even asks
    `why_cannot_play_land`: is `land` actually playable as a land at all.

    Mirrors `Game.play_land` at `.../magic_cards_model/models/game.py:7862`,
    same wording - a modal double-faced card counts through `Card.land_face`
    (CR 712.12), same as there. Kept as its own function, and called first in
    `play_land` below, so a dry run's check order matches the real one's
    exactly: this, then `Game.why_cannot_play_land`, then `_land_zone_refusal`.
    """
    land_face = land.card.land_face
    if not land.card.is_land and land_face is None:
        return f"{land.name} is not a land"
    return None


def _land_zone_refusal(game: Game, land: CardState, player: str) -> str | None:
    """The last of `Game.play_land`'s pre-mutation checks: is `land` somewhere
    `player` may play it from (their hand, or a live `PlayAction.PLAY`
    permission).

    Mirrors `Game.play_land` at `.../magic_cards_model/models/game.py:7862`,
    same wording - it runs after the is-a-land check and after
    `why_cannot_play_land`, exactly as `Game.play_land` runs it, which is why
    `play_land` below calls this last rather than folding it into one
    function with `_not_a_land_refusal`. Keep this in step with
    `Game.play_land` if that ever changes.
    """
    if land.zone is not Zone.HAND:
        permission = game.play_permission_for(land, player, PlayAction.PLAY)
        if permission is None:
            granted = play_permissions_on(game, land)
            if granted:
                return granted[0].why_not(land, player, game, action=PlayAction.PLAY)
            return (
                f"nothing allows {player} to play {land.name} from the "
                f"{land.zone.value} (CR 305.1, CR 701.18a)"
            )
    return None


def play_land(game: Game, req: PlayLandRequest) -> Outcome:
    """Play a land: locating the card and playing it are one refusal path.

    A name that resolves to nothing is exactly as much a refusal as the
    engine saying no once the card is found - both must come back as
    `Outcome(False, ...)`, so `action_play_land` can audit either one and
    `run_action` recognises it as `allowed: False` rather than falling
    through to releasing priority as if the play had gone through.

    `req.dry_run` answers without playing, checking in the same order
    `Game.play_land` does: `_not_a_land_refusal` first, then
    `Game.why_cannot_play_land` (the player-level checks - priority, main
    phase, empty stack, land drops left), then `_land_zone_refusal` last.
    """
    try:
        land = locate_land(game, req)
    except (KeyError, ValueError) as exc:
        return refused(exc)
    if req.dry_run:
        refusal = (
            _not_a_land_refusal(land)
            or game.why_cannot_play_land(req.player)
            or _land_zone_refusal(game, land, req.player)
        )
        return Outcome(refusal is None, refusal)
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


#: How many priorityless steps in a row `settle_priorityless_steps` will cross
#: before it gives up. CR 500.3 names only two such steps, and the longest real
#: run is cleanup -> untap -> (upkeep, which grants priority): two crossings.
#: Four is that with room to spare, and a hard stop so a mis-read of
#: `step_grants_priority` can never spin a request for ever.
MAX_PRIORITYLESS_STEPS = 4


def settle_priorityless_steps(game: Game) -> list[str]:
    """Run the game on past any step in which nobody receives priority.

    CR 500.3: "A step in which no players receive priority ends when all
    specified actions that take place during that step are completed. The only
    such steps are the untap step (see rule 502) and certain cleanup steps (see
    rule 514)." Nothing has to *ask* a player to leave such a step - the step
    ending is the next thing that happens, not a move somebody makes.

    The engine deliberately rests in those steps: they are observable places
    its own suite steps into, and `Game.pass_priority` refuses there outright
    (CR 502.4: "No player receives priority during the untap step"). That is
    right for the engine and wrong for a live game, where resting in a step
    that grants nobody priority is a frozen page: `priority_holder` is `None`,
    so no seat - human, `seat_cli`, MCP, or the AI seat - has anything it is
    allowed to do, and nothing will ever change that. This is where that gets
    settled, so every client is handed a state somebody can act on.

    Returns the steps crossed into, in order, so a caller can report and log
    what moved. Deliberately conservative - it stops, leaving the game exactly
    where it is, on any of:

    - the game being over, or still in CR 103.8's pregame;
    - `Game.step_grants_priority` being true, which is the honest answer for
      the step in progress and covers CR 514.3a's cleanup exception - a cleanup
      step with a trigger or a state-based action waiting *does* grant priority
      and must not be crossed;
    - a non-empty stack, which is something waiting on a player rather than on
      the step ending;
    - a step outside CR 500.3's two, which cannot be priorityless at all;
    - the engine refusing the advance, which is then left as it was.

    A trigger held over from the untap step by CR 502.4's second sentence
    ("Any ability that triggers during this step will be held until the next
    time a player would receive priority") is not dropped here: it stays in
    `pending_triggers`, and `Game.advance` puts it on the stack on arriving at
    the first step that does grant priority - which is where this loop stops.

    A pending decision is the caller's guard, not this one's: every call site
    holds the game lock, so a decision that was asked has already been answered
    (or defaulted) by the time control reaches here.
    """
    crossed: list[str] = []
    for _ in range(MAX_PRIORITYLESS_STEPS):
        if game.is_over or game.turn is None or game.turn.in_pregame:
            break
        if game.step_grants_priority or not game.stack_is_empty:
            break
        if game.turn.step not in (Step.UNTAP, Step.CLEANUP):
            break
        try:
            crossed.append(game.advance().value)
        except ValueError:  # the engine's own refusal - leave the game as it is
            break
    return crossed


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
    from .affordances import ability_hints

    listed = []
    for index, ability in enumerate(abilities):
        listed.append(
            {
                "index": index,
                "text": ability.text,
                "mana": getattr(ability, "mana", None) is not None,
                # What activating it actually asks the player for: its cost, a
                # loyalty symbol (CR 606.2), an X to announce, and whether it
                # targets at all. A UI without this cannot tell Karn's `+1`
                # (which targets) from his `-2` (which does not), and sending
                # an empty target list for the one that targets is read by the
                # engine as "I choose none" rather than "not chosen yet" - see
                # `affordances.py`'s module docstring.
                **ability_hints(ability),
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
                **ability_hints(ability),
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
