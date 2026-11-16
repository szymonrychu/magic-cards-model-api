"""Async Claude-seat sim matches: two kubernetes AI seat Jobs play one game, polled by id.

The game is booted in this process (`decks_api.boot_decklist_game`), both seats AI, owner
`SIM_OWNER`, outside the lobby (no slot, not listed, not reachable to an OIDC user). `start`
answers at once: the boot (card resolution may be cold) and the two seat Job creates run in a
background thread, so the tool call stays under the call cap; `status` says `starting` until
then and `failed` if they fail. The lobby's pregame backstop (`ai_pregame`) watches the game, and
a match whose seats are both gone or in error fails on the next reaper pass instead of holding
the slot. A match is bounded by `claude_deadline_s` (the seat Job deadline), not by the
engine-only batch cap. On game over, at the deadline or on failure the seats are stopped and the
game is deleted; the result is kept `result_ttl_s` in memory and `log_retention_s` as a row. A
start that has not finished `START_DEADLINE_S` after the call fails and frees the slot; a start
thread that finishes later cleans up what it created. `cancel` stops a live match the same way
(status `cancelled`) and keeps its log.

Every terminal transition (done, deadline, failed, cancelled) goes through `_finish`: one
status-guarded claim under the match lock, the `ended` event, one count on
`mtg_sim_claude_matches_total{status}` and one cleanup, so a racing cancel and reaper pass
stop the seats exactly once.

`status` answers from cached match state only: no cluster read, no lock held across I/O, so a
poll stays under MTG_SIM_CALL_TIMEOUT_S whatever the cluster does. The reaper (every 30 s) reads
the seats and the game and applies the transitions; it claims a transition under the match lock
and runs the cleanup I/O after releasing it.

Match rows are written by one background writer thread that coalesces the newest snapshot per
match and retries a failed write on its next pass; no tool call or reaper pass waits on the
database to write (reads stay direct).

Seat uids are credentials: no answer carries one (or the Job names that embed their prefix) and
nothing here logs one.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, get_args
from uuid import UUID

from magic_cards_model.models import Game
from prometheus_client import Counter, Gauge

from magic_cards_model_api import ai_seat
from magic_cards_model_api.agent_settings import Effort
from magic_cards_model_api.match_metrics import REGISTRY
from magic_cards_model_api.sim import gamelog
from magic_cards_model_api.sim.config import SimLimits
from magic_cards_model_api.sim.gamelog import TAIL_LINES, LogFilter, LogLine
from magic_cards_model_api.sim.manager import SimRefused
from magic_cards_model_api.sim.match import checked_support, resolve_deck
from magic_cards_model_api.sim.redact import redact, support_unknown

logger = logging.getLogger(__name__)

SIM_OWNER = "sim:claude"
EXPECTED_MINUTES = "33-55 per game"
POLL_EVERY_S = 300
_LAST_LOG = 10
_SEATS = ("a", "b")
_PREFIX_LEN = 8  # k8s Job names carry `uid[:8]` (`k8s_seats.job_name`)
#: `K8sSeats.status` states in which nothing plays the seat any more.
_DEAD_STATES = frozenset({"gone", "error"})
#: Statuses that hold the match slot; every other status is in `TERMINAL`.
_LIVE = ("starting", "running", "stalled")
TERMINAL = frozenset({"done", "deadline", "failed", "cancelled"})
_REASON_MAX = 200
RESTART_ERROR = "the server restarted during the match; the log so far is kept"
#: The retention sweep of persisted rows runs at most this often (monotonic seconds).
_RETENTION_EVERY_S = 3600
_LIST_MAX = 50
LOG_LIMIT_MAX = 500
_EFFORTS = get_args(Effort)
#: A start (boot with cold card resolution, two seat Job creates) that has not finished after
#: this long is failed by the reaper and its slot freed; a healthy start takes seconds.
START_DEADLINE_S = 300.0

MATCHES = Counter(
    "mtg_sim_claude_matches",
    "Claude-seat sim matches by final status",
    ["status"],
    registry=REGISTRY,
)
STALL_SECONDS = Gauge(
    "mtg_sim_claude_stall_seconds",
    "Seconds without a new record of the longest-stalled live Claude match (0 when none)",
    registry=REGISTRY,
)


_EPOCH = datetime.min.replace(tzinfo=UTC)


def _count(status: str) -> None:
    MATCHES.labels(status=status).inc()


@dataclass
class _ClaudeMatch:
    match_id: str
    decks: dict[str, str]  # seat -> deck label
    seed: int
    started: float
    seat_settings: dict[str, tuple[str, str]]  # seat -> (model, effort)
    game_id: str | None = None  # set once the background boot has booted the game
    uids: dict[str, str] = field(default_factory=dict)  # seat -> uid; a credential, never answered
    status: str = "starting"
    seat_states: dict[str, str] = field(default_factory=dict)
    checked: float | None = None  # clock time of the reaper's last seat and game read
    booting: threading.Thread | None = None
    turn: int | None = None
    step: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    finished: float | None = None
    started_wall: datetime | None = None  # wall time of `start`, the row's started_at
    finished_wall: datetime | None = None  # stamped when the match first becomes terminal
    support: dict[str, Any] = field(default_factory=lambda: support_unknown("pending"))
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_seq: int | None = None  # newest `records.seq` the reaper saw
    last_progress: float | None = None  # clock time of the last new record (or of `running`)
    stalled_reason: str | None = None
    ended: threading.Event = field(default_factory=threading.Event)  # set on the terminal claim

    def secrets(self) -> list[str]:
        return [s for uid in self.uids.values() for s in (uid, uid[:_PREFIX_LEN])]


class ClaudeMatchManager:
    def __init__(
        self,
        limits: SimLimits,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._wall = wall
        # Row writes go through one background writer (see `_persist`): callers never
        # block on the database.
        self._wlock = threading.Lock()
        self._wake = threading.Condition(self._wlock)  # new snapshot, or shutdown
        self._idle = threading.Condition(self._wlock)  # writer drained (flush_persist)
        self._pending: dict[str, dict[str, Any]] = {}  # newest unwritten snapshot per match
        self._writing = False
        self._writer: threading.Thread | None = None
        self._closing = False
        self._persist_retry_s = 5.0
        self._last_retention: float | None = None
        self._lock = threading.Lock()
        self._matches: dict[str, _ClaudeMatch] = {}

    def live_ids(self) -> list[str]:
        """Ids of the match runs currently held (the test suite's leak guard reads this)."""
        with self._lock:
            return list(self._matches)

    def start(
        self,
        deck_a: str,
        deck_b: str,
        seed: int | None,
        model: str | None = None,
        effort: str | None = None,
        seat_a: dict[str, Any] | None = None,
        seat_b: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mode = ai_seat.ai_seat_mode()
        if mode != "kubernetes":
            raise SimRefused(
                "Claude-seat matches need AI_SEAT_MODE=kubernetes (each seat runs as its own"
                f" Job); this deployment runs AI seats in {mode!r} mode."
            )
        try:
            label_a, text_a = resolve_deck(deck_a)
            label_b, text_b = resolve_deck(deck_b)
        except ValueError as exc:
            raise SimRefused(str(exc)) from exc
        settings = self._seat_settings(model, effort, {"a": seat_a, "b": seat_b})
        if seed is None:
            seed = random.SystemRandom().randrange(2**31)
        match = _ClaudeMatch(
            f"claude-{uuid.uuid4().hex}",
            {"a": label_a, "b": label_b},
            seed,
            self._clock(),
            settings,
        )
        with self._lock:
            active = [m for m in self._matches.values() if m.status in _LIVE]
            if len(active) >= self._limits.max_claude_matches:
                oldest = min(active, key=lambda m: m.started)
                minutes = int((self._clock() - oldest.started) // 60)
                verb = "running" if oldest.status == "running" else "being started"
                raise SimRefused(
                    f"A Claude match is already {verb} ({minutes} min elapsed; it is bounded"
                    f" by the {self._limits.claude_deadline_s:g} s seat deadline). Only one"
                    " runs at a time: poll it or retry later."
                )
            self._matches[match.match_id] = match
        match.started_wall = self._wall()
        self._persist(match)
        match.booting = threading.Thread(
            target=self._start_in_background,
            args=(match, text_a, text_b),
            daemon=True,
            name=f"{match.match_id}-start",
        )
        match.booting.start()
        return {
            "match_id": match.match_id,
            "status": "starting",
            "expected_minutes": EXPECTED_MINUTES,
            "poll_every_s": POLL_EVERY_S,
            "seed": seed,
            "decks": dict(match.decks),
            "seats": _seat_echo(settings),
        }

    def _seat_settings(
        self, model: str | None, effort: str | None, seats: dict[str, dict[str, Any] | None]
    ) -> dict[str, tuple[str, str]]:
        """Each seat's (model, effort): the seat override, else the call's, else the env default.

        Refuses a model outside `MTG_SIM_CLAUDE_MODELS` and an effort outside `Effort`, naming
        the allowed values.
        """
        lim = self._limits
        call_model, call_effort = model or lim.claude_model, effort or lim.claude_effort
        out: dict[str, tuple[str, str]] = {}
        for seat, override in seats.items():
            where = f"seat_{seat}"
            unknown = set(override or {}) - {"model", "effort"}
            if unknown:
                raise SimRefused(
                    f"{where}: unknown field(s) {sorted(unknown)}; only model and effort"
                    " are allowed."
                )
            seat_model = (override or {}).get("model") or call_model
            seat_effort = (override or {}).get("effort") or call_effort
            if seat_model not in lim.claude_models:
                raise SimRefused(
                    f"{where}: model {seat_model!r} is not allowed; use one of"
                    f" {', '.join(lim.claude_models)}."
                )
            if seat_effort not in _EFFORTS:
                raise SimRefused(
                    f"{where}: effort {seat_effort!r} is not allowed; use one of"
                    f" {', '.join(_EFFORTS)}."
                )
            out[seat] = (seat_model, seat_effort)
        return out

    def _start_in_background(self, match: _ClaudeMatch, text_a: str, text_b: str) -> None:
        """Boot, start the pregame backstop and both seats; on any failure fail the match.

        The match holds its slot while this runs (status `starting`). A failure frees it:
        whatever was created (the game, a seat already started, the backstop) is cleaned up
        and the reason is a fixed text or an exception type, never a message that may name a
        Job or echo a cluster error.
        """
        try:
            error = self._boot(match, (text_a, text_b))
        except BaseException as exc:  # noqa: BLE001 - a crashed start must free the slot
            logger.warning("claude sim match start failed (%s)", type(exc).__name__)
            error = f"The match could not be started ({type(exc).__name__}). Try again."
        if error is None:
            with match.lock:
                promoted = match.status == "starting"
                if promoted:
                    match.status = "running"
                    match.last_progress = self._clock()
            if promoted:
                self._persist(match)
                return
        elif self._finish(match, "failed", error, expect=("starting",)):
            return
        # Already failed by the start deadline or cancelled while this thread ran: clean up
        # everything it created (it may have created more after that cleanup).
        self._cleanup(match)

    def _boot(self, match: _ClaudeMatch, texts: tuple[str, str]) -> str | None:
        """The slow half of `start`. Returns `None` once both seats run, else the reason."""
        from magic_cards_model_api import ai_pregame
        from magic_cards_model_api.agent_settings import AgentSettings
        from magic_cards_model_api.decks_api import boot_decklist_game

        threading.Thread(
            target=self._compute_support,
            args=(match, *texts),
            daemon=True,
            name=f"{match.match_id}-support",
        ).start()
        base = AgentSettings.default(os.environ)
        agents = {
            seat: base.model_copy(update={"model": model, "effort": effort})
            for seat, (model, effort) in match.seat_settings.items()
        }
        try:
            game_id, by_name, _ = boot_decklist_game(
                [(_SEATS[0], texts[0], True), (_SEATS[1], texts[1], True)],
                match.seed,
                SIM_OWNER,
                agents,
            )
        except ValueError as exc:  # an illegal list: the message names the cards
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("claude sim match boot failed (%s)", type(exc).__name__)
            return f"The match could not be booted ({type(exc).__name__}). Try again."
        with match.lock:
            match.game_id, match.uids = game_id, by_name
            if match.status != "starting":
                return "abandoned"  # the start deadline passed; the caller cleans up
        # The bot seats' pregame backstop, as the lobby route starts it: a seat that exits
        # after its keep would otherwise leave the game in PREGAME_ACTIONS until the deadline.
        ai_pregame.start(game_id, list(_SEATS))
        api_base = ai_seat.api_service_url()
        for seat in _SEATS:
            if match.status != "starting":
                return "abandoned"
            try:
                started = ai_seat.registry.start(
                    uid=by_name[seat],
                    game_id=game_id,
                    player=seat,
                    api_base=api_base,
                    agent=agents[seat],
                )
            except Exception as exc:  # noqa: BLE001 - registry.start should never raise
                logger.warning("claude sim seat start raised (%s)", type(exc).__name__)
                started = {}
            if started.get("mechanism") != "kubernetes":
                # The fallback's reason may name the Job or echo a cluster error: not answered.
                return (
                    f"Seat {seat} could not be scheduled as a kubernetes Job, so nothing is"
                    " running. Retry later."
                )
        return None

    @staticmethod
    def _compute_support(match: _ClaudeMatch, text_a: str, text_b: str) -> None:
        match.support = checked_support(text_a, text_b)

    def _find(self, match_id: str) -> _ClaudeMatch | None:
        with self._lock:
            return self._matches.get(match_id)

    def _unknown(self, match_id: str) -> SimRefused:
        return SimRefused(
            f"No Claude match {match_id}; finished matches are kept"
            f" {self._limits.log_retention_s:g} s. Start one with sim_match_claude_start, or"
            " list recent ones with sim_match_claude_list."
        )

    def status(self, match_id: str) -> dict[str, Any]:
        """The match as the reaper last saw it; no cluster I/O, no lock held across I/O.
        A match this process no longer holds (result TTL, restart) answers from its row."""
        match = self._find(match_id)
        if match is not None:
            return self._answer(match)
        row = self._row(match_id)
        if row is None:
            raise self._unknown(match_id)
        return self._answer_from_row(row)

    def cancel(self, match_id: str, reason: str | None = None) -> dict[str, Any]:
        """Stop a starting, running or stalled match: seats stopped, game deleted, slot freed,
        log kept. A terminal match answers its final status unchanged, and so does a match
        known only from its persisted row (nothing in this process to stop)."""
        match = self._find(match_id)
        if match is None:
            row = self._row(match_id)
            if row is None:
                raise self._unknown(match_id)
            lines = self._log_lines_for(row.get("game_id"), [], LogFilter())
            out = self._answer_from_row(row)
            return {**out, "log_tail": gamelog.render(lines[-TAIL_LINES:])}
        error = "cancelled by caller"
        text = " ".join(gamelog.ascii_text(reason or "").split())[:_REASON_MAX]
        if text:
            error = f"{error}: {text}"
        self._finish(match, "cancelled", error)
        with match.lock:
            game_id = match.game_id
        lines = self._log_lines_for(game_id, match.secrets(), LogFilter())
        return {**self._answer(match), "log_tail": gamelog.render(lines[-TAIL_LINES:])}

    def _game_ref(self, match_id: str) -> tuple[_ClaudeMatch | None, dict[str, Any] | None]:
        """The in-memory match, else its persisted row; refused like `status` when neither."""
        match = self._find(match_id)
        if match is not None:
            return match, None
        row = self._row(match_id)
        if row is None:
            raise self._unknown(match_id)
        return None, row

    def log(
        self,
        match_id: str,
        since_seq: int = 0,
        limit: int = 200,
        turn: int | str | None = None,
        seat: str | None = None,
        kinds: str = "plays",
        reasoning: bool = True,
        board: str = "changes",
    ) -> dict[str, Any]:
        """One page of the compact-v1 log of a match, live or finished (see `gamelog`).

        Pass `next_seq` back as `since_seq` while `more`. A match with no game (the start
        failed before boot) answers an empty log with a note, never an error."""
        limit = max(1, min(int(limit), LOG_LIMIT_MAX))
        since_seq = max(0, int(since_seq))
        try:
            flt = gamelog.parse_filter(turn, seat, kinds, reasoning, board, limit)
        except ValueError as exc:
            raise SimRefused(str(exc)) from exc
        match, row = self._game_ref(match_id)
        if match is not None:
            with match.lock:
                game_id, status, step = match.game_id, match.status, match.step
                cur_turn, stalled_reason = match.turn, match.stalled_reason
            secrets = match.secrets()
        else:
            assert row is not None
            game_id, status, step = row.get("game_id"), row["status"], None
            cur_turn, stalled_reason = None, row.get("stalled_reason")
            secrets = []  # the seat uids are gone with the process
        out: dict[str, Any] = {
            "match_id": match_id,
            "status": status,
            "format": gamelog.FORMAT,
            "lines": "",
            "first_seq": None,
            "last_seq": None,
            "next_seq": since_seq,
            "more": False,
            "total_rows": 0,
            "turn": cur_turn,
            "step": step,
        }
        if game_id is None:
            out["note"] = "the match has no game log (it failed before the game was booted)"
            return out
        lines = self._log_lines_for(game_id, secrets, flt, since_seq)
        kept, more = gamelog.page(lines, limit, gamelog.LOG_ANSWER_MAX_CHARS)
        text = gamelog.render(kept)
        actions = [line for line in kept if not line.header]
        if kept:
            out["first_seq"] = kept[0].seq
            out["last_seq"] = kept[-1].end_seq or kept[-1].seq
        if actions:
            out["next_seq"] = actions[-1].end_seq or actions[-1].seq
        out["more"] = more
        total = self._max_seq(game_id)
        out["total_rows"] = total or 0
        if status == "stalled" and stalled_reason:
            stall = gamelog.ascii_text(f"-- stalled: {stalled_reason}")
            for secret in secrets:
                stall = stall.replace(secret, "<redacted>")
            text = f"{text}\n{stall}" if text else stall
        out["lines"] = text  # scrubbed per line above; redact() would blank the whole log
        return out

    def list(self, limit: int = 10) -> dict[str, Any]:
        """Recent matches, newest first: persisted rows, with a match held in memory
        reporting its live status. A store failure leaves the in-memory matches."""
        limit = max(1, min(_LIST_MAX, int(limit)))
        entries: dict[str, dict[str, Any]] = {}
        try:
            from magic_cards_model_api.store import store

            rows = store.list_sim_claude_matches(limit)
        except Exception as exc:  # noqa: BLE001 - the list stands without the table
            logger.warning("claude sim list failed (%s)", type(exc).__name__)
            rows = []
        for row in rows:
            result = row.get("result") or {}
            entries[row["match_id"]] = {
                "match_id": row["match_id"],
                "status": row["status"],
                "decks": row["decks"],
                "seats": row["seats"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "turns": result.get("turns"),
                "winner": result.get("winner"),
                "error": row["error"],
            }
        with self._lock:
            held = list(self._matches.values())
        for match in held:
            with match.lock:
                result = match.result or {}
                entries[match.match_id] = {
                    "match_id": match.match_id,
                    "status": match.status,
                    "decks": dict(match.decks),
                    "seats": _seat_echo(match.seat_settings),
                    "started_at": match.started_wall,
                    "finished_at": match.finished_wall,
                    "turns": result.get("turns"),
                    "winner": result.get("winner"),
                    "error": match.error,
                }
        newest = sorted(entries.values(), key=lambda e: e["started_at"] or _EPOCH, reverse=True)
        for entry in newest:
            for key in ("started_at", "finished_at"):
                if entry[key] is not None:
                    entry[key] = entry[key].isoformat()
        return {"matches": newest[:limit]}

    def _row(self, match_id: str) -> dict[str, Any] | None:
        from magic_cards_model_api.store import store

        try:
            return store.get_sim_claude_match(match_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable row is an unknown match
            logger.warning("claude sim row read failed (%s)", type(exc).__name__)
            return None

    def _persist(self, match: _ClaudeMatch) -> None:
        """Queue the match header row for the background writer (write-through, eventually).
        Never blocks on the store and never raises: the match runs from memory when the store
        is down. The snapshot is taken here under the match lock (no I/O)."""
        # The snapshot is taken while holding the queue lock (order: _wlock, then match.lock;
        # nothing takes them the other way round), so snapshot order is enqueue order.
        with self._wlock:
            with match.lock:
                row = {
                    "match_id": match.match_id,
                    "game_id": match.game_id,
                    "decks": dict(match.decks),
                    "seed": match.seed,
                    "seats": _seat_echo(match.seat_settings),
                    "status": match.status,
                    "error": match.error,
                    "result": match.result,
                    "started_at": match.started_wall,
                    "finished_at": match.finished_wall,
                    "last_seq": match.last_seq,
                    "last_progress_at": (
                        None
                        if match.last_progress is None
                        else self._wall() - timedelta(seconds=self._clock() - match.last_progress)
                    ),
                    "stalled_reason": match.stalled_reason,
                }
            self._enqueue(row)

    def _enqueue(self, row: dict[str, Any]) -> None:
        """Queue one snapshot (call with `_wlock` held): the newest wins, but a terminal
        snapshot is never replaced by a live one."""
        queued = self._pending.get(row["match_id"])
        if queued is None or queued["status"] not in TERMINAL or row["status"] in TERMINAL:
            self._pending[row["match_id"]] = row
        self._closing = False
        if self._writer is None:
            self._writer = threading.Thread(
                target=self._write_loop, daemon=True, name="claude-sim-persist"
            )
            self._writer.start()
        self._wake.notify_all()

    def _write_loop(self) -> None:
        """The one writer of match rows: drains the pending snapshots outside every manager
        and match lock. A failed write keeps its snapshot (unless a newer one arrived) and is
        retried on the next pass; the thread exits when nothing is pending."""
        from magic_cards_model_api.store import store

        while True:
            with self._wlock:
                if not self._pending or self._closing:
                    self._writer = None
                    self._idle.notify_all()
                    return
                batch, self._pending = self._pending, {}
                self._writing = True
            failed: dict[str, dict[str, Any]] = {}
            for match_id, row in batch.items():
                try:
                    store.put_sim_claude_match(row)
                except Exception as exc:  # noqa: BLE001 - persistence must never stop a match
                    logger.warning("claude sim match persist failed (%s)", type(exc).__name__)
                    failed[match_id] = row
            with self._wlock:
                self._writing = False
                for match_id, row in failed.items():
                    self._pending.setdefault(match_id, row)  # keep, unless superseded
                if not self._pending:
                    self._writer = None
                    self._idle.notify_all()
                    return
                self._idle.notify_all()
                if failed and not self._closing:
                    self._wake.wait(self._persist_retry_s)

    def flush_persist(self, timeout: float = 5.0) -> bool:
        """Wait until every queued row is written; True when drained, False on timeout."""
        deadline = time.monotonic() + timeout
        with self._wlock:
            while self._pending or self._writing:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._idle.wait(left)
        return True

    def shutdown(self, timeout: float = 5.0) -> None:
        """Flush the queued rows (bounded) and stop the writer; a later persist restarts it."""
        self.flush_persist(timeout)
        with self._wlock:
            self._closing = True
            self._pending.clear()
            writer = self._writer
            self._wake.notify_all()
        if writer is not None:
            writer.join(timeout=timeout)

    def _answer_from_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """`_answer` for a match this process does not hold: the persisted header."""
        status = row["status"]
        started, finished = row["started_at"], row["finished_at"]
        end = finished if finished is not None else self._wall()
        out: dict[str, Any] = {
            "match_id": row["match_id"],
            "status": status,
            "elapsed_s": round((end - started).total_seconds(), 1),
            "deadline_s": self._limits.claude_deadline_s,
            "decks": dict(row["decks"]),
            "seed": row["seed"],
            "turn": None,
            "step": None,
            "seats": {seat: {"state": "stopped", **row["seats"][seat]} for seat in _SEATS},
            **support_unknown("unavailable"),
        }
        if status in _LIVE:
            out["poll_every_s"] = POLL_EVERY_S
        if row.get("result") is not None:
            out["result"] = row["result"]
        if row.get("error") is not None:
            out["error"] = row["error"]
        return redact(out)

    def _finish(
        self,
        match: _ClaudeMatch,
        status: str,
        error: str | None,
        *,
        expect: Collection[str] = _LIVE,
        cleanup: bool = True,
    ) -> bool:
        """The one terminal transition: claim it only from a status in `expect`, set the
        result, `ended` and the metric, then clean up outside the lock. Returns whether this
        call claimed it (a racing caller that lost does nothing)."""
        result = self._result_of(match)
        with match.lock:
            if match.status not in expect:
                return False
            match.status, match.error, match.finished = status, error, self._clock()
            match.finished_wall = self._wall()
            if result is not None:
                match.result = result
            match.ended.set()
        _count(status)
        if cleanup:
            self._cleanup(match)  # before the row write: seats and game never wait on the store
        self._persist(match)
        return True

    def _result_of(self, match: _ClaudeMatch) -> dict[str, Any] | None:
        """Winner, drawn, turns and the last log lines of the match's game, or `None` when
        the game is not readable (not booted, gone or unloadable). Call without the lock."""
        from magic_cards_model_api.store import store

        with match.lock:
            game_id = match.game_id
        if game_id is None:
            return None
        try:
            game = store.get(game_id)
        except Exception as exc:  # noqa: BLE001 - GameUnloadable or a read failure: no result
            logger.warning("claude sim result read failed (%s)", type(exc).__name__)
            return None
        if game is None:
            return None
        turn = game.turn
        return {
            "winner": game.winner if game.winner in _SEATS else None,
            "drawn": bool(game.drawn),
            "turns": turn.turn_number if turn is not None else None,
            "last_log": self._last_log(game_id),
        }

    @staticmethod
    def _log_lines_for(
        game_id: str | None, secrets: list[str], flt: LogFilter, since_seq: int = 0
    ) -> list[LogLine]:
        """The compact-v1 lines of a game's ledger, live or deleted, secrets scrubbed.

        `since_seq > 0` projects only the rows after `since_seq` (that row itself is read as
        the delta baseline). `[]` when there is no game or the read fails.
        """
        from magic_cards_model_api.store import store

        if game_id is None:
            return []
        try:
            baseline = None
            if since_seq > 0:
                rows = store.records_since(game_id, since_seq - 1)
                if rows:
                    baseline = rows[0]["payload"].get("snapshot")
                    rows = rows[1:]
            else:
                rows = store.records(game_id)
            game = store.get(game_id)
            if game is None:
                archived = store.archived(game_id)
                game = archived.game if archived is not None else None
        except Exception as exc:  # noqa: BLE001 - the answer stands without its log
            logger.warning("claude sim log read failed (%s)", type(exc).__name__)
            return []

        def names(value: str) -> str | None:
            try:
                found = game.find_by_id(UUID(value)) if game is not None else None
            except Exception:  # noqa: BLE001 - not a card id of this game
                return None
            return getattr(found, "name", None)

        def scrub(text: str) -> str:
            for secret in secrets:
                if secret:
                    text = text.replace(secret, "<redacted>")
            return text

        return gamelog.project(rows, names, flt, baseline=baseline, scrub=scrub)

    def _advance(self, match: _ClaudeMatch) -> None:
        """The reaper's pass over one match: read seats and game, apply the transitions.

        All reads and the cleanup run without `match.lock`; the lock only guards the cached
        fields and the claim of a transition, so `status` never waits on cluster I/O.
        """
        from magic_cards_model_api.repository import GameUnloadable
        from magic_cards_model_api.store import store

        with match.lock:
            if match.status == "starting":
                if self._clock() - match.started < self._start_deadline_s():
                    return
                start_timed_out = True
            elif match.status not in ("running", "stalled"):
                return
            else:
                start_timed_out = False
                uids, game_id = dict(match.uids), match.game_id
        if start_timed_out:
            # Cleans up what the start created so far; its thread cleans up the rest.
            self._finish(
                match,
                "failed",
                f"The match did not finish starting within {self._start_deadline_s():g} s"
                " (card resolution or the seat Jobs hung), so it stopped. Try again.",
                expect=("starting",),
            )
            return
        states = {seat: _seat_state(uid) for seat, uid in uids.items()}
        try:
            game = store.get(game_id)
        except GameUnloadable:
            game = None
        turn = game.turn if game is not None else None
        seq = self._max_seq(game_id) if game is not None else None
        status, error = None, None
        if game is None:
            status = "failed"
            error = "The game is gone (ended outside the sim), so the match stopped."
        elif game.is_over:
            status = "done"
        elif self._clock() - match.started >= self._limits.claude_deadline_s:
            status = "deadline"
        elif states and all(state in _DEAD_STATES for state in states.values()):
            status = "failed"
            error = (
                "Both seats stopped (gone or error) before the game ended, so the match"
                " stopped; the result is the game so far."
            )
        with match.lock:
            if match.status not in ("running", "stalled"):
                return
            now = self._clock()
            match.seat_states, match.checked = states, now
            if game is not None:
                match.turn = turn.turn_number if turn is not None else None
                match.step = str(turn.step) if turn is not None else None
            changed = False
            if seq is not None and seq != match.last_seq:
                if match.last_seq is not None:
                    match.last_progress = now
                match.last_seq = seq
                changed = True
                if match.status == "stalled":
                    match.status, match.stalled_reason = "running", None
            idle = now - (match.last_progress if match.last_progress is not None else match.started)
        if status is not None:
            self._finish(match, status, error)
            return
        # A failed progress read (seq None) is no information, not "no progress".
        if seq is not None and idle >= self._limits.claude_stall_s:
            reason = self._stall_reason(game_id, game, states, idle)
            if idle >= self._limits.claude_stall_abort_s:
                abort = self._limits.claude_stall_abort_s
                self._finish(
                    match, "failed", f"aborted after {abort:g} s without progress: {reason}"
                )
                return
            with match.lock:
                if match.status not in ("running", "stalled"):
                    return
                match.status, match.stalled_reason = "stalled", reason
            changed = True
        if changed:
            self._persist(match)

    @staticmethod
    def _max_seq(game_id: str | None) -> int | None:
        from magic_cards_model_api.store import store

        if game_id is None:
            return None
        try:
            return store.max_record_seq(game_id)
        except Exception as exc:  # noqa: BLE001 - no progress read: the watchdog skips this pass
            logger.warning("claude sim progress read failed (%s)", type(exc).__name__)
            return None

    def _stall_reason(
        self, game_id: str, game: Game | None, states: dict[str, str], idle_s: float
    ) -> str:
        """One ASCII line on why the match looks stuck; a part whose data is missing is left
        out. Reads the store without `match.lock`."""
        from magic_cards_model_api.store import store

        parts = [f"no action for {idle_s:.0f} s"]
        turn = game.turn if game is not None else None
        if turn is not None:
            parts[0] += f" at T{turn.turn_number} {turn.step}"
        payload: dict[str, Any] = {}
        pending = None
        try:
            tail = store.recent_records(game_id, 1)
            payload = tail[0]["payload"] if tail else {}
            paused = store.paused_command(game_id)
            pending = paused[2].decision if paused is not None else None
        except Exception as exc:  # noqa: BLE001 - the reason stands with what could be read
            logger.warning("claude sim stall read failed (%s)", type(exc).__name__)
        if payload.get("priority_holder"):
            parts.append(f"priority: {payload['priority_holder']}")
        if pending is not None:
            kind = getattr(pending.kind, "value", pending.kind)
            prompt = str(pending.prompt)[:120]
            parts.append(f"pending decision: {kind} ({pending.player}): {prompt}")
        stack = (payload.get("snapshot") or {}).get("stack") or []
        if stack:
            top = stack[-1]
            parts.append(f"stack top: {top.get('name') if isinstance(top, dict) else top}")
        parts.append("seats " + " ".join(f"{seat}={state}" for seat, state in states.items()))
        return gamelog.ascii_text("; ".join(parts))

    def _start_deadline_s(self) -> float:
        return min(START_DEADLINE_S, self._limits.claude_deadline_s)

    @staticmethod
    def _last_log(game_id: str) -> list[dict[str, Any]]:
        from magic_cards_model_api.seatkit import log_headline
        from magic_cards_model_api.store import store

        try:
            rows = store.recent_records(game_id, _LAST_LOG)
        except Exception as exc:  # noqa: BLE001 - the result stands without its tail
            logger.warning("claude sim log read failed (%s)", type(exc).__name__)
            return []
        return [log_headline({**row["payload"], "seq": row["seq"]}) for row in rows]

    @staticmethod
    def _cleanup(match: _ClaudeMatch) -> None:
        """Stop the seats and delete the game; call without `match.lock` (cluster I/O)."""
        with match.lock:
            game_id, uids = match.game_id, list(match.uids.values())
        if game_id is not None:
            _stop_and_delete(game_id, uids)

    def _answer(self, match: _ClaudeMatch) -> dict[str, Any]:
        with match.lock:
            status, turn, step = match.status, match.turn, match.step
            result, error, finished = match.result, match.error, match.finished
            seat_states, checked = dict(match.seat_states), match.checked
            last_seq, last_progress = match.last_seq, match.last_progress
            stalled_reason = match.stalled_reason
        if status == "starting":
            states = {seat: "starting" for seat in _SEATS}
        elif status in ("running", "stalled"):
            states = {seat: seat_states.get(seat, "unknown") for seat in _SEATS}
        else:
            states = {seat: "stopped" for seat in _SEATS}
        echo = _seat_echo(match.seat_settings)
        seats = {seat: {"state": states[seat], **echo[seat]} for seat in _SEATS}
        end = finished if finished is not None else self._clock()
        out: dict[str, Any] = {
            "match_id": match.match_id,
            "status": status,
            "elapsed_s": round(end - match.started, 1),
            "deadline_s": self._limits.claude_deadline_s,
            "decks": dict(match.decks),
            "seed": match.seed,
            "turn": turn,
            "step": step,
            "seats": seats,
            **match.support,
        }
        if status in _LIVE:
            out["poll_every_s"] = POLL_EVERY_S
        if status in ("running", "stalled"):
            out["last_seq"] = last_seq
            out["last_progress_s_ago"] = (
                None if last_progress is None else round(self._clock() - last_progress, 1)
            )
            if status == "stalled":
                out["stalled_reason"] = stalled_reason
            # Seat states, turn and step are the reaper's last read (every 30 s).
            out["checked_s_ago"] = None if checked is None else round(self._clock() - checked, 1)
        if result is not None:
            out["result"] = result
        if error is not None:
            out["error"] = error
        return redact(out, match.secrets())

    def reap(self) -> list[str]:
        """Refresh each match's seat states, turn and step; apply the start-deadline, game-over,
        deadline and dead-seat transitions; drop old results. Runs on the reaper thread."""
        with self._lock:
            matches = list(self._matches.values())
        for match in matches:
            try:
                self._advance(match)
            except Exception as exc:  # noqa: BLE001 - one match must not stop the others
                logger.warning("claude sim reap failed (%s)", type(exc).__name__)
        now = self._clock()
        with self._lock:
            live = list(self._matches.values())
        idles = []
        for match in live:
            with match.lock:
                if match.status == "stalled" and match.last_progress is not None:
                    idles.append(now - match.last_progress)
        STALL_SECONDS.set(max(idles, default=0))
        with self._lock:
            old = [
                mid
                for mid, m in self._matches.items()
                if m.finished is not None and now - m.finished > self._limits.result_ttl_s
            ]
            for mid in old:
                del self._matches[mid]
        self._drop_old_rows(now)
        return old

    def _drop_old_rows(self, now: float) -> None:
        """Delete persisted rows past the log retention, at most once per hour."""
        if self._last_retention is not None and now - self._last_retention < _RETENTION_EVERY_S:
            return
        self._last_retention = now
        from magic_cards_model_api.store import store

        try:
            store.delete_sim_claude_matches_before(
                self._wall() - timedelta(seconds=self._limits.log_retention_s)
            )
        except Exception as exc:  # noqa: BLE001 - retention is best effort
            logger.warning("claude sim retention failed (%s)", type(exc).__name__)

    def sweep_orphans(self) -> list[str]:
        """Stop and delete every live `SIM_OWNER` game this manager does not track.

        After a restart the in-memory registry and match table are gone, so nothing would
        ever stop those seats or delete the game. The registry forgot the seats too, so
        stopping is best effort; deleting the game takes it out of the live set, and
        `K8sSeats.reconcile` then deletes its seat Jobs.
        """
        from magic_cards_model_api.store import store

        with self._lock:
            tracked = {m.game_id for m in self._matches.values() if m.status in _LIVE}
        swept: list[str] = []
        for game_id in store.ids():
            if game_id in tracked:
                continue
            try:
                if store.owner_user_id(game_id) != SIM_OWNER:
                    continue
                uids = list(store.seat_uids(game_id))
            except Exception as exc:  # noqa: BLE001 - one unreadable row must not stop the sweep
                logger.warning("claude sim sweep skipped a game (%s)", type(exc).__name__)
                continue
            _stop_and_delete(game_id, uids)
            swept.append(game_id)
        if swept:
            logger.info("claude sim sweep deleted %d orphaned game(s)", len(swept))
        self._fail_orphan_rows()
        return swept

    def _fail_orphan_rows(self) -> None:
        """A restart stopped the seats of every row still live: mark them failed, except the
        matches this manager holds."""
        with self._lock:
            keep = [mid for mid, m in self._matches.items() if m.status in _LIVE]
        try:
            from magic_cards_model_api.store import store

            failed = store.fail_live_sim_claude_matches(RESTART_ERROR, self._wall(), keep=keep)
        except Exception as exc:  # noqa: BLE001 - the sweep must not stop the lifespan
            logger.warning("claude sim row sweep failed (%s)", type(exc).__name__)
            return
        if failed:
            logger.info("claude sim sweep failed %d orphaned match row(s)", len(failed))


def _seat_echo(settings: dict[str, tuple[str, str]]) -> dict[str, dict[str, str]]:
    return {seat: {"model": model, "effort": effort} for seat, (model, effort) in settings.items()}


def _seat_state(uid: str) -> str:
    try:
        status = ai_seat.registry.status(uid)
    except Exception as exc:  # noqa: BLE001 - an unreadable seat is reported, not raised
        logger.warning("claude sim seat status failed (%s)", type(exc).__name__)
        return "unknown"
    state = (status or {}).get("state")
    return str(state) if state else "unknown"


def _stop_and_delete(game_id: str, uids: list[str]) -> None:
    """Stop the pregame backstop and each seat (its Job and any F1 follow-up Job), then delete
    the game. Never raises."""
    from magic_cards_model_api import ai_pregame
    from magic_cards_model_api.store import store

    ai_pregame.stop(game_id)
    for uid in uids:
        try:
            ai_seat.registry.stop(uid)
        except Exception as exc:  # noqa: BLE001 - the game is deleted regardless
            logger.warning("claude sim seat stop failed (%s)", type(exc).__name__)
    try:
        store.delete(game_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("claude sim game delete failed (%s)", type(exc).__name__)
