"""A live `future-*` helper is not an open turn; a live `current-*` is.

The seat's orchestrator drives a `current-N` and a `future-N` `seat-player`.
The wrapper's Stop hook lists the background work still running
(`pendingTasks`) and `Manager.complete` keeps the turn open while it is not
empty. A future that idles for the whole game must not hold the turn; a
current (a decision in flight) must, on both channels. Any other background
task (rules-researcher, Bash) keeps its old behaviour.

The transcripts are T1's recorded claude 2.1.282 runs (`fixtures/seat_pair`).
They name their helpers `alpha` and `beta`, so each test renames them to
`future-1` / `current-1` in a copy, and cuts the lead's transcript at the
points where its Stop hook fired.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from mtg_agent.wrapper import stop_hook
from mtg_agent.wrapper.transcript import pending_background_tasks
from tests.wrapper.test_background_tasks import (
    HANDOVER,
    assistant_tool,
    notification,
    prompt,
    tool_result,
    write,
)

FIXTURES = Path(__file__).parent / "fixtures" / "seat_pair"

#: Row index of each Stop hook (`hook_success` attachment) in the recorded lead
#: transcripts, in order.
SUBAGENT_STOPS = [35, 67, 78, 101]
TEAM_STOPS = [35, 39, 44, 61, 71, 76]


def variant(tmp_path: Path, channel: str, names: dict[str, str]) -> list[str]:
    """The lead transcript lines of a recorded channel, with `alpha`/`beta`
    renamed per `names` everywhere (rows and the helpers' meta files)."""
    root = tmp_path / channel
    shutil.copytree(FIXTURES / channel, root)
    for meta in root.rglob("*.meta.json"):
        meta.write_text(_rename(meta.read_text(), names))
    lead = next((root / "-workspace").glob("*.jsonl"))
    return [_rename(line, names) for line in lead.read_text().splitlines()]


def _rename(text: str, names: dict[str, str]) -> str:
    for old, new in names.items():
        text = re.sub(rf"\b{old}\b", new, text)
    return text


def stops_at(tmp_path: Path, lines: list[str], index: int) -> Path:
    """The transcript as the Stop hook found it at row `index`."""
    path = tmp_path / f"cut-{index}.jsonl"
    path.write_text("\n".join(lines[: index + 1]) + "\n")
    return path


def pending_at(tmp_path: Path, lines: list[str], stops: list[int]) -> list[int]:
    return [len(pending_background_tasks(stops_at(tmp_path, lines, i))) for i in stops]


FUTURE_ALPHA = {"alpha": "future-1", "beta": "current-1"}
CURRENT_ALPHA = {"alpha": "current-1", "beta": "future-1"}


# ---- subagents channel --------------------------------------------------------------------


def test_subagents_only_the_current_holds_the_turn(tmp_path):
    lines = variant(tmp_path, "subagents", FUTURE_ALPHA)
    # 35: both launched, future alpha not counted.
    # 67: beta (current) resumed by SendMessage - the old code lost it (0).
    # 78: beta reported, alpha (future) resumed.
    # 101: beta resumed, then stopped.
    assert pending_at(tmp_path, lines, SUBAGENT_STOPS) == [1, 1, 0, 0]


def test_subagents_mirrored_roles_flip_the_answer(tmp_path):
    lines = variant(tmp_path, "subagents", CURRENT_ALPHA)
    assert pending_at(tmp_path, lines, SUBAGENT_STOPS) == [1, 0, 1, 0]


def test_subagents_other_background_agents_keep_holding_the_turn(tmp_path):
    lines = variant(tmp_path, "subagents", {})
    assert pending_at(tmp_path, lines, SUBAGENT_STOPS) == [2, 1, 1, 0]


def test_a_subagent_resumed_by_sendmessage_is_tracked_until_it_reports(tmp_path):
    lines = variant(tmp_path, "subagents", FUTURE_ALPHA)
    ids = pending_background_tasks(stops_at(tmp_path, lines, 67))
    assert ids == ["toolu_01JcZJYqhhmNdihTG28HzbgF"]  # the resuming SendMessage


def test_a_launch_whose_result_is_not_on_disk_yet_still_counts(tmp_path):
    """One recorded run: the hook read the transcript while the Agent tool
    calls were still unanswered (`stop_reason=tool_use`) and deferred nothing."""
    lines = variant(tmp_path, "subagents", {"alpha": "current-1", "beta": "future-1"})
    assert len(pending_background_tasks(stops_at(tmp_path, lines, 23))) == 1
    assert len(pending_background_tasks(stops_at(tmp_path, lines, 25))) == 1
    future_only = variant(tmp_path / "f", "subagents", {"alpha": "future-1", "beta": "future-2"})
    assert pending_background_tasks(stops_at(tmp_path, future_only, 25)) == []


# ---- teams channel ------------------------------------------------------------------------


def test_teams_the_live_current_holds_the_turn_until_it_is_shut_down(tmp_path):
    lines = variant(tmp_path, "teams", FUTURE_ALPHA)
    # Only beta (current) counts through every Stop until the lead's
    # shutdown_request rows (65-68); at 71 both are gone.
    assert pending_at(tmp_path, lines, TEAM_STOPS) == [1, 1, 1, 1, 0, 0]


def test_teams_mirrored_roles(tmp_path):
    lines = variant(tmp_path, "teams", CURRENT_ALPHA)
    assert pending_at(tmp_path, lines, TEAM_STOPS) == [1, 1, 1, 1, 0, 0]


def test_teams_other_teammates_keep_holding_the_turn(tmp_path):
    lines = variant(tmp_path, "teams", {})
    assert pending_at(tmp_path, lines, TEAM_STOPS) == [2, 2, 2, 2, 0, 0]


def test_teams_two_futures_never_hold_the_turn(tmp_path):
    lines = variant(tmp_path, "teams", {"alpha": "future-1", "beta": "future-2"})
    assert pending_at(tmp_path, lines, TEAM_STOPS) == [0] * 6


def test_teams_the_idle_current_is_still_alive(tmp_path):
    """Both teammates have sent idle_notification by row 44; an idle teammate
    is alive and takes the next SendMessage."""
    lines = variant(tmp_path, "teams", FUTURE_ALPHA)
    assert len(pending_background_tasks(stops_at(tmp_path, lines, 44))) == 1


# ---- ending a helper however it ends -------------------------------------------------------


def use(tool_id: str, tool: str, tool_input: dict) -> dict:
    """`assistant_tool` with a tool input that itself has a `name` key."""
    line = assistant_tool(tool_id, tool)
    line["message"]["content"][0]["input"] = tool_input
    return line


def spawn(tool_id: str, name: str) -> list[dict]:
    return [
        use(tool_id, "Agent", {"name": name, "description": f"teammate {name}", "prompt": "x"}),
        tool_result(
            tool_id,
            "spawned",
            {
                "status": "teammate_spawned",
                "agent_id": f"{name}@session-abc",
                "name": name,
                "team_name": "session-abc",
            },
        ),
    ]


def test_a_teammate_stopped_by_taskstop_is_finished(tmp_path):
    path = write(
        tmp_path / "t.jsonl",
        [
            prompt(HANDOVER),
            *spawn("toolu_C", "current-1"),
            *spawn("toolu_F", "future-1"),
            assistant_tool("toolu_S", "TaskStop", task_id="current-1"),
            tool_result(
                "toolu_S",
                "stopped",
                {"message": "Successfully stopped task", "task_type": "in_process_teammate"},
            ),
        ],
    )
    assert pending_background_tasks(path) == []


def test_a_teammate_that_shut_down_is_finished(tmp_path):
    spawned = [prompt(HANDOVER), *spawn("toolu_C", "current-1")]
    path = write(tmp_path / "a.jsonl", spawned)
    assert pending_background_tasks(path) == ["toolu_C"]
    terminated = (
        'Another Claude session sent a message:\n<teammate-message teammate_id="system">\n'
        '{"type":"teammate_terminated","message":"current-1 has shut down."}\n'
        "</teammate-message>"
    )
    write(tmp_path / "b.jsonl", [*spawned, prompt(terminated)])
    assert pending_background_tasks(tmp_path / "b.jsonl") == []


def test_a_shutdown_request_finishes_the_teammate_but_a_failed_one_does_not(tmp_path):
    def ask(result: dict) -> list[dict]:
        return [
            prompt(HANDOVER),
            *spawn("toolu_C", "current-1"),
            assistant_tool(
                "toolu_R",
                "SendMessage",
                to="current-1",
                type="shutdown_request",
                message={"type": "shutdown_request"},
            ),
            tool_result("toolu_R", "ok", result),
        ]

    ok = write(tmp_path / "ok.jsonl", ask({"success": True, "target": "current-1"}))
    bad = write(tmp_path / "bad.jsonl", ask({"success": False, "message": "no such agent"}))
    assert pending_background_tasks(ok) == []
    assert pending_background_tasks(bad) == ["toolu_C"]


def test_a_message_to_a_teammate_does_not_end_it(tmp_path):
    path = write(
        tmp_path / "t.jsonl",
        [
            prompt(HANDOVER),
            *spawn("toolu_C", "current-1"),
            assistant_tool("toolu_M", "SendMessage", to="current-1", message="your turn"),
            tool_result("toolu_M", "sent", {"success": True, "message": "Message sent"}),
        ],
    )
    assert pending_background_tasks(path) == ["toolu_C"]


def test_a_killed_subagent_notification_finishes_it(tmp_path):
    path = write(
        tmp_path / "t.jsonl",
        [
            prompt(HANDOVER),
            use("toolu_A", "Agent", {"name": "current-1", "run_in_background": True}),
            tool_result(
                "toolu_A",
                "Async agent launched successfully.\nagentId: a9",
                {"isAsync": True, "status": "async_launched", "agentId": "a9"},
            ),
            prompt(notification("toolu_A", "a9", "killed")),
        ],
    )
    assert pending_background_tasks(path) == []


# ---- the Stop hook and the turn ------------------------------------------------------------


@pytest.mark.parametrize(("channel", "stop"), [("subagents", 35), ("teams", 35)])
def test_the_stop_hook_lists_only_open_turn_work(tmp_path, channel, stop):
    lines = variant(tmp_path, channel, FUTURE_ALPHA)
    path = stops_at(tmp_path, lines, stop)
    payload = json.dumps({"session_id": "s", "transcript_path": str(path)}).encode()
    result = stop_hook.build_result(payload, tmp_path / "absent.json")
    assert len(result["pendingTasks"]) == 1


@pytest.mark.parametrize("channel", ["subagents", "teams"])
def test_a_stop_with_only_a_future_live_completes_the_turn(monkeypatch, tmp_path, channel):
    from mtg_agent.wrapper.turns import TurnState
    from tests.wrapper.test_session import FakeProc, ready_manager

    manager = ready_manager(monkeypatch, FakeProc(b"ready"))
    try:
        turn_id = manager.submit("play")
        lines = variant(tmp_path, channel, {"alpha": "future-1", "beta": "future-2"})
        path = stops_at(tmp_path, lines, 35)
        payload = json.dumps({"session_id": "s", "transcript_path": str(path)}).encode()
        result = stop_hook.build_result(payload, tmp_path / "absent.json")
        result["turnId"] = turn_id
        manager.complete(result)
        assert manager.store.get(turn_id).state is TurnState.COMPLETE
    finally:
        manager.shutdown()
