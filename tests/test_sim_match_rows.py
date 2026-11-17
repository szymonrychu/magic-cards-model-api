"""The persisted Claude-seat match header (`sim_claude_matches`): the
`GameRepository` contract for `put/get/list/fail_live/delete_before`,
parameterised over `InMemoryRepository` and a postgres factory (the same
`_REPOSITORY_FACTORIES` pattern as `test_user_preferences.py`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import require_postgres_database_url
from sqlalchemy import text

from magic_cards_model_api.db import make_engine
from magic_cards_model_api.repository import (
    SIM_MATCH_LIVE_STATUSES,
    GameRepository,
    InMemoryRepository,
)
from magic_cards_model_api.repository_postgres import PostgresRepository

_TEST_DATABASE_URL_ENV = "MCM_TEST_DATABASE_URL"
_pg_engine = None

_COLUMNS = {
    "match_id",
    "game_id",
    "decks",
    "seed",
    "seats",
    "status",
    "error",
    "result",
    "started_at",
    "finished_at",
    "last_seq",
    "last_progress_at",
    "stalled_reason",
}


def _postgres_repository_factory() -> PostgresRepository:
    global _pg_engine
    url = require_postgres_database_url(_TEST_DATABASE_URL_ENV)
    if _pg_engine is None:
        _pg_engine = make_engine(url)
    with _pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE sim_claude_matches"))
    return PostgresRepository(url, engine=_pg_engine)


_REPOSITORY_FACTORIES = [InMemoryRepository, _postgres_repository_factory]


@pytest.fixture(params=_REPOSITORY_FACTORIES, ids=lambda factory: factory.__name__)
def repo(request: pytest.FixtureRequest) -> GameRepository:
    return request.param()


_NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)


def _row(
    match_id: str,
    status: str,
    started: datetime,
    finished: datetime | None = None,
    **kw: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "match_id": match_id,
        "game_id": None,
        "decks": {"a": ["Island"], "b": ["Mountain"]},
        "seed": 7,
        "seats": {
            "a": {"model": "sonnet", "effort": "low"},
            "b": {"model": "opus", "effort": "high"},
        },
        "status": status,
        "error": None,
        "result": None,
        "started_at": started,
        "finished_at": finished,
        "last_seq": None,
        "last_progress_at": None,
        "stalled_reason": None,
    }
    row.update(kw)
    return row


def test_live_statuses_are_starting_running_stalled() -> None:
    assert SIM_MATCH_LIVE_STATUSES == ("starting", "running", "stalled")


def test_get_is_none_for_an_unknown_match(repo: GameRepository) -> None:
    assert repo.get_sim_claude_match("nope") is None


def test_put_then_get_round_trips_every_column(repo: GameRepository) -> None:
    row = _row(
        "m1",
        "done",
        _NOW,
        _NOW + timedelta(minutes=5),
        game_id="g-1",
        seed=2**40,
        error="boom",
        result={"winner": "a", "turns": 9},
        last_seq=42,
        last_progress_at=_NOW + timedelta(minutes=4),
        stalled_reason="no record for 900 s",
    )
    assert set(row) == _COLUMNS
    repo.put_sim_claude_match(row)
    got = repo.get_sim_claude_match("m1")
    assert got == row
    assert got is not None and got["started_at"].tzinfo is not None


def test_put_again_updates_in_place(repo: GameRepository) -> None:
    repo.put_sim_claude_match(_row("m1", "running", _NOW))
    repo.put_sim_claude_match(_row("m1", "done", _NOW, _NOW + timedelta(minutes=1)))
    got = repo.get_sim_claude_match("m1")
    assert got is not None and got["status"] == "done"
    assert [r["match_id"] for r in repo.list_sim_claude_matches(10)] == ["m1"]


def test_list_is_newest_first_and_limited(repo: GameRepository) -> None:
    for i, name in enumerate(["old", "mid", "new"]):
        repo.put_sim_claude_match(_row(name, "running", _NOW + timedelta(minutes=i)))
    assert [r["match_id"] for r in repo.list_sim_claude_matches(2)] == ["new", "mid"]


def test_returned_rows_are_copies(repo: GameRepository) -> None:
    repo.put_sim_claude_match(_row("m1", "running", _NOW))
    first = repo.get_sim_claude_match("m1")
    assert first is not None
    first["status"] = "failed"
    first["decks"]["a"].append("Swamp")
    again = repo.get_sim_claude_match("m1")
    assert again is not None
    assert again["status"] == "running"
    assert again["decks"]["a"] == ["Island"]


def test_fail_live_marks_starting_running_and_stalled_failed(repo: GameRepository) -> None:
    repo.put_sim_claude_match(_row("s", "starting", _NOW))
    repo.put_sim_claude_match(_row("r", "running", _NOW))
    repo.put_sim_claude_match(_row("t", "stalled", _NOW, stalled_reason="quiet"))
    repo.put_sim_claude_match(_row("d", "done", _NOW, _NOW + timedelta(minutes=1)))
    repo.put_sim_claude_match(_row("k", "running", _NOW))
    stamp = _NOW + timedelta(hours=1)
    failed = repo.fail_live_sim_claude_matches("restarted", stamp, keep={"k"})
    assert sorted(failed) == ["r", "s", "t"]
    for match_id in failed:
        got = repo.get_sim_claude_match(match_id)
        assert got is not None
        assert got["status"] == "failed"
        assert got["error"] == "restarted"
        assert got["finished_at"] == stamp
        assert got["stalled_reason"] is None
    done = repo.get_sim_claude_match("d")
    assert done is not None and done["status"] == "done" and done["error"] is None
    kept = repo.get_sim_claude_match("k")
    assert kept is not None and kept["status"] == "running" and kept["finished_at"] is None


def test_delete_before_drops_only_old_finished_rows(repo: GameRepository) -> None:
    repo.put_sim_claude_match(
        _row("old", "done", _NOW - timedelta(days=41), _NOW - timedelta(days=40))
    )
    repo.put_sim_claude_match(
        _row("fresh", "done", _NOW - timedelta(days=2), _NOW - timedelta(days=1))
    )
    repo.put_sim_claude_match(_row("live", "running", _NOW - timedelta(days=50)))
    assert repo.delete_sim_claude_matches_before(_NOW - timedelta(days=30)) == 1
    assert repo.get_sim_claude_match("old") is None
    assert repo.get_sim_claude_match("fresh") is not None
    assert repo.get_sim_claude_match("live") is not None
