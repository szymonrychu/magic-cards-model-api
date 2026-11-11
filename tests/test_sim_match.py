"""The sim-match tool's shared pieces, starting with the interval it reports rates with."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_rates_module_matches_eval_report():
    from magic_cards_model_api.rates import beta_interval as b
    from scripts.eval_report import beta_interval as a

    assert a is b
    assert b(3, 10)["trials"] == 10


FIXTURES = Path(__file__).parent / "fixtures" / "sim"
RED = (FIXTURES / "red_burn.txt").read_text()
GREEN = (FIXTURES / "green_stompy.txt").read_text()


def test_resolve_deck_accepts_text_and_bundled_names():
    from magic_cards_model_api import api_decks
    from magic_cards_model_api.sim.match import resolve_deck

    label, text = resolve_deck(RED)
    assert label == "inline deck" and text == RED
    name = api_decks.list_bundled_decks()["decks"][0]
    label, text = resolve_deck(name)
    assert label == name
    assert text == (api_decks.DECKS_DIR / name).read_text()


def test_resolve_deck_unknown_name_lists_choices():
    from magic_cards_model_api.sim.match import resolve_deck

    with pytest.raises(ValueError) as exc:
        resolve_deck("no_such_deck.txt")
    msg = str(exc.value)
    assert "no_such_deck.txt" in msg
    assert msg.count(".txt") <= 11


def test_aggregate_counts_and_interval():
    from magic_cards_model_api.rates import beta_interval
    from magic_cards_model_api.sim.match import aggregate

    def game(i, winner):
        return {"game": i, "winner": winner, "drawn": False, "ended_by": "play"}

    per_game = [game(0, "a"), game(1, "a"), game(2, "a"), game(3, "b"), game(4, None)]
    per_game[4]["ended_by"] = "bound"
    out = aggregate("Red", "Green", per_game, requested=6, status="running")
    assert out["status"] == "running"
    assert out["requested_games"] == 6
    assert out["finished_games"] == 4
    assert out["unfinished"] == 1
    assert out["draws"] == 0
    assert out["deck_a"]["wins"] == 3 and out["deck_b"]["wins"] == 1
    assert out["deck_a"]["label"] == "Red"
    assert out["deck_a"]["win_rate"] == beta_interval(3, 4)
    assert out["deck_b"]["win_rate"] == beta_interval(1, 4)
    assert out["games"] == per_game
    assert "baseline" in out["note"]
    assert "support_warnings" in out


@pytest.fixture()
def short_games(monkeypatch):
    """Cap every game at 60 steps: a full baseline game costs about two minutes over HTTP."""
    from magic_cards_model_api import baseline_seat

    real = baseline_seat.play_out
    monkeypatch.setattr(baseline_seat, "play_out", lambda seats, **kw: real(seats, max_steps=60))


def test_play_match_same_seed_same_results(short_games):
    from magic_cards_model_api.sim.match import play_match

    runs = []
    for _ in range(2):
        got: list[dict] = []
        play_match(RED, GREEN, 2, 7, got.append)
        runs.append(got)
    assert len(runs[0]) == 2
    key = lambda g: (g["winner"], g["turn"], g["steps"])  # noqa: E731
    assert [key(g) for g in runs[0]] == [key(g) for g in runs[1]]
    for g in runs[0]:
        assert g["steps"] == 60 and g["ended_by"] == "bound"
        assert g["seed"] in (7, 8)


def test_play_match_alternates_seats(short_games):
    from magic_cards_model_api.sim.match import play_match
    from magic_cards_model_api.store import store

    got: list[dict] = []
    before = set(store.ids())
    play_match(RED, GREEN, 3, 1, got.append)
    assert [g["seed"] for g in got] == [1, 2, 3]
    assert [g["a_seat"] for g in got] == [1, 2, 1]
    assert [g["game"] for g in got] == [0, 1, 2]
    assert set(store.ids()) == before
    for g in got:
        assert set(g) == {
            "game", "seed", "a_seat", "winner", "drawn", "turn", "steps", "ended_by", "seconds",
        }
        assert g["winner"] in ("a", "b", None)


def test_worker_match_op_streams_progress_and_ends_with_done():
    from magic_cards_model_api.sim.handle import WorkerHandle

    handle = WorkerHandle()
    handle.start(timeout=60)
    rows: list[dict] = []
    handle.on_progress = rows.append
    try:
        out = handle.request(
            "match", {"deck_a": RED, "deck_b": GREEN, "games": 1, "seed": 3}, timeout=300
        )
    finally:
        handle.kill()
    assert out == {"done": True}
    assert [r["game"] for r in rows] == [0]
    assert not {"admin_key", "uid", "key"} & set(rows[0])
