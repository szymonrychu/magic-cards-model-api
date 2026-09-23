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


def observations(line: dict) -> Iterator[tuple[str, str]]:
    """`(kind, tool name)` for one transcript object - the activity-facing
    counterpart of `events` above.

    Deliberately a separate parser rather than a filter over `events`: this
    one never touches a tool's input or a text block's body, so no content
    can reach the published state even by accident. `thinking` blocks, which
    `events` skips because they carry no readable text, are exactly what
    this one wants: their presence is the evidence.
    """
    if line.get("type") != "assistant":
        return
    message = line.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
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
            yield "tool", str(block.get("name") or "")


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

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._state: str | None = None
        self._at = 0.0
        self._seq = 0

    def feed(self, lines: Iterable[dict]) -> None:
        for line in lines:
            self._feed_one(line)

    def _feed_one(self, line: dict) -> None:
        stamped = _parse_ts(line.get("timestamp"))
        for kind, name in observations(line):
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
        #: What the play UI's AI pill says. Fed unconditionally for the same
        #: reason: `AGENT_LOG_LEVEL` is a choice about this container's log
        #: volume, not a decision to leave the human opposite this seat
        #: staring at one static word for the whole match.
        self._activity = CurrentActivity()

    def enabled(self) -> bool:
        return self.rank > level_rank("turns")

    def activity_snapshot(self) -> dict[str, Any] | None:
        """What the agent is doing right now, for `Manager.snapshot` to
        publish - `None` until the transcript shows something."""
        return self._activity.snapshot()

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
        """Follow the newest transcript, log whatever is new, feed
        `ccw_inference_seconds`, and update what the agent is currently
        doing. Returns the number of events logged - the unit of the tests."""
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
        self._activity.feed(lines)
        return log_lines(lines, self.rank)

    def _run(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - logging never breaks a turn
                log.debug("activity watcher hiccup: %s", exc)
