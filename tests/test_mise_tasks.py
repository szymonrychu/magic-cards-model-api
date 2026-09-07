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
