"""The full state view: engine dump plus per-card detail and Scryfall images."""

from __future__ import annotations

from typing import Any

from magic_cards_model.machines import AbilityOnStack, Step
from magic_cards_model.machines.turn import PHASE_OF
from magic_cards_model.models import CardState, Game, Player, Zone

from . import scryfall
from .engine_api import list_abilities
from .store import store

StackObject = CardState | AbilityOnStack


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
    return {
        "id": str(state.id),
        "name": state.name,
        "card_name": state.card.name,
        "type_line": state.card.type_line,
        "mana_cost": state.card.mana_cost,
        "oracle_text": state.card.oracle_text,
        "zone": state.zone.value,
        "controller": state.controller,
        "owner": state.owner,
        "tapped": state.tapped,
        "face_up": state.face_up,
        "counters": dict(state.counters.root),
        "damage": state.damage,
        "summoning_sick": state.is_summoning_sick,
        "power": state.card.power,
        "toughness": state.card.toughness,
        "targets": _target_names(game, state.targets),
        "image": image.get("normal"),
        "small_image": image.get("small"),
        "art_crop": image.get("art_crop"),
        "abilities": list_abilities(game, state),
    }


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
        if state.is_summoning_sick:
            bits.append("(sick)")
        if state.card.power is not None:
            bits.append(f"[{state.card.power}/{state.card.toughness}]")
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
