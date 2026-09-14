"""The scorecard's per-script tally: `scripts/eval_report.py` over the
`script_run` rows a seat's play-script runner writes (`POST .../script-runs`,
`records.record_script_run_finish`).

Every row here is shaped the way that route writes it - `detail` carries each
`schemas.ScriptRunIn` field plus `game_id` and `player` - so a change to the
record shape breaks these tests rather than quietly changing the tally.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from magic_cards_model_api import audit
from scripts import eval_report


def row(seq: int, action: str, **detail: Any) -> dict[str, Any]:
    """One `records` row, shaped as `audit.record` writes it."""
    return {
        "seq": seq,
        "payload": {
            "ts": "2026-09-13T00:00:00+00:00",
            "action": action,
            "detail": detail,
            "turn": detail.get("turn", 1),
            "step": "precombat_main",
            "phase": "precombat_main",
            "stack_size": 0,
            "snapshot": None,
            "reasoning": None,
            "legal_actions_before": None,
            "legal_actions_for": None,
        },
    }


def script_run(
    seq: int,
    game_id: str,
    script_id: str,
    *,
    seat: str = "alice",
    exit: str = "chain_complete",
    archetype: str | None = "dimir-frog",
    advise: bool = False,
    resumes: int | None = 0,
    wall_ms: int | None = 100,
    priority: int | None = 50,
    reapplicable_after: str | None = None,
    invoked_via: str = "mcp",
) -> dict[str, Any]:
    """One finished run's `script_run` row, every `ScriptRunIn` field set."""
    return row(
        seq,
        audit.SCRIPT_RUN_ACTION,
        player=seat,
        run_id=f"run-{game_id}-{seq}",
        parent_run_id=None,
        game_id=game_id,
        seat=seat,
        role="advisor" if advise else "seat",
        advise=advise,
        script_id=script_id,
        script_sha="sha",
        guides_revision="rev",
        priority=priority,
        opposing_archetype_asserted=archetype,
        turn=3,
        step="precombat_main",
        seq_start=seq,
        seq_end=seq + 1,
        moves_done=1,
        moves_total=1,
        exit=exit,
        stop_reason=None,
        refusal_text=None,
        resumes=resumes,
        wall_ms=wall_ms,
        reapplicable_after=reapplicable_after,
        invoked_via=invoked_via,
    )


def ledger_game(
    game_id: str, runs: list[dict[str, Any]], *, winner: str | None = "__unfinished__"
) -> eval_report.GameRows:
    rows = [row(1, "create_game", players=["alice", "bob"]), *runs]
    if winner != "__unfinished__":
        rows.append(row(len(rows) + 1, "game_over", winner=winner, drawn=winner is None, turn=7))
    return eval_report.GameRows(
        game_id=game_id,
        players=["alice", "bob"],
        created_at="2026-09-13T00:00:00+00:00",
        rows=rows,
    )


def only(report: dict[str, Any], script_id: str, archetype: str | None = "dimir-frog"):
    matches = [
        entry
        for entry in report["scripts"]
        if entry["script_id"] == script_id and entry["opposing_archetype_asserted"] == archetype
    ]
    assert len(matches) == 1, report["scripts"]
    return matches[0]


# -- empty --------------------------------------------------------------------


def test_a_ledger_with_no_script_runs_reports_an_empty_tally():
    report = eval_report.build_report([ledger_game("g1", [], winner="alice")])

    assert report["scripts"] == []


def test_no_script_runs_renders_as_a_named_absence():
    text = eval_report.render(eval_report.build_report([ledger_game("g1", [], winner="alice")]))

    assert "SCRIPTS" in text
    assert "no script runs recorded" in text


def test_an_empty_ledger_reports_an_empty_tally():
    assert eval_report.build_report([])["scripts"] == []


# -- S1 T3: by_invoker, per seat -----------------------------------------------


def test_a_seat_with_no_runs_reports_an_empty_by_invoker():
    report = eval_report.build_report([ledger_game("g1", [], winner="alice")])

    assert report["seats"]["alice"]["scripts"]["by_invoker"] == {}


def test_by_invoker_tallies_mcp_and_cli_separately_per_seat():
    runs = [
        script_run(2, "g1", "fetch-shock", invoked_via="mcp"),
        script_run(3, "g1", "fetch-shock", invoked_via="mcp"),
        script_run(4, "g1", "float", invoked_via="cli"),
    ]
    report = eval_report.build_report([ledger_game("g1", runs, winner="alice")])

    assert report["seats"]["alice"]["scripts"]["by_invoker"] == {"cli": 1, "mcp": 2}


def test_by_invoker_keeps_each_seat_apart():
    runs = [
        script_run(2, "g1", "fetch-shock", seat="alice", invoked_via="mcp"),
        script_run(3, "g1", "fetch-shock", seat="bob", invoked_via="cli"),
    ]
    report = eval_report.build_report([ledger_game("g1", runs, winner="alice")])

    assert report["seats"]["alice"]["scripts"]["by_invoker"] == {"mcp": 1}
    assert report["seats"]["bob"]["scripts"]["by_invoker"] == {"cli": 1}


def test_by_invoker_accumulates_across_games():
    games = [
        ledger_game("g1", [script_run(2, "g1", "s", invoked_via="cli")], winner="alice"),
        ledger_game("g2", [script_run(2, "g2", "s", invoked_via="cli")], winner="alice"),
    ]

    report = eval_report.build_report(games)

    assert report["seats"]["alice"]["scripts"]["by_invoker"] == {"cli": 2}


def test_a_row_with_no_invoked_via_recorded_falls_back_to_unknown():
    """A ledger row from before S1 carries no `invoked_via` at all."""
    detail_row = script_run(2, "g1", "s")
    del detail_row["payload"]["detail"]["invoked_via"]

    report = eval_report.build_report([ledger_game("g1", [detail_row], winner="alice")])

    assert report["seats"]["alice"]["scripts"]["by_invoker"] == {"unknown": 1}


def test_the_rendered_report_shows_by_invoker_on_the_seats_own_lines():
    runs = [
        script_run(2, "g1", "fetch-shock", seat="alice", invoked_via="mcp"),
        script_run(3, "g1", "float", seat="alice", invoked_via="cli"),
    ]
    text = eval_report.render(eval_report.build_report([ledger_game("g1", runs, winner="alice")]))

    assert "scripts by invoker cli=1, mcp=1" in text


# -- one script ---------------------------------------------------------------


def test_one_script_tallies_runs_completion_exits_resumes_and_wall_time():
    runs = [
        script_run(2, "g1", "fetch-shock", exit="chain_complete", resumes=1, wall_ms=100),
        script_run(3, "g1", "fetch-shock", exit="chain_complete", resumes=0, wall_ms=300),
        script_run(4, "g1", "fetch-shock", exit="refused", resumes=2, wall_ms=None),
    ]
    report = eval_report.build_report([ledger_game("g1", runs, winner="alice")])

    entry = only(report, "fetch-shock")
    assert entry["runs"] == 3
    assert entry["completion"]["successes"] == 2
    assert entry["completion"]["trials"] == 3
    assert entry["exits"] == {"chain_complete": 2, "refused": 1}
    assert entry["resumes"] == 3
    assert entry["wall_ms_mean"] == 200.0


def test_a_script_run_row_is_not_reported_as_unclassified_play():
    runs = [
        row(2, audit.SCRIPT_RUN_STARTED_ACTION, player="alice", seat="alice", script_id="s"),
        script_run(3, "g1", "s"),
    ]
    report = eval_report.build_report([ledger_game("g1", runs, winner="alice")])

    assert report["unclassified_actions"] == {}
    assert report["seats"]["alice"]["actions"] == 0


def test_a_started_row_is_not_a_run():
    runs = [row(2, audit.SCRIPT_RUN_STARTED_ACTION, player="alice", seat="alice", script_id="s")]
    report = eval_report.build_report([ledger_game("g1", runs, winner="alice")])

    assert report["scripts"] == []


def test_a_run_with_no_wall_time_reports_no_mean_rather_than_zero():
    runs = [script_run(2, "g1", "s", wall_ms=None)]
    entry = only(eval_report.build_report([ledger_game("g1", runs)]), "s")

    assert entry["wall_ms_mean"] is None


def test_completion_rate_carries_an_interval():
    runs = [script_run(2, "g1", "s")]
    report = eval_report.build_report([ledger_game("g1", runs, winner="alice")])

    rates = list(eval_report.every_rate(only(report, "s")))
    assert only(report, "s")["completion"] in rates
    assert only(report, "s")["win_rate_when_completed"] in rates


# -- many scripts -------------------------------------------------------------


def test_each_script_and_asserted_archetype_is_its_own_row():
    runs = [
        script_run(2, "g1", "fetch-shock", archetype="dimir-frog"),
        script_run(3, "g1", "fetch-shock", archetype="boros-energy", exit="refused"),
        script_run(4, "g1", "bolt-face", archetype="dimir-frog", exit="no_match"),
        script_run(5, "g1", "bolt-face", archetype=None),
    ]
    report = eval_report.build_report([ledger_game("g1", runs, winner="alice")])

    assert len(report["scripts"]) == 4
    assert only(report, "fetch-shock", "dimir-frog")["exits"] == {"chain_complete": 1}
    assert only(report, "fetch-shock", "boros-energy")["exits"] == {"refused": 1}
    assert only(report, "bolt-face", "dimir-frog")["exits"] == {"no_match": 1}
    assert only(report, "bolt-face", None)["runs"] == 1


def test_script_rows_are_ordered_by_script_then_archetype():
    runs = [
        script_run(2, "g1", "zeta", archetype="b"),
        script_run(3, "g1", "alpha", archetype=None),
        script_run(4, "g1", "alpha", archetype="a"),
        script_run(5, "g1", "zeta", archetype="a"),
    ]
    report = eval_report.build_report([ledger_game("g1", runs)])

    assert [(e["script_id"], e["opposing_archetype_asserted"]) for e in report["scripts"]] == [
        ("alpha", None),
        ("alpha", "a"),
        ("zeta", "a"),
        ("zeta", "b"),
    ]


def test_runs_across_games_accumulate():
    games = [
        ledger_game("g1", [script_run(2, "g1", "s")], winner="alice"),
        ledger_game("g2", [script_run(2, "g2", "s", exit="refused")], winner="bob"),
    ]
    entry = only(eval_report.build_report(games), "s")

    assert entry["runs"] == 2
    assert entry["exits"] == {"chain_complete": 1, "refused": 1}


def test_win_rate_is_over_finished_games_where_the_seat_completed_the_chain():
    games = [
        # alice completes twice and wins: one game, not two.
        ledger_game("g1", [script_run(2, "g1", "s"), script_run(3, "g1", "s")], winner="alice"),
        # alice completes and loses.
        ledger_game("g2", [script_run(2, "g2", "s")], winner="bob"),
        # alice never completes the chain and wins: not a game this rate is over.
        ledger_game("g3", [script_run(2, "g3", "s", exit="refused")], winner="alice"),
        # alice completes, but the game never ended: no winner to count.
        ledger_game("g4", [script_run(2, "g4", "s")]),
        # bob completes the same script and wins: his seat, his game.
        ledger_game("g5", [script_run(2, "g5", "s", seat="bob")], winner="bob"),
        # a draw counts as a finished game, and not a win.
        ledger_game("g6", [script_run(2, "g6", "s")], winner=None),
    ]
    rate = only(eval_report.build_report(games), "s")["win_rate_when_completed"]

    assert rate["successes"] == 2
    assert rate["trials"] == 4


# -- advise separation --------------------------------------------------------


def test_advise_runs_are_counted_apart_from_the_seats_own_runs():
    runs = [
        script_run(2, "g1", "s", exit="chain_complete"),
        script_run(3, "g1", "s", exit="chain_complete", advise=True, resumes=4, wall_ms=900),
        script_run(4, "g1", "s", exit="declined", advise=True, resumes=1, wall_ms=100),
    ]
    entry = only(eval_report.build_report([ledger_game("g1", runs, winner="alice")]), "s")

    assert entry["runs"] == 1
    assert entry["exits"] == {"chain_complete": 1}
    assert entry["completion"]["trials"] == 1
    assert entry["resumes"] == 0
    assert entry["wall_ms_mean"] == 100.0
    advise = entry["advise"]
    assert advise["runs"] == 2
    assert advise["exits"] == {"chain_complete": 1, "declined": 1}
    assert advise["completion"]["successes"] == 1
    assert advise["completion"]["trials"] == 2
    assert advise["resumes"] == 5
    assert advise["wall_ms_mean"] == 500.0


def test_an_advise_only_script_reports_zero_own_runs():
    runs = [script_run(2, "g1", "s", advise=True)]
    entry = only(eval_report.build_report([ledger_game("g1", runs, winner="alice")]), "s")

    assert entry["runs"] == 0
    assert entry["completion"]["trials"] == 0
    assert entry["win_rate_when_completed"]["trials"] == 0
    assert entry["advise"]["runs"] == 1
    assert entry["advise"]["win_rate_when_completed"]["successes"] == 1
    assert entry["advise"]["win_rate_when_completed"]["trials"] == 1


def test_the_rendered_report_shows_advise_runs_on_their_own_line():
    runs = [script_run(2, "g1", "s"), script_run(3, "g1", "s", advise=True, exit="declined")]
    text = eval_report.render(eval_report.build_report([ledger_game("g1", runs, winner="alice")]))

    section = text.split("SCRIPTS", 1)[1]
    assert "s vs dimir-frog" in section
    assert "chain_complete x1" in section
    assert "advise" in section
    assert "declined x1" in section
    assert "95%" in section


def test_a_rendered_script_with_no_asserted_archetype_says_so():
    runs = [script_run(2, "g1", "s", archetype=None)]
    text = eval_report.render(eval_report.build_report([ledger_game("g1", runs)]))

    assert "s vs (no archetype asserted)" in text


# -- the cli ------------------------------------------------------------------


def _write_ledger(tmp_path: Path) -> Path:
    games = [
        ledger_game("g1", [script_run(2, "g1", "s")], winner="alice"),
        ledger_game("g2", [script_run(2, "g2", "s", exit="refused")], winner="bob"),
    ]
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps([game.to_json() for game in games]))
    return path


def test_json_output_off_a_ledger_file_carries_the_script_tally(tmp_path, capsys):
    path = _write_ledger(tmp_path)

    assert eval_report.main(["--ledger", str(path), "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert only(report, "s")["runs"] == 2


def test_game_filter_limits_the_script_tally_off_a_ledger_file(tmp_path, capsys):
    path = _write_ledger(tmp_path)

    assert eval_report.main(["--ledger", str(path), "--game", "g2", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["games"] == 1
    assert only(report, "s")["exits"] == {"refused": 1}


def test_text_output_off_a_ledger_file_carries_the_script_tally(tmp_path, capsys):
    path = _write_ledger(tmp_path)

    assert eval_report.main(["--ledger", str(path), "--game", "g1"]) == 0

    text = capsys.readouterr().out
    assert "SCRIPTS" in text
    assert "s vs dimir-frog" in text
    assert "refused" not in text.split("SCRIPTS", 1)[1].split("STALE SPLIT", 1)[0]


def test_the_script_run_action_names_match_the_audit_module():
    assert audit.SCRIPT_RUN_ACTION == eval_report.SCRIPT_RUN_ACTION
    assert set(audit.SCRIPT_RUN_ACTIONS) <= eval_report.NOT_PLAY
