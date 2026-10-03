"""What an AI seat runs as: the Claude Code model and effort, and an optional
thinking budget. Chosen per game in the lobby, carried into whichever runner
plays the seat (subprocess argv, wrapper dispatch, or a Job's env).

Names follow Claude Code's own: `--model`, `--effort` (low, medium, high,
xhigh, max) and the `MAX_THINKING_TOKENS` environment variable.

The default `effort` is `high` (owner direction, 2026-09-19, superseding the
2026-09-13 call below): AI seats play on Sonnet now, not Opus, and the cost
cut that bought is what makes `high` effort affordable again for the whole
game rather than only "the per-game lobby dropdown for a combo-heavy deck".
The earlier reasoning for `medium` is worth keeping for the record: a
live-pod audit (14 minutes of real play, 133 inferences) found latency
correlates almost perfectly with output tokens (corr 0.976), context size
does not (corr -0.101 - prefill caches nearly perfectly), and ~90% of every
inference's output is extended-thinking tokens - so effort, not state size,
is what makes a seat slow. That finding is unchanged; the owner's call on
what to trade for it is. `low` is still deliberately not the default: it is
documented as stopping to ask when a task is multi-step, but a seat's
`AskUserQuestion` tool is denied and it is forbidden from stopping, so a seat
that wants to ask and cannot may simply stop - worse than slow. This is not
hypothetical for `docker-compose.aivai.yml` (DR7), which defaults both of its
seats to `low` on purpose for a cheap self-play loop - the mitigation there
is not raising the seat's own effort but catching a seat that has gone quiet:
the Opus-vs-Opus loop's own watcher (DR8) treats a seat with no activity past
its grace as the stalled case to detect and restart, the same shape
`ai_pregame.py`'s `GONE`/`activity()` backstop already catches for a seat
that never started at all.
`AGENT_DEFAULT_EFFORT` overrides the default per deployment, the same as
`AGENT_DEFAULT_MODEL` does for the model - there is no longer a per-game
lobby control for either (2026-09-19: the model/effort/thinking-budget
pickers were removed from the lobby UI and from `NewUiPlayer`'s own request
shape; model and effort are a server-side deployment choice now, never a
per-request one).

`MAX_THINKING_TOKENS`/`max_thinking_tokens` is honoured only by models with a
fixed (non-adaptive) reasoning budget. Opus (still the review model, see
below) uses adaptive reasoning and silently ignores it - `effort` is the
only control that bites there. The field is kept for models that still
honour it, though with no lobby control left to set it, in practice it is
now only ever `None` unless a future deployment sets one through code.

The default model is `claude-sonnet-5` (owner direction, 2026-09-19, cost
cutting - supersedes the 2026-09-13 call for `opus` below), for an AI
opponent seat and a buddy alike - both fall back to `default()` when the
create-game request names no settings, which as of 2026-09-19 is *always*,
since the request can no longer name any (see above). `AGENT_DEFAULT_MODEL`
still overrides it per deployment - one server-side config, not a per-game
choice. The wire contract carries no default: `model` is required there, so
this only ever travels as a value.

**The post-game AI review is a separate, fixed model choice: Opus, always.**
`review_default()` returns `model="claude-opus-5", effort="high"` and never
reads `AGENT_DEFAULT_MODEL`/`AGENT_DEFAULT_EFFORT` - those name what a seat
*plays* with, and the review is deliberately the stronger, slower model
precisely because it runs once per game after the seat has already retired,
not on every turn's clock. See `mtg_agent.driver.review` for how the handoff
(the seat's own post-game reflection) and the game record reach it.

This crossed four repos in three spellings (T13, phase 2b): a snake_case
pydantic model, the `MODEL`/`EFFORT`/`MAX_THINKING_TOKENS` environment
variables, a bare `model_dump()` dict, and a camelCase `maxThinkingTokens`
read on the far end. The wire is now one spelling, camelCase, published as
`contract/agent-settings.schema.json` and produced only by `to_wire()` -
every seat-assignment write site (`k8s_seats.build_job`,
`ai_seat._dispatch_to_driver`) uses it, and the wrapper's/driver's own reads
of that payload expect it. `model`/`effort` are single words, so this is only
ever visible on `max_thinking_tokens`/`maxThinkingTokens`.

The env-var path (`env()`) is untouched: the claude CLI reads
`MODEL`/`EFFORT`/`MAX_THINKING_TOKENS` as environment variables, a wire this
schema does not describe, and `env()` already builds that dict from this
model's own attributes rather than duplicating its shape by hand.

`populate_by_name=True` plus `max_thinking_tokens`'s `validation_alias`
(accepting either spelling) keep a third wire working unmodified: the
browser's create-game request (`ui/lobby_model.js`'s `agentFrom`, read by
`decks_api.py`) still posts `max_thinking_tokens`, and `serialize.py`'s game
state view still echoes it that way to the play UI - that boundary was never
part of this task's "seat assignment" scope.

**Deliberately no `serialization_alias`/`alias` on the field.** T1
(phase 2b) started declaring pydantic `response_model`s on routes, and
FastAPI serialises those with `response_model_by_alias=True` by default -
so a plain `Field(alias="maxThinkingTokens")` (which sets both the
validation *and* the serialization alias) leaked camelCase into every
browser-facing response that embeds `AgentSettings`
(`PlayerView.agent` via `GameStateView`), not just the one seat-assignment
hop it was meant for. Keeping the alias input-only means `model_dump()`
and `model_dump(by_alias=True)` both produce `max_thinking_tokens` - by
construction, not by every route remembering to opt out - so a future route
that embeds `AgentSettings` in a new response model cannot regress this
silently. `to_wire()` renames the one field by hand instead.
`tests/test_agent_settings_wire_boundary.py` pins both ends of this
together: the browser wire is snake_case *and* the seat-assignment wire is
camelCase, in the same test.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

Effort = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_MODEL_ENV = "AGENT_DEFAULT_MODEL"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT_ENV = "AGENT_DEFAULT_EFFORT"
DEFAULT_EFFORT: Effort = "high"
#: The seat orchestrator's thinking cap (int >= 0, blank = unset). Never
#: inherited by the buddy (`advisor_default`).
DEFAULT_MAX_THINKING_TOKENS_ENV = "AGENT_DEFAULT_MAX_THINKING_TOKENS"

#: The buddy/advisor's model (owner decision, T6): pinned to Opus while
#: `AGENT_DEFAULT_MODEL` moves the seat orchestrator to Sonnet. Effort still
#: follows `AGENT_DEFAULT_EFFORT`.
ADVISOR_MODEL_ENV = "ADVISOR_MODEL"
ADVISOR_DEFAULT_MODEL = "claude-opus-5"
#: The buddy's own effort; blank = `AGENT_DEFAULT_EFFORT`.
ADVISOR_EFFORT_ENV = "ADVISOR_EFFORT"

#: The post-game review's own model/effort - fixed, never read from
#: `AGENT_DEFAULT_MODEL`/`AGENT_DEFAULT_EFFORT` and never a per-request
#: choice. See the module docstring's last section.
REVIEW_MODEL = "claude-opus-5"
REVIEW_EFFORT: Effort = "high"


class AgentSettings(BaseModel):
    # `extra="forbid"` to match this model's own committed contract, which
    # already says `additionalProperties: false`
    # (`contract/agent-settings.schema.json`) - and so that a caller naming a
    # setting this build does not have is told, rather than silently running
    # with the default. `schemas.RequestModel` is the same rule for every
    # request body; this class predates it and is a cross-service wire type
    # rather than a request shape, so it carries its own config.
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    model: str = Field(min_length=1)
    effort: Effort = "high"
    max_thinking_tokens: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("max_thinking_tokens", "maxThinkingTokens"),
    )

    @classmethod
    def default(cls, env: Mapping[str, str]) -> AgentSettings:
        model = (env.get(DEFAULT_MODEL_ENV) or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        effort_raw = (env.get(DEFAULT_EFFORT_ENV) or DEFAULT_EFFORT).strip() or DEFAULT_EFFORT
        cap_raw = (env.get(DEFAULT_MAX_THINKING_TOKENS_ENV) or "").strip()
        try:
            cap = int(cap_raw) if cap_raw else None
        except ValueError:
            cap = -1
        if cap is not None and cap < 0:
            raise ValueError(
                f"{DEFAULT_MAX_THINKING_TOKENS_ENV} must be an integer >= 0 or blank, "
                f"got {cap_raw!r}"
            )
        # `cls(...)` validates this against `Effort`'s Literal at construction
        # (raising `ValidationError` for a deployment env var naming a level
        # that does not exist) - the `cast` only tells the type checker that
        # validation is coming, it performs none itself.
        return cls(model=model, effort=cast(Effort, effort_raw), max_thinking_tokens=cap)

    @classmethod
    def advisor_default(cls, env: Mapping[str, str]) -> AgentSettings:
        """A buddy/advisor's settings: `ADVISOR_MODEL` (default Opus) at
        `ADVISOR_EFFORT` (blank = the seat's `AGENT_DEFAULT_EFFORT`), never
        carrying the seat's thinking cap."""
        model = (env.get(ADVISOR_MODEL_ENV) or "").strip() or ADVISOR_DEFAULT_MODEL
        seat = cls.default(env)
        effort = (env.get(ADVISOR_EFFORT_ENV) or "").strip() or seat.effort
        try:
            return cls(model=model, effort=cast(Effort, effort))
        except ValidationError as exc:
            raise ValueError(f"{ADVISOR_EFFORT_ENV} is invalid: {effort!r}") from exc

    @classmethod
    def review_default(cls) -> AgentSettings:
        """The post-game AI review's fixed settings - see the module
        docstring's last section. Takes no `env`, deliberately: unlike
        `default()` this is never a deployment override, only ever Opus at
        high effort."""
        return cls(model=REVIEW_MODEL, effort=REVIEW_EFFORT)

    def env(self) -> dict[str, str]:
        out = {"MODEL": self.model, "EFFORT": self.effort}
        if self.max_thinking_tokens is not None:
            out["MAX_THINKING_TOKENS"] = str(self.max_thinking_tokens)
        return out

    def claude_args(self) -> list[str]:
        return ["--model", self.model, "--effort", self.effort]

    def to_wire(self) -> dict[str, Any]:
        """The one JSON shape for a seat assignment - camelCase, matching
        `contract/agent-settings.schema.json`. Every cross-service hop that
        carries `AgentSettings` (the dispatch payload posted to the driver,
        directly or via `DRIVER_ASSIGNMENT_JSON`, and the driver's own push
        to the wrapper's `POST /v1/session`) goes through this, never a bare
        `model_dump()`.

        The field carries no serialization alias (see the module docstring),
        so `by_alias=True` would not rename anything here - the one field
        that needs it is renamed by hand instead."""
        data = self.model_dump(mode="json")
        data["maxThinkingTokens"] = data.pop("max_thinking_tokens")
        return data
