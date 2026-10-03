import json
import stat
import subprocess

from mtg_agent.wrapper import bootstrap
from mtg_agent.wrapper.config import Settings


def settings_in(tmp_path, **overrides) -> Settings:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    return Settings.from_env({"WORKSPACE": str(workspace), "HOME_DIR": str(home)} | overrides)


def test_claude_json_seeds_every_dialog_suppressor(tmp_path):
    s = settings_in(tmp_path, ANTHROPIC_API_KEY="sk-ant-0123456789abcdefghijKLMNOP")
    bootstrap.write_claude_json(s)
    doc = json.loads((s.home_dir / ".claude.json").read_text())
    assert doc["hasCompletedOnboarding"] is True
    assert doc["projects"][str(s.workspace)]["hasTrustDialogAccepted"] is True
    assert doc["customApiKeyResponses"]["approved"] == ["456789abcdefghijKLMNOP"[-20:]]
    assert doc["customApiKeyResponses"]["rejected"] == []


def test_claude_json_is_0600(tmp_path):
    s = settings_in(tmp_path, ANTHROPIC_API_KEY="sk-ant-secret")
    bootstrap.write_claude_json(s)
    mode = stat.S_IMODE((s.home_dir / ".claude.json").stat().st_mode)
    assert mode == 0o600


def test_claude_json_merges_into_an_existing_file(tmp_path):
    s = settings_in(tmp_path, ANTHROPIC_API_KEY="sk-ant-key")
    path = s.home_dir / ".claude.json"
    path.write_text(json.dumps({"keepMe": 1, "projects": {"/other": {"x": True}}}))
    bootstrap.write_claude_json(s)
    doc = json.loads(path.read_text())
    assert doc["keepMe"] == 1
    assert doc["projects"]["/other"] == {"x": True}
    assert doc["projects"][str(s.workspace)]["hasTrustDialogAccepted"] is True


def test_claude_json_treats_a_corrupt_existing_file_as_absent(tmp_path):
    s = settings_in(tmp_path)
    (s.home_dir / ".claude.json").write_text("{not json")
    bootstrap.write_claude_json(s)
    assert json.loads((s.home_dir / ".claude.json").read_text())["hasCompletedOnboarding"]


def test_claude_json_without_an_api_key_has_no_key_responses(tmp_path):
    s = settings_in(tmp_path)
    bootstrap.write_claude_json(s)
    assert "customApiKeyResponses" not in json.loads((s.home_dir / ".claude.json").read_text())


def test_settings_json_installs_the_stop_hook_and_bypass_mode(tmp_path):
    s = settings_in(tmp_path, HOOK_COMMAND="/usr/local/bin/cc-stop-hook")
    bootstrap.write_settings_json(s)
    doc = json.loads((s.home_dir / ".claude" / "settings.json").read_text())
    stop = doc["hooks"]["Stop"][0]
    assert stop["matcher"] == ""
    assert stop["hooks"] == [{"type": "command", "command": "/usr/local/bin/cc-stop-hook"}]
    assert doc["permissions"]["defaultMode"] == "bypassPermissions"
    assert doc["enableAllProjectMcpServers"] is True


def test_settings_json_denies_the_interactive_tools_a_headless_agent_cannot_use(tmp_path):
    s = settings_in(tmp_path)
    bootstrap.write_settings_json(s)
    deny = json.loads((s.home_dir / ".claude" / "settings.json").read_text())["permissions"]["deny"]
    assert "AskUserQuestion" in deny
    assert "ExitPlanMode" in deny
    assert "EnterPlanMode" in deny


def test_mcp_merge_unions_base_and_overlays_with_overlays_winning(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(
        json.dumps({"mcpServers": {"mtg": {"command": "old"}, "keep": {"type": "http"}}})
    )
    overlay_dir = tmp_path / "mcp.d"
    overlay_dir.mkdir()
    (overlay_dir / "10-mtg.json").write_text(
        json.dumps({"mcpServers": {"mtg": {"command": "new"}}})
    )
    (overlay_dir / "20-extra.json").write_text(
        json.dumps({"mcpServers": {"spellslinger": {"type": "http", "url": "https://x"}}})
    )
    (overlay_dir / "notes.txt").write_text("ignored")
    s = settings_in(tmp_path, BASE_MCP_PATH=str(base), MCP_OVERLAY_DIR=str(overlay_dir))
    bootstrap.merge_mcp(s)
    servers = json.loads((s.workspace / ".mcp.json").read_text())["mcpServers"]
    assert servers["mtg"] == {"command": "new"}
    assert servers["keep"] == {"type": "http"}
    assert servers["spellslinger"]["url"] == "https://x"


def test_mcp_json_is_0600(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"mcpServers": {}}))
    s = settings_in(tmp_path, BASE_MCP_PATH=str(base))
    bootstrap.merge_mcp(s)
    assert stat.S_IMODE((s.workspace / ".mcp.json").stat().st_mode) == 0o600


def test_mcp_merge_with_no_base_and_no_overlay_writes_an_empty_doc(tmp_path):
    s = settings_in(tmp_path)
    bootstrap.merge_mcp(s)
    assert json.loads((s.workspace / ".mcp.json").read_text()) == {"mcpServers": {}}


def test_claude_md_files_are_copied_to_both_locations(tmp_path):
    project = tmp_path / "project.md"
    project.write_text("# project rules")
    glob = tmp_path / "global.md"
    glob.write_text("# global rules")
    s = settings_in(tmp_path, PROJECT_CLAUDE_MD_PATH=str(project), GLOBAL_CLAUDE_MD_PATH=str(glob))
    bootstrap.write_claude_md(s)
    assert (s.workspace / "CLAUDE.md").read_text() == "# project rules"
    assert (s.home_dir / ".claude" / "CLAUDE.md").read_text() == "# global rules"


def test_missing_claude_md_sources_are_skipped_silently(tmp_path):
    s = settings_in(tmp_path, PROJECT_CLAUDE_MD_PATH=str(tmp_path / "gone.md"))
    bootstrap.write_claude_md(s)
    assert not (s.workspace / "CLAUDE.md").exists()


def test_skills_are_copied_in_order_with_later_dirs_winning(tmp_path):
    baked = tmp_path / "baked"
    (baked / "play").mkdir(parents=True)
    (baked / "play" / "SKILL.md").write_text("baked")
    custom = tmp_path / "custom"
    (custom / "play").mkdir(parents=True)
    (custom / "play" / "SKILL.md").write_text("custom")
    (custom / "extra").mkdir()
    (custom / "extra" / "SKILL.md").write_text("extra")
    s = settings_in(tmp_path, SKILLS_DIRS=f"{baked}:{custom}")
    bootstrap.copy_skills(s)
    root = s.workspace / ".claude" / "skills"
    assert (root / "play" / "SKILL.md").read_text() == "custom"
    assert (root / "extra" / "SKILL.md").read_text() == "extra"


def _git_env(home):
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
    }


def _credential_fill(home, host):
    return subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost={host}\n\n",
        env=_git_env(home),
        capture_output=True,
        text=True,
    )


def test_gitconfig_answers_github_credentials_from_the_token_file(tmp_path):
    token = tmp_path / "GH_PAT"
    token.write_text("github_pat_0123456789abcdefghijKLMNOP\n")
    s = settings_in(tmp_path)
    bootstrap.write_gitconfig(s, token_file=token)

    filled = _credential_fill(s.home_dir, "github.com")
    assert filled.returncode == 0, filled.stderr
    lines = filled.stdout.splitlines()
    assert "username=x-access-token" in lines
    assert "password=github_pat_0123456789abcdefghijKLMNOP" in lines


def test_gitconfig_never_contains_the_token_itself(tmp_path):
    token = tmp_path / "GH_PAT"
    token.write_text("github_pat_0123456789abcdefghijKLMNOP\n")
    s = settings_in(tmp_path)
    bootstrap.write_gitconfig(s, token_file=token)
    text = (s.home_dir / ".gitconfig").read_text()
    assert "github_pat_0123456789abcdefghijKLMNOP" not in text
    assert str(token) in text


def test_gitconfig_helper_is_scoped_to_github_and_trusts_the_guides_checkout(tmp_path):
    token = tmp_path / "GH_PAT"
    token.write_text("github_pat_0123456789abcdefghijKLMNOP\n")
    s = settings_in(tmp_path)
    bootstrap.write_gitconfig(s, token_file=token)
    path = s.home_dir / ".gitconfig"

    def get(*key):
        return subprocess.run(
            ["git", "config", "--file", str(path), *key],
            capture_output=True,
            text=True,
            env=_git_env(s.home_dir),
        )

    assert get("--get", "credential.https://github.com.helper").stdout.strip()
    assert get("--get-all", "credential.helper").stdout == ""
    assert get("--get-all", "safe.directory").stdout.splitlines() == ["/guides"]
    other = _credential_fill(s.home_dir, "gitlab.example.com")
    assert "github_pat_" not in other.stdout


def test_gitconfig_helper_stays_silent_without_a_token_file(tmp_path):
    s = settings_in(tmp_path)
    bootstrap.write_gitconfig(s, token_file=tmp_path / "absent")
    filled = _credential_fill(s.home_dir, "github.com")
    assert "password=" not in filled.stdout


def test_render_writes_the_gitconfig_with_the_mounted_token_path(tmp_path):
    s = settings_in(tmp_path)
    bootstrap.render(s)
    text = (s.home_dir / ".gitconfig").read_text()
    assert "/var/run/mtg-decks-git/GH_PAT" in text


def test_render_produces_every_file_in_one_call(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"mcpServers": {"mtg": {"command": "x"}}}))
    s = settings_in(tmp_path, BASE_MCP_PATH=str(base), ANTHROPIC_API_KEY="sk-ant-abc")
    bootstrap.render(s)
    assert (s.home_dir / ".claude.json").exists()
    assert (s.home_dir / ".claude" / "settings.json").exists()
    assert (s.workspace / ".mcp.json").exists()


# ---- the rules-researcher subagent (SUB, 2026-09-23) -----------------------
#
# The seat hands rules questions to a background subagent while it waits, so
# its own priority-holding time is never spent on research. The subagent is a
# project agent under the workspace (claude's cwd), rendered from code so the
# allowlist is pinned here rather than in a hand-edited file.


def _frontmatter(path) -> tuple[dict[str, str], str]:
    text = path.read_text()
    assert text.startswith("---\n")
    head, body = text[4:].split("\n---\n", 1)
    fields = {}
    for row in head.splitlines():
        key, _, value = row.partition(":")
        fields[key.strip()] = value.strip()
    return fields, body


def _names(value: str) -> list[str]:
    return [name.strip() for name in value.split(",") if name.strip()]


def researcher(tmp_path, **env):
    s = settings_in(tmp_path, **env)
    bootstrap.render(s)
    return _frontmatter(s.workspace / ".claude" / "agents" / "rules-researcher.md")


def test_render_writes_the_rules_researcher_as_a_project_agent(tmp_path):
    fields, _ = researcher(tmp_path)
    assert fields["name"] == bootstrap.RULES_RESEARCHER == "rules-researcher"
    assert fields["description"]


def test_the_rules_researcher_description_tells_the_seat_when_to_background_it(tmp_path):
    """2026-09-28 speed pass: the launch guidance used to live only in
    `prompt.turn_zero`/`ai_seat_brief.md` - this puts it on the subagent's
    own `description` too, the text a seat reads right when it is deciding
    how to launch it, so the choice does not depend on having read the
    briefing closely. `run_in_background: true` is the default; foreground
    is the exception, reserved for a decision that depends on the answer
    right now."""
    fields, _ = researcher(tmp_path)
    desc = fields["description"]
    assert "\n" not in desc, "the frontmatter line must stay a single line"
    assert "run_in_background: true" in desc
    assert "foreground" in desc
    assert "depend" in desc


def test_the_rules_researcher_may_only_look_things_up(tmp_path):
    fields, _ = researcher(tmp_path)
    tools = _names(fields["tools"])
    assert tools == list(bootstrap.RULES_RESEARCHER_TOOLS)
    assert "mcp__spellslinger__knowledge_search" in tools
    assert "mcp__spellslinger__resolve_cards" in tools
    for name in tools:
        assert name.startswith("mcp__spellslinger__") or name in {"Read", "Grep", "Glob"}, name


def test_the_rules_researcher_can_never_act_in_the_game(tmp_path):
    """Allowlisted by `tools`, and the game-acting and writing tools denied
    outright on top: a subagent must never move the game or write a file."""
    fields, _ = researcher(tmp_path)
    tools = _names(fields["tools"])
    assert not [name for name in tools if name.startswith("mcp__mtg")]
    assert not {"Bash", "Write", "Edit", "NotebookEdit", "Agent", "Task"} & set(tools)
    denied = set(_names(fields["disallowedTools"]))
    assert {"mcp__mtg", "Bash", "Write", "Edit", "NotebookEdit", "Agent", "Task"} <= denied


def test_the_rules_researcher_model_is_explicit_and_configurable(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert researcher(tmp_path / "a")[0]["model"] == "sonnet"
    assert researcher(tmp_path / "b", RULES_RESEARCHER_MODEL="haiku")[0]["model"] == "haiku"


def test_the_rules_researcher_answers_one_question_with_verified_citations(tmp_path):
    _, body = researcher(tmp_path)
    flat = " ".join(body.split())
    assert "one rules question" in flat
    assert "knowledge_search" in flat
    assert "CR" in flat
    assert "never from memory" in flat
    assert "cannot act in the game" in flat


def test_the_seat_itself_may_still_launch_subagents(tmp_path):
    s = settings_in(tmp_path)
    bootstrap.write_settings_json(s)
    deny = json.loads((s.home_dir / ".claude" / "settings.json").read_text())["permissions"]["deny"]
    assert "Agent" not in deny
    assert "Task" not in deny


def test_render_writes_the_seat_player_for_a_seat_but_not_an_advisor(tmp_path):
    (tmp_path / "seat").mkdir()
    (tmp_path / "adv").mkdir()
    seat = settings_in(tmp_path / "seat")
    bootstrap.render(seat)
    assert (seat.workspace / ".claude" / "agents" / "seat-player.md").is_file()
    adv = settings_in(tmp_path / "adv", MTG_ROLE="advisor")
    bootstrap.render(adv)
    assert not (adv.workspace / ".claude" / "agents" / "seat-player.md").exists()
    doc = json.loads((adv.home_dir / ".claude" / "settings.json").read_text())
    assert "teammateMode" not in doc


def test_settings_json_is_unchanged_when_the_channel_is_off(tmp_path):
    s = settings_in(tmp_path, SEAT_AGENT_CHANNEL="off")
    bootstrap.write_settings_json(s)
    doc = json.loads((s.home_dir / ".claude" / "settings.json").read_text())
    assert set(doc) == {"hooks", "enableAllProjectMcpServers", "permissions"}


# ---- the seat-player definition: await the baton, play a whole window ------


def _seat_player_prompt(tmp_path, **env) -> str:
    s = settings_in(tmp_path, **env)
    bootstrap.render(s)
    body = (s.workspace / ".claude" / "agents" / "seat-player.md").read_text()
    return " ".join(body.split("---\n", 2)[2].split())


def _section(flat: str, heading: str) -> str:
    return flat.split(f"## {heading}", 1)[1].split("## ", 1)[0]


def test_the_future_reads_the_briefing_once_plans_one_window_then_awaits_the_baton(tmp_path):
    future = _section(_seat_player_prompt(tmp_path), "The future role")
    steps = [
        "Read /workspace/current-game.md once",
        "one concrete `run_script` body",
        "ending in a pass",
        "`await_baton(player_uid=..., baton=<your token>, max_seconds=600)`",
        "in a loop",
    ]
    positions = [future.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "BATON_NOT_YET" in future and "keep awaiting" in future
    assert "never call an acting tool" in future


def test_the_future_checks_last_commit_and_the_pending_decision_when_it_goes_live(tmp_path):
    flat = _seat_player_prompt(tmp_path)
    assert "`live: true`" in flat
    assert "`last_commit`" in flat and "`pending_decision`" in flat
    assert "revalidate the inherited plan" in flat


def test_the_current_plays_the_whole_window_and_only_the_pass_ends_its_role(tmp_path):
    current = _section(_seat_player_prompt(tmp_path), "The current role")
    assert "play the whole priority window" in current
    assert "as many actions as the window needs" in current
    assert "prefer one `run_script` covering land, spells, attack and pass" in current
    for give_away in ("`pass`", "`pass_until`", "`arm=[...]`", "`then_pass`"):
        assert give_away in current, give_away
    assert "Only the pass ends your role" in current


def test_the_current_ends_its_run_after_the_pass_with_a_committed_line(tmp_path):
    flat = _seat_player_prompt(tmp_path)
    ends = _section(flat, "When your window closes")
    order = ["`baton_rotated: true`", "BATON_SPENT", "end your run with `committed: <one line>`"]
    positions = [ends.index(step) for step in order]
    assert positions == sorted(positions)
    assert "call nothing else" in ends
    assert "SendMessage" in ends and "`team-lead`" in ends


def test_the_seat_player_handles_every_server_baton_refusal(tmp_path):
    refusals = _section(_seat_player_prompt(tmp_path), "Refusals")
    assert "`BATON_NOT_YET`" in refusals and "keep awaiting" in refusals
    assert "`BATON_BUSY`" in refusals and "own in-flight call" in refusals
    assert "`BATON_SPENT`" in refusals
    assert "`BATON_REQUIRED`" in refusals and "`BATON_STALE`" in refusals


def test_the_seat_player_never_waits_on_a_promotion_message(tmp_path):
    flat = _seat_player_prompt(tmp_path)
    assert "promoted:" not in flat
    assert "Send plans to the orchestrator" not in flat
    assert "continuing you" not in flat
