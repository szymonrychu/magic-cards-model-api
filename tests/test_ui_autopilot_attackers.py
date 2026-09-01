"""The auto-pass run must not walk you through your own declare-attackers
step, run against the real `ui/app.js`.

**The bug.** The sibling of the one `tests/test_ui_autopilot_blockers.py`
covers, found while fixing it. The two combat declarations are symmetric and
the autopilot could see neither:

- Declaring attackers is a turn-based action. CR 508.1, verified with
  `knowledge_search(type="rules")` in the session that wrote this file: "First,
  the active player declares attackers. This turn-based action doesn't use the
  stack." CR 703.4i puts it immediately after the step begins. So nothing lands
  on the stack and `settings.stopOnStack` cannot see it.
- `src/magic_cards_model_api/decisions.py` keeps `DECLARE_ATTACKERS` out of
  `ANSWERABLE_KINDS` alongside `DECLARE_BLOCKERS`, because attacking has its
  own action (`actions/attack`) rather than the decision mailbox. So
  `state.pending_decision` is never set for it either.

The consequence is milder than the blockers one and is not overstated here:
passing through your own declare-attackers step loses you an attack, not the
game. What makes it worth stopping for is that it is silent - the engine's
`_ask_declaration` (`machines/decisions.py`) falls through to `default_answer`,
documented as "an unscripted player declares nobody, which is always legal", so
the turn simply produces no attack and nothing anywhere says why.

**The decision this file puts on record.** A run aimed past combat - "until my
main 2", "until my end step" - now stops in declare attackers every turn. That
is deliberate, not nagging: the setting defaults on so the player is asked
before the chance goes by, and one click clears it for the session.
`test_a_run_aimed_past_combat_is_stopped_in_combat_every_turn` and
`test_unticking_the_setting_lets_a_run_go_past_your_own_combat` are the pair
that pin it.

Like the blockers file, this drives the shipped functions under Node - the
exact file the browser is served, no build step, no Python reimplementation of
the logic under test.
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

#: The reason the new condition returns, in full - a test that only matched a
#: substring would not notice it changing under the player.
ATTACK_REASON = "your declare attackers step - declare attackers or carry on"

#: An attack as `serialize.py` writes it into `state.combat.attacks`.
ONE_ATTACK = [{"attacker": "att-1", "defender": "bob", "blockers": []}]


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


def panel_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
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
    state.update(overrides)
    return state


# -- the hole itself ---------------------------------------------------------


def test_a_run_stops_in_your_own_declare_attackers_step() -> None:
    """Alice auto-passes from her upkeep towards her end step. Her own combat
    is on the way, and the run must stop there rather than silently declaring
    nobody."""
    start = board("upkeep", 3)
    timeline = [board(step, 3) for step in TURN_STEPS[TURN_STEPS.index("draw") :]]

    result = run_until_stop(start, "end_step", "you", timeline)

    assert result["stopped"] is not None, "the run passed through its own declare attackers"
    assert result["stopped"]["step"] == "declare_attackers", result
    assert result["stopped"]["turn"] == 3
    assert result["stopped"]["active"] == "alice"
    assert result["stopped"]["reason"] == ATTACK_REASON
    assert "declare_blockers@3" not in result["passed_through"], result


def test_it_stops_whether_or_not_you_have_anything_to_attack_with() -> None:
    """The condition is the step and the seat, nothing else. Summoning
    sickness, defender, tap state and CR 508's restrictions and requirements
    are the engine's business, and an empty board is indistinguishable here
    from a board full of creatures that cannot attack - so the run stops and
    lets the player look."""
    start = board("upkeep", 3)
    empty_board = board("declare_attackers", 3, battlefield=[])

    result = run_until_stop(start, "end_step", "you", [empty_board])

    assert result["stopped"]["reason"] == ATTACK_REASON, result


def test_the_setting_defaults_on() -> None:
    """Same reason the other two do: the safe default is the one that does not
    let the chance go by without asking."""
    result = run_until_stop(board("upkeep", 1), "end_step", "you", [])

    assert result["defaults"]["stopWhenYouCanAttack"] is True, result["defaults"]


# -- the decision on record: a run aimed past combat is stopped in combat ----


def test_a_run_aimed_past_combat_is_stopped_in_combat_every_turn() -> None:
    """ "Until my main 2" crosses your own declare-attackers step by design, and
    is stopped there. This is the intended behaviour, not an accident: the
    default is on so that the attack is offered rather than skipped, and the
    next test is the escape hatch. Restarting the run from the stop carries on
    to the original destination, so nothing is unreachable."""
    start = board("upkeep", 4)
    timeline = [board(step, 4) for step in TURN_STEPS[TURN_STEPS.index("draw") :]]

    first = run_until_stop(start, "postcombat_main", "you", timeline)
    assert first["stopped"]["step"] == "declare_attackers", first

    # The player looks, decides not to attack, and starts the run again from
    # where it stopped - the same click they made the first time.
    resumed = run_until_stop(
        board("declare_attackers", 4),
        "postcombat_main",
        "you",
        [board(step, 4) for step in TURN_STEPS[TURN_STEPS.index("declare_blockers") :]],
    )
    assert resumed["stopped"]["step"] == "postcombat_main", resumed
    assert resumed["stopped"]["turn"] == 4


def test_unticking_the_setting_lets_a_run_go_past_your_own_combat() -> None:
    """One click, and "until my end step" runs the whole turn including
    combat - exactly the shape of the other two settings."""
    start = board("upkeep", 3)
    timeline = [board(step, 3) for step in TURN_STEPS[TURN_STEPS.index("draw") :]]

    result = run_until_stop(
        start, "end_step", "you", timeline, settings={"stopWhenYouCanAttack": False}
    )

    assert result["stopped"]["step"] == "end_step", result
    assert "declare_attackers@3" in result["passed_through"], result


def test_the_setting_is_independent_of_the_blockers_one() -> None:
    """Turning off the attackers stop must not turn off the blockers stop: a
    player who never attacks still wants to be asked to block."""
    start = board("end_step", 1)
    timeline = [
        board("declare_attackers", 2, "bob", combat={"attacks": ONE_ATTACK}),
        board("declare_blockers", 2, "bob", combat={"attacks": ONE_ATTACK}),
    ] + turn_cycle(3, "alice")

    result = run_until_stop(
        start, "precombat_main", "you", timeline, settings={"stopWhenYouCanAttack": False}
    )

    assert result["stopped"]["step"] == "declare_blockers", result
    assert "declare blockers" in result["stopped"]["reason"]


def test_starting_a_run_inside_your_own_declare_attackers_step_is_not_a_no_op() -> None:
    """Clicking "auto-pass until my main 2" while standing in your own
    declare-attackers step is the player having looked at combat and chosen to
    move on. A condition that fired on that very state would stop the run
    before it moved at all - a dead button rather than a safeguard - so the
    occurrence the run started in is declined here exactly as it is for the
    destination check."""
    start = board("declare_attackers", 4)
    timeline = [board(step, 4) for step in TURN_STEPS[TURN_STEPS.index("declare_attackers") :]]

    result = run_until_stop(start, "postcombat_main", "you", timeline)

    assert result["stopped"]["step"] == "postcombat_main", result
    assert "declare_attackers@4" in result["passed_through"], result


def test_the_next_combat_is_still_stopped_for_after_that() -> None:
    """The stamp is cleared the moment the board leaves that occurrence, so
    declining the occurrence the run started in costs nothing beyond it."""
    start = board("declare_attackers", 4)
    timeline = (
        [board(step, 4) for step in ["declare_attackers", "declare_blockers", "combat_damage"]]
        + turn_cycle(5, "bob")
        + turn_cycle(6, "alice")
    )

    result = run_until_stop(start, "end_step", "you", timeline)

    assert result["stopped"]["step"] == "declare_attackers", result
    assert result["stopped"]["turn"] == 6
    assert result["stopped"]["reason"] == ATTACK_REASON
    assert "declare_attackers@4" in result["passed_through"], result


def test_the_same_holds_for_the_blockers_condition() -> None:
    """The guard is shared, not duplicated: starting a run while standing in a
    declare-blockers step you are defending - which is exactly where a player
    who has just declared their blocks is standing - does not stop it dead
    either.

    This one carries a regression in its own right rather than a side effect of
    the attackers work, and stands against commit 1b8bd30 (the blockers
    condition as first shipped, before the guard was shared): there,
    `stopWhenAttacked` fired on the very first state of the timeline and the
    run stopped at `declare_blockers@2` having moved nowhere. The attackers
    setting is switched off here precisely so that nothing but the blockers
    guard can be what makes this pass.
    """
    start = board("declare_blockers", 2, "bob", combat={"attacks": ONE_ATTACK})
    timeline = [
        board("declare_blockers", 2, "bob", combat={"attacks": ONE_ATTACK}),
        board("combat_damage", 2, "bob", combat={"attacks": ONE_ATTACK}),
    ] + turn_cycle(3, "alice")

    result = run_until_stop(
        start, "precombat_main", "you", timeline, settings={"stopWhenYouCanAttack": False}
    )

    assert result["stopped"]["step"] == "precombat_main", result
    assert result["stopped"]["turn"] == 3
    assert "declare_blockers@2" in result["passed_through"], result


# -- and only when it should ------------------------------------------------


def test_the_defending_player_is_not_stopped_in_declare_attackers() -> None:
    """Bob is attacking. Alice has no declaration to make in his
    declare-attackers step, so her run carries on through it - and is stopped
    one step later by the blockers condition, which is the one that applies to
    her."""
    start = board("end_step", 1)
    timeline = [
        board("declare_attackers", 2, "bob", combat={"attacks": ONE_ATTACK}),
        board("declare_blockers", 2, "bob", combat={"attacks": ONE_ATTACK}),
    ]

    result = run_until_stop(start, "precombat_main", "you", timeline)

    assert result["stopped"]["step"] == "declare_blockers", result
    assert result["stopped"]["reason"] != ATTACK_REASON
    assert "declare_attackers@2" in result["passed_through"], result


def test_it_does_not_fire_in_the_combat_steps_after_declare_attackers() -> None:
    """Your attackers are declared and `state.combat.attacks` stays populated
    through the rest of your combat. Only the step where the declaration is
    actually made is a stopping point."""
    start = board("upkeep", 3)
    timeline = [
        board("declare_blockers", 3, combat={"attacks": ONE_ATTACK}),
        board("combat_damage", 3, combat={"attacks": ONE_ATTACK}),
        board("end_of_combat", 3, combat={"attacks": ONE_ATTACK}),
        board("postcombat_main", 3, combat={"attacks": ONE_ATTACK}),
        board("end_step", 3, combat={"attacks": ONE_ATTACK}),
    ]

    result = run_until_stop(start, "end_step", "you", timeline)

    assert result["stopped"]["step"] == "end_step", result
    assert "combat_damage@3" in result["passed_through"], result


def test_it_does_not_fire_outside_combat_at_all() -> None:
    start = board("upkeep", 3)
    timeline = [board(step, 3) for step in ["draw", "precombat_main", "beginning_of_combat"]]

    result = run_until_stop(start, "end_step", "you", timeline)

    assert result["stopped"] is None, result
    assert result["passed_through"] == [
        "draw@3",
        "precombat_main@3",
        "beginning_of_combat@3",
    ], result


def test_a_second_combat_phase_stops_the_run_again() -> None:
    """CR 500.8's added phase can give one turn a second declare-attackers
    step. It is a second chance to attack, so it is a second stopping point -
    the condition is a property of the state, with no per-run memory to make
    it fire once."""
    start = board("declare_blockers", 5)
    timeline = [
        board("combat_damage", 5, combat={"attacks": ONE_ATTACK}),
        board("end_of_combat", 5, combat={"attacks": ONE_ATTACK}),
        # ... and the extra combat phase begins.
        board("beginning_of_combat", 5),
        board("declare_attackers", 5),
    ]

    result = run_until_stop(start, "end_step", "you", timeline)

    assert result["stopped"]["step"] == "declare_attackers", result
    assert result["stopped"]["reason"] == ATTACK_REASON


# -- the toggle and the panel ------------------------------------------------


def test_the_toggle_really_writes_the_setting() -> None:
    """`toggleStopWhenYouCanAttackSetting` is what the checkbox calls, and it
    is the same shape as the other two toggles."""
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
    const before = game.settings.stopWhenYouCanAttack;
    toggleStopWhenYouCanAttackSetting(false);
    const off = game.settings.stopWhenYouCanAttack;
    toggleStopWhenYouCanAttackSetting(true);
    process.stdout.write(JSON.stringify({{
      before, off, on: game.settings.stopWhenYouCanAttack,
      exported: typeof toggleStopWhenYouCanAttackSetting,
      // The other two are untouched by it.
      stack: game.settings.stopOnStack,
      attacked: game.settings.stopWhenAttacked,
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
    assert result["stack"] is True
    assert result["attacked"] is True


def test_all_three_checkboxes_sit_together_in_the_priority_group() -> None:
    """The panel has to read as one consistent set rather than three
    one-offs."""
    html = action_bar_html(panel_state())

    priority_group = html.split("Priority")[1].split('<div class="label">')[0]
    assert "toggleStopOnStackSetting" in priority_group, priority_group
    assert "toggleStopWhenAttackedSetting" in priority_group, priority_group
    assert "toggleStopWhenYouCanAttackSetting" in priority_group, priority_group
    assert "stop when anything lands on the stack" in priority_group
    assert "stop when you are being attacked" in priority_group
    assert "stop when you could attack" in priority_group
    # All three checked by default, so a player who never opens the panel gets
    # all three protections.
    assert priority_group.count('type="checkbox" checked') == 3, priority_group


def test_the_rest_of_the_priority_group_is_untouched() -> None:
    html = action_bar_html(panel_state())

    assert 'id="autopilot-target"' in html, html
    assert "startAutopilotFromPicker()" in html, html
    assert "Auto-pass until" in html, html
    # And the step's own button still renders in the step it belongs to.
    assert "Declare attackers" in action_bar_html(panel_state(step="declare_attackers"))


# -- all four conditions coexist ---------------------------------------------


def test_the_game_ending_still_wins() -> None:
    """Checked first, and it must stay first: a finished game has no attack to
    declare."""
    over = board("declare_attackers", 3, winner="bob")

    result = run_until_stop(board("upkeep", 3), "end_step", "you", [over])

    assert result["stopped"]["reason"] == "game over - bob won", result


def test_a_decision_for_this_seat_still_wins_in_the_same_step() -> None:
    """A question addressed to you outranks "you could attack" - it names
    something specific, where this condition only names the step."""
    asking = board(
        "declare_attackers",
        3,
        pending_decision={"player": "alice", "prompt": "order these triggers"},
    )

    result = run_until_stop(board("upkeep", 3), "end_step", "you", [asking])

    assert "a decision is waiting for you" in result["stopped"]["reason"], result


def test_a_decision_for_the_other_seat_still_does_not_stop_the_run() -> None:
    timeline = [
        board("draw", 2, "bob", pending_decision={"player": "bob", "prompt": "scry 1"}),
    ] + turn_cycle(3, "alice")

    result = run_until_stop(board("end_step", 1), "precombat_main", "you", timeline)

    assert result["stopped"]["turn"] == 3, result
    assert result["stopped"]["step"] == "precombat_main"


def test_reaching_the_destination_still_wins_when_it_is_this_very_step() -> None:
    """ "Until my attack" stops with the destination's own wording, not the new
    condition's - the run went where it was told."""
    timeline = [board(step, 3) for step in TURN_STEPS[TURN_STEPS.index("draw") :]]

    result = run_until_stop(board("upkeep", 3), "declare_attackers", "you", timeline)

    assert result["stopped"]["step"] == "declare_attackers", result
    assert result["stopped"]["reason"] == "reached your declare attackers", result


def test_something_landing_on_the_stack_still_stops_the_run() -> None:
    timeline = [
        board("beginning_of_combat", 1, stack=[{"id": "spell-1", "name": "Lightning Bolt"}])
    ]

    result = run_until_stop(
        board("precombat_main", 1),
        "postcombat_main",
        "you",
        timeline,
        settings={"stopOnStack": True},
    )

    assert result["stopped"]["step"] == "beginning_of_combat", result
    assert "Lightning Bolt" in result["stopped"]["reason"]


def test_being_attacked_still_stops_the_run() -> None:
    """The condition added last time still fires with this one in place."""
    timeline = [board("declare_blockers", 2, "bob", combat={"attacks": ONE_ATTACK})]

    result = run_until_stop(board("end_step", 1), "precombat_main", "you", timeline)

    assert result["stopped"]["reason"] == (
        "you are being attacked by 1 creature - declare blockers"
    ), result


def test_all_four_conditions_fire_in_one_run_one_after_another() -> None:
    """Turned off one at a time, the same timeline stops at each condition in
    turn - so all four are live simultaneously rather than one shadowing the
    rest."""
    start = board("end_step", 1)
    timeline = [
        board("upkeep", 2, "bob", stack=[{"id": "trig-1", "name": "Bitterblossom"}]),
        board("declare_blockers", 2, "bob", combat={"attacks": ONE_ATTACK}),
        board("declare_attackers", 3, "alice"),
        board("precombat_main", 3, "alice"),
    ]

    with_stack = run_until_stop(start, "precombat_main", "you", timeline)
    assert "Bitterblossom" in with_stack["stopped"]["reason"], with_stack

    no_stack = run_until_stop(
        start, "precombat_main", "you", timeline, settings={"stopOnStack": False}
    )
    assert "declare blockers" in no_stack["stopped"]["reason"], no_stack

    no_blocks = run_until_stop(
        start,
        "precombat_main",
        "you",
        timeline,
        settings={"stopOnStack": False, "stopWhenAttacked": False},
    )
    assert no_blocks["stopped"]["reason"] == ATTACK_REASON, no_blocks

    none_of_them = run_until_stop(
        start,
        "precombat_main",
        "you",
        timeline,
        settings={
            "stopOnStack": False,
            "stopWhenAttacked": False,
            "stopWhenYouCanAttack": False,
        },
    )
    assert none_of_them["stopped"]["step"] == "precombat_main", none_of_them
    assert none_of_them["stopped"]["turn"] == 3


def test_the_next_occurrence_guard_still_holds() -> None:
    """Standing in your own declare-attackers step, "until my attack" still
    means the next one - and the new condition, which is true of the very
    state the run started in, must not turn that into an instant no-op."""
    start = board("declare_attackers", 1)
    timeline = (
        [board(step, 1) for step in TURN_STEPS[TURN_STEPS.index("declare_attackers") :]]
        + turn_cycle(2, "bob")
        + turn_cycle(3, "alice")
    )

    result = run_until_stop(start, "declare_attackers", "you", timeline)

    assert result["stopped"]["turn"] == 3, result
    assert result["stopped"]["step"] == "declare_attackers"
    assert result["stopped"]["reason"] == "reached your declare attackers"
    assert "declare_attackers@1" in result["passed_through"], result
