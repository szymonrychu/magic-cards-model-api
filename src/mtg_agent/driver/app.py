"""Choreography: who is playing what, and which turn to push next.

The driver holds no game state. It learns of a game when ai_seat.py hands it
a dispatched seat, reads that seat's decklist from the game server, works out
which guided deck it is, writes `current-game.md`, and pushes turn 0. When a
turn finishes it asks the game server whether the game ended; if it did, it
pushes exactly one reflection turn, files it and the post-game AI review
(`post_ai_review`, `review.py` - a separate, Opus-generated critique, best
effort and never blocking) once that reflection completes, then the handover
turn.

One agent container plays exactly one match. Once the handover is in (see
`start_handover`), the driver sends `DELETE /v1/session` and forgets the
assignment: the agent
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
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.playscripts.report import script_run_table

from .config import DriverSettings
from .guides import DeckGuide, GuideLibrary, ScriptsRegistration, parse_decklist
from .handover import (
    HANDOVER_BACKOFF_MAX_SECONDS,
    HANDOVER_BACKOFF_START_SECONDS,
    HANDOVER_BUDGET_SECONDS,
    HANDOVER_PUSH_BUDGET_SECONDS,
    MARKER_NAME,
    NUDGE_AFTER_SECONDS,
    POLL_SECONDS,
    clear_marker,
    conflict_warnings,
    write_marker,
)
from .prompt import (
    Assignment,
    advisor_turn_zero,
    handover_nudge,
    handover_turn,
    plan_header,
    plan_path,
    reflection_turn,
    render_current_game,
    turn_zero,
)
from .review import review_prompt
from .revision import guides_revision

log = logging.getLogger("driver")

#: Headroom over the wrapper's own `Settings.review_timeout` default (600s,
#: `oneshot.run_review`'s bound on the subprocess itself) - the driver's own
#: client timeout for `POST /v1/review` must outlast it, or a review that
#: was about to succeed reads here as an unreachable wrapper instead.
REVIEW_HTTP_TIMEOUT = 650.0

#: How long `retire` waits for the AI review's background thread
#: (`post_ai_review`, spawned by `turn_done`'s reflection branch) before
#: giving up on it and proceeding anyway - `DELETE /v1/session` tears the
#: agent container down, and `exit_after_match` can end this very process
#: right after, so a review still in flight when either happens is lost for
#: good rather than merely late. Bounded by `REVIEW_HTTP_TIMEOUT` (the
#: review's own slow call, `POST /v1/review`) plus headroom for the two
#: quick calls around it (the review-record fetch, the `ai-review` post,
#: each on `make_client`'s ordinary 30s timeout) - `post_ai_review` itself
#: never runs longer than this in practice, so the join is expected to
#: return well before its own timeout on every call that succeeds, and to
#: hit it only on the same failures that would have made the review
#: unreachable anyway.
RETIRE_REVIEW_JOIN_TIMEOUT = REVIEW_HTTP_TIMEOUT + 70.0

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


class HandoverIn(BaseModel):
    """`outcome` names how the seat's game ended for the handover turn. Left
    empty, the driver reads the game: a winner or a draw if there is one,
    otherwise `killed` - which is what the Job's preStop sends."""

    outcome: str = ""


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

    def __init__(self, detail: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(detail)
        self.retryable = retryable
        #: The agent's HTTP status, or None when nothing answered.
        self.status = status


def build_app(
    settings: DriverSettings,
    *,
    client_factory: Callable[[str], httpx.Client] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> FastAPI:
    """`clock` is the monotonic clock the handover's nudge and budget read;
    `sleep` is how the handover waits between pushes the agent refused."""
    app = FastAPI(title="mtg-agent-driver")
    now = clock or time.monotonic
    pause = sleep or time.sleep
    marker_path = settings.workspace / MARKER_NAME
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
        # How the game ended, kept from the reflection push for the handover.
        "outcome": None,
        # The running handover for this assignment (`start_handover`), and the
        # latest one this driver ran, which a preStop after retirement joins.
        "handover": None,
        "lastHandover": None,
        # Set, under `lock`, the first time `turn_done` starts processing the
        # reflection turn's own completion - before `post_reflection` or the
        # background review are ever started, so a retried or genuinely
        # concurrent duplicate delivery of that SAME callback (`webhook.
        # Deliverer` retries a slow response) sees it already True and skips
        # straight to answering with the current handover status, rather than
        # filing a second reflection or spawning a second review thread.
        "reflectionHandled": False,
        # The AI review's own background `threading.Thread` (`post_ai_
        # review`), once `turn_done` has spawned one - `retire` joins it,
        # bounded by `RETIRE_REVIEW_JOIN_TIMEOUT`, before it tears the
        # session down or exits the process, so the review is never merely
        # racing the container's own shutdown. `None` until a reflection
        # actually completes and spawns one; reset by `clear_locked` like
        # everything else assignment-scoped, once `retire` has already
        # joined whatever it named.
        "reviewThread": None,
    }

    def clear_locked() -> None:
        current.update(
            assignment=None,
            lastTurnId=None,
            reflectionTurnId=None,
            reflected=False,
            pending=False,
            turnActive=False,
            outcome=None,
            handover=None,
            reflectionHandled=False,
            reviewThread=None,
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
                status=response.status_code,
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

    def post_ai_review(assignment: Assignment, reflection_text: str) -> None:
        """Generate and file the post-game AI review - the handoff's other
        half, run on Opus (`AgentSettings.review_default`, fixed, never this
        seat's own play settings) from `reflection_text` (the just-posted
        reflection) plus the game's own review record. See
        `mtg_agent.driver.review`'s module docstring for the full reasoning.

        Best-effort throughout, exactly like `post_reflection` beside it:
        nothing here may block or fail the handover that always follows it.
        Three calls, each independently allowed to fail without touching the
        others already having happened - fetch the review record, ask the
        wrapper to generate the review, file the result."""
        try:
            with make_client(settings.game_api) as client:
                response = client.get(
                    f"/api/games/{assignment.game_id}/review", params={"uid": assignment.uid}
                )
            if response.status_code >= 400:
                log.warning(
                    "could not read the review record for game %s: %s",
                    assignment.game_id,
                    response.status_code,
                )
                return
            review_record = response.json()
        except Exception as exc:  # noqa: BLE001 - an unreachable game server must not block retire
            log.warning("could not read the review record for game %s: %s", assignment.game_id, exc)
            return

        try:
            prompt = review_prompt(assignment, reflection_text, review_record)
        except Exception as exc:  # noqa: BLE001 - a malformed review record must not block retire
            log.warning(
                "could not build the AI review prompt for game %s: %s", assignment.game_id, exc
            )
            return
        review_settings = AgentSettings.review_default()
        try:
            with make_client(settings.wrapper_api) as client:
                response = client.post(
                    "/v1/review",
                    json={
                        "prompt": prompt,
                        "model": review_settings.model,
                        "effort": review_settings.effort,
                    },
                    headers=wrapper_headers(),
                    timeout=REVIEW_HTTP_TIMEOUT,
                )
            if response.status_code >= 400:
                log.warning(
                    "the wrapper could not generate an AI review for game %s: %s",
                    assignment.game_id,
                    response.status_code,
                )
                return
            review_text = str(response.json().get("text") or "").strip()
        except Exception as exc:  # noqa: BLE001 - an unreachable/slow wrapper must not block retire
            log.warning("could not generate the AI review for game %s: %s", assignment.game_id, exc)
            return
        if not review_text:
            log.warning(
                "the AI review for game %s came back with no text; not filing it",
                assignment.game_id,
            )
            return

        try:
            with make_client(settings.game_api) as client:
                response = client.post(
                    f"/api/games/{assignment.game_id}/ai-review",
                    json={
                        "player": assignment.player,
                        "uid": assignment.uid,
                        "text": review_text,
                    },
                )
            if response.status_code >= 400:
                log.warning(
                    "game server refused the AI review for game %s: %s",
                    assignment.game_id,
                    response.status_code,
                )
        except Exception as exc:  # noqa: BLE001 - an unreachable game server must not block retire
            log.warning("could not post the AI review for game %s: %s", assignment.game_id, exc)

    def retire(assignment: Assignment) -> bool:
        """End the agent's life. `DELETE /v1/session` makes it exit, its
        restart policy gives the next match a fresh container, and we drop
        every trace of this game so the next dispatch starts clean.

        A failure here is logged and swallowed: an agent we cannot reach is
        already gone, and refusing to forget the finished game would wedge
        the driver against every future dispatch.

        **Joins the AI review's background thread first, bounded by
        `RETIRE_REVIEW_JOIN_TIMEOUT`.** The handover itself already ran to
        completion by the time anything calls `retire` (`run_handover`'s own
        last step) - nobody waits on the review to see the handover marker
        or its own turn complete. But `DELETE /v1/session` tears the agent
        container down, and `exit_after_match` can end this very process
        right after - a handover typically finishes in 20-30s, while an
        Opus review over a whole game routinely takes longer, so without
        this join the review was lost for good on essentially every real
        exit, not merely a slow one. Still best-effort throughout: a review
        that fails or does not finish within the bound is logged and
        retirement proceeds anyway, exactly as it always has for every
        other way `post_ai_review` can fail."""
        with lock:
            review_thread = current.get("reviewThread")
        if review_thread is not None and review_thread.is_alive():
            log.info(
                "match %s retiring: waiting on the AI review (up to %.0fs) before the session ends",
                assignment.game_id,
                RETIRE_REVIEW_JOIN_TIMEOUT,
            )
            review_thread.join(timeout=RETIRE_REVIEW_JOIN_TIMEOUT)
            if review_thread.is_alive():
                log.warning(
                    "match %s retiring: the AI review did not finish within %.0fs, "
                    "continuing without it",
                    assignment.game_id,
                    RETIRE_REVIEW_JOIN_TIMEOUT,
                )
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

    def ended_outcome(assignment: Assignment) -> str | None:
        """How the game ended, when it has; None while it runs or when the
        game server does not answer."""
        try:
            state = game_state(assignment.game_id, assignment.uid)
        except httpx.HTTPError as exc:
            log.warning("could not read game %s for the handover: %s", assignment.game_id, exc)
            return None
        winner = state.get("winner")
        if winner:
            return f"{winner} won"
        return "the game was a draw" if state.get("drawn") else None

    def script_runs(assignment: Assignment) -> str:
        """`script_run_table` for this game and seat, from the log the dispatched
        uid reads (an advisor uid resolves to its human, so a buddy reads the
        same rows). Read as the handover starts: a lobby that released the seat
        deletes the game right after."""
        try:
            with make_client(settings.game_api) as client:
                response = client.get(
                    f"/api/games/{assignment.game_id}/log", params={"uid": assignment.uid}
                )
            if response.status_code >= 400:
                return f"no script run table: the game server answered {response.status_code}"
            rows = response.json().get("log") or []
        except Exception as exc:  # noqa: BLE001 - a missing table never stops a handover
            return f"no script run table: the game server did not answer ({exc})"
        return script_run_table(rows, assignment.game_id, assignment.player)

    def wrapper_post(path: str, body: dict | None = None) -> bool:
        """POST a best-effort control message to the wrapper; False if it failed."""
        try:
            with make_client(settings.wrapper_api) as client:
                response = client.post(path, json=body, headers=wrapper_headers())
        except Exception as exc:  # noqa: BLE001 - a control message never fails a handover
            log.warning("could not reach the agent at %s: %s", path, exc)
            return False
        if response.status_code >= 400:
            log.warning("the agent refused %s: %s", path, response.status_code)
            return False
        return True

    def push_handover(assignment: Assignment, text: str, started: float) -> str:
        """Push the handover turn. Only a 409 - the interrupted turn still
        draining - is waited out, backing off, for `HANDOVER_PUSH_BUDGET_SECONDS`;
        past that, and on an agent that is gone or refuses, this raises, and
        the handover fails with the reason in its marker."""
        wait = HANDOVER_BACKOFF_START_SECONDS
        backing_off = False
        while True:
            try:
                return push_turn(text)
            except AgentUnavailable as exc:
                if exc.status != 409:
                    raise
            elapsed = now() - started
            if elapsed >= HANDOVER_PUSH_BUDGET_SECONDS:
                raise RuntimeError(
                    f"the agent still answered 409 {elapsed:.0f} s after the handover began: "
                    "its interrupted turn never ended, so the handover turn was never pushed"
                )
            if not backing_off:
                backing_off = True
                log.warning(
                    "handover for game %s: the agent is still busy with the interrupted turn, "
                    "backing off for up to %.0f s",
                    assignment.game_id,
                    HANDOVER_PUSH_BUDGET_SECONDS,
                )
            pause(min(wait, HANDOVER_PUSH_BUDGET_SECONDS - elapsed))
            wait = min(wait * 2, HANDOVER_BACKOFF_MAX_SECONDS)

    def await_handover_turn(assignment: Assignment, handover: dict, started: float) -> str:
        """`done` once the turn's `/turn-done` lands, `timeout` at the budget.
        One "push now" nudge goes out at `NUDGE_AFTER_SECONDS`, through
        `/v1/probe`, the one wrapper route that writes into a busy turn."""
        nudged = False
        while not handover["done"].wait(POLL_SECONDS):
            elapsed = now() - started
            if elapsed >= HANDOVER_BUDGET_SECONDS:
                return "timeout"
            if elapsed >= NUDGE_AFTER_SECONDS and not nudged:
                nudged = True
                with lock:
                    handover["status"]["nudged"] = True
                wrapper_post("/v1/probe", {"text": handover_nudge(assignment)})
        return "done"

    def run_handover(
        assignment: Assignment,
        handover: dict,
        prepared: str | Exception,
        interrupt: bool,
        started: float,
    ) -> None:
        """The handover's background half: interrupt, push, wait, then write the
        marker whatever happened, and only then retire."""
        state, error = "failed", None
        try:
            if isinstance(prepared, Exception):
                raise prepared
            if interrupt:
                interrupted = wrapper_post("/v1/interrupt")
                with lock:
                    handover["status"]["interrupted"] = interrupted
            turn_id = push_handover(assignment, prepared, started)
            with lock:
                handover["turnId"] = turn_id
                handover["status"]["turnId"] = turn_id
                if turn_id in handover["finished"]:
                    handover["done"].set()
                if current["assignment"] is assignment:
                    current.update(lastTurnId=turn_id, turnActive=not handover["done"].is_set())
            state = await_handover_turn(assignment, handover, started)
        except Exception as exc:  # noqa: BLE001 - every failure still writes the marker
            state, error = "failed", str(exc)
            log.error("handover for game %s failed: %s", assignment.game_id, exc)
        with lock:
            handover["status"].update(state=state, error=error)
            status = dict(handover["status"])
        try:
            write_marker(marker_path, status)
        except OSError as exc:
            log.error("could not write the handover marker %s: %s", marker_path, exc)
        log.info("handover %s for game %s: %s", status["handoverId"], assignment.game_id, state)
        with lock:
            ours = current["assignment"] is assignment
        if ours:
            retire(assignment)

    def start_handover(outcome: str = "") -> dict[str, Any]:
        """Start this assignment's handover, or join the one already started.

        The first call claims it under the lock, so concurrent callers - the
        release, a natural end, the Job's preStop - all get the same
        `handoverId` and exactly one handover turn is pushed; a later call
        gets its current status. The outcome, the run table and the conflict
        warnings are read here, before the answer; the turn itself runs in the
        background (`run_handover`), bounded by `HANDOVER_BUDGET_SECONDS`.

        With nothing assigned there is nothing to hand over: the latest
        handover's status is answered if there was one, otherwise an `idle`
        status is written as the marker at once, so a waiting preStop returns.
        """
        with lock:
            active = current["handover"]
            if active is not None:
                return dict(active["status"])
            assignment = current["assignment"]
            if assignment is None and current["lastHandover"] is not None:
                return dict(current["lastHandover"]["status"])
            started = now()
            handover: dict[str, Any] = {
                "status": {
                    "handoverId": uuid.uuid4().hex,
                    "state": "starting" if assignment is not None else "idle",
                    "outcome": outcome or None,
                    "gameId": assignment.game_id if assignment is not None else None,
                    "player": assignment.player if assignment is not None else None,
                    "role": assignment.role if assignment is not None else None,
                    "turnId": None,
                    "interrupted": False,
                    "nudged": False,
                    "error": None,
                    "marker": str(marker_path),
                },
                "done": threading.Event(),
                "turnId": None,
                # Turns that finished before the handover turn's id was known.
                "finished": set(),
            }
            current["lastHandover"] = handover
            if assignment is not None:
                current["handover"] = handover
            interrupt = bool(current["turnActive"])
            status = dict(handover["status"])
        if assignment is None:
            try:
                write_marker(marker_path, status)
            except OSError as exc:
                log.error("could not write the handover marker %s: %s", marker_path, exc)
            return status

        clear_marker(marker_path)
        prepared: str | Exception
        try:
            resolved = outcome or ended_outcome(assignment) or "killed"
            with lock:
                handover["status"].update(outcome=resolved, state="running")
            prepared = handover_turn(
                assignment,
                resolved,
                script_runs(assignment),
                conflict_warnings(assignment.scripts, assignment.deck_slug),
            )
        except Exception as exc:  # noqa: BLE001 - the background half records it
            prepared = exc
        log.info("handing over game %s (%s)", assignment.game_id, assignment.player)
        threading.Thread(
            target=run_handover,
            args=(assignment, handover, prepared, interrupt, started),
            daemon=True,
            name="driver-handover",
        ).start()
        with lock:
            return dict(handover["status"])

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

    def register_plays(
        game_id: str, uid: str, guide: DeckGuide | None, revision: str
    ) -> ScriptsRegistration:
        """Register the deck's play scripts under `uid` - the seat's own, or
        the buddy's advisor uid - with `PUT /api/games/{id}/seats/{uid}/
        scripts`, once, at dispatch. Nothing registers mid-game: the corpus
        a seat plays is the one its guides revision carried when it sat down.

        Never raises. A `scripts_disabled` refusal is the operator's kill
        switch and the seat plays without scripts; any other refusal, or a
        game server that does not answer, costs the seat its scripts the same
        way. Either is said in the briefing, and neither fails the dispatch -
        scripts are a help to the seat, the game is the point.
        """
        if guide is None:
            return ScriptsRegistration("no_deck")
        corpus = guide.plays()
        for notice in corpus.notices:
            log.warning(
                "game %s: skipped play file %s (%s): %s",
                game_id,
                notice.path,
                notice.code,
                notice.message,
            )
        body = {
            "deck_slug": guide.slug,
            "guides_revision": revision,
            "scripts": list(corpus.scripts),
            "inputs": list(corpus.inputs),
        }
        try:
            with make_client(settings.game_api) as client:
                response = client.put(
                    f"/api/games/{game_id}/seats/{uid}/scripts", params={"uid": uid}, json=body
                )
        except Exception as exc:  # noqa: BLE001 - scripts never fail a dispatch
            log.warning("could not register play scripts for game %s: %s", game_id, exc)
            return ScriptsRegistration("failed", corpus, f"the game server did not answer: {exc}")
        if response.status_code < 400:
            log.info(
                "registered %d play script(s) and %d declared input(s) for game %s uid %s",
                len(corpus.scripts),
                len(corpus.inputs),
                game_id,
                uid,
            )
            return ScriptsRegistration("registered", corpus)
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        code = answer.get("code") if isinstance(answer, dict) else None
        if code == "scripts_disabled":
            log.info("play scripts are disabled on the game server; game %s plays without", game_id)
            return ScriptsRegistration("disabled", corpus)
        detail = f"the game server answered {response.status_code}" + (f" {code}" if code else "")
        log.warning("play scripts were not registered for game %s: %s", game_id, detail)
        return ScriptsRegistration("failed", corpus, detail)

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
        scripts = register_plays(body.game_id, body.uid, identified.guide, revision)
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
            scripts=scripts,
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
                outcome=None,
                handover=None,
                lastHandover=None,
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
        finished = record.get("turnId")
        with lock:
            assignment = current["assignment"]
            reflected = current["reflected"]
            reflection_id = current["reflectionTurnId"]
            active = current["handover"]
            if active is None:
                # The turn the agent just finished is no longer in flight. Set
                # before anything else here, so a reflection push below can
                # turn it back on and a game that is still running reports the
                # truth: nothing is playing this seat at the moment.
                current["turnActive"] = False
            elif active["turnId"] is not None and finished == active["turnId"]:
                current["turnActive"] = False
                active["done"].set()
            elif active["turnId"] is None and finished:
                active["finished"].add(finished)
            handing_over = dict(active["status"]) if active is not None else None
        if assignment is None:
            return {"ignored": "no assignment"}
        if handing_over is not None:
            # Every turn ending during a handover - the interrupted one, the
            # reflection, the handover turn itself - belongs to it.
            return {"handover": handing_over}
        if reflection_id is not None and finished == reflection_id:
            # The notes are written, on disk, in this container only. Post
            # what the agent said while writing them into the game's own
            # ledger, then hand over immediately - the handover retires the
            # agent once its marker is written, and must not wait on the AI
            # review below, which can run for minutes.
            #
            # `reflectionHandled` dedupes this whole branch: `webhook.
            # Deliverer` retries a `/turn-done` delivery (its own 10s client
            # timeout, up to 3 attempts) whenever the driver does not answer
            # within it, and inline review generation used to blow through
            # that easily - three redundant deliveries of the SAME callback
            # used to file the reflection three times, generate the review
            # three times, and delay the handover by however long the
            # slowest of them took. The flag is claimed under `lock` before
            # any of that work starts, so a retried - or genuinely
            # concurrent, not merely sequential - duplicate delivery sees it
            # already set and skips straight to `start_handover`, which is
            # itself idempotent (`start_handover`'s own docstring: "the
            # first call claims it ... a later call gets its current
            # status") and so safe to call on every delivery, duplicate or
            # not.
            reflection_text_value = str(record.get("finalText") or "")
            with lock:
                already_handled = current["reflectionHandled"]
                current["reflectionHandled"] = True
            if not already_handled:
                post_reflection(assignment, reflection_text_value)
            with lock:
                ended = current["outcome"] or ""
            handover_status = start_handover(ended)
            if not already_handled:
                review_thread = threading.Thread(
                    target=post_ai_review,
                    args=(assignment, reflection_text_value),
                    daemon=True,
                )
                review_thread.start()
                with lock:
                    current["reviewThread"] = review_thread
            return {
                "handover": handover_status,
                "gameId": assignment.game_id,
                "reflected": True,
            }
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
            current.update(
                reflected=True, reflectionTurnId=turn_id, turnActive=True, outcome=outcome
            )
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

        It does not end the agent's session itself. It starts the handover
        with outcome `released` (`start_handover`), which interrupts the turn
        in flight and has the agent land what it learned; once the marker is
        written, the handover runs the same `retire` the end of a match runs.
        The answer is immediate and the handover runs behind it, bounded by
        `HANDOVER_BUDGET_SECONDS`. Nothing under `MEMORY_ROOT` is touched. The
        one thing skipped is the reflection turn, because the game was
        abandoned rather than played to an outcome.
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
            "releasing game %s (%s) without a reflection turn, handing over first: %s",
            assignment.game_id,
            assignment.player,
            body.reason or "no reason given",
        )
        return {
            "released": True,
            "gameId": assignment.game_id,
            "player": assignment.player,
            "handover": start_handover("released"),
            "reflected": False,
        }

    @app.post("/handover", status_code=202)
    def handover_route(body: HandoverIn) -> dict:
        """Start the handover, or join the one running; see `start_handover`."""
        return start_handover(body.outcome)

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
