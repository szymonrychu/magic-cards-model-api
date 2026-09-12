"""How many round trips one game turn costs a seat, and the two places this
repo was spending them for nothing.

A seat's turn latency is `cycles x per-cycle-latency`, and a cycle is one
tool call plus the inference that reads its answer. These tests pin the two
collapses that cut `cycles`, both grounded in a live seat pod's own
transcript (`mtg-seat-3b6613e0`, 65 tool calls in one agent activation):

1. **A wait that does not bind is a spin loop.** 55 of those 65 calls were
   `wait_for_priority`, and 53 of the 64 adjacent transitions were one
   `wait_for_priority` straight into the next. Every one of them asked for
   `max_seconds` (20, then 30, then 60, then 600) and every one of them
   came back in 61-118 ms with the same 9.5 KB board, because
   `compute_wake_reason` reported "another seat owes a pregame declaration"
   as a *wake* - so `block_until_wake` returned before its bound could
   apply. It is not a wake: CR 103.5 gives the mulligan declaration to each
   player in turn order and nothing at all happens until every player has
   declared ("Once each player has made a declaration, all players who
   decided to take mulligans do so at the same time"), and CR 103.6's
   opening-hand actions only begin "once the mulligan process (see rule
   103.5) is complete". A seat that has already declared has no legal move
   there - which is the definition of `NOTHING_NEW_YET`, not of a wake.
   Both rules were read in full with `mise run rules:find` in the session
   that wrote this.

2. **"What happened, and what can I do" was two calls.** Every answer that
   carries a state view already carries `available_actions` - what this
   seat can do now - but not one line of what just happened, so the seat
   had to spend a second call on `game_log` to find out. `prompt.py` said
   so outright ("read `battlefield_state`, read `game_log`"), and the live
   transcript shows the seat doing exactly that mid-loop. The log tail now
   rides along on the same answer, and that instruction is gone from both
   briefs - `tests/test_prompt_matches_tools.py` keeps it gone.

The third candidate collapse - "have an action-submitting tool return the
resulting state" - needed no work: `seatkit.run_action` has always returned
`state` and `available_actions` alongside the action's own outcome, and so
has `pass_until`. `test_an_action_already_answers_with_its_resulting_state`
pins that rather than claiming credit for it.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from conftest import add_card, find
from fastapi.testclient import TestClient
from test_mulligans import PLAIN_DECK, boot, declare, delete, keep_both_and_start

from magic_cards_model_api import seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import NOTHING_NEW_YET, Seats, compute_wake_reason, run_action
from magic_cards_model_api.store import store


def wired(monkeypatch, game_id: str) -> Seats:
    """`Seats` pointed at the in-process app rather than the network."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id)


def call(server: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """One MCP tool call, as the JSON the seat actually reads."""
    return json.loads(asyncio.run(server.call_tool(tool, args)).content[0].text)


# -- 1. the bounded wait has to bind while another seat owes a declaration ----


def test_waiting_on_another_seats_pregame_declaration_is_not_a_wake() -> None:
    """The pure condition, with no game in the loop. A seat that owes a
    declaration itself is woken (it is being asked for something); a seat
    waiting on somebody else's is not, because CR 103.5 leaves it nothing to
    do until every player has declared."""
    waiting_on_the_other_seat = {
        "pregame": {"awaiting_mulligan": ["bob"], "opening_hand_actor": None},
        "step": "mulligan",
    }
    assert compute_wake_reason(waiting_on_the_other_seat, None, "alice") is None

    owes_one_itself = {
        "pregame": {"awaiting_mulligan": ["alice", "bob"], "opening_hand_actor": None},
        "step": "mulligan",
    }
    reason = compute_wake_reason(owes_one_itself, None, "alice")
    assert reason is not None and "103.5" in reason


def test_waiting_on_another_seats_opening_hand_action_is_not_a_wake() -> None:
    """The same for CR 103.6: the actor is woken, the seat watching is not."""
    theirs = {"pregame": {"awaiting_mulligan": [], "opening_hand_actor": "bob"}, "step": "mulligan"}
    assert compute_wake_reason(theirs, None, "alice") is None

    mine = {"pregame": {"awaiting_mulligan": [], "opening_hand_actor": "alice"}}
    reason = compute_wake_reason(mine, None, "alice")
    assert reason is not None and "103.6" in reason


def test_wait_for_priority_sits_out_its_bound_in_the_pregame(client, monkeypatch) -> None:
    """The whole point, through the real MCP tool against a real booted game:
    alice has declared, bob has not, and `wait_for_priority(max_seconds=...)`
    must spend that bound on the server rather than handing back an identical
    board every 70 ms for the seat to burn an inference on."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        assert declare(client, info, "alice", take=False).status_code == 200
        assert client.get(
            f"/api/games/{info['game_id']}", params={"uid": info["seat_uids"]["alice"]}
        ).json()["pregame"]["awaiting_mulligan"] == ["bob"]

        server = build_server(wired(monkeypatch, info["game_id"]))
        started = time.monotonic()
        answer = call(
            server,
            "wait_for_priority",
            {"player_uid": info["seat_uids"]["alice"], "max_seconds": 2.0},
        )
        elapsed = time.monotonic() - started

        assert answer["wake_reason"] == NOTHING_NEW_YET, (
            "waiting on the other seat's CR 103.5 declaration is not a wake - "
            f"got {answer['wake_reason']!r}"
        )
        assert elapsed >= 1.5, (
            f"the bounded wait returned after {elapsed:.2f}s of a 2.0s bound - "
            "this is the 53-in-a-row spin loop the live pod ran"
        )
    finally:
        delete(client, info)


def test_the_seat_that_owes_the_declaration_is_still_woken_at_once(client, monkeypatch) -> None:
    """The other half, and the one that must not regress: a seat the game is
    actually waiting on may never be made to sit out a bound - it would be
    waiting for itself, and the whole game with it."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        server = build_server(wired(monkeypatch, info["game_id"]))
        started = time.monotonic()
        answer = call(
            server,
            "wait_for_priority",
            {"player_uid": info["seat_uids"]["alice"], "max_seconds": 20.0},
        )
        assert time.monotonic() - started < 2.0
        assert "103.5" in answer["wake_reason"]
    finally:
        delete(client, info)


# -- 2. the log rides along with the state -----------------------------------


def stock_a_bolt(client: TestClient, game_id: str, uid: str) -> tuple[str, str]:
    add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Lightning Bolt", "alice", "hand")
    board = client.get(f"/api/games/{game_id}", params={"uid": uid}).json()
    return board["hands"]["alice"][0]["id"], find(board["battlefield"], "Mountain")["id"]


def test_battlefield_state_carries_the_recent_log(client, game, monkeypatch) -> None:
    """ "What happened since I last acted" and "what can I do now" are one
    round trip. Before this, `battlefield_state` answered only the second half and the
    seat spent a whole extra cycle on `game_log` for the first."""
    seats = wired(monkeypatch, game)
    uid = next(uid for uid, name in store.seat_uids(game).items() if name == "alice")
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})

    server = build_server(seats)
    answer = call(server, "battlefield_state", {"player_uid": uid})

    assert "recent_log" in answer, (
        "battlefield_state must carry the log tail, not make it a second call"
    )
    assert answer["recent_log"]["entries"], answer["recent_log"]
    assert answer["recent_log"]["last_seq"] > 0
    assert len(answer["recent_log"]["entries"]) <= seatkit.RECENT_LOG_ENTRIES


def test_an_action_answer_carries_the_recent_log(client, game, monkeypatch) -> None:
    """Every action tool goes through `run_action`, so pinning it there pins
    `cast`, `play_land`, `activate`, `decide`, `pass` and the rest at once."""
    seats = wired(monkeypatch, game)
    uids = {name: uid for uid, name in store.seat_uids(game).items()}
    alice = seats.seat_for(uids["alice"])
    bolt, mountain = stock_a_bolt(client, game, uids["alice"])

    result = run_action(
        alice,
        lambda: alice.call_json(
            "POST",
            f"/api/games/{game}/actions/cast",
            {
                "player": "alice",
                "card_id": bolt,
                "targets": ["bob"],
                "mana_sources": [mountain],
            },
        ),
    )

    assert result["outcome"]["allowed"] is True
    assert result["recent_log"]["entries"], result["recent_log"]
    names = [entry["action"] for entry in result["recent_log"]["entries"]]
    assert "cast" in names, names


def test_an_action_already_answers_with_its_resulting_state(client, game, monkeypatch) -> None:
    """Not a new collapse - a pin on one that was already here, so nobody
    re-implements it. Submit-then-read has never been two calls: the action's
    own answer carries the state it produced and this seat's legal actions in
    it."""
    seats = wired(monkeypatch, game)
    uids = {name: uid for uid, name in store.seat_uids(game).items()}
    alice = seats.seat_for(uids["alice"])
    bolt, mountain = stock_a_bolt(client, game, uids["alice"])

    result = run_action(
        alice,
        lambda: alice.call_json(
            "POST",
            f"/api/games/{game}/actions/cast",
            {
                "player": "alice",
                "card_id": bolt,
                "targets": ["bob"],
                "mana_sources": [mountain],
            },
        ),
    )
    assert result["state"]["turn_number"] >= 1
    assert "actions" in result["available_actions"]


def test_pass_until_carries_the_recent_log(client, monkeypatch) -> None:
    """A `pass_until` run crosses windows the seat never sees. The log tail is
    how it finds out what it passed through without a second call."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        assert keep_both_and_start(client, info) == "upkeep"
        server = build_server(wired(monkeypatch, info["game_id"]))
        answer = call(
            server,
            "pass_until",
            {
                "player_uid": info["seat_uids"]["alice"],
                "step": "precombat_main",
                "reasoning": "nothing to do in my upkeep",
            },
        )
        assert answer["recent_log"]["entries"], answer["recent_log"]
        assert answer["recent_log"]["last_seq"] > 0
    finally:
        delete(client, info)


def test_game_log_can_be_asked_for_only_what_is_new(client, game, monkeypatch) -> None:
    """`since` was already on the API's own `GET .../log`; the tool never
    offered it, so a seat that wanted the new entries had to re-read the whole
    tail and diff it itself."""
    server = build_server(wired(monkeypatch, game))
    uid = next(uid for uid, name in store.seat_uids(game).items() if name == "alice")

    first = call(server, "game_log", {"player_uid": uid})
    watermark = first["last_seq"]
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})

    newer = call(server, "game_log", {"player_uid": uid, "since": watermark})
    assert newer["entries"], newer
    assert all(entry["seq"] > watermark for entry in newer["entries"])
    assert newer["last_seq"] > watermark
