"""Keep the container healthcheck out of uvicorn's access log.

`docker-compose.yml` probes `GET /api/health` every five seconds. Uvicorn's
access logger prints one line per request, so at default settings the game
container emits 17k healthcheck lines a day - enough to bury every real
request and every problem report between them.

Of the ways to fix that, this one is the narrowest:

- turning the access log off (`--no-access-log`) would hide real traffic too,
  which is exactly what the user does not want;
- changing the access-log *format* means replacing uvicorn's formatter and
  still deciding, per line, whether to print - the same filter with more
  moving parts;
- pointing the healthcheck at something that is not an HTTP route (a socket
  connect, a `python -c` import) stops testing the thing that matters: that
  the app answers requests.

So the probe keeps hitting the real route, and one `logging.Filter` on
`uvicorn.access` drops the line - but only when the probe *succeeded*. A
health request that 404s, 500s, or is refused still prints, because a
healthcheck that started failing is precisely the thing worth seeing.

Installed from `main.py`, the uvicorn entry point, which is imported after
uvicorn has configured logging - so the filter is attached to the logger
uvicorn actually built, not to one it is about to replace.
"""

from __future__ import annotations

import logging

#: The path the compose healthcheck probes.
HEALTH_PATH = "/api/health"

_FILTER_NAME = "mcm-health-access"


class HealthAccessFilter(logging.Filter):
    """Drop `uvicorn.access` records for successful healthcheck probes.

    Uvicorn logs access lines with `record.args` of
    `(client_addr, method, full_path, http_version, status_code)`. Anything
    that does not have that shape is passed through untouched: this filter
    must never be the reason a log line goes missing.
    """

    def __init__(self, path: str = HEALTH_PATH) -> None:
        super().__init__(name=_FILTER_NAME)
        self.path = path

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _client, method, full_path, _http_version, status = args
        if method != "GET" or str(full_path).split("?")[0] != self.path:
            return True
        try:
            code = int(status)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return True
        # 2xx/3xx: the probe worked, nobody needs to read about it. Anything
        # else is a healthcheck going wrong and stays in the log.
        return code >= 400


def install(path: str = HEALTH_PATH) -> HealthAccessFilter:
    """Attach the filter to `uvicorn.access`, at most once.

    Idempotent: importing the entry point twice (or a test importing it after
    the server did) must not stack filters."""
    logger = logging.getLogger("uvicorn.access")
    for existing in logger.filters:
        if getattr(existing, "name", None) == _FILTER_NAME:
            return existing  # type: ignore[return-value]
    installed = HealthAccessFilter(path)
    logger.addFilter(installed)
    return installed
