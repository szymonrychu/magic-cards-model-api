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
