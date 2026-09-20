"""Every read the api builds from the layered board shares one engine snapshot.

Production, 2026-09-14: the api pod pinned its 1-CPU limit because a state
view and a legal-actions read each rebuilt `Game.computed_board` once per
permanent (93-196 boards per GET state, 135-191 per legal-actions read), and
`records.legal_actions_before` paid the same inside the per-game lock on every
action - queueing requests past oauth2-proxy's 30s upstream timeout.

The engine's fix is a scope (`magic_cards_model.snapshot.board_snapshot`): the
board is computed once inside it and reused until something announces a
change. These tests pin the api's half, on real boards played through the real
routes:

- **count**: GET state, GET legal-actions (candidates on and off), GET
  affordances, and each POST action (a land drop, a cast, a resolution, a
  priority pass) compute the board a small constant number of times, and the
  same number on a 4-permanent board as on a 28-permanent one
  (`board_computations()`, the engine's own counter). An action is counted
  end to end, its engine command included: the command's own scans share one
  board each (the engine's side) and so does the resolution fingerprint taken
  around it (`commands._resolution_state`).
- **equivalence**: the JSON of GET state and GET legal-actions is
  byte-identical with every board computed afresh (`uncached_boards()`).
- **no stale board**: an action's own response and the next GET state show
  the board after the action, not the one enumerated before it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import CodeType, FrameType
from typing import Any

import httpx
import pytest
from conftest import _ADMIN_KEYS, add_card, force_turn_position
from magic_cards_model.machines import Step
from magic_cards_model.machines.layers import NotOnBoard
from magic_cards_model.models import CardState, Color, Zone
from magic_cards_model.models.game import Game
from magic_cards_model.snapshot import BoardSnapshotStale, board_computations, uncached_boards
from test_action_answer_size import BOARD, GRAVE, stage_card

from magic_cards_model_api import commands, engine_api, serialize
from magic_cards_model_api.board_scope import one_board
from magic_cards_model_api.main import app
from magic_cards_model_api.playscripts import hypothetical_facts
from magic_cards_model_api.store import store

#: A read may compute the layered board this many times, whatever the size of
#: the battlefield: one per game object it reads (the committed game, and the
#: paused one when there is one), with room for one forced recompute.
BOARDS_PER_READ = 4

#: An action may compute it this many times, whatever the size of the
#: battlefield: the enumeration before it, each of its command's scans (one
#: per event raised, per state trigger sweep, per state-based action round),
#: the resolution fingerprints around it, and the audit snapshot and response
#: after it.
BOARDS_PER_ACTION = 12

READS = (
    "get_state",
    "legal_actions",
    "legal_actions_candidates",
    "affordances",
    "hypothetical_facts",
)

#: The hand every board here holds, so the only thing that differs between the
#: small board and the large one is the battlefield.
HAND = ["Lightning Bolt", "Grizzly Bears", "Plains"]


def seat_uid(game_id: str, player: str) -> str:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}[player]


def stage_board(game_id: str, names: list[str], *, graveyard: bool = False) -> None:
    """`names` on both battlefields, `HAND` in alice's hand, precombat main
    with alice active - placed straight into the engine the way
    `test_action_answer_size.stage_card` does, so setup computes no view."""
    force_turn_position(game_id, Step.PRECOMBAT_MAIN, "alice")
    for name in names:
        stage_card(game_id, name, "alice", "battlefield")
        stage_card(game_id, name, "bob", "battlefield")
    for name in HAND:
        stage_card(game_id, name, "alice", "hand")
    if graveyard:
        for name in GRAVE:
            stage_card(game_id, name, "alice", "graveyard")
    # The city's blessing check reads the board only once a player without the
    # blessing controls ten permanents: a threshold, not a per-permanent cost,
    # but one the small board and the large one would fall on opposite sides
    # of. Both players already have it, so both boards read the same.
    game = store.get(game_id)
    assert game is not None
    for player in game.players:
        player.has_citys_blessing = True


def boards_for(call: Callable[[], httpx.Response]) -> int:
    before = board_computations()
    response = call()
    assert response.status_code == 200, response.text
    return board_computations() - before


def api_boards_for_hypothetical_facts(client, game_id: str, uid: str) -> int:
    """Boards GET hypothetical-facts computes in the api itself: the route's
    total less what the engine's `payability_matrix` computes inside. That
    matrix evaluates each land on its own decoded copy of the game and plays
    the land onto it, so its boards belong to those copies and no scope the
    api opens on the live game can share them."""
    inside = 0
    engine_matrix = hypothetical_facts.payability_matrix

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal inside
        before = board_computations()
        try:
            return engine_matrix(*args, **kwargs)
        finally:
            inside += board_computations() - before

    hypothetical_facts.payability_matrix = counted
    try:
        total = boards_for(
            lambda: client.get(
                f"/api/games/{game_id}/playscripts/hypothetical-facts", params={"uid": uid}
            )
        )
    finally:
        hypothetical_facts.payability_matrix = engine_matrix
    return total - inside


def new_game(client) -> str:
    response = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    assert response.status_code == 200, response.text
    info = response.json()
    _ADMIN_KEYS[info["game_id"]] = info["admin_key"]
    return info["game_id"]


def request_counts(client, game_id: str) -> dict[str, int]:
    """Boards each request computes, end to end: an action's count includes
    everything its command does in the engine, not only what this api reads
    around it."""
    admin = _ADMIN_KEYS[game_id]
    alice = seat_uid(game_id, "alice")
    game = store.get(game_id)
    assert game is not None
    bolt = next(card for card in game.player("alice").hand if card.name == "Lightning Bolt")
    plains = next(card for card in game.player("alice").hand if card.name == "Plains")
    return {
        "get_state": boards_for(lambda: client.get(f"/api/games/{game_id}", params={"uid": admin})),
        "legal_actions": boards_for(
            lambda: client.get(
                f"/api/games/{game_id}/legal-actions", params={"player": "alice", "uid": alice}
            )
        ),
        "legal_actions_candidates": boards_for(
            lambda: client.get(
                f"/api/games/{game_id}/legal-actions",
                params={"player": "alice", "uid": alice, "candidates": "true"},
            )
        ),
        "affordances": boards_for(
            lambda: client.get(
                f"/api/games/{game_id}/affordances/{bolt.id}", params={"player": "alice"}
            )
        ),
        "hypothetical_facts": api_boards_for_hypothetical_facts(client, game_id, alice),
        "play_land": boards_for(
            lambda: client.post(
                f"/api/games/{game_id}/actions/play-land",
                params={"uid": admin},
                json={"player": "alice", "card_id": str(plains.id)},
            )
        ),
        "cast": boards_for(
            lambda: client.post(
                f"/api/games/{game_id}/actions/cast",
                params={"uid": admin},
                json={"player": "alice", "card_id": str(bolt.id), "targets": ["player:bob"]},
            )
        ),
        "resolve_top": boards_for(
            lambda: client.post(f"/api/games/{game_id}/resolve-top", params={"uid": admin})
        ),
        "pass_priority": boards_for(
            lambda: client.post(
                f"/api/games/{game_id}/actions/pass-priority",
                params={"uid": admin},
                json={"player": "alice"},
            )
        ),
    }


def test_board_reads_compute_the_board_a_constant_number_of_times(client) -> None:
    small = new_game(client)
    large = new_game(client)
    try:
        stage_board(small, BOARD[:2])
        stage_board(large, BOARD)
        large_game = store.get(large)
        assert large_game is not None
        assert len(large_game.shared_zones["battlefield"]) >= 20

        small_counts = request_counts(client, small)
        large_counts = request_counts(client, large)

        over = {
            name: count
            for name, count in large_counts.items()
            if count > (BOARDS_PER_READ if name in READS else BOARDS_PER_ACTION)
        }
        assert not over, f"small {small_counts} large {large_counts}"
        assert large_counts == small_counts, f"small {small_counts} large {large_counts}"
    finally:
        for game_id in (small, large):
            client.delete(f"/api/games/{game_id}", params={"key": _ADMIN_KEYS.pop(game_id)})


def test_no_board_snapshot_is_open_over_the_game_a_command_runs_on(
    client, game, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enumeration before an action (`records.legal_actions_before`) reads
    the same `Game` the command is then handed. If its scope were still open,
    a second `computed_board()` on that game would be served from the cache
    instead of computed."""
    stage_board(game, BOARD[:2])
    run_command = commands.run_command
    fresh: list[int] = []

    def checked_run_command(committed: Any, *args: Any, **kwargs: Any) -> Any:
        before = board_computations()
        committed.computed_board()
        committed.computed_board()
        fresh.append(board_computations() - before)
        return run_command(committed, *args, **kwargs)

    monkeypatch.setattr(commands, "run_command", checked_run_command)
    plains = next(card for card in store.get(game).player("alice").hand if card.name == "Plains")
    response = client.post(
        f"/api/games/{game}/actions/play-land",
        params={"uid": _ADMIN_KEYS[game]},
        json={"player": "alice", "card_id": str(plains.id)},
    )
    assert response.status_code == 200, response.text
    assert fresh == [2]


def _read_bodies(game_id: str) -> dict[str, bytes]:
    """GET state and GET legal-actions (candidates on and off) as raw bytes,
    answered by the app in this thread's own context - an ASGI transport run
    by `asyncio.run`, whose tasks and threadpool calls inherit the caller's
    `ContextVar`s, so `uncached_boards()` around it reaches the routes.
    `TestClient` runs the app on a portal thread that would not see it."""
    admin = _ADMIN_KEYS[game_id]
    alice = seat_uid(game_id, "alice")
    bob = seat_uid(game_id, "bob")

    async def read() -> dict[str, bytes]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            requests = {
                "state_admin": (f"/api/games/{game_id}", {"uid": admin}),
                "state_bob": (f"/api/games/{game_id}", {"uid": bob}),
                "legal_actions": (
                    f"/api/games/{game_id}/legal-actions",
                    {"player": "alice", "uid": alice},
                ),
                "legal_actions_candidates": (
                    f"/api/games/{game_id}/legal-actions",
                    {"player": "alice", "uid": alice, "candidates": "true"},
                ),
            }
            bodies: dict[str, bytes] = {}
            for name, (path, params) in requests.items():
                response = await http.get(path, params=params)
                assert response.status_code == 200, response.text
                bodies[name] = response.content
            return bodies

    return asyncio.run(read())


@pytest.mark.parametrize(
    ("names", "graveyard"),
    [(BOARD[:2], False), (BOARD, False), (BOARD, True), (["Plains", "Sacred Foundry"], True)],
    ids=["small", "busy", "busy-with-graveyard", "lands-only"],
)
def test_board_reads_are_byte_identical_with_every_board_computed_afresh(
    client, names, graveyard
) -> None:
    game_id = new_game(client)
    try:
        stage_board(game_id, names, graveyard=graveyard)

        before = board_computations()
        cached = _read_bodies(game_id)
        cached_boards = board_computations() - before
        before = board_computations()
        with uncached_boards():
            uncached = _read_bodies(game_id)
        uncached_boards_computed = board_computations() - before

        # The uncached run really did reach the routes: it paid more boards.
        assert uncached_boards_computed > cached_boards
        for name in cached:
            assert cached[name] == uncached[name], name
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": _ADMIN_KEYS.pop(game_id)})


def _power_of(view: dict, name: str, controller: str) -> tuple[int, int]:
    card = next(
        card
        for card in view["battlefield"]
        if card["name"] == name and card["controller"] == controller
    )
    return card["power"], card["toughness"]


def test_an_action_is_followed_by_the_board_it_left_not_a_stale_one(client, game) -> None:
    """Angel of Jubilation's anthem (+1/+1 to other nonblack creatures) is a
    layer 7c effect, so it shows only on the computed board. The resolve
    request enumerates the board before resolving (`legal_actions_before`)
    and builds its response after; both it and the next GET state must see
    the anthem."""
    admin = _ADMIN_KEYS[game]
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    stage_card(game, "Grizzly Bears", "alice", "battlefield")
    for _ in range(4):
        stage_card(game, "Plains", "alice", "battlefield")
    stage_card(game, "Angel of Jubilation", "alice", "hand")

    view = client.get(f"/api/games/{game}", params={"uid": admin}).json()
    assert _power_of(view, "Grizzly Bears", "alice") == (2, 2)
    angel = next(card for card in view["hands"]["alice"] if card["name"] == "Angel of Jubilation")
    plains = [card["id"] for card in view["battlefield"] if card["name"] == "Plains"]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        params={"uid": admin},
        json={"player": "alice", "card_id": angel["id"], "mana_sources": plains},
    )
    assert cast.status_code == 200 and cast.json()["allowed"] is True, cast.text

    resolved = client.post(f"/api/games/{game}/resolve-top", params={"uid": admin})
    assert resolved.status_code == 200, resolved.text
    assert _power_of(resolved.json(), "Grizzly Bears", "alice") == (3, 3)

    after = client.get(f"/api/games/{game}", params={"uid": admin}).json()
    assert _power_of(after, "Grizzly Bears", "alice") == (3, 3)
    assert _power_of(after, "Angel of Jubilation", "alice") == (3, 3)


def _make_the_board_stale(game: Game, kind: str) -> CardState | None:
    """A real change the engine has to catch, made while a read holds a cached
    board: `stale` edits the game in place without announcing it and then makes
    an announced store, which is where the engine checks the cached board and
    raises `BoardSnapshotStale`; `missing` puts a permanent onto the battlefield
    in place and asks the cached board about it, which raises `NotOnBoard`
    (and returns the permanent it put there, for a caller to take back)."""
    printing = next(iter(game.battlefield)).card
    if kind == "stale":
        exiled = CardState(card=printing, zone=Zone.EXILE, controller="alice", owner="alice")
        game.shared_zones.setdefault(Zone.EXILE, []).append(exiled)
        bob = game.player("bob")
        bob.life = bob.life
        return None
    else:
        arrived = CardState(card=printing, zone=Zone.BATTLEFIELD, controller="alice", owner="alice")
        arrived.bind_to_game(game)
        game.shared_zones[Zone.BATTLEFIELD].append(arrived)
        try:
            game.characteristics_of(arrived)
        finally:
            _put_there.append(arrived)
        return arrived


#: Permanents `_make_the_board_stale` put onto a battlefield in place.
_put_there: list[CardState] = []


@contextmanager
def _stale_on_first_call(
    game: Game, callee: CodeType, kind: str, subject: CardState | None = None
) -> Iterator[list[bool]]:
    """For the extent of the block, the first call into `callee` in this thread
    (on `subject`, when given) makes the board stale from inside that call."""
    fired: list[bool] = []

    def tracer(frame: FrameType, event: str, arg: Any) -> None:
        if event != "call" or frame.f_code is not callee or fired:
            return None
        if subject is not None and frame.f_locals.get("permanent") is not subject:
            return None
        fired.append(True)
        _make_the_board_stale(game, kind)
        return None

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        yield fired
    finally:
        sys.settrace(previous)


STALE_KINDS = pytest.mark.parametrize(
    ("kind", "raised"), [("stale", BoardSnapshotStale), ("missing", NotOnBoard)]
)


def _loyalty_clause(game: Game, permanent: CardState) -> str:
    ability = next(
        ability
        for ability in game.activated_abilities(permanent)
        if ability.mana is not None and ability.loyalty is not None
    )
    return (
        f"{permanent.name} was not auto-tapped because its only mana ability costs "
        f"more than {{T}} ({ability.cost}: {ability.effect}) - activate it manually to "
        "include it"
    )


@STALE_KINDS
def test_a_board_gone_stale_inside_the_loyalty_advice_filter_is_re_read_and_filtered(
    client, game, caplog, kind, raised
) -> None:
    """Ugin, Eye of the Storms' `0: Add {C}{C}{C}` is a loyalty ability, so its
    "activate it manually" clause is filtered. A board that goes stale while
    Ugin's abilities are read must send the whole filter round again on fresh
    boards, never skip Ugin and leave the clause in."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Ugin, Eye of the Storms", "alice", "battlefield")
    live = store.get(game)
    assert live is not None
    ugin = next(card for card in live.battlefield if card.name == "Ugin, Eye of the Storms")
    reason = f"cannot pay {{9}}: {_loyalty_clause(live, ugin)}"

    with (
        caplog.at_level(logging.WARNING, logger="mcm.board_scope"),
        _stale_on_first_call(live, Game.activated_abilities.__code__, kind, ugin) as fired,
    ):
        filtered = engine_api.without_loyalty_mana_advice(live, reason)

    _put_there.clear()
    assert fired, "the board really went stale inside Ugin's read"
    assert filtered == "cannot pay {9}"
    assert any("afresh" in record.getMessage() for record in caplog.records), caplog.records


def _sites(game: Game) -> dict[str, tuple[CodeType, CardState | None, Callable[[], Any]]]:
    """Every broad `except` in the api that runs inside a board scope around an
    engine read of the board: the engine call it guards, and a call reaching it."""
    sol_ring = next(card for card in game.battlefield if card.name == "Sol Ring")
    ugin = next(card for card in game.battlefield if card.name == "Ugin, Eye of the Storms")
    abilities = Game.activated_abilities.__code__
    clause = f"x: {_loyalty_clause(game, ugin)}"
    return {
        "loyalty-advice": (
            abilities,
            ugin,
            lambda: engine_api._without_loyalty_mana_advice(game, clause),
        ),
        "mana-ability-at": (
            abilities,
            sol_ring,
            lambda: engine_api.mana_ability_at(game, sol_ring, 0),
        ),
        "sole-mana-ability": (
            abilities,
            sol_ring,
            lambda: engine_api._sole_mana_ability(game, sol_ring),
        ),
        "own-count": (
            engine_api._resolve.__code__,
            None,
            lambda: engine_api._expanded_to_the_abilitys_own_count(sol_ring, game, [Color("C")]),
        ),
        "list-abilities": (abilities, sol_ring, lambda: engine_api.list_abilities(game, sol_ring)),
    }


SITES = ["loyalty-advice", "mana-ability-at", "sole-mana-ability", "own-count", "list-abilities"]


@STALE_KINDS
@pytest.mark.parametrize("site", SITES)
def test_no_broad_except_inside_a_board_scope_swallows_a_stale_board(
    client, game, kind, raised, site
) -> None:
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "alice", "battlefield")
    add_card(client, game, "Ugin, Eye of the Storms", "alice", "battlefield")
    live = store.get(game)
    assert live is not None
    callee, subject, call = _sites(live)[site]

    with one_board(live):
        live.computed_board()
        with _stale_on_first_call(live, callee, kind, subject) as fired, pytest.raises(raised):
            call()
        # Taken back before this test's own scope ends, which would otherwise
        # catch the unannounced append itself.
        while _put_there:
            live.shared_zones[Zone.BATTLEFIELD].remove(_put_there.pop())
    assert fired


def _race_a_state_view(
    client, game: str, monkeypatch: pytest.MonkeyPatch, write: Callable[[Callable[[], None]], None]
) -> httpx.Response:
    """GET state parked right after its first battlefield card was read - the
    board computed and cached - while `write` runs in this thread. `write` is
    handed `carry_on`, which lets the view go on over the rest of the live
    battlefield list and waits for its response, so a write can let the read
    through at exactly the point of the edit it is racing."""
    parked, resume, read_done = threading.Event(), threading.Event(), threading.Event()
    armed = [True]
    card_view = serialize._card_view

    def parking_card_view(board: Game, card: CardState, images: Any) -> dict[str, Any]:
        view = card_view(board, card, images)
        if armed[0] and card.zone is Zone.BATTLEFIELD:
            armed[0] = False
            parked.set()
            assert resume.wait(30)
        return view

    monkeypatch.setattr(serialize, "_card_view", parking_card_view)
    outcome: dict[str, Any] = {}

    def read() -> None:
        try:
            outcome["response"] = client.get(
                f"/api/games/{game}", params={"uid": _ADMIN_KEYS[game]}
            )
        except BaseException as exc:  # noqa: BLE001 - handed to the test thread
            outcome["error"] = exc
        finally:
            read_done.set()

    def carry_on() -> None:
        resume.set()
        assert read_done.wait(30), "the state view never finished"

    reader = threading.Thread(target=read)
    reader.start()
    try:
        assert parked.wait(30), "the state view never reached the battlefield"
        write(carry_on)
    finally:
        resume.set()
        reader.join(30)
    assert "error" not in outcome, outcome.get("error")
    return outcome["response"]


def _battlefield_names(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [card["name"] for card in response.json()["battlefield"]]


def test_a_state_view_racing_the_setup_route_returns_the_card_it_put_on_the_battlefield(
    client, game, monkeypatch, caplog
) -> None:
    """`POST .../cards` appends to the battlefield list in place, outside any
    engine command and outside the lock the read does not take, and only then
    stores the new card's own fields. The view is let through right after the
    append: the route announces that edit, so the view recomputes its board
    rather than asking the cached one about a card it was never computed
    with, and needs no re-read."""
    for _ in range(3):
        add_card(client, game, "Forest", "alice", "battlefield")
    put_card_in_zone = engine_api.put_card_in_zone

    def write(carry_on: Callable[[], None]) -> None:
        def put_then_let_the_view_through(*args: Any, **kwargs: Any) -> None:
            put_card_in_zone(*args, **kwargs)
            carry_on()

        monkeypatch.setattr(engine_api, "put_card_in_zone", put_then_let_the_view_through)
        add_card(client, game, "Memnite", "alice", "battlefield")

    with caplog.at_level(logging.WARNING, logger="mcm.board_scope"):
        response = _race_a_state_view(client, game, monkeypatch, write)

    assert _battlefield_names(response).count("Memnite") == 1
    assert not [r for r in caplog.records if "afresh" in r.getMessage()], "announced: no re-read"


def test_a_state_view_racing_an_unannounced_battlefield_append_is_read_again(
    client, game, monkeypatch, caplog
) -> None:
    """An in-place append nobody announced leaves the cached board without the
    card; the lookup's `NotOnBoard` is a stale board, and the view is read
    again with every board computed afresh."""
    for _ in range(3):
        add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Memnite", "alice", "hand")
    live = store.get(game)
    assert live is not None

    def write(carry_on: Callable[[], None]) -> None:
        printing = next(card for card in live.player("alice").hand if card.name == "Memnite").card
        arrived = CardState(card=printing, zone=Zone.BATTLEFIELD, controller="alice", owner="alice")
        arrived.bind_to_game(live)
        live.shared_zones[Zone.BATTLEFIELD].append(arrived)
        carry_on()

    with caplog.at_level(logging.WARNING, logger="mcm.board_scope"):
        response = _race_a_state_view(client, game, monkeypatch, write)

    assert _battlefield_names(response).count("Memnite") == 1
    assert [r for r in caplog.records if "afresh" in r.getMessage()], "the view was read again"


def test_loyalty_mana_advice_reads_one_board_on_any_battlefield(client) -> None:
    """A refused cast whose reason carries the engine's "activate it manually"
    advice has that advice filtered by `engine_api.without_loyalty_mana_advice`
    after the command, which reads every permanent's activated abilities -
    one board for all of them, whatever the size of the battlefield."""
    from magic_cards_model_api import engine_api

    reason = "cannot pay {2}: Sol Ring was not auto-tapped - activate it manually to include it"
    small = new_game(client)
    large = new_game(client)
    try:
        stage_board(small, BOARD[:2])
        stage_board(large, BOARD)
        counts = {}
        for label, game_id in (("small", small), ("large", large)):
            game = store.get(game_id)
            assert game is not None
            before = board_computations()
            engine_api.without_loyalty_mana_advice(game, reason)
            counts[label] = board_computations() - before

        assert counts["large"] <= 1, counts
        assert counts["large"] == counts["small"], counts
    finally:
        for game_id in (small, large):
            client.delete(f"/api/games/{game_id}", params={"key": _ADMIN_KEYS.pop(game_id)})
