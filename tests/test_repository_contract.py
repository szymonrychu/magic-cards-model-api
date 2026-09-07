"""The `GameRepository` contract - one suite, run against every
implementation.

Parameterised over `repo` (see `_REPOSITORY_FACTORIES` below): today that is
only `InMemoryRepository`, but T9 adds a postgres-backed implementation by
appending its factory to that one list, not by copying this file. Every test
here talks to `repo` through `GameRepository`'s public surface only - never a
private attribute - so it is exactly as valid against a database as against
a dict.

`create_if_under`'s atomicity gets its own section: two documented races
(`repository.py`'s `create_if_under` docstring) that a naive postgres
implementation could easily reopen by doing the cap check and the insert as
separate statements. The concurrency tests below pin both from outside,
using only `GameRepository` calls - so they hold regardless of what a given
implementation uses internally to enforce them.
"""

from __future__ import annotations

import threading

import pytest
from magic_cards_model.models import Game
from test_decks_api import LEGAL_DECK, LEGAL_DECK_B

from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.decks import load_deck_into_game
from magic_cards_model_api.repository import GameRepository, InMemoryRepository

# T9 adds its postgres factory here, e.g. `_REPOSITORY_FACTORIES.append(...)`
# or, if it needs per-test setup/teardown, a wrapping factory function with
# the same zero-argument, fresh-instance signature as `InMemoryRepository`.
_REPOSITORY_FACTORIES = [InMemoryRepository]


def _real_game() -> Game:
    """A real engine `Game`, from a real (if minimal) legal decklist pair -
    exactly what `load_deck_into_game` builds for `POST /api/ui/games`, no
    hand-built model. Repository tests only need *a* game, so any two players
    with a legal deck each will do."""
    match = load_deck_into_game([("you", LEGAL_DECK), ("bot", LEGAL_DECK_B)])
    return match.games[-1]


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
# create_if_under - the atomic MAX_LIVE_GAMES cap check
# ---------------------------------------------------------------------------


def test_create_if_under_succeeds_below_the_cap(repo: GameRepository) -> None:
    game_id = repo.create_if_under(_real_game(), cap=1)

    assert game_id is not None
    assert repo.get(game_id) is not None


def test_create_if_under_refuses_once_the_cap_is_reached(repo: GameRepository) -> None:
    first = repo.create_if_under(_real_game(), cap=1)
    assert first is not None

    second = repo.create_if_under(_real_game(), cap=1)

    assert second is None
    assert len(repo.live_ui_games()) == 1


def test_create_if_under_ignores_a_finished_game_against_the_cap(repo: GameRepository) -> None:
    first_id = repo.create_if_under(_real_game(), cap=1)
    game = repo.get(first_id)
    assert game is not None
    game.winner = "you"  # ended - no longer counts against the cap

    second = repo.create_if_under(_real_game(), cap=1)

    assert second is not None


def test_create_if_under_with_ui_game_false_does_not_register_it(repo: GameRepository) -> None:
    game_id = repo.create_if_under(_real_game(), cap=5, ui_game=False)

    assert game_id is not None
    assert game_id not in repo.ui_games()


def test_create_if_under_a_successful_call_is_fully_visible_on_return(
    repo: GameRepository,
) -> None:
    """Pins that a successful `create_if_under` leaves no partially-visible
    state: by the time it returns, the game is gettable, has an admin key,
    is lockable, and (with the default `ui_game=True`) already counts in
    `ui_games`/`live_ui_games` - never some of these without the others."""
    game_id = repo.create_if_under(_real_game(), cap=5)
    assert game_id is not None

    assert repo.get(game_id) is not None
    assert repo.admin_key(game_id) is not None
    assert game_id in repo.ui_games()
    assert game_id in repo.live_ui_games()
    with repo.locked(game_id) as locked_game:
        assert locked_game is not None


def test_create_if_under_is_atomic_under_concurrent_callers(repo: GameRepository) -> None:
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
        game_id = repo.create_if_under(_real_game(), cap=cap)
        with lock:
            results.append(game_id)

    threads = [threading.Thread(target=attempt) for _ in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == cap
    assert len(set(succeeded)) == cap  # every winner got a distinct id
    assert len(repo.live_ui_games()) == cap


def test_create_if_under_admits_are_each_fully_visible_under_concurrency(
    repo: GameRepository,
) -> None:
    """The second documented race: admin-key mint, lock install and the
    `ui_games` append happening as one step, not three. Every thread whose
    `create_if_under` call returns a successful id checks - from inside that
    same thread, immediately on return - that the id is already fully live
    (gettable, has an admin key, lockable, counted). A caller that squeezed
    in a `create_if_under` between two other threads' racing calls must never
    observe a game that exists but is only half set up."""
    cap = 4
    attempts = 10
    failures: list[str] = []
    failures_lock = threading.Lock()
    ready = threading.Barrier(attempts)

    def attempt() -> None:
        ready.wait(timeout=5)
        game_id = repo.create_if_under(_real_game(), cap=cap)
        if game_id is None:
            return
        problems = []
        if repo.get(game_id) is None:
            problems.append("not gettable")
        if repo.admin_key(game_id) is None:
            problems.append("no admin key")
        if game_id not in repo.ui_games():
            problems.append("not in ui_games")
        try:
            with repo.locked(game_id):
                pass
        except KeyError:
            problems.append("not lockable")
        if problems:
            with failures_lock:
                failures.append(f"{game_id}: {', '.join(problems)}")

    threads = [threading.Thread(target=attempt) for _ in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert failures == []
