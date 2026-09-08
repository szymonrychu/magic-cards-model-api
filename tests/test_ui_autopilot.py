"""The priority-automation stop conditions, ported from the old
`autopilotStopReason`/`interpretPassOutcome` in `ui/app.js` onto the new
`ui/priority.js`'s `stopReason`/`interpretPassOutcome`.

The old picker ran to one named `(step, who)` destination under three
toggles (`stopOnStack`, `stopWhenAttacked`, `stopWhenYouCanAttack`). The new
model has two verbs and a `stops` panel: a destination is now `stops`
containing exactly that one step on the right side (`mine` for "you",
`theirs` for "opponent", both for "any"), and it is run by the verb
`pass_until`. `settings.stopOnStack` is the panel's own `onStack` tick, which
sits beside the two step lists because it can fire in any step and is live
alongside whatever steps are checked rather than instead of them.

Several old reason strings do not survive: the new messages are terser and
uniform across verbs (no card name, no prompt text, no winner's name), so
those assertions are rewritten to the new text with a note explaining why -
each one is a UI wording choice the verb model replaces, not a rule the old
code enforced. Where an old assertion only checked a substring that the new
wording still contains, it is left exactly as it was.

This still runs the real shipped `ui/priority.js` under Node via
`tests/ui_node.py`'s `run_module` - no build step, no Python
reimplementation of the logic under test.
"""

from __future__ import annotations

from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node


def base_state(**overrides: Any) -> dict:
    state = {
        "you": "alice",
        "winner": None,
        "drawn": False,
        "pending_decision": None,
        "step": "draw",
        "turn_number": 1,
        "active_player": "bob",
        "priority_holder": "bob",
        "stack": [],
        "combat": {"attacks": []},
    }
    state.update(overrides)
    return state


def call_stop_reason(
    state: dict,
    known_stack_ids: list[str] | None = None,
    verb: str = "pass_until",
    stops: dict | None = None,
    castable: bool = False,
) -> Any:
    """Calls the real `stopReason` against `state`, with a `run` built from a
    synthetic starting state that never coincides with any `state` this file
    passes in - its `step` ("cleanup") is not one any test below uses, so the
    "declined as the occurrence the run started in" guard `priority.js`
    added later (ported in `tests/test_ui_autopilot_next_occurrence.py`)
    never accidentally suppresses a stop this file is testing. Its
    `turn_number` mirrors `state`'s so nothing sees a turn change by
    surprise."""
    stops = stops if stops is not None else {"mine": [], "theirs": []}
    known_stack_ids = known_stack_ids or []
    start = base_state(
        step="cleanup",
        turn_number=state.get("turn_number", 1),
        active_player=state.get("active_player", "bob"),
        stack=[{"id": i} for i in known_stack_ids],
    )
    return run_module(
        "priority.js",
        "m.stopReason(state, m.startRun(verb, start), stops, castable)",
        state=state,
        start=start,
        verb=verb,
        stops=stops,
        castable=castable,
    )


UPKEEP_STOPS = {"mine": ["upkeep"], "theirs": []}


def test_module_exports_the_pure_functions_the_tests_drive() -> None:
    """The old test guarded `sendTargets`/`setHtml`/`aiStallNotice` too -
    other files' pure functions sharing `ui/app.js`'s single module surface.
    `ui/priority.js` is its own module with its own surface, so this now
    guards exactly what this file (and its four siblings) drives: nothing
    reachable here stops being exported without a test noticing."""
    exported = set(run_module("priority.js", "Object.keys(m)"))
    assert {
        "stopReason",
        "interpretPassOutcome",
        "startRun",
        "forgetStartingStep",
        "passButtonsFor",
        "DEFAULT_STOPS",
    } <= exported


# -- stop condition: a pending decision addressed to this seat --------------


def test_halts_on_a_pending_decision_for_this_seat() -> None:
    """The old message echoed the prompt (`"Pay 2 life?" in reason`); the new
    one is a fixed string with no prompt text - a UI wording choice, not a
    rule change, since the rule under test (a decision addressed to you
    halts the run) still holds and is what the assertion below checks."""
    state = base_state(pending_decision={"player": "alice", "kind": "may", "prompt": "Pay 2 life?"})
    reason = call_stop_reason(state, stops=UPKEEP_STOPS)
    assert reason == "a decision is pending"


def test_also_halts_on_a_decision_addressed_to_the_opponent() -> None:
    """This used to read `reason is None` - "only your own pending decision
    stops the run" - which was the live-lock bug: a paused command rolls the
    committed game back to before the attempt, so `priority_holder` can
    still read as this seat's even while the pause is on a DIFFERENT
    player's decision (a bob `pass-priority` that ends a step and pauses on
    alice's `declare_attackers`, say). Nothing can make progress until
    whoever the decision actually belongs to answers it, no matter whose
    priority the board still nominally shows - so this halts on any pending
    decision now, not only this seat's own, with a distinct reason string so
    a caller (or a human reading the toast) can tell the two apart."""
    state = base_state(pending_decision={"player": "bob", "kind": "may", "prompt": "Pay 2 life?"})
    reason = call_stop_reason(state, stops=UPKEEP_STOPS)
    assert reason == "waiting on another seat's decision"


# -- stop condition: the game ends -------------------------------------------


def test_halts_when_this_seat_wins() -> None:
    """The old message named the winner (`"alice" in reason`); the new one
    is the same fixed string for every ending - win, loss, or draw alike -
    another UI wording choice, not a rule change."""
    state = base_state(winner="alice")
    reason = call_stop_reason(state)
    assert reason == "the game is over"


def test_halts_when_the_opponent_wins() -> None:
    state = base_state(winner="bob")
    reason = call_stop_reason(state)
    assert reason == "the game is over"


def test_halts_on_a_draw() -> None:
    state = base_state(drawn=True)
    reason = call_stop_reason(state)
    assert reason == "the game is over"


# -- reaching the target: "pass until my upkeep" -----------------------------


def test_halts_on_reaching_the_players_own_upkeep() -> None:
    """The old exact string was "reached your upkeep"; the new stops panel
    reports "stop at your upkeep" for every checked stop, destination or not
    - terser and uniform, a wording choice, not a rule change. The rule
    (stopping exactly at your own named step) still holds, which is what
    this test is actually pinning."""
    state = base_state(step="upkeep", active_player="alice")
    reason = call_stop_reason(state, stops=UPKEEP_STOPS)
    assert reason == "stop at your upkeep"


def test_does_not_halt_on_the_opponents_upkeep() -> None:
    state = base_state(step="upkeep", active_player="bob")
    reason = call_stop_reason(state, stops=UPKEEP_STOPS)
    assert reason is None


# -- stop condition: something lands on the stack ----------------------------


def test_halts_on_a_new_stack_object_when_the_safe_default_is_on() -> None:
    """The old message named the card (`"Lightning Bolt" in reason`); the new
    one is a fixed string with no card name - a wording choice, not a rule
    change, since the rule (a new stack object halts the run) is what the
    exact-match assertion below still pins."""
    state = base_state(stack=[{"id": "spell-1", "name": "Lightning Bolt"}])
    reason = call_stop_reason(state, known_stack_ids=[], stops=UPKEEP_STOPS)
    assert reason == "something new is on the stack"


def test_does_not_halt_on_a_new_stack_object_when_the_setting_is_off() -> None:
    state = base_state(stack=[{"id": "spell-1", "name": "Lightning Bolt"}])
    reason = call_stop_reason(
        state, known_stack_ids=[], stops={**UPKEEP_STOPS, "onStack": False}
    )
    assert reason is None


def test_does_not_halt_on_a_stack_object_already_known() -> None:
    state = base_state(stack=[{"id": "spell-1", "name": "Lightning Bolt"}])
    reason = call_stop_reason(state, known_stack_ids=["spell-1"], stops=UPKEEP_STOPS)
    assert reason is None


def test_the_safe_default_is_stop_and_ask() -> None:
    """The `onStack` tick starts on, so a `stops` object that never mentions
    it (`UPKEEP_STOPS`, and every other one in this file) still checks the
    stack on every tick - the old `settings.stopOnStack: true` default,
    carried forward exactly."""
    state = base_state(stack=[{"id": "spell-1", "name": "Lightning Bolt"}])
    reason = call_stop_reason(state, known_stack_ids=[], stops=UPKEEP_STOPS)
    assert reason is not None


# -- no autopilot running -----------------------------------------------------


def test_there_is_no_null_run_case_to_return_early_from() -> None:
    """The old function's very first line, `if (!autopilot) return null`,
    only existed because a JS `null` stood in for "no automation is
    running" - a UI state, not a rule. The verb model does not carry that
    state through `stopReason` at all: a `run` is never optional, and a
    caller with nothing running simply never calls it. Asserted the only way
    that is still meaningful here - a real `run` still stops a finished
    game exactly like every other state does, with nothing special to
    early-return for."""
    reason = call_stop_reason(base_state(winner="alice"))
    assert reason == "the game is over"


# -- stop condition: the pass itself is refused ------------------------------


def test_interpret_pass_outcome_halts_on_a_refusal() -> None:
    """The old function returned a bare string or `null`; the new one
    returns `{ok, reason, ...}` so a caller branches on `.ok` rather than on
    string truthiness - a shape change, not a rule change: a refusal still
    halts the run, which `result["ok"] is False` pins."""
    result = run_module(
        "priority.js",
        'm.interpretPassOutcome({"allowed": false, "reason": "you do not have priority"})',
    )
    assert result["ok"] is False
    assert result["reason"] == "you do not have priority"


def test_interpret_pass_outcome_continues_on_success() -> None:
    result = run_module(
        "priority.js", 'm.interpretPassOutcome({"allowed": true, "outcome": "passed"})'
    )
    assert result["ok"] is True
    assert result["reason"] is None


def test_interpret_pass_outcome_is_not_ok_on_a_pause() -> None:
    """`allowed: null, paused: true` is what a `pass-priority` call gets back
    the moment it ends a step and the engine pauses on somebody's decision -
    `allowed !== false`, so the old code (`!res || res.allowed === false`)
    took this as a successful pass and looped forever re-issuing the
    identical no-op (the live-lock this fixes): nothing was committed, the
    pause is unchanged, and re-sending the same `pass-priority` reproduces
    the exact same pause every time. This is the regression pin - a caller
    that treats `ok: true` here would tick forever the same way."""
    result = run_module(
        "priority.js",
        'm.interpretPassOutcome({"allowed": null, "paused": true, "outcome": null})',
    )
    assert result["ok"] is False
    assert result["outcome"] is None


# -- reaching a *named* destination: "auto-pass until <step>" (feature 3) -----
#
# The old picker offered every step in the turn, for either player's turn.
# `who` says whose turn the step has to be in; the new `stops` panel does the
# same by which side (`mine`/`theirs`) the step is checked in.


def stops_for(step: str, who: str) -> dict:
    if who == "you":
        return {"mine": [step], "theirs": []}
    if who == "opponent":
        return {"mine": [], "theirs": [step]}
    return {"mine": [step], "theirs": [step]}  # "any"


def test_halts_on_reaching_a_named_step_in_your_own_turn() -> None:
    state = base_state(step="postcombat_main", active_player="alice")
    reason = call_stop_reason(state, stops=stops_for("postcombat_main", "you"))
    assert reason is not None
    assert "postcombat main" in reason


def test_does_not_halt_on_that_step_in_the_opponents_turn() -> None:
    state = base_state(step="postcombat_main", active_player="bob")
    assert call_stop_reason(state, stops=stops_for("postcombat_main", "you")) is None


def test_halts_on_a_named_step_in_the_opponents_turn() -> None:
    """The old reason named the picker's `who` choice literally
    (`"opponent" in reason`); the new stops panel says "their" for the
    non-"mine" side, matching "your" for the other - a wording choice, not
    a rule change, since the rule (this stop applies to the opponent's
    occurrence of the step) is what `reason is not None` still pins."""
    state = base_state(step="end_step", active_player="bob")
    reason = call_stop_reason(state, stops=stops_for("end_step", "opponent"))
    assert reason is not None
    assert "their" in reason


def test_does_not_halt_on_the_opponents_step_during_your_own_turn() -> None:
    state = base_state(step="end_step", active_player="alice")
    assert call_stop_reason(state, stops=stops_for("end_step", "opponent")) is None


def test_who_any_halts_in_either_players_turn() -> None:
    for active in ("alice", "bob"):
        state = base_state(step="upkeep", active_player=active)
        assert call_stop_reason(state, stops=stops_for("upkeep", "any")) is not None


def test_a_different_step_does_not_halt() -> None:
    state = base_state(step="draw", active_player="alice")
    assert call_stop_reason(state, stops=stops_for("upkeep", "you")) is None


def test_the_game_ending_outranks_the_destination() -> None:
    """Stop conditions are checked in order, and the game being over wins -
    a run must never keep passing into a finished game. The old assertion
    checked the winner's name was in the reason (`"bob" in reason"`); the
    new "the game is over" message is uniform across endings, so that part
    is a wording change, not a rule change - the ordering itself (game over
    beats the destination check) is what this test is really pinning, and it
    still holds: the exact string returned is the game-over one, not the
    "stop at your upkeep" one the destination would have produced."""
    state = base_state(step="upkeep", active_player="alice", winner="bob")
    reason = call_stop_reason(state, stops=stops_for("upkeep", "you"))
    assert reason == "the game is over"


def test_a_decision_outranks_the_destination() -> None:
    """Same ordering point as above, for the decision check: the old reason
    echoed the prompt; the new one is the fixed "a decision is pending"
    string, so the wording assertion is rewritten but the ordering (decision
    beats destination) still holds."""
    state = base_state(
        step="upkeep",
        active_player="alice",
        pending_decision={"player": "alice", "kind": "may", "prompt": "Pay 2 life?"},
    )
    reason = call_stop_reason(state, stops=stops_for("upkeep", "you"))
    assert reason == "a decision is pending"
