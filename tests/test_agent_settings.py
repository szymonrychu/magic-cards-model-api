from __future__ import annotations

import pytest
from pydantic import ValidationError

from magic_cards_model_api.agent_settings import AgentSettings


def test_defaults_come_from_env() -> None:
    settings = AgentSettings.default({"AGENT_DEFAULT_MODEL": "opus"})
    assert settings.model == "opus"
    # `medium`, not `high`: a live-pod audit found ~90% of every inference's
    # output is extended-thinking tokens and latency correlates almost
    # perfectly with output tokens (corr 0.976) - `high` effort is what makes
    # a seat slow. `low` is out too: it is documented as stopping to ask when
    # a task is multi-step, but `AskUserQuestion` is denied and the seat is
    # forbidden from stopping, so a seat that wants to ask and cannot may
    # simply stop. `medium` is the target. The per-game lobby dropdown can
    # still choose `high` for a combo-heavy deck - this is only the default.
    assert settings.effort == "medium"
    assert settings.max_thinking_tokens is None
    assert AgentSettings.default({}).model == "sonnet"
    # Straight from the model's own API, not grepped off the source line.
    assert AgentSettings.model_fields["effort"].default == "medium"


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
