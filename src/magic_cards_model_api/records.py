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

from magic_cards_model.machines.decisions import PendingDecision
from magic_cards_model.machines.legal_actions import legal_actions as _legal_actions
from magic_cards_model.models import Game


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


def require_reasoning(game_id: str, player: str | None, reasoning: str | None) -> None:
    """Refuse a mutating call with no `player` named, or a human seat, is
    never refused here - `reasoning` is optional for either. Only a
    registered agent seat (`store.ai_players(game_id)`) is held to it, and a
    blank or whitespace-only string counts as none given."""
    if not player:
        return
    from .store import store

    if player in store.ai_players(game_id) and not (reasoning and reasoning.strip()):
        raise MissingReasoning(player)


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
