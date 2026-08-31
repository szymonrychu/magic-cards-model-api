"""PID 1: config -> bootstrap -> session -> two servers.

The internal server binds loopback only. The public server is not served
until the session has booted, so a container health check simply retries
while claude is still starting.
"""

from __future__ import annotations

import logging
import os
import signal
import threading

import uvicorn

from . import bootstrap
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

    bootstrap.render(settings)

    metrics = Metrics()
    store = Store()
    deliverer = Deliverer(
        metrics, retries=settings.webhook_retries, default_url=settings.default_callback_url
    )
    manager = Manager(settings, store, metrics, on_turn_done=deliverer.deliver_async)

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

    log.info("booting claude")
    manager.start()
    log.info("session state=%s", manager.state)

    public_host, public_port = _split_addr(settings.http_addr, "0.0.0.0")  # noqa: S104
    server = uvicorn.Server(
        uvicorn.Config(
            build_public_app(manager, settings),
            host=public_host,
            port=public_port,
            log_level=settings.log_level,
        )
    )

    def stop(_signum, _frame) -> None:
        manager.shutdown()
        server.should_exit = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.run()
    manager.shutdown()


if __name__ == "__main__":
    main()
