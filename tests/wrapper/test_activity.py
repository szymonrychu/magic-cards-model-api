"""What the agent container prints while a turn is running.

Before this, `docker compose logs` showed "turn submitted" and then nothing
for half an hour. These tests write real claude transcript lines - the shape
`transcript.py` already parses, taken from a live session file - into a real
file on disk, tail it the way the container does, and read what came out.
"""

from __future__ import annotations

import json
import logging

import pytest

from mtg_agent.wrapper import activity


@pytest.fixture
def agent_log(caplog):
    caplog.set_level(logging.INFO, logger="agent")
    return caplog


def assistant(*blocks) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def text(body: str) -> dict:
    return {"type": "text", "text": body}


def tool(name: str, payload: dict) -> dict:
    return {"type": "tool_use", "name": name, "input": payload}


def tool_result(body, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": body, "is_error": is_error}],
        },
    }


def write(path, lines) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


@pytest.fixture
def home(tmp_path):
    project = tmp_path / ".claude" / "projects" / "-workspace"
    project.mkdir(parents=True)
    return tmp_path, project / "session-1.jsonl"


# ---- the events ------------------------------------------------------------


def test_the_agent_s_own_words_reach_the_log(agent_log):
    activity.log_lines(
        [assistant(text("Casting Bolt at the Ragavan."))], activity.level_rank("tools")
    )
    assert "says: Casting Bolt at the Ragavan." in agent_log.text


def test_every_tool_call_is_one_readable_line(agent_log):
    activity.log_lines(
        [
            assistant(
                tool("Bash", {"command": "mtg-seat cast 'Lightning Bolt' --target bob"}),
                tool("mcp__mtg__play_land", {"card": "Mountain", "uid": "abc"}),
            )
        ],
        activity.level_rank("tools"),
    )
    lines = agent_log.text.splitlines()
    assert any("calls Bash mtg-seat cast 'Lightning Bolt' --target bob" in line for line in lines)
    assert any(
        'calls mcp__mtg__play_land {"card": "Mountain", "uid": "abc"}' in line for line in lines
    )


def test_a_thinking_block_is_not_logged(agent_log):
    """The agent talking to itself, at length. The text blocks say the same
    thing in a form written to be read."""
    logged = activity.log_lines(
        [assistant({"type": "thinking", "thinking": "x" * 4000, "signature": "s"})],
        activity.level_rank("tools"),
    )
    assert logged == 0
    assert agent_log.text.strip() == ""


def test_a_failing_tool_is_a_warning_and_a_succeeding_one_is_silent(agent_log):
    activity.log_lines(
        [
            tool_result("the whole board state, several kilobytes of it"),
            tool_result("Exit code 1\nit is not your priority", is_error=True),
        ],
        activity.level_rank("tools"),
    )
    warnings = [r for r in agent_log.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, agent_log.text
    assert "it is not your priority" in warnings[0].getMessage()
    assert "several kilobytes" not in agent_log.text


def test_a_tool_result_that_is_a_block_list_still_reads(agent_log):
    activity.log_lines(
        [tool_result([{"type": "text", "text": "no such card in hand"}], is_error=True)],
        activity.level_rank("tools"),
    )
    assert "no such card in hand" in agent_log.text


# ---- truncation ------------------------------------------------------------


def test_a_page_long_message_is_cut_to_a_line(agent_log):
    activity.log_lines([assistant(text("word " * 2000))], activity.level_rank("tools"))
    (record,) = agent_log.records
    message = record.getMessage()
    assert "\n" not in message
    assert len(message) < activity.SAY_CHARS + 60
    assert "chars]" in message


def test_a_heredoc_of_a_command_is_cut_but_keeps_its_head(agent_log):
    command = "mtg-seat report-problem --text " + "y" * 5000
    activity.log_lines(
        [assistant(tool("Bash", {"command": command}))], activity.level_rank("tools")
    )
    (record,) = agent_log.records
    assert "mtg-seat report-problem" in record.getMessage()
    assert len(record.getMessage()) < activity.TOOL_CHARS + 60


def test_newlines_never_break_a_line_into_several(agent_log):
    activity.log_lines([assistant(text("first\nsecond\n\nthird"))], activity.level_rank("tools"))
    (record,) = agent_log.records
    assert "\n" not in record.getMessage()
    assert record.getMessage() == "says: first second third"


# ---- secrets ---------------------------------------------------------------


def test_an_api_key_never_reaches_the_log():
    """The agent can `env | grep` its own container. The uid stays - it is in
    every URL in this log already and says which seat is talking."""
    dirty = (
        "ANTHROPIC_API_KEY=sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZ uid=6770b15700034d15 "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdef"
    )
    clean = activity.redact(dirty)
    assert "sk-ant-api03-ZZZZ" not in clean
    assert "eyJhbGciOiJIUzI1NiJ9" not in clean
    assert "<redacted>" in clean
    assert "6770b15700034d15" in clean, "a seat uid is not a secret here"


def test_a_secret_inside_a_tool_call_is_redacted(agent_log):
    activity.log_lines(
        [assistant(tool("Bash", {"command": "curl -H 'X-Wrapper-Secret: hunter2hunter2' url"}))],
        activity.level_rank("tools"),
    )
    assert "hunter2hunter2" not in agent_log.text
    assert "<redacted>" in agent_log.text


# ---- verbosity -------------------------------------------------------------


def test_the_level_turns_the_pieces_off_one_at_a_time(agent_log):
    lines = [assistant(text("thinking out loud"), tool("Bash", {"command": "mtg-seat state"}))]

    activity.log_lines(lines, activity.level_rank("tools"))
    assert "says:" in agent_log.text and "calls Bash" in agent_log.text

    agent_log.clear()
    activity.log_lines(lines, activity.level_rank("messages"))
    assert "says:" in agent_log.text and "calls Bash" not in agent_log.text

    agent_log.clear()
    activity.log_lines(lines, activity.level_rank("turns"))
    assert agent_log.text.strip() == ""

    agent_log.clear()
    activity.log_lines(lines, activity.level_rank("off"))
    assert agent_log.text.strip() == ""


def test_a_typo_in_the_env_var_does_not_silence_the_log():
    assert activity.level_rank("tolos") == activity.level_rank("tools")
    assert activity.level_rank("") == activity.level_rank("tools")
    assert activity.level_rank("OFF") == activity.level_rank("off")


# ---- following a live file -------------------------------------------------


def test_the_watcher_finds_the_transcript_and_follows_it(home, agent_log):
    root, path = home
    watcher = activity.ActivityWatcher(root, "tools", poll=0.01)

    assert watcher.poll_once() == 0, "no file yet is not an error"

    write(path, [assistant(text("Keeping this seven."))])
    assert watcher.poll_once() == 1
    assert "Keeping this seven." in agent_log.text

    agent_log.clear()
    assert watcher.poll_once() == 0, "nothing new is logged twice"

    write(path, [assistant(tool("Bash", {"command": "mtg-seat keep"}))])
    assert watcher.poll_once() == 1
    assert "calls Bash mtg-seat keep" in agent_log.text


def test_a_half_written_line_is_held_until_its_newline_arrives(home, agent_log):
    """claude appends whole objects, but a read can still land mid-write."""
    root, path = home
    watcher = activity.ActivityWatcher(root, "tools", poll=0.01)
    blob = json.dumps(assistant(text("half a thought")))
    path.write_text(blob[:20])
    assert watcher.poll_once() == 0
    assert agent_log.text.strip() == ""

    with path.open("a", encoding="utf-8") as handle:
        handle.write(blob[20:] + "\n")
    assert watcher.poll_once() == 1
    assert "half a thought" in agent_log.text


def test_a_corrupt_line_is_skipped_rather_than_stopping_the_tail(home, agent_log):
    root, path = home
    watcher = activity.ActivityWatcher(root, "tools", poll=0.01)
    path.write_text("{not json at all\n" + json.dumps(assistant(text("still here"))) + "\n")
    assert watcher.poll_once() == 1
    assert "still here" in agent_log.text


def test_a_newer_session_file_takes_over(home, agent_log):
    root, path = home
    watcher = activity.ActivityWatcher(root, "tools", poll=0.01)
    write(path, [assistant(text("first session"))])
    watcher.poll_once()

    later = path.parent / "session-2.jsonl"
    write(later, [assistant(text("second session"))])
    import os

    os.utime(later, (path.stat().st_mtime + 10, path.stat().st_mtime + 10))
    agent_log.clear()
    assert watcher.poll_once() == 1
    assert "second session" in agent_log.text


def test_the_watcher_thread_does_nothing_when_the_level_is_off(home):
    root, path = home
    watcher = activity.ActivityWatcher(root, "off", poll=0.01)
    assert not watcher.enabled()
    watcher.start()
    watcher.stop()


def test_a_truncated_file_is_re_read_from_the_start(home, agent_log):
    """A shorter file cannot be tailed from a stale offset without decoding
    the middle of a line."""
    root, path = home
    watcher = activity.ActivityWatcher(root, "tools", poll=0.01)
    write(path, [assistant(text("a" * 200))])
    watcher.poll_once()
    agent_log.clear()
    path.write_text(json.dumps(assistant(text("rewritten"))) + "\n")
    assert watcher.poll_once() == 1
    assert "rewritten" in agent_log.text
