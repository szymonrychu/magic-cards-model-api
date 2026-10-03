import json

import pytest

from mtg_agent import seat_pair
from mtg_agent.seat_pair import Channel
from mtg_agent.wrapper import bootstrap
from mtg_agent.wrapper.config import Settings


def settings_in(tmp_path, **overrides) -> Settings:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    return Settings.from_env({"WORKSPACE": str(workspace), "HOME_DIR": str(home)} | overrides)


def _frontmatter(path):
    head, body = path.read_text()[4:].split("\n---\n", 1)
    fields = {}
    for row in head.splitlines():
        key, _, value = row.partition(":")
        fields[key.strip()] = value.strip()
    return fields, body


def _names(value):
    return [n.strip() for n in value.split(",") if n.strip()]


def agent_file(s):
    return s.workspace / ".claude" / "agents" / "seat-player.md"


def settings_json(s):
    return json.loads((s.home_dir / ".claude" / "settings.json").read_text())


def test_channel_parses_the_allowed_values_and_defaults_to_teams():
    assert Channel.parse("off") is Channel.OFF
    assert Channel.parse(" Teams ") is Channel.TEAMS
    assert Channel.parse("subagents") is Channel.SUBAGENTS
    assert Channel.parse("") is Channel.TEAMS
    assert Settings.from_env({}).seat_agent_channel is Channel.TEAMS


def test_an_invalid_channel_is_a_startup_error_naming_the_allowed_values():
    with pytest.raises(ValueError) as err:
        Settings.from_env({"SEAT_AGENT_CHANNEL": "carrier-pigeon"})
    for allowed in ("off", "teams", "subagents"):
        assert allowed in str(err.value)
    assert "carrier-pigeon" in str(err.value)


def test_settings_seat_subagent_model_and_effort():
    s = Settings.from_env({})
    assert s.seat_subagent_model == "claude-opus-5"
    assert s.seat_subagent_effort == ""
    s = Settings.from_env({"SEAT_SUBAGENT_MODEL": "opus", "SEAT_SUBAGENT_EFFORT": "high"})
    assert (s.seat_subagent_model, s.seat_subagent_effort) == ("opus", "high")


def test_role_names_and_agent_naming_helpers():
    assert seat_pair.ROLE_CURRENT == "current"
    assert seat_pair.ROLE_FUTURE == "future"
    assert seat_pair.SEAT_PLAYER == "seat-player"
    assert seat_pair.current_name(3) == "current-3"
    assert seat_pair.future_name(4) == "future-4"


def test_pair_active_only_for_a_seat_with_a_channel():
    assert seat_pair.pair_active(Channel.TEAMS, "seat")
    assert seat_pair.pair_active(Channel.SUBAGENTS, "seat")
    assert not seat_pair.pair_active(Channel.OFF, "seat")
    assert not seat_pair.pair_active(Channel.TEAMS, "advisor")
    assert not seat_pair.pair_active(Channel.SUBAGENTS, "buddy")


def test_settings_role_defaults_to_seat_and_reads_mtg_role():
    assert Settings.from_env({}).role == "seat"
    assert Settings.from_env({"MTG_ROLE": "advisor"}).role == "advisor"


def test_claude_args_are_unchanged_by_the_pair():
    s = Settings.from_env({"MODEL": "m", "EFFORT": "low", "SEAT_AGENT_CHANNEL": "teams"})
    assert s.claude_args(False) == ["--model", "m", "--effort", "low"]


@pytest.mark.parametrize(
    ("env", "teams"),
    [
        ({}, True),
        ({"SEAT_AGENT_CHANNEL": "teams"}, True),
        ({"SEAT_AGENT_CHANNEL": "subagents"}, False),
        ({"SEAT_AGENT_CHANNEL": "off"}, False),
        ({"SEAT_AGENT_CHANNEL": "teams", "MTG_ROLE": "advisor"}, False),
    ],
)
def test_settings_json_teams_keys_only_when_teams_and_pair_active(tmp_path, env, teams):
    s = settings_in(tmp_path, **env)
    bootstrap.write_settings_json(s)
    doc = settings_json(s)
    if teams:
        assert doc["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
        assert doc["teammateMode"] == "in-process"
    else:
        assert "env" not in doc
        assert "teammateMode" not in doc


def render_player(tmp_path, **env):
    s = settings_in(tmp_path, **env)
    bootstrap.render(s)
    return s


def test_seat_player_frontmatter(tmp_path):
    s = render_player(tmp_path)
    fields, _ = _frontmatter(agent_file(s))
    assert fields["name"] == "seat-player"
    assert fields["description"]
    assert fields["model"] == "claude-opus-5"
    tools = _names(fields["tools"])
    contract = [t["name"] for t in json.loads(bootstrap.TOOLS_CONTRACT.read_text())["tools"]]
    mtg = {t for t in tools if t.startswith("mcp__mtg__")}
    assert mtg == {f"mcp__mtg__{n}" for n in contract if n != "pass_baton"}
    assert "mcp__mtg__pass_baton" not in tools
    assert "mcp__spellslinger__knowledge_search" in tools
    assert "mcp__spellslinger__resolve_cards" in tools
    assert {"Read", "Grep", "Glob", "SendMessage"} <= set(tools)
    assert set(_names(fields["disallowedTools"])) == {
        "mcp__mtg__pass_baton",
        "Bash",
        "Write",
        "Edit",
        "NotebookEdit",
        "Agent",
        "Task",
        "TeamCreate",
        "TeamDelete",
    }
    assert not {"Bash", "Write", "Edit", "Agent"} & set(tools)


def test_seat_player_has_no_send_message_on_the_subagents_channel(tmp_path):
    s = render_player(tmp_path, SEAT_AGENT_CHANNEL="subagents")
    fields, _ = _frontmatter(agent_file(s))
    assert "SendMessage" not in _names(fields["tools"])


def test_seat_player_effort_is_not_a_frontmatter_key(tmp_path):
    """2.1.282's fixtures do not show agent-frontmatter effort honoured."""
    s = render_player(tmp_path, SEAT_SUBAGENT_EFFORT="high")
    fields, _ = _frontmatter(agent_file(s))
    assert "effort" not in fields


def test_seat_player_model_follows_settings(tmp_path):
    s = render_player(tmp_path, SEAT_SUBAGENT_MODEL="opus")
    assert _frontmatter(agent_file(s))[0]["model"] == "opus"


def test_seat_player_role_prompt_key_sentences(tmp_path):
    s = render_player(tmp_path)
    flat = " ".join(_frontmatter(agent_file(s))[1].split())
    for needle in (
        "/workspace/current-game.md",
        "future role",
        "current role",
        "BATON_EXEMPT_TOOLS",
        "never call an acting tool",
        "when-clauses",
        "arm",
        "one wait then one committing call per decision",
        "DSL-first",
        "pass the baton on every acting call",
        "revalidate the inherited plan",
        "committed: <one line>",
        "BATON_STALE",
        "you were demoted: stop acting",
    ):
        assert needle in flat, needle


@pytest.mark.parametrize(
    "env",
    [{"SEAT_AGENT_CHANNEL": "off"}, {"MTG_ROLE": "advisor"}],
)
def test_no_seat_player_when_the_pair_is_inactive(tmp_path, env):
    s = render_player(tmp_path, **env)
    assert not agent_file(s).exists()
    assert (s.workspace / ".claude" / "agents" / "rules-researcher.md").exists()
    assert "env" not in settings_json(s)


def test_baton_exempt_names_match_the_server():
    from magic_cards_model_api.baton import BATON_EXEMPT_TOOLS

    assert set(seat_pair.BATON_EXEMPT_TOOLS) == set(BATON_EXEMPT_TOOLS)
