from pathlib import Path

from mtg_agent.wrapper.config import Settings


def test_defaults_when_env_is_empty():
    s = Settings.from_env({})
    assert s.http_addr == ":8080"
    assert s.internal_addr == "127.0.0.1:8090"
    assert s.claude_path == "claude"
    assert s.workspace == Path("/workspace")
    assert s.permission_mode == "bypassPermissions"
    assert s.turn_timeout == 1800.0
    assert s.boot_timeout == 60.0
    assert s.submit_delay == 0.4
    assert s.webhook_retries == 3
    assert s.shared_secret == ""
    assert s.enable_all_mcp is True


def test_env_overrides_scalars_and_paths():
    s = Settings.from_env(
        {
            "HTTP_ADDR": ":9000",
            "MODEL": "sonnet",
            "WORKSPACE": "/tmp/ws",
            "TURN_TIMEOUT_SECONDS": "60",
            "WRAPPER_SHARED_SECRET": "hunter2",
            "MCP_OVERLAY_DIR": "/etc/wrapper/mcp.d",
            "SKILLS_DIRS": "/templates/skills:/etc/wrapper/skills",
        }
    )
    assert s.http_addr == ":9000"
    assert s.model == "sonnet"
    assert s.workspace == Path("/tmp/ws")
    assert s.turn_timeout == 60.0
    assert s.shared_secret == "hunter2"
    assert s.mcp_overlay_dir == Path("/etc/wrapper/mcp.d")
    assert s.skills_dirs == [Path("/templates/skills"), Path("/etc/wrapper/skills")]


def test_claude_args_never_carry_a_permission_or_print_flag():
    s = Settings.from_env({"MODEL": "sonnet"})
    assert s.claude_args(resume=False) == ["--model", "sonnet"]
    assert s.claude_args(resume=True) == ["--model", "sonnet", "--continue"]
    assert "-p" not in s.claude_args(resume=False)
    assert "--print" not in s.claude_args(resume=False)
    assert "--dangerously-skip-permissions" not in s.claude_args(resume=False)


def test_claude_args_omit_model_when_unset():
    assert Settings.from_env({}).claude_args(resume=False) == []
