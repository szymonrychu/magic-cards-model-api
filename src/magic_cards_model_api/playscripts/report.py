"""What a seat's scripts have done so far in one game, compact enough to hand
over.

`script_run_table` reads the `script_run` rows `POST .../script-runs` writes
(`records.record_script_run_finish`) and prints one line per script: how
often it ran, how each run ended, and how many runs ended with the script
still applying. A handover prompt carries it whole, so it is bounded at
`TABLE_LIMIT_BYTES`; past that the lowest-priority rows are cut and a
`TRUNCATION_MARKER` line says how many.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..audit import SCRIPT_RUN_ACTION

#: The most a table may take, in UTF-8 bytes.
TABLE_LIMIT_BYTES = 4096

#: The start of the last line of a table that was cut.
TRUNCATION_MARKER = "... table cut at 4 KB:"

#: What the runner posts as `reapplicable_after` when the re-check still applies.
REAPPLICABLE = "true"

HEADER = "script | priority | runs | exits | reapplicable_after"


@dataclass
class _ScriptRow:
    label: str
    priority: int | None = None
    runs: int = 0
    exits: dict[str, int] = field(default_factory=dict)
    reapplicable: int = 0

    def line(self) -> str:
        exits = " ".join(f"{name}={count}" for name, count in sorted(self.exits.items()))
        priority = "-" if self.priority is None else str(self.priority)
        return f"{self.label} | {priority} | {self.runs} | {exits} | {self.reapplicable}"


def _payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """A row as `store.records` returns it wraps the entry in `payload`; one
    from `audit.entries` is the entry itself."""
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else record


def script_run_table(records: Iterable[Mapping[str, Any]], game_id: str, seat: str) -> str:
    """One line per script `seat` ran in `game_id`, highest priority first.

    Advise runs - moves proposed to a human rather than taken - get a row of
    their own, labelled `(advise)`, so a proposal nobody accepted never reads
    as the seat's own play. `priority` is the latest one a run reported.

    `reapplicable_after` is the runner's one condition re-check at the end of
    a run that resolved a play: `"true"` when the script would apply again,
    `"false"` when not, absent when no play was found. The column counts the
    `"true"` runs, the loss data a future loop decision reads.
    """
    rows: dict[tuple[str, bool], _ScriptRow] = {}
    for record in records:
        payload = _payload(record)
        if payload.get("action") != SCRIPT_RUN_ACTION:
            continue
        detail = payload.get("detail") or {}
        if detail.get("seat") != seat or detail.get("game_id") != game_id:
            continue
        script_id = str(detail.get("script_id"))
        advise = bool(detail.get("advise"))
        label = f"{script_id} (advise)" if advise else script_id
        row = rows.setdefault((script_id, advise), _ScriptRow(label))
        row.runs += 1
        exit_name = str(detail.get("exit") or "unknown")
        row.exits[exit_name] = row.exits.get(exit_name, 0) + 1
        if detail.get("priority") is not None:
            row.priority = int(detail["priority"])
        if detail.get("reapplicable_after") == REAPPLICABLE:
            row.reapplicable += 1

    if not rows:
        return f"no script runs recorded for {seat} in {game_id}"

    ordered = [
        row.line()
        for _, row in sorted(
            rows.items(),
            key=lambda item: (item[1].priority is None, -(item[1].priority or 0), item[0]),
        )
    ]
    table = "\n".join([HEADER, *ordered])
    if _size(table) <= TABLE_LIMIT_BYTES:
        return table

    kept = [HEADER]
    for shown, line in enumerate(ordered):
        marker = _marker(len(ordered) - shown - 1)
        if _size("\n".join([*kept, line, marker])) > TABLE_LIMIT_BYTES:
            return "\n".join([*kept, _marker(len(ordered) - shown)])
        kept.append(line)
    raise AssertionError("a table over the limit always cuts at least one row")


def _marker(omitted: int) -> str:
    return f"{TRUNCATION_MARKER} {omitted} more script row(s) not shown"


def _size(text: str) -> int:
    return len(text.encode("utf-8"))
