"""A seat still reads its own script runs off `GET /log` once its game is over.

The driver's handover builds its script-run table (`playscripts.report.
script_run_table`) from `GET /api/games/{id}/log?uid=<seat uid>`, read the
moment the handover starts. Game 23868a95 posted nine `POST /script-runs`
and its handover table still said "no script runs recorded". Reproduced here
(A3 clause 0), in both the memory and the postgres suites, the two
conditions are:

- **natural game over** (`lobby.retire_game`) never touched the seat uids,
  so this read kept every row. That half is a regression guard: A3 keeps it
  that way by not releasing anything at game over.
- **an ended game** (End game / Forget, the soft delete) is where the rows
  went: `game_or_404` answered 404 for the archived row and
  `visibility.resolve_viewer` could not resolve the seat uid, so the seat's
  own script-run rows were unreachable although the ledger still held them.
  `GET /log` now authorises an archived game against the credentials the
  archive kept (`store.archived`), exactly the way `GET .../review`,
  `POST .../reflection` and `POST .../ai-review` already do.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import require_postgres_database_url
from fastapi.testclient import TestClient
from sqlalchemy import text

from magic_cards_model_api import store as store_module
from magic_cards_model_api.main import app

LEGAL_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
LEGAL_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"


def _swap_store(monkeypatch: pytest.MonkeyPatch, replacement: Any) -> None:
    """Point every module that bound the process-wide `store` singleton at
    `replacement`. `from .store import store` is a static binding in most of
    the package (see `test_version_conflict_http.py`'s fixture for why a
    partial patch fails in confusing ways), so every one of them is found
    and patched rather than listed by hand."""
    original = store_module.store
    for name, module in list(sys.modules.items()):
        if name.startswith("magic_cards_model_api") and getattr(module, "store", None) is original:
            monkeypatch.setattr(module, "store", replacement)


@pytest.fixture(params=["memory", "postgres"])
def client(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app over the in-memory store, then over a real postgres store."""
    if request.param == "postgres":
        from magic_cards_model_api.db import make_engine
        from magic_cards_model_api.repository_postgres import PostgresRepository

        url = require_postgres_database_url("MCM_TEST_DATABASE_URL")
        engine = make_engine(url)
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE records, games RESTART IDENTITY CASCADE"))
        _swap_store(monkeypatch, PostgresRepository(url, engine=make_engine(url)))
    with TestClient(app) as test_client:
        yield test_client
    from magic_cards_model_api import lobby

    live = sys.modules["magic_cards_model_api.lobby"].store
    for game_id in live.ui_games():
        lobby.reset_game(game_id, reason="test cleanup")


def boot(client: TestClient, *, buddy: bool = False) -> dict[str, Any]:
    alice: dict[str, Any] = {"username": "alice", "decklist_text": LEGAL_DECK, "ai": False}
    if buddy:
        alice["buddy"] = True
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [alice, {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": True}],
            "seed": 7,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def record_run(client: TestClient, game_id: str, uid: str, seat: str, role: str = "seat") -> None:
    board = client.get(f"/api/games/{game_id}", params={"uid": uid}).json()
    response = client.post(
        f"/api/games/{game_id}/script-runs",
        params={"uid": uid},
        json={
            "phase": "finish",
            "run_id": f"run-{seat}-{role}",
            "seat": seat,
            "role": role,
            "script_id": "drop-a-land",
            "turn": board["turn_number"],
            "step": board["step"],
            "exit": "completed",
        },
    )
    assert response.status_code == 200, response.text


def concede(client: TestClient, booted: dict[str, Any]) -> None:
    response = client.post(
        f"/api/games/{booted['game_id']}/actions/concede",
        params={"uid": booted["seat_uids"]["alice"]},
        json={"player": "alice"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["winner"] == "bob"


def script_runs(client: TestClient, game_id: str, uid: str | None) -> tuple[int, list[str]]:
    response = client.get(f"/api/games/{game_id}/log", params={"uid": uid} if uid else None)
    if response.status_code != 200:
        return response.status_code, []
    return 200, [
        entry["detail"]["seat"]
        for entry in response.json()["log"]
        if entry["action"] == "script_run"
    ]


def test_after_a_natural_game_over_the_seat_still_reads_its_script_runs(
    client: TestClient,
) -> None:
    booted = boot(client)
    game_id, bob = booted["game_id"], booted["seat_uids"]["bob"]
    record_run(client, game_id, bob, "bob")

    concede(client, booted)

    assert script_runs(client, game_id, bob) == (200, ["bob"])


def test_after_the_game_is_ended_the_seat_still_reads_its_script_runs(
    client: TestClient,
) -> None:
    """The reproduced drop: End game (or Forget) soft-deletes the row, and
    the seat's own read of its own runs used to 404."""
    booted = boot(client)
    game_id, bob = booted["game_id"], booted["seat_uids"]["bob"]
    record_run(client, game_id, bob, "bob")
    concede(client, booted)

    ended = client.delete(
        f"/api/lobby/games/{game_id}", params={"uid": booted["seat_uids"]["alice"]}
    )
    assert ended.status_code == 200, ended.text

    assert script_runs(client, game_id, bob) == (200, ["bob"])
    # The opponent's seat reads the ended log too, and never bob's runs.
    assert script_runs(client, game_id, booted["seat_uids"]["alice"]) == (200, [])


def test_an_ended_game_log_is_still_refused_to_a_caller_holding_no_credential(
    client: TestClient,
) -> None:
    """Serving the archive is scoped to that game's own credentials: nobody
    else learns the ended game still exists."""
    booted = boot(client)
    game_id = booted["game_id"]
    client.delete(f"/api/lobby/games/{game_id}", params={"uid": booted["seat_uids"]["alice"]})

    assert script_runs(client, game_id, None)[0] == 404
    assert script_runs(client, game_id, "not-a-seat-of-this-game")[0] == 404


def test_a_buddy_still_reads_its_humans_script_runs_after_game_over(
    client: TestClient,
) -> None:
    """Advisors hand over too, through the same read: an advisor uid
    resolves to the human it watches."""
    booted = boot(client, buddy=True)
    game_id = booted["game_id"]
    advisor = booted["advisor_uids"]["alice"]
    record_run(client, game_id, advisor, "alice", role="advisor")

    concede(client, booted)

    assert script_runs(client, game_id, advisor) == (200, ["alice"])
