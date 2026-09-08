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


def test_migration_job_secret_keys_match_deployment() -> None:
    # Cross-file invariant: the migration Job and the Deployment's `game`
    # container must read the exact same five POSTGRES_* keys from the exact
    # same Secret - a typo'd key name in one manifest would only surface at
    # Job-run time in production.
    docs = rendered()
    (deploy,) = by_kind(docs, "Deployment")
    game = deploy["spec"]["template"]["spec"]["containers"][0]
    (job,) = by_kind(docs, "Job")
    migrate = job["spec"]["template"]["spec"]["containers"][0]
    deploy_env = {e["name"]: e for e in game["env"]}
    job_env = {e["name"]: e for e in migrate["env"]}
    postgres_vars = [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    ]
    for name in postgres_vars:
        assert (
            job_env[name]["valueFrom"]["secretKeyRef"]
            == deploy_env[name]["valueFrom"]["secretKeyRef"]
        )


def test_migration_job_security_context_matches_deployment() -> None:
    # Cross-file invariant: the migration Job's pod- and container-level
    # securityContext must be identical to the Deployment's, since that
    # identity is what makes the Job admissible under the namespace's
    # restricted Pod Security Standard.
    docs = rendered()
    (deploy,) = by_kind(docs, "Deployment")
    pod = deploy["spec"]["template"]["spec"]
    (job,) = by_kind(docs, "Job")
    job_pod = job["spec"]["template"]["spec"]
    assert job_pod["securityContext"] == pod["securityContext"]
    (game,) = pod["containers"]
    (migrate,) = job_pod["containers"]
    assert migrate["securityContext"] == game["securityContext"]


def test_database_cluster_uses_rook_ceph_and_no_manual_credentials() -> None:
    docs = rendered()
    (cluster,) = by_kind(docs, "Cluster")
    spec = cluster["spec"]
    assert spec["storage"]["storageClass"] == "rook-ceph"
    assert spec["walStorage"]["storageClass"] == "rook-ceph"
    # CNPG must auto-generate the app Secret - never a hand-set credential.
    assert "superuserSecret" not in spec
    assert spec.get("enableSuperuserAccess", False) is False
    assert "password" not in str(spec).lower()
