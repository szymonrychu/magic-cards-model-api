"""Server-side answers for decisions with exactly one legal answer
(`forced_decisions`).

An AI seat used to spend a whole model inference on a declare-attackers pause
with nothing able to attack, or a trigger with one legal target. The harness
now answers those itself - but only when the engine's own enumeration (the
`legal-actions` rows `seatkit.available_actions` returns while a decision
stands) proves there is exactly one legal answer. Every board below is a real
board played through the real API with real Scryfall printings; the few
pure-function cases start from a shape captured off one of those boards and
change only the field the case is about.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import httpx
import pytest
from conftest import add_card, state
from fastapi.testclient import TestClient
from test_autopass import a_bear_that_can_attack, set_step

from magic_cards_model_api import forced_decisions, seatkit
from magic_cards_model_api.forced_decisions import (
    AUTO_REASONING_PREFIX,
    auto_answer_forced,
    forced_answer,
    post_forced_answer,
)
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


def seat_of(monkeypatch: pytest.MonkeyPatch, game_id: str, player: str) -> seatkit.Seat:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    uids = {name: uid for uid, name in store.seat_uids(game_id).items()}
    seat = Seats(api="http://testserver", game_id=game_id).seat_for(uids[player])
    assert seat is not None
    return seat


def pending_and_view(seat: seatkit.Seat) -> tuple[dict[str, Any], dict[str, Any], dict]:
    board = seat.fetch_state()
    pending = board["pending_decision"]
    assert pending is not None, board
    return pending, seatkit.available_actions(seat, board), board


def pause_on_declare_attackers(client: TestClient, game_id: str) -> None:
    set_step(client, game_id, "beginning_of_combat")
    client.post(f"/api/games/{game_id}/advance", json={"player": "alice"})


def cast_devourer(client: TestClient, game_id: str, colored_targets: int) -> list[str]:
    """alice casts Devourer of Destiny; its cast trigger ("exile target
    permanent that's one or more colors") pauses on `choose_targets`. The
    only colored permanents are bob's Grizzly Bears, `colored_targets` of
    them, so that is exactly the candidate count. Returns the bears' ids."""
    add_card(client, game_id, "Sol Ring", "alice", "battlefield")
    for _ in range(5):
        add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Devourer of Destiny", "alice", "hand")
    for _ in range(colored_targets):
        add_card(client, game_id, "Grizzly Bears", "bob", "battlefield")
    board = state(client, game_id)
    sources = [c["id"] for c in board["battlefield"] if c["name"] in ("Sol Ring", "Mountain")]
    cast = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": board["hands"]["alice"][0]["id"],
            "mana_sources": sources,
        },
    )
    assert cast.json().get("paused") is True, cast.json()
    return [c["id"] for c in board["battlefield"] if c["name"] == "Grizzly Bears"]


def play_steam_vents(client: TestClient, game_id: str, *, life: int) -> None:
    client.patch(f"/api/games/{game_id}/players/alice", json={"life": life})
    add_card(client, game_id, "Steam Vents", "alice", "hand")
    land = state(client, game_id)["hands"]["alice"][0]
    played = client.post(
        f"/api/games/{game_id}/actions/play-land",
        json={"player": "alice", "card_id": land["id"]},
    )
    assert played.json().get("paused") is True, played.json()


def logged_reasonings(client: TestClient, game_id: str, uid: str) -> list[str]:
    """The reasoning on each ledger row, as the acting seat itself reads its
    own log (an opponent or a uid-less read gets it redacted)."""
    log = client.get(f"/api/games/{game_id}/log", params={"uid": uid}).json()["log"]
    return [str(entry.get("reasoning")) for entry in log if entry.get("reasoning")]


# -- (a)/(b) combat declarations ----------------------------------------------


def test_declare_attackers_with_nothing_able_to_attack_is_forced_nobody(
    client, game, monkeypatch, caplog
):
    # A summoning-sick creature: the engine offers no attacker pair at all.
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    pause_on_declare_attackers(client, game)
    alice = seat_of(monkeypatch, game, "alice")
    pending, view, _ = pending_and_view(alice)
    assert pending["kind"] == "declare_attackers"

    answer = forced_answer(pending, view)
    assert answer == {"player": "alice", "pairs": [], "generation": pending["generation"]}

    with caplog.at_level(logging.INFO, logger=forced_decisions.logger.name):
        outcome = post_forced_answer(alice, pending, answer, "no creature can attack")
    assert outcome["accepted"] is True, outcome
    after = alice.fetch_state()
    assert (after.get("pending_decision") or {}).get("kind") != "declare_attackers"
    assert any(
        text.startswith(AUTO_REASONING_PREFIX) and "no creature can attack" in text
        for text in logged_reasonings(client, game, alice.uid)
    ), logged_reasonings(client, game, alice.uid)


def test_declare_attackers_with_a_creature_able_to_attack_is_not_forced(client, game, monkeypatch):
    a_bear_that_can_attack(client, game, "alice")
    pause_on_declare_attackers(client, game)
    pending, view, _ = pending_and_view(seat_of(monkeypatch, game, "alice"))
    assert pending["kind"] == "declare_attackers"
    assert forced_answer(pending, view) is None


# -- (c)/(d) targets ----------------------------------------------------------


def test_a_trigger_with_one_legal_target_is_forced(client, game, monkeypatch):
    (bears,) = cast_devourer(client, game, colored_targets=1)
    alice = seat_of(monkeypatch, game, "alice")
    pending, view, _ = pending_and_view(alice)
    assert pending["kind"] == "choose_targets"

    answer = forced_answer(pending, view)
    assert answer == {"player": "alice", "targets": [bears], "generation": pending["generation"]}
    outcome = post_forced_answer(alice, pending, answer, "one legal target")
    assert outcome["accepted"] is True, outcome
    assert any(AUTO_REASONING_PREFIX in text for text in logged_reasonings(client, game, alice.uid))


def test_a_trigger_with_two_legal_targets_is_not_forced(client, game, monkeypatch):
    cast_devourer(client, game, colored_targets=2)
    pending, view, _ = pending_and_view(seat_of(monkeypatch, game, "alice"))
    assert pending["kind"] == "choose_targets"
    assert forced_answer(pending, view) is None


def test_an_up_to_target_with_one_candidate_is_not_forced(client, game, monkeypatch):
    """The real one-candidate shape, with the floor lowered to 0 the way an
    "up to one target" requirement carries it: choosing nothing is legal too,
    so there are two answers and nothing is forced."""
    cast_devourer(client, game, colored_targets=1)
    pending, view, _ = pending_and_view(seat_of(monkeypatch, game, "alice"))
    optional_pending = {**pending, "target_minimum": 0}
    optional_view = copy.deepcopy(view)
    optional_view["pending_decision"] = optional_pending
    optional_view["actions"][0]["x_range"] = [0, 1]
    assert forced_answer(optional_pending, optional_view) is None


# -- (e) yes/no ---------------------------------------------------------------


def test_pay_life_that_cannot_be_paid_is_forced_no(client, game, monkeypatch):
    play_steam_vents(client, game, life=1)
    alice = seat_of(monkeypatch, game, "alice")
    pending, view, _ = pending_and_view(alice)
    assert pending["kind"] == "pay_life" and pending["affirmative_is_legal"] is False

    answer = forced_answer(pending, view)
    assert answer == {"player": "alice", "accept": False, "generation": pending["generation"]}
    outcome = post_forced_answer(alice, pending, answer, "cannot pay the life")
    assert outcome["accepted"] is True, outcome


def test_pay_life_that_can_be_paid_is_not_forced(client, game, monkeypatch):
    play_steam_vents(client, game, life=20)
    pending, view, _ = pending_and_view(seat_of(monkeypatch, game, "alice"))
    assert pending["kind"] == "pay_life"
    assert forced_answer(pending, view) is None


# -- (f) a failed enumeration -------------------------------------------------


def test_an_actions_error_is_never_forced(client, game, monkeypatch):
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    pause_on_declare_attackers(client, game)
    pending, view, board = pending_and_view(seat_of(monkeypatch, game, "alice"))
    assert forced_answer(pending, view) is not None, "the control: this board is forced"
    failed = seatkit._actions_unavailable(board, "the legal-actions call answered HTTP 500")
    assert forced_answer(pending, failed) is None
    assert forced_answer(pending, {**view, "actions_error": "boom"}) is None


# -- trigger ordering and single-option choices -------------------------------


def test_ordering_two_triggers_is_not_forced(client, game, monkeypatch):
    add_card(client, game, "Delver of Secrets", "alice", "battlefield")
    add_card(client, game, "Delver of Secrets", "alice", "battlefield")
    client.put(f"/api/games/{game}/step", json={"step": "untap", "player": "alice"})
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    alice = seat_of(monkeypatch, game, "alice")
    pending, view, _ = pending_and_view(alice)
    assert pending["kind"] == "order_triggers"
    assert forced_answer(pending, view) is None

    # One ability left to order (the real shape, one id dropped): forced.
    only = pending["cards"][0]
    single_pending = {**pending, "cards": [only]}
    single_view = copy.deepcopy(view)
    single_view["pending_decision"] = single_pending
    single_view["actions"][0]["decision_candidates"] = [only]
    assert forced_answer(single_pending, single_view) == {
        "player": "alice",
        "order": [only],
        "generation": pending["generation"],
    }


def _option_rows(template: dict[str, Any], options: list[str]) -> list[dict[str, Any]]:
    return [{**template, "decision_candidates": [], "decision_option": o} for o in options]


def test_a_mode_choice_with_one_option_is_forced_and_two_are_not(client, game, monkeypatch):
    """Built on a real enumerated row (the pay_life board's), re-keyed to a
    `choose_a_mode` question the way `decision_answers` enumerates one: one
    `answer_decision` row per offered option."""
    play_steam_vents(client, game, life=20)
    pending, view, _ = pending_and_view(seat_of(monkeypatch, game, "alice"))
    mode_pending = {**pending, "kind": "choose_a_mode", "options": ["draw a card"]}
    one = {**view, "pending_decision": mode_pending}
    one["actions"] = _option_rows(view["actions"][0], ["draw a card"])
    assert forced_answer(mode_pending, one) == {
        "player": "alice",
        "choice": "draw a card",
        "generation": pending["generation"],
    }
    two = {**one, "actions": _option_rows(view["actions"][0], ["draw a card", "gain 3 life"])}
    assert forced_answer({**mode_pending, "options": ["draw a card", "gain 3 life"]}, two) is None


def test_unknown_kinds_are_never_forced(client, game, monkeypatch):
    play_steam_vents(client, game, life=1)
    pending, view, _ = pending_and_view(seat_of(monkeypatch, game, "alice"))
    strange = {**pending, "kind": "some_future_kind"}
    assert forced_answer(strange, {**view, "pending_decision": strange}) is None


# -- (g) the loop --------------------------------------------------------------


def test_auto_answer_forced_answers_then_stops(client, game, monkeypatch, caplog):
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    pause_on_declare_attackers(client, game)
    alice = seat_of(monkeypatch, game, "alice")
    with caplog.at_level(logging.INFO, logger=forced_decisions.logger.name):
        latest, entries = auto_answer_forced(alice, alice.fetch_state())
    assert [entry["kind"] for entry in entries] == ["declare_attackers"]
    assert entries[0]["answer"] == {"pairs": []}
    assert entries[0]["why"]
    assert (latest.get("pending_decision") or {}).get("kind") != "declare_attackers"
    assert any(
        "auto-answered forced decision" in r.getMessage()
        and f"seat={alice.uid[:8]}" in r.getMessage()
        and "kind=declare_attackers" in r.getMessage()
        for r in caplog.records
    )


def test_auto_answer_forced_stops_at_a_decision_that_is_not_forced(client, game, monkeypatch):
    cast_devourer(client, game, colored_targets=2)
    alice = seat_of(monkeypatch, game, "alice")
    before = alice.fetch_state()
    latest, entries = auto_answer_forced(alice, before)
    assert entries == []
    assert latest["pending_decision"]["kind"] == "choose_targets"


def test_auto_answer_forced_leaves_a_decision_addressed_to_someone_else(client, game, monkeypatch):
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    pause_on_declare_attackers(client, game)
    bob = seat_of(monkeypatch, game, "bob")
    latest, entries = auto_answer_forced(bob, bob.fetch_state())
    assert entries == []
    assert latest["pending_decision"]["kind"] == "declare_attackers"


def test_auto_answer_forced_never_raises_on_an_http_error(client, game, monkeypatch):
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    pause_on_declare_attackers(client, game)
    alice = seat_of(monkeypatch, game, "alice")
    board = alice.fetch_state()

    real_request = alice.http.request

    def posts_unreachable(method: str, *a: Any, **k: Any) -> Any:
        if method.upper() == "POST":
            raise httpx.ConnectError("the api went away")
        return real_request(method, *a, **k)

    monkeypatch.setattr(alice.http, "request", posts_unreachable)
    latest, entries = auto_answer_forced(alice, board)
    assert latest is board
    assert len(entries) == 1 and "the api went away" in entries[0]["outcome"]


def test_auto_answer_forced_records_a_refusal_and_stops(client, game, monkeypatch):
    """A stale read: the pause was answered by someone else between the read
    and the post, so the named generation no longer stands."""
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    pause_on_declare_attackers(client, game)
    alice = seat_of(monkeypatch, game, "alice")
    board = alice.fetch_state()
    stale = copy.deepcopy(board)
    stale["pending_decision"]["generation"] = board["pending_decision"]["generation"] - 1
    real_available = seatkit.available_actions

    def with_stale_pending(seat: Any, _state: dict) -> dict:
        view = real_available(seat, board)
        view["pending_decision"] = stale["pending_decision"]
        return view

    monkeypatch.setattr(seatkit, "available_actions", with_stale_pending)
    _latest, entries = auto_answer_forced(alice, stale)
    assert len(entries) == 1
    assert entries[0]["refused"], entries[0]


def test_the_reasoning_stays_short(client, game, monkeypatch):
    play_steam_vents(client, game, life=1)
    alice = seat_of(monkeypatch, game, "alice")
    pending, view, _ = pending_and_view(alice)
    captured: dict[str, Any] = {}

    def capture(method: str, path: str, body: dict | None = None, **_k: Any) -> dict:
        captured.update(body or {})
        return {"accepted": True}

    monkeypatch.setattr(alice, "call_json", capture)
    post_forced_answer(alice, pending, forced_answer(pending, view), "x" * 1000)
    assert captured["reasoning"].startswith(AUTO_REASONING_PREFIX)
    assert len(captured["reasoning"]) <= forced_decisions.REASONING_MAX_CHARS
