from __future__ import annotations

from fastapi.testclient import TestClient
from test_decks_api import LEGAL_DECK as DECK_A
from test_decks_api import LEGAL_DECK_B as DECK_B


def create(client: TestClient, agent: dict | None = None, ai: bool = True) -> dict:
    bot = {"username": "bot", "decklist_text": DECK_B, "ai": ai}
    if agent is not None:
        bot["agent"] = agent
    return client.post(
        "/api/lobby/games", json={"players": [{"username": "you", "decklist_text": DECK_A}, bot]}
    ).json()


def test_agent_settings_default_and_show_on_the_player(client: TestClient) -> None:
    created = create(client)
    assert created["agents"] == {
        "bot": {"model": "opus", "effort": "medium", "max_thinking_tokens": None}
    }
    # 2b's T8: the game-state view no longer carries `agent` on any player
    # block at all - it moved to the lobby's own `GET
    # /api/lobby/games/{id}/seats` (S6).
    state = client.get(
        f"/api/games/{created['game_id']}", params={"uid": created["seat_uids"]["you"]}
    ).json()
    bot = next(p for p in state["players"] if p["username"] == "bot")
    assert "agent" not in bot
    you = next(p for p in state["players"] if p["username"] == "you")
    assert "agent" not in you

    seats = client.get(f"/api/lobby/games/{created['game_id']}/seats").json()["seats"]
    bot_seat = next(s for s in seats if s["player"] == "bot")
    assert bot_seat["agent"] == {"model": "opus", "effort": "medium", "max_thinking_tokens": None}
    you_seat = next(s for s in seats if s["player"] == "you")
    assert you_seat.get("agent") is None


def test_agent_settings_are_stored_as_given(client: TestClient) -> None:
    created = create(client, {"model": "opus", "effort": "xhigh", "max_thinking_tokens": 8000})
    assert created["agents"]["bot"] == {
        "model": "opus",
        "effort": "xhigh",
        "max_thinking_tokens": 8000,
    }


def test_agent_on_a_human_seat_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {
                    "username": "you",
                    "decklist_text": DECK_A,
                    "agent": {"model": "opus", "effort": "high"},
                },
                {"username": "bot", "decklist_text": DECK_B, "ai": True},
            ]
        },
    )
    assert response.status_code == 400
    assert "agent" in response.json()["detail"]
