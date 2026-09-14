"""An as-enters choice (CR 614.12a) paused and answered on the postgres store.

Production, 2026-09-14, game 07c5f818: a land's "as it enters, choose ..."
paused on `choose_a_quality`, and every `/decide` came back 409 "replay
diverged" because the postgres codec dropped `Decision.quality`, so the stored
question never equalled the replayed one. Cavern of Souls exposed it; this
plays a different land with the same mechanic - Sunken Citadel's "As it
enters, choose a color." - through the real routes on a real
`PostgresRepository`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from conftest import add_card, find, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from postgres_store_testkit import postgres_store

from magic_cards_model_api.repository_postgres import PostgresRepository


@pytest.fixture(autouse=True)
def postgres_repo(monkeypatch: pytest.MonkeyPatch) -> Iterator[PostgresRepository]:
    with postgres_store(monkeypatch) as repo:
        yield repo


def test_a_land_paused_on_its_as_enters_color_choice_can_be_decided(
    client: TestClient, game: str, postgres_repo: PostgresRepository
) -> None:
    uids = {name: uid for uid, name in postgres_repo.seat_uids(game).items()}
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    add_card(client, game, "Sunken Citadel", "alice", "hand")
    citadel = find(state(client, game, uid=uids["alice"])["hands"]["alice"], "Sunken Citadel")

    played = client.post(
        f"/api/games/{game}/actions/play-land",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_id": citadel["id"]},
    )
    assert played.status_code == 200, played.text
    pending = played.json()["pending_decision"]
    assert pending["kind"] == "choose_a_quality", pending
    assert pending["options"], pending

    stored = postgres_repo.paused_command(game)
    assert stored is not None
    assert stored[2].decision.quality == "color"
    assert stored[2].decision.options == tuple(pending["options"])

    chosen = pending["options"][-1]
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": chosen, "generation": pending.get("generation")},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json().get("accepted") is not False, answered.json()

    after = state(client, game, uid=uids["alice"])
    assert after["pending_decision"] is None, after["pending_decision"]
    find(after["battlefield"], "Sunken Citadel")
    assert postgres_repo.paused_command(game) is None
