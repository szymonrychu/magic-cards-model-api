import json
import threading
import time
from pathlib import Path

import pytest

from magic_cards_model_api.sim.config import SimLimits
from magic_cards_model_api.sim.handle import WorkerDied, WorkerError, WorkerTimeout
from magic_cards_model_api.sim.manager import Reaper, SandboxManager, SimRefused

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
SPEC = {"players": [{"name": "a"}, {"name": "b"}], "cards": []}


def load_spec(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeHandle:
    def __init__(self, fail=None):
        self.fail = fail
        self.killed = False
        self.started = False

    def start(self, timeout):
        self.started = True

    def request(self, op, args, timeout):
        if self.fail is not None and op != "new":
            raise self.fail
        return {"op": op}

    def kill(self):
        self.killed = True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def made():
    return []


def factory_for(made, **kw):
    def make():
        h = FakeHandle(**kw)
        made.append(h)
        return h

    return make


def manager(clock, made, **kw):
    factory = factory_for(made, **kw)
    return SandboxManager(SimLimits.from_env({}), handle_factory=factory, clock=clock)


def test_second_sandbox_is_refused_with_ttl_and_retry_advice(clock, made):
    m = manager(clock, made)
    m.new(SPEC)
    with pytest.raises(SimRefused, match=r"frees in 1800 s.*sim_end"):
        m.new(SPEC)


def test_new_returns_answer_and_sandbox_info(clock, made):
    out = manager(clock, made).new(SPEC, mode="x")
    assert out["op"] == "new"
    assert out["sandbox"]["id"].startswith("sim-")
    assert out["sandbox"]["expires_in_s"] == 1800
    assert out["sandbox"]["mode"] == "x"


def test_idle_sandbox_is_reaped_after_ttl(clock, made):
    m = manager(clock, made)
    sid = m.new(SPEC)["sandbox"]["id"]
    clock.now += 1801
    assert m.reap() == [sid]
    assert made[0].killed
    with pytest.raises(SimRefused, match="expired"):
        m.call(sid, "state", {})


def test_use_resets_the_idle_clock(clock, made):
    m = manager(clock, made)
    sid = m.new(SPEC)["sandbox"]["id"]
    clock.now += 1500
    m.call(sid, "state", {})
    clock.now += 1500
    assert m.reap() == []
    assert m.call(sid, "state", {}) == {"op": "state"}


def test_call_timeout_ends_the_sandbox(clock, made):
    m = manager(clock, made, fail=WorkerTimeout("slow"))
    sid = m.new(SPEC)["sandbox"]["id"]
    with pytest.raises(SimRefused, match="exceeded 25 s"):
        m.call(sid, "state", {})
    assert made[0].killed
    m.new(SPEC)


def test_worker_death_mid_call_frees_the_slot(clock, made):
    m = manager(clock, made, fail=WorkerDied("rss cap"))
    sid = m.new(SPEC)["sandbox"]["id"]
    with pytest.raises(SimRefused, match="worker stopped: rss cap"):
        m.call(sid, "state", {})
    m.new(SPEC)


def test_engine_refusal_on_a_call_is_an_answer(clock, made):
    m = manager(clock, made, fail=WorkerError("nope"))
    sid = m.new(SPEC)["sandbox"]["id"]
    assert m.call(sid, "act", {}) == {"allowed": False, "reason": "nope"}
    assert not made[0].killed


def test_end_kills_and_frees_the_slot(clock, made):
    m = manager(clock, made)
    sid = m.new(SPEC)["sandbox"]["id"]
    assert m.end(sid) == {"ended": True}
    assert made[0].killed
    m.new(SPEC)


def test_failed_new_kills_the_worker_and_frees_the_slot(clock):
    made = []

    class Bad(FakeHandle):
        def request(self, op, args, timeout):
            raise WorkerError("unknown card")

    def make():
        h = Bad()
        made.append(h)
        return h

    m = SandboxManager(SimLimits.from_env({}), handle_factory=make, clock=clock)
    with pytest.raises(SimRefused, match="unknown card"):
        m.new(SPEC)
    assert made[0].killed
    made.clear()
    with pytest.raises(SimRefused, match="unknown card"):
        m.new(SPEC)  # slot was free again


def test_shutdown_kills_everything(clock, made):
    m = manager(clock, made)
    m.new(SPEC)
    m.shutdown()
    assert made[0].killed


class Gated(FakeHandle):
    """A handle whose requests block until released; kill releases them as a dead worker."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def request(self, op, args, timeout):
        self.entered.set()
        self.release.wait(5)
        if self.killed:
            raise WorkerDied("killed")
        return {"op": op}

    def kill(self):
        super().kill()
        self.release.set()


def short_limits(cap="0.3"):
    return SimLimits.from_env({"MTG_SIM_CALL_TIMEOUT_S": cap})


def test_new_bounds_start_and_request_by_one_deadline(clock):
    seen = {}

    class Slow(FakeHandle):
        def start(self, timeout):
            seen["start"] = timeout
            time.sleep(0.2)

        def request(self, op, args, timeout):
            seen["request"] = timeout
            raise WorkerTimeout("slow")

    made = []

    def make():
        made.append(Slow())
        return made[-1]

    m = SandboxManager(short_limits("1"), handle_factory=make, clock=clock)
    with pytest.raises(SimRefused, match="exceeded the 1 s call limit"):
        m.new(SPEC)
    assert seen["start"] == 1.0
    assert 0 < seen["request"] <= 0.85
    assert made[0].killed
    with pytest.raises(SimRefused, match="exceeded"):
        m.new(SPEC)  # slot is free again


def test_factory_failure_frees_the_slot(clock):
    calls = []

    def make():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("spawn failed")
        return FakeHandle()

    m = SandboxManager(SimLimits.from_env({}), handle_factory=make, clock=clock)
    with pytest.raises(SimRefused, match="spawn failed"):
        m.new(SPEC)
    assert m.new(SPEC)["sandbox"]["id"]


def test_non_worker_error_on_new_becomes_a_refusal_and_frees_the_slot(clock):
    class Boom(FakeHandle):
        def start(self, timeout):
            raise OSError("exec failed")

    made = []

    def make():
        made.append(Boom())
        return made[-1]

    m = SandboxManager(SimLimits.from_env({}), handle_factory=make, clock=clock)
    with pytest.raises(SimRefused, match="exec failed"):
        m.new(SPEC)
    assert made[0].killed
    with pytest.raises(SimRefused, match="exec failed"):
        m.new(SPEC)


def test_concurrent_new_has_exactly_one_winner(clock):
    gate = Gated()
    m = SandboxManager(SimLimits.from_env({}), handle_factory=lambda: gate, clock=clock)
    results = []

    def first():
        results.append(m.new(SPEC))

    t = threading.Thread(target=first)
    t.start()
    assert gate.entered.wait(2)
    with pytest.raises(SimRefused, match="slot is in use"):
        m.new(SPEC)
    gate.release.set()
    t.join(2)
    assert len(results) == 1


def test_second_concurrent_call_is_refused_busy_within_the_cap(clock):
    gate = Gated()
    m = SandboxManager(short_limits(), handle_factory=lambda: gate, clock=clock)
    gate.release.set()
    sid = m.new(SPEC)["sandbox"]["id"]
    gate.release.clear()
    gate.entered.clear()
    t = threading.Thread(target=lambda: m.call(sid, "state", {}))
    t.start()
    assert gate.entered.wait(2)
    began = time.monotonic()
    with pytest.raises(SimRefused, match="busy with another call"):
        m.call(sid, "state", {})
    assert time.monotonic() - began < 1
    gate.release.set()
    t.join(2)


def test_reap_racing_an_in_flight_call_ends_it_cleanly(clock):
    gate = Gated()
    m = SandboxManager(SimLimits.from_env({}), handle_factory=lambda: gate, clock=clock)
    gate.release.set()
    sid = m.new(SPEC)["sandbox"]["id"]
    gate.release.clear()
    gate.entered.clear()
    outcome = []

    def run():
        try:
            m.call(sid, "state", {})
        except SimRefused as exc:
            outcome.append(str(exc))

    t = threading.Thread(target=run)
    t.start()
    assert gate.entered.wait(2)
    clock.now += 1801
    assert m.reap() == [sid]
    t.join(2)
    assert gate.killed
    assert outcome and "worker stopped" in outcome[0]
    with pytest.raises(SimRefused, match="No sandbox"):
        m.call(sid, "state", {})


def test_new_after_shutdown_is_refused(clock, made):
    m = manager(clock, made)
    m.shutdown()
    with pytest.raises(SimRefused, match="shutting down"):
        m.new(SPEC)
    assert made == []


def test_new_finishing_after_shutdown_kills_its_worker(clock):
    gate = Gated()
    m = SandboxManager(SimLimits.from_env({}), handle_factory=lambda: gate, clock=clock)
    errors = []

    def run():
        try:
            m.new(SPEC)
        except SimRefused as exc:
            errors.append(str(exc))

    t = threading.Thread(target=run)
    t.start()
    assert gate.entered.wait(2)
    m.shutdown()
    gate.release.set()
    t.join(2)
    assert gate.killed
    assert errors


def test_reaper_calls_registered_functions_and_survives_errors():
    ran = threading.Event()
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("x")

    r = Reaper(interval_s=0.01)
    r.register(boom)
    r.register(ran.set)
    r.start()
    assert ran.wait(2)
    r.stop()
    assert calls


def test_sim_new_unknown_card_refuses_and_frees_the_slot():
    m = SandboxManager(SimLimits.from_env({}))
    with pytest.raises(SimRefused, match="Not A Real Card"):
        m.new(
            {
                "players": [{"name": "a"}, {"name": "b"}],
                "cards": [{"card": "Not A Real Card", "player": "a"}],
            }
        )
    try:
        m.new(load_spec("bolt_bears.json"))
    finally:
        m.shutdown()


def test_real_worker_round_trip_and_end():
    m = SandboxManager(SimLimits.from_env({}))
    try:
        sid = m.new(load_spec("bolt_bears.json"))["sandbox"]["id"]
        assert "players" in json.dumps(m.call(sid, "state", {}))
        m.end(sid)
        with pytest.raises(SimRefused, match="No sandbox"):
            m.call(sid, "state", {})
    finally:
        m.shutdown()
