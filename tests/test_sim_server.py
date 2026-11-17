"""The mtg-sim MCP server: tools over a real worker, bearer auth, the /sim mount."""

from __future__ import annotations

import asyncio
import contextlib
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
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import Context
from sim_claude_kit import fake_registry as fixture_registry

from magic_cards_model_api.main import app
from magic_cards_model_api.sim.config import SimLimits
from magic_cards_model_api.sim.manager import SandboxManager
from magic_cards_model_api.sim.server import SimMount, build_sim_server

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
fake_registry = fixture_registry  # the fixture, bound under the name tests request


@pytest.fixture
def sim():
    limits = SimLimits.from_env({"MTG_SIM_BASELINE_ENABLED": "1"})
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


@pytest.mark.parametrize("sandbox_id", ["", "sb-nope"])
def test_sim_end_rejects_unknown_sandbox_ids(sim, sandbox_id):
    out = call(sim, "sim_end", sandbox_id=sandbox_id)
    assert "ended" not in out
    assert "No sandbox" in out["error"]


def test_tool_docs_name_the_actions_spec_and_labels(sim):
    docs = {t.name: t.description for t in asyncio.run(sim.list_tools())}
    assert "pass" in docs["sim_act"] and "play_land" in docs["sim_act"]
    assert "@" in docs["sim_act"] and "players" in docs["sim_new"]


def test_sim_new_doc_names_library_filler_and_face_up(sim):
    doc = {t.name: t.description for t in asyncio.run(sim.list_tools())}["sim_new"]
    assert "library_filler" in doc and "face_up" in doc and "empty library" in doc


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


def test_snapshot_and_restore_over_tools(sim, monkeypatch):
    monkeypatch.setenv("MTG_SIM_MAX_SNAPSHOTS", "1")  # read by the worker at start
    created = call(sim, "sim_new", spec=BOLT_SPEC)
    sid = created["sandbox"]["id"]
    snap = call(sim, "sim_snapshot", sandbox_id=sid, label="root")
    assert snap["result"] == {"snapshot": "s1", "label": "root"}
    over = call(sim, "sim_snapshot", sandbox_id=sid)
    assert (
        over["result"]["allowed"] is False and "MTG_SIM_MAX_SNAPSHOTS" in over["result"]["reason"]
    )
    body = {"card_id": "@bolt", "targets": ["@bears"]}
    call(sim, "sim_act", sandbox_id=sid, action="cast", player="alice", body=body)
    call(sim, "sim_act", sandbox_id=sid, action="pass", player="alice")
    call(sim, "sim_act", sandbox_id=sid, action="pass", player="bob")
    back = call(sim, "sim_restore", sandbox_id=sid, snapshot="root")
    assert back["state"] == created["state"]
    assert call(sim, "sim_state", sandbox_id=sid)["snapshots"][0]["label"] == "root"
    assert call(sim, "sim_end", sandbox_id=sid) == {"ended": True}
    assert "No sandbox" in call(sim, "sim_restore", sandbox_id=sid, snapshot="root")["error"]


def test_card_lookup_tool_needs_no_sandbox(sim):
    out = call(sim, "sim_card_lookup", names=["Lightning Bolt", "Grizzly Bears"])
    assert [c["name"] for c in out["cards"]] == ["Lightning Bolt", "Grizzly Bears"]
    assert all(c["engine_support"] == "handled" for c in out["cards"])
    too_many = call(sim, "sim_card_lookup", names=["Shock"] * 41)
    assert "1 to 40 names" in too_many["error"]


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
    limits = SimLimits.from_env({"MTG_SIM_CALL_TIMEOUT_S": "6", "MTG_SIM_BASELINE_ENABLED": "1"})
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


@pytest.fixture
def baseline_off_sim():
    limits = SimLimits.from_env({})
    manager = SandboxManager(limits)
    server = build_sim_server(manager, limits)
    yield server
    manager.shutdown()


def test_baseline_tools_are_not_registered_by_default(baseline_off_sim):
    names = {t.name for t in asyncio.run(baseline_off_sim.list_tools())}
    assert not names & {"sim_match", "sim_match_status", "sim_play_new"}
    assert {"sim_new", "sim_run_script", "sim_match_claude_start"} <= names
    assert "sim_play_new" not in baseline_off_sim.instructions
    assert "sim_match " not in baseline_off_sim.instructions


def test_baseline_tools_are_registered_with_the_flag(sim):
    names = {t.name for t in asyncio.run(sim.list_tools())}
    assert {"sim_match", "sim_match_status", "sim_play_new"} <= names
    assert "sim_play_new" in sim.instructions


def test_board_photo_guide_is_in_the_instructions_and_the_readme():
    from magic_cards_model_api.sim.server import INSTRUCTIONS

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    guide = readme[readme.index("### Usage guide (for the agent)") :]
    for text in (INSTRUCTIONS, guide):
        assert "photo" in text
        for step in ("sim_card_lookup", "sim_new", "sim_snapshot", "sim_restore", "library_filler"):
            assert step in text, step


@contextlib.contextmanager
def progress_app(monkeypatch, server, manager, claude=None) -> Iterator[str]:
    """Serve a fresh SimMount at /sim on a real socket; yield its /sim/mcp URL."""
    monkeypatch.setenv("MTG_SIM_TOKEN", "t0k")
    mount = SimMount(server, manager, claude=claude)
    fastapi_app = FastAPI(lifespan=mount.lifespan)
    fastapi_app.mount("/sim", mount)
    port = _free_port()
    uv = uvicorn.Server(
        uvicorn.Config(fastapi_app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert uv.started
    try:
        yield f"http://127.0.0.1:{port}/sim/mcp"
    finally:
        uv.should_exit = True
        thread.join(timeout=10)


def test_progress_notifications_stream_over_the_sim_mount(monkeypatch):
    limits = SimLimits.from_env({})
    manager = SandboxManager(limits)
    server = build_sim_server(manager, limits)

    @server.tool()
    async def probe_progress(ctx: Context) -> dict[str, Any]:
        for n in (1, 2, 3):
            await ctx.report_progress(n, None, f"tick {n}")
            await asyncio.sleep(0.05)
        return {"ok": True}

    seen: list[tuple[float, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, message))

    async def drive(url: str) -> Any:
        http = httpx2.AsyncClient(headers={"Authorization": "Bearer t0k"})
        async with http, Client(streamable_http_client(url, http_client=http)) as client:
            return await client.call_tool("probe_progress", {}, progress_callback=on_progress)

    with progress_app(monkeypatch, server, manager) as url:
        result = asyncio.run(drive(url))
    assert not result.is_error
    assert [p for p, _ in seen] == [1, 2, 3]
    assert seen[0][1] == "tick 1"


def test_wait_streams_heartbeats_and_returns_over_http(monkeypatch, fake_registry):
    from sim_claude_kit import GREEN, RED, _end_game, _game_id, _start

    from magic_cards_model_api.sim.claude import ClaudeMatchManager

    limits = SimLimits.from_env({"MTG_SIM_WAIT_HEARTBEAT_S": "0.2"})
    manager = SandboxManager(limits)
    claude_manager = ClaudeMatchManager(limits)
    server = build_sim_server(manager, limits, claude=claude_manager)
    match_id = _start(claude_manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(claude_manager, match_id)

    def concede() -> None:
        _end_game(game_id, "b")
        claude_manager.reap()

    seen: list[tuple[float, str | None]] = []

    async def on_progress(progress, total, message):
        seen.append((progress, message))

    async def drive(url: str) -> Any:
        http = httpx2.AsyncClient(headers={"Authorization": "Bearer t0k"}, timeout=30)
        async with http, Client(streamable_http_client(url, http_client=http)) as client:
            return await client.call_tool(
                "sim_match_claude_wait", {"match_id": match_id}, progress_callback=on_progress
            )

    timer = threading.Timer(1.0, concede)
    with progress_app(monkeypatch, server, manager, claude=claude_manager) as url:
        timer.start()
        try:
            result = asyncio.run(drive(url))
        finally:
            timer.join()
    assert not result.is_error
    assert len(seen) >= 3
    assert seen[0][1] is not None and seen[0][1].startswith("waiting:")
    assert [p for p, _ in seen] == sorted({p for p, _ in seen})
    answer = json.loads(result.content[0].text)
    assert answer["status"] == "done" and answer["match_id"] == match_id
    assert answer["log"]["format"] == "compact-v1"


def test_wait_tool_declares_max_result_size(sim):
    tools = {t.name: t for t in asyncio.run(sim.list_tools())}
    meta = tools["sim_match_claude_wait"].meta or {}
    assert meta.get("anthropic/maxResultSizeChars") == 60000


def test_wait_tool_refuses_a_non_positive_max_wait(sim):
    out = call(sim, "sim_match_claude_wait", match_id="claude-nope", max_wait_s=0)
    assert "max_wait_s" in out["error"]
    out = call(sim, "sim_match_claude_wait", match_id="claude-nope")
    assert "No Claude match" in out["error"]


def test_export_route_needs_the_bearer(monkeypatch):
    monkeypatch.setenv("MTG_SIM_TOKEN", "t0k")
    with TestClient(app) as c:
        assert c.get("/sim/matches/claude-x/export.md").status_code == 401
        assert c.get("/sim/matches/claude-x/export.jsonl").status_code == 401
        bad = c.get("/sim/matches/claude-x/export.md", headers={"Authorization": "Bearer no"})
        assert bad.status_code == 401
    monkeypatch.delenv("MTG_SIM_TOKEN")
    with TestClient(app) as c:
        assert c.get("/sim/matches/claude-x/export.md").status_code == 503


def _export_fixture(monkeypatch):
    from sim_claude_kit import GREEN, RED, _finish, _game_id, _start

    from magic_cards_model_api import audit
    from magic_cards_model_api.sim.claude import ClaudeMatchManager

    limits = SimLimits.from_env({})
    manager = SandboxManager(limits)
    claude_manager = ClaudeMatchManager(limits)
    server = build_sim_server(manager, limits, claude=claude_manager)
    match_id = _start(claude_manager, RED, GREEN, seed=1)["match_id"]
    audit.record(_game_id(claude_manager, match_id), "play_land", player="a", card="Forest")
    _finish(claude_manager, match_id)
    return server, manager, claude_manager, match_id


def test_export_route_serves_markdown_and_jsonl(monkeypatch, fake_registry):
    server, manager, claude_manager, match_id = _export_fixture(monkeypatch)
    root = {"Authorization": "Bearer t0k"}
    with progress_app(monkeypatch, server, manager, claude=claude_manager) as url:
        base = url.removesuffix("/mcp")
        md = httpx2.get(f"{base}/matches/{match_id}/export.md", headers=root)
        js = httpx2.get(f"{base}/matches/{match_id}/export.jsonl", headers=root)
        missing = httpx2.get(f"{base}/matches/claude-nope/export.md", headers=root)
        anon = httpx2.get(f"{base}/matches/{match_id}/export.md")
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert f'filename="{match_id}.md"' in md.headers["content-disposition"]
    assert md.text.startswith(f"# Claude match {match_id}")
    assert js.status_code == 200 and js.headers["content-type"].startswith("application/x-ndjson")
    assert f'filename="{match_id}.jsonl"' in js.headers["content-disposition"]
    assert all(json.loads(line)["seq"] for line in js.text.splitlines())
    assert missing.status_code == 404 and "error" in missing.json()
    assert anon.status_code == 401


def test_export_route_without_a_claude_manager_is_404(monkeypatch):
    limits = SimLimits.from_env({})
    manager = SandboxManager(limits)
    server = build_sim_server(manager, limits)
    with progress_app(monkeypatch, server, manager) as url:
        base = url.removesuffix("/mcp")
        out = httpx2.get(
            f"{base}/matches/claude-x/export.md", headers={"Authorization": "Bearer t0k"}
        )
    assert out.status_code == 404


def test_export_tool_answers_the_url_only(monkeypatch, fake_registry):
    server, _manager, _claude, match_id = _export_fixture(monkeypatch)
    out = call(server, "sim_match_claude_export", match_id=match_id, format="jsonl")
    assert out["url"].endswith(f"/sim/matches/{match_id}/export.jsonl")
    assert out["media_type"] == "application/x-ndjson" and "$MTG_SIM_TOKEN" in out["fetch"]
    assert "lines" not in out and "body" not in out
    default = call(server, "sim_match_claude_export", match_id=match_id)
    assert default["format"] == "markdown" and default["url"].endswith("export.md")
    bad = call(server, "sim_match_claude_export", match_id=match_id, format="pdf")
    assert "markdown" in bad["error"] and "jsonl" in bad["error"]
    assert (
        "No Claude match" in call(server, "sim_match_claude_export", match_id="claude-no")["error"]
    )
