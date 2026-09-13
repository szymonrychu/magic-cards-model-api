"""Reading the JSONL session transcript claude writes.

Two questions are asked of it. `last_assistant` answers "what did the agent
finally say, with what usage" - the text is only a fallback, since the Stop
hook payload carries `last_assistant_message` authoritatively.
`sum_turn_tokens` answers "what did this turn cost", summed across every
assistant message of the last turn: the single last-message usage badly
undercounts an agentic turn that made twenty tool calls.

A turn boundary is a `user` line whose `content` is a plain string. Tool
results arrive as `user` lines too, but carry an array `content`, so they do
not reset the accumulator.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

#: The line `Manager.submit` appends to every turn's prompt, naming the turn.
#: The Stop hook payload carries no turn id at all, so this is how the hook
#: says which turn stopped: the last marker in a prompt claude took in.
TURN_MARKER = "<!-- wrapper turn: {turn_id} -->"
TURN_MARKER_RE = re.compile(r"<!-- wrapper turn: (turn-[0-9a-z]+) -->")


def turn_marker(turn_id: str) -> str:
    return TURN_MARKER.format(turn_id=turn_id)


@dataclass
class TurnTokens:
    model: str
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0


def _lines(path: Path):
    try:
        with Path(path).open(encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    yield parsed
    except OSError:
        return


def last_assistant(path: Path) -> tuple[str, dict | None, str]:
    """`(text, usage, stop_reason)` of the final assistant line. Text falls
    back to the last line that actually had text, because a turn often ends
    on a tool-call-only assistant line that carries usage but says nothing."""
    text = ""
    usage: dict | None = None
    stop = ""
    for line in _lines(path):
        if line.get("type") != "assistant":
            continue
        message = line.get("message") or {}
        blocks = message.get("content") or []
        joined = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        raw_usage = message.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else usage
        stop = message.get("stop_reason") or ""
        if joined:
            text = joined
    return text, usage, stop


def _prompt_texts(line: dict) -> list[str]:
    """The prompt text a line gave claude: a typed or pasted `user` prompt
    (string content, or its text blocks - never a tool result), or a prompt
    queued mid-turn and taken in (`attachment` of type `queued_command`). An
    `enqueue` `queue-operation` is not one: the turn running then never saw it."""
    if line.get("type") == "user":
        content = (line.get("message") or {}).get("content")
    elif line.get("type") == "attachment":
        attachment = line.get("attachment") or {}
        if attachment.get("type") != "queued_command":
            return []
        content = attachment.get("prompt")
    else:
        return []
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
    return []


def last_turn_marker(path: Path) -> str:
    """The turn id of the last `TURN_MARKER` in a prompt claude took in, or ""."""
    found = ""
    for line in _lines(path):
        for text in _prompt_texts(line):
            for match in TURN_MARKER_RE.finditer(text):
                found = match.group(1)
    return found


def sum_turn_tokens(path: Path) -> list[TurnTokens]:
    """Per-model token totals for the LAST turn in the transcript."""
    by_model: dict[str, TurnTokens] = {}
    for line in _lines(path):
        kind = line.get("type")
        message = line.get("message") or {}
        if kind == "user" and isinstance(message.get("content"), str):
            by_model = {}
            continue
        if kind != "assistant":
            continue
        usage = message.get("usage") or {}
        model = message.get("model") or ""
        totals = by_model.setdefault(model, TurnTokens(model=model))
        totals.input += int(usage.get("input_tokens") or 0)
        totals.output += int(usage.get("output_tokens") or 0)
        totals.cache_read += int(usage.get("cache_read_input_tokens") or 0)
        totals.cache_creation += int(usage.get("cache_creation_input_tokens") or 0)
    return list(by_model.values())
