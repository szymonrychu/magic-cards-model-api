"""The run must not walk you through your own declare-attackers step, ported
from the old `ui/app.js` autopilot onto the new `ui/priority.js` verb model.

**The bug, as originally found.** The sibling of the one
`tests/test_ui_autopilot_blockers.py` covers:

- Declaring attackers is a turn-based action. CR 508.1, verified with
  `knowledge_search(type="rules")` in the session that ported this file:
  "First, the active player declares attackers. This turn-based action
  doesn't use the stack." CR 703.4i puts it immediately after the step
  begins. So nothing lands on the stack and a stack-only check cannot see
  it.
- `src/magic_cards_model_api/decisions.py` keeps `DECLARE_ATTACKERS` out of
  `ANSWERABLE_KINDS` alongside `DECLARE_BLOCKERS`, because attacking has its
  own action (`actions/attack`) rather than the decision mailbox. So
  `state.pending_decision` is never set for it either.

Passing through your own declare-attackers step loses you an attack, not the
game - the engine's `_ask_declaration` falls through to `default_answer`,
"an unscripted player declares nobody, which is always legal", so the turn
simply produces no attack and nothing anywhere says why.

**The port.** The old picker's `settings.stopWhenYouCanAttack` (default on)
becomes membership of `declare_attackers` in the new `stops.mine` - the same
safe default, expressed as data instead of a toggle. The old picker's named
destination (`autopilotTarget`, a `(step, who)` pair) becomes `stops`
containing exactly that one step on the matching side. Several old reason
strings do not survive: the new model collapses "reached the destination"
and "you can attack" into one uniform stops-panel message
("stop at your declare attackers"), and drops the card name, prompt text,
and winner's name from the others - a wording choice, not a rule change,
noted case by case below.

One fixture correction: the old `ONE_ATTACK`'s `defender` field was inert
under the old code (which checked only step, active player, and attacker
count - never who the attack actually named), so it was reused for both
"alice attacks bob" and "bob attacks alice" scenarios without consequence.
The new `stopReason`'s `attackedNow` checks `defender` directly, so getting
it right now matters; `ATTACK_ON_YOU` is `ONE_ATTACK` with the defender
corrected to `alice` for the scenarios that are actually about you being
attacked.

Like the blockers and next-occurrence files, this drives the shipped
`ui/priority.js` under Node via `tests/ui_node.py`'s `run_module` - no build
step, no Python reimplementation of the logic under test.
"""

from __future__ import annotations

from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node

#: The steps of one turn, in the order a run walks them (untap and cleanup
#: are never stop candidates).
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

#: Alice (the seat every test in this file plays) attacking bob - used only
#: where alice is the active player declaring the attack.
ONE_ATTACK = [{"attacker": "att-1", "defender": "bob", "blockers": []}]

#: Bob attacking alice - the "you are being attacked" shape, with the
#: defender corrected to the seat that is actually being attacked (see the
#: module docstring's note on this fixture).
ATTACK_ON_YOU = [{"attacker": "att-1", "defender": "alice", "blockers": []}]


def board(step: str, turn: int, active: str = "alice", **overrides: Any) -> dict[str, Any]:
    """A board state shaped like `GET /api/games/{id}`, with the fields
    `stopReason` reads."""
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


def stops_for(target_step: str, who: str, *, can_attack_stop: bool = True) -> dict[str, list[str]]:
    """The old picker's `(target_step, who)` plus `stopWhenYouCanAttack`,
    expressed as a `stops` object: the destination goes on the side `who`
    names, and `declare_attackers` goes on `mine` (your own attack step is
    never conditional on any destination's `who`) when the safety net is
    on."""
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
    and report the first state that stopped it. Mirrors exactly what the
    driver does with a state that did not stop it: refresh `knownStackIds`
    and call `forgetStartingStep`, both threaded back into `run` for the
    next tick, the same two lines the old `tickAutopilot` ran every poll."""
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


def panel_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "you": "alice",
        "priority_holder": "alice",
        "active_player": "alice",
        "turn_number": 1,
        "step": "precombat_main",
        "stack": [],
        "hands": {"alice": []},
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
    """Alice runs from her upkeep towards her end step. Her own combat is on
    the way, and the run must stop there rather than silently declaring
    nobody."""
    start = board("upkeep", 3)
    timeline = [board(step, 3) for step in TURN_STEPS[TURN_STEPS.index("draw") :]]
    stops = stops_for("end_step", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"] is not None, "the run passed through its own declare attackers"
    assert result["stopped"]["step"] == "declare_attackers", result
    assert result["stopped"]["turn"] == 3, result
    assert result["stopped"]["active"] == "alice", result
    # The old exact string was "your declare attackers step - declare
    # attackers or carry on"; the new stops panel says the same thing every
    # checked stop says - a wording choice, not a rule change.
    assert result["stopped"]["reason"] == "stop at your declare attackers", result
    assert "declare_blockers@3" not in result["passed_through"], result


def test_it_stops_whether_or_not_you_have_anything_to_attack_with() -> None:
    """The condition is the step and the seat, nothing else. Summoning
    sickness, defender, tap state and CR 508's restrictions and requirements
    are the engine's business, and an empty board is indistinguishable here
    from a board full of creatures that cannot attack - so the run stops and
    lets the player look."""
    start = board("upkeep", 3)
    empty_board = board("declare_attackers", 3, battlefield=[])
    stops = stops_for("end_step", "you")

    result = run_until_stop(start, "until_castable", stops, [empty_board])

    assert result["stopped"]["reason"] == "stop at your declare attackers", result


def test_the_setting_defaults_on() -> None:
    """The old default lived in `game.settings.stopWhenYouCanAttack`; its
    equivalent default now lives in `DEFAULT_STOPS.mine`, which already
    includes `declare_attackers` - the same safe default, as data instead of
    a toggle."""
    assert "declare_attackers" in run_module("priority.js", "m.DEFAULT_STOPS")["mine"]


# -- the decision on record: a run aimed past combat is stopped in combat ----


def test_a_run_aimed_past_combat_is_stopped_in_combat_every_turn() -> None:
    """ "Until my main 2" crosses your own declare-attackers step by design,
    and is stopped there. This is the intended behaviour, not an accident:
    the default is on so that the attack is offered rather than skipped, and
    the next test is the escape hatch. Restarting the run from the stop
    carries on to the original destination, so nothing is unreachable."""
    start = board("upkeep", 4)
    timeline = [board(step, 4) for step in TURN_STEPS[TURN_STEPS.index("draw") :]]
    stops = stops_for("postcombat_main", "you")

    first = run_until_stop(start, "until_castable", stops, timeline)
    assert first["stopped"]["step"] == "declare_attackers", first

    # The player looks, decides not to attack, and starts the run again from
    # where it stopped - the same click they made the first time.
    resumed = run_until_stop(
        board("declare_attackers", 4),
        "until_castable",
        stops,
        [board(step, 4) for step in TURN_STEPS[TURN_STEPS.index("declare_blockers") :]],
    )
    assert resumed["stopped"]["step"] == "postcombat_main", resumed
    assert resumed["stopped"]["turn"] == 4, resumed


def test_unticking_the_setting_lets_a_run_go_past_your_own_combat() -> None:
    """Leaving `declare_attackers` out of `stops.mine` (the picker's checkbox,
    unticked) lets "until my end step" run the whole turn including combat."""
    start = board("upkeep", 3)
    timeline = [board(step, 3) for step in TURN_STEPS[TURN_STEPS.index("draw") :]]
    stops = stops_for("end_step", "you", can_attack_stop=False)

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "end_step", result
    assert "declare_attackers@3" in result["passed_through"], result


def test_the_setting_is_independent_of_the_blockers_one() -> None:
    """Turning off the attackers stop must not turn off "being attacked": a
    player who never attacks still has to be told they are being attacked.
    In the new model "being attacked" is not a toggle at all any more -
    it is an unconditional rule with no opt-out - so the old
    `settings.stopWhenAttacked` half of this test has nothing left to
    disable; what remains, and what this still pins, is that leaving
    `declare_attackers` out of `stops.mine` does not touch it."""
    start = board("end_step", 1)
    timeline = [
        board("declare_attackers", 2, "bob", combat={"attacks": ATTACK_ON_YOU}),
        board("declare_blockers", 2, "bob", combat={"attacks": ATTACK_ON_YOU}),
    ] + turn_cycle(3, "alice")
    stops = stops_for("precombat_main", "you", can_attack_stop=False)

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "declare_blockers", result
    # The old exact string named the step ("...declare blockers"); the new
    # one is the uniform, unconditional "you are being attacked" - a wording
    # choice, not a rule change, since being attacked stopping the run at
    # all is exactly what this test is pinning.
    assert result["stopped"]["reason"] == "you are being attacked", result


def test_starting_a_run_inside_your_own_declare_attackers_step_is_not_a_no_op() -> None:
    """Clicking "auto-pass until my main 2" while standing in your own
    declare-attackers step is the player having looked at combat and chosen
    to move on. A condition that fired on that very state would stop the run
    before it moved at all - a dead button rather than a safeguard - so the
    occurrence the run started in is declined here exactly as it is for the
    destination check."""
    start = board("declare_attackers", 4)
    timeline = [board(step, 4) for step in TURN_STEPS[TURN_STEPS.index("declare_attackers") :]]
    stops = stops_for("postcombat_main", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "postcombat_main", result
    assert "declare_attackers@4" in result["passed_through"], result


def test_the_next_combat_is_still_stopped_for_after_that() -> None:
    """The occurrence flag is cleared the moment the board actually leaves
    that occurrence (`forgetStartingStep`), so declining the one the run
    started in costs nothing beyond it."""
    start = board("declare_attackers", 4)
    timeline = (
        [board(step, 4) for step in ["declare_attackers", "declare_blockers", "combat_damage"]]
        + turn_cycle(5, "bob")
        + turn_cycle(6, "alice")
    )
    stops = stops_for("end_step", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "declare_attackers", result
    assert result["stopped"]["turn"] == 6, result
    assert result["stopped"]["reason"] == "stop at your declare attackers", result
    assert "declare_attackers@4" in result["passed_through"], result


def test_the_same_holds_for_the_blockers_condition() -> None:
    """The occurrence guard is shared, not duplicated: starting a run while
    standing in a declare-blockers step you are defending - which is exactly
    where a player who has just declared their blocks is standing - does not
    stop it dead either.

    This one carries a regression in its own right rather than a side effect
    of the attackers work, and stands against the old commit 1b8bd30 (the
    blockers condition as first shipped, before the guard was shared): there,
    the attacked check fired on the very first state of the timeline and the
    run stopped at `declare_blockers@2` having moved nowhere. The attackers
    stop is left out of `stops.mine` here precisely so that nothing but the
    (now unconditional) attacked guard can be what makes this pass.
    """
    start = board("declare_blockers", 2, "bob", combat={"attacks": ATTACK_ON_YOU})
    timeline = [
        board("declare_blockers", 2, "bob", combat={"attacks": ATTACK_ON_YOU}),
        board("combat_damage", 2, "bob", combat={"attacks": ATTACK_ON_YOU}),
    ] + turn_cycle(3, "alice")
    stops = stops_for("precombat_main", "you", can_attack_stop=False)

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "precombat_main", result
    assert result["stopped"]["turn"] == 3, result
    assert "declare_blockers@2" in result["passed_through"], result


# -- and only when it should ------------------------------------------------


def test_the_defending_player_is_not_stopped_in_declare_attackers() -> None:
    """Bob is attacking. Alice has no declaration to make in his
    declare-attackers step, so her run carries on through it - and is
    stopped one step later by the unconditional "being attacked" rule."""
    start = board("end_step", 1)
    timeline = [
        board("declare_attackers", 2, "bob", combat={"attacks": ATTACK_ON_YOU}),
        board("declare_blockers", 2, "bob", combat={"attacks": ATTACK_ON_YOU}),
    ]
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "declare_blockers", result
    assert result["stopped"]["reason"] != "stop at your declare attackers", result
    assert "declare_attackers@2" in result["passed_through"], result


def test_it_does_not_fire_in_the_combat_steps_after_declare_attackers() -> None:
    """Your attackers are declared and `state.combat.attacks` stays
    populated through the rest of your combat. Only the step where the
    declaration is actually made is a stopping point."""
    start = board("upkeep", 3)
    timeline = [
        board("declare_blockers", 3, combat={"attacks": ONE_ATTACK}),
        board("combat_damage", 3, combat={"attacks": ONE_ATTACK}),
        board("end_of_combat", 3, combat={"attacks": ONE_ATTACK}),
        board("postcombat_main", 3, combat={"attacks": ONE_ATTACK}),
        board("end_step", 3, combat={"attacks": ONE_ATTACK}),
    ]
    stops = stops_for("end_step", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "end_step", result
    assert "combat_damage@3" in result["passed_through"], result


def test_it_does_not_fire_outside_combat_at_all() -> None:
    start = board("upkeep", 3)
    timeline = [board(step, 3) for step in ["draw", "precombat_main", "beginning_of_combat"]]
    stops = stops_for("end_step", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

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
    stops = stops_for("end_step", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "declare_attackers", result
    assert result["stopped"]["reason"] == "stop at your declare attackers", result


# -- the toggle and the panel ------------------------------------------------


def test_the_toggle_really_writes_the_setting() -> None:
    """The old checkbox called `toggleStopWhenYouCanAttackSetting`, writing
    into `game.settings`. The new model has no toggle function or settings
    object - "stop when you could attack" is just membership of
    `declare_attackers` in `stops.mine`, persisted like any other stop via
    `saveStops`/`loadStops`. This pins that round trip instead: turning the
    protection off means removing the step from `stops.mine`, and it
    survives a save/load exactly as any other stop does."""
    on = run_module("priority.js", "m.DEFAULT_STOPS")
    assert "declare_attackers" in on["mine"]

    off = {**on, "mine": [s for s in on["mine"] if s != "declare_attackers"]}
    round_tripped = run_module(
        "priority.js",
        "(() => { const store = new Map(); "
        "const storage = { getItem: (k) => store.get(k) ?? null, "
        "setItem: (k, v) => store.set(k, v) }; "
        "m.saveStops(storage, off); return m.loadStops(storage); })()",
        off=off,
    )
    assert "declare_attackers" not in round_tripped["mine"], round_tripped


def test_all_three_checkboxes_sit_together_in_the_priority_group() -> None:
    """The old panel's Priority group had to read as one consistent set of
    three checked-by-default checkboxes. The new model folds the three into:
    a stop (`declare_attackers` in `DEFAULT_STOPS.mine`), a verb choice
    (every verb but `pass_turn_regardless` checks the stack), and a rule
    with no opt-out (being attacked always stops the run - see
    `test_being_attacked_still_stops_the_run`). All three still hold for a
    player who never opens a panel."""
    stops = run_module("priority.js", "m.DEFAULT_STOPS")
    assert "declare_attackers" in stops["mine"], stops

    verbs = run_module("priority.js", "m.VERBS")
    stack_checking = [v for v in verbs if v != "pass_turn_regardless"]
    assert set(stack_checking) == {"pass", "pass_turn", "until_my_turn", "until_castable"}, verbs


def test_the_rest_of_the_priority_group_is_untouched() -> None:
    """The old picker and its three toggles sat beside "Declare attackers"'s
    own step button, and neither disturbed the other. There is no picker or
    toggle markup to check any more - `ui/priority.js` renders nothing, that
    is a later panel's job - so this now pins the equivalent: standing in
    your own declare-attackers step, with priority and nothing pending,
    still offers the full set of pass verbs. Reaching a stop here does not
    take away the player's ability to act."""
    state = panel_state(step="declare_attackers")
    assert run_module("priority.js", "m.passButtonsFor(s)", s=state) == run_module(
        "priority.js", "m.VERBS"
    )


# -- all conditions coexist ---------------------------------------------


def test_the_game_ending_still_wins() -> None:
    """Checked first, and it must stay first: a finished game has no attack
    to declare."""
    over = board("declare_attackers", 3, winner="bob")
    stops = stops_for("end_step", "you")

    result = run_until_stop(board("upkeep", 3), "until_castable", stops, [over])

    assert result["stopped"]["reason"] == "the game is over", result


def test_a_decision_for_this_seat_still_wins_in_the_same_step() -> None:
    """A question addressed to you outranks "you could attack" - it names
    something specific, where this condition only names the step."""
    asking = board(
        "declare_attackers",
        3,
        pending_decision={"player": "alice", "prompt": "order these triggers"},
    )
    stops = stops_for("end_step", "you")

    result = run_until_stop(board("upkeep", 3), "until_castable", stops, [asking])

    assert result["stopped"]["reason"] == "a decision is pending", result


def test_a_decision_for_the_other_seat_still_does_not_stop_the_run() -> None:
    timeline = [
        board("draw", 2, "bob", pending_decision={"player": "bob", "prompt": "scry 1"}),
    ] + turn_cycle(3, "alice")
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(board("end_step", 1), "until_castable", stops, timeline)

    assert result["stopped"]["turn"] == 3, result
    assert result["stopped"]["step"] == "precombat_main", result


def test_reaching_the_destination_still_wins_when_it_is_this_very_step() -> None:
    """ "Until my attack" used to stop with the destination's own wording
    ("reached your declare attackers"), distinct from the "you can attack"
    condition's own wording. The new model folds the two into one mechanism
    - `declare_attackers` in `stops.mine`, whether it got there as the named
    destination or as the safety net - so there is now exactly one message
    for reaching it, which is what this test now pins."""
    timeline = [board(step, 3) for step in TURN_STEPS[TURN_STEPS.index("draw") :]]
    stops = stops_for("declare_attackers", "you")

    result = run_until_stop(board("upkeep", 3), "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "declare_attackers", result
    assert result["stopped"]["reason"] == "stop at your declare attackers", result


def test_something_landing_on_the_stack_still_stops_the_run() -> None:
    timeline = [
        board("beginning_of_combat", 1, stack=[{"id": "spell-1", "name": "Lightning Bolt"}])
    ]
    stops = stops_for("postcombat_main", "you")

    result = run_until_stop(board("precombat_main", 1), "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "beginning_of_combat", result
    # No card name in the new message - a wording choice, not a rule change.
    assert result["stopped"]["reason"] == "something new is on the stack", result


def test_being_attacked_still_stops_the_run() -> None:
    """The condition added last time still fires with this one in place."""
    timeline = [board("declare_blockers", 2, "bob", combat={"attacks": ATTACK_ON_YOU})]
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(board("end_step", 1), "until_castable", stops, timeline)

    assert result["stopped"]["reason"] == "you are being attacked", result


def test_all_four_conditions_fire_in_one_run_one_after_another() -> None:
    """Turned off one at a time, the same timeline used to stop at each of
    four conditions in turn. In the new model, "being attacked" is no
    longer one of the four - it has no opt-out at all, so there is nothing
    left to turn off there, and the old test's third and fourth legs
    (`no_blocks`, `none_of_them`, which relied on disabling it) test a
    configuration that can no longer exist.

    A second casualty: "ignore the stack" is a verb choice now
    (`pass_turn_regardless`), and that verb is turn-scoped by design - MTGO's
    own "pass turn" always stops at the next turn boundary too - so it can no
    longer skip past a stack arrival to run on for two more turns the way
    `settings.stopOnStack: false` used to. Its own leg below is rewritten to
    a single tick where the stack and the attack are both present, which is
    the scenario it can actually still speak to: the unconditional attacked
    rule wins over the ignored stack, with nothing left able to disable
    either.

    What survives, and is what this test still pins: the stack check
    (verb choice) and the destination/attackers check (`stops.mine`) are two
    independently toggleable paths, and the unconditional attacked rule
    still wins over both when it applies - the coexistence the old test was
    checking, with one fewer knob."""
    start = board("end_step", 1)
    timeline = [
        board("upkeep", 2, "bob", stack=[{"id": "trig-1", "name": "Bitterblossom"}]),
        board("declare_blockers", 2, "bob", combat={"attacks": ATTACK_ON_YOU}),
        board("declare_attackers", 3, "alice"),
        board("precombat_main", 3, "alice"),
    ]
    stops = stops_for("precombat_main", "you")

    with_stack = run_until_stop(start, "until_castable", stops, timeline)
    assert with_stack["stopped"]["reason"] == "something new is on the stack", with_stack

    ignoring_the_stack = run_until_stop(
        board("upkeep", 2, "bob"),
        "pass_turn_regardless",
        stops,
        [
            board(
                "declare_blockers",
                2,
                "bob",
                stack=[{"id": "trig-1", "name": "Bitterblossom"}],
                combat={"attacks": ATTACK_ON_YOU},
            )
        ],
    )
    assert ignoring_the_stack["stopped"]["reason"] == "you are being attacked", ignoring_the_stack

    # The attackers check is still independently toggleable, but it can only
    # be observed once being attacked is out of the way - so this uses a
    # timeline with no attack in it at all.
    no_attack_timeline = [
        board("upkeep", 2, "bob"),
        board("declare_attackers", 3, "alice"),
        board("precombat_main", 3, "alice"),
    ]
    with_attackers_stop = run_until_stop(start, "until_castable", stops, no_attack_timeline)
    assert with_attackers_stop["stopped"]["step"] == "declare_attackers", with_attackers_stop

    without_attackers_stop = run_until_stop(
        start,
        "until_castable",
        stops_for("precombat_main", "you", can_attack_stop=False),
        no_attack_timeline,
    )
    assert without_attackers_stop["stopped"]["step"] == "precombat_main", without_attackers_stop
    assert without_attackers_stop["stopped"]["turn"] == 3, without_attackers_stop


def test_the_next_occurrence_guard_still_holds() -> None:
    """Standing in your own declare-attackers step, "until my attack" still
    means the next one - and the unconditional attacked/attackers checks,
    which are true of the very state the run started in whenever they apply,
    must not turn that into an instant no-op."""
    start = board("declare_attackers", 1)
    timeline = (
        [board(step, 1) for step in TURN_STEPS[TURN_STEPS.index("declare_attackers") :]]
        + turn_cycle(2, "bob")
        + turn_cycle(3, "alice")
    )
    stops = stops_for("declare_attackers", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["turn"] == 3, result
    assert result["stopped"]["step"] == "declare_attackers", result
    assert result["stopped"]["reason"] == "stop at your declare attackers", result
    assert "declare_attackers@1" in result["passed_through"], result
