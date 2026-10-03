"""Driver behaviour around a seat pair: the briefing travels with turn 0 as a
workspace file, a dead first boot is re-dispatched, and no continuation is
pushed while the pair is still alive.

The world is `tests/driver/world.py`'s fake game server and fake wrapper.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from mtg_agent.seat_pair import Channel
from tests.driver.world import build_world

CONTINUE_MARK = "The game is not over"


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path, channel=Channel.TEAMS)


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def dispatch(world, *, role: str = "seat") -> None:
    response = world.client.post(
        "/dispatch",
        json={
            "uid": world.uid,
            "game_id": world.game_id,
            "player": "bob",
            "api_base": "http://game:8000",
            "role": role,
        },
    )
    assert response.status_code == 202, response.text


def finish(world, turn_id: str, **fields) -> dict:
    body = {"turnId": turn_id, "state": "complete", "finalText": "idle"} | fields
    return world.client.post("/turn-done", json=body).json()


def settled_on(world, turn_id: str) -> None:
    """The driver has recorded `turn_id` as the turn in flight (a re-dispatch
    pushes first and records after, on its own thread)."""
    wait_for(
        lambda: (status := world.client.get("/status").json())["lastTurnId"] == turn_id
        and status["turnActive"]
    )


def continuations(world) -> list[dict]:
    return [turn for turn in world.submitted if turn["text"].startswith(CONTINUE_MARK)]


# ---- (1) the briefing is a workspace file -----------------------------------


def test_turn_zero_carries_the_rendered_briefing_as_a_workspace_file(world):
    dispatch(world)
    files = world.submitted[0]["files"]
    assert list(files) == ["current-game.md"]
    assert files["current-game.md"] == (world.settings.workspace / "current-game.md").read_text()
    assert "bob" in files["current-game.md"]


def test_a_turn_zero_retried_after_a_booting_agent_still_carries_the_file(world):
    world.state["agent"] = "booting"
    dispatch(world)
    world.state["agent"] = "up"
    wait_for(lambda: world.submitted)
    assert "current-game.md" in world.submitted[0]["files"]


def test_a_continuation_carries_no_files(world):
    dispatch(world, role="seat")
    world.state["agent"] = "up"
    pair_free = build_world.__globals__["Channel"].OFF  # noqa: F841 - documents the channel
    finish(world, "turn-1")
    assert all("files" not in turn for turn in world.submitted[1:])


# ---- (2) defect 4: a dead first boot is re-dispatched ------------------------


def test_a_first_turn_that_fails_agent_gone_is_pushed_again_once_the_wrapper_is_ready(world):
    dispatch(world)
    world.state["session_state"] = "dead"
    ready_at: list[float] = []

    def become_ready() -> None:
        time.sleep(0.1)
        ready_at.append(time.monotonic())
        world.state["session_state"] = "ready"

    threading.Thread(target=become_ready, daemon=True).start()
    pushed_at: list[float] = []
    world.state["on_message"] = lambda: pushed_at.append(time.monotonic())

    finish(world, "turn-1", state="failed", error="claude exited code=1")
    wait_for(lambda: len(world.submitted) == 2)

    assert pushed_at and ready_at and pushed_at[0] >= ready_at[0]
    first, second = world.submitted
    assert second["text"] == first["text"]
    assert second["files"] == first["files"]
    status = world.client.get("/status").json()
    assert status["turnActive"] is True
    assert status["lastTurnId"] == "turn-2"
    assert status["lastError"] is None


def test_three_agent_gone_failures_file_a_problem_report_and_push_no_fourth_turn(world):
    dispatch(world)
    finish(world, "turn-1", state="failed", error="claude exited code=1")
    settled_on(world, "turn-2")
    finish(world, "turn-2", state="failed", error="claude exited code=1")
    settled_on(world, "turn-3")
    body = finish(world, "turn-3", state="failed", error="claude exited code=1")

    assert "continued" not in body
    wait_for(lambda: world.problem_reports)
    time.sleep(0.2)
    assert len(world.submitted) == 3, "no fourth push"
    assert len(world.problem_reports) == 1
    status = world.client.get("/status").json()
    assert status["turnActive"] is False
    assert "claude exited code=1" in status["lastError"]


def test_a_session_that_went_dead_under_an_active_turn_is_redispatched(world):
    dispatch(world)
    assert world.client.get("/status").json()["turnActive"] is True
    world.state["session_state"] = "dead"
    world.client.get("/status")
    threading.Timer(0.1, lambda: world.state.update(session_state="ready")).start()
    wait_for(lambda: len(world.submitted) == 2)
    assert world.submitted[1]["text"] == world.submitted[0]["text"]
    assert "current-game.md" in world.submitted[1]["files"]


def test_a_session_that_is_merely_busy_is_not_redispatched(world):
    dispatch(world)
    world.state["session_state"] = "busy"
    for _ in range(3):
        world.client.get("/status")
    time.sleep(0.1)
    assert len(world.submitted) == 1


# ---- (3) no continuation while the pair is live ------------------------------


def test_fifteen_orchestrator_idles_with_a_live_pair_push_no_continuation(world, caplog):
    dispatch(world)
    caplog.set_level(logging.INFO, logger="driver")
    turn = "turn-1"
    for _ in range(15):
        body = finish(world, turn, pendingTasks=["future-1", "current-0"])
        assert "continued" not in body, body
        turn = f"turn-{len(world.submitted) + 1}"
        # each idle is a distinct turn id, as the wrapper would number them
    assert continuations(world) == []
    assert len(world.submitted) == 1
    assert "pair live" in caplog.text


def test_a_paired_seat_whose_record_shows_no_pair_is_continued(world):
    dispatch(world)
    body = finish(world, "turn-1", pendingTasks=[])
    assert body.get("continued") == "turn-2", body
    assert len(continuations(world)) == 1


def test_pending_tasks_on_an_unpaired_seat_do_not_suppress_the_continuation(tmp_path):
    off = build_world(tmp_path, channel=Channel.OFF)
    dispatch(off)
    body = finish(off, "turn-1", pendingTasks=["some-bash"])
    assert body.get("continued") == "turn-2", body


def test_a_live_pair_does_not_suppress_the_reflection_when_the_game_is_over(world):
    dispatch(world)
    world.state["winner"] = "bob"
    body = finish(world, "turn-1", pendingTasks=["future-1"])
    assert body.get("reflectionTurnId"), body
