import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_three_services(compose):
    assert set(compose["services"]) == {"game", "agent", "driver"}


def test_every_build_context_is_the_parent_so_the_engine_is_in_scope(compose):
    """magic-cards-model is a path dependency at ../magic-cards-model, so a
    build context of `.` cannot see it."""
    for name, service in compose["services"].items():
        assert service["build"]["context"] == "..", name


def test_the_game_serves_8000_and_can_read_the_guides(compose):
    game = compose["services"]["game"]
    assert "8000:8000" in game["ports"]
    assert any("/guides" in str(v) for v in game["volumes"])


def test_the_agent_mounts_guides_read_only_and_memory_read_write(compose):
    volumes = compose["services"]["agent"]["volumes"]
    guides = [v for v in volumes if "/guides" in str(v)]
    memory = [v for v in volumes if "/workspace/memory" in str(v)]
    assert guides and str(guides[0]).endswith(":ro")
    assert memory and not str(memory[0]).endswith(":ro")


def test_the_agent_needs_an_api_key_from_the_environment(compose):
    env = compose["services"]["agent"]["environment"]
    assert any("ANTHROPIC_API_KEY" in str(e) for e in env)


def test_the_agent_is_told_where_the_hook_and_the_internal_endpoint_are(compose):
    env = " ".join(str(e) for e in compose["services"]["agent"]["environment"])
    assert "HOOK_COMMAND" in env
    assert "CCW_INTERNAL_URL" in env


def test_the_driver_knows_the_game_and_the_wrapper(compose):
    env = " ".join(str(e) for e in compose["services"]["driver"]["environment"])
    assert "GAME_API" in env
    assert "WRAPPER_API" in env
    assert "DRIVER_SELF_URL" in env


def test_the_game_is_put_into_wrapper_mode_and_told_the_driver_url(compose):
    env = " ".join(str(e) for e in compose["services"]["game"]["environment"])
    assert "AI_SEAT_MODE" in env
    assert "DRIVER_URL" in env


def test_the_base_mcp_points_the_mtg_server_at_the_game_container():
    doc = json.loads((ROOT / "deploy" / "mcp.base.json").read_text())
    servers = doc["mcpServers"]
    assert "http://game:8000" in json.dumps(servers["mtg"])
    assert servers["spellslinger"]["type"] == "http"


def test_env_example_lists_every_variable_compose_requires(compose):
    example = (ROOT / ".env.example").read_text()
    for key in ("ANTHROPIC_API_KEY", "MTG_DECKS", "CLAUDE_MODEL"):
        assert key in example


def test_the_real_env_file_is_gitignored():
    assert "\n.env\n" in "\n" + (ROOT / ".gitignore").read_text()
