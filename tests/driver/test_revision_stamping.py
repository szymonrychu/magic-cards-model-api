"""Every game's artifacts name the guides revision they were played against.

The corpus moves - a guide edited between two seats being scheduled changes
what the agent was told, with nothing in the game's own record saying so.
The driver reads the revision off the guides root (a stamp file in
kubernetes, the checkout's own `.git` in compose) and writes it into both
artifacts a game leaves behind: the briefing the agent is handed on turn 0,
and the header of the per-game plan file it writes into memory.
"""

from __future__ import annotations

import json

import pytest

from mtg_agent.driver.revision import GUIDES_REVISION_FILE, UNKNOWN_REVISION
from tests.driver.world import build_world

REVISION = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def world(tmp_path):
    built = build_world(tmp_path)
    (built.settings.guides_root / GUIDES_REVISION_FILE).write_text(REVISION + "\n")
    return built


def dispatch(client, uid: str = "secret-uid", game_id: str = "g-1"):
    return client.post(
        "/dispatch",
        json={
            "uid": uid,
            "game_id": game_id,
            "player": "bob",
            "api_base": "http://game:8000",
            "agent": None,
        },
    )


def plan_file(world):
    return world.settings.memory_root / "dimir-frog" / "games" / "g-1.md"


def test_the_briefing_names_the_guides_revision(world) -> None:
    dispatch(world.client)
    briefing = (world.settings.workspace / "current-game.md").read_text()
    assert REVISION in briefing


def test_turn_zero_carries_the_revision_because_it_inlines_the_briefing(world) -> None:
    dispatch(world.client)
    assert REVISION in world.submitted[0]["text"]


def test_the_plan_file_is_seeded_with_a_header_naming_the_revision(world) -> None:
    dispatch(world.client)
    header = plan_file(world).read_text()
    assert REVISION in header
    assert "g-1" in header


def test_the_plan_file_header_is_not_mistaken_for_a_note_from_a_past_game(world) -> None:
    """`memory_index` lists every `*.md` under the deck's memory directory as
    "what you have already learned"; the header this dispatch just wrote is
    this game's own file, not a past one."""
    dispatch(world.client)
    briefing = (world.settings.workspace / "current-game.md").read_text()
    learned = briefing.split("## What you have already learned on this deck")[1]
    learned = learned.split("##")[0]
    assert "games/g-1.md" not in learned


def test_a_second_dispatch_of_the_same_seat_keeps_the_agents_own_plan(world) -> None:
    dispatch(world.client)
    plan_file(world).write_text("# my plan\n\nkill them\n")
    dispatch(world.client)
    assert plan_file(world).read_text() == "# my plan\n\nkill them\n"


def test_a_compose_checkout_answers_from_its_own_git(tmp_path) -> None:
    """No stamp file, a real `.git` - the compose bind mount - reaches the
    same two artifacts by the same route."""
    import subprocess

    built = build_world(tmp_path)
    root = built.settings.guides_root
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for args in (
        ("config", "user.email", "t@example.invalid"),
        ("config", "user.name", "test"),
        ("add", "-A"),
        ("commit", "-qm", "guides"),
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    dispatch(built.client)
    assert head in (built.settings.workspace / "current-game.md").read_text()
    assert head in (built.settings.memory_root / "dimir-frog" / "games" / "g-1.md").read_text()


def test_guides_with_no_revision_at_all_still_play(tmp_path) -> None:
    built = build_world(tmp_path)
    response = dispatch(built.client)
    assert response.status_code == 202, response.text
    assert json.loads(response.text)["deck"] == "dimir-frog"
    briefing = (built.settings.workspace / "current-game.md").read_text()
    assert UNKNOWN_REVISION in briefing
