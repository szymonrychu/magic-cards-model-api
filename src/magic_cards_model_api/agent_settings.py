"""What an AI seat runs as: the Claude Code model and effort, and an optional
thinking budget. Chosen per game in the lobby, carried into whichever runner
plays the seat (subprocess argv, wrapper dispatch, or a Job's env).

Names follow Claude Code's own: `--model`, `--effort` (low, medium, high,
xhigh, max) and the `MAX_THINKING_TOKENS` environment variable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field

Effort = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_MODEL_ENV = "AGENT_DEFAULT_MODEL"


class AgentSettings(BaseModel):
    model: str = Field(min_length=1)
    effort: Effort = "high"
    max_thinking_tokens: int | None = Field(default=None, ge=0)

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
