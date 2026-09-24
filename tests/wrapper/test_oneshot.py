"""`oneshot.run_review`'s argv, its environment and its failure taxonomy.

Two kinds of test here, and neither ever runs the real `claude` CLI:

- the argv/env/cwd shape is asserted against a captured `subprocess.run`
  call (`_captured`), so nothing at all is spawned - the reviewer now
  carries a tool allowlist, an MCP config and a writable checkout, and each
  of those is a flag that must be exactly right;
- the failure paths run a stub Python script standing in for `claude` (the
  same approach `test_buddy_lifecycle.py`'s `STUB_COMMAND` and
  `ai_seat.py`'s own tests use), so the real `subprocess` error handling is
  exercised for real.
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import pytest

from mtg_agent.wrapper import bootstrap, oneshot
from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.oneshot import ReviewFailed, run_review


def _stub(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-claude"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    script.chmod(0o755)
    return str(script)


def _rendered(tmp_path: Path) -> dict[str, str]:
    """A workspace, HOME and guides checkout rendered the way the wrapper's
    own bootstrap renders them, as env for `Settings.from_env`.

    `run_review` reads all three now - the workspace for `cwd` and its
    `.mcp.json`, HOME for `~/.gitconfig`, the checkout for `--add-dir` -
    and refuses with a `ReviewFailed` when the first two are missing, so a
    test that wants any OTHER failure has to render them first."""
    workspace = tmp_path / "workspace"
    home = tmp_path / "agent-home"
    guides = tmp_path / "guides"
    settings = Settings.from_env(
        {"WORKSPACE": str(workspace), "HOME_DIR": str(home), "GUIDES_ROOT": str(guides)}
    )
    guides.mkdir(parents=True, exist_ok=True)
    bootstrap.render(settings)
    return {
        "WORKSPACE": str(workspace),
        "HOME_DIR": str(home),
        "GUIDES_ROOT": str(guides),
    }


def _settings(claude_path: str, tmp_path: Path, **overrides) -> Settings:
    return Settings.from_env({"CLAUDE_PATH": claude_path, **_rendered(tmp_path), **overrides})


class _Completed:
    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = "the review text\n"
        self.stderr = ""


def _captured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides) -> dict[str, Any]:
    """`run_review` with `subprocess.run` replaced: no process at all, and
    the argv, cwd, env and timeout it was called with handed back."""
    seen: dict[str, Any] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen.update(kwargs)
        return _Completed()

    monkeypatch.setattr(oneshot.subprocess, "run", fake_run)
    settings = _settings("claude", tmp_path, **overrides)
    seen["text"] = run_review(settings, "critique this game", "claude-opus-5", "high")
    seen["settings"] = settings
    return seen


def _flag(argv: list[str], name: str) -> str:
    """The single value of `name` in `argv` - and proof it appears once."""
    positions = [i for i, item in enumerate(argv) if item == name]
    assert len(positions) == 1, f"{name} must appear exactly once in {argv}"
    index = positions[0]
    assert index + 1 < len(argv), f"{name} carries no value in {argv}"
    return argv[index + 1]


# ---------------------------------------------------------------------------
# the argv: a tool ALLOWLIST, the seat's own MCP config, the guides checkout
# ---------------------------------------------------------------------------


def test_the_reviewer_is_given_a_named_tool_allowlist_never_the_empty_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The review writes play review back into the deck's guide now, so it
    needs tools. `--tools ""` (every built-in tool disabled - `claude
    --help`'s own words: 'Use "" to disable all tools') is what it must no
    longer be, and "default" (every tool) is what it must not become
    instead: the flag carries `oneshot.REVIEW_TOOLS`, a named constant."""
    argv = _captured(monkeypatch, tmp_path)["argv"]

    tools = _flag(argv, "--tools")
    assert tools == ",".join(oneshot.REVIEW_TOOLS)
    assert tools != "", "a tool-less review can no longer do the job it is asked to do"
    assert tools != "default", "every built-in tool is not an allowlist"
    # The four things the allowlist exists to make possible.
    assert "Read" in oneshot.REVIEW_TOOLS
    assert "Edit" in oneshot.REVIEW_TOOLS
    assert "Write" in oneshot.REVIEW_TOOLS
    assert "Bash" in oneshot.REVIEW_TOOLS


def test_the_allowlist_reaches_the_guides_checkout_only_through_mcp_seat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T14's `gitwork.checkout_lock` is taken INSIDE `plays admit` and
    `guides propose`, so it only serializes the reviewer against the
    handover turn while every write goes through one of those commands. A
    bare `git` (or anything else that commits, pushes or rewrites the
    checkout) would walk straight past the lock, so no such rule is on the
    allowlist: every Bash rule names an `mcp-seat` subcommand, either by its
    bare name or by the absolute path the image installs it at (FC2: live
    evidence - loop game 2's p2 review, ledger seq 299 - showed the reviewer
    reaching for the absolute path too, and being refused because only the
    bare form was ever on the allowlist)."""
    argv = _captured(monkeypatch, tmp_path)["argv"]
    allowed = _flag(argv, "--allowedTools").split(",")
    entrypoint = oneshot.mcp_seat_entrypoint()

    bash_rules = [rule for rule in allowed if rule.startswith("Bash(")]
    assert bash_rules, "the review cannot run mcp-seat at all without a Bash rule"
    for rule in bash_rules:
        assert rule.startswith("Bash(mcp-seat ") or rule.startswith(f"Bash({entrypoint} "), (
            f"{rule} lets the reviewer work the checkout outside gitwork.checkout_lock - see T14"
        )
    assert "Bash" not in allowed, "an unscoped Bash is not an allowlist"

    # The commands the feature actually needs: the two write channels, the
    # matchup-plan proposal, and the read-only corpus listing - each
    # reachable both bare and by the absolute entrypoint path.
    joined = " ".join(bash_rules)
    for subcommand in ("plays priority", "plays admit", "guides propose", "plays check"):
        assert f"mcp-seat {subcommand}" in joined
        assert f"{entrypoint} {subcommand}" in joined


def test_review_bash_rules_cover_four_subcommands_bare_and_by_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Live evidence (loop game 2, p2 review, ledger seq 299): every
    `mcp-seat` call was refused, bare AND by absolute path
    (`/app/.venv/bin/mcp-seat`) alike. The planner's probe of the real CLI
    (`scripts/probe_review_permissions.sh`) showed the gap is command shape,
    not a missing subcommand: `Bash(mcp-seat plays check:*)` permits a bare
    invocation, never an absolute-path one, so a reviewer that reaches for
    the absolute path is refused by a rule that never named that shape. The
    fix names the same four subcommands twice: once bare, once by the
    resolved entrypoint path - exactly eight Bash rules, never more."""
    argv = _captured(monkeypatch, tmp_path)["argv"]
    allowed = _flag(argv, "--allowedTools").split(",")
    bash_rules = [rule for rule in allowed if rule.startswith("Bash(")]

    entrypoint = oneshot.mcp_seat_entrypoint()
    for subcommand in ("plays check", "plays priority", "plays admit", "guides propose"):
        assert f"Bash(mcp-seat {subcommand}:*)" in bash_rules
        assert f"Bash({entrypoint} {subcommand}:*)" in bash_rules
    assert len(bash_rules) == 8, f"expected exactly 8 Bash rules, got {bash_rules}"


def test_no_bash_rule_ever_allows_git_python_find_or_cd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T14's checkout_lock is taken INSIDE `plays admit`/`guides propose`
    only - see the module docstring - so nothing that could shell past it (a
    raw `git`, `python3`, `find` or a `cd` into the checkout) may ever be
    added, absolute-path rules included. `mcp-seat` itself shells out to
    git/python3 as a subprocess, which these rules do not govern and do not
    need to: Glob/Grep/Read already cover what a bare `find` would."""
    argv = _captured(monkeypatch, tmp_path)["argv"]
    allowed = _flag(argv, "--allowedTools").split(",")
    bash_rules = [rule for rule in allowed if rule.startswith("Bash(")]

    for rule in bash_rules:
        assert not rule.startswith("Bash(git"), rule
        assert not rule.startswith("Bash(python"), rule
        assert not rule.startswith("Bash(find"), rule
        assert not rule.startswith("Bash(cd"), rule
    assert "Bash" not in allowed
    assert "Bash(mcp-seat:*)" not in allowed, "no bare subcommand-less mcp-seat rule"


def test_review_bash_rules_absolute_form_uses_the_resolved_entrypoint_not_a_hardcoded_app_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'Derive it from the settings, or from shutil.which('mcp-seat') at
    argv build time; do not hardcode /app.' A PATH with no `mcp-seat` on it
    at all must still resolve to SOME absolute path - the sibling of the
    running interpreter's own venv, since `uv sync` installs both into the
    same one - never silently fall back to the bare name (which would make
    the 'absolute' rule identical to the bare one)."""
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")
    entrypoint = oneshot.mcp_seat_entrypoint()
    assert entrypoint != "mcp-seat"
    assert Path(entrypoint).is_absolute()
    assert entrypoint.endswith("/mcp-seat")
    assert "/app" not in entrypoint, "must never hardcode the image's own install path"


def test_the_handover_session_is_not_the_reviews_denied_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reviewer runs `--permission-prompts none` behind a narrow
    `--allowedTools`: anything outside `REVIEW_ALLOWED_TOOLS` is refused
    outright. The handover/follow-up turns are a different process
    entirely - the supervised PTY session `bootstrap.write_settings_json`
    renders `settings.json` for, whose `permissions.defaultMode` is
    `Settings.permission_mode` (`bypassPermissions` by default), never the
    review's own deny-by-default shape. Conflating the two would mean a
    permission fix here silently does nothing for the session it was
    actually written for: T14's live evidence (loop game 2 seq 299) was the
    REVIEW itself being refused, while p1's handover in the same game
    admitted plays just fine under the other session's rules."""
    seen = _captured(monkeypatch, tmp_path)
    argv = seen["argv"]
    settings = seen["settings"]

    assert _flag(argv, "--permission-prompts") == "none"

    settings_json = json.loads((settings.home_dir / ".claude" / "settings.json").read_text())
    assert settings_json["permissions"]["defaultMode"] == "bypassPermissions", (
        "the handover session runs bypassPermissions, never the review's own "
        "--permission-prompts none allowlist"
    )


def test_the_reviewer_can_still_file_a_system_problem_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T37's routing rule has two destinations, and this one used to be
    dead. `prompt.NOTES_ROUTING` sends a system problem - an api bug, an
    engine rules gap, a tool misbehaviour - to the `report_problem` tool,
    and the reviewer's allowlist named no MCP tool at all, so a reviewer
    that found one had nowhere to put it and the rule asked for something
    it could not do. `report_problem` is on the allowlist now, and
    `driver.review`'s prompt hands the reviewer the `player_uid` it
    takes."""
    argv = _captured(monkeypatch, tmp_path)["argv"]
    allowed = _flag(argv, "--allowedTools").split(",")

    assert "mcp__mtg__report_problem" in allowed, (
        "NOTES_ROUTING routes system problems here; an allowlist without it "
        "makes that half of the rule unreachable"
    )
    assert "mcp__mtg__report_problem" in oneshot.REVIEW_MCP_TOOLS


def test_report_problem_is_the_only_mcp_tool_the_reviewer_may_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The game is over: nothing on the `mtg` server except the problem
    report has a job left to do here, and the reviewer is handed a live
    seat credential to call it with. Naming exactly one tool is what keeps
    that credential usable for the report and for nothing else."""
    argv = _captured(monkeypatch, tmp_path)["argv"]
    allowed = _flag(argv, "--allowedTools").split(",")

    assert [rule for rule in allowed if rule.startswith("mcp__")] == ["mcp__mtg__report_problem"]
    for tool in (
        "mcp__mtg",
        "mcp__mtg__*",
        "mcp__mtg__cast",
        "mcp__mtg__pass",
        "mcp__mtg__activate",
        "mcp__mtg__declare_plan",
        "mcp__mtg__run_script",
        "mcp__spellslinger__knowledge_search",
    ):
        assert tool not in allowed, f"{tool} has no job in a finished game's review"


def test_the_interactive_tools_a_headless_process_can_only_stall_on_stay_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`bootstrap.DENY_TOOLS` is denied to the play session through the
    settings.json the play HOME carries. This process does not load that
    file (see `--setting-sources` below), so the denial has to be argv's
    job here - a picker with nobody at the terminal stalls a review exactly
    as surely as it stalls a turn."""
    argv = _captured(monkeypatch, tmp_path)["argv"]
    denied = _flag(argv, "--disallowed-tools").split(",")
    allowed = _flag(argv, "--allowedTools").split(",")

    for tool in bootstrap.DENY_TOOLS:
        assert tool in denied, f"{tool} must stay denied for the reviewer too"
        assert tool not in oneshot.REVIEW_TOOLS
        assert tool not in allowed
    # Nothing may PROMPT either: there is nobody to answer, and "denied
    # automatically" is the only headless-safe answer.
    assert _flag(argv, "--permission-prompts") == "none"
    assert "--dangerously-skip-permissions" not in argv, (
        "the allowlist is the boundary; bypassing permissions would make it decoration"
    )


def test_the_reviewer_loads_exactly_the_seats_own_mcp_servers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--strict-mcp-config` stays, but it is now paired with an explicit
    `--mcp-config`: the workspace `.mcp.json` `bootstrap.merge_mcp` wrote,
    and nothing from any other source."""
    seen = _captured(monkeypatch, tmp_path)
    argv = seen["argv"]
    settings = seen["settings"]

    assert "--strict-mcp-config" in argv
    assert _flag(argv, "--mcp-config") == str(settings.workspace / ".mcp.json")


def test_the_guides_checkout_is_reachable_and_the_play_settings_are_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two halves of the same reduction in isolation (D-2).

    `--add-dir` is what lets the file tools touch `/guides` at all - it
    sits outside `cwd`, and a file tool confined to the workspace could
    never edit a deck's `NOTES.md`. `--setting-sources ""` is the other
    half: HOME is the play session's own now, and its
    `.claude/settings.json` carries the play session's Stop hook, which a
    second `claude` reading that file would fire at the end of its own run
    and report as a turn completion."""
    seen = _captured(monkeypatch, tmp_path)
    argv = seen["argv"]
    settings = seen["settings"]

    assert _flag(argv, "--add-dir") == str(settings.guides_dir)
    assert _flag(argv, "--setting-sources") == "", (
        "no settings file may load: the play session's Stop hook lives in one"
    )


def test_the_prompt_travels_on_stdin_never_in_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FB1/E2BIG: the prompt used to be the last argv element after a bare
    `--`. Linux's `MAX_ARG_STRLEN` (131072 bytes per argv element) made a
    ~190KB review prompt fail with `OSError: [Errno 7] Argument list too
    long` before this - so the prompt now travels on stdin
    (`subprocess.run(..., input=_capped(prompt))`), and the trailing
    `--`/prompt pair argv used to carry is simply gone, not merely moved."""
    seen = _captured(monkeypatch, tmp_path)
    argv = seen["argv"]
    assert "critique this game" not in argv
    assert "--" not in argv
    assert argv[-2:] == ["--mcp-config", str(seen["settings"].workspace / ".mcp.json")]
    assert seen["input"] == oneshot._capped("critique this game")


# ---------------------------------------------------------------------------
# where it runs, and the environment it is given
# ---------------------------------------------------------------------------


def test_the_review_runs_in_the_agents_own_workspace_and_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D-2, the deliberate part: a throwaway HOME cannot reach
    `~/.gitconfig`'s credential helper, so no `mcp-seat` run inside it could
    ever push to mtg-decks main. The review runs in the
    workspace, as the agent, or it cannot do the job at all."""
    seen = _captured(monkeypatch, tmp_path)
    settings = seen["settings"]

    assert seen["cwd"] == settings.workspace
    assert seen["env"]["HOME"] == str(settings.home_dir)
    assert (settings.home_dir / ".gitconfig").is_file(), "sanity: bootstrap wrote the helper"
    assert (settings.workspace / ".mcp.json").is_file(), "sanity: bootstrap wrote the config"


def test_no_throwaway_home_is_made_or_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The per-call `tempfile.mkdtemp` HOME and its `shutil.rmtree` are
    gone, not merely unused: a temp HOME that is still created is a temp
    HOME a later edit can quietly start passing again.

    FC2 brought `shutil` back into this module (`shutil.which`, resolving
    the `mcp-seat` entrypoint for the absolute-path Bash rules) - a
    different job from the retired temp-HOME cleanup, so this only pins that
    `rmtree` specifically is never called, not that the whole module stays
    free of the `shutil` import."""

    def refuse_mkdtemp(*args, **kwargs):
        raise AssertionError("run_review must not create a temporary HOME any more")

    def refuse_rmtree(*args, **kwargs):
        raise AssertionError("run_review must not remove a temporary HOME any more")

    monkeypatch.setattr(tempfile, "mkdtemp", refuse_mkdtemp)
    monkeypatch.setattr(oneshot.shutil, "rmtree", refuse_rmtree)
    _captured(monkeypatch, tmp_path)
    assert not hasattr(oneshot, "tempfile"), "the throwaway HOME's import is gone too"


def test_the_child_env_is_named_variable_by_variable_never_inherited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_env_for` still builds the child's environment from nothing. A
    variable this wrapper's own process happens to carry does not reach the
    review unless `oneshot` names it - which is what makes a future env var
    added to the wrapper safe by default rather than safe by memory."""
    monkeypatch.setenv("SOME_UNRELATED_WRAPPER_VAR", "leaked")
    monkeypatch.setenv("HOOK_COMMAND", "cc-stop-hook")
    monkeypatch.setenv("CCW_RESULT_JSON", "/workspace/result.json")
    env = _captured(monkeypatch, tmp_path)["env"]

    assert "SOME_UNRELATED_WRAPPER_VAR" not in env
    assert "HOOK_COMMAND" not in env
    assert "CCW_RESULT_JSON" not in env


def test_a_stop_hook_that_still_fires_cannot_reach_the_wrappers_own_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Belt and braces for the one hazard the isolation used to cover.

    `cc-stop-hook` reads `CCW_INTERNAL_URL` straight off its own
    environment and DEFAULTS to the wrapper's internal listener when it is
    unset, so leaving the variable out is not a guard at all. A completion
    posted there with no turn id of its own is trusted by
    `session.Manager.complete` and would end whatever turn - possibly the
    handover turn - is actually in flight."""
    monkeypatch.setenv("CCW_INTERNAL_URL", "http://127.0.0.1:8090/internal/turn-complete")
    seen = _captured(monkeypatch, tmp_path)
    env = seen["env"]
    settings = seen["settings"]

    assert env["CCW_INTERNAL_URL"] == oneshot.REVIEW_STOP_HOOK_SINK
    assert settings.internal_addr not in env["CCW_INTERNAL_URL"]


def test_the_reviewer_commits_under_its_own_role_not_the_seats(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T14 gave the reviewer `gitwork.REVIEWER_ROLE` precisely because it is
    a THIRD writer for the same game and deck: sharing `MTG_ROLE` with the
    seat would author the reviewer's commits on main as the seat's."""
    from magic_cards_model_api.playscripts import gitwork

    monkeypatch.setenv("MTG_ROLE", "seat")
    env = _captured(monkeypatch, tmp_path)["env"]

    assert env["MTG_ROLE"] == gitwork.REVIEWER_ROLE
    assert env["MTG_ROLE"] != "seat"
    assert gitwork.REVIEWER_ROLE in gitwork.ROLE_NAMES, "the role must be one gitwork accepts"


def test_the_variables_mcp_seat_reads_are_named_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Everything `mcp-seat`'s guides commands read off their own
    environment, named one by one in `oneshot.PASSED_THROUGH`."""
    for name in oneshot.PASSED_THROUGH:
        monkeypatch.setenv(name, f"value-of-{name}")
    seen = _captured(monkeypatch, tmp_path)
    env = seen["env"]
    settings = seen["settings"]

    for name in oneshot.PASSED_THROUGH:
        assert env[name] == f"value-of-{name}"
    assert "MTG_GAME_ID" in oneshot.PASSED_THROUGH
    # D2b: the reviewer's own `plays admit` picks up this game's evidence
    # record from the directory the driver writes it to.
    assert "MTG_EVIDENCE_DIR" in oneshot.PASSED_THROUGH
    # `seat_cli` reads WORKSPACE for its drafts directory; it is the
    # wrapper's own setting, not something to pick off the environment.
    assert env["WORKSPACE"] == str(settings.workspace)


def test_nothing_for_the_retired_github_rest_path_is_passed_through() -> None:
    """Guide edits and play admits push straight to main through git's own
    credential helper; nothing reads a GitHub API host, a repository name or
    the token file's path off the environment any more."""
    for retired in ("MTG_GITHUB_REPO", "MTG_GITHUB_API", "GH_PAT_FILE"):
        assert retired not in oneshot.PASSED_THROUGH


def test_an_unset_passthrough_variable_is_left_out_rather_than_blanked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty variable and an absent one are not alike in `os.environ`: an
    empty string is a value, and a reader that only checks presence would see
    one."""
    for name in oneshot.PASSED_THROUGH:
        monkeypatch.delenv(name, raising=False)
    env = _captured(monkeypatch, tmp_path)["env"]
    for name in oneshot.PASSED_THROUGH:
        assert name not in env


def test_the_auth_credential_precedence_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An OAuth token from `claude setup-token` is NOT an API key and the
    CLI reads them from different variables - the same precedence
    `pty_proc.spawn_claude` applies."""
    env = _captured(
        monkeypatch,
        tmp_path,
        CLAUDE_CODE_OAUTH_TOKEN="oauth-token",
        ANTHROPIC_API_KEY="sk-key",
    )["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert "ANTHROPIC_API_KEY" not in env

    env = _captured(monkeypatch, tmp_path, ANTHROPIC_API_KEY="sk-key")["env"]
    assert env["ANTHROPIC_API_KEY"] == "sk-key"


# ---------------------------------------------------------------------------
# the timeout, and the driver's own client timeout over it
# ---------------------------------------------------------------------------


def test_the_review_is_bounded_by_review_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _captured(monkeypatch, tmp_path)
    assert seen["timeout"] == seen["settings"].review_timeout


def test_a_tool_using_review_is_given_more_time_than_a_tool_less_one() -> None:
    """Re-justified for the tool surface this task gives the review: the
    process can now wait up to `gitwork.CHECKOUT_LOCK_TIMEOUT` (300s) for
    the guides checkout before it does any work at all, then run several
    `mcp-seat` commands that fetch, merge or rebase, gate and push.
    The old 600s covered a model call and nothing else."""
    from magic_cards_model_api.playscripts.gitwork import CHECKOUT_LOCK_TIMEOUT

    bound = Settings.from_env({}).review_timeout
    assert bound > 600.0, "the tool-less bound cannot cover a review that waits on a lock"
    assert bound > CHECKOUT_LOCK_TIMEOUT * 2, (
        "a review that spends its whole budget waiting for the checkout has done nothing"
    )


def test_the_drivers_client_timeout_outlasts_the_wrappers_own_bound() -> None:
    """A review that was about to succeed must never read to the driver as
    an unreachable wrapper, and a review still running must never be
    abandoned by `retire` before the wrapper itself would have killed it."""
    from mtg_agent.driver.app import RETIRE_REVIEW_JOIN_TIMEOUT, REVIEW_HTTP_TIMEOUT
    from mtg_agent.driver.handover import PRESTOP_WAIT_SECONDS

    bound = Settings.from_env({}).review_timeout
    assert bound < REVIEW_HTTP_TIMEOUT
    assert RETIRE_REVIEW_JOIN_TIMEOUT > REVIEW_HTTP_TIMEOUT
    # DR3: `review_timeout` (and the two bounds stacked over it) tripled
    # (1200 -> 3600) so a review can run several `mcp-seat` rounds per game
    # rather than one. The driver container's own preStop wait
    # (`handover.PRESTOP_WAIT_SECONDS`, mirrored at `deploy/seat-job.yaml`,
    # and its Job's `terminationGracePeriodSeconds` over that) moved with
    # it, restoring - not flipping - the documented ordering invariant DR3
    # was asked to keep under test: the driver outlasts the wrapper. A
    # kubelet that kills the driver container at the OLD 1740s preStop wait
    # would cut a review that genuinely needs the new, longer
    # `REVIEW_HTTP_TIMEOUT` short before `retire`'s own join gives up on it.
    assert RETIRE_REVIEW_JOIN_TIMEOUT < PRESTOP_WAIT_SECONDS, (
        "the driver's preStop wait must still outlast retire's review-join "
        "bound, or a real review is killed by the kubelet before it can finish"
    )


# ---------------------------------------------------------------------------
# the failure taxonomy - every way this goes wrong is one ReviewFailed
# ---------------------------------------------------------------------------


def test_run_review_returns_stripped_stdout_on_success(tmp_path: Path) -> None:
    stub = _stub(
        tmp_path,
        """
        import sys
        assert sys.argv[1] == "--print"
        # The allowlist really arrives as one non-empty argv element.
        assert sys.argv[sys.argv.index("--tools") + 1].split(",")[0] == "Read"
        print("  the review text  \\n")
        """,
    )
    settings = _settings(stub, tmp_path)
    text = run_review(settings, "critique this game", "claude-opus-5", "high")
    assert text == "the review text"


def test_run_review_raises_on_a_nonzero_exit(tmp_path: Path) -> None:
    stub = _stub(
        tmp_path,
        """
        import sys
        sys.stderr.write("API Error: 401\\n")
        sys.exit(1)
        """,
    )
    with pytest.raises(ReviewFailed, match="claude exited 1.*401"):
        run_review(_settings(stub, tmp_path), "x", "m", "high")


def test_run_review_includes_stdout_when_the_cli_writes_its_failure_there(
    tmp_path: Path,
) -> None:
    """The real `claude` CLI writes an auth failure to STDOUT, not stderr -
    building the failure message from stderr alone left it as bare as
    "claude exited 1" with nothing telling an operator why, the one time
    they most need to know."""
    stub = _stub(
        tmp_path,
        """
        import sys
        print("Invalid API key \\u00b7 Please run /login")
        sys.exit(1)
        """,
    )
    with pytest.raises(ReviewFailed, match="claude exited 1.*Invalid API key"):
        run_review(_settings(stub, tmp_path), "x", "m", "high")


def test_run_review_raises_on_a_missing_binary(tmp_path: Path) -> None:
    settings = _settings(str(tmp_path / "no-such-claude"), tmp_path)
    with pytest.raises(ReviewFailed, match="not on PATH"):
        run_review(settings, "x", "m", "high")


def test_run_review_raises_on_timeout(tmp_path: Path) -> None:
    stub = _stub(tmp_path, "import time; time.sleep(5)")
    settings = _settings(stub, tmp_path, REVIEW_TIMEOUT_SECONDS="0.1")
    with pytest.raises(ReviewFailed, match="did not finish within 0.1s"):
        run_review(settings, "x", "m", "high")


def test_run_review_raises_on_an_unexecutable_binary(tmp_path: Path) -> None:
    """`OSError` (a `claude_path` that exists but is not executable) is a
    real, seen-in-production failure mode distinct from `claude` simply not
    existing - both must become a `ReviewFailed`, never an uncaught
    exception that 500s the wrapper's route."""
    not_executable = tmp_path / "not-executable"
    not_executable.write_text("#!/bin/sh\necho hi\n")
    not_executable.chmod(0o644)
    with pytest.raises(ReviewFailed):
        run_review(_settings(str(not_executable), tmp_path), "x", "m", "high")


def test_run_review_caps_an_oversized_prompt(tmp_path: Path) -> None:
    """The prompt now arrives on stdin, so the cap has to be proven there -
    an oversized prompt read off `sys.argv` would trivially pass this test
    for the wrong reason (there is no such argv element any more)."""
    captured = tmp_path / "captured-stdin-len.json"
    stub = _stub(
        tmp_path,
        f"""
        import json, sys
        data = sys.stdin.read()
        json.dump(len(data), open({str(captured)!r}, "w"))
        print("ok")
        """,
    )
    settings = _settings(stub, tmp_path)
    huge = "x" * 10_000_000
    run_review(settings, huge, "m", "high")
    assert json.loads(captured.read_text()) < len(huge)


def _prompt_200k() -> str:
    """A 200_000-char prompt, mostly plain ASCII with one multi-byte line
    (in UTF-8 bytes, not code points), and no leading/trailing whitespace -
    so `.strip()` on the way back out never changes it."""
    multibyte_line = "café résumé 世界 🎲 review"
    ascii_head = "a" * 50_000
    remainder = 200_000 - len(ascii_head) - len(multibyte_line) - 2  # 2 newlines
    prompt = f"{ascii_head}\n{multibyte_line}\n{'b' * remainder}"
    assert len(prompt) == 200_000
    return prompt


def test_run_review_sends_a_200k_prompt_on_stdin_byte_for_byte(tmp_path: Path) -> None:
    """RED for the E2BIG fix, run against the REAL `run_review` - no
    monkeypatched `subprocess.run`. The fake `claude` ignores its argv
    entirely and copies stdin to stdout, so this only passes if the prompt
    actually arrives on stdin: on Linux (`MAX_ARG_STRLEN` = 131072 bytes)
    unchanged code raises `OSError` E2BIG trying to put a 200KB prompt into
    one argv element, which `run_review` turns into `ReviewFailed`."""
    script = tmp_path / "fake-claude"
    script.write_text("#!/bin/sh\ncat\n")
    script.chmod(0o755)
    settings = _settings(str(script), tmp_path)
    prompt = _prompt_200k()

    text = run_review(settings, prompt, "m", "high")

    assert text == oneshot._capped(prompt).strip()
    assert text == prompt  # well under MAX_PROMPT_CHARS, so `_capped` is a no-op


def test_argv_never_carries_a_200k_prompt_or_exceeds_max_arg_strlen(tmp_path: Path) -> None:
    """The guard: whatever `_argv` returns, no element can carry the prompt
    - not the whole thing, not even its first 1000 characters - and no
    element crosses Linux's 131072-byte `MAX_ARG_STRLEN` per argv element,
    which is exactly the bound a re-embedded prompt would eventually blow
    through again."""
    prompt = _prompt_200k()
    settings = _settings("claude", tmp_path)
    mcp_config = settings.workspace / ".mcp.json"

    argv = oneshot._argv(settings, "claude-opus-5", "high", mcp_config)

    for element in argv:
        encoded = element.encode("utf-8")
        assert len(encoded) <= 131072, (
            f"{element[:80]!r}... is {len(encoded)} bytes, over Linux's MAX_ARG_STRLEN"
        )
    assert prompt not in argv
    prefix = prompt[:1000]
    for element in argv:
        assert prefix not in element


def test_an_unrendered_workspace_is_a_named_failure_not_a_cli_error(
    tmp_path: Path,
) -> None:
    """`--mcp-config` names a file that only `bootstrap.merge_mcp` writes.
    Without it `claude` fails with its own wording about a config path,
    which tells an operator nothing about WHY the path is not there."""
    settings = Settings.from_env(
        {
            # A path that does not exist: the pre-flight below must refuse
            # before anything is spawned, and this test must never reach a
            # real `claude` if a later edit ever removes it.
            "CLAUDE_PATH": str(tmp_path / "no-such-claude"),
            "WORKSPACE": str(tmp_path / "never-rendered"),
            "HOME_DIR": str(tmp_path / "home"),
            "GUIDES_ROOT": str(tmp_path / "guides"),
        }
    )
    with pytest.raises(ReviewFailed, match="bootstrap"):
        run_review(settings, "x", "m", "high")


def test_a_missing_guides_checkout_is_a_named_failure(tmp_path: Path) -> None:
    """`deploy/guides-clone.sh` makes the checkout in an init container. If
    it is not there the review cannot write anything back, and saying so by
    name beats `--add-dir` failing somewhere inside the CLI."""
    env = _rendered(tmp_path)
    env["GUIDES_ROOT"] = str(tmp_path / "no-guides-here")
    settings = Settings.from_env({"CLAUDE_PATH": str(tmp_path / "no-such-claude"), **env})
    with pytest.raises(ReviewFailed, match="guides checkout"):
        run_review(settings, "x", "m", "high")
