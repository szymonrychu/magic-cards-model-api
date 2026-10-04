"""DR1/FD4: converting one seat's own ledger rows into the play-script DSL.

`decision_rows(entries, seat)` turns every *committing* ledger row of `seat`
- a cast, an activation, a land play, a keyword-action route (suspend, plot,
foretell, cycle, equip, crew, station, turn_face_up, companion, unlock_door), an
answer to a pending decision, the two combat declarations (whether logged
by their own dedicated route or answered directly through `/decide`'s own
`pairs`), a
mulligan declaration or CR 103.6 opening-hand action, a priority pass
(`pass`/`pass_until` both post the same `pass_priority` action - see below),
or a script run it started - into one record:

    {seq, turn, step, action, dsl, reasoning, board, unexpressible}

`dsl` is that decision written as a schema-valid `Play` (`schema.play_errors`
sees no error in it) whenever the play-script vocabulary can say it at all;
`unexpressible` names why not when it cannot, and `dsl` is `None` then -
never a fabricated move standing in for one the DSL cannot make. The two
fields are always both present and always exactly one of them is `None`,
except `run_script` (see its own section below), which is a fixed shape of
its own rather than a `Play`. FD4 closes every converter gap this module used
to leave open: `concede` is the only decision that stays unexpressible on
purpose (CR 104.3a lets a player concede at any time, and a script must never
end the game). Every other label this module writes now carries its own
reason as a `:`-joined suffix, so `unexpressible` alone always says why:
`:unresolved` (a card, target or crew member genuinely could not be found in
the board this row carries), `:malformed` (the row's own data is missing or
the wrong shape to answer from at all - reachable only from a hand-built or
corrupted row, never a real route's own logging), `:too-many` (the row names
more objects than the schema's own bound for that field allows),
`:unrepresentable-step` (the row's own step/active-player is not one `when`
can name at all - a pregame or pass-through step, say), and `:unsupported`
(a `DecisionKind` this module has no branch for whatsoever).

**Row shape.** One entry is `audit.entries()`'s own shape: `seq`, `action`,
`detail` (the route's own per-action fields, keyed by `player`), and
`turn`/`step`/`active_player`/`reasoning` **stamped by `audit.record` itself,
outside `detail`** - T10 gives every row these four at the position it was
recorded, regardless of what the calling route passed as keyword arguments,
so this module reads them there rather than from `detail` (where an older
reader, or a test fixture built by hand, might also have put a copy - a
`detail["reasoning"]`/`detail["turn"]` is read as a fallback when the
top-level key is absent, never preferred over it). A row not shaped like a
recognized committing action, or not this seat's own **allowed** action
(`detail["allowed"] is True`, `records.require_reasoning`'s own commitment
test - a refusal committed nothing and is skipped, same as an offer, a
reflection, or any other non-action row), contributes no record at all. A
`paused: true` row is still committing exactly like any other once its own
`allowed` reads `True` - a command that has committed everything up to its
own pause (FD5) converts the same way a command that ran straight through
does; nothing here treats `paused` specially on its own.

**FD4 (b): the pre-action frame.** `audit.record` stamps each row's own
`state_view` (via DR2's `frame_views`) with the board *as it stood once this
row's own action had already happened* - the post-action frame. A card this
module looks up for the row's *own* move (what is being cast, activated, or
turned face up) must instead be searched for in the board as it stood right
*before* this action ran, or a cast reads back as being played from the
stack it just moved onto, and an off-zone activation (channel from hand, a
graveyard ability) reads its own source as already having left the zone its
cost just moved it out of.

**FD4-r1/G1: FD5's own pointer, preferred when it is there.** FD5 stamps two
fields for exactly this: `pre_action_frame`, the exact `seq` of the row this
action was taken against, and `pre_state`, this row's own best-effort
snapshot of the pre-action board, attached only when there is no earlier
frame at all (a game's very first row) - exactly one of the two is ever set.
`decision_rows` keeps every row's own `state_view`/`active_player` in
`by_seq`, keyed by `seq`, so a `pre_action_frame` is resolved by exact lookup
rather than re-derived. This is exact where the fallback below is only an
approximation, and it is checked first.

**The carried-forward fallback**, read when neither FD5 field is on the row
(an older row, or a game recorded before FD5 landed): ledger rows arrive in
the game's own chronological order, one action resolves fully (audited)
before the next one is even attempted (CR 116/117's own turn-based-action
discipline, and the store's own single paused-command slot - see the fold,
below), so the nearest *earlier* row's own frame **is**, in the ordinary
case, the exact board this row's action began against. `decision_rows` walks
`entries` tracking exactly that: the last state view any prior row carried,
used for every card lookup this row's own conversion makes - never the row's
own post-action frame.

This carried-forward frame is still only an approximation of a genuine
pre-action attachment: it is exactly right whenever every intervening row up
to this one also carries its own frame, but an event with no ledger row of
its own (a draw, say) between the nearest earlier frame and this row is
invisible to it - exactly the gap FD5's exact `pre_action_frame` pointer
closes once a row carries one. What this module can do without it is treat
the carried-forward frame as a first attempt rather than the only one: a
card missing from it for this row's own move is looked up a second time in
the row's own post-action frame purely to identify it
(`_cast_or_play_land`/`_activate`/`_keyword_action`), closing the practical
gap that carrying only the pre-action frame forward left open.

**`active_player` is not read the same way.** Unlike a card's zone,
`active_player` is already correct on every row's *own* field except one
kind: CR 117.4 lets a `pass` end the very step (and sometimes the turn) it
was made in, so a `pass` row's own `active_player` can read as the turn it
*produced*, never the one it was actually taken during - every other kind
(a cast, an activate, a decide, a keyword action, a declaration) never
changes whose turn it is, so its own value is already exactly right and is
read directly, not overridden by a carried-forward value from a row that
might belong to the other player entirely (FD4-r1/G16: seat B's own first
action of B's brand new turn used to be misread as "theirs", because the
carried-forward approximation used to apply to every kind uniformly, not
only to `pass`). Whenever FD5 stamps either `pre_action_frame` or
`pre_state` on a row at all, `audit.record` has already re-read that row's
own `active_player` off the pre-action board (`records.action_board`, the
same note that decides which of the two pointers gets attached) - so this
row's own field is exact then too, *pass included*, and neither pointer's
*referenced* row is ever consulted for it (FD4-r1: reading the referenced
row's own value there instead reproduced the identical "theirs" misread one
step later, once a row happened to carry a pointer).

**Board.** `compact_board` (below) turns one `GameStateView`-shaped dict -
what `GET /review/frames/{seq}` hands back for a row that has a recorded
state - into the compact board this module's own record carries for a
reviewer's own reading; unlike a move's own selectors, this is not something
in the vocabulary that must be pre-action, so it is built from the row's own
(post-action) frame, showing what the action actually did. This module never
fetches that view itself (no store access, no routes: DR2 wires it) - a row
that wants one attaches it under its own `state_view` key before calling
`decision_rows`, and a row without one degrades to the least specific
*honest* selector rather than guessing: an empty `{}` (matches "whichever
card", not a name) for `cast`/`play_land`, and `unresolved` for `activate`
and every keyword-action route, whose card selector is required and has no
honest empty value - see `_cast_or_play_land`/`_activate`/`_keyword_action`.

**Not in the vocabulary.** `concede` (`app.action_concede`'s own ledger
action, `"concede"` - CR 104.3a lets a player concede at any time, and the
vocabulary has no move for it, on purpose) is the one decision this module
reaches that stays unexpressible regardless of how much board data a row
carries.

`attack` (`declare_attackers`'s own ledger action, `"attack"`), `block`
(`declare_blockers`) and `pass`/`pass_until` (`pass_priority` - the same
ledger action for both; `pass_until` only *threads one reason across several
of them*, see `seatkit.pass_until`, so there is nothing here for it to
convert differently) are real moves: `pass` needs no board data at all
(`{"kind": "pass"}`), an empty `attackers`/`blocks` list needs none either
(`declare_attackers`/`declare_blockers` with an empty list is itself a legal
"no attackers"/"no blocks" declaration), and a non-empty one is written with
card-name-free selectors the same way `cast`/`play_land`/`activate` already
are - grouped by shared defender (attackers) or shared blocked attacker
(blockers) into one `AttackGroup`/`Block` apiece. FD4 (g): the same two
declarations answered directly through `/decide` (its own `pairs` field,
`DecisionKind.DECLARE_ATTACKERS`/`DECLARE_BLOCKERS`) convert exactly the same
way, reshaped from `pairs`' `{creature, defender}` shape into the dedicated
routes' own `attackers`/`blocks` shape first (`_combat_pairs_detail`) -
there is no second, weaker mapping for the `/decide` path. Without a
`state_view` that resolves every card an entry names, or with more distinct
groups than the schema's own `MAX_ATTACK_GROUPS`/`MAX_BLOCKS`, the row falls
back to `"declare_attackers:unresolved"`/`"declare_blockers:unresolved"` -
never a half-built move standing in for one it could not finish honestly.

**Keyword-action routes (FD4 (d)).** `suspend`, `plot`, `foretell`, `cycle`,
`equip` and `crew` (six existing routes), `turn_face_up`/`companion` (FD5's
two), and `unlock_door` (CR 709.5e's Room-unlock cost) all become one
`keyword_action` move apiece (`_keyword_action`): `card` the same
card-name-free way `cast`/`play_land` are, `target` for `equip` (the
creature it attaches to), `crew_with` for `crew` (the creatures tapped to
pay its cost), and `door` for `unlock_door` (the locked half's own printed
name, carried straight through - never resolved against the board, since it
names no card) - the three Picks/selectors resolved against the pre-action
frame the same as everything else. A card (or, for `equip`/`crew`, a
target/crew member; for `unlock_door`, its own `door` string) that cannot be
resolved is `"<action>:unresolved"` -
`"suspend:unresolved"`, `"equip:unresolved-target"`, and so on - never a
half-built move.

**Mulligan and opening-hand rows (FD4 (h)/G4).** `opening.py`'s own
`mulligan_declared` (CR 103.5's keep-or-take, with `bottom_chosen` when a
London mulligan bottoming was owed) and `opening_hand_action` (CR 103.6) rows
carry no `turn`/`step` at all - pregame has none yet - so their own `when`
is built directly from the schema-only `mulligan`/`pregame_actions` steps
FD2 added, rather than from the row's own (absent) position. A declaration
becomes the FD2 `mulligan` move (`keep`, the declaration's own inverse of
`take`; `bottom` is left out - the engine's own default, the back of the
hand, since the row names only *whether* a bottoming happened, not which
cards it chose); an opening-hand action becomes a plain `accept` answer to
the `opening_hand_action` decision kind, the same footing `may`/`pay_life`
already stand on - the row only ever says *that* the seat acted, and CR
103.6's own action is idempotent enough that naming *which* card is not
needed for a script to repeat the decision (the runtime, FD3, is what
chooses a live card to act on, the same way `cast`'s own selector does). The
same two decision kinds, `mulligan` and `opening_hand_action`, are also
reachable as a plain `decide` row (the engine's own pregame decide flow) -
`_pregame_decide_play` gives that identical treatment.

A `decide` row can also miss the vocabulary at a *finer* grain, one
`DecisionKind` at a time. `scry`/`surveil` (`to_bottom`/`to_graveyard` +
`top_order`), `put_on_top_or_bottom` (`position`), `choose_cards` look-and-
keep (`pick`, FD4 (h)/G14: an empty `card_ids` is a legal "choose none",
`{"select": {}, "count": 0}`, needing no board data), `choose_targets`
(`targets`, a card or a player - FD4 (h)/G14: an empty list is legal the
same way), `choose_players` (FD4 (h)/G14: an empty list is legal too),
`order_triggers` (FD4 (h)/G10: `order`, one selector tier per triggered
ability, in the row's own declared order) and `assign_combat_damage` (FD4
(h)/G11: `damage`, a `DamagePolicy` whose `tiers` replay the row's own
declared assignment order and whose `rest_to` is `"player"` exactly when the
row assigned anything to the defending player past its blockers) are all
built from board data the same way the combat moves are, and fall back to
`"decide:<kind>:unresolved"` when a card or target the row names cannot be
resolved in the attached (pre-action) board (`:malformed`/`:too-many` for
the row's own data being absent, mis-shaped or over a schema bound - see
each answer builder's own docstring). `choose_a_quality`/
`choose_a_mode` (O6) answer with `choice` (a literal string) unless the
kind is `choose_a_quality` and the chosen string itself resolves to a real
card in the board (FD4 (h)/G15 - Meddling Mage's own "name a card", as
opposed to Cavern of Souls' creature type, which never resolves to a card
and keeps the plain `choice`), in which case `choice_from` - a selector,
never the name itself - is written instead.

**Every decide row names its own pending decision (FD4 (f)).** A standalone
`answer` play's `true` always carries one `pending_decision` condition naming
the row's own `DecisionKind` (`schema.PendingDecisionCondition`,
`Play._standalone_pending_answer_names_the_pending_decision`), so the play
only ever runs as a direct answer to a decision already on the table, not on
`when` timing that happened to match this one historical row - unconditional
now, not only when the seat lacked priority (an earlier, narrower reading
this module used to give the same fact).

**Folding a decide into its own move (FD4 (f)/G2).** The store holds exactly
one paused command at a time (`store.paused_command`), so a `decide` row
that follows a `cast`/`play_land`/`activate`/keyword-action row of this same
seat *that row's own `paused: true`* can only ever be resolving the object
that same move already named - there is nothing else it could be answering.
Rather than writing that answer as a second, orphaned `decide` play with no
home, this module folds it straight into the *paused* move's own `answers`
field (every one of `CastMove`/`PlayLandMove`/`ActivateMove`/
`KeywordActionMove` carries one) and gives the `decide` row's own record the
*same*, now-enriched play - one committing row still gets one record each
(the module's own long-standing contract), but the two plays are byte-equal,
so a reader that dedupes by generalized DSL (`report.py`) already treats them
as the one decision they always were. A resolution that pauses more than
once before it finally completes (choose targets, then choose a mode, say)
keeps folding into the same original move across every one of its own
`decide` rows, up to the schema's own `MAX_ANSWERS`; a `decide` this module
cannot resolve breaks the fold rather than silently absorbing a partial
answer, and any row that is not itself a `decide` (a fresh action, this
seat's or the opponent's) ends it.

**Whose turn (FD4 (i)/G16).** `_when`'s `whose_turn` reads the *pre-action*
`active_player` computed above - FD5's exact pointer when the row carries
one, or, for a `pass` (the only kind whose own action can change it), the
carried-forward value, never the row's own post-action value: a `pass` that
ends the turn would otherwise read back as having been made on the turn it
*produced*, not the one it was actually taken during. Every other kind reads
its own field directly - see "`active_player` is not read the same way"
above.

**Script-driven rows are not counted twice (FD4 (j)/G17).** A row this
seat's own script run already produced - `stats.script_driven`'s own test,
inside the run's own span or carrying its reasoning suffix - contributes no
record of its own; the run's own `run_script` row already stands for it.

**`run_script`.** Running an existing play is not itself one of the DSL's
move kinds; it invokes one that is already written. Its `dsl` is therefore
not a `Play` but a small fixed dict, `{"kind": "run_script", "play_id": ...,
"offer_outcome": ...}` - `offer_outcome` is `"taken"` when a
`script_run_offered` row for the same `run_id` appears earlier in `entries`,
the same "this offer was followed" reading `report._offer_outcomes` gives
it, and `"unoffered"` otherwise (the seat, an operator, or arming started it
without ever being offered it).
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..audit import SCRIPT_RUN_OFFERED_ACTION, SCRIPT_RUN_STARTED_ACTION
from .schema import (
    AMOUNT_DECISION_KINDS,
    CHOICE_DECISION_KINDS,
    COUNT_ZONES,
    KEYWORD_ACTIONS,
    MAX_ABILITY_INDEX,
    MAX_ANSWERS,
    MAX_ATTACK_GROUPS,
    MAX_BLOCKS,
    MAX_DAMAGE_TIERS,
    MAX_ORDER,
    MAX_SELECTOR_WORDS,
    MAX_TARGETS,
    MAX_TOP_ORDER,
    STEPS,
    TARGET_ZONES,
)
from .selectors import parse_type_line

#: A defender/target token that named a card, but one this module could not
#: resolve in the attached `state_view` - distinct from `None`, which for a
#: `Defender` token means "the opponent player, so the field is simply left
#: out" (see `_defender_for_token`).
_UNRESOLVED = object()

#: The upper bound `compact_board` stays at or under, in characters of its
#: `json.dumps(..., sort_keys=True)` form - a review prompt's own budget
#: line, not a byte count (contrast `report.TABLE_LIMIT_BYTES`).
COMPACT_BOARD_CHARS = 600

#: The ledger action recorded for `pass`/`pass_until` alike - see the module
#: docstring.
_PASS_ACTION = "pass_priority"

#: `unexpressible` labels for the two combat-declaration fallbacks and the
#: one row-level fallback below, plus `concede` - `_CONCEDE` is the spec's
#: own word; the rest are this module's, and, per review finding 2, every
#: one of them now carries its own reason as a suffix rather than a bare
#: word: `:unresolved` (a card/target the row names could not be found in
#: the board attached to it), `:malformed` (the row's own data is missing or
#: the wrong shape to answer from at all - never reachable from a real
#: route's own logging, only from a hand-built or corrupted row), and
#: `:too-many` (the row names more objects than the schema's own bound for
#: that field allows).
_ATTACK = "declare_attackers:unresolved"
_BLOCK = "declare_blockers:unresolved"
_MULLIGAN = "mulligan:malformed"
_CONCEDE = "concede"

#: The ledger action -> this module's own canonical action-kind label, for
#: every committing row `decision_rows` recognizes at all. A raw action not
#: in this table contributes no record. FD4 (d): the nine keyword-action
#: routes (six existing, plus FD5's `turn_face_up`/`companion` and CR
#: 709.5e's `unlock_door`) map to themselves - `KEYWORD_ACTIONS` is both the
#: schema's own closed vocabulary for `KeywordActionMove.action` and, read
#: here, the exact set of ledger action names this module treats as one.
_ACTION_KIND: dict[str, str] = {
    "cast": "cast",
    "activate": "activate",
    "play_land": "play_land",
    "decide": "decide",
    "attack": "declare_attackers",
    "declare_blockers": "declare_blockers",
    "mulligan_declared": "mulligan",
    "opening_hand_action": "opening_hand_action",
    "concede": "concede",
    _PASS_ACTION: "pass",
    SCRIPT_RUN_STARTED_ACTION: "run_script",
    **{action: action for action in KEYWORD_ACTIONS},
}

#: The move kinds a `decide` row can be folded into (FD4 (f)/G2) - every
#: move kind the schema gives its own `answers` field.
_FOLDABLE_MOVE_KINDS = frozenset({"cast", "play_land", "activate", *KEYWORD_ACTIONS})

#: `decide` kinds a real answer schema can write with no card selector at
#: all - see the module docstring's "not in the vocabulary" section.
_ACCEPT_KINDS = frozenset({"may", "pay_life", "pay_energy", "pay_mana"})
_AMOUNT_FIELD = {
    "pay_energy_amount": "energy_amount",
    "pay_life_amount": "life_amount",
    "choose_an_amount": "amount",
}

#: `decide` kinds answered directly by the two combat-declaration moves
#: (FD4 (g)) - `/decide`'s own `pairs` field for `DECLARE_ATTACKERS`/
#: `DECLARE_BLOCKERS`, alongside the dedicated `attack`/`declare_blockers`
#: routes those same two moves already serve.
_COMBAT_DECIDE_KINDS = ("declare_attackers", "declare_blockers")

#: `decide` kinds answered with a plain `accept` bool but that carry no
#: `turn`/`step` of their own - CR 103.5/103.6's own pregame decisions -
#: mapped to the schema-only step their `mulligan`/`opening_hand_action`
#: counterpart action rows use (FD4 (h)/G4).
_PREGAME_DECIDE_STEPS = {"mulligan": "mulligan", "opening_hand_action": "pregame_actions"}


def _payload(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    """`entry` itself, or its `payload` when it is a raw `records` row -
    the same duck-typed read `report._payload` uses, so a caller that hands
    either shape through is read the same way."""
    payload = entry.get("payload")
    return payload if isinstance(payload, Mapping) else entry


def _top(entry: Mapping[str, Any], detail: Mapping[str, Any], key: str) -> Any:
    """A field `audit.record` stamps at the top of the row, read there
    first and off `detail` only as a fallback - see the module docstring."""
    value = entry.get(key)
    return value if value is not None else detail.get(key)


def _is_script_driven(entry: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> bool:
    """FD4 (j)/G17: `stats.script_driven`, imported lazily - `stats` imports
    `report`, which imports this module, so a module-level import here would
    be circular."""
    from .stats import script_driven

    return script_driven(entry, rows)


def decision_rows(
    entries: Iterable[Mapping[str, Any]], seat: str, *, with_boards: bool = True
) -> list[dict[str, Any]]:
    """Every committing row of `seat` in `entries`, converted - see the
    module docstring for the record shape, the pre-action frame every card
    lookup and `whose_turn` judgement reads, and what `unexpressible` means.

    `with_boards=False` leaves every record's `board` `None` without ever
    calling `compact_board` - for a caller that reads only `action`/`dsl`/
    `unexpressible` (the ops coverage sweep, OM1); the conversion itself
    still reads every frame it would otherwise."""
    rows = list(entries)
    out: list[dict[str, Any]] = []
    pre_state_view: Mapping[str, Any] | None = None
    pre_active_player: Any = None
    #: Every row's own `state_view`/`active_player` seen so far, keyed by its
    #: own `seq` - FD4-r1/G16: FD5's `pre_action_frame` names a row's own
    #: `seq` directly, so a later row can point at the *exact* row its
    #: action was taken against instead of trusting "the nearest earlier
    #: row", which a gap (an intervening event with no ledger row of its
    #: own) could get wrong. Never popped: `pre_action_frame` only ever
    #: points backward, so this only ever grows.
    by_seq: dict[Any, dict[str, Any]] = {}
    #: The most recent record still open to a fold (FD4 (f)/G2) - the record
    #: dict itself, appended to `out` by reference, so mutating its own
    #: `dsl` in place is visible to every caller already holding `out`.
    fold_target: dict[str, Any] | None = None

    for index, entry in enumerate(rows):
        payload = _payload(entry)
        raw_action = payload.get("action")
        kind = _ACTION_KIND.get(str(raw_action))
        row_state_view = entry.get("state_view")
        row_state_view = row_state_view if isinstance(row_state_view, Mapping) else None
        row_active_player = payload.get("active_player")
        row_seq = payload.get("seq", entry.get("seq"))

        # The pre-action frame/active_player this row's own conversion
        # reads. FD5 (G1/G16) may stamp the row itself with one of two
        # explicit pointers, checked first because both are exact where the
        # fallback below is only an approximation: `pre_state` is this row's
        # own best-effort snapshot of the pre-action board, attached only
        # when there is no earlier frame at all (a game's very first row);
        # `pre_action_frame` is the exact `seq` of the row this action was
        # taken against, read back out of `by_seq` below - the row that
        # named it, never re-derived. `0`/`None` both read as "not named".
        #
        # FD4-r1 (review): whichever pointer is set, it names where the
        # *board* (`pre_state`) comes from - never where `active_player`
        # comes from. `audit.record` stamps exactly one of the two pointers
        # in the same breath it re-reads `active_player`/`priority_holder`
        # off the pre-action board (`records.action_board`), so a row that
        # carries either pointer already carries its own exact,
        # pre-action-correct `active_player` too - reading the *referenced*
        # row's value instead (as this branch used to) reads a different
        # row's turn, not this one's, and misconverts a cast or a pass alike
        # the moment the referenced row belongs to the other player. The
        # row's own field is only ever missing on a hand-built fixture that
        # sets a pointer without it, so both branches still fall back to the
        # pointer's own board for that one honest-best-effort case.
        explicit_pre_state = payload.get("pre_state")
        explicit_pre_state = explicit_pre_state if isinstance(explicit_pre_state, Mapping) else None
        explicit_pre_frame = payload.get("pre_action_frame")
        referenced = by_seq.get(explicit_pre_frame) if explicit_pre_frame else None

        if explicit_pre_state is not None:
            pre_state = explicit_pre_state
            pre_active = (
                row_active_player
                if row_active_player is not None
                else explicit_pre_state.get("active_player")
            )
        elif referenced is not None:
            pre_state = referenced.get("state_view")
            pre_active = (
                row_active_player
                if row_active_player is not None
                else referenced.get("active_player")
            )
        else:
            # No explicit FD5 pointer (yet) - the carried-forward
            # approximation: the nearest *earlier* row's own value
            # (captured before this row's own values fold into the running
            # trackers below, so a row already converted is never read
            # against a frame its own action produced). `state_view` is
            # always read this way regardless of kind - every committing
            # row's own frame already includes its own action's effect, so
            # it is never this row's own pre-action board, whatever the
            # kind. `active_player`, in contrast, is only ever wrong on the
            # row whose own action can change it: CR 117.4 lets a `pass`
            # end the very step (and so, sometimes, the turn) it was made
            # in, and that row's own `active_player` then reads as the turn
            # it *produced*, not the one it was actually taken during - a
            # cast/activate/decide/keyword-action/declaration never changes
            # whose turn it is, so every other kind's own value is already
            # exactly right and must not be overridden by a stale carried
            # value (FD4-r1: seat B's own first action of B's new turn was
            # misread as "theirs" this way). The very first row a game (or a
            # hand-built single-row fixture) ever carries has no earlier row
            # at all to read instead, so both fall back to their own value -
            # the only honest reading available, and the one every existing
            # test fixture that attaches its own `state_view` to the one row
            # it builds already assumes.
            pre_state = pre_state_view if pre_state_view is not None else row_state_view
            if kind == "pass":
                pre_active = (
                    pre_active_player if pre_active_player is not None else row_active_player
                )
            else:
                pre_active = (
                    row_active_player if row_active_player is not None else pre_active_player
                )

        if row_state_view is not None:
            pre_state_view = row_state_view
        if row_active_player is not None:
            pre_active_player = row_active_player
        by_seq[row_seq] = {"state_view": row_state_view, "active_player": row_active_player}

        if kind is None:
            continue
        detail = payload.get("detail") or {}
        if detail.get("player") != seat and detail.get("seat") != seat:
            continue
        if kind != "run_script" and detail.get("allowed") is not True:
            continue
        if kind != "run_script" and _is_script_driven(entry, rows):
            # FD4 (j)/G17: already covered by its own run_script record.
            continue

        seq = payload.get("seq", entry.get("seq"))
        turn = _top(payload, detail, "turn")
        step = _top(payload, detail, "step")
        reasoning = _top(payload, detail, "reasoning")
        board = (
            compact_board(row_state_view) if with_boards and row_state_view is not None else None
        )

        if kind == "run_script":
            dsl = _run_script_dsl(rows, index, detail)
            unexpressible = None
            fold_target = None
        else:
            dsl, unexpressible, fold_target = _convert(
                kind,
                detail,
                seat=seat,
                turn=turn,
                step=step,
                active_player=pre_active,
                state_view=pre_state,
                post_state_view=row_state_view,
                fold_target=fold_target,
            )

        out.append(
            {
                "seq": seq,
                "turn": turn,
                "step": step,
                "action": kind,
                "dsl": dsl,
                "reasoning": reasoning,
                "board": board,
                "unexpressible": unexpressible,
            }
        )
        if kind != "run_script":
            record = out[-1]
            if dsl is not None and kind in _FOLDABLE_MOVE_KINDS and bool(detail.get("paused")):
                fold_target = record
            elif kind != "decide":
                fold_target = None
    return out


def frame_seqs_read(
    entries: Iterable[Mapping[str, Any]], seats: Iterable[str], recorded: Iterable[int]
) -> set[int]:
    """The `seq`s whose `state_view` `decision_rows(entries, seat)` actually
    reads for some seat in `seats`, given that exactly the rows in
    `recorded` carry one (OM1): each converted row's own frame (its
    `post_state_view`) and the frame it converts against - the row its
    `pre_action_frame` names, or, without one, the nearest earlier row with
    a frame (its own when there is none). Every other recorded state is
    read by nothing but a `board`, so a caller that asks for none
    (`with_boards=False`) can leave it encoded; attaching only these
    frames converts every row exactly as attaching all of them does.

    Walks `entries` with `decision_rows`' own pointer rules and its own
    filters (committing kind, the seat's own row, `allowed`, not
    script-driven; a `run_script` row reads no frame at all), kept beside
    it so the two cannot drift apart unnoticed."""
    rows = list(entries)
    wanted_seats = set(seats)
    available = set(recorded)
    seen: set[Any] = set()
    nearest: Any = None
    read: set[int] = set()
    for entry in rows:
        payload = _payload(entry)
        kind = _ACTION_KIND.get(str(payload.get("action")))
        row_seq = payload.get("seq", entry.get("seq"))
        explicit_pre_frame = payload.get("pre_action_frame")
        if isinstance(payload.get("pre_state"), Mapping):
            pre = None
        elif explicit_pre_frame and explicit_pre_frame in seen:
            pre = explicit_pre_frame
        else:
            pre = nearest if nearest is not None else row_seq
        seen.add(row_seq)
        if row_seq in available:
            nearest = row_seq
        if kind is None or kind == "run_script":
            continue
        detail = payload.get("detail") or {}
        if detail.get("player") not in wanted_seats and detail.get("seat") not in wanted_seats:
            continue
        if detail.get("allowed") is not True or _is_script_driven(entry, rows):
            continue
        read |= {row_seq, pre} & available
    return read


# -- run_script -----------------------------------------------------------------


def _run_script_dsl(
    rows: Sequence[Mapping[str, Any]], started_index: int, detail: Mapping[str, Any]
) -> dict[str, Any]:
    run_id = detail.get("run_id")
    play_id = str(detail.get("script_id"))
    taken = False
    for entry in rows[:started_index]:
        payload = _payload(entry)
        if payload.get("action") != SCRIPT_RUN_OFFERED_ACTION:
            continue
        offer_detail = payload.get("detail") or {}
        if offer_detail.get("run_id") == run_id:
            taken = True
            break
    return {
        "kind": "run_script",
        "play_id": play_id,
        "offer_outcome": "taken" if taken else "unoffered",
    }


# -- the play skeleton every expressible row shares -----------------------------


def _when(seat: str, active_player: Any, step: Any) -> dict[str, Any] | None:
    if step not in STEPS or active_player is None:
        return None
    whose_turn = "mine" if active_player == seat else "theirs"
    return {"whose_turn": whose_turn, "steps": [step], "stack": "any"}


def _play(play_id: str, when: dict[str, Any], moves: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": play_id,
        "name": play_id,
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": when,
        "true": [],
        "false": [],
        "moves": moves,
        "next": [],
    }


# -- cast / play_land / activate / keyword_action: card-name-free selectors -----


def _find_card(
    state_view: Mapping[str, Any] | None, card_id: Any, name: Any
) -> tuple[dict[str, Any], str] | None:
    """The card `card_id` names, searched for **as an id first** (FD4 (a):
    the ledger's own `card`/`card_id` fields are the card's UUID far more
    often than they are its printed name), and only once every zone one
    redacted view can show a card in at all has been searched for an id
    match does this fall back to `name` - the same pools, searched a second
    time. Plus this seat's own `known_cards` record of a hidden card it has
    looked at (CR 701.20e - a scry/surveil/look-and-keep decision's own
    candidates live only there, never on the public board). `None` when no
    view was given or nothing in it matches either way; this is the only
    place a view's own cards are read, so every selector this module writes
    traces back to here."""
    if not isinstance(state_view, Mapping):
        return None
    wanted_id = str(card_id) if card_id is not None else None
    wanted_name = str(name) if isinstance(name, str) else None
    if wanted_id is None and wanted_name is None:
        return None
    pools: list[tuple[str, Iterable[Any]]] = [
        ("battlefield", state_view.get("battlefield") or ()),
        ("stack", state_view.get("stack") or ()),
        ("exile", state_view.get("exile") or ()),
    ]
    hands = state_view.get("hands")
    if isinstance(hands, Mapping):
        for cards in hands.values():
            pools.append(("hand", cards or ()))
    graveyards = state_view.get("graveyards")
    if isinstance(graveyards, Mapping):
        for cards in graveyards.values():
            pools.append(("graveyard", cards or ()))
    known_cards = state_view.get("known_cards")
    if isinstance(known_cards, Mapping):
        for known in known_cards.values():
            if not isinstance(known, Mapping):
                continue
            for zone_name in ("hand", "sideboard"):
                pools.append((zone_name, known.get(zone_name) or ()))
            library = [
                entry.get("card")
                for entry in known.get("library") or ()
                if isinstance(entry, Mapping) and isinstance(entry.get("card"), Mapping)
            ]
            pools.append(("library", library))
    if wanted_id is not None:
        for zone, cards in pools:
            for card in cards:
                if isinstance(card, Mapping) and str(card.get("id")) == wanted_id:
                    return dict(card), zone
    if wanted_name is not None:
        for zone, cards in pools:
            for card in cards:
                if isinstance(card, Mapping) and card.get("name") == wanted_name:
                    return dict(card), zone
    return None


def _find_card_by_ref(state_view: Mapping[str, Any] | None, ref: Any) -> tuple[dict, str] | None:
    """One ledger field naming a card, which may hold the card's own id or
    (less often, and never for the same row alongside an id) its printed
    name - `_find_card`'s own id-then-name reading, over a single ambiguous
    value (FD4 (a))."""
    if ref is None:
        return None
    ref_str = str(ref)
    return _find_card(state_view, ref_str, ref_str)


def _selector_for_cards(cards: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A `Selector` matching every one of `cards` by characteristic alone -
    type-line words (`selectors.parse_type_line`, already casefolded and
    never a card's own `name`) plus keywords - never a name, which is what
    keeps this inside the card-name ban every selector in this schema is
    written under.

    `types_all` is the *intersection* of every card's own types: the
    selector's AND semantics still admit each card, since each one carries
    at least those shared types. `supertypes_any`/`subtypes_any`/
    `keywords_any` are each the *union* of every card's own words: the
    selector's OR-within-the-field semantics are then guaranteed to admit
    each card by at least one of its own words. One card is the trivial case
    of this - an intersection or union of one set is that set - so this is
    also `_card_selector`'s own reading."""
    type_sets: list[set[str]] = []
    supertypes: set[str] = set()
    subtypes: set[str] = set()
    keywords: set[str] = set()
    for card in cards:
        card_supertypes, card_types, card_subtypes = parse_type_line(card.get("type_line"))
        type_sets.append(card_types)
        supertypes |= card_supertypes
        subtypes |= card_subtypes
        keywords |= {str(k) for k in (card.get("keywords") or ()) if str(k).strip()}
    common_types = set.intersection(*type_sets) if type_sets else set()
    selector: dict[str, Any] = {}
    if common_types:
        selector["types_all"] = sorted(common_types)[:MAX_SELECTOR_WORDS]
    if supertypes:
        selector["supertypes_any"] = sorted(supertypes)[:MAX_SELECTOR_WORDS]
    if subtypes:
        selector["subtypes_any"] = sorted(subtypes)[:MAX_SELECTOR_WORDS]
    if keywords:
        selector["keywords_any"] = sorted(keywords)[:MAX_SELECTOR_WORDS]
    return selector


def _card_selector(card: Mapping[str, Any]) -> dict[str, Any]:
    """A `Selector` for `card` alone - `_selector_for_cards`'s one-card
    case."""
    return _selector_for_cards([card])


def _cast_or_play_land(
    kind: str,
    detail: Mapping[str, Any],
    state_view: Mapping[str, Any] | None,
    post_state_view: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """A `CastMove`/`PlayLandMove`, or `(None, "<kind>:unresolved")` - review
    finding 1: a card genuinely absent from the pre-action frame (drawn, or
    otherwise entered play, since the nearest earlier row's own frame - see
    the module docstring's own "pre-action frame" section) used to fall
    through to a bare `select: {}` unconditionally, matching *any* card in
    the zone rather than honestly failing. `select: {}` now stays reserved
    for the one case the docstring always meant it for - no `state_view` at
    all attached to this row, so there is nothing to look the card up in
    either way. With a pre-action frame attached but missing this specific
    card, this row's own post-action frame (where the card, already cast or
    played, is certainly present) is tried next, purely to identify it - its
    true pre-action zone is then read off the row's own logged `from_zone`
    rather than the post-action frame's (which would read back as `stack`
    for a cast). A card absent from both frames is `unresolved`."""
    ref = detail.get("card")
    pre_found = _find_card_by_ref(state_view, ref)
    found = pre_found
    if found is None and state_view is not None:
        found = _find_card_by_ref(post_state_view, ref)
        if found is None:
            return None, f"{kind}:unresolved"
    select = _card_selector(found[0]) if found is not None else {}
    if kind == "cast":
        zone = pre_found[1] if pre_found is not None else str(detail.get("from_zone") or "hand")
        if zone not in COUNT_ZONES:
            zone = "hand"
        move: dict[str, Any] = {"kind": "cast", "card": {"zone": zone, "select": select}}
        _add_cast_fields(move, detail, state_view)
    else:
        move = {"kind": "play_land", "card": {"select": select}}
    return move, None


def _face_selector(
    detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """`CastMove.face`: the announced `spell_face` named by the card's own
    per-face `type_line` (`view["faces"]`, transform/MDFC only - see
    `_add_cast_fields`'s own docstring), never the face's printed name.
    `None` when there is nothing to build it from at all."""
    spell_face = detail.get("spell_face")
    if not isinstance(spell_face, str) or not spell_face.strip():
        return None
    found = _find_card_by_ref(state_view, detail.get("card"))
    if found is None:
        return None
    card, _zone = found
    faces = card.get("faces")
    if not isinstance(faces, list):
        return None
    face = next((f for f in faces if isinstance(f, Mapping) and f.get("name") == spell_face), None)
    if face is None:
        return None
    _supertypes, types, _subtypes = parse_type_line(face.get("type_line"))
    if not types:
        return None
    return {"types_all": sorted(types)[:MAX_SELECTOR_WORDS]}


#: `CastMove`/`ActivateMove` cost-object field name -> the ledger's own
#: `CastRequest`/`ActivateRequest` field it is logged under (CR 118.3's
#: printed additional costs; `ActivateRequest` has no `tapping` twin).
_CAST_COST_OBJECT_FIELDS = {
    "sacrifice": "sacrificing",
    "discard": "discarding",
    "exile": "exiling",
    "tap": "tapping",
}
_ACTIVATE_COST_OBJECT_FIELDS = {
    "sacrifice": "sacrificing",
    "discard": "discarding",
    "exile": "exiling",
}


def _add_cast_fields(
    move: dict[str, Any], detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> None:
    """FD4 (c): every FD5-logged `CastRequest` field this move's own schema
    can carry, read defensively - a field FD5 has not started logging yet
    (or a fixture that never set it) simply stays absent, exactly like any
    other optional field this module has always left out when a row does
    not carry it. `mana: "auto"` is unconditional, unchanged from before -
    this module never names mana sources.

    `CastRequest.modes` is a list of mode *indices* (CR 700.2a), and no
    board view exposes a card's own mode text anywhere this module can read
    it - unlike an ability's own `abilities[i].text`, there is no `modes`
    field on a card view at all. Writing a `ModeChoice` from a bare index
    would not be a phrase at all, so `modes` is left out entirely rather
    than guessed at; `CastMove.modes` stays unset until a card view carries
    something this module can honestly turn into a phrase.

    `CastRequest.spell_face` names a face by its own printed name
    (`serialize.py`'s own `spell_face` docstring), which `face: Selector`
    must never repeat verbatim (the card-name ban covers a face name the
    same as a card's own). `serialize._faces_view` gives a transform/MDFC
    card's own per-face `type_line` (`view["faces"]`), letting this module
    name the announced face by its own types instead - honest, and never the
    printed name. A split or Adventure card's own faces are not broken out
    on the view at all (`_oracle_text` folds them into one combined
    `oracle_text`), so `face` is left unset for those today, the same
    "nothing here to build it from" reasoning `modes` gets."""
    move["mana"] = "auto"
    keyword = detail.get("keyword")
    alternative_cost = detail.get("alternative_cost")
    if isinstance(keyword, str) and keyword:
        move["cost"] = keyword.lower()
    elif alternative_cost is not None:
        move["cost"] = "printed"
    face_selector = _face_selector(detail, state_view)
    if face_selector is not None:
        move["face"] = face_selector
    targets = detail.get("targets")
    if isinstance(targets, list) and targets:
        seat = detail.get("player")
        built = [_target_for_token(str(t), str(seat), state_view) for t in targets]
        if all(t is not None for t in built) and len(built) <= MAX_TARGETS:
            move["targets"] = built
    x_value = detail.get("x_value")
    if isinstance(x_value, int) and not isinstance(x_value, bool):
        move["x"] = x_value
    shares = detail.get("shares")
    if isinstance(shares, list) and shares and all(isinstance(s, int) for s in shares):
        move["shares"] = shares
    optional_costs = detail.get("optional_costs")
    if isinstance(optional_costs, list) and optional_costs:
        picked = [
            {"text_has": str(entry["keyword"])}
            for entry in optional_costs
            if isinstance(entry, Mapping) and isinstance(entry.get("keyword"), str)
        ]
        if picked:
            move["optional_costs"] = picked
    for move_field, detail_field in _CAST_COST_OBJECT_FIELDS.items():
        pick = _pick_from_ids(detail.get(detail_field), state_view)
        if pick is not None:
            move[move_field] = pick
    for move_field, detail_field in (("delve", "delve"), ("improvise", "improvise")):
        pick = _pick_from_ids(detail.get(detail_field), state_view)
        if pick is not None:
            move[move_field] = pick
    convoke = detail.get("convoke")
    if isinstance(convoke, list) and convoke:
        convoke_ids = [entry.get("card_id") for entry in convoke if isinstance(entry, Mapping)]
        pick = _pick_from_ids(convoke_ids, state_view)
        if pick is not None:
            move["convoke"] = pick


def _pick_from_ids(ids: Any, state_view: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A `Pick` (`{select, count}`) for a cost-object field's own list of
    card ids, resolved the same card-name-free way every other selector in
    this module is - `None` when there is nothing to build (the field was
    not logged, or every id in it failed to resolve)."""
    if not isinstance(ids, list) or not ids:
        return None
    found = [_find_card_by_ref(state_view, ref) for ref in ids]
    if any(f is None for f in found):
        return None
    cards = [f[0] for f in found]
    return {"select": _selector_for_cards(cards), "count": len(cards)}


def _activate(
    detail: Mapping[str, Any],
    state_view: Mapping[str, Any] | None,
    post_state_view: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """An `ActivateMove` for this row, or `(None, label)` when there is no
    honest one to write. With no board data at all there is no `source` to
    select and no ability to name either way - `activate:unresolved`, never
    a bare empty selector the way `cast`/`play_land` fall back to (a
    `source` naming nothing is not "whichever permanent", it is no
    permanent). A pre-action frame attached but missing this specific source
    (review finding 6 - an intervening draw/trigger the carried-forward
    frame never saw) is resolved the same way `_cast_or_play_land` now does:
    identified off this row's own post-action frame instead of failing
    outright. Its true pre-action zone is unknown then (`action_activate`
    logs no zone hint at all, unlike a cast's own `from_zone`), so it
    defaults to `battlefield` - CR 602.1's own overwhelmingly common case,
    and already this function's own default for any zone `COUNT_ZONES`
    cannot name. With the card found, FD4 (c)/G7 prefers `ability_index`
    (FB2's own footing, exempt from the lint's `phrase-too-narrow` since
    FD2) over `ability_text_has` whenever a usable index is on the row at
    all - a hand-written oracle-text phrase is only as safe as knowing it is
    shared by at least three cards, which this module, with no card catalog
    of its own, cannot verify; the numeric index carries no such risk."""
    ref = detail.get("card_id") or detail.get("card")
    found = _find_card_by_ref(state_view, ref)
    if found is None and state_view is not None:
        post_found = _find_card_by_ref(post_state_view, ref)
        if post_found is not None:
            found = (post_found[0], "battlefield")
    if found is None:
        return None, "activate:unresolved"
    card, zone = found
    abilities = card.get("abilities") or []
    index = detail.get("ability_index")
    move: dict[str, Any] = {
        "kind": "activate",
        "source": {
            "zone": zone if zone in COUNT_ZONES else "battlefield",
            "select": _card_selector(card),
        },
    }
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index <= MAX_ABILITY_INDEX:
        move["ability_index"] = index
    else:
        ability = (
            abilities[index] if isinstance(index, int) and 0 <= index < len(abilities) else None
        )
        text = ability.get("text") if isinstance(ability, Mapping) else None
        if isinstance(text, str) and text.strip():
            move["ability_text_has"] = text
        else:
            return None, "activate:unresolved-ability"
    _add_activate_fields(move, detail, state_view)
    return move, None


def _add_activate_fields(
    move: dict[str, Any], detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> None:
    """FD4 (c): targets/sacrifice/discard/exile/x/color from FD5's logged
    `ActivateRequest` fields, read the same defensive way `_add_cast_fields`
    reads a cast's own."""
    targets = detail.get("targets")
    if isinstance(targets, list) and targets:
        seat = detail.get("player")
        built = [_target_for_token(str(t), str(seat), state_view) for t in targets]
        if all(t is not None for t in built) and len(built) <= MAX_TARGETS:
            move["targets"] = built
    for move_field, detail_field in _ACTIVATE_COST_OBJECT_FIELDS.items():
        pick = _pick_from_ids(detail.get(detail_field), state_view)
        if pick is not None:
            move[move_field] = pick
    x_value = detail.get("x_value")
    if isinstance(x_value, int) and not isinstance(x_value, bool):
        move["x"] = x_value
    color = detail.get("color")
    if isinstance(color, str) and color:
        move["color"] = color


def _keyword_action(
    action: str,
    detail: Mapping[str, Any],
    seat: str,
    state_view: Mapping[str, Any] | None,
    post_state_view: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """FD4 (d): one `KeywordActionMove` for any of the eight routes
    `KEYWORD_ACTIONS` names - card-name-free the same way every other move
    here is, `target`/`crew_with` resolved the same way. A card missing from
    the pre-action frame falls back to identifying it off the post-action
    one first (review finding 6, the same treatment `_cast_or_play_land`/
    `_activate` give their own card lookups) - its pre-action zone then
    defaults to `hand`, this function's own existing default for a zone
    `COUNT_ZONES` cannot name (every one of these eight routes originates
    from hand)."""
    card_ref = detail.get("card_id") if detail.get("card_id") is not None else detail.get("card")
    found = _find_card_by_ref(state_view, card_ref)
    if found is None and state_view is not None:
        post_found = _find_card_by_ref(post_state_view, card_ref)
        if post_found is not None:
            found = (post_found[0], "hand")
    if found is None:
        return None, f"{action}:unresolved"
    card, zone = found
    move: dict[str, Any] = {
        "kind": "keyword_action",
        "action": action,
        "card": {"zone": zone if zone in COUNT_ZONES else "hand", "select": _card_selector(card)},
    }
    if action == "equip":
        target_found = _find_card_by_ref(state_view, detail.get("target"))
        if target_found is None:
            return None, f"{action}:unresolved-target"
        target_card, _zone = target_found
        move["target"] = {
            "select": _card_selector(target_card),
            "side": "me" if target_card.get("controller") == seat else "opponent",
        }
    elif action == "crew":
        refs = detail.get("crewing") or []
        crewing_cards: list[dict[str, Any]] = []
        for ref in refs:
            crew_found = _find_card_by_ref(state_view, ref)
            if crew_found is None:
                return None, f"{action}:unresolved-crew"
            crewing_cards.append(crew_found[0])
        if crewing_cards:
            move["crew_with"] = {
                "select": _selector_for_cards(crewing_cards),
                "count": len(crewing_cards),
            }
    elif action == "station":
        # CR 702.184a: the one creature tapped, recorded as a one-id list.
        refs = detail.get("tapping") or []
        if isinstance(refs, str):
            refs = [refs]
        tapped_cards: list[dict[str, Any]] = []
        for ref in refs:
            tapped_found = _find_card_by_ref(state_view, ref)
            if tapped_found is None:
                return None, f"{action}:unresolved-tapped"
            tapped_cards.append(tapped_found[0])
        if tapped_cards:
            move["station_with"] = {
                "select": _selector_for_cards(tapped_cards),
                "count": 1,
            }
    elif action == "unlock_door":
        # CR 709.5e: the locked half's own printed name, carried straight
        # through - never a card, so there is nothing to resolve here.
        door = detail.get("door")
        if not isinstance(door, str) or not door.strip():
            return None, f"{action}:unresolved"
        move["door"] = door
    return move, None


# -- declare_attackers / declare_blockers: FB2's B4 combat moves, FD4 (g)'s ------
# -- /decide `pairs` route folded into the same two builders --------------------


def _defender_for_token(token: str, state_view: Mapping[str, Any] | None) -> Any:
    """The `Defender` FB2's `AttackGroup` names for `token` (a player's
    username or a card id - `decisions.declared_pairs_answer`'s own token
    shape for a `defender`): `None` when it names the opponent player (the
    schema's own default for a left-out `defender`, so nothing is written at
    all), a `DefenderCard` selector when it resolves to one of their
    permanents, or the module's own `_UNRESOLVED` sentinel when no board
    data can ground either reading honestly."""
    if not isinstance(state_view, Mapping):
        return _UNRESOLVED
    players = {
        p.get("username") for p in (state_view.get("players") or ()) if isinstance(p, Mapping)
    }
    if token in players:
        return None
    found = _find_card(state_view, token, None)
    if found is None:
        return _UNRESOLVED
    card, _zone = found
    return {"select": _card_selector(card), "side": "opponent"}


def _declare_attackers(
    detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """A `DeclareAttackersMove` for this row's own `attackers` (CR 508.1),
    grouped by shared defender into one `AttackGroup` apiece - "an attack of
    two creatures at the player" is one group, `count: 2`, not two. `[]`
    needs no board data at all (still a legal "no attackers" declaration);
    a non-empty list needs every attacker and every card-shaped defender
    resolved in `state_view`, or this is `None` (the caller's `_ATTACK`
    fallback)."""
    attackers = detail.get("attackers")
    if not isinstance(attackers, list):
        return None
    if not attackers:
        return {"kind": "declare_attackers", "attackers": []}
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for entry in attackers:
        if not isinstance(entry, Mapping):
            return None
        found = _find_card_by_ref(state_view, entry.get("card"))
        if found is None:
            return None
        card, _zone = found
        key = str(entry.get("defender"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(card)
    if len(order) > MAX_ATTACK_GROUPS:
        return None
    attack_groups: list[dict[str, Any]] = []
    for key in order:
        cards = groups[key]
        group: dict[str, Any] = {"select": _selector_for_cards(cards), "count": len(cards)}
        defender = _defender_for_token(key, state_view)
        if defender is _UNRESOLVED:
            return None
        if defender is not None:
            group["defender"] = defender
        attack_groups.append(group)
    return {"kind": "declare_attackers", "attackers": attack_groups}


def _declare_blockers(
    detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """A `DeclareBlockersMove` for this row's own `blocks` (CR 509.1),
    grouped by shared attacker into one `Block` apiece (a double block is one
    `Block`, `blocker.count: 2`). `[]` needs no board data at all; a
    non-empty list needs every blocker and every attacker resolved in
    `state_view`, or this is `None` (the caller's `_BLOCK` fallback)."""
    blocks = detail.get("blocks")
    if not isinstance(blocks, list):
        return None
    if not blocks:
        return {"kind": "declare_blockers", "blocks": []}
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for entry in blocks:
        if not isinstance(entry, Mapping):
            return None
        found = _find_card_by_ref(state_view, entry.get("blocker"))
        if found is None:
            return None
        blocker_card, _zone = found
        key = str(entry.get("blocked"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(blocker_card)
    if len(order) > MAX_BLOCKS:
        return None
    result: list[dict[str, Any]] = []
    for key in order:
        attacker_found = _find_card_by_ref(state_view, key)
        if attacker_found is None:
            return None
        attacker_card, _zone = attacker_found
        blocker_cards = groups[key]
        result.append(
            {
                "blocker": {
                    "select": _selector_for_cards(blocker_cards),
                    "count": len(blocker_cards),
                },
                "attacker": {"select": _card_selector(attacker_card)},
            }
        )
    return {"kind": "declare_blockers", "blocks": result}


def _combat_pairs_detail(decide_kind: str, detail: Mapping[str, Any]) -> dict[str, Any] | None:
    """FD4 (g): `/decide`'s own `pairs` (`{creature, defender}`, the shape
    `decisions.declared_pairs_answer` takes for both `DECLARE_ATTACKERS` and
    `DECLARE_BLOCKERS`), reshaped into the dedicated routes' own
    `attackers`/`blocks` detail shape so `_declare_attackers`/
    `_declare_blockers` need only one reading of either declaration, however
    it reached the ledger."""
    pairs = detail.get("pairs")
    if not isinstance(pairs, list):
        return None
    reshaped: list[dict[str, Any]] = []
    for pair in pairs:
        if not isinstance(pair, Mapping):
            return None
        creature = pair.get("creature")
        defender = pair.get("defender")
        if decide_kind == "declare_attackers":
            reshaped.append({"card": creature, "defender": defender})
        else:
            reshaped.append({"blocker": creature, "blocked": defender})
    key = "attackers" if decide_kind == "declare_attackers" else "blocks"
    return {key: reshaped}


# -- decide/answer ---------------------------------------------------------------


def _target_for_token(
    token: str, seat: str, state_view: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """One `Target` for a `choose_targets` token (a player's username or a
    card id - `decisions.targets_answer`'s own token shape): a
    `PlayerTarget` when the token names a player of the game, a `CardTarget`
    selector when it resolves to a card in `state_view` (`zone` set only
    when the card's own zone is one `CardTarget.zone` can name - FB2 T1;
    left out, a target still resolves over battlefield and stack), or `None`
    when neither reading can be grounded."""
    if not isinstance(state_view, Mapping):
        return None
    players = {
        p.get("username") for p in (state_view.get("players") or ()) if isinstance(p, Mapping)
    }
    if token in players:
        return {"player": "me"} if token == seat else {"player": "opponent"}
    found = _find_card(state_view, token, None)
    if found is None:
        return None
    card, zone = found
    target: dict[str, Any] = {
        "select": _card_selector(card),
        "side": "me" if card.get("controller") == seat else "opponent",
    }
    if zone in TARGET_ZONES:
        target["zone"] = zone
    return target


def _scry_or_surveil_answer(
    kind: str, detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """`to_bottom` (scry, CR 701.22a) or `to_graveyard` (surveil, CR 701.25a)
    plus `top_order` (FB2 A1) for a `scry`/`surveil` row's own `moved`/
    `top_order` ids. An empty `moved` with no `top_order` is the bare "keep
    everything on top, unchanged" answer, needing no board data at all - the
    same reading the module already gave it. A non-empty one needs every
    named card resolved in `state_view`, and `top_order` at or under
    `MAX_TOP_ORDER` tiers (one card's own selector per tier, in the order
    the row itself gives - the finest-grained honest reading of a fixed
    permutation), or this is `(None, "decide:<kind>:<reason>")` - `malformed`
    when `moved` itself is not a list, `too-many` when `top_order` overruns
    `MAX_TOP_ORDER`, `unresolved` when a named card cannot be found."""
    moved = detail.get("moved")
    top_order = detail.get("top_order")
    if not isinstance(moved, list):
        return None, f"decide:{kind}:malformed"
    if not moved and not top_order:
        return {"decision": kind}, None
    answer: dict[str, Any] = {"decision": kind}
    if moved:
        moved_found = [_find_card(state_view, card_id, None) for card_id in moved]
        if any(found is None for found in moved_found):
            return None, f"decide:{kind}:unresolved"
        field = "to_bottom" if kind == "scry" else "to_graveyard"
        answer[field] = _selector_for_cards([found[0] for found in moved_found])
    if top_order:
        if len(top_order) > MAX_TOP_ORDER:
            return None, f"decide:{kind}:too-many"
        order_found = [_find_card(state_view, card_id, None) for card_id in top_order]
        if any(found is None for found in order_found):
            return None, f"decide:{kind}:unresolved"
        answer["top_order"] = [_card_selector(found[0]) for found in order_found]
    return answer, None


def _choose_cards_answer(
    detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """A `pick` answer (a "look and keep" `choose_cards` row's own
    `card_ids`, e.g. a fetch's search, CR 401.2): every named card resolved
    in `state_view` and merged into one selector, `count` the number chosen.
    FD4 (h)/G14: an empty `card_ids` is a legal "choose none",
    `{"select": {}, "count": 0}`, needing no board data - `(None,
    "decide:choose_cards:malformed")` when the field itself is not a list,
    `(None, "decide:choose_cards:unresolved")` when the ids are present but
    any one of them cannot be resolved."""
    card_ids = detail.get("card_ids")
    if not isinstance(card_ids, list):
        return None, "decide:choose_cards:malformed"
    if not card_ids:
        return {"decision": "choose_cards", "pick": {"select": {}, "count": 0}}, None
    found = [_find_card(state_view, card_id, None) for card_id in card_ids]
    if any(entry is None for entry in found):
        return None, "decide:choose_cards:unresolved"
    cards = [entry[0] for entry in found if entry is not None]
    return {
        "decision": "choose_cards",
        "pick": {"select": _selector_for_cards(cards), "count": len(cards)},
    }, None


def _choose_targets_answer(
    detail: Mapping[str, Any], seat: str, state_view: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """A `targets` answer (FB2 A2) for a `choose_targets` row's own
    `targets` tokens - card or player, `_target_for_token` each. FD4
    (h)/G14: an empty list is a legal "choose none", needing no board data -
    `(None, "decide:choose_targets:malformed")` when the field is not a
    list, `:too-many` over `MAX_TARGETS`, `:unresolved` when any one token
    cannot be resolved."""
    tokens = detail.get("targets")
    if not isinstance(tokens, list):
        return None, "decide:choose_targets:malformed"
    if len(tokens) > MAX_TARGETS:
        return None, "decide:choose_targets:too-many"
    if not tokens:
        return {"decision": "choose_targets", "targets": []}, None
    targets: list[dict[str, Any]] = []
    for token in tokens:
        target = _target_for_token(str(token), seat, state_view)
        if target is None:
            return None, "decide:choose_targets:unresolved"
        targets.append(target)
    return {"decision": "choose_targets", "targets": targets}, None


def _order_triggers_answer(
    detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """FD4 (h)/G10: `order` for `order_triggers` (CR 603.3b) - one selector
    tier per pending triggered ability, in the row's own declared order
    (`Answer.order`'s own docstring: the first tier goes on the stack first
    and so resolves last; this module writes the tiers in exactly the order
    the row's own `order` list gives, which is that same stack-order
    convention already). `malformed` when `order` is missing, not a list, or
    empty; `too-many` over `MAX_ORDER`; `unresolved` when a named ability's
    own card cannot be found."""
    order_ids = detail.get("order")
    if not isinstance(order_ids, list) or not order_ids:
        return None, "decide:order_triggers:malformed"
    if len(order_ids) > MAX_ORDER:
        return None, "decide:order_triggers:too-many"
    found = [_find_card(state_view, ability_id, None) for ability_id in order_ids]
    if any(entry is None for entry in found):
        return None, "decide:order_triggers:unresolved"
    return {
        "decision": "order_triggers",
        "order": [_card_selector(entry[0]) for entry in found if entry is not None],
    }, None


def _assign_combat_damage_answer(
    detail: Mapping[str, Any], state_view: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """FD4 (h)/G11: `damage`, a `DamagePolicy` for `assign_combat_damage`
    (CR 510.1c/702.19b) - `tiers` replays the row's own `damage` list order
    (each entry's own declared assignment step), and `rest_to` is
    `"player"` exactly when the row assigned a positive amount to the
    defending player past its blockers (trample, CR 702.19b); left out
    otherwise, the schema's own default. `lethal_first` is left out too -
    this module has no board data proving whether every earlier tier's own
    lethal amount was actually assigned first, and the schema's own default
    is CR 510.1c's own order regardless. `malformed` when `damage` is
    missing, not a list, empty, or holds a badly-shaped entry; `unresolved`
    when a named target cannot be found; `too-many` over `MAX_DAMAGE_TIERS`."""
    entries = detail.get("damage")
    if not isinstance(entries, list) or not entries:
        return None, "decide:assign_combat_damage:malformed"
    tiers: list[dict[str, Any]] = []
    rest_to: str | None = None
    for item in entries:
        if not isinstance(item, Mapping):
            return None, "decide:assign_combat_damage:malformed"
        amount = item.get("amount")
        if not isinstance(amount, int) or isinstance(amount, bool):
            return None, "decide:assign_combat_damage:malformed"
        if amount <= 0:
            continue
        target = _defender_for_token(str(item.get("target")), state_view)
        if target is _UNRESOLVED:
            return None, "decide:assign_combat_damage:unresolved"
        if target is None:
            rest_to = "player"
            continue
        tiers.append(target["select"])
    if not tiers and rest_to is None:
        return None, "decide:assign_combat_damage:malformed"
    if len(tiers) > MAX_DAMAGE_TIERS:
        return None, "decide:assign_combat_damage:too-many"
    policy: dict[str, Any] = {"tiers": tiers}
    if rest_to is not None:
        policy["rest_to"] = rest_to
    return {"decision": "assign_combat_damage", "damage": policy}, None


def _choice_answer(
    kind: str, detail: Mapping[str, Any], seat: str, state_view: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """A `choice` answer (O6) for `choose_a_mode`/`choose_a_quality`, or -
    FD4 (h)/G15 - `choice_from` (a selector, never the name) when `kind` is
    `choose_a_quality` and the row's own chosen string itself resolves to a
    real card in `state_view` (Meddling Mage's own "name a card"). A
    `choose_a_quality` naming a creature type or other non-card quality
    (Cavern of Souls) never resolves to a card and keeps the plain
    `choice`, unchanged."""
    choice = detail.get("choice")
    if not isinstance(choice, str) or not choice.strip():
        return None, f"decide:{kind}:malformed"
    if kind == "choose_a_quality":
        found = _find_card(state_view, None, choice)
        if found is not None:
            card, zone = found
            return {
                "decision": kind,
                "choice_from": {
                    "zone": zone if zone in COUNT_ZONES else "battlefield",
                    "side": "me" if card.get("controller") == seat else "opponent",
                    "select": _card_selector(card),
                },
            }, None
    return {"decision": kind, "choice": choice}, None


def _answer_for_decide(
    decide_kind: Any, detail: Mapping[str, Any], seat: str, state_view: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """One `Answer` for a `decide` row's own `detail["kind"]`, or (`None`,
    the `unexpressible` label) - see the module docstring's finer-grained
    "not in the vocabulary" section for what each label means. Never called
    for `mulligan`/`opening_hand_action` (their own pregame handling) or the
    two combat-declaration kinds (their own move kinds) - `decision_rows`'s
    own dispatch routes those before this function is ever reached."""
    kind = str(decide_kind)
    if kind in _ACCEPT_KINDS:
        return {"decision": kind, "accept": bool(detail.get("accept"))}, None
    if kind in AMOUNT_DECISION_KINDS:
        field = _AMOUNT_FIELD.get(kind)
        amount = detail.get(field) if field else None
        if not isinstance(amount, int) or isinstance(amount, bool):
            return None, f"decide:{kind}:malformed"
        return {"decision": kind, "amount": amount}, None
    if kind in CHOICE_DECISION_KINDS:
        return _choice_answer(kind, detail, seat, state_view)
    if kind == "put_into_hand_or_graveyard":
        to_hand = detail.get("to_hand")
        if not isinstance(to_hand, bool):
            return None, f"decide:{kind}:malformed"
        return {"decision": kind, "to_hand": to_hand}, None
    if kind == "choose_pile":
        pile_one = detail.get("pile_one")
        if not isinstance(pile_one, bool):
            return None, f"decide:{kind}:malformed"
        return {"decision": kind, "pile_one": pile_one}, None
    if kind == "put_on_top_or_bottom":
        top = detail.get("top")
        if not isinstance(top, bool):
            return None, f"decide:{kind}:malformed"
        return {"decision": kind, "position": "top" if top else "bottom"}, None
    if kind in ("surveil", "scry"):
        return _scry_or_surveil_answer(kind, detail, state_view)
    if kind == "choose_cards":
        return _choose_cards_answer(detail, state_view)
    if kind == "choose_targets":
        return _choose_targets_answer(detail, seat, state_view)
    if kind == "choose_players":
        usernames = detail.get("usernames")
        if not isinstance(usernames, list):
            return None, f"decide:{kind}:malformed"
        players = sorted({"me" if name == seat else "opponent" for name in usernames})
        return {"decision": kind, "players": players}, None
    if kind == "order_triggers":
        return _order_triggers_answer(detail, state_view)
    if kind == "assign_combat_damage":
        return _assign_combat_damage_answer(detail, state_view)
    return None, f"decide:{kind}:unsupported"


def _pregame_decide_play(decide_kind: str, detail: Mapping[str, Any]) -> dict[str, Any] | None:
    """FD4 (h)/G4: a plain `accept` answer for `mulligan`/
    `opening_hand_action` reached through the engine's own pregame `decide`
    flow (as opposed to the dedicated `mulligan_declared`/
    `opening_hand_action` action rows - `_mulligan_declared_play`/
    `_opening_hand_action_play`) - `when` built from the schema-only step,
    since these rows carry no `turn`/`step` of their own."""
    accept = detail.get("accept")
    if not isinstance(accept, bool):
        return None
    step_name = _PREGAME_DECIDE_STEPS[decide_kind]
    when = {"whose_turn": "either", "steps": [step_name], "stack": "empty"}
    move = {"kind": "answer", "answers": [{"decision": decide_kind, "accept": accept}]}
    play = _play(f"decision-decide-{decide_kind}", when, [move])
    play["true"] = [{"kind": "pending_decision", "decision": decide_kind}]
    return play


def _mulligan_declared_play(detail: Mapping[str, Any]) -> dict[str, Any] | None:
    """FD4 (h)/G4: the FD2 `mulligan` move for a `mulligan_declared` row -
    `keep` the declaration's own inverse of `take`; `bottom` left out, the
    engine's own default (the back of the hand) - this row names only
    *whether* a bottoming happened (`bottom_chosen`), never which cards it
    put there, so naming a `bottom` selector here would be a guess this
    module does not make."""
    take = detail.get("take")
    if not isinstance(take, bool):
        return None
    when = {"whose_turn": "either", "steps": ["mulligan"], "stack": "empty"}
    move = {"kind": "mulligan", "keep": not take}
    return _play("decision-mulligan", when, [move])


def _opening_hand_action_play() -> dict[str, Any]:
    """FD4 (h)/G4: a plain `accept` answer to `opening_hand_action` for the
    dedicated action row - see the module docstring for why this module
    does not attempt to name *which* card the row acted on."""
    when = {"whose_turn": "either", "steps": ["pregame_actions"], "stack": "empty"}
    move = {"kind": "answer", "answers": [{"decision": "opening_hand_action", "accept": True}]}
    play = _play("decision-opening-hand-action", when, [move])
    play["true"] = [{"kind": "pending_decision", "decision": "opening_hand_action"}]
    return play


# -- fold: a decide resolving the preceding move's own object (FD4 (f)/G2) ------


def _fold_answer_into(fold_target: dict[str, Any], answer: dict[str, Any]) -> bool:
    """Appends `answer` onto `fold_target`'s own play's one move, or does
    nothing and reports `False` once that move already carries
    `MAX_ANSWERS` of them."""
    play = fold_target.get("dsl")
    if not isinstance(play, Mapping) or not play.get("moves"):
        return False
    move = play["moves"][0]
    answers = move.setdefault("answers", [])
    if len(answers) >= MAX_ANSWERS:
        return False
    answers.append(answer)
    return True


# -- dispatch --------------------------------------------------------------------


def _convert(
    kind: str,
    detail: Mapping[str, Any],
    *,
    seat: str,
    turn: Any,
    step: Any,
    active_player: Any,
    state_view: Mapping[str, Any] | None,
    post_state_view: Mapping[str, Any] | None,
    fold_target: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """`(dsl, unexpressible, next_fold_target)` for one non-`run_script` row.
    `fold_target` is the open fold from a still-unresolved paused move
    (FD4 (f)/G2); returned unchanged, cleared, or replaced by this row's own
    record - see `decision_rows`'s own bookkeeping, which this function
    otherwise leaves alone. `state_view` is the pre-action frame every
    lookup in this function reads by default; `post_state_view` (the row's
    own frame) is passed through only for `order_triggers` - CR 603.3b's
    own triggered abilities are not yet objects on the stack in the
    *pre*-action frame at all (ordering is what puts them there), so that
    one answer is the one honest exception to "never the post-action
    board"."""
    if kind == "mulligan":
        play = _mulligan_declared_play(detail)
        return (play, None, None) if play is not None else (None, _MULLIGAN, None)
    if kind == "concede":
        return None, _CONCEDE, None
    if kind == "opening_hand_action":
        return _opening_hand_action_play(), None, None

    if kind in ("cast", "play_land"):
        when = _when(seat, active_player, step)
        if when is None:
            return None, f"{kind}:unrepresentable-step", None
        move, label = _cast_or_play_land(kind, detail, state_view, post_state_view)
        if move is None:
            return None, label, None
        return _play(f"decision-{kind}", when, [move]), None, None
    if kind == "activate":
        # Review fix: this branch used to build the play straight off
        # `_when`'s own result with no None check at all - unlike every
        # other branch here - so a row whose step the vocabulary cannot
        # represent silently produced a schema-invalid `when: None` Play
        # reported as expressible (`unexpressible: None`).
        when = _when(seat, active_player, step)
        if when is None:
            return None, "activate:unrepresentable-step", None
        move, label = _activate(detail, state_view, post_state_view)
        if move is None:
            return None, label, None
        return _play("decision-activate", when, [move]), None, None
    if kind in KEYWORD_ACTIONS:
        move, label = _keyword_action(kind, detail, seat, state_view, post_state_view)
        if move is None:
            return None, label, None
        when = _when(seat, active_player, step)
        if when is None:
            return None, f"{kind}:unrepresentable-step", None
        return _play(f"decision-{kind}", when, [move]), None, None
    if kind == "pass":
        when = _when(seat, active_player, step)
        if when is None:
            return None, "pass:unrepresentable-step", None
        return _play("decision-pass", when, [{"kind": "pass"}]), None, None
    if kind == "declare_attackers":
        # Review fix: same missing-`when`-check bug as `activate` above.
        when = _when(seat, active_player, step)
        if when is None:
            return None, "declare_attackers:unrepresentable-step", None
        move = _declare_attackers(detail, state_view)
        if move is None:
            return None, _ATTACK, None
        return _play("decision-declare-attackers", when, [move]), None, None
    if kind == "declare_blockers":
        when = _when(seat, active_player, step)
        if when is None:
            return None, "declare_blockers:unrepresentable-step", None
        move = _declare_blockers(detail, state_view)
        if move is None:
            return None, _BLOCK, None
        return _play("decision-declare-blockers", when, [move]), None, None
    if kind == "decide":
        decide_kind = str(detail.get("kind"))
        if decide_kind in _PREGAME_DECIDE_STEPS:
            play = _pregame_decide_play(decide_kind, detail)
            if play is not None:
                return play, None, None
            return None, f"decide:{decide_kind}:malformed", None
        if decide_kind in _COMBAT_DECIDE_KINDS:
            combat_detail = _combat_pairs_detail(decide_kind, detail)
            fallback = _ATTACK if decide_kind == "declare_attackers" else _BLOCK
            if combat_detail is None:
                return None, fallback, fold_target
            builder = (
                _declare_attackers if decide_kind == "declare_attackers" else _declare_blockers
            )
            move = builder(combat_detail, state_view)
            if move is None:
                return None, fallback, fold_target
            when = _when(seat, active_player, step)
            if when is None:
                return None, f"{decide_kind}:unrepresentable-step", fold_target
            play = _play(f"decision-{decide_kind.replace('_', '-')}", when, [move])
            play["true"] = [{"kind": "pending_decision", "decision": decide_kind}]
            return play, None, fold_target
        # order_triggers is the one exception: see this function's own
        # docstring for why its cards are looked up post-action.
        lookup_view = post_state_view if decide_kind == "order_triggers" else state_view
        answer, unexpressible = _answer_for_decide(decide_kind, detail, seat, lookup_view)
        if answer is None:
            return None, unexpressible, None
        if fold_target is not None and _fold_answer_into(fold_target, answer):
            return copy.deepcopy(fold_target["dsl"]), None, fold_target
        when = _when(seat, active_player, step)
        if when is None:
            return None, f"decide:{decide_kind}:unrepresentable-step", None
        move = {"kind": "answer", "answers": [answer]}
        play = _play("decision-decide", when, [move])
        play["true"] = [{"kind": "pending_decision", "decision": decide_kind}]
        return play, None, None
    return None, f"{kind}:unsupported", None


# -- compact_board ----------------------------------------------------------------

#: CR 205.4a's own supertypes, plus `snow`/`ongoing` - the words
#: `selectors.SUPERTYPES` already treats as supertypes rather than types.
_LAND_WORD = "land"


def _players_by_name(view: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(player.get("username")): player
        for player in view.get("players") or ()
        if isinstance(player, Mapping)
    }


def _permanent(card: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": card.get("name") or card.get("card_name"),
        "tapped": bool(card.get("tapped")),
    }
    power, toughness = card.get("power"), card.get("toughness")
    if power is not None or toughness is not None:
        out["pt"] = (
            f"{power if power is not None else '*'}/{toughness if toughness is not None else '*'}"
        )
    counters = {k: v for k, v in (card.get("counters") or {}).items() if v}
    if counters:
        out["counters"] = counters
    return out


def _build_board(
    view: Mapping[str, Any], *, with_graveyard: bool, with_counters: bool
) -> dict[str, Any]:
    battlefield = [card for card in view.get("battlefield") or () if isinstance(card, Mapping)]
    exile = [card for card in view.get("exile") or () if isinstance(card, Mapping)]
    players: dict[str, Any] = {}
    for name, player in _players_by_name(view).items():
        own = [card for card in battlefield if card.get("controller") == name]
        lands = [card for card in own if _LAND_WORD in parse_type_line(card.get("type_line"))[1]]
        entry: dict[str, Any] = {
            "life": player.get("life"),
            "hand": player.get("hand_count"),
            "library": player.get("library_count"),
            "lands": {
                "untapped": sum(1 for card in lands if not card.get("tapped")),
                "total": len(lands),
            },
            "battlefield": [
                _permanent(card)
                if with_counters
                else {"name": card.get("name"), "tapped": bool(card.get("tapped"))}
                for card in sorted(own, key=lambda card: str(card.get("name")))
            ],
        }
        if with_graveyard:
            entry["graveyard"] = player.get("graveyard_count")
            entry["exile"] = sum(1 for card in exile if card.get("controller") == name)
        players[name] = entry
    return {
        "turn": view.get("turn_number"),
        "step": view.get("step"),
        "active_player": view.get("active_player"),
        "players": players,
        "stack": [
            card.get("name") for card in (view.get("stack") or ()) if isinstance(card, Mapping)
        ],
    }


def _size(obj: Any) -> int:
    import json

    return len(json.dumps(obj, sort_keys=True))


def _sorted(obj: Any) -> Any:
    """`obj`, recursively rebuilt with every dict's keys in sorted order -
    so the dict `compact_board` returns is already what
    `json.dumps(..., sort_keys=True)` would print, with no reader needing to
    ask for that explicitly."""
    if isinstance(obj, Mapping):
        return {key: _sorted(obj[key]) for key in sorted(obj, key=str)}
    if isinstance(obj, list):
        return [_sorted(item) for item in obj]
    return obj


def compact_board(state_view: Mapping[str, Any]) -> dict[str, Any]:
    """A `GameStateView`-shaped dict (`serialize.state_view`'s own shape, as
    `GET /review/frames/{seq}` hands it back), reduced to the compact board
    one decision record carries: per player, life, hand size, library size,
    graveyard/exile counts, lands untapped/total, and its own battlefield
    permanents as `{name, tapped, pt, counters}`; the stack's own card names,
    turn/step/active player shared at the top. Deterministic (same input,
    same output) and its keys are written in a fixed, sorted order at every
    level, so two calls on the same view - and `json.dumps(...,
    sort_keys=True)` of the result - agree byte for byte.

    Stays at or under `COMPACT_BOARD_CHARS` once serialized, cutting in this
    order until it does:

    1. every player's `graveyard`/`exile` counts, dropped whole;
    2. every battlefield permanent's `pt`/`counters`, down to bare
       `{name, tapped}`;
    3. the fullest player's (most battlefield permanents) own battlefield
       list, alphabetically-last name first, one permanent at a time, until
       that player's own list is empty - then the next-fullest, and so on
       until every player's battlefield list is empty;
    4. the stack's own names, bottom of stack (oldest, least likely to be
       what this decision is about) first, one at a time, until it fits or
       the stack is empty too.

    Life and the lands untapped/total summary are never cut at any stage -
    a board that has lost everything else still says how much life and how
    much mana each side has, which is the one thing a reviewer must never
    be left guessing at."""
    stages = [
        {"with_graveyard": True, "with_counters": True},
        {"with_graveyard": False, "with_counters": True},
        {"with_graveyard": False, "with_counters": False},
    ]
    board = None
    for stage in stages:
        board = _build_board(state_view, **stage)
        if _size(board) <= COMPACT_BOARD_CHARS:
            return _sorted(board)
    assert board is not None
    # Stage 3: trim battlefield lists, fullest player first, one permanent
    # at a time, until every player's battlefield list is empty. Stage 4:
    # once there is nothing left to trim there, the stack's own names,
    # oldest (bottom of stack) first - see the docstring's own numbered
    # order. `board["stack"]` alone can push a board over the cap (a long
    # stack with an empty battlefield), so this loop cannot stop at an empty
    # battlefield the way it used to.
    while _size(board) > COMPACT_BOARD_CHARS:
        fullest = max(
            board["players"].values(),
            key=lambda entry: len(entry["battlefield"]),
            default=None,
        )
        if fullest is not None and fullest["battlefield"]:
            fullest["battlefield"].pop()
            continue
        if board["stack"]:
            board["stack"].pop(0)
            continue
        break
    return _sorted(board)
