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
  these writes `"{X}" in (cost or "")` now. A CAST asks the wider question
  CR 107.3a actually poses - an X in a printed additional cost counts too -
  and `_cast_needs_x` puts the engine's own `cost_announces_x` over its own
  `additional_cost_phrases` on top of it, which is the identical reading
  `legal_actions._cast_action` makes for `Action.x_range`.
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

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from magic_cards_model.machines import AbilityOnStack
from magic_cards_model.machines.casting import (
    Casting,
    castable_faces,
    optional_additional_costs,
)
from magic_cards_model.machines.cost import (
    additional_cost_phrases,
    cost_announces_x,
    spend_restriction,
)
from magic_cards_model.machines.modes import modal_profile
from magic_cards_model.machines.targeting import (
    TargetingProfile,
    profile_for_modes_with_owners,
    profile_for_text,
    targeting_profile,
)
from magic_cards_model.models import CardState, Game, Player, Zone
from magic_cards_model.models.action import TargetSlot

from .alternative_cost_names import canonical_value
from .board_scope import STALE_BOARD, on_one_board, one_board
from .engine_api import dry_run_copy, unreadable_mode_notice, unreadable_modes

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


def _cast_needs_x(card: Any, mana_cost: str | None) -> bool:
    """Whether CASTING this card announces an X - the whole of CR 107.3a,
    not just the mana cost's half of it.

    CR 107.3a counts "a mana cost, alternative cost, additional cost, and/or
    activation cost with an {X}, [-X], or X in it", and the engine reads it
    exactly that way when it builds a row: `machines.legal_actions.
    _cast_action` sets `Action.x_range` from `"{X}" in mana_cost` **or** a
    lone X in any of `machines.cost.additional_cost_phrases`, citing this
    rule and naming Toxic Deluge's "pay X life" as the case.

    This published the mana-cost half alone, so Toxic Deluge's `needs_x` was
    False: no X box in the browser, no `x_value` in the request, and
    `machines.casting.determine_cost`'s "X is 0 if nobody ever said
    otherwise" made the wrath -0/-0 with 0 life paid. Every reader of
    `needs_x` was wrong at once - `actions.needsChoices` (fire the cast on
    the click), `actions.needsX` (`readyToFire` with no X entered),
    `render/ribbon.renderXInput` (no box), and every non-browser seat reading
    `CastHintsView.needs_x` - which is why the fix is here, at the one
    publisher, rather than in any of them.

    `cost_announces_x` is the engine's own predicate for "this printed cost
    has an X in it", and `additional_cost_phrases` its own enumeration of CR
    118.8a's sentences: no regex of this module's own, so a card that prints
    the additional cost differently arrives here answered for free.
    """
    return _needs_x(mana_cost) or any(
        cost_announces_x(phrase) for phrase in additional_cost_phrases(card)
    )


def _spell_faces(card: Any) -> list[dict[str, Any]] | None:
    """The faces a cast of this card may name (CR 709.3, CR 715.3), or None
    when there is no choice to make.

    `machines.casting.castable_faces` is the engine's own enumeration - a
    split card's two halves, an adventurer card's normal cast plus its
    Adventure, and `(None,)` for everything else - and it is the same set
    `machines.legal_actions._base_cost_options` emits one row per, so an
    entry here and a row there can never disagree about which faces exist.
    Each entry's `spell_face` is the value `CastRequest.spell_face` takes
    back verbatim, `None` meaning "no face named" (CR 715.3's normal cast).

    Published on the card view, beside `needs_x` and `modes`, because the
    hand menu has to offer the halves at the moment the player picks the card
    up - before any `Casting`, affordance or legal-actions row exists. Absent
    rather than a one-entry list for a single-faced card: there is nothing to
    choose, and the menu's plain Cast item is the whole of it.
    """
    faces = castable_faces(card)
    if len(faces) < 2:
        return None
    printed = {face.name: face for face in card.card_faces or []}
    # The faceless entry (CR 715.3's "play the card normally") is the
    # printing's own FRONT half - `Card.name`/`type_line` are the combined
    # `Bonecrusher Giant // Stomp` and `Creature - Giant // Instant`, which
    # name no face at all and are not what a menu item should read.
    front = (card.card_faces or [None])[0]
    views: list[dict[str, Any]] = []
    for name in faces:
        face = printed.get(name) if name is not None else front
        mana_cost = (face.mana_cost if face is not None else None) or card.mana_cost
        views.append(
            {
                "spell_face": name,
                "name": face.name if face is not None else card.name,
                "mana_cost": mana_cost,
                "type_line": face.type_line if face is not None else card.type_line,
                # CR 709.3a/715.3a: only the chosen half is evaluated, so
                # only its own cost announces an X. The named halves read
                # their own cost; the faceless entry gets the whole
                # printing's reading, additional-cost sentences included.
                "needs_x": (
                    _needs_x(mana_cost) if name is not None else _cast_needs_x(card, mana_cost)
                ),
            }
        )
    return views


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
        "needs_x": _cast_needs_x(card, mana_cost),
        "targeting": _profile_view(targeting_profile(card)),
    }
    spell_faces = _spell_faces(card)
    if spell_faces is not None:
        hints["spell_faces"] = spell_faces
    printed = optional_additional_costs(card)
    if printed:
        # CR 601.2b's optional additional costs, read off the printing the same
        # way `needs_x` and `modes` are - which is what lets the browser offer
        # the choice at the moment the player picks the card up, before any
        # `Casting`, affordance or legal-actions row exists. The reported
        # defect was exactly that gap: the option was on the row and in the
        # request model, and the one surface a human actually clicks never
        # read either, so a kicker card fired at its unkicked price the
        # instant it became legal.
        #
        # `machines/casting.optional_additional_costs` is the engine's own
        # single enumeration (`Action.additional_cost_options` is the same
        # answer on a row), so a mechanic it learns arrives here for free.
        # `understood: false` is carried through rather than dropped - a
        # printed option this engine cannot charge is shown, and said to be
        # uncharged, instead of being silently absent.
        hints["additional_costs"] = [
            {
                # The spelling `CastRequest.optional_costs[].keyword` takes
                # back and `ActionRow.optional_costs` prints; `None` for a
                # cost printed as a sentence with no keyword on it, which is
                # also the case this engine cannot charge.
                "keyword": option.keyword.capitalize() if option.keyword else None,
                "cost": option.cost,
                "rule": option.rule,
                "repeatable": option.repeatable,
                "understood": option.understood,
            }
            for option in printed
        ]
    printed_objects = _printed_object_costs(card)
    if printed_objects:
        # CR 702.51a/702.66a/702.126a's "pay part of this cost with objects
        # instead of mana", as a *printed* fact - which artifacts or creatures
        # are eligible is `object_costs` below, and needs a board. This half
        # is here for the same reason `additional_costs` is: it is what the
        # browser holds the instant the player picks the card up, before any
        # affordance fetch, and a cost that is not known that early is one
        # `ui/actions.js`'s `needsChoices` cannot stop the cast for. Kappa
        # Cannoneer is the case that proved it: no targets, no X, no modes and
        # no optional additional cost, so the cast fired at the full {5}{U}
        # with the artifact-tap step never drawn.
        hints["object_costs"] = printed_objects
    if modal is not None:
        # CR 700.2a's modes, each carrying whether this engine can read it
        # into an effect. `engine_api.unreadable_modes` is the same reading
        # that refuses the cast (`engine_api` raises
        # `unreadable_mode_notice`), asked before the choice instead of after
        # the targets are picked - so a mode that would spend the card for
        # nothing is marked on the chip rather than refused once it is too
        # late to have chosen differently. Failing closed out loud, the same
        # `understood: false` treatment an uncharged optional cost and an
        # unread CR 106.6 mana restriction already get.
        unreadable = {mode.number: mode for mode in unreadable_modes(state)}
        hints["modes"] = {
            "instruction": modal.instruction,
            "minimum": modal.minimum,
            "maximum": modal.maximum,
            "repeatable": modal.repeatable,
            "understood": modal.understood,
            "entwine_cost": modal.entwine_cost,
            "escalate_cost": modal.escalate_cost,
            "options": [
                {
                    "number": mode.number,
                    "text": mode.text,
                    "understood": mode.number not in unreadable,
                    "notice": (
                        unreadable_mode_notice(state, unreadable[mode.number])
                        if mode.number in unreadable
                        else None
                    ),
                }
                for mode in modal.modes
            ],
        }
    return hints


#: The object-cost keywords, each with the rule that defines it and the
#: engine's own predicate for "this printing has it" - never a reading of the
#: printed text by this module. `object_costs` below answers the same three
#: keywords against a board; this answers them off the printing alone.
#: Crew is deliberately absent: it is a Vehicle's own activated ability
#: (CR 702.122a), not part of casting anything.
_OBJECT_COST_KEYWORDS = (
    ("Convoke", "CR 702.51a", "has_convoke"),
    ("Delve", "CR 702.66a", "has_delve"),
    ("Improvise", "CR 702.126a", "has_improvise"),
)


def _printed_object_costs(card: Any) -> list[dict[str, Any]]:
    """Which of convoke, delve and improvise this printing prints."""
    from magic_cards_model.machines import objects_as_costs

    return [
        {"keyword": keyword, "rule": rule}
        for keyword, rule, predicate in _OBJECT_COST_KEYWORDS
        if getattr(objects_as_costs, predicate)(card)
    ]


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


def _profile_for(
    game: Game,
    state: CardState,
    ability_index: int | None,
    modes: list[int] | None,
    x_value: int | None,
    spell_face: str | None = None,
) -> tuple[TargetingProfile, list[tuple[int | None, str | None]] | None]:
    """The profile that governs this particular announcement: the engine's own
    public `Game.announcement_profile`, which reads an activated ability's own
    text (CR 602.2b), a modal spell's chosen modes, the named half of a split
    or adventurer card (CR 709.3b, CR 715.3b), or the whole card with X
    substituted (CR 107.3a).

    A modal spell with modes chosen is the one read that also needs to say
    which mode each requirement belongs to, so that one goes through the
    engine's `profile_for_modes_with_owners` to keep the owners.

    `spell_face` is passed straight through: `Game.announcement_profile`
    already ranks it below `ability_index` and `modes`, and it is the same
    read `legal_actions._cast_action` makes for every row it emits per face -
    without it, asking for Wear // Tear's candidates while casting Tear
    answered Wear's "target artifact", because `targeting_profile` reads
    `Card.front_oracle_text`.
    """
    if ability_index is None and modes:
        modal = modal_profile(state.card)
        if modal is not None:
            chosen = modal.resolve(modes)
            profile, owners = profile_for_modes_with_owners(state.card, chosen, x_value)
            return profile, [(chosen[owner].number, chosen[owner].text) for owner in owners]
    profile = game.announcement_profile(
        state,
        ability_index=ability_index,
        modes=modes,
        x_value=x_value,
        spell_face=spell_face,
    )
    return profile, None


@on_one_board
def candidates(
    game: Game,
    state: CardState,
    caster: str,
    *,
    ability_index: int | None = None,
    modes: list[int] | None = None,
    x_value: int | None = None,
    spell_face: str | None = None,
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
        profile, mode_of = _profile_for(game, state, ability_index, modes, x_value, spell_face)
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


@on_one_board
def slot_candidates(game: Game, slots: Sequence[TargetSlot]) -> dict[str, Any]:
    """A row's `targets` block: the engine's own candidates for each of its
    `target_slots` (`TargetSlot.candidates`/`player_candidates`, built by
    `legal_actions` from the predicate the announcement checks with, CR
    601.2c), written as the strings a seat hands straight back
    (`candidate_line`) and capped per slot.

    Nothing here decides legality - that is the engine's answer, read. `count`
    is the true total whatever the cap did to `candidates`: a seat weighing a
    sweeper needs the number even when it does not need the names.
    """
    players = {player.username: player for player in game.players}
    listed: list[dict[str, Any]] = []
    truncated_anywhere = False
    for index, slot in enumerate(slots):
        # A candidate id may name an ability on the stack (CR 115.2), which is
        # not a card: `find_by_id` alone dropped it from the list.
        things: list[Any] = [
            found
            for found in (
                game.find_by_id(card_id) or game.find_ability_by_id(card_id)
                for card_id in slot.candidates
            )
            if found
        ]
        things.extend(players[name] for name in slot.player_candidates if name in players)
        lines = [candidate_line(thing) for thing in things[:CANDIDATE_SLOT_CAP]]
        entry: dict[str, Any] = {"slot": index, "count": len(things), "candidates": lines}
        if len(things) > len(lines):
            entry["truncated"] = len(things) - len(lines)
            truncated_anywhere = True
        listed.append(entry)
    block: dict[str, Any] = {"understood": True, "slots": listed}
    if truncated_anywhere:
        block["more_with"] = MORE_CANDIDATES_WITH
    return block


@on_one_board
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
    original. The copy gets its own board scope, so the casting checks share
    one board on each side of the CR 601.2a move instead of one each.
    """
    probe = dry_run_copy(game)
    with one_board(probe):
        return _alternative_costs_on(probe, state, caster)


def _alternative_costs_on(probe: Game, state: CardState, caster: str) -> list[dict[str, Any]]:
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
    except STALE_BOARD:
        raise
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
            "alternative_cost": canonical_value(option),
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
    tapping, sacrificing and returning to hand all look at the battlefield
    (CR 701.21a's "you can only sacrifice something you control" narrows it
    to this player's own, and CR 702.49a's ninjutsu/CR 702.190a's sneak name
    an *attacking* creature the same way - only a permanent on the
    battlefield can be), discarding is hand-only by definition (CR 701.9a),
    and exiling reads the component's own `zone`, which is why Force of
    Negation exiles from hand and Abhorrent Oculus from a graveyard.

    `RETURN_TO_HAND`'s own reach is every battlefield permanent the caster
    controls, exactly like `TAP`/`SACRIFICE` - `_alternative_cost_candidates`
    below is what actually narrows that down to the ones currently unblocked
    (CR 509.1h), by asking each one's own `why_not` the same way every other
    kind here is filtered. Before this, Sneak's `requires` read 1 correctly
    but `candidates` came back `[]` unconditionally: a seat, or the web UI's
    alt-cost chips, had no way to discover which attacker could pay it.
    """
    from magic_cards_model.machines.cost import ComponentKind

    kind = ComponentKind(component.kind)
    if kind in (
        ComponentKind.TAP,
        ComponentKind.UNTAP,
        ComponentKind.SACRIFICE,
        ComponentKind.RETURN_TO_HAND,
    ):
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


@on_one_board
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
        has_station,
        improvise_offer,
        is_vehicle,
        power_of,
        station_offer,
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
    if state.zone is Zone.BATTLEFIELD and has_station(state.card):
        out["station"] = {
            "rule": "CR 702.184a",
            "candidates": eligible(station_offer(state), battlefield),
        }
    return out


def find_source(game: Game, card_id: UUID) -> CardState:
    state = game.find_by_id(card_id)
    if not isinstance(state, CardState):
        raise ValueError(f"no card with id {card_id} in this game")
    return state
