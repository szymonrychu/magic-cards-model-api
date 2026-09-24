"""DR2: `report_problem` gains a `category` - `engine` (a rules/engine/api
bug), `dsl` (a play the DSL could not express, or a missing DSL feature) or
`ux` (MCP tool friction) - so a `watch_game`-style consumer can separate an
engine bug from a DSL gap or a tool-surface complaint instead of stopping on
every report alike.

`POST /api/games/{id}/report` (`schemas.ReportProblemRequest.category`)
persists it in the audit row; a bad value is a 422 before the route ever
runs; a row written before this field existed carries no `category` key at
all and reads back as `engine` everywhere (`audit.problem_category`). The
MCP tool `report_problem(player_uid, text, category="engine")` forwards
whatever the caller passed straight onto the same route.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from test_mcp_input_required import call_tool, wired

from magic_cards_model_api import audit
from magic_cards_model_api.mcp import build_server


def _report(client: TestClient, game_id: str, key: str, **overrides: Any):
    body = {"player": "alice", "text": "something looks wrong", **overrides}
    return client.post(f"/api/games/{game_id}/report", json=body, params={"uid": key})


def _rows(client: TestClient, game_id: str, key: str) -> list[dict[str, Any]]:
    log = client.get(f"/api/games/{game_id}/log", params={"uid": key}).json()["log"]
    return [row for row in log if row["action"] == "report_problem"]


# ---------------------------------------------------------------------------
# the route: round trip, default, and the 422
# ---------------------------------------------------------------------------


def test_the_chosen_category_round_trips_into_the_ledger_row(
    client: TestClient, game: str, admin_key: str
) -> None:
    response = _report(client, game, admin_key, category="dsl")
    assert response.status_code == 200, response.text
    [row] = _rows(client, game, admin_key)
    assert row["detail"]["category"] == "dsl"


def test_every_category_is_accepted(client: TestClient, game: str, admin_key: str) -> None:
    for category in ("engine", "dsl", "ux"):
        response = _report(client, game, admin_key, category=category, text=f"a {category} note")
        assert response.status_code == 200, response.text
    rows = _rows(client, game, admin_key)
    assert [row["detail"]["category"] for row in rows] == ["engine", "dsl", "ux"]


def test_category_defaults_to_engine_when_not_given(
    client: TestClient, game: str, admin_key: str
) -> None:
    body = {"player": "alice", "text": "no category named at all"}
    response = client.post(f"/api/games/{game}/report", json=body, params={"uid": admin_key})
    assert response.status_code == 200, response.text
    [row] = _rows(client, game, admin_key)
    assert row["detail"]["category"] == "engine"


def test_a_bad_category_is_refused_with_422(client: TestClient, game: str, admin_key: str) -> None:
    response = _report(client, game, admin_key, category="not-a-real-category")
    assert response.status_code == 422, response.text
    assert _rows(client, game, admin_key) == []


# ---------------------------------------------------------------------------
# legacy rows: written before `category` existed, read as `engine`
# ---------------------------------------------------------------------------


def test_a_legacy_row_with_no_category_key_reads_as_engine() -> None:
    """The exact call the route made before this field existed: no
    `category` keyword at all - not even `None`."""
    audit.record("no-such-game-dr2-report", "report_problem", player="alice", text="old report")
    [row] = [
        entry
        for entry in audit.entries("no-such-game-dr2-report")
        if entry["action"] == "report_problem"
    ]
    assert "category" not in row["detail"]
    assert audit.problem_category(row["detail"]) == "engine"


def test_problem_category_prefers_an_explicit_value_over_the_default() -> None:
    assert audit.problem_category({"category": "ux"}) == "ux"
    assert audit.problem_category({"category": "dsl"}) == "dsl"
    assert audit.problem_category({}) == "engine"


# ---------------------------------------------------------------------------
# the MCP tool forwards whatever category it was given
# ---------------------------------------------------------------------------


def test_the_mcp_tool_forwards_the_chosen_category(
    client: TestClient, game: str, admin_key: str, monkeypatch
) -> None:
    _seats, uids = wired(client, monkeypatch, game)
    server = build_server(_seats)

    result = call_tool(
        server,
        "report_problem",
        {
            "player_uid": uids["alice"],
            "text": "a DSL gap - no move for suspend",
            "category": "dsl",
        },
    )
    assert '"recorded": true' in result.content[0].text.lower(), result.content[0].text

    [row] = _rows(client, game, admin_key)
    assert row["detail"]["category"] == "dsl"
    assert row["detail"]["player"] == "alice"


def test_the_mcp_tool_defaults_category_to_engine(
    client: TestClient, game: str, admin_key: str, monkeypatch
) -> None:
    _seats, uids = wired(client, monkeypatch, game)
    server = build_server(_seats)

    call_tool(
        server,
        "report_problem",
        {"player_uid": uids["bob"], "text": "the engine misjudged something"},
    )

    [row] = _rows(client, game, admin_key)
    assert row["detail"]["category"] == "engine"
