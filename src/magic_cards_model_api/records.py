"""T10: the two small pieces of policy that sit on top of the `records`
table `repository.py`/`repository_postgres.py` now own - reasoning required
of an agent seat, and "what else was legal" read off the engine's own
enumeration.

**Reasoning.** A game's agent seats are exactly `store.ai_players(game_id)` -
the same set `decks_api.start_game_from_decklists` already registers at game
creation (`register_ai_players`) and `POST /api/games`'s `NewPlayer.is_agent`
registers too (T10). This module does not invent a second "seat kind"
marker - reusing that set is what "the seat's kind is recorded at game
creation" means: nothing new is recorded, an existing fact is read.
`require_reasoning` is the gate every mutating route calls, as early as
possible (before touching the game), so an agent's un-reasoned action never
runs at all, illegal or not.

**"What else was legal."** `legal_actions_before` is a thin wrapper over
`magic_cards_model.machines.legal_actions.legal_actions` - the same function
`GET /api/games/{id}/legal-actions` (T11) calls, imported here rather than
re-implemented, per that endpoint's own module docstring ("what
`seatkit.available_actions` calls instead of brute-forcing"). This module
adds no authorization of its own and needs none: unlike the HTTP endpoint,
which must decide whether the *caller* may read another seat's hand-shaped
answer, this is called server-side, at the moment a seat's own action is
about to run, entirely inside the request that seat itself made - there is
no second party whose credentials need checking. The list this returns is
still hidden information (it can name cards in `player`'s own hand), which is
why it is written into the `records` row rather than returned to any HTTP
caller directly - `app.py`'s `GET .../log` route relies on
`visibility.redact_log_entry` to keep it out of an opponent's read of the
same log, the same way that endpoint already redacts everything else hidden.

**Guaranteeing "before, not after".** Every caller of `legal_actions_before`
in `app.py` calls it against the `Game` object `store.locked`/`store.get`
just handed back - before `commands.run_command`/`resume_command` (which
never mutate the `Game` they are given, working against their own deep copy
instead - see `commands.py`'s own module docstring) has been invoked, or,
for the handful of routes that refuse before ever building a `Command` at
all, before anything has touched the game. There is nothing to accidentally
enumerate "after" with: the object this reads is never the one a command
mutates.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from magic_cards_model.machines import Step, TurnMachine
from magic_cards_model.machines.decisions import PendingDecision
from magic_cards_model.models import CardState, Game

from .action_rows import legal_action_rows
from .audit import (
    PLAN_ACTION,
    SCRIPT_RUN_ACTION,
    SCRIPT_RUN_SLOT_ACTIONS,
    SCRIPT_RUN_STARTED_ACTION,
)
from .repository import RECENT_RECORDS_WINDOW

#: CR 103's pregame steps, which the engine models as two steps a game
#: *rests* in before CR 103.8's first turn ever begins. No turn is under way
#: in either, so there is no turn for a plan to be about - `require_plan`
#: passes straight through them, and the mulligan/opening-hand routes stay
#: gated on `reasoning` alone.
_PREGAME_STEPS = frozenset({Step.MULLIGAN, Step.PREGAME_ACTIONS})

#: The two beginning-phase steps (CR 500.4) a bare pass needs no plan for -
#: read only by `require_plan`'s `pass_shaped` exemption below. Untap grants
#: no player priority at all (CR 502.3) so it never reaches this gate in the
#: first place; upkeep and draw do, and are almost always a pass either way.
#: Every other step - both main phases, every combat step, the end step -
#: stays gated: a pass there is a real decision ("I am not casting anything,
#: not attacking, not blocking"), not the top of a turn nobody has said
#: anything about yet.
_PASS_EXEMPT_STEPS = frozenset({Step.UPKEEP, Step.DRAW})


class MissingReasoning(Exception):
    """Raised by `require_reasoning` when `player` is a registered agent seat
    (`store.ai_players`) and gave none. `app.py` maps this to an HTTP 422
    shaped like a pydantic validation error - `detail: [{"loc": [...],
    "msg": ..., "type": "missing"}]` - naming the field that was missing,
    rather than the bare 400 a `ValueError` gets everywhere else in this api:
    this is a well-formed request an agent seat is not allowed to make
    without saying why, not a malformed one."""

    def __init__(self, player: str) -> None:
        super().__init__(
            f"{player} is an agent seat, and this action needs `reasoning` - "
            "why it is doing this, in its own words"
        )
        self.player = player


def require_reasoning(
    game_id: str, player: str | None, reasoning: str | None, *, pass_shaped: bool = False
) -> None:
    """Refuse a mutating call with no `player` named, or a human seat, is
    never refused here - `reasoning` is optional for either. Only a
    registered agent seat (`store.ai_players(game_id)`) is held to it, and a
    blank or whitespace-only string counts as none given.

    `pass_shaped` exempts the one action whose reason is always the same
    sentence. Measured on the live ledger: of 149 rows, 92 carried
    reasoning, 75 of those 92 (82%) were `pass_priority`, they averaged 80
    characters, and a single string appeared 38 times because
    `seatkit.pass_until` threads one reason onto every pass a run makes. A
    pass is "I have nothing to do", and requiring an agent to compose that
    sentence again per priority buys a longer non-answer at roughly +28.6s
    per game turn, not a better one.

    **The exemption lives here, at the gate, and never in a tool or request
    signature.** `PassPriorityRequest.reasoning` is still accepted, still
    audited, and still worth filling in for the pass that actually means
    something ("holding up my counterspell"); it simply stops being
    compulsory. Moving the exemption into the schema would have taken the
    field away from the seat that had something to say.
    """
    if not player or pass_shaped:
        return
    from .store import store

    if player in store.ai_players(game_id) and not (reasoning and reasoning.strip()):
        raise MissingReasoning(player)


class MissingPlan(Exception):
    """Raised by `require_plan` when a registered agent seat tries to act on
    its own turn before declaring a plan for it. `app.py` maps this to the
    same HTTP 422 shape `MissingReasoning` gets - `detail: [{"loc": [...],
    "msg": ..., "type": "missing"}]` - naming `plan` as the field that is
    missing, and the message says where to put it, because an agent's only
    feedback is the response body."""

    def __init__(self, player: str, turn_number: int | None) -> None:
        turn = f"turn {turn_number}" if turn_number is not None else "this turn"
        super().__init__(
            f"{player} is an agent seat and has declared no plan for {turn} - "
            f"POST /api/games/{{game_id}}/plan first, saying what you read on "
            "the board, your role in this matchup, what you intend to do, and "
            "what you expect the opponent to do and why"
        )
        self.player = player
        self.turn_number = turn_number


def require_plan(
    game_id: str, player: str | None, *, game: Game | None = None, pass_shaped: bool = False
) -> None:
    """Refuse an agent seat's action on its own turn until a `plan_declared`
    record exists for that turn.

    A prediction is only evidence if it demonstrably preceded the outcome,
    and a facility nobody is obliged to use is a facility nobody uses - so
    the declaration is a precondition of acting rather than an invitation.
    Five things narrow it, each for its own reason:

    - **Only an agent seat.** Same set `require_reasoning` reads,
      `store.ai_players(game_id)`; a human seat is never held to it.
    - **Only its own turn** (`turn.active_player == player`). Responding on
      the opponent's turn - a flash spell, a pass - is not a turn to have a
      plan for, and demanding one there would refuse instant-speed play
      outright.
    - **Never in the pregame** (`_PREGAME_STEPS`). CR 103's mulligan and
      opening-hand windows happen before CR 103.8's first turn; there is no
      turn under way for a plan to be about.
    - **Never a concede.** CR 104.3a lets a player concede at any time; a
      gate that can refuse a concession can trap a seat in a game. That one
      is enforced at the call site - `app.py`'s `action_concede` simply does
      not call this.
    - **Never a pass in the beginning phase, and only with an empty stack**
      (`pass_shaped`, `_PASS_EXEMPT_STEPS` - upkeep and draw). `action_pass_
      priority` is the only caller that ever passes `pass_shaped=True`, and
      only for its own request - every other action shape, `decide` among
      them (live game 2026-09-14: the seat that triggered this gate's
      original complaint had a plan to declare, but the fix is that it
      should never have had to hold priority in its own upkeep to write one
      - see `DeclarePlanIn.for_turn`). A seat with nothing to do in the two
      steps where almost nothing ever happens should be able to say so
      without writing a plan first; a pass anywhere else, or any other kind
      of action anywhere, stays gated exactly as before. A trigger sitting on
      the stack in upkeep or draw (a landfall trigger, an upkeep cost) is a
      real decision the same way a main-phase pass is - "I am letting this
      resolve" - so the exemption only ever applies with `game.stack` empty;
      a pass with an object on the stack stays gated even in these two
      steps.

    **The lookup is bounded, and this is the part that had to be right.**
    This sits on the hot action path, so it reads at most
    `RECENT_RECORDS_WINDOW` of the game's newest rows
    (`store.recent_records`) and scans them newest-first, rather than
    loading the whole ledger with `store.records()` - which would get slower
    with every turn played. The window is safe because the answer, when it
    is yes, is always inside the current player-turn's own rows *or*, since
    `DeclarePlanIn.for_turn: "next"` exists, the seat's immediately preceding
    (the opponent's) turn - and both are, by construction, comfortably
    inside 500 rows of the newest ones for any realistic turn (`RECENT_
    RECORDS_WINDOW`'s own docstring: "a dozen steps, a priority round each,
    both seats' actions among them" - a wide margin over one turn's worth,
    and plan-ahead spans at most two). It is deliberately not an in-process
    cache either: the api may run more than one replica, and a memo of "who
    has declared" would answer differently per pod.

    The declaration is matched on `turns_taken`, not on the audit row's own
    `turn` stamp: `TurnMachine.turn_number` counts *rounds of the table*, so
    both seats' turns in one round share it, and an extra turn (CR 500.7)
    does not move it at all. `turns_taken` goes up exactly once per turn
    that begins, which is the identity a per-turn plan needs - and the one a
    plan declared ahead of time (`next_own_turn`) computes and writes in
    advance, so this matching logic itself needs no knowledge of `for_turn`
    at all: a plan-ahead row satisfies it the same way a same-turn one
    always has, the moment that turn actually arrives.
    """
    if not player:
        return
    from .store import store

    if game is None:
        game = store.get(game_id)
    if game is None or game.turn is None:
        return
    turn = game.turn
    if turn.step in _PREGAME_STEPS or turn.active_player != player:
        return
    if pass_shaped and turn.step in _PASS_EXEMPT_STEPS and not game.stack:
        return
    if player not in store.ai_players(game_id):
        return
    marker = turn.turns_taken
    for row in reversed(store.recent_records(game_id, RECENT_RECORDS_WINDOW)):
        payload = row["payload"]
        if payload.get("action") != PLAN_ACTION:
            continue
        detail = payload.get("detail") or {}
        if detail.get("player") == player and detail.get("turns_taken") == marker:
            return
    raise MissingPlan(player, turn.turn_number)


class OwnTurnInProgress(Exception):
    """Raised by `next_own_turn` when a seat asks to plan `for_turn: "next"`
    while its own turn is the one already under way. `app.py` answers 409
    with `code: own_turn_in_progress` - the same coded-409 shape `Scripts
    Disabled` already gets, so a runner branches on `code` rather than on
    this sentence.

    A plan is one declaration per turn (`require_plan`'s own matching is
    keyed on `turns_taken` alone, with no room for two rows to both claim
    the same turn), and "next" only means something looking forward from
    the opponent's turn - `"current"` (the default) is how a seat already in
    its own turn declares for it."""

    code = "own_turn_in_progress"

    def __init__(self, player: str) -> None:
        super().__init__(
            f'{player}\'s own turn is already under way - `for_turn: "next"` is for '
            'planning ahead, during the opponent\'s turn; use `for_turn: "current"` '
            "(the default) to declare for the turn already in front of you"
        )
        self.player = player


def next_own_turn(game: Game, player: str) -> TurnMachine:
    """`player`'s next own turn, as a fully-formed `TurnMachine` - never
    read, only computed, so `app.declare_plan` can stamp a plan-ahead row
    with the `turns_taken` (and `turn_number`) that turn will actually carry
    once it arrives.

    Computed by deep-copying `game.turn` and calling its own `begin_next_
    turn` (CR 500.1, 800.4) - the engine's real turn-order method, repeated
    until `player` comes up active - rather than re-implementing seating
    order, departed players, or CR 500.7's extra turns here. An extra turn
    queued for someone else is simply another `begin_next_turn` call that
    does not land on `player`, so it is skipped over exactly the way a real
    game would skip it (`test_for_turn_next_skips_a_queued_extra_turn`).

    Bounded at one full round of the table plus every extra turn presently
    queued - the most `begin_next_turn` could ever need to reach every seat
    at least once, extra turns included, starting from any seat's turn.
    Raises `ValueError` if that bound is somehow not enough, which would
    mean `player` is not seated in this game at all.

    Raises `OwnTurnInProgress` when `player`'s own turn is the one under
    way right now (outside the pregame): "next" is only sensible looking
    forward from someone else's turn.
    """
    turn = game.turn
    if turn is None:
        raise ValueError("no turn is under way to plan ahead of")
    if turn.step not in _PREGAME_STEPS and turn.active_player == player:
        raise OwnTurnInProgress(player)
    forecast = turn.model_copy(deep=True)
    if forecast.step in _PREGAME_STEPS:
        # No turn has begun yet at all (CR 103's mulligan/opening-hand
        # windows) - CR 103.8's first turn is seat 0's, by construction the
        # only turn `begin_next_turn` itself is not the right call for.
        forecast.begin_first_turn()
        if forecast.active_player == player:
            return forecast
    bound = len(forecast.players) + len(forecast.extra_turns) + 1
    for _ in range(bound):
        forecast.begin_next_turn()
        if forecast.active_player == player:
            return forecast
    raise ValueError(f"{player} is not seated in this game - no next turn of theirs exists")


#: Whether the request being served named a ledger position
#: (`expected_seq`) and it was the one the game was actually on. Set by
#: `require_current_board`, read by `named_a_current_board` - and by nothing
#: else.
#:
#: **A ContextVar rather than a parameter threaded through every route, and
#: that is the point.** `stale` has to reach a refusal row wherever one is
#: written, and this api writes them in six separate places:
#: `app.audit_outcome` (every route that reaches `_respond`),
#: `_answer_declaration`'s local `refuse()` (shared by `actions/attack` and
#: `actions/declare-blockers`),
#: `decide`'s own `refuse()`, `action_pass_priority`'s `except` arm,
#: `advance`'s `except` arm, and `opening._overruled`. Threading an argument
#: into each is how the first version of this shipped with `stale: False`
#: written from nowhere at all: the flag existed, the report had a bucket for
#: it, and the bucket was structurally always zero (MEMORY.md 2026-09-12).
#: One write site at the guard and one read site in `audit.record` cannot
#: drift that way - a route that records a refusal gets the stamp by
#: construction, including routes nobody has written yet.
#:
#: **Request isolation is pinned by a test rather than assumed**
#: (`test_expected_seq.py::test_the_flag_does_not_leak_from_one_request_into_
#: the_next`): a request that named a current board is followed immediately,
#: on the same client, by one that named none, and the second row must carry
#: no `stale` key. That ordering is the test - a leak is invisible when the
#: un-flagged call happens to go first, which is how every other case here
#: is shaped.
_NAMED_A_CURRENT_BOARD: ContextVar[bool] = ContextVar("named_a_current_board", default=False)


def named_a_current_board() -> bool:
    """Whether this request named a ledger position that turned out to be
    current - which is exactly what makes a refusal it goes on to record a
    *rules* refusal rather than an unknown one.

    Read by `audit.record` alone. The three states a refusal row can carry,
    and what a reader of the ledger may conclude from each:

    - `stale: True` - the board had moved; the engine was never asked.
    - `stale: False` - the seat named the board it decided against, that
      board was current, and the engine judged the action illegal on it.
      Necessary for a rules error but not sufficient: `offered_action` is
      the other half, and only a refusal of an action the api had *not*
      offered is counted as one (`scripts/eval_report.py`'s `tally.rules`).
    - no `stale` key - the caller named no board position at all (the
      browser, and every caller written before `expected_seq` existed), so
      nothing can be said about which of the two it was
      (`tally.unknown`). This third state stays honest deliberately:
      collapsing it into `False` would make `stale_split` report
      `"measured"` over data that measured nothing.
    """
    return _NAMED_A_CURRENT_BOARD.get()


#: Whether the action this request is running was one this api's own
#: enumeration offered for this seat, on this very board - `None` for a
#: request whose shape cannot be looked for in an enumeration at all (see
#: `OFFERED_KINDS`). Set by `note_offered_action`, read by
#: `offered_action` - and, exactly like `_NAMED_A_CURRENT_BOARD` above, by
#: nothing else.
#:
#: **A ContextVar for the same reason that one is.** The fact has to reach a
#: refusal row wherever one is written, and only one place in this api holds
#: both halves of it: `app._run_command`, which builds the `Command` from the
#: request *and* computes `legal_actions_before` from the game, under the
#: same lock, before the engine is asked anything. Threading it out of there
#: into six recording sites is how the `stale` flag first shipped written
#: from nowhere at all (MEMORY.md 2026-09-12). One write site and one read
#: site in `audit.record` cannot drift that way.
_OFFERED: ContextVar[bool | None] = ContextVar("offered_action", default=None)

#: Which enumerated `models.action.ActionKind` values each `commands.
#: COMMAND_HANDLERS` kind could have come out of, and nothing else.
#:
#: A positive list, and short on purpose: a command kind that is absent makes
#: no claim either way (`offered_action` answers `None`, `audit.record`
#: stamps nothing, and `scripts/eval_report.py` counts the row as unchecked
#: rather than as a rules error). The ones left out are left out for a
#: reason, not for lack of effort:
#:
#: - `advance`, `resolve_top`, `state_based_actions`, `pregame_settle` -
#:   there is no `ActionKind` for them; the enumeration does not offer them
#:   as rows at all.
#: - `crew` and `cycle` - both reach the engine through an activated ability
#:   (`ACTIVATE`, `ACTIVATE_OFF_ZONE`), but neither request carries the
#:   `ability_index` that says *which*, so a match on the card alone could
#:   claim an offer that was for a different ability of the same card.
#: - the two combat declarations and `decide`, which do not come through
#:   `_run_command` at all: a declaration is one answer built out of many
#:   enumerated rows (CR 508.1a/509.1a are checked over the whole of it), so
#:   "was this offered" is not a question the enumeration can answer about it.
OFFERED_KINDS: dict[str, frozenset[str]] = {
    "cast": frozenset({"cast"}),
    "play_land": frozenset({"play_land"}),
    "activate_ability": frozenset({"activate", "activate_mana", "activate_off_zone"}),
    "suspend": frozenset({"suspend"}),
    "plot": frozenset({"plot"}),
    "foretell": frozenset({"foretell"}),
    "pass_priority": frozenset({"pass_priority"}),
    "concede": frozenset({"concede"}),
    "equip": frozenset({"equip"}),
}

#: The two kinds that name no object. Everything else is an action taken with
#: a card, and a request that does not say which card (`card`, a printed
#: name, rather than `card_id`) is not something an enumeration of ids can be
#: searched for - that answers `None` rather than guessing.
_OBJECTLESS_KINDS = frozenset({"pass_priority", "concede"})

#: Announcement fields where the request's `None` is itself the answer, so
#: they are compared exactly. "No face named" is the plain printed cast
#: (CR 709.3/715.3), and the enumeration's plain row carries `None` for it
#: too - which is precisely how a face the enumeration never offered is kept
#: from matching the plain row it sits beside.
_EXACT_FIELDS = ("spell_face", "keyword", "alternative_cost")

#: Kinds whose request announces none of `_EXACT_FIELDS`: `EquipRequest` has
#: no face, keyword or alternative cost to name, while its row carries
#: `keyword: Equip` - the keyword its own route already is.
_NO_ANNOUNCEMENT_KINDS = frozenset({"equip"})

#: Fields the request may leave for the server to work out (which zone the
#: card is in, which of an ability's indices) - compared only when the
#: request actually names one.
_GIVEN_FIELDS = ("from_zone", "ability_index")


#: The board an action was taken on - `(game_id, turn number, step)` - for the
#: row this request records (`audit.record`'s `turn`/`step`/`phase`).
#:
#: A ContextVar for the reason `_OFFERED` is one. `audit.record` used to read
#: the store *after* the action had committed, so a pass that ended the
#: upkeep was labelled `draw`, and an attack declaration whose resume paused
#: again was labelled with the step before it (battle 3, B3-11). The label is
#: the step the action was taken in: the committed board a fresh command ran
#: against, or the paused board the answered question was asked on.
_ACTION_BOARD: ContextVar[tuple[str, int | None, Step | None] | None] = ContextVar(
    "action_board", default=None
)


def note_action_board(game_id: str, game: Game) -> None:
    """Remember `game` as the board this request's action is taken on."""
    turn = game.turn
    _ACTION_BOARD.set((game_id, turn.turn_number if turn else None, turn.step if turn else None))


def action_board(game_id: str) -> tuple[int | None, Step | None] | None:
    """`(turn number, step)` of the board `note_action_board` named for this
    game in this request, or `None` when nothing was noted."""
    noted = _ACTION_BOARD.get()
    if noted is None or noted[0] != game_id:
        return None
    return noted[1], noted[2]


def note_offered_action(kind: str, payload: Any, legal_before: LegalActionsBefore | None) -> None:
    """Work out, once, whether `payload`'s action is one `legal_before`
    offered - and remember it for whatever row this request goes on to
    record.

    Called from `app._run_command` before the engine runs, because that is
    the one place holding the request and the enumeration for the same board
    at the same moment.
    """
    _OFFERED.set(_was_offered(kind, payload, legal_before))


def offered_action() -> bool | None:
    """Whether the action this request is running was in this api's own
    enumeration for this seat and this board.

    Read by `audit.record` alone, and the three states a refusal row can
    carry are three different facts about who is at fault:

    - `offered: True` - the api listed this action as legal for this seat, on
      the board it was then refused against. Either the api contradicted its
      own offer (the Adventure face, CR 715.3, was a measured instance: the
      enumeration offered the row, posting it back verbatim was refused) or
      the seat mis-announced something it was genuinely offered - a target,
      a mode, an X. The ledger cannot tell those two apart, so neither is
      counted as a rules error.
    - `offered: False` - no row in the enumeration named this action at all.
      The seat asked for something that was never on offer, which is the one
      refusal that is unambiguously its own doing
      (`scripts/eval_report.py`'s `tally.rules`).
    - no `offered` key - this action's shape cannot be looked for in an
      enumeration (`OFFERED_KINDS`), or the row was recorded by a route that
      never built a command. Kept as its own third state for the same reason
      `named_a_current_board` keeps one: a missing check is not evidence of
      a defect, and the report says which of its rows it could not check.
    """
    return _OFFERED.get()


def _was_offered(kind: str, payload: Any, legal_before: LegalActionsBefore | None) -> bool | None:
    kinds = OFFERED_KINDS.get(kind)
    if kinds is None or legal_before is None:
        return None
    if not isinstance(payload, dict):
        return None
    needs_object = kind not in _OBJECTLESS_KINDS
    card_id = payload.get("card_id")
    if needs_object and not card_id:
        return None
    for row in legal_before.actions:
        if str(row.get("kind")) not in kinds:
            continue
        if needs_object and str(row.get("card_id") or "") != str(card_id):
            continue
        if kind not in _NO_ANNOUNCEMENT_KINDS and any(
            _same(row.get(name)) != _same(payload.get(name)) for name in _EXACT_FIELDS
        ):
            continue
        if any(
            payload.get(name) is not None and _same(row.get(name)) != _same(payload.get(name))
            for name in _GIVEN_FIELDS
        ):
            continue
        return True
    return False


def _same(value: Any) -> str | None:
    """One enumerated field and one request field, comparable: the row has
    been through `model_dump(mode="json")` and the request has not, so a
    `Zone` enum on one side meets its own `"hand"` on the other."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


class BoardMoved(Exception):
    """Raised by `require_current_board` when a seat names a ledger position
    (`expected_seq`) this game has already moved past.

    **The board this seat decided against is gone, and nothing was done
    about it.** That is a materially different thing from the engine
    refusing a move on the rules, and until this existed the two arrived
    looking identical: a seat that read the board, thought, and posted an
    action the opponent had already invalidated got back `claude does not
    have priority, you does` or `no pending decision for you` - a symptom of
    the race, phrased as if the seat had misread the rules. A corpus of
    those refusals cannot say how much of an agent's refusal rate is play
    quality and how much is latency, which is the single most important
    question about agent play there is.

    `app.py`'s registered handler answers 409 - the same status
    `VersionConflict`, `GameUnloadable` and `CommandDivergenceError` already
    take for "the committed state and what you were working from disagree" -
    with `stale: true` on the body and the ledger rows since `expected_seq`
    attached, so the caller sees what moved without a second read.

    **The `stale` boolean is the discriminator, never the message.** The
    text below says what happened, in the engine-adjacent plain style the
    rest of this api refuses in; it is for a person. A reader of the
    ledger separating races from rules errors reads `detail["stale"]`, and
    is never asked to pattern-match prose - engine wording is not a client
    contract (MEMORY.md 2026-09-08, "a refused pass is two different
    things").
    """

    def __init__(self, game_id: str, expected_seq: int, current_seq: int) -> None:
        super().__init__(
            f"the board moved: you decided against this game as it stood at log "
            f"entry #{expected_seq}, and it has since moved on to #{current_seq}. "
            "Nothing was done. Read what changed - the `since` entries on this "
            "answer are exactly that - and decide again against the board as it "
            "stands now."
        )
        self.game_id = game_id
        self.expected_seq = expected_seq
        self.current_seq = current_seq


def require_current_board(
    game_id: str,
    action: str,
    expected_seq: int | None,
    *,
    player: str | None = None,
    reasoning: str | None = None,
    dry_run: bool = False,
) -> None:
    """Refuse `action` outright when `expected_seq` names a ledger position
    this game has already moved past - `BoardMoved`, before the engine has
    been asked anything at all.

    Called first in every mutating route, ahead of `require_reasoning` and
    `require_plan`: a seat that lost a race is owed the news that it lost
    one, not a lecture about the plan it has not declared for a turn that
    may no longer be its own. And ahead of the engine by construction,
    which is the whole guarantee - a stale call must change nothing, so it
    must never reach `commands.run_command`.

    `expected_seq is None` returns immediately. That is the entire opt-out,
    and it is what keeps every existing caller - the browser included -
    behaving exactly as it did (`schemas.BoardRead`).

    A game with no ledger at all (`last_seq` of 0) returns immediately too.
    Every live game has at least its own `create_game` row, so a zero here
    means the game is not there - and that is a 404 the route itself is
    about to raise, with far more use to the caller than "the board moved
    from #7 to #0" about a game that never existed.

    **The refusal is recorded, once, as this action's own row**, with
    `allowed: False` and the three keys that make it readable without
    heuristics: `stale: True`, `moved_from` (what the seat named) and
    `moved_to` (where the ledger actually is). `reason` carries the sentence
    too, for the console line and for a person, but nothing machine-readable
    depends on it.

    No `legal_actions_before` rides on that row, deliberately: the
    enumeration would be "what is legal on the board as it stands now",
    while every other row's is "what was legal on the board this action was
    decided against" - and the seat has just been told that board is gone.
    Writing the current one under the same key would quietly change what the
    key means for exactly the rows a race analysis reads most.

    **A matching seq is recorded too, not just a mismatching one.** The
    match sets `_NAMED_A_CURRENT_BOARD`, which is what lets `audit.record`
    stamp `stale: False` on whatever this request goes on to refuse - "the
    engine ruled on a current board" is the other half of the split, and
    without it every genuine rules refusal is indistinguishable from an
    unmeasured one. See `named_a_current_board`.

    `dry_run` refuses the same way and writes nothing. A dry run is a probe
    that changes nothing and is deliberately unlogged (`app.audit_outcome` -
    one traced game's ledger grew to 2410 entries with 956 of them probe
    noise); a probe naming a board that has moved must not be the thing that
    starts logging them.
    """
    if expected_seq is None:
        return
    from . import audit

    current = audit.last_seq(game_id)
    if current == expected_seq:
        # The seat named a board position and it is the one the game is
        # actually on. Anything this request goes on to refuse is therefore a
        # refusal against a *current* board, and `audit.record` stamps it
        # `stale: False` - see `named_a_current_board` below.
        _NAMED_A_CURRENT_BOARD.set(True)
        return
    if current == 0:
        # Not there at all: the 404 the route is about to raise. Deliberately
        # NOT stamped - nothing about this request's board position was ever
        # confirmed, and there is no game for a refusal row to belong to.
        return
    moved = BoardMoved(game_id, expected_seq, current)
    if not dry_run:
        audit.record(
            game_id,
            action,
            player=player,
            allowed=False,
            reason=str(moved),
            stale=True,
            moved_from=expected_seq,
            moved_to=current,
            reasoning=reasoning,
        )
    raise moved


# -- play scripts ------------------------------------------------------------
#
# A seat Job registers the scripts it runs (`PUT .../seats/{uid}/scripts`) and
# reports each run (`POST .../script-runs`). Neither is a game action, so none
# of the gates above apply; what applies is below - the operator's kill
# switch, the schema every script must satisfy, and "a script runs once per
# turn and step", read back out of the ledger.

#: The operator's kill switch for agent-authored scripts,
#: published on the API Deployment with a default of `"true"`
#: (`deploy/k8s/50-deployment.yaml`, `deploy/k8s/README.md`).
SCRIPTS_ENABLED_ENV = "SCRIPTS_ENABLED"

#: The spellings that switch scripts off. Unset, or anything else, leaves
#: them on - the default the Deployment publishes.
_SWITCHED_OFF = frozenset({"false", "0", "no", "off"})


def scripts_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether agent-authored scripts are switched on. Read per call rather
    than at import: flipping the switch restarts the pod anyway, and a test
    can then set it for one request."""
    env = os.environ if env is None else env
    return env.get(SCRIPTS_ENABLED_ENV, "true").strip().lower() not in _SWITCHED_OFF


class ScriptsDisabled(Exception):
    """Raised by `require_scripts_enabled` while the kill switch is off.
    `app.py` answers 409 with `code: scripts_disabled` - a typed refusal, so
    the runner branches on the code and never on this sentence."""

    code = "scripts_disabled"

    def __init__(self, what: str) -> None:
        super().__init__(
            f"{what} was refused: agent-authored scripts are switched off on this "
            f"deployment ({SCRIPTS_ENABLED_ENV}=false). Play the seat without scripts."
        )


def require_scripts_enabled(what: str) -> None:
    """Refuse `what` - registering a corpus, starting a run - while the kill
    switch is off. A `finish` is deliberately not refused: a run already under
    way when the switch flipped still gets the row saying what it did."""
    if not scripts_enabled():
        raise ScriptsDisabled(what)


class InvalidScripts(Exception):
    """Raised when a registered corpus holds a script `playscripts.schema`
    refuses. `app.py` answers 422 with `code: invalid_scripts` and `errors`,
    one `PlaysError` dict per refused shape, each naming its script as
    `scripts[<index>]`. Nothing is stored: a corpus is registered whole or not
    at all."""

    code = "invalid_scripts"

    def __init__(self, errors: list[dict[str, str]]) -> None:
        listed = "; ".join(
            f"{error['path']} {error['code']}: {error['message']}" for error in errors
        )
        super().__init__(f"the scripts were not registered - {listed}")
        self.errors = errors


def script_errors(scripts: list[Any]) -> list[dict[str, str]]:
    """Every refusal of a corpus, as `PlaysError` dicts: each script's own
    `play_errors`, plus a second script reusing an earlier one's `id` - a run
    is reported by `script_id`, so two scripts under one id would make every
    run of either unattributable."""
    from .playscripts.schema import PlaysError, play_errors

    errors: list[PlaysError] = []
    first_seen: dict[str, int] = {}
    for index, script in enumerate(scripts):
        path = f"scripts[{index}]"
        errors.extend(play_errors(script, path))
        script_id = script.get("id") if isinstance(script, dict) else None
        if not isinstance(script_id, str):
            continue
        if script_id in first_seen:
            errors.append(
                PlaysError(
                    "malformed-play",
                    path,
                    f"id: {script_id!r} is already the id of scripts[{first_seen[script_id]}]",
                )
            )
        else:
            first_seen[script_id] = index
    return [error.to_dict() for error in errors]


def inputs_errors(inputs: list[Any]) -> list[dict[str, str]]:
    """Every refusal of a corpus's declared inputs, as `malformed-inputs`
    `PlaysError` dicts at path `inputs` - the `inputs.json` shape
    `playscripts.inputs` shares with mtg-decks M1b, judged on the list the
    file carries."""
    from .playscripts.inputs import SCHEMA_VERSION, inputs_problems
    from .playscripts.schema import PlaysError

    problems = inputs_problems({"schema_version": SCHEMA_VERSION, "inputs": inputs})
    return [PlaysError("malformed-inputs", "inputs", problem).to_dict() for problem in problems]


class UnknownScript(Exception):
    """Raised by `require_registered_script` when a `start` names a script no
    corpus registered under the uid that runs it holds. `app.py` answers 404
    with `code: unknown_script` and the `script_id`, and nothing is recorded:
    the ledger holds runs of scripts somebody declared, never of a name."""

    code = "unknown_script"

    def __init__(self, detail: dict[str, Any]) -> None:
        script_id = detail["script_id"]
        super().__init__(
            f"no script {script_id!r} is registered for {detail['seat']}'s {detail['role']} - "
            "a run starts a script from the corpus registered under the uid that runs it "
            "(PUT .../seats/{uid}/scripts), so this start did nothing"
        )
        self.script_id = script_id


def require_registered_script(game_id: str, uids: list[str], detail: dict[str, Any]) -> None:
    """Refuse a start of `detail['script_id']` unless the corpus registered
    under one of `uids` holds it. `uids` follows the role separation the
    route already enforces: a seat's run is checked against its seat uid's
    corpus and an advisor's against its advisor uid's, never the other's."""
    from .store import store

    for uid in uids:
        corpus = store.scripts(game_id, uid) or {}
        if any(
            isinstance(script, dict) and script.get("id") == detail["script_id"]
            for script in corpus.get("scripts") or []
        ):
            return
    raise UnknownScript(detail)


class ScriptAlreadyRan(Exception):
    """Raised by `record_script_run_start` when this seat already started or
    finished this script at this turn and step. `app.py` answers 409 with
    `exit: already_ran`, and nothing is recorded."""

    exit = "already_ran"

    def __init__(self, detail: dict[str, Any], prior_run_id: str) -> None:
        super().__init__(
            f"{detail['seat']} already ran {detail['script_id']} at turn {detail['turn']}, "
            f"{detail['step']} (run {prior_run_id}) - a script runs once per turn and step, "
            "so this start did nothing"
        )
        self.prior_run_id = prior_run_id


#: What makes two runs "the same run" for `already_ran`. `role` scopes the
#: once-per-turn-and-step slot per uid kind (B7-08): an advisor's advise run
#: proposes a move without taking it, so it must not consume the seat's own
#: slot for the same script, turn and step, and vice versa. Two advise runs
#: by the same advisor still share a key (same seat, same role) and collide.
_SCRIPT_RUN_KEY = ("seat", "role", "script_id", "turn", "step")

#: Serialises the read-then-append of a start within this process. Two
#: replicas can still race one start past each other; the ledger then holds
#: both rows and the second is visible to every later start.
_SCRIPT_RUN_LOCK = threading.Lock()


def _prior_script_run(game_id: str, detail: dict[str, Any]) -> str | None:
    """The `run_id` of an earlier start or finish sharing `detail`'s key, or
    `None`. Bounded to `RECENT_RECORDS_WINDOW`, like `require_plan`: a run at
    the current turn and step is by construction among the newest rows.

    B8-04: a finished run that made zero moves (`moves_done: 0`) touched
    nothing on the board - refused before its first move, `not_applicable`,
    `ambiguous` before sending, and the like - so it never consumed the
    once-per-turn-and-step slot and does not block a retry, consistent with
    that same finish's own `reapplicable_after: true`. A bare `start` with no
    matching `finish` yet (still in flight, or a crash) still blocks: this
    check cannot tell whether it moved anything. The release covers that run's
    own start row too (newest first, so its finish is always read before it).

    A finish row whose own `moves_done` is missing entirely (never posted, or
    stripped by an older writer) is never released either (S2 arming
    re-review finding 3): read as `prior.get("moves_done")`, unclamped - a
    bare `or 0` here once treated "the field was never posted" the same as
    "confirmed zero moves", releasing a slot a genuine, still-untold run might
    still be sitting on. `releases_slot`'s own `moves_done=None` handles it.

    The common decisions here are `playscripts.script_tools.releases_slot` -
    imported lazily, like every other cross-module read this function already
    does, rather than reimplemented: this ledger row and the hint's own
    in-process cache (`script_tools._record_run`) must apply the same rule on
    everything they share, or a run one calls "touched nothing" and the other
    calls "ran" drifts them apart - which is exactly what happened here once
    before (S2 arming re-review finding 4): this function's own inline copy
    of the rule kept excluding `already_ran` from release long after
    `script_tools._record_run`'s copy already did, so a finish whose own
    `exit` was `already_ran` (a fresh start colliding with an existing lock)
    released the slot here while staying locked in the hint - a caller that
    trusted the hint's "still available" and retried got refused all over
    again by this very function, on the very same board.

    `already_ran_releases=True` here, and only here (S2 arming re-review
    finding 3): once THAT fix landed, this function swung the other way and
    stopped releasing `already_ran` rows at all, which is its own separate
    bug - a `already_ran` row only ever records "some other run held this
    slot when I tried", never a genuine attempt of its own, so once the run
    it collided with is itself released, nothing here should still block a
    fresh start on the strength of a row that never did anything either.
    Concretely: start A, start B collides (`already_ran`), B's own finish
    posts `exit: already_ran, moves_done: 0`, A's own finish posts zero
    moves too (released on its own terms) - a fresh start C must succeed,
    not collide with B's collision row all over again. The hint's own cache
    (`_record_run`) keeps the narrower, still-locking rule
    (`already_ran_releases`'s own default `False`): it exists only to avoid
    re-suggesting, to this one process, a script that already collided at
    this exact position, regardless of what the ledger does with other rows
    later.

    `chained=False` always: a chain's own moves-under-reported-by-the-
    trailing-exit problem (`releases_slot`'s own `chained` parameter) is an
    artifact of the SINGLE aggregated `exit_block` `script_tools.run()`
    hands back for a whole chain - every ledger row here is one chain
    SEGMENT's own finish, posted with that segment's own accurate
    `moves_done` (`playscripts/runtime.py`'s `_end`, called once per
    segment), so no row here is ever itself "a chain" the way the hint's
    aggregate view can be."""
    from .playscripts.script_tools import releases_slot
    from .store import store

    key = tuple(detail.get(name) for name in _SCRIPT_RUN_KEY)
    released: set[Any] = set()
    for row in reversed(store.recent_records(game_id, RECENT_RECORDS_WINDOW)):
        payload = row["payload"]
        action = payload.get("action")
        if action not in SCRIPT_RUN_SLOT_ACTIONS:
            continue
        prior = payload.get("detail") or {}
        if tuple(prior.get(name) for name in _SCRIPT_RUN_KEY) != key:
            continue
        run_id = prior.get("run_id")
        if run_id in released:
            continue
        if action == SCRIPT_RUN_ACTION and releases_slot(
            prior.get("moves_done"),
            prior.get("exit"),
            advise=prior.get("advise") is True,
            chained=False,
            already_ran_releases=True,
        ):
            released.add(run_id)
            continue
        return str(run_id)
    return None


def record_script_run_start(game_id: str, detail: dict[str, Any]) -> int:
    """Write a `script_run_started` row for `detail` and return its `seq`, or
    raise `ScriptAlreadyRan`. The row names `seat` as its `player`, which is
    what keeps it out of every other viewer's log (`visibility.
    redact_log_entry`)."""
    from . import audit

    with _SCRIPT_RUN_LOCK:
        prior = _prior_script_run(game_id, detail)
        if prior is not None:
            raise ScriptAlreadyRan(detail, prior)
        return audit.record(game_id, SCRIPT_RUN_STARTED_ACTION, player=detail["seat"], **detail)


def record_script_run_finish(game_id: str, detail: dict[str, Any]) -> int:
    """Write the one `script_run` row for a finished run and return its
    `seq`. Under the same lock as a start, so a start never reads the ledger
    between a finish's check-free append and its own."""
    from . import audit

    with _SCRIPT_RUN_LOCK:
        return audit.record(game_id, SCRIPT_RUN_ACTION, player=detail["seat"], **detail)


def record_script_run_offer(game_id: str, detail: dict[str, Any]) -> int:
    """Write one `script_run_offered` row for `detail` and return its `seq`.

    Unlike `record_script_run_start`, this never checks `_prior_script_run`
    and never raises `ScriptAlreadyRan`: an offer is not a run, it does not
    hold the once-per-turn-and-step slot, and it must not collide with (or
    be collided into by) one. Still taken under `_SCRIPT_RUN_LOCK`, like the
    other two, so it is never interleaved with a start's own
    check-then-append."""
    from . import audit

    with _SCRIPT_RUN_LOCK:
        return audit.record(
            game_id, audit.SCRIPT_RUN_OFFERED_ACTION, player=detail["seat"], **detail
        )


@dataclass(frozen=True)
class LegalActionsBefore:
    """An enumeration and the seat it belongs to, inseparably.

    This pairing is the whole point of the type. The enumeration names
    cards in `player`'s own hand, so exactly one seat may ever read it back
    out of the log - and the first version of this feature decided *which*
    seat by looking at the audit entry's `detail["player"]`, a field the
    caller fills in and nothing validates. On `actions/declare-blockers`
    the two came apart: the enumeration was computed for the deciding seat
    while `detail["player"]` was whatever the request body said, so naming
    yourself in a refused request handed you the opponent's own answer.

    Carrying the subject alongside the actions makes that failure
    unexpressible: `audit.record` writes both keys from this one object or
    neither, and `visibility.redact_log_entry` reads the subject rather
    than anything the request supplied.
    """

    player: str
    actions: list[dict[str, Any]]


def legal_actions_before(
    game: Game, player: str | None, pending: PendingDecision | None
) -> LegalActionsBefore | None:
    """`player`'s full legal-action enumeration against `game` as it stands
    right now, paired with `player` itself - `None` when there is no clear
    actor to enumerate for (a system action with nobody named). Each
    `Action` is `model_dump(mode="json")`-ed, the same JSON-safe shape
    `GET .../legal-actions` already hands a caller, so it round-trips through
    the `records` table's JSONB `payload` column with no second codec.

    `player` must be the seat the server itself resolved for this action,
    never one read straight off a request body that a uid gate has not yet
    agreed with - see `LegalActionsBefore`."""
    if not player:
        return None
    _, actions = legal_action_rows(game, player, pending)
    return LegalActionsBefore(player=player, actions=actions)


# -- what a proposal actually says -------------------------------------------
#
# `POST /api/games/{id}/advice` records what an advisor thinks its human
# should do. The *body* of that proposal is machine-shaped - card ids, mana
# source ids, target tokens - which is exactly right for the browser that
# will post it verbatim, and useless to the human deciding whether to.
#
# The one-line rendering below is therefore derived **here, server-side, from
# the body and the live game**, never taken from the advisor. An agent-supplied
# summary is a second description of the same action that nothing keeps honest:
# it can name a different card from the one the body would actually cast, and a
# human reading only the summary would approve something else entirely. A
# server-derived one cannot disagree with the body it describes, because it is
# computed from it.

#: The request-body keys that carry a *cost* being paid rather than the thing
#: being done, with the preposition each one reads naturally with. Every one
#: of them is a list of card ids (`CastRequest.sacrificing`/`.discarding`/
#: `.exiling`/`.choosing`/`.tapping`, `ActivateRequest`'s same four,
#: `CrewRequest.crewing`).
_COST_KEYS = (
    ("crewing", "crewing with"),
    ("sacrificing", "sacrificing"),
    ("discarding", "discarding"),
    ("exiling", "exiling"),
    ("choosing", "choosing"),
    ("tapping", "tapping"),
)


def _named(game: Game, token: Any) -> str:
    """`token` as a human would read it: a card id resolved to the card's own
    name, a player's username left alone, anything else stringified.

    Resolution is best effort on purpose. A proposal is recorded against the
    board as it stood when the advisor read it, and this runs a moment later:
    a card that has since moved zones (or a body naming an id this game never
    had) must still produce a readable line rather than raise and lose the
    whole row."""
    text = str(token)
    try:
        card = game.find_by_id(UUID(text))
    except Exception:
        card = None
    if card is not None:
        return card.card.name
    if any(player.username == text for player in game.players):
        return text
    return text


def _names(game: Game, tokens: Any) -> list[str]:
    return [_named(game, token) for token in tokens] if isinstance(tokens, list) else []


def _path_tail(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _action_name(path: str) -> str:
    """The action a proposal's `path` names, in words - `".../actions/play-
    land"` reads `"play land"`. Derived from the path rather than taken as a
    field, for the same reason the summary as a whole is."""
    return _path_tail(path).replace("-", " ") or "act"


#: The one action path whose tail is not its own `models.action.ActionKind`'s
#: name. Every other path matches by name once the hyphen is normalised
#: (`actions/cast` -> `cast`, `actions/play-land` -> `play_land`), and
#: `actions/activate` matches `activate`, `activate_mana` and
#: `activate_off_zone` alike by prefix - one permanent's mana ability and its
#: other activated ability are both reachable through that one route, and
#: `ability_index` below is what tells them apart.
#:
#: `actions/crew` needs no entry and gets none: `legal_actions` does not
#: enumerate crew at all (CR 702.122a's ability has no `{T}` of its own and is
#: not one of the Vehicle's `activated_abilities`), and its cost is the
#: creatures tapped for it, which `_COST_KEYS`'s `crewing` already names in
#: full. An entry that matched nothing would only make the printed-cost
#: fallback fire on a cost the Vehicle's printing has nothing to do with.
_PATH_KINDS: dict[str, tuple[str, ...]] = {
    # CR 702.29b: cycling "can be activated only if the card is in a player's
    # hand", so the engine enumerates it as an off-zone activation while the
    # api gives it a route of its own.
    "cycle": ("activate_off_zone",),
}

#: The only path whose cost the card's *printed* mana cost can stand in for,
#: which is what `_printed_cost` is allowed to do. CR 601.2f on a cast's total
#: cost: "Usually this is just the mana cost." Nothing else is: an
#: activation's cost is its ability's, a crew's is the creatures, a special
#: action's is that action's own - quoting a printing there would name a
#: number the proposal never pays.
_PRINTED_COST_PATHS = frozenset({"cast"})

#: The body fields that tell two otherwise identical actions on the same card
#: apart - which ability of a permanent, which cast option, out of which zone.
#: Named identically on the request shapes (`schemas.ActivateRequest.
#: ability_index`, `CastRequest.keyword`/`.from_zone`/`.alternative_cost`) and
#: on `models.action.Action`, so one list serves both sides of the comparison.
_DISCRIMINATING_FIELDS = ("ability_index", "keyword", "from_zone", "alternative_cost")


def _kind_matches(kind: str, path: str) -> bool:
    """Whether an `Action` of `kind` could be what the route at `path` takes -
    see `_PATH_KINDS` for the two paths that do not simply spell their own
    kind."""
    tail = _path_tail(path).replace("-", "_")
    if tail in _PATH_KINDS:
        return kind in _PATH_KINDS[tail]
    return kind == tail or kind.startswith(f"{tail}_")


def _agreement(action: dict[str, Any], body: dict[str, Any]) -> int | None:
    """How well `action` (one `legal_actions` row, `model_dump`-ed) matches
    what `body` asked for: a count of the discriminating fields that actually
    agreed, or `None` when one of them outright contradicts - which rules the
    action out rather than ranking it lower.

    A count rather than a boolean because a body is allowed to be silent: a
    cast that names no `keyword` is the printed-mana-cost cast, and scoring
    "both absent" as agreement is what makes it beat the evoke row for the
    same card without having to declare that the silence *means* `None`."""
    agreed = 0
    for field in _DISCRIMINATING_FIELDS:
        want, have = body.get(field), action.get(field)
        if field == "alternative_cost" and isinstance(want, int):
            # An index into `GET .../affordances/{card_id}`'s own list
            # (`CastRequest.alternative_cost` takes either), which names
            # nothing this row carries - no information, not a contradiction.
            continue
        if want is None:
            agreed += 1 if have is None else 0
            continue
        if have is None or str(have).casefold() != str(want).casefold():
            return None
        agreed += 1
    return agreed


def _card_reference(body: dict[str, Any]) -> tuple[str, bool] | None:
    """Which card this proposal is about, and whether it said so by name -
    `None` when it names no card at all (`decide`, `pass-priority`, a whole
    attack declaration).

    Every action shape takes both spellings (`schemas.CastRequest.card_id`
    and `.card`, "used when no card_id is given: first match in from_zone";
    `CycleRequest`, `SpecialActionRequest` and `PlayLandRequest` the same),
    and the by-name one is not the exotic path: `mcp.py`'s `cast`, `cycle`
    and `play_land` send `card` for every argument that is not a uuid, which
    is what a model writing "Lightning Bolt" produces. So both spellings
    have to reach the cost lookup, or the documented path is the one that
    renders no price.

    `card_id` wins when both are given, which is the precedence the routes
    themselves apply (`engine_api.locate_castable` returns on `req.card_id`
    before it ever looks at `req.card`)."""
    identifier = body.get("card_id")
    if identifier is not None and str(identifier).strip():
        return str(identifier).strip(), False
    name = body.get("card")
    if name is not None and str(name).strip():
        return str(name).strip(), True
    return None


def _is_card(game: Game, action: dict[str, Any], reference: str, by_name: bool) -> bool:
    """Whether this `legal_actions` row is about the card `reference` names.

    A row carries only a `card_id` (`models.action.Action` has no name
    field), so a by-name reference is compared against the row's own
    resolved card name - the same resolution the rest of this section reads
    ids through. Case-insensitively, because a name is something a model
    typed."""
    identifier = str(action.get("card_id") or "")
    if not identifier:
        return False
    if not by_name:
        return identifier == reference
    return _named(game, identifier).casefold() == reference.casefold()


def _proposed_action(
    game: Game,
    path: str,
    body: dict[str, Any],
    legal_before: LegalActionsBefore | None,
) -> dict[str, Any] | None:
    """The row of `legal_before` this proposal is proposing, or `None` when
    nothing in it matches - including when the proposal names no card at all
    (`decide`, `pass-priority`, a whole attack declaration), which is every
    action whose cost is not a property of one named object.

    Matched rather than recomputed so the cost in the summary is the engine's
    own determined total for *this* action (CR 601.2f's total cost, with the
    increases and reductions it names and CR 118.9's alternative costs
    already applied), which a card's printed mana cost is not: Mind Stone
    prints `{2}` and its second ability costs
    `{1}, {T}, Sacrifice this artifact`. `legal_before` is the enumeration
    the recording route has already computed for its own row, so this costs
    no second pass over the engine.

    Two copies of one card in hand enumerate as two rows carrying the same
    name, and a by-name body cannot say which - so the best-scoring one
    wins, exactly as it does for the several rows one `card_id` can produce
    (a cast and its evoke). They are the same card with the same cost; the
    line is about the play, not about which copy leaves the hand."""
    reference = _card_reference(body)
    if reference is None or legal_before is None:
        return None
    identifier, by_name = reference
    best: tuple[int, dict[str, Any]] | None = None
    for action in legal_before.actions:
        if not _kind_matches(str(action.get("kind") or ""), path):
            continue
        if not _is_card(game, action, identifier, by_name):
            continue
        score = _agreement(action, body)
        if score is not None and (best is None or score > best[0]):
            best = (score, action)
    return best[1] if best is not None else None


def _named_card(game: Game, body: dict[str, Any]) -> CardState | None:
    """The `CardState` `body` names, by id or by name, or `None`.

    The by-name half is `engine_api.locate_castable` itself rather than a
    second implementation of it: "by id, or the first name match in
    `from_zone`" is a rule the cast route already owns, and a fallback that
    resolved a name differently from the route would quote the price of a
    card the human's own click would not cast."""
    from . import engine_api  # local: `engine_api` imports `schemas`, this does not
    from .schemas import CastRequest

    reference = _card_reference(body)
    if reference is None:
        return None
    identifier, by_name = reference
    if not by_name:
        try:
            return game.find_by_id(UUID(identifier))
        except Exception:
            return None
    try:
        return engine_api.locate_castable(game, CastRequest.model_validate(body))
    except Exception:
        return None


def _printed_cost(game: Game, path: str, body: dict[str, Any]) -> str | None:
    """The printed mana cost of the card `body` names, for a cast no
    currently-legal action matches.

    That happens for real, and it is not a defect: an advisor reads a board
    and records a moment later (a proposal is deliberately allowed to be born
    stale - see `app.record_advice`), and it may well propose the cast that
    becomes legal once the human has untapped or paid something else. A human
    still needs a price in front of them, so the printing stands in - said out
    loud as printed, because it is not a total the engine agreed to.

    Only for a cast (`_PRINTED_COST_PATHS`), because a cast is the only action
    whose cost the printing usually *is*."""
    if _path_tail(path).replace("-", "_") not in _PRINTED_COST_PATHS:
        return None
    card = _named_card(game, body)
    if card is None:
        return None
    return (card.card.mana_cost or "").strip() or None


def _cost_clause(
    game: Game, path: str, body: dict[str, Any], legal_before: LegalActionsBefore | None
) -> str | None:
    """`"for {1}{R}, sacrifice a creature"` - what this proposal would cost,
    or `None` when it costs nothing at all: CR 305.1's land play pays nothing
    ("the player simply puts the land onto the battlefield"), and a land
    prints no mana cost either, so the line says nothing about cost rather
    than inventing a `{0}`."""
    action = _proposed_action(game, path, body, legal_before)
    if action is not None:
        cost = str(action.get("cost") or "").strip()
        return f"for {cost}" if cost else None
    printed = _printed_cost(game, path, body)
    return f"for {printed} (printed)" if printed else None


def _source_names(game: Game, sources: Any) -> list[str]:
    """The mana sources `body["mana_sources"]` names, by card name. Takes
    both shapes the request allows (`list[UUID | schemas.ManaSource]`), since
    naming a source's colour or ability index does not change which permanent
    it is."""
    if not isinstance(sources, list):
        return []
    return [
        _named(game, source.get("card_id") if isinstance(source, dict) else source)
        for source in sources
    ]


def advice_summary(
    game: Game,
    method: str,
    path: str,
    body: dict[str, Any],
    legal_before: LegalActionsBefore | None = None,
) -> str:
    """One line saying what this proposal would actually do, card names and
    targets resolved against `game` - see this section's own comment for why
    the advisor never gets to write it.

    Deliberately built from a fixed list of well-known body keys rather than
    by dumping the body: the point is a sentence a human can read at a glance
    while the game is running, and an unknown key is far more likely to be
    noise (`expected_seq`, `reasoning`) than the thing being proposed. The
    exact body is recorded alongside this, in full, so nothing is lost by
    this line being short.

    `legal_before` is the enumeration the recording route has already
    computed for the same row (`legal_actions_before`), passed in so the cost
    this line quotes is the engine's own determined total for the action being
    proposed rather than the printed mana cost of the card it names - see
    `_proposed_action`. Omitted, the line still renders; only the cost falls
    back to the printing (`_printed_cost`), which is the same fallback a
    proposal no legal action matches already gets.
    """
    player = str(body.get("player") or "?")
    parts = [f"{player}: {_action_name(path)}"]

    card = body.get("card_id") or body.get("card")
    if card:
        parts.append(_named(game, card))
    if isinstance(body.get("ability_index"), int) and "activate" in path:
        parts.append(f"ability #{body['ability_index']}")
    if body.get("keyword"):
        parts.append(f"via {body['keyword']}")
    if body.get("from_zone") and body["from_zone"] != "hand":
        parts.append(f"from {body['from_zone']}")
    if isinstance(body.get("modes"), list) and body["modes"]:
        parts.append("modes " + ", ".join(str(mode) for mode in body["modes"]))
    if isinstance(body.get("x_value"), int):
        parts.append(f"X={body['x_value']}")
    cost = _cost_clause(game, path, body, legal_before)
    if cost:
        parts.append(cost)

    targets = _names(game, body.get("targets"))
    if targets:
        parts.append("targeting " + ", ".join(targets))
    attackers = body.get("attackers")
    if isinstance(attackers, list) and attackers:
        parts.append(
            "attacking "
            + ", ".join(
                f"{_named(game, pair.get('card_id'))} -> {_named(game, pair.get('defender'))}"
                for pair in attackers
                if isinstance(pair, dict)
            )
        )
    elif isinstance(attackers, list):
        parts.append("attacking with nobody")
    blocks = body.get("blocks")
    if isinstance(blocks, list) and blocks:
        parts.append(
            "blocking "
            + ", ".join(
                f"{_named(game, pair.get('blocker'))} blocks {_named(game, pair.get('blocked'))}"
                for pair in blocks
                if isinstance(pair, dict)
            )
        )
    elif isinstance(blocks, list):
        parts.append("blocking with nobody")
    pairs = body.get("pairs")
    if isinstance(pairs, list) and pairs:
        parts.append(
            "pairs "
            + ", ".join(
                f"{_named(game, pair.get('creature'))} -> {_named(game, pair.get('defender'))}"
                for pair in pairs
                if isinstance(pair, dict)
            )
        )

    for key, preposition in _COST_KEYS:
        named = _names(game, body.get(key))
        if named:
            parts.append(f"{preposition} " + ", ".join(named))
    sources = _source_names(game, body.get("mana_sources"))
    if sources:
        parts.append("paying with " + ", ".join(sources))

    # `decide`'s own answers (`schemas.DecideRequest`) - one kind per field,
    # so whichever one the body carries is the answer being proposed.
    if body.get("accept") is not None:
        parts.append("yes" if body["accept"] else "no")
    if body.get("choice"):
        parts.append(f"choosing {body['choice']}")
    for key, label in (("energy_amount", "energy"), ("life_amount", "life")):
        if isinstance(body.get(key), int):
            parts.append(f"paying {body[key]} {label}")
    for key, label in (("card_ids", "cards"), ("moved", "moving")):
        named = _names(game, body.get(key))
        if named:
            parts.append(f"{label} " + ", ".join(named))
    if isinstance(body.get("top_order"), list) and body["top_order"]:
        parts.append("top order " + ", ".join(_names(game, body["top_order"])))

    # The action and the thing it is done to read as one phrase ("alice: cast
    # Lightning Bolt"); every qualifier after that is its own clause.
    head = " ".join(parts[:2])
    qualifiers = parts[2:]
    return f"{head}, {', '.join(qualifiers)}" if qualifiers else head
