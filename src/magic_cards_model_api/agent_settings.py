"""What an AI seat runs as: the Claude Code model and effort, and an optional
thinking budget. Chosen per game in the lobby, carried into whichever runner
plays the seat (subprocess argv, wrapper dispatch, or a Job's env).

Names follow Claude Code's own: `--model`, `--effort` (low, medium, high,
xhigh, max) and the `MAX_THINKING_TOKENS` environment variable.

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

`populate_by_name=True` keeps a third wire working unmodified: the browser's
create-game request (`ui/lobby_model.js`'s `agentFrom`, read by
`decks_api.py`) still posts `max_thinking_tokens`, and `serialize.py`'s game
state view still echoes it that way to the play UI - that boundary was never
part of this task's "seat assignment" scope, so both spellings validate on
input and `to_wire()` alone is what standardises the output.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Effort = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_MODEL_ENV = "AGENT_DEFAULT_MODEL"


class AgentSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    model: str = Field(min_length=1)
    effort: Effort = "high"
    max_thinking_tokens: int | None = Field(default=None, ge=0, alias="maxThinkingTokens")

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
        `model_dump()`."""
        return self.model_dump(mode="json", by_alias=True)
