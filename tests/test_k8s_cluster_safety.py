"""No process reaches a cluster it was not deployed into, and no stack reaps
another stack's seat Jobs.

2026-09-27 incident: a test set `AI_SEAT_MODE=kubernetes` without stubbing
`k8s_seats.get`; `get()` fell back to `load_kube_config()`, whose current
context on the owner's laptop was prod, then reconciled - deleting every
`mtg-agent` Job whose game was not in the laptop process's own store, which
killed a live game's AI seat.
"""

from __future__ import annotations

import threading
import types

import pytest
from kubernetes import config as k8s_config

from magic_cards_model_api import k8s_seats
from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.k8s_seats import (
    APP_LABEL_VALUE,
    LABEL_APP,
    LABEL_GAME,
    LABEL_INSTANCE,
    K8sSeats,
    K8sSettings,
    build_followup_job,
    build_job,
    build_review_job,
    job_name,
)
from tests.test_k8s_manifests import by_name, rendered
from tests.test_k8s_seats import ENV, FakeBatch, FakeCore, FakeDriverHttp

AGENT = AgentSettings(model="sonnet", effort="high")


def _config_fails(*a, **kw):
    raise k8s_config.ConfigException("no in-cluster service account")


@pytest.fixture
def unbuilt(monkeypatch: pytest.MonkeyPatch):
    """`get()` with no override installed, and the fakes wired in where a
    real client would be built - so a test can see which config loader
    `get()` reached for without anything ever leaving the process."""
    k8s_seats.set_for_tests(None)
    monkeypatch.setattr(K8sSeats, "start_background", lambda self, *a, **kw: threading.Event())
    monkeypatch.setattr(k8s_seats.k8s_client, "BatchV1Api", FakeBatch)
    monkeypatch.setattr(k8s_seats.k8s_client, "CoreV1Api", FakeCore)
    loads: list[str] = []
    monkeypatch.setattr(k8s_seats.k8s_config, "load_incluster_config", _config_fails)
    monkeypatch.setattr(
        k8s_seats.k8s_config, "load_kube_config", lambda *a, **kw: loads.append("kubeconfig")
    )
    yield loads
    k8s_seats.set_for_tests(None)


# ---- 1. no kubeconfig fallback without an explicit opt-in -------------------


def test_get_never_falls_back_to_the_kubeconfig_without_the_opt_in(
    monkeypatch: pytest.MonkeyPatch, unbuilt: list[str]
) -> None:
    monkeypatch.delenv(k8s_seats.OUT_OF_CLUSTER_ENV, raising=False)
    with pytest.raises(k8s_seats.ClusterAccessRefused, match=k8s_seats.OUT_OF_CLUSTER_ENV):
        k8s_seats.get()
    assert unbuilt == []
    assert k8s_seats._seats is None


def test_get_uses_the_kubeconfig_only_when_explicitly_opted_in(
    monkeypatch: pytest.MonkeyPatch, unbuilt: list[str]
) -> None:
    monkeypatch.setenv(k8s_seats.OUT_OF_CLUSTER_ENV, "1")
    monkeypatch.setenv(k8s_seats.INSTANCE_ENV, "laptop-dev")
    seats = k8s_seats.get()
    assert unbuilt == ["kubeconfig"]
    assert seats.settings.instance_id == "laptop-dev"


def test_an_opt_in_other_than_1_is_not_an_opt_in(
    monkeypatch: pytest.MonkeyPatch, unbuilt: list[str]
) -> None:
    monkeypatch.setenv(k8s_seats.OUT_OF_CLUSTER_ENV, "yes")
    with pytest.raises(k8s_seats.ClusterAccessRefused):
        k8s_seats.get()
    assert unbuilt == []


def test_get_refuses_to_run_without_a_stack_instance_id(
    monkeypatch: pytest.MonkeyPatch, unbuilt: list[str]
) -> None:
    monkeypatch.setattr(k8s_seats.k8s_config, "load_incluster_config", lambda: None)
    monkeypatch.delenv(k8s_seats.INSTANCE_ENV, raising=False)
    with pytest.raises(k8s_seats.ClusterAccessRefused, match=k8s_seats.INSTANCE_ENV):
        k8s_seats.get()
    assert k8s_seats._seats is None


@pytest.mark.parametrize("bad", ["has space", "-leading", "trailing-", "x" * 64, "a/b"])
def test_an_instance_id_that_is_not_a_label_value_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match=k8s_seats.INSTANCE_ENV):
        K8sSettings.from_env({**ENV, k8s_seats.INSTANCE_ENV: bad})


# ---- 2. the test suite itself can never reach a cluster ----------------------


def test_the_suite_hands_out_a_fake_backend_even_in_kubernetes_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_SEAT_MODE", "kubernetes")
    seats = k8s_seats.get()
    assert not isinstance(seats.batch, k8s_seats.k8s_client.BatchV1Api)
    assert not isinstance(seats.core, k8s_seats.k8s_client.CoreV1Api)


def test_the_suite_blocks_every_kubernetes_config_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a test that clears the override AND opts in out-of-cluster cannot
    build a real client: the loaders themselves raise under the suite."""
    monkeypatch.setenv("AI_SEAT_MODE", "kubernetes")
    monkeypatch.setenv(k8s_seats.OUT_OF_CLUSTER_ENV, "1")
    for loader in ("load_kube_config", "load_incluster_config", "load_config"):
        with pytest.raises(Exception, match="test suite"):
            getattr(k8s_config, loader)()
    with pytest.raises(Exception, match="test suite"):
        k8s_config.new_client_from_config()
    k8s_seats.set_for_tests(None)
    try:
        with pytest.raises(Exception, match="test suite"):
            k8s_seats.get()
        assert k8s_seats._seats is None
    finally:
        k8s_seats.set_for_tests(None)


def test_the_suite_points_kubeconfig_nowhere() -> None:
    import os

    assert os.environ["KUBECONFIG"] == "/nonexistent"


# ---- 4. reconcile reaps only this stack's own Jobs ---------------------------


def _seats(instance: str = "stack-a") -> tuple[K8sSeats, FakeBatch]:
    batch = FakeBatch()
    settings = K8sSettings.from_env({**ENV, k8s_seats.INSTANCE_ENV: instance})
    return K8sSeats(settings, batch, FakeCore(), FakeDriverHttp({}), reconcile_interval=0), batch


def _foreign(batch: FakeBatch, name: str, game_id: str, instance: str | None) -> None:
    labels = {LABEL_APP: APP_LABEL_VALUE, LABEL_GAME: game_id}
    if instance is not None:
        labels[LABEL_INSTANCE] = instance
    batch.jobs[name] = types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, labels=labels),
        status=types.SimpleNamespace(active=1, succeeded=0, failed=0),
    )


def test_every_job_kind_is_labelled_with_the_owning_stack() -> None:
    settings = K8sSettings.from_env({**ENV, k8s_seats.INSTANCE_ENV: "stack-a"})
    jobs = [
        build_job(settings, game_id="g1", uid="u1", player="bot", agent=AGENT),
        build_job(settings, game_id="g1", uid="u2", player="bot", agent=AGENT, role="advisor"),
        build_followup_job(
            settings,
            game_id="g1",
            uid="u1",
            player="bot",
            role="seat",
            agent=AGENT,
            followup={"reasons": []},
        ),
        build_review_job(settings, game_id="g1", uid="u3", player="alice", agent=AGENT),
    ]
    for job in jobs:
        assert job["metadata"]["labels"][LABEL_INSTANCE] == "stack-a"
        assert job["spec"]["template"]["metadata"]["labels"][LABEL_INSTANCE] == "stack-a"


def test_reconcile_never_reaps_another_stacks_jobs_or_unlabelled_ones() -> None:
    seats, batch = _seats("stack-a")
    seats.start(uid="u1", game_id="g-dead", player="bot", agent=AGENT)
    _foreign(batch, "mtg-seat-other", "g-other", "stack-b")
    _foreign(batch, "mtg-seat-legacy", "g-legacy", None)

    report = seats.reconcile(live_game_ids=lambda: set())

    assert report["deleted"] == [job_name("g-dead", "u1")]
    assert set(batch.jobs) == {"mtg-seat-other", "mtg-seat-legacy"}


def test_reconcile_asks_the_cluster_only_for_this_stacks_jobs() -> None:
    seats, batch = _seats("stack-a")
    selectors: list[str] = []
    original = batch.list_namespaced_job

    def recording(namespace, label_selector=None, **kw):
        selectors.append(label_selector)
        return original(namespace, label_selector=label_selector, **kw)

    batch.list_namespaced_job = recording
    seats.reconcile(live_game_ids=lambda: set())
    assert selectors == [f"{LABEL_APP}={APP_LABEL_VALUE},{LABEL_INSTANCE}=stack-a"]


def test_a_stack_with_no_instance_id_reaps_nothing() -> None:
    batch = FakeBatch()
    seats = K8sSeats(K8sSettings.from_env(ENV | {k8s_seats.INSTANCE_ENV: ""}), batch, FakeCore())
    _foreign(batch, "mtg-seat-x", "g-dead", "")
    _foreign(batch, "mtg-seat-y", "g-dead", None)
    assert seats.reconcile(live_game_ids=lambda: set())["deleted"] == []
    assert set(batch.jobs) == {"mtg-seat-x", "mtg-seat-y"}


# ---- the deployed stack carries its instance id ------------------------------


def test_the_deployment_names_its_stack_instance() -> None:
    deploy = by_name(rendered(), "Deployment", "mtg-stack")
    (game,) = deploy["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e for e in game["env"]}
    value = env[k8s_seats.INSTANCE_ENV]["value"]
    assert K8sSettings.from_env({k8s_seats.INSTANCE_ENV: value}).instance_id == value
    assert k8s_seats.OUT_OF_CLUSTER_ENV not in env
