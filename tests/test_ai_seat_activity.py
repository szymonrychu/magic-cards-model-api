"""The agent's coarse activity, carried from the driver onto the seat status
the play UI already polls.

`GET /api/lobby/games/{id}/seats` hands the browser `ai_seat.registry.
status(uid)` verbatim, and that is where the label the human reads comes
from. Two of the three mechanisms can carry it: `wrapper` (the driver's own
`/status`) and `kubernetes` (`K8sSeats.status`, which reads the same driver).
`subprocess` cannot - there is no wrapper watching a transcript there - and
must keep answering exactly what it answered before.
"""

from __future__ import annotations

import types

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magic_cards_model_api import ai_seat
from magic_cards_model_api.agent_settings import AgentSettings

DEFAULT_AGENT = AgentSettings(model="sonnet")
ACTIVITY = {"state": "reading_cards", "seconds": 2.0, "seq": 9}


@pytest.fixture
def driver_status(monkeypatch):
    """A driver whose `/status` answer the test can rewrite in place."""
    reported: dict = {
        "lastTurnId": "turn-1",
        "pending": False,
        "turnActive": True,
        "lastError": None,
        "agentActivity": dict(ACTIVITY),
    }
    app = FastAPI()

    @app.post("/dispatch", status_code=202)
    def dispatch(body: dict):
        return {"deck": None, "turnId": "turn-1", "matchScore": 0.0}

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


# ---- wrapper mode ----------------------------------------------------------


def test_a_wrapper_seat_carries_the_activity_onto_its_status(driver_status):
    assert started().status("u")["activity"] == ACTIVITY


def test_a_driver_that_reports_no_activity_leaves_the_key_empty(driver_status):
    driver_status["agentActivity"] = None
    assert started().status("u")["activity"] is None


def test_a_driver_old_enough_not_to_report_activity_at_all_is_not_an_error(driver_status):
    del driver_status["agentActivity"]
    status = started().status("u")
    assert status["activity"] is None
    assert status["state"] == "dispatched"


def test_an_unreachable_driver_reports_no_activity(driver_status, monkeypatch):
    registry = started()

    def broken(base):
        raise httpx.ConnectError("no route to driver")

    monkeypatch.setattr(ai_seat, "_driver_client", broken)
    status = registry.status("u")
    assert status["state"] == "unreachable"
    assert status.get("activity") is None


# ---- subprocess mode -------------------------------------------------------


def test_a_subprocess_seat_reports_no_activity_at_all(monkeypatch):
    """No wrapper, no transcript watcher, nothing observed - the UI has to
    fall back to the plain state word, so the key must simply not be there
    rather than carry an invented one."""
    monkeypatch.setenv("AI_SEAT_MODE", "subprocess")
    runner = ai_seat.AiSeatRunner(
        uid="u", game_id="g", player="claude", api_base="http://game:8000", agent=DEFAULT_AGENT
    )
    assert "activity" not in runner.status()


# ---- kubernetes mode -------------------------------------------------------


class FakeDriverHttp:
    """The `http` factory `K8sSeats` is built with - a context manager whose
    `get` answers one canned driver `/status`."""

    def __init__(self, status: dict) -> None:
        self.status = status

    def __call__(self, base: str):
        outer = self

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, path, **kw):
                return types.SimpleNamespace(status_code=200, json=lambda: outer.status)

            def post(self, path, **kw):
                return types.SimpleNamespace(status_code=202, json=lambda: {})

        return Client()


def k8s_seats_with(driver_status: dict):
    from magic_cards_model_api.k8s_seats import K8sSeats, K8sSettings
    from tests.test_k8s_seats import ENV, FakeBatch, FakeCore

    seats = K8sSeats(
        K8sSettings.from_env(ENV),
        FakeBatch(),
        FakeCore(),
        FakeDriverHttp(driver_status),
        reconcile_interval=0,
    )
    seats.start(uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet"))
    return seats


def test_a_kubernetes_seat_carries_the_activity_onto_its_status():
    seats = k8s_seats_with(
        {"turnActive": True, "pending": False, "lastError": None, "agentActivity": dict(ACTIVITY)}
    )
    status = seats.status("u1")
    assert status["state"] == "thinking"
    assert status["activity"] == ACTIVITY


def test_a_kubernetes_seat_whose_driver_reports_nothing_leaves_the_key_empty():
    seats = k8s_seats_with({"turnActive": True, "pending": False, "lastError": None})
    assert seats.status("u1")["activity"] is None


def test_a_kubernetes_seat_that_is_not_running_yet_reports_no_activity():
    from magic_cards_model_api.k8s_seats import K8sSeats, K8sSettings
    from tests.test_k8s_seats import ENV, FakeBatch, FakeCore

    seats = K8sSeats(
        K8sSettings.from_env(ENV),
        FakeBatch(),
        FakeCore(phase="Pending", pod_ip=None),
        FakeDriverHttp({}),
        reconcile_interval=0,
    )
    seats.start(uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet"))
    status = seats.status("u1")
    assert status["state"] == "pending"
    assert status.get("activity") is None
