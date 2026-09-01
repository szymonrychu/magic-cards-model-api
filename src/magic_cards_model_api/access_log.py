"""Keep the polling out of uvicorn's access log, on all three containers.

`docker compose up` shows one interleaved stream, and almost every line in it
used to be a poll:

    driver-1  | INFO: 172.18.0.2:58554 - "GET /status HTTP/1.1" 200 OK
    game-1    | INFO: ... - "GET /api/ui/games/<id>/ai-seat/status?uid=..." 200 OK
    game-1    | INFO: ... - "GET /api/games/<id>?uid=... HTTP/1.1" 200 OK

The browser re-reads the game every ~1.2s, the play UI re-reads the AI seat's
status alongside it, the game server re-reads the driver's `/status`, and the
compose healthcheck probes `/api/health` every five seconds. Together that is
tens of thousands of lines a day, and it buries the only thing anyone opens
the log for: what the AI opponent is doing.

Of the ways to fix that, this one is the narrowest:

- turning the access log off (`--no-access-log`) would hide real traffic too,
  which is exactly what the user does not want;
- changing the access-log *format* means replacing uvicorn's formatter and
  still deciding, per line, whether to print - the same filter with more
  moving parts;
- pointing the healthcheck at something that is not an HTTP route (a socket
  connect, a `python -c` import) stops testing the thing that matters: that
  the app answers requests.

So every probe keeps hitting its real route, and one `logging.Filter` on
`uvicorn.access` drops the line - under two conditions that are the whole
safety argument:

1. **only reads.** GET and HEAD. A POST is a decision somebody made and is
   always printed, whatever path it went to.
2. **only successes.** Status < 400. A polled endpoint answering 404 or 500
   is precisely the thing worth seeing, so failure re-enables the line.

`ACCESS_LOG_QUIET=0` in the environment turns the whole thing off and gives
back uvicorn's unfiltered log.

Installed from each service's entry point (`magic_cards_model_api.main`,
`mtg_agent.driver.__main__`, `mtg_agent.wrapper.__main__`), which run after
uvicorn has configured logging - so the filter is attached to the logger
uvicorn actually built, not to one it is about to replace.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping

#: The path the compose healthcheck probes.
HEALTH_PATH = "/api/health"

#: Env var that turns the filter off. Quiet is the default.
QUIET_ENV = "ACCESS_LOG_QUIET"

_FILTER_NAME = "mcm-quiet-access"

#: Read-only, high-frequency routes, one regex each, anchored whole-path.
#:
#: Deliberately narrow. `/api/games/{id}` is the browser's state poll and is
#: silenced, but `/api/games` (the list) and `/api/games/{id}/anything-else`
#: are not - a sub-route is somebody asking a new question, not the loop.
QUIET_PATHS: tuple[re.Pattern[str], ...] = (
    # the compose healthcheck, and the liveness/readiness routes on the
    # driver and the agent wrapper
    re.compile(r"/api/health"),
    re.compile(r"/healthz"),
    re.compile(r"/readyz"),
    # the driver's assignment poll, read by the game server's AI seat
    re.compile(r"/status"),
    # the browser's game-state poll
    re.compile(r"/api/games/[^/]+"),
    # the play UI's AI-seat poll
    re.compile(r"/api/ui/games/[^/]+/ai-seat/status"),
)

#: Methods that can be silenced. Everything else - POST, DELETE, PATCH - is a
#: mutation and always prints, however chatty.
QUIET_METHODS = frozenset({"GET", "HEAD"})


def quiet_enabled(env: Mapping[str, str] | None = None) -> bool:
    raw = (env if env is not None else os.environ).get(QUIET_ENV, "").strip().lower()
    if not raw:
        return True
    return raw not in {"0", "false", "no", "off"}


def is_quiet_path(path: str) -> bool:
    """True when `path` (query string already removed) is one of the polls."""
    return any(pattern.fullmatch(path) for pattern in QUIET_PATHS)


class QuietAccessFilter(logging.Filter):
    """Drop `uvicorn.access` records for successful reads of polled routes.

    Uvicorn logs access lines with `record.args` of
    `(client_addr, method, full_path, http_version, status_code)`. Anything
    that does not have that shape is passed through untouched: this filter
    must never be the reason a log line goes missing.
    """

    def __init__(self, patterns: tuple[re.Pattern[str], ...] = QUIET_PATHS) -> None:
        super().__init__(name=_FILTER_NAME)
        self.patterns = patterns

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _client, method, full_path, _http_version, status = args
        if str(method).upper() not in QUIET_METHODS:
            return True
        path = str(full_path).split("?")[0]
        if not any(pattern.fullmatch(path) for pattern in self.patterns):
            return True
        try:
            code = int(status)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return True
        # 2xx/3xx: the poll worked, nobody needs to read about it. Anything
        # else is a polled route going wrong and stays in the log.
        return code >= 400


#: The pre-rename name. Kept so nothing that imported it breaks.
HealthAccessFilter = QuietAccessFilter


def install(env: Mapping[str, str] | None = None) -> QuietAccessFilter | None:
    """Attach the filter to `uvicorn.access`, at most once.

    Returns `None` when `ACCESS_LOG_QUIET` is off, and removes any filter a
    previous call installed - so the env var is the last word rather than the
    import order. Idempotent otherwise: importing an entry point twice (or a
    test importing it after the server did) must not stack filters."""
    logger = logging.getLogger("uvicorn.access")
    already = [f for f in logger.filters if getattr(f, "name", None) == _FILTER_NAME]
    if not quiet_enabled(env):
        for stale in already:
            logger.removeFilter(stale)
        return None
    if already:
        return already[0]  # type: ignore[return-value]
    installed = QuietAccessFilter()
    logger.addFilter(installed)
    return installed
