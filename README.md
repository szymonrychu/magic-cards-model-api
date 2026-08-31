# magic-cards-model-api

A self-test API over the [magic-cards-model](../magic-cards-model) rules
engine. Set up any board state, ask whether an action is allowed, and get the
engine's own refusal text back when it is not. A crude MTGO-style frontend is
served at `/` from the sibling repo `../magic-cards-model-test-frontend` when
it exists.

## Play it - Human vs AI

`http://127.0.0.1:8000/play/` is a small, buildless, server-rendered UI
(`ui/`, plain HTML/CSS/JS - no npm, no framework) over exactly the seat API
below: load a bundled decklist from `decks/` or paste/upload your own,
validate it (`Deck.check_legality`, CR 100.2a/100.4a), and start a game where
you click one seat and a headless `claude` subprocess plays the other,
driving `seat_cli` the same way a human dispatched to that seat would. If
`claude` is not on `PATH`, the UI falls back to showing the exact command and
prompt to run by hand in another terminal - the game is fully playable either
way. See `ai_seat.py` and `docs/ai_seat_brief.md` for the mechanism and the
brief the agent is given.

## Play against a continuous agent

`docker compose up --build` brings up three containers: the game (this repo,
with the `/play/` UI), an **agent** running one interactive `claude` under a
PTY for the length of one match, and a **driver** that tells the agent what it
is playing and recycles it between matches.

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
answers or the boot budget (`AGENT_BOOT_WAIT_SECONDS`, default 180) runs out.
A second *different* game arriving while a match is live is refused with a 409
naming the game in progress - the agent plays one at a time.

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
| play | http://127.0.0.1:8000/play/ |
| agent turns, probe, interrupt | http://127.0.0.1:8080/v1/... |
| which deck the driver identified | http://127.0.0.1:8090/status |
| the agent's notes | `./memory/` |

Set `AI_SEAT_MODE=subprocess` (the default outside compose) to go back to the
one-shot seat.

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
| `PUT /api/games/{id}/step` | `{step: "declare_attackers"}`; forward walks the turn machine, backward rewinds it raw |

Zones: `library`, `hand`, `battlefield`, `graveyard`, `stack`, `exile`,
`command`, `sideboard`. Shared zones (battlefield, stack, exile, command)
take the `player` as the controller. Counter kinds: `+1/+1`, `-1/-1`,
`loyalty`, `poison`, `energy`, `experience`, `time`, `charge`.

### The heart: legality questions

Each returns `{allowed: bool, reason: str | null, ...}` and performs the
action only when allowed and `dry_run` is not set.

| | |
|---|---|
| `POST /api/games/{id}/actions/cast` | `{player, card_id | card, from_zone?, targets?: [card ids or usernames], modes?: [1,2], x_value?, keyword?, mana_sources?: [card ids or {card_id, colors: ["R"]}], dry_run?}` - `keyword` (e.g. "Plot", "Foretell") announces a cast under a keyword permission (CR 601.2b); a suspended card's own free cast is picked up automatically once its last time counter is off, no `keyword` needed |
| `POST /api/games/{id}/actions/target` | `{source_id, target, ability_index?}` - a spell on the stack, a card in hand, or an activated ability line |
| `POST /api/games/{id}/actions/activate` | `{player, card_id, ability_index? (default 0), targets?, mana_sources?, sacrificing?, discarding?, dry_run?}` - mana abilities resolve straight into the pool |
| `POST /api/games/{id}/actions/suspend` | `{player, card_id | card, mana_sources?, dry_run?}` - CR 116.2f/702.62a: exile a card with suspend from hand, with N time counters |
| `POST /api/games/{id}/actions/plot` | `{player, card_id | card, mana_sources?, dry_run?}` - CR 116.2k/702.170b: exile a card with plot from hand |
| `POST /api/games/{id}/actions/foretell` | `{player, card_id | card, mana_sources?, dry_run?}` - CR 116.2h/702.143b: pay {2}, exile a card with foretell from hand face down |
| `POST /api/games/{id}/actions/cycle` | `{player, card_id | card, ability_index? (default 0), mana_sources?, dry_run?}` - CR 702.29a: discard a card with cycling from hand to draw (or typecycle); puts the ability on the stack |
| `POST /api/games/{id}/actions/attack` | `{attackers: [{card_id, defender: username or planeswalker card_id}], dry_run?}` |
| `POST /api/games/{id}/actions/play-land` | `{player, card_id | card}` - the land-drop rules come with it |

### Running the game forward

| | |
|---|---|
| `POST /api/games/{id}/resolve-top` | resolves the top of the stack |
| `POST /api/games/{id}/state-based-actions` | counter state-based actions, then the full check |
| `POST /api/games/{id}/advance` | one step, with turn-based actions; landing on `upkeep` also drives CR 702.62a's suspend upkeep trigger for every card the now-active player has suspended in exile, reported as `suspend_upkeep: [{card_id, card, time_counters, cast_permission_granted?}]` (`PUT .../step` does the same when it lands on `upkeep`) |
| `POST /api/games/{id}/decide?uid=` | `{player, card_ids: [...]}` - answers this game's pending decision; refused if `uid` is not that decision's own player |

### Decisions (CR 608.2d, mostly)

Some turn-based actions ask the player something instead of the engine
choosing for them - the cleanup discard (CR 514.1, 701.9b) is the first one.
Most of these questions are CR 608.2d; `choose_targets` is the one exception
(CR 603.3d, CR 601.2c instead - see `decisions.rule_for_kind`), since it is
choosing targets *while* a spell or ability goes on the stack, exactly the
case 608.2d's own wording excludes. When a question comes up, the mutating
request (`advance`, so far) blocks - starting once the deciding seat's own
state read has shown it the question (`decisions.DecisionMailbox.mark_delivered`),
not from the moment it was raised - for a server-side timeout (5 minutes by
default, `MCM_DECISION_TIMEOUT_SECONDS`) waiting for a seat to answer through
`decide`; on timeout it falls back to the engine's documented default and
writes a `decision_timeout` line to the game log *and* the state object's own
`decision_timeouts` list (naming the default taken and the rule), because a
default is a harness failure standing in for a real answer and should not be
readable only by digging through the log. The state object's `pending_decision`
field carries `{player, kind, prompt}` for every seat, plus `cards` and
`amount` for the deciding player (and the admin key) only - everyone else may
see that a choice is being made, never what it is a choice among.

### The state object

`GET /api/games/{id}` returns the turn, priority holder, players (life,
counters, mana pool, zone counts), and every card in every zone with: id,
name, type line, zone, controller, owner, tapped, face up, counters, damage,
summoning sickness, resolved target names, Scryfall image links, and the
activated abilities the engine would let you index into. The stack comes
top-first; spells and abilities are distinguishable by `kind`. `pending_decision`
is `null` unless a decision (CR 608.2d, or CR 603.3d/601.2c for `choose_targets`)
is currently blocked on an answer - see Decisions below.

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
