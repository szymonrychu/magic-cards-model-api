"""A release that lands mid-turn, end to end.

The real wrapper `Manager` and its real routes, over a fake claude that, like
the live one, runs no Stop hook for the turn an interrupt cancelled; the real
driver app; `world.py`'s fake game server. Live defect: game 995daaf7, the
seat blocked in `wait_for_priority` when the owner released the game - the
buddy, whose turn had already ended, handed over, and the seat's driver got
409 on every handover push until its 510 s budget ran out, with no marker.
"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from mtg_agent.driver import handover
from mtg_agent.driver.app import build_app
from mtg_agent.driver.prompt import HANDOVER_OPENING
from mtg_agent.wrapper.api import build_internal_app, build_public_app
from mtg_agent.wrapper.turns import TurnState
from tests.driver.world import build_world
from tests.wrapper.test_session import FakeProc, fast_settings, ready_manager


def wait_for(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def test_a_release_mid_turn_delivers_the_handover_turn_and_writes_a_done_marker(
    tmp_path, monkeypatch
):
    world = build_world(tmp_path)
    proc = FakeProc(b"ready")
    wrapper_settings = fast_settings(INTERRUPT_SETTLE_SECONDS="0.3", TURN_TIMEOUT_SECONDS="60")
    manager = ready_manager(monkeypatch, proc, wrapper_settings)
    public = build_public_app(manager, wrapper_settings)
    internal = TestClient(build_internal_app(manager))
    answers: list[tuple[str, int]] = []

    def record(response) -> None:
        answers.append((response.request.url.path, response.status_code))

    def client_factory(base_url: str):
        if "game" in base_url:
            return world.client_factory(base_url)
        client = TestClient(public, base_url=base_url)
        client.event_hooks = {"response": [record]}
        return client

    driver = TestClient(build_app(world.settings, client_factory=client_factory))
    manager.on_turn_done = lambda done: driver.post("/turn-done", json=done.full())
    try:
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
        turn_zero = wait_for(lambda: manager.current_turn_id)

        # The seat is blocked in a long MCP wait when the owner releases it.
        released = driver.post("/release", json={"game_id": world.game_id})
        assert released.status_code == 202, released.text

        wait_for(lambda: HANDOVER_OPENING.encode() in b"".join(proc.writes))
        handover_turn = manager.current_turn_id
        assert handover_turn is not None
        assert handover_turn != turn_zero
        assert manager.store.get(turn_zero).state is TurnState.FAILED

        # claude finishes the handover turn, and its Stop hook reports it.
        internal.post(
            "/internal/turn-complete", json={"finalText": "handed over", "stopReason": "end_turn"}
        )
        marker = world.settings.workspace / handover.MARKER_NAME
        wait_for(marker.exists)
        status = json.loads(marker.read_text())
        assert status["state"] == "done"
        assert status["error"] is None
        assert status["interrupted"] is True
        assert status["turnId"] == handover_turn
        conflicts = [path for path, code in answers if path == "/v1/messages" and code == 409]
        assert len(conflicts) <= 5, f"{len(conflicts)} handover pushes answered 409"
    finally:
        manager.shutdown()
