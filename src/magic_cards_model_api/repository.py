"""The `GameRepository` seam: what the API needs from wherever games live,
and `InMemoryRepository`, the in-memory implementation that seam replaces
`store.GameStore`'s body with.

`GameRepository` is a `typing.Protocol` - structural, not a base class to
inherit from. It covers the surface every one of this store's 12 in-repo
callers and 20+ test files actually uses: game lifecycle (`create`, `get`,
`delete`, `ids`), the mutation boundary (`locked`), seat credentials
(`register_seats`, `seat_uids`, `player_for_uid`, `admin_key`, `is_admin`),
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
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
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
        """The game for this id, or `None` if there is none."""
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
        """Remove this game and everything recorded beside it. Returns
        whether a game actually existed to remove."""
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
        before taking any lock.

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
        with self._lock:
            self._seats.pop(game_id, None)
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
            return self._games.pop(game_id, None) is not None

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

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._games)
