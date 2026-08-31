"""uvicorn entry point for the driver."""

from __future__ import annotations

import logging
import os

import uvicorn

from .app import build_app
from .config import DriverSettings


def main() -> None:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s %(message)s")
    # httpx logs one INFO line per request, which here means two lines of
    # "HTTP Request: POST http://agent:8080/v1/messages" for every turn - the
    # driver's own log already says what it pushed and what came back, so this
    # is pure padding between the lines an operator is reading for.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = DriverSettings.from_env(os.environ)
    host, _, port = settings.http_addr.rpartition(":")
    uvicorn.run(build_app(settings), host=host or "0.0.0.0", port=int(port))


if __name__ == "__main__":
    main()
