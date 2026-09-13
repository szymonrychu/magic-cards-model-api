"""A mutating call on a game must present a credential for that game.

`game_access.py`'s original rule was a deny-list: an advisor uid may not
act. A deny-list keyed on a credential cannot restrict a caller who simply
omits the credential, and that is exactly what the buddy this repo is
building could do - it already knows the `game_id` (it needs it to read), so
dropping the `uid` parameter walked straight past its own restriction. With
no uid the advisor branch never ran, and an ownerless game - which
`game_access.py`'s own migration note says is *every* game in this
deployment, since no bearer token has ever reached it - answered `True` to
anybody who knew its id.

So the gate needs a positive rule as well as a negative one:

**POST/PUT/PATCH/DELETE on a `{game_id}` route requires a credential that
resolves to a seat of that game, that game's admin key, or a bearer token
naming that game's owner.** Reads are untouched: an ownerless game is
exactly as readable as it has always been (`test_user_separation.py` pins
that), because "world-readable" is the deployment's current shape and
"world-writable" is what makes the owner's chosen product requirement - a
buddy that cannot act - unachievable.

The routes are enumerated from the live route table rather than listed here,
so a mutating route added tomorrow is covered without anyone remembering to
add it.

Every test in this file opts out of `conftest.py`'s harness credential (the
`no_harness_credential` marker): the whole point is to send a request with
nothing attached, which is precisely what that fixture exists to stop
happening by accident everywhere else.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from magic_cards_model_api.app import app as raw_app
from magic_cards_model_api.store import store

pytestmark = pytest.mark.no_harness_credential

_DUMMY_PATH_PARAMS = {
    "card_id": "00000000-0000-0000-0000-000000000000",
    "username": "alice",
    # `POST .../advice/{seq}/approve` (B2). The value never has to resolve to
    # a real ledger row: the gate this file tests runs before the route body,
    # so every case here is refused on the missing credential first.
    "seq": "1",
    # `PUT .../seats/{uid}/scripts` (A4): the uid a corpus is registered
    # under. Never a credential - the gate reads `?uid=` off the query.
    "uid": "not-a-real-uid",
}


def _mutating_game_operations() -> list[tuple[str, str]]:
    """`(method, template)` for every mutating operation whose route names
    `{game_id}`, read straight off the live route table."""
    found: list[tuple[str, str]] = []
    for route in raw_app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or "{game_id}" not in path:
            continue
        for method in sorted(methods):
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                found.append((method, path))
    return found


MUTATING_OPERATIONS = _mutating_game_operations()


def _concrete(template: str, game_id: str) -> str:
    params = {**_DUMMY_PATH_PARAMS, "game_id": game_id}

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        # A new mutating route with a path parameter nobody has given a dummy
        # for is exactly the case this file is enumerated to catch, so say so
        # instead of raising a bare `KeyError` from inside a lambda.
        assert name in params, (
            f"{template} names a path parameter `{name}` with no dummy value - "
            "add one to `_DUMMY_PATH_PARAMS`, do not narrow the enumeration"
        )
        return params[name]

    return re.sub(r"\{(\w+)\}", value, template)


def _is_gate_denial(response, game_id: str) -> bool:
    if response.status_code != 404:
        return False
    try:
        return response.json().get("detail") == f"no game {game_id}"
    except Exception:
        return False


def seat_uid(game_id: str, player: str) -> str:
    return next(uid for uid, name in store.seat_uids(game_id).items() if name == player)


def test_the_route_table_still_resolves_to_something() -> None:
    assert len(MUTATING_OPERATIONS) >= 30, MUTATING_OPERATIONS


@pytest.mark.parametrize(
    "method,template", MUTATING_OPERATIONS, ids=[f"{m} {t}" for m, t in MUTATING_OPERATIONS]
)
def test_a_mutation_with_no_credential_at_all_is_refused(
    client: TestClient, game: str, method: str, template: str
) -> None:
    """The hole, route by route. `POST .../actions/pass-priority` passed a
    real priority and `POST .../seats` minted a real, unrestricted seat
    credential, both for a caller that presented nothing whatsoever."""
    response = client.request(method, _concrete(template, game), json={"player": "alice"})

    assert _is_gate_denial(response, game), (
        f"{method} {template}: a caller presenting no credential at all was let through - "
        f"got {response.status_code} {response.text}"
    )


def test_no_credential_means_nothing_happened_not_merely_a_sad_answer(
    client: TestClient, game: str
) -> None:
    """The status code is not the point; the board is. Priority must not have
    moved and no seat must have been minted."""
    before_seats = dict(store.seat_uids(game))
    before_log = client.get(f"/api/games/{game}/log").json()["last_seq"]

    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    client.post(f"/api/games/{game}/seats", json={"player": "alice"})

    assert dict(store.seat_uids(game)) == before_seats
    assert client.get(f"/api/games/{game}/log").json()["last_seq"] == before_log


def test_a_bogus_credential_is_no_better_than_none(client: TestClient, game: str) -> None:
    for params in ({"uid": "not-a-real-uid"}, {"key": "not-a-real-key"}):
        response = client.post(
            f"/api/games/{game}/actions/pass-priority", params=params, json={"player": "alice"}
        )
        assert _is_gate_denial(response, game), f"{params}: {response.status_code} {response.text}"


# ---------------------------------------------------------------------------
# the other half: every credential that was already good stays good
# ---------------------------------------------------------------------------


def test_a_seat_uid_still_acts(client: TestClient, game: str) -> None:
    uid = seat_uid(game, "alice")

    response = client.post(
        f"/api/games/{game}/actions/pass-priority", params={"uid": uid}, json={"player": "alice"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is True, response.text


def test_the_admin_key_still_acts_in_the_key_slot(
    client: TestClient, game: str, admin_key: str
) -> None:
    response = client.post(
        f"/api/games/{game}/actions/pass-priority",
        params={"key": admin_key},
        json={"player": "alice"},
    )

    assert response.status_code == 200, response.text


def test_the_admin_key_still_acts_in_the_uid_slot(
    client: TestClient, game: str, admin_key: str
) -> None:
    """The admin key in the `uid` parameter is this repo's own long-standing
    convention - `visibility.resolve_viewer` checks `store.is_admin` on `uid`
    before anything else, and `POST .../pregame/settle` *requires* it that
    way. The gate has to recognise it in either slot or every harness and
    boot script in the repo breaks."""
    response = client.post(
        f"/api/games/{game}/actions/pass-priority",
        params={"uid": admin_key},
        json={"player": "alice"},
    )

    assert response.status_code == 200, response.text


def test_a_credential_in_the_body_still_acts(client: TestClient, game: str) -> None:
    """`POST .../plan`, `POST .../reflection` and the two `ai-seat` routes
    carry their uid in the JSON body rather than the query string, and
    `game_access._credentials` has always read both."""
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


def test_reads_of_an_ownerless_game_are_untouched(client: TestClient, game: str) -> None:
    """The migration story this positive rule deliberately does not change:
    an ownerless game stays as readable as it has always been."""
    assert client.get(f"/api/games/{game}").status_code == 200
    assert client.get(f"/api/games/{game}/log").status_code == 200
    assert client.get(f"/api/lobby/games/{game}/seats").status_code == 200
