"""What the agent is doing right now, as one coarse word.

The play UI used to say "AI (claude): thinking" for the whole of a match,
whatever the agent was actually doing. The transcript already says: every
`mcp__mtg__*` call, every `mcp__spellslinger__*` lookup, every `thinking`
block. `activity.observations` and `activity.state_for` are the two pure
functions that turn one transcript line into one of a small set of states,
and `CurrentActivity` is the live holder the wrapper publishes on
`GET /v1/session`.

The hard constraint these tests pin, beyond the mapping itself: **nothing
but the state word leaves this module**. The human opponent reads this label
mid-game, so a tool argument, a card name or a line of the agent's prose
reaching it would hand them the agent's plan. `observations` never yields
text or input at all - that is the property, and
`test_no_argument_card_name_or_prose_can_reach_the_published_state` is the
proof.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from mtg_agent.wrapper import activity


def assistant(*blocks, timestamp: str | None = None) -> dict:
    line = {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}
    if timestamp:
        line["timestamp"] = timestamp
    return line


def tool(name: str, payload: dict | None = None) -> dict:
    return {"type": "tool_use", "name": name, "input": payload or {}}


def text(body: str) -> dict:
    return {"type": "text", "text": body}


def thinking(signature: str = "sig") -> dict:
    """A `thinking` block as it actually arrives: a signature and no text.

    Measured on a live pod and independently on a second machine - 36
    thinking blocks, zero characters of content between them. The block's
    presence is real evidence the model is reasoning, and it is all the
    evidence there is; there is no text here to show and none is shown.
    """
    return {"type": "thinking", "thinking": "", "signature": signature}


# ---- the mapping, one state at a time --------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mcp__mtg__wait_for_priority", activity.WAITING),
        ("mcp__mtg__battlefield_state", activity.READING_BOARD),
        ("mcp__mtg__game_log", activity.READING_BOARD),
        ("mcp__mtg__decide", activity.DECIDING),
        ("mcp__mtg__mulligan", activity.DECIDING),
        ("mcp__mtg__opening_hand_action", activity.DECIDING),
        ("mcp__mtg__declare_plan", activity.DECIDING),
        ("mcp__mtg__list_scripts", activity.READING_BOARD),
        ("mcp__mtg__run_script", activity.ACTING),
        ("mcp__mtg__cast", activity.ACTING),
        ("mcp__mtg__play_land", activity.ACTING),
        ("mcp__mtg__activate", activity.ACTING),
        ("mcp__mtg__pass", activity.ACTING),
        ("mcp__mtg__pass_until", activity.WAITING),
        ("mcp__mtg__declare_attackers", activity.ACTING),
        ("mcp__mtg__declare_blockers", activity.ACTING),
        ("mcp__mtg__cycle", activity.ACTING),
        ("mcp__mtg__foretell", activity.ACTING),
        ("mcp__mtg__plot", activity.ACTING),
        ("mcp__mtg__suspend", activity.ACTING),
        ("mcp__mtg__report_problem", activity.ACTING),
        ("mcp__spellslinger__resolve_cards", activity.READING_CARDS),
        ("mcp__spellslinger__knowledge_search", activity.READING_RULES),
        ("Bash", activity.WORKING),
        ("Read", activity.WORKING),
        ("Write", activity.WORKING),
        ("TodoWrite", activity.WORKING),
        ("Agent", activity.WORKING),
        ("Agent:rules-researcher", activity.RESEARCHING_RULES),
        ("Task:rules-researcher", activity.RESEARCHING_RULES),
    ],
)
def test_every_tool_this_stack_offers_has_a_state(name, expected):
    assert activity.state_for("tool", name) == expected


def epoch(stamp: str) -> float:
    """A transcript timestamp as epoch seconds - the same reading
    `activity._parse_ts` takes, so the fixed clocks below are anchored to the
    line they are timing rather than to a magic constant."""
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def contract_tool_names() -> list[str]:
    path = Path(__file__).resolve().parents[2] / "contract" / "tools.json"
    return [tool["name"] for tool in json.loads(path.read_text())["tools"]]


def test_every_mtg_tool_in_the_contract_maps_to_something():
    """`contract/tools.json` is the `mtg` surface, exported from `mcp.py`'s
    own registrations. A tool added there must not fall off this mapping
    into the generic catch-all without anyone noticing."""
    names = contract_tool_names()
    assert names, "contract/tools.json is empty - the export task has not run"
    for name in names:
        state = activity.state_for("tool", f"mcp__mtg__{name}")
        assert state in (
            activity.WAITING,
            activity.READING_BOARD,
            activity.DECIDING,
            activity.ACTING,
        ), name


def test_the_script_tools_are_mapped_by_name_not_by_the_default():
    """`run_script` plays moves, so it is ACTING - but named, like every other
    tool the mapping has decided on, rather than left to the catch-all.
    `list_scripts` only reads, the same state as the other reads."""
    assert activity._MTG_STATES["run_script"] == activity.ACTING
    assert activity._MTG_STATES["list_scripts"] == activity._MTG_STATES["battlefield_state"]


def test_a_long_pass_until_reads_as_waiting_for_its_whole_wait():
    """L2: `pass_until` re-issues its own capped wait under one tool call while
    the opponent thinks (up to `mcp.IDLE_REISSUE_SECONDS`), so for most of
    its duration the seat is waiting on the opponent, not playing. The pill
    must say so for the whole call - only the one observation, however long
    the call runs - rather than "acting" for minutes."""
    now = [epoch("2026-09-23T10:00:00Z")]
    current = activity.CurrentActivity(now=lambda: now[0])
    current.feed(
        [
            assistant(
                tool("mcp__mtg__pass_until", {"step": "upkeep"}), timestamp="2026-09-23T10:00:00Z"
            )
        ]
    )
    now[0] += 110.0

    snapshot = current.snapshot()

    assert snapshot["state"] == activity.WAITING
    assert snapshot["seq"] == 1


def test_a_tool_result_after_a_decision_call_is_thinking_aged_from_the_result():
    """The exact production stall (game 048e1879, 2026-09-23):
    `opening_hand_action` (DECIDING) returns at 17:28:46, and one Opus call
    then thinks for 106s with no `assistant` block landing in between. The
    published state must move to THINKING the moment the tool result
    arrives, aged from that result's own timestamp - not sit on stale
    'deciding' with no elapsed time for the whole wait."""
    now = [epoch("2026-09-23T17:28:46Z")]
    current = activity.CurrentActivity(now=lambda: now[0])
    current.feed(
        [
            assistant(tool("mcp__mtg__opening_hand_action", {}), timestamp="2026-09-23T17:28:40Z"),
            {
                "type": "user",
                "timestamp": "2026-09-23T17:28:46Z",
                "message": {"content": [{"type": "tool_result", "content": "ok"}]},
            },
        ]
    )
    now[0] += 106.0

    snapshot = current.snapshot()

    assert snapshot["state"] == activity.THINKING
    assert snapshot["seconds"] == pytest.approx(106.0, abs=0.5)


def test_a_thinking_block_is_reported_as_thinking():
    assert activity.state_for("thinking", "") == activity.THINKING


def test_the_agent_s_own_prose_is_reported_as_thinking():
    assert activity.state_for("say", "") == activity.THINKING


# ---- the ones that are easy to get wrong -----------------------------------


def test_an_unknown_mtg_tool_is_a_play_not_a_crash():
    """A tool added to `mcp.py` after this mapping was written. Every `mtg`
    tool that is not a read or a declaration mutates the game, so the
    honest default for the server as a whole is "making its play"."""
    assert activity.state_for("tool", "mcp__mtg__brand_new_thing") == activity.ACTING


def test_an_unknown_spellslinger_tool_is_a_lookup():
    assert activity.state_for("tool", "mcp__spellslinger__whatever") == activity.READING_RULES


def test_an_unknown_mcp_server_falls_back_to_working():
    assert activity.state_for("tool", "mcp__something_else__go") == activity.WORKING


def test_an_unknown_bare_tool_name_falls_back_to_working():
    assert activity.state_for("tool", "SomethingNobodyHasWrittenYet") == activity.WORKING


def test_an_empty_tool_name_falls_back_to_working():
    assert activity.state_for("tool", "") == activity.WORKING


def test_an_event_kind_with_no_mapping_changes_nothing():
    """`None`, not a state: an event this function does not recognise must
    leave whatever the agent was last observed doing in place rather than
    inventing a word for it."""
    assert activity.state_for("tool-error", "mcp__mtg__cast") is None
    assert activity.state_for("", "") is None


# ---- observations: names only, never content -------------------------------


def test_observations_yield_the_kind_and_the_tool_name_and_nothing_else():
    line = assistant(
        thinking(),
        text("Bolt the Ragavan, then hold up Consign."),
        tool("mcp__mtg__cast", {"card": "Lightning Bolt", "reasoning": "kill the Ragavan"}),
    )
    assert list(activity.observations(line)) == [
        ("thinking", ""),
        ("say", ""),
        ("tool", "mcp__mtg__cast"),
    ]


def test_observations_yield_thinking_for_a_tool_result_line():
    """A `tool_result` returning is itself an observation now, not ignored -
    see `observations`'s own docstring for the production stall (game
    048e1879, 2026-09-23) this fixes."""
    result = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    }
    assert list(activity.observations(result)) == [("thinking", "")]


def test_a_failed_tool_result_is_also_a_thinking_observation():
    result = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": "x", "is_error": True}],
        },
    }
    assert list(activity.observations(result)) == [("thinking", "")]


def test_observations_ignore_a_user_line_with_no_tool_result():
    result = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    }
    assert list(activity.observations(result)) == []


def test_observations_survive_a_malformed_line():
    assert list(activity.observations({})) == []
    assert list(activity.observations({"type": "assistant", "message": "not a dict"})) == []
    assert list(activity.observations({"type": "assistant", "message": {"content": "text"}})) == []


def test_an_empty_text_block_is_not_an_observation():
    assert list(activity.observations(assistant(text("   ")))) == []


# ---- the live holder -------------------------------------------------------


def test_nothing_observed_yet_publishes_nothing():
    current = activity.CurrentActivity(now=lambda: 1000.0)
    assert current.snapshot() is None


def test_the_last_observed_event_is_what_is_published():
    current = activity.CurrentActivity(now=lambda: 1000.0)
    current.feed(
        [
            assistant(tool("mcp__mtg__wait_for_priority"), timestamp="2026-09-08T00:00:00.000Z"),
            assistant(tool("mcp__spellslinger__resolve_cards"), timestamp="2026-09-08T00:00:01Z"),
        ]
    )
    snapshot = current.snapshot()
    assert snapshot["state"] == activity.READING_CARDS
    assert snapshot["seq"] == 2


def test_the_age_comes_from_the_transcript_s_own_clock():
    """Seconds, computed here rather than downstream: the transcript's
    timestamps and this clock are the same host's, so no clock skew between
    the agent container and a browser can make a fresh state look stale."""
    written = epoch("2026-09-08T00:00:00Z")
    current = activity.CurrentActivity(now=lambda: written + 12.0)
    current.feed([assistant(tool("mcp__mtg__battlefield_state"), timestamp="2026-09-08T00:00:00Z")])
    assert current.snapshot()["seconds"] == pytest.approx(12.0, abs=0.5)


def test_a_line_with_no_usable_timestamp_is_aged_from_now():
    current = activity.CurrentActivity(now=lambda: 500.0)
    current.feed([assistant(tool("mcp__mtg__battlefield_state"))])
    assert current.snapshot()["seconds"] == pytest.approx(0.0, abs=0.01)


def test_a_clock_that_disagrees_never_publishes_a_negative_age():
    current = activity.CurrentActivity(now=lambda: epoch("2026-09-08T00:00:00Z"))
    current.feed([assistant(tool("mcp__mtg__battlefield_state"), timestamp="2026-09-08T00:01:00Z")])
    assert current.snapshot()["seconds"] == 0.0


def test_an_unmapped_event_leaves_the_previous_state_standing():
    """`redacted_thinking` (or any assistant block type this parser has not
    met) yields no observation at all - unlike a `tool_result` line, which
    now IS an observation (see
    `test_observations_yield_thinking_for_a_tool_result_line`)."""
    current = activity.CurrentActivity(now=lambda: 1000.0)
    current.feed([assistant(tool("mcp__mtg__wait_for_priority"))])
    current.feed([assistant({"type": "redacted_thinking", "data": "x"})])
    assert current.snapshot()["state"] == activity.WAITING


def test_the_watcher_publishes_what_it_tailed(tmp_path):
    """The real path: a transcript file on disk, tailed exactly the way the
    container tails it, read back off the watcher."""
    project = tmp_path / ".claude" / "projects" / "-workspace"
    project.mkdir(parents=True)
    session = project / "session-1.jsonl"
    with session.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(assistant(tool("mcp__mtg__wait_for_priority"))) + "\n")
    watcher = activity.ActivityWatcher(tmp_path, "tools")
    watcher.poll_once()
    assert watcher.activity_snapshot()["state"] == activity.WAITING
    with session.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(assistant(tool("mcp__spellslinger__knowledge_search"))) + "\n")
    watcher.poll_once()
    assert watcher.activity_snapshot()["state"] == activity.READING_RULES


def test_the_watcher_publishes_activity_even_with_the_log_turned_off(tmp_path):
    """`AGENT_LOG_LEVEL=off` is a log-volume choice, not a decision to leave
    the human opposite this seat staring at a static word - the same
    reasoning `InferenceTimer` is fed unconditionally for."""
    project = tmp_path / ".claude" / "projects" / "-workspace"
    project.mkdir(parents=True)
    (project / "session-1.jsonl").write_text(
        json.dumps(assistant(tool("mcp__mtg__cast", {"card": "Lightning Bolt"}))) + "\n"
    )
    watcher = activity.ActivityWatcher(tmp_path, "off")
    watcher.poll_once()
    assert watcher.activity_snapshot()["state"] == activity.ACTING


def test_no_transcript_at_all_publishes_nothing(tmp_path):
    watcher = activity.ActivityWatcher(tmp_path, "tools")
    watcher.poll_once()
    assert watcher.activity_snapshot() is None


# ---- the leak, pinned ------------------------------------------------------


def test_no_argument_card_name_or_prose_can_reach_the_published_state():
    """Requirement: the opposing human reads this label mid-game.

    Every channel the transcript offers is loaded here with something that
    would hand them the agent's read of the board - a card name in a tool
    input, a `reasoning` string, the agent's own prose - and the published
    snapshot is checked, as JSON, for every one of them.
    """
    secrets = [
        "Lightning Bolt",
        "Ragavan, Nimble Pilferer",
        "hold up the counterspell, he is on Frog",
        "Consign to Memory",
        "keeping this seven",
    ]
    current = activity.CurrentActivity(now=lambda: 1000.0)
    current.feed(
        [
            assistant(
                thinking(),
                text("hold up the counterspell, he is on Frog"),
                tool(
                    "mcp__mtg__cast",
                    {
                        "card": "Lightning Bolt",
                        "targets": "Ragavan, Nimble Pilferer",
                        "reasoning": "keeping this seven",
                    },
                ),
                tool("mcp__spellslinger__resolve_cards", {"names": ["Consign to Memory"]}),
            )
        ]
    )
    published = json.dumps(current.snapshot())
    for secret in secrets:
        assert secret not in published
    assert set(json.loads(published)) == {"state", "seconds", "seq"}
    assert json.loads(published)["state"] == activity.READING_CARDS


# ---- a rules question handed to a background subagent (SUB) ----------------
#
# The seat launches the `rules-researcher` subagent in the background while it
# waits, then goes straight back to `pass_until`. The launch has its own word,
# and nothing the subagent itself does may move the seat's published state:
# the seat is waiting, whatever its researcher is looking up.


def agent_launch(subagent_type: str) -> dict:
    return tool(
        "Agent",
        {
            "subagent_type": subagent_type,
            "description": "Can Consign to Memory counter a cast trigger?",
            "prompt": "He cast Ragavan, Nimble Pilferer; can I ...",
            "run_in_background": True,
        },
    )


def test_launching_the_rules_researcher_reads_as_researching_rules():
    line = assistant(agent_launch("rules-researcher"))
    [(kind, name)] = list(activity.observations(line))
    assert activity.state_for(kind, name) == activity.RESEARCHING_RULES == "researching_rules"


def test_the_legacy_task_tool_name_maps_the_same_way():
    line = assistant(tool("Task", {"subagent_type": "rules-researcher", "prompt": "x"}))
    [(kind, name)] = list(activity.observations(line))
    assert activity.state_for(kind, name) == activity.RESEARCHING_RULES


def test_any_other_subagent_is_plain_working():
    """A script-development subagent is not rules research; saying so would
    be a guess, and this mapping never guesses."""
    for payload in ({"subagent_type": "general-purpose", "prompt": "x"}, {"prompt": "x"}):
        [(kind, name)] = list(activity.observations(assistant(tool("Agent", payload))))
        assert activity.state_for(kind, name) == activity.WORKING


def test_the_launch_never_leaks_its_question():
    current = activity.CurrentActivity(now=lambda: 1000.0)
    current.feed([assistant(agent_launch("rules-researcher"))])
    published = json.dumps(current.snapshot())
    for secret in ("Consign to Memory", "Ragavan", "can I"):
        assert secret not in published
    assert json.loads(published)["state"] == activity.RESEARCHING_RULES


def test_a_subagent_s_own_sidechain_lines_are_not_the_seat_s_activity():
    line = assistant(tool("mcp__spellslinger__knowledge_search", {"q": "x"}))
    line["isSidechain"] = True
    assert list(activity.observations(line)) == []


def _write(path: Path, *lines: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def test_a_background_research_launch_during_a_wait_keeps_the_seat_waiting(tmp_path):
    """The whole sequence, tailed off disk the way the container tails it:
    the seat launches its researcher in the background, goes back to
    `pass_until`, and stays `waiting` while the researcher looks rules up -
    in the seat's own file as sidechain lines, or in its own subagent
    transcript (newer than the seat's) - and while the finished task's
    notification is queued as a prompt."""
    project = tmp_path / ".claude" / "projects" / "-workspace"
    project.mkdir(parents=True)
    session = project / "session-1.jsonl"
    watcher = activity.ActivityWatcher(tmp_path, "tools")

    _write(session, assistant(tool("mcp__mtg__pass_until", {"step": "end"})))
    watcher.poll_once()
    assert watcher.activity_snapshot()["state"] == activity.WAITING

    _write(session, assistant(agent_launch("rules-researcher")))
    watcher.poll_once()
    assert watcher.activity_snapshot()["state"] == activity.RESEARCHING_RULES

    _write(
        session,
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_R",
                        "content": "Async agent launched successfully.",
                    }
                ],
            },
        },
        assistant(tool("mcp__mtg__pass_until", {"step": "end"})),
    )
    watcher.poll_once()
    assert watcher.activity_snapshot()["state"] == activity.WAITING
    seq = watcher.activity_snapshot()["seq"]

    sidechain = assistant(tool("mcp__spellslinger__knowledge_search", {"q": "stack"}))
    sidechain["isSidechain"] = True
    _write(session, sidechain)
    subagents = project / "session-1" / "subagents"
    subagents.mkdir(parents=True)
    _write(
        subagents / "agent-a1.jsonl",
        assistant(tool("mcp__spellslinger__knowledge_search", {"q": "stack"})),
    )
    _write(
        session,
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": "<task-notification><tool-use-id>toolu_R</tool-use-id>"
                "<status>completed</status></task-notification>",
            },
        },
    )
    watcher.poll_once()
    snapshot = watcher.activity_snapshot()
    assert snapshot["state"] == activity.WAITING
    assert snapshot["seq"] == seq
