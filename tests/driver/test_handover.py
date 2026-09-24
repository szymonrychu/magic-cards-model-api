"""A14: the driver's handover - one idempotent `POST /handover`, reached by a
natural end, by `/release` and by the Job's preStop.

The world is `tests/driver/world.py`'s fake game server and fake wrapper, both
real FastAPI apps over ASGI. The clock the handover reads is injected, so the
1380 s nudge and the budget are driven by moving a number rather than waiting.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from magic_cards_model_api.playscripts.report import (
    DevelopmentCandidates,
    evidence_record,
    evidence_table,
    hand_played_summary,
    offer_table,
    script_run_table,
)
from mtg_agent.driver import handover
from mtg_agent.driver.prompt import HANDOVER_OPENING, REPUSH_OPENING
from tests.driver.world import ADVISOR_UID, FIXTURE_PLAYS, build_world


class Clock:
    """Monotonic seconds the test sets by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def world(tmp_path, clock):
    return build_world(tmp_path, clock=clock)


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition never became true")


def dispatch(world, *, role: str = "seat", uid: str | None = None) -> None:
    body = {
        "uid": uid or world.uid,
        "game_id": world.game_id,
        "player": "bob",
        "api_base": "http://game:8000",
        "role": role,
    }
    response = world.client.post("/dispatch", json=body)
    assert response.status_code == 202, response.text


def handover_turns(world) -> list[dict]:
    return [turn for turn in world.submitted if turn["text"].startswith(HANDOVER_OPENING)]


def handover_turn_id(world) -> str:
    wait_for(lambda: handover_turns(world))
    index = world.submitted.index(handover_turns(world)[0])
    return f"turn-{index + 1}"


def finish(world, turn_id: str, text: str = "pushed") -> None:
    world.client.post(
        "/turn-done", json={"turnId": turn_id, "state": "complete", "finalText": text}
    )


def marker(world):
    return world.settings.workspace / handover.MARKER_NAME


def marker_status(world) -> dict:
    wait_for(lambda: marker(world).exists())
    return json.loads(marker(world).read_text())


def section(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + len(heading) + 1)
    return text[start : end if end != -1 else len(text)]


# ---- idempotency --------------------------------------------------------------


def test_the_first_call_starts_the_handover_and_later_calls_join_it(world):
    dispatch(world)
    first = world.client.post("/handover", json={})
    second = world.client.post("/handover", json={"outcome": "released"})
    assert first.status_code == second.status_code == 202
    assert first.json()["handoverId"] == second.json()["handoverId"]
    assert second.json()["outcome"] == first.json()["outcome"] == "killed"

    finish(world, handover_turn_id(world))
    done = marker_status(world)
    later = world.client.post("/handover", json={}).json()
    assert later["handoverId"] == first.json()["handoverId"]
    assert later["state"] == done["state"] == "done"
    assert len(handover_turns(world)) == 1


def test_concurrent_calls_join_one_handover_and_push_one_turn(world):
    dispatch(world)
    barrier = threading.Barrier(6)
    answers: list[dict] = []

    def call() -> None:
        barrier.wait()
        answers.append(world.client.post("/handover", json={}).json())

    threads = [threading.Thread(target=call) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({answer["handoverId"] for answer in answers}) == 1
    finish(world, handover_turn_id(world))
    marker_status(world)
    assert len(handover_turns(world)) == 1


def test_a_turn_in_flight_is_interrupted_before_the_handover_is_pushed(world):
    dispatch(world)  # turn 0 is in flight
    world.client.post("/handover", json={})
    handover_turn_id(world)
    assert world.events.index("interrupt") < len(world.events) - 1
    assert world.events[-1] == "message"
    assert len(world.interrupts) == 1


def test_no_turn_in_flight_means_no_interrupt(world):
    dispatch(world)
    world.state["agent"] = "down"  # so no continuation can be pushed either
    finish(world, "turn-1")  # the game is still running: nothing in flight now
    world.state["agent"] = "up"
    world.client.post("/handover", json={})
    handover_turn_id(world)
    assert world.interrupts == []


def test_the_outcome_is_killed_unless_a_winner_is_known(world):
    dispatch(world)
    world.state["winner"] = "bob"
    answer = world.client.post("/handover", json={}).json()
    assert answer["outcome"] == "bob won"


def test_a_handover_with_nothing_assigned_writes_the_marker_at_once(world):
    answer = world.client.post("/handover", json={}).json()
    assert answer["state"] == "idle"
    assert marker_status(world)["state"] == "idle"


# ---- release ------------------------------------------------------------------


def test_release_starts_the_handover_and_retires_only_after_the_marker(world):
    dispatch(world)
    answer = world.client.post("/release", json={"uid": world.uid, "game_id": world.game_id})
    assert answer.status_code == 202
    body = answer.json()
    assert body["released"] is True
    assert body["handover"]["outcome"] == "released"
    assert world.state["deleted"] == 0, "release no longer ends the session directly"
    assert world.client.get("/status").json()["gameId"] == world.game_id

    turn_id = handover_turn_id(world)
    assert "released" in handover_turns(world)[0]["text"]
    assert world.state["deleted"] == 0
    finish(world, turn_id)
    assert marker_status(world)["outcome"] == "released"
    wait_for(lambda: world.state["deleted"] == 1)
    assert world.state["marker_at_delete"] is True
    assert world.client.get("/status").json()["gameId"] is None


def test_a_prestop_handover_during_a_release_joins_it(world):
    dispatch(world)
    released = world.client.post("/release", json={}).json()
    joined = world.client.post("/handover", json={}).json()
    assert joined["handoverId"] == released["handover"]["handoverId"]
    assert joined["outcome"] == "released"


# ---- natural end ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("ending", "outcome"), [("winner", "bob won"), ("drawn", "the game was a draw")]
)
def test_a_natural_end_reflects_then_posts_then_hands_over_then_retires(world, ending, outcome):
    dispatch(world)
    world.state[ending] = "bob" if ending == "winner" else True
    finish(world, "turn-1", "gg")
    assert "The game is over" in world.submitted[1]["text"]
    assert handover_turns(world) == []

    finish(world, "turn-2", "wrote")
    turn_id = handover_turn_id(world)
    assert outcome in handover_turns(world)[0]["text"]
    # The AI review (`review`/`ai_review` events) now runs in its own
    # background thread, started only after the handover, precisely so it
    # never delays or reorders the handover push - so it can legitimately
    # land its events before, after, or interleaved with "message" below.
    # Filtered out here because this assertion is about the SYNCHRONOUS
    # reflect-then-hand-over ordering `turn_done` itself still guarantees,
    # not about when the independent background review happens to finish.
    synchronous_events = [e for e in world.events if e not in ("review", "ai_review")]
    assert synchronous_events.index("reflection") < len(synchronous_events) - 1
    assert synchronous_events[-1] == "message"
    assert world.state["deleted"] == 0

    finish(world, turn_id)
    assert marker_status(world)["state"] == "done"
    wait_for(lambda: world.state["deleted"] == 1)
    assert world.state["marker_at_delete"] is True


# ---- the nudge and the budget ------------------------------------------------------


def test_the_budget_fits_a_handover_that_develops_through_subagents():
    """Game 16a9cf8f: the boros seat's own handover (two subagents, admits,
    a guides proposal) ran out a 510 s budget at 508 s. The budget is now
    28 minutes, nudged at 23, inside the driver preStop's 5460 s (DR3: raised
    to also outlast `driver.app.RETIRE_REVIEW_JOIN_TIMEOUT`); only the
    409 retry keeps its 120 s bound, since an interrupted turn ends in 20 s."""
    assert handover.HANDOVER_BUDGET_SECONDS == 1680.0
    assert handover.NUDGE_AFTER_SECONDS == 1380.0
    assert handover.HANDOVER_PUSH_BUDGET_SECONDS == 120.0
    assert handover.HANDOVER_BUDGET_SECONDS < handover.PRESTOP_WAIT_SECONDS == 5460.0


def test_the_prestop_module_waits_5460_seconds_by_default(tmp_path):
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    def unreachable(url: str, body: dict) -> None:
        raise OSError("connection refused")

    env = {"DRIVER_WORKSPACE": str(tmp_path), "HTTP_ADDR": ":8090"}
    assert handover.main([], env=env, post=unreachable, clock=lambda: now[0], sleep=sleep) == 0
    assert 5460 <= now[0] < 5462


def test_one_push_now_nudge_goes_out_at_1380_seconds(world, clock):
    dispatch(world)
    world.client.post("/handover", json={})
    turn_id = handover_turn_id(world)
    clock.now = handover.NUDGE_AFTER_SECONDS - 1
    time.sleep(0.2)
    assert world.probes == []
    clock.now = handover.NUDGE_AFTER_SECONDS
    wait_for(lambda: world.probes)
    assert "push now" in world.probes[0]["text"].lower()
    clock.now = handover.NUDGE_AFTER_SECONDS + 40
    time.sleep(0.2)
    assert len(world.probes) == 1
    finish(world, turn_id)
    assert marker_status(world)["nudged"] is True


# ---- the marker ---------------------------------------------------------------------


def test_the_marker_carries_the_status_on_success(world):
    dispatch(world)
    started = world.client.post("/handover", json={}).json()
    finish(world, handover_turn_id(world))
    status = marker_status(world)
    assert status["state"] == "done"
    assert status["handoverId"] == started["handoverId"]
    assert status["gameId"] == world.game_id


def test_the_marker_is_written_on_timeout(world, clock):
    dispatch(world)
    world.client.post("/handover", json={})
    handover_turn_id(world)
    clock.now = handover.HANDOVER_BUDGET_SECONDS
    assert marker_status(world)["state"] == "timeout"
    wait_for(lambda: world.state["deleted"] == 1)


def test_the_marker_is_written_on_failure(world):
    dispatch(world)
    world.state["agent"] = "refusing"
    world.client.post("/handover", json={})
    status = marker_status(world)
    assert status["state"] == "failed"
    assert status["error"]
    wait_for(lambda: world.client.get("/status").json()["gameId"] is None)


def test_an_agent_that_is_gone_fails_the_handover_rather_than_waiting_it_out(world):
    dispatch(world)
    world.state["agent"] = "down"
    world.client.post("/handover", json={})
    assert marker_status(world)["state"] == "failed"


class Sleeper:
    """The handover's sleep: records each wait and moves the clock by it."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.now += seconds


def test_a_busy_agent_is_backed_off_and_the_marker_fails_well_before_the_prestop(
    tmp_path, clock, caplog
):
    """Live, game 995daaf7: 5523 pushes answered 409 in 510 s, one every
    ~90 ms, and the marker said only `timeout`."""
    caplog.set_level(logging.INFO, logger="driver")
    sleeper = Sleeper(clock)
    world = build_world(tmp_path, clock=clock, sleep=sleeper)
    dispatch(world)
    world.state["agent"] = "busy"
    world.client.post("/handover", json={})
    status = marker_status(world)
    assert status["state"] == "failed"
    assert "409" in status["error"]
    assert clock.now <= 180, f"gave up only at {clock.now:.0f} s"
    assert world.state["conflicts"] <= 40
    assert sleeper.waits[1] > sleeper.waits[0]
    assert max(sleeper.waits) <= 5.0
    busy = [r.getMessage() for r in caplog.records if "backing off" in r.getMessage()]
    assert len(busy) == 1, busy
    wait_for(lambda: world.state["deleted"] == 1)


def test_a_409_is_retried_with_growing_waits_until_the_agent_accepts(tmp_path, clock):
    sleeper = Sleeper(clock)
    world = build_world(tmp_path, clock=clock, sleep=sleeper)
    dispatch(world)
    world.state["agent"] = "busy"
    world.state["busy_for"] = 3
    world.client.post("/handover", json={})
    finish(world, handover_turn_id(world))
    assert marker_status(world)["state"] == "done"
    assert world.state["conflicts"] == 4
    assert len(sleeper.waits) == 3
    assert sleeper.waits == sorted(sleeper.waits)
    assert sleeper.waits[0] < sleeper.waits[-1]


# ---- buddies ------------------------------------------------------------------------


def test_an_advisor_gets_the_identical_handover(tmp_path, clock):
    texts = {}
    for role, uid in (("seat", None), ("advisor", ADVISOR_UID)):
        world = build_world(tmp_path / role, clock=clock)
        dispatch(world, role=role, uid=uid)
        answer = world.client.post("/handover", json={}).json()
        assert answer["role"] == role
        handover_turn_id(world)
        texts[role] = handover_turns(world)[0]["text"]
        assert world.log_reads == [uid or world.uid]
    assert texts["seat"] == texts["advisor"]


# ---- what the prompt carries -------------------------------------------------------


def test_the_run_table_for_this_game_and_seat_is_in_the_handover_prompt(world):
    world.state["log"] = [
        {
            "seq": 1,
            "action": "script_run",
            "detail": {
                "player": "bob",
                "seat": "bob",
                "game_id": world.game_id,
                "script_id": "fetch-surveil-land",
                "exit": "done",
                "priority": 50,
                "reapplicable_after": "true",
            },
        }
    ]
    dispatch(world)
    world.client.post("/handover", json={})
    handover_turn_id(world)
    text = handover_turns(world)[0]["text"]
    expected = script_run_table(world.state["log"], world.game_id, "bob")
    assert "fetch-surveil-land | 50 | 1 | done=1 | 1" in expected
    assert expected in text


def test_the_conflict_block_says_none_when_the_corpus_has_no_live_warning(world):
    dispatch(world)
    world.client.post("/handover", json={})
    handover_turn_id(world)
    block = section(handover_turns(world)[0]["text"], "## Conflict warnings")
    assert "none" in block.lower()
    assert "play-conflict" not in block


def test_the_conflict_block_names_a_live_warning_compactly(tmp_path, clock):
    world = build_world(tmp_path, clock=clock)
    plays = world.settings.guides_root / "decks" / "dimir-frog" / "plays"
    copy = json.loads((FIXTURE_PLAYS / "fetch-surveil-land.json").read_text())
    copy["id"] = "fetch-surveil-land-twin"
    (plays / "fetch-surveil-land-twin.json").write_text(json.dumps(copy))
    dispatch(world)
    world.client.post("/handover", json={})
    handover_turn_id(world)
    block = section(handover_turns(world)[0]["text"], "## Conflict warnings")
    (line,) = [line for line in block.splitlines() if line.startswith("play-conflict: ")]
    ids, _, dimensions = line.removeprefix("play-conflict: ").partition(" (")
    assert sorted(ids.split(", ")) == ["fetch-surveil-land", "fetch-surveil-land-twin"]
    assert "whose_turn" in dimensions and "steps" in dimensions
    assert "none" not in block.lower()


# ---- the preStop module --------------------------------------------------------------


def test_the_prestop_module_posts_the_local_driver_and_returns_once_the_marker_exists(tmp_path):
    workspace = tmp_path / "driver-workspace"
    workspace.mkdir()
    posted: list[tuple[str, dict]] = []

    def post(url: str, body: dict) -> None:
        posted.append((url, body))
        (workspace / handover.MARKER_NAME).write_text("{}")

    env = {"DRIVER_WORKSPACE": str(workspace), "HTTP_ADDR": ":8090"}
    started = time.monotonic()
    assert handover.main(["--wait", "30"], env=env, post=post) == 0
    assert time.monotonic() - started < 5
    assert posted == [("http://127.0.0.1:8090/handover", {})]


def test_the_prestop_module_gives_up_after_wait_seconds_and_still_exits_zero(tmp_path):
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    def unreachable(url: str, body: dict) -> None:
        raise OSError("connection refused")

    env = {"DRIVER_WORKSPACE": str(tmp_path), "HTTP_ADDR": ":8090"}
    code = handover.main(
        ["--wait", "1740"], env=env, post=unreachable, clock=lambda: now[0], sleep=sleep
    )
    assert code == 0
    assert now[0] >= 1740


def test_the_prestop_module_exits_zero_on_arguments_it_cannot_read(tmp_path):
    assert handover.main(["--wait", "soon"], env={"DRIVER_WORKSPACE": str(tmp_path)}) == 0


# ---- D1: after a concede the seat reflects, then writes the guide updates ----


def test_after_a_concede_the_reflection_runs_and_the_review_carries_it(world):
    """A3: a natural game over - a concede included - no longer releases the
    AI seat, so the driver sees the game over on its next turn, reads both
    lists with the seat's own uid, reflects, and files the review with the
    reflection's own text rather than taking `handover_without_reflection`."""
    dispatch(world)
    world.state["winner"] = "alice"
    finish(world, "turn-1", "conceded")
    assert "The game is over" in world.submitted[1]["text"]
    assert {"uid": world.uid, "over": True} in world.seat_reads

    finish(world, "turn-2", "they curved out and I kept a slow hand")
    wait_for(lambda: world.review_requests)
    prompt = world.review_requests[0]["prompt"]
    assert "they curved out and I kept a slow hand" in prompt
    assert "(the seat left no reflection)" not in prompt
    assert "Opponent's deck: no guide" in prompt

    turn_id = handover_turn_id(world)
    text = " ".join(handover_turns(world)[0]["text"].split())
    assert "Opponent's deck: no guide" in text
    assert "## Guide updates" in handover_turns(world)[0]["text"]
    finish(world, turn_id)
    wait_for(lambda: world.ai_review_posts)
    assert world.ai_review_failures == []


def test_a_handover_after_game_over_identifies_the_opponent_lazily(world):
    """No reflection ran (the preStop arrived first): the handover still
    reads the revealed list before it writes the turn."""
    dispatch(world)
    world.state["winner"] = "alice"
    world.client.post("/handover", json={})
    handover_turn_id(world)
    assert {"uid": world.uid, "over": True} in world.seat_reads
    assert "Opponent's deck: no guide" in " ".join(handover_turns(world)[0]["text"].split())


def test_a_release_mid_game_has_no_revealed_list_and_says_so(world):
    dispatch(world)
    world.client.post("/release", json={})
    handover_turn_id(world)
    assert "Opponent's deck: not revealed" in " ".join(handover_turns(world)[0]["text"].split())


# ---- D2b: mandatory DSL development, evidence, one re-push, the outcome row ----------


def offer_row(seq: int, script_id: str, run_id: str, game_id: str = "g-1") -> dict:
    return {
        "seq": seq,
        "action": "script_run_offered",
        "detail": {
            "player": "bob",
            "seat": "bob",
            "game_id": game_id,
            "run_id": run_id,
            "script_id": script_id,
            "priority": 50,
            "advise": False,
            "turn": 3,
            "step": "precombat_main",
        },
    }


def hand_cast(seq: int, card: str) -> dict:
    return {
        "seq": seq,
        "action": "cast",
        "detail": {"player": "bob", "allowed": True, "reasoning": "r", "card": card},
    }


#: A game where the seat declined `frog-attack` for a hand cast, and cast
#: Fatal Push by hand twice: two development candidates.
CANDIDATE_LOG = [
    offer_row(1, "frog-attack", "run-1"),
    hand_cast(2, "Fatal Push"),
    hand_cast(3, "Fatal Push"),
]


def journal(world, *entries: dict, role: str = "seat") -> None:
    """Append what D2a's `mcp-seat` commands would have written - as `role`,
    "seat" by default, "reviewer" for a post-game AI review's own admits."""
    world.journal.parent.mkdir(parents=True, exist_ok=True)
    with world.journal.open("a") as handle:
        for entry in entries:
            handle.write(json.dumps({"ts": "t", "game_id": world.game_id, "role": role} | entry))
            handle.write("\n")


def admit(
    result: str = "admitted",
    dsl_change: bool = True,
    sha: str = "abc123",
    added: tuple[str, ...] | None = (),
    updated: tuple[str, ...] | None = (),
    reprioritized: tuple[str, ...] | None = (),
) -> dict:
    """A `plays admit` journal line, D2a's shape (`gitwork.journal`) plus
    DR5's `added`/`updated`/`reprioritized` play-id lists (`admit._landing`).
    Pass `None` for any of the three to build a LEGACY line - one written
    before DR5, that never carried the key at all - rather than one that
    carries it empty."""
    entry = {
        "command": "plays admit",
        "decks": ["dimir-frog"],
        "result": result,
        "sha": sha,
        "dsl_change": dsl_change,
    }
    if added is not None:
        entry["added"] = list(added)
    if updated is not None:
        entry["updated"] = list(updated)
    if reprioritized is not None:
        entry["reprioritized"] = list(reprioritized)
    return entry


def no_change(reason: str = "the board never allowed it", **candidate) -> dict:
    return (
        {"command": "no_change", "decks": ["dimir-frog"], "result": "recorded"}
        | candidate
        | {"reason": reason}
    )


def repush_turns(world) -> list[dict]:
    return [turn for turn in world.submitted if turn["text"].startswith(REPUSH_OPENING)]


def outcome(world) -> dict:
    wait_for(lambda: world.plays_outcomes)
    return world.plays_outcomes[0]


def test_the_journal_is_read_from_the_checkouts_git_dir(tmp_path):
    root = tmp_path / "guides"
    (root / ".git").mkdir(parents=True)
    assert handover.journal_path(root) == root / ".git" / "mcm-results.jsonl"


def test_a_linked_checkouts_journal_follows_its_gitdir_pointer(tmp_path):
    root = tmp_path / "guides"
    root.mkdir()
    real = tmp_path / "elsewhere" / "git"
    real.mkdir(parents=True)
    (root / ".git").write_text(f"gitdir: {real}\n")
    assert handover.journal_path(root) == real / "mcm-results.jsonl"


def test_read_journal_keeps_this_games_lines_for_this_role_and_skips_garbage(tmp_path):
    root = tmp_path / "guides"
    path = root / ".git" / "mcm-results.jsonl"
    path.parent.mkdir(parents=True)
    lines = [
        {"game_id": "g-1", "role": "seat", "command": "plays admit", "result": "admitted"},
        {"game_id": "g-2", "role": "seat", "command": "plays admit", "result": "admitted"},
        {"game_id": "g-1", "role": "reviewer", "command": "plays admit", "result": "admitted"},
    ]
    path.write_text(
        json.dumps(lines[0]) + "\n{half a line\n[1, 2]\n" + "\n".join(map(json.dumps, lines[1:]))
    )
    assert handover.read_journal(root, "g-1", "seat") == [lines[0]]
    assert handover.read_journal(tmp_path / "missing", "g-1", "seat") == []


def test_a_dsl_change_admit_satisfies_the_development_step():
    candidates = DevelopmentCandidates(plays=("frog-attack",), lines=("cast Fatal Push",))
    verdict = handover.assess([admit()], candidates)
    assert verdict.satisfied
    assert verdict.missing == ()
    assert verdict.admitted == (
        {"sha": "abc123", "dsl_change": True, "added": [], "updated": [], "reprioritized": []},
    )


# -- DR4: read_journal/assess expose the admit's own added/updated/reprioritized play ids ----


def test_assess_exposes_the_added_updated_and_reprioritized_play_ids_from_the_admit():
    candidates = DevelopmentCandidates()
    entry = admit(added=("new-play",), updated=("frog-attack",), reprioritized=("fetch-x",))
    verdict = handover.assess([entry], candidates)
    assert verdict.admitted == (
        {
            "sha": "abc123",
            "dsl_change": True,
            "added": ["new-play"],
            "updated": ["frog-attack"],
            "reprioritized": ["fetch-x"],
        },
    )


def test_a_legacy_admit_line_without_the_new_keys_yields_empty_lists():
    """A journal line written before DR5's `admit._landing` classification
    carries no `added`/`updated`/`reprioritized` key at all - `assess` must
    not raise on it, and must expose empty lists rather than omitting the
    keys, so a reader never has to branch on which shape it got."""
    candidates = DevelopmentCandidates()
    legacy = admit(added=None, updated=None, reprioritized=None)
    assert "added" not in legacy and "updated" not in legacy and "reprioritized" not in legacy
    verdict = handover.assess([legacy], candidates)
    assert verdict.admitted == (
        {"sha": "abc123", "dsl_change": True, "added": [], "updated": [], "reprioritized": []},
    )


# -- DR4: candidates the AI review already answered count as answered -------


def test_review_answered_plays_is_the_union_of_added_and_updated():
    reviewer_entries = [
        admit(added=("new-play",), updated=("frog-attack",), reprioritized=("fetch-x",)),
    ]
    assert handover.review_answered_plays(reviewer_entries) == frozenset(
        {"new-play", "frog-attack"}
    )


def test_review_answered_plays_excludes_a_priority_only_reprioritize():
    """A priority-only change never counts as answering a development
    candidate - the same rule the handover's own mandatory-development step
    already applies to a plain `plays priority` call."""
    reviewer_entries = [admit(added=(), updated=(), reprioritized=("fetch-x",))]
    assert handover.review_answered_plays(reviewer_entries) == frozenset()


def test_review_answered_plays_ignores_a_failed_admit():
    reviewer_entries = [admit(result="conflict", added=("new-play",))]
    assert handover.review_answered_plays(reviewer_entries) == frozenset()


def test_review_answered_plays_ignores_a_no_change_entry():
    reviewer_entries = [no_change(play="frog-attack")]
    assert handover.review_answered_plays(reviewer_entries) == frozenset()


def test_review_answered_plays_of_a_legacy_line_is_empty():
    reviewer_entries = [admit(added=None, updated=None, reprioritized=None)]
    assert handover.review_answered_plays(reviewer_entries) == frozenset()


def test_review_answered_plays_of_no_entries_is_empty():
    assert handover.review_answered_plays([]) == frozenset()


def test_plays_outcome_body_sends_added_updated_reprioritized_on_each_admitted_entry():
    """DR2's `PlaysOutcomeIn` schema takes these on each admitted entry -
    `plays_outcome_body` only has to forward what `assess` already built into
    `verdict.admitted`, never derive them itself."""
    candidates = DevelopmentCandidates()
    entry = admit(added=("new-play",), updated=("frog-attack", "fetch-x"), reprioritized=())
    verdict = handover.assess([entry], candidates)
    body = handover.plays_outcome_body(
        uid="u-1", player="bob", role="seat", verdict=verdict, reasons=(), repushed=False
    )
    assert body["admitted"] == [
        {
            "sha": "abc123",
            "dsl_change": True,
            "added": ["new-play"],
            "updated": ["frog-attack", "fetch-x"],
            "reprioritized": [],
        }
    ]


def test_a_priority_only_admit_does_not_satisfy_it_and_names_every_open_candidate():
    candidates = DevelopmentCandidates(plays=("frog-attack",), lines=("cast Fatal Push",))
    verdict = handover.assess([admit(dsl_change=False)], candidates)
    assert not verdict.satisfied
    assert "no conditions or advice-only change was admitted (`dsl_change`)" in verdict.missing
    assert "play `frog-attack`" in verdict.missing
    assert "line `cast Fatal Push`" in verdict.missing


def test_a_no_change_for_every_candidate_satisfies_it():
    candidates = DevelopmentCandidates(plays=("frog-attack",), lines=("cast Fatal Push",))
    entries = [no_change(play="frog-attack"), no_change(line="  cast  fatal push ")]
    verdict = handover.assess(entries, candidates)
    assert verdict.satisfied
    assert verdict.no_change == (
        {"candidate": "frog-attack", "reason": "the board never allowed it"},
        {"candidate": "cast fatal push", "reason": "the board never allowed it"},
    )


def test_a_no_change_for_only_some_candidates_leaves_the_rest_missing():
    candidates = DevelopmentCandidates(plays=("frog-attack",), lines=("cast Fatal Push",))
    verdict = handover.assess([no_change(play="frog-attack")], candidates)
    assert not verdict.satisfied
    assert "line `cast Fatal Push`" in verdict.missing
    assert "play `frog-attack`" not in verdict.missing


def test_a_game_with_no_candidates_needs_nothing():
    assert handover.assess([], DevelopmentCandidates()).satisfied


def test_the_last_admit_or_propose_decides_whether_the_writes_succeeded():
    propose_failed = {"command": "guides propose", "result": "gate_failed"}
    propose_ok = {"command": "guides propose", "result": "admitted", "sha": "def456"}
    candidates = DevelopmentCandidates()
    assert handover.assess([admit(), propose_failed], candidates).last_write_failed == (
        "guides propose answered `gate_failed`"
    )
    assert handover.assess([propose_failed, propose_ok], candidates).last_write_failed is None
    assert handover.assess([admit("nothing_to_admit")], candidates).last_write_failed is None
    verdict = handover.assess([propose_failed, propose_ok], candidates)
    assert verdict.proposed == ({"result": "gate_failed"}, {"sha": "def456", "result": "admitted"})


def test_the_evidence_file_is_written_where_the_agent_reads_it(world):
    world.state["log"] = CANDIDATE_LOG
    dispatch(world)
    world.client.post("/handover", json={})
    handover_turn_id(world)
    path = world.settings.workspace / "evidence" / world.game_id / "dimir-frog.json"
    assert json.loads(path.read_text()) == evidence_record(
        CANDIDATE_LOG, world.game_id, "bob", "dimir-frog", None
    )
    assert path == handover.evidence_path(world.settings.workspace, world.game_id, "dimir-frog")


def test_the_handover_prompt_carries_the_offers_the_hand_lines_the_evidence_and_the_candidates(
    world,
):
    world.state["log"] = CANDIDATE_LOG
    dispatch(world)
    world.client.post("/handover", json={})
    handover_turn_id(world)
    text = handover_turns(world)[0]["text"]
    assert offer_table(CANDIDATE_LOG, world.game_id, "bob") in text
    assert hand_played_summary(CANDIDATE_LOG, world.game_id, "bob") in text
    assert evidence_table(world.settings.guides_root, "dimir-frog") in text
    candidates = section(text, "## Development candidates")
    assert "--play frog-attack" in candidates
    assert '--line "cast Fatal Push"' in candidates
    assert world.log_reads == [world.uid]


def test_the_handover_skips_a_candidate_the_ai_review_already_admitted(world):
    """DR4 item 5: `handover.review_answered_plays`, read off this game's
    `role: "reviewer"` journal lines, must actually reach `handover_turn`
    from the running `/handover` endpoint - not only from a direct call to
    the prompt function, which always defaulted `review_answered` to empty
    and so could never catch app.py failing to wire it through."""
    world.state["log"] = CANDIDATE_LOG
    dispatch(world)
    journal(world, admit(added=("frog-attack",)), role="reviewer")
    world.client.post("/handover", json={})
    handover_turn_id(world)
    text = handover_turns(world)[0]["text"]
    candidates = section(text, "## Development candidates")
    assert "--play frog-attack" not in candidates
    assert "already answered by this game's AI review" in candidates
    assert '--line "cast Fatal Push"' in candidates


def test_an_unanswered_candidate_gets_exactly_one_re_push_then_an_unfinished_outcome(world):
    world.state["log"] = CANDIDATE_LOG
    dispatch(world)
    world.client.post("/handover", json={})
    finish(world, handover_turn_id(world))

    wait_for(lambda: repush_turns(world))
    text = " ".join(repush_turns(world)[0]["text"].split())
    assert "play `frog-attack`" in text
    assert "line `cast Fatal Push`" in text
    assert world.plays_outcomes == []
    assert not marker(world).exists()

    index = world.submitted.index(repush_turns(world)[0])
    finish(world, f"turn-{index + 1}")
    row = outcome(world)
    assert row["state"] == "unfinished"
    assert row["repushed"] is True
    assert row["followup"] is False
    assert any("re-push" in reason for reason in row["unfinished_reasons"])
    status = marker_status(world)
    assert status["state"] == "done"
    assert status["repushed"] is True
    wait_for(lambda: world.state["deleted"] == 1)
    assert len(repush_turns(world)) == 1
    assert world.events.index("plays_outcome") < world.events.index("delete")


def test_a_re_push_that_lands_the_change_is_complete(world):
    world.state["log"] = CANDIDATE_LOG
    dispatch(world)
    world.client.post("/handover", json={})
    finish(world, handover_turn_id(world))
    wait_for(lambda: repush_turns(world))
    journal(world, admit())
    index = world.submitted.index(repush_turns(world)[0])
    finish(world, f"turn-{index + 1}")
    row = outcome(world)
    assert row["state"] == "complete"
    assert row["repushed"] is True
    assert row["admitted"] == [
        {"sha": "abc123", "dsl_change": True, "added": [], "updated": [], "reprioritized": []}
    ]
    assert row["unfinished_reasons"] == []


def test_a_handover_that_answered_every_candidate_is_complete_with_no_re_push(world):
    world.state["log"] = CANDIDATE_LOG
    dispatch(world)
    world.client.post("/handover", json={})
    turn_id = handover_turn_id(world)
    journal(
        world,
        admit(dsl_change=False),
        no_change(play="frog-attack"),
        no_change(line="cast Fatal Push", reason="removal is a judgement call"),
    )
    finish(world, turn_id)
    row = outcome(world)
    assert row == {
        "game_id": world.game_id,
        "uid": world.uid,
        "player": "bob",
        "role": "seat",
        "state": "complete",
        "admitted": [
            {"sha": "abc123", "dsl_change": False, "added": [], "updated": [], "reprioritized": []}
        ],
        "proposed": [],
        "no_change": [
            {"candidate": "frog-attack", "reason": "the board never allowed it"},
            {"candidate": "cast Fatal Push", "reason": "removal is a judgement call"},
        ],
        "unfinished_reasons": [],
        "repushed": False,
        "followup": False,
        # F1: what the api builds a follow-up's assignment from. The game is
        # still running here, so no opponent list was revealed to identify.
        "deck": "dimir-frog",
    }
    wait_for(lambda: world.state["deleted"] == 1)
    assert repush_turns(world) == []


def test_a_failed_last_propose_makes_the_outcome_unfinished(world):
    dispatch(world)
    world.client.post("/handover", json={})
    turn_id = handover_turn_id(world)
    journal(world, {"command": "guides propose", "decks": ["dimir-frog"], "result": "conflict"})
    finish(world, turn_id)
    row = outcome(world)
    assert row["state"] == "unfinished"
    assert row["unfinished_reasons"] == [
        "the last write did not land: guides propose answered `conflict`"
    ]
    assert row["proposed"] == [{"result": "conflict"}]


def test_a_nudged_handover_is_unfinished(world, clock):
    dispatch(world)
    world.client.post("/handover", json={})
    turn_id = handover_turn_id(world)
    clock.now = handover.NUDGE_AFTER_SECONDS
    wait_for(lambda: world.probes)
    finish(world, turn_id)
    row = outcome(world)
    assert row["state"] == "unfinished"
    assert "nudged: the handover was still running at 23 minutes" in row["unfinished_reasons"]


def test_a_timed_out_handover_is_unfinished_and_never_re_pushed(world, clock):
    world.state["log"] = CANDIDATE_LOG
    dispatch(world)
    world.client.post("/handover", json={})
    handover_turn_id(world)
    clock.now = handover.HANDOVER_BUDGET_SECONDS
    row = outcome(world)
    assert row["state"] == "unfinished"
    assert "timeout: the handover turn did not end within 28 minutes" in row["unfinished_reasons"]
    assert repush_turns(world) == []
    assert marker_status(world)["state"] == "timeout"


def test_the_re_push_waits_within_the_remaining_budget(world, clock):
    world.state["log"] = CANDIDATE_LOG
    dispatch(world)
    world.client.post("/handover", json={})
    finish(world, handover_turn_id(world))
    wait_for(lambda: repush_turns(world))
    clock.now = handover.HANDOVER_BUDGET_SECONDS
    row = outcome(world)
    assert row["state"] == "unfinished"
    assert marker_status(world)["state"] == "timeout"
    assert len(repush_turns(world)) == 1


def test_a_failed_handover_still_posts_an_unfinished_outcome(world):
    dispatch(world)
    world.state["agent"] = "refusing"
    world.client.post("/handover", json={})
    row = outcome(world)
    assert row["state"] == "unfinished"
    assert any(reason.startswith("failed: ") for reason in row["unfinished_reasons"])
    assert marker_status(world)["state"] == "failed"


def test_an_unreachable_outcome_route_never_stops_the_retire(world):
    dispatch(world)
    world.client.post("/handover", json={})
    turn_id = handover_turn_id(world)
    world.state["game"] = "down"
    finish(world, turn_id)
    assert marker_status(world)["state"] == "done"
    wait_for(lambda: world.state["deleted"] == 1)


def test_a_re_push_the_agent_refuses_still_files_an_unfinished_outcome(world):
    world.state["log"] = CANDIDATE_LOG
    dispatch(world)
    world.client.post("/handover", json={})
    turn_id = handover_turn_id(world)
    world.state["agent"] = "refusing"
    finish(world, turn_id)
    row = outcome(world)
    assert row["state"] == "unfinished"
    assert row["repushed"] is True
    assert any(reason.startswith("failed: ") for reason in row["unfinished_reasons"])
    status = marker_status(world)
    assert status["state"] == "failed"
    assert status["error"]


def test_the_journal_success_results_are_gitworks():
    from magic_cards_model_api.playscripts import gitwork

    assert handover.WRITE_SUCCESS == gitwork.SUCCESS
