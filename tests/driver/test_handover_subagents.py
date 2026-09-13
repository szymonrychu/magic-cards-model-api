"""A handover whose subagent outlives the main turn's first Stop hook.

The real wrapper `Manager` and its real routes, fed Stop hook results the real
`stop_hook.build_result` reads off a transcript shaped like claude's; the real
driver app; `world.py`'s fake game server. Live defect: game 8a55cab7, a
seat's handover turn launched a script subagent and ended; the Stop hook
completed the turn, the driver wrote `handover.done`, and the pod could stop
before the subagent's work was in.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi.testclient import TestClient

from mtg_agent.driver import handover
from mtg_agent.driver.app import build_app
from mtg_agent.driver.prompt import HANDOVER_OPENING
from mtg_agent.wrapper import stop_hook
from mtg_agent.wrapper.api import build_internal_app, build_public_app
from mtg_agent.wrapper.transcript import turn_marker
from mtg_agent.wrapper.turns import TurnState
from tests.driver.world import build_world
from tests.wrapper.test_background_tasks import (
    agent_launch,
    append,
    assistant_text,
    hook_payload,
    notification,
    prompt,
    write,
)
from tests.wrapper.test_session import FakeProc, fast_settings, ready_manager


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def wait_for(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def handover_stack(tmp_path, monkeypatch, clock=None):
    world = build_world(tmp_path, clock=clock)
    proc = FakeProc(b"ready")
    wrapper_settings = fast_settings(INTERRUPT_SETTLE_SECONDS="0.3", TURN_TIMEOUT_SECONDS="60")
    manager = ready_manager(monkeypatch, proc, wrapper_settings)
    public = build_public_app(manager, wrapper_settings)
    internal = TestClient(build_internal_app(manager))

    def client_factory(base_url: str):
        if "game" in base_url:
            return world.client_factory(base_url)
        return TestClient(public, base_url=base_url)

    driver = TestClient(build_app(world.settings, client_factory=client_factory, clock=clock))
    manager.on_turn_done = lambda done: driver.post("/turn-done", json=done.full())
    dispatched = driver.post(
        "/dispatch",
        json={
            "uid": world.uid,
            "game_id": world.game_id,
            "player": "bob",
            "api_base": "http://game:8000",
        },
    )
    assert dispatched.status_code == 202, dispatched.text
    wait_for(lambda: manager.current_turn_id)
    released = driver.post("/release", json={"game_id": world.game_id})
    assert released.status_code == 202, released.text
    wait_for(lambda: HANDOVER_OPENING.encode() in b"".join(proc.writes))
    turn_id = wait_for(lambda: manager.current_turn_id)
    return world, manager, internal, turn_id


def stop(internal, path, tmp_path) -> None:
    result = stop_hook.build_result(hook_payload(path), tmp_path / "absent.json")
    internal.post("/internal/turn-complete", json=result)


def test_done_is_written_only_after_the_subagent_result_is_in(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    world, manager, internal, turn_id = handover_stack(tmp_path, monkeypatch)
    marker = world.settings.workspace / handover.MARKER_NAME
    transcript = tmp_path / "t.jsonl"
    try:
        write(
            transcript,
            [
                prompt(f"{HANDOVER_OPENING}\n\n{turn_marker(turn_id)}"),
                *agent_launch("toolu_A", "a1"),
                assistant_text("priorities admitted; a subagent is developing a script"),
            ],
        )
        # The main turn's first Stop hook, with the subagent still running.
        stop(internal, transcript, tmp_path)
        time.sleep(0.5)
        assert not marker.exists()
        assert manager.current_turn_id == turn_id
        assert manager.store.get(turn_id).state is TurnState.RUNNING

        # The subagent reports; claude answers it, admits, and stops again.
        append(
            transcript,
            [prompt(notification("toolu_A", "a1")), assistant_text("script admitted")],
        )
        stop(internal, transcript, tmp_path)
        wait_for(marker.exists)
        status = json.loads(marker.read_text())
        assert status["state"] == "done"
        assert status["turnId"] == turn_id

        messages = [record.getMessage() for record in caplog.records]
        finished = next(i for i, m in enumerate(messages) if "background tasks finished" in m)
        marked = next(
            i for i, m in enumerate(messages) if m.startswith("handover ") and m.endswith(": done")
        )
        assert finished < marked
    finally:
        manager.shutdown()


def test_a_subagent_that_never_reports_still_ends_at_the_budget(tmp_path, monkeypatch):
    clock = Clock()
    world, manager, internal, turn_id = handover_stack(tmp_path, monkeypatch, clock)
    marker = world.settings.workspace / handover.MARKER_NAME
    transcript = tmp_path / "t.jsonl"
    try:
        write(
            transcript,
            [
                prompt(f"{HANDOVER_OPENING}\n\n{turn_marker(turn_id)}"),
                *agent_launch("toolu_A", "a1"),
            ],
        )
        stop(internal, transcript, tmp_path)
        time.sleep(0.3)
        assert not marker.exists()

        clock.now = handover.HANDOVER_BUDGET_SECONDS
        wait_for(marker.exists)
        assert json.loads(marker.read_text())["state"] == "timeout"
    finally:
        manager.shutdown()
