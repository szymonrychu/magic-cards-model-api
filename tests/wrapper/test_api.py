import pytest
from fastapi.testclient import TestClient

from mtg_agent.wrapper.api import build_internal_app, build_public_app
from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.session import State
from tests.wrapper.test_session import FakeProc, fast_settings, ready_manager


@pytest.fixture
def wired(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    settings = manager.settings
    public = TestClient(build_public_app(manager, settings))
    internal = TestClient(build_internal_app(manager))
    yield manager, proc, public, internal
    manager.shutdown()


def test_post_messages_starts_a_turn(wired):
    _, proc, public, _ = wired
    response = public.post("/v1/messages", json={"text": "play a land"})
    assert response.status_code == 202
    assert response.json()["turnId"].startswith("turn-")
    assert b"".join(proc.writes).endswith(b"\r")


def test_post_messages_while_busy_is_409(wired):
    _, _, public, _ = wired
    public.post("/v1/messages", json={"text": "first"})
    assert public.post("/v1/messages", json={"text": "second"}).status_code == 409


def test_post_messages_without_text_is_400(wired):
    _, _, public, _ = wired
    assert public.post("/v1/messages", json={"text": "  "}).status_code == 400


def test_post_messages_when_dead_is_503(wired):
    manager, proc, public, _ = wired
    manager._state = State.DEAD
    assert public.post("/v1/messages", json={"text": "x"}).status_code == 503


def test_the_full_turn_round_trip_through_the_internal_endpoint(wired):
    _, _, public, internal = wired
    turn_id = public.post("/v1/messages", json={"text": "go"}).json()["turnId"]
    posted = internal.post(
        "/internal/turn-complete",
        json={
            "sessionId": "s1",
            "finalText": "I attack with the Frog",
            "transcriptPath": "/workspace/t.jsonl",
            "usage": {"output_tokens": 9},
            "stopReason": "end_turn",
            "turnTokens": [],
            "resultJson": None,
        },
    )
    assert posted.status_code == 202
    record = public.get(f"/v1/messages/{turn_id}").json()
    assert record["state"] == "complete"
    assert record["finalText"] == "I attack with the Frog"


def test_get_unknown_turn_is_404(wired):
    _, _, public, _ = wired
    assert public.get("/v1/messages/turn-nope").status_code == 404


def test_get_messages_lists_summaries_only(wired):
    _, _, public, internal = wired
    public.post("/v1/messages", json={"text": "go"})
    internal.post("/internal/turn-complete", json={"finalText": "done"})
    listed = public.get("/v1/messages").json()
    assert len(listed) == 1
    assert set(listed[0]) == {"turnId", "state", "startedAt", "completedAt", "lastActivityAt"}


def test_get_session_reports_the_snapshot(wired):
    _, _, public, _ = wired
    body = public.get("/v1/session").json()
    assert body["state"] == "ready"
    assert body["turnsCompleted"] == 0


def test_transcript_is_404_before_the_first_turn(wired):
    _, _, public, _ = wired
    assert public.get("/v1/transcript").status_code == 404


def test_transcript_is_served_once_a_turn_recorded_one(wired, tmp_path):
    manager, _, public, _ = wired
    path = tmp_path / "t.jsonl"
    path.write_text('{"type":"assistant"}\n')
    manager.transcript_path = str(path)
    response = public.get("/v1/transcript")
    assert response.status_code == 200
    assert '"type":"assistant"' in response.text


def test_probe_never_returns_409_while_busy(wired):
    _, _, public, _ = wired
    public.post("/v1/messages", json={"text": "long turn"})
    response = public.post("/v1/probe", json={})
    assert response.status_code == 202
    probe_id = response.json()["probeId"]
    assert public.get(f"/v1/probe/{probe_id}").json()["state"] == "pending"


def test_probe_without_a_live_session_is_503(wired):
    manager, _, public, _ = wired
    manager._state = State.DEAD
    assert public.post("/v1/probe", json={}).status_code == 503


def test_unknown_probe_is_404(wired):
    _, _, public, _ = wired
    assert public.get("/v1/probe/probe-999").status_code == 404


def test_interrupt_never_returns_409(wired):
    _, proc, public, _ = wired
    public.post("/v1/messages", json={"text": "go"})
    proc.writes.clear()
    response = public.post("/v1/interrupt")
    assert response.status_code == 202
    assert proc.writes == [b"\x1b"]


def test_operator_endpoints_are_open(wired):
    _, _, public, _ = wired
    assert public.get("/healthz").status_code == 200
    assert public.get("/readyz").status_code == 200
    assert b"ccw_turns_total" in public.get("/metrics").content


def test_readyz_is_503_when_the_session_is_dead(wired):
    manager, _, public, _ = wired
    manager._state = State.DEAD
    assert public.get("/readyz").status_code == 503


def test_the_shared_secret_gates_v1_but_not_the_operator_surface(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc, fast_settings(WRAPPER_SHARED_SECRET="hunter2"))
    client = TestClient(build_public_app(manager, manager.settings))
    try:
        assert client.get("/v1/session").status_code == 401
        assert client.get("/healthz").status_code == 200
        ok = client.get("/v1/session", headers={"X-Wrapper-Secret": "hunter2"})
        assert ok.status_code == 200
    finally:
        manager.shutdown()


def test_no_shared_secret_means_open(wired):
    _, _, public, _ = wired
    assert public.get("/v1/session").status_code == 200


def test_settings_default_has_no_secret():
    assert Settings.from_env({}).shared_secret == ""


def test_delete_session_stops_the_manager(wired):
    manager, proc, public, _ = wired
    response = public.delete("/v1/session")
    assert response.status_code == 202
    assert response.json() == {"stopping": True, "exiting": False}
    assert proc._closed
    assert manager._stopping is True


def test_delete_session_asks_the_process_to_exit(monkeypatch):
    """One container plays one match, so the endpoint has to end the process,
    not merely the session. The hook is asserted, not the interpreter dying -
    a real exit would take the test runner with it."""
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    exits: list[str] = []
    client = TestClient(
        build_public_app(manager, manager.settings, on_exit=lambda: exits.append("exit"))
    )
    try:
        response = client.delete("/v1/session")
        assert response.status_code == 202
        assert response.json() == {"stopping": True, "exiting": True}
        assert exits == ["exit"], "the exit hook runs after the response, exactly once"
        assert proc._closed, "the pty is closed before the process goes"
    finally:
        manager.shutdown()


def test_the_exit_hook_is_not_reachable_without_the_secret(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc, fast_settings(WRAPPER_SHARED_SECRET="hunter2"))
    exits: list[str] = []
    client = TestClient(
        build_public_app(manager, manager.settings, on_exit=lambda: exits.append("exit"))
    )
    try:
        assert client.delete("/v1/session").status_code == 401
        assert exits == []
        assert manager._stopping is False
    finally:
        manager.shutdown()
