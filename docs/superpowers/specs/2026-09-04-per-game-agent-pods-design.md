# Per-game agent pods - design

Date: 2026-09-04. Repo: magic-cards-model-api. Research brief:
`docs/superpowers/research/2026-09-04-mtgo-like-play-ui-and-per-game-agent-pods.md`.

## Goal

Let several games run at once, each AI seat played by its own agent pod
scheduled on demand through the Kubernetes API, with the agent's model and
thinking settings chosen when the game is created. Compose and the two
existing AI seat modes keep working.

Companion spec: `2026-09-04-play-ui-action-protocol-design.md` owns the
in-game screen. This spec owns the lobby, game creation, AI seat lifecycle,
and the cluster manifests.

## What constrains this today (verified)

- The store already serves several games with per-game locks
  (`tests/test_multi_game.py`).
- Single-game assumptions: the driver holds one global assignment and
  answers 409 to a second; the MCP server picks `games[0]` when `--game` is
  absent; `lobby.reset_previous_games` deletes earlier lobby games on every
  new one; the UI stores one game in `localStorage`.
- The only agent setting is `MODEL` becoming `--model`; there is no effort
  or thinking setting. Subprocess mode hardcodes `--model sonnet`.
- The cluster deploys one three-container Pod (game, agent, driver) with no
  ServiceAccount or RBAC; the memory PVC is RWO.

## Decisions

Approved by the user on 2026-09-04: lobby lists live games and no longer
resets them; per-AI-player model, effort and max thinking tokens; a live
game cap; one Job per AI seat carrying agent plus driver sidecar;
`ownerReferences` to the API pod; watch plus 60 s reconcile; RWX memory PVC;
driver self-dispatches from env and exits when the match ends; wrapper gains
`--effort` and `MAX_THINKING_TOKENS`; subprocess and wrapper modes stay;
ServiceAccount plus namespaced Role; NetworkPolicy on agent pods; the stack
Deployment becomes API-only.

## Game creation and lobby

### Request

`POST /api/ui/games`:

```json
{
  "players": [
    {"username": "you", "decklist_text": "...", "ai": false},
    {"username": "bot", "decklist_text": "...", "ai": true,
     "agent": {"model": "sonnet", "effort": "high", "max_thinking_tokens": null}}
  ],
  "seed": null
}
```

- `agent` is allowed only with `ai: true`. `model` is any non-empty string;
  the UI offers `sonnet`, `opus`, `haiku`, `fable` as suggestions. `effort`
  is one of `low`, `medium`, `high`, `xhigh`, `max`, default `high`.
  `max_thinking_tokens` is a non-negative integer or null (null leaves
  Claude Code's default; 0 disables thinking where the model allows it).
- Defaults when `agent` is absent: model from `AGENT_DEFAULT_MODEL` (env,
  default `sonnet`), effort `high`, thinking tokens null.
- `MAX_LIVE_GAMES` (env, default 4) counts lobby games that are not over;
  exceeding it is a 409 naming the count.
- The AI seat starts as part of creation; the separate
  `POST .../ai-seat/start` remains for retries.
- The response adds `agent` per AI player and `lobby_url` per seat. The
  previous `reset` field goes away with the reset itself.

### Store

`store` keeps `agent_settings[game_id][username]`. `GET /api/games/{id}`
shows `agent: {model, effort, max_thinking_tokens}` on the AI player's block
to every viewer. `GET /api/ui/games` lists live lobby games with players,
turn, step, winner, agent settings, and seat uids the caller already owns
(from a `uids` query parameter), never other seats' uids.

### Lobby screen

- A table of live games: opponent name, turn and step, agent model and
  effort, seat state (pending, playing, over), and Open and End buttons.
- New game: the current deck pickers plus, for the AI side, a model field
  with suggestions, an effort select, and an optional thinking tokens field.
- `localStorage` holds a list of `{gameId, uid, aiUid}` entries instead of
  one.
- End game: `DELETE /api/ui/games/{id}` with the seat uid, which stops the
  AI seat (subprocess terminate, driver release, or Job delete) and deletes
  the game.

## AI seat modes

`AI_SEAT_MODE` gains `kubernetes`. The registry keeps one runner per seat
uid; each mode's runner reports the same status shape.

- `subprocess`: argv becomes `claude --print --model <model> --effort
  <effort> ...` with `MAX_THINKING_TOKENS` in the child env when set.
- `wrapper`: unchanged transport. The dispatch payload gains `agent`; the
  driver passes `model`, `effort` and thinking tokens to the wrapper on
  `/v1/session` start. The single-agent 409 stays, since compose runs one
  agent.
- `kubernetes`: below.

## Scheduler (`k8s_seats.py`)

- Dependency: `kubernetes` (sync client), configured with
  `load_incluster_config()`, falling back to kubeconfig for local runs.
  Calls run from plain `def` routes, so FastAPI's threadpool absorbs them.
- One Job per AI seat: name `mtg-seat-<game_id[:8]>-<uid[:8]>`, labels
  `app.kubernetes.io/name=mtg-agent`, `mtg/game-id`, `mtg/seat-uid`,
  annotations with player, model, effort. Spec: `backoffLimit: 0`,
  `activeDeadlineSeconds` from `AGENT_JOB_DEADLINE_SECONDS` (default
  14400), `ttlSecondsAfterFinished` 600, template `restartPolicy: Never`,
  `ownerReferences` to the API's own Pod (`POD_NAME`, `POD_UID` from the
  downward API), `automountServiceAccountToken: false`, `imagePullSecrets`
  regcred, the pod security context used today plus `seccompProfile:
  RuntimeDefault`, `allowPrivilegeEscalation: false`, drop ALL.
- Containers, same images as today:
  - `agent` (wrapper image): env as today plus `MODEL`, `EFFORT`,
    `MAX_THINKING_TOKENS`, `BASE_MCP_PATH=/etc/mtg/mcp.json`; the MCP config
    is written by an `mcp-config` initContainer into an emptyDir at
    `/etc/mtg/mcp.json`, from a `--game <id>`/`--api http://<api-service>:8000`
    JSON literal baked in by `k8s_seats.mcp_config` and passed to that
    initContainer as the `MCP_JSON` env var - not a ConfigMap, since a per-Job
    ConfigMap would need `configmaps` RBAC and there is nothing left for one
    to hold once the config lives in the Job spec itself; secret `mtg-agent`
    via `envFrom`; memory PVC at `/workspace/memory`; guides from an
    initContainer as today.
  - `driver` (api image, `python -m mtg_agent.driver`): env `GAME_API`,
    `WRAPPER_API=http://127.0.0.1:8080`, `DRIVER_SELF_URL`,
    `DRIVER_ASSIGNMENT_JSON` holding `{uid, game_id, player, api_base,
    agent}`.
- Watch: a daemon thread runs `watch.Watch().stream(list_namespaced_job,
  label_selector=...)`, updating seat status on every event and relisting on
  410. Reconcile: every 60 s, and at startup, list Jobs by label; delete
  Jobs whose game is gone from the store; mark seats whose Job is gone as
  `error` with reason `job missing`; log Jobs that are Failed with the pod's
  last container state.
- Status: `GET .../ai-seat/status` merges Job phase, pod phase and the
  driver's `/status` fetched at the pod IP from `status.podIP`. States:
  `pending` (Job created, pod not ready), `thinking` (driver reports a turn
  active), `idle`, `gone` (Job complete), `error` (Failed, missing, or
  ImagePullBackOff surfaced by name).
- Stop: `delete_namespaced_job` with `propagation_policy="Background"`
  after a best-effort `POST /release` to the driver.

## Driver per pod

- At boot, if `DRIVER_ASSIGNMENT_JSON` is set the driver dispatches itself
  with the same code path `/dispatch` uses, then serves `/status`,
  `/turn-done`, `/release` as today. `/dispatch` stays for compose.
- After the reflection turn's `/turn-done`, `retire` runs and the driver
  process exits 0 once the wrapper's `DELETE /v1/session` returns, so both
  containers finish and the Job completes. On `/release` the same happens
  without a reflection.
- The driver's `api_base` for the agent's brief is `GAME_API`, not the
  browser URL, because the agent reaches the API through the Service.

## Wrapper settings

- `Settings` gains `effort` (`EFFORT`) and `max_thinking_tokens`
  (`MAX_THINKING_TOKENS`). `claude_args` appends `--effort <effort>` when
  set; `MAX_THINKING_TOKENS` is passed through to the child env when set.
- `GET /v1/session` reports `model`, `effort`, `max_thinking_tokens`; the
  token metric gains an `effort` label.

## Memory

- The `mtg-agent-memory` PVC becomes ReadWriteMany on `rook-ceph-rwx`,
  mounted by every agent pod. Per-game files under `memory/<deck>/games/`
  never collide; `vs-<archetype>.md` and `MEMORY.md` are last-writer-wins,
  accepted for now.
- A PVC's access mode is immutable once bound, so the live cluster's
  existing claim - bound `ReadWriteOnce` from the single-Pod topology - must
  be recreated, not patched, to pick up `ReadWriteMany`; documented in
  `deploy/k8s/README.md`.

## Cluster manifests

- `40-rbac.yaml`: ServiceAccount `mtg-api`; Role over `batch/jobs` (create,
  get, list, watch, delete), `pods` (get, list, watch), `pods/log` (get). No
  `configmaps` rule - the MCP config is written by the Job's own
  `mcp-config` initContainer from an env var (see "Scheduler" above), so the
  API never touches a ConfigMap; RoleBinding.
- `50-deployment.yaml`: `mtg-stack` becomes the API alone with
  `serviceAccountName: mtg-api`, env `AI_SEAT_MODE=kubernetes`,
  `AGENT_NAMESPACE`, `AGENT_IMAGE`, `API_IMAGE`, `API_SERVICE_URL`,
  `POD_NAME`/`POD_UID`/`POD_NAMESPACE` from the downward API, `MAX_LIVE_GAMES`.
  `POD_NAMESPACE` guards `ownerReferences`: a Job's owner reference must name
  an object in the Job's own namespace, so `k8s_seats.build_job` sets
  `ownerReferences` to the API's own Pod only when `owner_pod_namespace`
  (from `POD_NAMESPACE`) equals the Job's own namespace, and omits it
  otherwise rather than emit a cross-namespace reference that would either
  be rejected or garbage-collect the wrong Pod. The agent and driver
  containers and the guides initContainer move into the Job template, which
  lives in `k8s_seats.py` as a Python structure rendered from env, with the
  image tags substituted the same way as today.
- `45-networkpolicy.yaml`: agent pods accept ingress only from the API pod;
  egress to kube-dns, the API Service, the spellslinger MCP Service, and
  TCP 443 to any address for the Anthropic API.
- `60-service.yaml` drops the 8080 and 8090 ports.
- `30-storage.yaml`: memory PVC `ReadWriteMany`.
- `deploy/k8s/README.md` documents the new procedure, and that Jobs are
  visible with `kubectl get jobs -l app.kubernetes.io/name=mtg-agent`.

## Testing

- pytest: `NewUiGame` validation of `agent`; defaults; the live-game cap;
  no reset on a second game; `GET /api/ui/games` scoping of seat uids;
  End game stops the seat and deletes the game; subprocess argv carries
  model, effort and thinking tokens; wrapper `claude_args` with effort;
  driver self-dispatch from env and exit after retire (the existing driver
  tests' fake wrapper).
- Scheduler tests against a fake `BatchV1Api`/`CoreV1Api` are the one
  place a fake is acceptable, because the alternative is a cluster in CI;
  they cover the Job body, labels, ownerReferences, watch event handling,
  reconcile deletions, and status mapping.
- Manual: `mise run k8s:deploy` to the cluster, two games at once from two
  browser tabs, both agents playing; End game deletes its Job; API restart
  garbage-collects Jobs.

## Out of scope

Persisting games across API restarts; a warm pod pool; a DaemonSet image
pre-puller; per-game memory volumes; horizontal scaling of the API.
