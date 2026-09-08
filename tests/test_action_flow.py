""" "One tool call, one meaningful game action": `seatkit.run_action`, shared
by `mcp.py` and `seat_cli.py`.

Every action releases priority (CR 117.3c: the actor already keeps it - this
only decides whether the harness auto-passes it away) unless `hold_priority`
says otherwise, then checks once for this seat's next moment to act
(poll-and-return - `block_until_wake` never blocks any more, T11: MCP
2026-07-28 removed resumability). These tests drive the shared flow directly
(`seatkit.run_action`/`block_until_wake`) and, separately, through both
transports, to prove they agree. A wake that used to require a second thread
racing an in-flight blocked call is now just a second call, made after
whatever it was waiting for has actually happened - the same thing a real
caller does.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from conftest import add_card, find, life_of, state
from fastapi.testclient import TestClient
from mcp_types import InputRequiredResult
from test_decisions import advance_into_cleanup

from magic_cards_model_api import seat_cli
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import NOTHING_NEW_YET, Seats, block_until_wake, run_action
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


# -- releasing priority, checking, and checking again -------------------------


def test_action_releases_priority_and_the_next_check_sees_nothing_yet(client, game, monkeypatch):
    """`run_action` now checks exactly once, right after the action, rather
    than blocking until something happens - so right after alice casts and
    releases priority to bob, before bob has done anything at all, there is
    nothing new for her to see yet."""
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    bolt_id, mountain_id = stock_a_bolt(client, game)

    result = run_action(alice, cast_bolt_action(alice, game, bolt_id, mountain_id))
    assert result["outcome"]["allowed"] is True
    assert result["held_priority"] is False
    assert result["wake_reason"] == NOTHING_NEW_YET
    assert result["state"]["priority_holder"] == "bob"


def test_a_second_check_after_the_opponents_pass_sees_the_wake(client, game, monkeypatch):
    """The other half: once bob has actually passed (resolving the bolt on
    the stack), a fresh check - not the one that already returned above, a
    new one - sees the board has changed and priority is alice's again."""
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    bolt_id, mountain_id = stock_a_bolt(client, game)

    first = run_action(alice, cast_bolt_action(alice, game, bolt_id, mountain_id))
    assert first["wake_reason"] == NOTHING_NEW_YET

    # Bob passes with the bolt on the stack: it resolves, and priority comes
    # back to alice with the board changed (bob took 3, the stack is empty).
    passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert passed.json()["allowed"] is True

    woken = block_until_wake(alice, before=first["state"])
    assert "you have priority" in woken["wake_reason"]
    assert woken["state"]["priority_holder"] == "alice"
    assert life_of(woken["state"], "bob") == 17
    assert woken["state"]["stack"] == []


def test_hold_priority_returns_immediately_and_keeps_priority(client, game, monkeypatch):
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    bolt_id, mountain_id = stock_a_bolt(client, game)

    started = time.monotonic()
    result = run_action(
        alice,
        cast_bolt_action(alice, game, bolt_id, mountain_id),
        hold_priority=True,
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
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2, "a refusal must not block"
    assert result["outcome"]["allowed"] is False
    assert result["held_priority"] is True
    assert "refused" in result["wake_reason"]
    # The refusal changed nothing: the bolt is still in hand.
    assert bolt_id in [c["id"] for c in result["state"]["hands"]["alice"]]


def test_play_land_by_an_unresolvable_name_is_refused_not_a_silent_pass(client, game, monkeypatch):
    """The bug this regression proves fixed: `seat_cli.py`'s `play-land`
    sends a bare `card` name for anything that is not a UUID. When that name
    matched no land in hand, the endpoint used to raise a plain `ValueError`
    before `audit_outcome` ever ran - a 400 shaped `{"detail": "..."}`
    instead of the ordinary `{"allowed": False, ...}` refusal. `_refused`
    only recognises the latter shape, so `run_action` fell through past both
    the unknown-outcome and refused branches and treated the call as a
    success: it released priority and blocked, exactly as if the land had
    been played. Two such calls in a row - the report that started this -
    cost a full main phase and combat step with no warning and no audit
    entry to explain why.

    `held_priority` must be `True` and the wake reason must say "refused":
    the same two assertions `test_a_refused_action_returns_immediately_without_blocking`
    makes for a bad cast, now made for a `play-land` naming a card that is
    not in hand at all.
    """
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])

    started = time.monotonic()
    result = run_action(
        alice,
        lambda: alice.call_json(
            "POST",
            f"/api/games/{game}/actions/play-land",
            {"player": "alice", "card": "Not A Real Card"},
        ),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2, "a refusal must not block"
    assert result["outcome"]["allowed"] is False
    assert result["held_priority"] is True, "a refused play-land must not release priority"
    assert "refused" in result["wake_reason"]
    assert result["state"]["priority_holder"] == "alice", "priority never left, so it is still hers"


# -- the other wake conditions ------------------------------------------------


def test_pending_decision_shows_up_on_the_next_check(client, game, monkeypatch):
    """Reuses `test_decisions.py`'s cleanup-discard mechanism
    (`advance_into_cleanup`) to drive the turn into alice's own CR 608.2d
    cleanup discard, sequentially - no second thread needed any more: a
    single-shot check either sees the decision or it does not, so "wait for
    it" is just "check after the thing that raises it has actually
    happened", the same as any other poll-and-return caller."""
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    for _ in range(8):
        add_card(client, game, "Mountain", "alice", "hand")

    paused = advance_into_cleanup(game, "alice")
    assert paused is not None, "the discard should have asked, not defaulted silently"

    result = block_until_wake(alice, before=None)
    assert "a decision is waiting for you" in result["wake_reason"]
    assert result["state"]["pending_decision"]["player"] == "alice"
    assert result["state"]["pending_decision"]["kind"] == "choose_cards"

    # Answer it, proving it is a live decision, not a stale echo.
    pending = result["state"]["pending_decision"]
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [pending["cards"][0]]},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True


def test_game_over_shows_up_on_the_next_check(client, game, monkeypatch):
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    # Give away priority so the check below cannot fire on "you have
    # priority" instead - the point here is specifically the game-over wake.
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})

    before = block_until_wake(alice, before=None)
    assert before["wake_reason"] != "game over"

    client.patch(f"/api/games/{game}/players/bob", json={"life": 0})
    client.post(f"/api/games/{game}/state-based-actions")

    result = block_until_wake(alice, before=before["state"])
    assert result["wake_reason"] == "game over"
    assert result["state"]["winner"] == "alice"


def test_nothing_new_returns_the_nothing_new_wake_reason_and_does_not_corrupt_anything(
    client, game, monkeypatch
):
    seats, uids = wired_seats(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])  # bob never has priority in this game
    started = time.monotonic()
    result = block_until_wake(bob, before=None)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"block_until_wake took {elapsed:.1f}s - it must never block"
    assert result["wake_reason"] == NOTHING_NEW_YET
    # The game is exactly as it was: still alice's priority, nothing broken.
    assert result["state"]["priority_holder"] == "alice"
    still_fine = state(client, game)
    assert still_fine["priority_holder"] == "alice"


def test_two_seats_one_acts_the_others_next_check_sees_it(client, game, monkeypatch):
    """The realistic case: bob has nothing to see yet, waiting for his turn
    to matter; alice, who already has priority, acts and releases it to him.
    Bob's next check - and only his - sees the wake."""
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    bob = seats.seat_for(uids["bob"])
    bolt_id, mountain_id = stock_a_bolt(client, game)

    bob_before = block_until_wake(bob, before=None)
    assert bob_before["wake_reason"] == NOTHING_NEW_YET, "bob has no priority yet"

    # Alice's own call hands priority straight to bob - proving this is
    # bob's moment, not hers, is what the next check below is for.
    alice_result = run_action(alice, cast_bolt_action(alice, game, bolt_id, mountain_id))
    assert alice_result["outcome"]["allowed"] is True

    bob_woken = block_until_wake(bob, before=bob_before["state"])
    assert "you have priority" in bob_woken["wake_reason"]
    assert bob_woken["state"]["priority_holder"] == "bob"


def test_no_deadlock_block_until_wake_never_holds_the_game_lock(client, game, monkeypatch):
    """The invariant `decisions.py` documents for `decide` versus `Game.ask`
    applies equally here: a check must never hold `store.locked`, or it would
    deadlock against the very request that is supposed to change what it
    sees (that request also needs the lock to record its own action).

    `block_until_wake` is a single-shot GET read now (T11: poll-and-return),
    so this no longer needs a second thread to prove the point - holding the
    lock in this very thread and calling it directly is enough: if it ever
    tried to take `store.locked` itself, this would hang."""
    seats, uids = wired_seats(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])

    started = time.monotonic()
    with store.locked(game):
        result = block_until_wake(bob, before=None)
    elapsed = time.monotonic() - started
    assert elapsed < 2, "block_until_wake must not have tried to take the game lock"
    assert result["wake_reason"] == NOTHING_NEW_YET


# -- the bounded wait (`max_seconds`) -----------------------------------------
#
# The owner's report: an agent seat busy-polling with `Bash sleep 2` (then 3,
# 5, 15, 20, 25, 30 - a hand-rolled escalating backoff) between calls, because
# poll-and-return removed the only wait the tools ever offered. This brings a
# *bounded* one back: `max_seconds`, capped well under both `Seat`'s own
# `CLIENT_TIMEOUT_SECONDS` (30s) and any transport-level read timeout a real
# MCP client enforces on one tool call, so the call still always returns on
# its own rather than risking the connection outliving it - the exact hazard
# MCP 2026-07-28 dropping resumability (SEP-2577) made the old *unbounded*
# wait wrong. `WAIT_POLL_SECONDS` is monkeypatched small in these tests so
# they run fast without changing the mechanism under test.


def test_default_max_seconds_is_still_the_single_check(client, game, monkeypatch):
    """`max_seconds` left at its default (0.0) must behave exactly like
    before this feature existed - a single check, returned at once - so
    every existing caller (nothing passes `max_seconds` yet) is unaffected."""
    seats, uids = wired_seats(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])
    started = time.monotonic()
    result = block_until_wake(bob, before=None)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"took {elapsed:.2f}s - the default must not wait at all"
    assert result["wake_reason"] == NOTHING_NEW_YET


def test_a_wake_condition_appearing_mid_wait_is_noticed_within_max_seconds(
    client, game, monkeypatch
):
    """The whole point of bringing a wait back: unlike a single-shot check,
    this notices a wake condition that shows up *while the call is still
    open*, rather than only on the next separate call - promptly, not only
    once the whole bound has elapsed."""
    import threading

    from magic_cards_model_api import seatkit as seatkit_module

    monkeypatch.setattr(seatkit_module, "WAIT_POLL_SECONDS", 0.05)
    seats, uids = wired_seats(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])  # bob has no priority yet

    def hand_bob_priority() -> None:
        time.sleep(0.2)
        client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})

    threading.Thread(target=hand_bob_priority, daemon=True).start()

    started = time.monotonic()
    result = block_until_wake(bob, before=None, max_seconds=2.0)
    elapsed = time.monotonic() - started

    assert result["wake_reason"] != NOTHING_NEW_YET, result
    assert "you have priority" in result["wake_reason"]
    assert result["state"]["priority_holder"] == "bob"
    assert 0.15 < elapsed < 1.0, (
        f"took {elapsed:.2f}s - should notice promptly once the wake fires, "
        "not ride out the whole max_seconds bound"
    )


def test_nothing_new_returns_only_once_max_seconds_has_actually_elapsed(client, game, monkeypatch):
    """The other half: when nothing ever changes, the call still returns on
    its own once the bound is up (never past it) - not before, or the bound
    would be a lie, and not indefinitely, which is the whole hazard this
    feature exists to avoid reintroducing."""
    from magic_cards_model_api import seatkit as seatkit_module

    monkeypatch.setattr(seatkit_module, "WAIT_POLL_SECONDS", 0.05)
    seats, uids = wired_seats(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])

    started = time.monotonic()
    result = block_until_wake(bob, before=None, max_seconds=0.3)
    elapsed = time.monotonic() - started

    assert result["wake_reason"] == NOTHING_NEW_YET
    assert elapsed >= 0.3, f"returned early at {elapsed:.2f}s despite nothing changing"
    assert elapsed < 1.5, f"overshot the bound badly: {elapsed:.2f}s for max_seconds=0.3"


def test_a_caller_requested_max_seconds_past_the_ceiling_is_capped(client, game, monkeypatch):
    """`max_seconds` is a caller's request, not a caller's command - the
    ceiling (`MAX_WAIT_SECONDS`) is what actually keeps this safe to bring
    back at all, so a caller asking for more than it must not get more than
    it, ever."""
    from magic_cards_model_api import seatkit as seatkit_module

    monkeypatch.setattr(seatkit_module, "MAX_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(seatkit_module, "WAIT_POLL_SECONDS", 0.05)
    seats, uids = wired_seats(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])

    started = time.monotonic()
    result = block_until_wake(bob, before=None, max_seconds=999.0)
    elapsed = time.monotonic() - started

    assert result["wake_reason"] == NOTHING_NEW_YET
    assert elapsed < 1.5, f"a caller-requested 999s was not capped: took {elapsed:.2f}s"


def test_the_mcp_tool_threads_max_seconds_to_block_until_wake(client, game, monkeypatch):
    """`mcp.wait_for_priority`'s own `max_seconds` parameter has to actually
    reach `seatkit.block_until_wake` - spied here rather than timed again,
    since the wait mechanics themselves are already pinned above; what is
    new here is only that this transport passes the value through at all."""
    from magic_cards_model_api import seatkit as seatkit_module

    seen: list[float] = []
    real = seatkit_module.block_until_wake

    def spy(seat: Any, before: Any = None, max_seconds: float = 0.0) -> Any:
        seen.append(max_seconds)
        return real(seat, before, max_seconds=0.0)  # never actually wait in this test

    monkeypatch.setattr(seatkit_module, "block_until_wake", spy)
    seats, uids = wired_seats(monkeypatch, game)
    server = build_server(seats)

    call_mcp_tool(server, "wait_for_priority", {"player_uid": uids["alice"], "max_seconds": 7.5})
    assert seen == [7.5]


def test_seat_cli_wait_threads_max_seconds_to_block_until_wake(client, game, monkeypatch, capsys):
    """The same wiring, on the `seat_cli` transport - `wait --max-seconds`.
    `seat_cli.py` imports `block_until_wake` as a bare name (`from .seatkit
    import ... block_until_wake`), so the spy has to replace that name in
    `seat_cli`'s own module namespace - patching `seatkit.block_until_wake`
    itself would not be seen by `seat_cli`'s already-bound reference."""
    real = seat_cli.block_until_wake
    seen: list[float] = []

    def spy(seat: Any, before: Any = None, max_seconds: float = 0.0) -> Any:
        seen.append(max_seconds)
        return real(seat, before, max_seconds=0.0)

    monkeypatch.setattr(seat_cli, "block_until_wake", spy)
    seats, uids = wired_seats(monkeypatch, game)

    seat_cli.run(["--uid", uids["alice"], "--game", game, "wait", "--max-seconds", "7.5"])
    capsys.readouterr()
    assert seen == [7.5]


def test_the_mcp_tool_threads_max_seconds_to_pass_until(client, game, monkeypatch):
    """`mcp.pass_until`'s own `max_seconds` parameter has to actually reach
    `seatkit.pass_until` - the same spy shape as the `wait_for_priority`
    test above."""
    from magic_cards_model_api import seatkit as seatkit_module

    seen: list[float] = []
    real = seatkit_module.pass_until

    def spy(seat: Any, *, max_seconds: float = 0.0, **kwargs: Any) -> Any:
        seen.append(max_seconds)
        return real(seat, max_seconds=0.0, **kwargs)  # never actually wait in this test

    monkeypatch.setattr(seatkit_module, "pass_until", spy)
    seats, uids = wired_seats(monkeypatch, game)
    server = build_server(seats)

    call_mcp_tool(
        server,
        "pass_until",
        {"player_uid": uids["bob"], "step": "end_step", "max_seconds": 7.5},
    )
    assert seen == [7.5]


def test_seat_cli_pass_until_threads_max_seconds_to_pass_until(client, game, monkeypatch, capsys):
    """The same wiring, on the `seat_cli` transport - `pass-until
    --max-seconds`. Same reason as the `wait` test above for patching
    `seat_cli.pass_until` rather than `seatkit.pass_until`."""
    real = seat_cli.pass_until
    seen: list[float] = []

    def spy(seat: Any, *, max_seconds: float = 0.0, **kwargs: Any) -> Any:
        seen.append(max_seconds)
        return real(seat, max_seconds=0.0, **kwargs)

    monkeypatch.setattr(seat_cli, "pass_until", spy)
    seats, uids = wired_seats(monkeypatch, game)

    seat_cli.run(
        ["--uid", uids["bob"], "--game", game, "pass-until", "end_step", "--max-seconds", "7.5"]
    )
    capsys.readouterr()
    assert seen == [7.5]


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
        hold_priority=True,
    )
    dumped = str(result)
    assert "Grizzly Bears" not in dumped, "bob's hand leaked into the summary"
    assert "hands" not in result["available_actions"]
    assert "libraries" not in result["state"]
    assert result["state"]["hands"]["bob"] == []
    # bob's Mountain is public (battlefield), but its activatable mana ability
    # must not be offered to alice - only permanents *she* controls may be.
    activatable_ids = {
        entry["card_id"]
        for entry in result["available_actions"]["actions"]
        if entry["kind"] in ("activate", "activate_mana")
    }
    # `find()` returns the first name match, and alice has a Mountain of her
    # own on the battlefield too (`stock_a_bolt`) - filter on controller as
    # well, or this would silently compare against alice's own (by now
    # tapped, from paying for the bolt) Mountain instead of bob's untapped
    # one, which would pass for the wrong reason (tapped, not "not hers").
    bobs_mountain = next(
        card
        for card in state(client, game)["battlefield"]
        if card["name"] == "Mountain" and card["controller"] == "bob"
    )
    assert bobs_mountain["id"] not in activatable_ids


# -- both transports agree ----------------------------------------------------


def call_mcp_tool(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """A tool's answer as a plain dict either way: an ordinary text result is
    parsed as JSON, and an `InputRequiredResult` (a decision pending for the
    caller, T11) is unwrapped to the same shape via its own `request_state`,
    so a caller here need not branch on which wire shape it got back."""
    result = asyncio.run(server.call_tool(name, arguments))
    if isinstance(result, InputRequiredResult):
        assert result.request_state is not None
        return json.loads(result.request_state)
    return json.loads(result.content[0].text)


def test_seat_cli_and_mcp_tool_agree_on_the_same_flow(client, game, monkeypatch, capsys):
    """The whole "cast, then check" flow driven once through `seat_cli` and
    once through the MCP tool surface, on two independent games, proving
    they behave the same way (same shared `run_action`/`seatkit.
    block_until_wake`). Neither blocks any more (T11: poll-and-return), so
    this is two sequential calls each - cast, then check again once bob has
    actually passed - rather than two threads racing a blocked call."""
    seats, uids = wired_seats(monkeypatch, game)
    bolt_id, mountain_id = stock_a_bolt(client, game)

    # -- seat_cli path --
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
    first_printed = capsys.readouterr().out
    assert "nothing new yet" in first_printed, "nothing has happened yet - bob has not moved"

    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})

    seat_cli.run(["--uid", uids["alice"], "--game", game, "wait"])
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

        first = call_mcp_tool(
            server,
            "cast",
            {
                "player_uid": uids2["alice"],
                "card": bolt_id2,
                "targets": "bob",
                "mana_sources": mountain_id2,
            },
        )
        assert first["outcome"]["allowed"] is True
        assert first["wake_reason"] == "nothing new yet - call again to check"

        client.post(f"/api/games/{game2}/actions/pass-priority", json={"player": "bob"})

        result = call_mcp_tool(server, "wait_for_priority", {"player_uid": uids2["alice"]})
        assert "you have priority" in result["wake_reason"]
        assert life_of(result, "bob") == 17
    finally:
        client.delete(f"/api/games/{game2}")
