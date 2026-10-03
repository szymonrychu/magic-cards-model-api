"""scripts/match_time_budget.py on a small metrics fixture."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "match_time_budget"
sys.path.insert(0, str(ROOT / "scripts"))

import match_time_budget as mtb  # noqa: E402


def _report() -> dict:
    return mtb.build_report(
        seats={
            "p1": (FIX / "p1.metrics").read_text(),
            "p2": (FIX / "p2.metrics").read_text(),
        },
        agent_logs={"p1": (FIX / "p1.agent.log").read_text()},
        game_log=(FIX / "game.log").read_text(),
    )


def test_match_seconds_against_the_target() -> None:
    match = _report()["match"]
    assert match == {"seconds": 1986.68, "target_seconds": 900.0, "over_target": True}


def test_per_seat_waits_model_steps_and_promotions() -> None:
    p1 = _report()["seats"]["p1"]
    assert p1["waits"]["pass_until"] == {"calls": 4, "seconds": 100.0}
    assert p1["waits"]["wait_for_priority"] == {"calls": 2, "seconds": 11.5}
    assert p1["waits"]["await_baton"] == {"calls": 1, "seconds": 7.0}
    assert p1["model_step_seconds"] == {"orchestrator": 40.5, "current": 120.25, "future": 30.0}
    assert p1["promotions"] == 3
    p2 = _report()["seats"]["p2"]
    assert p2["waits"]["wait_for_priority"] == {"calls": 0, "seconds": 0.0}
    assert p2["promotions"] == 1
    assert p2["model_step_seconds"]["future"] == 0.0


def test_dispatch_to_first_acting_call_and_baton_refusals_from_the_agent_log() -> None:
    report = _report()["seats"]
    # Read, Write, battlefield_state, pass_baton are not acting; declare_plan is,
    # and its own model step counts (the wait before the first acting call).
    expected = 4.1 + 44.2 + 3.05 + 1.01 + 5.0
    assert report["p1"]["dispatch_to_first_acting_seconds"] == pytest.approx(expected)
    assert report["p1"]["baton_refusals"] == {
        "BATON_BUSY": 2,
        "BATON_NOT_YET": 1,
        "BATON_REQUIRED": 1,
        "BATON_REVOKED": 1,
        "BATON_SPENT": 1,
        "BATON_STALE": 2,
    }
    assert report["p2"]["dispatch_to_first_acting_seconds"] is None
    assert report["p2"]["baton_refusals"] == {}


def test_missing_game_log_line_leaves_the_match_null() -> None:
    report = mtb.build_report(seats={"p1": ""}, agent_logs={}, game_log="")
    assert report["match"] == {"seconds": None, "target_seconds": 900.0, "over_target": None}


def test_cli_prints_text_and_json() -> None:
    args = [
        sys.executable,
        str(ROOT / "scripts" / "match_time_budget.py"),
        "--seat",
        f"p1={FIX / 'p1.metrics'}",
        "--seat",
        f"p2={FIX / 'p2.metrics'}",
        "--agent-log",
        f"p1={FIX / 'p1.agent.log'}",
        "--game-log",
        str(FIX / "game.log"),
    ]
    text = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    assert "1986.7s of 900s" in text
    assert "p1" in text and "BATON_STALE=2" in text
    data = json.loads(
        subprocess.run([*args, "--json"], capture_output=True, text=True, check=True).stdout
    )
    assert data["seats"]["p1"]["promotions"] == 3
