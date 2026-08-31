"""The ccw_* Prometheus collectors, keeping the Go original's metric names.

A private registry rather than the process-global default, so tests can build
as many Metrics as they like without duplicate-timeseries errors.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.turns_total = Counter("ccw_turns_total", "Turns by result", ["result"], registry=r)
        self.turn_duration_seconds = Histogram(
            "ccw_turn_duration_seconds", "Turn duration", registry=r
        )
        self.turn_in_flight = Gauge("ccw_turn_in_flight", "1 while a turn is running", registry=r)
        self.claude_restarts_total = Counter(
            "ccw_claude_restarts_total", "claude process exits", registry=r
        )
        self.webhook_delivery_total = Counter(
            "ccw_webhook_delivery_total", "Callback deliveries", ["result"], registry=r
        )
        self.hook_received_total = Counter(
            "ccw_hook_received_total", "Stop-hook callbacks received", registry=r
        )
        self.turn_tokens_total = Counter(
            "ccw_turn_tokens_total", "Tokens by type", ["type", "model"], registry=r
        )
        self.turn_cost_usd_total = Counter(
            "ccw_turn_cost_usd_total", "Cumulative turn cost", registry=r
        )
        self.turn_stall_suspected_total = Counter(
            "ccw_turn_stall_suspected_total", "Inactivity timer firings", registry=r
        )
        self.probe_total = Counter("ccw_probe_total", "Probes by result", ["result"], registry=r)

    def record_tokens(self, turn_tokens: list[dict[str, Any]]) -> None:
        for entry in turn_tokens or []:
            model = entry.get("model") or ""
            for key in ("input", "output", "cache_read", "cache_creation"):
                value = entry.get(key) or 0
                if value:
                    self.turn_tokens_total.labels(type=key, model=model).inc(value)

    def record_cost(self, result_json: Any) -> None:
        if not isinstance(result_json, dict):
            return
        cost = result_json.get("total_cost_usd")
        if isinstance(cost, int | float):
            self.turn_cost_usd_total.inc(float(cost))

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
