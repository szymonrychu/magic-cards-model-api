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
ACTIONS_JS = Path(__file__).resolve().parents[1] / "ui" / "actions.js"

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


def test_send_catches_a_rejected_fetch_and_toasts_instead_of_throwing() -> None:
    """`send` is the one place every action path in `app.js` goes through
    instead of calling `api()` directly - a transport failure (a rejected
    `fetch`: the network is down, the server unreachable) must resolve to
    `null` and toast the error, never escape as an unhandled rejection.

    `document` is stubbed here, locally to this one test, with just enough
    (`querySelector("#toast")`) for `toast()` to run - `window` stays
    undefined, so `app.js`'s own `main()` guard still never fires."""
    result = run_app(
        """(async () => {
          const toastEl = { textContent: "", hidden: true };
          globalThis.document = { querySelector: (sel) => (sel === "#toast" ? toastEl : null) };
          globalThis.fetch = () => Promise.reject(new Error("network is down"));
          const res = await m.send("GET", "/api/games/x");
          return { res, toastText: toastEl.textContent, toastHidden: toastEl.hidden };
        })()"""
    )
    assert result["res"] is None, "a transport failure must resolve to null, not throw"
    assert result["toastHidden"] is False
    assert "network is down" in result["toastText"]


# Electrolyze: "up to two target creatures and/or players" - CR 115.6's "up
# to" shape, the case where the regression this file guards actually bit.
# Once both slots are filled, `activeSlotIndex` (and so `targetSlotIndex`)
# has nowhere left to offer (CR 601.2c never forces every "up to" slot filled
# all the way) - a second click on an already-chosen target still has to
# un-choose it rather than being swallowed by that "nowhere to fill" case.
SPLIT = {
    "id": "sp1",
    "name": "Electrolyze",
    "type_line": "Instant",
    "zone": "hand",
    "controller": "alice",
    "cast": {
        "mana_cost": "{1}{U}{R}",
        "needs_x": False,
        "modes": None,
        "targeting": {"targets_anything": True},
    },
}
SPLIT_AFF = {
    "targets_anything": True,
    "understood": True,
    "minimum": 1,
    "maximum": 2,
    "requirements": [
        {
            "phrase": "up to two target creatures and/or players",
            "count": 2,
            "minimum": 1,
            "unbounded": False,
            "variable_count": False,
            "mode": None,
            "mode_text": None,
        }
    ],
    "division": None,
    "candidates": [
        {"id": "bob", "kind": "player", "name": "bob", "requirements": [0]},
        {"id": "c9", "kind": "card", "name": "Memnite", "requirements": [0]},
    ],
}


def test_a_second_click_on_a_chosen_target_still_unchooses_it_once_the_slot_is_full() -> None:
    """The regression: `tryTarget` started resolving every fresh click
    through `targetSlotIndex`/`canFill`, and once an "up to two" slot holds
    both its targets, `targetSlotIndex` has nowhere left to offer (-1) - a
    second click on one of the two already-chosen targets was silently
    swallowed before it ever reached `actions.js`'s own `chooseTarget`, which
    already knows how to unfill an already-chosen target regardless of how
    much room is left. Drives the real, exported `tryTarget` against the
    real, exported `store` (both test-only exports - see their own comments
    in `app.js`) rather than a Python restatement of either."""
    result = run_app(
        """(async () => {
          const A = await import(pathToFileURL(actionsPath).href);
          globalThis.document = { querySelector: () => null };
          const state = { you: "alice", battlefield: [], pending_decision: null };
          m.store.set({ gameId: "g1", uid: "u1", state });
          let action = A.withAffordance(A.begin("cast", split, {}), aff);
          action = A.chooseTarget(action, "bob");
          action = A.chooseTarget(action, "c9");
          m.store.set({ action });
          const fullSlotIndex = A.targetSlotIndex(m.store.get().action);
          await m.tryTarget("bob");
          const after = m.store.get().action;
          return { fullSlotIndex, targetsAfter: after.slots.flatMap((s) => s.targets) };
        })()""",
        split=SPLIT,
        aff=SPLIT_AFF,
        actionsPath=str(ACTIONS_JS),
    )
    assert result["fullSlotIndex"] == -1, (
        "the slot must already be at its own cap for this to be the regression"
    )
    assert result["targetsAfter"] == ["c9"], "a second click on bob must remove it, not no-op"


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

VEHICLE = {
    "id": "sc",
    "name": "Smuggler's Copter",
    "type_line": "Artifact Vehicle",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [],
}

CREWED_VEHICLE_WITH_ABILITY = {
    "id": "vv",
    "name": "Skysovereign, Consul Flagship",
    "type_line": "Legendary Artifact Vehicle",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [
        {"index": 0, "text": "Whenever this Vehicle attacks...", "is_mana_ability": False}
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


def test_a_vehicle_with_no_abilities_still_offers_a_crew_menu() -> None:
    """CR 702.122a: crewing is a Vehicle's own thing to do even with no
    printed activated ability at all - `vehicle: true` is what tells
    `onPermanent` to add the "Crew" item to an otherwise-empty menu."""
    plan = run_app(
        "m.planPermanentClick(vehicle, app, state)",
        vehicle=VEHICLE,
        app=base_app(),
        state=base_state(),
    )
    assert plan == {"kind": "menu", "abilities": [], "vehicle": True}


def test_a_vehicle_with_an_ability_also_menus_rather_than_auto_activating() -> None:
    """A Vehicle with exactly one activated ability still isn't the
    single-ability auto-activate case (`abilities.length === 1 && !vehicle`)
    - crewing is always a second choice standing next to it."""
    plan = run_app(
        "m.planPermanentClick(vehicle, app, state)",
        vehicle=CREWED_VEHICLE_WITH_ABILITY,
        app=base_app(),
        state=base_state(),
    )
    assert plan["kind"] == "menu"
    assert plan["vehicle"] is True
    assert len(plan["abilities"]) == 1
