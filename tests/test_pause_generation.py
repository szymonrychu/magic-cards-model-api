"""The pause generation: a token that says *which* question an answer is for.

`_pause_slot_guard` (`app.py`) makes it impossible for one seat's answer to
be applied to another seat's question - a live pause is never silently
replaced, so an answer can only ever be read against the pause it was
offered for. What it cannot do is tell a *client* that the question moved on
between the read and the answer: a resolution that asks two decisions hands
the same seat a second `pending_decision` the instant the first is accepted,
and an agent that assembled its answer from a stale read posts it against a
question it never saw.

`pause_generation` closes that: a monotonic counter per game, bumped by
`repository.store_paused_command` - so it advances exactly when the standing
question changes and not when anything else about the game does - surfaced
on every `pending_decision` view and echoed back as `DecideRequest
.generation`. A mismatch is refused synchronously, with the pause left
standing so the seat can re-read and answer the question that is actually
there. Omitting it keeps the old behaviour, so no existing client breaks.
"""

from __future__ import annotations

import json
from typing import Any

from conftest import state
from fastapi.testclient import TestClient
from test_pause_slot_clobber import crack_the_strand, seats_for, steam_vents_in

from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def pending_of(client: TestClient, game_id: str, uid: str) -> dict[str, Any]:
    view = state(client, game_id, uid=uid)["pending_decision"]
    assert view is not None, "expected a pending decision"
    return view


def test_the_pending_decision_carries_a_generation_that_advances_with_the_pause(
    client, game
) -> None:
    """One generation per standing question. Answering the search moves the
    resolution on to the shockland's `pay_life`, which is a different
    question, so the counter advances with it.
    """
    uids = seats_for(game)
    paused = crack_the_strand(client, game)
    assert isinstance(paused["pending_decision"]["generation"], int), paused

    first = pending_of(client, game, uids["alice"])
    assert first["kind"] == "choose_cards"

    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [steam_vents_in(first)]},
    )
    assert answered.status_code == 200, answered.text
    second = answered.json()["pending_decision"]
    assert second["kind"] == "pay_life"
    assert second["generation"] > first["generation"], (
        f"the standing question changed, so the generation must have: {first} -> {second}"
    )
    assert pending_of(client, game, uids["alice"])["generation"] == second["generation"]


def test_answering_a_superseded_generation_is_refused_and_the_pause_survives(client, game) -> None:
    """The stale-read case. Alice reads the search at generation N, answers
    it, and the resolution moves on to the shockland at N+1. An answer still
    carrying N is for a question that is no longer standing - refused, with
    the `pay_life` question left exactly where it was so the same seat can
    read it and answer properly.
    """
    uids = seats_for(game)
    paused = crack_the_strand(client, game)
    first = pending_of(client, game, uids["alice"])
    stale = first["generation"]

    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "card_ids": [steam_vents_in(paused["pending_decision"])],
            "generation": stale,
        },
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True, answered.json()

    refused = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": False, "card_ids": [], "generation": stale},
    )
    assert refused.status_code == 200, refused.text
    body = refused.json()
    assert body["accepted"] is False, body
    assert "generation" in body["reason"], body

    standing = pending_of(client, game, uids["alice"])
    assert standing["kind"] == "pay_life", standing
    assert standing["generation"] != stale

    # And the seat can still answer the question that is actually there.
    accepted = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "accept": False,
            "card_ids": [],
            "generation": standing["generation"],
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["accepted"] is True, accepted.json()


def test_an_answer_that_names_no_generation_is_accepted(client, game) -> None:
    """`generation` is optional. Every client that predates it - and every
    hand-written `curl` - keeps working unchanged, which is the whole reason
    it is a check the caller opts into rather than a required field."""
    uids = seats_for(game)
    paused = crack_the_strand(client, game)
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [steam_vents_in(paused["pending_decision"])]},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True, answered.json()


def test_an_idempotent_re_post_does_not_advance_the_generation(client, game) -> None:
    """The counter tracks the standing *question*, not traffic. seatkit's
    autopass re-posts its own `pass-priority` on every `paused: true` it
    reads; `_pause_slot_guard` hands that back un-re-run without touching the
    slot, so an answer a seat assembled before the re-post must still be
    accepted afterwards.
    """
    uids = seats_for(game)
    paused = crack_the_strand(client, game)
    before = pending_of(client, game, uids["alice"])

    retry = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["pending_decision"]["generation"] == before["generation"], retry.json()

    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "card_ids": [steam_vents_in(paused["pending_decision"])],
            "generation": before["generation"],
        },
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True, answered.json()


def test_the_seat_cli_passes_a_generation_through_and_a_stale_one_is_refused(
    monkeypatch, capsys
) -> None:
    """`seat_cli decide --generation`, which is the only door an AI seat has.

    Driven exactly as a player runs it: the flag is read off the seat's own
    `pending_decision`, a stale value is refused with the CLI's own
    `allowed: false` shape, and the current one goes through.
    """
    import httpx

    from magic_cards_model_api import seat_cli

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    http = TestClient(app)
    try:
        created = http.post(
            "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
        ).json()
        game_id = created["game_id"]
        uids = created["seat_uids"]
        try:
            paused = crack_the_strand(http, game_id)
            vents = steam_vents_in(paused["pending_decision"])
            generation = pending_of(http, game_id, uids["alice"])["generation"]

            base = ["--uid", uids["alice"], "--game", game_id, "decide"]
            rc = seat_cli.run([*base, vents, "--generation", str(generation - 1), "--hold"])
            assert rc == 0
            refused = json.loads(capsys.readouterr().out)
            assert refused["outcome"]["accepted"] is False, refused
            assert "generation" in refused["outcome"]["reason"], refused

            rc = seat_cli.run([*base, vents, "--generation", str(generation), "--hold"])
            assert rc == 0
            accepted = json.loads(capsys.readouterr().out)
            # A search answered from the CLI hands this seat the shockland's
            # own `pay_life` straight back, which `seat_cli` reports as the
            # self-decision wake rather than as an outcome - so the proof the
            # answer landed is the question that followed it.
            assert accepted.get("outcome") is None, accepted
            assert "pay 2 life" in accepted["wake_reason"], accepted
            assert store.paused_command(game_id) is not None, "the shockland still has to ask"
            assert pending_of(http, game_id, uids["alice"])["kind"] == "pay_life"
        finally:
            http.delete(f"/api/games/{game_id}", params={"key": created["admin_key"]})
    finally:
        http.close()
