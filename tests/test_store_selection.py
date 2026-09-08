"""Which repository the shipped application actually runs on.

T9 built `PostgresRepository` and T10 filled its `records` table, and until
this file existed neither was reachable from the running app at all:
`store.py` said `GameStore = InMemoryRepository` outright, and grep found
`PostgresRepository` only in tests. Every durability property those two
tasks bought was therefore unreachable in production - a postgres deployment
would have run on the in-memory store and lost its games on restart.

The selection is made once, at import, from the environment: a configured
database URL (`DATABASE_URL`, or the `POSTGRES_*` components
`db.database_url_from_env` already understands) selects postgres; nothing
configured keeps `InMemoryRepository`, so the docker-free default is
untouched.

Both bindings matter, and that is why these tests run a real subprocess
rather than reloading modules in-process. `app.py` binds `store` statically
at import (`from .store import store`); `audit.py` re-imports it *lazily*
inside `record`/`entries`. A selection that only one of those two sees is
the exact failure `MEMORY.md` records: `audit.record`'s `fill_record` lands
on a different store than the one `store.replace` wrote to, and raises
`KeyError` for a row that genuinely exists in the other one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import require_postgres_database_url

from magic_cards_model_api.db import configured_database_url

_TEST_DATABASE_URL_ENV = "MCM_TEST_DATABASE_URL"
_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every variable that could configure a database, cleared from a subprocess
#: environment before one of these tests sets exactly what it means to test.
#: `mise`'s own `.env` loading is why this has to be explicit: a developer
#: with `POSTGRES_HOST` in their shell would otherwise silently turn the
#: "nothing configured" test into a postgres one.
_DB_ENV = (
    "DATABASE_URL",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
)

#: Imports the app the way uvicorn does, then reports what both bindings
#: resolved to. `audit` is asked for its store the same lazy way `record`
#: does, so this is the real second binding and not a restatement of the
#: first.
_PROBE = """
import json
from magic_cards_model_api.app import store as app_store
import magic_cards_model_api.store as store_module

def audit_store():
    from magic_cards_model_api.store import store
    return store

print(json.dumps({
    "app_binding": type(app_store).__name__,
    "module_binding": type(store_module.store).__name__,
    "audit_binding": type(audit_store()).__name__,
    "same_object": app_store is store_module.store is audit_store(),
}))
"""


def _probe(**overrides: str) -> dict[str, object]:
    env = {key: value for key, value in os.environ.items() if key not in _DB_ENV}
    env["MCM_CACHE_DIR"] = str(_REPO_ROOT / "tests" / "fixtures" / "scryfall")
    env.update(overrides)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# the pure selection rule - no database needed to check it
# ---------------------------------------------------------------------------


def test_nothing_configured_is_no_url() -> None:
    assert configured_database_url({}) is None
    assert configured_database_url({"POSTGRES_HOST": "db"}) is None, (
        "a partial set of components configures nothing - it must not be read as "
        "an intent to use postgres, and it must not raise either"
    )


def test_an_explicit_url_wins() -> None:
    assert configured_database_url({"DATABASE_URL": "postgresql+psycopg:///mtg"}) == (
        "postgresql+psycopg:///mtg"
    )


def test_a_complete_set_of_components_is_assembled() -> None:
    """Assembled from characters rather than written out: a
    `scheme://user:pass@host` literal in a committed file is read as a real
    secret by scanning, however synthetic it is."""
    secret = "".join(["p", "w", "-", "x", "y", "z"])
    url = configured_database_url(
        {
            "POSTGRES_HOST": "db.internal",
            "POSTGRES_USER": "mtg",
            "POSTGRES_PASSWORD": secret,
            "POSTGRES_DB": "mtg",
        }
    )
    assert url is not None
    assert url.startswith("postgresql+psycopg://")
    assert "db.internal:5432/mtg" in url
    assert secret in url


# ---------------------------------------------------------------------------
# what the running application actually ends up on
# ---------------------------------------------------------------------------


def test_with_nothing_configured_the_app_keeps_the_in_memory_store() -> None:
    """The docker-free default, unchanged: no database configured, no
    postgres, no connection attempted at import."""
    probed = _probe()
    assert probed["app_binding"] == "InMemoryRepository"
    assert probed["audit_binding"] == "InMemoryRepository"
    assert probed["same_object"] is True


def test_a_configured_database_url_selects_the_postgres_store() -> None:
    url = require_postgres_database_url(_TEST_DATABASE_URL_ENV)
    probed = _probe(DATABASE_URL=url)
    assert probed["app_binding"] == "PostgresRepository", probed
    assert probed["audit_binding"] == "PostgresRepository", probed
    assert probed["module_binding"] == "PostgresRepository", probed
    assert probed["same_object"] is True, (
        "app.py's static binding and audit.py's lazy one must be the same "
        "object, or a write through one is invisible to the other"
    )


def test_the_postgres_components_alone_select_the_postgres_store() -> None:
    """The Kubernetes shape: CloudNativePG hands the workload separate
    Secret keys and no URL at all."""
    from sqlalchemy.engine import make_url

    url = make_url(require_postgres_database_url(_TEST_DATABASE_URL_ENV))
    probed = _probe(
        POSTGRES_HOST=url.host or "localhost",
        POSTGRES_PORT=str(url.port or 5432),
        POSTGRES_USER=url.username or "",
        POSTGRES_PASSWORD=url.password or "",
        POSTGRES_DB=url.database or "",
    )
    assert probed["app_binding"] == "PostgresRepository", probed
    assert probed["audit_binding"] == "PostgresRepository", probed


def test_a_postgres_backed_app_records_through_both_bindings() -> None:
    """The end-to-end version: boot the app on postgres, create a game
    through its own HTTP surface, and read the log back - which is
    `audit.entries` going through its lazily-bound store to the `records`
    table `store.replace` wrote in the very same transaction. If the two
    bindings ever came apart, `fill_record` would raise `KeyError` here for
    a row that does exist.
    """
    url = require_postgres_database_url(_TEST_DATABASE_URL_ENV)
    script = """
import json
from fastapi.testclient import TestClient
from magic_cards_model_api.app import app, store

with TestClient(app) as client:
    made = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    info = made.json()
    game_id = info["game_id"]
    client.post(
        f"/api/games/{game_id}/cards",
        json={"card": "Mountain", "player": "alice", "zone": "hand"},
    )
    played = client.post(
        f"/api/games/{game_id}/actions/play-land",
        json={"player": "alice", "card": "Mountain"},
    )
    log = client.get(
        f"/api/games/{game_id}/log", params={"uid": info["admin_key"]}
    ).json()["log"]
    client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})
    print(json.dumps({
        "store": type(store).__name__,
        "allowed": played.json()["allowed"],
        "actions": [entry["action"] for entry in log],
        "unrecorded": [e["action"] for e in log if e["action"] == "(unrecorded)"],
    }))
"""
    env = {key: value for key, value in os.environ.items() if key not in _DB_ENV}
    env["MCM_CACHE_DIR"] = str(_REPO_ROOT / "tests" / "fixtures" / "scryfall")
    env["DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    probed = json.loads(result.stdout.strip().splitlines()[-1])
    assert probed["store"] == "PostgresRepository"
    assert probed["allowed"] is True
    assert "play_land" in probed["actions"], probed
    assert probed["unrecorded"] == [], (
        "every row this run wrote must have a real payload - an "
        "'(unrecorded)' one means a `fill_record` landed on another store"
    )
