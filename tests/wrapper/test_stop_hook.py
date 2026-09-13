import json
import subprocess
import sys
from pathlib import Path

import pytest

from mtg_agent.wrapper import stop_hook


def payload(**overrides) -> bytes:
    base = {
        "session_id": "a70a2699",
        "transcript_path": "",
        "cwd": "/workspace",
        "hook_event_name": "Stop",
        "last_assistant_message": "PONG",
    }
    return json.dumps(base | overrides).encode()


def test_final_text_prefers_the_payload_over_the_transcript(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "from transcript"}],
                    "usage": {"output_tokens": 3},
                    "stop_reason": "end_turn",
                },
            }
        )
        + "\n"
    )
    result = stop_hook.build_result(payload(transcript_path=str(path)), tmp_path / "absent.json")
    assert result["finalText"] == "PONG"
    assert result["usage"]["output_tokens"] == 3
    assert result["stopReason"] == "end_turn"
    assert result["sessionId"] == "a70a2699"
    assert result["transcriptPath"] == str(path)


def test_final_text_falls_back_to_the_transcript_when_the_payload_is_empty(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "from transcript"}],
                    "usage": {},
                    "stop_reason": "end_turn",
                },
            }
        )
        + "\n"
    )
    result = stop_hook.build_result(
        payload(last_assistant_message="", transcript_path=str(path)), tmp_path / "x.json"
    )
    assert result["finalText"] == "from transcript"


def test_result_json_is_folded_in_when_valid(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text('{"total_cost_usd": 0.42}')
    result = stop_hook.build_result(payload(), result_path)
    assert result["resultJson"] == {"total_cost_usd": 0.42}


def test_invalid_result_json_is_ignored(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text("{not json")
    assert stop_hook.build_result(payload(), result_path)["resultJson"] is None


def test_a_missing_transcript_still_produces_a_result(tmp_path):
    result = stop_hook.build_result(
        payload(transcript_path=str(tmp_path / "gone.jsonl")), tmp_path / "x.json"
    )
    assert result["finalText"] == "PONG"
    assert result["turnTokens"] == []


def test_corrupt_payload_raises(tmp_path):
    with pytest.raises(ValueError):
        stop_hook.build_result(b"{not json", tmp_path / "x.json")


def test_post_with_retry_gives_up_after_the_attempt_budget(monkeypatch):
    calls = []

    def always_fail(url, content, headers, timeout):
        calls.append(url)
        raise OSError("connection refused")

    monkeypatch.setattr(stop_hook, "_post_once", always_fail)
    monkeypatch.setattr(stop_hook.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError):
        stop_hook.post_with_retry("http://x/internal", b"{}", attempts=5, backoff=0)
    assert len(calls) == 5


def test_post_with_retry_stops_at_the_first_success(monkeypatch):
    calls = []

    def fail_then_succeed(url, content, headers, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise OSError("nope")

    monkeypatch.setattr(stop_hook, "_post_once", fail_then_succeed)
    monkeypatch.setattr(stop_hook.time, "sleep", lambda _: None)
    stop_hook.post_with_retry("http://x/internal", b"{}", attempts=5, backoff=0)
    assert len(calls) == 3


def test_main_exits_zero_even_when_everything_fails(tmp_path):
    """A Stop hook must never block or alter claude. Run it for real as a
    subprocess with a corrupt payload and an unreachable endpoint."""
    proc = subprocess.run(
        [sys.executable, "-m", "mtg_agent.wrapper.stop_hook"],
        input=b"{not json at all",
        env={
            "PATH": "/usr/bin:/bin",
            "CCW_INTERNAL_URL": "http://127.0.0.1:1/internal/turn-complete",
            "CCW_RESULT_JSON": str(tmp_path / "none.json"),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        },
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0


def test_the_result_names_the_turn_the_transcript_says_stopped(tmp_path):
    from mtg_agent.wrapper.transcript import turn_marker

    path = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": {"content": f"go\n\n{turn_marker('turn-abc')}"}},
        {"type": "assistant", "message": {"content": [], "usage": {}, "stop_reason": "end_turn"}},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    result = stop_hook.build_result(payload(transcript_path=str(path)), tmp_path / "absent.json")
    assert result["turnId"] == "turn-abc"


def test_the_result_names_no_turn_without_a_transcript(tmp_path):
    result = stop_hook.build_result(payload(), tmp_path / "absent.json")
    assert result["turnId"] == ""
