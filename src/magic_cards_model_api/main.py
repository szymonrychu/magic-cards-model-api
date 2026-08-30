"""The uvicorn entry point: `uv run uvicorn magic_cards_model_api.main:app`."""

from magic_cards_model_api.app import app

__all__ = ["app"]
