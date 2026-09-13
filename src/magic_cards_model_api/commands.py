"""The command runner: replays a mutating api action against the engine's
pause-and-resume machinery (`magic_cards_model.machines.decisions`) instead
of blocking a request thread inside `Game.ask`.

Built in two passes: T7a added this module additively, against `decisions.py`'s
old `GameDecisionProvider`/`DecisionMailbox` (a live provider blocking a
request thread until an answer arrived, or a timeout defaulted one), with
nothing here yet wired into an HTTP route. T7b is what actually switched
every route over to run through here and deleted that old blocking machinery
outright - there is no provider, no mailbox, and no decision timeout left
anywhere in this api; `app.py`'s routes call `run_command`/`resume_command`
directly, and `decisions.py` now holds only the JSON view, the per-kind
answer coercion, and `validate_answer`.

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
persists next), or, on a pause, hands it back only as `RunResult.board` - a
view of the board the question was raised over, never committed (see
`paused_board`). This is what lets a
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
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from uuid import UUID

from magic_cards_model.machines.decisions import (
    Answer,
    DecisionPending,
    PendingDecision,
    default_answer,
)
from magic_cards_model.machines.priority import PriorityOutcome
from magic_cards_model.models import CardState, Game, Zone
from pydantic import BaseModel

from . import engine_api
from .decisions import ANSWER_FIELD_FOR_KIND
from .schemas import (
    ActivateRequest,
    AdvanceRequest,
    CastRequest,
    ConcedeRequest,
    CrewRequest,
    CycleRequest,
    EquipRequest,
    PassPriorityRequest,
    PlayLandRequest,
    SpecialActionRequest,
)

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
        back, so a caller never hand-assembles `payload` itself.

        **`expected_seq` is dropped** (`schemas.BoardRead`). It says which
        board the *caller* decided against; it is not part of what the
        command is, and it has already done its whole job by the time this
        runs - `records.require_current_board` ran before the route reached
        the engine at all. Leaving it in `payload` would make it part of
        `Command`'s identity, and `app._pause_slot_guard` decides whether an
        arriving command is a retry of the standing pause or a competitor by
        comparing exactly that: the same action re-posted on a `paused: true`
        answer (seatkit's autopass does this on every one it reads) would
        stop matching the pause it is retrying the moment the two sends
        disagreed about the ledger position - which they always do, since the
        first send is armed and the retry is not - and come back a 409 the
        caller can do nothing with. It is also replayed (`pause_is_live`,
        `resume_command`) against a board that has moved on by construction,
        where a stored watermark could only ever be misleading."""
        payload = request.model_dump(mode="json")
        payload.pop("expected_seq", None)
        return cls(kind=kind, payload=payload)


def _run_cast(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.cast_card(game, CastRequest.model_validate(payload))


def _run_play_land(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.play_land(game, PlayLandRequest.model_validate(payload))


def _run_activate_ability(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.activate_ability(game, ActivateRequest.model_validate(payload))


def _run_resolve_top(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.resolve_top(game)


def _run_suspend(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.suspend_card(game, SpecialActionRequest.model_validate(payload))


def _run_plot(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.plot_card(game, SpecialActionRequest.model_validate(payload))


def _run_foretell(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.foretell_card(game, SpecialActionRequest.model_validate(payload))


def _run_cycle(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.cycle_card(game, CycleRequest.model_validate(payload))


def _run_crew(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.crew_vehicle(game, CrewRequest.model_validate(payload))


def _run_equip(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.equip_permanent(game, EquipRequest.model_validate(payload))


def _run_concede(game: Game, payload: dict[str, Any]) -> Any:
    """CR 104.3a: no engine refusal to catch - a player may concede at any
    time, and leaves the game immediately. Never asks anything, so this never
    pauses in practice, but `run_command` handles that uniformly either way.
    """
    req = ConcedeRequest.model_validate(payload)
    game.player(req.player)
    game.concede(req.player, by=req.player)
    return {"winner": game.winner}


def _card_fingerprint(state: CardState, game: Game) -> tuple[Any, ...]:
    """One card's own observable state, identity included (CR 400.7) - see
    `_resolution_state`."""
    return (
        str(state.zone_change_stamp),
        state.tapped,
        state.is_summoning_sick(game),
        state.damage,
        tuple(sorted(state.counters.root.items())),
        state.attachment,
    )


def _resolution_state(game: Game) -> dict[str, Any]:
    """A fingerprint of everything a resolution is meant to change, minus the
    stack itself - moved here (from `app.py`) with `_run_pass_priority` in
    T7b, so the fingerprint taken right after `Game.pass_priority` resolves
    something is taken *inside* the same deterministic replay a pause resumes
    into, before `settle_priorityless_steps` gets a chance to run and change
    tracked state (CR 500.5's mana-pool emptying) for a reason that has
    nothing to do with what actually resolved.

    Resolving always pops the stack, so the stack cannot say whether the
    resolution *did* anything. Everything else can - board, hands,
    graveyards, exile, life, and pools - as long as it is compared by each
    card's own identity and observable state (`_card_fingerprint`), not by
    zone-and-name alone: a bare list of names cannot tell a card that stayed
    apart from a card that left and came back to the exact same zone under
    the exact same name, which is precisely what a blink (Ephemerate) does.
    Keyed by card id so `_without_own_arrival` can discount one specific
    object rather than guessing which of several same-named cards changed.

    This is necessary but not sufficient on its own for a *spell*: every
    spell also leaves the stack somewhere as part of resolving at all
    (CR 608.2m), and that arrival mutates this fingerprint regardless of
    whether the spell's own effect ran. `_without_own_arrival` is what
    discounts that one mandatory move for spells; abilities need no such
    correction, since CR 608.2n removes a resolved ability from the stack
    without putting it anywhere (see `machines.triggers.AbilityOnStack`).

    **What this still cannot see**, honestly: the library, the command zone,
    and the sideboard are not fingerprinted at all (an effect that only
    shuffles the library, or only changes what is in a face-down command-zone
    card, reads as "nothing changed"); a card's own printed text or
    characteristics changed by a continuous effect that touches none of the
    fields above (e.g. a name change with no P/T, tap, or counter change)
    is invisible; and two effects that exactly cancel out within the same
    resolution (a counter added then removed by the same spell) leave no
    trace, the same blind spot a snapshot diff always has.
    """

    def zone_fingerprint(cards: Any) -> dict[str, tuple[Any, ...]]:
        return {str(c.id): _card_fingerprint(c, game) for c in cards if isinstance(c, CardState)}

    return {
        "life": {player.username: player.life for player in game.players},
        "pools": {player.username: dict(player.mana_pool.floating) for player in game.players},
        "battlefield": zone_fingerprint(game.shared_zones.get(Zone.BATTLEFIELD, [])),
        "hands": {player.username: zone_fingerprint(player.hand) for player in game.players},
        "graveyards": {
            player.username: zone_fingerprint(player.graveyard) for player in game.players
        },
        "exile": zone_fingerprint(game.shared_zones.get(Zone.EXILE, [])),
    }


def _without_own_arrival(game: Game, spell_id: UUID, after: dict[str, Any]) -> dict[str, Any]:
    """`after`, with a just-resolved spell's own mandatory landing undone.

    A permanent spell resolving onto the battlefield is not "nothing
    happened" - a new object started existing under a player's control, and
    the `changed` marker should say so. So this only touches spells that did
    *not* land on the battlefield: an instant or sorcery goes to its owner's
    graveyard (CR 608.2m), and a few cards route themselves to exile instead
    (flashback, adventure, foretell). That move is the fixed, mandatory
    conclusion of resolving at all - it happens whether or not the spell's
    own effect executed - so it is subtracted back out before comparing
    against `before`. Anything beyond it (a life total, a hand, a second
    card's zone, the battlefield) is left untouched and is the real signal.

    Removed by the spell's own id, now that `_resolution_state` keys each
    zone by id rather than by a bare list of names - exact, unlike a
    by-name removal, which could not tell two identically named cards
    landing in the same zone apart.

    A spell that instead ends up in a zone `_resolution_state` does not track
    at all (library, command, sideboard) needs no discount here: `before` and
    `after` already agree on those untracked zones, so no false "changed"
    can arise from that arrival in the first place.
    """
    landed = game.find_by_id(spell_id)
    if landed is None or landed.zone is Zone.BATTLEFIELD:
        return after
    landed_key = str(spell_id)

    if landed.zone is Zone.EXILE:
        return {**after, "exile": {k: v for k, v in after["exile"].items() if k != landed_key}}
    if landed.zone is Zone.GRAVEYARD:
        owner = landed.controller_or_owner
        graveyards = dict(after["graveyards"])
        if owner in graveyards:
            graveyards[owner] = {k: v for k, v in graveyards[owner].items() if k != landed_key}
        return {**after, "graveyards": graveyards}
    if landed.zone is Zone.HAND:
        owner = landed.controller_or_owner
        hands = dict(after["hands"])
        if owner in hands:
            hands[owner] = {k: v for k, v in hands[owner].items() if k != landed_key}
        return {**after, "hands": hands}
    return after


def _run_pass_priority(game: Game, payload: dict[str, Any]) -> Any:
    """`Game.pass_priority` resolves the stack's top object when both players
    have passed in succession on a non-empty stack (CR 117.4) - and resolving
    is exactly where a spell or ability's own CR 608.2d question gets asked,
    the same as `resolve_top`. A refusal (not this player's priority to pass)
    raises `ValueError`, which - unlike `DecisionPending` - is not caught
    here, so it propagates out of `run_command` for the caller to turn into
    an `allowed: False` response the way `app.py` always has.

    `settle_priorityless_steps` runs inside this handler too, after
    `pass_priority` itself, so a step crossed automatically (CR 500.3) is
    part of the same deterministic replay a pause resumes into, rather than
    a second mutation `app.py` performed outside `run_command` entirely.
    The resolution fingerprint is taken *before* it runs - see
    `_resolution_state`'s own docstring for why the order matters.
    """
    req = PassPriorityRequest.model_validate(payload)
    resolving_obj = game.stack[-1] if game.stack else None
    resolving = resolving_obj.name if resolving_obj is not None else None
    resolving_spell_id = resolving_obj.id if isinstance(resolving_obj, CardState) else None
    before = _resolution_state(game)
    outcome = game.pass_priority(req.player)
    detail: dict[str, Any] = {"outcome": outcome}
    if outcome is PriorityOutcome.RESOLVE_TOP:
        detail["resolved"] = resolving
        after = _resolution_state(game)
        if resolving_spell_id is not None:
            after = _without_own_arrival(game, resolving_spell_id, after)
        detail["changed"] = after != before
    detail["settled_through"] = engine_api.settle_priorityless_steps(game)
    return detail


def _run_advance(game: Game, payload: dict[str, Any]) -> Any:
    """Move the turn marker forward (CR 500.1). Entering a step whose
    turn-based action asks something with no scripted answer - the cleanup
    discard, CR 508.1's/CR 509.1's combat declarations - pauses here exactly
    like a resolution does.

    CR 702.62a's suspend countdown is one of the beginning-of-step triggers
    `Game.advance` itself puts on the stack as part of this same call, along
    with everything else that triggers "at the beginning of" the step just
    entered - nothing further is needed here for it.
    """
    req = AdvanceRequest.model_validate(payload)
    step = game.advance(req.player)
    return {"advanced_to": step.value}


def _run_state_based_actions(game: Game, payload: dict[str, Any]) -> Any:
    return engine_api.run_state_based_actions(game)


def _run_pregame_settle(game: Game, payload: dict[str, Any]) -> Any:
    """Walk the whole pregame forward (`Game.settle_pregame`) as an ordinary
    command: pauses the moment it asks a `mulligan`, `opening_hand_action`,
    or a mulliganed seat's bottoming `choose_cards` question with no
    scripted answer, exactly like every other command handler here. `payload`
    is unused - `PregameSettleRequest.use_defaults` is read by `app.py`'s
    `pregame_settle` route, not by this handler, since it decides *how the
    route drives this command* (one attempt that may pause, vs
    `run_command_to_completion` looping it to the end with defaults), not
    anything the command itself needs to know.

    `settle_priorityless_steps` runs inside this handler too, after
    `Game.settle_pregame` itself, on the same reasoning `_run_pass_priority`'s
    and `_run_advance`'s docstrings give for calling it inside theirs: CR
    103.8's first turn begins at its untap step, which grants nobody
    priority (CR 502.4) and CR 500.3 ends of its own accord, and that has to
    be part of the same deterministic replay a pause resumes into rather
    than a second mutation performed outside `run_command` once the command
    finally completes - which, for `pregame_settle`, can be several
    `POST .../decide` calls later, well after `app.py`'s own route function
    returned.
    """
    step = game.settle_pregame()
    settled_through = engine_api.settle_priorityless_steps(game)
    if settled_through and game.turn is not None:
        step = game.turn.step
    return {"step": step.value, "settled_through": settled_through}


#: Every command kind `run_command` knows how to replay, mapped to the
#: function that actually drives it - `engine_api` for most, or a thin
#: wrapper right above for the handful (`concede`, `pass_priority`,
#: `advance`, `state_based_actions`, `pregame_settle`) that call the
#: engine's `Game` methods directly rather than through an
#: `engine_api.Outcome`-returning function.
#:
#: Deliberately not every mutating route this API has - see this module's
#: own docstring and `app.py`'s route-by-route conversion notes (T7b). The
#: rest of `opening.py`'s pregame endpoints (`pregame/mulligan`, `pregame/
#: opening-hand/action`, `pregame/opening-hand/finish`) are still NOT here:
#: each is told an answer already made (a pre-scripted bottoming choice, an
#: engine method with no live question of its own) and so never pauses in
#: practice, while each also calls `audit.record(game_id, ...)` *internally*,
#: reading the *committed* game back out of the store for its snapshot
#: (`audit.record`'s own lazy `store.get(game_id)`). Running that audit call
#: from inside a command handler - against `working`, a copy the store has
#: not yet been told about - would snapshot the *previous* committed state,
#: one command stale, for every pregame audit entry. Those three routes have
#: no exposure to a live CR 608.2d/CR 508.1/CR 509.1 question and buy nothing
#: from pausing, so they are left exactly as they were rather than risking
#: that regression for no benefit.
#:
#: `pregame_settle` is the one pregame route that IS a command (T7b's own
#: audit found MULLIGAN/OPENING_HAND_ACTION otherwise unreachable through
#: any live transport - `ask_mulligan` and `Game.take_opening_hand_actions`
#: only ever ran from here). `app.py`'s route audits *after* `_run_command`/
#: `run_command_to_completion` returns, against the newly committed game,
#: the same way every other command-backed route already does - so it does
#: not hit the staleness problem above.
#:
#: Board-setup routes (`app.py`'s "setting the board up" section) are the
#: same judgement as the three direct pregame routes, for a simpler reason:
#: they are test-harness affordances, not engine-driving actions, and never
#: touch a live decision at all.
COMMAND_HANDLERS: dict[str, Callable[[Game, dict[str, Any]], Any]] = {
    "cast": _run_cast,
    "play_land": _run_play_land,
    "activate_ability": _run_activate_ability,
    "resolve_top": _run_resolve_top,
    "suspend": _run_suspend,
    "plot": _run_plot,
    "foretell": _run_foretell,
    "cycle": _run_cycle,
    "crew": _run_crew,
    "equip": _run_equip,
    "concede": _run_concede,
    "pass_priority": _run_pass_priority,
    "advance": _run_advance,
    "pregame_settle": _run_pregame_settle,
    "state_based_actions": _run_state_based_actions,
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

    `PAUSED`: `game` is `None` - nothing is committed, and a resume replays
    from the committed game rather than patching this attempt's copy (see
    this module's own docstring). `board` is that copy as it stood the moment
    the question was raised, for *viewing only* (`paused_board`): a seat
    reading state mid-cast must see the card on the stack (CR 601.2a) and
    the cost paid (CR 601.2h) that the question comes after, not the board
    before the command began (battle 2, B2-13). `pending` is the question a
    caller stores alongside `(committed game, command, tape)` for the next
    `resume_command` call. `tape` echoes back exactly the answers this
    attempt was given, unchanged, so a caller does not have to thread it
    through separately.
    """

    status: RunStatus
    game: Game | None = None
    outcome: Any = None
    pending: PendingDecision | None = None
    tape: tuple[Answer, ...] = ()
    board: Game | None = None
    asked_on: Game | None = None
    """`resume_command` only: the board the answered question was asked on
    (its divergence guard's paused copy), for viewing and labelling only."""


def _replay_copy(game: Game) -> Game:
    """A deep copy of `game` for one replay attempt to mutate freely,
    leaving `game` itself untouched.

    Unlike `engine_api.dry_run_copy`, this deliberately keeps `game_uid`
    rather than minting a fresh one: a dry-run probe must never collide with
    ids the real game mints later, but a replay's entire point is the
    opposite - mint the *same* ids in the *same* order as the attempt being
    replayed, deterministically, from the same `game_uid` (see this module's
    own docstring on determinism).

    A `Game` is fully serializable, so this is a plain deep copy. It used to
    null and restore `game.decisions.provider` around the copy, mirroring
    `engine_api.dry_run_copy`, because that field could hold arbitrary caller
    state `copy.deepcopy` could not necessarily copy. T8 deleted the field
    from the engine, so there is nothing left to guard against.
    """
    return game.model_copy(deep=True)


#: `Game`'s own 18 fail-closed notice lists (`models/game.py:1148-1306`) - one
#: per category of clause this engine recognised but could not read, each
#: appended and never cleared. `continuous_effects.layer_notices` is the
#: 19th and lives one level down (`ContinuousEffects`, not `Game` itself), so
#: `_notice_snapshot`/`_new_notices` read it separately below rather than
#: folding it into this tuple.
_NOTICE_LIST_NAMES: tuple[str, ...] = (
    "targeting_notices",
    "intervening_if_notices",
    "cast_condition_notices",
    "mode_notices",
    "cost_modifier_notices",
    "anaphoric_clause_notices",
    "imprint_notices",
    "cast_from_hand_exile_notices",
    "conditional_clause_notices",
    "reflexive_clause_notices",
    "unwired_choice_notices",
    "suspend_redirect_notices",
    "delayed_clause_notices",
    "evasion_notices",
    "undefined_power_notices",
    "unmodeled_trigger_effect_notices",
    "control_replacement_notices",
    "opening_hand_notices",
)

#: The key `_new_notices` files a `continuous_effects.layer_notices` entry
#: under - it has no `Game`-level list of its own name to borrow, so this is
#: what the payload's `"kind"` and `_notice_snapshot`'s dict both call it.
_LAYER_NOTICE_KIND = "layer_notices"


def _notice_snapshot(game: Game) -> dict[str, int]:
    """How many entries each of `game`'s 19 notice lists holds right now -
    `run_command`'s "before" half of the diff `_new_notices` takes after a
    command finishes."""
    snapshot = {name: len(getattr(game, name)) for name in _NOTICE_LIST_NAMES}
    snapshot[_LAYER_NOTICE_KIND] = len(game.continuous_effects.layer_notices)
    return snapshot


def _notice_source(game: Game, source_id: UUID | None) -> tuple[str | None, str | None]:
    """The zone and controller a notice's own source card had, read from
    `game` right now - `commands.run_command` is the one place in this whole
    pipeline that still holds a live `Game` next to a freshly-appended
    notice; `visibility.redact_log_entry` reads a persisted log entry back
    out of storage later with no `Game` to ask, so this is captured once,
    here, or never.

    `source_id=None` - `OpeningHandNotice`'s own shape carries no `source_id`
    at all, and a handful of the other eighteen lists type theirs
    `UUID | None` too - has nothing to look up and fails closed: `(None,
    None)` never equals a real viewer in `visibility.redact_notices`'s own
    "is this viewer the owner" check, so a notice this function cannot
    positively clear is blanked for everyone but `ADMIN`, the same posture
    the engine's own notice lists keep for a clause they could not read.
    """
    if source_id is None:
        return None, None
    card = game.find_by_id(source_id)
    if card is None:
        return None, None
    return card.zone.value, card.controller_or_owner


def _new_notices(game: Game, before: dict[str, int]) -> list[dict[str, Any]]:
    """Every notice appended to any of `game`'s 19 lists since `before` was
    taken, as `{"kind": <the engine's own list name>, **notice.model_dump
    (mode="json"), "zone": ..., "controller": ...}` - the engine's own record
    passed straight through, plus the two bookkeeping keys `_notice_source`
    computes and `visibility.redact_notices` reads later. No explanatory
    sentence is authored here - what a notice *means* belongs in the MCP
    tool docstring and `driver/prompt.py`, not in this data.

    Generic across all 19 lists and every command kind: this never has to
    change when a 20th notice list is added, and carries no per-handler
    knowledge of what any single notice type means.
    """
    notices: list[dict[str, Any]] = []
    for name in _NOTICE_LIST_NAMES:
        items = getattr(game, name)
        for item in items[before[name] :]:
            payload = {"kind": name, **item.model_dump(mode="json")}
            if name == "opening_hand_notices":
                # `OpeningHandNotice` (`machines/opening_hand.py`) carries no
                # `source_id` at all to look up - but CR 103.6's whole
                # premise is a card in an opening HAND, so the zone is known
                # even though this function has no way to name which
                # player's. `controller=None` still fails this closed in
                # `visibility.redact_notices`: no real viewer, `ADMIN`
                # exempted by its own bypass, is ever `None`'s own controller.
                zone, controller = "hand", None
            else:
                zone, controller = _notice_source(game, getattr(item, "source_id", None))
            payload["zone"] = zone
            payload["controller"] = controller
            notices.append(payload)
    for item in game.continuous_effects.layer_notices[before[_LAYER_NOTICE_KIND] :]:
        payload = {"kind": _LAYER_NOTICE_KIND, **item.model_dump(mode="json")}
        zone, controller = _notice_source(game, item.source_id)
        payload["zone"] = zone
        payload["controller"] = controller
        notices.append(payload)
    return notices


def _attach_notices(outcome: Any, notices: list[dict[str, Any]]) -> None:
    """File `notices` onto whatever a command handler returned, generically:
    `engine_api.cast_card` and its siblings return an `Outcome` whose
    `detail` gets splatted by `Outcome.body()` into both the direct response
    and (via `app.audit_outcome`) the log row; `pass_priority`/`advance`/
    `resolve_top`/`state_based_actions`/`concede`/`pregame_settle` return a
    plain `dict` instead, so the same key lands there directly. Either way
    this is the one place that has to know both shapes exist - every route
    downstream just reads `detail.get("notices")` / `outcome.get("notices")`.

    **Only set when `notices` is non-empty.** The engine's own notice lists
    already prove a command that raised nothing is not the same claim as one
    nobody checked (`Game.mode_notices == []` after a fully-read modal
    spell, asserted right alongside the fail-closed case in
    `test_modal_spells.py`) - an *absent* key here reads exactly as honestly
    as a present, empty one, and costs nothing on every route this touches:
    dozens of existing responses pin their exact shape without a `notices`
    key at all, and `response_model_exclude_unset` already drops an unset
    field for free, so there is no reason to add one everywhere just to
    immediately throw it away.
    """
    if not notices:
        return
    if isinstance(outcome, engine_api.Outcome):
        outcome.detail["notices"] = notices
    elif isinstance(outcome, dict):
        outcome["notices"] = notices


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
    `PAUSED` one the moment a question has no scripted answer and no
    unconsumed tape entry.

    **Every fail-closed notice this command's own handler caused is
    attributed here**, generically, for every `kind` - not per handler. The
    snapshot is taken right after `working` is minted (so it reads `game`'s
    own starting counts, before this attempt touches anything) and diffed
    once the handler returns; on `PAUSED` there is nothing to attribute yet
    (`working` is thrown away, and a resumed attempt re-runs this same
    command from `game` with a longer tape, so the eventual `COMPLETED`
    diff already covers the whole command, pause included - see
    `resume_command`'s own docstring). See `_new_notices`/`_attach_notices`.
    """
    # `game` and the copy `_attempt` mints hold the same notice counts, so the
    # "before" half is read off `game` itself.
    before_notices = _notice_snapshot(game)
    working, outcome, pending = _attempt(game, command, tape)
    if pending is not None:
        return RunResult(status=RunStatus.PAUSED, pending=pending, tape=tuple(tape), board=working)

    working.decisions.finish_replay()
    if isinstance(outcome, engine_api.Outcome) and outcome.reason:
        # CR 605.1a: never advise activating a loyalty ability mid-cast - see
        # `engine_api.without_loyalty_mana_advice`.
        outcome.reason = engine_api.without_loyalty_mana_advice(working, outcome.reason)
    _attach_notices(outcome, _new_notices(working, before_notices))
    return RunResult(status=RunStatus.COMPLETED, game=working, outcome=outcome, tape=tuple(tape))


def _attempt(
    game: Game, command: Command, tape: Sequence[Answer]
) -> tuple[Game, Any, PendingDecision | None]:
    """One replay of `command` against a fresh copy of `game`: the copy, the
    handler's outcome, and the question it paused on (`None` when it ran to
    completion). The one place in this module that catches `DecisionPending`
    by name."""
    handler = COMMAND_HANDLERS.get(command.kind)
    if handler is None:
        raise KeyError(f"no command handler registered for kind={command.kind!r}")
    working = _replay_copy(game)
    working.decisions.pause_when_unanswered = True
    working.decisions.begin_replay(tape)
    try:
        outcome = handler(working, command.payload)
    except DecisionPending as exc:
        return working, None, exc.pending
    return working, outcome, None


def board_at_pause(game: Game, command: Command, tape: Sequence[Answer]) -> Game | None:
    """The half-applied game a paused command is standing in, or `None` when
    replaying it no longer pauses.

    A pause is never committed - `game` is the board from before the command
    - but what a player is being asked about happened after that: Galvanic
    Discharge's "You get {E}{E}{E}, then you may pay any amount of {E}" asks
    with the three energy already given. Replaying the tape is deterministic
    (this module's docstring), so this is exactly the board the stored
    question was raised on, and nothing here is kept or committed.
    """
    try:
        working, _, pending = _attempt(game, command, tape)
    except Exception:  # a read must survive a replay that no longer runs
        logger.exception("replaying the paused %s command for a state view failed", command.kind)
        return None
    return working if pending is not None else None


def run_command_to_completion(game: Game, command: Command) -> RunResult:
    """Run `command` against `game` all the way to `COMPLETED`, answering
    every pause along the way with the engine's own `default_answer` for
    that exact decision, rather than a live seat's.

    This is what makes a headless caller's "just settle it, consult nobody"
    request possible without a second, bespoke code path around
    `pause_when_unanswered`: it drives the same tape every live `POST .../
    decide` call would build, pre-filled with `default_answer` instead of a
    seat's own choice - `ask_mulligan`/`Game.ask` read the tape before ever
    reaching `pause_when_unanswered` (see the engine's own `ask_mulligan`
    docstring), so a fully pre-filled tape produces the identical committed
    game a genuinely unattended run would, deterministically, in one call.

    `app.py`'s `pregame_settle` route is the one caller today
    (`PregameSettleRequest.use_defaults=True`), but nothing here is specific
    to the pregame - any command this module can run, it can settle with
    defaults the same way.
    """
    tape: list[Answer] = []
    result = run_command(game, command, tape)
    while result.status is RunStatus.PAUSED:
        assert result.pending is not None
        decision = result.pending.decision
        field = ANSWER_FIELD_FOR_KIND[decision.kind]
        answer = Answer(
            seq=result.pending.seq,
            kind=decision.kind,
            player=decision.player,
            **{field: default_answer(decision)},
        )
        tape.append(answer)
        result = run_command(game, command, tape)
    return result


#: The command kinds whose pause means a spell or ability is part-way through
#: *resolving* - CR 608.2g and CR 117.3b, quoted by
#: `DecisionInProgressError`. `pass_priority` reaches a resolution through
#: CR 117.4's "all players pass in succession"; `resolve_top` is the same
#: resolution driven directly.
RESOLVING_KINDS = frozenset({"pass_priority", "resolve_top"})

#: The command kinds whose pause means a player is part-way through
#: *announcing* an action - CR 601.2's steps 601.2a-i for a cast, and the
#: same shape for every other announcement this api drives (an activation,
#: a special action). CR 601.2i is the citation: the announcing player gets
#: priority only "once the steps described in 601.2a-h are completed".
ANNOUNCING_KINDS = frozenset(
    {
        "cast",
        "activate_ability",
        "crew",
        "equip",
        "cycle",
        "suspend",
        "plot",
        "foretell",
        "play_land",
    }
)

#: The command kinds whose pause means a turn-based action has not been
#: dealt with yet - CR 117.3a: "The active player receives priority at the
#: beginning of most steps and phases, after any turn-based actions ... have
#: been dealt with". `advance` pausing on CR 508.1's or CR 509.1's
#: declaration, or on CR 514.1's cleanup discard, is exactly this.
TURN_BASED_KINDS = frozenset({"advance"})


def paused_board(
    committed_game: Game,
    command: Command,
    tape: Sequence[Answer],
    expected_pending: PendingDecision,
) -> Game | None:
    """The board as it stands at a stored pause, or `None` when the pause no
    longer describes `committed_game`.

    Replays `command` from the committed snapshot with the stored `tape` -
    the same replay `resume_command`'s divergence guard runs - and returns
    that replay's working copy when it lands on the identical
    `PendingDecision` again. Nothing is committed: the copy is what a state
    read shows while the question stands, and a resume still replays from
    `committed_game`.

    Why a state read needs it (battle 2, B2-13): the pause is raised part
    way through the command, after whatever the command already did. A cast
    trigger's target question (CR 603.3d) and the CR 603.3b order question
    come only once CR 601.2h has paid the cost and CR 601.2i has made the
    spell cast, so serving the committed game instead showed the card in
    hand and the lands untapped under a question that is asked after both.

    A replay that completes, pauses somewhere else, or is refused outright
    (the engine raising an ordinary `ValueError` because the action is no
    longer legal) returns `None`: the committed state has moved past the
    point that ever asked this question.
    """
    try:
        guard = run_command(committed_game, command, tape)
    except Exception:  # noqa: BLE001 - any engine refusal means "no longer live"
        # `DecisionPending` is a `BaseException` and is caught by name inside
        # `run_command`, so it never reaches here; an ordinary `ValueError`
        # (or a pydantic `ValidationError`, which is one) does, and means the
        # stored command is not even legal to attempt any more.
        return None
    if guard.status is not RunStatus.PAUSED or guard.pending != expected_pending:
        return None
    return guard.board


def pause_is_live(
    committed_game: Game,
    command: Command,
    tape: Sequence[Answer],
    expected_pending: PendingDecision,
) -> bool:
    """Whether a stored pause still describes `committed_game`.

    Staleness is **tested, never assumed**: `paused_board` replays `command`
    from the committed snapshot with the stored `tape`, and the pause is live
    only if that replay lands on the identical `PendingDecision` again. A
    replay that completes instead, pauses somewhere else, or refuses outright
    means the committed state has moved past the point that ever asked this
    question, and the slot is holding a question nobody can answer any more.

    That distinction is the whole reason `app.py` cannot simply adopt "the
    first pause wins": a genuinely stale pause left standing would wedge the
    game forever, with a `pending_decision` a live seat can never resolve.
    It also cannot simply adopt "the newest command wins", which is the
    defect this exists to close - see `DecisionInProgressError`.

    Cheap in the case that matters: this only runs while something *is*
    stored, and a replay from a committed snapshot mutates nothing (see
    `run_command`).
    """
    return paused_board(committed_game, command, tape, expected_pending) is not None


class DecisionInProgressError(RuntimeError):
    """A command was attempted while this game is part-way through another
    one, waiting on a decision only one seat can answer.

    **The defect this closes.** `app.py`'s `_run_command` used to run every
    fresh command against the committed snapshot with no tape and then store
    `(command, tape, pending)` unconditionally, so any of the fourteen
    mutating routes could overwrite a live pause that already carried
    answers. In the game that produced this class, an opponent's autopass
    landed 227 ms after a seat answered a fetchland's search: the pass
    re-paused on the search it had already been given an answer for and
    wrote `(pass_priority, (), choose_cards)` over
    `(pass_priority, (choose_cards=SteamVents,), pay_life)`. The seat's next
    answer was then read against the rewound slot, dispatched on its kind,
    and completed the resolution as a decline. Nothing was corrupt, nothing
    diverged, and no guard could see it - a deleted tape leaves no trace.

    **And it was never a legal action in the first place**, which is why
    this is a rules refusal in the engine's own words rather than a
    transport-level lock. Nobody receives priority part-way through a
    command:

    - mid-resolution, CR 608.2g - "No other spells can normally be cast and
      no other abilities can normally be activated during resolution" - and
      CR 117.3b - "The active player receives priority after a spell or
      ability (other than a mana ability) resolves";
    - mid-announcement, CR 601.2i - the caster gets priority only "once the
      steps described in 601.2a-h are completed";
    - before a turn-based action has been dealt with, CR 117.3a - "The
      active player receives priority at the beginning of most steps and
      phases, after any turn-based actions ... have been dealt with";
    - and generally, CR 117.1 - "which player can take actions at any given
      time is determined by a system of priority. The player with priority
      may cast spells, activate abilities, and take special actions."

    `status_code = 409`: the same "your request is well formed, the game is
    not in a state that accepts it" this api already means by 409
    (`CommandDivergenceError`, `VersionConflict`, `PregameOverruled`).

    Two things are deliberately NOT this error, and `app.py` handles both
    before raising it: re-posting the *identical* command that is already
    paused, which gets the standing question handed straight back
    (idempotent retry - a polling client must never be pushed into
    escalating), and conceding, which CR 104.3a permits "at any time".
    """

    status_code = 409

    def __init__(
        self, paused_command: Command, pending: PendingDecision, attempted: Command
    ) -> None:
        self.paused_command = paused_command
        self.pending = pending
        self.attempted = attempted
        decision = pending.decision
        message = (
            f"{attempted.kind} cannot be taken right now: this game is part-way "
            f"through {paused_command.kind} and is waiting on {decision.player}'s "
            f"{decision.kind.value} choice ({decision.prompt}). "
            f"{self._citation(paused_command.kind)} Answer that decision first "
            "(POST .../decide, or its actions/attack and "
            "actions/declare-blockers aliases); nothing else can happen until "
            "it is answered."
        )
        super().__init__(message)
        logger.info(message)

    @staticmethod
    def _citation(paused_kind: str) -> str:
        """The rule that makes this refusal a rules refusal rather than a
        lock, chosen by what the game is part-way through."""
        if paused_kind in RESOLVING_KINDS:
            return (
                "CR 608.2g: no other spells can normally be cast and no other "
                "abilities can normally be activated during resolution, and "
                "CR 117.3b gives the active player priority only after a spell "
                "or ability resolves - so no player has priority right now."
            )
        if paused_kind in ANNOUNCING_KINDS:
            return (
                "CR 601.2i: priority comes back only once the announcement's "
                "own steps (CR 601.2a-h) are completed, so no player has "
                "priority right now."
            )
        if paused_kind in TURN_BASED_KINDS:
            return (
                "CR 117.3a: the active player receives priority at the "
                "beginning of a step or phase only after any turn-based "
                "actions have been dealt with, so no player has priority "
                "right now."
            )
        return (
            "CR 117.1: which player can take actions is determined by a system "
            "of priority, and no player has priority part-way through another "
            "player's command."
        )


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

    # `asked_on`: the board the answered question was asked on - the guard
    # replay's own paused copy, already built, so a caller labelling the
    # answer with its step replays nothing more.
    return replace(run_command(committed_game, command, [*tape, new_answer]), asked_on=guard.board)
