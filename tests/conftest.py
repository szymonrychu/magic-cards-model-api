"""Shared fixtures. Card data comes only from the recorded Scryfall fixtures
in tests/fixtures/scryfall - the network is never touched."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ["MCM_CACHE_DIR"] = str(Path(__file__).parent / "fixtures" / "scryfall")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from magic_cards_model.machines import Step  # noqa: E402

from magic_cards_model_api import ai_seat, lobby  # noqa: E402
from magic_cards_model_api.main import app  # noqa: E402
from magic_cards_model_api.store import store  # noqa: E402

#: Set only by `mise run test:pg` - never by the plain `mise run test`, which
#: explicitly `unset`s it. With this set, a postgres-only test that cannot
#: get a database URL must fail loudly (`require_postgres_database_url`
#: below), not skip: a plain `pytest.skip` inside a fixture is invisible to
#: CI's pass/fail line, so a variable name that ever drifts between
#: mise.toml/CI and a test file would otherwise leave the entire postgres
#: suite silently unexecuted while `test:pg` still reports success.
MCM_REQUIRE_POSTGRES_TESTS = "MCM_REQUIRE_POSTGRES_TESTS"


def require_postgres_database_url(env_var: str) -> str:
    """The postgres DSN read from `env_var`, or a skip/hard-failure depending
    on whether this run is required to actually exercise postgres. Every
    postgres-only fixture (the contract suite's postgres parameterisation,
    the CAS race tests) calls this instead of inlining its own
    `pytest.skip`, so the fail-instead-of-skip behaviour lives in exactly one
    place."""
    url = os.environ.get(env_var)
    if url:
        return url
    if os.environ.get(MCM_REQUIRE_POSTGRES_TESTS):
        pytest.fail(
            f"{env_var} is not set, but {MCM_REQUIRE_POSTGRES_TESTS}=1 (set by "
            "`mise run test:pg`) - the postgres suite is required to actually "
            "run, not skip"
        )
    pytest.skip(
        f"{env_var} is not set - postgres tests are skipped (run `mise run "
        "test:pg` to exercise them)"
    )
    raise AssertionError("unreachable: pytest.skip/pytest.fail always raise")


def _xdist_worker_schema() -> str | None:
    """This xdist worker's private postgres schema name (`test_gw0`, ...), or
    `None` outside xdist. `PYTEST_XDIST_WORKER` is set by pytest-xdist inside
    each worker subprocess (`gw0`, `gw1`, ...) and is unset for a plain,
    un-parallel `pytest` run - which keeps using whatever schema `mise run
    test:pg`'s own `alembic upgrade head` step already migrated (`public`),
    unchanged from before xdist existed."""
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    return f"test_{worker_id}" if worker_id else None


def _schema_scoped_database_url(base_url: str, schema: str) -> str:
    """`base_url` with its connection's `search_path` pinned to `schema`
    *alone* - the standard psycopg/libpq `options=-c search_path=...`
    parameter, carried as a URL query param so every consumer that already
    just forwards this URL unchanged (SQLAlchemy's `create_engine`,
    alembic's `env.py` via `database_url_from_env`) gets the isolated schema
    for free, with no schema-aware code of its own.

    Deliberately *not* `<schema>,public`: alembic decides whether a
    migration has already run by reading `alembic_version` through this same
    search_path, and `mise run test:pg`'s own bring-up step has already
    migrated `public` to head before pytest ever starts. With `public` as a
    fallback, a brand-new, still-empty worker schema resolves
    `alembic_version` straight through to `public.alembic_version` - already
    stamped at head - so `command.upgrade(cfg, "head")` sees nothing to do
    and silently leaves the worker schema with no tables at all, in which
    case every unqualified table lookup for the rest of that worker's run
    falls through to `public` too (`games`/`records`, shared with every
    other worker's `public`-only fallback) - the one thing this whole
    mechanism exists to prevent. There is no other schema-qualified object
    (extension, etc.) this codebase's migrations need, so a bare `schema`
    is sufficient."""
    from sqlalchemy.engine import make_url

    url = make_url(base_url)
    query = dict(url.query)
    query["options"] = f"-csearch_path={schema}"
    return url.set(query=query).render_as_string(hide_password=False)


def _provision_xdist_worker_schema(base_url: str, schema: str) -> None:
    """Create `schema` on the shared postgres `mise run test:pg` already
    brought up, then migrate it to head - once per worker process, before
    any test in that worker touches postgres. Every worker runs this against
    a *different* schema name, so six workers doing this at once (`-n 6`)
    never contend with each other."""
    from sqlalchemy import create_engine, text

    engine = create_engine(base_url, future=True)
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    finally:
        engine.dispose()

    from alembic.config import Config

    from alembic import command

    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    scoped_url = _schema_scoped_database_url(base_url, schema)
    # `%%`, not `%`: `Config.set_main_option` stores the value through
    # `configparser`, whose `BasicInterpolation` treats a lone `%` as the
    # start of an interpolation reference and refuses anything else - which
    # `options=-csearch_path%3D...` (the URL's own percent-encoding of `=`
    # and `,`) always is. Doubling it here is exactly what interpolation
    # undoes on the way back out (`get_main_option`/`get_section`), so
    # `env.py` sees the URL unchanged.
    cfg.set_main_option("sqlalchemy.url", scoped_url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def pytest_configure(config: pytest.Config) -> None:
    """Under `mise run test:pg -n 6`, give each xdist worker its own postgres
    schema so the six worker processes sharing the one `docker compose up -d
    postgres` instance cannot race each other's tables -
    `tests/postgres_store_testkit.py`'s `TRUNCATE TABLE ... games` at the top
    of every postgres-backed test would otherwise wipe out whatever another
    worker's in-flight test had just written, or a unique-constraint
    collision would fire between two workers' rows that would never have
    coexisted serially.

    Rewriting `MCM_TEST_DATABASE_URL` itself - rather than editing the ~15
    test files that independently call `require_postgres_database_url`
    directly - makes every one of them pick up the isolated schema with no
    per-file change: they all just read the env var this function has
    already repointed by the time any test runs.

    A no-op outside xdist (`_xdist_worker_schema` returns `None`) and outside
    `test:pg` (`MCM_TEST_DATABASE_URL` unset) - `config` is unused, required
    only by the `pytest_configure` hook signature.
    """
    del config
    base_url = os.environ.get("MCM_TEST_DATABASE_URL")
    if not base_url:
        return
    schema = _xdist_worker_schema()
    if schema is None:
        return
    _provision_xdist_worker_schema(base_url, schema)
    os.environ["MCM_TEST_DATABASE_URL"] = _schema_scoped_database_url(base_url, schema)


def pytest_terminal_summary(terminalreporter: Any, config: pytest.Config) -> None:
    """Print the shuffle seed where `-q` (this repo's `addopts`) still shows it.

    pytest-randomly announces `Using --randomly-seed=N` in the report header,
    which `-q` suppresses, so a failing shuffled CI run would not say which seed
    to replay. Not printed under `-p no:randomly`, where there is no seed."""
    seed = getattr(config.option, "randomly_seed", None)
    if seed is not None:
        terminalreporter.write_line(f"Using --randomly-seed={seed}")


#: The methods the harness credential is added to. `game_access.py`'s
#: positive rule also covers `DELETE`, and this deliberately does not:
#: `app.delete_game` and `decks_api.end_lobby_game` have always required a
#: seat uid or the admin key of their own, so no `DELETE` call site in this
#: suite was ever relying on presenting nothing - and adding a key to one
#: would silently turn `test_games.py::
#: test_delete_without_credentials_is_refused` green for the wrong reason.
_HARNESS_CREDENTIAL_METHODS = frozenset({"POST", "PUT", "PATCH"})


def _game_id_in(url: str) -> str | None:
    """The `{game_id}` segment of `url`, or `None` for a path that names no
    game. Both routers spell it the same way - `/api/games/<id>/...` and
    `/api/lobby/games/<id>/...` - so one "the segment after `games`" rule
    covers them."""
    segments = url.split("?")[0].split("#")[0].strip("/").split("/")
    if "games" not in segments:
        return None
    index = segments.index("games")
    return segments[index + 1] if len(segments) > index + 1 else None


def _already_credentialled(url: str, params: object, body: object) -> bool:
    if "uid=" in url or "key=" in url:
        return True
    for source in (params, body):
        if isinstance(source, dict) and ("uid" in source or "key" in source):
            return True
    return False


@pytest.fixture(autouse=True)
def _harness_credential(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """The test harness's own credential, presented on any mutating
    game-scoped request that carries none.

    `game_access.py` now requires a credential of every mutating call on a
    `{game_id}` route - a deny-list keyed on a uid cannot restrict a caller
    who omits the uid, which is the hole this fixture's counterpart,
    `tests/test_mutation_needs_a_credential.py`, pins closed. 469 call sites
    across 81 files in this suite presented nothing at all, because until
    now nothing made them.

    They are not 469 separate bugs, and the fix is not 469 edits. Every one
    of them is *this process*, driving a game *it created in this process*,
    and the credential for exactly that is the game's admin key -
    `store.admin_key` hands it over, so the harness is presenting a
    credential it genuinely holds rather than being handed an exemption. The
    key goes in `key=`, never over a `uid`/`key` a test supplied itself, and
    only ever for a game the store still knows.

    **Opt out with `@pytest.mark.no_harness_credential`** in any test whose
    subject is the credential rule itself -
    `tests/test_mutation_needs_a_credential.py` and
    `tests/test_user_separation.py` both do, and a new security test that
    forgets to will pass for the wrong reason. That is the one hazard this
    fixture carries, and it is the reason it is a marker rather than
    something implicit.

    Patched onto `TestClient.request` rather than wrapped around the
    `client` fixture on purpose: ~20 tests build a bare `TestClient(app)` of
    their own, or hand one to `seatkit` through
    `monkeypatch.setattr(httpx, "Client", ...)`, and every one of those has
    to be covered too.
    """
    if "no_harness_credential" in request.keywords:
        return
    original = TestClient.request

    def request_with_harness_credential(self, method, url, **kwargs):  # noqa: ANN001, ANN202
        if str(method).upper() in _HARNESS_CREDENTIAL_METHODS:
            target = str(url)
            params = kwargs.get("params")
            if not _already_credentialled(target, params, kwargs.get("json")):
                game_id = _game_id_in(target)
                key = store.admin_key(game_id) if game_id else None
                if key is not None and (params is None or isinstance(params, dict)):
                    kwargs["params"] = {**(params or {}), "key": key}
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(TestClient, "request", request_with_harness_credential)


def _no_cluster(*args: object, **kwargs: object) -> None:
    raise RuntimeError(
        "the test suite never reaches a Kubernetes cluster - install a fake with "
        "k8s_seats.set_for_tests(...)"
    )


class _NoClusterBatch:
    """An empty in-memory stand-in for `BatchV1Api`: the default seat
    backend every test gets, so a test that switches to
    `AI_SEAT_MODE=kubernetes` without installing its own fake schedules
    into nothing rather than into whatever cluster the machine can reach."""

    def __init__(self) -> None:
        self.jobs: dict[str, SimpleNamespace] = {}

    def create_namespaced_job(self, namespace: str, body: dict) -> SimpleNamespace:
        name = body["metadata"]["name"]
        self.jobs[name] = SimpleNamespace(
            metadata=SimpleNamespace(name=name, labels=body["metadata"]["labels"]),
            status=SimpleNamespace(active=1, succeeded=0, failed=0),
        )
        return self.jobs[name]

    def list_namespaced_job(self, namespace: str, label_selector: str | None = None, **kw):
        return SimpleNamespace(items=list(self.jobs.values()))

    def delete_namespaced_job(self, name: str, namespace: str, body: object = None) -> None:
        self.jobs.pop(name, None)


class _NoClusterCore:
    def list_namespaced_pod(self, namespace: str, label_selector: str | None = None, **kw):
        return SimpleNamespace(items=[])


@pytest.fixture(autouse=True)
def _no_real_cluster(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """2026-09-27: a test set `AI_SEAT_MODE=kubernetes` without stubbing
    `k8s_seats.get`, which then loaded the laptop's kubeconfig - prod - and
    reconciled a live game's seat Job away. Three independent lines now:
    every kubernetes config loader raises, `KUBECONFIG` points nowhere, and
    `get()` hands out an in-memory fake backend unless a test installs its
    own."""
    import kubernetes.config as k8s_config
    from kubernetes.config import incluster_config, kube_config

    from magic_cards_model_api import k8s_seats

    monkeypatch.setenv("KUBECONFIG", "/nonexistent")
    monkeypatch.delenv(k8s_seats.OUT_OF_CLUSTER_ENV, raising=False)
    monkeypatch.setenv(k8s_seats.INSTANCE_ENV, "pytest")
    for module in (k8s_config, kube_config):
        for name in (
            "load_kube_config",
            "load_kube_config_from_dict",
            "new_client_from_config",
            "new_client_from_config_dict",
            "load_config",
        ):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, _no_cluster)
    for module in (k8s_config, incluster_config):
        monkeypatch.setattr(module, "load_incluster_config", _no_cluster)
    k8s_seats.set_for_tests(
        k8s_seats.K8sSeats(
            k8s_seats.K8sSettings(instance_id="pytest"), _NoClusterBatch(), _NoClusterCore()
        )
    )
    yield
    k8s_seats.set_for_tests(None)


@pytest.fixture(autouse=True)
def _no_real_claude_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POST /api/lobby/games` now starts each `ai: true` seat itself, right
    after booting the game - which, in `subprocess` mode, means a game
    created in a test would otherwise spawn a *real* `claude` process the
    moment this dev machine happens to have one on PATH (it does).

    `AiSeatRegistry.start` already refuses to spawn when `claude_available()`
    is false, reporting a harmless fallback instead - so defaulting it to
    false here is already enough to keep every test safe without touching the
    mechanism itself. `command_for` is defaulted to a harmless stand-in too,
    as a second, independent line of defense at the actual spawn boundary -
    belt and suspenders against a future bug that calls `subprocess.Popen`
    without checking `claude_available()` first.

    Tests that exercise the real spawn path or the real argv/fallback text
    (`test_ai_seat.py`, `test_ai_seat_settings.py`, `test_lobby_reset.py`,
    `test_mulligans.py`) override one or both back themselves - `command_for`
    either to their own stand-in or to the real function captured at import
    time, before this fixture ever ran."""
    monkeypatch.setattr(ai_seat, "claude_available", lambda: False)
    monkeypatch.setattr(
        ai_seat, "command_for", lambda prompt, agent: ["python3", "-c", "print('DONE')"]
    )


@pytest.fixture
def unreadable_charm(monkeypatch: pytest.MonkeyPatch) -> str:
    """Makes `synthetic_cards_testkit.UNREADABLE_CHARM_NAME` resolve through
    `scryfall.fetch_card`, the seam `add_card` reaches; every other name
    still takes the real lookup. Returns the card's name."""
    from synthetic_cards_testkit import UNREADABLE_CHARM_NAME, UNREADABLE_CHARM_RAW

    from magic_cards_model_api import scryfall

    real_fetch_card = scryfall.fetch_card

    def fetch_card_or_unreadable_charm(name: str) -> dict:
        if name == UNREADABLE_CHARM_NAME:
            return UNREADABLE_CHARM_RAW
        return real_fetch_card(name)

    monkeypatch.setattr(scryfall, "fetch_card", fetch_card_or_unreadable_charm)
    return UNREADABLE_CHARM_NAME


@pytest.fixture
def unchargeable_cost_card(monkeypatch: pytest.MonkeyPatch) -> str:
    """Makes `synthetic_cards_testkit.UNCHARGEABLE_COST_NAME` resolve through
    `scryfall.fetch_card`, like `unreadable_charm`. Returns the card's name."""
    from synthetic_cards_testkit import UNCHARGEABLE_COST_NAME, UNCHARGEABLE_COST_RAW

    from magic_cards_model_api import scryfall

    real_fetch_card = scryfall.fetch_card

    def fetch_card_or_synthetic(name: str) -> dict:
        if name == UNCHARGEABLE_COST_NAME:
            return UNCHARGEABLE_COST_RAW
        return real_fetch_card(name)

    monkeypatch.setattr(scryfall, "fetch_card", fetch_card_or_synthetic)
    return UNCHARGEABLE_COST_NAME


def add_card(client: TestClient, game_id: str, name: str, player: str, zone: str, **kwargs):
    response = client.post(
        f"/api/games/{game_id}/cards",
        json={"card": name, "player": player, "zone": zone, **kwargs},
    )
    assert response.status_code == 200, response.text
    return response.json()


_ADMIN_KEYS: dict[str, str] = {}  # game_id -> admin key, stashed by the `game` fixture


def state(client: TestClient, game_id: str, uid: str | None = None) -> dict:
    """The board, filtered exactly as the API filters it for `uid`.

    With no `uid`, this reaches for the admin key the `game` fixture stashed
    at creation - the harness built this board, so it gets the harness's own
    full view, the same as any other operator holding that key. Tests that
    want the *filtered*, seat-scoped view (what a real player sees) pass a
    real seat uid explicitly.
    """
    if uid is None:
        uid = _ADMIN_KEYS.get(game_id)
    response = client.get(f"/api/games/{game_id}", params={"uid": uid} if uid else None)
    assert response.status_code == 200, response.text
    return response.json()


def find(cards: list[dict], name: str) -> dict:
    matches = [card for card in cards if card["name"] == name]
    assert matches, f"no {name} among {[card['name'] for card in cards]}"
    return matches[0]


def force_turn_position(game_id: str, step: Step, active_player: str) -> None:
    """Reach past the API into the live engine's own `Turn`/`PriorityMachine`
    to position a game at `step` for `active_player`, without replaying the
    turn machine to get there - legitimate test setup (the same "reach past
    the API, straight into the live engine object" idiom `test_special_
    actions.py`'s `_bump_turn` already used before this helper existed), and
    never a substitute for driving the real transition into the step under
    test, which every caller still does through the API afterwards.

    `PriorityMachine` tracks its own `active_index`/`holder_index` rather
    than reading `Turn`'s - `Game.advance` re-syncs it with `self.priority.
    reset(self.turn.active_index)` on every real turn rollover, so this does
    the same rather than only calling `sync_priority_with_step`, which
    grants or withholds for the step now in progress but never moves whose
    turn it resumes at (`PriorityMachine.grant`'s own docstring: "the seat
    it resumes at is unchanged").
    """
    game = store.get(game_id)
    assert game is not None and game.turn is not None and game.priority is not None
    game.turn.step = step
    game.turn.active_index = game.turn.players.index(active_player)
    game.priority.reset(game.turn.active_index)
    game.sync_priority_with_step()


def resolve_whole_stack(client: TestClient, game_id: str) -> list[str]:
    """Resolve every object on the stack, top down. Returns what resolved,
    in order - the caller's own way to drain triggers (CR 702.62a's suspend
    countdown among them, now a real triggered ability rather than a
    response field reporting a hand-driven removal).

    Refuses to loop when a resolution pauses on a decision (`resolved: None`,
    `paused: True`) - CR 702.62a's third ability now asks its own "you may
    cast it" (`Game.finish_suspend`, via `cast_during_resolution`) while it
    resolves, the same way cascade's hit does, so a stack that needs a
    `POST .../decide` answer partway through no longer drains on its own.
    Looping past that silently retried the same paused command forever
    (`body["resolved"]` reads `None` and the stack never shrinks) rather than
    failing loudly - a caller that expects a decision mid-drain must answer
    it itself and call this again for what is left."""
    resolved: list[str] = []
    while state(client, game_id)["stack"]:
        body = client.post(f"/api/games/{game_id}/resolve-top").json()
        assert body.get("paused") is not True, (
            f"resolve_whole_stack hit a pending decision it cannot answer: "
            f"{body.get('pending_decision')}"
        )
        resolved.append(body["resolved"])
    return resolved


def card_id(client: TestClient, game_id: str, name: str, player: str, zone: str) -> str:
    """Add `name` to `player`'s `zone` and return the id it was dealt.

    `hands` and `graveyards` in `state()` are per-player views; `battlefield`
    and `exile` are shared across both players, so only the former two are
    looked up by `player`.
    """
    add_card(client, game_id, name, player, zone)
    board = state(client, game_id)
    per_player = {"hand": board["hands"], "graveyard": board["graveyards"]}
    pool = per_player[zone][player] if zone in per_player else board[zone]
    return find(pool, name)["id"]


def life_of(state_dict: dict, username: str) -> int:
    return next(player for player in state_dict["players"] if player["username"] == username)[
        "life"
    ]


#: The process-wide singleton, captured at import. Not the module-level `store`
#: name: `postgres_store_testkit.postgres_store` rebinds that name in every module
#: (this one included) to a throwaway `PostgresRepository` for the length of a
#: test, and that swap is still in place when the guard tears down, so the guard
#: would diff the singleton before against a different repository after.
_SINGLETON_STORE = store


def _shared_state_snapshot() -> dict[str, set[str]]:
    """What the process-wide singletons hold right now, by kind.

    `store` (games, user preferences) and the three module-level sim managers
    are bound once at import and shared by every test in the process, so
    anything a test leaves in them is seen by whichever test runs next on the
    same xdist worker.
    """
    from magic_cards_model_api.sim import server as sim_server

    return {
        "game": set(_SINGLETON_STORE.ids()),
        "user preference": set(_SINGLETON_STORE.user_preference_subs()),
        "sim sandbox": set(sim_server.manager.live_ids()),
        "sim batch": set(sim_server.batches.live_ids()),
        "sim claude match": set(sim_server.claude.live_ids()),
    }


@contextlib.contextmanager
def leak_guard(nodeid: str) -> Iterator[None]:
    """Fail `nodeid` if the block leaves anything new in the shared singletons.

    Diffs against a snapshot taken on entry rather than asserting emptiness, so
    a session-scoped fixture that legitimately owns a game stays legal. Whatever
    leaked is cleaned up first (games deleted, preferences forgotten, sandboxes
    ended), so one leak fails its own culprit and does not cascade into the
    next test as a confusing victim.
    """
    from magic_cards_model_api.sim import server as sim_server
    from magic_cards_model_api.sim.manager import SimRefused

    before = _shared_state_snapshot()
    yield
    after = _shared_state_snapshot()
    leaked = {kind: after[kind] - before[kind] for kind in after if after[kind] - before[kind]}
    if not leaked:
        return
    for game_id in leaked.get("game", ()):
        _SINGLETON_STORE.delete(game_id)
    for sub in leaked.get("user preference", ()):
        _SINGLETON_STORE.delete_user_preferences(sub)
    for sandbox_id in leaked.get("sim sandbox", ()):
        with contextlib.suppress(SimRefused):
            sim_server.manager.end(sandbox_id)
    detail = ", ".join(f"{len(ids)} {kind}(s)" for kind, ids in sorted(leaked.items()))
    pytest.fail(f"{nodeid} left {detail} in the shared store", pytrace=False)


@pytest.fixture(autouse=True)
def _no_leaked_games(request):
    """Set up first and torn down last (so after `client`'s own cleanup), which
    makes it fail the test that leaked rather than a later victim of the leak."""
    with leak_guard(request.node.nodeid):
        yield


@pytest.fixture(autouse=True, scope="session")
def _store_empty_at_session_end():
    """The per-test diff cannot see a game a background thread creates after its
    test returned (`ClaudeMatchManager._start_in_background`). By the end of the
    session nothing may remain."""
    yield
    left = store.ids()
    for game_id in left:
        store.delete(game_id)
    if left:
        pytest.fail(f"{len(left)} game(s) left in the shared store at session end", pytrace=False)


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client
    # `store` is a module-level singleton shared by the whole pytest process,
    # not reset between tests just because `TestClient`'s context manager
    # exited. Since `POST /api/lobby/games` no longer resets a previous lobby
    # game on its own (`lobby.py` - End game is the only reset now), a test
    # that boots one and forgets to end or delete it would otherwise leak a
    # live slot into every test that runs after it: `MAX_LIVE_GAMES`
    # eventually refuses a perfectly innocent later test's boot, and
    # `Seats`-style "the one live game" discovery starts pointing at a game
    # nobody in the current test created (`test_mcp_resilience.py`). This is
    # the safety net a well-behaved test does not need (it already deletes
    # what it created), and the one a forgetful test cannot do without: every
    # game still in the store when a test ends is cleared before the next one
    # starts, lobby-booted games through `lobby.reset_game` (so their
    # watcher threads and AI seats are released too, not just deleted out from
    # under them), everything else through a plain `store.delete`.
    for game_id in store.ui_games():
        lobby.reset_game(game_id, reason="test cleanup")
    for game_id in store.ids():
        store.delete(game_id)


@pytest.fixture()
def admin_key(game: str) -> str:
    """The `game` fixture's own admin key - the operator's full-view
    credential, for tests that need to assert what that view still shows."""
    return _ADMIN_KEYS[game]


@pytest.fixture()
def game(client: TestClient) -> str:
    response = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    assert response.status_code == 200, response.text
    info = response.json()
    game_id = info["game_id"]
    admin_key = info["admin_key"]
    _ADMIN_KEYS[game_id] = admin_key
    yield game_id
    _ADMIN_KEYS.pop(game_id, None)
    client.delete(f"/api/games/{game_id}", params={"key": admin_key})
