"""D2: a failed hypothetical-facts fetch must never be mistaken for a real,
empty answer, and must never crash a caller either.

`seatkit.Seat.hypothetical_facts()` used to call `response.json()` with no
status check at all: a 404 error body ("no such seat") turned into a plain
dict with no "cards" key, which `runtime._HypotheticalCache.get` and
`script_tools.listing` both read as `{}` - a `count {select: {unlocks:
true}} == 0` condition then genuinely holds, silently skipping whatever
`prefer` tier depended on it, indistinguishable from a board with nothing
that unlocks anything. And a 500 with a non-JSON body raised a bare
`JSONDecodeError` straight out of `Seat.hypothetical_facts()`, uncaught by
`mcp._guarded` (which only catches `httpx.HTTPError`) - `run_script` left
its already-recorded run open, and `list_scripts` failed outright for every
script on the board, not only the ones that needed the missing facts.

This file proves the fix at all three levels the finding named: a real 404
(`Seat.hypothetical_facts` itself), a simulated 500 with a non-JSON body
(`Seat.hypothetical_facts` again, since a live server never actually answers
this way), and `script_tools.listing` marking only the affected entries
rather than failing the whole listing. A `run_script` test proves the
runtime's own ScriptExit conversion, the third piece the fix touches.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from conftest import add_card
from fastapi.testclient import TestClient
from test_playscript_runtime import (
    REASONING,
    _unlocks_land_script,
    hooked_seat,
    land_script,
    register,
    seat_uid,
    wired,
)

from magic_cards_model_api import seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.playscripts import script_tools
from magic_cards_model_api.playscripts.runtime import Exit, run_script
from magic_cards_model_api.seatkit import HypotheticalFactsUnavailable, Seat


class _NonJsonResponse:
    """Stands in for a 500 whose body is not JSON at all - a real live
    server never answers this way (every route here always answers JSON,
    error bodies included), so this is the one case in this file a hook
    simulates rather than triggers for real."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = "<html>internal server error</html>"

    def json(self) -> Any:
        raise json.JSONDecodeError("Expecting value", self.text, 0)


def _broken_hypothetical_facts(status: int) -> Any:
    def answer(method: str, path: str, body: Any) -> Any:
        if method == "GET" and path.endswith("/playscripts/hypothetical-facts"):
            return _NonJsonResponse(status)
        return None

    return answer


# -- Seat.hypothetical_facts itself ------------------------------------------


def test_a_real_404_raises_the_typed_error(client: TestClient, game: str, monkeypatch) -> None:
    """A genuinely bad uid: the real server's own 404, not a simulated one."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seat = Seat("alice", "not-a-real-uid", "http://testserver", game)

    with pytest.raises(HypotheticalFactsUnavailable) as excinfo:
        seat.hypothetical_facts()
    assert excinfo.value.status == 404
    assert "404" in str(excinfo.value)


def test_a_500_with_a_non_json_body_raises_the_typed_error(
    client: TestClient, game: str, monkeypatch
) -> None:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = seatkit.Seats(api="http://testserver", game_id=game)
    alice = seat_uid(game, "alice")
    seat, _ = hooked_seat(seats, alice, answer=_broken_hypothetical_facts(500))

    with pytest.raises(HypotheticalFactsUnavailable) as excinfo:
        seat.hypothetical_facts()
    assert excinfo.value.status == 500


# -- run_script: turned into a ScriptExit, the run still ends ----------------


def test_run_script_exits_unforeseen_when_facts_are_unavailable(
    client: TestClient, game: str, monkeypatch
) -> None:
    from conftest import force_turn_position
    from magic_cards_model.machines import Step

    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    alice = seat_uid(game, "alice")
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    script = _unlocks_land_script("drop-unlocks")
    register(client, game, alice, script)
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, alice, answer=_broken_hypothetical_facts(500))

    result = run_script(
        seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert "hypothetical facts unavailable" in (result.script_exit.stop_reason or "")
    assert "500" in (result.script_exit.stop_reason or "")
    # the run's own start was already recorded before the fetch failed - the
    # finish must still be posted, not left open.
    assert hook.posts("/script-runs"), "the run must be recorded, not left open"
    finishes = [body for body in hook.posts("/script-runs") if body.get("phase") == "finish"]
    assert finishes, "a finish must be posted even though facts were unavailable"


# -- listing: only the affected entries, never the whole listing ------------


def test_listing_marks_only_entries_that_need_the_missing_facts(
    client: TestClient, game: str, monkeypatch
) -> None:
    from conftest import force_turn_position
    from magic_cards_model.machines import Step

    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    alice = seat_uid(game, "alice")
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    needs_facts = _unlocks_land_script("needs-facts")
    plain = land_script("plain-drop", priority=10)
    register(client, game, alice, needs_facts, plain)
    seats = wired(monkeypatch, game)
    seat, _ = hooked_seat(seats, alice, answer=_broken_hypothetical_facts(404))

    result = script_tools.listing(seat, seats.script_corpora)

    assert isinstance(result, dict), result
    entries = {entry["id"]: entry for entry in result["scripts"]}
    affected = entries["needs-facts"]
    assert affected["applies"] is False
    assert affected["failing_condition"]["list"] == "hypothetical"
    assert "404" in affected["failing_condition"]["seen"]
    # the other script needed none of this and is judged normally.
    unaffected = entries["plain-drop"]
    assert unaffected["applies"] is True
    assert unaffected["failing_condition"] is None
