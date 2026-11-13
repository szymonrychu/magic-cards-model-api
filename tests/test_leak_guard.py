"""The autouse store leak guard in conftest.py (`leak_guard`, `_no_leaked_games`).

A test that leaves a game, a user preference or a sim manager entry behind in
a process-wide singleton poisons whichever test happens to run next on that
xdist worker (the victim fails, the culprit stays green). The guard fails the
culprit at teardown, by name, and cleans up so the damage does not cascade.

These tests call the guard's context manager directly with a made-up node id,
so a deliberate leak is observed without failing this test itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import leak_guard

from magic_cards_model_api.decks_api import boot_decklist_game
from magic_cards_model_api.sim import server as sim_server
from magic_cards_model_api.sim.config import SimLimits
from magic_cards_model_api.sim.manager import BatchManager, SandboxManager
from magic_cards_model_api.store import store

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
RED = (FIXTURES / "red_burn.txt").read_text()
GREEN = (FIXTURES / "green_stompy.txt").read_text()
NODEID = "tests/test_somewhere.py::test_the_culprit"


class _Handle:
    def __init__(self) -> None:
        self.killed = False

    def start(self, timeout):
        pass

    def request(self, op, args, timeout):
        return {"op": op, "done": True}

    def kill(self):
        self.killed = True


def _leak_a_game() -> str:
    game_id, _uids, _match = boot_decklist_game(
        [("a", RED, True), ("b", GREEN, True)], seed=1, owner_user_id=None
    )
    return game_id


def test_a_clean_test_passes_the_guard():
    with leak_guard(NODEID):
        pass


def test_a_test_that_deletes_its_own_game_passes_the_guard():
    with leak_guard(NODEID):
        store.delete(_leak_a_game())


def test_a_leaked_game_fails_the_guard_by_name_and_is_deleted():
    with pytest.raises(pytest.fail.Exception) as raised, leak_guard(NODEID):
        game_id = _leak_a_game()

    assert NODEID in str(raised.value)
    assert "game" in str(raised.value)
    assert game_id not in store.ids()


def test_a_game_that_was_there_before_is_not_blamed_on_this_test():
    game_id = _leak_a_game()
    try:
        with leak_guard(NODEID):
            pass
        assert game_id in store.ids()
    finally:
        store.delete(game_id)


def test_a_leaked_user_preference_fails_the_guard_and_is_deleted():
    with pytest.raises(pytest.fail.Exception) as raised, leak_guard(NODEID):
        store.set_user_preferences("leaker", {"a": 1})

    assert NODEID in str(raised.value)
    assert "user preference" in str(raised.value)
    assert "leaker" not in store.user_preference_subs()


def test_a_leaked_sandbox_fails_the_guard_and_is_ended(monkeypatch):
    fake = SandboxManager(SimLimits.from_env({}), handle_factory=_Handle)
    monkeypatch.setattr(sim_server, "manager", fake)

    with pytest.raises(pytest.fail.Exception) as raised, leak_guard(NODEID):
        fake.new({"players": [{"name": "a"}, {"name": "b"}], "cards": []})

    assert NODEID in str(raised.value)
    assert "sandbox" in str(raised.value)
    assert fake.live_ids() == []


def test_a_leaked_batch_fails_the_guard():
    # Swapped by hand, not with `monkeypatch`: a batch cannot be removed from a
    # manager short of shutting it down, so the swap must be undone before the
    # autouse guard's own teardown looks at the real `sim_server.batches`.
    fake = BatchManager(
        SimLimits.from_env({"MTG_SIM_BASELINE_ENABLED": "1"}), handle_factory=_Handle
    )
    real = sim_server.batches
    sim_server.batches = fake
    try:
        with pytest.raises(pytest.fail.Exception) as raised, leak_guard(NODEID):
            fake.start(RED, GREEN, 1, 1)
    finally:
        sim_server.batches = real
        fake.shutdown()

    assert NODEID in str(raised.value)
    assert "batch" in str(raised.value)


def test_the_guard_watches_the_singleton_even_while_the_store_name_is_swapped(monkeypatch):
    # `postgres_store_testkit.postgres_store` rebinds the `store` name in every
    # module, conftest included, and the swap outlives the guard's teardown.
    import conftest

    from magic_cards_model_api.repository import InMemoryRepository

    monkeypatch.setattr(conftest, "store", InMemoryRepository())

    with pytest.raises(pytest.fail.Exception) as raised, leak_guard(NODEID):
        store.set_user_preferences("leaker", {"a": 1})

    assert "user preference" in str(raised.value)
    assert "leaker" not in store.user_preference_subs()
