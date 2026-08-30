"""The live half of CR 608.2d: a decision asked mid-resolution, answered by a
real seat sitting outside the engine's call stack.

The engine (`magic_cards_model.machines.decisions`) asks a question through
`Game.ask`, which - since a decision provider was added there - tries a
scripted answer, then `Game.decisions.provider`, then its own documented
default. `GameDecisionProvider` is that provider for a game served by this
API: `Game.ask` calls `decide()` from *inside* whatever request thread is
mutating the game (casting a spell, advancing a step, anything that can reach
a CR 608.2d question), and that call blocks the thread until an answer
arrives on `DecisionMailbox` or `timeout_seconds` runs out.

**The invariant that makes this safe:** the request that *answers* a pending
decision (`POST /api/games/{id}/decide`) touches only the `DecisionMailbox` -
never the `Game` object, never `store.lock(game_id)`. It cannot, on pain of
deadlock: the asking request is the one holding that lock, parked inside
`Game.ask` waiting on this same mailbox for the answer. If the answering
request tried to take the lock too, it would queue behind the very thread that
is waiting on it, and neither would ever move again. `DecisionMailbox` has its
own, separate lock precisely so the two requests never contend for the same
one.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from magic_cards_model.machines.decisions import (
    NOT_ANSWERED,
    CardSplit,
    Decision,
    DecisionKind,
    default_answer,
)
from magic_cards_model.models.card_state import TargetRef

logger = logging.getLogger("mcm.decisions")

#: How long `Game.ask` will block waiting for a live answer before falling
#: back to the engine's own default (CR 608.2d does not require a timeout -
#: it assumes a player who is there - so this bound exists only because a
#: server request cannot hang forever). Configurable per deployment; a test
#: overrides a specific provider's `.timeout_seconds` directly rather than
#: this module constant, so it can use a short bound without affecting
#: anything else.
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("MCM_DECISION_TIMEOUT_SECONDS", "60"))

#: Every `DecisionKind` that `POST /api/games/{id}/decide` actually knows how
#: to answer - see `app.py`'s `decide` endpoint, which this set must be kept
#: in lockstep with.
#:
#: `DecisionKind.MULLIGAN`, `DECLARE_ATTACKERS`, and `DECLARE_BLOCKERS` are
#: deliberately absent. Reading `machines/decisions.py` and `models/game.py`
#: (`ask_mulligan`, `ask_attackers`, `ask_blockers`, all of which call
#: `DecisionScript.take` and `default_answer` directly) shows none of the
#: three ever consults `Game.decisions.provider` - the mailbox this module
#: publishes to. A live seat cannot answer them through this endpoint no
#: matter what `decide` supports: the mulligan declaration (CR 103.5) is
#: settled during `Match.start_game`, and attacking/blocking (CR 703.4i,
#: 703.4j) already have their own actions (`actions/attack`,
#: `actions/declare-blockers`) that call `Game.declare_attackers` /
#: `Game.declare_blockers` directly. Building `decide` support for them would
#: be speculative: nothing can ever reach it.
ANSWERABLE_KINDS = frozenset(
    {
        DecisionKind.MAY,
        DecisionKind.PAY_LIFE,
        DecisionKind.SCRY,
        DecisionKind.SURVEIL,
        DecisionKind.CHOOSE_CARDS,
        DecisionKind.CHOOSE_TARGETS,
    }
)


def _target_view(ref: TargetRef) -> dict[str, str]:
    """One candidate or chosen target, shaped for JSON.

    A target is a card *or* a player (CR 115.1), so unlike `cards` this cannot
    be a bare list of ids - `{"kind": "player", "id": "bob"}` is how a seat is
    told a target offered a face, not a card. An ability on the stack (CR
    113.1c, CR 115.2) is carried the same way a card is: `TargetRef` names it
    by `card_id` because that is the id field the engine's own `TargetRef`
    happens to use for "an object with an id that is not a player" - see
    `Game.resolve_target`, which tries a card lookup first and an ability
    lookup second for exactly that reason.
    """
    if ref.is_player:
        return {"kind": "player", "id": str(ref.username)}
    return {"kind": "card", "id": str(ref.card_id)}


@dataclass(frozen=True)
class PendingDecision:
    """The question, shaped for a state view rather than for the engine.

    Card identities travel here as strings (JSON has no UUID type). `cards`
    and `amount` are `CHOOSE_CARDS`'s shape; `targets` and `target_maximum`
    are `CHOOSE_TARGETS`'s - a target may be a player or an ability on the
    stack as well as a card (see `_target_view`), and "up to N" (CR 115.6)
    is not the same question as `CHOOSE_CARDS`'s exact `amount`, so it gets
    its own field rather than overloading `amount` with a different meaning.
    Every field here is `visibility.redact_view`'s job to strip for anyone but
    the deciding player and the admin - CR 101.4a lets the choice itself be
    public while what was looked at stays hidden, which is why a target that
    is a face-down permanent or a hidden-zone card must not be offered here to
    a seat that is not the one deciding; `redact_pending_decision` already
    drops this whole payload down to `{player, kind, prompt}` for everyone
    else, so nothing further is needed for that half of the rule.
    """

    player: str
    kind: str
    prompt: str
    source: str | None
    amount: int
    cards: tuple[str, ...]
    targets: tuple[dict[str, str], ...] = ()
    target_maximum: int | None = None
    #: Whether the yes branch of a `may`/`pay_life` question is legal at all
    #: (CR 608.2d, CR 119.4) - `False` at, say, 1 life against a 2-life
    #: payment, which is what a shockland at 1 life looks like before the
    #: deciding seat is even asked. A seat should not be offered "yes" for a
    #: question it cannot legally say yes to.
    affirmative_is_legal: bool = True

    @classmethod
    def from_engine(cls, decision: Decision) -> PendingDecision:
        return cls(
            player=decision.player,
            kind=decision.kind.value,
            prompt=decision.prompt,
            source=decision.source,
            amount=decision.amount,
            cards=tuple(str(card_id) for card_id in decision.cards),
            targets=tuple(_target_view(ref) for ref in decision.targets),
            target_maximum=decision.target_maximum,
            affirmative_is_legal=decision.affirmative_is_legal,
        )

    def to_view(self) -> dict[str, Any]:
        return {
            "player": self.player,
            "kind": self.kind,
            "prompt": self.prompt,
            "source": self.source,
            "amount": self.amount,
            "cards": list(self.cards),
            "targets": [dict(ref) for ref in self.targets],
            "target_maximum": self.target_maximum,
            "affirmative_is_legal": self.affirmative_is_legal,
        }


class DecisionMailbox:
    """One pending question and its answer, for one game.

    A condition variable, not a queue: CR 608.2d never has two questions
    pending on the same player at once (each is announced "while applying the
    effect", one at a time), so "the current question, if any" is the whole
    state this needs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._pending: PendingDecision | None = None
        self._answer: object = None
        self._answered = False

    def publish(self, pending: PendingDecision) -> None:
        """A question just became live. Wakes anyone already waiting on it."""
        with self._condition:
            self._pending = pending
            self._answer = None
            self._answered = False
            self._condition.notify_all()

    def wait_for_answer(self, timeout_seconds: float) -> object:
        """Block until an answer is posted or `timeout_seconds` elapses.

        Returns the answer, or `NOT_ANSWERED` on timeout. Either way the
        mailbox is cleared before returning, so a late answer that arrives
        just after a timeout is simply discarded rather than delivered to the
        next, unrelated question.
        """
        with self._condition:
            answered = self._condition.wait_for(lambda: self._answered, timeout=timeout_seconds)
            value = self._answer if answered else NOT_ANSWERED
            self._pending = None
            self._answer = None
            self._answered = False
            return value

    def answer_if(self, player: str, value: object) -> bool:
        """Post `value` as the answer, if `player` is who was actually asked.

        Returns whether it was accepted. Never touches anything but this
        mailbox's own lock - see the module docstring for why that matters.
        """
        with self._condition:
            if self._pending is None or self._pending.player != player:
                return False
            self._answer = value
            self._answered = True
            self._condition.notify_all()
            return True

    def snapshot(self) -> PendingDecision | None:
        """The question on the table right now, or `None`."""
        with self._condition:
            return self._pending


@dataclass
class GameDecisionProvider:
    """The `DecisionProvider` (see `machines/decisions.py`) for one game.

    Publishes each question to `mailbox`, blocks the calling thread - which is
    whatever request is deep in `Game.ask` - for up to `timeout_seconds`, and
    on timeout logs the default the engine is about to take instead of
    silently letting it happen. `game_id` is carried only for that log line.
    """

    mailbox: DecisionMailbox
    game_id: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def decide(self, decision: Decision) -> object:
        self.mailbox.publish(PendingDecision.from_engine(decision))
        answer = self.mailbox.wait_for_answer(self.timeout_seconds)
        if answer is NOT_ANSWERED:
            self._log_timeout(decision)
        return answer

    def _log_timeout(self, decision: Decision) -> None:
        """Never silent: the default taken, and the rule that permits it.

        Written to both the process log and the game's own audit log, because
        the audit log - not this process's stderr - is what a player reads.

        The wording splits on `ANSWERABLE_KINDS`, because "timed out" hides two
        very different failures. A `pay_life` timeout means a seat *saw* the
        question, in its `pending_decision`, with `POST /decide` able to take
        `{"accept": true}` for it, and simply did not answer in the allotted
        `timeout_seconds` - a live player who was asleep at the wheel, or slow.
        A kind absent from `ANSWERABLE_KINDS` (were one ever routed through
        `Game.ask` in the future without `decide` being taught it) times out
        the same way, but no seat *could* have answered it - `decide` refuses
        every kind outside that set - so blaming the player for "not
        answering" would misdescribe a transport gap as an inattentive human.
        This is exactly the shape of bug this module exists to prevent: see
        the blocking `pay_life` timeout on a shockland's "pay 2 life" that
        this distinction was added to stop from hiding again.
        """
        from . import audit  # lazy: audit imports store, which would cycle

        taken = default_answer(decision)
        taken_ids = [str(card_id) for card_id in taken] if isinstance(taken, tuple) else str(taken)
        rule = "CR 608.2d (no answer within the wait) falls back to the documented default"
        could_answer = decision.kind in ANSWERABLE_KINDS
        verb = (
            "did not answer in time"
            if could_answer
            else "could not answer - no transport wires this decision kind to a live seat"
        )
        line = (
            f"{decision.prompt} timed out after {self.timeout_seconds}s: "
            f"{decision.player} {verb}, so defaulted to {taken_ids} ({rule})"
        )
        logger.warning(line)
        audit.record(
            self.game_id,
            "decision_timeout",
            player=decision.player,
            kind=decision.kind.value,
            prompt=decision.prompt,
            defaulted_to=taken_ids,
            could_answer=could_answer,
            rule=rule,
        )


def bool_answer(accept: bool | None) -> bool:
    """A `decide` request body's `accept`, in the shape `Game.ask` expects for
    `MAY` and `PAY_LIFE` - a bare `bool`.

    Refuses a missing answer with a 400-shaped `ValueError` rather than
    letting `None` reach the engine, where `Game.ask`'s own `isinstance(value,
    bool)` check would raise the same way but inside the *other* request
    thread - the one parked in `Game.ask` - where nobody would ever see it.
    """
    if accept is None:
        raise ValueError("this decision needs a yes/no answer ('accept')")
    return accept


def card_split_answer(moved: list[str], top_order: list[str] | None) -> CardSplit:
    """A `decide` request body's `moved`/`top_order`, in the shape `Game.ask`
    expects for `SCRY` and `SURVEIL` - a `CardSplit` (CR 701.22a, CR 701.25a).

    `moved` names the cards leaving the top - to the bottom for a scry, to the
    graveyard for a surveil; every other card looked at goes back on top, in
    `top_order` if given or unchanged otherwise. Malformed ids are refused
    here with a 400 rather than reaching the engine, matching
    `card_ids_answer`.
    """
    moved_ids = tuple(UUID(card_id) for card_id in moved)
    order_ids = None if top_order is None else tuple(UUID(card_id) for card_id in top_order)
    return CardSplit(moved=moved_ids, top_order=order_ids)


def card_ids_answer(card_ids: list[str]) -> tuple[UUID, ...]:
    """A `decide` request body's `card_ids`, in the shape `Game.ask` expects
    for `CHOOSE_CARDS` - a `tuple[UUID, ...]`, so a malformed id is refused
    with a 400 here rather than reaching the engine at all."""
    return tuple(UUID(card_id) for card_id in card_ids)


def targets_answer(targets: list[str]) -> tuple[TargetRef, ...]:
    """A `decide` request body's `targets`, in the shape `Game.ask` expects
    for `CHOOSE_TARGETS` - a `tuple[TargetRef, ...]`.

    Each token is a card or ability id (a UUID string) or a player's username,
    told apart the same way `TargetRef.coerce` already tells a bare UUID from
    a bare string apart - no different from `card_ids_answer` in that this
    is pure parsing and never touches the `Game`. Whether the parsed reference
    was actually one of the candidates offered is `Game.ask`'s own check, on
    the other side of the mailbox; a malformed token is refused here with a
    400 rather than reaching the engine at all, matching `card_ids_answer`.
    """
    refs = []
    for token in targets:
        try:
            refs.append(TargetRef.to_card(UUID(token)))
        except ValueError:
            refs.append(TargetRef.to_player(token))
    return tuple(refs)
