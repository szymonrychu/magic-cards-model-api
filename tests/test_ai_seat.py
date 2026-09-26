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
from typing import Any

import httpx
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


def test_kubernetes_why_gone_carries_the_kubernetes_status_reason(
    kubernetes_mode, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    """`why_gone` used to answer one generic "the driver reports no live
    turn for this seat" for every kubernetes seat regardless of why -
    `K8sSeats.status`'s own `reason` (here, the Job vanishing from the
    cluster, discovered by `reconcile`) is far more useful on the audit
    log."""
    from magic_cards_model_api import k8s_seats
    from magic_cards_model_api.k8s_seats import job_name

    fresh_registry.start(
        uid="u-k8s", game_id="g-k8s", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    seats = k8s_seats.get()
    kubernetes_mode.jobs.pop(job_name("g-k8s", "u-k8s"))  # the Job vanished behind our back
    with seats._lock:
        seats._seats["u-k8s"]["created_at"] -= 1000  # past the reconcile grace period
    seats.reconcile(live_game_ids=lambda: {"g-k8s"})
    reason = fresh_registry.why_gone("u-k8s")
    assert "missing" in reason and job_name("g-k8s", "u-k8s") in reason


# -- FD7 gap 15: the human-seat review Job ------------------------------------
#
# A human seat never has a driver of its own while the game is live, so it
# never gets a normal `AiSeatRegistry.start`. `start_review_followup` is the
# post-game path that reaches one anyway, kubernetes-only - see
# `k8s_seats.build_review_job`'s own docstring for exactly what it dispatches
# today and what is still missing to make it a real DR3 review.


def test_kubernetes_start_review_followup_creates_the_review_job(
    kubernetes_mode, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    from magic_cards_model_api.k8s_seats import review_job_name

    status = fresh_registry.start_review_followup(uid="u-human", game_id="g-1", player="carol")

    assert status["mechanism"] == "kubernetes"
    assert status["state"] == "scheduled"
    assert review_job_name("g-1", "u-human") in kubernetes_mode.jobs


def test_kubernetes_start_review_followup_is_deduped_by_its_own_label(
    kubernetes_mode, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    first = fresh_registry.start_review_followup(uid="u-human", game_id="g-1", player="carol")
    again = fresh_registry.start_review_followup(uid="u-human", game_id="g-1", player="carol")
    assert (first["state"], again["state"]) == ("scheduled", "exists")
    assert kubernetes_mode.creates == 1


def test_kubernetes_start_review_followup_create_failure_reports_never_raises(
    kubernetes_mode, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    kubernetes_mode.create_error = RuntimeError("kubelet is down")
    status = fresh_registry.start_review_followup(uid="u-human", game_id="g-1", player="carol")
    assert status["mechanism"] == "kubernetes"
    assert status["state"] == "failed"
    assert "kubelet is down" in status["reason"]


def test_kubernetes_start_review_followup_forwards_its_review_key(
    kubernetes_mode, fresh_registry: ai_seat.AiSeatRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FD7-r1: `lobby._start_human_seat_reviews` reads this game's admin key
    and hands it through as `review_key` - `AiSeatRegistry.
    start_review_followup` must carry it to `K8sSeats.start_review_followup`
    unchanged, since that is the only place it reaches the Job's own
    assignment (`k8s_seats.build_review_job`) and, through it, the
    `app.declare_ai_review` gate a human seat's review must pass."""
    from magic_cards_model_api import k8s_seats

    calls: list[dict] = []
    real = k8s_seats.K8sSeats.start_review_followup

    def recording(self, **kwargs):
        calls.append(kwargs)
        return real(self, **kwargs)

    monkeypatch.setattr(k8s_seats.K8sSeats, "start_review_followup", recording)
    fresh_registry.start_review_followup(
        uid="u-human", game_id="g-1", player="carol", review_key="g1-admin-key"
    )
    assert calls and calls[0]["review_key"] == "g1-admin-key"


def test_review_followup_is_never_available_outside_kubernetes_mode(
    fresh_registry: ai_seat.AiSeatRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A human seat never gets an automatic review in compose mode: compose
    has no follow-up mechanism at all, for a human seat or an AI one - the
    reason says so rather than leaving the caller to guess."""
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    status = fresh_registry.start_review_followup(uid="u-human", game_id="g-1", player="carol")
    assert status["mechanism"] is None
    assert status["state"] == "unavailable"
    assert "compose" in status["reason"] and "follow-up mechanism" in status["reason"]


# -- wrapper mode: per-seat driver routing (DR7) -----------------------------
#
# `AI_SEAT_MODE=wrapper` normally dispatches every AI seat to the single
# `driver_url()`. DR7 lets a game server list several driver base URLs
# (`DRIVER_URLS`, comma-separated) so an Opus-vs-Opus game can put its two AI
# seats on two different driver containers instead of the second one 409ing
# against the first's "one container, one match" limit
# (`mtg_agent/driver/app.py:assign`). No real driver is spawned - a fake
# multi-driver farm stands in behind `httpx.MockTransport`, keeping with this
# repo's no-real-subprocess-for-httpx-calls testing style used elsewhere
# (`test_watch_game.py`, `test_post_game_lifecycle.py`).


class _FakeDriverFarm:
    """One entry per driver base URL: whether it already holds a match
    (`holding`, a game id or `None` for idle), whether it is reachable at
    all, and how many times `/dispatch` was actually called on it - so a
    test can assert routing skipped a driver without ever calling
    `/dispatch` on it."""

    def __init__(self) -> None:
        self.drivers: dict[str, dict[str, Any]] = {}
        self.dispatch_order: list[str] = []

    def add(self, base: str, *, holding: str | None = None, unreachable: bool = False) -> None:
        self.drivers[base] = {
            "holding": holding,
            "unreachable": unreachable,
            "dispatch_calls": 0,
            "force_409_once": False,
        }

    def _base_of(self, request: httpx.Request) -> str:
        url = request.url
        port = f":{url.port}" if url.port else ""
        base = f"{url.scheme}://{url.host}{port}"
        assert base in self.drivers, f"no fake driver registered for {base}"
        return base

    def handler(self, request: httpx.Request) -> httpx.Response:
        base = self._base_of(request)
        state = self.drivers[base]
        if state["unreachable"]:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/status":
            return httpx.Response(
                200,
                json={
                    "gameId": state["holding"],
                    "lastError": None,
                    "pending": False,
                    "turnActive": False,
                },
            )
        if request.url.path == "/dispatch":
            self.dispatch_order.append(base)
            state["dispatch_calls"] += 1
            if state["holding"] is not None or state["force_409_once"]:
                state["force_409_once"] = False
                return httpx.Response(409, json={"detail": f"busy: {base}"})
            state["holding"] = "assigned-game"
            return httpx.Response(
                202,
                json={
                    "deck": "izzet-flip",
                    "turnId": "t-1",
                    "confidence": 0.9,
                    "archetype": "tempo",
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")


@pytest.fixture
def driver_farm(monkeypatch: pytest.MonkeyPatch) -> _FakeDriverFarm:
    farm = _FakeDriverFarm()

    def fake_driver_client(base_url: str) -> httpx.Client:
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(farm.handler))

    monkeypatch.setattr(ai_seat, "_driver_client", fake_driver_client)
    return farm


def test_driver_urls_reads_the_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRIVER_URLS", " http://driver-p1:8090 , http://driver-p2:8090 ")
    assert ai_seat.driver_urls() == ["http://driver-p1:8090", "http://driver-p2:8090"]


def test_driver_urls_falls_back_to_driver_url_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DRIVER_URLS", raising=False)
    monkeypatch.delenv("DRIVER_URL", raising=False)
    assert ai_seat.driver_urls() == [ai_seat.driver_url()]

    monkeypatch.setenv("DRIVER_URL", "http://driver-solo:8090")
    assert ai_seat.driver_urls() == ["http://driver-solo:8090"]


def test_two_ai_seats_in_one_game_land_on_two_different_drivers(
    monkeypatch: pytest.MonkeyPatch,
    fresh_registry: ai_seat.AiSeatRegistry,
    driver_farm: _FakeDriverFarm,
) -> None:
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("DRIVER_URLS", "http://driver-p1:8090,http://driver-p2:8090")
    driver_farm.add("http://driver-p1:8090")
    driver_farm.add("http://driver-p2:8090")

    first = fresh_registry.start(
        uid="seat-p1", game_id="g1", player="p1", api_base="http://x", agent=DEFAULT_AGENT
    )
    second = fresh_registry.start(
        uid="seat-p2", game_id="g1", player="p2", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert first["mechanism"] == "wrapper"
    assert second["mechanism"] == "wrapper"
    assert first["driver"] != second["driver"]
    assert {first["driver"], second["driver"]} == {
        "http://driver-p1:8090",
        "http://driver-p2:8090",
    }


def test_a_busy_first_driver_is_skipped_for_the_idle_second_one(
    monkeypatch: pytest.MonkeyPatch,
    fresh_registry: ai_seat.AiSeatRegistry,
    driver_farm: _FakeDriverFarm,
) -> None:
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("DRIVER_URLS", "http://driver-p1:8090,http://driver-p2:8090")
    driver_farm.add("http://driver-p1:8090", holding="some-other-game")
    driver_farm.add("http://driver-p2:8090")

    status = fresh_registry.start(
        uid="seat-p1", game_id="g1", player="p1", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "wrapper"
    assert status["driver"] == "http://driver-p2:8090"
    # The busy driver's own /status said so before any /dispatch was spent on it.
    assert driver_farm.drivers["http://driver-p1:8090"]["dispatch_calls"] == 0


def test_a_409_on_dispatch_moves_on_to_the_next_driver(
    monkeypatch: pytest.MonkeyPatch,
    fresh_registry: ai_seat.AiSeatRegistry,
    driver_farm: _FakeDriverFarm,
) -> None:
    """A race: the first driver's own `/status` still said idle, but by the
    time `/dispatch` actually reached it another caller had just claimed it -
    the driver's 409 (not the earlier `/status` probe) is what moves routing
    on to the next candidate."""
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("DRIVER_URLS", "http://driver-p1:8090,http://driver-p2:8090")
    driver_farm.add("http://driver-p1:8090")
    driver_farm.drivers["http://driver-p1:8090"]["force_409_once"] = True
    driver_farm.add("http://driver-p2:8090")

    status = fresh_registry.start(
        uid="seat-p1", game_id="g1", player="p1", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "wrapper"
    assert status["driver"] == "http://driver-p2:8090"
    assert driver_farm.dispatch_order == ["http://driver-p1:8090", "http://driver-p2:8090"]


def test_all_drivers_busy_or_unreachable_falls_back_naming_each_one(
    monkeypatch: pytest.MonkeyPatch,
    fresh_registry: ai_seat.AiSeatRegistry,
    driver_farm: _FakeDriverFarm,
) -> None:
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("DRIVER_URLS", "http://driver-p1:8090,http://driver-p2:8090")
    driver_farm.add("http://driver-p1:8090", holding="game-a")
    driver_farm.add("http://driver-p2:8090", unreachable=True)

    status = fresh_registry.start(
        uid="seat-p1", game_id="g1", player="p1", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "fallback"
    assert status["state"] == "not_started"
    assert "http://driver-p1:8090" in status["reason"]
    assert "http://driver-p2:8090" in status["reason"]


def test_two_seats_never_share_a_driver_even_with_three_candidates(
    monkeypatch: pytest.MonkeyPatch,
    fresh_registry: ai_seat.AiSeatRegistry,
    driver_farm: _FakeDriverFarm,
) -> None:
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv(
        "DRIVER_URLS", "http://driver-p1:8090,http://driver-p2:8090,http://driver-p3:8090"
    )
    driver_farm.add("http://driver-p1:8090")
    driver_farm.add("http://driver-p2:8090")
    driver_farm.add("http://driver-p3:8090")

    first = fresh_registry.start(
        uid="seat-p1", game_id="g1", player="p1", api_base="http://x", agent=DEFAULT_AGENT
    )
    second = fresh_registry.start(
        uid="seat-p2", game_id="g1", player="p2", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert first["driver"] != second["driver"]


def test_unset_driver_urls_matches_todays_single_driver_behaviour(
    monkeypatch: pytest.MonkeyPatch,
    fresh_registry: ai_seat.AiSeatRegistry,
    driver_farm: _FakeDriverFarm,
) -> None:
    """With `DRIVER_URLS` unset, `driver_urls()` is the one-element
    `[driver_url()]` list - dispatch must take exactly the single-driver path
    it always has: no `/status` probe spent before the one and only
    `/dispatch` call, success and fallback shapes unchanged."""
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.delenv("DRIVER_URLS", raising=False)
    monkeypatch.setenv("DRIVER_URL", "http://driver:8090")
    driver_farm.add("http://driver:8090")

    status = fresh_registry.start(
        uid="seat-solo", game_id="g1", player="p1", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "wrapper"
    assert status["driver"] == "http://driver:8090"
    assert status["deck"] == "izzet-flip"
    assert status["turn_id"] == "t-1"
    # Only the one call this path has always made - no idle probe first.
    assert driver_farm.dispatch_order == ["http://driver:8090"]


def test_unset_driver_urls_unreachable_driver_falls_back_with_the_old_reason_wording(
    monkeypatch: pytest.MonkeyPatch,
    fresh_registry: ai_seat.AiSeatRegistry,
    driver_farm: _FakeDriverFarm,
) -> None:
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.delenv("DRIVER_URLS", raising=False)
    monkeypatch.setenv("DRIVER_URL", "http://driver:8090")
    driver_farm.add("http://driver:8090", unreachable=True)

    status = fresh_registry.start(
        uid="seat-solo", game_id="g1", player="p1", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "fallback"
    assert status["state"] == "not_started"
    assert "the driver at http://driver:8090 could not be reached" in status["reason"]


# -- HTTP layer (ai_seat_api.py) ---------------------------------------------


def test_ai_seat_status_before_start_is_not_started(client: TestClient) -> None:
    response = client.get("/api/lobby/games/no-such-game/ai-seat/status", params={"uid": "nobody"})
    assert response.status_code == 200
    assert response.json()["state"] == "not_started"


def test_ai_seat_start_refuses_an_unknown_uid(client: TestClient, game: str) -> None:
    response = client.post(f"/api/lobby/games/{game}/ai-seat/start", json={"uid": "not-a-real-uid"})
    assert response.status_code == 404


def test_the_old_ui_prefixed_ai_seat_routes_are_gone(client: TestClient, game: str) -> None:
    """2b's T8 moved `ai_seat_api.py`'s router off `/api/ui` onto
    `/api/lobby` - proves the move happened rather than the routes being
    merely duplicated at the new path. The same T5 precedent
    (`test_the_old_ui_prefixed_deck_routes_are_gone`): 404 for a GET, since
    the catch-all static mount at `/` finds no matching file there; 405 for
    a POST, since that mount only serves GET/HEAD."""
    assert (
        client.get(f"/api/ui/games/{game}/ai-seat/status", params={"uid": "nobody"}).status_code
        == 404
    )
    assert (
        client.post(f"/api/ui/games/{game}/ai-seat/start", json={"uid": "nobody"}).status_code
        == 405
    )
    assert (
        client.post(f"/api/ui/games/{game}/ai-seat/stop", json={"uid": "nobody"}).status_code == 405
    )
