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
        "onStack": True,
        "keepPriorityAfterCasting": False,
    }


def test_the_stack_tick_is_a_stop_of_its_own_and_starts_on() -> None:
    """The tick the owner was missing. It is not a destination and it does not
    replace the step ticks - it is an extra reason to stop on the way to one,
    which is why it lives beside `mine`/`theirs` rather than inside either.

    CR 117.4 resolves the top of the stack only once all players have passed
    in succession, so the pass this run has not yet made is the whole of your
    chance to answer whatever just landed. On by default for that reason."""
    assert run_module("priority.js", "m.DEFAULT_STOPS")["onStack"] is True


def test_the_keep_priority_after_casting_tick_defaults_off() -> None:
    """The owner's newest default: casting a spell auto-passes priority to
    the opponent (CR 117.3c already lets you keep it - this only decides
    whether the harness gives it up on your behalf). `onStack` off, this tick
    is the override, and it starts off to match today's auto-pass default -
    the opposite polarity from `onStack` above, deliberately: nothing here
    should silently start holding priority for a player who never touched the
    setting."""
    assert run_module("priority.js", "m.DEFAULT_STOPS")["keepPriorityAfterCasting"] is False


def test_stops_round_trip_through_storage() -> None:
    result = run_module(
        "priority.js",
        "(() => { const store = new Map(); "
        "const storage = { getItem: (k) => store.get(k) ?? null, "
        "setItem: (k, v) => store.set(k, v) }; "
        "m.saveStops(storage, { mine: ['upkeep'], theirs: [], onStack: false, "
        "keepPriorityAfterCasting: true }); "
        "return m.loadStops(storage); })()",
    )
    assert result == {
        "mine": ["upkeep"],
        "theirs": [],
        "onStack": False,
        "keepPriorityAfterCasting": True,
    }


def test_stops_saved_before_the_stack_tick_existed_read_back_with_it_on() -> None:
    """The storage key is unchanged, so a player's own step ticks survive this
    redesign - and a payload written before the tick existed carries no
    `onStack` at all. Absent has to mean on: the safe reading, and the one that
    matches `DEFAULT_STOPS`. Anything else silently takes the chance to respond
    away from every player who had already ticked a step."""
    result = run_module(
        "priority.js",
        "m.loadStops({ getItem: () => raw })",
        raw='{"mine": ["upkeep"], "theirs": ["end_step"]}',
    )
    assert result == {
        "mine": ["upkeep"],
        "theirs": ["end_step"],
        "onStack": True,
        "keepPriorityAfterCasting": False,
    }


def test_stops_saved_before_the_keep_priority_tick_existed_read_back_with_it_off() -> None:
    """The opposite polarity from `onStack`'s own backward-compatibility
    rule, and deliberately so: a payload written before this tick existed
    already represents a player used to today's auto-pass-after-casting
    behaviour, so absent has to mean "keep that default" - off - not silently
    start holding priority for everyone who never touched the setting."""
    result = run_module(
        "priority.js",
        "m.loadStops({ getItem: () => raw })",
        raw='{"mine": ["upkeep"], "theirs": ["end_step"]}',
    )
    assert result["keepPriorityAfterCasting"] is False


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


def test_pass_until_stops_when_something_hits_the_stack() -> None:
    """The stack tick, doing its job on the way to a destination it never
    reaches."""
    reason = run_module(
        "priority.js",
        "m.stopReason(later, m.startRun('pass_until', start), m.DEFAULT_STOPS)",
        start=board(),
        later=board(
            step="beginning_of_combat", stack=[{"id": "s1", "name": "Bolt", "kind": "spell"}]
        ),
    )
    assert reason == "something new is on the stack"


def test_unticking_the_stack_stop_runs_through_it_but_not_through_a_decision() -> None:
    """The counterweight, and what the old "Pass turn (even with a stack)"
    verb was for: with the tick off the run carries on past a spell. A pending
    decision is not a tick and cannot be turned off - the game is frozen until
    it is answered, whoever it is addressed to."""
    expr = "m.stopReason(later, m.startRun('pass_until', start), stops)"
    stops = {"mine": [], "theirs": [], "onStack": False}
    quiet = run_module(
        "priority.js",
        expr,
        start=board(),
        later=board(
            step="beginning_of_combat", stack=[{"id": "s1", "name": "Bolt", "kind": "spell"}]
        ),
        stops=stops,
    )
    assert quiet is None
    asked = run_module(
        "priority.js",
        expr,
        start=board(),
        later=board(pending_decision={"player": "alice", "kind": "may", "prompt": "x"}),
        stops=stops,
    )
    assert asked


def test_the_stack_tick_and_a_step_tick_are_both_live_at_once() -> None:
    """The owner's example: "passing until my precombat main" *and* "stop if
    something happens". The two are not alternatives - the run is aimed at a
    step tick and still hands the board back for a spell cast on the way."""
    stops = {"mine": ["precombat_main"], "theirs": [], "onStack": True}
    expr = "m.stopReason(later, m.startRun('pass_until', start), stops)"
    interrupted = run_module(
        "priority.js",
        expr,
        start=board(step="upkeep"),
        later=board(step="upkeep", stack=[{"id": "s1", "name": "Bolt", "kind": "spell"}]),
        stops=stops,
    )
    assert interrupted == "something new is on the stack"
    arrived = run_module(
        "priority.js",
        expr,
        start=board(step="upkeep"),
        later=board(step="precombat_main"),
        stops=stops,
    )
    assert arrived == "stop at your precombat main"


def test_pass_until_stops_at_every_step_tick_on_the_right_side() -> None:
    """Every existing phase tick still stops the run, on the side it is ticked
    on and only that side. `untap` and `cleanup` are not in the panel: no
    player receives priority in either (CR 502.4, CR 514.3)."""
    steps = [s for s in run_module("priority.js", "m.STEPS") if s not in ("untap", "cleanup")]
    assert len(steps) == 10, steps
    expr = "m.stopReason(later, m.startRun('pass_until', start), stops)"
    for step in steps:
        elsewhere = "upkeep" if step != "upkeep" else "draw"
        mine = run_module(
            "priority.js",
            expr,
            start=board(step=elsewhere),
            later=board(step=step),
            stops={"mine": [step], "theirs": [], "onStack": True},
        )
        assert mine == f"stop at your {step.replace('_', ' ')}", step

        theirs = run_module(
            "priority.js",
            expr,
            start=board(step=elsewhere),
            later=board(step=step, active_player="bob", turn_number=4),
            stops={"mine": [], "theirs": [step], "onStack": True},
        )
        assert theirs == f"stop at their {step.replace('_', ' ')}", step

        # Ticked on the other side only: the run carries on.
        wrong_side = run_module(
            "priority.js",
            expr,
            start=board(step=elsewhere),
            later=board(step=step),
            stops={"mine": [], "theirs": [step], "onStack": True},
        )
        assert wrong_side is None, step


def test_pass_until_stops_at_your_own_declare_attackers_step_when_ticked() -> None:
    """Declaring attackers is a turn-based action that does not use the stack
    (CR 508.1), and the pause that asks for it does not move `state.step`
    there either - the transition that would is what never finished (see
    `seatkit.autopass_stop_reason`'s docstring for the server-side twin of
    this reasoning). `state.pending_decision` is what announces it instead,
    stamped the moment the question is asked. `declare_attackers` on `mine`
    (on by default - `DEFAULT_STOPS`) is what stops the run: an ordinary
    checked stop like any other, not a rule with no tick."""
    assert "declare_attackers" in run_module("priority.js", "m.DEFAULT_STOPS")["mine"]
    later = board(
        step="beginning_of_combat",
        pending_decision={
            "player": "alice",
            "kind": "declare_attackers",
            "prompt": "Declare attackers",
        },
    )
    reason = run_module(
        "priority.js",
        "m.stopReason(later, m.startRun('pass_until', start), m.DEFAULT_STOPS)",
        start=board(step="upkeep"),
        later=later,
    )
    assert reason == "stop at your declare attackers"


def test_pass_until_runs_through_your_own_declare_attackers_step_when_unticked() -> None:
    """The owner's overrule of the old unconditional rule: untick
    `declare_attackers` from `stops.mine` and the run keeps going straight
    through the pause, the same as any other step whose tick is off. Nothing
    else in `stopReason` can see it either - it never touches the stack (CR
    508.1) and `state.step` never moves to name it - so the run does not stop
    at all. Answering the pause itself (declaring nobody) is the harness's
    job, not `stopReason`'s: this only says whether the run should look up."""
    later = board(
        step="beginning_of_combat",
        pending_decision={
            "player": "alice",
            "kind": "declare_attackers",
            "prompt": "Declare attackers",
        },
    )
    reason = run_module(
        "priority.js",
        "m.stopReason(later, m.startRun('pass_until', start), "
        "{ mine: [], theirs: [], onStack: false })",
        start=board(step="upkeep"),
        later=later,
    )
    assert reason is None


def test_a_pending_declare_blockers_decision_still_stops_unconditionally() -> None:
    """Unlike `declare_attackers`, being attacked was never a tick and stays
    that way - the owner has not asked for it to become one. This drives the
    same `state.pending_decision` path the fix above touches (rather than
    `attackedNow`'s own step/combat check) to pin that the two kinds did not
    accidentally start sharing the same tick-gated treatment."""
    later = board(
        step="beginning_of_combat",
        pending_decision={
            "player": "alice",
            "kind": "declare_blockers",
            "prompt": "Declare blockers",
        },
    )
    reason = run_module(
        "priority.js",
        "m.stopReason(later, m.startRun('pass_until', start), "
        "{ mine: [], theirs: [], onStack: false })",
        start=board(step="upkeep"),
        later=later,
    )
    assert reason == "a decision is pending"


def test_being_attacked_always_stops() -> None:
    """Not a tick and never was: with the stops panel emptied and the stack
    tick off, being attacked still stops the run."""
    expr = (
        "m.stopReason(later, m.startRun('pass_until', start), "
        "{ mine: [], theirs: [], onStack: false })"
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
        "m.stopReason(state, run, { mine: [], theirs: [] })",
        state=board(),
        run=run,
    )
    assert reason == "passed once"


def test_pass_stops_at_a_checked_stop_before_any_pass() -> None:
    """`pass` still falls through to the stops panel like `pass_until` -
    it does not have to wait for a pass to happen before a checked stop on
    the other side can halt it."""
    expr = "m.stopReason(later, m.startRun('pass', start), { mine: [], theirs: ['upkeep'] })"
    reason = run_module(
        "priority.js", expr, start=board(), later=board(active_player="bob", step="upkeep")
    )
    assert reason is not None


def test_pass_buttons_when_a_decision_is_pending() -> None:
    state = board(pending_decision={"player": "alice", "kind": "may", "prompt": "Pay 2 life?"})
    assert run_module("priority.js", "m.passButtonsFor(s)", s=state) == ["pass"]


def test_no_pass_buttons_when_the_pending_decision_is_someone_elses() -> None:
    """`priority_holder` can still read as `you` while the game is actually
    paused on a different seat's decision - a pass rolls the committed game
    back to before the attempt, so it never moved off you at all
    (`bob` passes, the step ends, and the engine pauses on `alice`'s
    `declare_attackers`: `priority_holder` is still `bob`). None of `pass`,
    `pass_until` can do anything until `alice` answers it, so nothing is
    offered - the old code offered the full verb list here, which is what
    let the browser autopilot re-issue an identical no-op pass every tick,
    forever, for the seat that is NOT the one being asked."""
    state = board(pending_decision={"player": "bob", "kind": "may", "prompt": "Pay 2 life?"})
    assert run_module("priority.js", "m.passButtonsFor(s)", s=state) == []


def test_pass_buttons() -> None:
    """Two verbs, and only two - the priority box offers what the rules do."""
    assert run_module("priority.js", "m.passButtonsFor(s)", s=board()) == ["pass", "pass_until"]
    assert run_module("priority.js", "m.passButtonsFor(s)", s=board(priority_holder="bob")) == []


# -- advanceOffered: the untap/cleanup way past a priorityless step --------


def test_advance_is_offered_in_your_own_untap_step() -> None:
    """CR 502.4: nobody receives priority during untap, so `passButtonsFor`
    offers nothing there - `POST .../advance` is the active player's own way
    past it."""
    assert run_module("priority.js", "m.advanceOffered(s)", s=board(step="untap")) is True


def test_advance_is_offered_in_your_own_cleanup_step() -> None:
    """CR 514.3: normally, no player receives priority during cleanup either."""
    assert run_module("priority.js", "m.advanceOffered(s)", s=board(step="cleanup")) is True


def test_advance_is_not_offered_in_an_ordinary_step() -> None:
    assert run_module("priority.js", "m.advanceOffered(s)", s=board(step="precombat_main")) is False


def test_advance_is_not_offered_on_the_opponents_untap_step() -> None:
    """It is the active player's own turn-based action (CR 502.1's untap) -
    the defending player has nothing to advance here."""
    result = run_module(
        "priority.js",
        "m.advanceOffered(s)",
        s=board(step="untap", active_player="bob", priority_holder=None),
    )
    assert result is False


def test_a_first_click_on_concede_only_arms_it() -> None:
    """CR 104.3a: "A player can concede the game at any time. A player who
    concedes leaves the game immediately. That player loses the game." The
    rule is why the button is always live; the browser pass is why it takes
    two clicks - with the opponent holding priority the box has no verbs in
    it, and Concede sat exactly where "Pass" had been a tick earlier."""
    assert run_module("priority.js", "m.concedeIntent(false)") == "arm"
    assert run_module("priority.js", "m.concedeIntent(true)") == "concede"
