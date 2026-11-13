"""One container, one match: the real process must actually exit.

`DELETE /v1/session` used to shut the session down and keep serving, so the
container never exited and its restart policy never fired. These tests run
the real `python -m mtg_agent.wrapper` against the stub claude and assert the
process is gone afterwards - the only assertion that can tell the difference.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

STUB = Path(__file__).resolve().parents[1] / "testdata" / "stubclaude.py"
SRC = Path(__file__).resolve().parents[2] / "src"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def children_running(parent: int, script: Path) -> list[int]:
    """Pids of `parent`'s direct children whose command line names `script`.

    Read from `/proc` rather than with `pkill -P <parent> -f <script>`, which
    did the same: procps is missing from some test containers (the
    agent pods), and a `FileNotFoundError` for `pkill` failed this test there
    on every run, whatever the order."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue  # exited while we looked
        # Field 4 is the parent pid; the command name before it is in
        # parentheses and may itself contain spaces.
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        if ppid == parent and str(script).encode() in cmdline:
            found.append(int(entry.name))
    return found


def wait_until(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def wrapper_process(tmp_path):
    """The wrapper as it runs in the container: its own process, its own
    listener, a stub standing in for the claude binary."""
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()

    claude = tmp_path / "claude"
    claude.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STUB}" "$@"\n')
    claude.chmod(0o755)

    port = free_port()
    internal_port = free_port()
    hook = (
        f"PYTHONPATH={SRC} CCW_INTERNAL_URL=http://127.0.0.1:{internal_port}"
        f"/internal/turn-complete CCW_RESULT_JSON={workspace / 'result.json'} "
        f"{sys.executable} -m mtg_agent.wrapper.stop_hook"
    )
    env = os.environ | {
        "PYTHONPATH": str(SRC),
        "CLAUDE_PATH": str(claude),
        "WORKSPACE": str(workspace),
        "HOME_DIR": str(home),
        "HTTP_ADDR": f"127.0.0.1:{port}",
        "INTERNAL_ADDR": f"127.0.0.1:{internal_port}",
        "HOOK_COMMAND": hook,
        "ANTHROPIC_API_KEY": "sk-ant-stub-key-0123456789",
        "BOOT_TIMEOUT_SECONDS": "45",
        "LOG_LEVEL": "info",
        "STUB_HOOK_COMMAND": hook,
    }
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "mtg_agent.wrapper"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"

    def alive() -> bool:
        if proc.poll() is not None:
            raise AssertionError(f"the wrapper died while booting: {proc.communicate()[0]}")
        try:
            return httpx.get(f"{base}/readyz", timeout=1.0).status_code == 200
        except httpx.HTTPError:
            return False

    try:
        wait_until(alive, 60, "the wrapper to report ready")
        yield proc, base
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_delete_session_exits_the_process_so_the_restart_policy_can_fire(wrapper_process):
    proc, base = wrapper_process
    response = httpx.delete(f"{base}/v1/session", timeout=5.0)
    assert response.status_code == 202
    assert response.json() == {"stopping": True, "exiting": True}

    assert proc.wait(timeout=30) == 0, "the container must exit, not linger with a dead session"

    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{base}/healthz", timeout=1.0)


def test_a_claude_that_dies_takes_the_container_with_it(wrapper_process):
    """A session marked dead can never serve another turn. Exiting recycles
    the container; staying up would leave it 'running' and useless."""
    proc, base = wrapper_process
    session = httpx.get(f"{base}/v1/session", timeout=5.0).json()
    assert session["state"] == "ready"

    # Kill the stub claude itself, not the wrapper: this is the crash case.
    # Scoped to a child of THIS test's wrapper process - a machine-wide match
    # on the command line alone, under parallel test workers each running
    # their own `stack`-fixture stub (tests/wrapper/test_integration.py),
    # killed an unrelated worker's in-flight stub process too (pty.fork()
    # makes the stub a direct child of `proc`, so its parent pid is exact).
    stubs = children_running(proc.pid, STUB)
    assert stubs, "the wrapper has no stub claude child to kill"
    for pid in stubs:
        os.kill(pid, signal.SIGTERM)

    assert proc.wait(timeout=30) == 0
