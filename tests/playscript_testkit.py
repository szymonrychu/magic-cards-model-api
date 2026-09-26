"""Shared scaffolding for the play-script tests.

The two worked examples from the approved architecture (D2), a small card
cache, a tmp guides root shaped like mtg-decks, and CardView / redacted state
view builders. The api repo is tested by ordinary practice, so the views here
are hand-built dicts in exactly the shape `serialize.state_view` followed by
`visibility.redact_view` emits; the real-game tests in
`test_playscript_conditions.py` prove that shape against the live serializer.
"""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
from typing import Any

SLUG = "boros-aggro"
MATCHUP_KEYS = ("dimir-frog", "living-end")

FETCH_SHOCK: dict[str, Any] = {
    "schema_version": 1,
    "id": "fetch-untapped-shock",
    "name": "Crack a fetch for an untapped shockland",
    "intent": "Thin and fix on my main phase when life is healthy.",
    "priority": 60,
    "opposing_archetypes": ["*"],
    "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
    "true": [
        {
            "kind": "count",
            "zone": "battlefield",
            "side": "me",
            "op": ">=",
            "n": 1,
            "select": {
                "types_all": ["Land"],
                "tapped": False,
                "ability_text_has": "Search your library for",
            },
        },
        {"kind": "life", "side": "me", "op": ">=", "n": 8},
    ],
    "false": [
        {"kind": "zone_size", "zone": "library", "side": "me", "op": "<=", "n": 5},
        {"kind": "stack_top", "controller": "opponent", "select": {}},
    ],
    "moves": [
        {
            "kind": "activate",
            "source": {
                "zone": "battlefield",
                "select": {"types_all": ["Land"], "ability_text_has": "Search your library for"},
            },
            "ability_text_has": "Search your library for",
            "sacrifice": "source",
            "answers": [
                {
                    "decision": "choose_cards",
                    "pick": {
                        "select": {"oracle_has": "you may pay 2 life"},
                        "count": 1,
                        "prefer": [{"subtypes_any": ["Mountain"]}],
                    },
                },
                {"decision": "pay_life", "accept": True},
            ],
        }
    ],
    "next": [],
}

FETCH_SURVEIL: dict[str, Any] = {
    "schema_version": 1,
    "id": "fetch-surveil-land",
    "name": "Fetch a surveil land at their end step",
    "intent": "Use the fetch on their turn and filter the top card.",
    "priority": 50,
    "opposing_archetypes": ["*"],
    "when": {"whose_turn": "theirs", "steps": ["end_step"], "stack": "empty"},
    "true": [
        {
            "kind": "count",
            "zone": "battlefield",
            "side": "me",
            "op": ">=",
            "n": 1,
            "select": {
                "types_all": ["Land"],
                "tapped": False,
                "ability_text_has": "Search your library for",
            },
        },
        {"kind": "life", "side": "me", "op": ">=", "n": 6},
    ],
    "false": [
        {
            "kind": "count",
            "zone": "battlefield",
            "side": "me",
            "op": ">=",
            "n": 3,
            "select": {"keywords_any": ["Surveil"], "types_all": ["Land"]},
        }
    ],
    "moves": [
        {
            "kind": "activate",
            "source": {
                "zone": "battlefield",
                "select": {"types_all": ["Land"], "ability_text_has": "Search your library for"},
            },
            "ability_text_has": "Search your library for",
            "sacrifice": "source",
            "answers": [
                {
                    "decision": "choose_cards",
                    "pick": {
                        "select": {"types_all": ["Land"], "keywords_any": ["Surveil"]},
                        "count": 1,
                    },
                },
                {"decision": "surveil", "to_graveyard": {"types_any": ["Land"]}},
            ],
        }
    ],
    "next": [],
}


#: A permanent castable at instant speed, for the capability proof (T18).
#: The card is chosen for being a legal flash permanent with a plain cost and
#: nothing else: no enter trigger to answer, no target to choose, no cost
#: choice. Nothing in the proof is about this card, and nothing asserts
#: anything specific to it - swap the printing and every test still reads.
FLASH_PERMANENT = "Containment Priest"


def flash_permanent_play(
    play_id: str,
    *,
    whose_turn: str = "theirs",
    steps: tuple[str, ...] = ("end_step",),
    stack: str = "empty",
    true: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A minimal play that casts a permanent at instant speed.

    The selector names the permanent by CHARACTERISTIC - a nonland permanent
    card with the flash keyword - never by name, the same discipline every
    other selector in this schema keeps."""
    return {
        "schema_version": 1,
        "id": play_id,
        "name": "Cast a permanent at instant speed",
        "intent": "Hold the permanent and commit it in the latest legal window.",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": whose_turn, "steps": list(steps), "stack": stack},
        "true": list(true or []),
        "false": [],
        "moves": [
            {
                "kind": "cast",
                "card": {
                    "zone": "hand",
                    "select": {"keywords_any": ["Flash"], "types_any": ["Creature"]},
                },
                "mana": "auto",
            }
        ],
        "next": [],
    }


def play(**over: Any) -> dict[str, Any]:
    """The fetch-shock worked example with top-level fields replaced."""
    base = copy.deepcopy(FETCH_SHOCK)
    base.update(over)
    return base


def advice_play(base: dict[str, Any], **over: Any) -> dict[str, Any]:
    """`base` (any registered play, real or a worked example), deep-copied
    and turned advice-only: `moves`/`next` forced empty, `lint._advice_only`'s
    own reading of "commits nothing". `over` replaces anything else - at
    least `id` (and usually `name`), since an advice-only sibling must never
    reuse the move-bearing play's own id in the same corpus."""
    doc = copy.deepcopy(base)
    doc.update(over, moves=[], next=[])
    return doc


# -- the card cache ------------------------------------------------------------

_FETCH = (
    "{T}, Pay 1 life, Sacrifice this land: Search your library for a %s or %s card, "
    "put it onto the battlefield, then shuffle."
)
_SHOCK = (
    "({T}: Add {%s} or {%s}.)\n"
    "As this land enters, you may pay 2 life. If you don't, it enters tapped."
)

CARDS: dict[str, dict[str, str]] = {
    "Arid Mesa": {"type_line": "Land", "oracle_text": _FETCH % ("Mountain", "Plains")},
    "Scalding Tarn": {"type_line": "Land", "oracle_text": _FETCH % ("Island", "Mountain")},
    "Wooded Foothills": {"type_line": "Land", "oracle_text": _FETCH % ("Mountain", "Forest")},
    "Sacred Foundry": {"type_line": "Land — Mountain Plains", "oracle_text": _SHOCK % ("R", "W")},
    "Blood Crypt": {"type_line": "Land — Swamp Mountain", "oracle_text": _SHOCK % ("B", "R")},
    "Stomping Ground": {"type_line": "Land — Mountain Forest", "oracle_text": _SHOCK % ("R", "G")},
    "Mountain": {"type_line": "Basic Land — Mountain", "oracle_text": "({T}: Add {R}.)"},
    "Lightning Bolt": {
        "type_line": "Instant",
        "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    },
    "Undercity Sewers": {
        "type_line": "Land — Island Swamp",
        "oracle_text": (
            "({T}: Add {U} or {B}.)\nThis land enters tapped.\nWhen this land enters, surveil 1."
        ),
    },
}


def make_root(
    tmp_path: Path,
    *,
    cards: dict[str, dict[str, str]] | None = None,
    matchups: tuple[str, ...] = MATCHUP_KEYS,
    slug: str = SLUG,
) -> Path:
    """A tmp guides root: `meta/card-data.json` plus one deck with a plays dir
    and a dated analysis run holding `matchups`."""
    (tmp_path / "meta").mkdir(parents=True, exist_ok=True)
    (tmp_path / "meta" / "card-data.json").write_text(
        json.dumps({"cards": CARDS if cards is None else cards}), encoding="utf-8"
    )
    deck = tmp_path / "decks" / slug
    (deck / "plays").mkdir(parents=True, exist_ok=True)
    run = deck / "analysis" / f"{slug}-modern-2026-09-10" / "domain3-matchup-threat" / "matchups"
    run.mkdir(parents=True, exist_ok=True)
    for stem in matchups:
        (run / f"{stem}.json").write_text("{}", encoding="utf-8")
    return tmp_path


def put(root: Path, doc: Any, *, slug: str = SLUG, stem: str | None = None) -> Path:
    stem = stem if stem is not None else doc["id"]
    path = root / "decks" / slug / "plays" / f"{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


def put_inputs(root: Path, doc: Any, *, slug: str = SLUG) -> Path:
    path = root / "decks" / slug / "plays" / "inputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


# -- CardView and redacted state view builders ----------------------------------

_ids = itertools.count(1)

FETCH_TEXT = _FETCH % ("Mountain", "Plains")


def ability(text: str, *, mana: bool = False) -> dict[str, Any]:
    return {
        "index": 0,
        "text": text,
        "mana": mana,
        "cost": None,
        "needs_x": False,
        "is_mana_ability": mana,
        "mana_amount": 1 if mana else None,
        "mana_cost_value": 0 if mana else None,
        "targeting": {"targets_anything": False, "understood": True, "requirements": []},
    }


def card(
    name: str = "Card",
    *,
    type_line: str = "Land",
    zone: str = "battlefield",
    controller: str | None = "alice",
    owner: str | None = None,
    tapped: bool = False,
    oracle_text: str = "",
    abilities: tuple[Any, ...] = (),
    keywords: tuple[str, ...] = (),
    counters: dict[str, int] | None = None,
    power: int | None = None,
    toughness: int | None = None,
    colors: tuple[str, ...] = (),
    mana_value: float = 0.0,
    mana_cost: str = "",
    cast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    built = []
    for position, entry in enumerate(abilities):
        one = ability(*entry[:1], mana=entry[1]) if isinstance(entry, tuple) else ability(entry)
        # FB2 M4: a card with more than one ability needs each its own real
        # position, the way a live view does - `ability_index` (`_move_
        # request`'s new branch) is picked by this list's own index.
        one["index"] = position
        built.append(one)
    doc = {
        "id": f"00000000-0000-4000-8000-{next(_ids):012d}",
        "name": name,
        "card_name": name,
        "zone": zone,
        "controller": controller,
        "owner": owner if owner is not None else controller,
        "tapped": tapped,
        "face_up": True,
        "type_line": type_line,
        "mana_cost": mana_cost,
        "oracle_text": oracle_text,
        "counters": dict(counters or {}),
        "damage": 0,
        "power": power,
        "toughness": toughness,
        "keywords": list(keywords),
        "colors": list(colors),
        "mana_value": mana_value,
        "abilities": built,
    }
    # FD3: `cast` mirrors `affordances.cast_hints`'s own unconditional
    # publication on a hand/exile card view - `spell_faces`/`modes`/
    # `additional_costs`, the shapes `runtime.cast_body`'s new `face`/
    # `modes`/`optional_costs` handling reads, never through
    # `available_actions`.
    if cast is not None:
        doc["cast"] = cast
    return doc


def cast_hints(
    *,
    mana_cost: str | None = None,
    spell_faces: list[dict[str, Any]] | None = None,
    modes: list[dict[str, Any]] | None = None,
    additional_costs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """FD3: a hand/exile card's own `cast` block
    (`affordances.cast_hints`) - pass to `card(..., cast=cast_hints(...))`."""
    hints: dict[str, Any] = {"needs_x": False}
    if mana_cost is not None:
        hints["mana_cost"] = mana_cost
    if spell_faces is not None:
        hints["spell_faces"] = spell_faces
    if modes is not None:
        hints["modes"] = {
            "instruction": "Choose one -",
            "minimum": 1,
            "maximum": 1,
            "repeatable": False,
            "understood": True,
            "entwine_cost": None,
            "escalate_cost": None,
            "options": modes,
        }
    if additional_costs is not None:
        hints["additional_costs"] = additional_costs
    return hints


def spell_face(name: str, type_line: str, *, spell_face: str | None = None) -> dict[str, Any]:
    """One `cast.spell_faces[]` entry (FD3, `affordances._spell_faces`) -
    `spell_face` left out is the card's own plain front-face cast."""
    return {"spell_face": spell_face, "name": name, "type_line": type_line, "needs_x": False}


def mode_option(number: int, text: str) -> dict[str, Any]:
    """One `cast.modes.options[]` entry (FD3, `affordances.cast_hints`)."""
    return {"number": number, "text": text, "understood": True, "notice": None}


def fetchland(**kw: Any) -> dict[str, Any]:
    return card("Fetch", type_line="Land", oracle_text=FETCH_TEXT, abilities=(FETCH_TEXT,), **kw)


def mountain(**kw: Any) -> dict[str, Any]:
    return card(
        "Mountain",
        type_line="Basic Land — Mountain",
        oracle_text="({T}: Add {R}.)",
        abilities=(("{T}: Add {R}.", True),),
        **kw,
    )


def bolt(**kw: Any) -> dict[str, Any]:
    kw.setdefault("zone", "hand")
    return card(
        "Lightning Bolt",
        type_line="Instant",
        oracle_text="Lightning Bolt deals 3 damage to any target.",
        colors=("R",),
        mana_value=1.0,
        **kw,
    )


def contents(*cards_and_counts: tuple[dict[str, Any], int]) -> list[dict[str, Any]]:
    """A `library_contents` / `sideboard_contents` entry list: no ids."""
    out = []
    for entry, count in cards_and_counts:
        stripped = {k: v for k, v in entry.items() if k != "id"}
        out.append({"card": stripped, "count": count})
    return out


def state_view(
    viewer: str = "alice",
    *,
    opponent: str = "bob",
    battlefield: list[dict[str, Any]] | None = None,
    stack: list[dict[str, Any]] | None = None,
    exile: list[dict[str, Any]] | None = None,
    hands: dict[str, list[dict[str, Any]]] | None = None,
    graveyards: dict[str, list[dict[str, Any]]] | None = None,
    known: dict[str, list[Any]] | None = None,
    library_contents: list[dict[str, Any]] | None = None,
    sideboard_contents: list[dict[str, Any]] | None = None,
    life: dict[str, int] | None = None,
    library_count: dict[str, int] | None = None,
    hand_count: dict[str, int] | None = None,
    graveyard_count: dict[str, int] | None = None,
    sideboard_count: dict[str, int] | None = None,
    active: str = "alice",
    lands_played: dict[str, int] | None = None,
    land_allowance: dict[str, int] | None = None,
    turn_number: int | None = 3,
    own_turns_taken: dict[str, int] | None = None,
    pending_decision: dict[str, Any] | None = None,
    combat_attacks: list[dict[str, Any]] | None = None,
    step: str = "precombat_main",
    priority_holder: str | None = None,
    pregame: dict[str, Any] | None = None,
    mana_pool: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """A redacted `GameStateView` as `viewer` receives it."""
    names = (viewer, opponent)

    def per(mapping: dict[str, int] | None, default: int) -> dict[str, int]:
        return {name: (mapping or {}).get(name, default) for name in names}

    lives = per(life, 20)
    libraries = per(library_count, 40)
    hand_counts = per(hand_count, 7)
    graves = per(graveyard_count, 0)
    sideboards = per(sideboard_count, 15)
    played = per(lands_played, 0)
    allowance = per(land_allowance, 1)
    # Defaults to `turn_number`'s own value: most callers never touch either
    # field and the two happen to coincide before any extra turn is taken,
    # so a test that only sets `turn_number` still gets a coherent view.
    taken = per(own_turns_taken, turn_number if turn_number is not None else 0)
    view: dict[str, Any] = {
        "game_id": "g",
        "pending_decision": pending_decision,
        "turn_number": turn_number,
        "step": step,
        "phase": step,
        "active_player": active,
        "priority_holder": priority_holder if priority_holder is not None else active,
        "players": [
            {
                "username": name,
                "life": lives[name],
                "counters": {},
                "hand_count": hand_counts[name],
                "library_count": libraries[name],
                "graveyard_count": graves[name],
                "sideboard_count": sideboards[name],
                "maximum_hand_size": 7,
                "lands_played": played[name],
                "land_allowance": allowance[name],
                "own_turns_taken": taken[name],
                "mana_pool": (mana_pool or {}).get(name) or {"floating": {}},
            }
            for name in names
        ],
        "battlefield": list(battlefield or []),
        "stack": list(stack or []),
        "exile": list(exile or []),
        "exile_pile": [],
        "play_permissions": {},
        "command": [],
        "hands": {name: (hands or {}).get(name, []) for name in names},
        "graveyards": {name: (graveyards or {}).get(name, []) for name in names},
        "known_cards": {
            viewer: {
                "hand": list((known or {}).get("hand", [])),
                "library": list((known or {}).get("library", [])),
                "sideboard": list((known or {}).get("sideboard", [])),
            }
        },
        "winner": None,
        "drawn": False,
        "losers": [],
        "conceded": [],
        "combat": {"attacks": list(combat_attacks or [])},
        "you": viewer,
        "pregame": pregame,
    }
    if library_contents is not None:
        view["library_contents"] = {viewer: library_contents}
    if sideboard_contents is not None:
        view["sideboard_contents"] = {viewer: sideboard_contents}
    return view


# -- FB2 (B4) builders: pass / declare_attackers / declare_blockers, the new --
# -- answer shapes, and a couple of card conveniences the new moves need -----


def creature(
    name: str = "Bear", *, power: int = 2, toughness: int = 2, **kw: Any
) -> dict[str, Any]:
    """A vanilla creature card view - untapped, on the battlefield, by
    default: the FB2 combat-move tests' usual attacker/blocker."""
    kw.setdefault("type_line", "Creature")
    return card(name, power=power, toughness=toughness, **kw)


def planeswalker(name: str = "Walker", **kw: Any) -> dict[str, Any]:
    """A planeswalker permanent view - `DeclareAttackersMove`'s own
    `DefenderCard` alternative to the opponent player (FB2 M2)."""
    kw.setdefault("type_line", "Legendary Planeswalker")
    return card(name, **kw)


def attacking(attacker: dict[str, Any], defender: str = "bob") -> dict[str, Any]:
    """One `combat.attacks` entry naming `attacker` as attacking `defender`,
    with no blockers yet - `Block.attacker`'s own candidate pool (FB2 M3)."""
    return {"attacker": attacker["id"], "defender": defender, "blockers": []}


def pass_move() -> dict[str, Any]:
    """FB2 M1: release priority once."""
    return {"kind": "pass"}


def attack_group(select: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    """One `AttackGroup` (FB2 M2): `select`/`count`/`prefer`/`ties`/`defender`."""
    group: dict[str, Any] = {"select": select if select is not None else {}}
    group.update(over)
    return group


def declare_attackers_move(attackers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """FB2 M2: `attackers: []` (the default) declares no attackers."""
    return {"kind": "declare_attackers", "attackers": list(attackers or [])}


def block(
    blocker_select: dict[str, Any] | None = None,
    attacker_select: dict[str, Any] | None = None,
    *,
    blocker: dict[str, Any] | None = None,
    attacker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One `Block` (FB2 M3): `blocker_select`/`attacker_select` build the
    plain shapes; pass a whole `blocker`/`attacker` dict instead for
    `count`/`prefer`/`ties` on either half."""
    return {
        "blocker": blocker if blocker is not None else {"select": blocker_select or {}},
        "attacker": attacker if attacker is not None else {"select": attacker_select or {}},
    }


def declare_blockers_move(blocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """FB2 M3: `blocks: []` (the default) declares no blocks."""
    return {"kind": "declare_blockers", "blocks": list(blocks or [])}


def top_order_answer(decision: str, tiers: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    """A scry/surveil answer ordering what stays on top (FB2 A1)."""
    item = {"decision": decision, "top_order": list(tiers)}
    item.update(over)
    return item


def position_answer(position: str, **over: Any) -> dict[str, Any]:
    """A `put_on_top_or_bottom` answer naming `top`/`bottom` directly (FB2 A1)."""
    item = {"decision": "put_on_top_or_bottom", "position": position}
    item.update(over)
    return item


def player_target(player: str = "opponent") -> dict[str, Any]:
    return {"player": player}


def card_target(
    select: dict[str, Any] | None = None, side: str = "opponent", **over: Any
) -> dict[str, Any]:
    """A `CardTarget` (FB2 T1): `select`/`side`, plus `prefer`/`ties`/`zone`."""
    target = {"select": select if select is not None else {}, "side": side}
    target.update(over)
    return target


def targets_answer(targets: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    """A `choose_targets` answer naming `targets` directly (FB2 A2), each
    entry a `card_target(...)` or a `player_target(...)`."""
    item = {"decision": "choose_targets", "targets": list(targets)}
    item.update(over)
    return item


# -- FD3 builders: keyword_action / mulligan / repeat moves, order / damage / --
# -- choice_from answers, and the pending-decision shapes each one reads -----


def keyword_action_move(
    action: str, select: dict[str, Any] | None = None, *, zone: str = "hand", **over: Any
) -> dict[str, Any]:
    """A `KeywordActionMove` (FD2/FD3 item 3): `action` names the route,
    `select` the card by characteristic; `target`/`crew_with` go in `over`."""
    move: dict[str, Any] = {
        "kind": "keyword_action",
        "action": action,
        "card": {"zone": zone, "select": select if select is not None else {}},
    }
    move.update(over)
    return move


def mulligan_move(keep: bool, *, bottom: dict[str, Any] | None = None) -> dict[str, Any]:
    """A `MulliganMove` (FD2/FD3 item 4)."""
    move: dict[str, Any] = {"kind": "mulligan", "keep": keep}
    if bottom is not None:
        move["bottom"] = bottom
    return move


def pick(select: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    """A bare `Pick` (`select`, plus `count`/`prefer`/`ties` in `over`) - the
    cost-object shape `sacrifice`/`discard`/`exile`/`tap`/`convoke`/`delve`/
    `improvise`/`crew_with`/`bottom` all share."""
    item: dict[str, Any] = {"select": select if select is not None else {}}
    item.update(over)
    return item


def repeat_move(
    moves: list[dict[str, Any]],
    until: list[dict[str, Any]],
    max_iterations: int,
    *,
    stop_when_refused: bool = False,
) -> dict[str, Any]:
    """FD9's `RepeatMove`, run by FD3 part 7."""
    return {
        "kind": "repeat",
        "moves": list(moves),
        "until": list(until),
        "max_iterations": max_iterations,
        "stop_when_refused": stop_when_refused,
    }


def order_answer(tiers: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    """An `order_triggers` answer (FD2, CR 603.3b): `order` tiers, first
    resolves last."""
    item = {"decision": "order_triggers", "order": list(tiers)}
    item.update(over)
    return item


def damage_policy(
    tiers: list[dict[str, Any]] | None = None,
    *,
    lethal_first: bool | None = None,
    rest_to: str | None = None,
) -> dict[str, Any]:
    """A `DamagePolicy` (FD2, `Answer.damage`)."""
    policy: dict[str, Any] = {}
    if tiers is not None:
        policy["tiers"] = list(tiers)
    if lethal_first is not None:
        policy["lethal_first"] = lethal_first
    if rest_to is not None:
        policy["rest_to"] = rest_to
    return policy


def damage_answer(policy: dict[str, Any], **over: Any) -> dict[str, Any]:
    item = {"decision": "assign_combat_damage", "damage": policy}
    item.update(over)
    return item


def choice_from_answer(
    zone: str, side: str, select: dict[str, Any] | None = None, **over: Any
) -> dict[str, Any]:
    """A `choose_a_quality` answer naming `choice_from` (FD2): the matched
    card's own name, resolved at run time."""
    item = {
        "decision": "choose_a_quality",
        "choice_from": {"zone": zone, "side": side, "select": select if select is not None else {}},
    }
    item.update(over)
    return item


def pending_order_triggers(abilities: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    """A redacted `order_triggers` pending decision - `abilities` entries are
    `{id, name, controller, text, source_card_id}` (`decisions.PendingDecision
    .abilities`)."""
    pending: dict[str, Any] = {
        "kind": "order_triggers",
        "prompt": "order triggers",
        "cards": [entry["id"] for entry in abilities],
        "abilities": list(abilities),
    }
    pending.update(over)
    return pending


def pending_assign_combat_damage(
    attacker_id: str,
    blockers: list[tuple[str, int]],
    *,
    amount: int,
    trample: bool = False,
    player: str = "alice",
    defender: str = "bob",
) -> dict[str, Any]:
    """A redacted `assign_combat_damage` pending decision - `blockers` is
    `[(card_id, lethal_damage), ...]`; `targets`/`lethal_damage` are aligned
    (`decisions.damage_assignment_answer`'s own reading), the defending
    player appended last when `trample` (offered only once every blocker has
    lethal, which this test builder does not itself enforce - it is the
    engine's own rule, not the DSL's)."""
    targets = [{"kind": "card", "id": card_id} for card_id, _ in blockers]
    lethal = [amount_needed for _, amount_needed in blockers]
    if trample:
        targets.append({"kind": "player", "id": defender})
        lethal.append(0)
    return {
        "kind": "assign_combat_damage",
        "prompt": "assign combat damage",
        "player": player,
        "cards": [attacker_id, *(card_id for card_id, _ in blockers)],
        "amount": amount,
        "targets": targets,
        "lethal_damage": lethal,
        "trample": trample,
    }


def pending_choose_a_quality(**over: Any) -> dict[str, Any]:
    pending: dict[str, Any] = {
        "kind": "choose_a_quality",
        "prompt": "choose a quality",
        "cards": [],
        "amount": 0,
        "options": [],
    }
    pending.update(over)
    return pending
