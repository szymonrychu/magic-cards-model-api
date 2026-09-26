"""The play-script runner: design D1's lifecycle, transport-agnostic.

Like `seatkit` itself, this module knows nothing of MCP or the CLI: it takes
a `seatkit.Seat`, the `Seats.script_corpora` cache, the agent's own
`reasoning` and `inputs`, and hands back a `RunResult` both transports can
render. Everything it does to the game goes through the routes a seat already
uses - `run_action`, `pass_until` and `POST .../decide` - so every engine
refusal, gate and board-moved check applies to a script move exactly as it
does to a hand-played one.

One run, in order (D1 steps 1-7):

1. resolve the script from the corpus the seat registered (read once per
   `Seats`, never cached on a `Seat`, which is remade every tool call);
2. check `opposing_archetypes` against the agent's asserted
   `inputs["opposing_archetype"]`;
3. if the board is not yet at `when.steps`/`whose_turn` and `wait` is on,
   `pass_until` there with every stop toggle forced on, and exit `unforeseen`
   with the stop reason on any stop short of it;
4. evaluate `true`/`false` over the redacted view (`include=contents` only
   when a condition needs it) and exit `not_applicable`, `needs_input`, or
   `invalid_input` for a supplied input the catalog refuses;
5. make each move: build the request from the state view with the
   selectors, send it through `run_action` holding priority (so
   `expected_seq` is re-armed per move, never one watermark for the chain),
   pass priority only over this run's own objects until they resolve, and
   answer each paused decision from the move's `answers`;
6. after every step of that, exit `interrupted` when the log since the run's
   watermark holds a non-pass entry by another player or the stack holds an
   object this run did not create;
7. once the moves are done, look at `next`: none applicable is
   `chain_complete`, one runs as a child run, more than one is `ambiguous`.

It never calls `available_actions`, and never passes priority beyond the
resolution of the last move. Exits are the closed `Exit` enum, returned as
typed fields - nothing a caller needs is only in prose.

Run state lives in the resume token alone (`RESUME_FIELDS`, plus a `chain`
block carrying the run's bookkeeping across calls). A token's `move_index`
counts the moves whose action has been sent. A token cut off before its start
record posted (while waiting) commits nothing, so resuming it simply carries
on the lifecycle. Once the start record exists - even with `move_index` 0 -
the token keeps the start's turn and step and is refused once either has
moved, since the start route's once-per-turn-and-step check ran for that
position only.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import ValidationError

from ..audit import UNRECORDED_ACTION
from ..baseline_seat import attack_candidates, block_candidates
from ..decisions import TOOL_FIELD_FOR_KIND
from ..seatkit import (
    AUTOPASS_STEPS,
    MAX_WAIT_SECONDS,
    POLL_SECONDS,
    HypotheticalFactsUnavailable,
    Seat,
    _refused,
    _unknown_outcome,
    controlled_notice,
    decision_is_mine,
    holds_priority,
    is_stale,
    pass_until,
    run_action,
    step_position,
)
from . import conditions
from .bindings import judge_bindings
from .inputs import BUILTIN_INPUT
from .schema import AMOUNT_DECISION_KINDS, CHOICE_DECISION_KINDS, WILDCARD, Play
from .selectors import Hypo, Present, matches

#: The tool parameter each decision kind is answered on, by kind value - the
#: fallback for a pending decision that does not name its `answer_field`.
ANSWER_FIELDS = {kind.value: name for kind, name in TOOL_FIELD_FOR_KIND.items()}
#: How many permanents an activation cost's "Sacrifice <n> ..." piece names.
SACRIFICE_NUMBER = re.compile(r"\bsacrifice (an?|another|one|two|three|four|five|\d+)\b", re.I)
DISCARD_NUMBER = re.compile(r"\bdiscard (an?|another|one|two|three|four|five|\d+)\b", re.I)
NUMBER_WORDS = {"a": 1, "an": 1, "another": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}

#: FB2 M2/M3: the two move kinds a pending combat decision may be answered
#: by, as the play's own first move - see `_pending_ready`.
COMBAT_MOVE_KINDS = ("declare_attackers", "declare_blockers")

MAX_SCRIPTS_PER_RUN = 3
MAX_MOVES_PER_RUN = 6
#: The longest one call runs before it hands back a resume token - the same
#: ceiling every other seatkit wait uses.
RUN_CAP_SECONDS = MAX_WAIT_SECONDS

RESUME_FIELDS = (
    "run_id",
    "script_id",
    "script_sha",
    "move_index",
    "answer_index",
    "known_stack_ids",
    "last_seq",
    "turn",
    "step",
    "parent_run_id",
)

PASS_ACTION = "pass_priority"
WHOSE_TURN_FOR_AUTOPASS = {"mine": "mine", "theirs": "opponent", "either": "any"}

EMPTY_REASONING = (
    "run_script needs your own reasoning: every move it makes is sent with it, "
    "and an agent seat's action without one is refused"
)

#: The reasoning `script_tools._call_template`'s `call` string leaves for the
#: seat to fill in - a run sent with this exact text still in it never wrote
#: its own reasoning, and is refused the same as an empty one (S1 review
#: finding 7).
REASONING_PLACEHOLDER = "<say why>"
PLACEHOLDER_REASONING = (
    f"run_script needs your own reasoning, not the call template's placeholder "
    f"text: replace {REASONING_PLACEHOLDER!r} with why you are running this "
    "script now"
)


class Exit(StrEnum):
    """How a run ended. Closed: a caller branches on this, never on prose."""

    CHAIN_COMPLETE = "chain_complete"
    NOT_APPLICABLE = "not_applicable"
    UNFORESEEN = "unforeseen"
    INTERRUPTED = "interrupted"
    AMBIGUOUS = "ambiguous"
    REFUSED = "refused"
    STALE = "stale"
    UNKNOWN_OUTCOME = "unknown_outcome"
    NEEDS_PLAN = "needs_plan"
    ALREADY_RAN = "already_ran"
    ADVISED = "advised"
    NEEDS_INPUT = "needs_input"
    INVALID_INPUT = "invalid_input"


@dataclass
class ScriptExit:
    """One run's typed outcome. `refusal_source` says whether `refusal` is
    the server's own text passed through or the runner's."""

    exit: Exit
    script_id: str | None = None
    run_id: str | None = None
    moves_done: int = 0
    moves_total: int = 0
    failing_condition: dict[str, Any] | None = None
    needs_input: str | None = None
    invalid_input: dict[str, Any] | None = None
    stop_reason: str | None = None
    refusal: str | None = None
    refusal_source: Literal["server", "runner"] | None = None
    ambiguous_names: list[str] | None = None
    applicable_next: list[str] | None = None
    prior_run_id: str | None = None
    proposal: dict[str, Any] | None = None
    unvalidated: list[dict[str, Any]] | None = None
    scripts_run: list[str] = field(default_factory=list)
    record_refusal: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["exit"] = self.exit.value
        return out


@dataclass
class RunResult:
    """`finished` is false only when the time cap cut the run off, and then
    `resume` is the token to hand back."""

    finished: bool
    script_exit: ScriptExit | None
    resume: dict[str, Any] | None = None
    state: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "finished": self.finished,
            "script_exit": self.script_exit.to_dict() if self.script_exit else None,
            "resume": self.resume,
            "state": self.state,
        }


def script_sha(script: dict[str, Any]) -> str:
    """A digest of the script exactly as registered."""
    canonical = json.dumps(script, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def chain_limit_reason(scripts_run: list[str], moves_run: int, play: dict[str, Any]) -> str | None:
    """Why `play` may not run next in this run, or None (design D6)."""
    if play["id"] in scripts_run:
        return f"{play['id']} already ran in this run, and a script runs at most once per run"
    if len(scripts_run) >= MAX_SCRIPTS_PER_RUN:
        return f"a run makes at most {MAX_SCRIPTS_PER_RUN} scripts"
    if moves_run + len(play["moves"]) > MAX_MOVES_PER_RUN:
        return f"a run makes at most {MAX_MOVES_PER_RUN} moves"
    return None


# -- run state -----------------------------------------------------------------


@dataclass
class _Run:
    run_id: str
    script_id: str
    script_sha: str
    parent_run_id: str | None
    scripts_run: list[str]
    moves_run: int
    move_index: int = 0
    answer_index: int = 0
    moves_done: int = 0
    known_stack_ids: list[str] = field(default_factory=list)
    last_seq: int = 0
    turn: int | None = None
    step: str | None = None
    started: bool = False
    seq_start: int | None = None
    resumes: int = 0
    wall_ms: int = 0
    #: True from the moment this run answers a decision until its resolution
    #: stops asking: nobody holds priority in between, so another player's
    #: pass landing then is the pause-slot clobber, not a pass.
    mid_resolution: bool = False

    def token(self, elapsed_ms: int) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "script_id": self.script_id,
            "script_sha": self.script_sha,
            "move_index": self.move_index,
            "answer_index": self.answer_index,
            "known_stack_ids": list(self.known_stack_ids),
            "last_seq": self.last_seq,
            "turn": self.turn,
            "step": self.step,
            "parent_run_id": self.parent_run_id,
            "chain": {
                "scripts_run": list(self.scripts_run),
                "moves_run": self.moves_run,
                "moves_done": self.moves_done,
                "mid_resolution": self.mid_resolution,
                "started": self.started,
                "seq_start": self.seq_start,
                "resumes": self.resumes,
                "wall_ms": self.wall_ms + elapsed_ms,
            },
        }

    @classmethod
    def from_token(cls, token: dict[str, Any]) -> _Run:
        chain = token.get("chain") or {}
        return cls(
            run_id=str(token["run_id"]),
            script_id=str(token["script_id"]),
            script_sha=str(token["script_sha"]),
            parent_run_id=token.get("parent_run_id"),
            scripts_run=list(chain.get("scripts_run") or [token["script_id"]]),
            moves_run=int(chain.get("moves_run") or 0),
            move_index=int(token["move_index"]),
            answer_index=int(token["answer_index"]),
            moves_done=int(chain.get("moves_done") or 0),
            known_stack_ids=[str(i) for i in token.get("known_stack_ids") or []],
            last_seq=int(token.get("last_seq") or 0),
            turn=token.get("turn"),
            step=token.get("step"),
            started=bool(chain.get("started")),
            seq_start=chain.get("seq_start"),
            resumes=int(chain.get("resumes") or 0) + 1,
            wall_ms=int(chain.get("wall_ms") or 0),
            mid_resolution=bool(chain.get("mid_resolution")),
        )


class _HypotheticalCache:
    """O4/O7's engine-computed facts (`Seat.hypothetical_facts`), fetched at
    most once per distinct board - "call it once per judgement... cache per
    (turns_taken, step, state seq)" (O7).

    `turns_taken` (the engine's per-turn counter that moves on an extra turn
    too, CR 500.7 - see `schema.TurnCondition`'s docstring for how it differs
    from the view's own `turn_number`) is not a field `GameStateView` carries,
    so there is no cheaper way to name "has the board moved" than reading the
    view itself: this keys on `turn_number` and `step` for a readable cache
    key, plus a hash of the whole fetched view standing in for "state seq" -
    any mutation changes some field of it, so the hash changes with it, which
    is the actual invalidation guarantee this cache needs. `turn_number`/
    `step` are kept alongside the hash only because they make a hit or miss
    legible in a debugger; the hash alone would already be correct.

    T4: the key also carries `horizon` (whether this call asked for the
    `next_turn` pass) alongside `(turn_number, step, digest)`. A play naming
    `payable_any`/`unlocks_next` and one naming only `unlocks`/`enters_any`
    can both judge the very same board position, and the two answers are not
    interchangeable - the horizon's answer carries extra keys the plain one
    never does, and the plain one's `unlocks_next` is simply absent rather
    than a stale "as of the untap step". Folding the flag into the same
    single-slot fingerprint (rather than keeping one slot per horizon) still
    gives each request its own answer: a fingerprint mismatch - whether the
    board moved or only the horizon asked for changed - always refetches, so
    a `next_turn` answer is never handed to a caller that asked for the
    plain one, or vice versa, even though both share one cache slot and the
    view hash's own invalidation guarantee is unchanged.
    """

    def __init__(self) -> None:
        self._fingerprint: tuple[Any, ...] | None = None
        self._facts: dict[str, Any] = {}

    def get(self, seat: Seat, state: dict[str, Any], *, horizon: bool = False) -> dict[str, Any]:
        digest = hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()
        fingerprint = (state.get("turn_number"), step_position(state), digest, horizon)
        if fingerprint != self._fingerprint:
            fetched = seat.hypothetical_facts(horizon="next_turn" if horizon else None)
            self._facts = dict(fetched.get("cards") or {}) if isinstance(fetched, dict) else {}
            self._fingerprint = fingerprint
        return self._facts


@dataclass
class _Context:
    seat: Seat
    reasoning: str
    corpus: dict[str, Any]
    inputs: dict[str, Any]
    catalog: dict[str, dict[str, Any]]
    deadline: float
    poll_seconds: float
    call_started: float
    #: Who started this run - `"mcp"` (`mcp.run_script`) or `"cli"`
    #: (`mcp-seat script run`) - carried onto both the start and the finish
    #: row (`_run_fields`) so a seat, and `eval_report.py`, can tell its own
    #: runs apart from one an operator staged from the CLI.
    invoked_via: str = "mcp"
    #: The caller going away (an MCP call cancelled by an interrupt): read
    #: like the time cap, so a run waiting on the board stops at once.
    should_stop: Callable[[], bool] | None = None
    #: O4/O7's per-run cache - see `_HypotheticalCache`. One per `_Context`,
    #: so every condition check in one run (and its chained `next` plays)
    #: shares it rather than re-fetching for an unmoved board.
    hypothetical: _HypotheticalCache = field(default_factory=_HypotheticalCache)

    @property
    def me(self) -> str:
        return self.seat.player

    def time_up(self) -> bool:
        if self.should_stop is not None and self.should_stop():
            return True
        return time.monotonic() >= self.deadline

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.call_started) * 1000)


#: `_foreign`'s answer when the log holds a row still being written: look again.
MID_WRITE: Literal["mid_write"] = "mid_write"


class _Unfinished(Exception):
    """The time cap hit mid-run; the run's token is the answer."""


# -- entry point ---------------------------------------------------------------


def run_script(
    seat: Seat,
    *,
    script_id: str,
    reasoning: str,
    corpora: dict[tuple[str, str], dict[str, Any]],
    inputs: dict[str, Any] | None = None,
    declared_inputs: list[dict[str, Any]] | None = None,
    wait: bool = True,
    resume: dict[str, Any] | None = None,
    max_seconds: float = RUN_CAP_SECONDS,
    poll_seconds: float = POLL_SECONDS,
    should_stop: Callable[[], bool] | None = None,
    invoked_via: str = "mcp",
    run_id: str | None = None,
) -> RunResult:
    """Run one registered script (and whatever single `next` play applies
    after it) for `seat`. `inputs` is passed to every condition check in the
    chain; `declared_inputs` is the deck's `inputs.json` list, which says
    which inputs are required. Advise seats get `Exit.ADVISED`.

    `invoked_via` is who started this run - `"mcp"`, `"cli"` or `"armed"`
    (`playscripts.arming.arm`, firing a script on its own between a seat's
    own moves) - carried onto the start and finish rows this run posts
    (`schemas.ScriptRunIn.invoked_via`, required there).

    `run_id`, when given, is used as this run's own id instead of a freshly
    minted one - only for a genuinely fresh start (`resume` is None); a
    resumed run always keeps the id its own token already carries. `script_
    tools.run` is the only caller that ever passes one (API-10): the `run_id`
    of an outstanding `phase: offer` row for this same `script_id`, so
    `report.offer_table` can pair this run back to the offer that proposed
    it."""
    if not reasoning or not reasoning.strip():
        return RunResult(
            True,
            ScriptExit(
                Exit.REFUSED,
                script_id=script_id,
                refusal=EMPTY_REASONING,
                refusal_source="runner",
            ),
        )
    if reasoning.strip() == REASONING_PLACEHOLDER:
        return RunResult(
            True,
            ScriptExit(
                Exit.REFUSED,
                script_id=script_id,
                refusal=PLACEHOLDER_REASONING,
                refusal_source="runner",
            ),
        )
    now = time.monotonic()
    cap = max(0.0, min(max_seconds, RUN_CAP_SECONDS))
    corpus = _corpus(seat, corpora, fresh=bool(resume))
    if isinstance(corpus, ScriptExit):
        return RunResult(True, corpus)
    supplied = dict(inputs or {})
    asserted = supplied.get(BUILTIN_INPUT)
    # B1 (FC5 item 8): `inputs`/`catalog` are judged through the same
    # `bindings.judge_bindings` `script_tools.applicable_hint`/`listing`/
    # `arming._judge_round` judge an offer against, so an offer and the run
    # it invites never disagree about what applied (the module's own
    # docstring). `declared_inputs` - the runner has no deck directory to
    # read matchup stems from, so the caller passes them straight through -
    # stands in for `judge_bindings`'s own `corpus["inputs"]` read, since
    # this call's `corpus` may not carry the same declared list a resumed
    # run's caller originally supplied.
    bindings = judge_bindings(
        seat,
        {**corpus, "inputs": list(declared_inputs or [])},
        archetype=asserted,
        supplied=supplied,
    )
    ctx = _Context(
        seat=seat,
        reasoning=reasoning.strip(),
        corpus=corpus,
        inputs=bindings.inputs,
        catalog=bindings.catalog,
        deadline=now + cap,
        poll_seconds=poll_seconds,
        call_started=now,
        should_stop=should_stop,
        invoked_via=invoked_via,
    )
    if resume:
        return _resume(ctx, script_id, resume)
    raw = _registered(corpus, script_id)
    if raw is None:
        return RunResult(True, _no_such_script(script_id))
    play = _normalised(raw)
    if isinstance(play, ScriptExit):
        return RunResult(True, play)
    if not play["moves"]:
        # API-3: a play with no moves is advice-only (`schema.Play`'s own
        # docstring) - refused here, before a `_Run` even exists and before
        # `_guarded`/`_drive` are ever entered, so no `start` row reaches the
        # ledger for it. A start refused there instead would still burn the
        # once-per-turn-and-step slot (`records._prior_script_run`) for a
        # play that never did anything, and `_drive` reads `play["moves"]`
        # unguarded in several places downstream (`_moves`, `_send_move`,
        # `_settle`, `_advise`, `_end`) that are never meant to be made
        # empty-safe one at a time - the refusal has to land upstream of all
        # of them at once, not inside `_drive` itself.
        return RunResult(True, _advice_only_refusal(play))
    run = _Run(
        run_id=run_id or uuid.uuid4().hex,
        script_id=play["id"],
        script_sha=script_sha(raw),
        parent_run_id=None,
        scripts_run=[play["id"]],
        moves_run=0,
    )
    return _guarded(ctx, run, play, wait=wait)


def _resume(ctx: _Context, script_id: str, token: dict[str, Any]) -> RunResult:
    try:
        run = _Run.from_token(token)
    except (KeyError, TypeError, ValueError) as exc:
        return RunResult(True, _runner_refusal(script_id, f"that resume token is malformed: {exc}"))
    raw = _registered(ctx.corpus, run.script_id)
    if raw is None or script_sha(raw) != run.script_sha:
        why = (
            f"{run.script_id} is no longer registered"
            if raw is None
            else f"{run.script_id} changed since this run began (sha {run.script_sha[:8]})"
        )
        return _end(ctx, run, None, _runner_refusal(run.script_id, f"cannot resume: {why}"))
    play = _normalised(raw)
    if isinstance(play, ScriptExit):
        return RunResult(True, play)
    if run.started or run.move_index > 0:
        state = ctx.seat.fetch_state()
        # `turns_taken`, not `turn_number` (CR 500's own round counter,
        # shared by both seats' turns in one round) - the same identity
        # `_start`/`_guarded` stamp `run.turn` from below, and the one
        # `records.py`'s own module docstring names as the rule for a
        # per-turn identity check: `turn_number` would let this resume
        # think the board had not moved across a same-round turn change.
        if (state.get("turns_taken"), step_position(state)) != (run.turn, run.step):
            moved = _runner_refusal(
                run.script_id,
                f"cannot resume: the run was at turn {run.turn}, {run.step} and the game is "
                f"now at turn {state.get('turns_taken')}, {step_position(state)}",
            )
            return _end(ctx, run, play, moved)
    return _guarded(ctx, run, play, wait=True)


def _guarded(ctx: _Context, run: _Run, play: dict[str, Any], *, wait: bool) -> RunResult:
    try:
        return _drive(ctx, run, play, wait=wait)
    except _Unfinished:
        state = ctx.seat.fetch_state()
        if not run.started:
            run.turn, run.step = state.get("turns_taken"), step_position(state)
        return RunResult(False, None, resume=run.token(ctx.elapsed_ms()), state=state)
    except HypotheticalFactsUnavailable as exc:
        # D2: wherever inside `_drive` a hypothetical-facts fetch failed
        # (`_evaluate`, a move's own `ctx.hypothetical.get`, an answer's),
        # the run ends here rather than an uncaught exception escaping past
        # this point - `_end` posts the run's finish (and its start, if it
        # never got one), so a run `_start` already recorded is never left
        # open just because facts it needed later were unavailable.
        return _end(ctx, run, play, ScriptExit(Exit.UNFORESEEN, stop_reason=str(exc)))


# -- the lifecycle -------------------------------------------------------------


def _drive(ctx: _Context, run: _Run, play: dict[str, Any], *, wait: bool) -> RunResult:
    seat = ctx.seat
    if run.move_index == 0:
        state = seat.fetch_state()
        if not run.started:
            # A chained child keeps its parent's watermark, so nothing that
            # landed between the parent's last check and this start is skipped.
            seat.recent_log()
            run.seq_start = seat.last_seq or 0
            run.last_seq = run.last_seq or run.seq_start
        if play["priority"] == 0:
            return _end(ctx, run, play, _runner_refusal(play["id"], "priority 0: disabled"))
        asserted = ctx.inputs.get(BUILTIN_INPUT)
        if not _archetype_applies(play, asserted):
            return _end(
                ctx,
                run,
                play,
                ScriptExit(
                    Exit.NOT_APPLICABLE,
                    failing_condition={
                        "list": "opposing_archetypes",
                        "index": None,
                        "seen": asserted,
                    },
                ),
            )
        # CR 723.5: while somebody else makes this seat's decisions it has
        # none of its own to script - stand down, saying who is deciding.
        controlled = controlled_notice(state, ctx.me)
        if controlled is not None:
            return _end(ctx, run, play, ScriptExit(Exit.UNFORESEEN, stop_reason=controlled))
        # B8-04: `_in_when` alone is a board position, not a priority window;
        # a move sent while the other seat holds priority is refused in the
        # engine's own words. Route through `_wait` whenever either is off.
        # A priority this seat decides for (CR 723.5, `holds_priority`) is its own.
        # FB2 items 2/3/7: unless this play's first move already answers a
        # decision genuinely pending for this seat right now (`_pending_ready`)
        # - a declare_attackers/declare_blockers decision does not move
        # `priority_holder`, so a defending seat's own declare-blockers play
        # would otherwise wait forever behind a priority it is never handed.
        if not _pending_ready(play, state, ctx.me) and (
            not _in_when(state, play["when"], ctx.me) or not holds_priority(state, ctx.me)
        ):
            waited = _wait(ctx, run, play, state, wait=wait)
            if isinstance(waited, ScriptExit):
                return _end(ctx, run, play, waited)
            state = waited
        if not run.started:
            try:
                refused = _start(ctx, run, play, state)
            except _Unregistered as unregistered:
                return _end(ctx, run, play, unregistered.out, record=False)
            if refused is not None:
                return _end(ctx, run, play, refused)
        failed = _condition_exit(_evaluate(ctx, play, state))
        if failed is not None:
            return _end(ctx, run, play, failed)
        if seat.advise:
            return _end(ctx, run, play, _advise(ctx, run, play, state))
        run.known_stack_ids = [str(obj.get("id")) for obj in state.get("stack") or []]
    stopped = _moves(ctx, run, play)
    if stopped is not None:
        return _end(ctx, run, play, stopped)
    return _next(ctx, run, play)


def _standalone_pending_kinds(play: dict[str, Any]) -> frozenset[str]:
    """The decision kinds this play's own `true` list names with a
    `pending_decision` condition, when the play's one and only move is
    `answer` (FB2 D2/item 7) - empty for every other shape."""
    moves = play.get("moves") or []
    if len(moves) != 1 or moves[0]["kind"] != "answer":
        return frozenset()
    return frozenset(
        condition["decision"]
        for condition in play.get("true") or []
        if condition.get("kind") == "pending_decision"
    )


def _pending_ready(play: dict[str, Any], state: dict[str, Any], me: str) -> bool:
    """FB2 items 2/3/7: is this play's first move already answerable, right
    now, without this seat holding priority in the ordinary CR 117 sense?

    True in two shapes, both gated on a decision of the right kind actually
    being pending for this seat (`decision_is_mine`) - a `declare_attackers`/
    `declare_blockers` decision does not move `priority_holder` off the
    active player even when it is the *defending* seat's own decision to
    answer, so `holds_priority` alone would wrongly gate a blocking seat's
    own declare-blockers play behind a wait that never resolves:

    - the play's own first move is `declare_attackers`/`declare_blockers`
      and a decision of that exact kind is pending for this seat (items 2/3);
    - the play's one move is `answer` and its `true` names a
      `pending_decision` of the kind actually pending (item 7, the
      standalone pending-decision answer).

    When true, `_drive` skips `_wait` (and so `_wait_for_own_priority`'s own
    UNFORESEEN-on-pending exit) entirely for this play's first move - there
    is nothing to wait for, the decision is already on the table."""
    pending = state.get("pending_decision")
    if not isinstance(pending, dict) or not decision_is_mine(pending, state, me):
        return False
    kind = pending.get("kind")
    moves = play.get("moves") or []
    if moves and moves[0]["kind"] in COMBAT_MOVE_KINDS:
        return moves[0]["kind"] == kind
    return kind in _standalone_pending_kinds(play)


def _wait(
    ctx: _Context, run: _Run, play: dict[str, Any], state: dict[str, Any], *, wait: bool
) -> dict[str, Any] | ScriptExit:
    """The board at the script's `when`, or why it never got there.

    B8-04: `when` names a step/turn/stack position, not a priority window,
    so the board can already satisfy it while the *other* seat still holds
    priority there - the runner's own first move would then be refused in
    the engine's own words ("bob does not have priority, alice does"). When
    the position is already right (`_in_when`), this waits for this seat's
    own priority within that same occurrence instead of sending a move that
    can only be refused; it never travels toward a *later* occurrence for
    that, which `_wait_for_own_priority` (not `pass_until`'s own stepping)
    keeps distinct."""
    when = play["when"]
    in_position = _in_when(state, when, ctx.me)
    at_step = step_position(state) in when["steps"] and _whose_matches(state, when, ctx.me)
    if ctx.seat.advise or not wait or (at_step and not in_position):
        return ScriptExit(
            Exit.NOT_APPLICABLE,
            failing_condition={"list": "when", "index": None, "seen": _position(state)},
        )
    if in_position:
        return _wait_for_own_priority(ctx, when, state)
    until = _next_step(state, when, ctx.me)
    autopass: dict[str, Any] | None = None
    while True:
        result = pass_until(
            ctx.seat,
            until_step=until,
            whose_turn=WHOSE_TURN_FOR_AUTOPASS[when["whose_turn"]],
            stop_on_stack=True,
            # T2: `stop_when_attacked`/`stop_when_you_can_attack` default off
            # on `pass_until` itself now - this runner waits with every stop
            # on regardless, explicitly, so a script's own wait cannot pass
            # through a combat this seat needed to see.
            stop_when_attacked=True,
            stop_when_you_can_attack=True,
            resume=autopass,
            reasoning=_suffixed(ctx, run, "wait"),
            max_seconds=max(0.0, ctx.deadline - time.monotonic()),
            include_actions=False,
            should_stop=ctx.should_stop,
        )
        reached = result.get("state") or ctx.seat.fetch_state()
        if result.get("finished"):
            if _in_when(reached, when, ctx.me):
                return reached
            return ScriptExit(Exit.UNFORESEEN, stop_reason=result.get("stopped_because"))
        autopass = result.get("resume")
        if ctx.time_up():
            raise _Unfinished


def _wait_for_own_priority(
    ctx: _Context, when: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any] | ScriptExit:
    """Poll until this seat holds priority in the occurrence of `when`'s
    position it is already sitting in (B8-04). Nothing this seat can do
    moves that along - only the other seat passing or acting does - so this
    polls exactly like `_settle`'s own idle wait rather than reaching for
    `pass_until`, which would treat this occurrence as already behind it."""
    while True:
        if state.get("winner") or state.get("drawn"):
            return ScriptExit(
                Exit.UNFORESEEN,
                stop_reason="the game ended while waiting for this seat's own priority",
            )
        controlled = controlled_notice(state, ctx.me)
        if controlled is not None:
            return ScriptExit(Exit.UNFORESEEN, stop_reason=controlled)
        pending = state.get("pending_decision")
        if decision_is_mine(pending, state, ctx.me):
            return ScriptExit(
                Exit.UNFORESEEN,
                stop_reason=(
                    f"a {pending.get('kind')} decision was asked of this seat while waiting "
                    "for its own priority"
                ),
            )
        if holds_priority(state, ctx.me):
            return state
        if not _in_when(state, when, ctx.me):
            return ScriptExit(
                Exit.UNFORESEEN,
                stop_reason=(
                    "the board moved on while waiting for this seat's own priority: "
                    f"now at {_position(state)}"
                ),
            )
        if ctx.time_up():
            raise _Unfinished
        time.sleep(ctx.poll_seconds)
        state = ctx.seat.fetch_state()


def _needs_settle(move: dict[str, Any]) -> bool:
    """FB2 M1: a `pass` move releases priority once and is done the moment
    the engine accepts it - there is nothing of this run's own to resolve
    (no decision it paused on, no object of its own on the stack), and
    walking `_settle`'s foreign-activity check for it would read whatever the
    seat this run just handed priority to does next as an interruption of a
    run that has already, deliberately, finished handing it off. Every other
    move kind still settles as before."""
    return move["kind"] != "pass"


def _moves(ctx: _Context, run: _Run, play: dict[str, Any]) -> ScriptExit | None:
    total = len(play["moves"])
    if run.move_index > 0 and _needs_settle(play["moves"][run.move_index - 1]):
        stopped = _settle(ctx, run, play)
        if stopped is not None:
            return stopped
        run.moves_done = run.move_index
    while run.move_index < total:
        if ctx.time_up():
            raise _Unfinished
        move = play["moves"][run.move_index]
        stopped = _send_move(ctx, run, play)
        # B7-14: the engine accepting the move (paid, resolved or paused) is
        # what "done" means, so `moves_done` counts it the moment `move_index`
        # advances - before `_settle` runs, so a stop mid-settle (ambiguous,
        # interrupted, unforeseen) still reports the move as sent and done.
        if stopped is None:
            run.moves_done = run.move_index
        else:
            return stopped
        if _needs_settle(move):
            stopped = _settle(ctx, run, play)
            if stopped is not None:
                return stopped
        run.moves_done = run.move_index
    return None


def _next(ctx: _Context, run: _Run, play: dict[str, Any]) -> RunResult:
    state = ctx.seat.fetch_state()
    applicable: list[tuple[str, dict[str, Any], str]] = []
    for next_id in play["next"]:
        raw = _registered(ctx.corpus, next_id)
        candidate = _normalised(raw) if raw is not None else None
        if not isinstance(candidate, dict) or candidate["priority"] == 0:
            continue
        if not _archetype_applies(candidate, ctx.inputs.get(BUILTIN_INPUT)):
            continue
        if not _in_when(state, candidate["when"], ctx.me):
            continue
        result = _evaluate(ctx, candidate, state)
        if result["needs_input"] is not None or result["invalid_input"] is not None:
            return _end(ctx, run, play, _condition_exit(result))
        if result["applies"]:
            applicable.append((next_id, candidate, script_sha(raw)))
    if len(applicable) > 1:
        return _end(
            ctx,
            run,
            play,
            ScriptExit(
                Exit.AMBIGUOUS,
                applicable_next=[next_id for next_id, _, _ in applicable],
                stop_reason="more than one next play applies; extend their conditions",
            ),
        )
    if not applicable:
        return _end(ctx, run, play, ScriptExit(Exit.CHAIN_COMPLETE))
    next_id, child, sha = applicable[0]
    moves_run = run.moves_run + len(play["moves"])
    limit = chain_limit_reason(run.scripts_run, moves_run, child)
    if limit is not None:
        return _end(ctx, run, play, ScriptExit(Exit.CHAIN_COMPLETE, stop_reason=limit))
    _end(
        ctx,
        run,
        play,
        ScriptExit(Exit.CHAIN_COMPLETE, stop_reason=f"chained into {next_id}"),
    )
    child_run = _Run(
        run_id=uuid.uuid4().hex,
        script_id=next_id,
        script_sha=sha,
        parent_run_id=run.run_id,
        scripts_run=[*run.scripts_run, next_id],
        moves_run=moves_run,
        last_seq=run.last_seq,
    )
    return _guarded(ctx, child_run, child, wait=False)


# -- moves ---------------------------------------------------------------------


def _send_move(ctx: _Context, run: _Run, play: dict[str, Any]) -> ScriptExit | None:
    seat = ctx.seat
    index = run.move_index
    move = play["moves"][index]
    seat.recent_log()
    state = seat.fetch_state(include_contents=conditions.needs_contents(move))
    reasoning = _suffixed(ctx, run, f"move {index + 1}/{len(play['moves'])} ({move['kind']})")
    if move["kind"] == "answer":
        pending = state.get("pending_decision")
        if pending is None or not decision_is_mine(pending, state, ctx.me):
            return ScriptExit(
                Exit.UNFORESEEN,
                stop_reason=f"move {index + 1} answers a decision, and none is pending for you",
            )
        run.move_index, run.answer_index = index + 1, 0
        return None
    if move["kind"] == "pass":
        # FB2 M1: the same shape the seat's own bare `pass` tool sends
        # (`mcp.pass_once`) - `hold_priority=False, release=False`, since the
        # action itself already is releasing priority (CR 117.3d); a second,
        # `run_action`-issued pass on top of it would just be refused,
        # harmlessly, but there is no reason to send it.
        answer = run_action(
            seat,
            lambda: seat.call_json(
                "POST",
                f"/api/games/{seat.game_id()}/actions/pass-priority",
                {"player": ctx.me, "reasoning": reasoning},
            ),
            hold_priority=False,
            release=False,
            include_actions=False,
            reasoning=reasoning,
        )
        stopped = _outcome_exit(answer)
        if stopped is not None:
            return stopped
        run.move_index, run.answer_index = index + 1, 0
        return None
    hypo = (
        ctx.hypothetical.get(ctx.seat, state, horizon=conditions.needs_next_turn(move))
        if conditions.needs_hypothetical(move)
        else None
    )
    mana = conditions.untapped_mana(state, ctx.me)
    built = _move_request(move, state, ctx.me, reasoning, hypo=hypo, mana=mana)
    if isinstance(built, ScriptExit):
        return built
    path, body = built
    answer = run_action(
        seat,
        lambda: seat.call_json("POST", f"/api/games/{seat.game_id()}/{path}", body),
        hold_priority=True,
        include_actions=False,
        reasoning=reasoning,
    )
    stopped = _outcome_exit(answer)
    if stopped is not None:
        return stopped
    run.move_index, run.answer_index = index + 1, 0
    return None


def _settle(ctx: _Context, run: _Run, play: dict[str, Any]) -> ScriptExit | None:
    """Carry the move in flight to its resolution: answer its decisions, pass
    over this run's own objects, and stop the moment anything foreign shows."""
    seat = ctx.seat
    index = run.move_index - 1
    move = play["moves"][index]
    reasoning = _suffixed(ctx, run, f"move {index + 1}/{len(play['moves'])} ({move['kind']})")
    while True:
        state = seat.fetch_state()
        foreign = _foreign(ctx, run, state)
        if foreign == MID_WRITE:
            if ctx.time_up():
                raise _Unfinished
            time.sleep(ctx.poll_seconds)
            continue
        if foreign is not None:
            return foreign
        controlled = controlled_notice(state, ctx.me)
        if controlled is not None:
            return ScriptExit(Exit.UNFORESEEN, stop_reason=controlled)
        pending = state.get("pending_decision")
        if pending is not None and decision_is_mine(pending, state, ctx.me):
            stopped = _answer(ctx, run, move, pending, state, reasoning)
            run.mid_resolution = True
            if stopped is not None:
                return stopped
            continue
        if pending is None:
            run.mid_resolution = False
        stack = state.get("stack") or []
        ours_on_top = bool(stack) and str(stack[0].get("id")) in run.known_stack_ids
        ours_on_top = ours_on_top and stack[0].get("controller") == ctx.me
        if pending is None and not ours_on_top:
            return None
        if pending is None and holds_priority(state, ctx.me):
            stopped = _outcome_exit(_pass(ctx, reasoning))
            if stopped is not None:
                return stopped
            continue
        if ctx.time_up():
            raise _Unfinished
        time.sleep(ctx.poll_seconds)


def _pass(ctx: _Context, reasoning: str) -> dict[str, Any]:
    seat = ctx.seat
    body = {"player": ctx.me, "reasoning": reasoning}
    return run_action(
        seat,
        lambda: seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/pass-priority", body),
        hold_priority=True,
        release=False,
        include_actions=False,
        reasoning=reasoning,
    )


def _answer(
    ctx: _Context,
    run: _Run,
    move: dict[str, Any],
    pending: dict[str, Any],
    state: dict[str, Any],
    reasoning: str,
) -> ScriptExit | None:
    seat = ctx.seat
    answers = move.get("answers") or []
    kind = pending.get("kind")
    found = next(
        (i for i in range(run.answer_index, len(answers)) if answers[i]["decision"] == kind), None
    )
    if found is None:
        return ScriptExit(
            Exit.UNFORESEEN,
            stop_reason=(
                f"a {kind} decision was asked ({pending.get('prompt')}) and this move's "
                "answers hold none for it"
            ),
        )
    view = state
    if conditions.needs_contents(answers[found]) or any(
        card.get("type_line") is None for card in _decision_cards(pending, state, ctx.me)
    ):
        view = seat.fetch_state(include_contents=True)
    hypo = (
        ctx.hypothetical.get(ctx.seat, view, horizon=conditions.needs_next_turn(answers[found]))
        if conditions.needs_hypothetical(answers[found])
        else None
    )
    mana = conditions.untapped_mana(view, ctx.me)
    body = decide_body(answers[found], pending, view, ctx.me, reasoning, hypo=hypo, mana=mana)
    if isinstance(body, ScriptExit):
        return body
    run.answer_index = found + 1

    def perform() -> Any:
        answer = seat.call_json("POST", f"/api/games/{seat.game_id()}/decide", body)
        if isinstance(answer, dict) and answer.get("accepted") is False:
            return {"allowed": False, **answer}
        return {"allowed": True, **answer} if isinstance(answer, dict) else answer

    return _outcome_exit(
        run_action(seat, perform, hold_priority=True, include_actions=False, reasoning=reasoning)
    )


def _foreign(
    ctx: _Context, run: _Run, state: dict[str, Any]
) -> ScriptExit | Literal["mid_write"] | None:
    """`interrupted` when another player did something other than pass since
    the run's watermark, posted a pass that neither went through nor merely
    paused on this run's own decision while this run was mid-resolution, or
    the stack holds an object this run did not make. A new object this seat
    controls is the run's own (its trigger) and is tracked instead.

    B7-09, two harmless shapes of "another seat's pass landed while this run
    was mid-resolution", both read off typed fields rather than the log
    entry's prose:

    - `allowed: true` - the pass actually went through, which only happens
      when that player held priority. CR 117.3b gives the active player
      priority the moment a resolution ends, so this run's own resolution
      had already finished by then (this run's `mid_resolution` flag simply
      had not caught up yet); it is cleared and the pass is not foreign.
    - `paused: true` with `paused_on.player == ctx.me` - the pass itself
      paused on *this run's own* pending decision (the pause-slot race), so
      nothing on the board moved.

    A pass that is instead refused, or paused on someone else's decision,
    still interrupts.

    `MID_WRITE` when a row is still the placeholder `store.replace` wrote
    (its route fills it only after releasing the game's lock): the
    watermark stays below it and the caller reads again, since the board it
    committed may already be visible while the row cannot yet say who acted."""
    seat = ctx.seat
    read = seat.call_json("GET", f"/api/games/{seat.game_id()}/log?since={run.last_seq}")
    if not isinstance(read, dict) or not isinstance(read.get("log"), list):
        reason = read.get("reason") if isinstance(read, dict) else read
        return ScriptExit(
            Exit.UNKNOWN_OUTCOME,
            stop_reason=f"the log could not be read to check nothing foreign happened: {reason}",
        )
    for entry in read["log"]:
        if not isinstance(entry, dict) or int(entry.get("seq") or 0) <= run.last_seq:
            continue
        if entry.get("action") == UNRECORDED_ACTION:
            return MID_WRITE
        detail = entry.get("detail") or {}
        player = detail.get("player")
        if not player or player == ctx.me:
            continue
        if entry.get("action") != PASS_ACTION:
            run.last_seq = int(entry["seq"])
            return ScriptExit(
                Exit.INTERRUPTED,
                stop_reason=f"{player} acted ({entry.get('action')}, log seq {entry['seq']})",
            )
        if run.mid_resolution and detail.get("allowed") is True:
            run.mid_resolution = False
            continue
        paused_on = detail.get("paused_on")
        paused_on_me = (
            detail.get("paused") is True
            and isinstance(paused_on, dict)
            and paused_on.get("player") == ctx.me
        )
        if run.mid_resolution and not paused_on_me:
            run.last_seq = int(entry["seq"])
            return ScriptExit(
                Exit.INTERRUPTED,
                stop_reason=(
                    f"{player} passed priority (log seq {entry['seq']}) while this run was "
                    "answering the decisions of a resolution, when nobody holds priority"
                ),
            )
    if isinstance(read.get("last_seq"), int):
        run.last_seq = max(run.last_seq, read["last_seq"])
    for obj in state.get("stack") or []:
        object_id = str(obj.get("id"))
        if object_id in run.known_stack_ids:
            continue
        if obj.get("controller") != ctx.me:
            return ScriptExit(
                Exit.INTERRUPTED,
                stop_reason=(
                    f"{obj.get('controller')} put {obj.get('name') or object_id} on the stack"
                ),
            )
        run.known_stack_ids.append(object_id)
    return None


def _outcome_exit(answer: dict[str, Any]) -> ScriptExit | None:
    """The exit one `run_action` answer forces, or None to carry on."""
    if answer.get("in_flight_action"):
        return None
    outcome = answer.get("outcome")
    reason = outcome.get("reason") if isinstance(outcome, dict) else None
    if _unknown_outcome(outcome):
        return ScriptExit(Exit.UNKNOWN_OUTCOME, stop_reason=reason)
    if is_stale(outcome):
        return ScriptExit(Exit.STALE, refusal=reason, refusal_source="server")
    if _missing_plan(outcome):
        return ScriptExit(Exit.NEEDS_PLAN, refusal=reason, refusal_source="server")
    if _refused(outcome):
        return ScriptExit(Exit.REFUSED, refusal=reason, refusal_source="server")
    return None


def _missing_plan(outcome: Any) -> bool:
    """`records.MissingPlan`'s 422: a validation-shaped `detail` naming the
    `plan` field as missing."""
    detail = outcome.get("detail") if isinstance(outcome, dict) else None
    return isinstance(detail, list) and any(
        isinstance(item, dict)
        and list(item.get("loc") or [])[-1:] == ["plan"]
        and item.get("type") == "missing"
        for item in detail
    )


# -- request bodies ------------------------------------------------------------


def _move_request(
    move: dict[str, Any],
    state: dict[str, Any],
    me: str,
    reasoning: str,
    *,
    hypo: Hypo | None = None,
    mana: int | None = None,
) -> tuple[str, dict[str, Any]] | ScriptExit:
    kind = move["kind"]
    if kind == "activate":
        return _activate_request(move, state, me, reasoning, hypo=hypo, mana=mana)
    if kind == "cast":
        body = cast_body(move, state, me, reasoning, hypo=hypo, mana=mana)
        return body if isinstance(body, ScriptExit) else ("actions/cast", body)
    if kind == "declare_attackers":
        return _attack_request(move, state, me, reasoning, hypo=hypo, mana=mana)
    if kind == "declare_blockers":
        return _block_request(move, state, me, reasoning, hypo=hypo, mana=mana)
    chosen = _pick(
        _zone_cards(state, "hand", me),
        move["card"]["select"],
        "the land to play",
        move["card"].get("prefer"),
        present=conditions.presence(state, me),
        ties=move["card"].get("ties"),
        hypo=hypo,
        mana=mana,
    )
    if isinstance(chosen, ScriptExit):
        return chosen
    return "actions/play-land", {"player": me, "card_id": chosen[0]["id"], "reasoning": reasoning}


def _activate_request(
    move: dict[str, Any],
    state: dict[str, Any],
    me: str,
    reasoning: str,
    *,
    hypo: Hypo | None = None,
    mana: int | None = None,
) -> tuple[str, dict[str, Any]] | ScriptExit:
    # FB2 M4: `ability_text_has`/`ability_index` name the ability exactly one
    # of two ways - the schema already refuses both or neither
    # (`activate-ability-ref`), so exactly one of these is set here.
    phrase = move.get("ability_text_has")
    index_ref = move.get("ability_index")
    present = conditions.presence(state, me)
    if phrase is not None:
        candidates = [
            card
            for card in _zone_cards(state, move["source"]["zone"], me)
            if _ability(card, phrase) is not None
        ]
    else:
        candidates = list(_zone_cards(state, move["source"]["zone"], me))
    chosen = _pick(
        candidates,
        move["source"]["select"],
        "the source to activate",
        move["source"].get("prefer"),
        present=present,
        ties=move["source"].get("ties"),
        hypo=hypo,
        mana=mana,
    )
    if isinstance(chosen, ScriptExit):
        return chosen
    source = chosen[0]
    if phrase is not None:
        ability = _ability(source, phrase) or {}
    else:
        abilities = source.get("abilities") or []
        if index_ref >= len(abilities):
            return ScriptExit(
                Exit.UNFORESEEN,
                stop_reason=(
                    f"{source.get('name') or source['id']} has no ability at index "
                    f"{index_ref} ({len(abilities)} on it)"
                ),
            )
        ability = abilities[index_ref]
    body: dict[str, Any] = {
        "player": me,
        "card_id": source["id"],
        "ability_index": ability.get("index", index_ref),
        "reasoning": reasoning,
    }
    targets = _targets(move, state, me, hypo=hypo, mana=mana)
    if isinstance(targets, ScriptExit):
        return targets
    if targets:
        body["targets"] = targets
    sacrifice = move.get("sacrifice")
    if sacrifice == "source":
        # "source" names the ability's own source as what its cost spends.
        # Almost always a literal CR 701.21 sacrifice (or, when the cost text
        # says "discard" instead, a discard - CR 118.3's `discarding`, never
        # sacrificing what was never on the battlefield to begin with). But
        # an off-zone cycling/unearth/transmute family (CR 702.29a/702.84a/
        # 702.53a) spells its whole ability out as "[cost], Discard this
        # card: ..." with nothing left to name - `off_zone_activation`
        # refuses any `sacrificing`/`discarding` given at all for one of
        # those, so "this card" in the ability's own cost text (never a
        # phrase a battlefield "Sacrifice/Discard a/another ..." cost uses,
        # which always names a selector instead) means send neither.
        cost_text = str(ability.get("cost") or "").casefold()
        if "this card" not in cost_text:
            key = (
                "discarding"
                if "discard" in cost_text and "sacrifice" not in cost_text
                else "sacrificing"
            )
            body[key] = [source["id"]]
    elif isinstance(sacrifice, dict):
        sacrificed = _pick(
            _zone_cards(state, "battlefield", me),
            sacrifice["select"],
            "the permanent to sacrifice",
            sacrifice.get("prefer"),
            sacrifice.get("count") or sacrifice_count(ability),
            present=present,
            ties=sacrifice.get("ties"),
            hypo=hypo,
            mana=mana,
        )
        if isinstance(sacrificed, ScriptExit):
            return sacrificed
        body["sacrificing"] = [card["id"] for card in sacrificed]
    discard = move.get("discard")
    if isinstance(discard, dict):
        # Unlike `sacrifice`, there is no "source" keyword to branch on here
        # - CR 701.9's discard only ever reads from hand, so a `discard`
        # dict always means "a hand card chosen by selector", never the
        # ability's own source (that case, "Discard this card", is
        # `sacrifice`'s own "source" value, handled above).
        discarded = _pick(
            _zone_cards(state, "hand", me),
            discard["select"],
            "the card to discard",
            discard.get("prefer"),
            discard.get("count") or discard_count(ability),
            present=present,
            ties=discard.get("ties"),
            hypo=hypo,
            mana=mana,
        )
        if isinstance(discarded, ScriptExit):
            return discarded
        body["discarding"] = [card["id"] for card in discarded]
    # B7-10 (B5-05 variant): a play never names its own mana sources, so the
    # request names none and lets the engine auto-pay (CR 601.2g) - matching
    # the CLI/MCP semantics of #32. Naming every untapped source here made an
    # audit row look like it paid with sources that never tapped.
    return "actions/activate", body


def _opponent_name(state: dict[str, Any], me: str) -> str | None:
    """The other player's username - a `Defender`/`PlayerTarget` naming
    `opponent` resolves to this (2-player games only, the same assumption
    `_on_side` already makes throughout this module)."""
    return next(
        (
            str(player.get("username"))
            for player in state.get("players") or []
            if player.get("username") != me
        ),
        None,
    )


def _pick_many(
    cards: list[dict[str, Any]],
    picker: dict[str, Any],
    what: str,
    *,
    present: Present | None = None,
    hypo: Hypo | None = None,
    mana: int | None = None,
) -> list[dict[str, Any]] | ScriptExit:
    """`count` (or, for an `AttackGroup`, `all`) cards matching `picker`'s
    own `select`, narrowed by `prefer`, from `cards` (FB2 M2/M3) - `_pick`'s
    own rules for everything except `count == "all"`, which takes every
    remaining candidate after `prefer` narrows them (there is no ambiguity to
    raise: nothing is left unchosen for `all` to be tied over)."""
    select = picker.get("select") or {}
    prefer = picker.get("prefer")
    count = picker.get("count", 1)
    if count != "all":
        return _pick(
            cards,
            select,
            what,
            prefer,
            int(count),
            present=present,
            ties=picker.get("ties"),
            hypo=hypo,
            mana=mana,
        )
    found = [
        card for card in cards if matches(select, card, present, untapped_mana=mana, hypo=hypo)
    ]
    if not found:
        return ScriptExit(Exit.UNFORESEEN, stop_reason=f"nothing matched {what}")
    for preference in prefer or []:
        narrowed = [
            card
            for card in found
            if matches(preference, card, present, untapped_mana=mana, hypo=hypo)
        ]
        if narrowed:
            found = narrowed
    return sorted(found, key=lambda card: str(card.get("name") or card.get("card_name")))


def _resolve_defender(
    defender: dict[str, Any] | None,
    state: dict[str, Any],
    me: str,
    *,
    present: Present | None = None,
    hypo: Hypo | None = None,
    mana: int | None = None,
) -> str | ScriptExit:
    """An `AttackGroup`'s own `defender` (FB2 M2): left out, or naming
    `player: opponent`, is the opponent's username; a `select`/`side` names a
    permanent of theirs (a planeswalker or battle) by the same pick rule
    every other selector-driven choice uses."""
    if defender is None or "player" in defender:
        opponent = _opponent_name(state, me)
        if opponent is None:
            return ScriptExit(Exit.UNFORESEEN, stop_reason="no opponent to declare as a defender")
        return opponent
    pool = [
        card
        for card in state.get("battlefield") or []
        if _on_side(card.get("controller"), "opponent", me)
    ]
    chosen = _pick(pool, defender["select"], "the defender", present=present, hypo=hypo, mana=mana)
    if isinstance(chosen, ScriptExit):
        return chosen
    return str(chosen[0]["id"])


def _attack_request(
    move: dict[str, Any],
    state: dict[str, Any],
    me: str,
    reasoning: str,
    *,
    hypo: Hypo | None = None,
    mana: int | None = None,
) -> tuple[str, dict[str, Any]] | ScriptExit:
    """`POST .../actions/attack`'s body for a `declare_attackers` move (FB2
    M2, CR 508.1) - the same route the seat's own `declare_attackers` tool
    and `BaselineSeat.attack_declaration` send to. Each `AttackGroup` is
    resolved over this seat's own untapped creatures that can attack
    (`baseline_seat.attack_candidates`: creature, untapped, not summoning
    sick - CR 302.6), in turn, so two groups never declare the same creature
    twice."""
    present = conditions.presence(state, me)
    pool = attack_candidates(state, me)
    used: set[str] = set()
    pairs: list[dict[str, str]] = []
    for index, group in enumerate(move.get("attackers") or []):
        available = [card for card in pool if str(card["id"]) not in used]
        chosen = _pick_many(
            available, group, f"attack group {index + 1}", present=present, hypo=hypo, mana=mana
        )
        if isinstance(chosen, ScriptExit):
            return chosen
        defender = _resolve_defender(
            group.get("defender"), state, me, present=present, hypo=hypo, mana=mana
        )
        if isinstance(defender, ScriptExit):
            return defender
        for card in chosen:
            used.add(str(card["id"]))
            pairs.append({"card_id": card["id"], "defender": defender})
    return "actions/attack", {"attackers": pairs, "reasoning": reasoning}


def _attacking_cards(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Every battlefield permanent this turn's `combat.attacks` names as an
    attacker, as card views (FB2 M3's `Block.attacker` own candidate pool)."""
    by_id = {str(card.get("id")): card for card in state.get("battlefield") or []}
    ids = [
        str(attack.get("attacker")) for attack in (state.get("combat") or {}).get("attacks") or []
    ]
    return [by_id[card_id] for card_id in ids if card_id in by_id]


def _block_request(
    move: dict[str, Any],
    state: dict[str, Any],
    me: str,
    reasoning: str,
    *,
    hypo: Hypo | None = None,
    mana: int | None = None,
) -> tuple[str, dict[str, Any]] | ScriptExit:
    """`POST .../actions/declare-blockers`'s body for a `declare_blockers`
    move (FB2 M3, CR 509.1) - the same route the seat's own
    `declare_blockers` tool and `BaselineSeat.block_declaration` send to.
    Each `Block`'s blocker(s) come from this seat's own untapped creatures
    (`baseline_seat.block_candidates`: creature, untapped - summoning
    sickness does not stop a block, CR 509.1a), one attacker at a time, so
    two blocks never reuse a blocker."""
    present = conditions.presence(state, me)
    blocker_pool = block_candidates(state, me)
    attacker_pool = _attacking_cards(state)
    used: set[str] = set()
    pairs: list[dict[str, str]] = []
    for index, block in enumerate(move.get("blocks") or []):
        attacker_pick = block["attacker"]
        attacker = _pick(
            attacker_pool,
            attacker_pick["select"],
            f"block {index + 1}'s attacker",
            attacker_pick.get("prefer"),
            present=present,
            ties=attacker_pick.get("ties"),
            hypo=hypo,
            mana=mana,
        )
        if isinstance(attacker, ScriptExit):
            return attacker
        available = [card for card in blocker_pool if str(card["id"]) not in used]
        blockers = _pick_many(
            available,
            block["blocker"],
            f"block {index + 1}'s blocker",
            present=present,
            hypo=hypo,
            mana=mana,
        )
        if isinstance(blockers, ScriptExit):
            return blockers
        for card in blockers:
            used.add(str(card["id"]))
            pairs.append({"blocker": card["id"], "blocked": attacker[0]["id"]})
    return "actions/declare-blockers", {"player": me, "blocks": pairs, "reasoning": reasoning}


def sacrifice_count(ability: dict[str, Any]) -> int:
    """How many permanents the ability's cost sacrifices: the number its
    "Sacrifice <n> ..." piece names, one for `a`/`another` or no such piece."""
    found = SACRIFICE_NUMBER.search(str(ability.get("cost") or ""))
    if found is None:
        return 1
    word = found.group(1).lower()
    return int(word) if word.isdigit() else NUMBER_WORDS[word]


def discard_count(ability: dict[str, Any]) -> int:
    """How many cards the ability's cost discards: the number its
    "Discard <n> ..." piece names (CR 701.9), one for `a`/`another` or no
    such piece."""
    found = DISCARD_NUMBER.search(str(ability.get("cost") or ""))
    if found is None:
        return 1
    word = found.group(1).lower()
    return int(word) if word.isdigit() else NUMBER_WORDS[word]


#: `CardTarget.zone` left out defaults to battlefield+stack (FB2 T1) -
#: today's behaviour, before `zone` existed at all.
DEFAULT_TARGET_ZONES = ("battlefield", "stack")


def _zone_pool(state: dict[str, Any], zone: str, side: str, me: str) -> list[dict[str, Any]]:
    """Every card in `zone` on `side`, for a `CardTarget`'s own candidate
    pool (FB2 T1) - the same zones `CardTarget.zone`'s closed vocabulary
    names (battlefield, stack, graveyard, exile), read the way `_on_side`
    already reads a controller/owner for every other selector-driven pick."""
    if zone in ("battlefield", "stack"):
        return [
            card for card in state.get(zone) or [] if _on_side(card.get("controller"), side, me)
        ]
    if zone == "graveyard":
        return [
            card
            for owner, cards in (state.get("graveyards") or {}).items()
            for card in cards or []
            if _on_side(owner, side, me)
        ]
    if zone == "exile":
        return [
            card
            for card in state.get("exile") or []
            if _on_side(card.get("owner") or card.get("controller"), side, me)
        ]
    return []


def _target_pool(
    state: dict[str, Any], side: str, me: str, zones: list[str] | None
) -> list[dict[str, Any]]:
    """A `CardTarget`'s own candidate pool, across every zone it names (FB2
    T1) - `zone` left out reads `DEFAULT_TARGET_ZONES`, today's behaviour."""
    pool: list[dict[str, Any]] = []
    for zone in zones or DEFAULT_TARGET_ZONES:
        pool.extend(_zone_pool(state, zone, side, me))
    return pool


def _targets(
    move: dict[str, Any],
    state: dict[str, Any],
    me: str,
    *,
    hypo: Hypo | None = None,
    mana: int | None = None,
) -> list[str] | ScriptExit:
    """The route's `targets` for a move, in order: a card target is the id
    its selector picks on its side, in its own zone(s) (FB2 T1 - left out,
    battlefield and stack), a player target that player's name. Both use
    `_pick`'s rules, `prefer`/`ties` (FB2 T1) included."""
    chosen = []
    for index, target in enumerate(move.get("targets") or []):
        if "player" in target:
            names = [str(player.get("username")) for player in state.get("players") or []]
            pool = [
                {"id": name, "name": name} for name in names if _on_side(name, target["player"], me)
            ]
            select: dict[str, Any] = {}
            prefer, ties = None, None
        else:
            zone = target.get("zone")
            pool = _target_pool(state, target["side"], me, [zone] if zone else None)
            select = target["select"]
            prefer, ties = target.get("prefer"), target.get("ties")
        picked = _pick(
            pool,
            select,
            f"target {index + 1}",
            prefer,
            present=conditions.presence(state, me),
            ties=ties,
            hypo=hypo,
            mana=mana,
        )
        if isinstance(picked, ScriptExit):
            return picked
        chosen.append(picked[0]["id"])
    return chosen


def _decision_targets_answer(
    targets: list[dict[str, Any]],
    pending: dict[str, Any],
    state: dict[str, Any],
    me: str,
    *,
    hypo: Hypo | None = None,
    mana: int | None = None,
) -> list[str] | ScriptExit:
    """A `choose_targets` answer's own `targets` (FB2 A2): each entry is a
    `CardTarget` (its own `select`/`prefer`/`ties`/`zone`, FB2 T1) or a
    `PlayerTarget`, resolved the same way `_targets` resolves a cast/
    activate move's own targets - but narrowed to what the decision actually
    offers (`pending.targets`, `_target_view`'s `{kind, id}` refs), since
    this decision's candidates are a fixed set the engine already chose, not
    the whole board."""
    offered = pending.get("targets") or []
    offered_players = {str(ref.get("id")) for ref in offered if ref.get("kind") == "player"}
    offered_cards = {str(ref.get("id")) for ref in offered if ref.get("kind") != "player"}
    present = conditions.presence(state, me)
    chosen: list[str] = []
    for index, target in enumerate(targets):
        what = f"target {index + 1}"
        if "player" in target:
            names = [str(player.get("username")) for player in state.get("players") or []]
            pool = [
                name
                for name in names
                if name in offered_players and _on_side(name, target["player"], me)
            ]
            if not pool:
                return ScriptExit(Exit.UNFORESEEN, stop_reason=f"nothing matched {what}")
            chosen.append(pool[0])
            continue
        zone = target.get("zone")
        pool = [
            card
            for card in _target_pool(state, target["side"], me, [zone] if zone else None)
            if str(card.get("id")) in offered_cards
        ]
        picked = _pick(
            pool,
            target["select"],
            what,
            target.get("prefer"),
            present=present,
            ties=target.get("ties"),
            hypo=hypo,
            mana=mana,
        )
        if isinstance(picked, ScriptExit):
            return picked
        chosen.append(str(picked[0]["id"]))
    return chosen


def cast_body(
    move: dict[str, Any],
    state: dict[str, Any],
    me: str,
    reasoning: str,
    *,
    hypo: Hypo | None = None,
    mana: int | None = None,
) -> dict[str, Any] | ScriptExit:
    """`POST .../actions/cast`'s body for a `cast` move, read off the view.
    A `cost` other than `normal` is the alternative cost it is announced
    under: the keyword, or for `printed` the card's name, which is how the
    engine names a card's own CR 118.9 cost."""
    zone = move["card"]["zone"]
    chosen = _pick(
        _zone_cards(state, zone, me),
        move["card"]["select"],
        "the card to cast",
        move["card"].get("prefer"),
        present=conditions.presence(state, me),
        ties=move["card"].get("ties"),
        hypo=hypo,
        mana=mana,
    )
    if isinstance(chosen, ScriptExit):
        return chosen
    targets = _targets(move, state, me, hypo=hypo, mana=mana)
    if isinstance(targets, ScriptExit):
        return targets
    body: dict[str, Any] = {
        "player": me,
        "card_id": chosen[0]["id"],
        "from_zone": zone,
        "reasoning": reasoning,
    }
    cost = move.get("cost")
    if cost is not None and cost != "normal":
        body["alternative_cost"] = chosen[0]["name"] if cost == "printed" else cost
    if targets:
        body["targets"] = targets
    # B7-10 (B5-05 variant): no sources named, so the engine auto-pays.
    return body


def decide_body(
    item: dict[str, Any],
    pending: dict[str, Any],
    state: dict[str, Any],
    me: str,
    reasoning: str,
    *,
    hypo: Hypo | None = None,
    mana: int | None = None,
) -> dict[str, Any] | ScriptExit:
    """`POST .../decide`'s body for one answers item, carrying the pending
    decision's own `generation`."""
    kind = pending.get("kind")
    body: dict[str, Any] = {"player": me, "reasoning": reasoning}
    if pending.get("generation") is not None:
        body["generation"] = pending["generation"]
    if kind in AMOUNT_DECISION_KINDS and "amount" in item:
        answer_field = pending.get("answer_field") or ANSWER_FIELDS[kind]
        body[answer_field] = pending.get("amount") if item["amount"] == "max" else item["amount"]
        return body
    if kind in CHOICE_DECISION_KINDS and "choice" in item:
        # O6: choose_a_mode is always fully enumerated (`pending.options`);
        # choose_a_quality sometimes is not (a creature type, a card name -
        # CR 614.12a's open questions), and an empty `options` there means
        # any legal value is accepted, left to the engine to judge.
        options = pending.get("options") or []
        if options and item["choice"] not in options:
            return ScriptExit(
                Exit.UNFORESEEN,
                stop_reason=(
                    f"{item['choice']!r} is not among the offered options for the "
                    f"{kind} decision ({', '.join(options)})"
                ),
            )
        body["choice"] = item["choice"]
        return body
    cards = _decision_cards(pending, state, me)
    present = conditions.presence(state, me)
    if kind == "choose_targets" and item.get("targets") is not None:
        # FB2 A2: `targets` is `choose_targets`'s own alternative to `pick` -
        # the one that can also name a player, the same `CardTarget`/
        # `PlayerTarget` shapes a cast/activate move's own `targets` use.
        chosen_ids = _decision_targets_answer(
            item["targets"], pending, state, me, hypo=hypo, mana=mana
        )
        if isinstance(chosen_ids, ScriptExit):
            return chosen_ids
        body["targets"] = chosen_ids
        return body
    pick = item.get("pick")
    if pick is not None:
        if kind == "choose_targets":
            by_id = {
                str(card.get("id")): card
                for card in [*(state.get("battlefield") or []), *(state.get("stack") or [])]
            }
            pool = [
                by_id[str(ref.get("id"))]
                for ref in pending.get("targets") or []
                if str(ref.get("id")) in by_id
            ]
            field_name = "targets"
        else:
            pool, field_name = cards, "card_ids"
        chosen = _pick(
            pool,
            pick["select"],
            f"the {kind} answer",
            pick.get("prefer"),
            pick.get("count", 1),
            present=present,
            ties=pick.get("ties"),
            hypo=hypo,
            mana=mana,
        )
        if isinstance(chosen, ScriptExit):
            return chosen
        body[field_name] = [card["id"] for card in chosen]
        return body
    if "accept" in item:
        # Item 1: a play's own `accept: True` for a MAY/PAY_LIFE/PAY_ENERGY/
        # PAY_MANA decision (CR 608.2d) is illegal when the engine's own view
        # says the yes branch is not legal (`affirmative_is_legal: False`,
        # e.g. Ugin's Labyrinth's imprint with no colorless mv >= 7 card in
        # hand). The engine always accepts "no" for these kinds, so answering
        # no rather than sending the illegal yes keeps the run going instead
        # of exiting REFUSED with the land already down.
        accept = bool(item["accept"])
        if (
            accept
            and kind in ("may", "pay_life", "pay_energy", "pay_mana")
            and not pending.get("affirmative_is_legal", True)
        ):
            accept = False
        body["accept"] = accept
        return body
    if kind in ("surveil", "scry"):
        moving = item.get("to_graveyard") if kind == "surveil" else item.get("to_bottom")
        # A bare answer (no `to_graveyard`/`to_bottom` selector) is schema-valid
        # and means "keep everything on top": `moved` is empty, not absent -
        # B7-07, the shape `fetch-surveil-land-keep` uses.
        moved_ids = (
            [
                card["id"]
                for card in cards
                if matches(moving, card, present, untapped_mana=mana, hypo=hypo)
            ]
            if moving is not None
            else []
        )
        body["moved"] = moved_ids
        tiers = item.get("top_order")
        if tiers:
            # FB2 A1: tiers ordering the cards that stay on top, first tier
            # on top - the same fan-out `prefer` gets, applied over what is
            # left once `moved` is set aside, with the rest after them in
            # looked-at order.
            moved_set = {str(card_id) for card_id in moved_ids}
            remaining = [card for card in cards if str(card["id"]) not in moved_set]
            ordered: list[dict[str, Any]] = []
            for tier in tiers:
                matched = [
                    card
                    for card in remaining
                    if matches(tier, card, present, untapped_mana=mana, hypo=hypo)
                ]
                ordered.extend(matched)
                remaining = [card for card in remaining if card not in matched]
            ordered.extend(remaining)
            body["top_order"] = [card["id"] for card in ordered]
        else:
            body["top_order"] = None
        return body
    if kind == "put_on_top_or_bottom" and item.get("position") is not None:
        # FB2 A1: `position` names top/bottom directly, an alternative to
        # `to_bottom` for the one card this decision ever offers - the
        # schema already refuses both together (`answer-shape-mismatch`).
        body["top"] = item["position"] == "top"
        return body
    if kind == "put_on_top_or_bottom" and item.get("to_bottom") is not None and cards:
        body["top"] = not matches(
            item["to_bottom"], cards[0], present, untapped_mana=mana, hypo=hypo
        )
        return body
    if kind == "put_into_hand_or_graveyard" and "to_hand" in item:
        # Dina's Guidance's "put it into your hand or graveyard" (CR 608.2d):
        # `True` for the hand, `False` for the graveyard.
        body["to_hand"] = bool(item["to_hand"])
        return body
    if kind == "choose_pile" and "pile_one" in item:
        # CR 700.3's pile primitive - a plain boolean, the same shape
        # `put_on_top_or_bottom` already is: `True` for pile one
        # (`pending_decision.cards`), `False` for its complement.
        body["pile_one"] = bool(item["pile_one"])
        return body
    if kind == "choose_players" and "players" in item:
        # CR 701.34a's own Proliferate player half: each "me"/"opponent"
        # token resolves to a real username the same way `_targets`' own
        # `PlayerTarget` already does, restricted to whoever the decision
        # actually offered (`pending.players`) - a script naming a side
        # nothing offered simply chooses nobody on that side, matching every
        # other answer shape's "chosen must be a subset of offered" posture.
        offered = {str(username) for username in pending.get("players") or ()}
        all_names = [str(player.get("username")) for player in state.get("players") or []]
        chosen_players: list[str] = []
        for side in item["players"]:
            for name in all_names:
                if name in offered and name not in chosen_players and _on_side(name, side, me):
                    chosen_players.append(name)
        body["usernames"] = chosen_players
        return body
    return ScriptExit(
        Exit.UNFORESEEN,
        stop_reason=f"the answer for the {kind} decision gives nothing that answers it",
    )


def _decision_cards(
    pending: dict[str, Any], state: dict[str, Any], me: str
) -> list[dict[str, Any]]:
    """The decision's candidates as card views a selector can read.

    `card_details` where the server sent them; else the same id in a zone
    this seat can see; else, for a card in this seat's own hidden library or
    sideboard (a search, a surveil), the characteristics of the entry its
    `card_labels` label names in the seat's own `library_contents` /
    `sideboard_contents` (`include=contents`). The label is only the join to
    those characteristics - nothing is decided by it. A candidate none of
    these resolve keeps just its id and label, and no characteristic matches it.
    """
    details = {
        str(card.get("id")): card
        for card in pending.get("card_details") or []
        if isinstance(card, dict)
    }
    visible = [*(state.get("battlefield") or []), *(state.get("stack") or [])]
    visible += list(state.get("exile") or [])
    for zone in ("hands", "graveyards"):
        for cards in (state.get(zone) or {}).values():
            visible += list(cards or [])
    by_id = {str(card.get("id")): card for card in visible if isinstance(card, dict)}
    by_label: dict[str, dict[str, Any]] = {}
    for key in ("library_contents", "sideboard_contents"):
        for entry in (state.get(key) or {}).get(me) or []:
            card = entry.get("card") if isinstance(entry, dict) else None
            if isinstance(card, dict) and card.get("name"):
                by_label.setdefault(str(card["name"]), card)
    labels = list(pending.get("card_labels") or [])
    out = []
    for index, card_id in enumerate(pending.get("cards") or []):
        key = str(card_id)
        label = labels[index] if index < len(labels) else None
        card = details.get(key) or by_id.get(key)
        if card is None and label in by_label:
            card = {**by_label[label], "id": key}
        out.append(card if card is not None else {"id": key, "name": label})
    return out


def _pick(
    cards: list[dict[str, Any]],
    select: dict[str, Any],
    what: str,
    prefer: list[dict[str, Any]] | None = None,
    count: int = 1,
    present: Present | None = None,
    ties: str | None = None,
    *,
    hypo: Hypo | None = None,
    mana: int | None = None,
) -> list[dict[str, Any]] | ScriptExit:
    """`count` cards matching `select`, narrowed by each `prefer` that still
    leaves some. Same-named candidates are interchangeable; different names
    left tied are `ambiguous`, unless `ties == "any"` (O2), which instead
    takes the first `count` of them by sorted name. `present` is the board a
    `missing_from` compares against; `hypo`/`mana` are O4/O7's engine facts
    and untapped-mana count, for a `select`/`prefer` naming `enters_any`,
    `unlocks`, `unlocks_by_search`, `unlocks_by_basic_search` or
    `affordable` - the caller fetches
    `hypo` only when the move actually needs it (`needs_hypothetical`)."""
    found = [
        card for card in cards if matches(select, card, present, untapped_mana=mana, hypo=hypo)
    ]
    if not found:
        return ScriptExit(Exit.UNFORESEEN, stop_reason=f"nothing matched {what}")
    for preference in prefer or []:
        narrowed = [
            card
            for card in found
            if matches(preference, card, present, untapped_mana=mana, hypo=hypo)
        ]
        if narrowed:
            found = narrowed
    names = sorted({str(card.get("name") or card.get("card_name")) for card in found})
    if len(found) > count and len(names) > 1:
        if ties != "any":
            return ScriptExit(
                Exit.AMBIGUOUS,
                ambiguous_names=names,
                stop_reason=f"different cards tie for {what}",
            )
        found = sorted(found, key=lambda card: str(card.get("name") or card.get("card_name")))
    if len(found) < count:
        # `count` is a floor this pick must meet, not a ceiling `found[:count]`
        # may silently truncate down to - the caller asked for `count` and
        # building a request with fewer used to send an under-sized answer
        # the engine could not always tell was wrong (Goblin Bombardment's
        # own cost only ever needs 1 sacrifice, so a script's own `count: 2`
        # picking just 1 still reads as a fully legal payment to the engine,
        # silently downgrading the script's stated intent with no error
        # anywhere). Fail closed here instead, before any request is built.
        return ScriptExit(
            Exit.UNFORESEEN,
            stop_reason=f"only found {len(found)} of the {count} needed for {what}",
        )
    return found[:count]


def _zone_cards(state: dict[str, Any], zone: str, me: str) -> list[dict[str, Any]]:
    if zone in ("battlefield", "stack"):
        return [card for card in state.get(zone) or [] if card.get("controller") == me]
    if zone == "hand":
        return list((state.get("hands") or {}).get(me) or [])
    if zone == "graveyard":
        return list((state.get("graveyards") or {}).get(me) or [])
    if zone == "exile":
        return [
            card
            for card in state.get("exile") or []
            if (card.get("owner") or card.get("controller")) == me
        ]
    return []


def _ability(card: dict[str, Any], phrase: str) -> dict[str, Any] | None:
    folded = phrase.casefold()
    return next(
        (
            ability
            for ability in card.get("abilities") or []
            if folded in str(ability.get("text") or "").casefold()
        ),
        None,
    )


def _on_side(controller: str | None, side: str, me: str) -> bool:
    if side == "me":
        return controller == me
    if side == "opponent":
        return controller is not None and controller != me
    return controller is not None


# -- conditions, when, archetypes ----------------------------------------------


def _evaluate(ctx: _Context, play: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    view = ctx.seat.fetch_state(include_contents=True) if conditions.needs_contents(play) else state
    hypo = (
        ctx.hypothetical.get(ctx.seat, view, horizon=conditions.needs_next_turn(play))
        if conditions.needs_hypothetical(play)
        else None
    )
    return conditions.evaluate(play, view, ctx.me, ctx.inputs, ctx.catalog, hypothetical=hypo)


def _condition_exit(result: dict[str, Any]) -> ScriptExit | None:
    """The exit one `conditions.evaluate` result forces, or None when the
    script applies."""
    invalid = result.get("invalid_input")
    if invalid is not None:
        return ScriptExit(
            Exit.INVALID_INPUT,
            invalid_input=invalid,
            stop_reason=(
                f"input {invalid['name']!r} = {invalid['value']!r} is invalid: {invalid['reason']}"
            ),
        )
    failing = {
        "list": result["failing_list"],
        "index": result["failing_index"],
        "seen": result["seen"],
    }
    if result["needs_input"] is not None:
        return ScriptExit(
            Exit.NEEDS_INPUT, needs_input=result["needs_input"], failing_condition=failing
        )
    if not result["applies"]:
        return ScriptExit(Exit.NOT_APPLICABLE, failing_condition=failing)
    return None


def _archetype_applies(play: dict[str, Any], asserted: Any) -> bool:
    listed = play["opposing_archetypes"]
    return WILDCARD in listed or (isinstance(asserted, str) and asserted in listed)


def _whose_matches(state: dict[str, Any], when: dict[str, Any], me: str) -> bool:
    active = state.get("active_player")
    if when["whose_turn"] == "mine":
        return active == me
    if when["whose_turn"] == "theirs":
        return active is not None and active != me
    return True


def _in_when(state: dict[str, Any], when: dict[str, Any], me: str) -> bool:
    return (
        step_position(state) in when["steps"]
        and _whose_matches(state, when, me)
        and (when["stack"] == "any" or not state.get("stack"))
    )


def _next_step(state: dict[str, Any], when: dict[str, Any], me: str) -> str:
    """The first of `when.steps` still to come: later this turn when the turn
    is already the right one, else the earliest of them next time round."""
    wanted = [step for step in AUTOPASS_STEPS if step in when["steps"]]
    current = step_position(state)
    if _whose_matches(state, when, me) and current in AUTOPASS_STEPS:
        here = AUTOPASS_STEPS.index(current)
        later = [step for step in wanted if AUTOPASS_STEPS.index(step) > here]
        if later:
            return later[0]
    return wanted[0]


def _position(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "turn": state.get("turn_number"),
        "step": step_position(state),
        "active_player": state.get("active_player"),
        "stack": len(state.get("stack") or []),
    }


# -- advise mode ---------------------------------------------------------------


def _advise(ctx: _Context, run: _Run, play: dict[str, Any], state: dict[str, Any]) -> ScriptExit:
    """Propose move 1 through `Seat._advise_answer` (a dry-run probe plus the
    `/advice` record) and hand the rest back unvalidated.

    `capture_draft=False` (S2 A6 review fix 3, 2026-09-14): an armed script
    firing here is a proposal for right now, never the later window a
    `stage_advice` call staged - capturing it into that empty slot would
    swallow the script's own move (this function would read the resulting
    `{"drafted": True, ...}` shape as `outcome.get("proposed")` missing, and
    exit `REFUSED`) instead of posting it and leaving the staged window for
    its own future moment."""
    seat = ctx.seat
    moves = play["moves"]
    first = moves[0]
    reasoning = _suffixed(ctx, run, f"move 1/{len(moves)} ({first['kind']})")
    if first["kind"] == "answer":
        pending = state.get("pending_decision")
        item = next(
            (a for a in first["answers"] if pending and a["decision"] == pending.get("kind")), None
        )
        if pending is None or not decision_is_mine(pending, state, ctx.me) or item is None:
            return ScriptExit(
                Exit.UNFORESEEN, stop_reason="no decision this move answers is pending"
            )
        if conditions.needs_contents(item):
            state = seat.fetch_state(include_contents=True)
        hypo = (
            ctx.hypothetical.get(ctx.seat, state, horizon=conditions.needs_next_turn(item))
            if conditions.needs_hypothetical(item)
            else None
        )
        mana = conditions.untapped_mana(state, ctx.me)
        body = decide_body(item, pending, state, ctx.me, reasoning, hypo=hypo, mana=mana)
        built: tuple[str, dict[str, Any]] | ScriptExit = (
            body if isinstance(body, ScriptExit) else ("decide", body)
        )
    else:
        if conditions.needs_contents(first):
            state = seat.fetch_state(include_contents=True)
        hypo = (
            ctx.hypothetical.get(ctx.seat, state, horizon=conditions.needs_next_turn(first))
            if conditions.needs_hypothetical(first)
            else None
        )
        mana = conditions.untapped_mana(state, ctx.me)
        built = _move_request(first, state, ctx.me, reasoning, hypo=hypo, mana=mana)
    if isinstance(built, ScriptExit):
        return built
    path, request = built
    answer = run_action(
        seat,
        lambda: seat.call_json(
            "POST", f"/api/games/{seat.game_id()}/{path}", request, capture_draft=False
        ),
        hold_priority=True,
        include_actions=False,
        reasoning=reasoning,
    )
    outcome = answer.get("outcome")
    if not (isinstance(outcome, dict) and outcome.get("proposed") is True):
        return _outcome_exit({"outcome": outcome}) or ScriptExit(
            Exit.REFUSED,
            refusal=str(outcome.get("reason") if isinstance(outcome, dict) else outcome),
            refusal_source="server",
        )
    return ScriptExit(
        Exit.ADVISED,
        proposal=outcome,
        unvalidated=[
            {"index": index + 1, "kind": move["kind"], "move": move}
            for index, move in enumerate(moves)
            if index > 0
        ],
    )


# -- recording -----------------------------------------------------------------


class _Unregistered(Exception):
    """The start route refused the script as `unknown_script`: the corpus
    this runner read is stale and the seat no longer registers it. No run
    began on the ledger, so there is no finish to post for it."""

    def __init__(self, out: ScriptExit) -> None:
        super().__init__(out.refusal)
        self.out = out


def _start(
    ctx: _Context, run: _Run, play: dict[str, Any] | None, state: dict[str, Any]
) -> ScriptExit | None:
    # `turns_taken`, not `turn_number`: `TurnMachine.turn_number` counts
    # rounds of the table and both seats' turns in one round share it, so a
    # `whose_turn: "either"` script that legitimately runs once on this
    # seat's own turn and again on the opponent's turn of the same round
    # (`turn_number` unchanged, `turns_taken` up one) must not collide on
    # the once-per-turn-and-step ledger slot (`records._SCRIPT_RUN_KEY`) -
    # see that module's own docstring for the identical rule already
    # applied to `require_plan`.
    run.turn, run.step = state.get("turns_taken"), step_position(state)
    answer = _post_run(ctx, {**_run_fields(ctx, run, play), "phase": "start"})
    if isinstance(answer, dict) and answer.get("recorded") is True:
        run.started = True
        return None
    if isinstance(answer, dict) and answer.get("code") == "unknown_script":
        raise _Unregistered(
            ScriptExit(Exit.REFUSED, refusal=str(answer.get("reason")), refusal_source="server")
        )
    if isinstance(answer, dict) and answer.get("exit") == Exit.ALREADY_RAN.value:
        run.started = True
        return ScriptExit(
            Exit.ALREADY_RAN,
            prior_run_id=answer.get("prior_run_id"),
            refusal=answer.get("reason"),
            refusal_source="server",
        )
    reason = answer.get("reason") if isinstance(answer, dict) else answer
    return ScriptExit(Exit.REFUSED, refusal=str(reason), refusal_source="server")


def _end(
    ctx: _Context,
    run: _Run,
    play: dict[str, Any] | None,
    out: ScriptExit,
    *,
    record: bool = True,
) -> RunResult:
    """Fill the run's fields into `out`, post the run's finish (and its start,
    if it never got one) unless `record` is off, and hand the result back. A
    start refused `unknown_script` turns off `record`: the ledger began no run,
    so no finish is posted and the server's refusal is the exit."""
    seat = ctx.seat
    state = seat.fetch_state()
    if record and not run.started:
        try:
            refused = _start(ctx, run, play, state)
        except _Unregistered as unregistered:
            out, record = unregistered.out, False
        else:
            if (
                refused is not None
                and refused.exit is Exit.ALREADY_RAN
                and out.exit is not Exit.REFUSED
            ):
                out = refused
    total = len(play["moves"]) if play else 0
    done = run.moves_done
    out.script_id, out.run_id = run.script_id, run.run_id
    out.moves_done, out.moves_total = done, total
    out.scripts_run = list(run.scripts_run)
    if not record:
        return RunResult(True, out, state=state)
    reapplicable = None
    if play is not None:
        # D2: this re-check itself needs hypothetical facts exactly when
        # the run that is ending did - if they were unavailable then, they
        # still are now, so the same failure must not raise a second time
        # out of `_end` (which every path, including the one already
        # handling that failure, calls to close the run out); treated as
        # not reapplicable, the same "not applicable" verdict `_judged`
        # gives a listing entry it cannot judge for the same reason.
        try:
            reapplicable = "true" if _evaluate(ctx, play, state)["applies"] else "false"
        except HypotheticalFactsUnavailable:
            reapplicable = "false"
    seat.recent_log()
    finish = {
        **_run_fields(ctx, run, play),
        "phase": "finish",
        "seq_end": seat.last_seq,
        "moves_done": done,
        "moves_total": total,
        "exit": out.exit.value,
        "stop_reason": out.stop_reason,
        "refusal_text": out.refusal,
        "resumes": run.resumes,
        "wall_ms": run.wall_ms + ctx.elapsed_ms(),
        "reapplicable_after": reapplicable,
    }
    recorded = _post_run(ctx, finish)
    if not (isinstance(recorded, dict) and recorded.get("recorded") is True):
        out.record_refusal = str(recorded.get("reason") if isinstance(recorded, dict) else recorded)
    return RunResult(True, out, state=state)


def _run_fields(ctx: _Context, run: _Run, play: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "parent_run_id": run.parent_run_id,
        "seat": ctx.me,
        "role": "advisor" if ctx.seat.advise else "seat",
        "advise": ctx.seat.advise,
        "script_id": run.script_id,
        "script_sha": run.script_sha,
        "guides_revision": ctx.corpus.get("guides_revision"),
        "priority": play["priority"] if play else None,
        "opposing_archetype_asserted": ctx.inputs.get(BUILTIN_INPUT),
        "turn": run.turn if isinstance(run.turn, int) else 0,
        "step": run.step or "unknown",
        "seq_start": run.seq_start,
        "invoked_via": ctx.invoked_via,
    }


def _post_run(ctx: _Context, body: dict[str, Any]) -> Any:
    seat = ctx.seat
    return seat.call_json("POST", f"/api/games/{seat.game_id()}/script-runs", body)


# -- corpus --------------------------------------------------------------------


#: `_corpus`'s marker for "nothing is registered under this uid" - stashed in
#: `corpora[key]` like a real corpus (S1 review finding 3) so a scriptless
#: seat's every later call skips the HTTP GET too, not just a registered
#: one's. `_UNREGISTERED_REASON` is carried alongside it so a cache hit and a
#: cache miss refuse in the same words. `_UNREGISTERED_AT_KEY` is the
#: `time.monotonic()` reading taken when the marker was written; a cache hit
#: is trusted only for `_UNREGISTERED_TTL_SECONDS` past that (S1 review
#: fix 1) - see `_UNREGISTERED_TTL_SECONDS`'s own docstring for why this must
#: expire at all.
_UNREGISTERED_MARKER = "_unregistered"
_UNREGISTERED_REASON = "_unregistered_reason"
_UNREGISTERED_AT_KEY = "_unregistered_at"

#: How long a genuine 404 ("nothing registered under this uid") is trusted
#: before the next call re-checks with a live GET, monotonic seconds (S1
#: review fix 1). A seat Job can start before its own corpus PUT has landed,
#: or race a retry of it; a permanent negative cache meant that seat never
#: saw scripts for the rest of the game once the very first read lost that
#: race. Only a genuine 404 sets `_UNREGISTERED_MARKER` at all (see below) -
#: a 5xx or an ambiguous timeout is never cached, so this TTL only bounds how
#: long a *real* "not registered" answer is trusted before asking again.
_UNREGISTERED_TTL_SECONDS = 30.0

#: Seat bookkeeping `_corpus`'s `fresh=True` refresh (every `resume`d run)
#: carries forward from the corpus dict it is about to replace - an explicit
#: allow-list, never every underscore-prefixed key (S1 review fix 2). The
#: old blanket rule carried forward *any* private key, `script_tools.
#: _PLAYS_CACHE_KEY` included - a derived cache of the scripts in the *old*
#: corpus - so a seat that re-registered a different corpus and then resumed
#: a run kept serving plays the server no longer had registered. These three
#: names mirror `script_tools._LAST_ASSERTED_KEY`, `_LAST_INPUTS_KEY` and
#: `_RECENT_RUNS_KEY` by hand: `script_tools` already imports this module, so
#: importing the names themselves back here would be circular. Keep this set
#: in sync with those three by hand if they ever change.
_CARRIED_BOOKKEEPING_KEYS = frozenset(
    {"_last_asserted_archetype", "_last_supplied_inputs", "_recent_runs"}
)


def _corpus(
    seat: Seat, corpora: dict[tuple[str, str], dict[str, Any]], *, fresh: bool
) -> dict[str, Any] | ScriptExit:
    key = (seat.game_id(), seat.uid)
    if not fresh and key in corpora:
        cached = corpora[key]
        if cached.get(_UNREGISTERED_MARKER):
            cached_at = cached.get(_UNREGISTERED_AT_KEY)
            age = time.monotonic() - cached_at if isinstance(cached_at, int | float) else None
            if age is not None and age < _UNREGISTERED_TTL_SECONDS:
                return _no_corpus_refusal(str(cached.get(_UNREGISTERED_REASON)))
            # The TTL has passed: fall through to a live GET rather than
            # trust a "not registered" answer that may be stale by now.
        else:
            return cached
    previous = corpora.get(key)
    path = f"/api/games/{key[0]}/seats/{seat.uid}/scripts"
    status, read = seat._request_json("GET", path, None)
    if not isinstance(read, dict) or not isinstance(read.get("scripts"), list):
        reason = read.get("reason") if isinstance(read, dict) else read
        if status == 404:
            # A genuine "nothing registered under this uid" - the only
            # outcome worth caching at all. A 5xx or an ambiguous timeout
            # (`status` is `None`) is not this seat's own fact about
            # itself, it is the transport's, and caching it would starve
            # every later call of a corpus that really is registered
            # (S1 review fix 1).
            corpora[key] = {
                _UNREGISTERED_MARKER: True,
                _UNREGISTERED_REASON: str(reason),
                _UNREGISTERED_AT_KEY: time.monotonic(),
            }
        return _no_corpus_refusal(str(reason))
    if isinstance(previous, dict) and not previous.get(_UNREGISTERED_MARKER):
        for name in _CARRIED_BOOKKEEPING_KEYS:
            if name in previous and name not in read:
                read[name] = previous[name]
    corpora[key] = read
    return read


def _no_corpus_refusal(reason: str) -> ScriptExit:
    return ScriptExit(Exit.REFUSED, refusal=reason, refusal_source="server")


def _registered(corpus: dict[str, Any], script_id: str) -> dict[str, Any] | None:
    return next(
        (
            doc
            for doc in corpus.get("scripts") or []
            if isinstance(doc, dict) and doc.get("id") == script_id
        ),
        None,
    )


def _normalised(raw: dict[str, Any]) -> dict[str, Any] | ScriptExit:
    try:
        return Play.model_validate(raw).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        return _runner_refusal(raw.get("id"), f"the registered script does not validate: {exc}")


def _no_such_script(script_id: str) -> ScriptExit:
    return _runner_refusal(script_id, f"no script {script_id!r} is registered for this seat")


def _runner_refusal(script_id: str | None, why: str) -> ScriptExit:
    return ScriptExit(Exit.REFUSED, script_id=script_id, refusal=why, refusal_source="runner")


#: The refusal text `_advice_only_refusal` always contains - `script_tools.
#: run`'s own signal (a plain substring check, the same idiom
#: `REASONING_PLACEHOLDER in refusal` already uses elsewhere) to surface this
#: one refusal as its own bare string, the way it already surfaces
#: `merged_inputs`' own, rather than the ordinary `{script_exit, state}`
#: envelope a play that actually ran gets.
ADVICE_ONLY_MARKER = "is advice-only: it carries no moves, there is nothing to run"


def _advice_only_refusal(play: dict[str, Any]) -> ScriptExit:
    """The refusal for a play with no moves (API-3; `schema.Play`'s own
    docstring: "advice-only... exists to explain a decision rather than to
    act on one"). Names the play and, since it never acts, gives its own
    `note` as the whole of what it has to say."""
    why = f"{play['id']} {ADVICE_ONLY_MARKER}, and its note is the whole of what it has to say"
    note = play.get("note")
    if note:
        why = f"{why}: {note}"
    return _runner_refusal(play["id"], why)


def _suffixed(ctx: _Context, run: _Run, what: str) -> str:
    return f"{ctx.reasoning} | script {run.script_id}@{run.script_sha[:8]} {what}"
