"""Seat speed knobs (S7): the orchestrator's effort and thinking cap come from
the environment; the buddy keeps its own effort and never inherits the cap."""

from __future__ import annotations

import pytest

from magic_cards_model_api.agent_settings import AgentSettings


def test_default_reads_the_thinking_cap_env() -> None:
    assert (
        AgentSettings.default({"AGENT_DEFAULT_MAX_THINKING_TOKENS": "4000"}).max_thinking_tokens
        == 4000
    )


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_blank_thinking_cap_is_unset(raw) -> None:
    env = {} if raw is None else {"AGENT_DEFAULT_MAX_THINKING_TOKENS": raw}
    assert AgentSettings.default(env).max_thinking_tokens is None


def test_zero_is_a_valid_cap() -> None:
    assert (
        AgentSettings.default({"AGENT_DEFAULT_MAX_THINKING_TOKENS": "0"}).max_thinking_tokens == 0
    )


@pytest.mark.parametrize("raw", ["abc", "-1", "1.5"])
def test_invalid_thinking_cap_names_the_env_var(raw) -> None:
    with pytest.raises(ValueError, match="AGENT_DEFAULT_MAX_THINKING_TOKENS"):
        AgentSettings.default({"AGENT_DEFAULT_MAX_THINKING_TOKENS": raw})


def test_advisor_effort_defaults_to_the_seat_effort() -> None:
    adv = AgentSettings.advisor_default({"AGENT_DEFAULT_EFFORT": "low"})
    assert adv.effort == "low"


def test_advisor_effort_env_overrides_and_ignores_the_cap() -> None:
    env = {
        "AGENT_DEFAULT_EFFORT": "low",
        "AGENT_DEFAULT_MAX_THINKING_TOKENS": "4000",
        "ADVISOR_EFFORT": "medium",
    }
    adv = AgentSettings.advisor_default(env)
    assert adv.effort == "medium"
    assert adv.max_thinking_tokens is None
    assert "MAX_THINKING_TOKENS" not in adv.env()


def test_blank_advisor_effort_falls_back() -> None:
    assert (
        AgentSettings.advisor_default({"AGENT_DEFAULT_EFFORT": "max", "ADVISOR_EFFORT": " "}).effort
        == "max"
    )


def test_invalid_advisor_effort_names_the_env_var() -> None:
    with pytest.raises(ValueError, match="ADVISOR_EFFORT"):
        AgentSettings.advisor_default({"ADVISOR_EFFORT": "turbo"})
