"""`PostgresRepository` (T9): the `GameRepository` protocol (`repository.py`)
backed by real postgres, for a stateless multi-replica deployment instead of
`InMemoryRepository`'s process-local dict.

One deliberate exception to "stateless": `reserve_slot`/`attach`/`release`'s
reservation ledger (M8) is process-local, like `_locks`/`_automatic_pregame`
below - see `__init__`'s note on why that is still the right shape for as
long as this whole phase deploys the lobby at `replicas: 1` (the 2b plan's
M9), and `T17` for when it stops being one.

**Two different concurrency techniques for two different problems**, per the
design gate's decision 5 (`docs/superpowers/plans/2026-09-07-api-pause-model-
and-postgres.md`):

- **Gameplay mutation** (`replace()`) is optimistic: `version` is a
  compare-and-swap counter, `UPDATE games SET state = ..., version =
  version + 1 WHERE id = :id AND version = :expected`. A `rowcount` of 0
  means somebody else committed first - this raises `VersionConflict`
  carrying the row as it now stands, and there is deliberately no
  server-side retry (a stale write's caller already computed a `Game` against
  state that is no longer current; retrying here would silently replay a
  decision the seat never actually made against the new state). The caller
  re-reads and re-applies, same as an HTTP 409 tells a client to. No
  `SELECT FOR UPDATE`, no advisory lock: both hold for however long the
  transaction is open, and a paused command can sit for as long as a human
  seat takes to answer - a lock held that long is a lock that starves every
  other request against the same row.
- **`reserve_slot`'s cap check (M8)** used to be `create_if_under`'s
  transaction-scoped `LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE` around one
  count query plus one insert. It is now a plain Python `threading.Lock`
  (`_reservations_lock`) around one count query plus an in-memory ledger
  update, no DB-level lock at all - correct precisely because the ledger it
  protects is itself process-local (see the module docstring's opening
  note): the only concurrent writers `_reservations_lock` needs to serialise
  against are other threads of *this* process, which a Python lock already
  does, without needing postgres to enforce it table-wide. `T17` reintroduces
  a real DB-level constraint once the reservation is a durable, cross-replica
  row instead.

**The identity-preserving read cache.** `locked()`/`get()` hand back the
*same* `Game` object across repeated calls in this process, as long as
nothing else has written a newer `version` - required by the contract suite
(`test_repository_contract.py`'s `... is game` assertions, unconditional
across every `_REPOSITORY_FACTORIES` entry) and cheap to keep true: a read
first checks the row's `version` against what this instance already holds
cached, and only re-parses `state` into a fresh `Game` when a different
process's write has actually landed. A single process's own writes
(`replace()`) update the cache directly rather than re-reading, so nothing
here ever reflects staleness of its *own* last write - only of a write made
elsewhere, which is exactly the case optimistic concurrency exists to catch
at the next `replace()`, not at the next `get()`.

**Paused-command storage.** `command`/`tape`/`pending` are plain columns on
the same row, updated by ordinary (non-CAS) `UPDATE`s - `store_paused_command`
/`clear_paused_command` are always called from inside the same already
`locked()` block a command ran under (see `repository.py`'s docstrings), so
within one process they are already serialised by `locked()`'s per-game
`threading.Lock`; nothing here claims they are safe against a *second*
process racing the same game's pause state, which is not a scenario any
caller in this codebase creates (`app.py` never releases `locked()` and then
separately calls `store_paused_command`).

`Decision`/`PendingDecision` are plain dataclasses, not pydantic - unlike
`Answer` (frozen pydantic, built precisely so the tape survives
`Game.model_dump(mode="json")` round-tripping), `Decision` was never made to
round-trip JSON on its own. `_encode_dataclass`/`_decode_dataclass` below are
this module's codec for them, driven by `dataclasses.fields` and each field's
resolved type annotation rather than by a hand-written key list: the
annotation says exactly what to rebuild (`tuple[UUID, ...]` comes back as a
tuple of `UUID`s, never a list of strings), and a field the engine adds is
covered the moment it exists. A hand-written list was what broke production
on 2026-09-14 (game 07c5f818): it predated `Decision.quality`,
`mana_sources`, `lethal_damage`, `default_damage` and `trample`, so every
as-enters choice read back unequal to its own replay and every `/decide`
answered 409 "replay diverged". A field whose type has no encoding rule is a
`TypeError` naming the field at write time, never a silent drop; a stored row
that predates a field decodes with that field's dataclass default.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import Enum
from types import NoneType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints
from uuid import UUID, uuid4

from magic_cards_model.machines.decisions import Answer, Decision, PendingDecision
from magic_cards_model.models import Game
from pydantic import BaseModel
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from .agent_settings import AgentSettings
from .commands import Command
from .db import games, make_engine, record_states, records
from .repository import (
    DEFAULT_RESERVATION_TTL_SECONDS,
    Advisor,
    ArchivedGame,
    GameUnloadable,
    _ReservationLedger,
    decode_record_state,
    encode_record_state,
)

__all__ = ["PostgresRepository", "VersionConflict"]

#: Every refusal to decode a stored snapshot is written here, at WARNING,
#: before it is raised - once per (game, version), not once per read, so a
#: 1.2s lobby poll against a stale row does not bury the log. Silence is
#: the one outcome this must never have: a row that stops loading is a
#: game somebody was playing.
log = logging.getLogger(__name__)


def _utc_isoformat(stamp: datetime | None) -> str | None:
    """One `DateTime(timezone=True)` column as UTC ISO 8601, or `None`.

    A naive value is read as UTC rather than as local time: every stamp this
    module writes is `datetime.now(UTC)`, and some drivers/backends hand a
    timestamp back with its tzinfo stripped.
    """
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat()


def _record_row(row: Any) -> dict[str, Any]:
    """One `records` row in the `{"seq", "payload", "created_at"}` shape the
    `GameRepository` protocol promises."""
    return {
        "seq": row.seq,
        "payload": row.payload,
        "created_at": _utc_isoformat(row.created_at),
    }


def _looks_like_id(game_id: str) -> bool:
    """Whether `game_id` is even syntactically a uuid - every id this
    repository itself mints (`uuid4().hex`) is, but a caller (or a contract-
    suite test) probing for an id that was never created at all commonly
    passes a plain sentinel string like `"no-such-game"`, which is not. The
    `id` column is plain `String` (see `db.py`'s module docstring for why),
    so a query against it with a non-uuid string is not a driver error - it
    is just a normal lookup that matches no row. This check exists purely to
    short-circuit that lookup before it reaches the driver at all, saving a
    round trip on every probe with an id that cannot possibly exist."""
    try:
        UUID(game_id)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


class VersionConflict(Exception):
    """Raised by `PostgresRepository.replace` when the CAS `UPDATE ... WHERE
    version = :expected` matched no row: another writer (another process, or
    another `PostgresRepository` instance in this one - the object identity
    that changes, not necessarily the machine) committed a newer version
    first.

    Carries the row as it now actually stands, so the loser can re-read
    (`get`) and decide whether to recompute and re-apply its attempt - the
    same thing an HTTP 409 response body exists to let a client do. There is
    no server-side retry: see the module docstring for why retrying here
    would be silently wrong rather than merely inefficient.
    """

    def __init__(
        self,
        game_id: str,
        expected_version: int,
        actual_version: int,
        current_game: Game,
    ) -> None:
        super().__init__(
            f"game {game_id}: expected version {expected_version}, "
            f"found {actual_version} - another writer committed first"
        )
        self.game_id = game_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.current_game = current_game


# ---------------------------------------------------------------------------
# Decision / PendingDecision <-> JSON, driven by each dataclass's own fields
# and type annotations - see the module docstring for why.
# ---------------------------------------------------------------------------

_SCALARS = (str, int, bool)


def _optional_inner(annotation: Any) -> Any | None:
    """`X` for an `X | None` annotation, `None` for anything else. Any other
    union is ambiguous to decode and refused by `_require_encodable`."""
    if get_origin(annotation) not in (Union, UnionType):
        return None
    args = get_args(annotation)
    inner = [arg for arg in args if arg is not NoneType]
    return inner[0] if len(args) == 2 and len(inner) == 1 else None


def _require_encodable(annotation: Any, where: str) -> None:
    """Refuse, by name, a field type this codec has no exact JSON rule for.

    Checked against the annotation, not the value, so an unencodable field
    fails even while it holds `None` or its empty default."""
    inner = _optional_inner(annotation)
    if inner is not None:
        _require_encodable(inner, where)
        return
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        _require_encodable(args[0], f"{where}[]")
        return
    if origin is tuple and args and Ellipsis not in args:
        for index, arg in enumerate(args):
            _require_encodable(arg, f"{where}[{index}]")
        return
    if origin is None and isinstance(annotation, type):
        if annotation in _SCALARS or annotation is UUID:
            return
        if issubclass(annotation, (Enum, BaseModel)) or dataclasses.is_dataclass(annotation):
            return
    raise TypeError(f"{where}: no JSON encoding for field type {annotation!r}")


@functools.cache
def _codec_fields(cls: type) -> tuple[tuple[dataclasses.Field[Any], Any], ...]:
    """Every field of dataclass `cls` with its resolved annotation, each one
    already checked encodable."""
    hints = get_type_hints(cls)
    resolved = tuple((f, hints[f.name]) for f in dataclasses.fields(cls))
    for f, annotation in resolved:
        _require_encodable(annotation, f"{cls.__name__}.{f.name}")
    return resolved


def _encode_value(value: Any, annotation: Any) -> Any:
    inner = _optional_inner(annotation)
    if inner is not None:
        return None if value is None else _encode_value(value, inner)
    args = get_args(annotation)
    if get_origin(annotation) is tuple:
        if args[-1] is Ellipsis:
            return [_encode_value(item, args[0]) for item in value]
        return [_encode_value(item, arg) for item, arg in zip(value, args, strict=True)]
    if annotation is UUID:
        return str(value)
    if issubclass(annotation, Enum):
        return value.value
    if issubclass(annotation, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(annotation):
        return _encode_dataclass(value)
    return value


def _decode_value(data: Any, annotation: Any) -> Any:
    inner = _optional_inner(annotation)
    if inner is not None:
        return None if data is None else _decode_value(data, inner)
    args = get_args(annotation)
    if get_origin(annotation) is tuple:
        if args[-1] is Ellipsis:
            return tuple(_decode_value(item, args[0]) for item in data)
        return tuple(_decode_value(item, arg) for item, arg in zip(data, args, strict=True))
    if annotation is UUID or issubclass(annotation, Enum):
        return annotation(data)
    if issubclass(annotation, BaseModel):
        return annotation.model_validate(data)
    if dataclasses.is_dataclass(annotation):
        return _decode_dataclass(annotation, data)
    return data


def _encode_dataclass(obj: Any) -> dict[str, Any]:
    return {
        f.name: _encode_value(getattr(obj, f.name), annotation)
        for f, annotation in _codec_fields(type(obj))
    }


def _decode_dataclass[T](cls: type[T], data: dict[str, Any]) -> T:
    """`cls` rebuilt from `_encode_dataclass`'s output. A key the row lacks
    takes the field's dataclass default - rows written before the field
    existed - and only a field with no default is an error."""
    kwargs: dict[str, Any] = {}
    for f, annotation in _codec_fields(cls):
        if f.name in data:
            kwargs[f.name] = _decode_value(data[f.name], annotation)
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise KeyError(f"{cls.__name__}.{f.name} is missing from the stored row")
    return cls(**kwargs)


def _decision_to_json(decision: Decision) -> dict[str, Any]:
    return _encode_dataclass(decision)


def _decision_from_json(data: dict[str, Any]) -> Decision:
    return _decode_dataclass(Decision, data)


def _pending_to_json(pending: PendingDecision) -> dict[str, Any]:
    return _encode_dataclass(pending)


def _pending_from_json(data: dict[str, Any]) -> PendingDecision:
    return _decode_dataclass(PendingDecision, data)


class PostgresRepository:
    """The `GameRepository` protocol, backed by the `games`/`records` tables
    `db.py` defines. Structural, like `InMemoryRepository` - satisfies
    `GameRepository` by having the same methods, not by inheriting from it.
    """

    #: See `repository.DEFAULT_RESERVATION_TTL_SECONDS` and
    #: `InMemoryRepository`'s own copy of this attribute - same name, same
    #: reasoning, so a test can shrink either implementation's TTL
    #: identically through the shared contract suite.
    RESERVATION_TTL_SECONDS: float = DEFAULT_RESERVATION_TTL_SECONDS

    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        #: `engine` lets a caller (the contract-suite factory, in
        #: particular) share one connection pool across many
        #: `PostgresRepository` instances in one test session rather than
        #: opening a fresh pool per instance - the identity-preserving cache
        #: below is still per-instance either way, which is what a test
        #: simulating two separate server processes against one database
        #: needs.
        self._engine = engine if engine is not None else make_engine(database_url)
        # game_id -> (last-known Game, last-known version), this process's
        # own view. See the module docstring's "identity-preserving read
        # cache" section.
        self._cache: dict[str, tuple[Game, int]] = {}
        # game_id -> (version, the decoder's own message) for a row this
        # build refuses to parse - the cache's negative half, guarded by the
        # same `_cache_lock` and keyed on `version` for the same reason, so
        # a stale row is parsed (and logged) once rather than on every poll,
        # and is retried the instant another writer moves its version. See
        # `_game_from_row`.
        self._undecodable: dict[str, tuple[int, str]] = {}
        self._cache_lock = threading.Lock()
        # game_id -> this process's mutation lock for that game. Process-
        # local, like `InMemoryRepository._locks` - not part of the
        # `GameRepository` protocol, only `locked()` itself is.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        # game_id -> username -> the pregame moves `ai_pregame.py`'s backstop
        # made *for* that seat. Process-local, like `_locks` above and like
        # the backstop itself (`ai_pregame.watch` is a thread in this
        # process, `AiSeatRegistry` a dict in it): a second replica has its
        # own watcher and its own view of what it did, and one replica's
        # backstop cannot make a declaration the other replica is the one to
        # explain. Persisting this would be the wrong fix for that; the
        # right one is the six-way cut, which moves the watcher out of the
        # request path entirely. Kept here so `PostgresRepository` offers
        # the same surface `InMemoryRepository` does - `opening.py` calls
        # both of these on whichever store `store.py` selected.
        self._automatic_pregame: dict[str, dict[str, set[str]]] = {}
        # game_id -> (player, kind) -> the notice for a pregame move the
        # backstop found owed by a seat nothing is playing and REFUSED to
        # make, the answer not being forced. Process-local for exactly the
        # reason `_automatic_pregame` above is: the watcher that writes it is
        # a thread in this process.
        self._pregame_stalls: dict[str, dict[tuple[str, str], dict[str, str]]] = {}
        # M8: pending reservations against `MAX_LIVE_GAMES`, keyed by token.
        # Process-local, like `_locks`/`_automatic_pregame` above and for the
        # same underlying reason: this whole phase deploys the lobby at
        # `replicas: 1` (the 2b plan's M9) precisely because other lobby
        # state (the pregame watcher, `AiSeatRegistry`) is still
        # process-local too, so a reservation racing only within this one
        # process is already the real deployment target - a durable,
        # cross-replica reservation is T17's job (its own `lobby_games`
        # table and a single atomic `INSERT ... WHERE count(*) < cap`), once
        # the lobby is its own service with its own database. A separate
        # lock from `_locks_lock`/`_cache_lock`: a reservation decision holds
        # it across a real DB round trip (`_live_ui_game_ids`) - see
        # `reserve_slot`.
        self._reservations = _ReservationLedger()
        self._reservations_lock = threading.Lock()

    # -- internal helpers ---------------------------------------------------

    def _game_from_row(self, game_id: str, state: Any, version: int) -> Game:
        """The identity-preserving cache's single write/read point: `state`
        is only ever re-parsed into a fresh `Game` when `version` has moved
        past what this instance already has cached - otherwise the same
        cached object comes back, in-place mutations (a caller that never
        called `replace()`, exactly like a direct `InMemoryRepository` caller
        never has to) included. See the module docstring's "identity-
        preserving read cache" section.

        **A row that will not decode raises `GameUnloadable`, and that is the
        only place a decode failure is ever turned into one.** Every read on
        this class comes through here, so one `try` covers `get`, `locked`,
        `archived` and `_live_ui_game_ids` alike. The failure is cached the
        same way a success is - keyed on `version`, so a row that is fixed by
        another writer is retried the moment its version moves, and a stale
        row that nothing touches is parsed once rather than on every tick of
        a 1.2s lobby poll (a full `Game` payload is ~180KB; see `MEMORY.md`).
        That cache is also what makes the WARNING fire once per (game,
        version) instead of once per read.

        `except Exception` and not `except SchemaVersionMismatch`: the schema
        bump is the case this was written for, but "this row will not decode"
        is the property callers actually need to survive, and a
        `ValidationError` from a genuinely corrupt payload has to be just as
        non-fatal to a listing. The engine's own message - whatever raised it
        - is carried through verbatim as `reason` and is what every layer
        above quotes; nothing here paraphrases it or decides what it meant.
        """
        with self._cache_lock:
            cached = self._cache.get(game_id)
            if cached is not None and cached[1] == version:
                return cached[0]
            failed = self._undecodable.get(game_id)
            if failed is not None and failed[0] == version:
                raise GameUnloadable(game_id, failed[1])
            try:
                game = Game.model_validate(state)
            except Exception as exc:
                reason = str(exc)
                self._undecodable[game_id] = (version, reason)
                self._cache.pop(game_id, None)
                log.warning(
                    "game %s (version %s) cannot be decoded by this build and will be "
                    "skipped by every listing until it is: %s",
                    game_id,
                    version,
                    reason,
                )
                raise GameUnloadable(game_id, reason) from exc
            self._undecodable.pop(game_id, None)
            self._cache[game_id] = (game, version)
            return game

    def _read_current(self, game_id: str) -> tuple[Game, int]:
        """This row's `Game` and `version`. Raises `KeyError` when the id
        does not exist - including when it is not even a syntactically valid
        uuid (the contract suite's "no-such-game" sentinels are not, and
        `_looks_like_id` short-circuits those without a DB round trip)."""
        if not _looks_like_id(game_id):
            raise KeyError(f"no game {game_id}")
        with self._engine.connect() as conn:
            row = conn.execute(
                select(games.c.state, games.c.version).where(
                    games.c.id == game_id, games.c.archived_at.is_(None)
                )
            ).first()
        if row is None:
            raise KeyError(f"no game {game_id}")
        state, version = row
        return self._game_from_row(game_id, state, version), version

    def _live_ui_game_ids(self, conn: Any) -> list[str]:
        """`ui_games()`, filtered to `not Game.is_over`, oldest-registered
        first - the shared logic `live_ui_games()` and `reserve_slot`'s cap
        check both need. Takes an open `conn` rather than opening its own
        (`reserve_slot` calls this from inside a connection it already has
        open; there is no reason to check a second one out of the pool for a
        read the held connection can just as well serve).

        Deliberately **not** a `state->>'winner'` SQL predicate: `is_over`
        is evaluated in Python, through `_game_from_row`'s cache, so a game
        this process already holds - mutated in place since its last
        `replace()`, exactly the way a direct `InMemoryRepository` caller is
        allowed to - is judged by that mutation rather than by whatever was
        last actually written to the row. That is what keeps this
        postgres-backed store observably identical to `InMemoryRepository`
        for a caller in the same process, which the contract suite (built
        against `InMemoryRepository`'s shared-reference semantics) assumes.

        **A row that will not decode counts as live, and never raises.**
        Not raising is the whole point: this is the single count
        `reserve_slot` makes, so one stale row used to break *creating* a
        game as surely as listing one. Counting it is the deliberate half
        - `is_over` (CR 104.1) is a fact about a board this build cannot
        read, and treating an unreadable game as finished would be the
        same silent default the engine refuses to make when it declines
        the snapshot in the first place. The row is there, `archived_at`
        is null, the lobby still lists it, and the slot it holds is real.
        What keeps that from wedging the cap forever is that End game
        works on an undecodable row exactly as it does on any other
        (`lobby.reset_game`, and `delete()`'s own `UPDATE` decodes
        nothing) - so the cap is always recoverable by the one deliberate
        act that was always the way to recover it.
        """
        rows = conn.execute(
            select(games.c.id, games.c.state, games.c.version)
            .where(games.c.ui_game.is_(True), games.c.archived_at.is_(None))
            .order_by(games.c.ui_game_registered_at.asc())
        ).all()
        live = []
        for game_id, state, version in rows:
            try:
                over = self._game_from_row(game_id, state, version).is_over
            except GameUnloadable:
                over = False
            if not over:
                live.append(game_id)
        return live

    def _cached_version(self, game_id: str) -> int:
        with self._cache_lock:
            cached = self._cache.get(game_id)
        if cached is not None:
            return cached[1]
        return self._read_current(game_id)[1]

    def _lock_for(self, game_id: str) -> threading.Lock:
        with self._locks_lock:
            lock = self._locks.get(game_id)
        if lock is not None:
            return lock
        self._read_current(game_id)  # KeyError before any lock is minted
        with self._locks_lock:
            return self._locks.setdefault(game_id, threading.Lock())

    def _update_column(self, game_id: str, **values: Any) -> None:
        # `archived_at IS NULL`: an ended game is not writable through any
        # of these accessors, matching `InMemoryRepository`, whose `delete`
        # pops the collections they read outright.
        if not _looks_like_id(game_id):
            return
        with self._engine.begin() as conn:
            conn.execute(
                update(games)
                .where(games.c.id == game_id, games.c.archived_at.is_(None))
                .values(**values)
            )

    def _select_one(self, game_id: str, *columns: Any) -> Any | None:
        """One row (or `None`) from `games`, projected to `columns` - the
        shared read path every plain accessor below uses. `None` both for an
        unknown id and for an id that is not even valid uuid syntax, which a
        native `uuid` `id` column would otherwise reject at the driver.

        Also `None` for a game that has been ended: `delete()` is a soft
        delete, and every accessor built on this one must answer for an
        archived game exactly what it answered when the row was really gone
        (`InMemoryRepository.delete` still pops all of these). `archived()`
        is the single method that deliberately does not go through here.
        """
        if not _looks_like_id(game_id):
            return None
        with self._engine.connect() as conn:
            return conn.execute(
                select(*columns).where(games.c.id == game_id, games.c.archived_at.is_(None))
            ).first()

    # -- lifecycle ------------------------------------------------------

    def create(self, game: Game, *, owner_user_id: str | None = None) -> str:
        game_id = uuid4().hex
        now = datetime.now(UTC)
        admin_key = uuid4().hex
        with self._engine.begin() as conn:
            conn.execute(
                insert(games).values(
                    id=game_id,
                    state=game.model_dump(mode="json"),
                    command=None,
                    tape=None,
                    pending=None,
                    version=0,
                    created_at=now,
                    admin_key=admin_key,
                    owner_user_id=owner_user_id,
                    seats={},
                    advisors={},
                    scripts={},
                    decklists={},
                    agent_settings={},
                    ai_players=[],
                    ui_game=False,
                    ui_game_registered_at=None,
                )
            )
        with self._cache_lock:
            self._cache[game_id] = (game, 0)
        with self._locks_lock:
            self._locks[game_id] = threading.Lock()
        return game_id

    def reserve_slot(self, cap: int) -> str | None:
        """M8: claim one of `cap` lobby slots against this process's
        in-memory reservation ledger plus a fresh read of how many `games`
        rows are actually live - see `__init__`'s note on why the ledger
        itself is process-local. `_reservations_lock` (a plain Python lock,
        not the DB's own `LOCK TABLE` `create_if_under` used to take) is
        sufficient here precisely because the whole decision - the DB read
        and the ledger update - happens while this one process's lock is
        held, so no other thread in this process can interleave a
        conflicting decision; a second replica racing the same table is a
        scenario `replicas: 1` (M9) does not create."""
        with self._reservations_lock:
            with self._engine.connect() as conn:
                live = len(self._live_ui_game_ids(conn))
            return self._reservations.reserve(
                live_count=live, cap=cap, ttl_seconds=self.RESERVATION_TTL_SECONDS
            )

    def attach(self, token: str, game_id: str) -> None:
        """Resolve `token` and register `game_id` as a live ui game as one
        step under `_reservations_lock`, so a concurrent `reserve_slot`'s DB
        read can never land in the gap between the two - the same reasoning
        `InMemoryRepository.attach` gives for doing both under one lock."""
        with self._reservations_lock:
            if not self._reservations.resolve(token, ttl_seconds=self.RESERVATION_TTL_SECONDS):
                raise KeyError(f"no such reservation {token}")
            self.register_ui_game(game_id)

    def release(self, token: str) -> None:
        with self._reservations_lock:
            self._reservations.resolve(token, ttl_seconds=self.RESERVATION_TTL_SECONDS)

    def get(self, game_id: str) -> Game | None:
        try:
            game, _version = self._read_current(game_id)
        except KeyError:
            return None
        return game

    def _next_seq(self, conn: Any, game_id: str) -> int:
        """The next `records.seq` for `game_id`, serialised against every
        other writer of this game's records with a transaction-scoped
        advisory lock (`pg_advisory_xact_lock`, released automatically at
        commit/rollback - the same reasoning `create_if_under`'s `LOCK TABLE`
        gives for never taking a session-scoped lock that could leak across a
        pooled connection, just narrowed to one game's own rows rather than
        the whole table).

        T10 decouples `records.seq` from `games.version`: T9 had every row
        come from a successful `replace()`, so `seq == new_version` held by
        construction. Once a refused action that never touches `games` at
        all (a `ValueError` caught before any command ran, an early
        `_answer_declaration` refusal, an admin action) also needs a row -
        the whole point of this task - a strict `seq == version` mapping is
        no longer expressible: the same game can commit fewer `games` writes
        than `records` rows. `seq` is now what `audit.py`'s old in-memory
        log always called it, a plain per-game append order, computed here
        from the table itself rather than borrowed from another column.
        See `MEMORY.md` for the T9 note this supersedes.
        """
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:gid))"), {"gid": game_id})
        current = conn.execute(
            select(func.coalesce(func.max(records.c.seq), 0)).where(records.c.game_id == game_id)
        ).scalar_one()
        return int(current) + 1

    def replace(self, game_id: str, game: Game, *, record: dict[str, Any] | None = None) -> int:
        expected_version = self._cached_version(game_id)
        new_version = expected_version + 1
        new_state = game.model_dump(mode="json")
        now = datetime.now(UTC)
        conflict: VersionConflict | None = None
        missing = False
        seq = 0
        with self._engine.begin() as conn:
            result = conn.execute(
                update(games)
                .where(games.c.id == game_id, games.c.version == expected_version)
                .values(state=new_state, version=new_version)
            )
            if result.rowcount == 0:
                row = conn.execute(
                    select(games.c.version, games.c.state).where(games.c.id == game_id)
                ).first()
                if row is None:
                    missing = True
                else:
                    actual_version, actual_state = row
                    # Through `_game_from_row`, not a bare `model_validate`:
                    # it is the one decode point (so a winner's row this
                    # build cannot read raises `GameUnloadable` here too,
                    # rather than a bare `RuntimeError`), and it already
                    # does the cache write this used to do by hand.
                    current_game = self._game_from_row(game_id, actual_state, actual_version)
                    conflict = VersionConflict(
                        game_id, expected_version, actual_version, current_game
                    )
            else:
                # Same transaction as the `games` CAS above - T9's own
                # guarantee, unchanged. `record` (T10) is the caller's full
                # payload when it already has one ready, `{}` (a placeholder
                # `fill_record` fills in later) when it does not yet - see
                # this method's own docstring.
                seq = self._next_seq(conn, game_id)
                conn.execute(
                    insert(records).values(
                        game_id=game_id, seq=seq, payload=record or {}, created_at=now
                    )
                )
        if missing:
            raise KeyError(f"no game {game_id}")
        if conflict is not None:
            raise conflict
        with self._cache_lock:
            self._cache[game_id] = (game, new_version)
            # This process just wrote a state it can obviously decode, so
            # any recorded refusal for this row is stale by construction.
            self._undecodable.pop(game_id, None)
        return seq

    def append_record(self, game_id: str, payload: dict[str, Any]) -> int:
        # No existence check, deliberately: `records.game_id` carries no
        # `ForeignKey` (`db.py`'s own docstring) and never raises for an
        # unknown id - see the protocol's own docstring on this method for
        # why (`audit.record`'s long-standing leniency, more than one
        # existing caller depends on it).
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            seq = self._next_seq(conn, game_id)
            conn.execute(
                insert(records).values(game_id=game_id, seq=seq, payload=payload, created_at=now)
            )
        return seq

    def fill_record(self, game_id: str, seq: int, payload: dict[str, Any]) -> None:
        with self._engine.begin() as conn:
            result = conn.execute(
                update(records)
                .where(records.c.game_id == game_id, records.c.seq == seq)
                .values(payload=payload)
            )
        if result.rowcount == 0:
            raise KeyError(f"no records row {game_id}/{seq}")

    def records(self, game_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(records.c.seq, records.c.payload, records.c.created_at)
                .where(records.c.game_id == game_id)
                .order_by(records.c.seq.asc())
            ).all()
        return [_record_row(row) for row in rows]

    def recent_records(self, game_id: str, limit: int) -> list[dict[str, Any]]:
        """`ORDER BY seq DESC LIMIT :limit`, reversed back to oldest-first.

        `uq_records_game_id_seq` already indexes `(game_id, seq)`, so this is
        an index scan of at most `limit` rows however long the game has run -
        which is the whole reason the plan gate asks this rather than
        `records()`. No new index is needed.
        """
        if limit <= 0:
            return []
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(records.c.seq, records.c.payload, records.c.created_at)
                .where(records.c.game_id == game_id)
                .order_by(records.c.seq.desc())
                .limit(limit)
            ).all()
        return [_record_row(row) for row in reversed(rows)]

    def records_since(self, game_id: str, since: int) -> list[dict[str, Any]]:
        """`WHERE seq > :since ORDER BY seq ASC`, no `LIMIT` needed.

        Bounded by the predicate rather than by a row count:
        `uq_records_game_id_seq` indexes `(game_id, seq)`, so this is a range
        scan that starts at `since` and reads only what it returns, however
        many turns of history sit below it. The route this serves discards
        everything at or below `since` anyway - it just used to discard it
        after postgres had already read and shipped every one of those rows,
        snapshot and legal-action enumeration included.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(records.c.seq, records.c.payload, records.c.created_at)
                .where(records.c.game_id == game_id, records.c.seq > since)
                .order_by(records.c.seq.asc())
            ).all()
        return [_record_row(row) for row in rows]

    def max_record_seq(self, game_id: str) -> int:
        """`max(seq)`, 0 for a game with no rows - the same index-served
        lookup `_next_seq` makes, without the advisory lock, since nothing
        here is about to write."""
        with self._engine.connect() as conn:
            current = conn.execute(
                select(func.coalesce(func.max(records.c.seq), 0)).where(
                    records.c.game_id == game_id
                )
            ).scalar_one()
        return int(current)

    def put_record_state(self, game_id: str, seq: int, game: Game) -> None:
        """Upsert via `INSERT ... ON CONFLICT (game_id, seq) DO UPDATE` on
        `pk_record_states` - one round trip whether this is the first write
        at this `(game_id, seq)` or a replacement, and no existence check
        first (the same no-existence-check leniency `append_record` gives
        for `game_id`, since `record_states.game_id` carries no
        `ForeignKey` either - see `db.py`)."""
        blob = encode_record_state(game)
        now = datetime.now(UTC)
        stmt = pg_insert(record_states).values(game_id=game_id, seq=seq, state=blob, created_at=now)
        stmt = stmt.on_conflict_do_update(
            constraint="pk_record_states",
            set_={"state": stmt.excluded.state, "created_at": stmt.excluded.created_at},
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def record_state_seqs(self, game_id: str) -> list[int]:
        """`SELECT seq FROM record_states WHERE game_id = :id ORDER BY seq` -
        the `state` column is never named in this query, so no blob is ever
        read to answer it, matching this method's own protocol docstring."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(record_states.c.seq)
                .where(record_states.c.game_id == game_id)
                .order_by(record_states.c.seq.asc())
            ).all()
        return [int(row[0]) for row in rows]

    def record_state(self, game_id: str, seq: int) -> Game | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(record_states.c.state).where(
                    record_states.c.game_id == game_id, record_states.c.seq == seq
                )
            ).first()
        if row is None:
            return None
        return decode_record_state(game_id, bytes(row[0]))

    def record_states_for_seqs(self, game_id: str, seqs: Iterable[int]) -> dict[int, Game]:
        """One `SELECT ... WHERE game_id = :id AND seq IN (...)` for every
        wanted `seq` - the batched sibling `record_state`'s own protocol
        docstring describes. A row whose blob will not decode is skipped
        rather than raised, same as the in-memory implementation."""
        wanted = sorted(set(seqs))
        if not wanted:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(record_states.c.seq, record_states.c.state).where(
                    record_states.c.game_id == game_id, record_states.c.seq.in_(wanted)
                )
            ).all()
        out: dict[int, Game] = {}
        for seq, blob in rows:
            try:
                out[int(seq)] = decode_record_state(game_id, bytes(blob))
            except GameUnloadable:
                continue
        return out

    def delete(self, game_id: str) -> bool:
        """The soft delete: stamp `archived_at` rather than issue a
        `DELETE`, so the board, hands and final state survive for a
        post-game review (revision `a2f6d9c31b57`, and `GameRepository
        .delete`'s own docstring).

        `command`/`tape`/`pending` are cleared in the same statement: a game
        that has ended holds no standing question, and `paused_command` must
        answer `None` for it exactly as it did when the row was destroyed.
        `ui_game` is deliberately *not* cleared - it records that the lobby
        booted this game, which stays true; `ui_games()`/`live_ui_games()`
        filter on `archived_at` instead.
        """
        if not _looks_like_id(game_id):
            return False
        with self._engine.begin() as conn:
            result = conn.execute(
                update(games)
                .where(games.c.id == game_id, games.c.archived_at.is_(None))
                .values(archived_at=datetime.now(UTC), command=None, tape=None, pending=None)
            )
        with self._cache_lock:
            self._cache.pop(game_id, None)
            self._undecodable.pop(game_id, None)
            self._automatic_pregame.pop(game_id, None)
            self._pregame_stalls.pop(game_id, None)
        with self._locks_lock:
            self._locks.pop(game_id, None)
        return result.rowcount > 0

    def archived(self, game_id: str) -> ArchivedGame | None:
        """The one read that looks past `archived_at` - see `ArchivedGame`.

        Goes through `_game_from_row` rather than parsing `state` itself, so
        a game this process is still holding live comes back as the same
        object every other read hands out (in-place mutations included), the
        way the contract suite requires of every read on this class.

        **The one read that reports `GameUnloadable` instead of raising it.**
        Every other single-game read lets the refusal escape to `app.py`'s
        409 handler, which is right for a board nobody can play; it is wrong
        here, because this read is the last way into the ledger of a game
        that was played to the end, and 409ing it makes that game's whole
        recorded reasoning unreachable for good. The other five columns need
        no decoding at all, so they are answered exactly as before and the
        refusal rides along in `unloadable` - the snapshot itself is still
        not decoded, and no `Game` is ever built from it.
        """
        if not _looks_like_id(game_id):
            return None
        with self._engine.connect() as conn:
            row = conn.execute(
                select(
                    games.c.state,
                    games.c.version,
                    games.c.admin_key,
                    games.c.seats,
                    games.c.created_at,
                    games.c.archived_at,
                    games.c.ai_players,
                ).where(games.c.id == game_id)
            ).first()
        if row is None:
            return None
        game: Game | None
        unloadable: str | None = None
        try:
            game = self._game_from_row(game_id, row.state, row.version)
        except GameUnloadable as exc:
            game, unloadable = None, exc.reason
        return ArchivedGame(
            game_id=game_id,
            game=game,
            admin_key=row.admin_key,
            seat_uids=dict(row.seats or {}),
            created_at=_utc_isoformat(row.created_at),
            deleted_at=_utc_isoformat(row.archived_at),
            unloadable=unloadable,
            ai_players=frozenset(row.ai_players or []),
        )

    def ids(self) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(select(games.c.id).where(games.c.archived_at.is_(None))).all()
        return [row[0] for row in rows]

    @contextmanager
    def locked(self, game_id: str) -> Iterator[Game]:
        lock = self._lock_for(game_id)
        with lock:
            game, _version = self._read_current(game_id)
            yield game

    # -- seat credentials -----------------------------------------------

    def register_seats(self, game_id: str, uids: dict[str, str]) -> None:
        self._update_column(game_id, seats=dict(uids))

    def seat_uids(self, game_id: str) -> dict[str, str]:
        row = self._select_one(game_id, games.c.seats)
        return dict(row[0] or {}) if row is not None else {}

    def player_for_uid(self, game_id: str, uid: str) -> str | None:
        return self.seat_uids(game_id).get(uid)

    # -- advisor credentials ----------------------------------------------

    def register_advisors(self, game_id: str, advisors: dict[str, Advisor]) -> None:
        self._update_column(
            game_id, advisors={uid: advisor.to_wire() for uid, advisor in advisors.items()}
        )

    def advisor_uids(self, game_id: str) -> dict[str, Advisor]:
        row = self._select_one(game_id, games.c.advisors)
        if row is None or not row[0]:
            return {}
        return {uid: Advisor.from_wire(value) for uid, value in row[0].items()}

    # -- play-script corpora --------------------------------------------

    def register_scripts(self, game_id: str, uid: str, corpus: dict[str, Any]) -> None:
        # `jsonb_set` on this uid's key alone, in one statement: a seat and
        # its advisor registering at once each replace only their own entry,
        # where reading, editing and writing back the whole map would let the
        # later write erase the earlier one.
        if not _looks_like_id(game_id):
            return
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE games SET scripts = jsonb_set(scripts, ARRAY[CAST(:uid AS text)], "
                    "CAST(:corpus AS jsonb)) WHERE id = :id AND archived_at IS NULL"
                ),
                {"uid": uid, "corpus": json.dumps(corpus), "id": game_id},
            )

    def scripts(self, game_id: str, uid: str) -> dict[str, Any] | None:
        row = self._select_one(game_id, games.c.scripts)
        if row is None or not row[0]:
            return None
        corpus = row[0].get(uid)
        return dict(corpus) if corpus is not None else None

    # -- admin key --------------------------------------------------------

    def admin_key(self, game_id: str) -> str | None:
        row = self._select_one(game_id, games.c.admin_key)
        return row[0] if row is not None else None

    def is_admin(self, game_id: str, key: str) -> bool:
        return bool(key) and self.admin_key(game_id) == key

    # -- owner_user_id (OIDC-based user separation) --------------------------

    def owner_user_id(self, game_id: str) -> str | None:
        row = self._select_one(game_id, games.c.owner_user_id)
        return row[0] if row is not None else None

    # -- created_at ---------------------------------------------------------

    def created_at(self, game_id: str) -> str | None:
        row = self._select_one(game_id, games.c.created_at)
        return _utc_isoformat(row[0]) if row is not None else None

    # -- decklists ------------------------------------------------------

    def register_decklists(self, game_id: str, decklists: dict[str, str]) -> None:
        self._update_column(game_id, decklists=dict(decklists))

    def decklists(self, game_id: str) -> dict[str, str]:
        row = self._select_one(game_id, games.c.decklists)
        return dict(row[0] or {}) if row is not None else {}

    # -- lobby-owned: agent settings, ai players, ui games ----------------

    def register_agent_settings(self, game_id: str, agents: dict[str, AgentSettings]) -> None:
        payload = {name: settings.model_dump(mode="json") for name, settings in agents.items()}
        self._update_column(game_id, agent_settings=payload)

    def agent_settings(self, game_id: str) -> dict[str, AgentSettings]:
        row = self._select_one(game_id, games.c.agent_settings)
        if row is None or not row[0]:
            return {}
        return {name: AgentSettings.model_validate(value) for name, value in row[0].items()}

    def register_ai_players(self, game_id: str, players: list[str]) -> None:
        self._update_column(game_id, ai_players=list(players))

    def ai_players(self, game_id: str) -> set[str]:
        row = self._select_one(game_id, games.c.ai_players)
        return set(row[0] or []) if row is not None else set()

    def record_automatic_pregame(self, game_id: str, player: str, kind: str) -> None:
        """Remember that `kind` (`"mulligan"` or `"opening_hand"`) was taken
        *for* `player` by `ai_pregame.py`'s backstop rather than by the seat -
        `InMemoryRepository`'s own method, same behaviour, and process-local
        for the reason `__init__` gives."""
        with self._cache_lock:
            self._automatic_pregame.setdefault(game_id, {}).setdefault(player, set()).add(kind)

    def automatic_pregame(self, game_id: str, player: str) -> set[str]:
        """The pregame moves the backstop made for this seat, if any."""
        with self._cache_lock:
            return set(self._automatic_pregame.get(game_id, {}).get(player, set()))

    def record_pregame_stall(
        self, game_id: str, player: str, kind: str, reason: str, text: str
    ) -> bool:
        """`InMemoryRepository`'s own method, same behaviour, process-local
        for the reason `__init__` gives."""
        with self._cache_lock:
            stalls = self._pregame_stalls.setdefault(game_id, {})
            if (player, kind) in stalls:
                return False
            stalls[(player, kind)] = {
                "player": player,
                "kind": kind,
                "reason": reason,
                "text": text,
            }
            return True

    def clear_pregame_stall(self, game_id: str, player: str, kind: str) -> None:
        with self._cache_lock:
            self._pregame_stalls.get(game_id, {}).pop((player, kind), None)

    def pregame_stalls(self, game_id: str) -> list[dict[str, str]]:
        with self._cache_lock:
            return [dict(stall) for stall in self._pregame_stalls.get(game_id, {}).values()]

    def register_ui_game(self, game_id: str) -> None:
        if not _looks_like_id(game_id):
            return
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            conn.execute(
                update(games)
                .where(
                    games.c.id == game_id,
                    games.c.ui_game.is_(False),
                    games.c.archived_at.is_(None),
                )
                .values(ui_game=True, ui_game_registered_at=now)
            )

    def ui_games(self) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(games.c.id)
                .where(games.c.ui_game.is_(True), games.c.archived_at.is_(None))
                .order_by(games.c.ui_game_registered_at.asc())
            ).all()
        return [row[0] for row in rows]

    def live_ui_games(self) -> list[str]:
        with self._engine.connect() as conn:
            return self._live_ui_game_ids(conn)

    # -- paused commands --------------------------------------------------

    def store_paused_command(
        self,
        game_id: str,
        command: Command,
        tape: tuple[Answer, ...],
        pending: PendingDecision,
    ) -> None:
        self._update_column(
            game_id,
            command=command.to_json(),
            tape=[answer.model_dump(mode="json") for answer in tape],
            pending=_pending_to_json(pending),
            # Bumped in the same `UPDATE` that writes the question, and
            # computed by the database rather than read-then-written here,
            # so two concurrent stores can never land on the same number.
            pause_generation=games.c.pause_generation + 1,
        )

    def pause_generation(self, game_id: str) -> int:
        row = self._select_one(game_id, games.c.pause_generation)
        return int(row[0]) if row is not None and row[0] is not None else 0

    def paused_command(
        self, game_id: str
    ) -> tuple[Command, tuple[Answer, ...], PendingDecision] | None:
        row = self._select_one(game_id, games.c.command, games.c.tape, games.c.pending)
        if row is None or row.command is None:
            return None
        command = Command.from_json(row.command)
        tape = tuple(Answer.model_validate(entry) for entry in (row.tape or ()))
        pending = _pending_from_json(row.pending)
        return command, tape, pending

    def clear_paused_command(self, game_id: str) -> None:
        self._update_column(game_id, command=None, tape=None, pending=None)
