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
    # The post-game AI review's one-shot `claude --print` bound (`POST
    # /v1/review`, `oneshot.py`) - separate from `turn_timeout`, which
    # bounds the supervised interactive session's own turns. 1200 rather
    # than the tool-less 600: the review now waits on the `/guides` checkout
    # lock (300s of the budget in the worst case) before it runs a single
    # `mcp-seat` command. See the field's own docstring.
    assert s.review_timeout == 1200.0
    assert s.guides_dir == Path("/guides")


def test_the_guides_checkout_is_one_directory_under_three_names():
    """`Settings.guides_dir` is spelled out rather than imported (the import
    would be a cycle, and the playscripts package builds the whole game app
    on import), so the duplication is pinned here instead of trusted."""
    from magic_cards_model_api.playscripts.gitwork import GUIDES_ROOT
    from mtg_agent.wrapper.bootstrap import GUIDES_DIR

    assert Settings.from_env({}).guides_dir == Path(GUIDES_DIR) == GUIDES_ROOT


def test_the_guides_checkout_can_be_moved_for_a_test_or_a_new_mount():
    assert Settings.from_env({"GUIDES_ROOT": "/tmp/guides"}).guides_dir == Path("/tmp/guides")


def test_env_overrides_scalars_and_paths():
    s = Settings.from_env(
        {
            "HTTP_ADDR": ":9000",
            "MODEL": "sonnet",
            "WORKSPACE": "/tmp/ws",
            "TURN_TIMEOUT_SECONDS": "60",
            "REVIEW_TIMEOUT_SECONDS": "45",
            "WRAPPER_SHARED_SECRET": "hunter2",
            "MCP_OVERLAY_DIR": "/etc/wrapper/mcp.d",
            "SKILLS_DIRS": "/templates/skills:/etc/wrapper/skills",
        }
    )
    assert s.http_addr == ":9000"
    assert s.model == "sonnet"
    assert s.workspace == Path("/tmp/ws")
    assert s.turn_timeout == 60.0
    assert s.review_timeout == 45.0
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


def test_effort_becomes_a_flag() -> None:
    settings = Settings.from_env(
        {"MODEL": "opus", "EFFORT": "xhigh", "MAX_THINKING_TOKENS": "4000"}
    )
    assert settings.claude_args(resume=False) == ["--model", "opus", "--effort", "xhigh"]
    assert settings.max_thinking_tokens == 4000


def test_no_effort_no_flag() -> None:
    assert "--effort" not in Settings.from_env({"MODEL": "opus"}).claude_args(resume=False)
