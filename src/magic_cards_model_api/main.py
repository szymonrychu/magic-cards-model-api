"""The uvicorn entry point: `uv run uvicorn magic_cards_model_api.main:app`.

Importing this module also installs the access-log filter that keeps the
polling out of the log: the healthcheck's five-second `GET /api/health`, and
the browser's `GET /api/games/{id}`, `GET /api/games/{id}/log` and
`GET /api/ui/games/{id}/ai-seat/status` every ~1.2s. Every mutation, and every
*failing* poll, still prints. See `access_log.py` for why it is done here and
not by turning the access log off, and `ACCESS_LOG_QUIET=0` to turn it back
on.
"""

from magic_cards_model_api import access_log
from magic_cards_model_api.app import app

# This process still serves both the api's and the lobby's routes, pre-cut
# (T13, phase 2b) - so it installs both owners' quiet patterns.
access_log.install(access_log.API_QUIET_PATHS + access_log.LOBBY_QUIET_PATHS)

__all__ = ["app"]
