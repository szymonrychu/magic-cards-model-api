from __future__ import annotations

import pytest
from pydantic import ValidationError

from magic_cards_model_api.agent_settings import AgentSettings


def test_defaults_come_from_env() -> None:
    settings = AgentSettings.default({"AGENT_DEFAULT_MODEL": "opus"})
    assert settings.model == "opus"
    assert settings.effort == "high"
    assert settings.max_thinking_tokens is None
    assert AgentSettings.default({}).model == "sonnet"


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
