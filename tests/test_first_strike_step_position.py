"""T9: CR 510.4's first-strike combat damage step, at the `seatkit`/
`playscripts` layer - fake states throughout (this project's own rule:
"magic_cards_model_api and every other service repo are tested by ordinary
practice: fakes, in-memory implementations, and test doubles are legitimate
tools there"). `tests/test_first_strike_step_label.py` is the real-engine
half; `tests/test_ui_priority.py`/`test_ui_panels.py`/`test_ui_ribbon.py`
carry the node-driven UI half.

The engine keeps one `Step.COMBAT_DAMAGE` for both occurrences of the combat
damage step (`Game.is_first_strike_damage_step`'s own docstring), so a real
state dict's `"step"` field reads `"combat_damage"` in both - only
`"first_strike_damage_step"` (a bool) tells them apart. `seatkit.
step_position(state)` is the one place that gets read into a second position
name, `"first_strike_damage"`, and everywhere in `seatkit.py`/`playscripts/`
that compares, ranks, stamps or records "the current step" reads that
position rather than `state["step"]` directly.
"""

from __future__ import annotations

from typing import Any

from magic_cards_model_api import seatkit
from magic_cards_model_api.playscripts import runtime, script_tools


def board(*, step: str = "precombat_main", first_strike_damage_step: bool = False, **over: Any):
    state: dict[str, Any] = {
        "step": step,
        "first_strike_damage_step": first_strike_damage_step,
        "turn_number": 1,
        "turns_taken": 1,
        "active_player": "alice",
        "priority_holder": "alice",
        "pending_decision": None,
        "stack": [],
        "winner": None,
        "drawn": None,
        "combat": {"attacks": []},
    }
    state.update(over)
    return state


# ---------------------------------------------------------------------------
# seatkit: the constant, the position helper, and the two step orderings
# ---------------------------------------------------------------------------


def test_the_constant_and_the_helper() -> None:
    assert seatkit.FIRST_STRIKE_DAMAGE == "first_strike_damage"
    assert seatkit.step_position(board(step="combat_damage")) == "combat_damage"
    assert (
        seatkit.step_position(board(step="combat_damage", first_strike_damage_step=True))
        == "first_strike_damage"
    )


def test_autopass_steps_and_full_step_order_place_it_between_blockers_and_damage() -> None:
    for steps in (seatkit.AUTOPASS_STEPS, seatkit._FULL_STEP_ORDER):
        assert (
            steps.index("declare_blockers")
            < steps.index("first_strike_damage")
            < steps.index("combat_damage")
        )


def test_step_rank_orders_it_between_declare_blockers_and_combat_damage() -> None:
    assert (
        seatkit._step_rank("declare_blockers")
        < seatkit._step_rank("first_strike_damage")
        < seatkit._step_rank("combat_damage")
    )


def test_first_strike_damage_is_a_legal_pass_until_target() -> None:
    assert seatkit.autopass_target_error("first_strike_damage", "mine") is None


# ---------------------------------------------------------------------------
# seatkit: pass_until's own bookkeeping - started-step, forgetting it, and
# the actual stop condition
# ---------------------------------------------------------------------------


def test_a_run_started_in_the_first_strike_step_counts_the_regular_step_as_new() -> None:
    """`start_autopass` stamps the position, not the raw step - so a run
    begun while the board sits in the first-strike occurrence does not read
    the regular occurrence as "still the same step", even though both carry
    `state["step"] == "combat_damage"`."""
    started = board(step="combat_damage", first_strike_damage_step=True)
    run = seatkit.start_autopass(started, "postcombat_main", "mine")
    assert run["started_step"] == "first_strike_damage"

    still_there = seatkit.forget_starting_occurrence(
        dict(run), board(step="combat_damage", first_strike_damage_step=True)
    )
    assert still_there["started_step"] == "first_strike_damage"

    moved_on = seatkit.forget_starting_occurrence(
        dict(run), board(step="combat_damage", first_strike_damage_step=False)
    )
    assert moved_on["started_step"] is None


def test_pass_until_first_strike_damage_stops_there() -> None:
    run = seatkit.start_autopass(board(step="declare_blockers"), "first_strike_damage", "mine")
    state = board(step="combat_damage", first_strike_damage_step=True)
    assert seatkit.autopass_stop_reason(state, run, "alice") is not None
    assert "first strike damage" in seatkit.autopass_stop_reason(state, run, "alice")


def test_first_strike_damage_is_passed_through_when_this_combat_never_has_one() -> None:
    """No first or double striker in this combat: the board still moves
    through `combat_damage` (the only step it gets, CR 506.1), and a run
    aimed at `first_strike_damage` must not falsely read that as reached -
    both share `state["step"] == "combat_damage"`, and only the flag (false
    here) tells them apart."""
    run = seatkit.start_autopass(board(step="declare_blockers"), "first_strike_damage", "mine")
    state = board(step="combat_damage", first_strike_damage_step=False)
    reason = seatkit.autopass_stop_reason(state, run, "alice")
    assert reason is None, reason


def test_pass_until_combat_damage_from_before_combat_stops_at_the_regular_step() -> None:
    """Naming the plain `combat_damage` destination from outside combat must
    stop at the SECOND occurrence, not the first-strike one - the position
    the run is watching for is `"combat_damage"`, and the first occurrence's
    own position is `"first_strike_damage"`, a different string entirely."""
    run = seatkit.start_autopass(board(step="declare_blockers"), "combat_damage", "mine")
    first_strike_step = board(step="combat_damage", first_strike_damage_step=True)
    assert seatkit.autopass_stop_reason(first_strike_step, run, "alice") is None

    regular_step = board(step="combat_damage", first_strike_damage_step=False)
    reason = seatkit.autopass_stop_reason(regular_step, run, "alice")
    assert reason is not None and "combat damage" in reason


# ---------------------------------------------------------------------------
# playscripts.runtime: `when.steps` firing
# ---------------------------------------------------------------------------


def _when(steps: list[str], *, whose_turn: str = "mine", stack: str = "any") -> dict[str, Any]:
    return {"whose_turn": whose_turn, "steps": steps, "stack": stack}


def test_a_combat_damage_when_does_not_fire_in_the_first_strike_step() -> None:
    when = _when(["combat_damage"])
    assert not runtime._in_when(
        board(step="combat_damage", first_strike_damage_step=True), when, "alice"
    )


def test_a_combat_damage_when_fires_in_the_regular_step() -> None:
    when = _when(["combat_damage"])
    assert runtime._in_when(
        board(step="combat_damage", first_strike_damage_step=False), when, "alice"
    )


def test_a_first_strike_damage_when_fires_only_in_the_first_strike_step() -> None:
    when = _when(["first_strike_damage"])
    assert runtime._in_when(
        board(step="combat_damage", first_strike_damage_step=True), when, "alice"
    )
    assert not runtime._in_when(
        board(step="combat_damage", first_strike_damage_step=False), when, "alice"
    )


def test_next_step_reads_the_position_not_the_raw_step() -> None:
    when = _when(["first_strike_damage"])
    assert (
        runtime._next_step(board(step="declare_blockers"), when, "alice") == "first_strike_damage"
    )
    # Already standing in the first-strike step itself: nothing of `when`
    # is still ahead this occurrence (it names only that one step), so the
    # next occurrence is next combat's.
    assert (
        runtime._next_step(
            board(step="combat_damage", first_strike_damage_step=True), when, "alice"
        )
        == "first_strike_damage"
    )


# ---------------------------------------------------------------------------
# playscripts.script_tools: the ordering helpers and the once-per-position
# dedup key
# ---------------------------------------------------------------------------


def test_step_order_key_orders_it_between_declare_blockers_and_combat_damage() -> None:
    assert (
        script_tools._step_order_key("declare_blockers")
        < script_tools._step_order_key("first_strike_damage")
        < script_tools._step_order_key("combat_damage")
    )


def test_next_step_strictly_after_finds_first_strike_damage_from_declare_blockers() -> None:
    assert (
        script_tools._next_step_strictly_after(["first_strike_damage"], "declare_blockers")
        == "first_strike_damage"
    )
    # Standing in `first_strike_damage` itself: it is not "strictly after"
    # its own current position.
    assert (
        script_tools._next_step_strictly_after(["first_strike_damage"], "first_strike_damage")
        is None
    )
    # `combat_damage` is still strictly after the first-strike position.
    assert (
        script_tools._next_step_strictly_after(["combat_damage"], "first_strike_damage")
        == "combat_damage"
    )


def test_the_once_per_position_dedup_key_distinguishes_the_two_occurrences() -> None:
    """`script_tools.step_position`, not `state.get("step")`, is what the
    once-per-turn-and-step run/offer dedup keys are built from - otherwise a
    script that fired in the first-strike step would read as "already ran
    this step" the moment the board reached the regular one right after."""
    first_strike_step = board(step="combat_damage", first_strike_damage_step=True)
    regular_step = board(step="combat_damage", first_strike_damage_step=False)
    assert script_tools.step_position(first_strike_step) == "first_strike_damage"
    assert script_tools.step_position(regular_step) == "combat_damage"
    assert script_tools.step_position(first_strike_step) != script_tools.step_position(regular_step)
