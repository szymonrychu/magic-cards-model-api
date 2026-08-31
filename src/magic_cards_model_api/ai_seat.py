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


def ai_seat_mode() -> str:
    """`subprocess` (the default, a one-shot `claude --print` per game) or
    `wrapper` (hand the seat to the driver, which drives a long-lived
    interactive claude that remembers previous games)."""
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


def command_for(prompt: str) -> list[str]:
    """The argv `claude` is spawned with. `--` is load-bearing - see the
    module docstring for why omitting it breaks the prompt."""
    return [
        "claude",
        "--print",
        "--model",
        "sonnet",
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
                command_for(prompt),
                cwd=API_REPO,
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
        self._lock = threading.Lock()

    def _dispatch_to_driver(
        self, *, uid: str, game_id: str, player: str, api_base: str
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
            "mechanism": "wrapper",
            "state": "dispatched",
            "deck": answer.get("deck"),
            "turn_id": answer.get("turnId"),
            "match_score": answer.get("matchScore"),
            "driver": base,
        }

    def start(self, *, uid: str, game_id: str, player: str, api_base: str) -> dict[str, Any]:
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
        if ai_seat_mode() == "wrapper":
            with self._lock:
                if uid in self._dispatched:
                    return self._dispatched[uid]
            status = self._dispatch_to_driver(
                uid=uid, game_id=game_id, player=player, api_base=api_base
            )
            # Recorded either way: a dispatch that failed used to be entirely
            # silent here - the UI got a "fallback" status and the container
            # log said nothing, so an unreachable driver looked exactly like
            # an agent that was simply thinking.
            if status["mechanism"] == "wrapper":
                with self._lock:
                    self._dispatched[uid] = status
                note = (
                    f"handed to the driver at {status['driver']}: deck="
                    f"{status.get('deck') or 'unrecognised'} match={status.get('match_score')} "
                    f"turn={status.get('turn_id')}"
                )
            else:
                note = f"NOT STARTED - {status.get('reason')}"
            from . import audit  # lazy: ai_seat is imported by modules audit imports

            audit.record(game_id, "ai_seat", player=player, note=note)
            return status

        with self._lock:
            existing = self._runners.get(uid)
        if existing is not None:
            return existing.status()

        prompt = build_prompt(uid=uid, game_id=game_id, player=player, api_base=api_base)
        if not claude_available():
            return {
                "uid": uid,
                "player": player,
                "mechanism": "fallback",
                "state": "not_started",
                "reason": "the `claude` CLI is not on PATH in this server process",
                "command": " ".join(command_for(prompt)),
                "prompt": prompt,
            }

        runner = AiSeatRunner(uid=uid, game_id=game_id, player=player, api_base=api_base)
        runner.start()
        with self._lock:
            self._runners[uid] = runner
        status = runner.status()
        if status["state"] == "error":
            # Spawn itself failed (`runner.error` set) - report the fallback
            # command alongside the error, rather than leaving the caller
            # with only "error" and no way to proceed by hand.
            status["mechanism"] = "fallback"
            status["command"] = " ".join(command_for(prompt))
            status["prompt"] = prompt
        return status

    def status(self, uid: str) -> dict[str, Any] | None:
        with self._lock:
            dispatched = self._dispatched.get(uid)
            runner = self._runners.get(uid)
        if dispatched is not None:
            return dispatched
        return runner.status() if runner is not None else None

    def stop(self, uid: str) -> bool:
        with self._lock:
            runner = self._runners.get(uid)
        if runner is None:
            return False
        runner.stop()
        return True


registry = AiSeatRegistry()
