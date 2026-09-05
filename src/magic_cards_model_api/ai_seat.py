"""Drive one seat with a headless Claude Sonnet subprocess.

The natural mechanism, confirmed working on this machine before anything was
built around it: `claude --print --dangerously-skip-permissions --allowedTools
Bash -- "<prompt>"` runs one non-interactive, single-process Claude session
that is itself agentic - it keeps calling the `Bash` tool (here, `seat_cli`)
in a loop until it decides the game is over or it gives up, all inside one
`claude` invocation. `--` before the prompt is load-bearing: `--allowedTools`
is variadic ("Bash <tools...>", `claude --help`) and without a separator it
swallows the prompt text as more tool names, which fails with "Input must be
provided either through stdin or as a prompt argument" - discovered by
running the bare command by hand before writing this module, not assumed.

**Fallback path.** If `claude` is not on PATH, or spawning it fails for any
reason, `start` never raises - it reports `mechanism: "fallback"` and hands
back the exact prompt and uid a human would need to run the same session by
hand in another terminal:

    claude --print --dangerously-skip-permissions --allowedTools Bash -- "<prompt>"

copy-pasted from the API repo directory. The game is fully playable this way;
only the automatic spawn is unavailable. The UI surfaces this box whenever
`mechanism` is `"fallback"`.

**"Visibly alive."** This module does not parse the subprocess's own
reasoning - the ground truth for "what did the AI seat last do" is the game's
own audit log (`audit.entries`, filtered to this seat's actions), which is
exactly as accurate as the log the human's own actions land in, and involves
no guessing about the shape of Claude's stdout. What this module *does* track
per uid is process liveness (`running`), its exit code once it has one, and a
tail of stdout/stderr for surfacing a crash - "thinking" is simply "the
subprocess is still running and has not asked for anything".

That same liveness has a second reader, and a stricter one: `activity` (and
`why_gone` beside it) answers `WORKING` / `STARTING` / `GONE` for
`ai_pregame.py`, which will take a seat's own CR 103.5 declaration away from
it when nothing is playing that seat. `status` is written for a human reading
a panel and may hedge; `activity` may not, so it is a separate method with
three defined answers, and it errs towards `GONE` for anything it cannot
establish - see its docstring for which way each failure falls and why.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import k8s_seats
from .agent_settings import AgentSettings

#: Root of this repo (`src/magic_cards_model_api/ai_seat.py` -> repo root),
#: the working directory `seat_cli` must run from, and where `claude` is
#: invoked from so its own CLAUDE.md/settings apply the same way they would
#: for a human running the identical command by hand.
API_REPO = Path(__file__).resolve().parents[2]

BRIEF_PATH = API_REPO / "docs" / "ai_seat_brief.md"

#: How many lines of stdout/stderr are kept per runner - enough to show a
#: crash's own error text without holding a whole game transcript in memory
#: for a process that, unlike the audit log, is not the source of truth for
#: what happened in the game.
TAIL_LINES = 200

#: What `AiSeatRegistry.activity` is allowed to conclude about one seat. Three
#: values, not two, because "has not answered yet" splits into two cases that
#: want opposite treatment from `ai_pregame.py`'s backstop:
#:
#: - `WORKING` - a process or an agent holds this seat and is mid-turn. It is
#:   thinking, and however long that takes it is going to answer for itself.
#: - `STARTING` - the seat has been handed over but is not yet able to act
#:   (the container is still booting). It has not had its chance yet, so no
#:   grace period should be running against it.
#: - `GONE` - nothing is playing this seat: never started, failed to start,
#:   exited, errored, or finished its turn without acting. Only this one is
#:   evidence that the human is waiting on nobody.
WORKING = "working"
STARTING = "starting"
GONE = "gone"


def ai_seat_mode() -> str:
    """`subprocess` (the default, a one-shot `claude --print` per game),
    `wrapper` (hand the seat to the driver, which drives a long-lived
    interactive claude that remembers previous games), or `kubernetes` (each
    AI seat gets its own Job - agent plus driver sidecar - scheduled and
    watched by `k8s_seats.K8sSeats`)."""
    return (os.environ.get("AI_SEAT_MODE") or "subprocess").strip().lower()


def driver_url() -> str:
    return (os.environ.get("DRIVER_URL") or "http://driver:8090").strip()


def _driver_client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=30.0)


def claude_available() -> bool:
    return shutil.which("claude") is not None


def build_prompt(*, uid: str, game_id: str, player: str, api_base: str) -> str:
    """The brief plus this seat's own identifiers, substituted in - the same
    shape a human dispatching a player through `seat_cli` would be given by
    hand (see `docs/ai_seat_brief.md`, adapted from the CLI playtest brief
    this project already uses)."""
    brief = BRIEF_PATH.read_text()
    filled = brief.format(api_repo=str(API_REPO), uid=uid, game_id=game_id, api_base=api_base)
    return (
        f"You are seated as {player!r} in game {game_id}, seat uid {uid}, "
        f"API at {api_base}.\n\n{filled}"
    )


def command_for(prompt: str, agent: AgentSettings) -> list[str]:
    """The argv `claude` is spawned with. `--` is load-bearing - see the
    module docstring for why omitting it breaks the prompt."""
    return [
        "claude",
        "--print",
        *agent.claude_args(),
        "--dangerously-skip-permissions",
        "--allowedTools",
        "Bash",
        "--",
        prompt,
    ]


@dataclass
class AiSeatRunner:
    """One AI seat's subprocess and what is known about it right now."""

    uid: str
    game_id: str
    player: str
    api_base: str
    agent: AgentSettings
    process: subprocess.Popen[str] | None = None
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    returncode: int | None = None
    stdout_tail: list[str] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)
    error: str | None = None
    """Set when the subprocess itself could not be spawned at all - distinct
    from `returncode` (spawned fine, then exited non-zero) so the UI can tell
    "never started" from "ran and then stopped/crashed"."""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def running(self) -> bool:
        if self.process is None:
            return False
        return self.process.poll() is None

    def _pump(self, stream: Any, tail: list[str]) -> None:
        for line in iter(stream.readline, ""):
            with self._lock:
                tail.append(line.rstrip("\n"))
                del tail[:-TAIL_LINES]
        stream.close()

    def start(self) -> None:
        prompt = build_prompt(
            uid=self.uid, game_id=self.game_id, player=self.player, api_base=self.api_base
        )
        try:
            self.process = subprocess.Popen(
                command_for(prompt, self.agent),
                cwd=API_REPO,
                env={**os.environ, **self.agent.env()},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self.error = f"could not spawn claude: {exc}"
            return
        threading.Thread(
            target=self._pump, args=(self.process.stdout, self.stdout_tail), daemon=True
        ).start()
        threading.Thread(
            target=self._pump, args=(self.process.stderr, self.stderr_tail), daemon=True
        ).start()
        threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self) -> None:
        assert self.process is not None
        self.process.wait()
        self.returncode = self.process.returncode
        self.ended_at = time.monotonic()

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()

    def status(self) -> dict[str, Any]:
        with self._lock:
            stdout_tail = list(self.stdout_tail[-20:])
            stderr_tail = list(self.stderr_tail[-20:])
        if self.error is not None:
            state = "error"
        elif self.running():
            state = "thinking"
        elif self.returncode == 0:
            state = "stopped"
        elif self.returncode is not None:
            state = "error"
        else:
            state = "starting"
        return {
            "uid": self.uid,
            "player": self.player,
            "mechanism": "subprocess",
            "state": state,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "returncode": self.returncode,
            "error": self.error,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        }


class AiSeatRegistry:
    """Every AI seat runner this process has started, keyed by seat uid -
    process-lifetime state, same as `store.GameStore`: a restart forgets it,
    and that is fine, since a restarted server has also forgotten every game."""

    def __init__(self) -> None:
        self._runners: dict[str, AiSeatRunner] = {}
        self._dispatched: dict[str, dict[str, Any]] = {}
        #: uid -> why this seat could not be handed to anything at all (no
        #: `claude` on PATH, a driver that did not answer). A seat here is
        #: `GONE` and will stay `GONE`: nothing was started, so nothing is
        #: coming.
        self._unavailable: dict[str, str] = {}
        self._lock = threading.Lock()

    def _dispatch_to_driver(
        self, *, uid: str, game_id: str, player: str, api_base: str, agent: AgentSettings
    ) -> dict[str, Any]:
        """Hand this seat to the driver. Never raises: an unreachable driver
        comes back as a fallback, exactly like a missing `claude` binary, so
        the UI has something to show and the game stays playable."""
        base = driver_url()
        payload = {
            "uid": uid,
            "game_id": game_id,
            "player": player,
            "api_base": api_base,
            "agent": agent.model_dump(),
        }
        try:
            with _driver_client(base) as client:
                response = client.post("/dispatch", json=payload)
                response.raise_for_status()
                answer = response.json()
        except Exception as exc:  # report, never raise - see the docstring
            return {
                "uid": uid,
                "player": player,
                "mechanism": "fallback",
                "state": "not_started",
                "reason": f"the driver at {base} could not be reached: {exc}",
            }
        return {
            "uid": uid,
            "player": player,
            "game_id": game_id,
            "mechanism": "wrapper",
            "state": "dispatched",
            "deck": answer.get("deck"),
            "turn_id": answer.get("turnId"),
            "match_score": answer.get("matchScore"),
            "driver": base,
            "dispatched_at": time.time(),
        }

    def _live_driver_status(self, dispatched: dict[str, Any]) -> dict[str, Any]:
        """`dispatched` refreshed from the driver's own `/status`.

        The dispatch answer is a snapshot of one instant - the instant the
        seat was handed over - and returning it unchanged for the rest of the
        game is how an agent that died, hit an auth error, or simply gave up
        kept reporting `state: "dispatched"` to the play UI forever. The
        driver knows better, so ask it; an unreachable driver is itself worth
        reporting, and never raises past here, because this is on the UI's
        poll path.
        """
        status = dict(dispatched)
        try:
            with _driver_client(status.get("driver") or driver_url()) as client:
                answer = client.get("/status").json()
        except Exception as exc:  # noqa: BLE001 - a poll must not 500 the panel
            return status | {"state": "unreachable", "last_error": f"driver unreachable: {exc}"}
        last_error = answer.get("lastError")
        status["last_error"] = last_error
        status["last_turn_id"] = answer.get("lastTurnId")
        status["reflected"] = bool(answer.get("reflected"))
        if last_error:
            # The one state the UI paints red and offers a way out of.
            status["state"] = "error"
        elif answer.get("pending"):
            status["state"] = "pending"
        return status

    def start(
        self, *, uid: str, game_id: str, player: str, api_base: str, agent: AgentSettings
    ) -> dict[str, Any]:
        """Start (or report the existing) AI seat for `uid`.

        Two mechanisms, chosen by `AI_SEAT_MODE`. `subprocess` (the default)
        spawns a one-shot `claude --print` that forgets the game the moment it
        ends. `wrapper` hands the seat to the driver, which pushes turns into a
        long-lived interactive claude that carries guides and notes across
        games.

        Never raises in either mode: an unavailable `claude` binary, one that
        fails to spawn, or an unreachable driver all come back as
        `mechanism: "fallback"` - see the module docstring. Calling this again
        for a uid already started returns its current status rather than
        starting a second seat.
        """
        mode = ai_seat_mode()
        if mode in ("wrapper", "kubernetes"):
            with self._lock:
                if uid in self._dispatched:
                    return self._dispatched[uid]
            if mode == "kubernetes":
                status = k8s_seats.get().start(uid=uid, game_id=game_id, player=player, agent=agent)
            else:
                status = self._dispatch_to_driver(
                    uid=uid, game_id=game_id, player=player, api_base=api_base, agent=agent
                )
            # Recorded either way: a dispatch that failed used to be entirely
            # silent here - the UI got a "fallback" status and the container
            # log said nothing, so an unreachable driver looked exactly like
            # an agent that was simply thinking.
            if status["mechanism"] in ("wrapper", "kubernetes"):
                with self._lock:
                    self._dispatched[uid] = status
                    # A uid that failed to dispatch once (driver unreachable,
                    # say) and now succeeds is no longer "unavailable" - stale
                    # here would otherwise outlive the seat it described and
                    # surface in `release()`'s `was_unavailable` for a seat
                    # that, in the end, actually ran.
                    self._unavailable.pop(uid, None)
                if status["mechanism"] == "kubernetes":
                    note = f"scheduled as kubernetes Job {status.get('job')}"
                else:
                    note = (
                        f"handed to the driver at {status['driver']}: deck="
                        f"{status.get('deck') or 'unrecognised'} "
                        f"match={status.get('match_score')} turn={status.get('turn_id')}"
                    )
            else:
                note = f"NOT STARTED - {status.get('reason')}"
                with self._lock:
                    self._unavailable[uid] = str(status.get("reason") or "the seat did not start")
            from . import audit  # lazy: ai_seat is imported by modules audit imports

            audit.record(game_id, "ai_seat", player=player, note=note)
            return status

        with self._lock:
            existing = self._runners.get(uid)
        if existing is not None:
            return existing.status()

        prompt = build_prompt(uid=uid, game_id=game_id, player=player, api_base=api_base)
        if not claude_available():
            with self._lock:
                self._unavailable[uid] = "the `claude` CLI is not on PATH in this server process"
            return {
                "uid": uid,
                "player": player,
                "mechanism": "fallback",
                "state": "not_started",
                "reason": "the `claude` CLI is not on PATH in this server process",
                "command": " ".join(command_for(prompt, agent)),
                "prompt": prompt,
            }

        runner = AiSeatRunner(
            uid=uid, game_id=game_id, player=player, api_base=api_base, agent=agent
        )
        runner.start()
        with self._lock:
            self._runners[uid] = runner
            # Same reasoning as the wrapper branch above: a uid that could not
            # start earlier and now has - even if this particular spawn goes
            # on to fail - is no longer the "nothing was ever started" case
            # `_unavailable` exists to record.
            self._unavailable.pop(uid, None)
        status = runner.status()
        if status["state"] == "error":
            # Spawn itself failed (`runner.error` set) - report the fallback
            # command alongside the error, rather than leaving the caller
            # with only "error" and no way to proceed by hand.
            status["mechanism"] = "fallback"
            status["command"] = " ".join(command_for(prompt, agent))
            status["prompt"] = prompt
        return status

    def _driver_activity(self, dispatched: dict[str, Any]) -> str:
        """What the driver (or, in kubernetes mode, `K8sSeats.status`) says
        about a seat it was handed.

        `pending` is the driver still trying to reach an agent container that
        is restarting (up to `AGENT_BOOT_WAIT_SECONDS`) - handed over, not yet
        able to act, so `STARTING`. `turn_active` is the honest "claude has
        this turn and has not finished it" signal: the wrapper plays a whole
        match inside one long turn, so a turn that has *finished* while the
        game is still in its pregame means the agent stopped playing - which
        is the exact shape of the hang `ai_pregame.py` exists for, and is
        `GONE` however healthy the container still looks.

        The kubernetes mechanism is treated the same way: `K8sSeats.status`'s
        `pending`/`thinking` states map to `STARTING`/`WORKING`, and every
        other state (`idle`, `gone`, `error`, or a seat `K8sSeats` no longer
        knows about at all) maps to `GONE`.
        """
        if dispatched.get("mechanism") == "kubernetes":
            status = k8s_seats.get().status(dispatched["uid"])
            if status is None:
                return GONE
            state = status.get("state")
            if state == "pending":
                return STARTING
            if state == "thinking":
                return WORKING
            return GONE
        try:
            with _driver_client(dispatched.get("driver") or driver_url()) as client:
                answer = client.get("/status").json()
        except Exception:  # noqa: BLE001 - an unreachable driver is a gone seat
            return GONE
        if answer.get("lastError"):
            return GONE
        if answer.get("pending"):
            return STARTING
        if answer.get("turnActive"):
            return WORKING
        # An older driver that does not report `turnActive` at all: fall back
        # to "it was dispatched and has not errored", which is the most this
        # can honestly claim. Never silently upgraded to WORKING on a missing
        # key - `lastTurnId` being set is what says a turn was ever pushed.
        return WORKING if answer.get("lastTurnId") and "turnActive" not in answer else GONE

    def activity(self, uid: str) -> str:
        """Is anything actually playing this seat right now: `WORKING`,
        `STARTING` or `GONE`.

        The question `ai_pregame.py`'s backstop needs answered before it takes
        a decision away from a seat. It is deliberately *not* the same as
        `status`, which is written for a human reading a panel: this returns
        one of three words with a defined meaning for that one caller, and
        errs towards `GONE` for anything it cannot establish - a seat nobody
        ever started, a driver that does not answer - because the failure of
        `GONE` is a bot's default keep, while the failure of `WORKING` is a
        human stranded at a panel with no button that moves.
        """
        with self._lock:
            dispatched = self._dispatched.get(uid)
            runner = self._runners.get(uid)
        if dispatched is not None:
            return self._driver_activity(dispatched)
        if runner is None:
            # Never started, or started and refused (`_unavailable`). Either
            # way nothing is playing this seat.
            return GONE
        if runner.error is not None or runner.returncode is not None:
            return GONE
        return WORKING if runner.running() else STARTING

    def why_gone(self, uid: str) -> str:
        """One line of evidence for a `GONE` verdict, for the audit log.

        The backstop declaring for a seat is a decision taken away from a
        player; a transcript that says only "automatic" leaves nobody able to
        tell a crashed agent from a missing binary from an agent that quietly
        gave up.
        """
        with self._lock:
            dispatched = self._dispatched.get(uid)
            runner = self._runners.get(uid)
            unavailable = self._unavailable.get(uid)
        if dispatched is not None:
            return "the driver reports no live turn for this seat"
        if runner is not None:
            if runner.error is not None:
                return runner.error
            if runner.returncode is not None:
                return f"the seat's claude subprocess exited {runner.returncode}"
            return "the seat's claude subprocess is not running"
        if unavailable is not None:
            return unavailable
        return "no process or agent was ever started for this seat"

    def status(self, uid: str) -> dict[str, Any] | None:
        with self._lock:
            dispatched = self._dispatched.get(uid)
            runner = self._runners.get(uid)
        if dispatched is not None:
            if dispatched.get("mechanism") == "kubernetes":
                return k8s_seats.get().status(uid)
            return self._live_driver_status(dispatched)
        return runner.status() if runner is not None else None

    def stop(self, uid: str) -> bool:
        with self._lock:
            dispatched = self._dispatched.get(uid)
            runner = self._runners.get(uid)
        if dispatched is not None and dispatched.get("mechanism") == "kubernetes":
            k8s_seats.get().stop(uid)
            return True
        if runner is None:
            return False
        runner.stop()
        return True

    def _release_from_driver(self, dispatched: dict[str, Any]) -> dict[str, Any]:
        """Tell the driver this seat is over, so the agent is free for the
        next game.

        This is the same end-of-match path the driver already runs when a game
        finishes (`retire`): the assignment is dropped and the agent's session
        ends, so the next dispatch gets a container with no memory of the
        abandoned game. It does NOT touch anything the continuous agent keeps
        on disk - the notes under `./memory` are a mounted volume neither this
        call nor the driver ever writes to. Never raises: a driver we cannot
        reach is reported, not propagated into the lobby's request.
        """
        base = dispatched.get("driver") or driver_url()
        payload = {"uid": dispatched.get("uid"), "game_id": dispatched.get("game_id")}
        try:
            with _driver_client(base) as client:
                response = client.post("/release", json=payload)
                if response.status_code >= 400:
                    return {
                        "released": False,
                        "reason": f"the driver answered {response.status_code}",
                    }
                return {"released": True, "driver": base, **response.json()}
        except Exception as exc:  # noqa: BLE001 - an unreachable driver is a released seat
            return {
                "released": False,
                "reason": f"the driver at {base} could not be reached: {exc}",
            }

    def release(self, uid: str) -> dict[str, Any]:
        """Forget this seat entirely, and stop whatever was playing it.

        What "reset the AI seat" means for one uid: a `subprocess`-mode runner
        is terminated, a `wrapper`-mode dispatch is handed back to the driver,
        a `kubernetes`-mode dispatch tells the driver sidecar then deletes the
        Job, and every record this registry kept under the uid - runner,
        dispatch, the reason it was unavailable - is dropped, so a stale
        status can never be polled back out of a finished game. The agent's
        own memory on disk is untouched; see `_release_from_driver` and
        `K8sSeats.release`.
        """
        with self._lock:
            runner = self._runners.pop(uid, None)
            dispatched = self._dispatched.pop(uid, None)
            unavailable = self._unavailable.pop(uid, None)
        mechanism = dispatched.get("mechanism") if dispatched is not None else None
        if mechanism is None and runner is not None:
            mechanism = "subprocess"
        released: dict[str, Any] = {
            "uid": uid,
            "mechanism": mechanism,
            "subprocess_stopped": False,
            "driver": None,
            "was_unavailable": unavailable,
        }
        if runner is not None:
            released["subprocess_stopped"] = runner.running()
            runner.stop()
        if dispatched is not None:
            if mechanism == "kubernetes":
                released["driver"] = k8s_seats.get().release(uid)
            else:
                released["driver"] = self._release_from_driver(dispatched)
        return released


registry = AiSeatRegistry()
