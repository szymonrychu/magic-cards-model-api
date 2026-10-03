"""Telemetry attribution across the seat pair: the orchestrator's own
transcript plus every subagent/teammate transcript of its session (T1's
`fixtures/seat_pair` layout), each tagged with a role from its agent name.

Both T1 fixture sets are used. Their helpers are called `alpha` and `beta`;
`installed(..., rename=True)` renames them to the seat pair's own names
(`current-1`, `future-1`, `seat_pair.current_name`/`future_name`) the way
the orchestrator spawns them, so the same real transcripts exercise the
role-specific rules.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry

from mtg_agent.seat_pair import current_name, future_name
from mtg_agent.wrapper import activity
from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.metrics import Metrics
from mtg_agent.wrapper.session import Manager
from mtg_agent.wrapper.turns import Store

FIXTURES = Path(__file__).parent / "fixtures" / "seat_pair"
CHANNELS = ("teams", "subagents")
RENAME = {"alpha": current_name(1), "beta": future_name(1)}


# ---- fixture plumbing -------------------------------------------------------


def _lines(path: Path) -> list[dict]:
    return [json.loads(raw) for raw in path.read_text().splitlines() if raw.strip()]


def _write_lines(path: Path, lines: list[dict]) -> None:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def _append(path: Path, *lines: dict) -> None:
    with path.open("a") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def _who(description: str) -> str:
    return description.rsplit(" ", 1)[-1]


def _rename_main(main: Path) -> None:
    lines = _lines(main)
    for line in lines:
        content = (line.get("message") or {}).get("content")
        if line.get("type") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_use":
                continue
            tool_input = block.get("input") or {}
            if block.get("name") == "Agent":
                who = tool_input.get("name") or _who(str(tool_input.get("description") or ""))
                tool_input["name"] = RENAME.get(who, who)
            elif block.get("name") == "SendMessage":
                for key in ("to", "recipient"):
                    if tool_input.get(key) in RENAME:
                        tool_input[key] = RENAME[tool_input[key]]
    _write_lines(main, lines)


def _rename_meta(meta_path: Path) -> None:
    meta = json.loads(meta_path.read_text())
    if meta.get("name") in RENAME:
        meta["name"] = RENAME[meta["name"]]
        meta_path.write_text(json.dumps(meta))


#: What a lead line stopping a helper carries (a `TaskStop`, a teams
#: `shutdown_request`, a `killed` task notification, `teammate_terminated`).
STOP_MARKERS = ('"TaskStop"', "shutdown_request", "<status>killed", "teammate_terminated")


def installed(
    tmp_path: Path, channel: str, rename: bool = True, live: bool = True
) -> tuple[Path, Path]:
    """`(home, main transcript)` - the fixture copied where the wrapper reads
    it (`$HOME/.claude/projects/-workspace`). Both probes end by stopping
    their helpers; `live` drops those lead lines, so the session reads as one
    still in play."""
    home = tmp_path / "home"
    projects = home / ".claude" / "projects"
    shutil.copytree(FIXTURES / channel / "-workspace", projects / "-workspace")
    main = next((projects / "-workspace").glob("*.jsonl"))
    if live:
        _write_lines(
            main,
            [
                line
                for line in _lines(main)
                if not any(marker in json.dumps(line) for marker in STOP_MARKERS)
            ],
        )
    if rename:
        _rename_main(main)
        for meta_path in subagents_dir(main).glob("*.meta.json"):
            _rename_meta(meta_path)
    return home, main


def subagents_dir(main: Path) -> Path:
    return main.with_suffix("") / "subagents"


def agent_file(main: Path, who: str) -> Path:
    """The helper transcript whose spawn description names `who` (alpha/beta)."""
    for meta_path in subagents_dir(main).glob("*.meta.json"):
        meta = json.loads(meta_path.read_text())
        if _who(str(meta.get("description") or "")) == who:
            return meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
    raise AssertionError(f"no helper {who}")


def agent_id(main: Path, who: str) -> str:
    return agent_file(main, who).stem.removeprefix("agent-")


def ts(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def last_ts(path: Path) -> float:
    return max(ts(line["timestamp"]) for line in _lines(path) if "timestamp" in line)


def session_end(main: Path) -> float:
    return max(last_ts(path) for path in [main, *subagents_dir(main).glob("*.jsonl")])


def helper_line(epoch: float, *blocks: dict, kind: str = "assistant") -> dict:
    line = {
        "type": kind,
        "isSidechain": True,
        "timestamp": iso(epoch),
        "message": {"role": kind, "content": list(blocks)},
    }
    if kind == "assistant":
        line["message"]["model"] = "claude-opus-5-5"
    return line


def lead_tool_use(epoch: float, name: str, tool_input: dict, tool_id: str = "toolu_x") -> dict:
    return {
        "type": "assistant",
        "isSidechain": False,
        "timestamp": iso(epoch),
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
        },
    }


def tool_use(tool_id: str, name: str) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def tool_result(tool_id: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}


def watched(home: Path, metrics: Metrics | None = None) -> activity.ActivityWatcher:
    watcher = activity.ActivityWatcher(home, "tools", poll=0.01, metrics=metrics)
    watcher.poll_once()
    return watcher


def sample(metrics: Metrics, name: str, labels: dict) -> float:
    return metrics.registry.get_sample_value(name, labels) or 0.0


# ---- (a) every transcript of the session, each with a role ------------------


@pytest.mark.parametrize(
    ("name", "role"),
    [
        (current_name(1), "current"),
        (current_name(12), "current"),
        (future_name(3), "future"),
        ("rules-researcher", "helper"),
        ("alpha", "helper"),
        ("", "helper"),
    ],
)
def test_an_agent_name_says_its_role(name, role):
    assert activity.role_for_name(name) == role


@pytest.mark.parametrize("channel", CHANNELS)
def test_the_watcher_follows_the_lead_and_every_helper_with_its_role(tmp_path, channel):
    home, main = installed(tmp_path, channel)
    watcher = watched(home)
    assert watcher.agent_roles() == {
        activity.MAIN_AGENT: "orchestrator",
        agent_id(main, "alpha"): "current",
        agent_id(main, "beta"): "future",
    }
    # Every helper transcript was actually tailed, not merely listed.
    for who in ("alpha", "beta"):
        assert watcher.agent_activity(agent_id(main, who)) is not None
    assert watcher.agent_activity(activity.MAIN_AGENT) is not None


@pytest.mark.parametrize("channel", CHANNELS)
def test_helpers_with_no_seat_pair_name_are_plain_helpers(tmp_path, channel):
    home, main = installed(tmp_path, channel, rename=False)
    watcher = watched(home)
    assert watcher.agent_roles() == {
        activity.MAIN_AGENT: "orchestrator",
        agent_id(main, "alpha"): "helper",
        agent_id(main, "beta"): "helper",
    }


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_helper_spawned_after_the_first_poll_is_picked_up(tmp_path, channel):
    home, main = installed(tmp_path, channel)
    beta = agent_file(main, "beta")
    parked = tmp_path / "parked"
    parked.mkdir()
    shutil.move(beta, parked / beta.name)
    watcher = watched(home)
    assert agent_id(main, "beta") not in watcher.agent_roles()
    shutil.move(parked / beta.name, beta)
    watcher.poll_once()
    assert watcher.agent_roles()[agent_id(main, "beta")] == "future"


@pytest.mark.parametrize("channel", CHANNELS)
def test_stopping_the_reporting_current_makes_the_waiting_future_current(tmp_path, channel):
    """S5's cycle: on `committed:` the orchestrator stops the current; the
    server already made the future's token live, and no message follows."""
    home, main = installed(tmp_path, channel)
    stop = (
        ("TaskStop", {"task_id": current_name(1)})
        if channel == "subagents"
        else ("SendMessage", {"to": current_name(1), "message": {"type": "shutdown_request"}})
    )
    _append(main, lead_tool_use(session_end(main) + 1, *stop))
    watcher = watched(home)
    assert watcher.agent_roles()[agent_id(main, "beta")] == "current"
    assert watcher.agent_roles()[agent_id(main, "alpha")] == "helper"
    assert watcher.activity_snapshot()["role"] == "current"


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_revoke_makes_the_waiting_future_current(tmp_path, channel):
    """Recovery: the current is gone without a report, the orchestrator
    calls `pass_baton(revoke=true)` and hands that live token to the future.
    A plain message to the future (a liveness check) promotes nobody."""
    home, main = installed(tmp_path, channel)
    at = session_end(main) + 1
    _append(main, lead_tool_use(at, "SendMessage", {"to": future_name(1), "message": "alive?"}))
    assert watched(home).agent_roles()[agent_id(main, "beta")] == "future"
    _append(
        main,
        lead_tool_use(
            at + 1, "mcp__mtg__pass_baton", {"player_uid": "u", "revoke": True}, "toolu_r"
        ),
        lead_tool_use(
            at + 2, "SendMessage", {"to": future_name(1), "message": "baton b2"}, "toolu_m"
        ),
    )
    watcher = watched(home)
    assert watcher.agent_roles()[agent_id(main, "beta")] == "current"
    assert watcher.agent_roles()[agent_id(main, "alpha")] == "helper"


# ---- (b) the pill: current first, orchestrator as the fallback --------------


@pytest.mark.parametrize("channel", CHANNELS)
def test_the_pill_shows_the_current_agent_prefixed_with_its_role(tmp_path, channel):
    home, main = installed(tmp_path, channel)
    watcher = watched(home)
    pill = watcher.activity_snapshot()
    current = watcher.agent_activity(agent_id(main, "alpha"))
    assert pill["role"] == "current"
    assert pill["state"] == current["state"]
    assert pill["seq"] == current["seq"]
    assert pill["label"] == f"current: {current['state']}"


@pytest.mark.parametrize("channel", CHANNELS)
def test_future_activity_never_replaces_the_current_on_the_pill(tmp_path, channel):
    home, main = installed(tmp_path, channel)
    at = session_end(main) + 5
    _append(
        agent_file(main, "beta"),
        helper_line(at, tool_use("toolu_f", "mcp__mtg__battlefield_state")),
    )
    _append(agent_file(main, "alpha"), helper_line(at - 2, tool_use("toolu_c", "mcp__mtg__pass")))
    watcher = watched(home)
    assert watcher.agent_activity(agent_id(main, "beta"))["state"] == activity.READING_BOARD
    pill = watcher.activity_snapshot()
    assert pill["role"] == "current"
    assert pill["state"] == activity.ACTING


@pytest.mark.parametrize("channel", CHANNELS)
def test_with_no_current_the_pill_falls_back_to_the_orchestrator(tmp_path, channel):
    home, _ = installed(tmp_path, channel, rename=False)
    watcher = watched(home)
    pill = watcher.activity_snapshot()
    orchestrator = watcher.agent_activity(activity.MAIN_AGENT)
    assert pill["role"] == "orchestrator"
    assert pill["state"] == orchestrator["state"]
    assert pill["label"] == f"orchestrator: {orchestrator['state']}"


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_stopped_current_hands_the_pill_back_to_the_orchestrator(tmp_path, channel):
    home, main = installed(tmp_path, channel)
    at = session_end(main) + 1
    _append(
        main,
        lead_tool_use(at, "TaskStop", {"task_id": future_name(1)}),
        lead_tool_use(at + 1, "TaskStop", {"task_id": current_name(1)}),
    )
    watcher = watched(home)
    assert watcher.activity_snapshot()["role"] == "orchestrator"


@pytest.mark.parametrize(
    ("channel", "stopped"), [("teams", {"alpha", "beta"}), ("subagents", {"beta"})]
)
def test_the_probes_own_stops_are_read_off_the_lead_transcript(tmp_path, channel, stopped):
    """teams: both teammates got a `shutdown_request`; subagents: beta got a
    `TaskStop` (and a `killed` notification), alpha only completed."""
    home, main = installed(tmp_path, channel, live=False)
    roster = activity.AgentRoster()
    roster.feed_main(_lines(main))
    for who in ("alpha", "beta"):
        path = agent_file(main, who)
        roster.add_agent(
            agent_id(main, who), json.loads(path.with_suffix(".meta.json").read_text())
        )
    assert {
        who for who in ("alpha", "beta") if roster.stopped_at(agent_id(main, who)) is not None
    } == stopped
    expected = "current" if "alpha" not in stopped else "orchestrator"
    assert watched(home).activity_snapshot()["role"] == expected


@pytest.mark.parametrize("channel", CHANNELS)
def test_the_session_snapshot_publishes_the_role_prefixed_pill(tmp_path, channel):
    home, _ = installed(tmp_path, channel)
    watcher = watched(home)
    manager = Manager(Settings.from_env({}), Store(), Metrics())
    manager.activity_source = watcher.activity_snapshot
    published = manager.snapshot()["activity"]
    assert published["role"] == "current"
    assert published["label"].startswith("current: ")


# ---- (c) the stall timer counts any live agent's line -----------------------


def stall_manager(now: list[float], timeout: float) -> Manager:
    settings = Settings.from_env({"TURN_TIMEOUT_SECONDS": str(timeout)})
    manager = Manager(settings, Store(), Metrics(), now=lambda: now[0])
    # A turn in flight, without a PTY: what `submit` leaves behind.
    manager._current = "turn-x"
    manager._current_started = now[0]
    return manager


@pytest.mark.parametrize("channel", CHANNELS)
def test_last_progress_is_the_newest_line_of_any_agent(tmp_path, channel):
    home, main = installed(tmp_path, channel)
    at = session_end(main) + 300
    _append(agent_file(main, "alpha"), helper_line(at, {"type": "thinking", "thinking": ""}))
    watcher = watched(home)
    assert watcher.last_progress() == pytest.approx(at)


@pytest.mark.parametrize("channel", CHANNELS)
def test_an_idle_orchestrator_while_the_current_thinks_is_not_a_stall(tmp_path, channel):
    home, main = installed(tmp_path, channel)
    lead_idle_since = last_ts(main)
    thinking_at = session_end(main) + 300
    _append(agent_file(main, "alpha"), helper_line(thinking_at, {"type": "thinking"}))
    watcher = watched(home)
    now = [lead_idle_since]
    manager = stall_manager(now, timeout=120)
    manager.progress_source = watcher.last_progress
    try:
        now[0] = thinking_at + 60  # the lead has been silent 360s+, current 60s
        manager.mark_stall_suspected("turn-x")
        assert manager.snapshot()["stallSuspectedSince"] is None
        assert manager.metrics.turn_stall_suspected_total._value.get() == 0
    finally:
        manager.shutdown()


@pytest.mark.parametrize("channel", CHANNELS)
def test_no_line_from_any_agent_for_the_timeout_is_still_a_stall(tmp_path, channel):
    home, main = installed(tmp_path, channel)
    watcher = watched(home)
    now = [session_end(main)]
    manager = stall_manager(now, timeout=120)
    manager.progress_source = watcher.last_progress
    try:
        now[0] = session_end(main) + 121
        manager.mark_stall_suspected("turn-x")
        assert manager.snapshot()["stallSuspectedSince"] == now[0]
        assert manager.metrics.turn_stall_suspected_total._value.get() == 1
    finally:
        manager.shutdown()


def test_progress_after_a_suspected_stall_clears_it():
    now = [1000.0]
    progress: list[float | None] = [None]
    manager = stall_manager(now, timeout=120)
    manager.progress_source = lambda: progress[0]
    try:
        now[0] = 1121.0
        manager.mark_stall_suspected("turn-x")
        assert manager.snapshot()["stallSuspectedSince"] == 1121.0
        progress[0] = 1200.0
        now[0] = 1241.0
        manager.mark_stall_suspected("turn-x")
        assert manager.snapshot()["stallSuspectedSince"] is None
    finally:
        manager.shutdown()


def test_progress_from_before_the_turn_started_does_not_count():
    now = [1000.0]
    manager = stall_manager(now, timeout=120)
    manager.progress_source = lambda: 900.0
    try:
        now[0] = 1121.0
        manager.mark_stall_suspected("turn-x")
        assert manager.snapshot()["stallSuspectedSince"] == 1121.0
    finally:
        manager.shutdown()


# ---- (d) tokens, cost and inference by role and model -----------------------


def test_token_series_carry_role_and_model():
    metrics = Metrics(registry=CollectorRegistry(), effort="low")
    metrics.record_tokens(
        [
            {"model": "claude-sonnet-5", "output": 3},
            {"role": "current", "model": "claude-opus-5-5", "output": 7},
        ]
    )
    labels = {"type": "output", "effort": "low"}
    assert (
        sample(
            metrics,
            "ccw_turn_tokens_total",
            labels | {"role": "orchestrator", "model": "claude-sonnet-5"},
        )
        == 3
    )
    assert (
        sample(
            metrics,
            "ccw_turn_tokens_total",
            labels | {"role": "current", "model": "claude-opus-5-5"},
        )
        == 7
    )


def test_inference_series_carry_role_and_model():
    metrics = Metrics(registry=CollectorRegistry())
    metrics.record_inference(2.0, 300, role="future", model="claude-opus-5-5")
    assert (
        sample(
            metrics,
            "ccw_inference_seconds_count",
            {"output_tokens_bucket": "200-500", "role": "future", "model": "claude-opus-5-5"},
        )
        == 1
    )


def test_cost_series_carry_role_and_model():
    metrics = Metrics(registry=CollectorRegistry())
    assert metrics.record_cost({"total_cost_usd": 0.5}, model="claude-sonnet-5")
    assert (
        sample(
            metrics,
            "ccw_turn_cost_usd_total",
            {"role": "orchestrator", "model": "claude-sonnet-5"},
        )
        == 0.5
    )


def test_token_cost_is_priced_per_model_and_kept_per_role():
    metrics = Metrics(registry=CollectorRegistry())
    metrics.record_token_cost(
        [
            {"model": "claude-sonnet-5", "input": 1_000_000, "output": 1_000_000},
            {
                "role": "current",
                "model": "claude-opus-5-5",
                "input": 1_000_000,
                "output": 1_000_000,
                "cache_read": 1_000_000,
                "cache_creation": 1_000_000,
            },
            {"role": "future", "model": "some-unpriced-model", "input": 5},
        ]
    )
    orchestrator = {"role": "orchestrator", "model": "claude-sonnet-5"}
    current = {"role": "current", "model": "claude-opus-5-5"}
    assert sample(metrics, "ccw_turn_cost_usd_total", orchestrator) == pytest.approx(12.0)
    assert sample(metrics, "ccw_turn_cost_usd_total", current) == pytest.approx(4 + 20 + 0.2 + 5)
    assert (
        metrics.registry.get_sample_value(
            "ccw_turn_cost_usd_total", {"role": "future", "model": "some-unpriced-model"}
        )
        is None
    )


def _expected_helper_tokens(path: Path) -> dict[str, int]:
    """Usage of one helper transcript, each API message counted once."""
    seen: dict[str, dict] = {}
    for line in _lines(path):
        message = line.get("message") or {}
        if line.get("type") == "assistant" and message.get("usage"):
            seen[message.get("id") or line.get("requestId")] = message["usage"]
    return {
        "input": sum(u.get("input_tokens") or 0 for u in seen.values()),
        "output": sum(u.get("output_tokens") or 0 for u in seen.values()),
        "cache_read": sum(u.get("cache_read_input_tokens") or 0 for u in seen.values()),
        "cache_creation": sum(u.get("cache_creation_input_tokens") or 0 for u in seen.values()),
    }


@pytest.mark.parametrize("channel", CHANNELS)
def test_helper_turn_tokens_sum_each_role_once_per_message(tmp_path, channel):
    _, main = installed(tmp_path, channel)
    entries = activity.agent_turn_tokens(main, since=0.0, until=session_end(main) + 1)
    by_role = {entry["role"]: entry for entry in entries}
    assert set(by_role) == {"current", "future"}
    for role, who in (("current", "alpha"), ("future", "beta")):
        entry = by_role[role]
        assert entry["model"] == "claude-opus-5-5"
        expected = _expected_helper_tokens(agent_file(main, who))
        assert {k: entry[k] for k in expected} == expected
        assert entry["output"] > 0


@pytest.mark.parametrize("channel", CHANNELS)
def test_helper_turn_tokens_only_count_the_window(tmp_path, channel):
    _, main = installed(tmp_path, channel)
    assert activity.agent_turn_tokens(main, since=session_end(main) + 1, until=1e12) == []


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_completed_turn_totals_every_agent_and_meters_each_role(tmp_path, channel):
    _, main = installed(tmp_path, channel)
    now = [0.0]
    metrics = Metrics(registry=CollectorRegistry())
    manager = Manager(
        Settings.from_env({"MODEL": "claude-sonnet-5"}), Store(), metrics, now=lambda: now[0]
    )
    manager.store.create("turn-x", "go", "", 0.0)
    manager._current, manager._current_started = "turn-x", 0.0
    now[0] = session_end(main) + 1
    orchestrator_tokens = {"model": "claude-sonnet-5", "input": 2, "output": 40}
    manager.complete({"transcriptPath": str(main), "turnTokens": [orchestrator_tokens]})
    record = manager.store.get("turn-x")
    roles = {entry.get("role", "orchestrator"): entry for entry in record.turn_tokens}
    assert set(roles) == {"orchestrator", "current", "future"}
    assert roles["orchestrator"] == orchestrator_tokens
    output = {"type": "output", "effort": manager.settings.effort}
    assert (
        sample(
            metrics,
            "ccw_turn_tokens_total",
            output | {"role": "orchestrator", "model": "claude-sonnet-5"},
        )
        == 40
    )
    for role in ("current", "future"):
        assert (
            sample(
                metrics,
                "ccw_turn_tokens_total",
                output | {"role": role, "model": "claude-opus-5-5"},
            )
            == roles[role]["output"]
        )
        assert (
            sample(metrics, "ccw_turn_cost_usd_total", {"role": role, "model": "claude-opus-5-5"})
            > 0
        )
    assert (
        sample(
            metrics, "ccw_turn_cost_usd_total", {"role": "orchestrator", "model": "claude-sonnet-5"}
        )
        > 0
    )


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_second_turn_does_not_count_the_first_turns_helper_usage_again(tmp_path, channel):
    _, main = installed(tmp_path, channel)
    now = [0.0]
    manager = Manager(Settings.from_env({}), Store(), Metrics(), now=lambda: now[0])
    for turn in ("turn-1", "turn-2"):
        manager.store.create(turn, "go", "", now[0])
        manager._current, manager._current_started = turn, now[0]
        now[0] = session_end(main) + 1
        manager.complete({"transcriptPath": str(main), "turnTokens": []})
    assert manager.store.get("turn-1").turn_tokens
    assert manager.store.get("turn-2").turn_tokens == []


@pytest.mark.parametrize("channel", CHANNELS)
def test_the_watcher_times_inferences_per_role_and_model(tmp_path, channel):
    home, _ = installed(tmp_path, channel)
    metrics = Metrics(registry=CollectorRegistry())
    watched(home, metrics)
    body = metrics.render()[0].decode()
    assert 'model="claude-sonnet-5"' in body
    for role in ("orchestrator", "current", "future"):
        model = "claude-sonnet-5" if role == "orchestrator" else "claude-opus-5-5"
        counted = [
            line
            for line in body.splitlines()
            if line.startswith("ccw_inference_seconds_count{")
            and f'role="{role}"' in line
            and f'model="{model}"' in line
        ]
        assert counted, f"no inference recorded for {role}"


# ---- (e) step timing per role ------------------------------------------------


def _completed_tool_calls(path: Path) -> int:
    issued: set[str] = set()
    done = 0
    for line in _lines(path):
        content = (line.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                issued.add(block.get("id"))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in issued:
                done += 1
    return done


@pytest.mark.parametrize("channel", CHANNELS)
def test_model_and_tool_steps_are_recorded_per_role(tmp_path, channel):
    home, main = installed(tmp_path, channel)
    metrics = Metrics(registry=CollectorRegistry())
    watched(home, metrics)
    for role, path in (
        ("orchestrator", main),
        ("current", agent_file(main, "alpha")),
        ("future", agent_file(main, "beta")),
    ):
        calls = _completed_tool_calls(path)
        assert calls > 0
        assert sample(metrics, "ccw_model_step_seconds_count", {"role": role}) == calls
    assert (
        sample(
            metrics,
            "ccw_tool_seconds_count",
            {"role": "current", "tool": "battlefield_state"},
        )
        == 1
    )


def test_the_current_model_step_is_timed_from_its_own_tool_result_not_futures(tmp_path):
    """current: result at t+10, next tool_use at t+14 (a 4s model step).
    future's read lands at t+13 in between; a shared timer would call
    current's step 1s."""
    home = tmp_path / "home"
    project = home / ".claude" / "projects" / "-workspace"
    subagents = project / "sess" / "subagents"
    subagents.mkdir(parents=True)
    t = 1_790_000_000.0
    main = project / "sess.jsonl"
    _write_lines(
        main,
        [
            lead_tool_use(t, "Agent", {"name": current_name(1)}, "toolu_spawn_c"),
            lead_tool_use(t, "Agent", {"name": future_name(1)}, "toolu_spawn_f"),
        ],
    )
    for agent, name in (("acur", current_name(1)), ("afut", future_name(1))):
        (subagents / f"agent-{agent}.meta.json").write_text(json.dumps({"name": name}))
    _write_lines(
        subagents / "agent-acur.jsonl",
        [
            helper_line(t + 1, tool_use("c1", "mcp__mtg__pass_until")),
            helper_line(t + 10, tool_result("c1"), kind="user"),
            helper_line(t + 14, tool_use("c2", "mcp__mtg__cast")),
            helper_line(t + 15, tool_result("c2"), kind="user"),
        ],
    )
    _write_lines(
        subagents / "agent-afut.jsonl",
        [
            helper_line(t + 12, tool_use("f1", "mcp__mtg__battlefield_state")),
            helper_line(t + 13, tool_result("f1"), kind="user"),
        ],
    )
    metrics = Metrics(registry=CollectorRegistry())
    watched(home, metrics)
    cast = sample(metrics, "ccw_tool_seconds_count", {"role": "current", "tool": "cast"})
    assert cast == 1
    # current's first step has no boundary in its file (timed from the
    # timer's start, clamped to 0); its second is the 4s step.
    current_sum = sample(metrics, "ccw_model_step_seconds_sum", {"role": "current"})
    assert current_sum >= 4.0
    assert sample(metrics, "ccw_model_step_seconds_count", {"role": "future"}) == 1
    future_sum = sample(metrics, "ccw_model_step_seconds_sum", {"role": "future"})
    assert future_sum < 12.0 + 1e-6
    buckets = {
        float(line.split('le="')[1].split('"')[0])
        if 'le="+Inf"' not in line
        else float("inf"): float(line.rsplit(" ", 1)[1])
        for line in metrics.render()[0].decode().splitlines()
        if line.startswith("ccw_model_step_seconds_bucket{") and 'role="current"' in line
    }
    # The 4s step sits above the 2s bucket edge: with interleaving it would be 1s.
    assert buckets[2.0] <= 1
    assert buckets[5.0] >= 1


def test_a_prompt_line_starts_a_new_model_step():
    """A teammate or task-notification prompt wakes an agent: its next
    tool_use is timed from that prompt, not from a tool result minutes ago."""
    metrics = Metrics(registry=CollectorRegistry())
    timer = activity.StepTimer(metrics=metrics)
    t = 1_790_000_000.0
    timer.feed(
        [
            helper_line(t, tool_use("a", "Bash")),
            helper_line(t + 1, tool_result("a"), kind="user"),
            {"type": "user", "timestamp": iso(t + 100), "message": {"content": "wake up"}},
            helper_line(t + 102, tool_use("b", "Bash")),
            helper_line(t + 103, tool_result("b"), kind="user"),
        ]
    )
    total = sample(metrics, "ccw_model_step_seconds_sum", {"role": "orchestrator"})
    assert total < 10
