"""A turn is not over while the subagents and background tasks it started run.

Live, game 8a55cab7: a seat's handover turn launched a script subagent,
claude ran its Stop hook the moment the main turn stopped, the wrapper
completed the turn, the driver wrote `handover.done`, and the pod could exit
with the subagent's work lost. Claude Code runs a background subagent or
command outside the turn: it answers the launching tool call at once ("Async
agent launched successfully", "Command running in background with ID: ..."),
and when the task ends it delivers a `<task-notification>` as a new prompt,
which claude answers and ends with a Stop hook of its own. So the transcript
says which launches have not reported back, and the Stop hook carries that
as `pendingTasks`; `Manager.complete` keeps the turn in flight until a Stop
hook reports none.

The transcript lines are shaped like the real ones claude writes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mtg_agent.wrapper import stop_hook
from mtg_agent.wrapper.session import State
from mtg_agent.wrapper.transcript import pending_background_tasks, turn_marker
from mtg_agent.wrapper.turns import TurnState
from tests.wrapper.test_session import FakeProc, ready_manager


def prompt(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant_tool(tool_id: str, name: str, **tool_input) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
            "usage": {},
            "stop_reason": "tool_use",
        },
    }


def assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": text}],
            "usage": {"output_tokens": 1},
            "stop_reason": "end_turn",
        },
    }


def tool_result(tool_id: str, text: str, tool_use_result: dict | None = None) -> dict:
    line = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": tool_id,
                    "type": "tool_result",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        },
    }
    if tool_use_result is not None:
        line["toolUseResult"] = tool_use_result
    return line


def agent_launch(tool_id: str, agent_id: str) -> list[dict]:
    return [
        assistant_tool(tool_id, "Agent", description="develop a fetch script", prompt="..."),
        tool_result(
            tool_id,
            f"Async agent launched successfully.\nagentId: {agent_id} (internal ID)\n"
            "The agent is working in the background. You will be notified automatically "
            "when it completes.",
            {"isAsync": True, "status": "async_launched", "agentId": agent_id},
        ),
    ]


def bash_launch(tool_id: str, task_id: str) -> list[dict]:
    return [
        assistant_tool(tool_id, "Bash", command="sleep 60", run_in_background=True),
        tool_result(
            tool_id,
            f"Command running in background with ID: {task_id}. Output is being written to: "
            f"/tmp/tasks/{task_id}.output. You will be notified when it completes.",
            {"stdout": "", "stderr": "", "interrupted": False, "backgroundTaskId": task_id},
        ),
    ]


def notification(tool_id: str, task_id: str, status: str = "completed") -> str:
    return (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n"
        f"<tool-use-id>{tool_id}</tool-use-id>\n<status>{status}</status>\n"
        f'<summary>Agent "develop a fetch script" finished</summary>\n'
        "<result>admitted fetch-untapped-shock</result>\n</task-notification>"
    )


def queued(text: str) -> dict:
    return {
        "type": "attachment",
        "attachment": {
            "type": "queued_command",
            "prompt": text,
            "commandMode": "task-notification",
        },
    }


def write(path: Path, lines: list[dict]) -> Path:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def append(path: Path, lines: list[dict]) -> None:
    with path.open("a") as handle:
        handle.writelines(json.dumps(line) + "\n" for line in lines)


HANDOVER = f"Hand over now.\n\n{turn_marker('turn-ho')}"


# ---- the transcript ----------------------------------------------------------------------


def test_a_launched_subagent_is_pending_until_its_notification_is_taken_in(tmp_path):
    path = write(
        tmp_path / "t.jsonl",
        [prompt(HANDOVER), *agent_launch("toolu_A", "a1"), assistant_text("launched")],
    )
    assert pending_background_tasks(path) == ["toolu_A"]
    append(path, [prompt(notification("toolu_A", "a1")), assistant_text("admitted")])
    assert pending_background_tasks(path) == []


def test_a_background_command_is_pending_too(tmp_path):
    path = write(tmp_path / "t.jsonl", [prompt(HANDOVER), *bash_launch("toolu_B", "b1")])
    assert pending_background_tasks(path) == ["toolu_B"]


def test_a_notification_absorbed_mid_turn_as_a_queued_command_counts(tmp_path):
    path = write(
        tmp_path / "t.jsonl",
        [prompt(HANDOVER), *agent_launch("toolu_A", "a1"), queued(notification("toolu_A", "a1"))],
    )
    assert pending_background_tasks(path) == []


def test_failed_and_killed_tasks_have_reported_back(tmp_path):
    path = write(
        tmp_path / "t.jsonl",
        [
            prompt(HANDOVER),
            *agent_launch("toolu_A", "a1"),
            *bash_launch("toolu_B", "b1"),
            prompt(notification("toolu_A", "a1", "failed")),
            prompt(notification("toolu_B", "b1", "killed")),
        ],
    )
    assert pending_background_tasks(path) == []


def test_a_running_status_update_is_not_a_finish(tmp_path):
    path = write(
        tmp_path / "t.jsonl",
        [
            prompt(HANDOVER),
            *agent_launch("toolu_A", "a1"),
            prompt(notification("toolu_A", "a1", "running")),
        ],
    )
    assert pending_background_tasks(path) == ["toolu_A"]


def test_a_task_stopped_or_collected_through_a_tool_has_reported_back(tmp_path):
    """TaskStop and a blocking TaskOutput that got the result often get no
    notification afterwards, so their own results finish the task."""
    path = write(
        tmp_path / "t.jsonl",
        [
            prompt(HANDOVER),
            *agent_launch("toolu_A", "a1"),
            *bash_launch("toolu_B", "b1"),
            assistant_tool("toolu_S", "TaskStop", task_id="a1"),
            tool_result(
                "toolu_S",
                "Successfully stopped task: a1",
                {"message": "Successfully stopped task: a1", "task_id": "a1"},
            ),
            assistant_tool("toolu_O", "TaskOutput", task_id="b1", block=True, timeout=60000),
            tool_result(
                "toolu_O",
                "<status>completed</status>",
                {"retrieval_status": "success", "task": {"task_id": "b1", "status": "completed"}},
            ),
        ],
    )
    assert pending_background_tasks(path) == []


def test_a_task_output_that_is_not_ready_leaves_the_task_pending(tmp_path):
    path = write(
        tmp_path / "t.jsonl",
        [
            prompt(HANDOVER),
            *bash_launch("toolu_B", "b1"),
            assistant_tool("toolu_O", "TaskOutput", task_id="b1", block=False, timeout=5000),
            tool_result(
                "toolu_O",
                "not ready",
                {"retrieval_status": "not_ready", "task": {"task_id": "b1", "status": "running"}},
            ),
        ],
    )
    assert pending_background_tasks(path) == ["toolu_B"]


def test_a_launch_before_the_last_turn_marker_belongs_to_an_earlier_turn(tmp_path):
    path = write(
        tmp_path / "t.jsonl",
        [
            prompt(f"play\n\n{turn_marker('turn-0')}"),
            *bash_launch("toolu_OLD", "old"),
            prompt(HANDOVER),
            *agent_launch("toolu_A", "a1"),
        ],
    )
    assert pending_background_tasks(path) == ["toolu_A"]


def test_a_foreground_tool_and_a_subagent_sidechain_are_not_launches(tmp_path):
    sidechain = agent_launch("toolu_SIDE", "s1")
    for line in sidechain:
        line["isSidechain"] = True
    path = write(
        tmp_path / "t.jsonl",
        [
            prompt(HANDOVER),
            assistant_tool("toolu_F", "Bash", command="ls"),
            tool_result(
                "toolu_F", "decks", {"stdout": "decks", "stderr": "", "interrupted": False}
            ),
            *sidechain,
        ],
    )
    assert pending_background_tasks(path) == []


def test_no_transcript_means_nothing_pending(tmp_path):
    assert pending_background_tasks(tmp_path / "absent.jsonl") == []


# ---- the Stop hook ---------------------------------------------------------------------------


def hook_payload(path: Path) -> bytes:
    return json.dumps(
        {"session_id": "s", "transcript_path": str(path), "hook_event_name": "Stop"}
    ).encode()


def test_the_stop_hook_reports_the_pending_tasks(tmp_path):
    path = write(tmp_path / "t.jsonl", [prompt(HANDOVER), *agent_launch("toolu_A", "a1")])
    result = stop_hook.build_result(hook_payload(path), tmp_path / "absent.json")
    assert result["pendingTasks"] == ["toolu_A"]
    assert result["turnId"] == "turn-ho"


def test_the_stop_hook_reports_none_without_a_transcript(tmp_path):
    result = stop_hook.build_result(b"{}", tmp_path / "absent.json")
    assert result["pendingTasks"] == []


# ---- the turn ----------------------------------------------------------------------------------


def test_a_stop_with_pending_tasks_keeps_the_turn_in_flight_until_they_report(
    monkeypatch, tmp_path, caplog
):
    caplog.set_level(logging.INFO, logger="wrapper.session")
    proc = FakeProc(b"ready")
    delivered: list = []
    manager = ready_manager(monkeypatch, proc)
    manager.on_turn_done = delivered.append
    try:
        turn_id = manager.submit("hand over now")
        path = tmp_path / "t.jsonl"
        write(
            path,
            [prompt(f"hand over now\n\n{turn_marker(turn_id)}"), *agent_launch("toolu_A", "a1")],
        )
        append(path, [assistant_text("a subagent is developing the script")])

        # The main turn's first Stop hook: the subagent is still running.
        manager.complete(stop_hook.build_result(hook_payload(path), tmp_path / "absent.json"))
        assert manager.current_turn_id == turn_id
        assert manager.state is State.BUSY
        assert manager.store.get(turn_id).state is TurnState.RUNNING
        assert delivered == []
        with __import__("pytest").raises(Exception, match="busy"):
            manager.submit("another turn")

        # The subagent reports; claude answers the notification and stops again.
        append(
            path, [prompt(notification("toolu_A", "a1")), assistant_text("admitted, handed over")]
        )
        manager.complete(stop_hook.build_result(hook_payload(path), tmp_path / "absent.json"))
        record = manager.store.get(turn_id)
        assert record.state is TurnState.COMPLETE
        assert [r.id for r in delivered] == [turn_id]
        assert manager.state is State.READY

        messages = [r.getMessage() for r in caplog.records]
        deferred = next(i for i, m in enumerate(messages) if "turn stop deferred" in m)
        finished = next(i for i, m in enumerate(messages) if "background tasks finished" in m)
        complete = next(i for i, m in enumerate(messages) if m.startswith("turn complete"))
        assert deferred < finished < complete
        assert "toolu_A" in messages[deferred] and "toolu_A" in messages[finished]
    finally:
        manager.shutdown()


def test_a_stop_with_nothing_pending_completes_at_once(monkeypatch, tmp_path):
    proc = FakeProc(b"ready")
    manager = ready_manager(monkeypatch, proc)
    try:
        turn_id = manager.submit("go")
        manager.complete({"turnId": turn_id, "finalText": "done", "pendingTasks": []})
        assert manager.store.get(turn_id).state is TurnState.COMPLETE
    finally:
        manager.shutdown()
