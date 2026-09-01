"""The auto-pass run must not walk you through your own declare-blockers step,
run against the real `ui/app.js`.

**The bug, as reported.** "Auto-pass until…" stopped for exactly four things:
the game ending, a `pending_decision` addressed to this seat, the named
destination being reached, and a new object arriving on the stack. None of them
fires when the opponent attacks, so a run aimed at (say) "my main 1" passed the
defending player straight through declare blockers and took the whole attack
unblocked.

Neither of the two existing near-miss conditions could ever have caught it:

- Declaring blockers is a turn-based action. CR 509.1, verified with
  `knowledge_search(type="rules")` in the session that wrote this file: "First,
  the defending player declares blockers. This turn-based action doesn't use
  the stack." CR 703.4j puts it immediately after the step begins. So nothing
  lands on the stack and `settings.stopOnStack` cannot see it.
- `src/magic_cards_model_api/decisions.py` deliberately keeps
  `DECLARE_ATTACKERS` and `DECLARE_BLOCKERS` out of `ANSWERABLE_KINDS`, because
  both have their own actions (`actions/attack`, `actions/declare-blockers`)
  rather than going through the decision mailbox. So `state.pending_decision`
  is never set for blocking either.

The fix is a third stop condition, `settings.stopWhenAttacked`, defaulting on
for the same reason `stopOnStack` does: the safe default is the one that does
not silently lose you the game. It fires on "the step is declare_blockers, this
seat is not the active player, and at least one attacker has been declared" -
and deliberately says nothing about whether any of your creatures could legally
block any of them, which is the engine's question, not this file's.

Like `tests/test_ui_autopilot_next_occurrence.py` and
`tests/test_ui_priority_panel.py`, this drives the shipped functions under Node
- the exact file the browser is served, no build step, no Python
reimplementation of the logic under test.
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

#: The steps of one turn, in the order the picker walks them (untap and cleanup
#: are not offered as auto-pass targets).
TURN_STEPS = [
    "upkeep",
    "draw",
    "precombat_main",
    "beginning_of_combat",
    "declare_attackers",
    "declare_blockers",
    "combat_damage",
    "end_of_combat",
    "postcombat_main",
    "end_step",
]

#: Two attacks as `serialize.py` writes them into `state.combat.attacks`:
#: attacker id, defender, and the blockers declared so far (none yet, which is
#: exactly the moment this stop condition is about).
TWO_ATTACKS = [
    {"attacker": "att-1", "defender": "alice", "blockers": []},
    {"attacker": "att-2", "defender": "alice", "blockers": []},
]


def board(step: str, turn: int, active: str = "alice", **overrides: Any) -> dict[str, Any]:
    """A board state shaped like `GET /api/games/{id}`, with the fields the
    autopilot's stop check reads."""
    state: dict[str, Any] = {
        "you": "alice",
        "winner": None,
        "drawn": False,
        "pending_decision": None,
        "step": step,
        "turn_number": turn,
        "active_player": active,
        "priority_holder": "alice",
        "stack": [],
        "combat": {"attacks": []},
    }
    state.update(overrides)
    return state


def under_attack(turn: int, **overrides: Any) -> dict[str, Any]:
    """Bob's declare-blockers step, with bob attacking alice - the seat this
    file plays is always alice."""
    return board("declare_blockers", turn, "bob", combat={"attacks": TWO_ATTACKS}, **overrides)


def run_until_stop(
    start: dict[str, Any],
    target_step: str,
    who: str,
    timeline: list[dict[str, Any]],
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a real autopilot run standing in `start`, then feed it `timeline`
    one board state at a time - the way `poll()` feeds `tickAutopilot` - and
    report the first state that stopped it, with the reason.

    The run object is the one the shipped `startAutopilot` builds and the stop
    check is the shipped `autopilotStopReason`; neither is re-described here.
    """
    script = f"""
    const banner = {{ hidden: true, innerHTML: "" }};
    globalThis.document = {{
      activeElement: null,
      body: {{}},
      addEventListener() {{}},
      querySelector: () => banner,
      querySelectorAll: () => [],
    }};
    const {{ startAutopilot, autopilotStopReason, forgetStartingStep, game }} =
      require({json.dumps(str(APP_JS))});
    const defaults = JSON.parse(JSON.stringify(game.settings));
    Object.assign(game.settings, {json.dumps(settings or {})});
    game.state = {json.dumps(start)};
    startAutopilot({json.dumps(target_step)}, {json.dumps(who)});

    const seen = [];
    let stopped = null;
    for (const state of {json.dumps(timeline)}) {{
      const reason = autopilotStopReason(state, game.autopilot, state.you, game.settings);
      if (reason) {{
        stopped = {{
          reason,
          step: state.step,
          turn: state.turn_number,
          active: state.active_player,
        }};
        break;
      }}
      // Exactly what `tickAutopilot` does with a state it did not stop on.
      game.autopilot.knownStackIds = new Set((state.stack || []).map((o) => o.id));
      forgetStartingStep(game.autopilot, state);
      seen.push(state.step + "@" + state.turn_number);
    }}
    process.stdout.write(JSON.stringify({{ stopped, passed_through: seen, defaults }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def turn_cycle(turn: int, active: str, **overrides: Any) -> list[dict[str, Any]]:
    return [board(step, turn, active, **overrides) for step in TURN_STEPS]


def action_bar_html(state: dict[str, Any]) -> str:
    """The HTML `renderActionBar` really writes into `#action-bar`."""
    script = f"""
    const bar = {{ innerHTML: "", contains: () => false, querySelector: () => null }};
    globalThis.document = {{
      activeElement: null,
      body: {{}},
      addEventListener() {{}},
      querySelector: () => bar,
      querySelectorAll: () => [],
    }};
    const {{ renderActionBar }} = require({json.dumps(str(APP_JS))});
    renderActionBar({json.dumps(state)}, "alice");
    process.stdout.write(JSON.stringify(bar.innerHTML));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


# -- the reported bug --------------------------------------------------------


def test_a_run_stops_when_this_seat_is_being_attacked() -> None:
    """Alice auto-passes from her own end step towards her next main 1. Bob
    takes his turn and attacks; the run must stop in his declare-blockers step
    rather than passing through it and eating the whole attack."""
    start = board("end_step", 1)
    timeline = [
        board("upkeep", 2, "bob"),
        board("draw", 2, "bob"),
        board("precombat_main", 2, "bob"),
        board("beginning_of_combat", 2, "bob"),
        board("declare_attackers", 2, "bob", combat={"attacks": TWO_ATTACKS}),
        under_attack(2),
        # Everything after here is what the bug used to let happen.
        board("combat_damage", 2, "bob", combat={"attacks": TWO_ATTACKS}),
    ] + turn_cycle(3, "alice")

    result = run_until_stop(start, "precombat_main", "you", timeline)

    assert result["stopped"] is not None, "the run passed through its own declare blockers"
    assert result["stopped"]["step"] == "declare_blockers", result
    assert result["stopped"]["turn"] == 2
    assert result["stopped"]["active"] == "bob"
    assert result["stopped"]["reason"] == "you are being attacked by 2 creatures - declare blockers"
    assert "combat_damage@2" not in result["passed_through"], result


def test_it_stops_for_a_single_attacker_too() -> None:
    """One attacker is still an attack, and the reason reads as English."""
    start = board("end_step", 1)
    one = [{"attacker": "att-1", "defender": "alice", "blockers": []}]
    timeline = [board("declare_blockers", 2, "bob", combat={"attacks": one})]

    result = run_until_stop(start, "precombat_main", "you", timeline)

    assert result["stopped"]["reason"] == "you are being attacked by 1 creature - declare blockers"


def test_the_setting_defaults_on() -> None:
    """Same reason `stopOnStack` does: the safe default is the one that does
    not silently lose you the game."""
    result = run_until_stop(board("end_step", 1), "precombat_main", "you", [])

    assert result["defaults"]["stopWhenAttacked"] is True, result["defaults"]


# -- and only when it should ------------------------------------------------


def test_the_attacking_player_is_not_stopped_in_the_same_step() -> None:
    """Alice is the active player here: these are her attackers, and blocking
    is bob's problem. Her run must sail through her own declare-blockers step
    exactly as it did before."""
    start = board("upkeep", 3)
    timeline = [
        board("declare_attackers", 3, "alice", combat={"attacks": TWO_ATTACKS}),
        board("declare_blockers", 3, "alice", combat={"attacks": TWO_ATTACKS}),
        board("combat_damage", 3, "alice", combat={"attacks": TWO_ATTACKS}),
        board("postcombat_main", 3, "alice", combat={"attacks": TWO_ATTACKS}),
    ]

    # `stopWhenYouCanAttack` off: this timeline crosses alice's own
    # declare-attackers step, which that setting stops for by design, and this
    # test is about the blockers condition rather than that one. See
    # `tests/test_ui_autopilot_attackers.py` for its own coverage.
    result = run_until_stop(
        start, "postcombat_main", "you", timeline, settings={"stopWhenYouCanAttack": False}
    )

    assert result["stopped"]["step"] == "postcombat_main", result
    assert "declare_blockers@3" in result["passed_through"], result


def test_no_attackers_means_no_stop() -> None:
    """Bob went to combat and declared nothing. There is nothing to block, so
    the run keeps going through his declare-blockers step."""
    start = board("end_step", 1)
    timeline = [
        board("declare_attackers", 2, "bob"),
        board("declare_blockers", 2, "bob"),
        board("combat_damage", 2, "bob"),
    ] + turn_cycle(3, "alice")

    result = run_until_stop(start, "precombat_main", "you", timeline)

    assert result["stopped"]["step"] == "precombat_main", result
    assert result["stopped"]["turn"] == 3
    assert "declare_blockers@2" in result["passed_through"], result


def test_it_does_not_fire_in_other_steps_of_a_combat_you_are_defending() -> None:
    """The attacks list is still populated in combat damage and end of combat.
    Only the step where the game asks you to block is a stopping point."""
    start = board("end_step", 1)
    timeline = [
        board("combat_damage", 2, "bob", combat={"attacks": TWO_ATTACKS}),
        board("end_of_combat", 2, "bob", combat={"attacks": TWO_ATTACKS}),
        board("postcombat_main", 2, "bob", combat={"attacks": TWO_ATTACKS}),
    ] + turn_cycle(3, "alice")

    result = run_until_stop(start, "precombat_main", "you", timeline)

    assert result["stopped"]["turn"] == 3, result
    assert result["stopped"]["step"] == "precombat_main"


def test_unticking_the_setting_lets_the_run_continue() -> None:
    """A player who wants to run through the opponent's whole turn can untick
    it, exactly as they can for the stack."""
    start = board("end_step", 1)
    timeline = [
        board("declare_attackers", 2, "bob", combat={"attacks": TWO_ATTACKS}),
        under_attack(2),
        board("combat_damage", 2, "bob", combat={"attacks": TWO_ATTACKS}),
    ] + turn_cycle(3, "alice")

    result = run_until_stop(
        start, "precombat_main", "you", timeline, settings={"stopWhenAttacked": False}
    )

    assert result["stopped"]["turn"] == 3, result
    assert result["stopped"]["step"] == "precombat_main"
    assert "declare_blockers@2" in result["passed_through"], result


def test_the_toggle_really_writes_the_setting() -> None:
    """`toggleStopWhenAttackedSetting` is what the checkbox calls, and it is
    the same shape as `toggleStopOnStackSetting`."""
    script = f"""
    const banner = {{ hidden: true, innerHTML: "" }};
    globalThis.document = {{
      activeElement: null,
      body: {{}},
      addEventListener() {{}},
      querySelector: () => banner,
      querySelectorAll: () => [],
    }};
    const {{ game }} = require({json.dumps(str(APP_JS))});
    const before = game.settings.stopWhenAttacked;
    toggleStopWhenAttackedSetting(false);
    const off = game.settings.stopWhenAttacked;
    toggleStopWhenAttackedSetting(true);
    process.stdout.write(JSON.stringify({{
      before, off, on: game.settings.stopWhenAttacked,
      exported: typeof toggleStopWhenAttackedSetting,
    }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    result = json.loads(proc.stdout)

    assert result["exported"] == "function", result
    assert result["before"] is True
    assert result["off"] is False
    assert result["on"] is True


def test_the_checkbox_is_rendered_next_to_the_stop_on_stack_one() -> None:
    """Both settings live in the Priority group, and the new one is wired to
    its toggle rather than assigning `game.settings` inline."""
    state = {
        "you": "alice",
        "priority_holder": "alice",
        "active_player": "alice",
        "turn_number": 1,
        "step": "precombat_main",
        "stack": [],
        "hands": {"alice": []},
        "play_permissions": {"alice": []},
        "battlefield": [],
        "pending_decision": None,
        "winner": None,
        "drawn": None,
        "combat": {"attacks": []},
    }

    html = action_bar_html(state)

    assert "toggleStopWhenAttackedSetting" in html, html
    assert "stop when you are being attacked" in html, html
    # Still in the same group as the setting it sits beside, and that one is
    # untouched.
    assert "toggleStopOnStackSetting" in html, html
    assert "stop when anything lands on the stack" in html, html
    priority_group = html.split("Priority")[1].split('<div class="label">')[0]
    assert "toggleStopWhenAttackedSetting" in priority_group, priority_group
    # Each safe default is checked by default, so a player who never opens the
    # panel gets all of them. Asserted by name rather than by counting checked
    # boxes: a count breaks on the next setting added and says nothing about
    # which one is wrong, which is exactly what happened when the
    # declare-attackers setting arrived.
    for toggle in (
        "toggleStopOnStackSetting",
        "toggleStopWhenAttackedSetting",
        "toggleStopWhenYouCanAttackSetting",
    ):
        assert f'type="checkbox" checked onchange="{toggle}' in priority_group, priority_group


# -- the three existing stop conditions, and the next-occurrence guard -------


def test_the_game_ending_still_stops_the_run() -> None:
    result = run_until_stop(
        board("precombat_main", 1),
        "precombat_main",
        "you",
        [board("precombat_main", 1, winner="bob")],
    )

    assert result["stopped"]["reason"] == "game over - bob won"


def test_a_decision_for_this_seat_still_stops_the_run() -> None:
    asking = board(
        "precombat_main",
        1,
        pending_decision={"player": "alice", "prompt": "look at the top four cards"},
    )

    result = run_until_stop(board("precombat_main", 1), "precombat_main", "you", [asking])

    assert "a decision is waiting for you" in result["stopped"]["reason"], result


def test_a_decision_for_the_other_seat_still_does_not_stop_the_run() -> None:
    """The condition is addressed-to-you, not any decision at all."""
    timeline = [
        board("draw", 2, "bob", pending_decision={"player": "bob", "prompt": "scry 1"}),
    ] + turn_cycle(3, "alice")

    result = run_until_stop(board("end_step", 1), "precombat_main", "you", timeline)

    assert result["stopped"]["turn"] == 3, result


def test_something_landing_on_the_stack_still_stops_the_run() -> None:
    timeline = [
        board("beginning_of_combat", 1, stack=[{"id": "spell-1", "name": "Lightning Bolt"}])
    ]

    result = run_until_stop(
        board("precombat_main", 1),
        "precombat_main",
        "you",
        timeline,
        settings={"stopOnStack": True},
    )

    assert result["stopped"]["step"] == "beginning_of_combat", result
    assert "Lightning Bolt" in result["stopped"]["reason"]


def test_unticking_stop_on_stack_still_lets_the_run_continue() -> None:
    timeline = [
        board("beginning_of_combat", 1, stack=[{"id": "spell-1", "name": "Lightning Bolt"}]),
    ] + turn_cycle(3, "alice")

    result = run_until_stop(
        board("precombat_main", 1),
        "precombat_main",
        "you",
        timeline,
        settings={"stopOnStack": False},
    )

    assert result["stopped"]["turn"] == 3, result


def test_reaching_the_destination_still_stops_the_run() -> None:
    timeline = [board(step, 1) for step in TURN_STEPS[TURN_STEPS.index("draw") :]]

    # `stopWhenYouCanAttack` off: this timeline crosses alice's own
    # declare-attackers step, which that setting stops for by design, and this
    # test is about the destination check rather than that one.
    result = run_until_stop(
        board("upkeep", 1), "end_step", "you", timeline, settings={"stopWhenYouCanAttack": False}
    )

    assert result["stopped"]["reason"] == "reached your end step", result
    assert result["stopped"]["step"] == "end_step"


def test_the_next_occurrence_guard_still_holds() -> None:
    """Standing in your own precombat main, "until my main 1" still means the
    next one - a whole turn cycle later, with the new condition never firing
    because nobody attacked."""
    start = board("precombat_main", 1)
    timeline = (
        [board(step, 1) for step in TURN_STEPS[TURN_STEPS.index("precombat_main") :]]
        + turn_cycle(2, "bob")
        + turn_cycle(3, "alice")
    )

    # `stopWhenYouCanAttack` off: this timeline crosses alice's own
    # declare-attackers step twice, which that setting stops for by design, and
    # this test is about the next-occurrence guard rather than that one.
    result = run_until_stop(
        start, "precombat_main", "you", timeline, settings={"stopWhenYouCanAttack": False}
    )

    assert result["stopped"]["turn"] == 3, result
    assert result["stopped"]["step"] == "precombat_main"
    assert "precombat_main@1" in result["passed_through"]


def test_being_attacked_beats_the_destination_when_both_are_true() -> None:
    """A run aimed at "the opponent's declare blockers" reaches its own target
    in the same state that the attack is visible in. The destination check runs
    first and wins, so the banner says where the run went rather than shouting
    about the attack - and either way the run has stopped, which is the point."""
    timeline = [under_attack(2)]

    result = run_until_stop(board("end_step", 1), "declare_blockers", "opponent", timeline)

    assert result["stopped"] is not None
    assert result["stopped"]["reason"] == "reached your opponent's declare blockers"
