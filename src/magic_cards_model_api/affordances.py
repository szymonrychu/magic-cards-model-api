"""What one action actually asks the player for, answered by the engine.

The browser UI used to guess. It showed a free-text "X value" box on every
spell, a free-text "modes" box on every spell, and it collected targets by
having the player click things on the board with no idea whether the spell
targeted at all, how many targets it wanted, or which of the things on the
board were legal. That is three different ways of asking a player to know the
card better than the program does, and one of them was actively wrong: an
ability that targets got `targets: []` sent for it, which the engine reads as
"I choose no targets" (CR 115.6's legal answer for "up to one") rather than
"the player has not chosen yet". Karn, the Great Creator's +1 went on the
stack targeting nothing every time, because the UI had no way to know it
wanted a target.

So this module answers, per action:

- does it target, how many, and which of the objects on the board are legal
  right now (CR 601.2c);
- is there an X to announce (CR 107.3a);
- is it modal, and what are the printed modes (CR 700.2);
- what does it cost, and is it a loyalty ability (CR 606.2).

Everything here is read from the engine's own parsers - `targeting_profile`,
`profile_for_text`, `profile_for_modes`, `modal_profile`, and each
`TargetRequirement.rejects` - never from a table of card names. Nothing here
mutates the game: `rejects` is a pure predicate, and the profiles are parsed
off printed text.

Two costs, split deliberately:

- `ability_hints`/`cast_hints` are pure text parsing and cheap enough to ride
  along in every state view, so the board can badge a targeting ability
  without a round trip.
- `candidates` walks every object on the board against every requirement, and
  is served from its own endpoint instead, called when the player actually
  picks a card up. At ~35 objects times a handful of requirements it is not
  expensive, but it is not worth paying on a 1.2s poll for the 30-odd cards
  the player is not currently holding.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from magic_cards_model.machines import AbilityOnStack
from magic_cards_model.machines.casting import Casting
from magic_cards_model.machines.modes import modal_profile, substitute_x
from magic_cards_model.machines.targeting import (
    TargetingProfile,
    profile_for_modes,
    profile_for_text,
    targeting_profile,
)
from magic_cards_model.models import CardState, Game, Player, Zone

#: Zones whose contents a spell or ability can realistically be asked to
#: target, and which every seat may see anyway (CR 400.2's public zones, plus
#: the stack itself for "target spell"). A hand or library card is not
#: enumerable as a candidate here - nothing in the pool targets one, and
#: guessing at hidden cards is exactly what `visibility.py` exists to stop.
CANDIDATE_ZONES = (Zone.BATTLEFIELD, Zone.GRAVEYARD, Zone.EXILE)


def _profile_view(profile: TargetingProfile) -> dict[str, Any]:
    """A targeting profile as the UI needs it.

    `understood=False` is reported rather than hidden: it means the parser
    could not read this card's targeting, so the UI must fall back to letting
    the player name targets freely instead of pretending the empty candidate
    list is the truth.
    """
    return {
        "targets_anything": profile.targets_anything,
        "understood": profile.understood,
        "minimum": profile.min_targets,
        "maximum": profile.max_targets,
        "requirements": [
            {
                "phrase": requirement.phrase,
                "count": requirement.count,
                "minimum": requirement.minimum,
                "unbounded": requirement.unbounded,
                "variable_count": requirement.variable_count,
            }
            for requirement in profile.requirements
        ],
        "division": (
            {"amount": profile.division.amount, "what": profile.division.what}
            if profile.division is not None
            else None
        ),
    }


def _no_targets() -> dict[str, Any]:
    return _profile_view(TargetingProfile())


def ability_hints(ability: Any) -> dict[str, Any]:
    """What activating this ability asks for, beyond the click.

    `ability` is one of `Game.activated_abilities`' entries - either a parsed
    `ActivatedAbility` or the intrinsic land ability the engine synthesises
    for a basic land (CR 305.6), which carries no `cost`/`loyalty` of its own.
    Both shapes are read with `getattr` rather than branched on, since the
    intrinsic one is deliberately not an `ActivatedAbility`.
    """
    cost = getattr(ability, "cost", None)
    loyalty = getattr(ability, "loyalty", None)
    text = getattr(ability, "text", "") or ""
    effect = getattr(ability, "effect", None) or text
    hints: dict[str, Any] = {
        "cost": cost,
        "needs_x": "{X}" in (cost or ""),
        "is_mana_ability": getattr(ability, "mana", None) is not None,
    }
    if loyalty is not None:
        # CR 107.7's `[+N]`/`[0]`/`[-N]`. `amount` is signed, so the sign is
        # the whole display: a UI showing "+1" and "-2" next to a loyalty
        # total is showing the player what the counters will do.
        hints["loyalty"] = {
            "amount": loyalty.amount,
            "label": f"+{loyalty.amount}" if loyalty.amount > 0 else str(loyalty.amount),
        }
    # A mana ability that offers a choice of colour needs one announced -
    # `Game.activate_ability`'s `color` argument, and `ActivateRequest.color`.
    # A Cavern of Souls or a Ravnica dual is unusable from a UI that never
    # asks, and a UI that asks on every land is asking five times too often,
    # so the options are reported here: `None` for an ability with no choice
    # to make, a list of colour letters for one that offers a fixed set, and
    # every colour for an "any colour" source (CR 106.1b).
    mana = getattr(ability, "mana", None)
    if mana is not None:
        if mana.any_color:
            hints["mana_options"] = ["W", "U", "B", "R", "G"]
        else:
            produced = {color.value for option in mana.options for color in option}
            hints["mana_options"] = sorted(produced) if len(mana.options) > 1 else None
    # An ability's targeting is read off its effect text, which is what
    # `engine_api.check_target` already asks `profile_for_text` for - the same
    # parse the engine itself runs at CR 602.2b announcement.
    hints["targeting"] = _profile_view(profile_for_text(effect))
    return hints


def cast_hints(state: CardState) -> dict[str, Any]:
    """What casting this card asks for: X, modes, and targets.

    Read off the printed card, so it is answerable for a card sitting in hand
    or in exile under a play permission, before any `Casting` exists.

    A modal card's targeting is deliberately reported as `understood=False`
    by `targeting_profile` - which targets it wants depends on which modes
    are chosen (CR 700.2f) - so `modes` here is what the UI must ask for
    first, and `candidates(..., modes=...)` is what answers the targeting
    question afterwards.
    """
    card = state.card
    modal = modal_profile(card)
    hints: dict[str, Any] = {
        "mana_cost": card.mana_cost,
        "needs_x": "{X}" in (card.mana_cost or ""),
        "targeting": _profile_view(targeting_profile(card)),
    }
    if modal is not None:
        hints["modes"] = {
            "instruction": modal.instruction,
            "minimum": modal.minimum,
            "maximum": modal.maximum,
            "repeatable": modal.repeatable,
            "understood": modal.understood,
            "entwine_cost": modal.entwine_cost,
            "escalate_cost": modal.escalate_cost,
            "options": [{"number": mode.number, "text": mode.text} for mode in modal.modes],
        }
    return hints


def _all_targetables(game: Game) -> list[Any]:
    """Every object a target could name, drawn from the public zones only."""
    things: list[Any] = list(game.players)
    for zone in CANDIDATE_ZONES:
        for state in game.shared_zones.get(zone, []):
            if isinstance(state, CardState):
                things.append(state)
    for player in game.players:
        for zone in CANDIDATE_ZONES:
            things.extend(player.cards_in(zone))
    things.extend(game.stack)
    return things


def _label(thing: Any) -> dict[str, Any]:
    if isinstance(thing, Player):
        return {"id": thing.username, "kind": "player", "name": thing.username, "zone": None}
    if isinstance(thing, AbilityOnStack):
        return {
            "id": str(thing.id),
            "kind": "ability",
            "name": f"ability from {thing.source_name}",
            "zone": "stack",
        }
    return {
        "id": str(thing.id),
        "kind": "card",
        "name": thing.name,
        "zone": thing.zone.value,
        "controller": thing.controller,
    }


def _profile_for(
    game: Game,
    state: CardState,
    caster: str,
    ability_index: int | None,
    modes: list[int] | None,
    x_value: int | None,
) -> TargetingProfile:
    """The profile that governs this particular announcement.

    Three shapes, in the order the engine itself would reach them:

    - an activated ability's own effect text (CR 602.2b), when `ability_index`
      names one;
    - a modal spell's chosen modes (CR 700.2f), when `modes` names them - the
      only way a modal card's targeting is answerable at all, which is why
      `targeting_profile` refuses on its own;
    - the whole card otherwise, with X substituted first when the caster has
      announced one, so "X target creatures" is read with a real count rather
      than left `variable_count` (CR 107.3a).

    The X branch is guarded on the card NOT being modal, and that guard is the
    whole reason this function is worth reading twice. It is the same defect
    engine commit 859e540 fixed inside `Casting`, which lived here too and
    outlived the engine's fix, because the two conditions that reach it are
    independent: `modes` empty and `x_value` set. The browser produces exactly
    that pair on its own. `ui/app.js` calls `refreshAffordance()` from
    `setXValue()` as well as from `toggleMode()`, and sends `modes` only when
    `action.modes.length` - so a player who types X before picking their modes
    asked this the un-modal way. For Kozilek's Command (`{X}{C}{C}`, four
    bullets) reading the whole text as one blob answered `understood=True`
    with FOUR requirements and a minimum of four, which is not a number that
    card can ever want: it chooses two modes and needs at most two targets.
    The UI believes that answer - `isPickingTargets()` reads it straight - so
    the board dimmed and demanded four targets, graveyard cards included, for
    a two-target spell.

    CR 700.2c is the rule both copies walked past: "If a spell or ability
    targets one or more targets only if a particular mode is chosen for it,
    its controller will need to choose those targets only if they chose that
    mode." A modal card with no modes chosen therefore falls through to
    `targeting_profile`, which answers `understood=False` - "ask me again once
    you have chosen" - and X changes nothing about that.
    """
    if ability_index is not None:
        ability = game.activated_abilities(state)[ability_index]
        effect = getattr(ability, "effect", None) or getattr(ability, "text", "")
        return profile_for_text(substitute_x(effect, x_value))
    modal = modal_profile(state.card)
    if modes and modal is not None:
        return profile_for_modes(state.card, modal.resolve(modes), x_value)
    if modal is None and x_value is not None and state.card.oracle_text:
        return profile_for_text(substitute_x(state.card.oracle_text, x_value))
    return targeting_profile(state.card)


def candidates(
    game: Game,
    state: CardState,
    caster: str,
    *,
    ability_index: int | None = None,
    modes: list[int] | None = None,
    x_value: int | None = None,
) -> dict[str, Any]:
    """Every object on the board this announcement could legally name.

    One entry per legal candidate, each carrying the indices of the
    requirements it can satisfy - so a UI showing Kozilek's Command's "target
    player" and "target creature" side by side can tell which slot a click is
    filling rather than lumping them into one undifferentiated list.

    `TargetRequirement.rejects` is the engine's own per-requirement predicate,
    the one `TargetingProfile._fill_slots` calls for each slot at CR 601.2c.
    Calling it here rather than re-deriving legality means a candidate list
    can never disagree with the announcement that follows it.

    A profile the parser could not read (`understood=False`) returns no
    candidates and says so: the honest answer is "the engine cannot enumerate
    this", which the UI turns into a free-entry box, not an empty list the
    player would read as "nothing is legal".
    """
    try:
        profile = _profile_for(game, state, caster, ability_index, modes, x_value)
    except (IndexError, ValueError) as exc:
        return {"error": str(exc), **_no_targets(), "candidates": []}

    view = _profile_view(profile)
    if not profile.understood or not profile.requirements:
        return {**view, "candidates": []}

    found: list[dict[str, Any]] = []
    for thing in _all_targetables(game):
        if thing is state and state.zone is not Zone.STACK:
            # The object doing the targeting is not yet on the stack, so it is
            # not a candidate for its own "target other" clauses; `rejects`
            # checks `excludes_source` against `source_id`, which is passed
            # below and covers the on-stack case properly.
            continue
        slots = [
            index
            for index, requirement in enumerate(profile.requirements)
            if requirement.rejects(thing, caster, game, state.id) is None
        ]
        if slots:
            found.append({**_label(thing), "requirements": slots})
    return {**view, "candidates": found}


def alternative_costs(game: Game, state: CardState, caster: str) -> list[dict[str, Any]]:
    """Every CR 118.9 alternative cost this card could be cast under.

    A real game lost this twice: a player wanted to pitch Force of Negation
    and had no way to reach the cost, because nothing in the state a seat
    reads ever said the card had one. `Casting.alternative_costs` is the
    engine's own discovery call, and it deliberately lists *unavailable*
    options too, carrying the reason - CR 118.9b makes "this card has no
    pitch cost" and "it has one and it is not your turn" different answers,
    and a UI shown only the legal ones cannot tell them apart.

    Building a `Casting` is *not* free: CR 601.2a moves the card to the stack
    as the proposal begins. This function only ever asks a question - it never
    completes a cast - so the whole thing runs under a `try/finally` that
    rewinds the proposal (CR 601.6, CR 733.1) on every path, answer or error
    alike. Without it, opening the alternative-cost row on a card would put it
    on the stack and leave it there.
    """
    try:
        casting = Casting(spell=state, caster=caster, origin_zone=state.zone, game=game)
    except ValueError:
        # Not castable from where it is right now (wrong zone, not this
        # player's card). No options rather than an error: the UI simply
        # shows no alternative-cost row. CR 601.3 refuses before CR 601.2a
        # moves anything, so there is nothing to rewind here.
        return []
    try:
        options = casting.alternative_costs
    except Exception:
        return []
    finally:
        if not casting.is_settled:
            casting.cancel()
    return [
        {
            "index": option.index,
            "source": option.source.value,
            "rule": option.rule,
            "source_name": option.source_name,
            "keyword": option.keyword,
            "description": option.description,
            "cost": str(option.cost) if option.cost is not None else None,
            "requires": option.requires,
            "available": option.available,
            "unavailable": option.unavailable,
        }
        for option in options
    ]


def object_costs(game: Game, state: CardState, player: str) -> dict[str, Any]:
    """Which objects could be spent on this card's cost, and for what.

    Convoke (CR 702.51a), delve (CR 702.66a) and crew (CR 702.122a) all pay
    part of a cost by spending objects rather than mana, and the engine
    shares one `ObjectOffer` across all three. The candidate list is that
    offer's own eligibility answer, so a UI cannot disagree with the payment
    that follows it.

    Only the keys that apply are present: a card with none of the three gets
    an empty dict, so the UI renders no row at all rather than three empty
    ones.
    """
    from magic_cards_model.machines.objects_as_costs import (
        convoke_offer,
        crew_number,
        crew_offer,
        delve_offer,
        has_convoke,
        has_delve,
        is_vehicle,
        power_of,
    )

    def eligible(offer: Any, cards: list[CardState]) -> list[dict[str, Any]]:
        found = []
        for card in cards:
            if offer.why_not(card, player, game) is None:
                entry = _label(card)
                if card.zone is Zone.BATTLEFIELD:
                    # Only crew cares about power, and only a battlefield
                    # permanent has a computed one - a delve candidate in the
                    # graveyard would report a meaningless 0.
                    entry["power"] = power_of(card, game)
                found.append(entry)
        return found

    battlefield = [
        card for card in game.shared_zones.get(Zone.BATTLEFIELD, []) if isinstance(card, CardState)
    ]
    out: dict[str, Any] = {}
    if has_convoke(state.card):
        out["convoke"] = {
            "rule": "CR 702.51a",
            "candidates": eligible(convoke_offer(), battlefield),
        }
    if has_delve(state.card):
        out["delve"] = {
            "rule": "CR 702.66a",
            "candidates": eligible(delve_offer(), list(game.player(player).graveyard)),
        }
    if is_vehicle(state.card):
        number = crew_number(state.card)
        out["crew"] = {
            "rule": "CR 702.122a",
            "number": number,
            "candidates": eligible(crew_offer(state), battlefield),
        }
    return out


def find_source(game: Game, card_id: UUID) -> CardState:
    state = game.find_by_id(card_id)
    if not isinstance(state, CardState):
        raise ValueError(f"no card with id {card_id} in this game")
    return state
