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

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import DriverSettings
from .guides import GuideLibrary, parse_decklist
from .prompt import (
    Assignment,
    advisor_turn_zero,
    plan_header,
    plan_path,
    reflection_turn,
    render_current_game,
    turn_zero,
)
from .revision import guides_revision

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
    agent: dict | None = None
    role: Literal["seat", "advisor"] = "seat"
    """Which kind of container this dispatch is for: a `seat` that plays
    `player`, or an `advisor` - B5's buddy - that watches `player`'s own view
    and proposes, and whose uid the game server refuses every action from
    (`magic_cards_model_api.game_access`). It picks the prose `assign` pushes
    as turn 0 (`prompt.advisor_turn_zero` rather than `prompt.turn_zero`) and
    nothing else: a buddy reuses the whole of this driver, the wrapper beside
    it and the wake loop a seat runs.

    Defaulted, so a dispatch body written before roles existed - and
    `k8s_seats`' own `DRIVER_ASSIGNMENT_JSON`, which writes the key only for
    a non-default role - reads back as a seat, unchanged."""


class ReleaseIn(BaseModel):
    """Who is giving the agent back. Both fields are optional so a caller that
    knows only one of them can still release the seat it dispatched, but a
    caller that names a game the agent is not playing is refused rather than
    allowed to retire somebody else's match."""

    uid: str = ""
    game_id: str = ""
    reason: str = ""


def _wanted_agent_settings(agent: dict) -> dict:
    """`agent` (`DispatchIn.agent`) read as the one wire spelling -
    camelCase, per `contract/agent-settings.schema.json` - and reshaped into
    exactly what `sync_agent_settings` compares against `GET /v1/session`'s
    answer and posts to `POST /v1/session`. Pulled out on its own so the wire
    read can be proven correct directly, without a live wrapper to talk to
    (see `tests/test_agent_settings_contract.py`)."""
    return {
        "model": agent.get("model") or "",
        "effort": agent.get("effort") or "",
        "maxThinkingTokens": agent.get("maxThinkingTokens"),
    }


def _default_client_factory(base_url: str) -> httpx.Client:
    # This client only ever talks to the wrapper in this same pod (compose:
    # the `agent` service; k8s: the sidecar container on 127.0.0.1) for two
    # kinds of calls: pushing turn text to `POST /v1/messages`, which itself
    # just enqueues the turn and returns - the agent's real thinking finishes
    # later, delivered over the `/turn-done` webhook, never held open on this
    # connection - and the per-game `GET`/`POST`/`DELETE /v1/session` calls
    # ahead of turn 0. None of those is a long-poll; 30s is slack against
    # ordinary jitter and a wrapper still booting, not a step that itself
    # takes anywhere near that long.
    return httpx.Client(base_url=base_url, timeout=30.0)


#: How long `/status` will wait on the wrapper for the agent's current
#: activity. Deliberately far shorter than the 30s above: this is one
#: localhost hop, on the play UI's own poll path, for one cosmetic word - a
#: wrapper that is busy or wedged must cost the pill its label, never hold
#: up the status the pregame backstop also reads.
ACTIVITY_READ_TIMEOUT = 2.0

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
    # False for the whole life of a compose-style driver that just sits and
    # takes dispatches; a per-game Job container flips this in `retire` once
    # `exit_after_match` says the container's one match is over, and
    # `__main__` polls it to know when to stop uvicorn and let the container
    # exit 0.
    app.state.should_exit = False
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

    def seed_plan_file(assignment: Assignment) -> None:
        """Write the plan file's header, so the game's own artifact names the
        guides revision it was played against whatever the agent does next.

        Never overwrites: a redispatch of the same seat (an agent container
        that restarted mid-match) must not throw away the plan the agent has
        already written."""
        path = plan_path(settings.memory_root, assignment)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(plan_header(assignment))

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

    def sync_agent_settings(agent: dict | None) -> None:
        """Push this seat's per-game model/effort/max_thinking_tokens to the
        wrapper before turn 0, if they differ from what it already reports.

        `agent` is `DispatchIn.agent` - `AgentSettings.to_wire()` from the
        game server (see `contract/agent-settings.schema.json`), so its keys
        are `model`/`effort`/`maxThinkingTokens` - one spelling, the same one
        `GET /v1/session` already answers with and `POST /v1/session`
        (`SessionIn`) now requires. Never raises: a wrapper old enough to
        answer 404 to both routes (or one simply unreachable) means "not
        supported, continue" - the seat still gets a match, just not this
        session's chosen model/effort, exactly as it did before this
        pass-through existed.
        """
        if not agent:
            return
        wanted = _wanted_agent_settings(agent)
        try:
            with make_client(settings.wrapper_api) as client:
                current_response = client.get("/v1/session", headers=wrapper_headers())
        except Exception as exc:  # noqa: BLE001 - an unreachable wrapper is not fatal here
            log.warning("could not read wrapper session settings: %s", exc)
            return
        if current_response.status_code == 404:
            log.info("wrapper does not support GET /v1/session; skipping agent settings push")
            return
        if current_response.status_code >= 400:
            log.warning(
                "wrapper session read answered %s; skipping agent settings push",
                current_response.status_code,
            )
            return
        live = current_response.json()
        if (
            live.get("model") == wanted["model"]
            and live.get("effort") == wanted["effort"]
            and live.get("maxThinkingTokens") == wanted["maxThinkingTokens"]
        ):
            return
        try:
            with make_client(settings.wrapper_api) as client:
                post_response = client.post("/v1/session", json=wanted, headers=wrapper_headers())
        except Exception as exc:  # noqa: BLE001 - an unreachable wrapper is not fatal here
            log.warning("could not push agent settings to the wrapper: %s", exc)
            return
        if post_response.status_code == 404:
            log.info(
                "wrapper does not support POST /v1/session; continuing with its own boot defaults"
            )
            return
        if post_response.status_code >= 400:
            log.warning(
                "wrapper refused agent settings model=%s effort=%s: %s",
                wanted["model"],
                wanted["effort"],
                post_response.status_code,
            )
            return
        log.info(
            "pushed agent settings to the wrapper: model=%s effort=%s max_thinking_tokens=%s",
            wanted["model"],
            wanted["effort"],
            wanted["maxThinkingTokens"],
        )

    def reflection_text(text: str) -> str:
        """`text` (the reflection turn's own `finalText`), or a placeholder
        when the agent said nothing - `POST .../reflection`'s `text` field
        is required non-empty (`schemas.Stated`), and a reflection turn that
        finished without a word is itself worth a row rather than silently
        posting none."""
        body = text.strip()
        return body if body else "(the agent finished the reflection turn without saying anything)"

    def post_reflection(assignment: Assignment, text: str) -> None:
        """Post the agent's own account of its reflection turn into the
        game's ledger (`POST /api/games/{game_id}/reflection`), so the
        post-game review's `reflectionOf` finds something rather than
        staying `null` forever (`ui/review_model.js`).

        This is the whole of what makes the reflection reach anywhere the
        game server can see: `prompt.reflection_turn` only ever told the
        agent to write markdown into its own memory directory, which the
        review page - and every other reader outside this container - has
        no way to open. `finalText` is what the agent actually said while
        doing that, over `POST /turn-done`, and it is the only account of
        the reflection this driver ever receives.

        Written after the game's result is already known - the reflection
        turn only runs once `turn_done` has confirmed a winner or a draw -
        so the route on the other end refuses anything filed before that,
        the same way `game_review` refuses to be read before it. Best
        effort, like `sync_agent_settings`: a game server that is briefly
        unreachable, or one old enough to answer 404, must not stop the
        agent retiring - the reflection is a nice to have, the retirement is
        not optional.
        """
        try:
            with make_client(settings.game_api) as client:
                response = client.post(
                    f"/api/games/{assignment.game_id}/reflection",
                    json={
                        "player": assignment.player,
                        "uid": assignment.uid,
                        "text": reflection_text(text),
                    },
                )
            if response.status_code >= 400:
                log.warning(
                    "game server refused the reflection for game %s: %s",
                    assignment.game_id,
                    response.status_code,
                )
        except Exception as exc:  # noqa: BLE001 - an unreachable game server must not block retire
            log.warning("could not post the reflection for game %s: %s", assignment.game_id, exc)

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
        if settings.exit_after_match:
            # A per-game Job plays exactly one match; once it is retired
            # there is no next dispatch to wait for, so the container should
            # exit rather than sit idle. `__main__` polls this flag and stops
            # uvicorn once it is set.
            app.state.should_exit = True
            log.info("match over, exiting")
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
                    if settings.exit_after_match:
                        # A per-game Job has no next dispatch to fall back to:
                        # an agent that never answers within `agent_boot_wait`
                        # means this match cannot be played at all, so the
                        # container should exit and fail visibly rather than
                        # idle until Kubernetes' own deadline finds it.
                        app.state.should_exit = True
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

    def seat_decklist(game_id: str, uid: str, player: str) -> str:
        """This seat's own decklist text.

        2b's T8 moved `decklist` off the game server's state view entirely -
        it was lobby-owned data (`store.decklists`) the api component read on
        every state poll, the hot reverse seam the six-way cut cannot carry -
        onto the lobby's own `GET /api/lobby/games/{id}/seats?uid=...`, which
        redacts every entry but the caller's own `uid` the same way
        `visibility._redact_player` used to redact this same field here.
        Pre-cut, that route lives on the same origin `settings.game_api`
        already points at.

        Best-effort: a failure reaching the lobby route (or a game with no
        decklists at all, `POST /api/games` builds a board directly) answers
        empty rather than aborting the dispatch - an empty mainboard satisfies
        no archetype's signature, so `GuideLibrary.identify` already answers
        the honest "no list to identify" rather than guessing a guide, and
        matchup-guide selection is not worth making the actual seat
        assignment newly fragile over.
        """
        try:
            with make_client(settings.game_api) as client:
                response = client.get(f"/api/lobby/games/{game_id}/seats", params={"uid": uid})
                if response.status_code >= 400:
                    return ""
                seats = response.json().get("seats") or []
        except httpx.HTTPError:
            return ""
        for seat in seats:
            if seat.get("player") == player:
                return seat.get("decklist") or ""
        return ""

    def opponent_of(state: dict, player: str) -> str:
        names = [s.get("username", "") for s in state.get("players") or []]
        others = [n for n in names if n and n != player]
        return others[0] if others else ""

    def assign(body: DispatchIn) -> dict:
        """The whole of a dispatch: claim the seat, learn its deck, write
        the briefing, and push turn 0. Used both by `POST /dispatch` and by
        the startup hook that self-dispatches a per-game Job's own match
        from `DRIVER_ASSIGNMENT_JSON` - one code path either way."""
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

        decklist = parse_decklist(seat_decklist(body.game_id, body.uid, body.player))
        identified = library.identify(decklist)
        # Read per dispatch, not once at startup: a compose driver is a
        # long-lived container over a bind-mounted checkout the operator can
        # pull under it between two games, and the revision that matters is
        # the one this game was played against.
        revision = guides_revision(settings.guides_root)
        assignment = Assignment(
            game_id=body.game_id,
            uid=body.uid,
            player=body.player,
            opponent=opponent_of(state, body.player),
            api_base=body.api_base or settings.game_api,
            decklist=decklist,
            identification=identified,
            guides_revision=revision,
            agent=body.agent,
            role=body.role,
        )

        settings.workspace.mkdir(parents=True, exist_ok=True)
        settings.memory_root.mkdir(parents=True, exist_ok=True)
        current_game_path = settings.workspace / "current-game.md"
        # The agent lives in another container and cannot open this file, so
        # turn 0 carries its contents; the path is kept for the driver's own
        # operators to read.
        briefing = render_current_game(assignment, settings.memory_root)
        current_game_path.write_text(briefing)
        # After the briefing, never before: `memory_index` lists every `*.md`
        # under the deck's memory directory as "what you have already
        # learned", and this game's own plan file is not a past game's note.
        seed_plan_file(assignment)

        # An advisor gets its own prose, never the seat's with caveats bolted
        # on: a buddy that is told to win the game will try to play it.
        write_turn_zero = advisor_turn_zero if assignment.role == "advisor" else turn_zero
        text = write_turn_zero(assignment, current_game_path, briefing)
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

        sync_agent_settings(body.agent)

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
                "deck": identified.guide.slug if identified.guide else None,
                "turnId": None,
                "confidence": identified.confidence,
                "archetype": identified.archetype,
                "candidates": [c.slug for c in identified.candidates],
                "pending": True,
                "reason": f"the agent is restarting; turn 0 will be pushed when it answers ({exc})",
            }

        with lock:
            if current["assignment"] is assignment:
                current.update(lastTurnId=turn_id, pending=False, turnActive=True)
        log.info(
            "dispatched game=%s player=%s vs=%s deck=%s (%s: %s, %d cards) turn=%s",
            body.game_id,
            body.player,
            assignment.opponent or "?",
            assignment.deck_slug,
            identified.confidence,
            ", ".join(f"{c.slug} {c.matched}/{c.clauses}" for c in identified.candidates)
            or "no candidate",
            decklist.total,
            turn_id,
        )
        return {
            "deck": identified.guide.slug if identified.guide else None,
            "turnId": turn_id,
            "confidence": identified.confidence,
            "archetype": identified.archetype,
            "candidates": [c.slug for c in identified.candidates],
            "pending": False,
        }

    @app.post("/dispatch", status_code=202)
    def dispatch(body: DispatchIn) -> dict:
        return assign(body)

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
            # The notes are written, on disk, in this container only. Post
            # what the agent said while writing them into the game's own
            # ledger before it is too late to say anything at all - then
            # stop the agent, forget the game, and let the restart policy
            # give the next dispatch a container with no memory of this one.
            post_reflection(assignment, str(record.get("finalText") or ""))
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

    @app.post("/release", status_code=202)
    def release(body: ReleaseIn) -> dict:
        """Give the agent back before its game ended.

        The lobby starting a new game abandons the old one, and without this
        the driver kept the old assignment: every dispatch for the new game
        got the 409 "this agent is already playing game <old>", so the human's
        second game had no opponent at all until the container was restarted
        by hand.

        It runs the same `retire` the end of a match runs - the assignment is
        dropped and the agent's session ends, so the next dispatch starts on a
        fresh conversation instead of one full of the abandoned game. Nothing
        under `MEMORY_ROOT` is touched: what the continuous agent learned is
        on a mounted volume that neither this endpoint nor `retire` writes to,
        and it is still there for the next match. The one thing lost is the
        reflection turn, because the game was abandoned rather than played to
        an outcome - said here, in the log, rather than left to be inferred.
        """
        with lock:
            assignment = current["assignment"]
        if assignment is None:
            return {"released": False, "reason": "this agent has no assignment"}
        if body.uid and assignment.uid != body.uid:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"this agent is playing game {assignment.game_id} as "
                    f"{assignment.player}, not the seat you named"
                ),
            )
        if body.game_id and assignment.game_id != body.game_id:
            raise HTTPException(
                status_code=409,
                detail=f"this agent is playing game {assignment.game_id}, not {body.game_id}",
            )
        log.info(
            "releasing game %s (%s) without a reflection turn: %s",
            assignment.game_id,
            assignment.player,
            body.reason or "no reason given",
        )
        stopped = retire(assignment)
        return {
            "released": True,
            "gameId": assignment.game_id,
            "player": assignment.player,
            "agentStopped": stopped,
            "reflected": False,
        }

    def agent_activity() -> dict[str, Any] | None:
        """What the agent is doing right now, read off the wrapper.

        The agent's own listener is not reachable from outside the pod, and
        `/status` below is already what the game server polls for this seat -
        so this is where the wrapper's coarse activity word crosses over on
        its way to the play UI's AI pill.

        Never raises, and bounded by `ACTIVITY_READ_TIMEOUT`: an unreachable
        agent, a wrapper old enough to 404 the route, a non-JSON body and an
        `activity` that is not an object all come back as `None`, which the
        UI reads as "fall back to the plain state word".
        """
        try:
            with make_client(settings.wrapper_api) as client:
                # Set on the client rather than passed as a per-request
                # `timeout=`: the two are the same thing for a real httpx
                # client, and `TestClient` (what the driver tests inject
                # here) warns about the keyword argument.
                client.timeout = httpx.Timeout(ACTIVITY_READ_TIMEOUT)
                response = client.get("/v1/session", headers=wrapper_headers())
        except Exception as exc:  # noqa: BLE001 - a status read never fails
            log.debug("could not read the agent's activity: %s", exc)
            return None
        if response.status_code >= 400:
            return None
        try:
            answer = response.json()
        except ValueError:
            return None
        if not isinstance(answer, dict):
            return None
        activity = answer.get("activity")
        return activity if isinstance(activity, dict) else None

    @app.get("/status")
    def status() -> dict:
        # Outside the lock: it is an HTTP round trip, and nothing below
        # depends on it being consistent with the snapshot taken under the
        # lock - it is one coarse word about the agent, not driver state.
        activity = agent_activity()
        with lock:
            assignment = current["assignment"]
            return {
                "agentActivity": activity,
                "gameId": assignment.game_id if assignment else None,
                "player": assignment.player if assignment else None,
                "deck": (assignment.guide.slug if assignment and assignment.guide else None),
                "agent": assignment.agent if assignment else None,
                "lastTurnId": current["lastTurnId"],
                "reflected": current["reflected"],
                "pending": current["pending"],
                "turnActive": current["turnActive"],
                "lastError": current["lastError"],
            }

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """A per-game Job carries its own match in `DRIVER_ASSIGNMENT_JSON`
        rather than waiting for a caller to `POST /dispatch` it, so it
        self-dispatches here, on startup, through the same `assign` the
        route uses. `assign` is synchronous (it blocks on `httpx.Client`
        calls), so it runs in a worker thread rather than the event loop -
        `to_thread` rather than a bare call, so a slow game server or agent
        does not stall the ASGI startup phase itself.

        Every failure - a body that will not parse as JSON, one that parses
        but fails validation, the game server being unreachable, the agent
        refusing turn 0 outright - is caught here, broadly, logged, and
        recorded on `/status` rather than raised: raising here would keep the
        container from ever starting, which is worse than a container that
        starts idle and reports why. For a per-game Job (`exit_after_match`)
        a self-dispatch that cannot even begin is terminal - there is no
        caller left to retry it - so the container is told to exit rather
        than sit idle until Kubernetes' own deadline finds it."""
        if settings.assignment_json:
            try:
                body = DispatchIn.model_validate_json(settings.assignment_json)
                await asyncio.to_thread(assign, body)
            except Exception as exc:  # noqa: BLE001 - any failure here is terminal
                log.error("self-dispatch failed: %s", exc)
                with lock:
                    current["lastError"] = str(exc)
                if settings.exit_after_match:
                    app.state.should_exit = True
        yield

    app.router.lifespan_context = lifespan

    return app
