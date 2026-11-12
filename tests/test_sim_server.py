"""The mtg-sim MCP server: tools over a real worker, bearer auth, the /sim mount."""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from magic_cards_model_api.main import app
from magic_cards_model_api.sim.config import SimLimits
from magic_cards_model_api.sim.manager import SandboxManager
from magic_cards_model_api.sim.server import build_sim_server

BOLT_SPEC = json.loads((Path(__file__).parent / "fixtures" / "sim" / "bolt_bears.json").read_text())
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def sim():
    limits = SimLimits.from_env({})
    manager = SandboxManager(limits)
    server = build_sim_server(manager, limits)
    yield server
    manager.shutdown()


def call(server, name: str, **kw: Any) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, kw))
    text = result.request_state if hasattr(result, "request_state") else result.content[0].text
    return json.loads(str(text))


def test_tools_listed(sim):
    names = {t.name for t in asyncio.run(sim.list_tools())}
    assert {"sim_new", "sim_state", "sim_legal_actions", "sim_act", "sim_end"} <= names


def test_tool_docs_name_the_actions_spec_and_labels(sim):
    docs = {t.name: t.description for t in asyncio.run(sim.list_tools())}
    assert "pass" in docs["sim_act"] and "play_land" in docs["sim_act"]
    assert "@" in docs["sim_act"] and "players" in docs["sim_new"]


def test_full_interaction_over_tools_and_no_credentials_leak(sim):
    answers = []
    created = call(sim, "sim_new", spec=BOLT_SPEC)
    answers.append(created)
    sid = created["sandbox"]["id"]
    assert sid.startswith("sim-")
    answers.append(
        call(
            sim,
            "sim_act",
            sandbox_id=sid,
            action="cast",
            player="alice",
            body={"card_id": "@bolt", "targets": ["@bears"]},
        )
    )
    answers.append(call(sim, "sim_legal_actions", sandbox_id=sid, player="bob"))
    answers.append(call(sim, "sim_act", sandbox_id=sid, action="pass", player="alice"))
    answers.append(call(sim, "sim_act", sandbox_id=sid, action="pass", player="bob"))
    state = call(sim, "sim_state", sandbox_id=sid)
    answers.append(state)
    answers.append(call(sim, "sim_state", sandbox_id=sid, full=True))
    graveyard = [c["name"] for c in state["state"]["players"]["bob"]["graveyard"]]
    assert graveyard == ["Grizzly Bears"]
    assert "support_warnings" in state
    assert call(sim, "sim_end", sandbox_id=sid) == {"ended": True}

    text = json.dumps(answers)
    for key in ("admin_key", "seat_uids", "uid", "key"):
        assert f'"{key}"' not in text
    stray = set(re.findall(r"\b[0-9a-f]{32}\b", text)) - _card_ids(answers)
    stray -= {s.removeprefix("sim-") for s in re.findall(r"sim-[0-9a-f]{32}", text)}
    assert not stray, [text[text.index(x) - 80 : text.index(x) + 40] for x in stray]


def _card_ids(value: Any) -> set[str]:
    """Every card id a state shows (under "id", as a label target) and the worker-local game_id."""
    if isinstance(value, dict):
        found = {v for k, v in value.items() if k in ("id", "game_id") and isinstance(v, str)}
        found |= {v for k, v in value.items() if k == "labels" for v in v.values()}
        return found.union(*(_card_ids(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_card_ids(v) for v in value))
    return set()


def test_refusal_comes_back_as_error_text(sim):
    sid = call(sim, "sim_new", spec=BOLT_SPEC)["sandbox"]["id"]
    second = call(sim, "sim_new", spec=BOLT_SPEC)
    assert second["error"].startswith("The sandbox slot is in use")
    call(sim, "sim_end", sandbox_id=sid)
    assert "No sandbox" in call(sim, "sim_state", sandbox_id=sid)["error"]


def test_mount_requires_the_bearer(monkeypatch):
    monkeypatch.setenv("MTG_SIM_TOKEN", "t0k")
    with TestClient(app) as c:
        assert c.post("/sim/mcp", json={}).status_code == 401
        for auth in ("Bearer nope", "t0k"):
            assert c.post("/sim/mcp", json={}, headers={"Authorization": auth}).status_code == 401


def test_mount_is_disabled_without_a_token(monkeypatch):
    monkeypatch.delenv("MTG_SIM_TOKEN", raising=False)
    with TestClient(app) as c:
        response = c.post("/sim/mcp", json={}, headers={"Authorization": "Bearer x"})
    assert response.status_code == 503
    assert response.json() == {"error": "sim disabled: MTG_SIM_TOKEN is not configured"}


def test_mount_survives_two_lifespans(monkeypatch):
    monkeypatch.setenv("MTG_SIM_TOKEN", "t0k")
    headers = {"Authorization": "Bearer t0k", **MCP_HEADERS}
    for _ in range(2):
        with TestClient(app) as c:
            response = c.post("/sim/mcp", json=INIT, headers=headers)
            assert response.status_code == 200, response.text
            assert "mtg-sim" in response.text


def test_second_lifespan_leaves_a_usable_manager(monkeypatch):
    from magic_cards_model_api.sim.server import mount

    monkeypatch.setenv("MTG_SIM_TOKEN", "t0k")
    for _ in range(2):
        with TestClient(app):
            created = call(mount._server, "sim_new", spec=BOLT_SPEC)
            assert "error" not in created, created
            sid = created["sandbox"]["id"]
            assert call(mount._server, "sim_end", sandbox_id=sid) == {"ended": True}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_app(monkeypatch) -> Iterator[str]:
    monkeypatch.setenv("MTG_SIM_TOKEN", "t0k")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}/sim/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_mcp_over_a_real_socket_with_bearer(live_app):
    async def drive() -> set[str]:
        http = httpx2.AsyncClient(headers={"Authorization": "Bearer t0k"})
        async with http, Client(streamable_http_client(live_app, http_client=http)) as client:
            return {t.name for t in (await client.list_tools()).tools}

    names = asyncio.run(drive())
    assert {"sim_new", "sim_state", "sim_legal_actions", "sim_act", "sim_end"} <= names


def test_sim_path_does_not_shadow_the_ui(monkeypatch):
    monkeypatch.setenv("MTG_SIM_TOKEN", "t0k")
    with TestClient(app) as c:
        assert c.get("/").status_code == 200


SCRIPT_FILE = Path(__file__).parent / "fixtures" / "sim" / "bolt_bears_script.json"
SCRIPT = json.loads(SCRIPT_FILE.read_text())


def test_run_script_tool_frees_the_slot_unless_keep(sim):
    done = call(sim, "sim_run_script", script=SCRIPT)
    assert done["stopped_at"] is None and "sandbox" not in done
    again = call(sim, "sim_run_script", script=SCRIPT, keep=True)
    sid = again["sandbox"]["id"]
    assert call(sim, "sim_new", spec=BOLT_SPEC)["error"].startswith("The sandbox slot is in use")
    state = call(sim, "sim_state", sandbox_id=sid)
    assert [c["name"] for c in state["state"]["players"]["bob"]["graveyard"]] == ["Grizzly Bears"]
    assert call(sim, "sim_end", sandbox_id=sid) == {"ended": True}


def test_run_script_refuses_an_invalid_script_as_text(sim):
    out = call(sim, "sim_run_script", script={"setup": BOLT_SPEC, "steps": [{"explode": {}}]})
    assert "error" in out
    assert "sandbox" not in call(sim, "sim_run_script", script=SCRIPT)


def test_run_script_answers_have_no_credentials(sim):
    answers = [call(sim, "sim_run_script", script=SCRIPT, keep=True)]
    text = json.dumps(answers)
    for key in ("admin_key", "seat_uids", "uid", "key"):
        assert f'"{key}"' not in text
    stray = set(re.findall(r"\b[0-9a-f]{32}\b", text)) - _card_ids(answers)
    stray -= {s.removeprefix("sim-") for s in re.findall(r"sim-[0-9a-f]{32}", text)}
    assert not stray
    call(sim, "sim_end", sandbox_id=answers[0]["sandbox"]["id"])


@pytest.fixture
def match_sim(monkeypatch):
    from magic_cards_model_api.sim.manager import BatchManager

    monkeypatch.setenv("MTG_SIM_TEST_OPS", "1")
    monkeypatch.setenv("MTG_SIM_TEST_MATCH_MAX_STEPS", "40")
    limits = SimLimits.from_env({"MTG_SIM_CALL_TIMEOUT_S": "6"})
    manager = SandboxManager(limits)
    batches = BatchManager(limits)
    server = build_sim_server(manager, limits, batches)
    yield server
    batches.shutdown()
    manager.shutdown()


def test_match_tools_are_listed_and_documented(sim):
    docs = {t.name: t.description for t in asyncio.run(sim.list_tools())}
    assert "sim_match" in docs and "sim_match_status" in docs
    assert "partial" in docs["sim_match"] and "baseline" in docs["sim_match"]
    assert "MTG_SIM_MAX_MATCH_GAMES" in docs["sim_match"] or "10" in docs["sim_match"]


def test_sim_match_tool_returns_running_then_done(match_sim):
    deck_a = (Path(__file__).parent / "fixtures" / "sim" / "red_burn.txt").read_text()
    deck_b = (Path(__file__).parent / "fixtures" / "sim" / "green_stompy.txt").read_text()
    answers = [call(match_sim, "sim_match", deck_a=deck_a, deck_b=deck_b, games=2, seed=3)]
    bid = answers[0]["batch_id"]
    assert answers[0]["status"] in ("running", "done")
    deadline = time.monotonic() + 120
    while answers[-1]["status"] == "running" and time.monotonic() < deadline:
        answers.append(call(match_sim, "sim_match_status", batch_id=bid))
    final = answers[-1]
    assert final["status"] == "done"
    wins = final["deck_a"]["wins"] + final["deck_b"]["wins"]
    assert wins + final["draws"] + final["unfinished"] == 2
    assert len(final["games"]) == 2
    assert "support_warnings" in final
    text = json.dumps(answers)
    for key in ("admin_key", "seat_uids", "uid", "key", "advisor_uids"):
        assert f'"{key}"' not in text
    assert not re.findall(r"\b[0-9a-f]{32}\b", text.replace(bid, ""))


def test_match_refusals_come_back_as_text(match_sim):
    out = call(match_sim, "sim_match", deck_a="nope.txt", deck_b="nope.txt", games=1)
    assert "error" in out
    out = call(match_sim, "sim_match", deck_a="a\nb", deck_b="a\nb", games=99)
    assert "MTG_SIM_MAX_MATCH_GAMES" in out["error"]
    assert "error" in call(match_sim, "sim_match_status", batch_id="batch-unknown")


def test_readme_lists_every_sim_tool(sim):
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    start = readme.index("## Chat simulation MCP")
    section = readme[start : readme.index("\n## ", start + 1)]
    missing = [t.name for t in asyncio.run(sim.list_tools()) if t.name not in section]
    assert not missing, f"README sim section lacks tools: {missing}"
