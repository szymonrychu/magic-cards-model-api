"""`playscripts.report.script_run_table`/`offer_table`: the compact per-script
run and per-offer tables a handover prompt carries, off the rows `POST
.../script-runs` writes for one seat in one game."""

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


# == offer_table ===============================================================
#
# `offer_table` (API-8): one line per play a runner offered this seat, off the
# `script_run_offered` rows `phase: offer` writes - accept/reject, never a run
# count. See `report.offer_table`'s own docstring for what "the next
# committing row", "offer set" and "passed over" mean precisely.


def offer_detail(
    script_id: str,
    run_id: str,
    *,
    game_id: str = "g1",
    seat: str = "alice",
    turn: int = 3,
    step: str = "precombat_main",
    priority: int | None = 50,
    advise: bool = False,
) -> dict[str, Any]:
    return {
        "player": seat,
        "run_id": run_id,
        "parent_run_id": None,
        "game_id": game_id,
        "seat": seat,
        "role": "seat",
        "advise": advise,
        "script_id": script_id,
        "script_sha": "sha",
        "guides_revision": "rev",
        "priority": priority,
        "opposing_archetype_asserted": "dimir-frog",
        "turn": turn,
        "step": step,
        "seq_start": None,
        "seq_end": None,
        "moves_done": None,
        "moves_total": None,
        "exit": None,
        "stop_reason": None,
        "refusal_text": None,
        "resumes": None,
        "wall_ms": None,
        "reapplicable_after": None,
        "invoked_via": "mcp",
    }


def started_detail(
    script_id: str,
    run_id: str,
    *,
    game_id: str = "g1",
    seat: str = "alice",
    turn: int = 3,
    step: str = "precombat_main",
) -> dict[str, Any]:
    return {
        "player": seat,
        "run_id": run_id,
        "parent_run_id": None,
        "game_id": game_id,
        "seat": seat,
        "role": "seat",
        "advise": False,
        "script_id": script_id,
        "script_sha": "sha",
        "guides_revision": "rev",
        "priority": 50,
        "opposing_archetype_asserted": "dimir-frog",
        "turn": turn,
        "step": step,
        "seq_start": None,
        "seq_end": None,
        "moves_done": None,
        "moves_total": None,
        "exit": None,
        "stop_reason": None,
        "refusal_text": None,
        "resumes": None,
        "wall_ms": None,
        "reapplicable_after": None,
        "invoked_via": "mcp",
    }


def action_detail(
    seat: str = "alice", *, allowed: bool | None = True, reasoning: str | None = "because"
) -> dict[str, Any]:
    return {"player": seat, "allowed": allowed, "reasoning": reasoning}


def offer_row_for(table: str, run_id: str) -> str:
    matches = [line for line in lines_of(table) if line.split(" | ")[1] == run_id]
    assert len(matches) == 1, table
    return matches[0]


def test_no_offers_is_a_named_absence():
    assert report.offer_table([], "g1", "alice") == "no script offers recorded for alice in g1"


def test_an_offer_with_nothing_after_it_is_just_offered_not_declined():
    """API-8 requirement 4: a cut log window or a finished game leaves an
    offer with no committing row after it - that is an offer and nothing
    else, never a decline."""
    records = [stored(1, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("fetch-shock", "run-1"))]

    table = report.offer_table(records, "g1", "alice")

    row = offer_row_for(table, "run-1")
    assert row.split(" | ")[5] == "offered"
    assert "declined" not in row


def test_a_started_row_for_the_same_play_is_taken():
    records = [
        stored(1, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("fetch-shock", "run-1")),
        stored(2, audit.SCRIPT_RUN_STARTED_ACTION, started_detail("fetch-shock", "run-1")),
    ]

    table = report.offer_table(records, "g1", "alice")

    assert offer_row_for(table, "run-1").split(" | ")[5] == "taken"


def test_a_started_row_for_a_sibling_offer_is_passed_over_not_declined():
    """F3: several plays ride one answer under a conflict - the hint offers
    every candidate as its own row at the same seat/turn/step, adjacent in
    the ledger, and the seat can take at most one. The others are 'passed
    over', not declined, so a reviewer bar counting `declined` never fires on
    a seat that did exactly what the conflict hint told it to."""
    records = [
        stored(1, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("fetch-shock", "run-a")),
        stored(2, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("fetch-shock", "run-b")),
        stored(3, audit.SCRIPT_RUN_STARTED_ACTION, started_detail("fetch-shock", "run-b")),
    ]

    table = report.offer_table(records, "g1", "alice")

    assert offer_row_for(table, "run-a").split(" | ")[5] == "passed over"
    assert offer_row_for(table, "run-b").split(" | ")[5] == "taken"


def test_offers_at_different_ledger_positions_are_not_one_offer_set():
    """Same seat/turn/step but NOT adjacent in the ledger (something else was
    recorded in between) is two separate offer sets, so a start of the other
    one is a decline, not a pass-over."""
    records = [
        stored(1, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("fetch-shock", "run-a")),
        stored(2, "cast", action_detail(allowed=False, reasoning=None)),
        stored(3, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("fetch-shock", "run-b")),
        stored(4, audit.SCRIPT_RUN_STARTED_ACTION, started_detail("fetch-shock", "run-b")),
    ]

    table = report.offer_table(records, "g1", "alice")

    assert offer_row_for(table, "run-a").split(" | ")[5] == "declined"
    assert offer_row_for(table, "run-b").split(" | ")[5] == "taken"


def test_a_decline_names_the_action_and_its_reasoning():
    """Reasoning is already mandatory on every committing agent action but a
    pass (`records.require_reasoning`), so it rides along for free here. A
    non-committing `plan_declared` row in between is skipped, never mistaken
    for the commitment."""
    records = [
        stored(1, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("fetch-shock", "run-1")),
        stored(2, audit.PLAN_ACTION, {"player": "alice", "plan": "attack"}),
        stored(3, "cast", action_detail(reasoning="racing the clock, tapping out now")),
    ]

    table = report.offer_table(records, "g1", "alice")

    row = offer_row_for(table, "run-1")
    fields = row.split(" | ")
    assert fields[5] == "declined"
    assert fields[6] == "cast"
    assert fields[7] == "racing the clock, tapping out now"


def test_a_decline_by_pass_names_the_action_with_no_blank_reasoning():
    """A pass declining an offer carries its action name and no reasoning
    (`require_reasoning`'s `pass_shaped` exemption) - the table says so
    rather than leaving the column blank. A refused (`allowed: false`) row in
    between is not a commitment and is skipped."""
    records = [
        stored(1, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("fetch-shock", "run-1")),
        stored(2, "cast", action_detail(allowed=False, reasoning=None)),
        stored(3, "pass_priority", action_detail(allowed=True, reasoning=None)),
    ]

    table = report.offer_table(records, "g1", "alice")

    row = offer_row_for(table, "run-1")
    fields = row.split(" | ")
    assert fields[5] == "declined"
    assert fields[6] == "pass_priority"
    assert fields[7] != "-"
    assert fields[7].strip() != ""
    assert "reasoning" in fields[7].lower()


def test_an_advise_only_plays_offer_is_counted_and_not_a_failure():
    """An advisor's play can be offered and declined like any other; it can
    simply never be 'taken' (only the seat itself ever posts `script_run_
    started`). With nothing after it, that must still read as 'offered', not
    as some implied failure."""
    records = [
        stored(
            1,
            audit.SCRIPT_RUN_OFFERED_ACTION,
            offer_detail("advisor-line", "run-1", advise=True),
        )
    ]

    table = report.offer_table(records, "g1", "alice")

    row = offer_row_for(table, "run-1")
    assert row.split(" | ")[0] == "advisor-line (advise)"
    assert row.split(" | ")[5] == "offered"


def test_offers_are_ordered_by_priority_then_sequence():
    records = [
        stored(1, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("low", "run-low", priority=10)),
        stored(2, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("high", "run-high", priority=90)),
        stored(3, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("none", "run-none", priority=None)),
    ]

    table = report.offer_table(records, "g1", "alice")

    assert [line.split(" | ")[1] for line in lines_of(table)[1:]] == [
        "run-high",
        "run-low",
        "run-none",
    ]


def test_other_seats_other_games_are_not_this_seats_offers():
    records = [
        stored(1, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("s", "run-1", seat="bob")),
        stored(2, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("s", "run-2", game_id="g2")),
    ]

    assert report.offer_table(records, "g1", "alice") == (
        "no script offers recorded for alice in g1"
    )


def test_audit_entries_shaped_offer_rows_are_read_the_same():
    records = [entry(1, audit.SCRIPT_RUN_OFFERED_ACTION, offer_detail("fetch-shock", "run-1"))]

    table = report.offer_table(records, "g1", "alice")

    assert offer_row_for(table, "run-1").split(" | ")[5] == "offered"


def test_an_offer_table_past_four_kilobytes_is_cut_with_a_marker():
    records = [
        stored(
            index,
            audit.SCRIPT_RUN_OFFERED_ACTION,
            offer_detail(
                f"script-with-a-fairly-long-identifier-{index:04d}",
                f"run-{index:04d}",
                priority=index,
            ),
        )
        for index in range(300)
    ]

    table = report.offer_table(records, "g1", "alice")

    assert report.TABLE_LIMIT_BYTES == 4096
    assert len(table.encode("utf-8")) <= report.TABLE_LIMIT_BYTES
    lines = lines_of(table)
    assert lines[-1].startswith(report.TRUNCATION_MARKER)
    shown = len(lines) - 2
    assert shown > 0
    assert f"{300 - shown} more" in lines[-1]
    # The highest priorities survive the cut.
    assert "run-0299" in lines[1]
