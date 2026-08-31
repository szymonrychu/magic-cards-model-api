"""The uvicorn entry point: `uv run uvicorn magic_cards_model_api.main:app`.

Importing this module also installs the access-log filter that keeps the
container healthcheck's five-second `GET /api/health` out of the log while
leaving every other request - and every *failing* health probe - in it. See
`access_log.py` for why it is done here and not by turning the access log off.
"""

from magic_cards_model_api import access_log
from magic_cards_model_api.app import app

access_log.install()

__all__ = ["app"]
