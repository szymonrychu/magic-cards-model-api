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

from magic_cards_model_api import audit, review
from magic_cards_model_api.playscripts import stats
from magic_cards_model_api.repository import ArchivedGame

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


def admit_result(*plays: tuple[str, str]) -> dict[str, Any]:
    """One `playscripts.admit.admit()` `"admitted"` result, in exactly the
    shape `_landing` builds it - `added`/`updated`/`reprioritized`, each a
    list of play ids - so these tests exercise `_admitted_counts` against the
    real producer's own shape rather than a label invented for the test (see
    `test_real_admit_...` below for the same claim proven against a real git
    checkout)."""
    result: dict[str, Any] = {"added": [], "updated": [], "reprioritized": []}
    for play_id, kind in plays:
        result[kind].append(play_id)
    return result


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
    "manual_calls": 0,
    "script_actions": 0,
    "adhoc_staged": 0,
    "adhoc_runs": 0,
    "manual_chains": (),
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
        admit_result(("play-a", "added"), ("play-b", "updated")),
        # `admit._landing` files a deleted play (play-d) under `updated`.
        admit_result(("play-c", "reprioritized"), ("play-d", "updated")),
    ]
    result = stats.game_play_stats([], GAME, SEAT, corpus=[], admitted=admitted)
    assert result["added"] == 1
    assert result["updated"] == 2
    assert result["reprioritized"] == 1
    assert result["added_ids"] == ("play-a",)
    assert result["updated_ids"] == ("play-b", "play-d")


def test_admitted_ids_are_deduped_across_several_admits_of_one_game():
    admitted = [
        admit_result(("play-a", "added")),
        admit_result(("play-a", "updated"), ("play-b", "updated")),
        admit_result(("play-b", "updated")),
    ]
    result = stats.game_play_stats([], GAME, SEAT, corpus=[], admitted=admitted)
    assert result["added"] == 0
    assert result["updated"] == 2
    assert result["added_ids"] == ()
    assert result["updated_ids"] == ("play-a", "play-b")


def test_an_admit_entry_with_no_plays_is_evidence_only_and_counts_nothing():
    # `admit()` admits evidence-only handovers too (see `admit.py`'s own
    # docstring): empty `added`/`updated`/`reprioritized`, not something this
    # function invents a fallback for.
    admitted = [
        {
            "sha": "abc",
            "dsl_change": False,
            "files": ["evidence.json"],
            "added": [],
            "updated": [],
            "reprioritized": [],
        }
    ]
    result = stats.game_play_stats([], GAME, SEAT, corpus=[], admitted=admitted)
    assert result["added"] == 0
    assert result["updated"] == 0
    assert result["reprioritized"] == 0


def test_real_admit_result_added_updated_and_reprioritized_are_classified_by_admit_py(
    tmp_path, monkeypatch
):
    """The finding this reproduces: `_admitted_counts` used to trust a
    caller-supplied `change` label that nothing in the codebase produced.
    This drives `playscripts.admit.admit()` against a real git checkout -
    the one place the add/update/reprioritize rule is actually evaluated -
    and feeds its own, unmodified result straight into `game_play_stats`."""
    import copy
    import json

    import guides_git_testkit as gitkit

    from magic_cards_model_api.playscripts import admit as admit_mod

    gitkit.isolate_git(tmp_path, monkeypatch)
    monkeypatch.delenv("MTG_EVIDENCE_DIR", raising=False)
    bare = gitkit.seed_guides(tmp_path)
    root = gitkit.clone(bare, tmp_path / "guides")
    slug = gitkit.SLUG

    def write(rel: str, doc: dict[str, Any]) -> None:
        (root / rel).write_text(gitkit.dump(doc), encoding="utf-8")

    def read(rel: str) -> dict[str, Any]:
        return json.loads((root / rel).read_text(encoding="utf-8"))

    # A brand-new play file: "added".
    new_play = copy.deepcopy(gitkit.FETCH_SURVEIL) | {
        "id": "fetch-late",
        "when": {"whose_turn": "theirs", "steps": ["upkeep"], "stack": "empty"},
    }
    write(f"decks/{slug}/plays/fetch-late.json", new_play)
    first = admit_mod.admit(root, slug, "game-1")
    assert first["result"] == "admitted", first

    # A real content change (beyond priority) on an existing play: "updated".
    doc = read(f"decks/{slug}/plays/fetch-untapped-shock.json")
    doc["intent"] = "Changed for this test."
    write(f"decks/{slug}/plays/fetch-untapped-shock.json", doc)
    second = admit_mod.admit(root, slug, "game-2")
    assert second["result"] == "admitted", second

    # A priority-only move on the surveil play: "reprioritized".
    doc = read(f"decks/{slug}/plays/fetch-surveil-land.json")
    doc["priority"] = doc["priority"] + 1
    write(f"decks/{slug}/plays/fetch-surveil-land.json", doc)
    third = admit_mod.admit(root, slug, "game-3")
    assert third["result"] == "admitted", third

    result = stats.game_play_stats([], GAME, SEAT, corpus=[], admitted=[first, second, third])
    assert result["added_ids"] == ("fetch-late",)
    assert result["updated_ids"] == ("fetch-untapped-shock",)
    assert result["reprioritized"] == 1
    assert result["added"] == 1
    assert result["updated"] == 1


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
    admitted = [admit_result(("frog-attack", "updated")), admit_result(("new-play", "added"))]
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
        "manual_calls": 1,
        "script_actions": 0,
        "adhoc_staged": 0,
        "adhoc_runs": 0,
        "manual_chains": (),
    }


# ======================================================================
# FD7 (5): reviewer `plays_outcome` rows are counted in `play_stats`'
# totals AND separately (gap 4), `plays_total` reflects whatever corpus the
# caller hands `build_review` at review time, and `dsl_coverage` aggregates
# `{kind: {count, unexpressible, labels}}` across a game's own decisions.
#
# `review.build_review` is exercised directly, `game=None` and no frame
# views - the same undecodable-branch shape `test_review.py`'s own
# `test_an_undecodable_game_with_no_recorded_ending_says_so_rather_than_
# guessing` builds by hand, here with a synthetic `plays_outcome` row no HTTP
# route can currently post (`schemas.PlaysOutcomeIn.role` is `seat`/`advisor`
# only - the driver side of filing a reviewer's own row is FD6/FD5's, this
# is only what `review.py` does with one once it exists in the ledger).
# ======================================================================


def decision(
    seq, action, *, player="alice", turn=3, step="precombat_main", active_player="alice", **detail
):
    return {
        "seq": seq,
        "action": action,
        "detail": {"player": player, "allowed": True, **detail},
        "turn": turn,
        "step": step,
        "active_player": active_player,
    }


def plays_outcome_row(seq, player="alice", **detail):
    return {
        "seq": seq,
        "action": audit.PLAYS_OUTCOME_ACTION,
        "detail": {"player": player, **detail},
        "turn": 3,
    }


def archived(players=("alice", "bob"), game_id="g1"):
    return ArchivedGame(
        game_id=game_id,
        game=None,
        admin_key="k",
        seat_uids={f"u-{p}": p for p in players},
        created_at="2026-01-01T00:00:00+00:00",
        deleted_at="2026-01-01T00:05:00+00:00",
        unloadable=None,
    )


def added(play_id: str) -> dict[str, Any]:
    return {"added": [play_id], "updated": [], "reprioritized": []}


def test_reviewer_plays_outcome_rows_count_in_the_totals_and_separately():
    entries = [
        plays_outcome_row(1, player="alice", role="seat", admitted=[added("seat-play")]),
        plays_outcome_row(2, player="alice", role="reviewer", admitted=[added("reviewer-play")]),
    ]
    body = review.build_review(archived(), entries)
    stats = body["play_stats"]["alice"]
    assert stats["added"] == 2
    assert sorted(stats["added_ids"]) == ["reviewer-play", "seat-play"]
    assert stats["reviewer"]["added"] == 1
    assert stats["reviewer"]["added_ids"] == ("reviewer-play",)


def test_a_seat_with_no_reviewer_rows_gets_reviewer_zeros_not_a_missing_key():
    entries = [plays_outcome_row(1, player="alice", role="seat", admitted=[added("seat-play")])]
    body = review.build_review(archived(), entries)
    stats = body["play_stats"]["alice"]
    assert stats["reviewer"] == {
        "added": 0,
        "updated": 0,
        "reprioritized": 0,
        "added_ids": (),
        "updated_ids": (),
    }


def test_a_role_less_legacy_row_is_never_counted_as_a_reviewer_row():
    """Every row before FD7 carries no `role` at all - `_decisions_and_
    play_stats` must not mistake that absence for `"reviewer"`."""
    entries = [plays_outcome_row(1, player="alice", admitted=[added("legacy-play")])]
    body = review.build_review(archived(), entries)
    stats = body["play_stats"]["alice"]
    assert stats["added"] == 1
    assert stats["reviewer"]["added"] == 0


def test_a_reviewer_row_for_another_seat_is_not_counted_here():
    entries = [plays_outcome_row(1, player="bob", role="reviewer", admitted=[added("bobs-play")])]
    body = review.build_review(archived(), entries)
    assert body["play_stats"]["alice"]["reviewer"]["added"] == 0
    assert body["play_stats"]["bob"]["reviewer"]["added"] == 1


def test_plays_total_reflects_the_corpus_handed_to_build_review_at_review_time():
    corpus = {"p1": {"moves": [{"kind": "pass"}]}, "p2": {"moves": []}}
    body = review.build_review(archived(players=("alice",)), [], corpora={"alice": corpus})
    stats = body["play_stats"]["alice"]
    assert stats["plays_total"] == 2
    assert stats["plays_move_bearing"] == 1
    assert stats["plays_advice"] == 1


# -- dsl_coverage -------------------------------------------------------------


def test_dsl_coverage_aggregates_count_unexpressible_and_labels_by_kind():
    entries = [
        decision(1, "pass_priority"),
        decision(2, "mulligan_declared", keep=True),
        decision(3, "concede"),
    ]
    body = review.build_review(archived(players=("alice",)), entries)
    coverage = body["dsl_coverage"]
    assert coverage["pass"] == {"count": 1, "unexpressible": 0, "labels": []}
    assert coverage["mulligan"] == {
        "count": 1,
        "unexpressible": 1,
        "labels": ["mulligan:malformed"],
    }
    assert coverage["concede"] == {"count": 1, "unexpressible": 1, "labels": ["concede"]}


def test_dsl_coverage_is_empty_for_a_game_with_no_decisions():
    body = review.build_review(archived(players=("alice",)), [])
    assert body["dsl_coverage"] == {}


# ======================================================================
# FD10 (c): `manual_calls`/`script_actions` (manual vs script-driven action
# rows), `adhoc_staged`/`adhoc_runs`, and `manual_chains` (a run of
# `MANUAL_CHAIN_MIN` or more consecutive manual rows, within one turn and
# step, with no opponent action between).
# ======================================================================

from magic_cards_model_api.records import SCRIPT_STAGED_ACTION  # noqa: E402


def script_driven_move(seq: int, *, seat: str = SEAT, allowed: bool | None = True, **extra: Any):
    """A committing/refused row `seat` made *through a script run* - carries
    the runner's own reasoning suffix (`runtime._suffixed`)."""
    return move(
        seq,
        "activate",
        seat=seat,
        allowed=allowed,
        reasoning="r | script loop@abcdef01 move 1/1 (activate)",
        **extra,
    )


def staged_row(seq: int, script_id: str, *, seat: str = SEAT) -> dict[str, Any]:
    return row(
        seq,
        SCRIPT_STAGED_ACTION,
        {"player": seat, "seat": seat, "id": script_id, "play": {}, "warnings": []},
    )


def test_manual_and_script_driven_counts_split_a_mixed_ledger():
    records = [move(1, "cast", card="a"), script_driven_move(2), move(3, "activate")]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    assert result["manual_calls"] == 2
    assert result["script_actions"] == 1


def test_manual_calls_ignores_other_seats_and_non_action_rows():
    records = [
        move(1, "cast", seat="bob"),
        offered(2, "fetch-shock", "run-a"),
        move(3, "cast", allowed=False),
    ]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    assert result["manual_calls"] == 1
    assert result["script_actions"] == 0


def test_script_driven_public_helper_reads_the_run_window_too():
    records = [
        started(1, "fetch-shock", "run-a"),
        move(2, "cast", card="a"),
        script_row(
            3, audit.SCRIPT_RUN_ACTION, "fetch-shock", "run-a", exit="resolved", moves_done=1
        ),
        move(4, "cast", card="b"),
    ]
    assert stats.script_driven(records[1], records) is True
    # Outside every run's own span (before the start, or after the finish):
    # never script-driven off the window alone.
    assert stats.script_driven(records[3], records) is False


def test_adhoc_staged_and_adhoc_runs_are_counted_by_id_prefix_alone():
    records = [
        staged_row(1, "adhoc-1-alice"),
        started(2, "adhoc-1-alice", "run-x"),
        script_row(
            3, audit.SCRIPT_RUN_ACTION, "adhoc-1-alice", "run-x", exit="resolved", moves_done=2
        ),
        # A registered (non-adhoc) run must never count toward `adhoc_runs`.
        started(4, "fetch-shock", "run-y"),
        script_row(5, audit.SCRIPT_RUN_ACTION, "fetch-shock", "run-y", exit="resolved"),
    ]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    assert result["adhoc_staged"] == 1
    assert result["adhoc_runs"] == 1


def test_three_manual_rows_are_not_yet_a_chain():
    records = [move(seq, "activate") for seq in range(1, 4)]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    assert result["manual_chains"] == ()


def test_four_consecutive_manual_rows_form_one_chain():
    records = [move(seq, "activate") for seq in range(1, 5)]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    assert result["manual_chains"] == (
        {
            "seqs": (1, 2, 3, 4),
            "kinds": ("activate", "activate", "activate", "activate"),
            "turn": 3,
            "step": None,
        },
    )


def test_a_chain_broken_by_an_opponent_action_splits_into_two_runs():
    records = [
        move(1, "activate"),
        move(2, "activate"),
        move(3, "activate", seat="bob"),
        move(4, "activate"),
        move(5, "activate"),
        move(6, "activate"),
        move(7, "activate"),
    ]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    # The first run (seqs 1, 2) never reaches `MANUAL_CHAIN_MIN` and is
    # dropped; only the second (4-7, four rows) survives as a chain.
    assert result["manual_chains"] == (
        {"seqs": (4, 5, 6, 7), "kinds": ("activate",) * 4, "turn": 3, "step": None},
    )


def test_a_script_driven_row_between_two_manual_ones_breaks_the_chain():
    records = [
        move(1, "activate"),
        move(2, "activate"),
        script_driven_move(3),
        move(4, "activate"),
        move(5, "activate"),
    ]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    assert result["manual_chains"] == ()


def test_finding_seq_191_shape_25_manual_activations_in_one_step_is_one_chain_of_25():
    """The evidence FD9/FD10 are built on: local game 3, finding seq 191, a
    seat spent about 25 MCP calls hand-driving a mana loop to reach X=8. A
    synthetic replay of that shape - 25 consecutive manual activations, one
    turn and step - must read back as exactly one chain of 25, not several
    smaller ones or none at all."""
    records = [move(seq, "activate") for seq in range(1, 26)]
    result = stats.game_play_stats(records, GAME, SEAT, corpus=[], admitted=[])
    assert len(result["manual_chains"]) == 1
    chain = result["manual_chains"][0]
    assert chain["seqs"] == tuple(range(1, 26))
    assert chain["kinds"] == ("activate",) * 25
    assert chain["turn"] == 3
    assert chain["step"] is None


def test_dsl_coverage_is_public_and_callable_on_the_decisions_dict_directly():
    """FD5 exposes this across games through an ops endpoint; it must not
    need a whole `build_review` call to be useful there."""
    decisions = {
        "alice": [
            decision(1, "pass_priority")["detail"] | {"action": "pass", "unexpressible": None}
        ]
    }
    coverage = review.dsl_coverage(decisions)
    assert coverage == {"pass": {"count": 1, "unexpressible": 0, "labels": []}}
