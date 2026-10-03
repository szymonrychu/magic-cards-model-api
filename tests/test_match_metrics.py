"""Per-match duration: first AI-seat dispatch to game end.

Fakes are fine here (api repo): the clock is `match_metrics._clock`, patched
to a hand-driven counter so durations are exact.
"""

from __future__ import annotations

import logging

import pytest

from magic_cards_model_api import ai_seat, match_metrics
from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.k8s_seats import ROLE_ADVISOR, ROLE_SEAT


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(match_metrics, "_clock", fake)
    monkeypatch.delenv("MATCH_TARGET_SECONDS", raising=False)
    match_metrics.reset()
    return fake


def sample(name: str, kind: str, mode: str) -> float:
    return match_metrics.REGISTRY.get_sample_value(name, {"kind": kind, "mode": mode}) or 0.0


def count(kind: str, mode: str) -> float:
    return sample("mtg_match_duration_seconds_count", kind, mode)


def total(kind: str, mode: str) -> float:
    return sample("mtg_match_duration_seconds_sum", kind, mode)


def over(kind: str, mode: str) -> float:
    return sample("mtg_match_over_target_total", kind, mode)


def test_aivai_when_every_seat_was_dispatched(clock: Clock) -> None:
    before = (count("aivai", "wrapper"), total("aivai", "wrapper"))
    match_metrics.dispatched("g-aivai", "u1", "wrapper")
    match_metrics.dispatched("g-aivai", "u2", "wrapper")
    clock.now += 600
    assert match_metrics.game_over("g-aivai", seats=2) == 600
    assert count("aivai", "wrapper") == before[0] + 1
    assert total("aivai", "wrapper") == before[1] + 600


def test_pvai_when_a_seat_was_never_dispatched(clock: Clock) -> None:
    before = count("pvai", "subprocess")
    match_metrics.dispatched("g-pvai", "u1", "subprocess")
    clock.now += 100
    match_metrics.game_over("g-pvai", seats=2)
    assert count("pvai", "subprocess") == before + 1


def test_mode_label_kubernetes(clock: Clock) -> None:
    before = count("pvai", "kubernetes")
    match_metrics.dispatched("g-k8s", "u1", "kubernetes")
    match_metrics.game_over("g-k8s", seats=2)
    assert count("pvai", "kubernetes") == before + 1


def test_first_dispatch_wins(clock: Clock) -> None:
    match_metrics.dispatched("g-first", "u1", "wrapper")
    clock.now += 300
    match_metrics.dispatched("g-first", "u2", "wrapper")
    clock.now += 300
    assert match_metrics.game_over("g-first", seats=2) == 600


def test_over_target_counts_and_warns(clock: Clock, caplog: pytest.LogCaptureFixture) -> None:
    before = over("pvai", "wrapper")
    match_metrics.dispatched("g-over", "u1", "wrapper")
    clock.now += 901
    with caplog.at_level(logging.INFO, logger=match_metrics.logger.name):
        match_metrics.game_over("g-over", seats=2)
    assert over("pvai", "wrapper") == before + 1
    [record] = [r for r in caplog.records if "match duration" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "match duration game=g-over kind=pvai mode=wrapper seconds=901 "
        "target_seconds=900 over_target=True"
    )


def test_under_target_logs_info_and_does_not_count(
    clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    before = over("pvai", "wrapper")
    match_metrics.dispatched("g-under", "u1", "wrapper")
    clock.now += 900
    with caplog.at_level(logging.INFO, logger=match_metrics.logger.name):
        match_metrics.game_over("g-under", seats=2)
    assert over("pvai", "wrapper") == before
    [record] = [r for r in caplog.records if "match duration" in r.getMessage()]
    assert record.levelno == logging.INFO
    assert "over_target=False" in record.getMessage()


def test_target_comes_from_env(clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATCH_TARGET_SECONDS", "60")
    before = over("pvai", "wrapper")
    match_metrics.dispatched("g-env", "u1", "wrapper")
    clock.now += 61
    match_metrics.game_over("g-env", seats=2)
    assert over("pvai", "wrapper") == before + 1


def test_game_with_no_dispatch_records_nothing() -> None:
    modes = ("subprocess", "wrapper", "kubernetes")
    before = [count(k, m) for k in ("aivai", "pvai") for m in modes]
    assert match_metrics.game_over("g-human", seats=2) is None
    assert before == [count(k, m) for k in ("aivai", "pvai") for m in modes]


def test_second_game_over_records_nothing(clock: Clock) -> None:
    match_metrics.dispatched("g-twice", "u1", "wrapper")
    clock.now += 10
    before = count("pvai", "wrapper")
    assert match_metrics.game_over("g-twice", seats=2) == 10
    assert match_metrics.game_over("g-twice", seats=2) is None
    assert count("pvai", "wrapper") == before + 1


def test_buckets_cover_the_target_ladder() -> None:
    for edge in (300, 600, 900, 1200, 1800, 2700, 3600, 5400):
        assert edge in match_metrics.BUCKETS


def test_registry_start_records_seat_dispatch_only(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    registry = ai_seat.AiSeatRegistry()
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setattr(
        registry,
        "_dispatch_to_driver",
        lambda **kw: {"uid": kw["uid"], "mechanism": "wrapper", "driver": "d"},
    )
    base = {
        "game_id": "g-reg",
        "player": "p",
        "api_base": "x",
        "agent": AgentSettings(model="sonnet"),
    }
    registry.start(uid="buddy", role=ROLE_ADVISOR, **base)
    clock.now += 5
    assert match_metrics.game_over("g-reg", seats=1) is None

    registry.start(uid="seat", role=ROLE_SEAT, **base)
    clock.now += 7
    assert match_metrics.game_over("g-reg", seats=1) == 7


def test_registry_start_ignores_a_failed_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ai_seat.AiSeatRegistry()
    monkeypatch.setenv("AI_SEAT_MODE", "kubernetes")
    monkeypatch.setattr(
        registry,
        "_dispatch_to_kubernetes",
        lambda **kw: {"uid": kw["uid"], "mechanism": "fallback", "reason": "no cluster"},
    )
    registry.start(
        uid="s", game_id="g-fail", player="p", api_base="x", agent=AgentSettings(model="sonnet")
    )
    assert match_metrics.game_over("g-fail", seats=1) is None


def test_kubernetes_dispatch_records_mode_kubernetes(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    registry = ai_seat.AiSeatRegistry()
    monkeypatch.setenv("AI_SEAT_MODE", "kubernetes")
    monkeypatch.setattr(
        registry,
        "_dispatch_to_kubernetes",
        lambda **kw: {"uid": kw["uid"], "mechanism": "kubernetes", "job": "j"},
    )
    registry.start(
        uid="s", game_id="g-k", player="p", api_base="x", agent=AgentSettings(model="sonnet")
    )
    before = count("aivai", "kubernetes")
    match_metrics.game_over("g-k", seats=1)
    assert count("aivai", "kubernetes") == before + 1


def test_subprocess_start_records_mode_subprocess(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    registry = ai_seat.AiSeatRegistry()
    monkeypatch.setenv("AI_SEAT_MODE", "subprocess")
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    registry.start(
        uid="s",
        game_id="g-sub",
        player="p",
        api_base="x",
        agent=AgentSettings(model="sonnet"),
    )
    before = count("aivai", "subprocess")
    clock.now += 3
    assert match_metrics.game_over("g-sub", seats=1) == 3
    assert count("aivai", "subprocess") == before + 1
    registry.stop("s")


def test_game_end_through_the_app_observes_once(client, game, clock: Clock) -> None:
    before = count("pvai", "wrapper")
    match_metrics.dispatched(game, "u1", "wrapper")
    clock.now += 42
    conceded = client.post(f"/api/games/{game}/actions/concede", json={"player": "bob"})
    assert conceded.status_code == 200, conceded.text
    assert count("pvai", "wrapper") == before + 1
    # a refused call on the finished game must not observe again
    client.post(f"/api/games/{game}/actions/concede", json={"player": "bob"})
    assert count("pvai", "wrapper") == before + 1


def test_metrics_route_exposes_the_series(client) -> None:
    match_metrics.dispatched("g-expo", "u1", "wrapper")
    match_metrics.game_over("g-expo", seats=2)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "mtg_match_duration_seconds_bucket" in response.text
    assert 'mtg_match_duration_seconds_count{kind="pvai",mode="wrapper"}' in response.text
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]
