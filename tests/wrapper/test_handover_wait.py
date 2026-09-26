"""A14: the agent container's preStop, `python -m mtg_agent.wrapper.handover_wait
--wait N`, blocks until the driver's handover marker exists or N seconds pass,
and always exits 0. The wrapper's own stop path holds the claude PTY open the
same way before it closes it."""

from __future__ import annotations

import threading
import time

from mtg_agent.wrapper import handover_wait


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_the_default_marker_is_the_shared_driver_workspace_file():
    assert str(handover_wait.MARKER_PATH) == "/driver-workspace/handover.done"


def test_it_returns_as_soon_as_the_marker_exists(tmp_path):
    marker = tmp_path / "handover.done"
    threading.Timer(0.1, lambda: marker.write_text("{}")).start()
    started = time.monotonic()
    assert handover_wait.wait_for_marker(marker, 30) is True
    assert time.monotonic() - started < 5


def test_the_pty_hold_cap_outlasts_the_driver_prestop():
    """The agent's preStop and its SIGTERM hold keep the PTY for 7370 s
    (review fix round 1, up from 5480): past the driver preStop's 7350 s,
    inside the Job's 7410 s grace."""
    assert handover_wait.DEFAULT_WAIT_SECONDS == 7370.0


def test_it_gives_up_after_the_wait(tmp_path):
    fake = FakeTime()
    found = handover_wait.wait_for_marker(
        tmp_path / "handover.done", 5480, clock=fake.clock, sleep=fake.sleep
    )
    assert found is False
    assert 5480 <= fake.now < 5482


def test_main_exits_zero_when_the_marker_exists(tmp_path):
    marker = tmp_path / "handover.done"
    marker.write_text("{}")
    assert handover_wait.main(["--wait", "5480", "--marker", str(marker)]) == 0


def test_main_exits_zero_after_the_wait_runs_out(tmp_path):
    fake = FakeTime()
    code = handover_wait.main(
        ["--wait", "5", "--marker", str(tmp_path / "handover.done")],
        clock=fake.clock,
        sleep=fake.sleep,
    )
    assert code == 0
    assert fake.now >= 5


def test_main_exits_zero_on_arguments_it_cannot_read():
    assert handover_wait.main(["--wait", "soon"]) == 0


def test_the_stop_path_closes_the_pty_only_after_the_marker(tmp_path):
    marker = tmp_path / "handover.done"
    closed: list[bool] = []
    threading.Timer(0.2, lambda: marker.write_text("{}")).start()
    handover_wait.hold_until_handover(lambda: closed.append(marker.exists()), marker, 30)
    assert closed == [True]


def test_the_stop_path_does_not_wait_outside_a_seat_job(tmp_path):
    marker = tmp_path / "no-shared-workspace" / "handover.done"
    closed: list[bool] = []
    started = time.monotonic()
    handover_wait.hold_until_handover(lambda: closed.append(True), marker, 5480)
    assert closed == [True]
    assert time.monotonic() - started < 1
