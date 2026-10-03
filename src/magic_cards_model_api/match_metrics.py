"""Per-match duration: the first AI-seat dispatch of a game to its ending.

The 15 minute target for an AI match had no measurement behind it. This module
keeps a process registry of when each game first handed a seat to an agent,
and observes `mtg_match_duration_seconds` once, when the game ends.

`kind` is `aivai` when every seat of the game was dispatched, else `pvai`;
`mode` is the dispatch mechanism (`subprocess`, `wrapper`, `kubernetes`). A
game that never dispatched an AI seat records nothing, and neither does a
second ending for a game already observed. Advisor (buddy) dispatches are
filtered out by the caller (`ai_seat.AiSeatRegistry.start`).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, Counter, Histogram

logger = logging.getLogger(__name__)

DEFAULT_TARGET_SECONDS = 900
BUCKETS = (30, 60, 120, 300, 600, 900, 1200, 1800, 2700, 3600, 5400, 7200)

REGISTRY = CollectorRegistry()
DURATION = Histogram(
    "mtg_match_duration_seconds",
    "Seconds from a game's first AI seat dispatch to its ending",
    ["kind", "mode"],
    buckets=BUCKETS,
    registry=REGISTRY,
)
OVER_TARGET = Counter(
    "mtg_match_over_target",
    "Matches that ran longer than MATCH_TARGET_SECONDS",
    ["kind", "mode"],
    registry=REGISTRY,
)


def _clock() -> float:
    return time.monotonic()


@dataclass
class _Game:
    started: float
    mode: str
    uids: set[str] = field(default_factory=set)
    done: bool = False


_lock = threading.Lock()
_games: dict[str, _Game] = {}


def target_seconds() -> float:
    try:
        return float(os.environ.get("MATCH_TARGET_SECONDS", DEFAULT_TARGET_SECONDS))
    except ValueError:
        return float(DEFAULT_TARGET_SECONDS)


def reset() -> None:
    """Forget every game (tests)."""
    with _lock:
        _games.clear()


def dispatched(game_id: str, uid: str, mode: str) -> None:
    """Record a seat dispatch; only the game's first moves the start time."""
    with _lock:
        game = _games.get(game_id)
        if game is None:
            game = _games[game_id] = _Game(started=_clock(), mode=mode)
        game.uids.add(uid)


def game_over(game_id: str, seats: int = 2) -> float | None:
    """Observe the finished game's duration; returns seconds, or None when
    nothing was recorded (no AI dispatch, or already observed)."""
    with _lock:
        game = _games.get(game_id)
        if game is None or game.done:
            return None
        game.done = True
        seconds = _clock() - game.started
        kind = "aivai" if len(game.uids) >= seats else "pvai"
        mode = game.mode
    target = target_seconds()
    over = seconds > target
    DURATION.labels(kind=kind, mode=mode).observe(seconds)
    if over:
        OVER_TARGET.labels(kind=kind, mode=mode).inc()
    logger.log(
        logging.WARNING if over else logging.INFO,
        "match duration game=%s kind=%s mode=%s seconds=%g target_seconds=%g over_target=%s",
        game_id,
        kind,
        mode,
        seconds,
        target,
        over,
    )
    return seconds
