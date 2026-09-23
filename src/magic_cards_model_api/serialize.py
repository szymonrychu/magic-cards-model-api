"""The full state view: engine dump plus per-card detail and Scryfall images."""

from __future__ import annotations

import json
from typing import Any

from magic_cards_model.machines import AbilityOnStack, Step
from magic_cards_model.machines.abilities import keywords_in_lines
from magic_cards_model.machines.casting import (
    KeywordCastOption,
    MarkedExile,
    PlayPermission,
    castable_from,
    granted_cast_options,
    marker_cast_options,
)
from magic_cards_model.machines.combat import Keyword
from magic_cards_model.machines.keywords import cycling_abilities, cycling_activation
from magic_cards_model.machines.layers import Characteristics
from magic_cards_model.machines.turn import PHASE_OF
from magic_cards_model.models import CardState, Game, Player, Supertype, Type, Zone
from magic_cards_model.models.card import Layout
from magic_cards_model.models.game import KnownCard
from magic_cards_model.models.mana import ManaPool

from . import assets, commands, player_control, scryfall
from .affordances import cast_hints
from .board_scope import on_one_board, read_on_one_board
from .commands import paused_game
from .decisions import PendingDecision
from .engine_api import list_abilities
from .opening import pregame_view
from .store import store

StackObject = CardState | AbilityOnStack

#: The api's own name for CR 510.4's first-strike combat damage step. The
#: engine deliberately keeps one `Step.COMBAT_DAMAGE` for both occurrences
#: (`Game.is_first_strike_damage_step`'s own docstring explains why - there is
#: no separate `Step` member for it), so every api reader that compares,
#: ranks, stamps or records "the current step" needs a second name to tell the
#: two occurrences apart, and this is it. `step_position`/`step_position_of`
#: below are the one place that name is minted from a `Game`; `seatkit.
#: FIRST_STRIKE_DAMAGE`/`seatkit.step_position` are a deliberate duplicate for
#: a state *dict* rather than an import - `seatkit.py`'s own header explains
#: why that module never imports the rest of this package.
FIRST_STRIKE_DAMAGE = "first_strike_damage"


def step_position_of(step: Step | None, first_strike_damage_step: bool) -> str | None:
    """`step_position`'s answer from an already-read `(step, first_strike_
    damage_step)` pair, for a caller that stamped the two off a `Game` earlier
    and no longer holds the game itself (`records.action_board`, read back by
    `audit.record`). `None` exactly when `step` is `None`."""
    if step is None:
        return None
    if first_strike_damage_step:
        return FIRST_STRIKE_DAMAGE
    return step.value


def step_position(game: Game) -> str | None:
    """The step name the api reports for `game`'s current position: `game
    .turn.step`'s own value normally, or `FIRST_STRIKE_DAMAGE` while `game
    .is_first_strike_damage_step` (CR 510.4/506.1) is true. `None` exactly
    when `game.turn` is `None` (before CR 103.8's first turn)."""
    turn = game.turn
    return step_position_of(
        turn.step if turn is not None else None, game.is_first_strike_damage_step
    )


def _type_line(
    supertypes: tuple[Supertype, ...], types: tuple[Type, ...], subtypes: tuple[str, ...]
) -> str:
    """A display type line built from computed characteristics.

    `Characteristics` (`machines/layers.py`) carries types as enum tuples, not
    a printed string - there is nothing to read off the card once layers have
    changed what it is, so this is the same left-side/right-side-of-the-dash
    shape Scryfall's own `type_line` uses (CR 205.1), rebuilt from the parts.
    """
    left = " ".join(word.value.capitalize() for word in (*supertypes, *types))
    if not subtypes:
        return left
    return f"{left} — {' '.join(subtypes)}"


def _keywords_of(game: Game, state: CardState) -> list[str]:
    """The keyword abilities this permanent has right now.

    On the battlefield this reads the computed board (CR 613.5), so a keyword
    granted or removed by a continuous effect shows up; the vocabulary is the
    combat keywords `machines/combat.has_keyword` knows plus whatever is
    actually printed on the card, so a granted keyword outside that list (a
    landwalk this printing doesn't have, say) is still caught. Off the
    battlefield there is no computed board to ask - "permanents exist only
    on the battlefield" (CR 403.3), so a card anywhere else has none of these
    to compute - this falls back to `CardState.keywords` instead, printed
    plus face-down/DFC handling, with no layers applied.
    """
    printed = state.keywords
    if state.zone is not Zone.BATTLEFIELD:
        return list(printed)
    characteristics = game.characteristics_of(state)
    vocabulary = [keyword.value for keyword in Keyword] + list(printed)
    return sorted(keywords_in_lines(list(characteristics.abilities), vocabulary))


def _current_characteristics(game: Game, state: CardState) -> tuple[str, int | None, int | None]:
    """(type_line, power, toughness) as they are right now.

    A permanent on the battlefield gets the computed board (CR 613.5) - the
    whole point of this module existing is that a Forest earthbend animated or
    a creature under an anthem does not look printed. Anything not on the
    battlefield (hand, graveyard, library, stack before it resolves) has no
    computed characteristics to ask for - "permanents exist only on the
    battlefield" (CR 403.3) - so it falls back to `CardState`'s own
    printed-plus-counters accessors, which already do the right thing for a
    double-faced card's chosen face.
    """
    if state.zone is not Zone.BATTLEFIELD:
        return state.card.type_line, state.power, state.toughness
    characteristics: Characteristics = game.characteristics_of(state)
    type_line = _type_line(
        characteristics.supertypes, characteristics.types, characteristics.subtypes
    )
    return type_line, characteristics.power, characteristics.toughness


def _current_colors(game: Game, state: CardState) -> list[str]:
    """This object's colors right now, as color letters - the computed board's
    on the battlefield (a color-setting effect shows), `CardState.colors`
    anywhere else, the same split `_current_characteristics` makes. Read from
    the engine so a play script's `colors_any` never re-derives color from a
    mana cost."""
    if state.zone is Zone.BATTLEFIELD:
        colors = game.characteristics_of(state).colors
    else:
        colors = state.colors
    return [color.value for color in colors]


#: name -> image links, remembered across requests. POSITIVE RESULTS ONLY, so
#: a miss is retried on the next poll: a card can be absent now and present a
#: moment later, because validating a decklist writes fresh printings into the
#: cache while the game is running. An `lru_cache` here would pin that miss for
#: the life of the process and the art would never appear.
_image_memo: dict[str, dict[str, str]] = {}


def _image_links(name: str) -> dict[str, str]:
    """Image links for one card name, from LOCAL DISK ONLY.

    Two tiers, both local: the Scryfall disk cache, then the engine's fixture
    pool. Reading only the cache - which is what this did - is wrong wherever
    the cache is cold, and the cache is cold by design for pooled cards:
    `fetch_card` returns a pooled printing WITHOUT copying it into
    `cache_dir()`, deliberately, so that the cache does not go stale against a
    moving engine checkout. A card served out of the pool therefore never
    appears in the cache at all, and this returned `{}` for it forever. On a
    dev box with a long-warm cache that is invisible; on a fresh deploy with an
    empty cache volume it is every card in the game, and the UI renders name
    plates with no art.

    Tier 3, Scryfall itself, is deliberately NOT used: this runs for every card
    in the game on every poll of the state, about once a second per open tab,
    and a network round trip per unknown card in that path would be a hang, not
    a picture. A name in neither tier gets no image, same as before.

    Memoised because a printing's image links never change, and without it a
    hundred-card board reparses a hundred JSON files on every poll.
    """
    hit = _image_memo.get(name)
    if hit is not None:
        return hit
    raw = scryfall.load_cached(name) or scryfall.load_pooled(name)
    links = scryfall.image_uris(raw) if raw else {}
    if links:
        _image_memo[name] = links
    return links


def _images_for(game: Game) -> dict[str, dict[str, str]]:
    """Card name -> image links. Best effort, local disk only."""
    images: dict[str, dict[str, str]] = {}
    for state in _all_cards(game):
        name = state.card.name
        if name in images:
            continue
        images[name] = dict(_image_links(name))
    return images


def _all_cards(game: Game) -> list[CardState]:
    cards: list[CardState] = []
    for zone_cards in game.shared_zones.values():
        cards.extend(state for state in zone_cards if isinstance(state, CardState))
    for player in game.players:
        for zone_cards in player.zones.values():
            cards.extend(zone_cards)
    return cards


def _target_refs(game: Game, targets: list[Any]) -> list[dict[str, str]]:
    """What each target resolves to: kind, id, and a display name - what a
    client needs to highlight the target on the board, not only print its
    name. The single place target resolution happens; `_target_names` is a
    thin wrapper over this that keeps only the `name` field."""
    from magic_cards_model.models import Player as PlayerModel

    refs = []
    for ref in targets:
        try:
            thing = game.resolve_target(ref)
        except Exception:
            thing = None
        if isinstance(thing, CardState):
            refs.append({"kind": "card", "id": str(thing.id), "name": thing.name})
        elif isinstance(thing, PlayerModel):
            refs.append({"kind": "player", "id": thing.username, "name": thing.username})
        elif isinstance(thing, AbilityOnStack):
            refs.append(
                {
                    "kind": "ability",
                    "id": str(thing.id),
                    "name": f"ability from {thing.source_name}",
                }
            )
        else:
            refs.append({"kind": "unknown", "id": str(ref), "name": str(ref)})
    return refs


def _target_names(game: Game, targets: list[Any]) -> list[str]:
    return [ref["name"] for ref in _target_refs(game, targets)]


def _special_actions(game: Game, state: CardState) -> dict[str, Any]:
    """What this hand card's CR 116.2 special actions and keyword-activated
    zone-leaving abilities offer right now: suspend/plot/foretell (each a
    `MarkedExile` special action) and cycling (an activated ability, but one
    that only functions from the hand - CR 702.29a).

    Every entry here is computed by building the engine's own action object
    and reading its cost back - `MarkedExile.plot`/`.foretell`/`.suspend` and
    `cycling_activation` all validate timing and printed-cost legality as part
    of construction and raise rather than mutate anything, so a card that
    cannot currently take the action is simply left out (its `ValueError` is
    the "not right now" answer), and building one to check never taps a
    source, discards a card, or otherwise changes the board - the same
    side-effect-free shape a `dry_run` cast already relies on.

    Empty for anything not in a hand: these are all CR 702.29b-style "only
    while in a player's hand" abilities.
    """
    if state.zone is not Zone.HAND:
        return {}
    controller = state.controller or state.owner
    if controller is None:
        return {}
    actions: dict[str, Any] = {}
    for name, factory in (
        ("plot", MarkedExile.plot),
        ("foretell", MarkedExile.foretell),
        ("suspend", MarkedExile.suspend),
    ):
        try:
            action = factory(game, state, controller)
        except ValueError:
            continue
        entry: dict[str, Any] = {"cost": str(action.cost)}
        if action.time_counters is not None:
            entry["time_counters"] = action.time_counters
        actions[name] = entry

    cyclers = []
    for index, ability in enumerate(cycling_abilities(state.card)):
        try:
            activation = cycling_activation(game, state, controller, index)
        except ValueError:
            continue
        cyclers.append({"index": index, "keyword": ability.canonical, "cost": str(activation.cost)})
    if cyclers:
        actions["cycling"] = cyclers
    return actions


def _exile_pile(state: CardState) -> dict[str, Any] | None:
    """CR 406.4's pile marker, for a card in exile: which special action put
    it there, whose it is, and the turn it landed - the fact that makes "may I
    cast this from exile" answerable at all (`Casting.use_keyword`/
    `MarkedExile`'s own `marker`/`owner`/`turn_exiled` reads). `None` for a
    card in exile no special action marked (an ordinary exiled card)."""
    annotation = state.exile_annotation
    if annotation is None:
        return None
    pile = {
        "marker": annotation.marker.value,
        "owner": annotation.owner,
        "turn_exiled": annotation.turn_exiled,
    }
    # The permanent this card was exiled *with*, when there is one -
    # `ExileAnnotation.linked_permanent_id`, which imprint and hideaway both
    # set. This is the whole of what makes "that card is under that
    # permanent" answerable by a client: the card is in exile like any other,
    # and only this id says which permanent it physically sits beneath.
    # Absent, rather than null, for every marker that links nothing - the
    # same shape the rest of this view uses for a field that does not apply.
    #
    # Nothing clears it when the linked permanent leaves the battlefield (see
    # `ExileAnnotation.linked_permanent_id`'s own docstring), so a reader has
    # to treat it as a lookup that can find nothing rather than a promise.
    if annotation.linked_permanent_id is not None:
        pile["linked_permanent_id"] = str(annotation.linked_permanent_id)
    return pile


def _cast_option_view(
    game: Game,
    spell: CardState,
    option: KeywordCastOption,
    caster: str,
    granted_by: str | None = None,
) -> dict[str, Any]:
    """One `KeywordCastOption` (`machines/casting.py`, CR 601.3's "a rule or
    effect allows that player to cast it") this hand, graveyard or exile card
    carries right now, shaped so a seat can drive `POST .../actions/cast`
    with `keyword=` set to exactly this `keyword` value.

    From the hand the option is never permission to *reach* the card - CR
    402.3 already lets its holder cast it from there - it is CR 118.9's
    alternative cost that replaces the printed mana cost, which `cost` and
    `pays_mana_cost: false` say. `POST .../actions/cast` takes it either as
    `keyword` or, identically, as `alternative_cost` (CR 118.9a allows one
    per spell, and `Casting._announce_alternative` routes a keyword-sourced
    option straight back into `use_keyword`).

    A real game already hit the gap this closes: Faithful Mending
    (`Flashback {1}{W}{U}`, CR 702.34a) was cast from a graveyard through this
    API for its printed `{W}{U}` face cost - the wrong cost, and no keyword
    named - because nothing in the state a seat reads ever said the card had
    a flashback option at all, let alone what it cost. `_play_permission_view`
    above closed the same shape of gap for `PlayPermission`; this is its
    counterpart for the keyword permissions of CR 601.2b (flashback, mayhem,
    plot, foretell, madness, airbend, a printed CR 604.6 zone permission, and
    a keyword granted by another permanent's static ability, CR 610.5/611.3d)
    - none of which need a live `PlayPermission` object at all, so none of
    them show up there.

    `usable_now`/`why_not` mirror `_play_permission_view`'s own fields:
    `option.why_not` already encodes every legality check that option carries
    (the right zone, an exile marker's owner, `discarded_this_turn`, timing),
    so this reports its answer rather than re-deriving it.
    """
    reason = option.why_not(spell, caster, game)
    entry: dict[str, Any] = {
        "keyword": option.keyword,
        "rule": option.rule,
        "pays_mana_cost": option.pays_mana_cost,
        "cost": str(option.cost) if option.cost is not None else None,
        "usable_now": reason is None,
        "why_not": reason,
    }
    if granted_by is not None:
        entry["granted_by"] = granted_by
    return entry


#: The zones a card carries `cast_options` in. The hand is here for the
#: keywords that replace a spell's mana cost from the zone it is already legal
#: to cast from - evoke (CR 702.74a), dash (CR 702.109a), morph (CR 702.37a),
#: megamorph (CR 702.37b), disguise (CR 702.168a), bestow (CR 702.103a) and
#: warp's hand half (CR 702.185a). Every one of those is a bare CR 118.9
#: alternative cost and needs no CR 601.3 permission to *reach* the card, which
#: is exactly why none of them ever appeared on any surface a seat could read:
#: `play_permissions` never sees them, and `_special_actions` covers only the
#: CR 116.2 special actions (plot, foretell, suspend) and cycling. A real game
#: lost an evoke to that gap - "I'm not able to evoke through webUI ... I tried
#: to evoke Wistfulness".
#:
#: Nothing here names a keyword: which ones apply is `castable_from`'s answer,
#: so a keyword the engine learns next shows up with no change to this file.
_CAST_OPTION_ZONES = (Zone.HAND, Zone.GRAVEYARD, Zone.EXILE)


def _cast_options_of(game: Game, state: CardState) -> list[dict[str, Any]]:
    """Every `_cast_option_view` this hand, graveyard or exile card has right
    now, or `[]` for a card anywhere else (a permanent on the battlefield or a
    spell on the stack is not being cast from where it sits) and for a card
    with no controller or owner recorded (nobody to be the `caster`
    `KeywordCastOption.why_not` asks about).

    Three sources, matching `Casting._use_keyword`'s own
    `printed_options + marker_options + granted_options` (the exact set CR
    601.2b lets an announcement pick from), so a keyword added to any of them
    shows up here for free rather than being re-listed by hand:

    - `castable_from(state.card, state.zone)` - the card's own printed
      keywords whose zone matches where it is sitting (CR 604.1's static
      abilities, e.g. CR 702.34a flashback, CR 702.187a mayhem, CR 702.170a
      plot, CR 702.143a foretell, CR 702.35a madness from a graveyard or
      exile; CR 702.74a evoke, CR 702.109a dash, CR 702.37a morph,
      CR 702.37b megamorph, CR 702.168a disguise, CR 702.103a bestow and
      CR 702.185a warp from the hand) plus a printed CR 604.6 sentence
      opening an extra zone for one of them (CR 113.6f).
    - `marker_cast_options(state)` - the permission a CR 406.4 exile pile
      itself grants rather than the card's own text (CR 701.65a's airbend).
      Empty for a hand card, which is in no pile of exile.
    - `granted_cast_options` - a keyword another permanent's static ability
      is granting this card right now (CR 610.5's "as you cast it", or CR
      611.3d's "while it sits in the zone", both kept alive on the object by
      CR 400.7b/400.7g once it is on the stack). Ashling, the Limitless
      granting evoke to an Elemental in hand is exactly this, and it is why
      the hand cannot be answered off the printed card alone.

    `caster` is this card's own controller-or-owner - the same fallback
    `_special_actions` uses - because every option here is a permission to
    a *player*, and the only player who could ever hold it for a card sitting
    in a hand, a graveyard or exile is whoever's card it is.

    **`usable_now` answers the option's own conditions and nothing else.**
    `KeywordCastOption.why_not` checks the zone, an exile marker's owner and
    turn, `discarded_this_turn`, and a keyword's own "as a sorcery" clause -
    never CR 117.1a's timing rule for the *card*, which is a question about
    the spell rather than about the keyword paying for it (the engine's own
    `legal_actions` documents the same gap and asks `why_cannot_cast_now`
    separately). So a creature's evoke option reads `usable_now: true` during
    an opponent's turn, exactly as a graveyard flashback option already does.
    `GET .../legal-actions` is the surface that answers "could I take this
    action right now", and it enumerates each of these as its own `cast`
    entry carrying `keyword` and `alternative_cost`.
    """
    if state.zone not in _CAST_OPTION_ZONES:
        return []
    caster = state.controller or state.owner
    if caster is None:
        return []
    options = castable_from(state.card, state.zone) + marker_cast_options(state)
    views = [_cast_option_view(game, state, option, caster) for option in options]
    for granted in granted_cast_options(game, state, caster, state.zone):
        views.append(_cast_option_view(game, state, granted.option, caster, granted.source_name))
    return views


def _play_permission_view(
    game: Game, permission: PlayPermission, images: dict[str, dict[str, str]]
) -> dict[str, Any] | None:
    """One `PlayPermission` (CR 601.3's dynamic half - `machines/casting.py`),
    shaped so a seat holding it can actually drive `Casting.use_permission()`:
    which card, from which zone, granted by what, whether it pays the mana
    cost, and when it expires.

    A real game already hit the gap this closes: a graveyard's flashback
    grant, or a card exiled and made playable "this turn", was accepted, the
    engine granted the permission, and nothing in the state a seat reads ever
    named it - the card just sat there, unusable though `POST .../actions/
    cast` (with no `keyword` needed - see `engine_api.cast_card`'s exile-zone
    branch) would have driven it straight through. (Cascade's own "you may
    cast it without paying its mana cost" no longer works this way at all,
    battle 8: accepting it casts the card immediately while the trigger is
    still resolving, and no permission is ever granted for a seat to find
    here.)

    `None` when the object the permission was granted against is no longer
    findable (moved zones since - CR 400.7 already makes the permission dead
    the instant that happens; `PlayPermission.names` is the same identity
    check `usable_play_permission` runs) - nothing useful to show a seat
    about a permission that no longer names anything on the board.
    """
    card = game.find_by_id(permission.card_id)
    if card is None or not permission.names(card):
        return None
    reason = permission.why_not(card, permission.player, game)
    granted_step = permission.granted_step.value if permission.granted_step is not None else None
    if permission.immediate:
        expires = (
            f"only for the {granted_step or '?'} step it was granted in (CR 117.2e), "
            "not until end of turn"
        )
    elif permission.until_end_of_turn:
        expires = "until end of turn (CR 514.2)"
    else:
        expires = "when used"
    return {
        "card": _card_view(game, card, images),
        "granted_by": permission.source_name,
        "action": permission.action.value,
        "pays_mana_cost": permission.pays_mana_cost,
        "from_zone": permission.zone.value,
        "rule": permission.rule,
        "until_end_of_turn": permission.until_end_of_turn,
        "immediate": permission.immediate,
        "granted_step": granted_step,
        "expires": expires,
        "expired": permission.expired(game),
        "usable_now": reason is None,
        "why_not": reason,
    }


def _play_permissions_view(
    game: Game, images: dict[str, dict[str, str]]
) -> dict[str, list[dict[str, Any]]]:
    """Every live `PlayPermission`, grouped by the player who may use it.

    Not secret: every permission this engine actually grants today (a
    flashback grant, a card exiled and made playable "this turn") names a
    card that is already visible in a public zone (CR 400.2's exile or
    graveyard), so there is nothing here for
    `visibility.redact_view` to hide beyond the ordinary face-down-card
    redaction it already applies to `exile`/`play_permissions["card"]["face_up"]`
    entries - handled generically since each entry nests a full `_card_view`.
    """
    by_player: dict[str, list[dict[str, Any]]] = {player.username: [] for player in game.players}
    for permission in game.play_permissions:
        view = _play_permission_view(game, permission, images)
        if view is not None:
            by_player.setdefault(permission.player, []).append(view)
    return by_player


#: The layouts whose faces are CR 712 double faces - one physical card with a
#: front and a back, as opposed to a split card's or an adventurer's halves,
#: which share one face (CR 709, CR 715).
_DOUBLE_FACED_LAYOUTS = frozenset(
    {
        Layout.TRANSFORM,
        Layout.MODAL_DFC,
        Layout.MELD,
        Layout.DOUBLE_FACED_TOKEN,
        Layout.REVERSIBLE_CARD,
    }
)


def _oracle_text(state: CardState) -> str | None:
    """The printed rules text a card view shows.

    The printing's own top-level text when Scryfall fills it in (every
    single-faced card, split and adventure cards). A double-faced printing
    leaves it null and puts each face's text on `card_faces`, so the view
    reads the face the engine says is up (`CardState.up_face`): the front
    face anywhere but the battlefield or stack (CR 712.8a), the face that is
    up there (CR 712.8d-f). `up_face` is read directly rather than through
    `CardState.oracle_text`, which blanks a face-down card - its owner's view
    keeps the printed text, and `visibility` stubs it for everyone else.
    """
    if state.card.oracle_text is not None:
        return state.card.oracle_text
    up_face = state.up_face
    return up_face.oracle_text if up_face is not None else None


def _faces_view(state: CardState) -> list[dict[str, Any]] | None:
    """Each face of a double-faced card, front first, or None for any other
    card. A play-script selector picks the face a card has in its zone from
    this list (CR 712.8a, 712.8f) - `type_line` off the battlefield is the
    printing's combined `Front // Back` line, which is neither face.

    One face may carry `land_face: True` (`playscripts.selectors._has_land_
    face`, O5): the engine's own `Card.land_face` (CR 712.12), which reads
    only a modal double-faced card - a transform or meld card's land back
    (Treasure Map // Treasure Cove) is reached only by transforming or
    melding (CR 712.2), never a land drop, so it is never marked here even
    though it is one of the `faces` below like any other."""
    faces = state.card.card_faces or []
    if state.card.layout not in _DOUBLE_FACED_LAYOUTS or len(faces) < 2:
        return None
    land_face = state.card.land_face
    return [
        {
            "name": face.name,
            "mana_cost": face.mana_cost,
            "type_line": face.type_line,
            "oracle_text": face.oracle_text,
            **({"land_face": True} if face is land_face else {}),
        }
        for face in faces
    ]


def _mana_value(game: Game, state: CardState) -> float:
    """This object's mana value (CR 202.3), the way `state.mana_value` itself
    never can: CR 202.3e makes an `{X}` in the cost read as the chosen number
    while the object is on the stack, not CR 107.3g's off-stack `X = 0` -
    which is all `CardState.mana_value` computes, since a `CardState` has
    nowhere of its own to keep the announced X once casting is over (see
    `magic_cards_model.models.game.CastRecord.x_value`'s own docstring).

    `state.mana_value` already does everything else right (meld's combined
    value, a land-face MDFC's 0, a face-down object's 0), so this only adds
    CR 202.3e's stack-only delta on top of it, the same arithmetic
    `Game.put_on_stack` already runs for its own `CAST_SPELL` event (see the
    CR 107.3i comment there: every `{X}` in the cost is worth the chosen X,
    not one shared between them - Walking Ballista's `{X}{X}` at X=3 is 6,
    not 3)."""
    base = state.mana_value
    if state.zone is not Zone.STACK:
        return base
    x_count = (state.mana_cost or "").count("{X}")
    if not x_count:
        return base
    return base + x_count * game.cast_record(state).x_value


def _card_view(game: Game, state: CardState, images: dict[str, dict[str, str]]) -> dict[str, Any]:
    image = images.get(state.card.name, {})
    proxied = assets.proxy_image_links(state.card.name, image) if image else {}
    type_line, power, toughness = _current_characteristics(game, state)
    view = {
        "id": str(state.id),
        "name": state.name,
        "card_name": state.card.name,
        # `type_line`/`power`/`toughness`/`keywords` are what this object *is*
        # right now: computed off the battlefield board (CR 613.5) when it's a
        # permanent, printed-plus-counters otherwise. `printed_*` is added
        # below only on the battlefield and only when it differs, so a card
        # nothing has touched carries no redundant duplicate fields, and an
        # animated or pumped one is flagged rather than just silently shown
        # with a different number a player has to notice on their own.
        "type_line": type_line,
        # A `transform`/`modal_dfc` printing leaves both top-level fields blank
        # and carries them per face; the object's own reading (`CardState.
        # mana_cost`/`oracle_text`, its up face - the front one in hand,
        # CR 712.8a) fills them in. A printing that has them keeps them.
        "mana_cost": state.card.mana_cost or state.mana_cost,
        "oracle_text": _oracle_text(state),
        "zone": state.zone.value,
        "controller": state.controller,
        "owner": state.owner,
        "tapped": state.tapped,
        "face_up": state.face_up,
        "counters": dict(state.counters.root),
        "damage": state.damage,
        "summoning_sick": state.is_summoning_sick(game),
        "power": power,
        "toughness": toughness,
        "colors": _current_colors(game, state),
        "mana_value": state.mana_value,
        "keywords": _keywords_of(game, state),
        "targets": _target_names(game, state.targets),
        "target_refs": _target_refs(game, state.targets),
        "image": proxied.get("normal"),
        "small_image": proxied.get("small"),
        "art_crop": proxied.get("art_crop"),
        "abilities": list_abilities(game, state),
    }
    # CR 614.12a's "as this enters, choose ..." answer, recorded on the object
    # that carries it - Pithing Needle's and Disruptor Flute's named card,
    # Cavern of Souls' creature type, Utopia Sprawl's colour. Without this the
    # board shows a permanent whose whole function is the name it chose and
    # never says which name that was.
    if state.choice is not None:
        view["choice"] = {"kind": state.choice.kind.value, "value": state.choice.value}
    faces = _faces_view(state)
    if faces is not None:
        view["faces"] = faces
    if state.zone in (Zone.HAND, Zone.EXILE):
        # What announcing this card would ask for: X, modes, targets. Read off
        # the printed card, so it is answerable before any `Casting` exists -
        # which is what lets the UI show an X box only for a spell that has an
        # X, and a target picker only for a spell that targets.
        view["cast"] = cast_hints(state)
    if state.zone is Zone.HAND:
        special_actions = _special_actions(game, state)
        if special_actions:
            view["special_actions"] = special_actions
    if state.zone is Zone.EXILE:
        exile_pile = _exile_pile(state)
        if exile_pile is not None:
            view["exile_pile"] = exile_pile
    if state.zone in _CAST_OPTION_ZONES:
        cast_options = _cast_options_of(game, state)
        if cast_options:
            # Own-view only: `visibility.redact_view` strips this back out of
            # a card whose owner/controller is not the viewer, the same shape
            # `_special_actions` already gets for free by only existing on a
            # hidden hand - see `visibility._redact_cast_options`.
            view["cast_options"] = cast_options
    if state.zone is Zone.STACK:
        # CR 202.3e: only on the stack does an `{X}` in the cost mean
        # anything other than 0 (CR 107.3g) - see `_mana_value`'s own
        # docstring. `x_value` itself is added only when the cost actually
        # has an `{X}` to announce, so a spell with none carries no stray
        # `x_value: 0` a client could mistake for an announced X (battle-7,
        # B7-13).
        view["mana_value"] = _mana_value(game, state)
        if "{X}" in (state.mana_cost or ""):
            view["x_value"] = game.cast_record(state).x_value
    if state.zone is Zone.BATTLEFIELD:
        # What this Equipment or Aura is attached to (`CardState.attachment`),
        # in the same `{kind, id, name}` shape every other reference on this
        # wire uses - a card, or a player, since an Aura's enchant ability can
        # name one. Absent when it is attached to nothing, which is how every
        # Equipment enters and how every other permanent stays. Battlefield
        # only: the engine clears the field on the way out of the zone, so
        # anywhere else the key would only ever be absent anyway.
        if state.attachment is not None:
            view["attached_to"] = _target_refs(game, [state.attachment])[0]
        if state.prepared:
            # CR 722.3a's "prepared" designation - a marker a permanent with
            # a prepare spell carries after its own preparation card cast
            # from exile, surfaced only on the battlefield the same way
            # `attached_to` is: the field the engine clears the moment the
            # permanent leaves is one a client need never see stale.
            view["prepared"] = True
        printed_type_line = state.card.type_line
        # The printing plus counters, never the layer engine: a bound card's
        # `power`/`toughness` already answer with the computed board (CR 613.5).
        printed_power, printed_toughness = state.printed_power, state.printed_toughness
        if printed_type_line != type_line:
            view["printed_type_line"] = printed_type_line
        if printed_power != power or printed_toughness != toughness:
            view["printed_power"] = printed_power
            view["printed_toughness"] = printed_toughness
    return view


def _stack_view(game: Game, images: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """The stack, top first."""
    views = []
    for obj in reversed(game.stack):
        if isinstance(obj, CardState):
            view = _card_view(game, obj, images)
            view["kind"] = "spell"
        else:
            view = {
                "kind": "ability",
                "id": str(obj.id),
                "name": obj.source_name,
                "controller": obj.controller,
                "text": obj.ability.text,
                "targets": _target_names(game, obj.targets),
                "target_refs": _target_refs(game, obj.targets),
                "zone": "stack",
            }
        views.append(view)
    return views


def _player_view(
    game: Game, player: Player, counters: dict[str, int] | None = None
) -> dict[str, Any]:
    """One seat's own block. `counters`, when given, replaces the committed
    player's own - `state_view` hands in the ones a paused command is
    standing on (`_paused_overlay`).

    No `decklist` or `agent` field any more (2b's T8, map's S6): both used to
    be read here from lobby-owned state (`store.decklists`/
    `store.agent_settings`) on *every* state read, including the browser's
    1.2s poll - the api component reading lobby-owned data on the hottest
    path in the app is exactly the reverse seam the six-way cut cannot carry
    across. Both moved to the lobby's own `GET /api/lobby/games/{id}/seats`
    (also the S7 fix, folding in what used to be a separate AI-seat-status
    poll) - `decklist` redacted there the same way `visibility._redact_player`
    redacted it here (present only for the caller's own `uid`), since it is
    still genuinely needed (`mtg_agent.driver.app`'s `seat_decklist`, for
    matchup-guide selection) and still hidden information under CR 401.2.
    `visibility.py` itself is untouched; its `decklist`-specific redaction is
    simply never triggered any more, since the key no longer exists here to
    redact.
    """
    return {
        "username": player.username,
        "life": player.life,
        "counters": dict(player.counters.root) if counters is None else counters,
        "mana_pool": _mana_pool_view(player.mana_pool),
        "hand_count": len(player.hand),
        "library_count": len(player.library),
        "graveyard_count": len(player.graveyard),
        "sideboard_count": len(player.sideboard),
        "maximum_hand_size": player.maximum_hand_size,
        # The engine's own land-play count and allowance, so "is a land drop
        # left" is read, never re-derived (`Game.why_cannot_play_land`).
        "lands_played": game.lands_played.get(player.username, 0),
        "land_allowance": game.land_allowance(player.username),
        # How many of this player's own turns have begun, extra turns
        # included (CR 500.7) - the engine's own count
        # (`TurnMachine.own_turns_taken`), never `turn_number` (rounds of
        # the table, blind to an extra turn). 0, never null, whenever `turn`
        # is set at all (pregame included); null only for the no-turn-
        # machine-at-all case a fresh `Game` before setup could carry.
        "own_turns_taken": game.turn.own_turns_taken(player.username)
        if game.turn is not None
        else None,
        # CR 723.5: who is making this player's decisions right now, when it
        # is somebody else - null while they decide for themselves (CR 723.9).
        "decisions_made_by": player_control.controllers(game).get(player.username),
    }


def _mana_pool_view(pool: ManaPool) -> dict[str, Any]:
    """One seat's mana pool (`ManaPoolView`), CR 106.6 tags included.

    `restricted` and `spend_effects` are **subsets** of `floating`, not
    alternatives to it. The engine keeps them that way on purpose - CR 106.4a
    has the player announce all the mana that is there, and a restriction
    narrows only what may be *done* with it - so this reports the whole
    `floating` total exactly as it always did and says, beside it, how much of
    that total is under each sentence. Reporting only the total was the defect:
    two restricted `{C}` serialised as `floating {"C": 2}` and were
    indistinguishable from two a player could spend on anything.

    `understood` travels with each restriction rather than being filtered out
    here. An unread CR 106.6 sentence is fail-closed in the engine, so that
    mana pays for nothing at all; a view that omitted the flag would show a
    player a resource they do not have.

    Both keys are always present, empty lists included, so a client never has
    to tell "no restrictions" from "this build does not report them".
    """
    return {
        "floating": {color.value: count for color, count in pool.floating.items()},
        "snow": {color.value: count for color, count in pool.snow.items()},
        "restricted": [
            {
                "restriction": {
                    "text": entry.restriction.text,
                    "understood": entry.restriction.understood,
                },
                "amounts": {color.value: count for color, count in entry.amounts.items()},
            }
            for entry in pool.restricted
        ],
        "spend_effects": [
            {
                "text": entry.effect.text,
                "amounts": {color.value: count for color, count in entry.amounts.items()},
            }
            for entry in pool.spend_effects
        ],
    }


@on_one_board
def snapshot(game: Game) -> dict[str, Any]:
    """A compact, full-information snapshot for the audit log.

    The log is the debugger's eye over a finished or running game, so nothing
    is hidden here: both players' hands, the board with tap/damage/counter
    state, the stack with each object's controller and targets, graveyards,
    exile, life, and pools. Cards are one readable string each, so a whole
    game's log stays digestible.
    """
    battlefield_states = [
        state
        for state in game.shared_zones.get(Zone.BATTLEFIELD, [])
        if isinstance(state, CardState)
    ]
    # Every permanent's index in this same list, doubling as this snapshot's
    # own object identity: a snapshot carries no card ids at all (unlike
    # `_card_view`'s `attached_to`, which names the engine's own uuid), and
    # `ui/review_model.js`'s `frameState` already builds a `bf-${index}` id
    # for each battlefield line straight off its position in the array. The
    # Python side and the JS side walk the exact same list in the exact same
    # order, so an index is an identity both already agree on for free -
    # nothing new has to ride along to make it one.
    battlefield_index = {state.id: index for index, state in enumerate(battlefield_states)}

    def host_index(state: CardState) -> int | None:
        """The battlefield index `state.attachment` (an Aura's enchanted
        object or player, CR 303.4b, or an Equipment's equipped creature, CR
        301.5a) points at, or `None` when there is none, it names a player
        rather than a card, or it names a card no longer on the battlefield -
        the same "not there any more" case `game.resolve_target` already
        answers for CR 400.7's zone changes.
        """
        if state.attachment is None:
            return None
        try:
            thing = game.resolve_target(state.attachment)
        except Exception:
            return None
        if not isinstance(thing, CardState):
            return None
        return battlefield_index.get(thing.id)

    def exile_host_index(state: CardState) -> int | None:
        """The battlefield index CR 406.4's `ExileAnnotation.
        linked_permanent_id` (imprint, hideaway) points at, or `None` for a
        card with no annotation, no link, or a link whose permanent has
        since left the battlefield - the field is never cleared when that
        happens (`ExileAnnotation.linked_permanent_id`'s own docstring), so
        this has to be a lookup that can find nothing rather than a promise
        that always resolves, exactly like `_exile_pile`'s live version.
        """
        annotation = state.exile_annotation
        if annotation is None or annotation.linked_permanent_id is None:
            return None
        return battlefield_index.get(annotation.linked_permanent_id)

    def battlefield_line(state: CardState) -> str:
        bits = [f"{state.controller}: {state.name}"]
        if state.tapped:
            bits.append("(tapped)")
        if state.is_summoning_sick(game):
            bits.append("(sick)")
        # Computed, not printed (CR 613.5): a battlefield permanent's power
        # and toughness are whatever the layer system currently makes them -
        # an animated land, an anthem, a pump - and a log recording the
        # printed number instead would hide exactly the state a debugger
        # reads this log to find.
        power, toughness = game.power_of(state), game.toughness_of(state)
        if power is not None:
            bits.append(f"[{power}/{toughness}]")
        if state.damage:
            bits.append(f"dmg:{state.damage}")
        if state.counters.root:
            counters = ",".join(f"{kind}={amount}" for kind, amount in state.counters.root.items())
            bits.append(f"({counters})")
        # Last, so `ui/review_model.js`'s `parsePermanent` - which strips
        # every suffix off the end, in the reverse of the order they are
        # appended here - takes this one off first. `N` is `host_index`
        # above: an Aura or Equipment tucked under the permanent that sits
        # at this snapshot's own battlefield index `N`.
        index = host_index(state)
        if index is not None:
            bits.append(f"(attached:{index})")
        return " ".join(bits)

    def exile_line(state: CardState) -> str:
        index = exile_host_index(state)
        return state.name if index is None else f"{state.name} (attached:{index})"

    def stack_object(obj: StackObject) -> dict[str, Any]:
        if isinstance(obj, CardState):
            return {
                "name": obj.name,
                "kind": "spell",
                "controller": obj.controller,
                "targets": _target_names(game, obj.targets),
                "target_refs": _target_refs(game, obj.targets),
            }
        return {
            "name": obj.source_name,
            "kind": "ability",
            "controller": obj.controller,
            "targets": _target_names(game, obj.targets),
            "target_refs": _target_refs(game, obj.targets),
        }

    combat_lines = []
    for attack in game.combat.attacks:
        attacker = game.find_by_id(attack.attacker_id)
        attacker_name = attacker.name if attacker else str(attack.attacker_id)
        blockers = []
        for blocker in attack.blockers:
            found = game.find_by_id(blocker)
            blockers.append(found.name if found else str(blocker))
        line = f"{attacker_name} -> {attack.defender}"
        if blockers:
            line += f" (blocked by {', '.join(blockers)})"
        combat_lines.append(line)

    battlefield = [battlefield_line(state) for state in battlefield_states]
    return {
        "life": {player.username: player.life for player in game.players},
        "pools": {
            player.username: {
                color.value: count for color, count in player.mana_pool.floating.items()
            }
            for player in game.players
        },
        "battlefield": battlefield,
        "hands": {
            player.username: [state.name for state in player.hand] for player in game.players
        },
        "stack": [stack_object(obj) for obj in game.stack],
        "graveyards": {
            player.username: [state.name for state in player.graveyard] for player in game.players
        },
        "exile": [
            exile_line(state)
            for state in game.shared_zones.get(Zone.EXILE, [])
            if isinstance(state, CardState)
        ],
        "combat": combat_lines,
    }


def game_summary(game: Game, game_id: str) -> dict[str, Any]:
    """S2 (2b's T8): exactly the scalars a lobby's game list needs to show
    something for each game - never the full `state_view`, which is what the
    N+1 this replaces was paying for. Every field here is a bare read off
    `game.turn`/`game.players`/`game.is_over`, none of it Scryfall-image or
    per-card work, so this is cheap enough to call once per game, in a loop,
    with nothing worth batching further at this layer - the batching that
    matters is one HTTP round trip for every id, not one call per game
    inside this function.
    """
    turn = game.turn
    step = turn.step if turn is not None else Step.UNTAP
    phase = PHASE_OF.get(step)
    return {
        "game_id": game_id,
        "is_over": game.is_over,
        "turn_number": turn.turn_number if turn is not None else None,
        "active_player": turn.players[turn.active_index] if turn is not None else None,
        "phase": phase.value if phase is not None else None,
        "seat_count": len(game.players),
    }


def unloadable_game_summary(game_id: str, reason: str, *, seat_count: int) -> dict[str, Any]:
    """`game_summary`'s shape for a game whose snapshot this build cannot
    decode (`repository.GameUnloadable`) - the same keys, with every scalar
    that could only have come off the board at its unknown value, plus
    `unloadable` carrying the decoder's own words.

    A function beside `game_summary` rather than an inline dict at each
    caller because two callers build this shape - `app.game_summaries_route`
    and `api_client.InProcessApiClient.game_summaries`, the route and the
    in-process seam standing in for it - and a lobby that saw two different
    shapes depending on which side of the (pre-cut) boundary answered would
    be a bug nobody would find until the cut.

    `is_over` is `False`, not `None`: `schemas.GameSummaryScalars` types it
    as a plain `bool`, and false is the honest value - CR 104.1 cannot be
    judged on a board nobody can read, and the whole point of the engine's
    refusal is that an unreadable state must not be guessed at. It is also
    what `_live_ui_game_ids` counts, so the lobby list and the
    `MAX_LIVE_GAMES` accounting say the same thing about the same game.
    """
    return {
        "game_id": game_id,
        "is_over": False,
        "turn_number": None,
        "active_player": None,
        "phase": None,
        "seat_count": seat_count,
        "unloadable": reason,
    }


def _known_entries(game: Game, paused: Game | None, player: str) -> list[KnownCard]:
    """What `player` knows right now: the committed game's `known_to`, plus
    the records a paused command's half-applied game already holds.

    A command paused on a question keeps no half-applied game (`commands.py`),
    so an effect that showed a player cards before asking them something -
    Thoughtseize revealing a hand before its choice, CR 701.20a keeping it
    revealed "for as long as necessary to complete the parts of the effect
    that card is relevant to" - left nothing in the committed game until the
    answer arrived. `paused` is that half-applied game (`commands.paused_game`),
    read and never committed. A record from it counts only for a card that is
    still that object in the committed game - the same id in the same zone,
    and in a library the same position - so nothing it says can describe a
    card the board no longer holds. The engine decides every record's viewers
    either way; `visibility.redact_view` still hands each seat its own entry.
    """
    entries = {entry.card_id: entry for entry in game.known_to(player)}
    if paused is None:
        return list(entries.values())
    for entry in paused.known_to(player):
        if entry.card_id in entries:
            continue
        card = game.find_by_id(entry.card_id)
        if card is None or card.zone is not entry.zone:
            continue
        if entry.zone is Zone.LIBRARY:
            owner = next((seat for seat in game.players if seat.username == entry.owner), None)
            library = owner.library if owner is not None else []
            position = next((i for i, state in enumerate(library) if state is card), None)
            if position != entry.index_from_top:
                continue
        entries[entry.card_id] = entry
    return list(entries.values())


def _known_cards_view(
    game: Game, images: dict[str, dict[str, str]], paused: Game | None = None
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """What each player knows about hidden cards, keyed by the player who knows.

    Read live from `Game.known_to` - the engine decides what is known and for
    how long (a look is the looker's alone, CR 701.20e; a library reordering
    ends a library record, CR 701.20d; a zone change ends any record,
    CR 400.7) - and this only shapes the answer. Only the hidden zones get a
    list: a hand (CR 402.3), a library with the card's live position
    (CR 401.2), and a sideboard (CR 400.11a). A record of a card sitting in a
    public zone adds nothing to what the board already shows.

    A player's own hand and own sideboard are dropped from that player's
    entry, because they already see them. Their known records about their own
    library are kept: `library_contents` gives no order, so "the top card is
    a Lightning Bolt" is information nothing else carries.

    Every player's entry is built here; `visibility.redact_view` hands each
    viewer only its own. While a command is paused, `paused` is its
    half-applied game and what it already showed counts too (`_known_entries`).
    """
    known: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for player in game.players:
        block: dict[str, list[dict[str, Any]]] = {"hand": [], "library": [], "sideboard": []}
        for entry in _known_entries(game, paused, player.username):
            if entry.zone not in (Zone.HAND, Zone.LIBRARY, Zone.SIDEBOARD):
                continue
            if entry.zone is not Zone.LIBRARY and entry.owner == player.username:
                continue
            card = game.find_by_id(entry.card_id)
            if card is None:
                continue
            card_view = _card_view(game, card, images)
            if entry.zone is Zone.LIBRARY:
                if entry.index_from_top is None:
                    continue
                block["library"].append(
                    {
                        "index_from_top": entry.index_from_top,
                        "source": entry.source,
                        "card": card_view,
                    }
                )
            else:
                block[entry.zone.value].append(card_view)
        block["library"].sort(key=lambda record: record["index_from_top"])
        known[player.username] = block
    return known


def _contents_view(
    game: Game, cards: list[CardState], images: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    """One hidden zone as an unordered multiset: `[{card, count}]`.

    CR 401.2 - "Players can't look at or change the order of cards in a
    library" - is why nothing here carries an id or a position: a player
    knows what went into their own deck, never where any of it sits, and an
    id would let a later view pin a card to a slot. Identical card views
    (id aside) collapse into one entry with a count, sorted by card name and
    then printing identity, so the answer is the same bytes however the
    library is ordered.
    """
    groups: dict[str, dict[str, Any]] = {}
    for card in cards:
        card_view = _card_view(game, card, images)
        del card_view["id"]
        signature = json.dumps(card_view, sort_keys=True, default=str)
        group = groups.get(signature)
        if group is None:
            groups[signature] = {
                "sort": (card.card.name, str(card.card.id), signature),
                "card": card_view,
                "count": 1,
            }
        else:
            group["count"] += 1
    return [
        {"card": group["card"], "count": group["count"]}
        for group in sorted(groups.values(), key=lambda group: group["sort"])
    ]


#: `_paused_overlay`'s answers, keyed by what makes one stale: the game, the
#: standing question's generation (bumped by every new pause), and the
#: committed board the replay started from (`_committed_signature`). A poll
#: repeats the key for as long as one question stands, so the replay runs once
#: per question.
_PAUSED_OVERLAYS: dict[tuple[str, int, str], dict[str, Any]] = {}
_PAUSED_OVERLAYS_LIMIT = 64


def _committed_signature(game: Game) -> str:
    """What an overlay was read against: board-setup routes mutate the
    committed game in place, so a stale overlay must not outlive them."""
    turn = game.turn
    return repr(
        (
            sorted((p.username, sorted(p.counters.root.items())) for p in game.players),
            sorted(
                (str(card.id), sorted(card.counters.root.items()))
                for card in game.shared_zones.get(Zone.BATTLEFIELD, [])
                if isinstance(card, CardState)
            ),
            turn.turn_number if turn is not None else None,
            turn.step.value if turn is not None else None,
            turn.active_index if turn is not None else None,
            game.priority.holder if game.priority is not None else None,
        )
    )


def _paused_overlay(game_id: str, game: Game, paused: Any) -> dict[str, Any]:
    """The turn position, priority holder and counters of the board a paused
    command is standing on, or `{}` when nothing is paused (or the replay no
    longer pauses).

    A pause is never committed (`commands.board_at_pause`), so without this a
    read showed the board from before the command:

    - CR 122.1 puts counters "on an object or player", and CR 107.14's energy is
      one - `{}` while Galvanic Discharge asked how much of the energy it had
      just given to pay (battle 2, B2-16).
    - CR 714.3c adds a Saga's lore counter "as the precombat main phase begins"
      - two Urza's Sagas read `draw`, lore 1, with the precombat main phase's
      chapter triggers being ordered, and the priority holder was the seat
      whose pass began that step (battle 3, B3-08), so a helper posted a pass
      the engine refused.
    """
    if paused is None:
        return {}
    command, tape, _ = paused
    key = (game_id, store.pause_generation(game_id), _committed_signature(game))
    cached = _PAUSED_OVERLAYS.get(key)
    if cached is None:
        board = commands.board_at_pause(game, command, tape)
        cached = {}
        if board is not None:
            turn = board.turn
            cached = {
                "player_counters": {
                    player.username: dict(player.counters.root) for player in board.players
                },
                "card_counters": {
                    str(card.id): dict(card.counters.root)
                    for card in board.shared_zones.get(Zone.BATTLEFIELD, [])
                    if isinstance(card, CardState)
                },
                "turn_number": turn.turn_number if turn is not None else None,
                "turns_taken": turn.turns_taken if turn is not None else None,
                "step": turn.step if turn is not None else None,
                # The paused replay board's own read of CR 510.4/506.1 - the
                # question stands in the first-strike step exactly when that
                # board does, regardless of what the committed board (still
                # sitting before the paused command) would answer.
                "first_strike_damage_step": board.is_first_strike_damage_step,
                "active_player": turn.players[turn.active_index] if turn is not None else None,
                "priority_holder": board.priority.holder if board.priority is not None else None,
            }
        if len(_PAUSED_OVERLAYS) >= _PAUSED_OVERLAYS_LIMIT:
            _PAUSED_OVERLAYS.clear()
        _PAUSED_OVERLAYS[key] = cached
    return cached


def state_view(game: Game, game_id: str, *, include_contents: bool = False) -> dict[str, Any]:
    """T1: this dict's shape is formalised as `schemas.GameStateView` - see
    that class and the "Response views" section it opens for the field-by-
    field mapping, and `app.py`'s `response_model=` declarations for where it
    is enforced. Nothing here changes to accommodate that: every route still
    gets back the same plain dict `state_view`/`visibility.redact_view`
    always built, and FastAPI validates/documents it against the model on
    the way out.

    None of this dict's own keys turned out to be dynamically keyed in a way
    a single model cannot express - `hands`/`graveyards`/`libraries`/
    `sideboards`/`play_permissions` are all `dict[str, list[...]]` keyed by
    player username, which OpenAPI's `additionalProperties` schema states
    exactly. What *does* resist a single fixed shape is `visibility
    .redact_view`'s own doing, not this function's: it can pop `libraries`/
    `sideboards` outright, shrink `pending_decision` to `{player, kind,
    prompt}`, and replace a face-down card with a smaller stub - `schemas
    .GameStateView`/`PendingDecisionView`/`CardView` model that by making
    every field a redaction step can remove `| None = None` rather than by
    reaching for `dict[str, Any]`.
    """
    # While an announcement is paused, the board is the one the question was
    # raised over, not the committed one from before it began: a cast
    # trigger's target question comes after CR 601.2a put the card on the
    # stack and CR 601.2h paid its cost (B2-13, `commands.paused_board`).
    # Nothing is committed by reading it. Resolution and turn-based pauses
    # keep the committed board's turn position from `game`, but take the paused
    # board's step, turn, priority holder and counters (`_paused_overlay`, B2-16
    # and B3-08): the question stands in the step it was asked in.
    #
    # They also show their objects where they stand at the question: CR 608.2c
    # follows a resolution's instructions in order, so Overlord of the
    # Balemurk's "mill four cards, then you may return ..." asks with the four
    # already in the graveyard (battle 5, B5-03). `board` supplies the zones and
    # the players; `game` the combat and the stack - CR 608.2n removes a
    # resolving ability from the stack only as the final part of its
    # resolution, and the replay has already popped it.
    paused = store.paused_command(game_id)
    committed = game
    board = game
    if paused is not None and paused[0].kind in commands.ANNOUNCING_KINDS:
        game = board = commands.paused_board(game, *paused) or game
    elif paused is not None:
        board = commands.board_at_pause(game, paused[0], paused[1]) or game
    overlay = _paused_overlay(game_id, committed, paused) if game is committed else {}
    images = _images_for(board) if board is game else {**_images_for(game), **_images_for(board)}
    # Replayed before any board is read, never part-way through the view: a
    # replay runs a command on a copy, and a change announced while a board
    # snapshot is open drops the board every later read would have shared.
    known_paused = paused_game(committed, paused[0], paused[1]) if paused is not None else None
    return read_on_one_board(
        lambda: _state_view_on(
            game_id,
            game,
            board,
            paused,
            overlay,
            images,
            known_paused,
            include_contents=include_contents,
        ),
        game,
        board,
    )


def frame_view(game: Game, game_id: str, *, you: str | None) -> dict[str, Any]:
    """R2's post-game replay frame: `game` exactly as `store.record_state`
    decoded it for one `records` row, viewed the same way `state_view`
    builds any other board - `_state_view_on` on one layered board
    (`read_on_one_board`) - but with no paused-command or pause-generation
    lookup, because a recorded state is never mid-pause: `put_record_state`
    is called with the game `audit.record` already read back from the store
    right after a command committed, and a paused command's own question is
    a separate, live-only concern (`state_view`'s `paused`/`overlay`/
    `known_paused`) that a historical frame has no analogue for. `game` is
    passed as both the turn/stack/combat game and the zone/player board -
    there is no separate "paused board" here, unlike `state_view`.

    **Omniscient by design, exactly like `app.game_review`'s own entries**:
    this is reachable only from behind `app._review_access`'s post-game
    gate, so both hands are left in full rather than redacted through
    `visibility.redact_view` - there is no hidden information left to
    protect once a game is over, and `game_review` already shows both
    seats' plans and reasoning side by side for the same reason.
    `libraries`/`sideboards` are still popped (CR 401.2: nobody may look at
    a library, not even their own - the same field `redact_view` always
    drops), but the counts on each player's own block are untouched. `you`
    is the viewing seat's own username, or `None` for the admin key - set
    directly here rather than by `redact_view`, since that function's own
    redaction (blanking the opponent's hand) is exactly what this view must
    not apply.
    """
    view = read_on_one_board(
        lambda: _state_view_on(
            game_id, game, game, None, {}, _images_for(game), None, include_contents=False
        ),
        game,
    )
    view = dict(view)
    view.pop("libraries", None)
    view.pop("sideboards", None)
    view["you"] = you
    return view


def _state_view_on(
    game_id: str,
    game: Game,
    board: Game,
    paused: Any,
    overlay: dict[str, Any],
    images: dict[str, dict[str, str]],
    known_paused: Game | None,
    *,
    include_contents: bool,
) -> dict[str, Any]:
    """`state_view`'s view of `game` (turn, stack, combat) and `board` (zones,
    players), read on one layered board each (`board_scope`)."""

    def views(cards: list[StackObject]) -> list[dict[str, Any]]:
        return [_card_view(board, state, images) for state in cards if isinstance(state, CardState)]

    turn = game.turn
    step = turn.step if turn is not None else Step.UNTAP
    turn_number = turn.turn_number if turn is not None else None
    turns_taken = turn.turns_taken if turn is not None else None
    active_player = turn.players[turn.active_index] if turn is not None else None
    priority_holder = game.priority.holder if game.priority is not None else None
    # CR 510.4/506.1: the engine keeps one `Step.COMBAT_DAMAGE` for both
    # occurrences of the combat damage step - `Game.is_first_strike_damage_
    # step`'s own docstring - so this flag, not `step` itself, is what tells a
    # viewer which one `game` is sitting in.
    first_strike_damage_step = game.is_first_strike_damage_step
    if overlay:
        step = overlay["step"] or step
        turn_number = overlay["turn_number"]
        turns_taken = overlay["turns_taken"]
        active_player = overlay["active_player"]
        priority_holder = overlay["priority_holder"]
        first_strike_damage_step = overlay.get("first_strike_damage_step", False)
    # The pending decision a paused command is waiting to resume, if any -
    # read from the repository's own paused-command slot (`commands.py`,
    # `repository.store_paused_command`) rather than a live mailbox: a
    # mutating request that crosses an unanswered question pauses and returns
    # immediately, and this is what a state read shows for it until
    # `POST .../decide` resumes it.
    pending = (
        PendingDecision.from_engine(paused[2].decision, store.pause_generation(game_id))
        if paused is not None
        else None
    )
    # The counters that question is asked with (`_paused_overlay`): `committed`
    # is the board from before the paused command, which never shows Galvanic
    # Discharge's three energy while it asks how many to pay.
    paused_counters = overlay.get("player_counters", {})
    battlefield_views = views(board.shared_zones.get(Zone.BATTLEFIELD, []))
    for card_view in battlefield_views:
        card_counters = overlay.get("card_counters", {}).get(card_view["id"])
        if card_counters is not None:
            card_view["counters"] = card_counters
    exile_views = views(board.shared_zones.get(Zone.EXILE, []))
    # CR 500.1 makes a phase part of a turn, and the engine's two pregame steps
    # (`Step.MULLIGAN`, `Step.PREGAME_ACTIONS`) run before CR 103.8's first
    # turn - so they are in no phase and are deliberately absent from
    # `PHASE_OF`. `phase` is therefore nullable, and a client that assumes a
    # string here must handle `null` (see `ui/app.js`'s phase track).
    phase = PHASE_OF.get(step)
    view = {
        "game_id": game_id,
        "pending_decision": pending.to_view() if pending is not None else None,
        "turn_number": turn_number,
        # S1 review fix 3: `turn_number` counts rounds of the table and
        # deliberately does not move for an extra turn (CR 500.7,
        # `machines/turn.py`'s own `TurnMachine.turns_taken` docstring) - a
        # client that needs "did the turn actually change, extra turns
        # included" (the play-script already-ran filter, `script_tools.
        # _record_run`/`applicable_hint`) must read this instead.
        "turns_taken": turns_taken,
        "step": step.value,
        "first_strike_damage_step": first_strike_damage_step,
        "phase": phase.value if phase is not None else None,
        # What CR 103.5/CR 103.6 are waiting for, or `None` once the game has
        # reached its first turn - see `opening.pregame_view`.
        "pregame": pregame_view(game, game_id),
        "active_player": active_player,
        "priority_holder": priority_holder,
        # CR 723.5: controlled player -> the player making their decisions this
        # turn. CR 723.3 leaves `priority_holder` on the controlled player, so
        # this is what says whose move a held priority actually is.
        "player_control": player_control.controllers(board),
        "players": [
            _player_view(board, player, paused_counters.get(player.username))
            for player in board.players
        ],
        "battlefield": battlefield_views,
        "stack": _stack_view(game, images),
        "exile": exile_views,
        # CR 406.4's marked piles - suspended cards (with their time counters,
        # via each card's own `counters`), foretold and plotted cards - pulled
        # out of `exile` so a seat does not have to scan the whole pile to
        # find what it may still act on. Cascade's own hit never sits here
        # any more (battle 8, B8-01): accepting it casts the card immediately
        # while the trigger is still resolving, so it never comes to rest in
        # exile at all. Every entry here is already a full `exile` card view;
        # `exile_pile` is a filtered *view* of `exile`, not a second source of
        # truth.
        "exile_pile": [card for card in exile_views if card.get("exile_pile") is not None],
        # CR 601.3's dynamic grants - see `_play_permissions_view`. Keyed by
        # the player who may use it; `POST .../actions/cast` (no `keyword`
        # needed) and `POST .../actions/play-land` already drive
        # `Casting.use_permission()` for a card sitting in the permission's
        # own zone, so this is purely the discoverability half of the gap.
        "play_permissions": _play_permissions_view(board, images),
        "command": views(board.shared_zones.get(Zone.COMMAND, [])),
        "hands": {player.username: views(player.hand) for player in board.players},
        "graveyards": {player.username: views(player.graveyard) for player in board.players},
        "libraries": {player.username: views(player.library) for player in board.players},
        "sideboards": {player.username: views(player.sideboard) for player in board.players},
        # A sibling of `hands`, never inside it - see `_known_cards_view`.
        "known_cards": _known_cards_view(board, images, known_paused),
        "winner": game.winner,
        "drawn": game.drawn,
        "losers": list(game.losers),
        "conceded": list(game.conceded),
        "combat": {
            "attacks": [
                {
                    "attacker": str(attack.attacker_id),
                    "defender": str(attack.defender),
                    "blockers": [str(blocker) for blocker in attack.blockers],
                }
                for attack in game.combat.attacks
            ]
        },
    }
    if include_contents:
        # Only on request (`GET /api/games/{id}?include=contents`): a seat's
        # own deck read back without its order, which nothing polling the
        # board needs on every read - see `_contents_view`.
        view["library_contents"] = {
            player.username: _contents_view(board, player.library, images)
            for player in board.players
        }
        view["sideboard_contents"] = {
            player.username: _contents_view(board, player.sideboard, images)
            for player in board.players
        }
    return view
