"""A mid-resolution decision (CR 608.2d, mostly), answered by a real seat
sitting outside the engine's call stack.

Most of these are CR 608.2d questions ("choices other than choices already
made as part of casting the spell, activating the ability, or otherwise
putting the spell or ability on the stack"), but not all: `CHOOSE_TARGETS` is
exactly the choice 608.2d's own wording carves out - it is choosing targets
*while putting* a triggered ability (CR 603.3d) or a spell (CR 601.2c) on the
stack, and `DECLARE_ATTACKERS`/`DECLARE_BLOCKERS` are CR 508.1/509.1's own
turn-based declarations, not resolution questions at all.

**Pausing, not blocking.** A mutating request that crosses a question with no
scripted answer does not park a thread waiting for one
(`magic_cards_model.machines.decisions`'s `DecisionScript.pause_when_unanswered`,
`commands.run_command`): it raises, the request returns immediately with the
question attached, and the repository remembers `(command, tape, pending)` so
a later `POST /api/games/{id}/decide` can *replay* the same command with one
more answer appended (`commands.resume_command`). `decide` is therefore an
ordinary mutating request like any other - it loads the game, resumes the
paused command, and commits whatever that produces. There is no longer a
resolving thread it must avoid racing, and no lock it must avoid taking.

This module is the rest of the plumbing that pausing does not replace: the
JSON-safe `PendingDecision` view a state read shows a seat
(`PendingDecision.from_engine`), which `DecisionKind`s a live seat may answer
at all (`ANSWERABLE_KINDS`), and the per-kind coercion from a `DecideRequest`
body to the value the engine's `Answer`/`Game.ask` expect back
(`bool_answer`, `card_ids_answer`, ..., `declared_pairs_answer`), plus
`validate_answer`, which re-runs the engine's own per-kind legality checks
synchronously so a malformed or illegal answer is refused on the spot rather
than surfacing later, misattributed to whatever request drives the paused
command's next attempt.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from magic_cards_model.machines.decisions import (
    CardSplit,
    DamageAssignment,
    Decision,
    DecisionKind,
    DeclaredPair,
    damage_assignment_answer,
    legal_options,
)
from magic_cards_model.models.card_state import TargetRef

from .schemas import DamageAssignmentRequest, DeclaredPairRequest

logger = logging.getLogger("mcm.decisions")

#: Every `DecisionKind` that `POST /api/games/{id}/decide` actually knows how
#: to answer - see `app.py`'s `decide` endpoint, which this set must be kept
#: in lockstep with. Covers all 24 of the engine's `DecisionKind` members -
#: there is no longer a second set for "answered somewhere else": pausing put
#: every decision, `DECLARE_ATTACKERS`/`DECLARE_BLOCKERS` included, on the one
#: tape a paused command replays from, so the one thing that used to be true
#: of exactly two kinds (no live transport at all) is no longer
#: true of any. `PAY_ENERGY` and `PAY_ENERGY_AMOUNT` (CR 107.14,
#: CR 118.3) and `CHOOSE_A_QUALITY` (CR 614.12a) joined this set after a
#: player's Utopia Sprawl froze a live game: the engine could ask the
#: question, `Game.ask` had nowhere live to route it, and `decide` refused
#: every shape the player tried, so the "as this enters, choose a color"
#: replacement effect never got its answer and the whole match sat stuck
#: behind it. `test_decision_kinds.test_every_decision_kind_is_answerable`
#: is the regression test: it enumerates the engine's own `DecisionKind` and
#: fails, by name, the moment a new one is added to
#: `magic_cards_model.machines.decisions` without a matching entry here -
#: the exact gap that let this one through.
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
#: `CHOOSE_AN_AMOUNT` ("put up to N +1/+0 counters on this creature" -
#: Clockwork Steed's own `{X}, {T}` ability, CR 608.2d) joined this set the
#: same way `PAY_LIFE_AMOUNT` did: caught by the drift test at merge time.
#: It is `PAY_ENERGY_AMOUNT`/`PAY_LIFE_AMOUNT`'s own shape - an `int` from 0
#: up to `Decision.amount` - generalised past paying anything at all: an
#: "up to N" resolution-time choice with no cost or payment attached, so
#: `choose_amount_answer` is `life_amount_answer` read with nothing spent.
#: Its own request field (`amount`), not shared with `energy_amount`/
#: `life_amount`: those two name what is being paid away, and this kind pays
#: nothing.
#: `CHOOSE_PLAYERS` (CR 701.34a's own "and/or players" half of Proliferate)
#: and `CHOOSE_PILE` (CR 700.3's pile primitive, Liliana of the Veil's own
#: `-6`, Do or Die) joined this set the Reality Fracture way every kind above
#: it did: the engine gained both, and the drift test caught the gap at merge
#: time rather than in a live game. `CHOOSE_PLAYERS` is `CHOOSE_CARDS`'s own
#: twin over `Decision.players` (usernames) instead of `Decision.cards` (card
#: ids) - `usernames_answer` reads exactly like `card_ids_answer` but with no
#: UUID parsing, since a username is not one. `CHOOSE_PILE` is a plain
#: boolean the same shape `PUT_ON_TOP_OR_BOTTOM` already is - `True` for pile
#: one (`Decision.cards`, the first player's own division), `False` for its
#: complement - so `pile_answer` is `top_or_bottom_answer` read for a pile,
#: with its own request field (`pile_one`) for the same reason `top` is not
#: named `accept`: a client must not confuse this two-way choice for a
#: yes/no.
#: `OPENING_HAND_ACTION` (CR 103.6, "some cards allow a player to take actions
#: with them from their opening hand") joined this set when the API started
#: running CR 103.6 at all. It had no live transport before that, on the
#: true-at-the-time grounds that `decks.load_deck_into_game` stopped at
#: `Match.start_game` and nothing ever ran the sequence, so the question was
#: never asked of anyone. It is asked now, through
#: `Game.take_opening_hand_actions`'s
#: `self.ask` - the one path that consults the replay tape - of a game
#: that is already in the store with its seat uids handed out. So a live
#: seat really does see it as its
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
#: `DECLARE_ATTACKERS` and `DECLARE_BLOCKERS` (CR 508.1, CR 509.1) are the
#: newest members. `machines/decisions.py`'s `ask_attackers`/`ask_blockers`
#: never consulted `Game.decisions.provider` - the old live blocking
#: transport - so under the blocking design a live seat genuinely had no way
#: to answer either through `decide`, and each had its own direct action
#: (`actions/attack`, `actions/declare-blockers`) calling
#: `Game.declare_attackers`/`Game.declare_blockers` outright, bypassing the
#: engine's own asker entirely. That was a second, undocumented transport for
#: combat declarations, alongside `Game.advance()` driving the askers above -
#: nobody had ever decided which one owned it. Pausing settles that: both
#: askers still consult a scripted answer and the replay tape before falling
#: through to `default_answer`, and now also raise `DecisionPending` when
#: `pause_when_unanswered` is set and neither speaks, the same as every other
#: asker in the engine - so `advance()` genuinely pauses on a real
#: `DECLARE_ATTACKERS`/`DECLARE_BLOCKERS` question the moment nothing
#: answers it, and the tape is the one transport that owns it. The two direct
#: actions (`app.py`) are now thin aliases over that same tape rather than a
#: second path around it - see their own docstrings.
#:
#: `MULLIGAN` joined the same way one engine change earlier: CR 103.5's
#: declaration was genuinely asked, but `ask_mulligan` never consulted a
#: provider at all, so `pending_decision` could not carry it and every seat
#: silently took the engine default of keeping the hand as dealt. That engine
#: gap is closed: `ask_mulligan` now consults the tape between the script and
#: the default, exactly as `Game.ask` does.
#:
#: Which path reaches it matters, and both exist. `Game.settle_pregame` - the
#: whole-pregame walk exposed here as the admin-only `POST /api/games/{id}/
#: pregame/settle` (`commands._run_pregame_settle`, an ordinary command like
#: any other) - goes through `ask_mulligan` for real, and by default (`Post
#: .../pregame/settle`'s `use_defaults=False`) genuinely pauses on it, the
#: same as any other command pauses on any other kind: `pending_decision`
#: carries it, and a live seat answers through `POST /api/games/{id}/decide`.
#: `use_defaults=True` instead pre-fills the tape with `default_answer` for
#: every pause the run hits (`commands.run_command_to_completion`), so an
#: admin settle can still take the engine's own defaults immediately and
#: never actually pause, without a second `pause_when_unanswered`-toggling
#: code path to keep in sync with this one. A seat declaring for *itself*
#: instead uses `POST /api/games/{id}/pregame/mulligan`, which calls
#: `Game.declare_mulligan`: told an answer already made, asking nobody,
#: pausing on nothing. All three are real; none makes another dead code.
#:
#: `validate_answer` gives `MULLIGAN` its own branch even so, because the
#: engine-side authority for a mulligan answer is `ask_mulligan`'s own
#: `isinstance` check and *not* `Game.ask` - a `MULLIGAN` decision never
#: reaches `Game.ask` at all, and would fall through that method's kind
#: dispatch to its trailing `CardSplit` branch if it did. `ORDER_TRIGGERS`
#: and `DECLARE_ATTACKERS`/`DECLARE_BLOCKERS` are the same shape: their
#: engine-side authority is their own asker (`ask_trigger_order`,
#: `ask_attackers`/`ask_blockers` by way of the shared `_ask_declaration`),
#: not `Game.ask`.
#:
#: The *bottoming* that follows a kept hand (CR 103.5's "puts a number of
#: those cards ... on the bottom of their library") needed nothing new here:
#: the engine asks it as a `CHOOSE_CARDS` decision with `exact=True` and
#: `amount = cards_to_bottom`, and this API has answered and rendered that
#: kind since long before. Its unanswered default is already "the back of the
#: offered cards", which is what `keep_hand(None)` does, so an absent player
#: behaves exactly as before. On the seat-driven path `opening.py`
#: pre-provides that choice from the declaration itself, so it never pauses
#: at all.
ANSWERABLE_KINDS = frozenset(
    {
        DecisionKind.MAY,
        DecisionKind.OPENING_HAND_ACTION,
        DecisionKind.PAY_LIFE,
        DecisionKind.PAY_ENERGY,
        DecisionKind.PAY_ENERGY_AMOUNT,
        DecisionKind.PAY_LIFE_AMOUNT,
        DecisionKind.CHOOSE_AN_AMOUNT,
        DecisionKind.PAY_MANA,
        DecisionKind.SCRY,
        DecisionKind.SURVEIL,
        DecisionKind.CHOOSE_CARDS,
        DecisionKind.CHOOSE_TARGETS,
        DecisionKind.CHOOSE_A_QUALITY,
        DecisionKind.CHOOSE_A_MODE,
        DecisionKind.PUT_ON_TOP_OR_BOTTOM,
        DecisionKind.PUT_INTO_HAND_OR_GRAVEYARD,
        DecisionKind.MULLIGAN,
        DecisionKind.ORDER_TRIGGERS,
        DecisionKind.DECLARE_ATTACKERS,
        DecisionKind.DECLARE_BLOCKERS,
        DecisionKind.ASSIGN_COMBAT_DAMAGE,
        DecisionKind.CHOOSE_PLAYERS,
        DecisionKind.CHOOSE_PILE,
        # FD5 (5): CR 702.52a - replace a draw by dredging a graveyard card,
        # or decline (an empty answer) and draw normally.
        DecisionKind.DREDGE,
    }
)
assert frozenset(DecisionKind) == ANSWERABLE_KINDS, (
    "ANSWERABLE_KINDS must cover every DecisionKind now that pausing gives "
    "every one of them a real transport - see this module's own docstring "
    "on this set, and test_decision_kinds.py's drift regression test"
)

#: Which single `machines.decisions.Answer` field carries a `kind`'s
#: coerced value - `app.py`'s `decide` (and its `actions/attack`/
#: `actions/declare-blockers` aliases) build `Answer(seq=..., kind=...,
#: player=..., **{ANSWER_FIELD_FOR_KIND[kind]: value})` from whichever
#: `*_answer` coercion ran for that kind, rather than one `if`/`elif` chain
#: per field name. This mirrors `machines/decisions.py`'s own, private
#: `_ANSWER_FIELD_FOR_KIND` table - duplicated rather than imported because
#: that one is not part of the engine's public surface, but never silently:
#: `Answer._check_one_value_field` raises loudly on a mismatch (the wrong
#: field set, or more than one), so a drift between the two tables fails a
#: request immediately rather than misrouting an answer.
ANSWER_FIELD_FOR_KIND: dict[DecisionKind, str] = {
    DecisionKind.MAY: "accept",
    DecisionKind.PAY_LIFE: "accept",
    DecisionKind.PAY_ENERGY: "accept",
    DecisionKind.PAY_MANA: "accept",
    DecisionKind.MULLIGAN: "accept",
    DecisionKind.OPENING_HAND_ACTION: "accept",
    DecisionKind.PUT_ON_TOP_OR_BOTTOM: "accept",
    DecisionKind.PUT_INTO_HAND_OR_GRAVEYARD: "accept",
    DecisionKind.PAY_ENERGY_AMOUNT: "amount",
    DecisionKind.PAY_LIFE_AMOUNT: "amount",
    DecisionKind.CHOOSE_AN_AMOUNT: "amount",
    DecisionKind.CHOOSE_CARDS: "card_ids",
    DecisionKind.ORDER_TRIGGERS: "card_ids",
    DecisionKind.SCRY: "split",
    DecisionKind.SURVEIL: "split",
    DecisionKind.CHOOSE_TARGETS: "targets",
    DecisionKind.CHOOSE_A_QUALITY: "text",
    DecisionKind.CHOOSE_A_MODE: "text",
    DecisionKind.DECLARE_ATTACKERS: "pairs",
    DecisionKind.DECLARE_BLOCKERS: "pairs",
    DecisionKind.ASSIGN_COMBAT_DAMAGE: "damage",
    DecisionKind.CHOOSE_PLAYERS: "usernames",
    DecisionKind.CHOOSE_PILE: "accept",
    DecisionKind.DREDGE: "card_ids",
}
assert set(ANSWER_FIELD_FOR_KIND) == set(DecisionKind)

#: Which **tool parameter** a seat actually types to answer a `kind`, and
#: which tool that parameter belongs to. Together these two tables are what
#: every pending decision's `answer_with`/`answer_field` is built out of, so
#: a seat is never left deriving "which argument answers a `scry`?" from
#: prose.
#:
#: Deliberately *not* derived from `ANSWER_FIELD_FOR_KIND` above, and the two
#: cannot be collapsed into one: that table names the engine-facing
#: `machines.decisions.Answer` field the coerced value is handed back on,
#: this one names the argument the caller writes, and they disagree on
#: twelve of the twenty-four kinds.
#:
#: | kind | `ANSWER_FIELD_FOR_KIND` | `TOOL_FIELD_FOR_KIND` |
#: |---|---|---|
#: | `scry`/`surveil` | `split` | `moved` (plus the optional `top_order`) |
#: | `choose_a_quality`/`choose_a_mode` | `text` | `choice` |
#: | `pay_energy_amount`/`pay_life_amount` | `amount` | `energy_amount`/`life_amount` |
#: | `put_on_top_or_bottom` | `accept` | `top` |
#: | `choose_pile` | `accept` | `pile_one` |
#: | `put_into_hand_or_graveyard` | `accept` | `to_hand` |
#: | `order_triggers` | `card_ids` | `order` |
#: | `declare_attackers`/`declare_blockers` | `pairs` | `attackers`/`blocks`, on **another tool** |
#:
#: The last row is the sharpest of them: the MCP `decide` tool has no `pairs`
#: parameter at all. CR 508.1/CR 509.1's declarations pause on the same tape
#: as every other kind and are answered through `POST .../decide` at the HTTP
#: layer, but from the tool surface they are reached only through their own
#: `declare_attackers`/`declare_blockers` tools - so those two rows have to
#: name a different tool as well as a different argument, which no derivation
#: from the engine-facing table could ever produce.
#:
#: That divergence is exactly why this ships as *data on the decision* rather
#: than as prose a seat reasons over: a seat guessing the parameter from the
#: kind gets twelve of twenty-four wrong, and it gets them wrong in the worst
#: possible way - `accept` and `card_ids` are real parameters of `decide`, so
#: the guess is accepted by the tool and refused by the route, which is the
#: measured "this decision needs a yes/no answer ('accept')" refusal.
TOOL_FIELD_FOR_KIND: dict[DecisionKind, str] = {
    DecisionKind.MAY: "accept",
    DecisionKind.PAY_LIFE: "accept",
    DecisionKind.PAY_ENERGY: "accept",
    DecisionKind.PAY_MANA: "accept",
    DecisionKind.MULLIGAN: "accept",
    DecisionKind.OPENING_HAND_ACTION: "accept",
    DecisionKind.PUT_ON_TOP_OR_BOTTOM: "top",
    DecisionKind.PUT_INTO_HAND_OR_GRAVEYARD: "to_hand",
    DecisionKind.PAY_ENERGY_AMOUNT: "energy_amount",
    DecisionKind.PAY_LIFE_AMOUNT: "life_amount",
    DecisionKind.CHOOSE_AN_AMOUNT: "amount",
    DecisionKind.CHOOSE_CARDS: "card_ids",
    DecisionKind.ORDER_TRIGGERS: "order",
    DecisionKind.SCRY: "moved",
    DecisionKind.SURVEIL: "moved",
    DecisionKind.CHOOSE_TARGETS: "targets",
    DecisionKind.CHOOSE_A_QUALITY: "choice",
    DecisionKind.CHOOSE_A_MODE: "choice",
    DecisionKind.DECLARE_ATTACKERS: "attackers",
    DecisionKind.DECLARE_BLOCKERS: "blocks",
    DecisionKind.ASSIGN_COMBAT_DAMAGE: "damage",
    DecisionKind.CHOOSE_PLAYERS: "usernames",
    DecisionKind.CHOOSE_PILE: "pile_one",
    DecisionKind.DREDGE: "card_ids",
}
assert set(TOOL_FIELD_FOR_KIND) == set(DecisionKind), (
    "TOOL_FIELD_FOR_KIND must cover every DecisionKind: a kind with no entry "
    "here ships a pending decision that cannot say how to answer itself, "
    "which puts the seat back to reading decide()'s docstring - see "
    "test_decision_kinds.py's drift guard on this table"
)

#: Which tool `TOOL_FIELD_FOR_KIND`'s parameter belongs to. `decide` for
#: every kind but the two combat declarations - see that table's own note.
TOOL_FOR_KIND: dict[DecisionKind, str] = {
    kind: (
        "declare_attackers"
        if kind is DecisionKind.DECLARE_ATTACKERS
        else "declare_blockers"
        if kind is DecisionKind.DECLARE_BLOCKERS
        else "decide"
    )
    for kind in DecisionKind
}
assert set(TOOL_FOR_KIND) == set(DecisionKind)


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
    #: The candidate players a `CHOOSE_PLAYERS` answer must be a subset of
    #: (CR 701.34a's own Proliferate player half) - `cards`' own twin, keyed
    #: by username rather than card id, since a player has no id of their
    #: own to be named by. Empty for every other kind.
    players: tuple[str, ...] = ()
    targets: tuple[dict[str, str], ...] = ()
    target_maximum: int | None = None
    #: The floor a mandatory target requirement carries (CR 601.2c, CR
    #: 115.6) - "a plain 'target permanent' is not a 'you may'", so an
    #: answer choosing fewer than this many legal targets is refused. `0`
    #: for a genuinely optional "up to N"/"any number of" requirement
    #: (CR 115.6, CR 107.1c), same as the engine `Decision.target_minimum`
    #: this is read from - see that field's own docstring in
    #: `machines/decisions.py` for the full rule.
    target_minimum: int | None = None
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
    #: Where the two halves of a `SCRY`/`SURVEIL` answer actually end up, in
    #: the asking card's own words: `moved_label` for the cards the answer
    #: names, `kept_label` for the ones it does not. CR 701.22a and CR 701.25a
    #: each fix their own pair ("on the bottom of your library" / "on top",
    #: and "into your graveyard" / "on top"), but the engine reuses
    #: `DecisionKind.SCRY` for the generalised look-then-split whose
    #: destinations belong entirely to the calling card - Devourer of
    #: Destiny's first-upkeep trigger keeps one card *on top* and exiles the
    #: rest (CR 406.1's holding area), which is neither rule's pair and is the
    #: exact opposite of scry's. A client that assumes the pair from the kind
    #: tells the deciding player the reverse of what will happen, so the
    #: engine stamps the destinations here and clients read them.
    #:
    #: Empty when the engine did not stamp them; a client then falls back to
    #: the CR 701.22a/701.25a wording its kind implies, which is right for
    #: every decision `scry`/`surveil` themselves raise.
    moved_label: str = ""
    kept_label: str = ""
    #: A name per `cards` entry, positionally aligned with it, or empty when
    #: the engine stamped none.
    #:
    #: `cards` is a list of ids, and for a decision drawn from a *hidden* zone
    #: (CR 400.2 makes the library and the hand hidden) an id is all a client
    #: would otherwise have: `redact_view` strips `libraries` from every
    #: non-admin viewer, including that viewer's own. `card_details` covers
    #: that for a viewer whose ids the *server* can resolve
    #: (`visibility.redact_pending_decision`) - which, for `ORDER_TRIGGERS`,
    #: now includes its own batch too, via `abilities` below rather than a
    #: zone lookup - but `card_labels` still travels for every kind
    #: regardless, unchanged, for a client that has not been updated to read
    #: the structured shape.
    #:
    #: A library search is the case that made this worth carrying: CR 701.23a
    #: has the searcher "look at all cards in that zone (even if it's a hidden
    #: zone)", so the candidate names are exactly what the searcher is
    #: entitled to see, and a picker that cannot print them is a picker played
    #: blind.
    card_labels: tuple[str, ...] = ()
    #: `ORDER_TRIGGERS` only: the structured twin of `card_labels` above, one
    #: dict per `cards` entry, positionally aligned - from the engine's own
    #: `Decision.abilities` (`machines.decisions.PendingAbility`), stamped
    #: directly onto the question the same way `card_labels` always was,
    #: since CR 603.3b is asked one step before CR 603.3's "its controller
    #: puts it on the stack" and no zone lookup can find these abilities at
    #: ask time. Each dict is `{"kind": "ability", "id", "name", "controller",
    #: "text", "source_card_id"}` - `visibility.redact_pending_decision` reads
    #: this as its fallback for an id `card_lookup` cannot resolve, which is
    #: what carries it into `card_details` on both api read paths (the state
    #: read, and a paused `advance`/`decide` response). Empty for every other
    #: kind.
    abilities: tuple[dict[str, str | None], ...] = ()
    #: `choose_cards` only: whether `amount` is a count or a ceiling.
    #:
    #: `True` (the default, and every question that existed before searching
    #: became one choice) means the answer must name exactly `amount` cards -
    #: the cleanup discard, CR 103.5's bottoming. `False` means any count from
    #: zero up to `amount` is legal: CR 107.1c's "any number", and CR 701.23b's
    #: declinable search - "If a player is searching a hidden zone for cards
    #: with a stated quality... that player isn't required to find some or all
    #: of those cards even if they're present in that zone." Finding nothing
    #: is a real answer there, and a client that renders every `choose_cards`
    #: as "choose up to N" or as "choose exactly N" is wrong about half of
    #: them.
    #:
    #: Not enforced again here: `validate_answer` already runs `Game.ask`'s own
    #: `exact`/`amount` arithmetic against this same `Decision` before an
    #: answer is accepted, and the engine runs it once more when the
    #: parked `Game.ask` returns. This field exists so a client can *ask the
    #: right question*, not so a second count check can drift from those two.
    exact: bool = True
    #: `choose_cards` only: what an unanswered question resolves to, in place
    #: of the kind's documented default, or `None` when the engine stamped no
    #: deviation.
    #:
    #: `Game.search_library` is the caller that stamps one. Its question is
    #: `exact=False` (CR 701.23b, above), whose generic default would be to
    #: find nothing - so an unscripted fetchland would pay the life, sacrifice
    #: itself and fetch no land. The engine's stated default is instead the
    #: first `amount` candidates in zone order, and it is published here so a
    #: seat - or an autopilot answering on one's behalf - can see what
    #: *silence* would cost before it decides to stay silent.
    default_cards: tuple[str, ...] | None = None
    #: `may` only: what an unanswered question resolves to, in place of the
    #: engine-wide "take it" convention, or `None` when the engine stamped no
    #: deviation - `default_cards`'s own shape, one kind over. `Game.may`'s
    #: "you may shuffle" clause is the one caller that stamps `False`: the
    #: shuffle ends whatever look or order the cards above it just fixed (CR
    #: 701.20d), so silence there means declining, not the generic "take it"
    #: every other `may` defaults to. From the engine's `Decision.default_may`
    #: (engine schema 41).
    default_may: bool | None = None
    #: The id of the object asking - the card or token, or the card behind an
    #: ability on the stack - from the engine's `Decision.source_id`. `None`
    #: for a question no object asks (a turn-based action such as the cleanup
    #: discard, a pregame choice). `source` names the card; this says which
    #: copy of it, so a client can anchor the question to the card on the
    #: board. `visibility.redact_pending_decision` withholds it where the
    #: object sits in a zone the viewer cannot see.
    source_card_id: str | None = None
    #: `assign_combat_damage` only (CR 510.1c, CR 702.19b): the damage each of
    #: `targets` still needs, aligned with it - 0 for the defending player or
    #: planeswalker. From the engine's `Decision.lethal_damage`, which already
    #: counts marked damage, deathtouch (1 is lethal), and damage other
    #: attackers assign this same step. A client shows this next to each
    #: recipient so a seat can see how much is "enough" before trampling past
    #: it (CR 702.19b forbids it otherwise). Empty for every other kind.
    lethal_damage: tuple[int, ...] = ()
    #: `assign_combat_damage` only: whether the attacker has trample (CR
    #: 702.19b), so damage may go past its blockers once each has lethal
    #: damage. `False` for every other kind.
    trample: bool = False

    #: `pay_mana` only: the untapped permanents whose mana abilities the payer
    #: may activate while paying (CR 608.2g, CR 605.3a), from the engine's own
    #: `Decision.mana_sources` - engine PR #17 (B6-08). Empty for every other
    #: kind, and empty here too where paying is only ever a floating-pool
    #: yes/no (`affirmative_is_legal` false, or an engine older than this
    #: field). A seat names a subset of these back in `decide`'s own
    #: `mana_sources` to choose which lands pay rather than accepting the
    #: engine's own pick (an `accept: true` naming none).
    mana_sources: tuple[str, ...] = ()

    #: Which standing question this is, from `repository.GameRepository
    #: .pause_generation` - monotonic per game, bumped only when the pause
    #: slot's question is replaced. A seat echoes it back as `DecideRequest
    #: .generation` to say which question it believes it is answering, and
    #: an answer carrying a superseded one is refused rather than applied to
    #: whatever is standing now. Not an engine field: `from_engine` takes it
    #: from the caller, which is `0` where there is no store to ask.
    generation: int = 0

    @classmethod
    def from_engine(cls, decision: Decision, generation: int = 0) -> PendingDecision:
        """This question, shaped for a view. `generation` is the store's own
        `pause_generation` for the game the question belongs to - it is not
        an engine fact and the engine knows nothing about it, so callers pass
        it in; `0` is what a caller with no store to ask means."""
        return cls(
            generation=generation,
            player=decision.player,
            kind=decision.kind.value,
            prompt=decision.prompt,
            source=decision.source,
            source_card_id=None if decision.source_id is None else str(decision.source_id),
            amount=decision.amount,
            cards=tuple(str(card_id) for card_id in decision.cards),
            # `getattr`: an engine older than the Reality Fracture bump
            # (`CHOOSE_PLAYERS`) still has to serve, and it carries no
            # `players` field at all - the same fallback `moved_label`/
            # `card_labels` already take just below.
            players=tuple(getattr(decision, "players", ()) or ()),
            targets=tuple(_target_view(ref) for ref in decision.targets),
            target_maximum=decision.target_maximum,
            target_minimum=decision.target_minimum,
            affirmative_is_legal=decision.affirmative_is_legal,
            options=decision.options,
            # `getattr` rather than attribute access: an engine older than the
            # `moved_label`/`kept_label` contract still has to serve, and the
            # empty string is the same "no label was stamped" the fields
            # themselves default to, which every client already falls back on.
            moved_label=getattr(decision, "moved_label", ""),
            kept_label=getattr(decision, "kept_label", ""),
            card_labels=tuple(getattr(decision, "card_labels", ()) or ()),
            abilities=tuple(
                {
                    "kind": "ability",
                    "id": str(ref.id),
                    "name": ref.source_name,
                    "controller": ref.controller,
                    "text": ref.text,
                    "source_card_id": ref.source_id,
                }
                for ref in getattr(decision, "abilities", ()) or ()
            ),
            exact=getattr(decision, "exact", True),
            # `default_cards` is `None` on the engine for "no deviation
            # stamped", and that is not the same as "the empty answer" - which
            # for an `exact=False` question is a legal answer a player may
            # actually give. So the distinction survives the crossing rather
            # than being flattened into `()`.
            default_cards=(
                None
                if getattr(decision, "default_cards", None) is None
                else tuple(str(card_id) for card_id in decision.default_cards or ())
            ),
            # `getattr`: an engine older than schema 41 carries no
            # `default_may` at all, and `None` is the same "no deviation
            # stamped" every such engine already meant by leaving it out.
            default_may=getattr(decision, "default_may", None),
            lethal_damage=tuple(getattr(decision, "lethal_damage", ()) or ()),
            trample=bool(getattr(decision, "trample", False)),
            mana_sources=tuple(
                str(card_id) for card_id in getattr(decision, "mana_sources", ()) or ()
            ),
        )

    def _answer_argument(self, kind: DecisionKind) -> str:
        """The one argument that answers this question, written out - the
        value half of `answer_with`'s `<field>=<value>`.

        Concrete wherever the decision itself makes it concrete: a `may`
        whose yes branch is illegal (`affirmative_is_legal` false) can only
        be answered `false`, so that is what the template says. Everywhere a
        real choice remains, the value is an angle-bracket placeholder that
        names *where the candidates are* - `pending_decision.cards`,
        `.targets`, `.options` - because those lists are already on this
        same block. Quoted where the tool parameter is a string and bare
        where it is an `int` or a `bool`, so the template is the shape the
        call actually takes rather than something a seat must retype.
        """
        if kind is DecisionKind.PAY_MANA and self.affirmative_is_legal and self.mana_sources:
            # CR 608.2g / CR 605.3a: paying may also name which untapped
            # mana sources to activate, rather than leaving the engine to tap
            # them all - `mana_sources` mirrors `pending_decision.mana_sources`
            # itself (this same field, one call up), and is optional: omitting
            # it is still `accept=true` alone, which taps every offered source.
            return (
                'true, mana_sources=["<any of pending_decision.mana_sources, to activate '
                'while paying; omit to tap them all automatically>"]'
            )
        if kind in (
            DecisionKind.MAY,
            DecisionKind.PAY_LIFE,
            DecisionKind.PAY_ENERGY,
            DecisionKind.PAY_MANA,
            DecisionKind.OPENING_HAND_ACTION,
        ):
            # CR 608.2d's "you may": yes is the answer worth spelling out,
            # and where yes is not legal at all (too little life, energy or
            # floating mana) no is the *only* answer, so the template says so
            # rather than offering a choice the route would refuse.
            return "true" if self.affirmative_is_legal else "false"
        if kind is DecisionKind.MULLIGAN:
            # CR 103.5. Not defaulted either way: unlike a "you may", neither
            # branch here is the one the question is nudging towards.
            return "<true to take a mulligan, false to keep this hand>"
        if kind is DecisionKind.PUT_ON_TOP_OR_BOTTOM:
            return "<true for the top of the library, false for the bottom>"
        if kind is DecisionKind.PUT_INTO_HAND_OR_GRAVEYARD:
            return "<true for your hand, false for your graveyard>"
        if kind is DecisionKind.CHOOSE_PILE:
            # CR 700.3's pile primitive: `pending_decision.cards` is pile
            # one's own contents (the first player's division), so the
            # complement of it, among whatever this player controls, is pile
            # two.
            return "<true for pile one (pending_decision.cards), false for the other pile>"
        if kind in (
            DecisionKind.PAY_ENERGY_AMOUNT,
            DecisionKind.PAY_LIFE_AMOUNT,
            DecisionKind.CHOOSE_AN_AMOUNT,
        ):
            # `amount` is the ceiling - the payer's own energy or life (CR
            # 118.3, CR 119.4) for the two pay-amount kinds, or whatever
            # ceiling this resolution offered (CR 608.2d) for
            # `choose_an_amount`; 0 is always legal and always the decline.
            return f"<an integer from 0 to {self.amount}>"
        if kind is DecisionKind.CHOOSE_CARDS:
            if self.amount == 1:
                return '"<one of pending_decision.cards>"'
            if self.exact:
                return f'"<exactly {self.amount} of pending_decision.cards, comma-separated>"'
            # CR 107.1c / CR 701.23b: any count from zero up, and the empty
            # answer is the decline rather than a missing answer.
            return (
                f'"<up to {self.amount} of pending_decision.cards, comma-separated; '
                f'empty declines>"'
            )
        if kind is DecisionKind.DREDGE:
            # CR 702.52a: at most one of `pending_decision.cards`, or an
            # empty answer to draw normally instead of dredging.
            return '"<at most one of pending_decision.cards, or empty to draw normally>"'
        if kind is DecisionKind.CHOOSE_PLAYERS:
            # `CHOOSE_CARDS`'s own template, over `players` instead of
            # `cards` - every printed proliferate in the pool is CR 107.1c's
            # "any number", so this kind is never `exact=True` in practice,
            # but the same shape check applies if it ever were.
            if self.amount == 1:
                return '"<one of pending_decision.players>"'
            if self.exact:
                return f'"<exactly {self.amount} of pending_decision.players, comma-separated>"'
            return (
                f'"<up to {self.amount} of pending_decision.players, comma-separated; '
                f'empty declines>"'
            )
        if kind is DecisionKind.ORDER_TRIGGERS:
            # CR 603.3b: a permutation of exactly what was offered, bottom of
            # the stack first.
            return (
                '"<every id in pending_decision.cards, comma-separated, bottom of the stack first>"'
            )
        if kind in (DecisionKind.SCRY, DecisionKind.SURVEIL):
            # The engine stamps where the two halves actually go, because the
            # `SCRY` kind is reused for any look-then-split and the keyword's
            # own pair is then wrong (see `moved_label`'s own note). Only
            # when nothing was stamped does the keyword's rule stand in -
            # CR 701.22a's bottom for a scry, CR 701.25a's graveyard for a
            # surveil - which is exactly the fallback every client already
            # makes, said out loud here rather than left to be inferred.
            moved = self.moved_label or (
                "the bottom of your library" if kind is DecisionKind.SCRY else "your graveyard"
            )
            # `amount`/`exact` are not a `CHOOSE_CARDS`-only shape (see that
            # field's own docstring caveat): `Game.ask`/`validate_answer`
            # enforce the identical count check on every `CardSplit`, scry's
            # own "any number" included (CR 107.1c, exact=False, amount equal
            # to the whole look), Waker of Waves/Codecracker Hound's "put one
            # of them" (exact=True, amount=1), and Devourer of Destiny's
            # "may put one back" (exact=False, amount=1, a real ceiling
            # rather than "any"). Saying "any of pending_decision.cards"
            # regardless was silent about a count the engine actually
            # refuses an answer for - see `validate_answer`'s own SCRY/
            # SURVEIL branch, which this mirrors word for word.
            if self.exact:
                span = f"exactly {self.amount}"
            elif self.amount == 1:
                span = "up to 1"
            elif self.amount == len(self.cards):
                span = "any number, including none,"
            else:
                span = f"up to {self.amount}"
            return f'"<{span} of pending_decision.cards, comma-separated; these go to {moved}>"'
        if kind is DecisionKind.CHOOSE_TARGETS:
            floor = self.target_minimum or 0
            ceiling = self.target_maximum
            if ceiling is None:
                span = "ids"
            elif floor and floor == ceiling:
                span = f"exactly {ceiling}"
            elif floor:
                span = f"{floor} to {ceiling}"
            else:
                span = f"up to {ceiling}"
            return f'"<{span} of pending_decision.targets, by id or player name>"'
        if kind in (DecisionKind.CHOOSE_A_QUALITY, DecisionKind.CHOOSE_A_MODE):
            if self.options:
                return '"<one of pending_decision.options>"'
            # CR 614.12a's open universe - a card name or a creature type,
            # which is why `options` is empty rather than enumerated.
            return '"<a card name or a creature type>"'
        if kind is DecisionKind.DECLARE_ATTACKERS:
            # CR 508.1. Single-quoted so the JSON list inside stays readable.
            return (
                '\'[{"card": "<your creature id>", '
                '"defender": "<a defending player or planeswalker id>"}]\''
            )
        if kind is DecisionKind.ASSIGN_COMBAT_DAMAGE:
            # CR 510.1c/702.19b: one (target, amount) pair per recipient in
            # `pending_decision.targets` (blockers, plus the defending player
            # or planeswalker once trample lets damage reach past them), and
            # the amounts must sum to exactly `pending_decision.amount`.
            return (
                '\'[{"target": "<a pending_decision.targets id or player name>", '
                '"amount": <int>}]\''
            )
        # CR 509.1, the only kind left.
        return '\'[{"blocker": "<your creature id>", "blocked": "<an attacker id>"}]\''

    def answer_with(self) -> str:
        """This decision, as the call that answers it - paste-ready, naming
        this question's own `generation`.

        The whole point of the field: `kind` -> parameter was documented only
        in the MCP `decide` tool's own ~200-line docstring, so answering cost
        a seat a read and a derivation before it could say anything at all,
        and twelve of the twenty-four kinds do not answer on the parameter their
        engine-side field name suggests (`TOOL_FIELD_FOR_KIND`).

        `generation` rides along on every `decide` template because echoing
        it is what turns "no pending decision for you" - the flat refusal a
        stale read earns - into the diagnostic that names both generations
        and says the resolution has moved on (`app.py`'s `decide`). The two
        combat declarations get no `generation`: `declare_attackers`/
        `declare_blockers` take no such parameter, and a template must never
        name an argument its tool does not have.
        """
        kind = DecisionKind(self.kind)
        tool = TOOL_FOR_KIND[kind]
        call = f"{TOOL_FIELD_FOR_KIND[kind]}={self._answer_argument(kind)}"
        if tool == "decide":
            call = f"{call}, generation={self.generation}"
        return f"{tool}({call})"

    def to_view(self) -> dict[str, Any]:
        view = {
            "generation": self.generation,
            "player": self.player,
            "kind": self.kind,
            "prompt": self.prompt,
            "source": self.source,
            "source_card_id": self.source_card_id,
            "amount": self.amount,
            "cards": list(self.cards),
            "players": list(self.players),
            "targets": [dict(ref) for ref in self.targets],
            "target_maximum": self.target_maximum,
            "target_minimum": self.target_minimum,
            "affirmative_is_legal": self.affirmative_is_legal,
            "options": list(self.options),
            "moved_label": self.moved_label,
            "kept_label": self.kept_label,
            "card_labels": list(self.card_labels),
            "exact": self.exact,
            "default_cards": None if self.default_cards is None else list(self.default_cards),
            "default_may": self.default_may,
            "lethal_damage": list(self.lethal_damage),
            "trample": self.trample,
            "mana_sources": list(self.mana_sources),
        }
        # `ORDER_TRIGGERS` only - see `abilities`' own docstring. Every other
        # kind's payload stays byte-identical: this key is absent, not empty,
        # when there is nothing to add, so nothing already reading `to_view`
        # sees a new key it did not ask for.
        if self.abilities:
            view["card_details"] = [dict(entry) for entry in self.abilities]
        view.update(
            {
                # The two fields that make this block answerable in one call
                # rather than in one call plus a docstring read - see
                # `answer_with` and `TOOL_FIELD_FOR_KIND`. Last in the dict on
                # purpose: everything above is what the question *is*, and
                # these two are what to do about it.
                "answer_field": TOOL_FIELD_FOR_KIND[DecisionKind(self.kind)],
                "answer_with": self.answer_with(),
            }
        )
        return view


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


def pay_mana_answer(accept: bool | None, mana_sources: list[str] | None) -> bool | tuple[UUID, ...]:
    """A `decide` request body's `accept`/`mana_sources`, in the shape
    `Game.ask` expects for `PAY_MANA` (CR 608.2g, CR 605.3a) - either a bare
    `bool` (`bool_answer`'s own shape, still legal: `accept: true` alone taps
    every offered source automatically) or, when `mana_sources` is given
    (engine PR #17, B6-08), a `tuple[UUID, ...]` naming which of
    `pending_decision.mana_sources` to activate instead.

    `mana_sources` is `None` (the default) for the plain yes/no shape every
    `pay_mana` answer had before this - `card_ids_answer`'s per-id parsing
    runs the same way here, so a malformed id is refused with a 400 rather
    than reaching the engine. An empty list is a real, distinct answer (pay
    from the floating pool alone, activating none of the offered sources),
    so it is not the same as omitting the field. Whether each id was actually
    offered, and whether "yes" is legal at all, is `validate_answer`'s job,
    matching `card_ids_answer`'s own division of labour.

    Naming sources while declining is refused here, matching the engine's own
    `Answer._check_one_value_field`: "a pay_mana answer names mana sources
    only when it pays".
    """
    accepted = bool_answer(accept)
    if mana_sources is None:
        return accepted
    if not accepted:
        raise ValueError(
            "this decision names mana sources only when it pays ('accept' must be true)"
        )
    return tuple(parse_id(source_id, "mana source id") for source_id in mana_sources)


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
    still run when the paused command replays and are not duplicated here;
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
    still run when the paused command replays and are not duplicated here.
    """
    if life_amount is None:
        raise ValueError("this decision needs an integer amount of life ('life_amount')")
    return life_amount


def choose_amount_answer(amount: int | None) -> int:
    """A `decide` request body's `amount`, in the shape `Game.ask` expects
    for `CHOOSE_AN_AMOUNT` - "put up to N +1/+0 counters on this creature"
    (Clockwork Steed) and any other resolution-time "choose an amount from 0
    up to N" with no cost or payment attached (CR 608.2d): a plain `int`
    from 0 up to `pending_decision.amount` (the ceiling this question
    offers). `life_amount_answer`'s own shape, with nothing paid: distinct
    from both `energy_amount`/`life_amount` - a client that answered this
    kind on either of those fields would be naming what gets paid away, and
    nothing here is paid at all.

    Refuses a missing amount with a 400-shaped `ValueError` here, matching
    `life_amount_answer`'s reasoning - `Game.ask`'s own `isinstance` and
    range checks (0..`pending_decision.amount`) still run when the paused
    command replays and are not duplicated here.
    """
    if amount is None:
        raise ValueError("this decision needs an integer amount ('amount')")
    return amount


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
    paused command replays and is not duplicated here.
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


def hand_or_graveyard_answer(to_hand: bool | None) -> bool:
    """A `decide` request body's `to_hand`, in the shape `Game.ask` expects
    for `PUT_INTO_HAND_OR_GRAVEYARD` (Dina's Guidance's "put it into your
    hand or graveyard", CR 608.2d) - a plain `bool`, `True` for the hand,
    `False` for the graveyard. Refuses a missing answer, matching
    `top_or_bottom_answer`; its own field, not `accept`, for the same reason
    `top` is: a two-way zone choice, not a yes/no.
    """
    if to_hand is None:
        raise ValueError("this decision needs a hand-or-graveyard answer ('to_hand')")
    return to_hand


def pile_answer(pile_one: bool | None) -> bool:
    """A `decide` request body's `pile_one`, in the shape `Game.ask` expects
    for `CHOOSE_PILE` (CR 700.3's pile primitive, Liliana of the Veil's own
    `-6`, Do or Die) - a plain `bool`, `True` for pile one
    (`pending_decision.cards`), `False` for its complement.

    Refuses a missing answer with a 400-shaped `ValueError`, matching
    `top_or_bottom_answer`'s reasoning: `Game.ask`'s own `isinstance(value,
    bool)` check would raise the same way but inside the other request
    thread, where nobody would ever see it. Its own request field
    (`pile_one`), not `accept`, so a client cannot confuse this two-way
    choice for a yes/no.
    """
    if pile_one is None:
        raise ValueError("this decision needs a pile answer ('pile_one')")
    return pile_one


def usernames_answer(usernames: list[str]) -> tuple[str, ...]:
    """A `decide` request body's `usernames`, in the shape `Game.ask` expects
    for `CHOOSE_PLAYERS` (CR 701.34a's own Proliferate player half) -
    `card_ids_answer`'s own twin, a `tuple[str, ...]` with no id parsing at
    all: a username is not a `UUID`, so there is nothing here to malform the
    way a card id could. Whether each name was actually offered, and whether
    the right count was named, is `validate_answer`'s job, matching
    `card_ids_answer`'s own division of labour.
    """
    return tuple(usernames)


def card_split_answer(moved: list[str], top_order: list[str] | None) -> CardSplit:
    """A `decide` request body's `moved`/`top_order`, in the shape `Game.ask`
    expects for `SCRY` and `SURVEIL` - a `CardSplit` (CR 701.22a, CR 701.25a).

    `moved` names the cards leaving the top - to the bottom for a scry, to the
    graveyard for a surveil; every other card looked at goes back on top, in
    `top_order` if given or unchanged otherwise. Malformed ids are refused
    here with a 400 rather than reaching the engine, matching
    `card_ids_answer`.
    """
    moved_ids = tuple(parse_id(card_id) for card_id in moved)
    order_ids = None if top_order is None else tuple(parse_id(card_id) for card_id in top_order)
    return CardSplit(moved=moved_ids, top_order=order_ids)


#: The one shape every card and ability id takes on the wire.
ID_FORMAT = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"


def malformed_id_reason(value: object, noun: str = "card id") -> str:
    """The refusal for an id that is not one (D15): it names the bad value and
    the full form, rather than a parser's own complaint about its length.
    Shared with `app._validation_error`, which says the same for a `UUID`
    request field."""
    return (
        f"{noun} {value!r} is not a full {noun}: expected the 36-character UUID "
        f"form {ID_FORMAT} (hex digits), exactly as the state view prints it - "
        "a shortened or truncated id cannot be resolved"
    )


def player_or_id_reason(token: object, role: str = "target") -> str:
    """The refusal for a "player name or card id" token that is neither (D15):
    it may be a truncated id or a misspelt name, so it names the token and
    the full id form both."""
    return (
        f"{role} {token!r} is neither a player name nor a card id: a card id is the "
        f"36-character UUID form {ID_FORMAT} (hex digits), exactly as the state view "
        "prints it - a shortened or truncated id cannot be resolved"
    )


def player_or_card(token: str, players: Collection[str], role: str = "target") -> str | UUID:
    """`token` as the player it names, or as the card id it is, or
    `ValueError(player_or_id_reason(...))`."""
    if token in players:
        return token
    try:
        return UUID(token)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(player_or_id_reason(token, role)) from exc


def parse_id(token: str, noun: str = "card id") -> UUID:
    """`token` as a `UUID`, or `ValueError(malformed_id_reason(...))`."""
    try:
        return UUID(token)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(malformed_id_reason(token, noun)) from exc


def card_ids_answer(card_ids: list[str]) -> tuple[UUID, ...]:
    """A `decide` request body's `card_ids`, in the shape `Game.ask` expects
    for `CHOOSE_CARDS` - a `tuple[UUID, ...]`, so a malformed id is refused
    with a 400 here rather than reaching the engine at all. Whether the
    right *number* was chosen, and whether each id was actually one of the
    candidates offered, is `validate_answer`'s job, run against the same
    `Decision` `Game.ask` itself would check - see that function's
    docstring for why that check happens here now rather than being left
    to surface, late and misattributed, out of a resumed command's replay.
    """
    return tuple(parse_id(card_id) for card_id in card_ids)


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
    return tuple(parse_id(ability_id, "ability id") for ability_id in order)


def targets_answer(targets: list[str], players: Collection[str]) -> tuple[TargetRef, ...]:
    """A `decide` request body's `targets`, in the shape `Game.ask` expects
    for `CHOOSE_TARGETS` - a `tuple[TargetRef, ...]`.

    Each token is a card or ability id (a UUID string) or a player's username,
    told apart the same way `TargetRef.coerce` already tells a bare UUID from
    a bare string apart - no different from `card_ids_answer` in that this
    is pure parsing and never touches the `Game`. Whether the parsed
    reference was actually one of the candidates offered, and whether enough
    of them were given, is `validate_answer`'s job; a malformed token is
    refused here with a 400 rather than reaching the engine at all, matching
    `card_ids_answer`. `players` is the game's seat names: a token that is
    neither one of them nor a UUID is refused naming the id form (D15).
    """
    refs = []
    for token in targets:
        thing = player_or_card(token, players)
        refs.append(
            TargetRef.to_card(thing) if isinstance(thing, UUID) else TargetRef.to_player(thing)
        )
    return tuple(refs)


def declared_pairs_answer(
    pairs: list[DeclaredPairRequest], players: Collection[str]
) -> tuple[DeclaredPair, ...]:
    """A `decide` request body's `pairs`, in the shape `_ask_declaration`
    (behind `ask_attackers`/`ask_blockers`) expects for `DECLARE_ATTACKERS`/
    `DECLARE_BLOCKERS` - a `tuple[DeclaredPair, ...]` (CR 508.1, CR 509.1).

    Each entry's `defender` is a card id or a player's username, told apart
    the same way `targets_answer` tells `CHOOSE_TARGETS`'s tokens apart -
    resolved here into `DeclaredPair.defender_card`/`defender_player`, the
    "name what you mean" shape the engine's tape needs to replay an attack on
    a player (who has no id of their own) as faithfully as an attack on a
    planeswalker. A malformed defender token never reaches the engine at all,
    matching `targets_answer`/`card_ids_answer`.
    """
    declared = []
    for pair in pairs:
        defender = player_or_card(pair.defender, players, "defender")
        if isinstance(defender, UUID):
            declared.append(DeclaredPair(creature=pair.creature, defender_card=defender))
        else:
            declared.append(DeclaredPair(creature=pair.creature, defender_player=defender))
    return tuple(declared)


def damage_answer(
    entries: list[DamageAssignmentRequest], players: Collection[str]
) -> tuple[DamageAssignment, ...]:
    """A `decide` request body's `damage`, in the shape `Game.ask` expects for
    `ASSIGN_COMBAT_DAMAGE` - a `tuple[DamageAssignment, ...]` (CR 510.1c,
    CR 702.19b).

    Each entry's `target` is a card id (a blocker) or a player's username (the
    defending player or planeswalker, offered only with trample), told apart
    the same way `declared_pairs_answer` tells a `defender` token apart -
    resolved here into `DamageAssignment.target_card`/`target_player`. A
    malformed target token never reaches the engine at all, matching
    `declared_pairs_answer`/`targets_answer`. Whether each named target was
    actually offered, whether the amounts sum to the attacker's power, and
    whether trample damage reaches past a blocker with less than its own
    lethal damage assigned is `validate_answer`'s job (via the engine's own
    `damage_assignment_answer`), not this parser's.
    """
    assigned = []
    for entry in entries:
        target = player_or_card(entry.target, players)
        if isinstance(target, UUID):
            assigned.append(DamageAssignment(target_card=target, amount=entry.amount))
        else:
            assigned.append(DamageAssignment(target_player=target, amount=entry.amount))
    return tuple(assigned)


def _illegal_option_refusal(decision: Decision) -> str:
    """`models/game.py`'s own `_illegal_option_refusal`, word for word - the
    refusal for choosing an option CR 608.2d rules out. Shared by
    `validate_answer`'s bool branch (`MAY`/`PAY_LIFE`/`PAY_ENERGY`/`PAY_MANA`
    answered `True` or `False`) and its `PAY_MANA` tuple branch below, which
    hits the identical "yes is not legal at all" case through a different
    answer shape."""
    refusal = (
        f"{decision.prompt}: that option is illegal, so CR 608.2d does not allow choosing "
        "it (\"The player can't choose an option that's illegal or impossible\")"
    )
    if decision.kind is DecisionKind.PAY_MANA:
        refusal += (
            " - the floating mana and the untapped mana sources available cannot pay it "
            "(CR 118.3: \"A player can't pay a cost without having the necessary "
            'resources to pay it fully")'
        )
    return refusal


def validate_answer(decision: Decision, value: object) -> None:
    """Refuse `value` with a `ValueError` if the engine's own asker for
    `decision.kind` would refuse it too - run from `decide` *before* the
    answer is ever appended to a paused command's tape, so the refusal is
    synchronous and reaches the answering seat directly rather than surfacing
    later, out of `commands.resume_command`, attached to a divergence or a
    completed-but-wrong replay.

    This exists because of exactly the shape of bug it closes: a
    `choose_cards` answer with the wrong number of cards used to sail past
    every check in this module (`card_ids_answer` only parses UUIDs) under
    the old blocking transport, get logged `allowed: true`, and get handed to
    the thread parked in `Game.ask` - which *does* check the count, and
    raises "needs 1 card(s) chosen, 0 given" on the resolving thread, not
    this request, misattributing the refusal to whatever unrelated request
    next drove the stack forward. Pausing closes the misattribution half of
    that (a bad answer now surfaces as a `ValueError` out of the very
    `resume_command` call that tried it), but running the check here first
    still matters: it keeps an obviously bad answer from ever reaching the
    replay machinery at all, with the same synchronous, well-worded refusal
    this API has always given.

    `Game.ask`'s validation itself is not called from here directly -
    `machines/decisions.py`'s own `Decision` is "deliberately stateless" (its
    own docstring) - every bound `Game.ask` checks (`amount`, `exact`,
    `cards`, `targets`, `target_maximum`, `target_minimum`, `options`,
    `affirmative_is_legal`) is already baked into the `Decision` the engine
    handed this API when it asked the question, via `legal_options` for the
    yes/no kinds and plain field comparisons for the rest. So this function
    runs the identical checks `Game.ask` runs, in the same order, against
    that same data - not a second opinion invented by this API, but the same
    arithmetic the engine already computed, read back out of the `Decision`
    it built. `legal_options` itself is imported and called, not re-derived,
    so at least the yes/no branch can never drift from the engine's own
    answer to "what may this player pick".

    Only kinds in `ANSWERABLE_KINDS` are ever passed here - `decide` already
    refuses every other kind before parsing an answer at all - so every
    branch of `Game.ask`'s own kind dispatch has a match here. Four kinds do
    not go through `Game.ask` at all and are matched against their own asker
    in `machines/decisions.py` instead: `MULLIGAN` against `ask_mulligan`,
    `ORDER_TRIGGERS` (CR 603.3b) against `ask_trigger_order`, and
    `DECLARE_ATTACKERS`/`DECLARE_BLOCKERS` (CR 508.1, CR 509.1) against the
    shape check `_ask_declaration` itself runs - full attack/block legality
    (an untapped creature, a legal defender, ...) is `Game.declare_attackers`/
    `Game.declare_blockers`'s own job, run when the paused command replays
    with the new answer, not this function's.
    """
    if decision.kind is DecisionKind.PAY_MANA and isinstance(value, tuple):
        # CR 608.2g / CR 605.3a: paying by naming which offered mana sources
        # to activate, engine PR #17 (B6-08) - `Game.ask`'s own tuple branch,
        # checked and worded identically, and checked *before* the plain
        # yes/no branch below so a tuple answer never falls into "needs a
        # bool answer" the way a stray non-bool would.
        if not all(isinstance(card_id, UUID) for card_id in value):
            raise ValueError(f"{decision.prompt}: needs a tuple of mana source ids")
        offered = set(decision.mana_sources)
        stray = [str(card_id) for card_id in value if card_id not in offered]
        if stray:
            raise ValueError(
                f"{decision.prompt}: {', '.join(stray)} is not one of the mana sources "
                f"{decision.player} can activate while paying (CR 605.3a)"
            )
        if True not in legal_options(decision):
            raise ValueError(_illegal_option_refusal(decision))
        return

    if decision.kind in (
        DecisionKind.MAY,
        DecisionKind.PAY_LIFE,
        DecisionKind.PAY_ENERGY,
        DecisionKind.PAY_MANA,
    ):
        if not isinstance(value, bool):
            raise ValueError(f"{decision.prompt}: a yes-or-no question needs a bool answer")
        if value not in legal_options(decision):
            raise ValueError(_illegal_option_refusal(decision))
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
            # tape - so there is nothing here to match a name against.
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

    if decision.kind is DecisionKind.CHOOSE_AN_AMOUNT:
        # `Game.ask`'s own wording for this kind, word for word - see that
        # branch in `models/game.py`, kept to parity by
        # `test_decision_validator_drift`.
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{decision.prompt}: needs an integer amount")
        if value < 0 or value > decision.amount:
            raise ValueError(
                f"{decision.prompt}: cannot choose {value}, at most {decision.amount} "
                f"is legal (CR 608.2d)"
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

    if decision.kind is DecisionKind.DREDGE:
        # CR 702.52a: at most one card, always one of the offered candidates
        # - the engine's own `Game.ask` re-checks this identically (`models/
        # game.py`), this is the synchronous, same-request half.
        if not isinstance(value, tuple) or not all(isinstance(v, UUID) for v in value):
            raise ValueError(f"{decision.prompt}: needs a tuple of at most one dredge card")
        if len(value) > 1:
            raise ValueError(
                f"{decision.prompt}: only one dredge card can replace a single draw (CR 702.52a)"
            )
        offered = set(decision.cards)
        stray = [str(card_id) for card_id in value if card_id not in offered]
        if stray:
            raise ValueError(
                f"{decision.prompt}: cannot dredge a card that was not offered: {', '.join(stray)}"
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

    if decision.kind is DecisionKind.PUT_INTO_HAND_OR_GRAVEYARD:
        if not isinstance(value, bool):
            raise ValueError(f"{decision.prompt}: needs a hand-or-graveyard bool answer")
        return

    if decision.kind is DecisionKind.CHOOSE_PILE:
        if not isinstance(value, bool):
            raise ValueError(f"{decision.prompt}: needs a which-pile bool answer")
        return

    if decision.kind is DecisionKind.CHOOSE_PLAYERS:
        if not isinstance(value, tuple) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{decision.prompt}: needs a tuple of chosen usernames")
        offered = set(decision.players)
        stray = [username for username in value if username not in offered]
        if stray:
            raise ValueError(
                f"{decision.prompt}: cannot choose a player that was not offered: "
                f"{', '.join(stray)}"
            )
        if len(set(value)) != len(value):
            raise ValueError(f"{decision.prompt}: the same player cannot be chosen twice")
        if decision.exact:
            if len(value) != decision.amount:
                raise ValueError(
                    f"{decision.prompt}: needs {decision.amount} player(s) chosen, "
                    f"{len(value)} given"
                )
        elif len(value) > decision.amount:
            raise ValueError(
                f"{decision.prompt}: at most {decision.amount} player(s) may be chosen "
                f"(CR 107.1c), {len(value)} given"
            )
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
        # `amount`/`exact` are the printed count of cards the player may move:
        # scry's "any number" (exact=False, amount = cards looked at), Waker of
        # Waves' "put one of them" (exact). `Game.ask`'s own check, word for word.
        chosen = len(set(value.moved))
        if decision.exact and chosen != decision.amount:
            raise ValueError(
                f"{decision.prompt}: exactly {decision.amount} card(s) must be chosen, "
                f"{chosen} given (CR 608.2d)"
            )
        if chosen > decision.amount:
            raise ValueError(
                f"{decision.prompt}: at most {decision.amount} card(s) may be chosen, "
                f"{chosen} given (CR 608.2d)"
            )
        return

    if decision.kind in (DecisionKind.DECLARE_ATTACKERS, DecisionKind.DECLARE_BLOCKERS):
        # `_ask_declaration`'s own checks, word for word - the engine-side
        # authority for these two kinds, not `Game.ask`. Deliberately no
        # stray/legality check beyond shape: `_ask_declaration` runs none
        # either (full legality is `Game.declare_attackers`/
        # `declare_blockers`'s job, downstream of this), and adding one here
        # would disagree with the engine on an answer it would still accept -
        # see `test_decision_validator_drift.py`.
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{decision.prompt}: the answer is a list of pairs")
        for pair in value:
            if isinstance(pair, DeclaredPair):
                continue
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError(f"{decision.prompt}: each answer is a (creature, target) pair")
        return

    if decision.kind is DecisionKind.ASSIGN_COMBAT_DAMAGE:
        # `damage_assignment_answer` is the engine-side authority for this
        # kind (`Game.ask` simply calls it, see `models/game.py`) - CR 510.1a's
        # amounts summing to exactly the attacker's power, CR 510.1c's "one of
        # its blockers, or the defender with trample", and CR 702.19b's
        # trample-past-lethal-damage floor are all enforced there, word for
        # word; this runs the identical check rather than a second copy of it.
        damage_assignment_answer(decision, value)
        return

    raise ValueError(f"{decision.prompt}: decide does not know how to validate a {decision.kind!r}")
