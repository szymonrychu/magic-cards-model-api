# Task 11 report
Status: DONE (full suite still running at handback).
Commit: c90dd62 feat(sim): sim_match engine-only batches with wall-clock cap and polling (T-123)
Red observed: ImportError BatchManager (.logs/mtg-chat-sim/task11-red.log). Green: tests/test_sim_match.py + test_sim_server.py 28 passed; mise run lint clean.
Full suite: /workdir/workdir/.logs/mtg-chat-sim/task11-full-test.log (running).
Measure (45c_aggro vs 45c_control, seed 1, 10 games, defaults): capped at 303.8 s with 0 games finished (game 1 at step ~530, ~0.55 s/step); worker peak RSS 274 MB. Fixture decks earlier: 85-140 s/game.
Hooks: worker op match_slow and env MTG_SIM_TEST_MATCH_MAX_STEPS, both only with MTG_SIM_TEST_OPS=1. Defaults unchanged.

## Fix round 1

- Added tests/test_sim_match.py::test_worker_death_mid_batch_fails_it_and_frees_the_slot (FakeHandle raising WorkerDied): passed at once (code already correct); pins failed status, error naming the cause, killed worker, new start succeeds.
- Added test_shutdown_before_the_worker_spawns_never_leaves_an_orphan. RED first (log .logs/mtg-chat-sim/task11-fix1-red.log: worker not killed). Fix: BatchManager._run checks `_closed` after the factory and after start (`_refuse_if_closed`), failing the batch and killing the worker before any match op.
- Docs (README, MEMORY, sim_match tool description) now state: 85-140 s per fixture game, bundled 45c decks 0 of 10 in 300 s, default 300 s usually yields few or no finished games, cap env-configurable (MTG_SIM_MATCH_WALL_S). Caps unchanged.
- Verify: `uv run --python 3.13 pytest tests/test_sim_*.py -q` all pass; `mise run lint` all checks passed (log task11-fix1-green.log). ruff format reflowed two existing test statements (whitespace only).
