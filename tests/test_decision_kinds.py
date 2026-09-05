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
window. `test_every_decision_kind_is_answerable_or_served_elsewhere` below is
the regression test for the *shape* of this bug, not just this instance of
it: it enumerates the engine's own `DecisionKind` members and fails, by name,
the moment a new one is added without a matching entry in
`decisions.ANSWERABLE_KINDS` or `decisions.SERVED_ELSEWHERE` - so the next
`DecisionKind` cannot silently freeze a game the same way.

`declare_attackers` and `declare_blockers` are the two `DecisionKind`s
deliberately not covered here with a live-seat test: reading
`machines/decisions.py` shows `ask_attackers` and `ask_blockers` take their
answer straight off `DecisionScript.take`, never touching
`Game.decisions.provider` (the mailbox this API publishes to) at all. No
request through this API can ever make one of them arrive at `POST /decide` -
and neither needs to, since both already have their own actions
(`actions/attack`, `actions/declare-blockers`) calling
`Game.declare_attackers`/`Game.declare_blockers` directly.
`OPENING_HAND_ACTION` (CR 103.6, through `Game.take_opening_hand_actions`'s
`self.ask`) and `MULLIGAN` (CR 103.5, which `ask_mulligan` puts to the
provider from `Game.settle_pregame`) are *not* in this group: both reach the
mailbox and both are answerable by a live seat - see `tests/test_mulligans.py`,
which drives each of them end to end.
`test_an_unwired_kind_is_refused_by_name`
below proves `decide`'s refusal path for them the only way it can be
proved - by publishing one by hand to the mailbox, the way a future
`DecisionKind` would arrive if a caller forgot to update
`decisions.ANSWERABLE_KINDS` and `app.py`'s `decide` branch together.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from conftest import add_card, find, life_of, state
from fastapi.testclient import TestClient
from magic_cards_model.machines.decisions import Decision, DecisionKind

from magic_cards_model_api import decisions
from magic_cards_model_api.decisions import PendingDecision
from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def set_short_timeout(game_id: str, seconds: float) -> None:
    provider = store.get(game_id).decisions.provider
    assert provider is not None
    provider.timeout_seconds = seconds


def run_in_background(post: Any) -> threading.Thread:
    """Fire one blocking POST on its own thread and return it, still running."""
    thread = threading.Thread(target=post)
    thread.start()
    thread.join(timeout=0.5)
    assert thread.is_alive(), "expected this call to block on a CR 608.2d question"
    return thread


def wait_for_pending(client: TestClient, game_id: str, uid: str, tries: int = 300) -> dict:
    for _ in range(tries):
        view = state(client, game_id, uid=uid)
        if view.get("pending_decision") is not None:
            return view
        time.sleep(0.01)
    raise AssertionError("no pending decision showed up in time")


# -- pay_life: the reported case, a shockland (CR 614.12a, CR 119.4) --------


def play_steam_vents_in_background(
    game_id: str, land_id: str, player: str = "alice"
) -> threading.Thread:
    def post() -> None:
        stepping_client = TestClient(app)
        stepping_client.post(
            f"/api/games/{game_id}/actions/play-land",
            json={"player": player, "card_id": land_id},
        )

    return run_in_background(post)


def test_pay_life_yes_pays_life_and_enters_untapped(client, game):
    """CR 119.4: paying is legal at 20 life, so "yes" is offered and, once
    taken, both halves of the choice happen - life goes down by 2 AND the
    land enters untapped (CR 614.12a: the choice is made *before* it enters,
    but the entering permanent's tapped state is what the choice was for)."""
    add_card(client, game, "Steam Vents", "alice", "hand")
    uids = seats_for(client, game)
    land = state(client, game, uid=uids["alice"])["hands"]["alice"][0]

    thread = play_steam_vents_in_background(game, land["id"])
    try:
        pending_view = wait_for_pending(client, game, uids["alice"])
        pending = pending_view["pending_decision"]
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
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

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

    thread = play_steam_vents_in_background(game, land["id"])
    try:
        wait_for_pending(client, game, uids["alice"])
        answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "accept": False},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

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

    thread = play_steam_vents_in_background(game, land["id"])
    try:
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

        # let the real answer through so the thread does not linger
        client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "accept": True},
        )
    finally:
        thread.join(timeout=10)


def test_pay_life_at_too_little_life_offers_no_yes(client, game):
    """CR 119.4: below the amount, paying is not a real option -
    `affirmative_is_legal` must say so, before the seat even answers, and
    `decide` must refuse an attempt to say yes anyway rather than letting an
    illegal payment through."""
    add_card(client, game, "Steam Vents", "alice", "hand")
    uids = seats_for(client, game)
    client.patch(f"/api/games/{game}/players/alice", json={"life": 1})
    land = state(client, game, uid=uids["alice"])["hands"]["alice"][0]

    thread = play_steam_vents_in_background(game, land["id"])
    try:
        pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
        assert pending["affirmative_is_legal"] is False

        # `decide` hands `True` to the engine, which is `Game.ask`'s own job
        # to refuse (CR 608.2d: "can't choose an option that's illegal") -
        # that refusal surfaces as a 500 in *this* request, not in `decide`'s,
        # because `Game.ask` runs on the thread blocked inside `play-land`,
        # not on this one (see the module docstring on `decisions.py`).
        client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "accept": False},
        )
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

    final = state(client, game, uid=uids["alice"])
    assert life_of(final, "alice") == 1, "an illegal payment is never taken"
    assert find(final["battlefield"], "Steam Vents")["tapped"] is True


def test_the_opponent_cannot_answer_a_pay_life_decision(client, game):
    add_card(client, game, "Steam Vents", "alice", "hand")
    uids = seats_for(client, game)
    land = state(client, game, uid=uids["alice"])["hands"]["alice"][0]

    thread = play_steam_vents_in_background(game, land["id"])
    try:
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
    finally:
        thread.join(timeout=10)


def test_the_opponent_cannot_see_the_pay_life_prompt_beyond_kind_and_player(client, game):
    add_card(client, game, "Steam Vents", "alice", "hand")
    uids = seats_for(client, game)
    land = state(client, game, uid=uids["alice"])["hands"]["alice"][0]

    thread = play_steam_vents_in_background(game, land["id"])
    try:
        wait_for_pending(client, game, uids["alice"])
        bobs_view = state(client, game, uid=uids["bob"])
        pending = bobs_view["pending_decision"]
        assert pending == {"player": "alice", "kind": "pay_life", "prompt": "pay 2 life"}

        client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "accept": True},
        )
    finally:
        thread.join(timeout=10)


def test_pay_life_timeout_logs_did_not_answer_not_could_not(client, game):
    """`pay_life` is in `decisions.ANSWERABLE_KINDS` - a seat really could
    have answered this one through `decide`. A timeout here must say the
    player *did not* answer, distinct from a kind nobody could ever answer
    (see the module docstring and `test_log_timeout_wording_for_an_unwired_kind`)."""
    add_card(client, game, "Steam Vents", "alice", "hand")
    uids = seats_for(client, game)
    land = state(client, game, uid=uids["alice"])["hands"]["alice"][0]
    set_short_timeout(game, 0.2)

    # The provider's own timeout (0.2s) is shorter than the usual 0.5s poll
    # used elsewhere to prove a call is blocked, so the play-land call may
    # already have returned, already defaulted, by the time that poll would
    # run - the same shape `test_decisions.py`'s own timeout test documents.
    results: list[Any] = []

    def post() -> None:
        stepping_client = TestClient(app)
        results.append(
            stepping_client.post(
                f"/api/games/{game}/actions/play-land",
                json={"player": "alice", "card_id": land["id"]},
            )
        )

    thread = threading.Thread(target=post)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive(), "the timeout should have released the request by now"
    assert results[0].status_code == 200, results[0].text

    final = state(client, game, uid=uids["alice"])
    assert life_of(final, "alice") == 18, "the documented default is to pay - CR 119.4's own text"
    assert find(final["battlefield"], "Steam Vents")["tapped"] is False

    log = client.get(f"/api/games/{game}/log", params={"uid": uids["alice"]}).json()["log"]
    entry = next(e for e in log if e["action"] == "decision_timeout")
    assert entry["detail"]["kind"] == "pay_life"
    assert entry["detail"]["could_answer"] is True


# -- scry / surveil: which cards move, which stay, in what order ------------


def stock_library_top(client: TestClient, game_id: str, player: str, names: list[str]) -> None:
    """`names[0]` ends up on top, `names[-1]` deepest of the ones named -
    `put_card_in_zone` inserts each new library addition at index 0, so
    adding in reverse order leaves them in the order given."""
    for name in reversed(names):
        add_card(client, game_id, name, player, "library")


def cast_and_resolve_in_background(
    game_id: str, player: str, card_id: str, sources: list[str], targets: list[str] | None = None
) -> threading.Thread:
    def post() -> None:
        stepping_client = TestClient(app)
        cast = stepping_client.post(
            f"/api/games/{game_id}/actions/cast",
            json={
                "player": player,
                "card_id": card_id,
                "mana_sources": sources,
                **({"targets": targets} if targets is not None else {}),
            },
        )
        assert cast.status_code == 200, cast.text
        stepping_client.post(f"/api/games/{game_id}/resolve-top")

    return run_in_background(post)


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

    thread = cast_and_resolve_in_background(game, "alice", preordain["id"], [island["id"]])
    try:
        pending_view = wait_for_pending(client, game, uids["alice"])
        pending = pending_view["pending_decision"]
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
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

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

    thread = cast_and_resolve_in_background(game, "alice", preordain["id"], [island["id"]])
    try:
        wait_for_pending(client, game, uids["alice"])
        client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "moved": [], "top_order": [mountain["id"], forest["id"]]},
        )
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

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

    thread = cast_and_resolve_in_background(
        game, "alice", plan["id"], [island["id"], red_source["id"]]
    )
    try:
        pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
        assert pending["kind"] == "surveil"
        assert pending["amount"] == 5

        client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "moved": [mountain["id"]], "top_order": [forest["id"]]},
        )
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

    final = state(client, game, uid=uids["alice"])
    admin_final = state(client, game)
    graveyard_names = {c["name"] for c in final["graveyards"]["alice"]}
    assert "Mountain" in graveyard_names, "the named card went to the graveyard, CR 701.25a"
    assert admin_final["libraries"]["alice"][0]["name"] == "Forest"


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

    thread = cast_and_resolve_in_background(
        game, "alice", plan["id"], [island["id"], red_source["id"]]
    )
    try:
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
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()


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

    thread = cast_and_resolve_in_background(
        game, "alice", plan["id"], [island["id"], red_source["id"]]
    )
    try:
        wait_for_pending(client, game, uids["alice"])
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
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()


# -- may: the other yes/no kind, sharing `decisions.bool_answer` ------------


def test_may_shares_the_same_bool_answer_path_as_pay_life(client, game):
    """No real card in the pool's registered `EFFECTS` resolves a bare `you
    may <something already covered>` clause cleanly enough to stage without
    a target, so this calls `Game.may` the same way `machines/effects.py`'s
    `_optional_clause` does for any such card - proving `decide` answers
    `MAY`, not just `PAY_LIFE`, without re-deriving a whole spell for a
    question `bool_answer` handles identically either way."""
    uids = seats_for(client, game)

    def ask_in_background(outcome: list[bool]) -> threading.Thread:
        def call() -> None:
            with store.locked(game) as live_game:
                outcome.append(live_game.may("alice", "you may draw a card"))

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive()
        return thread

    for accept in (True, False):
        outcome: list[bool] = []
        thread = ask_in_background(outcome)
        try:
            pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
            assert pending["kind"] == "may"
            answer = client.post(
                f"/api/games/{game}/decide",
                params={"uid": uids["alice"]},
                json={"player": "alice", "accept": accept},
            )
            assert answer.json()["accepted"] is True
        finally:
            thread.join(timeout=10)
        assert not thread.is_alive()
        assert outcome == [accept]


# -- pay_energy: the fixed-amount cousin of pay_life (CR 107.14, CR 118.3) --


def test_pay_energy_yes_spends_the_energy(client, game):
    """`Game.may_pay_energy` (Static Prison's "sacrifice this unless you pay
    {E}" reads the same call the other way round, but the payment itself is
    identical) is the same fixed-amount question `may_pay_life` already asks
    - `bool_answer` is what `decide` reuses for both, so this proves it
    covers `pay_energy` too, the same way `test_may_shares_the_same_bool_
    answer_path_as_pay_life` proved it for `may`."""
    uids = seats_for(client, game)
    client.patch(f"/api/games/{game}/players/alice", json={"counters": {"energy": 3}})

    def ask_in_background(outcome: list[bool]) -> threading.Thread:
        def call() -> None:
            with store.locked(game) as live_game:
                outcome.append(live_game.may_pay_energy("alice", 2, source="Static Prison"))

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive()
        return thread

    outcome: list[bool] = []
    thread = ask_in_background(outcome)
    try:
        pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
        assert pending["kind"] == "pay_energy"
        assert pending["amount"] == 2
        assert pending["affirmative_is_legal"] is True
        answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "accept": True},
        )
        assert answer.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome == [True]
    final = state(client, game)
    assert next(p for p in final["players"] if p["username"] == "alice")["counters"]["energy"] == 1


def test_pay_energy_below_the_amount_offers_no_yes(client, game):
    """CR 118.3: a player short on energy cannot legally say yes -
    `affirmative_is_legal` must say so before the seat even answers, the same
    floor `pay_life` enforces off life instead of energy."""
    uids = seats_for(client, game)
    client.patch(f"/api/games/{game}/players/alice", json={"counters": {"energy": 1}})

    def ask_in_background(outcome: list[bool]) -> threading.Thread:
        def call() -> None:
            with store.locked(game) as live_game:
                outcome.append(live_game.may_pay_energy("alice", 2, source="Static Prison"))

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive()
        return thread

    outcome: list[bool] = []
    thread = ask_in_background(outcome)
    try:
        pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
        assert pending["affirmative_is_legal"] is False
        client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "accept": False},
        )
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome == [False]


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

    thread = cast_and_resolve_in_background(
        game, "alice", discharge["id"], [mountain["id"]], targets=[bob_bears]
    )
    try:
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
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

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

    thread = cast_and_resolve_in_background(
        game, "alice", discharge["id"], [mountain["id"]], targets=[bob_bears]
    )
    try:
        wait_for_pending(client, game, uids["alice"])
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
    finally:
        thread.join(timeout=10)


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

    thread = resolve_top_in_background(game)
    try:
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
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

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
    of). The resolving thread is still parked on the same decision
    afterwards, so a legal answer following the illegal one still lets play
    proceed."""
    add_card(client, game, "Necrodominance", "alice", "battlefield")
    stock_library_top(client, game, "alice", ["Mountain", "Forest", "Grizzly Bears"])
    uids = seats_for(client, game)
    reach_end_step_with_necrodominance_on_the_stack(client, game)

    thread = resolve_top_in_background(game)
    try:
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
        assert thread.is_alive(), "a refused answer must not have unblocked the resolving thread"

        legal = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "life_amount": 0},
        )
        assert legal.status_code == 200, legal.text
        assert legal.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

    final = state(client, game)
    assert life_of(final, "alice") == 20, "an illegal payment is never taken, and 0 paid nothing"


# -- pay_mana: "unless its controller pays {N}" (CR 118.12a) ----------------


def test_pay_mana_yes_spends_floating_mana(client, game):
    """`Game.may_pay_mana` (Censor's/Mystical Dispute's "counter target spell
    unless its controller pays {N}", CR 118.12a) is asked of the *targeted
    spell's* controller, not the counterspell's caster, and only mana already
    floating counts (see the docstring on `may_pay_mana`). This proves
    `decide` reuses `bool_answer` for `pay_mana` the same way it already does
    for `pay_life`/`pay_energy`, and that a "yes" really spends the mana."""
    uids = seats_for(client, game)
    client.put(f"/api/games/{game}/players/alice/mana-pool", json={"set": {"R": 3}})

    def ask_in_background(outcome: list[bool]) -> threading.Thread:
        def call() -> None:
            with store.locked(game) as live_game:
                outcome.append(live_game.may_pay_mana("alice", 2, source="Mystical Dispute"))

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive()
        return thread

    outcome: list[bool] = []
    thread = ask_in_background(outcome)
    try:
        pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
        assert pending["kind"] == "pay_mana"
        assert pending["player"] == "alice"
        assert pending["amount"] == 2
        assert pending["affirmative_is_legal"] is True
        answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "accept": True},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome == [True]
    final = state(client, game)
    alice = next(p for p in final["players"] if p["username"] == "alice")
    assert alice["mana_pool"]["floating"].get("R", 0) == 1, "2 of the 3 floating {R} were spent"


def test_pay_mana_no_declines_and_leaves_mana_untouched(client, game):
    """The opposite answer: declining leaves the floating mana untouched -
    proving `decide` carries the chosen branch through, not just "any answer
    means paid", matching `test_pay_life_no_keeps_life_and_enters_tapped`."""
    uids = seats_for(client, game)
    client.put(f"/api/games/{game}/players/alice/mana-pool", json={"set": {"R": 3}})

    def ask_in_background(outcome: list[bool]) -> threading.Thread:
        def call() -> None:
            with store.locked(game) as live_game:
                outcome.append(live_game.may_pay_mana("alice", 2, source="Mystical Dispute"))

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive()
        return thread

    outcome: list[bool] = []
    thread = ask_in_background(outcome)
    try:
        wait_for_pending(client, game, uids["alice"])
        answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "accept": False},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome == [False]
    final = state(client, game)
    alice = next(p for p in final["players"] if p["username"] == "alice")
    assert alice["mana_pool"]["floating"].get("R", 0) == 3, "declining spends nothing"


def test_pay_mana_below_the_amount_offers_no_yes(client, game):
    """CR 118.12a/118.3: a player short on floating mana cannot legally say
    yes - `affirmative_is_legal` must say so before the seat even answers,
    the same floor `pay_energy` enforces off energy instead of mana."""
    uids = seats_for(client, game)
    client.put(f"/api/games/{game}/players/alice/mana-pool", json={"set": {"R": 1}})

    def ask_in_background(outcome: list[bool]) -> threading.Thread:
        def call() -> None:
            with store.locked(game) as live_game:
                outcome.append(live_game.may_pay_mana("alice", 2, source="Mystical Dispute"))

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive()
        return thread

    outcome: list[bool] = []
    thread = ask_in_background(outcome)
    try:
        pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
        assert pending["affirmative_is_legal"] is False
        client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "accept": False},
        )
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome == [False]


def test_pay_mana_missing_accept_is_refused_not_defaulted(client, game):
    """A `decide` call for `pay_mana` with no `accept` must be refused by
    name, not silently defaulted, matching `pay_life`'s and `pay_energy`'s
    refusal of a missing `accept`."""
    uids = seats_for(client, game)
    client.put(f"/api/games/{game}/players/alice/mana-pool", json={"set": {"R": 3}})

    def ask_in_background(outcome: list[bool]) -> threading.Thread:
        def call() -> None:
            with store.locked(game) as live_game:
                outcome.append(live_game.may_pay_mana("alice", 2, source="Mystical Dispute"))

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive()
        return thread

    outcome: list[bool] = []
    thread = ask_in_background(outcome)
    try:
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
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome == [True]


def test_the_caster_cannot_answer_a_pay_mana_decision_asked_of_the_opponent(client, game):
    """CR 118.12a asks the *targeted spell's controller*, not the resolving
    counterspell's caster - `bob` here is the one who would be casting
    Mystical Dispute, but `alice` is the one being asked, so only alice may
    answer, matching `test_the_opponent_cannot_answer_a_pay_life_decision`."""
    uids = seats_for(client, game)
    client.put(f"/api/games/{game}/players/alice/mana-pool", json={"set": {"R": 3}})

    def ask_in_background(outcome: list[bool]) -> threading.Thread:
        def call() -> None:
            with store.locked(game) as live_game:
                outcome.append(live_game.may_pay_mana("alice", 2, source="Mystical Dispute"))

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive()
        return thread

    outcome: list[bool] = []
    thread = ask_in_background(outcome)
    try:
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
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome == [True]


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

    thread = cast_and_resolve_in_background(
        game, "alice", sprawl["id"], [forests[0]], targets=[forests[1]]
    )
    try:
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
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

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

    thread = cast_and_resolve_in_background(
        game, "alice", sprawl["id"], [forests[0]], targets=[forests[1]]
    )
    try:
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
        assert thread.is_alive(), "a refused answer must not have unblocked the resolving thread"

        legal = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "choice": "G"},
        )
        assert legal.status_code == 200, legal.text
        assert legal.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()


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

    thread = cast_and_resolve_in_background(
        game, "alice", sprawl["id"], [forests[0]], targets=[forests[1]]
    )
    try:
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
    finally:
        thread.join(timeout=10)


# -- choose_a_mode: a modal triggered ability's "choose one -" (CR 603.3c,
# CR 700.2a) ------------------------------------------------------------


def declare_gargaroth_attacking(client: TestClient, game_id: str, uids: dict[str, str]) -> None:
    """Elder Gargaroth attacking bob puts its own "attacks or blocks, choose
    one -" trigger on the stack (CR 603.3c) - foreground calls only, since
    declaring an attacker never itself asks a `choose_a_mode` question; the
    ability is put on the stack with its mode not yet chosen (CR 603.3c
    chooses the mode "as part of putting that ability on the stack", but
    this engine reads it at resolution instead - see `machines/effects.py`'s
    `_resolve_modal_triggered_ability_clause`), so the question only shows up
    once something resolves the trigger."""
    gargaroth = add_card(client, game_id, "Elder Gargaroth", "alice", "battlefield")["added"][
        "card_id"
    ]
    client.patch(f"/api/games/{game_id}/cards/{gargaroth}", json={"summoning_sick": False})
    client.put(f"/api/games/{game_id}/step", json={"step": "beginning_of_combat"})
    client.post(f"/api/games/{game_id}/advance", json={"player": "alice"})
    attack = client.post(
        f"/api/games/{game_id}/actions/attack",
        json={"player": "alice", "attackers": [{"card_id": gargaroth, "defender": "bob"}]},
    )
    assert attack.status_code == 200, attack.text
    assert attack.json()["allowed"] is True, attack.json()


def test_choose_a_mode_picks_the_named_mode(client, game):
    """Elder Gargaroth's attack trigger: "choose one - Create a 3/3 green
    Beast creature token. / You gain 3 life. / Draw a card." A
    `choose_a_mode` decision's answer is the mode's own printed text, out of
    `pending_decision.options` - this is the other live-hang case the report
    names, alongside Necrodominance's `pay_life_amount`."""
    uids = seats_for(client, game)
    declare_gargaroth_attacking(client, game, uids)

    thread = resolve_top_in_background(game)
    try:
        pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
        assert pending["kind"] == "choose_a_mode"
        assert pending["player"] == "alice"
        gain_life_mode = next(mode for mode in pending["options"] if "gain 3 life" in mode)

        answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "choice": gain_life_mode},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

    final = state(client, game)
    assert life_of(final, "alice") == 23, "the chosen mode - gain 3 life - actually happened"


def test_choose_a_mode_rejects_a_mode_that_was_not_offered(client, game):
    """CR 700.2a's printed modes are the only legal answers.
    `decisions.validate_answer` refuses a string that is not one of
    `pending_decision.options` synchronously, in this request, against the
    same `options` `Game.ask` itself checks - never silently substituting
    the first mode, never letting the mismatch surface late on the
    resolving thread's side (see
    `test_an_engine_refused_answer_is_refused_synchronously_not_recorded_allowed`
    in `test_decisions.py`). The resolving thread stays parked on the same
    decision, so an offered mode afterwards still lets it resolve."""
    uids = seats_for(client, game)
    declare_gargaroth_attacking(client, game, uids)

    thread = resolve_top_in_background(game)
    try:
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
        assert thread.is_alive(), "a refused answer must not have unblocked the resolving thread"

        legal = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "choice": "You gain 3 life."},
        )
        assert legal.status_code == 200, legal.text
        assert legal.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

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


def resolve_top_in_background(game_id: str) -> threading.Thread:
    def post() -> None:
        stepping_client = TestClient(app)
        stepping_client.post(f"/api/games/{game_id}/resolve-top")

    return run_in_background(post)


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

        first = resolve_top_in_background(game_id)  # Subtlety resolves and enters
        try:
            choose_subtletys_target(client, game_id, uids["alice"], board["bears"]["id"])
        finally:
            first.join(timeout=10)
        assert not first.is_alive()

        second = resolve_top_in_background(game_id)  # the ETB trigger resolves
        try:
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
        finally:
            second.join(timeout=10)
        assert not second.is_alive()

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

        first = resolve_top_in_background(game_id)
        try:
            choose_subtletys_target(client, game_id, uids["alice"], board["bears"]["id"])
        finally:
            first.join(timeout=10)

        second = resolve_top_in_background(game_id)
        try:
            wait_for_pending(client, game_id, uids["bob"])
            answer = client.post(
                f"/api/games/{game_id}/decide",
                params={"uid": uids["bob"]},
                json={"player": "bob", "top": False},
            )
            assert answer.status_code == 200, answer.text
            assert answer.json()["accepted"] is True
        finally:
            second.join(timeout=10)
        assert not second.is_alive()

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

        first = resolve_top_in_background(game_id)
        try:
            choose_subtletys_target(client, game_id, uids["alice"], board["bears"]["id"])
        finally:
            first.join(timeout=10)

        second = resolve_top_in_background(game_id)
        try:
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
            second.join(timeout=10)
        assert not second.is_alive()
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

        first = resolve_top_in_background(game_id)
        try:
            choose_subtletys_target(client, game_id, uids["alice"], board["bears"]["id"])
        finally:
            first.join(timeout=10)

        second = resolve_top_in_background(game_id)
        try:
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
            second.join(timeout=10)
        assert not second.is_alive()
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


def advance_to_upkeep_in_background(game_id: str, player: str = "alice") -> threading.Thread:
    """The advance that fires both triggers, on its own thread - it blocks
    inside the engine on CR 603.3b's ordering until a seat answers it."""

    def post() -> None:
        stepping_client = TestClient(app)
        stepping_client.post(f"/api/games/{game_id}/advance", json={"player": player})

    return run_in_background(post)


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
    set_short_timeout(game, 5.0)

    thread = advance_to_upkeep_in_background(game)
    try:
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
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert stack_ability_ids(game) == chosen


def test_order_triggers_refuses_an_order_that_is_not_a_permutation(client, game):
    """Dropping one of the offered abilities is CR 603.3's "its controller
    puts it on the stack" going unperformed, so it is refused - synchronously,
    to the seat that sent it, with the decision left pending so the same seat
    can answer again (which it then does, and the stack ends up in that second
    answer's order, proving the question really did survive the refusal)."""
    uids = seats_for(client, game)
    stage_two_simultaneous_upkeep_triggers(client, game)
    set_short_timeout(game, 5.0)

    thread = advance_to_upkeep_in_background(game)
    try:
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
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert stack_ability_ids(game) == chosen


# -- the drift test: every DecisionKind is answerable or named elsewhere ----


def test_every_decision_kind_is_answerable_or_served_elsewhere():
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

    This enumerates every `DecisionKind` the engine actually defines and
    requires each one to be named on one side or the other:
    `decisions.ANSWERABLE_KINDS` (a live seat can answer it through `decide`)
    or `decisions.SERVED_ELSEWHERE` (it is deliberately never routed here,
    with a note on where it *is* answered instead - `mulligan`,
    `declare_attackers`, `declare_blockers`). A kind in neither set is
    exactly the bug: the engine can ask it, nothing in this API answers it,
    and the failure below names the kind and says what to do about it,
    rather than leaving the next author to rediscover the freeze in a live
    game the way this one was found."""
    from magic_cards_model.machines.decisions import DecisionKind

    all_kinds = set(DecisionKind)
    routed = decisions.ANSWERABLE_KINDS | set(decisions.SERVED_ELSEWHERE)
    orphaned = all_kinds - routed
    assert not orphaned, (
        f"DecisionKind(s) {sorted(k.value for k in orphaned)} are asked by the engine "
        f"(magic_cards_model.machines.decisions.DecisionKind) but neither answerable "
        f"through POST /api/games/{{id}}/decide (decisions.ANSWERABLE_KINDS) nor "
        f"documented as served elsewhere (decisions.SERVED_ELSEWHERE) - a live seat "
        f"asked one of these will see decide refuse every answer it tries and the "
        f"game will hang until GameDecisionProvider's timeout defaults it for them. "
        f"Fix: either wire an answer path for it in app.py's decide() and add it to "
        f"ANSWERABLE_KINDS, or, if it truly has its own transport the way "
        f"mulligan/declare_attackers/declare_blockers do, add it to SERVED_ELSEWHERE "
        f"with a note on where it is actually answered."
    )
    # And the two sets must not overlap - a kind claiming to be both
    # answerable here and served by some other transport is a contradiction,
    # not a redundancy.
    assert not (decisions.ANSWERABLE_KINDS & set(decisions.SERVED_ELSEWHERE))


# -- an unwired kind: refused by name, not defaulted -------------------------


def test_an_unwired_kind_is_refused_by_name(client, game):
    """`declare_attackers`/`declare_blockers` never reach this mailbox
    through real play (see the module docstring), so the only way to
    exercise `decide`'s refusal for one is to publish it directly - exactly
    the shape a *future* `DecisionKind` would take if it were routed through
    `Game.ask` without `app.py` and `decisions.ANSWERABLE_KINDS` being taught
    it. The refusal must name the kind, not silently default or 500.

    `mulligan` used to be this test's subject and no longer can be: the
    engine wired `ask_mulligan` to the provider, so it is a real, live,
    answerable kind now - see `decisions.ANSWERABLE_KINDS`."""
    uids = seats_for(client, game)
    mailbox = store.mailbox(game)
    assert mailbox is not None
    mailbox.publish(
        PendingDecision(
            player="alice",
            kind="declare_attackers",
            prompt="declare attackers",
            source=None,
            amount=0,
            cards=(),
        ),
        Decision(kind=DecisionKind.DECLARE_ATTACKERS, player="alice", prompt="declare attackers"),
    )
    try:
        response = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "accept": False},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] is False
        assert "declare_attackers" in body["reason"]
    finally:
        mailbox.wait_for_answer(0.01, 0.01)  # drain, so no other test sees it pending


def test_log_timeout_wording_for_an_unwired_kind(client, game):
    """Unit-level, on `GameDecisionProvider._log_timeout` directly: a kind
    outside `ANSWERABLE_KINDS` times out saying the player *could not*
    answer, never that they *did not* - see the module and provider
    docstrings for why conflating the two hid the reported bug."""
    from magic_cards_model.machines.decisions import Decision, DecisionKind

    provider = store.get(game).decisions.provider
    assert provider is not None
    decision = Decision(
        kind=DecisionKind.DECLARE_ATTACKERS, player="alice", prompt="declare attackers"
    )
    provider._log_timeout(decision)

    log = client.get(f"/api/games/{game}/log", params={"uid": store.admin_key(game)}).json()["log"]
    entry = next(e for e in reversed(log) if e["action"] == "decision_timeout")
    assert entry["detail"]["kind"] == "declare_attackers"
    assert entry["detail"]["could_answer"] is False


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

            thread = play_steam_vents_in_background(game_id, land["id"])
            try:
                deadline = time.monotonic() + 5
                pending = None
                while time.monotonic() < deadline:
                    pending = http.get(
                        f"/api/games/{game_id}", params={"uid": uids["alice"]}
                    ).json()["pending_decision"]
                    if pending is not None:
                        break
                    time.sleep(0.01)
                assert pending is not None and pending["kind"] == "pay_life"

                rc = seat_cli.run(
                    ["--uid", uids["alice"], "--game", game_id, "wait", "--max-seconds", "3"]
                )
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
            finally:
                thread.join(timeout=10)
            assert not thread.is_alive()

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

            thread = play_steam_vents_in_background(game_id, land["id"])
            try:
                deadline = time.monotonic() + 5
                pending = None
                while time.monotonic() < deadline:
                    pending = http.get(
                        f"/api/games/{game_id}", params={"uid": uids["alice"]}
                    ).json()["pending_decision"]
                    if pending is not None:
                        break
                    time.sleep(0.01)
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
            finally:
                thread.join(timeout=10)
            assert not thread.is_alive()

            final = http.get(f"/api/games/{game_id}", params={"uid": uids["alice"]}).json()
            assert life_of(final, "alice") == 20
            assert find(final["battlefield"], "Steam Vents")["tapped"] is True
        finally:
            http.delete(f"/api/games/{game_id}", params={"key": created["admin_key"]})
    finally:
        http.close()
