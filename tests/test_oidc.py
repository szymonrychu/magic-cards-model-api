"""Unit tests for `oidc.py`: settings resolution and bearer-token validation
in isolation, with no HTTP app involved - `test_user_separation.py` covers
the full route-level enforcement this module feeds.

Every valid token here is signed against `oidc_testkit`'s throwaway RSA
keypair and verified against its own JWKS handed to the app via
`OIDC_JWKS_STATIC` - never a real network call, matching this repo's rule
that the network is never touched in tests.
"""

from __future__ import annotations

import json

import pytest
from oidc_testkit import AUDIENCE, ISSUER, generate_keypair, jwks, token
from starlette.requests import Request

from magic_cards_model_api import oidc


@pytest.fixture(scope="session")
def private_key():
    return generate_keypair()


@pytest.fixture()
def configured_env(monkeypatch: pytest.MonkeyPatch, private_key) -> None:
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_JWKS_STATIC", json.dumps(jwks(private_key)))
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw})


# ---------------------------------------------------------------------------
# oidc_settings
# ---------------------------------------------------------------------------


def test_settings_are_none_with_nothing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
    monkeypatch.delenv("OIDC_JWKS_STATIC", raising=False)

    assert oidc.oidc_settings() is None


def test_settings_are_none_with_only_an_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-set environment is "nothing configured", not an error -
    `oidc.py`'s own reasoning, mirroring `db.configured_database_url`."""
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
    monkeypatch.delenv("OIDC_JWKS_STATIC", raising=False)

    assert oidc.oidc_settings() is None


def test_settings_resolve_once_issuer_and_a_jwks_source_are_both_set(
    configured_env: None,
) -> None:
    settings = oidc.oidc_settings()

    assert settings is not None
    assert settings.issuer == ISSUER
    assert settings.audience is None
    assert settings.jwks_static is not None


# ---------------------------------------------------------------------------
# current_user - anonymous and malformed
# ---------------------------------------------------------------------------


def test_no_authorization_header_is_anonymous(configured_env: None) -> None:
    assert oidc.current_user(_request()) is None


def test_non_bearer_scheme_is_refused(configured_env: None) -> None:
    with pytest.raises(Exception) as exc_info:
        oidc.current_user(_request({"Authorization": "Basic dXNlcjpwYXNz"}))
    assert getattr(exc_info.value, "status_code", None) == 401


def test_bearer_with_no_token_is_refused(configured_env: None) -> None:
    with pytest.raises(Exception) as exc_info:
        oidc.current_user(_request({"Authorization": "Bearer"}))
    assert getattr(exc_info.value, "status_code", None) == 401


def test_bearer_token_with_no_oidc_configured_is_refused(
    monkeypatch: pytest.MonkeyPatch, private_key
) -> None:
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
    monkeypatch.delenv("OIDC_JWKS_STATIC", raising=False)
    tok = token(private_key, sub="alice")

    with pytest.raises(Exception) as exc_info:
        oidc.current_user(_request({"Authorization": f"Bearer {tok}"}))
    assert getattr(exc_info.value, "status_code", None) == 401


# ---------------------------------------------------------------------------
# current_user - a real, valid token
# ---------------------------------------------------------------------------


def test_a_valid_token_resolves_to_its_subject(configured_env: None, private_key) -> None:
    tok = token(private_key, sub="alice", email="alice@example.com")

    user = oidc.current_user(_request({"Authorization": f"Bearer {tok}"}))

    assert user is not None
    assert user.sub == "alice"
    assert user.email == "alice@example.com"


def test_two_different_subjects_resolve_to_different_users(
    configured_env: None, private_key
) -> None:
    alice = oidc.current_user(
        _request({"Authorization": f"Bearer {token(private_key, sub='alice')}"})
    )
    bob = oidc.current_user(_request({"Authorization": f"Bearer {token(private_key, sub='bob')}"}))

    assert alice is not None and bob is not None
    assert alice.sub != bob.sub


# ---------------------------------------------------------------------------
# current_user - a token that fails to verify
# ---------------------------------------------------------------------------


def test_expired_token_is_refused(configured_env: None, private_key) -> None:
    tok = token(private_key, sub="alice", expired=True)

    with pytest.raises(Exception) as exc_info:
        oidc.current_user(_request({"Authorization": f"Bearer {tok}"}))
    assert getattr(exc_info.value, "status_code", None) == 401


def test_wrong_issuer_is_refused(configured_env: None, private_key) -> None:
    tok = token(private_key, sub="alice", issuer="https://not-the-configured-issuer.example/")

    with pytest.raises(Exception) as exc_info:
        oidc.current_user(_request({"Authorization": f"Bearer {tok}"}))
    assert getattr(exc_info.value, "status_code", None) == 401


def test_token_signed_by_an_unknown_key_is_refused(configured_env: None) -> None:
    """A different keypair entirely - not in the configured JWKS at all -
    signing a token that otherwise looks perfectly legitimate."""
    forger_key = generate_keypair()
    tok = token(forger_key, sub="alice", kid="not-in-the-jwks")

    with pytest.raises(Exception) as exc_info:
        oidc.current_user(_request({"Authorization": f"Bearer {tok}"}))
    assert getattr(exc_info.value, "status_code", None) == 401


def test_garbage_token_is_refused(configured_env: None) -> None:
    with pytest.raises(Exception) as exc_info:
        oidc.current_user(_request({"Authorization": "Bearer not-a-real-jwt-at-all"}))
    assert getattr(exc_info.value, "status_code", None) == 401


# ---------------------------------------------------------------------------
# audience
# ---------------------------------------------------------------------------


def test_no_audience_configured_accepts_a_token_that_carries_one_anyway(
    configured_env: None, private_key
) -> None:
    """PyJWT's own default would reject this (an app that passes no
    `audience` refuses any token carrying an `aud` claim) - `oidc.py`
    deliberately overrides that, since a real IdP (Keycloak included) always
    sets `aud`, and refusing every token whenever `OIDC_AUDIENCE` is unset
    would make the unset case useless in practice."""
    tok = token(private_key, sub="alice", audience="some-other-client")

    user = oidc.current_user(_request({"Authorization": f"Bearer {tok}"}))

    assert user is not None
    assert user.sub == "alice"


def test_configured_audience_is_required_and_checked(
    monkeypatch: pytest.MonkeyPatch, configured_env: None, private_key
) -> None:
    monkeypatch.setenv("OIDC_AUDIENCE", AUDIENCE)

    matching = token(private_key, sub="alice", audience=AUDIENCE)
    user = oidc.current_user(_request({"Authorization": f"Bearer {matching}"}))
    assert user is not None and user.sub == "alice"

    mismatched = token(private_key, sub="alice", audience="some-other-client")
    with pytest.raises(Exception) as exc_info:
        oidc.current_user(_request({"Authorization": f"Bearer {mismatched}"}))
    assert getattr(exc_info.value, "status_code", None) == 401

    no_aud_at_all = token(private_key, sub="alice")
    with pytest.raises(Exception) as exc_info:
        oidc.current_user(_request({"Authorization": f"Bearer {no_aud_at_all}"}))
    assert getattr(exc_info.value, "status_code", None) == 401


# ---------------------------------------------------------------------------
# a browser page load with a dead token goes to login, never raw JSON 401
# ---------------------------------------------------------------------------

_HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


def _owned_game(client, private_key) -> str:
    response = client.post(
        "/api/games",
        json={"players": [{"username": "alice"}, {"username": "bob"}]},
        headers={"Authorization": f"Bearer {token(private_key, sub='alice')}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["game_id"]


def _login_rd(response) -> str:
    from urllib.parse import parse_qs, urlsplit

    location = urlsplit(response.headers["location"])
    assert (location.scheme, location.netloc, location.path) == ("", "", "/oauth2/start")
    (rd,) = parse_qs(location.query)["rd"]
    return rd


@pytest.mark.no_harness_credential
def test_html_navigation_to_a_game_page_with_an_expired_token_redirects_to_login(
    configured_env: None, private_key, client
) -> None:
    game_id = _owned_game(client, private_key)
    expired = token(private_key, sub="alice", expired=True)

    response = client.get(
        f"/{game_id}/",
        headers={"Authorization": f"Bearer {expired}", "Accept": _HTML_ACCEPT},
        follow_redirects=False,
    )

    assert response.status_code == 302, response.text
    assert _login_rd(response) == f"/{game_id}/"


@pytest.mark.no_harness_credential
def test_the_login_redirect_carries_the_query_string_back(
    configured_env: None, private_key, client
) -> None:
    game_id = _owned_game(client, private_key)
    expired = token(private_key, sub="alice", expired=True)

    response = client.get(
        f"/{game_id}/?seat=abc&x=1",
        headers={"Authorization": f"Bearer {expired}", "Accept": _HTML_ACCEPT},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert _login_rd(response) == f"/{game_id}/?seat=abc&x=1"


@pytest.mark.no_harness_credential
def test_html_navigation_to_the_old_g_address_with_a_garbage_token_redirects_to_login(
    configured_env: None, private_key, client
) -> None:
    game_id = _owned_game(client, private_key)

    response = client.get(
        f"/g/{game_id}/",
        headers={"Authorization": "Bearer not-a-jwt", "Accept": _HTML_ACCEPT},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert _login_rd(response) == f"/g/{game_id}/"


@pytest.mark.no_harness_credential
def test_a_json_request_to_a_game_page_with_an_expired_token_keeps_json_401(
    configured_env: None, private_key, client
) -> None:
    game_id = _owned_game(client, private_key)
    expired = token(private_key, sub="alice", expired=True)

    response = client.get(
        f"/{game_id}/",
        headers={"Authorization": f"Bearer {expired}", "Accept": "application/json"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert response.json()["detail"].startswith("invalid bearer token: Signature has expired")


@pytest.mark.no_harness_credential
def test_api_routes_keep_json_401_even_for_an_html_accept(
    configured_env: None, private_key, client
) -> None:
    game_id = _owned_game(client, private_key)
    expired = token(private_key, sub="alice", expired=True)

    for path in (f"/api/games/{game_id}", "/api/lobby/games"):
        response = client.get(
            path,
            headers={"Authorization": f"Bearer {expired}", "Accept": _HTML_ACCEPT},
            follow_redirects=False,
        )
        assert response.status_code == 401, (path, response.text)
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"detail": "invalid bearer token: Signature has expired"}


@pytest.mark.no_harness_credential
def test_html_navigation_with_a_valid_token_serves_the_page(
    configured_env: None, private_key, client
) -> None:
    game_id = _owned_game(client, private_key)

    response = client.get(
        f"/{game_id}/",
        headers={
            "Authorization": f"Bearer {token(private_key, sub='alice')}",
            "Accept": _HTML_ACCEPT,
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    "path",
    ["//evil.example", "//evil.example/x", "/\\evil.example", "/\t/evil.example", "evil.example"],
)
def test_the_login_return_path_never_leaves_this_origin(path: str) -> None:
    assert oidc.login_return_path(path, "") == "/"
    assert oidc.login_return_path(path, "a=1") == "/"


def test_the_login_return_path_keeps_a_same_origin_path_and_query() -> None:
    assert oidc.login_return_path("/abc/", "") == "/abc/"
    assert oidc.login_return_path("/abc/", "x=1&y=2") == "/abc/?x=1&y=2"
