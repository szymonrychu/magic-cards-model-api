"""The gauntlet: N seeded games, one command, one scorecard at the end.

    mise run eval:gauntlet -- --seeds 10
    mise run eval:gauntlet -- --seeds 20 --agent baseline
    mise run eval:gauntlet -- --seed 1 --seed 2 --decks a=decks/x.txt,b=decks/y.txt

Nothing here is new machinery. Every piece already existed and had a job; this
is the wiring that makes them one command a person will actually run before
claiming a change made the agent play better:

- **`magic_cards_model_api.serve`** boots the games. It already takes one
  `--match PLAYER=PATH,PLAYER=PATH,seed=N` per matchup and boots them all into
  one process, with each seed recorded so any game here is replayable by
  passing that seed back.
- **`baseline_seat.BaselineSeat`** plays one side - a deterministic, zero-LLM
  control (see that module for the policy and why it cannot stall).
- **The agent seat** plays the other, started through the api's own
  `POST /api/lobby/games/{id}/ai-seat/start` in whatever `AI_SEAT_MODE` says.
  `wrapper` is the default here: the driver holds a long-lived interactive
  claude, which is the shape a real match is played in.
- **`scripts/watch_game.py`** decides when a game is finished with. It is an
  edge trigger rather than a status poll - "the run stops on the *first*
  problem report, not when the players happen to finish" - and its four edges
  (`report`, `game_over`, `stalled`, `timeout`) are this harness's whole
  definition of done. The baseline loop is handed that decision through
  `play_out(should_stop=...)` rather than forming a second opinion.
- **`scripts/eval_report.py`** prints the scorecard off the `games` and
  `records` tables.

## Seat uids come from the api, not from the console

`serve` prints each game's seat uids, and parsing that block would work until
the day the block is reworded. `POST /api/games/{id}/seats` exists for exactly
this caller - it is the api's own "seat back door", added so "a test harness or
a shared mcp server can call this instead" of reading `store.seat_uids` out of
a process it is not in. So the games are booted by `serve` and the credentials
are minted over HTTP. `serve`'s console output is still captured to a log, so
the seeds and the uids it printed stay on the record.

## Why this needs a database

`serve` boots into whatever store its environment selects, and with nothing
configured that is `InMemoryRepository` - a ledger inside a process this script
is about to shut down, which the scorecard could never read. `mise run
eval:gauntlet` brings postgres up and exports `DATABASE_URL` before calling
this; run directly, this refuses rather than play ten games nobody can report
on.

## Throughput: this shares one process, and it is measured, not assumed

A 10-seed `--agent baseline` acceptance run was left for 30 minutes and hit
every game's own `--max-seconds` bound still mid-game, at turn 6-9 - not
stalled (the server log showed steady `pass-priority`/`legal-actions`
traffic the whole time, 0 refusals out of 3197 ruled-on attempts) and not a
policy defect (attacks and blocks were being declared correctly). The cause
is throughput: every game in one run shares one server process - one CPU's
worth of GIL, and on postgres one connection pool - and `GET .../legal-
actions` is not free. A standalone timing probe (one game, no concurrency,
`InMemoryRepository`, `tests/fixtures`-scale decks) measured legal-actions
latency at mean 61ms / p90 154ms / max 226ms once the board had a dozen-odd
permanents on it, and needed about 400 of `baseline_seat.play_out`'s own
half-round steps - about 90 seconds, alone, with nothing else competing for
the CPU - to reach turn 7. Ten of those sharing one process is the 30
minutes/turn-9 result; this is a known, deliberate cost in `Game.
computed_board`'s own design (it does not cache), not something this module
works around.

**The honest acceptance condition, given that measurement**: a bounded
baseline-vs-baseline game reaches a real engine terminal state (win, loss,
or draw) within its own `--max-seconds`/`--stall-seconds` bounds, and the
harness never hangs or silently miscounts a cut-off game as a loss (see
`eval_report.build_report`'s `edges`/`unfinished_detail`). It is **not**
"ten concurrent games finish in any particular wall-clock time" - that is a
throughput property of this machine and this process model, tunable with
`--serial` and `--max-seconds`, and a claim this module does not make.
`--seeds 10 --agent baseline` finishing unattended in a lunch break is not
proven on this machine at this concurrency; `--seeds 2` is, and is the
size this module is actually exercised at in its own test suite.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # `python scripts/eval_gauntlet.py` from anywhere
    sys.path.insert(0, str(REPO_ROOT))

from magic_cards_model_api import baseline_seat  # noqa: E402
from magic_cards_model_api.api_client import HttpSeatApiClient  # noqa: E402
from scripts import eval_report, watch_game  # noqa: E402

#: The two decklists a bare `--seeds N` plays. Real tournament lists out of
#: this repo's own `decks/`, and two different archetypes on purpose: a mirror
#: measures a policy against itself, which is the one matchup that cannot show
#: a difference between arms.
DEFAULT_DECKS = "agent=decks/boros_energy.txt,baseline=decks/dimir_frog.txt"

#: How long the baseline waits, in rounds of finding nothing to do, before it
#: concedes rather than sit. Far higher than `baseline_seat.STALL_ROUNDS`,
#: which is tuned for two baselines taking turns instantly: here the other
#: seat is an agent that legitimately thinks for a minute at a time, and a
#: guard that fired on that would concede games the agent was winning. At the
#: one-second poll below this is roughly twenty minutes of total silence.
AGENT_STALL_ROUNDS = 1200


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--seeds",
        type=int,
        default=10,
        help="how many seeded games to play; seeds are 1..N unless --seed is given",
    )
    parser.add_argument(
        "--seed",
        action="append",
        type=int,
        default=[],
        help="an explicit seed; repeatable, and overrides --seeds",
    )
    parser.add_argument(
        "--decks",
        default=DEFAULT_DECKS,
        help=f"PLAYER=PATH,PLAYER=PATH for every matchup; default {DEFAULT_DECKS}",
    )
    parser.add_argument(
        "--agent",
        choices=("wrapper", "baseline", "none"),
        default="wrapper",
        help=(
            "who plays the first seat: `wrapper` starts the api's own agent seat in "
            "wrapper mode, `baseline` puts a second greedy baseline there (a zero-LLM "
            "control run), `none` leaves it to whoever is already dispatched"
        ),
    )
    parser.add_argument(
        "--agent-seat",
        default=None,
        help="which player the agent takes; default is the first --decks entry",
    )
    parser.add_argument("--port", type=int, default=8123, help="port to boot the server on")
    parser.add_argument(
        "--max-seconds", type=float, default=1800.0, help="per-game bound before `timeout`"
    )
    parser.add_argument(
        "--stall-seconds",
        type=float,
        default=600.0,
        help="quiet log time before `watch_game` calls a game stalled",
    )
    parser.add_argument("--poll", type=float, default=2.0, help="watcher poll interval")
    parser.add_argument(
        "--serial",
        action="store_true",
        help=(
            "play one game to its edge before starting the next, instead of all of them "
            "at once. Every game shares one server process (one CPU's worth of GIL) and, "
            "on postgres, one connection pool - on a machine where that contention is the "
            "bottleneck, N games run one at a time can finish sooner than N run together"
        ),
    )
    parser.add_argument(
        "--database-url", default=None, help="where the ledger lands; default $DATABASE_URL"
    )
    parser.add_argument("--dump", default=None, help="write the ledger read to this file")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--server-log",
        default=None,
        help="where the booted server's console goes; default a temp file, printed on failure",
    )
    return parser.parse_args(argv)


def _deck_players(decks: str) -> list[str]:
    players = [token.split("=", 1)[0].strip() for token in decks.split(",") if "=" in token]
    if len(players) != 2:
        raise SystemExit(f"--decks needs exactly two PLAYER=PATH entries, got {decks!r}")
    return players


def boot_server(args: argparse.Namespace, seeds: list[int], database_url: str) -> tuple[Any, Path]:
    """Start `magic_cards_model_api.serve` with one `--match` per seed.

    One process for every game, which is what `serve` was built for ("several
    matchups playable at once ... a change to this module only, not to the
    store or the API") - and what keeps a ten-game run from needing ten ports
    and ten databases.
    """
    log_path = Path(args.server_log) if args.server_log else Path("eval-gauntlet-server.log")
    command = [sys.executable, "-m", "magic_cards_model_api.serve", "--port", str(args.port)]
    for seed in seeds:
        command += ["--match", f"{args.decks},seed={seed}"]
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        # The seat kind the games are played with. `subprocess` (the default)
        # is a one-shot `claude --print` per game, which is not the shape a
        # real match is played in.
        "AI_SEAT_MODE": os.environ.get("AI_SEAT_MODE", "wrapper"),
        # The child's stdout is a file, so it is block-buffered by default and
        # the console log runs a whole block behind the game. An operator
        # watching a two-hour gauntlet needs it live.
        "PYTHONUNBUFFERED": "1",
    }
    handle = log_path.open("w")
    process = subprocess.Popen(  # noqa: S603 - our own module, argv built here
        command, cwd=REPO_ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT
    )
    return process, log_path


def wait_for_health(api: str, process: Any, log_path: Path, *, bound: float = 120.0) -> None:
    deadline = time.monotonic() + bound
    with httpx.Client(base_url=api, timeout=5.0) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SystemExit(
                    f"eval:gauntlet: the server exited before serving (code {process.returncode}). "
                    f"Its console:\n{_tail(log_path)}"
                )
            try:
                if client.get("/api/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1.0)
    raise SystemExit(
        f"eval:gauntlet: the server did not answer /api/health within {bound:.0f}s. "
        f"Its console:\n{_tail(log_path)}"
    )


def _tail(path: Path, lines: int = 40) -> str:
    try:
        return "\n".join(path.read_text().splitlines()[-lines:])
    except OSError as exc:  # pragma: no cover - only when the log itself is unreadable
        return f"(could not read {path}: {exc})"


#: `serve`'s own summary block, the two lines this harness reads back out of
#: it: which game it booted, and the uid it handed each player.
_READY = re.compile(r"^game ([0-9a-f]{32}) ready:", re.MULTILINE)
_SEAT_UID = re.compile(r"^    (\S+): ([0-9a-f]{32})$", re.MULTILINE)


def booted_games(log_path: Path) -> list[tuple[str, dict[str, str]]]:
    """The games *this run* booted, and their seat uids, read off `serve`'s
    own console block.

    Deliberately not `GET /api/games`, and this is the bug that taught the
    lesson: with a database configured, that route lists every live game in
    it - including games an earlier gauntlet left behind - so a run that
    trusted it adopted two strangers, drove them with its own baselines, and
    reported a four-game scorecard for a two-game run. `serve` prints exactly
    what it created, with `flush=True`, before uvicorn accepts a single
    request, so this block is both complete and this run's alone.
    """
    text = log_path.read_text()
    blocks = _READY.split(text)
    # `split` on a capturing group gives [before, id, block, id, block, ...].
    found: list[tuple[str, dict[str, str]]] = []
    for index in range(1, len(blocks) - 1, 2):
        game_id, block = blocks[index], blocks[index + 1]
        found.append((game_id, dict(_SEAT_UID.findall(block))))
    return found


def mint_seat(api: str, game_id: str, player: str) -> str:
    """A seat credential for `player`, from the api's own back door."""
    with httpx.Client(base_url=api, timeout=30.0) as client:
        response = client.post(f"/api/games/{game_id}/seats", json={"player": player})
        response.raise_for_status()
        return str(response.json()["uid"])


def start_agent_seat(api: str, game_id: str, uid: str) -> dict[str, Any]:
    with httpx.Client(base_url=api, timeout=60.0) as client:
        response = client.post(
            f"/api/lobby/games/{game_id}/ai-seat/start", json={"uid": uid}, params={"uid": uid}
        )
        if response.status_code != 200:
            return {"state": "gone", "error": f"HTTP {response.status_code}: {response.text}"}
        return dict(response.json())


def stop_agent_seat(api: str, game_id: str, uid: str) -> None:
    try:
        with httpx.Client(base_url=api, timeout=30.0) as client:
            client.post(
                f"/api/lobby/games/{game_id}/ai-seat/stop", json={"uid": uid}, params={"uid": uid}
            )
    except httpx.HTTPError:
        pass


def archive_game(api: str, game_id: str, uid: str) -> None:
    """Soft-delete this run's own game once it is done being played.

    `GET /api/games` lists every *live* game in the database
    (`archived_at IS NULL`), and nothing before this ever archived a
    gauntlet's games - so postgres, which outlives the process that booted
    them, kept every past run's games live forever. An operator watching a
    10-seed run from outside saw 34 live games: four earlier runs' games
    this harness never cleaned up, plus the current one's ten. `booted_games`
    already keeps a run from *adopting* games it did not create (see its own
    docstring); this is the other half - cleaning up the ones it did.

    Uses the same `DELETE /api/games/{id}` back door `stop_agent_seat` uses
    for the ai-seat stop, which soft-deletes (`store.delete` stamps
    `archived_at` rather than dropping the row) - so `eval_report`'s own
    `ledger_from_database`, which reads the `games`/`records` tables
    directly and does not filter on `archived_at`, can still read this run's
    ledger after this call. Best-effort: a game already gone, or a server
    that is on its way down, is not a reason to fail a run whose scorecard
    has already been computed.
    """
    try:
        with httpx.Client(base_url=api, timeout=30.0) as client:
            client.delete(f"/api/games/{game_id}", params={"uid": uid})
    except httpx.HTTPError:
        pass


def play_one(
    api: str,
    game_id: str,
    seats: list[baseline_seat.BaselineSeat],
    args: argparse.Namespace,
    *,
    stall_rounds: int,
) -> dict[str, Any]:
    """One game: the baseline seat(s) in one thread, `watch_game` in another,
    and the watcher's first edge is what ends both."""
    done = threading.Event()
    edge: dict[str, Any] = {}
    played: dict[str, Any] = {}

    def watch() -> None:
        try:
            edge.update(
                watch_game.watch(api, game_id, 0, args.max_seconds, args.stall_seconds, args.poll)
            )
        except Exception as exc:  # noqa: BLE001 - a watcher that dies must not hang the run
            edge.update({"edge": "watcher_failed", "game": game_id, "error": str(exc)})
        finally:
            done.set()

    def drive() -> None:
        try:
            played.update(
                baseline_seat.play_out(
                    seats,
                    max_steps=100_000,
                    poll=1.0,
                    stall_rounds=stall_rounds,
                    should_stop=done.is_set,
                )
            )
        except Exception as exc:  # noqa: BLE001 - same, and the edge still reports the game
            played.update({"error": str(exc)})

    watcher = threading.Thread(target=watch, name=f"watch-{game_id}", daemon=True)
    driver = threading.Thread(target=drive, name=f"baseline-{game_id}", daemon=True)
    watcher.start()
    driver.start()
    watcher.join()
    driver.join(timeout=60.0)
    return {"game": game_id, "edge": edge, "baseline": played}


def run(args: argparse.Namespace) -> int:
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "eval:gauntlet: no DATABASE_URL. The scorecard reads the `games` and "
            "`records` tables, and a run booted with no database records into an "
            "in-process store that dies with the server - so this refuses rather "
            "than play games nobody can report on. Use `mise run eval:gauntlet`, "
            "which brings postgres up, or pass --database-url."
        )
    seeds = args.seed or list(range(1, args.seeds + 1))
    players = _deck_players(args.decks)
    agent_player = args.agent_seat or players[0]
    if agent_player not in players:
        raise SystemExit(f"--agent-seat {agent_player!r} is not one of {players}")
    baseline_players = [name for name in players if name != agent_player]
    if args.agent == "baseline":
        baseline_players = list(players)

    api = f"http://127.0.0.1:{args.port}"
    print(f"eval:gauntlet: booting {len(seeds)} game(s) on {api} - seeds {seeds}", flush=True)
    process, log_path = boot_server(args, seeds, database_url)
    started: list[tuple[str, str]] = []
    booted: list[tuple[str, str]] = []
    plans: list[tuple[str, list[baseline_seat.BaselineSeat]]] = []
    try:
        wait_for_health(api, process, log_path)
        games = booted_games(log_path)
        if len(games) != len(seeds):
            raise SystemExit(
                f"eval:gauntlet: asked for {len(seeds)} games and `serve` reported "
                f"{len(games)}. Its console:\n{_tail(log_path)}"
            )
        seam = HttpSeatApiClient(api)
        for game_id, printed_uids in games:
            uids = {
                name: printed_uids.get(name) or mint_seat(api, game_id, name) for name in players
            }
            # Any one seat's uid is enough to archive this game later
            # (`DELETE /api/games/{id}` accepts either seat's credential) -
            # this is not a per-seat action, just a way in the door.
            booted.append((game_id, next(iter(uids.values()))))
            if args.agent == "wrapper":
                status = start_agent_seat(api, game_id, uids[agent_player])
                started.append((game_id, uids[agent_player]))
                print(
                    f"  {game_id}: agent seat {agent_player} -> "
                    f"{status.get('mechanism')}/{status.get('state')}"
                    + (f" ({status['error']})" if status.get("error") else ""),
                    flush=True,
                )
            seats = [
                baseline_seat.BaselineSeat(seam, game_id, name, uids[name])
                for name in baseline_players
            ]
            plans.append((game_id, seats))

        stall_rounds = (
            baseline_seat.STALL_ROUNDS if args.agent == "baseline" else AGENT_STALL_ROUNDS
        )
        results: list[dict[str, Any]] = []
        threads: list[threading.Thread] = []
        collected: dict[str, dict[str, Any]] = {}

        def one(game_id: str, seats: list[baseline_seat.BaselineSeat]) -> None:
            collected[game_id] = play_one(api, game_id, seats, args, stall_rounds=stall_rounds)

        if args.serial:
            # One game to its edge before the next starts. Still through
            # `play_one` (its own watcher/driver pair), just never more than
            # one of those pairs live at a time - see `--serial`'s own help
            # for why a machine bottlenecked on the shared server/database
            # can finish sooner this way than with every game racing at once.
            for game_id, seats in plans:
                one(game_id, seats)
        else:
            for game_id, seats in plans:
                thread = threading.Thread(target=one, args=(game_id, seats), name=f"game-{game_id}")
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join()
        results = [collected[game_id] for game_id, _ in plans if game_id in collected]

        print("", flush=True)
        print("EDGES (scripts/watch_game.py - the first thing that ended each game)", flush=True)
        for result in results:
            print(f"  {json.dumps(result['edge'], sort_keys=True)}", flush=True)
        print("", flush=True)
    finally:
        for game_id, uid in started:
            stop_agent_seat(api, game_id, uid)
        # Archive every game this run booted, whether it finished, was cut
        # off, or never got its first move - before the server it lives in
        # goes away. See `archive_game`'s own docstring for the accumulation
        # this fixes; done here rather than only on the happy path, so a
        # run that raised out of the `try` block still cleans up after
        # itself.
        for game_id, uid in booted:
            archive_game(api, game_id, uid)
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - a server that will not stop
            process.kill()

    # This run's own games and nothing else. The database outlives a
    # gauntlet - it is the same postgres every other run and every manual
    # game landed in - so a report over the whole table would silently mix
    # ten seeded games with whatever else was ever played.
    ledger = eval_report.ledger_from_database(database_url, game_ids=[gid for gid, _ in plans])
    if args.dump:
        Path(args.dump).write_text(
            json.dumps([game.to_json() for game in ledger], indent=1, sort_keys=True)
        )
    # `watch_game`'s own edge for each game - `report`/`game_over`/`stalled`/
    # `timeout` - threaded into the report so an unfinished game says *why*
    # it is unfinished (cut off by the harness) rather than leaving a reader
    # to wonder whether "unfinished" means "still playing" or "lost".
    edges = {result["game"]: result["edge"] for result in results}
    report = eval_report.build_report(ledger, edges=edges)
    print(json.dumps(report, indent=1, sort_keys=True) if args.json else eval_report.render(report))
    print(f"\nserver console: {log_path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
