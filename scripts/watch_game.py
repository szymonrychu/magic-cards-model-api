"""Watch one live game and return the moment it needs the operator's attention.

The autonomous playtest/DSL loop needs an edge trigger, not a status poll: the
run stops on the *first engine problem report*, not on every problem report -
a `dsl` or `ux` one is development signal for the DSL/UX loop (missing moves,
clunky MCP call sequences), never a reason to interrupt a live game. This
blocks until exactly one of six edges fires and prints it as JSON:

    {"edge": "report", "reports": [...], "findings": [...]}
        a player filed an ENGINE problem report - a `report_problem` row
        whose `detail.category` is `"engine"`, or a legacy row written
        before `category` existed at all, which counts as `engine` too
        (`audit.problem_category`'s own fallback). This is the only
        category that stops the watch.
    {"edge": "game_over", "winner": ..., "findings": [...],
     "play_stats": {...} | null, "play_stats_reason": null | "..."}
        the game ended (CR 104.1). `play_stats` is this game's per-seat
        play statistics from `GET .../review` (DR2) when `--review-key`
        (an admin key or a seat uid) made that reachable; otherwise it is
        `null` and `play_stats_reason` says why.
    {"edge": "stalled", ..., "findings": [...]}   no log entry for --stall-seconds
    {"edge": "timeout", "findings": [...]}        --max-seconds elapsed, game still live
    {"edge": "game_gone", "status": ..., "body": ...}     the game 404s mid-watch
    {"edge": "watcher_error", "status": ..., "body": ...} any other surprise

`findings` is every `dsl`/`ux` `report_problem` row seen since `--since`, as
`{"seq", "player", "category", "text"}` - collected, never a stop condition,
and carried on whichever of `report`/`game_over`/`stalled`/`timeout` ends the
watch. `game_gone`/`watcher_error` carry no `findings`: both abort before (or
while) reading the log, so there is nothing yet collected to hand back.

Usage:

    uv run python scripts/watch_game.py [--since N] [--max-seconds 1800] [--review-key KEY]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx


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


def _get_json(
    client: httpx.Client,
    url: str,
    *,
    key: str | None = None,
    expect: type | tuple[type, ...] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET `url` and return its body (or `body[key]` when `key` is given),
    or raise `WatchFailure` naming exactly what was wrong - never a bare
    `KeyError`/`TypeError` from indexing an unexpected shape.

    `expect` is the type the extracted value has to be, and it is the other
    half of that promise: validating only that the key is PRESENT left
    `entries[since:]` to raise the `TypeError` this docstring says cannot
    happen. Pass it wherever the caller is about to index or iterate what
    comes back.

    `params` is query-string arguments - only `_review_play_stats` uses it,
    to carry `--review-key` (an admin key or a seat uid) onto `GET .../review`
    without every other caller having to pass `params=None`.
    """
    response = client.get(url, params=params or {})
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
    value = body[key] if key is not None else body
    if expect is not None and not isinstance(value, expect):
        raise WatchFailure("watcher_error", response.status_code, body)
    return value


def latest_game(client: httpx.Client) -> str:
    """The newest game on the server - the `--game` unset default the module
    docstring's own usage line shows.

    Through `_get_json` like every other fetch, and called inside the
    caller's `try`: this used to index `.json()["games"]` directly, from
    outside it, so the default CLI path answered a vanished game with
    `KeyError: 'games'` - the bare indexing `WatchFailure` exists to prevent,
    on the very case `game_gone` was added for.
    """
    games = _get_json(client, "/api/games", key="games", expect=list)
    if not games:
        raise SystemExit("watch_game: no game on the server")
    newest = games[-1]
    if not isinstance(newest, dict) or "game_id" not in newest:
        raise WatchFailure("watcher_error", 200, {"games": games})
    return str(newest["game_id"])


def _log_entries(client: httpx.Client, game_id: str) -> list[dict[str, Any]]:
    """This game's ledger, validated far enough that reading `action` off a
    row cannot raise. A row that is not a dict at all is a shape this watcher
    cannot interpret, so it becomes `watcher_error` carrying the real body
    rather than a `KeyError` the gauntlet's blanket except would report as
    `watcher_failed {"error": "'action'"}`. A dict row with no `action` is
    NOT an error: it is simply not a problem report, and has no verb to show
    in a `stalled` tail."""
    entries = _get_json(client, f"/api/games/{game_id}/log", key="log", expect=list)
    if not all(isinstance(entry, dict) for entry in entries):
        raise WatchFailure("watcher_error", 200, {"log": entries})
    return entries


#: The only category that stops the watch - `audit.problem_category`'s own
#: fallback for a `report_problem` row with no `detail.category` key at all
#: (a row written before DR2 added the field). Mirrored here, not imported:
#: this script runs against a bare server URL, never the app's own modules.
_ENGINE_CATEGORY = "engine"


def _report_category(entry: dict[str, Any]) -> str:
    """A `report_problem` row's own category - `"engine"` for an explicit
    `engine` row, for a legacy row with no `category` key, or for any row
    whose `detail` is not even a mapping (the pre-DR2 fake shape this
    module's own regression tests still use); `detail["category"]` verbatim
    otherwise. Never raises on a malformed row - the same promise `_get_json`
    makes for a malformed response."""
    detail = entry.get("detail")
    if isinstance(detail, dict):
        category = detail.get("category")
        if isinstance(category, str) and category:
            return category
    return _ENGINE_CATEGORY


def _split_reports(
    fresh: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`fresh`'s `report_problem` rows, split into the ones that stop the
    watch (`engine`, `reports`) and the ones that only get collected
    (`dsl`/`ux`, `findings`) - see the module docstring's edge table."""
    reports: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for entry in fresh:
        if entry.get("action") != "report_problem":
            continue
        category = _report_category(entry)
        if category == _ENGINE_CATEGORY:
            reports.append(entry)
            continue
        detail = entry.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        findings.append(
            {
                "seq": entry.get("seq"),
                "player": detail.get("player"),
                "category": category,
                "text": detail.get("text"),
            }
        )
    return reports, findings


def _review_play_stats(
    client: httpx.Client, game_id: str, review_key: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    """The `game_over` edge's per-seat `play_stats`, read from `GET
    .../review` (DR2) now that the watch already knows the game is over -
    or `(None, reason)` when that read cannot be made. `GET .../review`
    requires an admin key or a seat uid (`app.game_review`'s own docstring);
    with none given, or with the review 40x-ing (the game vanished, or this
    key is not one of its credentials), a `game_over` result must still come
    back clean, never crash the watch that already has its real answer."""
    if not review_key:
        return None, "no --review-key given, so GET /review was never attempted"
    try:
        stats = _get_json(
            client,
            f"/api/games/{game_id}/review",
            key="play_stats",
            expect=dict,
            params={"key": review_key},
        )
    except WatchFailure as exc:
        return None, f"GET /review returned {exc.edge} (HTTP {exc.status}): {exc.body!r}"
    return stats, None


def watch(
    api: str,
    game: str | None,
    since: int,
    max_seconds: float,
    stall_seconds: float,
    poll: float,
    transport: httpx.BaseTransport | None = None,
    review_key: str | None = None,
) -> dict[str, Any]:
    with httpx.Client(base_url=api, timeout=30.0, transport=transport) as client:
        try:
            gid = game or latest_game(client)
        except WatchFailure as exc:
            # `game` rather than `gid`: nothing was resolved, so there is no
            # game id to name, and `None` says exactly that.
            return {"edge": exc.edge, "game": game, "status": exc.status, "body": exc.body}
        deadline = time.monotonic() + max_seconds
        last_len, last_change = since, time.monotonic()
        while True:
            try:
                entries = _log_entries(client, gid)
                fresh = entries[since:]
                # DR8: only an `engine`-category (or legacy, no-category)
                # report_problem row stops the watch; `dsl`/`ux` rows are
                # collected into `findings` instead - see the module
                # docstring's edge table.
                reports, findings = _split_reports(fresh)
                if reports:
                    return {
                        "edge": "report",
                        "game": gid,
                        "seq": len(entries),
                        "reports": reports,
                        "findings": findings,
                    }
                state = _get_json(client, f"/api/games/{gid}")
            except WatchFailure as exc:
                return {"edge": exc.edge, "game": gid, "status": exc.status, "body": exc.body}
            if state.get("winner") or state.get("drawn"):
                play_stats, play_stats_reason = _review_play_stats(client, gid, review_key)
                return {
                    "edge": "game_over",
                    "game": gid,
                    "seq": len(entries),
                    "winner": state.get("winner"),
                    "drawn": state.get("drawn"),
                    "turn": state.get("turn"),
                    "findings": findings,
                    "play_stats": play_stats,
                    "play_stats_reason": play_stats_reason,
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
                    "tail": [e.get("action") for e in entries[-5:]],
                    "turn": state.get("turn"),
                    "step": state.get("step"),
                    "priority_holder": state.get("priority_holder"),
                    "findings": findings,
                }
            if now >= deadline:
                return {"edge": "timeout", "game": gid, "seq": len(entries), "findings": findings}
            time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--game", default=None)
    parser.add_argument("--since", type=int, default=0, help="ignore log entries before this index")
    parser.add_argument("--max-seconds", type=float, default=1800.0)
    parser.add_argument("--stall-seconds", type=float, default=600.0)
    parser.add_argument("--poll", type=float, default=3.0)
    parser.add_argument(
        "--review-key",
        default=None,
        help=(
            "admin key or seat uid for this game's GET /review, used only to fill in the "
            "game_over edge's play_stats (DR2); omit it and play_stats comes back null with "
            "a play_stats_reason instead of failing the watch"
        ),
    )
    args = parser.parse_args(argv)
    result = watch(
        args.api,
        args.game,
        args.since,
        args.max_seconds,
        args.stall_seconds,
        args.poll,
        review_key=args.review_key,
    )
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
