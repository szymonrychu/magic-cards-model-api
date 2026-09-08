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

Nothing here can fail a turn: the thread catches everything and keeps going.
Logging is not worth breaking a game over.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from .metrics import Metrics

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
#: and a wrapper shared secret, the agent's own Bash commands can echo an
#: environment, and the container also carries a GH_PAT (deploy/seat-job.yaml,
#: used to clone the deck-guides repo) that could end up embedded in a
#: command the agent runs; none of that goes to stdout. Seat uids are NOT
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


def log_lines(lines: Iterable[dict], rank: int) -> int:
    count = 0
    for line in lines:
        for kind, text in events(line):
            emit(kind, text, rank)
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
    """

    def __init__(self, metrics: Metrics | None) -> None:
        self.metrics = metrics
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
            if self._group_id is None:
                self._group_id = rid
                self._group_start = self._prev_ts
            message = line.get("message") or {}
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
            self.metrics.record_inference(self._group_last - self._group_start, self._group_tokens)
        self._group_id = None
        self._group_start = None
        self._group_last = None
        self._group_tokens = 0


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


class ActivityWatcher:
    """A daemon thread that turns the live transcript into log lines.

    Read-only and entirely out of the turn's path: if it dies, the game is
    unaffected and only the log goes quiet.
    """

    def __init__(
        self,
        home_dir: Path,
        level: str = "tools",
        poll: float = POLL_SECONDS,
        metrics: Metrics | None = None,
    ) -> None:
        self.home_dir = Path(home_dir)
        self.rank = level_rank(level)
        self.poll = poll
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tail: TranscriptTail | None = None
        #: `ccw_inference_seconds`. Fed unconditionally in `poll_once`, not
        #: gated on `rank` - a production wrapper turned down to `off`/`turns`
        #: for log volume still wants latency data.
        self._timer = InferenceTimer(metrics)

    def enabled(self) -> bool:
        return self.rank > level_rank("turns")

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
        """Follow the newest transcript, log whatever is new, and feed
        `ccw_inference_seconds`. Returns the number of events logged - the
        unit of the tests."""
        newest = newest_transcript(self.home_dir)
        if newest is None:
            return 0
        if self._tail is None or self._tail.path != newest:
            # From byte 0. The agent container gets a fresh HOME per match
            # (one container, one match - see `wrapper/__main__.py`), so the
            # only file here is this session's, and its opening turn is worth
            # logging rather than skipping.
            self._tail = TranscriptTail(newest)
        lines = self._tail.read()
        self._timer.feed(lines)
        return log_lines(lines, self.rank)

    def _run(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - logging never breaks a turn
                log.debug("activity watcher hiccup: %s", exc)
