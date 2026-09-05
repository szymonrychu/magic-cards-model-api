"""Pins `PendingDecision.to_view()`'s real wire shape against the
hand-written decision fixtures `tests/test_ui_decisions.py` (and, by the same
shape, `tests/test_ui_ribbon.py`) feed to `ui/decisions.js` under Node.

The whole-branch review found those fixtures drifting from what
`PendingDecision.from_engine` can actually produce: `target_minimum: None`
and `moved_label`/`kept_label: None`, none of which the engine ever emits -
`target_minimum` is `int = 0` and the two labels default to `""` (see
`magic_cards_model.machines.decisions.Decision`, and
`magic_cards_model_api.decisions.PendingDecision`'s own field defaults). A
fixture with the wrong shape can pass a `decisions.js` test for the wrong
reason - the code path a real `None` would hit (`pending.target_minimum ||
0`) happens to fall back to the same value a real `0` does, so the bug was
silent - and this is the regression guard: a real `choose_targets` decision,
booted through the whole API stack, must carry exactly the fixture's key set
and an `int` `target_minimum`.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient
from test_ui_decisions import MAY

from magic_cards_model_api.main import app
from magic_cards_model_api.store import store

# -- copied from tests/test_decision_target_minimum.py, which does not export
# -- these as a shared fixture (see that module's own docstring for why its
# -- copy is a copy rather than an import) --------------------------------


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
    the stack, so it is run on a background thread. `daemon=True` since this
    file's own test never answers the decision, only reads the pending view."""
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


# -- the regression guard ---------------------------------------------------


def test_a_real_choose_targets_decision_matches_the_fixtures_key_set(
    client: TestClient, game: str
) -> None:
    uid = boot_a_choose_targets_decision(client, game)
    pending = client.get(f"/api/games/{game}", params={"uid": uid}).json()["pending_decision"]
    assert pending["kind"] == "choose_targets"

    # The deciding player's own view, for a target drawn from the shared
    # battlefield (never a hidden zone), carries no `card_details` -
    # `visibility.redact_pending_decision` only ever adds that key from
    # `cards`, and a `choose_targets` decision never populates `cards` at
    # all - so the real wire shape is exactly `PendingDecision.to_view()`'s
    # own key set, which is what `test_ui_decisions.py`'s `MAY` fixture (the
    # base every other fixture in that file spreads from) is built to match.
    assert set(pending.keys()) == set(MAY.keys())

    # The regression itself: a hand-written `None` here used to slip past
    # every `decisions.js` test because `pending.target_minimum || 0` reads
    # `None` and `0` identically - the engine never emits anything but an
    # `int`.
    assert isinstance(pending["target_minimum"], int)
    assert pending["target_minimum"] == 1
    assert isinstance(pending["moved_label"], str)
    assert isinstance(pending["kept_label"], str)
