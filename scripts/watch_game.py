"""Watch one live game and return the moment it needs the operator's attention.

The autonomous playtest loop needs an edge trigger, not a status poll: the run
stops on the *first* problem report, not when the players happen to finish. This
blocks until exactly one of those edges fires and prints it as JSON:

    {"edge": "report", "reports": [...]}   a player filed a problem report
    {"edge": "game_over", "winner": ...}   the game ended
    {"edge": "stalled", ...}               no log entry for --stall-seconds
    {"edge": "timeout"}                    --max-seconds elapsed, game still live
    {"edge": "game_gone", "status": ..., "body": ...}     the game 404s mid-watch
    {"edge": "watcher_error", "status": ..., "body": ...} any other surprise

Usage:

    uv run python scripts/watch_game.py [--since N] [--max-seconds 1800]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx


def latest_game(client: httpx.Client) -> str:
    games = client.get("/api/games").json()["games"]
    if not games:
        raise SystemExit("watch_game: no game on the server")
    return str(games[-1]["game_id"])


class WatchFailure(Exception):
    """A response `watch` did not get to interpret as game state.

    Carries the real HTTP status and body rather than letting the caller
    index into whatever came back: that indexing is exactly what used to
    raise a bare `KeyError: 'log'` the one time a watched game's row
    disappeared from postgres mid-run (a sibling test run truncated the
    shared `games` table) and every 404 that followed turned into an
    unreadable `watcher_failed {"error": "'log'"}` instead of "the game is
    gone".

    Two edges come out of this, not one, because they mean different
    things to an operator: `game_gone` (a 404) is a specific, well
    understood cause - the game itself no longer exists on the server, so
    there is nothing left to poll and no point retrying. `watcher_error` is
    everything else this watcher did not plan for - a non-2xx that is not a
    404, or a 2xx body missing the key it was fetched for - and it is the
    general safety net: whatever new way a server response can surprise this
    loop, it becomes a named, JSON-serialisable outcome, never a second
    flavour of unhandled exception.

    `game_gone` is deliberately its own edge and not folded into `stalled`
    or `timeout`: both of those mean the game is still there and merely
    quiet or has run long - each keeps `seq`, and a caller may reasonably
    resume watching from where it left off. A vanished game cannot be
    resumed at all, which is a different fact worth a different name.
    """

    def __init__(self, edge: str, status: int, body: Any) -> None:
        super().__init__(f"{edge}: HTTP {status}")
        self.edge = edge
        self.status = status
        self.body = body


def _get_json(client: httpx.Client, url: str, *, key: str | None = None) -> Any:
    """GET `url` and return its body (or `body[key]` when `key` is given),
    or raise `WatchFailure` naming exactly what was wrong - never a bare
    `KeyError`/`TypeError` from indexing an unexpected shape."""
    response = client.get(url)
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    if response.status_code == 404:
        raise WatchFailure("game_gone", response.status_code, body)
    if (
        response.status_code != 200
        or not isinstance(body, dict)
        or (key is not None and key not in body)
    ):
        raise WatchFailure("watcher_error", response.status_code, body)
    return body[key] if key is not None else body


def watch(
    api: str,
    game: str | None,
    since: int,
    max_seconds: float,
    stall_seconds: float,
    poll: float,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    with httpx.Client(base_url=api, timeout=30.0, transport=transport) as client:
        gid = game or latest_game(client)
        deadline = time.monotonic() + max_seconds
        last_len, last_change = since, time.monotonic()
        while True:
            try:
                entries = _get_json(client, f"/api/games/{gid}/log", key="log")
                fresh = entries[since:]
                reports = [e for e in fresh if e["action"] == "report_problem"]
                if reports:
                    return {"edge": "report", "game": gid, "seq": len(entries), "reports": reports}
                state = _get_json(client, f"/api/games/{gid}")
            except WatchFailure as exc:
                return {"edge": exc.edge, "game": gid, "status": exc.status, "body": exc.body}
            if state.get("winner") or state.get("drawn"):
                return {
                    "edge": "game_over",
                    "game": gid,
                    "seq": len(entries),
                    "winner": state.get("winner"),
                    "drawn": state.get("drawn"),
                    "turn": state.get("turn"),
                }
            now = time.monotonic()
            if len(entries) != last_len:
                last_len, last_change = len(entries), now
            elif now - last_change >= stall_seconds:
                return {
                    "edge": "stalled",
                    "game": gid,
                    "seq": len(entries),
                    "quiet_seconds": round(now - last_change),
                    "tail": [e["action"] for e in entries[-5:]],
                    "turn": state.get("turn"),
                    "step": state.get("step"),
                    "priority_holder": state.get("priority_holder"),
                }
            if now >= deadline:
                return {"edge": "timeout", "game": gid, "seq": len(entries)}
            time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--game", default=None)
    parser.add_argument("--since", type=int, default=0, help="ignore log entries before this index")
    parser.add_argument("--max-seconds", type=float, default=1800.0)
    parser.add_argument("--stall-seconds", type=float, default=600.0)
    parser.add_argument("--poll", type=float, default=3.0)
    args = parser.parse_args(argv)
    result = watch(args.api, args.game, args.since, args.max_seconds, args.stall_seconds, args.poll)
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
