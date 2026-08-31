"""Asking a possibly-stuck agent what it is doing, and cancelling it.

`POST /v1/messages` answers 409 for the whole duration of a turn. Without a
probe there is no way to ask a maybe-hung agent anything, only to kill it. A
probe is NOT a turn: it takes no turn slot, resets no deadline, and consumes
no turn budget.

The three states are three different diagnoses:
  pending   - the CLI took the text but never handed it to the model: the
              turn is blocked inside a single long tool call
  delivered - the model has it and is reaching tool-call boundaries
  answered  - a reply starting with the marker came back
"""

from __future__ import annotations

import threading
from enum import StrEnum

ANSWER_MARKER = "MTG-ALIVE"

DEFAULT_PROBE_TEXT = (
    "Do not stop what you are doing. Reply with exactly "
    f"{ANSWER_MARKER} <one sentence about what you are doing right now>."
)


class ProbeState(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    ANSWERED = "answered"


class ProbeUnavailableError(RuntimeError):
    """No live PTY to write to. Maps to 503, never 409."""


class ProbeTracker:
    """The single in-flight probe. Starting a new one supersedes the old,
    which is then reported as unknown rather than as a stale answer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._id: str | None = None
        self._state = ProbeState.PENDING
        self._started = 0.0
        self._answer_latency: float | None = None

    def start(self, probe_id: str, now: float) -> None:
        with self._lock:
            self._id = probe_id
            self._state = ProbeState.PENDING
            self._started = now
            self._answer_latency = None

    def mark_delivered(self, now: float) -> None:
        with self._lock:
            if self._id and self._state is ProbeState.PENDING:
                self._state = ProbeState.DELIVERED

    def mark_answered(self, now: float) -> None:
        with self._lock:
            if self._id and self._state is not ProbeState.ANSWERED:
                self._state = ProbeState.ANSWERED
                self._answer_latency = now - self._started

    def status(self, probe_id: str) -> dict | None:
        with self._lock:
            if probe_id != self._id:
                return None
            return {
                "probeId": self._id,
                "state": self._state,
                "startedAt": self._started,
                "answerLatency": self._answer_latency,
            }
