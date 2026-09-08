"""The full state view: engine dump plus per-card detail and Scryfall images."""

from __future__ import annotations

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

from . import scryfall
from .affordances import cast_hints
from .agent_settings import AgentSettings
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
    return {
        "marker": annotation.marker.value,
        "owner": annotation.owner,
        "turn_exiled": annotation.turn_exiled,
    }


def _cast_option_view(
    game: Game,
    spell: CardState,
    option: KeywordCastOption,
    caster: str,
    granted_by: str | None = None,
) -> dict[str, Any]:
    """One `KeywordCastOption` (`machines/casting.py`, CR 601.3's "a rule or
    effect allows that player to cast it") this graveyard or exile card
    carries right now, shaped so a seat can drive `POST .../actions/cast`
    with `keyword=` set to exactly this `keyword` value.

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


def _cast_options_of(game: Game, state: CardState) -> list[dict[str, Any]]:
    """Every `_cast_option_view` this graveyard or exile card has right now,
    or `[]` for anything else (a card in any other zone is cast the ordinary
    way, or - if in hand - is covered by `_special_actions` instead) and for
    a card with no controller or owner recorded (nobody to be the `caster`
    `KeywordCastOption.why_not` asks about).

    Three sources, matching `Casting._use_keyword`'s own
    `printed_options + marker_options + granted_options` (the exact set CR
    601.2b lets an announcement pick from), so a keyword added to any of them
    shows up here for free rather than being re-listed by hand:

    - `castable_from(state.card, state.zone)` - the card's own printed
      keywords whose zone matches where it is sitting (CR 604.1's static
      abilities, e.g. CR 702.34a flashback, CR 702.187a mayhem, CR 702.170a
      plot, CR 702.143a foretell, CR 702.35a madness) plus a printed CR 604.6
      sentence opening an extra zone for one of them (CR 113.6f).
    - `marker_cast_options(state)` - the permission a CR 406.4 exile pile
      itself grants rather than the card's own text (CR 701.65a's airbend).
    - `granted_cast_options` - a keyword another permanent's static ability
      is granting this card right now (CR 610.5's "as you cast it", or CR
      611.3d's "while it sits in the zone", both kept alive on the object by
      CR 400.7b/400.7g once it is on the stack).

    `caster` is this card's own controller-or-owner - the same fallback
    `_special_actions` uses - because every option here is a permission to
    a *player*, and the only player who could ever hold it for a card sitting
    in a graveyard or exile is whoever's card it is.
    """
    if state.zone not in (Zone.GRAVEYARD, Zone.EXILE):
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


def _card_view(game: Game, state: CardState, images: dict[str, dict[str, str]]) -> dict[str, Any]:
    image = images.get(state.card.name, {})
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
        "oracle_text": state.card.oracle_text,
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
        "keywords": _keywords_of(game, state),
        "targets": _target_names(game, state.targets),
        "target_refs": _target_refs(game, state.targets),
        "image": image.get("normal"),
        "small_image": image.get("small"),
        "art_crop": image.get("art_crop"),
        "abilities": list_abilities(game, state),
    }
    # CR 614.12a's "as this enters, choose ..." answer, recorded on the object
    # that carries it - Pithing Needle's and Disruptor Flute's named card,
    # Cavern of Souls' creature type, Utopia Sprawl's colour. Without this the
    # board shows a permanent whose whole function is the name it chose and
    # never says which name that was.
    if state.choice is not None:
        view["choice"] = {"kind": state.choice.kind.value, "value": state.choice.value}
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
    if state.zone in (Zone.GRAVEYARD, Zone.EXILE):
        cast_options = _cast_options_of(game, state)
        if cast_options:
            # Own-view only: `visibility.redact_view` strips this back out of
            # a card whose owner/controller is not the viewer, the same shape
            # `_special_actions` already gets for free by only existing on a
            # hidden hand - see `visibility._redact_cast_options`.
            view["cast_options"] = cast_options
    if state.zone is Zone.BATTLEFIELD:
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


def _player_view(
    player: Player, decklist: str | None = None, agent: AgentSettings | None = None
) -> dict[str, Any]:
    """One seat's own block.

    `decklist` is the raw decklist text this seat was booted from
    (`store.register_decklists`), and it is the one field here that is not
    public: CR 100.2a's constructed play is "a way of playing in which each
    player creates their own deck ahead of time", so a seat knows its own list
    and nothing entitles it to the opponent's - CR 401.1/401.2 make that deck
    a library nobody may look at once the game begins. `visibility.redact_view`
    is what enforces it; this function reports the full truth, exactly as it
    already does for `hands`. `None` for a game built board-first, which has no
    decklist to report.

    `agent` is that seat's `AgentSettings` (`store.register_agent_settings`) -
    present only for an AI seat, absent for a human one, same as `_ai_players`.
    Not secret the way `decklist` is: which model, effort and thinking budget
    play a bot seat is not hidden information under any CR rule, so it is
    reported on the player block itself rather than filtered per-viewer.
    """
    view = {
        "username": player.username,
        "life": player.life,
        "counters": dict(player.counters.root),
        "mana_pool": {
            "floating": {color.value: count for color, count in player.mana_pool.floating.items()},
            "snow": {color.value: count for color, count in player.mana_pool.snow.items()},
        },
        "hand_count": len(player.hand),
        "library_count": len(player.library),
        "graveyard_count": len(player.graveyard),
        "sideboard_count": len(player.sideboard),
        "maximum_hand_size": player.maximum_hand_size,
    }
    if decklist is not None:
        view["decklist"] = decklist
    if agent is not None:
        view["agent"] = agent.model_dump()
    return view


def snapshot(game: Game) -> dict[str, Any]:
    """A compact, full-information snapshot for the audit log.

    The log is the debugger's eye over a finished or running game, so nothing
    is hidden here: both players' hands, the board with tap/damage/counter
    state, the stack with each object's controller and targets, graveyards,
    exile, life, and pools. Cards are one readable string each, so a whole
    game's log stays digestible.
    """

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
        return " ".join(bits)

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

    battlefield = [
        battlefield_line(state)
        for state in game.shared_zones.get(Zone.BATTLEFIELD, [])
        if isinstance(state, CardState)
    ]
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
            state.name
            for state in game.shared_zones.get(Zone.EXILE, [])
            if isinstance(state, CardState)
        ],
        "combat": combat_lines,
    }


def state_view(game: Game, game_id: str) -> dict[str, Any]:
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
    decklists = store.decklists(game_id)
    agent_settings = store.agent_settings(game_id)
    # CR 500.1 makes a phase part of a turn, and the engine's two pregame steps
    # (`Step.MULLIGAN`, `Step.PREGAME_ACTIONS`) run before CR 103.8's first
    # turn - so they are in no phase and are deliberately absent from
    # `PHASE_OF`. `phase` is therefore nullable, and a client that assumes a
    # string here must handle `null` (see `ui/app.js`'s phase track).
    phase = PHASE_OF.get(step)
    return {
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
        "players": [
            _player_view(
                player, decklists.get(player.username), agent_settings.get(player.username)
            )
            for player in game.players
        ],
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
