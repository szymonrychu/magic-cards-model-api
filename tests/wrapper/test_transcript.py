import json
from pathlib import Path

from mtg_agent.wrapper.transcript import last_assistant, sum_turn_tokens


def write_jsonl(path: Path, lines: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def assistant(text: str, *, model: str = "claude-sonnet", stop: str = "end_turn", **usage):
    full = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    } | usage
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": text}],
            "model": model,
            "usage": full,
            "stop_reason": stop,
        },
    }


def test_last_assistant_returns_text_usage_and_stop_reason(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [
            {"type": "system", "message": "ignored"},
            assistant("first", output_tokens=1),
            {"type": "user", "message": {"content": "next turn"}},
            assistant("second", output_tokens=7, stop="end_turn"),
        ],
    )
    text, usage, stop = last_assistant(path)
    assert text == "second"
    assert usage["output_tokens"] == 7
    assert stop == "end_turn"


def test_last_assistant_concatenates_text_blocks_and_skips_non_text(tmp_path):
    line = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "a"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "text", "text": "b"},
            ],
            "usage": {},
            "stop_reason": "end_turn",
        },
    }
    text, _, _ = last_assistant(write_jsonl(tmp_path / "t.jsonl", [line]))
    assert text == "ab"


def test_last_assistant_keeps_last_nonempty_text_but_latest_usage(tmp_path):
    """A trailing tool-call-only assistant line carries usage but no text."""
    tool_only = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Bash"}],
            "usage": {"output_tokens": 99},
            "stop_reason": "tool_use",
        },
    }
    path = write_jsonl(tmp_path / "t.jsonl", [assistant("said this"), tool_only])
    text, usage, stop = last_assistant(path)
    assert text == "said this"
    assert usage["output_tokens"] == 99
    assert stop == "tool_use"


def test_last_assistant_tolerates_corrupt_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("not json\n" + json.dumps(assistant("ok")) + "\n")
    assert last_assistant(path)[0] == "ok"


def test_sum_turn_tokens_sums_the_last_turn_only(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [
            assistant("turn one", input_tokens=100, output_tokens=10),
            {"type": "user", "message": {"content": "a typed prompt"}},
            assistant("step", input_tokens=5, output_tokens=3),
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            assistant("step", input_tokens=6, output_tokens=4, cache_read_input_tokens=20),
        ],
    )
    (totals,) = sum_turn_tokens(path)
    assert totals.model == "claude-sonnet"
    assert totals.input == 11
    assert totals.output == 7
    assert totals.cache_read == 20
    assert totals.cache_creation == 0


def test_sum_turn_tokens_groups_by_model(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [
            {"type": "user", "message": {"content": "go"}},
            assistant("a", model="opus", output_tokens=1),
            assistant("b", model="haiku", output_tokens=2),
        ],
    )
    by_model = {t.model: t.output for t in sum_turn_tokens(path)}
    assert by_model == {"opus": 1, "haiku": 2}


def test_sum_turn_tokens_counts_each_api_message_once_at_its_last_line(tmp_path):
    # claude writes one line per content block of a response, each repeating
    # the response's usage; the output count can grow between them.
    def block(msg_id, text, **usage):
        line = assistant(text, **usage)
        line["message"]["id"] = msg_id
        return line

    path = write_jsonl(
        tmp_path / "t.jsonl",
        [
            {"type": "user", "message": {"content": "go"}},
            block("msg_1", "a", input_tokens=10, output_tokens=2, cache_read_input_tokens=100),
            block("msg_1", "b", input_tokens=10, output_tokens=5, cache_read_input_tokens=100),
            block("msg_1", "c", input_tokens=10, output_tokens=8, cache_read_input_tokens=100),
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            block("msg_2", "a", input_tokens=3, output_tokens=1, cache_creation_input_tokens=7),
            block("msg_2", "b", input_tokens=3, output_tokens=4, cache_creation_input_tokens=7),
        ],
    )
    (totals,) = sum_turn_tokens(path)
    got = (totals.input, totals.output, totals.cache_read, totals.cache_creation)
    assert got == (13, 12, 100, 7)


def test_sum_turn_tokens_on_missing_file_is_empty(tmp_path):
    assert sum_turn_tokens(tmp_path / "nope.jsonl") == []


# ---- which turn stopped ---------------------------------------------------------


def test_the_turn_marker_names_the_turn_and_is_found_again():
    from mtg_agent.wrapper.transcript import TURN_MARKER_RE, turn_marker

    marker = turn_marker("turn-dlef7p7my5b1")
    assert "turn-dlef7p7my5b1" in marker
    assert TURN_MARKER_RE.search(f"hand over now\n\n{marker}").group(1) == "turn-dlef7p7my5b1"


def test_last_turn_marker_reads_the_last_prompt_claude_took_in(tmp_path):
    from mtg_agent.wrapper.transcript import last_turn_marker, turn_marker

    path = write_jsonl(
        tmp_path / "t.jsonl",
        [
            {"type": "user", "message": {"content": f"play\n\n{turn_marker('turn-a')}"}},
            assistant(f"quoting {turn_marker('turn-echo')}"),
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": f"cat: {turn_marker('turn-tool')}"}
                    ]
                },
            },
            {
                "type": "user",
                "message": {"content": [{"type": "text", "text": turn_marker("turn-b")}]},
            },
            {
                "type": "attachment",
                "attachment": {
                    "type": "queued_command",
                    "prompt": f"hand over\n\n{turn_marker('turn-c')}",
                },
            },
            # Enqueued but not yet taken in: the turn that stopped never saw it.
            {"type": "queue-operation", "operation": "enqueue", "content": turn_marker("turn-d")},
            assistant("done"),
        ],
    )
    assert last_turn_marker(path) == "turn-c"


def test_last_turn_marker_is_empty_without_one(tmp_path):
    from mtg_agent.wrapper.transcript import last_turn_marker

    path = write_jsonl(tmp_path / "t.jsonl", [{"type": "user", "message": {"content": "go"}}])
    assert last_turn_marker(path) == ""
    assert last_turn_marker(tmp_path / "absent.jsonl") == ""
