"""Shared poll-loop driver for `ui/priority.js`'s `stopReason`, used by the
run-based tests in `tests/test_ui_autopilot_attackers.py`,
`tests/test_ui_autopilot_blockers.py`, and
`tests/test_ui_autopilot_next_occurrence.py` - the three files whose tests
feed a real run a whole timeline of board states rather than checking a
single `stopReason` call.

Pulled out once it was identical in all three (this is the same `stops_for`
and `run_until_stop` each file used to define locally).
"""

from __future__ import annotations

from typing import Any

from ui_node import run_module


def stops_for(
    target_step: str,
    who: str,
    *,
    can_attack_stop: bool = True,
    on_stack: bool = True,
) -> dict[str, Any]:
    """The old picker's `(target_step, who)` plus `stopWhenYouCanAttack` and
    `stopOnStack`, expressed as a `stops` object: the destination goes on the
    side `who` names, `declare_attackers` goes on `mine` (your own attack step
    is never conditional on any destination's `who`) when the safety net is on,
    and `onStack` is the stack tick that sits beside both step lists."""
    mine: list[str] = []
    theirs: list[str] = []
    if who in ("you", "any"):
        mine.append(target_step)
    if who in ("opponent", "any"):
        theirs.append(target_step)
    if can_attack_stop and "declare_attackers" not in mine:
        mine.append("declare_attackers")
    return {"mine": mine, "theirs": theirs, "onStack": on_stack}


def run_until_stop(
    start: dict[str, Any],
    verb: str,
    stops: dict[str, Any],
    timeline: list[dict[str, Any]],
    *,
    castable: bool = False,
) -> dict[str, Any]:
    """Start a real run standing in `start`, then feed it `timeline` one
    board state at a time - the way a real poll loop feeds `stopReason` -
    and report the first state that stopped it. On every tick that does not
    stop it, `recordPass` refreshes `knownStackIds` (and counts the pass) and
    `forgetStartingStep` is called, both threaded back into `run` for the
    next tick - the same two pieces of bookkeeping the old `tickAutopilot`
    ran once per poll tick."""
    expr = """
    (() => {
      let run = m.startRun(verb, start);
      const seen = [];
      let stopped = null;
      for (const state of timeline) {
        const reason = m.stopReason(state, run, stops, castable);
        if (reason) {
          stopped = {
            reason, step: state.step, turn: state.turn_number, active: state.active_player,
          };
          break;
        }
        run = m.recordPass(run, state);
        run = m.forgetStartingStep(run, state);
        seen.push(state.step + "@" + state.turn_number);
      }
      return { stopped, passed_through: seen };
    })()
    """
    return run_module(
        "priority.js",
        expr,
        start=start,
        verb=verb,
        stops=stops,
        timeline=timeline,
        castable=castable,
    )
