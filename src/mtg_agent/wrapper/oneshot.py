"""A single, non-interactive `claude --print` call - the post-game AI
review's own mechanism (`POST /v1/review` in `api.py`), and deliberately
never the supervised session `session.py`/`pty_proc.py` own.

`pty_proc.py`'s own module docstring says the wrapper never runs `claude -p`
for the *play* session - print mode assembles a different system prompt and
diverges on skills, hooks and permissions, and the whole point of that
component is that the agent plays in the harness a human gets. That
constraint is about the play session specifically; it says nothing about
this module, which exists for an unrelated, one-time job the play session
was never meant to do: read a finished game's own reflection and record, and
write a critique. Nothing here touches `session.Manager`, the turn state
machine, or the PTY at all - this is a plain blocking subprocess call,
bounded by `Settings.review_timeout`.

**Fully isolated from the play session, on purpose - this is load-bearing,
not incidental.** `bootstrap.write_settings_json` writes `settings.home_dir /
".claude" / "settings.json"` with the play session's own Stop hook
(`hooks.Stop[0].hooks[0].command = settings.hook_command`, normally
`cc-stop-hook`). A `claude --print` invoked with that SAME `HOME` would read
that same file and fire that same hook the moment it finishes - and
`cc-stop-hook` (`wrapper/stop_hook.py`) reports a turn completion to
`CCW_INTERNAL_URL`, hardcoded-defaulting to this wrapper's OWN internal
`/internal/turn-complete` listener even with that env var unset, since the
default happens to match `Settings.internal_addr`'s own default. A review's
"turn" has no real turn id behind it, and an out-of-band completion call
with an empty or wrong one can be mistaken for the completion of whatever
turn - possibly the handover turn - actually is in flight
(`session.Manager.complete`). So this never reuses `settings.home_dir`,
never reuses `settings.workspace` as `cwd` (which can carry its own
project-level `.mcp.json` from `bootstrap.merge_mcp`), and never inherits
`HOOK_COMMAND`/`CCW_INTERNAL_URL`/`CCW_RESULT_JSON` from this process's own
environment: `_isolated_home` makes a fresh, empty temp directory per call
(removed again once the call returns, success or failure), and `_env_for`
builds the child's env from nothing rather than `dict(os.environ)`, naming
only what a one-shot `claude --print` genuinely needs (`PATH`, the isolated
`HOME`, and whichever auth credential this deployment carries).

**No tools, no MCP servers, no permission bypass.** The review reads a
finished game's own record and reflection and writes a critique - it never
needs to touch a file, run a command, or call an MCP server, and the game
record can contain opponent-written or AI-generated text (a declared plan, a
piece of advice). `--tools ""` disables every built-in tool (`claude
--help`'s own words: 'Use "" to disable all tools' - verified against the
installed CLI, not assumed), `--strict-mcp-config` with no `--mcp-config`
loads zero MCP servers, and with nothing left to ask permission for,
`--dangerously-skip-permissions` is not passed at all - unlike `ai_seat.py`'s
own one-shot invocation, which plays an actual seat through the `Bash` tool
and genuinely needs it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import Settings

#: A generous ceiling on the prompt this call ever sends as a bare argv
#: element - not a realistic size (a game record plus a reflection,
#: `driver/review.py`'s own `MAX_DECLARATIONS` cap included, comes nowhere
#: close), but a backstop against passing something pathological straight
#: through to `subprocess.run`'s argv, which the OS itself bounds
#: (`ARG_MAX`) and would otherwise fail with a confusing `OSError` instead
#: of this module's own, clearer `ReviewFailed`.
MAX_PROMPT_CHARS = 200_000

#: Never passed through from this process's own environment - see the
#: module docstring's isolation section. `HOOK_COMMAND` configures which
#: script the Stop hook runs; `CCW_INTERNAL_URL`/`CCW_RESULT_JSON` are only
#: `cc-stop-hook`'s own targets, read straight off `os.environ` by that
#: script (`wrapper/stop_hook.py`), never through `Settings`.
_NEVER_INHERITED = ("HOOK_COMMAND", "CCW_INTERNAL_URL", "CCW_RESULT_JSON")


class ReviewFailed(Exception):
    """`claude` exited non-zero, could not be found, could not be executed,
    or ran past `Settings.review_timeout` - the one exception `run_review`
    raises, so `api.py`'s route has one thing to catch rather than four."""


def _env_for(settings: Settings, home: Path) -> dict[str, str]:
    """A minimal env, built from nothing rather than `dict(os.environ)`:
    `PATH` (to find `claude` and whatever it shells out to), the isolated
    `home` as `HOME`, and whichever auth credential this deployment carries
    - the same precedence `pty_proc.spawn_claude` applies (OAuth token wins
    over an API key). Everything else this process's own environment
    carries, `_NEVER_INHERITED` included, is deliberately left out rather
    than filtered after the fact - a future env var added to the wrapper's
    own process has to be named here to reach the child at all, not merely
    remembered to be stripped."""
    env: dict[str, str] = {"HOME": str(home)}
    path = os.environ.get("PATH")
    if path:
        env["PATH"] = path
    if settings.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
    elif settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    return env


def _capped(prompt: str) -> str:
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    return prompt[:MAX_PROMPT_CHARS] + "\n\n[truncated: the game record was too large]"


def run_review(settings: Settings, prompt: str, model: str, effort: str) -> str:
    """Run a bounded, isolated, tool-less `claude --print` and return its
    stdout, stripped.

    Raises `ReviewFailed` for every way this can go wrong - a nonzero exit
    (stderr's tail in the message), the timeout, `claude` missing from
    `PATH`, or any other `OSError` spawning it (permission denied on the
    binary is the seen-in-production case, but this covers the class rather
    than that one instance) - so the caller (`api.post_review`) has one
    exception to turn into a 502/504 rather than four call sites each
    guessing which failure mode it is looking at."""
    home = Path(tempfile.mkdtemp(prefix="mtg-review-home-"))
    try:
        argv = [
            settings.claude_path,
            "--print",
            "--model",
            model,
            "--effort",
            effort,
            "--tools",
            "",
            "--strict-mcp-config",
            "--",
            _capped(prompt),
        ]
        try:
            result = subprocess.run(
                argv,
                cwd=home,
                env=_env_for(settings, home),
                capture_output=True,
                text=True,
                timeout=settings.review_timeout,
            )
        except FileNotFoundError as exc:
            raise ReviewFailed(f"{settings.claude_path} is not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ReviewFailed(
                f"the review did not finish within {settings.review_timeout}s"
            ) from exc
        except OSError as exc:
            raise ReviewFailed(f"could not run {settings.claude_path}: {exc}") from exc
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if result.returncode != 0:
        # The real `claude` CLI writes some failures - an auth error among
        # them - to STDOUT, not stderr; building this from stderr alone left
        # a bare "claude exited 1" in production logs with nothing telling
        # an operator why, the one time they most need to know. Both
        # streams are captured either way, so both are read here - stdout
        # first, since a `--print` process on a genuine failure path prints
        # nothing else there for this to be confused with.
        combined = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
        )
        tail = combined[-2000:]
        raise ReviewFailed(
            f"claude exited {result.returncode}: {tail}"
            if tail
            else f"claude exited {result.returncode}"
        )
    return result.stdout.strip()
