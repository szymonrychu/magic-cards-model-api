"""The gauntlet driver: `scripts/eval_gauntlet.py`.

The run itself needs a booted server, ten games and an hour, so what is tested
here is everything around it that can be wrong *silently* - which games a run
believes it booted, and whether it refuses to play games nobody could report
on. Both of those were live defects, not hypotheticals.
"""

from __future__ import annotations

import socket
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import select

from magic_cards_model_api.db import games, make_engine
from scripts import eval_gauntlet

SERVE_CONSOLE = textwrap.dedent(
    """\
    booted 2 games:

    --- game 1/2 ---
    game 635713ddc45a4ecf9913ba0761dbb502 ready: agent vs baseline
      agent: 75 cards (60 mainboard, 15 sideboard) from decks/boros_energy.txt
      baseline: 75 cards (60 mainboard, 15 sideboard) from decks/dimir_frog.txt
      seed: 1
      opening hands (as dealt; CR 103.5 is the seats' own to declare): agent 7, baseline 7
      first player: agent (turn 0, mulligan step)
      seat uids (one per player, given out at dispatch):
        agent: e2777c44449b4e71b360ac9c780d6eb5
        baseline: 5f25f0a644a5423e8878136cde3b291f

    --- game 2/2 ---
    game de413e4540a74b13a02eb29cf194db03 ready: agent vs baseline
      agent: 75 cards (60 mainboard, 15 sideboard) from decks/boros_energy.txt
      baseline: 75 cards (60 mainboard, 15 sideboard) from decks/dimir_frog.txt
      seed: 2
      opening hands (as dealt; CR 103.5 is the seats' own to declare): agent 7, baseline 7
      first player: agent (turn 0, mulligan step)
      seat uids (one per player, given out at dispatch):
        agent: 43a0dba654d54295897532a6a1aa3dfe
        baseline: 7681a1b0265745bca6f5f76226307018
    """
)


def test_it_reads_its_own_games_out_of_the_servers_summary(tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    log.write_text(SERVE_CONSOLE)

    booted = eval_gauntlet.booted_games(log)

    assert [game_id for game_id, _ in booted] == [
        "635713ddc45a4ecf9913ba0761dbb502",
        "de413e4540a74b13a02eb29cf194db03",
    ]
    assert booted[0][1] == {
        "agent": "e2777c44449b4e71b360ac9c780d6eb5",
        "baseline": "5f25f0a644a5423e8878136cde3b291f",
    }


def test_it_does_not_adopt_games_it_did_not_boot(tmp_path: Path) -> None:
    """The live defect this exists for. `GET /api/games` lists every live game
    in the database, so with postgres configured a run that trusted it picked
    up two games an earlier gauntlet had left behind, drove them with its own
    baselines, and printed a four-game scorecard for a two-game run. The
    server's own summary names exactly what this run created.
    """
    log = tmp_path / "server.log"
    log.write_text(SERVE_CONSOLE)

    booted = eval_gauntlet.booted_games(log)

    assert len(booted) == 2


def test_a_console_with_no_games_yields_none(tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    log.write_text("Traceback (most recent call last):\n  serve: cannot read decklist\n")

    assert eval_gauntlet.booted_games(log) == []


def test_it_refuses_to_run_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run booted with no database records into an in-process store that
    dies with the server, so the scorecard would have nothing to read. Ten
    games played and nothing recorded is the worst possible outcome here -
    worse than not running - so it is refused up front."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    args = eval_gauntlet.parse_args(["--seeds", "2"])

    with pytest.raises(SystemExit) as refused:
        eval_gauntlet.run(args)

    assert "DATABASE_URL" in str(refused.value)


def test_the_default_matchup_is_two_real_decklists_from_this_repo() -> None:
    """A mirror measures a policy against itself, which is the one matchup
    that cannot show a difference between arms - and a decklist that does not
    exist fails an hour into a run rather than at the top of it."""
    players = eval_gauntlet._deck_players(eval_gauntlet.DEFAULT_DECKS)
    assert len(set(players)) == 2

    root = Path(__file__).resolve().parents[1]
    paths = [token.split("=", 1)[1] for token in eval_gauntlet.DEFAULT_DECKS.split(",")]
    assert len({Path(path).name for path in paths}) == 2
    for path in paths:
        assert (root / path).is_file(), path


def test_the_agent_seat_is_the_one_the_operator_named() -> None:
    assert eval_gauntlet.parse_args([]).agent == "wrapper"
    assert eval_gauntlet.parse_args(["--agent", "baseline"]).agent == "baseline"
    assert eval_gauntlet.parse_args(["--seed", "3", "--seed", "9"]).seed == [3, 9]


def test_serial_defaults_off_but_can_be_asked_for() -> None:
    """Concurrent is the default (every game shares one server process, one
    CPU's worth of GIL, and - on postgres - one connection pool) -
    `--serial` is the escape hatch for a machine where that contention makes
    a run slower than playing the same games one at a time. See this
    module's own docstring for the measurement that motivated it."""
    assert eval_gauntlet.parse_args([]).serial is False
    assert eval_gauntlet.parse_args(["--serial"]).serial is True


def test_the_baseline_gets_a_patient_stall_guard_against_an_agent() -> None:
    """`baseline_seat.STALL_ROUNDS` is tuned for two baselines taking turns
    instantly. An agent seat thinking for a minute is not a stalled game, and
    a guard that fired on that would concede games the agent was winning."""
    from magic_cards_model_api import baseline_seat

    assert eval_gauntlet.AGENT_STALL_ROUNDS > baseline_seat.STALL_ROUNDS * 100


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _live_game_ids(database_url: str, game_ids: list[str]) -> list[str]:
    """Which of `game_ids` are still live (`archived_at IS NULL`) - the same
    predicate `GET /api/games` filters on (`repository_postgres.ids`)."""
    engine = make_engine(database_url)
    try:
        with engine.connect() as conn:
            query = select(games.c.id).where(
                games.c.id.in_(game_ids), games.c.archived_at.is_(None)
            )
            return [str(row[0]) for row in conn.execute(query).all()]
    finally:
        engine.dispose()


def test_a_run_archives_the_games_it_booted_so_they_do_not_accumulate(
    tmp_path: Path,
) -> None:
    """The live defect this fixes: nothing ever archived a gauntlet's games,
    and postgres outlives the process that booted them - so every past run's
    games stayed live in `GET /api/games` forever. An operator watching a
    10-seed acceptance run from outside saw 34 live games: four earlier
    runs' games this harness never cleaned up, plus the current one's. A run
    must leave the database no fuller than it found it, whether or not its
    own games finished playing - this run's own `--max-seconds` is set low
    enough that neither game finishes, on purpose.
    """
    from conftest import require_postgres_database_url

    database_url = require_postgres_database_url("MCM_TEST_DATABASE_URL")
    log_path = tmp_path / "server.log"
    args = eval_gauntlet.parse_args(
        [
            "--seeds",
            "2",
            "--agent",
            "baseline",
            "--port",
            str(_free_port()),
            "--database-url",
            database_url,
            "--max-seconds",
            "6",
            "--stall-seconds",
            "3",
            "--poll",
            "0.3",
            "--server-log",
            str(log_path),
        ]
    )

    eval_gauntlet.run(args)

    game_ids = [game_id for game_id, _ in eval_gauntlet.booted_games(log_path)]
    assert len(game_ids) == 2, _tail_for_failure(log_path)
    assert _live_game_ids(database_url, game_ids) == []


def _tail_for_failure(log_path: Path) -> str:
    return "\n".join(log_path.read_text().splitlines()[-40:])
