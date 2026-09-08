"""Shared machinery for signing test bearer tokens against a throwaway RSA
keypair - used by `test_oidc.py` (unit-level) and `test_user_separation.py`
(the full HTTP-level enforcement suite). Not itself a `test_*.py` file, so
pytest never collects it as one.

Nothing here talks to a network or a real IdP: `jwks()` builds the exact
`{"keys": [...]}` document a real realm's `/certs` endpoint would serve, and
`oidc.py`'s `OIDC_JWKS_STATIC` escape hatch takes that document literally -
see that module's docstring for why the static path exists at all.

`ISSUER`/`AUDIENCE` are shaped after the real deployment target - the
`spellslinger` Keycloak realm's `magic-trainer-webui` confidential client
(GitLab `terraform` MR !243: an audience mapper, a default `groups` scope,
and the two test users `magic-trainer-test-user-a@szymonrichert.pl`/
`-b@...`), not a hypothetical `magic-trainer` realm that does not exist -
there is no new realm, this deployment reuses `spellslinger`'s. `oidc.py`
itself hardcodes neither; production points `OIDC_ISSUER`/`OIDC_AUDIENCE`/
`OIDC_JWKS_URL` at these same real values once the MR is applied.
"""

from __future__ import annotations

import json
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://sso.szymonrichert.pl/realms/spellslinger"
AUDIENCE = "magic-trainer-webui"
KID = "test-signing-key-1"


def generate_keypair() -> RSAPrivateKey:
    """One throwaway RSA keypair - generated once per test session
    (`private_key` fixture in `conftest.py`-adjacent files is not needed:
    callers share this via a module-level `@pytest.fixture(scope="session")`
    they define themselves) since key generation is the expensive part and
    nothing here needs more than one key across a whole test run."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(private_key: RSAPrivateKey, *, kid: str = KID) -> dict[str, Any]:
    """The public JWKS document for `private_key`, in the shape
    `OIDC_JWKS_STATIC` expects (`{"keys": [...]}` - the same shape a real
    issuer's `/certs` endpoint serves)."""
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = kid
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return {"keys": [jwk]}


def token(
    private_key: RSAPrivateKey,
    *,
    sub: str,
    issuer: str = ISSUER,
    audience: str | None = None,
    kid: str = KID,
    expired: bool = False,
    email: str | None = None,
    **extra_claims: Any,
) -> str:
    """A bearer token for `sub`, signed by `private_key` and shaped exactly
    as `oidc.py` validates: `iss`, `sub`, `exp` always present, `aud` only
    when `audience` is given, `email` only when given. `expired=True` backdates
    `exp` into the past, for the one negative test that needs a token that
    fails only on expiry and nothing else."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": issuer,
        "sub": sub,
        "iat": now,
        "exp": (now - 3600) if expired else (now + 3600),
        **extra_claims,
    }
    if audience is not None:
        payload["aud"] = audience
    if email is not None:
        payload["email"] = email
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


def bearer_header(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}
