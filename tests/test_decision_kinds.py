"""Every `DecisionKind` the engine can ask (`magic_cards_model.machines.decisions`),
matched against what `POST /api/games/{id}/decide` actually answers.

Before this file, `decide` only knew `choose_cards` and `choose_targets`
(`test_decisions.py`, `test_target_decisions.py`). Four more the engine's
`Game.ask` can route through this API's live `GameDecisionProvider` - `may`,
`pay_life`, `scry`, `surveil` - then fell through to `default_answer` no
matter what a seat sent, after parking the request for the full timeout. The
reported case was a shockland's "pay 2 life" (`pending_decision.kind ==
"pay_life"`): `decide` refused it outright, the request timed out, and
CR 119.4's choice was made *for* the player rather than *by* them.

A second wave hit the same gap for three kinds added to the engine within
hours of each other and never given an API path at all: a live player cast
Utopia Sprawl, the engine raised `choose_a_quality` ("as this Aura enters,
choose a color", CR 614.12a), and `decide` answered every shape the player
tried with "this endpoint does not know how to answer a 'choose_a_quality'
decision" - freezing the whole match, every seat, not just that card, since a
pending decision blocks the game until it is answered. `pay_energy` (a fixed
"pay {E}{E}...", CR 107.14/118.3) and `pay_energy_amount` (a variable "pay
any amount of {E}") were the other two gaps, added to the engine in the same
window. `test_every_decision_kind_is_answerable` below is the regression test
for the *shape* of this bug, not just this instance of it: it asserts that
`decisions.ANSWERABLE_KINDS` (a live seat can answer it through `decide`)
covers every `DecisionKind` the engine defines, so the next one cannot
silently freeze a game the same way. That is now a structural guarantee
(T7b's pause-and-resume gives every `DecisionKind` a real transport, and
`decisions.py` asserts the two sets equal at import time), not the two-set
"answerable or served elsewhere" split this test used to check by hand - see
that test's own docstring for the history.

`declare_attackers` and `declare_blockers` no longer stand apart from every
other kind either: before T7b, `ask_attackers`/`ask_blockers` took their
answer straight off `DecisionScript.take` and never reached the blocking
mailbox this API published to, so no request through this API could ever
make one of them arrive at `POST /decide` - both were answered only through
their own actions (`actions/attack`, `actions/declare-blockers`) calling
`Game.declare_attackers`/`Game.declare_blockers` directly, and this file
deliberately carried no live-seat test for either. Pausing changed that:
CR 508.1/CR 509.1's combat declarations now pause on `advance` exactly like
any other kind and are answerable through `POST /decide` directly (see
`decide`'s own `DECLARE_ATTACKERS`/`DECLARE_BLOCKERS` branch in `app.py`) as
well as through the two action aliases - `test_decisions.py`'s
`advance_into_cleanup` answers them inline wherever a turn is walked forward
without them being the point of the test. `OPENING_HAND_ACTION` (CR 103.6,
through `Game.take_opening_hand_actions`'s `self.ask`) and `MULLIGAN`
(CR 103.5, which `ask_mulligan` raises from `Game.settle_pregame`) were
always answerable by a live seat this way - see `tests/test_mulligans.py`,
which drives each of them end to end.

This file used to carry two more tests here: `test_an_unwired_kind_is_
refused_by_name` published a hand-built `PendingDecision` straight to
`store.mailbox(game_id)` to exercise `decide`'s refusal of a kind with no
live transport, and `test_log_timeout_wording_for_an_unwired_kind` called
`GameDecisionProvider._log_timeout` directly to check that refusal's
wording. `store.mailbox`, `GameDecisionProvider` and the timeout mechanism
it logged are all gone with T7b (a paused decision just stays paused,
indefinitely, until answered - it does not time out, and there is no longer
any kind with no live transport for either test's scenario to construct).
Both are deleted; `decide`'s own unreachable-kind branch stays in `app.py`,
marked `# pragma: no cover - unreachable`, for the same reason
`test_deadlock.py` was deleted in this same task rather than rewritten.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from conftest import add_card, find, life_of, state
from fastapi.testclient import TestClient

from magic_cards_model_api import decisions
from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def wait_for_pending(client: TestClient, game_id: str, uid: str) -> dict:
    """The state view showing a pending decision for `uid` - a same-request
    confirmation, not a poll loop: the mutating call that paused already
    carries the decision in its own response, so by the time this is called
    it is already sitting in the store (see `test_decisions.py`'s own
    version of this helper, which this matches)."""
    view = state(client, game_id, uid=uid)
    assert view.get("pending_decision") is not None, "no pending decision showed up"
    return view


# -- pay_life: the reported case, a shockland (CR 614.12a, CR 119.4) --------


def play_steam_vents(game_id: str, land_id: str, player: str = "alice") -> dict[str, Any]:
    """Play a land that raises `pay_life` (CR 608.2d) - the mutating request
    itself returns immediately, `paused: True`, with `pending_decision`
    attached (see `test_decisions.py`'s module docstring); there is no
    background thread to spawn for this any more."""
    response = TestClient(app).post(
        f"/api/games/{game_id}/actions/play-land",
        json={"player": player, "card_id": land_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_pay_life_yes_pays_life_and_enters_untapped(client, game):
    """CR 119.4: paying is legal at 20 life, so "yes" is offered and, once
    taken, both halves of the choice happen - life goes down by 2 AND the
    land enters untapped (CR 614.12a: the choice is made *before* it enters,
    but the entering permanent's tapped state is what the choice was for)."""
    add_card(client, game, "Steam Vents", "alice", "hand")
    uids = seats_for(client, game)
    land = state(client, game, uid=uids["alice"])["hands"]["alice"][0]

    played = play_steam_vents(game, land["id"])
    assert played["paused"] is True
    pending = played["pending_decision"]
    assert pending["player"] == "alice"
    assert pending["kind"] == "pay_life"
    assert pending["amount"] == 2
    assert pending["affirmative_is_legal"] is True

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True

    final = state(client, game, uid=uids["alice"])
    assert life_of(final, "alice") == 18
    vents = find(final["battlefield"], "Steam Vents")
    assert vents["tapped"] is False
    assert final.get("pending_decision") is None


def test_pay_life_no_keeps_life_and_enters_tapped(client, game):
    """The opposite answer to the same question: declining leaves life
    untouched and the land enters tapped - proving `decide` actually carries
    the *chosen* branch through, not just "any answer means untapped"."""
    add_card(client, game, "Steam Vents", "alice", "hand")
    uids = seats_for(client, game)
    land = state(client, game, uid=uids["alice"])["hands"]["alice"][0]

    play_steam_vents(game, land["id"])
    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": False},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True

    final = state(client, game, uid=uids["alice"])
    assert life_of(final, "alice") == 20
    vents = find(final["battlefield"], "Steam Vents")
    assert vents["tapped"] is True


def test_pay_life_missing_accept_is_refused_not_defaulted(client, game):
    """CR 608.2d needs an actual announcement. A `decide` call for a
    yes/no kind with no `accept` must be refused by name, not silently
    treated as "no" - a client bug should surface as a rejection, not as a
    wrong answer nobody chose."""
    add_card(client, game, "Steam Vents", "alice", "hand")
    uids = seats_for(client, game)
    land = state(client, game, uid=uids["alice"])["hands"]["alice"][0]

    play_steam_vents(game, land["id"])
    refused = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice"},
    )
    assert refused.status_code == 200
    body = refused.json()
    assert body["accepted"] is False
    assert "yes/no" in body["reason"]

    # the decision is still pending - answer it for real
    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )


def test_pay_life_at_too_little_life_offers_no_yes(client, game):
    """CR 119.4: below the amount, paying is not a real option -
    `affirmative_is_legal` must say so, before the seat even answers, and
    `decide` must refuse an attempt to say yes anyway rather than letting an
    illegal payment through."""
    add_card(client, game, "Steam Vents", "alice", "hand")
    uids = seats_for(client, game)
    client.patch(f"/api/games/{game}/players/alice", json={"life": 1})
    land = state(client, game, uid=uids["alice"])["hands"]["alice"][0]

    played = play_steam_vents(game, land["id"])
    pending = played["pending_decision"]
    assert pending["affirmative_is_legal"] is False

    # `decide` hands `True` to the engine, which is `Game.ask`'s own job to
    # refuse (CR 608.2d: "can't choose an option that's illegal").
    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": False},
    )

    final = state(client, game, uid=uids["alice"])
    assert life_of(final, "alice") == 1, "an illegal payment is never taken"
    assert find(final["battlefield"], "Steam Vents")["tapped"] is True


def test_the_opponent_cannot_answer_a_pay_life_decision(client, game):
    add_card(client, game, "Steam Vents", "alice", "hand")
    uids = seats_for(client, game)
    land = state(client, game, uid=uids["alice"])["hands"]["alice"][0]

    play_steam_vents(game, land["id"])
    refused = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["bob"]},
        json={"player": "bob", "accept": True},
    )
    assert refused.json()["accepted"] is False

    still_pending = state(client, game, uid=uids["alice"])
    assert still_pending.get("pending_decision") is not None

    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )


def test_the_opponent_cannot_see_the_pay_life_prompt_beyond_kind_and_player(client, game):
    add_card(client, game, "Steam Vents", "alice", "hand")
    uids = seats_for(client, game)
    land = state(client, game, uid=uids["alice"])["hands"]["alice"][0]

    play_steam_vents(game, land["id"])
    bobs_view = state(client, game, uid=uids["bob"])
    pending = bobs_view["pending_decision"]
    assert pending == {"player": "alice", "kind": "pay_life", "prompt": "pay 2 life"}

    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )


# -- scry / surveil: which cards move, which stay, in what order ------------


def stock_library_top(client: TestClient, game_id: str, player: str, names: list[str]) -> None:
    """`names[0]` ends up on top, `names[-1]` deepest of the ones named -
    `put_card_in_zone` inserts each new library addition at index 0, so
    adding in reverse order leaves them in the order given."""
    for name in reversed(names):
        add_card(client, game_id, name, player, "library")


def cast_and_resolve(
    game_id: str, player: str, card_id: str, sources: list[str], targets: list[str] | None = None
) -> dict[str, Any]:
    """Cast `card_id`, then resolve the stack's top - the call that may pause
    on the spell's own CR 608.2d question, returned directly rather than
    parked on a background thread (see `test_decisions.py`'s module
    docstring)."""
    client = TestClient(app)
    cast = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": player,
            "card_id": card_id,
            "mana_sources": sources,
            **({"targets": targets} if targets is not None else {}),
        },
    )
    assert cast.status_code == 200, cast.text
    return resolve_top(game_id)


def resolve_top(game_id: str) -> dict[str, Any]:
    """Resolve the stack's top - the call that may pause on the resolving
    object's own CR 608.2d question, returned directly."""
    response = TestClient(app).post(f"/api/games/{game_id}/resolve-top")
    assert response.status_code == 200, response.text
    return response.json()


def test_scry_moves_named_cards_to_the_bottom_and_orders_the_rest(client, game):
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Preordain", "alice", "hand")
    stock_library_top(client, game, "alice", ["Mountain", "Forest", "Grizzly Bears"])
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    island = find(board["battlefield"], "Island")
    preordain = board["hands"]["alice"][0]
    admin_board = state(client, game)
    mountain, forest, bears = admin_board["libraries"]["alice"][:3]

    resolved = cast_and_resolve(game, "alice", preordain["id"], [island["id"]])
    assert resolved["paused"] is True
    # `cast_and_resolve`'s own `resolve-top` call carries no `uid`, so its
    # response's `pending_decision` is the unauthenticated (fully redacted)
    # view - the deciding player's own scoped view comes from a separate
    # read, matching every other test in this file.
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "scry"
    assert pending["amount"] == 2
    assert set(pending["cards"]) == {mountain["id"], forest["id"]}

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "moved": [mountain["id"]],
            "top_order": [forest["id"]],
        },
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True

    final = state(client, game, uid=uids["alice"])
    admin_final = state(client, game)
    # Preordain drew the card scry left on top (Forest), then Mountain is
    # bottommost of what is left, and the untouched Bears sit above it.
    assert [c["name"] for c in final["hands"]["alice"]] == ["Forest"]
    remaining = [c["name"] for c in admin_final["libraries"]["alice"]]
    assert remaining[-1] == "Mountain", "the moved card went to the bottom, not vanished"
    assert "Forest" not in remaining, "the drawn card left the library entirely"
    assert final["graveyards"]["alice"][0]["name"] == "Preordain"


def test_scry_keep_everything_on_top_is_a_real_answer(client, game):
    """The opposite of the previous test: an empty `moved` genuinely keeps
    every card on top, in whatever `top_order` says - proving `decide` does
    not treat "nothing named" as "nothing understood"."""
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Preordain", "alice", "hand")
    stock_library_top(client, game, "alice", ["Mountain", "Forest", "Grizzly Bears"])
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    island = find(board["battlefield"], "Island")
    preordain = board["hands"]["alice"][0]
    admin_board = state(client, game)
    mountain, forest, _bears = admin_board["libraries"]["alice"][:3]

    cast_and_resolve(game, "alice", preordain["id"], [island["id"]])
    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "moved": [], "top_order": [mountain["id"], forest["id"]]},
    )

    final = state(client, game, uid=uids["alice"])
    admin_final = state(client, game)
    # Nothing moved to the bottom, so the draw takes the top of the ordering
    # just given - Mountain - and Forest is what is left on top.
    assert [c["name"] for c in final["hands"]["alice"]] == ["Mountain"]
    assert admin_final["libraries"]["alice"][0]["name"] == "Forest"


def test_surveil_moves_named_cards_to_the_graveyard(client, game):
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Contingency Plan", "alice", "hand")
    stock_library_top(client, game, "alice", ["Mountain", "Forest"])
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    island = find(board["battlefield"], "Island")
    red_source = find(board["battlefield"], "Mountain")
    plan = board["hands"]["alice"][0]
    admin_board = state(client, game)
    mountain, forest = admin_board["libraries"]["alice"][:2]

    cast_and_resolve(game, "alice", plan["id"], [island["id"], red_source["id"]])
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "surveil"
    assert pending["amount"] == 5

    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "moved": [mountain["id"]], "top_order": [forest["id"]]},
    )

    final = state(client, game, uid=uids["alice"])
    admin_final = state(client, game)
    graveyard_names = {c["name"] for c in final["graveyards"]["alice"]}
    assert "Mountain" in graveyard_names, "the named card went to the graveyard, CR 701.25a"
    assert admin_final["libraries"]["alice"][0]["name"] == "Forest"


# -- L1a: the count the engine enforces (`amount`/`exact`, `validate_answer`
# since PR #31) must show up in the hint text every client reads, not only
# in the fields a client would have to compute it from itself -------------


def test_scry_answer_with_states_any_number_not_any_of(client, game):
    """CR 701.22a's own 'any number of them' (`exact=False`, `amount` equal
    to the whole look) - a real scry through the state view and through the
    lifted MCP answer alike, never the old unqualified 'any of
    pending_decision.cards' that was silent about the count `validate_answer`
    actually enforces."""
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Preordain", "alice", "hand")
    stock_library_top(client, game, "alice", ["Mountain", "Forest", "Grizzly Bears"])
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    island = find(board["battlefield"], "Island")
    preordain = board["hands"]["alice"][0]

    cast_and_resolve(game, "alice", preordain["id"], [island["id"]])
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "scry"
    assert pending["exact"] is False
    assert pending["amount"] == len(pending["cards"]) == 2

    assert "any number, including none" in pending["answer_with"]
    assert "any of pending_decision.cards" not in pending["answer_with"]

    from magic_cards_model_api import mcp

    lifted = mcp._action_answer(
        {"outcome": {"accepted": True}, "state": {"pending_decision": pending}}
    )
    assert "any number, including none" in lifted["pending_decision"]["answer_with"]


def test_look_and_split_exact_one_states_exactly_one(client, game):
    """Codecracker Hound's 'put one into your hand and the other into your
    graveyard' (CR 608.2d) is `exact=True, amount=1` on the same `SCRY` kind
    a plain scry reuses (`tests/test_card_split_labels.py`'s own Devourer of
    Destiny coverage is the `exact=False` sibling of this one) - the hint
    must say 'exactly 1', never 'any of'."""
    from magic_cards_model.machines.decisions import Decision, DecisionKind

    from magic_cards_model_api import mcp

    engine_decision = Decision(
        kind=DecisionKind.SCRY,
        player="alice",
        prompt="look at the top two cards of your library",
        amount=1,
        exact=True,
        cards=(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        ),
        moved_label="your hand",
        kept_label="your graveyard",
    )
    pending = decisions.PendingDecision.from_engine(engine_decision, generation=3).to_view()

    assert "exactly 1" in pending["answer_with"]
    assert "any of pending_decision.cards" not in pending["answer_with"]

    lifted = mcp._action_answer(
        {"outcome": {"accepted": True}, "state": {"pending_decision": pending}}
    )
    assert "exactly 1" in lifted["pending_decision"]["answer_with"]


# -- surveil: the deciding player must be able to read what they are looking
# at (CR 701.20e), the opponent must not (CR 701.20e names only the deciding
# player) -----------------------------------------------------------------


def test_the_deciding_player_can_resolve_surveilled_card_identities(client, game):
    """The reported defect: `pending_decision.cards` on a `surveil` is a bare
    list of ids, and `libraries` is stripped for every non-admin viewer -
    including the viewer's own - so the deciding player had no way to learn
    what card each id names, and had to choose blind. `card_details` is the
    fix: the full view of exactly the cards this decision names, and nothing
    else from the library."""
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Contingency Plan", "alice", "hand")
    stock_library_top(client, game, "alice", ["Mountain", "Forest", "Grizzly Bears"])
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    island = find(board["battlefield"], "Island")
    red_source = find(board["battlefield"], "Mountain")
    plan = board["hands"]["alice"][0]
    admin_board = state(client, game)
    mountain, forest, bears = admin_board["libraries"]["alice"][:3]

    cast_and_resolve(game, "alice", plan["id"], [island["id"], red_source["id"]])
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "surveil"
    assert set(pending["cards"]) == {mountain["id"], forest["id"], bears["id"]}

    # The deciding player must be able to name every card the decision
    # offers, from `card_details` alone - not by cross-referencing a
    # library view they are never sent.
    details_by_id = {c["id"]: c["name"] for c in pending["card_details"]}
    assert details_by_id == {
        mountain["id"]: "Mountain",
        forest["id"]: "Forest",
        bears["id"]: "Grizzly Bears",
    }
    # And nothing beyond those three cards leaks - no surrounding library
    # order or contents.
    assert len(pending["card_details"]) == 3

    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "moved": [mountain["id"]], "top_order": [forest["id"]]},
    )


def test_the_opponent_cannot_resolve_a_pending_surveils_cards(client, game):
    """The opponent still learns that alice is surveilling (CR 101.4a: the
    choice itself is public) but never which cards, and never gets a
    `card_details` field to resolve them from - the genuine-look half of the
    fix must not regress."""
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Contingency Plan", "alice", "hand")
    stock_library_top(client, game, "alice", ["Mountain", "Forest"])
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    island = find(board["battlefield"], "Island")
    red_source = find(board["battlefield"], "Mountain")
    plan = board["hands"]["alice"][0]
    admin_board = state(client, game)
    mountain, forest = admin_board["libraries"]["alice"][:2]

    cast_and_resolve(game, "alice", plan["id"], [island["id"], red_source["id"]])
    bobs_view = state(client, game, uid=uids["bob"])
    pending = bobs_view["pending_decision"]
    assert pending == {"player": "alice", "kind": "surveil", "prompt": pending["prompt"]}
    assert "cards" not in pending
    assert "card_details" not in pending

    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "moved": [mountain["id"]], "top_order": [forest["id"]]},
    )


# -- may: the other yes/no kind, sharing `decisions.bool_answer` ------------


def stage_malevolent_rumble(client: TestClient, game_id: str) -> dict[str, str]:
    """Two untapped Forests, Malevolent Rumble in hand, and a library whose
    top four hold two more permanents (a second Forest, Grizzly Bears, Wall
    of Omens) and one non-permanent (Counterspell) - mirrors this API's own
    `test_command_runner.py::_malevolent_rumble_setup` (same board, same
    card). Malevolent Rumble's "Reveal the top four cards of your library.
    You may put a permanent card from among them into your hand." is the
    real card that stages a bare `may` cleanly, without a target of its own
    - no card in this pool's registered `EFFECTS` does that any more simply,
    and with T7b's pause-and-resume replacing the blocking mailbox, only a
    real `Command` (a live `cast`, here) can pause in a way `POST /decide`
    can resume; a bare `Game.may(...)` call outside one cannot."""
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Malevolent Rumble", "alice", "hand")
    for name in ("Grizzly Bears", "Wall of Omens", "Forest", "Counterspell"):
        add_card(client, game_id, name, "alice", "library")
    board = state(client, game_id)
    wall = next(c for c in board["libraries"]["alice"] if c["name"] == "Wall of Omens")
    return {
        "forests": [c["id"] for c in board["battlefield"] if c["name"] == "Forest"],
        "rumble": board["hands"]["alice"][0]["id"],
        "wall": wall["id"],
    }


def test_may_shares_the_same_bool_answer_path_as_pay_life(client, game):
    """Malevolent Rumble pauses on MAY ("you may put a permanent card ...
    into your hand"), then - once accepted - on CHOOSE_CARDS for which one,
    proving `decide` answers MAY, not just PAY_LIFE, without re-deriving a
    whole spell for a question `bool_answer` handles identically either way.
    This is the same two-decision chain `test_command_runner.py` drives
    against the raw command runner; this proves the same thing through a
    live seat's own `POST /decide` calls instead."""
    staged = stage_malevolent_rumble(client, game)
    uids = seats_for(client, game)

    resolved = cast_and_resolve(game, "alice", staged["rumble"], staged["forests"])
    assert resolved["paused"] is True
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "may"
    assert pending["player"] == "alice"

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["accepted"] is True
    assert body["paused"] is True
    next_pending = body["pending_decision"]
    assert next_pending["kind"] == "choose_cards"

    chosen = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [staged["wall"]]},
    )
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["accepted"] is True

    final = state(client, game, uid=uids["alice"])
    assert [c["name"] for c in final["hands"]["alice"]] == ["Wall of Omens"]


# -- pay_energy: the fixed-amount cousin of pay_life (CR 107.14, CR 118.3) --


def stage_static_prison_targeting_bears(
    client: TestClient, game_id: str, uids: dict[str, str]
) -> None:
    """Casts Static Prison, resolves it (entering and putting its own ETB
    trigger - "exile target nonland permanent an opponent controls... you
    get {E}{E}" - on the stack), then resolves that trigger, answering its
    CHOOSE_TARGETS with bob's Grizzly Bears (`test_target_decisions.py`'s own
    mechanism). Leaves alice with Static Prison on the battlefield and 2
    energy - `Game.may_pay_energy`'s own real caller (`machines/effects.py`'s
    `_sacrifice_unless_pay_energy_effect`, built for this exact card's "at
    the beginning of your first main phase, sacrifice this unless you pay
    {E}") is what raises `pay_energy` once a later main phase begins,
    replacing the direct `Game.may_pay_energy` call this test made before
    T7b's pause-and-resume replaced the blocking mailbox a bare engine call
    could park a live decision on."""
    add_card(client, game_id, "Plains", "alice", "battlefield")
    add_card(client, game_id, "Static Prison", "alice", "hand")
    bears = add_card(client, game_id, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    board = state(client, game_id)
    plains = find(board["battlefield"], "Plains")
    prison = board["hands"]["alice"][0]

    cast = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={"player": "alice", "card_id": prison["id"], "mana_sources": [plains["id"]]},
    )
    assert cast.status_code == 200, cast.text
    # Static Prison resolves and enters, and its ETB trigger's own target is
    # chosen as it is put on the stack (CR 603.3d) - within this same
    # `resolve-top` call, which is what pauses here on CHOOSE_TARGETS.
    resolved = resolve_top(game_id)
    assert resolved["paused"] is True
    pending = wait_for_pending(client, game_id, uids["alice"])["pending_decision"]
    assert pending["kind"] == "choose_targets"
    answer = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "targets": [bears]},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True
    resolve_top(game_id)  # the ETB trigger itself resolves: exile, then "you get {E}{E}"


def advance_to_precombat_main_with_static_prisons_trigger_on_the_stack(
    client: TestClient, game_id: str
) -> None:
    """Jump to `draw` raw, then `advance` once through the real turn machine
    (one step at a time, like every `advance` call) so CR 500.6's
    beginning-of-step event fires and Static Prison's "at the beginning of
    your first main phase" trigger reaches the stack - the same shape
    `reach_end_step_with_necrodominance_on_the_stack` uses for a different
    step. Placing the trigger never pauses by itself (only one trigger,
    nothing to order); it is resolving it, next, that asks."""
    client.put(f"/api/games/{game_id}/step", json={"step": "draw", "player": "alice"})
    advanced = client.post(f"/api/games/{game_id}/advance", json={"player": "alice"})
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["advanced_to"] == "precombat_main", advanced.json()


def test_pay_energy_yes_spends_the_energy(client, game):
    """Static Prison's own upkeep-adjacent trigger ("sacrifice this unless
    you pay {E}") is the same fixed-amount question `may_pay_life` already
    asks - `bool_answer` is what `decide` reuses for both, so this proves it
    covers `pay_energy` too, the same way `test_may_shares_the_same_bool_
    answer_path_as_pay_life` proved it for `may`."""
    uids = seats_for(client, game)
    stage_static_prison_targeting_bears(client, game, uids)
    advance_to_precombat_main_with_static_prisons_trigger_on_the_stack(client, game)

    resolved = resolve_top(game)
    assert resolved["paused"] is True
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "pay_energy"
    assert pending["amount"] == 1  # Static Prison's own "unless you pay {E}"
    assert pending["affirmative_is_legal"] is True

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True

    final = state(client, game)
    alice = next(p for p in final["players"] if p["username"] == "alice")
    assert alice["counters"]["energy"] == 1, "2 granted by the ETB, 1 spent"
    assert find(final["battlefield"], "Static Prison")["zone"] == "battlefield", (
        "paid, so it was never sacrificed"
    )


def test_pay_energy_below_the_amount_offers_no_yes(client, game):
    """CR 118.3: a player short on energy cannot legally say yes -
    `affirmative_is_legal` must say so before the seat even answers, the same
    floor `pay_life` enforces off life instead of energy. Zeroing alice's
    energy after Static Prison's ETB already granted its 2 is the same
    admin-patch staging `test_pay_life_at_too_little_life_offers_no_yes`
    already uses for life."""
    uids = seats_for(client, game)
    stage_static_prison_targeting_bears(client, game, uids)
    client.patch(f"/api/games/{game}/players/alice", json={"counters": {"energy": 0}})
    advance_to_precombat_main_with_static_prisons_trigger_on_the_stack(client, game)

    resolve_top(game)
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["affirmative_is_legal"] is False
    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": False},
    )
    resolve_top(game)  # Static Prison's own leaves-the-battlefield trigger, returning Bears

    final = state(client, game)
    assert "Static Prison" not in [c["name"] for c in final["battlefield"]], (
        "declined (forced, since paying was never legal), so it was sacrificed"
    )
    assert find(final["battlefield"], "Grizzly Bears")["zone"] == "battlefield", (
        "sacrificing Static Prison returned bob's exiled Grizzly Bears"
    )


# -- pay_energy_amount: "pay any amount of {E}" (CR 107.14, CR 118.3) -------


def test_pay_energy_amount_pays_the_named_amount(client, game):
    """Galvanic Discharge: "You get {E}{E}{E}..., then you may pay any amount
    of {E}." deals that much damage. A `pay_energy_amount` decision is an
    `int`, not a bool - this proves `decide` carries the chosen *amount*
    through to real damage, not just accepts or refuses the call."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Galvanic Discharge", "alice", "hand")
    bob_bears = add_card(client, game, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    mountain = find(board["battlefield"], "Mountain")
    discharge = board["hands"]["alice"][0]

    resolved = cast_and_resolve(
        game, "alice", discharge["id"], [mountain["id"]], targets=[bob_bears]
    )
    assert resolved["paused"] is True
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "pay_energy_amount"
    assert pending["amount"] == 3  # the three energy Galvanic Discharge just granted

    # 1, not the full 3 or the lethal 2: proving `decide` carries through
    # the exact amount chosen, not "as much as possible" - and staying
    # below Grizzly Bears' toughness so the creature is still there
    # afterwards to show the damage on.
    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "energy_amount": 1},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True

    final = state(client, game)
    alice = next(p for p in final["players"] if p["username"] == "alice")
    assert alice["counters"]["energy"] == 2, "3 granted, 1 spent"
    bears = find(final["battlefield"], "Grizzly Bears")
    assert bears["damage"] == 1


def test_pay_energy_amount_missing_value_is_refused_not_defaulted(client, game):
    """A `decide` call for `pay_energy_amount` with no `energy_amount` must be
    refused by name, matching `pay_life`'s refusal of a missing `accept`."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Galvanic Discharge", "alice", "hand")
    bob_bears = add_card(client, game, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    mountain = find(board["battlefield"], "Mountain")
    discharge = board["hands"]["alice"][0]

    cast_and_resolve(game, "alice", discharge["id"], [mountain["id"]], targets=[bob_bears])
    refused = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice"},
    )
    assert refused.status_code == 200
    body = refused.json()
    assert body["accepted"] is False
    assert "energy_amount" in body["reason"]

    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "energy_amount": 0},
    )


# -- pay_life_amount: "you may pay any amount of life" (CR 119.4) -----------


def reach_end_step_with_necrodominance_on_the_stack(client: TestClient, game_id: str) -> None:
    """Jump to `postcombat_main` raw (`PUT /step`, no hooks - see
    `engine_api.set_step`'s own docstring), then `advance` once through the
    real turn machine (`Game.advance`) so CR 500.6's beginning-of-step event
    actually fires and CR 603.3's `put_triggers_on_stack` runs -
    Necrodominance's end-step trigger only ever reaches the stack through
    that machinery, never through the raw step jump alone."""
    client.put(f"/api/games/{game_id}/step", json={"step": "postcombat_main", "player": "alice"})
    advanced = client.post(f"/api/games/{game_id}/advance", json={"player": "alice"})
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["advanced_to"] == "end_step", advanced.json()


def test_pay_life_amount_pays_the_named_amount(client, game):
    """Necrodominance's end-step trigger: "you may pay any amount of life.
    If you do, draw that many cards." A `pay_life_amount` decision is an
    `int`, not a bool - this proves `decide` carries the chosen *amount*
    through to a real life change and a real draw, not just accepts or
    refuses the call. This is the exact shape (Necrodominance's end step)
    the report names as one of the two live-hang cases."""
    add_card(client, game, "Necrodominance", "alice", "battlefield")
    stock_library_top(client, game, "alice", ["Mountain", "Forest", "Grizzly Bears"])
    uids = seats_for(client, game)
    reach_end_step_with_necrodominance_on_the_stack(client, game)

    resolved = resolve_top(game)
    assert resolved["paused"] is True
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "pay_life_amount"
    assert pending["player"] == "alice"
    assert pending["amount"] == 20  # alice's starting life total

    # 3, not the full 20 or 0: proving `decide` carries through the exact
    # amount chosen, matching `test_pay_energy_amount_pays_the_named_amount`'s
    # own reasoning for its variable-amount cousin.
    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "life_amount": 3},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True

    final = state(client, game)
    assert life_of(final, "alice") == 17, "3 paid, 20 - 3"
    assert [c["name"] for c in final["hands"]["alice"]] == [
        "Mountain",
        "Forest",
        "Grizzly Bears",
    ], "3 life paid drew 3 cards"


def test_pay_life_amount_rejects_paying_more_life_than_the_player_has(client, game):
    """CR 119.4: "only if their life total is greater than or equal to the
    amount of the payment" - trying to pay more life than alice actually has
    is illegal. `decisions.validate_answer` refuses it synchronously, in
    this request, using the same 0..amount range `Game.ask` itself checks -
    never silently clamped to the maximum, and never posted to the mailbox
    for the resolving thread to discover late (see
    `test_an_engine_refused_answer_is_refused_synchronously_not_recorded_allowed`
    in `test_decisions.py` for the general regression this is one instance
    of). The decision is still pending afterwards, so a legal answer
    following the illegal one still lets play proceed."""
    add_card(client, game, "Necrodominance", "alice", "battlefield")
    stock_library_top(client, game, "alice", ["Mountain", "Forest", "Grizzly Bears"])
    uids = seats_for(client, game)
    reach_end_step_with_necrodominance_on_the_stack(client, game)

    resolve_top(game)
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["amount"] == 20

    illegal = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "life_amount": 25},
    )
    assert illegal.status_code == 200
    body = illegal.json()
    assert body["accepted"] is False
    assert "cannot pay 25 life" in body["reason"]
    still_pending = state(client, game, uid=uids["alice"])
    assert still_pending.get("pending_decision") is not None, "a refused answer must not consume it"

    legal = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "life_amount": 0},
    )
    assert legal.status_code == 200, legal.text
    assert legal.json()["accepted"] is True

    final = state(client, game)
    assert life_of(final, "alice") == 20, "an illegal payment is never taken, and 0 paid nothing"


# -- pay_mana: "unless its controller pays {N}" (CR 118.12a) ----------------


def stage_mystical_dispute_targeting_bears(
    client: TestClient, game_id: str, floating: dict[str, int]
) -> str:
    """alice's Grizzly Bears goes on the stack, bob casts Mystical Dispute
    targeting it - "Counter target spell unless its controller pays {3}."
    (CR 118.12a) is asked of *alice*, Grizzly Bears' controller, not bob, who
    is casting the counterspell (see `Game.may_pay_mana`'s own docstring).
    `floating` stages alice's mana pool for the payment itself through the
    same `PUT .../mana-pool` staging this file already used before this
    task - a real card in place of the direct `Game.may_pay_mana` call this
    test made before T7b's pause-and-resume replaced the blocking mailbox a
    bare engine call could park a live decision on. Returns Grizzly Bears'
    id (its `.id` is stable across the hand-to-stack move, CR 400.7)."""
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Grizzly Bears", "alice", "hand")
    add_card(client, game_id, "Island", "bob", "battlefield")
    add_card(client, game_id, "Island", "bob", "battlefield")
    add_card(client, game_id, "Island", "bob", "battlefield")
    add_card(client, game_id, "Mystical Dispute", "bob", "hand")
    board = state(client, game_id)
    forests = [c["id"] for c in board["battlefield"] if c["name"] == "Forest"]
    islands = [c["id"] for c in board["battlefield"] if c["name"] == "Island"]
    bears = board["hands"]["alice"][0]["id"]
    dispute = board["hands"]["bob"][0]["id"]

    cast_bears = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={"player": "alice", "card_id": bears, "mana_sources": forests},
    )
    assert cast_bears.status_code == 200, cast_bears.text
    # Alice keeps priority after her own cast (CR 117.3c) - pass it to bob
    # before he can cast Mystical Dispute in response, the same shape
    # `get_both_spells_on_the_stack` uses for Subtlety.
    passed = client.post(f"/api/games/{game_id}/actions/pass-priority", json={"player": "alice"})
    assert passed.status_code == 200, passed.text
    cast_dispute = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "bob",
            "card_id": dispute,
            "mana_sources": islands,
            "targets": [bears],
        },
    )
    assert cast_dispute.status_code == 200, cast_dispute.text
    client.put(f"/api/games/{game_id}/players/alice/mana-pool", json={"set": floating})
    return bears


def test_pay_mana_yes_spends_floating_mana(client, game):
    """This proves `decide` reuses `bool_answer` for `pay_mana` the same way
    it already does for `pay_life`/`pay_energy`, and that a "yes" really
    spends the mana."""
    uids = seats_for(client, game)
    stage_mystical_dispute_targeting_bears(client, game, {"R": 4})

    resolved = resolve_top(game)
    assert resolved["paused"] is True
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "pay_mana"
    assert pending["player"] == "alice"
    assert pending["amount"] == 3  # Mystical Dispute's own "unless its controller pays {3}"
    assert pending["affirmative_is_legal"] is True

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True
    resolve_top(game)  # Mystical Dispute is off the stack; Grizzly Bears, uncountered, resolves

    final = state(client, game)
    alice = next(p for p in final["players"] if p["username"] == "alice")
    assert alice["mana_pool"]["floating"].get("R", 0) == 1, "3 of the 4 floating {R} were spent"
    assert find(final["battlefield"], "Grizzly Bears")["zone"] == "battlefield", (
        "paid, not countered"
    )


def test_pay_mana_no_declines_and_leaves_mana_untouched(client, game):
    """The opposite answer: declining leaves the floating mana untouched -
    proving `decide` carries the chosen branch through, not just "any answer
    means paid", matching `test_pay_life_no_keeps_life_and_enters_tapped`."""
    uids = seats_for(client, game)
    stage_mystical_dispute_targeting_bears(client, game, {"R": 4})

    resolve_top(game)
    wait_for_pending(client, game, uids["alice"])
    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": False},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True

    final = state(client, game)
    alice = next(p for p in final["players"] if p["username"] == "alice")
    assert alice["mana_pool"]["floating"].get("R", 0) == 4, "declining spends nothing"
    assert final["graveyards"]["alice"][0]["name"] == "Grizzly Bears", "declined, so countered"


def test_pay_mana_below_the_amount_offers_no_yes(client, game):
    """CR 118.12a/118.3: a player short on floating mana cannot legally say
    yes - `affirmative_is_legal` must say so before the seat even answers,
    the same floor `pay_energy` enforces off energy instead of mana."""
    uids = seats_for(client, game)
    stage_mystical_dispute_targeting_bears(client, game, {"R": 2})

    resolve_top(game)
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["affirmative_is_legal"] is False
    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": False},
    )


def test_pay_mana_missing_accept_is_refused_not_defaulted(client, game):
    """A `decide` call for `pay_mana` with no `accept` must be refused by
    name, not silently defaulted, matching `pay_life`'s and `pay_energy`'s
    refusal of a missing `accept`."""
    uids = seats_for(client, game)
    stage_mystical_dispute_targeting_bears(client, game, {"R": 3})

    resolve_top(game)
    wait_for_pending(client, game, uids["alice"])
    refused = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice"},
    )
    assert refused.status_code == 200
    body = refused.json()
    assert body["accepted"] is False
    assert "yes/no" in body["reason"]

    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )


def test_the_caster_cannot_answer_a_pay_mana_decision_asked_of_the_opponent(client, game):
    """CR 118.12a asks the *targeted spell's controller*, not the resolving
    counterspell's caster - `bob` here is the one casting Mystical Dispute,
    but `alice` is the one being asked, so only alice may answer, matching
    `test_the_opponent_cannot_answer_a_pay_life_decision`."""
    uids = seats_for(client, game)
    stage_mystical_dispute_targeting_bears(client, game, {"R": 3})

    resolve_top(game)
    wait_for_pending(client, game, uids["alice"])
    refused = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["bob"]},
        json={"player": "bob", "accept": True},
    )
    assert refused.json()["accepted"] is False

    still_pending = state(client, game, uid=uids["alice"])
    assert still_pending.get("pending_decision") is not None

    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )


# -- choose_a_quality: "as this enters, choose a color" (CR 614.12a) --------


def test_choose_a_quality_picks_the_named_color(client, game):
    """The reported bug, exactly: Utopia Sprawl ("As this Aura enters, choose
    a color.") cast on the caster's own Forest. `decide` used to refuse every
    shape a live player tried, freezing the whole match behind this one
    `choose_a_quality` decision - a pending decision blocks the game until it
    is answered, not just the seat that raised it."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Utopia Sprawl", "alice", "hand")
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    forests = [c["id"] for c in board["battlefield"] if c["name"] == "Forest"]
    sprawl = board["hands"]["alice"][0]

    resolved = cast_and_resolve(game, "alice", sprawl["id"], [forests[0]], targets=[forests[1]])
    assert resolved["paused"] is True
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "choose_a_quality"
    assert set(pending["options"]) == {"W", "U", "B", "R", "G"}

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": "R"},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True

    final = state(client, game)
    # A `choose_a_quality` answer that never arrived used to be indistinguishable
    # from one that did in exactly this way: an unattached Aura is destroyed by
    # the next state-based action check (CR 704.5m) - `state_based_actions`
    # below proves the attach (and so the choice) really went through, the same
    # regression `test_utopia_sprawl_attaches_to_the_forest_it_targeted` in the
    # engine repo's own scenario tests guards against.
    sprawl_state = find(final["battlefield"], "Utopia Sprawl")
    assert sprawl_state["zone"] == "battlefield"
    sba = client.post(f"/api/games/{game}/state-based-actions").json()
    assert "Utopia Sprawl" not in sba["destroyed"]


def test_choose_a_quality_rejects_a_color_outside_the_five(client, game):
    """CR 608.2d: "The player can't choose an option that's illegal or
    impossible" - `pending_decision.options` narrows a color choice to the
    five colors (CR 105.1), so a made-up color must not silently become the
    choice.

    `decisions.validate_answer` refuses it synchronously, in this request -
    against the same `options` `Game.ask` itself checks - rather than
    letting it reach the mailbox and surface, late, on the resolving
    thread's side (see
    `test_an_engine_refused_answer_is_refused_synchronously_not_recorded_allowed`
    in `test_decisions.py`). The resolving thread stays parked on the same
    decision, so a legal color afterwards still lets the cast resolve."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Utopia Sprawl", "alice", "hand")
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    forests = [c["id"] for c in board["battlefield"] if c["name"] == "Forest"]
    sprawl = board["hands"]["alice"][0]

    cast_and_resolve(game, "alice", sprawl["id"], [forests[0]], targets=[forests[1]])
    wait_for_pending(client, game, uids["alice"])
    illegal = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": "not-a-color"},
    )
    assert illegal.status_code == 200
    body = illegal.json()
    assert body["accepted"] is False
    assert "not-a-color" in body["reason"]
    still_pending = state(client, game, uid=uids["alice"])
    assert still_pending.get("pending_decision") is not None, "a refused answer must not consume it"

    legal = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": "G"},
    )
    assert legal.status_code == 200, legal.text
    assert legal.json()["accepted"] is True


def test_choose_a_quality_missing_choice_is_refused_not_defaulted(client, game):
    """A `decide` call for `choose_a_quality` with no `choice` must be
    refused by name, matching `pay_life`'s refusal of a missing `accept`."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Utopia Sprawl", "alice", "hand")
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    forests = [c["id"] for c in board["battlefield"] if c["name"] == "Forest"]
    sprawl = board["hands"]["alice"][0]

    cast_and_resolve(game, "alice", sprawl["id"], [forests[0]], targets=[forests[1]])
    wait_for_pending(client, game, uids["alice"])
    refused = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice"},
    )
    assert refused.status_code == 200
    body = refused.json()
    assert body["accepted"] is False
    assert "choice" in body["reason"]

    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": "G"},
    )


# -- choose_a_mode: a modal triggered ability's "choose one -" (CR 603.3c,
# CR 700.2b) ------------------------------------------------------------


def declare_gargaroth_attacking(
    client: TestClient, game_id: str, uids: dict[str, str]
) -> dict[str, Any]:
    """Elder Gargaroth attacking bob triggers its own "attacks or blocks,
    choose one -" ability. CR 603.3c: "If a triggered ability is modal, its
    controller announces the mode choice when putting the ability on the
    stack" (and CR 700.2b says the same), so the `choose_a_mode` question is
    asked by the attack declaration itself, before the trigger reaches the
    stack - not later, when something resolves it. Returns the attack
    response, which carries that pause."""
    gargaroth = add_card(client, game_id, "Elder Gargaroth", "alice", "battlefield")["added"][
        "card_id"
    ]
    client.patch(f"/api/games/{game_id}/cards/{gargaroth}", json={"summoning_sick": False})
    client.put(f"/api/games/{game_id}/step", json={"step": "beginning_of_combat"})
    client.post(f"/api/games/{game_id}/advance", json={"player": "alice"})
    attack = client.post(
        f"/api/games/{game_id}/actions/attack",
        params={"uid": uids["alice"]},
        # No `player`: `AttackRequest` does not declare one, and the seat is
        # resolved from `uid`. Pydantic discarded the key silently before
        # `schemas.RequestModel`.
        json={"attackers": [{"card_id": gargaroth, "defender": "bob"}]},
    )
    assert attack.status_code == 200, attack.text
    body = attack.json()
    assert body["allowed"] is True, body
    return body


def assert_mode_trigger_waits_on_the_stack(client: TestClient, game_id: str) -> None:
    """After the mode is answered, the trigger is on the stack (CR 603.3c)
    and nothing has resolved yet - the chosen mode happens only when it
    resolves."""
    board = state(client, game_id)
    assert board.get("pending_decision") is None
    assert [(item["kind"], item["name"]) for item in board["stack"]] == [
        ("ability", "Elder Gargaroth")
    ], "the mode was chosen as the trigger went on the stack"
    assert life_of(board, "alice") == 20, "the chosen mode has not resolved yet"


def test_choose_a_mode_picks_the_named_mode(client, game):
    """Elder Gargaroth's attack trigger: "choose one - Create a 3/3 green
    Beast creature token. / You gain 3 life. / Draw a card." A
    `choose_a_mode` decision's answer is the mode's own printed text, out of
    `pending_decision.options` - this is the other live-hang case the report
    names, alongside Necrodominance's `pay_life_amount`. The question is
    asked while the trigger is being put on the stack (CR 603.3c), so the
    attack declaration is the call that pauses on it."""
    uids = seats_for(client, game)
    attack = declare_gargaroth_attacking(client, game, uids)

    assert attack["paused"] is True
    assert attack["pending_decision"]["kind"] == "choose_a_mode"
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "choose_a_mode"
    assert pending["player"] == "alice"
    assert state(client, game)["stack"] == [], "the trigger is not on the stack before its mode"
    gain_life_mode = next(mode for mode in pending["options"] if "gain 3 life" in mode)

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": gain_life_mode},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True
    assert_mode_trigger_waits_on_the_stack(client, game)

    resolved = resolve_top(game)
    assert resolved.get("paused") is not True, (
        "the mode was already chosen - resolution asks nothing"
    )
    assert resolved["pending_decision"] is None

    final = state(client, game)
    assert life_of(final, "alice") == 23, "the chosen mode - gain 3 life - actually happened"


def test_choose_a_mode_rejects_a_mode_that_was_not_offered(client, game):
    """CR 700.2a's printed modes are the only legal answers.
    `decisions.validate_answer` refuses a string that is not one of
    `pending_decision.options` synchronously, in this request, against the
    same `options` `Game.ask` itself checks - never silently substituting
    the first mode, never letting the mismatch surface late on the
    engine's side (see
    `test_an_engine_refused_answer_is_refused_synchronously_not_recorded_allowed`
    in `test_decisions.py`). The paused attack declaration stays parked on
    the same decision, so an offered mode afterwards still puts the trigger
    on the stack with that mode, and it resolves as that mode alone."""
    uids = seats_for(client, game)
    attack = declare_gargaroth_attacking(client, game, uids)

    assert attack["paused"] is True
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert set(pending["options"]) == {
        "Create a 3/3 green Beast creature token.",
        "You gain 3 life.",
        "Draw a card.",
    }

    illegal = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": "Deal 3 damage to any target."},
    )
    assert illegal.status_code == 200
    body = illegal.json()
    assert body["accepted"] is False
    assert "Deal 3 damage to any target." in body["reason"]
    still_pending = state(client, game, uid=uids["alice"])
    assert still_pending.get("pending_decision") is not None, "a refused answer must not consume it"
    assert still_pending["stack"] == [], "a refused answer must not put the trigger on the stack"

    legal = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": "You gain 3 life."},
    )
    assert legal.status_code == 200, legal.text
    assert legal.json()["accepted"] is True
    assert_mode_trigger_waits_on_the_stack(client, game)

    resolve_top(game)

    final = state(client, game)
    assert life_of(final, "alice") == 23, "the offered mode - gain 3 life - actually happened"
    assert [c["name"] for c in final["hands"]["alice"]] == [], "no card drawn either"
    assert [c["name"] for c in final["battlefield"]] == ["Elder Gargaroth"], (
        "no Beast token was created either"
    )


# -- put_on_top_or_bottom: Subtlety's ETB (CR 608.2d) ------------------------
#
# alice: Subtlety in hand, four Islands ({2}{U}{U}). bob: Grizzly Bears in
# hand, two Forests, and is the active player - his sorcery-speed Bears needs
# his own main phase, and Subtlety's flash (CR 702.8b) is what lets alice
# answer from outside her turn. This is the same board `test_up_to_one_target_
# spell.py` in the engine repo builds for the same card, so a `decide` bug
# here is not a bug the engine repo's own scenario tests would ever see -
# they script the answer directly rather than routing it through a live
# mailbox.
#
# The stack, once both spells are up, is [Grizzly Bears, Subtlety]. Resolving
# the top twice does the whole thing: first Subtlety resolves, entering the
# battlefield and putting its own ETB trigger on the stack - which itself
# blocks on a `choose_targets` decision for the trigger's target (`test_
# target_decisions.py`'s own mechanism, exercised again here because
# Subtlety's target has to be chosen before its `put_on_top_or_bottom`
# question can even be asked). Second, the trigger resolves and asks
# `put_on_top_or_bottom` - of *bob*, Grizzly Bears' owner, not alice, who
# controls the resolving trigger (CR 108.3).


def stock_subtlety_board(client: TestClient, game_id: str, *, uid: str | None = None) -> dict:
    add_card(client, game_id, "Grizzly Bears", "bob", "hand")
    add_card(client, game_id, "Forest", "bob", "battlefield")
    add_card(client, game_id, "Forest", "bob", "battlefield")
    add_card(client, game_id, "Subtlety", "alice", "hand")
    for _ in range(4):
        add_card(client, game_id, "Island", "alice", "battlefield")
    board = state(client, game_id, uid=uid)
    return {
        "bears": board["hands"]["bob"][0],
        "forests": [c["id"] for c in board["battlefield"] if c["name"] == "Forest"],
        "subtlety": board["hands"]["alice"][0],
        "islands": [c["id"] for c in board["battlefield"] if c["name"] == "Island"],
    }


def new_subtlety_game(client: TestClient) -> tuple[str, dict[str, str]]:
    """A fresh game with bob active in his own precombat main - `game`'s own
    fixture always starts alice active, which is wrong for this board: bob's
    Grizzly Bears is sorcery-speed and needs to be cast on his turn.

    Built directly rather than through the `game` fixture, so `state()`'s
    admin-key fallback is unavailable here - every `state()` call below must
    pass an explicit `uid`, an admin key stashed by the test, or accept a
    redacted (seat-scoped) view."""
    created = client.post(
        "/api/games",
        json={
            "players": [{"username": "alice"}, {"username": "bob"}],
            "active_player": "bob",
            "step": "precombat_main",
        },
    ).json()
    uids = dict(created["seat_uids"])
    uids["admin"] = created["admin_key"]
    return created["game_id"], uids


def get_both_spells_on_the_stack(client: TestClient, game_id: str, board: dict) -> None:
    """Bob casts Grizzly Bears, passes priority, and alice flashes in
    Subtlety in response - all foreground calls, since none of them raises a
    decision. Leaves the stack as [Grizzly Bears, Subtlety]."""
    cast_bears = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={"player": "bob", "card_id": board["bears"]["id"], "mana_sources": board["forests"]},
    )
    assert cast_bears.status_code == 200, cast_bears.text
    passed = client.post(f"/api/games/{game_id}/actions/pass-priority", json={"player": "bob"})
    assert passed.status_code == 200, passed.text
    cast_subtlety = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": board["subtlety"]["id"],
            "mana_sources": board["islands"],
        },
    )
    assert cast_subtlety.status_code == 200, cast_subtlety.text


def choose_subtletys_target(client: TestClient, game_id: str, uid: str, bears_id: str) -> None:
    """Answers the `choose_targets` decision Subtlety's own ETB trigger asks
    (of alice, its controller) before its `put_on_top_or_bottom` question can
    be asked at all - see the section docstring."""
    wait_for_pending(client, game_id, uid)
    answer = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uid},
        json={"player": "alice", "targets": [bears_id]},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True


def test_put_on_top_or_bottom_top_returns_the_spell_to_the_top(client):
    game_id, uids = new_subtlety_game(client)
    try:
        board = stock_subtlety_board(client, game_id, uid=uids["admin"])
        get_both_spells_on_the_stack(client, game_id, board)

        resolve_top(game_id)  # Subtlety resolves and enters
        choose_subtletys_target(client, game_id, uids["alice"], board["bears"]["id"])

        resolved = resolve_top(game_id)  # the ETB trigger resolves
        assert resolved["paused"] is True
        pending = wait_for_pending(client, game_id, uids["bob"])["pending_decision"]
        assert pending["kind"] == "put_on_top_or_bottom"
        assert pending["player"] == "bob", "Grizzly Bears' owner, not Subtlety's controller"

        answer = client.post(
            f"/api/games/{game_id}/decide",
            params={"uid": uids["bob"]},
            json={"player": "bob", "top": True},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["accepted"] is True

        final = state(client, game_id, uid=uids["admin"])
        assert final["libraries"]["bob"][0]["name"] == "Grizzly Bears"
        assert final["stack"] == []
    finally:
        client.delete(f"/api/games/{game_id}")


def test_put_on_top_or_bottom_bottom_returns_the_spell_to_the_bottom(client):
    game_id, uids = new_subtlety_game(client)
    try:
        board = stock_subtlety_board(client, game_id, uid=uids["admin"])
        get_both_spells_on_the_stack(client, game_id, board)

        resolve_top(game_id)
        choose_subtletys_target(client, game_id, uids["alice"], board["bears"]["id"])

        resolve_top(game_id)
        wait_for_pending(client, game_id, uids["bob"])
        answer = client.post(
            f"/api/games/{game_id}/decide",
            params={"uid": uids["bob"]},
            json={"player": "bob", "top": False},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["accepted"] is True

        final = state(client, game_id, uid=uids["admin"])
        assert final["libraries"]["bob"][-1]["name"] == "Grizzly Bears"
    finally:
        client.delete(f"/api/games/{game_id}")


def test_put_on_top_or_bottom_missing_answer_is_refused_not_defaulted(client):
    """A `decide` call for `put_on_top_or_bottom` with no `top` must be
    refused by name, matching `pay_life`'s and `choose_a_quality`'s refusal of
    a missing answer - not silently defaulted to "top" (`default_answer`'s own
    engine-side fallback), which would hide exactly the same freeze this
    file's other kinds already guard against."""
    game_id, uids = new_subtlety_game(client)
    try:
        board = stock_subtlety_board(client, game_id, uid=uids["admin"])
        get_both_spells_on_the_stack(client, game_id, board)

        resolve_top(game_id)
        choose_subtletys_target(client, game_id, uids["alice"], board["bears"]["id"])

        resolve_top(game_id)
        wait_for_pending(client, game_id, uids["bob"])
        refused = client.post(
            f"/api/games/{game_id}/decide",
            params={"uid": uids["bob"]},
            json={"player": "bob"},
        )
        assert refused.status_code == 200
        body = refused.json()
        assert body["accepted"] is False
        assert "top" in body["reason"]

        # Still pending - the refusal above did not consume the question.
        still_pending = state(client, game_id, uid=uids["bob"])
        assert still_pending.get("pending_decision") is not None

        client.post(
            f"/api/games/{game_id}/decide",
            params={"uid": uids["bob"]},
            json={"player": "bob", "top": True},
        )
    finally:
        client.delete(f"/api/games/{game_id}")


def test_the_caster_cannot_answer_a_put_on_top_or_bottom_decision_asked_of_the_owner(client):
    """CR 108.3: alice controls the resolving Subtlety trigger, but bob owns
    the targeted Grizzly Bears, and it is bob's choice - alice trying to
    answer for him must be refused, matching `test_the_opponent_cannot_
    answer_a_pay_life_decision`'s shape for a decision asked of someone else."""
    game_id, uids = new_subtlety_game(client)
    try:
        board = stock_subtlety_board(client, game_id, uid=uids["admin"])
        get_both_spells_on_the_stack(client, game_id, board)

        resolve_top(game_id)
        choose_subtletys_target(client, game_id, uids["alice"], board["bears"]["id"])

        resolve_top(game_id)
        wait_for_pending(client, game_id, uids["bob"])
        refused = client.post(
            f"/api/games/{game_id}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "top": True},
        )
        assert refused.json()["accepted"] is False

        still_pending = state(client, game_id, uid=uids["bob"])
        assert still_pending.get("pending_decision") is not None

        client.post(
            f"/api/games/{game_id}/decide",
            params={"uid": uids["bob"]},
            json={"player": "bob", "top": True},
        )
    finally:
        client.delete(f"/api/games/{game_id}")


# -- order_triggers: CR 603.3b's "in any order they choose" -----------------
#
# The kind that made this file's own drift test fail: the engine grew
# `DecisionKind.ORDER_TRIGGERS` and `machines/decisions.ask_trigger_order`
# before anything asked it, and nothing in this API could answer either. The
# engine has since wired the ask site - `Game._put_pending_triggers_on_stack`
# hands each controller's own block of simultaneous triggers to
# `_order_triggers_by_controller`, which calls `ask_trigger_order` - so a live
# seat now reaches this question through an ordinary request, exactly the way
# the shockland above reaches `pay_life`. These two tests used to stand in for
# that by calling `ask_trigger_order` themselves on a background thread; they
# no longer need to, and do not. The advance below is a real one, the question
# is raised by the engine of its own accord in the middle of it, and the seat
# answers it through `POST /decide` - after which the *stack order* is the
# proof, not a value captured out of a thread.


def stage_two_simultaneous_upkeep_triggers(client: TestClient, game_id: str) -> None:
    """The board CR 603.3b's premise needs: two Delver of Secrets, both
    alice's, so one real untap-to-upkeep advance fires two "at the beginning of
    your upkeep" abilities in the same batch - "multiple abilities have
    triggered since the last time a player received priority". Both are one
    controller's, which is what makes the engine ask: the ordering is asked per
    controller, and only where that controller has two or more."""
    add_card(client, game_id, "Delver of Secrets", "alice", "battlefield")
    add_card(client, game_id, "Delver of Secrets", "alice", "battlefield")
    client.put(f"/api/games/{game_id}/step", json={"step": "untap", "player": "alice"})


def advance_to_upkeep(game_id: str, player: str = "alice") -> dict[str, Any]:
    """The advance that fires both triggers - the call that pauses on CR
    603.3b's ordering, returned directly."""
    response = TestClient(app).post(f"/api/games/{game_id}/advance", json={"player": player})
    assert response.status_code == 200, response.text
    return response.json()


def stack_ability_ids(game_id: str) -> list[str]:
    """The ids of what is on the stack, bottom first - the engine's own record
    of the order CR 603.3b settled."""
    return [str(obj.id) for obj in store.get(game_id).stack]


def test_order_triggers_puts_the_chosen_order_back_to_the_engine(client, game):
    """CR 603.3b: "each player, in APNAP order, puts each triggered ability
    they control on the stack in any order they choose". The engine raises the
    question itself, mid-advance, for alice's two simultaneous upkeep triggers;
    the seat answers with a permutation of the ability ids it was offered,
    bottom of the stack first, and that exact order is the order the abilities
    end up in on the stack - reversed from the order they triggered in, so this
    cannot pass by the default (which is the triggered order unchanged)
    standing in for an answer nobody gave."""
    uids = seats_for(client, game)
    stage_two_simultaneous_upkeep_triggers(client, game)

    advanced = advance_to_upkeep(game)
    assert advanced["paused"] is True
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["player"] == "alice"
    assert pending["kind"] == "order_triggers"
    offered = pending["cards"]
    assert len(offered) == 2, f"expected two simultaneous upkeep triggers, got {offered}"

    chosen = list(reversed(offered))
    assert chosen != offered, "the chosen order must differ from the triggered order"
    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "order": chosen},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True, answer.text
    assert stack_ability_ids(game) == chosen


def test_order_triggers_refuses_an_order_that_is_not_a_permutation(client, game):
    """Dropping one of the offered abilities is CR 603.3's "its controller
    puts it on the stack" going unperformed, so it is refused - synchronously,
    to the seat that sent it, with the decision left pending so the same seat
    can answer again (which it then does, and the stack ends up in that second
    answer's order, proving the question really did survive the refusal)."""
    uids = seats_for(client, game)
    stage_two_simultaneous_upkeep_triggers(client, game)

    advance_to_upkeep(game)
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    offered = pending["cards"]
    assert len(offered) == 2, f"expected two simultaneous upkeep triggers, got {offered}"

    refused = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "order": offered[:1]},
    )
    assert refused.status_code == 200, refused.text
    body = refused.json()
    assert body["accepted"] is False
    assert "order exactly the abilities offered" in body["reason"]

    still_pending = state(client, game, uid=uids["alice"])["pending_decision"]
    assert still_pending is not None
    assert still_pending["kind"] == "order_triggers"

    chosen = list(reversed(offered))
    accepted = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "order": chosen},
    )
    assert accepted.json()["accepted"] is True, accepted.text
    assert stack_ability_ids(game) == chosen


# -- the drift test: every DecisionKind is answerable -----------------------


def test_every_decision_kind_is_answerable():
    """The regression test for the shape of the reported bug, not just this
    instance of it.

    `choose_a_quality`, `pay_energy`, and `pay_energy_amount` were added to
    `magic_cards_model.machines.decisions.DecisionKind` and never given a
    path through `decide` - the engine could ask the question, `Game.ask`
    called into this API's live provider exactly as designed, and `decide`
    refused every shape a real player tried, freezing the match. Nothing in
    either repo's own test suite could have caught that gap: the engine's
    suite has no idea this API exists, and this API's suite, before this
    test, only ever exercised the kinds someone remembered to write a test
    for.

    Before T7b, this enumerated every `DecisionKind` the engine defines and
    required each one to be named in `decisions.ANSWERABLE_KINDS` (a live
    seat can answer it through `decide`) or `decisions.SERVED_ELSEWHERE` (it
    is deliberately never routed here, with a note on where it *is* answered
    instead - `mulligan`, `declare_attackers`, `declare_blockers`). T7b's
    pause-and-resume gave every `DecisionKind` a real transport - even
    `declare_attackers`/`declare_blockers`, which used to bypass `Game.ask`
    entirely (see the module docstring) - so `SERVED_ELSEWHERE` is gone and
    the invariant is simpler: `ANSWERABLE_KINDS` now equals every
    `DecisionKind`, full stop. `decisions.py` already asserts this at import
    time, right under `ANSWERABLE_KINDS`'s own definition, so this test is
    now more a documented regression guard than a live check - it stays
    because the *next* `DecisionKind` still needs a place that fails loudly,
    by name, if `decide()`'s own branch for it is never written, rather than
    leaving the next author to rediscover the freeze in a live game the way
    this one was found."""
    from magic_cards_model.machines.decisions import DecisionKind

    orphaned = set(DecisionKind) - decisions.ANSWERABLE_KINDS
    assert not orphaned, (
        f"DecisionKind(s) {sorted(k.value for k in orphaned)} are asked by the engine "
        f"(magic_cards_model.machines.decisions.DecisionKind) but not in "
        f"decisions.ANSWERABLE_KINDS, so POST /api/games/{{id}}/decide does not know "
        f"how to answer them - a live seat asked one of these will see decide refuse "
        f"every answer it tries, and the decision will stay pending forever (T7b's "
        f"pause-and-resume has no timeout to fall back on). Fix: wire an answer path "
        f"for it in app.py's decide() and add it to ANSWERABLE_KINDS."
    )


# T7b deleted two tests that used to live here:
#
# `test_an_unwired_kind_is_refused_by_name` published a hand-built
# `PendingDecision` straight to `store.mailbox(game_id)` to exercise
# `decide`'s refusal of a kind with no live transport (`declare_attackers`,
# before this task). `store.mailbox` is gone with the blocking machinery it
# belonged to, and there is no longer any `DecisionKind` with no live
# transport for the test to construct at all - so the scenario cannot be
# built any more, the same shape `tests/test_deadlock.py`'s deletion this
# session documents for an analogous reason.
#
# `test_log_timeout_wording_for_an_unwired_kind` called
# `GameDecisionProvider._log_timeout` directly (white-box) to check a timed-
# out decision's audit wording. `GameDecisionProvider` is deleted outright,
# and there is no concept of a decision "timing out" any more - a paused
# decision just stays paused, indefinitely, until answered - so the
# mechanism this test checked no longer exists.
#
# `decide`'s own unreachable-kind branch stays in `app.py`, still marked
# `# pragma: no cover - unreachable`, since `ANSWERABLE_KINDS` covering every
# `DecisionKind` is an invariant enforced by an `assert` in `decisions.py`,
# not a guarantee the type system gives for free.


# -- the seat CLI and the MCP tool, both driving the same shockland ----------


def test_the_seat_cli_answers_pay_life(monkeypatch, capsys) -> None:
    """The reported bug, end to end, through `seat_cli` exactly as a player
    would run it: `wait` shows the pay_life prompt, `decide --accept yes`
    pays it, and the land really does enter untapped for 18 life."""
    import httpx

    from magic_cards_model_api import seat_cli

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    http = TestClient(app)
    try:
        created = http.post(
            "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
        ).json()
        game_id = created["game_id"]
        uids = created["seat_uids"]
        try:
            add_card(http, game_id, "Steam Vents", "alice", "hand")
            land = state(http, game_id, uid=uids["alice"])["hands"]["alice"][0]

            played = play_steam_vents(game_id, land["id"])
            assert played["paused"] is True
            pending = played["pending_decision"]
            assert pending is not None and pending["kind"] == "pay_life"

            rc = seat_cli.run(["--uid", uids["alice"], "--game", game_id, "wait"])
            assert rc == 0
            assert "a decision is waiting for you" in capsys.readouterr().out

            rc = seat_cli.run(
                [
                    "--uid",
                    uids["alice"],
                    "--game",
                    game_id,
                    "decide",
                    "--accept",
                    "yes",
                    "--hold",
                ]
            )
            assert rc == 0
            printed = json.loads(capsys.readouterr().out)
            assert printed["outcome"]["accepted"] is True

            final = http.get(f"/api/games/{game_id}", params={"uid": uids["alice"]}).json()
            assert life_of(final, "alice") == 18
            assert find(final["battlefield"], "Steam Vents")["tapped"] is False
        finally:
            http.delete(f"/api/games/{game_id}", params={"key": created["admin_key"]})
    finally:
        http.close()


def test_the_mcp_tool_answers_pay_life(monkeypatch) -> None:
    """The same shockland, through the MCP `decide` tool that agents actually
    call - proving `mcp.py` and `seat_cli.py` agree, both on the request shape
    and on the outcome."""
    import asyncio

    import httpx

    from magic_cards_model_api import mcp
    from magic_cards_model_api.seatkit import Seats

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    http = TestClient(app)
    try:
        created = http.post(
            "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
        ).json()
        game_id = created["game_id"]
        uids = created["seat_uids"]
        try:
            add_card(http, game_id, "Steam Vents", "alice", "hand")
            land = state(http, game_id, uid=uids["alice"])["hands"]["alice"][0]

            played = play_steam_vents(game_id, land["id"])
            assert played["paused"] is True
            pending = played["pending_decision"]
            assert pending is not None and pending["kind"] == "pay_life"

            seats = Seats(api="http://testserver", game_id=game_id)
            server = mcp.build_server(seats)
            result = asyncio.run(
                server.call_tool(
                    "decide",
                    {"player_uid": uids["alice"], "accept": False, "hold_priority": True},
                )
            )
            payload = json.loads(result.content[0].text)
            assert payload["outcome"]["accepted"] is True

            final = http.get(f"/api/games/{game_id}", params={"uid": uids["alice"]}).json()
            assert life_of(final, "alice") == 20
            assert find(final["battlefield"], "Steam Vents")["tapped"] is True
        finally:
            http.delete(f"/api/games/{game_id}", params={"key": created["admin_key"]})
    finally:
        http.close()


# -- M2: every pending decision says how to answer it, in one call ----------
#
# The two gaps this section pins were both measured in the live cluster's
# `records` table, as refusals a seat hit while holding a perfectly readable
# `pending_decision`:
#
#   - "this decision needs a yes/no answer ('accept')" (`decisions.bool_answer`,
#     2 occurrences) - the seat had the question but not the mapping from its
#     `kind` to the parameter that answers it, which lived only in `decide`'s
#     ~200-line docstring;
#   - "no pending decision for you" (`app.py`'s `decide`, 11 occurrences) - a
#     stale read answered against a question that had already been replaced.
#     `DecideRequest.generation` has always had the far better diagnostic for
#     that, and the MCP tool simply had no parameter to send it with.
#
# `answer_with`/`answer_field` close the first and `decide(generation=...)`
# closes the second. Both are drift-guarded here, off the same exhaustiveness
# tables `decisions.py` asserts at import time.


def _tool_contract() -> dict[str, set[str]]:
    """Every MCP tool in `contract/tools.json`, by name, with its real
    parameter names - the artifact `mise run tools:export` writes from
    `mcp.py`'s own `@server.tool()` registrations.

    Checking `answer_with` against this rather than against a list retyped
    here is the point: a template that names a parameter the tool does not
    take is exactly the "how" thinking this feature exists to remove, and it
    would be worse than no template at all.
    """
    contract = Path(__file__).resolve().parent.parent / "contract" / "tools.json"
    data = json.loads(contract.read_text())
    return {tool["name"]: {param["name"] for param in tool["parameters"]} for tool in data["tools"]}


_KWARG = re.compile(r"(\w+)=(true|false|-?\d+|\"[^\"]*\"|'[^']*'|<[^>]*>)")


def parse_answer_with(answer_with: str) -> tuple[str, dict[str, Any]]:
    """One `answer_with` template, split into the tool name and the keyword
    arguments it names - as a seat pasting it would send them.

    Values are decoded the way the tool's own signature would take them:
    `true`/`false` to `bool`, a bare integer to `int`, a quoted span to the
    string inside the quotes (placeholder text included - a template for a
    kind whose answer is a choice cannot be concrete, and that is fine; what
    must be concrete is the *parameter name* and the generation)."""
    tool, _, rest = answer_with.partition("(")
    assert rest.endswith(")"), f"not a call template: {answer_with!r}"
    kwargs: dict[str, Any] = {}
    for name, raw in _KWARG.findall(rest[:-1]):
        if raw == "true":
            kwargs[name] = True
        elif raw == "false":
            kwargs[name] = False
        elif raw[0] in "\"'":
            kwargs[name] = raw[1:-1]
        elif raw[0] == "<":
            # An angle-bracket placeholder for a choice the decision cannot
            # make for the seat - a bare one where the parameter is an `int`
            # or a `bool`, so the template is the shape of the real call.
            kwargs[name] = raw
        else:
            kwargs[name] = int(raw)
    return tool, kwargs


def a_pending_decision(kind: Any, **overrides: Any) -> decisions.PendingDecision:
    """A `PendingDecision` of `kind`, filled in enough to be viewable.

    Built directly rather than played out of a real board on purpose: this is
    the *template* surface, which is a pure function of the decision's own
    fields, and there is no board state that could make a `kind` map to a
    different parameter. The behaviour that does need a real board - a `may`
    answered by pasting its own template, a stale generation refused by the
    route - is driven end to end below, through the live API.
    """
    fields: dict[str, Any] = {
        "player": "alice",
        "kind": kind.value,
        "prompt": "a question",
        "source": "Some Card",
        "amount": 1,
        "cards": ("11111111-1111-1111-1111-111111111111",),
        "generation": 7,
    }
    fields.update(overrides)
    return decisions.PendingDecision(**fields)


def test_the_tool_field_table_covers_every_decision_kind():
    """`decisions.TOOL_FIELD_FOR_KIND` is the second exhaustiveness guard on
    this surface, beside `ANSWER_FIELD_FOR_KIND`: a `DecisionKind` added to
    the engine with no entry here would ship a pending decision that cannot
    say how to answer itself, which is the whole gap this table closes.

    `decisions.py` asserts the same equality at import time; this test is
    what names the missing kind out loud when it fails."""
    from magic_cards_model.machines.decisions import DecisionKind

    missing = set(DecisionKind) - set(decisions.TOOL_FIELD_FOR_KIND)
    assert not missing, (
        f"DecisionKind(s) {sorted(k.value for k in missing)} have no entry in "
        f"decisions.TOOL_FIELD_FOR_KIND, so a seat asked one of them gets a "
        f"pending_decision with no answer_with template and is back to reading "
        f"decide()'s docstring to work out which parameter answers it."
    )
    assert set(decisions.TOOL_FOR_KIND) == set(DecisionKind)


def test_the_two_field_tables_genuinely_differ():
    """The reason this is emitted as data rather than derived from
    `ANSWER_FIELD_FOR_KIND`: the two tables disagree on real kinds.

    `ANSWER_FIELD_FOR_KIND` names the engine `Answer` field (`split`, `text`,
    `pairs`, `amount`, and `accept` even for a top/bottom choice); the MCP
    tool takes `moved`/`top_order`, `choice`, `energy_amount`/`life_amount`,
    `top`, and does not take `pairs` at all - it routes the two combat
    declarations to their own tools. Deriving one from the other would send
    a seat to a parameter that does not exist."""
    differing = {
        kind.value
        for kind in decisions.TOOL_FIELD_FOR_KIND
        if decisions.TOOL_FIELD_FOR_KIND[kind] != decisions.ANSWER_FIELD_FOR_KIND[kind]
    }
    # Pinned by name, not just counted: `decisions.py`'s own table of
    # divergences is prose next to the data, and prose drifts. If a kind
    # joins or leaves this set, that comment needs the same edit.
    assert differing == {
        "scry",
        "surveil",
        "choose_a_quality",
        "choose_a_mode",
        "pay_energy_amount",
        "pay_life_amount",
        "put_on_top_or_bottom",
        "order_triggers",
        "declare_attackers",
        "declare_blockers",
    }
    assert {kind: "decide" for kind in decisions.TOOL_FOR_KIND} != decisions.TOOL_FOR_KIND


def test_answer_with_names_this_kinds_real_tool_parameter():
    """A test per `DecisionKind`, driven off the exhaustiveness table so a
    new kind fails loudly rather than quietly shipping a template-less
    decision.

    For each kind: the view carries `answer_field` and `answer_with`, the
    field is the one `TOOL_FIELD_FOR_KIND` names, the template calls the tool
    `TOOL_FOR_KIND` names, that tool really exists in `contract/tools.json`,
    and *every* keyword the template names is a real parameter of it."""
    from magic_cards_model.machines.decisions import DecisionKind

    tools = _tool_contract()
    for kind in DecisionKind:
        view = a_pending_decision(kind).to_view()
        field = decisions.TOOL_FIELD_FOR_KIND[kind]
        expected_tool = decisions.TOOL_FOR_KIND[kind]
        assert view["answer_field"] == field, kind
        tool, kwargs = parse_answer_with(view["answer_with"])
        assert tool == expected_tool, (kind, view["answer_with"])
        assert tool in tools, (kind, tool)
        assert field in kwargs, (kind, view["answer_with"])
        unknown = set(kwargs) - tools[tool]
        assert not unknown, (
            f"{kind.value}'s answer_with names {sorted(unknown)}, which "
            f"{tool}() does not take (contract/tools.json)"
        )


def test_answer_with_names_this_decisions_own_generation():
    """The template is paste-ready for *this* question, not a generic one:
    it carries the generation the seat just read, so pasting it is also
    opting into the stale-answer check."""
    from magic_cards_model.machines.decisions import DecisionKind

    for kind in DecisionKind:
        view = a_pending_decision(kind, generation=41).to_view()
        _, kwargs = parse_answer_with(view["answer_with"])
        if decisions.TOOL_FOR_KIND[kind] == "decide":
            assert kwargs.get("generation") == 41, (kind, view["answer_with"])
        else:
            # `declare_attackers`/`declare_blockers` take no `generation` -
            # they are the two kinds `decide` cannot answer from the tool
            # surface at all, so the template must not invent a parameter.
            assert "generation" not in kwargs, (kind, view["answer_with"])


def test_a_may_decisions_template_names_accept_so_the_measured_refusal_is_unreachable(client, game):
    """The specific measured failure, prevented.

    `decisions.bool_answer` refuses a `may` answered without `accept` with
    "this decision needs a yes/no answer ('accept')" - 2 live occurrences in
    the cluster's records table, a seat holding the question and guessing at
    the parameter. A seat that pastes `answer_with` sends `accept`, so it
    cannot reach that branch: this drives the real Malevolent Rumble `may`
    through the live API, answering it with nothing but the template's own
    keywords."""
    staged = stage_malevolent_rumble(client, game)
    uids = seats_for(client, game)

    resolved = cast_and_resolve(game, "alice", staged["rumble"], staged["forests"])
    assert resolved["paused"] is True
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "may"
    assert pending["answer_field"] == "accept"

    tool, kwargs = parse_answer_with(pending["answer_with"])
    assert tool == "decide"
    assert isinstance(kwargs["accept"], bool)
    assert kwargs["generation"] == pending["generation"]

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", **kwargs},
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["accepted"] is True, body
    assert "needs a yes/no answer" not in json.dumps(body)


def test_the_next_questions_template_carries_the_new_generation(client, game):
    """Answering one question hands the seat the next one, and the next one's
    template names the *new* generation - which is what makes pasting it
    safe. This is the two-decision chain (`may`, then `choose_cards`) the
    generation counter exists for."""
    staged = stage_malevolent_rumble(client, game)
    uids = seats_for(client, game)

    cast_and_resolve(game, "alice", staged["rumble"], staged["forests"])
    first = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    _, kwargs = parse_answer_with(first["answer_with"])

    body = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", **kwargs},
    ).json()
    following = body["pending_decision"]
    assert following["kind"] == "choose_cards"
    assert following["answer_field"] == "card_ids"
    assert following["generation"] > first["generation"]
    _, next_kwargs = parse_answer_with(following["answer_with"])
    assert next_kwargs["generation"] == following["generation"]


def test_an_action_answers_lifted_pending_decision_carries_the_template(client, game):
    """The lifted, top-level `pending_decision` an action's own answer
    carries (`mcp._action_answer`, and the `input_required` result built off
    it) is the copy an agent actually reads - so it must carry the template
    too, not only the state view."""
    from magic_cards_model_api import mcp

    staged = stage_malevolent_rumble(client, game)
    uids = seats_for(client, game)
    cast_and_resolve(game, "alice", staged["rumble"], staged["forests"])
    view = wait_for_pending(client, game, uids["alice"])

    answer = mcp._action_answer({"outcome": {"accepted": True}, "state": view})
    lifted = answer["pending_decision"]
    assert lifted["answer_field"] == "accept"
    assert lifted["answer_with"].startswith("decide(")


def test_the_opponents_redacted_view_carries_no_template(client, game):
    """CR 101.4a's shrink is unchanged: a seat that is not the deciding one
    still sees `{player, kind, prompt}` and nothing else - the template is a
    tool for the seat being asked, not a wider payload for everyone."""
    staged = stage_malevolent_rumble(client, game)
    uids = seats_for(client, game)
    cast_and_resolve(game, "alice", staged["rumble"], staged["forests"])

    seen = state(client, game, uid=uids["bob"])["pending_decision"]
    assert set(seen) == {"player", "kind", "prompt"}


# -- generation, on the MCP decide tool -------------------------------------


def _mcp_decide(seats: Any, **kwargs: Any) -> str:
    import asyncio

    from magic_cards_model_api import mcp

    server = mcp.build_server(seats)
    result = asyncio.run(server.call_tool("decide", kwargs))
    # A call that leaves a decision still standing for this seat comes back
    # as MCP 2026-07-28's `input_required` shape, which carries its JSON in
    # `request_state` rather than in `content` (`mcp._tool_result`). Both are
    # the same text; this helper is about the tool's answer, not its wire
    # shape, so it reads whichever one is there.
    if hasattr(result, "request_state"):
        return str(result.request_state)
    return str(result.content[0].text)


def test_generation_reaches_the_route_from_the_mcp_tool(client, game, monkeypatch):
    """`DecideRequest.generation` has been enforced server-side since it was
    added, and `seat_cli` has exposed `--generation` for as long - the MCP
    tool simply had no such parameter, so the diagnostic was unreachable from
    the one surface agents actually call. Sending the *right* generation is
    accepted."""
    import httpx

    from magic_cards_model_api.seatkit import Seats

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    staged = stage_malevolent_rumble(client, game)
    uids = seats_for(client, game)
    cast_and_resolve(game, "alice", staged["rumble"], staged["forests"])
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]

    seats = Seats(api="http://testserver", game_id=game)

    # A generation that has never existed, so the only way the route can
    # name it back is if the tool's own parameter carried it there.
    impossible = pending["generation"] + 99
    refused = _mcp_decide(
        seats,
        player_uid=uids["alice"],
        accept=True,
        generation=impossible,
        hold_priority=True,
    )
    assert f"that answer names generation {impossible}" in refused, refused

    # And the real one goes through: the `may` is accepted and the
    # `choose_cards` that follows it becomes the standing question.
    text = _mcp_decide(
        seats,
        player_uid=uids["alice"],
        accept=True,
        generation=pending["generation"],
        hold_priority=True,
    )
    payload = json.loads(text)
    # `outcome` is `None` here on purpose - `seatkit.run_action` reports that
    # when the action paused again on a decision it could not see through,
    # which is exactly what accepting this `may` does (the follow-up
    # `choose_cards`). The proof the answer landed is that follow-up.
    assert payload["pending_decision"]["kind"] == "choose_cards", text
    assert payload["pending_decision"]["generation"] > pending["generation"]


def test_a_stale_generation_from_the_mcp_tool_is_refused_by_generation(client, game, monkeypatch):
    """The 11 measured "no pending decision for you" refusals, converted into
    the diagnostic one.

    The `may` is answered first, which replaces the standing question with
    the `choose_cards` that follows it and bumps the generation. Answering
    against the generation the seat first read is now refused *by name* -
    "that answer names generation N, but this game's standing question is
    generation M" - instead of a refusal that says nothing about why."""
    import httpx

    from magic_cards_model_api.seatkit import Seats

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    staged = stage_malevolent_rumble(client, game)
    uids = seats_for(client, game)
    cast_and_resolve(game, "alice", staged["rumble"], staged["forests"])
    stale = wait_for_pending(client, game, uids["alice"])["pending_decision"]["generation"]

    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True, "generation": stale},
    )

    seats = Seats(api="http://testserver", game_id=game)
    text = _mcp_decide(
        seats,
        player_uid=uids["alice"],
        card_ids=staged["wall"],
        generation=stale,
        hold_priority=True,
    )
    assert f"that answer names generation {stale}" in text, text
    assert "no pending decision for you" not in text
