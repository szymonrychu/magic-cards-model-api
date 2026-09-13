# magic-cards-model-api

A self-test API over the [magic-cards-model](../magic-cards-model) rules
engine. Set up any board state, ask whether an action is allowed, and get the
engine's own refusal text back when it is not. The play UI is served at `/`;
the crude MTGO-style frontend from the sibling repo
`../magic-cards-model-test-frontend`, when that repo exists next to this one,
is served at `/frontend`.

## Play it - Human vs AI

`http://127.0.0.1:8000/` is a small, buildless, server-rendered UI
(`ui/`, plain HTML/CSS/JS - no npm, no framework) over exactly the seat API
below: load a bundled decklist from `decks/` or paste/upload your own,
validate it (`Deck.check_legality`, CR 100.2a/100.4a), and start a game where
you click one seat and a headless `claude` subprocess plays the other,
driving `seat_cli` the same way a human dispatched to that seat would. If
`claude` is not on `PATH`, the UI falls back to showing the exact command and
prompt to run by hand in another terminal - the game is fully playable either
way. See `ai_seat.py` and `src/magic_cards_model_api/docs/ai_seat_brief.md` for
the mechanism and the brief the agent is given.

### The play screen

The board is laid out the way MTGO and Arena lay it out: the opponent on top,
the shared stack in the middle, you at the bottom. Everything the game ever
asks you sits in one side panel on the right, top to bottom: the prompt, the
casting strip, the priority verbs, the stops panel, and the mtg-buddy
advisor's current proposal. There is no confirm ribbon and no modal dialog.

**One click when there is nothing left to choose.** Clicking a land in your
hand plays it. Clicking a spell that has no modes, no X, no alternative cost
and nothing to target dry-runs the cast with automatic mana payment and, if
the engine allows it, casts it there and then - the lands it is about to tap
show a tap preview, then tap. Clicking a permanent you control that has
exactly one activated ability activates it, mana abilities included. A
permanent with several abilities (or an ability plus Crew) opens a small menu
anchored to the card, listing each ability with its cost.

**Slots.** A spell that still has something to choose lifts into a casting
strip above your hand, and the board becomes the prompt, in this order: modes,
X, alternative or additional costs, target slots, mana. Targets are slots, one
per requirement, each labelled with the mode it came from ("Mode 1: target
player"), so a modal spell can put the *same* player in two different slots.
Kozilek's Command choosing modes 1 and 2 with you as both targets is the case
this was built for. The active slot is the first unfilled one, the board
highlights what may legally fill it, clicking a slot chip refocuses it, and
clicking a chosen target again fills the active slot with it too when that
slot can legally take it (how the same player ends up in two different
Kozilek's Command slots) and un-chooses it otherwise. The cast fires the
moment the last required slot is filled and the dry run comes back allowed. A Cast button
appears only when the player has to say they are done: "up to N" targets, a
division, pinned mana, or a dry run that came back refused. Escape, or
clicking the lifted card, cancels.

**Pins.** By default a cast offers every untapped mana source you control and
lets the engine choose (CR 601.2g: mana abilities are activated before costs
are paid). To override that, click an untapped land while a cast is pending:
it is pinned as an explicit source, and a land that makes more than one colour
asks which first. Pinned sources go first and the rest follow, so the engine
still fills the remainder. Clicking a pinned land unpins it. With no cast
pending, clicking a land simply activates it and floats the mana, which the
you-row shows at all times.

**Combat.** In your declare attackers step (CR 508.1) clicking a creature
moves it into the red zone between the two battlefields; a defender menu
appears only when more than one defender is legal for it. "Attack" declares
them, "No attack" passes. Defending (CR 509.1), you click your creature and
then the attacker it blocks; the pair is listed, clicking it again removes it,
and "Block" / "No blocks" ends the step.

**Verbs and stops.** The priority box offers exactly two verbs, Pass and Pass
until, because priority has exactly two: CR 117.1 lists what the player with
priority may do ("cast spells, activate abilities, and take special actions")
and CR 117.3d has a player who chooses none of them pass. Resolving the top of
the stack and ending a step are what CR 117.4 makes happen once everyone has
passed in succession, not buttons. Pass gives up one priority; Pass until keeps
passing until a stop fires, and while it runs the box collapses to a single
Stop.

A run always halts when you are attacked, when a decision is waiting on anyone,
and when the game ends - none of those is a tick and none can be turned off.
Everything else is the stops panel: a grid of steps by whose turn it is, plus
one tick above it, **anything on the stack**. That tick is not a destination
and does not replace the step ticks - it is an extra reason to stop on the way
to one, which is the case it exists for ("pass until my precombat main, but
hand the board back if my opponent casts something in my upkeep"). It starts
ticked, because CR 117.4 resolves a spell the moment every player has passed in
succession, so the pass you have not made yet is the whole of your chance to
answer it. The step grid starts at your precombat main, your declare attackers,
your postcombat main, the opponent's declare attackers and the opponent's end
step, and whatever you check is saved in `localStorage` under `mcm.stops.v2`,
so it survives a reload - a panel saved before the stack tick existed reads
back with it on, and step ticks saved under the older `mcm.stops.v1` still
load. Beside the stack tick, "Keep priority by default" (on unless unticked,
never carried over from v1) keeps priority after you cast a spell or activate
a non-mana ability instead of auto-passing it. Untap and cleanup give nobody priority (CR 502.4, CR 514.3)
and the game crosses them itself (CR 500.3); in the rare state where one is
still showing, the box offers an Advance button instead of a pass, which is the
one escape from a step no seat is allowed to move. Conceding (CR 104.3a) is
offered for as long as the game is live, whoever holds priority, and always
takes two clicks: the first arms the button, the second sends it, and any
pass verb disarms it again. Once somebody has won or the game is drawn it
goes away. The "keep priority" checkbox on
the casting strip is CR 117.3c's retained priority for that one action: it
starts from the standing default, and unticking it passes after that action.

**The advisor panel.** The bottom of that column shows the mtg-buddy
advisor's current suggestion, if it has one: the decision summary, the
agent's own rationale, which ledger entry and step it was proposed at, and
Approve - which records the approval and then performs the play, so you do
not have to click it out on the board yourself. It is a static panel showing
exactly one proposal, never a list. Approve is enabled only while the
proposal is still fresh (nothing has happened in the game since the board the
agent read) and nothing else of yours is in flight; once the board moves, the
summary and rationale stay readable but dimmed, the panel says which entry
moved it, and the button is disabled until the advisor proposes again. A
proposal the server could not dry-run is marked "unchecked".

**The log.** Behind the topbar's **Log** button, next to Zones - it used to
be the bottom of the side panel, and the advisor panel has that corner now.
It reads `GET /api/games/{id}/log?since=` incrementally and shows every
action with its turn and step. A refused action appears in red with the
engine's own words, which is the first place to look when a click did not do
what you expected. The dashed row under your hand appears when
something grants you a play permission from a zone other than your hand.

Starting a game from the lobby never touches another: games live side by side,
up to `MAX_LIVE_GAMES` at once (see "Several games at once" below). **End
game** is the only reset there is - a human picks a game in the lobby and ends
it, rather than starting a new one silently ending an old one. Ending a game
stops opening its seat uids, stops its pregame watcher thread, and stops its
AI seat - a subprocess is terminated, a wrapper-mode seat is handed back to
the driver, a kubernetes-mode seat's Job is deleted - freeing a slot under the
cap. It is written to the game's own audit log (`game_reset`), so a transcript
ends with the reset rather than trailing off. What the continuous agent has
learned is untouched: that lives in `./memory`, and nothing in the reset
writes there. See `lobby.py`.

## Play against a continuous agent

`docker compose up --build` brings up three containers, running the
`AI_SEAT_MODE=wrapper` seat: the game (this repo, with the play UI at `/`), an
**agent** running one interactive `claude` under a PTY for the length of one
match, and a **driver** that tells the agent what it is playing and recycles
it between matches. Compose's agent container is a single continuous agent,
one per stack - see "Several games at once" below for the modes that run more
than one agent side by side.

The difference from the bundled AI seat is memory. The bundled seat is a
one-shot `claude --print` that forgets the game when it ends. The agent runs
in the interactive harness a human gets, and carries what it learned from one
game to the next through notes it writes itself.

**One container plays exactly one match.** When the game ends the driver
pushes a single reflection turn, then sends `DELETE /v1/session`; the agent
process exits, `restart: unless-stopped` brings up a fresh container, and the
next game starts with a brand-new `claude` session. The same thing happens if
the supervised `claude` dies mid-game: a session that can never serve another
turn takes the container down with it rather than sitting there "running".

So nothing crosses a match boundary except `./memory`, the host bind-mount the
agent writes its notes into. That is deliberate: a session that remembered
game one directly would carry its whole transcript - and its mistaken reads -
into game two, while a note has to be written down to survive.

A dispatch that lands while the replacement container is still booting (pty
spawn, bypass dialog and quiescence take up to about two minutes) is accepted
with `pending: true`, and the driver keeps offering turn 0 until the agent
answers or the boot budget (`AGENT_BOOT_WAIT_SECONDS`, default 360) runs out.
A second *different* game arriving while a match is live is refused with a 409
naming the game in progress - compose's one agent container plays one game at
a time. That limit is specific to `wrapper` mode's single agent container;
`kubernetes` mode gives every AI seat its own Job and has no such limit (see
"Several games at once" below).

What it knows before its first action:

- its own decklist, and which guided deck under `$MTG_DECKS/decks/` that is;
- that deck's `GAMEPLAN_GUIDE.md`, `SIDEBOARD_GUIDE.md` and per-matchup files;
- every note it has written itself, under `./memory/<deck>/`.

What it is deliberately NOT told: anything about your deck. It works the
matchup out from the cards you play, then opens the matching guide. Getting
that read right sooner is what the notes are for.

On the reflection turn - the last thing it does before the container exits -
it writes `./memory/<deck>/games/<game-id>.md` and revises
`./memory/<deck>/vs-<archetype>.md`. That directory is a host bind-mount, so
it survives every restart and every rebuild - and you can read and edit it
yourself.

| what | where |
|---|---|
| play | http://127.0.0.1:8000/ (`/play/` redirects here) |
| agent turns, probe, interrupt | http://127.0.0.1:8080/v1/... |
| which deck the driver identified | http://127.0.0.1:8090/status |
| the agent's notes | `./memory/` |

Set `AI_SEAT_MODE=subprocess` (the default outside compose) to go back to the
one-shot seat, or `AI_SEAT_MODE=kubernetes` to schedule each AI seat as its own
Job - see "Several games at once" below.

### The guides clone and the handover

`docker compose up` also runs a one-shot **guides-init** service before the
agent starts: it clones github.com/szymonrychu/mtg-decks writably into the
named `agent-guides` volume, using the token in `MTG_DECKS_GH_PAT` (see
`.env.example`) and `deploy/guides-clone.sh` - the same script and image the
kubernetes `guides` initContainer runs (`deploy/seat-job.yaml`). `agent`
mounts that volume read-write and waits for `guides-init` to finish; `driver`
mounts the same volume read-only. The `MTG_DECKS_GH_PAT` secret is mounted
only into `guides-init` and `agent` - never `driver` or `game` - at
`/var/run/mtg-decks-git/GH_PAT`, the same path the agent's git credential
helper reads (`wrapper/bootstrap.py`). Your own `$MTG_DECKS` checkout stays
mounted read-only into `game` for deck browsing, and is never mounted
writable into the agent - the agent's writes go to `agent-guides` and get
pushed upstream, not to your local clone.

**Handover.** The agent can edit a deck's guide mid-play (`boarding-plan.json`
matchup notes, `NOTES.md`) and hand it over with `mcp-seat guides propose`:
render, commit, push a `seat/<game-id>-guides` branch and open a pull request,
returning at once with `awaiting_owner_review` and the PR's URL. mtg-decks is
a private repo with no GitHub Actions budget, so its `ci` and `automerge`
workflows never run there - the command never waits on either, and the guide
edit is not live until the owner reviews and merges the PR by hand. Play
scripts are unaffected: `mcp-seat plays admit` still pushes straight to
mtg-decks main after the in-pod gate. That handover runs at the natural
points where the agent stops playing: when a game ends, and when the driver's
`POST /release` is called (a human taking the seat back, or a buddy standing
down). A plain `docker compose stop` or `docker compose down` does **not**
run a handover - it just stops the containers, mid-match state and all;
`agent`/`driver` are given a 10-minute `stop_grace_period` (mirroring the k8s
Job's `terminationGracePeriodSeconds`) purely so an *in-flight* handover from
one of those two triggers has room to finish before compose sends SIGKILL,
not to start one on its own.

### What `docker compose logs` shows

Three containers, three different things worth watching:

- **game** - one line per game action (the audit log), and, in a banner at
  WARNING, every `report_problem` a seat files, its text included. That call
  is the agent saying the engine got a rule wrong while it was playing, so it
  is deliberately the loudest thing in the log. `grep "PROBLEM REPORTED"`.
- **driver** - the whole match lifecycle: which deck it identified and how well
  it matched, `turn ... started: game=... seat=... deck=...` for every turn it
  pushes, the agent's own account of every finished turn (trimmed - the full
  text is on `GET /v1/messages/{turnId}` in the agent), the game ending, the
  reflection turn, and the retirement that stops the agent.
- **agent** - session boot, turn submitted/complete with its stop reason,
  suspected stalls, interrupts, a dead `claude` with the tail of its terminal,
  and - on the `agent` logger - a running account of what the seat is doing:

      INFO agent says: Bolting the Ragavan before it connects.
      INFO agent calls Bash mtg-seat cast 'Lightning Bolt' --target ragavan
      WARNING agent tool failed: Exit code 1 it is not your priority

  That comes from tailing the JSONL transcript claude writes as it goes
  (`wrapper/activity.py`), not from scraping the terminal. Each event is
  emitted whole, with its whitespace collapsed so one event stays one
  physical line; `thinking` blocks (which arrive carrying a signature and no
  text at all) and successful tool *results* are left out on purpose, and
  anything shaped like an API key or bearer token is redacted.

  The same tail also publishes one coarse word for what the agent is doing
  right now - `waiting`, `reading_board`, `reading_cards`, `reading_rules`,
  `deciding`, `acting`, `thinking` or `working` - on `GET /v1/session` as
  `activity: {state, seconds, seq}`. The driver carries it on its own
  `GET /status` as `agentActivity`, the game server puts it on the seat
  status, and the play UI's AI pill reads "AI (claude): reading cards"
  instead of a flat "thinking" for the whole match. Only the word crosses:
  no tool argument, no card name and none of the agent's prose is on that
  wire, because the human at the other seat is reading that pill mid-game.

Nothing here needs `LOG_LEVEL=debug`; that is for the wrapper's own internals.

### Turning the volume up or down

| env var | default | what it does |
|---|---|---|
| `ACCESS_LOG_QUIET` | `1` | Drops uvicorn access lines for *successful reads* of the polled routes on all three services: `/api/health`, `/healthz`, `/readyz`, `/status`, `/api/games/{id}`, `/api/games/{id}/log` and `/api/ui/games/{id}/ai-seat/status`. Those polls are the whole of the idle log. A POST is never dropped, and neither is any response that is not 2xx/3xx - a poll answering 404 or 500 is the thing worth reading. Set to `0` for uvicorn's unfiltered log. (`access_log.py`) |
| `AGENT_LOG_LEVEL` | `tools` | How much of the agent's activity the agent container prints: `off`, `turns` (boundaries only), `messages` (+ the agent's prose), `tools` (+ every tool call and every tool failure). (`wrapper/activity.py`) |

Both are read from `.env` by `docker compose`, so
`ACCESS_LOG_QUIET=0 docker compose up` is enough to get everything back.

## Several games at once

The lobby (`http://127.0.0.1:8000/`) lists every live game side by side -
starting one never ends another - with its own model, effort and lobby
controls per game. How many AI seats can actually run *at the same time*
depends on `AI_SEAT_MODE`:

| `AI_SEAT_MODE` | what plays each AI seat | how many games at once |
|---|---|---|
| `subprocess` (default outside compose) | a one-shot `claude --print` per seat per game (one subprocess plays the whole match agentically, then forgets it) | as many as the process can spawn; each seat is independent, nothing shared to contend over |
| `wrapper` (what `docker compose up` runs) | one continuous interactive `claude` under a PTY, in the compose stack's single **agent** container, recycled between matches (see "Play against a continuous agent" above) | one - a second game's dispatch is refused with a 409 while the one agent container is mid-match |
| `kubernetes` | one Kubernetes Job per AI seat, each carrying its own `agent` + `driver` sidecar, scheduled through the Kubernetes API (`src/magic_cards_model_api/k8s_seats.py`) | as many as the cluster and `MAX_LIVE_GAMES` allow - every seat gets its own Job, so nothing serializes them |

Compose stays a single continuous agent by design - it is one process
supervising one PTY, and that does not multiply. Running several games
against continuous agents at once means running against a cluster in
`kubernetes` mode instead.

### The lobby

- **List**: `GET /api/ui/games` returns every lobby-booted game - players,
  turn, step, winner, each AI player's `agent` settings, and (scoped to the
  seat uids the caller already holds, via a `?uids=` query) the uids it may
  act through. A game nobody's browser holds a uid for still shows up as an
  open seat for a new browser to claim. `uids` is **one comma-separated
  value** (`?uids=a,b`), not a repeated query parameter - `?uids=a&uids=b`
  keeps only the last one and silently hides your seat uid on every other
  game.
- **Open**: click a game in the list to resume it with the uid(s) this
  browser saved for it (`mcm.games.v1` in `localStorage`, a list of
  `{gameId, uid, aiUid}` entries - one per game, not one for the whole
  browser any more).
- **End game**: `DELETE /api/ui/games/{id}?uid=` (or `?key=` with the game's
  admin key) is the only reset. It stops that game's AI seat - subprocess
  terminated, wrapper dispatch handed back to the driver, or kubernetes Job
  deleted - and deletes the game, freeing a slot under the cap. It never
  touches `./memory`.
- **The cap**: `MAX_LIVE_GAMES` (env, default 4) counts lobby games that are
  not yet over; starting a new one past the cap is refused with a 409 naming
  the count. A finished game does not count against it; only End game frees a
  slot for one still in progress.

### Agent settings, per game

`POST /api/ui/games` takes an optional `agent` object per AI player -
`{model, effort, max_thinking_tokens}` (`AgentSettings` in
`agent_settings.py`) - carried into whichever mode plays that seat (subprocess
argv, the wrapper dispatch, or a Job's env), and shown back on `GET
/api/games/{id}`'s player block and in the lobby list above.

- `model`: any non-empty string. The UI suggests `sonnet`, `opus`, `haiku`,
  `fable`; the default when `agent` is omitted comes from `AGENT_DEFAULT_MODEL`
  (env, default `opus`, for an AI seat and a buddy alike).
- `effort`: one of `low`, `medium`, `high`, `xhigh`, `max`. Default `high`.
- `max_thinking_tokens`: a non-negative integer, or `null`/omitted to leave
  Claude Code's own default. `0` disables thinking where the model allows it.

In `wrapper` mode this is not just carried in a dispatch payload and ignored:
the driver's `assign` reads the wrapper's current settings (`GET
/v1/session`) and, if they differ from this seat's `agent`, pushes the new
ones (`POST /v1/session`) before turn 0. The wrapper restarts its supervised
claude with them - there is no way to change `--model`/`--effort` on an
already-running claude. An older wrapper that predates the route answers 404
to both calls, which the driver treats as "not supported, continue": the seat
still plays, just with whatever model/effort it booted with.

**Breaking change for scrapes**: the wrapper's `ccw_turn_tokens_total`
Prometheus counter (`/metrics`) carries `model` and `effort` labels now, in
addition to `type` - one time series per (type, model, effort) combination
rather than one per `type` alone. A query or dashboard panel that assumed
exactly one series per `type` (grouping only `by (type)`, or reading
`ccw_turn_tokens_total{type="output"}` expecting a single value) will now see
one series per model/effort choice instead, and needs an explicit `sum by
(type)` (or similar) to collapse back to the old shape.

### Seeing the Jobs (`kubernetes` mode)

```sh
kubectl -n mtg-engine get jobs -l app.kubernetes.io/name=mtg-agent
kubectl -n mtg-engine logs job/<name> -c agent
```

Every seat Job carries `app.kubernetes.io/name=mtg-agent` plus `mtg/game-id`
and `mtg/seat-uid`, so `-l mtg/game-id=<id>` narrows to one game's seat. A Job
runs its `agent` and `driver` containers for the length of one match and is
cleaned up automatically (`ttlSecondsAfterFinished`) once it finishes; deleting
it early is what End game does.

Deploying and operating the cluster - the ServiceAccount and RBAC, the
`ReadWriteMany` memory claim (**the live cluster's existing claim is bound
`ReadWriteOnce` and has to be recreated**, since a PVC's access mode is
immutable once bound), the NetworkPolicy, and the image tags - is all in
`deploy/k8s/README.md`.

## Measuring play strength

There was no way to say whether a change made the agent play better: no
self-play runner, no scripted opponent, no win-rate script anywhere in either
repo, and 2 of the 15 recorded games had ever reached an end state. This is
the measuring stick - a control to play against, and a scorecard to read.

```sh
mise run eval:gauntlet                            # 10 seeded games, agent vs baseline
mise run eval:gauntlet -- --seeds 20 --agent baseline   # a zero-LLM control run
mise run eval:gauntlet -- --seed 7 --decks a=decks/x.txt,b=decks/y.txt
```

The task brings postgres up itself and migrates it, then boots the seeded
matchups with `python -m magic_cards_model_api.serve --match ...` (one process,
one `--match` per seed - every game replayable by passing its seed back), plays
them, and prints the scorecard. It refuses to run without a database: `serve`
would otherwise record into an in-process store that dies with the server, and
ten games played with nothing recorded is worse than not running.

### The baseline seat

`src/magic_cards_model_api/baseline_seat.py` is a deterministic, zero-LLM
opponent driven by `GET .../legal-actions`. Play a land if one is legal; cast
the highest-cost affordable permanent, then the highest-cost affordable spell;
attack when the attacker survives the profitable block or when the race is
lethal; block only to prevent lethal; answer every other question with the
engine's own `machines/decisions.default_answer`. No randomness and no memory,
so two runs of one seed produce the same games and any difference between arms
belongs to the arm. It asks that endpoint with `candidates=true` and names a
spell's targets out of the row it is about to act on, so a targeted cast is
one round trip rather than two.

It lives here and not in the engine on purpose. CR 118.6 makes an unpayable
action a *legal* one, so the enumeration deliberately offers moves nobody
would make - choosing among them is a seat's business, and the engine stays a
stateless rules library.

It cannot stall, which matters more than it sounds: a stalling baseline
manufactures the very unfinished games the scorecard exists to count. Every
answerable decision kind has a default, a refused action is not retried until
the board moves, and a game where nothing has a move for long enough ends in a
concession (CR 104.3a) rather than a hang - a real ending, scored as a loss.

### The scorecard

`scripts/eval_report.py` reads the `games` and `records` tables and prints,
per seat: finish rate, record, turns to end, actions per game, reasoning
coverage (twice - `records.require_reasoning` exempts a pass, so the overall
share and the share over everything else are both shown), and refusal rate
split into **rules errors** and **stale races**. That split is the point: a
refusal because the seat misread a rule is a defect in how it plays, and one
because the board moved under a decision it had already read is a defect in
the harness that improving the agent will never fix. The split reads
`detail.stale`; a ledger recorded before that flag existed carries none, and
those are reported as unsplit rather than counted as rules errors.

```sh
uv run python scripts/eval_report.py --ledger run.json   # a captured run, no database
uv run python scripts/eval_report.py --json              # the configured database
```

**Every rate carries a 95 percent interval and no bare percentage is ever
printed**, because the numbers a small run produces are not what they look
like. Roughly 20 games gives about +/-20pp on a win rate, 50 gives about
+/-14pp, and resolving a 10pp A/B difference needs roughly 200 games *per
arm*. Refusal rate and finish rate are the exception, and the reason a 10-game
run is worth doing at all: each carries hundreds of attempts rather than one
outcome per game, so their intervals are tight immediately. Those two can gate
a change today; a win rate is something to accumulate towards. The report says
so in its own output.

Nothing here grades a declared plan. `review.py` refuses to, and this refuses
with it - a `plan_declared` row is not an action, not an attempt, and
contributes to no metric. Grading outcomes (who won, how many turns, how many
attempts were refused) is arithmetic over what the server wrote down; grading a
prediction would be a judgement dressed as arithmetic.

## Run it

One command, from this repo:

```sh
mise run install   # first time only
mise run           # serves http://127.0.0.1:8000
```

The engine is an editable path dependency pointing at
`../magic-cards-model`, so that repo must sit next to this one.

## Test and lint

```sh
mise run test    # pytest; card data comes from recorded Scryfall fixtures, no network
mise run lint    # ruff
mise run fmt     # ruff format + fix
```

## How it works

- Games live in memory: a restart wipes every session.
- Card data is fetched from the real Scryfall API
  (`User-Agent: magic-cards-model-api/0.1`, at most one request per 100 ms)
  and cached to disk under `.scryfall-cache/` (`MCM_CACHE_DIR` moves it), so a
  card is fetched once. Before the network, the engine's scenario-test pool at
  `../magic-cards-model/tests/fixtures/cards` is read - read-only, never
  written - which covers all but one card of every deck in `decks/` and takes
  a cold-cache decklist validation from ~35 seconds to instant.
  `MCM_FIXTURE_CARDS_DIR` points that elsewhere; an empty value turns it off,
  and a missing directory just means the network again.
  Every card is validated through the engine's `Card` model; non-Modern-legal
  cards are flagged in the response, not refused.
- Every legality answer comes from the engine itself: a `why_cannot_*` reason
  or the `ValueError` the engine raises at the point of action. The API never
  invents a refusal.
- `dry_run: true` on the action endpoints answers the question without
  touching state.

## Endpoints

All under `/api`. Interactive docs at `/docs`.

### Sessions

| | |
|---|---|
| `GET /api/health` | liveness |
| `POST /api/games` | `{players: [{username, life?}], active_player?, step?}` |
| `GET /api/games` | list sessions |
| `GET /api/games/{id}` | full state (below) |
| `DELETE /api/games/{id}` | drop the session |

`POST /api/games`'s `players` entries also take `is_agent?` (default
`false`) - a seat's kind, recorded once at creation. It is what
`records.require_reasoning` reads to decide whether a mutating action from
that seat needs `reasoning` - see "The heart: legality questions" below.

**Which store a deployment runs on is chosen from the environment**, once,
at import (`store.py`'s `select_store`). A configured database URL -
`DATABASE_URL`, or all of `POSTGRES_HOST`/`POSTGRES_USER`/
`POSTGRES_PASSWORD`/`POSTGRES_DB` (`POSTGRES_PORT` defaults to 5432), the
shape CloudNativePG hands a workload - selects `PostgresRepository`, and the
schema must already be migrated (`mise run db:migrate`, one-shot, never from
app startup). Nothing configured keeps `InMemoryRepository`, so a plain
`mise run run` and the whole `mise run test` suite stay docker-free with no
setup at all. A *partial* set of components counts as nothing configured
rather than as an error.

A postgres-backed deployment's mutating routes can answer `409` instead of
`200`/`400`: another writer committed a newer version of the game first
(`repository_postgres.py`'s optimistic-concurrency CAS). The body carries
`{detail, game_id, current_state}` - the game exactly as it now stands,
redacted for the caller's own `uid` the same way `GET /api/games/{id}` is -
so the loser can re-read and recompute its attempt against what actually
committed. There is no server-side retry.

### Cards

| | |
|---|---|
| `GET /api/cards?name=...` or `GET /api/cards/{name}` | Scryfall lookup, engine-validated, with image URIs and Modern status |
| `GET /api/card-names?q=...` | Scryfall autocomplete for live search |

### Setting the board up

| | |
|---|---|
| `POST /api/games/{id}/cards` | `{card: "Mountain", player, zone, position? (library "top"/"bottom"), tapped?, face_up?}` |
| `POST /api/games/{id}/cards/{cid}/move` | `{to_zone, player?, controller?, position?, tapped?, face_up?}` - allowed/reason like the actions |
| `POST /api/games/{id}/cards/{cid}/tap` | `{tapped: true/false}`; refusals come back as the engine's text |
| `PATCH /api/games/{id}/cards/{cid}` | `{face_up?, damage?, summoning_sick?, counters?: {"+1/+1": 2}}` |
| `PATCH /api/games/{id}/players/{name}` | `{life?, counters?: {"poison": 4}}` |
| `PUT /api/games/{id}/players/{name}/mana-pool` | `{add?: {"R": 2}, set?: {...}, empty?: true}` |
| `PUT /api/games/{id}/step` | `{step: "declare_attackers", player?, reasoning?}`; forward walks the turn machine, backward rewinds it raw - a raw teleport, running none of `advance`'s own beginning-of-step turn-based actions or triggers. **Not a seat verb**: no rule puts the turn marker anywhere, so this is the harness staging a board, and it has no MCP tool or `seat_cli` subcommand. It still moves the turn marker, so a caller that names an agent seat owes `reasoning` here like anywhere else |

Zones: `library`, `hand`, `battlefield`, `graveyard`, `stack`, `exile`,
`command`, `sideboard`. Shared zones (battlefield, stack, exile, command)
take the `player` as the controller. Counter kinds: `+1/+1`, `-1/-1`,
`loyalty`, `poison`, `energy`, `experience`, `time`, `charge`.

### The heart: legality questions

Each returns `{allowed: bool, reason: str | null, ...}` and performs the
action only when allowed and `dry_run` is not set. Every action below (and
`resolve-top`/`advance`/`decide`/`state-based-actions`/`PUT .../step`, and
the three `pregame/` declarations) also takes an optional `reasoning`
string - the seat's own stated reason for this action - **required** for a
seat this game registered as an agent (`is_agent` at creation, or a
decklist-booted seat's `ai: true`): omitting it there is refused with `422`,
naming the missing field, before the engine ever sees the request. Optional
and unenforced for a human seat. There is no exemption: every mutating route
either requires it or is named, with its reason, in
`tests/test_reasoning_gate.py`'s `EXEMPT_ROUTES`, and that test fails the
suite if a new route or MCP tool forgets.

Every mutating action writes exactly one row to this game's log, refusals
included, carrying the engine's own outcome, this `reasoning`, and every
action the acting seat could otherwise have taken at that exact moment
(`legal-actions`'s own enumeration, captured before the action ran, in
`legal_actions_before`, with the seat it was enumerated for in
`legal_actions_for`). `GET /api/games/{id}/log?uid=&since=` reads that log
back, oldest first.

`legal_actions_before` is exactly as much hidden information as
`legal-actions` itself is, and it is redacted on the same rule: only the
seat named in `legal_actions_for` - the seat the *server* resolved, never a
`player` field a caller filled in - and the admin key ever read it. A read
with no `uid`, or an unrecognised one, sees none of it, which is the one
place a `uid`-less log read is not the full debugger's eye: the rest of the
entry (snapshots included) still comes back unredacted for it.

| | |
|---|---|
| `POST /api/games/{id}/actions/cast` | `{player, card_id | card, from_zone?, spell_face?, targets?: [card ids or usernames], modes?: [1,2], x_value?, keyword?, alternative_cost?, choosing?, sacrificing?, discarding?, exiling?, tapping?, delve?, convoke?, shares?, mana_sources?: [card ids or {card_id, colors: ["R"]}], dry_run?}` - `keyword` (e.g. "Plot", "Foretell") announces a cast under a keyword permission (CR 601.2b); a suspended card's own free cast is picked up automatically once its last time counter is off, no `keyword` needed. `targets` is the spell's own targets (CR 601.2c) and never a cost: an alternative cost that names objects takes `choosing` (Solitude's `Evoke-Exile a white card from your hand`, Force of Negation's pitch), and a printed "as an additional cost to cast this spell" sentence takes `sacrificing`/`discarding`/`exiling`/`tapping` (CR 118.8a - Fling, Cathartic Reunion, Abhorrent Oculus, Swallow Whole). `GET .../affordances/{card_id}` names, per option, how many objects are needed (`requires`) and which cards qualify (`candidates`). `spell_face` is which face this cast announces - a half of a split card (CR 709.3) or an adventurer card's Adventure (CR 715.3) - by that face's printed name: the same string `GET .../legal-actions` put on the row being taken, handed back unchanged; absent means the front half, which is every single-faced card and an adventurer card's own creature half. An Adventure is charged its own cost, targets off its own text, and is exiled as it resolves (CR 715.3d) |
| `POST /api/games/{id}/actions/target` | `{source_id, target, ability_index?}` - a spell on the stack, a card in hand, or an activated ability line |
| `POST /api/games/{id}/actions/activate` | `{player, card_id, ability_index? (default 0), targets?, mana_sources?, sacrificing?, discarding?, dry_run?}` - mana abilities resolve straight into the pool; a card anywhere other than the battlefield (hand, graveyard, exile) whose own ability functions from there (CR 113.6j, CR 113.6m - Waker of Waves, Boseiju's or Otawara's Channel) is activated the same way, `ability_index` numbered off that card's own state view |
| `POST /api/games/{id}/actions/suspend` | `{player, card_id | card, mana_sources?, dry_run?}` - CR 116.2f/702.62a: exile a card with suspend from hand, with N time counters |
| `POST /api/games/{id}/actions/plot` | `{player, card_id | card, mana_sources?, dry_run?}` - CR 116.2k/702.170b: exile a card with plot from hand |
| `POST /api/games/{id}/actions/foretell` | `{player, card_id | card, mana_sources?, dry_run?}` - CR 116.2h/702.143b: pay {2}, exile a card with foretell from hand face down |
| `POST /api/games/{id}/actions/cycle` | `{player, card_id | card, ability_index? (default 0), mana_sources?, dry_run?}` - CR 702.29a: discard a card with cycling from hand to draw (or typecycle); puts the ability on the stack. Only for an actual cycling ability - a card that discards itself as an activated ability's cost without being cycling (Waker of Waves, Channel) is CR 113.6j's more general shape and goes through `actions/activate` instead |
| `POST /api/games/{id}/actions/attack` | `{attackers: [{card_id, defender: username or planeswalker card_id}], dry_run?}` |
| `POST /api/games/{id}/actions/play-land` | `{player, card_id | card}` - the land-drop rules come with it |
| `GET /api/games/{id}/legal-actions?player=&uid=` | `{actions: [Action, ...]}` - every action `player` could legally take right now (`machines.legal_actions`), not just the ones above: `pass_priority`/`concede` always included, `answer_decision`-only while a decision is pending for that player. Each `Action` carries `affordable: "yes"\|"no"\|"unknown"` and `unaffordable_reason` alongside its `kind` (CR 118.6: an unpayable action is still a legal one, so nothing here is dropped for being unaffordable) - what `seatkit.available_actions` calls instead of dry-running every candidate itself. `uid` must open `player`'s own seat (or be the game's admin key): the answer is that seat's hidden information - each hand card by id, its cost, its origin zone and whether it can be paid for - so any other caller is refused with 400 rather than answered empty, which an empty pool would be indistinguishable from |

### Running the game forward

| | |
|---|---|
| `POST /api/games/{id}/resolve-top` | resolves the top of the stack. **Not a seat verb**: CR 117.4 resolves the top because every player passed in succession, so a seat reaches this through `actions/pass-priority`. Kept as the harness's own way to drive and observe one resolution at a time |
| `POST /api/games/{id}/state-based-actions` | counter state-based actions, then the full check. Optional body `{player?, reasoning?}` - called with no body at all it is nobody's action and requires nothing |
| `POST /api/games/{id}/advance` | one step, with turn-based actions; beginning-of-step triggers go on the stack the same as any other trigger, CR 702.62a's suspend countdown among them for every card the now-active player has suspended in exile - resolve them through `resolve-top`/`pass-priority` like any other ability, there is no separate report field for it. **Not a seat verb**: CR 703.2 says turn-based actions "are not controlled by any player", and a step ends because everyone passed (CR 117.4) or because it grants nobody priority and its own actions finished (CR 500.3, which `pass-priority` settles for the caller). Kept for the harness, which steps into untap and cleanup deliberately to observe them, and for the play UI's Advance button - the human's one escape from a priorityless step the engine declined to cross |
| `POST /api/games/{id}/decide?uid=` | `{player, card_ids: [...]}` - answers this game's pending decision; refused if `uid` is not that decision's own player |

### Decisions (CR 608.2d, mostly)

Some turn-based actions ask the player something instead of the engine
choosing for them - the cleanup discard (CR 514.1, 701.9b) is the first one.
Most of these questions are CR 608.2d; `choose_targets` is one exception
(CR 603.3d, CR 601.2c instead - see `decisions.rule_for_kind`), since it is
choosing targets *while* a spell or ability goes on the stack, exactly the
case 608.2d's own wording excludes; `declare_attackers`/`declare_blockers`
(CR 508.1, CR 509.1) are turn-based declarations rather than resolution
questions, and `mulligan` (CR 103.5) is asked outside the game entirely, but
all three are answered through the same mechanism as every other kind.

Answering is pause-and-resume, not blocking (`commands.py`). Any
engine-driving mutating request (`advance`, `pass-priority`, `cast`,
`activate`, ..., and `pregame/settle` with `use_defaults` left `false`) that
crosses a question with no scripted answer returns *immediately* -
`allowed: null, paused: true, pending_decision: {...}` - instead of parking
the request; the game itself is left exactly as it was before the attempt,
and the paused command is remembered so `POST /api/games/{id}/decide?uid=`
can resume it with one more answer appended. There is no server-side
decision timeout configuration any more, nothing ever silently takes the
engine's default in place of a live answer, and the state object carries no
list of defaulted decisions - a decision simply stays paused, however long
that takes, until a seat answers it through `decide`.
The state object's `pending_decision` field carries `{player, kind, prompt}`
for every seat, plus `cards` and `amount` (and the rest of the kind-specific
detail) for the deciding player (and the admin key) only - everyone else may
see that a choice is being made, never what it is a choice among.

**Nothing else can happen while a decision is pending.** Any other mutating
command attempted meanwhile is refused with `409` and
`{allowed: false, reason: ...}` in the engine's own words - nobody receives
priority part-way through another command (CR 608.2g and CR 117.3b
mid-resolution, CR 601.2i mid-announcement, CR 117.3a before a turn-based
action is dealt with). Re-posting the *same* command that is already paused
is not a competing command: it gets the standing question handed straight
back, so a polling client is never pushed into escalating. Conceding
(CR 104.3a, "at any time") and a `dry_run` probe are exempt. A pause the
committed state has genuinely moved past is cleared rather than stood on, so
this can never wedge a game.

The deciding seat's own `pending_decision` also carries `generation`, a
counter that advances only when the standing question is replaced. A
resolution can ask more than one decision - a fetchland's search, then the
shockland's "pay 2 life" - and accepting the first replaces the question
with the second immediately. Echo it back as `decide`'s optional
`generation` (or `seat_cli decide --generation`) and an answer aimed at the
question you read is refused rather than applied to whatever is standing
now; omit it and the check is skipped.

### The state object

`GET /api/games/{id}` returns the turn, priority holder, players (life,
counters, mana pool, zone counts), and every card in every zone with: id,
name, type line, zone, controller, owner, tapped, face up, counters, damage,
summoning sickness, resolved target names, Scryfall image links, and the
activated abilities the engine would let you index into. The stack comes
top-first; spells and abilities are distinguishable by `kind`. `pending_decision`
is `null` unless a decision (CR 608.2d, or CR 603.3d/601.2c for `choose_targets`)
is currently paused, waiting on an answer - see Decisions below.

`exile_pile` is the CR 406.4-marked cards sitting in `exile` (suspended, with
their time counters; foretold; plotted; a cascade hit awaiting its cast) -
the same card views `exile` already carries, filtered down to the ones with
a marker, so a seat does not have to scan the whole pile. `play_permissions`
is every live CR 601.3 grant (`PlayPermission` - cascade's "you may cast it
without paying its mana cost", suspend's own free cast), keyed by the player
who may use it: which card, from which zone, granted by what, whether it
pays the mana cost, and when it expires (`until_end_of_turn` and cleanup, or
"when used" for suspend's standing grant). No separate endpoint drives one -
`POST .../actions/cast` (no `keyword` needed) and `POST .../actions/play-land`
already pick up a live permission covering the card being cast or played, the
same way they pick up a suspended card's free cast; `play_permissions` exists
so a seat can *see* the grant it is about to use.

## Known gaps (engine coverage, mirrored here)

- The engine's effects registry is keyed by card name: an unregistered card
  resolves and does nothing.
- Announcing fewer targets than required is not rejected by the engine's
  targeting parser.
- Modal targeting checks go through the card's whole spell text; choose modes
  on the cast itself for exact modal answers.
