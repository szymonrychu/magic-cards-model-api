"""Choreography: who is playing what, and which turn to push next.

The driver holds no game state. It learns of a game when ai_seat.py hands it
a dispatched seat, reads that seat's decklist from the game server, works out
which guided deck it is, writes `current-game.md`, and pushes turn 0. When a
turn finishes it asks the game server whether the game ended; if it did, it
pushes exactly one reflection turn, files it and the post-game AI review
(`post_ai_review`, `review.py` - a separate, Opus-generated critique, best
effort and never blocking) once that reflection completes, then the handover
turn.

**The review is not only the reflection turn's.** A game reaches a terminal
state several ways and only one of them is a completed reflection; the
others - a release, a preStop handover, an agent that could not be given a
reflection turn at all - used to file nothing, so the review page rendered
no `section.ai-review` whatsoever. `TERMINAL_PATHS` names each one and what
it does, `consider_ai_review` is the single place that decides, and `retire`
calls it as a funnel so a path added later reaches the decision even if
nobody wires it up. `UNREACHABLE_TERMINAL_PATH` names the one case this
process cannot act on at all, rather than leaving it unsaid.

**The review and the handover never touch the /guides checkout at the same
time (defect D-1).** The review runs on a background thread while the
handover turn is pushed into the interactive session, and the handover
turn's own step 2 is `mcp-seat plays admit` - so the two really can arrive
together, and a second committer inside one clone corrupts a
fetch-and-rebase rather than merely losing a race. The guarantee is a real
mutual-exclusion lock on the checkout itself
(`playscripts.gitwork.checkout_lock`), which `plays admit` and
`guides propose` both take for the whole of their git phase and which the
reviewer's own write channel takes too once it has one. A file lock, not an
in-process one, because the writers are separate processes in one container.

Choosing the lock rather than joining the review before the handover is what
KEEPS `post_ai_review`'s "best effort, never blocking retire" property: the
review stays on its daemon thread, nothing waits on it to hand over, and
`retire` still joins it once, bounded by `RETIRE_REVIEW_JOIN_TIMEOUT`, so a
slow review is never merely abandoned either.

One agent container plays exactly one match. Once the handover is in (see
`start_handover`), the driver sends `DELETE /v1/session` and forgets the
assignment: the agent
process exits, its restart policy brings up a fresh container with a fresh
conversation, and the next game starts from the notes on disk rather than
from a session that remembers the last one. The driver itself is not
recycled - it is the thing that survives to dispatch the next match.

**A follow-up is not a match (F1).** When a handover's `plays_outcome` row
says `unfinished`, the api schedules one follow-up Job whose assignment says
`mode: "followup"`. Such a driver pushes no turn 0 and plays nothing: it
pushes one `prompt.followup_turn`, waits on it for `FOLLOWUP_BUDGET_SECONDS`
with one nudge, reads the results journal, files its own `plays_outcome`
with `followup: true` (which the api never follows up again), and retires.
The turn runs as the handover-kind work it is, so `turn_done` routes its
completion to it and the seat-never-idle continuation never sees it; a
`/handover` or `/release` while it runs - its Job deleted because the game
was ended or forgotten - is the hard stop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.playscripts.report import (
    DevelopmentCandidates,
    development_candidates,
    evidence_record,
    evidence_table,
    hand_played_summary,
    offer_table,
    script_run_table,
)

from .config import DriverSettings
from .guides import (
    CONFIDENT,
    DeckGuide,
    GuideLibrary,
    Identification,
    ScriptsRegistration,
    parse_decklist,
)
from .handover import (
    HANDOVER_BACKOFF_MAX_SECONDS,
    HANDOVER_BACKOFF_START_SECONDS,
    HANDOVER_BUDGET_SECONDS,
    HANDOVER_PUSH_BUDGET_SECONDS,
    MARKER_NAME,
    NUDGE_AFTER_SECONDS,
    POLL_SECONDS,
    assess,
    clear_marker,
    conflict_warnings,
    plays_outcome_body,
    read_journal,
    review_answered_plays,
    unfinished_reasons,
    write_evidence,
    write_marker,
)
from .prompt import (
    FOLLOWUP_BUDGET_SECONDS,
    FOLLOWUP_NUDGE_AFTER_SECONDS,
    Assignment,
    advisor_turn_zero,
    continue_turn,
    followup_nudge,
    followup_turn,
    handover_nudge,
    handover_repush,
    handover_turn,
    plan_header,
    plan_path,
    reflection_turn,
    render_current_game,
    turn_zero,
)
from .review import play_statistics_block, review_prompt
from .revision import guides_revision

log = logging.getLogger("driver")

#: Headroom over the wrapper's own `Settings.review_timeout` default
#: (3600s, DR3 - up from 1200, `oneshot.run_review`'s bound on the
#: subprocess itself) - the driver's own client timeout for `POST
#: /v1/review` must outlast it, or a review that was about to succeed reads
#: here as an unreachable wrapper instead.
#:
#: It moved with that bound rather than being re-chosen: the review is a
#: tool-using call now (it edits a deck's guide and takes the change to
#: mtg-decks through `mcp-seat`, waiting on the `/guides` checkout lock
#: first), so this has to sit above whatever the wrapper allows itself.
#: `tests/wrapper/test_oneshot.py` pins the ordering against
#: `Settings.review_timeout` itself, so neither number can be moved alone.
REVIEW_HTTP_TIMEOUT = 3660.0

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
#:
#: Derived, so it follows `REVIEW_HTTP_TIMEOUT` up without a second edit.
#:
#: **DR3: this now exceeds the driver container's own preStop wait**
#: (`handover.PRESTOP_WAIT_SECONDS`, 1740s, mirrored at
#: `deploy/seat-job.yaml`) - past that the kubelet cuts the join short
#: anyway and the bound stops meaning anything for a review still running
#: that late. Raising `handover.py`'s `PRESTOP_WAIT_SECONDS` and the
#: deploy manifest's own preStop wait to match is out of this task's own
#: file scope (`handover.py` is imported here, never edited - see this
#: module's own docstring); until that follow-up lands, a review that
#: genuinely needs the whole of the new, longer `REVIEW_HTTP_TIMEOUT` can
#: still be cut short by the kubelet before `retire`'s own join here would
#: have given up on it.
RETIRE_REVIEW_JOIN_TIMEOUT = REVIEW_HTTP_TIMEOUT + 70.0

#: The auto-continue loop guard. One wrapper turn is meant to cover a whole
#: match, so a turn that ends while the game runs is followed at once by a
#: continuation turn (`continue_turn`). A continuation is "quick" - it did no
#: real play - when the board did not move at all while it ran (`board_mark`)
#: AND it ended within `CONTINUE_QUICK_SECONDS` AND either the game was
#: waiting on this very seat, or it ended within `CONTINUE_INSTANT_SECONDS`
#: (too soon to have run even one wake-loop wait - `wait_for_priority` holds
#: for up to 20s). A seat or buddy that waited and found the other side still
#: thinking is not looping, however often it happens. `CONTINUE_QUICK_LIMIT`
#: quick ones in a row means the agent ends every turn the moment it starts
#: (a broken session, a prompt it keeps refusing), and pushing more would
#: spin forever - so the driver stops, logs an error and files a problem
#: report instead. Any continuation that is not quick resets the count.
CONTINUE_QUICK_SECONDS = 30.0
CONTINUE_INSTANT_SECONDS = 5.0
CONTINUE_QUICK_LIMIT = 3

#: How long `turn_done` waits before checking a game it could not read once
#: more. One retry, bounded: a game server restarting is back well inside
#: this, and one that is not is reported through `lastError` rather than
#: leaving the seat idle with nobody told.
UNREACHABLE_RETRY_SECONDS = 5.0

#: How the wrapper words a failed turn when the agent itself is gone
#: (`wrapper.session.Manager._finish_failed`): claude exited, reported a fatal
#: error (auth, quota), or its PTY stopped taking input. Pushing a
#: continuation at those cannot help; they keep the pre-continuation
#: handling, a `lastError` the game server reads as an error.
AGENT_GONE_ERRORS = ("claude exited", "claude reported a fatal error", "write to pty failed")

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
    mode: Literal["play", "followup"] = "play"
    """`play` (a match: turn 0, the game, the reflection, the handover) or
    `followup` - F1's one bounded DSL-development turn for a handover that
    ended unfinished (`assign_followup`). Defaulted, and written by
    `k8s_seats` only on a follow-up Job, so every other payload reads as a
    match, unchanged."""
    followup: dict | None = None
    """A follow-up's payload, built by the api from the ledger when it
    scheduled the Job: `deck`, `decklist`, `opponent`, `opponent_decklist`,
    `opponent_identification`, `outcome`, `run_table`, `offers`,
    `hand_played`, `candidates` (`{plays, lines}`) and
    `unfinished_reasons`."""


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


#: Every way a game this driver is holding reaches a terminal state, by
#: name, with what each one does about the post-game AI review. "All paths"
#: is not a description a reader can check; this is (T34, clause a).
#:
#: The list is closed by construction rather than by vigilance: every entry
#: below ends in `retire`, and `retire` itself calls `consider_ai_review` as
#: its last-resort funnel, so a terminal path added later that drops the
#: assignment reaches the filing decision whether or not its author knew
#: this existed.
TERMINAL_PATHS = {
    "reflection_turn": (
        "the seat's reflection turn completed (`turn_done`'s reflection "
        "branch). The review is filed with the seat's own reflection text."
    ),
    "agent_unreachable": (
        "the game ended but the agent could not be given a reflection turn "
        "at all (`AgentUnavailable`), so `turn_done` retires it on the "
        "spot. The review is filed with no reflection text."
    ),
    "handover_without_reflection": (
        "a handover started with no reflection behind it - `POST /release` "
        "(the lobby abandoning the game, a lobby-side end or soft delete, "
        "the live-observed `releasing game ... without a reflection turn` "
        "line), `POST /handover`, and the Job's own preStop, which POSTs "
        "that route. The review is filed with no reflection text."
    ),
    "retire": (
        "the assignment is being dropped by any route that did not already "
        "decide - a driver interrupt, an abandonment, a pod that is being "
        "terminated gracefully enough to run its preStop. Last-resort "
        "funnel: files the review if nothing else already considered it."
    ),
}

#: The one terminal state this process cannot act on, stated rather than
#: quietly omitted: a pod killed outright (SIGKILL, a node loss, an OOM
#: kill) runs no code at all, so nothing here files a review and nothing
#: here records why. What is left in the ledger is a game with neither an
#: `ai_review` row nor an `ai_review_failed` one, and that combination means
#: exactly "no driver survived to decide" - which is a different, readable
#: fact from either "the reviewer failed" or "the reviewer was never
#: invoked". A graceful termination is not this case: Kubernetes runs the
#: Job's preStop, which reaches `handover_without_reflection` above.
UNREACHABLE_TERMINAL_PATH = "pod_killed_outright"


def board_mark(state: dict) -> tuple:
    """What "the game moved" is measured against: the turn, the step, who
    holds priority, the stack's depth and whose decision is pending. Any
    play changes at least one of them."""
    pending = state.get("pending_decision") or {}
    return (
        state.get("turn_number"),
        state.get("step"),
        state.get("priority_holder"),
        len(state.get("stack") or []),
        pending.get("player") if isinstance(pending, dict) else None,
    )


def waiting_on(state: dict) -> str | None:
    """The player the game is waiting on: a pending decision's owner, else
    the priority holder - or, while another player is making that player's
    decisions (CR 723.5, `players[].decisions_made_by`), that other player,
    since CR 723.3 leaves the controlled player holding priority by name."""
    pending = state.get("pending_decision")
    if isinstance(pending, dict) and pending.get("player"):
        who: str | None = str(pending["player"])
    else:
        holder = state.get("priority_holder")
        who = str(holder) if holder else None
    for block in state.get("players") or ():
        if isinstance(block, dict) and block.get("username") == who:
            decider = block.get("decisions_made_by")
            if isinstance(decider, str) and decider:
                return decider
    return who


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
        # Set, under `lock`, the first time ANY terminal path reaches
        # `consider_ai_review` - which is a wider net than
        # `reflectionHandled` beside it, because a game can now end several
        # ways that never touch the reflection branch at all. It is what
        # keeps "every terminal path considers the review" from becoming
        # "a game whose end trips two of them files two reviews".
        "aiReviewConsidered": False,
        # The AI review's own background `threading.Thread` (`post_ai_
        # review`), once `turn_done` has spawned one - `retire` joins it,
        # bounded by `RETIRE_REVIEW_JOIN_TIMEOUT`, before it tears the
        # session down or exits the process, so the review is never merely
        # racing the container's own shutdown. `None` until a reflection
        # actually completes and spawns one; reset by `clear_locked` like
        # everything else assignment-scoped, once `retire` has already
        # joined whatever it named.
        "reviewThread": None,
        # Auto-continue bookkeeping (`continue_play`): the continuation turn
        # in flight and when it was pushed, how many continuations in a row
        # ended within `CONTINUE_QUICK_SECONDS`, and every turn id whose
        # `/turn-done` was already handled - `webhook.Deliverer` retries a
        # slow delivery, and a retry must not push a second continuation or
        # mark the turn now in flight as finished.
        "continuationTurnId": None,
        "continuationStarted": 0.0,
        # `board_mark` of the game when the continuation was pushed.
        "continuationMark": None,
        "quickContinuations": 0,
        "doneTurns": set(),
        # `now()` since which nothing has been playing this seat (no driver
        # turn in flight, the wrapper not busy); None while something is.
        # `/status` answers it as `idleSeconds`, which the game server
        # debounces "stalled" on, so a turn boundary never flashes it.
        "idleSince": None,
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
            aiReviewConsidered=False,
            reviewThread=None,
            continuationTurnId=None,
            continuationStarted=0.0,
            continuationMark=None,
            quickContinuations=0,
            doneTurns=set(),
            idleSince=None,
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
            try:
                answer = response.json()
            except ValueError as exc:
                raise httpx.HTTPError(f"game server answered something not JSON: {exc}") from exc
            if not isinstance(answer, dict):
                raise httpx.HTTPError("game server answered something not a game")
            return answer

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
        try:
            turn_id = str(response.json()["turnId"])
        except (ValueError, KeyError, TypeError) as exc:
            # A 2xx that is not the wrapper's answer - an ingress error page,
            # a proxy's HTML - is an agent nobody can confirm took the turn.
            raise AgentUnavailable(
                f"the agent's answer carried no turn id: {exc!r}", retryable=True
            ) from exc
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

    def record_review_failure(
        assignment: Assignment, stage: str, reason: str, *, invoked: bool
    ) -> None:
        """File the durable record that the AI review could not be produced
        (`audit.AI_REVIEW_FAILED_ACTION`, via
        `POST /api/games/{id}/ai-review-failure`).

        `invoked` is the whole point: it says whether the reviewer itself
        was ever asked. Without it, an unreachable wrapper and a wrapper
        that ran Opus and got nothing usable leave the same trace - no
        review, no explanation - which is exactly the ambiguity the pod logs
        left behind and this exists to close.

        **A failure to record a failure is swallowed, on purpose.** This
        runs inside the best-effort review thread, after something has
        already gone wrong; raising here would turn a missing review into an
        uncaught exception out of a daemon thread and could only make the
        situation worse. The caller has already logged its own distinct
        warning line, so nothing is lost that was not already lost."""
        try:
            with make_client(settings.game_api) as client:
                response = client.post(
                    f"/api/games/{assignment.game_id}/ai-review-failure",
                    json={
                        "player": assignment.player,
                        "uid": assignment.uid,
                        "stage": stage,
                        "reason": reason,
                        "invoked": invoked,
                    },
                )
            if response.status_code >= 400:
                log.warning(
                    "ai-review-failure: the game server refused the %s failure signal "
                    "for game %s: %s",
                    stage,
                    assignment.game_id,
                    response.status_code,
                )
        except Exception as exc:  # noqa: BLE001 - a failure to record a failure is swallowed
            log.warning(
                "ai-review-failure: could not record the %s failure for game %s: %s",
                stage,
                assignment.game_id,
                exc,
            )

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
        wrapper to generate the review, file the result.

        **Best-effort is not silent (T35).** Every way this can end without
        a review now also files a durable row through
        `record_review_failure` - the stage that failed, what it said, and
        whether the reviewer was ever invoked - so an operator reading
        `GET /api/games/{id}/review` can tell "the reviewer failed" from
        "the reviewer was never invoked" without pod-log archaeology. The
        `log.warning` lines all stay, and each carries its own distinct
        `ai-review-failed[<stage>]` prefix so they are greppable and told
        apart in logs too; the durable row is in addition to logging, never
        instead of it."""
        try:
            with make_client(settings.game_api) as client:
                response = client.get(
                    f"/api/games/{assignment.game_id}/review", params={"uid": assignment.uid}
                )
            if response.status_code >= 400:
                log.warning(
                    "ai-review-failed[review_record]: the game server refused the review "
                    "record for game %s: %s",
                    assignment.game_id,
                    response.status_code,
                )
                record_review_failure(
                    assignment,
                    "review_record",
                    f"the game server answered {response.status_code}",
                    invoked=False,
                )
                return
            review_record = response.json()
        except Exception as exc:  # noqa: BLE001 - an unreachable game server must not block retire
            log.warning(
                "ai-review-failed[review_record]: could not read the review record for game %s: %s",
                assignment.game_id,
                exc,
            )
            record_review_failure(assignment, "review_record", str(exc) or repr(exc), invoked=False)
            return

        try:
            prompt = review_prompt(
                assignment,
                reflection_text,
                review_record,
                evidence=evidence_table(settings.guides_root, assignment.deck_slug),
            )
        except Exception as exc:  # noqa: BLE001 - a malformed review record must not block retire
            log.warning(
                "ai-review-failed[prompt]: could not build the AI review prompt for game %s: %s",
                assignment.game_id,
                exc,
            )
            record_review_failure(assignment, "prompt", str(exc) or repr(exc), invoked=False)
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
                    "ai-review-failed[generate]: the wrapper could not generate an AI "
                    "review for game %s: %s",
                    assignment.game_id,
                    response.status_code,
                )
                # The wrapper answered, so the reviewer WAS invoked - it ran
                # and failed, which is a different fact from never having
                # been asked.
                record_review_failure(
                    assignment,
                    "generate",
                    f"the wrapper answered {response.status_code}",
                    invoked=True,
                )
                return
            review_text = str(response.json().get("text") or "").strip()
        except Exception as exc:  # noqa: BLE001 - an unreachable/slow wrapper must not block retire
            log.warning(
                "ai-review-failed[generate]: could not reach the wrapper to generate the "
                "AI review for game %s: %s",
                assignment.game_id,
                exc,
            )
            # No answer at all - a refused connection, a timeout, a wrapper
            # that is not there. The reviewer was never invoked.
            record_review_failure(assignment, "generate", str(exc) or repr(exc), invoked=False)
            return
        if not review_text:
            log.warning(
                "ai-review-failed[empty]: the AI review for game %s came back with no "
                "text; not filing it",
                assignment.game_id,
            )
            record_review_failure(
                assignment,
                "empty",
                "the wrapper answered with no usable review text",
                invoked=True,
            )
            return

        # DR3 (3): appended once the review one-shot has returned, never
        # blocking the filing itself. `role="reviewer"` - the review shares
        # the `/guides` checkout with the handover but writes its own
        # journal lines under that role (`handover.py`'s own module
        # docstring); reading it and building the statistics block is
        # best-effort, exactly like every other step in this function - a
        # game whose journal cannot be read still files the review text it
        # already has, with no statistics block rather than none at all.
        try:
            journal_entries = read_journal(settings.guides_root, assignment.game_id, "reviewer")
            stats_block = play_statistics_block(
                review_record, journal_entries, assignment.player, assignment.game_id
            )
            review_text = f"{review_text}\n\n{stats_block}"
        except Exception as exc:  # noqa: BLE001 - statistics are a bonus, never a blocker
            log.warning(
                "ai-review[statistics]: could not append play statistics for game %s: %s",
                assignment.game_id,
                exc,
            )

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
                    "ai-review-failed[file]: the game server refused the AI review for game %s: %s",
                    assignment.game_id,
                    response.status_code,
                )
                record_review_failure(
                    assignment,
                    "file",
                    f"the game server answered {response.status_code}",
                    invoked=True,
                )
        except Exception as exc:  # noqa: BLE001 - an unreachable game server must not block retire
            log.warning(
                "ai-review-failed[file]: could not post the AI review for game %s: %s",
                assignment.game_id,
                exc,
            )
            record_review_failure(assignment, "file", str(exc) or repr(exc), invoked=True)

    def consider_ai_review(assignment: Assignment, reflection_text: str, path: str) -> bool:
        """ "The game has ended - file the AI review, or say why not."
        The one place that decides, reached from every name in
        `TERMINAL_PATHS`. Returns whether this call is the one that started
        the review.

        **Why one function rather than a call at each end.** The review used
        to be spawned from exactly one place, `turn_done`'s reflection
        branch, so every other way a game finishes filed nothing at all and
        the review page rendered no `section.ai-review` - not an empty one,
        none. Spraying the same three lines over each terminal path would
        have fixed the observed case and left the next one to be found in
        production again; funnelling them instead means the decision is made
        in one place, deduped in one place, and - because `retire` is one of
        the callers and every terminal path ends in `retire` - made even for
        a path nobody thought to wire up.

        **Deduped across paths, not merely across retries.**
        `reflectionHandled` beside it dedupes one callback being delivered
        three times; this claims `aiReviewConsidered` for the whole
        assignment, so a game that both reflects and hands over (the normal
        case - the reflection branch calls this and then starts the
        handover, which calls it again, and the handover retires, which
        calls it a third time) still files exactly one review.

        **Best effort, and never blocking.** Same property `post_ai_review`
        has always had: the work runs on a daemon thread, nothing here waits
        on it, and a failure inside it records `audit.AI_REVIEW_FAILED_
        ACTION` rather than propagating. A path that CANNOT produce a review
        - a game released before it ended, whose api-side post-game gate
        refuses the review record - is therefore never silent either: the
        refusal lands as a durable `review_record` failure row."""
        with lock:
            if current["aiReviewConsidered"]:
                log.info(
                    "the AI review for game %s was already considered; %s files none",
                    assignment.game_id,
                    path,
                )
                return False
            current["aiReviewConsidered"] = True
        log.info("considering the AI review for game %s, reached by %s", assignment.game_id, path)
        review_thread = threading.Thread(
            target=post_ai_review,
            args=(assignment, reflection_text),
            daemon=True,
            name="driver-ai-review",
        )
        review_thread.start()
        with lock:
            current["reviewThread"] = review_thread
        return True

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
        # The last-resort funnel (`TERMINAL_PATHS["retire"]`): whatever
        # brought the assignment here, the review has now been considered.
        # Before the join below, never after - a review this call is the one
        # to start still gets waited on.
        consider_ai_review(assignment, "", "retire")
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

    def game_log(assignment: Assignment) -> tuple[list[dict], str | None]:
        """This game's log as the dispatched uid reads it (an advisor uid
        resolves to its human, so a buddy reads the same rows), and why not
        when it could not be read. Read once, as the handover starts: a lobby
        that released the seat deletes the game right after, and every table
        the handover carries - and the evidence file - comes off this one
        read."""
        try:
            with make_client(settings.game_api) as client:
                response = client.get(
                    f"/api/games/{assignment.game_id}/log", params={"uid": assignment.uid}
                )
            if response.status_code >= 400:
                return [], f"the game server answered {response.status_code}"
            rows = response.json().get("log") or []
        except Exception as exc:  # noqa: BLE001 - a missing table never stops a handover
            return [], f"the game server did not answer ({exc})"
        return [row for row in rows if isinstance(row, dict)], None

    def ledger_tables(assignment: Assignment) -> dict[str, Any]:
        """Everything the handover reads off the game log (D2b): the run,
        offer and hand-played tables, the development candidates, and this
        game's evidence record, written where the agent's `plays admit`
        picks it up (`MTG_EVIDENCE_DIR`). The cross-game evidence table comes
        off the guides checkout instead. Best effort throughout: a log that
        cannot be read names why in every table and owes no candidates."""
        game, seat = assignment.game_id, assignment.player
        rows, unread = game_log(assignment)
        evidence = evidence_table(settings.guides_root, assignment.deck_slug)
        if unread is not None:
            return {
                "run_table": f"no script run table: {unread}",
                "offers": f"no script offer table: {unread}",
                "hand_played": f"no hand-played table: {unread}",
                "evidence": evidence,
                "candidates": DevelopmentCandidates(),
            }
        if assignment.guide is not None:
            opponent = assignment.opponent_identification
            try:
                write_evidence(
                    settings.workspace,
                    evidence_record(
                        rows,
                        game,
                        seat,
                        assignment.deck_slug,
                        opponent.archetype if opponent is not None else None,
                    ),
                )
            except OSError as exc:
                log.warning("could not write the evidence file for game %s: %s", game, exc)
        return {
            "run_table": script_run_table(rows, game, seat),
            "offers": offer_table(rows, game, seat),
            "hand_played": hand_played_summary(rows, game, seat),
            "evidence": evidence,
            "candidates": development_candidates(rows, game, seat),
        }

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
        `/v1/probe`, the one wrapper route that writes into a busy turn - one
        per handover, the re-push turn included, so it is read off the
        status rather than kept per wait."""
        with lock:
            done = handover["done"]
            stop = handover.get("stop")
            budget = handover.get("budget", HANDOVER_BUDGET_SECONDS)
            nudge_after = handover.get("nudgeAfter", NUDGE_AFTER_SECONDS)
            nudge_text = handover.get("nudgeText") or handover_nudge
        while not done.wait(POLL_SECONDS):
            if stop is not None and stop.is_set():
                return "stopped"
            elapsed = now() - started
            if elapsed >= budget:
                return "timeout"
            with lock:
                nudge = elapsed >= nudge_after and not handover["status"]["nudged"]
                if nudge:
                    handover["status"]["nudged"] = True
            if nudge:
                wrapper_post("/v1/probe", {"text": nudge_text(assignment)})
        return "done"

    def push_and_await(
        assignment: Assignment,
        handover: dict,
        text: str,
        started: float,
        push: Callable[[Assignment, str, float], str] | None = None,
    ) -> str:
        """Push one handover-owned turn and wait for it: the handover turn,
        its one re-push, or a follow-up's one turn (`push` is then
        `push_followup`). The 409 back-off is measured from this push; the
        budget, from the handover's own start."""
        turn_id = (push or push_handover)(assignment, text, now())
        with lock:
            handover["turnId"] = turn_id
            key = "turnId" if handover["status"]["turnId"] is None else "repushTurnId"
            handover["status"][key] = turn_id
            if turn_id in handover["finished"]:
                handover["done"].set()
            if current["assignment"] is assignment:
                current.update(lastTurnId=turn_id, turnActive=not handover["done"].is_set())
        return await_handover_turn(assignment, handover, started)

    def post_plays_outcome(assignment: Assignment, body: dict) -> None:
        """File the handover's `plays_outcome` ledger row (D2a's route).
        Best effort, like every post-game write: a refusal or an unreachable
        game server is logged and the retire goes ahead."""
        try:
            with make_client(settings.game_api) as client:
                response = client.post(f"/api/games/{assignment.game_id}/plays-outcome", json=body)
            if response.status_code >= 400:
                log.warning(
                    "plays-outcome: the game server refused game %s's row: %s",
                    assignment.game_id,
                    response.status_code,
                )
        except Exception as exc:  # noqa: BLE001 - never blocks the retire
            log.warning("plays-outcome: could not post game %s's row: %s", assignment.game_id, exc)

    def settle_development(
        assignment: Assignment, handover: dict, state: str, started: float
    ) -> str:
        """After the handover turn (D2b): read this game's journal, push one
        re-push turn when the development step is still open and budget is
        left, then file the `plays_outcome` row. Returns the handover's final
        state (the re-push's own, when there was one); a re-push that fails
        records its error in the status and is `failed`."""
        with lock:
            candidates = handover["candidates"]
        verdict = assess(
            read_journal(settings.guides_root, assignment.game_id, assignment.role), candidates
        )
        repushed = False
        elapsed = now() - started
        if state == "done" and not verdict.satisfied and elapsed < HANDOVER_BUDGET_SECONDS:
            repushed = True
            minutes_left = max(1, int((HANDOVER_BUDGET_SECONDS - elapsed) // 60))
            with lock:
                handover["status"]["repushed"] = True
                handover.update(done=threading.Event(), turnId=None, finished=set())
            log.info("game %s: development still open, re-pushing once", assignment.game_id)
            try:
                state = push_and_await(
                    assignment,
                    handover,
                    handover_repush(assignment, verdict.missing, minutes_left),
                    started,
                )
            except Exception as exc:  # noqa: BLE001 - the outcome row is still filed
                state = "failed"
                log.error("handover re-push for game %s failed: %s", assignment.game_id, exc)
                with lock:
                    handover["status"]["error"] = f"the re-push failed: {exc}"
            verdict = assess(
                read_journal(settings.guides_root, assignment.game_id, assignment.role),
                candidates,
            )
        with lock:
            nudged = bool(handover["status"]["nudged"])
            error = handover["status"].get("error")
        reasons = unfinished_reasons(
            state=state, error=error, nudged=nudged, repushed=repushed, verdict=verdict
        )
        body = plays_outcome_body(
            uid=assignment.uid,
            player=assignment.player,
            role=assignment.role,
            verdict=verdict,
            reasons=reasons,
            repushed=repushed,
            deck=assignment.guide.slug if assignment.guide is not None else None,
            opponent=opponent_summary(assignment),
        )
        with lock:
            handover["status"]["playsOutcome"] = body["state"]
        post_plays_outcome(assignment, body)
        return state

    def run_handover(
        assignment: Assignment,
        handover: dict,
        prepared: str | Exception,
        interrupt: bool,
        started: float,
    ) -> None:
        """The handover's background half: interrupt, push, wait, settle the
        mandatory development step (one re-push at most, then the
        `plays_outcome` row), then write the marker whatever happened, and
        only then retire."""
        state, error = "failed", None
        try:
            if isinstance(prepared, Exception):
                raise prepared
            if interrupt:
                interrupted = wrapper_post("/v1/interrupt")
                with lock:
                    handover["status"]["interrupted"] = interrupted
            state = push_and_await(assignment, handover, prepared, started)
        except Exception as exc:  # noqa: BLE001 - every failure still writes the marker
            state, error = "failed", str(exc)
            log.error("handover for game %s failed: %s", assignment.game_id, exc)
        with lock:
            handover["status"].update(state=state, error=error)
        try:
            state = settle_development(assignment, handover, state, started)
        except Exception as exc:  # noqa: BLE001 - every failure still writes the marker
            state = "failed"
            log.error("handover development for game %s failed: %s", assignment.game_id, exc)
            with lock:
                handover["status"]["error"] = str(exc)
        with lock:
            handover["status"]["state"] = state
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
                if active.get("stop") is not None and not active["stop"].is_set():
                    # A follow-up (F1): its Job is being deleted - the game
                    # was ended or forgotten - so this is the hard stop, not
                    # a handover. `run_followup` interrupts the turn, files
                    # the outcome and writes the marker at once.
                    active["status"]["stopReason"] = (
                        f"stopped: the follow-up was ended from outside ({outcome or 'killed'})"
                    )
                    active["stop"].set()
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
                    # D2b: whether the one re-push went out, and the
                    # `plays_outcome` row's state once it is filed.
                    "repushed": False,
                    "playsOutcome": None,
                    "error": None,
                    "marker": str(marker_path),
                },
                "done": threading.Event(),
                "turnId": None,
                # Turns that finished before the handover turn's id was known.
                "finished": set(),
                # What the mandatory development step must answer (D2b).
                "candidates": DevelopmentCandidates(),
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
        # Before the review below is considered, so the reviewer's prompt
        # names the same revealed deck the handover turn does. A no-op when
        # the reflection branch already identified it.
        identify_opponent(assignment)
        # `TERMINAL_PATHS["handover_without_reflection"]`: a handover is the
        # end of this driver's involvement however it was reached - a
        # `/release` from the lobby, a `/handover` from the Job's preStop,
        # or the reflection branch above, which has already considered the
        # review with the seat's own text and so deduplicates this call.
        consider_ai_review(assignment, "", "handover_without_reflection")
        prepared: str | Exception
        try:
            resolved = outcome or ended_outcome(assignment) or "killed"
            with lock:
                handover["status"].update(outcome=resolved, state="running")
            tables = ledger_tables(assignment)
            with lock:
                handover["candidates"] = tables["candidates"]
            prepared = handover_turn(
                assignment,
                resolved,
                tables["run_table"],
                conflict_warnings(assignment.scripts, assignment.deck_slug),
                offers=tables["offers"],
                hand_played=tables["hand_played"],
                evidence=tables["evidence"],
                candidates=tables["candidates"],
                review_answered=review_answered_plays(
                    read_journal(settings.guides_root, assignment.game_id, "reviewer")
                ),
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
        for seat in lobby_seats(game_id, uid):
            if seat.get("player") == player:
                return seat.get("decklist") or ""
        return ""

    def seat_support_warnings(game_id: str, uid: str, player: str) -> list[dict]:
        """This seat's own `support_warnings` (flow 6, W2) - `[]` best
        effort, the same rule `seat_decklist` follows: an unreachable lobby
        route or a game with no decklists at all answers empty rather than
        aborting the dispatch, and the briefing simply names no cards
        (`prompt._support_warnings_lines`). The driver never computes this
        itself; it only relays what `GET .../seats` already answered."""
        for seat in lobby_seats(game_id, uid):
            if seat.get("player") == player:
                return [w for w in (seat.get("support_warnings") or []) if isinstance(w, dict)]
        return []

    def lobby_seats(game_id: str, uid: str) -> list[dict]:
        """`GET /api/lobby/games/{id}/seats` as `uid` sees it; empty when the
        route cannot be read."""
        try:
            with make_client(settings.game_api) as client:
                response = client.get(f"/api/lobby/games/{game_id}/seats", params={"uid": uid})
                if response.status_code >= 400:
                    return []
                seats = response.json().get("seats") or []
        except (httpx.HTTPError, ValueError, AttributeError):
            return []
        return [seat for seat in seats if isinstance(seat, dict)]

    def opponent_decklist(game_id: str, uid: str, opponent: str) -> str | None:
        """The opponent's decklist text, or `None` when it is not readable.

        The same lobby route as `seat_decklist`, which hides every list but
        the caller's own while the game runs and reveals both to the game's
        own credentials once it is over (A2) - so this is read after game
        over only, with the seat's (or the buddy's advisor) uid. `opponent`
        is the name `opponent_of` recorded at dispatch; with none recorded
        there is no seat to read.
        """
        if not opponent:
            return None
        for seat in lobby_seats(game_id, uid):
            if seat.get("player") == opponent:
                return seat.get("decklist") or None
        return None

    def identify_opponent(assignment: Assignment) -> None:
        """Identify the opponent's revealed deck, once per assignment: before
        the reflection turn, or lazily when the handover starts first.

        Best effort. A list that cannot be read leaves
        `opponent_identification` at `None`, and every prompt that names the
        opponent then states its fallback: go by what the board showed.
        """
        with lock:
            if assignment.opponent_checked:
                return
            assignment.opponent_checked = True
        try:
            assignment.stub_decks = tuple(library.stub_decks())
            text = opponent_decklist(assignment.game_id, assignment.uid, assignment.opponent)
            if text is None:
                log.info("game %s: the opponent's decklist is not readable", assignment.game_id)
                return
            identified = library.identify(parse_decklist(text))
        except Exception as exc:  # noqa: BLE001 - never costs the reflection or the handover
            log.warning("could not identify the opponent in game %s: %s", assignment.game_id, exc)
            return
        assignment.opponent_identification = identified
        log.info(
            "game %s: opponent identified as %s (%s, archetype %s)",
            assignment.game_id,
            identified.guide.slug if identified.guide else "no guide",
            identified.confidence,
            identified.archetype,
        )

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
        from `DRIVER_ASSIGNMENT_JSON` - one code path either way. A
        `mode: "followup"` body is F1's follow-up, not a match
        (`assign_followup`)."""
        if body.mode == "followup":
            return assign_followup(body)
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
            support_warnings=tuple(seat_support_warnings(body.game_id, body.uid, body.player)),
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

    # -- F1: the follow-up DSL-development turn ------------------------------

    def opponent_summary(assignment: Assignment) -> dict | None:
        """The revealed opponent as the `plays_outcome` row records it
        (`{slug, archetype, confidence}`), or `None` when it was never
        identified. What the api hands a follow-up when the list itself is
        no longer readable."""
        identified = assignment.opponent_identification
        if identified is None:
            return None
        return {
            "slug": identified.guide.slug if identified.guide is not None else None,
            "archetype": identified.archetype,
            "confidence": identified.confidence,
        }

    def own_identification(payload: dict) -> Identification:
        """The seat's own deck for a follow-up: the deck the handover
        recorded when this checkout has its guide, else what the seat's own
        list identifies as - the same classifier its dispatch ran."""
        guide = library.get(str(payload["deck"])) if payload.get("deck") else None
        if guide is not None:
            signature = guide.signature()
            return Identification(
                CONFIDENT, guide=guide, archetype=signature.name if signature else None
            )
        return library.identify(parse_decklist(str(payload.get("decklist") or "")))

    def followup_opponent(payload: dict) -> Identification | None:
        """The opponent for a follow-up: identified from the revealed list
        the api copied into the payload, else rebuilt from the
        identification the handover recorded, else unknown."""
        text = payload.get("opponent_decklist")
        if text:
            return library.identify(parse_decklist(str(text)))
        recorded = payload.get("opponent_identification")
        if not isinstance(recorded, dict) or not recorded.get("confidence"):
            return None
        slug = recorded.get("slug")
        return Identification(
            str(recorded["confidence"]),
            guide=library.get(str(slug)) if slug else None,
            archetype=recorded.get("archetype"),
        )

    def push_followup(assignment: Assignment, text: str, started: float) -> str:
        """Push the follow-up's one turn. The agent container boots beside
        this driver, so a retryable refusal is waited out for
        `agent_boot_wait` - and never past the hard stop."""
        deadline = time.monotonic() + settings.agent_boot_wait
        while True:
            with lock:
                active = current["handover"]
                stopped = active is not None and active.get("stop") is not None
                stopped = stopped and active["stop"].is_set()
            if stopped:
                raise RuntimeError("the follow-up was stopped before its turn was pushed")
            try:
                turn_id = push_turn(text)
            except AgentUnavailable as exc:
                if not exc.retryable or time.monotonic() >= deadline:
                    raise
                time.sleep(settings.agent_retry_interval)
                continue
            with lock:
                if current["assignment"] is assignment:
                    current["pending"] = False
            return turn_id

    def followup_reasons(
        state: str, error: str | None, nudged: bool, stop_reason: str | None, verdict: Any
    ) -> list[str]:
        """Why a follow-up counts as unfinished - empty when it is complete.
        Its own words, not the handover's: its budget, its nudge, and no
        re-push, because a follow-up is one turn."""
        reasons: list[str] = []
        if nudged:
            reasons.append(
                "nudged: the follow-up was still running at "
                f"{int(FOLLOWUP_NUDGE_AFTER_SECONDS // 60)} minutes"
            )
        if state == "stopped":
            reasons.append(stop_reason or "stopped: the follow-up was ended from outside")
        elif state == "timeout":
            reasons.append(
                "timeout: the follow-up turn did not end within "
                f"{int(FOLLOWUP_BUDGET_SECONDS // 60)} minutes"
            )
        elif state == "failed":
            reasons.append(f"failed: {error or 'the follow-up turn could not be run'}")
        if verdict.last_write_failed:
            reasons.append(f"the last write did not land: {verdict.last_write_failed}")
        if not verdict.satisfied:
            reasons.append(
                "development still open after the follow-up: " + "; ".join(verdict.missing)
            )
        return reasons

    def run_followup(assignment: Assignment, followup: dict, text: str) -> None:
        """The follow-up's background half: push its one turn, wait on it
        (budget, one nudge, the hard stop), read the journal, file the
        `plays_outcome` row with `followup: true`, write the marker, retire.
        Never a re-push and never a continuation: one bounded turn."""
        state, error = "failed", None
        try:
            state = push_and_await(assignment, followup, text, now(), push=push_followup)
        except Exception as exc:  # noqa: BLE001 - every failure still files and retires
            state, error = "failed", str(exc)
            log.error("follow-up for game %s failed: %s", assignment.game_id, exc)
        with lock:
            if followup["stop"].is_set():
                state = "stopped"
            stop_reason = followup["status"].get("stopReason")
            nudged = bool(followup["status"]["nudged"])
            candidates = followup["candidates"]
        if state == "stopped":
            interrupted = wrapper_post("/v1/interrupt")
            with lock:
                followup["status"]["interrupted"] = interrupted
        try:
            verdict = assess(
                read_journal(settings.guides_root, assignment.game_id, assignment.role),
                candidates,
            )
            body = plays_outcome_body(
                uid=assignment.uid,
                player=assignment.player,
                role=assignment.role,
                verdict=verdict,
                reasons=followup_reasons(state, error, nudged, stop_reason, verdict),
                repushed=False,
                followup=True,
                deck=assignment.guide.slug if assignment.guide is not None else None,
                opponent=opponent_summary(assignment),
            )
            with lock:
                followup["status"]["playsOutcome"] = body["state"]
            post_plays_outcome(assignment, body)
        except Exception as exc:  # noqa: BLE001 - the marker and the retire still happen
            log.error("follow-up outcome for game %s failed: %s", assignment.game_id, exc)
            error = error or str(exc)
        with lock:
            followup["status"].update(state=state, error=error)
            status = dict(followup["status"])
        try:
            write_marker(marker_path, status)
        except OSError as exc:
            log.error("could not write the follow-up marker %s: %s", marker_path, exc)
        log.info("follow-up for game %s: %s", assignment.game_id, state)
        with lock:
            ours = current["assignment"] is assignment
        if ours:
            retire(assignment)

    def assign_followup(body: DispatchIn) -> dict:
        """Take F1's follow-up: build the assignment from the payload the api
        read off the ledger, and push the one follow-up turn - no turn 0, no
        game read, no scripts registered.

        The assignment is marked `reflected` and its review `considered`
        from the start: the game is over and its review was the handover's
        to file, so `/status` never reads the follow-up as a stranded seat,
        no continuation can ever be pushed for it, and `retire` files no
        second review. Its turn is held as handover-kind work
        (`current["handover"]`), which is what routes its `/turn-done` here
        and makes a `/handover` or `/release` the hard stop."""
        payload = body.followup or {}
        with lock:
            existing = current["assignment"]
            if existing is not None:
                if existing.uid == body.uid and existing.game_id == body.game_id:
                    return {"mode": "followup", "alreadyRunning": True}
                raise HTTPException(
                    status_code=409,
                    detail=f"this agent is already working on game {existing.game_id}",
                )
        opponent = followup_opponent(payload)
        assignment = Assignment(
            game_id=body.game_id,
            uid=body.uid,
            player=body.player,
            opponent=str(payload.get("opponent") or ""),
            api_base=body.api_base or settings.game_api,
            decklist=parse_decklist(str(payload.get("decklist") or "")),
            identification=own_identification(payload),
            guides_revision=guides_revision(settings.guides_root),
            agent=body.agent,
            role=body.role,
            opponent_identification=opponent,
            opponent_checked=True,
            stub_decks=tuple(library.stub_decks()),
        )
        raw = payload.get("candidates") or {}
        candidates = DevelopmentCandidates(
            plays=tuple(str(play) for play in raw.get("plays") or ()),
            lines=tuple(str(line) for line in raw.get("lines") or ()),
        )
        reasons = [str(reason) for reason in payload.get("unfinished_reasons") or ()]
        text = followup_turn(
            assignment,
            str(payload.get("outcome") or "the game is over"),
            reasons,
            run_table=str(payload.get("run_table") or "no script run table was recorded"),
            offers=str(payload.get("offers") or "no script offer table was recorded"),
            hand_played=str(payload.get("hand_played") or "no hand-played table was recorded"),
            evidence=evidence_table(settings.guides_root, assignment.deck_slug),
            candidates=candidates,
            review_answered=review_answered_plays(
                read_journal(settings.guides_root, assignment.game_id, "reviewer")
            ),
        )
        followup: dict[str, Any] = {
            "status": {
                "handoverId": uuid.uuid4().hex,
                "kind": "followup",
                "state": "running",
                "outcome": payload.get("outcome"),
                "gameId": assignment.game_id,
                "player": assignment.player,
                "role": assignment.role,
                "turnId": None,
                "interrupted": False,
                "nudged": False,
                "repushed": False,
                "playsOutcome": None,
                "error": None,
                "marker": str(marker_path),
            },
            "done": threading.Event(),
            "stop": threading.Event(),
            "turnId": None,
            "finished": set(),
            "candidates": candidates,
            "budget": FOLLOWUP_BUDGET_SECONDS,
            "nudgeAfter": FOLLOWUP_NUDGE_AFTER_SECONDS,
            "nudgeText": followup_nudge,
        }
        with lock:
            if current["assignment"] is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"this agent is already working on game {current['assignment'].game_id}",
                )
            clear_locked()
            current.update(
                assignment=assignment,
                reflected=True,
                aiReviewConsidered=True,
                pending=True,
                lastError=None,
                handover=followup,
                lastHandover=followup,
            )
        clear_marker(marker_path)
        settings.workspace.mkdir(parents=True, exist_ok=True)
        sync_agent_settings(body.agent)
        log.info(
            "follow-up for game %s (%s, %s): deck=%s, %d reason(s)",
            assignment.game_id,
            assignment.player,
            assignment.role,
            assignment.deck_slug,
            len(reasons),
        )
        threading.Thread(
            target=run_followup,
            args=(assignment, followup, text),
            daemon=True,
            name="driver-followup",
        ).start()
        return {"mode": "followup", "deck": assignment.deck_slug, "pending": True}

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
        """Claims the finished turn id before any work, so a concurrent or
        retried duplicate delivery is ignored - and gives the claim back if
        the work raises, so the Deliverer's retry of a delivery that failed
        is handled rather than dropped as a duplicate."""
        finished = record.get("turnId")
        with lock:
            duplicate = (
                bool(finished)
                and finished != current["reflectionTurnId"]
                and finished in current["doneTurns"]
            )
            claimed = bool(finished) and not duplicate
            if claimed:
                current["doneTurns"].add(finished)
        try:
            return handle_turn_done(record, finished, duplicate)
        except Exception:
            if claimed:
                with lock:
                    current["doneTurns"].discard(finished)
            raise

    def handle_turn_done(record: dict, finished: str | None, duplicate: bool) -> dict:
        if not duplicate:
            # After the duplicate check: a retried duplicate must neither
            # clear a loop-guard `lastError` nor set a stale one.
            log_turn_result(record)
        with lock:
            assignment = current["assignment"]
            reflected = current["reflected"]
            reflection_id = current["reflectionTurnId"]
            active = current["handover"]
            if active is None:
                # The turn the agent just finished is no longer in flight. Set
                # before anything else here, so a continuation or reflection
                # push below can turn it back on. A retried delivery of a turn
                # already handled leaves it alone: the turn in flight now is a
                # later one, and it has not finished.
                if not duplicate:
                    current["turnActive"] = False
                    current["idleSince"] = now()
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
            if not already_handled:
                # Considered BEFORE `start_handover` below, not after: that
                # call is what creates and starts `run_handover`'s own
                # background thread, which calls `retire` at its own end and
                # joins `current["reviewThread"]` only if it is already set.
                # A `run_handover` racing ahead fast enough used to reach
                # `retire` before this ever ran, finding nothing to join and
                # losing the review outright. `run_handover`'s thread cannot
                # exist before `start_handover` creates it, so considering
                # first makes the join possible on every ordering the OS
                # scheduler could ever produce, not merely a lucky one - and
                # it is what makes this the path that supplies the seat's
                # own reflection text, rather than the handover's empty one.
                consider_ai_review(assignment, reflection_text_value, "reflection_turn")
            with lock:
                ended = current["outcome"] or ""
            handover_status = start_handover(ended)
            return {
                "handover": handover_status,
                "gameId": assignment.game_id,
                "reflected": True,
            }
        if reflected:
            return {"ignored": "already reflected"}
        if duplicate:
            return {"ignored": "duplicate delivery"}
        error = str(record.get("error") or "")
        agent_gone = error.startswith(AGENT_GONE_ERRORS)
        return settle_turn(assignment, finished, agent_gone=agent_gone, retry=True)

    def retry_unreachable(assignment: Assignment, finished: str | None, agent_gone: bool) -> None:
        """`settle_turn`'s one delayed re-check of a game it could not read.

        Skipped when anything moved on meanwhile: the assignment was retired
        or replaced, a handover started, the game was reflected on, or some
        other turn is in flight."""
        time.sleep(UNREACHABLE_RETRY_SECONDS)
        with lock:
            moved_on = (
                current["assignment"] is not assignment
                or current["handover"] is not None
                or current["reflected"]
                or current["turnActive"]
            )
        if moved_on:
            return
        settle_turn(assignment, finished, agent_gone=agent_gone, retry=False)

    def file_problem_report(assignment: Assignment, text: str) -> None:
        """The same row the `mtg` MCP server's `report_problem` tool files
        (`POST /api/games/{id}/report`), under this seat's own uid. Best
        effort: an unreachable game server is logged, never raised."""
        try:
            with make_client(settings.game_api) as client:
                response = client.post(
                    f"/api/games/{assignment.game_id}/report",
                    params={"uid": assignment.uid},
                    json={"player": assignment.player, "text": text},
                )
            if response.status_code >= 400:
                log.warning(
                    "game server refused the problem report for game %s: %s",
                    assignment.game_id,
                    response.status_code,
                )
        except Exception as exc:  # noqa: BLE001 - a report must never break the driver
            log.warning("could not file a problem report for game %s: %s", assignment.game_id, exc)

    def moved_on_locked(assignment: Assignment) -> bool:
        return (
            current["assignment"] is not assignment
            or current["handover"] is not None
            or current["reflected"]
        )

    def continue_play(assignment: Assignment, finished: str | None, state: dict) -> dict:
        """Push a continuation turn: the game is still running and nothing
        is playing the seat. See `CONTINUE_QUICK_SECONDS` for the guard."""
        mark = board_mark(state)
        with lock:
            if moved_on_locked(assignment):
                return {"ignored": "assignment moved on"}
            if finished and finished == current["continuationTurnId"]:
                elapsed = now() - current["continuationStarted"]
                own_move = assignment.role != "advisor" and waiting_on(state) == assignment.player
                quick = (
                    mark == current["continuationMark"]
                    and elapsed < CONTINUE_QUICK_SECONDS
                    and (own_move or elapsed < CONTINUE_INSTANT_SECONDS)
                )
                current["quickContinuations"] = current["quickContinuations"] + 1 if quick else 0
            tripped = current["quickContinuations"] >= CONTINUE_QUICK_LIMIT
            if tripped:
                reason = (
                    f"stopped auto-continuing: {current['quickContinuations']} continuation "
                    f"turns in a row each ended within {CONTINUE_QUICK_SECONDS:g}s with the "
                    "game not moving at all while it was still running"
                )
                current["lastError"] = reason
        if tripped:
            log.error("game %s (%s): %s", assignment.game_id, assignment.player, reason)
            file_problem_report(
                assignment,
                f"The driver {reason}. The agent keeps ending its turn at once; "
                "the seat is no longer being played and needs a look.",
            )
            return {"ignored": "continuation loop", "gameId": assignment.game_id}
        try:
            turn_id = push_turn(continue_turn(assignment))
        except AgentUnavailable as exc:
            log.error(
                "game %s is still running but the agent could not continue: %s",
                assignment.game_id,
                exc,
            )
            with lock:
                # A handover that took the agent first is not a failure.
                if not moved_on_locked(assignment):
                    current["lastError"] = f"could not continue the game: {exc}"
            return {"ignored": "agent unavailable", "gameId": assignment.game_id}
        with lock:
            raced = moved_on_locked(assignment)
            if not raced:
                current.update(
                    lastTurnId=turn_id,
                    turnActive=True,
                    lastError=None,
                    continuationTurnId=turn_id,
                    continuationStarted=now(),
                    continuationMark=mark,
                    idleSince=None,
                )
        if raced:
            # A handover, a reflection or a new assignment claimed the seat
            # while this push was on the wire. Its turn must not run on
            # beside the handover's, and it must not overwrite that state.
            log.warning(
                "game %s: continuation turn %s raced a handover or retirement; interrupting it",
                assignment.game_id,
                turn_id,
            )
            wrapper_post("/v1/interrupt")
            return {"ignored": "assignment moved on during the push", "turnId": turn_id}
        log.info(
            "game %s still running after turn %s, continuation turn %s",
            assignment.game_id,
            finished or "?",
            turn_id,
        )
        return {"continued": turn_id, "gameId": assignment.game_id}

    def settle_turn(
        assignment: Assignment, finished: str | None, *, agent_gone: bool, retry: bool
    ) -> dict:
        """A play turn ended outside any handover: continue the game, or
        reflect on it once it is over."""
        try:
            state = game_state(assignment.game_id, assignment.uid)
        except httpx.HTTPError as exc:
            if retry:
                log.warning(
                    "could not read game state after a turn, checking again in %gs: %s",
                    UNREACHABLE_RETRY_SECONDS,
                    exc,
                )
                threading.Thread(
                    target=retry_unreachable,
                    args=(assignment, finished, agent_gone),
                    daemon=True,
                    name="driver-unreachable-retry",
                ).start()
            else:
                log.error("game %s still unreachable after a retry: %s", assignment.game_id, exc)
                with lock:
                    if current["assignment"] is assignment:
                        current["lastError"] = (
                            f"game server unreachable after turn {finished or '?'}: {exc}"
                        )
            return {"ignored": "game unreachable"}

        winner = state.get("winner")
        drawn = bool(state.get("drawn"))
        if not winner and not drawn:
            if agent_gone:
                return {"ignored": "game still running"}
            return continue_play(assignment, finished, state)

        outcome = f"{winner} won" if winner else "the game was a draw"
        identify_opponent(assignment)
        try:
            turn_id = push_turn(reflection_turn(assignment, settings.memory_root, outcome))
        except AgentUnavailable as exc:
            # No notes from this game, but the match is still over. Retire it
            # rather than hold an assignment that would 409 every future
            # dispatch against an agent that is already being replaced.
            log.error("game %s ended but the agent could not reflect: %s", assignment.game_id, exc)
            # The game is over and there will be no reflection: the review
            # is still worth filing, and `review_prompt` already renders a
            # missing one as "(the seat left no reflection)".
            consider_ai_review(assignment, "", "agent_unreachable")
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

    def agent_session() -> dict[str, Any] | None:
        """What the agent is doing right now, read off the wrapper.

        The agent's own listener is not reachable from outside the pod, and
        `/status` below is already what the game server polls for this seat -
        so this is where the wrapper's coarse activity word crosses over on
        its way to the play UI's AI pill.

        Never raises, and bounded by `ACTIVITY_READ_TIMEOUT`: an unreachable
        agent, a wrapper old enough to 404 the route, a non-JSON body and an
        `activity` that is not an object all come back as `None`, which the
        UI reads as "fall back to the plain state word".

        Answers the wrapper's whole session object, so `/status` can carry
        both its `activity` and its own `state` word.
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
        return answer if isinstance(answer, dict) else None

    @app.get("/status")
    def status() -> dict:
        # Outside the lock: it is an HTTP round trip, and nothing below
        # depends on it being consistent with the snapshot taken under the
        # lock - it is one coarse word about the agent, not driver state.
        session = agent_session() or {}
        activity = session.get("activity")
        busy = session.get("state") == "busy"
        with lock:
            # A handover in progress is the seat being wound down on purpose,
            # never a seat nobody is playing.
            if (
                current["turnActive"]
                or busy
                or current["assignment"] is None
                or current["handover"] is not None
            ):
                current["idleSince"] = None
            elif current["idleSince"] is None:
                current["idleSince"] = now()
            idle_seconds = (
                None if current["idleSince"] is None else round(now() - current["idleSince"], 1)
            )
            assignment = current["assignment"]
            return {
                "agentActivity": activity if isinstance(activity, dict) else None,
                # The wrapper's own "claude holds a turn" (`session.State.
                # BUSY`), whoever pushed that turn. `turnActive` only knows
                # the turns this driver pushed; an agent woken by a
                # background task's notification, or by a message sent to
                # the wrapper directly, is working all the same, and must
                # never read as idle.
                "agentBusy": busy,
                # How long nothing has been playing this seat, or None while
                # something is: the game server reports `stalled` only past
                # its own grace, so a turn boundary never flashes it.
                "idleSeconds": idle_seconds,
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
        route uses.

        **The dispatch runs as a background task, never awaited before
        `yield`.** `assign` is synchronous (it blocks on `httpx.Client` calls
        to the game server, and to the agent's wrapper for up to
        `agent_boot_wait`), so it still runs in a worker thread - `to_thread`,
        not a bare call - but awaiting that call here, before `yield`, used to
        hold ASGI startup open for the whole of it: uvicorn does not consider
        the app started, and serves NOTHING - not even `GET /status` - until
        lifespan reaches its `yield`. `k8s_seats.status()` reads `/status` to
        tell a `Running` pod that is still booting from one whose driver died,
        and allows only `driver_grace_seconds` (60s, deliberately untouched by
        this fix) before calling an unreachable driver an error; a
        self-dispatch slower than that - a slow game server, or an agent
        genuinely still coming up - used to make a perfectly healthy seat
        register as a dead one and hand the game to the pregame backstop.
        Backgrounding the dispatch is what keeps `/status` answering from the
        container's very first tick, so that grace clock has something to
        actually read.

        `current["pending"]` is set True the moment this task is scheduled -
        before `assign` itself is even called, let alone has claimed the
        seat - so a reader of `/status` during this window (`k8s_seats.
        status()`, `ai_seat.py`) sees the same "assigned, not yet confirmed
        playing" shape `assign`'s own ordinary path already produces, rather
        than the assignment-less, not-pending, not-active shape both readers
        read as `idle` - which they already treat as "this seat gave up", not
        "this seat has not started yet".

        Every failure - a body that will not parse as JSON, one that parses
        but fails validation, the game server being unreachable, the agent
        refusing turn 0 outright - is still caught broadly, logged, and
        recorded on `/status` rather than raised: an uncaught exception here
        would only become an unhandled-task-exception line in the log, worse
        than the same failure recorded where `/status` can show it. For a
        per-game Job (`exit_after_match`) a self-dispatch that cannot even
        begin is terminal - there is no caller left to retry it - so the
        container is told to exit rather than sit idle until Kubernetes' own
        deadline finds it.

        JSON parsing and validation stay synchronous, before `yield`: both
        are in-memory and instant, so there is nothing a background task buys
        there, and a malformed assignment can be recorded - and the container
        told to exit - before the app has even started.

        The task is kept on `app.state` so it is never garbage-collected
        mid-flight, and is cancelled and awaited - swallowing the
        `CancelledError` that produces - on shutdown, so a container tearing
        down never leaves a dispatch thread dangling behind it."""
        self_dispatch_task: asyncio.Task[None] | None = None
        if settings.assignment_json:
            try:
                body = DispatchIn.model_validate_json(settings.assignment_json)
            except Exception as exc:  # noqa: BLE001 - malformed input is terminal, not fatal to boot
                log.error("self-dispatch failed: %s", exc)
                with lock:
                    current["lastError"] = str(exc)
                if settings.exit_after_match:
                    app.state.should_exit = True
            else:
                with lock:
                    current["pending"] = True

                async def run_self_dispatch(body: DispatchIn = body) -> None:
                    try:
                        await asyncio.to_thread(assign, body)
                    except Exception as exc:  # noqa: BLE001 - any failure here is terminal
                        log.error("self-dispatch failed: %s", exc)
                        with lock:
                            current["lastError"] = str(exc)
                            current["pending"] = False
                        if settings.exit_after_match:
                            app.state.should_exit = True

                self_dispatch_task = asyncio.create_task(
                    run_self_dispatch(), name="driver-self-dispatch"
                )
                app.state.self_dispatch_task = self_dispatch_task
        yield
        if self_dispatch_task is not None:
            self_dispatch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self_dispatch_task

    app.router.lifespan_context = lifespan

    return app
