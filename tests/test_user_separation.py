"""OIDC-based user separation: the owner's own sentence, verbatim - "no way
for 1 user to login as the other and see other's games" - turned into one
enforcement suite.

Every route this api exposes under `/api/games/{game_id}/...` and
`/api/lobby/games/{game_id}/...` is enumerated straight from `app.routes`
(`_game_scoped_operations` below), not hand-picked, so a route added after
this file was written is covered automatically rather than silently
exempted. For each one: a game owned by alice (a bearer token identifies her
`sub`), accessed by bob (a *different*, equally valid, bearer token) or by
nobody at all, must come back exactly as if the game did not exist - same
404, same body - never a redacted-but-visible 200, never a 403 that would at
least confirm the id was real.

`game_access.GameAccessRoute` denies before any route's own body/query
validation runs, which is what makes one uniform assertion possible across
routes whose bodies otherwise have nothing in common: an unauthorized POST to
`actions/cast` and an unauthorized GET to `log` both fail exactly the same
way, before either ever asks what a `CastRequest` or a `since` parameter is
supposed to look like.

Machine clients (a seat's uid, or the game's admin key) are the other half of
this design and get their own tests below: they must keep working on an
*owned* game with **no bearer token at all**, because `seat_cli`/`mcp`/the AI
seat runner never carry one - a seat's authority is a capability the owner
handed out, independent of who is logged in. See `game_access.py`'s module
docstring for the full design.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from oidc_testkit import ISSUER, bearer_header, generate_keypair, jwks, token

from magic_cards_model_api.app import app as raw_app

#: This whole file's subject is what a caller may reach with the credential
#: it actually presents, and several tests here deliberately present none -
#: `conftest.py`'s `_harness_credential` fixture would hand them the game's
#: admin key and turn every one of those assertions green for the wrong
#: reason.
pytestmark = pytest.mark.no_harness_credential

LEGAL_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
LEGAL_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"

#: Every path parameter this api's game-scoped routes ever carry besides
#: `game_id` itself, and a syntactically-valid dummy value for it - the gate
#: only ever looks at `game_id`, so what these resolve to does not matter,
#: only that routing itself succeeds.
_DUMMY_PATH_PARAMS = {
    "card_id": "00000000-0000-0000-0000-000000000000",
    "username": "nobody",
}


def _game_scoped_operations(game_id: str) -> list[tuple[str, str]]:
    """`(method, concrete_path)` for every operation this app exposes whose
    route names `{game_id}` in its path - `game_id` substituted with the real
    id under test, every other path parameter with a harmless placeholder.
    Read straight from the live route table, not a hand-maintained list."""
    params = {**_DUMMY_PATH_PARAMS, "game_id": game_id}
    operations: list[tuple[str, str]] = []
    for route in raw_app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or "{game_id}" not in path:
            continue
        concrete = re.sub(r"\{(\w+)\}", lambda m: params[m.group(1)], path)
        for method in sorted(methods):
            if method == "HEAD":
                continue
            operations.append((method, concrete))
    return operations


def _call(client: TestClient, method: str, path: str, headers: dict[str, str]) -> object:
    body = {} if method in ("POST", "PUT", "PATCH") else None
    return client.request(method, path, json=body, headers=headers)


def _is_gate_denial(response, game_id: str) -> bool:
    """Whether `response` is exactly `game_access.py`'s own "no such game"
    refusal - the same body a genuinely nonexistent game produces, which is
    the point: a caller must never be able to tell "not yours" apart from
    "does not exist"."""
    if response.status_code != 404:
        return False
    try:
        detail = response.json().get("detail")
    except Exception:
        return False
    return detail == f"no game {game_id}"


@pytest.fixture(scope="session")
def private_key():
    return generate_keypair()


@pytest.fixture(autouse=True)
def _oidc_configured(monkeypatch: pytest.MonkeyPatch, private_key) -> None:
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_JWKS_STATIC", json.dumps(jwks(private_key)))
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)


@pytest.fixture()
def alice_headers(private_key) -> dict[str, str]:
    return bearer_header(token(private_key, sub="alice"))


@pytest.fixture()
def bob_headers(private_key) -> dict[str, str]:
    return bearer_header(token(private_key, sub="bob"))


def _create_raw_game(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.post(
        "/api/games",
        json={"players": [{"username": "alice"}, {"username": "bob"}]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_lobby_game(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": LEGAL_DECK, "ai": False},
                {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": False},
            ],
            "seed": 7,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# the central test: every route, enumerated from the code
# ---------------------------------------------------------------------------


def test_every_game_scoped_route_hides_an_owned_game_from_another_user(
    client: TestClient, alice_headers: dict[str, str], bob_headers: dict[str, str]
) -> None:
    game = _create_raw_game(client, alice_headers)
    game_id = game["game_id"]
    try:
        operations = _game_scoped_operations(game_id)
        assert len(operations) >= 30, "sanity: the route table did not resolve to almost nothing"

        for method, path in operations:
            bob_response = _call(client, method, path, bob_headers)
            assert _is_gate_denial(bob_response, game_id), (
                f"{method} {path}: bob (a different OIDC user) was not denied "
                f"as if the game did not exist - got {bob_response.status_code} "
                f"{bob_response.text}"
            )

            anon_response = _call(client, method, path, headers={})
            assert _is_gate_denial(anon_response, game_id), (
                f"{method} {path}: an anonymous caller was not denied as if "
                f"the game did not exist - got {anon_response.status_code} "
                f"{anon_response.text}"
            )

            alice_response = _call(client, method, path, alice_headers)
            assert not _is_gate_denial(alice_response, game_id), (
                f"{method} {path}: alice (the owner) was denied her own game - "
                f"got {alice_response.status_code} {alice_response.text}"
            )
    finally:
        client.delete(f"/api/games/{game_id}", headers=alice_headers)


# ---------------------------------------------------------------------------
# named tests for the owner's own sentence: see, read, act on, delete
# ---------------------------------------------------------------------------


def test_bob_cannot_see_alices_game_in_the_raw_list(
    client: TestClient, alice_headers: dict[str, str], bob_headers: dict[str, str]
) -> None:
    game = _create_raw_game(client, alice_headers)
    game_id = game["game_id"]
    try:
        bob_ids = {
            entry["game_id"]
            for entry in client.get("/api/games", headers=bob_headers).json()["games"]
        }
        alice_ids = {
            entry["game_id"]
            for entry in client.get("/api/games", headers=alice_headers).json()["games"]
        }
        anon_ids = {entry["game_id"] for entry in client.get("/api/games").json()["games"]}

        assert game_id not in bob_ids
        assert game_id not in anon_ids
        assert game_id in alice_ids
    finally:
        client.delete(f"/api/games/{game_id}", headers=alice_headers)


def test_bob_cannot_see_alices_lobby_game_in_the_lobby_list(
    client: TestClient, alice_headers: dict[str, str], bob_headers: dict[str, str]
) -> None:
    game = _create_lobby_game(client, alice_headers)
    game_id = game["game_id"]
    try:
        bob_ids = {
            entry["game_id"]
            for entry in client.get("/api/lobby/games", headers=bob_headers).json()["games"]
        }
        alice_ids = {
            entry["game_id"]
            for entry in client.get("/api/lobby/games", headers=alice_headers).json()["games"]
        }
        anon_ids = {entry["game_id"] for entry in client.get("/api/lobby/games").json()["games"]}

        assert game_id not in bob_ids
        assert game_id not in anon_ids
        assert game_id in alice_ids
    finally:
        client.delete(f"/api/lobby/games/{game_id}", headers=alice_headers)


def test_bob_cannot_read_alices_game_summary(
    client: TestClient, alice_headers: dict[str, str], bob_headers: dict[str, str]
) -> None:
    game = _create_raw_game(client, alice_headers)
    game_id = game["game_id"]
    try:
        bob_summaries = client.get(
            "/api/games/summary", params={"ids": game_id}, headers=bob_headers
        ).json()["summaries"]
        alice_summaries = client.get(
            "/api/games/summary", params={"ids": game_id}, headers=alice_headers
        ).json()["summaries"]

        assert bob_summaries == []
        assert len(alice_summaries) == 1
    finally:
        client.delete(f"/api/games/{game_id}", headers=alice_headers)


def test_bob_cannot_read_alices_game_state(
    client: TestClient, alice_headers: dict[str, str], bob_headers: dict[str, str]
) -> None:
    game = _create_raw_game(client, alice_headers)
    game_id = game["game_id"]
    try:
        response = client.get(f"/api/games/{game_id}", headers=bob_headers)
        assert response.status_code == 404
        assert response.json()["detail"] == f"no game {game_id}"

        # A player's own uid, minted by alice for *her* game, is bob's own
        # capability - it must not leak the state to a bob who is not
        # presenting it, and presenting his OIDC identity instead of it must
        # not substitute for it either.
        own = client.get(f"/api/games/{game_id}", headers=alice_headers)
        assert own.status_code == 200
    finally:
        client.delete(f"/api/games/{game_id}", headers=alice_headers)


def test_bob_cannot_act_on_alices_game(
    client: TestClient, alice_headers: dict[str, str], bob_headers: dict[str, str]
) -> None:
    game = _create_raw_game(client, alice_headers)
    game_id = game["game_id"]
    try:
        response = client.post(
            f"/api/games/{game_id}/actions/pass-priority",
            json={"player": "alice"},
            headers=bob_headers,
        )
        assert response.status_code == 404
        assert response.json()["detail"] == f"no game {game_id}"
    finally:
        client.delete(f"/api/games/{game_id}", headers=alice_headers)


def test_bob_cannot_delete_alices_game(
    client: TestClient, alice_headers: dict[str, str], bob_headers: dict[str, str]
) -> None:
    game = _create_raw_game(client, alice_headers)
    game_id = game["game_id"]
    try:
        response = client.delete(f"/api/games/{game_id}", headers=bob_headers)
        assert response.status_code == 404
        assert response.json()["detail"] == f"no game {game_id}"

        # Still there - bob's attempt genuinely did nothing.
        assert client.get(f"/api/games/{game_id}", headers=alice_headers).status_code == 200
    finally:
        client.delete(f"/api/games/{game_id}", headers=alice_headers)


def test_bob_cannot_mint_himself_a_seat_in_alices_game(
    client: TestClient, alice_headers: dict[str, str], bob_headers: dict[str, str]
) -> None:
    game = _create_raw_game(client, alice_headers)
    game_id = game["game_id"]
    try:
        response = client.post(
            f"/api/games/{game_id}/seats", json={"player": "alice"}, headers=bob_headers
        )
        assert response.status_code == 404
        assert response.json()["detail"] == f"no game {game_id}"
    finally:
        client.delete(f"/api/games/{game_id}", headers=alice_headers)


# ---------------------------------------------------------------------------
# seats and admin keys: capability-based authority, independent of OIDC
# ---------------------------------------------------------------------------


def test_a_seat_uid_keeps_working_on_an_owned_game_with_no_bearer_token(
    client: TestClient, alice_headers: dict[str, str]
) -> None:
    """The design's other half: `seat_cli`/`mcp`/the AI seat runner never
    carry a bearer token. A seat uid the owner handed out (here, alice's own
    `bob` seat from game creation) must still open the game with nothing but
    that uid - no `Authorization` header at all."""
    game = _create_raw_game(client, alice_headers)
    game_id = game["game_id"]
    bob_uid = game["seat_uids"]["bob"]
    try:
        response = client.get(f"/api/games/{game_id}", params={"uid": bob_uid})
        assert response.status_code == 200
        assert response.json()["you"] == "bob"
    finally:
        client.delete(f"/api/games/{game_id}", headers=alice_headers)


def test_the_admin_key_keeps_working_on_an_owned_game_with_no_bearer_token(
    client: TestClient, alice_headers: dict[str, str]
) -> None:
    game = _create_raw_game(client, alice_headers)
    game_id = game["game_id"]
    admin_key = game["admin_key"]
    try:
        response = client.get(f"/api/games/{game_id}/log", params={"key": admin_key})
        assert response.status_code == 200
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": admin_key})


def test_an_unrelated_uid_does_not_open_an_owned_game(
    client: TestClient, alice_headers: dict[str, str]
) -> None:
    game = _create_raw_game(client, alice_headers)
    game_id = game["game_id"]
    try:
        response = client.get(f"/api/games/{game_id}", params={"uid": "not-a-real-uid"})
        assert response.status_code == 404
        assert response.json()["detail"] == f"no game {game_id}"
    finally:
        client.delete(f"/api/games/{game_id}", headers=alice_headers)


# ---------------------------------------------------------------------------
# migration: an ownerless game (no bearer token at creation, every game
# before this feature existed) is exactly as reachable as it always was
# ---------------------------------------------------------------------------


def test_an_ownerless_game_stays_reachable_with_no_credentials_at_all(
    client: TestClient,
) -> None:
    game = _create_raw_game(client, headers={})  # no bearer token: today's shape
    game_id = game["game_id"]
    try:
        assert client.get(f"/api/games/{game_id}").status_code == 200
        assert game_id in {entry["game_id"] for entry in client.get("/api/games").json()["games"]}
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": game["admin_key"]})
