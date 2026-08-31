"""Endpoints the UI uses to start/watch/stop the AI-seat subprocess.

Thin: every actual game action the AI seat takes goes through the ordinary
seat API (`seat_cli`, talking to `app.py` like any other seat) - this module
only starts that subprocess and reports on it. See `ai_seat.py`'s docstring
for the mechanism and its fallback.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import ai_seat
from .store import store

router = APIRouter(prefix="/api/ui", tags=["ui"])


class StartAiSeat(BaseModel):
    uid: str


def _api_base(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


@router.post("/games/{game_id}/ai-seat/start")
def start_ai_seat(game_id: str, req: StartAiSeat, request: Request) -> dict[str, Any]:
    if store.get(game_id) is None:
        raise KeyError(f"no game {game_id}")
    player = store.player_for_uid(game_id, req.uid)
    if player is None:
        raise KeyError("that uid is no seat in this game")
    return ai_seat.registry.start(
        uid=req.uid, game_id=game_id, player=player, api_base=_api_base(request)
    )


@router.get("/games/{game_id}/ai-seat/status")
def ai_seat_status(game_id: str, uid: str) -> dict[str, Any]:
    status = ai_seat.registry.status(uid)
    if status is None:
        return {"uid": uid, "mechanism": None, "state": "not_started"}
    return status


@router.post("/games/{game_id}/ai-seat/stop")
def stop_ai_seat(game_id: str, req: StartAiSeat) -> dict[str, Any]:
    stopped = ai_seat.registry.stop(req.uid)
    return {"stopped": stopped}
