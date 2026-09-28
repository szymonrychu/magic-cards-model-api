"""Planning off the clock: a `pass_until` run that handed back because the
opponent is still deciding keeps running on the server, during the
opponent's turn, while the seat writes its next-turn plan.

Two halves:

- `seatkit._pass_until_once(..., background=True)` - the same run, with one
  extra rule: it never makes a pass on the seat's own turn, and never the
  pass that could end the opponent's turn (an empty stack in their end step
  or cleanup). Those are left for the seat's own next call, so the plan it
  is writing is always on record before its own turn begins.
- `background_autopass.BackgroundRuns` - the thread that keeps calling it,
  and hands the run back to the seat's next `pass_until` call.

Every board is a real board played through the real API.
"""

from __future__ import annotations

import time

from conftest import force_turn_position, state
from magic_cards_model.machines.turn import Step
from test_autopass import Opponent, stock_libraries, wired

from magic_cards_model_api import seatkit
from magic_cards_model_api.background_autopass import BackgroundRuns


def drive_background(seat, **kwargs):
    """Call the background form of one run the way the runner does, until it
    stops being a plain wait on the opponent."""
    resume = None
    give_up = time.monotonic() + 30
    while True:
        result = seatkit._pass_until_once(seat, resume=resume, background=True, **kwargs)
        if result["finished"] or not result.get("retryable", False):
            return result
        resume = result["resume"]
        assert time.monotonic() < give_up, result["stopped_because"]
        time.sleep(0.01)


def test_background_never_passes_the_step_that_ends_the_opponents_turn(client, game, monkeypatch):
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")

    with Opponent(game, "bob"):
        result = drive_background(
            alice, until_step="upkeep", whose_turn="mine", stop_when_attacked=False
        )

    assert result["finished"] is False
    assert result["stopped_because"].startswith(seatkit.BACKGROUND_HALT)
    board = state(client, game)
    assert board["active_player"] == "bob", "the background run crossed into alice's turn"
    assert board["step"] in ("end_step", "cleanup"), board["step"]
    assert board["priority_holder"] == "alice"
    # The same run finishes in the foreground from the resume it left.
    finished = seatkit._pass_until_once(
        alice, resume=result["resume"], until_step="upkeep", whose_turn="mine"
    )
    assert finished["stopped_because"] == "reached your upkeep", finished["stopped_because"]


def test_background_makes_no_pass_on_the_seats_own_turn(client, game, monkeypatch):
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")

    result = seatkit._pass_until_once(
        alice, until_step="end_step", whose_turn="mine", background=True
    )

    assert result["passes"] == 0
    assert result["finished"] is False
    assert result["stopped_because"].startswith(seatkit.BACKGROUND_HALT)
    assert state(client, game)["step"] == "precombat_main"


def test_the_runner_keeps_passing_and_hands_the_run_back(client, game, monkeypatch):
    seats, uids = wired(monkeypatch, game)
    stock_libraries(client, game)
    force_turn_position(game, Step.UPKEEP, "bob")
    runs = BackgroundRuns(poll_seconds=0.01)
    kwargs = {"until_step": "upkeep", "whose_turn": "mine", "stop_when_attacked": False}
    first = seatkit._pass_until_once(seats.seat_for(uids["alice"]), **kwargs)
    assert first["finished"] is False and first.get("retryable"), first["stopped_because"]

    with Opponent(game, "bob"):
        runs.start(uids["alice"], lambda: seats.seat_for(uids["alice"]), kwargs, first["resume"])
        give_up = time.monotonic() + 30
        while runs.status(uids["alice"]) == "running":
            assert time.monotonic() < give_up
            time.sleep(0.02)
        resume, summary = runs.take_over(uids["alice"], first["resume"])

    assert summary is not None and summary["passes"] > 0, summary
    assert summary["status"] == "halted", summary
    assert resume is not None and resume != first["resume"]
    assert state(client, game)["active_player"] == "bob"
    finished = seatkit._pass_until_once(seats.seat_for(uids["alice"]), resume=resume, **kwargs)
    assert finished["stopped_because"] == "reached your upkeep", finished["stopped_because"]
    assert runs.status(uids["alice"]) is None


def test_a_different_resume_is_not_taken_over(client, game, monkeypatch):
    seats, uids = wired(monkeypatch, game)
    force_turn_position(game, Step.UPKEEP, "bob")
    runs = BackgroundRuns(poll_seconds=0.01)
    kwargs = {"until_step": "upkeep", "whose_turn": "mine"}
    first = seatkit._pass_until_once(seats.seat_for(uids["alice"]), **kwargs)
    runs.start(uids["alice"], lambda: seats.seat_for(uids["alice"]), kwargs, first["resume"])

    resume, summary = runs.take_over(uids["alice"], {"step": "end_step"})

    assert resume is None
    assert summary is not None
    assert runs.status(uids["alice"]) is None


def test_stop_ends_the_run_and_reports_it(client, game, monkeypatch):
    seats, uids = wired(monkeypatch, game)
    force_turn_position(game, Step.UPKEEP, "bob")
    runs = BackgroundRuns(poll_seconds=0.01)
    kwargs = {"until_step": "upkeep", "whose_turn": "mine"}
    first = seatkit._pass_until_once(seats.seat_for(uids["alice"]), **kwargs)
    runs.start(uids["alice"], lambda: seats.seat_for(uids["alice"]), kwargs, first["resume"])

    summary = runs.stop(uids["alice"])

    assert summary is not None and summary["passes"] == 0
    assert runs.stop(uids["alice"]) is None
