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

#: The `role` label of a series fed from the seat's own claude (the main
#: transcript): with the seat pair it is the orchestrator, and without it the
#: seat itself. Helpers' roles come from `activity.role_for_name`.
ORCHESTRATOR = "orchestrator"

#: USD per million tokens, by model-id prefix: (input, output, cache read,
#: cache write 5m). First-party API list prices; a 5-minute cache write is
#: 1.25x input. Longest prefix wins, so `claude-opus-5-5` is not priced as
#: `claude-opus-5`. A model not listed here gets no cost sample at all - a
#: guessed price would be worse than a missing one.
MODEL_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
}

_TOKEN_TYPES = ("input", "output", "cache_read", "cache_creation")


def model_price(model: str) -> tuple[float, float, float, float] | None:
    for prefix in sorted(MODEL_PRICES, key=len, reverse=True):
        if model.startswith(prefix):
            return MODEL_PRICES[prefix]
    return None


def token_cost_usd(entry: dict[str, Any]) -> float | None:
    """One `turn_tokens` entry priced from `MODEL_PRICES`, or `None` for a
    model with no listed price."""
    price = model_price(str(entry.get("model") or ""))
    if price is None:
        return None
    counts = [float(entry.get(key) or 0) for key in _TOKEN_TYPES]
    return sum(count * rate for count, rate in zip(counts, price, strict=True)) / 1_000_000


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
            ["output_tokens_bucket", "role", "model"],
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
            "ccw_turn_tokens_total",
            "Tokens by type, agent role and model",
            ["type", "model", "effort", "role"],
            registry=r,
        )
        #: By agent role and model: the orchestrator's own `total_cost_usd`
        #: when claude wrote one, otherwise every agent's tokens priced from
        #: `MODEL_PRICES` (`record_token_cost`).
        self.turn_cost_usd_total = Counter(
            "ccw_turn_cost_usd_total", "Cumulative turn cost", ["role", "model"], registry=r
        )
        self.turn_stall_suspected_total = Counter(
            "ccw_turn_stall_suspected_total", "Inactivity timer firings", registry=r
        )
        self.probe_total = Counter("ccw_probe_total", "Probes by result", ["result"], registry=r)
        #: Fed from `activity.StepTimer`, one observation per completed tool
        #: call: how long the model took to produce the `tool_use` (the time
        #: since the previous tool result or prompt, or turn start), by the
        #: agent's role only - `ccw_tool_seconds` below carries the per-tool
        #: breakdown, and a tool label here would only double-count it.
        self.model_step_seconds = Histogram(
            "ccw_model_step_seconds",
            "Wall-clock duration from the previous tool result (or turn start) to a tool_use line",
            ["role"],
            buckets=STEP_SECONDS_BUCKETS,
            registry=r,
        )
        #: The other half of the same step: from `tool_use` to its matching
        #: `tool_result`, labelled by the tool's own short name (an `mcp`
        #: server prefix stripped, a builtin tool name kept as-is).
        self.tool_seconds = Histogram(
            "ccw_tool_seconds",
            "Wall-clock duration of one tool call, from tool_use to its tool_result",
            ["tool", "role"],
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
        """Each entry by type, model and role; an entry with no `role` is the
        orchestrator's (the Stop hook's own totals carry none)."""
        for entry in turn_tokens or []:
            model = entry.get("model") or ""
            role = entry.get("role") or ORCHESTRATOR
            for key in _TOKEN_TYPES:
                value = entry.get(key) or 0
                if value:
                    self.turn_tokens_total.labels(
                        type=key, model=model, effort=self.effort, role=role
                    ).inc(value)

    def record_inference(
        self, duration_seconds: float, output_tokens: int, role: str = ORCHESTRATOR, model: str = ""
    ) -> None:
        """One model inference finished. `duration_seconds < 0` is a clock
        skew or a mis-ordered transcript line, not a real observation - drop
        it rather than poison the histogram."""
        if duration_seconds < 0:
            return
        self.inference_seconds.labels(
            output_tokens_bucket=_output_tokens_bucket(output_tokens), role=role, model=model
        ).observe(duration_seconds)

    def record_step(
        self,
        tool: str,
        model_seconds: float,
        tool_seconds: float,
        auto_answers: int = 0,
        role: str = ORCHESTRATOR,
    ) -> None:
        """One tool-call step finished (fed from `activity.StepTimer`).

        The two durations are independent transcript-line measurements, so a
        clock skew poisoning one (`< 0`) is dropped on its own rather than
        discarding the other half of the same step too."""
        if model_seconds >= 0:
            self.model_step_seconds.labels(role=role).observe(model_seconds)
        if tool_seconds >= 0:
            self.tool_seconds.labels(tool=tool, role=role).observe(tool_seconds)
        if auto_answers:
            self.auto_answers_total.inc(auto_answers)

    def record_cost(self, result_json: Any, role: str = ORCHESTRATOR, model: str = "") -> bool:
        """The `total_cost_usd` claude wrote, if any; True when it counted."""
        if not isinstance(result_json, dict):
            return False
        cost = result_json.get("total_cost_usd")
        if not isinstance(cost, int | float):
            return False
        self.turn_cost_usd_total.labels(role=role, model=model).inc(float(cost))
        return True

    def record_token_cost(
        self, turn_tokens: list[dict[str, Any]], skip_roles: frozenset[str] = frozenset()
    ) -> None:
        """Every entry priced from `MODEL_PRICES`, by role and model; roles in
        `skip_roles` already have a cost (`record_cost`)."""
        for entry in turn_tokens or []:
            role = entry.get("role") or ORCHESTRATOR
            if role in skip_roles:
                continue
            cost = token_cost_usd(entry)
            if cost:
                self.turn_cost_usd_total.labels(role=role, model=entry.get("model") or "").inc(cost)

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
