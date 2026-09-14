"""`ASSIGN_COMBAT_DAMAGE` (CR 510.1c, 702.19b) paused and answered on the
postgres store.

The same real double block behind a real trampler as
`test_assign_combat_damage.py`, but on a real `PostgresRepository`: the pause
carries `lethal_damage`, `default_damage` and `trample`, all three of which
the postgres codec used to drop, so the stored question stopped equalling the
replayed one and the answer was refused as a diverged replay.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from conftest import find, state
from fastapi.testclient import TestClient
from postgres_store_testkit import postgres_store
from test_assign_combat_damage import _boot_double_block

from magic_cards_model_api.repository_postgres import PostgresRepository


@pytest.fixture(autouse=True)
def postgres_repo(monkeypatch: pytest.MonkeyPatch) -> Iterator[PostgresRepository]:
    with postgres_store(monkeypatch) as repo:
        yield repo


def test_a_damage_assignment_pause_round_trips_and_resumes(
    client: TestClient, game: str, postgres_repo: PostgresRepository
) -> None:
    ids = _boot_double_block(client, game)
    uids = {name: uid for uid, name in postgres_repo.seat_uids(game).items()}

    stored = postgres_repo.paused_command(game)
    assert stored is not None
    decision = stored[2].decision
    assert decision.trample is True
    assert len(decision.lethal_damage) == len(decision.targets)
    assert sorted(decision.lethal_damage) == [0, 1, 2]
    assert decision.default_damage is not None
    assert sum(amount for _, amount in decision.default_damage) == 4

    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "damage": [
                {"target": ids["bears"], "amount": 2},
                {"target": ids["mystic"], "amount": 1},
                {"target": "bob", "amount": 1},
            ],
        },
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True, answered.json()

    board = state(client, game)
    assert next(p["life"] for p in board["players"] if p["username"] == "bob") == 19
    assert find(board["battlefield"], "Lupine Harbingers")["damage"] == 3
    assert postgres_repo.paused_command(game) is None
