"""The `compact-v1` game log for Claude-seat sim matches: pure projection of `records` rows.

No store access and no I/O: callers pass the rows (as `store.records` returns them) and a card
name resolver. The constants below are the knobs to re-measure on a real ledger: they bound one
answer so it fits an MCP tool result, and the wait call's head-and-tail summary.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

FORMAT = "compact-v1"
REASON_MAX = 300
LOG_ANSWER_MAX_CHARS = 40_000
WAIT_LOG_MAX_CHARS = 45_000
WAIT_HEAD_TURNS = 2
TAIL_LINES = 20
FULL_BOARD_MAX_LIMIT = 10
PASS_ACTIONS = frozenset({"pass_priority"})

REFUSED_REASON_MAX = 120
CUT_MARK = " [cut]"

STEP_SHORT = {
    "mulligan": "mull",
    "pregame_actions": "pregame",
    "untap": "untap",
    "upkeep": "upkeep",
    "draw": "draw",
    "precombat_main": "main1",
    "beginning_of_combat": "combat",
    "declare_attackers": "attack",
    "declare_blockers": "block",
    "first_strike_damage": "fsdamage",
    "combat_damage": "damage",
    "end_of_combat": "endcombat",
    "postcombat_main": "main2",
    "end": "end",
    "cleanup": "cleanup",
}

#: ledger action name to the verb printed (any other action prints its own name).
VERBS = {
    "cast": "cast",
    "activate": "activate",
    "play_land": "land",
    "attack": "attack",
    "block": "block",
    "declare_blockers": "block",
    "pass_priority": "pass",
}

Names = Callable[[str], str | None]

_SEATS = ("a", "b")
_KINDS = ("plays", "all")
_BOARDS = ("none", "changes", "full")


@dataclass(frozen=True)
class LogFilter:
    turn: tuple[int, int] | None = None
    seat: str | None = None
    kinds: str = "plays"
    reasoning: bool = True
    board: str = "changes"


@dataclass(frozen=True)
class LogLine:
    """One rendered line; `text` may hold a second `"\\n   why: ..."` line.

    `end_seq` is the last ledger seq the line covers (a collapsed pass run), so a pager resumes
    after `end_seq or seq` without overlapping the next page.
    """

    seq: int
    turn: int | None
    text: str
    header: bool = False
    end_seq: int | None = None


def parse_filter(
    turn: int | str | None,
    seat: str | None,
    kinds: str,
    reasoning: bool,
    board: str,
    limit: int,
) -> LogFilter:
    """Validate the tool arguments; `ValueError` messages name the allowed values."""
    turn_range: tuple[int, int] | None = None
    if turn is not None and turn != "":
        bad = ValueError('turn must be an integer or a range "lo-hi" such as "5-8" with lo <= hi')
        if isinstance(turn, bool):
            raise bad
        if isinstance(turn, int):
            turn_range = (turn, turn)
        else:
            match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", str(turn))
            if match is None:
                raise bad
            lo = int(match.group(1))
            hi = int(match.group(2)) if match.group(2) is not None else lo
            if lo > hi:
                raise bad
            turn_range = (lo, hi)
    if seat is not None and seat not in _SEATS:
        raise ValueError('seat must be "a" or "b"')
    if kinds not in _KINDS:
        raise ValueError('kinds must be "plays" or "all"')
    if board not in _BOARDS:
        raise ValueError('board must be "none", "changes" or "full"')
    if board == "full" and limit > FULL_BOARD_MAX_LIMIT:
        raise ValueError(f'board "full" needs limit <= {FULL_BOARD_MAX_LIMIT}')
    return LogFilter(turn=turn_range, seat=seat, kinds=kinds, reasoning=reasoning, board=board)


def ascii_text(text: str) -> str:
    """NFKD, drop combining marks, any other non-ASCII character becomes `?`."""
    out: list[str] = []
    for char in unicodedata.normalize("NFKD", text):
        if char.isascii():
            out.append(char)
        elif not unicodedata.combining(char):
            out.append("?")
    return "".join(out)


def render(lines: list[LogLine]) -> str:
    return "\n".join(line.text for line in lines)


# -- projection ---------------------------------------------------------------------------


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _card(value: Any, names: Names) -> str:
    text = str(value)
    if _is_uuid(text):
        return names(text) or "card"
    return text


def _ref(value: Any, names: Names) -> str:
    """A player name, a resolved card id or a target dict, as plain text."""
    if isinstance(value, dict):
        for key in ("player", "card_id", "id"):
            if value.get(key) is not None:
                return _ref(value[key], names)
        return "?"
    text = str(value)
    if _is_uuid(text):
        return names(text) or text
    return text


def _subject(action: str, detail: dict[str, Any], names: Names) -> str:
    if action == "attack":
        pairs = detail.get("attackers") or []
        return ", ".join(
            f"{_card(p.get('card'), names)}->{_ref(p.get('defender'), names)}" for p in pairs
        )
    if action in ("block", "declare_blockers"):
        pairs = detail.get("blocks") or detail.get("block") or []
        return ", ".join(
            f"{_card(p.get('blocker'), names)}->{_card(p.get('blocked'), names)}" for p in pairs
        )
    card = detail.get("card")
    return f'"{_card(card, names)}"' if card is not None else ""


def _targets(detail: dict[str, Any], names: Names) -> str:
    targets = detail.get("targets")
    if targets is None or targets == [] or targets == "":
        return ""
    items = targets if isinstance(targets, list) else [targets]
    return ", ".join(_ref(item, names) for item in items)


def _counts(snapshot: dict[str, Any]) -> dict[str, dict[str, int]]:
    life = {str(p): v for p, v in (snapshot.get("life") or {}).items()}
    hand = {str(p): len(cards) for p, cards in (snapshot.get("hands") or {}).items()}
    board: dict[str, int] = {}
    for line in snapshot.get("battlefield") or []:
        owner, sep, _ = str(line).partition(": ")
        if sep:
            board[owner] = board.get(owner, 0) + 1
    for player in set(life) | set(hand):
        board.setdefault(player, 0)
    return {"life": life, "hand": hand, "board": board}


def _pairs(values: dict[str, int]) -> str:
    return " ".join(f"{p}{v}" for p, v in sorted(values.items()))


def _delta(prev: dict[str, Any] | None, snap: dict[str, Any] | None, board: str) -> str:
    if board == "none" or prev is None or snap is None:
        return ""
    before, after = _counts(prev), _counts(snap)
    return "".join(
        f" | {name} {_pairs(after[name])}"
        for name in ("life", "hand", "board")
        if before[name] != after[name]
    )


def _why(reasoning: Any, reason_max: int | None) -> str:
    if not isinstance(reasoning, str):
        return ""
    text = " ".join(reasoning.split())
    if not text:
        return ""
    if reason_max is not None and len(text) > reason_max:
        text = text[:reason_max] + "..."
    return "\n   why: " + text


def _header_text(turn: int, payload: dict[str, Any]) -> str:
    active = payload.get("active_player") or "-"
    snap = payload.get("snapshot")
    if snap is None:
        return f"== T{turn} {active} =="
    counts = _counts(snap)
    return (
        f"== T{turn} {active} (life {_pairs(counts['life'])}, board {_pairs(counts['board'])}) =="
    )


def _head(row: dict[str, Any], names: Names) -> str:
    payload = row["payload"]
    detail = payload.get("detail") or {}
    turn = payload.get("turn")
    step = payload.get("step")
    step_text = "-" if step is None else STEP_SHORT.get(step, step)
    action = payload.get("action") or "?"
    parts = [f"#{row['seq']}", "T?" if turn is None else f"T{turn}", step_text]
    if detail.get("player") is not None:
        parts.append(str(detail["player"]))
    parts.append(VERBS.get(action, action))
    subject = _subject(action, detail, names)
    if subject:
        parts.append(subject)
    targets = _targets(detail, names)
    if targets and action not in ("attack", "block", "declare_blockers"):
        parts.append("-> " + targets)
    return " ".join(parts)


def _kept(row: dict[str, Any], flt: LogFilter) -> bool:
    payload = row["payload"]
    detail = payload.get("detail") or {}
    turn = payload.get("turn")
    if flt.turn is not None and (turn is None or not flt.turn[0] <= turn <= flt.turn[1]):
        return False
    if flt.seat is not None and detail.get("player") != flt.seat:
        return False
    refused = detail.get("allowed") is False or payload.get("action") == "(unrecorded)"
    return not (flt.kinds == "plays" and refused)


def project(
    rows: list[dict[str, Any]],
    names: Names,
    flt: LogFilter,
    baseline: dict[str, Any] | None = None,
    reason_max: int | None = REASON_MAX,
    scrub: Callable[[str], str] = lambda s: s,
) -> list[LogLine]:
    """Render `rows` as compact-v1 lines, turn headers included.

    `baseline` is the snapshot of the row just before `rows[0]`; rows whose snapshot is None
    print no delta and do not reset the previous snapshot.
    """
    out: list[LogLine] = []
    prev = baseline
    last_turn: int | None = None
    announced = False
    index = 0
    snapshots: list[dict[str, Any] | None] = []
    for row in rows:
        snapshots.append(row["payload"].get("snapshot"))

    def emit(row: dict[str, Any], text: str, end_seq: int | None = None) -> None:
        nonlocal last_turn, announced
        payload = row["payload"]
        turn = payload.get("turn")
        if turn is not None and (not announced or turn != last_turn):
            head = scrub(ascii_text(_header_text(turn, payload)))
            out.append(LogLine(row["seq"], turn, head, header=True))
        if turn is not None:
            last_turn, announced = turn, True
        out.append(LogLine(row["seq"], turn, scrub(ascii_text(text)), end_seq=end_seq))

    while index < len(rows):
        row = rows[index]
        payload = row["payload"]
        snap = snapshots[index]
        if not _kept(row, flt):
            if snap is not None:
                prev = snap
            index += 1
            continue
        action = payload.get("action")
        if flt.kinds == "plays" and action in PASS_ACTIONS:
            end = index
            while (
                end + 1 < len(rows)
                and rows[end + 1]["payload"].get("action") in PASS_ACTIONS
                and _kept(rows[end + 1], flt)
                and rows[end + 1]["payload"].get("turn") == payload.get("turn")
                and rows[end + 1]["payload"].get("step") == payload.get("step")
            ):
                end += 1
            count = end - index + 1
            if count > 1:
                seats: list[str] = []
                for run_row in rows[index : end + 1]:
                    seat = (run_row["payload"].get("detail") or {}).get("player")
                    if seat is not None and seat not in seats:
                        seats.append(str(seat))
                step = payload.get("step")
                turn = payload.get("turn")
                step_text = "-" if step is None else STEP_SHORT.get(step, step)
                turn_text = "T?" if turn is None else f"T{turn}"
                text = f"#{row['seq']} {turn_text} {step_text} pass x{count}"
                if seats:
                    text += f" ({','.join(seats)})"
                last_snap = next(
                    (s for s in reversed(snapshots[index : end + 1]) if s is not None), None
                )
                text += _delta(prev, last_snap, flt.board)
                emit(row, text, end_seq=rows[end]["seq"])
                if last_snap is not None:
                    prev = last_snap
                index = end + 1
                continue
        text = _head(row, names) + _delta(prev, snap, flt.board)
        detail = payload.get("detail") or {}
        if flt.kinds == "all" and detail.get("allowed") is False:
            reason = " ".join(str(detail.get("reason") or "").split())[:REFUSED_REASON_MAX]
            text += f" REFUSED: {reason}"
        if flt.board == "full" and snap is not None:
            text += "\n   board: " + json.dumps(
                snap, separators=(",", ":"), ensure_ascii=True, sort_keys=True
            )
        if flt.reasoning:
            text += _why(payload.get("reasoning"), reason_max)
        emit(row, text)
        if snap is not None:
            prev = snap
        index += 1
    return out


# -- paging and budgets -------------------------------------------------------------------


def _drop_trailing_headers(lines: list[LogLine]) -> None:
    while lines and lines[-1].header:
        lines.pop()


def page(lines: list[LogLine], limit: int, max_chars: int) -> tuple[list[LogLine], bool]:
    """Keep lines in order up to `limit` action lines and `max_chars` rendered; `more` if cut."""
    kept: list[LogLine] = []
    size = 0
    actions = 0
    stopped = False
    for line in lines:
        if not line.header and actions >= limit:
            stopped = True
            break
        added = len(line.text) + (1 if kept else 0)
        if size + added > max_chars:
            if actions == 0 and not line.header:
                kept = [replace(line, text=line.text[: max_chars - len(CUT_MARK)] + CUT_MARK)]
                size = max_chars
                actions = 1
                continue
            stopped = True
            break
        kept.append(line)
        size += added
        if not line.header:
            actions += 1
    if stopped:
        _drop_trailing_headers(kept)
    return kept, len(kept) < len(lines)


def _marker(omitted: int, last_head_seq: int) -> LogLine:
    text = f"... {omitted} lines omitted; page with sim_match_log(since_seq={last_head_seq}) ..."
    return LogLine(last_head_seq, None, text, header=True)


def _rendered_len(lines: list[LogLine]) -> int:
    return sum(len(line.text) for line in lines) + max(len(lines) - 1, 0)


def head_tail(
    lines: list[LogLine],
    max_chars: int = WAIT_LOG_MAX_CHARS,
    head_turns: int = WAIT_HEAD_TURNS,
) -> tuple[list[LogLine], bool]:
    """All lines when they fit, else the first `head_turns` turns, a marker and the newest tail."""
    if _rendered_len(lines) <= max_chars:
        return lines, False
    seen: list[int] = []
    cut = len(lines)
    for position, line in enumerate(lines):
        if line.turn is not None and line.turn not in seen:
            if len(seen) >= head_turns:
                cut = position
                break
            seen.append(line.turn)
    head = list(lines[:cut])
    rest = lines[cut:]
    # The marker is sized for the worst case (every remaining line omitted).
    worst = _marker(len(lines), head[-1].end_seq or head[-1].seq if head else 0)
    # The head takes at most three quarters of the budget: the newest lines always get room.
    while head and _rendered_len([*head, worst]) > max_chars * 3 // 4:
        head.pop()
    last_seq = (head[-1].end_seq or head[-1].seq) if head else 0
    tail: list[LogLine] = []
    size = _rendered_len([*head, worst])
    for line in reversed(rest):
        if size + len(line.text) + 1 > max_chars:
            break
        tail.append(line)
        size += len(line.text) + 1
    tail.reverse()
    shown = len([x for x in head if not x.header]) + len([x for x in tail if not x.header])
    omitted = len([x for x in lines if not x.header]) - shown
    return [*head, _marker(omitted, last_seq), *tail], True


# -- export ------------------------------------------------------------------------------


def _scrub_values(value: Any, scrub: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _scrub_values(v, scrub) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub_values(v, scrub) for v in value]
    return value


def _cell(value: Any) -> str:
    return " ".join(str(value).split()).replace("|", "/")


def _header_rows(header: dict[str, Any]) -> list[tuple[str, str]]:
    decks = header.get("decks") or {}
    seats = header.get("seats") or {}
    result = header.get("result") or {}
    out: list[tuple[str, str]] = [
        ("Status", str(header.get("status"))),
        ("Decks", ", ".join(f"{seat}: {deck}" for seat, deck in decks.items())),
        (
            "Seats",
            ", ".join(
                f"{seat}: {s.get('model')} at {s.get('effort')}" for seat, s in seats.items()
            ),
        ),
        ("Seed", str(header.get("seed"))),
    ]
    if result:
        winner = result.get("winner")
        outcome = "draw" if result.get("drawn") else f"winner {winner}"
        out.append(("Result", f"{outcome}, {result.get('turns')} turns"))
    out.append(("Duration", f"{header.get('elapsed_s')} s"))
    if header.get("error"):
        out.append(("Error", str(header["error"])))
    if header.get("stalled_reason"):
        out.append(("Stall reason", str(header["stalled_reason"])))
    return out


def render_markdown(
    header: dict[str, Any],
    rows: list[dict[str, Any]],
    names: Names,
    scrub: Callable[[str], str],
) -> str:
    """The whole match as one Markdown document: header table, one section per turn, final board.

    Reasoning is not trimmed. Every line is scrubbed per substring; the text is ASCII.
    """
    out = [
        scrub(ascii_text(f"# Claude match {header.get('match_id')}")),
        "",
        "| Field | Value |",
        "| --- | --- |",
    ]
    out.extend(
        f"| {key} | {scrub(ascii_text(_cell(value)))} |" for key, value in _header_rows(header)
    )
    out.append("")
    if not rows:
        out.append("No actions were recorded for this match (no game log exists).")
    flt = LogFilter(kinds="plays", board="changes")
    lines = project(rows, names, flt, reason_max=None, scrub=scrub)
    active: dict[int, str] = {}
    for row in rows:
        payload = row["payload"]
        turn = payload.get("turn")
        if turn is not None and turn not in active:
            active[turn] = payload.get("active_player") or "-"
    section: int | None = None
    fenced = False
    for line in lines:
        if line.header:
            continue
        if not fenced or line.turn != section:
            if fenced:
                out.extend(["```", ""])
            title = (
                f"## Turn {line.turn} (active {active.get(line.turn, '-')})"
                if line.turn is not None
                else "## Before turn 1"
            )
            out.extend([scrub(ascii_text(title)), "", "```text"])
            section, fenced = line.turn, True
        out.append(line.text)
    if fenced:
        out.extend(["```", ""])
    last = next(
        (r["payload"]["snapshot"] for r in reversed(rows) if r["payload"].get("snapshot")), None
    )
    if last is not None:
        board = json.dumps(last, indent=2, sort_keys=True, ensure_ascii=True)
        out.extend(["## Final board", "", "```json", scrub(board), "```", ""])
    return "\n".join(out).rstrip("\n") + "\n"


def render_jsonl(rows: list[dict[str, Any]], scrub: Callable[[str], str]) -> str:
    """One JSON object per ledger row and line; every string value is scrubbed."""
    lines: list[str] = []
    for row in rows:
        payload = row["payload"]
        detail = payload.get("detail") or {}
        snapshot = payload.get("snapshot")
        record = {
            "seq": row["seq"],
            "ts": payload.get("ts"),
            "turn": payload.get("turn"),
            "step": payload.get("step"),
            "phase": payload.get("phase"),
            "actor": detail.get("player"),
            "action": payload.get("action"),
            "detail": detail,
            "life": snapshot.get("life") if snapshot else None,
            "reasoning": payload.get("reasoning"),
        }
        lines.append(json.dumps(_scrub_values(record, scrub), ensure_ascii=True, sort_keys=True))
    return "".join(f"{line}\n" for line in lines)
