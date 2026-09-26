"""FD7 gap 15 (parity item 15), completed: `mode: "review"` is a real third
`DispatchIn.mode` that declares the DR3 post-game AI review for a seat that
never played this game through a driver of its own - a human seat, whose
only path here is `k8s_seats.build_review_job` / `AiSeatRegistry.
start_review_followup`.

Before this, the review Job dispatched `mode: "followup"`, which runs
`prompt.followup_turn` - a bounded turn that finishes a handover's own
unfinished DSL development. A human seat has no handover and nothing
registered for that turn to redo, so the Job ran and retired having written
nothing resembling a narrative review (`k8s_seats.build_review_job`'s own
docstring, before this change, said as much). `mode: "review"` instead:

- reads the seat's own decklist straight from the lobby
  (`seat_decklist`/`GET /api/lobby/games/{id}/seats`), the same best-effort
  lookup a normal dispatch uses, rather than from any follow-up payload -
  there is no ledger entry recording a driver dispatch for a human uid to
  build one from;
- pushes no turn at all - a human played the match, not this agent, so
  there is no turn 0 and no reflection to write;
- fires `consider_ai_review` with an empty reflection text (the same
  already-handled shape `retire`'s own bare `path="retire"` call uses for a
  game that ended before its reflection turn ran) and then `retire`, which
  generates the review over the real DR3 path (`post_ai_review`: the review
  record, the wrapper's `/v1/review`, `POST .../ai-review`) and tears the
  agent container back down.

The world is `tests/driver/world.py`'s fake game server and fake wrapper.
"""

from __future__ import annotations

import time

import pytest

from magic_cards_model_api.agent_settings import AgentSettings
from tests.driver.world import build_world


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition never became true")


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path)


def dispatch_review(world, **overrides) -> dict:
    body = {
        "uid": world.uid,
        "game_id": world.game_id,
        "player": "bob",
        "api_base": "http://game:8000",
        "role": "seat",
        "agent": AgentSettings.review_default().to_wire(),
        "mode": "review",
    } | overrides
    response = world.client.post("/dispatch", json=body)
    assert response.status_code == 202, response.text
    return response.json()


def test_review_mode_is_accepted_and_answers_its_own_shape(world) -> None:
    answer = dispatch_review(world)
    assert answer["mode"] == "review"
    # The lobby's own decklist for this uid, `DECKLIST` in `world.py`,
    # satisfies the `dimir-frog` guide's signature - proof the deck came
    # from `seat_decklist`, never from an empty/unset payload.
    assert answer["deck"] == "dimir-frog"


def test_review_mode_declares_the_dr3_review_without_any_turn(world) -> None:
    dispatch_review(world)
    wait_for(lambda: world.ai_review_posts)
    assert world.review_requests, "the wrapper's /v1/review was never asked"
    posted = world.ai_review_posts[0]
    assert posted["game_id"] == world.game_id
    assert posted["player"] == "bob"
    assert posted["text"]
    # No play turn: a human played this match, not this agent.
    assert world.submitted == []
    assert "message" not in world.events


def test_review_mode_retires_the_agent_once_the_review_is_filed(world) -> None:
    dispatch_review(world)
    wait_for(lambda: world.state["deleted"] >= 1)
    status = world.client.get("/status").json()
    assert status["gameId"] is None


def test_review_mode_reads_the_lobby_not_a_followup_payload(world) -> None:
    """A body with no `followup` key at all still works: the deck comes
    from the lobby, not from `DispatchIn.followup`."""
    answer = dispatch_review(world)
    assert answer["deck"] == "dimir-frog"
    assert world.seat_reads, "seat_decklist never read the lobby"


def test_review_mode_rejects_a_rival_game_while_still_current(world) -> None:
    """One container, one job: a rival game dispatched before this one's
    review has retired gets the normal 409, the same shape
    `assign_followup` gives - proven by holding the wrapper's `/v1/review`
    open with a delay so the assignment is still current when the second
    dispatch lands."""
    world.state["wrapper_review_delay"] = 0.3
    dispatch_review(world)
    response = world.client.post(
        "/dispatch",
        json={
            "uid": "other-uid",
            "game_id": "g-2",
            "player": "carol",
            "api_base": "http://game:8000",
            "role": "seat",
            "agent": AgentSettings.review_default().to_wire(),
            "mode": "review",
        },
    )
    assert response.status_code == 409
    assert "already working on game" in response.json()["detail"]
