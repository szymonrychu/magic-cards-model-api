"""2b T11: the brief becomes package data, and a human seat is told to run
the installed `mcp-seat` console script rather than `cd {api_repo} &&
.venv/bin/python -m magic_cards_model_api.seat_cli`.

Once `ai_seat.py`'s repo (`magic-cards-lobby`, per 2b's decomposition map)
is a different container than the api's, there is no `{api_repo}` checkout
next to it to `cd` into - the brief has to be self-contained package data,
and the CLI it tells a human/agent seat to run has to be reachable by name
on `PATH`, which is exactly what a `[project.scripts]` entry gives an
installed package.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from magic_cards_model_api import ai_seat, seat_cli

ROOT = Path(__file__).resolve().parents[1]


def test_mcp_seat_console_script_is_registered_and_points_at_seat_cli() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]
    assert scripts.get("mcp-seat") == "magic_cards_model_api.seat_cli:run"


def test_ai_seat_no_longer_carries_a_repo_relative_working_directory() -> None:
    """`API_REPO`/`BRIEF_PATH` go with the `cd {api_repo}` instruction they
    existed for - see `build_prompt`/`docs/ai_seat_brief.md`."""
    assert not hasattr(ai_seat, "API_REPO")
    assert not hasattr(ai_seat, "BRIEF_PATH")


def test_the_brief_is_loaded_as_package_data() -> None:
    brief = ai_seat.load_brief()
    assert "mcp-seat" in brief
    assert "{api_repo}" not in brief
    assert "cd {api_repo}" not in brief
    assert ".venv/bin/python -m magic_cards_model_api.seat_cli" not in brief


def test_build_prompt_still_fills_in_the_seats_own_identifiers() -> None:
    prompt = ai_seat.build_prompt(
        uid="uid-123", game_id="game-456", player="alice", api_base="http://127.0.0.1:9000"
    )
    assert "uid-123" in prompt
    assert "game-456" in prompt
    assert "alice" in prompt
    assert "http://127.0.0.1:9000" in prompt
    assert "mcp-seat" in prompt


def test_seat_cli_run_is_the_entry_point_the_script_names() -> None:
    assert callable(seat_cli.run)
