"""uvicorn entry point for the driver."""

from __future__ import annotations

import logging
import os

import uvicorn

from .app import build_app
from .config import DriverSettings


def main() -> None:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s %(message)s")
    settings = DriverSettings.from_env(os.environ)
    host, _, port = settings.http_addr.rpartition(":")
    uvicorn.run(build_app(settings), host=host or "0.0.0.0", port=int(port))


if __name__ == "__main__":
    main()
