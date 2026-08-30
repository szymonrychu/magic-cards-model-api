"""One seat's proxy onto the API, and the "one call, one meaningful action"
flow both player-facing transports (`mcp.py`, `seat_cli.py`) build on.

`Seat`/`Seats` used to live in `mcp.py`, with `seat_cli.py` importing them
from there; they move here because both transports now also need the action
flow below, and putting it in the module that names the MCP tools would make
`seat_cli` import transport code to get non-transport logic. This project has
twice shipped two implementations of one rule that quietly drifted apart (a
duplicated mana parser, client-side visibility filtering that diverged from
the server); the fix both times was one shared module, which is what this is.

## The flow: `run_action`

CR 117.3c: "If a player has priority when they cast a spell, activate an
ability, or take a special action, that player receives priority afterward."
The engine already does this (`Game.retain_priority`, called from
`engine_api.cast_card`/`activate_ability`) - a player who acts is legally
entitled to act again before passing. Auto-passing right after one action is
therefore a **harness convenience layered on top of a correct rule**, not a
rule itself: two agents playing through a seat found it chatty and error-prone
to separately call an action, then `pass`, then `wait`, then re-fetch state, so
`run_action` folds those into one call. `hold_priority=True` is the escape
hatch back to the real rule - keep priority and decide what to do next, the
same as a human player would after casting a spell they mean to protect.
Recorded honestly in `magic-cards-model/docs/engine-coverage.md` rather than
left looking like a rule of its own.

## Wake conditions

A blocked call returns as soon as one of these holds, for this seat:

- it has priority *and* the visible board changed since the call began (not
  merely "has priority" - that would also fire the instant this seat's own
  action hands priority right back with nothing new to react to);
- a pending decision (CR 608.2d for most kinds; a `choose_targets` decision is
  CR 603.3d/601.2c instead - see `decisions.rule_for_kind`) is addressed to it;
- the game is over (a winner is set, or it is a draw);
- the step gives nobody priority (untap, cleanup) and this seat is the active
  player - the harness has no other way to make the turn proceed but calling
  `advance`.

## Why this cannot deadlock

`block_until_wake` polls `GET /api/games/{id}` - a read that takes no lock
beyond what that endpoint already takes internally, and releases it before
returning. It never holds `store.locked(game_id)` (`store.py`) across the
poll loop; if it did, the same-seat action that is supposed to wake it (or
the opponent's) would queue behind this call forever, since that action needs
the very lock this call would be sitting on. This is the same invariant
`decisions.py` documents for `POST .../decide` versus `Game.ask` - a blocking
wait and the request that clears it must never share a lock - restated here
because `run_action` is the second place in this codebase with the same
shape. `hold_priority=True` additionally guarantees the *action* path never
enters `block_until_wake` at all: the check happens before the loop is ever
reached, by construction, not by timing.

This used to be wrong in one more place, past the loop: every return path of
`run_action` called `available_actions(seat, state)` unconditionally, and
*that* function issues `POST .../actions/cast?dry_run=true` for every card in
hand (and `.../activate` for every ability) - each of which takes
`store.locked(game_id)` (`app.py`). When this seat wakes because a decision is
now pending *for it*, that lock is not free: it is held by the opponent's
request, parked deep inside `Game.ask` (`decisions.py`) waiting on this same
seat to answer through `POST .../decide`. Calling `available_actions` there
queued this call behind the very decision it was returning to let the seat
answer, for the full decision timeout - a real deadlock, not a wait, that
`available_actions` itself now avoids: see its own docstring for the fix.

## The other block: a single request, not the wait loop

`block_until_wake`'s own polling is quick reads and cannot itself run into a
client read timeout. The real risk is upstream of it: `perform()` - one
mutating call (`cast`/`activate`/`attack`/`pass-priority`/`resolve-top`/
`advance`) - can cross a live decision question (CR 608.2d, or CR 603.3d/601.2c
for `choose_targets`) and block *inside that one request*, in `Game.ask`
(`decisions.py`), for up to the server's own
`decision_timeout_seconds`. A `Seat`'s HTTP client timeout is sized to
comfortably outlast that (`resolve_client_timeout`), so a legitimate hold
never reads as a dead server; if a read/write/pool timeout happens anyway,
`Seat.call_json` reports it as `UNKNOWN_OUTCOME` - an action whose outcome is
genuinely unknown, not a refusal - rather than raising, so a caller is never
tempted to retry blind and risk a duplicate cast/activation/attack.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

import httpx

from .decisions import DEFAULT_TIMEOUT_SECONDS as SERVER_DECISION_TIMEOUT_SECONDS

DEFAULT_API = "http://127.0.0.1:8000"

#: Bumped whenever an action tool's contract changes shape. `/api/health`
#: reports this (see `app.py`); every tool call in `mcp.py` checks it against
#: its own import of this constant (`api_trouble`, below) and refuses that one
#: call - loudly, naming both versions - on a mismatch. Exists because the MCP
#: server is a long-running process spawned once at boot (`mcp.py`'s module
#: docstring) - editing this file after that process is already up does not
#: change what it is running, and without a check the first symptom used to
#: be a tool call failing in a way that looked like a bug in the call rather
#: than in the process serving it.
#:
#: This used to be a fatal check in `main()` before serving a single call -
#: correct in spirit (a stale server refusing to run beats a mysterious
#: failure later) but wrong in shape: the API is restarted once per playtest
#: game while the MCP process is spawned once for the whole session, so a
#: boot-time-only check meant the API being briefly unreachable at the wrong
#: moment (or, worse, at any later moment - the check never ran again) either
#: killed the MCP process outright or left it silently trusting a version it
#: had only confirmed once, forever. The check itself was right; only "fatal
#: at boot, never again" was the defect. See `api_trouble`.
#:
#: Bump this alongside any change to a tool's request/response shape.
PROTOCOL_VERSION = "seatkit-1"

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

NO_SUCH_SEAT = (
    "that uid is no seat in this game. You cannot act, and you may see nothing - "
    "a seat uid is given to you when you are dispatched."
)

#: How often a blocked call re-fetches state while waiting for a wake
#: condition. Small: this loop runs inside the caller's own process/thread,
#: unlike `DecisionMailbox` (`decisions.py`), which is woken by a condition
#: variable rather than polling - polling is simpler here because there is no
#: single "the answer arrived" event, only "check the board again".
POLL_SECONDS = 0.2

#: The default bound for a blocked action, when a tool/command does not name
#: its own. Every wait is bounded (never blocks forever); a caller that wants
#: to keep waiting just calls again.
DEFAULT_MAX_SECONDS = 60.0

#: Slack a `Seat`'s/`Seats`' own HTTP read timeout carries over the server's
#: longest *legitimate* single-request block, so ordinary network jitter
#: never masquerades as "the block itself ran over budget". That block is
#: not `DEFAULT_MAX_SECONDS` above - `run_action`'s own wait loop
#: (`block_until_wake`) polls with short, quick reads and re-fetches state
#: repeatedly, so no single HTTP call there is at risk. The real one is a
#: single mutating call (`cast`/`activate`/`attack`/`pass-priority`/
#: `resolve-top`/`advance`) crossing a live decision question (CR 608.2d, or
#: CR 603.3d/601.2c for `choose_targets`): `Game.ask`
#: (`decisions.py`) blocks that one request, inside the game's lock, for up
#: to the server's own `decision_timeout_seconds` (reported at
#: `/api/health`) before falling back to the engine's default answer. A
#: fixed client-side timeout shorter than that turned a legitimate hold into
#: a false "the action failed" - see `resolve_client_timeout`.
TIMEOUT_MARGIN_SECONDS = 30.0

#: Timeout for the one-off bootstrap request `resolve_client_timeout` makes
#: to learn the server's real decision-block bound before building the real
#: client. Short and fixed on purpose: this call must never itself become
#: the thing waiting on a long block, since nothing has decided anything yet.
BOOTSTRAP_TIMEOUT_SECONDS = 10.0

#: Shown in place of a raised exception whenever a request genuinely times
#: out waiting for an answer (`httpx.ReadTimeout`/`WriteTimeout`/`PoolTimeout`
#: - a request that was sent, unlike a bare connection failure). The action
#: may already have been performed server-side - the whole point of this
#: module's `resolve_client_timeout` is to make that rare, not to make it
#: impossible - so this must never read like an ordinary refusal (`run_action`
#: checks for it before its normal "refused" branch) and must never invite a
#: blind retry, which is exactly the duplicate-action hazard this exists to
#: prevent.
#: The `httpx` timeout flavours that mean "a request was sent and we do not
#: know what happened to it" - as opposed to `httpx.ConnectTimeout`/
#: `ConnectError`, which mean the request never left this process, so there
#: is nothing that could have landed server-side. Only these get the
#: `UNKNOWN_OUTCOME` treatment; a plain connection failure keeps its
#: existing, unambiguous "could not reach the API" message.
_AMBIGUOUS_TIMEOUTS = (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)

UNKNOWN_OUTCOME = (
    "TIMED OUT waiting for an answer - the outcome of this call is UNKNOWN. "
    "The action may already have gone through on the server even though "
    "this call never got a response. Do NOT retry it blindly: call state "
    "(or read the game log, e.g. its last log_seq) to see what actually "
    "happened first, then decide what to do from that - never from this "
    "timeout alone."
)


def resolve_client_timeout(bootstrap: httpx.Client) -> float:
    """The read timeout a `Seat`'s/`Seats`' own `httpx.Client` should use,
    derived from the live server's own decision-block bound
    (`/api/health`'s `decision_timeout_seconds`, `decisions.DEFAULT_TIMEOUT_SECONDS`
    on the server) plus `TIMEOUT_MARGIN_SECONDS` of slack - never a bare
    constant sized independently of the thing it must outlast, which is
    exactly how a previous wave shipped a client timeout shorter than a
    legitimate server-side block.

    `bootstrap` is used for exactly one request and is the caller's own
    client (so tests that monkeypatch `httpx.Client` are honoured, same as
    `api_trouble`); an unreachable server or an old build missing the field
    falls back to this process's own import of the server's default
    (`SERVER_DECISION_TIMEOUT_SECONDS`) rather than raising - `api_trouble`
    is what reports that problem loudly on every subsequent call, this only
    needs *a* safe number to build a client with right now.
    """
    try:
        health = bootstrap.get("/api/health").json()
        decision_timeout = float(health["decision_timeout_seconds"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        decision_timeout = SERVER_DECISION_TIMEOUT_SECONDS
    return decision_timeout + TIMEOUT_MARGIN_SECONDS


def api_trouble(http: httpx.Client, api: str) -> str | None:
    """`None` when `api` is reachable and running this process's own
    `PROTOCOL_VERSION`; otherwise the clear, actionable message a tool call
    should return in place of doing anything - never an exception, because
    the caller here is a long-lived MCP process and the thing being checked
    (the API) is restarted far more often than it is (once per playtest
    game, per this module's `PROTOCOL_VERSION` docstring).

    `http` is the caller's own `httpx.Client` (`Seat.http`/`Seats.http`), not
    a fresh one, so this goes through whatever transport the caller is
    already using - tests point it at an in-process `TestClient` by
    monkeypatching `httpx.Client`; a bare module-level `httpx.get` would
    silently skip that and hit the real network instead.
    """
    try:
        health = http.get("/api/health").json()
    except httpx.HTTPError as exc:
        return f"the game server is not reachable at {api}: {exc}"
    except ValueError:
        return f"the game server at {api} did not answer /api/health with JSON"
    reported = health.get("protocol_version")
    if reported != PROTOCOL_VERSION:
        return (
            f"PROTOCOL MISMATCH - refusing this call: this MCP server speaks "
            f"{PROTOCOL_VERSION!r} but the API at {api} reports {reported!r}. "
            "One of the two is running stale code - most likely this MCP "
            "process, since it is spawned once and never reloads (see this "
            "module's PROTOCOL_VERSION docstring). Restart the stale one "
            "before trusting anything it returns."
        )
    return None


class Seat:
    """One player's proxy onto the running game."""

    def __init__(
        self,
        player: str,
        uid: str,
        api: str,
        game_id: str | None,
        timeout: float | httpx.Timeout | None = None,
    ) -> None:
        self.player = player
        self.uid = uid
        self.api = api.rstrip("/")
        self._game_id = game_id
        #: `timeout` is normally `Seats.http.timeout` (resolved once, in
        #: `Seats.__init__`, against the live server) - `Seats.seat_for`
        #: passes it down so the many `Seat` objects it mints do not each pay
        #: for their own bootstrap health call. `None` (a `Seat` built
        #: directly, outside `Seats`) resolves its own, once, the same way.
        if timeout is None:
            with httpx.Client(base_url=self.api, timeout=BOOTSTRAP_TIMEOUT_SECONDS) as bootstrap:
                timeout = resolve_client_timeout(bootstrap)
        self.http = httpx.Client(base_url=self.api, timeout=timeout)
        # Belt and suspenders, as a bare float (see `Seats.__init__`'s same
        # comment on why not `httpx.Timeout(...)`): tests monkeypatch
        # `httpx.Client` itself to hand back a fresh `TestClient` that
        # ignores every constructor argument including `timeout`, so the
        # line above alone would silently leave that client on its own
        # unrelated default. A real `httpx.Client` already has this timeout
        # from the constructor; re-setting it is a no-op there.
        self.http.timeout = timeout.read if isinstance(timeout, httpx.Timeout) else timeout

    def game_id(self) -> str:
        """The game to play: the one given, or the only live one."""
        if self._game_id is None:
            games = self.http.get("/api/games").json()["games"]
            if not games:
                raise RuntimeError("no live game on the API - start one first")
            self._game_id = games[0]["game_id"]
        return self._game_id

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> str:
        """One API call; the answer as text, HTTP errors included as text.

        Every call carries this seat's uid as a query parameter. The server
        is authoritative for hidden information (`visibility.py`): any answer
        that carries a state view is already filtered to this uid by the time
        it gets here. This only trims image URLs, pure token weight with no
        bearing on what may be seen.
        """
        try:
            response = self.http.request(method, path, json=body, params={"uid": self.uid})
            try:
                parsed = response.json()
            except ValueError:
                return f"HTTP {response.status_code}\n{response.text}"
            if isinstance(parsed, dict):
                _trim_images(parsed)
            return f"HTTP {response.status_code}\n{json.dumps(parsed, indent=1, sort_keys=True)}"
        except _AMBIGUOUS_TIMEOUTS as exc:
            return f"HTTP TIMEOUT\n{UNKNOWN_OUTCOME} ({exc})"
        except httpx.HTTPError as exc:
            return f"could not reach the API: {exc}"

    def call_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Like `call`, but the parsed JSON body rather than text - what
        `run_action` needs to inspect whether an action was refused.

        A timeout that arrives *after* the request was sent
        (`_AMBIGUOUS_TIMEOUTS`) is reported the same shape as an ordinary
        refusal (`{"allowed": False, ...}`) so every existing caller's
        dict-merging keeps working, but carries `unknown_outcome: True` so
        `run_action` can tell "the server said no" apart from "we never
        heard back" and word its answer accordingly - see `UNKNOWN_OUTCOME`.
        A connection failure *before* anything was sent is not this kind of
        ambiguity (nothing to have landed) and is left to raise, same as
        before, for `_guarded`/`api_trouble` to report as "unreachable".
        """
        try:
            response = self.http.request(method, path, json=body, params={"uid": self.uid})
        except _AMBIGUOUS_TIMEOUTS as exc:
            return {
                "allowed": False,
                "reason": f"{UNKNOWN_OUTCOME} ({exc})",
                "unknown_outcome": True,
            }
        try:
            return response.json()
        except ValueError:
            return {"allowed": False, "reason": f"HTTP {response.status_code}: {response.text}"}

    def fetch_state(self) -> dict[str, Any]:
        """This seat's filtered view of the game, right now (`my_state`'s
        answer, minus image trimming duplication - callers that print it do
        that themselves)."""
        state = self.http.get(f"/api/games/{self.game_id()}", params={"uid": self.uid}).json()
        _trim_images(state)
        return state

    def my_untapped_sources(self, state: dict[str, Any]) -> list[str]:
        """Every untapped mana source I control, the set the UI offers too.

        Offering all of them lets the engine tap only what the cost needs
        (its payment stops at the shortfall); an agent may also pass an
        explicit, smaller set of source ids to any mana-paying tool.
        """
        return [
            card["id"]
            for card in state["battlefield"]
            if card["controller"] == self.player
            and not card["tapped"]
            and any(ability["mana"] for ability in card["abilities"])
        ]


class Seats:
    """The door keeper of the single toolset: uid -> the seat it opens.

    Uids are checked against the game itself (GET /api/games/{id}/seat), so
    this process holds no secrets and claiming a seat is impossible without
    the uid given out at dispatch.
    """

    def __init__(self, api: str, game_id: str | None) -> None:
        self.api = api.rstrip("/")
        #: `--game` pins this process to one id for its whole life - never
        #: re-discovered. `None` means "whatever's live", re-checked below on
        #: every call rather than cached after the first lookup: this process
        #: is spawned once for a whole session, but a playtest loop boots a
        #: brand new game (brand new id, brand new seat uids) once per game,
        #: so caching the id forever used to leave a long-lived MCP process
        #: silently pointed at a game the API no longer had, indistinguishable
        #: from the process itself being dead.
        self._pinned_game_id = game_id
        self._last_seen_game_id: str | None = None
        self._by_uid: dict[str, str] = {}
        # Bootstrap with a short, fixed timeout for the one health probe
        # `resolve_client_timeout` needs, then widen it to the derived value
        # for every real call this client makes from here on - resolved
        # once for this whole process's life, not per `Seat` (`seat_for`
        # hands the same value to every `Seat` it mints, below).
        self.http = httpx.Client(base_url=self.api, timeout=BOOTSTRAP_TIMEOUT_SECONDS)
        # A bare float, not `httpx.Timeout(...)`: tests monkeypatch
        # `httpx.Client` itself to hand back a `TestClient` (built on the
        # unrelated `httpx2` package here), so anything constructed through
        # `httpx.Client(...)` is safely swallowed by that patch - but this
        # assignment reaches the concrete instance directly, and handing it
        # a real `httpx.Timeout` object (a foreign type to `httpx2`) breaks
        # that client's own `__repr__`. A float is what every implementation
        # of `.timeout =` already accepts and normalises itself.
        self.http.timeout = resolve_client_timeout(self.http)

    def game_id(self) -> str:
        """The game to play: the one pinned via `--game`, or whichever the
        API currently reports live. In the unpinned case this re-asks the API
        every time rather than caching the answer - see `__init__`. When the
        live id has changed since the last call, `_by_uid` is dropped too: a
        new boot hands out brand new uids, and without this an old, cached
        uid would keep resolving to a player name and now be pointed at the
        new game it was never actually issued for."""
        if self._pinned_game_id is not None:
            return self._pinned_game_id
        games = self.http.get("/api/games").json()["games"]
        if not games:
            raise RuntimeError("no live game on the API - start one first")
        current = games[0]["game_id"]
        if current != self._last_seen_game_id:
            self._by_uid.clear()
            self._last_seen_game_id = current
        return current

    def seat_for(self, uid: str) -> Seat | None:
        """The seat this uid opens, or None when the uid is nobody's."""
        game_id = self.game_id()
        player = self._by_uid.get(uid)
        if player is None:
            response = self.http.get(f"/api/games/{game_id}/seat", params={"uid": uid.strip()})
            if response.status_code != 200:
                return None
            player = response.json()["player"]
            self._by_uid[uid.strip()] = player
        return Seat(player, uid.strip(), self.api, game_id, timeout=self.http.timeout)


def _trim_images(state: dict[str, Any]) -> None:
    """Drop image URLs off every card in a state-shaped dict, in place.

    Pure token-weight trimming for the agent - an agent cannot look at
    pictures - with no bearing on what may be seen: the server has already
    decided that (`visibility.py`) by the time a response gets here.
    """
    for key in ("battlefield", "stack", "exile", "command"):
        entries = state.get(key)
        if isinstance(entries, list):
            for entry in entries:
                _strip_card_weight(entry)
    for key in ("hands", "graveyards"):
        entries = state.get(key)
        if isinstance(entries, dict):
            for cards in entries.values():
                for entry in cards or []:
                    _strip_card_weight(entry)


def _strip_card_weight(entry: dict[str, Any]) -> None:
    """Drop per-card fields an agent has no use for (image URLs)."""
    if isinstance(entry, dict):
        for field in ("image", "small_image", "art_crop"):
            entry.pop(field, None)


def _parse_ids(value: str) -> list[str]:
    """A comma-separated list of card ids, or a JSON list."""
    value = value.strip()
    if not value:
        return []
    if value.startswith("["):
        return [str(entry) for entry in json.loads(value)]
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_pairs(value: str) -> list[dict[str, str]]:
    """A JSON list of {"card": ..., "defender"/"blocked": ...} objects."""
    value = value.strip()
    if not value:
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError('give a JSON list, e.g. [{"card": "...", "defender": "bob"}]')
    return parsed


# -- the "one call, one action" flow ----------------------------------------


def _board_signature(state: dict[str, Any]) -> Any:
    """A fingerprint of everything about the board this seat may see, used
    only to tell "the board changed since the call began" apart from "priority
    bounced straight back with nothing new". Two filtered views compare equal
    under this exactly when nothing visible to this seat differs."""
    return {key: value for key, value in state.items() if key != "wake_reason"}


def compute_wake_reason(
    state: dict[str, Any], before: dict[str, Any] | None, player: str
) -> str | None:
    """Which wake condition holds for `player` in `state`, or `None`.

    `before` is this seat's filtered view captured when the call began (or
    `None` for a caller that never had one, e.g. a bare status poll) - see the
    module docstring for why "you have priority" alone is not enough.
    """
    pending = state.get("pending_decision")
    if state.get("winner") or state.get("drawn"):
        return "game over"
    if pending is not None and pending.get("player") == player:
        return f"a decision is waiting for you: {pending.get('prompt')}"
    if (
        state.get("step") in ("untap", "cleanup")
        and state.get("active_player") == player
        and not state.get("stack")
    ):
        return "nobody gets priority in this step and it is your turn: advance the game"
    if state.get("stack") and state.get("priority_holder") == player:
        # The engine refuses `advance` outright while the stack is non-empty
        # (CR 500.2, 117.4) - steer towards the one action that is ever legal
        # here, rather than let a player whose `pass` was refused for some
        # other reason fall back to `advance` and bounce off that refusal too.
        return "the stack is non-empty and you have priority: pass_priority to let it resolve"
    if state.get("priority_holder") == player and (
        before is None or _board_signature(state) != _board_signature(before)
    ):
        return "you have priority and the board has changed"
    return None


def block_until_wake(
    seat: Seat,
    before: dict[str, Any] | None,
    max_seconds: float,
    poll_seconds: float = POLL_SECONDS,
) -> dict[str, Any]:
    """Poll this seat's own state until a wake condition holds, or time out.

    Never touches `store.locked` - see the module docstring's "why this
    cannot deadlock". Always returns within `max_seconds` (a "still waiting"
    answer, never an exception, never a silent infinite hang): the timeout
    wake reason tells a caller to call again rather than leaving it guessing.
    """
    deadline = time.monotonic() + max(0.0, max_seconds)
    while True:
        state = seat.fetch_state()
        reason = compute_wake_reason(state, before, seat.player)
        if reason is not None:
            return {"state": state, "wake_reason": reason}
        if time.monotonic() >= deadline:
            return {
                "state": state,
                "wake_reason": f"timed out after {max_seconds}s - call again to keep waiting",
            }
        time.sleep(poll_seconds)


def available_actions(seat: Seat, state: dict[str, Any]) -> dict[str, Any]:
    """What this seat can actually do right now, using the engine's own
    dry-run legality checks (`cast`/`activate` with `dry_run: true`) - never
    new rules logic here.

    Only ever looks at `state["hands"][seat.player]` and battlefield
    permanents `state` already says this seat controls - both already
    filtered by `visibility.py` before this function ever sees them - so this
    cannot surface the opponent's hand or either library even if a caller
    passed an unfiltered view by mistake: there would be nothing under the
    opponent's name to read to begin with (their hand is `[]` server-side,
    and libraries are dropped entirely).

    One extra HTTP round trip per hand card and per abililty on a controlled
    permanent - each is a dry run the engine already rewinds, so this is
    "expensive" only in the sense of doing an ordinary legality check for
    everything narrow enough to check, not in the sense of costing anything
    the API does not already pay for a single such check. A hand and a board
    are both small (single digits), so this stays cheap; it is not run inside
    the poll loop above, only once, right before a call returns.

    Skips every dry run - and returns empty - when `state["pending_decision"]`
    is addressed to this seat, and for a reason sharper than "why bother, the
    engine has frozen mid-resolution anyway": while that decision is
    outstanding nothing else is legal (whichever CR governs it - see
    `decisions.rule_for_kind`), so an honest legality check would say no to
    all of them regardless. The `cast`/`activate` dry runs below
    each take `store.locked(game_id)` (`app.py`), and that lock is exactly
    what is held by the request that raised this decision, parked inside
    `Game.ask` waiting on this seat's own answer (`POST .../decide`, which
    deliberately never takes that lock - see `decisions.py`'s module
    docstring). Probing anyway would queue this call behind the very decision
    it exists to let the seat answer, for the full decision timeout - the
    deadlock this function used to cause on every wake into a pending
    decision, traced live: 18 decision timeouts in one game, because
    `run_action` calls this on every return path (see the module docstring).
    """
    pending = state.get("pending_decision")
    if pending is not None and pending.get("player") == seat.player:
        return {
            "castable": [],
            "activatable": [],
            "attackers_requested": False,
            "blockers_requested": False,
            "pending_decision": pending,
        }

    game_id = seat.game_id()
    sources = seat.my_untapped_sources(state)

    def dry_run(path: str, body: dict[str, Any]) -> bool:
        body = {**body, "dry_run": True}
        if sources:
            body["mana_sources"] = sources
        try:
            answer = seat.call_json("POST", path, body)
        except httpx.HTTPError:
            return False
        return bool(isinstance(answer, dict) and answer.get("allowed"))

    castable = [
        card["id"]
        for card in state.get("hands", {}).get(seat.player, []) or []
        if dry_run(
            f"/api/games/{game_id}/actions/cast",
            {"player": seat.player, "card_id": card["id"]},
        )
    ]

    activatable = []
    for card in state.get("battlefield", []) or []:
        if card.get("controller") != seat.player:
            continue
        for ability in card.get("abilities", []) or []:
            if dry_run(
                f"/api/games/{game_id}/actions/activate",
                {
                    "player": seat.player,
                    "card_id": card["id"],
                    "ability_index": ability["index"],
                },
            ):
                activatable.append({"card_id": card["id"], "ability_index": ability["index"]})

    step = state.get("step")
    is_active = state.get("active_player") == seat.player
    return {
        "castable": castable,
        "activatable": activatable,
        "attackers_requested": step == "declare_attackers" and is_active,
        "blockers_requested": step == "declare_blockers" and not is_active,
        "pending_decision": state.get("pending_decision"),
    }


def _refused(outcome: Any) -> bool:
    return isinstance(outcome, dict) and outcome.get("allowed") is False


def _unknown_outcome(outcome: Any) -> bool:
    """Whether `outcome` is `call_json`'s ambiguous-timeout marker, not an
    ordinary rules refusal - see `UNKNOWN_OUTCOME`. Checked before
    `_refused`: both shapes carry `allowed: False`, but only this one means
    "we do not know what happened", and conflating them would tell a player
    "nothing changed, still your move" about a call that may well have gone
    through."""
    return isinstance(outcome, dict) and outcome.get("unknown_outcome") is True


#: Marker `_call_watching_for_self_decision` returns in place of an outcome
#: when it gave up on `perform` because a decision addressed to this same
#: seat showed up first - see that function's docstring for why this is not
#: an ordinary refusal or an `UNKNOWN_OUTCOME` (both of those mean the call
#: itself is done, one way or another; this one is still running).
SELF_DECISION_IN_FLIGHT = "SELF_DECISION_IN_FLIGHT"


def _call_watching_for_self_decision(
    seat: Seat, perform: Any, poll_seconds: float = POLL_SECONDS
) -> tuple[Any, dict[str, Any] | None]:
    """Run `perform` (one mutating call) on a background thread while polling
    this seat's own state for a decision addressed to it, so a decision that
    call itself raises *for this same seat* - CR 608.2d mid-resolution, or CR
    603.3d/601.2c putting one of this seat's own triggered abilities on the
    stack - does not sit unanswerable for the server's whole decision timeout.

    Why a decision like that is otherwise unanswerable: `POST .../decide`
    never takes `store.locked(game_id)` (`decisions.py`'s module docstring),
    so a *different* seat's own poll (`block_until_wake`) notices and answers
    a decision addressed to it promptly, well within the timeout - that path
    already works. But a real caller here is one MCP tool call from one
    client with exactly one request in flight at a time (`mcp.py`'s module
    docstring: "one call, one meaningful action"); it cannot send `decide`
    until `perform`'s own HTTP response comes back, and that response will
    not come back until `Game.ask`, deep inside the very request `perform`
    is waiting on, gets an answer. Nothing server-side can fix this - the
    lock was never the problem for this seat, its own single-threaded
    transport is - so the fix has to add a second, concurrent connection on
    the client side, which is what this function is.

    Returns `(outcome, None)` when `perform` finished before any such
    decision showed up - the ordinary case, including an ordinary refusal or
    a decision addressed to someone else, which this never reacts to (that
    seat's own poll is what answers those, unaffected by this one running
    concurrently). Returns `(SELF_DECISION_IN_FLIGHT, pending_decision)` when
    a decision naming this seat appeared first: `perform`'s own HTTP call is
    abandoned here - not aborted, since a request already sent may already be
    landing server-side - on a daemon thread that finishes or times out on
    its own with nobody left watching. The caller told about the decision
    this way has to answer it (`decide`) as a genuinely separate request
    before checking state or the game log for what the abandoned call itself
    ended up doing.
    """
    done = threading.Event()
    box: dict[str, Any] = {}

    def call() -> None:
        try:
            box["outcome"] = perform()
        except Exception as exc:  # re-raised below only if nobody moved on
            box["exception"] = exc
        finally:
            done.set()

    threading.Thread(target=call, daemon=True).start()
    while not done.wait(poll_seconds):
        state = seat.fetch_state()
        pending = state.get("pending_decision")
        if pending is not None and pending.get("player") == seat.player:
            return SELF_DECISION_IN_FLIGHT, pending
    if "exception" in box:
        raise box["exception"]
    return box["outcome"], None


def run_action(
    seat: Seat,
    perform: Any,
    *,
    hold_priority: bool = False,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    release: bool = True,
) -> dict[str, Any]:
    """Do one action, then behave the way both transports promise: unless
    told to hold, release priority, then block until this seat next has
    something to do - and always return the action's own outcome alongside
    whatever state it woke into, in one answer.

    `perform` is a zero-argument callable performing the actual HTTP call and
    returning its parsed JSON body. `release` is `False` only for `pass`
    itself (the action already *is* releasing priority - calling
    pass-priority a second time on top of it would just be refused, harmlessly,
    but there is no reason to make the extra request).

    Refusal and `hold_priority` both return **immediately, never blocking**:
    a refused action leaves this seat exactly where it was (still its move,
    try something else), and `hold_priority=True` must never block by
    construction - the check happens before `block_until_wake` is ever
    reached, not by timing or luck.

    `perform` itself, and the auto-pass below, both go through
    `_call_watching_for_self_decision` rather than being called directly:
    either can cross a decision addressed to this very seat (an ETB trigger
    the seat itself must target, the cleanup discard closing its own turn,
    ...), which - unlike a decision addressed to someone else - this seat's
    own single in-flight call could never otherwise answer. See that
    function's docstring for the full shape of why.
    """
    before = seat.fetch_state()
    outcome, self_decision = _call_watching_for_self_decision(seat, perform)

    if self_decision is not None:
        state = seat.fetch_state()
        return {
            "outcome": None,
            "held_priority": None,
            "wake_reason": (
                "a decision is waiting for you, raised by this very call: "
                f"{self_decision.get('prompt')} - answer it with decide() now; "
                "that unblocks the action you just took, whose own outcome you "
                "will only see afterward, from state or the game log"
            ),
            "state": state,
            "available_actions": available_actions(seat, state),
            "in_flight_action": True,
        }

    if _unknown_outcome(outcome):
        # A transport timeout, not a rules refusal: re-fetch state (a fresh
        # request - reads never take the game lock, see `block_until_wake`'s
        # docstring, so this is safe even if the timed-out call is still
        # parked server-side) so the answer at least carries the truth as of
        # right now, but word the reason honestly rather than as a refusal -
        # `held_priority` is `None` here on purpose: unlike the other
        # branches, this call cannot say whether it does or does not hold
        # priority, because it does not know whether the action happened.
        state = seat.fetch_state()
        return {
            "outcome": outcome,
            "held_priority": None,
            "wake_reason": UNKNOWN_OUTCOME,
            "state": state,
            "available_actions": available_actions(seat, state),
        }

    if _refused(outcome):
        state = seat.fetch_state()
        return {
            "outcome": outcome,
            "held_priority": True,
            "wake_reason": "refused - nothing changed, still your move",
            "state": state,
            "available_actions": available_actions(seat, state),
        }

    if hold_priority:
        state = seat.fetch_state()
        return {
            "outcome": outcome,
            "held_priority": True,
            "wake_reason": "hold_priority=true: kept priority, not blocking",
            "state": state,
            "available_actions": available_actions(seat, state),
        }

    pass_outcome = None
    if release:

        def perform_pass() -> Any:
            return seat.call_json(
                "POST",
                f"/api/games/{seat.game_id()}/actions/pass-priority",
                {"player": seat.player},
            )

        pass_outcome, pass_self_decision = _call_watching_for_self_decision(seat, perform_pass)
        if pass_self_decision is not None:
            # Releasing priority - not the action above, which already came
            # back clean - is what crossed a decision only this seat could
            # ever answer (`put_triggers_on_stack` runs at the top of every
            # `pass_priority`, CR 603.3). Same shape as the check on `perform`
            # itself: report it and let the caller `decide()` as a separate
            # call rather than sit behind its own in-flight pass.
            state = seat.fetch_state()
            return {
                "outcome": outcome,
                "held_priority": None,
                "wake_reason": (
                    "your action went through, but releasing priority raised a "
                    f"decision only you can answer: {pass_self_decision.get('prompt')} - "
                    "answer it with decide() now; check state or the game log "
                    "afterward for what releasing priority itself then did"
                ),
                "state": state,
                "available_actions": available_actions(seat, state),
                "in_flight_action": True,
            }
        if _unknown_outcome(pass_outcome):
            # The action itself is confirmed (we are past that check above);
            # it is *releasing priority* that is now ambiguous. Blocking
            # further would silently assume an answer this call does not
            # have - the resolving wake_reason from `block_until_wake` could
            # easily be "you have priority", which is simply not knowable
            # yet.
            state = seat.fetch_state()
            return {
                "outcome": outcome,
                "pass_outcome": pass_outcome,
                "held_priority": None,
                "wake_reason": UNKNOWN_OUTCOME,
                "state": state,
                "available_actions": available_actions(seat, state),
            }

    woken = block_until_wake(seat, before, max_seconds)
    result: dict[str, Any] = {
        "outcome": outcome,
        "held_priority": False,
        "wake_reason": woken["wake_reason"],
        "state": woken["state"],
        "available_actions": available_actions(seat, woken["state"]),
    }
    if pass_outcome is not None:
        result["pass_outcome"] = pass_outcome
    return result
