"""Conceding a game ends it - the pregame watcher with it, not just the board.

**A3 changed what a natural ending releases.** It used to release every
runner too, which sent the AI seat's driver down its no-reflection handover
and filed its review against a game the seat was no longer playing (live
game 23868a95). A natural ending now stops the pregame watcher and nothing
else; the AI seat and any buddy finish their post-game work on their own
uids and release themselves, with End game and a bounded backstop behind
them (`tests/test_post_game_lifecycle.py`). The history below is the
original task's, kept because the watcher half of it still holds.

CR 104.3a ("A player can concede the game at any time. A player who concedes
leaves the game immediately. That player loses the game.") and CR 104.2a (the
last player left in a two-or-more-player game wins) are the engine's half, and
the engine already does both: `Game.concede` appends to `conceded`, drops the
player out of `players_in_game`, and `_check_for_a_winner` names the survivor.
`app._record_game_over` then writes the ledger's one `game_over` row.

What none of that did was end the *game* in the sense the owner meant. A
lobby-booted game keeps three live things beside the board - the `ai_pregame`
watcher thread, whatever the `ai_seat` registry is holding for each seat uid,
and the same for every buddy - and until this task the only thing that ever
released them was a human pressing **End game** (`lobby.reset_game`, reached
from `DELETE /api/lobby/games/{id}` and `DELETE /api/games/{id}` and nowhere
else). So a concede left a watcher polling a finished game, an AI seat's
process or Job still running, and - in `wrapper` mode - the driver still
assigned, which is the leak `lobby.py` already calls permanent: it 409s every
later dispatch for the life of that container.

The fix hangs off `app._record_game_over`, which is the one place both
`_run_command` and `_resume` funnel a committed ending through, so it covers
every ending and not only the reported one. `test_a_lethal_damage_ending_
retires_the_runners_too` is the proof of that generality, on a path the
reported bug never touched.

What it deliberately does **not** do is delete or soft-delete the game. A
finished game stays readable and stays refusing (`commands.GameOverError`,
`tests/test_game_over_refusals.py`), its review stays reachable, and End game
remains the one thing that clears the row - see `test_the_game_itself_is_still
_there_to_read_after_a_concede`.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
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


def concede(client: TestClient, booted: dict[str, Any], player: str = "alice") -> dict[str, Any]:
    """Concede exactly as the play UI's two-click Concede button does."""
    response = client.post(
        f"/api/games/{booted['game_id']}/actions/concede",
        params={"uid": booted["seat_uids"][player]},
        json={"player": player},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture()
def subprocess_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Subprocess mode, the way `test_lobby_reset.py` does it: a real OS
    process spawned through the real code path, without paying for a real
    `claude`."""
    monkeypatch.setenv("AI_SEAT_MODE", "subprocess")
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(
        ai_seat,
        "command_for",
        lambda prompt, agent: ["python3", "-c", "import time; time.sleep(60)"],
    )


def wait_until(predicate, *, max_seconds: float = 10.0, poll: float = 0.02) -> None:
    """Bounded: a regression fails this suite instead of hanging it."""
    import time

    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(poll)
    assert predicate(), "condition never became true within the bound"


def test_a_concede_ends_the_game_with_a_winner(client: TestClient) -> None:
    """The headline. CR 104.3a's concession leaves one player in a two-player
    game, and CR 104.2a hands that player the win - readable through the board
    and through the ledger's own `game_over` row, the same two surfaces a game
    that ended any other way is read through."""
    booted = boot(client, seed=11, ai=False)
    assert concede(client, booted)["winner"] == "bob"

    board = client.get(
        f"/api/games/{booted['game_id']}", params={"uid": booted["seat_uids"]["alice"]}
    ).json()
    assert board["winner"] == "bob"
    assert board["drawn"] is False
    assert board["conceded"] == ["alice"]

    endings = [
        entry
        for entry in audit.entries(booted["game_id"])
        if entry["action"] == audit.GAME_OVER_ACTION
    ]
    assert len(endings) == 1, [entry["action"] for entry in audit.entries(booted["game_id"])]
    assert endings[0]["detail"]["winner"] == "bob"


def test_a_concede_stops_the_pregame_watcher(client: TestClient) -> None:
    """The watcher exists to settle a bot seat's pregame obligation. A
    conceded game has no obligation left to settle, and leaving the thread
    polling one is exactly the leak End game was written to stop."""
    booted = boot(client, seed=12, ai=True)
    assert ai_pregame.is_watching(booted["game_id"])
    threads = [t for t in threading.enumerate() if t.name == f"ai-pregame-{booted['game_id']}"]
    assert threads, "a game with a bot seat should have started a watcher thread"

    concede(client, booted)

    assert not ai_pregame.is_watching(booted["game_id"])
    wait_until(lambda: not any(t.is_alive() for t in threads))


def test_a_concede_does_not_release_the_ai_seat(client: TestClient, subprocess_seat: None) -> None:
    """A3 inverted this. The AI seat's post-game work - the reflection turn,
    the Opus review, the handover - runs on the seat's own uid after the
    game is over, so a concession leaves the runner exactly where it was.
    The pod releases itself once that work is done; End game and the
    bounded backstop (`test_post_game_lifecycle.py`) are the only things
    that release it from here."""
    booted = boot(client, seed=13, ai=True)
    ai_uid = booted["seat_uids"]["bob"]
    started = client.post(
        f"/api/lobby/games/{booted['game_id']}/ai-seat/start", json={"uid": ai_uid}
    ).json()
    assert started["mechanism"] == "subprocess"
    runner = ai_seat.registry._runners[ai_uid]
    wait_until(runner.running)

    concede(client, booted)

    assert runner.running()
    assert ai_seat.registry.status(ai_uid) is not None
    runner.stop()


def test_a_concede_writes_the_retirement_into_the_ended_games_log(client: TestClient) -> None:
    """A transcript never silently trails off (`lobby.py`). The retirement is
    a row of its own, after the ending it followed, naming what it covered -
    and it is written once, however many further calls the finished game
    refuses."""
    booted = boot(client, seed=14, ai=True)
    concede(client, booted)
    client.post(f"/api/games/{booted['game_id']}/actions/pass-priority", json={"player": "bob"})

    log = audit.entries(booted["game_id"])
    actions = [entry["action"] for entry in log]
    assert actions.count(audit.GAME_OVER_ACTION) == 1, actions
    assert actions.count("game_retired") == 1, actions
    assert actions.index(audit.GAME_OVER_ACTION) < actions.index("game_retired"), actions
    detail = log[actions.index("game_retired")]["detail"]
    assert detail["reason"] == "the game is over"
    assert detail["ai_seats_released"] == []
    assert "pregame watcher stopped" in detail["note"]


def test_the_game_itself_is_still_there_to_read_after_a_concede(client: TestClient) -> None:
    """Retiring the runners is not deleting the game. The board, the seat
    uids and the post-game review all still read, and every further action is
    still refused with CR 104.1's own `game_over` rather than a 404 - End game
    stays the one thing that clears the row."""
    booted = boot(client, seed=15, ai=True)
    uid = booted["seat_uids"]["alice"]
    concede(client, booted)

    assert client.get(f"/api/games/{booted['game_id']}", params={"uid": uid}).status_code == 200
    refused = client.post(
        f"/api/games/{booted['game_id']}/actions/pass-priority",
        params={"uid": uid},
        json={"player": "bob"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"] == "game_over", refused.text
    review = client.get(f"/api/games/{booted['game_id']}/review", params={"uid": uid})
    assert review.status_code == 200, review.text

    listed = client.get("/api/lobby/games", params={"uids": uid}).json()["games"]
    row = next(entry for entry in listed if entry["game_id"] == booted["game_id"])
    assert row["is_over"] is True


def test_a_lethal_damage_ending_retires_the_runners_too(client: TestClient) -> None:
    """Concession is the ending that was reported, never the specification.
    The retirement hangs off `_record_game_over`, which every committed
    ending funnels through, so an ending nothing in the report touched -
    CR 104.3b's zero life, swept by a state-based-action check - retires the
    same runners."""
    booted = boot(client, seed=16, ai=True)
    game_id = booted["game_id"]
    assert ai_pregame.is_watching(game_id)

    assert client.patch(f"/api/games/{game_id}/players/bob", json={"life": 0}).status_code == 200
    swept = client.post(f"/api/games/{game_id}/state-based-actions")
    assert swept.status_code == 200, swept.text
    assert swept.json()["winner"] == "alice"

    assert not ai_pregame.is_watching(game_id)
    assert "game_retired" in [entry["action"] for entry in audit.entries(game_id)]
