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
        "theirs": ["declare_attackers", "end"],
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
        run_module("priority.js", expr, start=board(), later=board(step="end"), castable=False)
        is None
    )
    assert run_module("priority.js", expr, start=board(), later=board(step="end"), castable=True)


def test_a_stop_halts_every_verb_on_the_right_side() -> None:
    expr = (
        "m.stopReason(later, m.startRun('until_my_turn', start), "
        "{ mine: [], theirs: ['end'] }, false)"
    )
    assert run_module(
        "priority.js",
        expr,
        start=board(),
        later=board(active_player="bob", turn_number=4, step="end"),
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


def test_pass_buttons() -> None:
    assert run_module("priority.js", "m.passButtonsFor(s)", s=board()) == [
        "pass",
        "pass_turn",
        "pass_turn_regardless",
        "until_my_turn",
        "until_castable",
    ]
    assert run_module("priority.js", "m.passButtonsFor(s)", s=board(priority_holder="bob")) == []
