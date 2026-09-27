"""FD5 (6): `GET /api/ops/dsl-coverage` - per-game and aggregated DSL
coverage across every game, behind an env-configured `OPS_TOKEN` rather than
any per-game admin key."""

from __future__ import annotations

from fastapi.testclient import TestClient
from test_review import _played_out

from magic_cards_model_api import records
from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def test_ops_dsl_coverage_refuses_without_the_configured_token(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv(records.OPS_TOKEN_ENV, "correct-token")
    response = client.get("/api/ops/dsl-coverage")
    assert response.status_code == 404, response.text

    wrong = client.get("/api/ops/dsl-coverage", params={"key": "wrong-token"})
    assert wrong.status_code == 404, wrong.text


def test_ops_dsl_coverage_refuses_every_caller_when_unconfigured(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.delenv(records.OPS_TOKEN_ENV, raising=False)
    response = client.get("/api/ops/dsl-coverage", params={"key": "anything"})
    assert response.status_code == 404, response.text


def test_ops_dsl_coverage_reads_a_finished_game_with_no_per_game_admin_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv(records.OPS_TOKEN_ENV, "the-real-token")
    client = TestClient(app)
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        # The route reads oldest-first and stops at `limit` (default 200); in a
        # shared test store other tests' games come first, so read them all.
        every_game = len(store.ids_since(None))
        response = client.get(
            "/api/ops/dsl-coverage", params={"key": "the-real-token", "limit": every_game}
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["since"] is None
        game_row = next(row for row in body["games"] if row["game_id"] == game_id)
        # `play_land` is a real, expressible move in this pool - it must show
        # up both per-game and in the folded aggregate.
        assert "play_land" in game_row["dsl_coverage"], game_row
        assert "play_land" in body["aggregated"], body["aggregated"]
        assert body["aggregated"]["play_land"]["count"] >= 1
    finally:
        store.delete(game_id)


def test_ops_dsl_coverage_since_filters_by_creation_time(monkeypatch) -> None:
    monkeypatch.setenv(records.OPS_TOKEN_ENV, "the-real-token")
    client = TestClient(app)
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        future = "2999-01-01T00:00:00+00:00"
        response = client.get(
            "/api/ops/dsl-coverage", params={"key": "the-real-token", "since": future}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert all(row["game_id"] != game_id for row in body["games"])
    finally:
        store.delete(game_id)


def test_ops_dsl_coverage_skips_a_game_still_in_progress(monkeypatch, client, game) -> None:
    monkeypatch.setenv(records.OPS_TOKEN_ENV, "the-real-token")
    response = client.get("/api/ops/dsl-coverage", params={"key": "the-real-token"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert all(row["game_id"] != game for row in body["games"])
