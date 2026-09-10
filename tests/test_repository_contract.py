"""The `GameRepository` contract - one suite, run against every
implementation.

Parameterised over `repo` (see `_REPOSITORY_FACTORIES` below): today that is
only `InMemoryRepository`, but T9 adds a postgres-backed implementation by
appending its factory to that one list, not by copying this file. Every test
here talks to `repo` through `GameRepository`'s public surface only - never a
private attribute - so it is exactly as valid against a database as against
a dict.

`reserve_slot`/`attach`/`release`'s atomicity gets its own section (M8): two
documented races (`repository.py`'s `reserve_slot` docstring, the same two
`create_if_under`'s docstring used to describe before it was deleted) that a
naive postgres implementation could easily reopen by doing the cap check
and the reservation as separate statements, or by leaking a slot when a
create fails. The concurrency tests below pin both from outside, using only
`GameRepository` calls - so they hold regardless of what a given
implementation uses internally to enforce them.
"""

from __future__ import annotations

import threading
import time

import pytest
from conftest import require_postgres_database_url
from magic_cards_model.machines.decisions import Answer, Decision, DecisionKind, PendingDecision
from magic_cards_model.models import Game
from sqlalchemy import text
from test_decks_api import LEGAL_DECK, LEGAL_DECK_B

from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.commands import Command
from magic_cards_model_api.db import make_engine
from magic_cards_model_api.decks import load_deck_into_game
from magic_cards_model_api.repository import GameRepository, InMemoryRepository
from magic_cards_model_api.repository_postgres import PostgresRepository

#: Set by `mise run test:pg` (or by hand) to a postgres DSN whose schema is
#: already at `alembic upgrade head` - never read by the plain `mise run
#: test`, so a fresh checkout with no database configured stays docker-free.
_TEST_DATABASE_URL_ENV = "MCM_TEST_DATABASE_URL"

#: One connection pool shared across every postgres-backed `repo` instance
#: this test session creates - `PostgresRepository`'s identity-preserving
#: cache is per-*instance*, not per-engine, so sharing the pool does not
#: leak state between tests; it only avoids opening a fresh pool per test.
_pg_engine = None


def _postgres_repository_factory() -> PostgresRepository:
    """A fresh, empty `PostgresRepository` against `MCM_TEST_DATABASE_URL` -
    the same zero-argument, fresh-instance signature `InMemoryRepository`
    itself has. Skips (readably) when that variable is not set, which is the
    default for the plain `mise run test` run - only `mise run test:pg` sets
    it, after applying migrations. Under `mise run test:pg`
    (`MCM_REQUIRE_POSTGRES_TESTS=1`) a missing URL is a hard failure instead
    of a skip - see `conftest.require_postgres_database_url`."""
    global _pg_engine
    url = require_postgres_database_url(_TEST_DATABASE_URL_ENV)
    if _pg_engine is None:
        _pg_engine = make_engine(url)
    with _pg_engine.begin() as conn:
        # Every test gets a clean pair of tables - `ids()`, `test_ids_lists_
        # exactly_the_created_games` and friends assume nothing but what this
        # test itself created is in there.
        conn.execute(text("TRUNCATE TABLE records, games RESTART IDENTITY CASCADE"))
    return PostgresRepository(url, engine=_pg_engine)


# T9 adds its postgres factory here rather than copying this file - the
# per-test setup/teardown (truncating the tables) needed a wrapping factory
# function rather than the bare class `InMemoryRepository` uses.
_REPOSITORY_FACTORIES = [InMemoryRepository, _postgres_repository_factory]


def test_the_postgres_factory_is_still_parameterised_into_the_contract_suite() -> None:
    """A cheap, always-runs (never skipped) guard against the whole postgres
    parameterisation quietly disappearing from `repo` - `_postgres_repository_
    factory` skipping/failing when it actually runs is a separate concern
    (see it and `conftest.require_postgres_database_url`); this only pins
    that it is still *in the list* to run at all."""
    assert _postgres_repository_factory in _REPOSITORY_FACTORIES


def _real_game() -> Game:
    """A real engine `Game`, from a real (if minimal) legal decklist pair -
    exactly what `load_deck_into_game` builds for `POST /api/lobby/games`, no
    hand-built model. Repository tests only need *a* game, so any two players
    with a legal deck each will do."""
    match = load_deck_into_game([("you", LEGAL_DECK), ("bot", LEGAL_DECK_B)])
    return match.games[-1]


def _real_pending(seq: int = 0) -> PendingDecision:
    """A real engine `PendingDecision` - the same dataclass `commands.
    run_command` builds from a real `DecisionPending`, just constructed
    directly rather than by actually pausing a command: the contract only
    needs *a* well-formed one to round-trip through the store, the same way
    `_real_game` only needs *a* well-formed `Game`."""
    decision = Decision(kind=DecisionKind.MAY, player="you", prompt="you may draw a card")
    return PendingDecision(
        seq=seq, decision=decision, rule="CR 608.2d (a choice made while the effect applies)"
    )


@pytest.fixture(params=_REPOSITORY_FACTORIES, ids=lambda factory: factory.__name__)
def repo(request: pytest.FixtureRequest) -> GameRepository:
    return request.param()


# ---------------------------------------------------------------------------
# create / get / delete / ids
# ---------------------------------------------------------------------------


def test_create_returns_a_fresh_id_and_get_returns_the_same_game(repo: GameRepository) -> None:
    game = _real_game()
    game_id = repo.create(game)

    assert game_id
    assert repo.get(game_id) is game


def test_create_ids_are_unique_across_calls(repo: GameRepository) -> None:
    first = repo.create(_real_game())
    second = repo.create(_real_game())

    assert first != second


def test_get_returns_none_for_an_unknown_id(repo: GameRepository) -> None:
    assert repo.get("no-such-game") is None


def test_ids_lists_exactly_the_created_games(repo: GameRepository) -> None:
    first = repo.create(_real_game())
    second = repo.create(_real_game())

    assert set(repo.ids()) == {first, second}


def test_delete_removes_the_game_and_reports_it_existed(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    assert repo.delete(game_id) is True
    assert repo.get(game_id) is None
    assert game_id not in repo.ids()


def test_delete_of_an_unknown_id_reports_false_and_changes_nothing(
    repo: GameRepository,
) -> None:
    game_id = repo.create(_real_game())

    assert repo.delete("no-such-game") is False
    assert repo.get(game_id) is not None


# ---------------------------------------------------------------------------
# locked - the mutation boundary
# ---------------------------------------------------------------------------


def test_locked_yields_the_stored_game(repo: GameRepository) -> None:
    game = _real_game()
    game_id = repo.create(game)

    with repo.locked(game_id) as locked_game:
        assert locked_game is game


def test_locked_raises_key_error_for_an_unknown_game(repo: GameRepository) -> None:
    with pytest.raises(KeyError), repo.locked("no-such-game"):
        pass


def test_locked_serialises_concurrent_holders(repo: GameRepository) -> None:
    """Two threads both asking for the same game's lock: the second must not
    get in until the first lets go. `order` records entry/exit interleaving -
    a broken (non-exclusive) `locked()` would let both `enter` events land
    before either `exit`."""
    game_id = repo.create(_real_game())
    order: list[str] = []
    first_holds = threading.Event()
    second_may_try = threading.Event()

    def hold_first() -> None:
        with repo.locked(game_id):
            order.append("first-enter")
            first_holds.set()
            second_may_try.wait(timeout=5)
            order.append("first-exit")

    def hold_second() -> None:
        first_holds.wait(timeout=5)
        second_may_try.set()
        with repo.locked(game_id):
            order.append("second-enter")

    t1 = threading.Thread(target=hold_first)
    t2 = threading.Thread(target=hold_second)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert order == ["first-enter", "first-exit", "second-enter"]


# ---------------------------------------------------------------------------
# seat credentials
# ---------------------------------------------------------------------------


def test_seat_uids_defaults_to_empty(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    assert repo.seat_uids(game_id) == {}


def test_register_seats_round_trips_through_seat_uids_and_player_for_uid(
    repo: GameRepository,
) -> None:
    game_id = repo.create(_real_game())
    repo.register_seats(game_id, {"uid-1": "you", "uid-2": "bot"})

    assert repo.seat_uids(game_id) == {"uid-1": "you", "uid-2": "bot"}
    assert repo.player_for_uid(game_id, "uid-1") == "you"
    assert repo.player_for_uid(game_id, "uid-2") == "bot"


def test_player_for_uid_is_none_for_an_unregistered_uid(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    repo.register_seats(game_id, {"uid-1": "you"})

    assert repo.player_for_uid(game_id, "uid-does-not-exist") is None


def test_seat_uids_returned_dict_is_a_copy(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    repo.register_seats(game_id, {"uid-1": "you"})

    repo.seat_uids(game_id)["uid-1"] = "tampered"

    assert repo.seat_uids(game_id) == {"uid-1": "you"}


# ---------------------------------------------------------------------------
# admin key
# ---------------------------------------------------------------------------


def test_create_mints_an_admin_key_that_is_admin(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    key = repo.admin_key(game_id)

    assert key
    assert repo.is_admin(game_id, key) is True


def test_is_admin_rejects_the_wrong_key(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    assert repo.is_admin(game_id, "not-the-real-key") is False


def test_is_admin_rejects_a_missing_key(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    assert repo.is_admin(game_id, "") is False


def test_admin_key_is_none_for_an_unknown_game(repo: GameRepository) -> None:
    assert repo.admin_key("no-such-game") is None


# ---------------------------------------------------------------------------
# owner_user_id - OIDC-based user separation
# ---------------------------------------------------------------------------


def test_create_with_no_owner_leaves_it_none(repo: GameRepository) -> None:
    """Backward compatibility: a caller that never names an owner (every
    caller today, since nothing issues bearer tokens yet) gets a game exactly
    as ownerless as before this feature existed."""
    game_id = repo.create(_real_game())

    assert repo.owner_user_id(game_id) is None


def test_create_records_the_given_owner(repo: GameRepository) -> None:
    game_id = repo.create(_real_game(), owner_user_id="oidc-sub-alice")

    assert repo.owner_user_id(game_id) == "oidc-sub-alice"


def test_attach_does_not_disturb_the_owner_create_already_recorded(
    repo: GameRepository,
) -> None:
    """M8's `reserve_slot`/`create`/`attach` split (replacing the deleted
    `create_if_under`) still leaves ownership exactly where `create()` put
    it: `attach` only resolves the reservation and registers the ui game, it
    never touches `owner_user_id`."""
    token = repo.reserve_slot(cap=5)
    assert token is not None
    game_id = repo.create(_real_game(), owner_user_id="oidc-sub-bob")
    repo.attach(token, game_id)

    assert repo.owner_user_id(game_id) == "oidc-sub-bob"


def test_owner_user_id_is_none_for_an_unknown_game(repo: GameRepository) -> None:
    assert repo.owner_user_id("no-such-game") is None


# ---------------------------------------------------------------------------
# created_at
# ---------------------------------------------------------------------------


def test_created_at_is_stamped_on_create_and_none_for_unknown(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    assert repo.created_at(game_id) is not None
    assert repo.created_at("no-such-game") is None


# ---------------------------------------------------------------------------
# decklists
# ---------------------------------------------------------------------------


def test_decklists_defaults_to_empty(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    assert repo.decklists(game_id) == {}


def test_register_decklists_round_trips(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    repo.register_decklists(game_id, {"you": LEGAL_DECK, "bot": LEGAL_DECK_B})

    assert repo.decklists(game_id) == {"you": LEGAL_DECK, "bot": LEGAL_DECK_B}


# ---------------------------------------------------------------------------
# lobby-owned: agent settings, ai players, ui games
# ---------------------------------------------------------------------------


def test_agent_settings_defaults_to_empty(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    assert repo.agent_settings(game_id) == {}


def test_register_agent_settings_round_trips(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    settings = {"you": AgentSettings(model="sonnet", effort="high")}
    repo.register_agent_settings(game_id, settings)

    assert repo.agent_settings(game_id) == settings


def test_ai_players_defaults_to_empty(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    assert repo.ai_players(game_id) == set()


def test_register_ai_players_round_trips(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    repo.register_ai_players(game_id, ["bot"])

    assert repo.ai_players(game_id) == {"bot"}


def test_register_ui_game_makes_it_appear_in_ui_games(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    assert game_id not in repo.ui_games()

    repo.register_ui_game(game_id)

    assert game_id in repo.ui_games()


def test_ui_games_omits_a_deleted_game(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    repo.register_ui_game(game_id)
    repo.delete(game_id)

    assert game_id not in repo.ui_games()


def test_live_ui_games_excludes_a_finished_game(repo: GameRepository) -> None:
    game = _real_game()
    game_id = repo.create(game)
    repo.register_ui_game(game_id)
    assert game_id in repo.live_ui_games()

    game.winner = "you"  # CR 104.1: this game is over

    assert game_id not in repo.live_ui_games()
    assert game_id in repo.ui_games()  # still on record, just not live


# ---------------------------------------------------------------------------
# reserve_slot / attach / release - the atomic MAX_LIVE_GAMES cap check (M8)
#
# `create_if_under` used to do the count-check and the create as one locked
# step. The reservation splits that in two: `reserve_slot` claims a slot -
# counting live ui games *and* every other outstanding reservation - before
# the caller does any (possibly slow, possibly failing) work to create the
# game; `attach` turns a successful creation into a real live game; `release`
# gives the slot back on failure. Every test below drives the full sequence
# through `GameRepository`'s public surface only, exactly like the rest of
# this contract suite.
# ---------------------------------------------------------------------------


def _reserve_and_attach(repo: GameRepository, cap: int) -> str | None:
    """`reserve_slot` then `create`+`attach` - the happy path a real caller
    (`start_game_from_decklists`) drives, collapsed to one call for tests
    that only care about the resulting game id, the same shape
    `create_if_under` used to hand back directly."""
    token = repo.reserve_slot(cap)
    if token is None:
        return None
    game_id = repo.create(_real_game())
    repo.attach(token, game_id)
    return game_id


def test_reserve_slot_succeeds_below_the_cap(repo: GameRepository) -> None:
    token = repo.reserve_slot(1)

    assert token is not None


def test_reserve_and_attach_creates_a_gettable_game(repo: GameRepository) -> None:
    game_id = _reserve_and_attach(repo, cap=1)

    assert game_id is not None
    assert repo.get(game_id) is not None


def test_reserve_slot_refuses_once_the_cap_is_reached(repo: GameRepository) -> None:
    first = _reserve_and_attach(repo, cap=1)
    assert first is not None

    second = repo.reserve_slot(1)

    assert second is None
    assert len(repo.live_ui_games()) == 1


def test_a_reservation_counts_against_the_cap_before_it_is_attached(
    repo: GameRepository,
) -> None:
    """Reserve-then-create's whole point: the slot is spoken for the moment
    `reserve_slot` returns a token, not once `attach` runs - a second caller
    racing the gap between them must still be refused, closing the same
    window `create_if_under`'s docstring used to describe as one lock around
    "both the count and the create"."""
    token = repo.reserve_slot(1)
    assert token is not None

    second = repo.reserve_slot(1)

    assert second is None


def test_reserve_slot_ignores_a_finished_game_against_the_cap(repo: GameRepository) -> None:
    first_id = _reserve_and_attach(repo, cap=1)
    game = repo.get(first_id)
    assert game is not None
    game.winner = "you"  # ended - no longer counts against the cap

    second = repo.reserve_slot(1)

    assert second is not None


def test_attach_registers_the_game_as_a_live_ui_game(repo: GameRepository) -> None:
    token = repo.reserve_slot(5)
    assert token is not None
    game_id = repo.create(_real_game())

    repo.attach(token, game_id)

    assert game_id in repo.ui_games()
    assert game_id in repo.live_ui_games()


def test_attach_raises_key_error_for_an_unknown_token(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    with pytest.raises(KeyError):
        repo.attach("not-a-real-token", game_id)


def test_attach_raises_key_error_for_an_already_attached_token(repo: GameRepository) -> None:
    token = repo.reserve_slot(5)
    assert token is not None
    game_id = repo.create(_real_game())
    repo.attach(token, game_id)

    with pytest.raises(KeyError):
        repo.attach(token, repo.create(_real_game()))


def test_release_frees_the_slot_for_a_later_reservation(repo: GameRepository) -> None:
    """The failure-path race M8 exists to close: a caller reserves, game
    creation then fails or raises before `attach`, and `release` - called
    from that caller's `finally` - must give the slot back rather than
    leaking it forever."""
    token = repo.reserve_slot(1)
    assert token is not None

    repo.release(token)

    second = repo.reserve_slot(1)
    assert second is not None


def test_release_is_a_no_op_for_an_unknown_token(repo: GameRepository) -> None:
    """`start_game_from_decklists` calls `release` unconditionally from a
    `finally`, including on the success path after `attach` already
    resolved the same token - `release` must never raise there."""
    repo.release("never-reserved")  # must not raise


def test_release_after_attach_does_not_unattach_the_game(repo: GameRepository) -> None:
    token = repo.reserve_slot(1)
    assert token is not None
    game_id = repo.create(_real_game())
    repo.attach(token, game_id)

    repo.release(token)  # must not raise, must not un-attach

    assert game_id in repo.live_ui_games()


def test_reserve_and_attach_is_fully_visible_on_return(repo: GameRepository) -> None:
    """Pins that a successful reserve-then-attach leaves no partially-visible
    state: by the time `attach` returns, the game is gettable, has an admin
    key, is lockable, and already counts in `ui_games`/`live_ui_games` -
    never some of these without the others. The same guarantee
    `create_if_under`'s own "fully visible on return" test pinned when the
    count-check and the create were one call."""
    game_id = _reserve_and_attach(repo, cap=5)
    assert game_id is not None

    assert repo.get(game_id) is not None
    assert repo.admin_key(game_id) is not None
    assert game_id in repo.ui_games()
    assert game_id in repo.live_ui_games()
    with repo.locked(game_id) as locked_game:
        assert locked_game is not None


def test_a_reservation_older_than_the_ttl_stops_counting_against_the_cap(
    repo: GameRepository,
) -> None:
    """A stale reservation - a process that reserved a slot and then died,
    or a caller bug that never reaches its `finally` - must not hold a slot
    hostage forever. `RESERVATION_TTL_SECONDS` is the explicit answer this
    task's brief asks for: a reservation older than the TTL is swept the
    next time `reserve_slot` runs and no longer counts. See
    `repository.py`'s module docstring for why this, and not a durable
    cross-restart record, is the right shape while the lobby is still
    single-process (`replicas: 1`, the 2b plan's M9) - a process restart
    wipes every reservation outright anyway."""
    repo.RESERVATION_TTL_SECONDS = 0.05
    token = repo.reserve_slot(1)
    assert token is not None

    time.sleep(0.1)

    second = repo.reserve_slot(1)
    assert second is not None


def test_attaching_an_expired_reservation_raises_key_error(repo: GameRepository) -> None:
    repo.RESERVATION_TTL_SECONDS = 0.05
    token = repo.reserve_slot(1)
    assert token is not None
    game_id = repo.create(_real_game())

    time.sleep(0.1)

    with pytest.raises(KeyError):
        repo.attach(token, game_id)


def test_reserve_slot_is_atomic_under_concurrent_callers(repo: GameRepository) -> None:
    """N threads racing a cap of `cap`: exactly `cap` must win, never more
    (the `MAX_LIVE_GAMES` overshoot this method exists to close) and never
    fewer (a correct implementation does not under-admit either)."""
    cap = 3
    attempts = 8
    results: list[str | None] = []
    lock = threading.Lock()
    ready = threading.Barrier(attempts)

    def attempt() -> None:
        ready.wait(timeout=5)
        token = repo.reserve_slot(cap)
        with lock:
            results.append(token)

    threads = [threading.Thread(target=attempt) for _ in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == cap
    assert len(set(succeeded)) == cap  # every winner got a distinct token


def test_a_failed_create_leaves_no_leaked_slot_under_concurrency(repo: GameRepository) -> None:
    """The failure-path race, under concurrency, in two waves so releasing a
    slot mid-wave (which legitimately lets a *concurrent* racer reuse it
    right away - not a leak) cannot be mistaken for one.

    Wave 1: exactly `cap` threads race `reserve_slot` - by
    `test_reserve_slot_is_atomic_under_concurrent_callers`, all `cap` win.
    Every *odd* winner then simulates a failed create (`release` instead of
    `attach` - exactly `start_game_from_decklists`'s `finally`), concurrently
    with the even winners' `attach`. Once every thread has finished, exactly
    the attached half must be live - nothing leaked, nothing double-counted.

    Wave 2, only after wave 1 fully joins: `cap` more threads race
    `reserve_slot` again. Exactly as many must win as wave 1 released - the
    freed slots came back, and not one more than that (no over-admission
    past the still-live half)."""
    cap = 4
    tokens: list[str | None] = []
    lock = threading.Lock()
    ready = threading.Barrier(cap)

    def wave_one(index: int) -> None:
        ready.wait(timeout=5)
        token = repo.reserve_slot(cap)
        with lock:
            tokens.append(token)
        assert token is not None  # exactly `cap` threads for `cap` slots
        if index % 2 == 0:
            game_id = repo.create(_real_game())
            repo.attach(token, game_id)
        else:
            repo.release(token)  # simulated failed create

    threads = [threading.Thread(target=wave_one, args=(i,)) for i in range(cap)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    attached = sum(1 for i in range(cap) if i % 2 == 0)
    released = cap - attached
    assert len(repo.live_ui_games()) == attached

    second_tokens: list[str | None] = []
    second_lock = threading.Lock()
    second_ready = threading.Barrier(cap)

    def wave_two() -> None:
        second_ready.wait(timeout=5)
        token = repo.reserve_slot(cap)
        with second_lock:
            second_tokens.append(token)

    second_threads = [threading.Thread(target=wave_two) for _ in range(cap)]
    for t in second_threads:
        t.start()
    for t in second_threads:
        t.join(timeout=5)

    succeeded_again = [t for t in second_tokens if t is not None]
    assert len(succeeded_again) == released  # exactly the freed slots came back


# ---------------------------------------------------------------------------
# replace - swapping in a command's completed working copy
# ---------------------------------------------------------------------------


def test_replace_swaps_in_the_new_game(repo: GameRepository) -> None:
    """`commands.run_command` never mutates the `Game` it is given - a
    completed command's result is a brand new object - so `replace` is how
    that new object becomes what `get`/`locked` hand back next."""
    game_id = repo.create(_real_game())
    new_game = _real_game()

    repo.replace(game_id, new_game)

    assert repo.get(game_id) is new_game


def test_replace_raises_key_error_for_an_unknown_game(repo: GameRepository) -> None:
    with pytest.raises(KeyError):
        repo.replace("no-such-game", _real_game())


# ---------------------------------------------------------------------------
# records (T10) - the append-only audit trail `audit.py` now reads and
# writes through the repository, rather than a module-global dict. This
# section is what makes `InMemoryRepository`'s copy of that behaviour a
# tested contract rather than an untested assumption the docker-free suite
# happens to exercise only indirectly through `app.py`'s routes.
# ---------------------------------------------------------------------------


def test_replace_returns_the_seq_of_the_records_row_it_wrote(repo: GameRepository) -> None:
    """Every successful `replace` writes one `records` row for that commit
    and returns its own `seq` - `app.py`'s `_run_command`/`_resume` thread
    this straight into `audit.record`'s `_fill_seq` so the row is filled in
    rather than duplicated once the caller knows the full payload."""
    game_id = repo.create(_real_game())
    seq = repo.replace(game_id, _real_game())
    assert seq == 1
    rows = repo.records(game_id)
    assert [row["seq"] for row in rows] == [1]
    assert rows[0]["payload"] == {}


def test_replace_with_a_record_payload_writes_it_atomically(repo: GameRepository) -> None:
    """A caller that already has the full payload ready (T10's atomic path)
    gets it written in the very same commit, not `{}` waiting on a later
    `fill_record`."""
    game_id = repo.create(_real_game())
    payload = {"action": "advance", "detail": {"allowed": True}}
    seq = repo.replace(game_id, _real_game(), record=payload)
    rows = repo.records(game_id)
    assert rows[-1]["seq"] == seq
    assert rows[-1]["payload"] == payload


def test_append_record_is_standalone_and_does_not_touch_the_game(
    repo: GameRepository,
) -> None:
    """A refusal caught before any command ran, or an admin action, has no
    game mutation to be atomic with - `append_record` is the write path for
    exactly that case, and it changes nothing about the committed game."""
    game_id = repo.create(_real_game())
    game_before = repo.get(game_id)
    seq = repo.append_record(game_id, {"action": "cast", "detail": {"allowed": False}})
    assert seq == 1
    assert repo.get(game_id) is game_before
    assert repo.records(game_id)[0]["payload"]["action"] == "cast"


def test_append_record_never_raises_for_an_unknown_game(repo: GameRepository) -> None:
    """`audit.record`'s own long-standing promise - a game that is no longer
    in the store, or was never in it, still gets its entry - now backed by
    the repository rather than a permissive module-global dict. More than
    one existing caller (a test harness probing an id that was never
    created, `ai_seat.AiSeatRegistry` dispatching before a game exists)
    relies on this."""
    seq = repo.append_record("no-such-game-at-all", {"action": "ai_seat"})
    assert seq == 1
    assert repo.records("no-such-game-at-all")[0]["payload"]["action"] == "ai_seat"


def test_records_are_returned_oldest_first_across_append_and_replace(
    repo: GameRepository,
) -> None:
    game_id = repo.create(_real_game())
    seq1 = repo.append_record(game_id, {"action": "create_game"})
    seq2 = repo.replace(game_id, _real_game(), record={"action": "advance"})
    seq3 = repo.append_record(game_id, {"action": "report_problem"})
    rows = repo.records(game_id)
    assert [row["seq"] for row in rows] == [seq1, seq2, seq3] == [1, 2, 3]
    assert [row["payload"]["action"] for row in rows] == [
        "create_game",
        "advance",
        "report_problem",
    ]


def test_fill_record_replaces_an_existing_rows_payload(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    seq = repo.replace(game_id, _real_game())  # writes seq=1 with payload {}
    repo.fill_record(game_id, seq, {"action": "cast", "detail": {"allowed": True}})
    rows = repo.records(game_id)
    assert len(rows) == 1
    assert rows[0]["payload"] == {"action": "cast", "detail": {"allowed": True}}


def test_fill_record_raises_key_error_for_a_row_that_does_not_exist(
    repo: GameRepository,
) -> None:
    game_id = repo.create(_real_game())
    with pytest.raises(KeyError):
        repo.fill_record(game_id, 1, {"action": "cast"})


def test_records_is_empty_for_a_game_with_none(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    assert repo.records(game_id) == []


def test_records_survive_the_games_own_deletion(repo: GameRepository) -> None:
    """The audit trail outlives the game it describes - `delete` must never
    remove it (T10, `db.py`'s dropped `ForeignKey` and `repository.py`'s
    `InMemoryRepository.delete` both say why): `lobby.reset_game`'s own
    `game_reset` row, written just before the delete, has to still be there
    for `GET /api/games/{id}/log` afterward, or a transcript would trail off
    exactly where it matters most."""
    game_id = repo.create(_real_game())
    repo.append_record(game_id, {"action": "game_reset"})
    assert repo.delete(game_id) is True
    rows = repo.records(game_id)
    assert [row["payload"]["action"] for row in rows] == ["game_reset"]


# ---------------------------------------------------------------------------
# paused commands - (Command, tape, PendingDecision), the property T7 exists
# to make durable: everything `commands.resume_command` needs to replay a
# paused command with one more answer appended, held by the repository
# rather than by a resolving thread's own stack frame.
# ---------------------------------------------------------------------------


def test_paused_command_is_none_for_a_game_with_no_pause(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    assert repo.paused_command(game_id) is None


def test_store_and_read_back_a_paused_command(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    command = Command(kind="cast", payload={"player": "you", "card": "Opt"})
    pending = _real_pending()
    tape = (Answer(seq=0, kind=DecisionKind.MAY, player="you", accept=True),)

    repo.store_paused_command(game_id, command, tape, pending)
    stored = repo.paused_command(game_id)

    assert stored is not None
    stored_command, stored_tape, stored_pending = stored
    assert stored_command == command
    assert stored_tape == tape
    assert stored_pending == pending


def test_storing_a_paused_command_again_replaces_it(repo: GameRepository) -> None:
    """A game has at most one live pause at a time - the same invariant the
    old `DecisionMailbox` held for a single pending question - so a second
    `store_paused_command` call overwrites the first rather than needing a
    `clear_paused_command` in between."""
    game_id = repo.create(_real_game())
    first = Command(kind="cast", payload={"player": "you"})
    second = Command(kind="advance", payload={"player": "you"})
    pending = _real_pending()

    repo.store_paused_command(game_id, first, (), pending)
    repo.store_paused_command(game_id, second, (), pending)

    stored_command, _stored_tape, _stored_pending = repo.paused_command(game_id)
    assert stored_command == second


def test_clear_paused_command_removes_it(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    repo.store_paused_command(game_id, Command(kind="cast", payload={}), (), _real_pending())

    repo.clear_paused_command(game_id)

    assert repo.paused_command(game_id) is None


def test_clear_paused_command_is_a_no_op_when_nothing_was_pending(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    repo.clear_paused_command(game_id)  # must not raise

    assert repo.paused_command(game_id) is None


def test_pause_generation_starts_at_zero(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    assert repo.pause_generation(game_id) == 0


def test_pause_generation_advances_on_every_store_and_survives_a_clear(
    repo: GameRepository,
) -> None:
    """The counter tracks how many times the standing question has been
    *replaced*, so a client holding a `pending_decision` can tell whether the
    one it read is still the one standing (`app.py`'s `decide`).

    It never goes backwards, and `clear_paused_command` does not reset it:
    a reset would let the next pause reuse a number a stale client is still
    holding, which is the one thing a generation may never do.
    """
    game_id = repo.create(_real_game())
    pending = _real_pending()

    repo.store_paused_command(game_id, Command(kind="cast", payload={}), (), pending)
    first = repo.pause_generation(game_id)
    repo.store_paused_command(game_id, Command(kind="advance", payload={}), (), pending)
    second = repo.pause_generation(game_id)

    assert second > first
    repo.clear_paused_command(game_id)
    assert repo.pause_generation(game_id) == second
    repo.store_paused_command(game_id, Command(kind="cast", payload={}), (), pending)
    assert repo.pause_generation(game_id) > second


def test_pause_generations_are_isolated_per_game(repo: GameRepository) -> None:
    game_a = repo.create(_real_game())
    game_b = repo.create(_real_game())

    repo.store_paused_command(game_a, Command(kind="cast", payload={}), (), _real_pending())

    assert repo.pause_generation(game_a) > 0
    assert repo.pause_generation(game_b) == 0


def test_paused_commands_are_isolated_per_game(repo: GameRepository) -> None:
    game_a = repo.create(_real_game())
    game_b = repo.create(_real_game())
    repo.store_paused_command(game_a, Command(kind="cast", payload={}), (), _real_pending())

    assert repo.paused_command(game_b) is None
    assert repo.paused_command(game_a) is not None


def test_delete_clears_the_paused_command_too(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    repo.store_paused_command(game_id, Command(kind="cast", payload={}), (), _real_pending())

    assert repo.delete(game_id) is True

    assert repo.get(game_id) is None
    assert repo.paused_command(game_id) is None


# ---------------------------------------------------------------------------
# recent_records - the bounded window the plan gate reads
# ---------------------------------------------------------------------------


def test_recent_records_returns_the_newest_rows_oldest_first(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    for n in range(10):
        repo.append_record(game_id, {"action": f"a{n}"})

    window = repo.recent_records(game_id, 3)

    assert [row["seq"] for row in window] == [8, 9, 10]
    assert [row["payload"]["action"] for row in window] == ["a7", "a8", "a9"]


def test_recent_records_never_returns_more_than_the_window(repo: GameRepository) -> None:
    """The property the plan gate depends on: this sits on the hot action
    path, and a full `records()` load would get slower every turn of a long
    game. Whatever the ledger grows to, the window does not."""
    game_id = repo.create(_real_game())
    sizes = []
    for batch in range(4):
        for n in range(50):
            repo.append_record(game_id, {"action": f"b{batch}-{n}"})
        sizes.append(len(repo.recent_records(game_id, 20)))

    assert sizes == [20, 20, 20, 20]
    assert len(repo.records(game_id)) == 200, "the ledger really did grow underneath it"


def test_recent_records_of_a_short_ledger_returns_all_of_it(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    repo.append_record(game_id, {"action": "only"})

    assert [row["payload"]["action"] for row in repo.recent_records(game_id, 20)] == ["only"]


def test_recent_records_is_empty_for_a_game_with_none(repo: GameRepository) -> None:
    assert repo.recent_records("no-such-game", 20) == []


# ---------------------------------------------------------------------------
# records_since / max_record_seq - the bounded read `GET .../log` resumes from
# ---------------------------------------------------------------------------


def test_records_since_returns_only_newer_rows_oldest_first(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    for n in range(5):
        repo.append_record(game_id, {"action": f"a{n}"})

    tail = repo.records_since(game_id, 3)

    assert [row["seq"] for row in tail] == [4, 5]
    assert [row["payload"]["action"] for row in tail] == ["a3", "a4"]


def test_records_since_zero_is_the_whole_ledger(repo: GameRepository) -> None:
    """`since=0` is what a first-ever poll sends, and it must mean "every
    row" - the same answer `records()` gives."""
    game_id = repo.create(_real_game())
    for n in range(4):
        repo.append_record(game_id, {"action": f"a{n}"})

    assert repo.records_since(game_id, 0) == repo.records(game_id)


def test_records_since_the_end_is_empty(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    repo.append_record(game_id, {"action": "only"})

    assert repo.records_since(game_id, 1) == []
    assert repo.records_since(game_id, 99) == []


def test_records_since_reads_only_the_tail(repo: GameRepository) -> None:
    """The property the log route depends on: cost tracks the tail asked
    for, never the ledger it sits at the end of."""
    game_id = repo.create(_real_game())
    sizes = []
    for batch in range(4):
        for n in range(50):
            repo.append_record(game_id, {"action": f"b{batch}-{n}"})
        sizes.append(len(repo.records_since(game_id, repo.max_record_seq(game_id) - 20)))

    assert sizes == [20, 20, 20, 20]
    assert len(repo.records(game_id)) == 200, "the ledger really did grow underneath it"


def test_records_since_is_empty_for_a_game_with_none(repo: GameRepository) -> None:
    assert repo.records_since("no-such-game", 0) == []


def test_max_record_seq_is_the_highest_seq_recorded(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())

    assert repo.max_record_seq(game_id) == 0

    for n in range(3):
        assert repo.append_record(game_id, {"action": f"a{n}"}) == repo.max_record_seq(game_id)

    assert repo.max_record_seq(game_id) == 3


def test_max_record_seq_is_zero_for_a_game_with_none(repo: GameRepository) -> None:
    assert repo.max_record_seq("no-such-game") == 0


def test_max_record_seq_is_per_game(repo: GameRepository) -> None:
    game_a = repo.create(_real_game())
    game_b = repo.create(_real_game())
    for n in range(3):
        repo.append_record(game_a, {"action": f"a{n}"})
    repo.append_record(game_b, {"action": "b0"})

    assert repo.max_record_seq(game_a) == 3
    assert repo.max_record_seq(game_b) == 1


# ---------------------------------------------------------------------------
# delete is soft: the row and its state survive for review
# ---------------------------------------------------------------------------


def test_archived_returns_a_live_game_with_no_deletion_stamp(repo: GameRepository) -> None:
    game_id = repo.create(_real_game())
    repo.register_seats(game_id, {"uid-you": "you"})

    archived = repo.archived(game_id)

    assert archived is not None
    assert archived.deleted_at is None
    assert archived.admin_key == repo.admin_key(game_id)
    assert archived.seat_uids == {"uid-you": "you"}
    assert [player.username for player in archived.game.players] == ["you", "bot"]


def test_archived_is_none_for_a_game_that_never_existed(repo: GameRepository) -> None:
    assert repo.archived("no-such-game") is None


def test_a_deleted_games_state_and_credentials_survive_for_review(repo: GameRepository) -> None:
    """The user's own requirement: "Games must be persisting in the database
    even after are stopped/finished/conceded." A hard delete kept the audit
    trail and threw away the board it describes, so a post-game review would
    have had reasoning with nothing to read it against."""
    game = _real_game()
    game_id = repo.create(game)
    repo.register_seats(game_id, {"uid-you": "you", "uid-bot": "bot"})
    admin = repo.admin_key(game_id)

    assert repo.delete(game_id) is True

    archived = repo.archived(game_id)
    assert archived is not None, "the row must outlive the deletion"
    assert archived.deleted_at is not None, "and must say when it was ended"
    assert [player.username for player in archived.game.players] == ["you", "bot"]
    assert len(archived.game.player("you").library) == len(game.player("you").library)
    assert archived.admin_key == admin
    assert archived.seat_uids == {"uid-you": "you", "uid-bot": "bot"}


def test_a_deleted_game_is_gone_from_every_live_surface(repo: GameRepository) -> None:
    """Soft, not invisible-only: everything that answers "is this game
    playable" must still say no, exactly as a hard delete made it say no."""
    game_id = repo.create(_real_game())
    repo.register_ui_game(game_id)

    assert repo.delete(game_id) is True

    assert repo.get(game_id) is None
    assert game_id not in repo.ids()
    assert game_id not in repo.ui_games()
    assert game_id not in repo.live_ui_games()
    assert repo.seat_uids(game_id) == {}
    assert repo.admin_key(game_id) is None
    with pytest.raises(KeyError), repo.locked(game_id):
        pass


def test_deleting_twice_reports_false_the_second_time(repo: GameRepository) -> None:
    """`lobby.reset_game` reports `deleted` off this return value, and the
    lobby's End button is clickable twice - the second click ends nothing."""
    game_id = repo.create(_real_game())

    assert repo.delete(game_id) is True
    assert repo.delete(game_id) is False


def test_a_deleted_game_does_not_hold_a_live_slot(repo: GameRepository) -> None:
    """`MAX_LIVE_GAMES` counts `live_ui_games()`; an archived row must not
    keep occupying a seat at the table it has been taken away from."""
    first = repo.create(_real_game())
    repo.register_ui_game(first)
    repo.delete(first)

    token = repo.reserve_slot(1)

    assert token is not None, "the ended game must have given its slot back"
