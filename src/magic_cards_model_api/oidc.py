"""Bearer-JWT identity: who is asking, when the request carries an OIDC
access token.

The owner's own words: "adding support for oidc-based (eg. through it's
headers), so engine supports user separation (no way for 1 user to login as
the other and see other's games)". "Through its headers" names oauth2-proxy's
usual shape (`X-Forwarded-User`, `X-Auth-Request-Email`, ...) - but a header
is only trustworthy when the request cannot reach the header-checking code
except through the proxy that set it. That condition is not met for this
process: GitHub PR #18 (open, not merged as this module lands) puts
oauth2-proxy in front of the **play UI only** (`play-magic.szymonrichert.pl`,
matching `docs/architecture.md`'s "oauth2-proxy fronts only the two
frontends") and deliberately gives `/api/*` no bypass past it yet - see this
module's own note below on what that bypass needs before it is safe to add.
Even once it exists, `docs/architecture.md`'s security section is explicit
that an API service must never be the thing trusting the proxy's headers:
"API services validate bearer JWTs locally against the realm's JWKS. They
never trust `X-Forwarded-*` headers." This module is that validator - a
**signed bearer JWT, checked locally against the realm's JWKS** - and never
trusts a header no signature backs, regardless of what fronts it.

**Fails closed, not open.** A request that presents no `Authorization` header
is anonymous (`current_user` returns `None`) - the pre-existing, credential-
free shape every route already had, preserved for backward compatibility (see
`game_access.py`'s module docstring for the full migration story). A request
that presents one that does not verify - wrong signature, wrong issuer,
expired, no OIDC issuer configured on this deployment at all - is refused
with 401, never silently downgraded to anonymous. Downgrading it would let a
forged or stale token quietly fall through to whatever an anonymous caller is
allowed to see, which for an *owned* game (`game_access.py`) is nothing - but
principle matters more than the accident of today's routes: a caller that
asserts an identity and fails to prove it must be told so, not waved through
as nobody.

**Nothing here is reachable until an operator actually configures it.**
`oidc_settings()` returns `None` - "OIDC is off" - unless `OIDC_ISSUER` and
either `OIDC_JWKS_URL` or `OIDC_JWKS_STATIC` are all set. The realm this
deployment will actually point at already exists as of GitLab `terraform` MR
!243 (not yet applied): the confidential client `magic-trainer-webui` in the
**existing `spellslinger` realm** - not a new `magic-trainer` realm - with an
audience mapper, a default `groups` scope, and two test users
(`magic-trainer-test-user-a@szymonrichert.pl`/`-b@...`). This module hardcodes
none of that; production sets `OIDC_ISSUER`/`OIDC_AUDIENCE`/`OIDC_JWKS_URL`
to those real values once the MR is applied and the api's deployment carries
them. `OIDC_JWKS_STATIC` (a literal JWKS JSON document) is the escape hatch
that makes this testable and runnable end to end without waiting on that:
CI, `tests/test_oidc.py`, and this task's own manual verification all sign
tokens against a throwaway keypair and hand its public JWKS to the app the
same way `spellslinger`'s own `/certs` endpoint would, over `OIDC_JWKS_URL`
in production.

**What the ingress needs before `/api/*` can safely bypass oauth2-proxy.**
PR #18 deliberately does not give the api ingress a bypass past the proxy,
correct at the time it was written: before this module existed, bypassing
would have exposed a completely unauthenticated game api to the internet.
Once this module is deployed and configured (the paragraph above), a bypass
becomes safe **only when both of these hold**:

1. `OIDC_ISSUER`/`OIDC_AUDIENCE`/`OIDC_JWKS_URL` are actually set on the api's
   deployment, pointing at `spellslinger`/`magic-trainer-webui` - not merely
   that this module exists in the image. Ownership only ever activates for a
   game created by an authenticated request (see `game_access.py`); with no
   OIDC config, every game stays ownerless and indistinguishable from today.
2. The api ingress carries **no `auth-url`/`auth-signin` annotation** - the
   existing, binding rule (`docs/architecture.md`) - so a machine client
   (an AI seat, `seat_cli`, the MCP http transport, none of which ever hold a
   bearer token) reaches this process directly and gets this module's plain
   JSON 401/`KeyError`-mapped 404 responses, never oauth2-proxy's HTML login
   redirect. This is also what a browser's unauthenticated `fetch` needs: PR
   #18's `--api-route=^/api/` makes an unauthenticated XHR through the proxy
   fail with a detectable 401 rather than a cross-origin 302 that `fetch`
   only ever sees as an opaque `TypeError` - bypassing the proxy for `/api/*`
   preserves that same detectability, since this module's own 401s are
   already `{"detail": ...}`-shaped JSON, exactly what the play UI's `api()`
   wrapper (`ui/api.js`) already knows how to surface as a real error rather
   than swallowing it as state.

Bypassing the proxy without (1) is not unsafe - it is simply inert, identical
to today. Bypassing without (2) reintroduces the HTML-redirect failure mode
PR #18 was written to avoid for exactly the clients that cannot follow one.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode

import jwt
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from jwt import PyJWKClient

__all__ = [
    "AuthenticatedUser",
    "BearerTokenRejected",
    "OidcSettings",
    "current_user",
    "is_page_navigation",
    "login_redirect",
    "login_return_path",
    "oidc_settings",
]

#: oauth2-proxy's own login entry point, on the same host that fronts this
#: api (`deploy/k8s/55-oauth2-proxy.yaml`); `rd` is where it returns after.
LOGIN_START_PATH = "/oauth2/start"


class BearerTokenRejected(HTTPException):
    """A 401 for a presented identity that did not verify. Its own class so
    `app.py` can send a browser page load to login instead of raw JSON, while
    every other caller gets the same `{"detail": ...}` 401 as any
    `HTTPException`."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=401, detail=detail)


def is_page_navigation(request: Request) -> bool:
    """Whether `request` is a browser loading a page: a GET outside `/api/`
    (the proxy's `--api-route=^/api/`, whose 401s `ui/api.js` handles) that
    accepts HTML."""
    path = request.url.path
    if request.method != "GET" or path == "/api" or path.startswith("/api/"):
        return False
    return "text/html" in request.headers.get("accept", "").lower()


def login_return_path(path: str, query: str) -> str:
    """`path?query` when `path` stays on this origin, `/` otherwise. Only a
    path starting with a single `/` qualifies: `//host` and `/\\host` are
    read by browsers as another host, and they drop tabs and newlines before
    deciding, so any control character also falls back to `/`."""
    if (
        not path.startswith("/")
        or path[1:2] in ("/", "\\")
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path)
    ):
        return "/"
    return f"{path}?{query}" if query else path


def login_redirect(request: Request) -> RedirectResponse:
    """302 to oauth2-proxy's login, returning to this same page after."""
    target = login_return_path(request.url.path, request.url.query)
    return RedirectResponse(url=f"{LOGIN_START_PATH}?{urlencode({'rd': target})}", status_code=302)


@dataclass(frozen=True)
class AuthenticatedUser:
    """One authenticated caller, from a verified bearer token's claims.

    `sub` is the OIDC subject - stable, opaque, and what `game_access.py`
    compares against a game's `owner_user_id`. `email` rides along only for
    logging/attribution (`audit.record`-shaped call sites, if any ever want
    it); it is never what ownership is checked against, because unlike `sub`
    an IdP does not promise it is stable or even present.
    """

    sub: str
    email: str | None = None


@dataclass(frozen=True)
class OidcSettings:
    """This deployment's OIDC configuration, read once per call from the
    environment - see `oidc_settings()`."""

    issuer: str
    audience: str | None
    jwks_url: str | None
    jwks_static: dict[str, Any] | None


def oidc_settings(env: Mapping[str, str] | None = None) -> OidcSettings | None:
    """This deployment's OIDC configuration, or `None` when it configures
    none - deliberately not cached: `db.configured_database_url` reads its
    own environment fresh on every call for the same reason, and the cost
    here is the same handful of dict lookups.

    `None` when `OIDC_ISSUER` is unset, or when neither `OIDC_JWKS_URL` nor
    `OIDC_JWKS_STATIC` is - a half-set environment (an issuer with no JWKS
    source, say) is treated as "nothing configured" rather than as an error,
    matching `db.configured_database_url`'s own reasoning: guessing that a
    stray leftover variable meant "turn OIDC on" and then failing every
    request is worse than staying off until every piece is actually present.

    `OIDC_JWKS_URL` is the realm's real JWKS endpoint - what a production
    deployment sets once an issuer exists. `OIDC_JWKS_STATIC` is a literal
    JWKS JSON document (the shape a `/certs` response has: `{"keys": [...]}`)
    for testing and offline/dev use with no live IdP; when both are set,
    `OIDC_JWKS_URL` wins. `OIDC_AUDIENCE` is optional - when unset, a token's
    `aud` claim is not checked at all (see `_validate_bearer_token`), not
    "checked against nothing and always rejected".
    """
    env = os.environ if env is None else env
    issuer = env.get("OIDC_ISSUER")
    jwks_url = env.get("OIDC_JWKS_URL") or None
    jwks_static_raw = env.get("OIDC_JWKS_STATIC")
    if not issuer or not (jwks_url or jwks_static_raw):
        return None
    jwks_static = json.loads(jwks_static_raw) if jwks_static_raw else None
    return OidcSettings(
        issuer=issuer,
        audience=env.get("OIDC_AUDIENCE") or None,
        jwks_url=jwks_url,
        jwks_static=jwks_static,
    )


@lru_cache(maxsize=8)
def _remote_jwks_client(jwks_url: str) -> PyJWKClient:
    """One cached `PyJWKClient` per `jwks_url`, so a real deployment's JWKS
    fetch is amortised (`cache_keys=True`) across requests rather than
    refetched on every bearer token - `lru_cache` keyed on the URL itself,
    since a process only ever has one issuer configured but this stays
    correct if that ever changes."""
    return PyJWKClient(jwks_url, cache_keys=True)


def _signing_key(settings: OidcSettings, token: str) -> Any:
    """The public key this token's signature verifies against - from the
    static JWKS when one is configured (tests, offline/dev; `OIDC_JWKS_URL`
    wins when both are set, per `oidc_settings`), otherwise fetched (and
    cached) from the realm's real JWKS endpoint.

    Matches by `kid` (JWT header) against each JWK's own `kid` when the
    token carries one - a JWKS document holds more than one key across a
    rotation, and signing with the wrong one must fail closed, not silently
    verify against whichever key happened to be first.
    """
    if settings.jwks_static is not None:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        keys = settings.jwks_static.get("keys", [])
        for key in keys:
            if kid is None or key.get("kid") == kid:
                return jwt.PyJWK(key).key
        raise jwt.InvalidKeyError("no matching key in the configured static JWKS")
    assert settings.jwks_url is not None  # oidc_settings() guarantees one of the two
    return _remote_jwks_client(settings.jwks_url).get_signing_key_from_jwt(token).key


def _validate_bearer_token(token: str, settings: OidcSettings) -> AuthenticatedUser:
    """`token`, verified against `settings` - signature, issuer, expiry, and
    audience when one is configured. Raises `HTTPException(401)` for
    anything wrong with it; never returns a user for a token that did not
    fully verify."""
    try:
        signing_key = _signing_key(settings, token)
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=settings.issuer,
            audience=settings.audience,
            options={
                "require": ["exp", "sub"],
                # No `settings.audience` configured means no `aud` claim is
                # checked at all - PyJWT's own default (`audience=None` but
                # `verify_aud` still true) instead *rejects* any token that
                # carries an `aud` claim when the caller passed none, which
                # would refuse every real-world IdP's token (Keycloak always
                # sets one) the moment an operator leaves `OIDC_AUDIENCE`
                # unset. Explicit is safer here than PyJWT's own default.
                "verify_aud": settings.audience is not None,
            },
        )
    except jwt.PyJWTError as exc:
        raise BearerTokenRejected(f"invalid bearer token: {exc}") from exc
    sub = claims.get("sub")
    if not sub:  # pragma: no cover - "require": ["sub"] above already guards this
        raise BearerTokenRejected("bearer token carries no subject")
    return AuthenticatedUser(sub=sub, email=claims.get("email"))


def current_user(request: Request) -> AuthenticatedUser | None:
    """The caller `request` asserts, or `None` for an anonymous one.

    `None` only for a request with no `Authorization` header at all - the
    pre-existing shape of every route before this task, and what every
    machine client (a seat's uid/admin-key, never a bearer token) keeps
    presenting. Anything else that is wrong - a header that is not
    `Bearer <token>`, a token that does not verify, or a bearer token
    presented when this deployment has no OIDC issuer configured to check it
    against - raises `HTTPException(401)` rather than being treated as
    anonymous: a caller that asserts an identity and cannot prove it is
    refused, not waved through as nobody (see this module's own docstring).
    """
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise BearerTokenRejected("malformed Authorization header")
    settings = oidc_settings()
    if settings is None:
        raise BearerTokenRejected(
            "a bearer token was presented but this deployment has no OIDC issuer configured"
        )
    return _validate_bearer_token(token, settings)
