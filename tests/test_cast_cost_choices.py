"""Paying a cast's cost with objects, from every front end that casts.

The defect this file was written for, in a live kubernetes seat's own words:

    "Cannot cast Solitude via Evoke: the cast tool has no parameter to specify
    which card to exile as the alternative cost. Tried `targets`,
    `mana_sources`, and `exiling` (undocumented) each set to the Faithful
    Mending card id - all rejected with 'exile needs 1 card(s) chosen, 0
    given'."

The seat was right, and it was right about two layers at once. `CastRequest`
already carried `choosing` ("this is how Force of Negation gets pitched"), but
`engine_api._drive_cast` only ever handed it to `use_alternative_cost` - the
`req.keyword` branch next to it called `casting.use_keyword(req.keyword)` with
no `choosing` at all, so announcing evoke *by keyword* (which is what
`legal-actions` and a hand card's `cast_options` both tell a seat to do) could
never name the card it pitches. And `mcp.py`'s `cast` tool exposed neither
`choosing` nor any of the other cost fields `CastRequest` has had all along.

The four printed additional costs (CR 118.8a, announced at CR 601.2b and
folded into the total cost at CR 601.2f) had never been reachable over HTTP at
all: `Casting.sacrificing`/`.discarding`/`.exiling`/`.tapping` existed in the
engine with nothing on the request able to fill them, so Fling was announced
for {1}{R}, sacrificed nothing, and refused to be paid.

Every card here is a real printing out of the engine's own packaged corpus and
every board is played through the real API. No stand-in engine, and no
hand-written card data.
"""

from __future__ import annotations

import json
from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient
from test_mcp_input_required import call_tool, wired

from magic_cards_model_api.mcp import build_server


def text_of(result: Any) -> str:
    """The text a tool call actually put on the wire, whichever of the two
    result shapes `mcp._tool_result` chose (`InputRequiredResult` carries it
    as `request_state`, an ordinary result as its first text content)."""
    if isinstance(result, str):
        return result
    state_text = getattr(result, "request_state", None)
    if state_text:
        return state_text
    content = getattr(result, "content", None)
    if content:
        return content[0].text
    raise AssertionError(f"no text on {result!r}")


def answer(result: Any) -> dict:
    return json.loads(text_of(result))


def hand_id(client: TestClient, game: str, name: str, player: str = "alice") -> str:
    return find(state(client, game)["hands"][player], name)["id"]


def battlefield_id(client: TestClient, game: str, name: str) -> str:
    return find(state(client, game)["battlefield"], name)["id"]


def lands(client: TestClient, game: str, name: str, count: int, player: str = "alice") -> None:
    for _ in range(count):
        add_card(client, game, name, player, "battlefield")


def cast_via_http(client: TestClient, game: str, **body: Any) -> dict:
    """One cast over the raw endpoint, offering every untapped land alice
    controls - the endpoint takes explicit `mana_sources`, where the MCP tool
    and the CLI both offer the whole untapped board on the caller's behalf."""
    body.setdefault(
        "mana_sources",
        [
            card["id"]
            for card in state(client, game)["battlefield"]
            if card["controller"] == "alice" and "Land" in card.get("type_line", "")
        ],
    )
    response = client.post(f"/api/games/{game}/actions/cast", json={"player": "alice", **body})
    assert response.status_code == 200, response.text
    return response.json()


# -- the reported defect: evoke's own pitch cost, by keyword -----------------


def test_the_http_cast_endpoint_pitches_a_card_to_evoke_announced_by_keyword(client, game):
    """CR 702.74a's evoke, announced the way `legal-actions` and a hand card's
    `cast_options` both spell it - `keyword: "Evoke"` - with the white card it
    exiles named in `choosing`. The `alternative_cost` spelling of the same
    announcement already worked; this one dropped `choosing` on the floor."""
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")
    pitched = hand_id(client, game, "Savannah Lions")

    outcome = cast_via_http(
        client,
        game,
        card="Solitude",
        keyword="Evoke",
        choosing=[pitched],
        reasoning="evoking to remove a threat",
    )

    assert outcome["allowed"] is True, outcome
    board = state(client, game)
    assert [card["name"] for card in board["stack"]] == ["Solitude"], board["stack"]
    assert [card["name"] for card in board["exile"]] == ["Savannah Lions"], board["exile"]


def test_the_cast_tool_pitches_a_card_to_evoke(client, game, monkeypatch):
    """The seat's own path, end to end: `mcp__mtg__cast` with `keyword` and
    `choosing`. This is the call the stuck pod made four times and could not
    make work, because `choosing` was not a parameter of the tool."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")
    pitched = hand_id(client, game, "Savannah Lions")

    result = call_tool(
        server,
        "cast",
        {
            "player_uid": uids["alice"],
            "card": "Solitude",
            "keyword": "Evoke",
            "choosing": pitched,
            "hold_priority": True,
            "reasoning": "evoking to remove a threat",
        },
    )

    assert answer(result)["outcome"]["allowed"] is True, answer(result)
    board = state(client, game)
    assert [card["name"] for card in board["stack"]] == ["Solitude"], board["stack"]
    assert [card["name"] for card in board["exile"]] == ["Savannah Lions"], board["exile"]


def test_a_pitch_the_caller_never_named_is_refused_in_the_engines_own_words(
    client, game, monkeypatch
):
    """Fail closed and loudly: leaving `choosing` out is refused with the
    engine's own count, never a silent no-op, and the refusal reaches the
    seat verbatim as the answer's very first key."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")

    result = call_tool(
        server,
        "cast",
        {
            "player_uid": uids["alice"],
            "card": "Solitude",
            "keyword": "Evoke",
            "reasoning": "evoking with nothing named",
        },
    )

    payload = answer(result)
    assert payload["outcome"]["allowed"] is False, payload
    assert payload["outcome"]["reason"] == "exile needs 1 card(s) chosen, 0 given", payload
    assert next(iter(payload)) == "outcome", list(payload)


def test_the_cast_tools_own_contract_names_every_cost_choice_it_accepts():
    """The seat guessed `targets`, `mana_sources` and `exiling` because the
    tool's own schema named none of the parameters that pay a cost with
    objects. `contract/tools.json` is that schema as this repo publishes it
    (`scripts/export_tools.py`), and it is what a seat's client reads."""
    from pathlib import Path

    contract = json.loads(
        (Path(__file__).resolve().parents[1] / "contract" / "tools.json").read_text()
    )
    cast_tool = next(tool for tool in contract["tools"] if tool["name"] == "cast")
    parameters = {parameter["name"] for parameter in cast_tool["parameters"]}
    assert {
        "choosing",
        "alternative_cost",
        "sacrificing",
        "discarding",
        "exiling",
        "tapping",
        "delve",
        "convoke",
    } <= parameters, sorted(parameters)

    doc = _cast_docstring()
    assert "`choosing`" in doc, doc
    assert "Exile a white card from your hand" in doc, doc


def _cast_docstring() -> str:
    """The `cast` tool's own docstring, read out of `mcp.py`'s source the same
    statically-walked way `scripts/export_tools.py` reads it - the text an MCP
    client is handed as the tool's description."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "magic_cards_model_api" / "mcp.py"
    ).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "cast":
            return ast.get_docstring(node) or ""
    raise AssertionError("no `cast` tool in mcp.py")


# -- finding the cost, not only paying it ------------------------------------


def test_the_affordance_names_which_cards_can_pay_an_alternative_cost(client, game):
    """`requires: 1` says how many and nothing about which, and "Exile a white
    card from your hand" is prose no client can filter a hand on. The
    affordance lists the legal answers themselves, the same way `object_costs`
    has always listed convoke's and delve's."""
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")  # white: legal
    add_card(client, game, "Preordain", "alice", "hand")  # blue: not
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")  # not in hand
    solitude = hand_id(client, game, "Solitude")

    affordance = client.get(
        f"/api/games/{game}/affordances/{solitude}", params={"player": "alice"}
    ).json()
    evoke = next(alt for alt in affordance["alternative_costs"] if alt["keyword"] == "Evoke")

    assert evoke["requires"] == 1, evoke
    assert [candidate["name"] for candidate in evoke["candidates"]] == ["Savannah Lions"], evoke
    assert evoke["candidates"][0]["pays"] == "exile", evoke
    assert evoke["candidates"][0]["id"] == hand_id(client, game, "Savannah Lions")


def test_a_mana_only_alternative_cost_offers_no_candidates(client, game):
    """Wistfulness's evoke is {G/U}{G/U} and names no objects at all, so there
    is nothing to choose and the list is empty - not the whole hand."""
    add_card(client, game, "Wistfulness", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    wistfulness = hand_id(client, game, "Wistfulness")

    affordance = client.get(
        f"/api/games/{game}/affordances/{wistfulness}", params={"player": "alice"}
    ).json()
    evoke = next(alt for alt in affordance["alternative_costs"] if alt["keyword"] == "Evoke")

    assert evoke["requires"] == 0, evoke
    assert evoke["candidates"] == [], evoke


# -- CR 118.8a's printed additional costs, one per object kind ---------------


def test_fling_sacrifices_the_creature_the_caller_named(client, game):
    """ "As an additional cost to cast this spell, sacrifice a creature."
    (CR 118.8a). Nothing on `CastRequest` could answer it before, so Fling was
    announced for {1}{R} and refused to be paid."""
    lands(client, game, "Mountain", 2)
    add_card(client, game, "Fling", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Elder Gargaroth", "bob", "battlefield")
    bears = battlefield_id(client, game, "Grizzly Bears")

    outcome = cast_via_http(
        client,
        game,
        card="Fling",
        sacrificing=[bears],
        targets=["bob"],
        reasoning="flinging the bears",
    )

    assert outcome["allowed"] is True, outcome
    board = state(client, game)
    assert "Grizzly Bears" not in [card["name"] for card in board["battlefield"]]
    assert "Grizzly Bears" in [card["name"] for card in board["graveyards"]["alice"]]


def test_cathartic_reunion_discards_the_two_cards_the_caller_named(client, game):
    """ "discard two cards" - a count greater than one, answered with as many
    ids as the cost asks for."""
    lands(client, game, "Mountain", 2)
    add_card(client, game, "Cathartic Reunion", "alice", "hand")
    add_card(client, game, "Shock", "alice", "hand")
    add_card(client, game, "Abrade", "alice", "hand")
    for _ in range(5):
        add_card(client, game, "Mountain", "alice", "library")

    outcome = cast_via_http(
        client,
        game,
        card="Cathartic Reunion",
        discarding=[hand_id(client, game, "Shock"), hand_id(client, game, "Abrade")],
        reasoning="digging",
    )

    assert outcome["allowed"] is True, outcome
    graveyard = [card["name"] for card in state(client, game)["graveyards"]["alice"]]
    assert sorted(graveyard) == ["Abrade", "Shock"], graveyard


def test_swallow_whole_taps_the_creature_the_caller_named(client, game):
    """ "tap an untapped creature you control" - the `tapping` kind, which
    nothing but the engine's own `Casting.tapping` could reach."""
    lands(client, game, "Plains", 1)
    add_card(client, game, "Swallow Whole", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield", tapped=True)
    lion = battlefield_id(client, game, "Savannah Lions")

    outcome = cast_via_http(
        client,
        game,
        card="Swallow Whole",
        tapping=[lion],
        targets=[battlefield_id(client, game, "Grizzly Bears")],
        reasoning="exiling the tapped bears",
    )

    assert outcome["allowed"] is True, outcome
    assert find(state(client, game)["battlefield"], "Savannah Lions")["tapped"] is True


def test_abhorrent_oculus_exiles_the_six_graveyard_cards_the_caller_named(client, game):
    """ "exile six cards from your graveyard" - the `exiling` kind."""
    lands(client, game, "Island", 3)
    add_card(client, game, "Abhorrent Oculus", "alice", "hand")
    fodder = ["Shock", "Abrade", "Preordain", "Ephemerate", "Mind Rot", "Angelsong"]
    for name in fodder:
        add_card(client, game, name, "alice", "graveyard")
    graveyard = state(client, game)["graveyards"]["alice"]
    chosen = [find(graveyard, name)["id"] for name in fodder]

    outcome = cast_via_http(
        client,
        game,
        card="Abhorrent Oculus",
        exiling=chosen,
        reasoning="paying the delirium tax",
    )

    assert outcome["allowed"] is True, outcome
    assert state(client, game)["graveyards"]["alice"] == []


def test_the_cast_tool_carries_every_printed_additional_cost_kind(client, game, monkeypatch):
    """The same four kinds through the seat's own tool, not only over HTTP -
    Fling stands in for all of them, since one shared `_parse_ids` hop carries
    each."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    lands(client, game, "Mountain", 2)
    add_card(client, game, "Fling", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    bears = battlefield_id(client, game, "Grizzly Bears")

    result = call_tool(
        server,
        "cast",
        {
            "player_uid": uids["alice"],
            "card": "Fling",
            "sacrificing": bears,
            "targets": "bob",
            "hold_priority": True,
            "reasoning": "flinging the bears",
        },
    )

    assert answer(result)["outcome"]["allowed"] is True, answer(result)


# -- the cost fields CastRequest already had, now reachable from the tool ----


def test_the_cast_tool_offers_graveyard_cards_to_delve(client, game, monkeypatch):
    """`CastRequest.delve` has existed since the object-cost work; the tool
    had no way to name the cards (CR 702.66a)."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    lands(client, game, "Island", 2)
    add_card(client, game, "Treasure Cruise", "alice", "hand")
    fodder = ["Shock", "Abrade", "Preordain", "Ephemerate", "Mind Rot", "Angelsong"]
    for name in fodder:
        add_card(client, game, name, "alice", "graveyard")
    for _ in range(5):
        add_card(client, game, "Island", "alice", "library")
    graveyard = state(client, game)["graveyards"]["alice"]
    exiled = [find(graveyard, name)["id"] for name in fodder]

    result = call_tool(
        server,
        "cast",
        {
            "player_uid": uids["alice"],
            "card": "Treasure Cruise",
            "delve": ",".join(exiled),
            "hold_priority": True,
            "reasoning": "delving for the draw",
        },
    )

    assert answer(result)["outcome"]["allowed"] is True, answer(result)


def test_the_cast_tool_taps_creatures_for_convoke(client, game, monkeypatch):
    """`CastRequest.convoke` likewise (CR 702.51a) - the UI has offered it
    since `ui/actions.js` gained `toggleConvoke`; the seat could not."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    add_card(client, game, "Conclave Tribunal", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Savannah Lions", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    crew = [
        card["id"]
        for card in state(client, game)["battlefield"]
        if card["name"] == "Savannah Lions"
    ]

    # No `targets`: Conclave Tribunal's own targeting lives on its
    # enter-the-battlefield trigger, not on the spell (CR 601.2c), exactly as
    # Solitude's does - the mistake the stuck seat made twice.
    #
    # Three lions against `{3}`, the fourth named `:W` for the `{W}`: CR
    # 702.51a lets a creature pay "{1} or one mana of that creature's color",
    # and offering all four with no colour is refused, correctly, with "{W}
    # has no {1} left to pay".
    offered = [*crew[:3], f"{crew[3]}:W"]
    result = call_tool(
        server,
        "cast",
        {
            "player_uid": uids["alice"],
            "card": "Conclave Tribunal",
            "convoke": ",".join(offered),
            "hold_priority": True,
            "reasoning": "convoking the tribunal out",
        },
    )

    assert answer(result)["outcome"]["allowed"] is True, answer(result)


def test_the_cast_tool_announces_an_alternative_cost_by_index(client, game, monkeypatch):
    """`CastRequest.alternative_cost` is the other half of `keyword`: the
    index (or name) out of `GET .../affordances/{card_id}`'s
    `alternative_costs`, which is how a *printed* pitch offer with no keyword
    of its own gets announced at all."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    add_card(client, game, "Subtlety", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    subtlety = hand_id(client, game, "Subtlety")
    affordance = client.get(
        f"/api/games/{game}/affordances/{subtlety}", params={"player": "alice"}
    ).json()
    evoke = next(alt for alt in affordance["alternative_costs"] if alt["keyword"] == "Evoke")

    result = call_tool(
        server,
        "cast",
        {
            "player_uid": uids["alice"],
            "card": "Subtlety",
            "alternative_cost": str(evoke["index"]),
            "choosing": hand_id(client, game, "Preordain"),
            "hold_priority": True,
            "reasoning": "evoking by index",
        },
    )

    assert answer(result)["outcome"]["allowed"] is True, answer(result)
    assert [card["name"] for card in state(client, game)["exile"]] == ["Preordain"]


# -- the two front ends agree (test_front_end_parity.py's own thesis) --------


def test_the_seat_cli_can_name_every_cost_choice_the_mcp_tool_can(
    client, game, monkeypatch, capsys
):
    """`seat_cli.py` and `mcp.py` are two front ends onto one seat; a cost a
    seat can pay through one and not the other is the exact class of bug
    `test_front_end_parity.py` exists to catch."""
    import httpx
    from fastapi.testclient import TestClient as _TestClient
    from test_decisions import seats_for

    from magic_cards_model_api import seat_cli
    from magic_cards_model_api.main import app

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _TestClient(app))
    uids = seats_for(client, game)
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")
    pitched = hand_id(client, game, "Savannah Lions")

    seat_cli.run(
        [
            "--api",
            "http://testserver",
            "--game",
            game,
            "--uid",
            uids["alice"],
            "--reasoning",
            "evoking to remove a threat",
            "cast",
            "Solitude",
            "--keyword",
            "Evoke",
            "--choosing",
            pitched,
            "--hold",
        ]
    )

    capsys.readouterr()
    assert [card["name"] for card in state(client, game)["exile"]] == ["Savannah Lions"]
