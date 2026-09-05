"""Per-game Kubernetes Job mode: the driver dispatches itself from
`DRIVER_ASSIGNMENT_JSON` at startup, and `retire` exits the process when
`DRIVER_EXIT_AFTER_MATCH` says the container plays exactly one match."""

import json

from fastapi.testclient import TestClient

from mtg_agent.driver.app import build_app
from mtg_agent.driver.config import DriverSettings
from tests.driver.test_app import world  # noqa: F401 - `world` is the fixture


def test_assignment_from_env_is_dispatched_at_startup(world, tmp_path) -> None:  # noqa: F811
    settings = DriverSettings.from_env(
        {
            "DRIVER_ASSIGNMENT_JSON": json.dumps(
                {
                    "uid": world.uid,
                    "game_id": world.game_id,
                    "player": "bot",
                    "api_base": "http://game:8000",
                    "agent": {"model": "sonnet", "effort": "high", "max_thinking_tokens": None},
                }
            ),
            "DRIVER_WORKSPACE": str(tmp_path),
            "MEMORY_ROOT": str(tmp_path / "memory"),
            "GUIDES_ROOT": str(tmp_path / "guides"),
            "GAME_API": "http://game:8000",
            "WRAPPER_API": "http://agent:8080",
        }
    )
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:  # runs the startup hook
        status = client.get("/status").json()
    assert status["gameId"] == world.game_id
    assert world.wrapper_posts[0]["text"].startswith("You are")  # turn 0 pushed


def test_retire_requests_exit_when_told_to(world, tmp_path) -> None:  # noqa: F811
    settings = DriverSettings.from_env(
        {
            "DRIVER_EXIT_AFTER_MATCH": "1",
            "DRIVER_WORKSPACE": str(tmp_path),
            "MEMORY_ROOT": str(tmp_path / "memory"),
            "GUIDES_ROOT": str(tmp_path / "guides"),
            "GAME_API": "http://game:8000",
            "WRAPPER_API": "http://agent:8080",
        }
    )
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:
        client.post(
            "/dispatch",
            json={
                "uid": world.uid,
                "game_id": world.game_id,
                "player": "bot",
                "api_base": "http://game:8000",
            },
        )
        client.post("/release", json={"uid": world.uid, "game_id": world.game_id})
    assert app.state.should_exit is True
