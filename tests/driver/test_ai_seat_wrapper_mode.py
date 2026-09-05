import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magic_cards_model_api import ai_seat
from magic_cards_model_api.agent_settings import AgentSettings

#: The agent settings threaded through every `.start()` call below - what
#: matters for these tests is that *some* `AgentSettings` reaches the
#: dispatch payload, not which one; `test_ai_seat_settings.py` pins the exact
#: shape of that payload for a chosen model/effort.
DEFAULT_AGENT = AgentSettings(model="sonnet")


@pytest.fixture
def driver(monkeypatch):
    """A real FastAPI stand-in for the driver, driven over ASGI by
    `TestClient` (itself an `httpx.Client`)."""
    received: list[dict] = []
    app = FastAPI()

    @app.post("/dispatch", status_code=202)
    def dispatch(body: dict):
        received.append(body)
        return {"deck": "dimir-frog", "turnId": "turn-1", "matchScore": 0.98}

    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("DRIVER_URL", "http://driver:8090")
    monkeypatch.setattr(ai_seat, "_driver_client", lambda base: TestClient(app, base_url=base))
    return received


def test_wrapper_mode_posts_the_seat_to_the_driver(driver):
    registry = ai_seat.AiSeatRegistry()
    status = registry.start(
        uid="secret-uid",
        game_id="g-1",
        player="bob",
        api_base="http://game:8000",
        agent=DEFAULT_AGENT,
    )
    assert status["mechanism"] == "wrapper"
    assert status["state"] == "dispatched"
    assert status["deck"] == "dimir-frog"
    assert driver[0] == {
        "uid": "secret-uid",
        "game_id": "g-1",
        "player": "bob",
        "api_base": "http://game:8000",
        "agent": DEFAULT_AGENT.model_dump(),
    }


def test_wrapper_mode_never_spawns_a_subprocess(driver, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("wrapper mode must not spawn claude")

    monkeypatch.setattr(ai_seat.subprocess, "Popen", explode)
    registry = ai_seat.AiSeatRegistry()
    registry.start(
        uid="u", game_id="g", player="bob", api_base="http://game:8000", agent=DEFAULT_AGENT
    )


def test_a_second_start_for_the_same_uid_dispatches_once(driver):
    registry = ai_seat.AiSeatRegistry()
    registry.start(
        uid="u", game_id="g", player="bob", api_base="http://game:8000", agent=DEFAULT_AGENT
    )
    registry.start(
        uid="u", game_id="g", player="bob", api_base="http://game:8000", agent=DEFAULT_AGENT
    )
    assert len(driver) == 1


def test_an_unreachable_driver_reports_a_fallback_not_an_exception(monkeypatch):
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("DRIVER_URL", "http://driver:8090")

    def broken(base):
        raise httpx.ConnectError("no route to driver")

    monkeypatch.setattr(ai_seat, "_driver_client", broken)
    status = ai_seat.AiSeatRegistry().start(
        uid="u", game_id="g", player="bob", api_base="http://game:8000", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "fallback"
    assert "driver" in status["reason"]


def test_subprocess_stays_the_default_mode(monkeypatch):
    monkeypatch.delenv("AI_SEAT_MODE", raising=False)
    assert ai_seat.ai_seat_mode() == "subprocess"


def test_the_driver_url_defaults_to_the_compose_service(monkeypatch):
    monkeypatch.delenv("DRIVER_URL", raising=False)
    assert ai_seat.driver_url() == "http://driver:8090"


def test_status_and_stop_are_harmless_for_a_wrapper_seat(driver):
    registry = ai_seat.AiSeatRegistry()
    registry.start(
        uid="u", game_id="g", player="bob", api_base="http://game:8000", agent=DEFAULT_AGENT
    )
    assert registry.status("u")["mechanism"] == "wrapper"
    assert registry.stop("u") is False


# ---- the status the play UI reads has to be live, not a dispatch snapshot ----
#
# The freeze: the agent's claude hit `API Error: 401` mid-game and stopped.
# `AiSeatRegistry.status` returned the dict built at dispatch time, so
# `GET /api/ui/games/{id}/ai-seat/status` said `state: "dispatched"` for the
# rest of the game no matter what became of the agent, and the play UI had
# nothing to show the human but a seat that was, as far as it knew, fine.


@pytest.fixture
def driver_with_status(monkeypatch):
    """The driver stand-in, plus a dict the test uses to move `/status`."""
    reported = {
        "lastTurnId": "turn-1",
        "reflected": False,
        "pending": False,
        "turnActive": True,
        "lastError": None,
    }
    app = FastAPI()

    @app.post("/dispatch", status_code=202)
    def dispatch(body: dict):
        return {"deck": "dimir-frog", "turnId": "turn-1", "matchScore": 0.98}

    @app.get("/status")
    def status():
        return reported

    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("DRIVER_URL", "http://driver:8090")
    monkeypatch.setattr(ai_seat, "_driver_client", lambda base: TestClient(app, base_url=base))
    return reported


def started(uid: str = "u") -> ai_seat.AiSeatRegistry:
    registry = ai_seat.AiSeatRegistry()
    registry.start(
        uid=uid, game_id="g", player="claude", api_base="http://game:8000", agent=DEFAULT_AGENT
    )
    return registry


def test_status_reports_a_healthy_seat_as_dispatched(driver_with_status):
    status = started().status("u")
    assert status["state"] == "dispatched"
    assert status["last_error"] is None
    assert status["last_turn_id"] == "turn-1"


def test_status_turns_red_once_the_driver_knows_the_turn_failed(driver_with_status):
    registry = started()
    assert registry.status("u")["state"] == "dispatched"
    driver_with_status["lastError"] = "turn turn-1 failed: API Error: 401"
    status = registry.status("u")
    assert status["state"] == "error"
    assert "401" in status["last_error"], "the UI has to be able to say why"


def test_an_unreachable_driver_is_reported_rather_than_raised(monkeypatch):
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("DRIVER_URL", "http://driver:8090")
    app = FastAPI()

    @app.post("/dispatch", status_code=202)
    def dispatch(body: dict):
        return {"deck": None, "turnId": "turn-1", "matchScore": 0.0}

    monkeypatch.setattr(ai_seat, "_driver_client", lambda base: TestClient(app, base_url=base))
    registry = started()

    def broken(base):
        raise httpx.ConnectError("no route to driver")

    monkeypatch.setattr(ai_seat, "_driver_client", broken)
    status = registry.status("u")
    assert status["state"] == "unreachable"
    assert "driver unreachable" in status["last_error"]


def test_a_status_read_never_dispatches_a_second_seat(driver_with_status):
    registry = started()
    for _ in range(3):
        registry.status("u")
    assert registry.status("u")["turn_id"] == "turn-1"


# ---- activity: the three-way answer the pregame backstop settles seats on ---
#
# `status` above is written for a human reading a panel. `activity` answers one
# question for one caller (`ai_pregame.py`): is anything actually playing this
# seat. It has to distinguish a booting container from a thinking agent from a
# seat nothing holds any more, because the backstop treats those three
# completely differently - and getting it wrong in either direction either
# overrules a live agent mid-decision or strands a human at a dead panel.


def test_a_seat_with_a_live_turn_is_working(driver_with_status):
    driver_with_status["turnActive"] = True
    assert started().activity("u") == ai_seat.WORKING


def test_a_seat_whose_agent_is_still_booting_is_starting(driver_with_status):
    """Handed over but not yet able to act - no clock should run against it."""
    driver_with_status["pending"] = True
    driver_with_status["turnActive"] = False
    assert started().activity("u") == ai_seat.STARTING


def test_a_seat_whose_turn_has_finished_is_gone(driver_with_status):
    """One turn covers the whole match, so a finished turn mid-game is an
    agent that stopped playing - the reported hang."""
    driver_with_status["turnActive"] = False
    assert started().activity("u") == ai_seat.GONE


def test_a_seat_whose_turn_failed_is_gone(driver_with_status):
    driver_with_status["turnActive"] = True
    driver_with_status["lastError"] = "turn turn-1 failed: API Error: 401"
    assert started().activity("u") == ai_seat.GONE


def test_a_seat_behind_an_unreachable_driver_is_gone(driver_with_status, monkeypatch):
    registry = started()

    def broken(base):
        raise httpx.ConnectError("no route to driver")

    monkeypatch.setattr(ai_seat, "_driver_client", broken)
    assert registry.activity("u") == ai_seat.GONE


def test_a_seat_nobody_ever_started_is_gone():
    """Not "starting". Nothing has claimed this seat, and a human waiting on
    it is waiting on nobody."""
    assert ai_seat.AiSeatRegistry().activity("never-seen") == ai_seat.GONE


def test_a_seat_that_could_not_start_says_why(monkeypatch):
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")

    def broken(base):
        raise httpx.ConnectError("no route to driver")

    monkeypatch.setattr(ai_seat, "_driver_client", broken)
    registry = ai_seat.AiSeatRegistry()
    registry.start(
        uid="u", game_id="g", player="bob", api_base="http://game:8000", agent=DEFAULT_AGENT
    )
    assert registry.activity("u") == ai_seat.GONE
    assert "driver" in registry.why_gone("u")
