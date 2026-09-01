"""Choreography: who is playing what, and which turn to push next.

The driver holds no game state. It learns of a game when ai_seat.py hands it
a dispatched seat, reads that seat's decklist from the game server, works out
which guided deck it is, writes `current-game.md`, and pushes turn 0. When a
turn finishes it asks the game server whether the game ended; if it did, it
pushes exactly one reflection turn.

One agent container plays exactly one match. Once the reflection turn is in,
the driver sends `DELETE /v1/session` and forgets the assignment: the agent
process exits, its restart policy brings up a fresh container with a fresh
conversation, and the next game starts from the notes on disk rather than
from a session that remembers the last one. The driver itself is not
recycled - it is the thing that survives to dispatch the next match.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import DriverSettings
from .guides import GuideLibrary, parse_decklist
from .prompt import Assignment, reflection_turn, render_current_game, turn_zero

log = logging.getLogger("driver")

#: How much of the agent's own account of a turn goes into the container log.
#: The whole `finalText` can be a page; the first paragraphs are where the
#: agent says what it did and why, which is what a person watching
#: `docker compose logs` wants. The full text is always in the turn record.
FINAL_TEXT_CHARS = 1200


def trim_final_text(text: str, limit: int = FINAL_TEXT_CHARS) -> str:
    """`text` collapsed to what is worth putting in the log: blank lines
    dropped, indented one level so it reads as a quoted block, and cut at
    `limit` characters with an explicit marker rather than silently."""
    body = "\n".join(line.rstrip() for line in text.strip().splitlines() if line.strip())
    if not body:
        return "  (the agent finished the turn without saying anything)"
    if len(body) > limit:
        body = body[:limit].rstrip() + f"\n... [{len(text) - limit} more characters]"
    return "\n".join(f"  {line}" for line in body.splitlines())


class DispatchIn(BaseModel):
    uid: str
    game_id: str
    player: str
    api_base: str = ""


def _default_client_factory(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=30.0)


#: Statuses the agent answers with while it is still coming up, or while a
#: turn from its previous life is still draining. All of them clear on their
#: own; none of them means "this game is not for me".
BOOTING_STATUSES = frozenset({409, 502, 503, 504})


class AgentUnavailable(RuntimeError):
    """The agent could not take a turn. `retryable` means it is expected to
    clear on its own - a container mid-restart, or one still draining a turn
    from its previous life."""

    def __init__(self, detail: str, *, retryable: bool) -> None:
        super().__init__(detail)
        self.retryable = retryable


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
        "pending": False,
        # True from the moment a turn is pushed to the agent until its
        # `/turn-done` callback lands: "claude has this turn and has not
        # finished it". The game server reads it off `/status` to tell a seat
        # that is thinking from a seat that has stopped playing - one long
        # turn covers a whole match here, so a *finished* turn in a game that
        # is still running is an agent that gave up, not a healthy idle.
        "turnActive": False,
        "lastError": None,
    }

    def clear_locked() -> None:
        current.update(
            assignment=None,
            lastTurnId=None,
            reflectionTurnId=None,
            reflected=False,
            pending=False,
            turnActive=False,
        )

    def game_state(game_id: str, uid: str) -> dict:
        with make_client(settings.game_api) as client:
            response = client.get(f"/api/games/{game_id}", params={"uid": uid})
            if response.status_code >= 400:
                raise httpx.HTTPError(f"game server answered {response.status_code}")
            return response.json()

    def wrapper_headers() -> dict[str, str]:
        return {"X-Wrapper-Secret": settings.wrapper_secret} if settings.wrapper_secret else {}

    def push_turn(text: str) -> str:
        """Statuses are read off the response rather than through
        `raise_for_status`, because the client is injected: the exception
        classes belong to whichever http library built it, while the status
        code is the same everywhere."""
        try:
            with make_client(settings.wrapper_api) as client:
                response = client.post(
                    "/v1/messages",
                    json={"text": text, "callbackUrl": f"{settings.self_url}/turn-done"},
                    headers=wrapper_headers(),
                )
        except Exception as exc:  # noqa: BLE001 - nothing answered on the socket
            raise AgentUnavailable(f"cannot reach the agent: {exc}", retryable=True) from exc
        if response.status_code >= 400:
            raise AgentUnavailable(
                f"the agent answered {response.status_code}",
                retryable=response.status_code in BOOTING_STATUSES,
            )
        turn_id = response.json()["turnId"]
        # Named seat and game, because the compose log interleaves three
        # services and "turn pushed" alone does not say who is playing what.
        with lock:
            assignment = current["assignment"]
        log.info(
            "turn %s started: game=%s seat=%s deck=%s (%d chars of prompt)",
            turn_id,
            assignment.game_id if assignment else "?",
            assignment.player if assignment else "?",
            assignment.deck_slug if assignment else "?",
            len(text),
        )
        return turn_id

    def retire(assignment: Assignment) -> bool:
        """End the agent's life. `DELETE /v1/session` makes it exit, its
        restart policy gives the next match a fresh container, and we drop
        every trace of this game so the next dispatch starts clean.

        A failure here is logged and swallowed: an agent we cannot reach is
        already gone, and refusing to forget the finished game would wedge
        the driver against every future dispatch."""
        stopped = True
        try:
            with make_client(settings.wrapper_api) as client:
                response = client.delete("/v1/session", headers=wrapper_headers())
            if response.status_code >= 400:
                stopped = False
                log.warning("the agent refused to stop: %s", response.status_code)
        except Exception as exc:  # noqa: BLE001 - an agent we cannot reach is already gone
            stopped = False
            log.warning("could not stop the agent after game %s: %s", assignment.game_id, exc)
        with lock:
            clear_locked()
        log.info("match %s retired, agent stopped=%s", assignment.game_id, stopped)
        return stopped

    def push_turn_when_reachable(assignment: Assignment, text: str) -> None:
        """Keep offering turn 0 to an agent that is restarting.

        A dispatch can land in the window between one match ending and the
        replacement container being ready - pty spawn, bypass dialog and
        quiescence take up to about two minutes. Failing the dispatch there
        would drop a game the human already started, and blocking the request
        for two minutes would outlast the caller's own 30s timeout, so the
        retry runs here, behind an already-accepted 202. It is bounded: after
        `agent_boot_wait` the assignment is dropped with a recorded error,
        which leaves the driver able to accept the next dispatch instead of
        holding a game nobody is playing."""
        deadline = time.monotonic() + settings.agent_boot_wait
        while True:
            with lock:
                if current["assignment"] is not assignment:
                    return  # superseded or retired while we waited
            try:
                turn_id = push_turn(text)
            except AgentUnavailable as exc:
                if not exc.retryable or time.monotonic() >= deadline:
                    with lock:
                        if current["assignment"] is assignment:
                            clear_locked()
                            current["lastError"] = f"agent unreachable: {exc}"
                    log.error(
                        "giving up on game %s: agent unreachable: %s", assignment.game_id, exc
                    )
                    return
                time.sleep(settings.agent_retry_interval)
                continue
            with lock:
                if current["assignment"] is assignment:
                    current.update(
                        lastTurnId=turn_id, pending=False, turnActive=True, lastError=None
                    )
            log.info("game %s reached the agent on retry, turn %s", assignment.game_id, turn_id)
            return

    def seat_decklist(state: dict, player: str) -> str:
        """This seat's own decklist text, out of its `players` block.

        `state` is `GET /api/games/{id}` read with *this seat's* uid, and the
        game server puts `decklist` on the viewer's own player block only - a
        seat knows the deck it registered and never the opponent's (see
        `visibility._redact_player`). So the loop below can only ever find its
        own list, and an opponent's entry simply has no `decklist` key.

        Empty for a game the server was not booted from decklists (`POST
        /api/games` builds a board directly), which is the honest answer: there
        is no list to identify, and `GuideLibrary.identify` scores an empty
        list at 0 rather than guessing a guide.
        """
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
                    "pending": current["pending"],
                }
            if existing is not None:
                # One container, one match. Retargeting mid-game would hand
                # the second game a conversation full of the first one, and
                # abandon the first game's seat with nobody playing it.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"this agent is already playing game {existing.game_id} "
                        f"as {existing.player}; it plays one match per container "
                        "and is free again once that game ends"
                    ),
                )

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

        text = turn_zero(assignment, current_game_path, briefing)
        with lock:
            # Claim the agent before talking to it, so a second game arriving
            # while we are pushing gets the 409 rather than a race.
            rival = current["assignment"]
            if rival is not None and rival.uid != body.uid:
                raise HTTPException(
                    status_code=409,
                    detail=f"this agent is already playing game {rival.game_id}",
                )
            current.update(
                assignment=assignment,
                lastTurnId=None,
                reflectionTurnId=None,
                reflected=False,
                pending=True,
                turnActive=False,
                lastError=None,
            )

        try:
            turn_id = push_turn(text)
        except AgentUnavailable as exc:
            if not exc.retryable:
                with lock:
                    clear_locked()
                    current["lastError"] = f"agent refused turn 0: {exc}"
                raise HTTPException(status_code=502, detail=f"agent: {exc}") from exc
            log.warning(
                "agent not ready for game %s, retrying in the background: %s", body.game_id, exc
            )
            threading.Thread(
                target=push_turn_when_reachable,
                args=(assignment, text),
                daemon=True,
                name="driver-push-turn-zero",
            ).start()
            return {
                "deck": guide.slug if guide else None,
                "turnId": None,
                "matchScore": score,
                "pending": True,
                "reason": f"the agent is restarting; turn 0 will be pushed when it answers ({exc})",
            }

        with lock:
            if current["assignment"] is assignment:
                current.update(lastTurnId=turn_id, pending=False, turnActive=True)
        log.info(
            "dispatched game=%s player=%s vs=%s deck=%s (match %.2f, %d cards) turn=%s",
            body.game_id,
            body.player,
            assignment.opponent or "?",
            assignment.deck_slug,
            score,
            sum(decklist.values()),
            turn_id,
        )
        return {
            "deck": guide.slug if guide else None,
            "turnId": turn_id,
            "matchScore": score,
            "pending": False,
        }

    def log_turn_result(record: dict) -> None:
        """The agent's own account of the turn it just finished.

        This callback carried `finalText` all along and the driver threw it
        away, which is why nothing the agent said ever reached the compose
        log. A failed turn is logged at ERROR with its reason; a normal one at
        INFO with a trimmed block of what the agent wrote."""
        turn_id = record.get("turnId") or "?"
        state = record.get("state") or "?"
        if state == "failed" or record.get("error"):
            reason = str(record.get("error") or state)
            log.error("turn %s failed: %s", turn_id, reason)
            # `/status` is the only thing the game server reads about this
            # seat, and a failed turn used to leave `lastError` at None -
            # so a seat whose agent had given up was indistinguishable from
            # one still thinking, for as long as the human cared to wait.
            with lock:
                current["lastError"] = f"turn {turn_id} failed: {reason}"
            return
        with lock:
            current["lastError"] = None
        stop = record.get("stopReason") or ""
        head = f"turn {turn_id} done ({state}{', stop=' + stop if stop else ''}); the agent says:"
        log.info("%s\n%s", head, trim_final_text(str(record.get("finalText") or "")))

    @app.post("/turn-done", status_code=202)
    def turn_done(record: dict) -> dict:
        log_turn_result(record)
        with lock:
            # The turn the agent just finished is no longer in flight. Set
            # before anything else here, so a reflection push below can turn
            # it back on and a game that is still running reports the truth:
            # nothing is playing this seat at the moment.
            current["turnActive"] = False
            assignment = current["assignment"]
            reflected = current["reflected"]
            reflection_id = current["reflectionTurnId"]
        if assignment is None:
            return {"ignored": "no assignment"}
        if reflection_id is not None and record.get("turnId") == reflection_id:
            # The notes are written. The match is over and so is this agent:
            # stop it, forget the game, and let the restart policy give the
            # next dispatch a container with no memory of this one.
            stopped = retire(assignment)
            return {"retired": True, "gameId": assignment.game_id, "agentStopped": stopped}
        if reflected:
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
        try:
            turn_id = push_turn(reflection_turn(assignment, settings.memory_root, outcome))
        except AgentUnavailable as exc:
            # No notes from this game, but the match is still over. Retire it
            # rather than hold an assignment that would 409 every future
            # dispatch against an agent that is already being replaced.
            log.error("game %s ended but the agent could not reflect: %s", assignment.game_id, exc)
            retire(assignment)
            return {"retired": True, "gameId": assignment.game_id, "reflected": False}
        with lock:
            current.update(reflected=True, reflectionTurnId=turn_id, turnActive=True)
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
                "pending": current["pending"],
                "turnActive": current["turnActive"],
                "lastError": current["lastError"],
            }

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app
