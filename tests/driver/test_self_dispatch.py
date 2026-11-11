"""Per-game Kubernetes Job mode: the driver dispatches itself from
`DRIVER_ASSIGNMENT_JSON` at startup, and `retire` exits the process when
`DRIVER_EXIT_AFTER_MATCH` says the container plays exactly one match.

A self-dispatch that never gets going must not leave the Job idling until
Kubernetes' own deadline finds it - a malformed assignment, an unreachable
game server, or an agent that never answers within `agent_boot_wait` are all
exercised below, each expected to record `lastError` and, when
`exit_after_match` is set, request the exit itself rather than wait to be
asked.
"""

import json
import threading

from fastapi.testclient import TestClient

from mtg_agent.driver.app import build_app
from mtg_agent.driver.config import DriverSettings
from tests.driver.test_app import (  # noqa: F401 - `world` is the fixture
    complete_handover,
    wait_for,
    world,
)

AGENT_SETTINGS = {"model": "sonnet", "effort": "high", "maxThinkingTokens": None}


def _env(tmp_path, **overrides) -> dict:
    """The environment every test here starts from - a workspace under
    `tmp_path` and the fake game/wrapper hosts `world.client_factory` routes.
    Individual tests add `DRIVER_ASSIGNMENT_JSON`, `DRIVER_EXIT_AFTER_MATCH`,
    or the boot-wait knobs on top."""
    base = {
        "DRIVER_WORKSPACE": str(tmp_path),
        "MEMORY_ROOT": str(tmp_path / "memory"),
        "GUIDES_ROOT": str(tmp_path / "guides"),
        "GAME_API": "http://game:8000",
        "WRAPPER_API": "http://agent:8080",
    }
    base.update(overrides)
    return base


def _assignment(w, **overrides) -> str:
    """A valid `DRIVER_ASSIGNMENT_JSON` body for `w`'s fake game, as JSON."""
    body = {
        "uid": w.uid,
        "game_id": w.game_id,
        "player": "bob",
        "api_base": "http://game:8000",
    }
    body.update(overrides)
    return json.dumps(body)


class _GatedClient:
    """Wraps one `httpx.Client`-shaped object (a `TestClient`, here) so its
    first `get` blocks on `gate` before delegating - `_gated_client_factory`'s
    way of pinning exactly when `assign`'s very first outbound call (`game_
    state`'s `GET /api/games/{id}`) may return, without depending on real
    timing."""

    def __init__(self, inner, gate: threading.Event) -> None:
        self._inner = inner
        self._gate = gate

    def __enter__(self) -> "_GatedClient":
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._inner.__exit__(*exc_info)

    def get(self, *args, **kwargs):
        # Bounded even though the test is expected to set `gate` itself:
        # a genuine regression must fail this test, never hang the suite.
        self._gate.wait(timeout=10.0)
        return self._inner.get(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _gated_client_factory(inner_factory, gate: threading.Event):
    """`inner_factory` (`world.client_factory`), except the very first client
    built for the game server is a `_GatedClient` - which is `assign`'s very
    first line of work (`game_state`), before it has touched `current` at
    all. Every later client (including every later call to the game server)
    passes straight through."""
    gated_once = {"done": False}

    def factory(base_url: str):
        client = inner_factory(base_url)
        if "game" in base_url and not gated_once["done"]:
            gated_once["done"] = True
            return _GatedClient(client, gate)
        return client

    return factory


def status_of(client) -> dict:
    return client.get("/status").json()


def test_assignment_from_env_is_dispatched_at_startup(world, tmp_path) -> None:  # noqa: F811
    settings = DriverSettings.from_env(
        _env(
            tmp_path,
            DRIVER_ASSIGNMENT_JSON=_assignment(world, agent=AGENT_SETTINGS),
        )
    )
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:  # runs the startup hook - the dispatch itself is background now
        status = wait_for(lambda: status_of(client) if status_of(client).get("gameId") else None)
    assert status["gameId"] == world.game_id
    assert status["agent"] == AGENT_SETTINGS  # the agent block round-trips through env
    assert world.wrapper_posts[0]["text"].startswith("You are")  # turn 0 pushed


def test_status_is_live_while_the_self_dispatch_is_still_booting(world, tmp_path) -> None:  # noqa: F811
    """The bug this guards: `lifespan` used to `await asyncio.to_thread(assign,
    body)` before `yield`, so ASGI startup - and therefore every route,
    including `GET /status` - never became available until the whole
    self-dispatch finished. A per-game Job's own `assign()` can be slow for
    perfectly good reasons (a slow game server, an agent still within its own
    `agent_boot_wait`), and `k8s_seats.status()` treats a `Running` pod whose
    `/status` cannot be reached at all for more than `driver_grace_seconds`
    (60s) as a dead seat - so any self-dispatch slower than that used to read
    as `state: error, reason: driver unreachable` even though the driver was
    perfectly alive and simply still choosing a deck.

    Proven without depending on real timing: `assign`'s very first outbound
    call is gated behind an `Event` nothing sets until this test says so, so
    the self-dispatch cannot possibly have finished when `/status` is first
    asked - and `/status` must still answer, truthfully, that a dispatch is
    under way rather than reporting the idle-and-finished shape a reader
    would otherwise mistake for a seat that gave up.
    """
    gate = threading.Event()
    settings = DriverSettings.from_env(_env(tmp_path, DRIVER_ASSIGNMENT_JSON=_assignment(world)))
    app = build_app(settings, client_factory=_gated_client_factory(world.client_factory, gate))

    entered = threading.Event()
    outcome: dict[str, dict] = {}

    def boot_and_probe() -> None:
        with TestClient(app) as client:
            entered.set()
            outcome["during_boot"] = client.get("/status").json()
            gate.set()
            # `gameId` is set before `pending` clears, so on a loaded runner a
            # poll can land between the two; wait for the settled shape.
            outcome["after_dispatch"] = wait_for(
                lambda: (
                    status_of(client)
                    if status_of(client).get("gameId") and status_of(client).get("pending") is False
                    else None
                )
            )

    thread = threading.Thread(target=boot_and_probe, daemon=True)
    thread.start()
    if not entered.wait(timeout=2.0):
        gate.set()  # unstick the hung self-dispatch so the thread can still exit
        thread.join(timeout=5.0)
        raise AssertionError(
            "the ASGI app never finished starting up: lifespan awaited the self-dispatch "
            "synchronously, so GET /status could not be served while the agent was still booting"
        )
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "the probing thread is still stuck talking to the driver"

    during_boot = outcome["during_boot"]
    assert during_boot["gameId"] is None
    assert during_boot["lastError"] is None
    # Alive-and-dispatching, not idle-and-finished: `k8s_seats.status()` and
    # `ai_seat.py` both read `pending` as "assigned, not yet confirmed
    # playing" rather than the `idle` shape they read as "this seat gave up".
    assert during_boot["pending"] is True

    after_dispatch = outcome["after_dispatch"]
    assert after_dispatch["gameId"] == world.game_id
    assert after_dispatch["pending"] is False


def test_retire_requests_exit_when_told_to(world, tmp_path) -> None:  # noqa: F811
    settings = DriverSettings.from_env(_env(tmp_path, DRIVER_EXIT_AFTER_MATCH="1"))
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:
        client.post(
            "/dispatch",
            json={
                "uid": world.uid,
                "game_id": world.game_id,
                "player": "bob",
                "api_base": "http://game:8000",
            },
        )
        client.post("/release", json={"uid": world.uid, "game_id": world.game_id})
        complete_handover(client, world.submitted)
        wait_for(lambda: app.state.should_exit)
    assert app.state.should_exit is True


def test_the_completed_match_also_exits_when_told_to(world, tmp_path) -> None:  # noqa: F811
    """`/release` is one caller of `retire`; the ordinary end-of-match path
    through `/turn-done`'s reflection turn is another, and both must exit -
    `retire` is the single place either reaches, so this guards that the exit
    signal lives there and not in one caller alone."""
    settings = DriverSettings.from_env(_env(tmp_path, DRIVER_EXIT_AFTER_MATCH="1"))
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:
        client.post(
            "/dispatch",
            json={
                "uid": world.uid,
                "game_id": world.game_id,
                "player": "bob",
                "api_base": "http://game:8000",
            },
        )
        world.state["winner"] = "bob"
        client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
        client.post(
            "/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"}
        )
        complete_handover(client, world.submitted)
        wait_for(lambda: app.state.should_exit)
    assert app.state.should_exit is True


def test_the_ai_review_completes_before_the_session_ends_and_the_process_exits(
    world,  # noqa: F811
    tmp_path,
) -> None:
    """The bug this guards: the AI review runs in a background daemon
    thread (`post_ai_review`, spawned by `turn_done`'s reflection branch),
    and `retire` used to fire `DELETE /v1/session` - which tears the agent
    container down - and, here, set `should_exit` - which ends this very
    driver process - without ever waiting on it. A handover typically
    finishes in 20-30s; an Opus review over a whole game routinely takes
    longer, so the review was lost for good on every real exit. `retire`
    must now join that thread (bounded by `RETIRE_REVIEW_JOIN_TIMEOUT`)
    before either the DELETE or the exit - proved here by a real, if short,
    delay on the fake wrapper's own `/v1/review`, and by `world.events`
    (appended to by both fake apps' route handlers, in the order their
    calls actually complete) showing the review landing first."""
    world.state["wrapper_review_delay"] = 0.3
    settings = DriverSettings.from_env(_env(tmp_path, DRIVER_EXIT_AFTER_MATCH="1"))
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:
        client.post(
            "/dispatch",
            json={
                "uid": world.uid,
                "game_id": world.game_id,
                "player": "bob",
                "api_base": "http://game:8000",
            },
        )
        world.state["winner"] = "bob"
        client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
        client.post(
            "/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"}
        )
        complete_handover(client, world.submitted)
        wait_for(lambda: app.state.should_exit)
    assert app.state.should_exit is True
    assert world.ai_review_posts, "the review must have been filed at all, not merely raced"
    assert world.events.index("ai_review") < world.events.index("delete"), (
        f"the session must not end before the AI review is filed: events were {world.events}"
    )


def test_the_review_thread_is_registered_before_the_handover_thread_is_created(
    world,  # noqa: F811
    tmp_path,
    monkeypatch,
) -> None:
    """`current["reviewThread"]` used to be set only AFTER `start_handover`
    returned - but `start_handover` itself is what creates and starts
    `run_handover`'s own background thread, which calls `retire` at its own
    end and joins `current["reviewThread"]` only if it is already set. A
    `run_handover` racing ahead fast enough could reach `retire` before the
    review thread was ever registered, finding nothing to join and losing
    the review outright - not merely slower than `RETIRE_REVIEW_JOIN_
    TIMEOUT`, never joined at all.

    Proven here without depending on real thread-scheduling luck: which of
    the two `threading.Thread` objects gets CONSTRUCTED first is a
    deterministic, single-threaded fact, not a race - `run_handover`'s own
    `Thread` is constructed only inside `start_handover`, so the review's
    own `Thread` being constructed (and, on the same code path, started and
    registered into `current["reviewThread"]`) strictly before that call is
    what makes the join possible on every ordering the OS scheduler could
    ever produce, not merely a lucky one."""
    created: list[str] = []
    original_init = threading.Thread.__init__

    def recording_init(self: threading.Thread, *args: object, **kwargs: object) -> None:
        target = kwargs.get("target")
        if target is not None and getattr(target, "__name__", None) in (
            "run_handover",
            "post_ai_review",
        ):
            created.append(target.__name__)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "__init__", recording_init)
    settings = DriverSettings.from_env(_env(tmp_path, DRIVER_EXIT_AFTER_MATCH="1"))
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:
        client.post(
            "/dispatch",
            json={
                "uid": world.uid,
                "game_id": world.game_id,
                "player": "bob",
                "api_base": "http://game:8000",
            },
        )
        world.state["winner"] = "bob"
        client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
        client.post(
            "/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"}
        )
        complete_handover(client, world.submitted)
        wait_for(lambda: app.state.should_exit)

    assert "post_ai_review" in created and "run_handover" in created, created
    assert created.index("post_ai_review") < created.index("run_handover"), created


# ---- a self-dispatch that never gets going must not hang the Job forever --


def test_a_malformed_assignment_starts_cleanly_and_records_the_error(world, tmp_path) -> None:  # noqa: F811
    """Neither unparseable JSON nor JSON missing required fields may raise out
    of the lifespan - a Job under `backoffLimit: 0` that failed to boot would
    exit 3 and never even serve `/status` to say why."""
    for bad_json in ("not json", json.dumps({"uid": "u"})):
        settings = DriverSettings.from_env(_env(tmp_path, DRIVER_ASSIGNMENT_JSON=bad_json))
        app = build_app(settings, client_factory=world.client_factory)
        with TestClient(app) as client:
            status = client.get("/status").json()
        assert status["lastError"], bad_json


def test_a_self_dispatch_that_cannot_reach_the_game_server_exits(world, tmp_path) -> None:  # noqa: F811
    world.state["game"] = "down"
    settings = DriverSettings.from_env(
        _env(
            tmp_path,
            DRIVER_ASSIGNMENT_JSON=_assignment(world),
            DRIVER_EXIT_AFTER_MATCH="1",
        )
    )
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:
        # The dispatch now runs in the background, so the failure lands a
        # moment after startup rather than before it.
        status = wait_for(lambda: status_of(client) if status_of(client)["lastError"] else None)
    assert status["lastError"] is not None
    assert app.state.should_exit is True


def test_a_self_dispatch_whose_agent_never_answers_exits(world, tmp_path) -> None:  # noqa: F811
    world.state["agent"] = "down"
    settings = DriverSettings.from_env(
        _env(
            tmp_path,
            DRIVER_ASSIGNMENT_JSON=_assignment(world),
            DRIVER_EXIT_AFTER_MATCH="1",
            AGENT_BOOT_WAIT_SECONDS="0.05",
            AGENT_RETRY_INTERVAL_SECONDS="0.01",
        )
    )
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app):
        wait_for(lambda: app.state.should_exit or None, timeout=2.0)
    assert app.state.should_exit is True
