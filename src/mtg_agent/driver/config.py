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
    agent_retry_interval: float = 3.0
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
            agent_retry_interval=float(value("AGENT_RETRY_INTERVAL_SECONDS", "3")),
            assignment_json=value("DRIVER_ASSIGNMENT_JSON", ""),
            exit_after_match=value("DRIVER_EXIT_AFTER_MATCH", "").lower() in ("1", "true", "yes"),
        )
