"""T9's concurrency acceptance test: two commands racing against one game's
committed state produce exactly one success and one conflict, and the loser
can re-read and re-apply against the winner's committed state.

Deliberately not part of `test_repository_contract.py` - that suite is the
protocol every `GameRepository` implementation satisfies identically,
including `InMemoryRepository`, whose `locked()` is a real mutex that makes
this race physically impossible to construct (the second caller blocks until
the first's whole `with` block, `replace()` included, has finished). What
this file pins is `PostgresRepository`-specific: two separate instances,
simulating two separate server processes each with their own process-local
`locked()` mutex and its own identity-preserving read cache, racing the same
row through the one thing that actually serialises them across processes -
`replace()`'s version CAS. See `repository_postgres.py`'s module docstring.

Skips (readably) under the same `MCM_TEST_DATABASE_URL` condition as the
contract suite, so the docker-free default `mise run test` run never touches
postgres.
"""

from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import text
from test_decks_api import LEGAL_DECK, LEGAL_DECK_B

from magic_cards_model_api.db import make_engine
from magic_cards_model_api.decks import load_deck_into_game
from magic_cards_model_api.repository_postgres import PostgresRepository, VersionConflict

_TEST_DATABASE_URL_ENV = "MCM_TEST_DATABASE_URL"


@pytest.fixture
def database_url() -> str:
    url = os.environ.get(_TEST_DATABASE_URL_ENV)
    if not url:
        pytest.skip(
            f"{_TEST_DATABASE_URL_ENV} is not set - postgres CAS tests are "
            "skipped (run `mise run test:pg` to exercise them)"
        )
    return url


@pytest.fixture
def two_repos(database_url: str) -> tuple[PostgresRepository, PostgresRepository]:
    """Two independent `PostgresRepository` instances against the same
    database, each with its own connection pool and its own process-local
    cache/lock state - the closest a single test process can get to two
    separate server replicas without actually running two servers."""
    engine = make_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE records, games RESTART IDENTITY CASCADE"))
    repo_a = PostgresRepository(database_url, engine=make_engine(database_url))
    repo_b = PostgresRepository(database_url, engine=make_engine(database_url))
    return repo_a, repo_b


def _real_game():
    match = load_deck_into_game([("you", LEGAL_DECK), ("bot", LEGAL_DECK_B)])
    return match.games[-1]


def test_replace_raises_version_conflict_when_a_different_writer_committed_first(
    two_repos: tuple[PostgresRepository, PostgresRepository],
) -> None:
    repo_a, repo_b = two_repos
    game_id = repo_a.create(_real_game())

    # Both "requests" read the same committed row (version 0) before either
    # writes - the shape of two seats' commands both starting from the state
    # the client fetched, not from each other's in-flight attempt.
    game_seen_by_a = repo_a.get(game_id)
    game_seen_by_b = repo_b.get(game_id)
    assert game_seen_by_a is not None
    assert game_seen_by_b is not None

    winner_game = game_seen_by_a.model_copy(deep=True)
    winner_game.players[0].life = 30
    repo_a.replace(game_id, winner_game)  # the one success

    loser_game = game_seen_by_b.model_copy(deep=True)
    loser_game.players[0].life = 1
    with pytest.raises(VersionConflict) as excinfo:
        repo_b.replace(game_id, loser_game)  # the one conflict - "409"

    conflict = excinfo.value
    assert conflict.game_id == game_id
    assert conflict.expected_version == 0
    assert conflict.actual_version == 1
    # The conflict carries the row as it now actually stands - the winner's
    # committed state, not the loser's attempted one.
    assert conflict.current_game.players[0].life == 30


def test_the_loser_can_reread_and_reapply(
    two_repos: tuple[PostgresRepository, PostgresRepository],
) -> None:
    repo_a, repo_b = two_repos
    game_id = repo_a.create(_real_game())

    game_seen_by_a = repo_a.get(game_id)
    game_seen_by_b = repo_b.get(game_id)
    assert game_seen_by_a is not None and game_seen_by_b is not None

    winner_game = game_seen_by_a.model_copy(deep=True)
    winner_game.players[0].life = 30
    repo_a.replace(game_id, winner_game)

    loser_game = game_seen_by_b.model_copy(deep=True)
    loser_game.players[0].life = 1
    with pytest.raises(VersionConflict):
        repo_b.replace(game_id, loser_game)

    # No server-side retry happened - re-reading and re-applying is on the
    # caller, exactly what an HTTP 409 tells a client to do.
    refreshed = repo_b.get(game_id)
    assert refreshed is not None
    assert refreshed.players[0].life == 30  # the winner's committed state

    reapplied = refreshed.model_copy(deep=True)
    reapplied.players[0].life = 25  # recomputed against the *current* state
    repo_b.replace(game_id, reapplied)  # now succeeds - version was 1, still current

    assert repo_a.get(game_id).players[0].life == 25


def test_two_threads_racing_replace_produce_exactly_one_success_and_one_conflict(
    two_repos: tuple[PostgresRepository, PostgresRepository],
) -> None:
    """The acceptance test's literal shape: two commands, genuinely
    concurrent (a `threading.Barrier` releases both at once), racing
    `replace()` against the same committed row. Exactly one succeeds; the
    other raises `VersionConflict`."""
    repo_a, repo_b = two_repos
    game_id = repo_a.create(_real_game())
    game_seen_by_a = repo_a.get(game_id)
    game_seen_by_b = repo_b.get(game_id)

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def attempt(repo: PostgresRepository, base, life: int) -> None:
        new_game = base.model_copy(deep=True)
        new_game.players[0].life = life
        barrier.wait(timeout=5)
        try:
            repo.replace(game_id, new_game)
        except VersionConflict:
            with outcomes_lock:
                outcomes.append("conflict")
        else:
            with outcomes_lock:
                outcomes.append("success")

    t1 = threading.Thread(target=attempt, args=(repo_a, game_seen_by_a, 30))
    t2 = threading.Thread(target=attempt, args=(repo_b, game_seen_by_b, 1))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert sorted(outcomes) == ["conflict", "success"]

    # Exactly one records row for this game - one committed transition, no
    # trace at all of the loser's rejected attempt.
    with repo_a._engine.connect() as conn:  # white-box: confirm the records write path
        rows = conn.execute(
            text("SELECT seq FROM records WHERE game_id = :id"), {"id": game_id}
        ).all()
    assert [row[0] for row in rows] == [1]
