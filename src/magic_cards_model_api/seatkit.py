"""One seat's proxy onto the API, and the "one call, one meaningful action"
flow both player-facing transports (`mcp.py`, `seat_cli.py`) build on.

`Seat`/`Seats` used to live in `mcp.py`, with `seat_cli.py` importing them
from there; they move here because both transports now also need the action
flow below, and putting it in the module that names the MCP tools would make
`seat_cli` import transport code to get non-transport logic. This project has
twice shipped two implementations of one rule that quietly drifted apart (a
duplicated mana parser, client-side visibility filtering that diverged from
the server); the fix both times was one shared module, which is what this is.

## The flow: `run_action`

CR 117.3c: "If a player has priority when they cast a spell, activate an
ability, or take a special action, that player receives priority afterward."
The engine already does this (`Game.retain_priority`, called from
`engine_api.cast_card`/`activate_ability`) - a player who acts is legally
entitled to act again before passing. Auto-passing right after one action is
therefore a **harness convenience layered on top of a correct rule**, not a
rule itself: two agents playing through a seat found it chatty and error-prone
to separately call an action, then `pass`, then `wait`, then re-fetch state, so
`run_action` folds those into one call. `hold_priority=True` is the escape
hatch back to the real rule - keep priority and decide what to do next, the
same as a human player would after casting a spell they mean to protect.
Recorded honestly in `magic-cards-model/docs/engine-coverage.md` rather than
left looking like a rule of its own.

The convenience is only ever right for an action that *uses the stack*, where
releasing is how the thing you just did gets to resolve. Two families of
action do not use the stack, and auto-passing after one of them costs the
seat the rest of its own phase for nothing:

- **special actions** (CR 116.1: "special actions are actions a player may
  take when they have priority that don't use the stack"), which CR 116.3
  answers directly - "if a player takes a special action, that player receives
  priority afterward". Both transports therefore default those tools to
  holding: `play_land` (CR 116.2a), `suspend` (CR 116.2f), `plot` (CR 116.2k),
  `foretell` (CR 116.2h). `cycle` holds with them, though cycling is *not* a
  special action - CR 702.29a makes it an ordinary activated ability, so its
  warrant is CR 117.3c rather than CR 116.3.
- **mana abilities** (CR 605.3b: an activated mana ability "doesn't go on the
  stack ... it resolves immediately after it is activated"). `activate` cannot
  default on that, since it is one tool for every ability and only the answer
  says which kind it was, so `run_action` holds on the outcome instead - see
  `added_mana`.

## Wake conditions

A blocked call returns as soon as one of these holds, for this seat:

- the game is still in the pregame - CR 103.5's mulligan step or CR 103.6's
  opening-hand step, which a game booted from decklists rests in before
  CR 103.8's first turn. Nobody has priority there, so every other condition
  below is unreachable and a seat waiting for one would wait out its whole
  bound while the game waited on it;
- it has priority *and* the visible board changed since the call began (not
  merely "has priority" - that would also fire the instant this seat's own
  action hands priority right back with nothing new to react to);
- a pending decision (CR 608.2d for most kinds; a `choose_targets` decision is
  CR 603.3d/601.2c instead, `declare_attackers`/`declare_blockers` CR 508.1/
  509.1) is addressed to it;
- the game is over (a winner is set, or it is a draw);
- the step gives nobody priority (untap, cleanup) and this seat is the active
  player - the harness has no other way to make the turn proceed but calling
  `advance`.

## Why this cannot deadlock

`block_until_wake` polls `GET /api/games/{id}` - a read that takes no lock
beyond what that endpoint already takes internally, and releases it before
returning. It never holds `store.locked(game_id)` (`store.py`) across the
poll loop. `hold_priority=True` additionally guarantees the *action* path
never enters `block_until_wake` at all: the check happens before the loop is
ever reached, by construction, not by timing.

This used to be wrong in one more place, past the loop, in a shape specific
to the old blocking decision transport: every return path of `run_action`
called `available_actions(seat, state)` unconditionally, and *that* function
issued `POST .../actions/cast?dry_run=true` for every card in hand (and
`.../activate` for every ability) - each of which takes `store.locked
(game_id)` (`app.py`). When this seat woke because a decision was now
pending *for it*, that lock used to not be free: it was held by the
opponent's request, parked deep inside `Game.ask` waiting on this same seat
to answer through `POST .../decide`. Calling `available_actions` there
queued this call behind the very decision it was returning to let the seat
answer, for the full decision timeout - a real deadlock, not a wait.

T11 removed the dry-run probe entirely: `available_actions` is now one `GET
.../legal-actions` call onto `machines.legal_actions.legal_actions` (Phase
1), which takes no lock at all - a pure read against the committed `Game`,
same as `GET /api/games/{id}` itself. The old deadlock shape cannot recur
because there is no longer a mutating request in this path to queue behind.
Whether a decision is pending for this seat is still read once, by the
server, before it enumerates anything - while one is outstanding, the answer
is that decision's own legal answers and nothing else (`legal_actions`'s own
module docstring) - which is correctness, not deadlock avoidance: nothing
else is legal while a decision stands, regardless of any lock.

## No transport-level block left to outlast

`block_until_wake` does one quick read and returns; so does `pass_until`,
which used to be the last deadline loop here (it slept until `max_seconds`,
so one MCP tool call sat open for as long as the opponent took to think).
`perform()` - one mutating call (`cast`/`activate`/`attack`/`pass-priority`/
`resolve-top`/`advance`) - used to be able to cross a live decision question
and block *inside that one request*, in `Game.ask`, for up to the server's
own `decision_timeout_seconds`; `Seat`'s HTTP client timeout used to be sized
to comfortably outlast that (`resolve_client_timeout`, since removed - see
`CLIENT_TIMEOUT_SECONDS`). That hazard is gone: a question with no scripted
answer now pauses and returns immediately instead of blocking the request at
all, so there is no long server-side hold for a client timeout to be shorter
than. `Seat.call_json` still reports a genuine read/write/pool timeout as
`UNKNOWN_OUTCOME` rather than raising - ordinary network jitter can still
happen - so a caller is never tempted to retry blind and risk a duplicate
cast/activation/attack.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

import httpx

DEFAULT_API = "http://127.0.0.1:8000"

#: Bumped whenever an action tool's contract changes shape. Every tool call in
#: `mcp.py` checks it against its own import of this constant (`api_trouble`,
#: below) and refuses that one call - loudly, naming both versions - on a
#: mismatch against whatever `/api/health` reports. Exists because the MCP
#: server is a long-running process spawned once at boot (`mcp.py`'s module
#: docstring) - editing this file after that process is already up does not
#: change what it is running, and without a check the first symptom used to
#: be a tool call failing in a way that looked like a bug in the call rather
#: than in the process serving it.
#:
#: This used to be a fatal check in `main()` before serving a single call -
#: correct in spirit (a stale server refusing to run beats a mysterious
#: failure later) but wrong in shape: the API is restarted once per playtest
#: game while the MCP process is spawned once for the whole session, so a
#: boot-time-only check meant the API being briefly unreachable at the wrong
#: moment (or, worse, at any later moment - the check never ran again) either
#: killed the MCP process outright or left it silently trusting a version it
#: had only confirmed once, forever. The check itself was right; only "fatal
#: at boot, never again" was the defect. See `api_trouble`.
#:
#: 2b T11: this used to be the value `/api/health` answered with too - the
#: api imported it straight out of this module, backwards (the api must not
#: depend on the future `magic-cards-mcp` package). The api now owns its own
#: copy (`magic_cards_model_api.protocol.PROTOCOL_VERSION`); this one stays,
#: independently, for exactly the comparison above - it has to be a value
#: baked into this build of the MCP process, not one read at the moment of
#: the check, or the check would trivially always agree with itself. The two
#: literals are kept equal by hand from here on - there is no import left to
#: do it for free once the repos actually split - the same way
#: `agent_settings.py`'s cross-repo spellings already are (MEMORY.md,
#: 2026-09-08). `tests/test_protocol_boundary.py` pins the two together.
#:
#: Bump this alongside any change to a tool's request/response shape.
PROTOCOL_VERSION = "seatkit-19"
#: `seatkit-18` is an **additive request** change and an **additive
#: response** one: a new `equip` tool over a new `actions/equip` route, a
#: legal-action row of kind `equip`, and rows gaining `printed_cost`,
#: `x_costs` and `unreadable_modes`; `cast` gained `improvise` (the CLI
#: `--improvise`), a legal-action cast row gained `improvise`, and every
#: action route, `decide` and every dry run now answer a finished game with
#: a `409 {"type": "game_over", ...}`.
#:
#: Battle 3 found equip unreachable (B3-01): CR 702.6a's ability is a keyword
#: the engine builds (`equip_activation`), not one of the Equipment's
#: `activated_abilities`, so Blade of the Bloodchief had no row and `activate`
#: refused every index. `EquipRequest` names the Equipment and its target, the
#: row carries the cost and target candidates, and `seat_cli equip` takes the
#: same `card_id`/`--target`/`--sources` the tool does. Battles 3 and 4 also
#: found rows that contradicted the engine: a cast row now prices the total
#: cost the cast is charged (CR 601.2f) and keeps the old one as
#: `printed_cost` - an X row keeps its X and lists, as `x_costs`, each X a
#: floor or reduction charges differently; a modal row names each mode the engine cannot read as
#: `unreadable_modes`, and a cast choosing one is refused rather than spending
#: the card for nothing; a loyalty ability that adds mana is an `activate` row,
#: never `activate_mana` (CR 605.1a); and an ability is offered only from the
#: zone it functions in (CR 113.6). A paused read now carries the paused
#: board's step, priority holder and counters, and `wake_reason` names the
#: seat that owns a decision another seat's call raised.
#:
#: A bump, the same reasoning as `seatkit-17`: an MCP process built before it
#: has no `equip` tool, so a seat told to equip has no way to. An older build
#: meeting a row of kind `equip` passes it through untouched.
#:
#: Battle 6 found improvise unreachable (B6-05): the engine has paid CR
#: 702.126a's artifact taps all along (`Game.cast(..., improvise=...)`), but
#: `CastRequest` refused the field, so Kappa Cannoneer and Metallic Rebuke
#: could only be cast off lands. A seatkit-17 MCP process has no parameter to
#: send and keeps reading a castable row's `improvise` as unknown noise. The
#: same battle drove a game past its `game_over` row (B6-06); a seatkit-17
#: process reads the new 409 as an ordinary refusal through `allowed: False`,
#: which is safe, but only this build's prose says what it means. These rode
#: the same `seatkit-18` rather than a bump, so an MCP process built on the
#: first `seatkit-18` (equip only) misses `improvise` the same way.
#:
#: Battle 7 added two more response fields, also riding `seatkit-18`. B7-13:
#: a spell cast for X (Nature's Rhythm harmonized for X=8) showed `mana_value
#: 2.0` and no X anywhere on the stack; a stack `CardView` now carries
#: `mana_value` with the chosen X counted (CR 202.3e) and, when its cost has
#: an `{X}`, `x_value`. B7-11: `resolve-top`'s own response gained `changed`,
#: the marker `pass_priority`'s resolve outcome already carried, and that
#: marker now sees power, toughness, keywords, controller, player counters
#: and the stack. An older process passes the new fields through untouched,
#: so a seat on it simply does not see them.
#:
#: The battle 7 review rode `seatkit-18` too, with no request or response
#: shape changed. U1: a card's `controller` in every view is the computed one
#: after a control change (the api's game copies kept each card bound to a
#: stale duplicate board), and `declare_attacker`/`declare_blocker` rows now
#: appear while that declaration question stands. U2: `targets` on `cast` and
#: `activate` may name an ability on the stack by its stack `id`, the id the
#: legal-action candidates already listed. An older process sends the same
#: strings and reads the same fields.
#:
#: Battle 8 riveted the last hole in the same `game_over` refusal shape
#: `seatkit-18` first introduced (B6-06 above): the server's own 409 was
#: already right, but `run_action`'s refusal branch answered every `allowed:
#: False` alike, so a finished game still came back `held_priority: true`,
#: `wake_reason: "refused - nothing changed, still your move"` - contradicting
#: "the game is over" in the same answer (B8-07). It now recognises
#: `type: game_over` and answers `held_priority: null` with the server's own
#: reason instead. Purely a client-side reading fix - no request or response
#: shape changed, so an older MCP process reading a newer api's `game_over`
#: body is unaffected either way; this still rides `seatkit-18`. (B8-05, the
#: decklist parser's blank-line sideboard boundary, is a lobby/api-layer fix
#: with no seat-facing contract at all and touches no tool shape.)
#:
#: Battle 4 made combat damage assignment a decision (B4-06), also riding
#: `seatkit-18`: the engine's `ASSIGN_COMBAT_DAMAGE` kind (CR 703.4k, 510.1c,
#: 702.19b), asked when a blocked attacker has two or more blockers or
#: trample with power past its blockers' lethal damage. `decide` gained a
#: `damage` parameter (a JSON list of `{"target": ..., "amount": ...}`),
#: `seat_cli decide` the matching `--damage` flag, and a `pending_decision`
#: of this kind carries `lethal_damage` (aligned with `targets`) and
#: `trample`. An older process has no `damage` parameter to send, so it
#: cannot answer this kind and reads it as an unrecognised one.
#:
#: Found `pay_mana`'s named-sources answer unreachable: engine PR #17 (B6-08)
#: taught `pay_mana` (CR 118.12a's "unless its controller pays {N}") to accept
#: a tuple of mana-source ids alongside its plain yes/no, so CR 608.2g's
#: "activate mana abilities before taking that action" can be answered by
#: naming *which* untapped permanents to tap rather than only spending what
#: was already floating - but `DecideRequest` had no field for the tuple and
#: `PendingDecision` never carried the engine's own `Decision.mana_sources`,
#: so a seat could only ever answer the bare `accept` and let the engine tap
#: everything (or nothing). `decide` gained `mana_sources` (a subset of
#: `pending_decision.mana_sources`, legal only alongside `accept: true`), and
#: `pending_decision` itself gained `mana_sources` - the offered untapped
#: permanents - so `answer_with`'s hint can say the option exists at all.
#: `seat_cli decide` gained the matching `--sources`, following the same
#: "name none, let the engine pick" rule `--sources` already carries on
#: `cast`/`activate`.
#:
#: Nothing here is unsendable by an older build, unlike `improvise`: an MCP
#: process built before this has no `mana_sources` parameter on `decide`, but
#: the plain `accept` it already sends stays a real, legal answer - the
#: engine taps every offered source on its own, exactly as before. So this
#: rides the same `seatkit-18` for a different reason than `improvise`'s own
#: paragraph gives: not "no build shipped in between", but "no build is left
#: unable to express what the api still accepts".
#:
#: Engine M1e (task S1) also rides `seatkit-18`, additive response only.
#: Every `actions[]` entry's `target_slots[]` carries the engine's own
#: `candidates` (object ids) and `player_candidates` (usernames) beside
#: `phrase`/`minimum`/`maximum`, on a cast row and on every activation row -
#: battlefield, loyalty, off-zone and equip alike - and the `targets` block
#: is written from those same lists rather than a second walk of the board,
#: so a candidate listed is one the announcement accepts. The equip row is
#: the engine's own (`keyword: Equip`, no `ability_index`), still served as
#: `kind: equip`, listed once and priced. An older process ignores the two
#: new keys, so nothing it sends is refused.
#:
#: `seatkit-19` is a **response** shape change - a field removed, not added,
#: so it is a bump rather than a ride: a legal-action or `play_permissions`
#: entry's `bottom_if_unused` is gone. Battle 8's funnel commit took the
#: standing `PlayPermission` away from cascade, suspend, rebound, Mosswort
#: Bridge and the rest of the "cast/play it without paying its mana cost"
#: effects - each now casts inline while the permitting spell or ability is
#: still resolving (`Game.cast_during_resolution`/`may_cast_during_
#: resolution`, CR 608.2g) - and took with it the one cleanup sweep
#: (`Game._bottom_unused_cascade_permissions`) that ever set the field to
#: anything but its own default. Permanently `False` on every permission this
#: engine still grants, it carried nothing left to say, so it is dropped
#: rather than kept as a key that can only ever read one value. An older
#: process reading `bottom_if_unused` off a row gets a plain missing key
#: (Python's `dict.get` default, not a KeyError) - safe for any reader that
#: was already treating it as informational rather than branching on it, and
#: `mcp.py`'s own tool description never told a seat to.
#:
#: Riding the same `seatkit-19`, additive response only: the state view (and
#: `battlefield_state`'s own `known_cards` section) now carries `known_cards`,
#: keyed by the player who knows - `Game.known_to`'s read of a hidden card's
#: live position (CR 401.2, CR 400.7), the same knowledge policy battle 5's
#: public-zone-to-hand tracking already used, extended by battle 8 (B8-03) to
#: a card put into a library from a public zone. An older process has no
#: `known_cards` parameter to ask for and ignores the new key on a row it
#: already reads whole.
#:
#: `seatkit-17` is an **additive request** change and an **additive
#: response** one: `cast` gained `optional_costs`, and a legal-action row
#: gained `optional_costs`, `printed_produces` and `activate_first`.
#:
#: Battle 2 found kicker unreachable: Sowing Mycospawn's only row was `cast
#: {3}{G}`, `CastRequest` had no field for the choice, and so the card's
#: exile-a-land half, and every kicker card in the pool, could never be chosen
#: by a seat - while the engine has charged kicker all along
#: (`Casting.pay_kicker`, CR 702.33a). `optional_costs` is the CR 601.2b
#: announcement of any optional cost the engine charges - kicker,
#: multikicker, replicate, offspring, blight, gift, entwine, escalate and
#: harmonize's creature tap - named by the keyword the
#: row's own `optional_costs` entry carries (`parse_optional_costs` is the one
#: parser both front ends use: `Kicker`, `Multikicker:2`, `Gift:bob`).
#:
#: A bump, the same reasoning as `seatkit-8`, `seatkit-12` and `seatkit-15`: an
#: MCP process built before it has no parameter to send the choice with, so a
#: seat told to kick would cast unkicked and believe otherwise. The response
#: fields are additive and an older build passes them through untouched.
#:
#: What did NOT change: no default. Absent `optional_costs` pays none, which
#: is every cast before this.
#:
#: `seatkit-16` is a **response** shape change: `wait_for_priority` answers an
#: advisor uid with `available_actions` on a real wake.
#:
#: An opponent seat has always been handed the engine's legal-action list -
#: target candidates and `pays_with` included - on its `pass`/`pass_until`
#: answers. An advise seat is refused `pass_until` (driving the game is not
#: advising it), so `wait_for_priority` is the answer that wakes it, and that
#: answer carried the whole board and no list. The buddy, the seat that thinks
#: at every decision point its human holds, was left to reason out legality
#: the engine would simply have told it. Now `mcp.wait_for_priority` adds the
#: list, shaped by the action answers' own `_compact_actions` (same candidate
#: and `pays_with` budgets, same stated truncation), when the seat is an
#: advisor AND a wake condition holds. An idle `nothing new yet` answer fetches
#: nothing, and a seat uid's wait is unchanged.
#:
#: Why a bump rather than a fold, against the precedent above: the widening
#: entry below declined one because no shape moved and nothing was unsendable,
#: and the response fields folded into an earlier version shipped in the same
#: build, with no build in between to describe. Neither holds here. A key is
#: added to an existing tool's answer, this build's advisor prose now says the
#: legal plays arrive on the wake and names no other way to look them up, and
#: the MCP server is a separate process spawned once and never reloaded. A
#: stale seatkit-15 MCP process serving a buddy that runs this prose would
#: answer every wake without the list, silently, leaving the advisor told to
#: choose from a list it never receives. That is the stale-process case the
#: PROTOCOL MISMATCH sentence exists to make loud, the same reasoning that
#: bumped seatkit-13 for a response-only change.
#:
#: What did NOT change: no request parameter anywhere, no api route (the
#: `legal-actions?candidates=true` read already existed), nothing an advisor
#: can use to move the game - the list is one more GET on a path that is
#: already a GET, and `pass_until` stays refused.
#:
#: `seatkit-15` is an **additive request** change: `cast` gained `spell_face`,
#: the half of a split card this cast announces (CR 709.3).
#:
#: The engine has always enumerated a split card as TWO `cast` actions, one
#: per half, each carrying `Action.spell_face` and its own cost - and this api
#: could not act on either of them. `CastRequest` had no face field and
#: `engine_api._drive_cast` never called `Casting.cast_as_split_half`, so
#: every cast of Wear // Tear resolved as Wear whatever row the seat thought
#: it was taking. A seat handed "cast Tear" in `available_actions` followed
#: its instructions and got the front half, or a refusal it could not
#: diagnose; `baseline_seat._plain_cast` skipped every `spell_face` row for
#: exactly that reason. The api was advertising an action it could not
#: perform, which is the same defect class as prose describing a field that
#: does not exist, except here the api itself was the liar.
#:
#: The value is the engine's own string, unmodified: what `Action.spell_face`
#: says is what `spell_face` takes, so a seat posts back what it read. That
#: round trip is the reason the field is named after the engine's own field
#: rather than given a parallel vocabulary of this api's invention - there is
#: no second place to look the spelling up.
#:
#: An MCP process built before this bump has no `spell_face` parameter to
#: forward however clearly the prompt tells it to announce one, so the silent
#: misbehaviour would be the old one: a seat casting the front half while
#: believing it cast the back. That is what turning this into the PROTOCOL
#: MISMATCH sentence prevents - same reasoning as `seatkit-8` and
#: `seatkit-12`, both of which closed a "the tool has no parameter for it"
#: gap on this same tool.
#:
#: What did NOT change: `spell_face` is the only parameter this bump touches
#: anywhere, additive and optional, and absent still means the plain front
#: face on every card. No response shape moved. Deliberately not folded in:
#: fuse (CR 702.102, casting both halves as one spell, `Casting.cast_fused`),
#: which is a second announcement with its own field and its own tests.
#:
#: **Widened in place, deliberately not a bump to `seatkit-16`.** This entry
#: used to end by excluding CR 715.3's Adventure, which this api refused with
#: a reason of its own: the engine's `Casting.targeting` was scoped to
#: `Layout.SPLIT`, so an Adventure was announced against the whole printing's
#: profile and a targeted Stomp came back "too many targets: 1 chosen where
#: the spell takes at most 0" while a targetless one resolved dealing no
#: damage. Engine commit fd28021 made the chosen face the discriminator on
#: every casting path (CR 601.3e: alternative characteristics "replace the
#: object's characteristics for this determination", the general rule CR
#: 709.3a and CR 715.3a both instance), so `spell_face` now accepts an
#: adventurer card's Adventure face too and `Action.target_slots` on that row
#: describes the Adventure rather than the printing's creature front.
#:
#: That is a widening of the accepted *value space* of a parameter that has
#: existed since this very version, with no request or response shape moved -
#: and this constant's own rule is "bump alongside any change to a tool's
#: request/response shape". A bump would make every `seatkit-15` MCP process
#: announce PROTOCOL MISMATCH and stop playing over a change that costs it
#: nothing: it already has the parameter, already forwards whatever
#: `Action.spell_face` string it read, and gains only that one more string now
#: works. The mismatch sentence exists for the opposite case - a build that
#: *cannot express* what the api now expects, which is why `seatkit-8`,
#: `seatkit-12` and `seatkit-15` itself were all bumps: each added a parameter
#: an older build had no way to send. Nothing here is unsendable by a
#: `seatkit-15` build. A newer build talking to an older api gets the old
#: api's refusal in its own words, which is diagnosable prose rather than a
#: silent wrong cast.
#:
#: `seatkit-14` is an **additive request** change and a **response** shape
#: change, and every half of it is one thing: handing a seat everything it
#: needs to *issue* its next action, not merely to choose one, in the answer
#: it has already been given.
#:
#: (1) **`available_actions.actions[].targets`.** Every entry that targets and
#: is not flatly unaffordable gains the legal candidates for each of its
#: requirement slots: `{understood, slots: [{slot, count, candidates,
#: truncated}], more_with}`, candidates as plain strings shaped
#: `"<uuid> <name> (<zone>, <controller>)"` or `"<player> (player)"` - the
#: exact spellings `targets` takes back, so a seat casts straight out of the
#: answer it just read. `Action.target_slots` was only ever the *shape* of a
#: requirement (the engine's own `TargetSlot` docstring points at
#: `Game.legal_targets` for the objects), so before this a seat holding a
#: legal, affordable, targeting action still had to spend a `battlefield_state`
#: round trip - and so a whole inference cycle - to find out what it could
#: name. `understood: false` carries no candidates and says why, never an
#: empty list, which would read as "nothing is legal"; a row with
#: `affordable: "no"` carries none either, which the new envelope key
#: `available_actions.targets_note` states once rather than every row
#: repeating.
#:
#: (2) **`board.battlefield_digest`.** One line per permanent, keyed by
#: controller: id, name, power/toughness, tapped, counters, summoning
#: sickness, what it is attached to, what it is attacking. `_action_answer`
#: already received the whole board and threw it away; ~62 bytes a permanent
#: is what it costs to stop doing that. `battlefield` STAYS in
#: `omitted_sections` - the digest is a summary of the section, not the
#: section - and a digest that will not fit its cap is replaced whole by a
#: pointer at `battlefield_state`, never emitted partially.
#:
#: (3) **Request, additive: `decide` gained `generation`.** Nothing else about
#: the call moved, and omitting it behaves exactly as before -
#: `DecideRequest.generation` has been `None`-means-skip since it was added,
#: `app.py`'s `decide` has enforced it with the right refusal text for as
#: long, and `seat_cli.py` has exposed `--generation` for as long. Only the
#: MCP tool had no parameter to send it with, so a diagnostic that already
#: existed server-side was unreachable from the one surface agents actually
#: call: a stale read earned the flat "no pending decision for you" (11
#: measured occurrences in the cluster's records table) instead of the
#: refusal that names both generations and says the resolution moved on.
#:
#: (4) **`pending_decision.answer_field` / `.answer_with`.** Every
#: `pending_decision` block - the state view's, the one an action's own answer
#: lifts to the top level, and the one an `input_required` result carries -
#: gained both keys. `answer_field` names the single tool parameter that
#: carries this kind's answer; `answer_with` is the whole call, paste-ready,
#: already carrying this question's own generation. The mapping they encode
#: (`decisions.TOOL_FIELD_FOR_KIND`/`TOOL_FOR_KIND`) disagrees with the
#: engine-facing field name on ten of the nineteen kinds and lived only in
#: `decide`'s own ~200-line docstring before this, which is exactly the
#: "how" thinking a seat had to do before it could answer at all - and where
#: it guessed wrong, the guess was a real parameter of `decide` accepted by
#: the tool and refused by the route ("this decision needs a yes/no answer
#: ('accept')", 2 measured occurrences).
#:
#: (5) **`available_actions.actions[].pays_with`.** The engine's `Action`
#: gained `pays_with` (`machines/cost.plan_mana_payment` in the engine, at
#: `4ad120c`): the tap plan `auto_tap` would actually execute for that cast,
#: one entry per source it would tap - `card_id`, `name`, `ability_index`,
#: `adds`, and `restriction` when that source's mana carries a CR 106.6
#: sentence. Present only on a `cast` action that owes mana and that the
#: engine has already said is payable (`affordable: "yes"`); absent
#: everywhere else, including on a payable cost the seat's own floating mana
#: already covers, where the honest answer is that nothing gets tapped.
#: Purely informational and never a request shape: nothing here asks a
#: caller to name mana sources back, and `mana_sources` is unchanged by this
#: entry - a seat spends its own reasoning on *which* of its sources a cast
#: eats ("this Bolt takes the Cavern of Souls I wanted for the
#: counterspell"), not on the mechanics of paying, which the engine already
#: does either way. The field reached `GET .../legal-actions` for free
#: (`schemas.ActionWithTargets` subclasses the engine's `Action` and passes
#: every field through untouched) and `mcp._compact_actions`/`_without_empty`
#: already dropped an empty list the same way they drop every other
#: empty/null key - so no code moved to carry it, and this paragraph is what
#: makes that fact into the standing contract rather than an accident an
#: unrelated refactor could quietly undo. An MCP process built before this
#: bump reads an answer whose `cast` entries carry a key it does not know and
#: goes on choosing mana sources the only way it ever could - by leaving
#: `mana_sources` empty and letting the engine pick - so the silent
#: misbehaviour this check exists to catch is a seat missing a reasoning cue
#: it did not know to look for, not a broken call.
#:
#: (6) **Advise mode: the action tools' answer shape, for an advisor uid
#: only.** `GET /api/seats/{uid}` now reports a `role` (`"seat"` or
#: `"advisor"`, wave 1), `Seats.seat_for` reads it - on every transport,
#: including the `--game`-pinned stdio branch, which did not consult that
#: endpoint at all before - and an advisor gets `Seat(advise=True)`. For such
#: a seat every *committing* POST is answered `{"proposed": true, "log_seq":
#: int, "summary": str, "outcome": ..., "state": ...}` instead of an action
#: outcome: the action is validated with `dry_run: true` where the request
#: shape has the field (exactly 7 of the 12 action shapes -
#: `DRY_RUN_SHAPES`), recorded as one `advice` ledger row, and never
#: performed. The other 5 shapes send no mutating request at all and are
#: marked `validated: false`. `run_action` stops before its auto-pass for an
#: advise seat (`held_priority: null`, `wake_reason` saying nothing moved),
#: `pass_until` is refused outright, and `declare_plan` returns the server's
#: own seat-only refusal. Nothing about a *seat* uid's answers moves.
#:
#: (7) **New api surface: `POST /api/games/{id}/advice` and
#: `POST /api/games/{id}/advice/{seq}/approve`** (`schemas.AdviceIn`,
#: `AdviceApproveIn`). Both record one ledger row and mutate no game.
#: `/advice` derives its own `summary` server-side from the request body it
#: is handed, so what a human reads can never disagree with what it would
#: send, and requires a non-blank `reasoning`; `/advice/{seq}/approve` is
#: human-uid only. The `advice` row is redacted to `{player}` for anyone but
#: its author (`visibility.redact_log_entry`) - a proposal names cards in
#: that player's hand.
#:
#: Items (6) and (7) are the *same* protocol version as (1)-(5) on purpose:
#: wave 1 (the advisor credential and its gate) and wave 2 (what the toolset
#: does with it) ship together, so there is no build in between for a
#: separate number to describe.
#:
#: An MCP process built before this bump neither sends `generation` nor knows
#: the new keys: it reads an answer whose action entries, `board` and
#: `pending_decision` carry keys it does not know, and goes on making the
#: extra round trip it was written to make - which is the silent,
#: slower-than-necessary behaviour this check exists to turn loud instead.
#:
#: What did NOT change, said plainly: `generation` on `decide` is the only
#: parameter this bump touches anywhere - additive, optional, and the sole
#: reason `contract/tools.json` moves at all. No tool lost or renamed a
#: parameter. Deliberately not folded in either: a `focus`/`for_action`
#: parameter to narrow which action's candidates come back. That shape was
#: considered and rejected - a seat must already know what it is about to do
#: in order to ask, which is exactly the question it was trying to answer.
#:
#: The api's own `GET .../legal-actions` grew `candidates=true` for this, and
#: that half is opt-in on purpose: the walk costs ~106 ms on a 28-permanent
#: board (`TargetRequirement.rejects` reaches `Game.characteristics_of`, and
#: `Game.computed_board` is uncached in the engine by CR 613.5's own
#: reasoning). A seat pays it once per action and saves a round trip;
#: `ui/app.js`, which polls the same endpoint every 1.2s and already has the
#: board on screen, does not ask and does not pay.
#:
#: `seatkit-13` is a **response** shape change: every mana source an action
#: tool offers now says what it actually makes and what it is allowed to pay
#: for. The engine's `Action` gained `produces`, `restriction` and
#: `restriction_understood`, so `available_actions` (and the `mana_sources`
#: block every action tool's answer carries) grew the same three fields, and
#: the api's own `ManaSource` grew `ability_index` to name *which* mana
#: ability of a multi-ability source is the one being described.
#:
#: `produces` is the printed production, not a board-resolved one - Cavern of
#: Souls reads "{1}" plus one of any colour because that is what the printing
#: says, and resolving it against the named creature type is the seat's job,
#: not this wire's. `restriction` carries the engine's own words for what the
#: mana may be spent on ("spend this mana only to cast a creature spell",
#: Cavern's chosen-type clause, Sunken Citadel's land-ability clause), and
#: `restriction_understood` says whether the engine parsed that clause or only
#: saw that one was printed - fail-closed, so a seat is never told an unread
#: restriction is absent.
#:
#: Before this a seat picking mana sources could not tell Delighted Halfling's
#: unrestricted mana from Cavern of Souls' creature-only mana, nor see that a
#: source it had just tapped was the reason its cast was refused: the tools
#: handed back a list of tappable permanents and nothing about what tapping
#: one would buy. An MCP process built before this bump reads an answer whose
#: `mana_sources` entries carry keys it does not know - which is the whole of
#: what this bump is, and the reason it is a **response** shape change and
#: nothing else.
#:
#: What did *not* change, said plainly so the next reader does not have to
#: check: the tool **request** shape is untouched, and `contract/tools.json`
#: is byte-identical across this bump because of it. `mcp.py` and
#: `seat_cli.py` both build `mana_sources` out of `sources_or_all`, which
#: returns a list of bare card ids parsed from a comma-separated string, so a
#: tool seat still cannot name an ability at all: on a two-mana-ability source
#: it names the permanent and the api picks the ability, exactly as before.
#: `ManaSource.ability_index` is reachable only through the HTTP API directly,
#: which today means the browser UI. Teaching the tools to name one is a
#: separate change to the request contract and its own bump - deliberately not
#: folded in here.
#:
#: `seatkit-12` is two shape changes at once, both found by one stuck
#: kubernetes seat that spent a whole turn failing to evoke Solitude.
#:
#: (1) **Request.** `cast` gained `choosing`, `alternative_cost`,
#: `sacrificing`, `discarding`, `exiling`, `tapping`, `delve` and `convoke`,
#: and `CastRequest` gained the four printed-additional-cost fields those
#: last need. Not one cost that names objects was reachable through the tool:
#: `CastRequest.choosing` had existed since Force of Negation's pitch and was
#: never exposed, `engine_api._drive_cast` dropped it entirely on the
#: `keyword` branch (which is the branch `legal-actions` and a hand card's
#: own `cast_options` both point a seat at), and CR 118.8a's printed
#: additional costs had no field on the request at all. The seat's own words:
#: "the cast tool has no parameter to specify which card to exile as the
#: alternative cost. Tried `targets`, `mana_sources`, and `exiling`
#: (undocumented) ... all rejected". Same reasoning as `seatkit-8`, which
#: closed exactly this gap for `activate`: an old MCP process has no
#: `choosing` parameter to forward however new the prompt telling it to is,
#: so the bump turns a silently-omitted field back into the PROTOCOL MISMATCH
#: sentence.
#:
#: (2) **Response.** Every action tool's answer (`cast`, `activate`, `pass`,
#: `decide`, `play_land`, `cycle`, `suspend`/`plot`/`foretell`,
#: `declare_attackers`/`declare_blockers`, `pass_until`) no longer carries the
#: whole board under `state`. It carries `outcome` first, then `wake_reason`,
#: `pending_decision`, a `board` block holding the spine, the stack, combat
#: and both players, then `available_actions` and `recent_log`;
#: `board.omitted_sections` names what was left out and `board
#: .read_the_board_with` points at `battlefield_state`. One `cast` answer had
#: reached 25,397 tokens - past the seat's own client's ceiling, so the agent
#: could not read the refusal the engine had spelled out and retried the same
#: illegal cast four times. `wait_for_priority` and `battlefield_state` are
#: unchanged: handing over the board is what they are for. An MCP process
#: built before this reads a response with no `state` key and shows a seat an
#: empty board rather than a narrowed one, which is precisely the silent
#: misbehaviour this check exists to make loud.
#:
#: `seatkit-11` (base `seatkit-9`; `seatkit-10` was claimed by another
#: unmerged branch, so this skips past it rather than colliding) added
#: `x_value` to the `activate` tool/CLI command, closing the gap a live game
#: found: Fiend Artisan's "{X}{B/G}, {T}, Sacrifice another creature: Search
#: your library for a creature card with mana value X or less..." had no way
#: to announce X at all, because `cast` already carried `x_value` and
#: `activate` did not - every activation of a cost with `{X}` in it silently
#: ran at the engine's own default, `x_value=0`, rather than refusing or
#: honouring what the seat actually asked for. `ActivateRequest.x_value` is
#: new, `engine_api.activate_ability` now threads it into
#: `Game.begin_activating`/`Game.activate_ability`, and refuses in words - the
#: engine's own `needs_x` answer, not a guess made here - when the ability's
#: own cost carries an `{X}` and none was named, rather than silently running
#: at zero. A request-shape change to a widely-used tool, the same
#: `seatkit-8` reasoning: an old MCP process built before this addition has
#: no `x_value` parameter to forward even if a newer prompt asked it to use
#: one, so the version bump is what turns that silent gap back into the
#: PROTOCOL MISMATCH sentence rather than a call that quietly omits X again.
#: `seatkit-10` (this repo's own numbering - picked deliberately past 8 and 9
#: to avoid a confusing collision: an unmerged branch had already taken this
#: constant to `seatkit-9` at the time this bump was written, and whichever
#: side lands second should reconcile to one higher than the other, not
#: retroactively fight over 8 or 9) is the T-something notice-plumbing wave:
#: `pass_priority`/`act`/every other mutating tool now carries a `notices`
#: key - `Game`'s own fail-closed notice records (19 lists,
#: `models/game.py:1148-1306`) that a resolving spell or ability's clause was
#: recognised but could not be read, attributed to the command that raised
#: them (`magic_cards_model_api.commands._attach_notices`) and redacted per
#: viewer (`visibility.redact_notices`). Before this bump a fail-closed
#: clause - Sheoldred's Edict-shaped, and every other card on the same
#: "recognised but unreadable" list - resolved doing nothing with no
#: indication anywhere in a seat's own tool result that the refusal had ever
#: happened; the engine already recorded why, this bump is what finally
#: hands that record to whoever asked. An MCP process built before it reads
#: a response missing `notices` entirely and silently treats every one of
#: these refusals as an ordinary no-op, same as before - not a hard failure,
#: but the exact silent-misbehaviour case this version check exists to turn
#: loud instead.
#:
#: `seatkit-9` merges two independent shape changes that landed on the same
#: base and were combined here: (1) `battlefield_state`'s per-card
#: `cast_options` now covers `Zone.HAND`, not only graveyard/exile, so a
#: hand card with a CR 118.9 keyword cost (evoke, dash, morph, megamorph,
#: disguise, bestow, warp's hand half) is badged on the poll instead of only
#: discoverable through the affordance endpoint. This is an additive
#: response field, the same shape of change `seatkit-5`'s `recent_log`
#: addition was, and that precedent - not an omission - is why it is bumped
#: alongside (2) rather than left to ride along unversioned: an MCP process
#: built before this sees the same silent gap `seatkit-5`'s docstring
#: describes, a hand that reads as carrying no alternative-cost options
#: rather than a hand that is missing a field it does not know to ask for.
#: (2) `activate`'s `sacrificing`/`discarding`/`exiling` parameters, below -
#: see that entry for what it closes. Two request/response shapes changed
#: between the same two points in history; one version bump covers both.
#:
#: `seatkit-8` added `sacrificing`/`discarding`/`exiling` to the `activate`
#: tool/CLI command, closing the gap a live game found: an activated ability
#: whose own cost names an object (Goblin Bombardment's "Sacrifice a
#: creature: ...") had no way to be paid at all, because
#: `ActivateRequest.sacrificing`/`.discarding` already existed on the request
#: schema (`engine_api.activate_ability` already threaded them into
#: `Game.begin_activating`) but neither seat surface exposed a way to set
#: them - every attempt failed with the engine's own honest, unreachable
#: refusal, "sacrifice needs 1 card(s) chosen, 0 given". A request-shape
#: change to a widely-used tool, per this constant's own rule: an old MCP
#: process built before this addition has no `sacrificing` parameter to
#: forward even if a newer prompt asked it to use one, so the version bump
#: is what turns that silent gap back into the PROTOCOL MISMATCH sentence
#: rather than a call that quietly omits the field again.
#:
#: `seatkit-7` replaced the `my_state` tool with `battlefield_state`, which
#: answers the same whole board when called with nothing but a uid and takes
#: two options to narrow it: `sections` (a comma-separated list of the state
#: view's own top-level keys) and `side` (`both`/`mine`/`opponent`, resolved
#: from the caller's own uid). The old name is gone outright rather than kept
#: as an alias, and this bump is what makes that safe to do: an MCP process
#: built before it serves `my_state` and has no `battlefield_state` at all,
#: so an agent driven by a prompt from after it would call a tool that does
#: not exist and read "Unknown tool" - a failure with no name on it. With the
#: bump, that same stale process refuses every call up front with the
#: PROTOCOL MISMATCH sentence naming which side is stale, which is exactly
#: what this check is for. An alias would not have helped either direction:
#: the stale process is stale in its *own* code, and nothing added here
#: reaches it (this process is spawned once and never reloads - see `mcp.py`).
#:
#: `seatkit-6` added the `declare_plan` tool/method (`POST .../plan`,
#: `schemas.DeclarePlanIn`). This one is not an optional-degrade case the way
#: earlier bumps mostly were (an older client missing a nicety, still able to
#: play): the backend gate this pays for, `records.require_plan`, already
#: refuses an agent seat's first mutating action of its own turn until a
#: `plan_declared` record exists for that turn - and it shipped *before* any
#: client could call the route that satisfies it. An MCP process built before
#: this bump has no `declare_plan` tool at all, so an agent seat driven by it
#: is refused, 422, on the very first action of every one of its own turns,
#: forever - not a degraded seat, a seat that cannot play. That is exactly
#: the silent-misbehaviour case this version check exists to turn into a
#: loud, named refusal instead of a mystery.
#:
#: `seatkit-5` put a `recent_log` on every answer that already carried a
#: state view (`my_state`, as `battlefield_state` was then called,
#: `wait_for_priority`, every action tool through `run_action`, `pass_until`)
#: and gave the `game_log` tool the `since` parameter the api's own
#: `GET .../log` has always had. "What happened since I last acted" and "what
#: can I do now" used to be two tool calls and two whole inference cycles -
#: `prompt.py` said so outright ("read `my_state`, read `game_log`") - and
#: they are one now. An MCP process from before this
#: simply does not carry the log, which reads to a seat as a game where
#: nothing ever happened between its own moves.
#:
#: `seatkit-4` (T10) gave every mutating tool a `reasoning` parameter - "why
#: are you doing this", in the seat's own words - required by the API for an
#: agent seat (refused 422 when it is missing) and optional for a human one.
#: An MCP process from before this omits the parameter entirely, so an agent
#: seat it drives would have every mutating call refused outright rather than
#: silently accepted with no reasoning recorded.
#:
#: `seatkit-3` removed `max_seconds` from every tool and command that carried
#: it - nothing blocks any more, so nothing was left for it to bound, and a
#: parameter that bounds nothing is a promise an MCP client cannot see
#: corrected (it receives no `description` on any parameter). It also gave
#: `pass_priority(until=...)`/`pass --until` a `resume` token, because that
#: run is poll-and-return now and spans several calls, and made that path
#: return `input_required` when it stops on a decision. An MCP process from
#: before this passes `max_seconds=` to tools that no longer take it.
#:
#: `seatkit-2` added the pregame tools (`mulligan`, `opening_hand_action`) and
#: the `pregame` block every state view now carries: a game booted from
#: decklists rests in CR 103.5's mulligan step until its seats declare, and an
#: MCP process from before this cannot drive one out of it - which looks, from
#: the seat's side, exactly like a game that never starts. Better a refusal
#: naming both versions.

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


#: Hex digits and dashes, at least six, with a digit or a dash among them - a
#: card id someone shortened or cut off. No printed card name has that shape
#: (one that did would need no letters past `f` and no spaces).
_ID_FRAGMENT = re.compile(r"^(?=.*[0-9-])[0-9a-f-]{6,}$", re.IGNORECASE)


def card_reference(value: str) -> dict[str, str]:
    """The request field a seat's `card` argument belongs in: `card_id` for
    an id, `card` for a name.

    An id fragment (`5609c7e2`) goes out as `card_id` too, not as a name: the
    API then refuses it as a malformed card id naming the full form
    (`app._validation_error`), where looking it up as a name could only say
    "no card named 5609c7e2" - true, and no help to a seat that meant an id.
    Shared by `seat_cli.py` and `mcp.py` so the two front ends split the same
    way."""
    stripped = value.strip()
    if _UUID.match(stripped) or _ID_FRAGMENT.match(stripped):
        return {"card_id": stripped}
    return {"card": value}


NO_SUCH_SEAT = (
    "that uid is no seat in this game. You cannot act, and you may see nothing - "
    "a seat uid is given to you when you are dispatched."
)

#: How many log entries ride along on an answer that already carries a state
#: view. A tail, not the history: `game_log` is still the tool for the whole
#: thing, and `recent_log["omitted"]` says when this tail dropped something so
#: a seat is never quietly told less than happened.
RECENT_LOG_ENTRIES = 15

#: The fields a ride-along log entry keeps. Deliberately NOT the whole entry:
#: `audit.record` stamps every row with a full-information `snapshot` of the
#: board and with `legal_actions_before`, the enumerated action pool of that
#: moment - so fifteen whole entries would be fifteen more board states and
#: fifteen more action pools bolted onto an answer that already carries the
#: current one of each. That is not a token-trimming judgement (payload size
#: buys no latency here); it is what a log *tail* is for. A seat asking "what
#: has happened since I last acted" is asking for the narrative, and the
#: snapshots are still one `game_log` call away, unchanged.
LOG_HEADLINE_FIELDS = ("seq", "ts", "action", "detail", "turn", "step", "reasoning")


def log_headline(entry: dict[str, Any]) -> dict[str, Any]:
    """One log entry as the narrative line a ride-along tail carries - the
    `LOG_HEADLINE_FIELDS` that are present, and nothing invented for one that
    is not."""
    return {field: entry[field] for field in LOG_HEADLINE_FIELDS if field in entry}


def _log_unavailable(since: int, reason: str) -> dict[str, Any]:
    """`recent_log`'s answer when the call itself failed: an empty tail that
    says so, rather than one a reader would take for "nothing happened"."""
    return {
        "entries": [],
        "last_seq": since,
        "omitted": 0,
        "log_error": (
            f"{reason} - this is NOT 'nothing has happened': the log could not be "
            "fetched at all. Call game_log, or call again."
        ),
    }


#: How often `_call_watching_for_self_decision` re-fetches state while one
#: mutating call of its own is genuinely in flight on another thread - the
#: only loop in this module that still polls at all, and it polls for the
#: duration of a request it is already waiting on rather than for anything to
#: happen at the table. Small: it has no condition variable to be woken by,
#: only "check the board again". Nothing else here sleeps: `block_until_wake`,
#: `wait_for_priority` and `pass_until` are all poll-and-return.
POLL_SECONDS = 0.2

#: The HTTP client read timeout every `Seat`/`Seats` builds with.
#:
#: This used to be derived from the server's own worst-case block
#: (`resolve_client_timeout`, since removed): a mutating call
#: (`cast`/`activate`/`attack`/`pass-priority`/`resolve-top`/`advance`)
#: crossing a live decision question used to block *inside that one
#: request*, in `Game.ask`, for up to the server's own
#: `decision_timeout_seconds` - and a client-side timeout shorter than that
#: turned a legitimate hold into a false "the action failed". That hazard is
#: gone, not merely mitigated: no mutating request blocks on a live decision
#: any more (`commands.py`'s pause-and-resume) - a question with no scripted
#: answer returns immediately instead of parking the request - so there is
#: no longer a server-side block for a client timeout to be shorter than.
#: `CLIENT_TIMEOUT_SECONDS` is just a generous, fixed bound against ordinary
#: network jitter and a slow deployment, the same as any other HTTP client.
CLIENT_TIMEOUT_SECONDS = 30.0

#: Shown in place of a raised exception whenever a request genuinely times
#: out waiting for an answer (`httpx.ReadTimeout`/`WriteTimeout`/`PoolTimeout`
#: - a request that was sent, unlike a bare connection failure). The action
#: may already have been performed server-side, so this must never read like
#: an ordinary refusal (`run_action` checks for it before its normal
#: "refused" branch) and must never invite a blind retry, which is exactly
#: the duplicate-action hazard this exists to prevent.
#: The `httpx` timeout flavours that mean "a request was sent and we do not
#: know what happened to it" - as opposed to `httpx.ConnectTimeout`/
#: `ConnectError`, which mean the request never left this process, so there
#: is nothing that could have landed server-side. Only these get the
#: `UNKNOWN_OUTCOME` treatment; a plain connection failure keeps its
#: existing, unambiguous "could not reach the API" message.
_AMBIGUOUS_TIMEOUTS = (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)

UNKNOWN_OUTCOME = (
    "TIMED OUT waiting for an answer - the outcome of this call is UNKNOWN. "
    "The action may already have gone through on the server even though "
    "this call never got a response. Do NOT retry it blindly: call state "
    "(or read the game log, e.g. its last log_seq) to see what actually "
    "happened first, then decide what to do from that - never from this "
    "timeout alone."
)


# ------------------------------------------------------------- advise mode --
#
# An advisor is a *role of the uid*, not a separate toolset: a buddy pod is
# the seat Job with a different uid, gets the same 19 MCP tools an opponent
# pod gets, and nothing about its dispatch (no CLI flag, no MCP config, no new
# Job placeholder) says which it is. `GET /api/seats/{uid}` does - `role` is
# `"seat"` or `"advisor"` - and `Seats.seat_for` turns that into
# `Seat(advise=True)`.
#
# Everything the role changes is enforced in `Seat._advise_answer`, because
# `Seat.call`/`Seat.call_json` are the one place every HTTP request this
# module makes leaves. Enforcing it per tool would mean 19 places to forget
# it, and the 20th tool written next month would not be covered at all.
#
# The server refuses an advisor's mutating POST anyway (`game_access.py`), so
# none of this is the only line of defence - but "refused, one action at a
# time, with nothing to say why" is a useless thing to hand an agent, and the
# gate's own `dry_run: true` carve-out is exactly as wide as the request
# shapes that *have* that field. See `DRY_RUN_SHAPES` for why that matters.

#: Every game-mutating action path a seat's own tools POST to, and the request
#: shape each one takes (`schemas.py`). Fourteen paths, twelve shapes - the
#: three CR 116.2 special actions share `SpecialActionRequest`.
#:
#: Frozen here, and pinned against `openapi.json` by
#: `tests/test_advise_mode.py`, so the split below cannot drift silently: a
#: shape that gains or loses `dry_run` changes which of an advisor's calls get
#: validated before they are proposed, and - far worse - which of them would
#: start being *sent*.
ACTION_REQUEST_SHAPES: dict[str, str] = {
    "actions/activate": "ActivateRequest",
    "actions/attack": "AttackRequest",
    "actions/cast": "CastRequest",
    "actions/concede": "ConcedeRequest",
    "actions/crew": "CrewRequest",
    "actions/cycle": "CycleRequest",
    "actions/equip": "EquipRequest",
    "actions/declare-blockers": "DeclareBlockersRequest",
    "actions/foretell": "SpecialActionRequest",
    "actions/pass-priority": "PassPriorityRequest",
    "actions/play-land": "PlayLandRequest",
    "actions/plot": "SpecialActionRequest",
    "actions/suspend": "SpecialActionRequest",
    "advance": "AdvanceRequest",
    "decide": "DecideRequest",
}

#: The 7 of those 12 shapes that carry `dry_run` - the ones an advisor can ask
#: the engine to *validate* before proposing, against an isolated probe copy
#: of the game (`engine_api.dry_run_copy`) that commits nothing.
#:
#: The other 5 (`DecideRequest`, `DeclareBlockersRequest`,
#: `PassPriorityRequest`, `ConcedeRequest`, `AdvanceRequest`) are the
#: dangerous case, and the reason this set is a frozen constant rather than a
#: runtime check. `game_access._advisor_may` authorises a POST whose raw body
#: says `dry_run: true`; a shape with no such field either drops it as an
#: unknown key or refuses the body outright, and then the route commits for
#: real. So an advise `Seat` must never lean on that carve-out for these five:
#: it sends **no mutating request at all** for them, builds the proposal from
#: `available_actions` instead, and marks it `validated: false`.
DRY_RUN_SHAPES = frozenset(
    {
        "ActivateRequest",
        "AttackRequest",
        "CastRequest",
        "CrewRequest",
        "CycleRequest",
        "EquipRequest",
        "PlayLandRequest",
        "SpecialActionRequest",
    }
)

#: The POST paths an advise `Seat` sends exactly as a seat would. All four are
#: ways of *saying* something about a game rather than doing something to it,
#: and the server already decides which of them an advisor may use: `/advice`
#: is advise mode's own channel (proposing through it must not recurse),
#: `/report` files a problem, and `/plan`/`/reflection` are seat-only - their
#: own refusal is the honest answer to give, not a proposal dressed up as one.
ADVISE_PASSTHROUGH_SUFFIXES = ("/advice", "/report", "/plan", "/reflection")

#: Why an advise `Seat` cannot run an auto-pass. A `pass_until` run gives up
#: priority over and over until a named point in the turn - which is not
#: advising, it is playing: the seat it advises would arrive at its own next
#: window to find the turn already spent.
ADVISE_NO_PASS_UNTIL = (
    "advisors propose one action at a time; use pass - a run that keeps "
    "passing priority until a named step is driving the game, not advising "
    "it, and the seat you advise is the one who gets to decide whether its "
    "turn is over"
)

#: What an advise `Seat` says in place of `run_action`'s wake reason. There is
#: nothing to wake for: nothing was performed, priority never moved, and the
#: board below is the same board the proposal was built against.
ADVISE_WAKE_REASON = (
    "proposed, not performed: this is recorded in the game log for the seat "
    "you advise to approve or ignore. Nothing has moved - it is still their "
    "move, and you still hold no priority of your own"
)

#: The reason carried by a proposal nobody validated - the 5 shapes with no
#: `dry_run` to ask with. Said out loud in the answer *and* recorded in the
#: ledger row (`validated: false`), because "the engine agreed this is legal"
#: and "seatkit thinks this is the shape of a legal action" are different
#: claims and a human approving one should know which they were handed.
ADVISE_UNVALIDATED_REASON = (
    "NOT validated: this action's request shape has no `dry_run`, so there "
    "is no way to ask the engine whether it is legal without actually doing "
    "it - and an advisor never does. The proposal was built from "
    "`available_actions` below; the seat you advise will find out for certain "
    "when it sends it"
)


def _action_key(path: str) -> str | None:
    """Which `ACTION_REQUEST_SHAPES` entry `path` names, or `None` for a path
    that is not one of them. Matched on the tail, so it is independent of the
    game id in the middle and of whichever router owns the route."""
    tail = path.rstrip("/")
    for key in ACTION_REQUEST_SHAPES:
        if tail.endswith("/" + key):
            return key
    return None


def _status_line(status: int | None) -> str:
    """The first line of `Seat.call`'s text answer. `None` is the ambiguous
    timeout case, which has no status to report - see `UNKNOWN_OUTCOME`."""
    return "HTTP TIMEOUT" if status is None else f"HTTP {status}"


def _reason_from_detail(detail: Any) -> str:
    """A human sentence out of whatever shape a non-2xx body's `detail`
    carries. Every exception handler in `app.py` writes `detail` as a plain
    string except the two shaped like a pydantic validation error
    (`RequestValidationError`, and `records.MissingReasoning`'s own handler,
    which mirrors that shape on purpose) - `detail: [{"loc", "msg", "type"},
    ...]`. The `msg` in there is the actual sentence a human wrote (an agent
    seat's own missing-`reasoning` explanation, say) - what a caller needs,
    not the list wrapping it, which is what `str(detail)` alone would hand
    back."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        msgs = [
            str(error["msg"]) for error in detail if isinstance(error, dict) and error.get("msg")
        ]
        if msgs:
            return "; ".join(msgs)
    return str(detail)


def _normalise_non_2xx(status_code: int, parsed: Any, text: str) -> dict[str, Any]:
    """A non-2xx response body, turned into the same tri-state-safe shape
    every `call_json` caller already checks with `_refused`/`_paused`/
    `_unknown_outcome` - never left in whatever raw shape the failing
    handler happened to write, which is exactly what let a 422 read as a
    completed action (see `call_json`'s own docstring).

    A body that already carries its own `allowed`
    (`commands.DecisionInProgressError`'s 409, `app.py`) is trusted and
    returned unchanged - it is already the shape this function would
    otherwise build. Every other 4xx is a request the server never acted on
    at all (refused before any command ran, same as `records.
    require_reasoning`'s 422), so it becomes an ordinary refusal, `reason`
    pulled out of whatever `detail` shape the body carried.

    A 5xx is the one status class kept out of that: unlike a 4xx, the
    request may genuinely have started running server-side before the
    server itself failed partway through, so this is not the same claim a
    4xx refusal makes ("nothing happened") - it gets the same honest
    `unknown_outcome` shape an ambiguous transport timeout already does, see
    `UNKNOWN_OUTCOME`, rather than a refusal a caller would wrongly trust as
    "still your move"."""
    base = parsed if isinstance(parsed, dict) else {}
    if "allowed" in base:
        return base
    reason = (
        _reason_from_detail(base["detail"]) if "detail" in base else f"HTTP {status_code}: {text}"
    )
    if status_code >= 500:
        return {
            **base,
            "allowed": False,
            "reason": f"{UNKNOWN_OUTCOME} (HTTP {status_code}: {reason})",
            "unknown_outcome": True,
        }
    return {**base, "allowed": False, "reason": reason}


def api_trouble(http: httpx.Client, api: str) -> str | None:
    """`None` when `api` is reachable and running this process's own
    `PROTOCOL_VERSION`; otherwise the clear, actionable message a tool call
    should return in place of doing anything - never an exception, because
    the caller here is a long-lived MCP process and the thing being checked
    (the API) is restarted far more often than it is (once per playtest
    game, per this module's `PROTOCOL_VERSION` docstring).

    `http` is the caller's own `httpx.Client` (`Seat.http`/`Seats.http`), not
    a fresh one, so this goes through whatever transport the caller is
    already using - tests point it at an in-process `TestClient` by
    monkeypatching `httpx.Client`; a bare module-level `httpx.get` would
    silently skip that and hit the real network instead.
    """
    try:
        health = http.get("/api/health").json()
    except httpx.HTTPError as exc:
        return f"the game server is not reachable at {api}: {exc}"
    except ValueError:
        return f"the game server at {api} did not answer /api/health with JSON"
    reported = health.get("protocol_version")
    if reported != PROTOCOL_VERSION:
        return (
            f"PROTOCOL MISMATCH - refusing this call: this MCP server speaks "
            f"{PROTOCOL_VERSION!r} but the API at {api} reports {reported!r}. "
            "One of the two is running stale code - most likely this MCP "
            "process, since it is spawned once and never reloads (see this "
            "module's PROTOCOL_VERSION docstring). Restart the stale one "
            "before trusting anything it returns."
        )
    return None


class Seat:
    """One player's proxy onto the running game."""

    def __init__(
        self,
        player: str,
        uid: str,
        api: str,
        game_id: str | None,
        timeout: float | httpx.Timeout | None = None,
        *,
        advise: bool = False,
        wake_signatures: dict[str, Any] | None = None,
    ) -> None:
        self.player = player
        self.uid = uid
        self.api = api.rstrip("/")
        self._game_id = game_id
        #: Advise mode: this uid is an *advisor* of `player` rather than
        #: `player`'s own seat (`GET /api/seats/{uid}`'s `role`, read by
        #: `Seats.seat_for` - never a flag a caller sets by hand, and never
        #: anything the agent holding this uid gets to choose).
        #:
        #: It reads exactly what `player` reads (the server resolves an
        #: advisor uid to that username - `visibility.resolve_viewer`) and it
        #: never moves the game: every committing POST becomes a proposal
        #: instead (`_advise_answer`), `run_action` stops before its auto-pass
        #: and `pass_until` is refused outright.
        self.advise = advise
        #: `timeout` is normally `Seats.http.timeout` - `Seats.seat_for`
        #: passes it down so the many `Seat` objects it mints all share one
        #: value. `None` (a `Seat` built directly, outside `Seats`) falls
        #: back to the same fixed default.
        if timeout is None:
            timeout = CLIENT_TIMEOUT_SECONDS
        self.http = httpx.Client(base_url=self.api, timeout=timeout)
        # Belt and suspenders, as a bare float (see `Seats.__init__`'s same
        # comment on why not `httpx.Timeout(...)`): tests monkeypatch
        # `httpx.Client` itself to hand back a fresh `TestClient` that
        # ignores every constructor argument including `timeout`, so the
        # line above alone would silently leave that client on its own
        # unrelated default. A real `httpx.Client` already has this timeout
        # from the constructor; re-setting it is a no-op there.
        self.http.timeout = timeout.read if isinstance(timeout, httpx.Timeout) else timeout
        #: P4: the newest ledger position this seat has actually been shown -
        #: `recent_log`'s own `last_seq`, which every `run_action` answer
        #: carries back to the caller. `None` until this seat has read the
        #: log even once, which is the "name no board" default every caller
        #: had before `expected_seq` existed.
        #:
        #: It lives on the `Seat` on purpose: an MCP action tool must not
        #: grow a parameter for this. An agent reasoning about a sequence
        #: number is an agent spending a decision on bookkeeping, and it
        #: would get it wrong (the number it would reach for is whatever it
        #: last saw in a transcript, not what it last read). The seat has the
        #: fact already and hands it over without being asked.
        self.last_seq: int | None = None
        #: Armed by `send_expected_seq_once`, consumed by the very next
        #: mutating `call_json`. One-shot rather than sticky, because the
        #: action itself moves the ledger: `run_action`'s auto-pass, sent
        #: after that action landed, would be stale by construction if this
        #: stayed armed.
        self._expect_seq_once: int | None = None
        #: `uid` -> the `board_signature` this seat last saw on a genuine
        #: wake, read and written by `block_until_wake` alone. `Seats.seat_for`
        #: mints a brand new `Seat` on every single tool call (only the
        #: `(player, role, game_id)` lookup persists, on `Seats` itself), so
        #: this dict is handed down from there rather than owned outright: an
        #: instance attribute on this ephemeral `Seat` would reset to nothing
        #: on the very next call and could never remember anything across
        #: separate MCP tool invocations, which is exactly the case that
        #: matters (see `block_until_wake`). A bare `Seat` built directly
        #: (tests, `seat_cli`'s one-shot process) gets its own private dict -
        #: correct there too, since nothing else ever calls in with the same
        #: uid for it to share.
        self._wake_signatures: dict[str, Any] = (
            wake_signatures if wake_signatures is not None else {}
        )

    def send_expected_seq_once(self, seq: int | None) -> None:
        """Arm the next mutating call this seat makes to name `seq` as the
        board it was decided against (`schemas.BoardRead.expected_seq`).

        Consumed by that one call and no other - see `_expect_seq_once`.
        `None` disarms, which is what a seat that has never read the log
        stays at."""
        self._expect_seq_once = seq

    def _with_expected_seq(self, method: str, body: dict[str, Any] | None) -> Any:
        """`body` with the armed `expected_seq` folded in, and the arming
        spent. A GET spends nothing: there is no mutating action on one to
        name a board for."""
        if method.upper() == "GET":
            return body
        seq = self._expect_seq_once
        self._expect_seq_once = None
        if seq is None:
            return body
        return {**(body or {}), "expected_seq": seq}

    def game_id(self) -> str:
        """The game to play: the one given, or the only live one."""
        if self._game_id is None:
            games = self.http.get("/api/games").json()["games"]
            if not games:
                raise RuntimeError("no live game on the API - start one first")
            self._game_id = games[0]["game_id"]
        return self._game_id

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> str:
        """One API call; the answer as text, HTTP errors included as text.

        Every call carries this seat's uid as a query parameter. The server
        is authoritative for hidden information (`visibility.py`): any answer
        that carries a state view is already filtered to this uid by the time
        it gets here. This only trims image URLs, pure token weight with no
        bearing on what may be seen.

        For an advise `Seat` a committing POST never leaves this method as
        itself - see `_advise_answer`, which is asked first and, when it
        answers, has already recorded the proposal this returns instead.
        """
        advised = self._advise_answer(method, path, body)
        if advised is not None:
            status, payload = advised
            if isinstance(payload, dict):
                _trim_images(payload)
            return f"{_status_line(status)}\n{json.dumps(payload, indent=1, sort_keys=True)}"
        try:
            response = self.http.request(method, path, json=body, params={"uid": self.uid})
            try:
                parsed = response.json()
            except ValueError:
                return f"HTTP {response.status_code}\n{response.text}"
            if isinstance(parsed, dict):
                _trim_images(parsed)
            return f"HTTP {response.status_code}\n{json.dumps(parsed, indent=1, sort_keys=True)}"
        except _AMBIGUOUS_TIMEOUTS as exc:
            return f"HTTP TIMEOUT\n{UNKNOWN_OUTCOME} ({exc})"
        except httpx.HTTPError as exc:
            return f"could not reach the API: {exc}"

    def call_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Like `call`, but the parsed JSON body rather than text - what
        `run_action` needs to inspect whether an action was refused.

        A timeout that arrives *after* the request was sent
        (`_AMBIGUOUS_TIMEOUTS`) is reported the same shape as an ordinary
        refusal (`{"allowed": False, ...}`) so every existing caller's
        dict-merging keeps working, but carries `unknown_outcome: True` so
        `run_action` can tell "the server said no" apart from "we never
        heard back" and word its answer accordingly - see `UNKNOWN_OUTCOME`.
        A connection failure *before* anything was sent is not this kind of
        ambiguity (nothing to have landed) and is left to raise, same as
        before, for `_guarded`/`api_trouble` to report as "unreachable".

        **Every non-2xx response is normalised too** (`_normalise_non_2xx`) -
        this used to return `response.json()` regardless of status, so a 422
        from `records.require_reasoning` (a well-formed request refused
        before it ever reached the engine) came back as FastAPI's bare
        validation shape, `{"detail": [...]}`: no `allowed`, no `paused`, no
        `unknown_outcome`, which `_refused`/`_paused`/`_unknown_outcome` all
        missed and `run_action` read as a completed action. See this
        module's own defect writeup (2026-09-08) for the live reproduction.

        For an advise `Seat` a committing POST is answered by
        `_advise_answer` - asked before `expected_seq` is folded in, so the
        body recorded in the proposal is the one the human's own client will
        post rather than one naming a board position somebody else read.
        """
        advised = self._advise_answer(method, path, body)
        if advised is not None:
            return advised[1]
        body = self._with_expected_seq(method, body)
        return self._request_json(method, path, body)[1]

    def _request_json(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int | None, Any]:
        """`(status, body)` for one request, the body already normalised into
        the tri-state shape `call_json` promises. `status` is `None` for the
        ambiguous-timeout case, which never had one.

        Split out of `call_json` so `_advise_answer` can make requests of its
        own (the `dry_run` probe, the `/advice` record) through exactly the
        same normalisation as every other call, and still report the real
        status code back to `Seat.call`'s text form."""
        try:
            response = self.http.request(method, path, json=body, params={"uid": self.uid})
        except _AMBIGUOUS_TIMEOUTS as exc:
            return None, {
                "allowed": False,
                "reason": f"{UNKNOWN_OUTCOME} ({exc})",
                "unknown_outcome": True,
            }
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if response.status_code >= 400:
            return response.status_code, _normalise_non_2xx(
                response.status_code, parsed, response.text
            )
        if parsed is None:
            return response.status_code, {
                "allowed": False,
                "reason": f"HTTP {response.status_code}: {response.text}",
            }
        return response.status_code, parsed

    def _advise_answer(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int | None, Any] | None:
        """`None` when this request may be sent as it is; `(status, answer)`
        when this is an advise `Seat` making a committing call, in which case
        the proposal has already been recorded by the time this returns.

        The one choke point advise mode is enforced at - `call` and
        `call_json` both ask it, and between them they are every HTTP request
        this module makes. The order of the guards below is the contract:

        1. **Not an advise seat, or not a POST.** Sent. A read is a read, and
           an advisor reads exactly what its human reads.
        2. **A path that is a channel for saying something**
           (`ADVISE_PASSTHROUGH_SUFFIXES`). Sent. `/plan` and `/reflection`
           are seat-only and the server's own refusal is the right answer to
           hand back; `/report` is allowed; `/advice` is this method's own
           output and must not recurse.
        3. **A body that already says `dry_run: true`.** Sent. That is
           already a question rather than a move - an advisor probing the
           engine on purpose gets exactly what a seat would.
        4. **A shape that supports `dry_run`** (`DRY_RUN_SHAPES`). Probed with
           `dry_run: true`; an outcome the engine refuses is handed straight
           back, verbatim and unrecorded, so the advisor tries something else
           rather than proposing something illegal.
        5. **Anything else.** *Nothing is sent.* The proposal is built from
           `available_actions` and marked `validated: false` - see
           `DRY_RUN_SHAPES` for why leaning on the gate's own `dry_run`
           carve-out here would be a way to commit a real action by accident.

        The `/advice` POST itself can be refused (a blank `reasoning`, most
        likely - the route requires the advisor's own words), and that
        refusal is returned rather than swallowed: a proposal nobody recorded
        must never read as one that was.
        """
        if not self.advise or method.upper() != "POST":
            return None
        if path.rstrip("/").endswith(ADVISE_PASSTHROUGH_SUFFIXES):
            return None
        sending = dict(body or {})
        if sending.get("dry_run") is True:
            return None
        shape = ACTION_REQUEST_SHAPES.get(_action_key(path) or "")
        validated = shape in DRY_RUN_SHAPES
        outcome: Any
        if validated:
            status, outcome = self._request_json(method, path, {**sending, "dry_run": True})
            if not (isinstance(outcome, dict) and outcome.get("allowed") is True):
                return status, outcome
        else:
            outcome = {
                "allowed": None,
                "validated": False,
                "reason": ADVISE_UNVALIDATED_REASON,
            }
        # The body the human's own client will post verbatim once it agrees:
        # the human as `player`, and no `dry_run` left in it (the probe above
        # was ours, not theirs). No `expected_seq` either - the row carries
        # `based_on_seq`, read server-side at the moment it is written, and
        # the human's client names its own board when it finally acts.
        ready = {key: value for key, value in sending.items() if key != "dry_run"}
        ready["player"] = self.player
        status, advice = self._request_json(
            "POST",
            f"/api/games/{self.game_id()}/advice",
            {
                "player": self.player,
                "method": method.upper(),
                "path": path,
                "body": ready,
                "reasoning": ready.get("reasoning") or "",
                "validated": validated,
            },
        )
        if not (isinstance(advice, dict) and advice.get("proposed") is True):
            return status, advice
        return status, {
            "proposed": True,
            "log_seq": advice["log_seq"],
            "summary": advice.get("summary"),
            "outcome": outcome,
            "state": self.fetch_state(),
        }

    def fetch_state(self) -> dict[str, Any]:
        """This seat's filtered view of the game, right now (`battlefield_state`'s
        answer, minus image trimming duplication - callers that print it do
        that themselves)."""
        state = self.http.get(f"/api/games/{self.game_id()}", params={"uid": self.uid}).json()
        _trim_images(state)
        return state

    def recent_log(self, *, since: int = 0, limit: int = RECENT_LOG_ENTRIES) -> dict[str, Any]:
        """The tail of this seat's own view of the action log - what happened,
        in the game's words, since `since` (0 for "the whole history, tailed").

        This is the half of the seat's question that no answer used to carry.
        Every state-bearing answer already says what this seat *can do*
        (`available_actions`); none of them said what had *been done*, so
        `prompt.py` told the agent to read `battlefield_state` and then `game_log` -
        two tool calls and two whole inference cycles for one question. Now
        `run_action`, `pass_until`, `block_until_wake` and `battlefield_state` all
        carry this, and the pair collapses to one call.

        One HTTP request, and it can never raise: like `available_actions`
        (see there for the same reasoning at greater length) this is called on
        paths that have already performed a mutating action and must still
        return that action's own outcome, so every failure comes back as
        `log_error` naming what went wrong rather than as an exception. An
        empty `entries` with no `log_error` means "nothing has happened since
        `since`" and nothing else.

        `omitted` is how many entries `limit` dropped off the front. It exists
        so a seat is never quietly handed less than happened: a non-zero
        `omitted` is the signal to call `game_log` for the rest, and the
        entries themselves are headlines (`LOG_HEADLINE_FIELDS`) rather than
        whole rows, which `game_log` also still serves in full.
        """
        params = {"uid": self.uid, "since": since}
        try:
            response = self.http.get(f"/api/games/{self.game_id()}/log", params=params)
        except _AMBIGUOUS_TIMEOUTS as exc:
            return _log_unavailable(since, f"the log call timed out: {exc}")
        except httpx.HTTPError as exc:
            return _log_unavailable(since, f"could not reach the API for the log: {exc}")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code != 200 or not isinstance(payload, dict):
            detail = payload.get("detail") if isinstance(payload, dict) else response.text.strip()
            return _log_unavailable(
                since, f"the log call answered HTTP {response.status_code}: {detail}"
            )
        entries = [entry for entry in (payload.get("log") or []) if isinstance(entry, dict)]
        kept = entries[-limit:] if limit and len(entries) > limit else entries
        last_seq = payload.get("last_seq", since)
        # P4: this is the moment the seat is *handed* a ledger position, so
        # this is where the watermark it will name on its next action is set.
        # Only on a real read - `_log_unavailable`'s early returns above never
        # reach here, so a failed log call leaves the previous watermark
        # standing rather than pretending the seat has seen up to `since`.
        if isinstance(last_seq, int):
            self.last_seq = last_seq
        return {
            "entries": [log_headline(entry) for entry in kept],
            "last_seq": last_seq,
            "omitted": len(entries) - len(kept),
        }

    def declare_plan(
        self,
        *,
        board_read: str,
        my_role: str,
        plan: str,
        expecting: str,
        because: str,
        confidence: str = "medium",
    ) -> dict[str, Any]:
        """`POST .../plan` (`schemas.DeclarePlanIn`) - a thin passthrough,
        no rule of the engine's own re-implemented here. This mutates no
        `Game` and holds no priority to release, so it goes through
        `call_json` directly rather than `run_action`: there is no wake
        condition to check afterward, and none of `run_action`'s auto-pass
        machinery applies to a call that never touched the board.

        `uid` travels in the request body, not only as the query parameter
        every other call already carries: the route checks it there
        (`app.declare_plan`), because a plan is hidden information about how
        this seat means to play, and the server must confirm the caller
        actually *is* the seat it is declaring for before writing anything
        into that seat's name.

        Returns `call_json`'s normalised shape either way - `declared: True`
        with the ledger's own `log_seq` on success, or `{"allowed": False,
        "reason": ...}` on a refusal (a blank field, or a uid that does not
        open this seat) - the same shape every action tool's own outcome
        already is, so a caller checks a refusal the one way it already
        knows rather than learning a second one for this call alone.
        """
        body = {
            "player": self.player,
            "uid": self.uid,
            "board_read": board_read,
            "my_role": my_role,
            "plan": plan,
            "prediction": {"expecting": expecting, "because": because, "confidence": confidence},
        }
        return self.call_json("POST", f"/api/games/{self.game_id()}/plan", body)

    def sources_or_all(self, given: str, state: dict[str, Any] | None = None) -> list[str]:
        """Which mana sources to offer: the ones named, else all untapped ones.

        Shared by both transports so "empty means all of them" can only ever
        mean one thing. It used to be written out twice, and the two copies
        already disagreed: `mcp.py` fell back to every untapped source
        whenever the parsed list came back empty, while `seat_cli.py` decided
        on the raw string, so a `--sources` value that parsed to nothing (a
        bare comma, stray whitespace) offered the engine *no* sources at all
        and the action was refused for mana it plainly had.
        """
        named = _parse_ids(given)
        if named:
            return named
        return self.my_untapped_sources(state if state is not None else self.fetch_state())

    def my_untapped_sources(self, state: dict[str, Any]) -> list[str]:
        """Every untapped mana source I control, the set the UI offers too.

        Offering all of them lets the engine tap only what the cost needs
        (its payment stops at the shortfall); an agent may also pass an
        explicit, smaller set of source ids to any mana-paying tool.
        """
        return untapped_mana_sources(state, self.player)


def untapped_mana_sources(state: dict[str, Any], player: str) -> list[str]:
    """Every untapped mana source `player` controls, read off a state view.

    One rule, one place: `Seat.my_untapped_sources` is this, and so is what
    `baseline_seat.py` offers the engine when it pays for a cast. Offering
    all of them lets the engine tap only what the cost needs (CR 601.2g -
    its payment stops at the shortfall), which is why a caller with no
    opinion should always offer the lot rather than pick.
    """
    return [
        card["id"]
        for card in state["battlefield"]
        if card["controller"] == player
        and not card["tapped"]
        and any(ability["mana"] for ability in card["abilities"])
    ]


class Seats:
    """The door keeper of the single toolset: uid -> the seat it opens.

    Uids are checked against the game itself (GET /api/games/{id}/seat), so
    this process holds no secrets and claiming a seat is impossible without
    the uid given out at dispatch.
    """

    def __init__(self, api: str, game_id: str | None, *, resolve_by_uid: bool = False) -> None:
        self.api = api.rstrip("/")
        #: `--game` pins this process to one id for its whole life - never
        #: re-discovered. `None` means "whatever's live", re-checked below on
        #: every call rather than cached after the first lookup: this process
        #: is spawned once for a whole session, but a playtest loop boots a
        #: brand new game (brand new id, brand new seat uids) once per game,
        #: so caching the id forever used to leave a long-lived MCP process
        #: silently pointed at a game the API no longer had, indistinguishable
        #: from the process itself being dead.
        self._pinned_game_id = game_id
        self._last_seen_game_id: str | None = None
        #: uid -> what `GET /api/seats/{uid}` said about it: `(player, role,
        #: game_id)`. One lookup per uid for the life of this process - a
        #: buddy pod or a seat pod makes thousands of tool calls and the
        #: answer cannot change for a uid that is still valid (a uid is
        #: never reissued for a different seat, game or role).
        self._by_uid: dict[str, tuple[str, str, str]] = {}
        #: uid -> last-woken `board_signature`, shared by reference into
        #: every `Seat` this mints (`seat_for`) so it survives exactly as
        #: long as `_by_uid` does - the cross-call watermark `block_until_wake`
        #: needs, since a `Seat` itself is remade fresh on every call.
        self._wake_signatures: dict[str, Any] = {}
        #: 2b T11: the http transport is one long-running process that may
        #: serve several concurrently-live games at once - "the one live
        #: game" (`game_id()` below) stops being a coherent idea for it, and
        #: `--game` becomes optional. `True` makes `seat_for` resolve each
        #: uid independently via `GET /api/seats/{uid}` (T8's seat-lookup
        #: endpoint, `app.find_seat`) instead of assuming a single game -
        #: `mcp.py`'s `main()` sets this only for the http transport, and
        #: only when no `--game` pins one explicitly.
        self._resolve_by_uid = resolve_by_uid
        # One fixed timeout for every real call this client makes - resolved
        # once for this whole process's life, not per `Seat` (`seat_for`
        # hands the same value to every `Seat` it mints, below).
        self.http = httpx.Client(base_url=self.api, timeout=CLIENT_TIMEOUT_SECONDS)
        # A bare float, not `httpx.Timeout(...)`: tests monkeypatch
        # `httpx.Client` itself to hand back a `TestClient` (built on the
        # unrelated `httpx2` package here), so anything constructed through
        # `httpx.Client(...)` is safely swallowed by that patch - but this
        # assignment reaches the concrete instance directly, and handing it
        # a real `httpx.Timeout` object (a foreign type to `httpx2`) breaks
        # that client's own `__repr__`. A float is what every implementation
        # of `.timeout =` already accepts and normalises itself.
        self.http.timeout = CLIENT_TIMEOUT_SECONDS

    def game_id(self) -> str:
        """The game to play: the one pinned via `--game`, or whichever the
        API currently reports live. In the unpinned case this re-asks the API
        every time rather than caching the answer - see `__init__`. When the
        live id has changed since the last call, `_by_uid` is dropped too: a
        new boot hands out brand new uids, and without this an old, cached
        uid would keep resolving to a player name and now be pointed at the
        new game it was never actually issued for."""
        if self._pinned_game_id is not None:
            return self._pinned_game_id
        games = self.http.get("/api/games").json()["games"]
        if not games:
            raise RuntimeError("no live game on the API - start one first")
        current = games[0]["game_id"]
        if current != self._last_seen_game_id:
            self._by_uid.clear()
            self._last_seen_game_id = current
        return current

    def seat_for(self, uid: str) -> Seat | None:
        """The seat this uid opens, or `None` when the uid is nobody's - an
        **advise** `Seat` when the uid is an advisor's rather than a seat's.

        One lookup for both, `GET /api/seats/{uid}` (`app.find_seat`), for
        every transport. That endpoint is the only place a uid's *role* is
        answered at all, and a buddy pod is dispatched as the seat Job with a
        different uid - no flag, no MCP config change, no new placeholder
        says which it is - so asking here is what makes advise mode work
        identically for stdio, shared-HTTP and `mcp-seat` subprocess mode.

        The pinned (`--game`) path used to ask `GET /api/games/{id}/seat`
        instead, which knows nothing about roles and 404s an advisor uid
        outright, so a buddy pinned to its game was simply nobody. It now
        asks the same question as everyone else and keeps its own isolation
        by *checking* the answer: a uid whose own game is not the pinned one
        is still nobody here, which is what `--game` is for.
        """
        uid = uid.strip()
        # Asked first, and only on the single-game path: it is what drops a
        # `_by_uid` cache belonging to a game that has since been replaced
        # (see `game_id`), so it has to run before `_lookup` reads that cache.
        expected_game = None if self._resolve_by_uid else self.game_id()
        found = self._lookup(uid)
        if found is None:
            return None
        player, role, uid_game = found
        if expected_game is not None and expected_game != uid_game:
            # `--game` pins this process to one game on purpose, and the
            # unpinned single-game path follows whichever one is live.
            # Resolving through the global uid lookup must not turn either
            # into a hint: a uid minted for another live game is nobody here,
            # exactly as it was when this path asked that game directly.
            return None
        return Seat(
            player,
            uid,
            self.api,
            uid_game,
            timeout=self.http.timeout,
            advise=role == "advisor",
            wake_signatures=self._wake_signatures,
        )

    def _lookup(self, uid: str) -> tuple[str, str, str] | None:
        """`(player, role, game_id)` for `uid` from `GET /api/seats/{uid}`,
        cached in `_by_uid` - `None` for a uid no live game recognises.

        Cached because it is on the path of every single tool call and the
        answer cannot change while the uid is still valid. It can stop being
        valid (the game it belongs to ends), and the cache is dropped for
        that: `game_id()` clears `_by_uid` the moment the live game id
        changes under an unpinned process, which is why `seat_for` asks it
        before calling this.

        The `resolve_by_uid` (shared-HTTP) path has no such trigger - it
        serves several live games at once, so there is no single "current"
        one whose replacement would signal a stale cache - and it used to ask
        fresh every call for exactly that reason. It now caches too, and the
        cost of that is narrow and visible: a uid whose game has since ended
        resolves to a `Seat` pointed at a game that is gone, and every call
        it makes answers 404 rather than the cleaner "no such seat". That is
        the right trade for a transport whose whole job is thousands of tool
        calls per uid, and the failure is loud either way."""
        cached = self._by_uid.get(uid)
        if cached is not None:
            return cached
        response = self.http.get(f"/api/seats/{uid}")
        if response.status_code != 200:
            return None
        body = response.json()
        # `role` is T11/wave-1's own field. Defaulted rather than required so
        # a toolset built after an api that predates it still resolves seats:
        # "no role reported" can only mean the api has none to report, and an
        # ordinary seat is the safe reading (an advisor uid would be refused
        # by that api's routes anyway, having no advisor support at all).
        found = (body["player"], body.get("role") or "seat", body["game_id"])
        self._by_uid[uid] = found
        return found


def _trim_images(state: dict[str, Any]) -> None:
    """Drop image URLs off every card in a state-shaped dict, in place.

    Pure token-weight trimming for the agent - an agent cannot look at
    pictures - with no bearing on what may be seen: the server has already
    decided that (`visibility.py`) by the time a response gets here.
    """
    for key in ("battlefield", "stack", "exile", "command"):
        entries = state.get(key)
        if isinstance(entries, list):
            for entry in entries:
                _strip_card_weight(entry)
    for key in ("hands", "graveyards"):
        entries = state.get(key)
        if isinstance(entries, dict):
            for cards in entries.values():
                for entry in cards or []:
                    _strip_card_weight(entry)


def _strip_card_weight(entry: dict[str, Any]) -> None:
    """Drop per-card fields an agent has no use for (image URLs)."""
    if isinstance(entry, dict):
        for field in ("image", "small_image", "art_crop"):
            entry.pop(field, None)


def _parse_ids(value: str) -> list[str]:
    """A comma-separated list of card ids, or a JSON list."""
    value = value.strip()
    if not value:
        return []
    if value.startswith("["):
        return [str(entry) for entry in json.loads(value)]
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_optional_costs(value: str) -> list[dict[str, Any]]:
    """`OptionalCostChoice` objects out of a comma-separated string - the one
    parser `seat_cli.py`'s `--optional-costs` and `mcp.py`'s `optional_costs`
    share, so both front ends send the same body.

    Each entry is a keyword (`Kicker`), optionally followed by `:`-separated
    arguments: a number is how many times a repeatable cost is paid
    (`Multikicker:2`), an id (or id fragment, which the API then refuses as a
    malformed id) is an object the cost names (`Blight:<creature id>`), and
    anything else is the opponent a gift is promised to (`Gift:bob`)."""
    choices: list[dict[str, Any]] = []
    for entry in value.split(","):
        keyword, *arguments = [part.strip() for part in entry.split(":")]
        if not keyword:
            continue
        choice: dict[str, Any] = {"keyword": keyword}
        for argument in arguments:
            if argument.isdigit():
                choice["times"] = int(argument)
            elif _UUID.match(argument) or _ID_FRAGMENT.match(argument):
                choice.setdefault("choosing", []).append(argument)
            elif argument:
                choice["opponent"] = argument
        choices.append(choice)
    return choices


def parse_convoke(value: str) -> list[dict[str, str]]:
    """`ConvokeChoice` objects out of a comma-separated string, where each
    entry is a creature id and, optionally, `:` and the colour symbol that
    creature covers.

    CR 702.51a lets a convoking creature pay "{1} or one mana of that
    creature's color", and those are genuinely different answers: four
    Savannah Lions offered against Conclave Tribunal's `{3}{W}` need three
    against the generic and one named `W`, and offering all four with no
    colour is refused - correctly - with "{W} has no {1} left to pay". An id
    on its own means the generic symbol, which is what the engine's own
    `color=None` already meant, so the plain spelling keeps working unchanged.

    A JSON list of ids is accepted too, the same as `_parse_ids`, and means
    every one against a generic symbol.
    """
    choices: list[dict[str, str]] = []
    for entry in _parse_ids(value):
        card_id, _, color = entry.partition(":")
        choice = {"card_id": card_id.strip()}
        if color.strip():
            choice["color"] = color.strip().upper()
        choices.append(choice)
    return choices


def _parse_pairs(value: str) -> list[dict[str, str]]:
    """A JSON list of {"card": ..., "defender"/"blocked": ...} objects."""
    value = value.strip()
    if not value:
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError('give a JSON list, e.g. [{"card": "...", "defender": "bob"}]')
    return parsed


# -- the "one call, one action" flow ----------------------------------------


def board_signature(state: dict[str, Any]) -> Any:
    """A fingerprint of everything about the board this seat may see, used
    only to tell "the board changed since the call began" apart from "priority
    bounced straight back with nothing new". Two filtered views compare equal
    under this exactly when nothing visible to this seat differs.

    Public because `baseline_seat.py` needs the same question answered - it
    forgets a refused action only once the board has actually moved - and
    this project has twice shipped two implementations of one rule that
    quietly drifted apart (see this module's own docstring)."""
    return {key: value for key, value in state.items() if key not in _NOT_THE_BOARD}


#: Keys a caller may have bolted onto a state view that are this harness
#: talking, not the board: `wake_reason` (`mcp.wait_for_priority` stamps it
#: on) and `recent_log` (`mcp.battlefield_state`). Both change without the board
#: changing - the log strictly grows - so counting either as board state
#: would wake a seat for its own history.
_NOT_THE_BOARD = frozenset({"wake_reason", "recent_log"})


def compute_wake_reason(
    state: dict[str, Any], before: dict[str, Any] | None, player: str
) -> str | None:
    """Which wake condition holds for `player` in `state`, or `None`.

    `before` is this seat's filtered view captured when the call began (or
    `None` for a caller that never had one, e.g. a bare status poll) - see the
    module docstring for why "you have priority" alone is not enough.
    """
    pending = state.get("pending_decision")
    if state.get("winner") or state.get("drawn"):
        return "game over"
    pregame = state.get("pregame")
    if isinstance(pregame, dict):
        # A game booted from decklists *rests* in CR 103.5's mulligan step and
        # then in CR 103.6's opening-hand step before CR 103.8's first turn.
        # Nobody has priority in either (nothing in CR 103.5 or CR 103.6 grants
        # any), so without this a seat sat in `wait_for_priority` until it timed
        # out while the game waited on that very seat to declare.
        if player in (pregame.get("awaiting_mulligan") or []):
            return "the game is waiting on your CR 103.5 mulligan declaration"
        if pregame.get("opening_hand_actor") == player:
            return "it is your turn to take CR 103.6 opening-hand actions, or finish"
        # ...but a seat that has already declared and is waiting on somebody
        # else's declaration is NOT woken. This used to return "the game has
        # not started yet: another seat owes a pregame declaration", and a
        # non-None reason is exactly what makes `block_until_wake` return
        # before its bound - so every `wait_for_priority(max_seconds=...)` a
        # seat made in the pregame came straight back with an unchanged board
        # and the seat spent a whole inference on it. A live pod
        # (`mtg-seat-3b6613e0`) ran 53 of those back to back, one every 3.2
        # seconds for the whole time its human opponent spent thinking, each
        # answer byte-identical to the last.
        #
        # It is not a wake because there is nothing there to act on. CR 103.5:
        # "First, the starting player declares whether they will take a
        # mulligan. Then each other player in turn order does the same. Once
        # each player has made a declaration, all players who decided to take
        # mulligans do so at the same time" - a seat that has declared has no
        # further move until every seat has, and CR 103.6's opening-hand
        # actions do not begin until "the mulligan process (see rule 103.5) is
        # complete". That is the definition of `NOTHING_NEW_YET`, so `None` is
        # the honest answer and the bounded wait gets to do its job. Both
        # rules were read in full (`mise run rules:find`) in the session that
        # changed this.
        return None
    if pending is not None and pending.get("player") == player:
        return f"a decision is waiting for you: {pending.get('prompt')}"
    if (
        state.get("step") in ("untap", "cleanup")
        and state.get("active_player") == player
        and not state.get("stack")
    ):
        return "nobody gets priority in this step and it is your turn: advance the game"
    if state.get("stack") and state.get("priority_holder") == player:
        # The engine refuses `advance` outright while the stack is non-empty
        # (CR 500.2, 117.4) - steer towards the one action that is ever legal
        # here, rather than let a player whose `pass` was refused for some
        # other reason fall back to `advance` and bounce off that refusal too.
        return "the stack is non-empty and you have priority: pass_priority to let it resolve"
    if state.get("priority_holder") == player and (
        before is None or board_signature(state) != board_signature(before)
    ):
        return "you have priority and the board has changed"
    return None


#: `block_until_wake`'s answer when it checked and no wake condition holds
#: yet - poll-and-return (see the module docstring's "No transport-level
#: block left to outlast"): the caller polls by calling again itself, rather
#: than this function holding a request open to notice the change for it.
NOTHING_NEW_YET = "nothing new yet - call again to check"


#: Ceiling for `max_seconds` on `block_until_wake`/`pass_until` - the bound
#: that makes bringing a server-side wait back at all safe. MCP 2026-07-28
#: removing resumability (SEP-2577) is why the old *unbounded* wait (up to
#: 570 seconds) was removed outright rather than merely shortened: a call
#: that blocks holding the request open loses it outright if the underlying
#: stream breaks mid-wait, and that risk scales with how long it sits open.
#: This keeps the bound small - comfortably under both this seat's own
#: game-API client timeout (`CLIENT_TIMEOUT_SECONDS`, 30s) and any
#: transport-level read timeout a real MCP client enforces on a single tool
#: call - so the call still always returns, on its own schedule, rather than
#: risking the connection outliving it. A caller-requested `max_seconds`
#: above this is silently capped, never honoured past it.
MAX_WAIT_SECONDS = 25.0

#: How often a bounded wait re-checks the wake condition. Short enough that a
#: wake condition is noticed promptly once it actually holds; long enough not
#: to hammer the game API with a read every few milliseconds.
WAIT_POLL_SECONDS = 1.0

#: `Seat._wake_signatures.get(seat.uid, ...)`'s "nothing cached yet" default -
#: never a real `board_signature`, which is always a `dict` (possibly empty),
#: so `is` against this tells "never woken" apart from "last woken on a board
#: whose signature happened to be falsy".
_NO_CACHED_WAKE_SIGNATURE = object()


def block_until_wake(
    seat: Seat, before: dict[str, Any] | None, max_seconds: float = 0.0
) -> dict[str, Any]:
    """Check this seat's own state, right now - and, if nothing has changed
    yet, keep checking for up to `max_seconds` (capped at `MAX_WAIT_SECONDS`)
    before giving up and returning `NOTHING_NEW_YET` anyway. Left at its
    default `0.0`, this is exactly the single check-and-return it has always
    been: never blocks, never sleeps.

    This used to poll in an *unbounded* loop for up to a caller-given
    `max_seconds` (60 by default, and an MCP client could ask for more),
    holding one HTTP request open the whole time it waited - up to 570
    seconds for `wait_for_priority` (`mcp.py`). MCP protocol revision
    2026-07-28 removed resumability (SEP-2577): a tool call that blocks like
    that loses the request outright if the underlying stream breaks
    mid-wait, with no way for either side to recover it, so that wait was
    removed outright rather than shortened - the module docstring's "No
    transport-level block left to outlast" section covers the history.

    That removal traded one problem for another: an agent seat with nothing
    to do had no way to wait at all, and started busy-polling with an
    escalating `Bash sleep` between calls instead - the exact log noise a
    predecessor's `pass` re-issued every 1200ms forever was supposed to be
    the last of. `max_seconds` is the bounded version of the wait that was
    removed: it still always returns on its own, and it is capped small
    enough that the SEP-2577 hazard stays negligible (a stream breaking
    during a wait of at most `MAX_WAIT_SECONDS` is the same order of risk as
    it breaking during any other single request) while giving a seat
    something to actually sit on instead of a Bash sleep-and-retry loop it
    has to invent for itself. A caller that needs longer than the bound just
    calls again - `NOTHING_NEW_YET` after the bound elapses says so exactly
    the way it always has, just later than an immediate check would.

    Never touches `store.locked` even while waiting - see the module
    docstring's "why this cannot deadlock": every iteration is an ordinary
    read, and `time.sleep` between them holds nothing.

    `before=None` is not "no baseline" any more - it is "no baseline *for
    this call*", and falls back to `seat`'s own cached `board_signature` from
    the last time a wait woke it (`seat._wake_signatures`, keyed by
    `seat.uid`; empty the first time this uid is ever seen). Without that
    fallback, `compute_wake_reason`'s priority branch treats `before is None`
    as "always different", so a caller with nothing to hand in - `wait_for_
    priority`, `seat_cli wait` - woke on *every* call merely because its
    player held priority, board unchanged or not: an advisor sitting on its
    human's decision spun forever, burning an inference each time. A caller
    that DOES have a real prior board (`run_action`) still passes it
    explicitly and this cache is never consulted for it - only ever a
    fallback for "nothing to compare against", never a second opinion on top
    of one the caller already has.

    The cache is written back on every genuine wake (`reason is not None`),
    regardless of which `before` produced it - `run_action`'s own explicit-
    `before` wake refreshes it too, so a seat's next *bare* wait always diffs
    against the most recent board this seat has actually been shown, whoever
    last saw it.
    """
    if before is None:
        cached = seat._wake_signatures.get(seat.uid, _NO_CACHED_WAKE_SIGNATURE)
        if cached is not _NO_CACHED_WAKE_SIGNATURE:
            before = cached
    max_seconds = max(0.0, min(max_seconds, MAX_WAIT_SECONDS))
    deadline = time.monotonic() + max_seconds
    while True:
        state = seat.fetch_state()
        reason = compute_wake_reason(state, before, seat.player)
        remaining = deadline - time.monotonic()
        if reason is not None or remaining <= 0:
            if reason is not None:
                seat._wake_signatures[seat.uid] = board_signature(state)
            # `recent_log` is a sibling of `state`, never a key inside it:
            # callers hand this very `state` back as the next call's `before`
            # (`run_action`, and `test_action_flow.py`), and `board_signature`
            # would then read a growing log as "the board has changed" and
            # wake the seat for its own history.
            return {
                "state": state,
                "wake_reason": reason or NOTHING_NEW_YET,
                "recent_log": seat.recent_log(),
            }
        time.sleep(min(WAIT_POLL_SECONDS, remaining))


def available_actions(seat: Seat, state: dict[str, Any]) -> dict[str, Any]:
    """What this seat can actually do right now - `GET .../legal-actions`
    (`app.py`), the api's own door onto Phase 1's enumeration surface,
    `machines.legal_actions.legal_actions(game, player)` in the engine.

    T11 replaces what this function used to be: one HTTP `dry_run` round trip
    per hand card, per graveyard/exile card with a `cast_options` entry, and
    per battlefield ability, each collapsed into a bare boolean before ever
    reaching this seat. That probe could only ever report "yes" or "no" - a
    dry run either succeeds or it does not - so a modal or X spell whose
    affordability genuinely cannot be judged yet (CR 107.3a: X is not
    announced until the spell is cast) always came back `castable: false`,
    indistinguishable from a card with no red mana in play. The engine's own
    enumeration carries a third answer, `Affordability.UNKNOWN`
    (`models/action.py`), and this function surfaces it rather than hiding it
    the way the old probe structurally had to - each entry under `actions`
    below carries its own `affordable` (`"yes"`/`"no"`/`"unknown"`) and
    `unaffordable_reason`, in the engine's own words.

    One HTTP call, not N: the server computes the whole pool in one request
    (CR 118.6 - an unpayable cast is still a legal one, so nothing here is
    dropped for being unaffordable), rather than this seat driving one dry
    run per candidate itself.

    Nothing here can leak the opponent's hand, either library, or an
    opponent's own cast options: `legal_actions` enumerates only what
    `seat.player` themself controls or holds (its own module docstring), the
    same guarantee the old probe leaned on `visibility.py`'s redaction for.

    `attackers_requested`/`blockers_requested` are read straight off
    `state["pending_decision"]["kind"]` - a `declare_attackers`/
    `declare_blockers` pause (CR 508.1, CR 509.1) addressed to this seat *is*
    this seat's moment to declare, the one case where a pending decision
    names the single legal thing to do rather than merely narrowing the pool
    (which `legal_actions` itself already does server-side: while a decision
    stands, `actions` holds nothing but that decision's own answers, for the
    player it addresses - see the endpoint's own docstring).

    **An empty `actions` means "you legally cannot do anything", and nothing
    else.** Every way this call can fail instead - the API unreachable, a
    read timeout, a 4xx/5xx, a body that is not the expected JSON - comes
    back with `actions_error` naming what went wrong, and `run_action`
    embeds that in all seven of its own answers. Without it a 404 or a 500
    carrying a JSON body parsed cleanly into `payload.get("actions", [])` and
    reached the seat as a silent, confident empty pool - and `Seat.http`
    never calls `raise_for_status`, so nothing upstream would have caught it
    either. The old per-candidate probe degraded on `httpx.HTTPError` for
    exactly this reason; one call in place of N makes the single failure
    total rather than partial, which is a reason to guard it harder, not
    less. The timeout guard is the same `_AMBIGUOUS_TIMEOUTS` split
    `call_json` documents, for the same reason: this is a plain read, so a
    timeout here changed nothing server-side - it is reported, never raised,
    because `run_action` calls this on paths that have already performed a
    mutating action and must still return that action's own outcome.
    """
    try:
        response = seat.http.get(
            f"/api/games/{seat.game_id()}/legal-actions",
            params={
                "uid": seat.uid,
                "player": seat.player,
                # Opt in to the per-action legal-target candidates
                # (`app._attach_target_candidates`). A seat pays ~106 ms of
                # server time for them on a 28-permanent board and saves a
                # whole round trip - and so a whole inference cycle - per
                # action; the browser, which does not set this, asks the same
                # endpoint on a 1.2s poll and already has the board on screen.
                "candidates": True,
            },
        )
    except _AMBIGUOUS_TIMEOUTS as exc:
        return _actions_unavailable(state, f"the legal-actions call timed out: {exc}")
    except httpx.HTTPError as exc:
        return _actions_unavailable(state, f"could not reach the API for legal actions: {exc}")
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code != 200 or not isinstance(payload, dict):
        detail = payload.get("detail") if isinstance(payload, dict) else response.text.strip()
        return _actions_unavailable(
            state, f"the legal-actions call answered HTTP {response.status_code}: {detail}"
        )
    return _actions_view(state, payload.get("actions") or [], None)


#: What the envelope says once, rather than every row saying it twenty times.
#: Two facts a reader of `actions` needs and cannot get from any single entry:
#: that a `targets` block is ready to be handed straight back, and that a row
#: without one is not a row with no legal targets. CR 118.6 keeps an unpayable
#: cast in the list, and enumerating what it could point at would be bytes
#: spent on a list the seat can do nothing with - so `affordable: "no"` rows
#: carry no candidates, and that is a statement about affordability, never
#: about the board.
TARGETS_NOTE = (
    "an action that targets carries `targets.slots[].candidates` - hand one straight back "
    'as `targets`. A row with `affordable: "no"` is listed (CR 118.6: an unpayable cast is '
    "still a legal one) but carries no candidates, because it cannot be taken yet - that is "
    "NOT 'nothing is legal for it'. `targets.understood: false` means the engine could not "
    "read the targeting phrase and will not guess; name targets yourself."
)


def _actions_view(state: dict[str, Any], actions: list[Any], error: str | None) -> dict[str, Any]:
    pending = state.get("pending_decision")
    kind = pending.get("kind") if pending is not None else None
    view: dict[str, Any] = {
        "actions": actions,
        "attackers_requested": kind == "declare_attackers",
        "blockers_requested": kind == "declare_blockers",
        "pending_decision": pending,
    }
    if any(isinstance(action, dict) and action.get("target_slots") for action in actions):
        view["targets_note"] = TARGETS_NOTE
    if error is not None:
        view["actions_error"] = error
    return view


def _actions_unavailable(state: dict[str, Any], reason: str) -> dict[str, Any]:
    """`available_actions`' answer when the call itself failed: an empty pool
    that says so, rather than one a reader would take for "nothing is legal".
    `attackers_requested`/`blockers_requested`/`pending_decision` are still
    filled in - they are read off the state this seat already has in hand,
    not off the failed call, so they stay true."""
    return _actions_view(
        state,
        [],
        f"{reason} - this is NOT 'you have nothing legal to do': the list could not be "
        "fetched at all. Read your state, or call again.",
    )


def _refused(outcome: Any) -> bool:
    return isinstance(outcome, dict) and outcome.get("allowed") is False


def _game_over(outcome: Any) -> bool:
    """Whether `outcome` is `commands.GameOverError`'s own 409 (CR 104.1) -
    `type: game_over`, named by the server precisely so a client can tell
    this refusal apart from an ordinary one without parsing the sentence
    (see `GameOverError`'s own docstring). It also carries `allowed: False`,
    so `_refused` is true of it too - this is the one case `run_action`'s own
    refusal branch must not answer with `held_priority: True`/"still your
    move": the game is over, so there is no move left to hold (B8-07)."""
    return isinstance(outcome, dict) and outcome.get("type") == "game_over"


def added_mana(outcome: Any) -> bool:
    """Whether `outcome` is a successful activation of a *mana* ability -
    `engine_api.activate_ability` marks exactly those `mana_ability: True`,
    because `Game.activate_ability` returns no stack object for them.

    Why `run_action` treats that as "hold priority" no matter what the caller
    asked for: a mana ability does not use the stack and resolves immediately
    (CR 605.3b), so there is nothing to release priority *for* - and releasing
    it is actively destructive. The auto-pass here is one half of "all players
    pass in succession while the stack is empty", which ends the phase or step
    (CR 500.2, CR 505.2); as it ends, "any unspent mana left in a player's mana
    pool empties" (CR 500.5). So the harness's convenience pass could throw
    away the very mana the call had just made, along with the rest of the
    seat's own main phase. CR 117.3c gives the activating player priority back
    ("if a player has priority when they cast a spell, activate an ability, or
    take a special action, that player receives priority afterward"); for a
    mana ability, keeping it is the only sane default.
    """
    return isinstance(outcome, dict) and outcome.get("mana_ability") is True


def _unknown_outcome(outcome: Any) -> bool:
    """Whether `outcome` is `call_json`'s ambiguous-timeout marker, not an
    ordinary rules refusal - see `UNKNOWN_OUTCOME`. Checked before
    `_refused`: both shapes carry `allowed: False`, but only this one means
    "we do not know what happened", and conflating them would tell a player
    "nothing changed, still your move" about a call that may well have gone
    through."""
    return isinstance(outcome, dict) and outcome.get("unknown_outcome") is True


def _paused(outcome: Any) -> bool:
    """Whether `outcome` is a command that paused on a decision rather than
    completing - `commands.py`'s pause-and-resume: the mutating call itself
    returns immediately with the question attached (`allowed: None`,
    `paused: True`, `pending_decision`), instead of blocking for it the way
    it used to. `run_action` treats this exactly like a decision that used to
    surface mid-flight through `_call_watching_for_self_decision` (below) -
    both mean this seat has to answer through `decide()` before it can tell
    what its own action did. In practice `perform()` now returns fast enough,
    paused or not, that `_call_watching_for_self_decision`'s own polling
    loop rarely gets a chance to fire at all - this is the check that
    actually catches it now."""
    return isinstance(outcome, dict) and outcome.get("paused") is True


def _addressed_to(pending: Any, player: str) -> bool:
    """Whether a paused answer's `pending_decision` is this seat's to answer
    - the `player` it names, never merely the seat whose call paused."""
    return isinstance(pending, dict) and pending.get("player") == player


def _waiting_on_another_seat(pending: Any) -> str:
    """`wake_reason` for a call that paused on a decision some other seat
    owns: whose it is and what it asks, and that this seat does nothing."""
    who = pending.get("player") if isinstance(pending, dict) else None
    prompt = pending.get("prompt") if isinstance(pending, dict) else None
    asked = f": {prompt}" if prompt else ""
    return (
        f"your action is paused on a decision {who or 'another player'} must make{asked} - "
        "it is not yours to answer; it completes once they do. Call wait_for_priority "
        "(or read state) to see what it came to"
    )


def is_stale(outcome: Any) -> bool:
    """Whether `outcome` is the server's own board-moved refusal
    (`records.BoardMoved`, `app._board_moved`): this seat named a ledger
    position the game had already moved past, and nothing was done.

    **The boolean, never the text.** This reads `stale`, the key the refusal
    carries precisely so no client has to match on prose - the house rule
    from the browser fix of 2026-09-08 ("a refused pass is two different
    things"): ask the board, never pattern-match refusal text, because
    engine wording is not a client contract. A stale refusal is a lost race,
    a rules refusal is a misread, and a run that treats the first as the
    second dies on a board it could simply have re-read.

    Distinct from `_refused` on purpose, even though a stale refusal also
    carries `allowed: False`: this one is the single case where retrying the
    identical action is the *right* move, which is what `run_action` does
    with it - once.
    """
    return isinstance(outcome, dict) and outcome.get("stale") is True


#: Marker `_call_watching_for_self_decision` returns in place of an outcome
#: when it gave up on `perform` because a decision addressed to this same
#: seat showed up first - see that function's docstring for why this is not
#: an ordinary refusal or an `UNKNOWN_OUTCOME` (both of those mean the call
#: itself is done, one way or another; this one is still running).
SELF_DECISION_IN_FLIGHT = "SELF_DECISION_IN_FLIGHT"


def _call_watching_for_self_decision(
    seat: Seat, perform: Any, poll_seconds: float = POLL_SECONDS
) -> tuple[Any, dict[str, Any] | None]:
    """Run `perform` (one mutating call) on a background thread while polling
    this seat's own state for a decision addressed to it, so a decision that
    call itself raises *for this same seat* - CR 608.2d mid-resolution, or CR
    603.3d/601.2c putting one of this seat's own triggered abilities on the
    stack - does not sit unanswerable for the server's whole decision timeout.

    Why a decision like that is otherwise unanswerable: `POST .../decide`
    never takes `store.locked(game_id)` (`decisions.py`'s module docstring),
    so a *different* seat's own poll (`block_until_wake`) notices and answers
    a decision addressed to it promptly, well within the timeout - that path
    already works. But a real caller here is one MCP tool call from one
    client with exactly one request in flight at a time (`mcp.py`'s module
    docstring: "one call, one meaningful action"); it cannot send `decide`
    until `perform`'s own HTTP response comes back, and that response will
    not come back until `Game.ask`, deep inside the very request `perform`
    is waiting on, gets an answer. Nothing server-side can fix this - the
    lock was never the problem for this seat, its own single-threaded
    transport is - so the fix has to add a second, concurrent connection on
    the client side, which is what this function is.

    Returns `(outcome, None)` when `perform` finished before any such
    decision showed up - the ordinary case, including an ordinary refusal or
    a decision addressed to someone else, which this never reacts to (that
    seat's own poll is what answers those, unaffected by this one running
    concurrently). Returns `(SELF_DECISION_IN_FLIGHT, pending_decision)` when
    a decision naming this seat appeared first: `perform`'s own HTTP call is
    abandoned here - not aborted, since a request already sent may already be
    landing server-side - on a daemon thread that finishes or times out on
    its own with nobody left watching. The caller told about the decision
    this way has to answer it (`decide`) as a genuinely separate request
    before checking state or the game log for what the abandoned call itself
    ended up doing.
    """
    done = threading.Event()
    box: dict[str, Any] = {}

    def call() -> None:
        try:
            box["outcome"] = perform()
        except Exception as exc:  # re-raised below only if nobody moved on
            box["exception"] = exc
        finally:
            done.set()

    threading.Thread(target=call, daemon=True).start()
    while not done.wait(poll_seconds):
        state = seat.fetch_state()
        pending = state.get("pending_decision")
        if pending is not None and pending.get("player") == seat.player:
            return SELF_DECISION_IN_FLIGHT, pending
    if "exception" in box:
        raise box["exception"]
    return box["outcome"], None


def run_action(
    seat: Seat,
    perform: Any,
    *,
    hold_priority: bool = False,
    release: bool = True,
    reasoning: str | None = None,
) -> dict[str, Any]:
    """Do one action, then behave the way both transports promise: unless
    told to hold, release priority, then check once for this seat's next
    moment to act - and always return the action's own outcome alongside
    whatever state that check saw, in one answer.

    `perform` is a zero-argument callable performing the actual HTTP call and
    returning its parsed JSON body. `release` is `False` only for `pass`
    itself (the action already *is* releasing priority - calling
    pass-priority a second time on top of it would just be refused, harmlessly,
    but there is no reason to make the extra request).

    **Nothing here bounds a wait, because nothing here waits.**
    `block_until_wake` is poll-and-return (see its own docstring - MCP
    2026-07-28 removed resumability, so this call never holds the request open
    waiting for something to happen), so there is no deadline to accept and
    this takes no `max_seconds`: a parameter that bounds nothing is a lie an
    agent reads as a promise, and the MCP wire format shows a caller no
    per-parameter description to correct it with. A caller that wants to keep
    watching just calls the action tool (or `wait_for_priority`) again;
    `wake_reason` says `NOTHING_NEW_YET` rather than blocking to make that
    true.

    Refusal and `hold_priority` both return **immediately, never blocking**:
    a refused action leaves this seat exactly where it was (still its move,
    try something else), and `hold_priority=True` must never block by
    construction - the check happens before `block_until_wake` is ever
    reached, not by timing or luck.

    One outcome overrides `hold_priority=False` and holds anyway: an activated
    *mana* ability (`added_mana`, see there for the CR 605.3b/500.2/500.5
    reasoning). Both transports get that from here rather than from their own
    defaults, because neither can know whether the ability was a mana ability
    until the answer comes back.

    `perform` itself, and the auto-pass below, both go through
    `_call_watching_for_self_decision` rather than being called directly:
    either can cross a decision addressed to this very seat (an ETB trigger
    the seat itself must target, the cleanup discard closing its own turn,
    ...), which - unlike a decision addressed to someone else - this seat's
    own single in-flight call could never otherwise answer. See that
    function's docstring for the full shape of why.

    `reasoning` (T10): the *triggering* action's own stated reasoning, not a
    second one for the auto-pass this function issues on its behalf. That
    auto-pass is this harness releasing priority after an agent's already-
    reasoned action, not a fresh decision of its own - mcp.py's `act` passes
    the same string through here that the tool call itself gave, so an agent
    seat's auto-release never gets refused with a 422 for a reasoning field
    nothing here would know how to fill in honestly.
    """
    before = seat.fetch_state()
    # Read by `answered` below, and set here rather than beside the retry
    # that flips it: every return path goes through `answered`, the advise
    # short-circuit included, so it has to be bound before any of them.
    stale_retry = False

    def answered(state: dict[str, Any], **fields: Any) -> dict[str, Any]:
        """One answer shape for all eight of this function's returns, so a
        field can never be added to seven of them and forgotten in the
        eighth. Three things ride together because a seat needs all three
        after every action and used to spend a separate round trip on each:
        the state the action left behind, this seat's legal actions in it,
        and the tail of what has actually happened (`recent_log` - see
        `Seat.recent_log`)."""
        answer = {
            **fields,
            "state": state,
            "available_actions": available_actions(seat, state),
            "recent_log": seat.recent_log(),
        }
        if stale_retry:
            # P4, and only when it happened: this action was refused as
            # stale once and re-sent against a freshly read board. A reader
            # of the answer (and of `recent_log`, which carries both rows)
            # can see the race rather than wonder why there are two.
            answer["stale_retry"] = True
        return answer

    if seat.advise:
        # Advise mode short-circuits **before** the auto-pass and the wake
        # check below, and that is the whole point: an advisor releasing
        # priority, or sitting in `block_until_wake` on the strength of an
        # action it did not take, would be driving the game rather than
        # advising it. `perform()` itself does not move the game either -
        # `Seat._advise_answer` turns it into a recorded proposal - so there
        # is nothing here to wake for and nothing to hold.
        #
        # None of the machinery above is reached either: no `expected_seq` is
        # armed (the proposal carries the server's own `based_on_seq`), no
        # stale retry is possible (nothing was committed to be stale against)
        # and `_call_watching_for_self_decision` has no in-flight mutation to
        # watch for a self-addressed decision behind.
        proposal = perform()
        state = proposal.get("state") if isinstance(proposal, dict) else None
        outcome = (
            {key: value for key, value in proposal.items() if key != "state"}
            if isinstance(proposal, dict)
            else proposal
        )
        return answered(
            state if isinstance(state, dict) else seat.fetch_state(),
            outcome=outcome,
            held_priority=None,
            wake_reason=ADVISE_WAKE_REASON,
        )

    # P4: name the board this action was decided against - the newest ledger
    # position this seat has been handed (`Seat.last_seq`, set by every
    # `recent_log` this function's own answers already carry). One-shot, so
    # the auto-pass below - sent after this action has itself moved the
    # ledger - names nothing and is never refused as stale.
    seat.send_expected_seq_once(seat.last_seq)
    outcome, self_decision = _call_watching_for_self_decision(seat, perform)
    if self_decision is None and is_stale(outcome):
        # The board moved between this seat's read and this call - a lost
        # race, not a misread. Ask the board what it actually is (the house
        # rule from MEMORY.md 2026-09-08: never pattern-match refusal text),
        # then send the same action once more against it.
        #
        # Once, and never a loop. A seat losing the race twice is a seat
        # whose decision is genuinely out of date, and the honest thing to
        # hand it back is the refusal - with `recent_log` alongside saying
        # what moved - rather than a spin that hides how contended the board
        # is from the very corpus this field exists to feed.
        stale_retry = True
        seat.recent_log()  # re-read: this is what moves `seat.last_seq` on
        seat.send_expected_seq_once(seat.last_seq)
        before = seat.fetch_state()
        outcome, self_decision = _call_watching_for_self_decision(seat, perform)
    if self_decision is None and _paused(outcome):
        # `perform()` itself paused (`commands.py`) rather than a decision
        # surfacing mid-flight through the polling race above - see
        # `_paused`'s own docstring for why this is the common case now.
        paused_on = outcome.get("pending_decision")
        if _addressed_to(paused_on, seat.player):
            self_decision = paused_on
        else:
            # B3-10: a decision is asked of whoever the resolving object
            # names - bob's pass resolving alice's Galvanic Discharge asks
            # alice. It is not this seat's to answer, and there is nothing
            # for this seat to release or wait on inside this call.
            return answered(
                seat.fetch_state(),
                outcome=outcome,
                held_priority=None,
                wake_reason=_waiting_on_another_seat(paused_on),
            )

    if self_decision is not None:
        state = seat.fetch_state()
        return answered(
            state,
            outcome=None,
            held_priority=None,
            wake_reason=(
                "a decision is waiting for you, raised by this very call: "
                f"{self_decision.get('prompt')} - answer it with decide() now; "
                "that unblocks the action you just took, whose own outcome you "
                "will only see afterward, from `recent_log` below or from state"
            ),
            in_flight_action=True,
        )

    if _unknown_outcome(outcome):
        # A transport timeout, not a rules refusal: re-fetch state (a fresh
        # request - reads never take the game lock, see `block_until_wake`'s
        # docstring, so this is safe even if the timed-out call is still
        # parked server-side) so the answer at least carries the truth as of
        # right now, but word the reason honestly rather than as a refusal -
        # `held_priority` is `None` here on purpose: unlike the other
        # branches, this call cannot say whether it does or does not hold
        # priority, because it does not know whether the action happened.
        state = seat.fetch_state()
        return answered(state, outcome=outcome, held_priority=None, wake_reason=UNKNOWN_OUTCOME)

    if _refused(outcome):
        state = seat.fetch_state()
        if _game_over(outcome):
            # B8-07: CR 104.1 - once the game has ended nothing more can be
            # done in it, so a refusal for that reason must not also claim
            # this seat holds priority or has a move to make; both would
            # flatly contradict "the game is over" in the very same answer.
            # The reason the server already gave (`GameOverError.__str__`)
            # says why, so it is reused rather than re-worded here.
            return answered(
                state,
                outcome=outcome,
                held_priority=None,
                wake_reason=outcome.get("reason")
                or "the game is over (CR 104.1) - read the final state and stop",
            )
        return answered(
            state,
            outcome=outcome,
            held_priority=True,
            wake_reason="refused - nothing changed, still your move",
        )

    if hold_priority or added_mana(outcome):
        state = seat.fetch_state()
        return answered(
            state,
            outcome=outcome,
            held_priority=True,
            wake_reason=(
                "hold_priority=true: kept priority, not blocking"
                if hold_priority
                else (
                    "a mana ability does not use the stack (CR 605.3b), so this kept "
                    "priority for you whatever you asked for: releasing it can end the "
                    "step (CR 500.2), and the mana you just made empties as it does "
                    "(CR 500.5). Spend it now, or pass explicitly to throw it away"
                )
            ),
        )

    pass_outcome = None
    # D16: release only priority this seat actually holds. A decision is
    # asked of whoever the resolving object names (bob paying life for his
    # own shockland in alice's turn), not of the priority holder, so after
    # `decide` the seat may hold nothing to release - and a pass posted anyway
    # is refused and recorded as this seat's refusal, inflating the audit
    # tallies (`eval_report`'s `rules_error_rate`) with a move nobody chose.
    if release and seat.fetch_state().get("priority_holder") == seat.player:

        def perform_pass() -> Any:
            body: dict[str, Any] = {"player": seat.player}
            if reasoning:
                body["reasoning"] = reasoning
            return seat.call_json(
                "POST", f"/api/games/{seat.game_id()}/actions/pass-priority", body
            )

        pass_outcome, pass_self_decision = _call_watching_for_self_decision(seat, perform_pass)
        if pass_self_decision is None and _paused(pass_outcome):
            paused_on = pass_outcome.get("pending_decision")
            if not _addressed_to(paused_on, seat.player):
                return answered(
                    seat.fetch_state(),
                    outcome=outcome,
                    pass_outcome=pass_outcome,
                    held_priority=False,
                    wake_reason=_waiting_on_another_seat(paused_on),
                )
            pass_self_decision = paused_on
        if pass_self_decision is not None:
            # Releasing priority - not the action above, which already came
            # back clean - is what crossed a decision only this seat could
            # ever answer (`put_triggers_on_stack` runs at the top of every
            # `pass_priority`, CR 603.3). Same shape as the check on `perform`
            # itself: report it and let the caller `decide()` as a separate
            # call rather than sit behind its own in-flight pass.
            state = seat.fetch_state()
            return answered(
                state,
                outcome=outcome,
                held_priority=None,
                wake_reason=(
                    "your action went through, but releasing priority raised a "
                    f"decision only you can answer: {pass_self_decision.get('prompt')} - "
                    "answer it with decide() now; check state or `recent_log` "
                    "afterward for what releasing priority itself then did"
                ),
                in_flight_action=True,
            )
        if _unknown_outcome(pass_outcome):
            # The action itself is confirmed (we are past that check above);
            # it is *releasing priority* that is now ambiguous. Blocking
            # further would silently assume an answer this call does not
            # have - the resolving wake_reason from `block_until_wake` could
            # easily be "you have priority", which is simply not knowable
            # yet.
            state = seat.fetch_state()
            return answered(
                state,
                outcome=outcome,
                pass_outcome=pass_outcome,
                held_priority=None,
                wake_reason=UNKNOWN_OUTCOME,
            )

    woken = block_until_wake(seat, before)
    result = answered(
        woken["state"],
        outcome=outcome,
        held_priority=False,
        wake_reason=woken["wake_reason"],
    )
    if pass_outcome is not None:
        result["pass_outcome"] = pass_outcome
    return result


# ------------------------------------------------------------- auto-passing --
#
# One call that keeps passing priority until a *named* point in the turn,
# shared by `mcp.pass_priority(until=...)` and `seat_cli pass --until`, so the
# two transports cannot drift on it. That sharing is the whole finding of this
# repo's front-end parity audit: the mana-ability hold was wrong in one
# transport and right in the other precisely because the behaviour lived in the
# transports instead of here.
#
# The shape is the web UI's, deliberately. `ui/app.js`'s "Feature 3" autopilot
# already shipped this for the human player - `autopilotTargets`,
# `startAutopilot`, `autopilotStopReason`, `forgetStartingStep` - and it is the
# proven design, including the two stop toggles a player ticks in the Priority
# panel. A player and an agent stopping in different places would be a bug in
# itself, so the conditions below are the same conditions in the same order.
#
# A seat must still be able to pass one priority at a time. Auto-passing is a
# convenience for stretches where this seat genuinely cannot act; a pass the
# opponent is watching is part of the game (see `docs/ai_seat_brief.md`), and
# nothing here takes that away.

#: The steps a run may be told to stop at, in turn order (CR 500.1's phases and
#: their steps). `untap` and `cleanup` are deliberately absent: no player
#: receives priority in either (CR 502.4, CR 514.3), so a run that passed its
#: way toward one would be naming a point it can never be given priority in.
#: The same list, and the same two omissions, as `ui/app.js`'s `STEPS` feeding
#: `autopilotTargets`.
AUTOPASS_STEPS = (
    "upkeep",
    "draw",
    "precombat_main",
    "beginning_of_combat",
    "declare_attackers",
    "declare_blockers",
    "combat_damage",
    "end_of_combat",
    "postcombat_main",
    "end_step",
)

#: Whose turn the named step has to belong to. `mine` and `opponent` are read
#: against `active_player`; `any` stops at the first occurrence either way.
AUTOPASS_WHOSE_TURN = ("mine", "opponent", "any")


def autopass_target_error(until_step: str, whose_turn: str) -> str | None:
    """Why this destination cannot be run to, or `None` if it can.

    Returned as an ordinary refusal string by both transports rather than
    raised: a mistyped step name is a caller error to correct, not a crash to
    take a long-lived MCP server down with.
    """
    if until_step not in AUTOPASS_STEPS:
        legal = ", ".join(AUTOPASS_STEPS)
        extra = ""
        if until_step in ("untap", "cleanup"):
            extra = (
                f" No player receives priority in the {until_step} step "
                "(CR 502.4, CR 514.3), so no run can ever stop there - name the "
                "step after it instead."
            )
        return f"'{until_step}' is not a step this can stop at. Legal steps: {legal}.{extra}"
    if whose_turn not in AUTOPASS_WHOSE_TURN:
        return (
            f"'{whose_turn}' is not a turn to stop in. "
            f"Legal values: {', '.join(AUTOPASS_WHOSE_TURN)}."
        )
    return None


def start_autopass(state: dict[str, Any], until_step: str, whose_turn: str) -> dict[str, Any]:
    """The bookkeeping one auto-pass run carries, stamped from the board it
    starts on. Mirrors `ui/priority.js`'s `startRun` (the buildless-UI port
    of the old `ui/app.js` autopilot this function itself was always the
    Python twin of).

    `started_turn`/`started_step` are the reason this is a function and not a
    literal: naming the step you are *already standing in* means the NEXT
    occurrence of it, never this one. A run that stopped where it started
    would be a no-op, which is never what the caller meant - "keep passing
    until my next upkeep", asked in an upkeep, is a whole turn of passing.
    `autopass_stop_reason` declines that one occurrence, and
    `forget_starting_occurrence` clears the stamp the moment the board leaves
    it.

    `started_pending_kind` narrows that same exemption for
    `declare_attackers`/`declare_blockers` specifically: a run that began on
    the paused declaration and a run that began in the same step after it was
    answered show the same `(turn_number, step)` pair, and only the pending
    decision's own kind tells "the pause the run began already sitting on"
    apart from the rest of that step - `None` when the run did not begin
    mid-pause at all.

    `known_stack_ids` is the same idea for the stack: only objects that arrive
    *after* the run began count as "something new landed", or a run started
    with a spell already resolving would stop immediately on it. A list rather
    than a set, like every other value here: the whole dict is handed back to
    the caller as `pass_until`'s `resume` token and has to survive a JSON
    round trip through an MCP tool argument.
    """
    pending = state.get("pending_decision")
    return {
        "step": until_step,
        "whose_turn": whose_turn,
        "started_turn": state.get("turn_number"),
        "started_step": state.get("step"),
        "started_pending_kind": pending.get("kind") if pending is not None else None,
        "known_stack_ids": [obj.get("id") for obj in (state.get("stack") or [])],
    }


def resume_autopass(
    resume: dict[str, Any] | None, state: dict[str, Any], until_step: str, whose_turn: str
) -> dict[str, Any]:
    """The run `pass_until` is continuing, or a fresh one stamped from the
    board when there is nothing to continue.

    A `resume` token is a previous call's own run dict handed straight back
    (`pass_until`'s answer carries one whenever `finished` is `false`), so
    everything it holds - above all the starting-occurrence stamp
    `start_autopass` documents - stays the stamp of where the run *began*,
    not of wherever the seat happens to be standing when the caller gets
    round to calling again. `step`/`whose_turn` are taken from the arguments
    rather than the token, so a caller that changes its destination mid-run
    gets the new one; every other field is the token's.
    """
    if not resume:
        return start_autopass(state, until_step, whose_turn)
    run = dict(resume)
    run["step"] = until_step
    run["whose_turn"] = whose_turn
    run.setdefault("known_stack_ids", [])
    return run


def forget_starting_occurrence(run: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Drop the starting-occurrence stamp once the board has actually left that
    occurrence, so the next time that step comes round the run stops there.

    Compares the whole `(turn_number, step)` pair rather than the step alone,
    which is what makes it right even where one turn holds two of the same
    step: CR 500.8 lets an effect add a phase to a turn and CR 500.9 lets one
    add a step, so a second combat phase brings a second declare-attackers step
    on the same turn number. `ui/app.js`'s `forgetStartingStep` is the same
    function for the same reason.
    """
    if run.get("started_step") is None:
        return run
    still_there = (
        state.get("step") == run["started_step"] and state.get("turn_number") == run["started_turn"]
    )
    if not still_there:
        run["started_step"] = None
        run["started_turn"] = None
    return run


def autopass_label(run: dict[str, Any]) -> str:
    """How a run names its own destination, for the stop reason it reports."""
    step = (run.get("step") or "").replace("_", " ")
    whose = run.get("whose_turn") or "mine"
    if whose == "mine":
        return f"your {step}"
    if whose == "opponent":
        return f"your opponent's {step}"
    return f"the next {step}"


def autopass_stop_reason(
    state: dict[str, Any],
    run: dict[str, Any],
    player: str,
    *,
    stop_on_stack: bool = True,
    stop_when_attacked: bool = True,
    stop_when_you_can_attack: bool = True,
) -> str | None:
    """Whether this auto-pass run must stop, and why - a pure function of the
    board, so both transports and the tests exercise the real conditions
    without a game in the loop.

    Checked in the same order `ui/app.js`'s `autopilotStopReason` checks them,
    so a human's autopilot and an agent's run stop in the same places:

    1. the game is over, or has not started yet (CR 103.5's mulligan step and
       CR 103.6's opening-hand step grant nobody priority, so a run that kept
       "passing" through them would burn its whole bound while the game waited
       on this very seat to declare);
    2. a decision is addressed to this seat (CR 608.2d) - it freezes the game
       until answered, and answering is the only legal move;
    3. the named destination is reached (next occurrence, never the one the
       run started in);
    4. this seat is the active player in its own declare-attackers step,
       unless `stop_when_you_can_attack` is off;
    5. this seat is the defending player in a declare-blockers step with
       attackers declared, unless `stop_when_attacked` is off;
    6. something new is on the stack, unless `stop_on_stack` is off.

    Conditions 4 and 5 are the two that are not merely convenience, and they
    exist for one shared reason: **both declarations are turn-based actions
    that "doesn't use the stack" (CR 508.1 for attackers, CR 509.1 for
    blockers; CR 703.4i and CR 703.4j put each immediately after its step
    begins - all verified with `knowledge_search` in the session that wrote
    this), so condition 6 can never see either of them.** Condition 2 is
    different now than it once was: pausing put `DECLARE_ATTACKERS` and
    `DECLARE_BLOCKERS` in `decisions.ANSWERABLE_KINDS` alongside every other
    kind, so `pending_decision` really is set for both - `actions/attack`/
    `actions/declare-blockers` are thin aliases over the same paused-command
    tape `decide` resumes, not a second transport `pending_decision` cannot
    see. So condition 2 has to know to step aside for exactly these two kinds
    and let conditions 4/5 govern them on their own toggles
    (`stop_when_you_can_attack`/`stop_when_attacked`) instead - a bare
    `pending.player == player` check would otherwise fire condition 2 for a
    combat declaration even when the caller explicitly asked not to stop for
    one, since a `pending_decision` addressed to this seat is exactly what a
    paused `declare_attackers`/`declare_blockers` looks like.

    One more point conditions 4 and 5 both rest on: a paused `POST
    .../advance` (or the `pass-priority` that crosses the same turn-based
    action while releasing priority) commits nothing, but a read during the
    pause names the step the question stands in (`serialize._paused_overlay`,
    battle 3 B3-08). `state["step"] == "declare_attackers"` is also true for
    the whole step after the declaration is answered, so it cannot tell the
    two apart; both conditions read `pending_decision["kind"]`, which is
    stamped the moment the question is asked.

    The two costs are different and worth stating separately rather than
    lumping together:

    - without condition 5, a seat running "until my upkeep" through the
      opponent's turn sails through its own declare-blockers step and takes
      the whole attack unblocked - a lost game, not a slow-play annoyance;
    - without condition 4, a seat running "until my postcombat main" passes
      its own declare-attackers step and simply does not attack. That loses an
      attack rather than the game, and what makes it worth stopping for is
      that it is *silent*: the engine's `_ask_declaration`
      (`machines/decisions.py`) falls through to its default answer, and "an
      unscripted player declares nobody, which is always legal", so the turn
      produces no attack and nothing anywhere says why.

    Both are skipped for the occurrence the run *started in*, exactly as
    condition 3 is, and not merely by analogy: asking to auto-pass past combat
    while already standing in that very step is a seat that has looked at
    combat and chosen to move on, and a condition firing on that state would
    stop the run before it moved at all - a dead call rather than a safeguard.
    The next combat is still stopped for, because by then
    `forget_starting_occurrence` has cleared the stamp.

    Deliberately NOT checked in either: whether this seat controls a creature
    that could legally attack, or block any of these attackers. Summoning
    sickness, defender, tap state, evasion, menace, protection and the whole
    of CR 508.1's and CR 509.1's restrictions and requirements are engine
    questions, and guessing at them here would be a worse bug than the ones
    this prevents. "It is your declare-attackers step" and "you are being
    attacked and the game is asking you to block" are the right granularity -
    the same granularity `ui/app.js` uses, so the human's two checkboxes and
    these two parameters mean exactly the same thing.
    """
    if state.get("winner"):
        return f"game over - {state['winner']} won"
    if state.get("drawn"):
        return "game over - drawn"
    if isinstance(state.get("pregame"), dict):
        return "the game has not started yet: the pregame (CR 103.5, CR 103.6) is still open"
    pending = state.get("pending_decision")
    pending_kind = pending.get("kind") if pending is not None else None
    addressed_to_you = pending is not None and pending.get("player") == player
    if addressed_to_you and pending_kind not in ("declare_attackers", "declare_blockers"):
        return f"a decision is waiting for you: {pending.get('prompt')}"

    # Hoisted above all three conditions that use it - the destination and
    # both combat stops - rather than computed inside the destination check,
    # because "not the occurrence this run started in" is one rule that all
    # three obey. `ui/priority.js`'s `sameOccurrence` hoists the same value
    # for the same reason.
    same_occurrence_this_run_started_in = (
        run.get("started_step") is not None
        and state.get("step") == run["started_step"]
        and state.get("turn_number") == run["started_turn"]
    )
    # Conditions 4 and 5 cannot reuse that value on its own: the paused
    # declaration and the rest of its step after it is answered show the
    # same `(turn, step)` pair (see this function's own docstring above).
    # `started_pending_kind`
    # (`start_autopass`) is what tells them apart: the exemption for a given
    # combat kind only holds when the run began already sitting on *that*
    # kind's own pause, not merely on a `(turn, step)` pair a later, different
    # pause happens to have inherited.
    same_combat_pause_this_run_started_in = (
        same_occurrence_this_run_started_in and run.get("started_pending_kind") == pending_kind
    )

    wanted = run.get("step")
    whose = run.get("whose_turn") or "mine"
    if wanted and state.get("step") == wanted:
        is_yours = state.get("active_player") == player
        whose_matches = whose == "any" or (whose == "mine") == is_yours
        if whose_matches and not same_occurrence_this_run_started_in:
            return f"reached {autopass_label(run)}"

    if (
        stop_when_you_can_attack
        and not same_combat_pause_this_run_started_in
        and addressed_to_you
        and pending_kind == "declare_attackers"
    ):
        return (
            "it is your declare-attackers step: declaring attackers does not use "
            "the stack (CR 508.1), so nothing else here would have woken you - "
            "attack, or carry on"
        )

    if (
        stop_when_attacked
        and not same_combat_pause_this_run_started_in
        and addressed_to_you
        and pending_kind == "declare_blockers"
    ):
        attacks = ((state.get("combat") or {}).get("attacks")) or []
        if attacks:
            creatures = "creature" if len(attacks) == 1 else "creatures"
            return (
                f"you are being attacked by {len(attacks)} {creatures} and it is your "
                "declare-blockers step: declaring blockers does not use the stack "
                "(CR 509.1), so nothing else here would have woken you"
            )

    if stop_on_stack:
        arrived = [
            obj
            for obj in (state.get("stack") or [])
            if obj.get("id") not in run.get("known_stack_ids", set())
        ]
        if arrived:
            return (
                f"something new went on the stack: {arrived[0].get('name') or arrived[0].get('id')}"
            )
    return None


#: How many priorities one `pass_until` call will give up before handing the
#: board back of its own accord.
#:
#: NOT a wait bound - nothing in that loop waits any more, and every iteration
#: past the first is paid for by a real, completed `pass-priority` that moved
#: the game. This is the safety net under that reasoning: if a pass were ever
#: to come back `allowed: True` without actually releasing priority, the loop's
#: "keep going while I still hold priority" condition would be true forever and
#: nothing else in it would ever stop. A cap on *work done* is honest where a
#: cap on *seconds waited* was not; hitting it is reported as its own stop
#: reason with `finished: false`, the same shape every other early return
#: takes.
MAX_PASSES_PER_CALL = 100


def pass_until(
    seat: Seat,
    *,
    until_step: str,
    whose_turn: str = "mine",
    stop_on_stack: bool = True,
    stop_when_attacked: bool = True,
    stop_when_you_can_attack: bool = True,
    resume: dict[str, Any] | None = None,
    reasoning: str | None = None,
    max_seconds: float = 0.0,
) -> dict[str, Any]:
    """`_pass_until_once` (below), optionally ridden out across a bounded wait
    rather than handed straight back the first time it would otherwise have
    to wait for the opponent.

    Left at its default `max_seconds=0.0`, this is exactly one call to
    `_pass_until_once` and nothing else - the poll-and-return behaviour that
    function's own docstring describes, unchanged for every existing caller.
    A positive `max_seconds` (capped at `MAX_WAIT_SECONDS`, the same ceiling
    `block_until_wake` uses and for the same SEP-2577 reasoning - see its own
    comment) retries a hand-back that is genuinely just *waiting on the
    opponent* - `_pass_until_once` marks it `retryable` when that is what it
    is - sleeping `WAIT_POLL_SECONDS` between attempts, until either the run
    makes it past that wait (possibly all the way to `finished: true`) or the
    bound runs out. `MAX_PASSES_PER_CALL`'s own hand-back is deliberately
    never retried this way: that cap exists so the caller gets to *look at
    the board*, and retrying it automatically would spend the whole bound
    re-hitting the same cap instead.

    The owner's problem this exists for: `wait_for_priority`'s own bounded
    wait (`block_until_wake`) covers the space between a seat's own actions,
    but a `pass_until` run handed back specifically because it is waiting on
    the opponent used to have nothing to sit on either, so a seat with
    nothing else to do reached for `Bash sleep` and a second call instead -
    the exact log noise the owner reported. This is that same bounded wait,
    on this tool too.
    """
    if seat.advise:
        # Refused up front, before a single priority is passed: see
        # `ADVISE_NO_PASS_UNTIL`. The answer keeps `_pass_until_once`'s own
        # shape so a caller reads this refusal the one way it already knows.
        state = seat.fetch_state()
        return {
            "stopped_because": ADVISE_NO_PASS_UNTIL,
            "passes": 0,
            "finished": True,
            "until": {"step": until_step, "whose_turn": whose_turn},
            "state": state,
            "available_actions": available_actions(seat, state),
            "recent_log": seat.recent_log(),
        }
    max_seconds = max(0.0, min(max_seconds, MAX_WAIT_SECONDS))
    deadline = time.monotonic() + max_seconds
    kwargs: dict[str, Any] = {
        "until_step": until_step,
        "whose_turn": whose_turn,
        "stop_on_stack": stop_on_stack,
        "stop_when_attacked": stop_when_attacked,
        "stop_when_you_can_attack": stop_when_you_can_attack,
        "reasoning": reasoning,
    }
    result = _pass_until_once(seat, resume=resume, **kwargs)
    while not result["finished"] and result.get("retryable", False) and time.monotonic() < deadline:
        time.sleep(min(WAIT_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        result = _pass_until_once(seat, resume=result.get("resume"), **kwargs)
    result.pop("retryable", None)
    return result


def _pass_until_once(
    seat: Seat,
    *,
    until_step: str,
    whose_turn: str = "mine",
    stop_on_stack: bool = True,
    stop_when_attacked: bool = True,
    stop_when_you_can_attack: bool = True,
    resume: dict[str, Any] | None = None,
    reasoning: str | None = None,
) -> dict[str, Any]:
    """Pass every priority this seat is *currently* able to pass, up to a named
    point in the turn - then return. Never sleeps, never waits for the
    opponent, never holds the request open for a deadline - `pass_until`
    (above) is the public entry point, and the only thing that ever calls
    this directly; its own bounded wait is what a caller who wants to wait
    at all should reach for, not a loop around this function by hand.

    This used to run a deadline loop: `max_seconds` (60 by default, and an MCP
    client could ask for more) with a `time.sleep` in every branch that had
    nothing to do yet, so one tool call sat open for as long as the opponent
    took to think. MCP protocol revision 2026-07-28 removed resumability
    (SEP-2577) - a call that blocks like that loses the request outright if the
    underlying stream breaks mid-wait - and this was the tool most likely to be
    holding one open when it did. `block_until_wake` and `wait_for_priority`
    were converted for that reason; this is the same conversion, and
    `max_seconds`/`poll_seconds` are gone rather than kept as inert
    parameters.

    So: check the stop conditions, act, look again, and **return the moment
    this seat would otherwise have to wait for somebody else** - the other
    seat holding priority, or a combat declaration only they can answer. The
    answer says where the run got to; the caller calls again.

    Progress within one call is not waiting and is not capped by the clock:
    every extra lap is paid for by a `pass-priority` that already completed
    and moved the game (or by answering this seat's own frozen combat
    declaration). A seat whose opponent answers promptly can therefore still
    cross several steps in one call, exactly as before - what it can no longer
    do is sit there while nothing happens. `MAX_PASSES_PER_CALL` bounds the
    work rather than the wall clock; see its own comment.

    `resume` is what makes a run that spans several calls one run rather than
    a fresh one each time, and it is not optional bookkeeping: the
    starting-occurrence exemption (`start_autopass`, and conditions 3/4/5 of
    `autopass_stop_reason`) deliberately does not stop for the step the run
    *began* in. Restarting the run on every call would re-stamp that exemption
    against wherever the seat happens to be standing - so a run that came back
    while sitting on its own `declare_attackers` pause would, on the very next
    call, treat that pause as "the step I started in" and pass straight
    through its own attack. That is the exact silent-no-attack defect
    condition 4 exists to prevent. Every answer with `finished: false`
    therefore carries a `resume` token; hand it back unchanged.

    Never touches `store.locked` (it only reads state and posts ordinary
    actions), so it cannot deadlock against the opponent's own moves - the
    same invariant `block_until_wake` documents.

    A `declare_attackers`/`declare_blockers` pause (CR 508.1, CR 509.1)
    addressed to this seat is not always a stop - `stop_when_you_can_attack`/
    `stop_when_attacked` may say not to - but it always freezes the game
    until answered (the turn-based action that would move `state["step"]`
    past it never finished), so when `autopass_stop_reason` does not stop for
    one, this call answers it with "nobody" (`actions/attack`/
    `actions/declare-blockers`, "if any" makes that always legal) before
    looking again. That is progress this seat can make on its own, so it does
    not end the call.

    The answer carries `stopped_because` (which condition fired, or why it
    handed the board back), `passes` (how many priorities this call actually
    gave up), `finished` (`True` only when a real stop condition fired -
    `False` means "call again, with `resume`"), the state it stopped on, and
    this seat's `available_actions` in it.

    A pass refused mid-run stops the run and reports the refusal rather than
    hammering the same refused call again.

    `reasoning` (T10): threaded onto every individual pass this run makes,
    same as `run_action`'s own - "I am auto-passing through this stretch" is
    itself the reasoning an agent seat gives once, for the whole run, not a
    fresh one per pass.
    """
    before = seat.fetch_state()
    run = resume_autopass(resume, before, until_step, whose_turn)
    passes = 0

    def answer(state: dict[str, Any], reason: str, finished: bool = True) -> dict[str, Any]:
        result = {
            "stopped_because": reason,
            "passes": passes,
            "finished": finished,
            "until": {"step": until_step, "whose_turn": whose_turn},
            "state": state,
            "available_actions": available_actions(seat, state),
            # A run crosses windows this seat never looks at, so it is the
            # tool with the most to say about what happened while it was not
            # watching - and until this it said none of it, leaving the seat
            # to spend a second call on `game_log` to find out what it had
            # just passed through. See `Seat.recent_log`.
            "recent_log": seat.recent_log(),
        }
        if not finished:
            # A plain copy: `run` is JSON-safe by construction (see
            # `start_autopass`), and this is the token `resume_autopass`
            # takes straight back on the next call.
            result["resume"] = dict(run)
        return result

    def handed_back(
        state: dict[str, Any], reason: str, *, retryable: bool = True
    ) -> dict[str, Any]:
        result = answer(
            state,
            f"{reason} - this call is not waiting for it: {passes} "
            "priorities passed so far. Call again (with `resume`) to keep going.",
            finished=False,
        )
        # Read by `pass_until`'s own bounded-wait wrapper: `retryable` says
        # whether this hand-back is worth automatically retrying inside that
        # wait. Waiting on the opponent is (the default); the
        # `MAX_PASSES_PER_CALL` cap below is not - retrying it would just
        # spend the whole wait re-hitting the same cap instead of letting
        # the caller look, which is the entire point of that cap.
        result["retryable"] = retryable
        return result

    while True:
        state = seat.fetch_state()
        reason = autopass_stop_reason(
            state,
            run,
            seat.player,
            stop_on_stack=stop_on_stack,
            stop_when_attacked=stop_when_attacked,
            stop_when_you_can_attack=stop_when_you_can_attack,
        )
        if reason is not None:
            return answer(state, reason)

        run["known_stack_ids"] = [obj.get("id") for obj in (state.get("stack") or [])]
        forget_starting_occurrence(run, state)

        pending = state.get("pending_decision")
        pending_kind = pending.get("kind") if pending is not None else None
        if (
            pending is not None
            and pending.get("player") == seat.player
            and pending_kind in ("declare_attackers", "declare_blockers")
        ):
            # `autopass_stop_reason` above already decided not to stop for
            # this pause - either it is not addressed to this seat's combat
            # toggles at all, or the seat asked not to stop for combat
            # (`stop_when_you_can_attack=False`/`stop_when_attacked=False`).
            # Either way the game is genuinely frozen (CR 508.1/CR 509.1's
            # turn-based action never finished, so `state["step"]` never
            # moved past it either - see `autopass_stop_reason`'s docstring)
            # until *somebody* answers, and "declare nobody" is always legal
            # ("if any"). `actions/attack`/`actions/declare-blockers` are the
            # aliases that already build the right `pairs` shape for this
            # decision (`app.py`'s `action_attack`/`action_declare_blockers`,
            # thin wrappers over the same paused-command tape `decide`
            # resumes) - used here rather than posting to `decide()` directly
            # with a hand-built answer, matching how a live client answers
            # these two kinds. This is progress this seat can make with
            # nobody's help, so the call carries on rather than handing back.
            if pending_kind == "declare_attackers":
                path = f"/api/games/{seat.game_id()}/actions/attack"
                body: dict[str, Any] = {"attackers": []}
            else:
                path = f"/api/games/{seat.game_id()}/actions/declare-blockers"
                body = {"player": seat.player, "blocks": []}

            def perform_declare(path: str = path, body: dict[str, Any] = body) -> Any:
                # Bound as defaults, not closed over directly: this closure
                # is called before either loop-local is ever reassigned, but
                # ruff's B023 cannot see that this loop always `continue`s or
                # `return`s before its next reassignment, so it flags the
                # bare closure as if the call could run late and see a
                # different `path`/`body` than the one built for it.
                return seat.call_json("POST", path, body)

            outcome, self_decision = _call_watching_for_self_decision(seat, perform_declare)
            if self_decision is None and _paused(outcome):
                self_decision = outcome.get("pending_decision")
            if self_decision is not None:
                return answer(
                    seat.fetch_state(),
                    "declaring nobody raised a decision only you can answer: "
                    f"{self_decision.get('prompt')} - answer it with decide() now",
                )
            if _unknown_outcome(outcome):
                return answer(seat.fetch_state(), UNKNOWN_OUTCOME)
            if _refused(outcome):
                return answer(
                    seat.fetch_state(),
                    f"declaring nobody was refused, so the run stopped: {outcome.get('reason')}",
                )
            # Answering a declaration is a turn-based action (CR 508.1, CR
            # 509.1), not a priority pass, so `passes` deliberately does not
            # count it - a caller reading `passes` is asking how many
            # priorities this run gave up, not how many turn-based actions it
            # answered on this seat's behalf.
            continue

        if pending is not None:
            # Any decision still outstanding, addressed to anyone - this seat's
            # own already stopped the run in `autopass_stop_reason`, except the
            # two combat kinds the branch above answers, so what reaches here
            # is somebody else's. Nothing this seat can do moves the game until
            # they answer, and `state["priority_holder"]` cannot be trusted
            # here: a paused command rolls the committed game back to before
            # the attempt (`commands.py`), so the value this state carries is
            # simply whoever held priority before the freeze - not "nobody has
            # priority", which is what a frozen game actually is.
            #
            # Passing on the strength of it is worse than useless. The pass
            # cannot complete (it crosses the same unanswered question), so it
            # pauses too - and `app.py`'s `_run_command` stores that second
            # pause over the first, discarding the question the other seat was
            # about to answer. A run that kept re-posting one is how a stored
            # pause slot got clobbered and an accepted answer went missing. So
            # this is a hand-back: the caller looks, and calls again once the
            # decision is answered.
            if pending_kind in ("declare_attackers", "declare_blockers"):
                frozen_on = f"{pending_kind} (CR 508.1, CR 509.1)"
            else:
                frozen_on = f"{pending_kind} decision (CR 608.2d)"
            return handed_back(
                state,
                f"the game is frozen on {pending.get('player')}'s own "
                f"{frozen_on} and only they can answer it",
            )

        if state.get("priority_holder") == seat.player:
            if passes >= MAX_PASSES_PER_CALL:
                return handed_back(
                    state,
                    f"this one call has already passed {MAX_PASSES_PER_CALL} priorities "
                    "without ever having to wait, which is more than any real stretch "
                    "of a turn needs - handing the board back so you can look at it",
                    retryable=False,
                )

            def perform_pass() -> Any:
                body: dict[str, Any] = {"player": seat.player}
                if reasoning:
                    body["reasoning"] = reasoning
                return seat.call_json(
                    "POST", f"/api/games/{seat.game_id()}/actions/pass-priority", body
                )

            # Same shape as `run_action`'s auto-pass: releasing priority runs
            # `put_triggers_on_stack` (CR 603.3) and can raise a decision only
            # this seat can ever answer, which this seat's own single in-flight
            # call could never get to. Hand it back rather than sit behind it.
            outcome, self_decision = _call_watching_for_self_decision(seat, perform_pass)
            if self_decision is None and _paused(outcome):
                # Unlike `_call_watching_for_self_decision`'s own polling
                # (which only ever hands back a decision addressed to this
                # seat - see its docstring), `perform_pass`'s own response
                # carries whatever `pending_decision` the pause landed on,
                # addressed to whoever it names - not necessarily this seat.
                # A pass on the *opponent's* turn is exactly what can cross
                # into their own `declare_attackers` (CR 508.1): this seat's
                # priority round still runs on their turn, and the second
                # pass of that round is what triggers the turn-based action,
                # whichever seat happens to make it.
                self_decision = outcome.get("pending_decision")
            if self_decision is not None and self_decision.get("kind") in (
                "declare_attackers",
                "declare_blockers",
            ):
                if self_decision.get("player") == seat.player:
                    # This very pass is what crossed into CR 508.1/CR 509.1's
                    # turn-based action, addressed to this seat itself - not
                    # a decision this seat necessarily has to break out of
                    # the run to answer through `decide()`. Looping back lets
                    # the top of the loop see it fresh: `autopass_stop_reason`
                    # decides whether to stop for it (the same as if it had
                    # been there before this pass was even attempted), and
                    # the branch above answers it with "nobody" when it does
                    # not. Deliberately not counted in `passes` either way -
                    # the pass itself never committed (the paused command's
                    # mutated copy was discarded, same as any other pause),
                    # so nothing here actually released priority yet.
                    continue
                # Addressed to the *other* seat (the case the comment above
                # explains): this seat cannot answer it and spinning on more
                # passes would just cross the exact same wall every time.
                return handed_back(
                    seat.fetch_state(),
                    f"passing crossed {self_decision.get('player')}'s own "
                    f"{self_decision.get('kind')} (CR 508.1, CR 509.1), which only "
                    "they can answer",
                )
            if self_decision is not None:
                return answer(
                    seat.fetch_state(),
                    "passing priority raised a decision only you can answer: "
                    f"{self_decision.get('prompt')} - answer it with decide() now",
                )
            if _unknown_outcome(outcome):
                return answer(seat.fetch_state(), UNKNOWN_OUTCOME)
            if _refused(outcome):
                return answer(
                    seat.fetch_state(),
                    f"a pass was refused, so the run stopped: {outcome.get('reason')}",
                )
            passes += 1
            continue

        return handed_back(
            state,
            "you do not hold priority right now, so there is nothing here to pass",
        )
