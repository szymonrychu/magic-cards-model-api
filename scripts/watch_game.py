"""Watch one live game and return the moment it needs the operator's attention.

The autonomous playtest loop needs an edge trigger, not a status poll: the run
stops on the *first* problem report, not when the players happen to finish. This
blocks until exactly one of those edges fires and prints it as JSON:

    {"edge": "report", "reports": [...]}   a player filed a problem report
    {"edge": "game_over", "winner": ...}   the game ended
    {"edge": "stalled", ...}               no log entry for --stall-seconds
    {"edge": "timeout"}                    --max-seconds elapsed, game still live

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


def watch(
    api: str, game: str | None, since: int, max_seconds: float, stall_seconds: float, poll: float
) -> dict[str, Any]:
    with httpx.Client(base_url=api, timeout=30.0) as client:
        gid = game or latest_game(client)
        deadline = time.monotonic() + max_seconds
        last_len, last_change = since, time.monotonic()
        while True:
            entries = client.get(f"/api/games/{gid}/log").json()["log"]
            fresh = entries[since:]
            reports = [e for e in fresh if e["action"] == "report_problem"]
            if reports:
                return {"edge": "report", "game": gid, "seq": len(entries), "reports": reports}
            state = client.get(f"/api/games/{gid}").json()
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
