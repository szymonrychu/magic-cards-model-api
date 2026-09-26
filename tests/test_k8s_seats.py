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
    assert parsed.agent == {"model": "sonnet", "effort": "high", "maxThinkingTokens": None}


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


def test_status_is_pending_during_the_driver_self_dispatch_boot_window() -> None:
    """The driver's own lifespan sets `current["pending"] = True` the moment
    self-dispatch is scheduled, before `assign` has recorded anything else -
    so a `Running` pod whose driver answers `/status` with exactly that
    shape (`pending: True`, `turnActive: False`, `lastError: None`) must
    read as `pending` here, the same "alive, not finished" state a booting
    pod gets before the driver is even reachable. Reading it as `idle`
    instead is indistinguishable from a seat that finished and gave up, and
    would hand a perfectly healthy, still-booting seat to the pregame
    backstop."""
    seats, _, _ = make(status={"turnActive": False, "pending": True, "lastError": None})
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    status = seats.status("u1")
    assert status["state"] == "pending"
    assert status["pod"] == "Running"
    assert status["driver"]["pending"] is True


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


# ---- the guides corpus: a writable checkout of main (A13) ------------------
#
# The k8s guides initContainer used to fetch `GUIDES_REF` shallow and detached,
# read-only - fine for reading, useless for an agent that has to commit a guide
# change and push it. It now runs `deploy/guides-clone.sh`, baked
# into the agent image, which fetches `main` partially (`--filter=blob:none`,
# full history, no blobs up front), checks out a local `main` tracking
# `origin/main`, strips the token back out of the remote URL, and stamps the
# revision it checked out into the guides volume, where the driver reads it.
# Writable mode needs a branch to commit on, so the ref must be `main`.

CLONE_SCRIPT_PATH = REPO_ROOT / "deploy" / "guides-clone.sh"
DOCKERFILE_AGENT_PATH = REPO_ROOT / "Dockerfile.agent"
#: Where Dockerfile.agent puts the script in the image, and where the
#: initContainer runs it from.
CLONE_SCRIPT_IN_IMAGE = "/usr/local/bin/guides-clone.sh"
TOKEN_DIR = "/var/run/mtg-decks-git"
TOKEN_FILE = f"{TOKEN_DIR}/GH_PAT"


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


def test_the_guides_init_container_runs_the_clone_script_baked_into_the_agent_image() -> None:
    """The script lives in one file, `deploy/guides-clone.sh`, rather than
    inline in the template - so the test below can run exactly what the
    image runs, and Dockerfile.agent is what puts it there."""
    container = _guides_init_container(_build())
    assert container["command"] == ["/bin/sh", CLONE_SCRIPT_IN_IMAGE]
    assert container["image"] == K8sSettings.from_env(ENV).agent_image
    dockerfile = DOCKERFILE_AGENT_PATH.read_text()
    assert re.search(
        r"^COPY\b.*\bmagic-cards-model-api/deploy/guides-clone\.sh\s+"
        + re.escape(CLONE_SCRIPT_IN_IMAGE)
        + r"\s*$",
        dockerfile,
        re.MULTILINE,
    ), dockerfile
    assert CLONE_SCRIPT_PATH.is_file()


def _origin_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A real bare repo with two commits on `main`, standing in for
    github.com/szymonrychu/mtg-decks, reachable at the production clone URL.

    `deploy/guides-clone.sh` is run verbatim - token URL, `remote set-url`
    scrub and all - and the only thing bent to fit a test is where that URL
    points: a `url.<local>.insteadOf` in a throwaway `$HOME/.gitconfig`
    rewrites the real https URL to this local repo. Overriding the URL with
    an env var of its own would have left the token handling, which is the
    part with a security consequence, untested. `uploadpack.allowFilter` is
    what GitHub has on, and without it a `--filter` fetch is silently a full
    one, so a partial clone would go unproven.
    """
    work = tmp_path / "work"
    origin = tmp_path / "origin.git"
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(work), *args], check=True, capture_output=True
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "test")
    (work / "README.md").write_text("first\n")
    run("add", "-A")
    run("commit", "-qm", "first")
    (work / "README.md").write_text("second\n")
    run("add", "-A")
    run("commit", "-qm", "second")
    run("branch", "pinned", "HEAD~1")
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(work), str(origin)],
        check=True,
        capture_output=True,
    )
    for key in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):
        subprocess.run(["git", "-C", str(origin), "config", key, "true"], check=True)

    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        '[url "file://'
        + str(origin)
        + '"]\n\tinsteadOf = https://x-access-token:test-token@github.com/szymonrychu/mtg-decks.git\n'
    )
    token_file = tmp_path / "GH_PAT"
    token_file.write_text("test-token\n")
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "GH_PAT_FILE": str(token_file),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return origin, env


def _rev(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run_guides_script(job: dict, tmp_path: Path, origin_env: dict[str, str]) -> Path:
    """Run `deploy/guides-clone.sh` - the file the image bakes in - with the
    rendered initContainer's own env, the way the real initContainer does."""
    container = _guides_init_container(job)
    assert container["command"] == ["/bin/sh", CLONE_SCRIPT_IN_IMAGE]
    env = {e["name"]: e.get("value") or "" for e in container.get("env", [])}
    env.update(origin_env)
    guides = tmp_path / "guides"
    guides.mkdir(exist_ok=True)
    env["GUIDES_DIR"] = str(guides)
    subprocess.run(["/bin/sh", str(CLONE_SCRIPT_PATH)], env=env, check=True, capture_output=True)
    return guides


def test_the_guides_script_checks_out_a_writable_main_tracking_origin(
    tmp_path: Path,
) -> None:
    origin, env = _origin_repo(tmp_path)
    guides = _run_guides_script(_build(), tmp_path, env)

    assert (guides / "README.md").read_text() == "second\n"
    # A real local branch, not a detached HEAD: the agent commits on it.
    assert _git(guides, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(guides, "rev-parse", "--abbrev-ref", "main@{upstream}") == "origin/main"
    # Full history, so a rebase or a merge-base against origin/main works.
    assert _git(guides, "rev-parse", "--is-shallow-repository") == "false"
    assert _git(guides, "rev-list", "--count", "HEAD") == "2"
    # Partial: history now, blobs on demand.
    assert _git(guides, "config", "remote.origin.partialclonefilter") == "blob:none"
    # The token is gone from the remote and from the whole config.
    assert _git(guides, "remote", "get-url", "origin") == (
        "https://github.com/szymonrychu/mtg-decks.git"
    )
    assert "x-access-token" not in (guides / ".git" / "config").read_text()
    assert "test-token" not in (guides / ".git" / "config").read_text()
    # The stamp is written, and kept out of `git status` so no commit sweeps it up.
    assert (guides / ".guides-revision").read_text().strip() == _rev(origin, "main")
    assert _git(guides, "status", "--porcelain") == ""


def test_the_guides_script_leaves_no_token_behind_in_the_checkout(
    tmp_path: Path,
) -> None:
    _, env = _origin_repo(tmp_path)
    guides = _run_guides_script(_build(), tmp_path, env)
    leaked = [
        path
        for path in guides.rglob("*")
        if path.is_file()
        and any(secret in path.read_bytes() for secret in (b"x-access-token", b"test-token"))
    ]
    assert leaked == []


def test_the_guides_script_refuses_an_unreadable_token_file(tmp_path: Path) -> None:
    _, env = _origin_repo(tmp_path)
    env["GH_PAT_FILE"] = str(tmp_path / "absent")
    with pytest.raises(subprocess.CalledProcessError) as refused:
        _run_guides_script(_build(), tmp_path, env)
    assert b"GH_PAT" in refused.value.stderr, refused.value.stderr
    assert not (tmp_path / "guides" / ".guides-revision").exists()


def test_the_driver_reads_the_revision_stamp_from_the_guides_volume() -> None:
    """The stamp is only useful if the driver container can open it: same
    volume, mounted at the same path the driver's `GUIDES_ROOT` names."""
    job = _build()
    pod = job["spec"]["template"]["spec"]
    driver = next(c for c in pod["containers"] if c["name"] == "driver")
    driver_env = {e["name"]: e.get("value") for e in driver["env"]}
    mounts = {m["name"]: m for m in driver["volumeMounts"]}
    assert driver_env["GUIDES_ROOT"] == mounts["guides"]["mountPath"] == "/guides"
    assert GUIDES_REVISION_FILE in CLONE_SCRIPT_PATH.read_text()


# `GUIDES_REF` used to reach `git fetch` as a positional argument, and git reads
# a leading `--` as an option wherever it appears - `--upload-pack=<command>` is
# a git option that runs a command. Verified back then:
# GUIDES_REF='--upload-pack=touch /tmp/pwned;git-upload-pack' fetched
# successfully AND created the file. The ref is still validated against
# `[A-Za-z0-9._/-]+` before anything runs, and writable mode then requires it
# to be exactly `main` - a pinned tag or sha has no branch to commit on.


@pytest.mark.parametrize(
    "ref",
    [
        "--upload-pack=true",
        "--exec=true",
        "main;true",
        "main$(true)",
        "main ref",
        "-",
    ],
)
def test_the_guides_script_refuses_a_ref_that_is_not_a_ref(tmp_path: Path, ref: str) -> None:
    _, env = _origin_repo(tmp_path)

    with pytest.raises(subprocess.CalledProcessError) as refused:
        _run_guides_script(_build({**ENV, "GUIDES_REF": ref}), tmp_path, env)

    assert b"GUIDES_REF is not a ref" in refused.value.stderr, refused.value.stderr
    assert not (tmp_path / "guides" / ".guides-revision").exists()


@pytest.mark.parametrize("ref", ["pinned", "v1.2.3", "release/2024-05", "0f0f0f0f"])
def test_writable_mode_refuses_any_ref_but_main_and_says_so(tmp_path: Path, ref: str) -> None:
    _, env = _origin_repo(tmp_path)

    with pytest.raises(subprocess.CalledProcessError) as refused:
        _run_guides_script(_build({**ENV, "GUIDES_REF": ref}), tmp_path, env)

    stderr = refused.value.stderr.decode()
    assert "writable" in stderr and "main" in stderr and ref in stderr, stderr
    assert not (tmp_path / "guides" / ".guides-revision").exists()


def test_the_guides_script_does_not_ask_for_a_shallow_fetch() -> None:
    code = "\n".join(
        line
        for line in CLONE_SCRIPT_PATH.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "--filter=blob:none" in code
    assert "--depth" not in code and "--shallow" not in code


# ---- the pod: writable guides, the token as a file, identity, handover -----
#
# Both roles render from the one template, so every property below is pinned
# for a seat AND for an advisor (a buddy).

ROLES = ("seat", "advisor")


def _build_role(role: str) -> dict:
    return build_job(
        K8sSettings.from_env(ENV),
        game_id="g1",
        uid="u1" if role == "seat" else "a1",
        player="bot" if role == "seat" else "you",
        agent=AgentSettings(model="sonnet"),
        role=role,
    )


def _pod(job: dict) -> dict:
    return job["spec"]["template"]["spec"]


def _container(job: dict, name: str) -> dict:
    return next(c for c in _pod(job)["containers"] if c["name"] == name)


def _mounts(container: dict) -> dict[str, dict]:
    return {m["name"]: m for m in container.get("volumeMounts", [])}


def _token_volume(job: dict) -> dict:
    return next(v for v in _pod(job)["volumes"] if "secret" in v)


@pytest.mark.parametrize("role", ROLES)
def test_the_agent_mounts_guides_writable_and_the_driver_read_only(role: str) -> None:
    job = _build_role(role)
    agent_guides = _mounts(_container(job, "agent"))["guides"]
    assert agent_guides["mountPath"] == "/guides"
    assert agent_guides.get("readOnly", False) is False
    driver_guides = _mounts(_container(job, "driver"))["guides"]
    assert driver_guides["mountPath"] == "/guides"
    assert driver_guides["readOnly"] is True


@pytest.mark.parametrize("role", ROLES)
def test_the_token_is_a_file_mounted_into_the_agent_and_the_init_container_only(
    role: str,
) -> None:
    job = _build_role(role)
    pod = _pod(job)
    volume = _token_volume(job)
    assert volume["secret"]["secretName"] == "mtg-decks-git"
    assert volume["secret"]["items"] == [{"key": "GH_PAT", "path": "GH_PAT"}]
    assert volume["secret"]["defaultMode"] == 0o440
    # 0440 is group-readable only; fsGroup is what makes uid 1000 that group.
    assert pod["securityContext"]["fsGroup"] == 1000 == pod["securityContext"]["runAsUser"]

    def token_mounts(container: dict) -> list[dict]:
        return [m for m in container.get("volumeMounts", []) if m["name"] == volume["name"]]

    by_name = {c["name"]: c for c in pod["initContainers"] + pod["containers"]}
    mounted_into = {name for name, c in by_name.items() if token_mounts(c)}
    assert mounted_into == {"guides", "agent"}
    for name in ("guides", "agent"):
        (mount,) = token_mounts(by_name[name])
        assert mount["mountPath"] == TOKEN_DIR
        assert mount["readOnly"] is True


@pytest.mark.parametrize("role", ROLES)
def test_the_token_is_never_an_env_var_on_any_container(role: str) -> None:
    job = _build_role(role)
    pod = _pod(job)
    for container in pod["initContainers"] + pod["containers"]:
        for entry in container.get("env", []):
            assert entry["name"] != "GH_PAT", container["name"]
            ref = entry.get("valueFrom", {}).get("secretKeyRef")
            assert not ref or ref["name"] != "mtg-decks-git", container["name"]
        for source in container.get("envFrom", []):
            assert source.get("secretRef", {}).get("name") != "mtg-decks-git", container["name"]


@pytest.mark.parametrize("role", ROLES)
def test_the_agent_carries_its_role_and_game(role: str) -> None:
    job = _build_role(role)
    env = {e["name"]: e.get("value") for e in _container(job, "agent")["env"]}
    assert env["MTG_ROLE"] == role
    assert env["MTG_GAME_ID"] == "g1"


@pytest.mark.parametrize("role", ROLES)
def test_the_agent_carries_no_dead_git_committer_vars(role: str) -> None:
    """FD6 (5), fb3-parity.md item 10: `gitwork.identity` sets the COMMITTER
    from `MTG_ROLE` regardless of these two - dead config, dropped from the
    manifest and from `wrapper.oneshot.PASSED_THROUGH`."""
    job = _build_role(role)
    env = {e["name"]: e.get("value") for e in _container(job, "agent")["env"]}
    assert "GIT_COMMITTER_NAME" not in env
    assert "GIT_COMMITTER_EMAIL" not in env


@pytest.mark.parametrize("role", ROLES)
def test_the_agent_reads_the_drivers_evidence_directory(role: str) -> None:
    """D2b: the driver writes this game's evidence record under its own
    workspace (`/driver-workspace/evidence/<game>/<deck>.json`), and `plays
    admit` in the agent container picks it up from `MTG_EVIDENCE_DIR` - the
    same volume, mounted read-only there."""
    from mtg_agent.driver import handover

    job = _build_role(role)
    env = {e["name"]: e.get("value") for e in _container(job, "agent")["env"]}
    assert env["MTG_EVIDENCE_DIR"] == "/driver-workspace/evidence"
    driver_env = {e["name"]: e.get("value") for e in _container(job, "driver")["env"]}
    workspace = Path(driver_env["DRIVER_WORKSPACE"])
    assert handover.evidence_path(workspace, "g1", "dimir-frog") == Path(
        env["MTG_EVIDENCE_DIR"], "g1", "dimir-frog.json"
    )
    assert _mounts(_container(job, "agent"))["driver-workspace"]["mountPath"] == str(workspace)


def test_role_and_game_id_reach_the_agent_through_their_own_placeholders() -> None:
    assert "__MTG_ROLE__" in SEAT_JOB_PLACEHOLDERS
    assert "__MTG_GAME_ID__" in SEAT_JOB_PLACEHOLDERS
    template = yaml.safe_load(TEMPLATE_PATH.read_text())
    agent = next(
        c for c in template["spec"]["template"]["spec"]["containers"] if c["name"] == "agent"
    )
    env = {e["name"]: e.get("value") for e in agent["env"]}
    assert env["MTG_ROLE"] == "__MTG_ROLE__"
    assert env["MTG_GAME_ID"] == "__MTG_GAME_ID__"


@pytest.mark.parametrize("role", ROLES)
def test_the_pod_gives_both_containers_time_to_hand_over(role: str) -> None:
    """5520 s of grace, sized for a handover that develops scripts and guides
    through subagents (game 16a9cf8f ran out a 510 s budget doing exactly
    that) AND for `retire`'s join on the post-game AI review thread after it
    (DR3): the driver's preStop waits 5460 of it for the handover, the
    agent's waits up to 5480 for that marker, so both finish inside the
    grace."""
    job = _build_role(role)
    assert _pod(job)["terminationGracePeriodSeconds"] == 5520
    driver_stop = _container(job, "driver")["lifecycle"]["preStop"]["exec"]["command"]
    assert driver_stop == ["python", "-m", "mtg_agent.driver.handover", "--wait", "5460"]
    agent_stop = _container(job, "agent")["lifecycle"]["preStop"]["exec"]["command"]
    assert agent_stop == [
        "python",
        "-m",
        "mtg_agent.wrapper.handover_wait",
        "--wait",
        "5480",
    ]


@pytest.mark.parametrize("role", ROLES)
def test_the_handover_budget_nests_inside_the_prestops_and_the_grace(role: str) -> None:
    """The marker is written (budget) before the driver preStop's wait ends,
    that before the agent's hold, that before the grace - and the Job's
    `activeDeadlineSeconds` is far longer than the grace, so it never cuts a
    handover short."""
    from mtg_agent.driver import handover
    from mtg_agent.wrapper import handover_wait

    job = _build_role(role)
    grace = _pod(job)["terminationGracePeriodSeconds"]
    driver_wait = float(_container(job, "driver")["lifecycle"]["preStop"]["exec"]["command"][-1])
    agent_wait = float(_container(job, "agent")["lifecycle"]["preStop"]["exec"]["command"][-1])
    assert handover.NUDGE_AFTER_SECONDS < handover.HANDOVER_BUDGET_SECONDS < driver_wait
    assert driver_wait < agent_wait == handover_wait.DEFAULT_WAIT_SECONDS < grace
    assert job["spec"]["activeDeadlineSeconds"] > grace


@pytest.mark.parametrize("role", ROLES)
def test_the_agent_sees_the_driver_workspace_read_only(role: str) -> None:
    job = _build_role(role)
    agent_mount = _mounts(_container(job, "agent"))["driver-workspace"]
    assert agent_mount["mountPath"] == "/driver-workspace"
    assert agent_mount["readOnly"] is True
    driver_mount = _mounts(_container(job, "driver"))["driver-workspace"]
    assert driver_mount["mountPath"] == "/driver-workspace"
    assert driver_mount.get("readOnly", False) is False


@pytest.mark.parametrize("role", ROLES)
def test_the_role_job_still_validates_as_a_batch_v1_job(role: str) -> None:
    job = _build_role(role)
    assert _leftover_placeholders(job) == []
    parsed = k8s_client.ApiClient()._ApiClient__deserialize(job, k8s_client.V1Job)
    spec = parsed.spec.template.spec
    assert spec.termination_grace_period_seconds == 5520
    agent = next(c for c in spec.containers if c.name == "agent")
    assert agent.lifecycle.pre_stop._exec.command[2] == "mtg_agent.wrapper.handover_wait"


# ---- the buddy: an advisor Job, its own name, its own role label ----------
#
# A buddy (B5) is an advisor pod, not a seat: same template, same images,
# same driver sidecar, a different name and one extra label. The three
# properties below are what the cluster actually keys off, so each is pinned
# directly rather than inferred from "role was passed in":
#
#   - the Job name says which kind it is at a glance (`mtg-buddy-...`),
#   - `app.kubernetes.io/name` stays `mtg-agent`, so `deploy/k8s/
#     45-networkpolicy.yaml`'s podSelector still selects the pod - without
#     it the buddy can reach neither the api nor the Anthropic API,
#   - `mtg/seat-uid` carries the *advisor's* uid, which is unique, so `_pod`
#     lookup, `status()` and `release()` work unchanged.

NETWORKPOLICY_PATH = REPO_ROOT / "deploy" / "k8s" / "45-networkpolicy.yaml"


def _build_advisor(env: dict[str, str] | None = None, **agent_kwargs) -> dict:
    kwargs = {"model": "sonnet", **agent_kwargs}
    return build_job(
        K8sSettings.from_env(env if env is not None else ENV),
        game_id="g1",
        uid="a1",
        player="you",
        agent=AgentSettings(**kwargs),
        role="advisor",
    )


def test_job_name_for_an_advisor_is_the_buddy_name() -> None:
    assert job_name("0123456789abcdef", "fedcba9876543210", "advisor") == (
        "mtg-buddy-01234567-fedcba98"
    )
    # A seat keeps the name it has always had, with or without the argument -
    # `tests/test_k8s_seats.py` pins it, and README and NetworkPolicy prose
    # reference it.
    assert job_name("0123456789abcdef", "fedcba9876543210", "seat") == (
        "mtg-seat-01234567-fedcba98"
    )
    assert job_name("0123456789abcdef", "fedcba9876543210") == "mtg-seat-01234567-fedcba98"


def test_no_placeholder_survives_rendering_an_advisor_job() -> None:
    """The driver reads the role from `__ASSIGNMENT_JSON__`'s payload; the
    agent container gets its own copy through `__MTG_ROLE__`, which the
    advisor render fills like every other placeholder."""
    assert _leftover_placeholders(_build_advisor()) == []


def test_the_advisor_job_is_named_and_labelled_as_a_buddy() -> None:
    job = _build_advisor()
    meta = job["metadata"]
    assert meta["name"] == "mtg-buddy-g1-a1"
    assert meta["labels"] == {
        "app.kubernetes.io/name": "mtg-agent",
        "mtg/game-id": "g1",
        "mtg/seat-uid": "a1",
        "mtg/role": "advisor",
    }
    # The pod carries the same labels, which is what `_pod`'s selector and
    # the NetworkPolicy's podSelector actually read.
    assert job["spec"]["template"]["metadata"]["labels"] == meta["labels"]


def test_a_seat_job_gains_no_role_label() -> None:
    """Absence means seat: adding `mtg/role: seat` would change every
    existing Job's label set for no reader that wants it."""
    assert "mtg/role" not in _build()["metadata"]["labels"]


def test_the_advisor_pod_still_matches_the_agent_networkpolicy() -> None:
    policy = yaml.safe_load(NETWORKPOLICY_PATH.read_text())
    selector = policy["spec"]["podSelector"]["matchLabels"]
    labels = _build_advisor()["spec"]["template"]["metadata"]["labels"]
    assert selector, "the NetworkPolicy selects by label - this test is meaningless without one"
    assert all(labels.get(key) == value for key, value in selector.items())


def test_the_advisor_assignment_carries_the_role_for_the_driver() -> None:
    from mtg_agent.driver.app import DispatchIn

    job = _build_advisor()
    driver = next(c for c in job["spec"]["template"]["spec"]["containers"] if c["name"] == "driver")
    denv = {e["name"]: e["value"] for e in driver["env"]}
    parsed = DispatchIn.model_validate_json(denv["DRIVER_ASSIGNMENT_JSON"])
    assert parsed.role == "advisor"
    assert parsed.uid == "a1" and parsed.player == "you"
    # A seat's payload is byte-identical to what it always was - `role` is
    # written only when it is not the default, so a driver that predates
    # roles reads an unchanged body.
    seat_driver = next(
        c for c in _build()["spec"]["template"]["spec"]["containers"] if c["name"] == "driver"
    )
    seat_env = {e["name"]: e["value"] for e in seat_driver["env"]}
    assert "role" not in json.loads(seat_env["DRIVER_ASSIGNMENT_JSON"])


def test_start_creates_the_buddy_job_under_its_own_name() -> None:
    seats, batch, _ = make()
    started = seats.start(
        uid="a1", game_id="g1", player="you", agent=AgentSettings(model="sonnet"), role="advisor"
    )
    assert started["job"] == "mtg-buddy-g1-a1"
    assert started["role"] == "advisor"
    assert list(batch.jobs) == ["mtg-buddy-g1-a1"]
    assert seats.status("a1")["job"] == "mtg-buddy-g1-a1"


def test_reconcile_leaves_a_live_games_buddy_job_alone() -> None:
    """`reconcile` deletes by game id, which is right for a buddy too - but
    only if the buddy's Job is actually seen and its game read off the same
    label. Both halves are asserted, rather than assumed from the code."""
    seats, batch, _ = make()
    seats.start(uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet"))
    seats.start(
        uid="a1", game_id="g1", player="you", agent=AgentSettings(model="sonnet"), role="advisor"
    )
    report = seats.reconcile(live_game_ids=lambda: {"g1"})
    assert report == {"deleted": [], "missing": []}
    assert set(batch.jobs) == {job_name("g1", "u1"), "mtg-buddy-g1-a1"}
    assert seats.status("a1")["state"] != "error"


def test_reconcile_deletes_a_dead_games_buddy_job_with_its_seat() -> None:
    seats, batch, _ = make()
    seats.start(uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet"))
    seats.start(
        uid="a1", game_id="g1", player="you", agent=AgentSettings(model="sonnet"), role="advisor"
    )
    report = seats.reconcile(live_game_ids=lambda: set())
    assert set(report["deleted"]) == {job_name("g1", "u1"), "mtg-buddy-g1-a1"}
    assert batch.jobs == {}


def test_reconcile_flags_a_vanished_buddy_job_rather_than_orphaning_it() -> None:
    seats, batch, _ = make()
    seats.start(
        uid="a1", game_id="g1", player="you", agent=AgentSettings(model="sonnet"), role="advisor"
    )
    batch.jobs.pop("mtg-buddy-g1-a1")  # vanished behind our back
    report = seats.reconcile(live_game_ids=lambda: {"g1"})
    assert report == {"deleted": [], "missing": ["a1"]}
    assert "missing" in seats.status("a1")["reason"]


def test_release_deletes_the_buddy_job_by_its_own_name() -> None:
    seats, batch, http = make()
    seats.start(
        uid="a1", game_id="g1", player="you", agent=AgentSettings(model="sonnet"), role="advisor"
    )
    released = seats.release("a1")
    assert batch.deleted == ["mtg-buddy-g1-a1"] and released["deleted"] is True
    assert seats.status("a1") is None


# ---- an active turn is never "idle"; a stranded mid-game seat is not either --
#
# Live 2026-09-22 the play UI's pill read "AI (claude): idle" while the agent
# was mid-turn planning: `turnActive` only knows the turns the driver itself
# pushed, and every other shape fell through to "idle".


def _state_for(driver_status: dict) -> str:
    seats, _, _ = make(status=driver_status)
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    return seats.status("u1")["state"]


def test_status_is_thinking_when_the_wrapper_holds_a_turn_the_driver_did_not_push() -> None:
    assert (
        _state_for(
            {
                "turnActive": False,
                "agentBusy": True,
                "pending": False,
                "lastError": None,
                "gameId": "g1",
                "reflected": False,
            }
        )
        == "thinking"
    )


def test_status_is_stalled_when_nothing_plays_a_seat_whose_game_is_running() -> None:
    assert (
        _state_for(
            {
                "turnActive": False,
                "agentBusy": False,
                "pending": False,
                "lastError": None,
                "gameId": "g1",
                "reflected": False,
            }
        )
        == "stalled"
    )


def test_status_is_idle_only_for_a_driver_with_no_game_left_to_play() -> None:
    idle = {"turnActive": False, "agentBusy": False, "pending": False, "lastError": None}
    assert _state_for(idle | {"gameId": None, "reflected": False}) == "idle"
    assert _state_for(idle | {"gameId": "g1", "reflected": True}) == "idle"


def test_a_turn_boundary_never_flashes_stalled() -> None:
    """Between one turn ending and the driver's continuation, across its 5s
    unreachable-game retry, or a handover gap: nothing is playing for a
    moment. Inside `STALLED_GRACE_SECONDS` that is still a seat at work."""
    from magic_cards_model_api.k8s_seats import STALLED_GRACE_SECONDS

    between = {
        "turnActive": False,
        "agentBusy": False,
        "pending": False,
        "lastError": None,
        "gameId": "g1",
        "reflected": False,
    }
    assert _state_for(between | {"idleSeconds": 0.0}) == "thinking"
    assert _state_for(between | {"idleSeconds": STALLED_GRACE_SECONDS - 1}) == "thinking"
    assert _state_for(between | {"idleSeconds": STALLED_GRACE_SECONDS}) == "stalled"


def test_the_grace_outlasts_the_driver_s_unreachable_retry() -> None:
    from magic_cards_model_api.k8s_seats import STALLED_GRACE_SECONDS
    from mtg_agent.driver.app import UNREACHABLE_RETRY_SECONDS

    assert STALLED_GRACE_SECONDS > UNREACHABLE_RETRY_SECONDS


# ---- F1: the follow-up DSL-development Job -----------------------------------
#
# One bounded Opus turn that finishes a handover which ended unfinished. It
# runs as the ORIGINAL seat's (or advisor's) uid - the credential the game
# already bound to that player - in a Job of its own: `mtg-followup-*`, no
# `mtg/seat-uid` label (so the seat's own pod lookup never finds it), and its
# own `activeDeadlineSeconds`.

FOLLOWUP = {
    "deck": "dimir-frog",
    "decklist": "4 Psychic Frog\n",
    "opponent": "alice",
    "opponent_decklist": "4 Lightning Bolt\n",
    "opponent_identification": None,
    "outcome": "bot won",
    "run_table": "no script runs recorded",
    "offers": "no script offers recorded",
    "hand_played": "no lines played by hand",
    "candidates": {"plays": [], "lines": []},
    "unfinished_reasons": ["nudged: the handover was still running at 23 minutes"],
}


def _followup_job(role: str = k8s_seats.ROLE_SEAT) -> dict:
    return k8s_seats.build_followup_job(
        K8sSettings.from_env(ENV),
        game_id="g1",
        uid="u1",
        player="bot",
        role=role,
        agent=AgentSettings.review_default(),
        followup=FOLLOWUP,
    )


def test_the_deadline_is_a_placeholder_and_a_seat_keeps_14400() -> None:
    assert "__ACTIVE_DEADLINE_SECONDS__" in SEAT_JOB_PLACEHOLDERS
    template = yaml.safe_load(TEMPLATE_PATH.read_text())
    assert template["spec"]["activeDeadlineSeconds"] == "__ACTIVE_DEADLINE_SECONDS__"
    job = _build()
    assert job["spec"]["activeDeadlineSeconds"] == k8s_seats.SEAT_ACTIVE_DEADLINE_SECONDS == 14400


def test_the_followup_job_is_its_own_job_bounded_by_its_own_deadline() -> None:
    job = _followup_job()
    assert job["metadata"]["name"] == k8s_seats.followup_job_name("g1", "u1")
    assert job["metadata"]["name"] == "mtg-followup-g1-u1"
    labels = {
        "app.kubernetes.io/name": "mtg-agent",
        "mtg/game-id": "g1",
        k8s_seats.LABEL_FOLLOWUP: "u1",
    }
    assert job["metadata"]["labels"] == labels
    assert job["spec"]["template"]["metadata"]["labels"] == labels
    assert k8s_seats.LABEL_SEAT not in job["metadata"]["labels"]
    assert job["spec"]["activeDeadlineSeconds"] == k8s_seats.FOLLOWUP_ACTIVE_DEADLINE_SECONDS
    assert 3000 <= k8s_seats.FOLLOWUP_ACTIVE_DEADLINE_SECONDS <= 4000
    assert _leftover_placeholders(job) == []


def test_the_followup_job_runs_opus_as_the_original_seat() -> None:
    from mtg_agent.driver.app import DispatchIn

    job = _followup_job()
    agent_env = {e["name"]: e.get("value") for e in _container(job, "agent")["env"]}
    assert agent_env["MODEL"] == AgentSettings.review_default().model == "claude-opus-5"
    assert agent_env["MTG_ROLE"] == "seat"
    driver_env = {e["name"]: e.get("value") for e in _container(job, "driver")["env"]}
    assert driver_env["SEAT_UID"] == "u1"
    parsed = DispatchIn.model_validate_json(driver_env["DRIVER_ASSIGNMENT_JSON"])
    assert (parsed.uid, parsed.player, parsed.role, parsed.mode) == (
        "u1",
        "bot",
        "seat",
        "followup",
    )
    assert parsed.followup == FOLLOWUP
    assert parsed.agent == AgentSettings.review_default().to_wire()


def test_an_advisors_followup_carries_its_role() -> None:
    job = _followup_job(k8s_seats.ROLE_ADVISOR)
    assert job["metadata"]["labels"][k8s_seats.LABEL_ROLE] == "advisor"
    agent_env = {e["name"]: e.get("value") for e in _container(job, "agent")["env"]}
    assert agent_env["MTG_ROLE"] == "advisor"
    driver_env = {e["name"]: e.get("value") for e in _container(job, "driver")["env"]}
    assert json.loads(driver_env["DRIVER_ASSIGNMENT_JSON"])["role"] == "advisor"


def _start_followup(seats: K8sSeats) -> dict:
    return seats.start_followup(
        uid="u1",
        game_id="g1",
        player="bot",
        role=k8s_seats.ROLE_SEAT,
        agent=AgentSettings.review_default(),
        followup=FOLLOWUP,
    )


def test_a_followup_is_created_once_and_deduped_by_its_job_label() -> None:
    seats, batch, _ = make()
    first = _start_followup(seats)
    assert first["state"] == "scheduled" and first["job"] == "mtg-followup-g1-u1"
    assert first["mechanism"] == "kubernetes"
    # Another replica (a fresh K8sSeats over the same cluster) finds the Job
    # by its label and creates nothing.
    other = K8sSeats(K8sSettings.from_env(ENV), batch, FakeCore(), FakeDriverHttp({}))
    again = _start_followup(other)
    assert again["state"] == "exists" and again["job"] == first["job"]
    assert batch.creates == 1


def test_followup_running_reads_the_job_from_the_cluster() -> None:
    seats, batch, _ = make()
    assert seats.followup_running("u1") is False
    _start_followup(seats)
    assert seats.followup_running("u1") is True
    batch.jobs["mtg-followup-g1-u1"].status.succeeded = 1
    assert seats.followup_running("u1") is False


def test_releasing_the_seat_also_deletes_its_followup_job() -> None:
    seats, batch, _ = make()
    seats.start(uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet"))
    _start_followup(seats)
    released = seats.release("u1")
    assert "mtg-followup-g1-u1" in batch.deleted
    assert released["followup"]["deleted"] == ["mtg-followup-g1-u1"]
    assert seats.followup_running("u1") is False


def test_release_followup_alone_deletes_the_followup_job() -> None:
    seats, batch, _ = make()
    _start_followup(seats)
    assert seats.release_followup("u1") == {"deleted": ["mtg-followup-g1-u1"]}
    assert batch.jobs == {}


def test_reconcile_deletes_a_followup_job_once_its_game_is_gone() -> None:
    """Forget or End mid-run is the hard stop: the game leaves `store.ids()`,
    and reconcile deletes every Job labelled with it - the follow-up's too."""
    seats, batch, _ = make()
    _start_followup(seats)
    report = seats.reconcile(lambda: set())
    assert "mtg-followup-g1-u1" in report["deleted"]
    assert batch.jobs == {}
