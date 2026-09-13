"""The supervised interactive claude process, and the turn state machine.

    booting -> ready -> busy -> ready -> ... -> dead

One process handles one turn at a time, so a second submit while busy is a
409. Everything here that looks like a magic number came from running the
real binary, not from taste - see the module comments at each one.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .config import Settings
from .metrics import Metrics
from .probe import DEFAULT_PROBE_TEXT, ProbeTracker, ProbeUnavailableError
from .pty_proc import ClaudeProcess, spawn_claude
from .ring import Ring
from .transcript import turn_marker
from .turns import Record, Store

log = logging.getLogger("wrapper.session")

#: Wrap the message in bracketed paste, then submit with a bare CR. ONE write
#: does not submit - the CR must be a separate write after a pause.
DEFAULT_PASTE_START = "\x1b[200~"
DEFAULT_PASTE_END = "\x1b[201~"
DEFAULT_SUBMIT = "\r"

#: The bypass-permissions warning is not seedable and appears on every boot.
BYPASS_NEEDLE = "Bypass Permissions mode"

#: Errors claude prints into the TUI and then *sits* at its prompt for: the
#: process does not exit, so `_watch` never fires, and no Stop hook runs, so
#: `complete` never fires either. Before this list the turn simply hung, and
#: the only timer on it was `mark_stall_suspected`, which by its own
#: docstring is a report and not a verdict - it logs, re-arms, and leaves the
#: seat busy forever.
#:
#: Observed live in this stack: an ANTHROPIC_API_KEY the API rejected made
#: the agent print "Please run /login - API Error: 401 API key is invalid."
#: three minutes into a turn and go quiet. `GET /v1/session` still said
#: `state: busy, stallSuspectedSince: null` half an hour later, the driver
#: still said `pending: false, lastError: null`, and the human in the play UI
#: sat at "AI (claude): dispatched" with an opponent that would never move.
#:
#: These are all terminal for the session, not for one turn - a turn failed
#: on one of them will fail on the next one too, and that is the point: the
#: reason reaches the driver and the UI on the first turn instead of never.
FATAL_NEEDLES = (
    "API Error: 401",
    "API Error: 403",
    "Please run /login",
    "Credit balance is too low",
    "OAuth token has expired",
)

#: Boot is declared over on output quiescence, never on a fixed delay: claude
#: paints its first frame in ~2s but is still initialising, and submitting
#: then exits it.
MIN_BOOT = 4.0
IDLE_READY = 1.5
BOOT_POLL = 0.15


@dataclass(frozen=True)
class SubmitSequence:
    paste_start: str = DEFAULT_PASTE_START
    paste_end: str = DEFAULT_PASTE_END
    submit: str = DEFAULT_SUBMIT


DEFAULT_SUBMIT_SEQ = SubmitSequence()


class State(StrEnum):
    BOOTING = "booting"
    READY = "ready"
    BUSY = "busy"
    DEAD = "dead"


class BusyError(RuntimeError):
    """A turn is already in flight."""


class NotReadyError(RuntimeError):
    """The session is still booting."""


class DeadError(RuntimeError):
    """The supervised claude process is gone."""


_id_lock = threading.Lock()
_last_id_nanos = 0


def new_turn_id() -> str:
    """`turn-<base36 nanos>`. The clock is only microsecond-grained on some
    platforms (macOS notably), so two ids minted in the same tick would
    collide - and a turn id keys the store. A monotonic floor makes the
    sequence strictly increasing, keeping ids both unique and sortable."""
    global _last_id_nanos
    with _id_lock:
        nanos = max(time.time_ns(), _last_id_nanos + 1)
        _last_id_nanos = nanos
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while nanos:
        nanos, remainder = divmod(nanos, 36)
        out = digits[remainder] + out
    return f"turn-{out}"


class Manager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        metrics: Metrics,
        *,
        now: Callable[[], float] = time.time,
        new_id: Callable[[], str] = new_turn_id,
        spawn: Callable[[Settings, bool], ClaudeProcess] = spawn_claude,
        on_turn_done: Callable[[Record], None] | None = None,
        on_dead: Callable[[], None] | None = None,
        submit_seq: SubmitSequence = DEFAULT_SUBMIT_SEQ,
    ) -> None:
        self.settings = settings
        self.store = store
        self.metrics = metrics
        self.now = now
        self.new_id = new_id
        self.spawn = spawn
        self.on_turn_done = on_turn_done
        self.on_dead = on_dead
        self.submit_seq = submit_seq

        self.ring = Ring()
        self.probes = ProbeTracker()
        self._probe_seq = 0
        self.transcript_path = ""
        self._lock = threading.RLock()
        self._pty_lock = threading.Lock()
        self._state = State.BOOTING
        self._proc: ClaudeProcess | None = None
        self._current: str | None = None
        self._current_started = 0.0
        self._timer: threading.Timer | None = None
        self._interrupt_timer: threading.Timer | None = None
        #: True from an interrupt escalation until a completion is accepted:
        #: while it holds, a completion naming no turn may be the escalated
        #: turn's late Stop hook, so it is not trusted (`complete`).
        self._escalated = False
        #: Turn id -> the background tasks a deferred Stop hook named, kept
        #: so the completion that follows can say they finished (`complete`).
        self._deferred: dict[str, list[str]] = {}
        self._stall_suspected_since: float | None = None
        self._fatal_turn: str | None = None
        self._last_fatal: str = ""
        self._stopping = False
        self._threads: list[threading.Thread] = []
        #: What the agent is doing right now, as one coarse word - set by
        #: `__main__` to the `ActivityWatcher`'s own `activity_snapshot`
        #: once that thread exists. An attribute rather than a constructor
        #: argument because the watcher is started *after* the session (it
        #: follows the transcript this boot creates), and because the
        #: wrapper must run perfectly well without one: left `None`, the
        #: snapshot simply reports no activity and the play UI falls back
        #: to the seat's plain state word.
        self.activity_source: Callable[[], dict[str, Any] | None] | None = None

    # ---- lifecycle -------------------------------------------------------

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    @property
    def current_turn_id(self) -> str | None:
        with self._lock:
            return self._current

    def start(self) -> None:
        proc = self.spawn(self.settings, False)
        with self._lock:
            self._proc = proc
            # Cleared here, atomically with the proc swap, not by `restart()`
            # before calling this: clearing it earlier reopens the window
            # where the old process's `_watch` thread can see `_stopping`
            # False *and* `self._proc` still pointing at the old (closed)
            # proc, and mark the session dead out from under the new one.
            self._stopping = False
        for target in (self._read_pty, self._watch):
            thread = threading.Thread(target=target, args=(proc,), daemon=True)
            thread.start()
            self._threads.append(thread)
        self.boot_wait(proc)

    def _read_pty(self, proc: ClaudeProcess) -> None:
        """Copy the TUI's output into the ring. The only window into the
        session: boot_wait reads it for dialogs, _watch logs its tail."""
        while True:
            try:
                chunk = proc.read(4096)
            except OSError:
                return
            if not chunk:
                return
            self.ring.write(chunk)
            self.fail_on_fatal_output()

    def fail_on_fatal_output(self) -> None:
        """Fail the in-flight turn if the TUI has printed a fatal error.

        Called on every PTY chunk, so the reason reaches the driver's
        callback within a read of claude giving up, rather than never. Scans
        the whole ring rather than only the new chunk: a needle can straddle
        a 4 KiB boundary, and `boot_wait` already reads the ring the same way
        for its dialog. A needle left in the ring by an earlier turn is not a
        false positive - every entry in `FATAL_NEEDLES` is fatal to the
        *session*, so failing the next turn on it too is the truth, and the
        ring is cleared on relaunch by `Ring.reset`.

        At most one failure per turn id, so the rest of a chunky error
        message does not fail an already-failed turn a second time.
        """
        with self._lock:
            turn_id = self._current
            if turn_id is None or turn_id == self._fatal_turn:
                return
        hit = next((needle for needle in FATAL_NEEDLES if self.ring.contains(needle)), None)
        if hit is None:
            return
        with self._lock:
            if self._current != turn_id:
                return
            self._fatal_turn = turn_id
            self._last_fatal = hit
            self._current = None
            self._cancel_timer_locked()
            self._stall_suspected_since = None
            if self._state is not State.DEAD:
                self._state = State.READY
        self._finish_failed(turn_id, f"claude reported a fatal error: {hit}")

    def boot_wait(self, proc: ClaudeProcess) -> None:
        """Accept the bypass warning, then wait for the TUI to settle.

        Without accepting the warning, the first turn's submit keystroke
        lands on the dialog and exits claude.
        """
        start = time.monotonic()
        deadline = start + self.settings.boot_timeout
        last_written = self.ring.written()
        last_change = start
        accepted = False

        while True:
            if self._is_dead() or not self._is_active(proc) or self._stopping:
                return
            now = time.monotonic()
            if now >= deadline:
                break
            if not accepted and self.ring.contains(BYPASS_NEEDLE):
                time.sleep(0.6)
                self.write_raw("\x1b[B")  # down arrow -> "Yes, I accept"
                time.sleep(0.25)
                self.write_raw("\r")
                accepted = True
                log.info("accepted bypass-permissions warning")
                time.sleep(0.5)
                continue
            written = self.ring.written()
            if written != last_written:
                last_written, last_change = written, now
            if now - start >= MIN_BOOT and now - last_change >= IDLE_READY:
                break
            time.sleep(BOOT_POLL)

        with self._lock:
            if self._state is State.BOOTING and self._proc is proc:
                self._state = State.READY
        log.info(
            "session ready boot_ms=%d accepted_bypass=%s",
            int((time.monotonic() - start) * 1000),
            accepted,
        )

    def _watch(self, proc: ClaudeProcess) -> None:
        """Block until claude exits, then mark the session dead and fail any
        turn that was in flight. Nothing recovers here: the container is
        restarted by its restart policy, and the transcript survives on the
        workspace volume - which is why `on_dead` asks the process to exit
        rather than leaving a container that is up and can never work."""
        try:
            code = proc.wait()
        except ChildProcessError:
            code = -1
        if self._stopping or not self._is_active(proc):
            # `not self._is_active(proc)`: this is the watcher for a process
            # `restart()` has already replaced - `_stopping` is only held
            # true for the narrow window `restart()` closes the old pty in,
            # and a slow-scheduled thread can wake up to check it after
            # `restart()` has already reset it and started the replacement.
            # Checking against the *current* `self._proc` instead of a flag
            # with a timing window is what actually makes this race-proof.
            return
        tail = self.ring.tail(800)
        log.error("claude exited code=%s pty_tail=%r", code, tail)
        self.metrics.claude_restarts_total.inc()
        with self._lock:
            self._state = State.DEAD
            turn_id = self._current
            self._current = None
            self._cancel_timer_locked()
        if turn_id:
            self._finish_failed(turn_id, f"claude exited with code {code}")
        if self.on_dead is not None:
            try:
                self.on_dead()
            except Exception as exc:  # noqa: BLE001 - a bad hook must not hide the death
                log.error("on_dead raised: %s", exc)

    def shutdown(self) -> None:
        self._stopping = True
        with self._lock:
            proc = self._proc
            self._cancel_timer_locked()
        if proc is not None:
            proc.close()

    def restart(self, settings: Settings) -> None:
        """Swap in new per-game settings (model/effort/max_thinking_tokens)
        and relaunch the supervised claude with them.

        `POST /v1/session`'s only caller: the driver's `assign` calls this
        (through the route) before pushing turn 0, when the per-game
        `AgentSettings` it was dispatched with differ from what this
        container booted with. Refuses with `BusyError` while a turn is in
        flight - the supervised claude cannot be pulled out from under one -
        and with `DeadError` if the session has already died. Otherwise this
        reuses `shutdown()`'s close-the-pty step and `start()`'s
        spawn-and-boot step, exactly as a driver-issued `DELETE /v1/session`
        followed by a fresh container would do, but inside this same
        process, rather than `on_exit`ing the whole process.
        """
        with self._lock:
            if self._state is State.DEAD:
                raise DeadError("session dead")
            if self._current is not None:
                raise BusyError("session busy")
            proc = self._proc
            # Borrow shutdown's own flag so `_watch` (still running for the
            # about-to-be-replaced `proc`) treats this exit as expected and
            # does not mark the session DEAD or fire `on_dead` for it.
            self._stopping = True
            self._cancel_timer_locked()
        if proc is not None:
            proc.close()
        self.ring.reset()
        with self._lock:
            self.settings = settings
            self._state = State.BOOTING
            # `_stopping` stays True across the gap until `start()` installs
            # the replacement `self._proc` and clears it atomically with that
            # swap - see the comment there. Clearing it here would let the
            # outgoing `_watch` thread (whose `proc.wait()` returns promptly
            # once `close()` above SIGTERMs the child) pass its guard while
            # `self._proc` still points at the very process it is watching,
            # since `spawn()` inside `start()` has not returned yet.
        self.start()

    def _is_dead(self) -> bool:
        return self.state is State.DEAD

    def _is_active(self, proc: ClaudeProcess) -> bool:
        with self._lock:
            return self._proc is proc

    # ---- writing ---------------------------------------------------------

    def write_raw(self, text: str) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.write(text.encode())
            except OSError as exc:
                log.warning("raw write failed: %s", exc)

    # ---- turns -----------------------------------------------------------

    def submit(self, text: str, callback_url: str = "") -> str:
        with self._lock:
            if self._state is State.DEAD:
                raise DeadError("session dead")
            if self._state is State.BOOTING:
                raise NotReadyError("session not ready")
            if self._current is not None:
                raise BusyError("session busy")
            turn_id = self.new_id()
            started = self.now()
            self.store.create(turn_id, text, callback_url, started)
            # Reserve the slot before the writes, so a concurrent submit sees
            # BusyError even while we sleep between the paste and the CR.
            self._current, self._current_started = turn_id, started
            self._state = State.BUSY
            proc = self._proc
            self._stall_suspected_since = None
        self.metrics.turn_in_flight.set(1)

        seq = self.submit_seq
        # The turn's marker rides on its prompt so the Stop hook can say which
        # turn stopped (`transcript.last_turn_marker`, checked in `complete`).
        stamped = f"{text}\n\n{turn_marker(turn_id)}"
        # Serialise against any other PTY writer (probe, interrupt) so nothing
        # lands inside the paste block or the delay window.
        with self._pty_lock:
            try:
                proc.write((seq.paste_start + stamped + seq.paste_end).encode())
                time.sleep(self.settings.submit_delay)
                proc.write(seq.submit.encode())
            except OSError as exc:
                self._finish_failed(turn_id, f"write to pty failed: {exc}")
                with self._lock:
                    self._current = None
                    self._state = State.READY
                raise

        with self._lock:
            self._timer = threading.Timer(
                self.settings.turn_timeout, self.mark_stall_suspected, args=(turn_id,)
            )
            self._timer.daemon = True
            self._timer.start()
        log.info("turn submitted turn_id=%s", turn_id)
        return turn_id

    def mark_stall_suspected(self, turn_id: str) -> None:
        """The inactivity timer fired. This is a REPORT, not a verdict: a
        wrapper inside one container cannot tell a hang from work it cannot
        see, and a Magic turn waits on a human. Re-arm and carry on."""
        with self._lock:
            if self._current != turn_id:
                return
            if self._stall_suspected_since is None:
                self._stall_suspected_since = self.now()
            self.metrics.turn_stall_suspected_total.inc()
            self._timer = threading.Timer(
                self.settings.turn_timeout, self.mark_stall_suspected, args=(turn_id,)
            )
            self._timer.daemon = True
            self._timer.start()
        log.warning("turn stall suspected turn_id=%s", turn_id)

    def complete(self, result: dict[str, Any]) -> None:
        """Complete the turn in flight - only if this is its Stop hook.

        `turnId` is the turn the hook read off the transcript. A different
        turn's id is a stale completion (an escalated or failed turn's Stop
        hook landing late) and is ignored. No id at all is trusted, as it
        always was, except after an escalation: then it may be the escalated
        turn's own late hook, and ending the next turn on it (the handover
        turn) would report a handover that never ran.

        A Stop hook that lists `pendingTasks` - subagents or background
        commands the turn launched that have not reported back - does not
        complete the turn: claude stopped, but the turn's work is still
        running, and it will take each task's notification as a prompt and
        run the Stop hook again. The turn completes at the first Stop hook
        that lists none. Nothing here bounds that wait: the driver's own
        turn and handover budgets do."""
        claimed = str(result.get("turnId") or "")
        pending = [str(task) for task in result.get("pendingTasks") or []]
        with self._lock:
            turn_id = self._current
            if turn_id is None:
                log.warning("turn-complete with no turn in flight, ignoring")
                return
            stale = (claimed and claimed != turn_id) or (not claimed and self._escalated)
            if stale:
                log.warning(
                    "stale turn-complete ignored: it is for turn %s, and turn_id=%s is in flight",
                    claimed or "(unnamed, after an interrupt escalation)",
                    turn_id,
                )
                return
            if pending:
                self._deferred[turn_id] = pending
                self.store.touch(turn_id, self.now())
                log.info(
                    "turn stop deferred turn_id=%s: %d background task(s) still running (%s); "
                    "the turn completes at the Stop hook after they report back",
                    turn_id,
                    len(pending),
                    ", ".join(pending),
                )
                return
            waited = self._deferred.pop(turn_id, None)
            self._escalated = False
            started = self._current_started
            self._cancel_timer_locked()
            self._current = None
            self._stall_suspected_since = None
            if self._state is not State.DEAD:
                self._state = State.READY
            path = result.get("transcriptPath") or ""
            if path:
                self.transcript_path = path

        if waited:
            log.info(
                "background tasks finished turn_id=%s: %s; completing the turn",
                turn_id,
                ", ".join(waited),
            )
        record = self.store.complete(
            turn_id,
            final_text=result.get("finalText") or "",
            result_json=result.get("resultJson"),
            usage=result.get("usage"),
            turn_tokens=result.get("turnTokens") or [],
            stop_reason=result.get("stopReason") or "",
            now=self.now(),
        )
        self.metrics.turn_in_flight.set(0)
        self.metrics.hook_received_total.inc()
        self.metrics.turns_total.labels(result="complete").inc()
        self.metrics.turn_duration_seconds.observe(max(0.0, self.now() - started))
        self.metrics.record_tokens(record.turn_tokens)
        self.metrics.record_cost(record.result_json)
        log.info(
            "turn complete turn_id=%s seconds=%.1f stop_reason=%s final_text_chars=%d",
            turn_id,
            max(0.0, self.now() - started),
            record.stop_reason or "-",
            len(record.final_text),
        )
        self._deliver(record)

    # ---- probing ---------------------------------------------------------

    def probe(self, text: str = "") -> str:
        """Write text straight into the PTY mid-turn. Never raises BusyError:
        working while a turn is in flight is the entire point."""
        with self._lock:
            proc = self._proc
            if proc is None or self._state in (State.DEAD, State.BOOTING):
                raise ProbeUnavailableError("probe unavailable: no live session")
            self._probe_seq += 1
            probe_id = f"probe-{self._probe_seq}"
        self.probes.start(probe_id, self.now())
        seq = self.submit_seq
        body = text or DEFAULT_PROBE_TEXT
        with self._pty_lock:
            try:
                proc.write((seq.paste_start + body + seq.paste_end).encode())
                time.sleep(self.settings.submit_delay)
                proc.write(seq.submit.encode())
            except OSError as exc:
                raise ProbeUnavailableError(f"probe write failed: {exc}") from exc
        self.metrics.probe_total.labels(result="written").inc()
        log.info("probe written probe_id=%s", probe_id)
        return probe_id

    def probe_status(self, probe_id: str) -> dict | None:
        return self.probes.status(probe_id)

    def interrupt(self) -> str | None:
        """One ESC byte. Pasted text is only consumed at the next tool-call
        boundary; ESC cancels synchronously even inside a running tool call,
        which is the one failure mode worth interrupting. The session, its
        id, transcript and context all survive.

        claude runs no Stop hook for an interrupted turn, so nothing would
        ever call `complete` for it: a turn in flight is given
        `interrupt_settle` seconds to end on its own, then
        `escalate_interrupt` ends it."""
        with self._lock:
            proc = self._proc
            if proc is None or self._state is State.DEAD:
                raise ProbeUnavailableError("interrupt unavailable: no live session")
            turn_id = self._current
        with self._pty_lock:
            try:
                proc.write(b"\x1b")
            except OSError as exc:
                raise ProbeUnavailableError(f"interrupt write failed: {exc}") from exc
        log.warning("interrupt written turn_id=%s", turn_id)
        if turn_id is not None:
            with self._lock:
                if self._current == turn_id:
                    if self._interrupt_timer is not None:
                        self._interrupt_timer.cancel()
                    self._interrupt_timer = threading.Timer(
                        self.settings.interrupt_settle, self.escalate_interrupt, args=(turn_id,)
                    )
                    self._interrupt_timer.daemon = True
                    self._interrupt_timer.start()
        return turn_id

    def escalate_interrupt(self, turn_id: str) -> None:
        """The interrupted turn has not ended within `interrupt_settle`: write
        a second ESC and end the turn as failed, so the slot is free for the
        next `submit` (the handover turn) while the PTY and the conversation
        stay up. A turn that ended in the meantime is left alone."""
        with self._lock:
            if self._current != turn_id:
                return
            self._interrupt_timer = None
            proc = self._proc
        log.warning(
            "interrupt escalation turn_id=%s: the turn did not end within %gs of the "
            "interrupt, and claude runs no Stop hook for an interrupted turn; writing a "
            "second interrupt and ending the turn",
            turn_id,
            self.settings.interrupt_settle,
        )
        if proc is not None:
            with self._pty_lock:
                try:
                    proc.write(b"\x1b")
                except OSError as exc:
                    log.warning("second interrupt write failed: %s", exc)
        with self._lock:
            if self._current != turn_id:
                return
            self._current = None
            self._escalated = True
            self._cancel_timer_locked()
            self._stall_suspected_since = None
            if self._state is not State.DEAD:
                self._state = State.READY
        self._finish_failed(
            turn_id,
            f"interrupted: the turn had not ended {self.settings.interrupt_settle:g}s "
            "after the interrupt",
        )

    def _finish_failed(self, turn_id: str, reason: str) -> None:
        # A turn that dies here used to be silent in the container log: the
        # driver was told over the callback, but nobody watching the agent
        # container saw why its turn stopped.
        log.error("turn failed turn_id=%s: %s", turn_id, reason)
        record = self.store.fail(turn_id, reason, self.now())
        self.metrics.turn_in_flight.set(0)
        self.metrics.turns_total.labels(result="failed").inc()
        self._deliver(record)

    def _deliver(self, record: Record) -> None:
        """Fire the completion callback OUTSIDE the lock."""
        if self.on_turn_done is None:
            return
        try:
            self.on_turn_done(record)
        except Exception as exc:  # noqa: BLE001 - delivery must not break a turn
            log.error("on_turn_done raised: %s", exc)

    def _cancel_timer_locked(self) -> None:
        for timer in (self._timer, self._interrupt_timer):
            if timer is not None:
                timer.cancel()
        self._timer = self._interrupt_timer = None

    # ---- reporting -------------------------------------------------------

    def _activity(self) -> dict[str, Any] | None:
        """`activity_source()`, or `None` - never a raise.

        The watcher is read-only and deliberately outside the turn path;
        `GET /v1/session` is on the play UI's own poll path, through the
        driver, so a hiccup reading the transcript must cost the pill its
        word and nothing else.
        """
        if self.activity_source is None:
            return None
        try:
            return self.activity_source()
        except Exception as exc:  # noqa: BLE001 - a status read never fails
            log.debug("could not read the agent's current activity: %s", exc)
            return None

    def snapshot(self) -> dict[str, Any]:
        activity = self._activity()
        completed, finished = self.store.counts()
        with self._lock:
            return {
                "activity": activity,
                "state": str(self._state),
                "turnsCompleted": completed,
                "turnsFinished": finished,
                "model": self.settings.model,
                "effort": self.settings.effort,
                "maxThinkingTokens": self.settings.max_thinking_tokens,
                "currentTurnId": self._current,
                "stallSuspectedSince": self._stall_suspected_since,
                "lastFatal": self._last_fatal,
                "transcriptPath": self.transcript_path,
            }
