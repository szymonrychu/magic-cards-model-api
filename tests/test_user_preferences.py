"""Per-OIDC-user preferences (speed-ai-seat): the play UI's speed-mode stop
settings, remembered per human across every device rather than per browser
alone.

Two layers, mirroring `test_repository_contract.py`'s own split:

- `repo` contract tests: `GameRepository.get_user_preferences`/
  `set_user_preferences`, parameterised over `InMemoryRepository` and a
  postgres factory - the same `_REPOSITORY_FACTORIES` pattern, kept local to
  this file rather than editing the shared one.
- `GET`/`PUT /api/me/preferences` route tests, through the real app with a
  real signed bearer token (`oidc_testkit`, the same throwaway-RSA-keypair
  machinery `test_oidc.py`/`test_user_separation.py` already use - no
  network, no real IdP).
"""

from __future__ import annotations

import pytest
from conftest import require_postgres_database_url
from oidc_testkit import ISSUER, generate_keypair, jwks, token
from sqlalchemy import text

from magic_cards_model_api.db import make_engine
from magic_cards_model_api.repository import GameRepository, InMemoryRepository
from magic_cards_model_api.repository_postgres import PostgresRepository
from magic_cards_model_api.schemas import USER_PREFERENCES_MAX_BYTES

_TEST_DATABASE_URL_ENV = "MCM_TEST_DATABASE_URL"
_pg_engine = None


def _postgres_repository_factory() -> PostgresRepository:
    global _pg_engine
    url = require_postgres_database_url(_TEST_DATABASE_URL_ENV)
    if _pg_engine is None:
        _pg_engine = make_engine(url)
    with _pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE user_preferences"))
    return PostgresRepository(url, engine=_pg_engine)


_REPOSITORY_FACTORIES = [InMemoryRepository, _postgres_repository_factory]


@pytest.fixture(params=_REPOSITORY_FACTORIES, ids=lambda factory: factory.__name__)
def repo(request: pytest.FixtureRequest) -> GameRepository:
    return request.param()


# ---------------------------------------------------------------------------
# repository contract
# ---------------------------------------------------------------------------


def test_get_is_none_for_a_sub_that_never_wrote(repo: GameRepository) -> None:
    assert repo.get_user_preferences("alice") is None


def test_set_then_get_round_trips_the_preferences(repo: GameRepository) -> None:
    repo.set_user_preferences("alice", {"autoForced": False, "tappedToast": True})

    stored = repo.get_user_preferences("alice")

    assert stored is not None
    assert stored["preferences"] == {"autoForced": False, "tappedToast": True}
    assert stored["updated_at"]


def test_set_returns_the_same_stamp_get_reports(repo: GameRepository) -> None:
    updated_at = repo.set_user_preferences("alice", {"a": 1})

    stored = repo.get_user_preferences("alice")

    assert stored is not None
    assert stored["updated_at"] == updated_at


def test_a_second_set_replaces_rather_than_merges(repo: GameRepository) -> None:
    repo.set_user_preferences("alice", {"a": 1, "b": 2})

    repo.set_user_preferences("alice", {"c": 3})

    stored = repo.get_user_preferences("alice")
    assert stored is not None
    assert stored["preferences"] == {"c": 3}


def test_different_subs_are_independent(repo: GameRepository) -> None:
    repo.set_user_preferences("alice", {"a": 1})
    repo.set_user_preferences("bob", {"b": 2})

    assert repo.get_user_preferences("alice")["preferences"] == {"a": 1}  # type: ignore[index]
    assert repo.get_user_preferences("bob")["preferences"] == {"b": 2}  # type: ignore[index]


def test_the_postgres_factory_is_parameterised_into_this_suite() -> None:
    assert _postgres_repository_factory in _REPOSITORY_FACTORIES


# ---------------------------------------------------------------------------
# GET/PUT /api/me/preferences
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def private_key():
    return generate_keypair()


@pytest.fixture()
def configured_env(monkeypatch: pytest.MonkeyPatch, private_key) -> None:
    import json

    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_JWKS_STATIC", json.dumps(jwks(private_key)))
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)


def _auth(private_key, sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(private_key, sub=sub)}"}


def test_get_with_no_bearer_token_is_404(client) -> None:
    response = client.get("/api/me/preferences")

    assert response.status_code == 404, response.text


def test_put_with_no_bearer_token_is_404(client) -> None:
    response = client.put("/api/me/preferences", json={"preferences": {"a": 1}})

    assert response.status_code == 404, response.text


def test_get_defaults_to_empty_for_a_first_time_user(configured_env, private_key, client) -> None:
    response = client.get("/api/me/preferences", headers=_auth(private_key, "alice"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preferences"] == {}
    assert body.get("updated_at") is None


def test_put_then_get_round_trips(configured_env, private_key, client) -> None:
    put = client.put(
        "/api/me/preferences",
        json={"preferences": {"autoForced": False, "tappedToast": True}},
        headers=_auth(private_key, "alice"),
    )
    assert put.status_code == 200, put.text
    assert put.json()["preferences"] == {"autoForced": False, "tappedToast": True}
    assert put.json()["updated_at"]

    got = client.get("/api/me/preferences", headers=_auth(private_key, "alice"))
    assert got.status_code == 200, got.text
    assert got.json()["preferences"] == {"autoForced": False, "tappedToast": True}


def test_a_second_put_replaces_rather_than_merges(configured_env, private_key, client) -> None:
    client.put(
        "/api/me/preferences",
        json={"preferences": {"a": 1, "b": 2}},
        headers=_auth(private_key, "alice"),
    )

    client.put(
        "/api/me/preferences", json={"preferences": {"c": 3}}, headers=_auth(private_key, "alice")
    )

    got = client.get("/api/me/preferences", headers=_auth(private_key, "alice"))
    assert got.json()["preferences"] == {"c": 3}


def test_two_users_preferences_are_independent(configured_env, private_key, client) -> None:
    client.put(
        "/api/me/preferences",
        json={"preferences": {"who": "alice"}},
        headers=_auth(private_key, "alice"),
    )
    client.put(
        "/api/me/preferences",
        json={"preferences": {"who": "bob"}},
        headers=_auth(private_key, "bob"),
    )

    alice = client.get("/api/me/preferences", headers=_auth(private_key, "alice"))
    bob = client.get("/api/me/preferences", headers=_auth(private_key, "bob"))

    assert alice.json()["preferences"] == {"who": "alice"}
    assert bob.json()["preferences"] == {"who": "bob"}


def test_an_oversized_preferences_body_is_a_typed_422(configured_env, private_key, client) -> None:
    huge = {"pad": "x" * (USER_PREFERENCES_MAX_BYTES + 1)}

    response = client.put(
        "/api/me/preferences", json={"preferences": huge}, headers=_auth(private_key, "alice")
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert any(err.get("type") == "preferences_too_large" for err in detail), detail


def test_a_body_within_the_cap_is_accepted(configured_env, private_key, client) -> None:
    fits = {"pad": "x" * (USER_PREFERENCES_MAX_BYTES - 64)}

    response = client.put(
        "/api/me/preferences", json={"preferences": fits}, headers=_auth(private_key, "alice")
    )

    assert response.status_code == 200, response.text
