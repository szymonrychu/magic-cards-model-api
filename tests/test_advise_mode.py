"""Advise mode: the buddy gets the same 19 tools, and "advise" is a property
of the uid's role rather than a separate tool surface.

Wave 1 (`test_game_access_advisor.py`) gave the advisor credential its own
storage, its own read-only gate and its own `role` in `GET /api/seats/{uid}`.
This is the other half: what the toolset *does* with that role.

- `Seats.seat_for` reads `role` off `GET /api/seats/{uid}` and builds
  `Seat(advise=True)` for an advisor - identically for stdio (`--game`
  pinned), shared-HTTP and `mcp-seat` subprocess mode, with no new flag, no
  MCP config change and no new Job placeholder. A buddy pod is the seat Job
  with a different uid.
- `Seat.call`/`Seat.call_json` - the one place every HTTP request leaves -
  turn an advise seat's *committing* POST into a **proposal**: validated with
  `dry_run: true` where the request shape supports it (exactly 7 of the 12
  action shapes), recorded as one `advice` ledger row, and handed back as
  `{"proposed": true, ...}`. The game never moves.
- The 5 shapes with no `dry_run` are the dangerous case. Wave 1's gate
  authorises a POST whose raw body says `dry_run: true`, so a model that does
  not declare the field could have the route commit for real - therefore for
  those 5 shapes **no mutating request is sent at all**: the proposal is
  built from `available_actions` and marked `validated: false`.
- `run_action` never auto-passes for an advise seat, and `pass_until` is
  refused outright: either would be the advisor driving the game.

A seat (non-advisor) `Seat` must be completely unchanged on every one of
these paths, which is what the `seat`-parameterised half of each test below
pins.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import add_card, card_id, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api import audit, protocol, seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.seatkit import Seat, Seats, pass_until, run_action
from magic_cards_model_api.store import store

REPO_ROOT = Path(__file__).resolve().parents[1]

ADVISOR_UID = "buddy-uid"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class RecordingClient:
    """A `TestClient` that remembers every request made through it.

    "Sent no mutating request at all" is the whole acceptance condition for
    the 5 `dry_run`-less shapes, and it is not observable from the board
    alone: a route that refused is indistinguishable from one that was never
    called. This records the wire.
    """

    def __init__(self, sent: list[tuple[str, str]]) -> None:
        self._inner = TestClient(app)
        self._sent = sent
        self.timeout = 5.0

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self._sent.append((method.upper(), str(url)))
        return self._inner.request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture()
def sent() -> list[tuple[str, str]]:
    """Every (method, url) the seats under test put on the wire."""
    return []


@pytest.fixture()
def wired(monkeypatch: pytest.MonkeyPatch, sent: list[tuple[str, str]]) -> None:
    """Point `seatkit`'s own `httpx.Client` at the in-process app, through
    the recorder - the same monkeypatch `test_action_flow.py` uses."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: RecordingClient(sent))


def advisor_uid(game_id: str, player: str = "alice", uid: str = ADVISOR_UID) -> str:
    """Register `uid` as an advisor on `player`'s seat (the lobby opt-in and
    the buddy pod that mint one are a later task - wave 1's own tests
    register through the repository the same way)."""
    store.register_advisors(game_id, {uid: Advisor(player=player)})
    return uid


def seat_uid(game_id: str, player: str = "alice") -> str:
    return next(uid for uid, name in store.seat_uids(game_id).items() if name == player)


def mutating_posts(sent: list[tuple[str, str]], game_id: str) -> list[tuple[str, str]]:
    """Every POST on the wire that was not one of the two channels an advisor
    is allowed to write into (`/advice`, `/report`), and not a `dry_run`
    probe (whose query string is indistinguishable here - see the callers,
    which assert on the path)."""
    return [
        (method, url)
        for method, url in sent
        if method == "POST" and "/advice" not in url and "/report" not in url
    ]


def seats_for(game_id: str, *, pinned: bool = True) -> Seats:
    return Seats(
        api="http://testserver",
        game_id=game_id if pinned else None,
        resolve_by_uid=not pinned,
    )


def buddy(game_id: str, *, pinned: bool = True, player: str = "alice") -> Seat:
    advisor_uid(game_id, player)
    seat = seats_for(game_id, pinned=pinned).seat_for(ADVISOR_UID)
    assert seat is not None
    return seat


def player_seat(game_id: str, *, pinned: bool = True, player: str = "alice") -> Seat:
    seat = seats_for(game_id, pinned=pinned).seat_for(seat_uid(game_id, player))
    assert seat is not None
    return seat


def stock_a_bolt(client: TestClient, game_id: str) -> tuple[str, str]:
    """alice: a Mountain on the battlefield and a Lightning Bolt in hand."""
    add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Lightning Bolt", "alice", "hand")
    board = state(client, game_id)
    return board["hands"]["alice"][0]["id"], find(board["battlefield"], "Mountain")["id"]


def cast_bolt(seat: Seat, game_id: str, bolt_id: str, mountain_id: str) -> Any:
    return lambda: seat.call_json(
        "POST",
        f"/api/games/{game_id}/actions/cast",
        {
            "player": "alice",
            "card_id": bolt_id,
            "targets": ["bob"],
            "mana_sources": [mountain_id],
            "reasoning": "bolt the bird before it connects",
        },
    )


def advice_rows(game_id: str) -> list[dict[str, Any]]:
    return [e for e in audit.entries(game_id) if e["action"] == audit.ADVICE_ACTION]


def approved_rows(game_id: str) -> list[dict[str, Any]]:
    return [e for e in audit.entries(game_id) if e["action"] == audit.ADVICE_APPROVED_ACTION]


# ---------------------------------------------------------------------------
# 1. role resolution
# ---------------------------------------------------------------------------


def test_an_advisor_uid_builds_an_advise_seat(client: TestClient, game: str, wired: None) -> None:
    seat = buddy(game)

    assert seat.advise is True
    assert seat.player == "alice", "an advisor reads its human's view, under its human's name"


def test_a_seat_uid_builds_an_ordinary_seat(client: TestClient, game: str, wired: None) -> None:
    advisor_uid(game)  # the game has an advisor; the seat must not notice

    assert player_seat(game).advise is False


def test_the_pinned_branch_consults_the_uid_lookup_for_the_role(
    client: TestClient, game: str, wired: None, sent: list[tuple[str, str]]
) -> None:
    """`--game` pins stdio to one game and used to resolve a uid through
    `GET /api/games/{id}/seat`, which knows nothing about roles (and 404s an
    advisor uid outright). One lookup, the same one every other transport
    makes."""
    seat = buddy(game, pinned=True)

    assert seat.advise is True
    assert ("GET", f"/api/seats/{ADVISOR_UID}") in sent


def test_the_unpinned_http_branch_resolves_the_role_too(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game, pinned=False)

    assert seat.advise is True
    assert seat.game_id() == game


def test_the_role_lookup_is_cached(
    client: TestClient, game: str, wired: None, sent: list[tuple[str, str]]
) -> None:
    """One round trip per uid for the whole life of the process, not one per
    tool call - a buddy pod makes thousands."""
    advisor_uid(game)
    seats = seats_for(game)

    seats.seat_for(ADVISOR_UID)
    seats.seat_for(ADVISOR_UID)

    lookups = [url for method, url in sent if url == f"/api/seats/{ADVISOR_UID}"]
    assert len(lookups) == 1, f"expected one cached lookup, saw {len(lookups)}"


def test_a_uid_from_another_game_is_still_nobody_in_a_pinned_process(
    client: TestClient, game: str, wired: None
) -> None:
    """`--game` is isolation, not a hint: resolving through the global uid
    lookup must not let a uid minted for a different game work against the
    pinned one."""
    other = client.post(
        "/api/games", json={"players": [{"username": "carol"}, {"username": "dave"}]}
    ).json()
    try:
        foreign = next(iter(store.seat_uids(other["game_id"])))

        assert seats_for(game).seat_for(foreign) is None
    finally:
        client.delete(f"/api/games/{other['game_id']}", params={"key": other["admin_key"]})


# ---------------------------------------------------------------------------
# 2. enforcement at the one choke point
# ---------------------------------------------------------------------------


def test_an_advise_seats_cast_dry_runs_and_records_a_proposal_instead_of_casting(
    client: TestClient, game: str, wired: None, sent: list[tuple[str, str]]
) -> None:
    seat = buddy(game)
    bolt_id, mountain_id = stock_a_bolt(client, game)
    before = state(client, game)

    answer = cast_bolt(seat, game, bolt_id, mountain_id)()

    assert answer["proposed"] is True
    assert isinstance(answer["log_seq"], int)
    assert answer["outcome"]["allowed"] is True, answer
    assert "state" in answer
    rows = advice_rows(game)
    assert len(rows) == 1, rows
    assert rows[0]["detail"]["validated"] is True
    assert rows[0]["seq"] == answer["log_seq"]
    after = state(client, game)
    assert after["stack"] == [], "an advisor's proposal must never put a spell on the stack"
    assert [c["id"] for c in after["hands"]["alice"]] == [
        c["id"] for c in before["hands"]["alice"]
    ], "the bolt left alice's hand - the advisor moved the game"
    assert find(after["battlefield"], "Mountain")["tapped"] is False, "the mana was really spent"


def test_the_cast_probe_really_carried_dry_run(
    client: TestClient, game: str, wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 7 shapes that support it are validated by the engine itself, and
    that is only true if the probe actually says so."""
    seat = buddy(game)
    bolt_id, mountain_id = stock_a_bolt(client, game)
    bodies: list[dict[str, Any]] = []
    inner = seat.http.request

    def spy(method: str, url: str, **kwargs: Any) -> Any:
        if method.upper() == "POST" and "/actions/cast" in str(url):
            bodies.append(kwargs.get("json") or {})
        return inner(method, url, **kwargs)

    monkeypatch.setattr(seat.http, "request", spy)

    cast_bolt(seat, game, bolt_id, mountain_id)()

    assert bodies and all(body.get("dry_run") is True for body in bodies), bodies


def test_the_recorded_body_is_ready_to_post_with_no_dry_run(
    client: TestClient, game: str, wired: None
) -> None:
    """The row carries the exact request the human's browser will send: the
    human as `player`, and no `dry_run` left in it."""
    seat = buddy(game)
    bolt_id, mountain_id = stock_a_bolt(client, game)

    cast_bolt(seat, game, bolt_id, mountain_id)()

    body = advice_rows(game)[0]["detail"]["body"]
    assert body["player"] == "alice"
    assert "dry_run" not in body
    assert body["card_id"] == bolt_id
    assert advice_rows(game)[0]["detail"]["method"] == "POST"
    assert advice_rows(game)[0]["detail"]["path"].endswith("/actions/cast")


def test_a_dry_run_the_engine_refuses_proposes_nothing(
    client: TestClient, game: str, wired: None
) -> None:
    """A proposal is only recorded once the engine has agreed the action is
    legal: a refused probe is handed straight back, verbatim, so the buddy
    can try something else."""
    seat = buddy(game)
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt_id = state(client, game)["hands"]["alice"][0]["id"]

    answer = seat.call_json(
        "POST",
        f"/api/games/{game}/actions/cast",
        {"player": "alice", "card_id": bolt_id, "targets": ["bob"], "reasoning": "no mana at all"},
    )

    assert answer.get("proposed") is not True, answer
    assert answer["allowed"] is False
    assert advice_rows(game) == []


def test_an_advise_seats_decide_sends_no_mutating_request_and_is_unvalidated(
    client: TestClient, game: str, wired: None, sent: list[tuple[str, str]]
) -> None:
    """`DecideRequest` has no `dry_run`, and wave 1's gate would wave through
    a body that merely *claimed* one - so nothing is sent at all."""
    seat = buddy(game)

    answer = seat.call_json(
        "POST",
        f"/api/games/{game}/decide",
        {"player": "alice", "accept": True, "reasoning": "keep this hand"},
    )

    assert answer["proposed"] is True
    assert answer["outcome"]["validated"] is False
    assert answer["outcome"]["allowed"] is None
    rows = advice_rows(game)
    assert len(rows) == 1
    assert rows[0]["detail"]["validated"] is False
    assert not [url for method, url in sent if method == "POST" and "/decide" in url], (
        "an advise seat put a decide POST on the wire - the one thing it must never do"
    )


@pytest.mark.parametrize(
    "suffix,body",
    [
        ("decide", {"player": "alice", "accept": True}),
        ("actions/declare-blockers", {"player": "alice", "blocks": []}),
        ("actions/pass-priority", {"player": "alice"}),
        ("actions/concede", {"player": "alice"}),
        ("advance", {"player": "alice"}),
    ],
)
def test_every_shape_without_dry_run_sends_nothing_and_proposes(
    client: TestClient,
    game: str,
    wired: None,
    sent: list[tuple[str, str]],
    suffix: str,
    body: dict[str, Any],
) -> None:
    seat = buddy(game)

    answer = seat.call_json(
        "POST", f"/api/games/{game}/{suffix}", {**body, "reasoning": "the whole plan, in one line"}
    )

    assert answer["proposed"] is True, answer
    assert answer["outcome"]["validated"] is False
    assert advice_rows(game)[0]["detail"]["validated"] is False
    assert mutating_posts(sent, game) == [], mutating_posts(sent, game)


def test_an_explicit_dry_run_from_an_advisor_is_still_just_a_dry_run(
    client: TestClient, game: str, wired: None
) -> None:
    """An advisor asking a question already had a way to ask it. A body that
    says `dry_run: true` is that question, not a committing call, so it is
    sent as-is and proposes nothing."""
    seat = buddy(game)
    land = card_id(client, game, "Mountain", "alice", "hand")

    answer = seat.call_json(
        "POST",
        f"/api/games/{game}/actions/play-land",
        {"player": "alice", "card_id": land, "dry_run": True},
    )

    assert answer.get("proposed") is not True
    assert answer["allowed"] is True
    assert advice_rows(game) == []


def test_run_action_does_not_auto_pass_for_an_advise_seat(
    client: TestClient, game: str, wired: None, sent: list[tuple[str, str]]
) -> None:
    """`run_action`'s whole job for a seat is release-and-wait. An advisor
    releasing priority would be an advisor driving the game."""
    seat = buddy(game)
    bolt_id, mountain_id = stock_a_bolt(client, game)
    before = state(client, game)

    result = run_action(seat, cast_bolt(seat, game, bolt_id, mountain_id))

    assert result["outcome"]["proposed"] is True
    assert result["held_priority"] is None
    assert "state" in result and "available_actions" in result and "recent_log" in result
    assert not [url for method, url in sent if method == "POST" and "pass-priority" in url], (
        "an advise seat auto-passed priority"
    )
    after = state(client, game)
    assert after["priority_holder"] == before["priority_holder"]
    assert after["step"] == before["step"]


def test_run_action_still_auto_passes_for_an_ordinary_seat(
    client: TestClient, game: str, wired: None, sent: list[tuple[str, str]]
) -> None:
    advisor_uid(game)
    seat = player_seat(game)
    bolt_id, mountain_id = stock_a_bolt(client, game)

    result = run_action(seat, cast_bolt(seat, game, bolt_id, mountain_id))

    assert result["outcome"]["allowed"] is True
    assert result["held_priority"] is False
    assert [url for method, url in sent if method == "POST" and "pass-priority" in url]


def test_pass_until_is_refused_in_advise_mode(
    client: TestClient, game: str, wired: None, sent: list[tuple[str, str]]
) -> None:
    seat = buddy(game)

    result = pass_until(seat, until_step="end_step", whose_turn="mine")

    assert result["finished"] is True
    assert result["passes"] == 0
    assert result["stopped_because"] == seatkit.ADVISE_NO_PASS_UNTIL
    assert "advisors propose one action at a time; use pass" in result["stopped_because"]
    assert mutating_posts(sent, game) == []


def test_pass_until_still_runs_for_an_ordinary_seat(
    client: TestClient, game: str, wired: None
) -> None:
    advisor_uid(game)
    seat = player_seat(game)

    result = pass_until(seat, until_step="end_step", whose_turn="mine")

    assert result["stopped_because"] != seatkit.ADVISE_NO_PASS_UNTIL
    assert result["passes"] >= 1, result


def test_declare_plan_returns_the_servers_own_refusal_for_an_advise_seat(
    client: TestClient, game: str, wired: None
) -> None:
    """A plan is seat-only per wave 1. Advise mode does not paper over that
    with a proposal: the server's own refusal is what comes back."""
    seat = buddy(game)

    answer = seat.declare_plan(
        board_read="b", my_role="r", plan="p", expecting="a fetchland", because="two-lander"
    )

    assert answer.get("proposed") is not True
    assert answer["allowed"] is False
    assert f"no game {game}" in answer["reason"]
    assert advice_rows(game) == []


def test_a_report_still_reaches_the_log_from_an_advise_seat(
    client: TestClient, game: str, wired: None
) -> None:
    """`/report` is the other channel wave 1 opened, and advise mode must not
    swallow it into a proposal."""
    seat = buddy(game)

    text = seat.call(
        "POST",
        f"/api/games/{game}/report",
        {"player": "alice", "text": "the engine let me block with a tapped creature"},
    )

    assert '"recorded": true' in text.lower()
    assert advice_rows(game) == []
    assert [e for e in audit.entries(game) if e["action"] == audit.PROBLEM_ACTION]


# ---------------------------------------------------------------------------
# the frozen shape set
# ---------------------------------------------------------------------------


def _openapi() -> dict[str, Any]:
    return json.loads((REPO_ROOT / "openapi.json").read_text())


def test_the_frozen_action_shapes_are_exactly_twelve_seven_of_which_dry_run() -> None:
    assert len(set(seatkit.ACTION_REQUEST_SHAPES.values())) == 12
    assert len(seatkit.DRY_RUN_SHAPES) == 7
    assert set(seatkit.ACTION_REQUEST_SHAPES.values()) >= seatkit.DRY_RUN_SHAPES


def test_every_frozen_path_declares_the_shape_the_map_names() -> None:
    schema = _openapi()
    for suffix, shape in seatkit.ACTION_REQUEST_SHAPES.items():
        path = f"/api/games/{{game_id}}/{suffix}"
        operation = schema["paths"].get(path, {}).get("post")
        assert operation is not None, f"{path} is not a POST route any more"
        ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert ref == f"#/components/schemas/{shape}", f"{path} no longer takes {shape}"


def test_the_frozen_dry_run_set_matches_openapi_exactly() -> None:
    """The drift pin: a shape that gains or loses `dry_run` must fail here
    rather than silently change which of an advisor's calls get validated -
    or, worse, which of them stop being validated and start being sent."""
    components = _openapi()["components"]["schemas"]
    carries = {
        name for name, schema in components.items() if "dry_run" in (schema.get("properties") or {})
    }

    assert carries == set(seatkit.DRY_RUN_SHAPES), (
        "seatkit.DRY_RUN_SHAPES has drifted from openapi.json - regenerate with "
        "`mise run openapi:dump` and update the frozen set deliberately"
    )


# ---------------------------------------------------------------------------
# 3. the /advice route
# ---------------------------------------------------------------------------


def advice_body(**over: Any) -> dict[str, Any]:
    body = {
        "player": "alice",
        "method": "POST",
        "path": "/api/games/x/actions/play-land",
        "body": {"player": "alice", "card_id": "00000000-0000-0000-0000-000000000000"},
        "reasoning": "land first, always",
        "validated": False,
    }
    body.update(over)
    return body


def test_advice_refuses_a_blank_reasoning_with_a_422_naming_it(
    client: TestClient, game: str
) -> None:
    uid = advisor_uid(game)

    response = client.post(
        f"/api/games/{game}/advice", params={"uid": uid}, json=advice_body(reasoning="   ")
    )

    assert response.status_code == 422, response.text
    assert "reasoning" in response.text


def test_advice_records_exactly_one_row_and_moves_no_game(client: TestClient, game: str) -> None:
    """`store.replace` writes its own `records` row for every commit, so a
    ledger that grew by exactly one proves nothing was committed."""
    uid = advisor_uid(game)
    land = card_id(client, game, "Mountain", "alice", "hand")
    before_seq = audit.last_seq(game)
    before = state(client, game)

    response = client.post(
        f"/api/games/{game}/advice",
        params={"uid": uid},
        json=advice_body(
            validated=True, body={"player": "alice", "card_id": land}, path="/actions/play-land"
        ),
    )

    assert response.status_code == 200, response.text
    assert audit.last_seq(game) == before_seq + 1
    assert state(client, game)["battlefield"] == before["battlefield"]


def test_based_on_seq_is_the_ledger_head_at_record_time(client: TestClient, game: str) -> None:
    uid = advisor_uid(game)
    add_card(client, game, "Mountain", "alice", "battlefield")  # moves the ledger
    head = audit.last_seq(game)

    response = client.post(f"/api/games/{game}/advice", params={"uid": uid}, json=advice_body())

    assert response.status_code == 200, response.text
    row = advice_rows(game)[0]
    assert row["detail"]["based_on_seq"] == head
    assert row["seq"] == head + 1
    assert response.json()["based_on_seq"] == head


def test_the_summary_is_derived_by_the_server_from_the_body(client: TestClient, game: str) -> None:
    """Server-derived, never taken from the agent, so it cannot disagree with
    the body it describes - card names resolved from the ids in that body."""
    uid = advisor_uid(game)
    bolt = card_id(client, game, "Lightning Bolt", "alice", "hand")

    response = client.post(
        f"/api/games/{game}/advice",
        params={"uid": uid},
        json=advice_body(
            path=f"/api/games/{game}/actions/cast",
            body={"player": "alice", "card_id": bolt, "targets": ["bob"]},
            summary="a summary the agent made up",
        ),
    )

    assert response.status_code == 200, response.text
    summary = advice_rows(game)[0]["detail"]["summary"]
    assert "Lightning Bolt" in summary, summary
    assert "bob" in summary, summary
    assert "made up" not in summary, "the agent's own summary reached the ledger"


def summary_of(client: TestClient, game_id: str, uid: str, tail: str, body: dict[str, Any]) -> str:
    """Record one proposal of `body` against `tail` and hand back the line
    the server derived for it."""
    record_advice(
        client, game_id, uid, path=f"/api/games/{game_id}/{tail}", body=body, validated=True
    )
    return advice_rows(game_id)[-1]["detail"]["summary"]


def test_the_summary_says_what_the_proposed_action_costs(client: TestClient, game: str) -> None:
    """The `summary` is specified as "card names resolved from ids, targets,
    cost" - and the cost half is the half a human approving a proposal cannot
    do without. The number of mana sources is not a cost, so the sources are
    named too."""
    uid = advisor_uid(game, "alice")
    mountain = card_id(client, game, "Mountain", "alice", "battlefield")
    bolt = card_id(client, game, "Lightning Bolt", "alice", "hand")

    summary = summary_of(
        client,
        game,
        uid,
        "actions/cast",
        {
            "player": "alice",
            "card_id": bolt,
            "targets": ["bob"],
            "mana_sources": [mountain],
        },
    )

    assert "Lightning Bolt" in summary, summary
    assert "for {R}" in summary, summary
    assert "paying with Mountain" in summary, summary
    assert "source(s)" not in summary, f"the sources are named, not counted: {summary}"


def test_the_cost_is_the_engines_own_total_not_the_cards_printed_mana_cost(
    client: TestClient, game: str
) -> None:
    """Mind Stone prints `{2}`; its second ability costs `{1}, {T}, Sacrifice
    this artifact`. Reading the printed mana cost off the card would say
    `{2}` for an activation that never pays it - so the cost is read off the
    engine's own enumeration of what is legal right now, which is where cost
    increases, reductions and alternative costs are already worked out."""
    uid = advisor_uid(game, "alice")
    stone = card_id(client, game, "Mind Stone", "alice", "battlefield")

    summary = summary_of(
        client,
        game,
        uid,
        "actions/activate",
        {"player": "alice", "card_id": stone, "ability_index": 1},
    )

    assert "for {1}, {T}, Sacrifice this artifact" in summary, summary
    assert "for {2}" not in summary, f"that is the printing, not the ability: {summary}"


def test_the_cost_tells_two_abilities_of_one_permanent_apart(client: TestClient, game: str) -> None:
    """Mind Stone's ability 0 is a bare `{T}` mana ability. Same card, same
    path, different `ability_index` - and therefore a different cost."""
    uid = advisor_uid(game, "alice")
    stone = card_id(client, game, "Mind Stone", "alice", "battlefield")

    summary = summary_of(
        client,
        game,
        uid,
        "actions/activate",
        {"player": "alice", "card_id": stone, "ability_index": 0},
    )

    assert "for {T}" in summary, summary
    assert "Sacrifice" not in summary, f"that is the other ability: {summary}"


def test_an_unmatched_proposal_still_says_the_printed_cost_and_says_it_is_printed(
    client: TestClient, game: str
) -> None:
    """A proposal may name an action that is not legal *yet* - the advisor
    reads a board, and a command can land between reading it and recording.
    The human still has to see a price, so the card's own printed mana cost
    stands in, labelled as printed rather than passed off as a total the
    engine agreed to."""
    uid = advisor_uid(game, "alice")
    pacifism = card_id(client, game, "Pacifism", "alice", "graveyard")

    summary = summary_of(
        client,
        game,
        uid,
        "actions/cast",
        {"player": "alice", "card_id": pacifism, "from_zone": "graveyard"},
    )

    assert "for {1}{W} (printed)" in summary, summary


def test_a_costless_proposal_invents_no_cost(client: TestClient, game: str) -> None:
    """CR 305.1's land play pays nothing at all ("the player simply puts the
    land onto the battlefield", so `Action.cost` is `None`), and a land prints
    no mana cost - so the line says nothing about cost rather than `{0}` or a
    printed blank."""
    uid = advisor_uid(game, "alice")
    forest = card_id(client, game, "Forest", "alice", "hand")

    summary = summary_of(
        client, game, uid, "actions/play-land", {"player": "alice", "card_id": forest}
    )

    assert summary == "alice: play land Forest", summary


def test_the_path_picks_the_cost_of_the_action_it_names_not_another_on_the_same_card(
    client: TestClient, game: str
) -> None:
    """Lupine Harbingers in hand is two legal things at once: a `{3}{G}` cast
    and CR 702.143a's foretell special action, which "may pay {2} and exile a
    card with foretell from their hand face down". Same card, same player,
    two costs - so the route the proposal is addressed to is what decides
    which one the line quotes."""
    uid = advisor_uid(game, "alice")
    harbingers = card_id(client, game, "Lupine Harbingers", "alice", "hand")
    body = {"player": "alice", "card_id": harbingers}

    foretold = summary_of(client, game, uid, "actions/foretell", body)
    cast = summary_of(client, game, uid, "actions/cast", body)

    assert "for {2}" in foretold, foretold
    assert "for {3}{G}" in cast, cast


def test_a_crew_proposal_quotes_no_printed_cost_because_the_creatures_are_the_cost(
    client: TestClient, game: str
) -> None:
    """Smuggler's Copter prints `{2}`, and crewing it never pays that: CR
    702.122a's crew cost is the creatures tapped for it, which the line
    already names. A printing quoted here would be a number the proposal does
    not pay, which is the whole failure the cost half exists to avoid - so no
    cost clause at all."""
    uid = advisor_uid(game, "alice")
    copter = card_id(client, game, "Smuggler's Copter", "alice", "battlefield")
    lions = card_id(client, game, "Savannah Lions", "alice", "battlefield")

    summary = summary_of(
        client,
        game,
        uid,
        "actions/crew",
        {"player": "alice", "card_id": copter, "crewing": [lions]},
    )

    assert summary == "alice: crew Smuggler's Copter, crewing with Savannah Lions", summary


def test_a_cycling_proposal_reads_the_cycling_cost(client: TestClient, game: str) -> None:
    """`actions/cycle` has a route of its own, while the engine enumerates
    cycling as an off-zone activation (CR 702.29b: it "can be activated only
    if the card is in a player's hand") - so the path-to-kind mapping is what
    finds it, and the cost is the cycling ability's, not Angelsong's printed
    `{1}{W}`."""
    uid = advisor_uid(game, "alice")
    angelsong = card_id(client, game, "Angelsong", "alice", "hand")

    summary = summary_of(
        client, game, uid, "actions/cycle", {"player": "alice", "card_id": angelsong}
    )

    assert "for {2}, Discard this card" in summary, summary
    assert "{1}{W}" not in summary, f"that is the printing, not the cycling cost: {summary}"


def test_a_decide_answer_is_summarised_without_a_cost(client: TestClient, game: str) -> None:
    """`decide` names no card and pays nothing; the cost lookup must not
    attach one (nor raise looking for it)."""
    uid = advisor_uid(game, "alice")

    summary = summary_of(client, game, uid, "decide", {"player": "alice", "accept": True})

    assert summary == "alice: decide yes", summary


def test_advice_carries_the_legal_actions_the_retrospective_will_want(
    client: TestClient, game: str
) -> None:
    uid = advisor_uid(game)
    add_card(client, game, "Mountain", "alice", "hand")

    client.post(f"/api/games/{game}/advice", params={"uid": uid}, json=advice_body())

    row = advice_rows(game)[0]
    assert row["legal_actions_for"] == "alice"
    assert isinstance(row["legal_actions_before"], list)


def test_advice_is_refused_for_a_uid_that_does_not_read_that_seat(
    client: TestClient, game: str
) -> None:
    advisor_uid(game, "alice")

    response = client.post(
        f"/api/games/{game}/advice",
        params={"uid": seat_uid(game, "bob")},
        json=advice_body(player="alice"),
    )

    assert response.status_code == 404, response.text
    assert advice_rows(game) == []


def test_advice_is_loud_on_the_console(client: TestClient, game: str, caplog) -> None:
    """The buddy's line is the most interesting thing in the log while a game
    is being advised; it gets `LOUD_ACTIONS`' banner, not one compact line."""
    uid = advisor_uid(game)
    assert audit.ADVICE_ACTION in audit.LOUD_ACTIONS

    with caplog.at_level("WARNING", logger="mcm.audit"):
        client.post(f"/api/games/{game}/advice", params={"uid": uid}, json=advice_body())

    assert "land first, always" in caplog.text


# ---------------------------------------------------------------------------
# 4. the approve record
# ---------------------------------------------------------------------------


def record_advice(client: TestClient, game: str, uid: str, **over: Any) -> int:
    response = client.post(
        f"/api/games/{game}/advice", params={"uid": uid}, json=advice_body(**over)
    )
    assert response.status_code == 200, response.text
    return response.json()["log_seq"]


def test_approve_is_refused_for_an_advisor_uid(client: TestClient, game: str) -> None:
    """Wave 1's gate refuses it (the path is neither `/advice` nor `/report`
    and the body claims no `dry_run`), which is correct and must stay so: the
    human approves, never the buddy."""
    uid = advisor_uid(game)
    seq = record_advice(client, game, uid)

    response = client.post(
        f"/api/games/{game}/advice/{seq}/approve", params={"uid": uid}, json={"player": "alice"}
    )

    assert response.status_code == 404, response.text
    assert approved_rows(game) == []


def test_approve_is_accepted_for_the_human(client: TestClient, game: str) -> None:
    uid = advisor_uid(game)
    seq = record_advice(client, game, uid)
    before_seq = audit.last_seq(game)
    before = state(client, game)

    response = client.post(
        f"/api/games/{game}/advice/{seq}/approve",
        params={"uid": seat_uid(game, "alice")},
        json={"player": "alice"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["advice_seq"] == seq
    rows = approved_rows(game)
    assert len(rows) == 1
    assert rows[0]["detail"]["advice_seq"] == seq
    assert audit.last_seq(game) == before_seq + 1, "approving committed something"
    assert state(client, game)["battlefield"] == before["battlefield"]


def test_approve_is_refused_for_the_other_seat(client: TestClient, game: str) -> None:
    uid = advisor_uid(game, "alice")
    seq = record_advice(client, game, uid)

    response = client.post(
        f"/api/games/{game}/advice/{seq}/approve",
        params={"uid": seat_uid(game, "bob")},
        json={"player": "bob"},
    )

    assert response.status_code == 404, response.text
    assert approved_rows(game) == []


def test_approve_is_refused_for_a_seq_that_is_not_an_advice_row(
    client: TestClient, game: str
) -> None:
    uid = advisor_uid(game)
    record_advice(client, game, uid)

    response = client.post(
        f"/api/games/{game}/advice/1/approve",
        params={"uid": seat_uid(game, "alice")},
        json={"player": "alice"},
    )

    assert response.status_code == 404, response.text


def test_the_approval_is_recorded_before_the_action_it_approves(
    client: TestClient, game: str
) -> None:
    """The ordering contract the browser half (a later task) must honour: the
    approval is recorded first, so the retrospective can pair it with the
    action that follows. Pinned here by the only thing the server can see -
    the ledger order - and by the fact that a refused approval leaves no row
    at all, so an action taken anyway has nothing to pair with."""
    uid = advisor_uid(game)
    land = card_id(client, game, "Mountain", "alice", "hand")
    seq = record_advice(client, game, uid, validated=True, body={"player": "alice"})

    approved = client.post(
        f"/api/games/{game}/advice/{seq}/approve",
        params={"uid": seat_uid(game, "alice")},
        json={"player": "alice"},
    )
    assert approved.status_code == 200, approved.text
    played = client.post(
        f"/api/games/{game}/actions/play-land",
        params={"uid": seat_uid(game, "alice")},
        json={"player": "alice", "card_id": land},
    )
    assert played.status_code == 200, played.text

    actions = [e["action"] for e in audit.entries(game)]
    assert actions.index(audit.ADVICE_ACTION) < actions.index(audit.ADVICE_APPROVED_ACTION)
    assert actions.index(audit.ADVICE_APPROVED_ACTION) < actions.index("play_land")


# ---------------------------------------------------------------------------
# 5. redaction
# ---------------------------------------------------------------------------


def test_an_opponent_polling_the_log_cannot_read_the_advice(client: TestClient, game: str) -> None:
    uid = advisor_uid(game, "alice")
    bolt = card_id(client, game, "Lightning Bolt", "alice", "hand")
    record_advice(
        client,
        game,
        uid,
        body={"player": "alice", "card_id": bolt, "targets": ["bob"]},
        reasoning="bolt the bird on their end step",
    )

    log = client.get(f"/api/games/{game}/log", params={"uid": seat_uid(game, "bob")}).json()["log"]
    row = next(e for e in log if e["action"] == audit.ADVICE_ACTION)

    assert row["detail"] == {"player": "alice"}, row["detail"]
    assert row["reasoning"] is None
    assert "Lightning Bolt" not in json.dumps(row)
    assert "bolt the bird" not in json.dumps(row)


def test_the_advised_human_reads_the_whole_advice(client: TestClient, game: str) -> None:
    uid = advisor_uid(game, "alice")
    bolt = card_id(client, game, "Lightning Bolt", "alice", "hand")
    record_advice(
        client,
        game,
        uid,
        body={"player": "alice", "card_id": bolt, "targets": ["bob"]},
        reasoning="bolt the bird on their end step",
    )

    log = client.get(f"/api/games/{game}/log", params={"uid": seat_uid(game, "alice")}).json()[
        "log"
    ]
    row = next(e for e in log if e["action"] == audit.ADVICE_ACTION)

    assert "Lightning Bolt" in row["detail"]["summary"]
    assert row["reasoning"] == "bolt the bird on their end step"


def test_the_advisor_reads_back_its_own_advice(client: TestClient, game: str) -> None:
    uid = advisor_uid(game, "alice")
    record_advice(client, game, uid)

    log = client.get(f"/api/games/{game}/log", params={"uid": uid}).json()["log"]
    row = next(e for e in log if e["action"] == audit.ADVICE_ACTION)

    assert row["reasoning"] == "land first, always"


# ---------------------------------------------------------------------------
# 6. protocol version
# ---------------------------------------------------------------------------


def test_the_protocol_version_is_unchanged_and_documents_advise_mode() -> None:
    """Wave 1 and wave 2 ship as one protocol version: the changelog entry is
    extended, never bumped to 15."""
    assert protocol.PROTOCOL_VERSION == seatkit.PROTOCOL_VERSION == "seatkit-14"
    changelog = Path(seatkit.__file__).read_text().split("PROTOCOL_VERSION = ", 1)[1]
    assert "advise" in changelog.lower()
    assert "/advice" in changelog


# ---------------------------------------------------------------------------
# 7. a seat is completely unaffected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suffix,body",
    [
        ("actions/cast", {"player": "alice", "card": "Lightning Bolt"}),
        ("decide", {"player": "alice", "accept": True}),
        ("actions/pass-priority", {"player": "alice"}),
        ("actions/declare-blockers", {"player": "alice", "blocks": []}),
        ("advance", {"player": "alice"}),
    ],
)
def test_a_seat_never_proposes_anything(
    client: TestClient, game: str, wired: None, suffix: str, body: dict[str, Any]
) -> None:
    """Whatever the answer is - allowed, refused, paused - it is never a
    proposal, and no advice row is ever written for a seat."""
    advisor_uid(game)
    seat = player_seat(game)

    answer = seat.call_json("POST", f"/api/games/{game}/{suffix}", {**body, "reasoning": "because"})

    assert isinstance(answer, dict)
    assert answer.get("proposed") is not True, answer
    assert advice_rows(game) == []


def test_a_seat_playing_a_land_still_really_plays_it(
    client: TestClient, game: str, wired: None
) -> None:
    advisor_uid(game)
    seat = player_seat(game)
    land = card_id(client, game, "Mountain", "alice", "hand")

    answer = seat.call_json(
        "POST", f"/api/games/{game}/actions/play-land", {"player": "alice", "card_id": land}
    )

    assert answer["allowed"] is True
    assert [c["name"] for c in state(client, game)["battlefield"]] == ["Mountain"]


# ---------------------------------------------------------------------------
# the same 19 tools, through the real MCP layer
# ---------------------------------------------------------------------------
#
# Advise mode is a property of the uid, not a toolset: a buddy pod is the seat
# Job with a different uid and gets the identical `build_server` surface. That
# is only true if it actually holds up end to end, through `mcp.py`'s own
# argument parsing and answer shaping rather than only through `seatkit`, so
# the tests below drive the real tools.


def tool_text(server: Any, name: str, arguments: dict[str, Any]) -> str:
    result = asyncio.run(server.call_tool(name, arguments))
    return result.content[0].text


def mcp_server(game_id: str) -> Any:
    return build_server(seats_for(game_id))


def test_the_buddy_gets_the_same_nineteen_tools(client: TestClient, game: str) -> None:
    """One server, one toolset: there is no advisor-only tool and no tool an
    advisor is denied - `contract/tools.json` is the committed list, and this
    pins that advise mode did not fork it."""
    names = {tool.name for tool in asyncio.run(mcp_server(game).list_tools())}
    committed = {
        tool["name"]
        for tool in json.loads((REPO_ROOT / "contract/tools.json").read_text())["tools"]
    }

    assert names == committed
    assert len(names) == 19


def test_the_cast_tool_proposes_for_an_advisor_and_casts_for_a_seat(
    client: TestClient, game: str, wired: None
) -> None:
    advisor_uid(game)
    stock_a_bolt(client, game)
    server = mcp_server(game)

    advised = tool_text(
        server,
        "cast",
        {
            "player_uid": ADVISOR_UID,
            "card": "Lightning Bolt",
            "targets": "bob",
            "reasoning": "three to the face closes it",
        },
    )

    assert '"proposed": true' in advised.lower(), advised
    assert state(client, game)["stack"] == []
    assert len(advice_rows(game)) == 1

    played = tool_text(
        server,
        "cast",
        {
            "player_uid": seat_uid(game, "alice"),
            "card": "Lightning Bolt",
            "targets": "bob",
            "reasoning": "three to the face closes it",
        },
    )

    assert '"proposed"' not in played.lower(), played
    assert len(advice_rows(game)) == 1, "a seat's own cast wrote an advice row"


def test_the_pass_tool_only_proposes_for_an_advisor(
    client: TestClient, game: str, wired: None, sent: list[tuple[str, str]]
) -> None:
    advisor_uid(game)
    server = mcp_server(game)

    answer = tool_text(
        server, "pass", {"player_uid": ADVISOR_UID, "reasoning": "nothing worth doing here"}
    )

    assert '"proposed": true' in answer.lower(), answer
    assert not [url for method, url in sent if method == "POST" and "pass-priority" in url]


def test_the_decide_tool_only_proposes_for_an_advisor(
    client: TestClient, game: str, wired: None, sent: list[tuple[str, str]]
) -> None:
    advisor_uid(game)
    server = mcp_server(game)

    answer = tool_text(
        server,
        "decide",
        {"player_uid": ADVISOR_UID, "accept": True, "reasoning": "the trigger is worth it"},
    )

    assert '"proposed": true' in answer.lower(), answer
    assert '"validated": false' in answer.lower(), answer
    assert not [url for method, url in sent if method == "POST" and "/decide" in url]


def test_the_pass_until_tool_is_refused_for_an_advisor(
    client: TestClient, game: str, wired: None
) -> None:
    advisor_uid(game)
    server = mcp_server(game)

    answer = tool_text(
        server, "pass_until", {"player_uid": ADVISOR_UID, "step": "end_step", "reasoning": "wait"}
    )

    assert "advisors propose one action at a time; use pass" in answer


def test_the_reading_tools_answer_an_advisor_exactly_as_they_answer_its_seat(
    client: TestClient, game: str, wired: None
) -> None:
    """The whole premise of the credential: an advisor sees what its human
    sees, byte for byte, because the server resolves it to that username."""
    advisor_uid(game)
    stock_a_bolt(client, game)
    server = mcp_server(game)

    advised = tool_text(server, "battlefield_state", {"player_uid": ADVISOR_UID})
    seated = tool_text(server, "battlefield_state", {"player_uid": seat_uid(game, "alice")})

    assert advised == seated


def test_the_declare_plan_tool_reports_the_servers_refusal_for_an_advisor(
    client: TestClient, game: str, wired: None
) -> None:
    advisor_uid(game)
    server = mcp_server(game)

    answer = tool_text(
        server,
        "declare_plan",
        {
            "player_uid": ADVISOR_UID,
            "board_read": "b",
            "my_role": "r",
            "plan": "p",
            "expecting": "a fetchland",
            "because": "two-lander",
        },
    )

    assert '"proposed"' not in answer.lower(), answer
    assert f"no game {game}" in answer
    assert advice_rows(game) == []


def test_the_report_problem_tool_still_files_for_an_advisor(
    client: TestClient, game: str, wired: None
) -> None:
    advisor_uid(game)
    server = mcp_server(game)

    answer = tool_text(
        server,
        "report_problem",
        {"player_uid": ADVISOR_UID, "text": "a tapped creature was allowed to block"},
    )

    assert '"recorded": true' in answer.lower(), answer
    assert [e for e in audit.entries(game) if e["action"] == audit.PROBLEM_ACTION]
    assert advice_rows(game) == []
