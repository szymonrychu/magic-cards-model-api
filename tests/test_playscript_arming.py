"""S2 A2: `playscripts/arming.py` - the arming core.

An agent may hand `arm()` an ordered list of script ids instead of running one
by hand: `arm()` judges every armed id against the board with the same
`_judged`/hint logic, fires the first (and only) one that applies through
`script_tools.run(wait=False, invoked_via="armed")`, re-judges the rest after
each completed run, and stops the batch the moment anything other than a
clean resolution happens. Lifetime one call: nothing here remembers an armed
list across calls.

Two kinds of coverage: a handful of true end-to-end tests over a real board
(firing for real, the conflict case, the reasoning refusals, the contents
fetch, `invoked_via` on the ledger), and control-flow tests that monkeypatch
`arming._judge_round` and `script_tools.run` directly - the exit-handling
table itself (`_judged`'s own applicability logic and every `Exit` value) is
already exhaustively covered against real boards in
`test_playscript_runtime.py`; what is new here is only how `arm()` sequences
calls to it.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import add_card, force_turn_position
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_playscript_runtime import (
    fetch_shock,
    finished,
    hooked_seat,
    land_script,
    register,
    seat_uid,
    started,
    wired,
)

from magic_cards_model_api.playscripts import arming, script_tools
from magic_cards_model_api.playscripts.runtime import REASONING_PLACEHOLDER, Exit

ARM_REASONING = "the human is thinking; fire this the moment it is legal"


@pytest.fixture()
def board(client: TestClient, game: str) -> str:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    return game


# -- refusals, before any fetch -------------------------------------------------


def test_an_empty_arm_list_does_nothing(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    result = arming.arm(seat, seats.script_corpora, arm=[], arm_reasoning="")

    assert result is None
    assert hook.sent == []


def test_a_blank_arm_reasoning_is_refused_before_any_fetch(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    result = arming.arm(seat, seats.script_corpora, arm=["drop"], arm_reasoning="   ")

    assert isinstance(result, str)
    assert hook.sent == []


def test_the_call_templates_placeholder_reasoning_is_refused_before_any_fetch(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    result = arming.arm(
        seat, seats.script_corpora, arm=["drop"], arm_reasoning=REASONING_PLACEHOLDER
    )

    assert isinstance(result, str)
    assert REASONING_PLACEHOLDER in result
    assert hook.sent == []


# -- real board: firing, invoked_via, and the conflict case --------------------


def test_a_single_applicable_armed_script_fires_for_real(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = arming.arm(seat, seats.script_corpora, arm=["drop"], arm_reasoning=ARM_REASONING)

    assert isinstance(result, dict), result
    assert result.get("armed_conflict") is None
    [entry] = result["armed"]
    assert entry["script"] == "drop"
    assert entry["exit"] == "chain_complete"
    assert hook.posts("/actions/play-land"), "the armed script actually played the land"

    [start] = started(board)
    [finish] = finished(board)
    assert start["invoked_via"] == "armed"
    assert finish["invoked_via"] == "armed"


def test_a_script_off_its_when_does_not_fire_and_never_waits(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wait=False` (D2): an armed script off its `when` right now is simply
    not applicable, never a reason to pass priority toward a later `when`."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, fetch_shock())
    # No fetch land on the board at all: the script's `true` never holds.
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = arming.arm(
        seat, seats.script_corpora, arm=[fetch_shock()["id"]], arm_reasoning=ARM_REASONING
    )

    assert isinstance(result, dict), result
    assert result["armed"] == []
    assert hook.posts("/actions/activate") == []


def test_two_armed_scripts_applying_at_once_is_a_conflict_and_neither_fires(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop-a"), land_script("drop-b", priority=45))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = arming.arm(
        seat, seats.script_corpora, arm=["drop-a", "drop-b"], arm_reasoning=ARM_REASONING
    )

    assert isinstance(result, dict), result
    assert set(result["armed_conflict"]) == {"drop-a", "drop-b"}
    assert result["armed"] == []
    assert hook.posts("/actions/play-land") == []
    assert started(board) == []


def test_a_contents_needing_armed_script_costs_exactly_one_extra_fetch(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    needs_contents = land_script(
        "needs-library-count",
        true=[{"kind": "count", "zone": "library", "side": "me", "op": ">=", "n": 1, "select": {}}],
    )
    register(client, board, alice, needs_contents)
    add_card(client, board, "Island", "alice", "hand")
    add_card(client, board, "Mountain", "alice", "library")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    calls = {"n": 0}
    real_fetch_state = seat.__class__.fetch_state

    def counting(self: Any, *a: Any, **kw: Any) -> Any:
        calls["n"] += 1
        return real_fetch_state(self, *a, **kw)

    monkeypatch.setattr(seat.__class__, "fetch_state", counting)

    judged, judged_state = arming._judge_round(
        seat, seats.script_corpora, ["needs-library-count"], {"opposing_archetype": "*"}
    )

    assert judged["needs-library-count"]["applies"] is True, judged
    assert judged_state.get("library_contents"), judged_state
    # One plain board fetch, then one extra `include=contents` fetch, paid
    # once for the whole round - never a second blind re-fetch.
    assert calls["n"] == 2, calls


# -- one shared deadline over the whole batch -----------------------------------


class _FakeClock:
    """A `time.monotonic` stand-in whose reading only moves when told to -
    `arming.time.monotonic()` is looked up through this instance once
    `arming`'s own `time` name is monkeypatched to it."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


def _judge_first_remaining(
    seat: Any, corpora: Any, ids: list[str], supplied: Any
) -> tuple[dict[str, Any], Any]:
    """Only the first of `ids` (this round's `remaining`) applies - lets a
    batch of several armed scripts fire one at a time, in order, across
    several rounds, rather than three simultaneous appliers tripping the
    conflict check."""
    applies = ids[0] if ids else None
    judged = {script_id: {"id": script_id, "applies": script_id == applies} for script_id in ids}
    return judged, {"turn_number": 1, "step": "precombat_main"}


def test_the_whole_armed_batch_shares_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """S2 review finding 3: each armed run used to get the *whole*
    `RUN_CAP_SECONDS` again, so three armed runs could together take up to
    3x the cap. `arm()` now sets one deadline for the whole batch when it
    starts, and each run gets only what is left of it."""
    clock = _FakeClock()
    monkeypatch.setattr(arming, "time", clock)
    monkeypatch.setattr(arming, "_judge_round", _judge_first_remaining)
    captured_max_seconds: list[float] = []

    def run(seat: Any, corpora: Any, *, script: str, max_seconds: float, **kwargs: Any) -> Any:
        captured_max_seconds.append(max_seconds)
        clock.now += 8.0  # each run "takes" 8 seconds of wall time
        return {
            "script_exit": {"script": script, "exit": Exit.CHAIN_COMPLETE.value, "moves_done": 1},
            "state": {"turn_number": 1, "step": "precombat_main"},
        }

    monkeypatch.setattr(script_tools, "run", run)
    seat = object()

    result = arming.arm(
        seat,
        {},
        arm=["a", "b", "c"],
        arm_reasoning=ARM_REASONING,  # type: ignore[arg-type]
    )

    assert isinstance(result, dict), result
    assert len(result["armed"]) == 3, result
    from magic_cards_model_api.playscripts.runtime import RUN_CAP_SECONDS

    assert captured_max_seconds[0] == RUN_CAP_SECONDS
    assert captured_max_seconds[1] == RUN_CAP_SECONDS - 8.0
    assert captured_max_seconds[2] == RUN_CAP_SECONDS - 16.0
    assert all(value <= RUN_CAP_SECONDS for value in captured_max_seconds)


def test_the_batch_stops_rather_than_starting_a_run_with_no_budget_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch whose own deadline is exhausted mid-way stops there rather
    than starting one more run with (effectively) no time left for it."""
    clock = _FakeClock()
    monkeypatch.setattr(arming, "time", clock)
    monkeypatch.setattr(arming, "_judge_round", _judge_first_remaining)
    calls = {"n": 0}

    def run(seat: Any, corpora: Any, *, script: str, max_seconds: float, **kwargs: Any) -> Any:
        calls["n"] += 1
        clock.now += 13.0  # each run "takes" 13 seconds; the third never starts
        return {
            "script_exit": {"script": script, "exit": Exit.CHAIN_COMPLETE.value, "moves_done": 1},
            "state": {"turn_number": 1, "step": "precombat_main"},
        }

    monkeypatch.setattr(script_tools, "run", run)
    seat = object()

    result = arming.arm(
        seat,
        {},
        arm=["a", "b", "c"],
        arm_reasoning=ARM_REASONING,  # type: ignore[arg-type]
        max_seconds=25.0,
    )

    assert isinstance(result, dict), result
    assert len(result["armed"]) == 2, result
    assert calls["n"] == 2, calls


# -- the 3-run cap ---------------------------------------------------------------


def needs_input_script(script_id: str) -> dict[str, Any]:
    """A script whose condition needs an input this test never supplies -
    every one of these is simultaneously in the `needs_input` position (never
    the `applies` one, so this never trips the conflict check), letting the
    batch work through several of them in `arm` order without depending on
    board state changing between rounds."""
    doc = land_script(script_id)
    doc["true"] = [
        *doc["true"],
        {"kind": "input", "name": "prefer_tempo", "op": "eq", "value": True},
    ]
    return doc


def test_the_batch_stops_at_three_armed_runs(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(
        client,
        board,
        alice,
        needs_input_script("need-1"),
        needs_input_script("need-2"),
        needs_input_script("need-3"),
        needs_input_script("need-4"),
        inputs=[
            {
                "name": "prefer_tempo",
                "type": "bool",
                "description": "Tempo first.",
                "required": True,
            }
        ],
    )
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = arming.arm(
        seat,
        seats.script_corpora,
        arm=["need-1", "need-2", "need-3", "need-4"],
        arm_reasoning=ARM_REASONING,
    )

    assert isinstance(result, dict), result
    assert result.get("armed_conflict") is None, result
    assert len(result["armed"]) == arming.MAX_ARMED_RUNS == 3
    assert {entry["script"] for entry in result["armed"]} == {"need-1", "need-2", "need-3"}
    assert all(entry["exit"] == "needs_input" for entry in result["armed"]), result["armed"]
    assert hook.posts("/actions/play-land") == []


# -- exit-handling control flow (monkeypatched) ---------------------------------


def _fake_judge(applies: dict[str, bool], needs_input: dict[str, str] | None = None) -> Any:
    def judge(seat: Any, corpora: Any, ids: list[str], supplied: Any) -> tuple[dict[str, Any], Any]:
        judged = {}
        for script_id in ids:
            entry: dict[str, Any] = {"id": script_id, "applies": applies.get(script_id, False)}
            if needs_input and script_id in needs_input:
                entry["needs_input"] = needs_input[script_id]
            judged[script_id] = entry
        return judged, {"turn_number": 1, "step": "precombat_main"}

    return judge


def _fake_run(exits: dict[str, list[dict[str, Any]]]) -> Any:
    """`script_tools.run` stand-in: pops the next canned `script_exit` queued
    for a script id and echoes it back the same shape `script_tools.run`
    hands to `arming.arm` - `{"script_exit": ..., "state": ...}`."""

    def run(seat: Any, corpora: Any, *, script: str, **kwargs: Any) -> dict[str, Any]:
        queue = exits[script]
        exit_block = {"script": script, "moves_done": 0, "moves_total": 0, **queue.pop(0)}
        return {"script_exit": exit_block, "state": {"turn_number": 1, "step": "precombat_main"}}

    return run


def _arm(
    monkeypatch: pytest.MonkeyPatch, *, applies: dict[str, bool], exits: dict[str, Any]
) -> Any:
    monkeypatch.setattr(arming, "_judge_round", _fake_judge(applies))
    monkeypatch.setattr(script_tools, "run", _fake_run(exits))
    seat = object()
    return arming.arm(
        seat,
        {},
        arm=list(applies),
        arm_reasoning=ARM_REASONING,  # type: ignore[arg-type]
    )


def test_chain_complete_counts_as_fired_and_the_batch_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _arm(
        monkeypatch,
        applies={"a": True},
        exits={"a": [{"exit": Exit.CHAIN_COMPLETE.value}]},
    )
    assert [entry["exit"] for entry in result["armed"]] == ["chain_complete"]


def test_not_applicable_is_dropped_and_the_batch_tries_the_next_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Round 1: "a" is the sole applier but its own run comes back
    # not_applicable (a race between the judge snapshot and the run itself);
    # round 2 re-judges with "a" gone and "b" now the sole applier.
    calls = {"n": 0}

    def judge(seat: Any, corpora: Any, ids: list[str], supplied: Any) -> tuple[dict[str, Any], Any]:
        calls["n"] += 1
        applies = "a" if calls["n"] == 1 else "b"
        return (
            {script_id: {"id": script_id, "applies": script_id == applies} for script_id in ids},
            {"turn_number": 1, "step": "precombat_main"},
        )

    monkeypatch.setattr(arming, "_judge_round", judge)
    monkeypatch.setattr(
        script_tools,
        "run",
        _fake_run(
            {
                "a": [{"exit": Exit.NOT_APPLICABLE.value}],
                "b": [{"exit": Exit.CHAIN_COMPLETE.value}],
            }
        ),
    )
    seat = object()

    result = arming.arm(seat, {}, arm=["a", "b"], arm_reasoning=ARM_REASONING)  # type: ignore[arg-type]

    assert [entry["script"] for entry in result["armed"]] == ["a", "b"]
    assert [entry["exit"] for entry in result["armed"]] == ["not_applicable", "chain_complete"]


def test_needs_input_is_listed_first_and_the_batch_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        arming,
        "_judge_round",
        _fake_judge({"a": False, "b": True}, needs_input={"a": "prefer_tempo"}),
    )
    monkeypatch.setattr(
        script_tools,
        "run",
        _fake_run(
            {
                "a": [{"exit": Exit.NEEDS_INPUT.value, "needs_input": "prefer_tempo"}],
                "b": [{"exit": Exit.CHAIN_COMPLETE.value}],
            }
        ),
    )
    seat = object()

    result = arming.arm(seat, {}, arm=["b", "a"], arm_reasoning=ARM_REASONING)  # type: ignore[arg-type]

    # "a" ran (its needs_input surfaced) and "b" ran too; "a"'s entry is
    # listed first regardless of `arm` order or firing order.
    assert result["armed"][0]["script"] == "a"
    assert result["armed"][0]["exit"] == "needs_input"


@pytest.mark.parametrize(
    "exit_value",
    [
        Exit.NEEDS_PLAN.value,
        Exit.REFUSED.value,
        Exit.UNFORESEEN.value,
        Exit.INTERRUPTED.value,
        Exit.AMBIGUOUS.value,
        Exit.STALE.value,
        Exit.UNKNOWN_OUTCOME.value,
        Exit.ADVISED.value,
    ],
)
def test_a_stopping_exit_ends_the_batch(monkeypatch: pytest.MonkeyPatch, exit_value: str) -> None:
    result = _arm(
        monkeypatch,
        applies={"a": True, "b": False},
        exits={"a": [{"exit": exit_value}], "b": [{"exit": Exit.CHAIN_COMPLETE.value}]},
    )
    # Only "a" fired: the batch stopped rather than trying "b" next.
    assert [entry["script"] for entry in result["armed"]] == ["a"]


def test_a_null_exit_time_cap_stops_and_carries_its_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _arm(
        monkeypatch,
        applies={"a": True, "b": False},
        exits={
            "a": [{"exit": None, "resume": {"run_id": "r1"}}],
            "b": [{"exit": Exit.CHAIN_COMPLETE.value}],
        },
    )
    assert [entry["script"] for entry in result["armed"]] == ["a"]
    assert result["armed"][0]["resume"] == {"run_id": "r1"}


def test_hands_back_the_fresh_board_on_a_stopping_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arming, "_judge_round", _fake_judge({"a": True}))

    def run(seat: Any, corpora: Any, *, script: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "script_exit": {"script": script, "exit": Exit.REFUSED.value, "moves_done": 0},
            "state": {"turn_number": 9, "step": "end_step"},
        }

    monkeypatch.setattr(script_tools, "run", run)
    seat = object()

    result = arming.arm(seat, {}, arm=["a"], arm_reasoning=ARM_REASONING)  # type: ignore[arg-type]

    assert result["state"] == {"turn_number": 9, "step": "end_step"}
