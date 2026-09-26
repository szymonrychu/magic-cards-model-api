"""FD5: the audit ledger carries every field a decision-DSL converter needs,
without ever having to read the *post*-action board.

- (1) `cast`/`activate` audit rows carry every field their own request shape
  can announce (`mana_sources` alone excepted - it stays out on purpose).
- (2) every row's `active_player`/`priority_holder` are the *pre*-action
  board's own, and each row references the pre-action frame it was taken
  against - the previous row's own `seq` (`pre_action_frame`), or, for a
  game's very first row, an attached `pre_state` snapshot.
- (3) a paused action is a *committing* row (`allowed: True, paused: True`),
  not one a DSL converter skips as noise.
- (4) `mulligan_declared`/`opening_hand_action`/`opening_hand_done` are
  committing rows too.
"""

from __future__ import annotations

from uuid import UUID

from conftest import add_card, state
from test_activate_sacrifice_cost import seats_for, stock_bombardment_board
from test_autopass import set_step, stock_libraries
from test_mulligans import boot, declare, finish_opening_hand

from magic_cards_model_api.store import store


def log_of(client, game: str) -> list[dict]:
    response = client.get(f"/api/games/{game}/log")
    assert response.status_code == 200, response.text
    return response.json()["log"]


def entry_of(log: list[dict], action: str, *, player: str | None = None) -> dict:
    matches = [
        entry
        for entry in log
        if entry["action"] == action and (player is None or entry["detail"].get("player") == player)
    ]
    assert matches, f"no {action} in {[entry['action'] for entry in log]}"
    return matches[-1]


# -- (1) every CastRequest/ActivateRequest field is logged ------------------


def test_a_cast_logs_every_announced_field(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = state(client, game)["hands"]["alice"][0]
    mountain = next(c for c in state(client, game)["battlefield"] if c["name"] == "Mountain")

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt["id"],
            "targets": ["bob"],
            "mana_sources": [mountain["id"]],
        },
    )
    assert cast.json()["allowed"] is True, cast.json()

    entry = entry_of(log_of(client, game), "cast")
    detail = entry["detail"]
    for field in (
        "keyword",
        "alternative_cost",
        "modes",
        "x_value",
        "shares",
        "optional_costs",
        "convoke",
        "delve",
        "improvise",
        "choosing",
        "sacrificing",
        "discarding",
        "exiling",
        "tapping",
    ):
        assert field in detail, (field, detail)
    # mana_sources stays too - a pre-existing field this task does not touch.
    assert detail["mana_sources"] == [mountain["id"]]


def test_an_activation_logs_every_announced_field(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    mountain = next(c for c in state(client, game)["battlefield"] if c["name"] == "Mountain")
    activate = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": mountain["id"]},
    )
    assert activate.json()["allowed"] is True, activate.json()

    entry = entry_of(log_of(client, game), "activate")
    detail = entry["detail"]
    for field in ("sacrificing", "discarding", "exiling", "x_value", "color"):
        assert field in detail, (field, detail)


# -- (2) pre-action active_player/priority_holder and pre_action_frame ------


def test_a_pass_that_ends_the_turn_logs_the_pre_action_active_player(client, game):
    stock_libraries(client, game)
    set_step(client, game, "end_step")
    first = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    assert first.json()["allowed"] is True, first.json()
    second = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert second.json()["allowed"] is True, second.json()

    log = log_of(client, game)
    bobs_pass = entry_of(log, "pass_priority", player="bob")
    # bob passed during alice's turn - the row must say so, not "bob",
    # even though the turn has moved on to bob's own by the time this row's
    # post-action board is read.
    assert bobs_pass["active_player"] == "alice", bobs_pass
    assert bobs_pass["priority_holder"] == "bob", bobs_pass


def test_a_cast_references_the_frame_in_which_the_card_was_in_hand(client, game):
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    bear = state(client, game)["hands"]["alice"][0]
    forests = [c["id"] for c in state(client, game)["battlefield"]]
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": bear["id"], "mana_sources": forests},
    )
    assert cast.json()["allowed"] is True, cast.json()

    entry = entry_of(log_of(client, game), "cast")
    pre_seq = entry["pre_action_frame"]
    assert pre_seq is not None, entry
    assert entry["pre_state"] is None, entry

    pre_game = store.record_state(game, pre_seq)
    pre_bear = pre_game.find_by_id(UUID(bear["id"]))
    assert pre_bear is not None
    assert pre_bear in pre_game.player("alice").hand


def test_the_first_row_a_game_ever_gets_carries_a_pre_state_not_a_frame(client, game):
    """Unit-level: `note_action_board`/`audit.record` fall back to an
    attached `pre_state` snapshot, computed off the real pre-action board,
    when there is no earlier row's `seq` to reference at all.

    A real, fully-built `Game` (borrowed from the `game` fixture) is noted
    against a *fresh* game id this store has never seen a row for - so
    `store.max_record_seq` on that id is `0` and the fallback fires, without
    this test having to hand-build a bare `Game` itself."""
    from magic_cards_model_api import audit, records

    game_obj = store.get(game)
    assert game_obj is not None
    fresh_id = f"{game}-fd5-no-prior-frame"
    assert store.max_record_seq(fresh_id) == 0

    records.note_action_board(fresh_id, game_obj)
    seq = audit.record(fresh_id, "test_action", player="alice", allowed=True)
    entry = audit.entries(fresh_id)[-1]
    assert entry["seq"] == seq
    assert entry["pre_action_frame"] is None, entry
    assert entry["pre_state"] is not None, entry
    assert "battlefield" in entry["pre_state"]


# -- (3) a paused action is committing ---------------------------------------


def test_a_paused_action_is_recorded_as_a_committing_row(client, game):
    """A `PAUSED` outcome (`app.py`'s shared `_respond`) is recorded
    `allowed: True, paused: True` - the command ran far enough to be asked a
    real question, so it is a committing row a DSL converter must not skip,
    not one it treats as noise. The HTTP response body's own `allowed: None`
    is unaffected (see `test_pause_slot_clobber.py`)."""
    seats_for(game)
    rig = stock_bombardment_board(client, game)

    paused = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": rig["bombardment_id"],
            "ability_index": 0,
            "sacrificing": [rig["bears_id"]],
        },
    )
    body = paused.json()
    assert body["allowed"] is None, body
    assert body["paused"] is True, body

    entry = entry_of(log_of(client, game), "activate")
    assert entry["detail"]["allowed"] is True, entry
    assert entry["detail"]["paused"] is True, entry


# -- (4) mulligan/opening-hand rows are committing ---------------------------


def test_mulligan_and_opening_hand_rows_are_committing(client):
    info = boot(
        client,
        alice_deck="60 Forest",
        bob_deck="60 Forest",
        seed=1,
    )
    game_id = info["game_id"]
    resp = declare(client, info, "alice", take=False)
    assert resp.status_code == 200, resp.text
    resp = declare(client, info, "bob", take=False)
    assert resp.status_code == 200, resp.text

    resp = finish_opening_hand(client, info, "alice")
    assert resp.status_code == 200, resp.text
    resp = finish_opening_hand(client, info, "bob")
    assert resp.status_code == 200, resp.text

    log = log_of(client, game_id)
    mull = entry_of(log, "mulligan_declared", player="alice")
    assert mull["detail"]["allowed"] is True, mull
    done = entry_of(log, "opening_hand_done", player="alice")
    assert done["detail"]["allowed"] is True, done
