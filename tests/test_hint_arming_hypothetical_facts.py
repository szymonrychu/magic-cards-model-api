"""The S1 hint and the S2 arming judge read hypothetical facts the way
`script_tools.listing` does (O7/D2).

A play whose selectors name `enters_any`/`unlocks`/`affordable` and the rest
(`conditions.needs_hypothetical`) can only be judged against the engine's own
facts. Before this, `applicable_hint` and `arming._judge_round` called
`_judged` with none, so `conditions.evaluate` raised `HypotheticalRequired`:
the hint silently dropped every such play (the whole hint, in fact) and the
arming judge raised out of the round. Now:

- the facts are fetched once per judgement, however many plays need them;
- a failed fetch marks only the plays that needed it not applicable, with
  why, and never breaks the answer;
- `armable`, which never evaluates a condition, never fetches them.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import add_card, force_turn_position
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_mcp_input_required import call_tool
from test_playscript_runtime import (
    _unlocks_land_script,
    land_script,
    register,
    seat_uid,
    wired,
)

from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.playscripts import arming, script_tools
from magic_cards_model_api.seatkit import HypotheticalFactsUnavailable, Seat


def counting_facts(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"n": 0}
    real = Seat.hypothetical_facts

    def counting(self: Seat) -> dict[str, Any]:
        calls["n"] += 1
        return real(self)

    monkeypatch.setattr(Seat, "hypothetical_facts", counting)
    return calls


def broken_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: Seat) -> dict[str, Any]:
        raise HypotheticalFactsUnavailable(404)

    monkeypatch.setattr(Seat, "hypothetical_facts", refuse)


def unlocks_hand(client: TestClient, game: str) -> None:
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")


def wait(monkeypatch: pytest.MonkeyPatch, game: str, uid: str) -> dict[str, Any]:
    server = build_server(wired(monkeypatch, game))
    result = call_tool(server, "wait_for_priority", {"player_uid": uid})
    return json.loads(result.content[0].text)


# -- the S1 hint --------------------------------------------------------------


def test_the_hint_judges_facts_needing_plays_with_one_fetch(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    alice = seat_uid(game, "alice")
    unlocks_hand(client, game)
    register(
        client,
        game,
        alice,
        _unlocks_land_script("drop-unlocks-a", priority=50),
        _unlocks_land_script("drop-unlocks-b", priority=40),
    )
    calls = counting_facts(monkeypatch)

    answer = wait(monkeypatch, game, alice)

    assert "scripts" in answer, answer
    ids = {entry["id"] for entry in answer["scripts"]["applicable"]}
    assert ids == {"drop-unlocks-a", "drop-unlocks-b"}, answer["scripts"]
    assert calls["n"] == 1, f"two facts-needing plays cost {calls['n']} facts fetches"


def test_a_failed_facts_fetch_drops_only_the_play_that_needed_it_from_the_hint(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    alice = seat_uid(game, "alice")
    unlocks_hand(client, game)
    register(
        client,
        game,
        alice,
        _unlocks_land_script("needs-facts"),
        land_script("plain-drop", priority=10),
    )
    broken_facts(monkeypatch)

    answer = wait(monkeypatch, game, alice)

    assert "scripts" in answer, answer
    [entry] = answer["scripts"]["applicable"]
    assert entry["id"] == "plain-drop"
    assert "call" in entry, entry


def test_armable_never_fetches_facts(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regression guard rather than a red test: a play whose `when` matches
    the board but whose seat does not hold priority is judged for `armable`
    only, which never evaluates its conditions, so it must never pay the
    facts fetch."""
    force_turn_position(game, Step.UPKEEP, "bob")
    alice = seat_uid(game, "alice")
    unlocks_hand(client, game)
    either_upkeep = {"whose_turn": "either", "steps": ["upkeep"], "stack": "empty"}
    register(client, game, alice, _unlocks_land_script("upkeep-unlocks", when=either_upkeep))
    seats = wired(monkeypatch, game)
    seat = seats.seat_for(alice)
    assert seat is not None
    state = {**seat.fetch_state(), "priority_holder": "bob"}
    calls = counting_facts(monkeypatch)

    hint = script_tools.applicable_hint(seat, seats.script_corpora, state)

    assert hint is not None
    assert [entry["id"] for entry in hint["armable"]] == ["upkeep-unlocks"], hint
    assert calls["n"] == 0, f"armable cost {calls['n']} facts fetches"


# -- the S2 arming judge ------------------------------------------------------


def test_the_arming_judge_fetches_facts_once_for_every_armed_play(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    alice = seat_uid(game, "alice")
    unlocks_hand(client, game)
    register(
        client,
        game,
        alice,
        _unlocks_land_script("drop-unlocks-a", priority=50),
        _unlocks_land_script("drop-unlocks-b", priority=40),
    )
    seats = wired(monkeypatch, game)
    seat = seats.seat_for(alice)
    assert seat is not None
    calls = counting_facts(monkeypatch)

    judged, _ = arming._judge_round(
        seat,
        seats.script_corpora,
        ["drop-unlocks-a", "drop-unlocks-b"],
        {"opposing_archetype": "*"},
    )

    assert judged["drop-unlocks-a"]["applies"] is True, judged
    assert judged["drop-unlocks-b"]["applies"] is True, judged
    assert calls["n"] == 1, f"two armed facts-needing plays cost {calls['n']} facts fetches"


def test_the_arming_judge_marks_only_the_play_needing_missing_facts(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    alice = seat_uid(game, "alice")
    unlocks_hand(client, game)
    register(
        client,
        game,
        alice,
        _unlocks_land_script("needs-facts"),
        land_script("plain-drop", priority=10),
    )
    seats = wired(monkeypatch, game)
    seat = seats.seat_for(alice)
    assert seat is not None
    broken_facts(monkeypatch)

    judged, _ = arming._judge_round(
        seat, seats.script_corpora, ["needs-facts", "plain-drop"], {"opposing_archetype": "*"}
    )

    affected = judged["needs-facts"]
    assert affected["applies"] is False
    assert affected["failing_condition"]["list"] == "hypothetical"
    assert "404" in affected["failing_condition"]["seen"]
    assert judged["plain-drop"]["applies"] is True, judged
