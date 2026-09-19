"""`oneshot.run_review` against a real OS subprocess - a stub Python script
standing in for `claude`, never the real CLI (the same approach
`test_buddy_lifecycle.py`'s `STUB_COMMAND` and `ai_seat.py`'s own tests use),
so these tests exercise the real argv-building, env, and error handling
without ever shelling out to a real model."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.oneshot import ReviewFailed, run_review


def _stub(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-claude"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    script.chmod(0o755)
    return str(script)


def _settings(claude_path: str, **overrides) -> Settings:
    return Settings.from_env({"CLAUDE_PATH": claude_path, **overrides})


def test_run_review_returns_stripped_stdout_on_success(tmp_path: Path) -> None:
    stub = _stub(
        tmp_path,
        """
        import sys
        expected = [
            "--print", "--dangerously-skip-permissions",
            "--model", "claude-opus-5", "--effort", "high", "--",
        ]
        assert sys.argv[1:8] == expected
        assert sys.argv[8] == "critique this game"
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
