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
| `30-storage.yaml` | PVCs for the agent's memory (now `ReadWriteMany` - see below) and the Scryfall cache |
| `40-rbac.yaml` | a `mtg-api` ServiceAccount, namespaced Role and RoleBinding letting the API create/watch/delete Jobs and read Pods |
| `45-networkpolicy.yaml` | pins every agent-seat Pod: reachable only from the API, allowed out only to the API, DNS, spellslinger, and HTTPS |
| `50-deployment.yaml` | the API Deployment - one replica, `Recreate` strategy, one `game` container: ports, env, probes, resources |
| `60-service.yaml` | ClusterIP Service for port 8000 only |
| `70-ingress.yaml` | the basic-auth Secret and the TLS ingress on `play-magic.szymonrichert.pl`, routing port 8000 only |

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
mise run k8s:deploy
```

which substitutes `__TAG__` with the commit sha and pipes the result to
`kubectl apply -f -`. Nothing rendered is written back into the repo. The
Deployment sets `AGENT_IMAGE` and `API_IMAGE` env vars to the same
`harbor.szymonrichert.pl/.../*:__TAG__` strings, substituted by the exact same
`sed`, so one substitution pins both the API's own image and the image every
Job it creates will pull - no separate `__AGENT_IMAGE__`/`__API_IMAGE__`
placeholder was needed.

If you are not using mise, do the same thing by hand - `TAG` has to match what
`mise.toml`'s `k8s:render`/`k8s:deploy` compute (`<api-sha>-<engine-sha>`, the
two repos' commit shas joined with a hyphen, docker:push tags the image with
the same string), not just this repo's own sha:

```sh
TAG="$(git rev-parse --short HEAD)-$(git -C ../magic-cards-model rev-parse --short HEAD)"
sed "s/__TAG__/${TAG}/g" deploy/k8s/*.yaml | kubectl apply -f -
```

`imagePullPolicy: IfNotPresent` throughout: a short commit sha is immutable, so
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

```sh
kubectl -n mtg-engine create secret generic mtg-decks-git \
  --from-literal=GH_PAT="$GH_PAT"
```

Scope: read access to **one private repo**, `szymonrychu/mtg-decks`, and nothing
else. A classic PAT with the `repo` scope works; a fine-grained PAT scoped to
that single repository with `Contents: Read-only` is tighter and preferred.
Every seat Job's `guides` initContainer uses it to clone the repo fresh into
that Job's own emptyDir.

`10-secret.template.yaml` exists for people who want either declarative; copy it
to `10-secret.yaml` (gitignored) and fill in the placeholders.

### The basic-auth Secret is already in git, on purpose

`70-ingress.yaml` carries `play-magic-auth` with a real, verified bcrypt hash
for user `play` / password `magic`. That password was asked for explicitly and
is deliberately weak, so there is nothing to protect by hiding the hash, and
committing it makes `kubectl apply` one step. It is a speed bump against
crawlers, not security. See the blunt comment on the Secret itself.

## Apply

In order. Steps 1-4 are the ones you only do once per cluster.

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

# 5. everything else, with the image tag substituted
mise run k8s:deploy
#   or, without mise (TAG must match mise.toml's <api-sha>-<engine-sha>):
#   TAG="$(git rev-parse --short HEAD)-$(git -C ../magic-cards-model rev-parse --short HEAD)"
#   sed "s/__TAG__/${TAG}/g" deploy/k8s/*.yaml | kubectl apply -f -

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
| play | `https://play-magic.szymonrichert.pl/` - user `play`, password `magic`. `/play/` redirects here |
| a seat's driver status | port-forward that seat Job's Pod, then `http://127.0.0.1:8090/status` |
| a seat's agent turns, probe, interrupt | port-forward that seat Job's Pod, then `http://127.0.0.1:8080/v1/...` |

```sh
kubectl -n mtg-engine port-forward deploy/mtg-stack 8000
kubectl -n mtg-engine port-forward job/<seat-job-name> 8080 8090
```

Neither 8080 nor 8090 is on the `mtg-stack` Service or the ingress any more -
there is no longer one fixed agent/driver pair behind that Service to route
to, and both remain unauthenticated, so a seat Job's debug ports are
reachable only by port-forward (or from inside the cluster, and only from the
API Pod - see the NetworkPolicy above).

`*.szymonrichert.pl` resolves to the LAN IP at home and to the public IP from
outside, so the host works from both with no extra DNS record - and is really
on the internet, which is why only 8000 is routed and why it has basic auth in
front of it. cert-manager issues the certificate from the `letsencrypt-prod`
ClusterIssuer into the `play-magic-tls` Secret on first apply; the first request
can 502 or serve a self-signed cert for the minute that takes.

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
constructs, not baked into this Deployment's Pod template. The API Deployment
keeps exactly the `game` container and the `scryfall-cache` PVC, plus a new
`serviceAccountName: mtg-api` and the downward-API env described below.

### `AI_SEAT_MODE=kubernetes`, and the env that goes with it

The `game` container's env grew: `AI_SEAT_MODE=kubernetes` (in place of the
old `wrapper` plus loopback `DRIVER_URL`, which no longer applies - there is
no longer one sidecar driver to loop back to), `AGENT_NAMESPACE`,
`AGENT_IMAGE`, `API_IMAGE`, `API_SERVICE_URL`, `SPELLSLINGER_MCP_URL`,
`MAX_LIVE_GAMES`, and `POD_NAME`/`POD_UID`/`POD_NAMESPACE` from the downward
API. The last three let `k8s_seats.build_job` set each Job's
`ownerReferences` to this Pod - but only when `POD_NAMESPACE` equals
`AGENT_NAMESPACE` (both `mtg-engine` here), because a cross-namespace
`ownerReferences` entry makes the garbage collector delete the Job within
seconds of creation instead of waiting for it to finish.

### Per-seat MCP config, not a ConfigMap

The old `20-configmap-mcp.yaml` supplied one `mcp.base.json`, mounted once,
shared by whichever agent happened to be running in the one Pod. There is no
longer one agent to share it with: each seat Job needs a `--game`-scoped `mtg`
MCP server, one per concurrent game. `k8s_seats.build_job` bakes that JSON
straight into the Job spec (`mcp_config`, written by the `mcp-config`
initContainer to an emptyDir) instead - no ConfigMap, no shared object at all.

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
- `kubectl apply --dry-run=client -f <rendered>` - all 11 objects accepted.
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
