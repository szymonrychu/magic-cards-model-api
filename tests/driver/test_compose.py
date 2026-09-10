import json
import re
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


def test_the_base_mcp_pins_the_mtg_server_to_the_stdio_transport():
    """2b T11: `mcp.py --transport` now defaults to `http`, which serves an
    ASGI app on a socket rather than speaking MCP over the spawned process's
    own stdin/stdout. This config spawns `mcp.py` as a subprocess
    (`command`/`args`, not a `url`) - the wrapper's MCP client talks to it
    over stdio, so it must keep asking for that transport explicitly, or the
    default flip silently breaks every continuous-agent compose session."""
    doc = json.loads((ROOT / "deploy" / "mcp.base.json").read_text())
    args = doc["mcpServers"]["mtg"]["args"]
    assert "--transport" in args
    assert args[args.index("--transport") + 1] == "stdio"


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


# -- no committed password literal ------------------------------------------
#: GitGuardian failed T9's pull request on three hardcoded `mtg` postgres
#: passwords, in `.env.example`, `.github/workflows/ci.yml` and
#: `docker-compose.yml`. They were throwaway local values, but a security check
#: that is always red is a security check nobody reads, and phase 2b creates six
#: more repos that each get a compose file. The password now comes from
#: `scripts/postgres-password.sh`, which generates one into the gitignored
#: `.postgres-password` on first use. These pin that it stays that way.


def _repo_file(name: str) -> str:
    return (ROOT / name).read_text()


def test_no_committed_file_carries_a_postgres_password_literal() -> None:
    """`POSTGRES_PASSWORD` must never be assigned a literal in a tracked file.

    Only two forms are allowed: empty (`.env.example`, documenting the key), or
    a shell interpolation of the variable itself.
    """
    interpolation = re.compile(r"\$\{[^}]*\}|\$\([^)]*\)|\$[A-Za-z_][A-Za-z0-9_]*")
    assignment = re.compile(r"POSTGRES_PASSWORD\s*[=:]\s*(\S+)")
    offenders = []
    for name in (".env.example", "docker-compose.yml", ".github/workflows/ci.yml", "mise.toml"):
        for line in _repo_file(name).splitlines():
            # Blank every shell interpolation first, so a line like
            # `POSTGRES_PASSWORD=${POSTGRES_PASSWORD:?...}` is not read as an
            # assignment of the text inside its own default clause.
            stripped = interpolation.sub("", line)
            match = assignment.search(stripped)
            # Once interpolations are blanked, an allowed line leaves nothing
            # but quotes behind: `POSTGRES_PASSWORD=""` or `POSTGRES_PASSWORD=`.
            if match and match.group(1).strip("\"'"):
                offenders.append(f"{name}: {line.strip()}")
    assert offenders == [], f"a postgres password literal is committed: {offenders}"


def test_compose_refuses_to_interpolate_without_a_password() -> None:
    """No `:-` default: compose must fail loudly rather than fall back."""
    compose = _repo_file("docker-compose.yml")
    assert "POSTGRES_PASSWORD:-" not in compose
    assert "${POSTGRES_PASSWORD:?" in compose


def test_the_generated_password_file_is_gitignored() -> None:
    assert ".postgres-password" in _repo_file(".gitignore")


def test_the_migrate_service_is_given_components_not_an_assembled_dsn() -> None:
    """`alembic/env.py` builds the DSN through `db.database_url_from_env`.

    Passing components rather than a URL is what the Helm chart will do with
    CloudNativePG's Secret keys, and it keeps a `user:password@host` shape out
    of this committed file - secret scanning flags that pattern even when every
    part of it is a variable reference.
    """
    migrate = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["migrate"]
    env = dict(pair.split("=", 1) for pair in migrate["environment"])
    assert "DATABASE_URL" not in env
    for key in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    ):
        assert key in env, f"migrate needs {key} to build its DSN"


def test_no_committed_file_carries_a_credentialed_uri() -> None:
    """No `scheme://user:password@host` anywhere, assembled or not."""
    uri = re.compile(r"://[^/\s\"']*:[^/\s\"']*@")
    offenders = [
        f"{name}: {line.strip()}"
        for name in ("docker-compose.yml", ".github/workflows/ci.yml", ".env.example")
        for line in _repo_file(name).splitlines()
        if uri.search(line)
    ]
    assert offenders == [], f"a credentialed URI is committed: {offenders}"


def test_the_driver_reads_the_guides_revision_from_the_checkout_it_mounts(compose):
    """Compose has no `.guides-revision` stamp - the kubernetes initContainer
    writes that one - so the driver derives the revision from the bind-
    mounted checkout's own `.git`. That only works while the path it reads
    (`GUIDES_ROOT`) is the path the checkout is mounted at."""
    driver = compose["services"]["driver"]
    env = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in driver["environment"]}
    guides = [str(v) for v in driver["volumes"] if "/guides" in str(v)]
    assert guides == [f"${{MTG_DECKS:?set MTG_DECKS in .env}}:{env['GUIDES_ROOT']}:ro"]
