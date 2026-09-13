"""Battles 3 and 4: what the ledger and a state read say about a game.

- **B4-09** - `exile_pile.turn_exiled` was `null` for an imprinted card
  (Ugin's Labyrinth) and a hidden-away one (Mosswort Bridge), while every other
  marked exile carried its turn. CR 406.4 keeps face-down exiled cards "in
  separate piles based on when they were exiled and how they were exiled".
- **B3-15** - a cast row's `from_zone` said `hand` for a card cast from exile
  or a graveyard: the request's default was logged instead of where the card
  was. CR 601.2a: the player "moves that card ... from where it is to the
  stack".
- **B3-11** - a pass that ended a step was labelled with the step that
  followed it, and an attack declaration answered into a pause with the step
  before it. The label is the step the action was taken in.
- **B3-08** - a read during a pause showed the committed board's step,
  priority holder and counters: Urza's Saga lore counters and `draw` while the
  precombat main phase's chapter triggers were being ordered (CR 714.3c puts
  the counter on "as the precombat main phase begins").
- **B3-09** - `blockers` auto-released priority the defending seat did not
  hold, posting a refused pass into the audit log (CR 509.1: declaring
  blockers is a turn-based action; the active player then receives priority,
  CR 117.3a).
- **B3-10** - `wake_reason` told the seat whose pass raised a decision for
  the *other* seat that "a decision is waiting for you".

Every CR number was read from the Comprehensive Rules text in the session
that wrote this. Every card is a real Scryfall printing played through the api.
"""

from __future__ import annotations

import json

import httpx
from conftest import add_card, find, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_autopass import a_bear_that_can_attack, set_step, stock_libraries

from magic_cards_model_api import seat_cli
from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def uids(game: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game).items()}


def log_rows(client, game: str, action: str) -> list[dict]:
    return [e for e in client.get(f"/api/games/{game}/log").json()["log"] if e["action"] == action]


def answer_everything(client, game: str, player: str) -> None:
    """Answer every question `player` is asked, the plain way: accept a may,
    take the first card offered, keep the offered trigger order."""
    for _ in range(6):
        view = state(client, game, uid=uids(game)[player])
        pending = view["pending_decision"]
        if pending is None or pending["player"] != player:
            return
        body: dict = {"player": player, "generation": pending.get("generation")}
        if pending["kind"] == "may":
            body["accept"] = True
        elif pending["kind"] == "choose_cards":
            body["card_ids"] = pending["cards"][:1]
        elif pending["kind"] == "order_triggers":
            body["order"] = pending["cards"]
        else:
            raise AssertionError(f"unexpected decision {pending}")
        answer = client.post(
            f"/api/games/{game}/decide", params={"uid": uids(game)[player]}, json=body
        ).json()
        assert answer["accepted"] is True, answer


# -- B4-09: turn_exiled for imprint and hideaway -------------------------------


def test_an_imprinted_card_records_the_turn_it_was_exiled(client, game):
    add_card(client, game, "Ugin's Labyrinth", "alice", "hand")
    add_card(client, game, "Emrakul, the Promised End", "alice", "hand")
    labyrinth = find(state(client, game)["hands"]["alice"], "Ugin's Labyrinth")["id"]
    played = client.post(
        f"/api/games/{game}/actions/play-land", json={"player": "alice", "card_id": labyrinth}
    ).json()
    assert played["allowed"] is True, played
    client.post(f"/api/games/{game}/resolve-top")
    answer_everything(client, game, "alice")

    board = state(client, game)
    (pile,) = [card["exile_pile"] for card in board["exile_pile"]]
    assert pile["marker"] == "imprinted", pile
    assert pile["turn_exiled"] == board["turn_number"], pile


def test_a_hidden_away_card_records_the_turn_it_was_exiled(client, game):
    stock_libraries(client, game)
    add_card(client, game, "Mosswort Bridge", "alice", "hand")
    bridge = find(state(client, game)["hands"]["alice"], "Mosswort Bridge")["id"]
    played = client.post(
        f"/api/games/{game}/actions/play-land", json={"player": "alice", "card_id": bridge}
    ).json()
    assert played["allowed"] is True, played
    client.post(f"/api/games/{game}/resolve-top")
    answer_everything(client, game, "alice")

    board = state(client, game)
    (pile,) = [card["exile_pile"] for card in board["exile_pile"]]
    assert pile["marker"] == "hideaway", pile
    assert pile["turn_exiled"] == board["turn_number"], pile


# -- B3-15: the zone a card was cast from --------------------------------------


def test_a_flashback_cast_logs_the_graveyard_it_was_cast_from(client, game):
    for land in ("Plains", "Island", "Island"):
        add_card(client, game, land, "alice", "battlefield")
    add_card(client, game, "Faithful Mending", "alice", "graveyard")
    board = state(client, game)
    mending = find(board["graveyards"]["alice"], "Faithful Mending")["id"]
    lands = [card["id"] for card in board["battlefield"]]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": mending,
            "keyword": "Flashback",
            "mana_sources": lands,
        },
    ).json()
    assert cast["allowed"] is True, cast
    (row,) = log_rows(client, game, "cast")
    assert row["detail"]["from_zone"] == "graveyard", row["detail"]


def test_a_permission_cast_logs_the_exile_it_was_cast_from(client, game):
    """`cast_origin_zone` reads the card's real zone right before the cast
    (B3-15: a permission cast used to log `from_zone: hand`, its request's
    own unhelpful default) rather than branching on how the cast was
    permitted - a plotted card driven through the ordinary `actions/cast`
    exercises the identical `locate_castable(...).zone` read a suspended
    card's now-inline free cast (`Game.finish_suspend`, cast_during_
    resolution, battle 8) no longer reaches through this route at all."""
    add_card(client, game, "Slickshot Show-Off", "alice", "hand")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    mountains = [card["id"] for card in state(client, game)["battlefield"]]
    client.post(
        f"/api/games/{game}/actions/plot",
        json={"player": "alice", "card": "Slickshot Show-Off", "mana_sources": mountains},
    )
    store.get(game).turn.turn_number += 1  # CR 702.170d: castable on a later turn
    exiled = find(state(client, game)["exile"], "Slickshot Show-Off")["id"]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": exiled, "from_zone": "exile", "keyword": "Plot"},
    ).json()
    assert cast["allowed"] is True, cast
    (row,) = log_rows(client, game, "cast")
    assert row["detail"]["from_zone"] == "exile", row["detail"]


def test_a_hand_cast_still_logs_the_hand(client, game):
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    board = state(client, game)
    bears = find(board["hands"]["alice"], "Grizzly Bears")["id"]
    forests = [card["id"] for card in board["battlefield"]]
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": bears, "mana_sources": forests},
    ).json()
    assert cast["allowed"] is True, cast
    (row,) = log_rows(client, game, "cast")
    assert row["detail"]["from_zone"] == "hand"


# -- B3-11: a row is labelled with the step it was taken in --------------------


def test_a_pass_that_ends_a_step_is_labelled_with_the_step_it_ended(client, game):
    assert state(client, game)["step"] == "precombat_main"
    for player in ("alice", "bob"):
        passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": player})
        assert passed.json()["allowed"] is True, passed.json()
    assert state(client, game)["step"] != "precombat_main"

    rows = log_rows(client, game, "pass_priority")
    assert [row["step"] for row in rows] == ["precombat_main", "precombat_main"], rows
    assert [row["phase"] for row in rows] == ["precombat_main", "precombat_main"]


def two_goblin_guides_attack(client, game) -> dict:
    """Two creatures whose "Whenever this creature attacks" triggers need
    ordering (CR 603.3b), so the declaration's resume pauses again."""
    stock_libraries(client, game)
    first = a_bear_that_can_attack(client, game, "alice")
    add_card(client, game, "Goblin Guide", "alice", "battlefield")
    add_card(client, game, "Goblin Guide", "alice", "battlefield")
    guides = [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Goblin Guide"]
    set_step(client, game, "beginning_of_combat")
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    pending = state(client, game)["pending_decision"]
    assert pending is not None and pending["kind"] == "declare_attackers", pending
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids(game)["alice"]},
        json={"attackers": [{"card_id": guide, "defender": "bob"} for guide in guides]},
    ).json()
    assert attack["allowed"] is True, attack
    return {"bear": first, "guides": guides, "attack": attack}


def test_an_attack_declaration_is_labelled_with_declare_attackers(client, game):
    staged = two_goblin_guides_attack(client, game)
    assert staged["attack"].get("paused") is True, staged["attack"]

    (row,) = log_rows(client, game, "attack")
    assert row["step"] == "declare_attackers", row


# -- B3-08: a read during a pause is the paused board --------------------------


def sagas_paused_on_their_chapter_triggers(client, game) -> list[str]:
    stock_libraries(client, game)
    add_card(client, game, "Urza's Saga", "alice", "battlefield")
    add_card(client, game, "Urza's Saga", "alice", "battlefield")
    sagas = [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Urza's Saga"]
    for saga in sagas:
        patched = client.patch(f"/api/games/{game}/cards/{saga}", json={"counters": {"lore": 1}})
        assert patched.status_code == 200, patched.text
    force_turn_position(game, Step.DRAW, "alice")
    alice = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    assert alice.json()["allowed"] is True, alice.json()
    bob = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"}).json()
    assert bob.get("paused") is True, bob
    return sagas


def test_a_read_during_a_pause_shows_the_paused_step_priority_and_counters(client, game):
    sagas = sagas_paused_on_their_chapter_triggers(client, game)

    for viewer in (uids(game)["alice"], uids(game)["bob"], None):
        view = state(client, game, uid=viewer)
        assert view["pending_decision"]["kind"] == "order_triggers", view["pending_decision"]
        assert view["step"] == "precombat_main", view["step"]
        assert view["phase"] == "precombat_main"
        assert view["priority_holder"] != "bob", view["priority_holder"]
        lore = [
            next(card for card in view["battlefield"] if card["id"] == saga)["counters"]
            for saga in sagas
        ]
        assert lore == [{"lore": 2}, {"lore": 2}], lore


def test_right_after_the_pause_is_answered_the_read_is_the_committed_board(client, game):
    sagas_paused_on_their_chapter_triggers(client, game)
    answer_everything(client, game, "alice")

    view = state(client, game)
    assert view["pending_decision"] is None
    assert view["step"] == "precombat_main"
    assert view["priority_holder"] == "alice"


# -- B3-09: blockers never posts a refused pass --------------------------------


def test_declaring_blockers_through_the_cli_posts_no_refused_pass(
    client, game, monkeypatch, capsys
):
    stock_libraries(client, game)
    bear = a_bear_that_can_attack(client, game, "alice")
    add_card(client, game, "Watchwolf", "bob", "battlefield")
    wolf = find(state(client, game)["battlefield"], "Watchwolf")["id"]
    set_step(client, game, "beginning_of_combat")
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids(game)["alice"]},
        json={"attackers": [{"card_id": bear, "defender": "bob"}]},
    ).json()
    assert attack["allowed"] is True, attack
    for player in ("alice", "bob"):
        client.post(f"/api/games/{game}/actions/pass-priority", json={"player": player})
    pending = state(client, game)["pending_decision"]
    assert pending is not None and pending["kind"] == "declare_blockers", pending

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    rc = seat_cli.run(
        [
            "--uid",
            uids(game)["bob"],
            "--game",
            game,
            "blockers",
            json.dumps([{"blocker": wolf, "blocked": bear}]),
        ]
    )
    printed = capsys.readouterr().out
    assert rc == 0, printed
    refused = [
        row
        for row in log_rows(client, game, "pass_priority")
        if row["detail"].get("player") == "bob" and row["detail"].get("allowed") is False
    ]
    assert refused == [], refused
    assert "does not have priority" not in printed, printed


# -- B3-10: wake_reason names whose decision it is ------------------------------


def test_a_pass_that_raises_the_other_seats_decision_does_not_wake_this_seat_for_it(
    client, game, monkeypatch, capsys
):
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Galvanic Discharge", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    board = state(client, game)
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": find(board["hands"]["alice"], "Galvanic Discharge")["id"],
            "targets": [find(board["battlefield"], "Grizzly Bears")["id"]],
            "mana_sources": [find(board["battlefield"], "Mountain")["id"]],
        },
    ).json()
    assert cast["allowed"] is True, cast
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    rc = seat_cli.run(["--uid", uids(game)["bob"], "--game", game, "pass"])
    printed = capsys.readouterr().out
    assert rc == 0, printed
    answer = json.loads(printed)
    assert state(client, game)["pending_decision"]["player"] == "alice"
    assert "waiting for you" not in answer["wake_reason"], answer["wake_reason"]
    assert "alice" in answer["wake_reason"], answer["wake_reason"]
    assert answer.get("in_flight_action") is not True, answer
