import os
import sys
import time

from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.pty_proc import spawn_claude


def read_until(proc, needle: bytes, timeout: float = 20.0) -> bytes:
    seen = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = proc.read(4096)
        except OSError:
            break
        if not chunk:
            break
        seen += chunk
        if needle in seen:
            return seen
    return seen


def test_the_child_gets_a_real_tty_and_the_configured_size(tmp_path):
    """A pipe would make claude render non-interactively. The child must see
    a tty on stdin and the 50x200 winsize the wrapper sets."""
    script = tmp_path / "probe.py"
    script.write_text(
        "import os, sys\n"
        "size = os.get_terminal_size(0)\n"
        "print('TTY', os.isatty(0), size.lines, size.columns, flush=True)\n"
    )
    settings = Settings.from_env(
        {"CLAUDE_PATH": sys.executable, "WORKSPACE": str(tmp_path), "MODEL": ""}
    )
    proc = spawn_claude(settings, resume=False, extra_args=[str(script)])
    try:
        assert b"TTY True 50 200" in read_until(proc, b"TTY")
    finally:
        proc.close()
        proc.wait()


def test_writes_reach_the_child_and_wait_returns_its_exit_code(tmp_path):
    script = tmp_path / "echo.py"
    script.write_text(
        "import sys\n"
        "line = sys.stdin.readline().strip()\n"
        "print('GOT:' + line, flush=True)\n"
        "sys.exit(3)\n"
    )
    settings = Settings.from_env({"CLAUDE_PATH": sys.executable, "WORKSPACE": str(tmp_path)})
    proc = spawn_claude(settings, resume=False, extra_args=[str(script)])
    try:
        proc.write(b"hello\r")
        assert b"GOT:hello" in read_until(proc, b"GOT:")
        assert proc.wait() == 3
    finally:
        proc.close()


def test_the_child_runs_in_the_workspace_directory(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    script = tmp_path / "cwd.py"
    script.write_text("import os; print('CWD', os.getcwd(), flush=True)\n")
    settings = Settings.from_env({"CLAUDE_PATH": sys.executable, "WORKSPACE": str(workspace)})
    proc = spawn_claude(settings, resume=False, extra_args=[str(script)])
    try:
        assert os.path.realpath(str(workspace)).encode() in read_until(proc, b"CWD")
    finally:
        proc.close()
        proc.wait()
