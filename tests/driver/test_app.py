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


def test_dispatch_includes_support_warnings_in_the_briefing(world):
    """Flow 6 (W2): `seat_support_warnings` reads `GET .../seats`' own
    `support_warnings` and `dispatch` carries it into the `Assignment` that
    `render_current_game` renders - the driver never computes this itself
    (it has no engine dependency), it only relays what the game server, the
    real source of truth, already answered."""
    world.state["support_warnings"] = [
        {
            "card": "Inscrutable Husk",
            "status": "silent",
            "clauses": [
                {
                    "text": "whenever this creature glimmers, frobnicate target zorblax",
                    "outcome": "silent",
                    "reason": "nothing in the engine reads this text",
                }
            ],
        }
    ]
    client = world.client
    settings = world.settings
    dispatch(client)
    text = (settings.workspace / "current-game.md").read_text()
    assert "cards the engine may not fully support" in text.lower()
    assert "Inscrutable Husk (silent)" in text
    assert "glimmers" in text


def test_dispatch_says_nothing_about_support_with_no_warnings(world):
    client = world.client
    settings = world.settings
    dispatch(client)
    text = (settings.workspace / "current-game.md").read_text()
    assert "may not fully support" not in text.lower()


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


def test_turn_done_mid_game_pushes_only_a_continuation(world):
    """A mid-game turn end is never left idle (`test_continue_turn.py`), and
    it is never mistaken for the end of the game either: no reflection."""
    client = world.client
    submitted = world.submitted
    dispatch(client)
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "ok"})
    assert len(submitted) == 2
    assert submitted[1]["text"].startswith("The game is not over")


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

    # DR3 (3): the filed text is the reviewer's own words plus the appended
    # "Play statistics" block now, so this checks the fixed fields and the
    # text's own prefix/block rather than exact equality.
    assert len(world.ai_review_posts) == 1
    posted = world.ai_review_posts[0]
    assert posted["game_id"] == "g-1"
    assert posted["player"] == "bob"
    assert posted["uid"] == "secret-uid"
    assert posted["text"].startswith("a solid game overall, but the turn 6 attack was a misread")
    assert "Play statistics:" in posted["text"]
    for line in ("plays in deck", "added", "updated", "used", "denied", "passed over"):
        assert f"- {line}: " in posted["text"], posted["text"]


def test_the_ai_review_statistics_fold_in_this_reviews_own_journal_admits(world):
    """DR3 (3): `post_ai_review` reads this game's `role="reviewer"` results
    journal AFTER the review one-shot returns, and the admits it finds there
    - this review's own `mcp-seat plays admit` calls, never anything the
    handover admitted - are what the appended block's `added`/`updated`
    counts come from."""
    guides_root = world.settings.guides_root
    git_dir = guides_root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    line = {
        "game_id": "g-1",
        "role": "reviewer",
        "command": "plays admit",
        "result": "admitted",
        "dsl_change": True,
        "added": ["p1", "p2"],
        "updated": ["p3"],
        "reprioritized": [],
    }
    (git_dir / "mcm-results.jsonl").write_text(json.dumps(line) + "\n")

    client = world.client
    state = world.state
    play_out_the_match(client, state, world.submitted)
    wait_for(lambda: len(world.ai_review_posts) == 1)

    text = world.ai_review_posts[0]["text"]
    assert "- added: 2" in text
    assert "- updated: 1" in text


def test_a_slow_review_never_delays_the_handover_response(world):
    """The bug: `post_ai_review` used to run inline, in `turn_done`, before
    the handover was even started - a slow (or merely not-instant) review
    generation delayed the handover response by exactly as long as the
    review took, up to `REVIEW_HTTP_TIMEOUT`. It must now run in the
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


def test_two_sequential_games_through_one_driver_each_get_their_own_review(world):
    """`reflectionHandled` (and the rest of `clear_locked`'s reset) must not
    leak from one match into the next. A compose-style driver (not
    `exit_after_match`) is not thrown away between matches - `retire`
    clears the assignment and the next dispatch, once the replacement agent
    container is up, starts clean - so the SAME driver process runs the
    whole reflect-review-handover flow twice here, and the second game's
    own reflection and review must land under its own game id and uid,
    exactly as the first game's did."""
    client = world.client
    state = world.state
    submitted = world.submitted

    play_out_the_match(client, state, submitted)
    wait_for(lambda: len(world.ai_review_posts) == 1)
    # DR3 (3): the appended "Play statistics" block, same as above.
    first = world.ai_review_posts[0]
    assert first["game_id"] == "g-1"
    assert first["player"] == "bob"
    assert first["uid"] == "secret-uid"
    assert first["text"].startswith("a solid game overall, but the turn 6 attack was a misread")
    assert "Play statistics:" in first["text"]

    state["agent"] = "up"  # the replacement container finished booting
    state["winner"] = None
    state["review_text"] = "the second game's own critique"
    dispatched = dispatch(client, uid="uid-2", game_id="g-2")
    assert dispatched.status_code == 202
    turn_zero_id = dispatched.json()["turnId"]

    # `complete_handover`/naive polling for "any handover-opening turn"
    # would find game ONE's own (already completed) handover turn - still
    # sitting in `submitted` - on the very first check, before game two's
    # own has even been pushed. Recording the count first and waiting for
    # it to grow is what actually waits for THIS game's handover turn.
    handover_turns_before = sum(
        1 for turn in submitted if turn["text"].startswith(HANDOVER_OPENING)
    )

    state["winner"] = "bob"
    client.post(
        "/turn-done", json={"turnId": turn_zero_id, "state": "complete", "finalText": "gg2"}
    )
    assert "The game is over" in submitted[-1]["text"]
    reflection_id = f"turn-{len(submitted)}"
    client.post(
        "/turn-done", json={"turnId": reflection_id, "state": "complete", "finalText": "wrote2"}
    )

    def newest_handover_turn_ids() -> list[str] | None:
        matches = [
            f"turn-{index + 1}"
            for index, turn in enumerate(submitted)
            if turn["text"].startswith(HANDOVER_OPENING)
        ]
        return matches if len(matches) > handover_turns_before else None

    handover_ids = wait_for(newest_handover_turn_ids)
    client.post(
        "/turn-done",
        json={"turnId": handover_ids[-1], "state": "complete", "finalText": "pushed"},
    )
    wait_for(lambda: client.get("/status").json()["gameId"] is None)

    wait_for(lambda: len(world.ai_review_posts) == 2)
    assert world.reflection_posts[-1] == {
        "game_id": "g-2",
        "player": "bob",
        "uid": "uid-2",
        "text": "wrote2",
    }
    # DR3 (3): the appended "Play statistics" block, same as above.
    second = world.ai_review_posts[-1]
    assert second["game_id"] == "g-2"
    assert second["player"] == "bob"
    assert second["uid"] == "uid-2"
    assert second["text"].startswith("the second game's own critique")
    assert "Play statistics:" in second["text"]
    # Neither game's own reflection or review was duplicated by the other's.
    assert len(world.reflection_posts) == 2
    assert len(world.ai_review_posts) == 2


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
    client.post(
        "/turn-done",
        json={"turnId": "turn-1", "state": "failed", "error": "claude exited with code 1"},
    )
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
    is still running, and nothing is playing the seat - here because the
    agent is gone, so no continuation could be pushed."""
    client = world.client
    dispatch(client)
    client.post(
        "/turn-done",
        json={"turnId": "turn-1", "state": "failed", "error": "claude exited with code 1"},
    )
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
    from mtg_agent.driver.prompt import _clip

    dispatch(world.client)

    text = (world.settings.workspace / "current-game.md").read_text()
    assert "## Your registered play scripts" in text
    for script in fixture_plays():
        assert f"`{script['id']}`" in text
        # `_clip` (`prompt.py`'s own row renderer, `_script_row`) trims a
        # name past its own character budget - the corpus vendors real,
        # sometimes long, play names, so the row is read the same way.
        assert _clip(script["name"]) in text
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


# -- T35: an AI review that could not be produced says so, durably -------------
#
# Every failure inside `post_ai_review` used to be swallowed to
# `log.warning`, and "the reviewer failed" was indistinguishable from "the
# reviewer was never invoked" without pod-log archaeology. Each of the four
# failure points now files an `ai_review_failed` row through
# `POST /api/games/{id}/ai-review-failure`, carrying the stage, the reason
# and whether the reviewer was ever invoked at all.


def test_a_failed_review_record_read_files_a_durable_signal(world):
    client = world.client
    state = world.state
    state["review_record"] = "error"
    play_out_the_match(client, state, world.submitted)
    wait_for(lambda: len(world.ai_review_failures) == 1)
    signal = world.ai_review_failures[0]
    assert signal["game_id"] == "g-1"
    assert signal["player"] == "bob"
    assert signal["uid"] == "secret-uid"
    assert signal["stage"] == "review_record"
    assert signal["invoked"] is False
    assert "500" in signal["reason"]
    assert world.ai_review_posts == []


def test_a_malformed_review_record_files_a_durable_signal(world):
    client = world.client
    state = world.state
    state["review_record"] = None
    play_out_the_match(client, state, world.submitted)
    wait_for(lambda: len(world.ai_review_failures) == 1)
    signal = world.ai_review_failures[0]
    assert signal["stage"] == "prompt"
    assert signal["invoked"] is False
    assert signal["reason"]
    assert world.review_requests == []
    assert world.ai_review_posts == []


def test_a_wrapper_that_cannot_generate_the_review_files_a_durable_signal(world):
    client = world.client
    state = world.state
    state["wrapper_review"] = "error"
    play_out_the_match(client, state, world.submitted)
    wait_for(lambda: len(world.ai_review_failures) == 1)
    signal = world.ai_review_failures[0]
    assert signal["stage"] == "generate"
    assert signal["invoked"] is True
    assert "502" in signal["reason"]
    assert world.ai_review_posts == []


def test_an_unreachable_wrapper_files_a_signal_that_says_it_was_never_invoked(world):
    """The exact ambiguity this task exists to close: a wrapper the driver
    could not reach never ran the reviewer, and that is a different fact
    from a reviewer that ran and failed."""
    client = world.client
    state = world.state
    state["wrapper_review"] = "down"
    play_out_the_match(client, state, world.submitted)
    wait_for(lambda: len(world.ai_review_failures) == 1)
    signal = world.ai_review_failures[0]
    assert signal["stage"] == "generate"
    assert signal["invoked"] is False
    assert world.ai_review_posts == []


def test_an_empty_review_files_a_durable_signal(world):
    client = world.client
    state = world.state
    state["review_text"] = "   "
    play_out_the_match(client, state, world.submitted)
    wait_for(lambda: len(world.ai_review_failures) == 1)
    signal = world.ai_review_failures[0]
    assert signal["stage"] == "empty"
    assert signal["invoked"] is True
    assert world.ai_review_posts == []


def test_a_review_that_the_game_server_refuses_files_a_durable_signal(world):
    """The fourth call, the filing itself: a review generated and then
    refused is not a review anybody can read, so it gets a signal too."""
    client = world.client
    state = world.state
    state["ai_review_route"] = "error"
    play_out_the_match(client, state, world.submitted)
    wait_for(lambda: len(world.ai_review_failures) == 1)
    signal = world.ai_review_failures[0]
    assert signal["stage"] == "file"
    assert signal["invoked"] is True
    assert world.ai_review_posts == []


def test_failing_to_record_a_failure_is_itself_swallowed(world):
    """Clause (e): a failure to record a failure must not be able to throw
    out of the background review thread or wedge retirement."""
    client = world.client
    state = world.state
    state["wrapper_review"] = "error"
    state["failure_route"] = "down"
    play_out_the_match(client, state, world.submitted)
    wait_for(lambda: client.get("/status").json()["gameId"] is None)
    assert world.ai_review_failures == []
    assert world.ai_review_posts == []
    assert world.reflection_posts != []


def test_a_successful_review_files_no_failure_signal(world):
    client = world.client
    state = world.state
    play_out_the_match(client, state, world.submitted)
    wait_for(lambda: len(world.ai_review_posts) == 1)
    assert world.ai_review_failures == []


def test_the_review_stays_best_effort_and_never_blocking_under_the_checkout_lock(world):
    """T14 clause (b): the serialization is a lock on the /guides checkout,
    NOT a join before the handover - so the property that made the join
    unnecessary has to still hold. A slow review neither delays the handover
    response nor is abandoned by retirement: `retire` still joins it, bounded.
    """
    client = world.client
    state = world.state
    state["wrapper_review_delay"] = 0.5
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    started = time.monotonic()
    body = client.post(
        "/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"}
    ).json()
    # The handover was answered without waiting on the review at all.
    assert time.monotonic() - started < 0.4
    assert body["handover"]["outcome"] == "bob won"

    complete_handover(client, world.submitted)
    wait_for(lambda: client.get("/status").json()["gameId"] is None)
    # And the slow review still landed rather than being dropped on the way out.
    assert len(world.ai_review_posts) == 1


# -- T34: a review is considered on EVERY way a game reaches a terminal state --
#
# `post_ai_review` used to be spawned only from `turn_done`'s reflection
# branch, so a game ending any other way filed nothing and its review page
# rendered no `section.ai-review` at all. `consider_ai_review` is now the one
# place that decides, and every terminal path reaches it.


def test_every_terminal_path_is_named_and_reaches_the_filing_decision():
    """The structural guard (clause f): the enumeration is not a comment.
    Every name in `TERMINAL_PATHS` is passed to `consider_ai_review` by some
    call site in the driver, and `retire` - which every terminal path ends
    in - is itself one of them, so a future terminal path that drops the
    assignment cannot silently join the set that files nothing."""
    from pathlib import Path

    from mtg_agent.driver.app import TERMINAL_PATHS

    source = Path(__import__("mtg_agent.driver.app", fromlist=["app"]).__file__ or "").read_text()
    for name in TERMINAL_PATHS:
        assert f'"{name}"' in source, f"no call site ever passes the terminal path {name!r}"
    assert "retire" in TERMINAL_PATHS
    assert "reflection_turn" in TERMINAL_PATHS
    assert "handover_without_reflection" in TERMINAL_PATHS


def test_a_handover_without_a_reflection_still_files_a_review(world):
    """The live-observed path: the retained cluster log shows a driver
    `releasing game ... without a reflection turn, handing over first`, and
    that game's review carried no `ai_review` action at all."""
    client = world.client
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    released = client.post("/release", json={"uid": "secret-uid", "reason": "lobby reset"})
    assert released.json()["reflected"] is False
    complete_handover(client, world.submitted)
    wait_for(lambda: client.get("/status").json()["gameId"] is None)

    wait_for(lambda: len(world.ai_review_posts) == 1)
    assert world.reflection_posts == []
    assert world.ai_review_posts[0]["player"] == "bob"


def test_a_reflectionless_review_prompt_is_well_formed(world):
    """Clause (c): `review_prompt` already tolerates a missing reflection,
    and the prompt it builds from an empty one plus a full ledger still
    names the seat, the outcome and the record's own declarations."""
    client = world.client
    state = world.state
    state["review_record"] = {
        "players": ["bob", "alice"],
        "winner": "bob",
        "drawn": False,
        "declarations": [
            {
                "player": "bob",
                "turn": 3,
                "plan": "trade on the ground and hold the counterspell",
                "prediction": "she taps out for the four-drop",
                "did": [{"action": "cast"}],
                "opponent_did": [{"action": "play_land"}],
            }
        ],
        "advice": [],
    }
    dispatch(client)
    state["winner"] = "bob"
    client.post("/release", json={"uid": "secret-uid", "reason": "lobby reset"})
    complete_handover(client, world.submitted)
    wait_for(lambda: len(world.review_requests) == 1)

    prompt = world.review_requests[0]["prompt"]
    assert "(the seat left no reflection)" in prompt
    assert "trade on the ground and hold the counterspell" in prompt
    assert "bob" in prompt and "alice" in prompt
    assert "bob won" in prompt


def test_a_game_that_ended_with_an_unreachable_agent_still_files_a_review(world):
    """The abnormal termination: the game is over, the agent cannot be given
    a reflection turn at all, and `turn_done` retires it on the spot. That
    path used to file nothing."""
    client = world.client
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    state["agent"] = "down"
    body = client.post(
        "/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"}
    ).json()
    assert body["retired"] is True
    assert body["reflected"] is False
    wait_for(lambda: len(world.ai_review_posts) == 1)
    assert world.ai_review_posts[0]["player"] == "bob"


def test_no_terminal_path_files_two_reviews_for_one_game(world):
    """Clause (d): the dedupe is extended to cover the new paths. The
    reflection branch considers the review, and the handover that follows it
    reaches `consider_ai_review` again, as does `retire` - exactly one
    review is filed."""
    client = world.client
    state = world.state
    play_out_the_match(client, state, world.submitted)
    wait_for(lambda: len(world.ai_review_posts) == 1)
    # A retried `/turn-done` delivery, which `webhook.Deliverer` really does
    # send up to three times.
    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"})
    client.post("/handover", json={"outcome": "bob won"})
    time.sleep(0.2)
    assert len(world.ai_review_posts) == 1
    assert len(world.review_requests) == 1


def test_a_review_on_a_new_path_never_blocks_the_handover_or_retirement(world):
    """Clause (e): the best-effort property holds everywhere. A wrapper that
    cannot generate the review on the handover-without-reflection path must
    still let the handover complete and the agent retire."""
    client = world.client
    state = world.state
    state["wrapper_review"] = "down"
    dispatch(client)
    state["winner"] = "bob"
    client.post("/release", json={"uid": "secret-uid", "reason": "lobby reset"})
    complete_handover(client, world.submitted)
    wait_for(lambda: client.get("/status").json()["gameId"] is None)
    wait_for(lambda: len(world.review_requests) == 1)
    assert world.ai_review_posts == []


def test_a_path_that_cannot_file_a_review_records_why(world, caplog):
    """Clause (b): "cannot" is only acceptable with a stated reason. A game
    released before it ended reaches the filing decision on the
    handover-without-reflection path, and when the record it would review is
    unusable it says so rather than skipping silently. Turning that stated
    reason into a durable `ai_review_failed` row is T35's deliverable and is
    asserted on T35's branch - this branch owns only "every terminal path
    reaches the decision, and a path that cannot file says why"."""
    import logging

    client = world.client
    state = world.state
    state["review_record"] = "not a review record"
    caplog.set_level(logging.WARNING, logger="driver")
    dispatch(client)
    client.post("/release", json={"uid": "secret-uid", "reason": "lobby reset"})
    complete_handover(client, world.submitted)
    wait_for(lambda: client.get("/status").json()["gameId"] is None)
    wait_for(
        lambda: any(
            "could not build the AI review prompt for game" in r.getMessage()
            for r in caplog.records
        )
    )
    assert world.review_requests == []
    assert world.ai_review_posts == []


# -- T36: the ordering that made D-7 invisible -------------------------------
#
# `turn_done` answers a turn that lands during a handover on the handover
# branch and RETURNS THERE - above the reflection check, above the
# game-ended check, above everything that used to be the only way a review
# was ever filed. That is not an edge case on the live-observed path, it is
# the path: the lobby releases the seat, the driver hands over first, and the
# turn the agent was in the middle of reports back into a handover that has
# already started. Pinning the ordering here, on the fake world, keeps the
# cheap structural guard beside the end-to-end proof in
# `tests/driver/test_review_filing.py`, which drives the same ordering
# through the real api and the real wrapper review route.


def test_the_turn_that_lands_during_a_release_returns_on_the_handover_branch(world):
    """The in-flight turn's callback never reaches the reflection check -
    and the review is filed anyway, which is the whole of D-7."""
    client = world.client
    state = world.state
    dispatch(client)
    state["winner"] = "bob"
    client.post("/release", json={"uid": "secret-uid", "reason": "lobby reset"})

    answered = client.post(
        "/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "I attack"}
    ).json()
    # The handover status, and nothing else: no `reflectionTurnId`, no
    # `reflected`, no `retired` - none of the keys the branches below it
    # answer with were ever reached.
    assert set(answered) == {"handover"}
    assert answered["handover"]["outcome"] == "released"

    complete_handover(client, world.submitted)
    wait_for(lambda: client.get("/status").json()["gameId"] is None)
    wait_for(lambda: len(world.ai_review_posts) == 1)
    # No reflection was ever pushed to the agent, or posted to the game
    # server: this game's review is the only post-game artifact it has. The
    # agent got turn zero and then, straight away, the handover turn.
    assert world.reflection_posts == []
    assert len(world.submitted) == 2
    assert world.submitted[1]["text"].startswith(HANDOVER_OPENING)


def test_a_release_files_the_review_before_the_session_is_torn_down(world):
    """Ordering again, one step further on: `retire` joins the review thread
    before `DELETE /v1/session`, so the review is filed while the agent
    container is still up rather than racing its shutdown. The event tape is
    the evidence - `ai_review` before `delete`, on the path that has no
    reflection to slow anything down.

    The generation is deliberately slow here: a review that answers instantly
    wins that race whether or not anything waits for it, so the assertion
    would hold for the wrong reason and a join that was removed again would
    go unnoticed."""
    client = world.client
    state = world.state
    state["wrapper_review_delay"] = 0.3
    dispatch(client)
    state["winner"] = "bob"
    client.post("/release", json={"uid": "secret-uid", "reason": "lobby reset"})
    complete_handover(client, world.submitted)
    wait_for(lambda: client.get("/status").json()["gameId"] is None)

    assert "ai_review" in world.events and "delete" in world.events
    assert world.events.index("ai_review") < world.events.index("delete")


# ---- D1: the opponent's deck, identified once the game is over ---------------

#: Two clauses the world's `OPPONENT_DECKLIST` satisfies in full.
PROWESS_FULL = {
    "schema_version": 1,
    "archetype": "izzet prowess",
    "signature": [
        {"cards": ["Monastery Swiftspear"], "min": 4, "why": "the one-drop"},
        {"cards": ["Lightning Bolt"], "min": 4, "why": "the burn"},
    ],
}

#: The same archetype with a second clause the list misses: half its
#: signature, so the classifier answers LOW.
PROWESS_PARTIAL = {
    **PROWESS_FULL,
    "signature": [
        PROWESS_FULL["signature"][0],
        {"cards": ["Dragon's Rage Channeler"], "min": 4, "why": "the other threat"},
    ],
}


def add_guide(world, slug: str, archetype: dict) -> None:
    root = world.settings.guides_root / "decks" / slug
    root.mkdir(parents=True)
    (root / "decklist.txt").write_text("4 Monastery Swiftspear\n")
    (root / "archetype.json").write_text(json.dumps(archetype))


def reach_the_handover(world, winner: str = "alice") -> tuple[str, str]:
    """Dispatch, end the game, finish the reflection: the reflection turn's
    text and the handover turn's text."""
    client = world.client
    dispatch(client)
    world.state["winner"] = winner
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    reflection = world.submitted[1]["text"]
    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"})
    wait_for(lambda: any(t["text"].startswith(HANDOVER_OPENING) for t in world.submitted))
    handover = next(t["text"] for t in world.submitted if t["text"].startswith(HANDOVER_OPENING))
    return reflection, handover


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize(
    ("setup", "in_reflection", "in_handover"),
    [
        (
            lambda w: add_guide(w, "prowess", PROWESS_FULL),
            "`prowess` (confident; archetype `izzet prowess`)",
            "`mcp-seat guides propose --deck dimir-frog --deck prowess --game-id g-1`",
        ),
        (
            lambda w: add_guide(w, "prowess", PROWESS_PARTIAL),
            "`prowess` (low confidence; archetype `izzet prowess`)",
            "Opponent's deck: `prowess` (low confidence",
        ),
        (
            lambda w: None,
            "no deck in the guides corpus matches",
            "Opponent's deck: no guide",
        ),
        (
            lambda w: w.state.update(opponent_decklist=DECKLIST),
            "mirror",
            "`mcp-seat guides propose --deck dimir-frog --game-id g-1`",
        ),
        (
            lambda w: w.state.update(opponent_decklist=None),
            "could not read",
            "Opponent's deck: not revealed",
        ),
    ],
    ids=["confident", "low", "none", "mirror", "missing"],
)
def test_the_opponents_deck_is_identified_before_the_reflection(
    world, setup, in_reflection, in_handover
):
    setup(world)
    reflection, handover = reach_the_handover(world)
    assert in_reflection in _flat(reflection)
    assert in_handover in _flat(handover)


def test_the_opponents_list_is_read_with_the_seats_own_uid_after_game_over(world):
    reach_the_handover(world)
    assert {"uid": "secret-uid", "over": True} in world.seat_reads


def test_the_opponents_list_is_read_once_per_game(world):
    reach_the_handover(world)
    assert sum(1 for read in world.seat_reads if read["over"]) == 1


def test_the_handover_lists_the_existing_stub_decks(world):
    stub = world.settings.guides_root / "decks" / "mono-red-burn"
    stub.mkdir(parents=True)
    (stub / "NOTES.md").write_text("# mono red burn\n")
    _, handover = reach_the_handover(world)
    assert "Existing stubs: `mono-red-burn`" in _flat(handover)


def test_an_unreadable_seats_route_is_a_missing_list_not_a_failed_reflection(world):
    """Best effort: the reflection is still pushed, with the fallback."""
    client = world.client
    dispatch(client)
    world.state["winner"] = "alice"
    world.state["opponent_decklist"] = ""
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    assert "could not read" in _flat(world.submitted[1]["text"])


def test_the_ai_review_prompt_carries_the_decks_cross_game_evidence(world):
    """D2b: the reviewer's two-distinct-games bars need declines from more
    than this one game, so its prompt carries `evidence_table` over the
    evidence files `plays admit` committed under the guides checkout."""
    from magic_cards_model_api.playscripts.report import evidence_table

    evidence = world.settings.guides_root / "decks" / "dimir-frog" / "plays" / "evidence"
    evidence.mkdir(parents=True)
    for game in ("old-1", "old-2"):
        (evidence / f"{game}.json").write_text(
            json.dumps(
                {
                    "game_id": game,
                    "deck": "dimir-frog",
                    "opponent_archetype": "izzet prowess",
                    "plays": {
                        "frog-attack": {"offered": 1, "taken": 0, "passed_over": 0, "declined": 1}
                    },
                }
            )
        )
    client, state = world.client, world.state
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "notes"})
    wait_for(lambda: len(world.review_requests) == 1)

    prompt = world.review_requests[0]["prompt"]
    table = evidence_table(world.settings.guides_root, "dimir-frog")
    assert "frog-attack | 2 | 2 | 0 | 0 | 2 | 2" in table
    assert table in prompt
    complete_handover(client, world.submitted)
