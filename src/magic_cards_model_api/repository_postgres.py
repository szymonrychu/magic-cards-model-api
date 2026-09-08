"""`PostgresRepository` (T9): the `GameRepository` protocol (`repository.py`)
backed by real postgres, for a stateless multi-replica deployment instead of
`InMemoryRepository`'s process-local dict.

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
- **`create_if_under`'s cap check** is a short, self-contained "is there room"
  decision with no long-running work inside it, so it uses an ordinary
  transaction-scoped `LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE`: released
  automatically at commit/rollback, never a session-scoped advisory lock that
  could leak across a pooled connection. `SHARE ROW EXCLUSIVE` conflicts with
  `ROW EXCLUSIVE` - the lock every plain `INSERT`/`UPDATE`/`DELETE` takes - so
  it genuinely blocks every concurrent `replace()` for as long as the
  transaction holding it is open, table-wide, not just the row it eventually
  inserts. That is acceptable here only because "create a lobby game" is a
  cold path with a short, bounded critical section (one count query plus one
  insert, no waiting on a decision or a human) - not because it leaves the
  per-turn gameplay path unlocked, which it does not.

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
round-trip JSON on its own. `_decision_to_json`/`_decision_from_json` below
are this module's hand-written codec for it, field by field, because a
generic reflection-based one would have to guess the difference between a
`tuple[UUID, ...]` and a `list[str]` on the way back in, and guessing wrong
here means answering a resumed command's question with the wrong id.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from magic_cards_model.machines.decisions import Answer, Decision, DecisionKind, PendingDecision
from magic_cards_model.models import Game
from magic_cards_model.models.card_state import TargetRef
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.engine import Engine

from .agent_settings import AgentSettings
from .commands import Command
from .db import games, make_engine, records

__all__ = ["PostgresRepository", "VersionConflict"]


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
# Decision / PendingDecision <-> JSON. Hand-written, not generic - see the
# module docstring for why.
# ---------------------------------------------------------------------------


def _decision_to_json(decision: Decision) -> dict[str, Any]:
    return {
        "kind": decision.kind.value,
        "player": decision.player,
        "prompt": decision.prompt,
        "source": decision.source,
        "card": str(decision.card) if decision.card is not None else None,
        "amount": decision.amount,
        "cards": [str(c) for c in decision.cards],
        "card_labels": list(decision.card_labels),
        "moved_label": decision.moved_label,
        "kept_label": decision.kept_label,
        "affirmative_is_legal": decision.affirmative_is_legal,
        "targets": [t.model_dump(mode="json") for t in decision.targets],
        "target_maximum": decision.target_maximum,
        "target_minimum": decision.target_minimum,
        "options": list(decision.options),
        "exact": decision.exact,
        "default_cards": (
            [str(c) for c in decision.default_cards] if decision.default_cards is not None else None
        ),
    }


def _decision_from_json(data: dict[str, Any]) -> Decision:
    default_cards = data.get("default_cards")
    return Decision(
        kind=DecisionKind(data["kind"]),
        player=data["player"],
        prompt=data["prompt"],
        source=data.get("source"),
        card=UUID(data["card"]) if data.get("card") else None,
        amount=data.get("amount", 0),
        cards=tuple(UUID(c) for c in data.get("cards", ())),
        card_labels=tuple(data.get("card_labels", ())),
        moved_label=data.get("moved_label", ""),
        kept_label=data.get("kept_label", ""),
        affirmative_is_legal=data.get("affirmative_is_legal", True),
        targets=tuple(TargetRef.model_validate(t) for t in data.get("targets", ())),
        target_maximum=data.get("target_maximum"),
        target_minimum=data.get("target_minimum", 0),
        options=tuple(data.get("options", ())),
        exact=data.get("exact", True),
        default_cards=tuple(UUID(c) for c in default_cards) if default_cards is not None else None,
    )


def _pending_to_json(pending: PendingDecision) -> dict[str, Any]:
    return {
        "seq": pending.seq,
        "decision": _decision_to_json(pending.decision),
        "rule": pending.rule,
    }


def _pending_from_json(data: dict[str, Any]) -> PendingDecision:
    return PendingDecision(
        seq=data["seq"], decision=_decision_from_json(data["decision"]), rule=data["rule"]
    )


class PostgresRepository:
    """The `GameRepository` protocol, backed by the `games`/`records` tables
    `db.py` defines. Structural, like `InMemoryRepository` - satisfies
    `GameRepository` by having the same methods, not by inheriting from it.
    """

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

    # -- internal helpers ---------------------------------------------------

    def _game_from_row(self, game_id: str, state: Any, version: int) -> Game:
        """The identity-preserving cache's single write/read point: `state`
        is only ever re-parsed into a fresh `Game` when `version` has moved
        past what this instance already has cached - otherwise the same
        cached object comes back, in-place mutations (a caller that never
        called `replace()`, exactly like a direct `InMemoryRepository` caller
        never has to) included. See the module docstring's "identity-
        preserving read cache" section."""
        with self._cache_lock:
            cached = self._cache.get(game_id)
            if cached is not None and cached[1] == version:
                return cached[0]
            game = Game.model_validate(state)
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
                select(games.c.state, games.c.version).where(games.c.id == game_id)
            ).first()
        if row is None:
            raise KeyError(f"no game {game_id}")
        state, version = row
        return self._game_from_row(game_id, state, version), version

    def _live_ui_game_ids(self, conn: Any) -> list[str]:
        """`ui_games()`, filtered to `not Game.is_over`, oldest-registered
        first - the shared logic `live_ui_games()` and `create_if_under`'s
        cap check both need. Takes an open `conn` rather than opening its own
        (`create_if_under` calls this from inside the transaction already
        holding the table lock; there is no reason to check a second
        connection out of the pool for a read the held connection can just
        as well serve).

        Deliberately **not** a `state->>'winner'` SQL predicate: `is_over`
        is evaluated in Python, through `_game_from_row`'s cache, so a game
        this process already holds - mutated in place since its last
        `replace()`, exactly the way a direct `InMemoryRepository` caller is
        allowed to - is judged by that mutation rather than by whatever was
        last actually written to the row. That is what keeps this
        postgres-backed store observably identical to `InMemoryRepository`
        for a caller in the same process, which the contract suite (built
        against `InMemoryRepository`'s shared-reference semantics) assumes.
        """
        rows = conn.execute(
            select(games.c.id, games.c.state, games.c.version)
            .where(games.c.ui_game.is_(True))
            .order_by(games.c.ui_game_registered_at.asc())
        ).all()
        return [
            game_id
            for game_id, state, version in rows
            if not self._game_from_row(game_id, state, version).is_over
        ]

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
        if not _looks_like_id(game_id):
            return
        with self._engine.begin() as conn:
            conn.execute(update(games).where(games.c.id == game_id).values(**values))

    def _select_one(self, game_id: str, *columns: Any) -> Any | None:
        """One row (or `None`) from `games`, projected to `columns` - the
        shared read path every plain accessor below uses. `None` both for an
        unknown id and for an id that is not even valid uuid syntax, which a
        native `uuid` `id` column would otherwise reject at the driver."""
        if not _looks_like_id(game_id):
            return None
        with self._engine.connect() as conn:
            return conn.execute(select(*columns).where(games.c.id == game_id)).first()

    # -- lifecycle ------------------------------------------------------

    def create(self, game: Game) -> str:
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
                    seats={},
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

    def create_if_under(self, game: Game, cap: int, *, ui_game: bool = True) -> str | None:
        game_id = uuid4().hex
        now = datetime.now(UTC)
        admin_key = uuid4().hex
        with self._engine.begin() as conn:
            # Transaction-scoped table lock - see the module docstring for
            # why this path (and only this path) takes one.
            conn.execute(text("LOCK TABLE games IN SHARE ROW EXCLUSIVE MODE"))
            live = len(self._live_ui_game_ids(conn))
            if live >= cap:
                return None
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
                    seats={},
                    decklists={},
                    agent_settings={},
                    ai_players=[],
                    ui_game=ui_game,
                    ui_game_registered_at=now if ui_game else None,
                )
            )
        with self._cache_lock:
            self._cache[game_id] = (game, 0)
        with self._locks_lock:
            self._locks[game_id] = threading.Lock()
        return game_id

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
                    current_game = Game.model_validate(actual_state)
                    with self._cache_lock:
                        self._cache[game_id] = (current_game, actual_version)
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
        result = []
        for row in rows:
            stamp = row.created_at
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            result.append(
                {
                    "seq": row.seq,
                    "payload": row.payload,
                    "created_at": stamp.astimezone(UTC).isoformat(),
                }
            )
        return result

    def delete(self, game_id: str) -> bool:
        if not _looks_like_id(game_id):
            return False
        with self._engine.begin() as conn:
            result = conn.execute(sa_delete(games).where(games.c.id == game_id))
        with self._cache_lock:
            self._cache.pop(game_id, None)
            self._automatic_pregame.pop(game_id, None)
        with self._locks_lock:
            self._locks.pop(game_id, None)
        return result.rowcount > 0

    def ids(self) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(select(games.c.id)).all()
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

    # -- admin key --------------------------------------------------------

    def admin_key(self, game_id: str) -> str | None:
        row = self._select_one(game_id, games.c.admin_key)
        return row[0] if row is not None else None

    def is_admin(self, game_id: str, key: str) -> bool:
        return bool(key) and self.admin_key(game_id) == key

    # -- created_at ---------------------------------------------------------

    def created_at(self, game_id: str) -> str | None:
        row = self._select_one(game_id, games.c.created_at)
        if row is None or row[0] is None:
            return None
        stamp = row[0]
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return stamp.astimezone(UTC).isoformat()

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

    def register_ui_game(self, game_id: str) -> None:
        if not _looks_like_id(game_id):
            return
        now = datetime.now(UTC)
        with self._engine.begin() as conn:
            conn.execute(
                update(games)
                .where(games.c.id == game_id, games.c.ui_game.is_(False))
                .values(ui_game=True, ui_game_registered_at=now)
            )

    def ui_games(self) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(games.c.id)
                .where(games.c.ui_game.is_(True))
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
        )

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
