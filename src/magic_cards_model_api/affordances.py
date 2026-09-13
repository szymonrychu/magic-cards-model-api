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

What overlaps `machines.legal_actions.legal_actions` (Phase 1, adopted by
`seatkit.available_actions` in T11), and what was actually folded onto it -
each piece attempted, not argued about in advance:

- **`ability_hints`'s `mana_options`: folded, and it was the one that
  mattered.** It used to be read off the parsed ability - `mana.any_color`
  became a flat WUBRG list, `mana.options` became the colours the printed
  text can produce - which is a guess off printed text, not what
  `Game.activate_ability` would accept. `Game.mana_color_options` (CR 605.1a)
  is the authoritative call, and the one `Action.color_options` is built
  from; `engine_api.list_abilities` asks it and hands the answer here.
  The narrowing this used to miss is real: the board-derived CR 106.7 survey,
  the CR 614.12a chosen colour on the permanent, and Mox Amber's CR 106.5
  "among legendary creatures and planeswalkers you control". The one shape
  the engine cannot answer is `list_abilities`' `parse_mana_abilities`
  fallback loop, whose abilities are not in `Game.activated_abilities` at all
  and so have no index to look one up by; the printed reading survives there
  and nowhere else.
- **The `{X}` check: folded** into `_needs_x` below, the one place either of
  these writes `"{X}" in (cost or "")` now.
- **`cast_hints`'s `targeting`, `modes` and `needs_x`: attempted, and kept.**
  The engine computes all three off the same parsers this module calls
  (`legal_actions._cast_action` runs `modal_profile` and the same
  announcement profile), so the *computation* is genuinely duplicated - but
  `Action` is where it lands, and `Action` is deliberately narrower, in the
  engine repo, and not this repo's to widen. Building these from it loses,
  concretely: `TargetingProfile.understood` (`Action.target_slots` is `()`
  both for "the parser could not read this" and for "it targets nothing", and
  `ui/app.js`'s `isPickingTargets` branches on the difference to decide
  between a free-entry box and "nothing is legal"); `division`; each
  requirement's `count`/`unbounded`/`variable_count`/`mode`/`mode_text`; and
  every field of `modes` except the bare text - including each mode's
  `number`, which is what a client sends back, and `minimum`/`maximum`/
  `entwine_cost`/`escalate_cost`.
  `needs_x` diverges outright rather than merely narrowing: `Action.x_range`
  is `None` for a *modal* X spell, because a modal card's X is not
  enumerable before its modes are chosen (CR 700.2c), while this module
  answers "this card has an X to announce" - so folding it would take the X
  box away from Kozilek's Command.
  There is also a structural mismatch, not a taste one: `legal_actions` only
  emits a `CAST` action when casting is legal *right now*, and emits nothing
  but a pending decision's own answers while one stands - whereas
  `serialize._card_view` attaches these hints to every hand and exile card on
  every poll, whether or not it could be cast this instant. Folding would
  make the UI's X box, mode picker and target picker disappear during the
  opponent's turn.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from magic_cards_model.machines import AbilityOnStack
from magic_cards_model.machines.casting import Casting, face_being_cast
from magic_cards_model.machines.cost import spend_restriction
from magic_cards_model.machines.keywords import off_zone_abilities
from magic_cards_model.machines.modes import modal_profile, substitute_x
from magic_cards_model.machines.targeting import (
    TargetingProfile,
    profile_for_modes_with_owners,
    profile_for_text,
    targeting_profile,
)
from magic_cards_model.machines.triggers import activated_abilities_of
from magic_cards_model.models import Card, CardState, Game, Player, Zone

from .engine_api import dry_run_copy, equip_target_requirements

#: Zones whose contents a spell or ability can realistically be asked to
#: target, and which every seat may see anyway (CR 400.2's public zones, plus
#: the stack itself for "target spell"). A hand or library card is not
#: enumerable as a candidate here - nothing in the pool targets one, and
#: guessing at hidden cards is exactly what `visibility.py` exists to stop.
CANDIDATE_ZONES = (Zone.BATTLEFIELD, Zone.GRAVEYARD, Zone.EXILE)


def _profile_view(
    profile: TargetingProfile, mode_of: list[tuple[int | None, str | None]] | None = None
) -> dict[str, Any]:
    """A targeting profile as the UI needs it.

    `understood=False` is reported rather than hidden: it means the parser
    could not read this card's targeting, so the UI must fall back to letting
    the player name targets freely instead of pretending the empty candidate
    list is the truth.

    `mode_of` has one `(mode_number, mode_text)` per requirement, from
    `profile_for_modes_with_owners` (CR 700.2f: each mode may target on its
    own); a non-modal profile reports `None` for both.
    """
    mode_of = mode_of or [(None, None)] * len(profile.requirements)
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
                "mode": mode,
                "mode_text": mode_text,
            }
            for requirement, (mode, mode_text) in zip(profile.requirements, mode_of, strict=True)
        ],
        "division": (
            {"amount": profile.division.amount, "what": profile.division.what}
            if profile.division is not None
            else None
        ),
    }


def _no_targets() -> dict[str, Any]:
    return _profile_view(TargetingProfile())


def _needs_x(cost: str | None) -> bool:
    """Whether a cost string carries an unannounced `{X}` (CR 107.3a) - the
    one check `ability_hints` and `cast_hints` both used to spell out on
    their own (`legal_actions._mana_affordability`'s own `mana.x_count`
    check is the engine's version of the same question, asked once X has
    been parsed into a `ManaCost` rather than off the raw printed string)."""
    return "{X}" in (cost or "")


def ability_hints(
    ability: Any,
    color_options: tuple[Any, ...] | None = None,
    source: Any = None,
) -> dict[str, Any]:
    """What activating this ability asks for, beyond the click.

    `ability` is one of `Game.activated_abilities`' entries - either a parsed
    `ActivatedAbility` or the intrinsic land ability the engine synthesises
    for a basic land (CR 305.6), which carries no `cost`/`loyalty` of its own.
    Both shapes are read with `getattr` rather than branched on, since the
    intrinsic one is deliberately not an `ActivatedAbility`.

    `color_options` is `Game.mana_color_options(permanent, index)` - the exact
    call `machines.legal_actions` builds `Action.color_options` from, passed
    in by `engine_api.list_abilities` rather than re-derived here. See
    `mana_options` below for why that matters and what it replaced.

    `source` is the permanent (or card) the ability is read off, needed only
    for `restriction` below: CR 106.6's sentence can point back at a choice
    recorded on the object itself ("the chosen type", Cavern of Souls), and the
    engine's `spend_restriction` takes the object rather than guessing. Every
    one of `engine_api.list_abilities`' three branches passes it - a branch
    that did not would publish `restriction: None` for an ability that has one,
    which is the same lie as not publishing the field at all.
    """
    cost = getattr(ability, "cost", None)
    loyalty = getattr(ability, "loyalty", None)
    text = getattr(ability, "text", "") or ""
    effect = getattr(ability, "effect", None) or text
    hints: dict[str, Any] = {
        "cost": cost,
        "needs_x": _needs_x(cost),
        # CR 605.1a: "it's not a loyalty ability" - Ugin's `0: Add {C}{C}{C}`
        # adds mana and still uses the stack.
        "is_mana_ability": (getattr(ability, "mana", None) is not None and loyalty is None),
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
    # to make, and a list of colour letters for one that offers a real set.
    #
    # THE ENGINE'S OWN ANSWER, not this module's. This used to read the
    # parsed ability directly - `mana.any_color` became a flat WUBRG list,
    # and `mana.options` became the set of colours its printed text can
    # produce - which is a *guess off printed text* rather than what
    # `Game.activate_ability` would actually accept right now.
    # `Game.mana_color_options` (CR 605.1a) is the authoritative call: it
    # narrows the printed set by the board-derived CR 106.7 survey, the
    # CR 614.12a chosen colour on the permanent itself, and Mox Amber's own
    # CR 106.5 "among legendary creatures and planeswalkers you control"
    # reading - so an option it drops is one an activation would refuse.
    # That is exactly the divergence `Action.color_options` exists for, and
    # keeping a second, looser copy here is the duplicated-rule shape this
    # project has already paid for twice (`seatkit.py`'s module docstring).
    #
    # `()` from the engine means "no real choice at all" (one printed
    # colour, or the choice already narrowed to nothing, CR 106.5), which is
    # this field's own `None` - the shape `ui/mana.js` and `ui/actions.js`
    # already read. Sorted, as the multi-option branch always was.
    mana = getattr(ability, "mana", None)
    if mana is not None:
        hints["mana_options"] = sorted(color.value for color in color_options or ()) or None
    # An ability's targeting is read off its effect text, which is what
    # `engine_api.check_target` already asks `profile_for_text` for - the same
    # parse the engine itself runs at CR 602.2b announcement.
    hints["targeting"] = _profile_view(profile_for_text(effect))
    # CR 106.6: "Some spells or abilities that produce mana restrict how that
    # mana can be spent". Read off this ability's own effect text by the
    # engine's `spend_restriction`, which is the same call `machines/cost.py`
    # makes when the mana is actually produced - so the sentence a seat is
    # shown here and the sentence enforced on the mana are one reading, not
    # two.
    #
    # `understood` is published rather than filtered: `spend_restriction`
    # answers `None` for an ability with no such sentence and a *clause-less*
    # restriction for one it could not read, and those are not the same thing.
    # The second fails closed - the mana is made, is announced (CR 106.4a) and
    # pays for nothing - so a seat that is not told cannot tell why its cast
    # was refused.
    restriction = spend_restriction(effect, source)
    hints["restriction"] = (
        None
        if restriction is None
        else {"text": restriction.text, "understood": restriction.understood}
    )
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
    # CR 712.8a: a double-faced card off the battlefield and stack has only its
    # front face's characteristics, and Scryfall leaves the top-level cost of a
    # `transform`/`modal_dfc` printing blank - the face carries it.
    mana_cost = card.mana_cost or state.mana_cost
    hints: dict[str, Any] = {
        "mana_cost": mana_cost,
        "needs_x": _needs_x(mana_cost),
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


def all_targetables(game: Game) -> list[Any]:
    """Every object a target could name, drawn from the public zones only.

    Public because it is the one piece of this module worth hoisting: a whole
    `legal-actions` request enumerates candidates for every targeting action
    it lists, and every one of them walks the same board. `app.
    get_legal_actions` calls this once per request and hands the list down to
    each `target_candidates` call - one walk, not one per action, which is
    the same N-into-one shape T11 already applied a layer up at the HTTP
    boundary.
    """
    things: list[Any] = list(game.players)
    for zone in CANDIDATE_ZONES:
        for state in game.shared_zones.get(zone, []):
            if isinstance(state, CardState):
                things.append(state)
    # Only the graveyard is a real per-player zone (CR 400.1: "Each player has
    # their own library, hand, and graveyard. The other zones are shared by all
    # players"). The battlefield and exile are already walked once each, above,
    # via `game.shared_zones` - asking a `Player` for either used to return an
    # empty pile it minted on the spot as a side effect of the read, and now
    # raises `SharedZoneError` instead.
    for player in game.players:
        things.extend(player.cards_in(Zone.GRAVEYARD))
    things.extend(game.stack)
    return things


def _label(thing: Any) -> dict[str, Any]:
    """One targetable object as `{id, kind, name, zone}`, plus `controller`
    for a card.

    **The face-up rule lives here**, once, rather than in each of the four
    callers - `candidates`, `candidate_line`, `object_costs` and the
    cost-component list all name objects through this. A face-down object
    has no name to print (CR 708.2 for a permanent turned face down, and
    `visibility._redact_facedown_card` is the same rule already applied to
    the state view), and `all_targetables` walks the battlefield,
    graveyards and exile - every zone a face-down card can sit in - so this
    is reachable the moment a requirement accepts one. Whether any current
    `TargetRequirement` does is beside the point: the rule belongs in the
    one function that turns an object into a name.

    Hidden from *everyone*, its own controller included, because this
    function has no viewer to ask - and failing closed is the right way
    round for a name that must not be guessable. The id is untouched: it is
    what a target is named by, and knowing an object is there is public
    (CR 400.2).
    """
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
        "name": thing.name if getattr(thing, "face_up", True) else "a face-down card",
        "zone": thing.zone.value,
        "controller": thing.controller,
    }


class FaceNotRead(ValueError):
    """Raised by `_profile_for` when an announcement names a face whose own
    targeting this cannot derive - see `_face_profile`.

    A `ValueError` subclass so it lands in the same `except` every other
    undecidable announcement already lands in, and its own class so
    `target_candidates` can say *which* of the two happened: "the targeting
    phrase is unreadable" and "the phrase is readable but belongs to a face
    nobody may attribute to this announcement" are different sentences to a
    seat, and only the second one is fixed by asking again.
    """


def _face_profile(card: Card, face: str) -> TargetingProfile:
    """The profile for the one named face of a multi-face card, or
    `FaceNotRead` when the card prints no such face.

    The named face's own `oracle_text` through `profile_for_text`, never
    `targeting_profile(card)`, which resolves `Card.front_oracle_text` and so
    answers the *front* face's question for every face. Matching the engine
    call for call is the whole contract `target_candidates` keeps: this list
    can never disagree with the CR 601.2c check that follows it.

    **Layout-blind, because the engine is** - the discriminator is the face
    that was named, not the layout it is printed under. CR 709.3b ("while
    it's on the stack, a split card has only the characteristics of the half
    being cast") and CR 715.3b ("while on the stack as an Adventure, the
    spell has only its alternative characteristics") are the same sentence
    about two layouts, CR 601.3e being the general rule both instance:
    "these alternative characteristics replace the object's characteristics
    for this determination". This read was scoped to `Layout.SPLIT` while the
    engine's own was, so an Adventure row came back `understood: false`; the
    engine's `Casting.targeting`, `Game._announcement_profile` and
    `Game._resolution_profile` all now key on the chosen face through
    `machines.casting.face_being_cast`, which is what this calls, so the
    three engine reads and this decoration are one reading rather than three
    that agree and one that does not.

    A name the card does not print raises: the engine's own announcement would
    refuse it (`Casting.cast_as_split_half`, `cast_as_adventure`), so the
    honest answer is to name the face that could not be read rather than hand
    back the whole card's candidates for a cast that cannot happen.
    """
    chosen = face_being_cast(card, face)
    if chosen is None:
        raise FaceNotRead(
            f"the announced face {face!r} is not a face of {card.name}, whose faces are "
            f"{[one.name for one in card.card_faces or ()]}"
        )
    return profile_for_text(chosen.oracle_text or "")


def _profile_for(
    game: Game,
    state: CardState,
    caster: str,
    ability_index: int | None,
    modes: list[int] | None,
    x_value: int | None,
    face: str | None = None,
) -> tuple[TargetingProfile, list[tuple[int | None, str | None]] | None]:
    """The profile that governs this particular announcement.

    Four shapes, in the order the engine itself would reach them:

    - an activated ability's own effect text (CR 602.2b), when `ability_index`
      names one;
    - one named face of a multi-face card, when `face` names one - CR 709.3b's
      split half reads its own text and nothing else can be derived at all
      (`_face_profile`);
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
        return profile_for_text(substitute_x(effect, x_value)), None
    if face is not None:
        return _face_profile(state.card, face), None
    modal = modal_profile(state.card)
    if modes and modal is not None:
        chosen = modal.resolve(modes)
        profile, owners = profile_for_modes_with_owners(state.card, chosen, x_value)
        mode_of = [(chosen[owner].number, chosen[owner].text) for owner in owners]
        return profile, mode_of
    if modal is None and x_value is not None and state.card.oracle_text:
        return profile_for_text(substitute_x(state.card.oracle_text, x_value)), None
    return targeting_profile(state.card), None


def activation_profile(
    game: Game, state: CardState, ability_index: int, *, off_zone: bool = False
) -> TargetingProfile:
    """What activating `state`'s ability `ability_index` targets (CR 602.2b
    applies CR 601.2c's target announcement to an activation).

    `off_zone` names the index space: `off_zone_abilities(card)`, the one an
    `ACTIVATE_OFF_ZONE` row carries (CR 113.6j, CR 113.6m), rather than
    `Game.activated_abilities`. A printed off-zone line is read the way the
    engine's `off_battlefield_activation` reads it, whole; cycling, unearth and
    transmute spell their effect out with no target, so they target nothing.
    """
    if not off_zone:
        return game._announcement_profile(
            state, ability_index=ability_index, modes=None, x_value=None
        )
    entry = off_zone_abilities(state.card)[ability_index]
    if entry.family != "printed":
        return profile_for_text("")
    printed = [ability for ability in activated_abilities_of(state.card) if ability.zone]
    return profile_for_text(printed[entry.index].text)


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
        profile, mode_of = _profile_for(game, state, caster, ability_index, modes, x_value)
    except (IndexError, ValueError) as exc:
        return {"error": str(exc), **_no_targets(), "candidates": []}

    view = _profile_view(profile, mode_of)
    if not profile.understood or not profile.requirements:
        return {**view, "candidates": []}

    found: list[dict[str, Any]] = []
    for thing in all_targetables(game):
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


#: How many candidates one target slot lists before it starts saying "and N
#: more" instead of naming them. Measured: Lightning Bolt's "any target" on a
#: 28-permanent board has 14 legal candidates, so 30 fits that and every other
#: normal targeted spell whole - only a "target creature" on a board wider
#: than any real one reaches it. The cap exists so a pathological board cannot
#: make one answer unreadable, not to trim ordinary ones; a slot that does hit
#: it keeps its true `count` and says what to call (`MORE_CANDIDATES_WITH`),
#: because a seat weighing a sweeper needs the number even when it does not
#: need the names.
CANDIDATE_SLOT_CAP = 30

#: What a truncated slot points at. There is no MCP tool that enumerates
#: candidates on its own - deliberately, since asking one would mean the seat
#: already knew which action it was about to take, which is the question it is
#: trying to answer - so the pointer is at the board itself: every candidate
#: is an object in a public zone (`CANDIDATE_ZONES`) or a player, and all of
#: those are in `battlefield_state`.
MORE_CANDIDATES_WITH = (
    "battlefield_state - more candidates are legal than are listed here; `count` is the "
    "true total, and every candidate is a permanent, a graveyard/exile card, a spell on "
    "the stack, or a player, all of which that tool shows in full"
)

#: What is said instead of a candidate list when the parser could not read
#: the targeting phrase at all. NOT an empty list, which would read as
#: "nothing on this board is legal" - a different and false statement. Same
#: contract `candidates` has kept since `ui/app.js` started branching on
#: `understood` to choose between a free-entry box and a picker.
UNREADABLE_TARGETING = (
    "the engine could not parse this targeting phrase, so it will not guess at the "
    "candidates - this is NOT 'nothing is legal'. Read the printed text and name "
    "targets yourself; the engine still checks them (CR 601.2c)"
)

#: What is said instead of a candidate list when the announcement names a
#: face this cannot derive a profile for (`_face_profile`/`FaceNotRead`).
#: Deliberately a different sentence from `UNREADABLE_TARGETING`: the phrase
#: is perfectly readable here, it just belongs to a face nobody may attribute
#: to this announcement, and telling a seat the parser failed would send it
#: looking for a defect that is not there. Same contract either way -
#: `understood: false`, no slots, a note - so every reader branching on
#: `understood` is unaffected.
UNREADABLE_FACE_TARGETING = (
    "this action announces a face this printing does not carry, so the candidates for it "
    "cannot be determined and will not be guessed at - this is NOT 'nothing is legal'. Take "
    "the face string from the row's own `spell_face`, unmodified, or name targets yourself; "
    "the engine still checks them (CR 601.2c)"
)


def candidate_line(thing: Any) -> str:
    """One legal candidate as a single string.

    A string rather than the `_label` object `candidates` returns, and the
    difference is not cosmetic: an action answer is serialised with
    `json.dumps(indent=1)`, which puts every key of every dict on a line of
    its own. The same fourteen candidates measured 2,384 bytes as objects and
    972 as strings - and the object's fields were `id`, `kind`, `name`,
    `zone`, `controller`, every one of which is in the string below.

    The id is the whole uuid, never a prefix: it is exactly what `cast`'s
    `targets` parameter takes back, and a prefix would have to be resolved
    against a board the answer carrying this deliberately does not include.
    A player is named the way `targets` names one - by username - and says
    `(player)` so it is never mistaken for a card whose id went missing.
    """
    if isinstance(thing, Player):
        return f"{thing.username} (player)"
    label = _label(thing)
    controller = label.get("controller") or getattr(thing, "controller", None) or "?"
    return f"{label['id']} {label['name']} ({label['zone']}, {controller})"


def target_candidates(
    game: Game,
    state: CardState,
    caster: str,
    *,
    ability_index: int | None = None,
    face: str | None = None,
    targetables: list[Any] | None = None,
    off_zone: bool = False,
) -> dict[str, Any]:
    """Every object this announcement could legally name, per requirement
    slot, as strings - `candidates` narrowed to what a *seat* needs and
    widened with the counts a seat needs to act on.

    Same engine calls as `candidates`, and deliberately so: `TargetRequirement
    .rejects` is the predicate `TargetingProfile._fill_slots` itself runs at
    CR 601.2c, so this list can never disagree with the announcement that
    follows it. The differences from `candidates` are all about who reads it:

    - **per slot, not per candidate.** `candidates` answers "which slots can
      this object fill", which is what a UI resolving a click needs;
      a seat is filling one slot at a time and needs "what may go here", so
      the inversion happens once, here, rather than in every reader.
    - **strings** (`candidate_line`), for the byte reason spelled out there.
    - **capped** at `CANDIDATE_SLOT_CAP` per slot, with the true `count` kept
      whatever the cap did to the list.
    - **no modes and no X.** CR 700.2c: a modal card's targeting is
      unanswerable before its modes are chosen, and `targeting_profile`
      already refuses on its own - which arrives here as `understood: False`
      and a sentence saying so, never an empty list. This is asked before the
      seat has announced anything, so there is nothing to substitute; the
      browser's `/affordances` endpoint is where a half-announced spell is
      re-asked with the modes and the X the player has picked so far.
    - **`face` IS taken**, unlike the two above, because it is not a choice
      still to be made: `Action.spell_face` is part of the row being
      described, so a row for CR 709.3's second half must be enumerated
      against that half's own text or not at all (`_face_profile`). A
      whole-printing candidate list under a face's name is the one answer
      this must never give - `seatkit.TARGETS_NOTE` tells a seat to hand a
      candidate straight back, so a wrong list costs it the action.
    """
    try:
        if off_zone:
            assert ability_index is not None
            profile = activation_profile(game, state, ability_index, off_zone=True)
        else:
            profile, _ = _profile_for(game, state, caster, ability_index, None, None, face=face)
    except FaceNotRead as exc:
        return {"understood": False, "note": f"{UNREADABLE_FACE_TARGETING} ({exc})"}
    except (IndexError, ValueError) as exc:
        return {"understood": False, "note": f"{UNREADABLE_TARGETING} ({exc})"}
    if not profile.understood:
        return {"understood": False, "note": UNREADABLE_TARGETING}
    if not profile.requirements:
        return {"understood": True, "slots": []}

    return _candidate_block(game, state, caster, list(profile.requirements), targetables)


def _candidate_block(
    game: Game,
    state: CardState,
    caster: str,
    requirements: list[Any],
    targetables: list[Any] | None,
) -> dict[str, Any]:
    """Per requirement slot, every object `TargetRequirement.rejects` lets
    `state`'s announcement name - `target_candidates`' answer once its
    requirements are known."""
    listed: list[list[str]] = [[] for _ in requirements]
    counts = [0] * len(requirements)
    for thing in all_targetables(game) if targetables is None else targetables:
        if thing is state and state.zone is not Zone.STACK:
            # Same exclusion `candidates` makes, for the same reason: the
            # object doing the targeting is not on the stack yet, so it is
            # not a candidate for its own "target other" clauses.
            continue
        line: str | None = None
        for index, requirement in enumerate(requirements):
            if requirement.rejects(thing, caster, game, state.id) is not None:
                continue
            counts[index] += 1
            if len(listed[index]) < CANDIDATE_SLOT_CAP:
                if line is None:
                    line = candidate_line(thing)
                listed[index].append(line)

    slots: list[dict[str, Any]] = []
    truncated_anywhere = False
    for index, lines in enumerate(listed):
        slot: dict[str, Any] = {"slot": index, "count": counts[index], "candidates": lines}
        dropped = counts[index] - len(lines)
        if dropped:
            slot["truncated"] = dropped
            truncated_anywhere = True
        slots.append(slot)
    block: dict[str, Any] = {"understood": True, "slots": slots}
    if truncated_anywhere:
        block["more_with"] = MORE_CANDIDATES_WITH
    return block


def equip_target_candidates(
    game: Game, equipment: CardState, caster: str, *, targetables: list[Any] | None = None
) -> dict[str, Any]:
    """What an Equipment's equip ability may target (CR 702.6a: "target
    creature you control"), per slot - the requirements the engine's targeting
    parser reads off the ability `equip_activation` builds, never the
    Equipment's own printed text, which carries no equip target at all."""
    from magic_cards_model.machines.keywords import equip_activation

    try:
        activation = equip_activation(game, equipment, equipment, caster)
    except ValueError as exc:
        return {"understood": False, "note": f"{UNREADABLE_TARGETING} ({exc})"}
    requirements = equip_target_requirements(activation)
    if not requirements:
        return {"understood": False, "note": UNREADABLE_TARGETING}
    return _candidate_block(game, equipment, caster, requirements, targetables)


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
    as the proposal begins, and `Casting.cancel()`'s own rewind re-appends the
    card to the end of its zone rather than reinserting it where it was
    (`engine_api.rewind_unfinished`'s docstring has the exact defect) - this
    function only ever asks a question, so it is run against
    `engine_api.dry_run_copy(game)` instead of `game` itself, and never
    assigns the real game to `casting` at all. `state` is looked up again by
    id in the copy, since a deep copy shares no `CardState` with the
    original.
    """
    probe = dry_run_copy(game)
    probe_state = probe.find_by_id(state.id)
    if probe_state is None:
        return []
    try:
        casting = Casting(
            spell=probe_state, caster=caster, origin_zone=probe_state.zone, game=probe
        )
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
            "candidates": _alternative_cost_candidates(probe, option, caster),
            "available": option.available,
            "unavailable": option.unavailable,
        }
        for option in options
    ]


def _alternative_cost_candidates(game: Game, option: Any, caster: str) -> list[dict[str, Any]]:
    """Which of `caster`'s cards could actually be named in `choosing` for
    this option, per component of its cost, in the engine's own words.

    `requires` on its own says *how many* and nothing about *which*, and the
    difference is a whole round trip: Solitude's evoke cost is "Exile a white
    card from your hand", so a hand of six with two white cards has two legal
    answers and four refusals, and neither the web UI's pitch tray nor an
    agent seat could tell them apart from `requires: 1` and a sentence of
    prose. `object_costs` has published `candidates` for convoke, delve and
    crew since those existed, for exactly this reason; this is the same answer
    for the same question, on the other half of the cost model.

    Legality is asked of the engine, never re-derived here: each candidate is
    offered to a copy of the component itself and kept only if
    `CostComponent.why_not` has nothing to say about it - the same call the
    real payment will make, so a card listed here cannot be one the cast then
    refuses.
    """
    from magic_cards_model.machines.cost import ComponentKind

    if option.cost is None:
        return []
    found: list[dict[str, Any]] = []
    seen: set[UUID] = set()
    for component in option.cost.components:
        if not component.required:
            continue
        for card in _cards_in_reach(game, component, caster):
            if card.id in seen:
                continue
            probe = component.model_copy(
                update={"cards": [card], "required": 1, "payer": component.payer or caster}
            )
            if probe.why_not(game) is None:
                seen.add(card.id)
                found.append({**_label(card), "pays": ComponentKind(component.kind).value})
    return found


def _cards_in_reach(game: Game, component: Any, caster: str) -> list[CardState]:
    """The cards a cost component of this kind could possibly name, before
    legality is asked - the pool `_alternative_cost_candidates` filters.

    The zone follows the kind, the way the engine's own `why_not` checks do:
    tapping and sacrificing look at the battlefield (CR 701.21a's "you can
    only sacrifice something you control" narrows it to this player's own),
    discarding is hand-only by definition (CR 701.9a), and exiling reads the
    component's own `zone`, which is why Force of Negation exiles from hand
    and Abhorrent Oculus from a graveyard.
    """
    from magic_cards_model.machines.cost import ComponentKind

    kind = ComponentKind(component.kind)
    if kind in (ComponentKind.TAP, ComponentKind.UNTAP, ComponentKind.SACRIFICE):
        return [
            card
            for card in game.shared_zones.get(Zone.BATTLEFIELD, [])
            if isinstance(card, CardState) and card.controller == caster
        ]
    if kind is ComponentKind.DISCARD:
        return list(game.player(caster).hand)
    if kind is ComponentKind.EXILE:
        zone = component.zone
        if zone is Zone.BATTLEFIELD:
            return [
                card
                for card in game.shared_zones.get(Zone.BATTLEFIELD, [])
                if isinstance(card, CardState) and card.controller == caster
            ]
        return list(game.player(caster).zones.get(zone, []))
    return []


def object_costs(game: Game, state: CardState, player: str) -> dict[str, Any]:
    """Which objects could be spent on this card's cost, and for what.

    Convoke (CR 702.51a), delve (CR 702.66a), improvise (CR 702.126a) and
    crew (CR 702.122a) all pay part of a cost by spending objects rather than
    mana, and the engine shares one `ObjectOffer` across all four. The candidate list is that
    offer's own eligibility answer, so a UI cannot disagree with the payment
    that follows it.

    Only the keys that apply are present: a card with none of the three gets
    an empty dict, so the UI renders no row at all rather than four empty
    ones.
    """
    from magic_cards_model.machines.objects_as_costs import (
        convoke_offer,
        crew_number,
        crew_offer,
        delve_offer,
        has_convoke,
        has_delve,
        has_improvise,
        improvise_offer,
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
    if has_improvise(state.card):
        out["improvise"] = {
            "rule": "CR 702.126a",
            "candidates": eligible(improvise_offer(), battlefield),
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
