"""`conftest.require_postgres_database_url` is what every postgres-only test
(the contract suite's postgres parameterisation, the CAS races) asks for its
database DSN through, instead of inlining its own `pytest.skip`. The point:
`mise run test:pg` sets `MCM_REQUIRE_POSTGRES_TESTS=1`, and with that set, a
missing database URL must be a hard failure, not a quiet skip - so a drifted
env var name (or any other reason the URL never got set) cannot leave the
postgres suite silently unexecuted while CI still reports green.
"""

from __future__ import annotations

import pytest
from conftest import MCM_REQUIRE_POSTGRES_TESTS, require_postgres_database_url

_ENV = "MCM_TEST_DATABASE_URL_FOR_THIS_TEST"


def test_returns_the_url_when_it_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "postgresql://example/db")
    assert require_postgres_database_url(_ENV) == "postgresql://example/db"


def test_skips_when_unset_and_not_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.delenv(MCM_REQUIRE_POSTGRES_TESTS, raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_postgres_database_url(_ENV)


def test_fails_loudly_when_unset_and_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setenv(MCM_REQUIRE_POSTGRES_TESTS, "1")
    with pytest.raises(pytest.fail.Exception):
        require_postgres_database_url(_ENV)
