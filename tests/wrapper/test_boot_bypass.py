"""Defect 4: with bypassPermissions, boot never goes READY on quiet alone.

The first boot of a container printed the bypass dialog later than
`MIN_BOOT + IDLE_READY` of quiet, boot_wait declared READY, the first turn's
submit landed on the dialog and claude exited with code 1.
"""

from __future__ import annotations

import threading
import time

import pytest

from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.metrics import Metrics
from mtg_agent.wrapper.session import DeadError, Manager, State
from mtg_agent.wrapper.turns import Store
from tests.wrapper.test_session import FakeProc, fast_settings

DIALOG = b"\x1b[2J Bypass\x1b[3C Permissions   mode \x1b[0m"


@pytest.fixture(autouse=True)
def quick_idle(monkeypatch):
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.05)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 0.1)


def bypass(**overrides) -> Settings:
    return fast_settings(PERMISSION_MODE="bypassPermissions", **overrides)


def spawning(*procs: FakeProc):
    queue = list(procs)
    spawned: list[FakeProc] = []

    def spawn(_settings, _resume):
        proc = queue.pop(0)
        spawned.append(proc)
        return proc

    return spawn, spawned


def test_a_dialog_8s_after_an_idle_quiet_period_is_still_accepted():
    proc = FakeProc(b"claude is starting")
    threading.Timer(8.0, lambda: proc.feed(DIALOG)).start()
    manager = Manager(bypass(BOOT_TIMEOUT_SECONDS="20"), Store(), Metrics(), spawn=lambda *_: proc)
    started = time.monotonic()
    manager.start()
    try:
        assert time.monotonic() - started >= 8.0, "went ready on idle before the dialog"
        assert manager.state is State.READY
        assert proc.writes[:2] == [b"\x1b[B", b"\r"]
        assert proc._closed is False
    finally:
        manager.shutdown()


def test_the_ready_prompt_marker_alone_is_enough_without_a_dialog():
    proc = FakeProc(b"\xe2\x8f\xb5\xe2\x8f\xb5 bypass permissions on (shift+tab to cycle)")
    manager = Manager(bypass(), Store(), Metrics(), spawn=lambda *_: proc)
    manager.start()
    try:
        assert manager.state is State.READY
        assert proc.writes == []
    finally:
        manager.shutdown()


def test_no_dialog_by_the_deadline_restarts_claude_once_and_retries():
    first, second = FakeProc(b"nothing useful"), FakeProc(DIALOG)
    spawn, spawned = spawning(first, second)
    manager = Manager(bypass(BOOT_TIMEOUT_SECONDS="0.5"), Store(), Metrics(), spawn=spawn)
    manager.start()
    try:
        assert spawned == [first, second]
        assert first._closed is True
        assert manager.state is State.READY
        assert second.writes[:2] == [b"\x1b[B", b"\r"]
        assert first.writes == []
    finally:
        manager.shutdown()


def test_two_boots_without_the_dialog_end_dead_never_ready():
    spawn, spawned = spawning(FakeProc(b"quiet"), FakeProc(b"quiet"))
    dead = threading.Event()
    manager = Manager(
        bypass(BOOT_TIMEOUT_SECONDS="0.4"), Store(), Metrics(), spawn=spawn, on_dead=dead.set
    )
    manager.start()
    assert len(spawned) == 2
    assert manager.state is State.DEAD
    assert dead.is_set()
    with pytest.raises(DeadError):
        manager.submit("hello")


def test_a_turn_submitted_during_boot_is_queued_behind_the_dialog():
    proc = FakeProc(b"claude is starting")
    threading.Timer(1.0, lambda: proc.feed(DIALOG)).start()
    manager = Manager(bypass(BOOT_TIMEOUT_SECONDS="20"), Store(), Metrics(), spawn=lambda *_: proc)
    booter = threading.Thread(target=manager.start)
    booter.start()
    time.sleep(0.3)
    assert manager.state is State.BOOTING
    result: list[str] = []
    submitter = threading.Thread(target=lambda: result.append(manager.submit("play a land")))
    submitter.start()
    time.sleep(0.3)
    assert proc.writes == [], "typed into the dialog"
    booter.join(30)
    submitter.join(30)
    try:
        assert result and result[0].startswith("turn-")
        assert proc.writes[:2] == [b"\x1b[B", b"\r"]
        assert proc.writes[2].startswith(b"\x1b[200~play a land")
    finally:
        manager.shutdown()
