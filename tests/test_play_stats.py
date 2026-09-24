"""DR1: `playscripts.stats.game_play_stats` - per-game play statistics, a
pure aggregation over three already-computed inputs: this seat's own ledger
rows (offer outcomes, reused from `report._offer_outcomes` rather than
reimplemented), the registered corpus for this game, and the admitted
entries a post-game handover produced.

Row-building helpers mirror `test_playscript_report.py`'s own local
`offered`/`started`/`move` shape - the real `audit.entries()` shape
`report._offer_outcomes` itself is tested against."""

from __future__ import annotations

from typing import Any

from magic_cards_model_api import audit
from magic_cards_model_api.playscripts import stats

GAME = "g1"
SEAT = "alice"


def row(seq: int, action: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {"seq": seq, "action": action, "detail": detail, "turn": 3}


def move(seq: int, action: str, *, seat: str = SEAT, allowed: bool | None = True, **extra):
    return row(seq, action, {"player": seat, "allowed": allowed, "reasoning": "r", **extra})


def script_row(
    seq: int,
    action: str,
    script_id: str,
    run_id: str,
    *,
    seat: str = SEAT,
    game_id: str = GAME,
    turn: int = 3,
    step: str = "precombat_main",
    **extra: Any,
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


def play_doc(play_id: str, *, moves: bool = True) -> dict[str, Any]:
    return {"id": play_id, "moves": [{"kind": "cast"}] if moves else []}


def admitted_row(play_id: str, change: str, *, game_id: str = GAME) -> dict[str, Any]:
    return {"game_id": game_id, "play_id": play_id, "change": change}


ZERO_STATS = {
    "plays_total": 0,
    "plays_move_bearing": 0,
    "plays_advice": 0,
    "offered": 0,
    "used": 0,
    "denied": 0,
    "passed_over": 0,
    "added": 0,
    "updated": 0,
    "reprioritized": 0,
    "added_ids": (),
    "updated_ids": (),
}


def test_empty_game_is_all_zeros_not_missing_keys():
    result = stats.game_play_stats([], GAME, SEAT, corpus=[], admitted=[])
    assert result == ZERO_STATS
    assert set(result) == set(ZERO_STATS)


def test_plays_total_move_bearing_and_advice_come_from_the_corpus():
    corpus = [play_doc("a"), play_doc("b"), play_doc("c", moves=False)]
    result = stats.game_play_stats([], GAME, SEAT, corpus=corpus, admitted=[])
    assert result["plays_total"] == 3
    assert result["plays_move_bearing"] == 2
    assert result["plays_advice"] == 1


def test_corpus_as_a_mapping_of_id_to_doc_is_accepted():
    corpus = {"a": play_doc("a"), "b": play_doc("b", moves=False)}
    result = stats.game_play_stats([], GAME, SEAT, corpus=corpus, admitted=[])
    assert result["plays_total"] == 2
    assert result["plays_move_bearing"] == 1
    assert result["plays_advice"] == 1


# -- offer outcomes: used / denied / passed_over / offered ----------------------


def test_taken_offer_is_used():
    records = [offered(1, "fetch-shock", "run-a"), started(2, "fetch-shock", "run-a")]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    assert result["offered"] == 1
    assert result["used"] == 1
    assert result["denied"] == 0
    assert result["passed_over"] == 0


def test_declined_offer_is_denied():
    records = [offered(1, "frog-attack", "run-c"), move(2, "cast", card="Fatal Push")]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    assert result["offered"] == 1
    assert result["used"] == 0
    assert result["denied"] == 1
    assert result["passed_over"] == 0


def test_sibling_taken_is_passed_over_not_denied():
    records = [
        offered(1, "fetch-shock", "run-a"),
        offered(2, "fetch-surveil", "run-b"),
        started(3, "fetch-surveil", "run-b"),
    ]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    assert result["offered"] == 2
    assert result["used"] == 1
    assert result["passed_over"] == 1
    assert result["denied"] == 0


def test_unanswered_offer_only_counts_as_offered():
    records = [offered(1, "unanswered", "run-e")]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    assert result["offered"] == 1
    assert result["used"] == 0
    assert result["denied"] == 0
    assert result["passed_over"] == 0


def test_offers_are_scoped_to_the_seat_and_game():
    records = [
        offered(1, "fetch-shock", "run-a", seat="bob"),
        offered(2, "fetch-shock", "run-b", game_id="another-game"),
        offered(3, "fetch-shock", "run-c"),
    ]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    assert result["offered"] == 1


def test_offer_outcomes_reuse_report_offer_outcomes_not_a_reimplementation():
    from magic_cards_model_api.playscripts import report

    assert stats.offer_outcomes is report._offer_outcomes


# -- admitted entries: added / updated / reprioritized --------------------------


def test_added_updated_reprioritized_counts_and_ids():
    admitted = [
        admitted_row("play-a", "added"),
        admitted_row("play-b", "updated"),
        admitted_row("play-c", "reprioritized"),
        admitted_row("play-d", "deleted"),
    ]
    result = stats.game_play_stats([], GAME, SEAT, corpus=[], admitted=admitted)
    assert result["added"] == 1
    assert result["updated"] == 2  # deletions counted as updated
    assert result["reprioritized"] == 1
    assert result["added_ids"] == ("play-a",)
    assert result["updated_ids"] == ("play-b", "play-d")


def test_admitted_ids_are_deduped_across_several_admits_of_one_game():
    admitted = [
        admitted_row("play-a", "added"),
        admitted_row("play-a", "updated"),
        admitted_row("play-b", "updated"),
        admitted_row("play-b", "updated"),
    ]
    result = stats.game_play_stats([], GAME, SEAT, corpus=[], admitted=admitted)
    assert result["added"] == 0
    assert result["updated"] == 2
    assert result["added_ids"] == ()
    assert result["updated_ids"] == ("play-a", "play-b")


def test_admitted_entries_for_other_games_are_ignored():
    admitted = [
        admitted_row("play-a", "added", game_id="another-game"),
        admitted_row("play-b", "added"),
    ]
    result = stats.game_play_stats([], GAME, SEAT, corpus=[], admitted=admitted)
    assert result["added"] == 1
    assert result["added_ids"] == ("play-b",)


# -- everything together ---------------------------------------------------------


def test_a_full_game_adds_up():
    corpus = [
        play_doc("fetch-shock"),
        play_doc("frog-attack"),
        play_doc("advice-only", moves=False),
    ]
    records = [
        offered(1, "fetch-shock", "run-a"),
        started(2, "fetch-shock", "run-a"),
        offered(3, "frog-attack", "run-c"),
        move(4, "cast", card="Fatal Push"),
    ]
    admitted = [admitted_row("frog-attack", "updated"), admitted_row("new-play", "added")]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=corpus, admitted=admitted)
    assert result == {
        "plays_total": 3,
        "plays_move_bearing": 2,
        "plays_advice": 1,
        "offered": 2,
        "used": 1,
        "denied": 1,
        "passed_over": 0,
        "added": 1,
        "updated": 1,
        "reprioritized": 0,
        "added_ids": ("new-play",),
        "updated_ids": ("frog-attack",),
    }
