"""Run the real app on a real `PostgresRepository` for the length of one test.

`store.py` picks the process-wide `store` singleton once, at import, and a
dozen modules (`app.py`, `game_access.py`, `lobby.py`, `serialize.py`, ...
and `conftest.py` itself) bind that object by name. `mise run test:pg` sets
only `MCM_TEST_DATABASE_URL`, not `DATABASE_URL`, so under it the app still
runs on `InMemoryRepository` - which is how a postgres-only codec bug reached
production with every api test green. `postgres_store` swaps every binding of
the singleton it can find in `sys.modules` for a fresh `PostgresRepository`
over truncated tables, and `monkeypatch` puts them all back afterwards.

Skips under the plain `mise run test`, fails loudly under `mise run test:pg`
(`conftest.require_postgres_database_url`).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from conftest import require_postgres_database_url
from sqlalchemy import text

from magic_cards_model_api import store as store_module
from magic_cards_model_api.db import make_engine
from magic_cards_model_api.repository_postgres import PostgresRepository

TEST_DATABASE_URL_ENV = "MCM_TEST_DATABASE_URL"


@contextmanager
def postgres_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[PostgresRepository]:
    url = require_postgres_database_url(TEST_DATABASE_URL_ENV)
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE record_states, records, games RESTART IDENTITY CASCADE"))
    repo = PostgresRepository(url, engine=engine)
    original = store_module.store
    for module in list(sys.modules.values()):
        namespace = getattr(module, "__dict__", None)
        if isinstance(namespace, dict) and namespace.get("store") is original:
            monkeypatch.setattr(module, "store", repo)
    try:
        yield repo
    finally:
        engine.dispose()
