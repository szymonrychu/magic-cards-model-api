import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magic_cards_model_api import ai_seat


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
        uid="secret-uid", game_id="g-1", player="bob", api_base="http://game:8000"
    )
    assert status["mechanism"] == "wrapper"
    assert status["state"] == "dispatched"
    assert status["deck"] == "dimir-frog"
    assert driver[0] == {
        "uid": "secret-uid",
        "game_id": "g-1",
        "player": "bob",
        "api_base": "http://game:8000",
    }


def test_wrapper_mode_never_spawns_a_subprocess(driver, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("wrapper mode must not spawn claude")

    monkeypatch.setattr(ai_seat.subprocess, "Popen", explode)
    registry = ai_seat.AiSeatRegistry()
    registry.start(uid="u", game_id="g", player="bob", api_base="http://game:8000")


def test_a_second_start_for_the_same_uid_dispatches_once(driver):
    registry = ai_seat.AiSeatRegistry()
    registry.start(uid="u", game_id="g", player="bob", api_base="http://game:8000")
    registry.start(uid="u", game_id="g", player="bob", api_base="http://game:8000")
    assert len(driver) == 1


def test_an_unreachable_driver_reports_a_fallback_not_an_exception(monkeypatch):
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("DRIVER_URL", "http://driver:8090")

    def broken(base):
        raise httpx.ConnectError("no route to driver")

    monkeypatch.setattr(ai_seat, "_driver_client", broken)
    status = ai_seat.AiSeatRegistry().start(
        uid="u", game_id="g", player="bob", api_base="http://game:8000"
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
    registry.start(uid="u", game_id="g", player="bob", api_base="http://game:8000")
    assert registry.status("u")["mechanism"] == "wrapper"
    assert registry.stop("u") is False
