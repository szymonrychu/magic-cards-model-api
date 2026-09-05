"""The AI seat runners honour per-game `AgentSettings`: the subprocess argv,
the wrapper dispatch payload, and (elsewhere) the child env and the driver's
own `Assignment`. See `docs/mcp-tools.md`-adjacent module docstrings in
`ai_seat.py` for the mechanisms themselves; this file only checks that the
chosen model/effort/thinking budget actually reach them."""

from __future__ import annotations

from magic_cards_model_api import ai_seat
from magic_cards_model_api.agent_settings import AgentSettings


def test_argv_carries_model_and_effort() -> None:
    argv = ai_seat.command_for("hello", AgentSettings(model="opus", effort="max"))
    assert argv[:6] == ["claude", "--print", "--model", "opus", "--effort", "max"]
    assert argv[-2:] == ["--", "hello"]


def test_dispatch_payload_carries_agent(monkeypatch) -> None:
    captured = {}

    class Response:
        status_code = 202

        def json(self):
            return {"deck": None, "turnId": "t1", "pending": False}

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, path, json):
            captured["path"], captured["json"] = path, json
            return Response()

    monkeypatch.setattr(ai_seat, "_driver_client", lambda base: Client())
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    registry = ai_seat.AiSeatRegistry()
    registry.start(
        uid="u1",
        game_id="g1",
        player="bot",
        api_base="http://x",
        agent=AgentSettings(model="fable", effort="low", max_thinking_tokens=0),
    )
    assert captured["json"]["agent"] == {
        "model": "fable",
        "effort": "low",
        "max_thinking_tokens": 0,
    }
