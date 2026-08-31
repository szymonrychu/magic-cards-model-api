"""The game-action audit log: one append-only list per game, plus a console line.

Every mutating endpoint records what was attempted and what came of it - allowed
or refused, with the engine's own reason verbatim - so two agents playing through
the API leave a complete, replayable record behind.

This module must stay importable without the FastAPI wiring (tests read the log
directly), so the session store is imported lazily inside `record`.
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import UTC, datetime
from typing import Any

from magic_cards_model.machines import PHASE_OF

_logs: dict[str, list[dict[str, Any]]] = {}
_lock = threading.Lock()

#: The audited action a seat's `report_problem` MCP call turns into. Named
#: here because this module treats it specially - see `record`.
PROBLEM_ACTION = "report_problem"

logger = logging.getLogger("mcm.audit")
if not logger.handlers:  # import-guarded: no duplicate handlers on re-import
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def record(game_id: str, action: str, **detail: Any) -> int:
    """Append one entry for `action` on `game_id`, then print one console line.

    The entry carries the game's turn number, step, phase, and stack size at the
    moment of recording, plus a full-information snapshot: board, both hands,
    stack with targets, graveyards, exile, life, pools. A game that is no longer
    in the store (or was never in it) still gets its entry, with those fields
    None.

    Returns the entry's own `seq` (this game's log length after appending it) -
    a free, already-monotonic idempotency marker: a caller who is unsure
    whether a call that timed out actually landed can compare the `log_seq`
    an action response carries (`app.py`'s `audit_outcome` and the
    `resolve-top`/`advance`/`pass-priority` endpoints) against a later read,
    rather than guessing from a blind retry.
    """
    from .serialize import snapshot  # lazy: keep module import light
    from .store import store  # lazy: audit imports nothing of the app at load

    game = store.get(game_id)
    turn = game.turn if game is not None else None
    step = turn.step if turn is not None else None
    entry = {
        "seq": 0,  # filled in under the lock, from the log's own length
        "ts": datetime.now(UTC).isoformat(),
        "action": action,
        "detail": dict(detail),
        "turn": turn.turn_number if turn is not None else None,
        "step": step.value if step is not None else None,
        "phase": PHASE_OF[step].value if step is not None else None,
        "stack_size": len(game.stack) if game is not None else None,
        "snapshot": snapshot(game) if game is not None else None,
    }
    with _lock:
        log = _logs.setdefault(game_id, [])
        entry["seq"] = len(log) + 1
        log.append(entry)
    if action == PROBLEM_ACTION:
        # A seat filing an engine bug is the single most valuable thing in this
        # log, and it used to be indistinguishable from the hundred routine
        # lines around it: the compact line below prints only card/target, so
        # the report's *text* - the whole point of the call - never reached the
        # console at all. It gets its own banner and WARNING level so it stands
        # out in `docker compose logs`.
        logger.warning(_problem_block(entry))
    else:
        logger.info(_console_line(entry))
    return entry["seq"]


def entries(game_id: str) -> list[dict[str, Any]]:
    """Every entry recorded for this game, oldest first."""
    with _lock:
        return [dict(entry, detail=dict(entry["detail"])) for entry in _logs.get(game_id, [])]


def _problem_block(entry: dict[str, Any]) -> str:
    """The console form of `report_problem`: a banner, the seat and position,
    and the report text itself, one indented line per line of it.

    Deliberately louder and taller than every other entry. It is the agent
    telling us the engine is wrong, live, and a person watching the container
    log should not have to be looking for it to notice."""
    detail = entry["detail"]
    turn = entry["turn"] if entry["turn"] is not None else "?"
    step = entry["step"] or "?"
    who = detail.get("player") or "?"
    text = str(detail.get("text") or "").strip() or "(no text)"
    lines = [
        "=" * 72,
        f"PROBLEM REPORTED by {who} - #{entry['seq']} [turn {turn} / {step}]",
    ]
    lines += [f"  {line}" for line in text.splitlines()]
    lines.append("=" * 72)
    return "\n".join(lines)


def _console_line(entry: dict[str, Any]) -> str:
    """One compact line, e.g. `#12 [turn 2 / precombat_main] alice cast Bears
    -> allowed | board 3 hands A7/B6 stack 1`."""
    detail = entry["detail"]
    turn = entry["turn"] if entry["turn"] is not None else "?"
    step = entry["step"] or "?"
    head = f"#{entry['seq']} [turn {turn} / {step}]"

    subject = detail.get("card") or detail.get("target") or ""
    if "from" in detail and "to" in detail:
        subject = f"{detail['from']} -> {detail['to']}"
    middle = f"{entry['action']} {subject}".strip()

    who = detail.get("player")
    line = f"{head} {who} {middle}" if who else f"{head} {middle}"
    if "resolved" in detail:
        # "changed nothing" is the signature of an effect the engine has no
        # implementation for, so it is called out rather than left to be
        # inferred from two consecutive snapshots.
        did = "changed the game" if detail.get("changed") else "CHANGED NOTHING"
        line += f" | resolved {detail['resolved']}: {did}"
    if "allowed" in detail:
        if detail["allowed"]:
            line += " -> allowed"
        else:
            line += f" -> refused: {detail.get('reason')}"

    if detail.get("note"):
        # A free-form note for events that are not card actions - a seat being
        # handed to the driver, say - so the console line says something more
        # than the action's bare name.
        line += f" | {detail['note']}"

    snap = entry.get("snapshot")
    if snap:
        hands = snap.get("hands", {})
        hand_counts = "/".join(f"{name[:1].upper()}{len(cards)}" for name, cards in hands.items())
        line += (
            f" | board {len(snap.get('battlefield', []))}"
            f" hands {hand_counts}"
            f" stack {len(snap.get('stack', []))}"
        )
    return line
