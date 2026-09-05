"""`ai_seat.py`: the headless-subprocess mechanism and its fallback.

Every test here spawns a *real* OS subprocess (never a mock of `subprocess`
itself, matching this project's no-mocks rule) - but not the real `claude`
CLI, which costs real API time/money and would make the suite flaky and slow.
`command_for` is monkeypatched to a fast, deterministic stand-in command
(`python3 -c "..."`) instead, so what is under test - process lifecycle,
status transitions, stdout/stderr capture, idempotent start, stop - is
exercised against a real process the same way it would be against `claude`,
just a cheap one.

The real `claude --print --dangerously-skip-permissions --allowedTools Bash
-- "<prompt>"` invocation this module actually runs in production was
confirmed by hand, once, outside this suite: `claude --help` documents
`--allowedTools` as variadic (`<tools...>`), which swallows an un-separated
trailing prompt argument and fails with "Input must be provided either
through stdin or as a prompt argument" - the `--` separator `command_for`
always includes is not a guess, it is what turned that failure into a
working, one-line "DONE" reply.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from magic_cards_model_api import ai_seat
from magic_cards_model_api.agent_settings import AgentSettings

#: What every `.start()` call below runs as - the exact model/effort do not
#: matter to these tests, only that some `AgentSettings` reaches the runner.
DEFAULT_AGENT = AgentSettings(model="sonnet")

# Captured at import time, before `tests/conftest.py`'s autouse fixture ever
# patches `ai_seat.command_for` to a harmless stand-in - the real function,
# for the tests below that check its actual argv/fallback text rather than a
# stand-in's.
_REAL_COMMAND_FOR = ai_seat.command_for


def wait_until(predicate, *, max_seconds: float = 5.0, poll: float = 0.05) -> None:
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(poll)
    assert predicate(), "condition never became true within the bound"


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> ai_seat.AiSeatRegistry:
    """Each test gets its own registry so runners from one test never leak
    into another's uid namespace."""
    registry = ai_seat.AiSeatRegistry()
    monkeypatch.setattr(ai_seat, "registry", registry)
    return registry


def test_build_prompt_fills_in_the_seats_own_identifiers() -> None:
    prompt = ai_seat.build_prompt(
        uid="uid-123", game_id="game-456", player="alice", api_base="http://127.0.0.1:9000"
    )
    assert "uid-123" in prompt
    assert "game-456" in prompt
    assert "alice" in prompt
    assert "http://127.0.0.1:9000" in prompt


def test_command_for_places_the_prompt_after_a_bare_dash_dash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_seat, "command_for", _REAL_COMMAND_FOR)
    # Load-bearing: `--allowedTools` is variadic in the real CLI and would
    # otherwise swallow the prompt as another tool name - see module docstring.
    command = ai_seat.command_for("a prompt with spaces", DEFAULT_AGENT)
    assert command[-2] == "--"
    assert command[-1] == "a prompt with spaces"
    assert "--dangerously-skip-permissions" in command
    assert "--allowedTools" in command


def test_claude_unavailable_reports_fallback_with_a_runnable_command(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    monkeypatch.setattr(ai_seat, "claude_available", lambda: False)
    monkeypatch.setattr(ai_seat, "command_for", _REAL_COMMAND_FOR)
    status = fresh_registry.start(
        uid="u1", game_id="g1", player="bob", api_base="http://127.0.0.1:8000", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "fallback"
    assert "claude" in status["command"]
    assert "u1" in status["prompt"]
    assert "g1" in status["prompt"]


def test_a_spawned_runner_completes_and_reports_its_output(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(
        ai_seat, "command_for", lambda prompt, agent: ["python3", "-c", "print('DONE')"]
    )
    status = fresh_registry.start(
        uid="u2", game_id="g2", player="bob", api_base="http://127.0.0.1:8000", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "subprocess"
    assert status["state"] in ("thinking", "stopped")

    wait_until(lambda: fresh_registry.status("u2")["state"] == "stopped")
    final = fresh_registry.status("u2")
    assert final["returncode"] == 0
    assert "DONE" in final["stdout_tail"]


def test_start_is_idempotent_for_the_same_uid(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    calls = []

    def fake_command(prompt: str, agent: AgentSettings) -> list[str]:
        calls.append(prompt)
        return ["python3", "-c", "print('DONE')"]

    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(ai_seat, "command_for", fake_command)
    fresh_registry.start(
        uid="u3", game_id="g3", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    fresh_registry.start(
        uid="u3", game_id="g3", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert len(calls) == 1, "a second start() for the same uid must not spawn a second process"


def test_stop_terminates_a_running_process(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(
        ai_seat,
        "command_for",
        lambda prompt, agent: ["python3", "-c", "import time; time.sleep(30)"],
    )
    fresh_registry.start(
        uid="u4", game_id="g4", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    wait_until(lambda: fresh_registry.status("u4")["state"] == "thinking")
    assert fresh_registry.stop("u4") is True
    wait_until(lambda: fresh_registry.status("u4")["state"] != "thinking", max_seconds=10)


def test_spawn_failure_falls_back_cleanly(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(ai_seat, "command_for", lambda prompt, agent: ["/no/such/binary-at-all"])
    status = fresh_registry.start(
        uid="u5", game_id="g5", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "fallback"
    assert status["state"] == "error"
    assert status["error"]


# -- kubernetes mode ----------------------------------------------------------
#
# `AI_SEAT_MODE=kubernetes` delegates to `k8s_seats.K8sSeats` rather than
# spawning a subprocess or dispatching to the driver directly - see
# `test_k8s_seats.py` for the runner itself against the same shape of fake
# Kubernetes client. This only checks the wiring: `registry.start` reaches
# `K8sSeats.start` (which is what actually creates the Job) and
# `registry.status` reports the `mechanism` the play UI keys off of.


class _FakeBatch:
    def __init__(self) -> None:
        self.jobs: dict[str, object] = {}
        self.creates = 0
        #: Set by a test to make the next `create_namespaced_job` raise it -
        #: a plain `RuntimeError`, not `kubernetes.client.exceptions
        #: .ApiException`, is exactly the shape of failure `K8sSeats.start`
        #: does not itself guard against (a transport error, say), so this is
        #: what proves `ai_seat.py`'s kubernetes branch never raises either.
        self.create_error: Exception | None = None

    def create_namespaced_job(self, namespace, body):
        import types

        if self.create_error is not None:
            raise self.create_error
        name = body["metadata"]["name"]
        self.creates += 1
        self.jobs[name] = types.SimpleNamespace(
            metadata=types.SimpleNamespace(name=name, labels=body["metadata"]["labels"])
        )
        return self.jobs[name]

    def list_namespaced_job(self, namespace, label_selector=None, **kw):
        import types

        return types.SimpleNamespace(
            items=list(self.jobs.values()), metadata=types.SimpleNamespace(resource_version="1")
        )

    def delete_namespaced_job(self, name, namespace, body=None):
        self.jobs.pop(name, None)


class _FakeCore:
    def list_namespaced_pod(self, namespace, label_selector=None, **kw):
        import types

        return types.SimpleNamespace(items=[])


@pytest.fixture
def kubernetes_mode(monkeypatch: pytest.MonkeyPatch):
    from magic_cards_model_api import k8s_seats

    monkeypatch.setenv("AI_SEAT_MODE", "kubernetes")
    batch = _FakeBatch()
    seats = k8s_seats.K8sSeats(k8s_seats.K8sSettings(), batch, _FakeCore())
    k8s_seats.set_for_tests(seats)
    yield batch
    k8s_seats.set_for_tests(None)


def test_kubernetes_mode_creates_a_job_and_reports_the_mechanism(
    kubernetes_mode, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    from magic_cards_model_api.k8s_seats import job_name

    status = fresh_registry.start(
        uid="u-k8s", game_id="g-k8s", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "kubernetes"
    assert job_name("g-k8s", "u-k8s") in kubernetes_mode.jobs

    live = fresh_registry.status("u-k8s")
    assert live is not None
    assert live["mechanism"] == "kubernetes"


def test_kubernetes_create_failure_falls_back_without_raising(
    kubernetes_mode, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    """`K8sSeats.start` only guards against `ApiException` - a transport-level
    failure underneath `create_namespaced_job` (here, a plain `RuntimeError`)
    would otherwise propagate straight out of `registry.start`, breaking the
    "never raises" contract every other mechanism keeps."""
    kubernetes_mode.create_error = RuntimeError("kubelet is down")
    status = fresh_registry.start(
        uid="u-k8s", game_id="g-k8s", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "fallback"
    assert status["state"] == "not_started"
    assert "kubelet is down" in status["reason"]
    assert kubernetes_mode.jobs == {}
    assert fresh_registry.activity("u-k8s") == ai_seat.GONE


def test_kubernetes_stop_deletes_the_job_and_allows_a_restart(
    kubernetes_mode, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    from magic_cards_model_api.k8s_seats import job_name

    fresh_registry.start(
        uid="u-k8s", game_id="g-k8s", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert kubernetes_mode.creates == 1

    assert fresh_registry.stop("u-k8s") is True
    assert job_name("g-k8s", "u-k8s") not in kubernetes_mode.jobs
    # No stale dispatch record left behind - otherwise `start()` below would
    # just hand back this now-deleted Job's old status instead of scheduling
    # a fresh one.
    assert fresh_registry.status("u-k8s") is None

    fresh_registry.start(
        uid="u-k8s", game_id="g-k8s", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert kubernetes_mode.creates == 2
    assert job_name("g-k8s", "u-k8s") in kubernetes_mode.jobs


def test_kubernetes_release_reports_the_mechanism_and_deletes_the_job(
    kubernetes_mode, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    from magic_cards_model_api.k8s_seats import job_name

    fresh_registry.start(
        uid="u-k8s", game_id="g-k8s", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    released = fresh_registry.release("u-k8s")
    assert released["mechanism"] == "kubernetes"
    assert job_name("g-k8s", "u-k8s") not in kubernetes_mode.jobs
    assert fresh_registry.status("u-k8s") is None


def test_kubernetes_activity_is_starting_before_the_pod_runs(
    kubernetes_mode, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    """`_FakeCore.list_namespaced_pod` above reports no pods at all -
    `K8sSeats.status` therefore says `pending`, which `_driver_activity` maps
    to `STARTING` for kubernetes exactly as it maps the driver's own
    `pending` for wrapper mode."""
    fresh_registry.start(
        uid="u-k8s", game_id="g-k8s", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert fresh_registry.activity("u-k8s") == ai_seat.STARTING


def test_kubernetes_activity_is_gone_when_the_cluster_errors(
    kubernetes_mode, fresh_registry: ai_seat.AiSeatRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from magic_cards_model_api import k8s_seats

    fresh_registry.start(
        uid="u-k8s", game_id="g-k8s", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )

    def boom(uid: str):
        raise RuntimeError("apiserver unreachable")

    monkeypatch.setattr(k8s_seats.get(), "status", boom)
    assert fresh_registry.activity("u-k8s") == ai_seat.GONE


# -- HTTP layer (ai_seat_api.py) ---------------------------------------------


def test_ai_seat_status_before_start_is_not_started(client: TestClient) -> None:
    response = client.get("/api/ui/games/no-such-game/ai-seat/status", params={"uid": "nobody"})
    assert response.status_code == 200
    assert response.json()["state"] == "not_started"


def test_ai_seat_start_refuses_an_unknown_uid(client: TestClient, game: str) -> None:
    response = client.post(f"/api/ui/games/{game}/ai-seat/start", json={"uid": "not-a-real-uid"})
    assert response.status_code == 404
