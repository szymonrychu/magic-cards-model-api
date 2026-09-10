"""`GET /api/games/{game_id}/legal-actions`: the api-side door onto Phase 1's
enumeration surface, `machines.legal_actions.legal_actions(game, player)`
(`magic-cards-model`).

T11 replaces `seatkit.available_actions`'s brute-force probe - one HTTP
`dry_run` round trip per hand card, per graveyard/exile card with a cast
option, and per battlefield ability, each collapsed into a bare boolean - with
one call into the engine's own enumeration, which reports an `Affordability`
verdict (`"yes"`/`"no"`/`"unknown"`) alongside every action rather than
hiding the `unknown` case the old probe could never represent (a dry run
either succeeds or it does not).

This file pins the endpoint itself; `test_action_flow.py` and
`test_graveyard_cast_options.py` pin `seatkit.available_actions`'s own new
shape, built on top of it.
"""

from __future__ import annotations

import time

import httpx
from conftest import _ADMIN_KEYS, add_card, find, state
from fastapi.testclient import TestClient
from test_action_answer_size import BOARD as BUSY_BOARD
from test_action_answer_size import GRAVE as BUSY_GRAVE
from test_decisions import advance_into_cleanup, stuff_hand

from magic_cards_model_api import affordances
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seat, Seats, available_actions
from magic_cards_model_api.store import store

#: What `candidates=true` may add to a `legal-actions` answer on a
#: 28-permanent board with three targeted spells in hand. Measured at ~106 ms,
#: which is what put the candidates behind an opt-in parameter in the first
#: place: the ~50 ms this had to stay under to ride along on every call was
#: never reachable, because `TargetRequirement.rejects` reaches
#: `Game.characteristics_of` and `Game.computed_board` is uncached in the
#: engine on purpose (CR 613.5 - continuous effects are applied "continually",
#: so a cached board would be a board that lies). Held here at 3x the
#: measurement, loose enough for a busy CI box and tight enough that an order
#: of magnitude cannot creep in unnoticed.
CANDIDATES_TIME_BUDGET = 0.35


def seat_uid(game_id: str, player: str) -> str:
    """The secret uid that opens `player`'s own seat in `game_id`."""
    return {name: uid for uid, name in store.seat_uids(game_id).items()}[player]


def legal_actions(client, game_id: str, player: str = "alice", **params) -> list[dict]:
    """The endpoint as that player's own seat sees it - the only caller it
    answers. Every test here presents a real uid, because the endpoint's whole
    answer is that seat's hidden information: what is in its hand, what each
    card costs, and whether it can pay."""
    response = client.get(
        f"/api/games/{game_id}/legal-actions",
        params={"player": player, "uid": seat_uid(game_id, player), **params},
    )
    assert response.status_code == 200, response.text
    return response.json()["actions"]


def with_candidates(client, game_id: str, player: str = "alice") -> list[dict]:
    """The same answer a seat gets - `candidates=true`, which is what
    `seatkit.available_actions` sets and the browser deliberately does not."""
    return legal_actions(client, game_id, player, candidates="true")


def by_kind(actions: list[dict], kind: str) -> list[dict]:
    return [action for action in actions if action["kind"] == kind]


def test_pass_priority_and_concede_are_always_offered_and_affordable(client, game):
    actions = legal_actions(client, game)
    (pass_priority,) = by_kind(actions, "pass_priority")
    (concede,) = by_kind(actions, "concede")
    assert pass_priority["affordable"] == "yes"
    assert concede["affordable"] == "yes"
    assert pass_priority["rule"] == "CR 117.3d"
    assert concede["rule"] == "CR 104.3a"


def test_a_payable_cast_is_reported_affordable(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")

    (cast,) = [
        a for a in by_kind(legal_actions(client, game), "cast") if a["card_id"] == bolt["id"]
    ]
    assert cast["affordable"] == "yes"
    assert cast["mana_cost"] == "{R}"
    assert cast["unaffordable_reason"] is None


def test_an_unpayable_cast_is_reported_not_affordable_with_the_engines_own_reason(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")

    (cast,) = [
        a for a in by_kind(legal_actions(client, game), "cast") if a["card_id"] == bolt["id"]
    ]
    assert cast["affordable"] == "no"
    assert cast["unaffordable_reason"]


def test_an_x_spell_before_x_is_announced_is_reported_unknown_not_flattened_to_no(client, game):
    """CR 107.3a: X is not announced until the spell is actually cast, so an
    enumeration asked before that point cannot say yes or no - it has to say
    it does not know yet. The old dry-run probe had no way to represent this
    at all (a dry run either succeeds or it does not); this is the case T11
    exists to stop hiding."""
    for _ in range(3):
        add_card(client, game, "Wastes", "alice", "battlefield")
    add_card(client, game, "Kozilek's Command", "alice", "hand")
    koz = find(state(client, game)["hands"]["alice"], "Kozilek's Command")

    (cast,) = [a for a in by_kind(legal_actions(client, game), "cast") if a["card_id"] == koz["id"]]
    assert cast["affordable"] == "unknown"
    assert "X" in cast["unaffordable_reason"]


def test_a_pending_decision_narrows_the_pool_to_its_own_answers_for_the_player_it_addresses(
    client, game
):
    """`legal_actions`'s own module docstring: while a `Decision` is
    outstanding, the legal actions ARE its legal answers and nothing else,
    for the player it addresses - and nothing at all for anyone else, since
    nothing else in the engine's own execution can happen until it is
    answered."""
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard: a real choice
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    paused = advance_into_cleanup(game)
    assert paused is not None, "the discard should have asked, not defaulted silently"
    assert paused["pending_decision"]["kind"] == "choose_cards"

    alice_actions = legal_actions(client, game, player="alice")
    assert alice_actions, "alice's own pending decision should offer its answers"
    assert all(a["kind"] == "answer_decision" for a in alice_actions)
    assert all(a["decision_candidates"] for a in alice_actions if a.get("x_range"))

    bob_actions = legal_actions(client, game, player="bob")
    assert bob_actions == [], "nothing else is legal while alice's own decision stands"


def test_unknown_affordability_reaches_the_seat_through_available_actions(
    client, game, monkeypatch
):
    """The endpoint tests above pin the raw HTTP response; this pins the
    other end of the wire T11 actually adopted `legal_actions` for -
    `seatkit.available_actions`, what an MCP/CLI seat's own `cast`/
    `wait_for_priority` calls embed as `available_actions` in their answer.
    The old brute-force dry-run probe could only ever report `castable: true`
    or nothing at all - there was no way for `UNKNOWN` to reach a seat
    through it, because a dry run either succeeds or it does not. This
    confirms it now does, unflattened, all the way to `Seat.available_actions`."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    for _ in range(3):
        add_card(client, game, "Wastes", "alice", "battlefield")
    add_card(client, game, "Kozilek's Command", "alice", "hand")
    koz = find(state(client, game)["hands"]["alice"], "Kozilek's Command")

    uids = {name: uid for uid, name in store.seat_uids(game).items()}
    seats = Seats(api="http://testserver", game_id=game)
    alice = seats.seat_for(uids["alice"])

    result = available_actions(alice, alice.fetch_state())
    (cast,) = [a for a in result["actions"] if a["kind"] == "cast" and a["card_id"] == koz["id"]]
    assert cast["affordable"] == "unknown"
    assert cast["affordable"] not in ("yes", "no"), (
        "UNKNOWN must reach the seat as its own verdict, not collapsed into a boolean"
    )


# -- who may ask ------------------------------------------------------------
#
# The answer is one seat's hidden information end to end: which cards are in
# its hand, what each of them costs, where each would be cast from, what it
# would target, and whether this seat can pay for it. `visibility.py`'s own
# contract - "a caller that presents nothing must never see more than one that
# presents garbage, and neither may see more than one that presents someone
# else's seat" - therefore governs this route exactly as it governs
# `GET /api/games/{id}`, whose `hands.bob` is already `[]` for all three.
#
# A refusal rather than an empty list, deliberately: `{"actions": []}` is a
# real answer this endpoint gives (a seat with a pending decision addressed to
# the other player has genuinely nothing legal), and a caller - `seatkit.
# available_actions` above all - must be able to tell "you may not ask that"
# from "there is nothing you can do". The shape is the api's own existing one
# for a credential that does not open what it names (`ValueError` -> 400, the
# same as `pregame_settle`'s admin-key check); there is no 403 anywhere in
# this api to match.


def hidden_card_names(client, game_id: str) -> set[str]:
    """Every card in bob's hand, by name - the identities no caller without
    bob's own uid may learn."""
    return {card["name"] for card in state(client, game_id)["hands"]["bob"]}


def ask(client, game_id: str, player: str, uid: str | None = None):
    params = {"player": player}
    if uid is not None:
        params["uid"] = uid
    return client.get(f"/api/games/{game_id}/legal-actions", params=params)


def test_a_caller_with_no_uid_is_refused_and_learns_nothing(client, game):
    add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "bob", "hand")
    secrets = hidden_card_names(client, game)
    assert secrets, "the fixture must actually have put something hidden in bob's hand"

    response = ask(client, game, "bob")
    assert response.status_code == 400, response.text
    for name in secrets:
        assert name not in response.text


def test_a_uid_minted_for_a_different_game_is_refused(client, game):
    add_card(client, game, "Lightning Bolt", "bob", "hand")
    secrets = hidden_card_names(client, game)
    other = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    try:
        response = ask(client, game, "bob", uid=seat_uid(other["game_id"], "bob"))
        assert response.status_code == 400, response.text
        for name in secrets:
            assert name not in response.text
    finally:
        client.delete(f"/api/games/{other['game_id']}", params={"key": other["admin_key"]})


def test_an_unknown_uid_is_refused(client, game):
    add_card(client, game, "Lightning Bolt", "bob", "hand")
    secrets = hidden_card_names(client, game)

    response = ask(client, game, "bob", uid="not-a-uid-anybody-ever-minted")
    assert response.status_code == 400, response.text
    for name in secrets:
        assert name not in response.text


def test_one_seats_uid_may_not_ask_about_the_other_seats_player(client, game):
    """The leak this route shipped with: `player` is a query parameter with
    nothing authenticating it, so alice - a real seat, holding a real uid -
    could name bob and read his hand out of the answer."""
    add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "bob", "hand")
    secrets = hidden_card_names(client, game)

    response = ask(client, game, "bob", uid=seat_uid(game, "alice"))
    assert response.status_code == 400, response.text
    for name in secrets:
        assert name not in response.text


def test_a_seat_may_ask_about_itself(client, game):
    add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "bob", "hand")

    response = ask(client, game, "bob", uid=seat_uid(game, "bob"))
    assert response.status_code == 200, response.text
    assert by_kind(response.json()["actions"], "cast"), "bob must still see his own cast"


def test_the_admin_key_may_ask_about_either_seat(client, game):
    """`visibility.resolve_viewer`'s third answer: the game's admin key is the
    operator's own eye over a game they created, and sees everything - the
    same trust level `GET /api/games/{id}` and `audit.snapshot` already grant
    it. The browser UI's own board is served under it."""
    add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "bob", "hand")

    response = ask(client, game, "bob", uid=_ADMIN_KEYS[game])
    assert response.status_code == 200, response.text
    assert by_kind(response.json()["actions"], "cast")


# -- when the call itself fails ---------------------------------------------
#
# `seatkit.available_actions` is one HTTP call where the old probe was N, so
# its single failure is total rather than partial. `Seat.http` never calls
# `raise_for_status`, and a 404 or a 500 carrying a JSON body parses cleanly
# into `payload.get("actions", [])` - an empty pool no reader could tell from
# "this seat legally cannot do anything", embedded verbatim in all seven of
# `run_action`'s return paths.


def test_a_refused_legal_actions_call_says_so_instead_of_reporting_an_empty_pool(
    client, game, monkeypatch
):
    """A seat asking about a player its uid does not open is refused with 400
    (`app.get_legal_actions`). The JSON body parses fine, so nothing before
    this made the answer anything but a confident empty list."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")

    impostor = Seat("bob", seat_uid(game, "alice"), "http://testserver", game)
    result = available_actions(impostor, state(client, game))

    assert result["actions"] == []
    assert "400" in result["actions_error"]
    assert "NOT" in result["actions_error"], "it must not read like 'nothing is legal'"


def test_a_404_from_the_legal_actions_call_says_so_instead_of_reporting_an_empty_pool(
    client, game, monkeypatch
):
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    lost = Seat("alice", seat_uid(game, "alice"), "http://testserver", "no-such-game")

    result = available_actions(lost, state(client, game))

    assert result["actions"] == []
    assert "404" in result["actions_error"]


def test_a_read_timeout_is_reported_not_raised_out_of_every_run_action_path(
    client, game, monkeypatch
):
    """`httpx.ReadTimeout` used to propagate straight out of this call, and so
    out of every one of `run_action`'s returns - including the ones that have
    already performed a mutating action and owe the caller its outcome."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seat = Seat("alice", seat_uid(game, "alice"), "http://testserver", game)
    board = state(client, game)

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("took too long")

    monkeypatch.setattr(seat.http, "get", timeout)
    result = available_actions(seat, board)

    assert result["actions"] == []
    assert "timed out" in result["actions_error"]


def test_a_working_call_carries_no_actions_error_at_all(client, game, monkeypatch):
    """The other half of the contract: `actions_error` is absent on the happy
    path, so its mere presence is the signal."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seat = Seat("alice", seat_uid(game, "alice"), "http://testserver", game)

    result = available_actions(seat, state(client, game))

    assert "actions_error" not in result
    assert by_kind(result["actions"], "pass_priority")


# -- the legal targets, computed with the enumeration -----------------------
#
# `Action.target_slots` is the *shape* of a targeting requirement and never
# its candidates - the engine's own `TargetSlot` docstring says as much, and
# points at `Game.legal_targets` for the objects. That left a seat holding a
# legal, affordable, targeting action and no idea what it could name, one
# round trip short of acting. The candidates are computed here instead, in
# the same request that enumerated the actions, off one walk of the board.
#
# Opt-in (`candidates=true`), and measured rather than assumed: the walk costs
# ~106 ms on a 28-permanent board, which a seat gladly pays once per action to
# save a whole inference cycle and the browser must not pay on a 1.2s poll it
# does not need the answer for. `seatkit.available_actions` sets it;
# `ui/app.js` does not.


def targets_of(actions: list[dict], kind: str = "cast") -> list[dict]:
    return [a for a in by_kind(actions, kind) if "targets" in a]


def burn_in_hand(client, game_id: str) -> dict:
    """A mountain, something to point at, and a Bolt to point with."""
    add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game_id, "Lightning Bolt", "alice", "hand")
    return find(state(client, game_id)["hands"]["alice"], "Lightning Bolt")


def test_candidates_are_off_unless_the_caller_asks_for_them(client, game):
    """The browser's own call, unchanged: it asks this endpoint every 1.2s,
    already has the whole board on screen to pick targets from, and must not
    pay ~106 ms of candidate enumeration for an answer it does not read."""
    bolt = burn_in_hand(client, game)

    (cast,) = [
        a for a in by_kind(legal_actions(client, game), "cast") if a["card_id"] == bolt["id"]
    ]
    assert cast["target_slots"], "it targets either way - that half is free"
    assert "targets" not in cast


def test_a_targeting_action_carries_its_legal_candidates_as_plain_strings(client, game):
    """Strings, not objects: fourteen candidates measured 972 bytes this way
    against 2,384 as dicts, because `json.dumps(indent=1)` gives every dict
    key a line. The id is the whole uuid, since that is what `targets` takes
    back."""
    bolt = burn_in_hand(client, game)
    bears = find(state(client, game)["battlefield"], "Grizzly Bears")

    (cast,) = [a for a in targets_of(with_candidates(client, game)) if a["card_id"] == bolt["id"]]

    assert cast["targets"]["understood"] is True
    (slot,) = cast["targets"]["slots"]
    assert slot["slot"] == 0
    assert slot["count"] == len(slot["candidates"])
    assert all(isinstance(one, str) for one in slot["candidates"])
    assert f"{bears['id']} Grizzly Bears (battlefield, bob)" in slot["candidates"]
    assert "alice (player)" in slot["candidates"]
    assert "bob (player)" in slot["candidates"]
    assert "truncated" not in slot, "nothing was cut, so nothing says it was"
    assert "more_with" not in cast["targets"], cast["targets"]


def test_an_action_that_targets_nothing_carries_no_targets_block_at_all(client, game):
    """`target_slots` empty means "this does not target" - there is nothing to
    enumerate and no key to spend bytes on."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    bears = find(state(client, game)["hands"]["alice"], "Grizzly Bears")

    (cast,) = [
        a for a in by_kind(with_candidates(client, game), "cast") if a["card_id"] == bears["id"]
    ]
    assert cast["target_slots"] == []
    assert "targets" not in cast


def test_an_unaffordable_action_is_still_listed_but_carries_no_candidates(client, game):
    """CR 118.6 keeps the row; nothing keeps the list. A candidate list for a
    spell that cannot be cast is bytes a seat can do nothing with."""
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")

    (cast,) = [
        a for a in by_kind(with_candidates(client, game), "cast") if a["card_id"] == bolt["id"]
    ]
    assert cast["affordable"] == "no"
    assert cast["target_slots"], "it still targets - it just cannot be paid for"
    assert "targets" not in cast


def test_an_activated_ability_carries_no_candidates_because_the_engine_names_no_slots(client, game):
    """The boundary of this, stated rather than discovered later. The engine's
    enumeration fills `target_slots` for a *cast* only
    (`legal_actions._cast_action`); a battlefield activation carries none,
    whatever the ability itself targets - Karn, the Great Creator's `+1`
    ("up to one target noncreature artifact") is listed with `target_slots:
    []` exactly like his `-2`, which targets nothing at all.

    So an `activate` row gets no candidates here, because there is no slot to
    hang them on and this repo will not re-derive from printed text what the
    engine declined to state. `/api/games/{id}/affordances/{card_id}` answers
    that question per ability (with `ability_index`), and the fix that would
    make it unnecessary belongs in the engine's `Action`, not here."""
    add_card(client, game, "Karn, the Great Creator", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "bob", "battlefield")

    activations = by_kind(with_candidates(client, game), "activate")

    assert len(activations) == 2, "Karn's +1 and -2 are both legal here"
    for action in activations:
        assert action["target_slots"] == [], action
        assert "targets" not in action, action


def test_the_board_is_walked_once_per_request_not_once_per_action(client, game, monkeypatch):
    """The design's own tripwire. Candidate enumeration is the expensive half
    of `affordances.py` by that module's own admission, and it is only
    affordable at all because the board is walked once for the whole request
    and reused across every action - never once per action, which is the
    N-round-trip shape T11 removed at the HTTP layer creeping back in one
    layer down."""
    for _ in range(3):
        add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    for name in ("Lightning Bolt", "Shock", "Shock"):
        add_card(client, game, name, "alice", "hand")

    walks = []
    real = affordances.all_targetables
    monkeypatch.setattr(
        affordances,
        "all_targetables",
        lambda game: (walks.append(game), real(game))[1],
    )

    enriched = targets_of(with_candidates(client, game))

    assert len(enriched) >= 3, [a["card_id"] for a in enriched]
    assert len(walks) == 1, f"{len(walks)} walks for {len(enriched)} targeting actions"


def test_candidate_enumeration_stays_within_its_measured_budget(client, game):
    """The number that decided the opt-in. Twenty-eight permanents, eight
    cards in each graveyard and three targeted spells in hand: the candidates
    add ~106 ms to an answer the enumeration alone already spends ~350 ms on,
    because `TargetRequirement.rejects` reaches `Game.characteristics_of` and
    `Game.computed_board` is deliberately uncached in the engine (CR 613.5).
    Past ~50 ms is what put candidates behind `candidates=true` rather than
    on every caller; this holds the cost to the ballpark that decision was
    made in, so a future regression is a decision to re-make rather than a
    slow endpoint nobody noticed."""
    for name in BUSY_BOARD:
        add_card(client, game, name, "alice", "battlefield")
        add_card(client, game, name, "bob", "battlefield")
    for name in BUSY_GRAVE:
        add_card(client, game, name, "alice", "graveyard")
        add_card(client, game, name, "bob", "graveyard")
    for name in ("Lightning Bolt", "Shock", "Shock"):
        add_card(client, game, name, "alice", "hand")

    def best(fetch) -> float:
        runs = []
        for _ in range(3):
            started = time.perf_counter()
            actions = fetch(client, game)
            runs.append(time.perf_counter() - started)
        return min(runs), actions

    without, _ = best(legal_actions)
    with_, enriched = best(with_candidates)

    assert targets_of(enriched), "the board must actually have produced candidates"
    added = with_ - without
    assert added < CANDIDATES_TIME_BUDGET, (
        f"candidates added {added * 1000:.1f} ms "
        f"({without * 1000:.1f} -> {with_ * 1000:.1f}), budget {CANDIDATES_TIME_BUDGET}"
    )
