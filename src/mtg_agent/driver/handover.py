"""The handover's fixed parts, and the driver container's preStop.

A seat hands over once: at a natural end (after the reflection turn), on
`/release`, or when Kubernetes deletes the Job. Each of those reaches the one
idempotent `POST /handover` in `app.py`; this module holds what that route
shares with the preStop: the marker both containers wait on, the time budget,
and the compact conflict-warning block the handover prompt carries.

`python -m mtg_agent.driver.handover --wait N` is the driver container's
preStop (`deploy/seat-job.yaml`). It POSTs the local driver's `/handover`
with no outcome, so the driver names `killed` unless it knows a winner or a
draw, then blocks until the marker exists or N seconds pass. It always exits
0: a preStop that fails only makes the kubelet log noise, never a better
handover.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .config import DriverSettings

if TYPE_CHECKING:
    from magic_cards_model_api.playscripts.lint import PlaysWarning

    from .guides import ScriptsRegistration

log = logging.getLogger("driver.handover")

#: The file both containers wait on, under the driver's workspace
#: (`/driver-workspace/handover.done` in the seat Job). It holds the
#: handover's status JSON whether it succeeded, timed out or failed.
MARKER_NAME = "handover.done"

#: When the one "push now" nudge goes out, in seconds since the handover began:
#: 23 minutes, five before the budget runs out.
NUDGE_AFTER_SECONDS = 1380.0

#: When a handover still waiting on its turn gives up and writes the marker:
#: 28 minutes, sized for a handover that admits, then develops scripts and
#: guides through subagents (game 16a9cf8f ran out 510 s doing that). Inside
#: the driver preStop's `PRESTOP_WAIT_SECONDS`, itself inside the Job's 1800 s
#: grace, so the retire after the marker still fits before the kubelet's
#: SIGKILL.
HANDOVER_BUDGET_SECONDS = 1680.0

#: The driver container's preStop wait (`deploy/seat-job.yaml`), the default
#: for `--wait`.
PRESTOP_WAIT_SECONDS = 1740.0

#: How long the handover turn's push is retried while the agent answers 409
#: (its interrupted turn not ended yet) before the handover fails and writes
#: the marker. The wrapper ends an interrupted turn `INTERRUPT_SETTLE_SECONDS`
#: (20 s) after the interrupt, so this is several settles, and far inside 1740 s.
HANDOVER_PUSH_BUDGET_SECONDS = 120.0

#: The first wait after a 409, doubled after each further one up to the max.
#: Live, a flat 50 ms retry sent 5523 pushes in one 510 s budget.
HANDOVER_BACKOFF_START_SECONDS = 0.5
HANDOVER_BACKOFF_MAX_SECONDS = 5.0

#: How often the waiting handover looks at its clock.
POLL_SECONDS = 0.05

#: How long the preStop gives the local driver to accept the POST.
POST_TIMEOUT_SECONDS = 10.0

Clock = Callable[[], float]
Sleep = Callable[[float], None]


def wait_for_marker(
    path: Path,
    wait: float,
    *,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
    poll: float = 0.5,
) -> bool:
    """True as soon as `path` exists, False once `wait` seconds pass first."""
    deadline = clock() + wait
    while not path.exists():
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(poll, remaining))
    return True


def write_marker(path: Path, status: Mapping[str, Any]) -> None:
    """Write the status whole or not at all, so a waiter never reads half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(json.dumps(dict(status), indent=2, sort_keys=True))
    partial.replace(path)


def clear_marker(path: Path) -> None:
    """Drop a marker left by an earlier match (a long-lived compose driver)."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not clear the old handover marker %s: %s", path, exc)


def format_conflicts(warnings: Sequence[PlaysWarning]) -> str:
    """One line per warning: its code, the script ids, the overlapping
    dimensions. The message is left out; the prompt already says what to do."""
    if not warnings:
        return "none: no two enabled scripts in the registered corpus can apply at one moment."
    return "\n".join(
        f"{warning.code}: {', '.join(warning.plays)} ({', '.join(warning.dimensions)})"
        for warning in warnings
    )


def conflict_warnings(registration: ScriptsRegistration | None, deck_slug: str) -> str:
    """`plays check`'s conflict warnings over the corpus this seat registered
    at dispatch, formatted by `format_conflicts`. Errors are not reported: the
    registration route refuses a corpus with any, so a registered one has
    none."""
    if registration is None or registration.status != "registered":
        status = registration.status if registration is not None else "not attempted"
        return f"none: no corpus is registered for this seat ({status})."
    # Imported here: the preStop imports this module and needs none of it.
    from magic_cards_model_api.playscripts.script_tools import static_conflicts  # noqa: PLC0415

    corpus = registration.corpus
    return format_conflicts(static_conflicts(list(corpus.scripts), list(corpus.inputs), deck_slug))


def local_url(http_addr: str) -> str:
    _, _, port = http_addr.rpartition(":")
    return f"http://127.0.0.1:{port or '8090'}/handover"


def _post(url: str, body: dict[str, Any]) -> None:
    httpx.post(url, json=body, timeout=POST_TIMEOUT_SECONDS)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    post: Callable[[str, dict[str, Any]], None] = _post,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> int:
    started = clock()
    try:
        parser = argparse.ArgumentParser(prog="python -m mtg_agent.driver.handover")
        parser.add_argument("--wait", type=float, default=PRESTOP_WAIT_SECONDS)
        args = parser.parse_args(argv)
        settings = DriverSettings.from_env(os.environ if env is None else env)
        try:
            post(local_url(settings.http_addr), {})
        except Exception as exc:  # noqa: BLE001 - a driver that does not answer is still waited on
            log.warning("could not start the handover: %s", exc)
        remaining = max(0.0, args.wait - (clock() - started))
        wait_for_marker(settings.workspace / MARKER_NAME, remaining, clock=clock, sleep=sleep)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - a preStop always exits 0
        log.warning("handover preStop: %s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
