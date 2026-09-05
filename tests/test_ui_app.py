"""`ui/app.js` under Node: the click planners, run against the real shipped
module rather than a Python restatement of it.

`app.js` ties every pure module (`actions.js`, `priority.js`, `combat.js`,
`decisions.js`, `targeting.js`) and every render module (`board.js`,
`ribbon.js`, `log.js`, `lobby.js`, `panels.js`) together into the running
page - most of what it does is DOM and `fetch`, neither of which this file
can exercise under Node. What it *can* exercise, and what actually decides
the click flows the task brief asks for, is the two pure planners
`app.js` factors its click handlers through: `planHandCardClick(card, app)`
("what does clicking this card in my hand do") and
`planPermanentClick(card, app, state)` ("what does clicking this permanent
on the battlefield do"). `onHandCard`/`onPermanent` are thin wrappers that
act on whichever plan comes back - so testing the planner is testing the
real decision, not a shadow copy of it.

Importing the module at all needs `localStorage` on `globalThis` before the
`import` resolves - `app.js`'s own top-level `createStore({... stops:
P.loadStops(localStorage) ...})` runs at import time, and `P.loadStops`
reads it immediately. `window` and `document` are deliberately left
undefined (not merely unstubbed) so `app.js`'s own closing guard -
`if (typeof window !== "undefined" && typeof document !== "undefined")
main();` - never fires: that guard exists precisely so a real browser runs
`main()` and this import does not. `fetch` is stubbed to throw, so any test
that accidentally exercises a network path fails loudly rather than hanging.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "ui" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")


def run_app(expression: str, **bindings: Any) -> Any:
    consts = "\n".join(f"const {name} = {json.dumps(value)};" for name, value in bindings.items())
    script = f"""
    import {{ pathToFileURL }} from "node:url";
    globalThis.localStorage = {{
      getItem: () => null,
      setItem() {{}},
      removeItem() {{}},
    }};
    globalThis.fetch = () => {{
      throw new Error("test_ui_app.py's planners must never touch the network");
    }};
    const m = await import(pathToFileURL({json.dumps(str(APP_JS))}).href);
    {consts}
    const __result = await eval({json.dumps(expression)});
    process.stdout.write(JSON.stringify(__result === undefined ? null : __result));
    """
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout or "null")


def test_app_js_still_exports_start_game_for_the_routes_test() -> None:
    """`tests/test_ui_routes.py` asserts the served `/app.js` contains the
    literal string `startGame` - this is the same guarantee, checked against
    the actual export rather than a substring in the served text."""
    assert run_app("typeof m.startGame") == "function"


# -- fixtures ----------------------------------------------------------------

MOUNTAIN = {
    "id": "land1",
    "name": "Mountain",
    "type_line": "Basic Land - Mountain",
    "zone": "hand",
    "controller": "alice",
}

MEMNITE = {
    "id": "mm",
    "name": "Memnite",
    "type_line": "Artifact Creature",
    "zone": "hand",
    "controller": "alice",
    "cast": {
        "mana_cost": "{0}",
        "needs_x": False,
        "modes": None,
        "targeting": {"targets_anything": False},
    },
}

KOZILEK = {
    "id": "kc",
    "name": "Kozilek's Command",
    "type_line": "Kindred Instant",
    "zone": "hand",
    "controller": "alice",
    "cast": {
        "mana_cost": "{X}{C}{C}",
        "needs_x": True,
        "modes": {"instruction": "Choose two -", "minimum": 2, "maximum": 2, "options": []},
        "targeting": {"targets_anything": True},
    },
}

CYCLING_LAND = {
    "id": "cl1",
    "name": "Tranquil Thicket",
    "type_line": "Land",
    "zone": "hand",
    "controller": "alice",
    "special_actions": {"cycling": [{"keyword": "Cycling", "cost": "{2}", "index": 0}]},
}

FORETELL_CARD = {
    "id": "f1",
    "name": "Alrund's Epiphany",
    "type_line": "Sorcery",
    "zone": "hand",
    "controller": "alice",
    "special_actions": {"foretell": {"cost": "{2}"}},
}

ONE_ABILITY_LAND = {
    "id": "d1",
    "name": "Sunbaked Canyon",
    "type_line": "Land",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [
        {"index": 0, "text": "{T}, Pay 1 life: Add {R} or {W}.", "is_mana_ability": True},
    ],
}

ONE_ACTIVATED_ABILITY_PW = {
    "id": "pw1",
    "name": "Test Planeswalker",
    "type_line": "Legendary Planeswalker",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [{"index": 0, "text": "+1: draw a card", "is_mana_ability": False}],
}

TWO_ABILITY_PW = {
    "id": "pw2",
    "name": "Two-Ability Planeswalker",
    "type_line": "Legendary Planeswalker",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [
        {"index": 0, "text": "+1: draw a card", "is_mana_ability": False},
        {"index": 1, "text": "-2: destroy target creature", "is_mana_ability": False},
    ],
}

ATTACKING_CREATURE = {
    "id": "bears1",
    "name": "Grizzly Bears",
    "type_line": "Creature - Bear",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [],
}


def base_app(**overrides: Any) -> dict:
    return {"action": None, "decisionDraft": {}, **overrides}


def base_state(**overrides: Any) -> dict:
    return {
        "you": "alice",
        "pending_decision": None,
        "step": "precombat_main",
        "active_player": "alice",
        "priority_holder": "alice",
        **overrides,
    }


# -- planHandCardClick --------------------------------------------------------


def test_a_land_with_nothing_else_plays_on_one_click() -> None:
    plan = run_app("m.planHandCardClick(land, app)", land=MOUNTAIN, app=base_app())
    assert plan == {"kind": "land"}


def test_a_nonland_with_no_specials_goes_straight_to_cast() -> None:
    plan = run_app("m.planHandCardClick(memnite, app)", memnite=MEMNITE, app=base_app())
    assert plan == {"kind": "cast"}


def test_a_modal_x_spell_also_goes_to_cast_first() -> None:
    """The planner only decides *which flow* a click starts, not whether that
    flow will need to ask for modes/X/targets afterward - that is
    `actions.js`'s `needsChoices`, consulted downstream in `beginCast`. A
    spell as choice-heavy as Kozilek's Command still starts the same way a
    Memnite does."""
    plan = run_app("m.planHandCardClick(koz, app)", koz=KOZILEK, app=base_app())
    assert plan == {"kind": "cast"}


def test_clicking_the_card_already_mid_cast_cancels_it() -> None:
    plan = run_app(
        "m.planHandCardClick(memnite, app)",
        memnite=MEMNITE,
        app=base_app(action={"cardId": "mm"}),
    )
    assert plan == {"kind": "cancel"}


def test_clicking_a_different_card_while_one_action_is_pending_is_a_target_click() -> None:
    plan = run_app(
        "m.planHandCardClick(memnite, app)",
        memnite=MEMNITE,
        app=base_app(action={"cardId": "something-else"}),
    )
    assert plan == {"kind": "target", "id": "mm"}


def test_a_cycling_land_offers_a_menu_naming_it_a_land() -> None:
    """A land whose only special action is cycling used to skip straight to
    `playLand` (the old `!specials.length` check never counted `cycling`),
    which meant a cycling land could never actually be cycled from the hand
    click. The menu case now checks both lists."""
    plan = run_app("m.planHandCardClick(land, app)", land=CYCLING_LAND, app=base_app())
    assert plan["kind"] == "menu"
    assert plan["isLand"] is True
    assert plan["specials"] == []
    assert len(plan["cycling"]) == 1


def test_a_nonland_special_action_offers_a_menu_naming_it_not_a_land() -> None:
    plan = run_app("m.planHandCardClick(card, app)", card=FORETELL_CARD, app=base_app())
    assert plan["kind"] == "menu"
    assert plan["isLand"] is False
    assert plan["specials"] == ["foretell"]
    assert plan["cycling"] == []


# -- planPermanentClick -------------------------------------------------------


def test_a_one_ability_land_during_a_pending_cast_pins_rather_than_targets() -> None:
    """Clicking a mana-ability land while a spell is being cast pins it as a
    mana source, as long as it is not itself a legal target (candidate) of
    the spell - CR 601.2c targeting takes priority over paying for a land
    that could genuinely be targeted."""
    action = {"cardId": "spell1", "affordance": {"candidates": [{"id": "someone-else"}]}}
    plan = run_app(
        "m.planPermanentClick(land, app, state)",
        land=ONE_ABILITY_LAND,
        app=base_app(action=action),
        state=base_state(),
    )
    assert plan == {"kind": "pin"}


def test_a_land_already_a_legal_target_is_a_target_click_not_a_pin() -> None:
    action = {"cardId": "spell1", "affordance": {"candidates": [{"id": "d1"}]}}
    plan = run_app(
        "m.planPermanentClick(land, app, state)",
        land=ONE_ABILITY_LAND,
        app=base_app(action=action),
        state=base_state(),
    )
    assert plan == {"kind": "target", "id": "d1"}


def test_a_choose_targets_decision_routes_board_clicks_to_target_even_with_no_action() -> None:
    plan = run_app(
        "m.planPermanentClick(creature, app, state)",
        creature=ATTACKING_CREATURE,
        app=base_app(),
        state=base_state(pending_decision={"player": "alice", "kind": "choose_targets"}),
    )
    assert plan == {"kind": "target", "id": "bears1"}


def test_a_creature_in_your_declare_attackers_step_toggles_into_the_red_zone() -> None:
    plan = run_app(
        "m.planPermanentClick(creature, app, state)",
        creature=ATTACKING_CREATURE,
        app=base_app(),
        state=base_state(step="declare_attackers"),
    )
    assert plan == {"kind": "attack"}


def test_the_defending_players_creature_is_a_block_click() -> None:
    plan = run_app(
        "m.planPermanentClick(creature, app, state)",
        creature=ATTACKING_CREATURE,
        app=base_app(),
        state=base_state(step="declare_blockers", active_player="bob"),
    )
    assert plan == {"kind": "block"}


def test_an_opponents_permanent_with_nothing_pending_is_none() -> None:
    plan = run_app(
        "m.planPermanentClick(creature, app, state)",
        creature={**ATTACKING_CREATURE, "controller": "bob"},
        app=base_app(),
        state=base_state(),
    )
    assert plan == {"kind": "none"}


def test_a_no_ability_permanent_of_yours_is_none_with_a_reason() -> None:
    plan = run_app(
        "m.planPermanentClick(creature, app, state)",
        creature=ATTACKING_CREATURE,
        app=base_app(),
        state=base_state(),
    )
    assert plan == {"kind": "none", "reason": "Grizzly Bears has no activated abilities"}


def test_a_single_activated_ability_activates_directly() -> None:
    plan = run_app(
        "m.planPermanentClick(pw, app, state)",
        pw=ONE_ACTIVATED_ABILITY_PW,
        app=base_app(),
        state=base_state(),
    )
    assert plan == {"kind": "activate", "abilityIndex": 0}


def test_two_activated_abilities_open_a_menu() -> None:
    plan = run_app(
        "m.planPermanentClick(pw, app, state)",
        pw=TWO_ABILITY_PW,
        app=base_app(),
        state=base_state(),
    )
    assert plan["kind"] == "menu"
    assert plan["vehicle"] is False
    assert len(plan["abilities"]) == 2
