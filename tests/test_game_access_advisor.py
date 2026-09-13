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
from pathlib import Path

import pytest
from conftest import card_id
from fastapi.testclient import TestClient
from oidc_testkit import ISSUER, bearer_header, generate_keypair, jwks, token
from starlette.requests import Request

from magic_cards_model_api import game_access, visibility
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.store import store

#: Every mutating POST the acceptance conditions name by hand, with a body
#: that is at least structurally plausible - the gate refuses before any of
#: them is ever validated, so the body only has to be JSON.
#: Bodies that name only fields their own route declares. They used not to -
#: `decide` was sent an `answer` object and `attack` a `player`, neither of
#: which exists on `DecideRequest`/`AttackRequest`, and pydantic threw both
#: away without a word. That is the same silent discard the `dry_run`
#: escalation rode in on, so `schemas.RequestModel` refuses it now and these
#: bodies had to become real ones.
MUTATING_POSTS = [
    ("actions/cast", {"player": "alice", "card": "Lightning Bolt"}),
    ("actions/pass-priority", {"player": "alice"}),
    ("decide", {"player": "alice", "choice": "G"}),
    ("actions/attack", {"attackers": []}),
    ("actions/declare-blockers", {"player": "alice", "blocks": []}),
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


class _Route:
    """The one attribute `game_access._route_template` reads off
    `request.scope["route"]` - FastAPI puts the matched `APIRoute` there
    (`fastapi.routing.APIRoute.matches`), and its `path` is the route
    *template*, never the caller's own spelling of the URL."""

    def __init__(self, path: str) -> None:
        self.path = path


def gate_request(
    method: str,
    path: str,
    *,
    query: str = "",
    body: object = None,
    route: str | None = None,
) -> Request:
    """A real Starlette `Request` for `game_access.can_access_game` to judge -
    the way to ask about a path (`/advice`) whose route belongs to a later
    task and does not exist in this app yet.

    `route` is the matched route template the router would have put in the
    scope, and defaults to `path` (the overwhelmingly common case: a path
    with no parameters after `{game_id}`). Passing the two apart is how the
    tests below pin that the gate reads the *template* and never the raw
    tail - see `test_a_raw_path_that_ends_in_advice_is_not_enough`.
    """
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
            "route": _Route(route if route is not None else path),
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


# ---------------------------------------------------------------------------
# the `dry_run` carve-out, read off the route's own declared shape
# ---------------------------------------------------------------------------
#
# The hole this section pins: the gate used to decide "this is only a probe"
# by reading `dry_run` straight off the raw request body, and only seven
# request models declare that field. Every other one is a plain `BaseModel`
# with an empty `model_config`, so pydantic *discards* an unexpected
# `dry_run` key without complaint - the gate said probe, the route then
# committed for real. On `POST .../seats` that was not a stray mutation but
# a privilege escalation: the route hands back a fresh, unrestricted seat
# uid, so an advisor could upgrade itself out of its own restriction with one
# extra key.


def _openapi() -> dict:
    """The committed contract, which is the only place the set of
    dry-run-capable shapes is written down twice - here and in
    `game_access.DRY_RUN_ROUTES`. Regenerated by `mise run openapi:dump`."""
    return json.loads((Path(__file__).resolve().parents[1] / "openapi.json").read_text())


def _body_ref(operation: dict) -> str | None:
    body = operation.get("requestBody")
    if not body:
        return None
    return body["content"]["application/json"]["schema"].get("$ref")


def _mutating_game_routes() -> list[tuple[str, str]]:
    """`(method, template)` for every mutating operation whose path names
    `{game_id}` - read off the contract rather than listed by hand, so a
    route added tomorrow is covered by the assertions below without anybody
    remembering to add it."""
    spec = _openapi()
    found = []
    for template, operations in sorted(spec["paths"].items()):
        if "{game_id}" not in template:
            continue
        for method in ("post", "put", "patch", "delete"):
            if method in operations:
                found.append((method.upper(), template))
    return found


def _dry_run_capable_routes() -> set[str]:
    """Every POST route whose own declared request model carries a `dry_run`
    field - i.e. the routes that can actually honour one."""
    spec = _openapi()
    schemas = spec["components"]["schemas"]
    capable = set()
    for template, operations in spec["paths"].items():
        ref = _body_ref(operations.get("post") or {})
        if ref is None:
            continue
        properties = schemas.get(ref.split("/")[-1], {}).get("properties") or {}
        if "dry_run" in properties:
            capable.add(template)
    return capable


MUTATING_GAME_ROUTES = _mutating_game_routes()
#: Every mutating `{game_id}` route an advisor has no business reaching at
#: all: it neither declares a `dry_run` nor is one of the two "say
#: something" channels (`game_access.ADVISOR_POST_SUFFIXES`). This is the
#: whole class the old gate waved through on a body carrying one extra key.
NO_DRY_RUN_ROUTES = [
    (method, template)
    for method, template in MUTATING_GAME_ROUTES
    if not (
        method == "POST"
        and (
            template in _dry_run_capable_routes()
            or template.endswith(game_access.ADVISOR_POST_SUFFIXES)
        )
    )
]


def route_url(template: str, game_id: str, card_id_value: str) -> str:
    return (
        template.replace("{game_id}", game_id)
        .replace("{card_id}", card_id_value)
        .replace("{username}", "alice")
    )


def test_the_dry_run_allowlist_matches_the_routes_that_declare_the_field() -> None:
    """`game_access.DRY_RUN_ROUTES` is the gate's own answer to "can this
    route honour a probe", and it must be exactly the set of POST routes
    whose request model declares `dry_run`. Asserted against `openapi.json`
    so it cannot drift when a shape gains or loses the field: add `dry_run`
    to a request model without adding its route here (or the reverse) and
    this fails, rather than the gate quietly refusing a real probe or - far
    worse - waving through a real mutation."""
    assert set(game_access.DRY_RUN_ROUTES) == _dry_run_capable_routes()


@pytest.mark.parametrize(
    "method,template", NO_DRY_RUN_ROUTES, ids=[f"{m} {t}" for m, t in NO_DRY_RUN_ROUTES]
)
def test_an_advisor_uid_is_refused_a_dry_run_a_route_cannot_honour(
    client: TestClient, game: str, method: str, template: str
) -> None:
    """A `dry_run` key on a shape that does not declare one is not a probe -
    it is a key pydantic used to throw away, which is exactly why the gate
    must not read it. Every mutating route that cannot honour one is refused
    with it, not just without it."""
    uid = advisor_uid(game)
    card = card_id(client, game, "Mountain", "alice", "hand")
    url = route_url(template, game, card)

    response = client.request(
        method, url, params={"uid": uid}, json={"player": "alice", "dry_run": True}
    )

    assert is_gate_denial(response, game), (
        f"{method} {template}: an advisor claimed dry_run on a shape that cannot honour one "
        f"and was let through - got {response.status_code} {response.text}"
    )


def test_an_advisor_uid_cannot_mint_itself_a_seat_by_claiming_dry_run(
    client: TestClient, game: str
) -> None:
    """The escalation, pinned forever.

    `app.mint_seat` takes no uid, performs no credential check beyond this
    gate, and returns a *real* seat uid for whatever player the body names.
    `MintSeatRequest` does not declare `dry_run`, so pydantic dropped the
    key and the route committed - the advisor walked out of the call holding
    an ordinary seat credential with no restriction on it at all. The seat
    map is asserted, not only the status code: the point is that nothing was
    minted, not merely that the response looked unhappy."""
    uid = advisor_uid(game)
    before = dict(store.seat_uids(game))

    response = client.post(
        f"/api/games/{game}/seats", params={"uid": uid}, json={"player": "alice", "dry_run": True}
    )

    assert is_gate_denial(response, game), response.text
    assert dict(store.seat_uids(game)) == before, (
        "an advisor minted itself a real seat by claiming dry_run on a shape that has no such field"
    )


def test_an_advisor_uid_cannot_tap_a_card_by_claiming_dry_run(
    client: TestClient, game: str, admin_key: str
) -> None:
    """`tap_card` takes a raw `dict[str, bool]` rather than a model, so
    `extra="forbid"` can never protect it - `{"dry_run": true}` simply
    defaults `tapped` to `True` and taps for real. The gate is the only
    thing that can refuse this one, which is why it has to stop trusting
    the body."""
    uid = advisor_uid(game)
    card = card_id(client, game, "Mountain", "alice", "hand")
    moved = client.post(
        f"/api/games/{game}/cards/{card}/move",
        params={"key": admin_key},
        json={"to_zone": "battlefield"},
    )
    assert moved.status_code == 200, moved.text

    response = client.post(
        f"/api/games/{game}/cards/{card}/tap", params={"uid": uid}, json={"dry_run": True}
    )

    assert is_gate_denial(response, game), response.text
    board = client.get(f"/api/games/{game}", params={"uid": uid}).json()
    assert [c["tapped"] for c in board["battlefield"]] == [False], (
        "an advisor tapped a real card by claiming dry_run"
    )


def test_an_advisor_uid_may_still_probe_every_route_that_declares_dry_run(
    client: TestClient, game: str
) -> None:
    """The other half: narrowing the carve-out to the declared shapes must
    not have narrowed it to nothing. `play-land` is the one exercised end to
    end above; this asserts the gate's own verdict for each of the rest, so
    a route losing its place in `DRY_RUN_ROUTES` is caught here and not by a
    buddy that silently stops being able to ask anything."""
    uid = advisor_uid(game)

    for template in sorted(game_access.DRY_RUN_ROUTES):
        request = gate_request(
            "POST",
            route_url(template, game, "00000000-0000-0000-0000-000000000000"),
            query=f"uid={uid}",
            body={"player": "alice", "dry_run": True},
            route=template,
        )
        assert asyncio.run(game_access.can_access_game(request, game)) is True, template


def test_a_raw_path_that_ends_in_advice_is_not_enough(client: TestClient, game: str) -> None:
    """The gate matches `/advice` and `/report` against the *matched route
    template*, never the caller's own path tail.

    Nothing exploits the old `request.url.path` match today - no mutating
    route ends in a path parameter, and Starlette's `{param}` does not
    match a `/` - but it was a trap set for the first route that does: a
    `POST /api/games/{game_id}/notes/{note}` would have become
    advisor-writable, with no change here and nobody looking. Reading the
    template closes it by construction: the template is a fixed string out
    of the route table, and a caller cannot spell one.
    """
    uid = advisor_uid(game)
    request = gate_request(
        "POST",
        f"/api/games/{game}/notes/advice",
        query=f"uid={uid}",
        body={"text": "not actually advice"},
        route="/api/games/{game_id}/notes/{note}",
    )

    assert asyncio.run(game_access.can_access_game(request, game)) is False


def test_an_unmatched_post_is_refused_rather_than_guessed_at(client: TestClient, game: str) -> None:
    """No matched route means no declared shape to consult, so there is
    nothing the gate could honestly permit: it fails closed."""
    uid = advisor_uid(game)
    request = gate_request(
        "POST", f"/api/games/{game}/report", query=f"uid={uid}", body={"text": "x"}
    )
    del request.scope["route"]

    assert asyncio.run(game_access.can_access_game(request, game)) is False


# ---------------------------------------------------------------------------
# visibility is not authorization: the per-route seat checks hold on their own
# ---------------------------------------------------------------------------
#
# `visibility.resolve_viewer` answering an advisor uid with its human's
# username is the whole design of the *read* filter - it is what makes the
# advisor's view byte-identical to the seat's by construction. But four
# routes used that same function as an authorization check ("does this uid
# open the seat this request names"), and widening it widened them too: they
# started saying yes to an advisor, leaving this module's gate as the only
# thing refusing. One bug there was then the entire guarantee.
#
# Every test below stands the gate down on purpose. What it asserts is the
# *second* line of defence: each route resolves the acting seat through
# `game_access.seat_for_uid`, which never resolves an advisor, so the refusal
# happens even with the gate wide open.


@pytest.fixture()
def _gate_stood_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """`game_access.can_access_game` replaced by "yes, always".

    Not a claim that the gate is broken - it is tested above. This is how
    the tests below can see whether anything *else* would refuse, which is
    the only question worth asking about defence in depth.
    """

    async def always(request, game_id):  # noqa: ANN001, ARG001
        return True

    monkeypatch.setattr(game_access, "can_access_game", always)


def test_seat_for_uid_never_answers_an_advisor_uid(client: TestClient, game: str) -> None:
    """The two questions, side by side, on the same uid: `resolve_viewer`
    says "you may see alice's cards" and `seat_for_uid` says "you are not
    alice"."""
    uid = advisor_uid(game, "alice")

    assert visibility.resolve_viewer(game, uid) == "alice"
    assert game_access.seat_for_uid(game, uid) is None
    assert game_access.seat_for_uid(game, seat_uid(game, "alice")) == "alice"
    assert game_access.seat_for_uid(game, None) is None
    assert game_access.seat_for_uid(game, "not-a-real-uid") is None


def test_decide_refuses_an_advisor_uid_with_the_gate_stood_down(
    client: TestClient, game: str, _gate_stood_down: None
) -> None:
    uid = advisor_uid(game, "alice")

    response = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uid},
        json={"player": "alice", "choice": "G"},
    )

    assert response.status_code == 200, response.text
    assert response.json().get("reason") == "that uid does not open the deciding player's seat", (
        response.text
    )


def test_a_declaration_refuses_an_advisor_uid_with_the_gate_stood_down(
    client: TestClient, game: str, admin_key: str, _gate_stood_down: None
) -> None:
    """`_answer_declaration` is the shared body of `actions/attack` and
    `actions/declare-blockers`; both reach the same seat check, and it sits
    *after* "is there such a decision pending", so this drives a real
    CR 508.1 declare-attackers question into being first - otherwise the
    refusal under test is never the one the route reaches."""
    uid = advisor_uid(game, "alice")
    stepped = client.put(
        f"/api/games/{game}/step", params={"key": admin_key}, json={"step": "beginning_of_combat"}
    )
    assert stepped.status_code == 200, stepped.text
    advanced = client.post(
        f"/api/games/{game}/advance",
        params={"uid": seat_uid(game, "alice")},
        json={"player": "alice"},
    )
    assert advanced.status_code == 200, advanced.text
    assert advanced.json().get("paused") is True, advanced.text

    response = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uid},
        json={"attackers": []},
    )

    assert response.status_code == 200, response.text
    assert response.json().get("reason") == "that uid does not open the deciding player's seat", (
        response.text
    )


@pytest.mark.parametrize(
    "suffix,body",
    [
        ("pregame/mulligan", {"player": "alice", "take": True}),
        (
            "pregame/opening-hand/action",
            {"player": "alice", "card_id": "00000000-0000-0000-0000-000000000000"},
        ),
        ("pregame/opening-hand/finish", {"player": "alice"}),
    ],
)
def test_the_pregame_routes_refuse_an_advisor_uid_with_the_gate_stood_down(
    client: TestClient, game: str, _gate_stood_down: None, suffix: str, body: dict
) -> None:
    """`_pregame_seat` is the shared check behind all three."""
    uid = advisor_uid(game, "alice")

    response = client.post(f"/api/games/{game}/{suffix}", params={"uid": uid}, json=body)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "that uid does not open alice's seat", response.text


def test_the_pregame_routes_still_accept_the_admin_key_with_the_gate_stood_down(
    client: TestClient, game: str, admin_key: str, _gate_stood_down: None
) -> None:
    """The other half of `_pregame_seat`: the operator's own credential is
    still enough to drive a seat it owns, and it is still recognised in the
    `uid` slot (`visibility.resolve_viewer`'s own convention, which every
    harness and boot script in this repo already uses)."""
    response = client.post(
        f"/api/games/{game}/pregame/mulligan",
        params={"uid": admin_key},
        json={"player": "alice", "take": True},
    )

    assert response.status_code == 400, response.text
    assert "mulligan process is not under way" in response.text, response.text


# ---------------------------------------------------------------------------
# /report: a row is attributed to the credential, not to the request body
# ---------------------------------------------------------------------------


def test_a_report_is_attributed_to_the_credential_that_filed_it(
    client: TestClient, game: str, admin_key: str
) -> None:
    """`/report` is one of the two POSTs an advisor may make, and
    `report_problem` attributed its ledger row to `req.player` - a plain
    request field. So an advisor of alice (or any seat) could file a row into
    the log in *bob's* name, and nothing downstream would say otherwise. The
    row is attributed to whoever the credential resolves to now; for an
    advisor uid that is the human it watches."""
    uid = advisor_uid(game, "alice")

    response = client.post(
        f"/api/games/{game}/report",
        params={"uid": uid},
        json={"player": "bob", "text": "bob is cheating, signed bob"},
    )

    assert response.status_code == 200, response.text
    log = client.get(f"/api/games/{game}/log", params={"uid": admin_key}).json()["log"]
    row = next(entry for entry in log if entry["action"] == "report_problem")
    assert row["detail"]["player"] == "alice", row


def test_a_seats_own_report_is_attributed_to_that_seat(
    client: TestClient, game: str, admin_key: str
) -> None:
    uid = seat_uid(game, "bob")

    response = client.post(
        f"/api/games/{game}/report",
        params={"uid": uid},
        json={"player": "alice", "text": "the engine let me block with a tapped creature"},
    )

    assert response.status_code == 200, response.text
    log = client.get(f"/api/games/{game}/log", params={"uid": admin_key}).json()["log"]
    row = next(entry for entry in log if entry["action"] == "report_problem")
    assert row["detail"]["player"] == "bob", row


def test_the_admin_key_still_files_a_report_for_the_player_it_names(
    client: TestClient, game: str, admin_key: str
) -> None:
    """The admin key opens no seat, so there is no resolved player to
    attribute to and the body's own `player` is all there is - the operator
    filing on a seat's behalf, which is what that field is left on the
    request for."""
    response = client.post(
        f"/api/games/{game}/report",
        params={"key": admin_key},
        json={"player": "bob", "text": "filed by the operator"},
    )

    assert response.status_code == 200, response.text
    log = client.get(f"/api/games/{game}/log", params={"uid": admin_key}).json()["log"]
    row = next(entry for entry in log if entry["action"] == "report_problem")
    assert row["detail"]["player"] == "bob", row


# ---------------------------------------------------------------------------
# a field a route cannot honour is a 422, not a discarded key
# ---------------------------------------------------------------------------


def test_a_request_model_refuses_a_field_it_does_not_declare(
    client: TestClient, game: str, admin_key: str
) -> None:
    """`schemas.RequestModel`'s whole reason for existing.

    Pydantic's default is to discard an undeclared key silently, which is how
    a `dry_run` the route could not honour became a real mutation: the gate
    believed the body and the route never saw the field at all.
    `MintSeatRequest.model_validate({"player": "alice", "dry_run": True})`
    answered `player='alice'`. Now the caller is told."""
    response = client.post(
        f"/api/games/{game}/seats",
        params={"key": admin_key},
        json={"player": "alice", "dry_run": True},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "dry_run"], response.text


def test_every_request_body_in_the_contract_forbids_undeclared_fields() -> None:
    """The invariant, checked against the contract rather than a list kept by
    hand: every schema `openapi.json` names as a request body (transitively -
    a nested `ManaSource` is as much a request shape as the `CastRequest`
    holding it) must say `additionalProperties: false`, which is how pydantic
    renders `extra="forbid"`. A request model added tomorrow that forgets the
    base class fails here."""
    spec = _openapi()
    schemas = spec["components"]["schemas"]

    def refs(node: object) -> list[str]:
        if isinstance(node, dict):
            found = []
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    found.append(value.split("/")[-1])
                else:
                    found += refs(value)
            return found
        if isinstance(node, list):
            return [name for item in node for name in refs(item)]
        return []

    pending = []
    for operations in spec["paths"].values():
        for operation in operations.values():
            body = (operation or {}).get("requestBody")
            if body:
                pending += refs(body["content"].get("application/json", {}).get("schema", {}))
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending += refs(schemas.get(name, {}))

    assert seen, "no request bodies found in the contract - the reader is wrong, not the api"
    #: The one deliberate exemption, argued in full at `schemas.
    #: ActivateRequest`: the browser sends `shares` for a divided activated
    #: ability, this shape has never declared it, and honouring it needs a
    #: change in the engine repo - so it is accepted and dropped as it always
    #: was, rather than becoming a 422 for a human player mid-game.
    exempt = {"ActivateRequest"}
    permissive = sorted(
        name
        for name in seen - exempt
        if schemas.get(name, {}).get("additionalProperties") is not False
    )
    assert exempt <= seen, "the exemption names a shape that is no longer a request body"
    assert permissive == [], (
        "these request shapes still discard an undeclared field instead of refusing it - "
        f"inherit `schemas.RequestModel`: {permissive}"
    )
