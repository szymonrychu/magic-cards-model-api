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
import pytest
from conftest import add_card, find, state
from fastapi.testclient import TestClient
from test_card_split_labels import boot as boot_the_devourer  # noqa: E402  (the Devourer board)
from test_card_split_labels import (
    reach_the_devourer_upkeep,
    resolve_top_in_background,
    wait_for_pending,
)
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
        # `board`, not `state`: an MCP action answer carries the narrowed
        # board (`mcp._action_answer`), where the CLI prints `run_action`'s
        # own full `state` for a human to scroll. Who holds priority and what
        # is floating are in both, and that is what parity is about here.
        assert mcp_answer["board"]["priority_holder"] == "alice"
        assert floating(mcp_answer["board"], "alice") == {"G": 1}
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


def test_an_unparseable_sources_value_still_pays_for_the_cast(client, game, monkeypatch, capsys):
    """`--sources ","` parses to no ids at all. `seat_cli` once branched on the
    raw string and offered the engine *no* sources, so the cast was refused for
    mana the seat plainly had. It now names none, exactly as the MCP tool does
    for an empty `mana_sources`, and the engine's automatic payment pays
    (B5-05, `tests/test_named_mana_sources.py`)."""
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
    assert holds == {
        "play_land",
        "suspend",
        "plot",
        "foretell",
        "cycle",
        # FD5 (5): turn_face_up/bring_companion are CR 116.2 special
        # actions too - CR 116.3 gives priority back after either, so both
        # hold by default like every other special action tool above.
        "turn_face_up",
        "bring_companion",
        # CR 709.5e: unlock_door, a Room's own unlock cost - also a special
        # action (CR 116.1: it does not use the stack), so it holds by
        # default too.
        "unlock_door",
    }, holds

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


# -- a look-then-split's destinations, in both front ends ---------------------
#
# The same bug `tests/test_card_split_labels.py` fixed for the browser, one
# layer over: an AI seat reads these docstrings and this help text the way a
# human reads the DOM, and both of them stated CR 701.22a's bottom-of-library
# and CR 701.25a's graveyard as the only two places a split's halves can go.
# The engine reuses `DecisionKind.SCRY` for any look-then-split, so that pair
# is a property of the *keyword*, never of the kind - Devourer of Destiny's
# first-upkeep trigger keeps the named card on top of the library and exiles
# the rest, the reverse of a scry on both halves. `pending_decision`'s
# `moved_label`/`kept_label` carry the asking card's own words, and every seat
# has to be told to read them.
#
# CR 701.22a ("To 'scry N' means to look at the top N cards of your library,
# then put any number of them on the bottom of your library in any order and
# the rest on top of your library in any order") and CR 701.25a ("... put any
# number of them into your graveyard and the rest on top of your library in any
# order") were both re-verified with `knowledge_search(type="rules")` in the
# session that wrote this section. They stay in the text - as the fallback for
# a decision the keywords themselves raise, which is the only thing they are.

THE_OLD_HARDCODED_PAIR = (
    "to the bottom of the library for a scry",
    "to the graveyard for a surveil",
    "Every card not named stays on top",
)


def decide_docstring() -> str:
    return inspect.getdoc(_tool_functions(build_a_server())["decide"]) or ""


def test_the_decide_docstring_sends_a_seat_to_the_labels_for_the_destinations():
    """`decide` is an AI seat's only instructions for answering a split. It
    must name the two fields, say they are authoritative, and no longer state
    the scry/surveil pair as the only possibility."""
    docstring = decide_docstring()

    assert "moved_label" in docstring, "the field a seat has to read is never named"
    assert "kept_label" in docstring
    for lie in THE_OLD_HARDCODED_PAIR:
        assert lie not in docstring, (
            f"the docstring still states one pair of destinations as the only one: {lie!r}"
        )
    assert "authoritative" in docstring
    assert "Devourer of Destiny" in docstring, (
        "the card that breaks the assumption is the shortest way to teach it"
    )
    assert "exiled" in docstring


def test_the_decide_docstring_keeps_the_keyword_pair_only_as_the_fallback():
    """CR 701.22a and CR 701.25a are still right for a decision `scry` or
    `surveil` itself raised, so the numbers stay - but only as what to assume
    when no label was stamped."""
    docstring = " ".join(decide_docstring().split())

    assert "CR 701.22a" in docstring and "CR 701.25a" in docstring
    fallback = docstring.index("Only when both labels are absent or empty")
    assert fallback < docstring.index("CR 701.22a"), (
        "both keyword rules must sit inside the fallback sentence, not before it"
    )


def test_battlefield_state_tells_a_seat_the_two_label_fields_are_there():
    """A seat that never reads `decide`'s bullet still reads its own state.
    `battlefield_state` describes what that payload holds, so it has to mention the two
    fields that decide where a split's cards go."""
    docstring = inspect.getdoc(_tool_functions(build_a_server())["battlefield_state"]) or ""

    assert "pending_decision" in docstring
    assert "moved_label" in docstring and "kept_label" in docstring
    assert "authoritative" in docstring


def subcommand(name: str) -> Any:
    """The `argparse` sub-parser for one command, off the live parser - so the
    help pinned here is the help `seat_cli <command> --help` prints."""
    sub = next(
        action for action in seat_cli.build_parser()._actions if getattr(action, "choices", None)
    )
    return sub.choices[name]


def moved_help() -> str:
    action = next(
        entry
        for entry in subcommand("decide")._actions
        if "--moved" in getattr(entry, "option_strings", ())
    )
    return " ".join((action.help or "").split())


def test_the_cli_moved_help_sends_a_seat_to_the_labels_too():
    """`seat_cli --help` is the other front end's only instructions, and it
    told the same lie in one line."""
    help_text = moved_help()

    assert "moved_label" in help_text and "kept_label" in help_text
    assert "to the bottom, CR 701.22a) or surveil (to the graveyard" not in help_text, (
        "the old one-line hardcoded pair is still the whole answer"
    )
    assert "CR 701.22a" in help_text and "CR 701.25a" in help_text, (
        "the keyword rules are still right for the keywords - keep them"
    )
    assert "Devourer of Destiny" in help_text
    assert "Only when both labels are absent" in help_text


def test_the_cli_moved_help_states_the_count_too():
    """L1a: `--moved`'s help named the destinations but never how many cards
    may go there - `pending_decision.amount`/`.exact` govern that count the
    same way `card_ids`'s own help already explains for `choose_cards`
    (compare the `card_ids` argument's help, above), and `validate_answer`
    refuses an answer outside it (PR #31)."""
    help_text = moved_help()

    assert "pending_decision.exact" in help_text
    assert "pending_decision.amount" in help_text


def test_the_decide_docstring_states_the_count_for_a_split():
    r"""L1a: the MCP `decide` tool's scry/surveil bullet is the analogue of
    the CLI help above - it explained `moved_label`/`kept_label` but never
    `pending_decision.amount`/`.exact`, so an agent reading it had no way to
    know a split answered with the wrong count would be refused.

    Scoped to the bullet itself (`- \`scry\` and \`surveil\`` up to the next
    bullet, `- \`opening_hand_action\``), not the whole docstring - the
    `choose_cards` bullet already explains `exact`/`amount` for its own kind,
    and a whole-docstring search would pass without the scry/surveil bullet
    ever mentioning either."""
    docstring = decide_docstring()
    start = docstring.index("`scry` and `surveil`")
    end = docstring.index("`opening_hand_action`")
    section = docstring[start:end]

    assert "pending_decision.exact" in section
    assert "pending_decision.amount" in section


def test_the_cli_state_prints_the_devourer_splits_two_destinations(client, monkeypatch, capsys):
    """Telling a seat the fields exist is not the same as showing them. A CLI
    seat gets a whole state dump; two short strings inside it are exactly what
    a reader skims past, and getting them wrong is what sent the reported game
    the wrong way. So `state` says them out loud.

    The board is the real one `tests/test_card_split_labels.py` builds: a real
    decklist of real Scryfall printings, the engine's own seeded shuffle
    (CR 103.3), CR 103.5's mulligan kept, CR 103.6b's opening-hand reveal
    taken, run to CR 103.7's first upkeep where Devourer of Destiny's delayed
    trigger (CR 603.7) asks the question. Nothing here is mocked.

    Stdout stays pure JSON - other programs parse it - so the note goes to
    stderr, and both halves are asserted.
    """
    wire(monkeypatch)
    info = boot_the_devourer(client)
    game_id, uid = info["game_id"], info["seat_uids"]["alice"]
    try:
        reach_the_devourer_upkeep(client, info)
        thread = resolve_top_in_background(game_id)
        try:
            pending = wait_for_pending(client, game_id, uid)
            rc = seat_cli.run(["--uid", uid, "--game", game_id, "state"])
            captured = capsys.readouterr()
            # Answer before asserting: the resolving request is parked inside
            # `Game.ask` holding this game's lock, so a failed assertion left
            # unanswered would hang teardown behind a decision timeout.
            answered = client.post(
                f"/api/games/{game_id}/decide",
                params={"uid": uid},
                json={"player": "alice", "moved": [pending["cards"][0]], "top_order": None},
            )
        finally:
            thread.join(timeout=15)
        assert not thread.is_alive()
        assert answered.status_code == 200, answered.text
        assert rc == 0, captured.out

        printed = json.loads(captured.out)
        assert printed["pending_decision"]["kind"] == "scry"

        said = captured.err
        assert "top of your library" in said, f"the kept half is never named: {said!r}"
        assert "exiled" in said, f"the exiled half is never named: {said!r}"
        assert "bottom" not in said, "nothing about this decision uses the bottom of a library"
        assert "Devourer of Destiny" in said, "the note never says which card is asking"
    finally:
        client.delete(f"/api/games/{game_id}")


def test_the_cli_says_nothing_extra_when_there_is_no_split_to_describe(
    client, game, monkeypatch, capsys
):
    """The counterweight: the note is not noise on every command. A state with
    no pending decision - and a decision carrying no labels, which is what an
    older engine or a plain CR 701.22a scry gives - leaves stderr empty and
    stdout untouched."""
    wire(monkeypatch)
    assert seat_cli.run(["--uid", uid_for(game, "alice"), "--game", game, "state"]) == 0
    captured = capsys.readouterr()

    assert json.loads(captured.out)["pending_decision"] is None
    assert captured.err == ""
    assert seat_cli.split_destinations({"pending_decision": None}) == ""
    assert seat_cli.split_destinations({"pending_decision": {"kind": "scry", "cards": []}}) == ""


# -- a search's shape, in both front ends -------------------------------------
#
# The sibling of the section above, and the same failure one wave later. A
# search used to be a walk - the engine offered each matching card in zone
# order and asked yes or no - so a searcher could only decline what it was
# shown and never name what it wanted. A live seat activated Wooded Foothills
# ("{T}, Pay 1 life, Sacrifice this land: Search your library for a Mountain or
# Forest card, put it onto the battlefield, then shuffle.", verified with
# `resolve_cards`) meaning to fetch Breeding Pool ("Land - Forest Island", a
# Forest card, verified the same way) and was handed a basic Forest.
#
# It is one `choose_cards` over every legal candidate now, and three fields on
# `pending_decision` carry that shape: `exact` (is `amount` a count or a
# ceiling), `card_labels` (what the hidden-zone ids are called), and
# `default_cards` (what silence costs). Both front ends' instructions have to
# name all three, for the same reason the labels above did: an AI seat reads a
# docstring and a `--help` line the way a human reads the board, and the engine
# and the browser moved while these two did not.
#
# Every CR number below was verified with `knowledge_search(type="rules")` in
# the session that wrote this section: CR 701.23a (the find is a look at all
# cards in the zone), CR 701.23b (a search for a stated quality is declinable),
# CR 701.23d (a search for a quantity must find that many, or as many as
# possible), CR 107.1c ("any number" includes zero), CR 400.2 (the library is
# not a public zone).

THE_OLD_DISCARD_ONLY_TEXT = (
    "the card id(s) to discard, exactly as many",
    "comma-separated card ids to discard/choose",
)


def test_the_decide_docstring_makes_exact_the_switch_and_the_empty_answer_real():
    """`exact` is what decides whether an answer must name `amount` cards or
    may name fewer, and the old bullet stated the exact case as the only one -
    so a seat obeying it could never take CR 701.23b's decline."""
    docstring = " ".join(decide_docstring().split())

    assert "`exact`" in docstring, "the field that decides the count is never named"
    assert "CR 701.23b" in docstring and "CR 701.23d" in docstring, (
        "the declinable search and the quantity search are the two branches `exact` "
        "picks between - a seat cannot tell them apart without them"
    )
    assert "CR 107.1c" in docstring
    assert "the empty answer is a real answer" in docstring, (
        "finding nothing is a legal answer where `exact` is false, and a seat that "
        "is not told so will never decline a fetch"
    )
    for lie in THE_OLD_DISCARD_ONLY_TEXT:
        assert lie not in docstring, (
            f"the docstring still states one count as the only one: {lie!r}"
        )


def test_the_decide_docstring_teaches_a_search_as_one_choice_over_the_whole_zone():
    """The misplay itself: a walk lets a searcher decline, a single choice lets
    it *name*. The bullet has to say which one this is, name the card that is
    asking, and send the seat to the labels for what the ids are."""
    docstring = " ".join(decide_docstring().split())

    assert "CR 701.23a" in docstring
    assert "not a walk" in docstring, "the shape of the question is never stated"
    assert "every legal candidate" in docstring
    assert "card_labels" in docstring, "the only naming a hidden-zone candidate has is never named"
    assert "CR 400.2" in docstring, "why a library id resolves to nothing is never given"
    assert "default_cards" in docstring and "silence" in docstring, (
        "what an unanswered search takes instead is never stated"
    )
    assert "source` names the card asking" in docstring
    assert "Wooded Foothills" in docstring and "Breeding Pool" in docstring, (
        "the reported misplay is the shortest way to teach the difference"
    )


def test_battlefield_state_tells_a_seat_the_three_search_fields_are_there():
    """A seat that never reads `decide`'s bullet still reads its own state, the
    same argument the `moved_label` guard above makes."""
    docstring = " ".join(
        (inspect.getdoc(_tool_functions(build_a_server())["battlefield_state"]) or "").split()
    )

    assert "`exact` says whether" in docstring
    assert "card_labels" in docstring
    assert "default_cards" in docstring
    assert "CR 701.23b" in docstring and "CR 701.23d" in docstring


def card_ids_help() -> str:
    action = next(entry for entry in subcommand("decide")._actions if entry.dest == "card_ids")
    return " ".join((action.help or "").split())


def test_the_cli_card_ids_help_carries_the_same_three_fields():
    """The other front end's only instructions, and they were one line about
    discarding - no search, no `exact`, nothing about declining."""
    help_text = card_ids_help()

    assert "pending_decision.exact" in help_text
    assert "card_labels" in help_text
    assert "default_cards" in help_text
    assert "CR 701.23a" in help_text
    assert "CR 701.23b" in help_text and "CR 701.23d" in help_text
    assert "CR 107.1c" in help_text and "CR 400.2" in help_text
    for lie in THE_OLD_DISCARD_ONLY_TEXT:
        assert lie not in help_text, f"the old discard-only line survives: {lie!r}"


def test_the_cli_says_out_loud_what_an_omitted_card_ids_means():
    """The ambiguity a defaulted positional carries: an omitted `card_ids` and
    a deliberate CR 701.23b decline post the identical empty answer. The
    contract is unchanged - both front ends still send the empty list - so the
    fix is to say so, and to give the decline a name it can be read by."""
    help_text = card_ids_help()

    assert "OMITTING THIS ARGUMENT SENDS THE EMPTY ANSWER" in help_text
    assert "--decline" in help_text

    decline = next(
        entry
        for entry in subcommand("decide")._actions
        if "--decline" in getattr(entry, "option_strings", ())
    )
    said = " ".join((decline.help or "").split())
    assert "CR 701.23b" in said
    assert "same request as an omitted card_ids" in said, (
        "the flag must not read as a second, different wire contract - it is an alias"
    )


def test_the_cli_refuses_a_decline_that_also_names_cards(client, game, monkeypatch, capsys):
    """Two different answers in one command. Refused before any request, so no
    half-meant answer reaches a decision that cannot be taken back."""
    wire(monkeypatch)
    rc = seat_cli.run(
        ["--uid", uid_for(game, "alice"), "--game", game, "decide", "some-id", "--decline"]
    )

    assert rc == 1
    said = capsys.readouterr().out
    assert "--decline names no cards" in said
    assert "CR 701.23b" in said


# -- every CLI command fronts an MCP tool, or is CLI-only on purpose -----------
#
# The two front ends are one seat, so a command one of them has and the other
# lacks is a gap until someone decides it is not. That decision is this
# allowlist: a command in `CLI_ONLY_COMMANDS` is deliberately not an MCP tool,
# with the reason next to it. Anything else must name the tool it fronts.

#: `seat_cli` command -> the MCP tool (in `contract/tools.json`) it fronts.
CLI_COMMAND_TOOLS = {
    "state": "battlefield_state",
    "wait": "wait_for_priority",
    "cast": "cast",
    "suspend": "suspend",
    "plot": "plot",
    "foretell": "foretell",
    "cycle": "cycle",
    "play-land": "play_land",
    "activate": "activate",
    "equip": "equip",
    "level-up": "level_up",
    "reconfigure": "reconfigure",
    "attackers": "declare_attackers",
    "blockers": "declare_blockers",
    "decide": "decide",
    "pass": "pass",
    "pass-until": "pass_until",
    "mulligan": "mulligan",
    "opening-hand-action": "opening_hand_action",
    "opening-hand-done": "opening_hand_action",
    "log": "game_log",
    "report": "report_problem",
    "plan": "declare_plan",
    "stage-advice": "stage_advice",
}

#: `seat_cli` commands whose subcommands each front one MCP tool: `script run`
#: is `run_script`, `script list` is `list_scripts` and `script stage` is
#: `stage_script`, with the same argument names
#: (`test_script_subcommands_take_their_tools_parameters_by_name`).
CLI_SUBCOMMAND_TOOLS = {
    "script": {"run": "run_script", "list": "list_scripts", "stage": "stage_script"},
}

#: Commands that are CLI-only by design, each with why.
CLI_ONLY_COMMANDS = {
    "plays": (
        "`plays` works a guides checkout's play scripts on the pod: `check` lints "
        "them offline, `priority` edits one play's priority, `admit` commits and "
        "pushes them to mtg-decks main. It touches no seat and no game, and a "
        "stateless MCP server has no guides tree to read or push (design D8)"
    ),
    "guides": (
        "`guides propose` commits a matchup play_plan / notes edit with the "
        "regenerated guides and pushes it to mtg-decks main from the pod's guides "
        "checkout; no seat, no game, and nothing an MCP server holds"
    ),
}

#: Every subcommand of a CLI-only command, each allowlisted with why. A new
#: nested command is a decision too, not something that rides in under its
#: parent's entry.
CLI_ONLY_SUBCOMMANDS = {
    "plays": {
        "check": "lints decks/<slug>/plays/*.json offline",
        "priority": "changes one play's priority in the pod's guides clone",
        "admit": "commits, gates and pushes play-script changes to mtg-decks main",
        "no-change": (
            "records in the guides clone's results journal why one post-game candidate "
            "(a play or a hand-played line) needs no DSL change; no seat, no game"
        ),
    },
    "guides": {
        "propose": "pushes a deck-guide edit to mtg-decks main from the pod's guides clone",
    },
}


def test_every_cli_command_fronts_an_mcp_tool_or_is_explicitly_cli_only():
    sub = next(
        action for action in seat_cli.build_parser()._actions if getattr(action, "choices", None)
    )
    commands = set(sub.choices)
    mapped = set(CLI_COMMAND_TOOLS) | set(CLI_SUBCOMMAND_TOOLS)
    assert not set(CLI_COMMAND_TOOLS) & set(CLI_ONLY_COMMANDS)
    assert not set(CLI_SUBCOMMAND_TOOLS) & (set(CLI_COMMAND_TOOLS) | set(CLI_ONLY_COMMANDS))
    assert commands == mapped | set(CLI_ONLY_COMMANDS), (
        "a seat_cli command is neither mapped to its MCP tool nor allowlisted as CLI-only: "
        f"{sorted(commands ^ (mapped | set(CLI_ONLY_COMMANDS)))}"
    )
    from scripts.export_tools import OUTPUT as TOOLS

    tools = {tool["name"] for tool in json.loads(TOOLS.read_text())["tools"]}
    for command, tool in CLI_COMMAND_TOOLS.items():
        assert tool in tools, f"{command} fronts {tool}, which is not an MCP tool"
    for command, nested in CLI_SUBCOMMAND_TOOLS.items():
        nested_parser = next(
            action for action in subcommand(command)._actions if getattr(action, "choices", None)
        )
        assert set(nested_parser.choices) == set(nested), command
        for name, tool in nested.items():
            assert tool in tools, f"{command} {name} fronts {tool}, which is not an MCP tool"
    for command, why in CLI_ONLY_COMMANDS.items():
        assert why.strip()
        assert command.replace("-", "_") not in tools


def _handover_section(role: str) -> str:
    from magic_cards_model_api import ai_seat

    brief = ai_seat.load_brief(role)
    start = brief.index("\n## Play scripts and guides, for handover\n")
    end = brief.find("\n## ", start + 1)
    return brief[start : end if end != -1 else len(brief)]


@pytest.mark.parametrize("role", ["seat", "advisor"])
def test_the_briefs_document_the_mandatory_post_game_dsl_step(role):
    """D2a: DSL development after the game is mandatory, not optional - the
    handover lands a conditions or advice-only change, or records `plays
    no-change` once per candidate - and the briefs say so, together with the
    evidence files and retiring a play by deleting its file."""
    section = _handover_section(role)
    flat = " ".join(section.split())
    assert "optional" not in flat.lower()
    assert "mandatory" in flat.lower()
    for command in (
        "mcp-seat plays priority",
        "mcp-seat plays admit",
        "mcp-seat plays no-change",
        "mcp-seat guides propose",
    ):
        assert command in flat, (role, command)
    assert "--play" in flat and "--line" in flat and "--reason" in flat
    assert "plays/evidence/" in flat
    assert "git rm" in flat
    assert "dsl_change" in flat


def test_cli_only_commands_have_exactly_their_allowlisted_subcommands():
    assert set(CLI_ONLY_SUBCOMMANDS) == set(CLI_ONLY_COMMANDS)
    for command, allowed in CLI_ONLY_SUBCOMMANDS.items():
        nested = next(
            action for action in subcommand(command)._actions if getattr(action, "choices", None)
        )
        assert set(nested.choices) == set(allowed), command
        assert all(why.strip() for why in allowed.values())


# -- the play-script tools: one argument surface, one answer --------------------
#
# `script run`/`script list` and `run_script`/`list_scripts` both call
# `playscripts.script_tools`, so the answer cannot drift; what can drift is the
# argument surface around it. The CLI's `dest`s are the tools' parameter names
# (with `--reasoning` the CLI's global flag, as for every other command), their
# defaults are the tools' defaults, and `--input NAME=VALUE` is `inputs`.


def _tool_parameters(tool: str) -> dict[str, dict[str, Any]]:
    from scripts.export_tools import OUTPUT as TOOLS

    [entry] = [t for t in json.loads(TOOLS.read_text())["tools"] if t["name"] == tool]
    return {param["name"]: param for param in entry["parameters"]}


def _script_subcommand(name: str) -> Any:
    nested = next(
        action for action in subcommand("script")._actions if getattr(action, "choices", None)
    )
    return nested.choices[name]


def test_script_subcommands_take_their_tools_parameters_by_name():
    top_level = {action.dest for action in seat_cli.build_parser()._actions}
    assert "reasoning" in top_level, "--reasoning is the CLI's global flag"
    for name, tool in CLI_SUBCOMMAND_TOOLS["script"].items():
        parameters = _tool_parameters(tool)
        actions = {
            action.dest: action
            for action in _script_subcommand(name)._actions
            if action.dest != "help"
        }
        # `baton` is the MCP process's own access token (`baton.py`); the CLI
        # talks to the game API, which has no baton.
        expected = set(parameters) - {"player_uid", "reasoning", "baton"}
        assert set(actions) == expected, (name, sorted(set(actions) ^ expected))
        for dest, action in actions.items():
            parameter = parameters[dest]
            if parameter["has_default"]:
                assert action.default == parameter["default"], (name, dest, action.default)
            else:
                assert not action.option_strings, (name, dest, "a required parameter is positional")
    assert "reasoning" in _tool_parameters("run_script")
    assert "reasoning" not in _tool_parameters("list_scripts")


def test_the_input_flag_is_repeatable_and_names_inputs():
    checked = []
    for name, tool in CLI_SUBCOMMAND_TOOLS["script"].items():
        if "inputs" not in _tool_parameters(tool):
            continue
        checked.append(name)
        [flag] = [
            action
            for action in _script_subcommand(name)._actions
            if "--input" in getattr(action, "option_strings", ())
        ]
        assert flag.dest == "inputs"
        assert type(flag).__name__ == "_AppendAction", "--input is repeatable"
        assert flag.default is None and flag.metavar == "NAME=VALUE"
        said = " ".join((flag.help or "").split())
        assert "repeat" in said and "JSON" in said
    assert sorted(checked) == ["list", "run"], checked


def _two_scripts_on_one_board(client: TestClient, game_id: str) -> str:
    from conftest import force_turn_position
    from magic_cards_model.machines import Step
    from test_playscript_runtime import land_script, register

    force_turn_position(game_id, Step.PRECOMBAT_MAIN, "alice")
    uid = uid_for(game_id, "alice")
    frog = land_script("frog-only", opposing_archetypes=["dimir-frog"])
    register(client, game_id, uid, frog, land_script("drop", priority=40))
    add_card(client, game_id, "Island", "alice", "hand")
    return uid


def test_both_front_ends_list_the_same_scripts_for_the_same_inputs(
    client, game, monkeypatch, capsys
):
    wire(monkeypatch)
    uid = _two_scripts_on_one_board(client, game)
    server = build_server(Seats(api="http://testserver", game_id=game))

    for flags, arguments in (
        ([], {}),
        (["--opposing-archetype", "dimir-frog"], {"opposing_archetype": "dimir-frog"}),
        (
            ["--input", "opposing_archetype=dimir-frog"],
            {"inputs": {"opposing_archetype": "dimir-frog"}},
        ),
    ):
        assert seat_cli.run(["--uid", uid, "--game", game, "script", "list", *flags]) == 0
        from_cli = json.loads(capsys.readouterr().out)
        from_mcp = json.loads(
            asyncio.run(server.call_tool("list_scripts", {"player_uid": uid, **arguments}))
            .content[0]
            .text
        )
        assert from_cli == from_mcp, flags
    assert from_cli["runtime_conflict"] is True


def test_both_front_ends_run_a_script_to_the_same_exit(client, game, monkeypatch, capsys):
    wire(monkeypatch)
    uid = _two_scripts_on_one_board(client, game)
    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    try:
        uid2 = _two_scripts_on_one_board(client, second)
        rc = seat_cli.run(
            [
                "--uid",
                uid,
                "--game",
                game,
                "--reasoning",
                "the frog plan",
                "script",
                "run",
                "frog-only",
                "--input",
                "opposing_archetype=dimir-frog",
            ]
        )
        assert rc == 0
        from_cli = json.loads(capsys.readouterr().out)["script_exit"]
        server = build_server(Seats(api="http://testserver", game_id=second))
        raw = asyncio.run(
            server.call_tool(
                "run_script",
                {
                    "player_uid": uid2,
                    "script": "frog-only",
                    "reasoning": "the frog plan",
                    "inputs": {"opposing_archetype": "dimir-frog"},
                },
            )
        )
        from_mcp = json.loads(raw.content[0].text)["script_exit"]
        assert from_cli == from_mcp
        assert from_cli["exit"] == "chain_complete"
    finally:
        client.delete(f"/api/games/{second}")
