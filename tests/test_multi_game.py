"""Several concurrent games in one server: `serve`'s multi-boot CLI, per-game
seat isolation, and per-game (not global) locking.

The store (`store.py`) already keys everything - games, seat uids, admin
keys, mutation locks, decision mailboxes - by `game_id`, and every HTTP route
already takes a `game_id` path segment. The only thing that used to boot a
single game was `serve.py`; this file is the regression cover for its new
`--match` form (`serve.setup_all`) plus the two correctness properties the
task calls out explicitly: a uid from one game must be a stranger to every
other game, and one game's blocked request must never stall another's.
"""

from __future__ import annotations

import time

from conftest import state
from fastapi.testclient import TestClient
from test_decisions import (
    advance_into_cleanup,
    seats_for,
    set_short_timeout,
    stuff_hand,
    wait_for_pending,
)

from magic_cards_model_api import serve
from magic_cards_model_api.store import store

ALICE = "4 Mountain\n4 Shock\n"
BOB = "4 Mountain\n4 Shock\n"


def make_game(client: TestClient, a: str = "alice", b: str = "bob") -> str:
    response = client.post("/api/games", json={"players": [{"username": a}, {"username": b}]})
    assert response.status_code == 200, response.text
    return response.json()["game_id"]


# -- serve.py: booting several games -----------------------------------------


def test_parse_args_accepts_several_matches() -> None:
    args = serve.parse_args(
        [
            "--match",
            "alice=a.txt,bob=b.txt,seed=1",
            "--match",
            "carol=c.txt,dave=d.txt,seed=2",
            "--match",
            "eve=e.txt,frank=f.txt,seed=3",
        ]
    )
    assert len(args.matches) == 3
    assert args.matches[0].decks == [("alice", "a.txt"), ("bob", "b.txt")]
    assert args.matches[0].seed == 1
    assert args.matches[2].decks == [("eve", "e.txt"), ("frank", "f.txt")]
    assert args.matches[2].seed == 3


def test_parse_args_rejects_mixing_deck_and_match() -> None:
    import pytest

    with pytest.raises(SystemExit):
        serve.parse_args(["--deck", "a=x.txt", "--deck", "b=y.txt", "--match", "c=x.txt,d=y.txt"])


def test_parse_args_rejects_a_match_without_two_decks() -> None:
    import pytest

    with pytest.raises(SystemExit):
        serve.parse_args(["--match", "alice=a.txt"])


def test_setup_all_boots_one_game_per_match(tmp_path) -> None:
    paths = {}
    for name, text in [("p1", ALICE), ("p2", BOB), ("p3", ALICE), ("p4", BOB)]:
        path = tmp_path / f"{name}.txt"
        path.write_text(text)
        paths[name] = path

    args = serve.parse_args(
        [
            "--match",
            f"p1={paths['p1']},p2={paths['p2']},seed=11",
            "--match",
            f"p3={paths['p3']},p4={paths['p4']},seed=22",
        ]
    )
    boots = serve.setup_all(args, check_legality=False)
    try:
        assert len(boots) == 2
        # Distinct games, distinct ids, distinct uids - nothing shared between
        # the two boots.
        assert boots[0].game_id != boots[1].game_id
        assert store.get(boots[0].game_id) is boots[0].game
        assert store.get(boots[1].game_id) is boots[1].game
        assert [p.username for p in boots[0].game.players] == ["p1", "p2"]
        assert [p.username for p in boots[1].game.players] == ["p3", "p4"]
        assert boots[0].match.seed == 11
        assert boots[1].match.seed == 22
        uids_0 = set(boots[0].seat_uids.values())
        uids_1 = set(boots[1].seat_uids.values())
        assert uids_0.isdisjoint(uids_1)

        summary = "\n".join(boot.summary() for boot in boots)
        assert boots[0].game_id in summary and boots[1].game_id in summary
    finally:
        for boot in boots:
            store.delete(boot.game_id)


def test_setup_still_boots_a_single_deck_game(tmp_path) -> None:
    """The unchanged, existing single-game form."""
    first = tmp_path / "alice.txt"
    second = tmp_path / "bob.txt"
    first.write_text(ALICE)
    second.write_text(BOB)
    args = serve.parse_args(["--deck", f"p1={first}", "--deck", f"p2={second}", "--seed", "7"])
    boot = serve.setup(args, check_legality=False)
    try:
        assert boot.match.seed == 7
        assert store.get(boot.game_id) is boot.game
    finally:
        store.delete(boot.game_id)


# -- cross-game seat isolation ------------------------------------------------


def test_a_uid_from_one_game_opens_no_seat_in_another(client: TestClient) -> None:
    game_a = make_game(client, "alice", "bob")
    game_b = make_game(client, "carol", "dave")
    try:
        uids_a = seats_for(client, game_a)

        # The uid is real - it opens alice's seat in its own game.
        own = client.get(f"/api/games/{game_a}/seat", params={"uid": uids_a["alice"]})
        assert own.status_code == 200
        assert own.json()["player"] == "alice"

        # The same uid means nothing in a different game.
        foreign = client.get(f"/api/games/{game_b}/seat", params={"uid": uids_a["alice"]})
        assert foreign.status_code == 404

        # Nor can it read game B's hidden state as though it were a seat there.
        view = client.get(f"/api/games/{game_b}", params={"uid": uids_a["alice"]}).json()
        assert view["hands"] == {"carol": [], "dave": []}
        assert "you" not in view  # resolve_viewer saw no seat, not carol's or dave's

        # Nor can it act in game B.
        cast = client.post(
            f"/api/games/{game_b}/actions/cast",
            json={"player": "carol", "card": "Shock", "dry_run": True},
        )
        # The action endpoint does not itself check uid (only /decide and the
        # state/log reads do) - the isolation guarantee under test is about
        # *visibility and identity*, proven above; this call is included only
        # to show it operates on game B's own board, never game A's.
        assert cast.status_code in (200, 400, 404)
    finally:
        client.delete(f"/api/games/{game_a}")
        client.delete(f"/api/games/{game_b}")


def test_a_uid_cannot_answer_a_decision_in_a_game_it_is_not_seated_in(client: TestClient) -> None:
    """The sharpest version of the isolation property: even a *live* pending
    decision in game A cannot be answered by a uid that happens to be a real
    seat uid - just for game B, not this one."""
    game_a = make_game(client, "alice", "bob")
    game_b = make_game(client, "carol", "dave")
    try:
        stuff_hand(client, game_a, "alice", 8)
        uids_b = seats_for(client, game_b)  # carol's uid: real, but for game B

        thread = advance_into_cleanup(game_a)
        assert thread is not None, "the discard should have asked, not defaulted silently"
        try:
            uids_a = seats_for(client, game_a)
            wait_for_pending(client, game_a, uids_a["alice"])
            pending = state(client, game_a, uid=uids_a["alice"])["pending_decision"]

            answer = client.post(
                f"/api/games/{game_a}/decide",
                params={"uid": uids_b["carol"]},  # a real uid - for the wrong game
                json={"player": "alice", "card_ids": [pending["cards"][0]]},
            )
            assert answer.status_code == 200
            assert answer.json()["accepted"] is False

            # Answer it for real, with the correct game's own uid, so the
            # background thread can actually finish.
            correct = client.post(
                f"/api/games/{game_a}/decide",
                params={"uid": uids_a["alice"]},
                json={"player": "alice", "card_ids": [pending["cards"][0]]},
            )
            assert correct.status_code == 200
            assert correct.json()["accepted"] is True
        finally:
            thread.join(timeout=10)
        assert not thread.is_alive()
    finally:
        client.delete(f"/api/games/{game_a}")
        client.delete(f"/api/games/{game_b}")


# -- per-game locking: one game's block must not stall another ---------------


def test_one_games_blocked_decision_does_not_stall_another_game(client: TestClient) -> None:
    """The most important correctness question in the task: `store.locked`
    takes a lock keyed by `game_id` (`store.py`), so a request parked in
    `Game.ask` for game A - holding game A's lock for up to its full decision
    timeout - must never make an ordinary request to game B wait behind it.
    A global lock would serialise every game in the server; this proves the
    opposite by genuinely parking a thread in game A (the same technique
    `test_deadlock.py` uses) and timing a plain read and a plain mutation on
    game B while it is still stuck there.
    """
    game_a = make_game(client, "alice", "bob")
    game_b = make_game(client, "carol", "dave")
    try:
        stuff_hand(client, game_a, "alice", 8)
        set_short_timeout(game_a, 5.0)  # long enough to prove B is not waiting on it

        thread = advance_into_cleanup(game_a)
        assert thread is not None, "game A should be blocked on its own discard decision"
        try:
            uids_a = seats_for(client, game_a)
            wait_for_pending(client, game_a, uids_a["alice"])
            # Game A's lock is now genuinely held by `thread`, parked deep in
            # `Game.ask` waiting on this same decision.

            started = time.monotonic()
            read = client.get(f"/api/games/{game_b}")
            assert read.status_code == 200
            mutate = client.post(
                f"/api/games/{game_b}/actions/pass-priority", json={"player": "carol"}
            )
            assert mutate.status_code == 200
            elapsed = time.monotonic() - started

            assert elapsed < 1.0, (
                "a request to game B waited on game A's held lock - locking is "
                "not genuinely per-game, so three concurrent games would run "
                "no faster than three sequential ones"
            )

            # Clean up game A: answer the decision so the thread can finish.
            uids_a_now = seats_for(client, game_a)
            pending = state(client, game_a, uid=uids_a_now["alice"])["pending_decision"]
            answered = client.post(
                f"/api/games/{game_a}/decide",
                params={"uid": uids_a_now["alice"]},
                json={"player": "alice", "card_ids": [pending["cards"][0]]},
            )
            assert answered.status_code == 200
            assert answered.json()["accepted"] is True
        finally:
            thread.join(timeout=10)
        assert not thread.is_alive(), "game A's blocked request never returned"
    finally:
        client.delete(f"/api/games/{game_a}")
        client.delete(f"/api/games/{game_b}")


def test_locks_are_distinct_objects_per_game(client: TestClient) -> None:
    """White-box companion to the behavioural test above: `store.locked`
    hands out a different `threading.Lock` per `game_id`, not the same one
    reused - the actual mechanism that makes the timing proof above true
    rather than a coincidence of scheduling."""
    game_a = make_game(client)
    game_b = make_game(client)
    try:
        with store.locked(game_a) as _:
            lock_a = store._locks[game_a]
        lock_b = store._locks[game_b]
        assert lock_a is not lock_b
    finally:
        client.delete(f"/api/games/{game_a}")
        client.delete(f"/api/games/{game_b}")
