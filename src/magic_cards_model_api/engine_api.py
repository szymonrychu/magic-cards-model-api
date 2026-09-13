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
    adventure_face,
    play_permissions_on,
)
from magic_cards_model.machines.targeting import profile_for_text, targeting_profile
from magic_cards_model.models import CardState, Color, Game, Layout, Zone
from magic_cards_model.models.card_state import Counter, ExileMarker, counter_state_based_actions
from pydantic import ValidationError

from .decisions import player_or_card
from .schemas import (
    ActivateRequest,
    AttackRequest,
    CastRequest,
    CrewRequest,
    CycleRequest,
    EquipRequest,
    ManaSource,
    OptionalCostChoice,
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


def is_mana_ability(ability: Any) -> bool:
    """CR 605.1a: a mana ability "could add mana ... it's not a loyalty
    ability". The engine's parsed `mana` is set on any ability that adds
    mana, Ugin, Eye of the Storms' `0: Add {C}{C}{C}` included - which uses
    the stack like every loyalty ability (CR 606.3)."""
    return getattr(ability, "mana", None) is not None and getattr(ability, "loyalty", None) is None


def without_loyalty_mana_advice(game: Game, text: str | None) -> str | None:
    """`text` without the engine's "activate it manually to include it" advice
    about a loyalty ability.

    The engine's payment planner (`machines/cost._withheld_mana_sources`)
    names every untapped source whose only mana ability costs more than
    `{T}`, and reads a loyalty ability that adds mana as one - so every
    refused cast told a seat to activate Ugin's `0` mid-cast. CR 605.1a says
    it is not a mana ability, and CR 606.3 lets it be activated only with the
    stack empty, never while a spell is being paid for. The clause is built
    here from the same fields the engine formats it from, for each loyalty
    ability on the battlefield, and removed; every other source's advice is
    left as the engine wrote it."""
    if not text or "activate it manually" not in text:
        return text
    for permanent in list(game.battlefield):
        try:
            abilities = game.activated_abilities(permanent)
        except Exception:  # noqa: BLE001 - a read of refusal prose must not raise
            continue
        for ability in abilities:
            if getattr(ability, "mana", None) is None or getattr(ability, "loyalty", None) is None:
                continue
            clause = (
                f"{permanent.name} was not auto-tapped because its only mana ability costs "
                f"more than {{T}} ({ability.cost}: {ability.effect}) - activate it manually to "
                "include it"
            )
            for joined in (f"{clause}; ", f"; {clause}", f": {clause}", clause):
                text = text.replace(joined, "")
    return text


def unreadable_modes(card: CardState) -> list[Any]:
    """The modes of `card` the engine cannot read into an effect.

    `effects.resolve_modal_spell` runs each chosen mode through
    `parse_mode_effect` and records a `ModeNotice` for one that reads as
    nothing, skipping it (CR 700.2a's modes, carried out one by one). The same
    reading, asked before the cast, is what tells a seat which modes would
    spend the card for nothing."""
    from magic_cards_model.machines.effects import parse_mode_effect
    from magic_cards_model.machines.modes import modal_profile

    profile = modal_profile(card.card)
    if profile is None or not profile.understood:
        return []
    return [mode for mode in profile.modes if parse_mode_effect(mode, 0) is None]


def unreadable_mode_notice(card: CardState, mode: Any) -> str:
    return (
        f"{card.name}'s mode {mode.number} ({mode.text!r}) cannot be read by this engine: "
        "chosen, it would resolve doing nothing and leave only a mode notice (CR 700.2a), "
        "so a cast choosing it is refused"
    )


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


def mana_ability_at(game: Game, source: CardState, ability_index: int) -> Any:
    """`source`'s activated ability number `ability_index`, refused in words
    when there is no such ability or it makes no mana.

    The index space is `Game.activated_abilities`' own - the same one
    `AbilityView.index` publishes, `ActivateRequest.ability_index` takes and
    `machines.cost.tappable_mana_activations` reports. CR 613.5: those are the
    permanent's *computed* abilities, so a permanent that has become a copy of
    something offers the copy's abilities here, never the printed card's.
    """
    try:
        abilities = list(game.activated_abilities(source))
    except Exception as exc:  # the engine's own refusal to read this object
        raise ValueError(f"{source.name} has no activated abilities to index into") from exc
    if not 0 <= ability_index < len(abilities):
        raise ValueError(
            f"{source.name} has no ability {ability_index}: it has "
            f"{len(abilities)} activated {'ability' if len(abilities) == 1 else 'abilities'}"
        )
    ability = abilities[ability_index]
    if getattr(ability, "mana", None) is None:
        raise ValueError(
            f"{source.name}'s ability {ability_index} ({ability.text}) is not a mana ability "
            "(CR 605.1a), so it cannot be named as a mana source"
        )
    return ability


def mana_from(
    source: CardState,
    game: Game,
    ability_index: int,
) -> list[Color]:
    """What tapping this source for `ability_index` produces, asked of the
    engine's own `ManaAbility.resolve`.

    Not `parse_mana_abilities(source.card)[0].produces()` any more, which was
    wrong twice over: it read the **printed card** rather than the permanent's
    computed abilities (CR 613.5 - a Forest that has become a copy of Sol Ring
    taps for `{C}{C}`), and it always took the **first** ability, which is the
    plain one on every source in this pool that prints a CR 106.6-restricted
    second one.

    `resolve` is the same call `Game.activate_ability`'s own mana fast path
    makes, so every refusal here is the engine's own sentence rather than one
    invented in this module - "this ability adds any colour; pass the colour
    chosen" for a choice nobody made, "this ability's mana has no defined type
    right now" for CR 106.5. That matters: a choice must never be *guessed* on
    a player's behalf here, because guessing is exactly what naming the ability
    was supposed to take out of the payment.

    Both arguments are required, and that is the whole shape of this function:
    it answers for one named ability of one permanent in one game. There is no
    "read it off the printed card instead" limb, because there is no caller
    without a game - `pay_with`'s named-ability branch is the only one that
    gets here, and it has both.
    """
    return _resolve(source, game, mana_ability_at(game, source, ability_index).mana, None)


def _narrowings(source: CardState, game: Game) -> tuple[frozenset[str], frozenset[str], bool]:
    """The board narrowings `ManaAbility.effective`/`.resolve` accept that
    this side of the seam can answer honestly, in their positional order.

    Three of them are plain state on the `CardState` - its own counters,
    whether a card is imprinted on it, and CR 614.12a's chosen colour (passed
    separately, as a keyword) - the same three `machines.cost._mana_narrowings`
    reads for the automatic path. The fourth is the controller's land types
    (CR 205.3i), which Urza's Tower's "If you control an Urza's Mine and an
    Urza's Power-Plant, add {C}{C}{C} instead" reads: `Game.activate_ability`
    asks `Game._controlled_land_types` for them, so this does too. Left empty
    (as it once was), a Tower named as a mana source was resolved to `{C}` and
    `tap_for_mana` then refused it, because the board says it makes three.
    CR 106.7's "could produce" survey is `effective_production`'s business;
    `CostPayment.tap_for_mana` re-validates against the real board either way.
    """
    return (
        game._controlled_land_types(source.controller_or_owner),
        frozenset(source.counters.counts),
        game.card_imprinted_on(source.id) is not None,
    )


def effective_production(
    game: Game, source: CardState, mana: Any
) -> tuple[list[list[Color]], bool]:
    """`ManaAbility.effective` for this source on this board: which productions
    the ability offers right now, and whether it is on its "any colour" limb.

    The same four narrowings `Game.mana_color_options` and `Game.activate_
    ability`'s own mana fast path hand it - the land types this source's
    controller controls (CR 205.3i, which Tron's "instead" clause reads), its
    counters, whether a card is imprinted on it, CR 106.7's could-produce
    survey and CR 614.12a's chosen colour - so a row that says what a source
    makes and the mana an activation actually adds are one answer.
    """
    land_types, counters, imprinted = _narrowings(source, game)
    surveyed = (
        game._could_produce_survey(mana.could_produce, source.controller_or_owner)
        if mana.could_produce is not None
        else None
    )
    return mana.effective(
        land_types,
        counters,
        imprinted,
        could_produce_colors=surveyed,
        chosen_color=_chosen_color(source),
    )


def _resolve(
    source: CardState,
    game: Game,
    mana: Any,
    chosen: Color | list[Color] | None,
) -> list[Color]:
    """`ManaAbility.resolve` for `chosen`, with `_narrowings` filled in."""
    land_types, counters, imprinted = _narrowings(source, game)
    return mana.resolve(
        land_types,
        counters,
        chosen,
        imprinted,
        chosen_color=_chosen_color(source),
    )


def _adds_any_color(source: CardState, game: Game, mana: Any) -> bool:
    """Whether this ability is on its "any colour" limb right now - the limb
    whose `color` argument may be a *sequence*, one colour per mana."""
    land_types, counters, imprinted = _narrowings(source, game)
    _options, any_color = mana.effective(
        land_types,
        counters,
        imprinted,
        chosen_color=_chosen_color(source),
    )
    return bool(any_color)


def _chosen_color(source: CardState) -> Color | None:
    """CR 614.12a's "as this enters, choose a color", recorded on the permanent
    as it entered - what "the chosen color" points back at."""
    from magic_cards_model.models.card_state import ChoiceKind

    choice = source.choice
    if choice is None or choice.kind is not ChoiceKind.COLOR or not choice.value:
        return None
    return Color(choice.value)


def _sole_mana_ability(game: Game, source: CardState) -> Any | None:
    """This source's one mana ability, or `None` where it has none or several.

    CR 613.5: the permanent's *computed* abilities, the same index space
    `mana_ability_at` reads. "Several" is `None` on purpose - with more than
    one and no `ability_index` naming which, nothing here knows which the
    player meant, and guessing is the thing this module refuses to do.
    """
    try:
        abilities = list(game.activated_abilities(source))
    except Exception:  # the engine's own refusal to read this object
        return None
    found = [ability.mana for ability in abilities if getattr(ability, "mana", None) is not None]
    return found[0] if len(found) == 1 else None


def _choice_argument(
    source: CardState,
    game: Game,
    mana: Any,
    named: list[Color],
) -> Color | list[Color] | None:
    """What the colours a caller named mean as CR 605.1a's `color` argument.

    One colour, however many times it was written, is one choice: `["C", "C"]`
    for Eldrazi Temple's `{C}{C}` is that same single choice spelled out per
    mana. Two *different* colours are a real mixture, and `ManaAbility.resolve`
    has a limb for exactly that - "add two mana in any combination of colors"
    (Interdimensional Web Watch) takes a sequence, one colour per mana. It is
    handed over whole, and the engine refuses it in its own words for an
    ability that adds mana of any one colour instead.

    An ability that is not on its any-colour limb at all takes a single colour
    or nothing; a mixture named there is passed as no choice at all, which
    raises the engine's own "pass the colour chosen" rather than reaching a
    branch that would compare a list against one printed option.
    """
    if len(set(named)) == 1:
        return named[0]
    return named if _adds_any_color(source, game, mana) else None


def _expanded_to_the_abilitys_own_count(
    source: CardState,
    game: Game,
    named: list[Color],
) -> list[Color]:
    """One colour named for a source with exactly one mana ability, with that
    ability's own count filled in: Shang-Chi's `{T}: Add two mana of any one
    color.` taps for `{R}{R}`, not for the one `{R}` the caller wrote.

    A colour is a *choice* (CR 605.1a's `color` argument), not a count - the
    ability decides how much mana that choice buys. Without this, every source
    whose ability adds more than one mana of a chosen colour is unpayable
    through the `{card_id, colors}` shape, because `tap_for_mana` is handed one
    mana the ability cannot produce on its own and refuses the whole tap.

    Best-effort by design: anything this cannot answer confidently - several
    mana abilities and no index naming one, a colour the ability refuses, an
    ability that adds one mana - falls back to the colours as written, so
    every refusal that shape already gave is the refusal it still gives, in
    `tap_for_mana`'s own words.
    """
    if len(named) != 1:
        return named
    mana = _sole_mana_ability(game, source)
    if mana is None:
        return named
    try:
        produced = _resolve(source, game, mana, named[0])
    except Exception:  # the engine's own refusal - `tap_for_mana` will repeat it
        return named
    return produced if len(produced) > 1 and set(produced) == {named[0]} else named


def mana_colors(
    source: CardState,
    colors: list[str] | None,
    game: Game | None = None,
    ability_index: int | None = None,
) -> list[Color]:
    """The colours a hand-picked source is being tapped for.

    Without `colors`, whatever the named ability itself produces (`mana_from`).

    With `colors` named, those are read as the *choice* the player made, never
    as an invoice for a particular number of mana: the ability's own count
    decides how much a chosen colour buys (`_expanded_to_the_abilitys_own_
    count`), and two different colours are the mixture limb of `ManaAbility.
    resolve` rather than a collapsed nothing (`_choice_argument`). When an
    `ability_index` names one particular ability, the answer is checked against
    what that ability would actually make, so a colour the named ability cannot
    produce is refused here rather than silently satisfied by a different
    ability on the same card.
    """
    if not colors:
        if game is None or ability_index is None:  # unreachable: `pay_with` names both
            raise ValueError(f"{source.name}: no colours and no ability named")
        return mana_from(source, game, ability_index)
    try:
        named = [Color(color.strip().upper()) for color in colors]
    except ValueError as exc:
        raise ValueError(f"unknown mana color in {colors}: use W, U, B, R, G or C") from exc
    if game is None:
        return named
    if ability_index is None:
        return _expanded_to_the_abilitys_own_count(source, game, named)
    mana = mana_ability_at(game, source, ability_index).mana
    produced = _resolve(source, game, mana, _choice_argument(source, game, mana, named))
    if len(named) == 1 and set(produced) == {named[0]}:
        # The caller named a colour, and this is what that colour buys - one
        # mana or several. Only a caller that spelled the production out per
        # mana gets held to the count it wrote, below.
        return produced
    if sorted(produced) != sorted(named):
        made = "".join("{" + color.value + "}" for color in produced) or "no mana"
        asked = "".join("{" + color.value + "}" for color in named)
        raise ValueError(f"{source.name}'s ability {ability_index} adds {made}, not {asked}")
    return produced


def target_thing(game: Game, token: str) -> Any:
    """A target named as a player name, a card id, or the id of an ability on
    the stack (`Game.find_ability_by_id`): Consign to Memory's "target
    triggered ability" names the `AbilityOnStack` itself, and whether it may
    is the engine's CR 601.2c check, not this lookup's."""
    thing = player_or_card(token, {player.username for player in game.players})
    if not isinstance(thing, UUID):
        return thing  # the engine accepts a username string anywhere a player goes
    found = game.find_by_id(thing) or game.find_ability_by_id(thing)
    if found is None:
        raise ValueError(
            f"target {token!r} is neither a player name, a card id, nor an ability on the stack"
        )
    return found


def pay_with(
    payment: Any,
    sources: list[UUID | ManaSource],
    game: Game,
) -> None:
    """Tap the given sources into the payment, then leave the rest to `pay()`.

    The sources are the mana abilities this app may activate on the player's
    behalf - the caller is the player's agent here, not the rules. Three kinds
    of entry are told apart:

    - a `ManaSource` naming an ability (`entry.ability_index`) is the most
      specific of the three: the player has said *which* mana ability of that
      source to tap it for, in the index space `AbilityView.index` publishes.
      The production comes off that ability alone, and the CR 106.6 restriction
      handed to `tap_for_mana` is **that ability's own**, passed explicitly.

      Explicitly, not by inference, and that is a correctness point rather than
      a tidiness one. Left to itself `tap_for_mana` re-derives the restriction
      from what the mana turned out to be (`machines.cost.
      restriction_for_production`), which asks which of the source's `{T}`
      abilities could have produced exactly that - and deliberately breaks a
      tie towards **unrestricted**, because a caller that named only mana
      should not have its spending narrowed on a guess. It also asks
      `ManaAbility.effective()` with no board at all, so an ability whose
      production is board-defined (Sunken Citadel's "the chosen color") matches
      nothing and comes back unrestricted however restricted it really is. A
      caller that named the ability is not guessing, so it says which
      restriction the mana carries and none of that runs.

      A named ability that offers a choice of colour with no colour given is
      refused in the engine's own words rather than picked for the player -
      auto-picking would put the very inference this branch exists to bypass
      straight back into the payment.

    - a `ManaSource` naming a colour (`entry.colors`) is a player's explicit
      choice - Sunbaked Canyon should make {R} and not {W} - and is tapped for
      exactly that colour, taking precedence over anything the engine would
      have picked;
    - a bare card id, or a `ManaSource` with no colour, names *which* source
      to consider but leaves *what colour it makes* to the engine. This is
      the common case: no source named at all (every untapped permanent the
      payer controls, below) and an explicit `--sources <id>` naming a dual both land here,
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

    `ability_index=None` - absent, or explicitly null - is the second and third
    shapes exactly as they always were: the branch below is only entered by an
    entry that positively names an ability.
    """
    from magic_cards_model.machines.cost import spend_restriction

    unpinned: list[CardState] = []
    for entry in sources:
        if isinstance(entry, ManaSource) and entry.ability_index is not None:
            source = find_card(game, entry.card_id)
            ability = mana_ability_at(game, source, entry.ability_index)
            payment.tap_for_mana(
                source,
                mana_colors(source, entry.colors, game, entry.ability_index),
                restriction=spend_restriction(ability.effect, source),
            )
        elif isinstance(entry, ManaSource) and entry.colors:
            source = find_card(game, entry.card_id)
            payment.tap_for_mana(source, mana_colors(source, entry.colors, game))
        else:
            card_id = entry.card_id if isinstance(entry, ManaSource) else entry
            unpinned.append(find_card(game, card_id))

    if not sources and payment.shortfall and payment.payer is not None:
        # Naming no source leaves the choice to the engine's automatic
        # payment over every untapped permanent the payer controls - it taps
        # only what the cost needs (CR 601.2g, CR 118.3c), so a front end has
        # no reason to offer the lot and a log row no reason to name it
        # (battle 5, B5-05).
        unpinned = [
            card
            for card in game.battlefield
            if card.controller == payment.payer and not card.tapped
        ]
    if unpinned and payment.shortfall:
        try:
            payment.auto_tap(unpinned)
        except ValueError:
            if sources:
                raise
            # Unnamed, the automatic payment is only an attempt: when it finds
            # no plan, the payment's own refusal (`pay`) names the real reason,
            # such as a CR 106.6 restriction, rather than "no combination".


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


class AmbiguousCardName(ValueError):
    """A cast by name that more than one different card in the zone answers
    to - refused naming each, so the caller can cast one by `card_id`."""

    def __init__(self, name: str, matches: list[CardState], where: str) -> None:
        self.candidates = [
            {"card_id": str(state.id), "card_name": state.card.name} for state in matches
        ]
        named = "; ".join(f"{c['card_name']} ({c['card_id']})" for c in self.candidates)
        super().__init__(
            f"{name!r} names more than one different card in {where}: {named} - "
            "cast the one you mean by its card_id"
        )


def answers_to_card_name(state: CardState, name: str) -> bool:
    """Whether a card is one `name` can mean: its full printed name, or its
    front face's name.

    CR 712.8a gives a double-faced card outside the battlefield and stack
    "only the characteristics of its front face", its name included, so Ajani,
    Nacatl Pariah // Ajani, Nacatl Avenger in hand is named Ajani, Nacatl
    Pariah. The front face is also what a split or adventurer card's plain
    cast is (`CastRequest.spell_face` names any other half), so that face's
    name means the same cast. A single-faced card has only its one name."""
    card = state.card
    front = card.front_face
    return name == card.name or (front is not None and name == front.name)


def _by_name(cards: list[Any], name: str, where: str) -> CardState:
    matches = [
        state
        for state in cards
        if isinstance(state, CardState) and answers_to_card_name(state, name)
    ]
    if not matches:
        raise ValueError(f"no {name} in {where}")
    if len({state.card.name for state in matches}) > 1:
        raise AmbiguousCardName(name, matches, where)
    return matches[0]


def locate_castable(game: Game, req: CastRequest) -> CardState:
    """The card to cast: by id, or by name in `from_zone` - a card's full
    printed name or its front face's (`answers_to_card_name`). Copies of one
    card are the same answer and the first is cast; a name two different
    cards answer to raises `AmbiguousCardName`."""
    if req.card_id is not None:
        return find_card(game, req.card_id)
    if not req.card:
        raise ValueError("give either card_id or card (a name)")
    zone = Zone(req.from_zone)
    if zone in SHARED_ZONES:
        return _by_name(list(game.shared_zones[zone]), req.card, f"the {zone.value}")
    player = game.player(req.player)
    return _by_name(list(player.cards_in(zone)), req.card, f"{req.player}'s {zone.value}")


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

    A `Game` is fully serializable, so `model_copy(deep=True)` is a plain
    deep copy with nothing to swap out first. This used to null and restore
    `game.decisions.provider` around the copy, because that field was typed
    to hold arbitrary caller state that `copy.deepcopy` could not copy (a
    lock, an open connection, ...). T8 deleted the field from the engine:
    there is now exactly one way to answer a decision, and an unanswered one
    resolves through the engine's own `default_answer` - the same
    deterministic, non-blocking answer a scenario test gets, which is the
    right one for a probe too.

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
    probe = copy_game(game)
    if hasattr(probe, "game_uid"):
        probe.game_uid = uuid4()
    return probe


def copy_game(game: Game) -> Game:
    """A deep copy of `game` whose objects point at the copy, not at `game`.

    `model_copy(deep=True)` copies each `CardState`'s private `_board_source`
    as a detached duplicate of the original game rather than as the copy, and
    it does not run `Game.model_post_init`, the engine's own relink walk. A card bound that way
    answers `controller`/`power`/`toughness` off a board where nothing done on
    the copy ever happens: Threaten resolved on a command's working copy and
    the committed game went on reporting the old controller (CR 613.1b's
    layer 2). Re-running the relink walk rebinds every object to the copy.
    """
    copied = game.model_copy(deep=True)
    copied.model_post_init(None)
    return copied


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
    except AmbiguousCardName as exc:
        return Outcome(
            False,
            str(exc),
            {"type": "ambiguous_card_name", "candidates": exc.candidates},
        )
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


#: The four object-naming pieces a printed "As an additional cost to cast this
#: spell, ..." sentence can ask for (CR 118.8a), mapped from this request's own
#: field to the `Casting` attribute the engine reads them off at CR 601.2f.
#: `Casting._printed_additional_costs` pools them by `ComponentKind` and hands
#: each sentence as many as it needs, in the order the sentences print, so
#: nothing here has to know which sentence wants which object.
_PRINTED_ADDITIONAL_COST_FIELDS = ("sacrificing", "discarding", "exiling", "tapping")


def _announce_printed_additional_costs(game: Game, req: CastRequest, casting: Casting) -> None:
    """Put the caster's CR 601.2b announcement of a printed additional cost
    onto the `Casting`, before anything asks what the spell costs.

    An empty list is left alone rather than written as one: the engine builds
    the component asking for its own count either way and refuses to be paid
    until the cards are there (CR 118.3), which is what lets a Fling announced
    with no creature rewind cleanly instead of failing at announcement.
    """
    for name in _PRINTED_ADDITIONAL_COST_FIELDS:
        ids = getattr(req, name)
        if ids:
            setattr(casting, name, [find_card(game, card_id) for card_id in ids])


def _announce_optional_costs(game: Game, req: CastRequest, casting: Casting) -> _ModeCosts:
    """CR 601.2b's announcement of the optional costs this cast pays - "If the
    spell has alternative or additional costs that will be paid as it's being
    cast such as buyback or kicker costs ..., the player announces their
    intentions to pay any or all of those costs" - onto the `Casting`, before
    anything determines the total cost (CR 601.2f).

    One engine door per mechanic, each refusing in its own words: kicker and
    multikicker (CR 702.33a/c) through `pay_kicker`, replicate (CR 702.56a),
    offspring (CR 702.175a), blight (CR 701.68a), gift (CR 702.174a) and
    harmonize's "tapping up to one untapped creature you control"
    (CR 702.180a/b, `pay_harmonize`, which refuses unless the cast was
    announced with `keyword: Harmonize`). `times` is passed to the two that
    take a count and repeats the call for the rest, whose own second-payment
    refusal then speaks.

    Entwine (CR 702.42a) and escalate (CR 702.120a) are costs of the mode
    choice, so they are only read here and paid by `_announce_modes`.
    """
    mode_costs = _ModeCosts()
    for choice in req.optional_costs or []:
        keyword = choice.keyword.strip().lower()
        chosen = [find_card(game, card_id) for card_id in choice.choosing or []]
        if keyword in ("kicker", "multikicker"):
            casting.pay_kicker(choice.times, choosing=chosen)
        elif keyword == "replicate":
            casting.pay_replicate(choice.times, choosing=chosen)
        elif keyword == "offspring":
            for _ in range(choice.times):
                casting.pay_offspring()
        elif keyword == "blight":
            if len(chosen) != 1:
                raise ValueError(
                    "blight puts its counters on one creature you control (CR 701.68a) - "
                    "name exactly that creature's id in choosing"
                )
            for _ in range(choice.times):
                casting.pay_blight(chosen[0])
        elif keyword == "gift":
            if not choice.opponent:
                raise ValueError(
                    "a gift is promised to an opponent (CR 702.174a) - name them in opponent"
                )
            for _ in range(choice.times):
                casting.pay_gift(choice.opponent)
        elif keyword == "harmonize":
            _paid_once(choice, "harmonize taps up to one creature (CR 702.180a)")
            if len(chosen) > 1:
                raise ValueError(
                    "harmonize taps up to one untapped creature you control (CR 702.180a) - "
                    "name at most one id in choosing"
                )
            casting.pay_harmonize(chosen[0] if chosen else None)
        elif keyword == "entwine":
            _paid_once(choice, "entwine is paid once, for all the modes (CR 702.42a)")
            mode_costs.entwined = True
        elif keyword == "escalate":
            _paid_once(
                choice,
                "escalate is paid once for each mode chosen beyond the first "
                "(CR 702.120a) - choose the modes, not a count",
            )
            mode_costs.escalate = chosen
        else:
            raise ValueError(
                f"{choice.keyword} is not an optional cost this engine pays (CR 601.2b): "
                "name one of Kicker, Multikicker, Replicate, Offspring, Blight, Gift, "
                "Entwine, Escalate or Harmonize"
            )
    return mode_costs


@dataclass
class _ModeCosts:
    """The optional costs a cast announces that are paid on its mode choice."""

    entwined: bool = False
    escalate: list[CardState] | None = None
    """The objects an escalate cost names, when the request named escalate."""


def _paid_once(choice: OptionalCostChoice, why: str) -> None:
    if choice.times != 1:
        raise ValueError(f"{choice.keyword} cannot be paid {choice.times} times: {why}")


def _announce_modes(req: CastRequest, casting: Casting, mode_costs: _ModeCosts) -> None:
    """CR 601.2b's mode choice, with the two optional costs that ride on it.

    Entwine is `choose_modes(..., entwined=True)`: CR 702.42a "You may choose
    all modes of this spell instead of just the number specified. If you do,
    you pay an additional [cost]" - the engine opens every mode and charges
    the cost in the one call, so an entwined request with no modes named is
    still announced and refused with the engine's own mode count.

    Escalate is `pay_escalate`, once the modes are known: CR 702.120a "For
    each mode you choose beyond the first as you cast this spell, you pay an
    additional [cost]." That cost is owed by choosing the modes, not by
    naming the keyword, so it is charged whenever an escalate spell is
    announced with more than one mode; naming `Escalate` is how the objects a
    non-mana escalate cost takes (Collective Brutality's discarded card) are
    handed over.
    """
    if req.modes or mode_costs.entwined:
        casting.choose_modes(req.modes or [], x_value=req.x_value, entwined=mode_costs.entwined)
        chosen = set(casting.chosen_mode_numbers)
        for mode in unreadable_modes(casting.spell):
            if mode.number in chosen:
                # Refused before the cost is paid, so the card stays where it
                # was: the engine would resolve this mode as a `ModeNotice`.
                raise ValueError(unreadable_mode_notice(casting.spell, mode))
    elif req.x_value is not None:
        # CR 107.3a's X is announced as part of CR 601.2b, before targets
        # are chosen at CR 601.2c - `Casting.targeting`'s own docstring
        # says so outright: "X is announced before targets, so it is
        # always known here." `choose_modes` is the one call that sets
        # `Casting.x_value`, and a non-modal spell has no modes of its
        # own to give it, so it still has to be called, with an empty
        # list, purely to announce X this early. Skipping it whenever
        # `req.modes` was empty (as it always is for a non-modal spell)
        # left `x_value` at its unset default for every `choose_targets`
        # below - Postmortem Lunge's own "target creature card with mana
        # value X" read X as 0 no matter what the caller actually
        # announced, and refused every legal target above mana value 0.
        # `determine_cost` still receives the same `x_value` afterwards;
        # this only moves the *announcement* earlier, exactly where CR
        # 601.2b already puts it.
        casting.choose_modes([], x_value=req.x_value)
    modal = casting.modal
    owed = (
        modal is not None
        and modal.escalate_cost is not None
        and len(casting.chosen_mode_numbers) > 1
    )
    if owed or mode_costs.escalate is not None:
        casting.pay_escalate(choosing=mode_costs.escalate)


def _announce_the_face(req: CastRequest, casting: Casting) -> None:
    """CR 709.3's choice of which half is being cast: "A player chooses which
    half of a split card they are casting before putting it onto the stack."
    CR 715.3 puts the same choice on an adventurer card: "As a player plays an
    adventurer card, the player chooses whether they play the card normally or
    as an Adventure."

    Before the stack is where both rules put it, and `Casting`'s own proposal
    has already made CR 601.2a's move by the time any of `_drive_cast` runs -
    so the earliest this api can name the face is here, first, ahead of every
    step that reads it. All three do: `base_cost` (the chosen face's own
    `mana_cost`, CR 709.3a/709.4b/715.3b), `targeting` (the chosen face's own
    text, CR 709.3b/715.3b) and `determine_cost`.

    Two engine doors, because the engine has two: `cast_as_adventure` for the
    inset Adventure face (it enters CR 715.3's own branch and stamps
    `CardState.face_played`, which is what CR 715.3d's exile-on-resolution
    then reads), `cast_as_split_half` for a half of a split card. Both refuse
    in the engine's own words - a face the card does not print, and a face
    named on a card that has no halves to name - and neither refusal is
    invented here.

    This api used to refuse the Adventure branch itself, with a reason of its
    own (`adventure_face_refusal`), because `Casting.targeting` was scoped to
    `Layout.SPLIT` and announced an Adventure against the whole printing's
    profile: measured, a targeted Stomp was refused "too many targets: 1
    chosen where the spell takes at most 0" and a targetless one resolved
    dealing no damage. Engine commit fd28021 made the chosen face - not the
    layout - the discriminator on every casting path (CR 601.3e: alternative
    characteristics "replace the object's characteristics for this
    determination"), so the refusal now blocks something the engine performs
    correctly, and the door is simply opened.
    """
    if req.spell_face is None:
        return
    card = casting.spell.card
    inset = adventure_face(card) if card.layout is Layout.ADVENTURE else None
    if inset is not None and req.spell_face == inset.name:
        casting.cast_as_adventure()
        return
    casting.cast_as_split_half(req.spell_face)


def _drive_cast(game: Game, req: CastRequest, casting: Casting, *, permitted: bool) -> Outcome:
    """Announce, pay and cast an already-proposed `Casting` (CR 601.2b-h).

    Every return that is not the final `Outcome` at the bottom leaves the
    casting unsettled on purpose - `cast_card`'s `finally` rewinds it.
    """
    try:
        _announce_the_face(req, casting)
    except ValueError as exc:
        return refused(exc)

    try:
        _announce_printed_additional_costs(game, req, casting)
    except (KeyError, ValueError) as exc:
        return refused(exc)

    chosen = [find_card(game, card_id) for card_id in req.choosing] if req.choosing else None
    try:
        if req.alternative_cost is not None:
            # CR 118.9, announced at CR 601.2b - before targets are chosen at
            # CR 601.2c, which is why this runs ahead of `choose_targets`
            # below. `use_alternative_cost` is the engine's one front door
            # over all four origins (a printed pitch offer, a keyword, a
            # granted permission, another permanent's static ability), so
            # this needs no branch per origin.
            casting.use_alternative_cost(req.alternative_cost, choosing=chosen)
        elif req.keyword:
            # CR 601.2b: plot and foretell are announced this way - the
            # keyword names both the exile pile the card must be in and what
            # is paid instead of the mana cost (`Casting.use_keyword`).
            #
            # `choosing` goes through here too, and used not to. A keyword
            # cost is not always mana: evoke is "Evoke—Exile a white card from
            # your hand" on Solitude and "Exile a black card from your hand"
            # on Grief (CR 702.74a), and `Casting.use_keyword` has always
            # taken the objects that pays with. Dropping them here made every
            # such cast refuse itself with "exile needs 1 card(s) chosen, 0
            # given" no matter what the caller sent - and by keyword is
            # exactly how `legal-actions` and a hand card's `cast_options`
            # both tell a caller to announce it, so the one spelling every
            # seat is pointed at was the one spelling that could not work.
            casting.use_keyword(req.keyword, choosing=chosen)
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
        mode_costs = _announce_optional_costs(game, req, casting)
    except (KeyError, ValueError) as exc:
        return refused(exc)

    try:
        _announce_modes(req, casting, mode_costs)
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
        # CR 702.51b/702.66a/702.126b: convoke, delve and improvise are neither
        # additional nor alternative costs - they "apply only after the total
        # cost ... is determined" - so they are spliced in exactly here, between
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
    return Outcome(
        True,
        detail={
            "spell_id": str(state.id),
            "cost": str(payment.cost),
            "tapped": tapped_view(payment),
        },
    )


def apply_object_costs(game: Game, spell: CardState, payment: Any, req: CastRequest) -> None:
    """Offer convoke creatures, delve cards and improvise artifacts against an
    open payment.

    All three keywords pay part of an already-determined cost by spending
    objects rather than mana - tapping an untapped creature you control (CR
    702.51a), exiling a card from your graveyard (CR 702.66a), tapping an
    untapped artifact you control (CR 702.126a) - which is why they share
    `machines/objects_as_costs.py` in the engine and share this one hook here.
    The objects are spent when the payment is paid, and given back if it is
    rewound (CR 733.1), so a refusal after this point leaves no trace.
    """
    from magic_cards_model.machines.objects_as_costs import convoke, delve, improvise

    for choice in req.convoke or []:
        creature = find_card(game, choice.card_id)
        color = Color(choice.color.strip().upper()) if choice.color else None
        convoke(payment, spell, creature, color)
    if req.delve:
        delve(payment, spell, *[find_card(game, card_id) for card_id in req.delve])
    if req.improvise:
        improvise(payment, spell, *[find_card(game, card_id) for card_id in req.improvise])


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
    return Outcome(
        True,
        detail={
            "ability_id": str(on_stack.id),
            "cost": str(payment.cost),
            "tapped": tapped_view(payment),
        },
    )


# -- what the ledger records about an action ---------------------------------


def cast_origin_zone(game: Game, req: CastRequest) -> str:
    """The zone `req`'s card is cast from on `game` - CR 601.2a moves it "from
    where it is to the stack" - rather than `CastRequest.from_zone`, which
    defaults to `hand` and is only read to find a card named without an id
    (battle 3, B3-15: a card cast from exile under Ragavan's permission was
    logged `from_zone: hand`)."""
    try:
        return locate_castable(game, req).zone.value
    except (KeyError, ValueError):
        return req.from_zone


#: The exile piles `ExileAnnotation` marks without a turn. Imprint and hideaway
#: link a card to a permanent (`linked_permanent_id`) and nothing in the engine
#: reads when they landed, so the engine leaves `turn_exiled` unset for them -
#: while every other pile carries it (battle 4, B4-09).
_UNSTAMPED_PILES = frozenset({ExileMarker.IMPRINTED, ExileMarker.HIDEAWAY})


def stamp_exile_turns(before: Game, after: Game) -> None:
    """Give each imprinted or hidden-away card that `after` exiled - and
    `before` did not hold in exile as that same object (CR 400.7's
    `zone_change_stamp`) - the turn it was exiled on.

    CR 406.4 keeps face-down exiled cards "in separate piles based on when
    they were exiled and how they were exiled". A command commits all at once,
    so the turn it commits on is the turn its exile happened on. Only a pile
    the engine left unstamped is touched, so an engine that stamps these
    itself makes this a no-op."""
    if after.turn is None:
        return
    earlier = {
        card.id: card.zone_change_stamp
        for card in before.shared_zones.get(Zone.EXILE, [])
        if isinstance(card, CardState)
    }
    for card in after.shared_zones.get(Zone.EXILE, []):
        if not isinstance(card, CardState):
            continue
        annotation = card.exile_annotation
        if (
            annotation is None
            or annotation.turn_exiled is not None
            or annotation.marker not in _UNSTAMPED_PILES
            or earlier.get(card.id) == card.zone_change_stamp
        ):
            continue
        card.exile_annotation = annotation.model_copy(
            update={"turn_exiled": after.turn.turn_number}
        )


# -- equipping (CR 702.6) ---------------------------------------------------


def equip_target_requirements(activation: Any) -> list[Any]:
    """The targeting requirements of an equip activation, read by the engine's
    own targeting parser off the ability CR 702.6a expands equip into
    ("Attach this permanent to target creature you control.")."""
    from magic_cards_model.machines.targeting import profile_for_text

    profile = profile_for_text(activation.ability.effect)
    return list(profile.requirements) if profile.understood else []


def equip_permanent(game: Game, req: EquipRequest) -> Outcome:
    """CR 702.6a's equip ability, activated: pay its cost and put "attach this
    permanent to target creature you control" on the stack.

    `machines.keywords.equip_activation` is the engine's object for it - the
    one its own `tests/scenarios/test_equip.py` builds - and its `why_not` is
    CR 602.5's refusal, "Activate only as a sorcery" (CR 307.5) included. The
    target is checked at announcement (CR 602.2b applies CR 601.2c), against
    the requirements the engine's targeting parser reads off that same
    ability, before anything is paid; the engine rechecks it on resolution
    (CR 608.2b)."""
    from magic_cards_model.machines.keywords import equip_activation

    try:
        equipment = find_card(game, req.card_id)
        target = find_card(game, req.target)
        activation = equip_activation(game, equipment, target, req.player)
    except (KeyError, ValueError) as exc:
        return refused(exc)

    refusal = activation.why_not()
    if refusal is not None:
        return refused(refusal)
    for requirement in equip_target_requirements(activation):
        rejected = requirement.rejects(target, req.player, game, equipment.id)
        if rejected is not None:
            return refused(f"{rejected} ({activation.rule})")

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
    detail: dict[str, Any] = {
        "card_id": str(card.id),
        "cost": str(payment.cost),
        "tapped": tapped_view(payment),
    }
    if action.time_counters is not None:
        detail["time_counters"] = action.time_counters
    return Outcome(True, detail=detail)


def suspend_card(game: Game, req: SpecialActionRequest) -> Outcome:
    """CR 116.2f/702.62a: exile a card from hand with N time counters, paying
    its suspend cost. The upkeep countdown and the free cast once the last
    counter is removed are the engine's own triggered abilities now
    (`machines/keywords._suspend_abilities`), put on the stack the same way
    any other trigger is - not this function's job."""
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
    return Outcome(
        True,
        detail={
            "ability_id": str(on_stack.id),
            "cost": str(payment.cost),
            "tapped": tapped_view(payment),
        },
    )


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


def _activate_off_zone_ability(game: Game, card: CardState, req: ActivateRequest) -> Outcome:
    """The off-battlefield half of `activate_ability`, for a card anywhere
    other than the battlefield: `machines.keywords.off_zone_activation`, the
    CR 113.6j/CR 113.6m (a printed ability, arbitrary card text) and CR
    702.29a (cycling, the rule's own fixed text) shapes unified into one
    index space - the same one `machines.legal_actions._off_zone_actions`
    already reports through `Action.ability_index`
    (`ActionKind.ACTIVATE_OFF_ZONE`), so a caller never has to guess which of
    the two the engine actually reads.

    Puts the ability on the stack rather than resolving it immediately, the
    same as the battlefield path above and `cycle_card` below - resolved
    through the ordinary `resolve-top`/`pass-priority` flow.
    """
    from magic_cards_model.machines.keywords import off_zone_activation

    def resolve_states(ids: list[UUID] | None) -> list[CardState] | None:
        return [find_card(game, card_id) for card_id in ids] if ids else None

    if req.exiling:
        # `machines.keywords.off_zone_activation`/`off_battlefield_activation`
        # take no `exiling=` at all - CR 113.6j/113.6m's own examples in the
        # pool (Waker of Waves, Boseiju's Channel) never pair with an exile
        # cost, and the engine has built no shape for one there yet. Refused
        # here, in words, rather than passed through: `off_zone_activation`
        # would otherwise raise a bare `TypeError` on the unexpected keyword,
        # which is a crash a seat cannot read as a refusal.
        return refused(
            f"{card.name}'s ability functions off the battlefield (CR 113.6j/113.6m), "
            "and an off-battlefield activation has no exile-as-cost path yet - only "
            "sacrificing and discarding are supported there"
        )

    if req.x_value is not None:
        # `off_zone_activation` takes no `x_value=` at all - none of the
        # CR 113.6j/113.6m abilities in the pool carry an `{X}` in a cost
        # that also functions off the battlefield, so the engine has built
        # no shape for one there yet either. Refused in words, the same
        # reasoning as the `exiling` guard above, rather than a bare
        # `TypeError` on the unexpected keyword.
        return refused(
            f"{card.name}'s ability functions off the battlefield (CR 113.6j/113.6m), "
            "and an off-battlefield activation has no X-announcement path yet"
        )

    try:
        targets = (
            [target_thing(game, token) for token in req.targets]
            if req.targets is not None
            else None
        )
        activation = off_zone_activation(
            game,
            card,
            req.player,
            req.ability_index,
            targets=targets,
            sacrificing=resolve_states(req.sacrificing),
            discarding=resolve_states(req.discarding),
        )
    except ValueError as exc:
        return refused(exc)

    refusal = activation.why_not()
    if refusal is not None:
        return refused(refusal)

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
    return Outcome(
        True,
        detail={
            "ability_id": str(on_stack.id),
            "cost": str(payment.cost),
            "tapped": tapped_view(payment),
        },
    )


def activate_ability(game: Game, req: ActivateRequest) -> Outcome:
    """Activate an ability: engine refusal, then payment, then the stack.

    A basic land, a shockland, or a Ravnica dual has no printed activated
    ability of its own - the engine's `Game.activated_abilities` (and so
    `why_cannot_activate`/`begin_activating`/`activate_ability`) already
    synthesises one from the land's basic land type(s), so there is nothing
    special-cased here for it: it is just another ability at index 0.

    A card anywhere other than the battlefield is routed to
    `_activate_off_zone_ability` instead - CR 113.6's own default is that an
    ability functions only on the battlefield, so `Game.why_cannot_activate`
    refuses every other zone outright, unconditionally, even for an ability
    whose own printed text reads a different zone (CR 113.6j, CR 113.6m).
    Before this branch existed, a Waker of Waves or a Boseiju Channel the
    engine's own enumeration (`machines.legal_actions.legal_actions`,
    `ActionKind.ACTIVATE_OFF_ZONE`) reported as legal and affordable had no
    way to actually be taken through this endpoint - it was refused "in hand,
    not on the battlefield" regardless. That is why the reported seat reached
    for `cycle` instead: a different CR 702.29a ability with its own,
    narrower endpoint, the only one that worked.
    """
    permanent = find_card(game, req.card_id)

    try:
        color = Color(req.color.strip().upper()) if req.color else None
    except ValueError:
        return refused(f"unknown mana color {req.color!r}: use W, U, B, R, G or C")

    if permanent.zone is not Zone.BATTLEFIELD:
        return _activate_off_zone_ability(game, permanent, req)

    refusal = game.why_cannot_activate(permanent, req.ability_index, req.player)
    if refusal is not None:
        return refused(refusal)

    # CR 602.2b: X is announced as part of the same moment the activation
    # cost is fixed, so it has to be known before `begin_activating` builds
    # `TotalCost` - a missing one is refused here, in words, rather than
    # defaulting to `x_value=0` the way `begin_activating`'s own signature
    # would otherwise let it. `"{X}" in ability.cost` is the same check
    # `affordances._needs_x`/`AbilityView.needs_x` already report to a seat
    # ahead of time; a mistaken cast whose reader could not classify the
    # ability at all has no `cost` (a basic land's intrinsic ability) and so
    # never matches.
    ability = game.activated_abilities(permanent)[req.ability_index]
    if "{X}" in (getattr(ability, "cost", None) or "") and req.x_value is None:
        return refused(
            f"{permanent.name}'s ability needs X announced before its cost can be "
            "paid (CR 602.2b) - pass x_value"
        )

    def resolve_states(ids: list[UUID] | None) -> list[CardState] | None:
        return [find_card(game, card_id) for card_id in ids] if ids else None

    payment = None
    try:
        # CR 602.2b runs CR 601.2c before CR 601.2h: the named targets (a card,
        # a player, or an ability on the stack) are announced and checked by
        # `begin_activating` before anything is paid.
        targets = (
            [target_thing(game, token) for token in req.targets]
            if req.targets is not None
            else None
        )
        payment = game.begin_activating(
            permanent,
            req.ability_index,
            req.player,
            sacrificing=resolve_states(req.sacrificing),
            discarding=resolve_states(req.discarding),
            exiling=resolve_states(req.exiling),
            x_value=req.x_value or 0,
            targets=targets,
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
        ability_on_stack = game.activate_ability(
            permanent,
            req.ability_index,
            req.player,
            targets=targets,
            color=color,
            x_value=req.x_value or 0,
        )
    except ValueError as exc:
        if payment is not None:
            payment.rewind()
        return refused(exc)

    game.retain_priority(req.player)
    if ability_on_stack is None:
        return Outcome(
            True,
            detail={"mana_ability": True, "cost": str(payment.cost)},
        )
    return Outcome(
        True,
        detail={
            "ability_id": str(ability_on_stack.id),
            "cost": str(payment.cost),
            "tapped": tapped_view(payment),
        },
    )


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
        if _answers_to_land_name(state, req.card):
            return state
    raise ValueError(f"{req.player} has no land named {req.card} in hand")


def _answers_to_land_name(state: CardState, name: str) -> bool:
    """Whether a hand card is a land `name` can mean.

    A card with a land face (CR 712.12 - `Card.land_face`, which reads every
    face, where `is_land` reads only the front one, CR 712.8a) answers to its
    full name and to each of its faces' names: a modal double-faced card is
    played by its front face's name ("Witch Enchanter"), its land face's name
    ("Witch-Blessed Meadow") or both joined. A single-faced land has no faces
    and answers to its one name."""
    card = state.card
    if not card.is_land and card.land_face is None:
        return False
    return name in {card.name, *(face.name for face in card.card_faces or [])}


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


def list_abilities(game: Game, state: CardState) -> list[dict[str, Any]]:
    """The activated abilities the engine would let a caller index into.

    `Game.activated_abilities` already synthesises a basic land's (or a
    shockland's, or a Ravnica dual's) intrinsic mana ability at index 0 when
    the card prints no activated ability of its own (`intrinsic_land_ability`,
    CR 305.6), so the normal loop below covers it directly - the `activate`
    endpoint no longer needs a separate mana-ability index range for it.

    The printed-mana fallback loop (`_printed_mana_activations`) still runs for
    the one shape that is not covered by that: a card whose computed activated
    abilities are all non-mana, so `Game.activated_abilities` never reaches the
    intrinsic fallback, but which still carries a printed `{T}: Add ...` or a
    basic land type. Nothing in the pool is shaped like that today.

    A card anywhere other than the battlefield reads a different pool
    entirely: `machines.keywords.off_zone_abilities`, the same one
    `activate_ability`'s `_activate_off_zone_ability` branch and
    `machines.legal_actions._off_zone_actions` already index into. `Game.
    activated_abilities` is not itself zone-restricted - it reads whatever
    the card's printed or computed text says, on the battlefield or off -
    so reading it unconditionally here reported an off-zone ability's index
    in that ordinary, unfiltered battlefield list rather than the off-zone
    pool's own, renumbered-from-zero one. Boseiju, Who Endures prints
    `{T}: Add {G}` first and Channel second: a Boseiju on the battlefield
    correctly offers both at indices 0 and 1, but a Boseiju in *hand* has
    only Channel to offer at all, and the off-zone pool numbers it 0, not 1
    - the number this function used to report for a hand Boseiju before the
    off-zone endpoint existed to call it, and would have gone on quietly
    misleading a caller now that it does.
    """
    from .affordances import ability_hints

    if state.zone is not Zone.BATTLEFIELD:
        from magic_cards_model.machines.keywords import off_zone_abilities

        return [
            {
                "index": entry.index,
                "text": entry.ability.text,
                "mana": False,
                **ability_hints(entry.ability, (), state),
            }
            for entry in off_zone_abilities(state.card)
        ]

    try:
        abilities = game.activated_abilities(state)
    except Exception:
        return []

    listed = []
    for index, ability in enumerate(abilities):
        # The colours `Game.activate_ability` would actually accept for this
        # ability right now (CR 605.1a) - the same call
        # `machines.legal_actions` builds `Action.color_options` from, asked
        # once here and handed to `ability_hints` rather than letting that
        # module keep its own looser reading of the printed text. Only for a
        # mana ability: `mana_color_options` returns `()` for anything else,
        # and skipping the call for the rest keeps this off the board scan.
        color_options = (
            game.mana_color_options(state, index)
            if getattr(ability, "mana", None) is not None
            else ()
        )
        listed.append(
            {
                "index": index,
                "text": ability.text,
                "mana": is_mana_ability(ability),
                # What activating it actually asks the player for: its cost, a
                # loyalty symbol (CR 606.2), an X to announce, and whether it
                # targets at all. A UI without this cannot tell Karn's `+1`
                # (which targets) from his `-2` (which does not), and sending
                # an empty target list for the one that targets is read by the
                # engine as "I choose none" rather than "not chosen yet" - see
                # `affordances.py`'s module docstring.
                **ability_hints(ability, color_options, state),
            }
        )
    if any(entry["mana"] for entry in listed):
        return listed
    for offset, activation in enumerate(_printed_mana_activations(state)):
        # The one shape `mana_color_options` cannot answer: these abilities
        # are read off the printed card and are NOT in
        # `game.activated_abilities(state)` at all (see this function's own
        # docstring), so there is no index for the engine to look one up by.
        # The printed reading stays here, and only here.
        parsed = activation.mana
        if parsed is not None and parsed.any_color:
            # CR 106.1b's five colours; `Color.COLORLESS` is not one of them
            # and "any color" never offers it.
            printed: tuple[Color, ...] = (
                Color.WHITE,
                Color.BLUE,
                Color.BLACK,
                Color.RED,
                Color.GREEN,
            )
        elif parsed is not None and len(parsed.options) > 1:
            printed = tuple({color for option in parsed.options for color in option})
        else:
            printed = ()
        listed.append(
            {
                "index": len(abilities) + offset,
                "text": activation.text,
                "mana": True,
                **ability_hints(activation, printed, state),
            }
        )
    return listed


def _printed_mana_activations(state: CardState) -> list[Any]:
    """This card's own `{T}`-cost mana abilities, read off the printed card -
    `parse_mana_abilities`' answer, kept as whole `ActivatedAbility` objects
    rather than the bare `ManaAbility` that function returns.

    Same filter, same intrinsic-land fallback (CR 305.6), same up-face
    narrowing - `parse_mana_abilities`' own docstring is the reference - but
    the activation survives, and with it the two things a bare `ManaAbility`
    cannot answer: its printed `text`, and the `effect` a CR 106.6 "Spend this
    mana only ..." sentence is read out of. `list_abilities`' fallback branch
    used to unpack the `ManaAbility` and then ask it for a `.mana` attribute it
    has never had, so `mana_options` and `is_mana_ability` came out empty for
    every ability this branch has ever listed.
    """
    from magic_cards_model.machines import intrinsic_land_ability, parse_abilities
    from magic_cards_model.machines.abilities import ActivatedAbility
    from magic_cards_model.machines.cost import CostKind

    face = state.up_face_name
    printed = [
        ability
        for ability in parse_abilities(state.card)
        if isinstance(ability, ActivatedAbility)
        and ability.mana is not None
        and any(piece.kind is CostKind.TAP for piece in ability.costs)
        and (face is None or ability.face == face)
    ]
    if printed:
        return printed
    intrinsic = intrinsic_land_ability(state.card, face)
    return [intrinsic] if intrinsic is not None and intrinsic.mana is not None else []


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
    card.bind_to_game(game)  # what `Game.put_onto_battlefield` does (CR 613.5)
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
