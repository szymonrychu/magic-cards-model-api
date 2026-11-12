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

Below "Keep priority by default", the stops panel has an **Automation**
group of three more ticks, all on by default: "Auto-answer forced choices"
answers a decision with only one legal answer for you instead of stopping to
ask; "Auto-pass when nothing is castable" passes straight through a stop
that offers nothing to do but pass, land plays and mana abilities included;
"Show auto-tapped mana" toggles the small toast that names which lands an
automatic payment tapped, for the moments an auto-tap should not be a
surprise. Untick any of the three to have that moment stop and ask instead.

**Always yield.** Every triggered or activated ability your opponent puts on
the stack gets its own small "Always yield" toggle beside it; ticking it
tells the UI to answer "no" to that exact trigger automatically from then on
- matched by controller, name and printed text together, so a same-named
ability with different wording, or the same ability under someone else's
control, is never swept in by mistake. The stops panel lists every trigger
you have set this way under the Automation ticks ("Always yield: `<name>`
(`<controller>`)"), each with its own remove control, so the whole standing
list is visible and reversible in one place without having to wait for the
trigger to show up on the stack again.

**Keyboard shortcuts.** F2 passes once and F6 runs Pass until, MTGO's own
bindings - each does exactly what its on-screen button does and only fires
while that button is actually offered (so F2 does nothing once you no
longer hold priority, and F6 does nothing once nothing is left to pass
until). Neither shortcut is offered while a text field has focus.

**Stops follow you.** Signed in, every stops setting above - the step grid,
the stack tick, Keep priority by default, the three Automation ticks and
the Always-yield list - is remembered on your account (`GET`/`PUT
/api/me/preferences`) and carried to any device you sign in on, the remote
copy winning whole over whatever this browser last held. Signed out, or
before that sync has ever run, it still falls back to this browser's own
`localStorage` copy exactly as described above.

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

**A game that ends retires itself.** Conceding is the obvious way to do that
(CR 104.3a - the Concede button, two clicks), and a lethal-damage sweep or a
draw is the same thing arrived at differently: the moment the game is over,
the pregame watcher stops and a `game_retired` row says so. Nothing else is
released. The AI seat (and a buddy, the same way) still has post-game work
to do on its own uid - the reflection turn, the Opus review, the handover -
and its pod releases itself when that is done. While it works the lobby row
reads "recording plays..." (`post_game_work` on `GET /api/lobby/games`).
The work is bounded by `lobby.POSTGAME_RELEASE_SECONDS` (about an hour after
the `game_over` row): past that, whatever is still running is hard-released
once and a second `game_retired` row says "post-game work overran". The game
itself is **not** deleted: it is still there to open, still refuses every
action with "the game is over", and still has a review. End game is the hard
stop at any time, and the only thing that clears the row.

**An unfinished handover gets one follow-up.** Every handover files a
`plays_outcome` row (`POST /api/games/{id}/plays-outcome`). When it says
`unfinished` - nudged, timed out, a write that did not land, development
still open after the re-push - the api schedules one **follow-up
DSL-development Job** for that seat or buddy (`lobby.schedule_followup`,
kubernetes mode only): a fresh `/guides` clone and one bounded Opus turn
(`AgentSettings.review_default()`, 45 minutes with a nudge at 40, the Job's
own `activeDeadlineSeconds: 3600`) that finishes the guide updates and the
mandatory script development. It runs as the original seat's (or buddy's)
own uid, so it can act as that player and nobody else. Its assignment is
built server-side from the ledger when it is scheduled - the deck, the
revealed opponent, the run, offer and hand-played tables and the unfinished
reasons - so a later archive costs it nothing. It files its own
`plays_outcome` row with `followup: true`, and that row never schedules
another: one follow-up per (game, uid), deduped by the Job's
`mtg/followup-of` label and by the ledger's `plays_outcome_followup` row.
Every other `AI_SEAT_MODE` records that row as `unavailable`. While it runs
the lobby row stays "recording plays...", bounded by the follow-up's own
window (`lobby.FOLLOWUP_RELEASE_SECONDS`); End game deletes the Job at once,
and Forget has reconcile delete it within a minute - its driver stops the
turn and files the outcome as `stopped`.

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
matchup notes and appended `origin: "seat"` rows, `NOTES.md`, a stub deck's
`NOTES.md` for an opponent archetype with no guide) and hand it over with
`mcp-seat guides propose --deck SLUG [--deck SLUG ...]`: render, gate, commit
and push straight to mtg-decks main, the same as `mcp-seat plays admit` does
for play scripts. Nobody reviews or merges either afterwards. When main moved
under the edit it is replayed onto it; a clash comes back as `conflict` with
the markers left in the files for the agent to resolve and run again, and
three lost push races come back as a retryable `error`.

After a game ends, the lobby reveals both decklists to the game's own seats,
and the driver runs the opponent's through the same classifier that picked
the seat's own guide. The reflection turn and the handover then name that
deck (slug, confidence, archetype, the seat's own plan row for it, and the
runners-up when the match is weak), and the handover makes three guide
updates a required step: the seat's own `decks/<own>/NOTES.md` (how the game
was won or lost, what to watch for), the opponent's `decks/<opp>/NOTES.md`
(their strategy; a stub deck when no guide matches, with the existing stubs
listed for reuse), and the plan's matchup row (or an appended `origin:
"seat"` row) on how to win the matchup - all landed by one `guides propose`
call. A mirror is one update under a single `--deck`; a seat whose own list
has no guide skips its own-deck update. The post-game reviewer gets the same
block. That handover runs at the natural
points where the agent stops playing: when a game ends, and when the driver's
`POST /release` is called (a human taking the seat back, or a buddy standing
down). A plain `docker compose stop` or `docker compose down` does **not**
run a handover - it just stops the containers, mid-match state and all;
`agent`/`driver` are given a 92-minute `stop_grace_period` (mirroring the k8s
Job's 5520 s `terminationGracePeriodSeconds`; the driver's handover itself
gives up at 28 minutes, after a push-now nudge at 23, and `retire` then joins
the post-game AI review's own thread for up to another 62 minutes) purely so
an *in-flight* handover or review from one of those two triggers has room to
finish before compose sends SIGKILL, not to start one on its own.

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
  reflection turn, the handover, and the retirement that stops the agent
  (the driver ends its own pod after the handover; the api no longer
  releases it at game over).
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

**Opus-vs-Opus in compose (DR7).** `docker-compose.yml`'s single `agent`/
`driver` cannot play *both* seats of one game either - a second dispatch to
the same driver hits the same "one container, one match" 409. A dedicated
`docker-compose.aivai.yml` (`mise run compose:aivai`) brings up two full
agent/driver pairs instead - `agent-p1`/`driver-p1` and `agent-p2`/
`driver-p2`, each with its own writable guides checkout (`agent-guides-p1`/
`agent-guides-p2`, so two git indexes never collide) and its own host port
(`8080`/`8081`) - plus `game` configured with `DRIVER_URLS` (a
comma-separated list, not the single `DRIVER_URL`): `ai_seat.driver_urls()`
reads it, and `_dispatch_to_driver` sends each seat to the first driver whose
own `/status` reports idle, retrying the next candidate on a `409` from a
race. Both seats default to `claude-opus-5` at `low` effort (`CLAUDE_MODEL`/
`CLAUDE_EFFORT`, same variables as the single-seat file) - a cheap, fast
self-play loop; the post-game review stays the fixed Opus-`high`
`agent_settings.review_default()` regardless. Bring it down with
`mise run compose:aivai:down`.

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
  admin key) is the only thing that deletes a game. It stops that game's AI
  seat - subprocess terminated, wrapper dispatch handed back to the driver,
  or kubernetes Job deleted - and deletes the game, freeing a slot under the
  cap. It never touches `./memory`. A game that reaches its own ending (a
  concession included) releases all the same runners by itself and keeps the
  row.
- **The cap**: `MAX_LIVE_GAMES` (env, default 4) counts lobby games that are
  not yet over; starting a new one past the cap is refused with a 409 naming
  the count. A finished game does not count against it; only End game frees a
  slot for one still in progress.

### Agent settings, per game

**2026-11-12 (owner, T-156): an AI seat may name its own `model` and `effort`.**
`NewUiPlayer` takes optional `model` (one of `MTG_SIM_CLAUDE_MODELS`) and
`effort` (`low`, `medium`, `high`, `xhigh`, `max`) on an `ai: true` seat (422
on a human seat or a value outside the lists); unset fields fall back to the
deployment default (`AGENT_DEFAULT_MODEL` / `AGENT_DEFAULT_EFFORT`, prod now
`claude-opus-5-5` at `low`). The seat's choice is recorded and kept across
`ai-seat/start`: a restart reuses the seat's recorded model and effort
(owner decision Q2), so an old game keeps the settings it was created with,
not the current default. The `agent`/`buddy_agent` objects stay refused. The history
below describes the earlier fixed-default rule.

**2026-09-19 (owner direction, cost cutting): model, effort and thinking
budget are a fixed server-side deployment choice, not a per-request one any
more.** `POST /api/ui/games`'s `NewUiPlayer` carries no `agent`/`buddy_agent`
field at all - a request that still sends one is refused (422, `extra=
"forbid"`) - and every AI seat and buddy alike always runs
`AgentSettings.default()` (`agent_settings.py`), shown back on `GET
/api/games/{id}`'s player block and in the lobby list above exactly as
before, just never chosen per game.

- `model`: `AGENT_DEFAULT_MODEL` (env, default `claude-sonnet-5`) - Sonnet,
  for an AI seat and a buddy alike, since Opus at the old default cost more
  than this deployment wants to spend on every turn of every game.
- `effort`: `AGENT_DEFAULT_EFFORT` (env, default `high`) - affordable now
  that play moved off Opus.
- `max_thinking_tokens`: still `null` always (`AgentSettings.default` never
  sets one) - a non-negative integer, or `null`, to leave Claude Code's own
  default. `0` disables thinking where the model allows it, for any code path
  that still constructs `AgentSettings` directly with one.
- **The post-game review is a separate, fixed choice: `claude-opus-5` at
  `high` effort, always** (`AgentSettings.review_default()`) - never read
  from `AGENT_DEFAULT_MODEL`/`AGENT_DEFAULT_EFFORT`, since those name what a
  seat *plays* with. See "Post-game AI review" below.

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

### Post-game AI review

When a game ends, the winning/losing/drawing seat's own driver already ran a
"reflection turn" (the seat's own Sonnet session writes a short account of
the game to its memory and posts it to `POST /api/games/{id}/reflection`,
`audit.REFLECTION_ACTION` - this is the "handoff", and it predates this
feature). Right after that reflection is posted, and before the agent
retires, the same driver now also generates a second, independent critique
on Opus (`AgentSettings.review_default()`, fixed - never
`AGENT_DEFAULT_MODEL`/`AGENT_DEFAULT_EFFORT`, which name the play default
only) and files it separately.

The mechanism (`mtg_agent/driver/review.py`, `mtg_agent/wrapper/oneshot.py`):

1. The driver fetches the game's own `GET /api/games/{id}/review` body (the
   same declarations/advice a human reviewer reads in the Replay's own
   thinking log - see below).
2. It builds a prompt from that plus the reflection text just posted
   (`review.review_prompt`), and POSTs it to the AGENT container's wrapper -
   `POST /v1/review` - since the driver's own container (`Dockerfile`)
   carries no `claude` binary; only the agent image
   (`Dockerfile.agent`) does.
3. The wrapper runs a single, bounded, non-PTY `claude --print` subprocess
   (`REVIEW_TIMEOUT_SECONDS`, default 1200s) - entirely separate from its
   supervised interactive session, which stays on whatever it was playing
   with and is never touched by this call. It runs in the agent's own
   workspace and HOME with a tool allowlist (`wrapper/oneshot.py`'s
   `REVIEW_TOOLS`/`REVIEW_ALLOWED_TOOLS`: the file tools, and Bash only for
   four `mcp-seat` subcommands), so the review can take play content back
   to the deck's guide the same way the seat's handover turn does. The two
   really do run at once, and `playscripts.gitwork.checkout_lock`
   serializes them on the `/guides` clone.
4. The result is filed under `POST /api/games/{id}/ai-review`
   (`audit.AI_REVIEW_ACTION`), shown last in the Replay's log - after the
   reflection, both written once the result is already known
   (`ui/replay_model.js`'s `logMessages`).

Every step is best-effort: an unreachable game server, an unreachable
wrapper, or a `claude` failure logs a warning and is simply skipped - never
blocks the handover or retirement that follows it. Not deduplicated across
seats (see `review.py`'s own module docstring for why); the Replay reads the
newest row of its kind rather than assuming exactly one exists.

### The Replay

A finished game's lobby row offers a "Replay" button (`reviewable(row)`,
`render/lobby.js`) instead of a Show/Open door onto a page of its own: it
opens the *same* game screen a live game uses, driven step by step from
`GET /api/games/{id}/review`'s ledger rather than from the poll. The pass/
phase controls (`#priority-box`/`#stops-box`/`#rail-pass`) are replaced by a
transport - step back, play/pause, step forward, a position counter, and a
scrubber (`#replay-bar`) - and the side panel's advisor becomes a thinking
log (`#replay-log`): every recorded reasoning, plan, advice, refusal,
reflection and AI review, `seq`-ordered and seat-coloured, with the current
step's own message marked. Autoplay starts the moment the door opens, one
step every 5 seconds, and stops at the last one; stepping manually never
pauses it, it only moves the next tick's window.

Every non-routine ledger row (a cast, an activation, a declaration, a
decide, a refusal - CR trainability is the whole point of this engine, so a
refusal is a step too) is a stop; a run of `advance`/`pass_priority` rows
folds into the *next* stop rather than becoming stops of its own
(`ui/replay_model.js`'s `replaySteps`). Both hands are shown face up
regardless of which seat this browser holds a uid for - a finished game's
review is full information by design.

Each step's board comes from one of two sources. A step whose `seq` is in
the review's own `exact_frames` (R2's own addition - the engine's exact
per-row state, kept in the `record_states` table and served only through
`GET /api/games/{id}/review/frames/{seq}`) fetches and shows that state
exactly, cached per `seq` and prefetched one step ahead so stepping forward
rarely waits. Every other step - which, for a game played before R2 landed,
is every step - falls back to the same text snapshot the ledger has always
carried (`audit.record`'s `serialize.snapshot`, parsed back into cards by
`ui/review_model.js`'s `frameState`): the same cards, tapped/counter/damage
state and board shape, just not byte-identical to a live `GameStateView`
(no exact attachment lines, exile with no owner). A response for a step the
viewer has already left is dropped rather than clobbering the one on
screen.

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

## Chat simulation MCP (`/sim/mcp`)

A bearer-protected MCP endpoint (streamable HTTP, stateless) for chat agents that want to study
a position: build a sandbox board from a spec and drive the real engine through it. It is a
sandbox only, never a live game: each sandbox runs in its own worker process with a stripped
environment, no database and a memory cap.

- Auth: `Authorization: Bearer <MTG_SIM_TOKEN>`. Unset token answers 503 (disabled), a wrong or
  missing one 401. The token comes from the `mtg-sim` Secret; it is not `OPS_TOKEN`.
- Reach: in-cluster only. `/sim` is denied at the public ingresses like `/metrics`.
- Tools: `sim_card_lookup`, `sim_new`, `sim_state`, `sim_legal_actions`, `sim_act`, `sim_snapshot`, `sim_restore`,
  `sim_run_script`, `sim_import_mtgo_log`, `sim_review_mtgo_log`, `sim_end`, `sim_match_claude_start`, `sim_match_claude_status`; and,
  only when `MTG_SIM_BASELINE_ENABLED` is set, `sim_play_new`, `sim_match`, `sim_match_status`.
  The engine baseline AI is off as an opponent by default: with the flag unset those three tools
  are not registered and `BatchManager.start` / `SandboxManager.new(mode="play")` refuse with a
  message naming the flag. `answers: "baseline"` of `sim_run_script` (the engine's default
  decision answer) and the MTGO import are not affected.
  Cards carry labels (`"label": "bolt"` in the spec, `"@bolt"` in action bodies).
- Card names: `sim_card_lookup(names)` checks up to 40 names through the Scryfall lookup the
  sandboxes use (cache first; exact, then fuzzy) and answers per name the canonical name, mana
  cost, type line, a short oracle text, power/toughness or loyalty and `engine_support`
  (`handled`, or the `support_warnings` status with the unsupported clauses), or `not_found`
  (unknown or ambiguous). It uses no sandbox slot.
- Snapshots: `sim_snapshot(sandbox_id, label=None)` saves the board (a pending decision
  included) inside the sandbox's own worker and answers its id (`s1`, `s2`, ...);
  `sim_restore(sandbox_id, snapshot)` (id or label) puts a fresh copy back, so the snapshot can be
  restored again after acting. A label used again replaces that snapshot. Board mode only, at most
  `MTG_SIM_MAX_SNAPSHOTS` per sandbox, gone when the sandbox ends. Every answer lists them under
  `snapshots`.
- Scenarios: `sim_run_script(script, keep=False)` builds the sandbox from `setup` and runs
  `steps` in one call, returning per-step results, the initial and final board and a diff. It
  stops at the first refused act, at a decision the next step does not answer (`answers:
  "stop"`, the default; `"baseline"` answers it with the baseline seat) or when the time
  budget is used up. A failed `expect` is reported, not fatal. The slot is freed afterwards
  unless `keep=true`, which returns a `sandbox` id to continue with `sim_act`. Example:

  ```json
  {"setup": {"players": [{"name": "alice"}, {"name": "bob"}],
             "cards": [{"card": "Lightning Bolt", "player": "alice", "label": "bolt"},
                       {"card": "Mountain", "player": "alice", "zone": "battlefield"},
                       {"card": "Grizzly Bears", "player": "bob", "zone": "battlefield",
                        "label": "bears"}],
             "mana_pool": {"alice": "R"}},
   "steps": [{"act": {"action": "cast", "player": "alice",
                      "body": {"card_id": "@bolt", "targets": ["@bears"]}}},
             {"act": {"action": "pass", "player": "alice"}},
             {"act": {"action": "pass", "player": "bob"}},
             {"expect": {"zones": {"bob.graveyard": ["Grizzly Bears"]}}}]}
  ```
- Batches (only with `MTG_SIM_BASELINE_ENABLED`): `sim_match(deck_a, deck_b, games=10, seed=None)` plays engine-only games between two
  decks (a bundled deck name or the decklist text), both seats the zero-LLM baseline policy,
  alternating who goes first, and reports wins, 95% intervals, draws, per-game rows and the
  seed. It answers within the call limit: `status: "running"` plus a `batch_id` means poll
  `sim_match_status(batch_id)`. One batch runs at a time (a second is refused with the time
  left); batches do not use the sandbox slot. `support_status` is `ok`, `pending` (not checked yet) or
  `unavailable` (lookup failed), and then `support_note` says results are not reliable. Caveats: the baseline plays weakly, so rates say
  little about real matchups, and a baseline game measured 85-140 s on the small fixture
  decks, and the bundled 45c_aggro vs 45c_control finished 0 of 10 games in 300 s (about 0.55 s
  per step). The default wall-clock cap is 1500 s (about 25 min, `MTG_SIM_MATCH_WALL_S`,
  configurable by env; owner decision Q7), so 10 games normally finish and the chat polls
  `sim_match_status` meanwhile; slow decks can still hit the cap: status `capped` returns the
  partial results of the finished games. Finished batches are kept
  86400 s (`MTG_SIM_RESULT_TTL_S`).
- Play mode (only with `MTG_SIM_BASELINE_ENABLED`): `sim_play_new(deck_me, deck_opponent, seed=None, me_first=None)` starts a game from
  two decks (a bundled deck name or the decklist text) where you hold the seat `you` and the
  zero-LLM baseline holds `baseline`. It uses the sandbox slot and its idle TTL (end it with
  `sim_end`). You see your own hand and only the opponent's hand count; setup and engine
  actions are refused and you can act only as `you`. Start with `sim_act` action `mulligan`,
  body `{"take": false}` to keep. After every accepted action and every `sim_state` the
  baseline plays on until it is your move: answers carry `your_turn`, `game_over` and
  `opponent_moves` (log headlines since your last call). A `note` says the opponent is still
  playing (call `sim_state` again; each call is bounded by the call limit) or that nobody can
  move (consider `concede`). The same `support_warnings`, `support_status` and `support_note`
  rules as the batches apply, covering both whole decks.
- MTGO logs: `sim_import_mtgo_log(log_text, upto_turn=None, run=False)` turns an MTGO game log
  (the client's `Match_GameLog` text, or the copied log) into a `sim_run_script` scenario. A log
  hides hands and libraries, so it is not replayed action by action: the board at the start of
  `upto_turn` (default the last turn; turns count across both players, 1, 2, 3...) is rebuilt
  and that turn's own lands, casts, activations, attacks and blocks become steps. Only the
  first game of a log is imported; lines of later games come back as unsupported with the reason
  "later game in the same log; import one game at a time", so split a multi-game log first.
  Limits: `log_text` over 1,000,000 characters is refused, an entry over 2000 characters is
  never matched, and at most 200 unsupported lines (300 characters each) are echoed, the rest
  only counted in `unsupported_omitted`. Exactly two players are required. Every line that could
  not be used is listed in `unsupported` with `line_no`, `text` and `reason`; nothing is dropped
  silently. `board_caveats`: MTGO logs carry no life changes, damage or deaths, so creatures that
  died may still be on the rebuilt battlefield and life totals stay at 20 unless a logged line
  changes them. Correct the setup before trusting a result. `run=true` also runs the scenario
  (it uses the sandbox slot and ends it) and adds `run` with the `sim_run_script` answer.
  `player` (a player name as in the log) rebuilds the turn from that seat: the opponent's hand
  cards the log first shows in that turn stay out of the setup, and an `add_card` step (which
  takes a `label`) adds each one right before the opponent plays or casts it, so the opponent's
  own actions still come from the log. Cards the opponent revealed earlier or got back to hand
  stay in the setup. `perspective` in the answer says how many cards were hidden; without
  `player` the scenario names every card the turn uses.
- MTGO review: `sim_review_mtgo_log(log_text, player)` answers at once (no worker, no slot) with
  a compact per-turn timeline of the first game seen from `player`'s seat: `players`, `you`,
  `opponent`, `winner` (if logged), `turns`, `notes`, `pregame` and `timeline`. Each turn has the
  active player, the events in log order as `"<line>: <text>"` (reminder text dropped),
  `opp_known_hand` (the opponent's hand cards known at the end of the turn, from full-hand
  reveals and bounces, minus what they played since) and `unmapped` (lines the parser could not
  read, with the reason). Opponent cards are named only from the line that made them public;
  in a line the parser could not read and that is not the reviewed player's, a card name stays only if an earlier line made it public (else "a card"). The full game is the default view; optional `upto_turn` cuts the timeline after that turn and gives no winner. Export
  headers (which name both decks) lose their text. MTGO logs no life totals, so there are none.
- Claude matches: `sim_match_claude_start(deck_a, deck_b, seed=None, model=None, effort=None,
  seat_a=None, seat_b=None)` starts one game between two
  Claude AI seat Jobs (kubernetes seat mode only) and answers a `match_id` at once. `model` and
  `effort` apply to both seats (defaults `claude-opus-5-5` at `low`); `seat_a` / `seat_b`
  (`{"model": ..., "effort": ...}`) override one seat. A model outside `MTG_SIM_CLAUDE_MODELS` or an
  effort outside the five levels is refused, and the start and status answers echo each seat's
  model and effort;
  `sim_match_claude_status(match_id)` answers from the last background check (every 30 s). One
  match at a time. A game takes about 33-55 min, and the match is bounded by the seat deadline
  (`MTG_SIM_CLAUDE_DEADLINE_S`), not by the engine-only batch cap.
- Caps, all read by `sim/config.py` (`SimLimits.from_env`) from the environment of the
  `mtg-stack` pod:

  | Env | Default | Meaning |
  |---|---|---|
  | `MTG_SIM_TOKEN` | none | bearer token; unset refuses every call (503) |
  | `MTG_SIM_BASELINE_ENABLED` | off | register `sim_match`, `sim_match_status`, `sim_play_new` (engine baseline AI as opponent); `1`/`true`/`yes`/`on` turn it on |
  | `MTG_SIM_MAX_SANDBOXES` | 1 | live sandboxes (also play games) at once |
  | `MTG_SIM_SANDBOX_IDLE_TTL_S` | 1800 | idle seconds before a sandbox is reaped |
  | `MTG_SIM_CALL_TIMEOUT_S` | 25 | a tool call never blocks longer; long work answers `running` |
  | `MTG_SIM_MAX_MATCH_GAMES` | 10 | games per `sim_match` |
  | `MTG_SIM_MATCH_WALL_S` | 1500 | wall clock of one engine-only batch; set by env (Q7 decided 1500 s) |
  | `MTG_SIM_MAX_BATCHES` | 1 | engine-only batches at once |
  | `MTG_SIM_MAX_SCRIPT_STEPS` | 200 | steps of one `sim_run_script` scenario |
  | `MTG_SIM_WORKER_MAX_RSS_MB` | 1024 | resident memory cap of a sim worker process |
  | `MTG_SIM_MAX_CLAUDE_MATCHES` | 1 | Claude matches at once |
  | `MTG_SIM_CLAUDE_DEADLINE_S` | 14400 | seat deadline of a Claude match (not the batch cap) |
  | `MTG_SIM_CLAUDE_MODEL` | claude-opus-5-5 | default model of a Claude match seat |
  | `MTG_SIM_CLAUDE_EFFORT` | low | default effort of a Claude match seat |
  | `MTG_SIM_CLAUDE_MODELS` | claude-opus-5-5, claude-opus-5, claude-sonnet-5-5, claude-sonnet-5, claude-haiku-4-5 | allowed models (comma list) |
  | `MTG_SIM_RESULT_TTL_S` | 86400 | how long finished batch and match results are kept |
  | `MTG_SIM_MAX_SNAPSHOTS` | 16 | snapshots per sandbox (`sim_snapshot`) |

### Preset entry (operator)

The tools reach an agent through its preset. Add this to the `mtg`, `marek-mtg` and any future
MTG preset (the orchestrator or a human applies it). The `mcp_servers` entry and the `env` entry reach
new sessions only; existing MTG sessions must be recreated. Claude Code expands
`${MTG_SIM_TOKEN}` in the header from the session env, which the plain `env` entry fills:

```json
"wrapper": {
  "mcp_servers": {
    "mtg-sim": {
      "type": "http",
      "url": "http://mtg-stack.mtg-engine.svc.cluster.local:8000/sim/mcp",
      "headers": {"Authorization": "Bearer ${MTG_SIM_TOKEN}"}
    }
  }
},
"env": {"MTG_SIM_TOKEN": {"value": "<token>", "expand": false}}
```

The token is a plain preset env, not an agents-vault entry (decision Q8=A, T-123): it is an
internal token the hive generates itself, agents cannot create vault entries, and the value is
accepted as readable in preset views and the orchestrator database.

If the preset sets `wrapper.permissions.allow`, a human must add `mcp__mtg-sim__*` there:
agents may not widen their own allow lists. Creating and rotating the token (Secret `mtg-sim`
is the same file; the value goes to the preset env) is in `deploy/k8s/README.md`.

### Usage guide (for the agent)

- Questions about an interaction or a position: `sim_new` with a minimal board (only the cards
  that matter), `sim_act` with `dry_run` first, then the real act, `sim_state` to read the board,
  `sim_end` when done. Free the slot always.
- A board photo with "what should I play now?": read the zones from the photo (both battlefields
  with tapped state and counters, graveyards, exile, your hand, life totals, the phase if told;
  crop per zone when cards are small), verify every name with `sim_card_lookup` and flag
  `not_found` or doubtful cards, then build the board with `sim_new` (`tapped`, `counters`,
  `face_up`, and `library_filler` so a draw step does not lose to an empty library). The
  opponent's hand is unknown: never invent cards for it. `sim_snapshot` the root, try each
  candidate line with `sim_act`, read the end state (life, board, cards, mana left), `sim_restore`
  the root and try the next; weigh what the opponent's visible open mana and colours could answer.
  Reply with the recognised board and its doubts, the best play as ordered steps, a short why,
  the alternatives considered and caveats (unsupported cards, guesses).
- Reproducing a game someone described: write the scenario and call `sim_run_script`.
- An MTGO log the user pasted: `sim_import_mtgo_log`, read `unsupported` and `board_caveats`,
  tell the user what could not be used and fix dead creatures and life totals in the setup, then
  call again with `run=true` (or feed `scenario` to `sim_run_script`).
- Reviewing an MTGO game: never review in the main agent. Hand the review to a SUBAGENT (the
  chat agent's own Agent/Task tool); the subagent calls `sim_review_mtgo_log(log_text, player=...)`
  with the user's player name as in the log. Skip obvious or forced decisions (the only land drop, the only
  legal play, a forced discard). Name only the decisive moments, the turns that swung the game
  in life, board, cards or tempo, and the mistakes; for each give the turn, what happened, the
  better line and why. The review subagent judges each decision with what the player could see,
  the information public up to that turn: their own cards, the public events so far and `opp_known_hand`, never a
  card the log shows later (it may fetch the prefix with `sim_review_mtgo_log(..., upto_turn=N)`). To test an
  alternative line, `sim_import_mtgo_log(log_text, upto_turn=N, player=...)` (the opponent's
  unseen hand cards are added only when the log shows them played), then `sim_run_script` or
  `sim_run_script(keep=true)` plus `sim_act`. Keep the review short: a few lines per moment.
- List against list (only when the baseline is enabled): `sim_match`; if it answers `running`, poll `sim_match_status` every 20 s or
  so. Say that the baseline plays weakly and that a `capped` batch is partial.
- "Let me play it" (only when the baseline is enabled): `sim_play_new`, keep with `mulligan` `{"take": false}`, play with `sim_act`
  as `you`, finish with `sim_end`.
- A Claude-vs-Claude match only when the user accepts 33-55 min per game:
  `sim_match_claude_start`, then poll `sim_match_claude_status` every few minutes (about 300 s),
  never in a tight loop.
- Always relay `support_warnings` (and `support_note`) to the user. Never claim a result for a
  card the engine runs as silent; say the result is unreliable.
- When a tool refuses with "slot in use" (or a batch or match is already running), tell the
  user, say how long the holder has left, and retry later; do not loop.
- Tool answers never contain admin keys or seat uids; do not ask for them.

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
mise run test:pg # the same suite against real postgres (docker compose)
mise run lint    # ruff
mise run fmt     # ruff format + fix
```

`main` holds only stable, fully tested, green code, and only that is
deployed. Before any merge to `main`, `lint`, `test` and `test:pg` must be
green on the rebased branch, against the engine revision it will ship with
(`../magic-cards-model` at its own green `main`, whose `test:full` must be
green too).

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
| `POST /api/games/{id}/actions/suspend` | `{player, card_id | card, x_value?, mana_sources?, dry_run?}` - CR 116.2f/702.62a: exile a card with suspend from hand, with N time counters; `x_value` is a "Suspend X" card's announced X (its time counters and the X in the cost, required for such a card; the hand card's `special_actions.suspend` is listed at its least X) |
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

## Fan Content notice

unofficial Fan Content permitted under the Fan Content Policy. Not approved/endorsed by Wizards. Portions of the materials used are property of Wizards of the Coast. (c)Wizards of the Coast LLC.

Card data comes from [Scryfall](https://scryfall.com).
