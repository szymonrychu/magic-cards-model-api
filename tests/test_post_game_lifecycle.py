"""Natural game over: the AI finishes its post-game work, then releases itself.

A game that ends on its own (`lobby.retire_game`, from
`app._record_game_over`) stops its pregame watcher and nothing else. Every
seat and advisor uid stays valid and every runner keeps running, so the
agent's reflection turn, its Opus review and its handover all run against a
live credential. The driver ends its own Job afterwards (`run_handover` ->
`retire` -> `exit_after_match`).

The backstop is bounded: `lobby.POSTGAME_RELEASE_SECONDS` after the
`game_over` row, a runner still finishing is hard-released once, by a daemon
timer armed at retire and by a lazy check on the lobby's own read
(`lobby.post_game_work`). End game (`reset_game`) stays the hard stop.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from magic_cards_model_api import ai_pregame, ai_seat, audit, k8s_seats, lobby
from tests.test_k8s_seats import ENV, FakeBatch, FakeCore, FakeDriverHttp

LEGAL_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
LEGAL_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"
SLEEPER = ["python3", "-c", "import time; time.sleep(60)"]


@pytest.fixture()
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> ai_seat.AiSeatRegistry:
    registry = ai_seat.AiSeatRegistry()
    monkeypatch.setattr(ai_seat, "registry", registry)
    return registry


@pytest.fixture()
def subprocess_seats(monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry):
    """Subprocess mode with a real, long-lived stand-in for `claude`: every AI
    seat and buddy the lobby starts is a process that is still running when
    the game ends, which is the shape of an agent mid-reflection."""
    monkeypatch.setenv("AI_SEAT_MODE", "subprocess")
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(ai_seat, "command_for", lambda prompt, agent: SLEEPER)
    yield fresh_registry
    for runner in list(fresh_registry._runners.values()):
        runner.stop()


def wait_until(predicate: Callable[[], bool], *, max_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "condition never became true within the bound"


def boot(client: TestClient, *, buddy: bool = False, seed: int = 21) -> dict[str, Any]:
    alice: dict[str, Any] = {"username": "alice", "decklist_text": LEGAL_DECK, "ai": False}
    if buddy:
        alice["buddy"] = True
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [alice, {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": True}],
            "seed": seed,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def start_bob(client: TestClient, booted: dict[str, Any]) -> ai_seat.AiSeatRunner:
    uid = booted["seat_uids"]["bob"]
    client.post(f"/api/lobby/games/{booted['game_id']}/ai-seat/start", json={"uid": uid})
    runner = ai_seat.registry._runners[uid]
    wait_until(runner.running)
    return runner


def concede(client: TestClient, booted: dict[str, Any]) -> None:
    response = client.post(
        f"/api/games/{booted['game_id']}/actions/concede",
        params={"uid": booted["seat_uids"]["alice"]},
        json={"player": "alice"},
    )
    assert response.status_code == 200, response.text


def retirements(game_id: str) -> list[dict[str, Any]]:
    return [e["detail"] for e in audit.entries(game_id) if e["action"] == lobby.RETIRE_ACTION]


# ---------------------------------------------------------------------------
# (a) retire_game stops the watcher and nothing else
# ---------------------------------------------------------------------------


def test_a_natural_game_over_leaves_the_ai_seat_running(
    client: TestClient, subprocess_seats: ai_seat.AiSeatRegistry
) -> None:
    booted = boot(client)
    runner = start_bob(client, booted)

    concede(client, booted)

    assert not ai_pregame.is_watching(booted["game_id"])
    assert runner.running()
    assert subprocess_seats.status(booted["seat_uids"]["bob"]) is not None
    [retired] = retirements(booted["game_id"])
    assert retired["ai_seats_released"] == []
    assert set(retired) >= {
        "reason",
        "note",
        "seats_invalidated",
        "advisors_invalidated",
        "watcher_stopped",
        "ai_seats_released",
    }
    assert "bob:subprocess" in retired["note"], retired["note"]


def test_a_natural_game_over_leaves_the_buddy_running_too(
    client: TestClient, subprocess_seats: ai_seat.AiSeatRegistry
) -> None:
    """Advisors hand over as well, so they get the same treatment."""
    booted = boot(client, buddy=True)
    advisor = booted["advisor_uids"]["alice"]
    buddy = subprocess_seats._runners[advisor]
    wait_until(buddy.running)

    concede(client, booted)

    assert buddy.running()
    assert lobby.post_game_work(booted["game_id"]) is True
    assert "alice:subprocess" in retirements(booted["game_id"])[0]["note"]


def test_a_natural_game_over_starts_the_human_seats_review_job_in_kubernetes_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    """FD7 gap 15 wiring: `retire_game` is the one place every natural
    ending funnels through, so it is what starts a human seat's own DR3
    review Job - kubernetes mode only, and never for a seat the registry
    already holds a runner for (bob is never started here, but the
    registry is made to hold one for him anyway, standing in for an AI
    seat that was)."""
    monkeypatch.setenv("AI_SEAT_MODE", "kubernetes")
    batch = FakeBatch()
    fake_seats = k8s_seats.K8sSeats(
        k8s_seats.K8sSettings.from_env(ENV), batch, FakeCore(), FakeDriverHttp({})
    )
    monkeypatch.setattr(k8s_seats, "get", lambda: fake_seats)
    booted = boot(client)
    assert list(batch.jobs) == [k8s_seats.job_name(booted["game_id"], booted["seat_uids"]["bob"])]
    bob = booted["seat_uids"]["bob"]
    fresh_registry._dispatched[bob] = {"mechanism": "kubernetes"}
    calls: list[tuple[str, str, str]] = []

    def fake_start_review_followup(
        *, uid: str, game_id: str, player: str, review_key: str
    ) -> dict[str, Any]:
        assert review_key
        calls.append((uid, game_id, player))
        return {"uid": uid, "mechanism": "kubernetes", "state": "scheduled"}

    monkeypatch.setattr(fresh_registry, "start_review_followup", fake_start_review_followup)

    concede(client, booted)

    assert calls == [(booted["seat_uids"]["alice"], booted["game_id"], "alice")]
    [retired] = retirements(booted["game_id"])
    assert retired["human_seat_reviews"] == [
        {
            "uid": booted["seat_uids"]["alice"],
            "mechanism": "kubernetes",
            "state": "scheduled",
            "player": "alice",
        }
    ]
    assert "started human-seat review job(s) [alice]" in retired["note"]


def test_a_natural_game_over_never_starts_a_human_review_outside_kubernetes_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    """Compose (and every other non-kubernetes mode) has no follow-up
    mechanism at all, for a human seat or an AI one."""
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        fresh_registry,
        "start_review_followup",
        lambda **kwargs: calls.append(kwargs) or {},
    )
    booted = boot(client)

    concede(client, booted)

    assert calls == []
    [retired] = retirements(booted["game_id"])
    assert retired["human_seat_reviews"] == []


def test_the_seat_uids_stay_valid_after_a_natural_game_over(client: TestClient) -> None:
    booted = boot(client)
    concede(client, booted)
    bob = booted["seat_uids"]["bob"]
    board = client.get(f"/api/games/{booted['game_id']}", params={"uid": bob})
    assert board.status_code == 200, board.text


def test_reflection_and_ai_review_with_the_seat_uid_are_accepted_after_game_over(
    client: TestClient,
) -> None:
    booted = boot(client)
    game_id, bob = booted["game_id"], booted["seat_uids"]["bob"]
    concede(client, booted)

    reflected = client.post(
        f"/api/games/{game_id}/reflection",
        json={"player": "bob", "uid": bob, "text": "I won on a concession."},
    )
    assert reflected.status_code == 200, reflected.text
    reviewed = client.post(
        f"/api/games/{game_id}/ai-review",
        json={"player": "bob", "uid": bob, "text": "Bob kept a fine seven."},
    )
    assert reviewed.status_code == 200, reviewed.text


# ---------------------------------------------------------------------------
# (b) End game is still the hard stop
# ---------------------------------------------------------------------------


def test_end_game_after_game_over_still_releases_and_deletes(
    client: TestClient, subprocess_seats: ai_seat.AiSeatRegistry
) -> None:
    booted = boot(client)
    runner = start_bob(client, booted)
    concede(client, booted)

    ended = client.delete(
        f"/api/lobby/games/{booted['game_id']}", params={"uid": booted["seat_uids"]["alice"]}
    )

    assert ended.status_code == 200, ended.text
    assert ended.json()["deleted"] is True
    wait_until(lambda: not runner.running())
    assert subprocess_seats.status(booted["seat_uids"]["bob"]) is None
    assert booted["game_id"] not in lobby._release_timers


# ---------------------------------------------------------------------------
# (c) AiSeatRegistry.finishing
# ---------------------------------------------------------------------------


class _FakeK8s:
    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def status(self, uid: str) -> dict[str, Any] | None:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"state": "pending"}, True),
        ({"state": "idle"}, True),
        ({"state": "thinking"}, True),
        ({"state": "gone"}, False),
        ({"state": "error"}, False),
        (None, False),
        (RuntimeError("the cluster is down"), False),
    ],
)
def test_a_kubernetes_seat_is_finishing_while_its_pod_is_alive(
    monkeypatch: pytest.MonkeyPatch, answer: Any, expected: bool
) -> None:
    registry = ai_seat.AiSeatRegistry()
    registry._dispatched["u1"] = {"uid": "u1", "game_id": "g1", "mechanism": "kubernetes"}
    monkeypatch.setattr(k8s_seats, "get", lambda: _FakeK8s(answer))
    assert registry.finishing("u1") is expected


def _driver_answering(monkeypatch: pytest.MonkeyPatch, handler: Callable) -> None:
    def client_for(base_url: str) -> httpx.Client:
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(ai_seat, "_driver_client", client_for)


@pytest.mark.parametrize(("game_id", "expected"), [("g1", True), ("g2", False), (None, False)])
def test_a_wrapper_seat_is_finishing_while_the_driver_still_holds_that_game(
    monkeypatch: pytest.MonkeyPatch, game_id: str | None, expected: bool
) -> None:
    registry = ai_seat.AiSeatRegistry()
    registry._dispatched["u1"] = {
        "uid": "u1",
        "game_id": "g1",
        "mechanism": "wrapper",
        "driver": "http://driver:8090",
    }
    _driver_answering(monkeypatch, lambda request: httpx.Response(200, json={"gameId": game_id}))
    assert registry.finishing("u1") is expected


def test_an_unreachable_driver_is_not_finishing(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ai_seat.AiSeatRegistry()
    registry._dispatched["u1"] = {"uid": "u1", "game_id": "g1", "mechanism": "wrapper"}

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to the driver")

    _driver_answering(monkeypatch, refuse)
    assert registry.finishing("u1") is False


@pytest.mark.parametrize("running", [True, False])
def test_a_subprocess_seat_is_finishing_while_its_process_runs(running: bool) -> None:
    registry = ai_seat.AiSeatRegistry()
    registry._runners["u1"] = SimpleNamespace(running=lambda: running)
    assert registry.finishing("u1") is running


def test_a_uid_nothing_was_started_for_is_not_finishing() -> None:
    assert ai_seat.AiSeatRegistry().finishing("nobody") is False


# ---------------------------------------------------------------------------
# (d) post_game_work and the bounded backstop
# ---------------------------------------------------------------------------


def test_post_game_work_is_true_while_finishing_and_false_after_release(
    client: TestClient, subprocess_seats: ai_seat.AiSeatRegistry
) -> None:
    booted = boot(client)
    game_id = booted["game_id"]
    runner = start_bob(client, booted)
    assert lobby.post_game_work(game_id) is False, "a game in progress has no post-game work"

    concede(client, booted)
    assert lobby.post_game_work(game_id) is True

    runner.stop()
    wait_until(lambda: not runner.running())
    assert lobby.post_game_work(game_id) is False


def test_the_overdue_release_fires_once_after_the_bound(
    client: TestClient,
    subprocess_seats: ai_seat.AiSeatRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    booted = boot(client)
    game_id = booted["game_id"]
    runner = start_bob(client, booted)
    concede(client, booted)
    real = time.time()

    monkeypatch.setattr(lobby, "clock", lambda: real + lobby.POSTGAME_RELEASE_SECONDS - 5)
    assert lobby.post_game_work(game_id) is True
    assert runner.running()

    monkeypatch.setattr(lobby, "clock", lambda: real + lobby.POSTGAME_RELEASE_SECONDS + 5)
    assert lobby.post_game_work(game_id) is False
    wait_until(lambda: not runner.running())
    assert lobby.post_game_work(game_id) is False
    assert lobby.release_overdue(game_id) is False

    overran = [r for r in retirements(game_id) if r["reason"].startswith("post-game work overran")]
    assert len(overran) == 1, retirements(game_id)
    assert overran[0]["reason"] == f"post-game work overran {int(lobby.POSTGAME_RELEASE_SECONDS)}s"
    assert [entry["player"] for entry in overran[0]["ai_seats_released"]] == ["bob"]


def test_retire_arms_a_daemon_timer_that_releases_an_overrunning_seat(
    client: TestClient,
    subprocess_seats: ai_seat.AiSeatRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lobby, "POSTGAME_RELEASE_SECONDS", 0.3)
    booted = boot(client)
    runner = start_bob(client, booted)

    concede(client, booted)
    timer = lobby._release_timers[booted["game_id"]]
    assert isinstance(timer, threading.Timer)
    assert timer.daemon

    wait_until(lambda: not runner.running())
    wait_until(lambda: booted["game_id"] not in lobby._release_timers)
    overran = [
        r
        for r in retirements(booted["game_id"])
        if r["reason"].startswith("post-game work overran")
    ]
    assert len(overran) == 1


def test_the_bound_is_derived_from_the_drivers_own_allowances() -> None:
    """The api cannot import the driver at runtime (they ship as separate
    images), so the numbers are mirrored and pinned here instead."""
    from mtg_agent.driver.app import RETIRE_REVIEW_JOIN_TIMEOUT
    from mtg_agent.driver.handover import HANDOVER_BUDGET_SECONDS

    assert lobby.DRIVER_HANDOVER_BUDGET_SECONDS == HANDOVER_BUDGET_SECONDS
    assert lobby.DRIVER_RETIRE_REVIEW_JOIN_TIMEOUT == RETIRE_REVIEW_JOIN_TIMEOUT
    assert lobby.POSTGAME_RELEASE_SECONDS == (
        lobby.DRIVER_REFLECTION_ALLOWANCE_SECONDS
        + HANDOVER_BUDGET_SECONDS
        + RETIRE_REVIEW_JOIN_TIMEOUT
    )
    # Review fix, round 1: `RETIRE_REVIEW_JOIN_TIMEOUT` moved from 3730s to
    # 5620s (decision-count aware now, up to its own new ceiling), so the
    # sanity range this pins moves with it (was 5000-7000, about 1h40m).
    assert 7000 <= lobby.POSTGAME_RELEASE_SECONDS <= 9000
    assert lobby.POSTGAME_RELEASE_SECONDS < 14400, "inside the Job's activeDeadlineSeconds"
