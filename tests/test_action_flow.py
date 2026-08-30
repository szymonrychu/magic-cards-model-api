""" "One tool call, one meaningful game action": `seatkit.run_action`, shared
by `mcp.py` and `seat_cli.py`.

Every action releases priority (CR 117.3c: the actor already keeps it - this
only decides whether the harness auto-passes it away) and blocks until this
seat next has something to do, unless `hold_priority` says otherwise. These
tests drive the shared flow directly (`seatkit.run_action`/`block_until_wake`)
and, separately, through both transports, to prove they agree.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import httpx
from conftest import add_card, find, life_of, state
from fastapi.testclient import TestClient

from magic_cards_model_api import seat_cli
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats, block_until_wake, run_action
from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def wired_seats(monkeypatch, game_id: str) -> tuple[Any, dict[str, str]]:
    """`Seats` pointed at the in-process `TestClient` rather than the network,
    the same trick `test_decisions.py` uses for the seat CLI."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id), seats_for(game_id)


def stock_a_bolt(client: TestClient, game_id: str, *, uid: str | None = None) -> tuple[str, str]:
    """alice: a Mountain and a Lightning Bolt. Returns (bolt_id, mountain_id).

    `uid` is needed for a game not built through the `game` fixture: `state()`
    otherwise falls back to an admin key that was only ever stashed for that
    fixture's own game, and a bare/no-uid view redacts every hand to nothing.
    """
    add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Lightning Bolt", "alice", "hand")
    board = state(client, game_id, uid=uid)
    mountain = find(board["battlefield"], "Mountain")
    bolt = board["hands"]["alice"][0]
    return bolt["id"], mountain["id"]


def cast_bolt_action(seat: Any, game_id: str, bolt_id: str, mountain_id: str) -> Any:
    return lambda: seat.call_json(
        "POST",
        f"/api/games/{game_id}/actions/cast",
        {
            "player": "alice",
            "card_id": bolt_id,
            "targets": ["bob"],
            "mana_sources": [mountain_id],
        },
    )


# -- releasing priority and blocking -----------------------------------------


def test_action_releases_priority_and_the_opponents_pass_wakes_it(client, game, monkeypatch):
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    bolt_id, mountain_id = stock_a_bolt(client, game)

    box: dict[str, Any] = {}

    def do_cast() -> None:
        box["result"] = run_action(
            alice, cast_bolt_action(alice, game, bolt_id, mountain_id), max_seconds=5
        )

    thread = threading.Thread(target=do_cast)
    thread.start()
    thread.join(timeout=0.5)
    assert thread.is_alive(), "alice should be blocked on her own next moment to act"

    # Bob passes with the bolt on the stack: it resolves, and priority comes
    # back to alice with the board changed (bob took 3, the stack is empty).
    passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert passed.json()["allowed"] is True

    thread.join(timeout=5)
    assert not thread.is_alive(), "alice's blocked call never returned"
    result = box["result"]
    assert result["outcome"]["allowed"] is True
    assert result["held_priority"] is False
    assert "you have priority" in result["wake_reason"]
    assert result["state"]["priority_holder"] == "alice"
    assert life_of(result["state"], "bob") == 17
    assert result["state"]["stack"] == []


def test_hold_priority_returns_immediately_and_keeps_priority(client, game, monkeypatch):
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    bolt_id, mountain_id = stock_a_bolt(client, game)

    started = time.monotonic()
    result = run_action(
        alice,
        cast_bolt_action(alice, game, bolt_id, mountain_id),
        hold_priority=True,
        max_seconds=30,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2, "hold_priority must never block"
    assert result["outcome"]["allowed"] is True
    assert result["held_priority"] is True
    assert result["state"]["priority_holder"] == "alice", "the cast retains priority (CR 117.3c)"
    assert result["state"]["stack"][0]["name"] == "Lightning Bolt", "the action itself still ran"


def test_a_refused_action_returns_immediately_without_blocking(client, game, monkeypatch):
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    # No mana on the battlefield at all: the cast is refused before anything changes.
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt_id = state(client, game)["hands"]["alice"][0]["id"]

    started = time.monotonic()
    result = run_action(
        alice,
        lambda: alice.call_json(
            "POST",
            f"/api/games/{game}/actions/cast",
            {"player": "alice", "card_id": bolt_id, "targets": ["bob"], "mana_sources": []},
        ),
        max_seconds=30,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2, "a refusal must not block"
    assert result["outcome"]["allowed"] is False
    assert result["held_priority"] is True
    assert "refused" in result["wake_reason"]
    # The refusal changed nothing: the bolt is still in hand.
    assert bolt_id in [c["id"] for c in result["state"]["hands"]["alice"]]


# -- the other wake conditions ------------------------------------------------


def test_pending_decision_wakes_a_blocked_call(client, game, monkeypatch):
    """Reuses `test_decisions.py`'s cleanup-discard mechanism: alice is
    blocked on her own status (not tied to any particular action) while a
    second thread drives the turn into cleanup, which asks her a CR 608.2d
    question - that question is what must wake her."""
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    for _ in range(8):
        add_card(client, game, "Mountain", "alice", "hand")

    box: dict[str, Any] = {}

    def do_wait() -> None:
        # Advancing the turn into cleanup crosses several step boundaries,
        # each of which hands the active player (alice) priority again (CR
        # 117.3a) - a legitimate wake in its own right, just not the one this
        # test is after. A real caller in that situation calls again; this
        # loop does the same, until the specific wake it wants shows up or
        # its own deadline runs out.
        deadline = time.monotonic() + 10
        before = None
        result: dict[str, Any] = {}
        while time.monotonic() < deadline:
            result = block_until_wake(alice, before=before, max_seconds=1)
            if "a decision is waiting for you" in result["wake_reason"]:
                break
            before = result["state"]
        box["result"] = result

    waiter = threading.Thread(target=do_wait)
    waiter.start()

    def advance_into_cleanup() -> None:
        stepping = TestClient(app)
        for _ in range(20):
            response = stepping.post(f"/api/games/{game}/advance", json={"player": "alice"})
            if response.status_code != 200:
                return
            if response.json().get("advanced_to") == "cleanup":
                return

    advancer = threading.Thread(target=advance_into_cleanup)
    advancer.start()
    try:
        waiter.join(timeout=15)
        assert not waiter.is_alive(), "the pending decision never woke the blocked call"
        result = box["result"]
        assert "a decision is waiting for you" in result["wake_reason"]
        assert result["state"]["pending_decision"]["player"] == "alice"
        assert result["state"]["pending_decision"]["kind"] == "choose_cards"

        # Answer it so the advancing thread can finish cleanly.
        pending = result["state"]["pending_decision"]
        client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "card_ids": [pending["cards"][0]]},
        )
    finally:
        advancer.join(timeout=10)
        assert not advancer.is_alive()


def test_game_over_wakes_a_blocked_call(client, game, monkeypatch):
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    # Give away priority so alice's wait does not fire on "you have priority"
    # instantly - the point here is specifically the game-over wake.
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})

    box: dict[str, Any] = {}

    def do_wait() -> None:
        box["result"] = block_until_wake(alice, before=None, max_seconds=10)

    thread = threading.Thread(target=do_wait)
    thread.start()
    thread.join(timeout=0.5)
    assert thread.is_alive()

    client.patch(f"/api/games/{game}/players/bob", json={"life": 0})
    client.post(f"/api/games/{game}/state-based-actions")

    thread.join(timeout=10)
    assert not thread.is_alive()
    assert box["result"]["wake_reason"] == "game over"
    assert box["result"]["state"]["winner"] == "alice"


def test_timeout_returns_the_timeout_wake_reason_and_does_not_corrupt_anything(
    client, game, monkeypatch
):
    seats, uids = wired_seats(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])  # bob never has priority in this game
    result = block_until_wake(bob, before=None, max_seconds=0.3)
    assert "timed out" in result["wake_reason"]
    # The game is exactly as it was: still alice's priority, nothing broken.
    assert result["state"]["priority_holder"] == "alice"
    still_fine = state(client, game)
    assert still_fine["priority_holder"] == "alice"


def test_two_seats_both_blocked_one_acts_the_other_wakes(client, game, monkeypatch):
    """The realistic case: bob is blocked waiting for his turn to matter;
    alice, who already has priority, acts and releases it. Bob's blocked wait
    - and only his - wakes."""
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    bob = seats.seat_for(uids["bob"])
    bolt_id, mountain_id = stock_a_bolt(client, game)

    bob_box: dict[str, Any] = {}

    def bob_waits() -> None:
        bob_box["result"] = block_until_wake(bob, before=None, max_seconds=10)

    bob_thread = threading.Thread(target=bob_waits)
    bob_thread.start()
    bob_thread.join(timeout=0.5)
    assert bob_thread.is_alive(), "bob has no priority yet - he should still be blocked"

    # Alice's own call hands priority straight to bob and then has nothing
    # left to wait for on her own account, so it rides out its bound (kept
    # short here purely so the test does not sit idle) rather than waking -
    # proving this is bob's moment, not hers, is what matters below.
    alice_result = run_action(
        alice, cast_bolt_action(alice, game, bolt_id, mountain_id), max_seconds=1.5
    )
    assert alice_result["outcome"]["allowed"] is True

    bob_thread.join(timeout=10)
    assert not bob_thread.is_alive(), "bob's blocked wait never woke up"
    assert "you have priority" in bob_box["result"]["wake_reason"]
    assert bob_box["result"]["state"]["priority_holder"] == "bob"


def test_no_deadlock_block_until_wake_never_holds_the_game_lock(client, game, monkeypatch):
    """The invariant `decisions.py` documents for `decide` versus `Game.ask`
    applies equally here: a blocked wait must never hold `store.locked`, or it
    would deadlock against the very request that is supposed to wake it (that
    request also needs the lock to record its own action). Proven by holding
    the lock for the whole test and blocking anyway - if `block_until_wake`
    ever tried to take it, this would hang past the thread's own join timeout
    instead of finishing well within it."""
    seats, uids = wired_seats(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])

    box: dict[str, Any] = {}

    def do_wait() -> None:
        started = time.monotonic()
        box["result"] = block_until_wake(bob, before=None, max_seconds=1.5)
        box["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=do_wait)
    with store.locked(game):
        thread.start()
        thread.join(timeout=5)
    assert not thread.is_alive(), "block_until_wake hung - it must have tried to take the game lock"
    assert box["elapsed"] < 4


# -- the available-actions summary --------------------------------------------


def test_available_actions_summary_never_leaks_the_opponents_hand_or_library(
    client, game, monkeypatch
):
    """Security test: `run_action`'s `available_actions` must be computed
    only from this seat's own filtered view - it must never surface the
    opponent's hand (secret) or either library (never shown to anyone)."""
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    bolt_id, mountain_id = stock_a_bolt(client, game)
    add_card(client, game, "Grizzly Bears", "bob", "hand")  # bob's own secret card
    add_card(client, game, "Mountain", "bob", "battlefield")

    result = run_action(
        alice,
        cast_bolt_action(alice, game, bolt_id, mountain_id),
        max_seconds=5,
        hold_priority=True,
    )
    dumped = str(result)
    assert "Grizzly Bears" not in dumped, "bob's hand leaked into the summary"
    assert "hands" not in result["available_actions"]
    assert "libraries" not in result["state"]
    assert result["state"]["hands"]["bob"] == []
    # bob's Mountain is public (battlefield), but its activatable mana ability
    # must not be offered to alice - only permanents *she* controls may be.
    activatable_ids = {entry["card_id"] for entry in result["available_actions"]["activatable"]}
    bobs_mountain = find(state(client, game)["battlefield"], "Mountain")
    assert bobs_mountain["id"] not in activatable_ids


# -- both transports agree ----------------------------------------------------


def call_mcp_tool(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, arguments))
    text = result.content[0].text
    import json

    return json.loads(text)


def test_seat_cli_and_mcp_tool_agree_on_the_same_flow(client, game, monkeypatch, capsys):
    """The whole "cast, release, block, wake" flow driven once through
    `seat_cli` and once through the MCP tool surface, on two independent
    games, proving they behave the same way (same shared `run_action`)."""
    seats, uids = wired_seats(monkeypatch, game)
    bolt_id, mountain_id = stock_a_bolt(client, game)

    # -- seat_cli path --
    def cli_cast() -> None:
        seat_cli.run(
            [
                "--uid",
                uids["alice"],
                "--game",
                game,
                "cast",
                bolt_id,
                "--target",
                "bob",
                "--sources",
                mountain_id,
            ]
        )

    cli_thread = threading.Thread(target=cli_cast)
    cli_thread.start()
    cli_thread.join(timeout=0.5)
    assert cli_thread.is_alive(), "seat_cli cast should also block on its own next moment"
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    cli_thread.join(timeout=5)
    assert not cli_thread.is_alive()
    cli_printed = capsys.readouterr().out
    assert "you have priority" in cli_printed
    assert life_of(state(client, game), "bob") == 17

    # -- MCP tool path, same shape of scenario on a second game --
    created = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    game2 = created["game_id"]
    try:
        seats2, uids2 = wired_seats(monkeypatch, game2)
        bolt_id2, mountain_id2 = stock_a_bolt(client, game2, uid=uids2["alice"])
        server = build_server(seats2)

        mcp_box: dict[str, Any] = {}

        def mcp_cast() -> None:
            mcp_box["result"] = call_mcp_tool(
                server,
                "cast",
                {
                    "player_uid": uids2["alice"],
                    "card": bolt_id2,
                    "targets": "bob",
                    "mana_sources": mountain_id2,
                },
            )

        mcp_thread = threading.Thread(target=mcp_cast)
        mcp_thread.start()
        mcp_thread.join(timeout=0.5)
        assert mcp_thread.is_alive(), "the MCP tool should block the same way seat_cli does"
        client.post(f"/api/games/{game2}/actions/pass-priority", json={"player": "bob"})
        mcp_thread.join(timeout=5)
        assert not mcp_thread.is_alive()
        result = mcp_box["result"]
        assert result["outcome"]["allowed"] is True
        assert "you have priority" in result["wake_reason"]
        assert life_of(result["state"], "bob") == 17
    finally:
        client.delete(f"/api/games/{game2}")
