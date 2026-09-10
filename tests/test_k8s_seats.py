from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import types
from pathlib import Path

import pytest
import yaml
from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException

from magic_cards_model_api import k8s_seats
from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.k8s_seats import (
    SEAT_JOB_PLACEHOLDERS,
    K8sSeats,
    K8sSettings,
    build_job,
    job_name,
    render_seat_job_template,
)
from mtg_agent.driver.revision import GUIDES_REVISION_FILE

ENV = {
    "AGENT_NAMESPACE": "mtg-engine",
    "API_SERVICE_URL": "http://mtg-stack.mtg-engine.svc.cluster.local:8000",
    "MCP_SERVICE_URL": "http://mtg-mcp.mtg-engine.svc.cluster.local:8080/mcp",
}

REPO_ROOT = Path(__file__).resolve().parents[1]

TEMPLATE_PATH = REPO_ROOT / "deploy" / "seat-job.yaml"

#: A leftover placeholder can survive even if it isn't one of the closed
#: set (a typo, a stray token) - this pattern is deliberately broader than
#: `SEAT_JOB_PLACEHOLDERS` so a test asserting "nothing survives" actually
#: catches that case instead of only checking the ones it already expects
#: to be gone.
_PLACEHOLDER_PATTERN = re.compile(r"__[A-Z_]+__")


def _leftover_placeholders(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for v in node.values():
            found.extend(_leftover_placeholders(v))
    elif isinstance(node, list):
        for v in node:
            found.extend(_leftover_placeholders(v))
    elif isinstance(node, str):
        found.extend(_PLACEHOLDER_PATTERN.findall(node))
    return found


def test_job_name_is_short_and_stable() -> None:
    assert job_name("0123456789abcdef", "fedcba9876543210") == "mtg-seat-01234567-fedcba98"


# ---- render_seat_job_template: pure substitution over parsed YAML ---------


def test_render_substitutes_only_the_closed_placeholder_set() -> None:
    template = {
        "a": "__GAME_ID__",
        "b": ["__SEAT_UID__", "unchanged", 3],
        "c": {"d": "__PLAYER__", "e": "__NOT_A_REAL_PLACEHOLDER__"},
    }
    values = dict.fromkeys(SEAT_JOB_PLACEHOLDERS, "")
    values.update({"__GAME_ID__": "g1", "__SEAT_UID__": "u1", "__PLAYER__": "bot"})
    rendered = render_seat_job_template(template, values)
    assert rendered == {
        "a": "g1",
        "b": ["u1", "unchanged", 3],
        "c": {"d": "bot", "e": "__NOT_A_REAL_PLACEHOLDER__"},
    }


def test_render_preserves_non_string_leaves() -> None:
    template = {"n": 3, "flag": False, "nothing": None}
    assert render_seat_job_template(template, {}) == template


# ---- build_job: render the real committed template -------------------------


def _build(env: dict[str, str] | None = None, **agent_kwargs) -> dict:
    kwargs = {"model": "sonnet", **agent_kwargs}
    return build_job(
        K8sSettings.from_env(env if env is not None else ENV),
        game_id="g1",
        uid="u1",
        player="bot",
        agent=AgentSettings(**kwargs),
    )


def test_committed_template_is_valid_yaml() -> None:
    # Guards the fixture itself: a template that doesn't even parse would
    # otherwise fail every other test here with a confusing traceback.
    text = TEMPLATE_PATH.read_text()
    parsed = yaml.safe_load(text)
    assert parsed["kind"] == "Job"


def test_no_placeholder_survives_rendering() -> None:
    job = _build()
    assert _leftover_placeholders(job) == []


def test_rendered_job_validates_as_a_batch_v1_job() -> None:
    job = _build()
    api_client = k8s_client.ApiClient()
    parsed = api_client._ApiClient__deserialize(job, k8s_client.V1Job)
    assert isinstance(parsed, k8s_client.V1Job)
    assert parsed.api_version == "batch/v1" and parsed.kind == "Job"
    assert parsed.metadata.name == job_name("g1", "u1")
    container_names = [c.name for c in parsed.spec.template.spec.containers]
    assert container_names == ["agent", "driver"]


def test_the_substituted_values_appear_where_expected() -> None:
    job = _build(model="opus", effort="max", max_thinking_tokens=4096)
    meta = job["metadata"]
    assert meta["name"] == job_name("g1", "u1")
    assert meta["namespace"] == "mtg-engine"
    assert meta["labels"] == {
        "app.kubernetes.io/name": "mtg-agent",
        "mtg/game-id": "g1",
        "mtg/seat-uid": "u1",
    }
    assert "ownerReferences" not in meta

    pod = job["spec"]["template"]["spec"]
    agent = next(c for c in pod["containers"] if c["name"] == "agent")
    agent_env = {e["name"]: e.get("value") for e in agent["env"]}
    assert agent_env["MODEL"] == "opus"
    assert agent_env["EFFORT"] == "max"
    assert agent_env["MAX_THINKING_TOKENS"] == "4096"

    driver = next(c for c in pod["containers"] if c["name"] == "driver")
    driver_env = {e["name"]: e.get("value") for e in driver["env"]}
    assert driver_env["GAME_API"] == "http://mtg-stack.mtg-engine.svc.cluster.local:8000"
    assert driver_env["GAME_ID"] == "g1"
    assert driver_env["SEAT_UID"] == "u1"
    assert driver_env["PLAYER"] == "bot"
    assignment = json.loads(driver_env["DRIVER_ASSIGNMENT_JSON"])
    assert assignment == {
        "uid": "u1",
        "game_id": "g1",
        "player": "bot",
        "api_base": "http://mtg-stack.mtg-engine.svc.cluster.local:8000",
        "agent": {"model": "opus", "effort": "max", "maxThinkingTokens": 4096},
    }

    mcp_container = next(c for c in pod["initContainers"] if c["name"] == "mcp-config")
    mcp_env = {e["name"]: e.get("value") for e in mcp_container["env"]}
    assert mcp_env["MCP_URL"] == "http://mtg-mcp.mtg-engine.svc.cluster.local:8080/mcp"
    assert "MCP_JSON" not in mcp_env


def test_max_thinking_tokens_absent_renders_as_empty_string() -> None:
    # `AgentSettings.env()` used to omit the env var entirely when unset;
    # the template's placeholder slot always exists now, so the closest
    # equivalent is an empty value - `wrapper/config.py`'s `_int_or_none`
    # already treats an empty `MAX_THINKING_TOKENS` the same as unset.
    job = _build(model="sonnet")
    agent = next(c for c in job["spec"]["template"]["spec"]["containers"] if c["name"] == "agent")
    agent_env = {e["name"]: e.get("value") for e in agent["env"]}
    assert agent_env["MAX_THINKING_TOKENS"] == ""


def test_namespace_is_set_in_code_from_settings_not_baked_in_the_template() -> None:
    job = build_job(
        K8sSettings.from_env({**ENV, "AGENT_NAMESPACE": "some-other-namespace"}),
        game_id="g1",
        uid="u1",
        player="bot",
        agent=AgentSettings(model="sonnet"),
    )
    assert job["metadata"]["namespace"] == "some-other-namespace"


def test_init_containers_present_in_order_with_security_context() -> None:
    job = _build()
    init_containers = job["spec"]["template"]["spec"]["initContainers"]
    names = [c["name"] for c in init_containers]
    assert names == ["guides", "mcp-config"]
    for container in init_containers:
        assert container["securityContext"]["allowPrivilegeEscalation"] is False


# ---- mcp-config initContainer: the T9 fix ----------------------------------
#
# Assertions on the rendered *value* alone (the string ended up in the right
# place) would not have caught the production bug this fixes - a rendered
# `MCP_URL=""` produced valid-looking YAML whose `mcp.json` was still wrong
# for a live agent. So `_run_mcp_config_script` actually executes the
# initContainer's own `/bin/sh -euc <script>` command with its own rendered
# env, exactly the way the real initContainer does, and reads back the JSON
# it wrote - the only way to prove the *emitted file*, not the template text,
# is correct.


def _run_mcp_config_script(job: dict, tmp_path: Path) -> dict:
    pod = job["spec"]["template"]["spec"]
    container = next(c for c in pod["initContainers"] if c["name"] == "mcp-config")
    command = container["command"]
    assert command[:2] == ["/bin/sh", "-euc"]
    script = command[2]
    env = {e["name"]: e.get("value") or "" for e in container["env"]}
    out = tmp_path / "mcp.json"
    env["MCP_JSON_OUT"] = str(out)
    subprocess.run(["/bin/sh", "-euc", script], env=env, check=True)
    return json.loads(out.read_text())


def test_mcp_config_env_carries_api_url_and_game_id_when_mcp_url_empty() -> None:
    job = _build({**ENV, "MCP_SERVICE_URL": ""})
    mcp_container = next(
        c for c in job["spec"]["template"]["spec"]["initContainers"] if c["name"] == "mcp-config"
    )
    mcp_env = {e["name"]: e.get("value") for e in mcp_container["env"]}
    assert mcp_env["MCP_URL"] == ""
    assert mcp_env["API_URL"] == ENV["API_SERVICE_URL"]
    assert mcp_env["GAME_ID"] == "g1"


def test_mcp_config_script_emits_stdio_mtg_entry_when_mcp_url_empty(tmp_path: Path) -> None:
    job = _build({**ENV, "MCP_SERVICE_URL": ""})
    rendered = _run_mcp_config_script(job, tmp_path)
    assert rendered["mcpServers"]["mtg"] == {
        "command": "/app/.venv/bin/python",
        "args": [
            "-m",
            "magic_cards_model_api.mcp",
            "--api",
            ENV["API_SERVICE_URL"],
            "--game",
            "g1",
            "--transport",
            "stdio",
        ],
        "alwaysLoad": True,
    }
    assert rendered["mcpServers"]["spellslinger"] == {
        "type": "http",
        "url": ("http://spellslinger-mcp-http.spellslinger-development.svc.cluster.local:8080/mcp"),
    }


def test_mcp_config_script_emits_http_mtg_entry_when_mcp_url_set(tmp_path: Path) -> None:
    job = _build(ENV)  # ENV carries a non-empty MCP_SERVICE_URL
    rendered = _run_mcp_config_script(job, tmp_path)
    assert rendered["mcpServers"]["mtg"] == {
        "type": "http",
        "url": ENV["MCP_SERVICE_URL"],
        "alwaysLoad": True,
    }
    assert "command" not in rendered["mcpServers"]["mtg"]
    assert rendered["mcpServers"]["spellslinger"]["type"] == "http"


def test_mcp_config_script_marks_mtg_always_loaded_in_the_stdio_branch(tmp_path: Path) -> None:
    """Claude Code defers MCP tools behind `ToolSearch` unless the server's
    own entry says otherwise, and the `mtg` server's tools are the seat's
    whole job - a seat that has to *search* for them spends its first
    inferences hunting instead of playing (13 `ToolSearch` calls and ~51s in
    one production pod before it gave up). `alwaysLoad` is the documented
    per-server opt-out (code.claude.com/docs/en/mcp, "Exempt a server from
    deferral": "set `alwaysLoad` to `true` in that server's configuration"),
    a plain sibling of `command`/`url` on the server entry. Note it is NOT
    the `anthropic/alwaysLoad` `_meta` key - that one is a *server*'s own
    per-tool marker in `tools/list` and does nothing in a config file, which
    is exactly the silently-wrong spelling this test pins against."""
    job = _build({**ENV, "MCP_SERVICE_URL": ""})
    rendered = _run_mcp_config_script(job, tmp_path)
    assert rendered["mcpServers"]["mtg"]["alwaysLoad"] is True


def test_mcp_config_script_marks_mtg_always_loaded_in_the_http_branch(tmp_path: Path) -> None:
    """The same, for the branch a non-empty `MCP_URL` takes. The script has
    two branches and only one of them is exercised by any given render, so
    both are asserted - a flag added to one and forgotten in the other fails
    silently in whichever deployment shape happens to take the other."""
    job = _build(ENV)  # ENV carries a non-empty MCP_SERVICE_URL
    rendered = _run_mcp_config_script(job, tmp_path)
    assert rendered["mcpServers"]["mtg"]["alwaysLoad"] is True


def test_compose_mcp_base_marks_mtg_always_loaded() -> None:
    """`deploy/mcp.base.json` is the docker-compose path's own base config,
    written by hand rather than by the initContainer script above - the same
    deferral applies to it, so it carries the same flag."""
    base = json.loads((REPO_ROOT / "deploy" / "mcp.base.json").read_text())
    assert base["mcpServers"]["mtg"]["alwaysLoad"] is True


def test_driver_assignment_json_validates_as_a_dispatch_in() -> None:
    """`build_job` writes the driver's own `DispatchIn` shape into
    `DRIVER_ASSIGNMENT_JSON` (the self-dispatch payload the driver's
    lifespan hook reads on startup, see `mtg_agent/driver/app.py`) - typed
    here directly against that model, rather than only asserting on
    substrings of the JSON, so a field renamed on either side of the
    `DispatchIn` API is caught."""
    from mtg_agent.driver.app import DispatchIn

    job = _build()
    driver = next(c for c in job["spec"]["template"]["spec"]["containers"] if c["name"] == "driver")
    denv = {e["name"]: e["value"] for e in driver["env"]}
    parsed = DispatchIn.model_validate_json(denv["DRIVER_ASSIGNMENT_JSON"])
    assert parsed.uid == "u1"
    assert parsed.game_id == "g1"
    assert parsed.player == "bot"
    assert parsed.agent == {"model": "sonnet", "effort": "medium", "maxThinkingTokens": None}


def test_job_is_plain_dicts_the_kubernetes_client_can_serialize() -> None:
    # `create_namespaced_job(namespace, body=dict)` hands this straight to
    # `ApiClient.sanitize_for_serialization`, which branches on
    # `isinstance(obj, dict)` - anything that fails that check (a
    # `types.MappingProxyType`, say) falls into the model-attribute branch and
    # raises `AttributeError: '...' object has no attribute 'openapi_types'`.
    job = _build()
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
    report = seats.reconcile(live_game_ids=lambda: {"g2"})
    assert batch.deleted == [job_name("g1", "u1")]
    assert report == {"deleted": [job_name("g1", "u1")], "missing": ["u2"]}
    assert seats.status("u2")["state"] == "error" and "missing" in seats.status("u2")["reason"]


# ---- reconcile: a failed live-set query is not the same as an empty one ----
#
# `reconcile` now performs the live-game query itself, via the callable it is
# handed, rather than trusting a pre-computed set built by the caller. That is
# what makes the guard below a property of `reconcile` itself, provable with a
# fake repository query, rather than something that only holds by accident of
# every current caller happening to wrap the query in its own try/except. A
# database blip that raises must never be read as "zero games are live" - the
# one distinction this whole task exists to enforce, because collapsing it
# deletes every seat Job in the namespace in a single pass.


def test_reconcile_refuses_to_delete_anything_if_the_live_set_query_failed() -> None:
    seats, batch, _ = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    seats.start(
        uid="u2", game_id="g2", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )

    def failing_query() -> set[str]:
        raise RuntimeError("postgres unreachable")

    report = seats.reconcile(live_game_ids=failing_query)

    assert batch.deleted == []
    assert report == {"deleted": [], "missing": []}
    assert set(batch.jobs) == {job_name("g1", "u1"), job_name("g2", "u2")}
    # Neither seat was touched by a query it never got an answer from.
    assert seats.status("u1")["state"] != "error"
    assert seats.status("u2")["state"] != "error"


def test_reconcile_deletes_every_job_when_the_query_genuinely_returns_empty() -> None:
    # The contrast case: a query that *succeeds* and genuinely reports no
    # live games is not a failure, and reconcile must still act on it -
    # otherwise the guard above could be satisfied by a `reconcile` that
    # simply never deletes anything, which is not the behaviour being tested.
    seats, batch, _ = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    seats.start(
        uid="u2", game_id="g2", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )

    report = seats.reconcile(live_game_ids=lambda: set())

    assert set(batch.deleted) == {job_name("g1", "u1"), job_name("g2", "u2")}
    assert batch.jobs == {}
    assert set(report["deleted"]) == {job_name("g1", "u1"), job_name("g2", "u2")}


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
    report = seats.reconcile(live_game_ids=lambda: {"g1"})
    assert report == {"deleted": [], "missing": []}
    assert seats.status("u1")["state"] != "error"


def test_reconcile_clears_a_stale_missing_verdict_once_the_job_reappears() -> None:
    seats, batch, _ = make()  # reconcile_interval=0 - judges immediately
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    job = batch.jobs.pop(job_name("g1", "u1"))
    first = seats.reconcile(live_game_ids=lambda: {"g1"})
    assert first["missing"] == ["u1"]
    assert seats.status("u1")["state"] == "error"
    batch.jobs[job_name("g1", "u1")] = job  # the Job is seen again
    second = seats.reconcile(live_game_ids=lambda: {"g1"})
    assert second["missing"] == []
    assert seats.status("u1")["state"] != "error"


def test_reconcile_marks_a_ttl_reaped_completed_job_gone_not_error() -> None:
    seats, batch, _ = make(phase="Succeeded")
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    assert seats.status("u1")["state"] == "gone"  # observed once, so `last_pod_phase` is recorded
    batch.jobs.pop(job_name("g1", "u1"))  # ttlSecondsAfterFinished reaped it
    report = seats.reconcile(live_game_ids=lambda: {"g1"})
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


# ---- status: an unreachable driver in a `Running` pod becomes `error` once
# past the grace period, or immediately if the driver container terminated --


def test_status_is_pending_when_the_driver_is_unreachable_within_the_grace_period() -> None:
    seats, _, _ = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )

    def unreachable(base):
        raise ConnectionError("no route to host")

    seats.http = unreachable
    status = seats.status("u1")
    assert status["state"] == "pending"


def test_status_is_error_when_the_driver_stays_unreachable_past_the_grace_period() -> None:
    seats, _, _ = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )

    def unreachable(base):
        raise ConnectionError("no route to host")

    seats.http = unreachable
    with seats._lock:
        seats._seats["u1"]["created_at"] -= 1000  # older than driver_grace_seconds (60s default)
    status = seats.status("u1")
    assert status["state"] == "error" and status["reason"] == "driver unreachable"


def test_status_is_error_immediately_when_the_driver_container_has_terminated() -> None:
    seats, _, _ = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    pod = types.SimpleNamespace(
        status=types.SimpleNamespace(
            pod_ip="10.0.0.5",
            phase="Running",
            container_statuses=[
                types.SimpleNamespace(
                    name="driver",
                    state=types.SimpleNamespace(
                        waiting=None,
                        terminated=types.SimpleNamespace(reason="Error"),
                    ),
                )
            ],
        ),
        metadata=types.SimpleNamespace(name="p1"),
    )
    seats.core.list_namespaced_pod = lambda *a, **kw: types.SimpleNamespace(items=[pod])
    status = seats.status("u1")
    # Caught from the pod's own container_statuses, not from a timed-out HTTP
    # call to a driver that will never answer - so this must not need the
    # grace period to elapse at all.
    assert status["state"] == "error" and status["reason"] == "Error"


# ---- release: any transport error, not only ApiException, must not 500 ----


def test_release_reports_deleted_false_on_a_transport_error_deleting_the_job() -> None:
    seats, batch, _ = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )

    def raise_transport_error(*a, **kw):
        raise RuntimeError("connection reset by peer")

    batch.delete_namespaced_job = raise_transport_error
    released = seats.release("u1")
    assert released["deleted"] is False
    assert "connection reset by peer" in released["reason"]


# ---- get(): the module-level singleton is built at most once, even racing,
# and an initial reconcile failure must not leave it with no background
# threads --------------------------------------------------------------------


def test_get_builds_only_one_instance_under_concurrent_callers(monkeypatch) -> None:
    k8s_seats.set_for_tests(None)
    starts: list[object] = []

    def recording_start_background(self, *a, **kw):
        starts.append(self)
        return threading.Event()

    monkeypatch.setattr(k8s_seats.K8sSeats, "start_background", recording_start_background)

    def slow_incluster_config():
        # Slow enough that a second `get()` call, arriving while the first is
        # still mid-build, reliably lands inside this window rather than
        # racing to start after the first has already finished - the whole
        # point of the test being a second thread that must be made to wait
        # on `_lock` rather than build its own `K8sSeats`.
        time.sleep(0.2)

    monkeypatch.setattr(k8s_seats.k8s_config, "load_incluster_config", slow_incluster_config)
    monkeypatch.setattr(k8s_seats.k8s_client, "BatchV1Api", FakeBatch)
    monkeypatch.setattr(k8s_seats.k8s_client, "CoreV1Api", FakeCore)

    results: list[object] = []

    def call() -> None:
        results.append(k8s_seats.get())

    try:
        t1 = threading.Thread(target=call)
        t2 = threading.Thread(target=call)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert len(results) == 2
        assert results[0] is results[1]
        assert len(starts) == 1
    finally:
        k8s_seats.set_for_tests(None)


def test_get_starts_background_threads_before_a_failing_initial_reconcile(monkeypatch) -> None:
    k8s_seats.set_for_tests(None)
    starts: list[object] = []

    def recording_start_background(self, *a, **kw):
        starts.append(self)
        return threading.Event()

    monkeypatch.setattr(k8s_seats.K8sSeats, "start_background", recording_start_background)
    monkeypatch.setattr(k8s_seats.k8s_config, "load_incluster_config", lambda: None)

    class RaisingOnceBatch(FakeBatch):
        def list_namespaced_job(self, namespace, label_selector=None, **kw):
            if not getattr(self, "_raised", False):
                self._raised = True
                raise RuntimeError("apiserver hiccup")
            return super().list_namespaced_job(namespace, label_selector=label_selector, **kw)

    monkeypatch.setattr(k8s_seats.k8s_client, "BatchV1Api", RaisingOnceBatch)
    monkeypatch.setattr(k8s_seats.k8s_client, "CoreV1Api", FakeCore)

    try:
        seats = k8s_seats.get()
        assert seats is not None
        assert len(starts) == 1  # started even though the initial reconcile raised
    finally:
        k8s_seats.set_for_tests(None)


def test_the_seat_job_images_come_from_settings_not_the_template() -> None:
    """The committed template must not carry a baked image tag.

    T9 generated `deploy/seat-job.yaml` from `build_job`'s output while the
    test environment was in force, so the fixture tag `t1` was baked into
    every `image:` line, and the images were left out of
    `SEAT_JOB_PLACEHOLDERS`. `k8s_seats` then stopped reading `AGENT_IMAGE`
    and `API_IMAGE` at all, while `deploy/k8s/50-deployment.yaml` went on
    publishing both with the real `__TAG__`. Every seat Job in the cluster
    asked for `...:t1`, which does not exist:

        Init:ErrImagePull
        harbor.szymonrichert.pl/containers/magic-cards-model-agent:t1

    T9's own "diff old output against new" check could not catch it: both
    sides were rendered under the same test env, so both said `t1`.
    """
    template = TEMPLATE_PATH.read_text()
    assert ":t1" not in template, "a test fixture image tag is baked into the committed template"

    job = build_job(
        K8sSettings.from_env(
            {**ENV, "AGENT_IMAGE": "reg.example/agent:v9", "API_IMAGE": "reg.example/api:v9"}
        ),
        game_id="g1",
        uid="u1",
        player="bot",
        agent=AgentSettings(model="sonnet"),
    )
    pod = job["spec"]["template"]["spec"]
    images = [c["image"] for c in pod["initContainers"]] + [c["image"] for c in pod["containers"]]
    assert set(images) == {"reg.example/agent:v9", "reg.example/api:v9"}, images


# ---- the guides corpus revision (P3) ---------------------------------------
#
# The k8s guides initContainer used to `git clone --depth 1` whatever `main`
# happened to be, with no pin and no record of what it got - two seats
# scheduled minutes apart could read different guides and nothing said so.
# It now takes an optional `GUIDES_REF` (default `main`) and stamps the
# revision it checked out into the guides volume, where the driver reads it.
# The default stays `main` on purpose: play wants current guides, only an
# evaluation harness wants a fixed ref.


def _guides_init_container(job: dict) -> dict:
    return next(
        c for c in job["spec"]["template"]["spec"]["initContainers"] if c["name"] == "guides"
    )


def test_every_placeholder_in_the_committed_template_is_in_the_closed_set() -> None:
    """`SEAT_JOB_PLACEHOLDERS` completeness, both directions: a token in the
    template that nobody substitutes reaches the cluster verbatim, and a
    name in the set that no longer appears in the template is a stale entry
    that stops meaning anything."""
    template = yaml.safe_load(TEMPLATE_PATH.read_text())
    in_template = set(_leftover_placeholders(template))
    assert in_template == set(SEAT_JOB_PLACEHOLDERS)


def test_guides_ref_is_a_placeholder_and_defaults_to_main() -> None:
    assert "__GUIDES_REF__" in SEAT_JOB_PLACEHOLDERS
    assert K8sSettings.from_env({}).guides_ref == "main"
    assert K8sSettings.from_env({**ENV, "GUIDES_REF": "  "}).guides_ref == "main"
    assert K8sSettings.from_env({**ENV, "GUIDES_REF": "v2"}).guides_ref == "v2"


def test_the_guides_init_container_carries_the_ref_it_was_configured_with() -> None:
    default_env = {e["name"]: e.get("value") for e in _guides_init_container(_build())["env"]}
    assert default_env["GUIDES_REF"] == "main"

    pinned = _build({**ENV, "GUIDES_REF": "0f0f0f0f"})
    pinned_env = {e["name"]: e.get("value") for e in _guides_init_container(pinned)["env"]}
    assert pinned_env["GUIDES_REF"] == "0f0f0f0f"


def _origin_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A real two-commit git repo standing in for github.com/szymonrychu/
    mtg-decks, reachable at the production clone URL.

    The initContainer's script is run verbatim - token URL, `remote set-url`
    scrub and all - and the only thing bent to fit a test is where that URL
    points: a `url.<local>.insteadOf` in a throwaway `$HOME/.gitconfig`
    rewrites the real https URL to this local repo. Overriding the URL with
    an env var of its own would have left the token handling, which is the
    part with a security consequence, untested.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(origin), *args], check=True, capture_output=True
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "test")
    (origin / "README.md").write_text("first\n")
    run("add", "-A")
    run("commit", "-qm", "first")
    (origin / "README.md").write_text("second\n")
    run("add", "-A")
    run("commit", "-qm", "second")
    run("branch", "pinned", "HEAD~1")

    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        '[url "file://'
        + str(origin)
        + '"]\n\tinsteadOf = https://x-access-token:test-token@github.com/szymonrychu/mtg-decks.git\n'
    )
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GH_PAT": "test-token",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return origin, env


def _rev(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run_guides_script(job: dict, tmp_path: Path, origin_env: dict[str, str]) -> Path:
    container = _guides_init_container(job)
    command = container["command"]
    assert command[:2] == ["/bin/sh", "-euc"]
    env = {e["name"]: e.get("value") or "" for e in container["env"]}
    env.update(origin_env)
    guides = tmp_path / "guides"
    guides.mkdir(exist_ok=True)
    env["GUIDES_DIR"] = str(guides)
    subprocess.run(["/bin/sh", "-euc", command[2]], env=env, check=True, capture_output=True)
    return guides


def test_the_guides_script_checks_out_main_by_default_and_stamps_its_revision(
    tmp_path: Path,
) -> None:
    origin, env = _origin_repo(tmp_path)
    guides = _run_guides_script(_build(), tmp_path, env)
    assert (guides / "README.md").read_text() == "second\n"
    assert (guides / ".guides-revision").read_text().strip() == _rev(origin, "main")


def test_the_guides_script_honours_an_explicit_guides_ref(tmp_path: Path) -> None:
    origin, env = _origin_repo(tmp_path)
    guides = _run_guides_script(_build({**ENV, "GUIDES_REF": "pinned"}), tmp_path, env)
    assert (guides / "README.md").read_text() == "first\n"
    assert (guides / ".guides-revision").read_text().strip() == _rev(origin, "pinned")


def test_the_guides_script_honours_a_bare_commit_sha_as_the_ref(tmp_path: Path) -> None:
    """A harness pins a sha, not only a branch or a tag - `fetch <ref>` takes
    all three, which `clone --branch` (what this replaced) did not."""
    origin, env = _origin_repo(tmp_path)
    sha = _rev(origin, "main~1")
    guides = _run_guides_script(_build({**ENV, "GUIDES_REF": sha}), tmp_path, env)
    assert (guides / ".guides-revision").read_text().strip() == sha


def test_the_guides_script_leaves_no_token_behind_in_the_checkout(tmp_path: Path) -> None:
    _, env = _origin_repo(tmp_path)
    guides = _run_guides_script(_build(), tmp_path, env)
    leaked = [
        path
        for path in guides.rglob("*")
        if path.is_file() and "x-access-token" in path.read_bytes().decode("utf-8", "replace")
    ]
    assert leaked == []


def test_the_driver_reads_the_revision_stamp_from_the_guides_volume() -> None:
    """The stamp is only useful if the driver container can open it: same
    volume, mounted at the same path the driver's `GUIDES_ROOT` names."""
    job = _build()
    pod = job["spec"]["template"]["spec"]
    driver = next(c for c in pod["containers"] if c["name"] == "driver")
    driver_env = {e["name"]: e.get("value") for e in driver["env"]}
    mounts = {m["name"]: m for m in driver["volumeMounts"]}
    assert driver_env["GUIDES_ROOT"] == mounts["guides"]["mountPath"] == "/guides"
    assert GUIDES_REVISION_FILE in _guides_init_container(job)["command"][2]
