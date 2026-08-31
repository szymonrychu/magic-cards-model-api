import json
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from mtg_agent.wrapper import bootstrap
from mtg_agent.wrapper.api import build_internal_app, build_public_app
from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.metrics import Metrics
from mtg_agent.wrapper.session import Manager, State
from mtg_agent.wrapper.turns import Store

STUB = Path(__file__).resolve().parents[1] / "testdata" / "stubclaude.py"
SRC = Path(__file__).resolve().parents[2] / "src"


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """The real wrapper, with the stub standing in for the claude binary and
    a real loopback internal server for the hook to POST to."""
    monkeypatch.setattr("mtg_agent.wrapper.session.MIN_BOOT", 0.5)
    monkeypatch.setattr("mtg_agent.wrapper.session.IDLE_READY", 0.5)

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    transcript = workspace / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "transcript text"}],
                    "model": "stub-model",
                    "usage": {"input_tokens": 11, "output_tokens": 4},
                    "stop_reason": "end_turn",
                },
            }
        )
        + "\n"
    )

    port = free_port()
    internal_url = f"http://127.0.0.1:{port}/internal/turn-complete"
    hook_command = (
        f"PYTHONPATH={SRC} CCW_INTERNAL_URL={internal_url} "
        f"CCW_RESULT_JSON={workspace / 'result.json'} "
        f"{sys.executable} -m mtg_agent.wrapper.stop_hook"
    )

    settings = Settings.from_env(
        {
            "CLAUDE_PATH": sys.executable,
            "WORKSPACE": str(workspace),
            "HOME_DIR": str(home),
            "BOOT_TIMEOUT_SECONDS": "30",
            "SUBMIT_DELAY_SECONDS": "0.2",
            "TURN_TIMEOUT_SECONDS": "60",
            "ANTHROPIC_API_KEY": "sk-ant-stub-key-0123456789",
        }
    )
    bootstrap.render(settings)

    monkeypatch.setenv("STUB_HOOK_COMMAND", hook_command)
    monkeypatch.setenv("STUB_TRANSCRIPT", str(transcript))
    monkeypatch.setenv("PYTHONPATH", str(SRC))

    manager = Manager(settings, Store(), Metrics())
    original_spawn = manager.spawn
    manager.spawn = lambda s, resume: __import__(
        "mtg_agent.wrapper.pty_proc", fromlist=["spawn_claude"]
    ).spawn_claude(s, resume, extra_args=[str(STUB)])
    assert original_spawn is not None

    server = uvicorn.Server(
        uvicorn.Config(build_internal_app(manager), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)

    manager.start()
    yield manager, settings, workspace
    manager.shutdown()
    server.should_exit = True


def test_the_bootstrap_seed_lands_before_claude_starts(stack):
    _, settings, workspace = stack
    seed = json.loads((settings.home_dir / ".claude.json").read_text())
    assert seed["projects"][str(workspace)]["hasTrustDialogAccepted"] is True
    # The approved entry is the key's last 20 characters, which for
    # "sk-ant-stub-key-0123456789" is "-stub-key-0123456789". (The plan wrote
    # a 22-character string here, which no [-20:] slice can produce.)
    assert seed["customApiKeyResponses"]["approved"] == [settings.anthropic_api_key[-20:]]
    assert seed["customApiKeyResponses"]["approved"] == ["-stub-key-0123456789"]
    assert (workspace / ".mcp.json").exists()


def test_boot_accepts_the_dialog_and_reaches_ready(stack):
    manager, _, _ = stack
    assert manager.state is State.READY
    assert manager.ring.contains("accepted, ready")


def test_a_full_turn_flows_from_submit_to_a_stored_result(stack):
    manager, settings, _ = stack
    client = TestClient(build_public_app(manager, settings))
    turn_id = client.post("/v1/messages", json={"text": "attack with the Frog"}).json()["turnId"]

    deadline = time.monotonic() + 30
    while manager.state is not State.READY and time.monotonic() < deadline:
        time.sleep(0.05)

    record = client.get(f"/v1/messages/{turn_id}").json()
    assert record["state"] == "complete"
    assert record["finalText"] == "ACK:attack with the Frog"
    assert record["stopReason"] == "end_turn"
    assert record["usage"]["output_tokens"] == 4
    assert record["turnTokens"] == [
        {"model": "stub-model", "input": 11, "output": 4, "cache_read": 0, "cache_creation": 0}
    ]


def test_a_second_turn_runs_in_the_same_claude_session(stack):
    """The point of the whole component: the process is long-lived, so turn
    two shares turn one's context."""
    manager, settings, _ = stack
    client = TestClient(build_public_app(manager, settings))
    for text in ("first", "second"):
        client.post("/v1/messages", json={"text": text})
        deadline = time.monotonic() + 30
        while manager.state is not State.READY and time.monotonic() < deadline:
            time.sleep(0.05)
    states = [t["state"] for t in client.get("/v1/messages").json()]
    assert states == ["complete", "complete"]
    assert manager.snapshot()["turnsCompleted"] == 2


def test_the_callback_url_receives_the_finished_turn(stack):
    manager, settings, _ = stack
    received: list[dict] = []
    port = free_port()
    from fastapi import FastAPI

    sink = FastAPI()

    @sink.post("/hook")
    def hook(body: dict) -> dict:
        received.append(body)
        return {"ok": True}

    server = uvicorn.Server(uvicorn.Config(sink, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)

    from mtg_agent.wrapper.webhook import Deliverer

    manager.on_turn_done = Deliverer(manager.metrics, backoff=0).deliver
    client = TestClient(build_public_app(manager, settings))
    client.post(
        "/v1/messages",
        json={"text": "go", "callbackUrl": f"http://127.0.0.1:{port}/hook"},
    )
    deadline = time.monotonic() + 30
    while not received and time.monotonic() < deadline:
        time.sleep(0.05)
    server.should_exit = True
    assert received[0]["finalText"] == "ACK:go"
    assert httpx is not None
