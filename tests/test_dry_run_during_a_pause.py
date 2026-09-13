"""A dry run gives the verdict the real call gives, pause or no pause
(battle 6, B6-09).

While alice was being asked Mystical Dispute's "unless its controller pays
{3}" (a `pay_mana` decision), `POST .../actions/activate` on her untapped land
was refused - `commands.DecisionInProgressError`, CR 608.2g - and the same
body with `dry_run: true` answered `allowed: true`. A seat asking "could I?"
was told yes about a move it could not make.

The dry run used to walk past the stored pause entirely, because it must not
touch the pause slot. It still does not touch it; it now asks the same
question of it that the real call does.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient
from test_decision_kinds import resolve_top, stage_mystical_dispute_targeting_bears

from magic_cards_model_api.store import store


def pause_on_pay_mana(client: TestClient, game: str) -> str:
    """alice asked to pay {3} for her Grizzly Bears, with an untapped Mountain
    she has not used; returns the Mountain's id."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    mountain = find(state(client, game)["battlefield"], "Mountain")["id"]
    stage_mystical_dispute_targeting_bears(client, game, {})
    paused = resolve_top(game)
    assert paused["paused"] is True, paused
    assert paused["pending_decision"]["kind"] == "pay_mana"
    return mountain


def post(client: TestClient, game: str, route: str, body: dict[str, Any]) -> tuple[int, Any]:
    response = client.post(f"/api/games/{game}/actions/{route}", json=body)
    return response.status_code, response.json()


def test_a_mana_ability_dry_run_during_pay_mana_is_refused_like_the_real_call(client, game):
    mountain = pause_on_pay_mana(client, game)
    body = {"player": "alice", "card_id": mountain, "ability_index": 0}
    standing = store.paused_command(game)

    dry_status, dry = post(client, game, "activate", {**body, "dry_run": True})
    real_status, real = post(client, game, "activate", body)

    assert (dry_status, dry["allowed"], dry["reason"]) == (
        real_status,
        real["allowed"],
        real["reason"],
    ), (dry, real)
    assert real["allowed"] is False
    # The dry run left the pause exactly where it was.
    assert store.paused_command(game) == standing
    assert find(state(client, game)["battlefield"], "Mountain")["tapped"] is False


def test_a_cast_dry_run_during_a_pause_is_refused_like_the_real_call(client, game):
    mountain = pause_on_pay_mana(client, game)
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")["id"]
    body = {"player": "alice", "card_id": bolt, "mana_sources": [mountain], "targets": ["bob"]}

    dry_status, dry = post(client, game, "cast", {**body, "dry_run": True})
    real_status, real = post(client, game, "cast", body)

    assert (dry_status, dry["allowed"], dry["reason"]) == (
        real_status,
        real["allowed"],
        real["reason"],
    ), (dry, real)
    assert real["allowed"] is False
