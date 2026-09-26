"""FD5 (7): `POST /api/games/{game_id}/seats/{uid}/scripts/adhoc` - staging a
mid-game ad-hoc play script over HTTP, the surface `records.
stage_adhoc_script` (FD10) sits behind: the same credential rule `PUT
.../seats/{uid}/scripts` uses, the `SCRIPTS_ENABLED` kill switch, and the
`code: invalid_scripts` refusal shape every script route already answers
with.
"""

from __future__ import annotations

import pytest
from conftest import add_card, state
from fastapi.testclient import TestClient
from test_playscript_runtime import land_script, seat_uid

from magic_cards_model_api import records
from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def _stage(client: TestClient, game_id: str, uid: str, **body):
    return client.post(
        f"/api/games/{game_id}/seats/{uid}/scripts/adhoc",
        params={"uid": uid},
        json={
            "play": land_script("inline"),
            "reasoning": "a mana loop I am facing right now",
            **body,
        },
    )


# -- auth ---------------------------------------------------------------------


def test_a_seat_stages_its_own_script(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    response = _stage(client, game, alice)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"].startswith("adhoc-")
    assert body["warnings"] == []


def test_the_admin_key_stages_a_script_for_any_seat(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    admin_key = store.admin_key(game)
    response = client.post(
        f"/api/games/{game}/seats/{alice}/scripts/adhoc",
        params={"key": admin_key},
        json={"play": land_script("inline"), "reasoning": "admin-staged"},
    )
    assert response.status_code == 200, response.text


def test_another_seats_own_uid_cannot_stage_for_this_one(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    bob = seat_uid(game, "bob")
    response = client.post(
        f"/api/games/{game}/seats/{alice}/scripts/adhoc",
        params={"uid": bob},
        json={"play": land_script("inline"), "reasoning": "not my uid"},
    )
    assert response.status_code == 404, response.text


@pytest.mark.no_harness_credential
def test_no_credential_at_all_is_refused(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    response = client.post(
        f"/api/games/{game}/seats/{alice}/scripts/adhoc",
        json={"play": land_script("inline"), "reasoning": "no uid, no key"},
    )
    assert response.status_code == 404, response.text


def test_an_unknown_uid_is_refused(client: TestClient, game: str) -> None:
    response = client.post(
        f"/api/games/{game}/seats/not-a-real-uid/scripts/adhoc",
        params={"uid": "not-a-real-uid"},
        json={"play": land_script("inline"), "reasoning": "nobody"},
    )
    assert response.status_code == 404, response.text


# -- the kill switch ------------------------------------------------------------


def test_the_kill_switch_refuses_staging(client: TestClient, game: str, monkeypatch) -> None:
    monkeypatch.setenv(records.SCRIPTS_ENABLED_ENV, "false")
    alice = seat_uid(game, "alice")
    response = _stage(client, game, alice)
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "scripts_disabled"
    assert body["allowed"] is False


# -- a refusal with its codes ---------------------------------------------------


def test_a_malformed_play_is_refused_as_invalid_scripts(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    response = _stage(client, game, alice, play={"not": "a real play document"})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "invalid_scripts"
    assert body["allowed"] is False
    assert body["errors"]


def test_a_card_name_in_the_play_is_refused(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    doc = land_script("inline")
    doc["moves"] = [
        {"kind": "play_land", "card": {"select": {"types_all": ["Land"]}}, "name": "Forest"}
    ]
    response = _stage(client, game, alice, play=doc)
    # Either the schema's own `extra="forbid"` (`name` is not a Selector
    # field) or a card-name-ban hit refuses this - both are `code:
    # invalid_scripts`, which is the contract this test pins.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_scripts"


def test_staging_more_than_the_cap_is_refused(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    for n in range(records.MAX_ADHOC_SCRIPTS):
        response = _stage(client, game, alice, play=land_script(f"loop-{n}"))
        assert response.status_code == 200, response.text
    over_cap = _stage(client, game, alice, play=land_script("one-too-many"))
    assert over_cap.status_code == 422, over_cap.text
    assert over_cap.json()["code"] == "invalid_scripts"


# -- staged then run by id, and an inline body run, end to end -----------------


def test_a_staged_script_is_then_run_by_id(game: str, monkeypatch) -> None:
    """A staged script is run by its assigned id through the real
    play-script runner, end to end over a real `TestClient` game - staging
    and running are two separate calls here, unlike `run_script(body=...)`'s
    own stage-then-run in one."""
    import httpx

    from magic_cards_model_api.seatkit import Seats

    client = TestClient(app)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)
    add_card(client, game, "Island", "alice", "hand")
    alice = seat_uid(game, "alice")

    staged = _stage(client, game, alice)
    assert staged.status_code == 200, staged.text
    script_id = staged.json()["id"]

    seats = Seats(api="http://testserver", game_id=game)
    seat = seats.seat_for(alice)
    from magic_cards_model_api.playscripts import script_tools

    result = script_tools.run(
        seat, seats.script_corpora, script=script_id, reasoning="running the staged loop"
    )
    assert isinstance(result, dict), result
    assert result["script_exit"]["exit"] == "chain_complete", result["script_exit"]
    assert [c["name"] for c in state(client, game)["battlefield"]] == ["Island"]


def test_an_inline_body_run_through_the_mcp_tool_stages_over_this_route(
    game: str, monkeypatch
) -> None:
    """End to end through a real `TestClient`: the MCP `run_script(body=...)`
    tool stages an inline play over this exact route
    (`playscripts.script_tools.stage`) and runs the freshly assigned id in
    the same call."""
    import httpx

    from magic_cards_model_api.seatkit import Seats

    client = TestClient(app)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)
    add_card(client, game, "Island", "alice", "hand")
    alice = seat_uid(game, "alice")

    seats = Seats(api="http://testserver", game_id=game)
    seat = seats.seat_for(alice)
    from magic_cards_model_api.playscripts import script_tools

    result = script_tools.run(
        seat, seats.script_corpora, body=land_script("inline"), reasoning="a loop, run inline"
    )
    assert isinstance(result, dict), result
    assert result["script_exit"]["exit"] == "chain_complete", result["script_exit"]
    assert [c["name"] for c in state(client, game)["battlefield"]] == ["Island"]
