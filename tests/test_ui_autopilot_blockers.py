"""The run must not walk you through your own declare-blockers step, ported
from the old `ui/app.js` autopilot onto the new `ui/priority.js` verb model.

**The bug, as originally reported.** The old picker's run stopped for
exactly four things: the game ending, a `pending_decision` addressed to this
seat, the named destination being reached, and a new object arriving on the
stack. None of them fires when the opponent attacks, so a run aimed at (say)
"my main 1" passed the defending player straight through declare blockers
and took the whole attack unblocked.

Neither of the two existing near-miss conditions could ever have caught it:

- Declaring blockers is a turn-based action. CR 509.1, verified with
  `knowledge_search(type="rules")` in the session that ported this file:
  "First, the defending player declares blockers. This turn-based action
  doesn't use the stack." CR 703.4j puts it immediately after the step
  begins. So nothing lands on the stack and a stack-only check cannot see
  it.
- `src/magic_cards_model_api/decisions.py` deliberately keeps
  `DECLARE_ATTACKERS` and `DECLARE_BLOCKERS` out of `ANSWERABLE_KINDS`,
  because both have their own actions (`actions/attack`,
  `actions/declare-blockers`) rather than going through the decision
  mailbox. So `state.pending_decision` is never set for blocking either.

**The port.** In the old picker, `settings.stopWhenAttacked` (default on)
was a toggle. In the new model it is not data at all: `stopReason` checks it
unconditionally, with no `stops` entry or verb choice able to disable it -
"being attacked" is one of the rules the port brief calls out as needing to
survive as a rule, not a UI choice, and here it survives as one with no
opt-out. Several old tests exercised turning it off
(`test_unticking_the_setting_lets_the_run_continue`,
`test_the_toggle_really_writes_the_setting`,
`test_the_checkbox_is_rendered_next_to_the_stop_on_stack_one`); each is
rewritten below to what is actually still true, with a docstring saying so.

One fixture correction, matching `tests/test_ui_autopilot_attackers.py`'s:
the old `TWO_ATTACKS`'s `defender: "alice"` was inert under the old code,
which never read it, and was reused as-is for
`test_the_attacking_player_is_not_stopped_in_the_same_step` even though
alice is the *attacker* there. The new `stopReason`'s `attackedNow` reads
`defender` directly, so that scenario needs its own fixture with the
defender corrected to `bob`; see `ALICE_ATTACKS` below.

Like `tests/test_ui_autopilot_attackers.py` and
`tests/test_ui_autopilot_next_occurrence.py`, this drives the shipped
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

#: Two attacks naming alice (the seat every test in this file plays,
#: excepting the one noted below) as the defender - the "you are being
#: attacked" shape.
TWO_ATTACKS = [
    {"attacker": "att-1", "defender": "alice", "blockers": []},
    {"attacker": "att-2", "defender": "alice", "blockers": []},
]

#: The same two attackers, naming bob as the defender - alice attacking, for
#: the one test where she is the active player declaring them.
ALICE_ATTACKS = [
    {"attacker": "att-1", "defender": "bob", "blockers": []},
    {"attacker": "att-2", "defender": "bob", "blockers": []},
]


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


def under_attack(turn: int, **overrides: Any) -> dict[str, Any]:
    """Bob's declare-blockers step, with bob attacking alice - the seat this
    file plays is always alice."""
    return board("declare_blockers", turn, "bob", combat={"attacks": TWO_ATTACKS}, **overrides)


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


def call_stop_reason(
    state: dict[str, Any],
    verb: str = "until_castable",
    stops: dict[str, list[str]] | None = None,
    castable: bool = False,
) -> Any:
    """A single `stopReason` call against `state`, with a `run` built from a
    synthetic starting state - step "cleanup", never used by a state under
    test here - so the occurrence guard never accidentally suppresses a stop
    this file is testing."""
    stops = stops if stops is not None else {"mine": [], "theirs": []}
    start = board("cleanup", state.get("turn_number", 1), "nobody", stack=[])
    return run_module(
        "priority.js",
        "m.stopReason(state, m.startRun(verb, start), stops, castable)",
        state=state,
        start=start,
        verb=verb,
        stops=stops,
        castable=castable,
    )


def run_until_stop(
    start: dict[str, Any],
    verb: str,
    stops: dict[str, list[str]],
    timeline: list[dict[str, Any]],
    *,
    castable: bool = False,
) -> dict[str, Any]:
    """Start a real run standing in `start`, then feed it `timeline` one
    board state at a time, reporting the first state that stopped it - the
    same two lines the old `tickAutopilot` ran on every state that did not
    stop it (`knownStackIds` refreshed, `forgetStartingStep` called) are run
    here too, threaded back into `run` for the next tick."""
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


# -- the reported bug --------------------------------------------------------


def test_a_run_stops_when_this_seat_is_being_attacked() -> None:
    """Alice runs from her own end step towards her next main 1. Bob takes
    his turn and attacks; the run must stop in his declare-blockers step
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
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"] is not None, "the run passed through its own declare blockers"
    assert result["stopped"]["step"] == "declare_blockers", result
    assert result["stopped"]["turn"] == 2, result
    assert result["stopped"]["active"] == "bob", result
    # The old exact string named the attacker count; the new one is the
    # uniform, unconditional message - a wording choice, not a rule change.
    assert result["stopped"]["reason"] == "you are being attacked", result
    assert "combat_damage@2" not in result["passed_through"], result


def test_it_stops_for_a_single_attacker_too() -> None:
    """One attacker is still an attack."""
    one = [{"attacker": "att-1", "defender": "alice", "blockers": []}]
    reason = call_stop_reason(board("declare_blockers", 2, "bob", combat={"attacks": one}))
    assert reason == "you are being attacked"


def test_the_setting_defaults_on() -> None:
    """The old default lived in `game.settings.stopWhenAttacked`. There is no
    equivalent setting to default in the new model at all - being attacked
    is unconditional, with no `stops` entry or verb able to turn it off -
    which is the stronger version of "the safe default is on"."""
    reason = call_stop_reason(
        under_attack(2), verb="pass_turn_regardless", stops={"mine": [], "theirs": []}
    )
    assert reason == "you are being attacked"


# -- and only when it should ------------------------------------------------


def test_the_attacking_player_is_not_stopped_in_the_same_step() -> None:
    """Alice is the active player here: these are her attackers, and
    blocking is bob's problem. Her run must sail through her own
    declare-blockers step exactly as it did before."""
    start = board("upkeep", 3)
    timeline = [
        board("declare_attackers", 3, "alice", combat={"attacks": ALICE_ATTACKS}),
        board("declare_blockers", 3, "alice", combat={"attacks": ALICE_ATTACKS}),
        board("combat_damage", 3, "alice", combat={"attacks": ALICE_ATTACKS}),
        board("postcombat_main", 3, "alice", combat={"attacks": ALICE_ATTACKS}),
    ]
    # `declare_attackers` left out of `stops.mine`: this timeline crosses
    # alice's own declare-attackers step, which that stop covers by design,
    # and this test is about the blockers condition rather than that one.
    # See `tests/test_ui_autopilot_attackers.py` for its own coverage.
    stops = stops_for("postcombat_main", "you", can_attack_stop=False)

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "postcombat_main", result
    assert "declare_blockers@3" in result["passed_through"], result


def test_no_attackers_means_no_stop() -> None:
    """Bob went to combat and declared nothing. There is nothing to block,
    so the run keeps going through his declare-blockers step."""
    start = board("end_step", 1)
    timeline = [
        board("declare_attackers", 2, "bob"),
        board("declare_blockers", 2, "bob"),
        board("combat_damage", 2, "bob"),
    ] + turn_cycle(3, "alice")
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "precombat_main", result
    assert result["stopped"]["turn"] == 3, result
    assert "declare_blockers@2" in result["passed_through"], result


def test_it_does_not_fire_in_other_steps_of_a_combat_you_are_defending() -> None:
    """The attacks list is still populated in combat damage and end of
    combat. Only the step where the game asks you to block is a stopping
    point."""
    start = board("end_step", 1)
    timeline = [
        board("combat_damage", 2, "bob", combat={"attacks": TWO_ATTACKS}),
        board("end_of_combat", 2, "bob", combat={"attacks": TWO_ATTACKS}),
        board("postcombat_main", 2, "bob", combat={"attacks": TWO_ATTACKS}),
    ] + turn_cycle(3, "alice")
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["turn"] == 3, result
    assert result["stopped"]["step"] == "precombat_main", result


def test_unticking_the_setting_lets_the_run_continue() -> None:
    """There is nothing left to untick: being attacked has no `stops` entry
    or verb able to disable it in the new model, so this now pins the
    stronger claim the old toggle's absence implies - even with every other
    stop stripped out (empty `stops`, the stack ignored via
    `pass_turn_regardless`), the attacked check still fires. There is no
    configuration of the new model that runs a defending player through an
    attack unnoticed. (Confined to bob's own turn 2, since
    `pass_turn_regardless` also stops on a turn change in its own right -
    see `tests/test_ui_priority.py` - and this test is about the attacked
    rule, not that one.)"""
    start = board("upkeep", 2, "bob")
    timeline = [
        board("declare_attackers", 2, "bob", combat={"attacks": TWO_ATTACKS}),
        under_attack(2),
        board("combat_damage", 2, "bob", combat={"attacks": TWO_ATTACKS}),
    ]

    result = run_until_stop(start, "pass_turn_regardless", {"mine": [], "theirs": []}, timeline)

    assert result["stopped"]["step"] == "declare_blockers", result
    assert result["stopped"]["turn"] == 2, result
    assert result["stopped"]["reason"] == "you are being attacked", result


def test_the_toggle_really_writes_the_setting() -> None:
    """The old checkbox called `toggleStopWhenAttackedSetting`, writing into
    `game.settings.stopWhenAttacked`. There is no setting to write in the new
    model - being attacked is a hard rule inside `stopReason`, not data - so
    this pins that directly: every verb still reports it, even with the
    stops panel emptied out (nothing left that could have disabled it)."""
    for verb in run_module("priority.js", "m.VERBS"):
        reason = call_stop_reason(under_attack(2), verb=verb, stops={"mine": [], "theirs": []})
        assert reason == "you are being attacked", (verb, reason)


def test_the_checkbox_is_rendered_next_to_the_stop_on_stack_one() -> None:
    """The old panel wired this checkbox next to "stop when anything lands
    on the stack"'s, so the two read as one group. There is no rendering to
    check any more - `ui/priority.js` renders nothing, that is a later
    panel's job - so what makes the two "read together" now is that
    `stopReason` checks the attacked rule before the stack, unconditionally:
    it wins even under a verb (`pass_turn_regardless`) chosen specifically to
    ignore the stack, with a stack object present at the very same tick."""
    reason = call_stop_reason(
        under_attack(2, stack=[{"id": "spell-1", "name": "Lightning Bolt"}]),
        verb="pass_turn_regardless",
    )
    assert reason == "you are being attacked"


# -- the three existing stop conditions, and the next-occurrence guard -------


def test_the_game_ending_still_stops_the_run() -> None:
    stops = stops_for("precombat_main", "you")
    result = run_until_stop(
        board("precombat_main", 1),
        "until_castable",
        stops,
        [board("precombat_main", 1, winner="bob")],
    )
    assert result["stopped"]["reason"] == "the game is over", result


def test_a_decision_for_this_seat_still_stops_the_run() -> None:
    asking = board(
        "precombat_main",
        1,
        pending_decision={"player": "alice", "prompt": "look at the top four cards"},
    )
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(board("precombat_main", 1), "until_castable", stops, [asking])

    assert result["stopped"]["reason"] == "a decision is pending", result


def test_a_decision_for_the_other_seat_still_does_not_stop_the_run() -> None:
    """The condition is addressed-to-you, not any decision at all."""
    timeline = [
        board("draw", 2, "bob", pending_decision={"player": "bob", "prompt": "scry 1"}),
    ] + turn_cycle(3, "alice")
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(board("end_step", 1), "until_castable", stops, timeline)

    assert result["stopped"]["turn"] == 3, result


def test_something_landing_on_the_stack_still_stops_the_run() -> None:
    timeline = [
        board("beginning_of_combat", 1, stack=[{"id": "spell-1", "name": "Lightning Bolt"}])
    ]
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(board("precombat_main", 1), "until_castable", stops, timeline)

    assert result["stopped"]["step"] == "beginning_of_combat", result
    assert result["stopped"]["reason"] == "something new is on the stack", result


def test_unticking_stop_on_stack_still_lets_the_run_continue() -> None:
    timeline = [
        board("beginning_of_combat", 1, stack=[{"id": "spell-1", "name": "Lightning Bolt"}]),
    ] + turn_cycle(3, "alice")
    stops = stops_for("precombat_main", "you")

    result = run_until_stop(board("precombat_main", 1), "pass_turn_regardless", stops, timeline)

    assert result["stopped"]["turn"] == 3, result


def test_reaching_the_destination_still_stops_the_run() -> None:
    timeline = [board(step, 1) for step in TURN_STEPS[TURN_STEPS.index("draw") :]]
    # `declare_attackers` left out of `stops.mine`: this timeline crosses
    # alice's own declare-attackers step, and this test is about the
    # destination check rather than that one.
    stops = stops_for("end_step", "you", can_attack_stop=False)

    result = run_until_stop(board("upkeep", 1), "until_castable", stops, timeline)

    # The old exact string was "reached your end step"; the new stops panel
    # says "stop at your X" uniformly - a wording choice, not a rule change.
    assert result["stopped"]["reason"] == "stop at your end step", result
    assert result["stopped"]["step"] == "end_step", result


def test_the_next_occurrence_guard_still_holds() -> None:
    """Standing in her own precombat main, "until my main 1" still means the
    next one - a whole turn cycle later, with the attacked condition never
    firing because nobody attacked."""
    start = board("precombat_main", 1)
    timeline = (
        [board(step, 1) for step in TURN_STEPS[TURN_STEPS.index("precombat_main") :]]
        + turn_cycle(2, "bob")
        + turn_cycle(3, "alice")
    )
    stops = stops_for("precombat_main", "you", can_attack_stop=False)

    result = run_until_stop(start, "until_castable", stops, timeline)

    assert result["stopped"]["turn"] == 3, result
    assert result["stopped"]["step"] == "precombat_main", result
    assert "precombat_main@1" in result["passed_through"]


def test_being_attacked_beats_the_destination_when_both_are_true() -> None:
    """A run aimed at "the opponent's declare blockers" reaches its own
    target in the same state the attack is visible in. The old code checked
    the destination first, so its own banner named the destination rather
    than the attack, despite this test's title; the new `stopReason` checks
    "you are being attacked" ahead of the stops panel (its ordering pinned by
    `tests/test_ui_priority.py`), so the reason text now really is the
    attacked one - this test's name describes the new code more literally
    than it ever described the old one, and either way the run has stopped,
    which was always the actual point."""
    timeline = [under_attack(2)]
    stops = {"mine": [], "theirs": ["declare_blockers"]}

    result = run_until_stop(board("end_step", 1), "until_castable", stops, timeline)

    assert result["stopped"] is not None
    assert result["stopped"]["reason"] == "you are being attacked"
