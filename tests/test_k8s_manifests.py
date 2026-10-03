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
    path = next(p for p in rule["http"]["paths"] if p["path"] == "/")
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
    path = next(p for p in rule["http"]["paths"] if p["path"] == "/")
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
        assert entry["valueFrom"]["secretKeyRef"]["name"] == "magic-trainer-webui-proxy", name
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


# ------------------------------------------------- a real identity reaches the api --
#
# The activation half of OIDC ownership. `oidc.py`/`game_access.py` have been
# in the image since phase 2b and were completely inert, because no bearer
# token ever reached this process: the proxy held the session and forwarded
# nothing signed, and `mtg-stack` carried no `OIDC_*` env, so
# `oidc.oidc_settings()` answered `None` and every game was created unowned.
# That is what made the lobby browser-local, and it is the reported defect
# ("matches that players are seeing are pinned to the browser instances, not
# username from keycloak").
#
# The route chosen is `--pass-authorization-header`: oauth2-proxy puts the
# realm-signed **ID token** on the upstream request as `Authorization:
# Bearer`, and `oidc.py` verifies that signature locally against the realm
# JWKS. No `X-Forwarded-*` header is ever trusted on its own authority, and
# the browser needs no change at all - see 55-oauth2-proxy.yaml's own comment
# for the evidence and the alternative that was rejected.

#: The realm terraform actually created (`infrastructure`'s keycloak root
#: module: `keycloak_realm.spellslinger`, client `magic-trainer-webui`).
ISSUER = "https://auth.szymonrichert.pl/realms/spellslinger"


def _api_env() -> dict:
    docs = rendered()
    deploy = by_name(docs, "Deployment", "mtg-stack")
    (game,) = deploy["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e for e in game["env"]}


def _proxy_env() -> dict:
    docs = rendered()
    deploy = by_name(docs, "Deployment", "mtg-oauth2-proxy")
    (main,) = [c for c in deploy["spec"]["template"]["spec"]["containers"] if c["name"] == "main"]
    return {e["name"]: e for e in main["env"]}


def test_the_proxy_forwards_a_signed_token_upstream_as_a_bearer() -> None:
    """Without this the api sees no identity at all and every game is created
    unowned - the whole reason ownership was dead code. Per oauth2-proxy
    v7.15.4's own docs this flag "pass[es] OIDC IDToken to upstream via
    Authorization Bearer header"; it is a *request* header on the proxied
    path, which is exactly what `oidc.current_user` reads."""
    env = _proxy_env()
    assert env["OAUTH2_PROXY_PASS_AUTHORIZATION_HEADER"]["value"] == "true"


def test_the_api_is_configured_to_verify_that_token_against_the_realm_jwks() -> None:
    """The binding rule from `docs/architecture.md`: "API services validate
    bearer JWTs locally against the realm's JWKS. They never trust
    `X-Forwarded-*` headers." A JWKS URL is what makes that possible, and
    without it `oidc_settings()` stays `None` and the forwarded token is
    refused rather than trusted."""
    env = _api_env()
    assert env["OIDC_ISSUER"]["value"] == ISSUER
    assert env["OIDC_JWKS_URL"]["value"] == f"{ISSUER}/protocol/openid-connect/certs"
    # The confidential client terraform created for this host. An ID token's
    # `aud` is the client it was issued for, so this is checkable.
    assert env["OIDC_AUDIENCE"]["value"] == "magic-trainer-webui"
    # Never a static JWKS in production: that is `oidc.py`'s offline/test
    # escape hatch, and pinning a key literal here would freeze it across a
    # realm key rotation.
    assert "OIDC_JWKS_STATIC" not in env


def test_the_two_halves_ship_together() -> None:
    """A proxy forwarding a bearer token to an api with no issuer configured
    is not merely inert - `oidc.current_user` refuses such a request with 401
    rather than treating it as anonymous (deliberately: an unprovable
    identity is not waved through). So enabling the forwarding without the
    verification config would 401 the entire play UI. The two must never be
    applied one without the other."""
    forwarding = _proxy_env().get("OAUTH2_PROXY_PASS_AUTHORIZATION_HEADER", {}).get("value")
    env = _api_env()
    configured = "OIDC_ISSUER" in env and ("OIDC_JWKS_URL" in env or "OIDC_JWKS_STATIC" in env)
    assert (forwarding == "true") == configured, (
        "OAUTH2_PROXY_PASS_AUTHORIZATION_HEADER and the api's OIDC_* config "
        "are two halves of one change - shipping either alone 401s the UI"
    )


def test_no_oidc_secret_is_committed_as_a_literal() -> None:
    """`OIDC_ISSUER`/`OIDC_AUDIENCE`/`OIDC_JWKS_URL` are public identifiers -
    a URL and a client id - and are literals on purpose. Nothing that is
    actually a credential may join them: the client *secret* stays in the
    `magic-trainer-webui-proxy` Secret, read only by the proxy."""
    for name, entry in _api_env().items():
        if "SECRET" in name or "PASSWORD" in name:
            assert "value" not in entry, name
    assert "OIDC_CLIENT_SECRET" not in _api_env()


def test_the_api_publishes_a_guides_ref_defaulting_to_main() -> None:
    """The knob an evaluation harness turns, and the reason it is a knob and
    not a pin: every seat Job's `guides` initContainer checks out this ref,
    and `main` (latest guides) is what play wants. Only a harness that needs
    two runs to read the same corpus sets it to a fixed revision."""
    docs = rendered()
    deploy = by_name(docs, "Deployment", "mtg-stack")
    env = {e["name"]: e for e in deploy["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["GUIDES_REF"]["value"] == "main"


def test_the_api_publishes_the_scripts_kill_switch_defaulting_to_on() -> None:
    """`SCRIPTS_ENABLED` is the operator's kill switch for agent-authored
    changes to the writable guides checkout: flipping it to "false" on the
    Deployment turns the feature off without an image rebuild. It is
    published explicitly, with its default, so the switch is visible in the
    manifest someone has to edit to pull it."""
    docs = rendered()
    deploy = by_name(docs, "Deployment", "mtg-stack")
    env = {e["name"]: e for e in deploy["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["SCRIPTS_ENABLED"] == {"name": "SCRIPTS_ENABLED", "value": "true"}


def test_the_api_publishes_ops_token_from_the_mtg_ops_secret() -> None:
    """`records.authorized_ops_caller` fails closed when `OPS_TOKEN` is unset,
    which is exactly what a deployment nobody has configured this for looks
    like - unless the Deployment actually wires it up from a Secret. Without
    this, `/api/ops/dsl-coverage` refuses every caller in prod forever, no
    matter how the operator tries to configure it, because nothing publishes
    the env var in the first place. `optional: true` so a namespace that has
    not created `mtg-ops` yet still starts the Pod (the route just stays
    refused, per `authorized_ops_caller`'s own fail-closed default)."""
    docs = rendered()
    deploy = by_name(docs, "Deployment", "mtg-stack")
    env = {e["name"]: e for e in deploy["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["OPS_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "mtg-ops",
        "key": "OPS_TOKEN",
        "optional": True,
    }


# ------------------------------------------------- the login stays alive mid-game --
#
# Production, 2026-09-14: oauth2-proxy logged `expiry:168h0m0s refresh:disabled`
# and forwarded the Keycloak ID token (30-minute lifetime) until the api
# refused it with 401 `invalid bearer token: Signature has expired`, which
# bounced players to login about every 30 minutes. Cookie refresh redeems the
# refresh token before that, and the refreshed ID token is what gets forwarded.

_GO_DURATION_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def _go_duration_seconds(text: str) -> float:
    """A Go `time.Duration` string (`20m`, `168h0m0s`, `1h30m`) in seconds -
    the format oauth2-proxy parses `--cookie-refresh`/`--cookie-expire` with."""
    import re

    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", text)
    assert parts and "".join(n + u for n, u in parts) == text, f"not a Go duration: {text!r}"
    return sum(float(n) * _GO_DURATION_UNITS[u] for n, u in parts)


def test_go_duration_parser_reads_the_forms_oauth2_proxy_prints() -> None:
    assert _go_duration_seconds("20m") == 1200
    assert _go_duration_seconds("168h0m0s") == 168 * 3600
    assert _go_duration_seconds("1h30m") == 5400


def test_the_proxy_refreshes_the_session_before_the_forwarded_token_expires() -> None:
    env = _proxy_env()
    assert "OAUTH2_PROXY_COOKIE_REFRESH" in env, "refresh is disabled: tokens die after 30m"
    refresh = _go_duration_seconds(env["OAUTH2_PROXY_COOKIE_REFRESH"]["value"])
    assert 0 < refresh < 30 * 60, "must refresh before the 30-minute ID token expires"
    expire = _go_duration_seconds(env.get("OAUTH2_PROXY_COOKIE_EXPIRE", {}).get("value", "168h"))
    # oauth2-proxy v7.15.4 pkg/validation/cookie.go refuses refresh >= expire.
    assert refresh < expire


def test_cookie_refresh_is_not_combined_with_a_minimal_session_cookie() -> None:
    # pkg/validation/sessions.go: "cookie_refresh > 0 requires oauth tokens in
    # sessions. session_cookie_minimal cannot be set" - a minimal cookie drops
    # the refresh token, and the proxy would refuse to start.
    env = _proxy_env()
    assert "OAUTH2_PROXY_COOKIE_REFRESH" in env
    minimal = env.get("OAUTH2_PROXY_SESSION_COOKIE_MINIMAL", {}).get("value", "false")
    assert minimal.lower() != "true"


def test_seat_job_carries_no_dead_git_committer_vars() -> None:
    """FD6 (5), fb3-parity.md item 10: `gitwork.identity` overrides the
    COMMITTER from `MTG_ROLE`, so `GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL`
    in the agent container's env never took effect - dead config, dropped
    from the manifest (and from `wrapper.oneshot.PASSED_THROUGH`, which
    would otherwise still forward it to the review one-shot for nothing)."""
    text = (ROOT / "deploy/seat-job.yaml").read_text()
    assert "GIT_COMMITTER_NAME" not in text
    assert "GIT_COMMITTER_EMAIL" not in text


def test_the_api_caps_glibc_malloc_arenas() -> None:
    """OM1: a threadpool of sync routes gives glibc one malloc arena per
    thread, and each keeps its freed high-water mark as RSS - the prod OOM's
    amplifier. `MALLOC_ARENA_MAX=2` caps that retention."""
    env = _api_env()
    assert env["MALLOC_ARENA_MAX"] == {"name": "MALLOC_ARENA_MAX", "value": "2"}


def test_the_api_requests_its_realistic_steady_state_and_keeps_the_1gi_limit() -> None:
    """OM1: a live game sits at 350-400 MB, so the request reserves 512Mi;
    the limit stays 1Gi - the review OOM was fixed in code, not by scaling."""
    docs = rendered()
    deploy = by_name(docs, "Deployment", "mtg-stack")
    (game,) = deploy["spec"]["template"]["spec"]["containers"]
    assert game["resources"]["requests"]["memory"] == "512Mi"
    assert game["resources"]["limits"]["memory"] == "1Gi"


def _api_deployment() -> dict:
    return by_name(rendered(), "Deployment", "mtg-stack")


def test_the_api_seats_default_to_sonnet_and_the_buddy_stays_opus() -> None:
    env = _api_env()
    assert env["AGENT_DEFAULT_MODEL"]["value"] == "claude-sonnet-5"
    assert env["ADVISOR_MODEL"]["value"] == "claude-opus-5"


def test_the_api_publishes_the_seat_pair_env_the_seat_jobs_are_rendered_from() -> None:
    env = _api_env()
    assert env["SEAT_AGENT_CHANNEL"]["value"] == "teams"
    assert env["SEAT_SUBAGENT_MODEL"]["value"] == "claude-opus-5"
    assert env["SEAT_SUBAGENT_EFFORT"]["value"] == ""


def test_the_game_pod_is_annotated_for_prometheus_scraping() -> None:
    annotations = _api_deployment()["spec"]["template"]["metadata"]["annotations"]
    assert annotations["prometheus.io/scrape"] == "true"
    assert annotations["prometheus.io/port"] == "8000"
    assert annotations["prometheus.io/path"] == "/metrics"


def test_the_public_ingresses_do_not_route_metrics_to_the_api() -> None:
    """GET /metrics has no auth in the cluster and oauth2-proxy only covers
    the external ingress, so /metrics must never reach the API through an
    Ingress. `/metrics` is routed to a Service with no endpoints instead
    (nginx answers 503); no auth-url/auth-signin annotation is involved."""
    docs = rendered()
    deny = by_name(docs, "Service", "mtg-metrics-deny")
    assert deny["spec"]["selector"] == {"app": "mtg-metrics-deny"}
    assert not any(
        d["spec"]["template"]["metadata"]["labels"].get("app") == "mtg-metrics-deny"
        for d in by_kind(docs, "Deployment")
    )
    for ingress in by_kind(docs, "Ingress"):
        (rule,) = ingress["spec"]["rules"]
        paths = {p["path"]: p for p in rule["http"]["paths"]}
        assert paths["/metrics"]["backend"]["service"]["name"] == "mtg-metrics-deny", ingress[
            "metadata"
        ]["name"]


# ---- `mise run k8s:render` also renders a seat Job and a buddy Job --------


def _render_jobs() -> list[dict]:
    from scripts.render_seat_jobs import render_jobs

    return render_jobs({"AGENT_NAMESPACE": "mtg-engine", "MTG_STACK_INSTANCE_ID": "t"})


def _env(job: dict, container: str) -> dict:
    spec = job["spec"]["template"]["spec"]
    c = next(c for c in spec["containers"] if c["name"] == container)
    return {e["name"]: e.get("value") for e in c["env"]}


def test_k8s_render_task_calls_the_seat_job_renderer() -> None:
    mise = (ROOT / "mise.toml").read_text()
    block = mise.split('[tasks."k8s:render"]', 1)[1].split("[tasks.", 1)[0]
    assert "scripts/render_seat_jobs.py" in block


def test_render_jobs_yields_a_seat_job_and_a_buddy_job() -> None:
    seat, buddy = _render_jobs()
    assert seat["kind"] == buddy["kind"] == "Job"
    assert "role" not in "".join(seat["metadata"]["labels"])
    assert any(k.endswith("role") for k in buddy["metadata"]["labels"])


def test_rendered_seat_runs_sonnet_and_buddy_runs_opus() -> None:
    seat, buddy = _render_jobs()
    assert _env(seat, "agent")["MODEL"] == "claude-sonnet-5"
    assert _env(buddy, "agent")["MODEL"] == "claude-opus-5"
    assert _env(seat, "agent")["MTG_ROLE"] != _env(buddy, "agent")["MTG_ROLE"]


def test_rendered_seat_requires_baton_and_buddy_does_not() -> None:
    seat, buddy = _render_jobs()

    def init_env(job: dict) -> dict:
        spec = job["spec"]["template"]["spec"]
        c = next(c for c in spec["initContainers"] if c["name"] == "mcp-config")
        return {e["name"]: e.get("value") for e in c["env"]}

    assert init_env(seat)["MTG_REQUIRE_BATON"] == "1"
    assert init_env(buddy)["MTG_REQUIRE_BATON"] != "1"


def test_prod_seat_and_buddy_jobs_render_the_owner_speed_settings() -> None:
    from magic_cards_model_api.agent_settings import AgentSettings
    from magic_cards_model_api.k8s_seats import ROLE_ADVISOR, K8sSettings, build_job

    env = {name: e.get("value", "") for name, e in _api_env().items()}
    env.setdefault("AGENT_NAMESPACE", "mtg-engine")

    def agent_env(agent: AgentSettings, role: str) -> dict:
        job = build_job(
            K8sSettings.from_env(env),
            game_id="g1",
            uid="u1",
            player="bot",
            agent=agent,
            role=role,
        )
        (c,) = [c for c in job["spec"]["template"]["spec"]["containers"] if c["name"] == "agent"]
        return {e["name"]: e.get("value") for e in c["env"]}

    seat = agent_env(AgentSettings.default(env), "seat")
    assert seat["EFFORT"] == "low"
    assert seat["MAX_THINKING_TOKENS"] == "4000"
    buddy = agent_env(AgentSettings.advisor_default(env), ROLE_ADVISOR)
    assert buddy["EFFORT"] == "medium"
    assert not buddy.get("MAX_THINKING_TOKENS")
