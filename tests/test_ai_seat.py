"""`ai_seat.py`: the headless-subprocess mechanism and its fallback.

Every test here spawns a *real* OS subprocess (never a mock of `subprocess`
itself, matching this project's no-mocks rule) - but not the real `claude`
CLI, which costs real API time/money and would make the suite flaky and slow.
`command_for` is monkeypatched to a fast, deterministic stand-in command
(`python3 -c "..."`) instead, so what is under test - process lifecycle,
status transitions, stdout/stderr capture, idempotent start, stop - is
exercised against a real process the same way it would be against `claude`,
just a cheap one.

The real `claude --print --dangerously-skip-permissions --allowedTools Bash
-- "<prompt>"` invocation this module actually runs in production was
confirmed by hand, once, outside this suite: `claude --help` documents
`--allowedTools` as variadic (`<tools...>`), which swallows an un-separated
trailing prompt argument and fails with "Input must be provided either
through stdin or as a prompt argument" - the `--` separator `command_for`
always includes is not a guess, it is what turned that failure into a
working, one-line "DONE" reply.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from magic_cards_model_api import ai_seat
from magic_cards_model_api.agent_settings import AgentSettings

#: What every `.start()` call below runs as - the exact model/effort do not
#: matter to these tests, only that some `AgentSettings` reaches the runner.
DEFAULT_AGENT = AgentSettings(model="sonnet")

# Captured at import time, before `tests/conftest.py`'s autouse fixture ever
# patches `ai_seat.command_for` to a harmless stand-in - the real function,
# for the tests below that check its actual argv/fallback text rather than a
# stand-in's.
_REAL_COMMAND_FOR = ai_seat.command_for


def wait_until(predicate, *, max_seconds: float = 5.0, poll: float = 0.05) -> None:
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(poll)
    assert predicate(), "condition never became true within the bound"


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> ai_seat.AiSeatRegistry:
    """Each test gets its own registry so runners from one test never leak
    into another's uid namespace."""
    registry = ai_seat.AiSeatRegistry()
    monkeypatch.setattr(ai_seat, "registry", registry)
    return registry


def test_build_prompt_fills_in_the_seats_own_identifiers() -> None:
    prompt = ai_seat.build_prompt(
        uid="uid-123", game_id="game-456", player="alice", api_base="http://127.0.0.1:9000"
    )
    assert "uid-123" in prompt
    assert "game-456" in prompt
    assert "alice" in prompt
    assert "http://127.0.0.1:9000" in prompt


def test_command_for_places_the_prompt_after_a_bare_dash_dash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ai_seat, "command_for", _REAL_COMMAND_FOR)
    # Load-bearing: `--allowedTools` is variadic in the real CLI and would
    # otherwise swallow the prompt as another tool name - see module docstring.
    command = ai_seat.command_for("a prompt with spaces", DEFAULT_AGENT)
    assert command[-2] == "--"
    assert command[-1] == "a prompt with spaces"
    assert "--dangerously-skip-permissions" in command
    assert "--allowedTools" in command


def test_claude_unavailable_reports_fallback_with_a_runnable_command(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    monkeypatch.setattr(ai_seat, "claude_available", lambda: False)
    monkeypatch.setattr(ai_seat, "command_for", _REAL_COMMAND_FOR)
    status = fresh_registry.start(
        uid="u1", game_id="g1", player="bob", api_base="http://127.0.0.1:8000", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "fallback"
    assert "claude" in status["command"]
    assert "u1" in status["prompt"]
    assert "g1" in status["prompt"]


def test_a_spawned_runner_completes_and_reports_its_output(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(
        ai_seat, "command_for", lambda prompt, agent: ["python3", "-c", "print('DONE')"]
    )
    status = fresh_registry.start(
        uid="u2", game_id="g2", player="bob", api_base="http://127.0.0.1:8000", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "subprocess"
    assert status["state"] in ("thinking", "stopped")

    wait_until(lambda: fresh_registry.status("u2")["state"] == "stopped")
    final = fresh_registry.status("u2")
    assert final["returncode"] == 0
    assert "DONE" in final["stdout_tail"]


def test_start_is_idempotent_for_the_same_uid(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    calls = []

    def fake_command(prompt: str, agent: AgentSettings) -> list[str]:
        calls.append(prompt)
        return ["python3", "-c", "print('DONE')"]

    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(ai_seat, "command_for", fake_command)
    fresh_registry.start(
        uid="u3", game_id="g3", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    fresh_registry.start(
        uid="u3", game_id="g3", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert len(calls) == 1, "a second start() for the same uid must not spawn a second process"


def test_stop_terminates_a_running_process(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(
        ai_seat,
        "command_for",
        lambda prompt, agent: ["python3", "-c", "import time; time.sleep(30)"],
    )
    fresh_registry.start(
        uid="u4", game_id="g4", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    wait_until(lambda: fresh_registry.status("u4")["state"] == "thinking")
    assert fresh_registry.stop("u4") is True
    wait_until(lambda: fresh_registry.status("u4")["state"] != "thinking", max_seconds=10)


def test_spawn_failure_falls_back_cleanly(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(ai_seat, "command_for", lambda prompt, agent: ["/no/such/binary-at-all"])
    status = fresh_registry.start(
        uid="u5", game_id="g5", player="bob", api_base="http://x", agent=DEFAULT_AGENT
    )
    assert status["mechanism"] == "fallback"
    assert status["state"] == "error"
    assert status["error"]


# -- HTTP layer (ai_seat_api.py) ---------------------------------------------


def test_ai_seat_status_before_start_is_not_started(client: TestClient) -> None:
    response = client.get("/api/ui/games/no-such-game/ai-seat/status", params={"uid": "nobody"})
    assert response.status_code == 200
    assert response.json()["state"] == "not_started"


def test_ai_seat_start_refuses_an_unknown_uid(client: TestClient, game: str) -> None:
    response = client.post(f"/api/ui/games/{game}/ai-seat/start", json={"uid": "not-a-real-uid"})
    assert response.status_code == 404
