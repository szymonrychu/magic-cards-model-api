"""`docker-compose.aivai.yml` (DR7): Opus-vs-Opus, two AI seats in one game.

Mirrors `test_compose.py`'s style (load the yaml, assert on its shape) for
the dedicated file rather than duplicating docker-compose.yml's own tests
onto it - these pin only what is *different* about the AIvAI file: two of
every wrapper-mode service, per-seat env/ports/volumes, and that
`docker-compose.yml` itself is untouched by this file's existence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.aivai.yml").read_text())


def _env(service: dict) -> dict:
    return dict(e.split("=", 1) for e in service["environment"])


def test_the_single_seat_compose_file_is_unchanged() -> None:
    """The plan's binding requirement: docker-compose.yml itself must not
    grow a flag or a profile for this - it stays the single-agent file it
    always was."""
    single = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert set(single["services"]) == {
        "game",
        "agent",
        "driver",
        "postgres",
        "migrate",
        "guides-init",
    }


def test_the_aivai_file_has_two_of_every_wrapper_mode_service(compose) -> None:
    assert set(compose["services"]) == {
        "postgres",
        "game",
        "guides-init-p1",
        "guides-init-p2",
        "agent-p1",
        "agent-p2",
        "driver-p1",
        "driver-p2",
    }


def test_every_built_services_context_is_the_parent(compose):
    for name, service in compose["services"].items():
        if "build" not in service:
            continue
        assert service["build"]["context"] == "..", name


def test_the_game_routes_to_both_drivers(compose):
    env = _env(compose["services"]["game"])
    assert env["AI_SEAT_MODE"] == "wrapper"
    assert env["DRIVER_URLS"] == "http://driver-p1:8090,http://driver-p2:8090"
    # DRIVER_URL must not also be set here - DRIVER_URLS always wins when
    # set (ai_seat.driver_urls), so a stray DRIVER_URL would only mislead a
    # reader about there being a single-driver fallback.
    assert "DRIVER_URL" not in env


def test_the_game_serves_8000(compose):
    game = compose["services"]["game"]
    assert "8000:8000" in game["ports"]


def test_the_game_has_no_guides_mount(compose):
    """FD6 (5), fb3-parity.md item 13: no code in `magic_cards_model_api`
    reads `/guides` outside `mcp-seat`'s own default root, and `game` never
    runs `mcp-seat` - the mount was unused, and prod's own api pod never
    carries it either."""
    game = compose["services"]["game"]
    assert not any("/guides" in str(v) for v in game.get("volumes") or [])


def test_the_two_agents_get_distinct_host_ports(compose):
    agent_p1 = compose["services"]["agent-p1"]
    agent_p2 = compose["services"]["agent-p2"]
    assert "8080:8080" in agent_p1["ports"]
    assert "8081:8080" in agent_p2["ports"]


def test_the_two_drivers_know_their_own_agent(compose):
    driver_p1 = _env(compose["services"]["driver-p1"])
    driver_p2 = _env(compose["services"]["driver-p2"])
    assert driver_p1["WRAPPER_API"] == "http://agent-p1:8080"
    assert driver_p1["DRIVER_SELF_URL"] == "http://driver-p1:8090"
    assert driver_p2["WRAPPER_API"] == "http://agent-p2:8080"
    assert driver_p2["DRIVER_SELF_URL"] == "http://driver-p2:8090"
    # Neither driver points at the other seat's agent.
    assert driver_p1["WRAPPER_API"] != driver_p2["WRAPPER_API"]
    assert driver_p1["DRIVER_SELF_URL"] != driver_p2["DRIVER_SELF_URL"]


def test_each_agent_and_its_driver_share_a_guides_volume_no_one_else_uses(compose):
    """Each seat's own writable checkout, read only by its own driver - so
    two git indexes never collide and each driver reads its own agent's
    results journal, never the other seat's."""
    services = compose["services"]

    def guides_volume_name(service_name: str) -> str:
        volumes = [str(v) for v in services[service_name]["volumes"] if "/guides" in str(v)]
        assert len(volumes) == 1, f"{service_name} must mount exactly one /guides volume"
        return volumes[0].split(":", 1)[0]

    p1_volume = guides_volume_name("agent-p1")
    p2_volume = guides_volume_name("agent-p2")
    assert p1_volume != p2_volume
    assert guides_volume_name("driver-p1") == p1_volume
    assert guides_volume_name("driver-p2") == p2_volume
    assert guides_volume_name("guides-init-p1") == p1_volume
    assert guides_volume_name("guides-init-p2") == p2_volume


def test_each_agent_guides_volume_is_writable_and_each_drivers_is_read_only(compose):
    services = compose["services"]
    for name in ("agent-p1", "agent-p2"):
        volumes = [str(v) for v in services[name]["volumes"] if "/guides" in str(v)]
        assert volumes and not any(v.endswith(":ro") for v in volumes), name
    for name in ("driver-p1", "driver-p2"):
        volumes = [str(v) for v in services[name]["volumes"] if "/guides" in str(v)]
        assert volumes and all(v.endswith(":ro") for v in volumes), name


def test_no_agent_or_driver_guides_volume_is_a_bind_of_the_owners_own_mtg_decks_checkout(compose):
    """`game` legitimately binds `$MTG_DECKS` read-only for its own deck
    browsing (same as docker-compose.yml) - only the wrapper-mode services
    that write guide changes must never see the owner's own checkout."""
    for name in ("agent-p1", "agent-p2", "driver-p1", "driver-p2"):
        for volume in compose["services"][name].get("volumes") or []:
            assert "MTG_DECKS" not in str(volume), name


def test_each_agent_depends_on_its_own_guides_init(compose):
    services = compose["services"]
    assert services["agent-p1"]["depends_on"]["guides-init-p1"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["agent-p2"]["depends_on"]["guides-init-p2"]["condition"] == (
        "service_completed_successfully"
    )
    # Never cross-wired to the other seat's clone.
    assert "guides-init-p2" not in services["agent-p1"]["depends_on"]
    assert "guides-init-p1" not in services["agent-p2"]["depends_on"]


def test_the_secret_appears_only_in_the_two_guides_inits_and_the_two_agents(compose):
    for name, service in compose["services"].items():
        secret_names = {
            (entry if isinstance(entry, str) else entry.get("source"))
            for entry in service.get("secrets", [])
        }
        if name in {"guides-init-p1", "guides-init-p2", "agent-p1", "agent-p2"}:
            assert "mtg_decks_gh_pat" in secret_names, name
        else:
            assert "mtg_decks_gh_pat" not in secret_names, name
    assert compose["secrets"]["mtg_decks_gh_pat"]["environment"] == "MTG_DECKS_GH_PAT"


def test_both_seats_default_to_opus_at_low_effort(compose):
    """Sourced from the dedicated CLAUDE_AIVAI_MODEL/CLAUDE_AIVAI_EFFORT -
    not the shared CLAUDE_MODEL/CLAUDE_EFFORT docker-compose.yml's single
    seat reads - so a .env with the single-seat file's own non-blank
    defaults set can never silently override this file's Opus-low seats
    (bring-up fix, 2026-09-24)."""
    for name in ("agent-p1", "agent-p2"):
        env = _env(compose["services"][name])
        assert env["MODEL"] == "${CLAUDE_AIVAI_MODEL:-claude-opus-5}"
        assert env["EFFORT"] == "${CLAUDE_AIVAI_EFFORT:-low}"


def test_the_game_seeds_its_own_agent_default_model_and_effort_from_the_aivai_vars(compose):
    """`game`, not agent-p1/agent-p2's own env, is what
    `agent_settings.AgentSettings.default` reads for a lobby-booted game
    (model/effort are no longer a per-request field) - so `game` needs its
    own AGENT_DEFAULT_MODEL/AGENT_DEFAULT_EFFORT, sourced from the same
    dedicated CLAUDE_AIVAI_MODEL/CLAUDE_AIVAI_EFFORT vars as the two
    wrapper containers, with the same claude-opus-5/low defaults."""
    env = _env(compose["services"]["game"])
    assert env["AGENT_DEFAULT_MODEL"] == "${CLAUDE_AIVAI_MODEL:-claude-opus-5}"
    assert env["AGENT_DEFAULT_EFFORT"] == "${CLAUDE_AIVAI_EFFORT:-low}"


def test_a_dotenv_carrying_the_shared_claude_model_does_not_change_the_aivai_seats() -> None:
    """CLAUDE_MODEL/CLAUDE_EFFORT ship non-blank in .env.example for the
    single-seat file - a literal `cp .env.example .env` leaves them set for
    every compose file that reads the same .env, including this one.
    Interpolate the real file the way `docker compose` would with those two
    set (as they always are, straight out of the example) and
    CLAUDE_AIVAI_MODEL/CLAUDE_AIVAI_EFFORT left unset, and confirm every
    aivai seat - the two wrapper containers and `game`'s own
    AGENT_DEFAULT_MODEL/AGENT_DEFAULT_EFFORT - still resolves to
    claude-opus-5/low, never the shared sonnet/high."""
    import json
    import os
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        pytest.skip("docker is not installed in this environment")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "MTG_DECKS": "/tmp",
        "POSTGRES_PASSWORD": "test-only-password",
        "CLAUDE_MODEL": "sonnet",
        "CLAUDE_EFFORT": "high",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "docker-compose.aivai.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    resolved_services = json.loads(result.stdout)["services"]
    for name in ("agent-p1", "agent-p2"):
        resolved_env = resolved_services[name]["environment"]
        assert resolved_env["MODEL"] == "claude-opus-5", (name, resolved_env["MODEL"])
        assert resolved_env["EFFORT"] == "low", (name, resolved_env["EFFORT"])
    game_env = resolved_services["game"]["environment"]
    assert game_env["AGENT_DEFAULT_MODEL"] == "claude-opus-5"
    assert game_env["AGENT_DEFAULT_EFFORT"] == "low"


def test_both_agents_and_both_drivers_have_a_ninety_two_minute_stop_grace_period(compose):
    for name in ("agent-p1", "agent-p2", "driver-p1", "driver-p2"):
        assert compose["services"][name]["stop_grace_period"] == "92m", name


def test_both_agents_get_the_shared_memory_bind_mount(compose):
    """Same host bind as docker-compose.yml's single agent - notes are keyed
    by deck under ./memory/<deck>/, so both seats sharing the host directory
    is fine; nothing here needs a per-seat memory volume."""
    for name in ("agent-p1", "agent-p2", "driver-p1", "driver-p2"):
        volumes = [str(v) for v in compose["services"][name]["volumes"]]
        memory = [v for v in volumes if "/workspace/memory" in v]
        assert memory, name
        if name.startswith("agent-"):
            assert not memory[0].endswith(":ro"), name


def test_no_committed_file_carries_a_postgres_password_literal_in_the_aivai_file() -> None:
    import re

    interpolation = re.compile(r"\$\{[^}]*\}|\$\([^)]*\)|\$[A-Za-z_][A-Za-z0-9_]*")
    assignment = re.compile(r"POSTGRES_PASSWORD\s*[=:]\s*(\S+)")
    offenders = []
    for line in (ROOT / "docker-compose.aivai.yml").read_text().splitlines():
        stripped = interpolation.sub("", line)
        match = assignment.search(stripped)
        if match and match.group(1).strip("\"'"):
            offenders.append(line.strip())
    assert offenders == [], f"a postgres password literal is committed: {offenders}"


def test_compose_aivai_refuses_to_interpolate_without_a_password() -> None:
    text = (ROOT / "docker-compose.aivai.yml").read_text()
    assert "POSTGRES_PASSWORD:-" not in text
    assert "${POSTGRES_PASSWORD:?" in text


def test_no_credentialed_uri_in_the_aivai_file() -> None:
    import re

    uri = re.compile(r"://[^/\s\"']*:[^/\s\"']*@")
    offenders = [
        line.strip()
        for line in (ROOT / "docker-compose.aivai.yml").read_text().splitlines()
        if uri.search(line)
    ]
    assert offenders == [], f"a credentialed URI is committed: {offenders}"


def test_docker_compose_config_parses_the_aivai_file() -> None:
    """The real thing, not just yaml.safe_load: catches an env-var
    interpolation mistake or an invalid compose key that a bare yaml parse
    would happily accept."""
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        pytest.skip("docker is not installed in this environment")
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "MTG_DECKS": "/tmp",
        "POSTGRES_PASSWORD": "test-only-password",
    }
    result = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "docker-compose.aivai.yml"), "config", "-q"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# FD6 (5): parity with the prod seat Job (deploy/seat-job.yaml)
# ---------------------------------------------------------------------------


def test_the_game_gets_postgres_vars_and_a_postgres_service_like_the_single_seat_file(
    compose,
) -> None:
    """fb3-parity.md item 5: without this, a finished local AIvAI game is
    lost on restart and the review reads a different store than prod's,
    since prod sets `REQUIRE_POSTGRES=1` off `mtg-database-app`. The
    `postgres` service and `postgres-data` volume already exist in this
    file (for `mise run test:pg` parity) - only `game`'s own env was
    unwired."""
    assert "postgres" in compose["services"]
    assert "postgres-data" in compose["volumes"]
    env = _env(compose["services"]["game"])
    assert env["POSTGRES_HOST"] == "postgres"
    assert env["POSTGRES_PORT"] == "5432"
    assert env["POSTGRES_USER"] == "${POSTGRES_USER:-mtg}"
    assert env["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD:?}"
    assert env["POSTGRES_DB"] == "${POSTGRES_DB:-mtg}"
    assert compose["services"]["game"]["depends_on"]["postgres"]["condition"] == ("service_healthy")


def test_each_seat_gets_a_driver_workspace_volume_shared_by_driver_and_agent(compose) -> None:
    """fb3-parity.md item 3: prod's seat Job shares one `driver-workspace`
    emptyDir between the driver (read-write) and the agent (read-only,
    `MTG_EVIDENCE_DIR=/driver-workspace/evidence`) - compose had neither the
    volume nor the env var, so a local `plays admit` committed no evidence
    file at all."""
    services = compose["services"]

    def workspace_volume(service_name: str) -> list[str]:
        return [str(v) for v in services[service_name]["volumes"] if "/driver-workspace" in str(v)]

    p1 = workspace_volume("driver-p1")
    p2 = workspace_volume("driver-p2")
    assert p1 and not any(v.endswith(":ro") for v in p1)
    assert p2 and not any(v.endswith(":ro") for v in p2)
    agent_p1 = workspace_volume("agent-p1")
    agent_p2 = workspace_volume("agent-p2")
    assert agent_p1 and all(v.endswith(":ro") for v in agent_p1)
    assert agent_p2 and all(v.endswith(":ro") for v in agent_p2)
    # Each seat's own volume - never shared across the two seats.
    assert p1[0].split(":", 1)[0] == agent_p1[0].split(":", 1)[0]
    assert p2[0].split(":", 1)[0] == agent_p2[0].split(":", 1)[0]
    assert p1[0].split(":", 1)[0] != p2[0].split(":", 1)[0]


def test_each_agent_gets_mtg_evidence_dir_on_the_shared_workspace(compose) -> None:
    for name in ("agent-p1", "agent-p2"):
        env = _env(compose["services"][name])
        assert env["MTG_EVIDENCE_DIR"] == "/driver-workspace/evidence"


def test_the_claude_code_cli_version_is_pinned_not_latest(compose) -> None:
    """fb3-parity.md item 11: prod pins nothing (`ARG CLAUDE_CODE_VERSION=
    latest`), so a rebuild silently drifts the CLI version FC2's permission
    rules depend on. One default, shared by every build in this file."""
    for name in ("agent-p1", "agent-p2", "guides-init-p1", "guides-init-p2"):
        args = compose["services"][name]["build"]["args"]
        assert args["CLAUDE_CODE_VERSION"] != "${CLAUDE_CODE_VERSION:-latest}", name
        assert "latest" not in args["CLAUDE_CODE_VERSION"], name


def test_seat_job_and_compose_and_dockerfile_pin_the_same_claude_code_version() -> None:
    import re

    dockerfile = (ROOT / "Dockerfile.agent").read_text()
    match = re.search(r"ARG CLAUDE_CODE_VERSION=(\S+)", dockerfile)
    assert match, "Dockerfile.agent must declare a default CLAUDE_CODE_VERSION"
    dockerfile_default = match.group(1)
    assert dockerfile_default != "latest"

    compose_text = (ROOT / "docker-compose.aivai.yml").read_text()
    assert f"CLAUDE_CODE_VERSION:-{dockerfile_default}" in compose_text

    mise_text = (ROOT / "mise.toml").read_text()
    assert mise_text.count(f"CLAUDE_CODE_VERSION:-{dockerfile_default}") >= 2
