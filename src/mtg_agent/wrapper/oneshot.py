"""A single, non-interactive `claude --print` call - the post-game AI
review's own mechanism (`POST /v1/review` in `api.py`), and deliberately
never the supervised session `session.py`/`pty_proc.py` own.

`pty_proc.py`'s own module docstring says the wrapper never runs `claude -p`
for the *play* session - print mode assembles a different system prompt and
diverges on skills, hooks and permissions, and the whole point of that
component is that the agent plays in the harness a human gets. That
constraint is about the play session specifically; it says nothing about
this module, which exists for an unrelated, one-time job the play session
was never meant to do: read a finished game's own reflection and record,
and write a critique, with no tool access and no ongoing conversation to
diverge from. Nothing here touches `session.Manager`, the turn state
machine, or the PTY at all - this is a plain blocking subprocess call,
bounded by `Settings.review_timeout`.

`--dangerously-skip-permissions` is carried over from `ai_seat.py`'s own
one-shot invocation (the "AI seat plays through one `claude --print`
subprocess" mechanism that module's docstring documents) even though this
call asks for no tools: a one-shot process that is not there to answer an
interactive permission prompt must never be able to block on one.
"""

from __future__ import annotations

import os
import subprocess

from .config import Settings


class ReviewFailed(Exception):
    """`claude` exited non-zero, could not be found, or ran past
    `Settings.review_timeout` - the one exception `run_review` raises, so
    `api.py`'s route has one thing to catch rather than three."""


def _env_for(settings: Settings) -> dict[str, str]:
    """The same auth precedence `pty_proc.spawn_claude` applies (OAuth token
    wins over an API key, and the loser is popped so it cannot shadow the
    winner) - duplicated rather than imported from there, since that
    function builds a pty-specific env (`HOME`, `MAX_THINKING_TOKENS`) this
    one-shot call has no use for and should not inherit by accident."""
    env = dict(os.environ)
    if settings.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
        env.pop("ANTHROPIC_API_KEY", None)
    elif settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env.setdefault("HOME", str(settings.home_dir))
    return env


def run_review(settings: Settings, prompt: str, model: str, effort: str) -> str:
    """Run `claude --print --model <model> --effort <effort> -- <prompt>` as
    a single blocking subprocess, bounded by `settings.review_timeout`, and
    return its stdout, stripped.

    Raises `ReviewFailed` for every way this can go wrong - a nonzero exit
    (stderr's tail in the message), the timeout, or `claude` missing from
    `PATH` - so the caller (`api.post_review`) has one exception to turn
    into a 502/504 rather than three call sites each guessing which failure
    mode it is looking at."""
    argv = [
        settings.claude_path,
        "--print",
        "--dangerously-skip-permissions",
        "--model",
        model,
        "--effort",
        effort,
        "--",
        prompt,
    ]
    try:
        result = subprocess.run(
            argv,
            cwd=settings.workspace,
            env=_env_for(settings),
            capture_output=True,
            text=True,
            timeout=settings.review_timeout,
        )
    except FileNotFoundError as exc:
        raise ReviewFailed(f"{settings.claude_path} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewFailed(f"the review did not finish within {settings.review_timeout}s") from exc
    if result.returncode != 0:
        tail = (result.stderr or "").strip()[-2000:]
        raise ReviewFailed(
            f"claude exited {result.returncode}: {tail}"
            if tail
            else f"claude exited {result.returncode}"
        )
    return result.stdout.strip()
