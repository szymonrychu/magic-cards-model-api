"""The `GameRepository` seam: what the API needs from wherever games live,
and `InMemoryRepository`, the in-memory implementation that seam replaces
`store.GameStore`'s body with.

`GameRepository` is a `typing.Protocol` - structural, not a base class to
inherit from. It covers the surface every one of this store's 12 in-repo
callers and 20+ test files actually uses: game lifecycle (`create`, `get`,
`delete`, `ids`), the mutation boundary (`locked`), seat credentials
(`register_seats`, `seat_uids`, `player_for_uid`, `admin_key`, `is_admin`),
advisor credentials (`register_advisors`, `advisor_uids` - see `Advisor`),
`created_at`, decklists, and the three lobby-owned collections
(`register_agent_settings`/`agent_settings`, `register_ai_players`/
`ai_players`, `register_ui_game`/`ui_games`/`live_ui_games`).

**`reserve_slot`/`attach`/`release` (M8) replace the deleted
`create_if_under`.** The `MAX_LIVE_GAMES` cap used to be enforced by welding
the count-check to the create under one lock (`create_if_under`'s own
docstring recorded the two races that closed). It is now a reservation the
lobby-owned caller drives itself: `reserve_slot` claims a slot before doing
any work, `attach` turns a successful `create` into a real live game, and
`release` gives the slot back when creation failed. See `reserve_slot`'s own
docstring and `DEFAULT_RESERVATION_TTL_SECONDS` above for the full shape,
including how a stale reservation from a process that died is handled.

One thing on `InMemoryRepository` deliberately stays OFF the protocol:
`_locks` and its `locked()` internals are process-local by nature - a
postgres implementation (T9) makes `locked()` a transaction instead, but
nothing about *how* it does that belongs in the interface. `locked()` itself
IS on the protocol (its signature is the transaction boundary every
implementation must offer); only the lock dict backing this one is not.

`_automatic_pregame` / `record_automatic_pregame()` / `automatic_pregame()`
stay exactly as `GameStore` had them, just no longer part of the interface
this task is carving out - moving or reshaping them now would be scope this
task does not own.

`_paused_commands` / `store_paused_command()` / `paused_command()` /
`clear_paused_command()` are new here (T7b): the old `_mailboxes` /
`mailbox()` this docstring used to describe, and the `_decision_timeouts` /
`record_decision_timeout()` / `decision_timeouts()` beside it, were the
decision-transport machinery a blocking `Game.ask` needed - a live question
and, when nobody answered it in time, a record of what default was taken
instead. Pausing needs neither: a command that crosses an unanswered question
does not block and cannot time out, it returns immediately with the question
attached, and what a caller persists until it is answered is not a question
in flight but `(Command, tape, PendingDecision)` - everything
`commands.resume_command` needs to replay the same command with one more
answer appended. These three methods ARE on the protocol: T9's postgres
implementation must persist a paused command exactly as durably as the game
itself, which is the property this whole task exists to buy - see
`tests/test_command_runner.py`'s restart-survival test.

Nothing else about `InMemoryRepository`'s behaviour changes here: this is
`store.GameStore`'s previous body, moved and renamed, with `store.GameStore`
now an alias for it so every existing import keeps working.
"""

from __future__ import annotations

import threading
import time
from bisect import bisect_right
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from magic_cards_model.machines.decisions import Answer, PendingDecision
from magic_cards_model.models import Game

from .agent_settings import AgentSettings
from .commands import Command

#: How long a reservation (`GameRepository.reserve_slot`) counts against the
#: cap before it is swept as abandoned - the "stale reservation from a
#: process that died" case M8 asks every implementation to answer
#: explicitly. A process that dies mid-request never calls `attach` or
#: `release` again, and nothing about this deployment's shape
#: (`replicas: 1` through the whole of 2b, per the plan's M9) makes a
#: reservation durable across *that* process's own restart either - a
#: restart wipes the repository outright, reservations included. What the
#: TTL actually guards against is narrower: a reservation that outlives its
#: request *without* the process dying (a caller bug that never reaches its
#: `finally`), which would otherwise hold a slot hostage for the life of the
#: process. Both implementations read this from an instance attribute of the
#: same name (`RESERVATION_TTL_SECONDS`), so a test can shrink it per
#: instance without needing a constructor argument the contract suite's
#: zero-argument factories don't have room for.
DEFAULT_RESERVATION_TTL_SECONDS = 30.0

#: How many of a game's newest `records` rows a bounded lookup reads. The
#: plan gate (`records.require_plan`) sits on the hot action path and asks
#: one question of the ledger - "has this seat declared a plan for the turn
#: it is acting in" - and the answer, when it is yes, is always inside the
#: current player-turn's own rows, which are by construction the newest ones.
#: So the window only has to be comfortably larger than one player-turn's
#: record count (a dozen steps, a priority round each, both seats' actions
#: among them), and 500 is that with a wide margin while staying O(1) in the
#: length of the game. A full `store.records()` load here would get slower
#: every turn, which is exactly the failure this constant exists to avoid.
RECENT_RECORDS_WINDOW = 500


@dataclass(frozen=True)
class ArchivedGame:
    """Everything about one game that outlives its own deletion.

    `delete()` is a *soft* delete: the game stops being live, disappears
    from the lobby and answers `None` to `get()`, but its row is kept so a
    post-game review has something to read the audit trail against. This is
    the one accessor that sees a game whether it is live or archived, and it
    is deliberately the only one - every other method on the protocol
    behaves exactly as it did when `delete` threw the row away, so nothing
    that asks "can this game be played" can accidentally answer yes.

    `admin_key` and `seat_uids` are here because the review endpoint has to
    authorise a reader *after* the game has ended, and the credentials it
    authorises against are the ones the game was played with. `deleted_at`
    is `None` for a game that is still live, and a UTC ISO 8601 stamp for
    one that has been ended.

    **`game` is optional, and `unloadable` is why.** This is the one read
    whose caller must never be handed a `GameUnloadable` instead of an
    answer: `GET .../review` is the only way left to reach the ledger of a
    game that has been played, and a snapshot this build refuses to decode
    (an engine `CURRENT_SCHEMA_VERSION` bump - see `GameUnloadable`) made
    that ledger permanently unreachable, which is the exact opposite of what
    the soft delete was built for. So the refusal is reported as *data*: an
    `ArchivedGame` with `game=None` and `unloadable` carrying the decoder's
    own words, beside every other field here, none of which was ever stored
    inside the snapshot. Nothing about the engine's refusal is softened -
    the snapshot is still not decoded, and no caller gets a `Game` built
    from it.
    """

    game_id: str
    game: Game | None
    """`None` only when `unloadable` is set."""
    admin_key: str | None
    seat_uids: dict[str, str]
    """uid -> username, as `seat_uids()` returns it for a live game."""
    created_at: str | None
    deleted_at: str | None
    unloadable: str | None = None
    """The decoder's own refusal (`GameUnloadable.reason`), verbatim, or
    `None` for the ordinary case where `game` is there."""


@dataclass(frozen=True)
class Advisor:
    """One advisor credential: whose view a uid reads, and what agent (if
    any) is meant to run behind it.

    An advisor is not a seat. It reads exactly what its `player` reads
    (`visibility.resolve_viewer` resolves an advisor uid to that username,
    so every redaction downstream filters for the human without knowing an
    advisor exists) and it may not act: `game_access.py` refuses every
    mutating request that presents one. That refusal is a property of the
    uid itself rather than of whatever client is holding it, which is the
    whole reason this is a separate map from `seat_uids` instead of a flag
    beside one.

    **`agent` lives inside the entry, deliberately.** The obvious-looking
    alternative - reusing `register_agent_settings`, which is keyed by
    *username* - cannot express this at all: an advisor watching a seat that
    is itself an AI seat would collide with that seat's own entry, and two
    advisors on one seat would collide with each other. Keying by uid is
    what keeps "which settings belong to which credential" answerable, and
    the settings then have nowhere else sensible to live.
    """

    player: str
    """The username whose view this credential reads - a real player of the
    game, never a name of its own."""
    agent: AgentSettings | None = None
    """What runs behind this advisor, when something does. `None` for an
    advisor nobody has assigned an agent to (a human kibitzer, or one
    registered before its runner exists)."""

    def to_wire(self) -> dict[str, Any]:
        """The JSON shape stored in the `games.advisors` column, and the one
        an implementation must accept back from `from_wire`."""
        return {
            "player": self.player,
            "agent": None if self.agent is None else self.agent.to_wire(),
        }

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> Advisor:
        """The inverse of `to_wire`. `AgentSettings` validates either
        spelling of `max_thinking_tokens` (its `AliasChoices`), so a row
        written by an older build reads back unchanged."""
        agent = data.get("agent")
        return cls(
            player=data["player"],
            agent=None if agent is None else AgentSettings.model_validate(agent),
        )


class _ReservationLedger:
    """token -> the `time.monotonic()` it was reserved at. Shared plumbing
    for `InMemoryRepository` and `PostgresRepository`'s `reserve_slot`/
    `attach`/`release` - not itself thread-safe: every method here must be
    called with the owning repository's own lock already held, exactly like
    `InMemoryRepository`'s other `..._locked` helpers (`_live_ui_count_locked`,
    `_append_record_locked`). That lock is what makes a whole reserve-or-
    refuse decision atomic; this class only tracks which tokens are still
    outstanding.
    """

    def __init__(self) -> None:
        self._pending: dict[str, float] = {}

    def _expire(self, now: float, ttl_seconds: float) -> None:
        expired = [token for token, at in self._pending.items() if now - at > ttl_seconds]
        for token in expired:
            del self._pending[token]

    def reserve(self, *, live_count: int, cap: int, ttl_seconds: float) -> str | None:
        now = time.monotonic()
        self._expire(now, ttl_seconds)
        if live_count + len(self._pending) >= cap:
            return None
        token = uuid4().hex
        self._pending[token] = now
        return token

    def resolve(self, token: str, *, ttl_seconds: float) -> bool:
        """Remove `token` if it is still a live (unexpired) reservation and
        report whether it was. `attach` raises on `False`; `release` treats
        `False` as the no-op it already wants to be."""
        self._expire(time.monotonic(), ttl_seconds)
        return self._pending.pop(token, None) is not None


class GameUnloadable(Exception):
    """A game's row exists, but this build cannot turn the snapshot in it
    back into a `Game`.

    The case this exists for is an engine schema bump: `Game.
    CURRENT_SCHEMA_VERSION` moves, and every row written under the previous
    value is refused by `Game._check_schema_version` with
    `SchemaVersionMismatch`. That refusal is the engine's to make and is not
    softened anywhere in this repo - a snapshot whose behaviour was recorded
    under a different engine version genuinely must not be replayed as
    though it were current. What is wrong is only *where the refusal lands*:
    it used to escape a batch read as a bare `RuntimeError`, so one stale row
    500'd `GET /api/games`, `GET /api/lobby/games`, and (through
    `reserve_slot`'s live-game count) `POST /api/lobby/games` for every game,
    including the ones that decode perfectly.

    So the repository converts it, at its single decode point, into this -
    an exception of the api's own vocabulary that a batch read can catch per
    row and a single-game read can let escape to `app.py`'s handler (409,
    `reason` verbatim in the body). `reason` is always the underlying
    decoder's own message, never a paraphrase: the engine says why, and
    every layer above quotes it.

    Deliberately not a `ValueError` (`app.py`'s `ValueError` handler answers
    400, which would read as "your request was malformed" for a condition
    the caller cannot do anything about) and deliberately not a `KeyError`
    (404 would say the game does not exist, when it does - and would send a
    seat off to start a new one instead of reporting the real cause).
    """

    def __init__(self, game_id: str, reason: str) -> None:
        super().__init__(f"game {game_id} cannot be loaded: {reason}")
        self.game_id = game_id
        self.reason = reason


@runtime_checkable
class GameRepository(Protocol):
    """Everything the API needs from wherever games are held.

    Structural: an implementation satisfies this by having these methods with
    these signatures, not by inheriting from it. `InMemoryRepository` is
    today's implementation; a postgres-backed one (T9) is another.
    """

    def create(self, game: Game, *, owner_user_id: str | None = None) -> str:
        """Store `game` under a fresh id and return it.

        `owner_user_id` is the OIDC subject (JWT `sub`) of whoever created
        this game, when the request that created it carried a valid bearer
        token - `None` (the default) for every caller that did not, which is
        every caller today. A game with no owner is unrestricted, exactly as
        every game was before this parameter existed: `game_access.py`'s
        ownership gate only ever narrows access for a game that *has* an
        owner, so an ownerless game stays reachable by uid/admin key alone,
        the pre-existing model. See `game_access.py`'s module docstring for
        the full authorization design and the migration story."""
        ...

    def reserve_slot(self, cap: int) -> str | None:
        """Claim one of `cap` lobby slots, atomically, and return a token -
        or `None` once `cap` slots are already spoken for. "Spoken for"
        counts both `live_ui_games()` and every other reservation this call
        has not yet resolved (`attach` or `release`), so the slot is claimed
        the instant this returns, before the caller has done any of the
        possibly slow, possibly failing work of actually creating the game.

        This is M8's reserve-then-create, replacing `create_if_under`
        (deleted): the two documented races that method's own docstring used
        to close - two concurrent callers both reading room under the cap,
        and a successful create only partially visible on return - are now
        closed by `reserve_slot` claiming the slot up front and `attach`
        making the eventual game fully visible in the same locked step it
        resolves the reservation. See `RESERVATION_TTL_SECONDS` for what
        happens to a reservation nobody ever resolves.
        """
        ...

    def attach(self, token: str, game_id: str) -> None:
        """Resolve a successful `reserve_slot` reservation into a real live
        game: `game_id` starts counting against the cap as an ordinary ui
        game (`register_ui_game`'s own effect), and `token` stops counting
        as an outstanding reservation - both in the same atomic step, so no
        concurrent `reserve_slot` can ever observe the gap between them.
        Raises `KeyError` for a token that is unknown, already resolved
        (attached or released), or expired past `RESERVATION_TTL_SECONDS`.
        """
        ...

    def release(self, token: str) -> None:
        """Give back a reservation that will never be attached - the
        `finally` `start_game_from_decklists` calls this from when creating
        the game failed or raised, so a slot is never leaked. Deliberately a
        no-op, never raising, for a token that is unknown, already released,
        or already attached: a caller that calls this unconditionally from a
        `finally` - including on the success path, after `attach` already
        ran - must never have that call blow up the response it is trying to
        return.
        """
        ...

    def get(self, game_id: str) -> Game | None:
        """The game for this id, or `None` if there is none.

        Raises `GameUnloadable` for a third case the `Game | None` return
        cannot express and must never be collapsed into either half of it: a
        row that exists but that this build cannot decode. `None` would say
        the game is gone (and a caller would tell a player their game
        vanished, or delete the seat's only handle on it); the underlying
        `RuntimeError` escaping raw is what took `GET /api/games` down. An
        implementation that holds live `Game` objects (`InMemoryRepository`)
        never decodes and so never raises this; one that stores a snapshot
        (`PostgresRepository`) can, on any row a schema bump left behind.

        **Every caller that reads more than one game must catch it per
        game**, skip that game, and say so in what it returns - see
        `app.list_games`, `decks_api.list_lobby_games` and
        `api_client.InProcessApiClient.game_summaries`. A caller reading one
        named game lets it escape: `app.py`'s handler answers 409 with the
        decoder's own words.
        """
        ...

    def replace(self, game_id: str, game: Game, *, record: dict[str, Any] | None = None) -> int:
        """Replace `game_id`'s committed game outright with `game`. Raises
        `KeyError` for an unknown game.

        `commands.run_command`/`resume_command` never mutate the `Game` they
        are given - a command runs to completion (or pauses) against an
        internal deep copy - so a caller that gets back a `RunStatus.
        COMPLETED` `RunResult` has a brand new `Game` object to persist as
        this game's next committed state, not an in-place mutation of the one
        `locked()` handed it. This is how that new object actually replaces
        the old one. Called from inside the same `with locked(game_id):`
        block a command ran under, so the whole read-run-commit sequence
        stays covered by one held lock.

        **T10**: every successful `replace()` also appends one `records` row
        for this commit - `record` is that row's payload (`{}` when the
        caller has none ready yet). For `PostgresRepository` the row is
        inserted in the very same transaction as the game update it commits
        alongside, exactly as it was when T9 built this method; a caller that
        does not yet have the full payload (`app.py`'s `_run_command`/
        `_resume`, which only know the engine's outcome once `commands.
        run_command` has already returned) fills it in afterward with
        `fill_record`, addressed by the `seq` this call returns.
        """
        ...

    def delete(self, game_id: str) -> bool:
        """End this game: it stops being live, and every other method here
        answers for it exactly as if it had been removed - `get` is `None`,
        `ids`/`ui_games`/`live_ui_games` omit it, `locked` raises, its seat
        credentials and admin key are gone, its paused command is cleared.
        Returns whether a live game actually existed to end (`False` for an
        unknown id, and for one already ended).

        **The row itself is kept** (`archived()` reads it back), which is
        the one thing this no longer does. The user's requirement was
        "Games must be persisting in the database even after are
        stopped/finished/conceded": the audit trail already outlived the
        game it describes (`records` carries no foreign key), but the board,
        hands and state it describes did not, so a post-game review would
        have had reasoning with nothing to read it against. See
        `ArchivedGame`.
        """
        ...

    def archived(self, game_id: str) -> ArchivedGame | None:
        """This game as a review may read it - live or ended - or `None`
        when no such game was ever created.

        The only method that sees past `delete()`. Everything a post-game
        review needs and nothing else: the final state, the credentials to
        authorise a reader against, and when (if ever) the game was ended.
        See `ArchivedGame`.

        **Never raises `GameUnloadable`**, unlike `get()`. A snapshot this
        build refuses to decode comes back as an `ArchivedGame` whose `game`
        is `None` and whose `unloadable` carries the decoder's own words, so
        the ledger of a game that was actually played stays reachable. See
        `ArchivedGame` for why this one read is the exception.
        """
        ...

    def append_record(self, game_id: str, payload: dict[str, Any]) -> int:
        """Append one `records` row for `game_id`, standalone - not paired
        with a `replace()` call, because there is no game mutation to be
        atomic with (a refusal caught before any command ever ran, an admin
        action, a lobby reset, ...). Returns the row's own `seq`, this game's
        next one in append order.

        Never raises for an unknown `game_id` - this is `audit.record`'s own
        write path, and `audit.record`'s docstring has always promised "a
        game that is no longer in the store (or was never in it) still gets
        its entry" (the old module-global `_logs` dict enforced nothing about
        the id it was keyed by, and more than one caller - a test harness
        probing a game id that was never created, `ai_seat.AiSeatRegistry`
        dispatching before a game exists - relies on that leniency)."""
        ...

    def fill_record(self, game_id: str, seq: int, payload: dict[str, Any]) -> None:
        """Replace the payload of the `records` row `replace()` already wrote
        at `(game_id, seq)` - `seq` is what that `replace()` call returned.
        This is how a caller that only learns an action's full detail (the
        engine's outcome, `audit.record`'s turn/step/snapshot) *after*
        `replace()` has already committed the game update still ends up with
        one complete row rather than an empty placeholder plus an orphaned
        second insert. Raises `KeyError` when no such row exists."""
        ...

    def records(self, game_id: str) -> list[dict[str, Any]]:
        """Every `records` row for `game_id`, oldest (lowest `seq`) first, as
        `{"seq": int, "payload": dict, "created_at": str}`. Empty for an
        unknown game - this mirrors `audit.entries`'s old behaviour rather
        than raising, since `GET /api/games/{id}/log` reads this for a game
        that has already been checked to exist by its own 404."""
        ...

    def records_since(self, game_id: str, since: int) -> list[dict[str, Any]]:
        """Every `records` row for `game_id` whose `seq` is greater than
        `since`, oldest first, in the same shape `records()` returns them.
        `since=0` is therefore the whole ledger, and a `since` at or past the
        last row is empty. Empty for an unknown game, like `records()`.

        The other bounded read, and the one `GET /api/games/{id}/log` serves
        every poll from. `records()` was the wrong shape for it in exactly
        the way `recent_records`'s docstring describes: the route already
        discarded every row at or below `since` in Python, so a caller
        resuming from the end of a long game paid for the whole game's
        history - each row a full `snapshot(game)` plus a
        `legal_actions_before` enumeration - to be handed nothing. No
        implementation may load a row it will not return; postgres does it
        with a `seq > :since` predicate on the same
        `uq_records_game_id_seq` index `recent_records` uses.
        """
        ...

    def max_record_seq(self, game_id: str) -> int:
        """The highest `seq` recorded for `game_id`, `0` when it has no rows.

        The `last_seq` half of that same log read, and the reason bounding
        the read does not change what the route answers. `last_seq` is the
        watermark a client resumes from, so it must stay the true latest seq
        even when the bounded read comes back empty - which is precisely the
        case where the rows themselves can no longer tell anyone what it is.
        Answered from the index, never by counting rows.
        """
        ...

    def recent_records(self, game_id: str, limit: int) -> list[dict[str, Any]]:
        """At most `limit` of this game's newest `records` rows, in the same
        oldest-first order and the same shape `records()` returns them.

        The bounded read. `records()` is fine for a review, which runs once
        when a game is over; it is the wrong shape for anything on the hot
        action path, because its cost grows with every turn played. Every
        implementation must answer this without loading the whole ledger -
        postgres does it with `ORDER BY seq DESC LIMIT`, served by the
        `uq_records_game_id_seq` unique index that already exists on
        `(game_id, seq)`.

        Deliberately not a cache: the api may run more than one replica, and
        an in-process memo of "who has declared a plan" would answer
        differently depending on which pod took the request.
        """
        ...

    def record_automatic_pregame(self, game_id: str, player: str, kind: str) -> None:
        """Remember that `kind` (`"mulligan"` or `"opening_hand"`) was taken
        *for* `player` by `ai_pregame.py`'s backstop rather than by the seat.

        On the protocol as of the runtime store selection: `store.py` now
        picks its implementation from the environment, so `opening.py` calls
        this on whichever one was picked and both have to offer it. Both
        implementations keep it in process memory - the backstop that writes
        it is itself a thread in this process, so there is nothing a second
        replica could usefully read here anyway. See
        `PostgresRepository.__init__`."""
        ...

    def automatic_pregame(self, game_id: str, player: str) -> set[str]:
        """The pregame moves the backstop made for this seat, if any."""
        ...

    def ids(self) -> list[str]:
        """Every game id currently held."""
        ...

    def locked(self, game_id: str) -> AbstractContextManager[Game]:
        """Hold this game's mutation lock for the duration of the `with`
        block, yielding the `Game`. Raises `KeyError` for an unknown game
        before taking any lock, and `GameUnloadable` (see `get`) for one
        whose stored snapshot this build cannot decode - a game nobody can
        read is a game nobody may act on either.

        This is the transaction boundary: a request holds it for its whole
        mutating body, so two requests can never mutate the same game at
        once. A backing store that is a real transactional database
        implements this as a transaction around the block.
        """
        ...

    def register_seats(self, game_id: str, uids: dict[str, str]) -> None:
        """Record `uids` (uid -> username) as this game's seat credentials."""
        ...

    def seat_uids(self, game_id: str) -> dict[str, str]:
        """uid -> username, for whoever is dispatching the players."""
        ...

    def player_for_uid(self, game_id: str, uid: str) -> str | None:
        """Whose seat this uid is, or `None` when the uid is not in this
        game."""
        ...

    def register_advisors(self, game_id: str, advisors: dict[str, Advisor]) -> None:
        """Record `advisors` (uid -> `Advisor`) as this game's advisor
        credentials. Replaces the whole map rather than merging into it,
        exactly like `register_seats` - a caller adding one reads, modifies
        and writes back under `locked()`."""
        ...

    def advisor_uids(self, game_id: str) -> dict[str, Advisor]:
        """uid -> `Advisor`, for this game. Empty for an unknown game and
        for a game nobody has given an advisor.

        Durable, never process-local: the api runs several replicas
        (`repository_postgres.py`'s note on per-replica watchers) and an
        advisor uid minted on one of them must be recognised by all of them,
        which is the same reason `seat_uids` lives here rather than in an
        in-process registry.
        """
        ...

    def admin_key(self, game_id: str) -> str | None:
        """This game's admin key, to hand to whoever just created it."""
        ...

    def is_admin(self, game_id: str, key: str) -> bool:
        """Whether `key` is this game's admin key. A missing key is never
        one."""
        ...

    def owner_user_id(self, game_id: str) -> str | None:
        """The OIDC subject that owns this game - `None` for an unknown game
        and, just as importantly, for a known one nobody ever attributed an
        owner to. `game_access.py`'s ownership gate treats the two
        identically: no owner means no restriction beyond the pre-existing
        uid/admin-key model. Set once, at `create()`, never afterward -
        `attach()` only turns a `reserve_slot()` reservation into a live ui
        game, it never touches ownership, which the caller already named at
        `create()` time. There is no `register_owner`, on purpose, so
        ownership can never be reassigned out from under a game after the
        fact.
        """
        ...

    def created_at(self, game_id: str) -> str | None:
        """When this game was created, UTC ISO 8601 - `None` for an unknown
        game."""
        ...

    def register_decklists(self, game_id: str, decklists: dict[str, str]) -> None:
        """Record the decklist text each seat was booted from: username ->
        text."""
        ...

    def decklists(self, game_id: str) -> dict[str, str]:
        """username -> decklist text, for a game booted from decklists."""
        ...

    def register_agent_settings(self, game_id: str, agents: dict[str, AgentSettings]) -> None:
        """Record each AI seat's `AgentSettings`: username -> settings.
        Lobby-owned."""
        ...

    def agent_settings(self, game_id: str) -> dict[str, AgentSettings]:
        """username -> `AgentSettings`, for this game's AI seats only.
        Lobby-owned."""
        ...

    def register_ai_players(self, game_id: str, players: list[str]) -> None:
        """Record which seats are meant to be driven by a bot rather than a
        human at a browser. Lobby-owned."""
        ...

    def ai_players(self, game_id: str) -> set[str]:
        """The usernames registered as bot-driven. Lobby-owned."""
        ...

    def register_ui_game(self, game_id: str) -> None:
        """Record that the play UI's lobby booted this game. Lobby-owned."""
        ...

    def ui_games(self) -> list[str]:
        """The lobby-booted games still in the store, oldest first.
        Lobby-owned."""
        ...

    def live_ui_games(self) -> list[str]:
        """The lobby-booted games that have not ended yet, oldest first.
        Lobby-owned."""
        ...

    def store_paused_command(
        self,
        game_id: str,
        command: Command,
        tape: tuple[Answer, ...],
        pending: PendingDecision,
    ) -> None:
        """Remember that `game_id` is paused mid-`command`, having replied to
        `tape` and now waiting on `pending`.

        Replaces whatever this game's previously stored paused command was,
        if any - a game has at most one live pause at a time, the same
        invariant `decisions.DecisionMailbox` used to hold ("every decision
        kind this module reaches ... never has two questions pending on the
        same player at once"). Called from a mutating route the moment
        `commands.run_command`/`resume_command` returns `RunStatus.PAUSED`.

        **Bumps `pause_generation`.** This is the only call that does, which
        is what makes the counter mean "the standing question changed" and
        not "something happened to this game".
        """
        ...

    def pause_generation(self, game_id: str) -> int:
        """How many times this game's standing question has been replaced -
        `0` for a game that has never paused.

        Monotonic per game and never reset, `clear_paused_command` included:
        a reset would let a later pause reuse a number a stale client is
        still holding, which is the one thing a generation may never do. It
        rides out on every `pending_decision` view and comes back as
        `DecideRequest.generation`, so a seat can say which question it
        believes it is answering - see `app.py`'s `decide`.
        """
        ...

    def paused_command(
        self, game_id: str
    ) -> tuple[Command, tuple[Answer, ...], PendingDecision] | None:
        """The paused command `game_id` is waiting to resume, or `None` when
        nothing is pending. This is where `serialize.state_view`'s
        `pending_decision` is read from, and what `POST .../decide` (and its
        `actions/attack`/`actions/declare-blockers` aliases) resume against.
        """
        ...

    def clear_paused_command(self, game_id: str) -> None:
        """Forget `game_id`'s paused command - called once it resumes to
        `RunStatus.COMPLETED`, or once a fresh command runs and pauses on a
        *different* question, in which case `store_paused_command` overwrites
        rather than needing this called first. A no-op when nothing was
        pending."""
        ...


class InMemoryRepository:
    """The in-memory `GameRepository`: game id -> engine `Game`. Nothing
    persists, and a restart wipes it.

    Alongside each game the store keeps that game's seat credentials - a
    secret uid per player - so a caller can prove which seat is theirs
    without the opponent ever being able to claim it. The uids are generated
    when the game is created, handed to whoever dispatches the players, and
    never appear in any state view.

    The store also mints one admin key per game, handed only to whoever
    created it (alongside the seat uids, in the same response) - the
    operator's own credential, good for the full, unfiltered view a harness
    needs to build and verify a scenario. It is never returned again after
    creation, exactly like a seat uid, and it never appears in any state view
    either.

    Each game also gets, at creation, a mutation lock. It closes a latent race
    this store used to leave open: `get()` used to hand out the shared `Game`
    with nothing stopping two requests from mutating it at once. `locked()`
    is that fix - every mutating endpoint holds it for the request's whole
    body. Answering a paused decision (`POST .../decide`) is an ordinary
    mutating request now and takes this same lock like any other - see
    `commands.py`'s module docstring for why that is safe: nothing blocks
    inside it any more.
    """

    #: See `DEFAULT_RESERVATION_TTL_SECONDS`'s docstring - an instance
    #: attribute (not a plain default parameter) so a test can shrink it per
    #: instance without the zero-argument contract-suite factory needing a
    #: constructor argument for it.
    RESERVATION_TTL_SECONDS: float = DEFAULT_RESERVATION_TTL_SECONDS

    def __init__(self) -> None:
        self._games: dict[str, Game] = {}
        self._seats: dict[str, dict[str, str]] = {}  # game_id -> uid -> username
        # game_id -> uid -> the advisor that uid is. Separate from `_seats`
        # on purpose: `game_access.py` refuses a mutating request that
        # presents one of these, and it can only do that if an advisor uid
        # is never mistakable for a seat uid. See `Advisor`.
        self._advisors: dict[str, dict[str, Advisor]] = {}
        self._admin_keys: dict[str, str] = {}  # game_id -> admin key
        # game_id -> the OIDC subject that created it, only when the creating
        # request carried a valid bearer token - absent (never `None`, just
        # missing) for every ownerless game, matching every other optional
        # per-game fact this store keeps beside `_games`. See `create()`.
        self._owner_user_id: dict[str, str] = {}
        self._created_at: dict[str, str] = {}  # game_id -> UTC ISO 8601 creation time
        # game_id -> its mutation lock. Process-local by nature, and not part
        # of `GameRepository` - only `locked()` itself is.
        self._locks: dict[str, threading.Lock] = {}
        # game_id -> the command it is paused mid-way through, the tape of
        # answers already accepted, and the question it is waiting on. At
        # most one per game (`store_paused_command`'s own docstring).
        self._paused_commands: dict[str, tuple[Command, tuple[Answer, ...], PendingDecision]] = {}
        # game_id -> how many times its standing question has been replaced.
        # Bumped by `store_paused_command` and by nothing else; deliberately
        # NOT cleared by `clear_paused_command` (see `pause_generation`).
        self._pause_generations: dict[str, int] = {}
        self._decklists: dict[str, dict[str, str]] = {}  # game_id -> username -> decklist text
        # Lobby-owned. game_id -> username -> that seat's AgentSettings. Only
        # AI seats have an entry - a human seat is never given one, same as
        # `_ai_players`.
        self._agent_settings: dict[str, dict[str, AgentSettings]] = {}
        # Lobby-owned. game_id -> usernames driven by a bot.
        self._ai_players: dict[str, set[str]] = {}
        # game_id -> username -> the pregame moves something made *for* that
        # seat rather than the seat making them itself. Decision-transport
        # adjacent machinery (T7 owns it), not part of `GameRepository`. See
        # `record_automatic_pregame`.
        self._automatic_pregame: dict[str, dict[str, set[str]]] = {}
        # Lobby-owned. The games the play UI's lobby booted, oldest first.
        # Only those are ever reset through `DELETE /api/ui/games/{id}`
        # (`lobby.py`): a game booted by `serve.py --match` or by
        # `POST /api/games` belongs to whoever booted it, and the browser
        # ending a lobby game must not touch somebody else's concurrent one.
        self._ui_games: list[str] = []
        # game_id -> its records rows, oldest first: {"seq", "payload",
        # "created_at"}. T10: what `audit.py` used to keep in its own
        # module-global `_logs` dict now lives here instead, so this store
        # satisfies exactly the same `GameRepository` surface postgres does -
        # see `replace`/`append_record`/`fill_record`/`records`.
        self._records: dict[str, list[dict[str, Any]]] = {}
        # game_id -> what `delete()` kept when it ended that game. The
        # postgres implementation keeps the whole row and flags it with
        # `archived_at`; here, `delete` still pops every live collection
        # exactly as it always did (so no accessor has to learn about
        # archiving) and moves the handful of facts a review needs into
        # this one dict instead. See `ArchivedGame` and `archived()`.
        self._archived: dict[str, ArchivedGame] = {}
        self._lock = threading.Lock()
        # M8: pending reservations against `MAX_LIVE_GAMES`, keyed by token.
        # Protected by `self._lock`, exactly like everything else here - see
        # `_ReservationLedger`'s own docstring for why it takes no lock of
        # its own.
        self._reservations = _ReservationLedger()

    def create(self, game: Game, *, owner_user_id: str | None = None) -> str:
        game_id = uuid4().hex
        with self._lock:
            self._games[game_id] = game
            self._admin_keys[game_id] = uuid4().hex
            if owner_user_id is not None:
                self._owner_user_id[game_id] = owner_user_id
            self._created_at[game_id] = datetime.now(UTC).isoformat()
            self._locks[game_id] = threading.Lock()
        return game_id

    def reserve_slot(self, cap: int) -> str | None:
        with self._lock:
            live = self._live_ui_count_locked()
            return self._reservations.reserve(
                live_count=live, cap=cap, ttl_seconds=self.RESERVATION_TTL_SECONDS
            )

    def attach(self, token: str, game_id: str) -> None:
        with self._lock:
            if not self._reservations.resolve(token, ttl_seconds=self.RESERVATION_TTL_SECONDS):
                raise KeyError(f"no such reservation {token}")
            self._register_ui_game_locked(game_id)

    def release(self, token: str) -> None:
        with self._lock:
            self._reservations.resolve(token, ttl_seconds=self.RESERVATION_TTL_SECONDS)

    def _register_ui_game_locked(self, game_id: str) -> None:
        """`register_ui_game`'s own body, called with `self._lock` already
        held - `attach` needs this, since the reservation's resolution and
        the `_ui_games` registration must happen as one atomic step (a
        concurrent `reserve_slot` must never observe the gap between
        them)."""
        if game_id not in self._ui_games:
            self._ui_games.append(game_id)

    def _live_ui_count_locked(self) -> int:
        """`len(live_ui_games())`, computed without re-taking `self._lock` -
        for `_create_locked`'s cap check, which already holds it."""
        return sum(
            1 for gid in self._ui_games if gid in self._games and not self._games[gid].is_over
        )

    def created_at(self, game_id: str) -> str | None:
        """When this game was created, UTC ISO 8601 - `None` for an unknown
        game. Stamped once, in `create()`, and never touched again."""
        with self._lock:
            return self._created_at.get(game_id)

    def register_ui_game(self, game_id: str) -> None:
        """Record that the play UI's lobby booted this game.

        `DELETE /api/ui/games/{id}` (End game, `lobby.reset_game`) resets
        exactly these and nothing else, so the marker is set only by
        `POST /api/ui/games` - never inferred.
        """
        with self._lock:
            self._register_ui_game_locked(game_id)

    def ui_games(self) -> list[str]:
        """The lobby-booted games still in the store, oldest first."""
        with self._lock:
            return [game_id for game_id in self._ui_games if game_id in self._games]

    def live_ui_games(self) -> list[str]:
        """The lobby-booted games that have not ended yet, oldest first.

        `Game.is_over` (CR 104.1: a winner or a draw) is what separates a game
        still worth a seat at the table from one that only hasn't been ended
        from the lobby yet - this is what `MAX_LIVE_GAMES` counts against, so
        a finished game does not itself block a new one.
        """
        with self._lock:
            return [
                game_id
                for game_id in self._ui_games
                if game_id in self._games and not self._games[game_id].is_over
            ]

    def register_decklists(self, game_id: str, decklists: dict[str, str]) -> None:
        """Record the decklist text each seat was booted from: username -> text.

        Kept beside the game rather than on it, for the same reason the seat
        uids are: `Game` is the engine's model of a game in progress and a
        registered decklist is not part of one. The engine's `Match.decks`
        does hold the parsed lists, but this store keys everything by
        `game_id` and never sees the `Match` - `decks_api`/`serve` hand it the
        `Game` alone - so the text is recorded here at boot instead.

        Only games booted from decklists have any (`POST /api/games` builds a
        board with no decks at all), and a game with none simply has nothing
        to report.
        """
        with self._lock:
            self._decklists[game_id] = dict(decklists)

    def decklists(self, game_id: str) -> dict[str, str]:
        """username -> decklist text, for a game booted from decklists.

        Full information: `serialize.state_view` hands each seat only its own
        entry, and `visibility.redact_view` is what enforces that (CR 100.2a's
        "each player creates their own deck" is knowledge of one's own deck,
        not of the opponent's).
        """
        with self._lock:
            return dict(self._decklists.get(game_id, {}))

    def register_agent_settings(self, game_id: str, agents: dict[str, AgentSettings]) -> None:
        """Record each AI seat's `AgentSettings`: username -> settings.
        Lobby-owned.

        Kept beside the game for the same reason `register_decklists` is:
        `Game` is the engine's model of a game in progress, and which Claude
        Code model, effort and thinking budget play a seat is a fact about
        this deployment, not about the game. Only AI seats get an entry -
        `decks_api.start_game_from_decklists` builds this dict from `player.ai`
        already, so a human seat is simply absent rather than recorded as
        having none.
        """
        with self._lock:
            self._agent_settings[game_id] = dict(agents)

    def agent_settings(self, game_id: str) -> dict[str, AgentSettings]:
        """username -> `AgentSettings`, for this game's AI seats only.
        Lobby-owned."""
        with self._lock:
            return dict(self._agent_settings.get(game_id, {}))

    def register_ai_players(self, game_id: str, players: list[str]) -> None:
        """Record which seats are meant to be driven by a bot rather than a
        human at a browser. Lobby-owned.

        Kept beside the game for the same reason the seat uids are: "this seat
        has a subprocess playing it" is a fact about *this deployment*, not
        about the game, and `Game` must not learn it. The one thing the server
        does with it is `ai_pregame.py`'s backstop, which will make a seat's
        own CR 103.5 declaration for it - and must therefore never be able to
        confuse a bot seat with the human's.
        """
        with self._lock:
            self._ai_players[game_id] = set(players)

    def ai_players(self, game_id: str) -> set[str]:
        """The usernames registered as bot-driven. Lobby-owned. Empty for
        every game that did not say so - a human seat is never inferred into
        this set."""
        with self._lock:
            return set(self._ai_players.get(game_id, set()))

    def record_automatic_pregame(self, game_id: str, player: str, kind: str) -> None:
        """Remember that `kind` (`"mulligan"` or `"opening_hand"`) was taken
        *for* `player` by `ai_pregame.py`'s backstop rather than by the seat.

        This is what lets a late declaration from the seat itself be refused
        with the truth instead of the engine's bare "has already kept a hand".
        The seat did not already keep a hand; something kept one on its
        behalf, and those are different sentences to an agent whose only
        feedback is the response body. See `opening.py`'s `PregameOverruled`.
        """
        with self._lock:
            self._automatic_pregame.setdefault(game_id, {}).setdefault(player, set()).add(kind)

    def automatic_pregame(self, game_id: str, player: str) -> set[str]:
        """The pregame moves the backstop made for this seat, if any."""
        with self._lock:
            return set(self._automatic_pregame.get(game_id, {}).get(player, set()))

    @contextmanager
    def locked(self, game_id: str) -> Iterator[Game]:
        """Hold `game_id`'s mutation lock for a whole request, yielding the
        `Game`. Raises `KeyError` for an unknown game before taking any lock.

        This is the lock the class docstring describes. Nothing parks inside
        it any more - `commands.run_command` never blocks, so a request that
        answers a paused decision (`POST .../decide`) takes this lock for its
        whole body exactly like any other mutating request.
        """
        with self._lock:
            game = self._games.get(game_id)
            lock = self._locks.get(game_id)
        if game is None or lock is None:
            raise KeyError(f"no game {game_id}")
        with lock:
            yield game

    def store_paused_command(
        self,
        game_id: str,
        command: Command,
        tape: tuple[Answer, ...],
        pending: PendingDecision,
    ) -> None:
        with self._lock:
            self._paused_commands[game_id] = (command, tape, pending)
            self._pause_generations[game_id] = self._pause_generations.get(game_id, 0) + 1

    def pause_generation(self, game_id: str) -> int:
        with self._lock:
            return self._pause_generations.get(game_id, 0)

    def paused_command(
        self, game_id: str
    ) -> tuple[Command, tuple[Answer, ...], PendingDecision] | None:
        with self._lock:
            return self._paused_commands.get(game_id)

    def clear_paused_command(self, game_id: str) -> None:
        with self._lock:
            self._paused_commands.pop(game_id, None)

    def admin_key(self, game_id: str) -> str | None:
        """This game's admin key, to hand to whoever just created it."""
        with self._lock:
            return self._admin_keys.get(game_id)

    def is_admin(self, game_id: str, key: str) -> bool:
        """Whether `key` is this game's admin key. A missing key is never one."""
        with self._lock:
            return bool(key) and self._admin_keys.get(game_id) == key

    def owner_user_id(self, game_id: str) -> str | None:
        with self._lock:
            return self._owner_user_id.get(game_id)

    def register_seats(self, game_id: str, uids: dict[str, str]) -> None:
        """Record `uids` (uid -> username) as this game's seat credentials."""
        with self._lock:
            self._seats[game_id] = dict(uids)

    def seat_uids(self, game_id: str) -> dict[str, str]:
        """uid -> username, for whoever is dispatching the players."""
        with self._lock:
            return dict(self._seats.get(game_id, {}))

    def player_for_uid(self, game_id: str, uid: str) -> str | None:
        """Whose seat this uid is, or None when the uid is not in this game."""
        with self._lock:
            return self._seats.get(game_id, {}).get(uid)

    def register_advisors(self, game_id: str, advisors: dict[str, Advisor]) -> None:
        """Record `advisors` (uid -> `Advisor`) as this game's advisor
        credentials, replacing whatever was there."""
        with self._lock:
            self._advisors[game_id] = dict(advisors)

    def advisor_uids(self, game_id: str) -> dict[str, Advisor]:
        """uid -> `Advisor`, for this game."""
        with self._lock:
            return dict(self._advisors.get(game_id, {}))

    def get(self, game_id: str) -> Game | None:
        with self._lock:
            return self._games.get(game_id)

    def replace(self, game_id: str, game: Game, *, record: dict[str, Any] | None = None) -> int:
        with self._lock:
            if game_id not in self._games:
                raise KeyError(f"no game {game_id}")
            self._games[game_id] = game
            return self._append_record_locked(game_id, record or {})

    def delete(self, game_id: str) -> bool:
        # `self._records` is deliberately NOT popped here (T10, matching
        # `db.py`'s dropped `ForeignKey` on the postgres side) - the audit
        # trail outlives the game it describes, on purpose: a reset's own
        # `game_reset` row is written just before this call
        # (`lobby.reset_game`), and it must still be there to read
        # afterward, or "the transcript ends with the reset" is a lie.
        #
        # Nor is the game itself thrown away any more: it moves into
        # `self._archived`, which is the whole of this store's soft delete.
        # Every live collection is still popped, so `get`/`ids`/`ui_games`/
        # `locked`/`seat_uids`/`admin_key` keep answering exactly what they
        # answered when this really did destroy the game - only `archived()`
        # can see what was kept.
        with self._lock:
            game = self._games.pop(game_id, None)
            if game is not None:
                self._archived[game_id] = ArchivedGame(
                    game_id=game_id,
                    game=game,
                    admin_key=self._admin_keys.get(game_id),
                    seat_uids=dict(self._seats.get(game_id, {})),
                    created_at=self._created_at.get(game_id),
                    deleted_at=datetime.now(UTC).isoformat(),
                )
            self._seats.pop(game_id, None)
            self._advisors.pop(game_id, None)
            self._admin_keys.pop(game_id, None)
            self._owner_user_id.pop(game_id, None)
            self._created_at.pop(game_id, None)
            self._locks.pop(game_id, None)
            self._paused_commands.pop(game_id, None)
            self._pause_generations.pop(game_id, None)
            self._decklists.pop(game_id, None)
            self._agent_settings.pop(game_id, None)
            self._ai_players.pop(game_id, None)
            self._automatic_pregame.pop(game_id, None)
            if game_id in self._ui_games:
                self._ui_games.remove(game_id)
            return game is not None

    def archived(self, game_id: str) -> ArchivedGame | None:
        with self._lock:
            ended = self._archived.get(game_id)
            if ended is not None:
                return ended
        # Through `get`, outside the lock (which is not reentrant), rather
        # than off `self._games` directly: this class never decodes anything
        # and so never refuses a snapshot of its own, but `archived()` is
        # specified to report such a refusal rather than raise it, and the
        # cheapest way to be sure this implementation upholds that is to
        # read the game the same way every other caller does and answer the
        # refusal here if one ever arrives.
        unloadable: str | None = None
        try:
            game = self.get(game_id)
        except GameUnloadable as exc:
            game, unloadable = None, exc.reason
        with self._lock:
            if game is None and unloadable is None:
                return None
            return ArchivedGame(
                game_id=game_id,
                game=game,
                admin_key=self._admin_keys.get(game_id),
                seat_uids=dict(self._seats.get(game_id, {})),
                created_at=self._created_at.get(game_id),
                deleted_at=None,
                unloadable=unloadable,
            )

    def _append_record_locked(self, game_id: str, payload: dict[str, Any]) -> int:
        """`append_record`'s own body, called with `self._lock` already
        held - `replace()` needs this, since it must not release the lock
        between mutating `self._games` and appending this commit's row."""
        log = self._records.setdefault(game_id, [])
        seq = len(log) + 1
        log.append(
            {"seq": seq, "payload": dict(payload), "created_at": datetime.now(UTC).isoformat()}
        )
        return seq

    def append_record(self, game_id: str, payload: dict[str, Any]) -> int:
        with self._lock:
            return self._append_record_locked(game_id, payload)

    def fill_record(self, game_id: str, seq: int, payload: dict[str, Any]) -> None:
        with self._lock:
            for entry in self._records.get(game_id, []):
                if entry["seq"] == seq:
                    entry["payload"] = dict(payload)
                    return
            raise KeyError(f"no records row {game_id}/{seq}")

    def records(self, game_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {**entry, "payload": dict(entry["payload"])}
                for entry in self._records.get(game_id, [])
            ]

    def recent_records(self, game_id: str, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            # `[-limit:]` on a list is the in-memory twin of postgres's
            # `ORDER BY seq DESC LIMIT` - the point of the method is that
            # neither implementation's cost grows with the ledger.
            window = self._records.get(game_id, [])[-limit:] if limit > 0 else []
            return [{**entry, "payload": dict(entry["payload"])} for entry in window]

    def records_since(self, game_id: str, since: int) -> list[dict[str, Any]]:
        with self._lock:
            log = self._records.get(game_id, [])
            # A binary search for the first row past `since`, then a slice of
            # the tail - the in-memory twin of postgres's `WHERE seq >
            # :since` served by the `(game_id, seq)` index, and bounded for
            # the same reason. `mise run test` runs against this
            # implementation, so the cheap path has to be the one the tests
            # actually exercise. `bisect` rather than `log[since:]` because
            # the arithmetic shortcut would silently return the wrong window
            # the day `seq` stops being this list's own 1-based index; the
            # sortedness it does rely on is `append`-order, which
            # `_append_record_locked` guarantees.
            tail = log[bisect_right(log, since, key=lambda row: row["seq"]) :]
            return [{**entry, "payload": dict(entry["payload"])} for entry in tail]

    def max_record_seq(self, game_id: str) -> int:
        with self._lock:
            log = self._records.get(game_id, [])
            return int(log[-1]["seq"]) if log else 0

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._games)
