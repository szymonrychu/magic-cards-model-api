"""Fixture ledger builders shared by the Claude-match game log tests (store row shape)."""

from __future__ import annotations

from typing import Any


def snap(
    life: tuple[int, int] = (20, 20),
    hand: tuple[int, int] = (5, 5),
    board: tuple[int, int] = (3, 3),
    stack: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """A snapshot in the shape `audit.record` stores."""
    return {
        "life": {"a": life[0], "b": life[1]},
        "hands": {"a": [f"h{i}" for i in range(hand[0])], "b": [f"h{i}" for i in range(hand[1])]},
        "battlefield": [f"a: Land {i}" for i in range(board[0])]
        + [f"b: Land {i}" for i in range(board[1])],
        "stack": list(stack),
    }


def row(
    seq: int,
    action: str,
    *,
    turn: int | None = 3,
    step: str | None = "precombat_main",
    player: str | None = "a",
    reasoning: str | None = None,
    allowed: bool = True,
    snapshot: dict[str, Any] | None = None,
    active_player: str | None = "a",
    phase: str | None = "main",
    **detail: Any,
) -> dict[str, Any]:
    """One `records` row as `store.records` returns it."""
    body: dict[str, Any] = dict(detail)
    if player is not None:
        body["player"] = player
    if not allowed:
        body["allowed"] = False
    return {
        "seq": seq,
        "created_at": "2026-11-16T00:00:00+00:00",
        "payload": {
            "ts": "2026-11-16T00:00:00+00:00",
            "action": action,
            "detail": body,
            "turn": turn,
            "step": step,
            "phase": phase,
            "snapshot": snapshot if snapshot is not None else snap(),
            "active_player": active_player,
            "priority_holder": player,
            "reasoning": reasoning,
        },
    }


def ledger(turns: int, per_turn: int, *, start_seq: int = 1) -> list[dict[str, Any]]:
    """`turns` turns of `per_turn` distinct non-pass rows each, alternating seats."""
    rows: list[dict[str, Any]] = []
    seq = start_seq
    for turn in range(1, turns + 1):
        for i in range(per_turn):
            seat = "a" if i % 2 == 0 else "b"
            rows.append(
                row(
                    seq,
                    "cast",
                    turn=turn,
                    player=seat,
                    card=f"Spell {turn}-{i}",
                    reasoning=f"reason {turn}-{i}",
                )
            )
            seq += 1
    return rows
