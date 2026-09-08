import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mtg_agent.driver.config import DriverSettings
from tests.driver.world import build_world


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path)


def dispatch(
    client, uid: str = "secret-uid", game_id: str = "g-1", agent: dict | None = None
) -> httpx.Response:
    return client.post(
        "/dispatch",
        json={
            "uid": uid,
            "game_id": game_id,
            "player": "bob",
            "api_base": "http://game:8000",
            "agent": agent,
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
    client = world.client
    body = json.loads(dispatch(client).text)
    assert body["deck"] == "dimir-frog"
    assert body["turnId"] == "turn-1"


def test_dispatch_writes_current_game_md_into_the_workspace(world):
    client = world.client
    settings = world.settings
    dispatch(client)
    text = (settings.workspace / "current-game.md").read_text()
    assert "secret-uid" in text
    assert "dimir-frog" in text
    assert "4 Psychic Frog" in text


def test_dispatch_pushes_turn_zero_with_its_own_callback_url(world):
    client = world.client
    submitted = world.submitted
    dispatch(client)
    turn = submitted[0]
    assert turn["callbackUrl"] == "http://driver:8090/turn-done"
    assert "current-game.md" in turn["text"]
    assert "wait_for_priority" in turn["text"]


def test_turn_zero_never_carries_the_opponents_list(world):
    client = world.client
    submitted = world.submitted
    dispatch(client)
    text = submitted[0]["text"]
    assert "alice" in text, "the opponent's seat NAME is public at a real table"
    lowered = text.lower()
    assert "opponent's decklist" not in lowered
    assert "opponent's deck" not in lowered


def test_dispatch_of_an_unguided_deck_still_starts_the_game(world):
    client = world.client
    submitted = world.submitted
    state = world.state
    state["decklist"] = "4 Lightning Bolt\n20 Mountain\n"
    body = dispatch(client).json()
    assert body["deck"] is None
    assert submitted[0]["text"]


def test_a_second_dispatch_for_the_same_uid_does_not_push_a_second_turn_zero(world):
    client = world.client
    submitted = world.submitted
    dispatch(client)
    dispatch(client)
    assert len(submitted) == 1


def test_turn_done_mid_game_pushes_nothing(world):
    client = world.client
    submitted = world.submitted
    dispatch(client)
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "ok"})
    assert len(submitted) == 1


def test_turn_done_after_a_win_pushes_exactly_one_reflection_turn(world):
    client = world.client
    submitted = world.submitted
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    assert len(submitted) == 2
    assert "The game is over" in submitted[1]["text"]
    assert "bob" in submitted[1]["text"]

    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"})
    assert len(submitted) == 2, "the reflection turn must not trigger another reflection"


def test_status_reports_the_current_assignment(world):
    client = world.client
    dispatch(client)
    body = client.get("/status").json()
    assert body["gameId"] == "g-1"
    assert body["deck"] == "dimir-frog"
    assert body["player"] == "bob"
    assert body["lastTurnId"] == "turn-1"


def test_status_before_any_dispatch_is_idle(world):
    client = world.client
    assert client.get("/status").json()["gameId"] is None


def test_status_reports_the_dispatched_agent_settings(world):
    """`DispatchIn.agent` is carried onto `Assignment` and shown back in
    `/status`, so the game server (and a human reading the driver's own
    status) can see what model/effort this container is running under."""
    client = world.client
    agent = {"model": "opus", "effort": "max", "maxThinkingTokens": None}
    dispatch(client, agent=agent)
    assert client.get("/status").json()["agent"] == agent


def test_status_agent_is_none_when_dispatch_carried_none(world):
    client = world.client
    dispatch(client)
    assert client.get("/status").json()["agent"] is None


def test_healthz(world):
    client = world.client
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
    client = world.client
    submitted = world.submitted
    dispatch(client)
    assert "4 Psychic Frog" in submitted[0]["text"]


# ---- one match per container ------------------------------------------------


def test_a_second_game_arriving_mid_match_is_refused_by_name(world):
    client = world.client
    submitted = world.submitted
    dispatch(client)
    response = dispatch(client, uid="other-uid", game_id="g-2")
    assert response.status_code == 409
    assert "g-1" in response.json()["detail"]
    assert len(submitted) == 1, "the live match must not be retargeted"


def test_the_completed_reflection_turn_stops_the_agent(world):
    client = world.client
    state = world.state
    play_out_the_match(client, state)
    assert state["deleted"] == 1


def test_retiring_clears_the_assignment_so_the_next_dispatch_starts_clean(world):
    client = world.client
    state = world.state
    play_out_the_match(client, state)
    assert client.get("/status").json()["gameId"] is None


def test_the_next_match_is_accepted_once_the_replacement_agent_is_up(world):
    client = world.client
    state = world.state
    play_out_the_match(client, state)
    state["agent"] = "up"  # the replacement container finished booting
    state["winner"] = None
    response = dispatch(client, uid="uid-2", game_id="g-2")
    assert response.status_code == 202
    assert response.json()["turnId"] == "turn-3"
    assert client.get("/status").json()["gameId"] == "g-2"


def test_the_agent_is_stopped_only_once_per_match(world):
    client = world.client
    state = world.state
    play_out_the_match(client, state)
    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete"})
    assert state["deleted"] == 1


def test_a_match_is_retired_even_when_the_agent_is_already_gone(world):
    client = world.client
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete"})
    state["agent"] = "down"
    body = client.post("/turn-done", json={"turnId": "turn-2", "state": "complete"}).json()
    assert body["retired"] is True
    assert body["agentStopped"] is False
    assert client.get("/status").json()["gameId"] is None


def test_a_game_that_ends_with_the_agent_already_dead_is_still_retired(world):
    client = world.client
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    state["agent"] = "down"
    body = client.post("/turn-done", json={"turnId": "turn-1", "state": "failed"}).json()
    assert body["retired"] is True
    assert body["reflected"] is False
    assert client.get("/status").json()["gameId"] is None


def test_a_dispatch_while_the_agent_is_booting_is_accepted_and_retried(world):
    client = world.client
    submitted = world.submitted
    state = world.state
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
    client = world.client
    state = world.state
    state["agent"] = "down"
    assert dispatch(client).json()["pending"] is True

    def failed_status():
        status = client.get("/status").json()
        return status if status["lastError"] else None

    status = wait_for(failed_status)
    assert status["gameId"] is None, "a game nobody can play must not wedge the driver"
    assert "unreachable" in status["lastError"]


def test_the_retry_stops_if_the_match_is_retired_under_it(world):
    client = world.client
    submitted = world.submitted
    state = world.state
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


# -- the contract this file's fake game server stands in for ------------------


def test_the_real_game_server_carries_the_field_seat_decklist_reads():
    """The fake `get_seats` in `world.py` returns `seats[].decklist`, and for
    a long time the real lobby route this task added did not agree with it -
    catching that disagreement is the whole point of this test, the same way
    it caught the original bug this docstring used to describe: the real
    API's game-state player block never carried `decklist` at all (2b's T8
    moved it off that view entirely, S6), so a double that kept the field
    only on the state view would silently disagree with reality and every
    driver test above would still pass for the wrong reason.

    This test is the join: it boots a real game through the real app, reads
    its seats exactly as the driver does (`GET
    /api/lobby/games/{id}/seats?uid=...` with the AI seat's own uid), and
    asserts the response really carries the field `seat_decklist` looks for -
    and does not carry the opponent's.
    """
    from magic_cards_model_api.main import app as game_app

    with TestClient(game_app) as game_client:
        booted = game_client.post(
            "/api/lobby/games",
            json={
                "players": [
                    {"username": "alice", "decklist_text": "56 Mountain\n4 Lightning Bolt"},
                    {"username": "bob", "decklist_text": "56 Forest\n4 Grizzly Bears"},
                ],
                "seed": 3,
            },
        ).json()
        try:
            # The state view itself carries no decklist for anyone any more -
            # the regression this task's own removal must not reintroduce a
            # different way (leaving it there after all).
            state = game_client.get(
                f"/api/games/{booted['game_id']}", params={"uid": booted["seat_uids"]["bob"]}
            ).json()
            assert all("decklist" not in player for player in state["players"])

            seats = game_client.get(
                f"/api/lobby/games/{booted['game_id']}/seats",
                params={"uid": booted["seat_uids"]["bob"]},
            ).json()["seats"]
        finally:
            game_client.delete(
                f"/api/games/{booted['game_id']}", params={"key": booted["admin_key"]}
            )

    from mtg_agent.driver.guides import parse_decklist

    by_player = {seat["player"]: seat for seat in seats}
    assert by_player["bob"].get("decklist") == "56 Forest\n4 Grizzly Bears"
    assert not by_player["alice"].get("decklist"), (
        "bob's own uid must never unlock alice's decklist too"
    )
    # And it parses to a real list, which is what `GuideLibrary.identify`
    # scores: an unparseable or empty answer is the "0 cards" symptom.
    assert parse_decklist(by_player["bob"]["decklist"]) == {"Forest": 56, "Grizzly Bears": 4}


# ---- a failed turn has to be visible to whoever asks the driver ------------
#
# The live freeze this covers: the agent stopped acting mid-game, and the
# game server - whose only window onto this seat is `GET /status` - kept
# telling the play UI the seat was fine, because `lastError` was only ever
# set on a dispatch that could not reach the agent, never on a turn that came
# back failed. The human sat in front of an opponent that would never move,
# with nothing on the page to say why.


def test_a_failed_turn_is_reported_by_status(world):
    client = world.client
    dispatch(client)
    client.post(
        "/turn-done",
        json={
            "turnId": "turn-1",
            "state": "failed",
            "error": "claude reported a fatal error: API Error: 401",
        },
    )
    status = client.get("/status").json()
    assert status["lastError"] is not None
    assert "401" in status["lastError"]
    assert "turn-1" in status["lastError"]


def test_a_turn_that_succeeds_clears_a_previous_failure(world):
    client = world.client
    dispatch(client)
    client.post("/turn-done", json={"turnId": "turn-1", "state": "failed", "error": "boom"})
    assert client.get("/status").json()["lastError"] is not None
    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "ok"})
    assert client.get("/status").json()["lastError"] is None


# ---- "is the agent mid-turn" - the signal the game server settles seats on --
#
# The game server's pregame backstop takes a seat's CR 103.5 declaration for
# it when nothing is playing that seat. It used to do that on a timer alone,
# and once the agent's prompts made it think for longer than the timer, the
# backstop began declaring over a live agent - see `ai_pregame.py`. What makes
# a liveness answer possible is this flag: one wrapper turn covers a whole
# match, so a turn that has *finished* while the game is still running is an
# agent that stopped playing, however healthy the container looks.


def test_a_dispatched_turn_is_reported_as_in_flight(world):
    client = world.client
    assert client.get("/status").json()["turnActive"] is False
    dispatch(client)
    assert client.get("/status").json()["turnActive"] is True


def test_a_finished_turn_is_no_longer_in_flight(world):
    """The exact shape of the reported hang: the agent's turn ended, the game
    is still running, and nothing is playing the seat."""
    client = world.client
    dispatch(client)
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "ok"})
    assert client.get("/status").json()["turnActive"] is False


def test_a_pushed_reflection_turn_is_in_flight_again(world):
    client = world.client
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    assert client.get("/status").json()["turnActive"] is True, (
        "the agent is writing its notes - that is a live turn"
    )


def test_a_dispatch_that_is_still_reaching_the_agent_has_no_live_turn(world):
    client = world.client
    state = world.state
    state["agent"] = "booting"
    dispatch(client)
    body = client.get("/status").json()
    assert body["pending"] is True
    assert body["turnActive"] is False, "a booting agent is not holding a turn"


# ---- assign pushes DispatchIn.agent to the wrapper's /v1/session ahead of
# turn 0, when it differs from what the wrapper already reports -------------


def test_dispatch_pushes_differing_agent_settings_to_the_wrapper_before_turn_0(world):
    client = world.client
    dispatch(client, agent={"model": "opus", "effort": "max", "maxThinkingTokens": 4096})
    assert world.session_posts == [{"model": "opus", "effort": "max", "maxThinkingTokens": 4096}]
    assert world.session_settings == {
        "model": "opus",
        "effort": "max",
        "maxThinkingTokens": 4096,
    }
    # And turn 0 was still pushed after the settings push.
    assert world.submitted


def test_dispatch_does_not_push_agent_settings_that_already_match(world):
    client = world.client
    world.session_settings.update(model="opus", effort="max", maxThinkingTokens=None)
    dispatch(client, agent={"model": "opus", "effort": "max", "maxThinkingTokens": None})
    assert world.session_posts == []


def test_dispatch_without_agent_settings_never_touches_the_wrapper_session(world):
    client = world.client
    dispatch(client, agent=None)
    assert world.session_posts == []


def test_dispatch_treats_an_older_wrapper_that_404s_session_as_unsupported(world):
    client = world.client
    world.state["session_supported"] = False
    response = dispatch(client, agent={"model": "opus", "effort": "max"})
    # Not supported, continue: turn 0 still goes out and the dispatch itself
    # answers normally rather than failing over a wrapper that predates the
    # per-game settings route.
    assert response.status_code == 202
    assert world.session_posts == []
    assert world.submitted
