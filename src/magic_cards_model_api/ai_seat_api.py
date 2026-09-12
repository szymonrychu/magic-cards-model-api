"""Endpoints the UI uses to start/watch/stop the AI-seat subprocess.

Thin: every actual game action the AI seat takes goes through the ordinary
seat API (`seat_cli`, talking to `app.py` like any other seat) - this module
only starts that subprocess and reports on it. See `ai_seat.py`'s docstring
for the mechanism and its fallback.

**These are also the buddy's restart and stop** (B5). A buddy is an advisor
(`repository.Advisor`) with a runner behind it, and the runner is the same
`ai_seat.registry` an AI seat's is - so it needs no routes of its own, only
these two taught to resolve the other kind of credential. The one thing that
could not be shared is which *field* names the target: see `StartAiSeat`.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter

from . import ai_seat, game_access
from .agent_settings import AgentSettings
from .schemas import RequestModel
from .store import store

router = APIRouter(prefix="/api/lobby", tags=["lobby"], route_class=game_access.GameAccessRoute)


class StartAiSeat(RequestModel):
    uid: str = ""
    """The seat uid to start or stop. For a seat this is both the target and
    the caller's own credential - they are the same string - which is why
    `game_access._credentials` reads a body's `uid` as the credential it
    authorises the request with."""
    advisor_uid: str = ""
    """An advisor uid to start or stop instead, named in its own field
    **precisely because it is not a credential this caller is presenting.**

    An advisor uid IS a credential, and a read-only one: `game_access.py`
    refuses every POST made with it. Putting a buddy's uid in `uid` above
    would therefore have the gate judge this request as the *advisor's* and
    refuse it - the human asking for their own buddy back would get a 404.
    So the target goes here, where the gate does not read it, and the caller
    authorises itself the way it always did: its own seat uid in `?uid=`, its
    bearer token as the game's owner, or the game's admin key.
    """

    def target(self) -> tuple[str, str]:
        """`(uid, role)` this request is about. Exactly one field may name
        it; naming neither, or both, is a refusal rather than a guess about
        which one was meant."""
        if bool(self.uid) == bool(self.advisor_uid):
            raise ValueError("name exactly one of `uid` (a seat) or `advisor_uid` (a buddy)")
        if self.advisor_uid:
            return self.advisor_uid, ai_seat.ROLE_ADVISOR
        return self.uid, ai_seat.ROLE_SEAT


def _resolve(game_id: str, req: StartAiSeat) -> tuple[str, str, str, AgentSettings]:
    """`(uid, role, player, agent)` for the runner this request names - the
    one lookup `start` and `stop` share.

    This is `store.player_for_uid` extended to advisors: a seat uid resolves
    to the player it plays and that player's own `agent_settings`, and an
    advisor uid resolves to the player it *reads* and to the settings stored
    inside its own advisor entry (`repository.Advisor.agent`, never the
    name-keyed map - `decks_api.NewUiPlayer.buddy_agent` says why). Anything
    else is a 404, indistinguishable from a game that does not exist, the
    same refusal this route has always given an unknown uid.
    """
    uid, role = req.target()
    if role == ai_seat.ROLE_ADVISOR:
        advisor = store.advisor_uids(game_id).get(uid)
        if advisor is None:
            raise KeyError("that uid is no advisor in this game")
        agent = advisor.agent or AgentSettings.default(os.environ)
        return uid, role, advisor.player, agent
    player = store.player_for_uid(game_id, uid)
    if player is None:
        raise KeyError("that uid is no seat in this game")
    agent = store.agent_settings(game_id).get(player) or AgentSettings.default(os.environ)
    return uid, role, player, agent


@router.post("/games/{game_id}/ai-seat/start")
def start_ai_seat(game_id: str, req: StartAiSeat) -> dict[str, Any]:
    """Start (or report) the AI seat or buddy this body names.

    The restart half of a buddy's lifecycle: creation starts one
    (`decks_api.start_game_from_decklists`), and this is how a human brings
    back one that fell over, or one whose game they have rejoined in a new
    browser. Never raises for a runner that cannot start -
    `AiSeatRegistry.start`'s own contract - so a buddy that will not come up
    answers 200 with `mechanism: "fallback"` and a reason, exactly as a seat
    does.
    """
    if store.get(game_id) is None:
        raise KeyError(f"no game {game_id}")
    uid, role, player, agent = _resolve(game_id, req)
    return ai_seat.registry.start(
        uid=uid,
        game_id=game_id,
        player=player,
        api_base=ai_seat.api_service_url(),
        agent=agent,
        role=role,
    )


@router.get("/games/{game_id}/ai-seat/status")
def ai_seat_status(game_id: str, uid: str) -> dict[str, Any]:
    status = ai_seat.registry.status(uid)
    if status is None:
        return {"uid": uid, "mechanism": None, "state": "not_started"}
    return status


@router.post("/games/{game_id}/ai-seat/stop")
def stop_ai_seat(game_id: str, req: StartAiSeat) -> dict[str, Any]:
    """Stop the AI seat or buddy this body names. The uid is resolved the
    same way `start` resolves it, so stopping something this game has never
    heard of is a 404 rather than a cheerful `stopped: false`."""
    if store.get(game_id) is None:
        raise KeyError(f"no game {game_id}")
    uid, _role, _player, _agent = _resolve(game_id, req)
    return {"stopped": ai_seat.registry.stop(uid)}
