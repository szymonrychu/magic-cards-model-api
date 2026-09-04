"""Which occurrence of a step "run until X" actually means, ported from the
old `ui/app.js` autopilot onto the new `ui/priority.js` verb model.

**The bug, as originally reported.** "If I'm on my precombat main and
specify to pass until precombat main, then I'm meaning the next one, not the
current one." The old `autopilotStopReason` stopped the run the instant
`state.step` matched, so naming the step you were already standing in was a
no-op: the banner appeared and vanished on the same poll tick and nothing
advanced.

The fix was the *occurrence*, not a skip. `startAutopilot` stamped the run
with the `(turn_number, step)` pair it began inside; `forgetStartingStep`
cleared the stamp the moment the board left that pair; and only while it was
set was that one occurrence declined as a stopping point. `ui/priority.js`
carries the same fix forward: `startRun` sets `stillAtStart: true`, and the
exported `forgetStartingStep(run, state)` clears it the same way - a pure
function this time, returning a new `run` rather than mutating one, so the
caller threads the result back in on every tick that did not stop (exactly
what `run_until_stop` below does, mirroring the old `tickAutopilot`).

CR 500.1 ("a turn consists of five phases, in this order: beginning,
precombat main, combat, postcombat main, and ending") and CR 500.8 were
verified with `knowledge_search(type="rules")` in the session that ported
this file.

Several old reason strings do not survive the port to the new model's terser,
uniform messages; each rewritten assertion below says why. One test
(`test_the_stop_button_ends_the_run_whatever_the_stamp_says`) tested a
`stopAutopilot()`/global-`game.autopilot` UI concept the verb model does not
have at all - a run is just a plain object the caller threads through calls,
and "stopping" it is simply not calling `stopReason` again - so it is
rewritten to pin the property that makes that true: `stopReason` and
`forgetStartingStep` are pure, with no state of their own to leak between
calls.
"""

from __future__ import annotations

from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node

#: One full turn cycle of the steps a run can stop at (untap and cleanup
#: never are).
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


def stops_for(target_step: str, who: str, *, can_attack_stop: bool = True) -> dict[str, list[str]]:
    mine: list[str] = []
    theirs: list[str] = []
    if who in ("you", "any"):
        mine.append(target_step)
    if who in ("opponent", "any"):
        theirs.append(target_step)
    if can_attack_stop and "declare_attackers" not in mine:
        mine.append("declare_attackers")
    return {"mine": mine, "theirs": theirs}


def run_until_stop(
    start: dict[str, Any],
    verb: str,
    stops: dict[str, list[str]],
    timeline: list[dict[str, Any]],
    *,
    castable: bool = False,
) -> dict[str, Any]:
    """Start a real run standing in `start`, then feed it `timeline` one
    board state at a time - the way a real poll loop feeds `stopReason` -
    and report the first state that stopped it. On every tick that does not
    stop it, `knownStackIds` is refreshed and `forgetStartingStep` is called,
    both threaded back into `run` for the next tick - the same two lines the
    old `tickAutopilot` ran once per poll tick."""
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
        run = { ...run, knownStackIds: (state.stack || []).map((o) => o.id) };
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
    # `declare_attackers` left out of `stops.mine`: this timeline crosses
    # alice's own declare-attackers step twice, which that stop covers by
    # design (see `tests/test_ui_autopilot_attackers.py`), and this test is
    # about which occurrence of the target the run stops at.
    stops = stops_for("precombat_main", "you", can_attack_stop=False)

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"] is not None, "the run never stopped at all"
    assert result["stopped"]["turn"] == 3, result
    assert result["stopped"]["step"] == "precombat_main", result
    # The old exact string was "reached your precombat main"; the new stops
    # panel says the same thing every checked stop says - a wording choice,
    # not a rule change, since the rule (stop at the *next* occurrence) is
    # what `turn == 3` and the two lines below actually pin.
    assert result["stopped"]["reason"] == "stop at your precombat main", result
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
    stops = stops_for("end_step", "you", can_attack_stop=False)

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["turn"] == 1, result
    assert result["stopped"]["step"] == "end_step", result


def test_the_same_step_in_the_opponents_turn_is_not_the_one_you_are_in() -> None:
    """The `who` dimension is untouched: standing in her own end step, alice
    asking for "opponent's end" stops in bob's, one turn later - the
    occurrence guard never fires, because that occurrence never matched
    `who`."""
    start = board("end_step", 1)
    timeline = [board("end_step", 1)] + turn_cycle(2, "bob")
    stops = stops_for("end_step", "opponent")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["turn"] == 2, result
    assert result["stopped"]["active"] == "bob", result
    # The old exact string named the picker's `who` choice literally
    # ("reached your opponent's end step"); the new stops panel says "their"
    # for the non-"mine" side, matching "your" for the other - a wording
    # choice, not a rule change.
    assert result["stopped"]["reason"] == "stop at their end step", result


def test_any_still_means_whichever_comes_first() -> None:
    """ "any" from alice's own upkeep skips only her current one and stops at
    bob's, the very next occurrence of the step in anyone's turn."""
    start = board("upkeep", 1)
    timeline = turn_cycle(1, "alice") + turn_cycle(2, "bob")
    # `declare_attackers` left out of `stops.mine`: this timeline crosses
    # alice's own declare-attackers step, and this test is about what "any"
    # means rather than that one.
    stops = stops_for("upkeep", "any", can_attack_stop=False)

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["turn"] == 2, result
    assert result["stopped"]["active"] == "bob", result


def test_an_opponents_step_you_are_standing_in_also_means_the_next_one() -> None:
    """The guard is about the occurrence, not about whose turn it is: sitting
    in bob's end step and asking for "opponent's end" runs on to bob's next
    one rather than stopping where it started."""
    start = board("end_step", 2, "bob")
    timeline = [board("end_step", 2, "bob")] + turn_cycle(3, "alice") + turn_cycle(4, "bob")
    # `declare_attackers` left out of `stops.mine`: alice's turn 3 lies
    # between bob's two end steps, so this timeline crosses her own
    # declare-attackers step; this test is about the occurrence guard rather
    # than that one.
    stops = stops_for("end_step", "opponent", can_attack_stop=False)

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["turn"] == 4, result
    assert result["stopped"]["active"] == "bob", result


# -- the existing stop conditions still win ----------------------------------


def test_something_landing_on_the_stack_still_stops_the_run_first() -> None:
    """The "not this occurrence" logic must never run past a stop condition:
    a spell arriving one step later stops the run there, long before the
    next precombat main."""
    start = board("precombat_main", 1)
    timeline = [
        board("beginning_of_combat", 1, stack=[{"id": "spell-1", "name": "Lightning Bolt"}]),
    ] + turn_cycle(3, "alice")
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "beginning_of_combat", result
    # No card name in the new message - a wording choice, not a rule change.
    assert result["stopped"]["reason"] == "something new is on the stack", result


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
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(start, "until_castable", stops, [asking])

    assert result["stopped"] is not None
    assert result["stopped"]["reason"] == "a decision is pending"


def test_the_game_ending_still_stops_the_run_first() -> None:
    start = board("precombat_main", 1)
    over = board("precombat_main", 1, winner="bob")
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(start, "until_castable", stops, [over])

    assert result["stopped"]["reason"] == "the game is over"


def test_the_stop_button_ends_the_run_whatever_the_stamp_says() -> None:
    """The old `Stop` button cleared `game.autopilot` outright, so the next
    tick had nothing to check. The new model has no `game.autopilot` and no
    `stopAutopilot()` - a `run` is a plain value the caller threads through
    calls, so "stopping" it is simply the caller not calling `stopReason`
    again, nothing to test on the module's own side.

    What is testable, and what made the old button trivially correct, is
    that `stopReason` and `forgetStartingStep` are pure: nothing about a
    finished run object leaks into a later, unrelated call. Two independent
    runs against the same module instance, one built and immediately
    discarded, must not affect the other."""
    discarded_run = run_module(
        "priority.js", "m.startRun('until_castable', start)", start=board("precombat_main", 1)
    )
    assert discarded_run is not None, "starting a run had no visible effect to discard"

    # A brand new run, unrelated to the one just discarded, still sees a
    # fresh `stillAtStart` and is not short-circuited by anything left over.
    fresh_reason = run_module(
        "priority.js",
        "m.stopReason(state, m.startRun('until_castable', state), stops, false)",
        state=board("precombat_main", 3),
        stops=stops_for("precombat_main", "you"),
    )
    assert fresh_reason is None, "a discarded run leaked into a fresh one"


def test_a_second_occurrence_inside_one_turn_is_a_real_stopping_point() -> None:
    """CR 500.8 lets an effect add a phase to a turn, so one turn can hold two
    declare-attackers steps. The run started in the first; the second is a
    different occurrence and stops it, even though the turn number never
    changed - this is exactly what `forgetStartingStep` exists for."""
    start = board("declare_attackers", 5)
    timeline = [
        board("declare_attackers", 5),
        board("declare_blockers", 5),
        board("combat_damage", 5),
        # ... and the extra combat phase begins.
        board("beginning_of_combat", 5),
        board("declare_attackers", 5),
    ]
    stops = stops_for("declare_attackers", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"] is not None, "the extra phase's step was skipped too"
    assert result["stopped"]["step"] == "declare_attackers"
    assert result["stopped"]["turn"] == 5
    assert result["passed_through"].count("declare_attackers@5") == 1, result
