"""Choreography: who is playing what, and which turn to push next.

The driver holds no game state. It learns of a game when ai_seat.py hands it
a dispatched seat, reads that seat's decklist from the game server, works out
which guided deck it is, writes `current-game.md`, and pushes turn 0. When a
turn finishes it asks the game server whether the game ended; if it did, it
pushes exactly one reflection turn.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import DriverSettings
from .guides import GuideLibrary, parse_decklist
from .prompt import Assignment, reflection_turn, render_current_game, turn_zero

log = logging.getLogger("driver")


class DispatchIn(BaseModel):
    uid: str
    game_id: str
    player: str
    api_base: str = ""


def _default_client_factory(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=30.0)


def build_app(
    settings: DriverSettings,
    *,
    client_factory: Callable[[str], httpx.Client] | None = None,
) -> FastAPI:
    app = FastAPI(title="mtg-agent-driver")
    library = GuideLibrary(settings.guides_root)
    make_client = client_factory or _default_client_factory
    lock = threading.Lock()
    current: dict[str, Any] = {
        "assignment": None,
        "lastTurnId": None,
        "reflectionTurnId": None,
        "reflected": False,
    }

    def game_state(game_id: str, uid: str) -> dict:
        with make_client(settings.game_api) as client:
            response = client.get(f"/api/games/{game_id}", params={"uid": uid})
            response.raise_for_status()
            return response.json()

    def push_turn(text: str) -> str:
        headers = {"X-Wrapper-Secret": settings.wrapper_secret} if settings.wrapper_secret else {}
        with make_client(settings.wrapper_api) as client:
            response = client.post(
                "/v1/messages",
                json={"text": text, "callbackUrl": f"{settings.self_url}/turn-done"},
                headers=headers,
            )
            response.raise_for_status()
            return response.json()["turnId"]

    def seat_decklist(state: dict, player: str) -> str:
        for seat in state.get("players") or []:
            if seat.get("username") == player:
                return seat.get("decklist") or ""
        return ""

    def opponent_of(state: dict, player: str) -> str:
        names = [s.get("username", "") for s in state.get("players") or []]
        others = [n for n in names if n and n != player]
        return others[0] if others else ""

    @app.post("/dispatch", status_code=202)
    def dispatch(body: DispatchIn) -> dict:
        with lock:
            existing = current["assignment"]
            if existing is not None and existing.uid == body.uid:
                log.info("dispatch for uid %s already served", body.uid)
                return {
                    "deck": existing.guide.slug if existing.guide else None,
                    "turnId": current["lastTurnId"],
                    "alreadyRunning": True,
                }

        try:
            state = game_state(body.game_id, body.uid)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"game server: {exc}") from exc

        decklist = parse_decklist(seat_decklist(state, body.player))
        guide, score = library.identify(decklist)
        assignment = Assignment(
            game_id=body.game_id,
            uid=body.uid,
            player=body.player,
            opponent=opponent_of(state, body.player),
            api_base=body.api_base or settings.game_api,
            decklist=decklist,
            guide=guide,
            match_score=score,
        )

        settings.workspace.mkdir(parents=True, exist_ok=True)
        settings.memory_root.mkdir(parents=True, exist_ok=True)
        current_game_path = settings.workspace / "current-game.md"
        # The agent lives in another container and cannot open this file, so
        # turn 0 carries its contents; the path is kept for the driver's own
        # operators to read.
        briefing = render_current_game(assignment, settings.memory_root)
        current_game_path.write_text(briefing)

        turn_id = push_turn(turn_zero(assignment, current_game_path, briefing))
        with lock:
            current.update(
                assignment=assignment,
                lastTurnId=turn_id,
                reflectionTurnId=None,
                reflected=False,
            )
        log.info(
            "dispatched game=%s player=%s deck=%s turn=%s",
            body.game_id,
            body.player,
            assignment.deck_slug,
            turn_id,
        )
        return {"deck": guide.slug if guide else None, "turnId": turn_id, "matchScore": score}

    @app.post("/turn-done", status_code=202)
    def turn_done(record: dict) -> dict:
        with lock:
            assignment = current["assignment"]
            reflected = current["reflected"]
            reflection_id = current["reflectionTurnId"]
        if assignment is None:
            return {"ignored": "no assignment"}
        if reflected or record.get("turnId") == reflection_id:
            return {"ignored": "already reflected"}

        try:
            state = game_state(assignment.game_id, assignment.uid)
        except httpx.HTTPError as exc:
            log.warning("could not read game state after a turn: %s", exc)
            return {"ignored": "game unreachable"}

        winner = state.get("winner")
        drawn = bool(state.get("drawn"))
        if not winner and not drawn:
            return {"ignored": "game still running"}

        outcome = f"{winner} won" if winner else "the game was a draw"
        turn_id = push_turn(reflection_turn(assignment, settings.memory_root, outcome))
        with lock:
            current.update(reflected=True, reflectionTurnId=turn_id)
        log.info("game %s over (%s), reflection turn %s", assignment.game_id, outcome, turn_id)
        return {"reflectionTurnId": turn_id, "outcome": outcome}

    @app.get("/status")
    def status() -> dict:
        with lock:
            assignment = current["assignment"]
            return {
                "gameId": assignment.game_id if assignment else None,
                "player": assignment.player if assignment else None,
                "deck": (assignment.guide.slug if assignment and assignment.guide else None),
                "lastTurnId": current["lastTurnId"],
                "reflected": current["reflected"],
            }

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app
