"""End game is the only reset there is - `lobby.py`.

Games the lobby boots live side by side: starting a second one from the lobby
never touches the first (that used to be the bug - `POST /api/lobby/games` reset
the previous lobby game out from under a human still playing it, its seat uids
dying, its `ai_pregame.py` watcher stopped, its AI seat released - so a
transcript spanned two games with nothing to say where one ended, and in
wrapper mode the driver still held the old assignment and refused to dispatch
the new game's seat at all).

Now the only way any of that happens is `DELETE /api/lobby/games/{id}` (End
game), triggered by a human, not by starting another game.

Every game here is a real game booted through the real endpoint the browser
posts to, from decklists made of the real Scryfall printings the fixture cache
pins (Mountain, Forest, Grizzly Bears, Lightning Bolt - basics are exempt from
the four-copy limit, so sixty legal cards are buildable from just those). No
mocks: the watcher threads are real threads and the store is the real store.

The wrapper-mode half of the reset - the driver being handed its agent back -
is covered against the real driver app in
`tests/driver/test_lobby_reset_releases_the_agent.py`.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from fastapi.testclient import TestClient

from magic_cards_model_api import ai_pregame, ai_seat, audit

LEGAL_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
LEGAL_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"


def boot(client: TestClient, *, seed: int, ai: bool = True) -> dict[str, Any]:
    """One game from the lobby, exactly as the browser boots it."""
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": LEGAL_DECK, "ai": False},
                {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": ai},
            ],
            "seed": seed,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def end(client: TestClient, booted: dict[str, Any]) -> dict[str, Any]:
    """End a lobby game exactly as the browser's End game button does."""
    response = client.delete(
        f"/api/lobby/games/{booted['game_id']}", params={"uid": booted["seat_uids"]["alice"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def wait_until(predicate, *, max_seconds: float = 10.0, poll: float = 0.02) -> None:
    """Bounded: a regression fails this suite instead of hanging it."""
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(poll)
    assert predicate(), "condition never became true within the bound"


def test_a_second_lobby_game_leaves_the_first_alone(client: TestClient) -> None:
    first = boot(client, seed=1)
    second = boot(client, seed=2)
    try:
        assert client.get(f"/api/games/{first['game_id']}").status_code == 200
        assert client.get(f"/api/games/{second['game_id']}").status_code == 200
    finally:
        client.delete(f"/api/games/{first['game_id']}", params={"key": first["admin_key"]})
        client.delete(f"/api/games/{second['game_id']}", params={"key": second["admin_key"]})


def test_end_game_deletes_it_and_its_seat_uids_stop_opening_anything(client: TestClient) -> None:
    """The uid the browser was holding is the credential the game was played
    with. Once it is ended, the uid opens nothing - not its own game, which
    is gone, and not some other, still-live game either, since it was never
    that game's uid to begin with."""
    first = boot(client, seed=1)
    second = boot(client, seed=2)  # stays live throughout - not ended here
    old_uid = first["seat_uids"]["alice"]
    end(client, first)
    try:
        assert client.get(f"/api/games/{first['game_id']}").status_code == 404
        assert (
            client.get(f"/api/games/{first['game_id']}/seat", params={"uid": old_uid}).status_code
            == 404
        )
        assert (
            client.get(f"/api/games/{second['game_id']}/seat", params={"uid": old_uid}).status_code
            == 404
        )
        # And it cannot act on the still-live game either: a pregame
        # declaration is the first move a stale browser tab would make.
        #
        # This used to be `_pregame_seat`'s own 400 ("that uid does not open
        # alice's seat"), reached because the gate let any caller through to
        # an ownerless game. `game_access.py`'s positive rule refuses a
        # mutating call whose credential opens no seat of *this* game before
        # the route runs, so the answer is now the module's standard
        # refusal: a 404 identical to the one a game that never existed
        # produces. That is this repo's own deliberate choice (see
        # `game_access.py`'s docstring - a caller must not be able to tell
        # "not yours" from "does not exist"), and it is a strictly stronger
        # answer to the question this test asks.
        refused = client.post(
            f"/api/games/{second['game_id']}/pregame/mulligan",
            params={"uid": old_uid},
            json={"player": "alice", "take": True},
        )
        assert refused.status_code == 404, refused.text
        assert refused.json()["detail"] == f"no game {second['game_id']}", refused.text
    finally:
        client.delete(f"/api/games/{second['game_id']}", params={"key": second["admin_key"]})


def test_ending_a_game_stops_its_pregame_watcher(client: TestClient) -> None:
    """A watcher thread outlives its usefulness by up to `MAX_LIFETIME_SECONDS`
    on its own. Ending the game ends it, so nothing is left polling a game the
    human has walked away from."""
    booted = boot(client, seed=1, ai=True)
    assert ai_pregame.is_watching(booted["game_id"])
    threads = [t for t in threading.enumerate() if t.name == f"ai-pregame-{booted['game_id']}"]
    assert threads, "a game with a bot seat should have started a watcher thread"

    end(client, booted)
    assert not ai_pregame.is_watching(booted["game_id"])
    wait_until(lambda: not any(t.is_alive() for t in threads))


def test_stopping_a_watcher_ends_its_thread_on_the_signal_itself(client: TestClient) -> None:
    """`ai_pregame.stop` has to end the thread on its own, not as a side
    effect of the game later disappearing from the store.

    Both happen when a game is ended, and leaning on the deletion would leave
    `stop` a no-op that only looked like it worked: the thread would keep
    polling until it happened to notice the game was gone, and a stop for any
    other reason would not end it at all. So this stops a watcher whose game
    is still very much alive.
    """
    booted = boot(client, seed=1, ai=True)
    threads = [t for t in threading.enumerate() if t.name == f"ai-pregame-{booted['game_id']}"]
    assert threads, "a game with a bot seat should have started a watcher thread"
    try:
        assert ai_pregame.stop(booted["game_id"]) is True
        wait_until(lambda: not any(t.is_alive() for t in threads))
        assert not ai_pregame.is_watching(booted["game_id"])
        # The game itself is untouched by stopping its watcher.
        assert client.get(f"/api/games/{booted['game_id']}").status_code == 200
    finally:
        client.delete(f"/api/games/{booted['game_id']}", params={"key": booted["admin_key"]})


def test_a_stale_watcher_cannot_settle_the_new_games_pregame(client: TestClient) -> None:
    """Ending a game happens strictly before whatever game a human starts
    next, so the old watcher is already gone by the time there is anything of
    a new game for it to act on - and the new game still owes both
    declarations to its own seats."""
    first = boot(client, seed=1, ai=True)
    end(client, first)
    second = boot(client, seed=2, ai=True)
    try:
        wait_until(lambda: not ai_pregame.is_watching(first["game_id"]))
        state = client.get(
            f"/api/games/{second['game_id']}", params={"uid": second["seat_uids"]["alice"]}
        ).json()
        assert state["pregame"]["awaiting_mulligan"] == ["alice", "bob"]
        automatic = [
            entry
            for entry in audit.entries(second["game_id"])
            if entry["detail"].get("automatic") or entry["action"] == "pregame_overruled"
        ]
        assert automatic == []
    finally:
        client.delete(f"/api/games/{second['game_id']}", params={"key": second["admin_key"]})
        ai_pregame.stop(second["game_id"])


def test_the_reset_is_written_into_the_ended_games_log(client: TestClient) -> None:
    """A transcript never silently trails off: the ended game's log ends with
    the reset and what it covered, naming why."""
    booted = boot(client, seed=1)
    end(client, booted)
    log = audit.entries(booted["game_id"])
    resets = [entry for entry in log if entry["action"] == "game_reset"]
    assert len(resets) == 1, [entry["action"] for entry in log]
    detail = resets[0]["detail"]
    assert detail["reason"] == "ended from the lobby"
    note = detail["note"]
    assert "ended from the lobby" in note
    assert "seat uid(s) invalidated" in note
    assert "memory volume is untouched" in note
    # It is the last thing in that game's transcript.
    assert log[-1]["action"] == "game_reset"


def test_end_game_reports_what_it_covered(client: TestClient) -> None:
    booted = boot(client, seed=1)
    covered = end(client, booted)
    assert covered["game_id"] == booted["game_id"]
    assert covered["players"] == ["alice", "bob"]
    assert covered["seats_invalidated"] == 2
    assert covered["deleted"] is True


def test_the_subprocess_seat_is_terminated_and_forgotten_when_ended(
    client: TestClient, monkeypatch
) -> None:
    """Subprocess mode. A real OS process holds the seat - not the real
    `claude` (that costs money and time), but a real long-lived process
    spawned through the same code path, the `command_for` stand-in
    `test_ai_seat.py` established. Ending the game kills it and drops every
    record of the uid, so no stale status can be polled back out."""
    monkeypatch.setenv("AI_SEAT_MODE", "subprocess")
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(
        ai_seat,
        "command_for",
        lambda prompt, agent: ["python3", "-c", "import time; time.sleep(60)"],
    )
    booted = boot(client, seed=1, ai=True)
    ai_uid = booted["seat_uids"]["bob"]
    started = client.post(
        f"/api/lobby/games/{booted['game_id']}/ai-seat/start", json={"uid": ai_uid}
    ).json()
    assert started["mechanism"] == "subprocess"
    runner = ai_seat.registry._runners[ai_uid]
    wait_until(runner.running)

    covered = end(client, booted)
    wait_until(lambda: not runner.running())
    assert ai_seat.registry.status(ai_uid) is None
    # And the reset says which seat it stopped.
    assert covered["ai_seats_released"] == [
        {
            "uid": ai_uid,
            "mechanism": "subprocess",
            "subprocess_stopped": True,
            "driver": None,
            "was_unavailable": None,
            "player": "bob",
        }
    ]


def test_a_game_the_lobby_did_not_boot_is_left_alone(client: TestClient) -> None:
    """`POST /api/games` and `serve.py --match` boot games that belong to
    whoever booted them. Booting a lobby game, and ending one, must never
    touch a game the lobby did not boot."""
    other = client.post(
        "/api/games", json={"players": [{"username": "carol"}, {"username": "dave"}]}
    ).json()
    booted = boot(client, seed=1)
    try:
        assert client.get(f"/api/games/{other['game_id']}").status_code == 200
        end(client, booted)
        assert client.get(f"/api/games/{other['game_id']}").status_code == 200
    finally:
        client.delete(f"/api/games/{other['game_id']}", params={"key": other["admin_key"]})


def test_a_rejected_decklist_does_not_touch_the_running_game(client: TestClient) -> None:
    """A typo in a second game's deck must not cost the human their first,
    still-running game - there is no implicit reset to trip over any more,
    but this still guards that a failed boot leaves everything untouched."""
    first = boot(client, seed=1)
    try:
        refused = client.post(
            "/api/lobby/games",
            json={
                "players": [
                    {"username": "alice", "decklist_text": "4 Mountain", "ai": False},
                    {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": True},
                ]
            },
        )
        assert refused.status_code == 400
        assert client.get(f"/api/games/{first['game_id']}").status_code == 200
        assert ai_pregame.is_watching(first["game_id"])
    finally:
        client.delete(f"/api/games/{first['game_id']}", params={"key": first["admin_key"]})
        ai_pregame.stop(first["game_id"])
