"""Guards the exact boundary T1 nearly broke: `AgentSettings.max_thinking_tokens`
carries a camelCase alias (T13) for exactly one hop - the seat-assignment wire
(`to_wire()`) - and must never leak that alias into a FastAPI `response_model`,
because `response_model_by_alias=True` is FastAPI's default and T1 started
declaring response models on routes that embed `AgentSettings`.

2b's T8 moved that embedding off `GameStateView`/`PlayerView.agent` (the
game-state view carries no `agent` field for any player any more, S6) onto
the lobby's own `GET /api/lobby/games/{id}/seats` (`decks_api.SeatDetail`) -
this test's browser-wire half moved with it.

One test, two assertions, so neither wire can regress without the other
catching it: the lobby-facing seats response must stay snake_case
(`test_game_agent_settings.py` already pins this at the JSON-body level; this
test additionally proves it via a real HTTP round trip), and the
seat-assignment `to_wire()` output must stay camelCase
(`test_agent_settings_contract.py` pins that against the committed schema;
this test is the one place that checks both ends together).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from test_decks_api import LEGAL_DECK as DECK_A
from test_decks_api import LEGAL_DECK_B as DECK_B

from magic_cards_model_api.agent_settings import AgentSettings


def test_the_lobby_seats_wire_stays_snake_case_and_the_seat_assignment_wire_stays_camel_case(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "you", "decklist_text": DECK_A},
                {"username": "bot", "decklist_text": DECK_B, "ai": True},
            ]
        },
    ).json()

    # The lobby wire: GET /api/lobby/games/{id}/seats is
    # response_model=GameSeatsResponse, which embeds AgentSettings at
    # SeatDetail.agent. FastAPI's default response_model_by_alias=True must
    # not turn this into "maxThinkingTokens".
    seats = client.get(f"/api/lobby/games/{created['game_id']}/seats").json()["seats"]
    bot = next(s for s in seats if s["player"] == "bot")
    assert "max_thinking_tokens" in bot["agent"]
    assert "maxThinkingTokens" not in bot["agent"]

    # The seat-assignment wire: to_wire() must still be the one place that
    # produces the camelCase shape contract/agent-settings.schema.json pins.
    wire = AgentSettings(model="sonnet").to_wire()
    assert "maxThinkingTokens" in wire
    assert "max_thinking_tokens" not in wire
