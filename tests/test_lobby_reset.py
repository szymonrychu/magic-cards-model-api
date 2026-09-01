"""Starting a game from the lobby resets the previous one - `lobby.py`.

The bug: `POST /api/ui/games` only ever added. The previous lobby game stayed
in the store with its seat uids still live, its `ai_pregame.py` watcher thread
still polling (for up to an hour), and its AI seat still recorded in
`ai_seat.registry` - so the transcript of a session spanned two games with
nothing to say where one ended, and in wrapper mode the driver still held the
old assignment and refused to dispatch the new game's seat at all.

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
        "/api/ui/games",
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


def wait_until(predicate, *, max_seconds: float = 10.0, poll: float = 0.02) -> None:
    """Bounded: a regression fails this suite instead of hanging it."""
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(poll)
    assert predicate(), "condition never became true within the bound"


def test_a_new_lobby_game_deletes_the_previous_one(client: TestClient) -> None:
    first = boot(client, seed=1)
    second = boot(client, seed=2)
    try:
        assert client.get(f"/api/games/{first['game_id']}").status_code == 404
        assert client.get(f"/api/games/{second['game_id']}").status_code == 200
    finally:
        client.delete(f"/api/games/{second['game_id']}")


def test_the_old_seat_uid_stops_opening_anything(client: TestClient) -> None:
    """The uid the browser was holding is the credential the abandoned game
    was played with. After the reset it opens nothing - not its own game,
    which is gone, and not the new one, which never issued it."""
    first = boot(client, seed=1)
    old_uid = first["seat_uids"]["alice"]
    second = boot(client, seed=2)
    try:
        assert (
            client.get(f"/api/games/{first['game_id']}/seat", params={"uid": old_uid}).status_code
            == 404
        )
        assert (
            client.get(f"/api/games/{second['game_id']}/seat", params={"uid": old_uid}).status_code
            == 404
        )
        # And it cannot act on the new game either: a pregame declaration is
        # the first move a stale browser tab would make.
        refused = client.post(
            f"/api/games/{second['game_id']}/pregame/mulligan",
            params={"uid": old_uid},
            json={"player": "alice", "take": True},
        )
        assert refused.status_code == 400, refused.text
        assert "seat" in refused.text
    finally:
        client.delete(f"/api/games/{second['game_id']}")


def test_the_old_games_pregame_watcher_is_stopped(client: TestClient) -> None:
    """A watcher thread outlives its usefulness by up to `MAX_LIFETIME_SECONDS`
    on its own. The reset ends it, so nothing is left polling a game the human
    has walked away from."""
    first = boot(client, seed=1, ai=True)
    assert ai_pregame.is_watching(first["game_id"])
    threads = [t for t in threading.enumerate() if t.name == f"ai-pregame-{first['game_id']}"]
    assert threads, "the first game should have started a watcher thread"

    second = boot(client, seed=2, ai=True)
    try:
        assert not ai_pregame.is_watching(first["game_id"])
        wait_until(lambda: not any(t.is_alive() for t in threads))
        # The new game has its own watcher, and it is a different one.
        assert ai_pregame.is_watching(second["game_id"])
    finally:
        client.delete(f"/api/games/{second['game_id']}")
        ai_pregame.stop(second["game_id"])


def test_stopping_a_watcher_ends_its_thread_on_the_signal_itself(client: TestClient) -> None:
    """`ai_pregame.stop` has to end the thread on its own, not as a side
    effect of the game later disappearing from the store.

    Both happen during a reset, and leaning on the deletion would leave `stop`
    a no-op that only looked like it worked: the thread would keep polling
    until it happened to notice the game was gone, and a stop for any other
    reason would not end it at all. So this stops a watcher whose game is
    still very much alive.
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
        client.delete(f"/api/games/{booted['game_id']}")


def test_a_stale_watcher_cannot_settle_the_new_games_pregame(client: TestClient) -> None:
    """The reset happens before the new game exists, so the old watcher is
    already gone by the time there is anything of the new game's for it to
    act on - and the new game still owes both declarations to its own seats."""
    first = boot(client, seed=1, ai=True)
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
        client.delete(f"/api/games/{second['game_id']}")
        ai_pregame.stop(second["game_id"])


def test_the_reset_is_written_into_both_games_logs(client: TestClient) -> None:
    """A transcript never silently spans two games: the abandoned game's log
    ends with the reset and what it covered, and the new game's log opens by
    naming what it replaced."""
    first = boot(client, seed=1)
    second = boot(client, seed=2)
    try:
        old_log = audit.entries(first["game_id"])
        resets = [entry for entry in old_log if entry["action"] == "game_reset"]
        assert len(resets) == 1, [entry["action"] for entry in old_log]
        note = resets[0]["detail"]["note"]
        assert "lobby" in note
        assert "seat uid(s) invalidated" in note
        assert "memory volume is untouched" in note
        # It is the last thing in that game's transcript.
        assert old_log[-1]["action"] == "game_reset"

        new_log = audit.entries(second["game_id"])
        replaced = [entry for entry in new_log if entry["action"] == "reset_previous_games"]
        assert len(replaced) == 1
        assert replaced[0]["detail"]["games"] == [first["game_id"]]
    finally:
        client.delete(f"/api/games/{second['game_id']}")


def test_the_first_lobby_game_reports_that_it_replaced_nothing(client: TestClient) -> None:
    """ "Nothing was reset" is recorded too - its absence would be
    indistinguishable from a reset that never ran."""
    first = boot(client, seed=1)
    try:
        assert first["reset"] == []
        entries = [
            entry
            for entry in audit.entries(first["game_id"])
            if entry["action"] == "reset_previous_games"
        ]
        assert len(entries) == 1
        assert entries[0]["detail"]["games"] == []
    finally:
        client.delete(f"/api/games/{first['game_id']}")


def test_the_response_says_what_starting_this_game_reset(client: TestClient) -> None:
    first = boot(client, seed=1)
    second = boot(client, seed=2)
    try:
        (covered,) = second["reset"]
        assert covered["game_id"] == first["game_id"]
        assert covered["players"] == ["alice", "bob"]
        assert covered["seats_invalidated"] == 2
        assert covered["deleted"] is True
    finally:
        client.delete(f"/api/games/{second['game_id']}")


def test_the_old_subprocess_seat_is_terminated_and_forgotten(
    client: TestClient, monkeypatch
) -> None:
    """Subprocess mode. A real OS process holds the old seat - not the real
    `claude` (that costs money and time), but a real long-lived process
    spawned through the same code path, the `command_for` stand-in
    `test_ai_seat.py` established. Starting a new game kills it and drops
    every record of the uid, so no stale status can be polled back out."""
    monkeypatch.setenv("AI_SEAT_MODE", "subprocess")
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(
        ai_seat, "command_for", lambda prompt: ["python3", "-c", "import time; time.sleep(60)"]
    )
    first = boot(client, seed=1, ai=True)
    ai_uid = first["seat_uids"]["bob"]
    started = client.post(
        f"/api/ui/games/{first['game_id']}/ai-seat/start", json={"uid": ai_uid}
    ).json()
    assert started["mechanism"] == "subprocess"
    runner = ai_seat.registry._runners[ai_uid]
    wait_until(runner.running)

    second = boot(client, seed=2, ai=False)
    try:
        wait_until(lambda: not runner.running())
        assert ai_seat.registry.status(ai_uid) is None
        # And the reset says which seat it stopped.
        (covered,) = second["reset"]
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
    finally:
        client.delete(f"/api/games/{second['game_id']}")


def test_a_game_the_lobby_did_not_boot_is_left_alone(client: TestClient) -> None:
    """`POST /api/games` and `serve.py --match` boot games that belong to
    whoever booted them. A browser starting a game must never delete one."""
    other = client.post(
        "/api/games", json={"players": [{"username": "carol"}, {"username": "dave"}]}
    ).json()
    booted = boot(client, seed=1)
    try:
        assert client.get(f"/api/games/{other['game_id']}").status_code == 200
    finally:
        client.delete(f"/api/games/{other['game_id']}")
        client.delete(f"/api/games/{booted['game_id']}")


def test_a_rejected_decklist_does_not_reset_the_running_game(client: TestClient) -> None:
    """The reset runs after validation on purpose: a typo in the deck the
    human is about to play must not cost them the game they are still in."""
    first = boot(client, seed=1)
    try:
        refused = client.post(
            "/api/ui/games",
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
        client.delete(f"/api/games/{first['game_id']}")
        ai_pregame.stop(first["game_id"])
