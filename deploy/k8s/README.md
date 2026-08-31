# The agent stack on Kubernetes - one Pod, three containers

**Status: a draft. These manifests have never been applied to a running
cluster.** They validate (see [Validation](#what-was-actually-validated)) and
the wiring below was derived from `docker-compose.yml` and the code, not
guessed - but nothing here has served a game. Treat the first `kubectl apply`
as the real test. `docker compose up` remains the supported way to run this.

This is the compose topology with the network collapsed onto loopback: the
game, the agent and the driver as three containers in a single Pod. Plain
YAML, no Helm, no Kustomize, no operator.

| file | what it is |
|---|---|
| `00-namespace.yaml` | the `mtg` namespace |
| `10-secret.template.yaml` | template for the `ANTHROPIC_API_KEY` Secret - placeholder only, never a real key |
| `20-configmap-mcp.yaml` | `mcp.base.json` with the `mtg` MCP server re-pointed at `127.0.0.1:8000` |
| `30-storage.yaml` | PVCs for the agent's memory and the Scryfall cache |
| `50-pod.yaml` | the Pod: game + agent + driver, ports, env, probes, resources |
| `60-service.yaml` | NodePort Service for 8000 (play), 8090 (driver status), 8080 (agent API) |

## Build and load the images

Both Dockerfiles take the **parent directory** as build context, because the
engine at `../magic-cards-model` is a path dependency. Run these from this
repo's root:

```sh
docker build -f Dockerfile       -t mtg-game:dev  ..
docker build -f Dockerfile.agent -t mtg-agent:dev ..
```

`imagePullPolicy: IfNotPresent` and the `:dev` tags mean the kubelet uses a
locally present image and never contacts a registry. That only works if the
image is present *on the node*. Pick whichever applies:

- **kind**: `kind load docker-image mtg-game:dev mtg-agent:dev`
- **k3s/containerd**: `docker save mtg-game:dev mtg-agent:dev | sudo k3s ctr images import -`
- **a real cluster**: push to a registry and change the two `image:` lines.

## Create the Secret

Do not put the key in a file. Create it imperatively:

```sh
kubectl -n mtg create secret generic mtg-agent \
  --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
```

`10-secret.template.yaml` exists for people who want it declarative; copy it to
`10-secret.yaml` (gitignored) and fill in the placeholder.

## Put the deck guides on the node

`50-pod.yaml` mounts `hostPath: /srv/mtg-decks` read-only at `/guides` in all
three containers. Copy your `~/Documents/mtg-decks` there on the node that will
run the Pod, then uncomment `nodeSelector` in the Pod and name that node.

## Apply

```sh
kubectl apply -f deploy/k8s/00-namespace.yaml
kubectl -n mtg create secret generic mtg-agent --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
kubectl apply -f deploy/k8s/20-configmap-mcp.yaml -f deploy/k8s/30-storage.yaml
kubectl apply -f deploy/k8s/50-pod.yaml -f deploy/k8s/60-service.yaml
kubectl -n mtg get pod mtg-stack -w
```

The Pod is not ready until claude has booted, which takes up to about two
minutes.

## Reach it

| what | where |
|---|---|
| play | `http://<node-ip>:30800/play/` |
| which deck the driver identified | `http://<node-ip>:30890/status` |
| agent turns, probe, interrupt | `http://<node-ip>:30880/v1/...` |

Or skip the node ports entirely:

```sh
kubectl -n mtg port-forward pod/mtg-stack 8000 8080 8090
```

## Logs

```sh
kubectl -n mtg logs mtg-stack -c agent -f       # session boot, turns, a dead claude
kubectl -n mtg logs mtg-stack -c agent --previous  # the match that just ended
kubectl -n mtg logs mtg-stack -c driver -f      # the match lifecycle
kubectl -n mtg logs mtg-stack -c game -f | grep "PROBLEM REPORTED"
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
| `mcp.base.json` -> `--api http://game:8000` | `--api http://127.0.0.1:8000` |

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

### Resources

Conservative, and requests deliberately well under limits:

| container | requests | limits | why |
|---|---|---|---|
| game | 100m / 256Mi | 1 CPU / 1Gi | one uvicorn app serving one game |
| agent | 250m / 1Gi | 2 CPU / 3Gi | node plus an interactive claude holding a long transcript; a turn is bursty |
| driver | 50m / 128Mi | 500m / 512Mi | bookkeeping and HTTP calls |

Nothing here was measured under load. If the agent gets OOMKilled mid-match,
raise its memory limit first.

## Storage choices, honestly

### Deck guides: `hostPath`, and what that costs

There is no bind mount in Kubernetes. The three candidates:

- **`hostPath`** (chosen). Simple, zero copying, and you keep editing guides
  in place and having the agent see them next match. **The cost: it pins the
  Pod to one node.** You must put the guides on that node and pin the Pod with
  `nodeSelector`, or the scheduler will happily place the Pod on a node where
  `/srv/mtg-decks` does not exist - and with `type: Directory` the Pod then
  fails to start rather than silently mounting an empty dir. `hostPath` is also
  a privileged-ish thing that many clusters restrict by policy. For a
  single-node homelab this is the right trade; for anything multi-tenant it is
  not.
- **ConfigMap**: does not fit. A ConfigMap is capped near 1MiB total, and the
  gameplan guides run to ~500 lines each plus per-matchup JSON. It would break
  the moment the guides grew.
- **Baking them into the image**: works, survives scheduling anywhere, and
  loses live editing - every guide edit becomes an image rebuild and a Pod
  recreate. That is the wrong loop for something you tune between games.

An honest fourth option if you outgrow the single node: put the guides on an
RWX PVC (NFS, CephFS) and sync them there. More moving parts, no node pinning.

### Agent memory: a PVC, and it is the important one

`/workspace/memory` is the only thing that crosses a match boundary. The agent
container is replaced after every game; the notes it wrote are the entire
mechanism for cross-game learning. It gets `mtg-agent-memory`, a 1Gi
ReadWriteOnce PVC.

### Scryfall cache: a PVC too

`/app/.scryfall-cache`, 2Gi. Not correctness-critical, purely cold-start cost -
a cold cache measured 34.5s for a single decklist, and without a volume every
Pod recreate pays it again.

### If the cluster has no default StorageClass

Neither PVC names a `storageClassName`, so both take the cluster default. With
no default, both stay `Pending` forever and the Pod never starts (`kubectl -n
mtg describe pvc mtg-agent-memory` says so explicitly). Either mark a class
default:

```sh
kubectl annotate storageclass <name> storageclass.kubernetes.io/is-default-class=true
```

or uncomment `storageClassName:` on both claims in `30-storage.yaml`.

## What was actually validated

Run on these manifests:

- `kubectl apply --dry-run=client -f deploy/k8s/` - all seven objects accepted.
- `kubectl apply --dry-run=server -f deploy/k8s/` against the live cluster on
  the `kubernetes-admin@kubernetes` context - all seven accepted. Server dry-run
  runs real schema validation, defaulting and admission, and it allocates-checks
  the NodePorts, so 30800/30880/30890 were free at that moment. Nothing was
  created: server dry-run does not persist.

Two things that dry-run surfaced and you should know:

- the `mtg` namespace **already exists** in that cluster (57 days old). Applying
  `00-namespace.yaml` will patch it, not create it. Check what is already in
  there before you apply the rest.
- the cluster's default StorageClass is `rook-ceph-rwx`, so both PVCs would
  bind to it as written.

`kubeval` and `kubectl-validate` are not installed here; no other linter ran.

## What remains unverified

Everything that needs the workload to actually run:

- **The Pod has never been applied for real.** No container has started from
  these manifests. Schema-valid is not working.
- The images were **not built or loaded** as part of this; the two `docker
  build` commands above are transcribed from `docker-compose.yml`, not
  executed here.
- Running **game and driver as uid 1000** (they run as root under compose) is
  untested. If either needs to write a path that is not a mounted volume, it
  will fail with EACCES and want either an extra volume or a per-container
  `runAsUser: 0` - and note that turning off `runAsNonRoot` at the Pod level
  would break the agent, so any such fix belongs on the individual container.
- The **8099 move** is untested end to end. It is consistent across
  `INTERNAL_ADDR` and `CCW_INTERNAL_URL`, and nothing else in the repo
  hardcodes 8090 for the wrapper, but no turn has been completed through it.
- The **MCP re-point** is untested: no MCP tool call has been made against
  `http://127.0.0.1:8000` from inside the agent container.
- **Probe timings** are reasoned from the code and the documented ~2 minute
  boot, not measured. If claude boots slower on your node, raise the
  startupProbe `failureThreshold`.
- **Resource requests and limits** are estimates. None were profiled.
- The **restart-per-match cycle** has not been observed under Kubernetes; the
  backoff behaviour described above is expected, not seen.
- **hostPath, PVC binding, fsGroup ownership** - none exercised. The guides
  path `/srv/mtg-decks` is a placeholder that exists on no node yet.
- The NodePorts passed a dry-run allocation check once; that is a point-in-time
  result, not a reservation.
