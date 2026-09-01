"""`seat_cli.py` and `mcp.py` are two front ends onto one seat. Where they
disagree, one of them is wrong, and a seat pays for it in real games.

The bug that opened this file: `seat_cli` held priority after the five special
actions (CR 116.3, "if a player takes a special action, that player receives
priority afterward") while the matching MCP tools released it, so an agent
played a land in its precombat main, released, and concluded it had to wait
until postcombat to cast the one-drop that land had just paid for. These tests
pin down the rest of that class - the shared defaults, the shared parsing, and
the commands one front end had and the other simply did not.

Every CR number in this module was verified against the Comprehensive Rules
with `knowledge_search` in the session that wrote it. Every card is a real
Scryfall printing from tests/fixtures/scryfall, and every board is a real
board played through the real API - no mocks, per the project's rule.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import httpx
from conftest import add_card, find, state
from fastapi.testclient import TestClient
from test_mulligans import (  # noqa: E402  (a sibling test module's real-deck helpers)
    LEYLINE_DECK,
    PLAIN_DECK,
    SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    boot,
    declare,
    delete,
    view,
)

from magic_cards_model_api import mcp as mcp_module
from magic_cards_model_api import seat_cli, seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


class _StaleHealth:
    """Stands in for `Seats.http` while asking `seatkit.api_trouble` what it
    would say about an API on a different `PROTOCOL_VERSION` - the same shape
    `test_mcp_resilience.py._FakeHttp` uses, and the only part of any test here
    that is not a real board: `/api/health` reports a build number, not game
    state, and this process only ever has one build number to report."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def get(self, path: str, timeout: float | None = None) -> Any:
        assert path == "/api/health"
        return self

    def json(self) -> dict[str, Any]:
        return self._payload


def wire(monkeypatch) -> None:
    """Point every `httpx.Client` this process builds at the in-process app,
    the same trick `test_action_flow.py` uses."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))


def uid_for(game_id: str, player: str) -> str:
    return next(uid for uid, who in store.seat_uids(game_id).items() if who == player)


def floating(board: dict[str, Any], player: str) -> dict[str, int]:
    return next(p for p in board["players"] if p["username"] == player)["mana_pool"]["floating"]


def a_forest(client: TestClient, game_id: str, uid: str) -> str:
    """One real Forest on alice's battlefield; its id. The engine synthesises
    the basic land's mana ability at index 0 (see `engine_api.activate_ability`),
    so `activate <id> 0` is the tap-for-{G} every seat actually makes."""
    add_card(client, game_id, "Forest", "alice", "battlefield")
    return find(state(client, game_id, uid)["battlefield"], "Forest")["id"]


# -- a mana ability must never auto-pass priority ----------------------------


def test_activating_a_mana_ability_keeps_priority_and_the_mana(client, game, monkeypatch):
    """CR 605.3b: an activated mana ability "doesn't go on the stack ... it
    resolves immediately after it is activated". There is nothing to release
    priority *for*, and releasing is destructive: the harness's auto-pass is
    half of "all players pass in succession while the stack is empty", which
    ends the phase (CR 500.2, CR 505.2), and "any unspent mana left in a
    player's mana pool empties" as it does (CR 500.5).

    So a seat that taps a Forest and gets auto-passed has made a green mana it
    can never spend, and thrown away the rest of its own main phase to do it.
    Before this was fixed, both front ends did exactly that.
    """
    wire(monkeypatch)
    uid = uid_for(game, "alice")
    forest = a_forest(client, game, uid)
    seat = Seats(api="http://testserver", game_id=game).seat_for(uid)

    result = seatkit.run_action(
        seat,
        lambda: seat.call_json(
            "POST",
            f"/api/games/{game}/actions/activate",
            {"player": "alice", "card_id": forest, "ability_index": 0},
        ),
        hold_priority=False,
        max_seconds=2,
    )

    assert result["outcome"]["allowed"] is True, result
    assert result["outcome"]["mana_ability"] is True
    assert result["held_priority"] is True, (
        "a mana ability does not use the stack (CR 605.3b) - releasing priority "
        "here can end the step (CR 500.2) and empty the mana with it (CR 500.5)"
    )
    assert result["state"]["priority_holder"] == "alice"
    assert floating(result["state"], "alice") == {"G": 1}


def test_both_front_ends_keep_the_mana_from_a_tapped_land(client, game, monkeypatch, capsys):
    """The same call, once through each front end, on two independent games:
    neither may hand priority away after a mana ability. This is the parity
    half - the rules half is the test above."""
    wire(monkeypatch)

    # -- seat_cli --
    uid = uid_for(game, "alice")
    forest = a_forest(client, game, uid)
    assert seat_cli.run(["--uid", uid, "--game", game, "activate", forest, "0"]) == 0
    cli_answer = json.loads(capsys.readouterr().out)
    assert cli_answer["held_priority"] is True
    assert cli_answer["state"]["priority_holder"] == "alice"
    assert floating(cli_answer["state"], "alice") == {"G": 1}

    # -- the MCP tool, same shape on a second game --
    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    try:
        uid2 = uid_for(second, "alice")
        forest2 = a_forest(client, second, uid2)
        server = build_server(Seats(api="http://testserver", game_id=second))
        answer = asyncio.run(
            server.call_tool(
                "activate", {"player_uid": uid2, "card_id": forest2, "ability_index": 0}
            )
        )
        mcp_answer = json.loads(answer.content[0].text)
        assert mcp_answer["held_priority"] is True
        assert mcp_answer["state"]["priority_holder"] == "alice"
        assert floating(mcp_answer["state"], "alice") == {"G": 1}
    finally:
        client.delete(f"/api/games/{second}")


def test_an_ability_that_uses_the_stack_still_releases(client, game, monkeypatch):
    """The counterweight: the mana-ability hold must not quietly become "every
    activation holds". Karn, the Great Creator's +1 is an ordinary activated
    ability - it goes on the stack, and releasing priority is exactly how it
    gets to resolve. `targets: []` is a real announcement of zero for its "up
    to one target" (CR 115.6), so nothing here stops to ask."""
    wire(monkeypatch)
    uid = uid_for(game, "alice")
    add_card(client, game, "Karn, the Great Creator", "alice", "battlefield")
    karn = find(state(client, game, uid)["battlefield"], "Karn, the Great Creator")["id"]
    seat = Seats(api="http://testserver", game_id=game).seat_for(uid)

    result = seatkit.run_action(
        seat,
        lambda: seat.call_json(
            "POST",
            f"/api/games/{game}/actions/activate",
            {
                "player": "alice",
                "card_id": karn,
                "ability_index": 0,
                "targets": [],
            },
        ),
        hold_priority=False,
        max_seconds=2,
    )

    assert result["outcome"]["allowed"] is True, result
    assert result["outcome"].get("mana_ability") is not True
    assert result["held_priority"] is False, "an ability on the stack needs priority released"


# -- the CR 103.6 opening-hand action the CLI simply did not have -------------


def test_the_seat_cli_can_take_an_opening_hand_action(client, monkeypatch, capsys):
    """CR 103.6a: Leyline of the Void's "if this card is in your opening hand,
    you may begin the game with it on the battlefield". `mcp.py` has had
    `opening_hand_action` for a whole wave; `seat_cli` had only the command
    that *finishes* the window, so a CLI seat could decline what its opening
    hand offered but never take it - a free permanent thrown away every game.
    """
    wire(monkeypatch)
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    )
    try:
        for player in ("alice", "bob"):
            assert declare(client, info, player, take=False).status_code == 200
        (offer,) = view(client, info, "alice")["pregame"]["opening_hand_options"]["alice"]
        assert offer["source"] == "Leyline of the Void"

        rc = seat_cli.run(
            [
                "--uid",
                info["seat_uids"]["alice"],
                "--game",
                info["game_id"],
                "opening-hand-action",
                offer["card_id"],
            ]
        )

        assert rc == 0, capsys.readouterr().out
        game_object = store.get(info["game_id"])
        assert [card.name for card in game_object.battlefield] == ["Leyline of the Void"]
    finally:
        delete(client, info)


# -- shared parsing, shared guard --------------------------------------------


def test_an_unparseable_sources_value_still_offers_every_untapped_source(
    client, game, monkeypatch, capsys
):
    """`--sources ","` parses to no ids at all. `mcp.py` read that as "nothing
    was named, offer everything"; `seat_cli` branched on the raw string
    instead and offered the engine *no* sources, so the cast was refused for
    mana the seat plainly had. One rule, one place: `Seat.sources_or_all`."""
    wire(monkeypatch)
    uid = uid_for(game, "alice")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")

    assert (
        seat_cli.run(
            [
                "--uid",
                uid,
                "--game",
                game,
                "cast",
                "Lightning Bolt",
                "--target",
                "bob",
                "--sources",
                ",",
                "--dry-run",
            ]
        )
        == 0
    )

    answer = json.loads(capsys.readouterr().out)
    assert answer["allowed"] is True, answer


def test_the_seat_cli_refuses_a_protocol_mismatch_the_way_every_mcp_tool_does(
    client, game, monkeypatch, capsys
):
    """`mcp.py` checks `seatkit.PROTOCOL_VERSION` before every tool call
    (`_guarded`); `seat_cli` checked nothing, so a command built from a
    different checkout than the running API found out by misreading an answer
    it did not understand. Same check, same wording, both front ends.

    The mismatch itself is staged the way `test_mcp_resilience.py` stages it -
    on the health probe, which is a plain `GET /api/health` - because both
    sides of the comparison read the one live `seatkit.PROTOCOL_VERSION` in
    this process, so an in-process game can never disagree with itself. The
    first assertion pins the CLI to the *shared* guard, so this cannot pass
    against a private reimplementation that drifts from `mcp.py`'s.
    """
    wire(monkeypatch)
    assert seat_cli.api_trouble is seatkit.api_trouble

    stale = seatkit.api_trouble(
        _StaleHealth({"status": "ok", "protocol_version": "seatkit-from-the-future"}),
        "http://testserver",
    )
    monkeypatch.setattr(seat_cli, "api_trouble", lambda http, api: stale)

    rc = seat_cli.run(["--uid", uid_for(game, "alice"), "--game", game, "state"])

    assert rc == 1
    printed = capsys.readouterr().out
    assert "PROTOCOL MISMATCH" in printed
    assert "seatkit-from-the-future" in printed
    assert seatkit.PROTOCOL_VERSION in printed
    assert "active_player" not in printed, "a refused call must not go on to act"


# -- the docstrings are the agent's only instructions ------------------------


def build_a_server() -> Any:
    """A server whose tools are only ever introspected, never called - so the
    `Seats` behind it never has to reach an API."""
    seats = Seats.__new__(Seats)
    seats.api = "http://testserver"
    seats._pinned_game_id = "unused"
    return build_server(seats)


def hold_defaults() -> dict[str, bool]:
    """Every MCP tool taking `hold_priority`, and what it defaults to. Read off
    the live signatures rather than a list kept by hand, so a tool whose default
    is flipped without its docstring being touched fails here."""
    defaults = {}
    for name, fn in _tool_functions(build_a_server()).items():
        parameter = inspect.signature(fn).parameters.get("hold_priority")
        if parameter is not None and parameter.default is not inspect.Parameter.empty:
            defaults[name] = parameter.default
    return defaults


def _tool_functions(server: Any) -> dict[str, Any]:
    """The registered tool callables, by tool name - the same objects an MCP
    client's `tools/list` describes itself from, so a docstring checked here is
    the docstring the model actually reads."""
    registry = server._tool_manager._tools
    assert registry, "the MCP server registered no tools at all"
    return {name: entry.fn for name, entry in registry.items()}


def test_every_hold_by_default_tool_says_so_in_its_own_docstring():
    """The MCP docstrings are the only instructions the model reading them
    gets. A tool that now holds priority by default while its docstring still
    says "releases priority unless hold_priority is true" actively teaches the
    agent the opposite of what the tool does - which is how the land-drop bug
    survived being half-fixed."""
    defaults = hold_defaults()
    assert defaults, "no MCP tool exposes hold_priority - the introspection is wrong"
    holds = {name for name, default in defaults.items() if default is True}
    assert holds == {"play_land", "suspend", "plot", "foretell", "cycle"}, holds

    for name in sorted(defaults):
        docstring = inspect.getdoc(_tool_functions(build_a_server()).get(name)) or ""
        if name in holds:
            assert "HOLDS PRIORITY BY DEFAULT" in docstring, (
                f"{name} holds priority by default and must say so in capitals"
            )
        else:
            assert "HOLDS PRIORITY BY DEFAULT" not in docstring, (
                f"{name} releases priority by default - its docstring claims otherwise"
            )


def test_the_mcp_module_docstring_does_not_promise_a_uniform_release():
    """The paragraph in `mcp.py` describing the action tools is read as an
    instruction too. It used to say, flatly, that every action tool "releases
    priority unless told to hold" - untrue of five tools and of every mana
    ability."""
    docstring = " ".join((mcp_module.__doc__ or "").split())
    for tool in ("play_land", "suspend", "plot", "foretell", "cycle"):
        assert tool in docstring, f"{tool} holds priority by default and the module never says so"
    assert "hold priority by default" in docstring
    assert "CR 116.3" in docstring
    assert "CR 605.3b" in docstring, "the mana-ability hold is not described either"


def test_the_seat_cli_module_docstring_does_not_promise_a_uniform_release():
    """The CLI's own docstring is what `--help` prints, and it made the same
    flat claim ("release priority unless `--hold` is given")."""
    docstring = seat_cli.__doc__ or ""
    assert "release priority unless `--hold` is given" not in docstring
    for command in ("play-land", "suspend", "plot", "foretell", "cycle"):
        assert command in docstring
    assert "CR 605.3b" in docstring


def test_cycling_is_not_described_as_a_special_action():
    """CR 702.29a makes cycling an ordinary activated ability - "'Cycling
    [cost]' means '[Cost], Discard this card: Draw a card.'" - not one of
    CR 116.2's special actions. It holds priority with them, but on CR 117.3c,
    and a docstring that cites CR 116.3 for it teaches the agent a rule that is
    not there. An unlooked-up CR number is a defect even when the behaviour it
    justifies is right."""
    docstring = inspect.getdoc(_tool_functions(build_a_server())["cycle"]) or ""
    assert "CR 702.29a" in docstring
    assert "CR 117.3c" in docstring
    assert "not a special action" in docstring
