"""The live half of a mid-resolution decision, answered by a real seat sitting
outside the engine's call stack.

Most of these are CR 608.2d questions ("choices other than choices already
made as part of casting the spell, activating the ability, or otherwise
putting the spell or ability on the stack"), but not all: `CHOOSE_TARGETS` is
exactly the choice 608.2d's own wording carves out - it is choosing targets
*while putting* a triggered ability (CR 603.3d) or a spell (CR 601.2c) on the
stack. `rule_for_kind`, below, is the one place that citation is decided, from
`decision.kind` - never hardcoded elsewhere, which is how this module used to
tell every caller "CR 608.2d" even for a `CHOOSE_TARGETS` decision it was
raising for the opposite reason 608.2d names.

The engine (`magic_cards_model.machines.decisions`) asks a question through
`Game.ask`, which - since a decision provider was added there - tries a
scripted answer, then `Game.decisions.provider`, then its own documented
default. `GameDecisionProvider` is that provider for a game served by this
API: `Game.ask` calls `decide()` from *inside* whatever request thread is
mutating the game (casting a spell, advancing a step, anything that can reach
a live decision), and that call blocks the thread until an answer arrives on
`DecisionMailbox` or `timeout_seconds` runs out.

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
    legal_options,
)
from magic_cards_model.models.card_state import TargetRef

logger = logging.getLogger("mcm.decisions")

#: How long `Game.ask` will block waiting for a live answer, *after* the
#: deciding seat's own state read has actually shown it the question, before
#: falling back to the engine's own default (no CR that reaches this module
#: requires a timeout at all - each assumes a player who is there - so this
#: bound exists only because a server request cannot hang forever).
#:
#: 60s (this constant's old default) was too short for a seat that is
#: instructed to call `knowledge_search`/`resolve_cards` before answering a
#: rules question it is unsure of - two round trips to another MCP server,
#: each with its own latency, comfortably eat a 60s budget before the seat
#: has even decided what to answer. Raised to 5 minutes; still configurable
#: per deployment via `MCM_DECISION_TIMEOUT_SECONDS`, and a test overrides a
#: specific provider's `.timeout_seconds` directly rather than this module
#: constant, so it can use a short bound without affecting anything else.
#:
#: "After delivery", not from `publish()`: `DecisionMailbox.wait_for_answer`
#: waits for the deciding seat's own `GET /api/games/{id}` to have shown it
#: the question (`mark_delivered`, called from `app.py`'s `get_game`) before
#: starting this clock, so a seat that has not yet had the chance to see the
#: decision is never charged for that gap - see that method's docstring.
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("MCM_DECISION_TIMEOUT_SECONDS", "300"))

#: How long to wait for a seat *that is already known to be reading this
#: game* to be shown the question - `DecisionMailbox.wait_for_answer`'s first
#: phase, which used to be given the whole of `DEFAULT_TIMEOUT_SECONDS`.
#:
#: That was the bug this constant exists to fix. The two phases measure two
#: completely different things: the second is "how long may a player think",
#: which is minutes; the first is "how long until this seat's own poll loop
#: comes round again", which is *one poll interval* - the browser UI polls
#: every 1.2s, `seatkit.block_until_wake` faster still. Charging the poll
#: interval 300 seconds meant a game whose seats were not polling at all -
#: every test, `serve.py`'s boot, an AI seat that has not started yet - paid
#: 2x300s for every question the engine asked it, and CR 103.5's mulligan
#: process asks two before the first turn even begins. That is how
#: the pregame came to block for 600-1200s on an ordinary two-decklist game
#: while holding `store.locked(game_id)`. The engine has since turned CR 103.5
#: and CR 103.6 into steps the game *rests* in, so no live game pays that at
#: all any more (`opening.py`) - but the two-phase bug was never specific to
#: the pregame, and this constant is what keeps every other question honest.
#:
#: 10s is roughly eight of the slowest poll interval any client here uses, so
#: a live seat is never cut off by it; it is only ever reached by a seat that
#: has stopped polling mid-game. `MCM_DECISION_DELIVERY_SECONDS` overrides.
DEFAULT_DELIVERY_SECONDS = float(os.environ.get("MCM_DECISION_DELIVERY_SECONDS", "10"))

#: How long to wait for a seat that has **never** read this game's state to
#: turn up - the presence probe, and the one thing that tells a live human
#: apart from an unattended game.
#:
#: A seat proves it is there by fetching `GET /api/games/{id}` with its own
#: uid; `DecisionMailbox.mark_watching` records that. Before the first such
#: read there is no evidence either way, and one question genuinely can beat
#: the first poll - a question raised by the very request that hands a seat
#: its uid cannot have been seen by that seat yet. So the first question put
#: to an unproven seat waits this long for that first read, and no longer. A
#: human at `/play/` polls within milliseconds of the creation response and
#: clears it every time; a test, `serve.py`'s boot, or an AI seat whose
#: subprocess has not spawned yet never does, and is defaulted after this one
#: probe rather than after two full timeouts.
#:
#: Deliberately *not* raised to "comfortably long": a seat that fails the
#: probe is latched absent (see `DecisionMailbox.delivery_bound`), so an
#: unattended game pays this once, not once per question. Raising it would
#: buy nothing for a live player - who is proven present in far less - and
#: cost every unattended game directly. `MCM_DECISION_PRESENCE_SECONDS`
#: overrides.
DEFAULT_PRESENCE_SECONDS = float(os.environ.get("MCM_DECISION_PRESENCE_SECONDS", "3"))

#: Every `DecisionKind` that `POST /api/games/{id}/decide` actually knows how
#: to answer - see `app.py`'s `decide` endpoint, which this set must be kept
#: in lockstep with. `PAY_ENERGY` and `PAY_ENERGY_AMOUNT` (CR 107.14,
#: CR 118.3) and `CHOOSE_A_QUALITY` (CR 614.12a) joined this set after a
#: player's Utopia Sprawl froze a live game: the engine could ask the
#: question, `Game.ask` had nowhere live to route it, and `decide` refused
#: every shape the player tried, so the "as this enters, choose a color"
#: replacement effect never got its answer and the whole match sat stuck
#: behind it. `test_decision_kinds.test_every_decision_kind_is_answerable_or_served_elsewhere`
#: is the regression test: it enumerates the engine's own `DecisionKind` and
#: fails, by name, the moment a new one is added to
#: `magic_cards_model.machines.decisions` without a matching entry here (or
#: in `SERVED_ELSEWHERE`, below) - the exact gap that let this one through.
#: `PAY_MANA` (CR 118.12a, "counter target spell unless its controller pays
#: {N}") is exactly that gap caught again, this time by the regression test
#: itself rather than a live game: it reuses `bool_answer`, the same
#: yes/no shape `PAY_ENERGY` already uses, since it too is a fixed-amount
#: payment. Asked of the *targeted spell's* controller, not the resolving
#: counterspell's caster - see `Game.may_pay_mana`'s docstring.
#: `PUT_ON_TOP_OR_BOTTOM` (Subtlety's "its owner puts it on the top or bottom
#: of their library") is the same gap a third time: the drift test caught it
#: at merge time rather than in a live game, the way `choose_a_quality` was
#: caught the hard way. Not `bool_answer` despite also being a `bool` under
#: the hood - `top_or_bottom_answer` refuses a missing answer the same way,
#: but the field name (`top`) is deliberately its own so a client cannot
#: confuse this top/bottom choice for a yes/no `accept`. Asked of the
#: targeted spell's *owner* (CR 108.3), never of Subtlety's controller - see
#: `machines/effects.py`'s `_parse_choose_up_to_one_target_spell_clauses`.
#: `PAY_LIFE_AMOUNT` (Necrodominance's "you may pay any amount of life", CR
#: 119.4) and `CHOOSE_A_MODE` (Elder Gargaroth's "attacks or blocks, choose
#: one -", CR 603.3c/700.2a) are the same gap a fourth time, caught by the
#: drift test at merge time this time rather than in a live game.
#: `PAY_LIFE_AMOUNT` reuses `PAY_ENERGY_AMOUNT`'s shape exactly - an `int`
#: from 0 up to `Decision.amount` (here the payer's life total rather than
#: their energy) - so `life_amount_answer` is `energy_amount_answer` read
#: for life; a distinct request field (`life_amount`, not `energy_amount`)
#: so a client cannot accidentally pay life with an energy-shaped answer or
#: vice versa. `CHOOSE_A_MODE` reuses `CHOOSE_A_QUALITY`'s shape exactly - a
#: non-empty `str` that must be one of `Decision.options` when any are
#: offered (CR 700.2a's modes are always fully enumerated, unlike a color
#: choice's five letters standing in for an open "any card name"/"any
#: creature type") - so it shares the same request field, `choice`, and
#: `mode_answer` differs from `quality_answer` only in its error text citing
#: CR 700.2a instead of CR 614.12a.
#: `OPENING_HAND_ACTION` (CR 103.6, "some cards allow a player to take actions
#: with them from their opening hand") joined this set when the API started
#: running CR 103.6 at all. It was in `SERVED_ELSEWHERE` before that, on the
#: true-at-the-time grounds that `decks.load_deck_into_game` stopped at
#: `Match.start_game` and nothing ever ran the sequence, so the question was
#: never asked of anyone. It is asked now, through
#: `Game.take_opening_hand_actions`'s
#: `self.ask` - the one path that consults `Game.decisions.provider`, i.e.
#: this API's mailbox - of a game that is already in the store with its seat
#: uids handed out. So a live seat really does see it as its
#: `pending_decision` and really can answer it here. A plain yes/no ("you may
#: begin the game with this card on the battlefield" - CR 103.6a; "you may
#: reveal it from your opening hand" - CR 103.6b), so it reuses `bool_answer`
#: and the `accept` field exactly as `MAY` does.
#: `ORDER_TRIGGERS` (CR 603.3b: "if multiple abilities have triggered since
#: the last time a player received priority ... each player, in APNAP order,
#: puts each triggered ability they control on the stack in any order they
#: choose") joined this set the moment the *engine* grew the kind - caught by
#: the drift test, not by a frozen game, which is the whole reason it was
#: caught early. It was wired here before the engine had an ask site at all,
#: because the commit that added one would otherwise have turned every
#: multi-trigger board into a hang, on a kind no seat could answer. That commit
#: has since landed: `Game._put_pending_triggers_on_stack` hands each
#: controller's block of simultaneous triggers to
#: `_order_triggers_by_controller`, which asks `ask_trigger_order` wherever a
#: controller has two or more, so a live seat reaches this question in ordinary
#: play.
#:
#: The answer is an *order*, not a selection: a permutation of exactly the
#: ability ids offered in `Decision.cards`, **bottom of the stack first**, so
#: the last id in the answer is the ability that resolves first. Its own
#: request field (`order`), not `card_ids`: those ids are `AbilityOnStack.id`s
#: and name no card at all, and "these, in this sequence" is a different
#: question from `CHOOSE_CARDS`'s "these, out of those". Anything that is not
#: a permutation - a dropped ability, a duplicate, a stray id - is refused,
#: matching `ask_trigger_order`'s own refusal: dropping one would be CR 603.3's
#: "its controller puts it on the stack" going unperformed, and adding one
#: would put an ability on the stack that never triggered.
ANSWERABLE_KINDS = frozenset(
    {
        DecisionKind.MAY,
        DecisionKind.OPENING_HAND_ACTION,
        DecisionKind.PAY_LIFE,
        DecisionKind.PAY_ENERGY,
        DecisionKind.PAY_ENERGY_AMOUNT,
        DecisionKind.PAY_LIFE_AMOUNT,
        DecisionKind.PAY_MANA,
        DecisionKind.SCRY,
        DecisionKind.SURVEIL,
        DecisionKind.CHOOSE_CARDS,
        DecisionKind.CHOOSE_TARGETS,
        DecisionKind.CHOOSE_A_QUALITY,
        DecisionKind.CHOOSE_A_MODE,
        DecisionKind.PUT_ON_TOP_OR_BOTTOM,
        DecisionKind.MULLIGAN,
        DecisionKind.ORDER_TRIGGERS,
    }
)

#: `DECLARE_ATTACKERS` and `DECLARE_BLOCKERS` are deliberately absent from
#: `ANSWERABLE_KINDS`. `machines/decisions.py`'s `ask_attackers`/`ask_blockers`
#: call `DecisionScript.take` and `default_answer` directly and never consult
#: `Game.decisions.provider` - the mailbox this module publishes to - so a
#: live seat cannot answer either through `decide` no matter what this API
#: supports. For attacking and blocking (CR 703.4i, 703.4j) that is fine:
#: both have their own actions (`actions/attack`, `actions/declare-blockers`)
#: calling `Game.declare_attackers` / `Game.declare_blockers` directly, so the
#: seat has a real transport, just not this one.
#:
#: `MULLIGAN` used to sit here too, as the one kind with no live transport at
#: all: CR 103.5's declaration was genuinely asked, but `ask_mulligan` never
#: published it to this mailbox, so `pending_decision` could not carry it and
#: every seat silently took the engine default of keeping the hand as dealt.
#: That engine gap is closed: `ask_mulligan` now consults the provider between
#: the script and the default, exactly as `Game.ask` does, so `MULLIGAN` has
#: moved to `ANSWERABLE_KINDS` as an ordinary yes/no.
#:
#: Which path reaches it matters, and both exist. `Game.settle_pregame` - the
#: headless walk of the whole pregame, exposed here as the admin-only
#: `POST /api/games/{id}/pregame/settle` - goes through `ask_mulligan` and so
#: really can put a `mulligan` question in this mailbox for a live seat to
#: answer through `decide`. A seat declaring for *itself* instead uses
#: `POST /api/games/{id}/pregame/mulligan`, which calls
#: `Game.declare_mulligan`: told an answer already made, asking nobody,
#: blocking on nothing. That is the transport a live game here uses, and it is
#: why the pregame no longer costs a decision timeout at all (`opening.py`).
#: Both are real; neither makes the other dead code.
#:
#: `validate_answer` gives it its own branch even so, because the engine-side
#: authority for a mulligan answer is `ask_mulligan`'s own `isinstance` check
#: and *not* `Game.ask` - a `MULLIGAN` decision never reaches `Game.ask` at
#: all, and would fall through that method's kind dispatch to its trailing
#: `CardSplit` branch if it did.
#:
#: The *bottoming* that follows a kept hand (CR 103.5's "puts a number of
#: those cards ... on the bottom of their library") needed nothing new here:
#: the engine asks it as a `CHOOSE_CARDS` decision with `exact=True` and
#: `amount = cards_to_bottom`, and this API has answered and rendered that
#: kind since long before. Its unanswered default is already "the back of the
#: offered cards", which is what `keep_hand(None)` does, so an absent player
#: behaves exactly as before. On the seat-driven path `opening.py`
#: pre-provides that choice from the declaration itself, so it never reaches
#: this mailbox at all.
#:
#: Named here, deliberately, rather than left as an absence - the drift test
#: treats a `DecisionKind` in neither set as the bug, and a kind that belongs
#: in neither must say why.
SERVED_ELSEWHERE: dict[DecisionKind, str] = {
    DecisionKind.DECLARE_ATTACKERS: (
        "use POST /api/games/{game_id}/actions/attack instead of decide"
    ),
    DecisionKind.DECLARE_BLOCKERS: (
        "use POST /api/games/{game_id}/actions/declare-blockers instead of decide"
    ),
}


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
    #: Whether the yes branch of a `may`/`pay_life`/`pay_energy` question is
    #: legal at all (CR 608.2d, CR 119.4, CR 118.3) - `False` at, say, 1 life
    #: against a 2-life payment, which is what a shockland at 1 life looks
    #: like before the deciding seat is even asked. A seat should not be
    #: offered "yes" for a question it cannot legally say yes to.
    affirmative_is_legal: bool = True
    #: The legal answers to a `choose_a_quality` question (CR 614.12a) - the
    #: five colors' letter codes for "choose a color", empty for "choose a
    #: card name"/"choose a creature type", whose universe is too large to
    #: enumerate (`machines/replacement.py`'s `_choose_a_quality`). Also
    #: empty for every other kind.
    options: tuple[str, ...] = ()

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
            options=decision.options,
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
            "options": list(self.options),
        }


class DecisionMailbox:
    """One pending question and its answer, for one game.

    A condition variable, not a queue: every decision kind this module
    reaches (CR 608.2d for most; CR 603.3d/601.2c for `choose_targets` - see
    the module docstring) never has two questions pending on the same player
    at once (each is announced while applying one effect or putting one
    spell/ability on the stack), so "the current question, if any" is the
    whole state this needs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._pending: PendingDecision | None = None
        #: The engine `Decision` behind `_pending` - not just its state-view
        #: shape. `decide` needs `amount`, `exact`, `target_minimum`, and the
        #: rest to run `validate_answer` against the same data `Game.ask`
        #: itself checks, so a malformed or out-of-range answer is refused
        #: before this mailbox ever carries it to the resolving thread. See
        #: `validate_answer`'s docstring for why that check has to happen
        #: here rather than being left to `Game.ask`.
        self._decision: Decision | None = None
        self._delivered = False
        self._answer: object = None
        self._answered = False
        #: Every seat that has read this game's state at least once, from
        #: `app.py`'s `get_game`. The evidence that somebody is behind a seat
        #: at all - see `delivery_bound`.
        self._watching: set[str] = set()
        #: Every seat whose presence probe has already expired without them
        #: reading the game. A latch, cleared the moment they do read it, so
        #: an unattended game probes once rather than once per question.
        self._absent: set[str] = set()
        #: Whether any presence probe has run to expiry on this game yet.
        #: Until one has, "no seat has read this game" is not yet evidence of
        #: absence - it is simply too early to tell, and the first question
        #: of a game routinely beats the first poll (see `delivery_bound`).
        self._probed = False

    def publish(self, pending: PendingDecision, decision: Decision) -> None:
        """A question just became live. Wakes anyone already waiting on it."""
        with self._condition:
            self._pending = pending
            self._decision = decision
            self._delivered = False
            self._answer = None
            self._answered = False
            self._condition.notify_all()

    def mark_delivered(self, player: str) -> None:
        """`player`'s own state read has now shown them the pending question.

        Called from `app.py`'s `get_game` - the same endpoint `Seat.fetch_state`
        polls (`seatkit.block_until_wake`) - so this fires at the first poll
        after `publish`, in practice a fraction of a second later. Ignored for
        anyone but the deciding player, and for a question already answered or
        no longer pending, so a stale or misdirected call cannot mark the
        *next* question delivered before it has even been asked.
        """
        with self._condition:
            if self._pending is not None and self._pending.player == player:
                self._delivered = True
                self._condition.notify_all()

    def mark_watching(self, player: str) -> None:
        """`player` has read this game's state - so somebody is behind that
        seat, whatever question is or is not pending right now.

        Called from `app.py`'s `get_game` for every recognised seat uid, not
        only when a decision happens to be pending for that seat (which is
        what separates this from `mark_delivered`). It is the whole of the
        evidence `delivery_bound` uses to tell a live player from a game
        nobody is watching, and it clears the absent latch: a seat that
        missed one probe and then turns up is present again from then on.
        """
        with self._condition:
            self._watching.add(player)
            self._absent.discard(player)
            self._condition.notify_all()

    def is_watching(self, player: str) -> bool:
        """Whether `player` has ever read this game's state."""
        with self._condition:
            return player in self._watching

    def delivery_bound(self, player: str, *, delivery: float, presence: float) -> float:
        """How long `wait_for_answer`'s first phase should wait for `player`
        to be shown the next question - see that method, and
        `DEFAULT_PRESENCE_SECONDS`.

        Three cases, and they are exactly the three states of "is anybody
        there":

        - **Proven present** (`player` has read this game's state): `delivery`
          - a handful of that seat's own poll intervals, no more, because a
          seat that is polling is shown the question on its very next tick.
        - **Proven absent**: `0.0`. Either `player` failed a probe of their
          own and has not read the game since, or some probe on this game has
          already expired and *no* seat has ever read it - a game nobody is
          watching at all. There is nothing to wait for, and this is what
          keeps an unattended game - every test, `serve.py`'s boot, an AI
          seat that has not spawned - paying one probe per game rather than
          one per question.
        - **Unproven** (no evidence either way yet): `presence`, the probe.
          One question really can beat the first poll - a question raised
          by the same request that mints a seat's uid cannot have been seen
          by that seat - so an unproven seat is given that one window and no
          more. The probe cannot simply be zero without writing off every
          seat whose first read is still in flight.
        """
        with self._condition:
            if player in self._watching:
                return delivery
            if player in self._absent:
                return 0.0
            if self._probed and not self._watching:
                return 0.0
            return presence

    def wait_for_answer(self, timeout_seconds: float, delivery_seconds: float) -> object:
        """Block until an answer is posted, or the wait runs out.

        The clock a seat can fairly be judged against only starts once it has
        had the chance to see the question (`mark_delivered`) - a seat that
        never even got a poll in has not "failed to answer in time", it has
        simply not been asked yet as far as it knows. So this waits, first,
        up to `delivery_seconds` for delivery-or-answer, then a fresh
        `timeout_seconds` for the real answer.

        The two bounds are separate, and that is the whole point. They
        measure different things: `timeout_seconds` is how long a player may
        think, which is minutes; `delivery_seconds` is how long until this
        seat's poll loop comes round, which is a poll interval - or zero,
        when `delivery_bound` has already established that nobody is behind
        the seat at all. Giving the first phase the second's budget is what
        made an unattended game block for two full timeouts per question.

        A first phase that expires undelivered latches `player` absent, so
        the next question put to the same silent seat is not probed again.

        Returns the answer, or `NOT_ANSWERED` on timeout. Either way the
        mailbox is cleared before returning, so a late answer that arrives
        just after a timeout is simply discarded rather than delivered to the
        next, unrelated question.
        """
        with self._condition:
            player = self._pending.player if self._pending is not None else None
            if delivery_seconds > 0:
                self._condition.wait_for(
                    lambda: self._delivered or self._answered, timeout=delivery_seconds
                )
                self._probed = True
            shown = self._delivered or self._answered
            if not shown and player is not None and player not in self._watching:
                # Never delivered, and this seat has never read the game at
                # all: there is nobody to wait for, so the second phase is
                # not entered. A seat that *is* watching still gets its full
                # thinking time even if the delivery mark somehow missed -
                # only a proven-absent seat is cut short.
                self._absent.add(player)
                answered = False
            else:
                answered = self._condition.wait_for(lambda: self._answered, timeout=timeout_seconds)
            value = self._answer if answered else NOT_ANSWERED
            self._pending = None
            self._decision = None
            self._delivered = False
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

    def current(self) -> tuple[PendingDecision, Decision] | None:
        """The question on the table right now, and the engine `Decision`
        behind it, or `None` - both read under the one lock so they always
        describe the same question, never a `PendingDecision` from one
        publish paired with a `Decision` from the next.

        `decide` uses this instead of `snapshot` because refusing a
        malformed answer before it ever reaches the mailbox (see
        `validate_answer`) needs the raw `Decision`'s `amount`, `exact`, and
        `target_minimum` - fields `PendingDecision` does not carry because
        read-only state views (`snapshot`'s only other caller) never needed
        them.
        """
        with self._condition:
            if self._pending is None or self._decision is None:
                return None
            return self._pending, self._decision


@dataclass
class GameDecisionProvider:
    """The `DecisionProvider` (see `machines/decisions.py`) for one game.

    Publishes each question to `mailbox`, blocks the calling thread - which is
    whatever request is deep in `Game.ask` - for up to `timeout_seconds`, and
    on timeout logs the default the engine is about to take instead of
    silently letting it happen. `game_id` is carried only for that log line.

    How long it blocks depends on whether anybody is actually behind the
    deciding seat, which `mailbox.delivery_bound` decides from that seat's
    own state reads. A proven-present seat gets `delivery_seconds` to be
    shown the question and then the full `timeout_seconds` to answer it; a
    seat nobody is sitting at gets one `presence_seconds` probe and, after
    that, nothing at all. Before this, every seat got `2 * timeout_seconds`
    whether or not it existed - see `DEFAULT_DELIVERY_SECONDS`.

    All three bounds are clamped to `timeout_seconds`, so a caller that sets
    a short `timeout_seconds` (every test that wants a fast default) really
    does get a short total block and does not have to know about the other
    two constants.
    """

    mailbox: DecisionMailbox
    game_id: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    delivery_seconds: float = DEFAULT_DELIVERY_SECONDS
    presence_seconds: float = DEFAULT_PRESENCE_SECONDS

    def decide(self, decision: Decision) -> object:
        # Published before the bound is worked out, not after: a seat whose
        # own read lands in that window is then already recorded as watching,
        # and the question it just saw is already the pending one.
        self.mailbox.publish(PendingDecision.from_engine(decision), decision)
        bound = self.mailbox.delivery_bound(
            decision.player,
            delivery=min(self.delivery_seconds, self.timeout_seconds),
            presence=min(self.presence_seconds, self.timeout_seconds),
        )
        answer = self.mailbox.wait_for_answer(self.timeout_seconds, bound)
        if answer is NOT_ANSWERED:
            self._log_timeout(decision, bound)
        return answer

    def _log_timeout(self, decision: Decision, delivery_bound: float = 0.0) -> None:
        """Never silent: the default taken, and the rule that permits it.

        Written to the process log, the game's own audit log, and the game
        *state* itself (`store.record_decision_timeout`, surfaced as
        `decision_timeouts` on every state view) - the audit log is what a
        player reads after the fact, but a default that changes what a
        resolving effect does belongs in the transcript a fresh state read
        shows too, not only in history a caller has to think to go looking
        for. A default is a harness failure standing in for a player's
        choice, never indistinguishable from one that was actually made.

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
        from .store import store  # lazy: same cycle as audit's own import

        taken = default_answer(decision)
        taken_ids = [str(card_id) for card_id in taken] if isinstance(taken, tuple) else str(taken)
        rule = rule_for_kind(decision.kind)
        could_answer = decision.kind in ANSWERABLE_KINDS
        watching = self.mailbox.is_watching(decision.player)
        if not could_answer:
            verb = "could not answer - no transport wires this decision kind to a live seat"
            waited = f"after {self.timeout_seconds}s"
        elif watching:
            verb = "did not answer in time"
            waited = f"after {self.timeout_seconds}s"
        else:
            # The third case, and the common one: nobody has ever read this
            # game's state as this seat, so there was no player to be slow.
            # Saying "did not answer in time" here would read as an
            # inattentive human where the truth is an unattended seat.
            verb = "never showed up - no seat has read this game's state as them"
            waited = f"after a {delivery_bound}s presence probe"
        line = (
            f"{decision.prompt} timed out {waited}: "
            f"{decision.player} {verb}, so defaulted to {taken_ids} ({rule})"
        )
        logger.warning(line)
        record = {
            "player": decision.player,
            "kind": decision.kind.value,
            "prompt": decision.prompt,
            "defaulted_to": taken_ids,
            "could_answer": could_answer,
            #: Whether any seat read this game's state as this player before
            #: the question expired. `False` is an unattended seat, not a slow
            #: one - the distinction `verb` above spells out in words.
            "watched": watching,
            "rule": rule,
        }
        audit.record(self.game_id, "decision_timeout", **record)
        store.record_decision_timeout(self.game_id, record)


def rule_for_kind(kind: DecisionKind) -> str:
    """The CR citation that actually governs asking a decision of `kind` -
    verified against the live rules text this session (`knowledge_search`),
    not stated from memory (this project's CLAUDE.md).

    Every `DecisionKind` this module ever asks through `Game.ask` is answered
    "while applying the effect" of a spell or ability already on the stack,
    which is CR 608.2d - except `CHOOSE_TARGETS`, which is choosing targets
    *while putting* the spell or ability on the stack in the first place, the
    exact case CR 608.2d's own wording excludes ("choices other than choices
    already made as part of casting the spell, activating the ability, or
    otherwise putting the spell or ability on the stack"). That step is CR
    603.3d for a triggered ability, which folds in CR 601.2c ("the player
    announces their choice of an appropriate object or player for each
    target") for the spell-casting case 603.3d itself delegates to.

    `OPENING_HAND_ACTION` is the second exception, and a further-out one: no
    effect is applying and nothing is going on the stack, because no turn has
    begun at all. It is CR 103.6's own sequence, put to a seat by
    `Game.take_opening_hand_actions` once CR 103.5's mulligan process is
    complete - see `opening.py`.

    This is the single place that decides the citation - `_log_timeout` calls
    it instead of a hardcoded string, which used to print "CR 608.2d" for
    every decision kind including `CHOOSE_TARGETS`, misdescribing exactly the
    case the rule's own text carves out.
    """
    if kind is DecisionKind.CHOOSE_TARGETS:
        return "CR 603.3d, CR 601.2c (targets chosen while going on the stack)"
    if kind is DecisionKind.MULLIGAN:
        # Also before the first turn, and one step earlier than CR 103.6:
        # the declaration each player makes in the mulligan process itself.
        return "CR 103.5 (the mulligan declaration, before the first turn)"
    if kind is DecisionKind.OPENING_HAND_ACTION:
        # No effect is applying and no spell is going on the stack: this is
        # CR 103.6's own start-of-game sequence, "once the mulligan process
        # (see rule 103.5) is complete, the starting player may take any such
        # actions in any order", before the first turn begins.
        return "CR 103.6 (an action taken from the opening hand, before the first turn)"
    if kind is DecisionKind.ORDER_TRIGGERS:
        # No effect is applying here either: this is the two-part process that
        # *puts* a batch of simultaneous triggers on the stack, one step
        # before any of them resolves. CR 603.3b, the same family of exception
        # `CHOOSE_TARGETS` is - a choice made while going on the stack, which
        # CR 608.2d's own wording excludes.
        return "CR 603.3b (the order simultaneous triggers go on the stack)"
    return "CR 608.2d (a choice made while the effect applies)"


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


def energy_amount_answer(energy_amount: int | None) -> int:
    """A `decide` request body's `energy_amount`, in the shape `Game.ask`
    expects for `PAY_ENERGY_AMOUNT` - a plain `int` from 0 up to however much
    energy the player has (CR 118.3, CR 107.14). Distinct from `PAY_ENERGY`
    (a fixed-cost yes/no answered by `bool_answer`, same as `MAY`/`PAY_LIFE`):
    `PAY_ENERGY_AMOUNT` is "pay any amount", so the answer is how much, not
    whether.

    Refuses a missing amount with a 400-shaped `ValueError` here, matching
    `bool_answer`'s reasoning for `accept` - `Game.ask`'s own `isinstance`
    and range checks (0..`pending_decision.amount`, the energy available)
    still run on the other side of the mailbox and are not duplicated here;
    this only keeps `None` from reaching them in the first place.
    """
    if energy_amount is None:
        raise ValueError("this decision needs an integer amount of energy ('energy_amount')")
    return energy_amount


def life_amount_answer(life_amount: int | None) -> int:
    """A `decide` request body's `life_amount`, in the shape `Game.ask`
    expects for `PAY_LIFE_AMOUNT` - a plain `int` from 0 up to the player's
    life total (CR 119.4, CR 608.2d). Distinct from `PAY_LIFE` (a fixed-cost
    yes/no answered by `bool_answer`, same as `MAY`/`PAY_ENERGY`) the same
    way `PAY_ENERGY_AMOUNT` is distinct from `PAY_ENERGY`: this kind is "pay
    any amount", so the answer is how much, not whether. Deliberately its
    own request field (`life_amount`), not shared with `PAY_ENERGY_AMOUNT`'s
    `energy_amount` - the two pay from different resources and a client that
    confused them would silently pay the wrong one.

    Refuses a missing amount with a 400-shaped `ValueError` here, matching
    `energy_amount_answer`'s reasoning - `Game.ask`'s own `isinstance` and
    range checks (0..`pending_decision.amount`, the life total available)
    still run on the other side of the mailbox and are not duplicated here.
    """
    if life_amount is None:
        raise ValueError("this decision needs an integer amount of life ('life_amount')")
    return life_amount


def mode_answer(choice: str | None) -> str:
    """A `decide` request body's `choice`, in the shape `Game.ask` expects
    for `CHOOSE_A_MODE` (CR 603.3c, CR 700.2a) - a plain, non-empty `str`
    naming one of `pending_decision.options` (a modal ability's printed
    modes, always fully enumerated - unlike `CHOOSE_A_QUALITY`'s open "any
    card name"/"any creature type" shape, so a client can always show every
    legal mode up front). Shares `CHOOSE_A_QUALITY`'s request field
    (`choice`) rather than growing its own: both kinds answer with "which of
    these printed strings did you pick", and `Game.ask` is what actually
    enforces the printed modes are respected.

    Refuses a missing or empty choice with a 400-shaped `ValueError` here,
    matching `quality_answer`'s reasoning for `choice`.
    """
    if not choice:
        raise ValueError("this decision needs a chosen mode ('choice'), CR 700.2a")
    return choice


def quality_answer(choice: str | None) -> str:
    """A `decide` request body's `choice`, in the shape `Game.ask` expects
    for `CHOOSE_A_QUALITY` (CR 614.12a) - a plain, non-empty `str` naming
    whichever of a color, a card name, or a creature type
    `pending_decision.options` calls for (empty `options` means "any card
    name" or "any creature type"; five color codes means "choose a color").

    Refuses a missing or empty choice with a 400-shaped `ValueError` here,
    matching `bool_answer`'s reasoning for `accept` - `Game.ask`'s own check
    that a non-empty `options` narrows the legal answers still runs on the
    other side of the mailbox and is not duplicated here.
    """
    if not choice:
        raise ValueError("this decision needs a chosen value ('choice'), CR 614.12a")
    return choice


def top_or_bottom_answer(top: bool | None) -> bool:
    """A `decide` request body's `top`, in the shape `Game.ask` expects for
    `PUT_ON_TOP_OR_BOTTOM` (Subtlety) - a plain `bool`, `True` for the top of
    the library, `False` for the bottom.

    Refuses a missing answer with a 400-shaped `ValueError`, matching
    `bool_answer`'s reasoning for `accept`: `Game.ask`'s own `isinstance(value,
    bool)` check would raise the same way but inside the other request thread,
    where nobody would ever see it.
    """
    if top is None:
        raise ValueError("this decision needs a top-or-bottom answer ('top')")
    return top


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
    with a 400 here rather than reaching the engine at all. Whether the
    right *number* was chosen, and whether each id was actually one of the
    candidates offered, is `validate_answer`'s job, run against the same
    `Decision` `Game.ask` itself would check - see that function's
    docstring for why that check happens here now rather than being left
    to surface, late and misattributed, on the other side of the mailbox.
    """
    return tuple(UUID(card_id) for card_id in card_ids)


def order_answer(order: list[str]) -> tuple[UUID, ...]:
    """A `decide` request body's `order`, in the shape
    `machines/decisions.ask_trigger_order` expects for `ORDER_TRIGGERS` - a
    `tuple[UUID, ...]`, bottom of the stack first (CR 603.3b).

    Ids only, never source names. `ask_trigger_order` also accepts a printed
    source name, because a *scenario* has to be able to state the order before
    the abilities exist; a live seat is looking at
    `pending_decision.cards` and has the real `AbilityOnStack.id`s in front of
    it, and this API could not resolve a name anyway - `Decision.cards` carries
    the ids and nothing carries the names alongside them. So a non-UUID token
    is refused here with a 400 rather than reaching the engine, exactly as
    `card_ids_answer` refuses a malformed card id. Whether the ids given are a
    *permutation* of the ones offered is `validate_answer`'s job.
    """
    return tuple(UUID(ability_id) for ability_id in order)


def targets_answer(targets: list[str]) -> tuple[TargetRef, ...]:
    """A `decide` request body's `targets`, in the shape `Game.ask` expects
    for `CHOOSE_TARGETS` - a `tuple[TargetRef, ...]`.

    Each token is a card or ability id (a UUID string) or a player's username,
    told apart the same way `TargetRef.coerce` already tells a bare UUID from
    a bare string apart - no different from `card_ids_answer` in that this
    is pure parsing and never touches the `Game`. Whether the parsed
    reference was actually one of the candidates offered, and whether enough
    of them were given, is `validate_answer`'s job; a malformed token is
    refused here with a 400 rather than reaching the engine at all, matching
    `card_ids_answer`.
    """
    refs = []
    for token in targets:
        try:
            refs.append(TargetRef.to_card(UUID(token)))
        except ValueError:
            refs.append(TargetRef.to_player(token))
    return tuple(refs)


def validate_answer(decision: Decision, value: object) -> None:
    """Refuse `value` with a `ValueError` if `Game.ask` (in
    `magic_cards_model.models.game`) would refuse it too - run from `decide`
    *before* the answer ever reaches `DecisionMailbox`, so the refusal is
    synchronous, reaches the answering seat directly, and the mailbox is
    never told an answer arrived that the engine was always going to reject.

    This exists because of exactly the shape of bug it closes: a
    `choose_cards` answer with the wrong number of cards used to sail past
    every check in this module (`card_ids_answer` only parses UUIDs), get
    posted to the mailbox, get logged `allowed: true`, and get handed to the
    thread parked in `Game.ask` - which *does* check the count, and raises
    "needs 1 card(s) chosen, 0 given". But `Game.ask` runs on the resolving
    thread, not this request, so by the time it raised, `decide` had already
    told the answering seat "accepted", the mailbox had already moved on, and
    the `ValueError` surfaced only later, attached to whatever unrelated
    request next drove the stack forward - the opponent's next
    `pass_priority`, in the game that exposed this. That is corruption by
    misattribution as much as it is a lost refusal: the seat who actually
    answered wrong never saw why, and the seat who happened to be running the
    engine next inherited a crash that was not theirs.

    `Game.ask`'s validation itself cannot be called from here - `decide` must
    never touch the `Game` object (see the module docstring's deadlock
    invariant), and `Game.ask` is not reentrant from a second thread while
    the first is still parked inside it. But `machines/decisions.py`'s own
    `Decision` is "deliberately stateless" (its own docstring) - every bound
    `Game.ask` checks (`amount`, `exact`, `cards`, `targets`,
    `target_maximum`, `target_minimum`, `options`, `affirmative_is_legal`) is
    already baked into the `Decision` the engine handed this API when it
    asked the question, via `legal_options` for the yes/no kinds and plain
    field comparisons for the rest. So this function runs the identical
    checks `Game.ask` runs, in the same order, against that same data,
    without touching the `Game` at all - not a second opinion invented by
    this API, but the same arithmetic the engine already computed, read back
    out of the `Decision` it built. `legal_options` itself is imported and
    called, not re-derived, so at least the yes/no branch can never drift
    from the engine's own answer to "what may this player pick".

    Only kinds in `ANSWERABLE_KINDS` are ever passed here - `decide` already
    refuses every other kind before parsing an answer at all - so every
    branch of `Game.ask`'s own kind dispatch has a match here. Two kinds do
    not go through `Game.ask` at all and are matched against their own asker
    in `machines/decisions.py` instead: `MULLIGAN` against `ask_mulligan`, and
    `ORDER_TRIGGERS` (CR 603.3b) against `ask_trigger_order`.
    """
    if decision.kind in (
        DecisionKind.MAY,
        DecisionKind.PAY_LIFE,
        DecisionKind.PAY_ENERGY,
        DecisionKind.PAY_MANA,
    ):
        if not isinstance(value, bool):
            raise ValueError(f"{decision.prompt}: a yes-or-no question needs a bool answer")
        if value not in legal_options(decision):
            raise ValueError(
                f"{decision.prompt}: that option is illegal, so CR 608.2d "
                f"does not allow choosing it"
            )
        return

    if decision.kind is DecisionKind.MULLIGAN:
        # CR 103.5's declaration. Its engine-side authority is *not*
        # `Game.ask` - `ask_mulligan` asks and validates this kind itself,
        # and a `MULLIGAN` decision never reaches `Game.ask` at all - so the
        # wording below is `ask_mulligan`'s own, word for word, and the drift
        # test compares this branch against that function rather than against
        # `Game.ask`'s kind dispatch. Both options are always legal when the
        # question is put (the engine simply does not ask a player at
        # CR 103.5's zero-hand ceiling), so there is no option set to check.
        if not isinstance(value, bool):
            raise ValueError(f"{decision.prompt}: a yes-or-no question needs a bool answer")
        return

    if decision.kind is DecisionKind.ORDER_TRIGGERS:
        # CR 603.3b's "in any order they choose". Like `MULLIGAN`, its
        # engine-side authority is not `Game.ask` at all but its own asker,
        # `machines/decisions.ask_trigger_order`, so the checks and the
        # wording below are that function's, in its order: the shape first,
        # then each token, then the permutation. The drift test compares this
        # branch against `ask_trigger_order` rather than `Game.ask`'s dispatch.
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{decision.prompt}: the answer is a sequence of ability ids or names")
        order: list[UUID] = []
        for item in value:
            if isinstance(item, UUID):
                order.append(item)
                continue
            if not isinstance(item, str):
                raise ValueError(f"{decision.prompt}: {item!r} is neither an ability id nor a name")
            # The one place this validator is deliberately *stricter* than the
            # engine, and it cannot be otherwise: `ask_trigger_order` reads a
            # `str` as a printed source name and matches it against the
            # `(id, name)` pairs it was called with, but a `Decision` carries
            # only the ids (`decision.cards`) - the names never cross the
            # mailbox - so there is nothing here to match a name against.
            # Refused by name rather than guessed at, and a live seat never
            # needs it: it is looking at `pending_decision.cards`.
            raise ValueError(
                f"{decision.prompt}: {item!r} is a name, not an ability id - answer "
                f"with the ids in pending_decision.cards"
            )
        if sorted(order, key=str) != sorted(decision.cards, key=str):
            raise ValueError(
                f"{decision.prompt}: the answer must order exactly the abilities offered"
            )
        return

    if decision.kind is DecisionKind.OPENING_HAND_ACTION:
        # CR 103.6's "may take any such actions": a plain yes/no, like `MAY`,
        # but its own branch because the rule that governs it is CR 103.6 and
        # not CR 608.2d - `legal_options` returns both booleans for this kind
        # (it is not one of the four `affirmative_is_legal` kinds), so there
        # is no option set to check, only the shape of the answer.
        # The wording is `Game.ask`'s own, word for word, because
        # `test_decision_validator_drift` holds the two refusals to parity.
        if not isinstance(value, bool):
            raise ValueError(f"{decision.prompt}: a yes-or-no question needs a bool answer")
        return

    if decision.kind is DecisionKind.PAY_ENERGY_AMOUNT:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{decision.prompt}: needs an integer amount of energy")
        if value < 0 or value > decision.amount:
            raise ValueError(
                f"{decision.prompt}: cannot pay {value} energy, only {decision.amount} "
                f"available (CR 118.3, CR 107.14)"
            )
        return

    if decision.kind is DecisionKind.PAY_LIFE_AMOUNT:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{decision.prompt}: needs an integer amount of life")
        if value < 0 or value > decision.amount:
            raise ValueError(
                f"{decision.prompt}: cannot pay {value} life, life total is only "
                f"{decision.amount} (CR 119.4)"
            )
        return

    if decision.kind is DecisionKind.CHOOSE_CARDS:
        if not isinstance(value, tuple) or not all(isinstance(v, UUID) for v in value):
            raise ValueError(f"{decision.prompt}: needs a tuple of chosen card ids")
        offered = set(decision.cards)
        stray = [str(card_id) for card_id in value if card_id not in offered]
        if stray:
            raise ValueError(
                f"{decision.prompt}: cannot choose a card that was not offered: {', '.join(stray)}"
            )
        if decision.exact:
            if len(value) != decision.amount:
                raise ValueError(
                    f"{decision.prompt}: needs {decision.amount} card(s) chosen, {len(value)} given"
                )
        elif len(value) > decision.amount:
            raise ValueError(
                f"{decision.prompt}: at most {decision.amount} card(s) may be chosen "
                f"(CR 107.1c), {len(value)} given"
            )
        return

    if decision.kind is DecisionKind.CHOOSE_TARGETS:
        if not isinstance(value, (tuple, list)):
            raise ValueError(f"{decision.prompt}: needs a tuple of targets")
        chosen = [TargetRef.coerce(target) for target in value]
        legal = set(decision.targets)
        stray = [str(ref) for ref in chosen if ref not in legal]
        if stray:
            raise ValueError(f"{decision.prompt}: not a legal target: {', '.join(stray)}")
        if decision.target_maximum is not None and len(chosen) > decision.target_maximum:
            raise ValueError(
                f"{decision.prompt}: too many targets chosen, at most "
                f"{decision.target_maximum} (CR 115.6)"
            )
        if len(chosen) < decision.target_minimum:
            raise ValueError(
                f"{decision.prompt}: at least {decision.target_minimum} target(s) "
                f"must be chosen, {len(chosen)} given, while a legal target exists "
                f"(CR 601.2c, CR 603.3d)"
            )
        return

    if decision.kind is DecisionKind.CHOOSE_A_QUALITY:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{decision.prompt}: needs a chosen value (CR 614.12a)")
        if decision.options and value not in decision.options:
            raise ValueError(
                f"{decision.prompt}: {value!r} is not among the legal choices "
                f"({', '.join(decision.options)})"
            )
        return

    if decision.kind is DecisionKind.CHOOSE_A_MODE:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{decision.prompt}: needs a chosen mode (CR 700.2a)")
        if decision.options and value not in decision.options:
            raise ValueError(
                f"{decision.prompt}: {value!r} is not among the printed modes "
                f"({', '.join(decision.options)})"
            )
        return

    if decision.kind is DecisionKind.PUT_ON_TOP_OR_BOTTOM:
        if not isinstance(value, bool):
            raise ValueError(f"{decision.prompt}: needs a top-or-bottom bool answer")
        return

    if decision.kind in (DecisionKind.SCRY, DecisionKind.SURVEIL):
        if not isinstance(value, CardSplit):
            raise ValueError(f"{decision.prompt}: needs a CardSplit answer")
        looked_at = set(decision.cards)
        stray = [str(card_id) for card_id in value.moved if card_id not in looked_at]
        if stray:
            raise ValueError(
                f"{decision.prompt}: cannot move a card that was not looked at "
                f"(CR 701.22a, CR 701.25a): {', '.join(stray)}"
            )
        return

    raise ValueError(f"{decision.prompt}: decide does not know how to validate a {decision.kind!r}")
