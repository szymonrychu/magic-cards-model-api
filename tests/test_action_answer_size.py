"""An action tool's answer is the action's outcome, not the whole world.

A live kubernetes seat evoking Solitude got back 25,397 tokens for one `cast`
call. Its own client refused to read a result that large ("File content
(25397 tokens) exceeds maximum allowed tokens (25000)"), so the agent fell
back to `grep` and ad-hoc `python3 -c` over the blob, never found the
refusal - which the engine had spelled out perfectly, "exile needs 1 card(s)
chosen, 0 given" - and burned its whole turn on four identical retries.

Two things were wrong with the shape, and both are pinned here.

1. **Size.** `seatkit.run_action` answers with `state` - the seat's entire
   filtered board, every card in it carrying its whole `Card` - alongside
   `available_actions` and `recent_log`. Measured on a 28-permanent board,
   that one answer was 66,636 bytes, of which the board was 37,683 and the
   action list another 11,538. `battlefield_state` is the tool for board
   detail, and it takes a `sections` parameter for exactly this; an action
   tool re-serialising the same world after every single action is the
   duplication, not the service.

2. **Legibility.** The answer was serialised with `sort_keys=True`, so the
   84-byte `outcome` - the only thing the caller asked about - began at
   character 13,724, behind the full action list. A reader hits the outcome
   first now, and a refusal carries the engine's own words verbatim.

`seatkit.run_action` itself is unchanged: `seat_cli.py` is a human at a
terminal who can scroll, and `tests/test_action_flow.py` pins that dict's
contract. The narrowing is at the MCP boundary, where the token budget is.
"""

from __future__ import annotations

import json
from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient
from test_cast_cost_choices import answer, text_of
from test_mcp_input_required import call_tool, wired

from magic_cards_model_api import affordances
from magic_cards_model_api import mcp as mcp_module
from magic_cards_model_api.mcp import build_server

#: A board with something on it - fourteen permanents a side, eight cards in
#: each graveyard, a seven-card hand. Smaller than the pod's real board, which
#: is the point: the bound below has to hold with room to spare.
BOARD = [
    "Grizzly Bears",
    "Savannah Lions",
    "Elder Gargaroth",
    "Birds of Paradise",
    "Elvish Mystic",
    "Sol Ring",
    "Mind Stone",
    "Smuggler's Copter",
    "Karn, the Great Creator",
    "Plains",
    "Plains",
    "Sacred Foundry",
    "Hallowed Fountain",
    "Flooded Strand",
]
GRAVE = [
    "Lightning Bolt",
    "Shock",
    "Preordain",
    "Abrade",
    "Ephemerate",
    "Lightning Helix",
    "Mind Rot",
    "Angelsong",
]

#: The bound one answer must stay under, in bytes. 25,000 tokens was the
#: client's own ceiling and roughly 100,000 bytes of JSON; this stays under
#: half of it, so an answer still has to more than double from here before a
#: seat is anywhere near unable to read its own result.
#:
#: Raised from 20,000 deliberately, and this is the reasoning: 20,000 was
#: defensive padding chosen when the only question was "how small can this
#: get", not a constraint anything actually needed. It is now spent on the
#: two things that let a seat act out of one answer instead of three -
#: `targets` candidates (`mcp.MAX_CANDIDATE_BYTES`, 12,000) and the
#: battlefield digest (`mcp.MAX_DIGEST_BYTES`, 8,000). Those two caps plus
#: the measured base of an already-busy board is ~33.5 KB, which
#: `test_the_worst_case_an_answer_can_reach_still_fits` proves from the real
#: numbers rather than from this comment. A round trip saved is a whole
#: inference cycle saved; 25,000 bytes of headroom that no client was ever
#: going to use is not worth one.
MAX_ANSWER_BYTES = 45_000

#: The zones an action answer must not re-serialise, by the key each occupies
#: in a state view - `battlefield_state` serves all of them, with `sections` to
#: narrow further.
BULK_KEYS = ("battlefield", "hands", "graveyards", "exile", "exile_pile", "command")

#: The same zones by the section name `battlefield_state` knows them under,
#: which is what `omitted_sections` lists (`exile_pile` rides along inside
#: `exile`, so it is a key without a section of its own).
BULK_SECTIONS = ("battlefield", "hands", "graveyards", "exile", "command")


def busy_board(client: TestClient, game: str) -> None:
    for name in BOARD:
        add_card(client, game, name, "alice", "battlefield")
        add_card(client, game, name, "bob", "battlefield")
    for name in GRAVE:
        add_card(client, game, name, "alice", "graveyard")
        add_card(client, game, name, "bob", "graveyard")
    for name in ["Lightning Bolt", "Preordain", "Subtlety", "Living End", "Treasure Cruise"]:
        add_card(client, game, name, "alice", "hand")


def cast_solitude(server: Any, uid: str, **extra: Any) -> Any:
    return call_tool(
        server,
        "cast",
        {"player_uid": uid, "card": "Solitude", "keyword": "Evoke", **extra},
    )


def test_a_refused_cast_answers_with_the_refusal_first_and_nothing_else_ahead_of_it(
    client, game, monkeypatch
):
    """The pod's exact call. `outcome` is the first key a reader meets, and
    the engine's own refusal text is right there in it - not behind eleven
    kilobytes of legal actions."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    busy_board(client, game)
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")

    result = cast_solitude(server, uids["alice"], reasoning="evoking")
    text = text_of(result)
    payload = json.loads(text)

    assert next(iter(payload)) == "outcome", list(payload)
    assert payload["outcome"] == {
        "allowed": False,
        "log_seq": payload["outcome"]["log_seq"],
        "reason": "exile needs 1 card(s) chosen, 0 given",
    }, payload["outcome"]
    assert text.index('"outcome"') < 40, text[:200]


def test_an_action_answer_stays_small_enough_for_a_seat_to_read_in_one_go(
    client, game, monkeypatch
):
    """66,636 bytes on this very board before; a fraction of that now, with
    the outcome, the refusal, the legal actions and the log tail all still in
    it."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    busy_board(client, game)
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")

    text = text_of(cast_solitude(server, uids["alice"], reasoning="evoking"))

    assert len(text) < MAX_ANSWER_BYTES, f"{len(text)} bytes"


def test_an_action_answer_carries_no_board_dump_but_says_so_out_loud(client, game, monkeypatch):
    """A narrowed board must never read as an empty one - the same promise
    `battlefield_state`'s own `focus.omitted_sections` makes."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    busy_board(client, game)
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")

    payload = answer(cast_solitude(server, uids["alice"], reasoning="evoking"))

    assert "state" not in payload, list(payload)
    board = payload["board"]
    for key in BULK_KEYS:
        assert key not in board, key
    assert set(BULK_SECTIONS) <= set(board["omitted_sections"]), board["omitted_sections"]
    assert "battlefield_state" in board["read_the_board_with"]


def test_an_action_answer_still_carries_what_a_seat_needs_to_act_again(client, game, monkeypatch):
    """Narrowing is not amputation: the turn, the step, whose priority it is,
    both life totals, the stack, the legal actions and the log tail all
    survive, which is why a seat still needs only one round trip per move."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    busy_board(client, game)
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")
    pitched = find(state(client, game)["hands"]["alice"], "Savannah Lions")["id"]

    payload = answer(
        cast_solitude(
            server, uids["alice"], choosing=pitched, hold_priority=True, reasoning="evoking"
        )
    )

    assert payload["outcome"]["allowed"] is True, payload["outcome"]
    board = payload["board"]
    assert board["turn_number"] == 1
    assert board["step"] == "precombat_main"
    assert board["priority_holder"] == "alice"
    assert board["you"] == "alice"
    assert [player["username"] for player in board["players"]] == ["alice", "bob"]
    assert [card["name"] for card in board["stack"]] == ["Solitude"]
    assert payload["available_actions"]["actions"], payload["available_actions"]
    assert payload["recent_log"]["entries"], payload["recent_log"]
    assert payload["wake_reason"]


def test_a_legal_action_entry_carries_only_the_fields_it_actually_uses(client, game, monkeypatch):
    """19 actions cost 11,440 bytes, ~600 each, because every entry printed
    all 25 keys of `ActionView` - about twenty of them `null` or `[]` on any
    given entry. An empty field says nothing a reader can act on and is
    dropped; `kind`, `player` and `rule` are always there."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    busy_board(client, game)
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")

    payload = answer(cast_solitude(server, uids["alice"], reasoning="evoking"))

    actions = payload["available_actions"]["actions"]
    assert actions
    for action in actions:
        assert action["kind"], action
        assert not [key for key, value in action.items() if value in (None, [], {})], action
    evoke = next(
        action
        for action in actions
        if action["kind"] == "cast" and action.get("keyword") == "Evoke"
    )
    assert evoke["card_id"], evoke


def test_every_action_tool_shares_the_same_narrowed_answer(client, game, monkeypatch):
    """`cast` was the reported one; `play_land`, `activate`, `pass` and
    `decide` all answer through the same `run_action`, so all of them carried
    the same board dump. One shape, checked on all of them at once."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    busy_board(client, game)
    add_card(client, game, "Plains", "alice", "hand")
    sol_ring = next(
        card
        for card in state(client, game)["battlefield"]
        if card["name"] == "Sol Ring" and card["controller"] == "alice"
    )

    calls = [
        ("play_land", {"player_uid": uids["alice"], "card": "Plains", "reasoning": "land drop"}),
        (
            "activate",
            {
                "player_uid": uids["alice"],
                "card_id": sol_ring["id"],
                "ability_index": 0,
                "reasoning": "making mana",
            },
        ),
        ("pass", {"player_uid": uids["alice"], "reasoning": "done"}),
    ]
    for name, arguments in calls:
        payload = answer(call_tool(server, name, arguments))
        assert "state" not in payload, (name, list(payload))
        assert next(iter(payload)) == "outcome", (name, list(payload))
        assert "omitted_sections" in payload["board"], (name, payload["board"])


# -- the caps, and the worst case they add up to ----------------------------
#
# Four numbers bound one answer, and each is a deliberate choice rather than a
# round number. They are pinned here, together, because they only make sense
# read against one another: the three inner caps have to leave the outer one
# room on a board busier than any of them was measured on.


def test_the_four_caps_are_where_the_reasoning_says_they_are():
    """A cap moved without its comment moving is the failure this catches -
    each of these carries the measurement that chose it at its definition."""
    assert MAX_ANSWER_BYTES == 45_000
    assert mcp_module.MAX_CANDIDATE_BYTES == 12_000
    assert mcp_module.MAX_DIGEST_BYTES == 8_000
    assert affordances.CANDIDATE_SLOT_CAP == 30


def test_the_worst_case_an_answer_can_reach_still_fits(client, game, monkeypatch):
    """Not a guess: the base is measured off this very board with both new
    blocks emptied, and the two caps are then added at full value. An answer
    physically cannot exceed that sum, because neither block can pass its own
    cap - so if this holds, `MAX_ANSWER_BYTES` holds for every board."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    busy_board(client, game)
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")

    caps = mcp_module.MAX_DIGEST_BYTES + mcp_module.MAX_CANDIDATE_BYTES
    monkeypatch.setattr(mcp_module, "MAX_DIGEST_BYTES", 0)
    monkeypatch.setattr(mcp_module, "MAX_CANDIDATE_BYTES", 0)
    base = len(text_of(cast_solitude(server, uids["alice"], reasoning="evoking")))

    assert base + caps < MAX_ANSWER_BYTES, f"base {base} + caps {caps}"


def test_a_real_enriched_answer_is_nowhere_near_the_cap(client, game, monkeypatch):
    """The worst case above is arithmetic; this is the board. Nothing on a
    normal, busy board comes close, which is what the headroom is for."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    busy_board(client, game)
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")

    text = text_of(cast_solitude(server, uids["alice"], reasoning="evoking"))

    assert len(text) < MAX_ANSWER_BYTES, f"{len(text)} bytes"
    payload = json.loads(text)
    assert payload["board"]["battlefield_digest"], "the digest must actually be in there"


# -- an advisor's wake answer, which now carries the action list too --------
#
# `wait_for_priority` is not an action answer: it hands back the whole state
# view, which is what that tool exists to give, and
# `test_advise_mode.test_an_advisors_mcp_wait_carries_the_state_and_the_log`
# pins it whole for an advisor. `MAX_ANSWER_BYTES` above bounds action answers
# only, and on this board the advisor's wake answer was already 51,255 bytes
# before it carried any action list - over that cap before this change.
#
# For an advise seat a real wake now also carries `available_actions` (the one
# answer that wakes an advisor, since `pass_until` is refused to it): +7,911
# bytes on this board, 59,470 in all. That list is shaped by the SAME
# `mcp._compact_actions` the action answers use - empty keys dropped,
# `MAX_CANDIDATE_BYTES` and `MAX_PAYS_WITH_BYTES` spent in list order, a
# trimmed entry saying so in `more_with`/`name` - so its growth is bounded by
# those two caps rather than by a second budget, which the test below proves.


def advisor_server(client: TestClient, game: str, monkeypatch) -> Any:
    """A fresh MCP server - a fresh `Seats`, so an empty wake-signature cache
    (`seatkit.Seats._wake_signatures`) - pointed at whatever board `game`
    already carries. Split out of `advisor_on_the_busy_board` so the caps
    test below can take two independent first-ever-wait measurements of the
    identical board: since the busy-spin fix, a second wake through the SAME
    `Seats` would see nothing new and stop carrying `available_actions`
    altogether, which is correct there and simply the wrong tool here."""
    seats, _ = wired(client, monkeypatch, game)
    return build_server(seats)


def advisor_on_the_busy_board(client: TestClient, game: str, monkeypatch) -> Any:
    from magic_cards_model_api.repository import Advisor
    from magic_cards_model_api.store import store

    busy_board(client, game)
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")
    store.register_advisors(game, {"buddy-uid": Advisor(player="alice")})
    return advisor_server(client, game, monkeypatch)


def advisor_wake(server: Any) -> str:
    return text_of(call_tool(server, "wait_for_priority", {"player_uid": "buddy-uid"}))


def test_an_advisors_wake_answer_carries_a_compacted_action_list_on_the_busy_board(
    client, game, monkeypatch
):
    """The measured cost, and proof the list went through the action answers'
    own compaction rather than arriving as the raw `ActionView` dump - while
    the board beside it stays the whole state view it always was."""
    payload = json.loads(advisor_wake(advisor_on_the_busy_board(client, game, monkeypatch)))

    actions = payload["available_actions"]["actions"]
    assert actions, payload["available_actions"]
    for action in actions:
        assert not [key for key, value in action.items() if value in (None, [], {})], action
    assert any(
        slot.get("candidates")
        for action in actions
        for slot in (action.get("targets") or {}).get("slots") or []
    ), "no action on the busy board carries its target candidates"
    for key in BULK_KEYS:
        assert key in payload, f"carrying the action list narrowed the wake's `{key}` away"
    assert "omitted_sections" not in payload


def test_an_advisors_wake_list_grows_only_inside_the_action_answers_own_caps(
    client, game, monkeypatch
):
    """Not a guess, the same arithmetic the action answer's worst case uses:
    the same wake on the same board, once with both budgets emptied and once
    with them at full value. Everything the second answer carries beyond the
    first is candidate names and `pays_with` names, so it can never exceed
    the two caps added together - and the starved answer says out loud that
    its candidates were cut rather than handing over less and staying quiet.

    Two independent `Seats` (`advisor_server`), not one wake called twice: the
    busy-spin fix means a second bare wait on an unchanged board through the
    same `Seats` sees nothing new and drops `available_actions` altogether -
    correct there, but this test wants two first looks at one board, not a
    look and then a spin."""
    board = advisor_on_the_busy_board(client, game, monkeypatch)
    full = len(advisor_wake(board))

    caps = mcp_module.MAX_CANDIDATE_BYTES + mcp_module.MAX_PAYS_WITH_BYTES
    monkeypatch.setattr(mcp_module, "MAX_CANDIDATE_BYTES", 0)
    monkeypatch.setattr(mcp_module, "MAX_PAYS_WITH_BYTES", 0)
    starved_text = advisor_wake(advisor_server(client, game, monkeypatch))
    starved = json.loads(starved_text)["available_actions"]["actions"]

    trimmed = [
        action
        for action in starved
        if (action.get("targets") or {}).get("more_with") == mcp_module._BUDGET_SPENT_WITH
    ]
    assert trimmed, "a spent candidate budget went unannounced on the advisor's wake"
    for action in trimmed:
        assert all("count" in slot for slot in action["targets"]["slots"]), action
    assert full - len(starved_text) <= caps, f"full {full} - starved {len(starved_text)}"
