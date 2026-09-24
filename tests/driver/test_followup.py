"""F1: the driver's `mode: "followup"` - one bounded Opus turn that finishes
the DSL development an unfinished handover left open.

No turn 0 and no play: the driver pushes `prompt.followup_turn` at once,
waits on it for `FOLLOWUP_BUDGET_SECONDS` with one nudge, reads the results
journal, files `plays_outcome` with `followup: true`, then retires and exits.
It is never a game, so the seat-never-idle continuation never applies. A
`/handover` or `/release` mid-run (the Job deleted: the game was ended or
forgotten) is the hard stop.

The world is `tests/driver/world.py`'s fake game server and fake wrapper; the
clock the follow-up reads is injected.
"""

from __future__ import annotations

import json
import time

import pytest

from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.k8s_seats import FOLLOWUP_ACTIVE_DEADLINE_SECONDS, driver_seat_state
from magic_cards_model_api.playscripts.report import DevelopmentCandidates
from mtg_agent.driver import handover
from mtg_agent.driver.guides import NONE, Identification, parse_decklist
from mtg_agent.driver.prompt import (
    FOLLOWUP_BUDGET_SECONDS,
    FOLLOWUP_NUDGE_AFTER_SECONDS,
    FOLLOWUP_OPENING,
    HANDOVER_OPENING,
    Assignment,
    followup_turn,
)
from tests.driver.world import ADVISOR_UID, DECKLIST, OPPONENT_DECKLIST, build_world

REASONS = [
    "nudged: the handover was still running at 23 minutes",
    "the last write did not land: plays admit answered `conflict`",
]
FOLLOWUP = {
    "deck": "dimir-frog",
    "decklist": DECKLIST,
    "opponent": "alice",
    "opponent_decklist": OPPONENT_DECKLIST,
    "opponent_identification": None,
    "outcome": "bob won",
    "run_table": "RUN-TABLE: fetch-surveil-land ran 3 times",
    "offers": "OFFER-TABLE: fetch-surveil-land declined 2",
    "hand_played": "HAND-TABLE: cast Psychic Frog x3 candidate",
    "candidates": {"plays": ["fetch-surveil-land"], "lines": ["cast Psychic Frog"]},
    "unfinished_reasons": REASONS,
}


class Clock:
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


def dispatch(world, *, role: str = "seat", uid: str | None = None, **followup) -> dict:
    body = {
        "uid": uid or world.uid,
        "game_id": world.game_id,
        "player": "bob",
        "api_base": "http://game:8000",
        "role": role,
        "agent": AgentSettings.review_default().to_wire(),
        "mode": "followup",
        "followup": FOLLOWUP | followup,
    }
    response = world.client.post("/dispatch", json=body)
    assert response.status_code == 202, response.text
    return response.json()


def journal(world, *entries: dict, role: str = "seat") -> None:
    world.journal.parent.mkdir(parents=True, exist_ok=True)
    with world.journal.open("a") as handle:
        for entry in entries:
            handle.write(json.dumps({"ts": "t", "game_id": world.game_id, "role": role} | entry))
            handle.write("\n")


def finish(world, turn_id: str = "turn-1") -> None:
    world.client.post(
        "/turn-done", json={"turnId": turn_id, "state": "complete", "finalText": "done"}
    )


def marker_status(world) -> dict:
    path = world.settings.workspace / handover.MARKER_NAME
    wait_for(path.exists)
    return json.loads(path.read_text())


def the_turn(world) -> str:
    wait_for(lambda: world.submitted)
    return world.submitted[0]["text"]


# ---- no turn 0, one follow-up turn --------------------------------------------


def test_the_followup_pushes_its_one_turn_and_no_turn_zero(world):
    answer = dispatch(world)
    text = the_turn(world)
    assert answer["mode"] == "followup"
    assert text.startswith(FOLLOWUP_OPENING)
    assert len(world.submitted) == 1
    assert not text.startswith(HANDOVER_OPENING)
    # Nothing a game needs: no scripts registered, no play briefing.
    assert world.script_puts == []
    assert "current-game.md" not in text


def test_the_followup_turn_carries_the_ledger_tables_and_the_reasons(world):
    dispatch(world)
    text = the_turn(world)
    for reason in REASONS:
        assert reason in text
    for table in ("run_table", "offers", "hand_played"):
        assert FOLLOWUP[table] in text
    assert "## Cross-game evidence" in text
    assert "mcp-seat plays no-change --deck dimir-frog --game-id g-1 --play fetch-surveil-land" in (
        text
    )
    assert '--line "cast Psychic Frog"' in text
    assert "mcp-seat plays admit --deck dimir-frog --game-id g-1" in text
    assert "mcp-seat guides propose --deck dimir-frog" in text
    assert f"git -C /guides log --oneline origin/main --grep 'Game-Id: {world.game_id}'" in text
    assert f"{int(FOLLOWUP_BUDGET_SECONDS // 60)} minutes" in text


# -- DR4: candidates the AI review already answered count as answered -------
#
# The follow-up is the same audience as the handover for this rule: it reads
# the game only through what the api scheduled it with, so it takes the
# review-answered set the same way `followup_turn` takes everything else it
# was not there to see - as an argument, not something it recomputes.


def _followup_assignment() -> Assignment:
    return Assignment(
        game_id="g-1",
        uid="secret-uid",
        player="bob",
        opponent="alice",
        api_base="http://game:8000",
        decklist=parse_decklist(DECKLIST),
        identification=Identification(NONE),
    )


def test_followup_turn_skips_a_review_answered_candidate():
    candidates = DevelopmentCandidates(plays=("fetch-surveil-land",), lines=("cast Psychic Frog",))
    text = followup_turn(
        _followup_assignment(),
        "bob won",
        REASONS,
        run_table=FOLLOWUP["run_table"],
        offers=FOLLOWUP["offers"],
        hand_played=FOLLOWUP["hand_played"],
        evidence="none",
        candidates=candidates,
        review_answered=("fetch-surveil-land",),
    )
    flat = " ".join(text.split())
    assert "already answered by this game's AI review" in flat
    assert "--play fetch-surveil-land" not in flat
    assert "cast Psychic Frog" in flat


def test_followup_turn_review_answered_defaults_to_none():
    candidates = DevelopmentCandidates(plays=("fetch-surveil-land",))
    text = followup_turn(
        _followup_assignment(),
        "bob won",
        REASONS,
        run_table=FOLLOWUP["run_table"],
        offers=FOLLOWUP["offers"],
        hand_played=FOLLOWUP["hand_played"],
        evidence="none",
        candidates=candidates,
    )
    flat = " ".join(text.split())
    assert "already answered by this game's AI review" not in flat
    assert "--play fetch-surveil-land" in flat


def test_the_followup_dispatch_skips_a_review_answered_candidate(world):
    """DR4 item 5, wired end to end: `assign_followup` must itself read this
    game's `role: "reviewer"` journal lines and pass
    `handover.review_answered_plays` through to `followup_turn` - not just
    the prompt function tested directly above, which always defaulted it to
    empty and so could never catch app.py failing to wire it through."""
    journal(
        world,
        {"command": "plays admit", "result": "admitted", "added": ["fetch-surveil-land"]},
        role="reviewer",
    )
    dispatch(world)
    text = the_turn(world)
    assert "already answered by this game's AI review" in text
    assert "--play fetch-surveil-land" not in text
    assert "cast Psychic Frog" in text


def test_the_followup_reads_the_evidence_table_off_the_guides_checkout(world):
    evidence = world.settings.guides_root / "decks" / "dimir-frog" / "plays" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "g-0.json").write_text(
        json.dumps(
            {
                "game_id": "g-0",
                "deck": "dimir-frog",
                "opponent_archetype": None,
                "plays": {
                    "fetch-surveil-land": {
                        "offered": 3,
                        "taken": 1,
                        "passed_over": 1,
                        "declined": 1,
                    }
                },
            }
        )
    )
    dispatch(world)
    cross_game = the_turn(world).split("## Cross-game evidence", 1)[1]
    assert "fetch-surveil-land" in cross_game


def test_the_opponent_is_identified_from_the_revealed_list(world):
    """`OPPONENT_DECKLIST` matches no guide: the follow-up is told to write a
    stub for it, exactly as the handover would have been."""
    dispatch(world)
    assert "Opponent's deck: no guide" in the_turn(world)


def test_a_recorded_identification_stands_in_for_a_missing_list(world):
    dispatch(
        world,
        opponent_decklist=None,
        opponent_identification={
            "slug": "dimir-frog",
            "archetype": "dimir frog",
            "confidence": "confident",
        },
    )
    text = the_turn(world)
    assert "Opponent's deck: `dimir-frog`" in text
    assert "mirror: one update" in text


def test_the_followup_runs_on_the_opus_settings_it_was_given(world):
    dispatch(world)
    the_turn(world)
    assert world.session_posts[-1]["model"] == AgentSettings.review_default().model


# ---- the outcome, the retire, and never a continuation --------------------------


def test_a_finished_followup_files_its_outcome_retires_and_never_continues(world):
    world.state["winner"] = None  # a game that reads as running must not matter
    dispatch(world)
    the_turn(world)
    journal(
        world, {"command": "plays admit", "result": "admitted", "sha": "s1", "dsl_change": True}
    )

    finish(world)

    status = marker_status(world)
    assert status["state"] == "done" and status["playsOutcome"] == "complete"
    wait_for(lambda: world.state["deleted"] == 1)
    [row] = world.plays_outcomes
    assert row["followup"] is True
    assert row["state"] == "complete"
    assert row["uid"] == world.uid and row["player"] == "bob" and row["role"] == "seat"
    assert row["admitted"] == [
        {"sha": "s1", "dsl_change": True, "added": [], "updated": [], "reprioritized": []}
    ]
    assert row["repushed"] is False
    assert row["deck"] == "dimir-frog"
    assert len(world.submitted) == 1, "no re-push and no continuation turn"
    assert world.events.index("plays_outcome") < world.events.index("delete")


def test_an_open_development_step_is_filed_unfinished_without_a_repush(world):
    dispatch(world)
    the_turn(world)
    finish(world)
    wait_for(lambda: world.plays_outcomes)
    [row] = world.plays_outcomes
    assert row["state"] == "unfinished" and row["followup"] is True
    assert any("development still open" in reason for reason in row["unfinished_reasons"])
    assert len(world.submitted) == 1


def test_a_candidate_answered_by_no_change_closes_the_step(world):
    dispatch(world)
    the_turn(world)
    journal(
        world,
        {"command": "no_change", "play": "fetch-surveil-land", "reason": "declined once"},
        {"command": "no_change", "line": "cast Psychic Frog", "reason": "one-off line"},
    )
    finish(world)
    wait_for(lambda: world.plays_outcomes)
    assert world.plays_outcomes[0]["state"] == "complete"


def test_the_followup_is_nudged_once_then_bounded_by_its_budget(world, clock):
    dispatch(world)
    the_turn(world)
    clock.now = FOLLOWUP_NUDGE_AFTER_SECONDS + 1
    wait_for(lambda: world.probes)
    assert "push now" in world.probes[0]["text"]
    clock.now = FOLLOWUP_BUDGET_SECONDS + 1

    status = marker_status(world)

    assert status["state"] == "timeout" and status["nudged"] is True
    assert len(world.probes) == 1
    wait_for(lambda: world.plays_outcomes)
    reasons = world.plays_outcomes[0]["unfinished_reasons"]
    assert any(reason.startswith("timeout") for reason in reasons)
    assert any(reason.startswith("nudged") for reason in reasons)
    wait_for(lambda: world.state["deleted"] == 1)


def test_the_budget_fits_inside_the_jobs_deadline():
    """Pod boot and the agent's own boot (up to two minutes each) come off
    the Job's `activeDeadlineSeconds` before the turn starts."""
    assert FOLLOWUP_NUDGE_AFTER_SECONDS < FOLLOWUP_BUDGET_SECONDS
    assert FOLLOWUP_BUDGET_SECONDS + 600 <= FOLLOWUP_ACTIVE_DEADLINE_SECONDS


def test_the_status_never_reads_stalled_while_the_followup_runs(world, clock):
    dispatch(world)
    the_turn(world)
    running = world.client.get("/status").json()
    assert driver_seat_state(running) == "thinking"
    finish(world)
    marker_status(world)
    clock.now = 10_000.0
    assert driver_seat_state(world.client.get("/status").json()) in ("idle", "thinking")


def test_a_turn_done_after_the_outcome_pushes_nothing(world):
    dispatch(world)
    the_turn(world)
    finish(world)
    wait_for(lambda: world.state["deleted"] == 1)
    finish(world, "turn-9")
    assert len(world.submitted) == 1


# ---- the hard stop -----------------------------------------------------------------


@pytest.mark.parametrize("route", ["/handover", "/release"])
def test_a_deleted_job_stops_the_followup_at_once(world, route):
    dispatch(world)
    the_turn(world)

    answer = world.client.post(route, json={})
    assert answer.status_code == 202

    status = marker_status(world)
    assert status["state"] == "stopped"
    assert world.interrupts, "the running turn is interrupted"
    wait_for(lambda: world.state["deleted"] == 1)
    wait_for(lambda: world.plays_outcomes)
    [row] = world.plays_outcomes
    assert row["followup"] is True and row["state"] == "unfinished"
    assert any(reason.startswith("stopped") for reason in row["unfinished_reasons"])
    assert len(world.submitted) == 1, "no handover turn is pushed for a follow-up"


# ---- an advisor's follow-up ---------------------------------------------------------


def test_an_advisors_followup_reads_and_files_under_its_own_role(world):
    dispatch(world, role="advisor", uid=ADVISOR_UID)
    the_turn(world)
    journal(
        world,
        {"command": "plays admit", "result": "admitted", "sha": "s9", "dsl_change": True},
        role="advisor",
    )
    finish(world)
    wait_for(lambda: world.plays_outcomes)
    [row] = world.plays_outcomes
    assert (row["uid"], row["role"], row["state"]) == (ADVISOR_UID, "advisor", "complete")


# ---- the handover's own outcome row names the deck and the opponent -----------------


def test_the_outcome_body_carries_the_deck_and_the_opponent():
    verdict = handover.assess([], handover_candidates())
    body = handover.plays_outcome_body(
        uid="u",
        player="bob",
        role="seat",
        verdict=verdict,
        reasons=["x"],
        repushed=False,
        deck="dimir-frog",
        opponent={"slug": None, "archetype": "izzet prowess", "confidence": "low"},
    )
    assert body["deck"] == "dimir-frog"
    assert body["opponent"] == {"slug": None, "archetype": "izzet prowess", "confidence": "low"}
    assert body["followup"] is False
    followup = handover.plays_outcome_body(
        uid="u",
        player="bob",
        role="seat",
        verdict=verdict,
        reasons=[],
        repushed=False,
        followup=True,
    )
    assert followup["followup"] is True and followup["state"] == "complete"
    assert "deck" not in followup and "opponent" not in followup


def handover_candidates():
    from magic_cards_model_api.playscripts.report import DevelopmentCandidates

    return DevelopmentCandidates()


def test_the_handovers_own_outcome_row_names_the_deck_and_the_revealed_opponent(world):
    """What the api builds the follow-up's assignment from: the row the
    handover files carries the seat's deck and the opponent the driver
    identified, so the follow-up gets the same identification even once
    the game is archived."""
    response = world.client.post(
        "/dispatch",
        json={
            "uid": world.uid,
            "game_id": world.game_id,
            "player": "bob",
            "api_base": "http://game:8000",
        },
    )
    assert response.status_code == 202, response.text
    world.state["winner"] = "bob"
    world.client.post("/handover", json={})
    wait_for(lambda: len(world.submitted) >= 2)
    finish(world, f"turn-{len(world.submitted)}")
    wait_for(lambda: world.plays_outcomes)
    row = world.plays_outcomes[0]
    assert row["deck"] == "dimir-frog"
    assert row["opponent"] == {"slug": None, "archetype": None, "confidence": "none"}
    assert row["followup"] is False
