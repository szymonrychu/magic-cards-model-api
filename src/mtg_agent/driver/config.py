"""The driver's container wiring."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DriverSettings:
    game_api: str = "http://game:8000"
    wrapper_api: str = "http://agent:8080"
    wrapper_secret: str = ""
    guides_root: Path = Path("/guides")
    memory_root: Path = Path("/workspace/memory")
    workspace: Path = Path("/workspace")
    self_url: str = "http://driver:8090"
    http_addr: str = ":8090"
    #: How long a dispatch may keep trying to reach an agent that is booting.
    #: A fresh container needs a pty spawn, the bypass dialog and output
    #: quiescence. Matches the agent container's own `startupProbe` budget in
    #: `k8s_seats.py` (36 x 10s = 360s) - a shorter wait here would give up on
    #: an agent Kubernetes itself is still willing to wait for.
    agent_boot_wait: float = 360.0
    #: Speed pass 2026-09-28: was 3.0. Prod measured pod-up +20-52s and
    #: dispatched +35-64s after `create_game`; `push_turn_when_reachable`
    #: (and the pregame `_dispatch_and_retry` path) retry a booting agent on
    #: this cadence, so it bounds how long the driver can sit on an agent
    #: that became reachable moments ago, up to `agent_boot_wait` worst case.
    #: A plain HTTP GET against a not-yet-listening socket is cheap, so
    #: polling 6x more often than before costs nothing but shaves up to
    #: ~2.5s off the worst case.
    agent_retry_interval: float = 0.5
    #: A dispatch body (the same shape `POST /dispatch` takes), set when the
    #: container is a per-game Kubernetes Job rather than a long-lived
    #: compose service: the driver dispatches itself at startup instead of
    #: waiting for a caller to POST it.
    assignment_json: str = ""
    #: Whether `retire` should stop the driver's own process once the match
    #: it was dispatched for is over - the per-game Job case, where the
    #: container is meant to exit 0 rather than sit idle waiting for the next
    #: match.
    exit_after_match: bool = False
    #: FD6 (4): the per-try timeout on the review-record GET
    #: (`app.post_ai_review`) - "well above 30s", the old, unconfigurable
    #: bound `make_client`'s own 30s default gave it (fb3-parity.md item 6:
    #: a live prod review timed out there, against an api pod capped at 1
    #: CPU / 1Gi). Configurable so a slower api deployment can raise it
    #: without a code change.
    review_record_timeout: float = 120.0
    #: How long, in total, the review-record GET keeps retrying a timeout or
    #: a 409 (the game not yet released or ended, fb3-parity.md item 1)
    #: before giving up and filing `ai_review_failed` with the final
    #: reason - never partway through, and never unboundedly.
    review_record_retry_budget: float = 300.0
    #: The first retry's delay; each retry after that doubles it, capped at
    #: `review_record_retry_max_delay` - a bounded exponential backoff, not
    #: a tight retry loop hammering a game server that just said "not yet".
    review_record_retry_start: float = 2.0
    review_record_retry_max_delay: float = 30.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DriverSettings:
        def value(key: str, default: str) -> str:
            return env.get(key, "").strip() or default

        return cls(
            game_api=value("GAME_API", "http://game:8000"),
            wrapper_api=value("WRAPPER_API", "http://agent:8080"),
            wrapper_secret=value("WRAPPER_SHARED_SECRET", ""),
            guides_root=Path(value("GUIDES_ROOT", "/guides")),
            memory_root=Path(value("MEMORY_ROOT", "/workspace/memory")),
            workspace=Path(value("DRIVER_WORKSPACE", "/workspace")),
            self_url=value("DRIVER_SELF_URL", "http://driver:8090"),
            http_addr=value("HTTP_ADDR", ":8090"),
            agent_boot_wait=float(value("AGENT_BOOT_WAIT_SECONDS", "360")),
            agent_retry_interval=float(value("AGENT_RETRY_INTERVAL_SECONDS", "0.5")),
            assignment_json=value("DRIVER_ASSIGNMENT_JSON", ""),
            exit_after_match=value("DRIVER_EXIT_AFTER_MATCH", "").lower() in ("1", "true", "yes"),
            review_record_timeout=float(value("REVIEW_RECORD_TIMEOUT_SECONDS", "120")),
            review_record_retry_budget=float(value("REVIEW_RECORD_RETRY_BUDGET_SECONDS", "300")),
            review_record_retry_start=float(value("REVIEW_RECORD_RETRY_START_SECONDS", "2")),
            review_record_retry_max_delay=float(
                value("REVIEW_RECORD_RETRY_MAX_DELAY_SECONDS", "30")
            ),
        )
