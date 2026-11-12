"""PATCH .../cards/{id} and .../players/{name} accept a counter of any name (CR 122.1).

CR 122.1 (looked up with `mise run rules:find 122.1` in the engine checkout):
"Counters with the same name or description are interchangeable." The rules
list a few kinds with rules of their own and leave every other name to the card
that invents it, so the engine keys counters by name (`Counters`, `Counter | str`).
The route used to refuse every name outside the engine's `Counter` enum, and the
MTGO log import (`sim_import_mtgo_log`) failed on Malcolm, Alluring Scoundrel's
chorus counter with `unknown counter kind 'chorus'`.

Cards: Malcolm, Alluring Scoundrel ("chorus") and Blademane Baku ("ki"), both
Modern-legal and neither name a member of the engine's enum.
"""

from __future__ import annotations

import pytest
from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api.engine_api import parse_counter_kind

MALCOLM = "Malcolm, Alluring Scoundrel"
BAKU = "Blademane Baku"


def staged(client: TestClient, game: str, name: str) -> str:
    add_card(client, game, name, "alice", "battlefield")
    return find(state(client, game)["battlefield"], name)["id"]


def counters_of(client: TestClient, game: str, name: str) -> dict[str, int]:
    return find(state(client, game)["battlefield"], name)["counters"]


def patch(client: TestClient, game: str, card_id: str, counters: dict[str, int]):
    return client.patch(f"/api/games/{game}/cards/{card_id}", json={"counters": counters})


def test_a_chorus_counter_is_accepted(client, game):
    malcolm = staged(client, game, MALCOLM)

    response = patch(client, game, malcolm, {"chorus": 4})

    assert response.status_code == 200, response.text
    assert counters_of(client, game, MALCOLM) == {"chorus": 4}


def test_a_ki_counter_is_accepted_beside_a_known_kind(client, game):
    baku = staged(client, game, BAKU)

    response = patch(client, game, baku, {"ki": 2, "+1/+1": 1})

    assert response.status_code == 200, response.text
    assert counters_of(client, game, BAKU) == {"ki": 2, "+1/+1": 1}


def test_setting_a_named_counter_to_zero_removes_it(client, game):
    malcolm = staged(client, game, MALCOLM)
    patch(client, game, malcolm, {"chorus": 3})

    response = patch(client, game, malcolm, {"chorus": 0})

    assert response.status_code == 200, response.text
    assert counters_of(client, game, MALCOLM) == {}


def test_the_name_is_stored_trimmed_and_lowercase_so_it_is_the_same_counter(client, game):
    malcolm = staged(client, game, MALCOLM)
    patch(client, game, malcolm, {"chorus": 1})

    response = patch(client, game, malcolm, {"  Chorus ": 3})

    assert response.status_code == 200, response.text
    assert counters_of(client, game, MALCOLM) == {"chorus": 3}


def test_a_keyword_counter_with_two_words_is_accepted(client, game):
    baku = staged(client, game, BAKU)

    response = patch(client, game, baku, {"double strike": 1})

    assert response.status_code == 200, response.text
    assert counters_of(client, game, BAKU) == {"double strike": 1}


def test_a_player_may_hold_a_named_counter(client, game):
    response = client.patch(f"/api/games/{game}/players/alice", json={"counters": {"rad": 2}})

    assert response.status_code == 200, response.text
    alice = next(p for p in state(client, game)["players"] if p["username"] == "alice")
    assert alice["counters"] == {"rad": 2}


@pytest.mark.parametrize(
    "kind",
    ["", "   ", "x" * 41, "bad\nname", "<script>", "a;b", "chorus counter!", "chörus"],
)
def test_a_name_that_cannot_be_a_counter_is_refused_with_400(client, game, kind):
    malcolm = staged(client, game, MALCOLM)

    response = patch(client, game, malcolm, {kind: 1})

    assert response.status_code == 400, response.text
    assert counters_of(client, game, MALCOLM) == {}


def test_a_negative_amount_is_still_refused(client, game):
    malcolm = staged(client, game, MALCOLM)

    response = patch(client, game, malcolm, {"chorus": -1})

    assert response.status_code == 400, response.text


def test_parse_counter_kind_keeps_the_engine_enum_for_the_names_it_lists():
    from magic_cards_model.models.card_state import Counter

    assert parse_counter_kind("loyalty") is Counter.LOYALTY
    assert parse_counter_kind("+1/+1") is Counter.PLUS_ONE
    assert parse_counter_kind("chorus") == "chorus"
