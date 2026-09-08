"""`contract/agent-settings.schema.json` is the one committed shape for
`AgentSettings` on the wire between the lobby and the wrapper/driver (the
seat assignment): camelCase, checked here against both ends so a spelling
mismatch is caught by CI rather than by a live seat silently running the
default model.

This is deliberately narrower than every place `AgentSettings` appears.
`decks_api.py`'s browser-facing create-game body and `serialize.py`'s game
state view are a different wire (browser <-> lobby/api) that stays
snake_case - `test_game_agent_settings.py` and `test_ui_lobby.py` pin that
one, unchanged. `AgentSettings.env()` is a third, unrelated wire (the
`MODEL`/`EFFORT`/`MAX_THINKING_TOKENS` environment variables the claude CLI
reads) with no JSON shape to validate at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from magic_cards_model_api.agent_settings import AgentSettings
from mtg_agent.wrapper.api import SessionIn

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "contract" / "agent-settings.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


# ---- the write end: what the lobby puts in a seat assignment --------------


def test_the_wire_shape_of_a_default_agent_validates(schema):
    payload = AgentSettings(model="sonnet").to_wire()
    jsonschema.validate(payload, schema)
    assert payload == {"model": "sonnet", "effort": "medium", "maxThinkingTokens": None}


def test_the_wire_shape_with_a_thinking_budget_validates(schema):
    payload = AgentSettings(model="opus", effort="max", max_thinking_tokens=4096).to_wire()
    jsonschema.validate(payload, schema)
    assert payload == {"model": "opus", "effort": "max", "maxThinkingTokens": 4096}


def test_the_wire_shape_never_carries_the_snake_case_key(schema):
    payload = AgentSettings(model="sonnet", max_thinking_tokens=10).to_wire()
    assert "max_thinking_tokens" not in payload
    assert "maxThinkingTokens" in payload
    jsonschema.validate(payload, schema)


def test_a_stray_snake_case_payload_is_rejected_by_the_schema(schema):
    """Proves the schema is not vacuously permissive: the shape this file
    replaced (`model_dump()` with no `by_alias`) must fail it."""
    stray = {"model": "sonnet", "effort": "high", "max_thinking_tokens": None}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(stray, schema)


# ---- the read end: what the wrapper accepts on POST /v1/session -----------


def test_the_wrapper_session_model_accepts_the_schema_shaped_payload(schema):
    payload = AgentSettings(model="opus", effort="max", max_thinking_tokens=4096).to_wire()
    jsonschema.validate(payload, schema)
    parsed = SessionIn.model_validate(payload)
    assert parsed.model == "opus"
    assert parsed.effort == "max"
    assert parsed.max_thinking_tokens == 4096


def test_the_wrapper_session_model_accepts_a_null_thinking_budget(schema):
    payload = AgentSettings(model="sonnet").to_wire()
    jsonschema.validate(payload, schema)
    parsed = SessionIn.model_validate(payload)
    assert parsed.max_thinking_tokens is None


def test_the_wrapper_session_model_refuses_the_old_snake_case_key():
    """The wrapper reads one spelling. A `max_thinking_tokens` key (the shape
    before this fix) must not silently validate - it would leave a live seat
    on whatever thinking budget the wrapper already booted with."""
    with pytest.raises(ValidationError):
        SessionIn.model_validate({"model": "opus", "effort": "max", "max_thinking_tokens": 4096})


# ---- the read end: what the driver reads out of DispatchIn.agent ----------


def test_driver_reads_the_seat_assignment_agent_camel_case(schema):
    """`sync_agent_settings` (`mtg_agent/driver/app.py`) is the driver's own
    read of the seat-assignment `agent` dict. Exercised directly here against
    a schema-valid payload, and against the wrapper it pushes to, via
    `tests/driver/test_app.py`'s fuller dispatch-level tests."""
    from mtg_agent.driver.app import _wanted_agent_settings

    payload = AgentSettings(model="opus", effort="max", max_thinking_tokens=4096).to_wire()
    jsonschema.validate(payload, schema)
    assert _wanted_agent_settings(payload) == {
        "model": "opus",
        "effort": "max",
        "maxThinkingTokens": 4096,
    }
