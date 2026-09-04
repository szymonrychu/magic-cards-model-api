"""`target_minimum` on a `choose_targets` `pending_decision` (CR 601.2c,
CR 115.6).

`target_maximum` was already served, but the floor a mandatory target
requirement carries - "target permanent" is not a "you may", so the engine
refuses fewer than `target_minimum` legal targets - was computed by the
engine and simply never crossed into the view. This is the same board
`test_target_decisions.py` boots (Devourer of Destiny's cast trigger,
"exile target permanent that's one or more colors", a plain mandatory
single target, so `target_minimum` and `target_maximum` are both 1), copied
here rather than imported since that module does not export it as a shared
fixture.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def stock_devourer_board(
    client: TestClient, game_id: str, *, uid: str | None = None
) -> dict[str, Any]:
    """alice: a Sol Ring and five Mountains, enough to cast Devourer of
    Destiny ({5}{C}{C}); bob: a Grizzly Bears to be the offered target."""
    add_card(client, game_id, "Sol Ring", "alice", "battlefield")
    for _ in range(5):
        add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Devourer of Destiny", "alice", "hand")
    add_card(client, game_id, "Grizzly Bears", "bob", "battlefield")
    board = state(client, game_id, uid=uid)
    return {
        "sol_ring": find(board["battlefield"], "Sol Ring"),
        "mountains": [c for c in board["battlefield"] if c["name"] == "Mountain"],
        "devourer": board["hands"]["alice"][0],
        "bears": find(board["battlefield"], "Grizzly Bears"),
    }


def cast_devourer_in_background(
    game_id: str, board: dict[str, Any], player: str = "alice"
) -> threading.Thread:
    """The cast call blocks inside `Game.ask` once the cast trigger is put on
    the stack, so it is run on a background thread.

    `daemon=True`, unlike the other files' copies of this helper: this file's
    only test never answers the decision (it only reads the pending view), so
    nothing ever unblocks the thread short of the engine's own multi-minute
    decision timeout - a plain thread would make the whole suite wait on it
    at interpreter shutdown.
    """
    sources = [board["sol_ring"]["id"]] + [m["id"] for m in board["mountains"]]

    def call() -> None:
        stepping_client = TestClient(app)
        stepping_client.post(
            f"/api/games/{game_id}/actions/cast",
            json={
                "player": player,
                "card_id": board["devourer"]["id"],
                "mana_sources": sources,
            },
        )

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    return thread


def wait_for_pending(client: TestClient, game_id: str, uid: str, tries: int = 300) -> dict:
    for _ in range(tries):
        view = state(client, game_id, uid=uid)
        if view.get("pending_decision") is not None:
            return view
        time.sleep(0.01)
    raise AssertionError("no pending decision showed up in time")


def boot_a_choose_targets_decision(client: TestClient, game_id: str) -> str:
    """Casts Devourer of Destiny in the background so its cast trigger's
    mandatory single-target `choose_targets` decision is pending, and
    returns alice's own seat uid."""
    uids = seats_for(client, game_id)
    board = stock_devourer_board(client, game_id)
    cast_devourer_in_background(game_id, board)
    wait_for_pending(client, game_id, uids["alice"])
    return uids["alice"]


def test_choose_targets_decision_says_its_minimum(client: TestClient, game: str) -> None:
    uid = boot_a_choose_targets_decision(
        client, game
    )  # from the copied helper, returns alice's uid
    pending = client.get(f"/api/games/{game}", params={"uid": uid}).json()["pending_decision"]
    assert pending["kind"] == "choose_targets"
    assert pending["target_minimum"] == 1
    assert pending["target_maximum"] == 1
