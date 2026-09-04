from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node


def board(**over):
    state = {
        "you": "alice",
        "priority_holder": "alice",
        "active_player": "alice",
        "turn_number": 3,
        "step": "precombat_main",
        "stack": [],
        "pending_decision": None,
        "combat": {"attacks": []},
        "battlefield": [],
        "hands": {"alice": []},
        "winner": None,
        "drawn": None,
    }
    state.update(over)
    return state


def test_default_stops_are_mtgo_like() -> None:
    assert run_module("priority.js", "m.DEFAULT_STOPS") == {
        "mine": ["precombat_main", "declare_attackers", "postcombat_main"],
        "theirs": ["declare_attackers", "end_step"],
    }


def test_stops_round_trip_through_storage() -> None:
    result = run_module(
        "priority.js",
        "(() => { const store = new Map(); "
        "const storage = { getItem: (k) => store.get(k) ?? null, "
        "setItem: (k, v) => store.set(k, v) }; "
        "m.saveStops(storage, { mine: ['upkeep'], theirs: [] }); "
        "return m.loadStops(storage); })()",
    )
    assert result == {"mine": ["upkeep"], "theirs": []}


def test_a_broken_storage_yields_defaults() -> None:
    result = run_module("priority.js", "m.loadStops({ getItem() { throw new Error('nope'); } })")
    assert result == run_module("priority.js", "m.DEFAULT_STOPS")


def test_stored_stops_that_parse_to_the_wrong_shape_yield_defaults() -> None:
    """ "[]" and "7" are both valid JSON, but neither is a {mine, theirs}
    object - as unusable as invalid JSON, so they get the same fallback
    rather than silently becoming empty stops."""
    for raw in ("[]", "7"):
        result = run_module("priority.js", "m.loadStops({ getItem: () => raw })", raw=raw)
        assert result == run_module("priority.js", "m.DEFAULT_STOPS"), raw


def test_pass_turn_stops_when_something_hits_the_stack() -> None:
    reason = run_module(
        "priority.js",
        "m.stopReason(later, m.startRun('pass_turn', start), m.DEFAULT_STOPS, false)",
        start=board(),
        later=board(
            step="beginning_of_combat", stack=[{"id": "s1", "name": "Bolt", "kind": "spell"}]
        ),
    )
    assert reason


def test_pass_turn_regardless_ignores_the_stack_but_not_a_decision() -> None:
    regardless_expr = (
        "m.stopReason(later, m.startRun('pass_turn_regardless', start), "
        "{ mine: [], theirs: [] }, false)"
    )
    quiet = run_module(
        "priority.js",
        regardless_expr,
        start=board(),
        later=board(
            step="beginning_of_combat", stack=[{"id": "s1", "name": "Bolt", "kind": "spell"}]
        ),
    )
    assert quiet is None
    asked = run_module(
        "priority.js",
        regardless_expr,
        start=board(),
        later=board(pending_decision={"player": "alice", "kind": "may", "prompt": "x"}),
    )
    assert asked


def test_until_my_turn_runs_through_the_opponents_turn() -> None:
    expr = (
        "m.stopReason(later, m.startRun('until_my_turn', start), { mine: [], theirs: [] }, false)"
    )
    assert (
        run_module(
            "priority.js",
            expr,
            start=board(),
            later=board(active_player="bob", priority_holder="alice", step="upkeep", turn_number=4),
        )
        is None
    )
    assert run_module("priority.js", expr, start=board(), later=board(turn_number=5, step="upkeep"))


def test_until_castable_stops_when_told_something_is_castable() -> None:
    expr = (
        "m.stopReason(later, m.startRun('until_castable', start), "
        "{ mine: [], theirs: [] }, castable)"
    )
    assert (
        run_module("priority.js", expr, start=board(), later=board(step="end_step"), castable=False)
        is None
    )
    assert run_module(
        "priority.js", expr, start=board(), later=board(step="end_step"), castable=True
    )


def test_a_stop_halts_every_verb_on_the_right_side() -> None:
    expr = (
        "m.stopReason(later, m.startRun('until_my_turn', start), "
        "{ mine: [], theirs: ['end_step'] }, false)"
    )
    assert run_module(
        "priority.js",
        expr,
        start=board(),
        later=board(active_player="bob", turn_number=4, step="end_step"),
    )
    assert (
        run_module(
            "priority.js",
            expr,
            start=board(),
            later=board(active_player="bob", turn_number=4, step="upkeep"),
        )
        is None
    )


def test_being_attacked_always_stops() -> None:
    expr = (
        "m.stopReason(later, m.startRun('pass_turn_regardless', start), "
        "{ mine: [], theirs: [] }, false)"
    )
    later = board(
        active_player="bob",
        turn_number=4,
        step="declare_blockers",
        combat={"attacks": [{"attacker": "c1", "defender": "alice", "blockers": []}]},
    )
    assert run_module("priority.js", expr, start=board(), later=later)


def test_pass_stops_after_one_recorded_pass() -> None:
    """`pass` yields after the first pass goes through - `recordPass` is what
    the caller runs to mark that, and `stopReason` reads `run.passes` off the
    result."""
    run = run_module("priority.js", "m.recordPass(m.startRun('pass', start), start)", start=board())
    reason = run_module(
        "priority.js",
        "m.stopReason(state, run, { mine: [], theirs: [] }, false)",
        state=board(),
        run=run,
    )
    assert reason == "passed once"


def test_pass_stops_at_a_checked_stop_before_any_pass() -> None:
    """`pass` still falls through to the stops panel like every other verb -
    it does not have to wait for a pass to happen before a checked stop on
    the other side can halt it."""
    expr = "m.stopReason(later, m.startRun('pass', start), { mine: [], theirs: ['upkeep'] }, false)"
    reason = run_module(
        "priority.js", expr, start=board(), later=board(active_player="bob", step="upkeep")
    )
    assert reason is not None


def test_pass_buttons_when_a_decision_is_pending() -> None:
    state = board(pending_decision={"player": "alice", "kind": "may", "prompt": "Pay 2 life?"})
    assert run_module("priority.js", "m.passButtonsFor(s)", s=state) == ["pass"]


def test_pass_buttons() -> None:
    assert run_module("priority.js", "m.passButtonsFor(s)", s=board()) == [
        "pass",
        "pass_turn",
        "pass_turn_regardless",
        "until_my_turn",
        "until_castable",
    ]
    assert run_module("priority.js", "m.passButtonsFor(s)", s=board(priority_holder="bob")) == []
