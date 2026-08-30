# magic-cards-model-api

A self-test API over the [magic-cards-model](../magic-cards-model) rules
engine. Set up any board state, ask whether an action is allowed, and get the
engine's own refusal text back when it is not. A crude MTGO-style frontend is
served at `/` from the sibling repo `../magic-cards-model-test-frontend` when
it exists.

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
  and cached to disk under `.scryfall-cache/`, so a card is fetched once.
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
`loyalty`, `poison`, `energy`, `experience`, `charge`.

### The heart: legality questions

Each returns `{allowed: bool, reason: str | null, ...}` and performs the
action only when allowed and `dry_run` is not set.

| | |
|---|---|
| `POST /api/games/{id}/actions/cast` | `{player, card_id | card, from_zone?, targets?: [card ids or usernames], modes?: [1,2], x_value?, mana_sources?: [card ids or {card_id, colors: ["R"]}], dry_run?}` |
| `POST /api/games/{id}/actions/target` | `{source_id, target, ability_index?}` - a spell on the stack, a card in hand, or an activated ability line |
| `POST /api/games/{id}/actions/activate` | `{player, card_id, ability_index? (default 0), targets?, mana_sources?, sacrificing?, discarding?, dry_run?}` - mana abilities resolve straight into the pool |
| `POST /api/games/{id}/actions/attack` | `{attackers: [{card_id, defender: username or planeswalker card_id}], dry_run?}` |
| `POST /api/games/{id}/actions/play-land` | `{player, card_id | card}` - the land-drop rules come with it |

### Running the game forward

| | |
|---|---|
| `POST /api/games/{id}/resolve-top` | resolves the top of the stack |
| `POST /api/games/{id}/state-based-actions` | counter state-based actions, then the full check |
| `POST /api/games/{id}/advance` | one step, with turn-based actions |
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

## Known gaps (engine coverage, mirrored here)

- The engine's effects registry is keyed by card name: an unregistered card
  resolves and does nothing.
- Announcing fewer targets than required is not rejected by the engine's
  targeting parser.
- Modal targeting checks go through the card's whole spell text; choose modes
  on the cast itself for exact modal answers.
