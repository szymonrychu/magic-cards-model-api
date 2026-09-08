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


# ---- full content, not truncated -------------------------------------------


def test_a_page_long_message_survives_intact(agent_log):
    """The whole point of this change: an operator debugging a decision needs
    the whole message, not a head with an elided tail."""
    words = "word " * 2000
    activity.log_lines([assistant(text(words))], activity.level_rank("tools"))
    (record,) = agent_log.records
    message = record.getMessage()
    assert "\n" not in message
    assert "chars]" not in message
    assert message == "says: " + " ".join(words.split())


def test_a_long_heredoc_of_a_command_survives_intact(agent_log):
    command = "mtg-seat report-problem --text " + "y" * 5000
    activity.log_lines(
        [assistant(tool("Bash", {"command": command}))], activity.level_rank("tools")
    )
    (record,) = agent_log.records
    assert record.getMessage() == f"calls Bash {command}"
    assert "chars]" not in record.getMessage()


def test_a_long_mcp_tool_call_survives_intact(agent_log):
    """A wide args dict with no headline field - the JSON-dump fallback -
    must not be cut either."""
    names = [f"Card Name {i}" for i in range(80)]
    activity.log_lines(
        [assistant(tool("mcp__spellslinger__resolve_cards", {"names": names}))],
        activity.level_rank("tools"),
    )
    (record,) = agent_log.records
    assert all(name in record.getMessage() for name in names)
    assert "chars]" not in record.getMessage()


def test_newlines_never_break_a_line_into_several(agent_log):
    activity.log_lines([assistant(text("first\nsecond\n\nthird"))], activity.level_rank("tools"))
    (record,) = agent_log.records
    assert "\n" not in record.getMessage()
    assert record.getMessage() == "says: first second third"


def test_a_multiline_heredoc_command_stays_one_physical_line(agent_log):
    """A real seat script: several lines of Python inside a Bash heredoc.
    The newlines are escaped (collapsed to spaces) rather than reproduced, so
    the event is still exactly one line for a line-oriented log reader - but
    every line of the script is still present, in order."""
    command = "python3 <<'EOF'\nprint('hi')\nprint('bye')\nEOF"
    activity.log_lines(
        [assistant(tool("Bash", {"command": command}))], activity.level_rank("tools")
    )
    (record,) = agent_log.records
    message = record.getMessage()
    assert "\n" not in message
    assert "print('hi')" in message
    assert "print('bye')" in message


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


def test_a_github_pat_used_as_a_git_clone_credential_is_redacted():
    """GH_PAT is in the agent's own environment (deploy/seat-job.yaml) for
    cloning the deck-guides repo. If the agent ever embeds the expanded value
    - not the `$GH_PAT` reference - in a command, it must not reach the log,
    now that arguments are logged in full rather than cut off before the
    credential's position in the string."""
    dirty = (
        "git clone https://x-access-token:ghp_abcdEFGH12345678901234567890123456"
        "@github.com/szymonrychu/mtg-decks.git /guides"
    )
    clean = activity.redact(dirty)
    assert "ghp_abcdEFGH" not in clean
    assert "<redacted>" in clean


def test_a_fine_grained_github_pat_is_redacted():
    dirty = (
        "token is github_pat_11ABCDEFG0123456789_"
        "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWX"
    )
    clean = activity.redact(dirty)
    assert "github_pat_11ABCDEFG" not in clean
    assert "<redacted>" in clean


def test_a_url_credential_is_redacted_even_without_the_word_token():
    """A basic-auth URL doesn't say `token=` or `secret:` anywhere, so the
    generic key/value pattern alone would miss it."""
    dirty = "curl https://user:s3cr3tvalue@example.com/path"
    clean = activity.redact(dirty)
    assert "s3cr3tvalue" not in clean
    assert "<redacted>" in clean


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


# ---- ccw_inference_seconds --------------------------------------------------
#
# One inference is one transcript `requestId`: claude flushes it as several
# content-block lines (a `thinking` line, then a `tool_use`/`text` line), each
# carrying the same final `usage.output_tokens`. Duration is measured between
# the JSONL's own `timestamp` fields - the point the model started responding
# (the line immediately before this requestId's first block) to the point the
# full response landed (this requestId's last block) - not wall-clock read
# time, so it is exact no matter how often the watcher polls.


def inference_block(ts: str, request_id: str, output_tokens: int, block_type: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "requestId": request_id,
        "message": {"content": [{"type": block_type}], "usage": {"output_tokens": output_tokens}},
    }


def user_boundary(ts: str) -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"content": [{"type": "tool_result", "content": "ok"}]},
    }


def test_one_inference_is_timed_from_the_prior_line_to_its_last_block():
    timer = activity.InferenceTimer(None)
    observed = []
    timer.metrics = type(
        "M", (), {"record_inference": lambda self, d, t: observed.append((d, t))}
    )()
    timer.feed(
        [
            user_boundary("2026-09-08T00:00:00.000Z"),
            inference_block("2026-09-08T00:00:00.500Z", "r1", 200, "thinking"),
            inference_block("2026-09-08T00:00:02.000Z", "r1", 200, "tool_use"),
            user_boundary("2026-09-08T00:00:02.100Z"),
        ]
    )
    assert observed == [(2.0, 200)]


def test_consecutive_inferences_are_timed_independently():
    timer = activity.InferenceTimer(None)
    observed = []
    timer.metrics = type(
        "M", (), {"record_inference": lambda self, d, t: observed.append((d, t))}
    )()
    timer.feed(
        [
            user_boundary("2026-09-08T00:00:00.000Z"),
            inference_block("2026-09-08T00:00:00.500Z", "r1", 200, "thinking"),
            inference_block("2026-09-08T00:00:02.000Z", "r1", 200, "tool_use"),
            user_boundary("2026-09-08T00:00:02.100Z"),
            inference_block("2026-09-08T00:00:02.600Z", "r2", 700, "thinking"),
            inference_block("2026-09-08T00:00:10.000Z", "r2", 700, "text"),
            user_boundary("2026-09-08T00:00:10.050Z"),
        ]
    )
    assert len(observed) == 2
    (d1, t1), (d2, t2) = observed
    assert d1 == pytest.approx(2.0)
    assert t1 == 200
    # r2's start boundary is the prior user line (00:00:02.100), not r1's own
    # last block - each group's start comes from what came right before IT.
    assert d2 == pytest.approx(7.9)
    assert t2 == 700


def test_the_final_in_flight_inference_is_not_recorded_without_a_following_line():
    """A streaming design cannot know an inference has ended until it sees
    what comes after it - the last inference before the transcript stops
    growing entirely (match end) is a documented gap, not a bug."""
    timer = activity.InferenceTimer(None)
    observed = []
    timer.metrics = type(
        "M", (), {"record_inference": lambda self, d, t: observed.append((d, t))}
    )()
    timer.feed(
        [
            user_boundary("2026-09-08T00:00:00.000Z"),
            inference_block("2026-09-08T00:00:00.500Z", "r1", 200, "thinking"),
            inference_block("2026-09-08T00:00:02.000Z", "r1", 200, "tool_use"),
        ]
    )
    assert observed == []


def test_a_missing_or_unparsable_timestamp_is_skipped_not_crashed():
    timer = activity.InferenceTimer(None)
    observed = []
    timer.metrics = type(
        "M", (), {"record_inference": lambda self, d, t: observed.append((d, t))}
    )()
    timer.feed(
        [
            user_boundary("not a timestamp"),
            inference_block("2026-09-08T00:00:00.500Z", "r1", 200, "thinking"),
            {
                "type": "assistant",
                "requestId": "r1",
                "message": {"content": [{"type": "tool_use"}]},
            },
            user_boundary("2026-09-08T00:00:02.100Z"),
        ]
    )
    # No usable start boundary reached this group - nothing observed, and
    # nothing raised.
    assert observed == []


def test_poll_once_feeds_the_watcher_s_own_metrics_regardless_of_log_level(home):
    """Metrics must not depend on `AGENT_LOG_LEVEL` - a production wrapper
    turned down to `off`/`turns` for log volume still wants latency data."""
    from mtg_agent.wrapper.metrics import Metrics

    root, path = home
    metrics = Metrics()
    watcher = activity.ActivityWatcher(root, "off", poll=0.01, metrics=metrics)
    write(
        path,
        [
            user_boundary("2026-09-08T00:00:00.000Z"),
            inference_block("2026-09-08T00:00:00.500Z", "r1", 200, "thinking"),
            inference_block("2026-09-08T00:00:02.000Z", "r1", 200, "tool_use"),
            user_boundary("2026-09-08T00:00:02.100Z"),
        ],
    )
    watcher.poll_once()
    body, _ = metrics.render()
    assert 'ccw_inference_seconds_count{output_tokens_bucket="200-500"} 1.0' in body.decode()
