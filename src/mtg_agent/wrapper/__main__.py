"""PID 1: config -> bootstrap -> session -> two servers.

The internal server binds loopback only. The public server is not served
until the session has booted, so a container health check simply retries
while claude is still starting.

This process is deliberately short-lived: it serves exactly one match. When
the driver sends `DELETE /v1/session`, or when the supervised claude dies,
the process exits and the container's restart policy brings up a fresh one
with a brand-new session. Only `/workspace/memory` crosses that boundary.
"""

from __future__ import annotations

import logging
import os
import signal
import threading

import uvicorn

from magic_cards_model_api import access_log

from . import bootstrap
from .activity import ActivityWatcher
from .api import build_internal_app, build_public_app
from .config import Settings
from .metrics import Metrics
from .session import Manager
from .turns import Store
from .webhook import Deliverer

log = logging.getLogger("wrapper")


def _split_addr(addr: str, default_host: str) -> tuple[str, int]:
    host, _, port = addr.rpartition(":")
    return (host or default_host), int(port)


def main() -> None:
    settings = Settings.from_env(os.environ)
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(levelname)s %(name)s %(message)s"
    )
    # `/healthz` and `/readyz` are polled; the turns are not. See access_log.py.
    access_log.install(access_log.WRAPPER_QUIET_PATHS)

    bootstrap.render(settings)

    metrics = Metrics(effort=settings.effort)
    store = Store()
    deliverer = Deliverer(
        metrics, retries=settings.webhook_retries, default_url=settings.default_callback_url
    )
    server: uvicorn.Server | None = None

    def request_exit(reason: str) -> None:
        """The one way this process stops. Both callers - the DELETE route and
        a dead claude - want the same thing: close the PTY, stop serving, let
        the restart policy hand the next match a fresh container."""
        log.info("exiting: %s", reason)
        manager.shutdown()
        if server is not None:
            server.should_exit = True

    manager = Manager(
        settings,
        store,
        metrics,
        on_turn_done=deliverer.deliver_async,
        # A session that has died can never serve another turn. Staying up as
        # a zombie would leave the container "running" and unable to work, and
        # the next dispatch would 503 forever; exiting recycles it instead.
        on_dead=lambda: request_exit("the supervised claude exited"),
    )

    internal_host, internal_port = _split_addr(settings.internal_addr, "127.0.0.1")
    internal = uvicorn.Server(
        uvicorn.Config(
            build_internal_app(manager),
            host=internal_host,
            port=internal_port,
            log_level="warning",
        )
    )
    threading.Thread(target=internal.run, daemon=True).start()

    # Built before the session boots so that a claude that dies during boot
    # can still set should_exit on a real server object; run() then returns
    # immediately instead of serving a session that will never be ready.
    public_host, public_port = _split_addr(settings.http_addr, "0.0.0.0")  # noqa: S104
    server = uvicorn.Server(
        uvicorn.Config(
            build_public_app(
                manager,
                settings,
                on_exit=lambda: request_exit("the driver ended the match"),
            ),
            host=public_host,
            port=public_port,
            log_level=settings.log_level,
        )
    )

    log.info("booting claude")
    manager.start()
    log.info("session state=%s", manager.state)

    # Started after the session, so the transcript it follows is this
    # container's. Daemon thread, read-only, and exempt from the exit path:
    # nothing it does can hold up a turn or a shutdown.
    watcher = ActivityWatcher(settings.home_dir, settings.agent_log_level)
    watcher.start()

    def stop(_signum, _frame) -> None:
        request_exit("signal")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.run()
    watcher.stop()
    manager.shutdown()


if __name__ == "__main__":
    main()
