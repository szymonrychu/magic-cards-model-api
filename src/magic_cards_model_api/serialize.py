"""The full state view: engine dump plus per-card detail and Scryfall images."""

from __future__ import annotations

from typing import Any

from magic_cards_model.machines import AbilityOnStack, Step
from magic_cards_model.machines.abilities import keywords_in_lines
from magic_cards_model.machines.combat import Keyword
from magic_cards_model.machines.layers import Characteristics
from magic_cards_model.machines.turn import PHASE_OF
from magic_cards_model.models import CardState, Game, Player, Supertype, Type, Zone

from . import scryfall
from .engine_api import list_abilities
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


def _images_for(game: Game) -> dict[str, dict[str, str]]:
    """Card name -> image links, out of the Scryfall cache. Best effort."""
    images: dict[str, dict[str, str]] = {}
    for state in _all_cards(game):
        name = state.card.name
        if name in images:
            continue
        raw = scryfall.load_cached(name)
        images[name] = scryfall.image_uris(raw) if raw else {}
    return images


def _all_cards(game: Game) -> list[CardState]:
    cards: list[CardState] = []
    for zone_cards in game.shared_zones.values():
        cards.extend(state for state in zone_cards if isinstance(state, CardState))
    for player in game.players:
        for zone_cards in player.zones.values():
            cards.extend(zone_cards)
    return cards


def _target_names(game: Game, targets: list[Any]) -> list[str]:
    from magic_cards_model.models import Player as PlayerModel

    names = []
    for ref in targets:
        try:
            thing = game.resolve_target(ref)
        except Exception:
            thing = None
        if isinstance(thing, CardState):
            names.append(thing.name)
        elif isinstance(thing, PlayerModel):
            names.append(thing.username)
        elif isinstance(thing, AbilityOnStack):
            names.append(f"ability from {thing.source_name}")
        else:
            names.append(str(ref))
    return names


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
        "image": image.get("normal"),
        "small_image": image.get("small"),
        "art_crop": image.get("art_crop"),
        "abilities": list_abilities(game, state),
    }
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
                "zone": "stack",
            }
        views.append(view)
    return views


def _player_view(player: Player) -> dict[str, Any]:
    return {
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
            }
        return {
            "name": obj.source_name,
            "kind": "ability",
            "controller": obj.controller,
            "targets": _target_names(game, obj.targets),
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
    images = _images_for(game)

    def views(cards: list[StackObject]) -> list[dict[str, Any]]:
        return [_card_view(game, state, images) for state in cards if isinstance(state, CardState)]

    turn = game.turn
    step = turn.step if turn is not None else Step.UNTAP
    mailbox = store.mailbox(game_id)
    pending = mailbox.snapshot() if mailbox is not None else None
    return {
        "game_id": game_id,
        "pending_decision": pending.to_view() if pending is not None else None,
        "decision_timeouts": store.decision_timeouts(game_id),
        "turn_number": turn.turn_number if turn is not None else None,
        "step": step.value,
        "phase": PHASE_OF[step].value,
        "active_player": turn.players[turn.active_index] if turn is not None else None,
        "priority_holder": game.priority.holder if game.priority is not None else None,
        "players": [_player_view(player) for player in game.players],
        "battlefield": views(game.shared_zones.get(Zone.BATTLEFIELD, [])),
        "stack": _stack_view(game, images),
        "exile": views(game.shared_zones.get(Zone.EXILE, [])),
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
