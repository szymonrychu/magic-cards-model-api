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
        ("mcp__mtg__cast", activity.ACTING),
        ("mcp__mtg__play_land", activity.ACTING),
        ("mcp__mtg__activate", activity.ACTING),
        ("mcp__mtg__pass", activity.ACTING),
        ("mcp__mtg__pass_until", activity.ACTING),
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


def test_observations_ignore_a_user_line_entirely():
    result = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
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
    current = activity.CurrentActivity(now=lambda: 1000.0)
    current.feed([assistant(tool("mcp__mtg__wait_for_priority"))])
    current.feed(
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "x", "is_error": True}],
                },
            }
        ]
    )
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
