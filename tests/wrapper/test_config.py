from pathlib import Path

from mtg_agent.wrapper.config import Settings, scaled_review_timeout


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
    # bounds the supervised interactive session's own turns. 3600 (DR3, up
    # from 1200): the review now runs several `mcp-seat` rounds - a
    # `plays check`/`plays admit` pair per decision and per declined offer,
    # plus `guides propose` - each capable of waiting on the `/guides`
    # checkout lock (300s in the worst case). See the field's own
    # docstring.
    assert s.review_timeout == 3600.0
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


# SUB (2026-09-23): the seat hands rules questions to a background
# `rules-researcher` subagent. Its model is its own setting, cheaper than the
# seat's, so a rules lookup never spends the seat's Opus budget.


def test_the_rules_researcher_runs_on_sonnet_by_default():
    assert Settings.from_env({}).rules_researcher_model == "sonnet"


def test_the_rules_researcher_model_is_its_own_setting():
    s = Settings.from_env({"MODEL": "opus", "RULES_RESEARCHER_MODEL": "haiku"})
    assert s.model == "opus"
    assert s.rules_researcher_model == "haiku"


# FD6 (3): the review timeout scales with the decision count a game's own
# review record carries, within a bound this module owns - a game with
# hundreds of decisions runs many more `mcp-seat` round trips than the flat
# `review_timeout` was ever sized for (see that field's own docstring).


def test_review_timeout_scaling_defaults():
    s = Settings.from_env({})
    assert s.review_timeout_per_decision == 6.0
    assert s.review_timeout_max == 10800.0


def test_review_timeout_scaling_env_overrides():
    s = Settings.from_env(
        {
            "REVIEW_TIMEOUT_PER_DECISION_SECONDS": "10",
            "REVIEW_TIMEOUT_MAX_SECONDS": "9000",
        }
    )
    assert s.review_timeout_per_decision == 10.0
    assert s.review_timeout_max == 9000.0


def test_scaled_review_timeout_is_the_flat_timeout_with_no_decisions():
    s = Settings.from_env({})
    assert scaled_review_timeout(s, 0) == s.review_timeout
    assert scaled_review_timeout(s, -5) == s.review_timeout


def test_scaled_review_timeout_grows_with_decision_count():
    s = Settings.from_env(
        {"REVIEW_TIMEOUT_SECONDS": "3600", "REVIEW_TIMEOUT_PER_DECISION_SECONDS": "6"}
    )
    assert scaled_review_timeout(s, 50) == 3600.0 + 50 * 6.0


def test_scaled_review_timeout_never_exceeds_the_configured_bound():
    s = Settings.from_env(
        {
            "REVIEW_TIMEOUT_SECONDS": "3600",
            "REVIEW_TIMEOUT_PER_DECISION_SECONDS": "100",
            "REVIEW_TIMEOUT_MAX_SECONDS": "5000",
        }
    )
    assert scaled_review_timeout(s, 1000) == 5000.0
