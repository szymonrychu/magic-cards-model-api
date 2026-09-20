"""`db.make_engine`'s own engine construction. No real Postgres is required
here - `create_engine` builds the `Engine` and its pool lazily, without
opening a connection, so a URL to a host that does not exist is enough to
inspect what the engine was configured with."""

from __future__ import annotations

from magic_cards_model_api.db import make_engine

FAKE_URL = "postgresql+psycopg://user:pass@nonexistent-host-xyz:5432/db"


def test_make_engine_pre_pings_its_pool() -> None:
    """A pooled connection that has gone stale (the far side closed it, or a
    network blip dropped it) is invisible to the pool until something tries
    to use it - `pool_pre_ping` makes the pool test a connection with a
    cheap `SELECT 1` before handing it out, so a request gets a fresh
    connection instead of surfacing a dead one as a request failure. Without
    it, a repository built once at process start and reused for the whole
    process lifetime (`store.py`'s own pattern) will eventually hand out a
    connection Postgres or a proxy in front of it has already dropped."""
    engine = make_engine(FAKE_URL)

    assert engine.pool._pre_ping is True
