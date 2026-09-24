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


def test_the_game_serves_8000_and_can_read_the_guides(compose):
    game = compose["services"]["game"]
    assert "8000:8000" in game["ports"]
    assert any("/guides" in str(v) for v in game["volumes"])


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
    for name in ("agent-p1", "agent-p2"):
        env = _env(compose["services"][name])
        assert env["MODEL"] == "${CLAUDE_MODEL:-claude-opus-5}"
        assert env["EFFORT"] == "${CLAUDE_EFFORT:-low}"


def test_both_agents_and_both_drivers_have_a_thirty_minute_stop_grace_period(compose):
    for name in ("agent-p1", "agent-p2", "driver-p1", "driver-p2"):
        assert compose["services"][name]["stop_grace_period"] == "30m", name


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
