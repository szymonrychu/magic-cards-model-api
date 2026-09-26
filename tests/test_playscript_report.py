"""D2b: `playscripts.report`'s post-game development tables - the lines a
seat played by hand, the per-game evidence record the driver writes for
`plays admit` to commit, the cross-game evidence table aggregated from those
committed files, and the development candidates a handover must answer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from magic_cards_model_api import audit
from magic_cards_model_api.playscripts import report
from magic_cards_model_api.records import SCRIPT_STAGED_ACTION

FROG_ID = "5f0c3a52-2b44-4c55-8d1a-0f3f5d1e9a01"
UNNAMED_ID = "9b8c816f-33af-4e64-a2c0-115cf73b37b4"


def row(seq: int, action: str, detail: dict[str, Any]) -> dict[str, Any]:
    """A row as `GET /api/games/{id}/log` answers it (`audit.entries`)."""
    return {"seq": seq, "action": action, "detail": detail, "turn": 3}


def move(seq: int, action: str, *, seat: str = "alice", allowed: bool | None = True, **extra):
    return row(seq, action, {"player": seat, "allowed": allowed, "reasoning": "r", **extra})


def script_row(
    seq: int,
    action: str,
    script_id: str,
    run_id: str,
    *,
    seat: str = "alice",
    game_id: str = "g1",
    turn: int = 3,
    step: str = "precombat_main",
    **extra,
) -> dict[str, Any]:
    return row(
        seq,
        action,
        {
            "player": seat,
            "seat": seat,
            "game_id": game_id,
            "run_id": run_id,
            "script_id": script_id,
            "priority": 50,
            "advise": False,
            "turn": turn,
            "step": step,
            **extra,
        },
    )


def offered(seq, script_id, run_id, **kw):
    return script_row(seq, audit.SCRIPT_RUN_OFFERED_ACTION, script_id, run_id, **kw)


def started(seq, script_id, run_id, **kw):
    return script_row(seq, audit.SCRIPT_RUN_STARTED_ACTION, script_id, run_id, **kw)


def finished(seq, script_id, run_id, **kw):
    return script_row(seq, audit.SCRIPT_RUN_ACTION, script_id, run_id, exit="done", **kw)


def line_of(table: str, line: str) -> str:
    matches = [text for text in table.splitlines() if text.split(" | ")[0] == line]
    assert len(matches) == 1, table
    return matches[0]


# -- hand_played_summary ---------------------------------------------------------


def test_no_hand_played_line_is_a_named_absence():
    assert report.hand_played_summary([], "g1", "alice") == (
        "no hand-played casts, activations or land plays recorded for alice in g1"
    )


def test_hand_played_lines_are_grouped_by_kind_and_card_and_repeats_are_candidates():
    records = [
        move(1, "play_land", card="Watery Grave"),
        move(2, "cast", card="Psychic Frog"),
        move(3, "play_land", card="Watery Grave"),
        move(4, "cast", card="Fatal Push"),
        move(5, "play_land", card="Watery Grave"),
    ]

    table = report.hand_played_summary(records, "g1", "alice")

    assert table.splitlines()[0] == report.HAND_PLAYED_HEADER
    assert line_of(table, "play land Watery Grave") == "play land Watery Grave | 3 | candidate"
    assert line_of(table, "cast Psychic Frog") == "cast Psychic Frog | 1 | -"
    assert line_of(table, "cast Fatal Push") == "cast Fatal Push | 1 | -"
    # Candidates first, most repeated first.
    assert table.splitlines()[1].startswith("play land Watery Grave")


def test_moves_inside_a_script_run_are_not_hand_played():
    """A script's own moves are rows of their own, between its start and its
    finish (and inside the finish's `seq_start`..`seq_end`); they are what a
    script already does, never a line to write a script for."""
    records = [
        started(1, "fetch-shock", "run-1"),
        move(2, "activate", card_id=FROG_ID, ability_index=0),
        move(3, "play_land", card="Watery Grave"),
        finished(4, "fetch-shock", "run-1", seq_start=1, seq_end=3),
        move(5, "play_land", card="Watery Grave"),
    ]

    table = report.hand_played_summary(records, "g1", "alice")

    assert line_of(table, "play land Watery Grave") == "play land Watery Grave | 1 | -"
    assert "activate" not in table


def test_refused_paused_and_other_seats_moves_are_not_hand_played():
    records = [
        move(1, "cast", card="Psychic Frog", allowed=False),
        move(2, "cast", card="Psychic Frog", allowed=None),
        move(3, "cast", card="Psychic Frog", seat="bob"),
        move(4, "pass_priority"),
        move(5, "cast", card="Psychic Frog"),
    ]

    table = report.hand_played_summary(records, "g1", "alice")

    assert line_of(table, "cast Psychic Frog") == "cast Psychic Frog | 1 | -"
    assert len(table.splitlines()) == 2


def test_a_card_named_by_id_is_named_from_the_ledger_when_it_can_be():
    records = [
        row(1, "add_card", {"card": "Psychic Frog", "card_id": FROG_ID, "player": "alice"}),
        move(2, "cast", card=FROG_ID),
        move(3, "cast", card="Psychic Frog"),
        move(4, "activate", card_id=FROG_ID, ability_index=1),
        move(5, "activate", card_id=UNNAMED_ID, ability_index=None),
    ]

    table = report.hand_played_summary(records, "g1", "alice")

    assert line_of(table, "cast Psychic Frog") == "cast Psychic Frog | 2 | candidate"
    assert line_of(table, "activate Psychic Frog ability 1") == (
        "activate Psychic Frog ability 1 | 1 | -"
    )
    assert line_of(table, f"activate card {UNNAMED_ID}") == f"activate card {UNNAMED_ID} | 1 | -"


def test_the_hand_played_summary_is_bounded_like_the_other_tables():
    records = [move(n, "cast", card=f"Card number {n:04d} {'x' * 60}") for n in range(1, 300)]

    table = report.hand_played_summary(records, "g1", "alice")

    assert len(table.encode("utf-8")) <= report.TABLE_LIMIT_BYTES
    assert table.splitlines()[-1].startswith(report.TRUNCATION_MARKER)


# -- development candidates --------------------------------------------------------


def test_candidates_are_declined_and_passed_over_plays_plus_repeated_hand_lines():
    records = [
        offered(1, "fetch-shock", "run-a"),
        offered(2, "fetch-surveil", "run-b"),
        started(3, "fetch-surveil", "run-b"),
        finished(4, "fetch-surveil", "run-b", seq_start=3, seq_end=4),
        offered(5, "frog-attack", "run-c"),
        move(6, "cast", card="Fatal Push"),
        offered(7, "taken-play", "run-d"),
        started(8, "taken-play", "run-d"),
        finished(9, "taken-play", "run-d", seq_start=8, seq_end=9),
        move(10, "cast", card="Fatal Push"),
        move(11, "cast", card="Psychic Frog"),
        offered(12, "unanswered", "run-e"),
    ]

    candidates = report.development_candidates(records, "g1", "alice")

    # fetch-shock was passed over for its sibling, frog-attack declined for a
    # hand cast; the taken play and the offer nobody answered are not.
    assert candidates.plays == ("fetch-shock", "frog-attack")
    assert candidates.lines == ("cast Fatal Push",)
    assert not candidates.empty


def test_no_candidates_is_empty():
    assert report.development_candidates([], "g1", "alice").empty


# -- evidence_record ------------------------------------------------------------------


def test_the_evidence_record_counts_each_plays_outcomes_with_the_offer_pairing():
    records = [
        offered(1, "fetch-shock", "run-a"),
        offered(2, "fetch-shock", "run-b"),
        started(3, "fetch-shock", "run-b"),
        offered(4, "frog-attack", "run-c"),
        move(5, "cast", card="Fatal Push"),
        offered(6, "frog-attack", "run-d"),
        offered(7, "frog-attack", "run-e", game_id="another-game"),
        offered(8, "frog-attack", "run-f", seat="bob"),
    ]

    record = report.evidence_record(records, "g1", "alice", "dimir-frog", "izzet prowess")

    assert record == {
        "game_id": "g1",
        "deck": "dimir-frog",
        "opponent_archetype": "izzet prowess",
        "plays": {
            "fetch-shock": {"offered": 2, "taken": 1, "passed_over": 1, "declined": 0},
            "frog-attack": {"offered": 2, "taken": 0, "passed_over": 0, "declined": 1},
        },
    }
    json.dumps(record)


def test_an_evidence_record_with_no_offers_has_no_plays():
    record = report.evidence_record([], "g1", "alice", "dimir-frog", None)
    assert record == {
        "game_id": "g1",
        "deck": "dimir-frog",
        "opponent_archetype": None,
        "plays": {},
    }


# -- evidence_table ---------------------------------------------------------------------


def write_evidence(root: Path, deck: str, game_id: str, plays: dict, mtime: float) -> None:
    folder = root / "decks" / deck / "plays" / "evidence"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{game_id}.json"
    path.write_text(
        json.dumps({"game_id": game_id, "deck": deck, "opponent_archetype": "x", "plays": plays})
    )
    os.utime(path, (mtime, mtime))


def counts(offered=0, taken=0, passed_over=0, declined=0) -> dict[str, int]:
    return {"offered": offered, "taken": taken, "passed_over": passed_over, "declined": declined}


def test_no_evidence_is_a_named_absence(tmp_path):
    assert report.evidence_table(tmp_path, "dimir-frog") == (
        "no cross-game evidence recorded for dimir-frog yet "
        "(decks/dimir-frog/plays/evidence/ holds no game)"
    )


def test_the_evidence_table_aggregates_every_game_and_counts_distinct_games(tmp_path):
    write_evidence(
        tmp_path,
        "dimir-frog",
        "g1",
        {"fetch-shock": counts(2, 1, 0, 1), "frog-attack": counts(1, 0, 1, 0)},
        100,
    )
    write_evidence(tmp_path, "dimir-frog", "g2", {"fetch-shock": counts(3, 1, 0, 2)}, 200)
    write_evidence(tmp_path, "prowess", "g3", {"fetch-shock": counts(9, 0, 0, 9)}, 300)

    table = report.evidence_table(tmp_path, "dimir-frog")

    assert table.splitlines()[0] == report.EVIDENCE_HEADER
    assert line_of(table, "fetch-shock") == "fetch-shock | 2 | 5 | 2 | 0 | 3 | 2"
    assert line_of(table, "frog-attack") == "frog-attack | 1 | 1 | 0 | 1 | 0 | 0"
    # Most declined first.
    assert table.splitlines()[1].startswith("fetch-shock")


def test_the_evidence_table_reads_only_the_newest_games(tmp_path):
    for n in range(5):
        write_evidence(tmp_path, "dimir-frog", f"g{n}", {f"play-{n}": counts(1, 0, 0, 1)}, 100 + n)

    table = report.evidence_table(tmp_path, "dimir-frog", games=2)

    assert "play-4" in table and "play-3" in table
    assert "play-2" not in table and "play-0" not in table


def test_an_unreadable_evidence_file_is_skipped_and_named(tmp_path):
    write_evidence(tmp_path, "dimir-frog", "g1", {"fetch-shock": counts(1, 0, 0, 1)}, 100)
    bad = tmp_path / "decks" / "dimir-frog" / "plays" / "evidence" / "g2.json"
    bad.write_text("{not json")

    table = report.evidence_table(tmp_path, "dimir-frog")

    assert line_of(table, "fetch-shock") == "fetch-shock | 1 | 1 | 0 | 0 | 1 | 1"
    assert "1 unreadable evidence file(s) skipped" in table


def test_the_evidence_table_is_bounded(tmp_path):
    plays = {f"play-{n:03d}-{'x' * 60}": counts(1, 0, 0, 1) for n in range(200)}
    write_evidence(tmp_path, "dimir-frog", "g1", plays, 100)

    table = report.evidence_table(tmp_path, "dimir-frog")

    assert len(table.encode("utf-8")) <= report.TABLE_LIMIT_BYTES
    assert table.splitlines()[-1].startswith(report.TRUNCATION_MARKER)


def test_the_evidence_record_round_trips_into_the_evidence_table(tmp_path):
    records = [offered(1, "frog-attack", "run-c"), move(2, "cast", card="Fatal Push")]
    record = report.evidence_record(records, "g1", "alice", "dimir-frog", None)
    folder = tmp_path / "decks" / "dimir-frog" / "plays" / "evidence"
    folder.mkdir(parents=True)
    (folder / "g1.json").write_text(json.dumps(record))

    table = report.evidence_table(tmp_path, "dimir-frog")

    assert line_of(table, "frog-attack") == "frog-attack | 1 | 1 | 0 | 0 | 1 | 1"


# ======================================================================
# FD7 (1): decision candidates - every expressible decision `seat` faced is
# owed a play or a `plays no-change --decision <seq>`, deduped by its
# generalized DSL (`decision_dsl.decision_rows`' own `dsl`), never only the
# old declined-offers-plus-repeated-hand-lines set (`development_candidates`,
# unchanged above and still read by `app._followup_payload`). `assess`
# reports which of them are still open.
# ======================================================================


def decision_row(
    seq: int,
    action: str,
    *,
    player: str = "alice",
    turn: int = 3,
    step: str = "precombat_main",
    active_player: str = "alice",
    **detail: Any,
) -> dict[str, Any]:
    """A ledger row shaped for `decision_dsl.decision_rows` (`turn`/`step`/
    `active_player` top-level, as `audit.entries()` writes them) - matching
    `tests/test_decision_dsl.py`'s own `row()`, not this file's `row()`,
    which never carries `step`/`active_player` and so never yields a
    `dsl` for anything but the vocabulary gaps."""
    return {
        "seq": seq,
        "action": action,
        "detail": {"player": player, "allowed": True, **detail},
        "turn": turn,
        "step": step,
        "active_player": active_player,
    }


def plays_outcome(seq: int, player: str = "alice", **detail: Any) -> dict[str, Any]:
    return {
        "seq": seq,
        "action": audit.PLAYS_OUTCOME_ACTION,
        "detail": {"player": player, **detail},
        "turn": 3,
    }


def test_every_expressible_decision_is_a_candidate():
    records = [
        decision_row(1, "pass_priority"),
        decision_row(2, "pass_priority", step="combat_damage"),
    ]
    candidates = report.decision_candidates(records, "alice")
    assert {c.seqs for c in candidates} == {(1,), (2,)}


def test_repeated_identical_decisions_are_one_candidate_deduped_by_generalized_dsl():
    records = [decision_row(1, "pass_priority"), decision_row(2, "pass_priority")]
    candidates = report.decision_candidates(records, "alice")
    assert len(candidates) == 1
    assert candidates[0].seqs == (1, 2)
    assert candidates[0].id == report.decision_candidate_id(1)


def test_an_unexpressible_decision_is_never_a_candidate():
    records = [decision_row(1, "mulligan_declared", keep=True), decision_row(2, "concede")]
    assert report.decision_candidates(records, "alice") == ()


def test_a_different_seats_decisions_are_not_this_seats_candidates():
    records = [decision_row(1, "pass_priority", player="bob", active_player="bob")]
    assert report.decision_candidates(records, "alice") == ()


def test_assess_reports_every_candidate_as_open_with_nothing_in_the_ledger():
    records = [
        decision_row(1, "pass_priority"),
        decision_row(2, "pass_priority", step="combat_damage"),
    ]
    verdict = report.assess(records, "alice")
    assert len(verdict.candidates) == 2
    assert {c.id for c in verdict.open} == {c.id for c in verdict.candidates}
    assert not verdict.satisfied


def test_a_no_change_by_decision_closes_only_its_own_candidate():
    records = [
        decision_row(1, "pass_priority"),
        decision_row(2, "pass_priority", step="combat_damage"),
        plays_outcome(
            3,
            admitted=[],
            no_change=[{"candidate": report.decision_candidate_id(1), "reason": "fine as is"}],
        ),
    ]
    verdict = report.assess(records, "alice")
    assert len(verdict.candidates) == 2
    assert [c.seqs for c in verdict.open] == [(2,)]
    assert not verdict.satisfied


def test_a_no_change_closes_every_seq_of_its_own_group():
    records = [
        decision_row(1, "pass_priority"),
        decision_row(2, "pass_priority"),
        plays_outcome(
            3,
            admitted=[],
            no_change=[{"candidate": report.decision_candidate_id(1), "reason": "fine as is"}],
        ),
    ]
    verdict = report.assess(records, "alice")
    assert len(verdict.candidates) == 1
    assert verdict.open == ()
    assert verdict.satisfied


def test_an_admitted_play_closes_every_open_candidate():
    records = [
        decision_row(1, "pass_priority"),
        decision_row(2, "pass_priority", step="combat_damage"),
        plays_outcome(
            3,
            admitted=[
                {"dsl_change": True, "added": ["new-play"], "updated": [], "reprioritized": []}
            ],
        ),
    ]
    verdict = report.assess(records, "alice")
    assert verdict.open == ()
    assert verdict.satisfied


def test_an_admit_carrying_nothing_admitted_closes_nothing():
    records = [decision_row(1, "pass_priority"), plays_outcome(2, admitted=[])]
    verdict = report.assess(records, "alice")
    assert len(verdict.open) == 1
    assert not verdict.satisfied


def test_a_reprioritization_only_admit_with_no_real_dsl_change_closes_nothing():
    """Review fix: `admitted` non-empty is not by itself "development
    happened" - a pure `plays priority` nudge (`dsl_change: False`, nothing
    in `added`/`updated`) is not a DSL change, exactly the distinction
    `mtg_agent.driver.handover.assess`'s own `dsl_change` already draws, and
    must not silently close every open candidate in the game."""
    records = [
        decision_row(1, "pass_priority"),
        plays_outcome(
            2,
            admitted=[
                {"dsl_change": False, "added": [], "updated": [], "reprioritized": ["some-play"]}
            ],
        ),
    ]
    verdict = report.assess(records, "alice")
    assert len(verdict.open) == 1
    assert not verdict.satisfied


def test_no_decisions_is_satisfied_with_nothing_open():
    verdict = report.assess([], "alice")
    assert verdict.candidates == ()
    assert verdict.open == ()
    assert verdict.satisfied


def test_a_no_change_or_admit_for_another_seat_does_not_close_this_seats_candidate():
    records = [
        decision_row(1, "pass_priority"),
        plays_outcome(
            2,
            player="bob",
            admitted=[{"added": ["new-play"], "updated": [], "reprioritized": []}],
        ),
    ]
    verdict = report.assess(records, "alice")
    assert len(verdict.open) == 1
    assert not verdict.satisfied


# ======================================================================
# FD10 (a): manual chains and ad-hoc scripts that ran are candidates too,
# closed the same way an ordinary decision candidate is.
# ======================================================================


def staged(seq: int, script_id: str, *, seat: str = "alice") -> dict[str, Any]:
    return row(
        seq,
        SCRIPT_STAGED_ACTION,
        {"player": seat, "seat": seat, "id": script_id, "play": {}, "warnings": []},
    )


def test_a_manual_chain_is_a_candidate():
    records = [move(seq, "activate") for seq in range(1, 5)]
    candidates = report.manual_chain_candidates(records, "alice")
    assert len(candidates) == 1
    assert candidates[0].seqs == (1, 2, 3, 4)
    assert candidates[0].id == report.decision_candidate_id(1)


def test_manual_chain_candidates_join_assess_as_open():
    records = [move(seq, "activate") for seq in range(1, 5)]
    verdict = report.assess(records, "alice")
    assert len(verdict.candidates) == 1
    assert not verdict.satisfied


def test_a_no_change_by_decision_closes_a_manual_chain_candidate_too():
    records = [
        *(move(seq, "activate") for seq in range(1, 5)),
        plays_outcome(
            5,
            admitted=[],
            no_change=[{"candidate": report.decision_candidate_id(1), "reason": "fine as is"}],
        ),
    ]
    verdict = report.assess(records, "alice")
    assert verdict.open == ()
    assert verdict.satisfied


def test_a_short_run_of_manual_rows_is_not_a_candidate():
    records = [move(seq, "activate") for seq in range(1, 4)]
    assert report.manual_chain_candidates(records, "alice") == ()


def test_a_staged_and_run_adhoc_script_is_a_candidate():
    records = [
        staged(1, "adhoc-1-alice"),
        started(2, "adhoc-1-alice", "run-x"),
        script_row(3, audit.SCRIPT_RUN_ACTION, "adhoc-1-alice", "run-x", exit="resolved"),
    ]
    candidates = report.adhoc_script_candidates(records, "alice")
    assert len(candidates) == 1
    assert candidates[0].seqs == (1,)
    assert candidates[0].dsl == {"id": "adhoc-1-alice"}
    assert candidates[0].id == report.decision_candidate_id(1)


def test_a_staged_but_never_run_adhoc_script_is_not_a_candidate():
    records = [staged(1, "adhoc-1-alice")]
    assert report.adhoc_script_candidates(records, "alice") == ()


def test_adhoc_script_candidates_join_assess_and_are_closed_by_no_change():
    records = [
        staged(1, "adhoc-1-alice"),
        started(2, "adhoc-1-alice", "run-x"),
        script_row(3, audit.SCRIPT_RUN_ACTION, "adhoc-1-alice", "run-x", exit="resolved"),
    ]
    verdict = report.assess(records, "alice")
    # `decision_dsl.decision_rows` also turns the `script_run_started` row
    # itself into an ordinary `run_script` decision candidate - unrelated to,
    # and coexisting with, the ad-hoc-script candidate this test is about;
    # only the latter is asserted on by id, never the total count.
    adhoc_candidate = next(c for c in verdict.candidates if c.action == "adhoc_script")
    assert adhoc_candidate.seqs == (1,)
    assert adhoc_candidate.id in {c.id for c in verdict.open}
    assert not verdict.satisfied

    closed = [
        *records,
        plays_outcome(
            4,
            admitted=[],
            no_change=[{"candidate": report.decision_candidate_id(1), "reason": "fine as is"}],
        ),
    ]
    verdict2 = report.assess(closed, "alice")
    assert adhoc_candidate.id not in {c.id for c in verdict2.open}
