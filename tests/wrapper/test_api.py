import inspect

import pytest
from fastapi.testclient import TestClient

from mtg_agent.wrapper.api import build_internal_app, build_public_app
from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.metrics import Metrics
from mtg_agent.wrapper.session import Manager, State
from mtg_agent.wrapper.turns import Store
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


@pytest.fixture
def restartable(monkeypatch):
    """Like `wired`, but `spawn` hands back a fresh `FakeProc` on every call -
    `wired`'s single fixed proc can't stand in for a `restart()` (or a real
    claude respawn), which closes the current process and boots a new one."""
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.01)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 0.05)
    spawned: list[FakeProc] = []

    def spawn(_settings: Settings, _resume: bool) -> FakeProc:
        proc = FakeProc(b"ready")
        spawned.append(proc)
        return proc

    manager = Manager(fast_settings(MODEL="sonnet"), Store(), Metrics(), spawn=spawn)
    manager.start()
    public = TestClient(build_public_app(manager, manager.settings))
    yield manager, spawned, public
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


# ---- POST /v1/session: per-game model/effort/max_thinking_tokens ----------


def test_post_session_restarts_claude_with_the_new_settings(restartable):
    manager, spawned, public = restartable
    assert len(spawned) == 1
    response = public.post(
        "/v1/session", json={"model": "opus", "effort": "max", "maxThinkingTokens": 4096}
    )
    assert response.status_code == 202
    assert response.json() == {"restarting": True, "model": "opus", "effort": "max"}
    assert len(spawned) == 2, "the supervised claude must actually be relaunched"
    snapshot = public.get("/v1/session").json()
    assert snapshot["model"] == "opus"
    assert snapshot["effort"] == "max"
    assert snapshot["maxThinkingTokens"] == 4096
    assert snapshot["state"] == "ready"


def test_post_session_refuses_the_snake_case_spelling(restartable):
    """T13: this wire is camelCase only. The shape it replaced
    (`max_thinking_tokens`) must be a hard 422 over real HTTP, not a silent
    fallback to the default budget - see `SessionIn`'s own `extra="forbid"`
    docstring."""
    manager, spawned, public = restartable
    response = public.post(
        "/v1/session", json={"model": "opus", "effort": "max", "max_thinking_tokens": 4096}
    )
    assert response.status_code == 422
    assert len(spawned) == 1, "a rejected body must not restart the supervised claude"


def test_post_session_is_409_while_a_turn_is_in_flight(restartable):
    manager, spawned, public = restartable
    public.post("/v1/messages", json={"text": "a whole match's worth of turns"})
    response = public.post("/v1/session", json={"model": "opus"})
    assert response.status_code == 409
    assert len(spawned) == 1, "a busy session must not be restarted"


def test_post_session_is_503_once_the_session_is_dead(restartable):
    manager, _, public = restartable
    manager._state = State.DEAD
    assert public.post("/v1/session", json={"model": "opus"}).status_code == 503


# ---- what the agent is doing right now -------------------------------------
#
# `GET /v1/session` is the only surface the driver can reach, so it is where
# the transcript watcher's coarse activity word crosses out of this
# container. See `tests/wrapper/test_activity_state.py` for the word itself.


def test_the_session_reports_no_activity_before_anything_is_observed(wired):
    _, _, public, _ = wired
    assert public.get("/v1/session").json()["activity"] is None


def test_the_session_reports_whatever_the_watcher_last_observed(wired):
    manager, _, public, _ = wired
    manager.activity_source = lambda: {"state": "reading_cards", "seconds": 1.2, "seq": 4}
    assert public.get("/v1/session").json()["activity"] == {
        "state": "reading_cards",
        "seconds": 1.2,
        "seq": 4,
    }


def test_a_watcher_that_raises_never_takes_the_session_route_down(wired):
    """The watcher is read-only and out of the turn path by design; a hiccup
    in it must not turn the driver's own poll into a 500."""
    manager, _, public, _ = wired

    def broken():
        raise RuntimeError("the transcript went away")

    manager.activity_source = broken
    response = public.get("/v1/session")
    assert response.status_code == 200
    assert response.json()["activity"] is None


# ---- the post-game AI review: a one-shot call, entirely separate from the
# supervised interactive session `wired`/`restartable` above exercise -------
#
# `POST /v1/review` never touches `manager` at all: it runs a bounded,
# non-PTY `claude --print` subprocess through an injectable `review_runner`,
# so these tests never spawn a real `claude` and never touch the turn state
# machine a `/v1/messages` turn would.


@pytest.fixture
def review_wired(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    settings = manager.settings
    calls: list[dict] = []

    def fake_runner(settings, prompt, model, effort):
        calls.append({"prompt": prompt, "model": model, "effort": effort})
        return "the review text"

    public = TestClient(build_public_app(manager, settings, review_runner=fake_runner))
    yield public, calls
    manager.shutdown()


def test_post_review_runs_the_injected_runner_and_returns_its_text(review_wired):
    public, calls = review_wired
    response = public.post(
        "/v1/review",
        json={"prompt": "critique this game", "model": "claude-opus-5", "effort": "high"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"text": "the review text"}
    assert calls == [{"prompt": "critique this game", "model": "claude-opus-5", "effort": "high"}]


def test_post_review_without_a_prompt_is_400(review_wired):
    public, calls = review_wired
    response = public.post("/v1/review", json={"prompt": "  ", "model": "m", "effort": "high"})
    assert response.status_code == 400
    assert calls == [], "a blank prompt must never reach the runner"


def test_post_review_never_touches_the_supervised_session(review_wired):
    """The whole point of a separate one-shot call: it must not compete with
    a real turn for the single supervised claude process, and must not
    require one to be idle first (unlike `/v1/session`'s 409-while-busy)."""
    public, _ = review_wired
    started = public.post("/v1/messages", json={"text": "a turn"})
    assert started.status_code == 202
    response = public.post(
        "/v1/review", json={"prompt": "critique this game", "model": "m", "effort": "high"}
    )
    assert response.status_code == 200, response.text


def test_post_review_surfaces_a_runner_failure_as_502(monkeypatch):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)

    def failing_runner(settings, prompt, model, effort):
        from mtg_agent.wrapper.oneshot import ReviewFailed

        raise ReviewFailed("claude exited 1")

    public = TestClient(build_public_app(manager, manager.settings, review_runner=failing_runner))
    try:
        response = public.post(
            "/v1/review", json={"prompt": "critique this game", "model": "m", "effort": "high"}
        )
        assert response.status_code == 502
        assert "claude exited 1" in response.json()["detail"]
    finally:
        manager.shutdown()


def test_the_default_review_runner_is_the_real_one_shot_call(monkeypatch):
    """Every test above substitutes a fake runner, which is exactly why the
    wiring itself needs a test of its own: nothing else here would notice
    `build_public_app` losing its default, and the review route would then
    500 in production while the whole suite stayed green.

    It matters more now that the runner is not harmless. The real
    `oneshot.run_review` works this container's own workspace, HOME and
    `/guides` checkout - a test that accidentally reached it would commit
    and push, not merely be slow - so the default is asserted by identity
    and never called."""
    from mtg_agent.wrapper import oneshot
    from mtg_agent.wrapper.api import build_public_app as builder

    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    try:
        signature = inspect.signature(builder)
        assert signature.parameters["review_runner"].default is oneshot.run_review
    finally:
        manager.shutdown()


def test_post_review_is_guarded_by_the_shared_secret(monkeypatch):
    proc = FakeProc(b"ready")
    settings = fast_settings(WRAPPER_SHARED_SECRET="s3cr3t")
    manager = ready_manager(monkeypatch, proc, settings=settings)
    public = TestClient(build_public_app(manager, settings, review_runner=lambda *a: "unused"))
    try:
        response = public.post("/v1/review", json={"prompt": "x", "model": "m", "effort": "high"})
        assert response.status_code == 401
    finally:
        manager.shutdown()
