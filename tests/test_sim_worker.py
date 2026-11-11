"""Sim worker child process and its parent-side handle (spawns real children)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from magic_cards_model_api.sim.handle import WorkerDied, WorkerError, WorkerHandle, WorkerTimeout
from magic_cards_model_api.sim.worker import worker_env

FIXTURES = Path(__file__).parent / "fixtures" / "sim"


def load_spec(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_worker_runs_a_sandbox_round_trip():
    h = WorkerHandle()
    t0 = time.monotonic()
    h.start(timeout=60)
    cold = time.monotonic() - t0
    try:
        out = h.request("new", load_spec("bolt_bears.json"), timeout=60)
        assert out["labels"]["bolt"]
        assert h.request("state", {}, timeout=25)["state"]["players"]["alice"]["life"] == 20
        acts = h.request("legal_actions", {"player": "alice"}, timeout=25)
        assert acts["result"]["actions"]
        done = h.request("act", {"action": "pass", "player": "alice", "body": {}}, timeout=25)
        assert "state" in done
        rss_kb = int(
            next(
                line
                for line in Path(f"/proc/{h.pid}/status").read_text().splitlines()
                if line.startswith("VmHWM")
            ).split()[1]
        )
        print(f"MEASURED cold_start_s={cold:.2f} rss_after_new_mb={rss_kb / 1024:.0f}")
        assert h.request("end", {}, timeout=25) == {"ended": True}
    finally:
        h.kill()
    assert not h.alive


def test_failed_new_is_an_error_not_a_death():
    h = WorkerHandle()
    h.start(timeout=60)
    try:
        spec = load_spec("bolt_bears.json")
        spec["cards"][0]["card"] = "Definitely Not A Card"
        with pytest.raises(WorkerError):
            h.request("new", spec, timeout=60)
        assert h.alive
        assert h.request("ping", {}, timeout=10) == {"pong": True}
    finally:
        h.kill()


def test_worker_env_strips_database_and_tokens():
    env = worker_env(
        {
            "POSTGRES_HOST": "db",
            "DATABASE_URL": "x",
            "MTG_SIM_TOKEN": "t",
            "OPS_TOKEN": "o",
            "OIDC_ISSUER": "i",
            "AI_SEAT_MODE": "a",
            "REQUIRE_POSTGRES": "1",
            "MCM_TEST_DATABASE_URL": "u",
            "MCM_CACHE_DIR": "/c",
        }
    )
    assert env == {"MCM_CACHE_DIR": "/c", "MTG_SIM_WORKER": "1"}


def test_request_timeout_raises_and_kill_cleans_up(monkeypatch):
    monkeypatch.setenv("MTG_SIM_TEST_OPS", "1")
    h = WorkerHandle()
    h.start(timeout=60)
    with pytest.raises(WorkerTimeout):
        h.request("sleep", {"seconds": 5}, timeout=0.5)
    h.kill()
    h.kill()  # idempotent
    assert not h.alive


def test_sleep_op_is_absent_without_test_ops(monkeypatch):
    monkeypatch.delenv("MTG_SIM_TEST_OPS", raising=False)
    h = WorkerHandle()
    h.start(timeout=60)
    try:
        with pytest.raises(WorkerError, match="unknown op"):
            h.request("sleep", {"seconds": 0}, timeout=10)
    finally:
        h.kill()


def test_worker_exits_over_rss_cap(monkeypatch):
    monkeypatch.setenv("MTG_SIM_WORKER_MAX_RSS_MB", "1")
    h = WorkerHandle()
    with pytest.raises(WorkerDied, match="MTG_SIM_WORKER_MAX_RSS_MB"):
        h.start(timeout=60)
    assert not h.alive


def test_request_after_death_raises_worker_died():
    h = WorkerHandle()
    h.start(timeout=60)
    h.kill()
    with pytest.raises(WorkerDied):
        h.request("ping", {}, timeout=5)


def test_worker_refuses_to_run_on_postgres():
    code = (
        "import runpy, magic_cards_model_api.store as s\n"
        "s.store = object()\n"
        "runpy.run_module('magic_cards_model_api.sim.worker', run_name='__main__')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], input="", capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 3
    assert proc.stdout == ""
