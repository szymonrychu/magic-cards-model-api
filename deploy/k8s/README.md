# The agent stack on Kubernetes - one Pod, three containers

**Status: a draft. These manifests have never been applied to a running
cluster.** They validate (see [What was actually validated](#what-was-actually-validated))
and the wiring below was derived from `docker-compose.yml` and the code, not
guessed - but nothing here has served a game. Treat the first `kubectl apply`
as the real test. `docker compose up` remains the supported way to run this.

This is the compose topology with the network collapsed onto loopback: the
game, the agent and the driver as three containers in a single Pod, plus one
initContainer that clones the deck guides. Plain YAML, no Helm, no Kustomize,
no operator.

| file | what it is |
|---|---|
| `00-namespace.yaml` | the `mtg-engine` namespace (NOT `mtg` - see below) |
| `10-secret.template.yaml` | templates for the Claude credential and the GitHub PAT - placeholders only, never real values |
| `20-configmap-mcp.yaml` | `mcp.base.json` with `mtg` re-pointed at `127.0.0.1:8000` and spellslinger at its in-cluster Service |
| `30-storage.yaml` | PVCs for the agent's memory and the Scryfall cache |
| `50-pod.yaml` | the Pod: guides initContainer, then game + agent + driver, ports, env, probes, resources |
| `60-service.yaml` | ClusterIP Service for 8000 (play), 8080 (agent API), 8090 (driver status) |
| `70-ingress.yaml` | the basic-auth Secret and the TLS ingress on `play-magic.szymonrichert.pl`, routing port 8000 only |

## The three things you have to supply

Everything else applies as-is. These three do not exist until you create them,
and the Pod will not run without the first two.

| what | where it goes | how |
|---|---|---|
| **Claude OAuth token** (`claude setup-token`) | Secret `mtg-agent`, key `CLAUDE_CODE_OAUTH_TOKEN`, namespace `mtg-engine` | [Create the Secrets](#create-the-secrets) |
| **GitHub PAT** for the deck guides | Secret `mtg-decks-git`, key `GH_PAT`, namespace `mtg-engine` | [Create the Secrets](#create-the-secrets) |
| **Harbor pull secret** | Secret `regcred`, namespace `mtg-engine` | [The registry pull secret](#the-registry-pull-secret) |

The full ordered sequence is under [Apply](#apply).

## Namespace: `mtg-engine`, not `mtg`

A namespace called `mtg` already exists in this cluster (57 days old, empty at
the time of writing) and belongs to something else. These manifests deliberately
use a new `mtg-engine` namespace and never touch `mtg`.

## Images: Harbor, and the `__TAG__` placeholder

The three app containers and the initContainer pull from Harbor:

```
harbor.szymonrichert.pl/containers/magic-cards-model-api:__TAG__     # game, driver
harbor.szymonrichert.pl/containers/magic-cards-model-agent:__TAG__   # agent, guides init
```

**`__TAG__` is a literal placeholder, not a tag that exists.** The intended way
to deploy is:

```sh
mise run k8s:deploy
```

which substitutes `__TAG__` with `git rev-parse --short HEAD` and pipes the
result to `kubectl apply -f -`. Nothing rendered is written back into the repo.

If you are not using mise, do the same thing by hand:

```sh
TAG=$(git rev-parse --short HEAD)
sed "s/__TAG__/${TAG}/g" deploy/k8s/*.yaml | kubectl apply -f -
```

`imagePullPolicy: IfNotPresent` throughout: a short commit sha is immutable, so
one tag always means one image and `Always` would only add a registry round
trip per container start. If you ever push a mutable tag (`latest`), change the
policy with it.

### The registry pull secret

Harbor is private, so the Pod carries `imagePullSecrets: [{name: regcred}]`.
`imagePullSecrets` are namespaced, and the new namespace starts without one.
Every other namespace in this cluster already has a `regcred`
docker-registry Secret for `harbor.szymonrichert.pl` (robot account
`robot$kubernetes`). The easy path is to copy one:

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
`docker-compose.yml` says the same thing.

If you would rather use a real `sk-ant-...` key:

```sh
kubectl -n mtg-engine create secret generic mtg-agent \
  --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
```

Set exactly one. The agent container declares **both** env vars from **the same
Secret**, each with `optional: true` on the `secretKeyRef`, so whichever key you
did not create is simply absent instead of blocking Pod start. The wrapper
prefers the token and clears the other so they cannot shadow each other.

### The GitHub PAT for the deck guides

```sh
kubectl -n mtg-engine create secret generic mtg-decks-git \
  --from-literal=GH_PAT="$GH_PAT"
```

Scope: read access to **one private repo**, `szymonrychu/mtg-decks`, and nothing
else. A classic PAT with the `repo` scope works; a fine-grained PAT scoped to
that single repository with `Contents: Read-only` is tighter and preferred.

`10-secret.template.yaml` exists for people who want either declarative; copy it
to `10-secret.yaml` (gitignored) and fill in the placeholders.

### The basic-auth Secret is already in git, on purpose

`70-ingress.yaml` carries `play-magic-auth` with a real, verified bcrypt hash
for user `play` / password `magic`. That password was asked for explicitly and
is deliberately weak, so there is nothing to protect by hiding the hash, and
committing it makes `kubectl apply` one step. It is a speed bump against
crawlers, not security. See the blunt comment on the Secret itself.

## Apply

In order. Steps 1-3 are the ones you only do once per cluster.

```sh
# 1. namespace
kubectl apply -f deploy/k8s/00-namespace.yaml

# 2. registry pull secret (copied from an existing namespace)
kubectl -n default get secret regcred -o yaml \
  | sed 's/namespace: default/namespace: mtg-engine/' \
  | kubectl apply -f -

# 3. the two credentials
kubectl -n mtg-engine create secret generic mtg-agent \
  --from-literal=CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN"
kubectl -n mtg-engine create secret generic mtg-decks-git \
  --from-literal=GH_PAT="$GH_PAT"

# 4. everything else, with the image tag substituted
mise run k8s:deploy
#   or, without mise:
#   sed "s/__TAG__/$(git rev-parse --short HEAD)/g" deploy/k8s/*.yaml | kubectl apply -f -

# 5. watch it come up
kubectl -n mtg-engine get pod mtg-stack -w
```

The Pod goes `Init:0/1` (cloning the guides) then `Running` but **not ready**
until claude has booted, which takes up to about two minutes.

## Reach it

| what | where |
|---|---|
| play | `https://play-magic.szymonrichert.pl/play/` - user `play`, password `magic` |
| which deck the driver identified | port-forward, then `http://127.0.0.1:8090/status` |
| agent turns, probe, interrupt | port-forward, then `http://127.0.0.1:8080/v1/...` |

```sh
kubectl -n mtg-engine port-forward pod/mtg-stack 8000 8080 8090
```

`*.szymonrichert.pl` resolves to the LAN IP at home and to the public IP from
outside, so the host works from both with no extra DNS record - and is really
on the internet, which is why only 8000 is routed and why it has basic auth in
front of it. cert-manager issues the certificate from the `letsencrypt-prod`
ClusterIssuer into the `play-magic-tls` Secret on first apply; the first request
can 502 or serve a self-signed cert for the minute that takes.

## Logs

```sh
kubectl -n mtg-engine logs mtg-stack -c guides           # the deck-guide clone
kubectl -n mtg-engine logs mtg-stack -c agent -f         # session boot, turns, a dead claude
kubectl -n mtg-engine logs mtg-stack -c agent --previous # the match that just ended
kubectl -n mtg-engine logs mtg-stack -c driver -f        # the match lifecycle
kubectl -n mtg-engine logs mtg-stack -c game -f | grep "PROBLEM REPORTED"
```

`--previous` matters here more than usual: the agent container is *replaced*
after every match, so the log of the game you just played is the previous
container's.

## What changed versus compose, and why

### Every service name became `127.0.0.1`

One Pod is one network namespace.

| compose | here |
|---|---|
| `DRIVER_URL=http://driver:8090` (game) | `http://127.0.0.1:8090` |
| `GAME_API=http://game:8000` (driver) | `http://127.0.0.1:8000` |
| `WRAPPER_API=http://agent:8080` (driver) | `http://127.0.0.1:8080` |
| `DRIVER_SELF_URL=http://driver:8090` (driver) | `http://127.0.0.1:8090` |
| `mcp.base.json` `--api http://game:8000` | `--api http://127.0.0.1:8000` |

The MCP one is the easy one to miss. The `mtg` MCP server is not a container:
the agent spawns it itself over stdio, inside the agent container, and it
reaches the game over HTTP using the `--api` URL baked into `mcp.base.json`.
Left alone it would try to resolve the DNS name `game` and fail.

**How the corrected file is supplied:** the wrapper reads whatever
`BASE_MCP_PATH` points at (`bootstrap.merge_mcp`), then merges it into the
workspace `.mcp.json`. So `20-configmap-mcp.yaml` carries a k8s copy, mounted
at `/etc/mtg/mcp.base.json`, and the agent container sets
`BASE_MCP_PATH=/etc/mtg/mcp.base.json`. `deploy/mcp.base.json` in the repo is
untouched and compose keeps using it. That was preferred over a ConfigMap
overmount of `/app/deploy/mcp.base.json`: same effect, but pointing an env var
at a new file is visible in the Pod spec, while an overmount silently replaces
a file that looks unchanged in the image.

### Spellslinger MCP goes in-cluster, not through the public ingress

Still enabled, different address:

```
https://mcp.szymonrichert.pl/mcp
  ->  http://spellslinger-mcp-http.spellslinger-development.svc.cluster.local:8080/mcp
```

The public ingress for that server (`magic-cards-model/deploy/mcp-ingress.yaml`)
carries `whitelist-source-range` limited to the LAN ranges. A Pod's source
address as seen by that ingress is a cluster pod IP, so the old URL was both a
pointless round trip out and back and a good way to eat a 403. The Service
address skips the ingress entirely. Plain HTTP is fine: it never leaves the
cluster network. Verified present: Service `spellslinger-mcp-http`, port 8080,
namespace `spellslinger-development`.

### A port collision that compose was hiding

The wrapper's internal listener is `INTERNAL_ADDR`, and compose sets it to
`127.0.0.1:8090` - **the same port the driver serves on**. Separate network
namespaces made that fine. In one Pod it is a bind clash. Here the internal
listener moves to `127.0.0.1:8099`, and `CCW_INTERNAL_URL` (the URL the stop
hook posts a finished turn to) moves with it. Port map:

```
8000 game      8080 agent      8090 driver      8099 agent-internal (loopback)
```

Compose is not changed; 8090 is still correct there.

### Non-root, and it must stay that way

claude refuses bypass-permissions mode as root ("cannot be used with root/sudo
privileges for security reasons") and exits 1, which surfaces as `claude exited
code=1`. `Dockerfile.agent` creates a uid/gid 1000 `agent` user for exactly
this reason. The Pod's `securityContext` is `runAsNonRoot: true`,
`runAsUser/runAsGroup: 1000`, `fsGroup: 1000` to match. If you hit a permission
error on a volume, fix the volume - do not set `runAsUser: 0`.

Note this also runs the *game* and *driver* containers as uid 1000; their image
has no `USER` line and runs as root under compose. Their writable paths are
mounted volumes (`fsGroup` covers those), so this should be fine - but it is
one of the unverified items below.

### `restartPolicy: Always`

The agent exits deliberately at the end of every match - one match per
container, then a fresh claude session - and it exits **zero** (a clean return
out of `wrapper/__main__`). `OnFailure` only restarts non-zero exits, so it
would leave the stack dead after game one. `Never` is worse. `Always` restarts
the agent container **in place**: a container restart is not a Pod restart, so
the game and driver containers keep running, keep their listeners, and keep
their state across the agent's recycle. That is what compose's
`restart: unless-stopped` did.

One difference to expect: Kubernetes applies an exponential restart backoff
(up to 5 minutes) to a container that keeps exiting, regardless of exit code,
and a container recycled once per match will accumulate it. If the gap between
matches grows, `CrashLoopBackOff` on the agent is the reason - it is the
restart backoff doing its job, not a failure.

### Probes

- **game**, **driver**: `/api/health` and `/healthz`, readiness and liveness.
- **agent**: a `startupProbe` on `/healthz` owns the whole boot window - 10s x
  36 = 6 minutes of grace. This is load-bearing: the wrapper boots claude
  *before* it starts serving HTTP at all, so nothing answers on 8080 for up to
  two minutes, and a bare liveness probe would kill the container mid-boot
  forever. Liveness only starts once startup has passed. Readiness uses
  `/readyz`, which 503s until claude has actually booted; liveness deliberately
  does **not** use `/readyz`, because a dead claude already takes the container
  down by exiting, and that is the designed recycle path.

### Env carried over from compose

Everything compose sets is set here, including the four that an earlier draft of
these manifests had dropped: `ACCESS_LOG_QUIET=1` on all three containers (it
keeps the ~1.2s UI polls and the driver's `/status` polls out of the access
log), and `AGENT_LOG_LEVEL=tools` on the agent (what the agent prints off its
own live transcript). `MODEL=sonnet` and `GUIDES_ROOT=/guides` were already
there. Compose reads these from `.env` with defaults; here they are the
defaults, written out literally.

### Resources

Conservative, and requests deliberately well under limits:

| container | requests | limits | why |
|---|---|---|---|
| guides (init) | 50m / 64Mi | 500m / 256Mi | one shallow git clone |
| game | 100m / 256Mi | 1 CPU / 1Gi | one uvicorn app serving one game |
| agent | 250m / 1Gi | 2 CPU / 3Gi | node plus an interactive claude holding a long transcript; a turn is bursty |
| driver | 50m / 128Mi | 500m / 512Mi | bookkeeping and HTTP calls |

Nothing here was measured under load. If the agent gets OOMKilled mid-match,
raise its memory limit first.

## Storage choices, honestly

### Deck guides: an initContainer that clones them, every Pod start

There is no bind mount in Kubernetes. An earlier draft used
`hostPath: /srv/mtg-decks`, which worked but **pinned the Pod to one node**:
you had to copy the guides onto that node by hand and pin the Pod with a
`nodeSelector`, or the scheduler would place it somewhere `/srv/mtg-decks` did
not exist. `hostPath` is also restricted by policy on a lot of clusters.

That is gone. The guides now live in an `emptyDir` that an initContainer fills
by cloning `github.com/szymonrychu/mtg-decks` before any app container starts.
Strictly better on two axes: **no node pinning at all**, and the guides are
**current at every restart** rather than being whatever was last copied onto
one node. The `nodeSelector` and the hostPath are both deleted.

Details worth knowing:

- **The image is the agent image**, `magic-cards-model-agent:__TAG__`.
  `Dockerfile.agent` apt-installs `git`; the game/driver `Dockerfile` installs
  nothing beyond `uv` and has no git, so it could not do this job. Reusing an
  image the kubelet is already pulling for the agent container avoids a second
  registry and a second pull.
- **It runs as uid 1000**, under the Pod `securityContext` like everything
  else. The `emptyDir` is chowned to `fsGroup` 1000 with the setgid bit, so
  uid 1000 can write into it. Nothing in the clone needs root.
- **The PAT does not stay in the repo it cloned.** The token has to be in the
  clone URL (`https://x-access-token:${GH_PAT}@github.com/...`), and `git clone`
  persists whatever URL it was given into `.git/config` as
  `remote.origin.url` - which would then sit inside `/guides` for the life of
  the Pod, in cleartext, readable by a claude session that can read files. So
  the remote is immediately reset to the tokenless URL, and the script greps
  `.git/config` afterwards to prove the token is gone. The clone is `--depth 1`,
  so there is no fetch history carrying it either.
- **A failed clone fails the Pod, loudly.** `set -e` and a non-zero exit leave
  the Pod in `Init:Error` / `Init:CrashLoopBackOff`, plainly visible in
  `kubectl get pod`. It never starts with a silently empty `/guides`, which
  would look healthy and play badly. `GIT_TERMINAL_PROMPT=0` makes a bad or
  expired PAT fail immediately instead of hanging on a credential prompt.

The cost, versus the hostPath it replaced: you can no longer edit a guide on
the node and have the agent see it next match. Push to the repo and restart the
Pod instead. For a repo you already keep in git, that is the right loop.

### Agent memory: a PVC, and it is the important one

`/workspace/memory` is the only thing that crosses a match boundary. The agent
container is replaced after every game; the notes it wrote are the entire
mechanism for cross-game learning. It gets `mtg-agent-memory`, a 1Gi
ReadWriteOnce PVC.

### Scryfall cache: a PVC too

`/app/.scryfall-cache`, 2Gi. Not correctness-critical, purely cold-start cost -
a cold cache measured 34.5s for a single decklist, and without a volume every
Pod recreate pays it again.

### StorageClass

Neither PVC names a `storageClassName`, so both take the cluster default. In
this cluster that is **`rook-ceph-rwx`** (CephFS), confirmed with
`kubectl get sc` - both claims bind to it as written, no edit needed. On a
cluster with no default they would stay `Pending` forever and the Pod would
never start; `30-storage.yaml` explains the fix.

## What was actually validated

Run on these manifests, with `__TAG__` substituted with a dummy value into a
temp directory (nothing rendered was left in the repo, and nothing was applied
for real):

- `kubectl apply --dry-run=client -f <rendered>/` - all 10 objects accepted.
- `kubectl apply --dry-run=server -f <rendered>/` against the live cluster on
  the `kubernetes-admin@kubernetes` context - all 10 accepted. Server dry-run
  runs real schema validation, defaulting and admission. Nothing was created:
  server dry-run does not persist.
  One wrinkle worth recording: a server dry-run of the namespaced objects
  fails with `namespaces "mtg-engine" not found`, because the namespace's own
  dry-run creation does not persist for the objects that follow it. So the
  namespaced objects were re-rendered into the existing `mtg` namespace purely
  to get them admitted by the API server, and all 10 passed that way. That is
  a validation artifact only - nothing was written to `mtg`, and the committed
  manifests all say `mtg-engine`.
- Server-side defaulting confirmed both PVCs resolve to
  `storageClassName: rook-ceph-rwx`.
- The initContainer script parses clean under `sh -n`.
- The committed basic-auth hash was generated with `htpasswd -nbB play magic`
  and then **verified**: `htpasswd -vb` accepts `magic` and rejects a wrong
  password.

Cluster facts checked directly, not assumed:

- ClusterIssuer `letsencrypt-prod` exists and is Ready.
- Service `spellslinger-mcp-http` exists in `spellslinger-development` on port
  8080.
- Default StorageClass is `rook-ceph-rwx`.
- Secret `regcred` exists in `default` and is `kubernetes.io/dockerconfigjson`.
- Namespace `mtg` exists (57 days old) and `mtg-engine` does not.

`kubeval` and `kubectl-validate` are not installed here; no other linter ran.

## What remains unverified

Everything that needs the workload to actually run:

- **The Pod has never been applied for real.** No container has started from
  these manifests. Schema-valid is not working.
- **The Harbor images may not exist at the tag you deploy.** Nothing here built
  or pushed `magic-cards-model-api` or `magic-cards-model-agent`, and no tag was
  checked against the registry. If the CI that pushes them has not run for your
  commit, you get `ImagePullBackOff`.
- **The `regcred` copy has not been done.** The copy command is transcribed, not
  executed; the `mtg-engine` namespace does not exist yet, so nothing could be
  copied into it.
- **The guides clone has never run.** The PAT, the repo's private-ness, the
  emptyDir ownership under `fsGroup`, and the `.git/config` grep are all
  reasoned from how git and the kubelet behave, not observed. If the guide repo
  has submodules, `--depth 1` without `--recurse-submodules` will not bring
  them.
- **The ingress has never served a request.** No certificate has been issued for
  `play-magic.szymonrichert.pl`, no basic-auth challenge has been seen in a
  browser, and the claim that nothing in the annotations breaks a 1.2s poll is
  reasoned (no rate limit, no connection limit, generous timeouts) rather than
  measured.
- Running **game and driver as uid 1000** (they run as root under compose) is
  untested. If either needs to write a path that is not a mounted volume, it
  will fail with EACCES and want either an extra volume or a per-container
  `runAsUser: 0` - and note that turning off `runAsNonRoot` at the Pod level
  would break the agent, so any such fix belongs on the individual container.
- The **8099 move** is untested end to end. It is consistent across
  `INTERNAL_ADDR` and `CCW_INTERNAL_URL`, and nothing else in the repo
  hardcodes 8090 for the wrapper, but no turn has been completed through it.
- The **MCP re-points** are untested: no MCP tool call has been made against
  `http://127.0.0.1:8000` or against the spellslinger Service DNS name from
  inside the agent container.
- **Probe timings** are reasoned from the code and the documented ~2 minute
  boot, not measured. If claude boots slower on your node, raise the
  startupProbe `failureThreshold`.
- **Resource requests and limits** are estimates. None were profiled.
- The **restart-per-match cycle** has not been observed under Kubernetes; the
  backoff behaviour described above is expected, not seen.
- **PVC binding and fsGroup ownership** - not exercised.
- `mise run k8s:deploy` is written by a sibling change in this repo and was not
  run from here.
