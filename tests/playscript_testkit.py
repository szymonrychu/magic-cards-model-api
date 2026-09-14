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


def play(**over: Any) -> dict[str, Any]:
    """The fetch-shock worked example with top-level fields replaced."""
    base = copy.deepcopy(FETCH_SHOCK)
    base.update(over)
    return base


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
) -> dict[str, Any]:
    built = []
    for entry in abilities:
        built.append(
            ability(*entry[:1], mana=entry[1]) if isinstance(entry, tuple) else ability(entry)
        )
    return {
        "id": f"00000000-0000-4000-8000-{next(_ids):012d}",
        "name": name,
        "card_name": name,
        "zone": zone,
        "controller": controller,
        "owner": owner if owner is not None else controller,
        "tapped": tapped,
        "face_up": True,
        "type_line": type_line,
        "mana_cost": "",
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
        "pending_decision": None,
        "turn_number": turn_number,
        "step": "precombat_main",
        "phase": "precombat_main",
        "active_player": active,
        "priority_holder": active,
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
        "combat": {"attacks": []},
        "you": viewer,
    }
    if library_contents is not None:
        view["library_contents"] = {viewer: library_contents}
    if sideboard_contents is not None:
        view["sideboard_contents"] = {viewer: sideboard_contents}
    return view
