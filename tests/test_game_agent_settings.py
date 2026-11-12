from __future__ import annotations

from fastapi.testclient import TestClient
from test_decks_api import LEGAL_DECK as DECK_A
from test_decks_api import LEGAL_DECK_B as DECK_B

#: The fixed play default (`AgentSettings.default`, `agent_settings.py`):
#: Sonnet at high effort, since 2026-09-19 (owner direction, cost cutting -
#: the model/effort pickers this module used to exercise are gone, see
#: `test_ui_lobby.py`'s equivalent update). No request may override it any
#: more - `create()` below never sends an `agent` key at all.
DEFAULT_AGENT = {"model": "claude-sonnet-5", "effort": "high", "max_thinking_tokens": None}


def create(client: TestClient, ai: bool = True) -> dict:
    bot = {"username": "bot", "decklist_text": DECK_B, "ai": ai}
    return client.post(
        "/api/lobby/games", json={"players": [{"username": "you", "decklist_text": DECK_A}, bot]}
    ).json()


def test_agent_settings_default_and_show_on_the_player(client: TestClient) -> None:
    created = create(client)
    assert created["agents"] == {"bot": DEFAULT_AGENT}
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
    assert bot_seat["agent"] == DEFAULT_AGENT
    you_seat = next(s for s in seats if s["player"] == "you")
    assert you_seat.get("agent") is None


def test_agent_field_on_an_ai_seat_is_rejected(client: TestClient) -> None:
    """Model is no longer a per-request choice (2026-09-19): `NewUiPlayer`
    carries no `agent`/`buddy_agent` field at all any more, and
    `RequestModel`'s `extra="forbid"` turns a request that still sends one
    into a 422 - the same refusal `test_agent_field_on_a_human_seat_is_
    rejected_too` gets for a human seat, since there is no longer a
    "which seat may name it" distinction to enforce: neither may."""
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "you", "decklist_text": DECK_A},
                {
                    "username": "bot",
                    "decklist_text": DECK_B,
                    "ai": True,
                    "agent": {"model": "opus", "effort": "xhigh", "max_thinking_tokens": 8000},
                },
            ]
        },
    )
    assert response.status_code == 422


def test_agent_field_on_a_human_seat_is_rejected_too(client: TestClient) -> None:
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
    assert response.status_code == 422


def test_an_old_stored_agent_still_loads_even_though_no_request_can_write_one(
    client: TestClient,
) -> None:
    """A game created before 2026-09-19 could carry any model/effort a
    request chose (`{"model": "opus", "effort": "medium", ...}` was the old
    server default - see `test_agent_settings_contract.py`'s own history).
    `NewUiPlayer` losing the field only closes the WRITE path; every read of
    already-stored `AgentSettings` (`store.register_agent_settings`,
    unchanged by this pass) must still work exactly as it always did."""
    from magic_cards_model_api.agent_settings import AgentSettings
    from magic_cards_model_api.store import store

    created = create(client)
    game_id = created["game_id"]
    old_shaped = AgentSettings(model="opus", effort="medium", max_thinking_tokens=None)
    store.register_agent_settings(game_id, {"bot": old_shaped})

    listing = client.get("/api/lobby/games", params={"uids": created["seat_uids"]["you"]}).json()[
        "games"
    ]
    row = next(g for g in listing if g["game_id"] == game_id)
    assert row["agents"]["bot"] == {
        "model": "opus",
        "effort": "medium",
        "max_thinking_tokens": None,
    }

    seats = client.get(f"/api/lobby/games/{game_id}/seats").json()["seats"]
    bot_seat = next(s for s in seats if s["player"] == "bot")
    assert bot_seat["agent"]["model"] == "opus"


def test_buddy_agent_field_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {
                    "username": "you",
                    "decklist_text": DECK_A,
                    "buddy": True,
                    "buddy_agent": {"model": "opus"},
                },
                {"username": "bot", "decklist_text": DECK_B, "ai": True},
            ]
        },
    )
    assert response.status_code == 422


# -- per-seat model and effort on the lobby (T-156) ----------------------------------


def create_with(client: TestClient, **seat: object) -> object:
    bot = {"username": "bot", "decklist_text": DECK_B, "ai": True, **seat}
    return client.post(
        "/api/lobby/games", json={"players": [{"username": "you", "decklist_text": DECK_A}, bot]}
    )


def test_an_ai_seat_may_name_its_model_and_effort(client: TestClient) -> None:
    response = create_with(client, model="claude-haiku-4-5", effort="max")
    assert response.status_code == 200
    created = response.json()
    expected = {"model": "claude-haiku-4-5", "effort": "max", "max_thinking_tokens": None}
    assert created["agents"] == {"bot": expected}
    seats = client.get(f"/api/lobby/games/{created['game_id']}/seats").json()["seats"]
    assert next(s for s in seats if s["player"] == "bot")["agent"] == expected


def test_a_seat_naming_only_one_keeps_the_deployment_default_for_the_other(
    client: TestClient,
) -> None:
    created = create_with(client, effort="low").json()
    assert created["agents"]["bot"]["model"] == "claude-sonnet-5"
    assert created["agents"]["bot"]["effort"] == "low"


def test_a_bad_model_or_effort_is_refused_naming_the_allowed_values(client: TestClient) -> None:
    bad_model = create_with(client, model="gpt-9")
    assert bad_model.status_code == 422
    assert "claude-opus-5-5" in bad_model.text
    assert create_with(client, effort="extreme").status_code == 422


def test_model_and_effort_on_a_human_seat_are_refused(client: TestClient) -> None:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "you", "decklist_text": DECK_A, "effort": "low"},
                {"username": "bot", "decklist_text": DECK_B, "ai": True},
            ]
        },
    )
    assert response.status_code == 422


def test_ai_seat_restart_keeps_the_seats_model_and_effort(client: TestClient, monkeypatch) -> None:
    from magic_cards_model_api import ai_seat

    created = create_with(client, model="claude-haiku-4-5", effort="max").json()
    started: list = []
    monkeypatch.setattr(ai_seat.registry, "start", lambda **kw: started.append(kw) or {})
    uid = created["seat_uids"]["bot"]
    client.post(f"/api/lobby/games/{created['game_id']}/ai-seat/start", json={"uid": uid})
    assert (started[-1]["agent"].model, started[-1]["agent"].effort) == ("claude-haiku-4-5", "max")
