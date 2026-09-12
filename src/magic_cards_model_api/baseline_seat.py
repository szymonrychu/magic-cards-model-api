"""A greedy, deterministic, zero-LLM seat - the opponent every measurement
of agent play strength is measured *against*.

There was no way to say whether a change made the agent play better. No
self-play runner, no scripted opponent, no win-rate script in either repo,
and 2 of 15 recorded games ever finished. A number needs a control, and this
is the control: a policy with no model behind it, no randomness in it, and
no memory, so two runs of the same seeds produce the same games and any
difference between arms belongs to the arm rather than to the opponent.

**It lives in the api repo, and it is not a rule.** The engine is a stateless
rules library (its own ROADMAP says so): it answers *what is legal*, and
CR 118.6 is the reason that answer is not a policy - "attempting to cast a
spell, activate an ability, or take a special action that has an unpayable
cost is a legal action", so the enumeration deliberately offers moves nobody
would make. Choosing among them is a seat's business. Putting this in
`magic-cards-model` would make the engine opinionated about play, which is
exactly the line that repo's `CLAUDE.md` draws.

## The policy, in full

Read in order; the first branch that has something to say wins.

1. **The game is over** - nothing to do.
2. **The pregame** (CR 103.5's mulligan step, CR 103.6's opening-hand step).
   Nobody holds priority there, so a seat that only knows how to pass would
   sit out the whole clock while the game waits on it. Keep the dealt hand,
   take no opening-hand action - `default_answer`'s own answer for both
   kinds.
3. **A decision addressed to this seat.** The two combat declarations get
   the judgement below; every other kind gets the engine's own
   `machines/decisions.default_answer` semantics, branch for branch.
4. **This seat has priority.**
   - A non-empty stack: pass, so the top resolves (CR 117.4). `advance` is
     refused outright while the stack is up, and passing is the only move
     that makes anything happen.
   - Play a land if one is legal. It costs nothing and it never comes back
     this turn (CR 305.2's one-per-turn), so it always precedes a cast.
   - Cast the highest-cost affordable **permanent**, then the highest-cost
     affordable **spell**. Board beats one-shot at equal price, and beats it
     at unequal price too - that is what "greedy" means here.
   - Otherwise pass priority. Passing is what walks the turn: everyone
     passing on an empty stack ends the step (CR 117.4), which is how this
     policy reaches combat and the end of its own turn without a second
     mechanism.
5. **A step that grants nobody priority** (untap, cleanup - CR 502.4,
   CR 514.3) on this seat's own turn: `advance`, the only way out, since
   CR 703.2's turn-based actions are "not controlled by any player".
6. Otherwise there is genuinely nothing to do: it is the opponent's move.

**Attacking** (CR 508.1a): attack with a creature when it survives the
profitable block, or when the race is lethal. A block is profitable *for the
defender* when the blocker kills the attacker and lives through it; a creature
attacks when no untapped opposing creature can make that block. And when the
attackers' combined power is at least the defender's life total, everything
attacks regardless - a trade that wins the game is not a bad trade.

**Blocking** (CR 509.1a): block only to prevent lethal. Sum the power of
everything attacking this seat; if that is below its life total, declare
nobody ("if any" makes that a real declaration). Otherwise assign blockers
to the biggest attackers, largest first, until the damage still getting
through is survivable - and stop there, because a chump block that changes
no outcome is a creature spent for nothing.

**Both declarations read their candidate pairs off the board, not off the
enumeration. That is an engine gap, named precisely here because "out of
scope" is not a description of one.**
`machines.legal_actions.decision_answers` does special-case
`DECLARE_ATTACKERS`/`DECLARE_BLOCKERS` and calls `_attacker_actions`/
`_blocker_actions` for the pairs, so the wiring exists - but both of those
return `[]` unless `game.turn.step` is already `Step.DECLARE_ATTACKERS`/
`DECLARE_BLOCKERS`, and while the matching decision stands it is not:
`advance` pauses to *ask* before it commits the step transition, so a standing
`declare_attackers` question is asked on a board still in
`beginning_of_combat`, and a standing `declare_blockers` one on a board still
in `declare_attackers`. Re-measured against engine `4ad120c`: at the one
moment the api will accept a declaration, `GET .../legal-actions` answers
exactly `[answer_decision: declare_nobody]` and no pairs. **What would close
it**, either half being enough: the step predicate in those two functions
accepts the pending decision's own kind as an alternative to the committed
step (`decision_answers` already knows the kind and already holds the
`Game`), or `Game.advance` commits the step before it pauses to ask. Both are
engine changes in `magic-cards-model`; neither is expressible from this repo.
Until one lands, candidates are built here the way `ui/combat.js` already
builds them ("nothing here enforces a rule"), and the engine's own refusal is
what actually decides: `BaselineSeat` drops one pair at a time from a refused
declaration until it is accepted, which bottoms out at the always-legal empty
answer.

## Why it cannot stall, and what that claim is worth

A stalling baseline is worse than no baseline: it manufactures the very
unfinished games the scorecard exists to count. **This section was wrong
once** - it listed five defences and two of them did not hold, and a 10-seed
run then finished 3 games and reported 7 timeouts. What is written here is
what the code does; where the guarantee is conditional, the condition is
named rather than rounded up.

- Every branch above ends in an action whenever one is legal at all -
  `pass_priority` and `concede` are always in the enumeration when this seat
  holds priority, so "nothing to do" is only ever reachable when it is
  genuinely not this seat's move.
- Every decision kind the api can answer has a branch in `decide_body`
  (`test_every_answerable_decision_kind_has_a_default_body` pins that), so a
  question can never stand unanswered.
- **No answer is posted twice against a board that has not moved.** A refused
  enumerated row is remembered by `action_key` and not offered again; a
  refused *answer* - a pass, a decision, a combat declaration, anything whose
  `Move.key` is `""` because no single row backs it - is remembered by
  `answer_fingerprint`. Both are forgotten when the board moves
  (`seatkit.board_signature`), because a refusal is a fact about a board.
  Only the first half of this existed: a `key == ""` refusal dropped the
  board signature instead, which re-derived the identical move AND threw the
  refusal memory away, and that was an unbounded loop of byte-identical
  posts.
- A cast whose targets this board cannot fill is declined *before* it is
  attempted (`BaselineSeat._pick`), because the board-moved rule above cannot
  save it: the opponent's own move changes the board every round and correctly
  clears the memory of the refusal. A live gauntlet put 547 identical "too few
  targets" rows in one game before this existed.
- A refused combat declaration is narrowed one pair at a time down to the
  empty answer, and the sequence is monotonically non-increasing and pins
  there (`attack_declaration`). It used to grow a pair back once `drop`
  passed the number of pairs, and the empty answer was a fixed point it
  re-posted forever if the engine refused it - which a CR 508.1d attacking or
  CR 509.1a blocking requirement can. Narrowing has no answer to a
  requirement; what happens instead is the point below.
- And `play_out` carries the backstop: a round in which no seat *advanced* -
  no accepted move, and no refusal that was new against this board
  (`BaselineSeat.advanced`) - is an idle round, and `stall_rounds`
  consecutive idle rounds end the game with a concession (CR 104.3a, legal at
  any time; `app._run_command` exempts it from the pause-slot guard so a
  question standing for the other seat cannot block it). That is a real
  ending, recorded as one, and it shows up in the scorecard as a loss rather
  than as a silent hang. `play_out` checks that the concession was accepted
  and reports `stall_guard_refused` if it was not, rather than spinning out
  the rest of `max_steps`. Reading "this seat attempted something" as
  progress, which is what `step`'s return value says, is what kept this guard
  from ever firing.

**The one case this does not cover**, stated rather than glossed: a board
that keeps *changing* without the game getting anywhere clears every seat's
refusal memory each round, so each round can hold a genuinely new refusal and
no round is idle. A refused action changes nothing, so a changing board means
some seat's move was accepted - i.e. the engine moved the game - and the
games this control plays are finite. `max_steps` is the outer bound on that
argument being wrong, and it is reported as `bound`: a defect in this module,
never a game the scorecard may count as a loss.

## What it deliberately does not do

No activated abilities beyond the mana the engine taps for it, no
alternative costs, no modal or X spells (their affordability is `unknown`
before a mode or an X is announced - CR 107.3a - so the enumeration already
excludes them from "affordable"), no casting from anywhere but the hand, no
search and no rollout. Each of those is a judgement, and a control that
makes judgements is a control that drifts. Targets are the one place it must
name something: it takes the least-committal legal answer, `minimum` targets
per requirement, preferring the opponent - the same shape `default_answer`
gives CR 601.2c.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from magic_cards_model.models.mana import ManaCost

from .api_client import SeatApiClient
from .seatkit import board_signature, untapped_mana_sources

#: What the baseline says when the api asks an agent seat why it did
#: something (`records.require_reasoning`). A policy's reason for an action
#: is the branch that produced it, so the branch is what it says - and it
#: says it in the same words every time, because a control that varied its
#: prose would be pretending to think.
REASONS = {
    "mulligan_keep": "baseline: keep the dealt hand (CR 103.5's unscripted default)",
    "opening_hand_done": "baseline: take no CR 103.6 opening-hand action",
    "play_land": "baseline: a land drop is free and does not come back this turn",
    "cast_permanent": "baseline: the most expensive permanent I can pay for",
    "cast_spell": "baseline: the most expensive spell I can pay for",
    "attack": "baseline: attackers that survive the profitable block, or a lethal race",
    "block": "baseline: block only what would otherwise be lethal",
    "decide": "baseline: the engine's own default answer for this question",
    "pass_priority": "baseline: nothing worth doing here",
    "advance": "baseline: nobody gets priority in this step (CR 703.2)",
    "concede": "baseline: no legal move left to make (CR 104.3a)",
}

#: A permanent is one of these (CR 110.4a's permanent types, plus battle).
#: Read off the *computed* type line the state view already carries, so an
#: animated land counts as the creature it currently is (CR 613.1d).
PERMANENT_TYPES = ("artifact", "battle", "creature", "enchantment", "land", "planeswalker")

#: How many consecutive rounds of every seat having nothing to say `play_out`
#: tolerates before it concedes the game rather than spin. Three rather than
#: one: a seat legitimately has nothing to do while the other is mid-turn,
#: and a round in which the *other* seat acted is not an idle round at all,
#: so this only ever counts rounds where the whole table was silent.
STALL_ROUNDS = 3


@dataclass(frozen=True)
class Move:
    """One call this policy has decided to make.

    `path` is relative to `/api/games/{game_id}/`, so the seam
    (`api_client.SeatApiClient.act`) is the only thing that ever builds a
    URL. `label` names the branch that produced it - it is what the seat
    tells the api as its reasoning, and what a reader of the ledger sees.
    """

    label: str
    path: str
    body: dict[str, Any]
    key: str = ""
    """The enumerated action this move came from (`action_key`), or `""` for
    a move that is not one of them (a pregame declaration, a concession).
    `BaselineSeat` remembers this on a refusal so the same move is not tried
    again against an unchanged board."""


# -- reading the enumeration -------------------------------------------------


def action_key(action: dict[str, Any]) -> str:
    """A stable identity for one enumerated action, across two reads of an
    unchanged board.

    Everything that distinguishes two rows of the same kind: which object,
    which of its abilities, which face or alternative cost it is announced
    under, and - for the combat declarations, where one creature has one row
    per legal defender - which pair. Deliberately not the whole row:
    `affordable`/`unaffordable_reason` move with the board, and a refusal
    this seat is trying to remember must not be forgotten because the row it
    came from now reads slightly differently.
    """
    return "|".join(
        str(action.get(field_name) or "")
        for field_name in (
            "kind",
            "card_id",
            "from_zone",
            "ability_index",
            "keyword",
            "alternative_cost",
            "spell_face",
            "land_face",
            "defender",
            "attacker_id",
            "decision_option",
        )
    )


def answer_fingerprint(move: Move) -> str:
    """A stable identity for one whole answer this seat posts.

    `action_key` identifies an enumerated *row*, and most moves have one -
    but `pass_priority`, `advance`, `decide` and the two pregame declarations
    do not (`Move.key` is `""`), and neither does a combat declaration, which
    is one answer built out of many rows. Those are exactly the moves a
    refusal could not be remembered for, so the loop they made was: post,
    refused, re-derive, post the identical body again, forever.

    This is what a refusal is remembered by instead - the path plus the body
    that went with it, minus `reasoning`, which is prose this seat chooses
    and never part of what the api ruled on. Two posts that would be
    byte-identical on the wire share a fingerprint; anything the policy
    actually changed (a narrowed declaration, a different card, different
    targets) does not.
    """
    body = {name: value for name, value in move.body.items() if name != "reasoning"}
    return f"{move.path}|{json.dumps(body, sort_keys=True, default=str)}"


def mana_value(action: dict[str, Any]) -> int:
    """This action's mana value, for "the most expensive one".

    Read off `Action.mana_cost`, which the engine documents as "just the mana
    portion of `cost`, as `ManaCost.parse` can read it back" - parsed with
    the engine's own parser rather than counted by hand, because a hybrid or
    phyrexian symbol is worth what the rules say it is worth and not what a
    symbol count would guess. An unreadable or absent cost is worth 0, which
    sorts it last among affordable candidates rather than dropping it.
    """
    raw = action.get("mana_cost")
    if not raw:
        return 0
    try:
        return ManaCost.parse(str(raw)).converted
    except (ValueError, KeyError):
        return 0


def _affordable(action: dict[str, Any]) -> bool:
    """CR 118.6 keeps an unpayable action legal, so the enumeration lists it -
    `affordable` is the only thing that tells "legal and payable" apart from
    "legal and not". `unknown` (a mode or an X not yet announced, CR 107.3a)
    is not a yes: this policy announces neither, so it would be guessing."""
    return action.get("affordable") == "yes"


def _plain_cast(action: dict[str, Any]) -> bool:
    """A printed cast from hand and nothing more exotic.

    Every field ruled out here is an announcement this policy does not make -
    an alternative cost (CR 118.9), a keyword cast, a chosen face (CR 709.3,
    CR 715.3), a mode (CR 700.2), an X (CR 107.3a), a splice (CR 702.47) -
    and announcing one badly is a refusal, which the scorecard would then
    report as a rules error the agent never made.

    `spell_face` stays on that list now that `CastRequest.spell_face` exists
    and the api can genuinely cast a split card's back half: the skip is this
    policy's own choice, not the capability gap it used to be, and
    `tests/test_cast_spell_face.py` pins both halves of that sentence by
    posting the very row this passes over. Two live facts keep the choice the
    right one. The engine's `Action.target_slots` is still face-blind
    (`legal_actions._cast_action` derives it from `Game._announcement_profile`,
    which takes no face), so both of a split card's rows report the FRONT
    half's phrase - "target artifact" on the Tear row - and `_fill_targets`
    reads exactly that, plus the candidates the api enriched it with from the
    same face-blind profile: this policy would announce an artifact for a
    spell that destroys enchantments and earn a refusal in the ledger. And an
    adventurer card's Adventure row (CR 715.3) is refused outright by the api,
    for the reason `engine_api.adventure_face_refusal` spells out; nothing on
    an enumerated row tells these two layouts apart without going back to the
    card. Take face rows here when the engine reads the announced face into
    `target_slots`, and not before.
    """
    return (
        action.get("from_zone") == "hand"
        and not action.get("alternative_cost")
        and not action.get("keyword")
        and not action.get("spell_face")
        and not action.get("modes")
        and not action.get("x_range")
        and not action.get("splice_options")
    )


def _card_view(state: dict[str, Any], card_id: str | None) -> dict[str, Any] | None:
    """The state view's own entry for `card_id`, wherever this seat can see
    it. The enumeration carries ids and never a type line, so this is how
    "is it a permanent" gets asked."""
    if not card_id:
        return None
    pools: list[Any] = [state.get("battlefield"), state.get("stack"), state.get("exile")]
    pools.extend((state.get("hands") or {}).values())
    pools.extend((state.get("graveyards") or {}).values())
    for pool in pools:
        for card in pool or ():
            if isinstance(card, dict) and card.get("id") == card_id:
                return card
    return None


def _is_permanent(card: dict[str, Any] | None) -> bool:
    if card is None:
        return False
    type_line = str(card.get("type_line") or "").lower()
    return any(word in type_line for word in PERMANENT_TYPES)


# -- the board judgements the enumeration does not make ----------------------


def _int(value: Any) -> int | None:
    """A power/toughness off a card view. `None` for a non-creature, and for a
    characteristic-defining `*` this build reports as a string.

    Never guessed at. A creature this policy cannot size is one it reads as
    unable to kill (`_kills` says no) and unable to be killed (`_survives_
    block` says yes) - which biases towards attacking, deliberately: the
    engine settles combat either way, and a control that invented a number
    would be making the one judgement it exists not to make."""
    return value if isinstance(value, int) else None


def _remaining_toughness(card: dict[str, Any]) -> int | None:
    toughness = _int(card.get("toughness"))
    if toughness is None:
        return None
    return toughness - int(card.get("damage") or 0)


def _creatures(state: dict[str, Any], controller: str) -> list[dict[str, Any]]:
    return [
        card
        for card in state.get("battlefield") or ()
        if card.get("controller") == controller
        and "creature" in str(card.get("type_line") or "").lower()
    ]


def opponent_of(state: dict[str, Any], player: str) -> str | None:
    for entry in state.get("players") or ():
        if entry.get("username") != player:
            return str(entry.get("username"))
    return None


def _life_of(state: dict[str, Any], username: str | None) -> int:
    for player in state.get("players") or ():
        if player.get("username") == username:
            return int(player.get("life") or 0)
    return 0


def _kills(blocker: dict[str, Any], attacker: dict[str, Any]) -> bool:
    power = _int(blocker.get("power"))
    toughness = _remaining_toughness(attacker)
    if power is None or toughness is None:
        return False
    return power >= toughness


def _survives_block(attacker: dict[str, Any], blocker: dict[str, Any]) -> bool:
    power = _int(attacker.get("power"))
    toughness = _remaining_toughness(blocker)
    if power is None or toughness is None:
        return True
    return power < toughness


def attack_candidates(state: dict[str, Any], player: str) -> list[dict[str, Any]]:
    """Creatures that could plausibly attack: mine, untapped, not summoning
    sick (CR 302.6).

    **A candidate list, never a legality ruling** - exactly what
    `ui/combat.js`'s `attackCandidates` is, and for the same stated reason:
    "the engine still decides what is legal - `actions/attack` refuses
    anything CR 508.1a would". Defender (CR 702.3b), a "can't attack unless"
    restriction, a propaganda tax: none of them is read here, because reading
    them here would be a second copy of a rule that already lives in
    `Game.why_cannot_attack`. `BaselineSeat` narrows a refused declaration
    instead, which lets the engine's own answer be the one that decides.

    This is read off the board rather than off `GET .../legal-actions`
    because the enumeration cannot offer the pairs at the one moment the api
    accepts them: a `declare_attackers` decision stands while the *committed*
    board is still in `beginning_of_combat`, and `machines.legal_actions.
    _attacker_actions` returns nothing outside `Step.DECLARE_ATTACKERS`. Any
    `declare_attacker` row that does show up is preferred over this - see
    `attack_declaration`.
    """
    return [
        card
        for card in _creatures(state, player)
        if not card.get("tapped") and not card.get("summoning_sick")
    ]


def block_candidates(state: dict[str, Any], player: str) -> list[dict[str, Any]]:
    """Creatures that could plausibly block: mine and untapped (CR 509.1a -
    summoning sickness does not stop a block). A candidate list on the same
    terms as `attack_candidates`; flying, menace, protection and every other
    restriction stay the engine's to enforce."""
    return [card for card in _creatures(state, player) if not card.get("tapped")]


def attack_declaration(
    state: dict[str, Any],
    actions: Sequence[dict[str, Any]],
    player: str,
    *,
    drop: int = 0,
) -> list[dict[str, str]]:
    """CR 508.1a's declaration, as this policy makes it: `[{card_id,
    defender}]`, possibly empty.

    Attack with a creature when it survives the profitable block - no
    untapped opposing creature both kills it and lives - or when the whole
    team's power is at least the defender's life total, which is a race the
    trades no longer matter in.

    `drop` removes that many pairs from the end, in id order: the narrowing
    `BaselineSeat` applies after the engine has refused a declaration, since
    the engine's refusal is about the whole answer rather than one pair. The
    sequence it generates is monotonically non-increasing and pins at the
    empty declaration: `drop >= len(chosen)` is `[]`, and every larger `drop`
    is `[]` again.

    **That is termination of the sequence, not a guarantee the engine accepts
    where it lands.** The empty declaration is legal in the ordinary case
    (CR 508.1a's "if any"), but an attacking requirement - "attacks each
    combat if able", CR 508.1d - can refuse it, and narrowing has no answer
    to a requirement: shrinking moves away from it. `BaselineSeat` therefore
    treats an answer it has already had refused against an unchanged board as
    nothing left to say, and `play_out`'s stall guard is what ends such a
    game (see this module's "Why it cannot stall").
    """
    enumerated = {
        str(action.get("card_id")): str(action.get("defender") or "")
        for action in actions
        if action.get("kind") == "declare_attacker"
    }
    opponent = opponent_of(state, player)
    if enumerated:
        pairs = enumerated
    else:
        pairs = {str(card.get("id")): opponent or "" for card in attack_candidates(state, player)}
    if not pairs or not opponent:
        return []

    blockers = [card for card in _creatures(state, opponent) if not card.get("tapped")]

    def survives_every_profitable_block(attacker: dict[str, Any]) -> bool:
        return not any(
            _kills(blocker, attacker) and _survives_block(attacker, blocker) for blocker in blockers
        )

    candidates = {
        card_id: card
        for card_id, card in ((card_id, _card_view(state, card_id)) for card_id in pairs)
        if card is not None
    }
    # The race: everything that could attack, against the life that is
    # actually there. A trade that ends the game is not a bad trade.
    team = sum(_int(card.get("power")) or 0 for card in candidates.values())
    lethal_race = team >= _life_of(state, opponent) > 0
    chosen = [
        {"card_id": card_id, "defender": pairs[card_id]}
        for card_id, card in sorted(candidates.items())
        if lethal_race or survives_every_profitable_block(card)
    ]
    # `max(..., 0)`, never a bare `len(chosen) - drop`: that is a negative
    # slice once `drop` passes `len(chosen)`, and a negative slice puts pairs
    # BACK - with two pairs, `drop == 3` answered `chosen[:-1]`, so the
    # sequence oscillated instead of bottoming out.
    return chosen[: max(len(chosen) - drop, 0)]


def block_declaration(
    state: dict[str, Any],
    actions: Sequence[dict[str, Any]],
    player: str,
    *,
    drop: int = 0,
) -> list[dict[str, str]]:
    """CR 509.1a's declaration, as this policy makes it: `[{blocker,
    blocked}]`, possibly empty.

    Only to prevent lethal, and only as much of it as lethal demands. The
    incoming damage is the summed power of everything attacking this seat;
    blockers go on the biggest attackers first, and the moment what is still
    getting through drops below this seat's life total the rest stay home -
    a chump block that changes no outcome is a creature spent for nothing.

    `drop` narrows a refused declaration exactly as it does for attacking -
    monotonically non-increasing, pinned at `[]` once it passes the number of
    blocks chosen, and with the same caveat: a blocking requirement
    (CR 509.1a) can refuse the empty answer, and no amount of narrowing
    answers a requirement. See `attack_declaration`.
    """
    enumerated: dict[str, list[str]] = {}
    for action in actions:
        if action.get("kind") == "declare_blocker":
            enumerated.setdefault(str(action.get("attacker_id")), []).append(
                str(action.get("card_id"))
            )

    incoming: list[tuple[int, str]] = []
    for attack in (state.get("combat") or {}).get("attacks") or []:
        if attack.get("defender") != player or attack.get("blockers"):
            continue
        attacker = _card_view(state, str(attack.get("attacker")))
        if attacker is None:
            continue
        incoming.append((_int(attacker.get("power")) or 0, str(attack.get("attacker"))))
    damage = sum(power for power, _ in incoming)
    life = _life_of(state, player)
    if not incoming or damage < life:
        return []

    available = sorted(str(card.get("id")) for card in block_candidates(state, player))
    blocks: list[dict[str, str]] = []
    used: set[str] = set()
    for power, attacker_id in sorted(incoming, key=lambda pair: (-pair[0], pair[1])):
        if damage < life:
            break
        pool = [
            blocker_id
            for blocker_id in (enumerated.get(attacker_id, available) if enumerated else available)
            if blocker_id not in used
        ]
        if not pool:
            continue
        # The sturdiest legal-looking blocker, so the block is as likely as
        # this policy can make it to cost nothing; ties broken by id so two
        # runs of one seed block identically.
        best = max(
            pool,
            key=lambda blocker_id: (
                _remaining_toughness(_card_view(state, blocker_id) or {}) or 0,
                blocker_id,
            ),
        )
        blocks.append({"blocker": best, "blocked": attacker_id})
        used.add(best)
        damage -= power
    return blocks[: max(len(blocks) - drop, 0)]


# -- answering questions the engine's own way --------------------------------

#: Every `DecisionKind` value `decide_body` answers, mapped to the branch that
#: answers it. `answers_kind` reads this; the combat declarations are absent
#: on purpose - they are judgements, not defaults, and go through
#: `attack_declaration`/`block_declaration` instead.
_DEFAULTED_KINDS = frozenset(
    {
        "may",
        "pay_life",
        "pay_energy",
        "pay_mana",
        "mulligan",
        "opening_hand_action",
        "pay_energy_amount",
        "pay_life_amount",
        "put_on_top_or_bottom",
        "order_triggers",
        "choose_cards",
        "choose_targets",
        "choose_a_quality",
        "choose_a_mode",
        "scry",
        "surveil",
    }
)


def answers_kind(kind: str) -> bool:
    """Whether `decide_body` has a branch for this decision kind. A kind it
    does not answer is a kind the baseline would stall on, so this is asserted
    against the api's own `ANSWER_FIELD_FOR_KIND` rather than left to be
    discovered in a stuck game."""
    return kind in _DEFAULTED_KINDS


def decide_body(pending: dict[str, Any], player: str) -> dict[str, Any]:
    """`POST .../decide`'s body for the standing question, following
    `machines/decisions.default_answer` branch for branch.

    `default_answer` is the engine's own answer to "nobody said what they
    want", and it is documented never to return an illegal option - which is
    exactly the property a control needs: the baseline's answers are the
    engine's, so a game it plays differs from a scripted one only where this
    module actually has an opinion (the land, the cast, the two combat
    declarations).

    One branch cannot be the engine's, and it is called out rather than
    quietly fudged: `CHOOSE_A_QUALITY` with no enumerated options ("choose a
    card name", "choose a creature type" - a universe too large to list)
    defaults to `""` in the engine, and `decisions.quality_answer` refuses an
    empty `choice` on the wire. The asking card's own name goes in instead: a
    real, non-empty, deterministic answer that the engine accepts, rather
    than a question left standing forever.
    """
    kind = str(pending.get("kind"))
    body: dict[str, Any] = {"player": player, "reasoning": REASONS["decide"]}
    generation = pending.get("generation")
    if generation is not None:
        # Which question this answers (`DecideRequest.generation`): a
        # resolution that asks two questions replaces the standing one the
        # instant the first is accepted, and an answer that does not say
        # which question it is for can land on the wrong one.
        body["generation"] = generation

    cards = [str(card) for card in pending.get("cards") or ()]
    options = [str(option) for option in pending.get("options") or ()]

    if kind in ("mulligan", "opening_hand_action"):
        # CR 103.5/CR 103.6 are both "may", and both are decisions a scenario
        # states rather than ones an engine takes on a player's behalf.
        body["accept"] = False
    elif kind in ("may", "pay_life", "pay_energy", "pay_mana"):
        body["accept"] = bool(pending.get("affirmative_is_legal", True))
    elif kind == "pay_energy_amount":
        body["energy_amount"] = 0
    elif kind == "pay_life_amount":
        body["life_amount"] = 0
    elif kind == "put_on_top_or_bottom":
        body["top"] = True
    elif kind == "order_triggers":
        # CR 603.3b's "in any order they choose" with nobody choosing: the
        # order they triggered in.
        body["order"] = cards
    elif kind in ("scry", "surveil"):
        # CR 701.22a/CR 701.25a: keep everything on top, in the order it was
        # looked at - the least-committal answer, and the one that keeps a
        # rigged library rigged.
        body["moved"] = []
        body["top_order"] = None
    elif kind == "choose_cards":
        body["card_ids"] = _choose_cards_answer(pending, cards)
    elif kind == "choose_targets":
        targets = list(pending.get("targets") or ())
        minimum = int(pending.get("target_minimum") or 0)
        body["targets"] = [str(ref.get("id")) for ref in targets[:minimum]]
    elif kind == "choose_a_quality" or kind == "choose_a_mode":
        body["choice"] = options[0] if options else str(pending.get("source") or "baseline")
    else:  # pragma: no cover - `test_every_answerable_decision_kind_has_a_default_body`
        raise ValueError(f"the baseline has no default answer for a {kind!r} decision")
    return body


def _choose_cards_answer(pending: dict[str, Any], cards: list[str]) -> list[str]:
    default_cards = pending.get("default_cards")
    if default_cards is not None:
        # The asker wrote down what an unscripted player takes - CR 701.23b's
        # declinable search stamps one, because finding nothing would
        # sacrifice a fetchland for no land at all.
        return [str(card) for card in default_cards]
    if not pending.get("exact", True):
        # CR 107.1c's "any number": none at all is the least-destructive
        # answer, and the one that does not empty a hand to Valakut Awakening.
        return []
    amount = int(pending.get("amount") or 0)
    if amount <= 0:
        return []
    return cards[len(cards) - amount :]


# -- the policy --------------------------------------------------------------


def has_turn(state: dict[str, Any], player: str) -> bool:
    """Whether it is this seat's move at all - the union of every branch in
    `choose_move` that produces one.

    One rule, one place, and it is load-bearing rather than tidy: `step` uses
    it to decide whether to fetch `GET .../legal-actions` before choosing.
    While the opponent takes a whole turn, a seat without this pays for an
    enumeration over its own hand every round to be told, again, that it is
    not its move - and a ten-game gauntlet is one python process serving
    every one of those reads.
    """
    if state.get("winner") or state.get("drawn"):
        return False
    pregame = state.get("pregame")
    if isinstance(pregame, dict):
        return player in (pregame.get("awaiting_mulligan") or []) or (
            pregame.get("opening_hand_actor") == player
        )
    pending = state.get("pending_decision")
    if isinstance(pending, dict):
        return pending.get("player") == player
    if state.get("priority_holder") == player:
        return True
    return bool(
        state.get("step") in ("untap", "cleanup")
        and state.get("active_player") == player
        and not state.get("stack")
    )


def choose_move(
    state: dict[str, Any],
    actions: Sequence[dict[str, Any]],
    player: str,
    *,
    refused: Collection[str] = (),
    narrowing: int = 0,
) -> Move | None:
    """What this seat does next, or `None` when it is genuinely not its move.

    `state` is this seat's own filtered view, `actions` its own
    `GET .../legal-actions` enumeration. `refused` is every `action_key` this
    seat has already had refused against this same board - a refusal is
    remembered rather than retried, which is what keeps one bad row from
    becoming a loop. `narrowing` is how many pairs a combat declaration drops
    after the engine has refused one (see `attack_declaration`): a
    declaration is refused as a whole, so there is no single row to blame.

    See the module docstring for the branch order and why each branch is
    where it is.
    """
    refused = set(refused)
    if state.get("winner") or state.get("drawn"):
        return None

    pregame = state.get("pregame")
    if isinstance(pregame, dict):
        if player in (pregame.get("awaiting_mulligan") or []):
            return Move(
                "mulligan_keep",
                "pregame/mulligan",
                {"player": player, "take": False, "reasoning": REASONS["mulligan_keep"]},
            )
        if pregame.get("opening_hand_actor") == player:
            return Move(
                "opening_hand_done",
                "pregame/opening-hand/finish",
                {"player": player, "reasoning": REASONS["opening_hand_done"]},
            )
        # Declared already and waiting on the other seat: CR 103.5's round
        # does not resolve until every player has spoken, so there is nothing
        # here to act on.
        return None

    pending = state.get("pending_decision")
    if isinstance(pending, dict):
        if pending.get("player") != player:
            # A question standing for the other seat holds the game's one
            # pause slot (`commands.pause_is_live`), so every mutating call
            # this seat could make would be refused as another command's
            # business. There is nothing to do but let them answer.
            return None
        kind = str(pending.get("kind"))
        if kind == "declare_attackers":
            return Move(
                "attack",
                "actions/attack",
                {
                    "attackers": attack_declaration(state, actions, player, drop=narrowing),
                    "reasoning": REASONS["attack"],
                },
            )
        if kind == "declare_blockers":
            return Move(
                "block",
                "actions/declare-blockers",
                {
                    "player": player,
                    "blocks": block_declaration(state, actions, player, drop=narrowing),
                    "reasoning": REASONS["block"],
                },
            )
        return Move("decide", "decide", decide_body(pending, player))

    if state.get("priority_holder") == player:
        return _priority_move(state, actions, player, refused)

    if (
        state.get("step") in ("untap", "cleanup")
        and state.get("active_player") == player
        and not state.get("stack")
    ):
        return Move("advance", "advance", {"player": player, "reasoning": REASONS["advance"]})

    return None


def _priority_move(
    state: dict[str, Any],
    actions: Sequence[dict[str, Any]],
    player: str,
    refused: set[str],
) -> Move:
    def usable(kinds: tuple[str, ...]) -> list[dict[str, Any]]:
        return [
            action
            for action in actions
            if action.get("kind") in kinds and action_key(action) not in refused
        ]

    if state.get("stack"):
        # The engine refuses `advance` outright while the stack is up
        # (CR 500.2, CR 117.4); passing is the move that makes the top
        # resolve, and this policy holds nothing back to respond with.
        return _pass(player)

    lands = usable(("play_land",))
    if lands:
        land = lands[0]
        return Move(
            "play_land",
            "actions/play-land",
            {
                "player": player,
                "card_id": str(land.get("card_id")),
                "reasoning": REASONS["play_land"],
            },
            key=action_key(land),
        )

    casts = [action for action in usable(("cast",)) if _affordable(action) and _plain_cast(action)]
    permanents = [
        action for action in casts if _is_permanent(_card_view(state, str(action.get("card_id"))))
    ]
    spells = [action for action in casts if action not in permanents]
    for pool, label in ((permanents, "cast_permanent"), (spells, "cast_spell")):
        if not pool:
            continue
        # Most expensive first; ties broken by the card id so two runs of one
        # seed cast the same card in the same order.
        best = max(pool, key=lambda action: (mana_value(action), str(action.get("card_id"))))
        return Move(
            label,
            "actions/cast",
            {
                "player": player,
                "card_id": str(best.get("card_id")),
                "mana_sources": untapped_mana_sources(state, player),
                "targets": None,
                "reasoning": REASONS[label],
            },
            key=action_key(best),
        )

    return _pass(player)


def _pass(player: str) -> Move:
    return Move(
        "pass_priority",
        "actions/pass-priority",
        {"player": player, "reasoning": REASONS["pass_priority"]},
    )


# -- driving one seat --------------------------------------------------------


@dataclass
class BaselineSeat:
    """One seat played by `choose_move`, over a `SeatApiClient`.

    Holds three pieces of state, and none of it is game state: the
    enumerated actions the api has already refused against the board as it
    stands right now, every whole answer it has refused against that same
    board (`answer_fingerprint` - the moves that have no enumerated row to
    be remembered by), and how far a refused combat declaration has been
    narrowed. The board moving (`seatkit.board_signature`) forgets all three,
    because a refusal is a fact about a board rather than about an action.
    """

    client: SeatApiClient
    game_id: str
    player: str
    uid: str
    refused: set[str] = field(default_factory=set)
    refused_answers: set[str] = field(default_factory=set)
    narrowing: int = 0
    signature: Any = None
    moves: int = 0
    refusals: int = 0
    advanced: bool = False
    """Whether the most recent `step` got anywhere: a move the api accepted,
    or a refusal that was new against this board and so narrowed what is
    left to try. `play_out`'s stall guard counts a round idle when no seat
    advanced, which is what makes it able to fire at all - reading `step`'s
    return value instead counted a refused move as a round of play, and a
    seat that keeps attempting refused moves never let the guard fire."""

    def step(self, state: dict[str, Any] | None = None) -> Move | None:
        """Read, choose, act - once. Returns the move made, or `None` when
        this seat had nothing to do.

        `state` is this seat's board when the caller already has it in hand -
        `play_out` reads it to check whether the game is over, and reading it
        a second time here doubled every round's cost for nothing.
        """
        self.advanced = False
        if state is None:
            state = self.client.game_state(self.game_id, self.uid)
        current = board_signature(state)
        if current != self.signature:
            self.signature = current
            self.refused.clear()
            self.refused_answers.clear()
            self.narrowing = 0
        if not has_turn(state, self.player):
            return None
        actions = self.client.legal_actions(self.game_id, self.player, self.uid, candidates=True)
        move = self._pick(state, actions)
        if move is None:
            return None
        outcome = self.client.act(self.game_id, move.path, move.body, self.uid)
        self.moves += 1
        if outcome.get("allowed") is False:
            self.refusals += 1
            fingerprint = answer_fingerprint(move)
            # New against this board, so this refusal narrowed what is left
            # to try and the round was not idle. A refusal of something
            # already refused is the loop, and `_pick` is what stops it
            # happening at all - this is the belt to that brace.
            self.advanced = fingerprint not in self.refused_answers
            self.refused_answers.add(fingerprint)
            if move.label in ("attack", "block"):
                # A declaration is refused as a whole (CR 508.1a/509.1a are
                # checked over the whole answer by `Game.declare_attackers`/
                # `declare_blockers`), so there is no row to blame - the
                # answer shrinks by one pair instead, down to the empty
                # declaration, which is where the sequence pins.
                self.narrowing += 1
            elif move.key:
                self.refused.add(move.key)
            # A move with no enumerated row behind it (a pregame
            # declaration, a combat declaration, a pass) is remembered by
            # `answer_fingerprint` above and nothing else. Dropping
            # `self.signature` is what this used to do instead, and it made
            # the loop worse rather than better: the next read re-derived
            # from scratch AND threw away this board's refusal memory, so it
            # produced the identical move again.
        else:
            self.advanced = True
        return move

    def concede(self) -> dict[str, Any]:
        """CR 104.3a: "A player can concede the game at any time." The
        backstop `play_out` reaches for, never part of the policy itself."""
        return self.client.act(
            self.game_id,
            "actions/concede",
            {"player": self.player, "reasoning": REASONS["concede"]},
            self.uid,
        )

    def _pick(self, state: dict[str, Any], actions: Sequence[dict[str, Any]]) -> Move | None:
        """The policy's move, with its targets named - re-asking when the
        first choice turns out to be one this seat cannot legally announce.

        A cast with no legal target is still a *legal action* (CR 118.6 is
        about cost, and `legal_actions` does not consult the board for a
        target either), so the enumeration offers Galvanic Discharge against
        an empty board and the engine then refuses the announcement at
        CR 601.2c. Declining in-process rather than attempting it is what
        keeps that out of the ledger: a live gauntlet recorded 547 identical
        "too few targets" rows in one game, because the opponent's own move
        changed the board every round and cleared this seat's memory of the
        last refusal. `refused` is the same set a real refusal feeds, so one
        pass through this loop cannot revisit a card it has already declined.

        It also never hands back an answer this board has already had
        refused (`answer_fingerprint`). For a move with an enumerated row
        behind it that means stepping to the next row; for one without - a
        pass, a decision, a combat declaration already narrowed to the empty
        answer - there is nothing to step to, and this seat says it has
        nothing to do rather than post the identical body again.
        """
        for _ in range(len(actions) + 1):
            move = choose_move(
                state, actions, self.player, refused=self.refused, narrowing=self.narrowing
            )
            if move is None:
                return None
            filled = self._fill_targets(state, actions, move)
            if filled is None:
                self.refused.add(move.key)
                continue
            if answer_fingerprint(filled) not in self.refused_answers:
                return filled
            # This exact answer has already been refused against this board,
            # so posting it again could only be refused again. A move with an
            # enumerated row behind it steps to the next row; one without -
            # a pass, a decision, a combat declaration narrowed to the empty
            # answer - has nothing to step to, and this seat says so rather
            # than spin. `play_out`'s stall guard is what ends a game that
            # reaches here (see this module's "Why it cannot stall").
            if not filled.key:
                return None
            self.refused.add(filled.key)
        return None  # pragma: no cover - the loop is bounded by the pool it draws from

    def _fill_targets(
        self, state: dict[str, Any], actions: Sequence[dict[str, Any]], move: Move
    ) -> Move | None:
        """Name targets for a cast that requires them (CR 601.2c), or `None`
        when the board cannot fill every requirement this announcement makes.

        **No second question of the api.** `step` asks for its enumeration
        with `candidates=True`, so each targeting row arrived carrying its own
        legal candidates (`app._attach_target_candidates`) alongside the
        requirement *shape* `target_slots` reports - one answer, chosen from
        and acted on. A row with `target_slots` and no `targets` block is a
        row this seat declines: either the engine could not read the targeting
        phrase (`understood: false`, which carries no slots and says why) or
        the api did not enrich it at all, which it only skips for an
        unaffordable cast - a row this policy was never going to choose.
        Declining in-process is exactly what keeps a refusal out of the
        ledger.

        It takes `minimum` targets per requirement, which is
        `default_answer`'s own CR 601.2c answer, preferring the opponent: a
        control that picked its targets cleverly would stop being a control.
        """
        if move.label not in ("cast_permanent", "cast_spell"):
            return move
        action = next(
            (candidate for candidate in actions if action_key(candidate) == move.key), None
        )
        slots = list((action or {}).get("target_slots") or ())
        wanted = sum(int(slot.get("minimum") or 0) for slot in slots)
        if not wanted:
            return move
        offered = (action or {}).get("targets") or {}
        chosen = choose_targets(state, slots, offered.get("slots") or [], self.player)
        if len(chosen) < wanted:
            return None
        return Move(move.label, move.path, {**move.body, "targets": chosen}, key=move.key)


def candidate_of(line: str) -> dict[str, str]:
    """One `affordances.candidate_line` string, back as the three facts a
    choice is made on: `id`, `kind`, `controller`.

    The wire carries a candidate as a string rather than an object for a
    measured reason (`affordances.candidate_line`: the same fourteen
    candidates cost 2,384 bytes as dicts and 972 as strings), so reading one
    is this end's half of that trade. Two shapes, both from that function:
    `"<username> (player)"`, and `"<uuid> <name> (<zone>, <controller>)"`.

    Split from the right, because the *name* is the part that can carry
    brackets of its own - "Erase (Not the Urza's Legacy One)" is a real
    printing - while the trailing `(...)` this function reads is appended by
    `candidate_line` itself and is always last.
    """
    head, _, tail = line.rpartition(" (")
    parenthetical = tail[:-1] if tail.endswith(")") else tail
    if not head:
        # Nothing this end recognises - no trailing bracket at all. Kept as
        # an id rather than dropped: the engine still judges it at CR 601.2c,
        # and inventing a refusal here would be this seat guessing at a rule.
        return {"id": line, "kind": "card", "controller": ""}
    if parenthetical == "player":
        return {"id": head, "kind": "player", "controller": head}
    _zone, _, controller = parenthetical.rpartition(", ")
    card_id, _, _name = head.partition(" ")
    return {"id": card_id, "kind": "card", "controller": controller}


def choose_targets(
    state: dict[str, Any],
    slots: Sequence[dict[str, Any]],
    offered: Sequence[dict[str, Any]],
    player: str,
) -> list[str]:
    """The least-committal legal announcement: `minimum` targets per
    requirement, each the opponent where the opponent is legal for that slot,
    and never the same object twice.

    `offered` is the enumeration's own per-slot candidate list -
    `schemas.ActionTargets.slots`, as `GET .../legal-actions?candidates=true`
    folded it into this very action's row. `TargetRequirement.rejects` is
    what filled it, the same predicate `TargetingProfile._fill_slots` runs at
    CR 601.2c, so a choice made from here can never disagree with the
    announcement that follows. Each entry's `slot` is its index into `slots`
    (`Action.target_slots`), which is what pairs a candidate list with the
    `minimum` it has to satisfy.
    """
    opponents = {
        str(entry.get("username"))
        for entry in state.get("players") or ()
        if entry.get("username") != player
    }
    by_slot = {int(entry.get("slot") or 0): entry for entry in offered}
    chosen: list[str] = []
    taken: set[str] = set()
    for index, slot in enumerate(slots):
        wanted = int(slot.get("minimum") or 0)
        lines = by_slot.get(index, {}).get("candidates") or ()
        pool = [candidate_of(str(line)) for line in lines]
        pool = [candidate for candidate in pool if candidate["id"] not in taken]
        # The opponent first (a face, then anything they control), then
        # whatever is left, in the order the engine offered it.
        pool.sort(
            key=lambda candidate: (
                0
                if candidate["kind"] == "player" and candidate["id"] in opponents
                else 1
                if candidate["controller"] in opponents
                else 2
            )
        )
        for candidate in pool[:wanted]:
            chosen.append(candidate["id"])
            taken.add(candidate["id"])
    return chosen


def play_out(
    seats: Sequence[BaselineSeat],
    *,
    max_steps: int = 2000,
    poll: float = 0.0,
    stall_rounds: int = STALL_ROUNDS,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Drive `seats` round-robin until the game ends, and guarantee that it
    does.

    Returns what happened: `over`, `winner`, `drawn`, `steps`, and
    `ended_by` - `"play"` when the engine ended the game itself,
    `"stall_guard"` when a seat had to concede because no seat got anywhere
    for `stall_rounds` rounds, `"stopped"` when `should_stop` said to,
    `"stall_guard_refused"` when the guard's own concession was refused (see
    below), `"bound"` when none of those happened inside `max_steps` (which
    is a defect in this module, and is reported as one rather than hidden).

    The guard is deliberately a concession rather than a silent stop: a game
    that ends is a game the scorecard can count, and a game the baseline could
    not play is a loss it should be seen to take.

    **A round is idle when no seat advanced**, which is not the same as no
    seat having attempted anything: a move the api refused is only progress
    the first time, because it narrows what that seat has left to try against
    this board (`BaselineSeat.advanced`). Counting an attempt as progress is
    what kept this guard from ever firing.

    Three knobs, all for the mixed game the gauntlet actually runs - one
    baseline against an agent seat nothing here drives:

    - `poll` sleeps between rounds, so a baseline waiting on an agent's turn
      does not spend that turn hammering the api with reads.
    - `stall_rounds` raises the guard's patience. An agent seat thinking for
      a minute is not a stalled game, and a guard tuned for two baselines
      would concede out from under it.
    - `should_stop` hands the decision "this game is finished with" to
      whoever actually owns it - `scripts/watch_game.py`, whose edges
      (`report`, `game_over`, `stalled`, `timeout`) are the harness's own
      definition of done. The loop having a second opinion is how two
      definitions of "finished" drift apart.
    """
    if not seats:
        raise ValueError("play_out needs at least one seat")
    steps = 0
    idle_rounds = 0
    conceded = False
    while steps < max_steps:
        acted = False
        for seat in seats:
            # Asked before every seat rather than once a round: this loop
            # runs in a thread `scripts/eval_gauntlet.py` joins as soon as
            # the watcher's edge fires, and a round of two seats each taking
            # up to the seam's own 30s http timeout is how long that join
            # waited. See `eval_gauntlet.play_one`.
            if should_stop is not None and should_stop():
                return _outcome(_read(seats), steps, "stopped")
            state = seat.client.game_state(seat.game_id, seat.uid)
            if state.get("winner") or state.get("drawn"):
                return _outcome(state, steps, "stall_guard" if conceded else "play")
            seat.step(state)
            # `seat.advanced`, never `step`'s return value: `step` hands back
            # the move it made whether the api ACCEPTED or REFUSED it, so
            # reading that counted a refusal as a round of play and reset
            # `idle_rounds` for as long as any seat kept attempting anything.
            # The guard could not fire, and the run spun to `max_steps`.
            if seat.advanced:
                acted = True
            steps += 1
            if steps >= max_steps:
                break
        idle_rounds = 0 if acted else idle_rounds + 1
        if idle_rounds >= stall_rounds and not conceded:
            state = _read(seats)
            active = state.get("active_player")
            quitter = next((seat for seat in seats if seat.player == active), seats[0])
            outcome = quitter.concede()
            conceded = True
            if outcome.get("allowed") is False:
                # The one thing this guard rests on, checked rather than
                # assumed. CR 104.3a makes a concession legal at any time and
                # `app._run_command` exempts it from the pause-slot guard for
                # exactly that reason, so this is not expected - and a guard
                # that fired, was refused, and then span out the remaining
                # `max_steps` reporting `bound` would hide the one failure
                # that matters here.
                return _outcome(_read(seats), steps, "stall_guard_refused")
            idle_rounds = 0
        elif not acted and poll:
            time.sleep(poll)
    state = _read(seats)
    if state.get("winner") or state.get("drawn"):
        return _outcome(state, steps, "stall_guard" if conceded else "play")
    return _outcome(state, steps, "bound")


def _read(seats: Sequence[BaselineSeat]) -> dict[str, Any]:
    return seats[0].client.game_state(seats[0].game_id, seats[0].uid)


def _outcome(state: dict[str, Any], steps: int, ended_by: str) -> dict[str, Any]:
    return {
        "over": bool(state.get("winner") or state.get("drawn")),
        "winner": state.get("winner"),
        "drawn": bool(state.get("drawn")),
        "turn": state.get("turn_number"),
        "steps": steps,
        "ended_by": ended_by,
    }
