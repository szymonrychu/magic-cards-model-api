"""The command runner (T7a): replays a mutating api action against the
engine's pause-and-resume machinery (`magic_cards_model.machines.decisions`)
instead of blocking a request thread inside `Game.ask` (see `decisions.py`'s
`GameDecisionProvider`, which stays exactly as it is - this module is
additive only, nothing here is wired into an HTTP route yet, and T7b is what
switches the routes over and removes the old blocking machinery).

**The shape a paused game keeps** (see the engine's own `memory/pause-is-
snapshot-plus-tape.md`): `(committed Game, Command, answers so far)`. There
is no half-applied `Game` kept anywhere - resuming is *replay*, not a
checkpoint: the same command re-runs from the last committed snapshot with
one more answer appended to the tape, and a `Game` mutated by a paused
attempt is thrown away rather than patched, because a game mid-resolution
(a spell partway through applying its effect, a cost half paid) is not a
legal state this engine's own invariants describe.

**`run_command` never mutates the `Game` it is given.** It always works
against an internal deep copy (`_replay_copy`) and either returns that copy,
mutated to completion, as `RunResult.game` (the new committed state a caller
persists next), or discards it entirely on a pause. This is what lets a
caller hold one committed `Game` and make as many independent attempts
against it as it likes - `resume_command`'s own replay-divergence guard
below depends on exactly this: the guard attempt and the real attempt must
each start from the *same*, untouched committed state, not from whatever the
other one left behind.

**`DecisionPending` is a `BaseException`, not an `Exception`** (see the
engine's `memory/decision-pending-is-a-baseexception.md`) - deliberately, so
it passes undisturbed through the bare `except Exception`/`except
ValueError` handlers `Game.resolve_top`, `Game._resolve_ability` and
`machines/cost.py`'s `CostPayment.pay` use to unwind a *failed* resolution.
`run_command` is the one place in this module that catches it, and it
catches it by name - `except DecisionPending`, never a bare `except
Exception` - so a genuine engine failure (an ordinary `ValueError` a request
already has to handle) still propagates instead of being mistaken for a
pause.

**Determinism**, and why replay reproduces the identical pause rather than a
plausibly-similar one: object ids are `uuid5(game_uid, objects_created)`
(`models/game.py`), so replaying the same command from the same committed
snapshot mints the same ids in the same order and asks the same questions in
the same order. `DecisionScript.begin_replay` (called once per attempt, at
the top of `run_command`) is what makes `PendingDecision.seq` restart at 0
for each attempt, matching the `Answer.seq` values a caller assigns from a
previous `pending.seq` - without it, a resumed attempt would inherit
whatever `asked`/`consumed` the copied `Game` already carried and the
numbering would not line up with the tape being replayed.

**The replay-divergence guard** (`resume_command`/`CommandDivergenceError`)
is the safety net that determinism claim depends on actually holding: before
a new answer is ever accepted onto the tape, `resume_command` re-runs the
command from the committed snapshot with the *unmodified* tape and asserts
the pause it lands on again is the exact same `PendingDecision` that was
stored. A mismatch means something became nondeterministic between the
original pause and this resume - the tape would otherwise answer a question
that was never actually asked, silently producing a wrong-but-plausible
game. This mirrors `DecisionScript.take_from_tape`'s own fail-loud stance
(a mismatched tape entry raises rather than being skipped) one level up, at
the command boundary rather than the single-decision boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from magic_cards_model.machines.decisions import Answer, DecisionPending, PendingDecision
from magic_cards_model.models import Game
from pydantic import BaseModel

from . import engine_api
from .schemas import ActivateRequest, CastRequest, PlayLandRequest

logger = logging.getLogger("mcm.commands")


@dataclass(frozen=True)
class Command:
    """One mutating api action, captured completely enough to re-run it
    against a committed `Game` snapshot with a longer answer tape.

    `kind` names an entry in `COMMAND_HANDLERS`. `payload` is that command's
    request body already reduced to plain JSON-safe values (`str`, `int`,
    `float`, `bool`, `None`, and nested `dict`/`list` of the same) - built by
    `Command.build` from a live pydantic request via `model_dump(mode=
    "json")`, never carried as a live object. That is what makes `Command`
    round-trip through `json.dumps`/`json.loads` whole (`to_json`/
    `from_json`), which is what lets it become a postgres column later (T9)
    with no second serialisation layer of its own.
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """This command as a plain, `json.dumps`-safe `dict`."""
        return {"kind": self.kind, "payload": self.payload}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Command:
        """The inverse of `to_json` - accepts the shape `json.loads` hands
        back, including a `payload` that was omitted or `None`."""
        return cls(kind=data["kind"], payload=dict(data.get("payload") or {}))

    @classmethod
    def build(cls, kind: str, request: BaseModel) -> Command:
        """A `Command` from a live request object - `request.model_dump
        (mode="json")` is exactly the JSON-safe payload `from_json` expects
        back, so a caller never hand-assembles `payload` itself."""
        return cls(kind=kind, payload=request.model_dump(mode="json"))


def _run_cast(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.cast_card(game, CastRequest.model_validate(payload))


def _run_play_land(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.play_land(game, PlayLandRequest.model_validate(payload))


def _run_activate_ability(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.activate_ability(game, ActivateRequest.model_validate(payload))


def _run_resolve_top(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.resolve_top(game)


#: Every command kind `run_command` knows how to replay, mapped to the
#: `engine_api` function that actually drives it. Deliberately not every
#: mutating route this API has (see this module's own docstring and the
#: T7a task that built it) - two announcement-style commands that put
#: something on the stack or the battlefield with no engine decision of
#: their own (`cast`, `play_land`), one more announcement-style command
#: whose payment can involve a live choice on some abilities
#: (`activate_ability`), and the one resolution-style command
#: (`resolve_top`) that is where a spell or ability's own CR 608.2d
#: questions actually get asked - the two categories the acceptance tests
#: for this task require at least one of each from. A following task can
#: extend this table the same way `decisions.ANSWERABLE_KINDS` grows, one
#: entry at a time.
COMMAND_HANDLERS: dict[str, Callable[[Game, dict[str, Any]], Any]] = {
    "cast": _run_cast,
    "play_land": _run_play_land,
    "activate_ability": _run_activate_ability,
    "resolve_top": _run_resolve_top,
}


class RunStatus(StrEnum):
    COMPLETED = "completed"
    PAUSED = "paused"


@dataclass(frozen=True)
class RunResult:
    """What one `run_command` call came to.

    `COMPLETED`: `game` is the internal working copy `run_command` made from
    the `Game` it was given, mutated to reflect the command's full effect -
    the new committed state a caller persists next - and `outcome` is
    whatever the engine-driving function itself returned (an
    `engine_api.Outcome`, or a plain `dict` for `resolve_top`).

    `PAUSED`: `game` is `None`. The half-applied working copy this attempt
    was mutating is thrown away rather than returned - there is no legal
    half-applied state to keep (see this module's own docstring) - and
    `pending` is the question a caller stores alongside `(committed game,
    command, tape)` for the next `resume_command` call. `tape` echoes back
    exactly the answers this attempt was given, unchanged, so a caller does
    not have to thread it through separately.
    """

    status: RunStatus
    game: Game | None = None
    outcome: Any = None
    pending: PendingDecision | None = None
    tape: tuple[Answer, ...] = ()


def _replay_copy(game: Game) -> Game:
    """A deep copy of `game` for one replay attempt to mutate freely,
    leaving `game` itself untouched.

    Unlike `engine_api.dry_run_copy`, this deliberately keeps `game_uid`
    rather than minting a fresh one: a dry-run probe must never collide with
    ids the real game mints later, but a replay's entire point is the
    opposite - mint the *same* ids in the *same* order as the attempt being
    replayed, deterministically, from the same `game_uid` (see this module's
    own docstring on determinism).

    `game.decisions.provider` cannot survive `copy.deepcopy` (a live
    `GameDecisionProvider` holds a `threading.Lock` -
    `TypeError: cannot pickle '_thread.lock' object`) - nulled here before
    the copy and restored on `game` right after, the same shape
    `engine_api.dry_run_copy` uses for the same reason. The copy's own
    provider is left `None`, not pointed back at the original mailbox - see
    `dry_run_copy`'s docstring for why that matters, which applies here
    identically.
    """
    provider = game.decisions.provider
    game.decisions.provider = None
    try:
        copy = game.model_copy(deep=True)
    finally:
        game.decisions.provider = provider
    copy.decisions.provider = None
    return copy


def run_command(game: Game, command: Command, tape: Sequence[Answer] = ()) -> RunResult:
    """Run `command` against a fresh copy of `game`, replaying `tape` for
    every decision an earlier, paused attempt at this same command already
    received. `game` itself is never mutated - see this module's own
    docstring.

    Catches `DecisionPending` explicitly, never through a bare `except
    Exception` - it is a `BaseException` precisely so that ordinary handlers
    do not catch it (see this module's own docstring), and this is the one
    place in this module allowed to catch it by name.

    Returns a `COMPLETED` `RunResult` once the command runs to completion
    with `tape` fully consumed (`DecisionScript.finish_replay` asserts
    that - a tape longer than the questions this run actually asked raises,
    the same way a caller feeding it a stale tape would be a bug), or a
    `PAUSED` one the moment a question has no scripted answer, no unconsumed
    tape entry and no provider.
    """
    handler = COMMAND_HANDLERS.get(command.kind)
    if handler is None:
        raise KeyError(f"no command handler registered for kind={command.kind!r}")

    working = _replay_copy(game)
    working.decisions.pause_when_unanswered = True
    working.decisions.begin_replay(tape)
    try:
        outcome = handler(working, command.payload)
    except DecisionPending as exc:
        return RunResult(status=RunStatus.PAUSED, pending=exc.pending, tape=tuple(tape))

    working.decisions.finish_replay()
    return RunResult(status=RunStatus.COMPLETED, game=working, outcome=outcome, tape=tuple(tape))


class CommandDivergenceError(RuntimeError):
    """The replay-divergence guard tripped: re-running `command` from the
    committed snapshot with the tape as it stood *before* a new answer was
    accepted did not reproduce the `PendingDecision` that was stored.

    Raised instead of silently appending the new answer to the tape, because
    the tape is read positionally (`DecisionScript.take_from_tape`) - an
    answer meant for the stored, expected question would otherwise be fed to
    whichever question a diverged replay actually asks next, answering a
    question that was never asked and producing a wrong game that still runs
    to completion. See this module's own docstring, and the engine's
    `memory/pause-is-snapshot-plus-tape.md` and `memory/decision-pending-is-
    a-baseexception.md`.

    `status_code = 409`: the stored pause is stale, not the caller's request
    malformed or missing - the same distinction a concurrent-write conflict
    gets everywhere else in an http api. Not itself raised as an http
    response here (T7a wires no routes) - a caller that turns this into one
    reads `status_code` off it.
    """

    status_code = 409

    def __init__(self, expected: PendingDecision, actual: RunResult) -> None:
        self.expected = expected
        self.actual = actual
        if actual.status is RunStatus.COMPLETED:
            found = "the command completed instead of pausing again"
        else:
            assert actual.pending is not None
            found = (
                f"paused on {actual.pending.decision.kind.value} "
                f"(seq={actual.pending.seq}) for {actual.pending.decision.player} instead"
            )
        message = (
            f"replay diverged: expected to re-pause on "
            f"{expected.decision.kind.value} (seq={expected.seq}) for "
            f"{expected.decision.player}, but {found}"
        )
        super().__init__(message)
        logger.error(message)


def resume_command(
    committed_game: Game,
    command: Command,
    tape: Sequence[Answer],
    expected_pending: PendingDecision,
    new_answer: Answer,
) -> RunResult:
    """Resume a paused command.

    `committed_game` is the last state a caller persisted - never mutated by
    this call, since `run_command` never mutates the `Game` it is given.
    `tape` is every answer already accepted for this command, in order;
    `expected_pending` is the `PendingDecision` that was stored the moment
    `tape` stopped covering the questions this command asks; `new_answer` is
    the answer just given for that exact question.

    The replay-divergence guard runs first, on every call, before
    `new_answer` is accepted onto anything: `command` is replayed from
    `committed_game` with the *unmodified* `tape`, and the resulting pause
    must be exactly `expected_pending` - a different question, a different
    player, or no pause at all (the command completed) raises
    `CommandDivergenceError` instead. Only once that guard passes is
    `new_answer` appended and the command replayed a second time, again from
    `committed_game`, to produce the `RunResult` this function actually
    returns - `COMPLETED` if that was the last question, or `PAUSED` on the
    next one if it was not.

    Each of the two replays is `run_command`'s own fresh copy of
    `committed_game` (see `_replay_copy`), so neither attempt's mutation
    leaks into the other or into `committed_game` itself.
    """
    guard = run_command(committed_game, command, tape)
    if guard.status is not RunStatus.PAUSED or guard.pending != expected_pending:
        raise CommandDivergenceError(expected_pending, guard)

    return run_command(committed_game, command, [*tape, new_answer])
