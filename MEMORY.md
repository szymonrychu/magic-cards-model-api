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
