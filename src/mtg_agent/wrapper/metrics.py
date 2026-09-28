"""The ccw_* Prometheus collectors, keeping the Go original's metric names.

A private registry rather than the process-global default, so tests can build
as many Metrics as they like without duplicate-timeseries errors.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

#: `ccw_inference_seconds` bucket boundaries. Chosen against a real 14-minute
#: play session (133 inferences): median 4.7s, p90 16.4s, latency fitting
#: `0.78 + output_tokens / 84` (corr 0.976) - so resolution matters most
#: between about 2s and 25s, with a long, coarser tail for the rare inference
#: that reasons for minutes.
INFERENCE_SECONDS_BUCKETS = (1, 2, 3, 5, 8, 12, 17, 25, 40, 60, 90, 150)

#: `output_tokens_bucket` label edges. From the same session: output tokens
#: per inference implied by the latency fit above range from a few dozen
#: (a short refusal or a one-line answer) to several thousand (a deep
#: combat-math inference at high effort) - about 90% of that is
#: extended-thinking tokens, not the visible text/tool_use.
_OUTPUT_TOKEN_BUCKET_EDGES = (200, 500, 1000, 2000, 4000)

#: `ccw_model_step_seconds`/`ccw_tool_seconds` bucket boundaries. Fed from
#: `activity.StepTimer`, one observation per completed tool call: a tool
#: itself runs from a fast in-memory board read (tens of ms) to a rules
#: lookup or a script run (several seconds), and the model-thinking half of a
#: step is bounded below by inference latency itself - coarser than
#: `INFERENCE_SECONDS_BUCKETS` on purpose, since this is an operational
#: latency signal, not the tokens-vs-time fit that histogram was tuned for.
STEP_SECONDS_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40, 90)


def _output_tokens_bucket(output_tokens: int) -> str:
    if output_tokens < _OUTPUT_TOKEN_BUCKET_EDGES[0]:
        return f"<{_OUTPUT_TOKEN_BUCKET_EDGES[0]}"
    for lo, hi in zip(_OUTPUT_TOKEN_BUCKET_EDGES, _OUTPUT_TOKEN_BUCKET_EDGES[1:], strict=False):
        if output_tokens < hi:
            return f"{lo}-{hi}"
    return f">={_OUTPUT_TOKEN_BUCKET_EDGES[-1]}"


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None, effort: str = "") -> None:
        self.registry = registry or CollectorRegistry()
        #: Set once at process start from `Settings.effort`; carried as a
        #: label on every token sample rather than passed per-call, since a
        #: single wrapper process runs one effort for its whole life.
        self.effort = effort
        r = self.registry
        self.turns_total = Counter("ccw_turns_total", "Turns by result", ["result"], registry=r)
        self.turn_duration_seconds = Histogram(
            "ccw_turn_duration_seconds", "Turn duration", registry=r
        )
        #: One observation per model inference (one transcript `requestId`),
        #: not per turn - `turn_duration_seconds` above spans a whole match.
        #: Labelled by output-token bucket so an effort change's effect on
        #: latency can be read straight from production rather than argued
        #: from a one-off audit.
        self.inference_seconds = Histogram(
            "ccw_inference_seconds",
            "Wall-clock duration of one model inference, by output-token bucket",
            ["output_tokens_bucket"],
            buckets=INFERENCE_SECONDS_BUCKETS,
            registry=r,
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
            "ccw_turn_tokens_total", "Tokens by type", ["type", "model", "effort"], registry=r
        )
        self.turn_cost_usd_total = Counter(
            "ccw_turn_cost_usd_total", "Cumulative turn cost", registry=r
        )
        self.turn_stall_suspected_total = Counter(
            "ccw_turn_stall_suspected_total", "Inactivity timer firings", registry=r
        )
        self.probe_total = Counter("ccw_probe_total", "Probes by result", ["result"], registry=r)
        #: Fed from `activity.StepTimer`, one observation per completed tool
        #: call: how long the model took to produce the `tool_use` (the time
        #: since the previous tool result, or turn start). Unlabelled -
        #: `ccw_tool_seconds` below carries the per-tool breakdown, and a
        #: second label dimension here would only double-count it.
        self.model_step_seconds = Histogram(
            "ccw_model_step_seconds",
            "Wall-clock duration from the previous tool result (or turn start) to a tool_use line",
            buckets=STEP_SECONDS_BUCKETS,
            registry=r,
        )
        #: The other half of the same step: from `tool_use` to its matching
        #: `tool_result`, labelled by the tool's own short name (an `mcp`
        #: server prefix stripped, a builtin tool name kept as-is).
        self.tool_seconds = Histogram(
            "ccw_tool_seconds",
            "Wall-clock duration of one tool call, from tool_use to its tool_result",
            ["tool"],
            buckets=STEP_SECONDS_BUCKETS,
            registry=r,
        )
        #: A tool result whose JSON body carries an `auto_answered` list (the
        #: server answered one or more pending decisions on the caller's
        #: behalf) - incremented by that list's length, not by 1 per result.
        self.auto_answers_total = Counter(
            "ccw_auto_answers_total",
            "Server-side auto-answered decisions observed in a tool result",
            registry=r,
        )

    def record_tokens(self, turn_tokens: list[dict[str, Any]]) -> None:
        for entry in turn_tokens or []:
            model = entry.get("model") or ""
            for key in ("input", "output", "cache_read", "cache_creation"):
                value = entry.get(key) or 0
                if value:
                    self.turn_tokens_total.labels(type=key, model=model, effort=self.effort).inc(
                        value
                    )

    def record_inference(self, duration_seconds: float, output_tokens: int) -> None:
        """One model inference finished. `duration_seconds < 0` is a clock
        skew or a mis-ordered transcript line, not a real observation - drop
        it rather than poison the histogram."""
        if duration_seconds < 0:
            return
        self.inference_seconds.labels(
            output_tokens_bucket=_output_tokens_bucket(output_tokens)
        ).observe(duration_seconds)

    def record_step(
        self, tool: str, model_seconds: float, tool_seconds: float, auto_answers: int = 0
    ) -> None:
        """One tool-call step finished (fed from `activity.StepTimer`).

        The two durations are independent transcript-line measurements, so a
        clock skew poisoning one (`< 0`) is dropped on its own rather than
        discarding the other half of the same step too."""
        if model_seconds >= 0:
            self.model_step_seconds.observe(model_seconds)
        if tool_seconds >= 0:
            self.tool_seconds.labels(tool=tool).observe(tool_seconds)
        if auto_answers:
            self.auto_answers_total.inc(auto_answers)

    def record_cost(self, result_json: Any) -> None:
        if not isinstance(result_json, dict):
            return
        cost = result_json.get("total_cost_usd")
        if isinstance(cost, int | float):
            self.turn_cost_usd_total.inc(float(cost))

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
