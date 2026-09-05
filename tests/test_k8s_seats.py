from __future__ import annotations

import json
import types

from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException

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

    def create_namespaced_job(self, namespace, body):
        name = body["metadata"]["name"]
        if name in self.jobs:
            raise ApiException(status=409, reason="AlreadyExists")
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
        self.status, self.posts = status, []

    def __call__(self, base):
        outer = self

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, path, **kw):
                return types.SimpleNamespace(status_code=200, json=lambda: outer.status)

            def post(self, path, **kw):
                outer.posts.append((base, path))
                return types.SimpleNamespace(status_code=202, json=lambda: {})

        return Client()


def make(status=None, **core):
    batch, corev1 = FakeBatch(), FakeCore(**core)
    http = FakeDriverHttp(status or {"turnActive": True, "pending": False, "lastError": None})
    return K8sSeats(K8sSettings.from_env(ENV), batch, corev1, http), batch, http


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
