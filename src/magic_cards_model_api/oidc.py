"""Bearer-JWT identity: who is asking, when the request carries an OIDC
access token.

The owner's own words: "adding support for oidc-based (eg. through it's
headers), so engine supports user separation (no way for 1 user to login as
the other and see other's games)". "Through its headers" names oauth2-proxy's
usual shape (`X-Forwarded-User`, `X-Auth-Request-Email`, ...) - but a header
is only trustworthy when the request cannot reach this app except through the
proxy that sets it, and nothing about this deployment guarantees that today
(`deploy/k8s/70-ingress.yaml` routes straight to this service; there is no
oauth2-proxy in front of it, and `docs/2b-plan.md`'s decision 2 keeps it that
way for this phase). Trusting `X-Forwarded-User` here would mean trusting
whatever the caller sends, which is exactly the hole this task exists to
close. So this module validates a **signed bearer JWT locally against the
realm's JWKS** instead - the primitive `docs/architecture.md`'s own security
section already names as the target ("API services validate bearer JWTs
locally against the realm's JWKS. They never trust `X-Forwarded-*` headers.")
- and never trusts a header no signature backs.

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
either `OIDC_JWKS_URL` or `OIDC_JWKS_STATIC` are all set. No Keycloak realm
exists for this deployment yet (`docs/2b-plan.md`'s M1: the terraform repo
that would stand one up is out of this phase) - this module is the API-side
half the owner asked to be built now, wired to whatever issuer eventually
exists, not a claim that the issuer exists today. `OIDC_JWKS_STATIC` (a
literal JWKS JSON document) is the escape hatch that makes this testable and
runnable end to end without one: CI, `tests/test_oidc.py`, and this task's
own manual verification all sign tokens against a throwaway keypair and hand
its public JWKS to the app the same way a real realm's `/certs` endpoint
would, over `OIDC_JWKS_URL` in production.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

__all__ = ["AuthenticatedUser", "OidcSettings", "oidc_settings", "current_user"]


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
        raise HTTPException(status_code=401, detail=f"invalid bearer token: {exc}") from exc
    sub = claims.get("sub")
    if not sub:  # pragma: no cover - "require": ["sub"] above already guards this
        raise HTTPException(status_code=401, detail="bearer token carries no subject")
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
        raise HTTPException(status_code=401, detail="malformed Authorization header")
    settings = oidc_settings()
    if settings is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "a bearer token was presented but this deployment has no OIDC issuer configured"
            ),
        )
    return _validate_bearer_token(token, settings)
