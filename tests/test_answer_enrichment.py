"""An action answer carries enough to choose AND issue the next action.

`test_action_answer_size.py` pins what an action's answer must not carry.
This file pins what it must: the legal targets for every action that targets,
and a one-line-per-permanent digest of the battlefield it left behind.

The gap both close is the same one, seen from opposite ends. A seat that
reads `available_actions` learns that casting Lightning Bolt is legal and
affordable and that it wants one target - and then has no idea what it may
name, because `target_slots` is deliberately the *shape* of the requirement
and never its candidates (`TargetSlot`'s own docstring in the engine says
so). The board that would answer that is `battlefield_state`, one more round
trip and one more whole inference cycle away, which is precisely the cost the
"one call, one meaningful action" answer exists to avoid paying.

So the candidates ride along, as plain strings rather than objects, and the
board rides along as one line per permanent. Both are capped, and both fail
loud rather than quietly short: a truncated slot keeps its true `count` and
says what to call, and a digest that will not fit is replaced *whole* by a
pointer at `battlefield_state` - never emitted half-complete, because a seat
that misses a blocker because the digest ran out of room is exactly the
failure this must not have.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from conftest import add_card, find, resolve_whole_stack, state
from magic_cards_model.machines.legal_actions import legal_actions as engine_legal_actions
from magic_cards_model.models.action import ActionKind
from test_action_answer_size import busy_board
from test_autopass import stage_an_attack_on_bob, stock_libraries
from test_cast_cost_choices import answer
from test_mcp_input_required import call_tool, wired
from test_pays_with import _tap_a_mana_source

from magic_cards_model_api import affordances, seatkit
from magic_cards_model_api import mcp as mcp_module
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.store import store


def targeting_actions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        action
        for action in payload["available_actions"]["actions"]
        if isinstance(action.get("targets"), dict)
    ]


def uuid_of(candidate: str) -> str:
    """The id half of one candidate line, which is what `targets` takes back."""
    return candidate.split(" ", 1)[0]


# -- the whole point: one answer is enough to issue the next action ---------


def held_up_burn(client, server, game: str, uids: dict[str, str]) -> dict[str, Any]:
    """The commonest shape a seat's turn actually has: alice holds up burn
    with `pass_until`, bob casts into it, and alice's next call comes back
    with priority because something new went on the stack (`stop_on_stack`,
    `seatkit.autopass_stop_reason`'s condition 6). That answer is the one an
    agent has to act out of."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Shock", "bob", "hand")

    holding = {"player_uid": uids["alice"], "step": "end_step", "reasoning": "holding up burn"}
    handed_back = answer(call_tool(server, "pass_until", holding))
    call_tool(
        server,
        "cast",
        {"player_uid": uids["bob"], "card": "Shock", "targets": "alice", "reasoning": "burn"},
    )
    woken = answer(
        call_tool(server, "pass_until", {**holding, "resume": json.dumps(handed_back["resume"])})
    )
    assert woken["board"]["priority_holder"] == "alice", woken["stopped_because"]
    return woken


def test_a_seat_can_build_a_whole_cast_out_of_one_pass_until_answer(client, game, monkeypatch):
    """The acceptance condition this task exists for. Everything the `cast`
    call below needs - the card id, and a legal target's id - is read out of
    the `pass_until` answer that woke alice up. There is no second tool call
    between the two, and no `battlefield_state` anywhere."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    woken = held_up_burn(client, server, game, uids)

    # From here down, `woken` is the ONLY source. Nothing else is fetched.
    bolt = next(
        action
        for action in targeting_actions(woken)
        if action["kind"] == "cast" and action["affordable"] == "yes"
    )
    assert bolt["targets"]["understood"] is True
    (slot,) = bolt["targets"]["slots"]
    bears = next(line for line in slot["candidates"] if "Grizzly Bears" in line)

    result = answer(
        call_tool(
            server,
            "cast",
            {
                "player_uid": uids["alice"],
                "card": bolt["card_id"],
                "targets": uuid_of(bears),
                "reasoning": "bolt the bears",
            },
        )
    )

    assert result["outcome"]["allowed"] is True, result["outcome"]
    # The stack is serialised top first, so alice's response leads it.
    assert [card["name"] for card in result["board"]["stack"]] == ["Lightning Bolt", "Shock"]


def test_a_candidate_is_a_plain_string_carrying_the_full_id_it_is_named_by(
    client, game, monkeypatch
):
    """Objects cost 2,384 bytes where the same fourteen candidates cost 972 as
    strings, because `json.dumps(indent=1)` puts every dict key on a line of
    its own. The id is the full uuid and not a prefix: it is what `targets`
    takes back, and a prefix would have to be resolved against a board this
    answer does not carry."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    woken = held_up_burn(client, server, game, uids)
    bears = find(state(client, game)["battlefield"], "Grizzly Bears")

    bolt = next(action for action in targeting_actions(woken) if action["kind"] == "cast")
    (slot,) = bolt["targets"]["slots"]

    for candidate in slot["candidates"]:
        assert isinstance(candidate, str), candidate
    line = next(one for one in slot["candidates"] if "Grizzly Bears" in one)
    assert line == f"{bears['id']} Grizzly Bears (battlefield, bob)", line
    assert UUID(uuid_of(line)) == UUID(bears["id"])
    assert "bob (player)" in slot["candidates"], slot["candidates"]
    assert "alice (player)" in slot["candidates"], slot["candidates"]


def test_an_unaffordable_action_carries_no_candidates_and_the_envelope_says_why(
    client, game, monkeypatch
):
    """`affordable == "no"` rows are listed (CR 118.6 - an unpayable cast is
    still a legal one) but carry no candidate list: enumerating targets for a
    spell that cannot be cast is a list a seat can do nothing with. Said once
    in the envelope rather than as a key on every row."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    add_card(client, game, "Cryptic Command", "alice", "hand")
    woken = held_up_burn(client, server, game, uids)

    available = woken["available_actions"]
    broke = [
        action
        for action in available["actions"]
        if action["kind"] == "cast" and action["affordable"] == "no"
    ]
    assert broke, "the fixture must actually have an unaffordable cast in it"
    for action in broke:
        assert "targets" not in action, action
    assert "affordable" in available["targets_note"]


def test_the_note_appears_even_when_every_targeting_action_is_unaffordable(
    client, game, monkeypatch
):
    """`test_an_unaffordable_action_carries_no_candidates_and_the_envelope_says_why`
    above always had a second, affordable targeting action in the same
    fixture (`held_up_burn`'s own Lightning Bolt) attaching `targets`
    somewhere in the list - so `targets_note`'s own condition, keyed on
    whether any entry actually carries `targets`, never had to prove itself
    on a board where NOTHING does. That is exactly the board a seat holding
    only unaffordable targeting spells has, and it is the board this note
    exists for most: without it, nothing anywhere in the answer explains why
    every one of those rows carries no candidates."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    add_card(client, game, "Lightning Bolt", "alice", "hand")  # {R}, no red source at all

    payload = answer(
        call_tool(server, "pass", {"player_uid": uids["alice"], "reasoning": "look around"})
    )

    available = payload["available_actions"]
    targeting = [a for a in available["actions"] if a.get("target_slots")]
    assert targeting, "the fixture must actually have a targeting action in it"
    assert all(a["affordable"] == "no" for a in targeting), targeting
    assert all("targets" not in a for a in targeting), targeting
    assert "affordable" in available.get("targets_note", ""), available


# -- understood: false is not "nothing is legal" ----------------------------


def _engine_cast_rows(live, card_id: UUID) -> list:
    """The engine's own cast rows for one hand card, `target_slots` and all."""
    return [
        action
        for action in engine_legal_actions(live, "alice")
        if action.card_id == card_id and action.kind is ActionKind.CAST
    ]


def test_an_unreadable_targeting_phrase_says_so_rather_than_listing_nothing(client, game):
    """A modal card's targeting is unanswerable before its modes are chosen.
    The engine's row carries no slot, so no `targets` block is written for it,
    and the browser's `/affordances` answer says `understood: false` - never a
    list a reader would take for "nothing on this board is legal"."""
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game, "Kozilek's Command", "alice", "hand")
    live = store.get(game)
    koz = affordances.find_source(
        live, UUID(find(state(client, game)["hands"]["alice"], "Kozilek's Command")["id"])
    )

    rows = _engine_cast_rows(live, koz.id)
    assert rows and all(row.target_slots == () for row in rows), rows
    assert affordances.candidates(live, koz, "alice")["understood"] is False


def test_no_legal_candidates_is_a_different_answer_from_an_unreadable_phrase(client, game):
    """ "Destroy target creature" with no creature anywhere is understood
    perfectly and has an empty list, which is the truth."""
    add_card(client, game, "Murder", "alice", "hand")
    live = store.get(game)
    murder = UUID(find(state(client, game)["hands"]["alice"], "Murder")["id"])
    (row,) = _engine_cast_rows(live, murder)

    block = affordances.slot_candidates(live, row.target_slots)

    assert block["understood"] is True
    (slot,) = block["slots"]
    assert slot["count"] == 0
    assert slot["candidates"] == []


def test_a_truncated_slot_keeps_the_true_count_and_says_what_to_call(client, game, monkeypatch):
    """`count` is the total, always - a slot that lists three of six still
    says six, because a seat deciding whether to sweep needs the number, not
    the sample."""
    monkeypatch.setattr(affordances, "CANDIDATE_SLOT_CAP", 3)
    for _ in range(6):
        add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game, "Murder", "alice", "hand")
    live = store.get(game)
    murder = UUID(find(state(client, game)["hands"]["alice"], "Murder")["id"])
    (row,) = _engine_cast_rows(live, murder)

    block = affordances.slot_candidates(live, row.target_slots)

    (slot,) = block["slots"]
    assert slot["count"] == 6
    assert len(slot["candidates"]) == 3
    assert slot["truncated"] == 3
    assert "battlefield_state" in block["more_with"]


def test_an_untruncated_slot_carries_neither_truncated_nor_more_with(client, game):
    """The absent keys are the signal that nothing was cut."""
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game, "Murder", "alice", "hand")
    live = store.get(game)
    murder = UUID(find(state(client, game)["hands"]["alice"], "Murder")["id"])
    (row,) = _engine_cast_rows(live, murder)

    block = affordances.slot_candidates(live, row.target_slots)

    (slot,) = block["slots"]
    assert "truncated" not in slot, slot
    assert "more_with" not in block, block


# -- the battlefield digest -------------------------------------------------


def test_an_action_answer_carries_one_line_per_permanent(client, game, monkeypatch):
    """`_action_answer` already receives the whole state and throws the
    battlefield away. One line per permanent is ~62 bytes where the card view
    is ~1,300, and it is the difference between a seat that knows what is on
    the board and one that has to ask."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "bob", "battlefield")
    bears = find(state(client, game)["battlefield"], "Grizzly Bears")

    payload = answer(
        call_tool(server, "pass", {"player_uid": uids["alice"], "reasoning": "look around"})
    )

    digest = payload["board"]["battlefield_digest"]
    assert set(digest) == {"alice", "bob"}
    (alice_line,) = digest["alice"]
    assert alice_line == f"{bears['id']} Grizzly Bears 2/2 sick", alice_line
    assert any("Sol Ring" in line for line in digest["bob"]), digest["bob"]


def test_the_digest_names_what_each_attacker_is_attacking(client, game, monkeypatch):
    """The half of the digest a defender's whole decision hangs on. `combat`
    survives into `board` as the engine's own `{attacker, defender, blockers}`
    id triples, which say nothing a reader can act on without a battlefield to
    resolve them against - and that battlefield is the very section this
    answer leaves out. So the attack is written onto the permanent's own line,
    where whoever is choosing blockers is already looking."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    stock_libraries(client, game)
    bear = stage_an_attack_on_bob(client, game)

    payload = answer(
        call_tool(server, "pass", {"player_uid": uids["alice"], "reasoning": "swinging"})
    )

    (line,) = payload["board"]["battlefield_digest"]["alice"]
    # Attacking taps it (CR 508.1f), and the digest says both.
    assert line == f"{bear} Grizzly Bears 2/2 T attacking bob", line
    assert payload["board"]["battlefield_digest"]["bob"] == []


def test_the_digest_spells_out_tapped_and_counters_and_omits_what_is_absent(
    client, game, monkeypatch
):
    """The format pinned rather than described, and the absent halves pinned
    with it: a vanilla untapped land is its id and its name and nothing
    else, which is what "absent parts omitted" has to mean for the ~62 bytes
    a line to hold.

    Field order is the spec's own: `<P>/<T> <T> <sick> <counter>x<n> on
    <attached> attacking <x>` - `T` and `sick` both read as flags about the
    permanent itself and belong together, ahead of `+1/+1x2`, which is a
    count rather than a flag."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    add_card(client, game, "Grizzly Bears", "alice", "battlefield", tapped=True)
    add_card(client, game, "Mountain", "alice", "battlefield")
    board = state(client, game)["battlefield"]
    bears = find(board, "Grizzly Bears")
    patched = client.patch(
        f"/api/games/{game}/cards/{bears['id']}", json={"counters": {"+1/+1": 2}}
    )
    assert patched.status_code == 200, patched.text

    payload = answer(
        call_tool(server, "pass", {"player_uid": uids["alice"], "reasoning": "look around"})
    )

    lines = payload["board"]["battlefield_digest"]["alice"]
    bears_line = next(line for line in lines if "Grizzly Bears" in line)
    assert bears_line == f"{bears['id']} Grizzly Bears 4/4 T sick +1/+1x2", bears_line
    mountain_line = next(line for line in lines if "Mountain" in line)
    assert mountain_line == f"{find(board, 'Mountain')['id']} Mountain", mountain_line


def test_the_digest_says_what_an_aura_is_attached_to(client, game, monkeypatch):
    """An Aura or Equipment whose line did not say what it was on would be a
    line about a permanent whose whole function is missing - and `attached_to`
    is a battlefield-only field, so this answer is the only place a seat that
    is not calling `battlefield_state` would ever see it."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    for _ in range(2):
        add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game, "Pacifism", "alice", "hand")
    bears = find(state(client, game)["battlefield"], "Grizzly Bears")

    call_tool(
        server,
        "cast",
        {
            "player_uid": uids["alice"],
            "card": "Pacifism",
            "targets": bears["id"],
            "reasoning": "shutting the bear off",
        },
    )
    resolve_whole_stack(client, game)
    payload = answer(
        call_tool(server, "pass", {"player_uid": uids["alice"], "reasoning": "look around"})
    )

    pacifism = next(
        line for line in payload["board"]["battlefield_digest"]["alice"] if "Pacifism" in line
    )
    assert pacifism.endswith("Pacifism on Grizzly Bears"), pacifism


def test_the_digest_is_not_the_battlefield_section_and_does_not_claim_to_be(
    client, game, monkeypatch
):
    """`omitted_sections` still names `battlefield`, because the section - the
    full card views, with oracle text, images and abilities - genuinely is not
    here. The digest is a summary of it, and calling it the section would be a
    lie `tests/test_action_answer_size.py` correctly catches."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    busy_board(client, game)

    payload = answer(
        call_tool(server, "pass", {"player_uid": uids["alice"], "reasoning": "look around"})
    )

    board = payload["board"]
    assert "battlefield" in board["omitted_sections"]
    assert "battlefield" not in board
    assert len(board["battlefield_digest"]["alice"]) == 14


def test_a_digest_that_will_not_fit_is_replaced_whole_and_never_half_emitted(
    client, game, monkeypatch
):
    """The failure this must not have: a seat forming a WRONG BELIEF about
    the board - missing a blocker because it read a partial digest and took
    it for the whole battlefield. So the cap does not trim the digest's own
    list - it throws the whole thing away and says where the board actually
    is, the same promise `omitted_sections` already makes for the sections
    it names.

    That is a narrower claim than "no permanent's name may appear anywhere
    in the answer", which is the assertion this test used to make and which
    `pays_with` (`Action.pays_with`'s tap plan), `unaffordable_reason` (the
    engine's own refusal prose) and a targeting action's own candidate
    lines (`"<uuid> <name> (<zone>, <controller>)"`, deliberately added by
    `_attach_target_candidates` - the whole point of that enrichment) all
    violate the moment the board has more than a handful of permanents on
    it. None of those three is the hazard this test guards against: each
    names a card in service of one stated, scoped fact about one action -
    "this is what the Bolt would tap", "this is why the Cryptic Command is
    unaffordable", "this is what the Bolt may legally hit" - never as a
    claim to have surveyed the whole battlefield the way the digest does.
    A seat reading one cannot mistake it for a board summary, so none of the
    three can produce the specific wrong belief ("I have seen every
    permanent") this test is pinning against - and scrubbing them would gut
    the enrichment work that put them there on purpose. `pays_with` gets its
    own separate byte budget below regardless (`MAX_PAYS_WITH_BYTES`) - a
    real but distinct concern from this test's own, about an answer's total
    size rather than about a false belief in its completeness.

    So the leak check here is scoped to `board` itself - the digest's own
    home - rather than the whole answer text."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    monkeypatch.setattr(mcp_module, "MAX_DIGEST_BYTES", 400)
    busy_board(client, game)
    permanents = state(client, game)["battlefield"]

    result = call_tool(server, "pass", {"player_uid": uids["alice"], "reasoning": "look around"})
    payload = answer(result)
    board = json.dumps(payload["board"])
    digest = payload["board"]["battlefield_digest"]

    assert isinstance(digest, str), digest
    assert "battlefield_state" in digest
    # A digest line is `"<id> <name> ..."` (`mcp._digest_line`), so that
    # prefix is what a half-emitted digest would leave behind - scanned
    # across the whole board block, which is where the digest lives and
    # the only place a board summary may come from. Deliberately NOT
    # scanned across the whole answer: a card's bare name is not the
    # digest's to own, and the action enumeration alongside it names cards
    # for reasons of its own that this cap says nothing about - a target
    # candidate line, and `Action.pays_with` naming the land an affordable
    # cast would tap. Those have their own budget
    # (`MAX_CANDIDATE_BYTES`, below) and their own contract.
    for card in permanents:
        line = f"{card['id']} {card['name']}"
        assert line not in board, f"{card['name']} leaked out of a digest that did not fit"


def test_the_digest_survives_a_board_with_nothing_on_it(client, game, monkeypatch):
    """An empty battlefield is a real board state, and an empty list per
    player says so - it is the digest being *absent* that would be ambiguous."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)

    payload = answer(
        call_tool(server, "pass", {"player_uid": uids["alice"], "reasoning": "look around"})
    )

    assert payload["board"]["battlefield_digest"] == {"alice": [], "bob": []}


# -- the answer's own candidate budget --------------------------------------


def test_the_candidate_budget_is_spent_in_list_order_and_drops_names_not_counts(
    client, game, monkeypatch
):
    """`MAX_CANDIDATE_BYTES` bounds the whole answer, not one entry, and it is
    spent in the order the engine enumerated. An entry past the budget keeps
    its `slots[].count` - still the true total - and says where the names are;
    it never comes back with a shorter list that reads as complete."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    for _ in range(3):
        add_card(client, game, "Mountain", "alice", "battlefield")
    for _ in range(6):
        add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    for name in ("Lightning Bolt", "Shock", "Shock"):
        add_card(client, game, name, "alice", "hand")
    # Enough for the first entry's own candidate list and nothing after it,
    # measured off this very board rather than guessed - the point is the
    # boundary, and a hand-picked number would move every time a fixture card
    # changed length.
    whole = targeting_actions(
        answer(call_tool(server, "pass", {"player_uid": uids["alice"], "reasoning": "looking"}))
    )
    one_entry = len(json.dumps([whole[0]["targets"]["slots"][0]["candidates"]], indent=1))
    monkeypatch.setattr(mcp_module, "MAX_CANDIDATE_BYTES", one_entry)

    payload = answer(
        call_tool(server, "pass", {"player_uid": uids["alice"], "reasoning": "look around"})
    )

    enriched = targeting_actions(payload)
    assert len(enriched) >= 3, enriched
    named = [one for one in enriched if one["targets"]["slots"][0].get("candidates")]
    silent = [one for one in enriched if "candidates" not in one["targets"]["slots"][0]]
    assert named, "the budget must have paid for the entries a reader meets first"
    assert silent, "and must have run out before the last of them"
    assert enriched.index(named[-1]) < enriched.index(silent[0]), "spent out of order"
    for one in silent:
        (slot,) = one["targets"]["slots"]
        assert slot["count"] == 8, slot
        assert "battlefield_state" in one["targets"]["more_with"]
        assert one["targets"]["understood"] is True


def test_the_budget_never_reaches_back_into_the_answer_it_narrowed(client, game, monkeypatch):
    """`seat_cli.py` prints `run_action`'s own dict in full for a human who
    can scroll, and `tests/test_action_flow.py` pins that contract. The MCP
    boundary's budget must not mutate the entries it was handed on its way
    past them."""
    seats, uids = wired(client, monkeypatch, game)
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    monkeypatch.setattr(mcp_module, "MAX_CANDIDATE_BYTES", 0)

    live = seats.seat_for(uids["alice"])
    available = seatkit.available_actions(live, live.fetch_state())
    before = json.dumps(available, sort_keys=True)
    mcp_module._compact_actions(available)

    assert json.dumps(available, sort_keys=True) == before


# -- pays_with's own budget ---------------------------------------------------


def test_pays_with_keeps_names_while_its_own_budget_lasts_and_drops_them_once_spent(
    client, game, monkeypatch
):
    """`pays_with` is unbudgeted no longer. `MAX_PAYS_WITH_BYTES` spends in
    list order exactly like `MAX_CANDIDATE_BYTES` already does for target
    candidates, and an entry that does not fit loses only its `name` -
    `card_id`, `ability_index`, `adds` and `restriction` all survive, so the
    plan itself is never partial, only less convenient to read without a
    second `battlefield_state` lookup."""
    seats, uids = wired(client, monkeypatch, game)
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    add_card(client, game, "Lightning Bolt", "alice", "hand")

    live = seats.seat_for(uids["alice"])
    available = seatkit.available_actions(live, live.fetch_state())
    priced = [a for a in available["actions"] if a.get("pays_with")]
    assert len(priced) >= 2, priced
    one_entry = len(json.dumps(priced[0]["pays_with"], indent=1))
    monkeypatch.setattr(mcp_module, "MAX_PAYS_WITH_BYTES", one_entry)

    compacted = mcp_module._compact_actions(available)
    trimmed = [a for a in compacted["actions"] if a.get("pays_with")]
    omitted_marker = mcp_module._PAYS_WITH_NAME_OMITTED
    named = [a for a in trimmed if a["pays_with"][0]["name"] != omitted_marker]
    omitted = [a for a in trimmed if a["pays_with"][0]["name"] == omitted_marker]
    assert named, "the budget must have paid for the entry a reader meets first"
    assert omitted, "and must have run out before the last of them"
    assert trimmed.index(named[-1]) < trimmed.index(omitted[0]), "spent out of order"
    for action in omitted:
        entry = action["pays_with"][0]
        assert entry["card_id"], entry
        assert entry["adds"], entry


def test_pays_with_is_unaffected_by_the_digest_being_withheld(client, game, monkeypatch):
    """`MAX_DIGEST_BYTES` and `MAX_PAYS_WITH_BYTES` are independent budgets
    on purpose: a board too big for its own one-line-per-permanent digest
    does not also blank out a still-legal cast's own tap plan, because the
    two say different things. The digest claims to summarise the whole
    battlefield truthfully and must never do that half-way; `pays_with`
    never claims that at all - it is one fact about one action - so there is
    nothing dishonest about it surviving a digest that did not fit."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    monkeypatch.setattr(mcp_module, "MAX_DIGEST_BYTES", 1)
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")

    payload = _tap_a_mana_source(server, uids, client, game, "Forest")
    assert payload["board"]["battlefield_digest"] == mcp_module._DIGEST_TOO_BIG

    casts = [a for a in payload["available_actions"]["actions"] if a.get("kind") == "cast"]
    (cast_action,) = casts
    assert cast_action["pays_with"][0]["name"] == "Mountain"
