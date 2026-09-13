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

from . import assets, scryfall
from .affordances import cast_hints
from .commands import paused_game
from .decisions import PendingDecision
from .engine_api import list_abilities
from .opening import pregame_view
from .store import store

StackObject = CardState | AbilityOnStack


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

    A real game already hit the gap this closes: cascade offering "you may
    cast Living End without paying its mana cost" was accepted, the engine
    granted the permission, and nothing in the state a seat reads ever named
    it - the card just sat in exile until cleanup bottomed it, unusable
    though `POST .../actions/cast` (with no `keyword` needed - see
    `engine_api.cast_card`'s exile-zone branch) would have driven it straight
    through.

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
        "bottom_if_unused": permission.bottom_if_unused,
        "usable_now": reason is None,
        "why_not": reason,
    }


def _play_permissions_view(
    game: Game, images: dict[str, dict[str, str]]
) -> dict[str, list[dict[str, Any]]]:
    """Every live `PlayPermission`, grouped by the player who may use it.

    Not secret: every permission this engine actually grants today (cascade,
    suspend's own third ability) names a card that is already visible in a
    public zone (CR 400.2's exile), so there is nothing here for
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
    printing's combined `Front // Back` line, which is neither face."""
    faces = state.card.card_faces or []
    if state.card.layout not in _DOUBLE_FACED_LAYOUTS or len(faces) < 2:
        return None
    return [
        {
            "name": face.name,
            "mana_cost": face.mana_cost,
            "type_line": face.type_line,
            "oracle_text": face.oracle_text,
        }
        for face in faces
    ]


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
        "mana_cost": state.card.mana_cost,
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
        printed_type_line = state.card.type_line
        printed_power, printed_toughness = state.power, state.toughness
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


def _player_view(game: Game, player: Player) -> dict[str, Any]:
    """One seat's own block.

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
        "counters": dict(player.counters.root),
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
    images = _images_for(game)

    def views(cards: list[StackObject]) -> list[dict[str, Any]]:
        return [_card_view(game, state, images) for state in cards if isinstance(state, CardState)]

    turn = game.turn
    step = turn.step if turn is not None else Step.UNTAP
    # The pending decision a paused command is waiting to resume, if any -
    # read from the repository's own paused-command slot (`commands.py`,
    # `repository.store_paused_command`) rather than a live mailbox: a
    # mutating request that crosses an unanswered question pauses and returns
    # immediately, and this is what a state read shows for it until
    # `POST .../decide` resumes it.
    paused = store.paused_command(game_id)
    pending = (
        PendingDecision.from_engine(paused[2].decision, store.pause_generation(game_id))
        if paused is not None
        else None
    )
    exile_views = views(game.shared_zones.get(Zone.EXILE, []))
    # CR 500.1 makes a phase part of a turn, and the engine's two pregame steps
    # (`Step.MULLIGAN`, `Step.PREGAME_ACTIONS`) run before CR 103.8's first
    # turn - so they are in no phase and are deliberately absent from
    # `PHASE_OF`. `phase` is therefore nullable, and a client that assumes a
    # string here must handle `null` (see `ui/app.js`'s phase track).
    phase = PHASE_OF.get(step)
    view = {
        "game_id": game_id,
        "pending_decision": pending.to_view() if pending is not None else None,
        "turn_number": turn.turn_number if turn is not None else None,
        "step": step.value,
        "phase": phase.value if phase is not None else None,
        # What CR 103.5/CR 103.6 are waiting for, or `None` once the game has
        # reached its first turn - see `opening.pregame_view`.
        "pregame": pregame_view(game),
        "active_player": turn.players[turn.active_index] if turn is not None else None,
        "priority_holder": game.priority.holder if game.priority is not None else None,
        "players": [_player_view(game, player) for player in game.players],
        "battlefield": views(game.shared_zones.get(Zone.BATTLEFIELD, [])),
        "stack": _stack_view(game, images),
        "exile": exile_views,
        # CR 406.4's marked piles - suspended cards (with their time counters,
        # via each card's own `counters`), foretold and plotted cards, and a
        # cascade hit awaiting its cast - pulled out of `exile` so a seat does
        # not have to scan the whole pile to find what it may still act on.
        # Every entry here is already a full `exile` card view; `exile_pile`
        # is a filtered *view* of `exile`, not a second source of truth.
        "exile_pile": [card for card in exile_views if card.get("exile_pile") is not None],
        # CR 601.3's dynamic grants - see `_play_permissions_view`. Keyed by
        # the player who may use it; `POST .../actions/cast` (no `keyword`
        # needed) and `POST .../actions/play-land` already drive
        # `Casting.use_permission()` for a card sitting in the permission's
        # own zone, so this is purely the discoverability half of the gap.
        "play_permissions": _play_permissions_view(game, images),
        "command": views(game.shared_zones.get(Zone.COMMAND, [])),
        "hands": {player.username: views(player.hand) for player in game.players},
        "graveyards": {player.username: views(player.graveyard) for player in game.players},
        "libraries": {player.username: views(player.library) for player in game.players},
        "sideboards": {player.username: views(player.sideboard) for player in game.players},
        # A sibling of `hands`, never inside it - see `_known_cards_view`.
        "known_cards": _known_cards_view(
            game, images, paused_game(game, paused[0], paused[1]) if paused is not None else None
        ),
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
            player.username: _contents_view(game, player.library, images) for player in game.players
        }
        view["sideboard_contents"] = {
            player.username: _contents_view(game, player.sideboard, images)
            for player in game.players
        }
    return view
