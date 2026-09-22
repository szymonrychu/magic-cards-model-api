"""A seat is never left idle mid-game.

The live failure (seat pod mtg-seat-0cf08288, 2026-09-22): the agent ended its
own turn mid-game with a chat-style status summary, `turn_done` found the game
still running and answered `{"ignored": "game still running"}` - pushing
nothing - and the seat sat idle until a human clicked "Pass priority for
<ai>". The driver now pushes a continuation turn instead, bounded by a loop
guard, and `/status` says so honestly.

The world is `tests/driver/world.py`'s fake game server and fake wrapper; the
clock the loop guard reads is injected, so "a continuation that ended within
30 seconds" is a number the test moves, not a wait.
"""

from __future__ import annotations

import time

import pytest

from mtg_agent.driver import app as driver_app
from mtg_agent.driver.prompt import HANDOVER_OPENING
from tests.driver.world import ADVISOR_UID, build_world

CONTINUE_MARK = "The game is not over"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def world(tmp_path, clock):
    return build_world(tmp_path, clock=clock)


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def dispatch(world, *, role: str = "seat", uid: str | None = None) -> None:
    response = world.client.post(
        "/dispatch",
        json={
            "uid": uid or world.uid,
            "game_id": world.game_id,
            "player": "bob",
            "api_base": "http://game:8000",
            "role": role,
        },
    )
    assert response.status_code == 202, response.text


def finish(world, turn_id: str, **fields) -> dict:
    body = {"turnId": turn_id, "state": "complete", "finalText": "status summary"} | fields
    return world.client.post("/turn-done", json=body).json()


def continuations(world) -> list[dict]:
    return [turn for turn in world.submitted if turn["text"].startswith(CONTINUE_MARK)]


# ---- auto-continue ----------------------------------------------------------


def test_a_turn_that_ends_mid_game_is_continued_at_once(world):
    dispatch(world)
    body = finish(world, "turn-1", stopReason="end_turn")
    assert body.get("continued") == "turn-2", body
    assert len(world.submitted) == 2
    assert world.submitted[1]["callbackUrl"] == "http://driver:8090/turn-done"
    assert CONTINUE_MARK in world.submitted[1]["text"]
    status = world.client.get("/status").json()
    assert status["turnActive"] is True
    assert status["lastTurnId"] == "turn-2"


def test_the_continuation_prompt_says_to_keep_playing_and_never_summarise(world):
    dispatch(world)
    finish(world, "turn-1")
    text = world.submitted[1]["text"]
    assert "nobody reads" in text.lower()
    assert "summary" in text.lower()
    assert "wait_for_priority" in text
    assert "battlefield_state" in text
    assert "plan" in text.lower()


def test_a_turn_that_failed_for_a_non_terminal_reason_is_continued(world):
    """A max-turns stop or any other error the wrapper did not attribute to
    the agent being gone: the agent is still there, so the game goes on."""
    dispatch(world)
    body = finish(world, "turn-1", state="failed", error="reached max turns")
    assert body.get("continued") == "turn-2", body
    status = world.client.get("/status").json()
    assert status["turnActive"] is True
    assert status["lastError"] is None, "the seat is playing again, not in error"


@pytest.mark.parametrize(
    "error",
    [
        "claude exited with code 1",
        "claude reported a fatal error: API Error: 401",
        "write to pty failed: [Errno 5] Input/output error",
    ],
)
def test_a_turn_that_failed_because_the_agent_is_gone_keeps_today_s_handling(world, error):
    dispatch(world)
    body = finish(world, "turn-1", state="failed", error=error)
    assert "continued" not in body
    assert len(world.submitted) == 1
    status = world.client.get("/status").json()
    assert status["turnActive"] is False
    assert status["lastError"] and error.split(":")[0] in status["lastError"]


def test_an_agent_that_cannot_take_the_continuation_is_reported(world):
    dispatch(world)
    world.state["agent"] = "down"
    body = finish(world, "turn-1")
    assert "continued" not in body
    status = world.client.get("/status").json()
    assert status["turnActive"] is False
    assert status["lastError"], "a seat nobody can reach must not read as healthy"


def test_a_duplicate_delivery_of_the_finished_turn_does_not_push_twice(world):
    """`webhook.Deliverer` retries a slow `/turn-done`. The retry of turn-1
    arriving after turn-2 was pushed must neither push a third turn nor
    mark turn-2 as finished."""
    dispatch(world)
    finish(world, "turn-1")
    body = finish(world, "turn-1")
    assert len(world.submitted) == 2, body
    assert world.client.get("/status").json()["turnActive"] is True


def test_the_reflection_path_is_unchanged_after_continuations(world):
    dispatch(world)
    finish(world, "turn-1")
    world.state["winner"] = "bob"
    body = finish(world, "turn-2", finalText="gg")
    assert body["reflectionTurnId"] == "turn-3"
    assert "The game is over" in world.submitted[2]["text"]
    assert len(continuations(world)) == 1


def test_the_reflection_turn_itself_is_never_continued(world):
    dispatch(world)
    world.state["winner"] = "bob"
    finish(world, "turn-1", finalText="gg")
    body = finish(world, "turn-2", finalText="wrote")
    assert body.get("reflected") is True
    assert continuations(world) == []


def test_a_turn_that_ends_during_a_handover_is_not_continued(world):
    dispatch(world)
    world.client.post("/release", json={"uid": world.uid})
    wait_for(lambda: any(t["text"].startswith(HANDOVER_OPENING) for t in world.submitted))
    finish(world, "turn-1")
    assert continuations(world) == []


def test_a_turn_with_no_assignment_is_not_continued(world):
    body = finish(world, "turn-9")
    assert body == {"ignored": "no assignment"}
    assert world.submitted == []


# ---- the loop guard ---------------------------------------------------------


#: A board where the seat itself holds priority: the game is waiting on it.
SEAT_TO_ACT = {"turn_number": 4, "step": "precombat_main", "priority_holder": "bob", "stack": []}
#: The same board with the human opponent to act: the seat is waiting on them.
OPPONENT_TO_ACT = SEAT_TO_ACT | {"priority_holder": "alice"}


def test_three_quick_continuations_trip_the_guard_and_file_a_report(world, clock):
    """No progress, the game waiting on this very seat, and each continuation
    over within 30s: the agent keeps ending its turn instead of playing."""
    world.state["board"] = dict(SEAT_TO_ACT)
    dispatch(world)
    finish(world, "turn-1")  # turn zero ends: continuation turn-2
    for turn in ("turn-2", "turn-3"):
        clock.now += driver_app.CONTINUE_QUICK_SECONDS - 1
        assert "continued" in finish(world, turn)
    clock.now += driver_app.CONTINUE_QUICK_SECONDS - 1
    body = finish(world, "turn-4")
    assert "continued" not in body, body
    assert len(continuations(world)) == driver_app.CONTINUE_QUICK_LIMIT
    assert len(world.problem_reports) == 1
    report = world.problem_reports[0]
    assert report["game_id"] == world.game_id
    assert report["uid"] == world.uid, "filed under the seat's own credential"
    assert report["player"] == "bob"
    assert "continu" in report["text"]
    status = world.client.get("/status").json()
    assert status["turnActive"] is False
    assert status["lastError"], "a tripped guard must read as a problem, not a healthy idle"


def test_a_long_continuation_resets_the_guard(world, clock):
    world.state["board"] = dict(SEAT_TO_ACT)
    dispatch(world)
    finish(world, "turn-1")
    quick = driver_app.CONTINUE_QUICK_SECONDS - 1
    slow = driver_app.CONTINUE_QUICK_SECONDS + 1
    turn = 2
    for elapsed in (quick, quick, slow, quick, quick):
        clock.now += elapsed
        assert "continued" in finish(world, f"turn-{turn}"), turn
        turn += 1
    assert world.problem_reports == []


def test_turn_zero_ending_quickly_does_not_count_toward_the_guard(world, clock):
    dispatch(world)
    clock.now += 1
    finish(world, "turn-1")
    for turn in ("turn-2", "turn-3"):
        clock.now += 1
        assert "continued" in finish(world, turn)
    assert world.problem_reports == []


# ---- the game server is briefly unreachable --------------------------------


def test_an_unreachable_game_is_checked_again_after_a_short_delay(world, monkeypatch):
    monkeypatch.setattr(driver_app, "UNREACHABLE_RETRY_SECONDS", 0.05)
    dispatch(world)
    world.state["game"] = "down"
    body = finish(world, "turn-1")
    assert body == {"ignored": "game unreachable"}
    world.state["game"] = "up"
    wait_for(lambda: continuations(world))
    wait_for(lambda: world.client.get("/status").json()["turnActive"] is True)


def test_a_game_that_stays_unreachable_is_reported_not_left_idle(world, monkeypatch):
    monkeypatch.setattr(driver_app, "UNREACHABLE_RETRY_SECONDS", 0.05)
    dispatch(world)
    world.state["game"] = "down"
    finish(world, "turn-1")
    wait_for(lambda: world.client.get("/status").json()["lastError"])
    assert continuations(world) == []


# ---- the buddy --------------------------------------------------------------


def test_an_advisor_turn_is_continued_with_advisor_wording(world):
    dispatch(world, role="advisor", uid=ADVISOR_UID)
    body = finish(world, "turn-1")
    assert body.get("continued") == "turn-2", body
    text = world.submitted[1]["text"]
    assert CONTINUE_MARK in text
    assert "advis" in text.lower()


# ---- /status: an active turn is never idle ---------------------------------


def test_status_reports_a_busy_agent_even_without_a_driver_turn(world):
    """The agent can be working with no driver-pushed turn in flight - woken
    by a background task's notification or a message pushed straight to the
    wrapper. The wrapper's own session state is the truth there."""
    dispatch(world)
    world.state["agent"] = "down"
    finish(world, "turn-1")  # continuation cannot be pushed: no driver turn
    world.state["agent"] = "up"
    world.state["session_state"] = "busy"
    status = world.client.get("/status").json()
    assert status["turnActive"] is False
    assert status["agentBusy"] is True


def test_status_reports_an_idle_agent_as_not_busy(world):
    world.state["session_state"] = "ready"
    assert world.client.get("/status").json()["agentBusy"] is False


def test_status_reports_no_busy_flag_for_an_unreachable_agent(world):
    world.state["agent"] = "down"
    assert world.client.get("/status").json()["agentBusy"] is False


def test_any_game_progress_resets_the_guard(world, clock):
    """A seat that plays one window and ends its turn quickly is playing, not
    looping: the board moved during the continuation."""
    world.state["board"] = dict(SEAT_TO_ACT)
    dispatch(world)
    finish(world, "turn-1")
    for turn in range(2, 9):
        clock.now += 5
        world.state["board"] = world.state["board"] | {"step": f"step-{turn}"}
        assert "continued" in finish(world, f"turn-{turn}"), turn
    assert world.problem_reports == []


def test_a_seat_waiting_on_its_opponent_never_trips(world, clock):
    """The human is thinking; the seat's continuation ran a 20s wait and
    ended with nothing to do. Nothing moved, but nothing was the seat's to
    move either."""
    world.state["board"] = dict(OPPONENT_TO_ACT)
    dispatch(world)
    finish(world, "turn-1")
    for turn in range(2, 9):
        clock.now += 25
        assert "continued" in finish(world, f"turn-{turn}"), turn
    assert world.problem_reports == []


def test_a_seat_whose_decisions_another_player_makes_never_trips(world, clock):
    """CR 723.5: the human cast Emrakul, the Promised End and is making the
    seat's decisions this turn. The seat holds priority by name (CR 723.3),
    but none of it is the seat's to move - waiting that out is not a loop."""
    world.state["board"] = SEAT_TO_ACT | {
        "players": [
            {"username": "bob", "decisions_made_by": "alice"},
            {"username": "alice", "decisions_made_by": None},
        ]
    }
    dispatch(world)
    finish(world, "turn-1")
    for turn in range(2, 9):
        clock.now += 25
        assert "continued" in finish(world, f"turn-{turn}"), turn
    assert world.problem_reports == []


def test_an_advisor_s_idle_waits_never_trip(world, clock):
    """A buddy whose human holds priority and is thinking: each continuation
    waits its 20s, finds nothing new to advise on, and ends."""
    world.state["board"] = dict(SEAT_TO_ACT)  # bob, the advised human, to act
    dispatch(world, role="advisor", uid=ADVISOR_UID)
    finish(world, "turn-1")
    for turn in range(2, 9):
        clock.now += 22
        assert "continued" in finish(world, f"turn-{turn}"), turn
    assert world.problem_reports == []


def test_continuations_that_end_instantly_trip_even_while_waiting_on_someone_else(world, clock):
    """Under `CONTINUE_INSTANT_SECONDS` the agent did not even run one wait:
    that is a broken loop whoever the game is waiting on - advisors included."""
    world.state["board"] = dict(OPPONENT_TO_ACT)
    dispatch(world, role="advisor", uid=ADVISOR_UID)
    finish(world, "turn-1")
    bodies = []
    for turn in range(2, 2 + driver_app.CONTINUE_QUICK_LIMIT):
        clock.now += 1
        bodies.append(finish(world, f"turn-{turn}"))
    assert "continued" not in bodies[-1], bodies
    assert len(world.problem_reports) == 1
    assert world.problem_reports[0]["uid"] == ADVISOR_UID


# ---- delivery failures and retries -----------------------------------------


def test_a_delivery_that_raises_is_handled_again_on_the_retry(world):
    """The Deliverer retries a `/turn-done` that failed. The first attempt
    must not have marked the turn handled, or the retry is dropped as a
    duplicate and the seat is stranded."""
    dispatch(world)
    world.state["game"] = "explode"
    with pytest.raises(RuntimeError):
        finish(world, "turn-1")
    world.state["game"] = "up"
    body = finish(world, "turn-1")
    assert body.get("continued") == "turn-2", body


def test_a_non_json_game_answer_is_treated_as_unreachable(world, monkeypatch):
    monkeypatch.setattr(driver_app, "UNREACHABLE_RETRY_SECONDS", 0.05)
    dispatch(world)
    world.state["game"] = "html"
    response = world.client.post(
        "/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "x"}
    )
    assert response.status_code == 202
    assert response.json() == {"ignored": "game unreachable"}
    wait_for(lambda: world.client.get("/status").json()["lastError"])


def test_a_non_json_wrapper_answer_to_the_continuation_sets_last_error(world):
    dispatch(world)
    world.state["agent"] = "html"
    response = world.client.post(
        "/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "x"}
    )
    assert response.status_code == 202
    assert "continued" not in response.json()
    assert world.client.get("/status").json()["lastError"]


def test_a_duplicate_delivery_does_not_touch_last_error(world, clock):
    """A retried duplicate must neither clear a loop-guard error nor set a
    stale one."""
    world.state["board"] = dict(SEAT_TO_ACT)
    dispatch(world)
    finish(world, "turn-1")
    for turn in range(2, 2 + driver_app.CONTINUE_QUICK_LIMIT):
        clock.now += 1
        finish(world, f"turn-{turn}")
    tripped = world.client.get("/status").json()["lastError"]
    assert tripped
    finish(world, f"turn-{1 + driver_app.CONTINUE_QUICK_LIMIT}")  # a retry, complete
    assert world.client.get("/status").json()["lastError"] == tripped
    finish(world, "turn-1", state="failed", error="late failure")
    assert world.client.get("/status").json()["lastError"] == tripped


# ---- a handover arriving while the continuation is being pushed ------------


def test_a_handover_during_the_continuation_push_wins(world):
    """`/release` lands while the continuation push is on the wire. The
    continuation must be interrupted and must not overwrite the handover's
    own state."""
    dispatch(world)

    def release_mid_push() -> None:
        world.state["on_message"] = None
        world.client.post("/release", json={"uid": world.uid})

    world.state["on_message"] = release_mid_push
    body = finish(world, "turn-1")
    assert "continued" not in body, body
    assert world.interrupts, "the continuation that raced the handover is interrupted"
    wait_for(lambda: any(t["text"].startswith(HANDOVER_OPENING) for t in world.submitted))
    continuation_id = body["turnId"]
    assert world.submitted[int(continuation_id.split("-")[1]) - 1]["text"].startswith(CONTINUE_MARK)
    status = world.client.get("/status").json()
    assert status["lastTurnId"] != continuation_id, "the continuation never became the seat's turn"


# ---- /status: "stalled" is debounced ---------------------------------------


def test_status_reports_how_long_nothing_has_been_playing(world, clock):
    dispatch(world)
    assert world.client.get("/status").json()["idleSeconds"] is None, "turn zero is live"
    world.state["agent"] = "down"
    finish(world, "turn-1")
    world.state["agent"] = "up"
    assert world.client.get("/status").json()["idleSeconds"] == 0
    clock.now += 25
    assert world.client.get("/status").json()["idleSeconds"] == 25


def test_a_turn_boundary_does_not_count_as_idle(world, clock):
    dispatch(world)
    finish(world, "turn-1")  # continued at once
    clock.now += 60
    assert world.client.get("/status").json()["idleSeconds"] is None


def test_a_busy_wrapper_is_not_idle(world, clock):
    dispatch(world)
    world.state["agent"] = "down"
    finish(world, "turn-1")
    world.state["agent"] = "up"
    world.state["session_state"] = "busy"
    clock.now += 60
    assert world.client.get("/status").json()["idleSeconds"] is None
