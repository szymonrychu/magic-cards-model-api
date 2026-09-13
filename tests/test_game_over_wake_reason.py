"""B8-07: a post-game refusal must not claim priority or a turn to take.

Battle 8's living-end vs dimir-frog: after `game_over`, `seatkit.run_action`
wrapped the server's own `type: game_over` 409 (already correct - see
`test_game_over_refusals.py`) in the same generic "ordinary refusal" shape
every other rules refusal gets: `held_priority: true` and `wake_reason:
"refused - nothing changed, still your move"`. That flatly contradicts "the
game is over" in the very same answer - there is no move left to hold, CR
104.1: "nothing more can be done in it."

Every case here plays a real game to a real, engine-decided ending (CR
104.3a's concede, the same route `test_game_over_refusals.py` uses) and then
drives the refused call through the real `Seat`/`seatkit.run_action` the way
`seat_cli.py`'s `pass`, `cast`/`activate`/etc. ("action routes") and `decide`
all do - never a hand-built outcome dict standing in for one.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_game_over_refusals import finish_with_a_spell_on_the_stack

from magic_cards_model_api import seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


def _seat(monkeypatch, game_id: str, player: str) -> seatkit.Seat:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=game_id)
    uid = {name: uid for uid, name in store.seat_uids(game_id).items()}[player]
    seat = seats.seat_for(uid)
    assert seat is not None
    return seat


def _assert_no_priority_or_turn_hints(result: dict[str, Any]) -> None:
    """The shared shape every one of these call kinds must land on: the
    outcome is still the server's own `type: game_over` 409, but the wrapper
    around it holds nothing and hints at no turn - never `held_priority:
    true`, never the "still your move" text, never any other wording that
    claims this seat has a move to make."""
    assert result["outcome"]["type"] == "game_over", result
    assert result["outcome"]["allowed"] is False, result
    assert result["held_priority"] is not True, result
    wake_reason = result["wake_reason"].lower()
    assert "still your move" not in wake_reason, result
    assert "nothing changed" not in wake_reason, result
    assert "the game is over" in wake_reason, result


def test_run_action_on_an_action_route_after_game_over(monkeypatch, client, game) -> None:
    """An action route (`cast`, `activate`, `play-land`, ...) is exercised
    through `pass-priority` here, the same route
    `test_a_pass_no_longer_resolves_the_spell_left_on_the_stack` proves is
    refused at the raw HTTP layer - this proves the client-side wrapper on
    top of that same refusal."""
    finish_with_a_spell_on_the_stack(client, game)
    seat = _seat(monkeypatch, game, "alice")

    def perform() -> Any:
        return seat.call_json(
            "POST", f"/api/games/{game}/actions/pass-priority", {"player": "alice"}
        )

    result = seatkit.run_action(seat, perform, hold_priority=False)

    _assert_no_priority_or_turn_hints(result)
    assert "pass_outcome" not in result, result


def test_run_action_as_pass_after_game_over(monkeypatch, client, game) -> None:
    """`seat_cli.py`'s own `pass` command: `run_action(..., release=False)`."""
    finish_with_a_spell_on_the_stack(client, game)
    seat = _seat(monkeypatch, game, "alice")

    def perform() -> Any:
        return seat.call_json(
            "POST", f"/api/games/{game}/actions/pass-priority", {"player": "alice"}
        )

    result = seatkit.run_action(seat, perform, hold_priority=False, release=False)

    _assert_no_priority_or_turn_hints(result)


def test_run_action_as_decide_after_game_over(monkeypatch, client, game) -> None:
    """`seat_cli.py`'s `decide` command shapes its own outcome
    (`{"allowed": False, **answer}` on `accepted is False`) before handing it
    to the identical `run_action`/`act()` flow every other command uses."""
    finish_with_a_spell_on_the_stack(client, game)
    seat = _seat(monkeypatch, game, "alice")

    def perform_decide() -> Any:
        answer = seat.call_json(
            "POST", f"/api/games/{game}/decide", {"player": "alice", "accept": True}
        )
        if isinstance(answer, dict) and answer.get("accepted") is False:
            return {"allowed": False, **answer}
        return {"allowed": True, **answer} if isinstance(answer, dict) else answer

    result = seatkit.run_action(seat, perform_decide, hold_priority=False)

    _assert_no_priority_or_turn_hints(result)


def test_a_dry_run_after_game_over_carries_no_priority_or_turn_hints(
    monkeypatch, client, game
) -> None:
    """A dry run never goes through `run_action` at all (`seat_cli.py`'s
    `act()` prints `perform()` verbatim for `dry_run=True`) - it is just the
    server's own `type: game_over` body. Locking that in here: it must never
    grow a `held_priority`/misleading `wake_reason` of its own either."""
    finish_with_a_spell_on_the_stack(client, game)
    seat = _seat(monkeypatch, game, "alice")

    outcome = seat.call_json(
        "POST",
        f"/api/games/{game}/actions/play-land",
        {"player": "alice", "dry_run": True},
    )

    assert outcome["type"] == "game_over", outcome
    assert outcome["allowed"] is False, outcome
    assert "held_priority" not in outcome, outcome
    assert "wake_reason" not in outcome, outcome
    assert "the game is over" in outcome["reason"], outcome
