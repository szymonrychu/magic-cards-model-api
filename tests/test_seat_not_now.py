"""F1 (api half): the seat-facing surface of `not_now` - `seatkit.
available_actions` carries `LegalActionsResponse.not_now` through unchanged,
and `mcp._compact_actions` renders each row as not castable right now, kept
apart from `actions` so no tool downstream ever treats it as legal.

`tests/test_legal_actions_endpoint.py` pins the raw HTTP shape (`GET
.../legal-actions`'s own `not_now` key, the engine `blocked_casts` reason and
rule, the viewer check, `legal_actions_before` untouched); this file is what
sits on top of it.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api import mcp as mcp_module
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats, available_actions
from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def wired_seat(monkeypatch: pytest.MonkeyPatch, game_id: str, player: str = "alice"):
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=game_id)
    return seats.seat_for(seats_for(game_id)[player])


def _stack_a_bolt(client: TestClient, game_id: str, player: str = "alice") -> None:
    add_card(client, game_id, "Mountain", player, "battlefield")
    add_card(client, game_id, "Lightning Bolt", player, "hand")
    board = state(client, game_id)
    bolt = find(board["hands"][player], "Lightning Bolt")
    mountain = find(board["battlefield"], "Mountain")
    cast = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": player,
            "card_id": bolt["id"],
            "targets": ["bob"],
            "mana_sources": [mountain["id"]],
        },
    )
    assert cast.status_code == 200, cast.text


def test_seatkit_available_actions_carries_not_now_through(client, game, monkeypatch) -> None:
    _stack_a_bolt(client, game)
    add_card(client, game, "Ocelot Pride", "alice", "hand")
    ocelot = find(state(client, game)["hands"]["alice"], "Ocelot Pride")

    seat = wired_seat(monkeypatch, game)
    result = available_actions(seat, seat.fetch_state())

    blocked = [row for row in result["not_now"] if row["card_id"] == ocelot["id"]]
    assert len(blocked) == 1, result["not_now"]
    assert blocked[0]["rule"] == "117.1a"

    legal = [a for a in result["actions"] if a.get("card_id") == ocelot["id"]]
    assert legal == [], "a blocked cast must never also appear in actions"


def test_available_actions_carries_no_not_now_key_when_nothing_is_blocked(
    client, game, monkeypatch
) -> None:
    seat = wired_seat(monkeypatch, game)
    result = available_actions(seat, seat.fetch_state())
    assert result["not_now"] == []


def test_compact_actions_renders_not_now_rows_as_not_castable_now() -> None:
    available = {
        "actions": [{"kind": "pass_priority", "affordable": "yes"}],
        "not_now": [
            {
                "card_id": "11111111-1111-1111-1111-111111111111",
                "name": "Ocelot Pride",
                "zone": "hand",
                "spell_face": None,
                "reason": "Ocelot Pride is not an instant and has no flash, so alice can only "
                "cast it during their own main phase with the stack empty (CR 117.1a, "
                "CR 702.8a)",
                "rule": "117.1a",
            }
        ],
        "attackers_requested": False,
        "blockers_requested": False,
        "pending_decision": None,
    }

    compacted = mcp_module._compact_actions(available)

    assert "not_now" in compacted
    (row,) = compacted["not_now"]
    assert row["card_id"] == "11111111-1111-1111-1111-111111111111"
    assert row["name"] == "Ocelot Pride"
    assert row["castable_now"] == "no"
    assert row["why_not"] == available["not_now"][0]["reason"]
    assert row["rule"] == "117.1a"
    # Kept apart from `actions` - no tool reading `actions` alone ever sees it.
    assert compacted["actions"] == [{"kind": "pass_priority", "affordable": "yes"}]


def test_compact_actions_omits_not_now_entirely_when_empty() -> None:
    available = {
        "actions": [],
        "not_now": [],
        "attackers_requested": False,
        "blockers_requested": False,
        "pending_decision": None,
    }
    compacted = mcp_module._compact_actions(available)
    assert "not_now" not in compacted
