from __future__ import annotations

import json
import threading
import types

import pytest
from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException

from magic_cards_model_api import k8s_seats
from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.k8s_seats import (
    K8sSeats,
    K8sSettings,
    build_job,
    job_name,
    mcp_config,
)

ENV = {
    "AGENT_NAMESPACE": "mtg-engine",
    "AGENT_IMAGE": "harbor/agent:abc",
    "API_IMAGE": "harbor/api:abc",
    "POD_NAME": "mtg-stack-1",
    "POD_UID": "uid-1",
    "POD_NAMESPACE": "mtg-engine",
    "SPELLSLINGER_MCP_URL": "http://ss:8080/mcp",
}


def test_job_name_is_short_and_stable() -> None:
    assert job_name("0123456789abcdef", "fedcba9876543210") == "mtg-seat-01234567-fedcba98"


def test_mcp_config_binds_the_game() -> None:
    cfg = mcp_config(K8sSettings.from_env(ENV), "g1")
    assert cfg["mcpServers"]["mtg"]["args"] == [
        "-m",
        "magic_cards_model_api.mcp",
        "--game",
        "g1",
        "--api",
        "http://mtg-stack.mtg-engine.svc.cluster.local:8000",
    ]
    assert cfg["mcpServers"]["spellslinger"]["url"] == "http://ss:8080/mcp"


def test_job_shape() -> None:
    job = build_job(
        K8sSettings.from_env(ENV),
        game_id="g1",
        uid="u1",
        player="bot",
        agent=AgentSettings(model="opus", effort="max", max_thinking_tokens=0),
    )
    meta = job["metadata"]
    assert meta["name"] == job_name("g1", "u1")
    assert meta["labels"] == {
        "app.kubernetes.io/name": "mtg-agent",
        "mtg/game-id": "g1",
        "mtg/seat-uid": "u1",
    }
    assert meta["ownerReferences"] == [
        {"apiVersion": "v1", "kind": "Pod", "name": "mtg-stack-1", "uid": "uid-1"}
    ]
    spec = job["spec"]
    assert (
        spec["backoffLimit"] == 0
        and spec["ttlSecondsAfterFinished"] == 600
        and spec["activeDeadlineSeconds"] == 14400
    )
    pod = spec["template"]["spec"]
    assert pod["restartPolicy"] == "Never" and pod["automountServiceAccountToken"] is False
    names = [c["name"] for c in pod["containers"]]
    assert names == ["agent", "driver"]
    agent = pod["containers"][0]
    env = {e["name"]: e.get("value") for e in agent["env"]}
    assert env["MODEL"] == "opus" and env["EFFORT"] == "max" and env["MAX_THINKING_TOKENS"] == "0"
    assert {"secretRef": {"name": "mtg-agent"}} in agent["envFrom"]
    driver = pod["containers"][1]
    denv = {e["name"]: e.get("value") for e in driver["env"]}
    assert denv["DRIVER_EXIT_AFTER_MATCH"] == "1"
    assert '"game_id": "g1"' in denv["DRIVER_ASSIGNMENT_JSON"]
    assert agent["securityContext"]["allowPrivilegeEscalation"] is False
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}


def _build(env: dict[str, str]) -> dict:
    return build_job(
        K8sSettings.from_env(env),
        game_id="g1",
        uid="u1",
        player="bot",
        agent=AgentSettings(model="sonnet"),
    )


def test_owner_references_absent_when_uid_missing() -> None:
    env = {k: v for k, v in ENV.items() if k != "POD_UID"}
    job = _build(env)
    assert "ownerReferences" not in job["metadata"]


def test_owner_references_absent_when_name_missing() -> None:
    env = {k: v for k, v in ENV.items() if k != "POD_NAME"}
    job = _build(env)
    assert "ownerReferences" not in job["metadata"]


def test_owner_references_absent_when_namespaces_differ() -> None:
    env = {**ENV, "POD_NAMESPACE": "some-other-namespace"}
    job = _build(env)
    assert "ownerReferences" not in job["metadata"]


def test_mcp_json_round_trips_to_mcp_config() -> None:
    settings = K8sSettings.from_env(ENV)
    job = build_job(
        settings, game_id="g1", uid="u1", player="bot", agent=AgentSettings(model="sonnet")
    )
    init_containers = job["spec"]["template"]["spec"]["initContainers"]
    mcp_container = next(c for c in init_containers if c["name"] == "mcp-config")
    mcp_env = {e["name"]: e["value"] for e in mcp_container["env"]}
    assert json.loads(mcp_env["MCP_JSON"]) == mcp_config(settings, "g1")


def test_init_containers_present_in_order_with_security_context() -> None:
    job = _build(ENV)
    init_containers = job["spec"]["template"]["spec"]["initContainers"]
    names = [c["name"] for c in init_containers]
    assert names == ["guides", "mcp-config"]
    for container in init_containers:
        assert container["securityContext"]["allowPrivilegeEscalation"] is False


def test_job_is_plain_dicts_the_kubernetes_client_can_serialize() -> None:
    # `create_namespaced_job(namespace, body=dict)` hands this straight to
    # `ApiClient.sanitize_for_serialization`, which branches on
    # `isinstance(obj, dict)` - anything that fails that check (a
    # `types.MappingProxyType`, say) falls into the model-attribute branch and
    # raises `AttributeError: '...' object has no attribute 'openapi_types'`.
    # This is the exact code path that would have caught the shared,
    # module-level `CONTAINER_SECURITY` mapping-proxy regression.
    job = _build(ENV)
    json.dumps(job)
    sanitized = k8s_client.ApiClient().sanitize_for_serialization(job)
    assert isinstance(sanitized, dict)


# ---- K8sSeats: create, watch, reconcile, status, stop -----------------------
#
# The one allowed fake in this project: a hand-rolled stand-in for the
# Kubernetes API client, not a mock of behaviour under test. Everything the
# runner actually does - build the Job body, read pod status, talk to the
# driver sidecar over HTTP, decide state - runs for real against these fakes.


class FakeBatch:
    def __init__(self):
        self.jobs = {}
        self.deleted = []
        self.delete_calls = []  # (name, body) - so a test can check propagation_policy
        self.creates = 0  # only incremented on an actual successful create
        #: Set by a test to make the *next* `create_namespaced_job` raise
        #: this instead of creating anything - cleared automatically after
        #: it fires once, so a retry after the failure behaves normally.
        self.raise_once: Exception | None = None

    def create_namespaced_job(self, namespace, body):
        if self.raise_once is not None:
            exc, self.raise_once = self.raise_once, None
            raise exc
        name = body["metadata"]["name"]
        if name in self.jobs:
            raise ApiException(status=409, reason="AlreadyExists")
        self.creates += 1
        self.jobs[name] = types.SimpleNamespace(
            metadata=types.SimpleNamespace(name=name, labels=body["metadata"]["labels"]),
            status=types.SimpleNamespace(active=1, succeeded=0, failed=0),
        )
        return self.jobs[name]

    def list_namespaced_job(self, namespace, label_selector=None, **kw):
        return types.SimpleNamespace(
            items=list(self.jobs.values()), metadata=types.SimpleNamespace(resource_version="1")
        )

    def delete_namespaced_job(self, name, namespace, body=None):
        self.deleted.append(name)
        self.delete_calls.append((name, body))
        self.jobs.pop(name, None)


class FakeCore:
    def __init__(self, pod_ip="10.0.0.5", phase="Running"):
        self.pod_ip, self.phase = pod_ip, phase

    def list_namespaced_pod(self, namespace, label_selector=None, **kw):
        pod = types.SimpleNamespace(
            status=types.SimpleNamespace(
                pod_ip=self.pod_ip, phase=self.phase, container_statuses=[]
            ),
            metadata=types.SimpleNamespace(name="p1"),
        )
        return types.SimpleNamespace(items=[pod])


class FakeDriverHttp:
    def __init__(self, status):
        self.status, self.posts, self.gets = status, [], []

    def __call__(self, base):
        outer = self

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, path, **kw):
                outer.gets.append((base, path))
                return types.SimpleNamespace(status_code=200, json=lambda: outer.status)

            def post(self, path, **kw):
                outer.posts.append((base, path))
                return types.SimpleNamespace(status_code=202, json=lambda: {})

        return Client()


def make(status=None, reconcile_interval=0, **core):
    # `reconcile_interval=0` by default - not the runner's real 60s default -
    # so every test here that reconciles gets `reconcile()`'s old "judge a
    # missing Job immediately" behaviour unless it opts into a longer window
    # on purpose (the age-gate tests below do exactly that).
    batch, corev1 = FakeBatch(), FakeCore(**core)
    http = FakeDriverHttp(status or {"turnActive": True, "pending": False, "lastError": None})
    seats = K8sSeats(
        K8sSettings.from_env(ENV), batch, corev1, http, reconcile_interval=reconcile_interval
    )
    return seats, batch, http


def test_start_creates_the_job_once() -> None:
    seats, batch, _ = make()
    first = seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    again = seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    assert first["mechanism"] == "kubernetes" and first["job"] == job_name("g1", "u1")
    assert again["job"] == first["job"] and list(batch.jobs) == [first["job"]]


def test_status_merges_pod_and_driver() -> None:
    seats, _, _ = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    status = seats.status("u1")
    assert (
        status["state"] == "thinking"
        and status["pod"] == "Running"
        and status["driver"]["turnActive"] is True
    )


def test_status_is_pending_before_the_pod_runs() -> None:
    seats, _, _ = make(phase="Pending", pod_ip=None)
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    assert seats.status("u1")["state"] == "pending"


def test_release_tells_the_driver_then_deletes_the_job() -> None:
    seats, batch, http = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    released = seats.release("u1")
    assert http.posts == [("http://10.0.0.5:8090", "/release")]
    assert batch.deleted == [job_name("g1", "u1")] and released["deleted"] is True
    assert seats.status("u1") is None


def test_reconcile_deletes_jobs_of_dead_games_and_flags_missing_jobs() -> None:
    seats, batch, _ = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    seats.start(
        uid="u2", game_id="g2", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    batch.jobs.pop(job_name("g2", "u2"))  # vanished behind our back
    report = seats.reconcile(live_game_ids={"g2"})
    assert batch.deleted == [job_name("g1", "u1")]
    assert report == {"deleted": [job_name("g1", "u1")], "missing": ["u2"]}
    assert seats.status("u2")["state"] == "error" and "missing" in seats.status("u2")["reason"]


# ---- reconcile: a just-created seat must never be pinned at `error` --------
#
# `start()` inserts a seat into `_seats` before `create_namespaced_job`
# returns - a reconcile pass landing in that narrow window used to see the
# seat but not yet its Job and flag a perfectly healthy seat as `error`
# forever (nothing else ever cleared it). `reconcile_interval` gives a
# just-created seat a grace period, and a later pass clears a stale `missing`
# verdict once the Job is seen again.


def test_reconcile_does_not_flag_a_seat_still_mid_create() -> None:
    seats, batch, _ = make(reconcile_interval=999)
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    batch.jobs.pop(job_name("g1", "u1"))  # a reconcile pass landing before create returned
    report = seats.reconcile(live_game_ids={"g1"})
    assert report == {"deleted": [], "missing": []}
    assert seats.status("u1")["state"] != "error"


def test_reconcile_clears_a_stale_missing_verdict_once_the_job_reappears() -> None:
    seats, batch, _ = make()  # reconcile_interval=0 - judges immediately
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    job = batch.jobs.pop(job_name("g1", "u1"))
    first = seats.reconcile(live_game_ids={"g1"})
    assert first["missing"] == ["u1"]
    assert seats.status("u1")["state"] == "error"
    batch.jobs[job_name("g1", "u1")] = job  # the Job is seen again
    second = seats.reconcile(live_game_ids={"g1"})
    assert second["missing"] == []
    assert seats.status("u1")["state"] != "error"


def test_reconcile_marks_a_ttl_reaped_completed_job_gone_not_error() -> None:
    seats, batch, _ = make(phase="Succeeded")
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    assert seats.status("u1")["state"] == "gone"  # observed once, so `last_pod_phase` is recorded
    batch.jobs.pop(job_name("g1", "u1"))  # ttlSecondsAfterFinished reaped it
    report = seats.reconcile(live_game_ids={"g1"})
    assert report["missing"] == []
    assert seats.status("u1")["state"] == "gone"


# ---- status: a terminal pod phase is decided before any driver call --------
#
# The pod's IP can be recycled onto an unrelated pod the instant the pod
# itself is gone, so asking "is the driver at this IP happy" is meaningless
# once the pod has already succeeded or failed - and must not even be tried.


def test_a_succeeded_pod_is_gone_even_when_the_driver_still_answers() -> None:
    seats, _, http = make(phase="Succeeded")
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    status = seats.status("u1")
    assert status["state"] == "gone" and status["driver"] is None
    assert http.gets == []  # the (possibly recycled) pod IP was never asked


# ---- status: a transient failure reading the pod is "pending", not a raise -


def test_status_survives_a_pod_lookup_that_raises() -> None:
    seats, _, _ = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )

    def boom(*a, **kw):
        raise RuntimeError("api server unavailable")

    seats.core.list_namespaced_pod = boom
    status = seats.status("u1")
    assert status["state"] == "pending"
    assert "api server unavailable" in status["reason"]


# ---- start: a create failure must not leave a seat behind for the next
# `start()` to trip on, and popping it must not raise on its own ------------
#
# `start()` re-raises whatever `create_namespaced_job` threw (other than 409
# AlreadyExists) - it is `ai_seat._dispatch_to_kubernetes` that turns this
# into a `fallback`, not `K8sSeats` itself. What `K8sSeats.start` alone must
# guarantee: its own optimistic `_seats[uid]` insert is always undone on a
# real failure (even if a concurrent `release()` already popped it - popping
# twice must not raise), so the uid is never stuck `already_started` for a
# Job that was never actually created.


def test_start_pops_the_seat_and_reraises_on_a_non_409_create_failure() -> None:
    seats, batch, _ = make()
    batch.raise_once = ApiException(status=500, reason="boom")

    with pytest.raises(ApiException):
        seats.start(
            uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
        )
    assert seats.status("u1") is None


def test_start_survives_a_release_racing_the_create_call() -> None:
    seats, batch, _ = make()

    def create_then_race_a_release(namespace, body):
        # A concurrent `release()` pops the seat before `start()`'s own
        # exception handler runs - simulated directly since the real race is
        # a matter of thread-scheduling luck, not something worth flaking a
        # test over. Popping an already-gone seat must not raise, and the
        # original create failure must still propagate.
        seats._seats.pop("u1", None)
        raise ApiException(status=500, reason="boom")

    batch.create_namespaced_job = create_then_race_a_release
    with pytest.raises(ApiException):
        seats.start(
            uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
        )
    assert seats.status("u1") is None


def test_a_failed_create_leaves_nothing_behind_for_a_retry_to_trip_on() -> None:
    seats, batch, _ = make()
    batch.raise_once = RuntimeError("transient apiserver hiccup")

    with pytest.raises(RuntimeError):
        seats.start(
            uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
        )
    # The failed attempt created nothing and left no seat behind...
    assert seats.status("u1") is None
    assert batch.creates == 0

    # ...so the retry is a genuine first attempt, not `already_started`
    # handing back stale status for a Job that was never created.
    status = seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    assert batch.creates == 1
    assert status["mechanism"] == "kubernetes"
    assert status["job"] == job_name("g1", "u1")
    assert job_name("g1", "u1") in batch.jobs


# ---- release: the delete really asks for Background propagation -----------


def test_release_deletes_with_background_propagation() -> None:
    seats, batch, _ = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    seats.release("u1")
    name, body = batch.delete_calls[-1]
    assert name == job_name("g1", "u1")
    assert body.propagation_policy == "Background"


# ---- run_watch: no busy-loop when the stream ends without raising ---------


def test_run_watch_sleeps_between_reconnects_when_the_stream_ends_cleanly(
    monkeypatch,
) -> None:
    seats, _, _ = make()

    class _FakeWatch:
        def stream(self, *a, **kw):
            return iter([])  # ends immediately, no exception

    monkeypatch.setattr(k8s_seats.k8s_watch, "Watch", _FakeWatch)

    stop = threading.Event()
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            stop.set()

    thread = threading.Thread(target=seats.run_watch, args=(stop,), kwargs={"sleep": fake_sleep})
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert sleeps[:2] == [1.0, 1.0]
