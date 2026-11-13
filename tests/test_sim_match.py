"""The sim-match tool's shared pieces, starting with the interval it reports rates with."""

from __future__ import annotations

import time
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
    out = aggregate(
        "Red", "Green", per_game, requested=6, status="running", deck_a_text=RED, deck_b_text=GREEN
    )
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
            "game",
            "seed",
            "a_seat",
            "winner",
            "drawn",
            "turn",
            "steps",
            "ended_by",
            "seconds",
        }
        assert g["winner"] in ("a", "b", None)


def test_worker_match_op_streams_progress_and_ends_with_done():
    from magic_cards_model_api.sim.handle import WorkerHandle

    handle = WorkerHandle()
    handle.start(timeout=60)
    rows: list[dict] = []
    handle.on_progress = rows.append
    try:
        # max_steps keeps each game short (a full baseline game takes about two minutes).
        out = handle.request(
            "match",
            {"deck_a": RED, "deck_b": GREEN, "games": 2, "seed": 3, "max_steps": 40},
            timeout=300,
        )
        assert handle.on_progress is None  # cleared once the op finished
    finally:
        handle.kill()
    assert out == {"done": True}
    assert [r["game"] for r in rows] == [0, 1]
    for r in rows:
        assert not {"admin_key", "uid", "key"} & set(r)


class FakeHandle:
    """A worker stand-in whose `match` request blocks until released or killed."""

    def __init__(self):
        import threading

        self.on_progress = None
        self.release = threading.Event()
        self.killed = False

    def start(self, timeout):
        pass

    def request(self, op, args, timeout):
        from magic_cards_model_api.sim.handle import WorkerTimeout

        if not self.release.wait(timeout=timeout):
            raise WorkerTimeout("slow")
        return {"done": True}

    def kill(self):
        self.killed = True
        self.release.set()


def _batches(env=None, handles=None, **kw):
    from magic_cards_model_api.sim.config import SimLimits
    from magic_cards_model_api.sim.manager import BatchManager

    limits = SimLimits.from_env({"MTG_SIM_BASELINE_ENABLED": "1", **(env or {})})
    made = handles if handles is not None else []

    def factory():
        made.append(FakeHandle())
        return made[-1]

    return BatchManager(limits, handle_factory=factory, **kw), made


def test_live_ids_lists_the_held_batches():
    batches, made = _batches()
    assert batches.live_ids() == []

    batch_id = batches.start(RED, GREEN, 2, 1)

    assert batches.live_ids() == [batch_id]
    batches.shutdown()


def test_second_batch_is_refused_while_one_runs():
    from magic_cards_model_api.sim.manager import SimRefused

    batches, made = _batches()
    first = batches.start(RED, GREEN, 2, 1)
    assert first.startswith("batch-")
    with pytest.raises(SimRefused) as exc:
        batches.start(RED, GREEN, 2, 1)
    msg = str(exc.value)
    assert msg.startswith("A batch is already running (about ")
    assert "s left of its 1500 s cap). Poll it or retry later." in msg
    made[0].release.set()
    assert batches.status(first, 5)["status"] == "done"
    assert batches.start(RED, GREEN, 2, 1) != first  # the slot is free again
    batches.shutdown()


def test_games_above_cap_refused():
    from magic_cards_model_api.sim.manager import SimRefused

    batches, made = _batches({"MTG_SIM_MAX_MATCH_GAMES": "3"})
    with pytest.raises(SimRefused) as exc:
        batches.start(RED, GREEN, 4, None)
    assert "MTG_SIM_MAX_MATCH_GAMES" in str(exc.value)
    assert made == []
    with pytest.raises(SimRefused):
        batches.start(RED, GREEN, 0, None)
    with pytest.raises(SimRefused) as exc:
        batches.start("no_such_deck.txt", GREEN, 1, None)
    assert "no_such_deck.txt" in str(exc.value)


def test_random_seed_is_drawn_and_reported():
    batches, made = _batches()
    bid = batches.start(RED, GREEN, 1, None)
    made[0].release.set()
    out = batches.status(bid, 5)
    assert isinstance(out["seed"], int)
    assert out["batch_id"] == bid
    batches.shutdown()


def test_finished_batch_reaped_after_result_ttl():
    from magic_cards_model_api.sim.manager import SimRefused

    now = [1000.0]
    batches, made = _batches({"MTG_SIM_RESULT_TTL_S": "50"}, clock=lambda: now[0])
    bid = batches.start(RED, GREEN, 1, 1)
    made[0].release.set()
    assert batches.status(bid, 5)["status"] == "done"
    now[0] += 40
    assert batches.reap() == []
    assert batches.status(bid, 0)["status"] == "done"
    now[0] += 20
    assert batches.reap() == [bid]
    with pytest.raises(SimRefused):
        batches.status(bid, 0)


def test_wall_cap_kills_and_keeps_partial_results(monkeypatch):
    from magic_cards_model_api.sim.config import SimLimits
    from magic_cards_model_api.sim.manager import BatchManager

    monkeypatch.setenv("MTG_SIM_TEST_OPS", "1")
    limits = SimLimits.from_env({"MTG_SIM_MATCH_WALL_S": "12", "MTG_SIM_BASELINE_ENABLED": "1"})
    batches = BatchManager(limits, op="match_slow")
    bid = batches.start(RED, GREEN, 3, 5)
    try:
        out = batches.status(bid, 60)
    finally:
        batches.shutdown()
    assert out["status"] == "capped"
    assert len(out["games"]) == 1
    assert out["finished_games"] == 1
    assert out["requested_games"] == 3
    assert out["seed"] == 5
    assert out["elapsed_s"] >= 12 - 1


def test_worker_death_mid_batch_fails_it_and_frees_the_slot():
    from magic_cards_model_api.sim.handle import WorkerDied

    class Dying(FakeHandle):
        def request(self, op, args, timeout):
            raise WorkerDied("exit code -9")

    made = []

    def factory():
        made.append(Dying() if not made else FakeHandle())
        return made[-1]

    from magic_cards_model_api.sim.config import SimLimits
    from magic_cards_model_api.sim.manager import BatchManager

    batches = BatchManager(
        SimLimits.from_env({"MTG_SIM_BASELINE_ENABLED": "1"}), handle_factory=factory
    )
    bid = batches.start(RED, GREEN, 2, 1)
    out = batches.status(bid, 5)
    assert out["status"] == "failed"
    assert "worker stopped" in out["error"] and "exit code -9" in out["error"]
    assert made[0].killed
    second = batches.start(RED, GREEN, 2, 1)  # the slot is free again
    assert second != bid
    made[1].release.set()
    assert batches.status(second, 5)["status"] == "done"
    batches.shutdown()


def test_shutdown_before_the_worker_spawns_never_leaves_an_orphan():
    import threading

    entered, gate = threading.Event(), threading.Event()
    made = []

    class Late(FakeHandle):
        def __init__(self):
            super().__init__()
            self.requested = False

        def request(self, op, args, timeout):
            self.requested = True
            return super().request(op, args, timeout)

    def factory():
        entered.set()
        gate.wait(5)
        made.append(Late())
        return made[-1]

    from magic_cards_model_api.sim.config import SimLimits
    from magic_cards_model_api.sim.manager import BatchManager

    batches = BatchManager(
        SimLimits.from_env({"MTG_SIM_BASELINE_ENABLED": "1"}), handle_factory=factory
    )
    bid = batches.start(RED, GREEN, 2, 1)
    assert entered.wait(5)
    stopper = threading.Thread(target=batches.shutdown)
    stopper.start()
    time.sleep(0.3)
    gate.set()
    stopper.join(15)
    assert not stopper.is_alive()
    assert made[0].killed and not made[0].requested
    assert batches.status(bid, 0)["status"] == "failed"


def test_support_warnings_are_computed_once_per_batch_not_per_poll(monkeypatch):
    from magic_cards_model_api.sim import match

    calls: list[set[str]] = []

    def fake_block(names):
        calls.append(set(names))
        return {"support_warnings": [{"card": "X"}], "support_note": "note"}

    monkeypatch.setattr(match, "support_block", fake_block)
    batches, made = _batches()
    bid = batches.start(RED, GREEN, 2, 1)
    try:
        for _ in range(3):
            out = batches.status(bid, 0.2)
            assert "support_warnings" in out
        made[0].release.set()
        done = batches.status(bid, 5)
        assert done["status"] == "done"
        assert done["support_warnings"] == [{"card": "X"}]
        assert done["support_note"] == "note"
        batches.status(bid, 0)
        batches.status(bid, 0)
    finally:
        batches.shutdown()
    assert len(calls) == 1


def test_poll_before_support_is_computed_says_pending_not_an_empty_all_clear(monkeypatch):
    import threading

    from magic_cards_model_api.sim import match

    gate = threading.Event()

    def slow_block(names):
        gate.wait(10)
        return {"support_warnings": []}

    monkeypatch.setattr(match, "support_block", slow_block)
    batches, made = _batches()
    bid = batches.start(RED, GREEN, 2, 1)
    try:
        out = batches.status(bid, 0)
        assert out["support_status"] == "pending"
        assert out["support_warnings"] == []
        assert "could not be checked yet" in out["support_note"]
        assert "not" in out["support_note"] and "reliable" in out["support_note"]
        gate.set()
        made[0].release.set()
        done = batches.status(bid, 5)
        assert done["support_status"] == "ok"
        assert "support_note" not in done
    finally:
        gate.set()
        batches.shutdown()


def test_failed_support_lookup_says_unavailable_and_logs_once(monkeypatch, caplog):
    from magic_cards_model_api.sim import match

    def boom(names):
        raise RuntimeError("scryfall down SECRETCARD")

    monkeypatch.setattr(match, "support_block", boom)
    batches, made = _batches()
    with caplog.at_level("WARNING"):
        bid = batches.start(RED, GREEN, 2, 1)
        try:
            made[0].release.set()
            done = batches.status(bid, 5)
            assert done["status"] == "done"
            batches.status(bid, 0)
        finally:
            batches.shutdown()
    assert done["support_status"] == "unavailable"
    assert "could not be checked at all" in done["support_note"]
    assert "reliable" in done["support_note"]
    warns = [r for r in caplog.records if r.levelname == "WARNING" and "support" in r.getMessage()]
    assert len(warns) == 1
    assert "SECRETCARD" not in warns[0].getMessage()
    assert warns[0].exc_info is None


def test_ok_support_keeps_warnings_and_note(monkeypatch):
    from magic_cards_model_api.sim import match

    monkeypatch.setattr(
        match,
        "support_block",
        lambda names: {"support_warnings": [{"card": "X"}], "support_note": "note"},
    )
    batches, made = _batches()
    bid = batches.start(RED, GREEN, 2, 1)
    try:
        made[0].release.set()
        done = batches.status(bid, 5)
    finally:
        batches.shutdown()
    assert done["support_status"] == "ok"
    assert done["support_warnings"] == [{"card": "X"}]
    assert done["support_note"] == "note"


def test_batches_are_refused_while_the_baseline_is_off():
    from magic_cards_model_api.sim.config import SimLimits
    from magic_cards_model_api.sim.manager import BatchManager, SimRefused

    made: list = []
    batches = BatchManager(SimLimits.from_env({}), handle_factory=lambda: made.append(1))
    with pytest.raises(SimRefused, match="MTG_SIM_BASELINE_ENABLED"):
        batches.start("a", "b", 1, 1)
    assert made == []
