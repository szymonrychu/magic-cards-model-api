"""One game this build cannot decode must never take down an endpoint that
lists many.

The live outage this pins: the engine's `Game.CURRENT_SCHEMA_VERSION` moved
to 5 while production postgres still held rows written at 3.
`Game._check_schema_version` refuses a mismatched snapshot with
`SchemaVersionMismatch` - deliberately a `RuntimeError` so pydantic will not
rewrap it into a `ValidationError` - and every read path decoded every
matching row unconditionally, so a single stale row 500'd `GET /api/games`,
`GET /api/lobby/games` and, through `reserve_slot`'s own
`_live_ui_game_ids` count, `POST /api/lobby/games` as well. The lobby was
down for games that decoded perfectly well.

The engine's refusal is not the defect and is not weakened here: a snapshot
whose behaviour was recorded under another engine version genuinely must not
be replayed. The defect is the api decoding a batch as though every row in
it were guaranteed to parse. So: a row that cannot be decoded is *skipped*
by every batch read, *named* (in the engine's own words) in the payload that
skipped it, and *refused with a 409* when someone opens it directly.

**The fixture is a genuinely schema-3-shaped payload, not a relabelled
one.** `_schema_3_state` plays two real decklists through the real
`load_deck_into_game` - the exact call `POST /api/lobby/games` makes - dumps
the resulting `Game`, and then walks that dump back over the two bumps the
engine itself records since version 3 (`Game.CURRENT_SCHEMA_VERSION`'s own
docstring): `Game.keyword_line_notices`, added at 4 -> 5, and
`TargetRef.zone_change_stamp`, added at 3 -> 4. Everything else in it is
byte-for-byte what the engine wrote.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import require_postgres_database_url
from fastapi.testclient import TestClient
from magic_cards_model.models import Game, SchemaVersionMismatch
from sqlalchemy import text
from test_decks_api import LEGAL_DECK, LEGAL_DECK_B

from magic_cards_model_api import audit
from magic_cards_model_api.db import make_engine
from magic_cards_model_api.decks import load_deck_into_game
from magic_cards_model_api.repository import GameUnloadable, InMemoryRepository
from magic_cards_model_api.repository_postgres import PostgresRepository

_TEST_DATABASE_URL_ENV = "MCM_TEST_DATABASE_URL"

#: The schema version production's four stale rows were written at, and the
#: one this file's fixture reproduces.
STALE_SCHEMA_VERSION = 3


def _strip_key(node: Any, key: str) -> None:
    """Remove `key` wherever it appears in a decoded-JSON tree - how a field
    that a later schema version *added* looks in a snapshot written before
    it existed."""
    if isinstance(node, dict):
        node.pop(key, None)
        for value in node.values():
            _strip_key(value, key)
    elif isinstance(node, list):
        for value in node:
            _strip_key(value, key)


def _schema_3_state() -> dict[str, Any]:
    """A real board, dumped by the real engine, walked back to schema 3."""
    match = load_deck_into_game([("alice", LEGAL_DECK), ("bob", LEGAL_DECK_B)], seed=11)
    state = json.loads(json.dumps(match.games[-1].model_dump(mode="json")))
    # 4 -> 5 added `Game.keyword_line_notices`.
    state.pop("keyword_line_notices", None)
    # 3 -> 4 added `TargetRef.zone_change_stamp`, wherever a `TargetRef` sits.
    _strip_key(state, "zone_change_stamp")
    state["schema_version"] = STALE_SCHEMA_VERSION
    return state


def engine_refusal() -> str:
    """The engine's own message for this fixture - never a message this test
    invented. Every assertion below about *what the api said* is checked
    against this string, so the api can only ever be reported as quoting the
    engine verbatim."""
    try:
        Game.model_validate(_schema_3_state())
    except SchemaVersionMismatch as exc:
        return str(exc)
    raise AssertionError("the engine accepted a schema-3 snapshot")


def test_the_schema_3_fixture_is_genuinely_refused_by_the_engine() -> None:
    """The anchor for everything else here: this payload is refused by the
    real validator, for the real production reason, and the refusal is a
    `RuntimeError` (never a `ValueError`) exactly as the engine intends."""
    with pytest.raises(SchemaVersionMismatch) as caught:
        Game.model_validate(_schema_3_state())
    assert isinstance(caught.value, RuntimeError)
    assert not isinstance(caught.value, ValueError)
    assert f"schema_version={STALE_SCHEMA_VERSION!r}" in str(caught.value)
    assert f"decodes schema_version={Game.CURRENT_SCHEMA_VERSION}" in str(caught.value)


# --------------------------------------------------------------------------
# The postgres repository - where the outage actually happened. `mise run
# test:pg` runs these; the plain `mise run test` skips them (there is no
# in-memory analogue: `InMemoryRepository` holds live `Game` objects and
# never decodes anything).
# --------------------------------------------------------------------------


@pytest.fixture
def pg() -> PostgresRepository:
    url = require_postgres_database_url(_TEST_DATABASE_URL_ENV)
    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE records, games RESTART IDENTITY CASCADE"))
    return PostgresRepository(url, engine=engine)


def _real_game() -> Game:
    return load_deck_into_game([("alice", LEGAL_DECK), ("bob", LEGAL_DECK_B)], seed=3).games[-1]


def _stale_row(repo: PostgresRepository, *, ui_game: bool) -> str:
    """One row exactly as production holds it: created by this application,
    with a `state` column an older build wrote. The `version` bump is what a
    real other-process write would have done, and is what makes this
    instance's identity-preserving cache re-read the column."""
    game_id = repo.create(_real_game())
    if ui_game:
        repo.register_ui_game(game_id)
    with repo._engine.begin() as conn:  # noqa: SLF001 - fixture setup, not a call site
        conn.execute(
            text("UPDATE games SET state = :state, version = version + 1 WHERE id = :id"),
            {"state": json.dumps(_schema_3_state()), "id": game_id},
        )
    return game_id


def test_ids_still_lists_a_row_it_cannot_decode(pg: PostgresRepository) -> None:
    good = pg.create(_real_game())
    stale = _stale_row(pg, ui_game=False)
    assert sorted(pg.ids()) == sorted([good, stale])


def test_get_refuses_an_undecodable_row_in_the_engines_own_words(
    pg: PostgresRepository,
) -> None:
    stale = _stale_row(pg, ui_game=False)
    with pytest.raises(GameUnloadable) as caught:
        pg.get(stale)
    assert caught.value.game_id == stale
    assert caught.value.reason == engine_refusal()


def test_live_ui_games_survives_an_undecodable_live_row_and_still_counts_it(
    pg: PostgresRepository,
) -> None:
    """A mix, with the undecodable one live: the good games still come back,
    and the bad row is counted as occupying its slot - see
    `_live_ui_game_ids`'s own note on why an unreadable row is not assumed
    to be finished."""
    good = pg.create(_real_game())
    pg.register_ui_game(good)
    stale = _stale_row(pg, ui_game=True)
    live = pg.live_ui_games()
    assert good in live
    assert stale in live


def test_reserve_slot_still_hands_out_a_slot_beside_an_undecodable_live_row(
    pg: PostgresRepository,
) -> None:
    """The half of the outage that broke *creating* a game: `reserve_slot`
    counts live ui games, and counting them decoded every one of them."""
    stale = _stale_row(pg, ui_game=True)
    token = pg.reserve_slot(4)
    assert token is not None
    fresh = pg.create(_real_game())
    pg.attach(token, fresh)
    assert sorted(pg.live_ui_games()) == sorted([stale, fresh])


def test_reserve_slot_refuses_once_undecodable_rows_alone_fill_the_cap(
    pg: PostgresRepository,
) -> None:
    _stale_row(pg, ui_game=True)
    _stale_row(pg, ui_game=True)
    assert pg.reserve_slot(2) is None


def test_every_live_row_being_undecodable_is_not_fatal(pg: PostgresRepository) -> None:
    _stale_row(pg, ui_game=True)
    _stale_row(pg, ui_game=True)
    assert len(pg.live_ui_games()) == 2
    assert len(pg.ids()) == 2


def test_delete_still_works_on_a_row_it_cannot_decode(pg: PostgresRepository) -> None:
    """The recovery path, and the reason counting an undecodable row against
    the cap can never wedge the lobby forever."""
    stale = _stale_row(pg, ui_game=True)
    assert pg.delete(stale) is True
    assert pg.live_ui_games() == []


def test_archived_reports_an_undecodable_snapshot_instead_of_raising(
    pg: PostgresRepository,
) -> None:
    """The read the post-game review is built on. `archived()` decodes the
    same `state` column every other read does, so a stale row used to make
    `GET .../review` 409 - and the reasoning of a game somebody played to
    the end became permanently unreachable, which is the one thing the
    review exists to prevent. It now reports the refusal as data
    (`ArchivedGame.unloadable`, the engine's own words) and still hands back
    every plain column beside it, none of which needs decoding."""
    stale = _stale_row(pg, ui_game=True)
    pg.register_seats(stale, {"uid-alice": "alice", "uid-bob": "bob"})

    archived = pg.archived(stale)
    assert archived is not None
    assert archived.game is None
    assert archived.unloadable == engine_refusal()
    assert archived.game_id == stale
    assert archived.seat_uids == {"uid-alice": "alice", "uid-bob": "bob"}
    assert archived.admin_key is not None
    assert archived.created_at is not None
    assert archived.deleted_at is None


def test_archived_still_reports_an_undecodable_snapshot_after_the_soft_delete(
    pg: PostgresRepository,
) -> None:
    """End game is the documented way to get an undecodable game's slot
    back, so "ended AND undecodable" is a state production reaches on
    purpose - and it is exactly the state whose review must still read."""
    stale = _stale_row(pg, ui_game=True)
    pg.register_seats(stale, {"uid-alice": "alice"})
    assert pg.delete(stale) is True

    archived = pg.archived(stale)
    assert archived is not None
    assert archived.game is None
    assert archived.unloadable == engine_refusal()
    assert archived.deleted_at is not None
    assert archived.seat_uids == {"uid-alice": "alice"}


def test_archived_carries_no_refusal_for_a_row_it_can_decode(
    pg: PostgresRepository,
) -> None:
    """The other half: nothing about the ordinary path changed."""
    good = pg.create(_real_game())
    archived = pg.archived(good)
    assert archived is not None
    assert archived.game is not None
    assert archived.unloadable is None


# --------------------------------------------------------------------------
# The HTTP surface. Runs on the ordinary in-memory singleton with
# `InMemoryRepository.get` refusing designated ids - patched on the *class*,
# so every module that bound the singleton at import (`app`, `decks_api`,
# `lobby`, `game_access`, `api_client`, `audit`) sees the same refusal
# without any of them being rebound. The refusal it raises carries the real
# engine message, taken from the real validator.
# --------------------------------------------------------------------------


@pytest.fixture
def unloadable(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A mutable `{game_id: reason}` - add an id to it and every read of that
    game refuses exactly the way a stale postgres row does."""
    poisoned: dict[str, str] = {}
    real_get = InMemoryRepository.get

    def get(self: InMemoryRepository, game_id: str) -> Game | None:
        reason = poisoned.get(game_id)
        if reason is not None:
            raise GameUnloadable(game_id, reason)
        return real_get(self, game_id)

    monkeypatch.setattr(InMemoryRepository, "get", get)
    return poisoned


def _lobby_game(client: TestClient, alice: str, bob: str) -> dict[str, Any]:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": alice, "decklist_text": LEGAL_DECK},
                {"username": bob, "decklist_text": LEGAL_DECK_B},
            ],
            "seed": 5,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_listing_games_survives_one_that_cannot_be_decoded(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    first = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    second = client.post(
        "/api/games", json={"players": [{"username": "carol"}, {"username": "dave"}]}
    ).json()
    unloadable[second["game_id"]] = engine_refusal()

    response = client.get("/api/games")
    assert response.status_code == 200, response.text
    listed = {entry["game_id"]: entry for entry in response.json()["games"]}
    assert set(listed) == {first["game_id"], second["game_id"]}
    assert listed[first["game_id"]].get("unloadable") is None
    assert listed[second["game_id"]]["unloadable"] == engine_refusal()


def test_listing_survives_every_game_being_undecodable(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    ids = [
        client.post(
            "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
        ).json()["game_id"]
        for _ in range(2)
    ]
    for game_id in ids:
        unloadable[game_id] = engine_refusal()

    response = client.get("/api/games")
    assert response.status_code == 200, response.text
    listed = response.json()["games"]
    assert sorted(entry["game_id"] for entry in listed) == sorted(ids)
    assert all(entry["unloadable"] == engine_refusal() for entry in listed)


def test_the_summary_route_flags_an_undecodable_game_rather_than_dropping_it(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    created = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    unloadable[created["game_id"]] = engine_refusal()

    response = client.get("/api/games/summary", params={"ids": created["game_id"]})
    assert response.status_code == 200, response.text
    summaries = response.json()["summaries"]
    assert [entry["game_id"] for entry in summaries] == [created["game_id"]]
    assert summaries[0]["unloadable"] == engine_refusal()


def test_the_lobby_lists_a_game_it_cannot_decode_rather_than_vanishing_it(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    good = _lobby_game(client, "alice", "bob")
    bad = _lobby_game(client, "carol", "dave")
    unloadable[bad["game_id"]] = engine_refusal()
    uids = ",".join([*good["seat_uids"].values(), *bad["seat_uids"].values()])

    response = client.get("/api/lobby/games", params={"uids": uids})
    assert response.status_code == 200, response.text
    listed = {entry["game_id"]: entry for entry in response.json()["games"]}
    assert set(listed) == {good["game_id"], bad["game_id"]}
    assert listed[good["game_id"]].get("unloadable") is None

    entry = listed[bad["game_id"]]
    assert entry["unloadable"] == engine_refusal()
    # Nothing in the ledger says this game ended, so nothing here claims it
    # did - and the lobby offers no review door for it.
    assert entry["recorded_ending"] is None
    # The seats are still named - a game the user played must not read as a
    # game with no players. None of this comes from the snapshot.
    assert sorted(entry["players"]) == ["carol", "dave"]
    assert entry["seats"] == bad["seat_uids"]
    assert entry["turn_number"] is None
    assert entry["phase"] is None
    assert entry["is_over"] is False


def test_a_new_lobby_game_can_still_be_started_beside_an_undecodable_one(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    """The half of the outage that broke creating: the lobby's own boot path
    counts every live ui game."""
    bad = _lobby_game(client, "alice", "bob")
    unloadable[bad["game_id"]] = engine_refusal()
    fresh = _lobby_game(client, "carol", "dave")
    assert fresh["game_id"] != bad["game_id"]


def test_opening_an_undecodable_game_is_a_409_naming_the_mismatch(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    created = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    unloadable[created["game_id"]] = engine_refusal()

    response = client.get(f"/api/games/{created['game_id']}", params={"key": created["admin_key"]})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["detail"] == engine_refusal()
    assert body["game_id"] == created["game_id"]
    assert body["unloadable"] is True


def test_an_undecodable_game_can_still_be_ended_from_the_lobby(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    """The escape hatch that makes counting an unloadable game against
    `MAX_LIVE_GAMES` recoverable rather than permanent."""
    bad = _lobby_game(client, "alice", "bob")
    unloadable[bad["game_id"]] = engine_refusal()
    uid = next(iter(bad["seat_uids"].values()))

    response = client.delete(f"/api/lobby/games/{bad['game_id']}", params={"uid": uid})
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    assert bad["game_id"] not in client.get("/api/lobby/games").json()["games"]


def test_the_lobby_names_the_ledgers_record_of_an_undecodable_games_ending(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    """The row that a person who played a game to the end has to be able to
    act on. `is_over` stays `False` for an undecodable row - CR 104.1 cannot
    be judged on a board nobody can read, and that value is what
    `MAX_LIVE_GAMES` counts - but the ledger's own `game_over` row is a
    different kind of evidence: it was written when it happened, by the build
    that could still read the board. Naming it is what lets the lobby offer
    that game's review knowing `GET .../review` will answer it (rebuilt from
    the same ledger) rather than 409."""
    bad = _lobby_game(client, "alice", "bob")
    game_id = bad["game_id"]
    conceded = client.post(
        f"/api/games/{game_id}/actions/concede",
        json={"player": "alice", "uid": bad["seat_uids"]["alice"]},
    )
    assert conceded.status_code == 200, conceded.text
    ending = [e for e in audit.entries(game_id) if e["action"] == audit.GAME_OVER_ACTION]
    assert len(ending) == 1, "the game really did finish before the schema moved under it"

    unloadable[game_id] = engine_refusal()
    listed = {
        entry["game_id"]: entry
        for entry in client.get(
            "/api/lobby/games", params={"uids": ",".join(bad["seat_uids"].values())}
        ).json()["games"]
    }
    entry = listed[game_id]
    assert entry["unloadable"] == engine_refusal()
    assert entry["recorded_ending"] == ending[0]["seq"]
    assert entry["is_over"] is False, (
        "unchanged: `is_over` is a fact about a board this build cannot read, "
        "and it is what the live-game cap counts"
    )

    # And the door it justifies actually opens.
    review = client.get(f"/api/games/{game_id}/review", params={"uid": bad["seat_uids"]["alice"]})
    assert review.status_code == 200, review.text
    assert review.json()["reconstructed"] is True
