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

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from magic_cards_model.machines import Step
from magic_cards_model.machines.decisions import PendingDecision
from magic_cards_model.machines.legal_actions import legal_actions as _legal_actions
from magic_cards_model.models import CardState, Game

from .audit import PLAN_ACTION
from .repository import RECENT_RECORDS_WINDOW

#: CR 103's pregame steps, which the engine models as two steps a game
#: *rests* in before CR 103.8's first turn ever begins. No turn is under way
#: in either, so there is no turn for a plan to be about - `require_plan`
#: passes straight through them, and the mulligan/opening-hand routes stay
#: gated on `reasoning` alone.
_PREGAME_STEPS = frozenset({Step.MULLIGAN, Step.PREGAME_ACTIONS})


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


def require_plan(game_id: str, player: str | None, *, game: Game | None = None) -> None:
    """Refuse an agent seat's action on its own turn until a `plan_declared`
    record exists for that turn.

    A prediction is only evidence if it demonstrably preceded the outcome,
    and a facility nobody is obliged to use is a facility nobody uses - so
    the declaration is a precondition of acting rather than an invitation.
    Four things narrow it, each for its own reason:

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

    **The lookup is bounded, and this is the part that had to be right.**
    This sits on the hot action path, so it reads at most
    `RECENT_RECORDS_WINDOW` of the game's newest rows
    (`store.recent_records`) and scans them newest-first, rather than
    loading the whole ledger with `store.records()` - which would get slower
    with every turn played. The window is safe because the answer, when it
    is yes, is always inside the current player-turn's own rows, and those
    are by construction the newest ones. It is deliberately not an
    in-process cache either: the api may run more than one replica, and a
    memo of "who has declared" would answer differently per pod.

    The declaration is matched on `turns_taken`, not on the audit row's own
    `turn` stamp: `TurnMachine.turn_number` counts *rounds of the table*, so
    both seats' turns in one round share it, and an extra turn (CR 500.7)
    does not move it at all. `turns_taken` goes up exactly once per turn
    that begins, which is the identity a per-turn plan needs.
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
    if current == expected_seq or current == 0:
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
    actions = _legal_actions(game, player, pending=pending)
    return LegalActionsBefore(
        player=player, actions=[action.model_dump(mode="json") for action in actions]
    )


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
