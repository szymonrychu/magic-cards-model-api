# The stack on Kubernetes - the API, plus one Job per AI seat

This used to be one Pod with three containers (game, agent, driver) mirroring
`docker-compose.yml`'s topology. It is not that any more. The API is now a
single-container Deployment, and it creates one Kubernetes Job per AI seat
itself (`AI_SEAT_MODE=kubernetes`, `src/magic_cards_model_api/k8s_seats.py`) -
each Job carries its own `agent` + `driver` containers, runs for the length of
one match, and is cleaned up by `ttlSecondsAfterFinished` when it is done.
Plain YAML, no Helm, no Kustomize, no operator.

| file | what it is |
|---|---|
| `00-namespace.yaml` | the `mtg-engine` namespace (NOT `mtg` - see below) |
| `10-secret.template.yaml` | templates for the Claude credential and the GitHub PAT - placeholders only, never real values |
| `20-database.yaml` | the `mtg-database` CloudNativePG `Cluster` - see [Database: CloudNativePG](#database-cloudnativepg) |
| `25-migration-job.yaml` | one-shot `alembic upgrade head` Job, run before the Deployment on every deploy - see [Migrations: a Job, waited on](#migrations-a-job-waited-on) |
| `30-storage.yaml` | PVCs for the agent's memory (now `ReadWriteMany` - see below) and the Scryfall cache |
| `40-rbac.yaml` | a `mtg-api` ServiceAccount, namespaced Role and RoleBinding letting the API create/watch/delete Jobs and read Pods |
| `45-networkpolicy.yaml` | pins every agent-seat Pod: reachable only from the API, allowed out only to the API, DNS, spellslinger, and HTTPS |
| `50-deployment.yaml` | the API Deployment - one replica, `Recreate` strategy, one `game` container: ports, env, probes, resources |
| `55-oauth2-proxy.yaml` | oauth2-proxy Deployment + Service, fronting the play UI host only - see [Login: oauth2-proxy and Keycloak](#login-oauth2-proxy-and-keycloak) |
| `60-service.yaml` | ClusterIP Service for port 8000 only |
| `70-ingress.yaml` | the `mtg-stack-sso` Ingress on `play-magic.szymonrichert.pl` (no auth annotations - routes to oauth2-proxy) and the `mtg-metrics-deny` Service - see [Login: oauth2-proxy and Keycloak](#login-oauth2-proxy-and-keycloak) |

There is no `20-configmap-mcp.yaml` any more. The old ConfigMap supplied a
loopback-pointed `mcp.base.json` to the one Pod's agent container; per-seat
Jobs have no shared Pod to loop back to, so each Job's `mcp-config`
initContainer writes its own `/etc/mtg/mcp.json` from a `--game`-scoped JSON
literal baked in by `k8s_seats.mcp_config` (see `env: MCP_JSON` on that
initContainer) - there is nothing left for a ConfigMap to hold.

## The three things you have to supply

Everything else applies as-is. These three do not exist until you create them,
and neither the API Pod nor any seat Job will run without the first two.

| what | where it goes | how |
|---|---|---|
| **Claude OAuth token** (`claude setup-token`) | Secret `mtg-agent`, key `CLAUDE_CODE_OAUTH_TOKEN`, namespace `mtg-engine` | [Create the Secrets](#create-the-secrets) |
| **GitHub PAT** for the deck guides | Secret `mtg-decks-git`, key `GH_PAT`, namespace `mtg-engine` | [Create the Secrets](#create-the-secrets) |
| **Harbor pull secret** | Secret `regcred`, namespace `mtg-engine` | [The registry pull secret](#the-registry-pull-secret) |

The full ordered sequence is under [Apply](#apply).

## Namespace: `mtg-engine`, not `mtg`

A namespace called `mtg` already exists in this cluster and belongs to
something else. These manifests deliberately use a new `mtg-engine` namespace
and never touch `mtg`.

## RBAC, and the Pod Security Standard

The API creates a Job (and reads its Pods) for every AI seat it dispatches, so
it needs its own ServiceAccount and a namespaced Role - never a ClusterRole,
because creating Pods (which a Job does on your behalf) is a privilege
boundary worth keeping inside one namespace. `40-rbac.yaml` grants exactly:

- `batch/jobs`: `create`, `get`, `list`, `watch`, `delete`
- `pods`: `get`, `list`, `watch` (to find a seat Job's Pod IP and phase)
- `pods/log`: `get`

No `configmaps` rule: the Job's own `mcp-config` initContainer writes the MCP
config from an env var, so the API never touches a ConfigMap.

Label the namespace with the restricted Pod Security Standard once, so a bug
in a future Job template cannot ask for more than a seat Pod should ever need
(root, host access, extra capabilities - `k8s_seats.build_job` already sets
`runAsNonRoot`, drops all capabilities, and disallows privilege escalation on
every container, precisely so this label does not block it):

```sh
kubectl label namespace mtg-engine pod-security.kubernetes.io/enforce=restricted
```

## NetworkPolicy: agent Pods are pinned

`45-networkpolicy.yaml` selects every Pod labelled
`app.kubernetes.io/name: mtg-agent` (every seat Job's Pod carries this label -
see `k8s_seats.LABEL_APP`) and, both directions:

- **Ingress**: only from Pods labelled `app: mtg-stack` (the API Deployment) -
  the driver's `/status` and any control call the API makes land, nothing else
  does.
- **Egress**: DNS (`kube-system`, UDP/TCP 53 - CoreDNS is `kube-dns`,
  `10.96.0.10`, confirmed with `kubectl -n kube-system get svc kube-dns` on
  this cluster), the API on 8000, the `spellslinger-development` namespace on
  8080 (the spellslinger MCP Service), and HTTPS (443) to everywhere *except*
  the RFC1918 private ranges. The API and spellslinger are reached through
  their own selector-based rules above regardless of the ipBlock - those two
  bullets do not depend on the CIDR math below at all.

The egress `ipBlock`'s `except` list is the RFC1918 ranges
(`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), not this cluster's specific
pod/service CIDRs. Checked directly against this cluster with
`kubectl cluster-info dump | grep -m1 cluster-cidr` (pod CIDR `10.244.0.0/16`)
and `grep -m1 service-cluster-ip-range` (service CIDR `10.96.0.0/12`): both
are already subsets of `10.0.0.0/8`, so the RFC1918 list as committed already
excludes them - no cluster-specific edit was needed.

**What actually goes wrong if this is pointed at a cluster whose pod or
service CIDR falls *outside* the RFC1918 ranges** (a cluster using
`100.64.0.0/10` for pods - CGNAT space, not RFC1918 - say): the `except` list
would *not* cover that range, so the 443-only HTTPS rule would admit egress to
every other Pod and Service IP in the cluster on port 443, not just the
Anthropic API - a broader hole, not a block. (Getting this backwards - "the
agent can no longer reach the API or spellslinger" - is wrong: those two are
reached through the earlier `podSelector`/`namespaceSelector` rules, which
that rule cannot affect either way.) If your cluster's ranges are outside
10.0.0.0/8, `172.16.0.0/12` and `192.168.0.0/16`, add them to the `except`
list so this policy keeps agent egress scoped to the Anthropic API and
nothing else on the cluster network.

The `except` list also carries `169.254.0.0/16` (link-local, unrouted -
notably where a cloud provider's instance-metadata endpoint lives, e.g.
`169.254.169.254`), so an agent's outbound HTTPS cannot reach the node's own
metadata service even though it is not part of the cluster's Pod/Service
address space and RFC1918 does not cover it either.

## Images: Harbor, and the `__TAG__` placeholder

The API image, and the two images every seat Job pulls, come from Harbor:

```
harbor.szymonrichert.pl/containers/magic-cards-model-api:__TAG__     # game (this Deployment), driver (per-seat Job)
harbor.szymonrichert.pl/containers/magic-cards-model-agent:__TAG__   # agent, guides init (per-seat Job)
```

**`__TAG__` is a literal placeholder, not a tag that exists.** The intended way
to deploy is:

```sh
mise run k8s:deploy 1.2.3        # or: MTG_VERSION=1.2.3 mise run k8s:deploy
```

The version is the X.Y.Z that CI (the `images` job on main) pushed to Harbor and
tagged `vX.Y.Z` in git; anything not matching `^[0-9]+\.[0-9]+\.[0-9]+$` is
refused, and `k8s:deploy` no longer builds or pushes. It substitutes `__TAG__`
with that version and pipes the result to
`kubectl apply -f -`. Nothing rendered is written back into the repo. The
Deployment sets `AGENT_IMAGE` and `API_IMAGE` env vars to the same
`harbor.szymonrichert.pl/.../*:__TAG__` strings, substituted by the exact same
`sed`, so one substitution pins both the API's own image and the image every
Job it creates will pull - no separate `__AGENT_IMAGE__`/`__API_IMAGE__`
placeholder was needed.

If you are not using mise, do the same thing by hand with the released version:

```sh
TAG=1.2.3
sed "s/__TAG__/${TAG}/g" deploy/k8s/*.yaml | kubectl apply -f -
```

`imagePullPolicy: IfNotPresent` throughout: a released X.Y.Z tag is immutable, so
one tag always means one image and `Always` would only add a registry round
trip per container start. If you ever push a mutable tag (`latest`), change the
policy with it.

### The registry pull secret

Harbor is private, so the API Pod (and every seat Job's Pod) carries
`imagePullSecrets: [{name: regcred}]`. `imagePullSecrets` are namespaced, and
the new namespace starts without one. Every other namespace in this cluster
already has a `regcred` docker-registry Secret for `harbor.szymonrichert.pl`
(robot account `robot$kubernetes`). The easy path is to copy one:

```sh
kubectl -n default get secret regcred -o yaml \
  | sed 's/namespace: default/namespace: mtg-engine/' \
  | kubectl apply -f -
```

That carries stale `resourceVersion`/`uid` fields; `kubectl apply` ignores them.
If you would rather build it from the robot credentials directly:

```sh
kubectl -n mtg-engine create secret docker-registry regcred \
  --docker-server=harbor.szymonrichert.pl \
  --docker-username='robot$kubernetes' \
  --docker-password="$HARBOR_ROBOT_TOKEN"
```

Without it the containers sit in `ImagePullBackOff` with
`unauthorized to access repository`.

## Database: CloudNativePG

`20-database.yaml` is a hand-written `postgresql.cnpg.io/v1` `Cluster` named
`mtg-database`, applied by this repo's own `mise run k8s:deploy` - not a Helm
chart, not anything in the separate `infrastructure` estate repo. The CNPG
*operator* is already installed cluster-wide (namespace `postgres-operator`)
and needs nothing extra to serve a new namespace; this file only asks it for
one database.

`spec.instances: 1`, a 5Gi data volume and a separate 1Gi WAL volume (CNPG's
own convention - every other CNPG `Cluster` in this cluster does the same),
`bootstrap.initdb` creating database `mtg` owned by role `mtg`. No
`imageName` (rides the operator's default Postgres image), no `monitoring:`
block, no `backups:`/`.spec.backup` block - all three default off, correct
for a small single-instance database with no stated backup requirement.
`enableSuperuserAccess` is left at its default (`false`): nothing in this
repo ever needs more than the owner role below.

**StorageClass: `rook-ceph`, not the cluster's default `rook-ceph-rwx`.**
`rook-ceph` is RBD block storage (ReadWriteOnce) - the right shape for a
single-writer Postgres instance, and what every other CNPG `Cluster` in this
cluster pins explicitly. `rook-ceph-rwx` is CephFS (ReadWriteMany) - right
for `mtg-agent-memory` above, wrong here: an RWX volume for a database that
never shares its own storage is over-provisioned, not merely unnecessary.

CNPG auto-generates a role and a Secret from `bootstrap.initdb` - **no
password is written anywhere in this repo.** The Secret is named
`<cluster-name>-app`, i.e. **`mtg-database-app`**, and carries (among other,
unused, keys):

| key | value |
|---|---|
| `host` | the primary's in-cluster service DNS name |
| `port` | `5432` |
| `username` | `mtg` |
| `password` | generated |
| `dbname` | `mtg` |

`25-migration-job.yaml` and `50-deployment.yaml` both read these five keys
via `secretKeyRef` into `POSTGRES_HOST`/`POSTGRES_PORT`/`POSTGRES_USER`/
`POSTGRES_PASSWORD`/`POSTGRES_DB` - **not** a composed `DATABASE_URL`, even
though the Secret also carries ready-made `uri`/`fqdn-uri`/`jdbc-uri` keys:
none of those include the `postgresql+psycopg://` driver prefix (psycopg 3)
this app's SQLAlchemy engine needs (`db.database_url_from_env` assembles the
DSN itself from the five components). Both manifests' `env:` blocks are
shaped identically on purpose, so a reviewer can see at a glance they read
the same Secret the same way.

## Migrations: a Job, waited on

`25-migration-job.yaml` is a one-shot `batch/v1` `Job` running
`alembic upgrade head` against `mtg-database` - the exact command
`docker-compose.yml`'s already-working `migrate` service runs against the
same image. It is a Job, never an initContainer and never something `game`
runs at its own startup: **alembic takes no lock of its own**, so a
concurrent or premature `upgrade head` is a real corruption hazard, not a
hypothetical one - the same reasoning `docker-compose.yml`'s `migrate`
service comments carry.

The Job's name is `mtg-migrate-__TAG__` - the same `__TAG__` placeholder
every other manifest here carries, substituted the same way. A Job's spec is
mostly immutable, so re-applying an existing Job with a different image would
be rejected outright; naming it after the tag means a new deploy (new tag) is
always a fresh Job object, while re-applying an unchanged tag hits an
already-existing, already-`Complete` Job and is a no-op.
`ttlSecondsAfterFinished: 3600` cleans each one up an hour after it finishes.
`backoffLimit: 0` and `restartPolicy: Never`: a failed migration should stop
and be looked at, not silently retried. `activeDeadlineSeconds: 300` bounds
how long a hung migration can occupy the namespace.

`mise run k8s:deploy` applies this Job and **waits for it to finish before
applying the Deployment** - see [the phased `k8s:deploy` below](#apply). A
bare `kubectl apply -f deploy/k8s/` of the whole directory is **not**
equivalent to `mise run k8s:deploy` any more: it would apply everything,
including the Deployment, in one shot with no ordering guarantee between the
migration and the app rolling onto the new schema. Do the same thing by hand,
in order, if you are not using mise:

```sh
TAG=1.2.3   # a released X.Y.Z
NS=mtg-engine

# 1. everything except the migration Job, Deployment, Service, Ingress
for f in deploy/k8s/00-namespace.yaml deploy/k8s/20-database.yaml \
         deploy/k8s/30-storage.yaml deploy/k8s/40-rbac.yaml \
         deploy/k8s/45-networkpolicy.yaml; do
  sed "s/__TAG__/${TAG}/g" "$f"
done | kubectl apply -f -

# 2. wait for the database
kubectl wait --for=condition=Ready cluster/mtg-database -n "$NS" --timeout=300s

# 3. run the migration and wait for it to finish - do NOT proceed on failure
#    or timeout; see the mise task's own comment for the Complete/Failed/
#    timeout polling logic and why a plain `kubectl wait --for=condition=
#    complete` is not enough here
sed "s/__TAG__/${TAG}/g" deploy/k8s/25-migration-job.yaml | kubectl apply -f -
kubectl -n "$NS" logs "job/mtg-migrate-${TAG}" -f   # watch it, or poll .status.conditions

# 4. only once the migration Job is Complete: the app itself
for f in deploy/k8s/50-deployment.yaml deploy/k8s/60-service.yaml deploy/k8s/70-ingress.yaml; do
  sed "s/__TAG__/${TAG}/g" "$f"
done | kubectl apply -f -
```

## Create the Secrets

Do not put credentials in a file in the repo. Create both imperatively.

### The Claude credential - `CLAUDE_CODE_OAUTH_TOKEN` is the recommended one

```sh
kubectl -n mtg-engine create secret generic mtg-agent \
  --from-literal=CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN"
```

Get the value from `claude setup-token`. **It is not an API key.** The CLI
reads OAuth tokens and API keys from different variables, and a setup-token
placed in `ANTHROPIC_API_KEY` is sent as an API key and gets a 401 -
`docker-compose.yml` says the same thing. Every seat Job's `agent` container
pulls both keys from this same Secret via `envFrom` (`k8s_seats.build_job`),
so one Secret update is picked up by the next Job created, no restart needed.

If you would rather use a real `sk-ant-...` key:

```sh
kubectl -n mtg-engine create secret generic mtg-agent \
  --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
```

Set exactly one. Whichever key you did not create is simply absent from the
Secret; the wrapper prefers the token and clears the other so they cannot
shadow each other.

### The GitHub PAT for the deck guides

Every seat Job's `guides` initContainer makes a **writable** clone of the
private deck-guides repo, `szymonrychu/mtg-decks` (`deploy/guides-clone.sh`:
`main` fetched with `--filter=blob:none`, a local `main` tracking
`origin/main`), and the agent commits guide and play-script changes in it and
pushes them straight to `main`. So the token needs write access, to that one
repo only.

Create a **fine-grained PAT** (GitHub - Settings - Developer settings -
Fine-grained tokens) with:

| setting | value |
|---|---|
| Repository access | Only select repositories: `szymonrychu/mtg-decks` |
| Contents | Read and write |
| Metadata | Read-only (GitHub adds it automatically) |
| Workflows | **No access** - never grant it: it would let a pushed branch rewrite the repo's CI |
| everything else | No access |

A classic PAT is not acceptable any more: its `repo` scope is every repo the
account can reach.

```sh
kubectl -n mtg-engine create secret generic mtg-decks-git \
  --from-literal=GH_PAT="$GH_PAT"
```

**How it reaches the pod: a file, never an env var.** `deploy/seat-job.yaml`
mounts the Secret's `GH_PAT` key at `/var/run/mtg-decks-git/GH_PAT` (mode
`0440`, readable by uid 1000 through `fsGroup: 1000`) into the `guides`
initContainer and the `agent` container only; the `driver` gets no mount. The
initContainer puts the token in the remote URL just long enough to fetch, then
strips it and refuses to finish if `x-access-token` is still in `.git/config`.
The agent authenticates through a credential helper in `/agent-home/.gitconfig`
(written by `src/mtg_agent/wrapper/bootstrap.py`, scoped to
`https://github.com`) that reads the file each time git asks - the token is
never written into the gitconfig. Commits carry `GIT_COMMITTER_NAME=mtg-agent`
and `GIT_COMMITTER_EMAIL=mtg-agent@mtg-trainer.invalid`.

**Rotating the token.** Generate the new PAT, then replace the Secret in place:

```sh
kubectl -n mtg-engine create secret generic mtg-decks-git \
  --from-literal=GH_PAT="$GH_PAT" \
  --dry-run=client -o yaml | kubectl apply -f -
```

New seat Jobs use it immediately. Running pods see the new file once the
kubelet syncs the Secret volume (about a minute), and because the credential
helper re-reads the file on every push, nothing needs a restart. Revoke the
old PAT on GitHub afterwards.

**The kill switch.** `deploy/k8s/50-deployment.yaml` publishes
`SCRIPTS_ENABLED` (default `"true"`) to the API. To turn agent-authored
scripts and guide changes off without a rebuild:

```sh
kubectl -n mtg-engine set env deployment/mtg-stack SCRIPTS_ENABLED=false
```

That restarts the API with the switch off (`Recreate` strategy). Set it back
to `"true"` - or re-apply `50-deployment.yaml` - to turn it on again. The hard
stop, if an agent's pushes themselves are the problem, is revoking the PAT on
GitHub: every pod's next push fails at once, and the read-only play path keeps
working off the checkout it already has.

`10-secret.template.yaml` exists for people who want either declarative; copy it
to `10-secret.yaml` (gitignored) and fill in the placeholders.

### The chat simulation token - Secret `mtg-sim`

The `/sim/mcp` endpoint (chat sandbox tools, see the README section "Chat simulation MCP")
is guarded by its own bearer token, not `OPS_TOKEN`. `50-deployment.yaml` reads the Secret with
`optional: true`: without it the endpoint answers 503 (disabled). The agents need the same value
through an agents-vault entry (write-only: the value goes in once and is never shown again).

Generate the value once into a shell variable, hand it to the Secret and to the vault, then
drop it, without ever echoing it. The vault web API (`vault serve-web`, no bearer, source
allowlist; loopback is allowed by default) is reached through a port-forward to the
`agents-vault-web` Service in namespace `agents` (Service port 80, pod port 8000); the public
`/vault` host sits behind browser SSO and is not meant for scripts. Routes and bodies are the
agents-vault README "Web API": `POST /vault/v1/entries` with
`{name, description, labels, envs: [{env, protected, value}]}`, and
`PUT /vault/v1/entries/{entry_id}/value` with `{values: {ENV: value}}` (a protected value is at
least 8 characters).

First creation (Secret, then the vault entry; the returned `id` goes into the preset `env`
reference):

```sh
MTG_SIM_TOKEN="$(openssl rand -hex 32)"
kubectl -n mtg-engine create secret generic mtg-sim \
  --from-literal=MTG_SIM_TOKEN="$MTG_SIM_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n agents port-forward svc/agents-vault-web 8000:80 >/dev/null &
PF=$!
sleep 3
jq -n --arg v "$MTG_SIM_TOKEN" '{name: "mtg-sim", description: "Bearer token of the mtg-stack /sim/mcp endpoint", labels: [], envs: [{env: "MTG_SIM_TOKEN", protected: true, value: $v}]}' \
  | curl -sS -X POST -H 'Content-Type: application/json' --data-binary @- \
      http://127.0.0.1:8000/vault/v1/entries | jq '{id, name, version}'
kill "$PF"
unset MTG_SIM_TOKEN PF
kubectl -n mtg-engine rollout restart deployment/mtg-stack
```

Rotation (same Secret apply, then a new vault version on the existing entry; set `ENTRY_ID` to
the entry id, a uuid):

```sh
MTG_SIM_TOKEN="$(openssl rand -hex 32)"
kubectl -n mtg-engine create secret generic mtg-sim \
  --from-literal=MTG_SIM_TOKEN="$MTG_SIM_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n agents port-forward svc/agents-vault-web 8000:80 >/dev/null &
PF=$!
sleep 3
jq -n --arg v "$MTG_SIM_TOKEN" '{values: {MTG_SIM_TOKEN: $v}}' \
  | curl -sS -X PUT -H 'Content-Type: application/json' --data-binary @- \
      "http://127.0.0.1:8000/vault/v1/entries/$ENTRY_ID/value" | jq '{id, name, version}'
kill "$PF"
unset MTG_SIM_TOKEN PF
kubectl -n mtg-engine rollout restart deployment/mtg-stack
```

**Restart after every Secret change, first creation included.** `50-deployment.yaml` reads the
Secret as env with `optional: true`, and env from a Secret is read at pod start, not
live-reloaded. A pod that started before the Secret existed keeps the endpoint disabled (503)
until `kubectl -n mtg-engine rollout restart deployment/mtg-stack`.

**Rotating.** The vault write creates a new entry version, but agents-vault pins the version at
session create and a repeat pin returns the first pins, and `session_patch` cannot change vault
refs. So existing sessions keep the old token until they are discarded and recreated. From the
moment the Secret changes and the Deployment restarts until a session is recreated, that session
gets 401 from `/sim/mcp`. Never paste the value into a chat, a commit or a log.

## Login: oauth2-proxy and Keycloak

Phase 2b. The play UI host moved off basic auth and behind oauth2-proxy in
front of Keycloak. The shape:

```
play-magic.szymonrichert.pl -> Ingress mtg-stack-sso (no auth annotations)
                             -> Service mtg-oauth2-proxy:80
                             -> the proxy
                             -> http://mtg-stack.mtg-engine.svc.cluster.local:8000
```

oauth2-proxy is an **in-path reverse proxy** - its own Deployment and Service,
`55-oauth2-proxy.yaml` - sitting in front of `mtg-stack` at the edge, not the
nginx `auth-url`/`auth-signin` external-auth pattern. `auth-url`/`auth-signin`
are forbidden on any ingress in this workbench (they turn a bearer-token
request into a cookie-login redirect and break every machine client that
would otherwise present a JWT straight through), and `test_k8s_manifests.py`
now pins that no Ingress anywhere in this deploy carries either annotation.

### Why a first-class Deployment, not the estate's sidecar injector

The estate has a mutating-webhook injector that adds an oauth2-proxy sidecar
to any Pod carrying four opt-in annotations - the "obvious" one-line option,
and the first thing evaluated. It does not fit here, for four separate
reasons:

- **This namespace's `restricted` Pod Security Standard rejects it outright.**
  `mtg-engine` is the only namespace involved that enforces `restricted`; the
  injector's sidecar carries no `securityContext` at all. A server-side
  dry-run of a minimal, fully restricted-compliant pod carrying the injector's
  opt-in annotations was rejected at admission in this session:

  ```
  Error from server (Forbidden): error when creating "pss-probe.yaml": pods "injector-pss-probe"
  is forbidden: violates PodSecurity "restricted:latest": allowPrivilegeEscalation != false
  (container "oauth2-proxy-proxy" must set securityContext.allowPrivilegeEscalation=false),
  unrestricted capabilities (container "oauth2-proxy-proxy" must set securityContext.capabilities.drop=["ALL"])
  ```

  The mutating webhook runs before validating admission, so the sidecar it
  adds is what PSS then rejects, taking the whole Pod down with it. Every
  namespace the injector successfully runs in today carries no PSS label at
  all; relaxing `mtg-engine` to get it working would give up hardening this
  Deployment and every seat Job already satisfy.
- **It is master-realm only.** The injector's OIDC issuer is a process-wide
  flag pointed at `realms/master`, with no per-Pod override. This login needs
  the `spellslinger` realm, which the injector cannot reach.
- **It hijacks the Service port every in-cluster caller depends on.** The
  injector renames the app container's `http` port to `http-insecure` and
  gives its own sidecar the name `http`, so any Service that targets by name
  silently switches to the proxy. AI seat Jobs, the driver and the MCP server
  all reach this API at `http://mtg-stack.mtg-engine.svc.cluster.local:8000`
  directly, never through the ingress - hijacking that Service would 302 every
  one of those in-cluster callers to Keycloak, exactly the failure this login
  must not cause. `test_k8s_manifests.py` pins that the `mtg-stack` Service
  still exposes exactly `[8000]` selecting `app: mtg-stack`.
- **It puts the client secret in the pod spec as a plaintext literal**, and
  emits neither `SET_XAUTHREQUEST` nor `PASS_ACCESS_TOKEN`, so the
  cookie-to-bearer conversion a future frontend needs is impossible through
  it.

`55-oauth2-proxy.yaml` is plain YAML reproducing the estate's
`oauth2proxy-external` chart's rendered env contract by hand, because this
repo has no Helm and no helmfile - `mise run k8s:deploy` is a `sed` over
`deploy/k8s/*.yaml` piped to `kubectl apply -f -`, and introducing a single
OCI Helm release into a namespace otherwise managed by `kubectl apply` would
be a split-brain deploy story.

### The one prerequisite: Secret `magic-trainer-webui-proxy`

`55-oauth2-proxy.yaml` hard-references one Secret, `magic-trainer-webui-proxy` in
namespace `mtg-engine`, with six keys: `proxy-client-id`,
`proxy-client-secret`, `proxy-cookie-secret`, `proxy-cookie-domain`,
`proxy-redirect-url`, `proxy-allowed-groups`. It is created by terraform in
the `infrastructure` repo's `keycloak/` root and is **never committed here** -
same pattern as the Claude and GitHub Secrets above. The `mtg-oauth2-proxy`
Pod will not start without it: every one of those six keys is a
`secretKeyRef`, so a missing Secret means every credential env var is unset
and oauth2-proxy fails at boot. If the Pod sits in `CreateContainerConfigError`
or similar, check `kubectl -n mtg-engine get secret magic-trainer-webui-proxy` first.

### `--api-route=^/api/`, and why

The proxy runs with `--api-route=^/api/`. Per oauth2-proxy's own docs, a
request to a path matching `--api-route` must already carry a valid session
cookie (or a bearer JWT, once `--skip-jwt-bearer-tokens` applies) - no
redirect to login is ever done for it, it gets a plain 401 instead. So a
browser **navigation** to `/` or `/g/<id>/` still gets the normal 302 to
Keycloak, while the play UI's ~1.2s XHR polls against `/api/*` get a clean,
same-origin 401 the JavaScript can act on - instead of a cross-origin 302 to
`auth.szymonrichert.pl` that `fetch` can only see as an opaque `TypeError:
Failed to fetch`.

`ui/api.js` is the app-side half of this: its `api()` wrapper now treats any
401/403 as `goToLogin()`, which navigates to
`` `/oauth2/start?rd=${encodeURIComponent(pathname + search)}` `` (oauth2-proxy's
documented login-start endpoint, kept relative so the redirect target never
needs `--whitelist-domain`) at most once, guarded against the board poll, the
seat poll and a lobby refresh all hitting a 401 in the same tick. Every
caller that used to silently swallow an auth failure - the board poll, the
seat poll, `refreshGames`, `loadBundledDecks` - gets the redirect for free,
because it fires inside `api()` before the throw.

### The cutover, and how not to lock yourself out

Two steps. Production is untouched by step 1.

**Step 1 - stand the proxy up, change nothing at the edge.** Apply with
`70-ingress.yaml` still at its pre-cutover state: the host keeps its old
auth and keeps pointing at the app, and `55-oauth2-proxy.yaml` just adds a
Deployment and Service that nothing routes to yet. Verify without a browser:

```sh
kubectl -n mtg-engine port-forward deploy/mtg-oauth2-proxy 4180:4180
curl -sI localhost:4180/            # expect 302 to https://auth.szymonrichert.pl/realms/spellslinger/...
                                    #   with redirect_uri=https%3A%2F%2Fplay-magic.szymonrichert.pl%2Foauth2%2Fcallback
curl -sI localhost:4180/api/health  # expect 401, NOT a 302 - proves --api-route
```

The first `curl` carrying the right issuer and the right `redirect_uri`
proves in one shot that OIDC discovery reached Keycloak, that the client id
resolved, and that the `magic-trainer-webui-proxy` Secret mounted. The second proves
`--api-route` is doing what it should. If either fails, nothing has changed
for any user yet - fix it before step 2.

**Step 2 - flip the edge.** Apply the `70-ingress.yaml` change: the host
`play-magic.szymonrichert.pl` moves to `mtg-stack-sso`, the Ingress
pointing at the proxy with no auth annotations. The old basic-auth escape
hatch (`mtg-stack` Ingress on `pl.szymonrichert.pl`, `play-magic-auth` Secret)
has been removed from the manifests; `kubectl port-forward` is the
unconditional fallback if the SSO path fails.

**TLS.** `mtg-stack-sso` deliberately reuses the existing `play-magic-tls`
Secret, which already carries a valid SAN for `play-magic.szymonrichert.pl`,
so the production host has **zero TLS gap** through this cutover.

**Rollback** is one file: revert the `70-ingress.yaml` change and re-run
`mise run k8s:deploy`. The `mtg-oauth2-proxy` Deployment can stay running,
unrouted, while whatever went wrong is fixed. Full removal is deleting
`55-oauth2-proxy.yaml`.

## Apply

In order. Steps 1-4b are the ones you only do once per cluster.

```sh
# 1. namespace, then the restricted Pod Security Standard on it
kubectl apply -f deploy/k8s/00-namespace.yaml
kubectl label namespace mtg-engine pod-security.kubernetes.io/enforce=restricted

# 2. registry pull secret (copied from an existing namespace)
kubectl -n default get secret regcred -o yaml \
  | sed 's/namespace: default/namespace: mtg-engine/' \
  | kubectl apply -f -

# 3. the two credentials
kubectl -n mtg-engine create secret generic mtg-agent \
  --from-literal=CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN"
kubectl -n mtg-engine create secret generic mtg-decks-git \
  --from-literal=GH_PAT="$GH_PAT"

# 4. RBAC (ServiceAccount, Role, RoleBinding) - the API Deployment names this
#    ServiceAccount, so apply it before or together with everything else
kubectl apply -f deploy/k8s/40-rbac.yaml

# 4b. the oauth2-proxy credential Secret - created by terraform in the
#     infrastructure repo, NOT by a command in this repo. mtg-oauth2-proxy
#     will not start without it. See Login: oauth2-proxy and Keycloak.
kubectl -n mtg-engine get secret magic-trainer-webui-proxy

# 5. everything else, with the image tag substituted - deploy/k8s/*.yaml now
#    includes 55-oauth2-proxy.yaml, so this also stands up the proxy
#    Deployment and Service (see Login: oauth2-proxy and Keycloak for the
#    two-step cutover that actually routes traffic to it). This is now a
#    PHASED apply, not one `kubectl apply -f -` of the whole directory: the
#    database Cluster is created and waited on, then a migration Job runs
#    and is waited on, and only once it succeeds is the Deployment (which
#    expects the new schema) applied - see "Migrations: a Job, waited on"
#    above for why, and the mise task itself for the exact ordering.
mise run k8s:deploy 1.2.3
#   or, without mise (TAG is the released X.Y.Z):
#   see the hand-equivalent phased sequence under
#   "Migrations: a Job, waited on" above - do NOT run
#   `sed "s/__TAG__/${TAG}/g" deploy/k8s/*.yaml | kubectl apply -f -` as a
#   single shot any more, it applies the Deployment before the migration Job
#   is known to have finished

# 6. watch it come up
kubectl -n mtg-engine get pod -l app=mtg-stack -w
```

The API Pod is a single container; it is ready as soon as `/api/health`
answers, no `Init:` phase and no multi-minute claude boot to wait on any more -
that all happens per-seat, inside a Job, once a game actually dispatches an AI
seat:

```sh
kubectl -n mtg-engine get jobs -l app.kubernetes.io/name=mtg-agent
kubectl -n mtg-engine get pods -l app.kubernetes.io/name=mtg-agent
kubectl -n mtg-engine logs job/<name> -c agent -f
kubectl -n mtg-engine logs job/<name> -c driver -f
```

### The `mtg-agent-memory` PVC cannot change access mode in place

If this cluster already has a `mtg-agent-memory` PVC bound from an earlier
apply (the one-Pod-three-container design used `ReadWriteOnce`), `kubectl
apply` on the new `ReadWriteMany` spec will be **rejected**, not silently
applied:

```
The PersistentVolumeClaim "mtg-agent-memory" is invalid: spec: Forbidden:
spec is immutable after creation except resources.requests and
volumeAttributesClassName for bound claims
```

A PVC's `accessModes` cannot be changed after it is bound - confirmed with
`kubectl apply --dry-run=server` against this cluster's existing PVC before
committing this change. The only fix is delete-and-recreate, and the default
`Delete` reclaim policy on `rook-ceph-rwx` means deleting the PVC deletes the
underlying volume too - **which is where the agent's cross-game notes live**.
The order that actually works:

1. **Scale the Deployment (or otherwise stop every Pod that mounts the
   claim) to zero first.** `kubernetes.io/pvc-protection` is a finalizer
   Kubernetes puts on every PVC that is mounted by a running Pod: `kubectl
   delete pvc mtg-agent-memory` while the old Pod is still running does not
   error and does not remove it - it marks the PVC `Terminating` and the
   delete simply hangs there until the last Pod using it goes away. Nothing
   is lost by this hang, but it is easy to mistake for the delete having
   failed outright.
2. **Copy `/workspace/memory` out** of the (now-stopped, or a throwaway) Pod
   that still mounts the old claim - `kubectl cp`, run before step 1 if that
   is easier, or from a one-off Pod you create yourself mounting the same
   PVC read-only.
3. Delete the PVC (now it actually goes away, per the finalizer above), then
   `kubectl apply` the new `ReadWriteMany` spec.
4. **Copy the notes back in through a one-off Pod that mounts the new
   claim** - not the API Deployment's own Pod, since that mounts
   `scryfall-cache` and nothing else; a bare `Pod` spec with
   `mtg-agent-memory` as its only volume, `kubectl cp` the backed-up
   `/workspace/memory` into it, then delete that Pod.

This was **not** performed as part of writing these manifests - it touches a
live volume and was left for whoever actually cuts the cluster over.

## Reach it

| what | where |
|---|---|
| play (production) | `https://play-magic.szymonrichert.pl/` - Keycloak login via oauth2-proxy, see [Login: oauth2-proxy and Keycloak](#login-oauth2-proxy-and-keycloak). `/play/` redirects here |
| a seat's driver status | port-forward that seat Job's Pod, then `http://127.0.0.1:8090/status` |
| a seat's agent turns, probe, interrupt | port-forward that seat Job's Pod, then `http://127.0.0.1:8080/v1/...` |

```sh
kubectl -n mtg-engine port-forward deploy/mtg-stack 8000
kubectl -n mtg-engine port-forward job/<seat-job-name> 8080 8090
```

Neither 8080 nor 8090 is on the `mtg-stack` Service or the ingress -
there is no longer one fixed agent/driver pair behind that Service to route
to, and both remain unauthenticated, so a seat Job's debug ports are
reachable only by port-forward (or from inside the cluster, and only from the
API Pod - see the NetworkPolicy above).

In-cluster callers - AI seat Jobs, the driver, the MCP server - always reach
the API at `http://mtg-stack.mtg-engine.svc.cluster.local:8000` directly,
never through the ingress or through oauth2-proxy. Nothing about the login
change touches this: `60-service.yaml` is unchanged, and a test now pins that
the `mtg-stack` Service still exposes exactly `[8000]` selecting
`app: mtg-stack`.

`*.szymonrichert.pl` resolves to the LAN IP at home and to the public IP from
outside, so the host works from both with no extra DNS record - and is
really on the internet, which is why it is fronted by oauth2-proxy/Keycloak
rather than reaching `mtg-stack:8000` unauthenticated. cert-manager issues the
certificate from the `letsencrypt-prod` ClusterIssuer; `play-magic.szymonrichert.pl`
reuses the pre-existing `play-magic-tls` Secret with zero gap.

## Logs

```sh
kubectl -n mtg-engine logs deploy/mtg-stack -c game -f | grep "PROBLEM REPORTED"
kubectl -n mtg-engine get jobs -l app.kubernetes.io/name=mtg-agent
kubectl -n mtg-engine logs job/<name> -c guides             # the deck-guide clone
kubectl -n mtg-engine logs job/<name> -c agent -f            # session boot, turns, a dead claude
kubectl -n mtg-engine logs job/<name> -c driver -f           # the match lifecycle
```

There is no `--previous` container to fall back to any more: a finished seat
is a finished Job (`ttlSecondsAfterFinished`), not a replaced container in an
otherwise-still-running Pod. Grab the logs before the TTL cleans the Job up,
or raise `AGENT_JOB_TTL_SECONDS` if you need a longer window.

**Metrics, breaking change**: each seat's `agent` container's
`ccw_turn_tokens_total` (scraped from its own Pod, per seat Job, not
aggregated here) now carries `model` and `effort` labels alongside `type` -
one series per (type, model, effort) rather than per `type` alone. Anything
scraping or dashboarding this metric across seats needs `sum by (type)` (or
similar) to collapse back to the old one-series-per-type shape; per-game
`agent` settings otherwise fan it out into one series per model/effort
combination in play.

## What changed versus the one-Pod-three-container design

### The API Deployment lost two containers and four volumes

`agent`, `driver`, the `guides` initContainer, and the `guides`, `memory`,
`mcp-base`, `driver-workspace` volumes are all gone from `50-deployment.yaml`.
They still exist - just per-seat, inside the Job `k8s_seats.build_job`
renders from `deploy/seat-job.yaml`, not baked into this Deployment's Pod
template. The API Deployment keeps exactly the `game` container and the
`scryfall-cache` PVC, plus a new `serviceAccountName: mtg-api` and the env
described below.

### `AI_SEAT_MODE=kubernetes`, and the env that goes with it

The `game` container's env grew: `AI_SEAT_MODE=kubernetes` (in place of the
old `wrapper` plus loopback `DRIVER_URL`, which no longer applies - there is
no longer one sidecar driver to loop back to), `AGENT_NAMESPACE`,
`AGENT_IMAGE`, `API_IMAGE`, `API_SERVICE_URL`, `SPELLSLINGER_MCP_URL`, and
`MAX_LIVE_GAMES`. It also now carries `POSTGRES_HOST`/`POSTGRES_PORT`/
`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` (from the `mtg-database-app`
Secret) and `REQUIRE_POSTGRES=1` - see [Database: CloudNativePG](#database-cloudnativepg)
and [Migrations: a Job, waited on](#migrations-a-job-waited-on).
`REQUIRE_POSTGRES` is what makes `store.py` crash loudly at startup on a
missing or unreachable database instead of silently falling back to
`InMemoryRepository` - the whole reason this Deployment now points at a real
database rather than merely being able to. There is deliberately no
downward-API `POD_NAME`/`POD_UID`/`POD_NAMESPACE` any more (phase 2b T9/T10):
seat Jobs carry no `ownerReferences` at all now, because with durable games
an API restart must not garbage-collect the seat Jobs of games that survived
it -
`activeDeadlineSeconds`/`ttlSecondsAfterFinished` on each Job are the whole
backstop.

`MTG_STACK_INSTANCE_ID` (required in kubernetes mode) is this stack's
identity. Every Job it creates carries it as the `mtg/stack-instance` label,
and reconcile deletes only the Jobs that carry its own value, so no other
process can reap this stack's seats. The transition rule for Jobs created
before the label existed: reconcile never deletes them. They still expire,
because each one's `activeDeadlineSeconds` (at most 4h) and
`ttlSecondsAfterFinished` bound it, and `release()` still deletes a seat's
Job by name.

Kubernetes mode builds its client only from the pod's in-cluster service
account. There is no silent fallback to a kubeconfig any more: running
outside a cluster needs `MTG_K8S_OUT_OF_CLUSTER=1` set on purpose, and then
it uses the current kubeconfig context. Nothing in this repo sets that
opt-in, not compose (which runs `wrapper` mode), not scripts and not tests.
The 2026-09-27 incident is why: a test run on a laptop whose context was
prod reconciled a live game's seat away.

### The follow-up DSL-development Job

A seat's (or buddy's) handover that ends `unfinished` gets one more Job:
`mtg-followup-<game>-<uid>`, rendered from the same `deploy/seat-job.yaml`
by `k8s_seats.build_followup_job`. Same pod shape - guides clone, agent,
driver - with four differences, all set in code:

- **the credential is the original one.** The driver runs as the seat's (or
  advisor's) own uid and role, the credential `game_access` already binds to
  that player; nothing is minted and nothing is widened.
- **Opus, one turn.** `__MODEL__` is `AgentSettings.review_default()`, and
  the assignment carries `mode: "followup"` plus the payload the api built
  from the ledger. The driver pushes no turn 0 and plays nothing: one
  45-minute turn (a nudge at 40), then its `plays_outcome` row with
  `followup: true`, then it retires and the Job completes.
- **its own label and deadline.** It carries `mtg/followup-of: <uid>`
  *instead of* `mtg/seat-uid`, so the seat's own pod lookup never finds it,
  and `activeDeadlineSeconds: 3600` through the template's
  `__ACTIVE_DEADLINE_SECONDS__` placeholder (a seat's stays 14400).
- **one per (game, uid).** Deduped by that label in the cluster and by the
  `plays_outcome_followup` ledger row; the follow-up's own outcome never
  schedules another.

It keeps `mtg/game-id`, so the hard stops are the ones every seat Job has:
End game deletes it through the registry's release, Forget has the
reconcile loop delete it (its game left the live set), and its preStop
`/handover` makes the driver stop the turn and write the marker at once.
While it runs, `GET /api/lobby/games` reports the finished game with
`post_game_work: true` for up to `lobby.FOLLOWUP_RELEASE_SECONDS`. The
existing RBAC (`jobs` create/list/delete) and NetworkPolicy
(`app.kubernetes.io/name: mtg-agent`) already cover it.

```sh
kubectl -n mtg-engine get jobs -l mtg/followup-of          # every follow-up
kubectl -n mtg-engine logs job/mtg-followup-<game>-<uid> -c driver
```

### Per-seat MCP config, not a ConfigMap

The old `20-configmap-mcp.yaml` supplied one `mcp.base.json`, mounted once,
shared by whichever agent happened to be running in the one Pod. There is no
longer one agent to share it with: each seat Job needs its own `mtg` MCP
server entry. `deploy/seat-job.yaml`'s `mcp-config` initContainer writes a
static two-entry `mcp.json` to an emptyDir (`mtg` as a stateless HTTP entry,
its URL the one substituted `MCP_URL` env - phase 2b M2) - no ConfigMap, no
shared object, and no more per-game `mcp.py --game` subprocess command.

### RBAC and a NetworkPolicy are new, because the API now creates Pods

Nothing in the old design needed the API to talk to the Kubernetes API
server. Now it creates a Job (hence a Pod) per seat, so it needs its own
ServiceAccount and Role (`40-rbac.yaml`), and those seat Pods need their
network access constrained the way loopback constrained it before
(`45-networkpolicy.yaml`) - see both sections above.

### The Service and Ingress lost two ports

`60-service.yaml` is down to port 8000; `70-ingress.yaml` was already
8000-only. The old 8080/8090 debug ports pointed at one fixed agent/driver
pair sharing this Deployment's Pod; that pair does not exist any more, so
there is nothing stable for the Service to route those ports to. Use
port-forward against a seat Job's own Pod instead (see
[Reach it](#reach-it)).

### `mtg-agent-memory` becomes `ReadWriteMany`

Several seats' Pods now mount the same claim at once, for as many games as
`MAX_LIVE_GAMES` allows running concurrently, potentially scheduled on
different nodes. `ReadWriteOnce` would let only the first seat's Pod bind it;
every other seat's Pod would sit in `ContainerCreating` waiting for the
volume. See [The `mtg-agent-memory` PVC cannot change access mode in
place](#the-mtg-agent-memory-pvc-cannot-change-access-mode-in-place) for what
that means for a cluster that already has this PVC bound.

### Everything inside one seat Job is still the old three-container shape

The env, volumes, probes and non-root `securityContext` that used to live on
this Deployment's `agent` and `driver` containers are still exactly that
shape - they just live in `k8s_seats.build_job` now, one Job per seat, instead
of being baked into this file. Read that module next to this README for the
per-seat wiring (ports, `INTERNAL_ADDR` moved off 8090, `BASE_MCP_PATH`, the
guides clone script, the probes, resource requests/limits).

## Storage choices, honestly

### Deck guides: still an initContainer clone, now per-seat

There is no bind mount and no ConfigMap-backed shared copy. Each seat Job's
`guides` initContainer clones `github.com/szymonrychu/mtg-decks` fresh into
that Job's own `emptyDir` before its `agent`/`driver` containers start - the
same script the old Pod-level initContainer ran, just templated per Job by
`k8s_seats.build_job` instead of hand-written once here. The PAT-hygiene and
loud-failure behaviour described in the module docstring is unchanged.

### Agent memory: a PVC, and it is the important one

`/workspace/memory` is the only thing that crosses a match boundary. A seat
Job's containers are gone once the Job finishes; the notes they wrote are the
entire mechanism for cross-game learning. It gets `mtg-agent-memory`, a 1Gi
`ReadWriteMany` PVC, mounted by every concurrent seat Job's Pod at once.

### Scryfall cache: a PVC too

`/app/.scryfall-cache`, 2Gi, mounted on the API Deployment only (seat Jobs
never fetch card data directly). Not correctness-critical, purely speed: a
cold cache re-fetches every card on first use (measured at 34.5s for a single
decklist), and every Pod restart would pay it again without a volume.

### StorageClass

Neither PVC names a `storageClassName`, so both take the cluster default. In
this cluster that is **`rook-ceph-rwx`** (CephFS), confirmed with
`kubectl get sc` - both claims bind to it as written, no edit needed, and
CephFS is what makes `ReadWriteMany` on `mtg-agent-memory` possible at all. On
a cluster with no default they would stay `Pending` forever and the Pod would
never start; `30-storage.yaml` explains the fix.

## What was actually validated

- `uv run pytest tests/test_k8s_manifests.py` - parses every non-template
  manifest with `__TAG__` substituted via `yaml.safe_load_all` and asserts
  the Deployment is API-only with the right env (including `AGENT_NAMESPACE`,
  `API_SERVICE_URL`, `SPELLSLINGER_MCP_URL`) and satisfies the restricted Pod
  Security Standard (`seccompProfile`, `allowPrivilegeEscalation: false`,
  `capabilities.drop: [ALL]`), the Role/RoleBinding/absence of a ClusterRole
  including the `pods/log` rule, the memory PVC's `ReadWriteMany` mode, the
  Service's single port, the NetworkPolicy's selector and policy types, and
  the cross-file invariant that the NetworkPolicy's ingress source matches
  the Deployment's own pod-template labels.
- `kubectl apply --dry-run=client -f <rendered>` - all 13 objects accepted.
- `kubectl apply --dry-run=server -f <rendered>` against the live cluster
  (context `kubernetes-admin@kubernetes`, namespace `mtg-engine`, which
  already exists on this cluster from an earlier apply of the previous
  three-container design) - every object except the memory PVC accepted; see
  [The `mtg-agent-memory` PVC cannot change access mode in
  place](#the-mtg-agent-memory-pvc-cannot-change-access-mode-in-place) for
  the one that was not, and why.
- `kubectl cluster-info dump | grep -m1 cluster-cidr` /
  `grep -m1 service-cluster-ip-range` against the live cluster, to confirm
  the NetworkPolicy's RFC1918 `except` list already covers this cluster's own
  pod (`10.244.0.0/16`) and service (`10.96.0.0/12`) ranges.
- `kubectl -n kube-system get svc kube-dns` against the live cluster, to
  confirm CoreDNS actually lives in `kube-system` under that name
  (`10.96.0.10`, `53/UDP,TCP`) before pointing the DNS egress rule's
  `namespaceSelector` there.

## What was verified against the live cluster (2026-09-05)

The four bullets that used to sit under "What remains unverified" below were
all cleared by a cluster run recorded in
`.superpowers/sdd/2026-09-04-per-game-agent-pods/task-12-report.md`, on
`kubernetes-admin@kubernetes`, namespace `mtg-engine`, image tag
`ae38dea-9608ea9`:

- **Two concurrent seat Jobs, on the `ReadWriteMany` `mtg-agent-memory`
  claim.** Two games were dispatched at once; both Jobs' Pods mounted the
  same PVC and both agents read the restored notes under
  `/workspace/memory/unknown-deck/` as their first act. The migration itself
  (delete the old `ReadWriteOnce` claim, recreate `ReadWriteMany`, restore
  the notes from a backup) was performed by hand and confirmed byte-for-byte
  identical afterwards.
- **Admission under the restricted Pod Security Standard.** The namespace
  was labelled `pod-security.kubernetes.io/enforce=restricted` for the first
  time in this run; both seat Jobs' Pods (agent and driver containers, and
  the `guides` initContainer) were admitted and reached `Running` without a
  PSS warning or a manifest rewrite.
- **The per-game MCP config.** Each agent called `mcp__mtg__*` tools scoped
  to its own seat uid and got real answers back - no cross-talk between the
  two concurrently-running games.
- **NetworkPolicy egress** to the API (the `mtg` MCP server), DNS, the
  spellslinger MCP server, and the Anthropic API (443) - all reached
  successfully by both seat Pods.
- **`ownerReferences` garbage collection on an API restart.** `kubectl
  rollout restart deploy/mtg-stack` deleted the old API Pod, and the
  leftover seat Job (from a game ended mid-run) was already gone by the time
  the rollout reported complete - no manual cleanup needed.

## What remains unverified

- Everything the old README already flagged as unverified about the
  per-seat container shape (non-root uid 1000 for game/driver, the 8099
  internal-port move, the MCP re-point, probe timings, resource
  requests/limits, `regcred`/Harbor image availability) was exercised live
  in the run above and came up clean on the first try, but no dedicated
  chaos/failure-injection pass (a killed driver container, a stalled boot,
  an apiserver hiccup mid-reconcile) has been run against the live cluster -
  only the unit tests in `tests/test_k8s_seats.py` cover those paths.
- `kubeval` and `kubectl-validate` are still not installed here; no other
  manifest linter has run against the live cluster's admission controller
  beyond the restricted PSS label itself.

## Identity: making a Keycloak login actually own a game

Everything in the section above ends at the *edge*: oauth2-proxy decides who
may reach the site. It said nothing about who anything **belongs to**. Until
this change, no signed token ever reached `mtg-stack`, so `oidc.oidc_settings()`
answered `None`, every game was created with `owner_user_id = None`, and the
lobby's list of games could only ever be keyed on the browser's own
`localStorage`. That is the reported defect: "matches that players are seeing
are pinned to the browser instances, not username from keycloak".

Two env changes close it, and they are **two halves of one change** - see the
ordering warning below.

### The token, and which one

`55-oauth2-proxy.yaml` sets `OAUTH2_PROXY_PASS_AUTHORIZATION_HEADER=true`.
Per oauth2-proxy v7.15.4's own configuration reference, that flag will "pass
OIDC IDToken to upstream via Authorization Bearer header" - a **request**
header on the proxied path, which is exactly where `oidc.current_user` looks.
Its source is unambiguous about which token: `getAuthorizationHeader()` in
`pkg/apis/options/legacy_options.go` builds the header from
`{Claim: "id_token", Prefix: "Bearer "}`, and `session_state.go` resolves
`"id_token"` to the session's ID token.

Two nearby flags are *not* what does this, and it is worth knowing why:

| flag | token | request or response |
|---|---|---|
| `--pass-authorization-header` | ID token | upstream **request** - what we use |
| `--set-authorization-header` | ID token | **response** - for nginx `auth_request`, which this estate forbids |
| `--pass-access-token` | access token | request, as `X-Forwarded-Access-Token` |
| `--set-xauthrequest` (+ the above) | access token | response, as `X-Auth-Request-Access-Token` |

The last row is the "spellslinger-codex pattern": a frontend calls
`/oauth2/auth` and reads its own access token off the response headers. It
was rejected here because it needs the browser to fetch, cache and refresh a
token with a 30-minute lifespan, for no gain - the chosen flag needs **zero
UI change**, since the play UI's existing `fetch` calls already go through
this proxy and simply start arriving with an identity attached. There is no
legacy flag at all that forwards the *access* token as `Authorization:
Bearer`; only oauth2-proxy's alpha YAML `injectRequestHeaders` can, and
upstream reserves the right to break that config with no notice.

### The api verifies it; it never trusts it

`50-deployment.yaml` sets three variables on `mtg-stack`:

```
OIDC_ISSUER    https://auth.szymonrichert.pl/realms/spellslinger
OIDC_JWKS_URL  https://auth.szymonrichert.pl/realms/spellslinger/protocol/openid-connect/certs
OIDC_AUDIENCE  magic-trainer-webui
```

All three are public identifiers - a realm URL, its JWKS endpoint, a client
id - never credentials. The client *secret* stays in the
`magic-trainer-webui-proxy` Secret and is read only by the proxy.

`oidc.py` verifies the forwarded token's **signature, issuer, expiry and
audience locally against the realm's JWKS** on every request. This is the
binding rule from `docs/architecture.md`: "API services validate bearer JWTs
locally against the realm's JWKS. They never trust `X-Forwarded-*` headers."
Nothing here trusts a header on its own authority - a forged `Authorization`
header, from the proxy's position or anywhere else, is refused with a 401
because it does not verify. JWKS is fetched live (and cached per URL) rather
than pinned with `OIDC_JWKS_STATIC`; that variable is `oidc.py`'s offline/test
escape hatch, and pinning a key would break the first time the realm rotates
its signing keys.

### Ordering: never apply one half without the other

`oidc.current_user` refuses a bearer token it has no issuer to check -
deliberately, so an unprovable identity is never silently downgraded to
anonymous. So **enabling the forwarding while `mtg-stack` has no `OIDC_*`
config 401s the entire play UI.** `tests/test_k8s_manifests.py::test_the_two_halves_ship_together`
pins the two together so they cannot drift apart in this repo.

`mise run k8s:deploy` applies both files in one pass, so the committed state
is always consistent. The only window is the seconds during which
`mtg-stack`'s `Recreate` rollout is between Pods while the proxy is already
forwarding; it is self-healing. To avoid even that, apply in two steps:

```sh
# 1. Give the api its OIDC config first. Harmless on its own: no token is
#    being forwarded yet, so nothing changes and every game stays unowned.
kubectl -n mtg-engine set env deploy/mtg-stack \
  OIDC_ISSUER=https://auth.szymonrichert.pl/realms/spellslinger \
  OIDC_JWKS_URL=https://auth.szymonrichert.pl/realms/spellslinger/protocol/openid-connect/certs \
  OIDC_AUDIENCE=magic-trainer-webui
kubectl -n mtg-engine rollout status deploy/mtg-stack

# 2. Then turn on the forwarding.
kubectl -n mtg-engine set env deploy/mtg-oauth2-proxy \
  OAUTH2_PROXY_PASS_AUTHORIZATION_HEADER=true
kubectl -n mtg-engine rollout status deploy/mtg-oauth2-proxy
```

A later `mise run k8s:deploy` converges on the same values from the committed
manifests.

### Prerequisite in the `infrastructure` repo

The `spellslinger` realm, the `magic-trainer-webui` confidential client, its
audience mapper, the `magic-trainer-webui` group and the two test users are
created by terraform, in that repo's `keycloak/` root module. Nothing here
creates any of it; this deployment only points at it. **That terraform must be
applied before this change does anything**, and applying it is a merge to
`main` in the terraform repo (its CI auto-applies).

### What does *not* change

- **Machine clients are untouched.** `seat_cli`, `seatkit`/`mcp.py`, the AI
  seat Jobs and the in-cluster driver all reach
  `mtg-stack.mtg-engine.svc.cluster.local:8000` directly and never pass
  through the proxy at all. They carry no bearer token and authenticate with
  a seat uid, which `game_access.py` permits independently of who owns a
  game. `tests/test_user_separation.py` pins that.
- **No ingress gains `auth-url`/`auth-signin`.** The binding rule holds; the
  proxy stays an in-path reverse proxy.
- **Games that already exist stay unowned.** Nothing backfills
  `owner_user_id` on a row created before this. Those games remain visible to
  any browser that still holds one of their seat uids, exactly as today, and
  they will not appear on a second computer. Only games created *after* this
  is live get an owner and become cross-device. To move an old game over,
  open it once on the old browser and copy the URL - or just end it and start
  a new one.
