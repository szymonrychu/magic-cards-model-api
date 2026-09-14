"""`playscripts.report.script_run_table`: the compact per-script run table a
handover prompt carries, off the `script_run` rows `POST .../script-runs`
writes for one seat in one game."""

from __future__ import annotations

from typing import Any

from magic_cards_model_api import audit
from magic_cards_model_api.playscripts import report


def run_detail(
    script_id: str,
    *,
    game_id: str = "g1",
    seat: str = "alice",
    exit: str = "chain_complete",
    priority: int | None = 50,
    advise: bool = False,
    reapplicable_after: str | None = None,
    invoked_via: str = "mcp",
) -> dict[str, Any]:
    return {
        "player": seat,
        "run_id": f"run-{script_id}",
        "parent_run_id": None,
        "game_id": game_id,
        "seat": seat,
        "role": "advisor" if advise else "seat",
        "advise": advise,
        "script_id": script_id,
        "script_sha": "sha",
        "guides_revision": "rev",
        "priority": priority,
        "opposing_archetype_asserted": "dimir-frog",
        "turn": 3,
        "step": "precombat_main",
        "seq_start": 1,
        "seq_end": 2,
        "moves_done": 1,
        "moves_total": 1,
        "exit": exit,
        "stop_reason": None,
        "refusal_text": None,
        "resumes": 0,
        "wall_ms": 10,
        "reapplicable_after": reapplicable_after,
        "invoked_via": invoked_via,
    }


def stored(seq: int, action: str, detail: dict[str, Any]) -> dict[str, Any]:
    """A row as `store.records` returns it."""
    return {"seq": seq, "payload": {"action": action, "detail": detail, "turn": 3}}


def entry(seq: int, action: str, detail: dict[str, Any]) -> dict[str, Any]:
    """A row as `audit.entries` returns it."""
    return {"seq": seq, "action": action, "detail": detail, "turn": 3}


def lines_of(table: str) -> list[str]:
    return table.splitlines()


def row_for(table: str, label: str) -> str:
    matches = [line for line in lines_of(table) if line.split(" | ")[0] == label]
    assert len(matches) == 1, table
    return matches[0]


# -- empty --------------------------------------------------------------------


def test_no_records_is_a_named_absence():
    table = report.script_run_table([], "g1", "alice")

    assert table == "no script runs recorded for alice in g1"


def test_other_seats_other_games_and_started_rows_are_not_this_seats_runs():
    records = [
        stored(1, audit.SCRIPT_RUN_ACTION, run_detail("s", seat="bob")),
        stored(2, audit.SCRIPT_RUN_ACTION, run_detail("s", game_id="g2")),
        stored(3, audit.SCRIPT_RUN_STARTED_ACTION, run_detail("s")),
        stored(4, "cast", {"player": "alice"}),
    ]

    assert report.script_run_table(records, "g1", "alice") == (
        "no script runs recorded for alice in g1"
    )


# -- one script ---------------------------------------------------------------


def test_one_script_is_a_header_and_one_row():
    records = [
        stored(1, audit.SCRIPT_RUN_ACTION, run_detail("fetch-shock", priority=60)),
        stored(
            2,
            audit.SCRIPT_RUN_ACTION,
            run_detail("fetch-shock", priority=60, exit="refused", reapplicable_after="true"),
        ),
        stored(3, audit.SCRIPT_RUN_ACTION, run_detail("fetch-shock", priority=60)),
    ]

    table = report.script_run_table(records, "g1", "alice")

    assert lines_of(table) == [
        "script | priority | runs | exits | reapplicable_after | invoked_via",
        "fetch-shock | 60 | 3 | chain_complete=2 refused=1 | 1 | mcp=3",
    ]
    assert report.TRUNCATION_MARKER not in table


def test_reapplicable_after_counts_only_runs_whose_recheck_still_applies():
    """The runner posts `"true"` or `"false"` for every run that resolved a play
    (`runtime._end`: one condition re-check at the end) and nothing for a run
    that found none, so only `"true"` is the count that means anything."""
    records = [
        stored(1, audit.SCRIPT_RUN_ACTION, run_detail("s", reapplicable_after="true")),
        stored(2, audit.SCRIPT_RUN_ACTION, run_detail("s", reapplicable_after="false")),
        stored(3, audit.SCRIPT_RUN_ACTION, run_detail("s", reapplicable_after="false")),
        stored(4, audit.SCRIPT_RUN_ACTION, run_detail("s", exit="no_match")),
        stored(5, audit.SCRIPT_RUN_ACTION, run_detail("s", reapplicable_after="true")),
    ]

    assert row_for(report.script_run_table(records, "g1", "alice"), "s") == (
        "s | 50 | 5 | chain_complete=4 no_match=1 | 2 | mcp=5"
    )


def test_audit_entries_shaped_rows_are_read_the_same():
    records = [entry(1, audit.SCRIPT_RUN_ACTION, run_detail("fetch-shock", priority=60))]

    assert row_for(report.script_run_table(records, "g1", "alice"), "fetch-shock") == (
        "fetch-shock | 60 | 1 | chain_complete=1 | 0 | mcp=1"
    )


def test_a_script_with_no_priority_prints_a_dash():
    records = [stored(1, audit.SCRIPT_RUN_ACTION, run_detail("s", priority=None))]

    assert row_for(report.script_run_table(records, "g1", "alice"), "s") == (
        "s | - | 1 | chain_complete=1 | 0 | mcp=1"
    )


def test_the_latest_reported_priority_is_the_one_shown():
    records = [
        stored(1, audit.SCRIPT_RUN_ACTION, run_detail("s", priority=10)),
        stored(2, audit.SCRIPT_RUN_ACTION, run_detail("s", priority=70)),
        stored(3, audit.SCRIPT_RUN_ACTION, run_detail("s", priority=None)),
    ]

    assert row_for(report.script_run_table(records, "g1", "alice"), "s").split(" | ")[1] == "70"


# -- many scripts -------------------------------------------------------------


def test_many_scripts_are_ordered_by_priority_then_id():
    records = [
        stored(1, audit.SCRIPT_RUN_ACTION, run_detail("low", priority=10)),
        stored(2, audit.SCRIPT_RUN_ACTION, run_detail("high-b", priority=90)),
        stored(3, audit.SCRIPT_RUN_ACTION, run_detail("unranked", priority=None)),
        stored(4, audit.SCRIPT_RUN_ACTION, run_detail("high-a", priority=90, exit="no_match")),
        stored(5, audit.SCRIPT_RUN_ACTION, run_detail("low", priority=10, exit="refused")),
    ]

    table = report.script_run_table(records, "g1", "alice")

    assert [line.split(" | ")[0] for line in lines_of(table)[1:]] == [
        "high-a",
        "high-b",
        "low",
        "unranked",
    ]
    assert row_for(table, "low") == "low | 10 | 2 | chain_complete=1 refused=1 | 0 | mcp=2"
    assert row_for(table, "high-a") == "high-a | 90 | 1 | no_match=1 | 0 | mcp=1"


# -- advise separation --------------------------------------------------------


def test_advise_runs_are_a_row_of_their_own():
    records = [
        stored(1, audit.SCRIPT_RUN_ACTION, run_detail("s", priority=50)),
        stored(2, audit.SCRIPT_RUN_ACTION, run_detail("s", priority=50, advise=True)),
        stored(
            3,
            audit.SCRIPT_RUN_ACTION,
            run_detail("s", priority=50, advise=True, exit="declined", reapplicable_after="true"),
        ),
    ]

    table = report.script_run_table(records, "g1", "alice")

    assert row_for(table, "s") == "s | 50 | 1 | chain_complete=1 | 0 | mcp=1"
    assert row_for(table, "s (advise)") == (
        "s (advise) | 50 | 2 | chain_complete=1 declined=1 | 1 | mcp=2"
    )


# -- invoked_via ----------------------------------------------------------------


def test_a_seats_own_mcp_run_names_no_operator():
    records = [stored(1, audit.SCRIPT_RUN_ACTION, run_detail("s", invoked_via="mcp"))]

    assert row_for(report.script_run_table(records, "g1", "alice"), "s") == (
        "s | 50 | 1 | chain_complete=1 | 0 | mcp=1"
    )


def test_a_cli_run_is_labelled_not_started_by_you():
    records = [stored(1, audit.SCRIPT_RUN_ACTION, run_detail("s", invoked_via="cli"))]

    assert row_for(report.script_run_table(records, "g1", "alice"), "s") == (
        "s | 50 | 1 | chain_complete=1 | 0 | cli=1 (not started by you)"
    )


def test_a_mix_of_mcp_and_cli_runs_tallies_both_and_still_flags_cli():
    records = [
        stored(1, audit.SCRIPT_RUN_ACTION, run_detail("s", invoked_via="mcp")),
        stored(2, audit.SCRIPT_RUN_ACTION, run_detail("s", invoked_via="mcp")),
        stored(3, audit.SCRIPT_RUN_ACTION, run_detail("s", invoked_via="cli")),
    ]

    assert row_for(report.script_run_table(records, "g1", "alice"), "s") == (
        "s | 50 | 3 | chain_complete=3 | 0 | cli=1 mcp=2 (not started by you)"
    )


def test_a_row_with_no_invoked_via_recorded_falls_back_to_unknown():
    """A ledger row from before S1 carries no `invoked_via` at all."""
    detail = run_detail("s")
    del detail["invoked_via"]
    records = [stored(1, audit.SCRIPT_RUN_ACTION, detail)]

    assert row_for(report.script_run_table(records, "g1", "alice"), "s") == (
        "s | 50 | 1 | chain_complete=1 | 0 | unknown=1"
    )


def test_an_armed_run_names_no_operator_either():
    """S2: an `armed` run is this seat's own too - `arming.arm` fired it
    between its own moves, not an operator staging one from the CLI - so it
    gets no `(not started by you)` flag, the same as a plain `mcp` run."""
    records = [stored(1, audit.SCRIPT_RUN_ACTION, run_detail("s", invoked_via="armed"))]

    assert row_for(report.script_run_table(records, "g1", "alice"), "s") == (
        "s | 50 | 1 | chain_complete=1 | 0 | armed=1"
    )


def test_a_mix_of_armed_and_cli_runs_flags_only_cli():
    records = [
        stored(1, audit.SCRIPT_RUN_ACTION, run_detail("s", invoked_via="armed")),
        stored(2, audit.SCRIPT_RUN_ACTION, run_detail("s", invoked_via="armed")),
        stored(3, audit.SCRIPT_RUN_ACTION, run_detail("s", invoked_via="cli")),
    ]

    assert row_for(report.script_run_table(records, "g1", "alice"), "s") == (
        "s | 50 | 3 | chain_complete=3 | 0 | armed=2 cli=1 (not started by you)"
    )


# -- truncation -----------------------------------------------------------------


def test_a_table_past_four_kilobytes_is_cut_with_a_marker():
    records = [
        stored(
            index,
            audit.SCRIPT_RUN_ACTION,
            run_detail(f"script-with-a-fairly-long-identifier-{index:04d}", priority=index),
        )
        for index in range(300)
    ]

    table = report.script_run_table(records, "g1", "alice")

    assert report.TABLE_LIMIT_BYTES == 4096
    assert len(table.encode("utf-8")) <= report.TABLE_LIMIT_BYTES
    lines = lines_of(table)
    assert lines[-1].startswith(report.TRUNCATION_MARKER)
    shown = len(lines) - 2
    assert shown > 0
    assert f"{300 - shown} more" in lines[-1]
    # The highest priorities survive the cut.
    assert lines[1].startswith("script-with-a-fairly-long-identifier-0299 | 299 |")


def test_a_table_exactly_at_the_limit_is_not_cut():
    header = "script | priority | runs | exits | reapplicable_after | invoked_via\n"
    fixed = " | 1 | 1 | chain_complete=1 | 0 | mcp=1"
    script_id = "x" * (report.TABLE_LIMIT_BYTES - len(header) - len(fixed))
    records = [stored(1, audit.SCRIPT_RUN_ACTION, run_detail(script_id, priority=1))]

    table = report.script_run_table(records, "g1", "alice")

    assert len(table.encode("utf-8")) == report.TABLE_LIMIT_BYTES
    assert report.TRUNCATION_MARKER not in table


def test_a_single_row_too_long_to_fit_is_cut_rather_than_overflowing():
    records = [stored(1, audit.SCRIPT_RUN_ACTION, run_detail("y" * 10_000, priority=1))]

    table = report.script_run_table(records, "g1", "alice")

    assert len(table.encode("utf-8")) <= report.TABLE_LIMIT_BYTES
    assert lines_of(table)[-1].startswith(report.TRUNCATION_MARKER)
    assert "1 more" in lines_of(table)[-1]
