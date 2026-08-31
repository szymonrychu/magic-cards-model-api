"""One turn's record, and the in-memory store of every turn this process ran.

Process-lifetime state: a restart forgets it, exactly like the game server's
own session store. The transcript on disk is what survives.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TurnState(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class Record:
    id: str
    state: TurnState
    text: str
    callback_url: str
    started_at: float
    last_activity_at: float = 0.0
    completed_at: float | None = None
    final_text: str = ""
    result_json: Any = None
    usage: dict | None = None
    turn_tokens: list[dict] = field(default_factory=list)
    stop_reason: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if not self.last_activity_at:
            self.last_activity_at = self.started_at

    def summary(self) -> dict:
        return {
            "turnId": self.id,
            "state": str(self.state),
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "lastActivityAt": self.last_activity_at,
        }

    def full(self) -> dict:
        return self.summary() | {
            "text": self.text,
            "finalText": self.final_text,
            "resultJson": self.result_json,
            "usage": self.usage,
            "turnTokens": self.turn_tokens,
            "stopReason": self.stop_reason,
            "error": self.error,
            "callbackUrl": self.callback_url,
        }


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[str, Record] = {}
        self._order: list[str] = []

    def create(self, id: str, text: str, callback_url: str, now: float) -> Record:
        record = Record(
            id=id,
            state=TurnState.RUNNING,
            text=text,
            callback_url=callback_url,
            started_at=now,
        )
        with self._lock:
            self._by_id[id] = record
            self._order.append(id)
        return record

    def get(self, id: str) -> Record | None:
        with self._lock:
            return self._by_id.get(id)

    def list(self) -> list[Record]:
        with self._lock:
            return [self._by_id[i] for i in self._order]

    def _require(self, id: str) -> Record:
        record = self._by_id.get(id)
        if record is None:
            raise KeyError(f"turn: not found: {id}")
        return record

    def complete(
        self,
        id: str,
        *,
        final_text: str,
        result_json: Any,
        usage: dict | None,
        turn_tokens: list[dict],
        stop_reason: str,
        now: float,
    ) -> Record:
        with self._lock:
            record = self._require(id)
            record.state = TurnState.COMPLETE
            record.final_text = final_text
            record.result_json = result_json
            record.usage = usage
            record.turn_tokens = turn_tokens
            record.stop_reason = stop_reason
            record.completed_at = now
            return record

    def fail(self, id: str, reason: str, now: float) -> Record:
        with self._lock:
            record = self._require(id)
            record.state = TurnState.FAILED
            record.error = reason
            record.completed_at = now
            return record

    def touch(self, id: str, now: float) -> bool:
        with self._lock:
            record = self._by_id.get(id)
            if record is None or record.state is not TurnState.RUNNING:
                return False
            record.last_activity_at = now
            return True

    def counts(self) -> tuple[int, int]:
        """`(completed, finished)` - successful turns, and all terminal ones."""
        with self._lock:
            records = list(self._by_id.values())
        completed = sum(1 for r in records if r.state is TurnState.COMPLETE)
        finished = sum(1 for r in records if r.state is not TurnState.RUNNING)
        return completed, finished
