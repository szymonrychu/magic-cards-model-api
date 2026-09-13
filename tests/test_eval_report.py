"""The scorecard: `scripts/eval_report.py` over the `records` and `games`
tables.

Every metric is checked against a ledger built here, row by row, in the exact
shape `audit.record` writes - so a change to that shape breaks these tests
rather than quietly changing what the scorecard reports. The ledger is
written to disk as a fixture too, because the report has to be readable
without a database for anyone to run it against a captured run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import add_card, state

from scripts import eval_report

FIXTURE = Path(__file__).parent / "fixtures" / "eval" / "ledger.json"


# -- ledger construction -----------------------------------------------------


def row(seq: int, action: str, **detail: Any) -> dict[str, Any]:
    """One `records` row, shaped exactly as `audit.record` writes it."""
    reasoning = detail.pop("_reasoning", None)
    turn = detail.pop("_turn", 1)
    return {
        "seq": seq,
        "payload": {
            "ts": "2026-09-10T00:00:00+00:00",
            "action": action,
            "detail": detail,
            "turn": turn,
            "step": "precombat_main",
            "phase": "precombat_main",
            "stack_size": 0,
            "snapshot": None,
            "reasoning": reasoning,
            "legal_actions_before": None,
            "legal_actions_for": None,
        },
    }


def game(
    game_id: str,
    rows: list[dict[str, Any]],
    *,
    players: tuple[str, str] = ("alice", "bob"),
    archived_at: str | None = None,
) -> eval_report.GameRows:
    return eval_report.GameRows(
        game_id=game_id,
        players=list(players),
        created_at="2026-09-10T00:00:00+00:00",
        archived_at=archived_at,
        rows=rows,
    )


def finished_game(
    game_id: str,
    winner: str | None,
    *,
    turns: int = 7,
    extra: list[dict[str, Any]] | None = None,
) -> eval_report.GameRows:
    rows = [row(1, "create_game", players=["alice", "bob"])]
    rows.extend(extra or [])
    rows.append(
        row(
            len(rows) + 1,
            "game_over",
            winner=winner,
            drawn=winner is None,
            note=f"{winner} wins" if winner else "the game is a draw",
            _turn=turns,
        )
    )
    return game(game_id, rows)


# -- outcomes ----------------------------------------------------------------


def test_finish_rate_counts_the_games_that_actually_ended():
    games = [
        finished_game("g1", "alice"),
        finished_game("g2", "bob"),
        game("g3", [row(1, "create_game"), row(2, "cast", player="alice", allowed=True)]),
    ]

    report = eval_report.build_report(games)

    assert report["games"] == 3
    assert report["finished"] == 2
    assert report["finish_rate"]["successes"] == 2
    assert report["finish_rate"]["trials"] == 3
    assert report["unfinished"] == ["g3"]


def test_the_winner_comes_from_the_ledgers_own_game_over_row():
    """`review.ending_of`'s witness: the row written the moment `Game.is_over`
    first read true. A scorecard that inferred the result from final life
    totals would be doing the arithmetic `review.py` refuses to do."""
    report = eval_report.build_report([finished_game("g1", "alice"), finished_game("g2", "alice")])

    assert report["seats"]["alice"]["wins"] == 2
    assert report["seats"]["bob"]["wins"] == 0
    assert report["seats"]["bob"]["losses"] == 2


def test_a_draw_is_neither_a_win_nor_a_loss():
    report = eval_report.build_report([finished_game("g1", None)])

    alice = report["seats"]["alice"]
    assert (alice["wins"], alice["losses"], alice["draws"]) == (0, 0, 1)
    assert report["seats"]["bob"]["draws"] == 1


def test_turns_to_end_reads_the_game_over_rows_own_turn():
    report = eval_report.build_report(
        [finished_game("g1", "alice", turns=5), finished_game("g2", "bob", turns=11)]
    )

    assert report["turns_to_end"]["n"] == 2
    assert report["turns_to_end"]["mean"] == 8.0
    assert report["turns_to_end"]["min"] == 5
    assert report["turns_to_end"]["max"] == 11


def test_an_unfinished_game_contributes_no_turn_count():
    report = eval_report.build_report(
        [finished_game("g1", "alice", turns=5), game("g2", [row(1, "create_game")])]
    )

    assert report["turns_to_end"]["n"] == 1


# -- per-seat activity -------------------------------------------------------


def test_actions_per_game_counts_only_a_seats_own_play():
    plays = [
        row(2, "cast", player="alice", allowed=True),
        row(3, "play_land", player="alice", allowed=True),
        row(4, "pass_priority", player="bob", allowed=True),
        # Board setup and bookkeeping are not plays and never count.
        row(5, "add_card", player="alice", card="Forest"),
        row(6, "patch_card", card_id="x"),
    ]
    report = eval_report.build_report([finished_game("g1", "alice", extra=plays)])

    assert report["seats"]["alice"]["actions"] == 2
    assert report["seats"]["alice"]["actions_per_game"] == 2.0
    assert report["seats"]["bob"]["actions"] == 1


def test_an_unrecognised_action_is_named_rather_than_silently_dropped():
    """A scorecard that quietly ignores a row it does not know is a scorecard
    that under-reports the day a new action is audited."""
    report = eval_report.build_report(
        [finished_game("g1", "alice", extra=[row(2, "teleport", player="alice", allowed=True)])]
    )

    assert report["unclassified_actions"] == {"teleport": 1}


def test_a_plan_declaration_is_never_scored():
    """`review.py` refuses to grade a declaration, and this refuses with it:
    a `plan_declared` row is not an action, not an attempt, and carries no
    reasoning coverage of its own."""
    plans = [
        row(2, "plan_declared", player="alice", turns_taken=1),
        row(3, "cast", player="alice", allowed=True, _reasoning="because"),
    ]
    report = eval_report.build_report([finished_game("g1", "alice", extra=plans)])

    alice = report["seats"]["alice"]
    assert alice["actions"] == 1
    assert alice["attempts"] == 1
    assert alice["reasoning"]["successes"] == 1


# -- reasoning ---------------------------------------------------------------


def test_reasoning_coverage_is_the_share_of_attempts_that_said_why():
    rows = [
        row(2, "cast", player="alice", allowed=True, _reasoning="removing the blocker"),
        row(3, "cast", player="alice", allowed=False, reason="no", _reasoning="trying anyway"),
        row(4, "play_land", player="alice", allowed=True),
        row(5, "play_land", player="alice", allowed=True, _reasoning="   "),
    ]
    report = eval_report.build_report([finished_game("g1", "alice", extra=rows)])

    coverage = report["seats"]["alice"]["reasoning"]
    assert coverage["trials"] == 4
    assert coverage["successes"] == 2, "blank and whitespace-only are not reasons"
    assert coverage["point"] == pytest.approx(0.5)


def test_pass_shaped_rows_are_reported_separately():
    """`records.require_reasoning` exempts a pass, so a seat that reasons
    about everything else still scores below 100% overall - the two numbers
    are reported side by side rather than one hiding the other."""
    rows = [
        row(2, "pass_priority", player="alice", allowed=True),
        row(3, "pass_priority", player="alice", allowed=True),
        row(4, "cast", player="alice", allowed=True, _reasoning="the plan"),
    ]
    report = eval_report.build_report([finished_game("g1", "alice", extra=rows)])

    alice = report["seats"]["alice"]
    assert alice["reasoning"]["point"] == pytest.approx(1 / 3)
    assert alice["reasoning_beyond_passes"]["trials"] == 1
    assert alice["reasoning_beyond_passes"]["point"] == pytest.approx(1.0)


# -- refusals, split ---------------------------------------------------------


def test_refusal_rate_is_over_attempts_the_engine_ruled_on():
    rows = [
        row(2, "cast", player="alice", allowed=True),
        row(3, "cast", player="alice", allowed=False, reason="pool {} cannot pay {R}", stale=False),
        row(4, "cast", player="alice", allowed=False, reason="not your priority", stale=True),
        # A paused action is neither allowed nor refused - it is a question.
        row(5, "advance", player="alice", allowed=None, paused=True),
    ]
    report = eval_report.build_report([finished_game("g1", "alice", extra=rows)])

    refusals = report["seats"]["alice"]["refusals"]
    assert refusals["trials"] == 3, "the paused row is not an attempt the engine ruled on"
    assert refusals["successes"] == 2


def test_refusals_split_into_rules_errors_and_stale_races():
    """`offered: False` on the two rules rows, and it is not decoration: a
    refusal is the seat's own misreading only once the ledger says this api
    had *not* offered the action (`audit._with_offered`). These rows carried
    `stale: False` alone when this test was written, and counting that as a
    rules error is what let an api refusing an action it had itself
    enumerated be scored as the agent misreading the rules.
    """
    rows = [
        row(
            2,
            "cast",
            player="alice",
            allowed=False,
            reason="cannot pay",
            stale=False,
            offered=False,
        ),
        row(
            3,
            "cast",
            player="alice",
            allowed=False,
            reason="cannot pay",
            stale=False,
            offered=False,
        ),
        row(4, "attack", player="alice", allowed=False, reason="not pending", stale=True),
    ]
    report = eval_report.build_report([finished_game("g1", "alice", extra=rows)])

    split = report["seats"]["alice"]["refusal_split"]
    assert split["rules"] == 2
    assert split["stale"] == 1
    assert split["unknown"] == 0
    assert split["unchecked"] == 0
    assert report["stale_split"] == "measured"
    assert report["offer_split"] == "measured"


def test_an_absent_stale_flag_is_reported_as_unknown_not_as_a_rules_error():
    """P4 adds `detail.stale`; a ledger recorded before it landed carries
    none. Counting those as rules errors would invent an agent defect out of
    a missing field, so they are counted as their own thing and the report
    says the split could not be made."""
    rows = [
        row(2, "cast", player="alice", allowed=False, reason="cannot pay"),
        row(3, "cast", player="alice", allowed=False, reason="not your priority"),
    ]
    report = eval_report.build_report([finished_game("g1", "alice", extra=rows)])

    split = report["seats"]["alice"]["refusal_split"]
    assert (split["rules"], split["stale"], split["unknown"]) == (0, 0, 2)
    assert report["stale_split"] == "unknown"


def test_a_partly_flagged_ledger_says_so():
    rows = [
        row(2, "cast", player="alice", allowed=False, reason="cannot pay", stale=False),
        row(3, "cast", player="alice", allowed=False, reason="cannot pay"),
    ]
    report = eval_report.build_report([finished_game("g1", "alice", extra=rows)])

    assert report["stale_split"] == "partial"


# -- intervals ---------------------------------------------------------------


def test_every_rate_carries_an_interval():
    report = eval_report.build_report([finished_game("g1", "alice"), finished_game("g2", "bob")])

    rates = list(eval_report.every_rate(report))
    assert rates
    for rate in rates:
        # The posterior mean, not the observed point: 0 successes out of 1 is
        # a real observation whose 95% interval does not reach 0, and a
        # report that clamped the interval to contain it would be widening
        # the bounds to flatter the number.
        assert rate["low"] <= rate["mean"] <= rate["high"], rate
        assert 0.0 <= rate["low"] <= 1.0 and 0.0 <= rate["high"] <= 1.0, rate
        assert rate["point"] is None or 0.0 <= rate["point"] <= 1.0, rate


def test_a_rate_with_no_trials_is_reported_as_no_data_rather_than_zero():
    report = eval_report.build_report([game("g1", [row(1, "create_game")])])

    refusals = report["seats"]["alice"]["refusals"] if "alice" in report["seats"] else None
    assert refusals is None or refusals["trials"] == 0


def test_the_interval_narrows_as_the_sample_grows():
    twenty = eval_report.beta_interval(10, 20)
    fifty = eval_report.beta_interval(25, 50)

    assert twenty["half_width"] == pytest.approx(0.20, abs=0.03)
    assert fifty["half_width"] == pytest.approx(0.14, abs=0.03)
    assert fifty["half_width"] < twenty["half_width"]


def test_the_same_ledger_reports_the_same_interval_twice():
    """Sampled, like `mtg-decks/scripts/gauntlet_ev.py`'s own `_beta_ci95` -
    but seeded, so a rerun of one gauntlet does not move the number it is
    being compared against."""
    games = [finished_game("g1", "alice"), finished_game("g2", "bob")]

    first = eval_report.build_report(games)
    second = eval_report.build_report(games)

    assert first == second


# -- what gets printed -------------------------------------------------------


def test_the_report_prints_an_interval_and_never_a_bare_percentage():
    report = eval_report.build_report([finished_game("g1", "alice"), finished_game("g2", "bob")])

    text = eval_report.render(report)

    figures = re.compile(r"\d[\d.]*\s*%")
    for line in text.splitlines():
        if figures.search(line):
            assert "+/-" in line and "[" in line, f"a bare percentage: {line}"


def test_the_report_states_its_own_signal_sizing():
    text = eval_report.render(
        eval_report.build_report([finished_game(f"g{i}", "alice") for i in range(10)])
    )

    assert "signal sizing" in text.lower()
    assert "20 games" in text
    assert "200" in text, "an A/B of a 10pp change needs roughly 200 games per arm"
    assert "10 games" in text, "the run's own size has to be in the sentence"


def test_the_report_says_when_the_stale_split_could_not_be_made():
    text = eval_report.render(
        eval_report.build_report(
            [finished_game("g1", "alice", extra=[row(2, "cast", player="alice", allowed=False)])]
        )
    )

    assert "stale" in text.lower()
    assert "unknown" in text.lower()


def test_the_report_names_the_unfinished_games():
    text = eval_report.render(
        eval_report.build_report([game("g-unfinished", [row(1, "create_game")])])
    )

    assert "g-unfinished" in text


def test_an_unfinished_game_reports_the_edge_that_cut_it_off():
    """`finished 3/10` must never be readable as "the other 7 lost" - a run
    that hit its own `--max-seconds` bound mid-game is not a result, it is
    the harness giving up on watching. `watch_game.watch`'s own edge
    (`timeout`/`stalled`/`report`) is threaded through so the report says
    which one ended each unfinished game, instead of leaving a reader to
    guess whether it is still running, stuck, or lost."""
    unfinished = game("g3", [row(1, "create_game"), row(2, "cast", player="alice", allowed=True)])
    edges = {"g3": {"edge": "timeout", "game": "g3", "seq": 7, "turn": 8}}

    report = eval_report.build_report([unfinished], edges=edges)

    assert report["unfinished_detail"]["g3"]["edge"] == "timeout"
    assert report["unfinished_detail"]["g3"]["turn"] == 8


def test_an_unfinished_game_with_no_edge_given_says_so_rather_than_guessing():
    unfinished = game("g3", [row(1, "create_game")])

    report = eval_report.build_report([unfinished])

    assert report["unfinished_detail"]["g3"]["edge"] == "unknown"


def test_a_finished_game_carries_no_unfinished_detail():
    report = eval_report.build_report(
        [finished_game("g1", "alice")], edges={"g1": {"edge": "game_over"}}
    )

    assert "g1" not in report["unfinished_detail"]


def test_the_rendered_report_names_each_unfinished_games_own_edge():
    unfinished = game("g3", [row(1, "create_game"), row(2, "cast", player="alice", allowed=True)])
    edges = {"g3": {"edge": "timeout", "game": "g3", "seq": 7, "turn": 8}}

    text = eval_report.render(eval_report.build_report([unfinished], edges=edges))

    assert "g3" in text
    assert "timeout" in text
    assert "turn 8" in text


def test_the_rendered_report_says_unfinished_is_not_a_loss():
    """The one sentence that keeps a reader from misreading a low finish
    rate as a bad win rate for whichever seat did not finish."""
    unfinished = game("g3", [row(1, "create_game")])

    text = eval_report.render(eval_report.build_report([unfinished]))

    assert "not scored as a loss" in text


# -- loading -----------------------------------------------------------------


def test_the_fixture_ledger_is_committed_and_reports_cleanly():
    """The report must be runnable against a captured run with no database,
    which is also how a reader checks a number this file asserts."""
    games = eval_report.load_ledger_file(FIXTURE)

    report = eval_report.build_report(games)

    assert report["games"] == len(games) > 0
    assert eval_report.render(report)


def test_it_reads_the_games_and_records_tables_through_the_repository(client, game_fixture):
    """The real path: a game played through the api, read back out of the
    same two tables the scorecard reads in production."""
    add_card(client, game_fixture, "Forest", "alice", "hand")
    forest = state(client, game_fixture)["hands"]["alice"][0]["id"]
    played = client.post(
        f"/api/games/{game_fixture}/actions/play-land",
        json={"player": "alice", "card_id": forest, "reasoning": "ramp"},
    )
    assert played.json()["allowed"] is True, played.json()

    from magic_cards_model_api.store import store

    games = eval_report.ledger_from_store(store, game_ids=[game_fixture])
    report = eval_report.build_report(games)

    assert report["games"] == 1
    assert report["seats"]["alice"]["actions"] >= 1
    assert report["seats"]["alice"]["reasoning"]["successes"] >= 1


@pytest.fixture()
def game_fixture(game):
    return game


def test_the_fixture_ledger_on_disk_matches_what_the_loader_expects():
    raw = json.loads(FIXTURE.read_text())
    assert isinstance(raw, list) and raw
    for entry in raw:
        assert set(entry) >= {"game_id", "players", "rows"}


# -- the control arm measures its own split too -------------------------------


def _baseline_ledger(game_id: str) -> list[eval_report.GameRows]:
    from magic_cards_model_api.store import store

    return eval_report.ledger_from_store(store, game_ids=[game_id])


def test_a_baseline_driven_refusal_lands_in_a_measured_split(client, game_fixture, monkeypatch):
    """The same hole MEMORY.md records as fixed one level up, one level down.

    `records.require_current_board` sets the flag that lets `audit.record`
    stamp `stale: False` only when a request names a matching `expected_seq`
    - and `baseline_seat` named none. So the WRAPPER agent seat
    (`seatkit.run_action`, which arms it on every action) was measured while
    the CONTROL ARM was not: on a baseline-only ledger every refusal fell in
    the `unknown` bucket and `stale_split` read `unknown`, for exactly the
    arm the whole comparison rests on.

    The engine's verdict here is the one thing faked, and the only thing:
    everything from the request body through `require_current_board`, the
    `records` row and the report is the real path. A refusal is what the
    split is about, and a baseline that reliably provokes one out of the
    engine would have to be built out of a card fixture rather than out of
    the policy.
    """
    from magic_cards_model_api import baseline_seat, engine_api
    from magic_cards_model_api.api_client import HttpSeatApiClient
    from magic_cards_model_api.store import store

    add_card(client, game_fixture, "Forest", "alice", "hand")
    monkeypatch.setattr(
        engine_api,
        "play_land",
        lambda game, req: engine_api.refused("the engine says no, on this very board"),
    )
    uid = {name: uid for uid, name in store.seat_uids(game_fixture).items()}["alice"]
    seat = baseline_seat.BaselineSeat(HttpSeatApiClient(http=client), game_fixture, "alice", uid)

    move = seat.step()

    assert move is not None and move.label == "play_land", move
    assert seat.refusals == 1, "the engine refused it, and the seat counted that"
    refusals = [
        payload
        for rows in _baseline_ledger(game_fixture)
        for payload in rows.payloads()
        if payload.get("detail", {}).get("allowed") is False
    ]
    assert [payload["detail"].get("stale") for payload in refusals] == [False], refusals

    report = eval_report.build_report(_baseline_ledger(game_fixture))

    assert report["stale_split"] == "measured", report
    split = report["seats"]["alice"]["refusal_split"]
    assert split["unknown"] == 0, report
    # In the `offered` bucket rather than `rules`, and that is the other
    # finding, next door: the api had listed this land drop as legal for this
    # seat a moment before refusing it, so whatever it was, it was not the
    # seat misreading the rules. What this test is about is that the row is
    # SPLIT at all - the baseline named the board it decided against, so the
    # race question has an answer.
    assert (split["offered"], split["rules"], split["unchecked"]) == (1, 0, 0), report


def test_an_api_refusal_of_an_action_the_api_offered_is_not_a_rules_error(
    client, game_fixture, monkeypatch
):
    """What `rules_error_rate` is allowed to count.

    `GET .../legal-actions` is the api's own claim that an action is legal for
    this seat right now, and `records.legal_actions_before` writes that same
    enumeration onto the row of whatever the seat then did. So the ledger can
    say which of two things a refusal was, and only the second is the agent's
    fault:

    - the api refused an action it had itself offered, on the very board it
      offered it on (the Adventure face was a measured instance of this: the
      enumeration listed `["cast", "Stomp", <id>]`, posting it back verbatim
      was refused, and the row read as an agent rules error);
    - the seat asked for something that was never on offer.

    Both rows here are real: one refusal of an offered action, one of an
    action the enumeration does not contain, in one game, split apart.
    """
    from magic_cards_model_api import baseline_seat, engine_api
    from magic_cards_model_api.api_client import HttpSeatApiClient
    from magic_cards_model_api.store import store

    add_card(client, game_fixture, "Forest", "alice", "hand")
    forest = state(client, game_fixture)["hands"]["alice"][0]["id"]
    uid = {name: uid for uid, name in store.seat_uids(game_fixture).items()}["alice"]

    # (1) the api refuses the land drop it had just offered
    monkeypatch.setattr(
        engine_api,
        "play_land",
        lambda game, req: engine_api.refused("this api cannot do that yet"),
    )
    seat = baseline_seat.BaselineSeat(HttpSeatApiClient(http=client), game_fixture, "alice", uid)
    assert seat.step() is not None

    # (2) and the seat asks for something the enumeration never offered - a
    # land is not a spell, and no `cast` row names it.
    refused = client.post(
        f"/api/games/{game_fixture}/actions/cast",
        params={"uid": uid},
        json={
            "player": "alice",
            "card_id": forest,
            "expected_seq": _last_seq(client, game_fixture, uid),
            "reasoning": "asking for something that was never offered",
        },
    )
    assert refused.json()["allowed"] is False, refused.json()

    rows = [
        payload
        for game_rows in _baseline_ledger(game_fixture)
        for payload in game_rows.payloads()
        if payload.get("detail", {}).get("allowed") is False
    ]
    assert [(row["action"], row["detail"]["stale"], row["detail"]["offered"]) for row in rows] == [
        ("play_land", False, True),
        ("cast", False, False),
    ], rows

    report = eval_report.build_report(_baseline_ledger(game_fixture))
    alice = report["seats"]["alice"]

    assert alice["refusal_split"] == {
        "rules": 1,
        "offered": 1,
        "stale": 0,
        "unknown": 0,
        "unchecked": 0,
    }, alice
    assert alice["rules_error_rate"]["successes"] == 1, "only the unoffered one is the agent's"
    assert report["offer_split"] == "measured", report
    rendered = eval_report.render(report)
    assert "offered" in rendered, rendered


def _last_seq(client, game_id: str, uid: str) -> int:
    return int(client.get(f"/api/games/{game_id}/log", params={"uid": uid}).json()["last_seq"])


def test_a_refusal_with_no_offered_flag_is_unchecked_not_a_rules_error():
    """The third state, kept for the same reason the missing `stale` flag
    keeps one. A ledger from a build that did not record whether the api had
    offered the action cannot say whether a refusal was the seat's own
    misreading, and counting it as one would invent an agent defect out of a
    field nobody wrote. The report says which of its rows it could not check
    rather than filling the gap with a guess.
    """
    rows = [
        row(2, "cast", player="alice", allowed=False, reason="cannot pay", stale=False),
        row(3, "cast", player="alice", allowed=False, reason="cannot pay", stale=False),
    ]
    report = eval_report.build_report([finished_game("g1", "alice", extra=rows)])

    split = report["seats"]["alice"]["refusal_split"]
    assert (split["rules"], split["offered"], split["unchecked"]) == (0, 0, 2), split
    assert report["stale_split"] == "measured", "the race question was answered"
    assert report["offer_split"] == "unknown", "the offer question was not"
    assert report["seats"]["alice"]["rules_error_rate"]["successes"] == 0
    rendered = eval_report.render(report)
    assert "unchecked       2 refusal(s) carry no detail.offered" in rendered, rendered
    assert "OFFER SPLIT" in rendered and "cannot be told apart" in rendered, rendered


def test_every_refusal_lands_in_exactly_one_bucket():
    """The five buckets are a partition, not five counters that happen to be
    near each other: they must add up to the refusals the report says there
    were, or some refusal is being reported twice or not at all."""
    rows = [
        row(2, "cast", player="alice", allowed=False, reason="a", stale=False, offered=False),
        row(3, "cast", player="alice", allowed=False, reason="b", stale=False, offered=True),
        row(4, "cast", player="alice", allowed=False, reason="c", stale=False),
        row(5, "cast", player="alice", allowed=False, reason="d", stale=True),
        row(6, "cast", player="alice", allowed=False, reason="e"),
        row(7, "cast", player="alice", allowed=True),
    ]
    report = eval_report.build_report([finished_game("g1", "alice", extra=rows)])

    alice = report["seats"]["alice"]
    assert sum(alice["refusal_split"].values()) == alice["refusals"]["successes"] == 5, alice
    assert alice["refusal_split"] == {
        "rules": 1,
        "offered": 1,
        "unchecked": 1,
        "stale": 1,
        "unknown": 1,
    }, alice
    assert report["stale_split"] == "partial" and report["offer_split"] == "partial", report
