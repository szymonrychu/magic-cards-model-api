# Play UI and action protocol - design

Date: 2026-09-04. Repo: magic-cards-model-api. Research brief:
`docs/superpowers/research/2026-09-04-mtgo-like-play-ui-and-per-game-agent-pods.md`.

## Goal

Replace the play UI served at `/` (`/play/` redirects there) with one that
plays like MTGO: one click when there is nothing left to choose, targets
picked on the board per requirement slot, mana auto-paid with a preview and
a per-cast override, tiered priority passing with persistent stops, inline
combat declaration, and a game log. Fix the two reported defects on the way:
ability activation takes three steps, and a modal spell cannot take the same
player as the target of two different modes (Kozilek's Command).

Companion spec: `2026-09-04-per-game-agent-pods-design.md` owns game creation,
the lobby, and everything about AI seats. This spec is the in-game screen and
the API additions it needs.

## What is wrong today (verified in `ui/app.js`)

- Targets are a set: `toggleTarget` removes an id on its second click, so a
  duplicate across two modes is unexpressible. The affordance's per-candidate
  `requirements` slot indices are never read; `division` is never read.
- Every action ends in a ribbon confirm, including a land drop and a mana
  ability. A permanent with several abilities goes modal, then ribbon.
- `mana_sources` is never sent and `dry_run` never used, so the player taps
  each land through the same two-step flow before casting.
- Attackers and blockers are modal dialogs; "no blocks" does not pass.
- No game log; refusals go to three different sinks; the board is rebuilt
  from `innerHTML` every 1.2 s, so hover and focus are lost each tick.
- `target_minimum` is read but never served, so "exactly N" never renders.

## Decisions

Approved by the user on 2026-09-04: rewrite, still buildless (ES modules, no
npm, no framework); immediate actions when nothing is left to choose; per-slot
targeting; auto-pay with preview and click-to-pin override; tiered pass verbs
plus a persisted stops panel; additive API changes only; polling stays.

## Interaction model

### Hand

- Click a land: `play-land` fires at once. Refusal shows as a toast on the
  card and a log line.
- Click a spell: the card's own hints decide whether anything is left to
  choose (`cast.modes`, `cast.targeting.targets_anything`, `cast.needs_x`,
  `alternative_costs`, `object_costs`); when nothing is, a dry-run cast with
  auto-pay checks affordability and, if allowed, the cast fires at once. Otherwise the
  card lifts into a casting strip above the hand and the board becomes the
  prompt, in this order: modes, X, alternative or additional costs (convoke,
  delve, pitch), target slots, mana override. The cast fires the moment the
  last required slot is filled and the dry run is allowed; a Cast button
  exists only for "up to N" targets and divisions, where the player has to
  say they are done. Escape or clicking the lifted card cancels.
- Special actions (cycling, suspend, plot, foretell) are entries in the same
  inline menu as the card's cast option when the card offers more than one
  thing, read from `special_actions` rather than a hardcoded list.

### Battlefield

- Click a permanent you control with one activated ability: activate at
  once (mana abilities included). Colour choice for a dual land appears as
  two or three colour chips on the card, not a ribbon step.
- Several abilities, or an ability plus Crew: an inline menu anchored to the
  card lists each with its cost, text, and a greyed state carrying the dry
  run's refusal when it cannot be activated now.
- An ability that targets or needs X follows the same strip flow as a cast.
- The opponent's permanents are hoverable for preview only.

### Combat

- Declare attackers (your turn, `declare_attackers`, you hold priority):
  clicking a creature toggles it into the red zone row between the
  battlefields. A defender menu appears only when more than one defender is
  legal for it. "Attack" posts `actions/attack`; "No attack" passes priority.
  Right-click "attack with all" is out of scope.
- Declare blockers: click your creature, then the attacker it blocks; a line
  is drawn and the pair is listed. Clicking the pair removes it. "Block"
  posts `declare-blockers`; "No blocks" passes priority.
- Damage assignment order stays with the engine's decision prompt.

### Decisions

- Decisions render in the ribbon as today, one renderer per kind, pinned by
  `tests/test_ui_decision_coverage.py`.
- `choose_targets` uses the same board picker as casting, per slot.
- Scry and surveil keep the split renderer, and gain kept-card ordering by
  drag or arrow buttons, sending `top_order`.
- A decision shows the remaining time from `decision_timeouts`.

### Ribbon

Top to bottom: opponent and you info (life, counters, pool, hand and library
counts), the stack with each object's targets by name, the in-flight prompt
(casting strip details, decision, combat summary), the priority verbs, the
stops panel, and the game log. The log reads `GET /api/games/{id}/log?uid=`
incrementally by `seq` and shows engine refusals in red, exactly as the test
frontend does.

## Targeting model

- API: each entry of `requirements` gains `mode` (the 1-based mode number it
  came from, or null for a non-modal spell) and `mode_text`. The engine
  already computes this in `profile_for_modes_with_owners`; the API only has
  to stop discarding it. `candidates[].requirements` stays as is.
- UI: `targets` is a list of slots, one per requirement entry expanded by its
  `count` (an unbounded or "up to X" requirement is one growable slot). Each
  slot has a label built from `mode_text` and `phrase`. The active slot is
  the first unfilled one; clicking a slot chip refocuses it. A board click
  fills the active slot when the object's `requirements` contain that slot's
  requirement index. The same object may fill two slots of different
  requirements. Highlighting on the board follows the active slot.
- Sending: the flat `targets` list is built by walking the slots in
  requirement order, which is exactly the order the engine's slot assignment
  expects.
- Division: when the affordance carries `division`, each filled target gets
  a number stepper; the sum must equal the amount; the cast sends `shares`.
  `CastRequest` gains `shares: list[int] | None`, applied through
  `announce_division` after targets.

## Mana

- Default: the cast body carries every untapped mana source the player
  controls, as `seatkit.sources_or_all` does for the MCP and CLI.
- Preview: the dry-run response gains `tapped` from the engine's `auto_tap`
  result. Lands in that list show a tap preview while the strip is open.

  As shipped, each entry is `{card_id, name}`: an id and a name, no colour.
  This paragraph first asked for a `mana` field beside them, and it is not
  there. The engine's `auto_tap` result does not hand one back per source,
  and the preview does not need it - its whole job is to light the right
  cards up on the board, and the board already draws each land's own mana
  symbols. `tests/test_dry_run_tapped.py` pins the two-key shape.
- Override: clicking an untapped land while a cast or activation is pending
  pins it as a `ManaSource` with the colour picked from its chips; pinned
  sources go first in `mana_sources`, the rest of the untapped sources
  follow so the engine still fills the remainder. Clicking a pinned land
  unpins it. Clicking a land with no pending cast activates it as today and
  floats the mana.

  As shipped, "the colour picked from its chips" is a menu, not chips: a land
  that makes more than one colour opens the same anchored popover a
  multi-ability permanent opens, listing "Tap for {B}" / "Tap for {G}"
  (`app.js`'s `pinLand`). Nothing is drawn on the card itself. It is anchored
  to the card and it is not a ribbon step, which is what the Battlefield
  section above was actually asking for, so the deviation is in the
  affordance and not in the flow. Confirmed in a browser in Task 19.
- The floating pool is always visible in the you-info row.

## Priority

- Verbs (buttons in the ribbon, no key bindings encoded): Pass; Pass turn
  (auto-pass while the stack stays empty this turn); Pass turn regardless
  (auto-pass this turn even with objects on the stack, still stopping for
  decisions and your own declare steps); Pass until my next turn; Pass until
  I can cast something (auto-pass while a dry run of every hand card and
  ability is refused); Stop.
- Stops panel: a grid of steps by my turn and opponent's turn. A checked
  stop means auto-pass halts when priority arrives there. Defaults: my
  precombat main, my declare attackers, my postcombat main, opponent's
  declare attackers, opponent's end step. Stored in `localStorage` under a
  versioned key.
- Auto-pass always stops when you are attacked, when a new object appears on
  the stack, and when a decision is pending, as `autopilotStopReason` does
  today; that predicate moves to `priority.js` and keeps its unit tests.
- Auto-pass sends one `pass-priority` per tick and reads `outcome` and
  `settled_through` from the response instead of only `allowed`.

## Protocol additions (all additive)

| Change | Where |
|---|---|
| `requirements[].mode`, `requirements[].mode_text` | `affordances._profile_view`, `candidates` |
| `tapped` in dry-run detail of cast and activate | `engine_api._drive_cast`, `activate_ability` |
| `dry_run` on `play-land` | `PlayLandRequest`, `engine_api.play_land` |
| `shares` on `CastRequest` | `schemas.py`, `_drive_cast` |
| `targets` on stack objects as `[{kind, id, name}]` next to the existing names | `serialize._card_view`, `_ability_view` |
| `target_minimum` on `pending_decision` | `PendingDecision.to_view` |
| `GET /log` accepts `since=<seq>` | `app.py`, `audit.entries` |

`seatkit`, MCP, and CLI keep working unchanged; `PROTOCOL_VERSION` does not
bump because nothing they read changes shape.

## Code structure

```
ui/
  index.html          shell, script type=module
  style.css
  app.js              boot: lobby or game, polling loop
  api.js              fetch wrapper, uid handling, refusal -> log
  store.js            state, subscribe, last-known state per tick
  dom.js              keyed children reconcile, event delegation
  actions.js          the one state machine: idle | casting | activating |
                      attacking | blocking | deciding, plus slot logic
  targeting.js        slots from an affordance, fill/unfill, flatten, highlight
  mana.js             sources_or_all, pin/unpin, preview from tapped
  priority.js         stop predicate, verbs, stops panel model
  render/
    board.js hand.js stack.js ribbon.js combat.js decisions.js log.js
```

`dom.js` reconciles children by `data-key` so a card element persists across
ticks; hover preview and open menus survive. Each module exports pure
functions with no DOM access except `render/*` and `dom.js`, so
`actions.js`, `targeting.js`, `mana.js`, and `priority.js` load in Node.

## Testing

- Node-run unit tests through pytest, as `tests/test_ui_*.py` do today:
  slot filling with a duplicate player across two modes of Kozilek's
  Command, flattening order, division sums, mana pin ordering, stop
  predicate, stops persistence, "nothing left to choose" detection.
- pytest scenarios for every protocol addition, including a full
  Kozilek's Command cast with `targets=[alice, alice, ...]` through the HTTP
  API and its resolution.
- `tests/test_front_end_parity.py` and `test_ui_decision_coverage.py` keep
  passing; renderers exist for every decision kind.
- Manual browser pass with Chrome for each flow before merge: land, plain
  cast, modal cast with a repeated player, activation with one and several
  abilities, mana pin, attack, block, scry, pass verbs and stops.

## Out of scope

SSE or websockets; key bindings; right-click menus; the lobby and AI seat
panel (companion spec); mobile layout.
