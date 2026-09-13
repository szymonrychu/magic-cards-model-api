"""The agent container's side of the handover.

`python -m mtg_agent.wrapper.handover_wait --wait N` is the agent container's
preStop (`deploy/seat-job.yaml`): it blocks until the driver writes its
handover marker, or N seconds pass, so the kubelet does not stop the wrapper -
and the claude PTY the handover turn runs in - while the handover is still
going. It always exits 0.

`hold_until_handover` is the same wait on the wrapper's own stop path: a
SIGTERM closes the PTY only after the marker, when the shared workspace is
mounted at all.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from mtg_agent.driver.handover import MARKER_NAME, Clock, Sleep, wait_for_marker

log = logging.getLogger("wrapper.handover")

#: The driver's marker, on the emptyDir both seat Job containers mount.
MARKER_PATH = Path("/driver-workspace") / MARKER_NAME

#: The agent preStop's wait in the seat Job, inside its 600 s grace.
DEFAULT_WAIT_SECONDS = 560.0


def hold_until_handover(
    then: Callable[[], None],
    marker: Path = MARKER_PATH,
    wait: float = DEFAULT_WAIT_SECONDS,
    *,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> None:
    """Run `then` once the marker exists or `wait` passes. Without the shared
    workspace (compose, a local run) there is no handover to wait for."""
    try:
        if marker.parent.is_dir():
            wait_for_marker(marker, wait, clock=clock, sleep=sleep)
    finally:
        then()


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> int:
    try:
        parser = argparse.ArgumentParser(prog="python -m mtg_agent.wrapper.handover_wait")
        parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS)
        parser.add_argument("--marker", type=Path, default=MARKER_PATH)
        args = parser.parse_args(argv)
        wait_for_marker(args.marker, args.wait, clock=clock, sleep=sleep)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - a preStop always exits 0
        log.warning("handover wait: %s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
