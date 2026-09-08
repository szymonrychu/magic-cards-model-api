"""The agent's coarse activity, carried from the wrapper to the game server.

The wrapper watches the transcript and publishes one word on `GET
/v1/session` (`tests/wrapper/test_activity_state.py`). The driver is the only
thing that can reach it - the agent's HTTP listener is not exposed outside
the pod - so `GET /status`, which the game server already polls for this
seat, is where that word crosses over.

Everything here is about degrading safely: an unreachable agent, a wrapper
old enough not to know the route, and a malformed answer must each leave
`/status` answering exactly what it answered before, with no activity, and
never a 500 on the play UI's own poll path.
"""

from __future__ import annotations

import pytest

from tests.driver.world import build_world


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path)


def test_status_carries_the_agent_s_current_activity(world):
    world.state["activity"] = {"state": "reading_cards", "seconds": 1.5, "seq": 7}
    assert world.client.get("/status").json()["agentActivity"] == {
        "state": "reading_cards",
        "seconds": 1.5,
        "seq": 7,
    }


def test_a_wrapper_that_has_observed_nothing_yet_reports_no_activity(world):
    world.state["activity"] = None
    assert world.client.get("/status").json()["agentActivity"] is None


def test_an_unreachable_agent_reports_no_activity_rather_than_failing_the_poll(world):
    world.state["agent"] = "down"
    status = world.client.get("/status")
    assert status.status_code == 200
    assert status.json()["agentActivity"] is None
    assert status.json()["turnActive"] is False, "every other field still answers as before"


def test_a_wrapper_that_predates_the_field_reports_no_activity(world):
    """`GET /v1/session` answering 404 is the same "not supported, continue"
    case `sync_agent_settings` already handles - never an error here."""
    world.state["session_supported"] = False
    assert world.client.get("/status").json()["agentActivity"] is None


def test_an_activity_that_is_not_an_object_is_dropped(world):
    world.state["activity"] = "reading_cards"
    assert world.client.get("/status").json()["agentActivity"] is None
