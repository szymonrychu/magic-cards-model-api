# A continuous Claude Code agent that plays Magic against you

Design, 2026-08-31.

## Problem

`magic-cards-model-api` already serves a playable game: the engine, the HTTP
seat API, the `/play/` webUI, and the `mtg` MCP toolset. The opponent it gives
you is `ai_seat.py`, which spawns `claude --print --allowedTools Bash` once per
game. That agent:

- forgets everything the moment the game ends, so it cannot build a read on a
  matchup or notice it lost the same way twice;
- has no strategy material - it is handed a generic brief and its own board
  state, and nothing else;
- runs in a codepath (`-p`, print mode) that assembles a different system
  prompt from the interactive harness, so skills, hooks and MCP behave
  differently from what a human gets.

We want an opponent that persists across games, remembers what it played and
why, has the deck guides from `~/Documents/mtg-decks` available, and knows
which deck it is piloting before it is asked for a single action.

`tatara-claude-code-wrapper` (Go) already solves the hard part of this: it
supervises one long-lived **interactive** `claude` process over a PTY and
exposes it as an HTTP turn API. This design ports that wrapper to Python,
lands it in `magic-cards-model-api`, and wires it to the game through a small
driver service, all brought up by one `docker compose up`.

## Non-goals

- Multi-game routing. One agent container plays one match at a time.
- Kubernetes. No OIDC/Keycloak, no pod TTL, no handoff turns.
- Changing the engine. `../magic-cards-model` is not touched.
- Replacing the existing subprocess AI seat. It stays as the default mode.

## Architecture

Three containers, one docker network.

```
                       docker compose
  +-------------+        +-----------------+        +-------------+
  |   game      |        |     driver      |        |   agent     |
  |             |        |                 |        |             |
  | uvicorn     |<-------| choreography    |------->| wrapper     |
  | /api        |  state | + memory index  | turns  | (PID 1)     |
  | /play/ UI   |        |                 |        |   |         |
  | MCP module  |        +-----------------+        |   v         |
  +-------------+                 ^                 | interactive |
        |                         |                 |   claude    |
        | ai_seat.py POSTs        |                 |   over PTY  |
        +-- /dispatch ------------+                 +------|------+
                                                           |
        <--------- mtg MCP tools (stdio, --api game:8000) --+
```

The human plays in `/play/` on `game`. The agent plays through the same seat
API via the `mtg` MCP server, which it spawns itself over stdio and which
talks to `game:8000`. Neither side can see the other's hidden information -
`visibility.py` on the server redacts by seat uid, and that boundary is
unchanged.

## Component 1: the wrapper - `src/mtg_agent/wrapper/`

A faithful Python port of `tatara-claude-code-wrapper`, minus the parts that
only mean something under a Kubernetes operator.

### Module map

| Go | Python | Responsibility |
|---|---|---|
| `cmd/wrapper/config.go` | `config.py` | env (UPPER_SNAKE) and mounted file paths to a typed `Settings` |
| `internal/bootstrap/bootstrap.go` | `bootstrap/render.py` | orchestrates the render, in order |
| `internal/bootstrap/claudejson.go` | `bootstrap/claudejson.py` | `~/.claude.json` no-dialog seed |
| `internal/bootstrap/settings.go` | `bootstrap/settings.py` | `~/.claude/settings.json`: Stop hook, bypass mode, MCP auto-enable |
| `internal/bootstrap/mcp.go` | `bootstrap/mcp.py` | base `.mcp.json` merged with `/etc/wrapper/mcp.d/*.json` overlays, 0600 |
| `internal/bootstrap/skills.go` | `bootstrap/skills.py` | baked + custom skills into `/workspace/.claude/skills` |
| `internal/bootstrap/claudemd.go` | `bootstrap/claudemd.py` | `/workspace/CLAUDE.md` and `~/.claude/CLAUDE.md` |
| `internal/bootstrap/repo.go` | `bootstrap/repo.py` | optional shallow clone into `/workspace` |
| `internal/bootstrap/hooks_lifecycle.go` | `lifecycle.py` | the six `HOOK_*` shell hooks |
| `internal/session/pty.go` | `session/pty.py` | spawn `claude` under a PTY; a writer seam for tests |
| `internal/session/ring.go` | `session/ring.py` | thread-safe ring buffer, ANSI- and whitespace-normalised matching |
| `internal/session/session.go` | `session/session.py` | boot navigation, submit, complete, snapshot, state machine |
| `internal/session/interrupt.go` | `session/interrupt.py` | one ESC byte, then resolve the interrupted turn |
| (probe endpoints) | `session/probe.py` | mid-turn probe with `pending`/`delivered`/`answered` |
| `internal/turn/` | `turn.py` | turn record + thread-safe in-memory store |
| `internal/httpapi/` | `httpapi.py` | FastAPI public router + loopback-only internal router |
| `internal/webhook/` | `webhook.py` | async retrying callback delivery |
| `internal/metrics/` | `metrics.py` | `prometheus_client`, the same `ccw_*` names |
| `cmd/cc-stop-hook/` | `stop_hook.py` | the Stop-hook entry point, installed as a console script |

Python-specific choices: the PTY comes from stdlib `pty`/`os.openpty`/`termios`
(no third-party equivalent of `creack/pty` needed); concurrency is threads plus
`threading.Lock`, matching the Go goroutine/mutex structure rather than
introducing asyncio into the session core; the HTTP layer is FastAPI, already a
dependency of this repo.

### Behaviour preserved exactly

These came out of the Go project's spike against the real `claude` binary. They
are ported as-is, and the port's tests pin each one:

1. **Interactive over a PTY, never `-p`.** `-p` and
   `--dangerously-skip-permissions` are forbidden. The point is that the agent
   runs in the same harness a human gets.
2. **Terminal output is never parsed for results.** The ring buffer exists only
   for boot-dialog detection and debug logging. Results come from the Stop hook
   plus the on-disk transcript.
3. **`~/.claude.json` seeding**: `hasCompletedOnboarding`,
   `customApiKeyResponses.approved` containing the last 20 characters of
   `ANTHROPIC_API_KEY`, and `projects["/workspace"].hasTrustDialogAccepted`.
   Written 0600.
4. **The "Bypass Permissions mode" warning is not seedable** and appears on
   every boot. It is detected in the ring buffer with ANSI escapes and
   whitespace stripped (the TUI lays words out with cursor moves), then
   accepted with Down + Enter.
5. **Boot completes on output quiescence**, not a fixed sleep: no new PTY bytes
   for 1.5s, with a ~4s floor, capped by `BOOT_TIMEOUT_SECONDS`. `claude`
   renders its first frame in about 2s but is still initialising; submitting
   then kills it.
6. **Submit is two writes**: `ESC[200~ <text> ESC[201~` (bracketed paste), a
   ~400ms pause, then `\r`. One write does not submit.
7. **The Stop hook always exits 0**, whatever happens. A hook must never block
   or alter `claude`.
8. **The internal turn-complete endpoint binds to 127.0.0.1 only**, on its own
   listener, so it is unreachable from outside the container.
9. **Inactivity does not fail a turn.** `TURN_TIMEOUT_SECONDS` firing records a
   suspected stall (`ccw_turn_stall_suspected_total`, `stallSuspectedSince` on
   `GET /v1/session`) and re-arms. A game turn can legitimately sit for minutes
   waiting on the human.

### HTTP API

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/messages` | `{text, callbackUrl?}` -> 202 `{turnId}`; 409 while a turn is in flight |
| GET | `/v1/messages` | turn summaries |
| GET | `/v1/messages/{turnId}` | full record; 404 if unknown |
| GET | `/v1/session` | `{state, turnsCompleted, model, stallSuspectedSince}` |
| GET | `/v1/transcript` | raw JSONL; 404 before the first turn |
| POST | `/v1/probe` | `{text?}` written mid-turn; never 409; 503 with no live PTY |
| GET | `/v1/probe/{probeId}` | `pending` / `delivered` / `answered`; 404 if superseded |
| POST | `/v1/interrupt` | one ESC byte; never 409; 503 with no live PTY |
| DELETE | `/v1/session` | graceful shutdown |
| GET | `/healthz` `/readyz` `/metrics` | operator surface, no auth |

Auth: `OIDC_ISSUER` and the Keycloak verifier are dropped. In their place,
`WRAPPER_SHARED_SECRET` (optional, empty by default) requires a matching
`X-Wrapper-Secret` header on `/v1/*`. Empty means open, which is correct on a
private compose network.

Internal: `POST /internal/turn-complete`, loopback listener only.

### Dropped from the Go original

- `internal/auth` (OIDC JWT verification) - replaced by the shared secret above.
- `AGENT_POD_TTL_SECONDS`, `handoff: true` turns, `contractVersion`,
  `CHECKOUT_BRANCH`/`TASK_BRANCH` review-pod behaviour. These exist to
  coordinate with a Kubernetes operator that does not exist here.
- `internal/convstore` S3 paths. Already removed upstream; the on-disk
  transcript check that backs crash-resume is kept.
- `safetypush`, the release-pin and no-S3 guards. Repo-specific to tatara.

## Component 2: the driver - `src/mtg_agent/driver/`

A FastAPI service. It owns choreography and memory; it holds no game state of
its own.

### Dispatch

`ai_seat.py` gains a second mechanism, selected by `AI_SEAT_MODE`
(`subprocess`, the current default, or `wrapper`). In `wrapper` mode
`registry.start` POSTs `{uid, game_id, player, api_base}` to
`DRIVER_URL/dispatch` instead of spawning a process, and reports
`mechanism: "wrapper"` with the driver's answer. `ai_seat_api.py`, the UI, and
the seat API are untouched. This keeps the secret seat uid on the path it
already travels - the driver never needs a privileged endpoint to discover it.

On `/dispatch` the driver:

1. `GET /api/games/{id}?uid=...` - its own decklist and both seat names.
2. Fingerprints that decklist against `/guides/decks/*/decklist.txt` to name
   the archetype. A deck matching none of them is fine: the agent is told there
   is no guide for it and plays from the decklist alone.
3. Renders `/workspace/current-game.md`: game id, seat uid, seat name, the full
   decklist, absolute paths to that deck's `GAMEPLAN_GUIDE.md`,
   `SIDEBOARD_GUIDE.md`, `META_CALLS.md` and `matchups/`, and an index of the
   agent's own past notes for this deck under `/workspace/memory/`.
4. `POST wrapper /v1/messages` with turn 0 and
   `callbackUrl: DRIVER_URL/turn-done`.
5. On `/turn-done`, checks whether the game ended. If it did, pushes a
   reflection turn.

### Turn 0

Built from `docs/ai_seat_brief.md` (the rules of engagement there are already
correct and hard-won) with the `seat_cli` command section replaced by the `mtg`
MCP tools, plus:

- who it is, which game, which seat uid;
- its decklist and archetype;
- read `current-game.md` first, then the gameplan guide, then its own memory
  index for this deck, **before** taking any action;
- identify the opponent's archetype from cards it actually sees, then read the
  matching `matchups/<archetype>.json`;
- play until `winner` or a drawn game.

The wrapper refuses a second turn while turn 0 is in flight, so the agent
necessarily has its deck and guide loaded before its first action. In-game it
stays inside that single turn, blocking on the MCP `wait_for_priority` tool -
the same tool the human's UI click resolves against.

### What the agent is not told

Nothing about the opponent's decklist or archetype. It deduces the matchup from
cards it sees. The server would happily tell the driver the human's list, and
the driver deliberately does not pass it on: notes written by an agent that saw
its opponent's deck are worthless for real matchups, and learning to read a
matchup from the first few plays is exactly what the cross-game memory is for.

### Memory

`/workspace/memory/`, a host bind-mount, so it survives every container
recreate. Layout:

```
memory/
  MEMORY.md                       index, one line per note
  <deck>/
    vs-<archetype>.md             the read: their threats, our answers, lines
    games/<game-id>.md            one game: result, key decisions, what to change
```

The reflection turn asks the agent to write one game note and to update the
matchup note and the index. Writing is the agent's own file editing inside
`/workspace` - the driver renders the index into turn 0 but never writes notes
itself.

### Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/dispatch` | `{uid, game_id, player, api_base}` -> 202 `{turnId, deck, guide}` |
| POST | `/turn-done` | the wrapper's webhook target |
| GET | `/status` | current game, deck, last turn id and state |
| GET | `/healthz` | liveness |

## Component 3: docker compose

The engine is a path dependency (`../magic-cards-model`), so every build
context is the parent directory. `docker-compose.yml` lives in
`magic-cards-model-api` and uses `context: ..`.

| service | runs | ports | mounts |
|---|---|---|---|
| `game` | `uvicorn magic_cards_model_api.main:app` - API, `/play/` UI | 8000 | scryfall cache volume; `${MTG_DECKS:-~/Documents/mtg-decks}` -> `/guides` ro |
| `agent` | `python -m mtg_agent.wrapper` as PID 1, supervising interactive `claude` | 8080 | `${MTG_DECKS}` -> `/guides` ro; `./memory` -> `/workspace/memory` rw |
| `driver` | `python -m mtg_agent.driver` | 8090 | `${MTG_DECKS}` -> `/guides` ro; `./memory` rw |

`game` mounts the guides too so the four guided decklists
(`dimir-frog`, `living-end`, `orzhov-martyr`, `prison-tron`) are selectable in
the UI's deck picker alongside the eight bundled ones. Otherwise the agent
would usually be handed a deck it has no guide for.

Dockerfiles:

- `Dockerfile` (new; the repo has none today): python 3.13, `uv sync`, the
  engine copied from `../magic-cards-model`. Used by `game` and `driver`.
- `Dockerfile.agent`: the above plus node and the pinned `claude` CLI in its
  own layer behind `ARG CLAUDE_CODE_VERSION`, so bumping it rebuilds one layer.

Environment, from a gitignored `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
MTG_DECKS=/Users/szymonri/Documents/mtg-decks
CLAUDE_MODEL=sonnet
```

The agent's `.mcp.json`, written by `bootstrap/mcp.py`, carries two servers:
`mtg` (stdio, `python -m magic_cards_model_api.mcp --api http://game:8000`) and
`spellslinger` (http, the hosted rules/card server) so the agent can look rules
and card text up instead of reciting them from memory.

## Testing

`tests/wrapper/` and `tests/driver/`, pytest, run by the existing `mise run
test`.

Wrapper unit tests, mirroring the Go suite:

- `config`: env parsing, defaults, file-path pointers.
- `bootstrap`: each renderer's output, the MCP overlay merge, 0600 permissions
  on the two secret-bearing files, skill copying, render ordering.
- `ring`: ANSI stripping, whitespace-normalised matching, wraparound.
- `session`: boot-dialog detection and the Down+Enter answer; quiescence
  timing; the two-write submit order and the gap between the writes; state
  transitions including `dead`; the timer/complete race resolving once.
- `turn`: concurrent completion, unknown turn ids.
- `webhook`: retry with backoff, drop after `WEBHOOK_RETRIES`, metric.
- `stop_hook`: payload parsing, `last_assistant_message` preferred over the
  transcript, usage summed across every assistant message, exit 0 on every
  failure path.
- `httpapi`: 409 while busy, 503 with no PTY, 404 on unknown ids, the shared
  secret when set, the internal router refusing non-loopback.

Wrapper integration test: a stub `claude` script under `tests/testdata/` that
emits the bypass dialog, accepts a bracketed paste, and invokes the stop hook -
the same technique as the Go repo's `testdata/stubclaude`. It exercises
bootstrap seed, PTY boot, dialog accept, two-write submit and hook callback
with no API key and no network.

Driver tests: decklist fingerprinting against the real guide decklists
(including the no-match case), the rendered `current-game.md`, turn-0 assembly,
`/dispatch` and `/turn-done` behaviour, and end-of-game detection triggering
exactly one reflection turn. The wrapper and game are addressed over HTTP, so
these use `httpx`'s transport substitution against real FastAPI apps rather
than mocking.

This repo's no-mocks rule governs *rules* tests: every test of engine behaviour
builds a real board from real Scryfall printings. A stub `claude` binary is not
a mocked game state, and no test in either new suite touches the engine.

## Risks and open points

- **Bumping `claude` can break the boot path.** The bypass dialog's wording and
  the paste/submit behaviour are undocumented. The version is pinned in its own
  Docker layer, and the integration test's stub encodes the expected sequence,
  so a real-binary regression shows up as a boot timeout with the PTY tail
  logged. That tail is the single most useful diagnostic and is kept.
- **The `mtg` MCP server picks "the single live game"** when not given
  `--game`. Fine for one match at a time. Two concurrent matchups against one
  agent container would need the driver to pass `--game`, which is out of scope.
- **A long human think-tank looks like a stalled turn.** Handled by the ported
  behaviour that a stall is recorded and never fails a turn.
- **Guides are large** (roughly 500 lines per gameplan guide). Turn 0 hands over
  paths, not contents, so the agent reads what it needs.

## Delivery order

1. Wrapper port with its tests, runnable standalone against a stub `claude`.
2. `AI_SEAT_MODE=wrapper` in `ai_seat.py`.
3. Driver with its tests.
4. Dockerfiles and `docker-compose.yml`.
5. A README section: bring it up, play a game, where the memory lands.
