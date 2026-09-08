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

The convenience is only ever right for an action that *uses the stack*, where
releasing is how the thing you just did gets to resolve. Two families of
action do not use the stack, and auto-passing after one of them costs the
seat the rest of its own phase for nothing:

- **special actions** (CR 116.1: "special actions are actions a player may
  take when they have priority that don't use the stack"), which CR 116.3
  answers directly - "if a player takes a special action, that player receives
  priority afterward". Both transports therefore default those tools to
  holding: `play_land` (CR 116.2a), `suspend` (CR 116.2f), `plot` (CR 116.2k),
  `foretell` (CR 116.2h). `cycle` holds with them, though cycling is *not* a
  special action - CR 702.29a makes it an ordinary activated ability, so its
  warrant is CR 117.3c rather than CR 116.3.
- **mana abilities** (CR 605.3b: an activated mana ability "doesn't go on the
  stack ... it resolves immediately after it is activated"). `activate` cannot
  default on that, since it is one tool for every ability and only the answer
  says which kind it was, so `run_action` holds on the outcome instead - see
  `added_mana`.

## Wake conditions

A blocked call returns as soon as one of these holds, for this seat:

- the game is still in the pregame - CR 103.5's mulligan step or CR 103.6's
  opening-hand step, which a game booted from decklists rests in before
  CR 103.8's first turn. Nobody has priority there, so every other condition
  below is unreachable and a seat waiting for one would wait out its whole
  bound while the game waited on it;
- it has priority *and* the visible board changed since the call began (not
  merely "has priority" - that would also fire the instant this seat's own
  action hands priority right back with nothing new to react to);
- a pending decision (CR 608.2d for most kinds; a `choose_targets` decision is
  CR 603.3d/601.2c instead, `declare_attackers`/`declare_blockers` CR 508.1/
  509.1) is addressed to it;
- the game is over (a winner is set, or it is a draw);
- the step gives nobody priority (untap, cleanup) and this seat is the active
  player - the harness has no other way to make the turn proceed but calling
  `advance`.

## Why this cannot deadlock

`block_until_wake` polls `GET /api/games/{id}` - a read that takes no lock
beyond what that endpoint already takes internally, and releases it before
returning. It never holds `store.locked(game_id)` (`store.py`) across the
poll loop. `hold_priority=True` additionally guarantees the *action* path
never enters `block_until_wake` at all: the check happens before the loop is
ever reached, by construction, not by timing.

This used to be wrong in one more place, past the loop, in a shape specific
to the old blocking decision transport: every return path of `run_action`
called `available_actions(seat, state)` unconditionally, and *that* function
issued `POST .../actions/cast?dry_run=true` for every card in hand (and
`.../activate` for every ability) - each of which takes `store.locked
(game_id)` (`app.py`). When this seat woke because a decision was now
pending *for it*, that lock used to not be free: it was held by the
opponent's request, parked deep inside `Game.ask` waiting on this same seat
to answer through `POST .../decide`. Calling `available_actions` there
queued this call behind the very decision it was returning to let the seat
answer, for the full decision timeout - a real deadlock, not a wait.

T11 removed the dry-run probe entirely: `available_actions` is now one `GET
.../legal-actions` call onto `machines.legal_actions.legal_actions` (Phase
1), which takes no lock at all - a pure read against the committed `Game`,
same as `GET /api/games/{id}` itself. The old deadlock shape cannot recur
because there is no longer a mutating request in this path to queue behind.
Whether a decision is pending for this seat is still read once, by the
server, before it enumerates anything - while one is outstanding, the answer
is that decision's own legal answers and nothing else (`legal_actions`'s own
module docstring) - which is correctness, not deadlock avoidance: nothing
else is legal while a decision stands, regardless of any lock.

## No transport-level block left to outlast

`block_until_wake` does one quick read and returns; so does `pass_until`,
which used to be the last deadline loop here (it slept until `max_seconds`,
so one MCP tool call sat open for as long as the opponent took to think).
`perform()` - one mutating call (`cast`/`activate`/`attack`/`pass-priority`/
`resolve-top`/`advance`) - used to be able to cross a live decision question
and block *inside that one request*, in `Game.ask`, for up to the server's
own `decision_timeout_seconds`; `Seat`'s HTTP client timeout used to be sized
to comfortably outlast that (`resolve_client_timeout`, since removed - see
`CLIENT_TIMEOUT_SECONDS`). That hazard is gone: a question with no scripted
answer now pauses and returns immediately instead of blocking the request at
all, so there is no long server-side hold for a client timeout to be shorter
than. `Seat.call_json` still reports a genuine read/write/pool timeout as
`UNKNOWN_OUTCOME` rather than raising - ordinary network jitter can still
happen - so a caller is never tempted to retry blind and risk a duplicate
cast/activation/attack.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

import httpx

DEFAULT_API = "http://127.0.0.1:8000"

#: Bumped whenever an action tool's contract changes shape. Every tool call in
#: `mcp.py` checks it against its own import of this constant (`api_trouble`,
#: below) and refuses that one call - loudly, naming both versions - on a
#: mismatch against whatever `/api/health` reports. Exists because the MCP
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
#: 2b T11: this used to be the value `/api/health` answered with too - the
#: api imported it straight out of this module, backwards (the api must not
#: depend on the future `magic-cards-mcp` package). The api now owns its own
#: copy (`magic_cards_model_api.protocol.PROTOCOL_VERSION`); this one stays,
#: independently, for exactly the comparison above - it has to be a value
#: baked into this build of the MCP process, not one read at the moment of
#: the check, or the check would trivially always agree with itself. The two
#: literals are kept equal by hand from here on - there is no import left to
#: do it for free once the repos actually split - the same way
#: `agent_settings.py`'s cross-repo spellings already are (MEMORY.md,
#: 2026-09-08). `tests/test_protocol_boundary.py` pins the two together.
#:
#: Bump this alongside any change to a tool's request/response shape.
PROTOCOL_VERSION = "seatkit-8"
#: `seatkit-8` added `sacrificing`/`discarding`/`exiling` to the `activate`
#: tool/CLI command, closing the gap a live game found: an activated ability
#: whose own cost names an object (Goblin Bombardment's "Sacrifice a
#: creature: ...") had no way to be paid at all, because
#: `ActivateRequest.sacrificing`/`.discarding` already existed on the request
#: schema (`engine_api.activate_ability` already threaded them into
#: `Game.begin_activating`) but neither seat surface exposed a way to set
#: them - every attempt failed with the engine's own honest, unreachable
#: refusal, "sacrifice needs 1 card(s) chosen, 0 given". A request-shape
#: change to a widely-used tool, per this constant's own rule: an old MCP
#: process built before this addition has no `sacrificing` parameter to
#: forward even if a newer prompt asked it to use one, so the version bump
#: is what turns that silent gap back into the PROTOCOL MISMATCH sentence
#: rather than a call that quietly omits the field again.
#:
#: `seatkit-7` replaced the `my_state` tool with `battlefield_state`, which
#: answers the same whole board when called with nothing but a uid and takes
#: two options to narrow it: `sections` (a comma-separated list of the state
#: view's own top-level keys) and `side` (`both`/`mine`/`opponent`, resolved
#: from the caller's own uid). The old name is gone outright rather than kept
#: as an alias, and this bump is what makes that safe to do: an MCP process
#: built before it serves `my_state` and has no `battlefield_state` at all,
#: so an agent driven by a prompt from after it would call a tool that does
#: not exist and read "Unknown tool" - a failure with no name on it. With the
#: bump, that same stale process refuses every call up front with the
#: PROTOCOL MISMATCH sentence naming which side is stale, which is exactly
#: what this check is for. An alias would not have helped either direction:
#: the stale process is stale in its *own* code, and nothing added here
#: reaches it (this process is spawned once and never reloads - see `mcp.py`).
#:
#: `seatkit-6` added the `declare_plan` tool/method (`POST .../plan`,
#: `schemas.DeclarePlanIn`). This one is not an optional-degrade case the way
#: earlier bumps mostly were (an older client missing a nicety, still able to
#: play): the backend gate this pays for, `records.require_plan`, already
#: refuses an agent seat's first mutating action of its own turn until a
#: `plan_declared` record exists for that turn - and it shipped *before* any
#: client could call the route that satisfies it. An MCP process built before
#: this bump has no `declare_plan` tool at all, so an agent seat driven by it
#: is refused, 422, on the very first action of every one of its own turns,
#: forever - not a degraded seat, a seat that cannot play. That is exactly
#: the silent-misbehaviour case this version check exists to turn into a
#: loud, named refusal instead of a mystery.
#:
#: `seatkit-5` put a `recent_log` on every answer that already carried a
#: state view (`my_state`, as `battlefield_state` was then called,
#: `wait_for_priority`, every action tool through `run_action`, `pass_until`)
#: and gave the `game_log` tool the `since` parameter the api's own
#: `GET .../log` has always had. "What happened since I last acted" and "what
#: can I do now" used to be two tool calls and two whole inference cycles -
#: `prompt.py` said so outright ("read `my_state`, read `game_log`") - and
#: they are one now. An MCP process from before this
#: simply does not carry the log, which reads to a seat as a game where
#: nothing ever happened between its own moves.
#:
#: `seatkit-4` (T10) gave every mutating tool a `reasoning` parameter - "why
#: are you doing this", in the seat's own words - required by the API for an
#: agent seat (refused 422 when it is missing) and optional for a human one.
#: An MCP process from before this omits the parameter entirely, so an agent
#: seat it drives would have every mutating call refused outright rather than
#: silently accepted with no reasoning recorded.
#:
#: `seatkit-3` removed `max_seconds` from every tool and command that carried
#: it - nothing blocks any more, so nothing was left for it to bound, and a
#: parameter that bounds nothing is a promise an MCP client cannot see
#: corrected (it receives no `description` on any parameter). It also gave
#: `pass_priority(until=...)`/`pass --until` a `resume` token, because that
#: run is poll-and-return now and spans several calls, and made that path
#: return `input_required` when it stops on a decision. An MCP process from
#: before this passes `max_seconds=` to tools that no longer take it.
#:
#: `seatkit-2` added the pregame tools (`mulligan`, `opening_hand_action`) and
#: the `pregame` block every state view now carries: a game booted from
#: decklists rests in CR 103.5's mulligan step until its seats declare, and an
#: MCP process from before this cannot drive one out of it - which looks, from
#: the seat's side, exactly like a game that never starts. Better a refusal
#: naming both versions.

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

NO_SUCH_SEAT = (
    "that uid is no seat in this game. You cannot act, and you may see nothing - "
    "a seat uid is given to you when you are dispatched."
)

#: How many log entries ride along on an answer that already carries a state
#: view. A tail, not the history: `game_log` is still the tool for the whole
#: thing, and `recent_log["omitted"]` says when this tail dropped something so
#: a seat is never quietly told less than happened.
RECENT_LOG_ENTRIES = 15

#: The fields a ride-along log entry keeps. Deliberately NOT the whole entry:
#: `audit.record` stamps every row with a full-information `snapshot` of the
#: board and with `legal_actions_before`, the enumerated action pool of that
#: moment - so fifteen whole entries would be fifteen more board states and
#: fifteen more action pools bolted onto an answer that already carries the
#: current one of each. That is not a token-trimming judgement (payload size
#: buys no latency here); it is what a log *tail* is for. A seat asking "what
#: has happened since I last acted" is asking for the narrative, and the
#: snapshots are still one `game_log` call away, unchanged.
LOG_HEADLINE_FIELDS = ("seq", "ts", "action", "detail", "turn", "step", "reasoning")


def log_headline(entry: dict[str, Any]) -> dict[str, Any]:
    """One log entry as the narrative line a ride-along tail carries - the
    `LOG_HEADLINE_FIELDS` that are present, and nothing invented for one that
    is not."""
    return {field: entry[field] for field in LOG_HEADLINE_FIELDS if field in entry}


def _log_unavailable(since: int, reason: str) -> dict[str, Any]:
    """`recent_log`'s answer when the call itself failed: an empty tail that
    says so, rather than one a reader would take for "nothing happened"."""
    return {
        "entries": [],
        "last_seq": since,
        "omitted": 0,
        "log_error": (
            f"{reason} - this is NOT 'nothing has happened': the log could not be "
            "fetched at all. Call game_log, or call again."
        ),
    }


#: How often `_call_watching_for_self_decision` re-fetches state while one
#: mutating call of its own is genuinely in flight on another thread - the
#: only loop in this module that still polls at all, and it polls for the
#: duration of a request it is already waiting on rather than for anything to
#: happen at the table. Small: it has no condition variable to be woken by,
#: only "check the board again". Nothing else here sleeps: `block_until_wake`,
#: `wait_for_priority` and `pass_until` are all poll-and-return.
POLL_SECONDS = 0.2

#: The HTTP client read timeout every `Seat`/`Seats` builds with.
#:
#: This used to be derived from the server's own worst-case block
#: (`resolve_client_timeout`, since removed): a mutating call
#: (`cast`/`activate`/`attack`/`pass-priority`/`resolve-top`/`advance`)
#: crossing a live decision question used to block *inside that one
#: request*, in `Game.ask`, for up to the server's own
#: `decision_timeout_seconds` - and a client-side timeout shorter than that
#: turned a legitimate hold into a false "the action failed". That hazard is
#: gone, not merely mitigated: no mutating request blocks on a live decision
#: any more (`commands.py`'s pause-and-resume) - a question with no scripted
#: answer returns immediately instead of parking the request - so there is
#: no longer a server-side block for a client timeout to be shorter than.
#: `CLIENT_TIMEOUT_SECONDS` is just a generous, fixed bound against ordinary
#: network jitter and a slow deployment, the same as any other HTTP client.
CLIENT_TIMEOUT_SECONDS = 30.0

#: Shown in place of a raised exception whenever a request genuinely times
#: out waiting for an answer (`httpx.ReadTimeout`/`WriteTimeout`/`PoolTimeout`
#: - a request that was sent, unlike a bare connection failure). The action
#: may already have been performed server-side, so this must never read like
#: an ordinary refusal (`run_action` checks for it before its normal
#: "refused" branch) and must never invite a blind retry, which is exactly
#: the duplicate-action hazard this exists to prevent.
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


def _reason_from_detail(detail: Any) -> str:
    """A human sentence out of whatever shape a non-2xx body's `detail`
    carries. Every exception handler in `app.py` writes `detail` as a plain
    string except the two shaped like a pydantic validation error
    (`RequestValidationError`, and `records.MissingReasoning`'s own handler,
    which mirrors that shape on purpose) - `detail: [{"loc", "msg", "type"},
    ...]`. The `msg` in there is the actual sentence a human wrote (an agent
    seat's own missing-`reasoning` explanation, say) - what a caller needs,
    not the list wrapping it, which is what `str(detail)` alone would hand
    back."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        msgs = [
            str(error["msg"]) for error in detail if isinstance(error, dict) and error.get("msg")
        ]
        if msgs:
            return "; ".join(msgs)
    return str(detail)


def _normalise_non_2xx(status_code: int, parsed: Any, text: str) -> dict[str, Any]:
    """A non-2xx response body, turned into the same tri-state-safe shape
    every `call_json` caller already checks with `_refused`/`_paused`/
    `_unknown_outcome` - never left in whatever raw shape the failing
    handler happened to write, which is exactly what let a 422 read as a
    completed action (see `call_json`'s own docstring).

    A body that already carries its own `allowed`
    (`commands.DecisionInProgressError`'s 409, `app.py`) is trusted and
    returned unchanged - it is already the shape this function would
    otherwise build. Every other 4xx is a request the server never acted on
    at all (refused before any command ran, same as `records.
    require_reasoning`'s 422), so it becomes an ordinary refusal, `reason`
    pulled out of whatever `detail` shape the body carried.

    A 5xx is the one status class kept out of that: unlike a 4xx, the
    request may genuinely have started running server-side before the
    server itself failed partway through, so this is not the same claim a
    4xx refusal makes ("nothing happened") - it gets the same honest
    `unknown_outcome` shape an ambiguous transport timeout already does, see
    `UNKNOWN_OUTCOME`, rather than a refusal a caller would wrongly trust as
    "still your move"."""
    base = parsed if isinstance(parsed, dict) else {}
    if "allowed" in base:
        return base
    reason = (
        _reason_from_detail(base["detail"]) if "detail" in base else f"HTTP {status_code}: {text}"
    )
    if status_code >= 500:
        return {
            **base,
            "allowed": False,
            "reason": f"{UNKNOWN_OUTCOME} (HTTP {status_code}: {reason})",
            "unknown_outcome": True,
        }
    return {**base, "allowed": False, "reason": reason}


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
        #: `timeout` is normally `Seats.http.timeout` - `Seats.seat_for`
        #: passes it down so the many `Seat` objects it mints all share one
        #: value. `None` (a `Seat` built directly, outside `Seats`) falls
        #: back to the same fixed default.
        if timeout is None:
            timeout = CLIENT_TIMEOUT_SECONDS
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

        **Every non-2xx response is normalised too** (`_normalise_non_2xx`) -
        this used to return `response.json()` regardless of status, so a 422
        from `records.require_reasoning` (a well-formed request refused
        before it ever reached the engine) came back as FastAPI's bare
        validation shape, `{"detail": [...]}`: no `allowed`, no `paused`, no
        `unknown_outcome`, which `_refused`/`_paused`/`_unknown_outcome` all
        missed and `run_action` read as a completed action. See this
        module's own defect writeup (2026-09-08) for the live reproduction.
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
            parsed = response.json()
        except ValueError:
            parsed = None
        if response.status_code >= 400:
            return _normalise_non_2xx(response.status_code, parsed, response.text)
        if parsed is None:
            return {"allowed": False, "reason": f"HTTP {response.status_code}: {response.text}"}
        return parsed

    def fetch_state(self) -> dict[str, Any]:
        """This seat's filtered view of the game, right now (`battlefield_state`'s
        answer, minus image trimming duplication - callers that print it do
        that themselves)."""
        state = self.http.get(f"/api/games/{self.game_id()}", params={"uid": self.uid}).json()
        _trim_images(state)
        return state

    def recent_log(self, *, since: int = 0, limit: int = RECENT_LOG_ENTRIES) -> dict[str, Any]:
        """The tail of this seat's own view of the action log - what happened,
        in the game's words, since `since` (0 for "the whole history, tailed").

        This is the half of the seat's question that no answer used to carry.
        Every state-bearing answer already says what this seat *can do*
        (`available_actions`); none of them said what had *been done*, so
        `prompt.py` told the agent to read `battlefield_state` and then `game_log` -
        two tool calls and two whole inference cycles for one question. Now
        `run_action`, `pass_until`, `block_until_wake` and `battlefield_state` all
        carry this, and the pair collapses to one call.

        One HTTP request, and it can never raise: like `available_actions`
        (see there for the same reasoning at greater length) this is called on
        paths that have already performed a mutating action and must still
        return that action's own outcome, so every failure comes back as
        `log_error` naming what went wrong rather than as an exception. An
        empty `entries` with no `log_error` means "nothing has happened since
        `since`" and nothing else.

        `omitted` is how many entries `limit` dropped off the front. It exists
        so a seat is never quietly handed less than happened: a non-zero
        `omitted` is the signal to call `game_log` for the rest, and the
        entries themselves are headlines (`LOG_HEADLINE_FIELDS`) rather than
        whole rows, which `game_log` also still serves in full.
        """
        params = {"uid": self.uid, "since": since}
        try:
            response = self.http.get(f"/api/games/{self.game_id()}/log", params=params)
        except _AMBIGUOUS_TIMEOUTS as exc:
            return _log_unavailable(since, f"the log call timed out: {exc}")
        except httpx.HTTPError as exc:
            return _log_unavailable(since, f"could not reach the API for the log: {exc}")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code != 200 or not isinstance(payload, dict):
            detail = payload.get("detail") if isinstance(payload, dict) else response.text.strip()
            return _log_unavailable(
                since, f"the log call answered HTTP {response.status_code}: {detail}"
            )
        entries = [entry for entry in (payload.get("log") or []) if isinstance(entry, dict)]
        kept = entries[-limit:] if limit and len(entries) > limit else entries
        return {
            "entries": [log_headline(entry) for entry in kept],
            "last_seq": payload.get("last_seq", since),
            "omitted": len(entries) - len(kept),
        }

    def declare_plan(
        self,
        *,
        board_read: str,
        my_role: str,
        plan: str,
        expecting: str,
        because: str,
        confidence: str = "medium",
    ) -> dict[str, Any]:
        """`POST .../plan` (`schemas.DeclarePlanIn`) - a thin passthrough,
        no rule of the engine's own re-implemented here. This mutates no
        `Game` and holds no priority to release, so it goes through
        `call_json` directly rather than `run_action`: there is no wake
        condition to check afterward, and none of `run_action`'s auto-pass
        machinery applies to a call that never touched the board.

        `uid` travels in the request body, not only as the query parameter
        every other call already carries: the route checks it there
        (`app.declare_plan`), because a plan is hidden information about how
        this seat means to play, and the server must confirm the caller
        actually *is* the seat it is declaring for before writing anything
        into that seat's name.

        Returns `call_json`'s normalised shape either way - `declared: True`
        with the ledger's own `log_seq` on success, or `{"allowed": False,
        "reason": ...}` on a refusal (a blank field, or a uid that does not
        open this seat) - the same shape every action tool's own outcome
        already is, so a caller checks a refusal the one way it already
        knows rather than learning a second one for this call alone.
        """
        body = {
            "player": self.player,
            "uid": self.uid,
            "board_read": board_read,
            "my_role": my_role,
            "plan": plan,
            "prediction": {"expecting": expecting, "because": because, "confidence": confidence},
        }
        return self.call_json("POST", f"/api/games/{self.game_id()}/plan", body)

    def sources_or_all(self, given: str, state: dict[str, Any] | None = None) -> list[str]:
        """Which mana sources to offer: the ones named, else all untapped ones.

        Shared by both transports so "empty means all of them" can only ever
        mean one thing. It used to be written out twice, and the two copies
        already disagreed: `mcp.py` fell back to every untapped source
        whenever the parsed list came back empty, while `seat_cli.py` decided
        on the raw string, so a `--sources` value that parsed to nothing (a
        bare comma, stray whitespace) offered the engine *no* sources at all
        and the action was refused for mana it plainly had.
        """
        named = _parse_ids(given)
        if named:
            return named
        return self.my_untapped_sources(state if state is not None else self.fetch_state())

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

    def __init__(self, api: str, game_id: str | None, *, resolve_by_uid: bool = False) -> None:
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
        #: 2b T11: the http transport is one long-running process that may
        #: serve several concurrently-live games at once - "the one live
        #: game" (`game_id()` below) stops being a coherent idea for it, and
        #: `--game` becomes optional. `True` makes `seat_for` resolve each
        #: uid independently via `GET /api/seats/{uid}` (T8's seat-lookup
        #: endpoint, `app.find_seat`) instead of assuming a single game -
        #: `mcp.py`'s `main()` sets this only for the http transport, and
        #: only when no `--game` pins one explicitly.
        self._resolve_by_uid = resolve_by_uid
        # One fixed timeout for every real call this client makes - resolved
        # once for this whole process's life, not per `Seat` (`seat_for`
        # hands the same value to every `Seat` it mints, below).
        self.http = httpx.Client(base_url=self.api, timeout=CLIENT_TIMEOUT_SECONDS)
        # A bare float, not `httpx.Timeout(...)`: tests monkeypatch
        # `httpx.Client` itself to hand back a `TestClient` (built on the
        # unrelated `httpx2` package here), so anything constructed through
        # `httpx.Client(...)` is safely swallowed by that patch - but this
        # assignment reaches the concrete instance directly, and handing it
        # a real `httpx.Timeout` object (a foreign type to `httpx2`) breaks
        # that client's own `__repr__`. A float is what every implementation
        # of `.timeout =` already accepts and normalises itself.
        self.http.timeout = CLIENT_TIMEOUT_SECONDS

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
        if self._resolve_by_uid:
            return self._seat_for_any_game(uid)
        game_id = self.game_id()
        player = self._by_uid.get(uid)
        if player is None:
            response = self.http.get(f"/api/games/{game_id}/seat", params={"uid": uid.strip()})
            if response.status_code != 200:
                return None
            player = response.json()["player"]
            self._by_uid[uid.strip()] = player
        return Seat(player, uid.strip(), self.api, game_id, timeout=self.http.timeout)

    def _seat_for_any_game(self, uid: str) -> Seat | None:
        """`resolve_by_uid=True` path: one round trip to `GET
        /api/seats/{uid}` finds this uid's game and player with no game id
        assumed up front - what the http transport needs to serve several
        live games from one process. No `_by_uid` caching here: unlike the
        single/pinned-game path, there is no one "current live game" whose
        restart would tell this process to drop a stale cache (see
        `game_id`'s docstring) - every call simply asks fresh."""
        response = self.http.get(f"/api/seats/{uid.strip()}")
        if response.status_code != 200:
            return None
        body = response.json()
        return Seat(
            body["player"], uid.strip(), self.api, body["game_id"], timeout=self.http.timeout
        )


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
    return {key: value for key, value in state.items() if key not in _NOT_THE_BOARD}


#: Keys a caller may have bolted onto a state view that are this harness
#: talking, not the board: `wake_reason` (`mcp.wait_for_priority` stamps it
#: on) and `recent_log` (`mcp.battlefield_state`). Both change without the board
#: changing - the log strictly grows - so counting either as board state
#: would wake a seat for its own history.
_NOT_THE_BOARD = frozenset({"wake_reason", "recent_log"})


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
    pregame = state.get("pregame")
    if isinstance(pregame, dict):
        # A game booted from decklists *rests* in CR 103.5's mulligan step and
        # then in CR 103.6's opening-hand step before CR 103.8's first turn.
        # Nobody has priority in either (nothing in CR 103.5 or CR 103.6 grants
        # any), so without this a seat sat in `wait_for_priority` until it timed
        # out while the game waited on that very seat to declare.
        if player in (pregame.get("awaiting_mulligan") or []):
            return "the game is waiting on your CR 103.5 mulligan declaration"
        if pregame.get("opening_hand_actor") == player:
            return "it is your turn to take CR 103.6 opening-hand actions, or finish"
        # ...but a seat that has already declared and is waiting on somebody
        # else's declaration is NOT woken. This used to return "the game has
        # not started yet: another seat owes a pregame declaration", and a
        # non-None reason is exactly what makes `block_until_wake` return
        # before its bound - so every `wait_for_priority(max_seconds=...)` a
        # seat made in the pregame came straight back with an unchanged board
        # and the seat spent a whole inference on it. A live pod
        # (`mtg-seat-3b6613e0`) ran 53 of those back to back, one every 3.2
        # seconds for the whole time its human opponent spent thinking, each
        # answer byte-identical to the last.
        #
        # It is not a wake because there is nothing there to act on. CR 103.5:
        # "First, the starting player declares whether they will take a
        # mulligan. Then each other player in turn order does the same. Once
        # each player has made a declaration, all players who decided to take
        # mulligans do so at the same time" - a seat that has declared has no
        # further move until every seat has, and CR 103.6's opening-hand
        # actions do not begin until "the mulligan process (see rule 103.5) is
        # complete". That is the definition of `NOTHING_NEW_YET`, so `None` is
        # the honest answer and the bounded wait gets to do its job. Both
        # rules were read in full (`mise run rules:find`) in the session that
        # changed this.
        return None
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


#: `block_until_wake`'s answer when it checked and no wake condition holds
#: yet - poll-and-return (see the module docstring's "No transport-level
#: block left to outlast"): the caller polls by calling again itself, rather
#: than this function holding a request open to notice the change for it.
NOTHING_NEW_YET = "nothing new yet - call again to check"


#: Ceiling for `max_seconds` on `block_until_wake`/`pass_until` - the bound
#: that makes bringing a server-side wait back at all safe. MCP 2026-07-28
#: removing resumability (SEP-2577) is why the old *unbounded* wait (up to
#: 570 seconds) was removed outright rather than merely shortened: a call
#: that blocks holding the request open loses it outright if the underlying
#: stream breaks mid-wait, and that risk scales with how long it sits open.
#: This keeps the bound small - comfortably under both this seat's own
#: game-API client timeout (`CLIENT_TIMEOUT_SECONDS`, 30s) and any
#: transport-level read timeout a real MCP client enforces on a single tool
#: call - so the call still always returns, on its own schedule, rather than
#: risking the connection outliving it. A caller-requested `max_seconds`
#: above this is silently capped, never honoured past it.
MAX_WAIT_SECONDS = 25.0

#: How often a bounded wait re-checks the wake condition. Short enough that a
#: wake condition is noticed promptly once it actually holds; long enough not
#: to hammer the game API with a read every few milliseconds.
WAIT_POLL_SECONDS = 1.0


def block_until_wake(
    seat: Seat, before: dict[str, Any] | None, max_seconds: float = 0.0
) -> dict[str, Any]:
    """Check this seat's own state, right now - and, if nothing has changed
    yet, keep checking for up to `max_seconds` (capped at `MAX_WAIT_SECONDS`)
    before giving up and returning `NOTHING_NEW_YET` anyway. Left at its
    default `0.0`, this is exactly the single check-and-return it has always
    been: never blocks, never sleeps.

    This used to poll in an *unbounded* loop for up to a caller-given
    `max_seconds` (60 by default, and an MCP client could ask for more),
    holding one HTTP request open the whole time it waited - up to 570
    seconds for `wait_for_priority` (`mcp.py`). MCP protocol revision
    2026-07-28 removed resumability (SEP-2577): a tool call that blocks like
    that loses the request outright if the underlying stream breaks
    mid-wait, with no way for either side to recover it, so that wait was
    removed outright rather than shortened - the module docstring's "No
    transport-level block left to outlast" section covers the history.

    That removal traded one problem for another: an agent seat with nothing
    to do had no way to wait at all, and started busy-polling with an
    escalating `Bash sleep` between calls instead - the exact log noise a
    predecessor's `pass` re-issued every 1200ms forever was supposed to be
    the last of. `max_seconds` is the bounded version of the wait that was
    removed: it still always returns on its own, and it is capped small
    enough that the SEP-2577 hazard stays negligible (a stream breaking
    during a wait of at most `MAX_WAIT_SECONDS` is the same order of risk as
    it breaking during any other single request) while giving a seat
    something to actually sit on instead of a Bash sleep-and-retry loop it
    has to invent for itself. A caller that needs longer than the bound just
    calls again - `NOTHING_NEW_YET` after the bound elapses says so exactly
    the way it always has, just later than an immediate check would.

    Never touches `store.locked` even while waiting - see the module
    docstring's "why this cannot deadlock": every iteration is an ordinary
    read, and `time.sleep` between them holds nothing.
    """
    max_seconds = max(0.0, min(max_seconds, MAX_WAIT_SECONDS))
    deadline = time.monotonic() + max_seconds
    while True:
        state = seat.fetch_state()
        reason = compute_wake_reason(state, before, seat.player)
        remaining = deadline - time.monotonic()
        if reason is not None or remaining <= 0:
            # `recent_log` is a sibling of `state`, never a key inside it:
            # callers hand this very `state` back as the next call's `before`
            # (`run_action`, and `test_action_flow.py`), and `_board_signature`
            # would then read a growing log as "the board has changed" and
            # wake the seat for its own history.
            return {
                "state": state,
                "wake_reason": reason or NOTHING_NEW_YET,
                "recent_log": seat.recent_log(),
            }
        time.sleep(min(WAIT_POLL_SECONDS, remaining))


def available_actions(seat: Seat, state: dict[str, Any]) -> dict[str, Any]:
    """What this seat can actually do right now - `GET .../legal-actions`
    (`app.py`), the api's own door onto Phase 1's enumeration surface,
    `machines.legal_actions.legal_actions(game, player)` in the engine.

    T11 replaces what this function used to be: one HTTP `dry_run` round trip
    per hand card, per graveyard/exile card with a `cast_options` entry, and
    per battlefield ability, each collapsed into a bare boolean before ever
    reaching this seat. That probe could only ever report "yes" or "no" - a
    dry run either succeeds or it does not - so a modal or X spell whose
    affordability genuinely cannot be judged yet (CR 107.3a: X is not
    announced until the spell is cast) always came back `castable: false`,
    indistinguishable from a card with no red mana in play. The engine's own
    enumeration carries a third answer, `Affordability.UNKNOWN`
    (`models/action.py`), and this function surfaces it rather than hiding it
    the way the old probe structurally had to - each entry under `actions`
    below carries its own `affordable` (`"yes"`/`"no"`/`"unknown"`) and
    `unaffordable_reason`, in the engine's own words.

    One HTTP call, not N: the server computes the whole pool in one request
    (CR 118.6 - an unpayable cast is still a legal one, so nothing here is
    dropped for being unaffordable), rather than this seat driving one dry
    run per candidate itself.

    Nothing here can leak the opponent's hand, either library, or an
    opponent's own cast options: `legal_actions` enumerates only what
    `seat.player` themself controls or holds (its own module docstring), the
    same guarantee the old probe leaned on `visibility.py`'s redaction for.

    `attackers_requested`/`blockers_requested` are read straight off
    `state["pending_decision"]["kind"]` - a `declare_attackers`/
    `declare_blockers` pause (CR 508.1, CR 509.1) addressed to this seat *is*
    this seat's moment to declare, the one case where a pending decision
    names the single legal thing to do rather than merely narrowing the pool
    (which `legal_actions` itself already does server-side: while a decision
    stands, `actions` holds nothing but that decision's own answers, for the
    player it addresses - see the endpoint's own docstring).

    **An empty `actions` means "you legally cannot do anything", and nothing
    else.** Every way this call can fail instead - the API unreachable, a
    read timeout, a 4xx/5xx, a body that is not the expected JSON - comes
    back with `actions_error` naming what went wrong, and `run_action`
    embeds that in all seven of its own answers. Without it a 404 or a 500
    carrying a JSON body parsed cleanly into `payload.get("actions", [])` and
    reached the seat as a silent, confident empty pool - and `Seat.http`
    never calls `raise_for_status`, so nothing upstream would have caught it
    either. The old per-candidate probe degraded on `httpx.HTTPError` for
    exactly this reason; one call in place of N makes the single failure
    total rather than partial, which is a reason to guard it harder, not
    less. The timeout guard is the same `_AMBIGUOUS_TIMEOUTS` split
    `call_json` documents, for the same reason: this is a plain read, so a
    timeout here changed nothing server-side - it is reported, never raised,
    because `run_action` calls this on paths that have already performed a
    mutating action and must still return that action's own outcome.
    """
    try:
        response = seat.http.get(
            f"/api/games/{seat.game_id()}/legal-actions",
            params={"uid": seat.uid, "player": seat.player},
        )
    except _AMBIGUOUS_TIMEOUTS as exc:
        return _actions_unavailable(state, f"the legal-actions call timed out: {exc}")
    except httpx.HTTPError as exc:
        return _actions_unavailable(state, f"could not reach the API for legal actions: {exc}")
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code != 200 or not isinstance(payload, dict):
        detail = payload.get("detail") if isinstance(payload, dict) else response.text.strip()
        return _actions_unavailable(
            state, f"the legal-actions call answered HTTP {response.status_code}: {detail}"
        )
    return _actions_view(state, payload.get("actions") or [], None)


def _actions_view(state: dict[str, Any], actions: list[Any], error: str | None) -> dict[str, Any]:
    pending = state.get("pending_decision")
    kind = pending.get("kind") if pending is not None else None
    view: dict[str, Any] = {
        "actions": actions,
        "attackers_requested": kind == "declare_attackers",
        "blockers_requested": kind == "declare_blockers",
        "pending_decision": pending,
    }
    if error is not None:
        view["actions_error"] = error
    return view


def _actions_unavailable(state: dict[str, Any], reason: str) -> dict[str, Any]:
    """`available_actions`' answer when the call itself failed: an empty pool
    that says so, rather than one a reader would take for "nothing is legal".
    `attackers_requested`/`blockers_requested`/`pending_decision` are still
    filled in - they are read off the state this seat already has in hand,
    not off the failed call, so they stay true."""
    return _actions_view(
        state,
        [],
        f"{reason} - this is NOT 'you have nothing legal to do': the list could not be "
        "fetched at all. Read your state, or call again.",
    )


def _refused(outcome: Any) -> bool:
    return isinstance(outcome, dict) and outcome.get("allowed") is False


def added_mana(outcome: Any) -> bool:
    """Whether `outcome` is a successful activation of a *mana* ability -
    `engine_api.activate_ability` marks exactly those `mana_ability: True`,
    because `Game.activate_ability` returns no stack object for them.

    Why `run_action` treats that as "hold priority" no matter what the caller
    asked for: a mana ability does not use the stack and resolves immediately
    (CR 605.3b), so there is nothing to release priority *for* - and releasing
    it is actively destructive. The auto-pass here is one half of "all players
    pass in succession while the stack is empty", which ends the phase or step
    (CR 500.2, CR 505.2); as it ends, "any unspent mana left in a player's mana
    pool empties" (CR 500.5). So the harness's convenience pass could throw
    away the very mana the call had just made, along with the rest of the
    seat's own main phase. CR 117.3c gives the activating player priority back
    ("if a player has priority when they cast a spell, activate an ability, or
    take a special action, that player receives priority afterward"); for a
    mana ability, keeping it is the only sane default.
    """
    return isinstance(outcome, dict) and outcome.get("mana_ability") is True


def _unknown_outcome(outcome: Any) -> bool:
    """Whether `outcome` is `call_json`'s ambiguous-timeout marker, not an
    ordinary rules refusal - see `UNKNOWN_OUTCOME`. Checked before
    `_refused`: both shapes carry `allowed: False`, but only this one means
    "we do not know what happened", and conflating them would tell a player
    "nothing changed, still your move" about a call that may well have gone
    through."""
    return isinstance(outcome, dict) and outcome.get("unknown_outcome") is True


def _paused(outcome: Any) -> bool:
    """Whether `outcome` is a command that paused on a decision rather than
    completing - `commands.py`'s pause-and-resume: the mutating call itself
    returns immediately with the question attached (`allowed: None`,
    `paused: True`, `pending_decision`), instead of blocking for it the way
    it used to. `run_action` treats this exactly like a decision that used to
    surface mid-flight through `_call_watching_for_self_decision` (below) -
    both mean this seat has to answer through `decide()` before it can tell
    what its own action did. In practice `perform()` now returns fast enough,
    paused or not, that `_call_watching_for_self_decision`'s own polling
    loop rarely gets a chance to fire at all - this is the check that
    actually catches it now."""
    return isinstance(outcome, dict) and outcome.get("paused") is True


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
    release: bool = True,
    reasoning: str | None = None,
) -> dict[str, Any]:
    """Do one action, then behave the way both transports promise: unless
    told to hold, release priority, then check once for this seat's next
    moment to act - and always return the action's own outcome alongside
    whatever state that check saw, in one answer.

    `perform` is a zero-argument callable performing the actual HTTP call and
    returning its parsed JSON body. `release` is `False` only for `pass`
    itself (the action already *is* releasing priority - calling
    pass-priority a second time on top of it would just be refused, harmlessly,
    but there is no reason to make the extra request).

    **Nothing here bounds a wait, because nothing here waits.**
    `block_until_wake` is poll-and-return (see its own docstring - MCP
    2026-07-28 removed resumability, so this call never holds the request open
    waiting for something to happen), so there is no deadline to accept and
    this takes no `max_seconds`: a parameter that bounds nothing is a lie an
    agent reads as a promise, and the MCP wire format shows a caller no
    per-parameter description to correct it with. A caller that wants to keep
    watching just calls the action tool (or `wait_for_priority`) again;
    `wake_reason` says `NOTHING_NEW_YET` rather than blocking to make that
    true.

    Refusal and `hold_priority` both return **immediately, never blocking**:
    a refused action leaves this seat exactly where it was (still its move,
    try something else), and `hold_priority=True` must never block by
    construction - the check happens before `block_until_wake` is ever
    reached, not by timing or luck.

    One outcome overrides `hold_priority=False` and holds anyway: an activated
    *mana* ability (`added_mana`, see there for the CR 605.3b/500.2/500.5
    reasoning). Both transports get that from here rather than from their own
    defaults, because neither can know whether the ability was a mana ability
    until the answer comes back.

    `perform` itself, and the auto-pass below, both go through
    `_call_watching_for_self_decision` rather than being called directly:
    either can cross a decision addressed to this very seat (an ETB trigger
    the seat itself must target, the cleanup discard closing its own turn,
    ...), which - unlike a decision addressed to someone else - this seat's
    own single in-flight call could never otherwise answer. See that
    function's docstring for the full shape of why.

    `reasoning` (T10): the *triggering* action's own stated reasoning, not a
    second one for the auto-pass this function issues on its behalf. That
    auto-pass is this harness releasing priority after an agent's already-
    reasoned action, not a fresh decision of its own - mcp.py's `act` passes
    the same string through here that the tool call itself gave, so an agent
    seat's auto-release never gets refused with a 422 for a reasoning field
    nothing here would know how to fill in honestly.
    """
    before = seat.fetch_state()

    def answered(state: dict[str, Any], **fields: Any) -> dict[str, Any]:
        """One answer shape for all eight of this function's returns, so a
        field can never be added to seven of them and forgotten in the
        eighth. Three things ride together because a seat needs all three
        after every action and used to spend a separate round trip on each:
        the state the action left behind, this seat's legal actions in it,
        and the tail of what has actually happened (`recent_log` - see
        `Seat.recent_log`)."""
        return {
            **fields,
            "state": state,
            "available_actions": available_actions(seat, state),
            "recent_log": seat.recent_log(),
        }

    outcome, self_decision = _call_watching_for_self_decision(seat, perform)
    if self_decision is None and _paused(outcome):
        # `perform()` itself paused (`commands.py`) rather than a decision
        # surfacing mid-flight through the polling race above - see
        # `_paused`'s own docstring for why this is the common case now.
        self_decision = outcome.get("pending_decision")

    if self_decision is not None:
        state = seat.fetch_state()
        return answered(
            state,
            outcome=None,
            held_priority=None,
            wake_reason=(
                "a decision is waiting for you, raised by this very call: "
                f"{self_decision.get('prompt')} - answer it with decide() now; "
                "that unblocks the action you just took, whose own outcome you "
                "will only see afterward, from `recent_log` below or from state"
            ),
            in_flight_action=True,
        )

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
        return answered(state, outcome=outcome, held_priority=None, wake_reason=UNKNOWN_OUTCOME)

    if _refused(outcome):
        state = seat.fetch_state()
        return answered(
            state,
            outcome=outcome,
            held_priority=True,
            wake_reason="refused - nothing changed, still your move",
        )

    if hold_priority or added_mana(outcome):
        state = seat.fetch_state()
        return answered(
            state,
            outcome=outcome,
            held_priority=True,
            wake_reason=(
                "hold_priority=true: kept priority, not blocking"
                if hold_priority
                else (
                    "a mana ability does not use the stack (CR 605.3b), so this kept "
                    "priority for you whatever you asked for: releasing it can end the "
                    "step (CR 500.2), and the mana you just made empties as it does "
                    "(CR 500.5). Spend it now, or pass explicitly to throw it away"
                )
            ),
        )

    pass_outcome = None
    if release:

        def perform_pass() -> Any:
            body: dict[str, Any] = {"player": seat.player}
            if reasoning:
                body["reasoning"] = reasoning
            return seat.call_json(
                "POST", f"/api/games/{seat.game_id()}/actions/pass-priority", body
            )

        pass_outcome, pass_self_decision = _call_watching_for_self_decision(seat, perform_pass)
        if pass_self_decision is None and _paused(pass_outcome):
            pass_self_decision = pass_outcome.get("pending_decision")
        if pass_self_decision is not None:
            # Releasing priority - not the action above, which already came
            # back clean - is what crossed a decision only this seat could
            # ever answer (`put_triggers_on_stack` runs at the top of every
            # `pass_priority`, CR 603.3). Same shape as the check on `perform`
            # itself: report it and let the caller `decide()` as a separate
            # call rather than sit behind its own in-flight pass.
            state = seat.fetch_state()
            return answered(
                state,
                outcome=outcome,
                held_priority=None,
                wake_reason=(
                    "your action went through, but releasing priority raised a "
                    f"decision only you can answer: {pass_self_decision.get('prompt')} - "
                    "answer it with decide() now; check state or `recent_log` "
                    "afterward for what releasing priority itself then did"
                ),
                in_flight_action=True,
            )
        if _unknown_outcome(pass_outcome):
            # The action itself is confirmed (we are past that check above);
            # it is *releasing priority* that is now ambiguous. Blocking
            # further would silently assume an answer this call does not
            # have - the resolving wake_reason from `block_until_wake` could
            # easily be "you have priority", which is simply not knowable
            # yet.
            state = seat.fetch_state()
            return answered(
                state,
                outcome=outcome,
                pass_outcome=pass_outcome,
                held_priority=None,
                wake_reason=UNKNOWN_OUTCOME,
            )

    woken = block_until_wake(seat, before)
    result = answered(
        woken["state"],
        outcome=outcome,
        held_priority=False,
        wake_reason=woken["wake_reason"],
    )
    if pass_outcome is not None:
        result["pass_outcome"] = pass_outcome
    return result


# ------------------------------------------------------------- auto-passing --
#
# One call that keeps passing priority until a *named* point in the turn,
# shared by `mcp.pass_priority(until=...)` and `seat_cli pass --until`, so the
# two transports cannot drift on it. That sharing is the whole finding of this
# repo's front-end parity audit: the mana-ability hold was wrong in one
# transport and right in the other precisely because the behaviour lived in the
# transports instead of here.
#
# The shape is the web UI's, deliberately. `ui/app.js`'s "Feature 3" autopilot
# already shipped this for the human player - `autopilotTargets`,
# `startAutopilot`, `autopilotStopReason`, `forgetStartingStep` - and it is the
# proven design, including the two stop toggles a player ticks in the Priority
# panel. A player and an agent stopping in different places would be a bug in
# itself, so the conditions below are the same conditions in the same order.
#
# A seat must still be able to pass one priority at a time. Auto-passing is a
# convenience for stretches where this seat genuinely cannot act; a pass the
# opponent is watching is part of the game (see `docs/ai_seat_brief.md`), and
# nothing here takes that away.

#: The steps a run may be told to stop at, in turn order (CR 500.1's phases and
#: their steps). `untap` and `cleanup` are deliberately absent: no player
#: receives priority in either (CR 502.4, CR 514.3), so a run that passed its
#: way toward one would be naming a point it can never be given priority in.
#: The same list, and the same two omissions, as `ui/app.js`'s `STEPS` feeding
#: `autopilotTargets`.
AUTOPASS_STEPS = (
    "upkeep",
    "draw",
    "precombat_main",
    "beginning_of_combat",
    "declare_attackers",
    "declare_blockers",
    "combat_damage",
    "end_of_combat",
    "postcombat_main",
    "end_step",
)

#: Whose turn the named step has to belong to. `mine` and `opponent` are read
#: against `active_player`; `any` stops at the first occurrence either way.
AUTOPASS_WHOSE_TURN = ("mine", "opponent", "any")


def autopass_target_error(until_step: str, whose_turn: str) -> str | None:
    """Why this destination cannot be run to, or `None` if it can.

    Returned as an ordinary refusal string by both transports rather than
    raised: a mistyped step name is a caller error to correct, not a crash to
    take a long-lived MCP server down with.
    """
    if until_step not in AUTOPASS_STEPS:
        legal = ", ".join(AUTOPASS_STEPS)
        extra = ""
        if until_step in ("untap", "cleanup"):
            extra = (
                f" No player receives priority in the {until_step} step "
                "(CR 502.4, CR 514.3), so no run can ever stop there - name the "
                "step after it instead."
            )
        return f"'{until_step}' is not a step this can stop at. Legal steps: {legal}.{extra}"
    if whose_turn not in AUTOPASS_WHOSE_TURN:
        return (
            f"'{whose_turn}' is not a turn to stop in. "
            f"Legal values: {', '.join(AUTOPASS_WHOSE_TURN)}."
        )
    return None


def start_autopass(state: dict[str, Any], until_step: str, whose_turn: str) -> dict[str, Any]:
    """The bookkeeping one auto-pass run carries, stamped from the board it
    starts on. Mirrors `ui/priority.js`'s `startRun` (the buildless-UI port
    of the old `ui/app.js` autopilot this function itself was always the
    Python twin of).

    `started_turn`/`started_step` are the reason this is a function and not a
    literal: naming the step you are *already standing in* means the NEXT
    occurrence of it, never this one. A run that stopped where it started
    would be a no-op, which is never what the caller meant - "keep passing
    until my next upkeep", asked in an upkeep, is a whole turn of passing.
    `autopass_stop_reason` declines that one occurrence, and
    `forget_starting_occurrence` clears the stamp the moment the board leaves
    it.

    `started_pending_kind` narrows that same exemption for
    `declare_attackers`/`declare_blockers` specifically: a paused decision
    freezes `state["step"]` at whatever it was *before* the pause (see
    `autopass_stop_reason`'s docstring), so a declare-blockers pause can show
    the exact same `(turn_number, step)` pair a run's own declare-attackers
    step already stamped as its start - two genuinely different occurrences
    that would otherwise look identical to the `(turn, step)` pair alone.
    Comparing the pending decision's own kind alongside it is what tells
    "the pause the run began already sitting on" apart from "a different
    pause that merely inherited the same frozen step" - `None` when the run
    did not begin mid-pause at all.

    `known_stack_ids` is the same idea for the stack: only objects that arrive
    *after* the run began count as "something new landed", or a run started
    with a spell already resolving would stop immediately on it. A list rather
    than a set, like every other value here: the whole dict is handed back to
    the caller as `pass_until`'s `resume` token and has to survive a JSON
    round trip through an MCP tool argument.
    """
    pending = state.get("pending_decision")
    return {
        "step": until_step,
        "whose_turn": whose_turn,
        "started_turn": state.get("turn_number"),
        "started_step": state.get("step"),
        "started_pending_kind": pending.get("kind") if pending is not None else None,
        "known_stack_ids": [obj.get("id") for obj in (state.get("stack") or [])],
    }


def resume_autopass(
    resume: dict[str, Any] | None, state: dict[str, Any], until_step: str, whose_turn: str
) -> dict[str, Any]:
    """The run `pass_until` is continuing, or a fresh one stamped from the
    board when there is nothing to continue.

    A `resume` token is a previous call's own run dict handed straight back
    (`pass_until`'s answer carries one whenever `finished` is `false`), so
    everything it holds - above all the starting-occurrence stamp
    `start_autopass` documents - stays the stamp of where the run *began*,
    not of wherever the seat happens to be standing when the caller gets
    round to calling again. `step`/`whose_turn` are taken from the arguments
    rather than the token, so a caller that changes its destination mid-run
    gets the new one; every other field is the token's.
    """
    if not resume:
        return start_autopass(state, until_step, whose_turn)
    run = dict(resume)
    run["step"] = until_step
    run["whose_turn"] = whose_turn
    run.setdefault("known_stack_ids", [])
    return run


def forget_starting_occurrence(run: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Drop the starting-occurrence stamp once the board has actually left that
    occurrence, so the next time that step comes round the run stops there.

    Compares the whole `(turn_number, step)` pair rather than the step alone,
    which is what makes it right even where one turn holds two of the same
    step: CR 500.8 lets an effect add a phase to a turn and CR 500.9 lets one
    add a step, so a second combat phase brings a second declare-attackers step
    on the same turn number. `ui/app.js`'s `forgetStartingStep` is the same
    function for the same reason.
    """
    if run.get("started_step") is None:
        return run
    still_there = (
        state.get("step") == run["started_step"] and state.get("turn_number") == run["started_turn"]
    )
    if not still_there:
        run["started_step"] = None
        run["started_turn"] = None
    return run


def autopass_label(run: dict[str, Any]) -> str:
    """How a run names its own destination, for the stop reason it reports."""
    step = (run.get("step") or "").replace("_", " ")
    whose = run.get("whose_turn") or "mine"
    if whose == "mine":
        return f"your {step}"
    if whose == "opponent":
        return f"your opponent's {step}"
    return f"the next {step}"


def autopass_stop_reason(
    state: dict[str, Any],
    run: dict[str, Any],
    player: str,
    *,
    stop_on_stack: bool = True,
    stop_when_attacked: bool = True,
    stop_when_you_can_attack: bool = True,
) -> str | None:
    """Whether this auto-pass run must stop, and why - a pure function of the
    board, so both transports and the tests exercise the real conditions
    without a game in the loop.

    Checked in the same order `ui/app.js`'s `autopilotStopReason` checks them,
    so a human's autopilot and an agent's run stop in the same places:

    1. the game is over, or has not started yet (CR 103.5's mulligan step and
       CR 103.6's opening-hand step grant nobody priority, so a run that kept
       "passing" through them would burn its whole bound while the game waited
       on this very seat to declare);
    2. a decision is addressed to this seat (CR 608.2d) - it freezes the game
       until answered, and answering is the only legal move;
    3. the named destination is reached (next occurrence, never the one the
       run started in);
    4. this seat is the active player in its own declare-attackers step,
       unless `stop_when_you_can_attack` is off;
    5. this seat is the defending player in a declare-blockers step with
       attackers declared, unless `stop_when_attacked` is off;
    6. something new is on the stack, unless `stop_on_stack` is off.

    Conditions 4 and 5 are the two that are not merely convenience, and they
    exist for one shared reason: **both declarations are turn-based actions
    that "doesn't use the stack" (CR 508.1 for attackers, CR 509.1 for
    blockers; CR 703.4i and CR 703.4j put each immediately after its step
    begins - all verified with `knowledge_search` in the session that wrote
    this), so condition 6 can never see either of them.** Condition 2 is
    different now than it once was: pausing put `DECLARE_ATTACKERS` and
    `DECLARE_BLOCKERS` in `decisions.ANSWERABLE_KINDS` alongside every other
    kind, so `pending_decision` really is set for both - `actions/attack`/
    `actions/declare-blockers` are thin aliases over the same paused-command
    tape `decide` resumes, not a second transport `pending_decision` cannot
    see. So condition 2 has to know to step aside for exactly these two kinds
    and let conditions 4/5 govern them on their own toggles
    (`stop_when_you_can_attack`/`stop_when_attacked`) instead - a bare
    `pending.player == player` check would otherwise fire condition 2 for a
    combat declaration even when the caller explicitly asked not to stop for
    one, since a `pending_decision` addressed to this seat is exactly what a
    paused `declare_attackers`/`declare_blockers` looks like.

    One more wrinkle conditions 4 and 5 both have to work around: a paused
    `POST .../advance` (or the `pass-priority` that crosses the same
    turn-based action while releasing priority) leaves `state["step"]` naming
    the step the call was made *from*, not the one it paused trying to enter
    (`app.py`'s `advance` docstring - "the committed game is left exactly as
    it was") - the turn-based action that would have moved the step pointer
    never finished, so it never moved. A `state["step"] == "declare_attackers"`
    check can therefore never see the pause itself, only the state that
    results *after* it is answered - which is too late to be a stop. Both
    conditions read `pending_decision["kind"]` instead, which is stamped the
    moment the question is asked, before anything about the step itself has
    changed.

    The two costs are different and worth stating separately rather than
    lumping together:

    - without condition 5, a seat running "until my upkeep" through the
      opponent's turn sails through its own declare-blockers step and takes
      the whole attack unblocked - a lost game, not a slow-play annoyance;
    - without condition 4, a seat running "until my postcombat main" passes
      its own declare-attackers step and simply does not attack. That loses an
      attack rather than the game, and what makes it worth stopping for is
      that it is *silent*: the engine's `_ask_declaration`
      (`machines/decisions.py`) falls through to its default answer, and "an
      unscripted player declares nobody, which is always legal", so the turn
      produces no attack and nothing anywhere says why.

    Both are skipped for the occurrence the run *started in*, exactly as
    condition 3 is, and not merely by analogy: asking to auto-pass past combat
    while already standing in that very step is a seat that has looked at
    combat and chosen to move on, and a condition firing on that state would
    stop the run before it moved at all - a dead call rather than a safeguard.
    The next combat is still stopped for, because by then
    `forget_starting_occurrence` has cleared the stamp.

    Deliberately NOT checked in either: whether this seat controls a creature
    that could legally attack, or block any of these attackers. Summoning
    sickness, defender, tap state, evasion, menace, protection and the whole
    of CR 508.1's and CR 509.1's restrictions and requirements are engine
    questions, and guessing at them here would be a worse bug than the ones
    this prevents. "It is your declare-attackers step" and "you are being
    attacked and the game is asking you to block" are the right granularity -
    the same granularity `ui/app.js` uses, so the human's two checkboxes and
    these two parameters mean exactly the same thing.
    """
    if state.get("winner"):
        return f"game over - {state['winner']} won"
    if state.get("drawn"):
        return "game over - drawn"
    if isinstance(state.get("pregame"), dict):
        return "the game has not started yet: the pregame (CR 103.5, CR 103.6) is still open"
    pending = state.get("pending_decision")
    pending_kind = pending.get("kind") if pending is not None else None
    addressed_to_you = pending is not None and pending.get("player") == player
    if addressed_to_you and pending_kind not in ("declare_attackers", "declare_blockers"):
        return f"a decision is waiting for you: {pending.get('prompt')}"

    # Hoisted above all three conditions that use it - the destination and
    # both combat stops - rather than computed inside the destination check,
    # because "not the occurrence this run started in" is one rule that all
    # three obey. `ui/priority.js`'s `sameOccurrence` hoists the same value
    # for the same reason.
    same_occurrence_this_run_started_in = (
        run.get("started_step") is not None
        and state.get("step") == run["started_step"]
        and state.get("turn_number") == run["started_turn"]
    )
    # Conditions 4 and 5 cannot reuse that value on its own: a paused
    # decision freezes `state["step"]` at whatever it was *before* the pause
    # (see this function's own docstring above), so a declare-blockers pause
    # can show the exact same `(turn, step)` pair a declare-attackers step
    # already stamped as the run's start - two different occurrences that
    # would otherwise look identical. `started_pending_kind`
    # (`start_autopass`) is what tells them apart: the exemption for a given
    # combat kind only holds when the run began already sitting on *that*
    # kind's own pause, not merely on a `(turn, step)` pair a later, different
    # pause happens to have inherited.
    same_combat_pause_this_run_started_in = (
        same_occurrence_this_run_started_in and run.get("started_pending_kind") == pending_kind
    )

    wanted = run.get("step")
    whose = run.get("whose_turn") or "mine"
    if wanted and state.get("step") == wanted:
        is_yours = state.get("active_player") == player
        whose_matches = whose == "any" or (whose == "mine") == is_yours
        if whose_matches and not same_occurrence_this_run_started_in:
            return f"reached {autopass_label(run)}"

    if (
        stop_when_you_can_attack
        and not same_combat_pause_this_run_started_in
        and addressed_to_you
        and pending_kind == "declare_attackers"
    ):
        return (
            "it is your declare-attackers step: declaring attackers does not use "
            "the stack (CR 508.1), so nothing else here would have woken you - "
            "attack, or carry on"
        )

    if (
        stop_when_attacked
        and not same_combat_pause_this_run_started_in
        and addressed_to_you
        and pending_kind == "declare_blockers"
    ):
        attacks = ((state.get("combat") or {}).get("attacks")) or []
        if attacks:
            creatures = "creature" if len(attacks) == 1 else "creatures"
            return (
                f"you are being attacked by {len(attacks)} {creatures} and it is your "
                "declare-blockers step: declaring blockers does not use the stack "
                "(CR 509.1), so nothing else here would have woken you"
            )

    if stop_on_stack:
        arrived = [
            obj
            for obj in (state.get("stack") or [])
            if obj.get("id") not in run.get("known_stack_ids", set())
        ]
        if arrived:
            return (
                f"something new went on the stack: {arrived[0].get('name') or arrived[0].get('id')}"
            )
    return None


#: How many priorities one `pass_until` call will give up before handing the
#: board back of its own accord.
#:
#: NOT a wait bound - nothing in that loop waits any more, and every iteration
#: past the first is paid for by a real, completed `pass-priority` that moved
#: the game. This is the safety net under that reasoning: if a pass were ever
#: to come back `allowed: True` without actually releasing priority, the loop's
#: "keep going while I still hold priority" condition would be true forever and
#: nothing else in it would ever stop. A cap on *work done* is honest where a
#: cap on *seconds waited* was not; hitting it is reported as its own stop
#: reason with `finished: false`, the same shape every other early return
#: takes.
MAX_PASSES_PER_CALL = 100


def pass_until(
    seat: Seat,
    *,
    until_step: str,
    whose_turn: str = "mine",
    stop_on_stack: bool = True,
    stop_when_attacked: bool = True,
    stop_when_you_can_attack: bool = True,
    resume: dict[str, Any] | None = None,
    reasoning: str | None = None,
    max_seconds: float = 0.0,
) -> dict[str, Any]:
    """`_pass_until_once` (below), optionally ridden out across a bounded wait
    rather than handed straight back the first time it would otherwise have
    to wait for the opponent.

    Left at its default `max_seconds=0.0`, this is exactly one call to
    `_pass_until_once` and nothing else - the poll-and-return behaviour that
    function's own docstring describes, unchanged for every existing caller.
    A positive `max_seconds` (capped at `MAX_WAIT_SECONDS`, the same ceiling
    `block_until_wake` uses and for the same SEP-2577 reasoning - see its own
    comment) retries a hand-back that is genuinely just *waiting on the
    opponent* - `_pass_until_once` marks it `retryable` when that is what it
    is - sleeping `WAIT_POLL_SECONDS` between attempts, until either the run
    makes it past that wait (possibly all the way to `finished: true`) or the
    bound runs out. `MAX_PASSES_PER_CALL`'s own hand-back is deliberately
    never retried this way: that cap exists so the caller gets to *look at
    the board*, and retrying it automatically would spend the whole bound
    re-hitting the same cap instead.

    The owner's problem this exists for: `wait_for_priority`'s own bounded
    wait (`block_until_wake`) covers the space between a seat's own actions,
    but a `pass_until` run handed back specifically because it is waiting on
    the opponent used to have nothing to sit on either, so a seat with
    nothing else to do reached for `Bash sleep` and a second call instead -
    the exact log noise the owner reported. This is that same bounded wait,
    on this tool too.
    """
    max_seconds = max(0.0, min(max_seconds, MAX_WAIT_SECONDS))
    deadline = time.monotonic() + max_seconds
    kwargs: dict[str, Any] = {
        "until_step": until_step,
        "whose_turn": whose_turn,
        "stop_on_stack": stop_on_stack,
        "stop_when_attacked": stop_when_attacked,
        "stop_when_you_can_attack": stop_when_you_can_attack,
        "reasoning": reasoning,
    }
    result = _pass_until_once(seat, resume=resume, **kwargs)
    while not result["finished"] and result.get("retryable", False) and time.monotonic() < deadline:
        time.sleep(min(WAIT_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        result = _pass_until_once(seat, resume=result.get("resume"), **kwargs)
    result.pop("retryable", None)
    return result


def _pass_until_once(
    seat: Seat,
    *,
    until_step: str,
    whose_turn: str = "mine",
    stop_on_stack: bool = True,
    stop_when_attacked: bool = True,
    stop_when_you_can_attack: bool = True,
    resume: dict[str, Any] | None = None,
    reasoning: str | None = None,
) -> dict[str, Any]:
    """Pass every priority this seat is *currently* able to pass, up to a named
    point in the turn - then return. Never sleeps, never waits for the
    opponent, never holds the request open for a deadline - `pass_until`
    (above) is the public entry point, and the only thing that ever calls
    this directly; its own bounded wait is what a caller who wants to wait
    at all should reach for, not a loop around this function by hand.

    This used to run a deadline loop: `max_seconds` (60 by default, and an MCP
    client could ask for more) with a `time.sleep` in every branch that had
    nothing to do yet, so one tool call sat open for as long as the opponent
    took to think. MCP protocol revision 2026-07-28 removed resumability
    (SEP-2577) - a call that blocks like that loses the request outright if the
    underlying stream breaks mid-wait - and this was the tool most likely to be
    holding one open when it did. `block_until_wake` and `wait_for_priority`
    were converted for that reason; this is the same conversion, and
    `max_seconds`/`poll_seconds` are gone rather than kept as inert
    parameters.

    So: check the stop conditions, act, look again, and **return the moment
    this seat would otherwise have to wait for somebody else** - the other
    seat holding priority, or a combat declaration only they can answer. The
    answer says where the run got to; the caller calls again.

    Progress within one call is not waiting and is not capped by the clock:
    every extra lap is paid for by a `pass-priority` that already completed
    and moved the game (or by answering this seat's own frozen combat
    declaration). A seat whose opponent answers promptly can therefore still
    cross several steps in one call, exactly as before - what it can no longer
    do is sit there while nothing happens. `MAX_PASSES_PER_CALL` bounds the
    work rather than the wall clock; see its own comment.

    `resume` is what makes a run that spans several calls one run rather than
    a fresh one each time, and it is not optional bookkeeping: the
    starting-occurrence exemption (`start_autopass`, and conditions 3/4/5 of
    `autopass_stop_reason`) deliberately does not stop for the step the run
    *began* in. Restarting the run on every call would re-stamp that exemption
    against wherever the seat happens to be standing - so a run that came back
    while sitting on its own `declare_attackers` pause would, on the very next
    call, treat that pause as "the step I started in" and pass straight
    through its own attack. That is the exact silent-no-attack defect
    condition 4 exists to prevent. Every answer with `finished: false`
    therefore carries a `resume` token; hand it back unchanged.

    Never touches `store.locked` (it only reads state and posts ordinary
    actions), so it cannot deadlock against the opponent's own moves - the
    same invariant `block_until_wake` documents.

    A `declare_attackers`/`declare_blockers` pause (CR 508.1, CR 509.1)
    addressed to this seat is not always a stop - `stop_when_you_can_attack`/
    `stop_when_attacked` may say not to - but it always freezes the game
    until answered (the turn-based action that would move `state["step"]`
    past it never finished), so when `autopass_stop_reason` does not stop for
    one, this call answers it with "nobody" (`actions/attack`/
    `actions/declare-blockers`, "if any" makes that always legal) before
    looking again. That is progress this seat can make on its own, so it does
    not end the call.

    The answer carries `stopped_because` (which condition fired, or why it
    handed the board back), `passes` (how many priorities this call actually
    gave up), `finished` (`True` only when a real stop condition fired -
    `False` means "call again, with `resume`"), the state it stopped on, and
    this seat's `available_actions` in it.

    A pass refused mid-run stops the run and reports the refusal rather than
    hammering the same refused call again.

    `reasoning` (T10): threaded onto every individual pass this run makes,
    same as `run_action`'s own - "I am auto-passing through this stretch" is
    itself the reasoning an agent seat gives once, for the whole run, not a
    fresh one per pass.
    """
    before = seat.fetch_state()
    run = resume_autopass(resume, before, until_step, whose_turn)
    passes = 0

    def answer(state: dict[str, Any], reason: str, finished: bool = True) -> dict[str, Any]:
        result = {
            "stopped_because": reason,
            "passes": passes,
            "finished": finished,
            "until": {"step": until_step, "whose_turn": whose_turn},
            "state": state,
            "available_actions": available_actions(seat, state),
            # A run crosses windows this seat never looks at, so it is the
            # tool with the most to say about what happened while it was not
            # watching - and until this it said none of it, leaving the seat
            # to spend a second call on `game_log` to find out what it had
            # just passed through. See `Seat.recent_log`.
            "recent_log": seat.recent_log(),
        }
        if not finished:
            # A plain copy: `run` is JSON-safe by construction (see
            # `start_autopass`), and this is the token `resume_autopass`
            # takes straight back on the next call.
            result["resume"] = dict(run)
        return result

    def handed_back(
        state: dict[str, Any], reason: str, *, retryable: bool = True
    ) -> dict[str, Any]:
        result = answer(
            state,
            f"{reason} - this call is not waiting for it: {passes} "
            "priorities passed so far. Call again (with `resume`) to keep going.",
            finished=False,
        )
        # Read by `pass_until`'s own bounded-wait wrapper: `retryable` says
        # whether this hand-back is worth automatically retrying inside that
        # wait. Waiting on the opponent is (the default); the
        # `MAX_PASSES_PER_CALL` cap below is not - retrying it would just
        # spend the whole wait re-hitting the same cap instead of letting
        # the caller look, which is the entire point of that cap.
        result["retryable"] = retryable
        return result

    while True:
        state = seat.fetch_state()
        reason = autopass_stop_reason(
            state,
            run,
            seat.player,
            stop_on_stack=stop_on_stack,
            stop_when_attacked=stop_when_attacked,
            stop_when_you_can_attack=stop_when_you_can_attack,
        )
        if reason is not None:
            return answer(state, reason)

        run["known_stack_ids"] = [obj.get("id") for obj in (state.get("stack") or [])]
        forget_starting_occurrence(run, state)

        pending = state.get("pending_decision")
        pending_kind = pending.get("kind") if pending is not None else None
        if (
            pending is not None
            and pending.get("player") == seat.player
            and pending_kind in ("declare_attackers", "declare_blockers")
        ):
            # `autopass_stop_reason` above already decided not to stop for
            # this pause - either it is not addressed to this seat's combat
            # toggles at all, or the seat asked not to stop for combat
            # (`stop_when_you_can_attack=False`/`stop_when_attacked=False`).
            # Either way the game is genuinely frozen (CR 508.1/CR 509.1's
            # turn-based action never finished, so `state["step"]` never
            # moved past it either - see `autopass_stop_reason`'s docstring)
            # until *somebody* answers, and "declare nobody" is always legal
            # ("if any"). `actions/attack`/`actions/declare-blockers` are the
            # aliases that already build the right `pairs` shape for this
            # decision (`app.py`'s `action_attack`/`action_declare_blockers`,
            # thin wrappers over the same paused-command tape `decide`
            # resumes) - used here rather than posting to `decide()` directly
            # with a hand-built answer, matching how a live client answers
            # these two kinds. This is progress this seat can make with
            # nobody's help, so the call carries on rather than handing back.
            if pending_kind == "declare_attackers":
                path = f"/api/games/{seat.game_id()}/actions/attack"
                body: dict[str, Any] = {"attackers": []}
            else:
                path = f"/api/games/{seat.game_id()}/actions/declare-blockers"
                body = {"player": seat.player, "blocks": []}

            def perform_declare(path: str = path, body: dict[str, Any] = body) -> Any:
                # Bound as defaults, not closed over directly: this closure
                # is called before either loop-local is ever reassigned, but
                # ruff's B023 cannot see that this loop always `continue`s or
                # `return`s before its next reassignment, so it flags the
                # bare closure as if the call could run late and see a
                # different `path`/`body` than the one built for it.
                return seat.call_json("POST", path, body)

            outcome, self_decision = _call_watching_for_self_decision(seat, perform_declare)
            if self_decision is None and _paused(outcome):
                self_decision = outcome.get("pending_decision")
            if self_decision is not None:
                return answer(
                    seat.fetch_state(),
                    "declaring nobody raised a decision only you can answer: "
                    f"{self_decision.get('prompt')} - answer it with decide() now",
                )
            if _unknown_outcome(outcome):
                return answer(seat.fetch_state(), UNKNOWN_OUTCOME)
            if _refused(outcome):
                return answer(
                    seat.fetch_state(),
                    f"declaring nobody was refused, so the run stopped: {outcome.get('reason')}",
                )
            # Answering a declaration is a turn-based action (CR 508.1, CR
            # 509.1), not a priority pass, so `passes` deliberately does not
            # count it - a caller reading `passes` is asking how many
            # priorities this run gave up, not how many turn-based actions it
            # answered on this seat's behalf.
            continue

        if pending is not None:
            # Any decision still outstanding, addressed to anyone - this seat's
            # own already stopped the run in `autopass_stop_reason`, except the
            # two combat kinds the branch above answers, so what reaches here
            # is somebody else's. Nothing this seat can do moves the game until
            # they answer, and `state["priority_holder"]` cannot be trusted
            # here: a paused command rolls the committed game back to before
            # the attempt (`commands.py`), so the value this state carries is
            # simply whoever held priority before the freeze - not "nobody has
            # priority", which is what a frozen game actually is.
            #
            # Passing on the strength of it is worse than useless. The pass
            # cannot complete (it crosses the same unanswered question), so it
            # pauses too - and `app.py`'s `_run_command` stores that second
            # pause over the first, discarding the question the other seat was
            # about to answer. A run that kept re-posting one is how a stored
            # pause slot got clobbered and an accepted answer went missing. So
            # this is a hand-back: the caller looks, and calls again once the
            # decision is answered.
            if pending_kind in ("declare_attackers", "declare_blockers"):
                frozen_on = f"{pending_kind} (CR 508.1, CR 509.1)"
            else:
                frozen_on = f"{pending_kind} decision (CR 608.2d)"
            return handed_back(
                state,
                f"the game is frozen on {pending.get('player')}'s own "
                f"{frozen_on} and only they can answer it",
            )

        if state.get("priority_holder") == seat.player:
            if passes >= MAX_PASSES_PER_CALL:
                return handed_back(
                    state,
                    f"this one call has already passed {MAX_PASSES_PER_CALL} priorities "
                    "without ever having to wait, which is more than any real stretch "
                    "of a turn needs - handing the board back so you can look at it",
                    retryable=False,
                )

            def perform_pass() -> Any:
                body: dict[str, Any] = {"player": seat.player}
                if reasoning:
                    body["reasoning"] = reasoning
                return seat.call_json(
                    "POST", f"/api/games/{seat.game_id()}/actions/pass-priority", body
                )

            # Same shape as `run_action`'s auto-pass: releasing priority runs
            # `put_triggers_on_stack` (CR 603.3) and can raise a decision only
            # this seat can ever answer, which this seat's own single in-flight
            # call could never get to. Hand it back rather than sit behind it.
            outcome, self_decision = _call_watching_for_self_decision(seat, perform_pass)
            if self_decision is None and _paused(outcome):
                # Unlike `_call_watching_for_self_decision`'s own polling
                # (which only ever hands back a decision addressed to this
                # seat - see its docstring), `perform_pass`'s own response
                # carries whatever `pending_decision` the pause landed on,
                # addressed to whoever it names - not necessarily this seat.
                # A pass on the *opponent's* turn is exactly what can cross
                # into their own `declare_attackers` (CR 508.1): this seat's
                # priority round still runs on their turn, and the second
                # pass of that round is what triggers the turn-based action,
                # whichever seat happens to make it.
                self_decision = outcome.get("pending_decision")
            if self_decision is not None and self_decision.get("kind") in (
                "declare_attackers",
                "declare_blockers",
            ):
                if self_decision.get("player") == seat.player:
                    # This very pass is what crossed into CR 508.1/CR 509.1's
                    # turn-based action, addressed to this seat itself - not
                    # a decision this seat necessarily has to break out of
                    # the run to answer through `decide()`. Looping back lets
                    # the top of the loop see it fresh: `autopass_stop_reason`
                    # decides whether to stop for it (the same as if it had
                    # been there before this pass was even attempted), and
                    # the branch above answers it with "nobody" when it does
                    # not. Deliberately not counted in `passes` either way -
                    # the pass itself never committed (the paused command's
                    # mutated copy was discarded, same as any other pause),
                    # so nothing here actually released priority yet.
                    continue
                # Addressed to the *other* seat (the case the comment above
                # explains): this seat cannot answer it and spinning on more
                # passes would just cross the exact same wall every time.
                return handed_back(
                    seat.fetch_state(),
                    f"passing crossed {self_decision.get('player')}'s own "
                    f"{self_decision.get('kind')} (CR 508.1, CR 509.1), which only "
                    "they can answer",
                )
            if self_decision is not None:
                return answer(
                    seat.fetch_state(),
                    "passing priority raised a decision only you can answer: "
                    f"{self_decision.get('prompt')} - answer it with decide() now",
                )
            if _unknown_outcome(outcome):
                return answer(seat.fetch_state(), UNKNOWN_OUTCOME)
            if _refused(outcome):
                return answer(
                    seat.fetch_state(),
                    f"a pass was refused, so the run stopped: {outcome.get('reason')}",
                )
            passes += 1
            continue

        return handed_back(
            state,
            "you do not hold priority right now, so there is nothing here to pass",
        )
