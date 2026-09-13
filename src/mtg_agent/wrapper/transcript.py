"""Reading the JSONL session transcript claude writes.

Two questions are asked of it. `last_assistant` answers "what did the agent
finally say, with what usage" - the text is only a fallback, since the Stop
hook payload carries `last_assistant_message` authoritatively.
`sum_turn_tokens` answers "what did this turn cost", summed across every
assistant message of the last turn: the single last-message usage badly
undercounts an agentic turn that made twenty tool calls.
`pending_background_tasks` answers "is this turn's work still running": the
subagents and background commands the turn launched that have not reported
back.

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


#: A task-notification status that is not an end: the task is still going.
_RUNNING_STATUSES = frozenset({"running", "pending"})
_NOTIFICATION_RE = re.compile(r"<task-notification>(.*?)</task-notification>", re.DOTALL)
_TAG_RE = re.compile(r"<(task-id|tool-use-id|status)>\s*([^<]*?)\s*</\1>")
_LAUNCH_TEXTS = (
    "Async agent launched",
    "running in background with ID",
    "launched in background",
)


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _launch_ids(block: dict, result: object) -> set[str] | None:
    """The ids a background launch answers to (its task or agent id), or None
    when this tool result is not a background launch."""
    ids: set[str] = set()
    launched = False
    if isinstance(result, dict):
        for key in ("backgroundTaskId", "agentId", "taskId"):
            value = result.get(key)
            if isinstance(value, str) and value:
                ids.add(value)
        launched = bool(result.get("isAsync") or result.get("backgroundTaskId")) or (
            bool(result.get("taskId")) and "retrieval_status" not in result
        )
    text = _tool_result_text(block)
    if any(needle in text for needle in _LAUNCH_TEXTS):
        launched = True
        ids.update(re.findall(r"(?:agentId|with ID|Task ID): ?([A-Za-z0-9_-]+)", text))
    return ids if launched else None


def _finished_by_tool(name: str, tool_input: dict, result: object) -> str:
    """The task id a TaskStop, or a TaskOutput that got the task's end, finished."""
    task_id = str(tool_input.get("task_id") or tool_input.get("bash_id") or "")
    if not task_id or not isinstance(result, dict) or result.get("is_error"):
        return ""
    if name in ("TaskStop", "KillShell", "KillBash"):
        return task_id
    if name in ("TaskOutput", "BashOutput", "AgentOutputTool"):
        task = result.get("task") if isinstance(result.get("task"), dict) else {}
        status = str(task.get("status") or result.get("status") or "")
        if result.get("retrieval_status") == "success" or (
            status and status not in _RUNNING_STATUSES
        ):
            return task_id
    return ""


def pending_background_tasks(path: Path) -> list[str]:
    """The tool-use ids of the subagents and background commands launched since
    the last `TURN_MARKER` prompt that have not reported back, in launch order.

    Claude Code answers a background launch at once (an `Agent` tool result
    "Async agent launched successfully", a `Bash` with `backgroundTaskId`), and
    reports its end as a `<task-notification>` prompt naming the launch's
    `<tool-use-id>` and `<task-id>` with a terminal `<status>`, taken in as a
    `user` prompt or a `queued_command` attachment. A `TaskStop`, or a
    `TaskOutput` that retrieved the task's end, may get no notification after
    it, so those finish a task too. Lines of a subagent's own sidechain are
    not this turn's launches. Everything before the last marker belongs to an
    earlier turn."""
    lines = [line for line in _lines(path) if not line.get("isSidechain")]
    start = 0
    for index, line in enumerate(lines):
        if any(TURN_MARKER_RE.search(text) for text in _prompt_texts(line)):
            start = index
    launched: dict[str, set[str]] = {}
    tool_uses: dict[str, tuple[str, dict]] = {}
    finished: set[str] = set()
    for line in lines[start:]:
        content = (line.get("message") or {}).get("content")
        if line.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_input = block.get("input")
                    tool_uses[str(block.get("id"))] = (
                        str(block.get("name") or ""),
                        tool_input if isinstance(tool_input, dict) else {},
                    )
        elif line.get("type") == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_id = str(block.get("tool_use_id") or "")
                result = line.get("toolUseResult")
                name, tool_input = tool_uses.get(tool_id, ("", {}))
                done = _finished_by_tool(name, tool_input, result)
                if done:
                    finished.add(done)
                    continue
                ids = _launch_ids(block, result)
                if ids is not None:
                    launched[tool_id] = ids | {tool_id}
        for text in _prompt_texts(line):
            for body in _NOTIFICATION_RE.findall(text):
                tags = dict(_TAG_RE.findall(body))
                if tags.get("status", "").lower() in _RUNNING_STATUSES:
                    continue
                finished.update(value for key, value in tags.items() if key != "status" and value)
    return [tool_id for tool_id, ids in launched.items() if not ids & finished]
