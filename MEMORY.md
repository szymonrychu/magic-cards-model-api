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

- **2026-09-08 - the initial alembic migration was edited in place, not
  superseded.** `374af1d25f73_initial_schema.py` had `down_revision = None`
  and nothing else in the chain - T9 had just landed this session, before
  any real deployment could depend on the old shape - so the `ForeignKey`
  removal above went directly into that migration rather than a second
  `alembic revision` on top of it. This is a one-time allowance for a schema
  still in its first iteration, not a precedent: once a migration has shipped
  anywhere real, the right move is a new revision, never editing history.

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
  means `store.py`'s singleton selection is still hardcoded to
  `InMemoryRepository` with no env-var-driven switch to `PostgresRepository`
  at all - T9 built the postgres implementation and this task exercises it
  end-to-end from a test, but nothing wires it into an actual deployment.
  That wiring is real, additional scope (a new env var, `store.py` choosing
  a class, probably `serve.py`/`main.py` touching startup) that neither T9
  nor T10 was asked to do - noted here so it is not mistaken for already
  done.

- **2026-09-08 - reasoning is required for an agent seat's *game* action, not
  for a CR 103.5/103.6 pregame declaration, even from the same seat.**
  `records.require_reasoning` gates every `actions/*`, `resolve-top`,
  `advance`, `decide`, and `state-based-actions` route, but deliberately not
  `pregame/mulligan`, `pregame/opening-hand/action`, or `pregame/opening-
  hand/finish`. Two reasons, not one: `ai_pregame.py`'s own backstop makes
  these exact declarations *for* a quiet agent seat with no reasoning to
  give (`opening.declare_mulligan`'s `automatic=True`), and the existing,
  pinned race-condition suite (`test_mulligans.py`) drives them directly
  over HTTP from a test harness that has none either - unlike a targeted
  spell or a declared attack, a pregame declaration has no live "why" this
  api could require honestly. `reasoning` is still accepted and recorded on
  all three when a caller does supply one.
