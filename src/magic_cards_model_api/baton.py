"""The baton: who may act for a seat, and when that passes on.

Several agents can share one seat uid (an orchestrator with a "current" and a
"future" agent). Per uid the store holds one `live` token (the current agent,
the only one the acting MCP tools accept), at most one `next` token (the
future agent, which waits in `await_baton`) and the recently `spent` ones.

`pass_baton` mints a `next` token - live at once when the uid has none - and
never revokes or blocks. The live token rotates on the server, at the end of
the priority window: when an acting call that gave priority away returns
(`finish`), the live token becomes spent and `next` becomes live, under the
same lock that serialises the uid's acting calls (`begin`). Rotation is never
timed by an orchestrator, so nothing lands between a call's check and its
action.

Enforcement is per uid: on once a baton was issued for it, or for every uid
when the server runs with `--require-baton` (`MTG_REQUIRE_BATON=1`). With
neither, tokens are not checked - the legacy, subprocess and advisor paths -
though the per-uid acting lock still holds.

The baton lives only in the MCP process; the game API wire is unchanged.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: The read-only tools (and the orchestrator's `pass_baton`, and the future
#: agent's `await_baton`) that never act in the game, so they never need the
#: baton and never take the acting lock.
BATON_EXEMPT_TOOLS = frozenset(
    {
        "battlefield_state",
        "game_log",
        "list_scripts",
        "report_problem",
        "pass_baton",
        "await_baton",
    }
)

#: Acting tools that wait on the game rather than commit to it: a revoke
#: interrupts them, and they are not listed in a window's committed actions.
WAIT_TOOLS = frozenset({"pass_until", "wait_for_priority"})

#: Acting tools that commit nothing to the game (they wait, stage or record a
#: plan), so they never appear in `last_commit.window_actions`.
NON_COMMITTING_TOOLS = frozenset(
    {"wait_for_priority", "stage_script", "declare_plan", "stage_advice"}
)

BATON_OK = "ok"
BATON_REQUIRED = "BATON_REQUIRED"
BATON_STALE = "BATON_STALE"
BATON_SPENT = "BATON_SPENT"
BATON_NOT_YET = "BATON_NOT_YET"
BATON_BUSY = "BATON_BUSY"
BATON_REVOKED = "BATON_REVOKED"
BATON_ADVISOR = "BATON_ADVISOR"

#: How many spent tokens per uid are remembered (so a late call gets
#: BATON_SPENT rather than BATON_STALE).
SPENT_MEMORY = 64

#: The longest `await_baton` may block.
MAX_AWAIT_SECONDS = 600.0

#: How long `pass_baton(revoke=true)` waits for an interrupted wait to return
#: before it queues the revoke behind it instead.
REVOKE_WAIT_SECONDS = 10.0

#: Appended to the answer of the call that closed the window.
WINDOW_CLOSED = "window closed - your baton is spent; end your run with your committed line"

#: `pass_baton`'s answer to an advisor (buddy) uid. On the http `MCP_URL`
#: path a buddy shares this process and its one store with the seats; its
#: advisor uid is the only credential it holds, so refusing that uid is what
#: keeps a buddy from rotating anything.
ADVISOR_REFUSAL = (
    f"{BATON_ADVISOR}: this uid is an advisor's (a buddy); an advisor has no baton and "
    "may not rotate one. Only the seat's own orchestrator calls pass_baton. Nothing changed."
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


@dataclass
class Flight:
    """One acting call in flight for a uid: what `_guarded` holds between
    `begin` and `finish`."""

    uid: str
    tool: str
    token: str
    wait: bool
    passed: bool = False
    pass_seq: int | None = None
    interrupt: threading.Event = field(default_factory=threading.Event)

    def mark_passed(self, outcome: Any = None) -> None:
        """This call made a successful pass for the seat."""
        self.passed = True
        if isinstance(outcome, dict) and isinstance(outcome.get("log_seq"), int):
            self.pass_seq = outcome["log_seq"]


@dataclass
class _Seat:
    live: str | None = None
    next: str | None = None
    live_since: str | None = None
    spent: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    last_commit: dict[str, Any] | None = None
    window_actions: list[str] = field(default_factory=list)
    flight: Flight | None = None
    #: `next` was minted by a revoke queued behind the call in flight.
    revoke_queued: bool = False
    #: A `pass_until` returned before any pass and left a background run
    #: going: the window closes at that run's first pass.
    background_pending: dict[str, Any] | None = None


class BatonStore:
    """Per-seat-uid batons, thread-safe.

    Prod topology assumption: the game Deployment is `replicas: 1`, so an
    http-served MCP is one process with one store; a second MCP replica would
    hold its own batons and must not be run behind a round-robin.
    """

    def __init__(self, *, require_all: bool = False) -> None:
        self.require_all = require_all
        self._seats: dict[str, _Seat] = {}
        #: The acting lock of uids no baton was issued for.
        self._unenforced: dict[str, Flight] = {}
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)

    # ---- minting ----

    def pass_baton(
        self, uid: str, *, revoke: bool = False, revoke_wait: float = REVOKE_WAIT_SECONDS
    ) -> dict[str, Any]:
        """Mint a token for `uid`: `next` (replacing any unpromoted next,
        which dies), or `live` at once when the uid has no live token.

        `revoke` (recovery only) makes the minted token live in place of the
        current one: at once when nothing is in flight; after an in-flight
        wait was interrupted and returned (`applied`); or, behind a non-wait
        acting call, the moment it returns (`queued_behind:<tool>`)."""
        token = secrets.token_urlsafe(24)
        with self._changed:
            seat = self._seats.setdefault(uid, _Seat())
            revoke_state: str | None = None
            if seat.live is None:
                self._make_live(seat, token)
            else:
                seat.next = token
                seat.revoke_queued = False
                if revoke:
                    flight = seat.flight
                    if flight is not None and flight.wait:
                        flight.interrupt.set()
                        deadline = time.monotonic() + revoke_wait
                        while seat.flight is flight and time.monotonic() < deadline:
                            self._changed.wait(deadline - time.monotonic())
                        flight = seat.flight
                    if seat.next != token:
                        # Something else minted over this token while we waited.
                        revoke_state = "superseded"
                    elif flight is None:
                        self._apply_revoke(seat)
                        revoke_state = "applied"
                    else:
                        seat.revoke_queued = True
                        revoke_state = f"queued_behind:{flight.tool}"
            self._changed.notify_all()
            answer = self._describe(seat, token)
        if revoke:
            answer["revoke"] = revoke_state or "applied"
        return answer

    def issue(self, uid: str) -> str:
        """`pass_baton(uid)`'s token alone."""
        return self.pass_baton(uid)["baton"]

    def _make_live(self, seat: _Seat, token: str) -> None:
        seat.live = token
        seat.live_since = _now()
        seat.window_actions = []
        seat.background_pending = None

    def _spend(self, seat: _Seat, token: str, tool: str, seq: int | None) -> None:
        seat.spent[token] = {"tool": tool, "seq": seq}
        while len(seat.spent) > SPENT_MEMORY:
            seat.spent.popitem(last=False)

    def _apply_revoke(self, seat: _Seat) -> None:
        if seat.live is not None:
            self._spend(seat, seat.live, "pass_baton(revoke)", None)
        token = seat.next
        seat.next = None
        seat.revoke_queued = False
        if token is not None:
            self._make_live(seat, token)
        else:
            seat.live = None

    def _describe(self, seat: _Seat, token: str) -> dict[str, Any]:
        state = "live" if _same(token, seat.live) else "next" if _same(token, seat.next) else ""
        return {
            "baton": token,
            "state": state or "stale",
            "live_holder_since": seat.live_since,
            "last_commit": seat.last_commit,
            "in_flight": seat.flight.tool if seat.flight is not None else None,
        }

    # ---- checking ----

    def enforced(self, uid: str) -> bool:
        with self._lock:
            return self.require_all or uid in self._seats

    def _verdict(self, seat: _Seat | None, token: str) -> str:
        if seat is None and not self.require_all:
            return BATON_OK
        if not token:
            return BATON_REQUIRED
        if seat is None:
            return BATON_STALE
        if _same(token, seat.live):
            return BATON_OK
        if _same(token, seat.next):
            return BATON_NOT_YET
        if any(_same(token, spent) for spent in seat.spent):
            return BATON_SPENT
        return BATON_STALE

    def check(self, uid: str, token: str) -> str:
        """`BATON_OK`, or why `token` may not act for `uid`: `BATON_REQUIRED`
        (none given), `BATON_NOT_YET` (the queued next token), `BATON_SPENT`
        (its window closed) or `BATON_STALE` (unknown, or a next token that
        was replaced before it went live)."""
        with self._lock:
            return self._verdict(self._seats.get(uid), token)

    def state_of(self, uid: str, token: str) -> str:
        """`live`, `next`, `spent` or `stale` for `token` (`live` for any token
        when the uid is not enforced)."""
        return {
            BATON_OK: "live",
            BATON_NOT_YET: "next",
            BATON_SPENT: "spent",
        }.get(self.check(uid, token), "stale")

    def spent_detail(self, uid: str, token: str) -> dict[str, Any]:
        with self._lock:
            seat = self._seats.get(uid)
            if seat is None:
                return {}
            for spent, detail in seat.spent.items():
                if _same(token, spent):
                    return dict(detail)
        return {}

    def holder(self, uid: str, token: str) -> str:
        """For the timing log: `-` when not enforced, `current` for the live
        token, `future` for the next one, `reader` otherwise."""
        if not self.enforced(uid):
            return "-"
        return {BATON_OK: "current", BATON_NOT_YET: "future"}.get(self.check(uid, token), "reader")

    def last_commit(self, uid: str) -> dict[str, Any] | None:
        with self._lock:
            seat = self._seats.get(uid)
            return None if seat is None else seat.last_commit

    def in_flight(self, uid: str) -> str | None:
        with self._lock:
            seat = self._seats.get(uid)
            flight = (seat.flight if seat is not None else None) or self._unenforced.get(uid)
            return None if flight is None else flight.tool

    # ---- one acting call ----

    def begin(self, uid: str, tool: str, token: str) -> tuple[str, Flight | None]:
        """Check `token` and take `uid`'s acting lock, atomically. Returns
        `(BATON_OK, flight)`, or `(code, None)` when refused - `(BATON_BUSY,
        <the call in flight>)` when another acting call holds the lock -
        having touched nothing. A uid with no baton is locked too, without
        starting enforcement for it."""
        with self._lock:
            seat = self._seats.get(uid)
            verdict = self._verdict(seat, token)
            if verdict != BATON_OK:
                return verdict, None
            busy = (seat.flight if seat is not None else None) or self._unenforced.get(uid)
            if busy is not None:
                return BATON_BUSY, busy
            flight = Flight(uid=uid, tool=tool, token=token, wait=tool in WAIT_TOOLS)
            if seat is not None:
                seat.flight = flight
            else:
                self._unenforced[uid] = flight
            return BATON_OK, flight

    def finish(
        self,
        flight: Flight,
        *,
        committed: bool,
        closes_window: bool,
        reasoning: str = "",
        seq: int | None = None,
        defer_to_background: bool = False,
    ) -> bool:
        """Release `flight`'s acting lock; `True` when this call closed the
        priority window and rotated the baton.

        `committed`: the call landed an action (it joins `window_actions`).
        `closes_window`: the call gave priority away (or completed the
        seat's pregame). `defer_to_background`: the call passed nothing
        itself but left a background run that will - the window closes at
        that run's first pass (`background_passed`)."""
        with self._changed:
            if self._unenforced.get(flight.uid) is flight:
                del self._unenforced[flight.uid]
                self._changed.notify_all()
                return False
            seat = self._seats.get(flight.uid)
            if seat is None or seat.flight is not flight:
                return False
            seat.flight = None
            rotated = False
            if _same(flight.token, seat.live):
                if committed:
                    seat.window_actions.append(flight.tool)
                if closes_window:
                    self._rotate(seat, flight.tool, reasoning, seq)
                    rotated = True
                elif defer_to_background:
                    seat.background_pending = {
                        "token": flight.token,
                        "tool": flight.tool,
                        "reasoning": reasoning,
                    }
            if seat.revoke_queued:
                self._apply_revoke(seat)
            self._changed.notify_all()
            return rotated

    def _rotate(self, seat: _Seat, tool: str, reasoning: str, seq: int | None) -> None:
        # Called with the lock held, `seat.live` being the window's token.
        assert seat.live is not None
        self._spend(seat, seat.live, tool, seq)
        seat.last_commit = {
            "tool": tool,
            "reasoning": reasoning,
            "seq": seq,
            "window_actions": list(seat.window_actions),
        }
        token = seat.next
        seat.next = None
        seat.revoke_queued = False
        if token is not None:
            self._make_live(seat, token)
        else:
            seat.live = None
            seat.live_since = None
            seat.window_actions = []
            seat.background_pending = None

    def background_passed(self, uid: str, outcome: Any = None) -> bool:
        """A background run made a pass for `uid`. When a `pass_until` left
        that run going before it passed anything itself, this is where its
        window closes; with an acting call in flight, that call carries the
        pass instead and rotates when it returns."""
        with self._changed:
            seat = self._seats.get(uid)
            if seat is None:
                return False
            if seat.flight is not None:
                seat.flight.mark_passed(outcome)
                return False
            pending = seat.background_pending
            if pending is None or not _same(pending["token"], seat.live):
                return False
            seq = outcome.get("log_seq") if isinstance(outcome, dict) else None
            self._rotate(seat, pending["tool"], pending["reasoning"], seq)
            self._changed.notify_all()
            return True

    def revoke_background(self, uid: str) -> None:
        """A revoke stopped `uid`'s background run: nothing closes there."""
        with self._lock:
            seat = self._seats.get(uid)
            if seat is not None:
                seat.background_pending = None

    # ---- the future agent ----

    def await_live(
        self,
        uid: str,
        token: str,
        max_seconds: float,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> str:
        """Block until `token` is live (`live`), or say what it is: `next`
        at `max_seconds`, `spent`/`stale` at once (it can never go live)."""
        deadline = time.monotonic() + max(0.0, min(max_seconds, MAX_AWAIT_SECONDS))
        with self._changed:
            while True:
                verdict = self._verdict(self._seats.get(uid), token)
                if verdict == BATON_OK:
                    return "live"
                if verdict != BATON_NOT_YET:
                    return "spent" if verdict == BATON_SPENT else "stale"
                left = deadline - time.monotonic()
                if left <= 0 or should_stop():
                    return "next"
                self._changed.wait(min(left, 0.5))


def refusal(code: str, detail: dict[str, Any] | None = None) -> str:
    """The plain tool answer for a refused acting call."""
    detail = detail or {}
    if code == BATON_REQUIRED:
        return (
            "BATON_REQUIRED: acting tools on this seat need the current baton; "
            "the orchestrator issues it with pass_baton. Read with battlefield_state/"
            "game_log/list_scripts and send your plan to the orchestrator."
        )
    if code == BATON_SPENT:
        return (
            f"BATON_SPENT: your priority window closed ({detail.get('tool', 'unknown')} at seq "
            f"{detail.get('seq')}); end your run with your committed line"
        )
    if code == BATON_NOT_YET:
        return "BATON_NOT_YET: you are the future agent; call await_baton"
    if code == BATON_BUSY:
        return f"BATON_BUSY: {detail.get('tool', 'another call')} in flight"
    return (
        "BATON_STALE: this token is not this seat's current or next baton (never issued "
        "here, or replaced before it went live); you are not the current player. Read with "
        "battlefield_state/game_log/list_scripts and send your plan to the orchestrator."
    )


REVOKED_ANSWER = (
    f"{BATON_REVOKED}: the orchestrator revoked this baton while the wait was in "
    "flight; the wait stopped. End your run - you are no longer the current player."
)
