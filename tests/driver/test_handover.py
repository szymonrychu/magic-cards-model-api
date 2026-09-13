"""A14: the driver's handover - one idempotent `POST /handover`, reached by a
natural end, by `/release` and by the Job's preStop.

The world is `tests/driver/world.py`'s fake game server and fake wrapper, both
real FastAPI apps over ASGI. The clock the handover reads is injected, so the
420 s nudge and the budget are driven by moving a number rather than waiting.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from magic_cards_model_api.playscripts.report import script_run_table
from mtg_agent.driver import handover
from mtg_agent.driver.prompt import HANDOVER_OPENING
from tests.driver.world import ADVISOR_UID, FIXTURE_PLAYS, build_world


class Clock:
    """Monotonic seconds the test sets by hand."""

    def __init__(self) -> None:
        self.now = 0.0

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
    body = {
        "uid": uid or world.uid,
        "game_id": world.game_id,
        "player": "bob",
        "api_base": "http://game:8000",
        "role": role,
    }
    response = world.client.post("/dispatch", json=body)
    assert response.status_code == 202, response.text


def handover_turns(world) -> list[dict]:
    return [turn for turn in world.submitted if turn["text"].startswith(HANDOVER_OPENING)]


def handover_turn_id(world) -> str:
    wait_for(lambda: handover_turns(world))
    index = world.submitted.index(handover_turns(world)[0])
    return f"turn-{index + 1}"


def finish(world, turn_id: str, text: str = "pushed") -> None:
    world.client.post(
        "/turn-done", json={"turnId": turn_id, "state": "complete", "finalText": text}
    )


def marker(world):
    return world.settings.workspace / handover.MARKER_NAME


def marker_status(world) -> dict:
    wait_for(lambda: marker(world).exists())
    return json.loads(marker(world).read_text())


def section(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + len(heading) + 1)
    return text[start : end if end != -1 else len(text)]


# ---- idempotency --------------------------------------------------------------


def test_the_first_call_starts_the_handover_and_later_calls_join_it(world):
    dispatch(world)
    first = world.client.post("/handover", json={})
    second = world.client.post("/handover", json={"outcome": "released"})
    assert first.status_code == second.status_code == 202
    assert first.json()["handoverId"] == second.json()["handoverId"]
    assert second.json()["outcome"] == first.json()["outcome"] == "killed"

    finish(world, handover_turn_id(world))
    done = marker_status(world)
    later = world.client.post("/handover", json={}).json()
    assert later["handoverId"] == first.json()["handoverId"]
    assert later["state"] == done["state"] == "done"
    assert len(handover_turns(world)) == 1


def test_concurrent_calls_join_one_handover_and_push_one_turn(world):
    dispatch(world)
    barrier = threading.Barrier(6)
    answers: list[dict] = []

    def call() -> None:
        barrier.wait()
        answers.append(world.client.post("/handover", json={}).json())

    threads = [threading.Thread(target=call) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({answer["handoverId"] for answer in answers}) == 1
    finish(world, handover_turn_id(world))
    marker_status(world)
    assert len(handover_turns(world)) == 1


def test_a_turn_in_flight_is_interrupted_before_the_handover_is_pushed(world):
    dispatch(world)  # turn 0 is in flight
    world.client.post("/handover", json={})
    handover_turn_id(world)
    assert world.events.index("interrupt") < len(world.events) - 1
    assert world.events[-1] == "message"
    assert len(world.interrupts) == 1


def test_no_turn_in_flight_means_no_interrupt(world):
    dispatch(world)
    finish(world, "turn-1")  # the game is still running: nothing in flight now
    world.client.post("/handover", json={})
    handover_turn_id(world)
    assert world.interrupts == []


def test_the_outcome_is_killed_unless_a_winner_is_known(world):
    dispatch(world)
    world.state["winner"] = "bob"
    answer = world.client.post("/handover", json={}).json()
    assert answer["outcome"] == "bob won"


def test_a_handover_with_nothing_assigned_writes_the_marker_at_once(world):
    answer = world.client.post("/handover", json={}).json()
    assert answer["state"] == "idle"
    assert marker_status(world)["state"] == "idle"


# ---- release ------------------------------------------------------------------


def test_release_starts_the_handover_and_retires_only_after_the_marker(world):
    dispatch(world)
    answer = world.client.post("/release", json={"uid": world.uid, "game_id": world.game_id})
    assert answer.status_code == 202
    body = answer.json()
    assert body["released"] is True
    assert body["handover"]["outcome"] == "released"
    assert world.state["deleted"] == 0, "release no longer ends the session directly"
    assert world.client.get("/status").json()["gameId"] == world.game_id

    turn_id = handover_turn_id(world)
    assert "released" in handover_turns(world)[0]["text"]
    assert world.state["deleted"] == 0
    finish(world, turn_id)
    assert marker_status(world)["outcome"] == "released"
    wait_for(lambda: world.state["deleted"] == 1)
    assert world.state["marker_at_delete"] is True
    assert world.client.get("/status").json()["gameId"] is None


def test_a_prestop_handover_during_a_release_joins_it(world):
    dispatch(world)
    released = world.client.post("/release", json={}).json()
    joined = world.client.post("/handover", json={}).json()
    assert joined["handoverId"] == released["handover"]["handoverId"]
    assert joined["outcome"] == "released"


# ---- natural end ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("ending", "outcome"), [("winner", "bob won"), ("drawn", "the game was a draw")]
)
def test_a_natural_end_reflects_then_posts_then_hands_over_then_retires(world, ending, outcome):
    dispatch(world)
    world.state[ending] = "bob" if ending == "winner" else True
    finish(world, "turn-1", "gg")
    assert "The game is over" in world.submitted[1]["text"]
    assert handover_turns(world) == []

    finish(world, "turn-2", "wrote")
    turn_id = handover_turn_id(world)
    assert outcome in handover_turns(world)[0]["text"]
    assert world.events.index("reflection") < len(world.events) - 1
    assert world.events[-1] == "message"
    assert world.state["deleted"] == 0

    finish(world, turn_id)
    assert marker_status(world)["state"] == "done"
    wait_for(lambda: world.state["deleted"] == 1)
    assert world.state["marker_at_delete"] is True


# ---- the nudge and the budget ------------------------------------------------------


def test_one_push_now_nudge_goes_out_at_420_seconds(world, clock):
    dispatch(world)
    world.client.post("/handover", json={})
    turn_id = handover_turn_id(world)
    clock.now = handover.NUDGE_AFTER_SECONDS - 1
    time.sleep(0.2)
    assert world.probes == []
    clock.now = handover.NUDGE_AFTER_SECONDS
    wait_for(lambda: world.probes)
    assert "push now" in world.probes[0]["text"].lower()
    clock.now = handover.NUDGE_AFTER_SECONDS + 40
    time.sleep(0.2)
    assert len(world.probes) == 1
    finish(world, turn_id)
    assert marker_status(world)["nudged"] is True


# ---- the marker ---------------------------------------------------------------------


def test_the_marker_carries_the_status_on_success(world):
    dispatch(world)
    started = world.client.post("/handover", json={}).json()
    finish(world, handover_turn_id(world))
    status = marker_status(world)
    assert status["state"] == "done"
    assert status["handoverId"] == started["handoverId"]
    assert status["gameId"] == world.game_id


def test_the_marker_is_written_on_timeout(world, clock):
    dispatch(world)
    world.client.post("/handover", json={})
    handover_turn_id(world)
    clock.now = handover.HANDOVER_BUDGET_SECONDS
    assert marker_status(world)["state"] == "timeout"
    wait_for(lambda: world.state["deleted"] == 1)


def test_the_marker_is_written_on_failure(world):
    dispatch(world)
    world.state["agent"] = "refusing"
    world.client.post("/handover", json={})
    status = marker_status(world)
    assert status["state"] == "failed"
    assert status["error"]
    wait_for(lambda: world.client.get("/status").json()["gameId"] is None)


def test_an_agent_that_is_gone_fails_the_handover_rather_than_waiting_it_out(world):
    dispatch(world)
    world.state["agent"] = "down"
    world.client.post("/handover", json={})
    assert marker_status(world)["state"] == "failed"


# ---- buddies ------------------------------------------------------------------------


def test_an_advisor_gets_the_identical_handover(tmp_path, clock):
    texts = {}
    for role, uid in (("seat", None), ("advisor", ADVISOR_UID)):
        world = build_world(tmp_path / role, clock=clock)
        dispatch(world, role=role, uid=uid)
        answer = world.client.post("/handover", json={}).json()
        assert answer["role"] == role
        handover_turn_id(world)
        texts[role] = handover_turns(world)[0]["text"]
        assert world.log_reads == [uid or world.uid]
    assert texts["seat"] == texts["advisor"]


# ---- what the prompt carries -------------------------------------------------------


def test_the_run_table_for_this_game_and_seat_is_in_the_handover_prompt(world):
    world.state["log"] = [
        {
            "seq": 1,
            "action": "script_run",
            "detail": {
                "player": "bob",
                "seat": "bob",
                "game_id": world.game_id,
                "script_id": "fetch-surveil-land",
                "exit": "done",
                "priority": 50,
                "reapplicable_after": "true",
            },
        }
    ]
    dispatch(world)
    world.client.post("/handover", json={})
    handover_turn_id(world)
    text = handover_turns(world)[0]["text"]
    expected = script_run_table(world.state["log"], world.game_id, "bob")
    assert "fetch-surveil-land | 50 | 1 | done=1 | 1" in expected
    assert expected in text


def test_the_conflict_block_says_none_when_the_corpus_has_no_live_warning(world):
    dispatch(world)
    world.client.post("/handover", json={})
    handover_turn_id(world)
    block = section(handover_turns(world)[0]["text"], "## Conflict warnings")
    assert "none" in block.lower()
    assert "play-conflict" not in block


def test_the_conflict_block_names_a_live_warning_compactly(tmp_path, clock):
    world = build_world(tmp_path, clock=clock)
    plays = world.settings.guides_root / "decks" / "dimir-frog" / "plays"
    copy = json.loads((FIXTURE_PLAYS / "fetch-surveil-land.json").read_text())
    copy["id"] = "fetch-surveil-land-twin"
    (plays / "fetch-surveil-land-twin.json").write_text(json.dumps(copy))
    dispatch(world)
    world.client.post("/handover", json={})
    handover_turn_id(world)
    block = section(handover_turns(world)[0]["text"], "## Conflict warnings")
    (line,) = [line for line in block.splitlines() if line.startswith("play-conflict: ")]
    ids, _, dimensions = line.removeprefix("play-conflict: ").partition(" (")
    assert sorted(ids.split(", ")) == ["fetch-surveil-land", "fetch-surveil-land-twin"]
    assert "whose_turn" in dimensions and "steps" in dimensions
    assert "none" not in block.lower()


# ---- the preStop module --------------------------------------------------------------


def test_the_prestop_module_posts_the_local_driver_and_returns_once_the_marker_exists(tmp_path):
    workspace = tmp_path / "driver-workspace"
    workspace.mkdir()
    posted: list[tuple[str, dict]] = []

    def post(url: str, body: dict) -> None:
        posted.append((url, body))
        (workspace / handover.MARKER_NAME).write_text("{}")

    env = {"DRIVER_WORKSPACE": str(workspace), "HTTP_ADDR": ":8090"}
    started = time.monotonic()
    assert handover.main(["--wait", "30"], env=env, post=post) == 0
    assert time.monotonic() - started < 5
    assert posted == [("http://127.0.0.1:8090/handover", {})]


def test_the_prestop_module_gives_up_after_wait_seconds_and_still_exits_zero(tmp_path):
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    def unreachable(url: str, body: dict) -> None:
        raise OSError("connection refused")

    env = {"DRIVER_WORKSPACE": str(tmp_path), "HTTP_ADDR": ":8090"}
    code = handover.main(
        ["--wait", "540"], env=env, post=unreachable, clock=lambda: now[0], sleep=sleep
    )
    assert code == 0
    assert now[0] >= 540


def test_the_prestop_module_exits_zero_on_arguments_it_cannot_read(tmp_path):
    assert handover.main(["--wait", "soon"], env={"DRIVER_WORKSPACE": str(tmp_path)}) == 0
