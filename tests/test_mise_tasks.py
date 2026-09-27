"""`mise run test` must stay docker-free no matter what `.env` (or the shell)
sets `MCM_TEST_DATABASE_URL` to - `mise.toml`'s `[env]` loads `.env` for
every task (`_.file`, the estate convention), so without an explicit
override the plain default suite silently starts hitting postgres the moment
a developer sets that variable locally for `mise run test:pg`'s benefit.
`mise run test:pg` is the mirror image: it must always run against postgres,
never quietly skip it, so it exports the flag `conftest.py`'s
`require_postgres_database_url` checks before it ever invokes pytest.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mise() -> dict:
    return tomllib.loads((ROOT / "mise.toml").read_text())


def test_plain_test_task_unsets_the_postgres_database_url() -> None:
    """Whatever `.env`/the shell put in `MCM_TEST_DATABASE_URL`, the task
    body itself must clear it before `pytest` ever runs - `[env]`'s `_.file`
    load happens before the task body, so only the task body can undo it."""
    run = _mise()["tasks"]["test"]["run"]
    assert "unset MCM_TEST_DATABASE_URL" in run


def test_plain_test_task_clears_the_require_postgres_flag() -> None:
    """`test` must not leave `MCM_REQUIRE_POSTGRES_TESTS` set even if it
    leaked in from the shell - only `unset`ing it (never assigning it) is
    acceptable here."""
    run = _mise()["tasks"]["test"]["run"]
    assert "unset MCM_TEST_DATABASE_URL MCM_REQUIRE_POSTGRES_TESTS" in run
    assert "MCM_REQUIRE_POSTGRES_TESTS=1" not in run


def test_test_pg_task_requires_postgres_to_actually_run() -> None:
    """`test:pg` must not be able to pass with the postgres suite silently
    skipped - it exports the flag that turns a missing `MCM_TEST_DATABASE_URL`
    into a hard pytest failure instead of a skip (see `conftest.py`)."""
    run = _mise()["tasks"]["test:pg"]["run"]
    assert "MCM_REQUIRE_POSTGRES_TESTS=1" in run


def test_test_pg_task_fails_loudly_if_postgres_never_comes_up() -> None:
    """The readiness loop must not fall through silently when postgres never
    answers `pg_isready` - it has to exit non-zero with a clear reason."""
    run = _mise()["tasks"]["test:pg"]["run"]
    assert "set -eu" in run
    assert "exit 1" in run


def test_test_pg_task_tears_down_a_worktrees_own_throwaway_postgres() -> None:
    """A worktree's postgres is its own throwaway project (COMPOSE_PROJECT_NAME
    suffixed off the main checkout's) - it must clean itself up rather than
    accumulate a fresh container/volume on every `test:pg` run. The main
    checkout's own long-lived postgres (COMPOSE_PROJECT_NAME unchanged) must
    never be torn down by this task."""
    run = _mise()["tasks"]["test:pg"]["run"]
    assert "trap" in run
    assert "docker compose down -v postgres" in run
    assert '"$COMPOSE_PROJECT_NAME" != "magic-cards-model-api"' in run


# -- eval:gauntlet (P5) ------------------------------------------------------
#
# The measuring stick has to be one command, or it is not a measuring stick -
# a harness that needs six shell steps remembered correctly is one nobody runs
# before claiming a change made the agent better. These pin the properties
# that make `mise run eval:gauntlet` that command.


def test_the_gauntlet_task_exists_and_says_what_it_does() -> None:
    task = _mise()["tasks"]["eval:gauntlet"]
    assert task["description"].strip()


def test_the_gauntlet_runs_the_harness_through_uv() -> None:
    """Never a bare `python`: the estate convention is that every tool runs
    through the pinned environment (`mise run`/`uv run`), and a gauntlet is
    worthless if two runs of it used two interpreters."""
    run = _mise()["tasks"]["eval:gauntlet"]["run"]
    assert "uv run python scripts/eval_gauntlet.py" in run


def test_the_gauntlet_forwards_its_own_arguments() -> None:
    """`mise run eval:gauntlet -- --seeds 20 --agent baseline` has to reach
    the script, or the seed count and the opponent are unchangeable."""
    run = _mise()["tasks"]["eval:gauntlet"]["run"]
    assert '"$@"' in run


def test_the_gauntlet_brings_up_the_database_it_reports_from() -> None:
    """The scorecard reads the `games` and `records` tables, so a run whose
    ledger lands in an in-process store is a run nobody can report on. The
    task brings postgres up itself and migrates it, exactly as `test:pg`
    does."""
    run = _mise()["tasks"]["eval:gauntlet"]["run"]
    assert "docker compose up -d postgres" in run
    assert "alembic upgrade head" in run
    assert "DATABASE_URL" in run


def test_the_gauntlet_fails_loudly_if_postgres_never_comes_up() -> None:
    """Never a silent fall back to a store the report cannot read - the same
    rule `test:pg` follows about never quietly skipping postgres."""
    run = _mise()["tasks"]["eval:gauntlet"]["run"]
    assert "set -eu" in run
    assert "exit 1" in run


def test_the_gauntlet_prints_the_report() -> None:
    """The point of the task is the scorecard, so the script that prints it
    is what the task ends on - not a file a reader has to go and find."""
    task = _mise()["tasks"]["eval:gauntlet"]
    assert "report" in task["description"].lower()


def test_the_plain_test_task_still_needs_no_docker() -> None:
    """Adding a docker-using task must not make the default suite need one:
    `mise run test` is the docker-free entry point and stays that way."""
    run = _mise()["tasks"]["test"]["run"]
    assert "docker" not in run


# -- compose:aivai / compose:aivai:down (DR7) --------------------------------
#
# The Opus-vs-Opus stack gets its own bring-up/down pair, mirroring the
# `compose`/`compose:down` postgres-password pattern rather than growing a
# flag on those tasks.


def test_the_aivai_compose_task_exists_and_says_what_it_does() -> None:
    task = _mise()["tasks"]["compose:aivai"]
    assert task["description"].strip()


def test_the_aivai_compose_task_targets_the_dedicated_compose_file() -> None:
    run = _mise()["tasks"]["compose:aivai"]["run"]
    assert "-f docker-compose.aivai.yml" in run
    assert "up --build" in run


def test_the_aivai_compose_task_generates_a_postgres_password_the_same_way() -> None:
    """Same `scripts/postgres-password.sh` source as `compose`'s own task -
    two independently-typed passwords would be a way for the aivai stack's
    postgres to silently diverge from the single-seat stack's."""
    run = _mise()["tasks"]["compose:aivai"]["run"]
    assert "./scripts/postgres-password.sh" in run
    assert "POSTGRES_PASSWORD" in run


def test_the_aivai_compose_down_task_targets_the_dedicated_compose_file() -> None:
    run = _mise()["tasks"]["compose:aivai:down"]["run"]
    assert "-f docker-compose.aivai.yml" in run
    assert "down" in run


def test_the_aivai_compose_down_task_forwards_its_own_arguments() -> None:
    """`mise run compose:aivai:down -- -v` has to reach `docker compose down`,
    or the guides/postgres volumes are unreachable to drop."""
    run = _mise()["tasks"]["compose:aivai:down"]["run"]
    assert '"$@"' in run


def test_the_aivai_compose_down_task_defaults_mtg_decks_like_the_single_seat_one() -> None:
    run = _mise()["tasks"]["compose:aivai:down"]["run"]
    assert 'MTG_DECKS="${MTG_DECKS:-/tmp}"' in run


# -- per-checkout compose isolation -------------------------------------------
#
# Every git worktree checkout of this repo shares the SAME basename
# (`magic-cards-model-api`), which is docker compose's own default project
# name - so without an explicit override, a worktree's `docker compose up`
# attaches to the main checkout's own project: same volume, same container,
# but scripts/postgres-password.sh reads a per-checkout password. See
# scripts/compose_env.py's own docstring for the corruption that caused for
# real. Every task that invokes `docker compose` must run that derivation and
# export its result before touching compose.

_COMPOSE_TASKS = [
    "test:pg",
    "eval:gauntlet",
    "compose",
    "compose:down",
    "compose:aivai",
    "compose:aivai:down",
]


def test_every_compose_invoking_task_derives_and_exports_the_project_name() -> None:
    mise = _mise()
    for task in _COMPOSE_TASKS:
        run = mise["tasks"][task]["run"]
        assert "scripts/compose_env.py" in run, task
        assert "export COMPOSE_PROJECT_NAME POSTGRES_HOST_PORT" in run, task


def test_postgres_bring_up_tasks_read_back_the_actual_bound_port() -> None:
    """`test:pg`/`eval:gauntlet` bring postgres up themselves and must never
    hardcode `:5432` in the connection string they build - POSTGRES_HOST_PORT
    is ephemeral (0) for a worktree, so the ACTUAL bound port has to be read
    back from compose."""
    mise = _mise()
    for task in ("test:pg", "eval:gauntlet"):
        run = mise["tasks"][task]["run"]
        assert "docker compose port postgres 5432" in run, task
        assert "localhost:5432" not in run, task
