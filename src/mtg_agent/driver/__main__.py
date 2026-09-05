"""uvicorn entry point for the driver.

A compose-style driver runs `uvicorn.run` and sits forever, taking whatever
`POST /dispatch` sends it. A per-game Kubernetes Job container is not meant to
sit: it dispatches itself from `DRIVER_ASSIGNMENT_JSON` on startup (see the
`build_app` lifespan hook) and, once `DRIVER_EXIT_AFTER_MATCH` is set, `retire`
flips `app.state.should_exit` when that one match ends. A daemon thread polls
that flag the way `wrapper/__main__.py` polls its own `should_exit` - the
server object is the only thing that can stop `server.run()`, and nothing else
in this process would otherwise notice the flag changed.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import uvicorn

from magic_cards_model_api import access_log

from .app import build_app
from .config import DriverSettings


def _watch_for_exit(app, server: uvicorn.Server) -> None:
    while not server.should_exit:
        if app.state.should_exit:
            server.should_exit = True
            return
        time.sleep(1.0)


def main() -> None:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s %(message)s")
    # httpx logs one INFO line per request, which here means two lines of
    # "HTTP Request: POST http://agent:8080/v1/messages" for every turn - the
    # driver's own log already says what it pushed and what came back, so this
    # is pure padding between the lines an operator is reading for.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # The game server re-reads `GET /status` for as long as a seat is
    # dispatched, which was one uvicorn access line per poll. See
    # access_log.py; a `/status` that starts failing still prints.
    access_log.install()
    settings = DriverSettings.from_env(os.environ)
    host, _, port = settings.http_addr.rpartition(":")
    app = build_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host=host or "0.0.0.0", port=int(port)))
    threading.Thread(target=_watch_for_exit, args=(app, server), daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
