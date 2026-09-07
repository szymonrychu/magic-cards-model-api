import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_five_services(compose):
    """`postgres`/`migrate` (T9) joined the original three - see docker-
    compose.yml's own module comment for why they are not yet wired into
    `game`."""
    assert set(compose["services"]) == {"game", "agent", "driver", "postgres", "migrate"}


def test_every_built_services_context_is_the_parent_so_the_engine_is_in_scope(compose):
    """magic-cards-model is a path dependency at ../magic-cards-model, so a
    build context of `.` cannot see it. `postgres` is a stock image with no
    `build` of its own, so only the services this repo actually builds need
    this."""
    for name, service in compose["services"].items():
        if "build" not in service:
            continue
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


def test_the_agent_carries_the_effort_setting(compose):
    env = " ".join(str(e) for e in compose["services"]["agent"]["environment"])
    assert "EFFORT" in env


def test_env_example_lists_every_variable_compose_requires(compose):
    example = (ROOT / ".env.example").read_text()
    for key in ("ANTHROPIC_API_KEY", "MTG_DECKS", "CLAUDE_MODEL", "CLAUDE_EFFORT"):
        assert key in example


def test_the_real_env_file_is_gitignored():
    assert "\n.env\n" in "\n" + (ROOT / ".gitignore").read_text()


def test_migrate_is_profile_gated_so_a_plain_up_never_starts_it(compose):
    """The plan's binding requirement: migrations run "as a one-shot ... never
    from app startup and never from an init container". `profiles` is what
    keeps `docker compose up` (no `--profile`) from ever starting `migrate`
    on its own - dropping this key is exactly the regression this pins."""
    assert compose["services"]["migrate"].get("profiles") == ["migrate"]


def test_no_service_depends_on_migrate(compose):
    """Nothing may wait on `migrate` in its `depends_on` - that would turn
    the one-shot into something `docker compose up` starts transitively,
    the same failure mode `profiles` alone is not enough to rule out if a
    later change adds a dependency edge instead of removing the gate."""
    for name, service in compose["services"].items():
        depends_on = service.get("depends_on") or {}
        assert "migrate" not in depends_on, name


def test_no_application_entrypoint_runs_alembic_upgrade_head():
    """Migrations must never run from `game`'s own startup or from an init
    container - only from the one-shot `migrate` service/`mise run
    db:migrate`. `docker-compose.yml`'s `game`/`agent`/`driver` services have
    no `command` override (they use the image's own `CMD`), so this checks
    the one place a startup path could hide: the `Dockerfile`s' `CMD`/
    `ENTRYPOINT`, plus any `command:` a service might someday set."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for name, service in compose["services"].items():
        command = service.get("command")
        if command is None:
            continue
        joined = " ".join(command) if isinstance(command, list) else str(command)
        assert "alembic" not in joined or name == "migrate", name

    for dockerfile in ("Dockerfile", "Dockerfile.agent"):
        text = (ROOT / dockerfile).read_text()
        assert "alembic" not in text, dockerfile
