"""What an AI seat runs as: the Claude Code model and effort, and an optional
thinking budget. Chosen per game in the lobby, carried into whichever runner
plays the seat (subprocess argv, wrapper dispatch, or a Job's env).

Names follow Claude Code's own: `--model`, `--effort` (low, medium, high,
xhigh, max) and the `MAX_THINKING_TOKENS` environment variable.

The default `effort` is `medium`, not `high`: a live-pod audit (14 minutes of
real play, 133 inferences) found latency correlates almost perfectly with
output tokens (corr 0.976), context size does not (corr -0.101 - prefill
caches nearly perfectly), and ~90% of every inference's output is
extended-thinking tokens - so effort, not state size, is what makes a seat
slow. `low` is deliberately not the default: it is documented as stopping to
ask when a task is multi-step, but a seat's `AskUserQuestion` tool is denied
and it is forbidden from stopping, so a seat that wants to ask and cannot may
simply stop - worse than slow. The per-game lobby dropdown can still choose
`high` for a combo-heavy deck; this only changes the default.

`MAX_THINKING_TOKENS`/`max_thinking_tokens` is honoured only by models with a
fixed (non-adaptive) reasoning budget. The default model, Sonnet 5, uses
adaptive reasoning and silently ignores it - `effort` is the only control
that bites there. The field is kept for models that still honour it.

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
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

Effort = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_MODEL_ENV = "AGENT_DEFAULT_MODEL"


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
    effort: Effort = "medium"
    max_thinking_tokens: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("max_thinking_tokens", "maxThinkingTokens"),
    )

    @classmethod
    def default(cls, env: Mapping[str, str]) -> AgentSettings:
        return cls(model=(env.get(DEFAULT_MODEL_ENV) or "sonnet").strip() or "sonnet")

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
