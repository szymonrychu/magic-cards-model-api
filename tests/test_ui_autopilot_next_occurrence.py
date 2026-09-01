"""Which occurrence of a step "auto-pass until X" actually means, run against
the real `ui/app.js`.

**The bug, as reported.** "If I'm on my precombat main and specify to pass
until precombat main, then I'm meaning the next one, not the current one."
`autopilotStopReason` stopped the run the instant `state.step` matched, so
naming the step you were already standing in was a no-op: the banner appeared
and vanished on the same poll tick and nothing advanced.

The fix is the *occurrence*, not a skip. `startAutopilot` stamps the run with
the `(turn_number, step)` pair it began inside; `forgetStartingStep` clears the
stamp the moment the board leaves that pair; and only while it is set is that
one occurrence declined as a stopping point. So a target the player was not
already standing in never matches the stamp at all and behaves exactly as it
did before, and a second occurrence of the same step inside the same turn -
which CR 500.8's added phase can produce, a second combat phase bringing a
second declare-attackers step - is a real stopping point rather than a
skipped one. Nothing is skipped unconditionally.

This drives the real shipped functions under Node - `startAutopilot` to build
the run exactly as the browser's picker does, then `autopilotStopReason` over
a sequence of board states, which is precisely what `tickAutopilot` consults
once per `poll()` tick. `tests/test_ui_autopilot.py` covers the stop
conditions themselves; this file covers where the run ends.

CR 500.1 ("a turn consists of five phases, in this order: beginning, precombat
main, combat, postcombat main, and ending") and CR 500.8 were verified with
`knowledge_search(type="rules")` in the session that wrote this file.
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

#: One full turn cycle of the steps the picker offers, in order (untap and
#: cleanup are not offered as targets, so a run never stops in them).
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


def board(step: str, turn: int, active: str = "alice", **overrides: Any) -> dict[str, Any]:
    state = {
        "you": "alice",
        "winner": None,
        "drawn": False,
        "pending_decision": None,
        "step": step,
        "turn_number": turn,
        "active_player": active,
        "priority_holder": "alice",
        "stack": [],
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

    The stop check is the shipped `autopilotStopReason`; the run object is the
    one the shipped `startAutopilot` builds. Neither is re-described here.
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
      // Exactly what `tickAutopilot` does with a state it did not stop on -
      // both lines are the shipped ones, not a re-description of them.
      game.autopilot.knownStackIds = new Set((state.stack || []).map((o) => o.id));
      forgetStartingStep(game.autopilot, state);
      seen.push(state.step + "@" + state.turn_number);
    }}
    process.stdout.write(JSON.stringify({{ stopped, passed_through: seen }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def turn_cycle(turn: int, active: str, **overrides: Any) -> list[dict[str, Any]]:
    return [board(step, turn, active, **overrides) for step in TURN_STEPS]


# -- the reported bug --------------------------------------------------------


def test_naming_the_step_you_are_standing_in_means_the_next_one() -> None:
    """Standing in alice's own precombat main on turn 1, "until my main 1"
    runs the rest of turn 1, all of bob's turn 2, and stops in alice's
    precombat main on turn 3."""
    start = board("precombat_main", 1)
    timeline = (
        # The rest of alice's own turn, from where she is standing.
        [board(step, 1) for step in TURN_STEPS[TURN_STEPS.index("precombat_main") :]]
        + turn_cycle(2, "bob")
        + turn_cycle(3, "alice")
    )

    result = run_until_stop(start, "precombat_main", "you", timeline)

    assert result["stopped"] is not None, "the run never stopped at all"
    assert result["stopped"]["turn"] == 3, result
    assert result["stopped"]["step"] == "precombat_main"
    assert result["stopped"]["reason"] == "reached your precombat main"
    # It really did go the long way round rather than skipping a step or two.
    assert "precombat_main@1" in result["passed_through"]
    assert "end_step@2" in result["passed_through"]


def test_a_step_you_are_not_standing_in_still_stops_at_its_first_occurrence() -> None:
    """The off-by-one fix must not cost every other target a whole turn: from
    alice's precombat main, "until my end step" stops in the very next end
    step, on the same turn, exactly as it always did."""
    start = board("precombat_main", 1)
    timeline = [board(step, 1) for step in TURN_STEPS[TURN_STEPS.index("precombat_main") :]] + (
        turn_cycle(2, "bob")
    )

    result = run_until_stop(start, "end_step", "you", timeline)

    assert result["stopped"]["turn"] == 1, result
    assert result["stopped"]["step"] == "end_step"


def test_the_same_step_in_the_opponents_turn_is_not_the_one_you_are_in() -> None:
    """The `who` dimension is untouched: standing in her own end step, alice
    asking for "opponent's end" stops in bob's, one turn later - the
    occurrence guard never fires, because that occurrence never matched
    `who`."""
    start = board("end_step", 1)
    timeline = [board("end_step", 1)] + turn_cycle(2, "bob")

    result = run_until_stop(start, "end_step", "opponent", timeline)

    assert result["stopped"]["turn"] == 2, result
    assert result["stopped"]["active"] == "bob"
    assert result["stopped"]["reason"] == "reached your opponent's end step"


def test_any_still_means_whichever_comes_first() -> None:
    """ "any" from alice's own upkeep skips only her current one and stops at
    bob's, the very next occurrence of the step in anyone's turn."""
    start = board("upkeep", 1)
    timeline = turn_cycle(1, "alice") + turn_cycle(2, "bob")

    result = run_until_stop(start, "upkeep", "any", timeline)

    assert result["stopped"]["turn"] == 2, result
    assert result["stopped"]["active"] == "bob"


def test_an_opponents_step_you_are_standing_in_also_means_the_next_one() -> None:
    """The guard is about the occurrence, not about whose turn it is: sitting
    in bob's end step and asking for "opponent's end" runs on to bob's next
    one rather than stopping where it started."""
    start = board("end_step", 2, "bob")
    timeline = [board("end_step", 2, "bob")] + turn_cycle(3, "alice") + turn_cycle(4, "bob")

    result = run_until_stop(start, "end_step", "opponent", timeline)

    assert result["stopped"]["turn"] == 4, result
    assert result["stopped"]["active"] == "bob"


# -- the existing stop conditions still win ----------------------------------


def test_something_landing_on_the_stack_still_stops_the_run_first() -> None:
    """The new "not this occurrence" logic must never run past a stop
    condition: a spell arriving one step later stops the run there, long
    before the next precombat main."""
    start = board("precombat_main", 1)
    timeline = [
        board("beginning_of_combat", 1, stack=[{"id": "spell-1", "name": "Lightning Bolt"}]),
    ] + turn_cycle(3, "alice")

    result = run_until_stop(
        start, "precombat_main", "you", timeline, settings={"stopOnStack": True}
    )

    assert result["stopped"]["step"] == "beginning_of_combat", result
    assert "Lightning Bolt" in result["stopped"]["reason"]


def test_a_decision_for_this_seat_still_stops_the_run_first() -> None:
    """Including one raised in the very step the run started in - being told
    to keep going past this occurrence is not a licence to keep passing while
    a question is waiting."""
    start = board("precombat_main", 1)
    asking = board(
        "precombat_main",
        1,
        pending_decision={"player": "alice", "prompt": "look at the top four cards"},
    )

    result = run_until_stop(start, "precombat_main", "you", [asking])

    assert result["stopped"] is not None
    assert "a decision is waiting for you" in result["stopped"]["reason"]


def test_the_game_ending_still_stops_the_run_first() -> None:
    start = board("precombat_main", 1)
    over = board("precombat_main", 1, winner="bob")

    result = run_until_stop(start, "precombat_main", "you", [over])

    assert result["stopped"]["reason"] == "game over - bob won"


def test_the_stop_button_ends_the_run_whatever_the_stamp_says() -> None:
    """`Stop` clears the run outright, so the next tick has nothing to check -
    the new logic cannot outlive it."""
    script = f"""
    const banner = {{ hidden: true, innerHTML: "" }};
    globalThis.document = {{
      activeElement: null,
      body: {{}},
      addEventListener() {{}},
      querySelector: () => banner,
      querySelectorAll: () => [],
    }};
    const {{ startAutopilot, autopilotStopReason, game }} = require({json.dumps(str(APP_JS))});
    game.state = {json.dumps(board("precombat_main", 1))};
    startAutopilot("precombat_main", "you");
    const running = game.autopilot !== null;
    stopAutopilot("stopped by you");
    process.stdout.write(JSON.stringify({{
      running,
      afterStop: game.autopilot,
      reason: game.autopilotStoppedReason,
      stillChecks: autopilotStopReason(
        {json.dumps(board("precombat_main", 3))}, game.autopilot, "alice", game.settings
      ),
    }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    result = json.loads(proc.stdout)

    assert result["running"] is True, "the run never started"
    assert result["afterStop"] is None, "Stop must clear the run outright"
    assert result["reason"] == "stopped by you"
    assert result["stillChecks"] is None


def test_a_second_occurrence_inside_one_turn_is_a_real_stopping_point() -> None:
    """CR 500.8 lets an effect add a phase to a turn, so one turn can hold two
    declare-attackers steps. The run started in the first; the second is a
    different occurrence and stops it, even though the turn number never
    changed."""
    start = board("declare_attackers", 5)
    timeline = [
        board("declare_attackers", 5),
        board("declare_blockers", 5),
        board("combat_damage", 5),
        # ... and the extra combat phase begins.
        board("beginning_of_combat", 5),
        board("declare_attackers", 5),
    ]

    result = run_until_stop(start, "declare_attackers", "you", timeline)

    assert result["stopped"] is not None, "the extra phase's step was skipped too"
    assert result["stopped"]["step"] == "declare_attackers"
    assert result["stopped"]["turn"] == 5
    assert result["passed_through"].count("declare_attackers@5") == 1, result
