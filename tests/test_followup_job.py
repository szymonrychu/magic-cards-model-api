"""F1: the Opus follow-up DSL-development Job for an unfinished handover.

A seat's (or an advisor's) handover files one `plays_outcome` row. When that
row says `unfinished` and is not itself a follow-up's, the api schedules ONE
follow-up Job for that (game, uid) in kubernetes mode: the original uid's own
credential, `AgentSettings.review_default()` (Opus), an assignment built
server-side from the ledger at schedule time, and its own bounded deadline.
Deduped by the Job's label and by the ledger. Every other mode records
`plays_outcome_followup` as `unavailable`.

While the follow-up runs the lobby row stays "recording plays..."
(`lobby.post_game_work`), bounded by the follow-up's own window; End game
and Forget stay the hard stop (the Job is released, or reconciled away).

The cluster is `tests/test_k8s_seats.py`'s hand-rolled Kubernetes client
stand-in; everything the api does over it runs for real.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from magic_cards_model_api import ai_seat, audit, k8s_seats, lobby
from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.k8s_seats import K8sSeats, K8sSettings
from magic_cards_model_api.playscripts.report import (
    hand_played_summary,
    offer_table,
    script_run_table,
)
from magic_cards_model_api.store import store
from tests.test_k8s_seats import ENV, FakeBatch, FakeCore, FakeDriverHttp

LEGAL_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
LEGAL_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"
REASONS = ["nudged: the handover was still running at 23 minutes"]
ROUTE = "/api/games/{game_id}/plays-outcome"


@pytest.fixture()
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> ai_seat.AiSeatRegistry:
    registry = ai_seat.AiSeatRegistry()
    monkeypatch.setattr(ai_seat, "registry", registry)
    return registry


@pytest.fixture()
def cluster(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> Iterator[SimpleNamespace]:
    """Kubernetes mode over the fake cluster. The seat's own pod reads as
    finished (`Succeeded`), so the only post-game work left is whatever
    follow-up the test schedules."""
    monkeypatch.setenv("AI_SEAT_MODE", "kubernetes")
    batch, core = FakeBatch(), FakeCore(phase="Succeeded")
    http = FakeDriverHttp({"turnActive": False, "pending": False, "lastError": None})
    seats = K8sSeats(K8sSettings.from_env(ENV), batch, core, http, reconcile_interval=0)
    k8s_seats.set_for_tests(seats)
    try:
        yield SimpleNamespace(seats=seats, batch=batch, core=core, registry=fresh_registry)
    finally:
        # Ended here, while the fake cluster is still the one `get()` hands
        # out: the `client` fixture's own cleanup runs after this one.
        for game_id in store.ui_games():
            lobby.reset_game(game_id, reason="test cleanup")
        k8s_seats.set_for_tests(None)


def boot(client: TestClient, *, buddy: bool = False) -> dict[str, Any]:
    alice: dict[str, Any] = {"username": "alice", "decklist_text": LEGAL_DECK, "ai": False}
    if buddy:
        alice["buddy"] = True
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [alice, {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": True}],
            "seed": 21,
        },
    )
    assert response.status_code == 200, response.text
    booted = response.json()
    client.post(
        f"/api/lobby/games/{booted['game_id']}/ai-seat/start",
        json={"uid": booted["seat_uids"]["bob"]},
    )
    conceded = client.post(
        f"/api/games/{booted['game_id']}/actions/concede",
        params={"uid": booted["seat_uids"]["alice"]},
        json={"player": "alice"},
    )
    assert conceded.status_code == 200, conceded.text
    return booted


def outcome(
    client: TestClient,
    booted: dict[str, Any],
    *,
    uid: str | None = None,
    player: str = "bob",
    role: str = "seat",
    **overrides: Any,
):
    body = {
        "uid": uid or booted["seat_uids"][player],
        "player": player,
        "role": role,
        "state": "unfinished",
        "unfinished_reasons": REASONS,
        "deck": "dimir-frog",
        "opponent": {"slug": "prowess", "archetype": "izzet prowess", "confidence": "confident"},
        **overrides,
    }
    response = client.post(ROUTE.format(game_id=booted["game_id"]), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def followup_jobs(batch: FakeBatch) -> dict[str, Any]:
    return {name: job for name, job in batch.jobs.items() if name.startswith("mtg-followup-")}


def followup_rows(game_id: str) -> list[dict[str, Any]]:
    return [e["detail"] for e in audit.entries(game_id) if e["action"] == lobby.FOLLOWUP_ACTION]


def assignment_of(batch: FakeBatch, name: str) -> dict[str, Any]:
    return json.loads(batch.bodies[name]["driver_env"]["DRIVER_ASSIGNMENT_JSON"])


@pytest.fixture()
def recorded_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every Job body the fake cluster was asked to create, so a test
    can read the assignment the driver would self-dispatch from."""
    original = FakeBatch.create_namespaced_job

    def create(self: FakeBatch, namespace: str, body: dict) -> Any:
        created = original(self, namespace, body)
        pod = body["spec"]["template"]["spec"]
        driver = next(c for c in pod["containers"] if c["name"] == "driver")
        agent = next(c for c in pod["containers"] if c["name"] == "agent")
        bodies = self.__dict__.setdefault("bodies", {})
        bodies[body["metadata"]["name"]] = {
            "body": body,
            "driver_env": {e["name"]: e.get("value") for e in driver["env"]},
            "agent_env": {e["name"]: e.get("value") for e in agent["env"]},
        }
        return created

    monkeypatch.setattr(FakeBatch, "create_namespaced_job", create)


# ---------------------------------------------------------------------------
# (a) scheduling: once per (game, uid), never for `complete` or a follow-up's
# ---------------------------------------------------------------------------


def test_the_action_is_named_plays_outcome_followup() -> None:
    assert lobby.FOLLOWUP_ACTION == audit.PLAYS_OUTCOME_FOLLOWUP_ACTION
    assert audit.PLAYS_OUTCOME_FOLLOWUP_ACTION == "plays_outcome_followup"


def test_the_eval_report_never_grades_the_followup_row_as_play() -> None:
    """It names a player, so an unclassified row would be read as that
    seat's move."""
    from scripts import eval_report

    assert audit.PLAYS_OUTCOME_FOLLOWUP_ACTION in eval_report.NOT_PLAY


@pytest.mark.usefixtures("recorded_bodies")
def test_an_unfinished_outcome_schedules_one_opus_followup_job(
    client: TestClient, cluster: SimpleNamespace
) -> None:
    booted = boot(client)
    game_id, bob = booted["game_id"], booted["seat_uids"]["bob"]

    answer = outcome(client, booted)

    assert answer["followup"] == "scheduled"
    [name] = followup_jobs(cluster.batch)
    assert name == k8s_seats.followup_job_name(game_id, bob)
    created = cluster.batch.bodies[name]
    assert created["agent_env"]["MODEL"] == AgentSettings.review_default().model
    assert created["body"]["spec"]["activeDeadlineSeconds"] == (
        k8s_seats.FOLLOWUP_ACTIVE_DEADLINE_SECONDS
    )
    [row] = followup_rows(game_id)
    assert (row["player"], row["role"], row["state"], row["job"]) == (
        "bob",
        "seat",
        "scheduled",
        name,
    )
    assert "uid" not in row


@pytest.mark.usefixtures("recorded_bodies")
def test_the_followup_acts_only_as_the_original_seat_through_its_uid(
    client: TestClient, cluster: SimpleNamespace
) -> None:
    booted = boot(client)
    outcome(client, booted)
    [name] = followup_jobs(cluster.batch)
    assignment = assignment_of(cluster.batch, name)
    assert assignment["uid"] == booted["seat_uids"]["bob"]
    assert assignment["player"] == "bob"
    assert assignment.get("role", "seat") == "seat"
    assert assignment["mode"] == "followup"
    assert assignment["agent"] == AgentSettings.review_default().to_wire()


@pytest.mark.usefixtures("recorded_bodies")
def test_the_assignment_is_built_server_side_from_the_ledger(
    client: TestClient, cluster: SimpleNamespace
) -> None:
    """Everything the follow-up needs rides in the assignment, read at
    schedule time, so a later archive costs it nothing."""
    booted = boot(client)
    game_id, bob = booted["game_id"], booted["seat_uids"]["bob"]
    outcome(client, booted)
    [name] = followup_jobs(cluster.batch)
    followup = assignment_of(cluster.batch, name)["followup"]

    rows = client.get(f"/api/games/{game_id}/log", params={"uid": bob}).json()["log"]
    assert followup["deck"] == "dimir-frog"
    assert followup["decklist"] == LEGAL_DECK_B
    assert followup["opponent"] == "alice"
    assert followup["opponent_decklist"] == LEGAL_DECK
    assert followup["opponent_identification"] == {
        "slug": "prowess",
        "archetype": "izzet prowess",
        "confidence": "confident",
    }
    assert followup["outcome"] == "bob won"
    assert followup["run_table"] == script_run_table(rows, game_id, "bob")
    assert followup["offers"] == offer_table(rows, game_id, "bob")
    assert followup["hand_played"] == hand_played_summary(rows, game_id, "bob")
    assert followup["candidates"] == {"plays": [], "lines": []}
    assert followup["unfinished_reasons"] == REASONS


def test_a_second_unfinished_outcome_schedules_nothing_more(
    client: TestClient, cluster: SimpleNamespace
) -> None:
    booted = boot(client)
    outcome(client, booted)
    # The Job finished and was TTL-reaped: the label no longer dedupes, the
    # ledger still does.
    cluster.batch.jobs.clear()

    again = outcome(client, booted)

    assert again["followup"] == "deduped"
    assert followup_jobs(cluster.batch) == {}
    assert len(followup_rows(booted["game_id"])) == 1


def test_a_complete_outcome_schedules_nothing(client: TestClient, cluster: SimpleNamespace) -> None:
    booted = boot(client)
    answer = outcome(client, booted, state="complete", unfinished_reasons=[])
    assert answer.get("followup") is None
    assert followup_jobs(cluster.batch) == {}
    assert followup_rows(booted["game_id"]) == []


def test_a_followups_own_unfinished_outcome_never_schedules_a_second(
    client: TestClient, cluster: SimpleNamespace
) -> None:
    booted = boot(client)
    answer = outcome(client, booted, followup=True)
    assert answer.get("followup") is None
    assert followup_jobs(cluster.batch) == {}
    assert followup_rows(booted["game_id"]) == []


def test_an_advisor_gets_its_own_followup_under_its_own_uid(
    client: TestClient, cluster: SimpleNamespace
) -> None:
    booted = boot(client, buddy=True)
    advisor = booted["advisor_uids"]["alice"]
    outcome(client, booted)
    outcome(client, booted, uid=advisor, player="alice", role="advisor")

    names = set(followup_jobs(cluster.batch))
    assert names == {
        k8s_seats.followup_job_name(booted["game_id"], booted["seat_uids"]["bob"]),
        k8s_seats.followup_job_name(booted["game_id"], advisor),
    }
    advisor_job = cluster.batch.jobs[k8s_seats.followup_job_name(booted["game_id"], advisor)]
    assert advisor_job.metadata.labels[k8s_seats.LABEL_ROLE] == "advisor"
    roles = {(row["player"], row["role"]) for row in followup_rows(booted["game_id"])}
    assert roles == {("bob", "seat"), ("alice", "advisor")}


# ---------------------------------------------------------------------------
# mode fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["subprocess", "wrapper"])
def test_other_modes_record_the_followup_as_unavailable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fresh_registry: ai_seat.AiSeatRegistry,
    mode: str,
) -> None:
    monkeypatch.setenv("AI_SEAT_MODE", mode)
    monkeypatch.setattr(ai_seat, "claude_available", lambda: False)
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": LEGAL_DECK, "ai": False},
                {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": True},
            ],
            "seed": 21,
        },
    )
    booted = response.json()
    client.post(
        f"/api/games/{booted['game_id']}/actions/concede",
        params={"uid": booted["seat_uids"]["alice"]},
        json={"player": "alice"},
    )
    try:
        answer = outcome(client, booted)
        assert answer["followup"] == "unavailable"
        [row] = followup_rows(booted["game_id"])
        assert (row["player"], row["role"], row["state"]) == ("bob", "seat", "unavailable")
        assert mode in row["reason"]
        assert outcome(client, booted)["followup"] == "deduped"
    finally:
        lobby.reset_game(booted["game_id"], reason="test cleanup")


# ---------------------------------------------------------------------------
# (e) post_game_work stays true while the follow-up runs, bounded
# ---------------------------------------------------------------------------


def test_post_game_work_is_true_while_the_followup_runs(
    client: TestClient, cluster: SimpleNamespace
) -> None:
    booted = boot(client)
    game_id = booted["game_id"]
    assert lobby.post_game_work(game_id) is False, "the seat's own pod already finished"

    outcome(client, booted)
    assert lobby.post_game_work(game_id) is True
    listed = client.get("/api/lobby/games").json()
    [row] = [g for g in listed["games"] if g["game_id"] == game_id]
    assert row["post_game_work"] is True

    [job] = followup_jobs(cluster.batch).values()
    job.status.succeeded = 1
    assert lobby.post_game_work(game_id) is False


def test_the_followup_extends_the_post_game_bound_and_then_is_released(
    client: TestClient, cluster: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    booted = boot(client)
    game_id = booted["game_id"]
    outcome(client, booted)
    [name] = followup_jobs(cluster.batch)
    real = time.time()

    monkeypatch.setattr(lobby, "clock", lambda: real + lobby.POSTGAME_RELEASE_SECONDS + 5)
    assert lobby.post_game_work(game_id) is True, "inside the follow-up's own window"
    assert name in cluster.batch.jobs

    monkeypatch.setattr(lobby, "clock", lambda: real + lobby.FOLLOWUP_RELEASE_SECONDS + 5)
    assert lobby.post_game_work(game_id) is False
    assert name not in cluster.batch.jobs
    overran = [
        e["detail"]
        for e in audit.entries(game_id)
        if e["action"] == lobby.RETIRE_ACTION
        and str(e["detail"].get("reason", "")).startswith("post-game work overran")
    ]
    assert len(overran) == 1


def test_the_followup_window_mirrors_the_jobs_deadline() -> None:
    assert lobby.FOLLOWUP_RELEASE_SECONDS > k8s_seats.FOLLOWUP_ACTIVE_DEADLINE_SECONDS
    assert lobby.FOLLOWUP_RELEASE_SECONDS - k8s_seats.FOLLOWUP_ACTIVE_DEADLINE_SECONDS <= 300


def test_scheduling_rearms_the_release_timer_for_the_followup_window(
    client: TestClient, cluster: SimpleNamespace
) -> None:
    booted = boot(client)
    outcome(client, booted)
    timer = lobby._release_timers[booted["game_id"]]
    assert timer.interval == lobby.FOLLOWUP_RELEASE_SECONDS


# ---------------------------------------------------------------------------
# the hard stop: End game releases it, Forget reconciles it away
# ---------------------------------------------------------------------------


def test_end_game_deletes_the_followup_job(client: TestClient, cluster: SimpleNamespace) -> None:
    booted = boot(client)
    outcome(client, booted)
    [name] = followup_jobs(cluster.batch)

    ended = client.delete(
        f"/api/lobby/games/{booted['game_id']}", params={"uid": booted["seat_uids"]["alice"]}
    )

    assert ended.status_code == 200, ended.text
    assert name in cluster.batch.deleted
    assert followup_jobs(cluster.batch) == {}


def test_forget_is_the_hard_stop_reconcile_deletes_the_followup(
    client: TestClient, cluster: SimpleNamespace
) -> None:
    booted = boot(client)
    outcome(client, booted)
    [name] = followup_jobs(cluster.batch)

    store.delete(booted["game_id"])
    report = cluster.seats.reconcile(lambda: set(store.ids()))

    assert name in report["deleted"]
    assert followup_jobs(cluster.batch) == {}


def test_the_handovers_timer_waits_out_an_open_followup_window(
    client: TestClient, cluster: SimpleNamespace
) -> None:
    """The timer `retire_game` armed fires at the handover's bound; a
    follow-up scheduled since then has its own window, so the timer re-arms
    for what is left of it instead of stopping the follow-up."""
    booted = boot(client)
    game_id = booted["game_id"]
    outcome(client, booted)
    [name] = followup_jobs(cluster.batch)

    lobby._release_timer_fired(game_id)

    timer = lobby._release_timers[game_id]
    assert 0 < timer.interval <= lobby.FOLLOWUP_RELEASE_SECONDS
    assert name in cluster.batch.jobs
    assert not [
        e
        for e in audit.entries(game_id)
        if str(e["detail"].get("reason", "")).startswith("post-game work overran")
    ]


def test_an_unfinished_outcome_after_end_game_schedules_nothing(
    client: TestClient, cluster: SimpleNamespace
) -> None:
    """End game releases the seat, and its pod's preStop handover can still
    file `unfinished` afterwards (the route reads `store.archived`). The
    game is gone, so no follow-up Job is created: the row says `skipped`."""
    booted = boot(client)
    game_id = booted["game_id"]
    ended = client.delete(
        f"/api/lobby/games/{game_id}", params={"uid": booted["seat_uids"]["alice"]}
    )
    assert ended.status_code == 200, ended.text

    answer = outcome(client, booted)

    assert answer["followup"] == "skipped"
    assert followup_jobs(cluster.batch) == {}
    [row] = followup_rows(game_id)
    assert (row["player"], row["state"]) == ("bob", "skipped")
    assert "ended" in row["reason"]
