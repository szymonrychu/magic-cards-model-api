# Project memory

Standing conventions and corrections, recorded as they came up - one dated
entry per decision or dead-end, per this workbench's convention. This repo
also has a gitignored `memory/` directory, but that is the continuous AI
seat's own runtime learning (mounted at `/workspace/memory` in the agent
container) - a different thing from this file, which is project memory for
whoever works on the repo itself.

- 2026-09-20 kicker reaching a human (the owner's "Sowing Mycospawn, cast with
  kicker didn't work. I simply wasn't presented with option to pay the
  kicker"): every server layer already had it - `ActionRow.optional_costs`,
  `CastRequest.optional_costs` with `times`, the seat CLI's `--optional-costs`
  and the MCP tool's `optional_costs`, all landed in battle 2 - and the one
  surface that never read any of them was `ui/`. `beginCast` asks
  `needsChoices`, which only knew about X, modes, targeting and `cast_options`,
  so a kicker card fired on the click that opened its own menu. The option is
  published on `affordances.cast_hints` (`card.cast.additional_costs`), NOT on
  the affordance endpoint or only on the legal-actions row: it is a printed
  fact needing no `Casting`, it rides the board poll the browser already has,
  and `needsChoices` runs before any affordance fetch - sourcing it from the
  row would mean the browser fetching legal actions before every cast. The
  row's own `optional_costs` stays the board-aware answer (affordability,
  entwine/escalate/harmonize) and is now reconciled against the engine's
  `Action.additional_cost_options` rather than being a second free-running
  enumeration: anything the engine prints that this api's keyword table does
  not know is appended with `understood: false` and the engine's rule, which
  is how a printed cost the engine cannot charge (March of Otherworldly
  Light's "you may exile any number of white cards from your hand") reaches a
  seat and the strip at all instead of reading like a card with no option.
  Counts, never booleans, end to end - `optionalCosts` is a keyword->times map
  on the in-flight action, and the repeatable ones get a number input keyed
  `optional-cost:<keyword>` so `castingStripEditInProgress` protects a
  half-typed count the way it already protects X.
- 2026-09-14 script-dsl O1/O2/O3/O5/O6/O8 (opening plays DSL groundwork, O4/O7
  deferred - they need engine feasibility/enters-tapped helpers that do not
  exist yet): `turn` reads `turn_number`, verified live against
  `TurnMachine.begin_first_turn`/`advance`/`grant_extra_turn` - it counts
  rounds of the table, so it is my Nth turn on the play and on the draw
  alike, and an extra turn never advances it. `land_face` (O5) is true for a
  plain land too, not just an MDFC's back face - deliberate, since a plain
  land trivially has a land face and the DSL gains nothing from excluding
  it. No live MDFC-land or landcycling card is cached in
  `tests/fixtures/scryfall` (checked): O3's runtime "hand carries the
  ability" proof uses Angelsong's plain Cycling instead (same
  `off_zone_abilities` code path as landcycling), O5's "play_land plays that
  face by card_id" proof is a hand-built-view unit test against
  `_move_request` directly, and O6's `choose_a_quality` proof uses a real
  Cavern of Souls. `CHOICE_DECISION_KINDS` is derived from the api's own
  `decisions.TOOL_FIELD_FOR_KIND` (kinds answered on `choice`), the same
  pattern `AMOUNT_DECISION_KINDS` already used, so the engine learning a new
  choice-shaped decision extends it for free. mtg-decks' O3 test list drops
  "Wastes" from the malformed-word parametrize (present in the API's small
  fabricated card cache but absent from mtg-decks' fixture, which copies the
  real `meta/card-data.json` where Wastes is a real card name, doubling the
  error code).
- 2026-09-14 script-dsl O4/O7 (`enters_any`, `unlocks`, `unlocks_by_search`,
  `affordable`, on top of the engine's `machines.hypothetical` land helpers):
  `selectors.matches` cannot answer any of the first three from a redacted
  view alone - they read what a land would do if it entered, which only a
  live `Game` can say, and `evaluate()`/`matches()` run in the MCP seat
  process, which holds no `Game` (`Seat.fetch_state` is HTTP-only; confirmed
  by tracing `mcp.py` -> `script_tools.run/listing` -> `runtime._evaluate`).
  So the view carries no `enters`/`unlocks` field of its own: a new module,
  `playscripts/hypothetical_facts.py`, runs one `payability_matrix` call
  server-side (never one `payability` call per land/card pair) and returns a
  plain `{card_id: {enters?, unlocks?, unlocks_by_search?}}` mapping, served
  by a new dedicated route (`GET .../playscripts/hypothetical-facts`, seat-
  uid-scoped only - never the admin key, since a fact is always "for this
  player") rather than a new `include=` value on the state route, because the
  fields only ever matter to a handful of plays and a shared `include`
  mechanism would tempt computing them on every poll. `selectors.matches`
  takes this mapping as `hypo` (a new kwarg, mirroring `present` for
  `missing_from`) and raises `HypotheticalRequired` (mirroring
  `ContentsRequired`) when a selector needs it and none was fetched at all -
  never a silent false, unlike a `hypo` that was fetched but has no entry for
  this card, which is the ordinary "absent characteristic" case. `affordable`
  needs no engine call at all (`mana_value <= untapped_mana + extra`,
  `conditions.untapped_mana`, already used for `{kind: untapped_mana}`), so
  it never raises for a missing context, just never matches - the one
  deliberate asymmetry with the other three. `unlocks_by_search` also extends
  `needs_contents` (it reads the viewer's own library to find what a fetch
  could search for), mirrored by a generic `_selector_fields`/`_uses_any`
  walk that (like `_missing_froms`) skips a play's `moves` and a move's
  `answers`, since those get their own gate at their own call sites.
  `runtime._HypotheticalCache` (one per `_Context`, so one per run and its
  chained `next` plays) fetches at most once per distinct board: `turns_taken`
  is not a field `GameStateView` carries (only `turn_number`, "my Nth turn",
  which is not the same counter), so there is no cheaper fingerprint than the
  fetched state itself - keyed on `(turn_number, step, sha256(state))` for a
  hit/miss legible in a debugger, though the hash alone is what actually
  guarantees correctness. `unlocks`/`enters_any` are computed and exposed only
  for the viewer's own hand (and library lands that flow into
  `unlocks_by_search`'s aggregation): a library card's redacted view has no
  stable id (`_contents_view` strips it and groups by signature), so there is
  no way to key a per-card fact onto one - a `count` over `zone: library` with
  `enters_any`/`unlocks` in its `select` is schema-legal but can never
  usefully match, since `hypo` is never populated by card id for a hidden
  zone. A lethal optional payment (a shockland at low life, CR 704.5a) reads
  `enters_any: [tapped]`, never `untapped_if_paid`, both in the fact itself
  and already inside `payability_matrix`'s own math (a lethal-if-paid land is
  judged on its declined, tapped board). Timing on a realistic board (7-card
  hand, ~19 candidate lands) is well under the ~0.3s the engine's own
  equivalent test measures.
- **2026-09-13 - bd1: the buddy panel always said "(no rationale was
  recorded)" because `ui/advice_model.js`'s `viewOf` read `detail.reasoning`,
  while `audit.record` writes `reasoning` as the row's own TOP-LEVEL key
  (a keyword argument, like every action route).** The UI test fixture put
  `reasoning` inside `detail`, so the suite pinned the wrong shape and stayed
  green; `review.py` and `review_model.js` already read top-level first,
  which is why the retrospective was fine. The fixture now mirrors
  `audit.record`, and `test_the_rationale_sent_through_the_real_route_is_
  what_the_panel_shows` drives `POST .../advice` -> `GET .../log` -> the
  panel. Lesson: a UI fixture for a ledger row is built from a real route
  read at least once, never hand-shaped from a comment.
- **2026-09-13 - bd1: the rationale was long because the prose asked for it
  ("three to five sentences, every time", production rows ~1,000 chars), and
  nothing bounded it.** Now one or two plain sentences, at most 240
  characters, in `prompt.advisor_turn_zero` and `docs/ai_buddy_brief.md`.
  Enforced three ways: `schemas.AdviceIn.reasoning` (`Rationale`: typed 422
  `reasoning_too_long` whose msg says shorten it, plus `maxLength: 240` in
  the contract), `seatkit._advise_answer` refuses first client-side before
  any `dry_run` probe (so MCP and the seat CLI share it), and the panel and
  retrospective clip to `RATIONALE_MAX_CHARS` for rows older than the cap.
  A seat's own per-action `reasoning` is not capped. Narrowing a request is
  a bump: `seatkit-19`.
- **2026-09-13 - bd1: the panel showed internal wording ("unchecked - the
  server could not dry-run...", "The board moved after this was proposed -
  see entry #N") because `render/advice.js` rendered agent/log-facing state
  verbatim to the human.** Replaced by one `.advice-status` line from
  `advice_model.adviceStatus`: stale "Buddy is thinking", busy "Buddy is
  waiting for your play to finish", unverified "Buddy is unsure about this
  play", ready "Buddy is waiting for your call". `validated`, `based_on_seq`
  and `ADVISE_UNVALIDATED_REASON` stay in the payload and agent answers.
- **2026-09-13 - play scripts are a closed vocabulary, gated by 17 shared
  codes.** `playscripts/schema.py` is the pydantic model (7 condition kinds:
  count, zone_size, life, stack_top, land_drop_available, untapped_mana,
  input; 4 move kinds; no iteration, attack, block, mulligan, concede or pass
  field), and `mcp-seat plays check` (`playscripts/lint.py`) mirrors
  mtg-decks' stdlib `scripts/check_plays.py` rule for rule: malformed-play,
  unknown-field, id-mismatch, bounds-exceeded, forbidden-kind,
  hidden-zone-side-any, card-name-in-field, phrase-too-narrow, unknown-next,
  next-cycle, chain-too-deep, too-many-plays, too-many-active-for-archetype,
  unknown-archetype-key, unknown-input, invalid-input-value, malformed-inputs,
  plus the non-blocking play-conflict/next-conflict warnings. An
  `ability_text_has`/`oracle_has` phrase must match at least 3 distinct cards'
  oracle text in the guides root's `meta/card-data.json` - the
  anti-overfitting rule; a card name is never a selector. The conflict
  algorithm is pinned by `tests/fixtures/conflict_corpus.json`, a
  byte-identical copy (sha256 pinned) of mtg-decks' fixture. Two readings:
  `known_cards` is keyed by the player who KNOWS, so "the opponent's known
  hand" is `known_cards[viewer].hand` filtered to cards the opponent owns (the
  requirement's `known_cards[opp]` never survives redaction); and the view
  gained engine-read `colors`/`mana_value` per card and
  `lands_played`/`land_allowance` per player, so `colors_any`, `mana_value`
  and `land_drop_available` never re-derive a rule from a mana cost or a
  guess. Optional pydantic fields are typed without `None` (`tapped: bool =
  None`) so an explicit `null` is refused like the stdlib gate refuses it; a
  `mode="before"` wildcard validator is not an option, pydantic forbids it on
  the `kind` discriminator.
- **2026-09-13 - the default model for an AI opponent seat and a buddy is
  `opus`.** The owner's call. Spelled as the codebase already spells
  `AgentSettings.model` (`opus`, never a versioned id), and set in the three
  places a default is decided: `AgentSettings.default` (the server, when a
  create request names no `agent`/`buddy_agent`), `lobby_model.js`'s
  `DEFAULT_MODEL` (a blank lobby model field), and compose's `CLAUDE_MODEL`
  fallback. `contract/agent-settings.schema.json` is unchanged on purpose: it
  has no default - `model` is required on the wire - so the default only ever
  travels as a value; `test_the_wire_shape_of_the_server_default_validates`
  pins that. `AGENT_DEFAULT_MODEL` still overrides it per deployment.
- **2026-09-12 - a 5xx is not a refusal, and the baseline must never
  remember one as if it were.** `api_client.act` mapped every status >= 400
  to `{"allowed": False}`, and the anti-stall memory then fingerprinted it:
  a keyless move (`pass_priority`, `advance`, `decide`, a declaration) has no
  next row for `_pick` to step to, so one 503 silenced the seat for the rest
  of the game and `play_out` conceded - a fabricated baseline loss, in the
  parallel gauntlet mode that produces the 503. A 5xx now carries
  `unknown_outcome: True` (the shape `seatkit._normalise_non_2xx` already
  used) and is retried, not remembered; a 4xx is still fingerprinted, because
  that memory is the fix for the unbounded repost loop. A stall caused by an
  unreachable server ends `ended_by: "transport"` with no concession.
  `_refusal` also keeps the body's own keys now - the 409 board-moved answer's
  `stale`/`last_seq` never reached this seam's only caller. `baseline_seat`
  is the only consumer of `api_client.act`; nothing depended on the
  conflation.

- **2026-09-12 - the control arm could not measure its own refusal split.**
  The `stale: False` stamp needs the request to name a current
  `expected_seq`. `seatkit.run_action` arms one; `baseline_seat` never did, so
  a baseline-only ledger read `stale_split: unknown` - the same structurally
  empty bucket recorded below, one level down, for exactly the arm every agent
  number is compared against. `LegalActionsResponse.last_seq` now carries the
  position with the enumeration (read *before* the board, so it can never be
  newer than what the seat saw), `SeatApiClient.board_seq` holds it, and
  `BaselineSeat` folds it in at the seam - never into `Move.body`, whose
  fingerprint is the anti-stall memory. `MulliganRequest` became `BoardRead`
  as the one route the policy posts that could not name a board.

- **2026-09-12 - `stale: False` is not a rules error on its own.** A refusal
  on a current board was scored as the agent misreading the rules even when
  the api had itself offered the action (the Adventure face, since fixed in
  the engine, was the measured case). `app._run_command` holds the request and
  `legal_actions_before` for the same board, so it sets
  `records.offered_action` (a ContextVar, the `stale` flag's own pattern) and
  `audit.record` stamps `offered` on refusal rows. `eval_report` now splits
  refusals five ways - `rules` (never offered: the only agent fault),
  `offered`, `stale`, `unknown`, `unchecked` - with an `offer_split` beside
  `stale_split`. Decision, and its cost: a seat that mis-announces an offered
  action (a bad target, X, mode) lands in `offered`, not `rules`, because the
  ledger cannot tell that from the api contradicting itself and
  `rules_error_rate` must only count what is certainly the seat's fault - read
  it as a floor. `crew`, `cycle`, the declarations and `decide` make no claim
  (no `ability_index`, or not a single enumerated row).
- **2026-09-12 - degradation must never make a match EASIER.** The
  non-obvious half of hardening the deck-guide readers, and the one that must
  not be reintroduced. `guides._clauses` dropped an unusable clause from an
  `archetype.json` and left the rest of the file standing, which looks like
  the forgiving thing to do and is not: `Candidate.clauses` counts the
  survivors, so a signature with one clause broken is a SHORTER signature,
  and a shorter signature is a cheaper one to satisfy in full. Measured on
  one list against one two-clause archetype - intact it answered 1 of 2 and
  got `low`; with the second clause malformed it answered 1 of 1 and got
  `confident`. The `exclude` side is worse: an `exclude` clause is the file
  saying "a list holding these is playing the other deck, not me", so
  dropping the unusable row deletes the disavowal and a correctly disowned
  list is handed that wrong guide confidently. Both are a confidently wrong
  answer, which is strictly worse for the agent than no answer at all,
  because it plays someone else's gameplan instead of its own list. So a
  malformed file costs the deck its WHOLE signature and the other twelve
  guides answer as before. Keeping an unusable row as an unsatisfiable clause
  was considered and rejected: safe for `signature`, useless for `exclude`
  (an exclude nobody can satisfy is an exclude that is not there), so it
  needs opposite treatments for the two halves of one file - and asymmetry
  between two readers of this corpus is exactly what let `signature_index`
  and `load_signature` drift apart in the first place.

- **2026-09-12 - the card's byte budget is per card, and it is never
  divided.** `render_current_game` used to hand each branch of a family
  `CARD_TARGET_BYTES // len(offered)` with a shared cap, on the theory that a
  briefing's cost is the sum of the cards in it. It bought those bytes out of
  the one thing the brief requires on every one of fifteen to nineteen rows:
  at a 5120 byte target and an 8192 byte cap there is no rung of the trim
  ladder left that keeps the tells table's `watch for` column, so the tron
  case - the case `_family` exists for, and a routine one when pilots bring
  their own lists - rendered `prison-tron` at 6923 bytes and `eldrazi-tron`
  at 7879 with the column deleted off all 34 rows between them, while each of
  those two plans renders whole at 11-12 KB under its own cap. Both halves of
  the trade were the wrong half: 14802 bytes of card, over the 12288 per-card
  cap the brief does set, and still missing the field. Rendered at its own
  budget each branch comes out whole - 11659 and 10026 bytes, 34 rows, no
  empty cell, neither truncated - for 21685 bytes of card in a 25 KB
  briefing, against the 58540 + 58973 bytes of gameplan guide it replaces. Two things
  to keep:

  (1) **A briefing-wide budget is not a budget the brief set.** The only
  numbers written down are per card (10 KB target, 12 KB hard cap), and the
  card's own trim ladder is the only authorised way to spend less. Inventing
  an outer budget hands the ladder a floor it cannot meet honestly and the
  cost comes out of whichever field the last rung happens to delete.

  (2) **A test at a squeezed budget is not a test of the default path.**
  Every family test asserted the renderer behaved honestly under the divided
  budget - and two of them asserted the loss as expected behaviour ("it
  dropped section 3 and said otherwise") - so a green suite hid a briefing
  with zero `| watch for |` in it. What was missing was an assertion about
  the briefing the driver actually produces:
  `test_a_family_briefing_keeps_the_watch_for_on_every_row_of_every_branch`
  is that, and the squeezed-budget tests are now named for what they are.

- **2026-09-12 - a cast can name its half, and the value is the engine's
  own string.** `CastRequest.spell_face` takes exactly what
  `Action.spell_face` said on the row being taken - post `GET
  .../legal-actions`'s `"Tear"` straight back - and `engine_api._drive_cast`
  hands it to `Casting.cast_as_split_half` first, ahead of every step that
  reads it (`base_cost`, `targeting`, `determine_cost`). That round trip is
  the non-obvious part and it is deliberate: the field is named after the
  engine's own field rather than given a parallel api vocabulary, because a
  seat that has to convert what it read before it can send it is a seat that
  will convert it wrong, and there is no second place to look the spelling up.
  Absent still means the front half - every caller written before this sends
  nothing.

  Before this the api was advertising an action it could not perform: the
  engine enumerated Wear // Tear as two `cast` rows and the api cast Wear for
  both of them. `baseline_seat._plain_cast`'s skip of `spell_face` rows was
  the workaround, and it stayed on for one more change as a stated policy
  choice, because `Action.target_slots` was still face-blind engine-side and
  the Adventure half was refused. **Superseded by the entry below.**

  CR 715.3's Adventure was **refused**, not announced, and the refusal was
  right when it was written: `Casting.cast_as_adventure` charged the right
  cost and stamped `face_played`, but `Casting.targeting` read the whole
  printing for every layout except a split card, so Stomp was announced with
  no target requirement at all - measured, a target came back "too many
  targets: 1 chosen where the spell takes at most 0" and a targetless Stomp
  resolved dealing no damage. **Superseded by the entry below.**

- **2026-09-12 - the Adventure refusal is gone, and it was a measured
  refusal rather than a missing feature.** `engine_api.adventure_face_refusal`
  existed because an Adventure announced against the whole printing's profile
  had zero target requirements: measured both ways, a targeted Stomp was
  refused "too many targets: 1 chosen where the spell takes at most 0" and a
  targetless one resolved dealing no damage, so a refusal a seat could read
  beat a spell that cost {1}{R} and did nothing. **Engine commit `fd28021`
  "Evaluate the chosen face, not the front one, on every casting path (CR
  601.3e)" closed it**: CR 715.3a/715.3b turned out to be CR 709.3a/709.3b
  word for word, CR 601.3e is the general rule both instance ("these
  alternative characteristics replace the object's characteristics for this
  determination"), and `Casting.targeting`, `Game._announcement_profile` and
  `Game._resolution_profile` all key on the chosen face now rather than on
  `Layout.SPLIT`.

  What that changed here, all in one branch: `_announce_the_face` routes the
  inset face to `Casting.cast_as_adventure` (the engine's own door, and the
  stamp CR 715.3d's exile-on-resolution reads); `affordances._face_profile`
  stops being split-only and reads any face the printing carries through
  `machines.casting.face_being_cast`, so an Adventure row's candidates are
  the Adventure's; and `baseline_seat._plain_cast` stops skipping face rows,
  which is what the old docstring said to do once the engine read the
  announced face into `target_slots`. Face rows are their own pool in
  `choose_move`, tried after permanents and faceless spells - the state view
  carries one `type_line` per printing, so `_is_permanent` cannot classify a
  named half and must not try. `PROTOCOL_VERSION` stays `seatkit-15`: no
  request or response shape moved, only the value space a parameter that
  version already added will accept (argued in full in `seatkit.py`'s own
  changelog entry).

- **2026-09-12 - two audit gaps: a public component vanished from
  `openapi.json`, and the baseline paid a round trip this branch existed to
  remove.**

  (1) **A subclass replaces a component's name, it does not extend it.**
  Retyping `LegalActionsResponse.actions` to `list[ActionWithTargets]` was
  meant to add candidates and did - but FastAPI names a component only where a
  route references one, and with the subclass as the only reference the
  engine's own `Action` stopped being emitted into `#/components/schemas`
  altogether. `tests/test_openapi_drift.py` passed straight through it,
  because its `REQUEST_MODEL_COMPONENTS`/`RESPONSE_MODEL_COMPONENTS`
  allowlists are hand-written and nobody had listed `Action`. Both halves are
  fixed: `actions: list[ActionWithTargets | Action]` names both (and is the
  truth about the answer - `candidates=false` rows are plain engine `Action`s,
  `candidates=true` adds a *required* `targets` block, which is what keeps the
  two branches distinguishable), and the drift guard now derives its baseline
  from `git show origin/main:openapi.json` instead of from a list, so any
  component that leaves the contract is red with its own name in the message.
  Hand-listing what must not break cannot cover what nobody thought to list;
  deriving it from the artifact can. CI's checkout needed `fetch-depth: 0`
  for that baseline ref to exist at all.

  (2) **The candidate list was never only on `/affordances`.** The baseline's
  per-cast second call is gone: `step` asks `legal-actions` with
  `candidates=True` and `_fill_targets` reads the `targets` block off the very
  row it is about to act on, so a targeted cast costs three calls (state,
  enumeration, act) rather than four - and a `_pick` loop that declines
  several casts in a row now costs zero extra calls rather than one apiece.
  `SeatApiClient` lost its `affordances` method entirely, so the shortcut
  cannot come back by accident, and
  `test_a_cast_names_its_targets_out_of_the_enumeration_it_already_read` pins
  the call list. Reading the candidates means parsing
  `affordances.candidate_line`'s string form (`baseline_seat.candidate_of`),
  split from the RIGHT: a card name can carry brackets of its own ("Erase (Not
  the Urza's Legacy One)"), while the appended `(<zone>, <controller>)` is
  always last.

- **2026-09-12 - the advisor panel took the bottom-right corner, and the log
  moved into an overlay of its own rather than into `#modal`.** Three things
  this cost, worth not rediscovering.

  (1) **`#log-modal` is a sibling of `#modal`, never a dialog inside it.**
  `panels.js`'s `closeModal` empties `#modal`'s content on every close, and
  `render/log.js`'s `mountLog` subscribes once at boot and keyed-patches one
  `#log-box` for the life of the page. Nested, the log's element would be
  destroyed the first time any other dialog closed and that subscription
  would patch a detached node for the rest of the game - a log that silently
  stops growing. So the overlay is built once in `index.html` and only
  toggled (`toggleLogModal`/`closeLogModal`/`logModalIsOpen`), and `mountLog`
  itself is untouched.

  (2) **A hidden box has no scroll geometry, so opening the log needed its
  own scroll pin.** `mountLog`'s auto-scroll reads `scrollHeight`, which is 0
  while the overlay is `hidden`, so every poll tick pinned `scrollTop` to 0
  and the log opened on entry #1 of 500. `toggleLogModal` reads
  `scrollHeight` immediately after clearing `hidden` - which is what forces
  the browser's synchronous reflow - and pins the bottom there, because that
  is the only place where the laid-out height exists.

  (3) **Advice freshness is judged over-conservatively on purpose.**
  `advice_model.js`'s `isFresh` stales a proposal on ANY ledger row after the
  board the agent read, including rows that move nothing (`plan_declared`,
  `report_problem`). Deciding whether the board "really" changed means
  re-implementing the engine in the browser; a false "stale" costs one
  re-think, a false "fresh" means approving a play against a board that has
  moved. A proposal with no `based_on_seq` is never fresh. Approve also
  records `/advice/{seq}/approve` BEFORE performing the play, and performs
  nothing if that record fails: an unrecorded approval is, in the ledger the
  retrospective reads, indistinguishable from a play the human thought of
  themselves.
- **2026-09-12 - an advice `summary`'s cost comes from the action the engine
  enumerated, never from the card's printing - and only a cast may fall back
  to the printing at all.** The cost half of `records.advice_summary`.

  A proposal's body is machine-shaped, so the line a human approves has to
  spell out the price. Reading `card.card.mana_cost` off the named card is the
  obvious way and the wrong one: Mind Stone prints `{2}` and its second
  ability costs `{1}, {T}, Sacrifice this artifact`, Lupine Harbingers is
  simultaneously a `{3}{G}` cast and a `{2}` foretell (CR 702.143a), and
  CR 601.2f's increases and reductions never reach a printing at all. So
  `_proposed_action` matches the body against the `legal_actions_before` row
  the route has already computed for the same ledger entry - free, and it is
  the engine's own determined total - keyed on the path's own
  `models.action.ActionKind` plus `ability_index`/`keyword`/`from_zone`/
  `alternative_cost` agreement.

  Two things that surprised: `legal_actions` does **not** enumerate crew at
  all (CR 702.122a's ability has no `{T}` of its own), and cycling comes back
  as `activate_off_zone` with `keyword: "Cycling"` even though the api gives
  it an `actions/cycle` route - hence `records._PATH_KINDS`. Crew therefore
  gets no cost clause rather than a mapping that matches nothing, because the
  printed-cost fallback would then have quoted the Vehicle's `{2}` for an
  ability that never pays it. That fallback is restricted to `actions/cast`
  (`_PRINTED_COST_PATHS`) for exactly that reason - CR 601.2f, "usually this
  is just the mana cost", is true of a cast and of nothing else - and says
  `(printed)` out loud when it fires, because a proposal may legitimately name
  a play that is not legal yet.

- **2026-09-12 - advise mode: the dangerous half is the 5 request shapes with
  no `dry_run`, not the 7 with one.** B2's whole hazard, written down because
  it is not visible from either side on its own.

  `game_access._advisor_may` (wave 1) authorises an advisor's POST when the
  *raw body* says `dry_run: true`. That reads as a general "an advisor may ask
  questions" rule, and it is not: it is exactly as wide as the request shapes
  that actually have the field. Seven of the twelve action shapes do
  (`Activate`/`Attack`/`Cast`/`Crew`/`Cycle`/`PlayLand`/`SpecialAction`); the
  other five (`Decide`/`DeclareBlockers`/`PassPriority`/`Concede`/`Advance`)
  do not, so a body claiming `dry_run: true` on one of them passes the gate
  and then reaches a route that commits for real. An advise `Seat` therefore
  sends **no mutating request at all** for those five and builds the proposal
  locally, marked `validated: false` - never "send it with `dry_run` and hope".
  The split is a frozen constant (`seatkit.DRY_RUN_SHAPES`) pinned against
  `openapi.json` by `tests/test_advise_mode.py`, because a shape quietly
  gaining or losing the field silently changes which calls get *sent*.

  Two smaller decisions from the same task. The `advice` row's `summary` is
  derived server-side (`records.advice_summary`) rather than taken from the
  agent: an agent-written summary is a second description of the same action
  that nothing keeps honest, and a human who approves after reading only the
  summary would approve something else. And `Seats.seat_for` now resolves
  *every* uid through `GET /api/seats/{uid}`, the pinned `--game` branch
  included, because that endpoint is the only place a uid's `role` is
  answered - the pinned branch's old `GET /api/games/{id}/seat` 404s an
  advisor uid outright, so a buddy pinned to its game was simply nobody. It
  keeps its isolation by checking the game the lookup reports against the pin
  rather than by asking a narrower question.
- **2026-09-12 - a decklist is identified by its archetype signature, never
  by resemblance to a sample 75.** `GuideLibrary.identify` reads each guide's
  `decks/<slug>/archetype.json` from the mtg-decks corpus and asks every
  archetype the same question about the same MAINBOARD: does this list hold
  `min` copies across the cards this clause names. `_similarity` and
  `MATCH_THRESHOLD` are gone and must not come back as a fallback - two code
  paths identifying the same 60 cards differently, depending on whether the
  list was bundled or pasted, was the bug. Three things worth knowing before
  touching it:

  (1) **`archetype` in those files is not the directory slug.** `prison-tron`
  calls itself "urzatron", `orzhov-martyr` calls itself "orzhov blink",
  `affinity` calls itself "pinnacle affinity". The slug names the guide
  directory, the `archetype` field names the deck; conflating them reports
  the wrong deck to the agent.

  (2) **The tron family needed a rule the outcome table did not spell out.**
  `prison-tron` and `eldrazi-tron` share two clauses verbatim and declare
  `parent: tron`. A list with nine Tron lands and Temple/Labyrinth mana
  answers both shared clauses and neither branch's own, so both sit at 2/3 -
  and the specificity tie-breaks (clause count, then exclude count) DO
  separate them, because prison-tron carries one exclude clause and
  eldrazi-tron none. Letting that tie-break decide hands the pilot the wrong
  branch's gameplan, which `eldrazi-tron/archetype.json` explicitly argues
  against. So the family check runs BEFORE the tie-breaks: leaders by
  strength alone that share a parent and satisfied the same clauses as each
  other resolve to the parent, `low`, with no single guide and both branches'
  guides offered.

  (3) **The corpus's own answer on two lists that look like misses.** The
  bundled `jeskai_aggro` and `dimir_control` classify confidently as
  boros-aggro and dimir-frog: they run the whole boros one-drop suite and 4
  Psychic Frog + 3 Quantum Riddler respectively, and those signatures' own
  `why` text says that is what the archetype IS. `boros_energy` matches the
  single commissioned boros guide on all three clauses. These are the corpus
  being consistent, not drift - `tests/driver/test_guides.py`'s table records
  each with its reasoning, pinned to mtg-decks sha
  `936b324bedcad1c4de6e328c0f32c33a03bfa703`.

- **2026-09-12 - the agent's deck brief is rendered into turn 0, not read
  from a 58 KB guide.** `GAMEPLAN_GUIDE.md` is 42-73 KB of markdown across the
  corpus (72.8 KB for orzhov-martyr) and it is *generated*: the structured
  source is `analysis/<run>/domain3-matchup-threat/boarding-plan.json`, which
  was sitting unread beside the file the seat was told to open.
  `driver/matchup_card.py` renders that plan into ~10 KB of turn-0 context
  instead.

  Three things this cost time to get right:

  (1) **An archetype's signature cards are keyed on the name the archetype
  file calls itself, not on its directory.** `decks/affinity/archetype.json`
  says `"archetype": "pinnacle affinity"`, and `pinnacle affinity` is what
  every boarding plan in the corpus writes; keying the lookup on the directory
  slug alone found 6 of 19 rows instead of 9. `signature_index` keys on both.

  (2) **A boarding plan has no deck-wide keep rule.** Every `keep` in it is
  written for a named matchup, so the card presents the *modal* keep across
  the plan's own matchups, says how many matchups back it, and does not pass
  one matchup's keep off as universal.

  (3) **The specified trim order - `watch_for` first, top-three detail second
  - has no power on some plans.** dimir-frog's longest `watch_for` is 123
  characters, so squeezing the table's cap to 85 saves less than the
  truncation notice costs, and the detail levels do all the work there. The
  ladder still tries the table first (that is the order the requirement asks
  for); the test asserts both branches rather than pretending the first lever
  always bites. The corpus renders 10.0-11.5 KB at full fidelity, so 11 of 13
  guides trim by a rung or two against the 10 KB target and every one of them
  lands under the 12 KB hard cap.

  Also: the seat's own `vs-<archetype>.md` standing notes are now inlined into
  the briefing (4 KB budget, alphabetical, the deduced matchup's note first
  when a caller knows it); a past game's write-up stays a path, because there
  can be dozens. Every note key is the tells table's canonical slug or
  `unknown`, and `reflection_turn` says so - a note filed under an invented
  name is one the next briefing never looks up.

- **2026-09-12 - a byte cap is not a trim lever, and the card's notice is
  derived from the rung rather than written next to it.** Halving the hard
  cap for a family briefing (entry above, (3)) handed every real corpus plan
  a budget no honest rendering can meet: the trim ladder bottomed out around
  9.3 KB, the 6 KB cap took the rest with a blunt byte cut at the end of the
  text, and each card came out missing two to six tells rows, the brew case,
  the one-small-read pointer and the whole of section 3 - while its own
  notice went on saying "Nothing was dropped - every archetype still has a
  row". Three things came out of fixing it:

  (1) **The ladder got the two levers it was missing**, both announced: the
  tells table's `watch for` COLUMN goes after its prose has given all it is
  going to (the prose rungs are nearly free on this corpus - most `watch_for`
  values are already under 85 characters), and section 3 then keeps its
  heading and the three names while its detail goes to the matchup files.
  That reaches ~6.2-6.9 KB with every row, every section and every pointer
  intact - which is as far down as the ladder goes honestly, and the reason
  no caller is allowed to ask it for less than a whole card.

  (2) **Rows are the last thing to give, and they are named where they
  were.** Below every lever the renderer drops tells rows from the bottom of
  the field up and lists the archetypes that lost one, so their
  `vs-<name>.md` keys and matchup files stay reachable. Only a budget that
  cannot hold even an empty table gets the byte cut, and the notice then says
  the card was cut and what follows the marker is missing.

  (3) **`_notice` is computed from the `_Trim` rung that rendered the card**,
  not written alongside the loop that picked it, and it separates SHORTENED
  from DROPPED. A notice written by hand is a second source of truth about
  what the card did to itself, and it was wrong at every rung below the two
  it knew about.

  (`CARD_FLOOR_BYTES`, the floor a shared cap did not go below, is gone with
  the sharing itself - see the 2026-09-12 entry at the top of this file.)

- **2026-09-12 - the matchup card reads the 75, the classifier reads the 60,
  and both halves of that are in one briefing.** The card and the classifier
  were built on separate branches and integrating them turned up one real
  bug and two near-misses worth recording:

  (1) **The swap intersection must be asked of the WHOLE 75, not of the
  mainboard.** `parse_decklist` returns a split `Decklist` because a
  signature clause may only be answered from the maindeck - nobody has cast
  a sideboard card. A boarding swap is the opposite question: every card a
  plan boards IN comes out of the sideboard by definition, so intersecting
  on `.mainboard` stamps `(NOT IN YOUR LIST)` on every single board-in.
  dimir-frog is the case verbatim - `esper blink` is its biggest matchup and
  boards three Mystical Dispute, which its list ships in the sideboard
  behind a bare blank line. `guides.held_faces` takes the union of both
  boards and is the only place that decision lives.

  (2) **The boarding plan and the matchup files are read off the SAME
  analysis run, selected by the run's date suffix.** A separate
  `sorted(glob("analysis/*/.../boarding-plan.json"))` sorts by deck name and
  picks the newest run only by accident - two runs with different name
  prefixes is all it takes for 2025's plan to beat 2026's. `_analysis_key`
  picks the run; `matchups_dir` and `boarding_plan` both come off it.

  (3) **A family match hands over two guides, so it renders two cards.**
  `Identification.guides` returns both leading branches when the list
  resolved to a parent. Dividing one card's budget between them looked like
  the thrifty answer and was not - superseded, see the 2026-09-12 entry at
  the top of this file.

- **2026-09-10 - an action answer now carries its own legal targets, and the
  candidates are opt-in because the engine will not cache the board.** M1's
  three findings, in the order they cost time.

  (1) **Candidate enumeration is ~106 ms on a 28-permanent board**, on top of
  the ~350 ms `machines.legal_actions` already spends there. The cause is not
  this repo's: `TargetRequirement.rejects` reaches `Game.characteristics_of`,
  which calls `Game.computed_board`, which is deliberately uncached in the
  engine - CR 613.5 says continuous effects are applied "continually", so a
  cached board would be a board that lies, and its docstring says as much. So
  `GET .../legal-actions` grew `candidates=true` rather than folding them into
  every answer: `seatkit.available_actions` sets it and pays once per action
  to save a whole round trip, and `ui/app.js`, which polls the same endpoint
  every 1.2s with the board already on screen, does not set it and pays
  nothing. `tests/test_legal_actions_endpoint.py` pins both the added cost and
  the single board walk per request.

  (2) **`Action.target_slots` is a cast-only field.**
  `legal_actions._cast_action` fills it; `_battlefield_ability_actions` does
  not, so Karn, the Great Creator's `+1` ("up to one target noncreature
  artifact") is enumerated with `target_slots: []`, exactly like his `-2`,
  which targets nothing. Candidates therefore ride on casts only. This repo
  deliberately does NOT re-derive an ability's targeting from printed text to
  fill the gap - `/api/games/{id}/affordances/{card_id}` already answers that
  per `ability_index`, and the real fix belongs in the engine's own `Action`.

  (3) **Strings, not objects, and the reason is `json.dumps(indent=1)`**,
  which puts every key of every dict on a line of its own. The same fourteen
  candidates measured 2,384 bytes as `{id, kind, name, zone, controller}`
  dicts and 982 as `"<uuid> <name> (<zone>, <controller>)"` strings, carrying
  the identical five facts. The same reasoning shapes `board
  .battlefield_digest`: ~62 bytes a permanent against the ~1,300 of a card
  view. Both caps fail loud rather than short - a truncated slot keeps its
  true `count`, and a digest that will not fit is replaced whole by a pointer
  at `battlefield_state`, never emitted partially, because a seat that misses
  a blocker because the digest ran out of room is the failure this exists to
  avoid.

- **2026-09-10 - the guides corpus revision is stamped into every game, and
  `GUIDES_REF` is a knob, not a pin.** The deck guides live in a third repo
  (`github.com/szymonrychu/mtg-decks`) and used to reach a seat Job through a
  bare `git clone --depth 1` of `main` with no record of what it got, so two
  seats scheduled minutes apart could read different guides and no artifact
  said so. The `guides` initContainer now fetches `GUIDES_REF` (default
  `main`) and writes `git rev-parse HEAD` to `/guides/.guides-revision`; the
  driver reads that back and puts the revision in the agent's briefing and in
  the header of the per-game plan file.

  Three things worth remembering. (1) The default stays `main` on purpose -
  play wants current guides; only an evaluation harness pins a ref. (2)
  Compose has no stamp file (the guides are a bind mount, not a clone), so
  `mtg_agent/driver/revision.py` falls back to reading the checkout's own
  `.git` - by hand, not by shelling out, because the driver image has no git
  binary and the bind-mounted checkout belongs to another uid, where `git
  rev-parse` refuses on `safe.directory` before printing anything. (3) The
  driver seeds the plan file with its header rather than asking the agent to
  write the revision, because a fact the agent is merely told to copy is one
  it can get wrong - and a game that ends before the agent writes a plan still
  leaves an artifact naming the corpus. `tests/driver/test_guides_revision.py`,
  `tests/driver/test_revision_stamping.py`.

- **2026-09-10 - a stale action and a rules refusal are two different
  things, and only a boolean can tell them apart.** A seat that read the
  board, thought, and then acted on a board the opponent had already moved
  got back `claude does not have priority, you does` or `no pending decision
  for you` - the symptom of a lost race, phrased as if the seat had misread
  the rules. The ledger could not separate the two, so "how much of an
  agent's refusal rate is play quality and how much is latency" was
  unanswerable. Every mutating route now takes an optional `expected_seq`
  (`schemas.BoardRead`) naming the ledger position the seat decided against;
  `records.require_current_board` compares it to `audit.last_seq` and
  refuses with `records.BoardMoved` **before the engine runs**, writing one
  row with `stale: true`, `moved_from` and `moved_to`. Four decisions worth
  keeping:

  (1) **Optional, and it stays optional.** `None` is "I name no board" and
  is exactly the behaviour every existing caller - the browser above all -
  already had. The regression guard is a test of its own, not a comment.

  (2) **The boolean is the discriminator, never the message.** The refusal
  text is for a person; nothing machine-readable keys on it. Same house rule
  as the browser fix of 2026-09-08 ("a refused pass is two different
  things"): ask the board, never pattern-match refusal text.

  (3) **The handler re-uses `game_log` rather than reading the ledger a
  second way.** The rows attached to the refusal as `since` are `GET
  .../log?since=` for the same `uid` this request already carried on its
  query string - so the redaction argument is that route's argument,
  unchanged, and a caller can never be handed more on a refusal than it
  could have fetched itself. Bounded at `moved_to`, which drops the
  refusal's own row: the caller is holding that already.

  (4) **The seat remembers its own watermark; no tool signature grew.**
  `seatkit.Seat.last_seq` is set by every `recent_log` (the moment a seat is
  *handed* a position), and `run_action` arms it for exactly one mutating
  call (`send_expected_seq_once`). One-shot on purpose: the action itself
  moves the ledger, so `run_action`'s auto-pass would be stale by
  construction if the arming were sticky. On a stale refusal the seat
  re-reads and re-sends **once** - never a loop, because a seat that loses
  the race twice has a genuinely out-of-date decision and hiding that from
  the corpus defeats the point. An MCP parameter for this was considered and
  rejected: an agent reasoning about a sequence number spends a decision on
  bookkeeping and gets it wrong. `tests/test_expected_seq.py`.

  (5) **`expected_seq` never reaches `Command.payload`.** It says which
  board the caller decided against and has done its whole job before the
  engine is asked anything, so `commands.Command.build` drops it. Leaving it
  in made it part of `Command`'s identity, and `app._pause_slot_guard` uses
  exactly that equality to tell a retry of the standing pause from a
  competitor: the armed call that stored the pause and the unarmed re-post
  that retries it (seatkit's autopass re-posts on every `paused: true` it
  reads) disagreed about the ledger position, so the retry came back a 409
  the caller could do nothing with. The admin-only `POST .../pregame/settle`
  is on the guard too, after its admin gate rather than before it: it is a
  command like any other, its `use_defaults=True` branch settles a whole
  pregame without going through `_run_command`, and the refusal writes a
  ledger row a caller without the key has no business writing.

- **2026-09-10 - an advisor credential is its own map, not a flag on
  `seat_uids`.** The advisor uid (a credential that reads one human seat's game
  and may not act on it) lives in a new `games.advisors` JSONB column, uid ->
  `{"player": str, "agent": <AgentSettings wire dict or null>}`
  (`repository.Advisor`, revision `b8d24f6c1a07`), and resolves through
  `store.player_for_advisor_uid` - never through `store.player_for_uid`.

  Reusing `seat_uids` is the obvious-looking move and it is wrong twice over.
  `player_for_uid` is read by every existing caller as "this uid may act as
  that player" (`declare_plan`, `whose_seat`, `game_access`'s own seat branch),
  so an advisor uid in that map is a credential that acts, silently, everywhere
  - the exact thing the whole feature exists to prevent. And the entry needs a
  second field (which agent runs behind it), which a `uid -> username` map has
  nowhere to put.

  The trap that forced the shape of the entry itself: the settings could NOT go
  in `agent_settings`, which is keyed by **username**. An advisor watching a
  seat that is already an AI seat collides with that seat's own row, and two
  advisors on one seat collide with each other - a JSONB object has one value
  per key and no ordering to disambiguate them, so the collision is silent and
  last-write-wins. Keying by uid and nesting `agent` inside the entry is what
  keeps "which settings belong to which credential" answerable at all.

  The read-only-ness is enforced in exactly one place, `game_access.
  can_access_game`, and **before** the ownership question rather than after it:
  an ownerless game (which is every game today) returns `True` from that check
  immediately, so an advisor asked about one would be waved through before its
  own rule was consulted. Its verdict is the only one in that module that
  depends on what the request is doing - any GET, a POST to `/advice` or
  `/report`, or a POST carrying `dry_run: true`; everything else is the same
  404 every other denial gives. `visibility.resolve_viewer` resolves the uid to
  the *human's username*, so `redact_view`/`redact_log_entry`/
  `redact_pending_decision` needed no change at all and the advisor's view is
  byte-identical to its human's by construction. `tests/
  test_game_access_advisor.py`.

- **2026-09-10 - the measuring stick: a greedy baseline seat and a
  records-derived scorecard.** Three findings worth keeping, all of them found
  by running the thing rather than by reading it.

  (1) **`legal_actions` cannot offer the combat pairs at the moment the api
  accepts them.** `machines.legal_actions._attacker_actions`/`_blocker_actions`
  both gate on `game.turn.step is Step.DECLARE_ATTACKERS`/`DECLARE_BLOCKERS`,
  but while the corresponding decision *stands* the committed board is still a
  step behind (the `advance` that asked the question paused before committing),
  so the enumeration is nothing but `answer_decision: declare_nobody`. The
  baseline therefore reads its own candidate pairs off the board - exactly what
  `ui/combat.js` already does, with its own "nothing here enforces a rule"
  note - and narrows a refused declaration one pair at a time until the engine
  accepts, bottoming out at the always-legal empty answer. Still true at engine
  `4ad120c`, re-measured 2026-09-12 - and what would close it is now written
  down in `baseline_seat.py`'s own module docstring rather than left as "out
  of scope": `decision_answers` already routes both kinds to
  `_attacker_actions`/`_blocker_actions`, so the only thing in the way is
  those two functions' `game.turn.step` predicate. Either it learns to accept
  the pending decision's own kind as an alternative to the committed step, or
  `Game.advance` commits the step before it pauses to ask. Both are engine
  changes; neither is expressible from this repo.

  (2) **A legal cast is not a targetable one, and the difference cost 547 rows
  in one game.** `legal_actions` lists a cast whether or not the board offers a
  legal target - CR 118.6 is about cost, and nothing there consults the board
  for CR 601.2c - so Galvanic Discharge against an empty board is *legal*,
  refused at announcement, and offered again on the next priority. The refused
  set did not save it: the opponent's own move changes the board every round,
  which correctly clears the memory of the refusal. The seat now names its
  targets *before* acting, and declines in-process when it cannot fill every
  requirement. Refusal rate fell from 87% to 0 on the same matchup.

  This entry used to call `GET .../affordances/{card_id}` "the only place the
  candidate list lives", and that was never true: the first commit of this
  same branch put the list on `GET .../legal-actions?candidates=true`
  (`app._attach_target_candidates`), which is where the seat reads it from as
  of 2026-09-12 - see that day's entry. The affordances endpoint is the
  *browser's* door onto the same enumeration, and the one that can re-ask a
  half-announced spell with the modes and the X the player has picked; it is
  not the only place the list lives, and a seat using it was paying a round
  trip for an answer it already held.

  (3) **`GET /api/games` is not "the games I booted".** With `DATABASE_URL`
  set it lists every live game in the database, including ones an earlier
  gauntlet left behind - so a run that trusted it adopted two strangers, drove
  them with its own baselines, and printed a four-game scorecard for a two-game
  run. `scripts/eval_gauntlet.py` reads `serve`'s own printed summary instead,
  which names exactly what that process created.

  Also: `PATCH /api/games/{id}/cards/{card_id}` accepts a `tapped` field and
  silently ignores it (`patch_card` applies `face_up`/`damage`/
  `summoning_sick`/`counters` and never `tapped`); `POST .../cards/{id}/tap` is
  the route that works. Noted, not fixed here.

- **2026-09-10 - the log route reads what it returns, and a dry run
  enumerates nothing.** Two server-side costs that both grew with the length of
  a game.

  (1) `GET /api/games/{id}/log` loaded the whole ledger (`audit.entries` ->
  `store.records`, no predicate, no limit) and then dropped everything at or
  below `since` in Python. Every row carries a full `snapshot(game)` plus a
  `legal_actions_before` enumeration - 11,538 bytes on the 28-permanent board
  `tests/test_action_answer_size.py` measures - so a poll cost the entire game's
  history to hand back the one row that was new. It now reads
  `records_since(game_id, since)` (postgres: a `seq > :since` range scan on the
  existing `uq_records_game_id_seq`; in-memory: `bisect` then slice, so `mise
  run test` exercises the bounded path rather than only `test:pg` doing so).
  The trap worth remembering: `last_seq` must NOT be derived from the rows that
  read returns. A caught-up poller matches none of them, and answering 0 sends
  it back to the start of the game on its next tick while answering its own
  `since` back freezes the watermark - neither fails loudly, `ui/app.js` just
  quietly re-fetches or quietly stops. It comes from `max_record_seq` (index
  only, no payloads), and is raised to the rows' own maximum when a row lands
  between the two reads, so a client can never resume past an entry it was not
  given. `tests/test_log_bounded_read.py`.

  (2) A dry run no longer computes `legal_actions_before`. It was computed
  inside the per-game lock for every mutating POST, dry runs included, and then
  dropped: a dry run writes no `records` row at all (`_run_command` skips
  `store.replace`, `audit_outcome` returns before `audit.record`), and the field
  appears in no response body - it is hidden information only
  `visibility.redact_log_entry` ever hands out, off a row that exists. Checked
  before removing it, because this ledger feeds review and agent training:
  `ui/review_model.js` reads `review.entries`' action/detail/reasoning/snapshot/
  seq and never mentions the field or dry runs, and no dry-run row has ever
  existed for any consumer to depend on. The UI fires one dry run per candidate
  before every real cast, so this was the per-keystroke cost of the play
  surface, serialised behind the lock every other seat waits on.
  `tests/test_dry_run_cost.py`.
- **2026-09-10 - every action click opens the anchored picker, and the
  one-option popover IS the confirm.** A card click that had exactly one
  thing to do used to fire it - `playLand` on a plain land, `beginCast` on a
  plain spell, `beginActivate` on a permanent with one ability - so a misclick
  in the hand row was a land drop. All three now open the same popover a
  multi-ability permanent already opened, with one item in it. No separate
  confirm component and no Cancel item: `showPopover` already leaves on
  Escape, on a click away, and on a pick, and adding a Cancel would be a
  second way to say the same thing. The planners are unchanged - `{kind:
  "land"}` still means "a plain land play" - because what changed is what
  the click *does*, not what it means; `menus.js`'s `handMenuItems`/
  `abilityMenuItems` build the single item (the one ability of an `activate`
  plan is read off the card, since the plan names no list of its own).

  The line the change deliberately does NOT cross: a click that SELECTS is
  untouched and opens nothing - picking a target, pinning a mana source
  mid-cast (a basic land still pins on one click with no menu at all, the
  standing commitment), and toggling an attacker or a blocker into the combat
  draft. A confirm on a target pick would make a three-target spell a
  nine-click affair. A card with nothing to do stays a silent no-op rather
  than opening a dialog to explain itself. Both halves are pinned by tests
  that drive the real handlers through the real `showPopover`
  (`tests/test_ui_app.py`'s T5 section), not by the planners alone.

  Enrichment is a second, parallel step and never a precondition: the menu is
  built from local state and is on screen before `GET .../legal-actions`
  (uid-guarded to this seat, ~1.1KB, ~2ms) is even answered. When the answer
  lands, `menus.js`'s `enrichItems` matches each row to an item by `card_id` +
  `kind` + `ability_index` and marks what the engine says it cannot pay for
  with `disabled` plus the engine's own `unaffordable_reason` as the
  sublabel - fields `showPopover` already supported and no caller had ever
  set. Three details worth keeping: `"unknown"` affordability is not `"no"`
  (a modal or X spell before its parameters are announced) and disables
  nothing; an item matching several rows (evoke: one `cast` kind, printed
  cost and alternative cost) is unaffordable only when EVERY row says so; and
  a menu item with no matching row at all (Crew is not an `ActionKind`) is
  left exactly as it opened. A failed read, or an answer that lands after the
  player left the dialog, is a silent no-op - `ribbon.js`'s new
  `popoverIsOpenFor(anchor)` is what makes the second case answerable, and
  the anchor is resolved once on the click because the redraw happens long
  after that dispatch nulled `event.currentTarget`. On the click only: the
  1.2s poll's ban on per-card affordance work stands.

  A dynamic play permission (cascade's hit, suspend's own third ability) is
  an action click too, and was missed on the first pass: `onPermission` began
  the cast on the click. It is not a zones-modal-only path - `render/
  board.js` wires it to `#permitted-row` and to the zone ribbon on the board
  itself - so it now opens the same one-option `Cast` confirm, captioned with
  the source that granted it, keyed `{card_id, kind: "cast", ability_index:
  null}` so the same enrichment finds it. Its `from_zone` still reaches the
  request unchanged.

  That is also what forced `actionAnchor`: the zones popover is a `.modal`
  drawn ABOVE `.popover`, so a confirm opened over one of the modal's own
  cards would sit under the scrim, dimmed and unclickable, with Escape
  closing the modal instead of the menu. The menu's geometry and stacking are
  frozen (see the anchored-menu note below), so the modal is closed instead
  and the confirm anchored over the board's own node for that card - the zone
  ribbon draws every graveyard/exile card and `#permitted-row` every
  permission, both keyed by card id, and the clicked node is the fallback if
  neither is there. Uniform for every action menu, not a permission special
  case: an unearth ability clicked in the modal had the same problem.

- **2026-09-10 - "any color" never includes `{C}`, so the tie this branch was
  justified by does not exist, and a chosen colour is a choice rather than a
  count.** Three corrections and one thing this repo cannot fix, all in the
  restricted-mana wire.

  (1) The branch was argued for with "Cavern of Souls and Delighted Halfling
  each pair an unrestricted `{T}: Add {C}.` with a restricted 'add one mana of
  any color' whose production set includes `{C}`, so production-matching
  inference resolves the tie toward unrestricted and gets those two cards
  wrong". False. CR 106.1a: there are five colors of mana, white, blue, black,
  red and green; CR 106.1b: there are six *types*, those five and colorless -
  both looked up in the rules text this session. Colorless is a type of mana
  that is not a color, so "any color" cannot make `{C}`, `{C}` names the plain
  ability unambiguously, and inference was right about both cards all along.
  The real case is **Sunken Citadel**, whose abilities both read "the chosen
  color": `restriction_for_production` asks `ManaAbility.effective()` with no
  board, "the chosen color" narrows to nothing without one, neither ability
  matches, and mana that may only be spent on abilities of land sources comes
  back carrying no restriction at all. The wrong justification was written
  into this file, `ManaSource`'s docstring and the test module docstring; all
  three now name Sunken Citadel, and the test that claims the divergence takes
  both answers off one board (named index refused, the identical unnamed tap
  allowed) instead of comparing two named indexes, which proved nothing about
  inference.

  (2) A colour on the wire is CR 605.1a's `color` argument - a *choice* - and
  the ability decides how much mana that choice buys. Shang-Chi's "{T}: Add
  two mana of any one color." was untappable from every client because
  `{card_id, colors: ["R"]}` was handed to `tap_for_mana` as a one-mana
  production the ability cannot make. `engine_api._expanded_to_the_abilitys_
  own_count` fills the count in server-side, from the ability itself, for the
  no-index shape as well as the named one. Deliberately not client-side: the
  count is nowhere on `AbilityView`, so the browser would be guessing, and the
  MCP and CLI seats would each have to guess the same way.

  (3) Two *different* colours are a mixture, and `ManaAbility.resolve` has a
  limb for it ("add two mana in any combination of colors"). The api used to
  collapse them to "no choice at all" and hand back "pass the colour chosen"
  to a player who had chosen twice; the sequence goes through whole now.
  **It still cannot be paid with**, and that half is engine-side:
  `CostPayment.tap_for_mana` re-validates a hand-picked production against
  `effective()`'s options, and those enumerate one colour repeated
  `any_color_count` times per colour, never a mixture - so Interdimensional
  Web Watch resolves to `{W}{U}` and is then refused "cannot be tapped to add
  {W}{U}". Pinned by a `strict=True` xfail in
  `tests/test_named_mana_ability_payment.py` so it flips the moment the engine
  enumerates mixed combinations.

  (4) The `seatkit-13` note oversold its reach. `mcp.py` and `seat_cli.py`
  build `mana_sources` from `sources_or_all`, which returns bare card ids
  parsed out of a comma-separated string - no seat can name an ability, which
  is why `contract/tools.json` is byte-identical across the bump. The note
  says so now: a response-shape change, browser-only on the request side.

- **2026-09-09 - the artifacts were generated against a partial engine, and
  "integrated" is a claim that has to be checked against the branch heads.**
  `magic-cards-model-integration` held engine `main` plus both engine branches
  of this wave, and that was the base every generated artifact here was dumped
  from - but it had taken `eng1-restriction-grammar` only as far as the commit
  that was its head when the worktree was made. eng1 put three more on top
  afterwards (the per-mana colour count, Smokebraider as the second printing of
  "any combination of colors", and the schema 5 -> 6 bump), so the integration
  branch did not merge cleanly with eng1's real head and could not land -
  which mattered here because the same wave's ledger fix, the one that stops
  eng1 and eng2 being red when merged separately, was sitting on that partial
  base. Merged properly, engine-side, before regenerating anything.

  Three conflicts, all of them two branches adding a field beside each other:
  `Action` takes `color_choice_count` next to `produces`/`restriction`/
  `restriction_understood`, `_battlefield_ability_actions` fills both onto each
  of its two construction sites, and `docs/engine-coverage.md`'s CR 106.6 row
  keeps the "read bar one documented fail-closed row" narrowing while taking
  eng1's addition of Smokebraider to the read list. `KNOWN_UNREAD_RESTRICTIONS`
  still names exactly Rosheen Meanderer, because Smokebraider arrives read.

  The lesson for the artifact task, which is the one that owns generated files:
  a `path` dependency and a green `test_openapi_drift.py` prove only that the
  dump matches the code that is *installed*, never that the installed code is
  the whole wave. Check the integration base against `git log <branch>..HEAD`
  and `HEAD..<branch>` for **every** branch it claims to carry, not against the
  commit it was branched from. `tests/test_restricted_mana_contract.py` is the
  standing version of that check and now pins `Action.color_choice_count`
  alongside the other eight fields: a regeneration against an engine carrying
  only part of the wave goes red on the committed bytes.

  Regenerating on the real base moved `openapi.json` by exactly one property
  (`Action.color_choice_count`, `integer`, default `0`). `mise run
  fixtures:payloads` and `mise run tools:export` both came back byte-identical
  - the payloads because no fixture board activates a colour-choosing mana
  ability, and `tools.json` because decision D6 is browser-only.

- **2026-09-09 - restricted mana on the wire: four decisions that are not
  obvious from the diff.** (1) `Action.produces` is the **printed**
  production, deliberately not board-resolved. Cavern of Souls reports "{1}"
  plus one mana of any colour because that is what the printing says;
  resolving "any colour" against the creature type its controller actually
  named is the reader's job, not this wire's. Resolving it here would have
  meant the engine answering a question about a board state the seat can
  already see, and it would have made the same action serialise differently
  for two viewers of the same permanent. (2) `ManaSource.ability_index` must
  be sent with `restriction=` **explicitly**; never left to
  `restriction_for_production` to infer from what the mana turned out to be.
  The plan expected the Cavern of Souls / Delighted Halfling tie to be the
  case that proves it - both said to pair an unrestricted `{T}: Add {C}.` with
  a restricted "add one mana of any color" whose production set includes `{C}`
  - and **that premise is simply false**: "any color" is CR 106.1a's five
  colours, and colorless is a type of mana that is not a color at all
  (CR 106.1b, both looked up 2026-09-10), so `{C}` can only ever have come
  from the plain ability and there is no tie to break. A sweep of all 747
  engine printings agrees: no card in the pool has a restricted and an
  unrestricted `{T}` ability that can produce the same mana, so inference and
  the named ability give the same answer on both of those cards. The real
  divergence is board-blindness, and it is live: inference asks
  `ManaAbility.effective()` with no board at all, so Sunken Citadel's two "the
  chosen color" abilities match nothing and come back **unrestricted** however
  restricted they are.
  Passing the restriction through is what keeps the fail-closed side of that,
  and the tie-break toward unrestricted is what makes getting it wrong quiet. (3) `serialize.snapshot`'s `pools` were
  left alone on purpose - they stay a flat colour-to-count dict and did
  **not** grow `restricted`/`spend_effects` the way `ManaPoolView` did. That
  snapshot is the audit log's recorded answer about a moment already passed,
  read back by builds other than the one that wrote it; `ManaPoolView` is a
  live view a seat acts on now. Widening the snapshot would have changed the
  shape of rows already on disk for no reader that needs them. (4)
  `fixtures/payloads/*.json` is pinned **by SHA** from `magic-cards-frontend`.
  Running `mise run fixtures:payloads` is therefore a real cross-repo
  consequence, not a local artifact refresh: every regeneration moves the
  hash that repo asserts against, and the frontend has to be updated in the
  same wave or its own pin goes red. Regenerate it only when the payload
  shape genuinely moved, and say so when you do.

  Operationally, the artifact task cannot run *beside* the tasks it
  regenerates for, and the first attempt at it proved that by committing an
  artifact with none of the new fields in it. `openapi.json` is dumped from
  the live app, so it can only show a field once that field's code is in the
  tree, and the api's engine is a `path` dependency on `../magic-cards-model`
  - which in a workbench worktree is a **symlink to the shared checkout**, not
  to any sibling worktree, so an engine change is invisible here until it is
  on engine `main`. Regenerating before then commits a half-finished schema
  that `test_openapi_drift.py` holds wrong in both directions: it passes,
  because a stale-but-self-consistent dump matches the app that produced it.

  The fix is to make the base real rather than to wait for it. This branch
  merges its three sibling api branches into itself (it lands last, so their
  commits arrive here first either way), and the engine side is a detached
  worktree at `magic-cards-model-integration` holding engine `main` plus both
  engine branches, with the `magic-cards-model` symlink repointed at it and
  `uv sync --reinstall-package magic-cards-model` run so the venv's editable
  `.pth` follows. Repointing the venv and reverting it afterwards proves
  nothing, because nothing committed then holds the claim; that is what
  `tests/test_restricted_mana_contract.py` is for. It asserts the eight fields
  against the committed bytes of `openapi.json` - the assertion
  `test_openapi_drift.py` structurally cannot make - so an artifact
  regenerated against a base missing the change fails loudly instead of
  passing quietly.

- **2026-09-09 - CR 106.6 reached the wire, and a payment can now name which
  mana ability it taps.** Two halves. Read-only: `ManaPoolView` gained
  `restricted` and `spend_effects` (`serialize._mana_pool_view`), each entry
  carrying the sentence *and* how much of `floating` is under it - the engine
  keeps both as **subsets** of `floating` on purpose (CR 106.4a announces all
  the mana that is there), so without the amounts two restricted `{C}`
  serialised as `floating {"C": 2}` and were indistinguishable from two a
  player could spend on anything. `RestrictionView` is deliberately two
  fields, `text` + `understood`, and no `clauses`: publishing the engine's
  `SpendClause` tuple would pin a parser-internal shape that is still moving.
  `understood=False` is **published, never filtered** - an unread CR 106.6
  sentence is fail-closed in the engine, so that mana pays for nothing at all
  and a view that hid the flag would show a player a resource they do not
  have. `AbilityView.restriction` is the same claim one step earlier, filled
  in all three of `engine_api.list_abilities`' branches via
  `affordances.ability_hints(..., source)`. No `visibility.redact_view` pass
  was needed (the same question commit 3eadbd9 asked): a pool is public
  (CR 106.4a) and `_redact_player` already leaves the whole block alone, and a
  hand card's entry is dropped for every other viewer card-and-all.
  `serialize.snapshot`'s audit pools and `commands.py`'s board fingerprint
  were left alone on purpose (approved decision D3).

  Writable: `ManaSource.ability_index`, and a third branch in
  `engine_api.pay_with` that resolves the production from the **named**
  ability and passes `tap_for_mana(restriction=...)` explicitly. That is
  correctness, not tidiness. Left to itself `tap_for_mana` calls
  `restriction_for_production`, which matches on what the mana turned out to
  be, breaks a tie toward *unrestricted* on purpose, and - the live divergence
  pinned by `test_the_named_abilitys_restriction_beats_what_inference_would_
  have_said` - asks `ManaAbility.effective()` with **no board**, so Sunken
  Citadel's two "the chosen color" abilities match nothing and come back
  unrestricted however restricted they are. Worth recording: the Cavern of
  Souls / Delighted Halfling tie the plan described **does not exist** - "any
  color" excludes colorless (CR 106.1a / CR 106.1b), so `{C}` names their
  plain ability unambiguously - and a sweep of all 747 engine printings found
  no other card whose restricted and unrestricted `{T}` mana abilities can
  produce the same mana either. The board-blindness above is the real
  divergence, and it is the one the mutation test pins.

  Two things the api cannot ask the engine for: CR 106.7's "could produce"
  survey and the controller's land types both live behind `Game`'s private
  helpers, so `mana_from` leaves them unset and an ability that needs them is
  refused with the engine's own CR 106.5 wording rather than guessed at.
  Everything else `resolve` accepts (counters, imprint, CR 614.12a's chosen
  colour) is plain `CardState` state and is passed. `tap_for_mana`
  re-validates board-aware afterwards, so a disagreement is refused, never
  quietly accepted.

  Found and fixed on the way: `list_abilities`' intrinsic-mana fallback branch
  iterated `parse_mana_abilities`, which returns bare `ManaAbility` objects,
  and then asked each one for a `.mana` attribute it has never had - so
  `mana_options` and `is_mana_ability` were silently empty for every ability
  that branch has ever listed. It now iterates whole `ActivatedAbility`
  objects (`_printed_mana_activations`), which also gives the branch the
  `.effect` a CR 106.6 sentence is read out of and a real printed `.text`,
  retiring `_mana_ability_text`. Nothing in the card pool reaches that branch,
  so the test gets there by making `Game.activated_abilities` answer with
  nothing for one permanent.

- **2026-09-09 - split-card decklist matching (`Wear/Tear` etc.) was already
  general, from a prior session; the only real gap was `Fire Magic` missing
  from the pool entirely, and one bundled-deck assertion pinned the old
  gap.** `scryfall._faces`/`fixture_slug`/`_pool_names_match`/`load_pooled`
  already derive every accepted spelling from a printing's own name
  structure (split on `//`, casefold, strip) - verified generically across
  all 32 multi-faced pool printings and 4 spellings each (full name, front
  face, `A/B`, `A//B`), 0 misses, via a new
  `test_every_multi_faced_pool_card_resolves_from_any_spelling`
  (`tests/test_fixture_pool.py`). No code change was needed there. `Fire
  Magic` was added to the engine's pool via `scripts/add_card.py` (engine
  repo). `test_every_bundled_deck_is_almost_entirely_pooled`'s assertion
  moved from `{"Fire Magic"}` to `set()` and is **expected to stay red
  here** until the engine-repo commit adding `fire_magic.json` merges to
  `../magic-cards-model` (this repo's `path` dependency is editable against
  that shared checkout, not any worktree) - confirmed it goes green with no
  further api-repo change by pointing `MCM_FIXTURE_CARDS_DIR` at the
  engine worktree that already has the file.

- **2026-09-09 - a cost that names objects was unreachable from `cast` at
  three layers at once, and the answer was too big to say so.** A live
  kubernetes seat (`mtg-seat-75bd9739-35bc3b7f-wkl4j`) spent a whole turn and
  four identical calls failing to evoke Solitude. Every layer was wrong in a
  different way and each one hid the next. (1) `engine_api._drive_cast`
  handed `req.choosing` to `use_alternative_cost` and *not* to
  `casting.use_keyword` next to it - and by keyword is exactly how
  `legal-actions` and a hand card's own `cast_options` tell a caller to
  announce evoke, so the one spelling every surface points at was the one
  spelling that could not pay a non-mana keyword cost. (2) `mcp.py`'s `cast`
  tool exposed none of `CastRequest`'s cost fields at all - not `choosing`,
  which had existed since Force of Negation's pitch, nor `alternative_cost`,
  `delve` or `convoke` - so the seat guessed `targets`, `mana_sources` and
  `exiling` and was refused by each. (3) CR 118.8a's printed additional costs
  had no field on `CastRequest` in the first place: `Casting.sacrificing`
  /`.discarding`/`.exiling`/`.tapping` had always been there with nothing able
  to fill them, so Fling was announced for {1}{R}, sacrificed nothing and
  refused to be paid. All three are closed, `seat_cli` carries the same eight
  flags (the parity `test_front_end_parity.py` exists for), and
  `affordances.alternative_costs` now publishes `candidates` next to
  `requires` - which cards qualify, not just how many - because "Exile a white
  card from your hand" is prose no client can filter a hand on. The engine
  itself needed no change: its refusals ("exile needs 1 card(s) chosen, 0
  given") were correct and complete the whole time, and merely unreachable.

- **2026-09-09 - an action tool answers about the action, not about the
  world; measured, not estimated.** The same stuck seat could not read the
  refusal above because one `cast` result was 25,397 tokens, past its own
  client's 25,000-token ceiling - so it fell back to `grep` and `python3 -c`
  over the blob and still never found it. Reproduced through the real MCP
  tool on a 28-permanent board: 66,636 bytes, of which `state` was 37,683 and
  `available_actions` another 11,538, while the `outcome` that answered the
  call was 84 bytes sitting at character 13,724 - behind everything else,
  because the payload was serialised `sort_keys=True`. Now 12,244 bytes on
  that same board (an 81.6% cut) with `outcome` at character 3.
  `mcp._action_answer` drops the board's bulk (battlefield, hands,
  graveyards, exile, command) in favour of a `board` block holding the spine,
  the stack, combat and both players, names what it left out in
  `board.omitted_sections`, and drops the ~20 null/empty fields every
  `ActionView` printed. Three things were deliberate: the narrowing lives at
  the MCP boundary and NOT in `seatkit.run_action`, because `seat_cli.py` is
  the other front end onto that same function and is a human who can scroll
  (and `test_action_flow.py` pins that dict); `wait_for_priority` and
  `battlefield_state` are untouched, since handing over the board is what
  they are for; and `pending_decision` is lifted to the top level, which is
  where `mcp._tool_result` already looks when a payload has no `state` key,
  so the `input_required` shaping keeps working with no second check.

- **2026-09-07 - `Game.model_dump()` size scales with library size, not board
  complexity.** Measured through the real HTTP action endpoints (`POST
  .../actions/play-land`, `POST .../advance`), not hand-built: two real 60-card
  legal decklists (`52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt` vs `52
  Forest\n4 Grizzly Bears\n4 Lightning Bolt`, the same pair
  `tests/test_repository_contract.py` and `tests/test_decks_api.py` use),
  played from the opening hand through mulligan/opening-hand declarations,
  then 7 full turns (14 individual player-turns) with a land played every
  turn, landing on turn 8's precombat main with 14 lands on the battlefield
  and both libraries at 46 cards. `json.dumps(game.model_dump(mode="json"),
  separators=(",", ":"))` (compact, matching how JSONB actually stores it, no
  pretty-printing whitespace) came to 182,389 bytes. Of that, 66,861 bytes
  (about 37%) was just the `card` sub-object embedded in the 92 remaining
  library `CardState`s - each library card carries its own full copy of the
  (already gameplay-trimmed) `Card` model, not a reference, so the library's
  contribution to the row scales with how many cards are still in it. That is
  lower than a verbally-quoted 74% figure from the audit that flagged this -
  plausibly because this board's library holds only two small, text-light
  basic-land printings; a deck with more diverse, oracle-text-heavy nonbasic
  cards would push the proportion higher. The number to remember is not the
  exact percentage, it is the shape of the cost: `_update_column`/`replace()`
  in `repository_postgres.py` rewrite the whole `state` JSONB column on every
  write, so a game's row-rewrite cost tracks its library size (which only
  shrinks - CR gives no way to un-draw), not how complicated the board is.

- **2026-09-07 - Not every column on `games` is covered by the version CAS.**
  `_update_column` (`repository_postgres.py:296-301`) writes `seats`,
  `decklists`, `agent_settings`, `ai_players`, `command`, `tape` and `pending`
  with a plain `UPDATE ... WHERE id = :id`, no `version` filter and no
  `version` increment. `replace()`'s compare-and-swap only covers the `state`
  column. So those seven columns are last-writer-wins across replicas: two
  processes racing a write to any of them (e.g. two seats registering at once)
  can have the later commit silently clobber the earlier one, with no
  `VersionConflict` raised. Within one process this is already serialised by
  `locked()`'s per-game `threading.Lock` (see the module docstring's
  "paused-command storage" section) - the gap is only across processes. This
  is within T9's agreed scope (the plan only asked the game-state CAS to be
  real), not a bug to fix reflexively - but it is a real property, worth
  knowing before somebody rediscovers it by losing a write.

- **2026-09-07 - `records.seq == games.version` is a code invariant, not a
  schema one.** `records.seq` is set to `new_version` at the single call site
  `repository_postgres.py:424`, inside the same transaction as the `games` CAS
  update that produced that version - so in practice every committed `records`
  row's `seq` equals the `games.version` it was written under. But nothing in
  the DDL (`db.py`) enforces this: `UNIQUE(game_id, seq)` on `records` stops
  two records rows from claiming the same `seq`, but a `CHECK` spanning
  `records.seq` and `games.version` is not expressible - a `CHECK` constraint
  can't reference another table. So the equality holds only as long as every
  writer goes through this one code path; a future direct `UPDATE games SET
  version = ...` or a hand-inserted `records` row would silently break it with
  no constraint violation to catch it. No cross-table trigger was built for
  this (out of scope for T9) - this entry exists so the assumption is written
  down before something depends on it silently.

- **2026-09-08 - `pass_until` died on its second pass because `poll()`
  published a board it had read before the seat's own action.** The owner's
  live report ("my pass_until doesn't work") looked like a `pass_until` bug
  and was not one: every human action in the reported game
  (`045634da21ab4cca88d9c3268745f064`) was followed 1.5-2.2s later by a
  duplicate the engine refused - passes with "you does not have priority,
  claude does", an `attack`, and five `declare-blockers` in one combat.
  `poll()` reads the board, then reads the log, then calls `tickRun()`;
  anything this seat does in that window lands on a server whose answer to
  the already-issued board read predates it. `poll()` wrote that board in as
  current (so the screen showed the pre-action board, which is what had the
  owner clicking again) and handed it to `tickRun()`, which read
  `priority_holder` as still the human's and passed a second time. Reproduced
  against a live local server by holding the `/log` read open and clicking
  "Pass until" inside it. It is not a rare race in a late game: one poll
  cycle grows past `POLL_MS` (a bigger board, a longer log), so a poll is in
  flight at every instant - which is why the same game's early turns ran the
  feature perfectly (seq 20-132) and everything after seq 144 failed. Fix: a
  `mutations` counter in the store, bumped by `send()` before every mutating
  request; a poll whose reads span one of them discards them. NOT a server
  bug - the state endpoint was probed under concurrent load on both
  repositories and never returned a board older than a committed pass.

- **2026-09-08 - a refused pass is two different things, and ending a run on
  both is how it dies silently.** `tickRun`'s old last line was `if (!out.ok)
  store.set({run: null})`, which treated "this seat does not hold priority
  any more" (a lost race - the opponent moved between the read and the pass,
  and the next tick is fine) exactly like "this move is not available" (a
  real refusal), and treated a rejected `fetch` as a refusal at all. Narrowed:
  a refused pass now ends the run only when a fresh board read still says
  this seat holds priority. The board is asked rather than the refusal text
  pattern-matched - engine wording is not a client contract.

- **2026-09-08 - `test_a_spell_cast_in_a_window_the_run_passed_through_hands_
  priority_back` was flaky in its own right, and not for the reason it
  looked.** Its opponent thread makes two independent calls, the cast and
  bob's own pass; the seat under test can poll between them, and
  `autopass_stop_reason`'s stack stop fires on the cast alone (correctly -
  CR 117.4 makes the unmade pass the whole chance to respond). The stop's
  snapshot then reads `priority_holder: "bob"` and the final assertion
  failed. Demonstrated deterministically by inserting a 0.5s sleep between
  bob's two calls, which reproduces CI's `assert 'bob' == 'alice'` every
  time. The assertion now reads the settled board (both of bob's calls have
  landed by then - `thread.join` has returned) instead of the stop's
  snapshot. A test-level race in the test's own harness, and nothing to do
  with the browser-side `pass_until` defect the same session was chasing.

- **2026-09-08 - T10 supersedes the T9 note above: `records.seq` is no
  longer `== games.version`, by design.** Once a refused action that never
  touches `games` at all (a `ValueError` caught before any command ran, an
  early `_answer_declaration` refusal, an admin action, a lobby reset) also
  needs a row - T10's whole point - a strict `seq == version` mapping stops
  being expressible: the same game can accumulate more `records` rows than
  `games` commits. `seq` is now a plain per-game append order, computed from
  the `records` table itself (`repository_postgres.PostgresRepository.
  _next_seq`, serialised with a transaction-scoped `pg_advisory_xact_lock`
  keyed by `game_id` - narrower than `create_if_under`'s whole-table lock,
  and released automatically the same way). `replace()` still writes exactly
  one row per successful CAS (T9's pinned guarantee, unchanged;
  `test_repository_postgres_cas.py` still passes unmodified) - it just no
  longer borrows its `seq` from the version it just wrote.

- **2026-09-08 - `records.game_id` carries no `ForeignKey` (T10, and the
  `alembic` migration this repo's only one so far was edited in place rather
  than a second one added - see below).** T9's original schema had `ON
  DELETE CASCADE`. Once `records` became the actual audit trail rather than
  an empty placeholder, that constraint was wrong: `lobby.reset_game` writes
  the ended game's last `records` row - the reset itself - and then deletes
  the game, and `test_lobby_reset.py::
  test_the_reset_is_written_into_the_ended_games_log` (pre-existing, pinned)
  requires that row still be readable afterward ("a transcript never
  silently trails off"). A `CASCADE` deletes exactly that row the instant
  the game it describes is removed. Fixed by dropping the constraint
  entirely (`InMemoryRepository.delete` matches: it no longer pops
  `self._records[game_id]`) - the two tables are linked only by `game_id`
  values matching, never by a constraint the database enforces.

- **2026-09-08 (corrected same day) - editing a shipped migration in place
  is never a one-time allowance, and this one broke every already-migrated
  database.** The `ForeignKey` removal above first went straight into
  `374af1d25f73_initial_schema.py` with its revision id unchanged, on the
  reasoning that T9 had landed only hours earlier so nothing real could
  depend on the old shape. That reasoning is wrong wherever the migration
  has run *at all*, deployment or not: `alembic upgrade head` is a no-op on
  a database already stamped `374af1d25f73`, so the CASCADE survives there
  forever while this branch's code hard-depends on it being gone -
  `append_record` for an unknown game raises `ForeignKeyViolation` on that
  database and nowhere else. It was green locally only because `mise run
  test:pg` reuses the persistent `magic-cards-model-api_postgres-data`
  volume, which happened to have been provisioned from the edited file:
  green on one machine, red on the next. Fixed properly: the initial
  revision is restored to what it shipped as, and `8f1c47a0b6d2` drops the
  constraint as its own revision (guarded on `pg_constraint`, so it is a
  no-op on a database provisioned from the briefly-edited file).
  `tests/test_migrations.py` creates its own scratch database per test,
  provisions it from the initial revision, and proves the upgrade removes
  the constraint - never reusing the shared, possibly-cached one.

- **2026-09-08 - a `dry_run` request must skip repository persistence
  outright, not just skip auditing it.** Before T10, `_run_command` called
  `store.replace()` for *any* `RunStatus.COMPLETED` result, dry runs
  included - `engine_api.cast_card`'s dry-run branch never mutates the real
  `Game` (CR-legal, `dry_run_copy`'s own isolated probe), but `commands.
  run_command` still reports `COMPLETED`, and `replace()` doesn't know or
  care that the `Game` it was handed is state-identical to what's already
  committed: it bumps `version` and writes a `records` row regardless. Pre-
  T10 this was silently wasteful (an empty-payload row nobody ever read).
  Once `audit.entries`/`GET /log` started reading directly from `records`,
  it became a real bug: `_respond`'s `dry_run` branch skips `audit.record`
  entirely (by design - see `test_dry_run_cast_is_not_audited`), so that
  row's payload never got filled in, and a subsequent `GET /log` choked on
  `entry["action"]` for the resulting bare `{"seq": ...}` row. Fixed two
  ways, both worth keeping: `_run_command` now reads `request.dry_run` and
  skips `store.replace`/`store.store_paused_command` outright when set, and
  `audit.entries` now defaults every expected key (`_UNRECORDED_DEFAULTS`)
  onto a payload that is missing one, so a future gap of the same shape
  degrades a reader gracefully instead of crashing it.

- **2026-09-08 - `store.py`'s singleton has two independent bindings a test
  swapping backends must patch separately, and this is not just a testing
  footgun.** `app.py` does `from .store import store` once, at import time -
  overwriting `magic_cards_model_api.store.store` afterward does not change
  `app.py`'s already-bound name. `audit.py` re-imports `from .store import
  store` *lazily*, inside `record`/`entries` themselves, precisely so it
  keeps working when the app module reloads - but that also means it reads
  whichever object `magic_cards_model_api.store.store` currently is, not
  whatever `app.py`'s own `store` was patched to. A test standing up a real
  `PostgresRepository` for `app.py`'s routes (`tests/
  test_version_conflict_http.py`) must monkeypatch both names, or `audit.
  record`'s own `fill_record` call silently lands on the untouched real
  `InMemoryRepository` singleton and raises `KeyError` for a row that
  genuinely exists - just in the other store. More importantly: this also
  means `store.py`'s singleton selection was, at the time this was written,
  hardcoded to `InMemoryRepository` with no switch to `PostgresRepository`
  at all. That wiring landed later the same day - see the
  `store.select_store()` note at the end of this file - and the two-bindings
  trap above is exactly why the choice is made at import rather than by
  rebinding the name afterward.

- **2026-09-08 (corrected same day) - the pregame routes ARE gated, and the
  exemption they briefly had rested on a false premise.** An earlier pass
  left `pregame/mulligan`, `pregame/opening-hand/action` and
  `pregame/opening-hand/finish` outside `records.require_reasoning`, giving
  two reasons. The first was simply not true: `ai_pregame.settle_now` calls
  `opening.declare_mulligan` / `opening.finish_opening_hand_actions`
  directly, in process (`ai_pregame.py`'s `_take_default`), and never
  travels through the HTTP route at all - so the backstop never reaches the
  gate and could not have been affected by it. The second was test
  pressure, not a rule: `tests/test_ui_e2e.py` and `tests/test_mulligans.py`
  post those routes for a seat that *is* this fixture's `ai: true` one, with
  no reasoning. Both are now gated like every other mutating route and the
  fixtures supply a reason, which is the honest direction of that fix.
  Keeping or mulliganing an opening hand is the first real choice an agent
  makes in a game; there is no reason it alone should be unexplained.

- **2026-09-08 - `state-based-actions` and `PUT .../step` are gated too, and
  the enumeration list is now enforced by a test rather than by hand.**
  `state-based-actions` took no body at all, so it could neither name a seat
  nor be gated, while `README.md` said it took an optional `reasoning` and
  this file said it was gated - two documents describing a hole. It now
  takes the optional `ActorRequest` body they both described (called with no
  body it is nobody's action and requires nothing). `PUT .../step` reads
  like board setup and is not: it moves the turn marker, runs the suspend
  upkeep, and is an MCP tool every agent holds - it was the one mutating
  tool that never grew `reasoning`. `tests/test_reasoning_gate.py` is the
  standing guard: default-deny over every mutating route *and* every MCP
  tool, with an exemption list that must name a reason, plus a check that
  no exemption names something that no longer exists.

- **2026-09-08 - `legal_actions_before` must be redacted on the seat the
  server resolved, never on a field the caller supplied.** The first version
  keyed the log's redaction on the audit entry's `detail["player"]`. On
  `actions/declare-blockers` that field is `req.player`, straight off the
  request body and validated against nothing, while the enumeration itself
  was computed for the *deciding* seat (`engine_decision.player`) - so alice
  could post with her own uid and `"player": "alice"`, be refused for not
  opening bob's seat, and then read bob's own pre-decision enumeration out
  of the log. `records.legal_actions_before` now returns a
  `LegalActionsBefore(player, actions)` pair, `audit.record` writes both
  halves or neither (`legal_actions_before` + `legal_actions_for`), and
  `visibility.redact_log_entry` reads the subject rather than anything the
  request carried - the two can no longer come apart. `_answer_declaration`
  and `decide` additionally compute nothing at all until the uid gate has
  agreed who the caller is.

- **2026-09-08 - `GET .../log` with no uid used to hand out every seat's
  `legal_actions_before` in full.** `redact_log_entry` was applied only for
  `isinstance(viewer, str)`, which is false for no-uid *and* for an
  unrecognised uid, so a caller presenting nothing saw strictly more than
  one presenting garbage - of exactly the hidden information
  `GET .../legal-actions` refuses to anyone but the named seat. The
  unauthenticated *snapshot* view predates this branch and is deliberate
  (the debugger's eye), so it stays; `legal_actions_before` now goes through
  the gate for every non-admin viewer, and `redact_log_entry` applies the
  seat-shaped snapshot filtering only for a real seat uid so that stayed
  unchanged.

- **2026-09-08 - `store.py` now selects its repository from the
  environment**, superseding the note above that recorded this as unwired
  scope. `db.configured_database_url` answers "is a database configured at
  all" (`None` is not an error, unlike `database_url_from_env`), and
  `store.select_store()` picks `PostgresRepository` when it says yes and
  `InMemoryRepository` when it says no. The choice is made *at import*
  precisely because of the two-bindings trap recorded above: choosing once,
  before `app.py`'s static import or `audit.py`'s lazy one can run, is what
  keeps both views the same object. `PostgresRepository` had to grow
  `record_automatic_pregame`/`automatic_pregame` (process-local dicts, like
  its `_locks`) to be selectable at all - `opening.py` calls them on
  whichever store was chosen. `tests/test_store_selection.py` boots real
  subprocesses and checks both bindings.

- **2026-09-08 - the `records` table has no retention, no GC, and no way to
  read an orphaned row back through the API.** Dropping the FK is genuinely
  motivated - `lobby.reset_game` writes the `game_reset` row and *then*
  deletes the game, so a CASCADE would delete the very row that makes the
  transcript end with the reset - but the consequence is that every deleted
  game's rows stay in `records` forever, and `GET /api/games/{id}/log`
  404s on the game before it ever reads them. Nothing prunes them and
  nothing exports them. That is a deliberate deferral, not an oversight:
  reading a finished game's transcript back is what the review surface in
  `docs/architecture.md` is for, and retention belongs with it.

- **2026-09-08 - `audit.entries` no longer deep-copies each entry's
  `detail`.** The old module-global version returned
  `dict(entry, detail=dict(entry["detail"]))`; reading from the repository
  returns `{**defaults, **row["payload"]}`, whose `detail` is the stored
  dict itself for `InMemoryRepository` (postgres re-parses JSONB, so it is
  a fresh object there). No current caller mutates it, and `redact_log_entry`
  already rebuilds rather than edits in place - noted because the two
  implementations differ in aliasing, which is the kind of difference a
  future caller could trip over.

- **2026-09-08 - a seat has two priority verbs, and the routes behind the
  retired ones stay.** CR 117.1 lists what the player with priority may do
  ("cast spells, activate abilities, and take special actions") and CR 117.3d
  has a player who chooses none of them pass; CR 117.4 makes resolving the
  stack's top and ending a step consequences of everyone passing, and CR 703.2
  says turn-based actions "are not controlled by any player". So `resolve_top`,
  `advance` and `set_step` were never player actions, and they are gone from
  `mcp.py`, `seat_cli`, `prompt.py`, `docs/ai_seat_brief.md` and the play UI's
  verb chips. **Their HTTP routes are deliberately kept**: roughly a hundred
  test call sites drive and observe boards through them (including CR 500.3's
  untap and cleanup, which `tests/test_priorityless_steps.py` reaches only via
  `POST .../advance`), and `tests/test_advance_refusals.py` pins the engine's
  own refusal text on one of them. Retiring a *verb* and deleting a *route* are
  different decisions; only the first was the over-grown surface.

  Two things kept deliberately against the tidier surface: the play UI's
  Advance button (`priority.js`'s `advanceOffered`) is the human's only escape
  from a priorityless step `settle_priorityless_steps` declined to cross, and
  `wait_for_priority` stayed because it observes rather than acts.

- **2026-09-08 - "stop when anything hits the stack" is a modifier, not a
  destination.** It combines with the step ticks rather than replacing one:
  the case it exists for is "pass until my precombat main, AND hand the board
  back if the opponent casts something in my upkeep", which needs both live on
  the same run. In the UI it is `stops.onStack`, beside `stops.mine`/
  `stops.theirs` rather than inside either; over MCP/CLI it is the
  already-existing `stop_on_stack`. `mcm.stops.v1` is unchanged as a storage
  key and a payload written before the tick existed reads back with it ON -
  absent has to mean on, or the redesign silently takes the chance to respond
  away from everyone who had already ticked a step.

- **2026-09-08 - `seatkit.pass_until` hands the board back for ANY outstanding
  decision, not only a combat one.** A pause rolls the committed game back
  (`commands.py`), so `priority_holder` still reads as whoever held it before
  the freeze - it is stale, not "nobody". A pass posted on the strength of it
  cannot complete (it crosses the same unanswered question), pauses too, and
  `app.py`'s `_run_command` stores that second pause over the first, discarding
  the question the other seat was about to answer. Pinned by
  `test_a_run_never_passes_at_a_game_frozen_on_another_seats_decision`.
- **2026-09-08 - the pause slot is one seat's half-finished command, and the
  guard is keyed on command equality everywhere except
  `pregame/settle?use_defaults=true`.** `app.py::_pause_slot_guard` refuses
  any command that is not byte-identical to the live stored one
  (`DecisionInProgressError`, 409), and hands the identical one its own
  standing question back rather than re-running it - that idempotent-retry
  branch is what actually closes the clobber, because seatkit's autopass
  re-posts *its own* `pass-priority` on every `paused: true` it reads, so
  the clobbering command was the stored command. Staleness is tested
  (`commands.pause_is_live` replays from the committed snapshot) rather than
  assumed, or a stale pause would wedge the game forever. The `use_defaults`
  settle is deliberately keyed on the stored command's *kind* instead:
  `use_defaults` is part of the payload, so the `use_defaults=False` settle
  that left the pause and the `use_defaults=True` call that overrides it are
  never the same `Command`, and refusing there would take away the
  operator's only escape hatch for a seat gone quiet mid-pregame.

- **2026-09-08 - `pause_generation` is bumped by `store_paused_command` and
  by nothing else, and is never reset.** The guard above makes a stale
  answer impossible to *apply*; the generation is what tells a *client* the
  standing question moved on between its read and its answer, which a
  two-decision resolution does the instant the first answer is accepted.
  Bumping it anywhere else (a commit, a clear) would make it mean "something
  happened to this game" instead of "the standing question changed", and
  resetting it on `clear_paused_command` would let a later pause reuse a
  number a stale client is still holding - the one thing a generation may
  never do. It is deliberately absent from the redacted
  `{player, kind, prompt}` a non-deciding seat sees: only the deciding seat
  can answer, so only the deciding seat has any use for it.
  `DecideRequest.generation` is optional, so every client that predates it
  keeps working - it is a guarantee a caller opts into.

- **2026-09-08 - `k8s_seats.build_job` is a renderer now, and everything
  that isn't one of the nine `SEAT_JOB_PLACEHOLDERS` (phase 2b T9, plan M10)
  is baked into the committed `deploy/seat-job.yaml`, not read from env.**
  That includes image tags (still the literal `__TAG__` token
  `deploy/k8s/*.yaml` already uses, but nothing renders it for this file
  yet - `mise run k8s:deploy` doesn't touch `deploy/seat-job.yaml`, so every
  seat Job will pull whatever tag is checked in until phase 2b's T20 fixes
  raw-manifest tag pinning for real), the memory PVC name, the guides repo,
  both secret names, `activeDeadlineSeconds`/`ttlSecondsAfterFinished`, and
  the spellslinger URL. `K8sSettings.mcp_url` (env `MCP_SERVICE_URL`)
  defaults to `""` because the mcp service does not exist as its own
  deployment yet - `deploy/k8s/50-deployment.yaml` does not set it, so
  `MCP_URL` on the rendered Job's `mcp-config` initContainer is empty in
  production today. `ownerReferences` and the downward-API
  `POD_NAME`/`POD_UID`/`POD_NAMESPACE` env that fed it are gone entirely
  (T10 needs this: a durable-games lobby restart must not garbage-collect
  the seat Jobs of games that survived it) - `metadata.namespace` is the one
  exception kept in code rather than baked static, because a body/URL
  namespace mismatch is a hard Kubernetes API rejection, not a soft drift.
- **2026-09-08 - T13: `QUIET_PATHS` split by *future owner*, not by process.**
  `WRAPPER_QUIET_PATHS` and `DRIVER_QUIET_PATHS` both keep `/healthz` -
  legitimately duplicated, because the wrapper and the driver are two
  entry points shipping in one repo (`magic-cards-claude-wrapper`), each
  with its own probe on its own port (`k8s_seats.py`'s `agent`/`driver`
  containers). A naive "no pattern may repeat" test is wrong for this file;
  the real invariant is that api/lobby patterns never leak into
  wrapper/driver tuples or back.

- **2026-09-08 - T13: `AgentSettings`'s wire is camelCase on exactly one
  hop - the seat assignment - and nowhere else.** `to_wire()`
  (`model_dump(by_alias=True)`) is used only by the two dispatch-payload
  write sites (`k8s_seats.build_job`, `ai_seat._dispatch_to_driver`) and
  read by the driver's `_wanted_agent_settings` and the wrapper's
  `SessionIn`. The browser-facing wire (`decks_api.py`'s create-game body,
  `serialize.py`'s game state view, `ui/lobby_model.js`) stays snake_case
  deliberately - out of this task's scope, and `AgentSettings.
  model_config.populate_by_name=True` keeps that wire validating unchanged
  even though the field now also carries a camelCase alias. `SessionIn`
  additionally needed `extra="forbid"`: without it, posting the old
  `max_thinking_tokens` key is not a validation error at all - pydantic
  silently ignores the unrecognised key and the field falls back to `None`,
  which is exactly the silent wrong-default failure this contract exists to
  prevent. `contract/agent-settings.schema.json` is the committed shape;
  `tests/test_agent_settings_contract.py` validates both the write end and
  the read end against it.
- **2026-09-08 - T1: giving every route a named `response_model` needed
  `response_model_exclude_unset=True` on every route, not just the models
  themselves - and a custom `APIRoute` defaulting it app-wide does NOT work.**
  `visibility.redact_view` and a face-down card's stub both work by
  *omitting* keys from the plain dict `serialize.py` builds
  (`libraries`/`sideboards` popped outright, a face-down card missing
  `type_line`/`mana_cost`/...), and dozens of existing tests assert exactly
  that absence (`"libraries" not in board`, `"decklist" not in
  blocks[theirs]`). FastAPI's default response serialisation does not
  preserve that: it fills every declared field's default back in, so a
  `GameStateView`-shaped response started carrying `"paused": false`/`"you":
  null`/`"type_line": null` keys the wire format never had, and 38 tests
  broke on first pass. pydantic's own "was this key in the validated input"
  tracking (`response_model_exclude_unset=True`) fixes it: a key genuinely
  present with a `null` value (`state.controller` on an uncontrolled card)
  still survives, and only a key truly absent from the source dict is left
  out - reproducing the exact shape `serialize.py`/`visibility.py` already
  built, field for field.

  A first attempt set this app-wide via a custom `_ExcludeUnsetRoute(APIRoute)`
  (`app.router.route_class = _ExcludeUnsetRoute`, `__init__` doing
  `kwargs.setdefault("response_model_exclude_unset", True)`) rather than
  repeating the kwarg on every decorator - and it silently did nothing.
  `APIRouter.get`/`.post`/etc. each declare `response_model_exclude_unset:
  bool = False` as their own named parameter and forward it to
  `add_api_route` **explicitly**, so by the time the route class is
  constructed the key is already in `kwargs` (value `False`, not absent) -
  `setdefault` never fires. Every route silently kept `exclude_unset=False`;
  `openapi.json` was correct throughout (nothing about the *schema* depends
  on this flag), so `mise run test`, not the drift test, is what caught it.
  Confirmed by reading a live route's own `.response_model_exclude_unset`
  attribute (`False`) with the custom class installed. Fixed by dropping the
  class and passing `response_model_exclude_unset=True` explicitly on all
  ~40 route decorators instead - repetitive, but the only form FastAPI
  actually honours here.

- **2026-09-08 - landing T1 onto T13's merged tree: a `Field(alias=...)` is
  both a validation alias and a serialization alias, and T1's
  `response_model`s turned T13's on-purpose single-hop alias into a
  browser-facing leak.** `AgentSettings.max_thinking_tokens` carried
  `Field(..., alias="maxThinkingTokens")` so `to_wire()`'s
  `model_dump(by_alias=True)` would camelCase the one seat-assignment wire
  (T13). Once T1 gave routes real `response_model`s, FastAPI's default
  `response_model_by_alias=True` applied that same alias to every response
  embedding `AgentSettings` - `PlayerView.agent` via `GameStateView` - so
  `GET /api/games/{id}` started handing the browser `maxThinkingTokens`
  instead of the `max_thinking_tokens` `ui/lobby_model.js` and
  `test_game_agent_settings.py` both depend on. Fixed by splitting the
  alias: the field now carries only a `validation_alias=AliasChoices(
  "max_thinking_tokens", "maxThinkingTokens")` (accepting both spellings on
  input, and free for the browser's snake_case posts), no serialization
  alias at all - so `model_dump()` and `model_dump(by_alias=True)` both
  produce `max_thinking_tokens` by construction, and a future route that
  embeds `AgentSettings` cannot reopen this leak by forgetting a per-route
  opt-out. `to_wire()` renames the one field by hand instead of relying on
  `by_alias`. `tests/test_agent_settings_wire_boundary.py` pins both wires
  in one test. `openapi.json` needed no new diff once the code was fixed -
  the file already committed on this branch matched the corrected shape;
  only the live (buggy) app disagreed with it, which is what the drift test
  caught.
- **2026-09-08 - `allowed` is tri-state everywhere it is written, and every
  consumer of it must branch on `is True`/`is False`/else, never truthiness.**
  Two live bugs from treating it as a plain bool, both reproduced against a
  running server before being fixed: `audit._console_line`'s `if
  detail["allowed"]:` folded a paused command's `allowed: None` into the
  refusal arm, printing `pass_priority -> refused: None` for a pause that
  refused nothing (`detail.get("reason")` is `None` there too, since nothing
  was refused) - fixed with a real third arm, and a `paused_on: {player,
  kind}` detail (`app._paused_on`) threaded into every one of the seven
  `PAUSED` `audit.record` call sites so the line says what it paused *on*.
  Separately, `Seat.call_json` (`seatkit.py`) returned `response.json()`
  regardless of HTTP status, so any non-2xx whose body carried no `allowed`
  (FastAPI's own 422 validation shape among them - `records.
  require_reasoning`'s refusal, which raises before `audit.record` ever
  runs, so a 422 leaves no log row at all) was read by `run_action` as a
  completed action, auto-pass included. Fixed generally, not just for 422:
  `_normalise_non_2xx` treats every 4xx as a refusal (the request never ran
  server-side) with `reason` pulled from whatever shape `detail` carries -
  a plain string everywhere except the two pydantic-validation-shaped
  handlers, whose `msg` is the sentence that actually matters - and leaves a
  5xx as the same honest `unknown_outcome` an ambiguous transport timeout
  already gets, since a server error may have half-landed. A body that
  already carries its own `allowed` (`DecisionInProgressError`'s 409) is
  trusted unchanged. `mcp.py`'s and `seat_cli.py`'s own `decide` handlers
  build `{"allowed": True, **answer}` - both were transitively fixed by
  this, since a dict literal's later `**answer` (now correctly carrying
  `allowed: False` for a non-2xx) overrides the initial `True`; neither
  needed editing.

- **2026-09-08 - T7: a seat's `api_base` was two request-derived call sites,
  not one.** The plan named `ai_seat_api.py:29`'s `_api_base(request)`, but
  `decks_api.start_game_from_decklists` imported and called the same helper
  for the auto-start-on-creation path (`POST /api/ui/games`) - undetected
  because every existing test hits the app on one origin, so a request-
  derived URL and the real api URL were always equal by coincidence. Both
  call sites now read `ai_seat.api_service_url()` (`API_SERVICE_URL`, same
  env var and default `K8sSettings.from_env` already used for the kubernetes
  mechanism - `k8s_seats.py`). `_api_base` and the now-unused `Request`
  parameters on both routes are gone. The two new HTTP-level tests in
  `test_ai_seat_settings.py` forge an arbitrary `Host` header on both routes
  and assert the dispatched payload still carries the configured service URL
  - a same-origin test would not have caught the original bug at all.
- **2026-09-08 - 2b T5: deck validation's new home is `/api/decks*`, not
  `/api/ui/decks*`, and that URL choice is load-bearing, not cosmetic.**
  Moving `_resolve_all`/`validate_decklist_text` off the lobby's `/api/ui`
  namespace and onto plain `/api` (a new `api_decks.py`) puts it in the same
  URL space `app.py`'s engine-facing routes already use - which is exactly
  where the eventual `magic-cards-api` component's surface lives after the
  cut, while `/api/ui/*` stays `magic-cards-lobby`'s. Getting the prefix
  right now, while both live in one process, means no route has to move
  again at the cut. The browser's own calls (`ui/render/lobby.js`) had to
  follow immediately - `/api/ui/decks` 404s once the router moves, and a
  POST to a now-unclaimed path under the static mount at `/` answers 405,
  not 404, because that mount only serves GET/HEAD (`test_ui_routes.py`'s
  own subject) - a wrinkle a naive "old path is gone" test would get wrong
  by asserting 404 for every method.

- **2026-09-08 - 2b T5: `api_client.ApiClient`'s in-process implementation
  is a bare function call, not an httpx round trip through `TestClient`.**
  The precedent elsewhere in this repo (`seatkit.Seat`, and the
  `monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))`
  pattern the 2b plan's own risk-1 note calls out) always goes through
  `httpx.Client`, real or monkeypatched, because those callers are meant to
  run either in-process or against a deployed service unchanged.
  `InProcessApiClient` deliberately does not: it calls
  `api_decks.validate_decklist_text` directly. Pre-cut there is no separate
  api process to call anyway, so a fake HTTP hop through `TestClient` would
  buy nothing; the honest shape is "this function, called directly," with
  an `HttpApiClient` (not built by this task) taking over post-cut. The
  `ApiClient` protocol is what makes that swap cost one constructor call at
  the call site and nothing else.

- **2026-09-08 - T4: `TestClient`'s `httpx` transport normalises `../`
  dot-segments in a request path before sending it - a `%2e%2e` traversal
  test is the only one that actually reaches the route's own guard.**
  Writing the escape test for `GET /g/{game_id}/{rest:path}`'s new
  path-traversal check (`app.py`, resolves `rest` inside `ui/` and refuses
  anything that walks out via `Path.is_relative_to`), a literal
  `client.get(f"/g/{game}/../../../pyproject.toml")` never reached the
  handler at all: the client itself collapsed the path to `/pyproject.toml`
  before sending (confirmed via `resp.request.url`), which 404s through the
  ordinary root `StaticFiles` mount and proves nothing about the route's own
  defense. Percent-encoding the dots (`%2e%2e`) survives client-side
  normalisation (that only recognises the literal `..` segment) and is
  decoded back to `..` in the ASGI scope's path by the time Starlette's
  `:path` converter sees it, so the request genuinely lands in the handler -
  confirmed live against a running server (`resp.request.url` still showed
  `/g/<id>/%2e%2e/...`, status 200, body was `index.html`'s 6005 bytes, not
  `pyproject.toml`'s 1593).

- **2026-09-08 - T2: `game_id` and every `CardState`/ability `id` are the
  only real per-run nondeterminism in a captured API payload, and neither is
  seedable through the HTTP boundary.** `fixtures/payloads/<case>.json`
  (`scripts/generate_payload_fixtures.py`) has to be byte-identical across
  runs (M11: `magic-cards-frontend` pins this corpus by sha), but `game_id`
  is a bare `uuid4().hex` (`repository.py`) and every object id is minted
  off a random per-game `game_uid` (`ids.py`'s `uuid5(game_uid,
  objects_created)`) - the deterministic-replay allocator only makes ids
  reproducible *given* a fixed `game_uid`, and nothing in this api's HTTP
  surface lets a caller supply one (unlike shuffles, which the `seed` field
  on `/api/ui/games` does control). `canonicalize()` is the seed substitute:
  it walks a captured payload and replaces every id-shaped string (a dashed
  UUID or a bare 32-hex-digit one) with a placeholder numbered by first
  appearance, so two runs that mint different random ids still write the
  same file. Verified nothing else in `serialize.py` needs it - no
  timestamps anywhere, and every zone this api reports is a plain list, not
  a `set`.
- **2026-09-08 - T2: two of the seven fixture cases reuse the existing
  hybrid tests' own synthetic decklists rather than a real tournament
  list.** `split_card_labels` and `mulligan_prompt` boot through `POST
  /api/ui/games`, which needs decklist text - `test_card_split_labels.py`'s
  `DEVOURER_DECK` (`56 Swamp\n4 Devourer of Destiny`) and
  `test_mulligans.py`'s `PLAIN_DECK` (`60 Forest`), both real Scryfall
  printings, same seeds. Devourer of Destiny is not a card any real Modern
  list runs, so "a real tournament decklist with pilot and date" and "the
  exact board the split-card-labels bug needs" cannot both be satisfied at
  once here - and this repo's own `CLAUDE.md` scopes the real-tournament-
  decklist rule to `magic-cards-model`'s card-rules tests, not to
  `magic-cards-model-api`'s ordinary test practice. Reused the tests' own
  boards rather than inventing a new one, matching the plan's own words for
  this task ("exactly what the six hybrid UI tests need today").
- **2026-09-08 - MEMORY.md itself carried an unresolved git conflict marker
  (`<<<<<<< HEAD` / `=======` / `>>>>>>> 2b-t4-game-addressing` around the T7/
  T5/T4 entries) from a prior merge that landed both sides' work but never
  resolved this file's own markers.** Both sides were independent, non-
  overlapping entries, not a real conflict - fixed by keeping both and
  dropping the markers, while landing T8 on top.

- **2026-09-08 - 2b T8: `decklist` did not simply disappear off the game
  state view - it had a real, tested, out-of-scope consumer
  (`mtg_agent.driver.app`'s `seat_decklist`, matchup-guide selection),
  caught by `tests/driver/test_app.py::
  test_the_real_game_server_carries_the_field_seat_decklist_reads`, a test
  written specifically to guard against this exact silent regression.**
  The plan's S6 text ("Delete those two fields from the api's player view...
  They move to a new lobby route") was ambiguous about whether "They"
  included `decklist` as well as `agent` - the route description only named
  "agent settings and seat liveness". Deleting `decklist` outright would
  have broken that test and silently reintroduced the historical bug its
  own docstring describes (every dispatch logged `deck=unknown-deck`, no
  matchup guide ever loaded) - proven by literally reverting
  `seat_decklist` to return `""` and watching
  `test_dispatch_identifies_the_deck_and_reports_it` fail with exactly that
  symptom. Resolution: `decklist` also moved to
  `GET /api/lobby/games/{id}/seats`, but redacted per-viewer by an optional
  `uid` query parameter the same way `visibility._redact_player` redacted it
  on the old route (present only on the entry whose own uid the caller
  presents) - an unredacted route would have handed any caller who knows a
  game id (public, via `list_lobby_games`) both players' full decklists,
  a CR 401.2 violation `/ai-seat/status`'s unredacted agent-settings
  exposure never was. `mtg_agent/driver/app.py`'s `seat_decklist` (not in
  T8's own file list, but genuinely broken by the S6 change with no other
  fix available) now calls this route instead of reading the state view,
  best-effort - a failure to reach it answers `""` rather than aborting the
  seat's own dispatch, since guide selection is not worth making the actual
  assignment newly fragile over. `admin_key` gets no special access on this
  new route (no current consumer needs it); `visibility.py` itself stays
  untouched, per the task's own note - its `decklist`-specific redaction is
  simply never triggered from the state view any more.

- **2026-09-08 - 2b T8: `response_model_exclude_unset=True` bit a second
  time, this time on `AgentSettings` embedded in a *new* response model.**
  `decks_api.SeatDetail.agent` handed FastAPI the raw `AgentSettings`
  instance straight out of `store.agent_settings()` rather than a plain
  dict; because that instance is already the declared type, FastAPI
  serializes it as-is rather than reconstructing one from a dict, so its
  `model_fields_set` stayed whatever `AgentSettings.default()`'s
  constructor happened to set explicitly (`model` only) - `effort` and
  `max_thinking_tokens` silently vanished from the wire even though both
  were genuinely present values, not really-unset ones. Same root cause as
  T1's `MEMORY.md` entry on this file, different route. Fixed the way
  `_player_view` used to get it for free: `.model_dump()` the instance into
  a plain dict with every key present before returning it, forcing FastAPI
  to validate a fresh instance from that dict and mark all three fields
  set. `test_agent_settings_default_and_show_on_the_player` pins it (proven
  by reverting to the bare instance and watching `effort`/
  `max_thinking_tokens` disappear).

- **2026-09-08 - 2b T8: `list_lobby_games`'s S2 fix (`GET
  /api/games/summary`) intentionally does not mirror the old
  `LobbyGameSummary` wire field-for-field.** The plan named the new
  endpoint's own scalars exactly ("id, is_over, turn number, active player,
  phase, seat count") - a generalised, reusable api-owned contract, not a
  copy of the four ad hoc fields `list_lobby_games` used to extract
  (`turn_number`, `step`, `winner`, `drawn`). `LobbyGameSummary` changed to
  match what is actually available: `phase` (coarser than `step`) and
  `is_over` (bool, not a winner's name) - `winner`/`drawn` were never part
  of the N+1 anyway (already free reads off the locally-fetched `Game`), so
  dropping the winner's *name* from the lobby list is a real, deliberate
  narrowing this task accepted rather than an accident; nothing in `ui/`
  displayed it beyond the boolean "over" state. `ui/lobby_model.js`,
  `ui/render/lobby.js`, and their Node tests moved with it.

- **2026-09-08 - 2b T11: `mcp.py --transport` defaults to `http`, which
  silently broke the working docker-compose continuous-agent flow until
  `deploy/mcp.base.json` was pinned to `--transport stdio` explicitly.**
  That file spawns `mcp.py` as a subprocess via `command`/`args` (not a
  `url`) - the wrapper's own MCP client talks to the child over its stdin/
  stdout, which is exactly what the old, unconditional `server.run("stdio")`
  gave it for free. Flipping the default to `http` without also fixing this
  config would have made the compose stack spawn an http-serving process
  that never speaks a word of MCP over the pipe the wrapper is reading -
  caught by adding `tests/driver/test_compose.py::
  test_the_base_mcp_pins_the_mtg_server_to_the_stdio_transport` before
  making the change (confirmed red: the args list carried no `--transport`
  at all), not discovered by running the stack. `deploy/seat-job.yaml`
  needed no equivalent fix - its `mcp-config` initContainer already renders
  an http-`type` client config pointing at `MCP_URL` (empty in production
  today per the T9 note above), already anticipating exactly this shape.

- **2026-09-08 - 2b T11: the api's own `/api/health` used to source its
  `protocol_version` from an import of `seatkit.PROTOCOL_VERSION` - the
  only import left pointing from the api toward the future
  `magic-cards-mcp` package, backwards.** A new `protocol.py` is the api's
  own canonical copy; `seatkit.py` keeps an independent literal (it cannot
  import this package once the repos split) for its own `api_trouble`
  staleness check, which was never the api-import's job to begin with - it
  already compared the health response against its own local constant.
  The two are now kept equal by hand, the same way `agent_settings.py`'s
  cross-repo spellings already are; `tests/test_protocol_boundary.py` pins
  both the severed import (`app` module has no `seatkit` name at all any
  more) and the two constants' equality.

- **2026-09-08 - 2b T11: `Seats.seat_for`'s new `resolve_by_uid=True` path
  only fires for the http transport with no `--game` pinned, never as a
  general improvement to the pinned/stdio path.** It would have been a
  strictly more capable replacement for `game_id()`'s "the one live game"
  auto-discovery (`GET /api/seats/{uid}` finds any uid's own game with no
  guessing at all), but the plan scopes it to http mode specifically, and
  the existing stdio auto-discovery semantics (`test_mcp_resilience.py`'s
  restart/cache-drop behaviour) are left untouched rather than risking a
  behavioural change nobody asked for in this task.

- **2026-09-08 - 2b T11: porting the ~28 test files that read
  `store.seat_uids` straight out of the api singleton is T12's job, not
  T11's - checked against the plan rather than assumed.** T11 only had to
  add the seat-lookup capability `mcp.py`'s http transport now uses
  (`Seats._seat_for_any_game`, on top of T8's `GET /api/seats/{uid}`); T12
  ("reorganise the suite by future owner, and prove the out-of-process
  harness") is explicitly where those files move onto a `live_api` fixture
  and the `store.seat_uids` back door is deleted from them. Left untouched
  here on purpose.
- **2026-09-08 - 2b T6: the reservation ledger behind `reserve_slot`/
  `attach`/`release` (M8) is process-local in *both* implementations,
  `PostgresRepository` included - not yet T17's durable, cross-replica
  `lobby_games` row.** The plan's own text (`docs/2b-plan.md:794`, "the
  reservation from T6 becomes the single atomic INSERT described in M8" at
  T17) says this stays a precursor shape until the lobby has its own
  database; there is no `lobby_games` table pre-cut for a real cross-process
  reservation to live in. Reusing the precedent `_locks`/`_automatic_pregame`
  already set on `PostgresRepository` (process-local, correct only because
  `replicas: 1` holds for this whole phase per M9) kept the two
  implementations symmetric and let `reserve_slot`'s cap decision become a
  plain `threading.Lock` instead of `create_if_under`'s `LOCK TABLE ... SHARE
  ROW EXCLUSIVE` - a real DB-level lock was only ever needed to serialise
  concurrent *processes*, which this phase does not have.
- **2026-09-08 - 2b T6: a stale reservation (the fourth concurrency case the
  task asked to be answered explicitly) is closed two ways, not one.** A
  process that dies mid-request can never call `attach`/`release` again, but
  since the ledger is process-local (see above), that process's own restart
  wipes every reservation it held anyway - nothing survives to leak. What
  does need an explicit answer is a reservation that outlives its request
  *without* the process dying (a caller bug that never reaches its
  `finally`): `RESERVATION_TTL_SECONDS` (default 30s, an instance attribute
  so a test can shrink it without the contract suite's zero-argument
  factories needing a constructor parameter) sweeps it on the next
  `reserve_slot`/`attach`/`release` call, no timer thread needed.
- **2026-09-08 - 2b T6: "calls the api client to create the game" (M8's own
  wording) resolved to `store.create()` directly, not a new method on
  `api_client.ApiClient`.** The task's file list omits `api_client.py`, and
  M8 itself says "the api half keeps a plain `create`" - `ApiClient` (T5) is
  scoped to deck validation and the batch game-summary read; giving it a
  create-game method is stage B's (the actual HTTP extraction) job, not
  T6's, while the lobby and api are still one process.
- **2026-09-08 - `ccw_inference_seconds`: claude's own JSONL transcript lines
  carry a `timestamp` (ISO 8601 ms) and `requestId`, confirmed against a real
  session file rather than assumed.** One inference is one `requestId`,
  flushed as several content-block lines (a `thinking` line, then a
  `tool_use`/`text` line) that all carry the same final `usage.output_tokens`
  - so both duration and output tokens are observable at the same point,
  from the transcript's own timestamps rather than local read time, making
  the measurement exact regardless of the watcher's poll interval.
  `activity.InferenceTimer` times a group from the line immediately before
  its first block to its own last block, and only finalizes once a *later*
  line proves the group has no more blocks coming - so the one inference
  still in flight when the transcript stops growing (match end) is never
  recorded. Also: `ActivityWatcher.start()` used to skip spawning the thread
  entirely when `AGENT_LOG_LEVEL` was `off`/`turns` (`enabled()`); metrics
  must not depend on log verbosity, so `start()` no longer gates on it -
  `rank` still gates the text log inside `poll_once`, but the watcher itself
  (and `ccw_inference_seconds`) always runs.
- **2026-09-08 - the eager-load key is `alwaysLoad`, and the other spelling
  is a decoy.** Claude Code defers every MCP server's tools behind its
  `ToolSearch` tool by default; the per-server opt-out is `alwaysLoad: true`,
  a plain sibling of `command`/`url` on the entry inside `mcpServers`
  (code.claude.com/docs/en/mcp, "Exempt a server from deferral"; confirmed
  again against the installed CLI's own config schema). `anthropic/alwaysLoad`
  is a *different thing that looks like the same thing*: an MCP `_meta` key a
  **server** puts on an individual tool in its `tools/list` answer. Written
  into a config file it does nothing at all, silently. Both
  `deploy/seat-job.yaml`'s mcp-config branches and `deploy/mcp.base.json`
  carry the real one, and `tests/test_k8s_seats.py` executes the initContainer
  script and asserts on the JSON it wrote rather than on the template text -
  the only thing that would have caught the wrong spelling. `spellslinger` is
  left deferred on purpose: large, occasional reference surface, not the
  seat's per-turn toolset.
- **2026-09-08 - a wake reason that means "you can do nothing" is a spin
  loop, not a wake.** `compute_wake_reason` returned "the game has not started
  yet: another seat owes a pregame declaration" as a *reason*, and
  `block_until_wake` returns the instant a reason is non-None - so
  `max_seconds` never applied in the pregame at all. A live seat pod
  (`mtg-seat-3b6613e0`) made 55 `wait_for_priority` calls in one agent
  activation, 53 of them back to back, each returning in ~70 ms with a
  byte-identical 9.5 KB board, one full inference cycle every 3.2 seconds for
  as long as its human opponent thought about a mulligan. It now returns
  `None` there (CR 103.5: each player declares in turn order and nothing
  happens until all have; CR 103.6 does not begin until that process is
  complete - both read in full in the session), so the bounded wait binds. The
  seat the game is actually waiting on is still woken at once, and there is a
  test pinning that half specifically: it is the half whose regression would
  hang the whole game.
- **2026-09-08 - the answer a seat gets carries what happened, not just what
  is.** Every state-bearing answer already carried `available_actions` and
  none carried a line of history, so `prompt.py` told the agent in as many
  words to read `my_state` and then `game_log` - two calls and two inference
  cycles for one question. `recent_log` now rides along on `run_action`,
  `pass_until`, `block_until_wake` and `my_state` (`seatkit.RECENT_LOG_ENTRIES`
  entries, `omitted` counting what the tail dropped). It carries
  `LOG_HEADLINE_FIELDS` rather than whole rows *not* to save tokens - payload
  size buys no latency here, corr(latency, context) = -0.101 - but because
  `audit.record` stamps every row with a full board `snapshot` and a
  `legal_actions_before` pool, and fifteen of each bolted onto an answer that
  already carries the current one of each is not a log tail. `game_log` still
  serves whole rows, and gained the `since` the api's own `GET .../log` always
  had.

- **2026-09-08 - `pass_priority` is exempt from the reasoning gate, and the
  exemption is at the gate rather than in the tool signature.** Measured on
  the live database: 149 rows, 92 carrying reasoning, but only 43 distinct
  strings - and 75 of those 92 (82%) were `pass_priority`, averaging 80
  characters, with one string appearing 38 times because
  `seatkit.pass_until` threads a single reason onto every pass a run makes.
  A search of the whole corpus for a genuine opponent read returned zero.
  Requiring a bigger `reasoning` field would have made the same non-answer
  longer at roughly +28.6s per game turn. `require_reasoning` grew a
  `pass_shaped` keyword and `app.py`'s `action_pass_priority` passes it;
  `PassPriorityRequest.reasoning` is still accepted and still audited, so
  the pass that *does* mean something ("holding up my counterspell") can
  still say so.

- **2026-09-08 - the agent's thinking cannot be harvested: the transcript
  keeps signatures and zero characters of text.** Verified on a live pod and
  independently on a second machine - 36 `thinking` blocks, 0 chars between
  them. `wrapper/activity.py`'s comment claiming they are skipped because
  they are *long* was written against a premise that no longer holds and has
  been corrected. Anything that wants an agent's reasoning has to ask the
  agent (`POST .../plan`, and the per-action `reasoning` gate), never mine a
  transcript for it.

- **2026-09-08 - the plan gate's ledger lookup is bounded on purpose, and
  keyed on `turns_taken` rather than on the audit row's `turn`.**
  `records.require_plan` runs on the hot action path, so it reads at most
  `RECENT_RECORDS_WINDOW` (500) of the newest rows through the new
  `GameRepository.recent_records` - postgres serves that with `ORDER BY seq
  DESC LIMIT`, already indexed by `uq_records_game_id_seq` on `(game_id,
  seq)`, so no new index was needed. A full `store.records()` load would
  have got slower every turn of a long game, and an in-process "who has
  declared" cache would answer differently per replica. The window is safe
  because a plan for the *current* turn is always among the newest rows.
  The key matters too: `TurnMachine.turn_number` counts rounds of the table,
  so both seats share it in a round and an extra turn (CR 500.7) does not
  move it at all; `turns_taken` goes up exactly once per turn that begins,
  which is the identity a per-turn plan needs. `audit.record` stamps `turn`
  (the round) on every row regardless, so the declaration writes
  `turns_taken` into its own `detail`.

- **2026-09-08 - `delete()` is a soft delete now (`games.archived_at`,
  revision `a2f6d9c31b57`), and every other accessor pretends nothing
  changed.** The owner's requirement was "Games must be persisting in the
  database even after are stopped/finished/conceded". `records` already
  outlived its game (no foreign key, revision `8f1c47a0b6d2`), so what
  survived a delete was the reasoning and what did not was the board it
  reasoned about. `delete` now stamps `archived_at` and clears
  `command`/`tape`/`pending`; `get`/`ids`/`ui_games`/`live_ui_games`/`locked`
  /`seat_uids`/`admin_key` all filter on `archived_at IS NULL`, so an ended
  game is exactly as unplayable as a removed one was, and the whole existing
  contract suite passes unmodified. `archived()` (returning `ArchivedGame`)
  is the single accessor that looks past the filter, and only the review
  route uses it. `InMemoryRepository` keeps the parity by still popping every
  live collection and moving the four retained facts into its own
  `_archived` dict, rather than teaching twenty accessors about a flag.
  Worth knowing: `k8s_seats`' reconcile uses `store.ids()` as "which games
  still exist" to garbage-collect agent Jobs - had `ids()` started including
  archived rows, every ended game would have kept its Job alive forever.

- **2026-09-08 - `GET .../review` deliberately does NOT use
  `game_or_404`, and deliberately does NOT use
  `visibility.resolve_viewer`.** The first is what stranded 144 rows of the
  live deployment's only substantively played game: the ledger outlives the
  game, but `GET .../log` checks the game exists before reading it. The
  second is subtler - `resolve_viewer(game_id, None)` returns `None`, which
  is a viewer in its own right (the "debugger's eye" the log route documents
  and `redact_log_entry` only partly guards). That is the wrong primitive
  for a credential check, so the review route compares the presented
  `uid`/`key` against the *archived* row's own `admin_key` and `seat_uids`
  and 404s otherwise, then refuses with 409 until the game is over
  (`Game.is_over`, CR 104.1 - looked up in session: "A game ends immediately
  when a player wins, when the game is a draw, or when the game is
  restarted") or has been ended.

- **2026-09-08 - PRE-EXISTING, NOT FIXED HERE: `GET /api/games/{id}/log`
  with no `uid` returns both players' full hands on a live game.**
  `visibility.redact_log_entry` applies its snapshot filtering only `if
  isinstance(viewer, str)`, and `resolve_viewer(game_id, None)` returns
  `None`, which is not a `str` - so a caller presenting nothing gets the
  unredacted snapshot. `app.py`'s own docstring calls this the debugger's
  eye and means it, but `GET /api/lobby/games` publishes every lobby game's
  id with no credential, so "nobody knows the id" is not the protection it
  reads as. Left alone deliberately (a separate defect, and fixing it here
  would have tangled this diff); the review route was built not to inherit
  it. What *was* tightened, because it is the same door the new plan data
  would have walked out of: `reasoning` and a `plan_declared` row's whole
  detail are now dropped for every viewer that is not that row's own actor
  or the admin key.

- **2026-09-08 - the review's board has to be parsed back out of strings, and
  loses two relationships doing it.** `serialize.snapshot` writes each
  permanent as one line ("alice: Grizzly Bears (tapped) (sick) [4/5] dmg:2
  (+1/+1=2)") and hands, graveyards and exile as bare card names - it is the
  audit log's compact frame, not the `state_view` card objects. So
  `ui/review_model.js` parses those lines back into card-shaped objects
  (`parsePermanent`) and joins the printings in by name from `GET
  /api/cards/{name}`, which is also the only way the review board can put
  lands in the land row at all (`board_layout.isLandRow` reads a `type_line`,
  and the snapshot carries none). Two things the snapshot simply does not
  record and the review therefore cannot draw: which permanent an Aura or
  Equipment is attached to, and which card was exiled with which permanent -
  so a review board never tucks a card under another one where the live board
  does. Also worth knowing: a `cast` row's `detail["card"]` is whatever the
  client sent, so a client that cast by `card_id` leaves a uuid there; the
  page drops uuids rather than showing them.

- **2026-09-08 - the lobby's End no longer drops the game from
  `mcm.games.v1`.** `DELETE /api/lobby/games/{id}` is a soft delete
  (`games.archived_at`) whose whole purpose is that a finished game stays
  reviewable, and `GET .../review` needs a seat uid that only the browser
  holds - dropping the entry made the server's archive permanently
  unreachable. End now stamps the entry `endedAt` (keeping `uid`, and the
  opponent's name, since an archived game is filtered out of `GET
  /api/lobby/games` and the server will never name it again), the row renders
  as "ended" with Show thinking and Forget, and Forget is what actually drops
  it.

- **2026-09-08 - the AI pill's activity limit is 300s, and 45s was measured
  wrong before it shipped.** The pill now says what the agent is doing
  (`activity.state_for` in the wrapper -> the driver's `agentActivity` ->
  the seat status -> `aiSeatActivityLabel` in `ui/store.js`), and retires an
  observation it can no longer stand behind. The first cut set that limit to
  `AI_STALL_SECONDS` (45s) on the reasoning that the pill should not
  contradict the stall banner. Replaying 7 real seat transcripts (65 minutes
  of play, from `~/.claude/projects/-Users-szymonri-Documents-magic-cards-
  model-api-players-*`) through the shipped code showed that was wrong twice
  over: `wait_for_priority` legitimately blocks with **nothing** written to
  the transcript for a median 28s and a p90 of 110s, so a 45s limit retired
  a live, correct "waiting for priority" mid-wait and blanked 49% of a 5s
  poll's samples back to the flat "thinking" the feature exists to remove -
  and the pill does not contradict the banner anyway, it explains it. At
  300s the only thing that goes stale in that data is one genuinely
  abandoned 30-minute silence; every real in-play gap is under 2 minutes.
  The wider lesson: this label's tuning constants are measurable off real
  transcripts, and guessing them from the shape of the UI got it backwards.

- **2026-09-08 - the activity label leaks nothing by construction, not by
  filtering.** The opposing human reads that pill mid-game, so a card name or
  a line of the agent's prose in it would reopen exactly what `visibility.py`
  had to be changed to close. `activity.observations` is a second, separate
  parser beside `activity.events` for this reason alone: it yields
  `(kind, tool name)` and never touches a tool's input or a text block's
  body, so there is no channel for content to travel down - and the UI's
  label comes from a fixed table keyed by the state word. Do not "simplify"
  the two parsers into one that carries text and filters it later.
- **2026-09-08 - the graveyards and exiles are a third column, and the live
  board is the only place they fit.** `#zone-ribbon` sits between the
  battlefield and the control ribbon, four rows top to bottom: the opponent's
  graveyard, the opponent's exile, your exile, your graveyard - the
  battlefield's own mirroring applied to the zones, decided by
  `board_layout.js`'s `zoneRibbon` and not by the CSS. Two things worth
  knowing. Exile is one shared list on the wire, so the two exile rows are
  that list split by `controller || owner` (the same fallback `serialize.py`
  uses to decide who could cast a card sitting there), and a card it cannot
  attribute is drawn on the far side rather than dropped. And the review page
  deliberately did *not* get the ribbon: `serialize.snapshot` writes exile as
  bare card names with no owner, so a review frame cannot honestly split it
  per seat - it keeps the old horizontal `.exile-row` strip, which is also why
  that CSS block is still here. Narrowing the board for the column exposed one
  real bug: `.lands-row` was `flex: 0 1 auto` and shrank, slicing the bottom
  off anything tucked under a land (a Utopia Sprawl under its Forest). It no
  longer shrinks; `#mtg-board` scrolls instead.
- **2026-09-08 - `my_state` is now `battlefield_state`, removed rather than
  aliased, and `PROTOCOL_VERSION` is `seatkit-7`.** The tool answers the same
  whole board when called with a uid alone; two options narrow it -
  `sections` (a comma-separated list of the state view's own top-level keys:
  `battlefield`, `hands`, `graveyards`, `exile` (which carries `exile_pile`),
  `command`, `stack`, `combat`, `players`, `play_permissions`, `pregame`,
  `pending_decision`, `recent_log`) and `side` (`both`/`mine`/`opponent`,
  resolved from the caller's own uid, never a username - a mirror match is
  where a username-shaped option breaks). Three decisions worth keeping.
  (1) The section names are the payload's own keys on purpose: a prettier
  parallel vocabulary would be a second thing to learn and a second thing to
  drift. (2) No deprecation alias. An MCP process is spawned once and never
  reloads its code (`mcp.py`'s docstring), so a stale one serves `my_state`
  whatever this repo does; an alias helps neither direction of skew, and the
  `seatkit-7` bump is what turns "new prompt, old MCP server" into the
  PROTOCOL MISMATCH sentence naming the stale side instead of a bare "Unknown
  tool". (3) Do not sell this as a latency win. Measured on this machine,
  per-inference latency fits `0.78s + output_tokens/84` and correlates with
  context size at -0.101 - prefill caches, so a shorter answer buys nothing.
  What it does buy is fewer round trips when one narrowed call replaces two,
  and slower growth toward the ~185k-token compaction cliff a three-turn
  session hit. The one mechanical saving is real but small: `recent_log` is
  its own HTTP call (`Seat.recent_log`), and narrowing it away skips it.
  Filtering happens on the already-redacted view the API returns
  (`visibility.redact_view` runs server-side), so narrowing can only remove -
  `tests/test_battlefield_state.py` proves it rather than arguing it, by
  asserting every focused answer's strings are a subset of the unfocused
  one's.

- **2026-09-09 - evoke was never missing from the API, only from the cheap
  half of it, and the fix is the state view rather than the affordance.** The
  owner's report ("I'm not able to evoke through webUI ... I tried to evoke
  Wistfulness") looked like an unimplemented feature and was not one. Checked
  live against the in-process client before touching anything: `GET
  .../affordances/{card_id}` already listed Wistfulness's evoke under
  `alternative_costs` (`AlternativeCostView`, source `keyword`, rule
  CR 702.74a, cost `{G/U}{G/U}`), `GET .../legal-actions` already enumerated
  a second `cast` action for it carrying `keyword: "Evoke"` and
  `alternative_cost: "Evoke"`, and `POST .../actions/cast` already accepted
  both spellings and paid `{G/U}{G/U}`. What was missing was any signal on
  the *board poll*: `serialize._cast_options_of` opened with `if state.zone
  not in (Zone.GRAVEYARD, Zone.EXILE): return []`, so a client rendering from
  `GET /api/games/{id}` had nothing to badge a hand card with. Extending that
  function to `Zone.HAND` was chosen over publishing `alternative_costs` on
  every hand card for one reason: `affordances.alternative_costs` builds a
  `Casting`, which needs `engine_api.dry_run_copy(game)` - a whole-game deep
  copy - *per card*, and the affordance endpoint's own comment already says
  that is "per-card work not worth doing for every card on the board on every
  poll". `_cast_options_of` is pure (`castable_from` + `marker_cast_options` +
  `granted_cast_options`, no copy, no `Casting`), so it is what belongs in the
  1.2s poll; the affordance stays the rich on-demand answer, and the two agree
  because both read the same engine functions. One consequence worth knowing:
  `cast_options[i].usable_now` answers `KeywordCastOption.why_not` only, which
  never asks CR 117.1a's timing - a creature's evoke option reads
  `usable_now: true` on an opponent's turn, exactly as a graveyard flashback
  option always has. `legal-actions` is the timing-correct surface.

- **2026-09-09 - a hand card's `cast_options` needs no `_redact_cast_options`
  pass, and adding one would be dead code.** `visibility.redact_view` replaces
  every `hands` list that is not the viewer's own with `[]` - card, field and
  all - so CR 402.3 is answered by dropping the whole card rather than one of
  its keys, and a viewer with no uid matches no player name and reads every
  hand empty. Pinned in `tests/test_hand_cast_options.py` by serialising bob's
  and an anonymous viewer's entire response bodies to JSON and asserting the
  string "Evoke" appears nowhere in either, which catches a leak through any
  nested key rather than only through the one the assertion was written for.
  Separately noted and deliberately not fixed: the harness endpoints
  (`POST .../cards`, `.../tap`, `PATCH .../cards/{id}`, `PUT .../step`,
  `POST .../state-based-actions`) return `state_view` with no redaction at
  all, so they already hand back both hands by name - adding one field to a
  view that already carries every card's identity changes nothing there.

- **2026-09-09 - interacting with the board does not change the board's
  geometry, and that is now enforced in two places.** The reported symptom
  was "the webUI is getting ugly with clicks": choices rendered under the
  cards, and the casting-method dropdown grew the page a vertical scrollbar.
  Both were one property being violated. (1) `#casting-strip` was a row of
  `#mtg-board`, between `#my-lands` and `#my-hand`, so beginning a cast
  inserted a block into the board's own flex column and every card above it
  moved; it is a panel in `#side-panel` now, directly under `#prompt-box`.
  (2) `.popover` was `position: absolute` at the anchor's *document*
  coordinates with no bound - and every card in the hand row sits at the
  bottom of the window, so its menu was placed below the bottom of the
  window and the document stretched to contain it. It is `position: fixed`
  with `ui/menus.js`'s `anchoredPosition` clamping it into the viewport and
  capping `max-height`. Measured on a real 26-permanent board at 1440x900,
  1000x800, 1440x520, 1440x260 and 1440x150: `documentElement.scrollHeight`
  is identical before and after opening any menu, and no board row's
  `getBoundingClientRect` moves by a pixel.
- **2026-09-09 - the two surfaces are split by scope, not by convenience.**
  A choice *about a card* (which way to cast it, which ability, which colour
  a land taps for, which player an attacker is aimed at) is an anchored menu
  over that card. The in-flight announcement reads card-scoped but
  cannot be one: it stays open while the player clicks *other* cards to fill
  its target slots, and an anchored menu closes on the first click-away - so
  it is the ribbon panel. Turn-scoped questions (a pending decision, CR
  103.5/103.6's pregame, the combat prompt) stay in `#prompt-box`.
  `ui/menus.js`'s `surfaceForChoice`/`choiceForPlan` are that rule as pure
  functions, tested directly.
- **2026-09-09 - two ugliness fixes that only a browser pass could find.**
  With the strip moved next to `#prompt-box`, the prompt box's own in-flight
  summary printed the same title and the same Cancel an inch above the panel
  saying it again - `#prompt-box` now hides entirely while an announcement is
  in flight (`tests/test_ui_ribbon.py`'s Kozilek scenario asserts it). And
  the ability menu on Walking Ballista read "Remove a +1/+1 counter from this
  creature: It deals 1 damage to any target." with "Remove a +1/+1 counter
  from this creature" repeated in dim type below it: a printed activated
  ability is "cost: effect", so `abilityMenuItems` drops the cost sublabel
  when the text already opens with it.
- **2026-09-09 - `#prompt-box`'s and `#priority-box`'s border colours are
  dead rules, and have been.** `#side-panel > div` (`style.css`) is an id
  plus a type selector, so it outranks the bare `#prompt-box { border-color:
  var(--warn) }` and `#priority-box { border-color: var(--accent) }` below
  it: measured with `getComputedStyle` on a live board, both render `--line`
  like every other panel. Found while giving the relocated `#casting-strip`
  its accent border back (written `#side-panel > #casting-strip` so it
  actually wins). Not fixed for the other two on purpose - reviving them is a
  visible restyling of panels that change was not about - but somebody
  should decide whether those two colours were meant to be there.
- **2026-09-09 - Goblin Bombardment's gap ("sacrifice needs 1 card(s) chosen,
  0 given", unreachable) was two front ends missing a field the engine layer
  already carried, not an engine gap.** `schemas.ActivateRequest` already had
  `sacrificing`/`discarding` and `engine_api.activate_ability` already
  threaded them into `Game.begin_activating` - the field simply had no way in
  from `seat_cli.py` (no `--sacrificing` flag) or `mcp.py` (no `sacrificing`
  parameter), so a real seat could never set it. Fixed by widening the shape
  rather than special-casing sacrifice: `sacrificing`/`discarding` are now
  `list[UUID] | None` (a cost can ask for more than one object - "Sacrifice
  two creatures" - and the engine already consumes a pool of however many
  were named, `Game._choice_cost`), and `exiling: list[UUID] | None` was
  added alongside them for Back from the Brink's own shape
  (`Game.activation_cost`'s docstring names it). `exiling` only reaches a
  *battlefield* activation: `machines.keywords.off_zone_activation` takes no
  `exiling=` kwarg at all, so `engine_api._activate_off_zone_ability` refuses
  it by name up front rather than letting it hit a bare `TypeError` on an
  unexpected keyword. `PROTOCOL_VERSION` bumped to `seatkit-8` (a new
  parameter on an existing, widely-used tool is a request-shape change per
  that constant's own rule) - `protocol.py`, `seatkit.py`, `contract/
  tools.json` (`mise run tools:export`), `openapi.json` (`mise run
  openapi:dump`) and `src/mtg_agent/driver/prompt.py` all needed the matching
  update, and `tests/test_openapi_drift.py`/`tests/test_prompt_matches_tools.
  py` are what catch a repeat of any one being forgotten. The play UI
  (`ui/actions.js`'s `bodyFor`) has the identical hole - never builds
  `sacrificing`/`discarding`/`exiling` into an `activate` request body at all
  - left unfixed on purpose: two other unmerged branches are already reworking
  the choice-presentation layer, and a third touching the same file would
  tangle the merge.
- **2026-09-09 - merging the hand-cast-options/UI-rework stack with the
  activate-sacrifice-cost stack: `PROTOCOL_VERSION` goes to `seatkit-9`, not
  a straight take of either side's `seatkit-8`.** The hand-cast-options
  branch (`3eadbd9`) never touched `protocol.py`/`seatkit.py` at all - its
  own commit message reasoned `contract/tools.json` stayed byte-identical so
  no bump was needed. That reasoning does not hold up against this file's
  own precedent: `seatkit-5`'s `recent_log` was the same shape of change
  (an additive field on an existing state view) and *was* bumped, on exactly
  the "a stale MCP process reads this as nothing having happened" logic that
  applies here too - a stale process reads a hand as carrying no
  alternative-cost options rather than a hand missing a field it cannot ask
  for. So the merged tree bumps to `seatkit-9`, one above the higher side's
  `seatkit-8`, with a changelog entry (`seatkit.py`) naming both shape
  changes it now covers. `protocol.py`, `seatkit.py`, `contract/tools.json`
  (`mise run tools:export`) and `openapi.json` (`mise run openapi:dump`) all
  needed the matching regeneration; `src/mtg_agent/driver/prompt.py` needed
  no wording change since its evoke/cast-option and sacrifice/discard/exile
  prose already independently describe the two features correctly.
- **2026-09-09 - Fiend Artisan's X gap was the same shape as Goblin
  Bombardment's, one layer over: `cast` carried `x_value` on every surface,
  `activate` carried it nowhere, and the missing case was silent rather than
  refused.** `ActivateRequest` had no `x_value` field at all (unlike
  `sacrificing`/`discarding`/`exiling`, which the engine layer already
  threaded through before their seat surfaces existed) - `Game.
  begin_activating`/`Game.activate_ability` both already accepted
  `x_value: int = 0` and fold it into the ability's own mana cost via
  `ManaCost.with_x`, so this was never an engine gap either, only a request
  shape this repo had not built yet. Added `ActivateRequest.x_value`,
  threaded it through `engine_api.activate_ability` into both engine calls
  (the same value to both, per `begin_activating`'s own docstring), and gave
  `seat_cli.py activate` a `--x` flag and `mcp.py`'s `activate` tool an
  `x_value` parameter, the same shape `seatkit-8` already added for
  sacrifice/discard/exile. **Also made the missing case loud**: activating an
  ability whose own cost carries `{X}` with no `x_value` named is now refused
  with the engine's own reason (`"{X}" in ability.cost`, the same check
  `affordances._needs_x`/`AbilityView.needs_x` already report to a seat ahead
  of time) rather than silently running at X=0 - the reported seat mistook
  this silent zero for its own misplay, fetching only Dryad Arbor (mana value
  0) with Fiend Artisan's tutor instead of the bigger creature it actually
  asked for. Off-battlefield activation gets the same treatment `exiling`
  already has: `machines.keywords.off_zone_activation` takes no `x_value=` at
  all, so `engine_api._activate_off_zone_ability` refuses it by name rather
  than a bare `TypeError`. `PROTOCOL_VERSION` bumped to `seatkit-11`, not
  `seatkit-10` - two other unmerged branches had already taken `seatkit-9`
  and `seatkit-10`, so this skips past both rather than colliding with either
  once they land; `protocol.py`, `seatkit.py`, `contract/tools.json`
  (`mise run tools:export`), `openapi.json` (`mise run openapi:dump`) and
  `src/mtg_agent/driver/prompt.py` all needed the matching update.
- **2026-09-09 - a second, unrelated X defect in the same session: `cast`
  read a non-modal spell's X too late for its own targeting to see it.**
  Reported as Postmortem Lunge (`{X}{B/P}`, "Return target creature card with
  mana value X ...") refusing every target above mana value 0 no matter what
  `x_value` was sent. `engine_api._drive_cast` only called `Casting.
  choose_modes(req.modes, x_value=req.x_value)` `if req.modes:` - the one
  call that sets `Casting.x_value` - so a non-modal spell, which has no modes
  to give, never had `x_value` set at all until `determine_cost` ran, several
  lines *after* `choose_targets`. `Casting.targeting`'s own docstring already
  says outright that a non-modal `{X}` spell's text is read "with X already
  substituted... X is announced before targets, so it is always known here"
  (CR 107.3a/601.2b/601.2c) - true of the engine when driven in that order,
  false of how this repo was driving it. Not an engine gap: `../magic-cards-
  model`'s own `tests/scenarios/test_postmortem_lunge.py` already passed,
  because it calls `choose_modes`/`determine_cost` with `x_value` before
  `choose_targets` directly. Fixed with one added branch - `elif req.x_value
  is not None: casting.choose_modes([], x_value=req.x_value)` - calling the
  same announcement a modal spell already uses, with an empty mode list,
  purely to fix `x_value` before targets are chosen; `determine_cost`
  downstream is untouched and still receives the same value. No request/
  response shape changed (`CastRequest.x_value` already existed and was
  already reaching this function; only the order it was consumed in was
  wrong), so no `PROTOCOL_VERSION` bump - this is a bug fix, not a contract
  change. A modal spell's own path (`if req.modes:`) is untouched; the new
  branch only fires when there were no modes to announce, which is also why
  `test_modal_x_cast.py`/`test_kozilek_command_ui_flow.py` needed no changes
  to stay green. An activated ability never showed this symptom -
  `Game.activate_ability` sets `x_value` and `targets` in one call, so there
  is no two-phase window for the order to matter; the window exists only
  because a `Casting` is built up over several separate calls.

- **2026-09-09 - An undecodable game counts against `MAX_LIVE_GAMES`, and End
  game is what makes that safe.** The engine's `Game.CURRENT_SCHEMA_VERSION`
  moved to 5 while production postgres still held rows written at 3;
  `Game._check_schema_version` refused them with `SchemaVersionMismatch` (a
  `RuntimeError`, deliberately, so pydantic will not rewrap it), and nothing
  caught it. Because `_live_ui_game_ids` and every listing route decoded
  *every* matching row unconditionally, two stale rows 500'd `GET /api/games`,
  `GET /api/lobby/games` and - through `reserve_slot`'s live count - `POST
  /api/lobby/games` as well, for every game including the ones that decoded
  fine. The fix is resilience, not data: `PostgresRepository._game_from_row`
  is the one decode point and now raises `repository.GameUnloadable` carrying
  the decoder's own message; batch reads catch it per row and list the game
  with `unloadable` set; a single-game read lets it reach `app._game_unloadable`
  (409, the engine's words verbatim, `unloadable: true`). No migration, no
  version-tolerant decoder, no archiving of the stale rows.

  The deliberate half is the cap. `_live_ui_game_ids` **counts** a live row it
  cannot decode: `is_over` (CR 104.1) is a fact about a board this build
  cannot read, and calling an unreadable game finished would be the same
  silent default the engine refuses to make when it declines the snapshot -
  and it would let the cap be exceeded with no signal. Counting it risks the
  opposite failure (a lobby wedged at the cap by rows nobody can open), which
  is why `end_lobby_game`/`lobby.reset_game`/`audit.record` were all made to
  tolerate the refusal: End game needs no board at all (the credential check
  reads the seats and admin-key columns; the delete is an `UPDATE`), so the
  slot is always recoverable by the one deliberate act that always was the way
  to recover it. Get this pair wrong in either direction and you either block
  new games forever or silently blow the cap.

  Two smaller notes from the same pass. The failure is cached beside the
  identity-preserving read cache (`_undecodable`, keyed on `version`), so a
  ~180KB payload is not re-parsed on every 1.2s poll and the WARNING fires
  once per (game, version) rather than once per read. And `store.known_players`
  exists because every listing route has to name a game's players without the
  `Game` - the `seats` and `decklists` columns need no decoding, and "the game
  you were playing, with nobody in it" is a worse answer than none.

- **2026-09-09 - the lobby was pinned to the browser because ownership was
  live code that nothing ever reached, and the fix is one oauth2-proxy flag
  plus three env vars.** The owner's report ("matches that players are seeing
  are pinned to the browser instances, not username from keycloak") looked
  like a missing feature and was not one: `oidc.py`, `game_access.py`, and
  `list_lobby_games`'s owner filter had all shipped in phase 2b and were
  correct. They were simply unreachable - `mtg-stack` carried no `OIDC_*`
  env, so `oidc_settings()` returned `None`, so no request could ever be
  authenticated, so every game was created with `owner_user_id = None` and
  the only thing the lobby could key on was `mcm.games.v1` in `localStorage`.
  Diagnosing this by reading `decks_api.py` alone would have missed it
  entirely; the evidence was in `deploy/k8s/50-deployment.yaml`'s env block,
  which had no OIDC in it at all.

- **2026-09-09 - `--pass-authorization-header` forwards the ID token, not the
  access token, and that distinction is the whole hinge of the OIDC
  cutover.** oauth2-proxy v7.15.4's `getAuthorizationHeader()`
  (`pkg/apis/options/legacy_options.go`) builds `{Claim: "id_token", Prefix:
  "Bearer "}`, and `session_state.go` resolves `"id_token"` to `s.IDToken` -
  so both `--pass-authorization-header` (request) and
  `--set-authorization-header` (response) carry the **ID** token. The access
  token only ever gets its own header names, `X-Forwarded-Access-Token`
  (`--pass-access-token`, request) and `X-Auth-Request-Access-Token`
  (`--set-xauthrequest` **plus** `--pass-access-token`, response) - and there
  is no legacy flag at all that puts the access token in `Authorization:
  Bearer`. Only alpha `injectRequestHeaders` YAML can, and upstream reserves
  the right to break that with no notice. Worth writing down because the
  manifest's own pre-existing comment described the `/oauth2/auth` +
  `x-auth-request-access-token` route ("the spellslinger-codex pattern") as
  the intended future path; the request-header route is strictly simpler for
  this app, because it needs **zero** browser change - the UI's existing
  `fetch` calls already traverse the proxy, so they just start arriving with
  an identity. It also avoids putting token caching and 30-minute-lifespan
  refresh into the browser.

- **2026-09-09 - the proxy flag and the api's `OIDC_*` env are two halves of
  one change; shipping either alone 401s the whole play UI.** `oidc.
  current_user` deliberately refuses a bearer token when the deployment has
  no issuer configured to check it against, rather than silently downgrading
  it to anonymous - a good rule that becomes a live outage the moment the
  proxy starts forwarding a token to an api that cannot verify it. The
  reverse direction is harmless (OIDC config with no token forwarded is
  simply inert, every game unowned, identical to today), which is why the
  README's two-step activation sets the api's env *first*.
  `tests/test_k8s_manifests.py::test_the_two_halves_ship_together` asserts
  the biconditional so the two cannot drift apart in the committed manifests.

- **2026-09-09 - the owner is the one caller whose `seats` may be widened,
  and "which seat is mine" then has to come from `agents`, not key order.**
  `list_lobby_games` used to compute `seats` as "the uids you already
  presented in `?uids=`", which is exactly right as a privacy rule and
  exactly wrong for a second computer: a fresh browser presents none, so the
  owner got her row back with `seats: {}` - visible, unopenable. Ownership is
  a locally-verified signature and is strictly stronger evidence than holding
  one of a game's uids, so it is the one identity that may be handed uids it
  did not present; everyone else keeps the old rule verbatim, and an
  *ownerless* game widens for nobody (which is what keeps `mise run dev`, the
  test server, and every game already in production behaving exactly as
  before). Once uids arrive unasked, though, the browser can no longer use
  "the uid I saved" to tell its own seat from the AI's - so `seatsFromRow`
  (`ui/lobby_model.js`) falls back to the seat with no entry in the row's
  `agents` map, and only then to key order. Key order is the server's dict
  iteration order and is never a signal: the AI seat can legally be listed
  first, and picking it would seat the player as the bot.

- **2026-09-09 - existing games are deliberately not backfilled.** Every game
  in production predates this and has `owner_user_id = NULL`. No migration
  sets one, because there is nothing honest to set it to - the api never saw
  an identity when those games were created, and inferring one from a seat
  uid would be a guess. They stay reachable exactly as they are today (any
  browser holding a seat uid) and they will not appear on a second computer.
  Only games created after the cutover are cross-device.
- **2026-09-09 - `store.archived()` is the one read that reports
  `GameUnloadable` instead of raising it, and the review is rebuilt from the
  ledger.** The same schema-3-vs-5 mismatch that took the listings down was
  still 409ing `GET /api/games/{id}/review` - on a 191-action game somebody
  had actually played to the end, whose entire recorded reasoning was
  therefore unreachable, which is the exact thing the soft delete and the
  plan-declaration seam exist to prevent. The engine's refusal is untouched
  (no permissive decode, no migrated or mutated row, read path only); what
  changed is that `archived()` now answers with `ArchivedGame(game=None,
  unloadable=<the decoder's own words>)` and the other five columns beside
  it, all of which were always plain columns. `review.build_review` then
  rebuilds the head from the ledger: `players` from the newest frame's
  `life` keys (the engine's own seat order) falling back to the `seats`
  column, and the outcome from the `game_over` row and nothing else. It is
  marked `reconstructed` with `unloadable` verbatim, and anything the
  ledger cannot establish is `None` and named in `unestablished` - never
  guessed, and in particular never inferred from a life total, which is the
  inference `GAME_OVER_ACTION` was added to remove.

  Both gates are unchanged and now shared (`app._post_game`, used by
  `game_review` and `declare_reflection` alike): the credential check reads
  the archived row's own `admin_key`/`seat_uids`, and "over or ended" falls
  back to the ledger's `game_over` row only when there is no decodable
  board to ask - its *absence* still refuses, so an undecodable game that
  is still being played is refused exactly as before. `_entries_if_
  undecodable` keeps the decodable path from loading a megabyte of ledger
  just to answer the gate.

  The lobby half needed a new fact, not a looser one: `is_over` stays
  `False` for an undecodable row (it is what `MAX_LIVE_GAMES` counts, and
  CR 104.1 is not judgeable on a board nobody can read), so
  `LobbyGameSummary.recorded_ending` carries the `game_over` row's `seq`
  for `unloadable` rows only - computed for no other row, because for those
  `is_over` already answers it off the board and a ledger read per game per
  1.2s poll is the N+1 the summary seam was cut to remove.
  `ui/render/lobby.js`'s `reviewable` keys on it, so the review door is
  offered exactly when the review will actually read, and withheld (rather
  than offered and 409ing) when nothing establishes the game finished.

  Checked and *not* found while there: no wrong-id bug in the Review
  control. `renderGamesTable` binds `row` per iteration (`for (const row of
  rows)`), the handler passes that same row's `gameId`/`myUid`, `open()`
  puts them in the URL verbatim with no "current game" fallback, and the
  table is only redrawn by an explicit `refreshGames` (the lobby table has
  no poll), so no row can move under the pointer. What the lobby does lack
  is any way to tell two games against the same AI apart: rows carry
  opponent/turn/agent but no id or date, and `list_lobby_games` sorts
  oldest-first with locally-known ended games appended last, so the game
  just played is not the top row.

- **2026-09-09 - the lobby's "no id-mixup bug" gap: rows had nothing to tell
  two games apart by.** The click path was already proven innocent (see the
  entry above); the actual defect was that a row carried opponent/turn/agent
  and nothing that named *which* game it was, and `list_lobby_games` sorted
  oldest-first with ended rows appended last - so the game someone just
  played was neither distinguishable from an older one against the same
  opponent/decks nor even the top row. Fixed on both sides: the server sort
  is `reverse=True` on the same `created_at` key it already sorted by (no
  new query, no N+1 - `LobbyGameSummary.created_at` was already a plain
  column), and the client adds a short, stable id (`shortGameId` - the first
  8 hex chars of the `uuid4().hex` game id, the same prefix a human already
  sees in server logs and the game's own URL) and a relative "when"
  (`relativeTime`, pure and deterministic - it takes `nowMs` as a parameter
  rather than reading the wall clock, which is what keeps
  `tests/test_ui_lobby.py` able to pin exact wording). `rowTimestamp` picks
  `createdAt` for a listed row or `endedAt` for one only `mcm.games.v1`
  remembers (an archived game is dropped by `list_lobby_games` outright, so
  it never carries a `created_at`); `lobbyViewModel` sorts those
  browser-only ended rows itself, newest-ended-first, since the server has
  nothing left to sort them by. The state cell became a pill
  (`pill state-<state>`, reusing the page's existing `.pill.ok`/`.pill.bad`
  colors rather than inventing new ones) so the one row actually playable
  right now reads differently from a finished, ended, or unopenable one -
  `gameState(row)` was already computing that distinction and the row's
  appearance was simply not using it.
- **2026-09-09 - card art and mana-symbol SVGs are proxied through this api
  now (`assets.py`); the browser no longer talks to Scryfall directly, and
  the requirement was "no direct scryfall calls", not "no direct scryfall
  calls from the game board".** Three sites reached the browser with a raw
  `cards.scryfall.io`/`svgs.scryfall.io` URL, not one: `serialize.py`'s
  `_card_view` (the board), `symbology.py`'s `get_symbology()` (mana pips),
  and `scryfall.lookup_card()` as returned verbatim by `GET /api/cards`/
  `GET /api/cards/{name}` - the third one only found by actually tracing
  which browser code calls that route (`ui/render/review.js`'s
  `loadCatalogue`, the review page's own card art), not by trusting the
  two-site inventory a prior pass had already written down. `GET
  /api/card-art?name=&size=` and `GET /api/symbol-svg?token=` are the new
  routes (`assets.py`), and neither accepts a URL or anything shaped like
  one: `name`+`size` (a three-value enum) and `token` are the whole query
  surface, and the actual upstream URL fetched always comes from Scryfall's
  own JSON response, never the request. `scryfall.fetch_asset_bytes` is a
  second defense under that: it checks the resolved URL's host against
  `ASSET_HOSTS` (`cards.scryfall.io`/`svgs.scryfall.io`) before opening a
  socket and again after following any redirect, so even a redirect chain
  cannot turn this into a fetch of an internal address. Cached on disk
  beside the JSON cache (`scryfall.cache_dir() / "assets"`), keyed by a hash
  of the upstream URL itself (immutable per URL - Scryfall's own version
  query), not by the caller-facing name/token, so the cache stays correct
  regardless of how many identifiers end up resolving to the same asset.
  `fixtures/payloads/*.json` (the T2 corpus `magic-cards-frontend` pins by
  sha) had to be regenerated (`mise run fixtures:payloads`) since the URLs
  they capture changed shape - a real cross-repo consequence, noted rather
  than silently absorbed. `seatkit.py`'s `_strip_card_weight` already drops
  `image`/`small_image`/`art_crop` outright before an agent ever sees them,
  and nothing in `seatkit.py`/`mcp.py` reads `symbology` at all - so this
  needed no `PROTOCOL_VERSION` bump, checked rather than assumed.

- **2026-09-09 - the play UI can now say WHICH mana ability a pinned source
  taps, and `ability_index` is sent only when the player actually chose.**
  Mid-cast, `pinLand` used to take `findIndex(a => a.is_mana_ability)` - the
  first mana ability and nothing else - and then throw the index away
  anyway, so Eldrazi Temple's restricted `{C}{C}` (its index 1) was
  unreachable from the browser. `app.js` now plans the pin in two pure,
  exported steps (`planPin`, then `planPinnedAbility` once a menu answered),
  and a source with more than one mana ability gets an anchored popover of
  its abilities, each labelled with its whole printed text so the restricted
  one is identifiable. The deliberate asymmetry: a source with exactly ONE
  mana ability names no index at all (`abilityIndex: null`), so `mana.js`'s
  `manaSources` keeps emitting a bare id (or `{card_id, colors}`) and the
  engine's `auto_tap` keeps solving the common case - adding a click, or an
  index, to every basic land would have been the regression. Also fixed
  while in there: a menu opened from inside another menu's `onSelect` cannot
  re-read the original click's `event.currentTarget` (that dispatch is over
  and the browser has nulled it), so `pinLand` resolves the anchor element
  once, up front, and hands it to both popovers.

- **2026-09-10 - a click that registered looked exactly like one that was
  lost, so an action's own `poll()` was replaced by a `refresh()` of its
  own.** Three things were wrong at once in the action path and only
  together did they produce the reported sluggishness: nothing anywhere
  showed in-flight state, so the player had no signal for 0.4-2.4s; nothing
  guarded a double submit, so the instinctive second click was a real second
  request; and the `await poll()` every action path ended in was frequently a
  silent no-op, because `poll()` returns immediately while `app.polling` is
  set and late-game a background poll is in flight at every instant - which
  then discarded its own reads through the `mutations` guard (correctly:
  they predate the action). Net effect: up to a full `POLL_MS` of inert UI
  after a click that had already succeeded. `send()` now carries both the
  in-flight state (`store.busy`, `body.busy`, one seam for all nine action
  paths) and a method+path+body double-submit guard that returns `null` -
  which every caller already reads as "nothing was decided". The post-action
  refresh is a fresh pair of reads rather than "await the in-flight poll,
  then re-poll": awaiting it makes the player's feedback wait on the very
  cycle that is already too slow, and against a held `/log` read it waits
  forever (`tests/test_ui_app.py`'s stale-poll tests hold exactly that read
  open, and would deadlock). Only the board half is awaited - `/log` is the
  slow half and no part of "did my move happen" - and both halves are now
  issued concurrently in `poll()` too, since they were never dependent. The
  `mutations` guard is untouched and still governs both, `refresh()`
  included; a `ticking` latch keeps the run's own `declareNoAttackers` from
  recursing now that it no longer bounces off the `polling` flag.

- **2026-09-10 - the store's listener fan-out is deferred while a press is in
  flight, because a re-render between mousedown and mouseup detaches the very
  button the browser is holding the press on and the click is eaten; the hold
  is capped at 1s (`PRESS_CAP_MS`) since a `pointerup` can genuinely go
  missing, and a touch or pen release waits for its synthetic click
  (`TOUCH_CLICK_MS`, ~300ms later in a task of its own) rather than for the
  next macrotask.**

- **2026-09-10 - the same eaten click, one layer up: the `legal-actions`
  enrichment now PATCHES the open menu (`updatePopoverItems`) instead of
  reopening it, `busy` means "a submission is outstanding" rather than "any
  `send()` is" so reads and dry-run probes stop inerting the casting strip,
  `body.busy` no longer sets `pointer-events: none` on anything (`send()`'s
  `inFlight` set is the only real double-submit guard), and every board read
  carries an issue ordinal so the older of two concurrent reads cannot land
  on the newer - `mutations` can only order reads against this seat's own
  requests, never two reads against each other.**

- **2026-09-10 - a pending decision now says how to answer itself
  (`answer_field`, `answer_with`), and the answer has to be emitted as data
  rather than derived from `ANSWER_FIELD_FOR_KIND`: that table names the
  engine `Answer` field and the new `TOOL_FIELD_FOR_KIND` names the MCP tool
  parameter, and they disagree on ten of the eighteen kinds (`split` vs
  `moved`, `text` vs `choice`, `amount` vs `energy_amount`/`life_amount`,
  `accept` vs `top`, `card_ids` vs `order`, and `pairs` vs
  `attackers`/`blocks` on a different tool entirely, since `decide` has no
  `pairs` parameter). Both tables carry the same `set(...) == set(DecisionKind)`
  guard. The MCP `decide` tool also gained `generation` - the enforcement and
  its refusal text had existed at `app.py`'s `decide` since the field was
  added, and `seat_cli --generation` with it, so the only thing missing was a
  tool parameter to reach it: a stale read was earning the flat "no pending
  decision for you" (11 measured) instead of the diagnostic naming both
  generations. `seatkit-14` covers both halves - additive on the request,
  additive on the response.**

- **2026-09-12 - the matchup card's rendered size is not a function of its
  plan's size, and the fixture slice is not the card's worst case.** Measured
  over all thirteen guides in `mtg-decks` at `01fae6c`, corpus root
  `~/Documents/mtg-decks`, with the ladder that gives full fidelity the whole
  cap: the three largest `boarding-plan.json` (orzhov-martyr 141730,
  dimir-frog 110697, prison-tron 109669) render the 7th, 8th and 3rd largest
  cards (10982, 10864, 11357), while the largest cards come from mid-sized
  plans - living-end 11566 from a 107974 byte plan, affinity 11490 from
  97592 - because the trim ladder bounds the output and what is left is set
  by matchup COUNT and how many of those archetypes have an `archetype.json`
  to quote. Maximum in the corpus is 11566 against the 12288 cap, no guide
  truncated; the smallest rung renders at most 6923 (prison-tron). The full
  table is in `tests/fixtures/guides/README.md`, pinned by
  `test_the_readme_quotes_the_size_the_card_actually_renders_to` so it cannot
  drift again. Two consequences: the card's byte figure is path-dependent -
  the card prints the plan's absolute path twice (the header, and the
  one-read line), so a figure moves by exactly TWICE the root's length: 58
  bytes larger under `.claude/worktrees/<name>/...` than under `code/`, 56
  smaller under the `/guides` a seat Job clones to. Never quote a raw figure
  without its root, and pin only `byte_size - 2 * len(str(plan_path))` in a
  test. And the cap holds regardless of what the fixture pins, because
  `_capped` enforces `hard_cap_bytes` by cutting bytes in code. Also
  re-measured the `matchups/*.json` figure the briefing quotes: 211 files,
  min 3376, median 6202, p90 8194, max 28492, so the prose says 3-9 KB (28 KB
  at the largest), not the 5-20 KB it used to claim.

- **2026-09-12 - a measurement written into docs in the same commit that
  changes the code being measured is stale on arrival.** `27291a8` reordered
  the trim ladder AND wrote byte figures measured before the reorder into
  `tests/fixtures/guides/README.md` and this file; every figure was 1.3-1.9
  KB low with the ranking wrong, and the same commit's own test docstring
  contradicted them. Worse, the program that made its suite green existed
  only as uncommitted working-tree edits - `HEAD` failed 9 tests of
  `tests/driver/test_matchup_card.py`. Two habits out of it: re-measure
  AFTER the last code change and never reuse a number from earlier in the
  session, and make any figure a doc quotes a thing a test reads out of the
  doc, so "the docs went stale" becomes a red suite rather than a review
  finding.
- **2026-09-12 - never infer intent from the raw request body. The advisor
  gate permitted a POST carrying `dry_run: true`, read straight off the JSON,
  and only nine routes declare that field: on every other shape pydantic
  DISCARDS an unexpected key without a word, so the gate said "probe" and the
  route committed for real. Twenty mutating routes were reachable this way,
  and `POST .../seats` was not a stray mutation but privilege escalation -
  `mint_seat` takes no uid, checks nothing beyond the gate, and hands back a
  fresh unrestricted seat credential, so one extra key let a read-only
  credential upgrade itself out of its own restriction. The gate reads the
  matched route's own declared shape now (`game_access.DRY_RUN_ROUTES`,
  asserted against `openapi.json` so it cannot drift) and every request body
  inherits `schemas.RequestModel`'s `extra="forbid"`, so an undeclared field
  is a 422 instead of a discarded key. `POST .../cards/{card_id}/tap` takes a
  raw `dict[str, bool]` that no `model_config` can protect - the gate is its
  only guard. Route matching is on `request.scope["route"].path`, the
  template, never on `request.url.path`'s tail, which a caller can spell.**

- **2026-09-12 - visibility is not authorization, and `visibility.
  resolve_viewer` answers only the first. It resolves an advisor uid to the
  human seat it watches, which is exactly right for the read filter and
  exactly wrong as "does this uid open the seat this request names" - four
  routes asked it that (`decide`, `_answer_declaration` behind
  attack/declare-blockers, `_pregame_seat` behind mulligan and opening-hand),
  so with the gate stood down in a test an advisor declared alice's attackers
  and got `{"allowed": true}`. Acting-seat checks call
  `game_access.seat_for_uid`, which never resolves an advisor; redaction and
  GET-visibility questions (`advance`, `get_legal_actions`) keep
  `resolve_viewer`. The two have deliberately different names because the two
  questions were being asked with one function.**

- **2026-09-12 - a deny-list keyed on a credential cannot restrict a caller
  who omits the credential. The advisor restriction only ran `if uid and
  ...`; with no uid the code fell through to "this game has no owner, so
  yes", and no bearer token has ever reached this deployment, so that is
  every game. Dropping one query parameter passed a real priority and minted
  a real seat. `game_access.can_access_game` now carries a POSITIVE rule -
  POST/PUT/PATCH/DELETE on a `{game_id}` route requires a seat uid of that
  game, its admin key (accepted in EITHER slot, since `resolve_viewer` and
  `pregame/settle` have always taken it in `uid=`), or an owner bearer token.
  Reads are untouched; an ownerless game stays world-readable, which is the
  migration story. `seat_for_uid` consults `store.archived` when the live
  lookup misses, because a lobby-ended game is soft-deleted while its seat
  uid is still that seat's credential and `POST .../reflection` is written
  exactly then. 469 uncredentialled call sites in the suite are covered by
  `conftest._harness_credential` (the admin key of a game the harness itself
  created, patched onto `TestClient.request`, skipping DELETE, opt-out via
  `@pytest.mark.no_harness_credential`) - which is also the one hazard here:
  a new security test that forgets the marker passes for the wrong reason.**

- **2026-09-12 - two acceptance-run anomalies, both diagnosed and fixed.**
  (1) A 10-seed run's `GET /api/games` showed 34 live games, not 10: postgres
  outlives the process that boots into it, and nothing ever archived a
  gauntlet's games, so every past run's games (four of them, from earlier
  dev/test runs: 2+2+10+10) stayed live forever, indistinguishable from the
  current run's own 10. Fixed by soft-deleting every game a run booted, via
  the existing `DELETE /api/games/{id}`, in `eval_gauntlet.run`'s own
  `finally` - regression test boots a real subprocess against postgres and
  asserts the live-game count is unchanged after the run.
  (2) Baseline-vs-baseline mirrors were NOT stalling and NOT a policy defect
  (attacks/blocks were being declared correctly, 0 refusals in 3197 ruled-on
  attempts) - the acceptance run's 10 concurrent games shared one server
  process (one CPU's worth of GIL, one postgres pool), and `legal-actions`
  costs real time (measured mean 61ms/p90 154ms/max 226ms once a board had a
  dozen-odd permanents, `Game.computed_board` recomputing rather than
  caching, by design, out of scope to change). Confirmed by timing: the same
  seed's game reached its own real `game_over` in ~131s run alone
  (`--serial`) vs ~264s sharing the process with one other game - roughly
  2x slower at just 2-way concurrency. Fix was `--serial` (play games one at
  a time) plus making `eval_report`'s output honest about a cut-off game:
  `unfinished_detail` carries `watch_game`'s own edge (`timeout`/`stalled`/
  `report`) per game, and the rendered report states outright that
  "unfinished" is never scored as a loss for either seat. The acceptance
  condition is now stated explicitly in `eval_gauntlet`'s own docstring: a
  bounded game reaching a real terminal state within its own bounds, not
  "ten concurrent games finish in a given wall-clock time" - the latter is
  a throughput property of this machine, tunable, and unproven at n=10.

- **2026-09-12 - the retrospective half of mtg-buddy, against a write side
  (B2) that had not landed yet.** `audit.ADVICE_ACTION`/`ADVICE_APPROVED_
  ACTION` did not exist, so both constants are defined here to the contract
  given (`"advice"`/`"advice_approved"`), for B2 to meet rather than
  reinvent. One deliberate deviation from that contract's literal wording:
  `reasoning` is read off the entry's own top-level field first (falling
  back to `detail["reasoning"]`), not out of `detail`, because every action
  route in this codebase already passes `reasoning` as `audit.record`'s own
  keyword argument, which lands top-level - the contract's `detail: {...,
  reasoning, ...}` reads as the logical field set, not literally which key
  it lives under.
  `_advice`'s `did` is deliberately never a row the engine refused
  (`detail.allowed is False`) - a refusal is a failed attempt, not "what
  happened". This makes "approved but refused" and "never approved, did
  nothing" collapse to the same `did: None`, distinguished only by the
  `approved` flag - intentional, not an oversight: the alternative (showing
  the refusal as `did`) would make `followed` ambiguous (does "followed"
  mean "attempted the same thing" or "successfully did the same thing"?).
  Verified `_declarations` is unaffected: its marks are `PLAN_ACTION`-only,
  `_advice`'s are `ADVICE_ACTION`-only, and an advice row still shows up
  inside a declaration's own `did`/`opponent_did` list like any other row by
  that seat (test: `test_advice_rows_do_not_disturb_the_declaration_
  windows`).

  **Follow-up the same day**: `did` excluding a refused row was right, but
  the refusal itself must not be thrown away - this engine's whole reason
  for existing is saying out loud *why* a refused action is illegal, and
  that reason is the single most instructive line a retrospective can show.
  Added `refused` alongside `did` (never touching `did`'s own meaning): every
  row by the advised seat in the same window that the engine refused,
  reason verbatim, never assumed to be at most one (a seat can retry). Now a
  fourth, visibly distinct outcome: approved, attempted, and refused with the
  engine's own words - rendered in `ui/render/review.js` in place of the
  ambiguous empty-outcome line, which now renders only when `refused` is
  also empty. Mirrored line-for-line in `ui/review_model.js`'s `deriveAdvice`
  fallback (`refusedInWindow`) and in `reshapeServerAdvice`.

  **Visibility, confirmed rather than assumed**: `GET .../review` has no
  per-seat redaction of any kind - the whole `build_review` result, `advice`
  included, goes to whichever seat uid or the admin key asks, once the game
  is over. An advice row's `summary`/`reasoning`/`body` can name cards that
  were in the human seat's own hand at the time, and the opponent reads them
  post-game exactly as the advised seat does - the same rule `declarations`
  already lived under. Stated explicitly in `app.game_review`'s own
  docstring and proven by `test_the_opponent_reads_the_same_advice_the_
  human_seat_does`, rather than left as an unstated consequence of reusing
  the same route.

- **2026-09-12 - "no permanent name leaks once the digest is withheld" is
  not the same claim as "the digest is never half-emitted", and conflating
  them nearly broke a legitimate feature.** Wiring the engine's
  `Action.pays_with` (mana tap plan, `magic-cards-model` at `4ad120c`)
  through to an MCP answer made a pre-existing test
  (`test_a_digest_that_will_not_fit_is_replaced_whole_and_never_half_
  emitted`) fail: a `pays_with` entry legitimately names the permanent it
  would tap, and that name showed up in the answer even when
  `MAX_DIGEST_BYTES` forced the battlefield digest itself to be withheld.
  The instinct was to scrub the name everywhere the digest is absent: wrong,
  because the test's actual hazard is a seat forming a false belief that it
  has seen the *whole* board - which only the digest (a claimed survey of
  every permanent) can cause. `pays_with`, `unaffordable_reason` (engine
  refusal prose) and a targeting action's own candidate lines all name a
  card in service of one scoped fact about one action and never claim
  completeness, so narrowing the test's assertion to `board` itself (with
  the reasoning written into its docstring) was the correct fix, not a
  weakened test - scrubbing them would have gutted the very target-candidate
  enrichment that channel exists for. `pays_with` still got its own real
  byte budget (`mcp.MAX_PAYS_WITH_BYTES`, independent of `MAX_DIGEST_BYTES`)
  since it had never had one at all.
- **2026-09-12 - the two-tool loop's prohibitions are safe everywhere except
  one answer: `wait` (`seatkit.block_until_wake`) is the ONE answer carrying
  neither `available_actions` nor `recent_log`, so the loop's wait is
  `pass-until <step> --max-seconds 20` and a bare `wait` is reserved for the
  single case where `pass` is refused for want of priority.** Narrowing the
  agent prose to "a wait, then exactly one committing call, nothing in
  between" means forbidding `state`/`battlefield_state` and `log`/`game_log`
  outright, and that is only honest where the wait's own answer really does
  replace them. `block_until_wake` returns `recent_log` as a *sibling* of
  `state` (its docstring says why it can never be a key inside it) and never
  computes `available_actions` at all, and `seat_cli`'s `wait` branch prints
  `woken["state"]` alone - so the brief's older advice ("run `wait
  --max-seconds 20`" whenever an action woke with "nothing new yet") plus the
  new prohibitions left a seat holding an answer with no action list, no log
  tail, and no sanctioned way to get either. `pass-until` goes through
  `seatkit.pass_until` and carries all three, which is why it, not `wait`, is
  the loop's wait; what a bare `wait` *is* good for is that `pending_decision`
  is part of the state itself, so the paste-ready `answer_with` rides it. The
  MCP transport differs and the prose had to differ with it:
  `wait_for_priority` merges `recent_log` into its state view (so "`recent_log`
  rides every answer" is true there) but still carries no `available_actions`,
  which is now said out loud where the tool is introduced. The brief's `wait`
  bullet and `tests/test_prompt_matches_tools.py` keep the three facts and
  the prose in step - the tests read the shape off the running CLI and MCP
  server first, then pin the sentences against it, so a future change to
  either answer fails on the behaviour rather than on the prose.
- **2026-09-12 - the buddy reuses the whole seat lifecycle, and the two
  places it could not are both about the uid being a credential.** An advisor
  gets `role` threaded through `AiSeatRegistry.start` into all three
  mechanisms - the brief (`docs/ai_buddy_brief.md`), `DispatchIn.role`
  (picking `prompt.advisor_turn_zero`), and `k8s_seats.build_job` (the
  `mtg-buddy-*` name plus `mtg/role=advisor`, with `app.kubernetes.io/name`
  left at `mtg-agent` so `45-networkpolicy.yaml` still selects the pod).
  `reconcile` needed nothing: it lists by that same app label and deletes by
  `mtg/game-id`, which is right for a buddy too. No new Job template and no
  new `SEAT_JOB_PLACEHOLDERS` entry - `role` rides in
  `__ASSIGNMENT_JSON__`'s payload, written only when it is not `seat` so a
  seat's body stays byte-identical. **The two exceptions.** (1)
  `ai-seat/start|stop` take the buddy's uid in a new `advisor_uid` field, not
  in `uid`: `game_access._credentials` reads a body's own `uid` as the
  CALLER's credential, and an advisor credential is refused every POST - so
  the human asking for their own buddy back with `{"uid": <advisor>}` got a
  404 from the gate before the route ever ran. (2) `game_seats` takes the
  advisor uid out of an advisor's `ai_seat` status (`_advisor_row_status`):
  every registry status dict names its own uid, so listing the advisor's
  liveness on that broadly-readable route leaked the credential through the
  status even though the row itself carried no `uid` field. Dropping the
  `uid` key was not enough, and review caught it: a `mechanism: "fallback"`
  start also hands back `prompt` and `command`, which are the brief and the
  argv with the uid substituted INTO them, and that is the shape the api
  container actually produces (no `claude` on PATH). So the helper drops
  every uid-bearing key AND scrubs the uid as a substring from whatever is
  left - a kubernetes `reason` quotes the `mtg/seat-uid=<uid>` selector its
  API error was raised against, and a subprocess `stdout_tail` is the
  agent's own output. Removing it by value, not only by name, is what covers
  the key nobody enumerated. Three tests, one per status shape (running,
  never started, tried and failed), because the running one alone missed the
  fallback. `AiSeatRegistry.status` also answers a remembered
  `fallback` now instead of `None` (`_fallbacks`), because a buddy is started
  once at creation and a `None` there rendered as a bare "not started" with
  no reason for a runner that had genuinely tried and failed.**

- **2026-09-12 - `Action.pays_with` names a land, and the digest test had to
  learn the difference.** The engine's tap plan carries the permanent's name
  as well as its id, on purpose. That made
  `test_a_digest_that_will_not_fit_is_replaced_whole_and_never_half_emitted`
  red: it asserted no battlefield name appeared anywhere in an action answer,
  as a proxy for the digest never being trimmed. The plan is a complete list
  and the name on it is deliberate, so the check was re-aimed instead of
  weakened. Two branches wrote that fix independently and M4's is the one
  that landed (`cd38b85`): the scan is scoped to `board`, the digest's own
  home, and looks for the digest line's own `<id> <name>` prefix. B5's
  version (lift `pays_with` out, keep scanning the whole answer) was dropped
  in the merge because the whole-answer scan is wrong in principle as well as
  for `pays_with` - a targeting action's candidate lines name cards too, and
  legitimately. Any engine pointer move that touches an `Action` field needs
  `mise run openapi:dump` re-run as well; the dump is only as current as the
  pointer it was taken against.**
- **2026-09-12: an engine pointer advance reddens this repo's committed
  artifacts, and `mise run test` is where it shows up - not just `test:pg`.**
  `magic-cards-model` landing `Action.pays_with` made `openapi.json` stale
  (`ActionWithTargets` subclasses the engine's `Action` on purpose, so an
  engine field arrives with no code change here) and broke
  `test_answer_enrichment.py`'s digest-cap test, which had been scanning the
  whole answer for `"<name>` - a pattern that never matched a digest line
  (`"<id> <name> ..."`) and only ever passed because nothing else in the
  answer named a card as a bare string value. Two lessons: after
  `git submodule update --remote` on the engine, run `mise run openapi:dump`
  before believing a red suite belongs to the branch in hand; and a leak
  assertion must match the shape of the thing it is guarding, or it is
  asserting nothing until an unrelated field makes it fire.

- **2026-09-12 - a metric that is structurally always zero while every rate
  around it reads healthy.** `scripts/eval_report.py` has counted refusals
  into three buckets since P4 - `stale` (a race), `rules` (a genuine
  illegal action), `unknown` (cannot tell) - and `rules` was always 0.
  Nothing was wrong with the report: `elif stale is False: tally.rules += 1`
  is correct. The *write* side never produced a `False`. `detail["stale"]`
  was set in exactly one place, `records.require_current_board`, and only
  ever to `True`; no route stamped it on an ordinary refusal. So every rules
  refusal fell into `unknown`, `rules_error_rate` read `0/N` forever, and
  `stale_split` could never be better than `"partial"` - the ceiling was
  structural, not data-dependent. Only synthetic rows in
  `tests/test_eval_report.py` ever exercised the branch, which is precisely
  why nothing went red: the read side was tested against a ledger shape the
  write side could not produce. **The whole capability the field exists to
  deliver - "of 32 refusals in 556 agent attempts, how many were
  latency" - was missing for months, from a report that looked complete.**
  Two lessons kept: (1) a three-state field needs all three states written
  by production code and asserted end to end, never per-state unit rows;
  (2) the fix belongs at ONE write site, not threaded as a parameter through
  every refusal-recording call - this api records refusals in eleven places
  (`app.audit_outcome`, `decide`/`declare_blockers`'s local `refuse()`,
  `pass_priority`/`advance`'s `except` arms, `opening.py`, ...) and asking
  each to pass a flag is how it came to be passed by none. It is now one
  `ContextVar` set by the guard (`records.named_a_current_board`) and one
  read in `audit.record`, so a route written tomorrow is stamped by
  construction. The `unknown` bucket stays honest: absent still means the
  caller named no board position, and collapsing it into `False` would make
  `stale_split` report `"measured"` over data that measured nothing.

- **2026-09-12 - "Why it cannot stall" was false, and the module said it in
  prose. Never restate that claim without a test that would see it break.**
  `baseline_seat.play_out` set `acted = True` whenever `seat.step(state)`
  returned a `Move`, and `step` returns the move whether the api ACCEPTED or
  REFUSED it. So `idle_rounds` reset every round for as long as any seat kept
  attempting anything, the `idle_rounds >= stall_rounds` concession backstop
  could never fire, and a game spun until `max_steps` (100,000 in the
  gauntlet) and was reported as a `timeout` - the unfinished game the
  scorecard exists to count. Two tests passed straight through it:
  `test_two_baselines_play_a_bounded_game_to_an_end_state` (a healthy game
  never refuses anything) and
  `test_the_stall_guard_ends_a_game_the_policy_cannot_finish` (which forces
  `choose_move` to `None`, so the seat attempts nothing at all). The path
  that shipped broken is the third one - a seat that keeps ATTEMPTING and has
  every attempt refused - and it now has its own test with a seam that
  refuses every action. A round is idle when no seat *advanced*
  (`BaselineSeat.advanced`: an accepted move, or a refusal that was new
  against this board), the guard checks that its own concession was accepted
  (`stall_guard_refused`), and the module's prose now names the one case the
  guarantee does not cover - a board that keeps changing without the game
  getting anywhere - instead of listing five defences of which two did not
  hold. A move with no enumerated row behind it (`Move.key == ""`: a pass, a
  decision, a pregame or combat declaration) could not be remembered by
  `action_key`, and what it did instead - dropping `self.signature` - made
  the loop worse, because the re-derive threw away this board's refusal
  memory and produced the identical move. Those are remembered by
  `answer_fingerprint` (path plus body, minus the seat's own `reasoning`
  prose) and `_pick` never posts an answer this board has already refused.

- **2026-09-12 - a narrowing sequence is only a termination argument if you
  check the arithmetic and the fixed point.** `attack_declaration`/
  `block_declaration` narrowed a refused declaration with `chosen[:
  len(chosen) - drop]`, whose docstring claimed "at `drop >= len(pairs)` this
  is the empty declaration ... so the narrowing always terminates". Both
  halves were wrong. A negative slice re-GROWS the list: with two pairs,
  `drop == 3` is `chosen[:-1]`, so the sequence went 2, 1, 0, 1, 0 and
  oscillated instead of bottoming out (`max(len(chosen) - drop, 0)` now). And
  the empty declaration is a FIXED POINT, so if the engine refuses it - a
  CR 508.1d attacking or CR 509.1a blocking requirement does, and narrowing
  moves away from a requirement rather than towards it - `step` incremented
  `narrowing` forever and re-posted a byte-identical body. Narrowing to empty
  is now the end of what the policy has to say, not a guarantee of
  acceptance; the stall guard is what ends such a game, and both docstrings
  say so.
- **2026-09-12: two parallel tasks, each correct alone, wrong together -
  `advisor_turn_zero` kept telling the buddy to read a 58 KB gameplan guide
  after a sibling task had already replaced that instruction with an inlined
  matchup card for the seat.** One task narrowed `turn_zero`'s doctrine to
  the inlined card (`render_current_game` via `matchup_card.py`); the other
  added `advisor_turn_zero` in the same file, against a base that predated
  the first. Neither diff conflicted, so nothing forced a look at the other's
  prose, and the buddy - the most inference-hungry seat, thinking at every
  decision point its human holds - kept the one instruction that costs a
  whole turn of output for a `Read`. Fixed by pointing `advisor_turn_zero` at
  the same `briefing` the driver already built with `render_current_game`
  (it was already threaded through as a parameter, just not trusted) instead
  of telling it to go read the guide itself. Lesson: two prose-editing tasks
  in the same file that both touch "what turn 0 tells the agent to read"
  need to diff against each other explicitly, not just against their own
  base, or a base-truth instruction can survive in one branch after the
  other branch made it wrong.
- **2026-09-12: `DELETE /api/games/{id}` recorded the deletion and released
  nothing, and in `wrapper` mode that leak is PERMANENT, not merely untidy.**
  The route did `audit.record` + `store.delete`, never `lobby.reset_game`, so
  the pregame watcher kept polling, the seat's process stayed alive, and the
  registry kept answering `status()` for uids of a game that no longer
  existed. The non-obvious consequence is the wrapper mechanism: the driver
  holds one assignment per container and 409s a second dispatch ("it plays
  one match per container and is free again once that game ends"), and the
  only thing that ever tells it a game ended is the release this route
  skipped - so one delete wedged that container for its whole life. Subprocess
  mode leaked a live `claude`; kubernetes mode leaked the Job until reconcile
  noticed, up to 60s. Fixed by delegating to `lobby.reset_game`, which also
  closed the pre-existing seat half. `reset_game` gained an `action` argument
  rather than the route losing its own audit name: the payload is identical
  either way, but the ended game's transcript still says which of the two
  routes ended it, and `scripts/eval_report.py` already knew both names.
- **2026-09-12: `review.py` computed `followed`, `review_model.js` mirrored
  it, and NO renderer read it - so "approved and followed" and "approved, then
  did something else" rendered byte-identically.** Server-side field, mirrored
  twice through the view model, and dead at the last hop. The second state is
  the one a trainer reads the retrospective for, and "four distinct outcomes,
  each rendered distinctly" was an explicit acceptance condition of that work.
  It survived a green suite because the test named
  `test_advice_approved_and_followed_...` set `followed=True` in its fixture
  and all five of its assertions passed identically with `False` - a fixture
  field asserted by nothing reads as coverage. Fixed with an
  `advice-followthrough` line above the `did` frame, independent of
  `approved` (a human can do exactly what was proposed without approving it),
  and the test now asserts the line and fails when the flag is flipped.
  Lesson: a computed field is not delivered until something reads it, and the
  cheap check is to flip the fixture value and see whether any assertion
  moves.
- **2026-09-13 - an MCP advisor's wake carries `available_actions`, gated on
  advise AND a real wake, and that was a bump to `seatkit-16`.** The buddy is
  refused `pass_until`, so `wait_for_priority` is the answer that wakes it, and
  it carried the board and no list while an opponent seat got the list on every
  `pass`/`pass_until`. `mcp.wait_for_priority` now adds it through the action
  answers' own `_compact_actions` only when `seat.advise` and the wake reason is
  not `NOTHING_NEW_YET` - an idle poll never pays the ~106 ms read, and a seat
  uid's wait is unchanged (its list already rides its actions, so adding it
  there is pure redundant cost). Measured on `test_action_answer_size.py`'s busy
  board: roughly +8 KB on a wake answer that was ALREADY about 51 KB
  before (the exact split was not re-measured), over `MAX_ANSWER_BYTES` (45,000), which bounds action answers
  only; the whole state view is what that tool is for and an advisor test pins
  it whole, so the list is bounded by the existing candidate and `pays_with`
  caps (proved by starving both) rather than by narrowing the board. Bumped
  rather than folded because the MCP server is its own once-spawned process and
  this build's advisor prose promises the list: a stale `seatkit-15` process
  would silently answer without it, which is the case PROTOCOL MISMATCH exists
  for. The fetch is one GET on a path that was already a GET; no POST path was
  added and `pass_until` stays refused.
- **2026-09-13 - the MCP wait path busy-spun because `Seats.seat_for` mints a
  brand new `Seat` on every single tool call, so a naive "remember it on the
  `Seat` instance" fix would have been a no-op.** `block_until_wake(seat,
  before=None, ...)` - what `mcp.wait_for_priority` and `seat_cli wait` both
  call, since neither performed the action a real `before` would come from -
  fed `compute_wake_reason` a permanent `None`, whose priority branch treats
  `before is None` as "always different": a seat holding priority woke on
  *every* call, board unchanged or not. An advisor sitting on its human's
  decision spun forever. The fix caches `board_signature` per uid in
  `Seats._wake_signatures` (shared by reference into every `Seat` it mints,
  parallel to `_by_uid`) rather than as a plain `Seat` attribute, precisely
  because `Seat` itself does not survive between tool calls - only `Seats`
  does. `block_until_wake` reads that cache only when `before is None` (so
  `run_action`'s own real `before` is never second-guessed) and writes it back
  on every genuine wake, whoever's `before` produced it. Confirmed a naive
  per-`Seat` cache would have masked nothing: `test_advise_mode.py`'s
  `test_an_advisor_woken_with_the_legal_actions_still_cannot_move_the_game`
  had been looping `wait_for_priority` 3 times on an unchanged board asserting
  a real wake every time - it was pinning the bug as a feature - and needed
  updating alongside the fix.
- **2026-09-13 - `test_action_answer_size.py` cost 6 CI minutes twice
  (`test`/`test:pg`) because every one of its eleven tests rebuilt an
  identical 44-card board through `add_card`, and each call recomputes the
  whole board's `characteristics_of` from scratch (no memoisation of parsed
  oracle text anywhere in the engine) as the board grows - not the actions
  under test, which were cheap by comparison. Fixed at the test layer, not
  the engine: `stage_card`/`_stage_busy_board` place cards straight through
  `engine_api.put_card_in_zone` (skipping the `state_view` every caller here
  threw away unread), and the resulting `Game` is cached once per session
  and deep-copied onto each test's own game rather than restaged. 136s -> 27.8s
  for the file, 712s -> 211s for the whole suite. The underlying
  `characteristics_of` cost is a real production hot path with no caching -
  left alone here since nothing in this file's own scope called for an
  engine change, but any future work touching that path should know eleven
  tests in one file were enough to double CI's budget.
- 2026-09-13 A13: the seat Job guides checkout is writable (deploy/guides-clone.sh: partial `--filter=blob:none` fetch of main, local main tracking origin/main), so GUIDES_REF is now main-only and the pinned-ref/sha path is gone - a harness wanting a fixed corpus has no knob until a read-only mode returns. The PAT is a file mount (/var/run/mtg-decks-git/GH_PAT, 0440 via fsGroup) read by a ~/.gitconfig credential helper, never an env var. SCRIPTS_ENABLED is published/documented here - this line used to say nothing reads it, which was already stale by 2026-09-14: `records.scripts_enabled` (records.py ~585-590) reads it and `records.require_scripts_enabled` is checked by `register_scripts` (app.py ~3341) and `record_script_run`'s start phase (app.py ~3472). preStop modules mtg_agent.driver.handover / mtg_agent.wrapper.handover_wait are named in the template but implemented by A14.
- 2026-09-13 A1 (seatkit-17): `known_cards` is a sibling block of `hands` in `serialize.state_view`, keyed by the player who knows and built live from engine `Game.known_to` (own hand/sideboard dropped, own-library looks kept); `visibility.redact_view` keeps only the viewer's entry and rebuilds it from recognised shapes (fail closed). A player's own library/sideboard are served only as unordered `{card, count}` multisets with no ids or positions (`library_contents`/`sideboard_contents`), only behind `GET /api/games/{id}?include=contents` or a named `battlefield_state` section - never in the default board, an action answer, or a wake, so the answer budgets are untouched. `schemas.CardView` now inherits its fields from `_CardViewFields` so `ContentsCardView` can omit `id` without a type override; pydantic keeps `id` first, so response key order did not move.

- **2026-09-13 - Complex decisions moved into a persistent floating window
  (`ui/render/decision_window.js`, `#decision-window` built once in
  `index.html`).** Scry, surveil, look-at-the-top split, every `choose_cards`
  (whatever zone), `order_triggers`, and the seat's own pregame mulligan are
  answered there with real cards; small kinds stay in `#prompt-box`, which
  now only points at the window for the complex ones. `decisions.js`'s
  `usesDecisionWindow` is the single routing test. It cannot be a panel that
  `renderPromptBox` rebuilds: the press guard stops deferring after
  `PRESS_CAP_MS`, so a card mid-drag there is torn down on the next poll. The
  window keys its content on player/kind/generation/cards, patches in place,
  and defers every render while a card or the window is being dragged. Card
  drags are pointer events plus `elementFromPoint` (`.dw-slot.dragging` is
  `pointer-events: none` so the hit test sees the lane underneath), not HTML5
  drag-and-drop, which never fires for touch. A trigger-ordering window shows
  the offered order as a full order, so confirming untouched posts it; the
  answer is still `answerFor` over that order.

- **2026-09-13 - the play UI keeps priority by default, under `mcm.stops.v2`.**
  The owner: casting without keeping priority was too easy, and activating
  must match. `stops.keepPriorityAfterCasting` (default off, v1) became
  `stops.keepPriorityByDefault` (default on, v2). The key moved because every
  v1 payload wrote `false` as the old default, not as a choice, so v1's value
  is ignored rather than migrated; v1's step ticks and stack tick still load
  when no v2 payload exists. `A.begin` seeds `holdPriority: null`, meaning the
  casting strip's tick follows the standing default until touched. Agent and
  REST defaults (`hold_priority=false`) are deliberately unchanged.
- 2026-09-13 A2: the board reads the opponent's known hand as `known_cards[you].hand` filtered by `owner`, not `known_cards[opp]` - the view is keyed by the knower and redacted to the viewer's own entry, so `known_cards[opp]` is never present. Known library records now carry `source` (engine `KnownCard.source`, a non-string is coerced to None by `redact_view`) so the top-card chip can name what showed it. The known hue is a `--known` token with a `prefers-color-scheme: light` override.
- 2026-09-13 A4: play-script routes. The corpus lives in `games.scripts` (JSONB, uid -> corpus, revision d5b2e8f4a913), keyed by the uid the seat Job runs under, so a buddy registers under its advisor uid and `game_access.ADVISOR_OWN_PUT_ROUTES` lets an advisor PUT its own corpus only. Postgres writes one key with `jsonb_set` so a seat and its advisor registering together cannot erase each other. `already_ran` is read from the ledger (`script_run_started`/`script_run` rows, bounded by RECENT_RECORDS_WINDOW), not a column, so it holds across replicas; two replicas racing one start can both land. `visibility.redact_log_entry` now returns `None` for a row the viewer may not know exists (script runs, uid-less reads included), and `game_log` drops those while `last_seq` still counts them. The kill switch refuses PUT and `start` but still records `finish`, so a run in flight keeps its row. The post-game review is otherwise unredacted, but `game_review` drops script-run rows for the opponent (`visibility.hides_script_run`), and `review.py` never counts one as a seat's move (`did`, `opponent_did`, advice `did`/`refused`).
- 2026-09-13 A12: `mcp-seat plays priority|admit` and `guides propose` (playscripts/admit.py, guides_propose.py, shared gitwork.py). The agent image has no mise, so the in-pod gate runs `python3 scripts/check_plays.py` and `sys.executable -m magic_cards_model_api.seat_cli plays check` directly - a documented exception to the mise-for-everything rule. The GitHub token for the REST pull-request call comes from `git credential fill` (A13's helper), falling back to the mounted GH_PAT file, and lives only in that request's Authorization header. The conflict ratchet (`conflict_regression`) compares origin/main against the rebased HEAD over git trees, materialising only plays files and matchup stems (so a blob:none clone fetches no analysis blobs). Priority-only rebase conflicts replay the delta recorded against the merge base in .git/mtg-plays-deltas.json, recomputed from the diff at admit time so a hand edit replays too. On gate_failed/conflict_regression the admit commit is soft-reset so the next admit is one commit; on pr_opened local main is reset --keep to origin/main (the commit lives on the pushed branch). `guides propose` commits on a local seat/<game>-guides branch and switches back, so local main never carries a commit that must not reach main. `error` is an extra infrastructure result (no token, GitHub refused) beyond the typed verdicts.
- 2026-09-13 F2v (battle 1 D02-D05, D07, D08, D13; rules mirrored by mtg-decks `scripts/check_plays.py`):
  (1) A double-faced card view (engine layout transform, modal_dfc, meld, double_faced_token or reversible_card, with >= 2 `card_faces`) carries `faces: [{name, mana_cost, type_line, oracle_text}]`, front first; single-faced, split and adventure cards carry none. Card-level `oracle_text` is the printing's top-level text, else `CardState.up_face`'s text (front face off the battlefield and stack, CR 712.8a; the up face there, CR 712.8d-f) - read from `up_face`, not `CardState.oracle_text`, which blanks a face-down card for its own owner. `type_line` off the battlefield stays the combined `Front // Back` line.
  (2) Selectors on a view with `faces` read type words and `oracle_has` from `faces[0]` in every zone but battlefield and stack; on the stack from the face whose `name` equals the view's `name` (else `faces[0]`); on the battlefield from the view's computed `type_line` and `oracle_text`. A split card (no `faces`) still unions both halves.
  (3) `conditions.evaluate` results gain `invalid_input: {name, value, reason} | None`. Before any condition, every supplied input is checked by `inputs.supplied_input_problem`: a name not in the catalog, an enum value not in `values`, a non-bool for a bool, a non-int (bool included) or a value outside min..max for an int. An invalid input makes `applies` false with no `failing_list`. The default catalog is the built-in alone (`opposing_archetype` = `*`), so a matchup stem needs the deck's catalog. A missing optional input still just does not hold.
  (4) `CODES` gains four, appended in this order (21 total): `unknown-decision-kind` (`answers[].decision` not in `schema.DECISION_KINDS`, the sorted values of `decisions.ANSWERABLE_KINDS`, all 18 engine `DecisionKind`s); `unknown-cast-cost` (`cast.cost` not in `schema.CAST_COSTS`); `duplicate-step` (`when.steps` names a step twice); `wildcard-with-keys` (`opposing_archetypes` holds `*` and has more than one entry, `["*","*"]` included). `forbidden-kind` now also covers an answer for `declare_attackers`, `declare_blockers` or `mulligan`. The last two are field validators, so they fire only when that list has no bound or item error of its own. A card name in `decision` now reports `card-name-in-field` and `unknown-decision-kind`.
  (5) `cast.cost` is optional, a closed enum: normal, adventure, airbend, bestow, dash, disguise, escape, evoke, flashback, foretell, harmonize, impending, madness, mayhem, megamorph, morph, plot, printed, warp. These are the engine's cast option keywords, lowercased (`_option_from_keyword`, plus `keyword_cast_options`' Adventure and `marker_cast_options`' Airbend), plus `printed` (`AlternativeCostSource.PRINTED`, a pitch cost announced as `CastRequest.alternative_cost`), `normal` (the printed mana cost) and `impending` (CR 702.176a; the engine offers no impending cast yet, so a runner refuses it like any cost no live cast option offers). `tests/test_playscript_schema.py` reads the engine source, so a new engine cast option fails it until named.
  (6) New `bounds-exceeded` bounds: the selector word lists (`types_all`, `types_any`, `subtypes_any`, `supertypes_any`, `colors_any`, `keywords_any`) max 15 (CR 205.2a's 15 card types); `cast.targets` max 6; `pick.prefer` max 4; `opposing_archetypes` max 32 (the largest analysis run has 19 matchup stems); an `in`/`not_in` input value list max 32 (a runtime check, pydantic error type `too_many_input_values`, since `value` is untyped in the JSON Schema). Every array in `contract/playscript.schema.json` now has `maxItems`.
  (7) D13: while a command is paused, `serialize.state_view` replays it (`commands.paused_game`, about 3 ms, read only, never committed). It merges that half-applied game's `known_to` records for cards that are still the same id in the same zone (and, in a library, the same position) of the committed game. So Thoughtseize's revealed hand is known while its choice is pending, and redaction is unchanged. Scry/surveil/look-at-top-and-split looks surface the same way once the engine records the look before asking (engine `_look_before_choosing`, same date). Wire changes (`faces`, non-null double-faced `oracle_text`, known records while paused) are folded into the seatkit-17 paragraph, with no bump.
- **2026-09-13 - the D2 script format lives in the repo too.** The DSL design doc was only a session scratchpad file, outside every repo, so its `cast` line went stale when `cast.cost` landed. `docs/superpowers/specs/2026-09-13-play-script-format-design.md` now carries the D2 format (with the D6 bounds, `cast.cost` and the closed `answers[].decision`), and `tests/test_playscript_format_doc.py` pins its cast-cost, decision-kind and never-answered lists to `CAST_COSTS`, `DECISION_KINDS` and `FORBIDDEN_DECISION_KINDS`, so a schema change fails until the doc follows.
- 2026-09-13 F2v-r1.1: D13's surveil case closed in the engine, not the api: `Game.scry`/`surveil`/`look_at_top_and_split` now record the chooser's look before the question, so `commands.paused_game` holds it and `serialize._known_entries` merges it unchanged. Tests: `test_a_scry_look_is_known_while_the_scry_is_pending`, `test_a_surveil_look_is_known_while_the_surveil_is_pending` (Otherworldly Gaze). Needs the engine commit on `f2v-dsl-view-engine` to land first.
- 2026-09-13 F2l (battle 1 D06, D09-D12): `plays check` lint contract changes, which the mtg-decks follow-up mirrors byte for byte in `scripts/check_plays.py` and whose corpus it copies back (sha256 9f286f4f6c59b4e7b7a2acbd9cfb2c4f92fa1648b79fd9302a85735dc77a7adc).
  - New error code `never-applicable`, appended last to CODES (22 codes, after F2v's four). The api keeps `schema.CODES` at 21 and adds it in `lint.CODES` with `lint.CorpusError` (so `schema.py`, owned by F2v, is untouched); stdlib just appends it to its CODES tuple. Raised per play JSON object, right after the input errors, in this order: (1) `when.stack == "empty"` and a `true` entry of kind `stack_top` - one error naming the first such entry: `when.stack is empty but true[i] needs a stack_top; the play can never apply`; (2) walking object conditions true list then false list with their original indexes, each normalized as `(holds, json.dumps(_canon(condition), sort_keys=True))` with the land_drop_available value-false flip, the first place whose opposite `(not holds, text)` was already seen - one error per distinct text: `{first_where} and {where} contradict: {text} must both hold and not hold, so the play can never apply`.
  - D06 decided from the stdlib contract table: a non-blank string archetype key failing only the api's `ArchetypeKey` pattern (`esper_blink`) is `unknown-archetype-key` alone; lint drops the schema's `malformed-play` at `opposing_archetypes[i]` for a non-blank string entry. A blank key stays `malformed-play` + `unknown-archetype-key` in both. Runtime `Play.model_validate` still refuses a non-kebab stem; analysis stems are kebab in practice.
  - Conflict exclusion changed: UNREADABLE_CODES is gone. A play is left out of play-conflict and next-conflict when ANY error is reported with its file's path (structural, id, name, phrase, input, never-applicable, unknown-archetype-key, unknown-next, next-cycle, chain-too-deep), computed after the graph errors; deck-level (`plays/` dir) and inputs.json errors exclude nothing.
  - D09: `next-cycle` no longer returns early. `chain-too-deep` is computed for every play not in a cycle, with edges into cyclic plays ignored (cycle g-a/g-b beside chain g-d..g-g: next-cycle x2 plus chain-too-deep on g-d; x->y->{z,a-in-cycle} counts 2). next-cycle errors are emitted in play order.
  - D11: the `opposing_archetypes` dimension compares effective archetype sets. A play's set is None (every archetype) when its string list is empty or holds `*`, else the list's set; then it is intersected with every `("values", allowed)` input constraint named `opposing_archetype` from `_constraints` (so a false-list condition is negated first). Proven when the intersection is empty (None meets X gives X; None meets None never proves). A list's `*` admits every asserted value including `*`; a named list admits only its keys.
  - Corpus: description and the `opposing_archetypes` dimension rule text rewritten; new verdict `errored` (fields `errors` = codes raised against fragment b's file, gate exit 1 and no warnings, plus `proven_by`/`dimensions` from the pure algorithm); CHANGED case `input-built-in-archetype-eq-vs-eq` proven_by `["input"]` -> `["opposing_archetypes", "input"]`; NEW cases, each with proven_by and dimensions (all dimensions are the base four): `archetypes-list-vs-built-in-input-disjoint` (exclusive by opposing_archetypes), `archetypes-list-vs-built-in-input-false-list-negated` (exclusive), `archetypes-list-narrowed-by-its-own-built-in-input` (exclusive), `archetypes-list-vs-built-in-input-overlapping` (conflicting), `errored-unknown-archetype-key-is-left-out-of-conflicts` (errors unknown-archetype-key), `errored-never-applicable-empty-stack-and-stack-top` and `errored-never-applicable-one-condition-true-and-false` (errors never-applicable).
  - D12, docs only: a phrase equal to a card name, basic land names included (`oracle_has: "Island"`), is `card-name-in-field`; `subtypes_any: ["Island"]` is the intended form (lint and package docstrings).
- 2026-09-13 A12/F2l integration fix: `admit._conflicts` was linting the before/after ratchet corpus with a hardcoded `CardIndex({})`, so every `ability_text_has`/`oracle_has` phrase came back `phrase-too-narrow` and F2l's errored-play exclusion then dropped every play from conflict computation - the ratchet compared empty-vs-empty and never refused a real regression (`test_a_change_adding_a_conflicting_pair_is_refused` and two siblings in `tests/test_plays_admit.py` went from `conflict_regression` to `admitted`). Fixed by materializing `meta/card-data.json` from the same rev (`git.show(f"{rev}:meta/card-data.json")`) into the temp corpus and loading a real `CardIndex` from it, exactly like `plays check`'s own `run_check` does; a rev missing that file now raises the existing `gate_failed` result naming the file rather than silently falling back to an empty index (`test_a_clone_without_card_data_fails_closed`, `seed_guides(..., card_data=False)`). In practice `_gate`'s own `plays check` subprocess already fails closed first whenever the working tree has no card data at all, so that new test mostly exercises the pre-existing gate path - the ratchet's own check matters for the narrower case where a rev's committed tree lacks the file while the working tree (post-rebase, same content) does not diverge, which is the same fact in this codebase's normal flow but is still worth failing closed on directly rather than assuming. Also rewrote `test_resolving_a_conflict_by_extending_a_condition_is_admitted`: the old body added `life me >= 8` to `b.false` while `b.true` already had it verbatim - F2l's own-list contradiction rule (`_never_applicable`, exact-literal opposite-polarity match) now catches that as `never-applicable` and the test regressed to `gate_failed`. Rewritten to narrow `b.true`'s existing life condition to `<= 5` (replacing, not adding beside, the inherited `>= 8`) so the pair is proven exclusive via the `numeric_range` dimension without `b` contradicting itself - `_never_applicable` only catches identical-condition polarity flips, not a numeric-range self-contradiction, so an *added* second life bound would not have been caught by lint even though it is semantically impossible; replacing is the clean fix, not a lint-passes-so-it's-fine one.
- 2026-09-13 G3 (battle 2 B2-08, B2-09, B2-10, B2-12; rules mirrored by mtg-decks `scripts/check_plays.py`):
  (1) B2-08: `AbilityView` gains `mana_amount` and `mana_cost_value` (both `null` for a non-mana ability; seatkit-17 paragraph extended, no bump). `mana_amount` is what the ability adds if activated now: a bare `{T}` ability is read off the engine's public `machines.cost.mana_candidate_scan` (board-resolved conditional clauses - Urza's Tower 3 under Tron, Mine and Power Plant 2 - CR 106.7 survey, chosen colour, CR 106.5, CR 302.6, CR 602.5), and a row the scan leaves out is 0 (a tapped source too). Any other mana ability (a filter, a life or sacrifice cost) is 0 when `{T}` and tapped or `can_tap_for_mana` is false, or `Game.why_cannot_activate` refuses, or `unparsed_rider` is set; otherwise `ManaAbility.effective` with the permanent's own narrowings and `among_colors`. The api deliberately does not call `Game._controlled_land_types`/`_could_produce_survey` (private), which is why the filter path has no land types; no filter prints a land-type conditional. Count = `any_color_count` for any colour, else the longest option, devotion via `devotion_to` (max over the colours). `mana_cost_value` = sum of `ManaSymbol.mana_value` over the cost's `CostKind.MANA` pieces (`{1}` 1, `{W/U}` 1, bare `{T}` and intrinsic lands 0).
  `untapped_mana` counting rule (`conditions.untapped_mana`): sources are `seatkit.untapped_mana_sources`; each source activates at most one of its mana abilities with `mana_amount` > 0; restricted mana counts in full (CR 106.6 restricts spending, not making); a `mana_cost_value` 0 ability adds `mana_amount` to the pool; a filter (`mana_cost_value` > 0) is activated after every free ability, in ascending cost order, only when the pool holds its cost, adding `mana_amount - mana_cost_value`; the answer is the largest pool over every choice of one ability or none per source. Only sources with a filter netting more than their best free ability are enumerated (both ways, `itertools.product`); every other source adds its best free amount, which is exact because none and a no-better filter are dominated - a naive take-or-skip walk hung on a 44-source board. Filter cost colours are not judged. Tron 7, Tron + Eldrazi Temple 9, Island + Mystic Gate 2, Island + two Mystic Gates 3, Captivating Cave + Island 2, summoning-sick Llanowar Elves 0.
  (2) B2-09: `colors_any` `colorless` (or `c`, any case) matches a view whose `colors` is empty (CR 105.2c) and never a colored card. The closed vocabulary is `selectors.COLOR_LETTERS` (white, blue, black, red, green, colorless, by name or letter); the format doc's `- Colors:` line is pinned to it. The schema still accepts any non-blank word there; an unknown word matches nothing.
  (3) B2-10: `CODES` gains `answer-shape-mismatch` (22nd, appended). `schema.AMOUNT_DECISION_KINDS` = the kinds `decisions.ANSWER_FIELD_FOR_KIND` answers on `amount` (`pay_energy_amount`, `pay_life_amount`). An answer item gains `amount`: an integer >= 0 or `"max"` (`AMOUNT_POLICIES`, the decision's own ceiling). On an amount kind the answer must carry `amount` and none of `pick`/`accept`/`to_graveyard`/`to_bottom`; an `amount` on any other kind (in `DECISION_KINDS`) is refused; all three are `answer-shape-mismatch`, reported by `Answer`'s model validator and by the `_answer_shape_mismatches` supplement (so it still fires beside another error in the same answer; a decision outside `DECISION_KINDS` is left to `unknown-decision-kind`). A negative amount is `bounds-exceeded`; a bool, float, string other than `max`, or null is `malformed-play`. A target (`cast.targets` and new `activate.targets`, 0-6) is `{select, side}` or `{player: me|opponent}` (`CardTarget`/`PlayerTarget`, discriminated by the presence of `player`); anything else is `malformed-play`, a key beside either shape `unknown-field`. `activate.sacrifice` is `source`, `none`, or `{select, count?: >=1, prefer?: [0-4]}` (`SacrificeChoice`, Pick's shape: permanents the activator controls). Shape-union tags (`amount-count`, `amount-policy`, `card-target`, `player-target`, `sacrifice-keyword`, `sacrifice-choice`) are skipped by `_walk`/`_last_field`, so locations read `moves[0].sacrifice.count`. Lint needed no change: `check_play` runs `play_errors`. Existing test `test_every_answerable_decision_kind_validates` now leaves out the amount kinds (it answered them with `accept`). `tests/fixtures/conflict_corpus.json` unchanged: no case uses amounts, targets on activate, sacrifice or colors.
  (4) B2-12: already closed by F2v for transforming cards (battle 2 ran on 4aed3c8, before F2v; its snapshots show `faces: null`). `tests/test_transform_dfc_card_view.py` pins it on real boards: Ajani, Nacatl Pariah in hand, graveyard, exile and library contents is only his front face (not a Planeswalker, CR 712.8a); front face up on the battlefield the Pariah (CR 712.8d); transformed the Avenger with non-null `oracle_text`, Planeswalker, red, mana value 2 (CR 712.8e); Delver of Secrets the same. CR 712.8a/d/e and 105.2c, 106.1b, 106.6, 118.3c, 302.6, 605.1a read from the engine's `.rules/MagicCompRules.txt`.
- 2026-09-13 A5: play-script runner (`playscripts/runtime.py`). Two non-obvious facts from a real board: (1) the opponent pass that starts a resolution is itself logged `pass_priority` with `paused: true`/`paused_on`, byte-for-byte the shape of the pause-slot clobber re-post, so "interrupted" treats another player's pass as foreign only while the run is mid-resolution (it answered a decision and the resolution has not stopped asking); (2) a library search's `pending_decision.card_details` is empty, so selectors read a candidate's characteristics from the seat's own `library_contents` (`include=contents`), joined on `card_labels` - the label is a join key, never a behaviour key. A resume token with `move_index` 0 was cut off while waiting and commits nothing, so it continues the lifecycle; any later token is refused once turn or step moved.
- 2026-09-13: playscripts/runtime.py red phase replayed by removing each behaviour and watching its test fail (41 removals, all red). It found three tests nothing covered (prefer tie-break, log-only and stack-only foreign checks) - added. When replacing a module in a loop, delete its __pycache__ pyc per mutation: same-size edits reused stale bytecode.

- **2026-09-13 - `pending_decision.source_card_id`** carries the engine's `Decision.source_id`; `visibility.redact_pending_decision` nulls it when the source is in a zone the viewer cannot see (libraries, sideboards, other hands), and `repository_postgres` round-trips `source_id` too. Additive response field an older MCP build passes through untouched, so no `PROTOCOL_VERSION` bump.
- **2026-09-13 - paused action responses are redacted too.** The `pending_decision` an acting route hands back when it pauses (`_respond`'s routes, `concede`, `pass-priority`, the declaration aliases, `decide`) goes through `app._paused_pending_view` for the actor the route already trusts (`req.player`, or the uid-resolved deciding seat): bob's pass that pauses on alice's question gets `{player, kind, prompt}`, and a source in a zone the actor cannot see is nulled. Tests that read alice's candidates out of bob's response now read her own view.
- **2026-09-13 - small decisions anchor to their card.** `ui/render/decision_popover.js` answers the kinds `decisions.js`'s `usesDecisionPopover` names in `#decision-popover` (its own element, never `#popover`: the card menu closes on outside click and Escape, a required decision does neither) beside the `.card[data-card-id]` / `.stack-object[data-key]` that `source_card_id` names. `popoverSubject` is shared with `renderPromptBox`, so the ribbon falls back only when that lookup fails. Cards inside `#modal` never anchor (the modal paints over z 50), and a zero rect counts as not rendered. The content is `renderDecision` rebuilt only when the view model JSON changes, with focus and caret restored; placement writes a style only when the value differs, so an unchanged poll touches nothing.
- **2026-09-13 - battle 1 D14/D15/D16 (seat CLI and audit).** `engine_api.locate_land` resolves a name through `Card.land_face` (every face), so an MDFC plays by its full name or any face name; `is_land` alone reads only the front face. A malformed id is refused as `malformed_card_id` naming the value and the 36-char form: `app._validation_error` rewrites pydantic's `uuid_*` errors (adding `allowed: false`/`reason`, dropping the `UUID | ManaSource` union's sibling branch error; every other 422 keeps its old shape), `decisions.parse_id` says the same for `decide` ids, and `seatkit.card_reference` sends an id fragment as `card_id` rather than a name from both front ends. A "player name or card id" token (a cast/activate target, a `decide` target, an attack defender) that is neither a seat name nor a UUID is refused by `decisions.player_or_card` naming the id form too, before any candidate check, since it may be a truncated id or a misspelt name. `run_action`'s auto-release now posts a pass only when `priority_holder` is the seat, so a decision answered outside priority no longer writes a refused `pass_priority` row into the eval tallies; MCP defaults unchanged.
- **2026-09-13 - a paused announcement serves the board it paused over (battle 2, B2-13).** `commands.run_command` now returns the paused working copy as `RunResult.board`, and `commands.paused_board` replays the stored pause to get it (`pause_is_live` is that call `is not None`). `serialize.state_view` shows it only when the paused command is in `ANNOUNCING_KINDS`: a cast trigger's target and order questions come after CR 601.2a/601.2h, and serving the committed game showed the card in hand and the lands untapped under them. Resolution and turn-based pauses still serve the committed board, because `advance`'s docstring and `seatkit.pass_until`'s combat stops (`tests/test_autopass.py`) read the step `advance` was called from. The replay costs about 50 ms per state read while such a pause stands; nothing is cached, since board-setup routes mutate the in-memory committed game in place.
- **2026-09-13 - battle 2 B2-02/B2-03 (double-faced casts).** The engine's `legal_actions._base_cost_options` reads a `transform`/`modal_dfc` printing's blank top-level `mana_cost` and reports CR 118.6's unpayable cost, while `Casting.base_cost` charges the front face. `action_rows.legal_action_rows` (now what both `GET .../legal-actions` and `records.legal_actions_before` serve) rebuilds that plain cast row through the engine's own `_cast_action` with the front face's cost; the card view and `cast_hints` fall back to `CardState.mana_cost`/`oracle_text` only when the printing's top-level field is blank, so split cards keep their joined cost. `engine_api.locate_castable` matches a card's full name or its front face's name (CR 712.8a); a name two different cards answer to is refused `type: ambiguous_card_name` with `candidates`. Noted for the engine repo: `_base_cost_options` should fall back to the front face the way `base_cost` does.
- **2026-09-13 - battle 2 B2-06 (optional additional costs), protocol seatkit-17.** `CastRequest.optional_costs: [{keyword, times, choosing, opponent}]` drives `Casting.pay_kicker`/`pay_replicate`/`pay_offspring`/`pay_blight`/`pay_gift` right after the alternative-cost announcement; cast rows carry `optional_costs` (keyword, rule, printed cost, repeatable, affordability of the row plus that cost). CLI `--optional-costs` and MCP `optional_costs` share `seatkit.parse_optional_costs` (`Kicker`, `Multikicker:2`, `Gift:bob`, `Blight:<id>`). Bumped to seatkit-17 because an older MCP build cannot send the choice; the DSL integration branch also claims seatkit-17, so the two need reconciling on merge.
- **2026-09-13 - battle 2 B2-14 (mana rows in context).** Mana rows resolve `produces` on the board via `engine_api.effective_production` (the same land-type/counter/imprint/survey/chosen-colour narrowings `Game.activate_ability` uses) and keep the printed phrase as `printed_produces` when they differ; `_narrowings` now fills land types from `Game._controlled_land_types`, so naming a Tron land by `ability_index` pays with its real output. A cast row the engine calls unpayable is re-checked by activating each untapped `{T}`+mana ability (up to 16 trials, pairs included) on a `dry_run_copy` through `engine_api.activate_ability`; if `plan_mana_payment` then pays, the row is `affordable: yes` with `activate_first`, and `baseline_seat._plain_cast` skips such rows. Dead end: Mystic Gate-style pair filters (`{W}{W}, {W}{U}, or {U}{U}`) cannot be activated at all - the engine's `ManaAbility.resolve` takes one colour, which never equals a two-mana option - so the test uses Darkwater Catacombs (recorded into `tests/fixtures/scryfall`). That is an engine gap.
- **2026-09-13 - battle 2 B2-16 (counters while paused).** A pause is never committed, so the state view showed pre-command player counters. `commands.board_at_pause` replays the stored tape (sharing `_attempt` with `run_command`) and `serialize._counters_at_pause` overlays each player's counters, cached per (game, pause generation, committed counters) so a poll replays once per question.
- **2026-09-13 - battle 2 B2-06 review (entwine, escalate, harmonize).** `optional_costs` also takes `Entwine` (fed to `Casting.choose_modes(entwined=True)`, so an entwined request always announces modes and the engine refuses a short list), `Escalate` (`_announce_modes` calls `Casting.pay_escalate` whenever an escalate spell is announced with more than one mode, named or not - CR 702.120a's cost is owed by the mode choice; naming it hands over a non-mana cost's objects, `Escalate:<card id>`) and `Harmonize` (`Casting.pay_harmonize` with at most one creature, only on a `keyword: Harmonize` cast). Rows offer entwine/escalate on modal casts and harmonize only on the harmonize row, whose tap is `affordable: unknown` unless the row already pays. `times` other than 1 on any of the three is refused. Noted for the engine repo: `Game.cast` never calls `pay_escalate`, so an engine-side multi-mode escalate cast is still undercharged. Wire shape unchanged, seatkit-17 kept.
- 2026-09-13 A10: the driver registers `decks/<slug>/plays/*.json` plus `plays/inputs.json` once at dispatch (seat and advisor uid) via `PUT .../seats/{uid}/scripts`, which now takes and reads back `inputs` (judged as `malformed-inputs`). Bad files are skipped per file as `PlaysError` notices, logged and listed in the briefing; any registration failure, `scripts_disabled` included, never fails the dispatch. Gotcha: `tests/test_prompt_matches_tools.py` reads any `name(arg)` in `driver/prompt.py` whose args are real tool parameter names (`text`, ...) or `kw=` as a tool-call example - so `len(text)`, `@dataclass(frozen=True)` and `field(default_factory=...)` there fail it. Keep such Python in `guides.py`.
- 2026-09-13 A5i: the runner catches up with F2v and G3. (1) `cast.cost` goes out as `CastRequest.alternative_cost`, not `keyword`: the engine lowercases and matches either the option's keyword or its `source_name`, so `evoke`/`impending` reach keyword options as-is, and `printed` is sent as the chosen card's name (a printed option's `source_name` is the spell's name, its keyword None); `normal` sends nothing. (2) `sacrifice: source` sends `sacrificing: [source id]` - a probe showed the engine accepts naming the source for Flooded Strand's "Sacrifice this land" (and also accepts none), so naming it is harmless and explicit; `none` omits the field and the engine refuses a real sacrifice cost in its own words. A left-out sacrifice `count` is the number word after "Sacrifice" in `AbilityView.cost` (`a`/`an`/`another` = 1), the only view field that names it. (3) Amount answers post on `pending_decision.answer_field` (falling back to `decisions.TOOL_FIELD_FOR_KIND`), `max` = the decision's `amount`. (4) Invalid inputs: the format doc had no exit list, so `invalid_input` was appended last to the closed `Exit` enum (13 exits) and the doc gained a pinned `- Exits:` line; the exit carries `invalid_input: {name, value, reason}`, `failing_condition` None, and a `stop_reason` naming the input and value, which is what the finish record stores. `_next` checks it too, because a resumed run skips its own conditions and a chained play is the first to see the new inputs.
- 2026-09-13 A6: `run_script`/`list_scripts` (MCP) and `mcp-seat script run|list` share `playscripts/script_tools.py`. Non-obvious choices: `max_seconds` 0.0 means the runner's own cap (`runtime.RUN_CAP_SECONDS`), since a 0 deadline would stop a run before its first move; `script_exit.unvalidated` names moves by index and kind only, because a move's selectors are unbounded strings and echoing them broke the 45 KB answer bound; a cut-off run's `script_exit` has `exit: null` plus `resume`. `list_scripts`' static conflicts run `lint._conflict_warnings` over the registered corpus with the built-in archetype enum rebuilt from the stems the corpus names plus one `~any-other-matchup` value (a corpus carries no analysis run), so two plays each excluding a different named stem still conflict. Declared inputs are read from the corpus's `inputs` key when the scripts route returns one (A10), else none.
- **2026-09-13 - engine #15 look-split count drift.** Engine 9947808 made `Game.ask` enforce `Decision.amount`/`exact` on every `CardSplit` (CR 608.2d). The engine's real `scry`/`surveil` pass `exact=False`, but `Decision.exact` defaults to `True`, so a hand-built SCRY/SURVEIL decision without `exact=False` reads as "exactly N". The drift test now builds them with `exact=False`, and `validate_answer` mirrors the count check word for word (`test_look_split_count_matches` covers the exact and at-most shapes).
- **2026-09-13 - battle 3/4 API fixes (H2).** Equip (B3-01) is `POST .../actions/equip` (`EquipRequest`: `card_id`, `target`, `mana_sources`), a `kind: equip` legal-action row built from `machines.keywords.equip_activation` (cost, `why_not`, target slot from `profile_for_text` on the synthesized ability), `seat_cli equip <card_id> --target` and the MCP `equip` tool. `ActionRow.kind` widens to `ActionKind | "equip"`; the index-aligned engine `Action` is an `activate` with `keyword: Equip`. The route checks the target against those requirements before paying, because `equip_activation` itself accepts an illegal target and only fizzles on resolution.
- **2026-09-13 - rows filtered and re-priced in `action_rows`, all from engine reads.** B3-04: an off-zone row is dropped when `off_zone_abilities(card)[i].zone` is not the card's zone or the seat is not its owner (CR 602.2/108.4a); a battlefield row is dropped when a self-exile/self-discard cost component (`Game.activation_cost`) names another zone (CR 113.6m). B4-05: a loyalty ability that adds mana is `activate`, `is_mana_ability`/`mana` false (CR 605.1a), and `engine_api.without_loyalty_mana_advice` strips the engine's "activate it manually" clause for loyalty abilities from row reasons and from every command refusal (`commands.run_command`). B4-02: cast rows are rebuilt at `apply_modifications(printed, static_cost_modifiers_for, cost_reduction_for(targets=[]))` with `printed_cost`. B3-05: `unreadable_modes` from `parse_mode_effect` (the reading `resolve_modal_spell` uses), and a cast choosing one is refused before payment.
- **2026-09-13 - ledger and reads (H2).** B3-11: `records.note_action_board` (ContextVar keyed by game) labels `turn`/`step`/`phase` with the board the action was taken on - the committed board in `_run_command`, `RunResult.asked_on` (the divergence guard's paused copy) in `_resume`. B3-15: the cast row's `from_zone` is `engine_api.cast_origin_zone`. B4-09: `engine_api.stamp_exile_turns` fills `turn_exiled` for imprint/hideaway at commit. B3-08: `serialize._paused_overlay` (replaces `_counters_at_pause`) gives every non-announcing pause the paused board's step, turn, active player, priority holder and player/permanent counters; `test_autopass` now expects the declare-attackers/blockers step during those pauses. B3-10: `run_action` answers a pause on another seat's decision with `_waiting_on_another_seat`, never "a decision is waiting for you". B3-09 was already closed on main by #26's D16 check (the battle tree `01dddfc` predates it); pinned by a regression test only. Protocol bumped to `seatkit-18`.
- **2026-09-13 - engine gaps found in battles 3/4, left for the engine.** (1) `legal_actions` enumerates no equip and `ActionKind` has no `EQUIP`; `equip_activation` does not check "target creature you control" at activation (CR 602.2b/601.2c). (2) `_battlefield_ability_actions` and `cost._withheld_mana_sources` treat a loyalty ability that adds mana as a mana ability (CR 605.1a). (3) `abilities._activated_ability_zone` misses "Exile this card from your graveyard" (CR 113.6m): Seasoned Pyromancer's ability has `zone None`, so it is never offered from the graveyard at all. (4) `_off_zone_actions` offers shared-exile cards to every seat and reports a wrong-zone ability as `unknown` when targets are missing. (5) imprint (`effects.py` imprint clause) and hideaway (`effects.py`, `keywords.py`) build `ExileAnnotation` without `turn_exiled`. (6) cast rows are priced at the printed cost, not `Casting.total_cost`. (7) Thraben Charm's mode 3 ("Exile any number of target players' graveyards") is also unreadable by `parse_mode_effect`, not only mode 1. Each API fix above is a no-op once the engine does it.
- **2026-09-13 - B4-02 review: X rows, cost-free alternatives, unmatched rows.** `action_rows.charge_schedule` runs `Casting.total_cost`'s own `apply_modifications(printed.with_x(x), ...)` at X = 0, 1, ... until one step adds exactly the X symbols' generic mana (from there a floor no longer applies and a generic reduction is covered, so it stays linear). The X below that point go out as `x_costs` (`{x, cost, mana_cost}`); from it on `cost`/`mana_cost` keep the X (`{X}{1}{R}` under Sphere, `{X}{X}` for Ballista under Trinisphere past X=1). When a reduction eats into X's own mana (Ruby Medallion on Meltdown) no mana cost says it, so `cost` is `{X}{R} less {1}` and `mana_cost` is left off. A row with no mana part (Solitude's evoke) is taxed too (CR 118.9d). A row whose cost strings match no cast option keeps the charged mana plus its component text and is `affordable: unknown`, never the printed cost. Activation rows need nothing: the engine charges `Game.activation_cost` (its own "costs {N} less to activate" included) and no board-wide activation modifier exists in the engine, and the rows are built from that same call - pinned by tests on Takenuma's Channel, Walking Ballista and equip. The cast and activate dry runs' `cost` is the payment's mana only, so the tests compare it to `mana_cost`. `x_costs` rides the unmerged seatkit-18 bump.
- **2026-09-13 - battle 5 B5-03 (zones at a resolution pause).** `serialize.state_view` now reads zones and the players block off `commands.board_at_pause` for every pause that is not an announcement (resolution, turn-based), while combat and the stack stay the committed game's (CR 608.2n removes a resolving ability from the stack only as the final part of its resolution; the replay has already popped it, which the payload fixtures caught); on rebase over H2 the step, turn, priority holder and counters come from H2's `_paused_overlay`. Overlord of the Balemurk's "mill four, then you may return" asks with the four already in the graveyard. The replay is uncached, like B2-13's announcement board: about one engine replay per state read while such a pause stands.
- **2026-09-13 - battle 5 B5-05 (named mana sources only).** CLI `cast`/`activate --sources` and the MCP `cast`/`activate` `mana_sources` send only the sources named; `""` or omitted sends none. `engine_api.pay_with` then runs `CostPayment.auto_tap` over every untapped permanent `payment.payer` controls, so the REST route itself pays a request that names nothing (it used to refuse "pool {} cannot pay"). A fallback that finds no plan is swallowed so `pay` refuses with its own reason (a CR 106.6 restriction sentence, not "no combination"); named sources still raise as before. Mana ability activations carry no `tapped`. Every payment's success detail carries `tapped` (as a dry run always did), and `app.audit_outcome` logs those ids as the row's `mana_sources`, so a row names the permanents that paid - `[]` for Flooded Strand or an evoke. `suspend`/`plot`/`foretell`/`cycle` front ends still offer every untapped source through `Seat.sources_or_all`; their rows are still correct because the log records payers.
- **2026-09-13 - battle 5 B5-10 (activation rows carry target slots).** The engine's `legal_actions._battlefield_ability_actions` and `_off_zone_actions` never set `Action.target_slots`, so every activation row read `[]` (Goblin Bombardment, Vampire Hexmage, Faerie Macabre alike). `action_rows._Rows.with_target_slots` fills them from `affordances.activation_profile`: `Game._announcement_profile(ability_index=...)` on the battlefield, and for `ACTIVATE_OFF_ZONE` the printed zone ability's whole text in `off_zone_abilities`' index space (cycling, unearth, transmute target nothing). `affordances.target_candidates(off_zone=True)` reads the same profile, since `Game.activated_abilities` is the wrong index space for a hand card. Noted for the engine repo: `legal_actions` should build activation `target_slots` itself, as `_cast_action` does. This reverses the boundary `tests/test_legal_actions_endpoint.py` used to pin (Karn, the Great Creator's `+1` with no slots, "belongs in the engine"): the owner asked for the rows now, and the slots come from the engine's own profile call, not a repo-side text reading.
- **2026-09-13 - battle 6 B6-05 (improvise), protocol stays seatkit-18.** `CastRequest.improvise: list[UUID]` goes to `objects_as_costs.improvise` in `engine_api.apply_object_costs` beside convoke and delve (CR 702.126b: after the total cost is determined); CLI `--improvise`, MCP `improvise`, `object_costs.improvise` candidates on affordances. A cast row the engine leaves `unknown` with "improvise ... is not evaluated" is re-asked in `action_rows._Rows.offer_improvise`: the engine's `improvise_offer().why_not` picks the controller's artifacts (no-mana ones first), one generic comes off per artifact via `ManaCost.minus`, and `plan_mana_payment` pays the rest from every source not improvising; `yes` names them in the row's `improvise`, otherwise the engine's own `no` comes back. The raw cast route pays only from `mana_sources` it is handed, so a caller passes the lands too.
- **2026-09-13 - battle 6 B6-06 (API half): a finished game refuses every action.** `commands.GameOverError` (409, body `type: game_over`, `allowed`/`accepted: False`, `winner`, `drawn`) is raised by the `REFUSED_AFTER_GAME_OVER` route dependency on all 21 action/decide/pregame routes - a dependency so it runs before body validation and before `records.require_current_board`, whose `BoardMoved` would otherwise tell a seat whose `expected_seq` predates the `game_over` row to re-read and retry - and again under the lock in `_run_command` (concede and dry runs included) and `_resume`. `legal_action_rows` returns no rows for a finished game. Engine-side refusal (task K1) is separate; this does not rely on it.
- **2026-09-13 - battle 6 B6-09: a dry run is no longer exempt from the pause guard.** `_run_command` asks `_pause_slot_guard` about a dry run as the real command it probes (`dry_run: False` in the payload), with `clear_stale=False`, so it gets the same `DecisionInProgressError` 409 (or the standing pause, for the paused command itself) and still never writes the slot. Consequence: `seatkit.available_actions`' per-card probes during another seat's pause now read as refusals, which is what the real calls would be.
- **2026-09-13 - merge of main (H2, J2, #31, #33, #34) into script-dsl.** Both sides had claimed `seatkit-18`; main's equip/re-priced-rows paragraph keeps 18 as main wrote it and the DSL paragraph is renumbered `seatkit-19` (`test_seatkit_18_stays_mains_equip_paragraph` pins the split). `state_view`'s `known_cards` and `library_contents`/`sideboard_contents` now read off J2's `board` like every other zone, so during a resolution pause they describe the same board the zones show. The merged MCP tool set is 22 (19 plus `run_script`, `list_scripts`, `equip`).
- 2026-09-13 K3 (battle 6 B6-01, B6-07). (1) `inputs.inputs_problems` is no longer a pydantic model: `PydanticCustomError` substitutes a bare `{key}` from its context only, so `{name!r}` went out verbatim, and the model validator raised on the first name problem (and never ran beside a field error). It is now a line-for-line port of mtg-decks `scripts/check_plays.py` `_inputs_problems` (identical at 4ff06d9 and the G3 mirror 6e402ca): every problem, document order, check_plays' exact words - so pydantic's `Extra inputs are not permitted` is now `inputs[3].default: is not a field of a 'int' input`. The PUT scripts route (`records.inputs_errors`), `plays check` and the admit gate (which runs `plays check`) all read it. `load_inputs` returns the file's list as-is. Parity doc (redeclared built-in, duplicate `plan`, int with an out-of-range `default: 99`) pinned in `tests/test_playscript_inputs_messages.py` from a real check_plays run; the same file AST-scans every `PydanticCustomError` in `src/` for a placeholder that is not a bare context key. mtg-decks mirror needs nothing for this: check_plays already reports all, in these words. (2) `untapped_mana` excludes any mana ability whose view carries `restriction` (engine `spend_restriction`, understood or not), filters included; restricted mana is never counted rather than counted when the move can spend it, because which spell a move resolves to is a run-time fact a static check cannot see. Eldrazi Temple counts its plain `{C}` (Tron + Temple 8, Temple + Island 2); off-pool Tablet of Discovery counts its `{R}` only. Supersedes G3's "restricted mana counts in full". check_plays does not evaluate conditions, so the mirror needs only its `untapped_mana` prose, if it carries the old rule (it does not at 6e402ca).
- 2026-09-13 K3 review fix. `ScriptsRegisterIn.inputs` is `Any` (OpenAPI still an array of objects via `WithJsonSchema`): typed `list[dict]`, a non-list `inputs` (`{"x":1}`, `null`, `[1]`) never reached `records.inputs_errors` and went out as pydantic's generic `loc: [body, inputs]` 422 instead of `invalid_scripts` / `malformed-inputs` (`inputs must be a list`, `inputs[0]: must be an object`). Same reasoning `scripts` already had.
- 2026-09-13 I3 (battle 4 B4-08): `never-applicable` also catches numeric range contradictions inside one play; mtg-decks I3m mirrors it in `scripts/check_plays.py` and copies the corpus back (sha256 95ba65dc55a62f13169ddcb5c16221bb3313e97fcf48e53b4894d0725a91f049). No new code (CODES stays 23).
  - Rule (3rd pass of `_never_applicable`, after the stack_top pass and the identical-literal pass, so its errors come last): walk object conditions true list then false list with original indexes (`true[i]`/`false[j]`). Each condition's numeric constraint is exactly the one `exclusivity`'s `numeric_range` dimension uses (now `lint._constraint`, shared by `_constraints`): a count/zone_size/life/untapped_mana condition with op in OPS and int `n` keys on `json.dumps(_canon(condition minus op and n), sort_keys=True)`, range `_ints(op, n, floor, None)` with floor None for life and 0 otherwise; a readable int input declared in the deck's catalog and with no `invalid-input-value` problem keys on `json.dumps({"kind": "input", "name": name})` with range `_ints(op, value, min, max)`; a false condition's op goes through NEGATED first. Enum/bool inputs and other kinds are not judged here.
  - For each condition in walk order, compare it against every earlier numeric condition in walk order; the first earlier one with the same key, a different normalized text (`json.dumps(_canon(condition), sort_keys=True)`, so a pair that is one normalized condition is left to the identical-literal pass and not reported twice), and `_ints_disjoint(earlier, current)` true yields one error, and that key is never reported again (one error per key). Pairwise only: three conditions that only jointly contradict (`>= 5`, `<= 5`, `!= 5`) are not reported, matching `numeric_range`'s pairwise test; a single condition's own empty range (`count < 0`) is not judged.
  - Message: `{earlier_where} and {where} contradict: {earlier_text} and {text} are on one key whose ranges cannot overlap (a false condition negated first), so the play can never apply`.
  - Examples: `life me >= 10` + `life me <= 5` (true[0]/true[1]); `untapped_mana >= 4` + `< 2`; int input `eq 3` + `>= 5`; true `life me >= 10` + false `life me > 5`; true `clock in [1,2]` + false `clock < 4`. Not reported: life 5..10, `>= 5` + `<= 5`, different sides, different selectors, true `untapped_mana >= 2` + false `>= 4`.
  - Corpus: NEW case `errored-never-applicable-numeric-range-in-one-play` (a: true life me >= 3; b: true life me >= 10, false life me > 5; both mine/precombat_main/empty/`*`), errors `["never-applicable"]`, proven_by `[]`, dimensions base four + `numeric_range`.
- 2026-09-13 A11: script doctrine is prose pinned in tests/driver/test_prompt.py - five verbatim sentences in a `## Scripts` section of `turn_zero`, `advisor_turn_zero`, ai_seat_brief.md and ai_buddy_brief.md, plus the loop sentence that `run_script` is both the wait and the committing call. Both briefs go through `str.format`, so any brace in them must be doubled; the prompt's `inputs={{...}}` example is an f-string for the same reason. Baseline on engine 9947808: `decisions.validate_answer` lacked `Game.ask`'s scry/surveil pick-count check (B3-12), ported word for word; the `fixtures/payloads/split_card_labels.json` drift failing alongside it was regenerated here, in 293edd2.
- 2026-09-13 A8: Guides slices refreshed to mtg-decks script-dsl 67f9663 (45 plays, no inputs.json); the conflict corpus at 67f9663 matches the API copy before ab40a34; API script-dsl ab40a34 added numeric-range cases and re-pinned its sha, so the API side is newer and mtg-decks is the one to mirror it (API to mtg-decks). Runner: a `surveil`/`scry` answer naming no split moves nothing (the `-keep` plays rely on it); and while mid-resolution, another player's pass logged `allowed: true` clears `mid_resolution` instead of interrupting, because only a player holding priority gets an allowed pass (CR 117.3b) - a clobber is logged `paused: true` exactly like a legitimate resolving pass, so `paused` cannot tell them apart and still interrupts. `tests/test_playscript_end_to_end.py` stages a board for every vendored play from its deck's printings (local cache/pool only, never the network), never a second copy of a legend on the battlefield (CR 704.5j would ask first).
- **2026-09-13 - L1a look-split hint states the count.** `decisions.py`'s `_answer_argument` said "any of pending_decision.cards" for every SCRY/SURVEIL decision regardless of `amount`/`exact`, even though `validate_answer` (PR #31) already enforces the same count `Game.ask` does. Now it reads `exact`/`amount` the way `CHOOSE_CARDS` already did: `exact` says "exactly N"; `exact=False` with `amount==1` says "up to 1" (Codecracker Hound's "may put it" shape); `exact=False` with `amount` equal to the whole look says "any number, including none" (a plain scry/surveil); anything else says "up to N" (Devourer of Destiny's "may put one of four back"). Mirrored in the MCP `decide` docstring's scry/surveil bullet, the CLI `--moved` help, `schemas.DecideRequest.moved`'s docstring, and `ui/decisions.js`'s `split()` (now gates `submit.enabled` on the same count and exposes `countHint`, read by `ui/render/decision_window.js`'s status line) - `chooseCards`/`chooseMode` already did this for `choose_cards`. Fixed a stale `test_ui_decision_window.py` `SCRY` fixture that inherited `exact: True` from `BASE` (a real scry is CR 107.1c's `exact=False`) - it had gone unnoticed only because nothing read `exact` for a split before. No protocol bump: no request/response shape changed, only the `answer_with` string's content and doc prose. `fixtures/payloads/split_card_labels.json` regenerated (the Devourer of Destiny fixture's `answer_with` line). Pre-existing, unrelated: `test_openapi_drift.py`'s `ImproviseCostView` failures were already red at `origin/main` (ea368ce) before this task touched anything - `ImproviseCostView` never existed in `schemas.py` at that commit despite being in the committed `openapi.json`.
- **2026-09-13 - battle 7 B7-11 (`changed` blind to P/T, control, player counters, the stack, and a direct resolve).** `commands._card_fingerprint` tracked zone stamp, tapped, sickness, damage, counters and attachment only, so an until-end-of-turn pump (Craterhoof Behemoth's trigger, prowess, Mutagenic Growth) read `changed: false`/"CHANGED NOTHING". It now also carries `state.controller` and, on the battlefield, the whole `Game.characteristics_of` result (P/T, abilities, types, colors, computed controller) - one call, no keyword re-parsing, and no import of `serialize` (which imports `commands`). `_resolution_state` gained `player_counters` (energy, poison) and `stack`, the latter taken with `resolving=` the object about to resolve so its own mandatory removal is not a change, while a copy put on the stack or a countered trigger is. `/resolve-top` called directly never fingerprinted at all; `_run_resolve_top` now does what `_run_pass_priority` does, and `ResolveTopResponse` gained `changed`. Tests in `test_audit_log.py`: Giant Growth via pass-priority, Lightning Helix, Servant of the Conduit's energy trigger, and two `effect_for` test doubles (a base-controller change, a spell copy) for shapes the pool cannot reach without a pause. Noted, not fixed: Threaten's until-end-of-turn control does not show in the API card view (`controller` stays the old one after it resolves), and `engine_api.target_thing` cannot name an ability on the stack, so Consign to Memory has no target.
- **2026-09-13 - battle 7 B7-13 (X on the stack), protocol stays seatkit-18.** CR 202.3e: an `{X}` in a spell's cost is the chosen number while the object is on the stack, 0 everywhere else (CR 107.3g) - but `CardState.mana_value` only ever gives the off-stack reading, and `CardState` has nowhere of its own to keep an announced X once casting ends (`Game.cast_record(spell).x_value` is where it actually lives, popped at various points once the spell leaves the stack - see `CastRecord`'s own docstring). `serialize._mana_value` adds CR 202.3e's stack-only delta on top of `state.mana_value` (`x_count * cast_record.x_value`, `x_count` counting `{X}` occurrences so Walking Ballista's `{X}{X}` at X=3 reads 6, not 3, per CR 107.3i); `_card_view` sets `mana_value`/`x_value` only while `state.zone is Zone.STACK`, and only sets `x_value` when the cost actually has an `{X}`. `ui/render/board.js`'s stack row now shows `(X=n)` next to the name. Regression tests in `tests/test_stack_x_value.py` (Postmortem Lunge X=2, Walking Ballista X=3, Lightning Bolt with no X at all).
- **2026-09-13 - battle 8 B8-05 (blank-line decklist sideboard boundary).** `decks.parse_decklist` skipped every blank line outright and switched boards only on an explicit `Deck`/`Sideboard` header or an `SB:` prefix, so dimir-frog's real decklist.txt - an MTGO-style export with no header, only a blank line before its fifteen-card sideboard - merged the whole file into a 75-card mainboard with an empty sideboard, and nothing refused it: CR 100.5 sets no maximum outside Commander, so 75 >= 60's minimum passed `Deck.check_legality` clean. Fixed to match mtg-decks' own `scripts/decklist.py`: a blank line closes the mainboard once real content has been seen (a leading blank, or one between comment lines before any entry, is not a boundary), and once closed, later blank lines are spacing only - there is no third section. The explicit header and `SB:` forms are unchanged and still work standalone or after a blank line. `tests/test_decks.py` covers the boundary, leading-blank and header-after-blank shapes, plus every real decklist under `tests/fixtures/guides/decks/*/decklist.txt` (dimir-frog included) parsing to 60 main + 15 side - counts verified in-session against mtg-decks' own parser. `tests/test_decks_api.py` adds an end-to-end `POST /api/lobby/games` boot of a blank-line-sideboard deck (confirming the fix is wired through the real boot route, sideboard set aside per CR 103.2a) alongside the pre-existing `test_start_game_refuses_an_illegal_deck_naming_the_problem`, which already proved the lobby's too-few-cards refusal (CR 100.2a's minimum, `DeckIllegal` -> 400) was never silent - that half of the finding needed a regression test, not a code change. No protocol bump: this is a lobby/api-layer parsing fix with no seat-facing tool contract.
- **2026-09-13 - battle 8 B8-07 (post-game refusal claimed priority), protocol stays seatkit-18.** The server's own `commands.GameOverError` 409 (`type: game_over`, `allowed`/`accepted: False`, `winner`, `drawn` - seatkit-18's B6-06 half) was already correct, but `seatkit.run_action`'s generic `_refused` branch answered every `allowed: False` outcome alike, so a refused action, `decide`, or `pass` after the game ended still came back `held_priority: true` and `wake_reason: "refused - nothing changed, still your move"` - directly contradicting "the game is over" in the same answer. A dry run was already fine (it never goes through `run_action`, just prints the server body verbatim). Added `seatkit._game_over(outcome)` (checks `type == "game_over"`) and a branch ahead of the ordinary refusal in `run_action`: `held_priority: None`, `wake_reason` reused from the server's own `reason` rather than re-worded. `tests/test_game_over_wake_reason.py` drives a real game to a real ending (CR 104.3a concede, same route `test_game_over_refusals.py` uses) through the real `Seat`/`run_action` for an action route, `pass`, and `decide`'s own outcome-shaping, plus a locked-in regression for the dry-run path that was already correct. Purely a client-side reading fix - no request/response shape changed, so an older MCP process is unaffected either way.
- **2026-09-13 - `assign_combat_damage`, protocol stays seatkit-18.** The engine's `DecisionKind.ASSIGN_COMBAT_DAMAGE` (CR 703.4k, 510.1c, 702.19b) joins `ANSWERABLE_KINDS`: `ANSWER_FIELD_FOR_KIND`/`TOOL_FIELD_FOR_KIND` both map it to a new `damage` field (`DecideRequest.damage: list[DamageAssignmentRequest]`, `{target, amount}`); `decisions.damage_answer` resolves each `target` as a card id or player name the same way `declared_pairs_answer` resolves a defender, and `validate_answer` calls the engine's own `damage_assignment_answer` directly rather than re-deriving its checks, so the two can never drift. `PendingDecision` gained `lethal_damage`/`trample`, read straight off the engine `Decision` and added to `PendingDecisionView` (they were silently dropped by the response model until then - a state read's `pending_decision` is typed, not the free-form dict `to_view()` builds). CLI `decide --damage` and the MCP `decide(damage=...)` param take the same `[{"target":..., "amount":...}]` JSON `attackers`/`blocks` already use, via `_parse_pairs`. `baseline_seat.decide_body` recomputes the engine's own default split (lethal to each blocker in order, the rest to the defender with trample or onto the last blocker) from the public `pending_decision` fields, since `default_damage` itself is not on the wire. UI: `ui/decisions.js`'s `assignDamage` is a new decision-window mode (`ui/render/decision_window.js`'s `damageMode`) - one row per recipient with its `lethal_damage` shown and a number input, gated on the total equalling `pending.amount` exactly; CR 702.19b's stricter trample-order check is left to the server, same as every other kind's deeper legality. Test gotcha: a double-block scenario's blockers deal their *combined power* back to the attacker in the same step (CR 510.2) - Grizzly Bears (2/2) + Savannah Lions (2/1, power 2) is 4 back, exactly lethal to a 4-toughness attacker, which looks like an unrelated engine bug (the whole board wiping) until you notice both blockers' power summed to the attacker's own toughness; swapped in Elvish Mystic (1/1) so the attacker survives and the test can show marked damage.
- 2026-09-13 script-dsl main sync: main's `assign_combat_damage` (#38) became a play-script decision kind through `ANSWERABLE_KINDS`, but no script answer field can express a damage split, so it joins `FORBIDDEN_DECISION_KINDS` (`forbidden-kind`): scripts stay out of combat. The mtg-decks `scripts/check_plays.py` mirror must forbid it too (orchestrator owns that mirror). Also: script-dsl card views carry `mana_value` everywhere (selectors read it), so main's B7-13 test now expects the printed value off the stack.
- 2026-09-13 W1 (battle 4 B4-10): selector `missing_from {zones, side, by}`, so one Tron land-search play fetches whichever piece is missing (a solver showed the existing vocabulary needs 8 plays, over MAX_ACTIVE_PER_ARCHETYPE 6). Semantics (runtime, `selectors._missing` over `conditions.presence`): a candidate matches when no card on `side` in `zones` shares the compared characteristic; `by: subtype` compares the candidate's subtypes narrowed to the selector's own `subtypes_any` (all of them without it, so Tron lands all share `Urza's`), `by: name` the views' `name`; nothing to compare never matches; the candidate itself counts when in a listed zone; zones read exactly as `count` reads them, so the opponent's hidden zones are known cards only (unknown = absent) and the viewer's own library/sideboard is `include=contents` (`needs_contents` now walks every `missing_from` in a play or move, and the runner fetches contents for a move or answer that needs it). `matches(selector, card, present)` raises ValueError when a `missing_from` selector gets no `present`. What the mtg-decks stdlib mirror (`scripts/check_plays.py`) must copy: (1) every selector object may carry `missing_from`, an object with exactly `zones`, `side`, `by` (another key `unknown-field`; a missing key, a non-object, or a non-list `zones` `malformed-play`); `zones` holds 1-7 words from the count zones `battlefield, graveyard, exile, hand, stack, library, sideboard` (empty or more than 7 `bounds-exceeded`, and each item past a too-long list still judged; an unknown word `malformed-play`); `side` is `me` or `opponent` and `by` is `subtype` or `name`, anything else `malformed-play`. (2) No new code: CODES unchanged. (3) No exclusivity change: `missing_from` stays inside the condition's canonical JSON key, its `zones` sorted and de-duplicated by the generic scalar-list canonicalization, so it is never a dimension; the mirror only has to keep canonicalizing nested selector objects the same way. (4) Copy `tests/fixtures/conflict_corpus.json` byte for byte (sha256 f196d44c8c0f09d25f9b552160ef5af3a11c92220baa788ece2ad77eab8f3e66): five new `missing-from-*` cases, each with `proven_by` and `dimensions`.
- **2026-09-13 - battle 7 runtime fixes (B7-07, B7-08, B7-09, B7-10, B7-14).** `decide_body` (runtime.py) now treats a bare `surveil`/`scry` answer (`to_graveyard`/`to_bottom` absent, not merely empty) as "keep everything on top" - `moved: []` - rather than falling through to `unforeseen`; a selector present, even an empty `{}` one, still filters as before (checked by identity against `None`, not truthiness, since an empty dict is falsy). `already_ran`'s once-per-turn-and-step key (`records._SCRIPT_RUN_KEY`) gained `role`: an advisor's advise run and the seat's own run of the same script/turn/step no longer collide, but two advise runs by the same advisor still do. `_foreign` no longer classifies another seat's `pass_priority` as `interrupted` when that pass itself paused on *this run's own* pending decision (`paused: true`, `paused_on.player == ctx.me`) - the engine's own CR 608.2g pause-slot-clobber fix already keeps it harmless, so `test_the_pause_slot_clobber_race_surfaces_as_stale_or_interrupted` (nondeterministic before) is now `..._never_reports_interrupted` and deterministic. `_activate_request`/`cast_body` no longer auto-populate `mana_sources` from `untapped_mana_sources` - a play cannot name its own sources today, so the request always omits the field and the engine auto-pays, matching #32/B5-05's CLI/MCP semantics; an audit row now names only what actually paid. `_moves` sets `run.moves_done` the instant `_send_move` succeeds (before `_settle` runs), not only after the whole move settles, so a stop mid-settle (`ambiguous`, `interrupted`, `unforeseen`) still reports the move as done - this also fixes a resumed run cut off by the time cap mid-`_settle` (an `_Unfinished` raised inside `_settle` used to skip the `moves_done` update entirely via the `or` short-circuit).
- **2026-09-13 - battle 8 runtime/route fixes (B8-04, B8-06).** `when` names a board position (step/turn/stack), not a priority window: `runtime._drive` now also routes through `_wait` when `state["priority_holder"] != ctx.me` even when `_in_when` is already true, and the new `_wait_for_own_priority` polls (like `_settle`'s own idle wait, never `pass_until`'s stepping, which would treat the current occurrence as already behind it) until this seat holds priority in that same occurrence, or reports `unforeseen` if the position itself moves on first; `wait=false` still reports `not_applicable` without ever sending the move. `records._prior_script_run` no longer blocks `already_ran` on a *finished* run whose `moves_done` is 0 (refused before its first move, `not_applicable`, ...) - it touched nothing, so it never consumed the slot; a bare `start` with no finish yet still blocks (cannot tell if it moved something). `POST .../script-runs` gained `dependencies=REFUSED_AFTER_GAME_OVER` (both phases, no exception like the kill switch's finish-still-lands rule), so a run after `game_over` is refused with no ledger row.
- 2026-09-13 p3 landing review: B8-04's zero-move release skipped only the finish row, so the same run's start row (same key) still blocked every real retry; `_prior_script_run` now releases that run_id's start too, and never releases an `unknown_outcome` finish (the move may have landed uncounted) or an advise finish (always zero moves; two advise runs by one advisor must still collide). `_wait_for_own_priority` ends `unforeseen` on game over (`winner`/`drawn`) or a decision pending for this seat, instead of polling to the cap and handing back a token that polls again.
- 2026-09-13 A14: the handover is one idempotent driver `POST /handover` (reached by the natural end after the reflection and post_reflection, by `/release`, and by the driver preStop `python -m mtg_agent.driver.handover --wait 540`). The first call claims it under the lock, so joiners share one `handoverId` and one handover turn. The run table (`script_run_table` over `GET /api/games/{id}/log?uid=<dispatched uid>`; an advisor uid resolves to its human, so a buddy reads the same rows) and the conflict block (`script_tools.static_conflicts` over the corpus the driver registered, one line `code: ids (dimensions)`) are read synchronously before `/release` answers, because the lobby deletes the game right after releasing its seats. Only a 409 (the interrupted turn draining) is retried on the push; an agent that is gone or refuses fails the handover at once rather than burning the 510 s budget. The 420 s nudge goes through `/v1/probe`, the one wrapper route that writes into a busy turn (`/v1/messages` answers 409). The marker is written on done, timeout and failure, and only then does `retire` DELETE the session. The wrapper's SIGTERM path now only stops serving and holds the PTY with `handover_wait.hold_until_handover`, which waits only when `/driver-workspace` is mounted, so compose and local runs exit as before; the hold runs after `server.run()` because uvicorn captures the signal during serve and re-raises it afterwards.
- **2026-09-13 - pay_mana accepts named mana sources, protocol stays seatkit-18.** Engine PR #17 (B6-08) taught `Game.ask`'s `PAY_MANA` branch to accept a tuple of mana-source ids alongside the plain bool (CR 608.2g/CR 605.3a). `DecideRequest.mana_sources: list[str] | None` carries it (legal only with `accept: true`; `decisions.pay_mana_answer` parses and refuses naming sources while declining, matching the engine's own `Answer._check_one_value_field`); `decisions.validate_answer` gained a `PAY_MANA` tuple branch mirroring `Game.ask`'s own stray-id and `affirmative_is_legal` checks word for word (`_illegal_option_refusal` factored out so both branches share the CR 118.3 clause); `app.decide` builds `Answer(accept=True, card_ids=...)` for a tuple answer. `decisions.PendingDecision`/`PendingDecisionView` gained `mana_sources` (the engine's own `Decision.mana_sources`, the offered untapped permanents) and `answer_with` names the option when any are offered. `seat_cli decide` gained `--sources` (cast/activate's own "name none, let the engine pick" rule) and the MCP `decide` tool gained `mana_sources`. `test_decision_validator_drift.py` covers a legal tuple (a subset, the full offer, and the empty tuple), an illegal one (a stray id), and a tuple asked when `affirmative_is_legal` is already false; `test_decision_kinds.py` pays Mystical Dispute's "unless its controller pays {3}" by naming three untapped Mountains through `decide`, end to end. Contract regenerated (`openapi.json`, `contract/tools.json`, `fixtures/payloads/*`); the change is additive (an older MCP build's plain `accept` still taps every offered source automatically), so it rides the same `seatkit-18` rather than a bump.
- **2026-09-13 - S1: rows take the engine's target slots, protocol stays seatkit-18.** Engine M1e builds `TargetSlot` with `candidates`/`player_candidates` on every cast and activation row and emits its own equip row, so `_Rows.with_target_slots`, `_Rows.equip_rows`, `affordances.target_candidates`/`equip_target_candidates` and `engine_api.equip_target_requirements` are gone; `app._attach_target_candidates` writes `targets` from the row's own slots via `affordances.slot_candidates` (no api board walk), and the engine equip row is served as `kind: equip`. `engine_api.activate_ability` hands targets to `Game.begin_activating` (CR 601.2c before 601.2h), so a Hexmage may target itself. The unprinted-face refusal test went with the function: no row can name such a face.
- **2026-09-13 - toolbar tidy: Zones and Log buttons removed, Concede/Report/Lobby pinned top-right.** Owner request. The "Zones" popover (`panels.js`'s `openZonesModal`/`zoneSection`, deleted) showed graveyards, shared exile, CR 406.4's marked exile piles, and the command zone; everything but the command zone was already a subset of `state.exile`/`state.graveyards` (`exile_pile` cards are a filtered *view* of `exile` per `serialize.py`, not a second list), so those stay reachable in the always-visible zone ribbon. The command zone is new there: `ui/board_layout.js`'s `zoneRibbon` now returns six rows (an opponent/you command row bookending the existing graveyard/exile pair). The "Log" button's `#log-modal` overlay is gone too; `#log-box` (`render/log.js`'s `mountLog`, untouched) moved back into `#side-panel` as its always-visible last child with a bounded `flex: 0 1 200px` height, so the advisor panel keeps the flexible room it was given. Concede (`render/ribbon.js`'s `renderConcedeButton`, split out of `renderPriorityBox`) and Report/Lobby (unchanged handlers, just re-parented) now render into `#game-toolbar`/`#concede-slot`, a `position: fixed` corner pinned top-right at every width (`#game`'s own `padding-top` reserves the strip). Picked up mid-flight from a stopped agent: its uncommitted board_layout.js/ribbon.js/index.html/style.css changes were sound, but it had not touched `app.js` (still imported/called the deleted `openZonesModal`/`toggleLogModal`/`closeLogModal`/`logModalIsOpen`) or the tests that pinned the old shape (`test_ui_panels.py`'s log-overlay suite, `test_ui_ribbon.py`'s Concede-in-`#priority-box` tests, `test_ui_advice_layout.py`, `test_ui_hidden_elements.py`, `test_ui_menus.py`, `test_ui_store.py`) - all fixed here, plus a dead `#log-modal` entry in `decision_popover.js`'s `NOT_AN_ANCHOR` and now-unused `.zone-grid`/`.zone-empty`/`.modal-inner > .label` CSS. New `tests/test_ui_game_toolbar.py` covers the removal and the move; layout positioning itself (the `position: fixed` CSS) falls under the formatting/layout test exception.
- **2026-09-13 - the play UI can equip.** Equip (PR #30/#41's `kind: equip` row) had no offer anywhere in `ui/`: equip has no entry in `card.abilities` (`Game.activated_abilities` never synthesises it), so `planPermanentClick`'s existing "no abilities -> none, one ability -> auto-activate" branches silently swallowed every Equipment. `planPermanentClick` now routes an Equipment permanent (`/\bequipment\b/i` on `type_line`) to the menu branch with a new `equipment: true` flag, same shape as `vehicle`. But unlike Crew (always offered, then dry-run-refused), Equip's CR 702.6a "activate only as a sorcery" timing makes the engine drop the row entirely outside a main phase rather than mark it unaffordable, so `menus.js`'s existing enrich-after-open pattern (offer unconditionally, greyed out later) cannot be reused - it would leave a live "Equip" button up during combat that always refuses. `app.js`'s new `openPermanentMenu` asks `GET .../legal-actions?candidates=true` BEFORE opening anything, and `menus.js`'s `abilityMenuItems` only appends the "Equip" item when a matching row came back; picking it opens a second small popover built from the row's own `targets.slots[0].candidates` (`menus.js`'s new `parseCandidateLine`/`equipTargetMenuItems` - equip has no `/affordances/{card_id}` entry point of its own, so the legal-actions row's candidate lines are the only source), and picking a target posts `POST .../actions/equip` with `expected_seq` from that same read (`app.js`'s new `beginEquip`/`fireEquip`), following the ribbon's keep-priority-by-default same as `fire()`. No protocol or schema change - the route, request shape and target-candidate format already existed (PR #30/#41); this is UI-only. `tests/test_ui_menus.py` covers the new menu-item/parsing functions in isolation; `tests/test_ui_app.py` drives the real click through the fake-DOM popover harness for the offer-gating (present in a main phase, absent outside one), the target popover's candidates, the posted body/`expected_seq`, and refusal rendering.

- **2026-09-13 - U1 (control until end of turn in the view).** `Game.model_copy(deep=True)` copies each `CardState._board_source` as a detached duplicate of the original game and skips `Game.model_post_init`'s relink, so every command's working copy (`commands._replay_copy`, `engine_api.dry_run_copy`) answered `controller` off a board without the Threaten effect, and that copy is what got committed. `engine_api.copy_game` re-runs the relink; `put_card_in_zone` binds the staged card. `get_legal_actions` builds rows on `commands.board_at_pause` while a declare-attackers or declare-blockers question stands: the committed board is a step behind and the engine enumerates no attacker pairs outside that step (the gap `baseline_seat.attack_candidates` worked around). Engine half: Act of Treason's word order lost the untap and the haste.
- **2026-09-13 - U2 (an ability on the stack as a target).** `engine_api.target_thing` falls back to `Game.find_ability_by_id`. The activate route resolves `targets` before `begin_activating` and passes them in (CR 602.2b announces before paying), so a named ability on the stack is checked by the engine before any cost. Engine half: `TargetRequirement.ability_kinds` (triggered vs activated) and `Game._legal_target_candidates` offering abilities on the stack; these tests need an engine sibling carrying both. Protocol stays `seatkit-18`.
- **2026-09-13 - U1/U2 after merging #41/#42.** Binding staged cards made `serialize._card_view`'s `printed_power`/`printed_toughness` (read off `state.power`/`toughness`) the computed values, so an animated land lost its printed fields; they now read the engine's `CardState._printed_power`/`_printed_toughness` (printing plus counters, no layers). #41's `affordances.slot_candidates` resolved the engine's `TargetSlot.candidates` through `find_by_id` alone, dropping an ability on the stack the engine had listed; it falls back to `find_ability_by_id`.
- **2026-09-13 - U1 follow-ups.** `serialize._card_view` reads the engine's public `CardState.printed_power`/`printed_toughness` (engine 62e9d12), not the private names. `action_attack`'s dry run, while a declare-attackers question stands, goes through `_dry_run_declaration`: the same `declared_pairs_answer`/`validate_answer` and `commands.resume_command` replay the real answer gets, never committed, so a creature named twice is refused by both.
- **2026-09-13 - battle 8 B8-01/B8-08 companion, cascade casts during resolution.** Engine c0626c6: accepting cascade's "you may cast it" (or suspend's own third ability, engine funnel commit 4ce9ae1) now casts the hit immediately through `Game.cast_during_resolution`/`may_cast_during_resolution` while the permitting trigger is still resolving (CR 608.2g) - face, mode, target, and (post-4ce9ae1) kicker/multikicker/entwine/escalate/offspring/gift/blight/mandatory-cost decisions are asked as ordinary mid-resolution `pending_decision`s chained off the same `POST .../decide` that accepted the `may`, no separate `actions/cast` call, no lingering `PlayPermission` (a chained `decide` response already carries `paused`/`pending_decision` when the resumed command pauses again - no need to re-GET state to see the next question). `tests/test_play_permissions.py` rewritten: Violent Outburst into Living End (stack order is top-first in `_stack_view`, so Living End resolves *before* the cascading spell), Bloodbraid Elf into Kolaghan's Command (two `choose_a_mode` decisions then two `choose_targets`, plus bob's own `choose_cards` for which card to discard even with only one), Bloodbraid Elf into Fatal Push with no legal target anywhere (the engine now refuses `accept: true` outright as illegal per CR 608.2d rather than accepting and silently bottoming - `pending_decision.affirmative_is_legal` is `False` up front). `tests/test_special_actions.py`'s suspend test and two of `tests/test_play_permissions.py`'s three original suspend tests rewritten the same way; the third (`...is_only_good_for_its_own_upkeep...`) is deleted outright - its premise (accept, then let the step move on before driving the cast) cannot happen any more, since the accept and the cast are the same synchronous call. `tests/test_battle3_ledger_and_reads.py`'s `test_a_permission_cast_logs_the_exile_it_was_cast_from` swapped Living End/suspend for Slickshot Show-Off/plot - `engine_api.cast_origin_zone` reads `locate_castable(...).zone` regardless of which mechanism permitted the cast, so any from-exile cast exercises the same B3-15 regression guard. `conftest.resolve_whole_stack` now asserts `paused is not True` instead of looping forever appending `None` when a drained resolution itself pauses on a decision (this is what caused a `-x`-less pytest run to appear to hang indefinitely rather than fail fast) - card fixtures needed (Violent Outburst, Kolaghan's Command, Fatal Push, Bloodbraid Elf, Aether Spellbomb, Memory Lapse) all resolve through `scryfall.py`'s tier-2 bundled corpus already, no new `tests/fixtures/scryfall/*.json` needed.
- **2026-09-13 - `bottom_if_unused` removed, `PROTOCOL_VERSION` bumped to `seatkit-19`.** Dead since the funnel commit: nothing sets it `True` any more (only `PlayPermission`'s own `False` default remains, `machines/casting.py:1128`) now that cascade's cleanup-bottoms-an-unused-permission sweep is gone. Dropped from `serialize._play_permission_view` and `schemas.PlayPermissionView` rather than kept as a key that can only ever read one value - a genuine field removal, not additive, so `protocol.py`/`seatkit.py` both bump and `seatkit.py` carries the changelog paragraph `tests/test_protocol_boundary.py` requires.
- **2026-09-13 - `known_cards`: B8-03's public-zone-to-library knowledge, surfaced for the first time.** `Game.known_to` existed before this session (battle 5's public-zone-to-hand tracking, extended by B8-03 to a card put into a library from a public zone) but had no API reader at all - checked via `git log --all`: the one prior attempt (`known_cards` on the unmerged `script-dsl` branch, seatkit-17, never reached main) predates the current seatkit-18 baseline and was not merged. `serialize._known_cards_view` builds `{knower: {hand, library, sideboard}}` for every player (own hand/sideboard dropped since a seat already sees those whole; own library kept, since `libraries` never reaches any seat at all); `visibility._redact_known_cards` keeps only the viewer's own entry, admin sees all. Added to `schemas.GameStateView` (`KnownCardsView`/`KnownLibraryCardView`, additive - rides `seatkit-19` alongside the `bottom_if_unused` removal above rather than needing its own bump) and to `mcp.py`'s `SECTION_KEYS`/`_BY_USERNAME_KEYS` (free: `side` filtering already works, since after redaction a non-admin viewer's own username is the only key ever present). `tests/test_known_cards.py`: Lightning Bolt countered by Memory Lapse onto its owner's library top, read back as both alice and bob (both know the identical public fact), and the record's own disappearance once drawn. Explicitly out of scope, not built: `script-dsl`'s `library_contents`/`sideboard_contents` unordered-multiset extension and its `?include=contents` query param - unrelated to B8-03, no ticket asked for it.
- **2026-09-13 - Kolaghan's Command's damage mode actually reads.** Engine 4ce9ae1: the damage-to-target reader choked on the card's own possessive apostrophe in its self-referential mode text ("Kolaghan's Command deals 2 damage to any target"), so the mode was legal to choose (CR 700.2a never refused it) but resolved doing nothing - a defect invisible as a refusal. `tests/test_kolaghans_command_damage_mode.py` proves it end to end through the plain modal cast route (no cascade): the mode renders in `affordances`, is choosable alongside a second mode, and actually deals its 2.
- **2026-09-13 - z1: two more cast-during-resolution scenarios closed, dead helper dropped.** Picking up the uncommitted z1-api-cascade-companion work above: the checklist called for a kicker spell off a cascade and one of rebound/hideaway through the real routes, neither yet covered. `tests/test_play_permissions.py::test_a_kicker_spell_off_a_cascade_pays_the_kicker_through_decide` (Bloodbraid Elf into a kicked Burst Lightning) found that paying an optional additional cost's mana is its own `pay_mana` decision chained after the `may`/target decisions, not auto-tapped the way the engine's own `Scenario` harness makes it look (that harness's `will_take_the_may` answers `PAY_MANA` questions itself under the hood) - a real seat must answer it explicitly. New `tests/test_rebound_recast.py` proves Ephemerate's rebound end to end: cast normally, resolves into exile instead of the graveyard, `force_turn_position(UNTAP) -> advance` to alice's own next upkeep (the same idiom suspend's tests use - a raw `PUT .../step` fires no beginning-of-step triggers) reaches the delayed trigger's own `may`, accepting it re-casts immediately and asks a fresh target, and the second cast (from exile, not hand) lands in the graveyard for good rather than rebounding forever. `conftest.drain_suspend_upkeeps` deleted - unused since both callers switched to their own inline upkeep-draining after suspend's third ability started pausing on a decision (the helper's blind `resolve_whole_stack` over all `count` upkeeps would hit that pause and raise on the last one, so nothing could safely call it as split).
- 2026-09-13 owner decision: mtg-decks is private with no GitHub Actions budget, so its `ci`/`automerge` workflows never run. `guides_propose._publish` now returns `awaiting_owner_review` (added to `gitwork.SUCCESS`) instead of `pr_opened`, and the PR body, module docstring, seat_cli help, both AI briefs, the handover prompt (`prompt.handover_turn`, prose pinned in `tests/driver/test_prompt.py`), the D2 spec and the README all say the owner merges guide PRs by hand and the agent must not wait for it. The code never polled for checks/automerge/merge state to begin with (only the wording claimed automerge); scripts are unchanged, `plays admit` still pushes straight to mtg-decks main.
- 2026-09-13 all-in-main merge (U1, Z1, BD1, UI2, script-dsl): `known_cards` had two implementations, Z1's (B8-03 only) and script-dsl's (`source`, paused-command knowledge, `library_contents`/`sideboard_contents`); script-dsl's is the one kept, Z1's `serialize._known_cards_view`/`visibility._redact_known_cards` copies and its `known_cards` entry in `mcp._BY_USERNAME_KEYS` are gone, and Z1's `tests/test_known_cards.py` (Memory Lapse onto the library top) runs against it. Z1's line above calling script-dsl's version unmerged is superseded. Protocol: one `seatkit-19` paragraph covers the DSL, Z1's decide-chained free casts and `bottom_if_unused` removal, and BD1's rationale cap; `seatkit-18` is main's paragraph unchanged.
- 2026-09-13 HO1 (live, game 995daaf7): a release mid-turn never handed over. Root cause: Claude Code runs no Stop hook for an interrupted turn (hooks-guide: "They don't fire on user interrupts"), and the Stop hook's `/internal/turn-complete` is the only path to `Manager.complete`, the one call that frees `_current`. So the driver's ESC (`/v1/interrupt`) cancelled the blocked `wait_for_priority` but the wrapper stayed busy for good, and the handover push got 409 5523 times (flat 50 ms retry) until the 510 s budget, with no handover turn. Fix: `Manager.interrupt` arms `escalate_interrupt` after `INTERRUPT_SETTLE_SECONDS` (20 s): if the turn is still in flight it writes a second ESC, logs one `interrupt escalation` line, and fails the turn `interrupted: ...` so the next submit is accepted with the PTY kept. The driver backs off on 409 (0.5 s doubling to 5 s) and fails the handover with the reason in the marker after `HANDOVER_PUSH_BUDGET_SECONDS` (120 s). Wait tools: `Seat.fetch_state` returned a 404 body as a board, so a wait on a deleted game read "nothing new yet" for its whole bound; `block_until_wake`/`pass_until` now answer `GAME_GONE` on the first 404/410, and take `should_stop`, which `mcp.cancel_probe` fills from the request's `cancel_requested` (a sync tool runs on a worker thread the SDK's cancel cannot stop; `run_script` reads it as its time cap). A late Stop hook cannot end the wrong turn: the Stop hook payload names no turn, so `Manager.submit` appends `<!-- wrapper turn: <turnId> -->` to every pasted prompt (the stored record text stays unstamped), the hook reports `turnId` = the last marker in a prompt claude took in (`transcript.last_turn_marker`: `user` string or text-block content, or an `attachment` of type `queued_command`; never tool results, assistant text or an unconsumed `enqueue`), and `Manager.complete` ignores, with one `stale turn-complete ignored` line, a claim naming another turn, and an unnamed claim while an escalation has not yet been followed by an accepted completion. An unnamed claim with no escalation pending is trusted as before.
- 2026-09-13 HO2 (live, game 16a9cf8f): the boros seat's own handover (two subagents, plays admit conflicts, guides propose) hit `timeout` at 508 s of the 510 s budget, lowered two fetch plays by 20 that never had a window (exits none/not_applicable; reverted in mtg-decks), and ran `git stash` in /guides. Budget now: Job `terminationGracePeriodSeconds` 1800 (seat and buddy share `deploy/seat-job.yaml`), driver preStop `--wait 1740` (`handover.PRESTOP_WAIT_SECONDS`), `HANDOVER_BUDGET_SECONDS` 1680 with the nudge at 1380, agent preStop and wrapper SIGTERM PTY hold 1760, compose `stop_grace_period` 30m; the 409 push bound stays 120 s. `activeDeadlineSeconds` 14400 and reconcile's Background Job delete never cut the grace short, so both are unchanged. The handover turn, both briefs and the play-script spec now carry the rules (only scripts that ran with recorded exits, never lower for not_applicable/no window, at most 5 per game, priority and admits first then subagent development, never git stash in /guides), and `mcp-seat plays priority` refuses `priority_limit` past 5 per call and per game (`--game-id` or MTG_GAME_ID, starts kept in `.git/mtg-plays-priority-games.json`, which admit leaves alone).
- **2026-09-13 - T1 scalable board layout.** The live board's seven card rows (`.fit-row`: opp hand, opp lands, opp other permanents, stack, my other permanents, my lands, my hand) are fixed flex shares of `#mtg-board` (0.6/1/1.2/0.8/1.2/1/1.2, basis 0) and never resize with content; the cards resize instead. `ui/fit.js`'s `fitCardWidth` is the pure sizing rule and `render/board.js`'s `fitBoardRow` measures a row and writes its `--card-w` after every render and from a `ResizeObserver` on the board and each row. Non-obvious choices: (1) a tapped `card-fit` card changes its own box to 88:63 and rotates only the art inside it, because a bare `rotate` keeps a portrait layout box and overlaps both neighbours; (2) `patchStacks` stamps `data-tapped` on a pile when any card in it is tapped or preview-tapped, and the pile reserves a landscape width from that, so a tapped Equipment tucked under an untapped creature cannot overhang the next pile - `fit.js` counts the whole pile as tapped, which over-reserves slightly but is never short; (3) tuck offsets are the old 9px/20px at `card-md`'s 108px expressed as ratios (`TUCK_X`/`TUCK_Y`, mirrored in `.perm-stack`'s `--tuck-x`/`--tuck-y` with a 76px fallback so review frames' `card-sm` piles draw as before) and a test pins the CSS to the JS; (4) the `.lands-row` "never shrink" rule (tucked cards sliced) now only governs review frames - on the live board the fit caps card height so the pile fits the row; (5) `card-sm`/`card-md`/`card-lg` stay for the zone ribbon, the decision window and review frames, which are not board rows (`#permitted-row` was on this list until T4 fitted it too); (6) the red zone row is gone - `.attacking`/`.blocking` on the permanents carry combat; (7) stack objects are `.stack-object` wrappers (kept so `decision_popover.js` still anchors on `.stack-object[data-key]`) holding a `card-fit` card plus a fixed 16px `.stack-caption` that the fit subtracts from the row height. The page never scrolls at any size: `#game` is one viewport tall, and at 1100px wide or less the board takes the whole width and the controls move into T3's drawer (see the next line).
- **2026-09-13 - T1 no-page-scroll below 1100px.** The old `max-width: 1100px` block stacked the grid with `height: auto`, so every phone in landscape scrolled the page, and seven 44px-floor rows (about 499px) cannot fit a 390px-tall window anyway. Now `#game` is one viewport tall (`100dvh`) at every size: on a desktop a grid of the full-height board, the zone ribbon and a 340px side column (turn block, banners, side panel); in mobile mode (T3, at 1100px wide or less or a coarse pointer in landscape) the board alone, with the zone ribbon hidden and the controls in a drawer; and a `max-height: 560px` block that tightens spacing. The card floor is no longer fixed: `render/board.js`'s `fitBoardFloor` measures the height the board has left for cards and writes a shared `--card-floor` (`ui/fit.js`'s `fitFloor`, 44px down to `MIN_FLOOR_WIDTH` 20px) that the rows' `min-height` and every `fitCardWidth` call use. A regex over `style.css` cannot prove layout, so `tests/test_ui_viewport_layout.py` mounts the real board in headless Chrome, in exact-size iframes (a headless window's viewport is not its `--window-size`), and checks no page scroll, no board scroll, rows in order and every card inside its row at twelve sizes from 667x375 (and 390x844 portrait) to 1920x1080; the harness page POSTs its measurements to the test server's `/__result` and the test kills Chrome once they arrive, because Chrome may not exit on its own.
- 2026-09-13 T2 card preview: `ui/preview.js` replaces the fixed `#card-preview` panel with one `#card-zoom` element (z 65: above `.modal` so modal cards preview, below `.toast`). Mouse hover (pointerover with `pointerType: "mouse"`, so a touch tap never opens it) centres a >=260px/2.2x copy over the card; it also hides on a mouse press and stays hidden for that card until the pointer leaves it, because the click opens the anchored confirm `#popover` right there and the preview would cover it. A touch/pen press held 350ms (10px slop) opens a centred overlay; the release swallows its click at `window` capture, so no document-level outside-click listener (the card menu's) sees it either. The long-press hold is `store.holdStart`/`holdEnd` in `createStore`, separate from the press guard's `pressing` flag so `PRESS_CAP_MS` still flushes ordinary presses; `holdEnd` runs on a `setTimeout(0)` after lift. `hoverableCards` moved from `render/panels.js` to `preview.js`.
- 2026-09-14 HO3 (live, game 8a55cab7): (1) both seats proposed guides on `seat/<game>-guides`, so mtg-decks PR #123 mixed boros and frog commits; `guides propose` now uses `guides_branch` = `seat/<game>-<deck>-<role>-guides` (role is MTG_ROLE, `seat` or `advisor`, via `gitwork.role`), so each seat and buddy gets its own branch and PR and a repeat propose reuses its own. (2) A handover turn ended with a script subagent still running: Claude Code answers a background launch at once (Agent `Async agent launched`, Bash `backgroundTaskId`), runs the Stop hook when the main turn stops, and later delivers a `<task-notification>` (tool-use-id, task-id, status) as a new prompt that it answers and ends with another Stop hook. Chosen approach b plus the prompt rule: `transcript.pending_background_tasks` lists launches since the last turn marker with no terminal notification (a `user` prompt or `queued_command` attachment; `running` is not terminal), no `TaskStop`, and no `TaskOutput` that retrieved the end (those often get no notification; seen in local transcripts), sidechain lines skipped; the Stop hook sends it as `pendingTasks`, and `Manager.complete` keeps the turn in flight (`turn stop deferred ...`) until a Stop hook lists none (`background tasks finished ...` then `turn complete`). The handover prompt and both briefs say to wait for every subagent and background task before ending the turn; the nudge says to stop them. Limit: the scan reads Claude Code's transcript wording and fields, which are not a documented contract, so a format change could hide a launch (old behaviour) or a finish; a task that never finishes is bounded only by the driver's 1680 s handover budget (marker `timeout`).
- 2026-09-14 S1: live AI-vs-AI seats never called `run_script`/`list_scripts` on their own and cracked every fetch land by hand (`play_land` then `activate`), even though both tools were registered (mcp-config `alwaysLoad`, `mcp.py` ~2405-2511, `contract/tools.json`). Root causes, all now closed: (1) no wake/action answer said a script applied - finding out cost a separate `list_scripts` call no seat ever made, and a fetch's own plays apply right after the fetch lands on the battlefield, exactly the moment a seat habitually reaches for `activate` by hand. Fixed by `scripts: {"applicable": [...]}` on every answer through `mcp._tool_result` (`playscripts.script_tools.applicable_hint`, `seatkit-20`): same judgement `list_scripts` makes, built from the state the answer already carries (never a second board fetch - a play needing `include=contents` the hint cannot fetch is treated as not applying, `_hint_judged`, rather than raising `conditions.ContentsRequired`), capped at `MAX_HINT_SCRIPTS` (5), `conflict: true` with `call` withheld when more than one applies at once. (2) `driver/prompt.py`'s loop said "two tool calls, and nothing in between" while omitting `run_script` from the committing-call enumeration and requiring `list_scripts` first in the Scripts section; both prompts and both CLI briefs now say the hint's `call` IS the committing call, name the fetch-land case concretely, and state plainly that scripts never run on their own. (3) staged CLI runs during development had made a seat believe scripts "auto-fire" (written into its own memory) - `schemas.ScriptRunIn.invoked_via` (`"mcp"`/`"cli"`, required, 422 otherwise) now records who actually started each run: `mcp.run_script` sends `"mcp"`, `mcp-seat script run` sends `"cli"`; `playscripts.report.script_run_table`'s handover row and `scripts/eval_report.py`'s per-seat `scripts.by_invoker` both surface it, a `"cli"` count flagged "not started by you" since a seat's own runs are always `"mcp"`. No DB migration needed anywhere here: a `script_run` row lives in the `records` table's schemaless `payload` jsonb column (`repository_postgres.append_record`), so a new `detail` key needs no column.
- 2026-09-14 T3 mobile shell: the `#opp-info`/`#you-info` rows are gone; each seat's player badge (`render/board.js`'s `patchBadge`, built once and patched in place, pool rebuilt only when `mana_pool` changes) is the first column of its land row, and the lands go in a sibling `.fit-cards` (`#opp-lands-cards`/`#my-lands-cards`) that `fitBoardRow` measures, so the fit's width budget already excludes the badge; `fitBoardFloor` still keys off the outer `.fit-row`. The badge is a wrapping flex row that packs from its land row's own height (`@container badge-row (max-height: 80px)` in `ui/style.css`, a wider badge in two shorter lines) so its zone counts show without scrolling at 740x360 and on a short desktop window. Zone counts open `render/zone_viewer.js` (keyed by `zoneRibbon` row keys, cards decorated by board.js's shared `zoneCardDecorator`, Escape caught at document capture with `stopImmediatePropagation` so app.js's cancel does not also fire). Mobile mode is one media block, `(max-width: 1100px), (pointer: coarse) and (orientation: landscape)`: single board column, `#zone-ribbon` hidden, and `#drawer` (a `display: contents` wrapper on desktop around `#topbar`/`#banners`/`#side-panel`) becomes a fixed panel left of a 64px right rail. The rail, not a hidden toolbar, is how the toolbar stays reachable: `tests/test_ui_game_toolbar.py` forbids a second `.game-toolbar {` rule, so the rail reuses `--toolbar-max` to wrap the three buttons into a column above `#drawer-toggle`. `ui/drawer.js`'s `promptKey` (window subject, own opening-hand turn, own pending decision, an action in flight, own priority keyed by turn/step/stack, nothing during an autopass run) opens the drawer only when the key changes, so closing it on a question sticks until the next one. The viewport harness serves `?drawer=open` and must strip the query string before its page lookup, and it disables the drawer transition in that frame or it measures mid-slide.
- 2026-09-14 T4 permitted row and preview scroll: `#permitted-row` is fitted like a board row but is not a `.fit-row` (tests pin exactly seven of those), so it has its own `#mtg-board > #permitted-row` rule: `flex: 1 1 0` (a lands row's share), `hidden` and so `display: none` with no box or gap while it has no permission, and `card-fit` cards whose `--card-w` `render/board.js` fits from that share through `fitBoardRow` (it is pushed onto `fitted`, so `fitBoardFloor` counts it as an eighth row and lowers the shared floor on a phone). Reusing `#my-hand`'s `--card-w` instead was rejected: a content-sized row sized from the hand's width takes height from the hand, which refits smaller, a loop with no fixed share to settle on, and at 844x390 the old 108px `card-md` row scrolled the board. The hover preview no longer closes on every scroll: `ui/preview.js`'s `onScroll` closes it only when the scroll target contains the hovered card or has no `contains` (the window); `document` contains everything, so a page scroll still closes it, while `render/log.js` pinning the log to its newest line leaves it open.
- 2026-09-14 S2A1 plan ahead (live game 2026-09-14: an agent's upkeep pass was refused by `require_plan`, then spent ~110s writing a plan while the human waited on it, held in its own upkeep). Two independent fixes rather than one: (1) `DeclarePlanIn.for_turn: "current" | "next"` - `"next"`, declared on the opponent's turn, writes the plan row against this seat's own next turn, computed by `records.next_own_turn` deep-copying the game's `TurnMachine` and calling its own `begin_next_turn` (never re-deriving turn order) until the seat comes up active, bounded by seat count plus queued extra turns so CR 500.7 extra turns are skipped exactly as a real game skips them; `for_turn: "next"` during the seat's own turn is `OwnTurnInProgress`, 409. `require_plan`'s `turns_taken`-equality matching needed no change at all - a plan-ahead row satisfies it the moment that turn actually arrives, same as a same-turn declaration always has. (2) Separately, `pass_priority` is now exempt from `require_plan` outright when the step is upkeep or draw (`pass_shaped=True`, mirroring `require_reasoning`'s own exemption) - a bare pass in the two steps almost nothing happens in needs no plan on record at all, planned-ahead or not. Both ship together because the live failure was really two problems: no way to plan ahead, and no reason a beginning-phase pass should ever have been gated to begin with. `seat_cli.py` gained a `plan` command (`--board-read/--my-role/--plan/--expecting/--because/--confidence/--for-turn`) fronting the existing `declare_plan` MCP tool - `contract/tools.json` and `openapi.json` regenerated (`mise run tools:export`, `mise run openapi:dump`). Review round, same day: the beginning-phase pass exemption (2) was too wide - it now also requires `not game.stack`, since a trigger or spell sitting on the stack in upkeep or draw is a real decision, not "nothing to do". And a plan-ahead row's own top-level `turn` (stamped by `audit.record` from the live board at write time) named the turn the declaration was made IN, not its target - divergent even in 2-player games whenever the currently-active seat is not seat 0 (`TurnMachine.turn_number` only increments on the wrap to seat 0), and outright wrong-player in 3+ player games. Fixed by recording the target `turn_number` as its own `detail` key at `app.declare_plan` (never touching `audit.record`'s own honest write-time stamp, which `ui/review_model.js`'s `reviewTurns` still uses for its chronological group-boundary detection) - `review.py`'s `_declarations` prefers it over `entry["turn"]`, and `reviewTurns` attaches a `plan_declared` row to the group matching its *target* turn in a second pass, rather than to whichever group happens to be open when the row was chronologically written.
- 2026-09-14 S2 A7 stall/latency metrics: `audit.record` stamps every row with `active_player` (`turn.players[turn.active_index]`) and `priority_holder` (`game.priority.holder`), both `None` on a row recorded before this and on any game the store cannot decode - old rows keep reading fine everywhere (`_UNRECORDED_DEFAULTS` covers both). `scripts/eval_report.py` per seat: `own_turn_first_action_ms` groups a game's rows into turn instances (`_turn_instances`) by a row's own write-time `(turn, active_player)` pair - never `detail.turns_taken`, which on a plan-ahead row (`for_turn: "next"`) names the *target* turn instead of the one it was written in, so grouping on it would have let a plan-ahead row read as the seat's own first action of a turn that has not started; `detail.declared_turns_taken` (always physical) only bridges a plan row into its own turn's other rows for the rare CR 500.7 extra-turn case where the pair repeats. Pregame (`turn_number` 0) is excluded outright - not one of a seat's own turns. `advice.latency_ms` (`drafted`/`undrafted`, keyed by `detail.drafted_at_seq is not None`) times from the row whose own `priority_holder` first reads as the advised seat, tracked in one forward pass - the sound signal over guessing a window from an action's name and outcome. A ledger from before this stamp lands `n=0` on both metrics rather than a guess, on the same "unknown, not invented" footing `stale`/`offered` already established here.
- 2026-09-14 T5 rail Pass: plain priority no longer opens the mobile drawer. `ui/drawer.js`'s `promptKey` keys only questions that need a choice (decision window subject, own opening-hand step, own pending decision incl. combat declarations, a cast/activation in flight); priority held by this seat returns `null`, so holding priority never slides the drawer over the board. Pass and Pass until live on the right rail instead: `#rail-pass` (outside `#drawer`, `display: none` on a desktop, fixed above `#drawer-toggle` in the mobile block) is rendered by `render/ribbon.js`'s `renderRailPass` from `mountRibbon`, with the same `verbButton`/`onVerb` as `#priority-box`, hidden whenever the box offers no verb, while an autopass run answers, and while any decision is pending (the box keeps Pass beside an own decision; the rail does not, because the drawer opens for that decision). The priority box's Pass until is a plain `onVerb("pass_until")`, not a menu, so the rail's is too. The rail width is unchanged (64px), so `#game`'s right padding and `fit.js` needed nothing. `tests/test_ui_viewport_layout.py` clips each card to the board box before its rail overlap check: at 390x844 a sideways-scrolling row's card extends past the board, which `overflow-x: hidden` clips.
- 2026-09-14 T9 login kept alive: Loki showed oauth2-proxy v7.15.4 `expiry:168h0m0s refresh:disabled` forwarding the 30-minute Keycloak ID token (PASS_AUTHORIZATION_HEADER) until `oidc.py` refused it with 401 `invalid bearer token: Signature has expired`, so players hit login about every 30 minutes and a page load showed raw JSON. `deploy/k8s/55-oauth2-proxy.yaml` sets `OAUTH2_PROXY_COOKIE_REFRESH=20m`: verified in v7.15.4 source that keycloak-oidc inherits `OIDCProvider.redeemRefreshToken`, which replaces the session IDToken (so the forwarded bearer gets a fresh exp), that the refresh runs in the session loader before proxying once session age exceeds the period, that validation needs refresh < cookie_expire (168h default) and refuses it with SESSION_COOKIE_MINIMAL=true (already false, so the cookie size is unchanged; it splits past 4000 bytes). Keycloak SSO Session Idle still bounds an idle user. On the api, `oidc.BearerTokenRejected` (an HTTPException 401 subclass) is what `current_user` raises; `app.py`'s handler for it returns 302 `/oauth2/start?rd=<path?query>` for a GET outside `/api/` whose Accept has text/html (page routes 401 via `GameAccessRoute`, which gates `/{game_id}/` for owned games), and FastAPI's JSON 401 otherwise. `rd` falls back to `/` unless the path starts with one `/` (no `//`, `/\`, or control chars).
- 2026-09-14 T6 log panel removed: `#log-box`, `ui/render/log.js` (`mountLog`), its `#log-box`/`.log-line` CSS and `tests/test_ui_log.py` are gone from the play UI; `#advice-box` is now `#side-panel`'s last child and its only flexible one (`flex: 1 1 auto; min-height: 0; overflow-y: auto`), filling the rest of the column on a desktop and of `#drawer > #side-panel` in mobile mode, `hidden` with no proposal. The log POLLING and `app.log` stay: advisor proposals are `action: "advice"` ledger rows on the same `GET .../log?since=` read, and `advice_model.js` (`currentAdvice`, `isFresh`, `adviceStatus`) plus `render/advice.js`'s render key read `app.log`, so deleting the poll along with the panel would silently blank the buddy panel. `tests/test_ui_advice.py`'s poll test drives the real `app.js` `poll()` into a DOM built from `index.html`'s side-panel ids; `tests/test_ui_viewport_layout.py` pins the fill (advice bottom at the side panel's content bottom) at 1920x1080, 1280x720 and the open 844x390 drawer, and re-measures every frame with the panel hidden.
- 2026-09-14 T7 phase track in the top row: `#phase-track` moved out of `#topbar` into `#game-toolbar`, which is now one `flex-wrap: nowrap` row across the top (desktop: `left/right: 8px`, 36px tall, in `#game`'s 52px `padding-top`; mobile block: a 28px `--strip-h` strip at `top/left/right: 0`, compact buttons, `#game` padding-top `strip + 4px`, `#drawer` starts at `top: var(--strip-h)`, `--rail-w` narrowed 64px to 56px, `--toolbar-max` and the stacked-rail button rules gone). The track is `order: -1` (so a button inserted as the row's first child, like T8's fullscreen button, still lands right of it), `flex: 1 1 auto; min-width: 0; overflow-x: auto; scrollbar-width: none`; with the scrollbar hidden, `render/panels.js`'s `renderPhaseTrack` centres the `.current` step when the track overflows (getBoundingClientRect, a no-op in the Node fake DOMs). Removed the dead `cursor: pointer` and `li.target` track styles. `fit.js`/`board.js` needed nothing: they measure the rows' own boxes. The viewport harness now mounts the real `mountTopbar` with the fixture at `end_step`, so 667x375 overflows and the scroll-into-view is measured.
- 2026-09-14 T8 mobile fullscreen: `ui/fullscreen.js` puts `#fullscreen-toggle` first in `#game-toolbar`, hidden unless `document.fullscreenEnabled`; a click calls `requestFullscreen({navigationUI: "hide"})` then `screen.orientation.lock("landscape")` (only works while fullscreen, Chrome-based Android only), every rejection or throw swallowed. The choice is `mcm.fullscreen` in localStorage; a reload always leaves fullscreen and a request needs a user gesture, so with the choice on the next click anywhere re-requests once from a passive document capture listener that never stops or prevents it (a long press's swallowed click never reaches it). Any `fullscreenchange` to not-fullscreen forgets the choice (button, back gesture, Escape) except after `pagehide`, because an unload's exit is not the player leaving. Platform limits (2026): Safari on iPhone has no element fullscreen as of iOS 26.6, so the button is hidden there and Add to Home Screen is the only way to get the room back - it opens standalone with the status bar showing (display `fullscreen` and `orientation` are ignored; `black-translucent` draws under it, and since iOS 26 every Home Screen site opens as a web app, so apple-mobile-web-app-capable only matters before 26); iPadOS 16.4+ has unprefixed element fullscreen; Android honours the manifest's display `fullscreen` and orientation `landscape` when installed. `ui/manifest.webmanifest` uses a relative `start_url` `./` with scope `/`, so installing from `/<game id>/` reopens that game and from `/` the lobby; `app.py` pins the `.webmanifest` media type because a host mime.types is read over Python's table. Safe areas: `viewport-fit=cover`, `--safe-*` tokens read from `env()` once at the end of `style.css`, body padded by them, `#game` height is `100dvh` less top and bottom insets, and the fixed toolbar, `#rail-pass`, `#drawer-toggle` and `#drawer` move in with `translate` so their own `top`/`right`/`bottom` stay untouched; the viewport harness mounts the real button and measures an 844x390 frame with iPhone-like insets set through those tokens. Icons come from `mise run icons:generate` (stdlib PNG writer).
- 2026-09-14 T12 mobile-room review fixes: the manifest link carries `crossorigin="use-credentials"`, because browsers fetch a manifest without cookies otherwise and play-magic's oauth2-proxy (only `--api-route=^/api/`, no skip-auth routes) answered that fetch with the Keycloak login page, silently dropping the manifest (no install prompt, fullscreen, landscape or icon); adding a skip-auth route for it was rejected to keep auth intact. `render/decision_window.js`'s `clampPosition(pos, size, viewport, limits)` now keeps the title bar (the window's only drag handle) below `limits.top` and never below the viewport bottom (`limits.handle`): the mount measures `#game-toolbar`'s bounding rect bottom at every clamp (safe-area drop included, `NO_TOP_ROW_PX` 0 when the row is absent or has no box, so the lobby and row-less test pages keep the old reach-the-top behaviour) and the handle's height (`FALLBACK_HANDLE_PX` 32 unmeasured). Dragging, a remembered spot shown on a poll, a window already showing at mount and a `resize` all clamp through it; before, a drag to the top edge (or a saved top 0) left the title bar under the z-index 55 row for good. `tests/test_ui_viewport_layout.py` measures one more 844x390 frame (`decision=top`, the last one; the safe-area frame is now `measurements[-2]`) with top 0 saved and the window mounted twice.
- 2026-09-14 T11 one board per read: every read-only board computation shares one engine snapshot per request (`board_scope.read_on_one_board`/`on_one_board`: `serialize.state_view`/`snapshot`, `get_legal_actions` rows plus candidates, `legal_action_rows` so `records.legal_actions_before` inside `store.locked`, `affordances`, `commands._resolution_state`, `engine_api.without_loyalty_mana_advice`); scopes close before `commands.run_command` and a `BoardSnapshotStale` re-reads with `uncached_boards()`. A POST action's own command still rebuilt the board per permanent inside the engine (122 boards at 28 permanents), which the api cannot scope: `snapshot.command` never caches, so the engine (branch `t11-engine-command-scans`: 3e88673 `snapshot.scan_pin`, 6184313 the pinned scans, 7ece922 the pin's cross-thread drop races, f5982d4 a command lowers only the pins it lifted; stacked on T13 efe067b) pins one board per trigger scan, state trigger sweep and state-based action check. Measured in-process at the final heads (api 8ec3a6e + engine cb3bd19, whose src is f5982d4's), 28 permanents, median of 7, before (api 44d56cb + engine 538ad5f) vs after: GET state 1249 boards/1492 ms to 1/10.2, GET legal-actions 511/618 to 1/10.3, POST play-land 719/874 to 10/19.7, POST pass-priority 806/993 to 6/18.3 (boards counted as calls to the engine's real computation, identical in all 7 games; no quiet window came in 25 minutes, so before and after ran interleaved, one game per process, with 2-5 other pytest processes live, median 2, load average about 5.2; an earlier quiet-machine run before f5982d4 gave the same boards and ms within 1.5 ms after, 40 ms before); an earlier run on a real Boros-vs-Frog snapshot at turn 11 (64 permanents) gave GET state 6500 -> 37 ms, legal-actions 4187 -> 48, pass 5412 -> 71. Deploy: the image copies the sibling `../magic-cards-model` checkout (uv.lock has only an editable path source, no sha), so shipping needs `code/magic-cards-model` at the engine commit and the superrepo pointer moved - no lock change. `test:pg` ran against a local mise postgres through `MCM_TEST_DATABASE_URL` (Docker Desktop's vmnetd step needs an interactive admin prompt); the role needs a password or `test_store_selection`'s components-only case picks the in-memory store.
- 2026-09-14 T15 auto-pass after paused stack actions: with keep-priority off, a cast/activation/equip/cycle whose route answered `paused: true` used to pass at once (refused with a toast, a decision was open) and then never pass after `decide()` completed it. `app.js`'s `afterStackAction` (shared by `fire`, `fireEquip`, `cycle`) now owes the pass as `store.pendingRelease` `{seat, generation, readSeq}`; `decide()` reads it before posting, only for the question on the board (same `generation`), clears it on a refusal (`accepted: false` - /decide never answers `allowed`, so a refused decide never toasted before either), keeps it on a re-pause and passes once on completion. Cleared on cancel, any new action, leaving the game, and by `settleRelease` once a board read issued after arming shows the question gone. Cycle now passes like any activation (no tick, standing default). Investigator's premise corrected: on engine 538ad5f an activation naming no sacrifice is REFUSED ("sacrifice needs 1 card(s) chosen, 0 given"), not paused; the real pause is `targets` omitted (`Game.begin_activating`: None asks), proven in `tests/test_activate_sacrifice_cost.py`.
- 2026-09-14 T16 postgres Decision codec is field-driven: `repository_postgres.py`'s `_encode_dataclass`/`_decode_dataclass` walk `dataclasses.fields` with `get_type_hints` (tuples rebuilt as tuples, UUIDs, enums, pydantic `TargetRef`, nested dataclasses, `X | None`), a field type with no rule is a `TypeError` naming the field at encode time, and a stored row missing a field takes its dataclass default. Why: the hand-written key list (856cb51) predated engine 9947808's `Decision.quality` and never had `mana_sources`, `lethal_damage`, `default_damage`, `trample`; `paused_command` reads the pause from the DB on every call and `resume_command`/`pause_is_live` compare it to the replay by full equality, so production game 07c5f818 (Cavern of Souls, `choose_a_quality`) answered every `/decide` with 409 "replay diverged", and so would every as-enters choice and damage-assignment pause. Nothing caught it because `mise run test:pg` sets only `MCM_TEST_DATABASE_URL`, the app store stays in-memory, and the contract suite stored only a MAY. `tests/postgres_store_testkit.py`'s `postgres_store` now rebinds every module's `store` to a real `PostgresRepository` for api-level tests; `tests/test_repository_postgres_decision_codec.py` fails when the engine adds a field its value table lacks.
- 2026-09-14 T17 speed-fix review: (1) a `CommandDivergenceError` from `app._resume` (decide, attack, declare-blockers) clears the stale pause under the lock before the 409, whose body is now stable - `detail`/`reason` = `commands.QUESTION_EXPIRED` ("This question has expired ... Re-issue the original action to be asked again.", no "diverged"), `expired: True`, `allowed`/`accepted: False`; the replay comparison stays in the server log. Why: 6184313's per-computation `board_effect_id` makes a pause stored before a deploy diverge after it forever. `_dry_run_declaration` then still never wrote the slot (B6-09), leaving the next real command's `_pause_slot_guard` to clear it; superseded by 1352236 (next line), which runs the dry-run attack under `store.locked` and clears a diverged DECLARE_ATTACKERS pause before its 409. `ui/api.js` puts the parsed body on the thrown error (`err.body`) and `ui/app.js` `send()` hands an `expired` 409 back as a refusal, so `decide()` toasts and refreshes; seatkit already reads a body carrying `allowed` as a refusal and only retries `stale: True`. (2) `board_scope.STALE_BOARD` = (`BoardSnapshotStale`, engine `layers.NotOnBoard`, a `KeyError` subclass `ComputedBoard.of` now raises): `read_on_one_board` re-reads uncached on either, and every broad `except` around an engine board read inside a scope (`engine_api` loyalty-advice filter, `mana_ability_at`, `_sole_mana_ability`, `_expanded_to_the_abilitys_own_count`, `list_abilities`) re-raises them first. Not touched, on purpose: `serialize._target_refs`/`host_index` and `records._named*` (no board read in the try), `affordances.alternative_costs` and `commands` replays (they work on a copy, whose changes cannot make the committed game's snapshot stale). (3) `engine_api.put_card_in_zone`/`move_card`/`lift_card` wrap their in-place zone list edits in `board_scope.announced_edit` (`announce_mutation` + `mutation_landed`), so a read racing `POST .../cards` recomputes. Tests: `tests/test_decide_diverged_pause.py`, `tests/test_board_snapshot_scope.py` (a `sys.settrace` hook makes a real stale board inside the guarded engine call; the race parks GET state in `serialize._card_view` with `threading.Event`s).
- 2026-09-14 T17 review follow-up: the dry-run attack (`action_attack` with `dry_run`, resuming a standing DECLARE_ATTACKERS pause via `_dry_run_declaration`) is a resume path too, so it now runs under `store.locked` and clears a diverged pause before the `QUESTION_EXPIRED` 409 - before, the 409 said "cleared" while the pause still stood. This is a deliberate exception to "a dry run never writes the slot": a pause that fails the replay under the lock is never live, so clearing it cannot delete a live tape. Pinned by `tests/test_decide_diverged_pause.py` (in-memory and Postgres). declare-blockers has no dry-run path.
- 2026-09-14 T17 board counts retaken at api 1352236 + engine f26408a with the harness that gave T11's 1/1/10/6 (28 permanents; alice holds Lightning Bolt, Grizzly Bears and Plains; both players have the city's blessing; one warm-up game, then 7 fresh games; boards counted as calls to the engine's `Game._compute_board`): GET state 1, GET legal-actions 1, POST play-land 10, POST pass-priority 6, the same in all 7 games, so no change. The same harness on archived T11 heads (api b6f3a20 + engine cb3bd19) also gives 1/1/10/6. A second scratch harness, which also uses `test_action_answer_size`'s board but a fresh game per request, only Plains and Lightning Bolt in hand and no city's blessing, reads 1/1/11/7 on both T11 and T17 heads. Its extra board on each POST comes from that setup, not from T17, so compare counts only within one harness.
- 2026-09-14: GET hypothetical-facts runs on one board (`@on_one_board` on `build_hypothetical_facts`): the api's own boards went 15/627 to a constant (small/large battlefield). The engine's `payability_matrix` still computes boards on its decoded per-land copies (38 vs 164 at 4 vs 28 permanents); no api scope reaches those copies, so `tests/test_board_snapshot_scope.py` counts only the api's share.
- 2026-09-14 S2 arming re-review (6 findings, all fixed at once): (1) `script_tools._arm_call_template` used to build ONE `pass_until` window out of DIFFERENT armable entries' own `when`s (earliest step across all of them, `whose_turn` from the top one only) - matched neither script; `_next_occurrence` now computes each entry's own nearest `(phase, step, whose_turn)` from the CURRENT board (never just the earliest step named in turn order - a `theirs` `when` with a step already behind it this turn must roll to the NEXT one, not wrap to next turn), and the template arms only the entries sharing the single nearest window, leaving the rest listed under `armable` without being in the call. (2) `pass_until`'s "reached" stop (`autopass_stop_reason`) fires the instant a destination step begins, board-wide - for a `theirs`/`either` destination that is usually still the active opponent's own first priority in it (CR 117.3a), so arming (which needs this seat to hold priority) silently never fired; `_wait_for_own_priority_at_destination` now keeps polling, inside the call's own bounded wait, until this seat gets priority in that SAME occurrence or the step moves on without it (never via `_pass_until_once`'s own `resume`, which would reset the started-occurrence exemption and swallow the very "reached" it needs to react to). (3) a chained run's own trailing `exit_block.moves_done` is only the LAST script's count (a child's `_Run` starts at 0) - a parent that made a real move before handing off to a zero-move child reported the whole run as untouched, so `script_tools._record_run` left the parent unlocked in the hint cache, re-offered, and the ledger's own already-ran gate then refused the retry; fixed by treating `len(scripts_run) > 1` as its own unconditional "moved" signal, independent of the trailing count. (4) that release rule and `records._prior_script_run`'s had drifted (the ledger released an `already_ran` collision finish, the hint never did) even after an earlier round fixed the advise-specific half of it; both now call one shared `script_tools.releases_slot(moves_done, exit, advise=..., chained=...)` - `records.py` always passes `chained=False` (every ledger row is one chain segment's own accurate finish, never the aggregate). (5) `arming.arm`'s batch loop read `run_budget` before `_judge_round`'s own fetch and would start a run with whatever near-zero time was left, posting a `start` row the run's own time cap then cut off with no `finish` - which permanently blocks the script for the step; now reads the budget AFTER judging and refuses to start below `MIN_ARMED_RUN_SECONDS` (3.0), stopping the batch with a note instead. (6) `seatkit._fire_armed_scripts` handed arming a full fresh `RUN_CAP_SECONDS` (25s) on top of whatever the wait already spent (up to its own `MAX_WAIT_SECONDS`, also 25s) - up to ~50s for one `wait_and_arm`/`pass_until` call, breaking `MAX_WAIT_SECONDS`'s own under-30s rationale; both now share one `TOTAL_CALL_BUDGET_SECONDS` (29.0 = `MAX_WAIT_SECONDS` + a 4s allowance) measured from the call's own entry, and skip arming entirely with `armed_skipped` when less than `MIN_ARMED_RUN_SECONDS` of it is left once the wait is done.
- 2026-09-14 S2 arming third re-review (4 findings, all fixed): (1) `pass_until`'s wait for OWN priority at a `theirs`/`either` destination (`_wait_for_own_priority_at_destination`) used to answer `finished: true` outright the moment its bounded wait ended without ever getting priority there - default `max_seconds=0.0` (the tool's own default, and what a hand-typed `arm_call` got before this) meant the wait never even ran once - carrying no `armed`, no note, and critically no `resume`; a caller that took that at face value and called again fresh got a run stamped from the board it was standing on, which exempts THAT destination occurrence and skips to the next one a whole turn later. Same for `should_stop` interrupting mid-wait (dropped `resume` the outer retry loop's own interrupt handling already kept), and for a stack object arriving mid-wait (left the stale "reached ..." text instead of reporting it). Fixed: the wait now hands back `finished: false` with a `resume` carrying the run's ORIGINAL starting stamp on every way out short of actually getting priority (deadline, interrupt); a stack arrival is reported like `autopass_stop_reason`'s own check would, and arming still runs on the result. **`arm_call`'s final shape** (what the prompt/brief work on `s2a8-prompts` should match): a seat's template now carries `max_seconds=25.0` (`seatkit.MAX_WAIT_SECONDS`) as its own argument - `pass_until(player_uid="<uid>", step="<step>", whose_turn="<mine|opponent|any>", arm=[...], arm_reasoning="<say why>", opposing_archetype="<archetype>", max_seconds=25.0[, inputs={...}])` - never for an advisor's `wait_for_priority(player_uid="<uid>", arm=[...], arm_reasoning="<say why>", opposing_archetype="<archetype>"[, inputs={...}])`, which has no matching wait to bound. The behavioural piece a driver prompt needs to say explicitly: a `pass_until` call with `arm` set can come back `finished: false` with a `resume` token even though `arm` was given - that means the window opened but this seat has not held priority in it yet (or the wait ran out, or was interrupted); the caller must call `pass_until` again with that exact `resume` (same other args) to keep trying in the SAME window, never a fresh call with no `resume` - that would silently skip this window and wait for the next one instead. (2) `_next_occurrence`'s own step-in-turn-order comparisons (`_next_step_at_or_after`/`_has_step_at_or_after`, now `_next_step_strictly_after`/`_has_step_strictly_after`) used `>=` against the board's current step, so a `when` naming the step the board is already sitting in counted as its own "next occurrence" - `pass_until`'s starting-occurrence exemption then reads that as the NEXT one, skipping this turn's actual window; `cleanup` (no priority, CR 514.3) was also sorted as "before everything", the same as `untap`, when it is really last, so a `theirs`-only `when` past every named step at cleanup still came back as recurring this turn. Both comparisons are now strict, and `_step_order_key` sorts `cleanup` after every named step. `_arm_call_template` also grouped kept `armable` entries by the exact `(step, whose_turn)` triple, which excludes a `theirs` and an `either` entry landing on the same real occurrence (`theirs`->`opponent`, `either`->`any` - different strings); it now groups by `(phase, step)`, the occurrence itself, so both get armed together in one call. (3) a prior round's fix (see the round-2 line above) over-corrected `releases_slot`: it stopped releasing an `already_ran` collision finish on EITHER side, so once a start collided (`already_ran`) and the run it collided with was itself later released (zero moves), a fresh start still refused, blocked on the collision row alone (`start A, start B` collides, `B` finishes `already_ran`, `A` finishes with 0 moves and releases, `start C` still 409s). `releases_slot` gained `already_ran_releases` (default `False`, the hint's own narrower "do not re-suggest a just-collided script to myself" job) and now treats a missing `moves_done` (`None`, not `0`) as always blocking rather than `prior.get("moves_done") or 0` silently releasing a finish row that never said how many moves it made; `records._prior_script_run` passes `already_ran_releases=True` and the raw, unclamped `moves_done`. (4) `_record_run` only ever locked `script_id`, the chain's originally-invoked parent - a chain A->B->C left B and C, each separately registered, still offered by `applicable_hint` and still eligible for `arming._judge_round`'s own conflict check. Every id in `exit_block['scripts_run']` strictly before the trailing one made real moves by construction (`_next` only chains a completed run into its next script once every one of that run's own moves has sent), so `_record_run` now locks the parent, every middle segment unconditionally, and the trailing segment only when `moves_done > 0`.
- 2026-09-14 S2 A6 buddy drafts: `stage_advice(player_uid, step, whose_turn)` (new MCP tool, `stage-advice` CLI command, `contract/tools.json`/`openapi.json` regenerated) arms an advisor's own *next* committing call to be captured into `Seats.advice_drafts[uid]` (`{"window": ..., "action": None}`, shared by reference into every `Seat` this uid mints, same as `_wake_signatures`) instead of posted - the same next-occurrence stamp `start_autopass`/`resume_autopass` give `pass_until`. `Seat._advise_answer` fills in `"action"` on that next call, past the dry-run probe/overlong-rationale guards (a refused probe leaves the arm untouched). `seatkit.resolve_advice_draft`, wired into `mcp._tool_result` for every advisor answer (action tools, `wait_for_priority`, `pass_until`), re-checks three independent conditions on every poll and posts the moment all hold: (a) window (`draft_window_status`, rank-ordered via `_FULL_STEP_ORDER` so "already passed" is detected fresh each poll without predicting a future turn number), (b) `opponent_board_signature` unchanged (opponent permanents id/tapped/counters, stack ids, both life - never the advised player's own hand/mana), (c) legality (re-`dry_run` for the 7 validated shapes, `_unvalidated_action_still_legal`'s board-truth predicates for the 5 that cannot be). Any failure drops the draft and answers `draft: {"status": "stale", "failed": "window"|"board"|"legality"|"refused"}` (`"refused"` when `/advice` itself refuses the post - a blank reasoning would already have been caught at draft time, so this is not expected in practice); still-waiting answers `{"status": "held"}` and a real post answers `{"status": "posted", "log_seq": ...}`. Same-day review round (6 findings, all fixed): (1) the window's own "after" check compared step rank alone, wrong the moment the target step ranks before the staged one (next occurrence means next turn) - `Seat.stage_advice` now stamps `window["stamped_step"]`/`["stamped_turns_taken"]` (persistent, unlike `forget_starting_occurrence`-cleared `started_step`/`started_turn`) and `draft_window_status` compares against those, `turns_taken` throughout (not `turn_number`, which CR 500.7's extra turn does not move); (2) each `seat_cli.run()` is a fresh process/`Seats()`, so a staged window never survived one CLI call to the next - now persisted to a per-(game, uid) JSON file (`MCM_CLI_DRAFTS_DIR`, else `$WORKSPACE/cli-drafts`, else a repo-relative fallback), loaded/saved around every command, with `seatkit.draft_status_for_answer` shared between `mcp._attach_draft` and the CLI's own `emit` so both transports report identically; (3) an armed script firing through `_advise` was itself a committing call, so a staged-but-empty window swallowed the script's own proposal (`{"drafted": True}` misread as `REFUSED "None"`) - `Seat.call_json`/`_advise_answer` gained `capture_draft` (default `True`), `_advise` passes `False`; (4) an armed script proposing and a captured draft's window being reached in the same answer could both post - `draft_status_for_answer` now checks `state["armed"]` first and answers `{"status": "superseded_by_script", "script": ...}`, dropping the draft, when a script already proposed; (5) two concurrent `resolve_advice_draft` calls for one uid could both post, and the unconditional pop at the end could delete a brand new restage - a per-uid `threading.Lock` (`Seats.advice_draft_locks`, shared by reference like `advice_drafts`) now guards the whole read-check-post-pop sequence, non-blocking (a second caller answers `"held"` at once), and the final pop only drops the entry if it is still the exact object this call claimed (`_pop_draft_if_same`); (6) a decide draft's legality check asked only whether *some* decision was pending for the player - now stores `decision_kind`/`decision_generation` (the pending decision's own `repository.GameRepository.pause_generation`) at capture time and requires both to match the pending decision standing at the window. `wait_for_priority(wake_to_draft=True)` (advisor-only, refused for a seat) additionally wakes when the board changes while the human does not hold priority and nothing is staged, so a buddy gets nudged to draft ahead of time rather than only once priority actually arrives. `AdviceIn.drafted_at_seq` rides into the ledger row's `detail` unread by this task - S2 A7's metrics reads it back. `test_every_mutating_mcp_tool_takes_reasoning` needed `stage_advice` added to `EXEMPT_TOOLS`: it mutates no `Game` and the call it arms carries its own reasoning already.
- 2026-09-14 S2 A8 prompts and briefs: `for_turn`, `arm`/`armable`/`arm_call`, `stage_advice` and `wake_to_draft` all shipped in S2/S2 A6 without ever reaching `driver/prompt.py` or either CLI brief - not one field name. Every "scripts never run on their own" sentence (both MCP prompts, both CLI briefs) is now "Scripts only run when you start them with `run_script`/`script run` or arm them on a wait; a run you neither started nor armed was started by someone else." The seat loop teaches plan-ahead (`declare_plan(for_turn="next")` during the opponent's turn) and arming a `pass_until` by copying the hint's `arm_call` verbatim - never composing the arm arguments by hand, per a live warning that `s2-arming` was still reshaping `arm_call`'s own template and `pass_until`'s wait-for-own-priority behaviour while this task ran, so the prose pins only the tool name and the `arm` argument and tells a seat to resume with `resume` when a note says it is still waiting for its own priority to fire something armed. The buddy prompt gained the same arming note on `wait_for_priority` plus a new "Draft ahead while your human plays" section (`wait_for_priority(wake_to_draft=True)` then `stage_advice`, reading `draft.status` - `held`/`posted`/`stale`/`superseded_by_script` - and re-thinking on `stale`). The CLI briefs got the same content in CLI vocabulary (`--arm`/`--arm-reasoning`, `plan --for-turn next`, `stage-advice`); `ai_seat_brief.md`'s old "Keep a plan, in writing" scratch-file section (pre-`declare_plan`) is replaced with a real "Declare your plan before you act" section, and `mcp-seat wait` gained a `--wake-to-draft` flag (`seat_cli.py`, wired to `wait_and_arm`'s existing `wake_to_draft`/`draft_staged` params) since the CLI had no way to reach it at all before this task. `turn_zero` grew 6.3%, well under the ~15% cap; `advisor_turn_zero` grew 15.4% for the new draft-ahead section, which carries no such cap. Merged `s2a6-buddy-drafts` onto `s2-arming` first (2 conflicts: `MEMORY.md`, kept both entries; `seatkit.wait_and_arm`, kept both sides - the arming re-review's `call_start` budget capture and the buddy drafts' `wake_to_draft`/`draft_staged` threading through `block_until_wake`). This merge (S2 A8 into arming round-3): merged `s2a8-prompts` (HEAD 50195d2) back onto `s2-arming` (HEAD 8cbd171, three more re-review rounds since `s2a8-prompts` branched at df6eab8) with `git merge --no-ff`; conflicts in `MEMORY.md` (kept both sides' entries, this line added) and `seatkit.py` auto-merged clean by git with no marker conflicts - verified by hand that round-3's `pass_until`/`_wait_for_own_priority_at_destination` resume/give-up logic and buddy-drafts' `wake_to_draft`/`draft_staged` threading through `block_until_wake` both survived intact.
- 2026-09-14 S2 A9 protocol paragraph: the `seatkit-20` changelog in `seatkit.py` only ever named S1's script hint (`scripts.applicable`, `turns_taken`, `invoked_via`); folded in the rest of what ships in the same bump, verified against the actual code by grep rather than written from memory - plan ahead (`declare_plan(for_turn="next")`, 409 `own_turn_in_progress`, the upkeep/draw empty-stack `pass_priority` exemption from `require_plan`), arming (`arm`/`arm_reasoning`/`opposing_archetype`/`inputs` on `wait_for_priority` and `pass_until`; `armed`/`armed_conflict`/`armed_error`/`armed_skipped`; `scripts.armable`/`arm_call`; `invoked_via: "armed"`; `pass_until`'s `finished: false`/`resume` while it waits for this seat's own priority at an armed destination), buddy pre-drafts (`wake_to_draft`, `stage_advice`, `draft.status`'s `posted`/`held`/`stale`/`superseded_by_script` values and its `failed` sub-key, `AdviceIn.drafted_at_seq`), and S2 A7's ledger stamps (`active_player`, `priority_holder`, `turns_taken` on every row, distinct from the state view's own `turns_taken`). No renames needed - every key and tool named already matched the code verbatim. `own_turns_taken` (a player-view field) and `hypothetical-facts` ship on `o-opening-dsl`, not yet merged here - deliberately left out of this paragraph; add them to the `seatkit-20` changelog when that branch merges. `tests/test_protocol_boundary.py` gained one assertion test per bundle plus a negative test pinning the two `o-opening-dsl` terms absent. No schema or tool shape changed by this task, so `openapi.json`/`contract/tools.json` needed no regeneration.
- 2026-09-14 S2 A6 second review round (5 findings, all fixed, 4 commits): (1) each `seat_cli.run()` is a separate OS process, so the first round's file-based persistence still raced - a background `wait` that loaded nothing could delete a draft a foreground `stage-advice` wrote moments later, and a background `wait` racing a foreground `pass` on the same ready draft could both post. `seat_cli._cli_draft_lock` now holds an `fcntl.flock` on a sibling lock file for the WHOLE command (re-reading fresh under it, never a startup snapshot), `_save_cli_draft` writes atomically (temp file + `os.replace`) and only deletes a file that still carries the `_cli_token` this call itself loaded. (3, same commit - the two are inseparable, same functions) `_cli_drafts_dir`'s unguarded `mkdir` crashed every advisor CLI command before its own action ran when the drafts directory was unwritable, and `emit()` persisted before printing - an exception there lost the action result. Every disk op now catches its own `OSError`, logs to stderr once, and folds it into `draft.note` instead of raising. (2) `draft_status_for_answer` superseded ANY captured draft the instant a script advised anywhere in the same answer, even one staged for a window nowhere near reached - the check moved inside `resolve_advice_draft`, after window/board/legality all hold, so only a draft genuinely about to post can be superseded; `_script_already_advised` also widened to catch a MANUAL `run_script`/CLI `script run` proposing (`script_exit` threaded through `mcp._attach_draft`/`seat_cli.emit`), not only an armed script firing inside `wait_for_priority`'s poll. (4) `state["turns_taken"]` is `None` whenever `game.turn` itself is `None` (before CR 103.8's first turn, earlier than CR 103's own mulligan phase which already has `turns_taken == 0`) - the stamp went `None` and `draft_window_status`'s "after" check never fired on it, so a draft staged that early sat held forever and `wake_to_draft` never woke again; `stage_advice` now refuses outright (`STAGE_ADVICE_PREGAME`) and `draft_window_status` normalises a `None` `turns_taken` to `0` as a second guard. (5) `_advise_answer` set `armed["action"]` before the `recent_log` HTTP call that fills `opponent_signature`/`drafted_at_seq` - a concurrent `resolve_advice_draft` could see the draft as captured but missing those fields and drop it as stale-on-board before it was ever whole; the whole action dict is now built in a local first and installed into `armed` under the per-uid lock in one go. Commits split by what is actually separable: findings 1+3 share the same `seat_cli.py` functions (one commit), 2/4/5 land in `seatkit.py`'s own disjoint functions (one commit each) - each commit independently green on the full suite, verified by reconstructing intermediate file states via `git apply` on hand-split hunks (never `git add -p`, not scriptable non-interactively) plus one direct revert/reapply for `seat_cli.py`'s small `script_exit` param (finding 2's own CLI-side wiring, which needed seatkit's finding-2 signature to exist first).
- 2026-09-14 S2 A6 merge into arming (round 2/3 fixes + narrowed lock scope): merged `s2a6-buddy-drafts` (HEAD 9364a14) onto `s2-arming` at `git merge --no-ff` 8ee24d2, bringing in 91f955d/1ce5ae7/16bca10/ac9ce66/e02bcec (cross-process flock, supersede only in-window, emit order, `STAGE_ADVICE_PREGAME`, atomic capture) and 9364a14 (`seat_cli.run()` restructured into `_dispatch()` with the CLI draft lock narrowed to its own read-modify-write sections, never a whole command). Conflicts: `seat_cli.py`'s "wait" command - `s2-arming`'s own A8 had independently wired `--wake-to-draft`/`draft_staged` onto the PRE-restructure code; kept the narrowed-lock structure and reapplied wake-to-draft inside it, reading the staged draft fresh off disk (`_load_cli_draft`) rather than the in-process cache the old code read, matching the "never trust a snapshot older than the wait" rule `_dispatch`'s own docstring lays out (a misaligned diff3 hunk also left a duplicate mis-indented `cast`-body fragment plus stray markers, removed by hand). `MEMORY.md` and `tests/test_buddy_drafts.py` conflicts were both two-sided independent additions at the same seam - kept both. Then checked the `seatkit-20` paragraph and both buddy prompt/brief against the merged code: `stage_advice`'s pregame refusal (`{"staged": false, "reason": STAGE_ADVICE_PREGAME}`) and the CLI-only `draft.note` field (`seat_cli.emit`, no MCP equivalent - MCP keeps its draft in memory, never on disk) were both client-visible and unnamed; added test-first (`test_seatkit_20_names_the_stage_advice_pregame_refusal_and_cli_draft_note`) then the paragraph. Same test-first pattern for one short clause each in `driver/prompt.py`'s and `ai_buddy_brief.md`'s "Draft ahead while your human plays" section, naming that `stage_advice`/`stage-advice` is refused before the first turn.
- 2026-09-14 int-agents-dsl merge of `s2-arming` (ae64525) onto o-opening-dsl: only `script_tools.py` and this file conflicted textually. `_judged` now takes one `facts: _FactsOnce | None` (replacing listing's `hypothetical`/`hypothetical_unavailable` pair) beside S2's `evaluate_conditions`; `_FactsOnce` fetches `Seat.hypothetical_facts` at most once per judgement and only when a `needs_hypothetical` play reaches condition evaluation, so `listing`, `applicable_hint` and `arming._judge_round` judge facts identically, a `HypotheticalFactsUnavailable` marks only those plays `failing_condition.list == "hypothetical"` with the error text, and `armable` (`evaluate_conditions=False`) never fetches. Gate order in `_judged`: priority, archetype, when, pending decision, armable-only skip, facts unavailable, conditions. Pinned by `tests/test_hint_arming_hypothetical_facts.py`.
- 2026-09-14 test decks dependency removed: `test_playscript_end_to_end.py` used to read `open-fetch-unlocks.json` off a sibling `../mtg-decks` checkout; it is now vendored verbatim (from mtg-decks `f5b86bc`) at `tests/fixtures/guides/ahead-of-pin/decks/boros-aggro/plays/`, outside `decks/` so the pinned corpus the seed tests glob and register is unchanged - see that README's "Plays ahead of the pin".
- 2026-09-14 supersedes the S2 A9 protocol paragraph line above: that line deliberately left `own_turns_taken` and `hypothetical-facts` out of the `seatkit-20` changelog because they still lived on the then-unmerged `o-opening-dsl` branch. `o-opening-dsl` has since merged and both are named in the paragraph (the int-agents-dsl merge line above, and 5aa35f0 "seatkit-20 paragraph names the opening-plays DSL additions"); this int-agents-dsl follow-up pass further verified the paragraph against the merged code and added two more sections it still left implicit - `armed_facts_unavailable`/`scripts.note` (arming/hint now surface a `HypotheticalFactsUnavailable` fetch failure instead of only marking plays not applicable and staying silent), and the live-together active cap, `enters`'s three values, `prefer`/`ties` on `play_land.card`/`cast.card`/`activate.source`, and `unlocks*` requiring a legal target with X-cost cards never counting as unlocked. `tests/test_protocol_boundary.py` gained one assertion test per addition.
- 2026-09-19 vendored mtg-decks slice refreshed to `decks-landing` d681d42 (41->100 plays; `tests/fixtures/guides/README.md` has the corpus diff), and `tests/test_playscript_end_to_end.py`'s generic stager (`stage()`) taught three things the new `open-*` family needed that it never had: (1) a `turn` field on `Board` (`TurnMachine.own_turns_taken`) plus `holds()`'s "turn" kind and `apply_board`/`fetch_shock_board` stamping `game.turn.turns_taken_by` directly - every new play gates on it. (2) an `opposing_archetype`/`inputs` pair on `Board`, pinned from a play's own true/false `input` conditions AND its top-level `opposing_archetypes` gate (a separate, non-condition check `script_tools` runs first), threaded into `listing`/`run` calls. (3) the big one - `enters_any`/`unlocks`/`unlocks_by_search`/`unlocks_by_basic_search`/`affordable` (`selectors._HYPO_FIELDS`) can't be decided offline at all (`HypotheticalRequired`). Split into two readings: `loose_matches` (hypo fields dropped) for any "must find a card" context - counts loop, move card/source, library picks - and plain `matches(..., hypo={})` (hypo fields never match) for anything guarding a `false` condition or ranking/filler-safety, where "never matches" is the safe direction. Neither reading can pick a card that's actually `unlocks: true` for real, so `stage_hypo_count` exists for a `true` count naming a hypo field: it tries the deck's own real land candidates (best `move_prefer` tier first) against a throwaway scratch game via the real `GET .../hypothetical-facts` route (`_real_hypo_facts`), pairing each with no companion and then real one-mana-value-1-to-3 hand spells (`_companion_candidates`, excluding anything needing a target the empty probe board can't offer except `target player`/`target opponent`), extra mana lands for a companion costing more than the one land under test (`_mana_helpers`, checked against `untapped_mana` false guards first), and library lands the candidate's own search reaches (basics sorted first or last depending on whether the play's own selector wants `unlocks_by_basic_search` or keeps it a sibling play's territory) - plus a same-zone sibling-guard check so a combo that also flips a MORE SPECIFIC play's own guard true is rejected. Four plays stay `ENGINE_GAPS`-xfailed rather than faked: `instant-reanimator/bin-fatty-vs-permanent-exile` (Psychic Frog's "Discard a card:" battlefield cost has no DSL field to name it - `sacrifice`/`schemas.ActivateIn.sacrificing`/`.discarding` are always inline, never a paused decision the play's own `choose_cards` answer could resolve), `esper-blink/ephemerate-protect-lone-creature` (needs `stack_top`, and staging keeps the stack empty by design), `esper-blink/orchid-phantom-breaks-tron` (needs a Land on the OPPONENT's battlefield, which `Board` never represents), `living-end/open-land-idle-mdfc` (needs `land_face: true` on a real MDFC in hand, but `printing_view()` never carries a `faces` list so the only thing that ever matches is a plain Land, which then trips the play's own "zero plain lands" condition). One real production fix fell out of this: `runtime._activate_request`'s `sacrifice: "source"` always sent `sacrificing`, but an off-zone cycling/unearth/transmute keyword (CR 702.29a/702.84a/702.53a) spells its cost out as "[cost], Discard this card: ..." with nothing left to name and `off_zone_activation` refuses ANY object cost given at all - "this card" in the ability's own cost text (never a phrase a battlefield "Sacrifice/Discard a ..." selector-based cost uses) now means send neither `sacrificing` nor `discarding`, and `key = "discarding" if "discard" in cost_text ...` otherwise. Two more fixture-level updates the refresh exposed: `tests/driver/test_app.py::test_the_briefing_lists_the_registered_scripts` now compares against `prompt._clip(name)` (a play's own row is length-capped by design, and names got longer) rather than the raw name, and `fetch_shock_board` (7 call sites) now also stages a second land and a red card in hand for `fetch-untapped-shock`'s own newer true conditions.
- 2026-09-19 follow-up: closed all four of the above ENGINE_GAPS instead of leaving them xfailed. Three were the stager's own gap, not the engine's, and got real support: `Board.opponent_cards`/`opponent_zone` (a `count` condition naming `side: "opponent"` is staged from `universal_pool()` - every locally-cached printing, engine corpus plus this suite's scryfall cache, `functools.cache`d - never the deck's own `pool`, since the opponent's real deck is never modeled; `stage_opponent_count`); `stage_opponent_object_on_stack` (a `stack_top` true condition needs a genuine opposing object, which only really driving pass/cast/pass through the API can give - staged with a purely-generic-cost, untargeted instant found the same way); `printing_view()` now builds a `faces` list (`serialize._faces_view`'s own shape, `land_face: True` on the face naming Land) for a real `layout: "modal_dfc"` printing, so `selectors._has_land_face` stops falling back to "a plain land is its own land face". `orchid-phantom-breaks-tron` also needed `decline_opponent_yes_no` (a "may"/"pay_life"/"pay_energy" decision the trigger hands the OPPONENT can never be raced against a `run_script` call in progress - `runtime._foreign`, B7-09, treats the opponent answering their own required decision as this run being interrupted no differently than an unrelated action; answered between a stopped run and its `resume`, one-shot, never concurrently) and the generic test's final assertion accepting `exit: "interrupted"` when every one of the script's own moves finished, since a script cannot be faulted for what its own resolution legitimately handed another player. The fourth (`bin-fatty-vs-permanent-exile`) was real: added `activate.discard` (`schema.py`'s `DiscardChoice`/`Discard`, mirroring `sacrifice` with no `source` value - CR 701.9 only ever reads hand) end to end - `runtime._activate_request` now also builds `ActivateRequest.discarding` from it (`discard_count`, mirroring `sacrifice_count`), `contract/playscript.schema.json` regenerated, the format spec and `test_playscript_format_doc.py`'s exact-line assertion updated, and the mirror in mtg-decks `scripts/check_plays.py` + `docs/DECK-PIPELINE.md` landed on `decks-landing` (commit `67b580e`, `mise run test`: 4395 passed) rewriting the play itself off its old `choose_cards`-answer workaround; `conflict_corpus.json` needed no re-copy, already byte-identical both repos. Pin advanced `d681d42` -> `67b580e` (one file changed). Also found and fixed a real ordering bug in `test_ui_viewport_layout.py` while chasing its reported flakiness under load: appending a `src`-less iframe before setting `src` briefly navigates it to `about:blank`, which fires its own "load" event the polling code could latch onto and then read forever - `src` is now set before `appendChild`, frames load and settle one at a time instead of racing 13 at once (caps peak Chrome/CPU load to one frame regardless of what else is running), and the fixed two-`requestAnimationFrame` wait was replaced with a real poll for `#phase-track` actually gaining its items. A second, rarer failure mode (`readyState: "complete"`, `#phase-track` present, zero children, no script error - not reproduced often enough to root-cause past "likely a headless-Chrome iframe-navigation quirk") got `measurements`' own collection wrapped in up to 6 retries, each a fresh Chrome profile and port, so one flaky run can no longer cascade into every test in the file failing together.
- 2026-09-19 engine advanced bf3d1ca -> 03a891c (int-engine-dsl, the Reality Fracture set plus the number sweep, snapshot schema 21 -> 33): wired the two new `DecisionKind`s end to end. `CHOOSE_PILE` (CR 700.3's pile primitive, Liliana of the Veil's `-6`, Do or Die) is `PUT_ON_TOP_OR_BOTTOM`'s own boolean shape - `pile_one` is its own request/tool/DSL field name (not `accept`), matching the engine's `_ANSWER_FIELD_FOR_KIND` ("accept" internally, but a client must not confuse a two-way pile choice for a yes/no) and its exact refusal text ("needs a which-pile bool answer", "the same player cannot be chosen twice" for its sibling kind - `test_decision_validator_drift.py`'s parity check is word-for-word, not just accept/reject). `CHOOSE_PLAYERS` (CR 701.34a's Proliferate player half) is `CHOOSE_CARDS`'s own twin over `Decision.players`/usernames - `usernames` field throughout (matches the engine table, so it never joins `TOOL_FIELD_FOR_KIND`'s "differs from ANSWER_FIELD_FOR_KIND" set the way `pile_one` does). Both got: `decisions.py` (ANSWERABLE_KINDS, the two field tables, `PendingDecision.players`, coercion + validate_answer branches mirroring `Game.ask` exactly), `schemas.py`/`app.py` (`DecideRequest.pile_one`/`.usernames`), `mcp.py`'s `decide` tool (+ docstring, `contract/tools.json` re-exported), `ui/decisions.js` (`choosePile` reuses `chooseCardsOptions` to label pile one in the prompt; `choosePlayers` reuses the generic multi-select `options` control's `card_ids` draft key even though the real answer field is `usernames`, the same way `choice` is shared by two kinds), `baseline_seat.py` (`choose_pile` defaults pile one, `choose_players` mirrors the engine's own tail-slice default), and the playscript DSL (`Answer.pile_one`/`.players: list[me|opponent]`, `runtime.decide_body` resolving "me"/"opponent" the same way `_targets`' `PlayerTarget` already does, restricted to what the decision actually offered). `layer_remainder_notices` (new `Game` list, the "Empower Jace 2." sibling-sentence gap) added to `commands._NOTICE_LIST_NAMES` - it was the only *new* notice list this bump added; four PRE-EXISTING ones (`search_description_notices`, `unreadable_copy_exception_notices`, `unreadable_counter_amount_notices`, `keyword_line_notices`) were already unwired before this bump and were deliberately left alone (out of this task's scope, no test currently catches the gap). Emblems (CR 114.2, `Game.create_emblem`, Zone.COMMAND) and the two new predefined tokens (`JACE`, `HEARTWOOD`) needed no new API code at all - the shared-zone `views()`/`_card_view` path and `list_abilities` already serialize any `CardState`/`TokenState` generically, loyalty included (a planeswalker's loyalty is just the `loyalty` counter, already in `counters`). `CardState.prepared` (CR 722.3a) DID need a field: `serialize._card_view` adds `prepared: true` on the battlefield only (mirrors `attached_to`'s conditional-inclusion pattern), `ui/cards.js` grew a top-center `badge-prepared` chip. No Postgres-specific work was needed for schema 33 or any new `Decision` field - `repository_postgres.py`'s dataclass codec is fully reflection-based (type-annotation driven), so `players: tuple[str, ...]` round-trips the same way `options`/`card_labels` already did; only the codec *test*'s hand-written `_every_field_set()` table needed the new field added. `test_battle3_action_rows.py` needed an unrelated fix: the number sweep taught the engine to read Thraben Charm's mode 1 (dynamic damage, "twice the number of creatures you control"), so the "engine cannot read this mode" regression test had to move to mode 3 (still genuinely unreadable: "any number of target players' graveyards").
- 2026-09-20 T38, `guides propose` gates CONTENT as well as file paths (`playscripts/content_gate.py`, outcome `refused_content`, distinct from the file-path `refused` because the remedy differs - revert a file versus reword a sentence or file a report). Three decisions worth keeping. (1) The vocabulary cannot be a flat word list: several of the terms the design names collide head-on with Magic vocabulary, and matching the bare word would refuse real play prose. `stack` is CR 405, so only `stack trace` counts; `pod` and `deploy` are printed on cards (Birthing Pod, Deploy the Gatewatch), so only `in-pod`/`pods`/`the pod`/`pod restart` and `deployment`/`redeploy`/`a deploy`/`the deploy` count - deliberately NOT `deploy the`, which is both a card name and ordinary play prose ("deploy the threats"); `engine` is what a deck's card advantage runs on, so only `engine bug`/`rules gap`/`rules engine`/`the engine returned`; and `exception` is excluded when followed by "of" or "to", because "with the exception of" is just English. `tests/test_guides_content_gate.py` keeps both directions as parametrised corpora (SYSTEM_PASSAGES and PLAY_PASSAGES) so a new term has to clear the play-prose side before it lands. (2) The backtick rule is structural, not lexical: a backtick-quoted span counts only when it contains a code character (`_ - / . ( ) = : { } [ ] $ *`), so `hold_priority`, `--deck` and `scripts/x.py` are caught and a backticked card name is not. A single lowercase word in backticks (`decide`) is therefore a deliberate false negative - it is indistinguishable from emphasis on a card name, and refusing it would refuse every guide that backticks a card. (3) Fail-closed has exactly two reachable paths and both are tested: bytes that will not decode as UTF-8, and a boarding plan whose `matchups[].notes` is not a list of strings (or whose `play_plan` is not an object). Both answer `refused_content` with "could not decide", never an admit. The gate reads ADDED text only (difflib against HEAD for NOTES.md, a Counter difference over the editable plan strings) because a deck whose notes already trip it has to stay editable, which T40 needs. `plays admit` is deliberately NOT gated - every play `intent` in the DECKS corpus carries a "Limits:" clause about the DSL's own vocabulary gaps in exactly this register, so the gate would produce nothing there but false refusals; the exclusion and its reason live in `content_gate`'s own docstring, with a test pinning that the docstring says so.
- 2026-09-20 a game that ends now retires itself, which is what "conceding a game should also mean ending it" was asking for. The engine half was never the bug: `Game.concede` (CR 104.3a) drops the player, `_check_for_a_winner` (CR 104.2a) names the survivor, `app._record_game_over` writes the `game_over` row, the board says `winner`, the lobby stops counting the game live and offers its review - all of that already worked. What did not was everything this api holds *beside* the board: the `ai_pregame` watcher thread kept polling a finished game, `ai_seat.registry` kept each seat uid's runner or dispatch (and each buddy's), and in `wrapper` mode the driver stayed assigned - the leak `lobby.py` already calls permanent, a 409 on every later dispatch for the life of that container. Nothing released any of it except a human pressing End game (`lobby.reset_game`, reached only from `DELETE /api/lobby/games/{id}` and `DELETE /api/games/{id}`), so this was never concede-specific: a lethal-damage sweep leaked exactly the same way. New `lobby.retire_game` runs every bullet of `reset_game` except the delete (shared `_release_runners`), called from `app._record_game_over` - the one place `_run_command` and `_resume` both funnel a committed ending through, so every ending retires and there is no second copy of the machinery. It records `game_retired` (in `eval_report.NOT_PLAY`) only when something was actually released, so a harness game's ledger is unchanged. Deliberately NOT a delete: a finished game stays readable, keeps answering `type: game_over` (B6-06) rather than 404, and keeps its review - End game remains the only thing that clears the row. The AI seat therefore gets End game's `/release`, i.e. the driver's `TERMINAL_PATHS["handover_without_reflection"]` (review still filed, reflection text empty unless the agent got its reflection turn in first) rather than `reflection_turn`; that is the same trade End game has always made and the price of not leaving a driver assigned. Not done, noted: the release is on the request thread, so an unreachable driver can add up to `_driver_client`'s 30s to the ending action.

- 2026-09-20 The pregame backstop no longer answers a choice. Reported from a live kubernetes game: `ai_pregame.watch` found bob's CR 103.5 declaration owed, `ai_seat.registry.activity` said `GONE` (the k8s path: `K8sSeats.status` returned `state: "error"`, reason `"driver unreachable"`, which `_driver_activity` maps to `GONE`), and `settle` declared a keep for a seat whose agent had not booted yet - locking in a zero-land seven. Fixed generically rather than by teaching it to count lands: `ai_pregame.legal_moves` asks the ENGINE how many answers it would accept (`Player.can_mulligan` for CR 103.5, `Game.opening_hand_options` for CR 103.6) and `is_forced` acts only when there is exactly one. The justification for making that the whole rule is that both pregame windows close for good once they pass, so every pregame obligation is one-shot and a guessed answer can never be corrected later; the only remaining variable is how many answers exist. A choice is instead STALLED: `store.record_pregame_stall` (process-local beside `_automatic_pregame`, both repositories), one `pregame_stalled` audit row per (seat, move), `pregame.stalled` on every state read (`opening.pregame_view` now takes `game_id`), the play UI's `aiStallNotice`/ribbon, and `ai_seat_brief.md`. The notice names the three ways out - restart the agent, decide for the seat with the game's admin key (`_pregame_seat` already accepts it on all three pregame routes), abandon from the lobby - and clears itself when the move is finally made. Consequence worth knowing: `PregameOverruled` is now reachable only on a FORCED move, in practice CR 103.6's pass on a hand that offers nothing, so the mulligan-overrule tests moved there. Yes, a game can still be started with an AI seat whose driver never comes up: it deals, holds at CR 103.5 with the notice up, and plays the moment somebody decides. Separate defect, NOT fixed here: `k8s_seats` `driver_grace_seconds` defaults to 60s from pod `Running` while the driver's own `AGENT_BOOT_WAIT_SECONDS` is 360, so a slow-but-healthy boot is still labelled "driver unreachable" and reported `GONE` - harmless now that a gone seat only loses its forced moves, but the label is wrong.
