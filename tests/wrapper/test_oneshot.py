"""`oneshot.run_review` against a real OS subprocess - a stub Python script
standing in for `claude`, never the real CLI (the same approach
`test_buddy_lifecycle.py`'s `STUB_COMMAND` and `ai_seat.py`'s own tests use),
so these tests exercise the real argv-building, env, and error handling
without ever shelling out to a real model."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from mtg_agent.wrapper import bootstrap
from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.oneshot import ReviewFailed, run_review


def _stub(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-claude"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    script.chmod(0o755)
    return str(script)


def _settings(claude_path: str, **overrides) -> Settings:
    return Settings.from_env({"CLAUDE_PATH": claude_path, **overrides})


#: The full argv `run_review` must always build, up to the prompt itself -
#: no `--dangerously-skip-permissions` (the review needs no tool permission
#: to bypass, since it is given no tools at all), `--tools ""` (disables
#: every built-in tool - `claude --help`'s own words: 'Use "" to disable all
#: tools'), `--strict-mcp-config` with no `--mcp-config` passed (so no MCP
#: server loads, project or user).
EXPECTED_FLAGS = [
    "--print",
    "--model",
    "claude-opus-5",
    "--effort",
    "high",
    "--tools",
    "",
    "--strict-mcp-config",
    "--",
]


def test_run_review_returns_stripped_stdout_on_success(tmp_path: Path) -> None:
    stub = _stub(
        tmp_path,
        f"""
        import sys
        expected = {EXPECTED_FLAGS!r}
        assert sys.argv[1 : 1 + len(expected)] == expected
        assert sys.argv[1 + len(expected)] == "critique this game"
        print("  the review text  \\n")
        """,
    )
    settings = _settings(stub, WORKSPACE=str(tmp_path))
    text = run_review(settings, "critique this game", "claude-opus-5", "high")
    assert text == "the review text"


def test_run_review_raises_on_a_nonzero_exit(tmp_path: Path) -> None:
    stub = _stub(
        tmp_path,
        """
        import sys
        sys.stderr.write("API Error: 401\\n")
        sys.exit(1)
        """,
    )
    settings = _settings(stub, WORKSPACE=str(tmp_path))
    with pytest.raises(ReviewFailed, match="claude exited 1.*401"):
        run_review(settings, "x", "m", "high")


def test_run_review_raises_on_a_missing_binary(tmp_path: Path) -> None:
    settings = _settings(str(tmp_path / "no-such-claude"), WORKSPACE=str(tmp_path))
    with pytest.raises(ReviewFailed, match="not on PATH"):
        run_review(settings, "x", "m", "high")


def test_run_review_raises_on_timeout(tmp_path: Path) -> None:
    stub = _stub(tmp_path, "import time; time.sleep(5)")
    settings = _settings(stub, WORKSPACE=str(tmp_path), REVIEW_TIMEOUT_SECONDS="0.1")
    with pytest.raises(ReviewFailed, match="did not finish within 0.1s"):
        run_review(settings, "x", "m", "high")


def test_run_review_raises_on_an_unexecutable_binary(tmp_path: Path) -> None:
    """`OSError` (e.g. `PermissionError` - a `claude_path` that exists but
    is not executable, a `-x` mode file this container's own uid cannot
    run) is a real, seen-in-production failure mode distinct from `claude`
    simply not existing (`FileNotFoundError`, covered above) - both must
    become a `ReviewFailed`, never an uncaught exception that 500s the
    wrapper's route."""
    not_executable = tmp_path / "not-executable"
    not_executable.write_text("#!/bin/sh\necho hi\n")
    not_executable.chmod(0o644)
    settings = _settings(str(not_executable), WORKSPACE=str(tmp_path))
    with pytest.raises(ReviewFailed):
        run_review(settings, "x", "m", "high")


def test_run_review_caps_an_oversized_prompt(tmp_path: Path) -> None:
    captured = tmp_path / "captured-argv-len.json"
    stub = _stub(
        tmp_path,
        f"""
        import json, sys
        json.dump(len(sys.argv[-1]), open({str(captured)!r}, "w"))
        print("ok")
        """,
    )
    settings = _settings(stub, WORKSPACE=str(tmp_path))
    huge = "x" * 10_000_000
    run_review(settings, huge, "m", "high")
    received_len = json.loads(captured.read_text())
    assert received_len < len(huge), "an oversized prompt must be capped, not sent whole"


# ---------------------------------------------------------------------------
# isolation from the play session's HOME - the Stop hook must never fire
# ---------------------------------------------------------------------------


def test_run_review_uses_an_isolated_home_never_the_play_sessions(tmp_path: Path) -> None:
    """The real bug: `settings.home_dir` is where `bootstrap.write_settings_
    json` writes `.claude/settings.json` with the play session's Stop hook
    (`hooks.Stop[0].hooks[0].command = settings.hook_command`) - a `claude
    --print` invoked with that SAME HOME reads that same file and fires
    that same hook when it finishes, which can report a turn completion
    with a missing/wrong turn id and interfere with the real handover turn
    (`session.Manager.complete`). `run_review` must use a HOME of its own
    that carries no such file, every time."""
    play_home = tmp_path / "play-home"
    play_home.mkdir()
    settings = Settings.from_env(
        {
            "CLAUDE_PATH": "unused",
            "HOME_DIR": str(play_home),
            "WORKSPACE": str(tmp_path / "workspace"),
            "HOOK_COMMAND": "cc-stop-hook",
        }
    )
    # The real bootstrap step, run for real against `play_home` - proves
    # this test is checking against the actual file the play session
    # writes, not a hand-typed stand-in for it.
    bootstrap.write_settings_json(settings)
    assert (play_home / ".claude" / "settings.json").is_file(), "sanity: bootstrap wrote it"
    play_settings_doc = json.loads((play_home / ".claude" / "settings.json").read_text())
    assert play_settings_doc["hooks"]["Stop"][0]["hooks"][0]["command"] == "cc-stop-hook"

    captured = tmp_path / "captured-home.json"
    stub_path = tmp_path / "fake-claude"
    stub_path.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        f"json.dump({{'home': os.environ.get('HOME'), 'cwd': os.getcwd(),"
        " 'has_hook_command': 'HOOK_COMMAND' in os.environ,"
        " 'has_ccw_internal_url': 'CCW_INTERNAL_URL' in os.environ,"
        " 'has_ccw_result_json': 'CCW_RESULT_JSON' in os.environ},"
        f" open({str(captured)!r}, 'w'))\n"
        "print('ok')\n"
    )
    stub_path.chmod(0o755)
    settings = Settings.from_env(
        {
            "CLAUDE_PATH": str(stub_path),
            "HOME_DIR": str(play_home),
            "WORKSPACE": str(tmp_path / "workspace"),
            "HOOK_COMMAND": "cc-stop-hook",
        }
    )
    (tmp_path / "workspace").mkdir(exist_ok=True)

    run_review(settings, "critique this game", "m", "high")

    result = json.loads(captured.read_text())
    used_home = Path(result["home"])
    assert used_home != play_home, "run_review must never use the play session's own HOME"
    assert not (used_home / ".claude" / "settings.json").is_file(), (
        "the isolated HOME must carry no settings.json at all - the play session's "
        "Stop hook config must be unreachable from it"
    )
    assert result["cwd"] != str(tmp_path / "workspace"), (
        "cwd must not be the play workspace either - it can carry its own project-level "
        ".mcp.json (bootstrap.merge_mcp) that --strict-mcp-config alone does not cover "
        "if cwd is ever changed back to it"
    )
    assert result["has_hook_command"] is False, "HOOK_COMMAND must not reach the subprocess env"
    assert result["has_ccw_internal_url"] is False
    assert result["has_ccw_result_json"] is False


def test_run_reviews_isolated_home_is_cleaned_up_afterwards(tmp_path: Path) -> None:
    captured = tmp_path / "captured-home-path.json"
    stub = _stub(
        tmp_path,
        f"""
        import json, os
        json.dump(os.environ.get("HOME"), open({str(captured)!r}, "w"))
        print("ok")
        """,
    )
    settings = _settings(stub, WORKSPACE=str(tmp_path))
    run_review(settings, "x", "m", "high")
    used_home = Path(json.loads(captured.read_text()))
    assert not used_home.exists(), "the per-call isolated HOME must not be left on disk"
