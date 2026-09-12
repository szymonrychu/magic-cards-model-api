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


# ---------------------------------------------------------------------------
# the seat back door, and the credential the gate now requires
# ---------------------------------------------------------------------------


class _InProcess:
    """`httpx.Client`'s context-manager shape, over the in-process app.

    `mint_seat` builds its own client against a booted server's URL, which is
    right there and useless here - so what is swapped out is the client, not
    the function under test. Every call still reaches the real app, and so
    the real `game_access.GameAccessRoute` gate.
    """

    def __init__(self, client: object) -> None:
        self._client = client

    def __enter__(self) -> object:
        return self._client

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture()
def in_process(client, monkeypatch: pytest.MonkeyPatch):
    """`eval_gauntlet`'s `httpx`, answering in-process.

    Marked `no_harness_credential` at every call site below, and that marker
    is the whole point: `conftest._harness_credential` otherwise staples this
    process's admin key onto any mutating game-scoped request that carries
    none, so a test of "does this caller present a credential" would pass
    without the caller presenting anything.
    """
    import types

    import httpx

    monkeypatch.setattr(
        eval_gauntlet,
        "httpx",
        types.SimpleNamespace(
            Client=lambda **kwargs: _InProcess(client), HTTPError=httpx.HTTPError
        ),
    )
    return client


@pytest.mark.no_harness_credential
def test_minting_a_seat_presents_the_credential_the_gate_requires(in_process, game) -> None:
    """`POST /api/games/{id}/seats` is a mutating call on a `{game_id}` route,
    so `game_access.can_access_game` requires a credential *for that game* -
    a seat uid, the admin key, or the owner's bearer token. This caller
    presented none, so the back door this harness is documented to rely on
    answered 404 and `raise_for_status` aborted the whole run.
    `tests/test_mutation_needs_a_credential.py` pins that refusal
    deliberately; the refusal is right and the caller was wrong.

    Any one seat uid opens the game (`seat_for_uid`), and this harness holds
    them: `serve` prints them before uvicorn accepts a request.
    """
    from magic_cards_model_api.store import store

    alice_uid = next(uid for uid, name in store.seat_uids(game).items() if name == "alice")

    minted = eval_gauntlet.mint_seat("http://in-process", game, "bob", alice_uid)

    assert store.player_for_uid(game, minted) == "bob"
    assert minted != alice_uid


@pytest.mark.no_harness_credential
def test_a_printed_block_that_lost_a_uid_is_minted_with_one_this_run_holds(
    in_process, game
) -> None:
    """The fallback exists because `_SEAT_UID` reads a console block and a
    reworded block stops matching - so it has to work when it is reached.
    Any one seat uid opens the game for the gate, and one is what `serve`
    printed."""
    from magic_cards_model_api.store import store

    alice_uid = next(uid for uid, name in store.seat_uids(game).items() if name == "alice")

    uids = eval_gauntlet.resolve_uids(
        "http://in-process", game, ["alice", "bob"], {"alice": alice_uid}
    )

    assert uids["alice"] == alice_uid
    assert store.player_for_uid(game, uids["bob"]) == "bob"


@pytest.mark.no_harness_credential
def test_a_printed_block_with_no_uid_at_all_is_a_loud_refusal(in_process, game) -> None:
    """With nothing off the console there is no credential to present, and
    `POST .../seats` is a mutating call on a `{game_id}` route. Said here,
    once, rather than discovered as a 404 out of `raise_for_status` halfway
    through a ten-game boot."""
    with pytest.raises(SystemExit) as refused:
        eval_gauntlet.resolve_uids("http://in-process", game, ["alice", "bob"], {})

    assert "seat uid" in str(refused.value)
    assert game in str(refused.value)


# ---------------------------------------------------------------------------
# one game at a time actually means one game at a time
# ---------------------------------------------------------------------------


def _edge(game_id: str) -> dict[str, str]:
    return {"edge": "game_over", "game": game_id}


def test_play_one_waits_for_its_driver_and_says_it_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watcher's edge is what ends the game, and the driver stops on the
    same signal (`play_out(should_stop=...)`). `play_one` reports whether it
    actually did, because everything the `finally` block does next -
    `archive_game`, `process.terminate()` - races a driver that is still
    issuing calls."""
    import threading
    import time

    monkeypatch.setattr(eval_gauntlet.watch_game, "watch", lambda *a, **k: _edge("g"))
    stopped = threading.Event()

    def play_out(seats, **kwargs):
        while not kwargs["should_stop"]():
            time.sleep(0.01)
        stopped.set()
        return {"ended_by": "stopped"}

    monkeypatch.setattr(eval_gauntlet.baseline_seat, "play_out", play_out)

    result = eval_gauntlet.play_one(
        "http://in-process", "g", [], eval_gauntlet.parse_args([]), stall_rounds=3
    )

    assert stopped.is_set(), "the driver was left running"
    assert result["driver"] == "stopped", result
    assert result["baseline"] == {"ended_by": "stopped"}, result


def test_play_one_reports_a_driver_it_could_not_stop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`driver.join(timeout=60.0)` gave up after a minute and said nothing.
    The thread is a daemon and kept driving the game, so in `--serial` the
    next game started alongside it and the `finally` block's cleanup ran
    while those baselines were still issuing calls - cleanup racing a driver
    that was abandoned rather than stopped, and per-game timings this harness
    exists to produce that no longer mean anything. Abandonment is now
    explicit, named, and reported."""
    import threading

    monkeypatch.setattr(eval_gauntlet.watch_game, "watch", lambda *a, **k: _edge("g"))
    release = threading.Event()

    def play_out(seats, **kwargs):
        release.wait(30.0)
        return {"ended_by": "stopped"}

    monkeypatch.setattr(eval_gauntlet.baseline_seat, "play_out", play_out)

    try:
        result = eval_gauntlet.play_one(
            "http://in-process",
            "g",
            [],
            eval_gauntlet.parse_args([]),
            stall_rounds=3,
            driver_grace=0.2,
        )
    finally:
        release.set()

    assert result["driver"] == "abandoned", result
    assert "abandoned" in capsys.readouterr().err


def test_serial_does_not_start_a_game_alongside_an_abandoned_driver() -> None:
    """ "Play one game to its edge before starting the next" is the whole
    claim `--serial` makes, and a driver that outlived its join breaks it.
    The run stops there and names the games it did not play, rather than
    quietly overlapping two of them."""
    played: list[str] = []

    def one(game_id: str, seats: list[object]) -> dict[str, str]:
        played.append(game_id)
        return {"driver": "abandoned" if game_id == "b" else "stopped"}

    unplayed = eval_gauntlet.play_serially([("a", []), ("b", []), ("c", [])], one)

    assert played == ["a", "b"]
    assert unplayed == ["c"]


def test_an_abandoned_driver_fails_the_run() -> None:
    """A run whose per-game timings are not independent has not produced the
    thing it exists to produce, so it does not exit 0 and leave that to be
    noticed in the scrollback."""
    stopped = [{"game": "a", "driver": "stopped"}, {"game": "b", "driver": "stopped"}]

    assert eval_gauntlet.exit_code(stopped) == 0
    assert eval_gauntlet.exit_code([*stopped, {"game": "c", "driver": "abandoned"}]) == 1
