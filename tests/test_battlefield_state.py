"""`battlefield_state` replaces `my_state`: the same seat-filtered board, with
two axes of focus.

The old tool answered one question - "the whole board, as this seat may see
it" - and a seat that wanted only its own graveyard got the entire view
anyway. Two axes turn out to cover every focus the board actually offers,
because the view itself has exactly two:

1. **which sections** (`sections`), named with the view's own top-level keys,
   so there is no second vocabulary to learn: what a seat asks for is what it
   reads back.
2. **whose side** (`side`), resolved from the caller's own uid - `mine` is the
   seat whose uid was passed and `opponent` is everyone else, never a
   username the caller has to get right. A mirror match is the case that
   makes a username-shaped option dangerous, and it is why the answer echoes
   which usernames each side resolved to.

Zero arguments is the whole board, unchanged - narrowing is opt-in.

The hard constraint this file exists to pin is the last test: a focused
answer must never contain one byte the unfocused answer would not have
contained. Selection happens on the *already redacted* view the API hands
back (`visibility.redact_view` runs server-side, this process never sees the
unredacted state - see `mcp.py`'s module docstring), so narrowing can only
ever remove. The test proves it rather than trusting the argument.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from conftest import add_card
from fastapi.testclient import TestClient

from magic_cards_model_api import mcp as mcp_module
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seat, Seats
from magic_cards_model_api.store import store

#: The section vocabulary, pinned here rather than imported so this file
#: states the contract instead of mirroring whatever `mcp.py` happens to
#: hold. `test_the_section_vocabulary_is_the_views_own_keys` ties the two.
SECTIONS: dict[str, tuple[str, ...]] = {
    "battlefield": ("battlefield",),
    "hands": ("hands",),
    "graveyards": ("graveyards",),
    "exile": ("exile", "exile_pile"),
    "command": ("command",),
    "stack": ("stack",),
    "combat": ("combat",),
    "players": ("players",),
    "play_permissions": ("play_permissions",),
    "pregame": ("pregame",),
    "pending_decision": ("pending_decision",),
    "recent_log": ("recent_log",),
    "known_cards": ("known_cards",),
    "library_contents": ("library_contents",),
    "sideboard_contents": ("sideboard_contents",),
}

#: Sections served only when named: a seat's own library and sideboard as
#: unordered contents, fetched with `?include=contents` and never part of the
#: default whole board.
ON_REQUEST = ("library_contents", "sideboard_contents")

#: Keys no `sections` value ever removes - the turn, the step, who has
#: priority, whether the game is over, and which seat is asking. Dropping any
#: of these would save nothing and could have a seat act on a board without
#: knowing whose turn it is.
SPINE = ("game_id", "turn_number", "step", "phase", "active_player", "priority_holder", "you")


def wired(monkeypatch, game_id: str) -> Seats:
    """`Seats` pointed at the in-process app rather than the network."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id)


def text_of(server: Any, args: dict[str, Any]) -> str:
    """One `battlefield_state` call, as the seat reads it - text, because a
    refusal is a plain sentence and not JSON."""
    return asyncio.run(server.call_tool("battlefield_state", args)).content[0].text


def answer(server: Any, args: dict[str, Any]) -> dict[str, Any]:
    return json.loads(text_of(server, args))


def strings_in(value: Any) -> set[str]:
    """Every string anywhere inside a JSON answer - keys and values both."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found |= strings_in(item)
    elif isinstance(value, list):
        for item in value:
            found |= strings_in(item)
    elif isinstance(value, str):
        found.add(value)
    return found


@pytest.fixture()
def board(client: TestClient, game: str) -> dict[str, str]:
    """A game with something of each seat's in every zone that has a side,
    and the two seat uids."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Forest", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Elvish Mystic", "bob", "hand")
    add_card(client, game, "Abrade", "alice", "graveyard")
    add_card(client, game, "Mind Rot", "bob", "graveyard")
    add_card(client, game, "Island", "alice", "exile")
    add_card(client, game, "Ephemerate", "bob", "exile")
    return {name: uid for uid, name in store.seat_uids(game).items()}


def test_the_section_vocabulary_is_the_views_own_keys() -> None:
    """The names a seat passes are the keys it reads back - no translation
    table, no second vocabulary."""
    assert mcp_module.SECTION_KEYS == SECTIONS


def test_no_options_answers_the_whole_board(board, game, monkeypatch) -> None:
    """Zero arguments is exactly what `my_state` used to answer: everything
    this seat may see. Narrowing is opt-in, never the default."""
    server = build_server(wired(monkeypatch, game))

    whole = answer(server, {"player_uid": board["alice"]})

    for section, keys in SECTIONS.items():
        for key in keys:
            if section in ON_REQUEST:
                assert key not in whole, f"`{key}` is served only when asked for"
            else:
                assert key in whole, f"the unfocused board is missing `{key}`"
    for key in SPINE:
        assert key in whole
    assert whole["focus"]["sections"] == sorted(set(SECTIONS) - set(ON_REQUEST))
    assert whole["focus"]["omitted_sections"] == sorted(ON_REQUEST)
    assert whole["focus"]["side"] == "both"


def test_contents_are_fetched_only_when_named(board, game, monkeypatch) -> None:
    """The contents blocks cost a request flag, not a filter over a board
    that already carried them: the default answer never asks the api for
    them, and naming one does."""
    server = build_server(wired(monkeypatch, game))
    asked: list[bool] = []
    original = Seat.fetch_state

    def recording(self: Seat, *, include_contents: bool = False) -> dict[str, Any]:
        asked.append(include_contents)
        return original(self, include_contents=include_contents)

    monkeypatch.setattr(Seat, "fetch_state", recording)

    answer(server, {"player_uid": board["alice"]})
    answer(server, {"player_uid": board["alice"], "sections": "battlefield,known_cards"})
    focused = answer(server, {"player_uid": board["alice"], "sections": "library_contents"})
    both = answer(
        server, {"player_uid": board["alice"], "sections": "sideboard_contents,library_contents"}
    )

    assert asked == [False, False, True, True]
    assert list(focused["library_contents"]) == ["alice"]
    assert "sideboard_contents" not in focused
    assert list(both["sideboard_contents"]) == ["alice"]


def test_side_narrows_known_cards_by_whose_card_it_is(board, client, game, monkeypatch) -> None:
    """`known_cards` is keyed by the seat that knows, so `side` narrows the
    records inside that entry by the card's own owner, and the contents
    blocks by their owner's username."""
    from magic_cards_model.models import Zone

    add_card(client, game, "Shock", "bob", "hand")
    add_card(client, game, "Forest", "alice", "library")
    with store.locked(game) as live:
        live.reveal(next(card for card in live.player("bob").hand if card.name == "Shock"))
        live.look(live.player("alice").library[0], "alice")
        assert live.player("alice").library[0].zone is Zone.LIBRARY
    server = build_server(wired(monkeypatch, game))
    sections = "known_cards,library_contents"

    theirs = answer(
        server, {"player_uid": board["alice"], "sections": sections, "side": "opponent"}
    )
    mine = answer(server, {"player_uid": board["alice"], "sections": sections, "side": "mine"})
    both = answer(server, {"player_uid": board["alice"], "sections": sections})

    assert [card["name"] for card in both["known_cards"]["alice"]["hand"]] == ["Shock"]
    assert [r["card"]["name"] for r in both["known_cards"]["alice"]["library"]] == ["Forest"]
    assert [card["name"] for card in theirs["known_cards"]["alice"]["hand"]] == ["Shock"]
    assert theirs["known_cards"]["alice"]["library"] == []
    assert theirs["library_contents"] == {}
    assert mine["known_cards"]["alice"]["hand"] == []
    assert [r["card"]["name"] for r in mine["known_cards"]["alice"]["library"]] == ["Forest"]
    assert list(mine["library_contents"]) == ["alice"]


def test_each_section_can_be_asked_for_on_its_own(board, game, monkeypatch) -> None:
    server = build_server(wired(monkeypatch, game))

    for section, keys in SECTIONS.items():
        focused = answer(server, {"player_uid": board["alice"], "sections": section})
        for key in keys:
            assert key in focused, f"`sections={section}` dropped its own `{key}`"
        for other, other_keys in SECTIONS.items():
            if other == section:
                continue
            for key in other_keys:
                assert key not in focused, f"`sections={section}` still carries `{key}`"
        for key in SPINE:
            assert key in focused, f"`sections={section}` dropped the spine key `{key}`"
        assert focused["focus"]["sections"] == [section]
        assert section not in focused["focus"]["omitted_sections"]


def test_sections_combine(board, game, monkeypatch) -> None:
    server = build_server(wired(monkeypatch, game))

    focused = answer(
        server, {"player_uid": board["alice"], "sections": "battlefield, graveyards ,exile"}
    )

    assert focused["battlefield"] and focused["graveyards"]
    assert "exile" in focused and "exile_pile" in focused
    assert "hands" not in focused and "players" not in focused
    assert focused["focus"]["sections"] == ["battlefield", "exile", "graveyards"]
    assert "hands" in focused["focus"]["omitted_sections"]


def test_side_narrows_to_the_calling_seat(board, game, monkeypatch) -> None:
    """`mine` is whoever's uid was passed - never a username the caller has
    to name, which is the option shape a mirror match breaks."""
    server = build_server(wired(monkeypatch, game))

    mine = answer(server, {"player_uid": board["alice"], "side": "mine"})

    assert mine["focus"]["you"] == "alice"
    assert mine["focus"]["opponents"] == ["bob"]
    assert {card["controller"] for card in mine["battlefield"]} == {"alice"}
    assert list(mine["hands"]) == ["alice"]
    assert list(mine["graveyards"]) == ["alice"]
    assert [player["username"] for player in mine["players"]] == ["alice"]
    assert {card["controller"] for card in mine["exile"]} == {"alice"}


def test_side_narrows_to_the_other_seat(board, game, monkeypatch) -> None:
    server = build_server(wired(monkeypatch, game))

    theirs = answer(server, {"player_uid": board["alice"], "side": "opponent"})

    assert {card["controller"] for card in theirs["battlefield"]} == {"bob"}
    assert list(theirs["graveyards"]) == ["bob"]
    assert [player["username"] for player in theirs["players"]] == ["bob"]
    # Still redacted: the opponent's hand is listed and empty, never filled.
    assert theirs["hands"] == {"bob": []}


def test_the_two_axes_combine(board, game, monkeypatch) -> None:
    server = build_server(wired(monkeypatch, game))

    focused = answer(
        server, {"player_uid": board["alice"], "sections": "graveyards", "side": "opponent"}
    )

    assert list(focused["graveyards"]) == ["bob"]
    assert "battlefield" not in focused


def test_an_unknown_section_is_refused_by_name(board, game, monkeypatch) -> None:
    server = build_server(wired(monkeypatch, game))

    refusal = text_of(server, {"player_uid": board["alice"], "sections": "battlefield,librarys"})

    assert "librarys" in refusal
    assert "battlefield" in refusal and "graveyards" in refusal, (
        "the refusal must list what is real"
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(refusal)


def test_an_unknown_side_is_refused_by_name(board, game, monkeypatch) -> None:
    server = build_server(wired(monkeypatch, game))

    refusal = text_of(server, {"player_uid": board["alice"], "side": "alice"})

    assert "alice" in refusal
    assert "mine" in refusal and "opponent" in refusal and "both" in refusal
    with pytest.raises(json.JSONDecodeError):
        json.loads(refusal)


def test_narrowing_the_log_away_skips_its_http_call(board, game, monkeypatch) -> None:
    """`recent_log` is its own request to the API (`Seat.recent_log`), not a
    slice of the state the first request already returned - so a seat that
    narrows it away must not pay for it. Dropping it from the answer while
    still fetching it would be the pointless half of the saving.

    (`tests/test_seat_round_trips.py` pins the other direction: the log rides
    along with the board by default, so the two are one round trip.)
    """
    server = build_server(wired(monkeypatch, game))

    def never(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("the log was fetched for an answer that does not carry it")

    monkeypatch.setattr(Seat, "recent_log", never)

    focused = answer(server, {"player_uid": board["alice"], "sections": "battlefield"})

    assert "recent_log" not in focused


def test_my_state_is_gone_and_battlefield_state_stands_in_its_place(game, monkeypatch) -> None:
    server = build_server(wired(monkeypatch, game))

    names = set(server._tool_manager._tools)

    assert "battlefield_state" in names
    assert "my_state" not in names, "the replaced tool must not linger under its old name"


def test_a_focused_answer_can_never_show_more_than_the_unfocused_one(
    board, game, monkeypatch
) -> None:
    """The hidden-information constraint, stated as the only property that
    actually guarantees it: focusing removes and never adds. Every section on
    every side is compared against the unfocused answer - if any narrowing
    could reach a zone the seat is not entitled to, some string in it would
    have to appear here that the whole board does not contain.

    `focus` itself is excluded from both sides: it is this tool's own echo of
    what was asked for, not board content, and it necessarily says `mine`
    where the unfocused answer says `both`.
    """
    server = build_server(wired(monkeypatch, game))
    # The whole board with every section named, so the on-request contents
    # blocks are in the superset too; the default answer is itself checked
    # against that superset below like any other narrowing.
    whole = answer(server, {"player_uid": board["alice"], "sections": ",".join(SECTIONS)})
    whole.pop("focus")
    allowed = strings_in(whole)
    hidden = {"Elvish Mystic"}  # bob's hand, which alice may never see
    assert not hidden & allowed, "the unfocused answer already leaks the opponent's hand"

    for side in ("both", "mine", "opponent"):
        for section in [*SECTIONS, ""]:
            focused = answer(
                server, {"player_uid": board["alice"], "sections": section, "side": side}
            )
            focused.pop("focus")
            leaked = strings_in(focused) - allowed
            assert not leaked, f"`sections={section!r} side={side}` invented {sorted(leaked)}"
            assert "libraries" not in focused and "sideboards" not in focused
            for key in ("hands", "graveyards"):
                for cards in focused.get(key, {}).values():
                    assert not hidden & strings_in(cards)
