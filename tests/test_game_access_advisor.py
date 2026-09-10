"""The advisor credential: a uid whose read-only-ness is a property of the
uid itself, enforced by the server rather than by whatever client happens to
be holding it.

An advisor watches one human seat's game and may say things about it. It is
not a seat: it may read everything that seat may read (`visibility.
resolve_viewer` resolves it to the human's own username, so the advisor's
view is byte-identical to the human's - `test_visibility.py` pins that), it
may ask questions that change nothing (`dry_run`), and it may write into the
places that are explicitly a channel for saying something (`/advice`,
`/report`). Everything else - every action, every decision, every
declaration - is refused as if the game did not exist.

The gate lives in exactly one place, `game_access.GameAccessRoute`, which
wraps every route whose path carries `{game_id}` and runs before the route's
own body parsing. That is why this file can assert one uniform refusal across
routes whose bodies have nothing in common, and why a mutating route added
tomorrow is covered by it without being listed here.

**Denial is 404 with `no game {id}`, never 403** - this repo's deliberate
existing choice (`game_access.py`'s module docstring): a caller must not be
able to tell "not yours" from "does not exist".
"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import card_id
from fastapi.testclient import TestClient
from oidc_testkit import ISSUER, bearer_header, generate_keypair, jwks, token
from starlette.requests import Request

from magic_cards_model_api import game_access
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.store import store

#: Every mutating POST the acceptance conditions name by hand, with a body
#: that is at least structurally plausible - the gate refuses before any of
#: them is ever validated, so the body only has to be JSON.
MUTATING_POSTS = [
    ("actions/cast", {"player": "alice", "card": "Lightning Bolt"}),
    ("actions/pass-priority", {"player": "alice"}),
    ("decide", {"player": "alice", "answer": {"kind": "may", "value": True}}),
    ("actions/attack", {"player": "alice", "attackers": []}),
    ("actions/declare-blockers", {"player": "alice", "blockers": []}),
]


def advisor_uid(game_id: str, player: str = "alice", uid: str = "advisor-uid") -> str:
    """Register `uid` as an advisor on `player`'s seat and hand it back.

    There is no route that mints one yet (the lobby opt-in and the buddy pod
    that would use it are separate tasks) - the credential itself, its
    storage and its gate are what this task delivers, so the tests register
    it straight through the repository the way that route eventually will.
    """
    store.register_advisors(game_id, {uid: Advisor(player=player)})
    return uid


def seat_uid(game_id: str, player: str) -> str:
    return next(uid for uid, name in store.seat_uids(game_id).items() if name == player)


def is_gate_denial(response, game_id: str) -> bool:
    """`game_access.py`'s own refusal, exactly: 404 with the same body a
    genuinely nonexistent game produces."""
    if response.status_code != 404:
        return False
    try:
        return response.json().get("detail") == f"no game {game_id}"
    except Exception:
        return False


def gate_request(method: str, path: str, *, query: str = "", body: object = None) -> Request:
    """A real Starlette `Request` for `game_access.can_access_game` to judge -
    the way to ask about a path (`/advice`) whose route belongs to a later
    task and does not exist in this app yet."""
    payload = b"" if body is None else json.dumps(body).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "server": ("testserver", 80),
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": query.encode(),
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )


# ---------------------------------------------------------------------------
# reads: everything the human seat may read
# ---------------------------------------------------------------------------


def test_an_advisor_uid_reads_the_state(client: TestClient, game: str) -> None:
    uid = advisor_uid(game)

    response = client.get(f"/api/games/{game}", params={"uid": uid})

    assert response.status_code == 200, response.text
    assert response.json()["you"] == "alice"


def test_an_advisor_uid_reads_the_log(client: TestClient, game: str) -> None:
    uid = advisor_uid(game)

    response = client.get(f"/api/games/{game}/log", params={"uid": uid})

    assert response.status_code == 200, response.text


def test_an_advisor_uid_reads_its_players_legal_actions(client: TestClient, game: str) -> None:
    """`legal-actions` hard-refuses (400) any uid that does not open the seat
    it names, because `{"actions": []}` is a real answer and must never be
    confused with "not allowed". An advisor uid *does* open its player's view,
    so it is answered rather than refused."""
    uid = advisor_uid(game, "alice")

    response = client.get(
        f"/api/games/{game}/legal-actions", params={"player": "alice", "uid": uid}
    )

    assert response.status_code == 200, response.text
    assert "actions" in response.json()


def test_an_advisor_uid_is_still_refused_the_other_seats_legal_actions(
    client: TestClient, game: str
) -> None:
    uid = advisor_uid(game, "alice")

    response = client.get(f"/api/games/{game}/legal-actions", params={"player": "bob", "uid": uid})

    assert response.status_code == 400, response.text


def test_a_genuinely_foreign_uid_is_still_refused_legal_actions(
    client: TestClient, game: str
) -> None:
    """The deliberate refusal this endpoint already had must survive: an
    advisor is not a general amnesty."""
    response = client.get(
        f"/api/games/{game}/legal-actions", params={"player": "alice", "uid": "not-a-real-uid"}
    )

    assert response.status_code == 400, response.text


# ---------------------------------------------------------------------------
# writes: refused, one uniform way, for every mutating route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix,body", MUTATING_POSTS, ids=[path for path, _ in MUTATING_POSTS])
def test_an_advisor_uid_is_refused_on_a_mutating_post(
    client: TestClient, game: str, suffix: str, body: dict
) -> None:
    uid = advisor_uid(game)

    response = client.post(f"/api/games/{game}/{suffix}", params={"uid": uid}, json=body)

    assert is_gate_denial(response, game), (
        f"{suffix}: an advisor uid must be refused as if the game did not exist - "
        f"got {response.status_code} {response.text}"
    )


def test_an_advisor_uid_cannot_declare_a_plan(client: TestClient, game: str) -> None:
    """`declare_plan` stays seat-only: a plan is a seat's own statement of
    what it means to do, and an advisor does not act."""
    uid = advisor_uid(game)

    response = client.post(
        f"/api/games/{game}/plan",
        json={
            "uid": uid,
            "player": "alice",
            "board_read": "b",
            "my_role": "r",
            "plan": "p",
            "prediction": {"expecting": "a fetchland", "because": "they kept a two-lander"},
        },
    )

    assert response.status_code == 404, response.text


def test_an_advisor_uid_cannot_delete_the_game(client: TestClient, game: str) -> None:
    uid = advisor_uid(game)

    response = client.delete(f"/api/games/{game}", params={"uid": uid})

    assert is_gate_denial(response, game), response.text


def test_an_advisor_uid_cannot_mint_itself_a_seat(client: TestClient, game: str) -> None:
    """The seat back door is a mutation like any other - an advisor uid must
    not be able to upgrade itself into a seat through it."""
    uid = advisor_uid(game)

    response = client.post(
        f"/api/games/{game}/seats", params={"uid": uid}, json={"player": "alice"}
    )

    assert is_gate_denial(response, game), response.text


# ---------------------------------------------------------------------------
# writes that change nothing, or that are the channel for saying something
# ---------------------------------------------------------------------------


def test_an_advisor_uid_may_post_a_dry_run(client: TestClient, game: str) -> None:
    """A `dry_run` is a question, not a move (`engine_api.dry_run_copy`
    answers it against an isolated probe copy), so the gate lets one
    through on a shape that supports it."""
    uid = advisor_uid(game)
    land = card_id(client, game, "Mountain", "alice", "hand")

    response = client.post(
        f"/api/games/{game}/actions/play-land",
        params={"uid": uid},
        json={"player": "alice", "card_id": land, "dry_run": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is True
    board = client.get(f"/api/games/{game}", params={"uid": uid}).json()
    assert board["battlefield"] == [], "a dry run the advisor asked for still changed nothing"


def test_the_same_post_without_dry_run_is_refused(client: TestClient, game: str) -> None:
    """The other half of the test above: it is `dry_run: true` that is
    allowed, not the route."""
    uid = advisor_uid(game)
    land = card_id(client, game, "Mountain", "alice", "hand")

    response = client.post(
        f"/api/games/{game}/actions/play-land",
        params={"uid": uid},
        json={"player": "alice", "card_id": land},
    )

    assert is_gate_denial(response, game), response.text


def test_a_dry_run_that_is_false_is_refused(client: TestClient, game: str) -> None:
    uid = advisor_uid(game)
    land = card_id(client, game, "Mountain", "alice", "hand")

    response = client.post(
        f"/api/games/{game}/actions/play-land",
        params={"uid": uid},
        json={"player": "alice", "card_id": land, "dry_run": False},
    )

    assert is_gate_denial(response, game), response.text


def test_an_advisor_uid_may_file_a_report(client: TestClient, game: str) -> None:
    uid = advisor_uid(game)

    response = client.post(
        f"/api/games/{game}/report",
        params={"uid": uid},
        json={"player": "alice", "text": "the engine let me block with a tapped creature"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["recorded"] is True


def test_an_advisor_uid_may_post_to_a_path_ending_in_advice(client: TestClient, game: str) -> None:
    """`/advice`'s own route body is a separate task; the gate that will let
    it through is this one, and it is asked here directly so the permission
    is pinned now rather than when the route lands."""
    uid = advisor_uid(game)
    request = gate_request(
        "POST", f"/api/games/{game}/advice", query=f"uid={uid}", body={"text": "bolt the bird"}
    )

    assert asyncio.run(game_access.can_access_game(request, game)) is True


def test_an_advisor_uid_is_refused_a_post_to_an_unlisted_path(
    client: TestClient, game: str
) -> None:
    """The mirror of the test above through the same door: it is `/advice`
    and `/report` that are allowed, not "any path the gate has not heard
    of"."""
    uid = advisor_uid(game)
    request = gate_request(
        "POST", f"/api/games/{game}/actions/cast", query=f"uid={uid}", body={"player": "alice"}
    )

    assert asyncio.run(game_access.can_access_game(request, game)) is False


# ---------------------------------------------------------------------------
# an ordinary seat uid is entirely unaffected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix,body", MUTATING_POSTS, ids=[path for path, _ in MUTATING_POSTS])
def test_an_ordinary_seat_uid_is_unaffected_by_the_advisor_gate(
    client: TestClient, game: str, suffix: str, body: dict
) -> None:
    advisor_uid(game)  # the game has an advisor; the seat must not notice
    uid = seat_uid(game, "alice")

    response = client.post(f"/api/games/{game}/{suffix}", params={"uid": uid}, json=body)

    assert not is_gate_denial(response, game), (
        f"{suffix}: a seat uid was refused by the advisor gate - "
        f"got {response.status_code} {response.text}"
    )


def test_an_ordinary_seat_uid_still_reads_and_still_acts(client: TestClient, game: str) -> None:
    advisor_uid(game)
    uid = seat_uid(game, "alice")
    land = card_id(client, game, "Mountain", "alice", "hand")

    assert client.get(f"/api/games/{game}", params={"uid": uid}).status_code == 200
    played = client.post(
        f"/api/games/{game}/actions/play-land",
        params={"uid": uid},
        json={"player": "alice", "card_id": land},
    )
    assert played.status_code == 200, played.text
    board = client.get(f"/api/games/{game}", params={"uid": uid}).json()
    assert [card["name"] for card in board["battlefield"]] == ["Mountain"]


def test_an_ordinary_seat_uid_still_declares_a_plan(client: TestClient, game: str) -> None:
    advisor_uid(game)
    uid = seat_uid(game, "alice")

    response = client.post(
        f"/api/games/{game}/plan",
        json={
            "uid": uid,
            "player": "alice",
            "board_read": "b",
            "my_role": "r",
            "plan": "p",
            "prediction": {"expecting": "a fetchland", "because": "they kept a two-lander"},
        },
    )

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# an owned game: where "a foreign uid is refused everywhere" is observable
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def private_key():
    return generate_keypair()


@pytest.fixture()
def _oidc_configured(monkeypatch: pytest.MonkeyPatch, private_key) -> None:
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_JWKS_STATIC", json.dumps(jwks(private_key)))
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)


@pytest.fixture()
def owned_game(client: TestClient, private_key, _oidc_configured) -> str:
    """A game owned by an OIDC identity - the only shape in which an
    unrecognised uid is refused *at all*, since an ownerless game is
    reachable by anyone who knows its id (`game_access.py`'s migration
    story). The advisor gate itself is ownership-independent, which is why
    every test above uses an ordinary ownerless game."""
    headers = bearer_header(token(private_key, sub="alice"))
    response = client.post(
        "/api/games",
        json={"players": [{"username": "alice"}, {"username": "bob"}]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    game_id = response.json()["game_id"]
    yield game_id
    client.delete(f"/api/games/{game_id}", headers=headers)


def test_a_foreign_uid_reaches_nothing_of_an_owned_game(
    client: TestClient, owned_game: str
) -> None:
    advisor_uid(owned_game)  # a real advisor exists; this uid is still not it

    reads = [
        client.get(f"/api/games/{owned_game}", params={"uid": "not-a-real-uid"}),
        client.get(f"/api/games/{owned_game}/log", params={"uid": "not-a-real-uid"}),
        client.get(
            f"/api/games/{owned_game}/legal-actions",
            params={"player": "alice", "uid": "not-a-real-uid"},
        ),
    ]
    writes = [
        client.post(
            f"/api/games/{owned_game}/{suffix}", params={"uid": "not-a-real-uid"}, json=body
        )
        for suffix, body in MUTATING_POSTS
    ]

    for response in reads + writes:
        assert is_gate_denial(response, owned_game), (
            f"{response.request.method} {response.request.url}: a foreign uid was not "
            f"refused - got {response.status_code} {response.text}"
        )


def test_an_advisor_uid_reads_an_owned_game_and_still_cannot_act_on_it(
    client: TestClient, owned_game: str
) -> None:
    uid = advisor_uid(owned_game)

    assert client.get(f"/api/games/{owned_game}", params={"uid": uid}).status_code == 200
    for suffix, body in MUTATING_POSTS:
        response = client.post(f"/api/games/{owned_game}/{suffix}", params={"uid": uid}, json=body)
        assert is_gate_denial(response, owned_game), (
            f"{suffix}: got {response.status_code} {response.text}"
        )


def test_an_advisor_uid_of_one_game_is_nobody_in_another(
    client: TestClient, owned_game: str, game: str
) -> None:
    """The advisor map is per game, like every other credential here."""
    uid = advisor_uid(game, "alice", uid="advisor-of-the-other-game")

    response = client.get(f"/api/games/{owned_game}", params={"uid": uid})

    assert is_gate_denial(response, owned_game), response.text
