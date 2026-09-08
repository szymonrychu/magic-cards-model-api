"""The AI seat runners honour per-game `AgentSettings`: the subprocess argv,
the wrapper dispatch payload, and (elsewhere) the child env and the driver's
own `Assignment`. See `docs/mcp-tools.md`-adjacent module docstrings in
`ai_seat.py` for the mechanisms themselves; this file only checks that the
chosen model/effort/thinking budget actually reach them."""

from __future__ import annotations

from fastapi.testclient import TestClient

from magic_cards_model_api import ai_seat
from magic_cards_model_api.agent_settings import AgentSettings

# Captured at import time, before `tests/conftest.py`'s autouse fixture ever
# patches `ai_seat.command_for` to a harmless stand-in - the real function,
# for the one test below that needs its actual argv rather than a stand-in.
_REAL_COMMAND_FOR = ai_seat.command_for

# 52 Mountain/Forest (unlimited, basic) + 4 Grizzly Bears + 4 Lightning Bolt =
# 60, legal under CR 100.2a's four-copy limit - same shape `test_decks_api.py`
# already uses, kept local here to stay self-contained.
LEGAL_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
LEGAL_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"


def test_argv_carries_model_and_effort(monkeypatch) -> None:
    monkeypatch.setattr(ai_seat, "command_for", _REAL_COMMAND_FOR)
    argv = ai_seat.command_for("hello", AgentSettings(model="opus", effort="max"))
    assert argv[:6] == ["claude", "--print", "--model", "opus", "--effort", "max"]
    assert argv[-2:] == ["--", "hello"]


def test_api_service_url_reads_env_with_the_k8s_settings_default(monkeypatch) -> None:
    """`K8sSettings.from_env` (`k8s_seats.py`) already reads `API_SERVICE_URL`
    with this exact default for the kubernetes mechanism - `api_service_url`
    is the same lookup for the other two mechanisms, and must not drift from
    it."""
    monkeypatch.delenv("API_SERVICE_URL", raising=False)
    assert ai_seat.api_service_url() == "http://mtg-stack.mtg-engine.svc.cluster.local:8000"
    monkeypatch.setenv("API_SERVICE_URL", "http://configured-service:9000")
    assert ai_seat.api_service_url() == "http://configured-service:9000"


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
        "maxThinkingTokens": 0,
    }


# -- creation itself starts the seat (POST /api/ui/games, subprocess mode) --
#
# This is the task's headline behavior - `start_game_from_decklists` starting
# each `ai: true` seat right after `ai_pregame.start`, without a human having
# to click the lobby's separate "start AI seat" button - and until this test,
# nothing asserted it: deleting the block in `decks_api.py` left the suite
# green.


class _CapturingDriverClient:
    """Stands in for `ai_seat._driver_client`: records the `/dispatch`
    payload's `api_base` instead of making a real HTTP call, so a test can
    assert on it without a driver process."""

    def __init__(self, captured: dict) -> None:
        self._captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, path, json):
        self._captured["json"] = json

        class Response:
            status_code = 202

            def json(self):
                return {"deck": None, "turnId": "t1", "pending": False}

        return Response()


# -- T7: the seat's API URL comes from API_SERVICE_URL, never from whatever
# Host header the request that triggered dispatch happened to carry. Real
# regression: after the six-way cut, `POST /api/ui/games` and
# `POST .../ai-seat/start` both arrive at the *lobby*, not the api - a URL
# derived from that request would point every seat at the lobby, and every
# seat call would 404. Both tests forge an arbitrary `Host` header so a fix
# that merely "usually works" (single-service compose, same-origin tests)
# cannot pass by accident.


def test_creation_dispatch_ignores_the_hosts_header_and_uses_api_service_url(
    monkeypatch, client: TestClient
) -> None:
    captured: dict = {}
    monkeypatch.setattr(ai_seat, "_driver_client", lambda base: _CapturingDriverClient(captured))
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("API_SERVICE_URL", "http://mtg-stack.mtg-engine.svc.cluster.local:8000")

    response = client.post(
        "/api/ui/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": LEGAL_DECK},
                {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": True},
            ]
        },
        headers={"host": "totally-wrong-host.example:9999"},
    )
    assert response.status_code == 200, response.text
    assert captured["json"]["api_base"] == "http://mtg-stack.mtg-engine.svc.cluster.local:8000"


def test_manual_start_route_ignores_the_hosts_header_and_uses_api_service_url(
    monkeypatch, client: TestClient
) -> None:
    # Boot in subprocess mode (claude unavailable, so creation never
    # auto-starts) - isolates the manual `POST .../ai-seat/start` route's
    # own dispatch from creation's.
    response = client.post(
        "/api/ui/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": LEGAL_DECK},
                {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": True},
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    bot_uid = body["seat_uids"]["bob"]
    assert ai_seat.registry.status(bot_uid) is None, "should not have auto-started"

    captured: dict = {}
    monkeypatch.setattr(ai_seat, "_driver_client", lambda base: _CapturingDriverClient(captured))
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("API_SERVICE_URL", "http://mtg-stack.mtg-engine.svc.cluster.local:8000")

    started = client.post(
        f"/api/ui/games/{body['game_id']}/ai-seat/start",
        json={"uid": bot_uid},
        headers={"host": "totally-wrong-host.example:9999"},
    )
    assert started.status_code == 200, started.text
    assert captured["json"]["api_base"] == "http://mtg-stack.mtg-engine.svc.cluster.local:8000"


def test_creation_starts_the_ai_seat_in_subprocess_mode(monkeypatch, client: TestClient) -> None:
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(
        ai_seat,
        "command_for",
        lambda prompt, agent: ["python3", "-c", "import time; time.sleep(30)"],
    )
    response = client.post(
        "/api/ui/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": LEGAL_DECK},
                {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": True},
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    bot_uid = body["seat_uids"]["bob"]

    # No call to `POST .../ai-seat/start` yet - creation alone must have
    # already spawned the runner.
    status = ai_seat.registry.status(bot_uid)
    assert status is not None, "creation did not start the seat on its own"
    assert status["mechanism"] == "subprocess"
    assert status["state"] == "thinking"
    runner = ai_seat.registry._runners[bot_uid]

    # The separate start route stays, and calling it for an already-started
    # uid must not spawn a second process.
    started = client.post(f"/api/ui/games/{body['game_id']}/ai-seat/start", json={"uid": bot_uid})
    assert started.status_code == 200
    assert ai_seat.registry._runners[bot_uid] is runner
