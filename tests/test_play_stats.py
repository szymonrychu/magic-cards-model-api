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
    }
