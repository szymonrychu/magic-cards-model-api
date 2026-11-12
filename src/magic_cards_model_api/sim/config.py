"""Resource limits of the chat simulation, read from MTG_SIM_* env vars."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


def _number(env: Mapping[str, str], name: str, default: float, cast: Callable[[str], Any]) -> Any:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ValueError(f"{name}: not a valid number: {raw!r}") from exc


@dataclass(frozen=True)
class SimLimits:
    token: str | None
    max_sandboxes: int
    sandbox_idle_ttl_s: float
    call_timeout_s: float
    max_match_games: int
    match_wall_s: float
    max_batches: int
    max_script_steps: int
    worker_max_rss_mb: int
    max_claude_matches: int
    claude_deadline_s: float
    result_ttl_s: float

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SimLimits:
        env = os.environ if env is None else env
        return cls(
            token=env.get("MTG_SIM_TOKEN") or None,
            max_sandboxes=_number(env, "MTG_SIM_MAX_SANDBOXES", 1, int),
            sandbox_idle_ttl_s=_number(env, "MTG_SIM_SANDBOX_IDLE_TTL_S", 1800, float),
            call_timeout_s=_number(env, "MTG_SIM_CALL_TIMEOUT_S", 25, float),
            max_match_games=_number(env, "MTG_SIM_MAX_MATCH_GAMES", 10, int),
            match_wall_s=_number(env, "MTG_SIM_MATCH_WALL_S", 1500, float),
            max_batches=_number(env, "MTG_SIM_MAX_BATCHES", 1, int),
            max_script_steps=_number(env, "MTG_SIM_MAX_SCRIPT_STEPS", 200, int),
            worker_max_rss_mb=_number(env, "MTG_SIM_WORKER_MAX_RSS_MB", 1024, int),
            max_claude_matches=_number(env, "MTG_SIM_MAX_CLAUDE_MATCHES", 1, int),
            claude_deadline_s=_number(env, "MTG_SIM_CLAUDE_DEADLINE_S", 14400, float),
            result_ttl_s=_number(env, "MTG_SIM_RESULT_TTL_S", 86400, float),
        )
