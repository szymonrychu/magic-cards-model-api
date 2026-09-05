"""Wrapper mode: End game hands the agent back.

The live symptom this used to fix by resetting implicitly. One agent container
plays one match: the driver holds a single assignment and answers 409 - "this
agent is already playing game <id>" - to any dispatch for another game, and
only ever lets go when the match it is holding *ends*. Before End game
existed, a human who went back to the lobby and started a second game never
ended the first, so the second game's seat could not be dispatched at all: the
play UI showed a fallback, and the opponent never moved.

Now games the lobby boots live side by side and `DELETE /api/ui/games/{id}`
(End game) is the only reset - so this exercises the release through that
endpoint rather than through starting a second game.

Nothing here is mocked. The driver is the real `build_app`; the game server is
the real app, booting real games from real Scryfall printings; only the agent
is a double, and it is a real FastAPI app answering the real wrapper routes
over ASGI (`tests/driver/test_app.py`'s pattern), because the alternative is
spawning a real `claude` per test.

The regression this file also guards: End game must NOT destroy what the
continuous agent has learned. Its notes live under `MEMORY_ROOT`, a mounted
volume; retiring an agent ends its *session* and must leave that directory
exactly as it found it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from magic_cards_model_api import ai_seat
from magic_cards_model_api.main import app as game_app
from mtg_agent.driver.app import build_app
from mtg_agent.driver.config import DriverSettings

LEGAL_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
LEGAL_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The real game server, the real driver, and an agent double.

    Wired the way the compose stack wires them: the game server reaches the
    driver over `DRIVER_URL`, and the driver reaches the game server and the
    agent over its own settings. Every hop is a real ASGI request.
    """
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "notes.md").write_text("# what I learned last game\nbolt the bird\n")

    agent_state: dict[str, Any] = {"deleted": 0, "turns": []}
    agent = FastAPI()

    @agent.post("/v1/messages", status_code=202)
    def post_message(body: dict) -> dict:
        agent_state["turns"].append(body)
        return {"turnId": f"turn-{len(agent_state['turns'])}"}

    @agent.delete("/v1/session", status_code=202)
    def delete_session() -> dict:
        # A real wrapper exits here and its restart policy brings up a fresh
        # container. Modelled as an instantly-replaced agent so this test is
        # about the release, not about boot timing (which
        # `test_app.py::push_turn_when_reachable` already covers).
        agent_state["deleted"] += 1
        return {"stopping": True, "exiting": True}

    game_client = TestClient(game_app)

    settings = DriverSettings(
        game_api="http://game:8000",
        wrapper_api="http://agent:8080",
        guides_root=tmp_path / "guides",
        memory_root=memory,
        workspace=tmp_path / "workspace",
        self_url="http://driver:8090",
        agent_boot_wait=1.0,
        agent_retry_interval=0.01,
    )

    def client_factory(base_url: str) -> httpx.Client:
        return TestClient(game_app if "game" in base_url else agent, base_url=base_url)

    driver_client = TestClient(build_app(settings, client_factory=client_factory))

    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("DRIVER_URL", "http://driver:8090")
    monkeypatch.setattr(
        ai_seat, "_driver_client", lambda base: TestClient(driver_client.app, base_url=base)
    )
    registry = ai_seat.AiSeatRegistry()
    monkeypatch.setattr(ai_seat, "registry", registry)

    with game_client:
        yield game_client, driver_client, agent_state, memory


def boot(client: TestClient, *, seed: int) -> dict[str, Any]:
    response = client.post(
        "/api/ui/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": LEGAL_DECK, "ai": False},
                {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": True},
            ],
            "seed": seed,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def end(client: TestClient, booted: dict[str, Any]) -> dict[str, Any]:
    """End a lobby game exactly as the browser's End game button does."""
    response = client.delete(
        f"/api/ui/games/{booted['game_id']}", params={"uid": booted["seat_uids"]["alice"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def start_ai_seat(client: TestClient, booted: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        f"/api/ui/games/{booted['game_id']}/ai-seat/start",
        json={"uid": booted["seat_uids"]["bob"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_the_next_games_seat_reaches_the_agent_after_end_game(world) -> None:
    """The whole point: ending a game from the lobby frees the driver, so a
    game started afterward gets a real dispatch, not the 409-turned-fallback
    the old assignment used to cause."""
    game_client, driver_client, agent_state, _ = world
    first = boot(game_client, seed=1)
    assert start_ai_seat(game_client, first)["mechanism"] == "wrapper"
    assert driver_client.get("/status").json()["gameId"] == first["game_id"]

    end(game_client, first)
    # The driver let go of the old game as part of the release.
    assert driver_client.get("/status").json()["gameId"] is None
    assert agent_state["deleted"] == 1

    second = boot(game_client, seed=2)
    try:
        status = start_ai_seat(game_client, second)
        assert status["mechanism"] == "wrapper", status
        assert driver_client.get("/status").json()["gameId"] == second["game_id"]
    finally:
        game_client.delete(f"/api/games/{second['game_id']}", params={"key": second["admin_key"]})


def test_the_agents_memory_survives_end_game(world) -> None:
    """The regression that would quietly ruin the continuous agent: ending
    the seat must end the session, not the notes."""
    game_client, _, _, memory = world
    first = boot(game_client, seed=1)
    start_ai_seat(game_client, first)
    before = {path.name: path.read_text() for path in memory.iterdir()}

    end(game_client, first)

    after = {path.name: path.read_text() for path in memory.iterdir()}
    assert after == before
    assert memory.is_dir()


def test_the_release_is_recorded_against_the_ended_game(world) -> None:
    game_client, _, _, _ = world
    first = boot(game_client, seed=1)
    start_ai_seat(game_client, first)
    covered = end(game_client, first)
    (released,) = covered["ai_seats_released"]
    assert released["player"] == "bob"
    assert released["mechanism"] == "wrapper"
    assert released["driver"]["released"] is True
    assert released["driver"]["gameId"] == first["game_id"]
    # Abandoned, not played out: no reflection turn was written, and the
    # answer says so rather than leaving it to be assumed.
    assert released["driver"]["reflected"] is False


def test_the_old_seats_status_is_forgotten_after_end_game(world) -> None:
    """A stale uid must not be able to poll a status out of the registry: the
    game it belonged to is gone, and reporting on it would put a dead game's
    opponent back on the panel of a live one."""
    game_client, _, _, _ = world
    first = boot(game_client, seed=1)
    start_ai_seat(game_client, first)
    old_uid = first["seat_uids"]["bob"]
    end(game_client, first)
    second = boot(game_client, seed=2)
    try:
        answer = game_client.get(
            f"/api/ui/games/{second['game_id']}/ai-seat/status", params={"uid": old_uid}
        ).json()
        assert answer["state"] == "not_started"
        assert answer["mechanism"] is None
    finally:
        game_client.delete(f"/api/games/{second['game_id']}", params={"key": second["admin_key"]})


def test_the_driver_refuses_a_release_for_a_game_it_is_not_playing(world) -> None:
    """The release names the game it is giving back. A caller naming another
    one is refused rather than allowed to retire somebody else's match."""
    game_client, driver_client, _, _ = world
    first = boot(game_client, seed=1)
    try:
        start_ai_seat(game_client, first)
        refused = driver_client.post("/release", json={"game_id": "some-other-game"})
        assert refused.status_code == 409
        assert driver_client.get("/status").json()["gameId"] == first["game_id"]
    finally:
        game_client.delete(f"/api/games/{first['game_id']}", params={"key": first["admin_key"]})


def test_releasing_an_idle_driver_is_not_an_error(world) -> None:
    _, driver_client, _, _ = world
    answer = driver_client.post("/release", json={})
    assert answer.status_code == 202
    assert answer.json() == {"released": False, "reason": "this agent has no assignment"}
