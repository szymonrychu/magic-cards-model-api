"""Every `DecisionKind` the engine can ask (`magic_cards_model.machines.decisions`),
matched against what `POST /api/games/{id}/decide` actually answers.

Before this file, `decide` only knew `choose_cards` and `choose_targets`
(`test_decisions.py`, `test_target_decisions.py`). The other four the engine's
`Game.ask` can route through this API's live `GameDecisionProvider` - `may`,
`pay_life`, `scry`, `surveil` - fell through to `default_answer` no matter what
a seat sent, after parking the request for the full timeout. The reported case
was a shockland's "pay 2 life" (`pending_decision.kind == "pay_life"`):
`decide` refused it outright, the request timed out, and CR 119.4's choice was
made *for* the player rather than *by* them.

`mulligan`, `declare_attackers`, and `declare_blockers` are the other three
`DecisionKind`s and are deliberately not covered here with a live-seat test:
reading `machines/decisions.py` and `models/game.py` shows `ask_mulligan`,
`ask_attackers`, and `ask_blockers` all take their answer straight off
`DecisionScript.take`, never touching `Game.decisions.provider` (the mailbox
this API publishes to) at all. No request through this API can ever make one
of them arrive at `POST /decide` - attacking and blocking already have their
own actions (`actions/attack`, `actions/declare-blockers`) that call
`Game.declare_attackers`/`Game.declare_blockers` directly, and a mulligan is
settled inside `Match.start_game`. `test_an_unwired_kind_is_refused_by_name`
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
    game_id: str, player: str, card_id: str, sources: list[str]
) -> threading.Thread:
    def post() -> None:
        stepping_client = TestClient(app)
        cast = stepping_client.post(
            f"/api/games/{game_id}/actions/cast",
            json={"player": player, "card_id": card_id, "mana_sources": sources},
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


# -- an unwired kind: refused by name, not defaulted -------------------------


def test_an_unwired_kind_is_refused_by_name(client, game):
    """`mulligan`/`declare_attackers`/`declare_blockers` never reach this
    mailbox through real play (see the module docstring), so the only way to
    exercise `decide`'s refusal for one is to publish it directly - exactly
    the shape a *future* `DecisionKind` would take if it were routed through
    `Game.ask` without `app.py` and `decisions.ANSWERABLE_KINDS` being taught
    it. The refusal must name the kind, not silently default or 500."""
    uids = seats_for(client, game)
    mailbox = store.mailbox(game)
    assert mailbox is not None
    mailbox.publish(
        PendingDecision(
            player="alice",
            kind="mulligan",
            prompt="take a mulligan",
            source=None,
            amount=0,
            cards=(),
        )
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
        assert "mulligan" in body["reason"]
    finally:
        mailbox.wait_for_answer(0.01)  # drain, so no other test sees it pending


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
            http.delete(f"/api/games/{game_id}")
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
            http.delete(f"/api/games/{game_id}")
    finally:
        http.close()
