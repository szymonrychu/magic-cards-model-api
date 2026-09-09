"""The owner's own sentence: "User must be able to start a game on one
computer and finish on the other."

`test_user_separation.py` covers the negative half of OIDC ownership - bob
cannot see alice's game. This file covers the positive half, which was
missing and is what the cross-device defect actually is: alice, on a *second*
browser with an empty `localStorage` and therefore no seat uid to present,
must still (a) see her own games in `GET /api/lobby/games` with no `?uids=`
at all, and (b) get a **playable** uid back for them - a row with an empty
`seats` map is a row she can look at and not open, which is exactly the bug.

The rule the widening must not break, pinned here too: `seats` is widened
**only for the owner**. A caller who is not the owner - another logged-in
user, or an anonymous browser holding a uid - keeps seeing exactly the uids
it already presented, never one it did not. That is `list_lobby_games`'s
pre-existing guarantee ("a browser never learns a uid that is not already
its own") and ownership is the only thing that may relax it.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from oidc_testkit import ISSUER, bearer_header, generate_keypair, jwks, token

LEGAL_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
LEGAL_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"


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


def _create_lobby_game(client: TestClient, headers: dict[str, str], *, ai: bool = True) -> dict:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "you", "decklist_text": LEGAL_DECK, "ai": False},
                {"username": "bot", "decklist_text": LEGAL_DECK_B, "ai": ai},
            ],
            "seed": 7,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _row(client: TestClient, game_id: str, headers: dict[str, str], **params) -> dict | None:
    listing = client.get("/api/lobby/games", params=params, headers=headers).json()["games"]
    return next((g for g in listing if g["game_id"] == game_id), None)


# ---------------------------------------------------------------------------
# the defect: a second computer, empty localStorage, no uid to present
# ---------------------------------------------------------------------------


def test_the_owner_sees_her_own_game_with_no_uids_at_all(
    client: TestClient, alice_headers: dict[str, str]
) -> None:
    """The first half of the cross-device requirement. A fresh browser sends
    no `?uids=` because it has nothing saved; the row must be there anyway,
    on the strength of the bearer token alone."""
    created = _create_lobby_game(client, alice_headers)
    game_id = created["game_id"]
    try:
        assert _row(client, game_id, alice_headers) is not None
    finally:
        client.delete(f"/api/lobby/games/{game_id}", headers=alice_headers)


def test_the_owner_gets_a_playable_uid_on_a_device_that_holds_none(
    client: TestClient, alice_headers: dict[str, str]
) -> None:
    """The second, and the one the defect actually turns on: seeing the row
    is useless without a seat uid to open it with. The owner gets the full
    `seats` map - every seat of her own game - and the uid in it is the real
    one, good enough to read the board with and nothing weaker."""
    created = _create_lobby_game(client, alice_headers)
    game_id = created["game_id"]
    try:
        row = _row(client, game_id, alice_headers)
        assert row is not None
        assert row["seats"] == created["seat_uids"], (
            "the owner must get every seat of her own game, not only the uids "
            "this browser already had"
        )

        # And it is genuinely playable: the uid the fresh browser just
        # learned opens the board, with no other credential.
        my_uid = row["seats"]["you"]
        state = client.get(f"/api/games/{game_id}", params={"uid": my_uid})
        assert state.status_code == 200, state.text
        assert state.json()["you"] == "you"
    finally:
        client.delete(f"/api/lobby/games/{game_id}", headers=alice_headers)


def test_the_owner_gets_the_ai_seat_uid_too(
    client: TestClient, alice_headers: dict[str, str]
) -> None:
    """The lobby row needs both: `myUid` to open the game and `aiUid` to
    (re)start the AI seat from the game screen. A device that saved neither
    must get both from the server."""
    created = _create_lobby_game(client, alice_headers)
    game_id = created["game_id"]
    try:
        row = _row(client, game_id, alice_headers)
        assert row is not None
        assert row["seats"]["bot"] == created["seat_uids"]["bot"]
    finally:
        client.delete(f"/api/lobby/games/{game_id}", headers=alice_headers)


# ---------------------------------------------------------------------------
# and the rule the widening must not break
# ---------------------------------------------------------------------------


def test_a_different_user_still_sees_nothing_of_it(
    client: TestClient, alice_headers: dict[str, str], bob_headers: dict[str, str]
) -> None:
    created = _create_lobby_game(client, alice_headers)
    game_id = created["game_id"]
    try:
        assert _row(client, game_id, bob_headers) is None
        assert _row(client, game_id, headers={}) is None
    finally:
        client.delete(f"/api/lobby/games/{game_id}", headers=alice_headers)


def test_a_non_owner_holding_one_uid_learns_only_that_uid(
    client: TestClient, alice_headers: dict[str, str], bob_headers: dict[str, str]
) -> None:
    """A seat uid is a capability alice handed out; holding one lets bob see
    the row, and still tells him nothing about the *other* seat. Ownership is
    the only thing that widens `seats`, and bob is not the owner."""
    created = _create_lobby_game(client, alice_headers)
    game_id = created["game_id"]
    bot_uid = created["seat_uids"]["bot"]
    try:
        row = _row(client, game_id, bob_headers, uids=bot_uid)
        assert row is not None
        assert row["seats"] == {"bot": bot_uid}
    finally:
        client.delete(f"/api/lobby/games/{game_id}", headers=alice_headers)


def test_an_ownerless_game_still_only_exposes_the_uids_presented(
    client: TestClient,
) -> None:
    """The anonymous/dev path - `mise run dev`, the test server, and every
    game already in production - is untouched: no owner means no widening,
    and `seats` stays filtered to exactly what `?uids=` asked about."""
    created = _create_lobby_game(client, headers={})
    game_id = created["game_id"]
    my_uid = created["seat_uids"]["you"]
    try:
        row = _row(client, game_id, headers={}, uids=my_uid)
        assert row is not None
        assert row["seats"] == {"you": my_uid}

        bare = _row(client, game_id, headers={})
        assert bare is not None
        assert bare["seats"] == {}
    finally:
        client.delete(f"/api/lobby/games/{game_id}", params={"uid": my_uid})
