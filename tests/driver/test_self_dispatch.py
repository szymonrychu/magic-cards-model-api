"""Per-game Kubernetes Job mode: the driver dispatches itself from
`DRIVER_ASSIGNMENT_JSON` at startup, and `retire` exits the process when
`DRIVER_EXIT_AFTER_MATCH` says the container plays exactly one match.

A self-dispatch that never gets going must not leave the Job idling until
Kubernetes' own deadline finds it - a malformed assignment, an unreachable
game server, or an agent that never answers within `agent_boot_wait` are all
exercised below, each expected to record `lastError` and, when
`exit_after_match` is set, request the exit itself rather than wait to be
asked.
"""

import json

from fastapi.testclient import TestClient

from mtg_agent.driver.app import build_app
from mtg_agent.driver.config import DriverSettings
from tests.driver.test_app import (  # noqa: F401 - `world` is the fixture
    complete_handover,
    wait_for,
    world,
)

AGENT_SETTINGS = {"model": "sonnet", "effort": "high", "maxThinkingTokens": None}


def _env(tmp_path, **overrides) -> dict:
    """The environment every test here starts from - a workspace under
    `tmp_path` and the fake game/wrapper hosts `world.client_factory` routes.
    Individual tests add `DRIVER_ASSIGNMENT_JSON`, `DRIVER_EXIT_AFTER_MATCH`,
    or the boot-wait knobs on top."""
    base = {
        "DRIVER_WORKSPACE": str(tmp_path),
        "MEMORY_ROOT": str(tmp_path / "memory"),
        "GUIDES_ROOT": str(tmp_path / "guides"),
        "GAME_API": "http://game:8000",
        "WRAPPER_API": "http://agent:8080",
    }
    base.update(overrides)
    return base


def _assignment(w, **overrides) -> str:
    """A valid `DRIVER_ASSIGNMENT_JSON` body for `w`'s fake game, as JSON."""
    body = {
        "uid": w.uid,
        "game_id": w.game_id,
        "player": "bob",
        "api_base": "http://game:8000",
    }
    body.update(overrides)
    return json.dumps(body)


def test_assignment_from_env_is_dispatched_at_startup(world, tmp_path) -> None:  # noqa: F811
    settings = DriverSettings.from_env(
        _env(
            tmp_path,
            DRIVER_ASSIGNMENT_JSON=_assignment(world, agent=AGENT_SETTINGS),
        )
    )
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:  # runs the startup hook
        status = client.get("/status").json()
    assert status["gameId"] == world.game_id
    assert status["agent"] == AGENT_SETTINGS  # the agent block round-trips through env
    assert world.wrapper_posts[0]["text"].startswith("You are")  # turn 0 pushed


def test_retire_requests_exit_when_told_to(world, tmp_path) -> None:  # noqa: F811
    settings = DriverSettings.from_env(_env(tmp_path, DRIVER_EXIT_AFTER_MATCH="1"))
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:
        client.post(
            "/dispatch",
            json={
                "uid": world.uid,
                "game_id": world.game_id,
                "player": "bob",
                "api_base": "http://game:8000",
            },
        )
        client.post("/release", json={"uid": world.uid, "game_id": world.game_id})
        complete_handover(client, world.submitted)
        wait_for(lambda: app.state.should_exit)
    assert app.state.should_exit is True


def test_the_completed_match_also_exits_when_told_to(world, tmp_path) -> None:  # noqa: F811
    """`/release` is one caller of `retire`; the ordinary end-of-match path
    through `/turn-done`'s reflection turn is another, and both must exit -
    `retire` is the single place either reaches, so this guards that the exit
    signal lives there and not in one caller alone."""
    settings = DriverSettings.from_env(_env(tmp_path, DRIVER_EXIT_AFTER_MATCH="1"))
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:
        client.post(
            "/dispatch",
            json={
                "uid": world.uid,
                "game_id": world.game_id,
                "player": "bob",
                "api_base": "http://game:8000",
            },
        )
        world.state["winner"] = "bob"
        client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
        client.post(
            "/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"}
        )
        complete_handover(client, world.submitted)
        wait_for(lambda: app.state.should_exit)
    assert app.state.should_exit is True


def test_the_ai_review_completes_before_the_session_ends_and_the_process_exits(
    world,  # noqa: F811
    tmp_path,
) -> None:
    """The bug this guards: the AI review runs in a background daemon
    thread (`post_ai_review`, spawned by `turn_done`'s reflection branch),
    and `retire` used to fire `DELETE /v1/session` - which tears the agent
    container down - and, here, set `should_exit` - which ends this very
    driver process - without ever waiting on it. A handover typically
    finishes in 20-30s; an Opus review over a whole game routinely takes
    longer, so the review was lost for good on every real exit. `retire`
    must now join that thread (bounded by `RETIRE_REVIEW_JOIN_TIMEOUT`)
    before either the DELETE or the exit - proved here by a real, if short,
    delay on the fake wrapper's own `/v1/review`, and by `world.events`
    (appended to by both fake apps' route handlers, in the order their
    calls actually complete) showing the review landing first."""
    world.state["wrapper_review_delay"] = 0.3
    settings = DriverSettings.from_env(_env(tmp_path, DRIVER_EXIT_AFTER_MATCH="1"))
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:
        client.post(
            "/dispatch",
            json={
                "uid": world.uid,
                "game_id": world.game_id,
                "player": "bob",
                "api_base": "http://game:8000",
            },
        )
        world.state["winner"] = "bob"
        client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
        client.post(
            "/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"}
        )
        complete_handover(client, world.submitted)
        wait_for(lambda: app.state.should_exit)
    assert app.state.should_exit is True
    assert world.ai_review_posts, "the review must have been filed at all, not merely raced"
    assert world.events.index("ai_review") < world.events.index("delete"), (
        f"the session must not end before the AI review is filed: events were {world.events}"
    )


# ---- a self-dispatch that never gets going must not hang the Job forever --


def test_a_malformed_assignment_starts_cleanly_and_records_the_error(world, tmp_path) -> None:  # noqa: F811
    """Neither unparseable JSON nor JSON missing required fields may raise out
    of the lifespan - a Job under `backoffLimit: 0` that failed to boot would
    exit 3 and never even serve `/status` to say why."""
    for bad_json in ("not json", json.dumps({"uid": "u"})):
        settings = DriverSettings.from_env(_env(tmp_path, DRIVER_ASSIGNMENT_JSON=bad_json))
        app = build_app(settings, client_factory=world.client_factory)
        with TestClient(app) as client:
            status = client.get("/status").json()
        assert status["lastError"], bad_json


def test_a_self_dispatch_that_cannot_reach_the_game_server_exits(world, tmp_path) -> None:  # noqa: F811
    world.state["game"] = "down"
    settings = DriverSettings.from_env(
        _env(
            tmp_path,
            DRIVER_ASSIGNMENT_JSON=_assignment(world),
            DRIVER_EXIT_AFTER_MATCH="1",
        )
    )
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:
        status = client.get("/status").json()
    assert status["lastError"] is not None
    assert app.state.should_exit is True


def test_a_self_dispatch_whose_agent_never_answers_exits(world, tmp_path) -> None:  # noqa: F811
    world.state["agent"] = "down"
    settings = DriverSettings.from_env(
        _env(
            tmp_path,
            DRIVER_ASSIGNMENT_JSON=_assignment(world),
            DRIVER_EXIT_AFTER_MATCH="1",
            AGENT_BOOT_WAIT_SECONDS="0.05",
            AGENT_RETRY_INTERVAL_SECONDS="0.01",
        )
    )
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app):
        wait_for(lambda: app.state.should_exit or None, timeout=2.0)
    assert app.state.should_exit is True
