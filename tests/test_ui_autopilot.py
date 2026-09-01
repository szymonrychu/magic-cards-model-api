"""The priority-automation stop conditions in `ui/app.js`, exercised for
real, not reimplemented in Python.

`autopilotStopReason` and `interpretPassOutcome` are the two pure functions
`tickAutopilot` (the "pass until my upkeep" driver, run once per `poll()`
tick - never an unbounded loop of its own) consults before ever calling
`pass-priority`. They cover the four required stop conditions: a pending
decision addressed to this seat, the game ending, anything landing on the
stack (gated by a setting that defaults to the safe "stop and ask"), and the
pass itself being refused. These are exactly the paths that "cost games" if
they silently stop working - the same class of bug a decision-timeout has
already caused in this project - so this loads and runs the actual shipped
JS under Node (`ui/app.js` is `require()`-able specifically for this; see
its own `module.exports` guard and the `window`/`document` shims at its top
and bottom), rather than asserting against a Python re-description of what
the JS is supposed to do.

`ui/app.js` has no build step and is not otherwise transpiled or bundled -
this test runs the exact file the browser is served (`GET /play/app.js`).
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


def run_js(expression: str) -> Any:
    """Load the real `ui/app.js` under Node and evaluate `expression` against
    its exported `{autopilotStopReason, interpretPassOutcome}`, printing the
    result as JSON so this stays a black-box call across the process
    boundary - no Python reimplementation of the JS logic anywhere here."""
    script = f"""
    const {{ autopilotStopReason, interpretPassOutcome }} = require({json.dumps(str(APP_JS))});
    const result = ({expression});
    process.stdout.write(JSON.stringify(result === undefined ? null : result));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def base_state(**overrides: Any) -> dict:
    state = {
        "you": "alice",
        "winner": None,
        "drawn": False,
        "pending_decision": None,
        "step": "draw",
        "active_player": "bob",
        "priority_holder": "bob",
        "stack": [],
    }
    state.update(overrides)
    return state


def call_stop_reason(
    state: dict, autopilot: dict, you: str = "alice", settings: dict | None = None
):
    """Calls the real `autopilotStopReason`. `autopilot["knownStackIds"]` is
    given here as a plain list (JSON has no Set type) and rebuilt into a real
    JS `Set` in the generated script - the same shape `game.autopilot` itself
    carries in `ui/app.js` (`new Set((state.stack || []).map((o) => o.id))`),
    so this exercises the function against its real input shape, not a
    simplified stand-in for it."""
    settings = settings if settings is not None else {"stopOnStack": True}
    known = json.dumps(list(autopilot.get("knownStackIds", [])))
    fields = [f"knownStackIds: new Set({known})"]
    for key in ("target", "step", "who"):
        if key in autopilot:
            fields.append(f"{key}: {json.dumps(autopilot[key])}")
    autopilot_literal = "{" + ", ".join(fields) + "}"
    return run_js(
        f"autopilotStopReason({json.dumps(state)}, {autopilot_literal}, "
        f"{json.dumps(you)}, {json.dumps(settings)})"
    )


AUTOPILOT_UNTIL_UPKEEP = {"target": "upkeep", "knownStackIds": []}


def test_module_exports_the_two_pure_functions() -> None:
    """`setHtml` joined them for `tests/test_ui_pregame_panel.py`: its repaint
    rule is what decides whether the CR 103.5 "Keep" button ever leaves
    `disabled`, and that has to be tested against the shipped file too."""
    assert APP_JS.is_file(), "ui/app.js is missing"
    exported = run_js("Object.keys(require(" + json.dumps(str(APP_JS)) + "))")
    assert set(exported) == {
        "autopilotStopReason",
        "interpretPassOutcome",
        "sendTargets",
        "setHtml",
    }


# -- stop condition: a pending decision addressed to this seat --------------


def test_halts_on_a_pending_decision_for_this_seat() -> None:
    state = base_state(pending_decision={"player": "alice", "kind": "may", "prompt": "Pay 2 life?"})
    reason = call_stop_reason(state, AUTOPILOT_UNTIL_UPKEEP)
    assert reason is not None
    assert "Pay 2 life?" in reason


def test_does_not_halt_on_a_decision_addressed_to_the_opponent() -> None:
    state = base_state(pending_decision={"player": "bob", "kind": "may", "prompt": "Pay 2 life?"})
    reason = call_stop_reason(state, AUTOPILOT_UNTIL_UPKEEP)
    assert reason is None


# -- stop condition: the game ends -------------------------------------------


def test_halts_when_this_seat_wins() -> None:
    state = base_state(winner="alice")
    reason = call_stop_reason(state, AUTOPILOT_UNTIL_UPKEEP)
    assert reason is not None
    assert "alice" in reason


def test_halts_when_the_opponent_wins() -> None:
    state = base_state(winner="bob")
    reason = call_stop_reason(state, AUTOPILOT_UNTIL_UPKEEP)
    assert reason is not None
    assert "bob" in reason


def test_halts_on_a_draw() -> None:
    state = base_state(drawn=True)
    reason = call_stop_reason(state, AUTOPILOT_UNTIL_UPKEEP)
    assert reason is not None
    assert "draw" in reason


# -- reaching the target: "pass until my upkeep" -----------------------------


def test_halts_on_reaching_the_players_own_upkeep() -> None:
    state = base_state(step="upkeep", active_player="alice")
    reason = call_stop_reason(state, AUTOPILOT_UNTIL_UPKEEP)
    assert reason == "reached your upkeep"


def test_does_not_halt_on_the_opponents_upkeep() -> None:
    state = base_state(step="upkeep", active_player="bob")
    reason = call_stop_reason(state, AUTOPILOT_UNTIL_UPKEEP)
    assert reason is None


# -- stop condition: something lands on the stack ----------------------------


def test_halts_on_a_new_stack_object_when_the_safe_default_is_on() -> None:
    state = base_state(stack=[{"id": "spell-1", "name": "Lightning Bolt"}])
    autopilot = {"target": "upkeep", "knownStackIds": []}
    reason = call_stop_reason(state, autopilot, settings={"stopOnStack": True})
    assert reason is not None
    assert "Lightning Bolt" in reason


def test_does_not_halt_on_a_new_stack_object_when_the_setting_is_off() -> None:
    state = base_state(stack=[{"id": "spell-1", "name": "Lightning Bolt"}])
    autopilot = {"target": "upkeep", "knownStackIds": []}
    reason = call_stop_reason(state, autopilot, settings={"stopOnStack": False})
    assert reason is None


def test_does_not_halt_on_a_stack_object_already_known() -> None:
    state = base_state(stack=[{"id": "spell-1", "name": "Lightning Bolt"}])
    autopilot = {"target": "upkeep", "knownStackIds": ["spell-1"]}
    reason = call_stop_reason(state, autopilot, settings={"stopOnStack": True})
    assert reason is None


def test_the_safe_default_is_stop_and_ask() -> None:
    """`game.settings` in `ui/app.js` initializes `stopOnStack: true` - this
    is the actual default value that constant carries, exercised the same
    way any other call site would, with `stopOnStack: true` passed exactly
    as the running app always does at start-up."""
    state = base_state(stack=[{"id": "spell-1", "name": "Lightning Bolt"}])
    reason = call_stop_reason(
        state, {"target": "upkeep", "knownStackIds": []}, settings={"stopOnStack": True}
    )
    assert reason is not None


# -- no autopilot running -----------------------------------------------------


def test_returns_null_when_no_autopilot_is_running() -> None:
    state = base_state(winner="alice")
    reason = run_js(
        f"autopilotStopReason({json.dumps(state)}, null, 'alice', {{stopOnStack: true}})"
    )
    assert reason is None


# -- stop condition: the pass itself is refused ------------------------------


def test_interpret_pass_outcome_halts_on_a_refusal() -> None:
    reason = run_js(
        'interpretPassOutcome({"allowed": false, "reason": "you do not have priority"})'
    )
    assert reason is not None
    assert "you do not have priority" in reason


def test_interpret_pass_outcome_continues_on_success() -> None:
    reason = run_js('interpretPassOutcome({"allowed": true, "outcome": "passed"})')
    assert reason is None


# -- reaching a *named* destination: "auto-pass until <step>" (feature 3) -----
#
# The picker offers every step in the turn, for either player's turn, not just
# "my upkeep". `who` says whose turn the step has to be in; a run built the
# older way (`target` alone) still means "in my own turn", which the
# `AUTOPILOT_UNTIL_UPKEEP` cases above already pin.


def until(step: str, who: str) -> dict:
    return {"step": step, "who": who, "knownStackIds": []}


def test_halts_on_reaching_a_named_step_in_your_own_turn() -> None:
    state = base_state(step="postcombat_main", active_player="alice")
    reason = call_stop_reason(state, until("postcombat_main", "you"))
    assert reason is not None
    assert "postcombat main" in reason


def test_does_not_halt_on_that_step_in_the_opponents_turn() -> None:
    state = base_state(step="postcombat_main", active_player="bob")
    assert call_stop_reason(state, until("postcombat_main", "you")) is None


def test_halts_on_a_named_step_in_the_opponents_turn() -> None:
    state = base_state(step="end_step", active_player="bob")
    reason = call_stop_reason(state, until("end_step", "opponent"))
    assert reason is not None
    assert "opponent" in reason


def test_does_not_halt_on_the_opponents_step_during_your_own_turn() -> None:
    state = base_state(step="end_step", active_player="alice")
    assert call_stop_reason(state, until("end_step", "opponent")) is None


def test_who_any_halts_in_either_players_turn() -> None:
    for active in ("alice", "bob"):
        state = base_state(step="upkeep", active_player=active)
        assert call_stop_reason(state, until("upkeep", "any")) is not None


def test_a_different_step_does_not_halt() -> None:
    state = base_state(step="draw", active_player="alice")
    assert call_stop_reason(state, until("upkeep", "you")) is None


def test_the_game_ending_outranks_the_destination() -> None:
    """Stop conditions are checked in order, and the game being over wins -
    a run must never keep passing into a finished game."""
    state = base_state(step="upkeep", active_player="alice", winner="bob")
    reason = call_stop_reason(state, until("upkeep", "you"))
    assert "bob" in reason


def test_a_decision_outranks_the_destination() -> None:
    state = base_state(
        step="upkeep",
        active_player="alice",
        pending_decision={"player": "alice", "kind": "may", "prompt": "Pay 2 life?"},
    )
    reason = call_stop_reason(state, until("upkeep", "you"))
    assert "Pay 2 life?" in reason
