"""Sneak (CR 702.190a), the alternative cast engine flow 2 T13 built
(`CastRecord.sneak_defender`), reachable end to end through the api.

The engine's own flag, verbatim: "The API's engine_api.cast_card may need to
pass choosing when a keyword is used." `engine_api._drive_cast` already
forwards `choosing` to `Casting.use_keyword` for every keyword (the fix
`test_cast_cost_choices.py` landed for evoke's own pitch cost covers Sneak's
announcement for free, since neither branch names a keyword of its own). The
actual gap measured here is one step earlier: `affordances._cards_in_reach`
never named `ComponentKind.RETURN_TO_HAND` a reachable cost, so `GET
.../affordances/{card_id}`'s `alternative_costs[i].candidates` came back
empty for Sneak even though `requires` correctly read 1 - a seat, or the web
UI's own alt-cost chips, had no way to *discover* which unblocked attacker
could pay it, only that one was needed. `casting.use_keyword("Sneak",
choosing=[...])` already worked once a caller somehow knew the id to send.

Every card here is a real Scryfall printing already shipped with the engine
package (`magic_cards_model/data/printings/michelangelo_s_technique.json`,
`.../foot_ninjas.json`, both Modern/Pioneer/Standard legal), and every board
is played through the real API - no stand-in engine for either fixture. The
mcp tool and seat_cli tests use light doubles for the seat plumbing the same
way `test_cast_cost_choices.py` does (`wired`/`call_tool`), never for the
engine itself.
"""

from __future__ import annotations

import json
from typing import Any

from conftest import add_card, find, resolve_whole_stack, state
from fastapi.testclient import TestClient
from test_legal_actions_endpoint import legal_actions
from test_mcp_input_required import call_tool, wired
from ui_node import requires_node, run_module

from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def hand_id(client: TestClient, game: str, name: str, player: str = "alice") -> str:
    return find(state(client, game)["hands"][player], name)["id"]


def battlefield_id(client: TestClient, game: str, name: str) -> str:
    return find(state(client, game)["battlefield"], name)["id"]


def lands(client: TestClient, game: str, name: str, count: int, player: str = "alice") -> None:
    for _ in range(count):
        add_card(client, game, name, player, "battlefield")


def text_of(result: Any) -> str:
    """The text a tool call actually put on the wire - `test_cast_cost_
    choices.py`'s own helper, restated because a scenario test file does not
    import from another scenario test file."""
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


def attack_and_leave_unblocked(
    client: TestClient, game: str, attacker_id: str, defender: str = "bob"
) -> None:
    """Alice attacks with `attacker_id`, `defender` declares no blockers, and
    the game is left sitting in alice's own declare blockers step with
    priority hers to use - CR 702.190a's own window, "any time you could cast
    an instant during your declare blockers step" - and `attacker_id`
    unblocked (CR 509.1h). Nothing here advances past that point: the caller
    drives whatever `POST .../actions/cast` (or a plain `GET .../affordances`
    or `GET .../legal-actions`) it wants from exactly this position.

    **Engine bug worked around here, out of this worktree's scope to fix**:
    `Game.declare_blockers_step` (magic_cards_model/models/game.py, ~line
    4213) only calls `self.declare_blockers(blocks)` `if blocks:` - so a
    defending player who legally declares *no* blockers at all (the single
    most common case an entirely unblocked attacker sees) never has
    `Combat.blockers_declared` set `True`. `why_not_unblocked` (CR 509.1h)
    and `_why_not_return_to_hand`/`KeywordCastOption.why_not` all read that
    flag, so every all-clear declaration leaves both Sneak and ninjutsu
    refused with "blockers have not been declared yet" forever - proven by
    reaching into the live engine object here and printing
    `store.get(game).combat.blockers_declared` right after a real `blocks:
    []` POST to `.../actions/declare-blockers` returned `allowed: True`: it
    read `False`. `machines/legal_actions.py:1539` reads the same flag, so
    this is not Sneak-specific - it is reported to the orchestrator to route
    to the engine repo, and worked around here (the same "reach past the
    API into the live engine object" idiom `conftest.force_turn_position`
    already uses for test setup) so this file can still prove the api layer
    itself end to end.
    """
    client.patch(f"/api/games/{game}/cards/{attacker_id}", json={"summoning_sick": False})
    uids = seats_for(game)
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    into_declare_attackers = client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    assert into_declare_attackers.json()["pending_decision"]["kind"] == "declare_attackers", (
        into_declare_attackers.json()
    )
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": attacker_id, "defender": defender}]},
    )
    assert attack.json()["allowed"] is True, attack.json()
    into_declare_blockers = client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    assert into_declare_blockers.json()["pending_decision"]["kind"] == "declare_blockers", (
        into_declare_blockers.json()
    )
    blocks = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids[defender]},
        json={"player": defender, "blocks": []},
    )
    assert blocks.json()["allowed"] is True, blocks.json()
    assert state(client, game)["step"] == "declare_blockers"
    live = store.get(game)
    assert live is not None and live.combat is not None
    live.combat.blockers_declared = True


# -- discovery: cast_options on the hand card ---------------------------------


def test_sneak_is_unusable_outside_the_casters_own_declare_blockers_step(client, game):
    """CR 702.190a's window has not opened yet: the option is still published
    (a seat, or the UI, needs to see it exists at all) but `usable_now` is
    false and says why - never silently absent."""
    add_card(client, game, "Michelangelo's Technique", "alice", "hand")
    card = hand_id(client, game, "Michelangelo's Technique")
    affordance_card = find(state(client, game)["hands"]["alice"], "Michelangelo's Technique")
    assert card == affordance_card["id"]

    sneak = next(entry for entry in affordance_card["cast_options"] if entry["keyword"] == "Sneak")
    assert sneak["rule"] == "CR 702.190a"
    assert sneak["usable_now"] is False
    assert "declare blockers step" in sneak["why_not"], sneak


def test_sneak_becomes_usable_once_alice_controls_an_unblocked_attacker(client, game):
    add_card(client, game, "Michelangelo's Technique", "alice", "hand")
    bears = add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    attack_and_leave_unblocked(client, game, bears)

    card = find(state(client, game)["hands"]["alice"], "Michelangelo's Technique")
    sneak = next(entry for entry in card["cast_options"] if entry["keyword"] == "Sneak")
    assert sneak["usable_now"] is True, sneak
    assert sneak["why_not"] is None
    assert sneak["cost"] == "{3}{G}, Return an unblocked creature you control to its owner's hand"
    assert sneak["pays_mana_cost"] is False


# -- the measured gap: which creature the affordance says can pay it ---------


def test_the_affordance_names_the_unblocked_attacker_sneak_can_return(client, game):
    """The reported gap itself: `requires` was always 1, `candidates` came
    back `[]`. A blocked or non-attacking creature alice controls, and a
    creature bob controls, must never be offered."""
    add_card(client, game, "Michelangelo's Technique", "alice", "hand")
    bears = add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    add_card(client, game, "Elvish Mystic", "alice", "battlefield")  # never attacks, never offered
    add_card(client, game, "Llanowar Elves", "bob", "battlefield")  # not alice's, never offered
    attack_and_leave_unblocked(client, game, bears)

    technique = hand_id(client, game, "Michelangelo's Technique")
    affordance = client.get(
        f"/api/games/{game}/affordances/{technique}", params={"player": "alice"}
    ).json()
    sneak = next(alt for alt in affordance["alternative_costs"] if alt["keyword"] == "Sneak")

    assert sneak["requires"] == 1, sneak
    assert [c["name"] for c in sneak["candidates"]] == ["Grizzly Bears"], sneak
    assert sneak["candidates"][0]["id"] == bears
    assert sneak["candidates"][0]["pays"] == "return_to_hand", sneak


def test_a_blocked_attacker_is_never_offered_as_a_sneak_candidate(client, game):
    """CR 509.1h: blocked, not unblocked, even with no blocker left on the
    battlefield to show for it - `why_not_unblocked` reads the live combat,
    not "no creature is currently blocking". A second, unblocked attacker
    keeps Sneak itself available (`Casting`'s own constructor needs *some*
    currently-legal door onto the card before it can even be built to
    enumerate alternatives at all - a card with no legal door of any kind
    right now, keyword included, is a different, pre-existing gap this task
    does not touch), so this isolates the one thing under test: which
    creature the option names as a candidate."""
    add_card(client, game, "Michelangelo's Technique", "alice", "hand")
    bears = add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    elves = add_card(client, game, "Elvish Mystic", "alice", "battlefield")["added"]["card_id"]
    blocker = add_card(client, game, "Wall of Omens", "bob", "battlefield")["added"]["card_id"]
    for creature in (bears, elves):
        client.patch(f"/api/games/{game}/cards/{creature}", json={"summoning_sick": False})
    uids = seats_for(game)
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={
            "attackers": [
                {"card_id": bears, "defender": "bob"},
                {"card_id": elves, "defender": "bob"},
            ]
        },
    )
    client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    # Blocking bears alone leaves elves unblocked, and a non-empty `blocks`
    # list makes the engine set `Combat.blockers_declared` correctly on its
    # own (see `attack_and_leave_unblocked`'s own docstring for the case
    # where it does not).
    blocked = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={"player": "bob", "blocks": [{"blocker": blocker, "blocked": bears}]},
    )
    assert blocked.json()["allowed"] is True, blocked.json()

    technique = hand_id(client, game, "Michelangelo's Technique")
    affordance = client.get(
        f"/api/games/{game}/affordances/{technique}", params={"player": "alice"}
    ).json()
    sneak = next(alt for alt in affordance["alternative_costs"] if alt["keyword"] == "Sneak")
    assert [c["name"] for c in sneak["candidates"]] == ["Elvish Mystic"], sneak
    assert sneak["available"] is True, sneak


# -- legal-actions surfaces the row -------------------------------------------


def test_legal_actions_surfaces_the_sneak_cast_row_during_declare_blockers(client, game):
    add_card(client, game, "Michelangelo's Technique", "alice", "hand")
    bears = add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    attack_and_leave_unblocked(client, game, bears)

    rows = legal_actions(client, game, "alice")
    sneak_rows = [row for row in rows if row.get("keyword") == "Sneak"]
    assert sneak_rows, rows
    assert sneak_rows[0]["kind"] == "cast", sneak_rows[0]
    assert (
        sneak_rows[0]["cost"]
        == "{3}{G}, Return an unblocked creature you control to its owner's hand"
    ), sneak_rows[0]


def test_legal_actions_offers_no_sneak_row_before_the_declare_blockers_step(client, game):
    add_card(client, game, "Michelangelo's Technique", "alice", "hand")
    rows = legal_actions(client, game, "alice")
    assert not [row for row in rows if row.get("keyword") == "Sneak"], rows


# -- casting it, through the real engine --------------------------------------


def test_the_http_cast_endpoint_sneaks_out_michelangelos_technique(client, game):
    """The whole announcement: `keyword="Sneak"`, `choosing=[<the unblocked
    attacker>]`, `{3}{G}` rather than the printed `{4}{G}` - the exact shape
    `engine_api._drive_cast`'s `req.keyword` branch was flagged to need."""
    lands(client, game, "Forest", 4)
    add_card(client, game, "Michelangelo's Technique", "alice", "hand")
    bears = add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    attack_and_leave_unblocked(client, game, bears)

    forests = [
        card["id"] for card in state(client, game)["battlefield"] if card["name"] == "Forest"
    ]
    outcome = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Michelangelo's Technique",
            "keyword": "Sneak",
            "choosing": [bears],
            "mana_sources": forests,
        },
    ).json()

    assert outcome["allowed"] is True, outcome
    assert outcome["cost"] == "{3}{G}", outcome
    board = state(client, game)
    assert [card["name"] for card in board["stack"]] == ["Michelangelo's Technique"], board["stack"]
    # CR 601.2h: the return is part of paying the cost, so the attacker is
    # back in alice's hand before the spell has even resolved.
    assert [card["name"] for card in board["hands"]["alice"]] == ["Grizzly Bears"], board["hands"]
    assert board["combat"]["attacks"] == [], board["combat"]


def test_the_http_cast_endpoint_sneaks_out_foot_ninjas_tapped_and_attacking(client, game):
    """CR 702.190b: "A permanent spell whose sneak cost was paid enters the
    battlefield tapped and attacking" - the same defender the returned
    creature was attacking, read off `CastRecord.sneak_defender` before
    payment took the attacker out of combat."""
    lands(client, game, "Plains", 4)
    add_card(client, game, "Foot Ninjas", "alice", "hand")
    bears = add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    attack_and_leave_unblocked(client, game, bears, defender="bob")

    plains = [card["id"] for card in state(client, game)["battlefield"] if card["name"] == "Plains"]
    outcome = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Foot Ninjas",
            "keyword": "Sneak",
            "choosing": [bears],
            "mana_sources": plains,
        },
    ).json()
    assert outcome["allowed"] is True, outcome
    assert outcome["cost"] == "{3}{W/B}", outcome

    # Puts Foot Ninjas onto the battlefield, then drains its own "When this
    # creature enters, you gain 3 life" trigger off the stack too.
    resolve_whole_stack(client, game)

    board = state(client, game)
    ninjas = find(board["battlefield"], "Foot Ninjas")
    assert ninjas["tapped"] is True, ninjas
    assert board["combat"]["attacks"] == [
        {"attacker": ninjas["id"], "defender": "bob", "blockers": []}
    ], board["combat"]
    assert find(board["hands"]["alice"], "Grizzly Bears")
    alice_life = next(p["life"] for p in board["players"] if p["username"] == "alice")
    assert alice_life == 23, board["players"]  # 20 starting + 3 (Foot Ninjas' own ETB)


def test_sneak_is_refused_in_the_engines_own_words_with_nothing_chosen(client, game):
    """Fail closed: leaving `choosing` off is refused with the engine's own
    count, not a silent no-op and not a 400 with no `allowed` key."""
    lands(client, game, "Forest", 4)
    add_card(client, game, "Michelangelo's Technique", "alice", "hand")
    bears = add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    attack_and_leave_unblocked(client, game, bears)

    forests = [
        card["id"] for card in state(client, game)["battlefield"] if card["name"] == "Forest"
    ]
    outcome = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Michelangelo's Technique",
            "keyword": "Sneak",
            "mana_sources": forests,
        },
    ).json()
    assert outcome["allowed"] is False, outcome
    assert "needs an unblocked creature" in outcome["reason"], outcome
    assert "chosen to return to its owner's hand" in outcome["reason"], outcome


# -- the mcp tool and the CLI --------------------------------------------------


def test_the_cast_tool_sneaks_out_michelangelos_technique(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    lands(client, game, "Forest", 4)
    add_card(client, game, "Michelangelo's Technique", "alice", "hand")
    bears = add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    attack_and_leave_unblocked(client, game, bears)

    result = call_tool(
        server,
        "cast",
        {
            "player_uid": uids["alice"],
            "card": "Michelangelo's Technique",
            "keyword": "Sneak",
            "choosing": bears,
            "hold_priority": True,
            "reasoning": "sneaking the technique out past a blocker-free board",
        },
    )
    assert answer(result)["outcome"]["allowed"] is True, answer(result)
    board = state(client, game)
    assert [card["name"] for card in board["stack"]] == ["Michelangelo's Technique"]
    assert [card["name"] for card in board["hands"]["alice"]] == ["Grizzly Bears"]


def test_the_cast_tools_docstring_mentions_sneak():
    """The seat has to be able to *discover* the keyword exists at all,
    exactly like every other hand keyword this file documents."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "magic_cards_model_api" / "mcp.py"
    ).read_text()
    doc = None
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "cast":
            doc = ast.get_docstring(node) or ""
            break
    assert doc is not None, "no `cast` tool in mcp.py"
    assert '"Sneak"' in doc, doc
    assert "702.190a" in doc, doc
    assert "declare blockers" in doc, doc


def test_the_seat_cli_can_sneak_out_michelangelos_technique(client, game, monkeypatch, capsys):
    import httpx
    from fastapi.testclient import TestClient as _TestClient
    from test_decisions import seats_for as decisions_seats_for

    from magic_cards_model_api import seat_cli
    from magic_cards_model_api.main import app

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _TestClient(app))
    uids = decisions_seats_for(client, game)
    lands(client, game, "Forest", 4)
    add_card(client, game, "Michelangelo's Technique", "alice", "hand")
    bears = add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    attack_and_leave_unblocked(client, game, bears)

    seat_cli.run(
        [
            "--api",
            "http://testserver",
            "--game",
            game,
            "--uid",
            uids["alice"],
            "--reasoning",
            "sneaking the technique out",
            "cast",
            "Michelangelo's Technique",
            "--keyword",
            "Sneak",
            "--choosing",
            bears,
            "--hold",
        ]
    )

    capsys.readouterr()
    board = state(client, game)
    assert [card["name"] for card in board["stack"]] == ["Michelangelo's Technique"]


def test_seat_clis_keyword_help_mentions_sneak():
    """`--keyword`'s own help text enumerates every hand keyword by name -
    the same discoverability question the mcp docstring answers."""
    from magic_cards_model_api import seat_cli

    parser = seat_cli.build_parser()
    cast_action = None
    for action in parser._actions:
        if action.dest == "command":
            for sub in action.choices.values():
                for sub_action in sub._actions:
                    if sub_action.dest == "keyword":
                        cast_action = sub_action
    assert cast_action is not None, "no --keyword argument found on the cast subcommand"
    assert "Sneak" in cast_action.help, cast_action.help


# -- the UI offers it exactly like evoke, ninjutsu's closest wired precedent -


@requires_node
def test_the_ui_offers_sneak_exactly_the_way_it_offers_evoke(client, game):
    """`ninjutsu` itself is not wired through this api at all - nothing in
    `ui/`, `engine_api.py` or `mcp.py` names it, so there is no ninjutsu
    surface to imitate. The nearest *wired* precedent, and the one this
    task's own brief points at ("the closest precedent"), is evoke's own
    alt-cost flow (`test_evoke_ui_decision.py`): a keyword alternative cost
    that needs an object named. `ui/actions.js`'s `needsChoices`/
    `needsExplicitFire`/`readyToFire` and `ui/render/ribbon.js`'s
    `renderAltCosts` are already keyword-agnostic - built off
    `card.cast_options`/`action.affordance.alternative_costs`, never a
    hard-coded keyword list - so Sneak rides the exact same path evoke does
    with no UI code of its own, proven here the same way that file proves
    evoke's: the real engine's own affordance, fed to the real `ui/actions.js`
    module."""
    add_card(client, game, "Michelangelo's Technique", "alice", "hand")
    bears = add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    add_card(client, game, "Elvish Mystic", "alice", "battlefield")  # never attacks, never offered
    attack_and_leave_unblocked(client, game, bears)

    card = find(state(client, game)["hands"]["alice"], "Michelangelo's Technique")
    assert card.get("cast_options"), card
    assert run_module("actions.js", "m.needsChoices(card, null)", card=card) is True

    technique = hand_id(client, game, "Michelangelo's Technique")
    affordance = client.get(
        f"/api/games/{game}/affordances/{technique}", params={"player": "alice"}
    ).json()
    sneak = next(alt for alt in affordance["alternative_costs"] if alt["keyword"] == "Sneak")
    assert [c["name"] for c in sneak["candidates"]] == ["Grizzly Bears"], sneak

    action_before_choice = {
        "pinned": [],
        "affordance": affordance,
        "slots": [],
        "altCost": None,
    }
    action_after_choice = {**action_before_choice, "altCost": sneak["index"]}
    fire_gate = run_module(
        "actions.js",
        "[m.needsExplicitFire(before), m.needsExplicitFire(after)]",
        before=action_before_choice,
        after=action_after_choice,
    )
    assert fire_gate == [
        True,  # nothing chosen yet - never auto-fire at the printed mana cost
        False,  # Sneak chosen - the click the player actually made
    ]

    result = run_module(
        "actions.js",
        """(() => {
          let a = m.begin('cast', card, {});
          a = m.withAffordance(a, affordance);
          a = m.chooseAltCost(a, sneak.index);
          const beforePick = m.readyToFire(a);
          a = m.togglePitch(a, sneak.candidates[0].id);
          const afterPick = m.readyToFire(a);
          return [beforePick, afterPick];
        })()""",
        card=card,
        affordance=affordance,
        sneak=sneak,
    )
    assert result == [
        False,  # Sneak chosen, no creature named yet to return
        True,  # the one unblocked attacker named - ready to fire
    ]
