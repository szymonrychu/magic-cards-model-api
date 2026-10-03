"""Defect 5: the SIGTERM hold is bounded by HANDOVER_HOLD_SECONDS, and the
claude process group does not outlive the wrapper."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

from mtg_agent.wrapper import handover_wait
from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.pty_proc import spawn_claude

STUB = Path(__file__).resolve().parents[1] / "testdata" / "stubclaude.py"
SRC = Path(__file__).resolve().parents[2] / "src"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie is gone for our purposes
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        return bool(out.stdout.strip()) and not out.stdout.strip().startswith("Z")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_default_hold_is_the_handover_wait_default():
    assert Settings.from_env({}).handover_hold_seconds == handover_wait.DEFAULT_WAIT_SECONDS


def test_the_hold_is_read_from_the_environment():
    assert Settings.from_env({"HANDOVER_HOLD_SECONDS": "0"}).handover_hold_seconds == 0.0
    assert Settings.from_env({"HANDOVER_HOLD_SECONDS": "12.5"}).handover_hold_seconds == 12.5


def test_zero_closes_at_once_even_with_the_workspace_mounted(tmp_path):
    calls: list[str] = []
    handover_wait.hold_until_handover(
        lambda: calls.append("closed"),
        tmp_path / "handover.done",
        0,
        clock=lambda: pytest.fail("a zero hold must not poll"),
        sleep=lambda _s: pytest.fail("a zero hold must not sleep"),
    )
    assert calls == ["closed"]


def test_no_dispatched_assignment_skips_the_hold(tmp_path):
    calls: list[str] = []
    handover_wait.hold_until_handover(
        lambda: calls.append("closed"),
        tmp_path / "handover.done",
        500,
        dispatched=False,
        clock=lambda: pytest.fail("nothing was dispatched, nothing to hand over"),
        sleep=lambda _s: pytest.fail("nothing was dispatched, nothing to hand over"),
    )
    assert calls == ["closed"]


def test_the_manager_knows_whether_an_assignment_was_dispatched(monkeypatch):
    from tests.wrapper.test_session import FakeProc, ready_manager

    manager = ready_manager(monkeypatch, FakeProc(b"ready"))
    try:
        assert manager.dispatched is False
        manager.submit("turn zero")
        assert manager.dispatched is True
    finally:
        manager.shutdown()


def test_an_existing_marker_skips_the_hold(tmp_path):
    marker = tmp_path / "handover.done"
    marker.write_text("{}")
    calls: list[str] = []
    handover_wait.hold_until_handover(
        lambda: calls.append("closed"),
        marker,
        500,
        sleep=lambda _s: pytest.fail("the marker is already there"),
    )
    assert calls == ["closed"]


def test_close_kills_the_whole_process_group_and_escalates_to_sigkill(tmp_path):
    """A claude that ignores SIGTERM, and a grandchild of it, are both gone
    within the 5 s SIGTERM grace plus a margin."""
    pids = tmp_path / "pids"
    script = tmp_path / "stubborn.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import os, signal, subprocess, sys, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            kid = subprocess.Popen(["sleep", "300"])
            open({str(pids)!r}, "w").write(f"{{os.getpid()}} {{kid.pid}}")
            print("up", flush=True)
            time.sleep(300)
            """
        )
    )
    settings = Settings.from_env({"CLAUDE_PATH": sys.executable, "WORKSPACE": str(tmp_path)})
    proc = spawn_claude(settings, resume=False, extra_args=[str(script)])
    deadline = time.monotonic() + 10
    while not pids.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    child, grandchild = (int(p) for p in pids.read_text().split())
    started = time.monotonic()
    proc.close()
    proc.wait()
    assert time.monotonic() - started < 8
    assert not alive(child)
    deadline = time.monotonic() + 2
    while alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(grandchild)


@pytest.fixture
def wrapper(tmp_path):
    workspace, home = tmp_path / "workspace", tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    pidfile = tmp_path / "claude.pid"
    claude = tmp_path / "claude"
    claude.write_text(f'#!/bin/sh\necho $$ > "{pidfile}"\nexec "{sys.executable}" "{STUB}" "$@"\n')
    claude.chmod(0o755)
    port, internal = free_port(), free_port()
    env = os.environ | {
        "PYTHONPATH": str(SRC),
        "CLAUDE_PATH": str(claude),
        "WORKSPACE": str(workspace),
        "HOME_DIR": str(home),
        "HTTP_ADDR": f"127.0.0.1:{port}",
        "INTERNAL_ADDR": f"127.0.0.1:{internal}",
        "HOOK_COMMAND": f"{sys.executable} -c pass",
        "ANTHROPIC_API_KEY": "sk-ant-stub-key-0123456789",
        "BOOT_TIMEOUT_SECONDS": "45",
        "HANDOVER_HOLD_SECONDS": "0",
        "STUB_HOOK_COMMAND": "true",
    }
    proc = subprocess.Popen([sys.executable, "-m", "mtg_agent.wrapper"], env=env)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/readyz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("wrapper never became ready")
    yield proc, pidfile
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def test_sigterm_with_a_zero_hold_exits_within_6s_with_the_child_gone(wrapper):
    proc, pidfile = wrapper
    claude_pid = int(pidfile.read_text())
    assert alive(claude_pid)
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=6)
    assert time.monotonic() - started < 6
    assert not alive(claude_pid)
