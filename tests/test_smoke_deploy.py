"""scripts/smoke_deploy.py: the post-deploy smoke check run in the pod.

The check logic is pure and is tested against fakes. One test drives the real
MCP probe against the real app on a live socket, so the streamable-HTTP
handshake (initialize, initialized, tools/list) is exercised for real.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from magic_cards_model_api.main import app
from scripts import smoke_deploy as smoke

TOKEN = "s3cr3t-bearer-value"
GOOD_TOOLS = [*smoke.PRESENT_TOOLS, "sim_new", "sim_act", "sim_end"]


def _seat_job(env_by_container: dict[str, list[str]], init: dict[str, list[str]] | None = None):
    def containers(spec):
        return [
            {"name": name, "env": [{"name": var, "value": "x"} for var in names]}
            for name, names in spec.items()
        ]

    return {
        "spec": {
            "template": {
                "spec": {
                    "initContainers": containers(init or {}),
                    "containers": containers(env_by_container),
                }
            }
        }
    }


def _run(**overrides):
    probes = {
        "get_health": lambda: (200, {"status": "ok"}),
        "list_tools": lambda: list(GOOD_TOOLS),
        "default_settings": lambda: ("claude-opus-5-5", "low"),
        "seat_job": lambda: _seat_job({"agent": ["EFFORT", "MODEL"]}),
        "secret": TOKEN,
    }
    probes.update(overrides)
    lines: list[str] = []
    code = smoke.run_checks(out=lines.append, **probes)
    return code, lines


def test_all_good_exits_zero_with_one_pass_line_per_check():
    code, lines = _run()

    assert code == 0
    pass_lines = [line for line in lines if line.startswith("PASS ")]
    assert len(pass_lines) == 1 + 7 + 1 + 1
    assert not any(line.startswith("FAIL") for line in lines)
    assert lines[-1] == "smoke: all checks passed"


@pytest.mark.parametrize("gone", smoke.ABSENT_TOOLS)
def test_a_retired_tool_still_listed_fails(gone):
    code, lines = _run(list_tools=lambda: [*GOOD_TOOLS, gone])

    assert code == 1
    assert f"FAIL tool {gone} absent: still listed" in lines


@pytest.mark.parametrize("wanted", smoke.PRESENT_TOOLS)
def test_a_missing_new_tool_fails(wanted):
    code, lines = _run(list_tools=lambda: [t for t in GOOD_TOOLS if t != wanted])

    assert code == 1
    assert f"FAIL tool {wanted} present: not listed" in lines


def test_health_not_ok_fails():
    code, lines = _run(get_health=lambda: (200, {"status": "starting"}))

    assert code == 1
    assert any(line.startswith("FAIL health") for line in lines)


@pytest.mark.parametrize("got", [("claude-sonnet-5", "high"), ("claude-opus-5-5", "high")])
def test_a_wrong_lobby_default_fails(got):
    code, lines = _run(default_settings=lambda: got)

    assert code == 1
    assert any(line.startswith("FAIL lobby default") for line in lines)


def test_the_real_default_settings_match_the_prod_env(monkeypatch):
    monkeypatch.setenv("AGENT_DEFAULT_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("AGENT_DEFAULT_EFFORT", "low")

    assert smoke._default_settings() == ("claude-opus-5-5", "low")


def test_the_effort_level_env_in_any_seat_container_fails():
    job = _seat_job({"agent": ["EFFORT"]}, init={"mcp-config": [smoke.FORBIDDEN_SEAT_ENV]})

    code, lines = _run(seat_job=lambda: job)

    assert code == 1
    assert any(
        line.startswith("FAIL seat Job has no CLAUDE_CODE_EFFORT_LEVEL") and "mcp-config" in line
        for line in lines
    )


def test_the_job_k8s_seats_really_renders_passes_the_check():
    job = smoke._seat_job()

    assert smoke.check_seat_job_env(job).ok


def test_a_probe_that_raises_is_a_fail_and_the_others_still_run():
    def boom():
        raise ConnectionRefusedError("connection refused")

    code, lines = _run(get_health=boom)

    assert code == 1
    assert "FAIL health: ConnectionRefusedError: connection refused" in lines
    assert sum(line.startswith("PASS ") for line in lines) == 7 + 1 + 1


def test_the_bearer_token_is_never_printed_even_when_an_error_carries_it():
    def leaky():
        raise RuntimeError(f"401 for Authorization: Bearer {TOKEN}")

    code, lines = _run(list_tools=leaky)

    assert code == 1
    assert any(line.startswith("FAIL sim tool list") for line in lines)
    assert TOKEN not in "\n".join(lines)
    assert "[redacted]" in "\n".join(lines)


def test_main_without_a_token_fails_the_tool_check_without_calling_the_network(monkeypatch, capsys):
    monkeypatch.delenv("MTG_SIM_TOKEN", raising=False)
    monkeypatch.setattr(smoke, "_http_get_health", lambda: (200, {"status": "ok"}))

    def no_network(*_a, **_k):
        raise AssertionError("must not call the MCP endpoint without a token")

    monkeypatch.setattr(smoke, "mcp_list_tools", no_network)

    assert smoke.main() == 1
    out = capsys.readouterr().out
    assert "FAIL sim tool list: RuntimeError: MTG_SIM_TOKEN is not set" in out


def test_mcp_message_reads_plain_json_and_sse():
    assert smoke._mcp_message(b'{"a": 1}', "application/json") == {"a": 1}
    sse = b'event: message\ndata: {"a": 2}\n\n'
    assert smoke._mcp_message(sse, "text/event-stream") == {"a": 2}


@pytest.fixture
def live_url(monkeypatch) -> Iterator[str]:
    monkeypatch.setenv("MTG_SIM_TOKEN", TOKEN)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_mcp_list_tools_speaks_the_real_handshake_to_the_real_app(live_url):
    names = smoke.mcp_list_tools(TOKEN, base_url=live_url)

    assert "sim_new" in names
    assert "sim_review_mtgo_log" in names
    assert "sim_snapshot" in names
