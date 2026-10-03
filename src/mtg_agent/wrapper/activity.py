"""What the agent is actually doing, one line at a time, on stdout.

`docker compose logs` had no window into the seat's thinking. The wrapper
printed "turn submitted" and, half an hour later, "turn complete"; everything
between - the agent's reasoning, every seat command it ran, every rules
lookup - happened inside an interactive TUI in another process and reached
nobody. The driver's `finalText` block (see `driver/app.py`) is a summary
written after the fact, not a running account.

There are two places that account could come from:

- **the PTY stream.** `session.Ring` already carries it, which is tempting.
  But it is a *rendered terminal*: box-drawing, spinners, ANSI, and lines
  that get repainted in place. Parsing it means reverse-engineering a TUI
  that changes with every claude release - the ring's own docstring says
  turn results must never come from there, and it is right.
- **the JSONL transcript claude writes as it goes.** Structured, append-only,
  one object per line, with the assistant's text and every `tool_use` block
  in it. `transcript.py` already reads this file after a turn; this module
  reads it *during* one.

So: the transcript, tailed. The path is not known up front (the Stop hook
only reports it once a turn has ended), so the watcher globs
`$HOME_DIR/.claude/projects/*/*.jsonl` and follows the newest file, picking
up a new session file when one appears.

With the seat pair (`mtg_agent.seat_pair`) that file is only the
orchestrator's. Every subagent or teammate the session spawns writes its own
transcript next to it, `<session>/subagents/agent-<agentId>.jsonl` with its
spawn metadata in `agent-<agentId>.meta.json` (layout recorded by T1 in
`tests/wrapper/fixtures/seat_pair/README.md`). The watcher follows all of
them and tags each with a role from its agent name (`role_for_name`,
`AgentRoster`): `orchestrator` for the main transcript, `current`/`future` for
the seat pair's players, `helper` for anything else (the rules researcher).
The AI pill shows the current player, metrics carry the role, and the stall
timer counts a line from any of them as progress.

Verbosity is `AGENT_LOG_LEVEL`, and governs the text log only - `off`/`turns`
still feed `ccw_inference_seconds` (see `InferenceTimer`), since that is a
metric a production wrapper wants regardless of how loud its log is:

    off       nothing logged
    turns     turn boundaries only (what the wrapper always logged)
    messages  + the agent's own prose
    tools     + every tool call and every tool failure   [default]

Every event is the full content, not a head with the rest elided. An earlier
version cut each line to a few hundred characters with a `[+N chars]` marker;
that hid exactly the part an operator reading `kubectl logs` needs when
debugging what the agent decided and why, so it is gone. `AGENT_LOG_LEVEL` is
still the way to get a terser log - drop to `messages` or `turns` to lose
whole categories of line - but a line that is emitted is emitted whole.

An agent message can be a page and a Bash command can be a hundred-line
heredoc; both contain newlines, and a raw multi-line dump would interleave
badly with the rest of the compose log and break line-oriented log shipping
(kubectl/docker compose logs, and anything reading this stream expect one
event per physical line). So each event's whitespace - including every
newline - is collapsed to single spaces before it is emitted: one event stays
one physical line, and every word of it is still there in order. The full
text with its original line breaks is always on `GET /v1/messages/{turnId}`
and in the transcript, for a reader that wants the original formatting.

The same tail feeds two things that are not logs at all: `ccw_inference_
seconds` (see `InferenceTimer`) and the coarse "what is the agent doing right
now" word the play UI's AI pill shows (see `CurrentActivity`, and the block
comment above it for the two rules that word obeys). Both are fed regardless
of `AGENT_LOG_LEVEL`.

Nothing here can fail a turn: the thread catches everything and keeps going.
Logging is not worth breaking a game over.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ..seat_pair import ROLE_CURRENT, ROLE_FUTURE
from .bootstrap import RULES_RESEARCHER
from .metrics import ORCHESTRATOR, Metrics

log = logging.getLogger("agent")

#: How often the watcher looks for new transcript bytes, and for a newer
#: session file. Fast enough that a line lands while the agent is still on the
#: same thought; slow enough to be free.
POLL_SECONDS = 0.5

_LEVELS = ("off", "turns", "messages", "tools")


def level_rank(level: str) -> int:
    """`AGENT_LOG_LEVEL` as an ordinal. Anything unrecognised is the default,
    because a typo in an env var must not silence the log."""
    try:
        return _LEVELS.index((level or "").strip().lower())
    except ValueError:
        return _LEVELS.index("tools")


# ---- redaction -------------------------------------------------------------

#: Anything shaped like a credential. The stack handles an ANTHROPIC_API_KEY
#: and a wrapper shared secret, and the agent's own Bash commands can echo an
#: environment. The container also has a GitHub PAT: no longer an env var,
#: but mounted as a file at /var/run/mtg-decks-git/GH_PAT (deploy/seat-job.yaml)
#: that git's credential helper reads to push to the deck-guides repo - and
#: the agent can still `cat` that file, or paste the token into a remote URL
#: or a curl header. So the redaction stays; none of that goes to stdout.
#: Seat uids are NOT
#: redacted - they are already in every URL in this log and identify which
#: seat is talking.
#:
#: Now that lines carry full content instead of a truncated head, a
#: credential can no longer hide past the cut - these patterns are what
#: stands between "full logs" and "full logs including a token".
_SECRETS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # an ANTHROPIC_API_KEY
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),  # an Authorization header
    re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{20,}"),  # a classic GitHub token (GH_PAT et al)
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),  # a fine-grained GitHub PAT
    re.compile(r"://[^\s/@]+:[^\s/@]+@"),  # user:secret@ basic auth in any URL
)

#: `token=...`, `secret: ...`, and friends - anything the credential-shaped
#: patterns above don't already catch by their own value shape.
_KEY_VALUE_SECRET = re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\b(\s*[:=]\s*)\S+")


def redact(text: str) -> str:
    out = str(text)
    out = _SECRETS[0].sub("sk-ant-<redacted>", out)
    out = _SECRETS[1].sub("Bearer <redacted>", out)
    out = _SECRETS[2].sub("<redacted>", out)
    out = _SECRETS[3].sub("<redacted>", out)
    out = _SECRETS[4].sub("://<redacted>@", out)
    return _KEY_VALUE_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", out)


def one_line(text: str) -> str:
    """Collapse whitespace - including every newline - to single spaces, and
    redact. No length limit: the full content is the point. Collapsing
    whitespace, rather than cutting the text, is what keeps one event on one
    physical line for a line-oriented log reader."""
    return " ".join(redact(str(text)).split())


# ---- turning transcript lines into events ----------------------------------

#: Tool -> the field that says what it is about. A generic JSON dump of the
#: input is technically complete and unreadable; this is the one thing a
#: person scanning the log wants to see.
_HEADLINE_FIELD = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "Task": "description",
    "WebFetch": "url",
    "TodoWrite": "",
}


def describe_tool(name: str, payload: Any) -> str:
    """`name` plus the readable part of its input, on one line."""
    if not isinstance(payload, dict):
        return one_line(f"{name} {payload}")
    field = _HEADLINE_FIELD.get(name)
    if field:
        value = payload.get(field)
        if value:
            return one_line(f"{name} {value}")
    if field == "":
        return one_line(name)
    try:
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(payload)
    if rendered in ("{}", "null"):
        return one_line(name)
    return one_line(f"{name} {rendered}")


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


def events(line: dict) -> Iterator[tuple[str, str]]:
    """`(kind, text)` for one transcript object.

    Kinds are `say`, `tool` and `tool-error`. `thinking` blocks are skipped
    on purpose: they are the agent talking to itself, and the text blocks
    say the same thing in a form meant to be read.

    This used to add "and they are long", which is simply not true of what
    reaches this function. Measured on a live pod and independently on a
    second machine: 36 `thinking` blocks in the transcript, **zero**
    characters of text between them - they arrive carrying a signature and
    nothing else. So there is no thinking text here to harvest, skipped or
    not; anything that wants the agent's reasoning has to ask the agent for
    it (`POST /api/games/{id}/plan`, and the per-action `reasoning` every
    mutating route already requires), not mine it out of a transcript.
    """
    kind = line.get("type")
    message = line.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if kind == "assistant":
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text", "")).strip():
                yield "say", one_line(block["text"])
            elif block.get("type") == "tool_use":
                yield "tool", describe_tool(str(block.get("name") or "?"), block.get("input"))
        return
    if kind == "user" and isinstance(content, list):
        # Only *failures*. A successful tool result is the board state the
        # agent asked for; echoing it would be the whole game twice over.
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if not block.get("is_error"):
                continue
            yield "tool-error", one_line(_result_text(block.get("content")))


# ---- what the agent is doing right now, as one coarse word -----------------
#
# The play UI's AI pill used to read "AI (claude): thinking" for the whole of
# a match, whatever the agent was doing, because the seat status is the only
# thing the browser can see and all it knew was "a turn is in flight". The
# transcript this module already tails knows far more than that - so the
# watcher publishes one word off it, the driver carries that word to the game
# server on `GET /status`, and the pill says what the agent is actually up to.
#
# Two rules shape everything below, and neither is negotiable:
#
# - **Observed, never inferred.** A state is set by an event that happened,
#   and nothing else moves it. No timer promotes silence into a narrative;
#   a quiet agent keeps whatever it was last seen doing, and the age of that
#   observation is published alongside it so a reader can discount it.
# - **The word, and nothing else.** The human opponent reads this label
#   mid-game. A tool argument, a card name or a line of the agent's prose
#   reaching it would hand them the agent's plan and its read of the board -
#   the leak `visibility.py` had to be changed to close. `observations`
#   therefore yields the event kind and the *tool name*, never any content,
#   and `state_for` maps that to a fixed word; there is no channel here for
#   text to travel down, by construction rather than by filtering.

#: `mcp__mtg__wait_for_priority`/`pass_until` - parked, waiting for its
#: moment to act. `pass_until` passes priority too, but it re-issues its own
#: capped wait under one call while the opponent thinks (L2,
#: `mcp.IDLE_REISSUE_SECONDS`), so for most of the call it is waiting.
WAITING = "waiting"
#: `battlefield_state`/`game_log` - reading the board it is allowed to see.
READING_BOARD = "reading_board"
#: A spellslinger card lookup.
READING_CARDS = "reading_cards"
#: A spellslinger rules lookup (`knowledge_search`), and anything else on
#: that server: it is the cards-and-rules knowledge service, so "looking
#: something up" is the honest default for a tool this mapping has not met.
READING_RULES = "reading_rules"
#: Handing a rules question to its background `rules-researcher` subagent
#: (`bootstrap.RULES_RESEARCHER`) - one observation, at the launch; the seat
#: goes straight back to waiting, and nothing the subagent does after that is
#: the seat's own activity (see `observations`).
RESEARCHING_RULES = "researching_rules"
#: Answering something the game asked it: a pending decision, a mulligan, an
#: opening-hand action, or the plan it has to declare before it acts.
DECIDING = "deciding"
#: Any other `mtg` tool - all of them mutate the game.
ACTING = "acting"
#: A `thinking` block, or the agent's own prose. `thinking` blocks arrive
#: carrying a signature and ZERO characters of text (measured on a live pod
#: and independently on a second machine, 36 blocks and nothing between
#: them), so their presence is the whole of the evidence: the model is
#: reasoning. What about is not in the transcript and is not shown.
THINKING = "thinking"
#: Everything else the agent can do - `Bash`, `Read`, `Write`, its todo list,
#: a tool from a server nobody here has met. Deliberately vague, because
#: this is exactly the case where a specific word would be a guess.
WORKING = "working"

_MTG_PREFIX = "mcp__mtg__"
_SPELLSLINGER_PREFIX = "mcp__spellslinger__"

#: The subagent-launching tool, under its current name and its old one.
_AGENT_TOOLS = ("Agent", "Task")
#: `Agent:rules-researcher` - a launch of that one subagent type, as
#: `observations` names it. The type is a fixed word from this stack's own
#: config, never the launch's question.
_RESEARCHER_LAUNCHES = {f"{tool}:{RULES_RESEARCHER}" for tool in _AGENT_TOOLS}

#: The `mtg` tools that are not plays. Everything else the server offers
#: changes the game, so `ACTING` is its default - see `state_for`.
_MTG_STATES = {
    "wait_for_priority": WAITING,
    "pass_until": WAITING,
    "battlefield_state": READING_BOARD,
    "game_log": READING_BOARD,
    "decide": DECIDING,
    "mulligan": DECIDING,
    "opening_hand_action": DECIDING,
    "declare_plan": DECIDING,
    "list_scripts": READING_BOARD,
    "run_script": ACTING,
}


def state_for(kind: str, name: str) -> str | None:
    """One observation - `(kind, tool name)` from `observations` - as one of
    the coarse states above, or `None` for an event that says nothing about
    what the agent is doing.

    `None` means "leave the last observed state standing". It is not a state
    of its own and never reaches a reader: an event this function does not
    recognise must not be dressed up as activity.
    """
    if kind in ("thinking", "say"):
        return THINKING
    if kind != "tool":
        return None
    tool = str(name or "")
    if tool in _RESEARCHER_LAUNCHES:
        return RESEARCHING_RULES
    if tool.startswith(_MTG_PREFIX):
        return _MTG_STATES.get(tool[len(_MTG_PREFIX) :], ACTING)
    if tool.startswith(_SPELLSLINGER_PREFIX):
        # This repo does not pin spellslinger's tool list (it is another
        # service's contract), so the split is on the name: `resolve_cards`
        # and anything else card-shaped is a card lookup, the rest of the
        # server is a rules lookup. Both are honest at this grain - the
        # agent is looking something up either way.
        return READING_CARDS if "card" in tool[len(_SPELLSLINGER_PREFIX) :] else READING_RULES
    return WORKING


def observations(line: dict, include_sidechain: bool = False) -> Iterator[tuple[str, str]]:
    """`(kind, tool name)` for one transcript object - the activity-facing
    counterpart of `events` above.

    Deliberately a separate parser rather than a filter over `events`: this
    one never touches a tool's input or a text block's body, so no content
    can reach the published state even by accident. `thinking` blocks, which
    `events` skips because they carry no readable text, are exactly what
    this one wants: their presence is the evidence.

    A subagent's own sidechain line is not an observation of the main
    transcript's agent: the seat launched it and went back to waiting, so its
    researcher's lookups must not move the seat's published state off
    `waiting`. `include_sidechain` is for a helper's own transcript (every
    line of which is a sidechain line), tracked as that helper's activity.

    A `user` line carrying a `tool_result` is an observation too, and yields
    `("thinking", "")` - the same kind a `thinking` block does. This used to
    be ignored outright, which is exactly what let a real stall through
    unlabelled (game 048e1879, 2026-09-23): `opening_hand_action` returned at
    17:28:46, and one Opus call at EFFORT=high then thought for 106s (7000
    thinking tokens) with nothing else written to the transcript before its
    next `assistant` block - so the published state sat on stale 'deciding'
    with no elapsed time for the whole 106s, and the stall banner, which
    defers to a live activity label, could not fire either. The instant a
    tool call returns, the model is back to reasoning about what it read,
    which is real evidence of activity even though this parser is never
    shown what about - so the state moves to THINKING from that moment, and
    `CurrentActivity` ages it from this line's own timestamp rather than
    from whatever tool call preceded it.
    """
    kind = line.get("type")
    if kind not in ("assistant", "user") or (line.get("isSidechain") and not include_sidechain):
        return
    message = line.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    if kind == "user":
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield "thinking", ""
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "thinking":
            yield "thinking", ""
        elif block_type == "text" and str(block.get("text", "")).strip():
            yield "say", ""
        elif block_type == "tool_use":
            yield "tool", _tool_name(block)


def _tool_name(block: dict) -> str:
    """The tool name, and for a `rules-researcher` launch that subagent type
    too - the one input field read here, and only compared against a fixed
    word, so no argument text can travel on."""
    name = str(block.get("name") or "")
    tool_input = block.get("input")
    if (
        name in _AGENT_TOOLS
        and isinstance(tool_input, dict)
        and tool_input.get("subagent_type") == RULES_RESEARCHER
    ):
        return f"{name}:{RULES_RESEARCHER}"
    return name


def emit(kind: str, text: str, rank: int) -> None:
    """One event to the `agent` logger, if the level allows it."""
    if kind == "say":
        if rank >= level_rank("messages"):
            log.info("says: %s", text)
    elif kind == "tool":
        if rank >= level_rank("tools"):
            log.info("calls %s", text)
    elif kind == "tool-error" and rank >= level_rank("tools"):
        log.warning("tool failed: %s", text)


def log_lines(lines: Iterable[dict], rank: int, who: str = "") -> int:
    """Log every event of `lines`; `who` (a helper's role and name) prefixes
    each so a helper's line is never read as the seat's own."""
    count = 0
    for line in lines:
        for kind, text in events(line):
            emit(kind, f"[{who}] {text}" if who else text, rank)
            count += 1
    return count


# ---- following the file ----------------------------------------------------


class TranscriptTail:
    """Byte offset into one JSONL file, and the parsed objects since last read.

    Holds a partial trailing line back until its newline arrives: claude
    appends a whole object at a time, but a read can still land mid-write.
    A file that got shorter (a new session reusing the name, a truncation)
    resets to the start rather than decoding from a stale offset.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self._partial = ""

    def read(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            self.offset, self._partial = 0, ""
        if size == self.offset:
            return []
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                blob = handle.read(size - self.offset)
        except OSError:
            return []
        self.offset += len(blob)
        text = self._partial + blob.decode("utf-8", "replace")
        chunks = text.split("\n")
        self._partial = chunks.pop()
        out = []
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                parsed = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
        return out


def _parse_ts(raw: Any) -> float | None:
    """A transcript line's own `timestamp` field (ISO 8601, `...Z`), as
    epoch seconds - or `None` for a missing/unparsable one. Using the
    JSONL's own timestamps rather than local read time is what makes the
    duration below exact regardless of how often the watcher polls."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class CurrentActivity:
    """The last thing the agent was observed doing, and how long ago.

    Fed transcript lines in order; `snapshot()` is what `GET /v1/session`
    publishes and, through the driver, what the play UI's AI pill reads.

    The age is computed *here*, in seconds, rather than shipping a timestamp
    downstream: this clock and the transcript's own timestamps belong to the
    same host, so no skew between the agent container and a browser can make
    a fresh observation look stale or a stale one look fresh. A negative age
    (a line stamped in the future by a resyncing clock) publishes as zero
    rather than as a number nothing downstream would know what to do with.
    """

    def __init__(
        self, now: Callable[[], float] = time.time, include_sidechain: bool = False
    ) -> None:
        self._now = now
        self._include_sidechain = include_sidechain
        self._lock = threading.Lock()
        self._state: str | None = None
        self._at = 0.0
        self._seq = 0

    def feed(self, lines: Iterable[dict]) -> None:
        for line in lines:
            self._feed_one(line)

    def _feed_one(self, line: dict) -> None:
        stamped = _parse_ts(line.get("timestamp"))
        for kind, name in observations(line, self._include_sidechain):
            state = state_for(kind, name)
            if state is None:
                continue
            with self._lock:
                self._state = state
                self._at = stamped if stamped is not None else self._now()
                self._seq += 1

    def snapshot(self) -> dict[str, Any] | None:
        """`{state, seconds, seq}`, or `None` before anything was observed.

        `seq` counts observations rather than state changes, so a reader can
        tell "still reading the board, three tools later" from "nothing has
        happened since I last looked" - which is the only honest way to know
        an agent has gone quiet without guessing from a timer.
        """
        with self._lock:
            if self._state is None:
                return None
            return {
                "state": self._state,
                "seconds": round(max(0.0, self._now() - self._at), 1),
                "seq": self._seq,
            }


class InferenceTimer:
    """`ccw_inference_seconds` observations, fed transcript lines in order.

    One inference is one `requestId`: claude flushes it as several
    content-block lines (a `thinking` line, then a `tool_use`/`text` line),
    each carrying the same final `usage.output_tokens`. A group's *start* is
    the timestamp of the line immediately before its first block (the point
    the model started responding to what came before); its *end* is the
    timestamp of its own last block (the point the full response landed).
    Both come from the transcript's own `timestamp` field, not wall-clock
    read time, so the measurement is exact no matter how often the watcher
    polls.

    A group only finalizes once a later line proves it has no more blocks
    coming - so the very last inference in flight when the transcript stops
    growing (match end) is never recorded. That is an inherent limit of
    timing a live tail rather than a finished file, not a bug.

    One timer follows one transcript. `role` is the agent's role, set by the
    watcher before each line; a group is labelled with the role at its first
    block and the `message.model` its lines name.
    """

    def __init__(self, metrics: Metrics | None, role: str = ORCHESTRATOR) -> None:
        self.metrics = metrics
        self.role = role
        self._group_role = role
        self._group_model = ""
        self._prev_ts: float | None = None
        self._group_id: str | None = None
        self._group_start: float | None = None
        self._group_last: float | None = None
        self._group_tokens: int = 0

    def feed(self, lines: Iterable[dict]) -> None:
        for line in lines:
            self._feed_one(line)

    def _feed_one(self, line: dict) -> None:
        ts = _parse_ts(line.get("timestamp"))
        kind = line.get("type")
        rid = line.get("requestId") if kind == "assistant" else None

        if self._group_id is not None and rid != self._group_id:
            self._finish_group()

        if kind == "assistant" and rid:
            message = line.get("message") or {}
            if self._group_id is None:
                self._group_id = rid
                self._group_start = self._prev_ts
                self._group_role = self.role
                self._group_model = str(message.get("model") or "")
            usage = message.get("usage") or {}
            output_tokens = usage.get("output_tokens")
            if isinstance(output_tokens, int):
                self._group_tokens = output_tokens
            if ts is not None:
                self._group_last = ts

        if ts is not None:
            self._prev_ts = ts

    def _finish_group(self) -> None:
        if (
            self.metrics is not None
            and self._group_start is not None
            and self._group_last is not None
        ):
            self.metrics.record_inference(
                self._group_last - self._group_start,
                self._group_tokens,
                role=self._group_role,
                model=self._group_model,
            )
        self._group_id = None
        self._group_start = None
        self._group_last = None
        self._group_tokens = 0


def _short_tool_name(name: str) -> str:
    """The mcp tool's own short name (its server prefix stripped) or, for a
    builtin, the name unchanged - `mcp__mtg__wait_for_priority` ->
    `wait_for_priority`, `Bash` -> `Bash`."""
    if name.startswith(_MTG_PREFIX):
        return name[len(_MTG_PREFIX) :]
    if name.startswith(_SPELLSLINGER_PREFIX):
        return name[len(_SPELLSLINGER_PREFIX) :]
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


def _auto_answered_count(content: Any) -> int:
    """A tool result's `auto_answered` list length, or 0 - the result's own
    text (`_result_text`) parsed as JSON, `0` for anything that is not JSON,
    not an object, or has no such list. Never raises on malformed input."""
    text = _result_text(content)
    if not text:
        return 0
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return 0
    if not isinstance(parsed, dict):
        return 0
    answers = parsed.get("auto_answered")
    return len(answers) if isinstance(answers, list) else 0


class StepTimer:
    """`ccw_model_step_seconds`/`ccw_tool_seconds`/`ccw_auto_answers_total`,
    plus one `tools`-level log line per completed tool call.

    Fed transcript lines in order, same shape as `InferenceTimer`. For every
    `tool_use` block: `model_ms` is the time from the previous
    `user`/`tool_result` line (or this timer's own construction - "turn
    start") to the line carrying the `tool_use` block; `tool_ms` is the time
    from that line to the matching `tool_result` (matched by the block's own
    `id`/`tool_use_id`) - so a step is neither logged nor recorded until its
    result lands. A tool call in flight when the transcript stops growing is
    an inherent gap of timing a live tail, the same one `InferenceTimer`
    documents for its own last group.

    Metrics are fed unconditionally, the log line only at `rank >=
    level_rank("tools")` - the same split `ActivityWatcher` already makes
    between `AGENT_LOG_LEVEL` (text log volume) and what a production
    wrapper wants regardless of it.

    A prompt the agent takes in - a `user` line whose content is a string: a
    turn, a teammate message, a task notification - is a step boundary too:
    the model starts on it, so a helper woken by a message is timed from the
    message, not from its last tool result minutes before.

    One timer follows one transcript, so with the seat pair the current
    player's step is measured from its own tool results, never from the
    future player's reads in between. `role` is set by the watcher before
    each line; a step carries the role it had when its `tool_use` was issued.

    Every field access here is defensive (`.get` plus an `isinstance` guard)
    rather than wrapped in `try`/`except`, matching `observations`'/
    `InferenceTimer`'s own style - a malformed or out-of-order line degrades
    to "nothing observed", never an exception.
    """

    def __init__(
        self,
        metrics: Metrics | None = None,
        rank: int = 0,
        now: Callable[[], float] = time.time,
        role: str = ORCHESTRATOR,
    ) -> None:
        self.metrics = metrics
        self.rank = rank
        self.role = role
        #: The last observed `user`/`tool_result` line's timestamp - "turn
        #: start" until the first one arrives.
        self._boundary_ts: float = now()
        #: tool_use id -> (short name, issued timestamp, model_ms already
        #: computed at issue time, role at issue) - popped once its
        #: `tool_result` lands.
        self._pending: dict[str, tuple[str, float, float, str]] = {}

    def feed(self, lines: Iterable[dict]) -> None:
        for line in lines:
            self._feed_one(line)

    def _feed_one(self, line: dict) -> None:
        kind = line.get("type")
        message = line.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        ts = _parse_ts(line.get("timestamp"))
        if kind == "user" and isinstance(content, str) and ts is not None:
            self._boundary_ts = ts
            return
        if not isinstance(content, list):
            return
        if kind == "assistant":
            self._feed_tool_uses(content, ts)
        elif kind == "user":
            self._feed_tool_results(content, ts)

    def _feed_tool_uses(self, content: list, ts: float | None) -> None:
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id = block.get("id")
            name = block.get("name")
            if not isinstance(tool_id, str) or not tool_id:
                continue
            if not isinstance(name, str) or not name:
                continue
            if ts is None:
                continue
            model_ms = max(0.0, (ts - self._boundary_ts) * 1000.0)
            self._pending[tool_id] = (name, ts, model_ms, self.role)

    def _feed_tool_results(self, content: list, ts: float | None) -> None:
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if ts is not None:
                self._boundary_ts = ts
            tool_id = block.get("tool_use_id")
            if not isinstance(tool_id, str) or not tool_id:
                continue
            pending = self._pending.pop(tool_id, None)
            if pending is None or ts is None:
                continue
            name, issued_ts, model_ms, role = pending
            tool_ms = max(0.0, (ts - issued_ts) * 1000.0)
            auto_answers = _auto_answered_count(block.get("content"))
            self._record(name, model_ms, tool_ms, auto_answers, role)

    def _record(
        self, name: str, model_ms: float, tool_ms: float, auto_answers: int, role: str
    ) -> None:
        short = _short_tool_name(name)
        if self.metrics is not None:
            self.metrics.record_step(
                short, model_ms / 1000.0, tool_ms / 1000.0, auto_answers, role=role
            )
        if self.rank >= level_rank("tools"):
            suffix = f" auto_answers={auto_answers}" if auto_answers else ""
            if role != ORCHESTRATOR:
                suffix += f" role={role}"
            log.info("step tool=%s model_ms=%.0f tool_ms=%.0f%s", short, model_ms, tool_ms, suffix)


def newest_transcript(home_dir: Path) -> Path | None:
    """The most recently modified session file under `~/.claude/projects`."""
    root = home_dir / ".claude" / "projects"
    try:
        candidates = [p for p in root.glob("*/*.jsonl") if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ---- the seat pair: which agent wrote a line, in which role ----------------

#: The key `ActivityWatcher.agent_roles`/`agent_activity` give the main
#: transcript (the orchestrator). A helper's key is its own `agentId`.
MAIN_AGENT = "main"

#: Any agent that is not the orchestrator and not a seat-pair player: the
#: rules researcher, a script-development subagent, a probe's `alpha`.
HELPER = "helper"

_PLAYER_NAME_RE = re.compile(rf"^({ROLE_CURRENT}|{ROLE_FUTURE})(-\d+)?$")
_NOTIFICATION_RE = re.compile(r"<task-notification>(.*?)</task-notification>", re.DOTALL)
_NOTIFICATION_TAG_RE = re.compile(r"<(task-id|status)>\s*([^<]*?)\s*</\1>")
_TERMINATED_RE = re.compile(r'"teammate_terminated"[^}]*?"message"\s*:\s*"([^"\s]+) has shut down')


def role_for_name(name: str | None) -> str:
    """`current-<n>` -> current, `future-<n>` -> future (the names
    `seat_pair.current_name`/`future_name` give the players), anything else
    -> helper."""
    match = _PLAYER_NAME_RE.match(str(name or "").strip())
    return match.group(1) if match else HELPER


def subagents_dir(main: Path) -> Path:
    """Where the session `main` keeps its helpers' transcripts."""
    return main.with_suffix("") / "subagents"


def _agent_id_of(path: Path) -> str:
    return path.name.removesuffix(".jsonl").removeprefix("agent-")


def _read_meta(transcript: Path) -> dict:
    meta_path = transcript.with_name(transcript.name.removesuffix(".jsonl") + ".meta.json")
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _prompt_texts(line: dict) -> list[str]:
    """Prompt text a line gave claude: a `user` string (or its text blocks),
    or a prompt queued mid-turn (`attachment` of type `queued_command`)."""
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
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
    return []


class AgentRoster:
    """Who each helper transcript belongs to, and its role at any moment.

    Built from the orchestrator's own transcript (`feed_main`) and each
    helper's spawn metadata (`add_agent`). A helper's name is in its
    `.meta.json` (teams), or only in the lead's `Agent` call that spawned it
    (subagents: matched by the meta's `toolUseId`, or by the launch result's
    `agentId`). Its role comes from that name (`role_for_name`), and moves:

    - a waiting future player becomes current when the orchestrator stops
      the live current (S5's cycle: it stops the agent that reported
      `committed:`, the server having made the future's token live), or
      when it calls `pass_baton(revoke=true)` (recovery: the current is gone
      and the future gets the live token); it keeps its name `future-<n>`;
    - a current player that another player has since become current over is
      retired and counts as a helper from then on;
    - a player stops being live at a `TaskStop` or `shutdown_request` naming
      it, or a `killed` task notification or `teammate_terminated` for it.

    Times are the transcripts' own timestamps (epoch seconds).
    """

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._spawn_tool_ids: dict[str, str] = {}
        #: Agent `tool_use` id -> (name asked for, timestamp).
        self._spawns: dict[str, tuple[str, float | None]] = {}
        #: `agentId` from an async launch result -> its Agent `tool_use` id.
        self._launches: dict[str, str] = {}
        self._tool_uses: dict[str, str] = {}
        self._revokes: list[float] = []
        self._stops: list[tuple[float, str]] = []

    # -- feeding --

    def add_agent(self, agent_id: str, meta: dict) -> None:
        self._names.setdefault(agent_id, "")
        name = meta.get("name")
        if isinstance(name, str) and name:
            self._names[agent_id] = name
        tool_use_id = meta.get("toolUseId")
        if isinstance(tool_use_id, str) and tool_use_id:
            self._spawn_tool_ids[agent_id] = tool_use_id

    def known(self, agent_id: str) -> bool:
        return agent_id in self._names

    def feed_main(self, lines: Iterable[dict]) -> None:
        for line in lines:
            stamp = _parse_ts(line.get("timestamp"))
            content = (line.get("message") or {}).get("content")
            if line.get("type") == "assistant" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        self._feed_tool_use(block, stamp)
            elif line.get("type") == "user" and isinstance(content, list):
                self._feed_launch(line, content)
            if stamp is not None:
                for text in _prompt_texts(line):
                    self._feed_prompt(text, stamp)

    def _feed_tool_use(self, block: dict, stamp: float | None) -> None:
        name = str(block.get("name") or "")
        tool_id = str(block.get("id") or "")
        tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
        self._tool_uses[tool_id] = name
        if name in _AGENT_TOOLS:
            self._spawns[tool_id] = (str(tool_input.get("name") or ""), stamp)
            return
        if stamp is None:
            return
        if name == "SendMessage":
            ref = str(tool_input.get("to") or tool_input.get("recipient") or "")
            message = tool_input.get("message")
            if isinstance(message, dict) and message.get("type") == "shutdown_request":
                self._stops.append((stamp, ref))
        elif _short_tool_name(name) == "pass_baton" and tool_input.get("revoke") is True:
            self._revokes.append(stamp)
        elif name == "TaskStop":
            ref = str(tool_input.get("task_id") or "")
            if ref:
                self._stops.append((stamp, ref))

    def _feed_launch(self, line: dict, content: list) -> None:
        result = line.get("toolUseResult")
        if not isinstance(result, dict):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = str(block.get("tool_use_id") or "")
            if self._tool_uses.get(tool_id) not in _AGENT_TOOLS:
                continue
            for key in ("agentId", "agent_id"):
                value = result.get(key)
                if isinstance(value, str) and value:
                    self._launches[value] = tool_id

    def _feed_prompt(self, text: str, stamp: float) -> None:
        for body in _NOTIFICATION_RE.findall(text):
            tags = dict(_NOTIFICATION_TAG_RE.findall(body))
            if tags.get("status", "").lower() == "killed" and tags.get("task-id"):
                self._stops.append((stamp, tags["task-id"]))
        for name in _TERMINATED_RE.findall(text):
            self._stops.append((stamp, name))

    # -- reading --

    def agents(self) -> list[str]:
        return list(self._names)

    def name_of(self, agent_id: str) -> str:
        if self._names.get(agent_id):
            return self._names[agent_id]
        tool_id = self._spawn_tool_ids.get(agent_id) or self._launches.get(agent_id) or ""
        return self._spawns.get(tool_id, ("", None))[0]

    def _spawned_at(self, agent_id: str) -> float | None:
        tool_id = self._spawn_tool_ids.get(agent_id) or self._launches.get(agent_id) or ""
        return self._spawns.get(tool_id, ("", None))[1]

    def _refers_to(self, ref: str, agent_id: str) -> bool:
        if not ref:
            return False
        name = self.name_of(agent_id)
        return ref == agent_id or (bool(name) and ref.split("@", 1)[0] == name)

    def _became_current_at(self, agent_id: str) -> float | None:
        """When this agent became the current player, or None if never."""
        return self._current_since().get(agent_id)

    def _current_since(self) -> dict[str, float]:
        """Replay the lead's spawns, stops and messages in time order. A
        `current-<n>` is current from its spawn (from the start of time if the
        spawn is not on record); the oldest waiting `future-<n>` from the stop
        of the live current or from a `pass_baton(revoke=true)`."""
        events: list[tuple[float, int, str, str]] = []
        for agent_id in self._names:
            base = role_for_name(self.name_of(agent_id))
            if base in (ROLE_CURRENT, ROLE_FUTURE):
                spawned = self._spawned_at(agent_id)
                events.append(
                    (spawned if spawned is not None else float("-inf"), 0, base, agent_id)
                )
        events += [(at, 1, "stop", ref) for at, ref in self._stops]
        events += [(at, 1, "revoke", "") for at in self._revokes]
        became: dict[str, float] = {}
        current: str | None = None
        waiting: list[str] = []
        for at, _, kind, who in sorted(events):
            if kind == ROLE_CURRENT:
                became[who], current = at, who
            elif kind == ROLE_FUTURE:
                waiting.append(who)
            elif kind == "stop":
                waiting = [agent for agent in waiting if not self._refers_to(who, agent)]
                if current is not None and self._refers_to(who, current):
                    current = None
                    if waiting:
                        current = waiting.pop(0)
                        became[current] = at
            elif waiting:
                current = waiting.pop(0)
                became[current] = at
        return became

    def stopped_at(self, agent_id: str) -> float | None:
        stops = [at for at, ref in self._stops if self._refers_to(ref, agent_id)]
        return min(stops) if stops else None

    def role_at(self, agent_id: str, at: float | None = None) -> str:
        """The agent's role at `at` (epoch seconds; None means now)."""
        base = role_for_name(self.name_of(agent_id))
        became = self._became_current_at(agent_id)
        moment = float("inf") if at is None else at
        if became is None or moment < became:
            return base
        for other in self._names:
            if other == agent_id:
                continue
            other_became = self._became_current_at(other)
            if other_became is not None and became < other_became <= moment:
                return HELPER
        return ROLE_CURRENT

    def active_current(self) -> str | None:
        """The live current player now, or None."""
        live = [
            agent_id
            for agent_id in self._names
            if self.role_at(agent_id) == ROLE_CURRENT and self.stopped_at(agent_id) is None
        ]
        if not live:
            return None
        return max(live, key=lambda agent_id: self._became_current_at(agent_id) or float("-inf"))


def _load_roster(main: Path) -> tuple[AgentRoster, list[Path]]:
    roster = AgentRoster()
    roster.feed_main(TranscriptTail(main).read())
    try:
        helpers = sorted(subagents_dir(main).glob("agent-*.jsonl"))
    except OSError:
        helpers = []
    for path in helpers:
        roster.add_agent(_agent_id_of(path), _read_meta(path))
    return roster, helpers


def agent_turn_tokens(main: Path, since: float, until: float) -> list[dict[str, Any]]:
    """Token totals of every helper of the session `main` for the lines
    stamped in `(since, until]`, one entry per (role, model) in the shape of
    the Stop hook's `turnTokens` plus `role`.

    The Stop hook only reads the orchestrator's transcript; this is the rest
    of the turn's usage (the seat pair's Opus players, the researcher). Each
    API message is counted once, at its last line: claude writes one line per
    content block of a response, each repeating the response's usage (the
    output count can still grow between them), and the role is the one the
    agent held at that line."""
    roster, helpers = _load_roster(Path(main))
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for path in helpers:
        agent_id = _agent_id_of(path)
        final: dict[str, tuple[float, str, dict]] = {}
        for line in TranscriptTail(path).read():
            if line.get("type") != "assistant":
                continue
            message = line.get("message") or {}
            usage = message.get("usage")
            stamp = _parse_ts(line.get("timestamp"))
            if not isinstance(usage, dict) or stamp is None or not since < stamp <= until:
                continue
            key = str(message.get("id") or line.get("requestId") or line.get("uuid") or id(line))
            # The last line of a message carries its final usage.
            final[key] = (stamp, str(message.get("model") or ""), usage)
        for stamp, model, usage in final.values():
            role = roster.role_at(agent_id, stamp)
            entry = totals.setdefault(
                (role, model),
                {
                    "model": model,
                    "input": 0,
                    "output": 0,
                    "cache_read": 0,
                    "cache_creation": 0,
                    "role": role,
                },
            )
            entry["input"] += int(usage.get("input_tokens") or 0)
            entry["output"] += int(usage.get("output_tokens") or 0)
            entry["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
            entry["cache_creation"] += int(usage.get("cache_creation_input_tokens") or 0)
    return [totals[key] for key in sorted(totals)]


class _Track:
    """One transcript followed live: its tail, its activity, its timers."""

    def __init__(self, path: Path, metrics: Metrics | None, rank: int, helper: bool) -> None:
        self.tail = TranscriptTail(path)
        self.activity = CurrentActivity(include_sidechain=helper)
        self.inference = InferenceTimer(metrics)
        self.steps = StepTimer(metrics, rank=rank)

    def feed(self, lines: list[dict], role_at: Callable[[float | None], str]) -> None:
        for line in lines:
            role = role_at(_parse_ts(line.get("timestamp")))
            self.inference.role = role
            self.steps.role = role
            self.inference.feed((line,))
            self.steps.feed((line,))
            self.activity.feed((line,))


class ActivityWatcher:
    """A daemon thread that turns the live transcripts into log lines.

    Follows the newest session transcript (the orchestrator) and every
    helper transcript of that session (see the module docstring).

    Read-only and entirely out of the turn's path: if it dies, the game is
    unaffected and only the log goes quiet.
    """

    def __init__(
        self,
        home_dir: Path,
        level: str = "tools",
        poll: float = POLL_SECONDS,
        metrics: Metrics | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.home_dir = Path(home_dir)
        self.rank = level_rank(level)
        self.poll = poll
        self.metrics = metrics
        self._now = now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        #: The orchestrator's transcript. Its timers feed
        #: `ccw_inference_seconds` and the step metrics unconditionally, not
        #: gated on `rank` - a production wrapper turned down to `off`/`turns`
        #: for log volume still wants latency data - and its activity feeds
        #: the AI pill for the same reason: `AGENT_LOG_LEVEL` is a choice
        #: about this container's log volume, not a decision to leave the
        #: human opposite this seat staring at one static word.
        self._main: _Track | None = None
        self._helpers: dict[str, _Track] = {}
        self._roster = AgentRoster()
        #: The newest line any agent of the session wrote (`last_progress`).
        self._last_line_at: float | None = None

    def enabled(self) -> bool:
        return self.rank > level_rank("turns")

    def activity_snapshot(self) -> dict[str, Any] | None:
        """What the seat is doing right now, for `Manager.snapshot` to
        publish - `None` until the transcript shows something.

        The live current player's latest activity when there is one and it
        has been seen doing something, otherwise the orchestrator's: a future
        player's activity never reaches the pill. `role` names whose it is
        and `label` prefixes the word with it; `state`, `seconds` and `seq`
        keep their meaning (`CurrentActivity.snapshot`)."""
        with self._lock:
            current = self._roster.active_current()
            track = self._helpers.get(current or "")
            main = self._main
        if track is not None:
            snapshot = track.activity.snapshot()
            if snapshot is not None:
                return _with_role(snapshot, ROLE_CURRENT)
        if main is None:
            return None
        snapshot = main.activity.snapshot()
        return _with_role(snapshot, ORCHESTRATOR) if snapshot is not None else None

    def agent_roles(self) -> dict[str, str]:
        """Every followed transcript (`MAIN_AGENT` or a helper's `agentId`)
        and its role now."""
        with self._lock:
            roles = {MAIN_AGENT: ORCHESTRATOR} if self._main is not None else {}
            roles.update({agent_id: self._roster.role_at(agent_id) for agent_id in self._helpers})
            return roles

    def agent_activity(self, agent_id: str) -> dict[str, Any] | None:
        """One agent's own latest activity (`CurrentActivity.snapshot`)."""
        with self._lock:
            track = self._main if agent_id == MAIN_AGENT else self._helpers.get(agent_id)
        return track.activity.snapshot() if track is not None else None

    def last_progress(self) -> float | None:
        """The newest line any agent of the session wrote, as epoch seconds
        (its own timestamp, or the time it was read when it has none) - the
        stall timer's measure of progress (`Manager.progress_source`)."""
        with self._lock:
            return self._last_line_at

    def start(self) -> None:
        # No longer gated on `enabled()`: the watcher's job is not just text
        # logging any more (which `rank` still controls, inside `poll_once`)
        # but also feeding `ccw_inference_seconds`, which every level wants.
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="agent-activity", daemon=True)
        self._thread.start()
        log.info("watching the agent's transcript under %s", self.home_dir / ".claude" / "projects")

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> int:
        """Follow the newest transcript and its helpers' transcripts, log
        whatever is new, feed the metrics, and update what each agent is
        currently doing. Returns the number of events logged - the unit of
        the tests."""
        newest = newest_transcript(self.home_dir)
        if newest is None:
            return 0
        with self._lock:
            if self._main is None or self._main.tail.path != newest:
                # From byte 0. The agent container gets a fresh HOME per match
                # (one container, one match - see `wrapper/__main__.py`), so
                # the only file here is this session's, and its opening turn
                # is worth logging rather than skipping.
                self._main = _Track(newest, self.metrics, self.rank, helper=False)
                self._helpers = {}
                self._roster = AgentRoster()
                self._last_line_at = None
            main, roster = self._main, self._roster
        lines = main.tail.read()
        with self._lock:
            roster.feed_main(lines)
        main.feed(lines, lambda _at: ORCHESTRATOR)
        self._note_progress(lines)
        count = log_lines(lines, self.rank)
        count += self._poll_helpers(newest, roster)
        return count

    def _poll_helpers(self, main: Path, roster: AgentRoster) -> int:
        try:
            paths = sorted(subagents_dir(main).glob("agent-*.jsonl"))
        except OSError:
            return 0
        count = 0
        for path in paths:
            agent_id = _agent_id_of(path)
            with self._lock:
                # The meta is re-read until it names the agent: it can land
                # after the transcript's first line.
                if not roster.known(agent_id) or not roster.name_of(agent_id):
                    roster.add_agent(agent_id, _read_meta(path))
                track = self._helpers.get(agent_id)
                if track is None:
                    track = _Track(path, self.metrics, self.rank, helper=True)
                    self._helpers[agent_id] = track
            lines = track.tail.read()
            if not lines:
                continue

            def role_at(at: float | None, agent_id: str = agent_id) -> str:
                with self._lock:
                    return roster.role_at(agent_id, at)

            track.feed(lines, role_at)
            self._note_progress(lines)
            who = f"{role_at(None)} {roster.name_of(agent_id) or agent_id}"
            count += log_lines(lines, self.rank, who=who)
        return count

    def _note_progress(self, lines: list[dict]) -> None:
        if not lines:
            return
        stamps = [_parse_ts(line.get("timestamp")) for line in lines]
        newest = max((at for at in stamps if at is not None), default=None)
        if newest is None:
            newest = self._now()
        with self._lock:
            if self._last_line_at is None or newest > self._last_line_at:
                self._last_line_at = newest

    def _run(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - logging never breaks a turn
                log.debug("activity watcher hiccup: %s", exc)


def _with_role(snapshot: dict[str, Any], role: str) -> dict[str, Any]:
    return {**snapshot, "role": role, "label": f"{role}: {snapshot['state']}"}
