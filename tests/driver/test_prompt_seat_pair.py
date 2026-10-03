"""T5: the driver's prompts for the seat pair.

With `SEAT_AGENT_CHANNEL` other than `off` and a `seat` role, the seat's own
claude is a Sonnet orchestrator that never acts in the game: it runs two Opus
`seat-player` agents (`current` holds the baton and acts, `future` plans read
only) and rotates the baton between them. Everything else - `off`, or an
advisor - renders byte-identical to the single-agent prose.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from magic_cards_model_api.playscripts.report import DevelopmentCandidates
from mtg_agent import seat_pair
from mtg_agent.driver.config import DriverSettings
from mtg_agent.driver.guides import NONE, Identification, parse_decklist
from mtg_agent.driver.prompt import (
    HANDOVER_OPENING,
    WHOLE_GAME_TURN,
    WHOLE_GAME_TURN_PAIR,
    Assignment,
    continue_turn,
    handover_turn,
    reflection_turn,
    turn_zero,
)
from mtg_agent.seat_pair import Channel
from mtg_agent.wrapper.config import Settings as WrapperSettings
from tests.driver.world import build_world

CONTRACT = Path(__file__).resolve().parents[2] / "contract" / "tools.json"
GAME_PATH = Path("/driver-workspace/current-game.md")
MEMORY = Path("/workspace/memory")
BRIEFING = "# current game\nyou are bob\n"
PAIR = (Channel.TEAMS, Channel.SUBAGENTS)


def _assignment(role: str = "seat") -> Assignment:
    return Assignment(
        game_id="g-1",
        uid="secret-uid",
        player="bob",
        opponent="alice",
        api_base="http://game:8000",
        decklist=parse_decklist("4 Psychic Frog\n"),
        identification=Identification(NONE),
        role=role,
    )


def _tables() -> dict:
    return {
        "offers": "play | run_id\nfrog-attack | r1",
        "hand_played": "line | times | candidate\ncast Fatal Push | 2 | candidate",
        "evidence": "play | games\nfrog-attack | 1",
        "candidates": DevelopmentCandidates(plays=("frog-attack",), lines=()),
    }


def _handover(assignment: Assignment, **kwargs) -> str:
    return handover_turn(assignment, "bob won", "run table", "(none)", **_tables(), **kwargs)


def _renders(assignment: Assignment, **kwargs) -> dict[str, str]:
    return {
        "turn_zero": turn_zero(assignment, GAME_PATH, BRIEFING, **kwargs),
        "continue_turn": continue_turn(assignment, **kwargs),
        "reflection_turn": reflection_turn(assignment, MEMORY, "bob won", **kwargs),
        "handover_turn": _handover(assignment, **kwargs),
    }


def _flat(text: str) -> str:
    return " ".join(text.split())


# ---- (a) the channel reaches the driver, and off/advisor change nothing -----


@pytest.mark.parametrize(
    ("raw", "channel"),
    [("", Channel.TEAMS), ("off", Channel.OFF), ("Subagents", Channel.SUBAGENTS)],
)
def test_driver_settings_read_the_channel_with_the_wrappers_parser(raw, channel):
    env = {"SEAT_AGENT_CHANNEL": raw}
    assert DriverSettings.from_env(env).seat_agent_channel is channel
    assert WrapperSettings.from_env(env).seat_agent_channel is channel


def test_driver_settings_refuse_an_unknown_channel_naming_the_allowed_values():
    with pytest.raises(ValueError, match="allowed values are off, teams, subagents"):
        DriverSettings.from_env({"SEAT_AGENT_CHANNEL": "carrier-pigeon"})


def test_the_driver_default_channel_is_the_wrappers_default():
    assert DriverSettings().seat_agent_channel is WrapperSettings().seat_agent_channel


def test_channel_off_renders_every_prompt_exactly_as_the_single_agent_seat():
    seat = _assignment()
    assert _renders(seat, channel=Channel.OFF) == _renders(seat)


@pytest.mark.parametrize("channel", PAIR)
def test_an_advisor_renders_every_prompt_as_today_whatever_the_channel(channel):
    buddy = _assignment("advisor")
    assert _renders(buddy, channel=channel) == _renders(buddy)


def test_the_single_agent_prose_carries_no_pair_vocabulary():
    for name, text in _renders(_assignment()).items():
        for word in ("orchestrator", "seat-player", "pass_baton", "current-1", "future-1"):
            assert word not in text, f"{name} mentions {word!r} with the pair off"


def test_the_single_agent_turn_zero_keeps_its_own_loop():
    text = turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=Channel.OFF)
    assert "## The loop" in text
    assert WHOLE_GAME_TURN in text


# ---- (b) turn zero in pair mode is the orchestrator brief -------------------


@pytest.mark.parametrize("channel", PAIR)
def test_pair_turn_zero_makes_the_seat_the_orchestrator_that_never_acts(channel):
    text = _flat(turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=channel))
    assert "You are the orchestrator" in text
    assert "you never call an acting `mtg` tool" in text
    assert "`pass_baton`" in text and "`report_problem`" in text


@pytest.mark.parametrize("channel", PAIR)
def test_pair_turn_zero_carries_the_ids_and_the_file_path_not_the_briefing(channel):
    """The driver delivers the briefing as /workspace/current-game.md (S3/S4):
    the orchestrator neither reads nor writes it, and turn 0 carries no body."""
    text = _flat(turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=channel))
    assert BRIEFING.strip().splitlines()[-1] not in text
    assert "# current game" not in text
    assert "`/workspace/current-game.md`" in text
    assert str(GAME_PATH) not in text
    assert "g-1" in text and "'secret-uid'" in text and "'bob'" in text
    assert "`Write`" not in text


@pytest.mark.parametrize("channel", PAIR)
def test_pair_turn_zero_mints_live_then_next_and_spawns_both_in_one_message(channel):
    text = _flat(turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=channel))
    start = text.split("## On start", 1)[1].split("##", 1)[0]
    steps = [
        "1. Call `pass_baton(player_uid=...)`",
        "`state: live`",
        "for `current-1`",
        "2. Call `pass_baton(player_uid=...)` again",
        "`state: next`",
        "for `future-1`",
        "3. Spawn `current-1` and `future-1` in one message",
    ]
    positions = [start.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "`subagent_type` `seat-player`" in text


@pytest.mark.parametrize("channel", PAIR)
def test_pair_turn_zero_states_the_three_step_cycle_on_each_committed_report(channel):
    text = _flat(turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=channel))
    cycle = text.split("## On every `committed:` report", 1)[1].split("## ", 1)[0]
    assert "once per priority window" in cycle
    steps = [
        "1. Stop the agent that reported",
        "2. Call `pass_baton(player_uid=...)` for a fresh token",
        "`state: next`",
        "3. Spawn `future-<n+1>`",
        "That is the whole cycle",
    ]
    positions = [cycle.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "act on each report with the listed calls only" in cycle
    assert "no analysis" in cycle
    assert "exactly two live player agents" in text


@pytest.mark.parametrize("channel", PAIR)
def test_pair_turn_zero_never_promotes_relays_or_reissues(channel):
    """The server rotates the baton at the end of the priority window (S1):
    nothing about promotion by message, carrying plans, or handing a live
    agent a fresh token survives in the orchestrator's brief."""
    text = _flat(turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=channel))
    assert "promoted:" not in text
    assert "relay" not in text.lower()
    assert "Forward each plan" not in text
    assert "re-issue the baton" not in text
    assert "never hand a token to an agent that already holds one" in text
    assert "the server makes" in text


@pytest.mark.parametrize("channel", PAIR)
def test_pair_turn_zero_never_names_isolation_and_spawns_with_fixed_parameters(channel):
    """Defect 6: `Agent` with `isolation: worktree` failed with "Cannot
    create agent worktree". The brief lists the spawn parameters and allows
    no other, without naming the one that broke."""
    text = _flat(turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=channel))
    assert "isolation" not in text.lower()
    assert "worktree" not in text.lower()
    assert "and no other parameter" in text


@pytest.mark.parametrize("channel", PAIR)
def test_pair_turn_zero_recovers_a_lost_current_with_a_revoke(channel):
    text = _flat(turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=channel))
    recovery = text.split("## Recovery only", 1)[1].split("## ", 1)[0]
    assert "without a `committed:` report" in recovery
    assert "`pass_baton(player_uid=..., revoke=true)`" in recovery
    revoke = recovery.index("revoke=true")
    hand = recovery.index("to the future agent")
    assert revoke < hand
    assert "`BATON_STALE`" not in text and "`BATON_REQUIRED`" not in text


@pytest.mark.parametrize("channel", PAIR)
def test_pair_turn_zero_stops_both_agents_when_the_game_ends(channel):
    text = _flat(turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=channel))
    assert "When the game ends" in text
    assert "stop both player agents" in text


def test_teams_channel_spawns_teammates_and_reads_reports_as_their_messages():
    text = _flat(turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=Channel.TEAMS))
    assert "teammate" in text
    assert '`SendMessage` with `{"type": "shutdown_request"}`' in text
    assert "there is no `TeamCreate`" in text
    assert "`run_in_background`" not in text
    assert "`TaskStop`" not in text
    assert "message each other" not in text


def test_subagents_channel_spawns_background_agents_whose_run_end_is_the_report():
    text = _flat(turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=Channel.SUBAGENTS))
    assert "`run_in_background` set to true" in text
    assert "`TaskStop`" in text
    assert "ends its run" in text
    assert "shutdown_request" not in text


# ---- (c) a continuation re-establishes the pair, it does not act ------------


@pytest.mark.parametrize("channel", PAIR)
def test_pair_continue_turn_re_establishes_the_pair_rather_than_acting(channel):
    text = continue_turn(_assignment(), channel=channel)
    assert text.startswith("The game is not over")
    flat = _flat(text)
    assert "re-establish the pair" in flat
    assert "check which player agents are alive" in flat
    assert "respawn any that is missing" in flat
    assert "`pass_baton(player_uid=...)`" in flat
    assert "Keep playing: call `wait_for_priority" not in flat


@pytest.mark.parametrize("channel", PAIR)
def test_pair_continue_turn_never_rotates_blindly(channel):
    flat = _flat(continue_turn(_assignment(), channel=channel))
    assert "re-issue the baton" not in flat
    assert "both are alive, call nothing" in flat
    assert "by the `state` its answer gives" in flat
    assert "`pass_baton(player_uid=..., revoke=true)`" in flat
    assert "never rotate" in flat


@pytest.mark.parametrize("channel", PAIR)
def test_pair_whole_game_turn_tells_the_orchestrator_to_re_establish_the_pair(channel):
    text = turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=channel)
    assert WHOLE_GAME_TURN_PAIR in text
    assert WHOLE_GAME_TURN not in text
    flat = _flat(WHOLE_GAME_TURN_PAIR)
    assert "re-establish the pair" in flat
    assert "respawn" in flat
    assert "re-issue the baton" not in flat


# ---- (d) reflection and handover in pair mode -------------------------------


@pytest.mark.parametrize("channel", PAIR)
def test_pair_reflection_asks_the_final_current_agent_before_stopping_it(channel):
    seat = _assignment()
    text = reflection_turn(seat, MEMORY, "bob won", channel=channel)
    assert text.startswith("The game is over: bob won")
    flat = _flat(text)
    asks = flat.index("ask the final current agent for its reflection notes")
    stops = flat.index("then stop it")
    assert asks < stops
    assert "The final current agent played this game; you did not" in flat
    # the files are written exactly as today, from its notes
    assert _flat(reflection_turn(seat, MEMORY, "bob won").split("\n\n", 2)[2]) in flat


@pytest.mark.parametrize("channel", PAIR)
def test_pair_handover_stops_both_player_agents_before_the_work_starts(channel):
    seat = _assignment()
    text = _handover(seat, channel=channel)
    assert text.startswith(HANDOVER_OPENING)
    flat = _flat(text)
    stop = flat.index("Stop both player agents before any handover work starts")
    assert stop < flat.index("## What to do")
    assert _flat(_handover(seat).split("\n\n", 1)[1]) in flat


# ---- (e) the decision loop lives in the seat-player prompt ------------------


@pytest.mark.parametrize("channel", PAIR)
def test_pair_turn_zero_references_the_seat_player_loop_rather_than_duplicating_it(channel):
    text = turn_zero(_assignment(), GAME_PATH, BRIEFING, channel=channel)
    assert "## The loop" not in text
    assert "Your steady state is two tool calls" not in text
    assert "carries the decision loop" in _flat(text)
    assert "plays a whole priority window" in _flat(text)


def test_the_seat_player_prompt_carries_every_rule_turn_zero_references():
    from mtg_agent.wrapper.bootstrap import SEAT_PLAYER_PROMPT

    flat = _flat(SEAT_PLAYER_PROMPT)
    current = flat.split("## The current role", 1)[1].split("## Refusals", 1)[0]
    for needle in (
        "play the whole priority window",
        "one wait then one committing call per decision",
        "DSL-first",
        "call `declare_plan` during the opponent's turn",
        "arming never runs a play",
        "start the armed play yourself with `run_script`",
    ):
        assert needle in current, needle


# ---- (f) every tool the pair prose names is real -----------------------------

#: Claude Code's own tools and their arguments, which the pair prose names to
#: drive the agents; never `mtg` MCP surface.
CLAUDE_VOCABULARY = {
    "Agent",
    "SendMessage",
    "TaskStop",
    "Write",
    "subagent_type",
    "run_in_background",
    "name",
    "prompt",
    "to",
    "task_id",
    "type",
    "shutdown_request",
    "TeamCreate",
    "mtg",
    "committed",
    # the field of `pass_baton`'s answer the orchestrator reads
    "state",
}


def _contract() -> tuple[set[str], set[str]]:
    tools = json.loads(CONTRACT.read_text())["tools"]
    return {t["name"] for t in tools}, {p["name"] for t in tools for p in t["parameters"]}


@pytest.mark.parametrize("channel", PAIR)
def test_every_tool_the_pair_prose_names_exists_in_the_contract(channel):
    tools, params = _contract()
    seat = _assignment()
    pair_prose = "\n".join(
        [
            turn_zero(seat, GAME_PATH, "", channel=channel),
            continue_turn(seat, channel=channel),
            reflection_turn(seat, MEMORY, "bob won", channel=channel),
            _handover(seat, channel=channel),
        ]
    )
    calls = re.findall(r"`([a-z_]+)\(([^()`]*)\)`", pair_prose)
    assert calls
    for name, args in calls:
        assert name in tools, f"pair prose calls `{name}(...)`, not a contract tool"
        for kwarg in re.findall(r"([a-z_]+)=", args):
            assert kwarg in params
    for token in re.findall(r"`([A-Za-z_]+)`", pair_prose):
        if token.isupper():
            continue  # a refusal code such as BATON_STALE
        assert (
            token in tools
            or token in params
            or token in CLAUDE_VOCABULARY
            or (token in _single_agent_tokens())
        ), f"pair prose names `{token}`, which is no contract tool or parameter"


def test_every_call_the_seat_player_prompt_shows_exists_in_the_contract():
    """`await_baton` and the window-closing calls the seat-player definition
    names are real tools with real parameters (`test_prompt_matches_tools.py`
    reads only prompt.py; the seat-player prose lives in the bootstrap)."""
    from mtg_agent.wrapper.bootstrap import SEAT_PLAYER_PROMPT

    tools, params = _contract()
    assert "await_baton" in tools
    calls = re.findall(r"`([a-z_]+)\(([^()`]*)\)`", SEAT_PLAYER_PROMPT)
    assert "await_baton" in {name for name, _ in calls}
    for name, args in calls:
        assert name in tools, f"seat-player prose calls `{name}(...)`, not a contract tool"
        for kwarg in re.findall(r"([a-z_]+)=", args):
            assert kwarg in params, f"`{name}({kwarg}=...)` is no contract parameter"


def _single_agent_tokens() -> set[str]:
    """Tokens the shared single-agent prose (reflection, handover) already
    carries - `test_prompt_matches_tools.py` owns those."""
    seat = _assignment()
    text = reflection_turn(seat, MEMORY, "bob won") + _handover(seat)
    return set(re.findall(r"`([A-Za-z_]+)`", text))


def test_the_pair_prose_shows_pass_baton_as_a_call_the_contract_guard_checks():
    source = (
        Path(__file__).resolve().parents[2] / "src" / "mtg_agent" / "driver" / "prompt.py"
    ).read_text()
    assert "`pass_baton(player_uid=...)`" in source


# ---- the driver app passes its channel into the prompts ----------------------


def _dispatch(world) -> None:
    response = world.client.post(
        "/dispatch",
        json={
            "uid": world.uid,
            "game_id": world.game_id,
            "player": "bob",
            "api_base": "http://game:8000",
        },
    )
    assert response.status_code == 202, response.text


def _finish(world, turn_id: str, **fields) -> dict:
    body = {"turnId": turn_id, "state": "complete", "finalText": "x"} | fields
    return world.client.post("/turn-done", json=body).json()


@pytest.mark.parametrize("channel", PAIR)
def test_the_app_renders_turn_zero_and_continuations_for_its_channel(tmp_path, channel):
    world = build_world(tmp_path, channel=channel)
    _dispatch(world)
    assert "You are the orchestrator" in world.submitted[0]["text"]
    _finish(world, "turn-1", stopReason="end_turn")
    assert "re-establish the pair" in _flat(world.submitted[1]["text"])
    world.state["winner"] = "bob"
    _finish(world, "turn-2")
    assert "ask the final current agent" in _flat(world.submitted[2]["text"])


def test_the_app_with_the_pair_off_renders_the_single_agent_prompts(tmp_path):
    world = build_world(tmp_path, channel=Channel.OFF)
    _dispatch(world)
    assert "## The loop" in world.submitted[0]["text"]
    assert "orchestrator" not in world.submitted[0]["text"]


@pytest.mark.parametrize("channel", PAIR)
def test_the_app_handover_stops_the_pair_first(tmp_path, channel):
    world = build_world(tmp_path, channel=channel)
    _dispatch(world)
    world.state["winner"] = "bob"
    world.client.post("/release", json={"uid": world.uid, "reason": "lobby reset"})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        handovers = [t for t in world.submitted if t["text"].startswith(HANDOVER_OPENING)]
        if handovers:
            break
        time.sleep(0.01)
    assert handovers, "no handover turn was pushed"
    assert "Stop both player agents" in _flat(handovers[0]["text"])


def test_the_seat_player_name_is_the_one_the_prose_spawns():
    assert seat_pair.SEAT_PLAYER == "seat-player"
    assert seat_pair.current_name(1) == "current-1"
    assert seat_pair.future_name(2) == "future-2"
