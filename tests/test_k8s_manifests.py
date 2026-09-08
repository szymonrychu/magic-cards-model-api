from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def rendered() -> list[dict]:
    text = "".join(
        (p.read_text().replace("__TAG__", "t1") + "\n---\n")
        for p in sorted((ROOT / "deploy/k8s").glob("*.yaml"))
        if not p.name.endswith(".template.yaml")
    )
    return [doc for doc in yaml.safe_load_all(text) if doc]


def by_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def test_api_runs_alone_with_a_service_account_and_job_env() -> None:
    docs = rendered()
    (deploy,) = by_kind(docs, "Deployment")
    pod = deploy["spec"]["template"]["spec"]
    assert [c["name"] for c in pod["containers"]] == ["game"]
    assert pod["serviceAccountName"] == "mtg-api"
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["AI_SEAT_MODE"]["value"] == "kubernetes"
    assert env["AGENT_IMAGE"]["value"].endswith(":t1") and env["API_IMAGE"]["value"].endswith(":t1")
    # No downward-API POD_NAME/POD_UID/POD_NAMESPACE any more (phase 2b T9):
    # `ownerReferences` is gone, and with it the only thing that plumbing
    # was for - a durable-games lobby restart must not garbage-collect the
    # seat Jobs of games that survived it.
    assert "POD_NAME" not in env and "POD_UID" not in env and "POD_NAMESPACE" not in env
    assert env["AGENT_NAMESPACE"]["value"] == "mtg-engine"
    assert env["API_SERVICE_URL"]["value"]
    assert env["SPELLSLINGER_MCP_URL"]["value"]


def test_api_pod_satisfies_the_restricted_pod_security_standard() -> None:
    docs = rendered()
    (deploy,) = by_kind(docs, "Deployment")
    pod = deploy["spec"]["template"]["spec"]
    pod_security = pod["securityContext"]
    assert pod_security["runAsNonRoot"] is True
    assert pod_security["seccompProfile"]["type"] == "RuntimeDefault"
    (game,) = pod["containers"]
    container_security = game["securityContext"]
    assert container_security["allowPrivilegeEscalation"] is False
    assert container_security["capabilities"]["drop"] == ["ALL"]


def test_rbac_is_namespaced_and_minimal() -> None:
    docs = rendered()
    (role,) = by_kind(docs, "Role")
    rules = {(tuple(r["apiGroups"]), tuple(r["resources"])): set(r["verbs"]) for r in role["rules"]}
    assert rules[(("batch",), ("jobs",))] == {"create", "get", "list", "watch", "delete"}
    assert rules[(("",), ("pods",))] == {"get", "list", "watch"}
    assert rules[(("",), ("pods/log",))] == {"get"}
    assert not by_kind(docs, "ClusterRole")
    (binding,) = by_kind(docs, "RoleBinding")
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "mtg-api", "namespace": "mtg-engine"}
    ]


def test_memory_is_shared_and_the_service_exposes_only_the_api() -> None:
    docs = rendered()
    memory = next(
        d
        for d in by_kind(docs, "PersistentVolumeClaim")
        if d["metadata"]["name"] == "mtg-agent-memory"
    )
    assert memory["spec"]["accessModes"] == ["ReadWriteMany"]
    (service,) = by_kind(docs, "Service")
    assert [p["port"] for p in service["spec"]["ports"]] == [8000]


def test_network_policy_pins_agent_pods() -> None:
    docs = rendered()
    (policy,) = by_kind(docs, "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "mtg-agent"}
    assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    # Cross-file invariant: the only ingress source is the API's own Pods, so
    # this must match the Deployment's pod-template labels exactly, or the
    # policy silently stops admitting the API's traffic on a label rename.
    (deploy,) = by_kind(docs, "Deployment")
    deployment_pod_labels = deploy["spec"]["template"]["metadata"]["labels"]
    ingress_from_labels = policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
    assert ingress_from_labels == deployment_pod_labels
