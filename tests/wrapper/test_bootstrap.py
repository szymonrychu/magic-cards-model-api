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
