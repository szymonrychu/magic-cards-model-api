"""Spawn interactive `claude` with a real controlling terminal.

`pty.fork()` rather than a pipe or a bare `openpty`: the child must be a
session leader whose controlling terminal is the pty slave, or the TUI does
not behave like a TUI. This is the Python equivalent of the Go original's
creack/pty `Start`.

The wrapper never runs `claude -p`. Print mode assembles a different system
prompt and diverges on skills, hooks and permissions; the whole point of this
component is that the agent runs in the harness a human gets.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import pty
import signal
import struct
import termios
from typing import Protocol

from .config import Settings

ROWS = 50
COLS = 200


class ClaudeProcess(Protocol):
    """The seam the session supervises. Tests substitute a fake."""

    pid: int

    def read(self, n: int) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def wait(self) -> int: ...
    def close(self) -> None: ...


class PtyProcess:
    def __init__(self, pid: int, master_fd: int) -> None:
        self.pid = pid
        self._fd = master_fd
        self._status: int | None = None

    def read(self, n: int) -> bytes:
        try:
            return os.read(self._fd, n)
        except OSError as exc:
            if exc.errno == errno.EIO:  # the slave side closed: normal EOF
                return b""
            raise

    def write(self, data: bytes) -> int:
        return os.write(self._fd, data)

    def wait(self) -> int:
        if self._status is None:
            _, status = os.waitpid(self.pid, 0)
            self._status = os.waitstatus_to_exitcode(status)
        return self._status

    def close(self) -> None:
        """Signal the child FIRST, then close the master.

        The order is load-bearing. The session runs a reader thread parked in
        `os.read` on this same fd; on macOS `os.close` then blocks until that
        read returns, and the read cannot return while the child still holds
        the slave open - the two wait on each other forever. Killing the
        child drops the last slave reference, the parked read gets EIO and
        returns, and the close completes.
        """
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(self.pid, signal.SIGTERM)
        with contextlib.suppress(OSError):
            os.close(self._fd)


def _set_winsize(fd: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))


def spawn_claude(
    settings: Settings, resume: bool, *, extra_args: list[str] | None = None
) -> PtyProcess:
    """Fork `claude` under a pty and return the master side.

    `extra_args` exists for tests, which spawn a stub interpreter script
    rather than the real binary; production callers never pass it.
    """
    argv = [settings.claude_path, *(extra_args or []), *settings.claude_args(resume)]
    env = dict(os.environ)
    env.setdefault("HOME", str(settings.home_dir))
    # Either credential, never both: a `claude setup-token` OAuth token and an
    # API key authenticate differently, and an ANTHROPIC_API_KEY left in the
    # environment takes precedence over the token, so passing both makes a
    # valid token fail. The token wins when it is set, and the key is cleared
    # out of the inherited environment so it cannot shadow it.
    if settings.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
        env.pop("ANTHROPIC_API_KEY", None)
    elif settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    # Set explicitly rather than left to `claude_args`: there is no CLI flag
    # for it, only this env var. Popped when None so a `restart()` that
    # clears the budget does not inherit the boot env's value - `env` starts
    # as a copy of `os.environ`, which still carries whatever this container
    # booted with.
    if settings.max_thinking_tokens is not None:
        env["MAX_THINKING_TOKENS"] = str(settings.max_thinking_tokens)
    else:
        env.pop("MAX_THINKING_TOKENS", None)

    pid, master_fd = pty.fork()
    if pid == 0:  # child
        try:
            os.chdir(settings.workspace)
            os.execvpe(argv[0], argv, env)
        except Exception:  # noqa: BLE001 - the child must never return
            os._exit(127)
    _set_winsize(master_fd)
    return PtyProcess(pid, master_fd)
