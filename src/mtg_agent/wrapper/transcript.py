"""Reading the JSONL session transcript claude writes.

Two questions are asked of it. `last_assistant` answers "what did the agent
finally say, with what usage" - the text is only a fallback, since the Stop
hook payload carries `last_assistant_message` authoritatively.
`sum_turn_tokens` answers "what did this turn cost", summed across every
assistant message of the last turn: the single last-message usage badly
undercounts an agentic turn that made twenty tool calls.
`pending_background_tasks` answers "is this turn's work still running": the
subagents, teammates and background commands the turn launched that have not
reported back or been shut down - minus a seat pair's `future-N` helper, which
plans for the next decision and so is never an open turn.

A turn boundary is a `user` line whose `content` is a plain string. Tool
results arrive as `user` lines too, but carry an array `content`, so they do
not reset the accumulator.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..seat_pair import ROLE_CURRENT, ROLE_FUTURE

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


_ROLE_RE = re.compile(rf"\b({ROLE_CURRENT}|{ROLE_FUTURE})-\d+\b")
_TEAMMATE_MESSAGE_RE = re.compile(r"<teammate-message\b[^>]*>(.*?)</teammate-message>", re.DOTALL)
_SHUT_DOWN_RE = re.compile(r"(\S+) has shut down")
_AGENT_TOOLS = frozenset({"Agent", "Task"})
_SHUTDOWN_RESPONSES = frozenset({"shutdown_response", "shutdown_approved"})


def helper_role(*texts: object) -> str:
    """`ROLE_CURRENT` / `ROLE_FUTURE` when the first of `texts` naming a seat-pair
    helper (`current-N`, `future-N`) does, else "" (some other background task)."""
    for text in texts:
        match = _ROLE_RE.search(str(text or ""))
        if match:
            return match.group(1)
    return ""


@dataclass
class _Task:
    """One piece of background work: the tool call that started it (what
    `pending_background_tasks` returns), every handle it answers to (task and
    agent ids, a teammate's name), and its seat-pair role."""

    key: str
    ids: set[str]
    role: str = ""
    launched: bool = False


def _meta_texts(transcript: Path, agent_id: str) -> list[str]:
    """`name` and `description` from the helper's `agent-<id>.meta.json` beside
    the lead transcript: a background subagent's name is only recorded there."""
    meta = transcript.with_suffix("") / "subagents" / f"agent-{agent_id}.meta.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return (
        [str(data.get(key) or "") for key in ("name", "description")]
        if isinstance(data, dict)
        else []
    )


def _finished_by_tool(name: str, tool_input: dict, result: object) -> set[str]:
    """The handles a TaskStop, or a TaskOutput that got the task's end, finished."""
    task_id = str(tool_input.get("task_id") or tool_input.get("bash_id") or "")
    if not task_id or not isinstance(result, dict) or result.get("is_error"):
        return set()
    if name in ("TaskStop", "KillShell", "KillBash"):
        return {task_id}
    if name in ("TaskOutput", "BashOutput", "AgentOutputTool"):
        task = result.get("task") if isinstance(result.get("task"), dict) else {}
        status = str(task.get("status") or result.get("status") or "")
        if result.get("retrieval_status") == "success" or (
            status and status not in _RUNNING_STATUSES
        ):
            return {task_id}
    return set()


def _shutdown_target(name: str, tool_input: dict, result: object) -> str:
    """The teammate a `SendMessage` shutdown_request that went through names."""
    message = tool_input.get("message")
    kind = tool_input.get("type") or (message.get("type") if isinstance(message, dict) else "")
    if name != "SendMessage" or kind != "shutdown_request":
        return ""
    if not isinstance(result, dict) or not result.get("success"):
        return ""
    return str(result.get("target") or tool_input.get("to") or tool_input.get("recipient") or "")


def _ended_teammates(text: str) -> set[str]:
    """Names of teammates a teammate-message block says have gone: the system's
    `teammate_terminated`, or a teammate's approved `shutdown_response`."""
    ended: set[str] = set()
    for body in _TEAMMATE_MESSAGE_RE.findall(text):
        try:
            data = json.loads(body.strip())
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        kind = data.get("type")
        if kind == "teammate_terminated":
            match = _SHUT_DOWN_RE.search(str(data.get("message") or ""))
            if match:
                ended.add(match.group(1))
        elif kind in _SHUTDOWN_RESPONSES and data.get("approve") is not False:
            ended.add(str(data.get("from") or ""))
    ended.discard("")
    return ended


def _background_tasks(path: Path) -> list[_Task]:
    """Every task launched since the last `TURN_MARKER` prompt that is still
    live, in launch order. See `pending_background_tasks`."""
    lines = [line for line in _lines(path) if not line.get("isSidechain")]
    start = 0
    for index, line in enumerate(lines):
        if any(TURN_MARKER_RE.search(text) for text in _prompt_texts(line)):
            start = index
    live: dict[str, _Task] = {}
    tool_uses: dict[str, tuple[str, dict]] = {}
    roles: dict[str, str] = {}  # agent id -> role, for a resume by SendMessage

    def finish(handles: set[str]) -> None:
        for key in [k for k, task in live.items() if task.ids & handles]:
            del live[key]

    for line in lines[start:]:
        content = (line.get("message") or {}).get("content")
        if line.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_id = str(block.get("id"))
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                name = str(block.get("name") or "")
                tool_uses[tool_id] = (name, tool_input)
                if name in _AGENT_TOOLS and (
                    tool_input.get("run_in_background") or tool_input.get("name")
                ):
                    # The hook can run before the launch's result is on disk;
                    # the call being unanswered is itself a pending launch.
                    live[tool_id] = _Task(
                        tool_id,
                        {tool_id},
                        helper_role(tool_input.get("name"), tool_input.get("description")),
                    )
        elif line.get("type") == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_id = str(block.get("tool_use_id") or "")
                result = line.get("toolUseResult")
                name, tool_input = tool_uses.get(tool_id, ("", {}))
                pending = live.get(tool_id)
                if pending is not None and not pending.launched:
                    del live[tool_id]  # answered: re-added below only if it launched
                stopped = _finished_by_tool(name, tool_input, result)
                if stopped:
                    finish(stopped)
                    continue
                target = _shutdown_target(name, tool_input, result)
                if target:
                    finish({target})
                    continue
                if not isinstance(result, dict):
                    result = {}
                if result.get("status") == "teammate_spawned":
                    handles = {tool_id}
                    for key in ("agent_id", "name"):
                        value = result.get(key) or tool_input.get(key)
                        if isinstance(value, str) and value:
                            handles.add(value)
                    role = helper_role(result.get("name"), tool_input.get("name"))
                    live[tool_id] = _Task(tool_id, handles, role, launched=True)
                    continue
                resumed = result.get("resumedAgentId")
                if isinstance(resumed, str) and resumed:
                    live[tool_id] = _Task(
                        tool_id, {resumed, tool_id}, roles.get(resumed, ""), launched=True
                    )
                    continue
                ids = _launch_ids(block, result)
                if ids is not None:
                    role = helper_role(tool_input.get("name"), tool_input.get("description"))
                    for agent_id in ids:
                        role = role or helper_role(*_meta_texts(path, agent_id))
                    for agent_id in ids:
                        roles[agent_id] = role
                    live[tool_id] = _Task(tool_id, ids | {tool_id}, role, launched=True)
        for text in _prompt_texts(line):
            for body in _NOTIFICATION_RE.findall(text):
                tags = dict(_TAG_RE.findall(body))
                if tags.get("status", "").lower() in _RUNNING_STATUSES:
                    continue
                finish({value for key, value in tags.items() if key != "status" and value})
            finish(_ended_teammates(text))
    return list(live.values())


def pending_background_tasks(path: Path) -> list[str]:
    """The tool-use ids of the open-turn work launched since the last
    `TURN_MARKER` prompt that has not ended, in launch order.

    Claude Code answers a background launch at once (an `Agent` tool result
    "Async agent launched successfully", a `Bash` with `backgroundTaskId`, a
    teammate's `teammate_spawned`), and reports a subagent's end as a
    `<task-notification>` prompt naming the launch's `<tool-use-id>` and
    `<task-id>` with a terminal `<status>`, taken in as a `user` prompt or a
    `queued_command` attachment. A subagent resumed by `SendMessage` ("Resuming
    agent", `resumedAgentId`) is a new launch that reports the same way. A
    teammate lives until it is shut down (`teammate_terminated`, an approved
    `shutdown_response`, or a `shutdown_request` that went through) or stopped;
    going idle does not end it. A `TaskStop`, or a `TaskOutput` that retrieved
    the task's end, may get no notification after it, so those finish a task
    too. A launch whose result is not yet written still counts.

    A seat pair's `future-N` helper is left out: it plans the next decision and
    so is never reason to hold the turn open, while a `current-N` (a decision in
    flight) and any other helper or command are. Lines of a subagent's own
    sidechain are not this turn's launches. Everything before the last marker
    belongs to an earlier turn."""
    return [task.key for task in _background_tasks(path) if task.role != ROLE_FUTURE]
