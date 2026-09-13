"""U1 (battle 7 review): an until-end-of-turn control change in the API.

CR 613.1b puts control-changing effects in layer 2, and CR 514.2 ends "until
end of turn" effects in the cleanup step (both read whole with `mise run
rules:find` in the session that wrote this file). The engine answers
`CardState.controller` off the computed board once a card is bound to its
game (`CardState.bind_to_game`), and `Game.model_post_init` binds every card
a game holds - but a pydantic `model_copy(deep=True)` does not run it, and
every command here runs on such a copy (`commands._replay_copy`,
`engine_api.dry_run_copy`). Each copied card stayed bound to a detached
duplicate of the pre-command game, which had no Threaten effect in it, so the
card view went on reading the old controller after the spell resolved.

Threaten is in the engine's pool; Act of Treason ("Gain control of target
creature until end of turn. Untap that creature. It gains haste until end of
turn.") is the off-pool printing with different wording, recorded into
tests/fixtures/scryfall. Mother of Runes is the stolen creature with a {T}
ability, so an activate row has a seat to go to.
"""

from __future__ import annotations

from conftest import add_card, find, force_turn_position, state
from magic_cards_model.machines import Step
from test_front_end_parity import uid_for

from magic_cards_model_api.mcp import _battlefield_digest


def _steal(client, game, spell: str, creature: str) -> str:
    """Bob controls `creature`; alice casts `spell` on it and it resolves.
    Returns the creature's id."""
    for _ in range(3):
        add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, creature, "bob", "battlefield")
    add_card(client, game, spell, "alice", "hand")
    board = state(client, game)
    target = find(board["battlefield"], creature)
    mountains = [c["id"] for c in board["battlefield"] if c["name"] == "Mountain"]
    assert target["controller"] == "bob"
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": spell,
            "targets": [target["id"]],
            "mana_sources": mountains,
        },
    )
    assert cast.json()["allowed"] is True, cast.text
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    resolved = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert resolved.json()["allowed"] is True, resolved.text
    assert state(client, game)["stack"] == []
    return target["id"]


def _controller_everywhere(client, game, card_id: str) -> set[str]:
    """What the admin view, both seat views and the MCP digest say."""
    seen = set()
    for uid in (None, uid_for(game, "alice"), uid_for(game, "bob")):
        board = state(client, game, uid)
        card = next(c for c in board["battlefield"] if c["id"] == card_id)
        seen.add(card["controller"])
        digest = _battlefield_digest(board)
        (owner_of_line,) = [
            who for who, lines in digest.items() if any(card_id in line for line in lines)
        ]
        seen.add(owner_of_line)
    return seen


def _rows(client, game, player: str) -> list[dict]:
    return client.get(
        f"/api/games/{game}/legal-actions",
        params={"player": player, "uid": uid_for(game, player), "candidates": "true"},
    ).json()["actions"]


def _activate_rows_for(rows: list[dict], card_id: str) -> list[dict]:
    return [r for r in rows if r["kind"] == "activate" and r["card_id"] == card_id]


def _end_the_turn(client, game) -> None:
    """Advance alice's turn through its cleanup step (CR 514.2) into bob's."""
    for _ in range(15):
        board = state(client, game)
        if board["active_player"] == "bob":
            return
        body = client.post(f"/api/games/{game}/advance", json={"player": "alice"}).json()
        assert body.get("allowed", True) is not False, body
        if (body.get("pending_decision") or {}).get("kind") == "declare_attackers":
            declared = client.post(
                f"/api/games/{game}/actions/attack",
                params={"uid": uid_for(game, "alice")},
                json={"attackers": []},
            ).json()
            assert declared.get("allowed", True) is not False, declared
    raise AssertionError("alice's turn never ended")


def test_threaten_shows_the_new_controller_in_every_view(client, game):
    bears = _steal(client, game, "Threaten", "Grizzly Bears")
    assert _controller_everywhere(client, game, bears) == {"alice"}


def test_threaten_offers_the_hasty_attack_to_its_new_controller(client, game):
    """CR 508.1a's declaration is asked of the active player once combat
    reaches the declare attackers step. While that question stands the
    committed board is still in the beginning of combat step, so the rows
    are built on the board the question was raised on: alice gets the
    stolen, hasty Grizzly Bears as an attacker and bob gets nothing for it."""
    bears = _steal(client, game, "Threaten", "Grizzly Bears")
    force_turn_position(game, Step.BEGINNING_OF_COMBAT, "alice")
    paused = client.post(f"/api/games/{game}/advance", json={"player": "alice"}).json()
    assert paused.get("paused") is True, paused
    assert paused["pending_decision"]["kind"] == "declare_attackers", paused
    attackers = {
        r["card_id"] for r in _rows(client, game, "alice") if r["kind"] == "declare_attacker"
    }
    assert bears in attackers, attackers
    assert not [r for r in _rows(client, game, "bob") if r.get("card_id") == bears]


def test_act_of_treason_moves_the_view_and_the_rows_then_cleanup_reverts_them(client, game):
    mother = _steal(client, game, "Act of Treason", "Mother of Runes")
    assert _controller_everywhere(client, game, mother) == {"alice"}
    assert _activate_rows_for(_rows(client, game, "alice"), mother), "haste: alice may tap it"
    assert not _activate_rows_for(_rows(client, game, "bob"), mother)

    _end_the_turn(client, game)
    assert _controller_everywhere(client, game, mother) == {"bob"}
    assert _activate_rows_for(_rows(client, game, "bob"), mother)
    assert not _activate_rows_for(_rows(client, game, "alice"), mother)


def _attack(client, game, card_ids: list[str], *, dry_run: bool = False) -> dict:
    return client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uid_for(game, "alice")},
        json={
            "attackers": [{"card_id": card_id, "defender": "bob"} for card_id in card_ids],
            "dry_run": dry_run,
            "reasoning": "test",
        },
    ).json()


def test_a_dry_run_attack_checks_the_board_the_declaration_is_answered_on(client, game):
    """While CR 508.1a's question stands, the committed board is still in the
    beginning of combat step. The real declaration is checked on the board the
    question was raised on (the replayed pause), so a dry run of the same pairs
    is answered there too: the whole declaration, not each pair alone, and
    the dry run and the real call agree - here on the same creature named
    twice (CR 508.1a chooses which creatures attack, each once)."""
    bears = _steal(client, game, "Threaten", "Grizzly Bears")
    force_turn_position(game, Step.BEGINNING_OF_COMBAT, "alice")
    paused = client.post(f"/api/games/{game}/advance", json={"player": "alice"}).json()
    assert paused["pending_decision"]["kind"] == "declare_attackers", paused

    twice = [bears, bears]
    dry_twice = _attack(client, game, twice, dry_run=True)
    real_twice = _attack(client, game, twice)
    assert real_twice["allowed"] is False, real_twice
    assert dry_twice == {**dry_twice, "allowed": False, "reason": real_twice["reason"]}, dry_twice

    dry = _attack(client, game, [bears], dry_run=True)
    assert dry["allowed"] is True, dry
    real = _attack(client, game, [bears])
    assert real["allowed"] is True, "the dry run answered nothing: the question still stood"
