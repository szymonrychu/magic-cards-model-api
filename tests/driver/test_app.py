import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mtg_agent.driver.app import build_app
from mtg_agent.driver.config import DriverSettings

DECKLIST = "4 Psychic Frog\n4 Fatal Push\n2 Consign to Memory\n"


@pytest.fixture
def world(tmp_path):
    """A fake game server and a fake wrapper, both real FastAPI apps driven
    over ASGI by `TestClient` (itself an `httpx.Client`) - no mocking library
    involved."""
    guides = tmp_path / "guides" / "decks" / "dimir-frog"
    guides.mkdir(parents=True)
    (guides / "decklist.txt").write_text(DECKLIST)
    (guides / "GAMEPLAN_GUIDE.md").write_text("# gameplan\n")

    state = {"winner": None, "decklist": DECKLIST, "agent": "up", "deleted": 0}
    submitted: list[dict] = []

    game = FastAPI()

    @game.get("/api/games/{game_id}")
    def get_game(game_id: str, uid: str):
        return {
            "id": game_id,
            "players": [
                {"username": "bob", "decklist": state["decklist"]},
                {"username": "alice"},
            ],
            "winner": state["winner"],
        }

    wrapper = FastAPI()

    @wrapper.post("/v1/messages", status_code=202)
    def post_message(body: dict):
        if state["agent"] == "booting":
            raise HTTPException(status_code=503, detail="session not ready")
        if state["agent"] == "down":
            raise httpx.ConnectError("connection refused")
        submitted.append(body)
        return {"turnId": f"turn-{len(submitted)}"}

    @wrapper.delete("/v1/session", status_code=202)
    def delete_session():
        if state["agent"] == "down":
            raise httpx.ConnectError("connection refused")
        state["deleted"] += 1
        # A real wrapper exits here; the replacement container boots for a
        # while before it can take a turn.
        state["agent"] = "booting"
        return {"stopping": True, "exiting": True}

    def client_factory(base_url: str) -> httpx.Client:
        app = game if "game" in base_url else wrapper
        return TestClient(app, base_url=base_url)

    settings = DriverSettings(
        game_api="http://game:8000",
        wrapper_api="http://agent:8080",
        wrapper_secret="",
        guides_root=tmp_path / "guides",
        memory_root=tmp_path / "memory",
        workspace=tmp_path / "workspace",
        self_url="http://driver:8090",
        http_addr=":8090",
        agent_boot_wait=1.0,
        agent_retry_interval=0.01,
    )
    client = TestClient(build_app(settings, client_factory=client_factory))
    return client, settings, submitted, state


def dispatch(client, uid: str = "secret-uid", game_id: str = "g-1") -> httpx.Response:
    return client.post(
        "/dispatch",
        json={
            "uid": uid,
            "game_id": game_id,
            "player": "bob",
            "api_base": "http://game:8000",
        },
    )


def wait_for(predicate, timeout: float = 5.0):
    """Poll a real endpoint until the driver's background work lands. Bounded,
    so a regression fails the test instead of hanging the suite."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def play_out_the_match(client, state) -> None:
    """Dispatch, win, and let the reflection turn complete."""
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"})


def test_dispatch_identifies_the_deck_and_reports_it(world):
    client, _, _, _ = world
    body = json.loads(dispatch(client).text)
    assert body["deck"] == "dimir-frog"
    assert body["turnId"] == "turn-1"


def test_dispatch_writes_current_game_md_into_the_workspace(world):
    client, settings, _, _ = world
    dispatch(client)
    text = (settings.workspace / "current-game.md").read_text()
    assert "secret-uid" in text
    assert "dimir-frog" in text
    assert "4 Psychic Frog" in text


def test_dispatch_pushes_turn_zero_with_its_own_callback_url(world):
    client, settings, submitted, _ = world
    dispatch(client)
    turn = submitted[0]
    assert turn["callbackUrl"] == "http://driver:8090/turn-done"
    assert "current-game.md" in turn["text"]
    assert "wait_for_priority" in turn["text"]


def test_turn_zero_never_carries_the_opponents_list(world):
    client, _, submitted, _ = world
    dispatch(client)
    text = submitted[0]["text"]
    assert "alice" in text, "the opponent's seat NAME is public at a real table"
    lowered = text.lower()
    assert "opponent's decklist" not in lowered
    assert "opponent's deck" not in lowered


def test_dispatch_of_an_unguided_deck_still_starts_the_game(world):
    client, _, submitted, state = world
    state["decklist"] = "4 Lightning Bolt\n20 Mountain\n"
    body = dispatch(client).json()
    assert body["deck"] is None
    assert submitted[0]["text"]


def test_a_second_dispatch_for_the_same_uid_does_not_push_a_second_turn_zero(world):
    client, _, submitted, _ = world
    dispatch(client)
    dispatch(client)
    assert len(submitted) == 1


def test_turn_done_mid_game_pushes_nothing(world):
    client, _, submitted, _ = world
    dispatch(client)
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "ok"})
    assert len(submitted) == 1


def test_turn_done_after_a_win_pushes_exactly_one_reflection_turn(world):
    client, _, submitted, state = world
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    assert len(submitted) == 2
    assert "The game is over" in submitted[1]["text"]
    assert "bob" in submitted[1]["text"]

    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"})
    assert len(submitted) == 2, "the reflection turn must not trigger another reflection"


def test_status_reports_the_current_assignment(world):
    client, _, _, _ = world
    dispatch(client)
    body = client.get("/status").json()
    assert body["gameId"] == "g-1"
    assert body["deck"] == "dimir-frog"
    assert body["player"] == "bob"
    assert body["lastTurnId"] == "turn-1"


def test_status_before_any_dispatch_is_idle(world):
    client, _, _, _ = world
    assert client.get("/status").json()["gameId"] is None


def test_healthz(world):
    client, _, _, _ = world
    assert client.get("/healthz").status_code == 200


def test_settings_from_env_reads_the_container_wiring():
    settings = DriverSettings.from_env(
        {
            "GAME_API": "http://game:8000",
            "WRAPPER_API": "http://agent:8080",
            "GUIDES_ROOT": "/guides",
            "MEMORY_ROOT": "/workspace/memory",
            "DRIVER_SELF_URL": "http://driver:8090",
        }
    )
    assert settings.game_api == "http://game:8000"
    assert settings.guides_root == Path("/guides")
    assert settings.memory_root == Path("/workspace/memory")


def test_turn_zero_inlines_the_decklist_because_containers_share_no_disk(world):
    client, _, submitted, _ = world
    dispatch(client)
    assert "4 Psychic Frog" in submitted[0]["text"]


# ---- one match per container ------------------------------------------------


def test_a_second_game_arriving_mid_match_is_refused_by_name(world):
    client, _, submitted, _ = world
    dispatch(client)
    response = dispatch(client, uid="other-uid", game_id="g-2")
    assert response.status_code == 409
    assert "g-1" in response.json()["detail"]
    assert len(submitted) == 1, "the live match must not be retargeted"


def test_the_completed_reflection_turn_stops_the_agent(world):
    client, _, _, state = world
    play_out_the_match(client, state)
    assert state["deleted"] == 1


def test_retiring_clears_the_assignment_so_the_next_dispatch_starts_clean(world):
    client, _, _, state = world
    play_out_the_match(client, state)
    assert client.get("/status").json()["gameId"] is None


def test_the_next_match_is_accepted_once_the_replacement_agent_is_up(world):
    client, _, submitted, state = world
    play_out_the_match(client, state)
    state["agent"] = "up"  # the replacement container finished booting
    state["winner"] = None
    response = dispatch(client, uid="uid-2", game_id="g-2")
    assert response.status_code == 202
    assert response.json()["turnId"] == "turn-3"
    assert client.get("/status").json()["gameId"] == "g-2"


def test_the_agent_is_stopped_only_once_per_match(world):
    client, _, _, state = world
    play_out_the_match(client, state)
    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete"})
    assert state["deleted"] == 1


def test_a_match_is_retired_even_when_the_agent_is_already_gone(world):
    client, _, _, state = world
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete"})
    state["agent"] = "down"
    body = client.post("/turn-done", json={"turnId": "turn-2", "state": "complete"}).json()
    assert body["retired"] is True
    assert body["agentStopped"] is False
    assert client.get("/status").json()["gameId"] is None


def test_a_game_that_ends_with_the_agent_already_dead_is_still_retired(world):
    client, _, _, state = world
    dispatch(client)
    state["winner"] = "bob"
    state["agent"] = "down"
    body = client.post("/turn-done", json={"turnId": "turn-1", "state": "failed"}).json()
    assert body["retired"] is True
    assert body["reflected"] is False
    assert client.get("/status").json()["gameId"] is None


def test_a_dispatch_while_the_agent_is_booting_is_accepted_and_retried(world):
    client, _, submitted, state = world
    state["agent"] = "booting"
    body = dispatch(client).json()
    assert body["pending"] is True
    assert body["turnId"] is None
    assert body["deck"] == "dimir-frog", "the deck is known even before the agent answers"
    assert client.get("/status").json()["gameId"] == "g-1", "the game is not dropped"

    state["agent"] = "up"  # the replacement finished booting
    assert wait_for(lambda: client.get("/status").json()["lastTurnId"]) == "turn-1"
    assert len(submitted) == 1


def test_a_dispatch_to_an_agent_that_never_comes_back_gives_up_and_frees_the_driver(world):
    client, _, _, state = world
    state["agent"] = "down"
    assert dispatch(client).json()["pending"] is True

    def failed_status():
        status = client.get("/status").json()
        return status if status["lastError"] else None

    status = wait_for(failed_status)
    assert status["gameId"] is None, "a game nobody can play must not wedge the driver"
    assert "unreachable" in status["lastError"]


def test_the_retry_stops_if_the_match_is_retired_under_it(world):
    client, _, submitted, state = world
    state["agent"] = "down"
    dispatch(client)
    wait_for(lambda: client.get("/status").json()["lastError"])
    state["agent"] = "up"
    time.sleep(0.05)
    assert submitted == [], "the abandoned game must never be pushed later"


def test_settings_read_the_boot_wait_from_the_environment():
    settings = DriverSettings.from_env(
        {"AGENT_BOOT_WAIT_SECONDS": "42", "AGENT_RETRY_INTERVAL_SECONDS": "0.5"}
    )
    assert settings.agent_boot_wait == 42.0
    assert settings.agent_retry_interval == 0.5
