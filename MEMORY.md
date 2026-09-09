# Project memory

Standing conventions and corrections, recorded as they came up - one dated
entry per decision or dead-end, per this workbench's convention. This repo
also has a gitignored `memory/` directory, but that is the continuous AI
seat's own runtime learning (mounted at `/workspace/memory` in the agent
container) - a different thing from this file, which is project memory for
whoever works on the repo itself.

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
