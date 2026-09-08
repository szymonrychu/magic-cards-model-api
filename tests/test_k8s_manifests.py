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


def by_name(docs, kind, name):
    # Phase 2b oauth2-proxy: a second Deployment, Service and Ingress landed
    # alongside mtg-stack's own, so `(x,) = by_kind(docs, kind)` no longer
    # selects a single object - it raises "too many values to unpack". Select
    # by name instead, everywhere a test means one specific object.
    return next(d for d in by_kind(docs, kind) if d["metadata"]["name"] == name)


def test_api_runs_alone_with_a_service_account_and_job_env() -> None:
    docs = rendered()
    deploy = by_name(docs, "Deployment", "mtg-stack")
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
    deploy = by_name(docs, "Deployment", "mtg-stack")
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
    service = by_name(docs, "Service", "mtg-stack")
    assert [p["port"] for p in service["spec"]["ports"]] == [8000]


def test_network_policy_pins_agent_pods() -> None:
    docs = rendered()
    (policy,) = by_kind(docs, "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "mtg-agent"}
    assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    # Cross-file invariant: the only ingress source is the API's own Pods, so
    # this must match the Deployment's pod-template labels exactly, or the
    # policy silently stops admitting the API's traffic on a label rename.
    deploy = by_name(docs, "Deployment", "mtg-stack")
    deployment_pod_labels = deploy["spec"]["template"]["metadata"]["labels"]
    ingress_from_labels = policy["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
    assert ingress_from_labels == deployment_pod_labels


def test_migration_job_secret_keys_match_deployment() -> None:
    # Cross-file invariant: the migration Job and the Deployment's `game`
    # container must read the exact same five POSTGRES_* keys from the exact
    # same Secret - a typo'd key name in one manifest would only surface at
    # Job-run time in production.
    docs = rendered()
    # Phase 2b oauth2-proxy added a second Deployment (mtg-oauth2-proxy)
    # alongside mtg-stack's own, so `(deploy,) = by_kind(docs, "Deployment")`
    # no longer selects a single object - select by name instead.
    deploy = by_name(docs, "Deployment", "mtg-stack")
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
    # Same by-name reasoning as above: two Deployments exist post phase 2b.
    deploy = by_name(docs, "Deployment", "mtg-stack")
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


# ---------------------------------------------------------- phase 2b oauth2-proxy --
#
# The play UI host moves from nginx basic auth to oauth2-proxy in front of
# Keycloak; the escape-hatch host keeps basic auth so a Keycloak/client
# misconfiguration cannot lock everyone out at once. See 55-oauth2-proxy.yaml
# and 70-ingress.yaml for the manifests these tests are pinning.

NO_EXTERNAL_AUTH_ANNOTATIONS = {
    "nginx.ingress.kubernetes.io/auth-type",
    "nginx.ingress.kubernetes.io/auth-secret",
    "nginx.ingress.kubernetes.io/auth-realm",
    "nginx.ingress.kubernetes.io/auth-url",
    "nginx.ingress.kubernetes.io/auth-signin",
}


def test_the_play_ui_host_is_fronted_by_oauth2_proxy_not_basic_auth() -> None:
    docs = rendered()
    ingress = by_name(docs, "Ingress", "mtg-stack-sso")
    (rule,) = ingress["spec"]["rules"]
    assert rule["host"] == "play-magic.szymonrichert.pl"
    (path,) = rule["http"]["paths"]
    assert path["backend"]["service"]["name"] == "mtg-oauth2-proxy"
    assert path["backend"]["service"]["port"]["number"] == 80
    annotations = set(ingress["metadata"].get("annotations", {}))
    assert not annotations & NO_EXTERNAL_AUTH_ANNOTATIONS


def test_no_ingress_anywhere_carries_an_external_auth_annotation() -> None:
    # The workbench's binding convention: oauth2-proxy sits in the request
    # path as a reverse proxy, never as an nginx auth-url/auth-signin
    # annotation - that pattern turns a bearer-token request into a
    # cookie-login redirect and breaks every machine client. Pinned here so a
    # future Ingress cannot reintroduce it by copying an outside example.
    docs = rendered()
    for ingress in by_kind(docs, "Ingress"):
        for key in ingress["metadata"].get("annotations", {}):
            assert not key.endswith("auth-url"), ingress["metadata"]["name"]
            assert not key.endswith("auth-signin"), ingress["metadata"]["name"]


def test_the_escape_hatch_keeps_basic_auth_on_its_own_host() -> None:
    docs = rendered()
    ingress = by_name(docs, "Ingress", "mtg-stack")
    (rule,) = ingress["spec"]["rules"]
    assert rule["host"] == "pl.szymonrichert.pl"
    (path,) = rule["http"]["paths"]
    assert path["backend"]["service"]["name"] == "mtg-stack"
    assert path["backend"]["service"]["port"]["number"] == 8000
    annotations = ingress["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/auth-type"] == "basic"
    assert annotations["nginx.ingress.kubernetes.io/auth-secret"] == "play-magic-auth"


def test_oauth2_proxy_satisfies_the_restricted_pod_security_standard() -> None:
    # A server-side dry-run against the live cluster proved the estate's
    # oauth2-proxy admission-webhook sidecar is rejected by this namespace's
    # `restricted` policy for exactly the two fields asserted below - which
    # is why the proxy is a first-class Deployment here rather than an
    # injected sidecar.
    docs = rendered()
    deploy = by_name(docs, "Deployment", "mtg-oauth2-proxy")
    pod = deploy["spec"]["template"]["spec"]
    pod_security = pod["securityContext"]
    assert pod_security["runAsNonRoot"] is True
    assert pod_security["seccompProfile"]["type"] == "RuntimeDefault"
    (main,) = [c for c in pod["containers"] if c["name"] == "main"]
    container_security = main["securityContext"]
    assert container_security["allowPrivilegeEscalation"] is False
    assert container_security["capabilities"]["drop"] == ["ALL"]


def test_oauth2_proxy_takes_every_credential_from_a_secret_ref() -> None:
    docs = rendered()
    deploy = by_name(docs, "Deployment", "mtg-oauth2-proxy")
    (main,) = [c for c in deploy["spec"]["template"]["spec"]["containers"] if c["name"] == "main"]
    env = {e["name"]: e for e in main["env"]}
    secret_backed = [
        "OAUTH2_PROXY_CLIENT_ID",
        "OAUTH2_PROXY_CLIENT_SECRET",
        "OAUTH2_PROXY_COOKIE_SECRET",
        "OAUTH2_PROXY_COOKIE_DOMAIN",
        "OAUTH2_PROXY_REDIRECT_URL",
        "OAUTH2_PROXY_ALLOWED_GROUPS",
    ]
    for name in secret_backed:
        entry = env[name]
        assert "value" not in entry, name
        assert entry["valueFrom"]["secretKeyRef"]["name"] == "mtg-play-proxy", name
    # Regression guard: no env var whose name contains SECRET may ever carry
    # a literal `value` - that is a committed secret.
    for entry in main["env"]:
        if "SECRET" in entry["name"]:
            assert "value" not in entry, entry["name"]


def test_oauth2_proxy_returns_401_on_the_api_routes_the_ui_polls() -> None:
    # --api-route=^/api/ is the whole lobby-resiliency mechanism: a browser
    # NAVIGATION to / gets a 302 to Keycloak, while the UI's ~1.2s XHR polls
    # against /api/* get a clean 401 the JavaScript can detect, instead of a
    # cross-origin 302 that `fetch` turns into an opaque "Failed to fetch".
    docs = rendered()
    deploy = by_name(docs, "Deployment", "mtg-oauth2-proxy")
    (main,) = [c for c in deploy["spec"]["template"]["spec"]["containers"] if c["name"] == "main"]
    assert "--api-route=^/api/" in main["args"]
    env = {e["name"]: e for e in main["env"]}
    assert (
        env["OAUTH2_PROXY_UPSTREAMS"]["value"]
        == "http://mtg-stack.mtg-engine.svc.cluster.local:8000"
    )


def test_the_in_cluster_api_service_is_unchanged_so_ai_seats_are_unaffected() -> None:
    # AI seat Jobs, the driver and the MCP server all reach the API at
    # http://mtg-stack.mtg-engine.svc.cluster.local:8000 (see k8s_seats.py),
    # never through the ingress, so nothing at the edge may change this
    # Service. This is exactly why the estate's sidecar-injector approach was
    # rejected for this host: it hijacks the Service port and would 302
    # in-cluster callers to Keycloak.
    docs = rendered()
    service = by_name(docs, "Service", "mtg-stack")
    assert [p["port"] for p in service["spec"]["ports"]] == [8000]
    assert service["spec"]["selector"] == {"app": "mtg-stack"}
