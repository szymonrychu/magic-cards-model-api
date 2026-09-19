import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mtg_agent.driver.config import DriverSettings
from mtg_agent.driver.prompt import HANDOVER_OPENING
from tests.driver.world import DECKLIST, build_world


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


def complete_handover(client, submitted: list[dict]) -> str:
    """Wait for the handover turn to be pushed and finish it; its turn id."""
    ids = wait_for(
        lambda: [
            f"turn-{index + 1}"
            for index, turn in enumerate(submitted)
            if turn["text"].startswith(HANDOVER_OPENING)
        ]
    )
    client.post("/turn-done", json={"turnId": ids[0], "state": "complete", "finalText": "pushed"})
    return ids[0]


def play_out_the_match(client, state, submitted: list[dict]) -> None:
    """Dispatch, win, let the reflection and the handover turns complete, and
    wait for the retirement the handover ends with."""
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"})
    complete_handover(client, submitted)
    wait_for(lambda: client.get("/status").json()["gameId"] is None)


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


@pytest.mark.parametrize(
    "archetype",
    [
        # Every shape that was reproduced raising out of the guide readers.
        # The corpus is a SEPARATE repo (github.com/szymonrychu/mtg-decks),
        # cloned read-only by an initContainer at a ref that is not pinned by
        # default, so any of these can appear under a running driver between
        # two games without anything in this repo changing.
        '{"archetype": "zz", "signature": [{"cards": 5, "min": 1}]}',
        '{"archetype": "zz", "signature": 5}',
        '{"archetype": "zz", "signature": [{"cards": {"a": 1}, "min": 1}]}',
        '{"archetype": "zz", "signature": [{"cards": ["Psychic Frog"], "min": 4}],'
        ' "exclude": [{"cards": 5, "min": 1}]}',
        "{ half written",
    ],
)
def test_one_malformed_archetype_in_the_corpus_does_not_five_hundred_the_dispatch(world, archetype):
    """The blast radius, at the level where it cost a game.

    `/dispatch` calls `library.identify(decklist)` unguarded and `identify`
    asks EVERY archetype in the corpus - so a malformed file belonging to a
    deck neither seat is playing used to raise out of the loop and 500 the
    dispatch for BOTH seats and EVERY deck. Turn 0 never rendered and neither
    agent ever played.

    The seat is still identified: the broken deck is the only one that loses
    anything.
    """
    broken = world.settings.guides_root / "decks" / "zz-broken"
    broken.mkdir(parents=True)
    (broken / "decklist.txt").write_text("4 Psychic Frog\n")
    (broken / "archetype.json").write_text(archetype)

    response = dispatch(world.client)
    assert response.status_code == 202, response.text
    assert response.json()["deck"] == "dimir-frog"
    assert world.submitted[0]["text"]


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
    assert complete_handover(client, submitted) == "turn-3"
    reflections = [turn for turn in submitted if "The game is over" in turn["text"]]
    assert len(reflections) == 1, "the reflection turn must not trigger another reflection"


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
    play_out_the_match(client, state, world.submitted)
    assert state["deleted"] == 1


def test_the_completed_reflection_turn_posts_it_to_the_game_server(world):
    """`prompt.reflection_turn` only ever told the agent to write markdown
    into its own memory directory - this is what makes the write-up reach
    anywhere the game server (and so the post-game review) can see it:
    `finalText`, the agent's own account of the reflection turn, posted to
    `POST /api/games/{id}/reflection` before the agent is retired."""
    client = world.client
    state = world.state
    play_out_the_match(client, state, world.submitted)
    assert world.reflection_posts == [
        {
            "game_id": "g-1",
            "player": "bob",
            "uid": "secret-uid",
            "text": "wrote",
        }
    ]


def test_the_completed_reflection_turn_also_generates_and_posts_an_ai_review(world):
    """The other half of the handoff: once the reflection is posted, the
    driver asks the fake wrapper's `/v1/review` (Opus, `AgentSettings.
    review_default` - never the seat's own play settings) to critique the
    game, using the just-posted reflection plus the game's own review
    record, and files the answer under `POST .../ai-review`.

    The review itself runs in a background thread (started after
    `start_handover`, never before), so `play_out_the_match`'s own wait for
    retirement can win the race against it landing - `wait_for` below is
    what makes this deterministic rather than occasionally flaky."""
    client = world.client
    state = world.state
    play_out_the_match(client, state, world.submitted)

    wait_for(lambda: len(world.ai_review_posts) == 1)
    assert len(world.review_requests) == 1
    review_request = world.review_requests[0]
    assert review_request["model"] == "claude-opus-5"
    assert review_request["effort"] == "high"
    # The prompt is built from the reflection text this same call sequence
    # just posted - proof the review runs AFTER the reflection, not before.
    assert "wrote" in review_request["prompt"]
    assert "bob" in review_request["prompt"]

    assert world.ai_review_posts == [
        {
            "game_id": "g-1",
            "player": "bob",
            "uid": "secret-uid",
            "text": "a solid game overall, but the turn 6 attack was a misread",
        }
    ]


def test_a_slow_review_never_delays_the_handover_response(world):
    """The bug: `post_ai_review` used to run inline, in `turn_done`, before
    the handover was even started - a slow (or merely not-instant) review
    generation delayed the handover response by exactly as long as the
    review took, up to `REVIEW_HTTP_TIMEOUT` (650s). It must now run in the
    background, started only after `start_handover`, so a slow review never
    delays retirement at all."""
    client = world.client
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    state["wrapper_review_delay"] = 2.0
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})

    started = time.monotonic()
    response = client.post(
        "/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"}
    )
    elapsed = time.monotonic() - started

    assert response.status_code == 202
    assert response.json()["handover"] is not None
    assert elapsed < 1.0, f"turn-done waited {elapsed:.2f}s on a 2s-slow review"

    wait_for(lambda: len(world.ai_review_posts) == 1, timeout=5.0)


def test_a_retried_reflection_turn_done_does_not_duplicate_work(world):
    """`webhook.Deliverer` retries a `/turn-done` delivery (its own 10s
    client timeout, up to 3 attempts) whenever the driver does not answer
    within it - which inline review generation used to blow through easily.
    Simulated here by simply POSTing the SAME reflection turn-done twice,
    the way a slow first response and a retried second delivery look from
    the driver's own side: both must succeed, but the reflection and the
    review must each be filed exactly once."""
    client = world.client
    submitted = world.submitted
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})

    first = client.post(
        "/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"}
    )
    assert first.status_code == 202
    assert first.json()["handover"] is not None

    second = client.post(
        "/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"}
    )
    assert second.status_code == 202
    assert second.json()["handover"] is not None

    assert world.reflection_posts == [
        {"game_id": "g-1", "player": "bob", "uid": "secret-uid", "text": "wrote"}
    ]

    complete_handover(client, submitted)
    wait_for(lambda: client.get("/status").json()["gameId"] is None)
    wait_for(lambda: len(world.ai_review_posts) == 1)
    assert len(world.review_requests) == 1


def test_two_concurrent_deliveries_of_the_same_reflection_file_one_reflection(world):
    """The actual race `webhook.Deliverer` can produce: its own client-side
    timeout can fire while the driver is still mid-response, so a retried
    delivery can be genuinely concurrent with the first one still being
    processed, not merely sequential. Three real threads hitting the same
    endpoint at once is what actually exercises the dedup lock, rather than
    relying on the first call happening to finish before the second one
    starts."""
    client = world.client
    submitted = world.submitted
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})

    results: list = []
    lock = threading.Lock()

    def fire() -> None:
        response = client.post(
            "/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"}
        )
        with lock:
            results.append(response)

    threads = [threading.Thread(target=fire) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status_code == 202 for r in results), [r.text for r in results]
    assert world.reflection_posts == [
        {"game_id": "g-1", "player": "bob", "uid": "secret-uid", "text": "wrote"}
    ]

    complete_handover(client, submitted)
    wait_for(lambda: client.get("/status").json()["gameId"] is None)
    wait_for(lambda: len(world.ai_review_posts) == 1)
    assert len(world.review_requests) == 1


def test_an_unreachable_wrapper_review_does_not_block_retirement(world):
    """Best-effort, exactly like `post_reflection` and `sync_agent_settings`
    beside it: a wrapper `/v1/review` this driver cannot reach must not wedge
    the agent mid-retirement, and must not stop the reflection itself from
    having already been filed."""
    client = world.client
    state = world.state
    state["wrapper_review"] = "down"
    play_out_the_match(client, state, world.submitted)
    assert world.reflection_posts != []
    assert world.ai_review_posts == []


def test_a_claude_failure_generating_the_review_does_not_block_retirement(world):
    client = world.client
    state = world.state
    state["wrapper_review"] = "error"
    play_out_the_match(client, state, world.submitted)
    assert world.reflection_posts != []
    assert world.ai_review_posts == []


def test_an_unreachable_game_server_for_the_review_record_does_not_block_retirement(world):
    """`get_review` (fetching the game record the prompt is built from) is
    the first of the AI review's three calls - an unreachable game server
    there must fail the review generation the same way a later failure
    would, without ever reaching the wrapper."""
    client = world.client
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    real_get_review = state["game"]

    # Flip the game server down only once the reflection has landed (so the
    # reflection post itself still succeeds), by way of a second `state`
    # flag `post_ai_review` cannot tell apart from the review-record read -
    # both go through the same `game` app, so simply going down here covers
    # the review's own GET too.
    state["game"] = "down"
    body = client.post(
        "/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"}
    ).json()
    assert body["handover"]["outcome"] == "bob won"
    complete_handover(client, world.submitted)
    wait_for(lambda: client.get("/status").json()["gameId"] is None)
    assert world.review_requests == []
    assert world.ai_review_posts == []
    assert real_get_review == "up"


def test_a_review_the_wrapper_answers_with_no_text_is_not_filed(world):
    client = world.client
    state = world.state
    state["review_text"] = "   "
    play_out_the_match(client, state, world.submitted)
    assert world.ai_review_posts == []


def test_a_malformed_review_record_does_not_block_retirement(world):
    """`review_prompt` (`review_prompt(assignment, reflection_text,
    review_record)`) is a plain function call between the fetch and the
    wrapper POST, outside either of their own try/excepts - a game server
    answering something `GET .../review` was never meant to (`null`, here)
    must not propagate an uncaught exception out of the background review
    thread, and must not stop retirement, exactly like every other way this
    best-effort chain can fail."""
    client = world.client
    state = world.state
    state["review_record"] = None
    play_out_the_match(client, state, world.submitted)
    assert world.reflection_posts != []
    assert world.review_requests == []
    assert world.ai_review_posts == []


def test_a_reflection_turn_that_said_nothing_still_posts_a_placeholder(world):
    client = world.client
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "   "})
    assert world.reflection_posts[0]["text"]
    assert "without saying anything" in world.reflection_posts[0]["text"]


def test_an_unreachable_game_server_does_not_block_retirement(world):
    """`post_reflection` is best-effort, exactly like `sync_agent_settings`:
    a game server this driver cannot reach when the reflection turn
    completes must not wedge the agent mid-retirement."""
    client = world.client
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    state["game"] = "down"
    body = client.post(
        "/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"}
    ).json()
    assert body["handover"]["outcome"] == "bob won"
    complete_handover(client, world.submitted)
    wait_for(lambda: client.get("/status").json()["gameId"] is None)
    assert world.reflection_posts == []


def test_retiring_clears_the_assignment_so_the_next_dispatch_starts_clean(world):
    client = world.client
    state = world.state
    play_out_the_match(client, state, world.submitted)
    assert client.get("/status").json()["gameId"] is None


def test_the_next_match_is_accepted_once_the_replacement_agent_is_up(world):
    client = world.client
    state = world.state
    play_out_the_match(client, state, world.submitted)
    state["agent"] = "up"  # the replacement container finished booting
    state["winner"] = None
    response = dispatch(client, uid="uid-2", game_id="g-2")
    assert response.status_code == 202
    assert response.json()["turnId"] == "turn-4"
    assert client.get("/status").json()["gameId"] == "g-2"


def test_the_agent_is_stopped_only_once_per_match(world):
    client = world.client
    state = world.state
    play_out_the_match(client, state, world.submitted)
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
    assert body["handover"]["gameId"] == "g-1"
    wait_for(lambda: client.get("/status").json()["gameId"] is None)
    assert state["deleted"] == 0, "an agent that is gone could not be stopped"


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
    # classifies: an unparseable or empty answer is the "0 cards" symptom.
    parsed = parse_decklist(by_player["bob"]["decklist"])
    assert parsed.mainboard == {"Forest": 56, "Grizzly Bears": 4}
    assert parsed.sideboard == {}


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


# ---- the deck's play scripts are registered once, at dispatch ---------------
#
# `assign` loads `decks/<slug>/plays/` (and `inputs.json`) off the identified
# guide and registers them with `PUT /api/games/{id}/seats/{uid}/scripts`
# under the uid the dispatch runs with - a seat's or a buddy's - before turn 0
# is written, so the briefing can say what the game server holds.

FIXTURE_PLAYS = Path(__file__).resolve().parent / "fixtures" / "mtg-decks" / "dimir-frog" / "plays"

DECLARED_INPUTS = {
    "schema_version": 1,
    "inputs": [
        {
            "name": "plan",
            "type": "enum",
            "values": ["race", "control"],
            "description": "Which half of the plan this game is on.",
            "required": True,
        }
    ],
}


def fixture_plays() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(FIXTURE_PLAYS.glob("*.json"))]


def dispatch_as(client, role: str, uid: str) -> httpx.Response:
    return client.post(
        "/dispatch",
        json={
            "uid": uid,
            "game_id": "g-1",
            "player": "bob",
            "api_base": "http://game:8000",
            "role": role,
        },
    )


def world_plays_dir(world) -> Path:
    return world.settings.guides_root / "decks" / "dimir-frog" / "plays"


@pytest.mark.parametrize(("role", "uid"), [("seat", "secret-uid"), ("advisor", "advisor-uid")])
def test_dispatch_registers_the_decks_plays_under_its_own_uid(world, role, uid):
    from mtg_agent.driver.revision import guides_revision

    response = dispatch_as(world.client, role, uid)

    assert response.status_code == 202, response.text
    assert len(world.script_puts) == 1
    put = world.script_puts[0]
    assert (put["game_id"], put["uid"], put["caller"]) == ("g-1", uid, uid)
    assert put["body"]["deck_slug"] == "dimir-frog"
    assert put["body"]["guides_revision"] == guides_revision(world.settings.guides_root)
    assert sorted(s["id"] for s in put["body"]["scripts"]) == sorted(
        s["id"] for s in fixture_plays()
    )
    assert put["body"]["inputs"] == [], "no inputs.json is an empty declared-inputs list"


@pytest.mark.parametrize(("role", "uid"), [("seat", "secret-uid"), ("advisor", "advisor-uid")])
def test_inputs_json_is_registered_beside_the_plays(world, role, uid):
    (world_plays_dir(world) / "inputs.json").write_text(json.dumps(DECLARED_INPUTS))

    dispatch_as(world.client, role, uid)

    body = world.script_puts[0]["body"]
    assert body["inputs"] == DECLARED_INPUTS["inputs"]
    assert len(body["scripts"]) == len(fixture_plays())


def test_the_corpus_is_registered_once_and_never_mid_game(world):
    client = world.client
    dispatch(client)
    dispatch(client)
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "ok"})
    world.state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"})

    assert len(world.script_puts) == 1


def test_an_unguided_deck_registers_nothing(world):
    world.state["decklist"] = "4 Lightning Bolt\n20 Mountain\n"
    dispatch(world.client)
    assert world.script_puts == []
    text = (world.settings.workspace / "current-game.md").read_text()
    assert "No play scripts are registered" in text


def test_a_scripts_disabled_refusal_does_not_fail_the_dispatch(world):
    world.state["scripts_enabled"] = False

    response = dispatch(world.client)

    assert response.status_code == 202, response.text
    assert response.json()["turnId"] == "turn-1"
    assert len(world.script_puts) == 1
    text = (world.settings.workspace / "current-game.md").read_text()
    assert "Play scripts are disabled" in text
    for script in fixture_plays():
        assert f"`{script['id']}`" not in text
    assert "Play scripts are disabled" in world.submitted[0]["text"]


def test_an_unreachable_scripts_route_does_not_fail_the_dispatch(world):
    """Best effort like the settings push: a refusal the driver cannot act on
    costs the seat its scripts, said in the briefing, never the game."""
    world.state["scripts_route"] = "down"

    response = dispatch(world.client)

    assert response.status_code == 202, response.text
    assert response.json()["turnId"] == "turn-1"
    text = (world.settings.workspace / "current-game.md").read_text()
    assert "could not be registered" in text


def test_the_briefing_lists_the_registered_scripts(world):
    dispatch(world.client)

    text = (world.settings.workspace / "current-game.md").read_text()
    assert "## Your registered play scripts" in text
    for script in fixture_plays():
        assert f"`{script['id']}`" in text
        assert script["name"] in text
        assert f"priority {script['priority']}" in text
        assert ", ".join(script["opposing_archetypes"]) in text
    assert "## Your registered play scripts" in world.submitted[0]["text"]


def test_an_invalid_play_file_is_skipped_logged_and_named_in_the_briefing(world, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="driver")
    (world_plays_dir(world) / "broken.json").write_text('{"schema_version": 1, "iterate": true}')

    dispatch(world.client)

    body = world.script_puts[0]["body"]
    assert sorted(s["id"] for s in body["scripts"]) == sorted(s["id"] for s in fixture_plays())
    text = (world.settings.workspace / "current-game.md").read_text()
    assert "decks/dimir-frog/plays/broken.json" in text
    assert "unknown-field" in text
    assert any("decks/dimir-frog/plays/broken.json" in r.getMessage() for r in caplog.records)


def test_an_invalid_inputs_json_is_named_and_does_not_block_the_plays(world, caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="driver")
    (world_plays_dir(world) / "inputs.json").write_text('{"schema_version": 1, "inputs": 5}')

    response = dispatch(world.client)

    assert response.status_code == 202
    body = world.script_puts[0]["body"]
    assert body["inputs"] == []
    assert len(body["scripts"]) == len(fixture_plays())
    text = (world.settings.workspace / "current-game.md").read_text()
    assert "decks/dimir-frog/plays/inputs.json" in text
    assert "malformed-inputs" in text
    assert any("malformed-inputs" in r.getMessage() for r in caplog.records)


def test_the_scripts_listing_is_bounded(tmp_path):
    """A deck can carry any number of plays and each can be long-named. The
    briefing's listing has its own byte cap, so the briefing stays within
    the caps it already had however large the corpus grows."""
    from magic_cards_model_api.playscripts.schema import PlaysError
    from mtg_agent.driver.guides import (
        CONFIDENT,
        GuideLibrary,
        PlaysCorpus,
        ScriptsRegistration,
        parse_decklist,
    )
    from mtg_agent.driver.prompt import PLAYS_SECTION_CAP_BYTES, Assignment, render_current_game

    world = build_world(tmp_path)
    decklist = parse_decklist(DECKLIST)
    identified = GuideLibrary(world.settings.guides_root).identify(decklist)
    assert identified.confidence == CONFIDENT
    base = fixture_plays()[0]
    scripts = tuple(
        {
            **base,
            "id": f"play-{index:03d}",
            "name": "a very long play name " * 20,
            "opposing_archetypes": [f"archetype-{n}" for n in range(32)],
        }
        for index in range(200)
    )
    notices = tuple(
        PlaysError("unknown-field", f"decks/dimir-frog/plays/bad-{i}.json", "x" * 500)
        for i in range(50)
    )

    def briefing(registration):
        return render_current_game(
            Assignment(
                game_id="g",
                uid="u",
                player="bob",
                opponent="alice",
                api_base="http://game",
                decklist=decklist,
                identification=identified,
                scripts=registration,
            ),
            tmp_path / "memory",
        )

    listed = briefing(ScriptsRegistration("registered", PlaysCorpus(scripts, (), notices)))
    unlisted = briefing(ScriptsRegistration("registered", PlaysCorpus((), (), ())))
    grown = len(listed.encode()) - len(unlisted.encode())
    assert 0 < grown <= PLAYS_SECTION_CAP_BYTES
    assert PLAYS_SECTION_CAP_BYTES <= 4 * 1024
    assert "`play-000`" in listed
    assert "not listed" in listed


def test_the_real_game_server_accepts_the_corpus_the_driver_registers(tmp_path):
    """The join for the fake PUT in `world.py`: the body `assign` builds from
    the refreshed fixture corpus, inputs.json included, is one the real route
    admits and reads back whole."""
    from magic_cards_model_api.main import app as game_app
    from mtg_agent.driver.guides import GuideLibrary

    world = build_world(tmp_path)
    (world_plays_dir(world) / "inputs.json").write_text(json.dumps(DECLARED_INPUTS))
    corpus = GuideLibrary(world.settings.guides_root).get("dimir-frog").plays()
    assert corpus.notices == ()

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
        uid = booted["seat_uids"]["bob"]
        path = f"/api/games/{booted['game_id']}/seats/{uid}/scripts"
        try:
            put = game_client.put(
                path,
                params={"uid": uid},
                json={
                    "deck_slug": "dimir-frog",
                    "guides_revision": "unknown",
                    "scripts": list(corpus.scripts),
                    "inputs": list(corpus.inputs),
                },
            )
            read = game_client.get(path, params={"uid": uid}).json()
        finally:
            game_client.delete(
                f"/api/games/{booted['game_id']}", params={"key": booted["admin_key"]}
            )

    assert put.status_code == 200, put.text
    assert put.json()["count"] == len(fixture_plays())
    assert read["inputs"] == DECLARED_INPUTS["inputs"]
