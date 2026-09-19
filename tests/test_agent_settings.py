from __future__ import annotations

import pytest
from pydantic import ValidationError

from magic_cards_model_api.agent_settings import AgentSettings


def test_defaults_come_from_env() -> None:
    settings = AgentSettings.default({"AGENT_DEFAULT_MODEL": "opus"})
    assert settings.model == "opus"
    # `high`, not `medium` (owner direction, 2026-09-19: cost cutting moved
    # play to Sonnet, and the effort default moved to `high` alongside it -
    # the standing latency-vs-effort finding this comment used to cite was
    # about the old Opus default, not this one). An explicit
    # `AGENT_DEFAULT_EFFORT` still overrides it per deployment, exactly like
    # `AGENT_DEFAULT_MODEL` does for the model.
    assert settings.effort == "high"
    assert settings.max_thinking_tokens is None
    assert AgentSettings.default({}).model == "claude-sonnet-5"
    assert AgentSettings.default({}).effort == "high"
    assert AgentSettings.default({"AGENT_DEFAULT_MODEL": "  "}).model == "claude-sonnet-5"
    # Straight from the model's own API, not grepped off the source line.
    # This is the bare pydantic field default (used when deserialising old
    # stored data that carries no `effort` key at all, or when code
    # constructs `AgentSettings(model=...)` without naming one) - matches
    # `.default()`'s own "high" now rather than trailing the old "medium"
    # default the field used to carry on its own.
    assert AgentSettings.model_fields["effort"].default == "high"


def test_default_effort_env_override() -> None:
    settings = AgentSettings.default({"AGENT_DEFAULT_EFFORT": "max"})
    assert settings.model == "claude-sonnet-5"
    assert settings.effort == "max"
    # Blank behaves like unset, same as `AGENT_DEFAULT_MODEL` already does.
    assert AgentSettings.default({"AGENT_DEFAULT_EFFORT": "  "}).effort == "high"


def test_review_default_is_fixed_opus_high() -> None:
    """The post-game AI review always runs on Opus at high effort - a fixed
    server-side choice, never a per-request one and never overridden by
    `AGENT_DEFAULT_MODEL`/`AGENT_DEFAULT_EFFORT` (those name the PLAY
    default only)."""
    review = AgentSettings.review_default()
    assert review.model == "claude-opus-5"
    assert review.effort == "high"
    assert review.max_thinking_tokens is None


def test_effort_is_validated() -> None:
    with pytest.raises(ValidationError):
        AgentSettings(model="sonnet", effort="turbo")
    with pytest.raises(ValidationError):
        AgentSettings(model="", effort="high")
    with pytest.raises(ValidationError):
        AgentSettings(model="sonnet", effort="high", max_thinking_tokens=-1)


def test_env_and_args() -> None:
    settings = AgentSettings(model="fable", effort="max", max_thinking_tokens=0)
    assert settings.env() == {"MODEL": "fable", "EFFORT": "max", "MAX_THINKING_TOKENS": "0"}
    assert settings.claude_args() == ["--model", "fable", "--effort", "max"]
    assert "MAX_THINKING_TOKENS" not in AgentSettings(model="sonnet", effort="high").env()
