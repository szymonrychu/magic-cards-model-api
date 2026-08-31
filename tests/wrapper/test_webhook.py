import pytest

from mtg_agent.wrapper.metrics import Metrics
from mtg_agent.wrapper.turns import Record, TurnState
from mtg_agent.wrapper.webhook import Deliverer


def a_record(callback_url: str = "https://cb.example/x") -> Record:
    return Record(
        id="turn-1",
        state=TurnState.COMPLETE,
        text="go",
        callback_url=callback_url,
        started_at=1.0,
        completed_at=2.0,
        final_text="PONG",
    )


def test_metrics_expose_the_ccw_names(tmp_path):
    metrics = Metrics()
    metrics.turns_total.labels(result="complete").inc()
    body, content_type = metrics.render()
    assert b"ccw_turns_total" in body
    assert b"ccw_turn_in_flight" in body
    assert "text/plain" in content_type


def test_record_tokens_counts_every_type_and_model():
    metrics = Metrics()
    metrics.record_tokens(
        [{"model": "sonnet", "input": 10, "output": 3, "cache_read": 5, "cache_creation": 1}]
    )
    body = metrics.render()[0].decode()
    assert 'ccw_turn_tokens_total{model="sonnet",type="input"} 10.0' in body
    assert 'ccw_turn_tokens_total{model="sonnet",type="cache_read"} 5.0' in body


def test_record_cost_only_counts_when_the_agent_wrote_one():
    metrics = Metrics()
    metrics.record_cost(None)
    metrics.record_cost({"nothing": 1})
    assert metrics.turn_cost_usd_total._value.get() == 0
    metrics.record_cost({"total_cost_usd": 0.25})
    assert metrics.turn_cost_usd_total._value.get() == pytest.approx(0.25)


def test_delivery_posts_the_full_record(monkeypatch):
    posted = []
    metrics = Metrics()
    deliverer = Deliverer(metrics)
    monkeypatch.setattr(deliverer, "_post", lambda url, body: posted.append((url, body)))
    deliverer.deliver(a_record())
    url, body = posted[0]
    assert url == "https://cb.example/x"
    assert body["turnId"] == "turn-1"
    assert body["finalText"] == "PONG"
    assert metrics.webhook_delivery_total.labels(result="ok")._value.get() == 1


def test_delivery_retries_then_drops_and_counts_it(monkeypatch):
    attempts = []
    metrics = Metrics()
    deliverer = Deliverer(metrics, retries=3, backoff=0)

    def always_fail(url, body):
        attempts.append(url)
        raise OSError("refused")

    monkeypatch.setattr(deliverer, "_post", always_fail)
    deliverer.deliver(a_record())
    assert len(attempts) == 3
    assert metrics.webhook_delivery_total.labels(result="dropped")._value.get() == 1


def test_a_record_without_a_callback_url_falls_back_to_the_default(monkeypatch):
    posted = []
    deliverer = Deliverer(Metrics(), default_url="https://default.example/hook")
    monkeypatch.setattr(deliverer, "_post", lambda url, body: posted.append(url))
    deliverer.deliver(a_record(callback_url=""))
    assert posted == ["https://default.example/hook"]


def test_no_callback_url_at_all_is_a_silent_no_op(monkeypatch):
    posted = []
    deliverer = Deliverer(Metrics())
    monkeypatch.setattr(deliverer, "_post", lambda url, body: posted.append(url))
    deliverer.deliver(a_record(callback_url=""))
    assert posted == []


def test_deliver_async_delivers_on_a_background_thread(monkeypatch):
    """deliver_async is the wiring the Manager uses; exercise it for real."""
    import threading
    import time

    posted: list[tuple[str, int]] = []
    deliverer = Deliverer(Metrics())
    monkeypatch.setattr(
        deliverer,
        "_post",
        lambda url, body: posted.append((url, threading.get_ident())),
    )
    deliverer.deliver_async(a_record())
    deadline = time.monotonic() + 5
    while not posted and time.monotonic() < deadline:
        time.sleep(0.01)
    assert posted[0][0] == "https://cb.example/x"
    assert posted[0][1] != threading.get_ident()
