import threading

import pytest

from mtg_agent.wrapper.turns import Record, Store, TurnState


def test_create_then_complete():
    store = Store()
    record = store.create("turn-1", "hello", "https://cb.example/x", 1000.0)
    assert record.state is TurnState.RUNNING
    assert store.get("turn-1").callback_url == "https://cb.example/x"

    done = store.complete(
        "turn-1",
        final_text="PONG",
        result_json={"status": "success"},
        usage={"output_tokens": 3},
        turn_tokens=[],
        stop_reason="end_turn",
        now=1005.0,
    )
    assert done.state is TurnState.COMPLETE
    assert done.final_text == "PONG"
    assert done.stop_reason == "end_turn"
    assert done.completed_at == 1005.0


def test_complete_or_fail_on_an_unknown_id_raises():
    store = Store()
    with pytest.raises(KeyError):
        store.complete(
            "missing",
            final_text="",
            result_json=None,
            usage=None,
            turn_tokens=[],
            stop_reason="",
            now=1.0,
        )
    with pytest.raises(KeyError):
        store.fail("missing", "boom", 1.0)


def test_list_is_in_creation_order_and_carries_state():
    store = Store()
    store.create("a", "1", "", 1.0)
    store.create("b", "2", "", 2.0)
    store.fail("a", "boom", 3.0)
    listed = store.list()
    assert [r.id for r in listed] == ["a", "b"]
    assert listed[0].state is TurnState.FAILED
    assert listed[0].error == "boom"


def test_touch_moves_last_activity_but_not_started_at():
    store = Store()
    record = store.create("turn-1", "hello", "", 1000.0)
    assert record.last_activity_at == 1000.0
    assert store.touch("turn-1", 1007.0) is True
    got = store.get("turn-1")
    assert got.last_activity_at == 1007.0
    assert got.started_at == 1000.0


def test_touch_is_a_noop_on_unknown_or_terminal_turns():
    store = Store()
    assert store.touch("missing", 1.0) is False

    store.create("done", "x", "", 1000.0)
    store.complete(
        "done",
        final_text="ok",
        result_json=None,
        usage=None,
        turn_tokens=[],
        stop_reason="end_turn",
        now=1005.0,
    )
    assert store.touch("done", 2000.0) is False
    assert store.get("done").last_activity_at == 1000.0


def test_counts_separate_successful_turns_from_all_finished_turns():
    store = Store()
    store.create("a", "", "", 1.0)
    store.create("b", "", "", 2.0)
    store.create("c", "", "", 3.0)
    store.complete(
        "a", final_text="", result_json=None, usage=None, turn_tokens=[], stop_reason="", now=4.0
    )
    store.fail("b", "boom", 5.0)
    assert store.counts() == (1, 2)


def test_concurrent_creates_all_land():
    store = Store()

    def make(index: int) -> None:
        store.create(f"turn-{index}", "", "", float(index))

    threads = [threading.Thread(target=make, args=(i,)) for i in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(store.list()) == 50
    assert len({r.id for r in store.list()}) == 50


def test_record_summary_omits_the_heavy_fields():
    record = Record(id="t", state=TurnState.RUNNING, text="hi", callback_url="", started_at=1.0)
    summary = record.summary()
    assert set(summary) == {"turnId", "state", "startedAt", "completedAt", "lastActivityAt"}
