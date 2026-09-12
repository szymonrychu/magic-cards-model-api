"""P4: a seat says which board it decided against, and a stale action is
refused as stale.

The question this exists to make answerable is "how many of an agent's
refusals were races, and how many were rules errors" - unanswerable while a
seat acting on a board that had already moved got `claude does not have
priority, you does` or `no pending decision for you`, refusal text
indistinguishable from a genuine misread. `expected_seq` names the ledger
position the seat decided against; when it does not match, the route refuses
before the engine runs and the ledger row carries `stale: true`.

The discriminator is the boolean, never the message. Every assertion below
that separates a stale refusal from a rules refusal reads
`detail["stale"]`, and one of them deliberately proves the two texts are of
no use for that.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api import seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats, run_action
from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def log_of(client: TestClient, game_id: str, admin_key: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/games/{game_id}/log", params={"uid": admin_key})
    assert response.status_code == 200, response.text
    return response.json()["log"]


def last_seq_of(client: TestClient, game_id: str) -> int:
    response = client.get(f"/api/games/{game_id}/log")
    assert response.status_code == 200, response.text
    return response.json()["last_seq"]


def stock_a_bolt(client: TestClient, game_id: str) -> tuple[str, str]:
    """alice: a Mountain on the battlefield and a Lightning Bolt in hand."""
    add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Lightning Bolt", "alice", "hand")
    board = state(client, game_id)
    return board["hands"]["alice"][0]["id"], find(board["battlefield"], "Mountain")["id"]


def cast_body(bolt_id: str, mountain_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "player": "alice",
        "card_id": bolt_id,
        "targets": ["bob"],
        "mana_sources": [mountain_id],
        **extra,
    }


# ---------------------------------------------------------------------------
# the three shapes of a mutating call: stale, current, absent
# ---------------------------------------------------------------------------


def test_a_stale_expected_seq_is_refused_and_the_engine_never_runs(client, game) -> None:
    bolt_id, mountain_id = stock_a_bolt(client, game)
    stale = last_seq_of(client, game)
    # Something else happens: the board this seat decided against has moved.
    add_card(client, game, "Forest", "bob", "battlefield")
    assert last_seq_of(client, game) > stale

    response = client.post(
        f"/api/games/{game}/actions/cast", json=cast_body(bolt_id, mountain_id, expected_seq=stale)
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["allowed"] is False
    assert body["stale"] is True
    assert body["moved_from"] == stale
    assert body["moved_to"] == last_seq_of(client, game) - 1  # this refusal's own row is newer

    # The engine never ran: the Bolt is still in hand, the Mountain untapped,
    # and bob is still on 20.
    board = state(client, game)
    assert [card["name"] for card in board["hands"]["alice"]] == ["Lightning Bolt"]
    assert find(board["battlefield"], "Mountain")["tapped"] is False
    assert not board["stack"]
    assert next(p for p in board["players"] if p["username"] == "bob")["life"] == 20


def test_a_current_expected_seq_behaves_exactly_as_today(client, game) -> None:
    bolt_id, mountain_id = stock_a_bolt(client, game)
    current = last_seq_of(client, game)

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json=cast_body(bolt_id, mountain_id, expected_seq=current),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is True
    assert "stale" not in body
    assert state(client, game)["stack"], "the cast went on the stack, same as ever"


def test_no_expected_seq_at_all_behaves_exactly_as_today(client, game) -> None:
    """The regression guard for every existing caller, the browser included:
    a body with no `expected_seq` key must be indistinguishable from one sent
    before this field existed."""
    bolt_id, mountain_id = stock_a_bolt(client, game)
    # Deliberately move the ledger on first: an absent `expected_seq` opts out
    # of the guard entirely, so even a board that has moved changes nothing.
    add_card(client, game, "Forest", "bob", "battlefield")

    response = client.post(f"/api/games/{game}/actions/cast", json=cast_body(bolt_id, mountain_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is True
    assert "stale" not in body
    assert state(client, game)["stack"]


# ---------------------------------------------------------------------------
# the ledger row, and the boolean that is the whole point of it
# ---------------------------------------------------------------------------


def test_the_stale_row_carries_the_boolean_and_where_the_board_moved(
    client, game, admin_key
) -> None:
    bolt_id, mountain_id = stock_a_bolt(client, game)
    stale = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")
    moved_to = last_seq_of(client, game)

    client.post(
        f"/api/games/{game}/actions/cast", json=cast_body(bolt_id, mountain_id, expected_seq=stale)
    )

    rows = [row for row in log_of(client, game, admin_key) if row["action"] == "cast"]
    assert len(rows) == 1, rows
    detail = rows[0]["detail"]
    assert detail["allowed"] is False
    assert detail["stale"] is True
    assert detail["moved_from"] == stale
    assert detail["moved_to"] == moved_to


def test_a_rules_refusal_and_a_stale_refusal_differ_by_the_boolean_alone(
    client, game, admin_key
) -> None:
    """A reader of the ledger tells the two apart with no heuristics: the
    boolean, and nothing about the message text."""
    bolt_id, mountain_id = stock_a_bolt(client, game)

    # A rules refusal: bob has no Bolt to cast, and it is not bob's priority.
    rules = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "bob", "card_id": bolt_id, "targets": ["alice"]},
    )
    assert rules.status_code == 200, rules.text
    assert rules.json()["allowed"] is False

    stale_seq = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")
    client.post(
        f"/api/games/{game}/actions/cast",
        json=cast_body(bolt_id, mountain_id, expected_seq=stale_seq),
    )

    refusals = [
        row
        for row in log_of(client, game, admin_key)
        if row["action"] == "cast" and row["detail"].get("allowed") is False
    ]
    assert len(refusals) == 2, refusals
    rules_row, stale_row = refusals
    assert rules_row["detail"].get("stale") is None
    assert stale_row["detail"].get("stale") is True
    # The texts are of no use for this: neither is machine-readable, which is
    # exactly why the boolean exists.
    assert rules_row["detail"]["reason"] != stale_row["detail"]["reason"]


def test_the_refusal_says_the_board_moved(client, game) -> None:
    bolt_id, mountain_id = stock_a_bolt(client, game)
    stale = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")

    body = client.post(
        f"/api/games/{game}/actions/cast", json=cast_body(bolt_id, mountain_id, expected_seq=stale)
    ).json()
    assert "moved" in body["reason"]
    assert body["reason"] == body["detail"]


# ---------------------------------------------------------------------------
# what moved, on the same answer
# ---------------------------------------------------------------------------


def test_the_refusal_carries_the_ledger_entries_since_the_given_seq(client, game) -> None:
    """No second read: the caller is handed what moved, on the refusal."""
    bolt_id, mountain_id = stock_a_bolt(client, game)
    stale = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")
    add_card(client, game, "Island", "bob", "battlefield")

    body = client.post(
        f"/api/games/{game}/actions/cast", json=cast_body(bolt_id, mountain_id, expected_seq=stale)
    ).json()
    since = body["since"]
    assert [row["seq"] for row in since] == [stale + 1, stale + 2]
    assert [row["action"] for row in since] == ["add_card", "add_card"]
    assert body["last_seq"] == stale + 2


def test_the_entries_since_are_the_same_read_the_log_route_already_serves(client, game) -> None:
    """`since` is `GET .../log?since=` for this same caller, not a second,
    differently-redacted read: a seat uid gets its seat's own view of those
    rows, and never the opponent's stated reasoning."""
    uids = seats_for(game)
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    board = state(client, game)
    bolt_id = board["hands"]["alice"][0]["id"]
    mountain_id = find(board["battlefield"], "Mountain")["id"]
    stale = last_seq_of(client, game)

    # bob acts, with words of his own attached.
    passed = client.post(
        f"/api/games/{game}/actions/pass-priority",
        json={"player": "bob", "reasoning": "bob's own private read of this board"},
    )
    assert passed.status_code == 200, passed.text

    body = client.post(
        f"/api/games/{game}/actions/cast",
        params={"uid": uids["alice"]},
        json=cast_body(bolt_id, mountain_id, expected_seq=stale),
    ).json()
    expected = client.get(
        f"/api/games/{game}/log", params={"uid": uids["alice"], "since": stale}
    ).json()
    # Bounded at `moved_to`: the fresh read below also carries the refusal's
    # own row, appended after the board had already moved.
    assert body["since"] == [row for row in expected["log"] if row["seq"] <= body["moved_to"]]
    assert body["since"], "there was something to show"
    assert all(row["reasoning"] is None for row in body["since"]), (
        "alice must not read bob's stated reasoning off a refusal any more "
        "than she reads it off the log route"
    )


# ---------------------------------------------------------------------------
# every mutating route, not just the interesting one
# ---------------------------------------------------------------------------

#: Every route that reaches `records.legal_actions_before` - the whole set of
#: mutating actions - with a body well-formed enough to parse. The stale
#: guard runs before anything else, so none of these bodies has to describe a
#: legal move: what is under test is that each route has the field and refuses
#: on it.
MUTATING_ROUTES: list[tuple[str, str, str, dict[str, Any]]] = [
    ("step", "PUT", "/api/games/{gid}/step", {"step": "upkeep", "player": "alice"}),
    ("cast", "POST", "/api/games/{gid}/actions/cast", {"player": "alice", "card": "Mountain"}),
    ("suspend", "POST", "/api/games/{gid}/actions/suspend", {"player": "alice", "card": "X"}),
    ("plot", "POST", "/api/games/{gid}/actions/plot", {"player": "alice", "card": "X"}),
    ("foretell", "POST", "/api/games/{gid}/actions/foretell", {"player": "alice", "card": "X"}),
    ("cycle", "POST", "/api/games/{gid}/actions/cycle", {"player": "alice", "card": "X"}),
    ("concede", "POST", "/api/games/{gid}/actions/concede", {"player": "alice"}),
    (
        "activate",
        "POST",
        "/api/games/{gid}/actions/activate",
        {"player": "alice", "card_id": "00000000-0000-0000-0000-000000000000"},
    ),
    (
        "crew",
        "POST",
        "/api/games/{gid}/actions/crew",
        {
            "player": "alice",
            "card_id": "00000000-0000-0000-0000-000000000000",
            "crewing": ["00000000-0000-0000-0000-000000000001"],
        },
    ),
    ("attack", "POST", "/api/games/{gid}/actions/attack", {"attackers": []}),
    (
        "declare_blockers",
        "POST",
        "/api/games/{gid}/actions/declare-blockers",
        {"player": "alice", "blocks": []},
    ),
    ("play_land", "POST", "/api/games/{gid}/actions/play-land", {"player": "alice", "card": "X"}),
    ("pass_priority", "POST", "/api/games/{gid}/actions/pass-priority", {"player": "alice"}),
    ("state_based_actions", "POST", "/api/games/{gid}/state-based-actions", {"player": "alice"}),
    ("resolve_top", "POST", "/api/games/{gid}/resolve-top", {"player": "alice"}),
    ("advance", "POST", "/api/games/{gid}/advance", {"player": "alice"}),
    ("decide", "POST", "/api/games/{gid}/decide", {"player": "alice"}),
    # Admin-key only, and a mutating command like any other: it reaches the
    # same `records.legal_actions_before` sites through `_run_command`. The
    # parametrised tests below send this game's admin key on every row, which
    # changes nothing for the seat routes (the board guard runs ahead of every
    # viewer check) and is what lets this one past its own gate.
    ("pregame_settle", "POST", "/api/games/{gid}/pregame/settle", {}),
]


@pytest.mark.parametrize(
    "action,method,path,body", MUTATING_ROUTES, ids=[row[0] for row in MUTATING_ROUTES]
)
def test_every_mutating_route_refuses_a_stale_expected_seq(
    client, game, admin_key, action: str, method: str, path: str, body: dict[str, Any]
) -> None:
    stale = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")

    response = client.request(
        method,
        path.format(gid=game),
        params={"uid": admin_key},
        json={**body, "expected_seq": stale},
    )
    assert response.status_code == 409, response.text
    answer = response.json()
    assert answer["stale"] is True
    assert answer["allowed"] is False
    assert answer["moved_from"] == stale

    rows = [row for row in log_of(client, game, admin_key) if row["action"] == action]
    assert len(rows) == 1, rows
    assert rows[0]["detail"]["stale"] is True


@pytest.mark.parametrize(
    "action,method,path,body", MUTATING_ROUTES, ids=[row[0] for row in MUTATING_ROUTES]
)
def test_every_mutating_route_takes_a_current_expected_seq_without_refusing_on_it(
    client, game, admin_key, action: str, method: str, path: str, body: dict[str, Any]
) -> None:
    """The other half of the same guard: a *current* seq is never the reason
    a route refuses. Most of these bodies describe an illegal move and are
    refused on their own merits - what must never appear is `stale`."""
    current = last_seq_of(client, game)
    response = client.request(
        method,
        path.format(gid=game),
        params={"uid": admin_key},
        json={**body, "expected_seq": current},
    )
    assert response.status_code != 409 or "stale" not in response.json(), response.text


# ---------------------------------------------------------------------------
# dry runs still log nothing
# ---------------------------------------------------------------------------


def test_a_stale_dry_run_is_refused_but_still_writes_no_ledger_row(client, game, admin_key) -> None:
    """Dry runs are deliberately unlogged (one traced game's log grew to 2410
    entries, 956 of them probe noise). Carrying `expected_seq` must not start
    logging them."""
    bolt_id, mountain_id = stock_a_bolt(client, game)
    stale = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")
    before = len(log_of(client, game, admin_key))

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json=cast_body(bolt_id, mountain_id, expected_seq=stale, dry_run=True),
    )
    assert response.status_code == 409, response.text
    assert response.json()["stale"] is True
    assert len(log_of(client, game, admin_key)) == before, "a dry run logged a row"


# ---------------------------------------------------------------------------
# the seat's own side of it
# ---------------------------------------------------------------------------


def wired_seats(monkeypatch, game_id: str):
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id), seats_for(game_id)


def wire_spy(monkeypatch, seat: Any) -> list[tuple[str, Any]]:
    """Every request body this seat actually puts on the wire, in order.

    Read off `seat.http.request` rather than off `call_json`'s argument: the
    watermark is folded in inside `call_json` (`Seat._with_expected_seq`), so
    the body a caller passed is not the body that was sent - and it is the
    sent one this file is about."""
    sent: list[tuple[str, Any]] = []
    original = seat.http.request

    def spy(method: str, path: str, **kwargs: Any) -> Any:
        sent.append((path, kwargs.get("json")))
        return original(method, path, **kwargs)

    monkeypatch.setattr(seat.http, "request", spy)
    return sent


def test_run_action_sends_the_last_seq_the_seat_was_handed(client, game, monkeypatch) -> None:
    """No MCP tool signature grows a parameter: the `Seat` remembers its own
    watermark and `run_action` puts it on the wire."""
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    bolt_id, mountain_id = stock_a_bolt(client, game)

    sent = wire_spy(monkeypatch, alice)

    # The seat has been handed nothing yet, so the first action carries no
    # watermark - exactly as every caller behaved before this existed.
    run_action(
        alice,
        lambda: alice.call_json(
            "POST", f"/api/games/{game}/actions/cast", cast_body(bolt_id, mountain_id)
        ),
    )
    first_cast = next(body for path, body in sent if path.endswith("/actions/cast"))
    assert "expected_seq" not in first_cast

    # `run_action` handed the seat a `last_seq` on the way out (its answer's
    # own `recent_log`), so the next action names it.
    assert alice.last_seq is not None
    watermark = alice.last_seq
    sent.clear()
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    board = state(client, game)
    second_bolt = board["hands"]["alice"][0]["id"]
    second_mountain = find(board["battlefield"], "Mountain")["id"]
    run_action(
        alice,
        lambda: alice.call_json(
            "POST",
            f"/api/games/{game}/actions/cast",
            cast_body(second_bolt, second_mountain),
        ),
    )
    second_cast = next(body for path, body in sent if path.endswith("/actions/cast"))
    assert second_cast["expected_seq"] == watermark


def test_run_action_does_not_put_its_watermark_on_the_auto_pass(client, game, monkeypatch) -> None:
    """The action itself moves the ledger, so the release that follows it is
    stale by construction. Arming is one-shot for exactly that reason."""
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    alice.last_seq = last_seq_of(client, game)
    bolt_id, mountain_id = stock_a_bolt(client, game)
    alice.last_seq = last_seq_of(client, game)

    sent = wire_spy(monkeypatch, alice)
    run_action(
        alice,
        lambda: alice.call_json(
            "POST", f"/api/games/{game}/actions/cast", cast_body(bolt_id, mountain_id)
        ),
    )
    passes = [body for path, body in sent if path.endswith("/actions/pass-priority")]
    assert passes, "the auto-pass did run"
    assert all("expected_seq" not in body for body in passes)


def test_a_seat_refused_as_stale_re_reads_and_retries_once(client, game, monkeypatch) -> None:
    """The house rule from the browser fix (MEMORY.md 2026-09-08, "a refused
    pass is two different things"): ask the board, never pattern-match the
    refusal text. A stale refusal is a lost race, so the seat re-reads and
    sends the same action once more against the board that is actually
    there."""
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    bolt_id, mountain_id = stock_a_bolt(client, game)
    # A watermark that was true a moment ago and is not any more.
    alice.last_seq = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")

    sent = wire_spy(monkeypatch, alice)
    result = run_action(
        alice,
        lambda: alice.call_json(
            "POST", f"/api/games/{game}/actions/cast", cast_body(bolt_id, mountain_id)
        ),
    )

    casts = [body for path, body in sent if path.endswith("/actions/cast")]
    assert len(casts) == 2, casts
    assert casts[0]["expected_seq"] != casts[1]["expected_seq"]
    assert result["outcome"]["allowed"] is True, result["outcome"]
    assert result["stale_retry"] is True
    assert state(client, game)["stack"], "the retry actually cast the Bolt"


def test_the_retry_happens_exactly_once(client, game, monkeypatch) -> None:
    """Once, never a loop: a seat whose every attempt loses the race is
    handed the refusal to reason about rather than spun forever."""
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    bolt_id, mountain_id = stock_a_bolt(client, game)
    alice.last_seq = last_seq_of(client, game)

    sent: list[tuple[str, Any]] = []
    original = alice.http.request

    def spy(method: str, path: str, **kwargs: Any) -> Any:
        sent.append((path, kwargs.get("json")))
        if path.endswith("/actions/cast"):
            # Somebody else moves the board in the instant between this
            # seat's read and its call - every time, so the retry loses its
            # race exactly the way the first attempt did.
            add_card(client, game, "Forest", "bob", "battlefield")
        return original(method, path, **kwargs)

    monkeypatch.setattr(alice.http, "request", spy)
    result = run_action(
        alice,
        lambda: alice.call_json(
            "POST", f"/api/games/{game}/actions/cast", cast_body(bolt_id, mountain_id)
        ),
    )

    casts = [body for path, body in sent if path.endswith("/actions/cast")]
    assert len(casts) == 2, casts
    assert seatkit.is_stale(result["outcome"])
    assert result["stale_retry"] is True
    assert not state(client, game)["stack"], "nothing was cast"


# ---------------------------------------------------------------------------
# the admin-only pregame command, both of its branches
# ---------------------------------------------------------------------------


def settled_hands(client: TestClient, game_id: str) -> int:
    return sum(len(cards) for cards in state(client, game_id)["hands"].values())


def test_pregame_settle_with_use_defaults_refuses_a_stale_board_and_settles_nothing(
    client, game, admin_key
) -> None:
    """`use_defaults=True` runs `commands.run_command_to_completion` in the
    route's own lock rather than through `_run_command`, so its own guard is
    the only thing standing between a stale operator call and a whole pregame
    settled against a board that has moved."""
    stale = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")
    moved_to = last_seq_of(client, game)

    response = client.post(
        f"/api/games/{game}/pregame/settle",
        params={"uid": admin_key},
        json={"use_defaults": True, "expected_seq": stale},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["stale"] is True
    assert body["allowed"] is False
    assert body["moved_from"] == stale
    assert body["moved_to"] == moved_to

    rows = log_of(client, game, admin_key)
    assert [row["action"] for row in rows if row["action"].startswith("pregame")] == [
        "pregame_settle"
    ], "the engine never ran: no `pregame_settled` row"
    assert next(row for row in rows if row["action"] == "pregame_settle")["detail"]["stale"] is True


def test_pregame_settle_without_expected_seq_settles_exactly_as_today(
    client, game, admin_key
) -> None:
    """The regression guard for the operator caller that predates this
    field: no `expected_seq` key, a board that has moved, and the settle runs
    anyway."""
    add_card(client, game, "Forest", "bob", "battlefield")
    response = client.post(
        f"/api/games/{game}/pregame/settle",
        params={"uid": admin_key},
        json={"use_defaults": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["pregame"] is None
    assert any(row["action"] == "pregame_settled" for row in log_of(client, game, admin_key))


def test_pregame_settle_with_a_current_expected_seq_settles(client, game, admin_key) -> None:
    current = last_seq_of(client, game)
    response = client.post(
        f"/api/games/{game}/pregame/settle",
        params={"uid": admin_key},
        json={"use_defaults": True, "expected_seq": current},
    )
    assert response.status_code == 200, response.text
    assert response.json()["pregame"] is None


# ---------------------------------------------------------------------------
# `expected_seq` is not part of what a command IS
# ---------------------------------------------------------------------------


def test_an_armed_call_that_pauses_still_recognises_its_own_retry(client, game) -> None:
    """The identical command re-posted on a `paused: true` answer is a retry,
    not a competitor (`app._pause_slot_guard`) - and that must hold whether or
    not the call that stored the pause happened to name a board.

    `expected_seq` says which board the caller decided against. It is not part
    of what the command *is*, so it must not reach `Command.payload`, where
    the pause slot's own equality check would read two sends of the same
    action as two different commands and answer the second with a 409 nobody
    can do anything with.
    """
    add_card(client, game, "Flooded Strand", "alice", "battlefield")
    add_card(client, game, "Steam Vents", "alice", "library")
    add_card(client, game, "Island", "alice", "library")
    board = state(client, game)
    strand = find(board["battlefield"], "Flooded Strand")["id"]
    activated = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": strand, "ability_index": 0},
    )
    assert activated.status_code == 200, activated.text
    first = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    assert first.status_code == 200, first.text

    # bob passes naming the board he read, and the fetch's CR 701.23a search
    # pauses on him.
    paused = client.post(
        f"/api/games/{game}/actions/pass-priority",
        json={"player": "bob", "expected_seq": last_seq_of(client, game)},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["paused"] is True, paused.json()

    # The same pass again, this time naming nothing - exactly what seatkit's
    # autopass re-posts on every `paused: true` it reads.
    retry = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert retry.status_code == 200, retry.text
    retried = retry.json()
    assert retried["paused"] is True, retried
    assert retried["pending_decision"]["kind"] == "choose_cards", retried


# ---------------------------------------------------------------------------
# the 409 body is a log read, and it must be THIS caller's log read
# ---------------------------------------------------------------------------
#
# `_board_moved` builds `since` by calling `game_log` with the caller's own
# `uid`, which is the right idea: the caller is handed precisely what a log
# read of its own would have given it. But it read that uid only off the
# query string, and `game_log`'s uid-less default is deliberately the
# *debugger's* eye - full snapshots, every hand, both seats
# (`serialize.state_view`'s `"hands"`). So a caller whose credential the
# handler failed to see was not merely under-redacted, it was handed the
# maximum: `{"hands": {"alice": [...], "bob": [...]}}`, both players' cards,
# on any stale `expected_seq`. Verified before the fix on
# `POST .../actions/pass-priority` with no credential at all.
#
# It resolves the credential through `game_access._credentials` now - the
# one function that already knows every slot a credential arrives in
# (query string first, then the JSON body's own `uid`/`key`) - instead of
# re-implementing half of it.


@pytest.mark.no_harness_credential
def test_a_stale_call_with_no_credential_never_reaches_the_409_at_all(
    client, game, admin_key
) -> None:
    """The leak's actual path, closed twice over. `game_access.py`'s positive
    rule refuses an uncredentialed mutation before the route runs, so the
    handler that used to answer it with both hands is never reached.

    Marked `no_harness_credential` so `conftest.py` does not hand this call
    the admin key it is the whole point of this test not to have.
    """
    stale = client.get(f"/api/games/{game}/log").json()["last_seq"]
    stocked = client.post(
        f"/api/games/{game}/cards",
        params={"key": admin_key},
        json={"card": "Island", "player": "bob", "zone": "hand"},
    )
    assert stocked.status_code == 200, stocked.text

    response = client.post(
        f"/api/games/{game}/actions/pass-priority",
        json={"player": "alice", "expected_seq": stale},
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"no game {game}", response.text
    assert "Island" not in response.text


def test_a_seats_own_stale_409_carries_only_its_own_hand(client, game) -> None:
    uids = seats_for(game)
    add_card(client, game, "Mountain", "alice", "hand")
    add_card(client, game, "Island", "bob", "hand")
    stale = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")

    response = client.post(
        f"/api/games/{game}/actions/pass-priority",
        params={"uid": uids["alice"]},
        json={"player": "alice", "expected_seq": stale},
    )

    assert response.status_code == 409, response.text
    hands = [row["snapshot"]["hands"] for row in response.json()["since"] if row.get("snapshot")]
    assert hands, response.text
    for hand in hands:
        assert hand["alice"] == ["Mountain"], hand
        # `<hidden>`, not absent: `visibility.redact_snapshot` keeps the
        # *count* of the opponent's hand public (CR 402.3 hides the cards,
        # not how many there are) and replaces each identity.
        assert hand["bob"] == ["<hidden>"], hand


def test_the_admin_key_in_its_own_slot_is_recognised_by_the_stale_handler(
    client, game, admin_key
) -> None:
    """`key=` is a credential slot too, and the handler used to look only at
    `uid=`. The admin key is the one identity entitled to the full board
    (`visibility.ADMIN`), so what it gets back is unchanged - but it gets it
    because the handler resolved the credential it was actually given, not
    because it failed to find one and defaulted to everything."""
    add_card(client, game, "Island", "bob", "hand")
    stale = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")

    response = client.post(
        f"/api/games/{game}/actions/pass-priority",
        params={"key": admin_key},
        json={"player": "alice", "expected_seq": stale},
    )

    assert response.status_code == 409, response.text
    hands = [row["snapshot"]["hands"] for row in response.json()["since"] if row.get("snapshot")]
    assert hands and all(hand["bob"] == ["Island"] for hand in hands), response.text
