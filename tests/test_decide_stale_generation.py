"""F2: `decide` is refused as stale by the identity of the pending decision,
not by the raw ledger seq alone.

**The race this fixes (live).** p2 reads a `choose_cards` decision at ledger
seq N. Before p2 answers, the opponent's unrelated `pass_priority` lands and
the ledger moves to N+1. p2's answer still names `expected_seq=N`, and
`records.require_current_board` refused it outright as stale - even though
the question p2 read is still the one standing, untouched by the opponent's
pass. The seat had not lost any race at all: the decision it answered
against is provably the same one, because `store.pause_generation` only
advances when the *question itself* is replaced (`store.store_paused_command`
is the only call that bumps it).

`require_current_board` now takes an optional `decision_generation`. For the
`decide` action alone, a mismatched `expected_seq` is forgiven when a pause is
still standing and its generation matches the one the seat names - the
generation is the proof that the question answered is the question read.
Every other mismatch - no generation given, a different generation, no pause
standing, or any action other than `decide` - is refused exactly as before.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, state
from fastapi.testclient import TestClient

from magic_cards_model_api import records
from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def last_seq_of(client: TestClient, game_id: str) -> int:
    response = client.get(f"/api/games/{game_id}/log")
    assert response.status_code == 200, response.text
    return response.json()["last_seq"]


def log_of(client: TestClient, game_id: str, admin_key: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/games/{game_id}/log", params={"uid": admin_key})
    assert response.status_code == 200, response.text
    return response.json()["log"]


def flooded_strand_decision(client: TestClient, game_id: str, alice: str) -> dict[str, Any]:
    """A real `choose_cards` decision (Flooded Strand's fetch, CR 701.23a),
    the exact kind `test_advance_refusals.py` already drives - paused,
    waiting on which land to find, with a real `generation`."""
    strand = add_card(client, game_id, "Flooded Strand", "alice", "battlefield")["added"][
        "card_id"
    ]
    add_card(client, game_id, "Plains", "alice", "library")
    activated = client.post(
        f"/api/games/{game_id}/actions/activate",
        json={"player": "alice", "card_id": strand},
    ).json()
    assert activated["allowed"] is True, activated
    resolved = client.post(
        f"/api/games/{game_id}/resolve-top", params={"uid": alice}
    ).json()
    assert resolved["paused"] is True, resolved
    pending = resolved["pending_decision"]
    assert pending["kind"] == "choose_cards"
    return pending


# ---------------------------------------------------------------------------
# the case this exists for: a stale expected_seq, a matching generation
# ---------------------------------------------------------------------------


def test_an_unrelated_row_after_the_read_plus_matching_generation_is_accepted_and_applied(
    client, game
) -> None:
    uids = seats_for(game)
    pending = flooded_strand_decision(client, game, uids["alice"])
    stale = last_seq_of(client, game)

    # The opponent's own row lands after p2 read the decision - unrelated to
    # the question p2 is about to answer.
    add_card(client, game, "Forest", "bob", "battlefield")
    assert last_seq_of(client, game) > stale

    decided = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "card_ids": [pending["cards"][0]],
            "expected_seq": stale,
            "generation": pending["generation"],
        },
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["accepted"] is True, body
    assert "stale" not in body

    board = state(client, game)
    assert board["stack"] == []
    assert any(c["name"] == "Plains" for c in board["battlefield"])


def test_a_new_pause_after_the_read_is_refused_as_stale_with_moved_from_and_moved_to(
    client, game
) -> None:
    """The generation moved on too, so the forgiveness does not apply: this
    seat is now genuinely answering a question that is no longer standing."""
    uids = seats_for(game)
    pending = flooded_strand_decision(client, game, uids["alice"])
    stale = last_seq_of(client, game)
    old_generation = pending["generation"]

    stored = store.paused_command(game)
    assert stored is not None
    command, tape, engine_pending = stored
    # A fresh pause replaces the standing one - the one thing that bumps
    # `pause_generation` (`repository.py`'s own guarantee).
    store.store_paused_command(game, command, tape, engine_pending)
    add_card(client, game, "Forest", "bob", "battlefield")
    assert store.pause_generation(game) != old_generation

    decided = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "card_ids": [pending["cards"][0]],
            "expected_seq": stale,
            "generation": old_generation,
        },
    )
    assert decided.status_code == 409, decided.text
    body = decided.json()
    assert body["stale"] is True
    assert body["moved_from"] == stale
    assert body["moved_to"] == last_seq_of(client, game) - 1


def test_decide_without_generation_plus_a_stale_seq_is_refused_as_before(client, game) -> None:
    uids = seats_for(game)
    pending = flooded_strand_decision(client, game, uids["alice"])
    stale = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")

    decided = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "card_ids": [pending["cards"][0]],
            "expected_seq": stale,
        },
    )
    assert decided.status_code == 409, decided.text
    body = decided.json()
    assert body["stale"] is True
    assert body["moved_from"] == stale


def test_the_answer_is_still_validated_against_the_standing_decision_not_the_staleness_path(
    client, game
) -> None:
    """A matching generation forgives the stale `expected_seq`, but the
    answer itself still has to satisfy the engine: a `card_ids` naming
    something Flooded Strand's search never offered is refused by the
    decision layer, and that refusal carries no `stale` key at all."""
    uids = seats_for(game)
    pending = flooded_strand_decision(client, game, uids["alice"])
    stale = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")

    decided = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "card_ids": ["00000000-0000-0000-0000-000000000000"],
            "expected_seq": stale,
            "generation": pending["generation"],
        },
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["accepted"] is False, body
    assert "stale" not in body
    # the pause is still standing - a bad answer never consumed the question
    assert store.paused_command(game) is not None


def test_a_stale_dry_run_decide_writes_nothing(client, game, admin_key) -> None:
    """`require_current_board`'s `dry_run` guarantee (no ledger row for a
    refused probe) holds exactly the same with `decision_generation` given,
    when that generation does not match what is standing."""
    uids = seats_for(game)
    pending = flooded_strand_decision(client, game, uids["alice"])
    stale = last_seq_of(client, game)
    add_card(client, game, "Forest", "bob", "battlefield")
    before = len(log_of(client, game, admin_key))

    raised = False
    try:
        records.require_current_board(
            game,
            "decide",
            stale,
            player="alice",
            dry_run=True,
            decision_generation=pending["generation"] + 999,
        )
    except records.BoardMoved:
        raised = True
    assert raised, "a mismatched generation must not forgive the stale seq"
    assert len(log_of(client, game, admin_key)) == before, "a dry run logged a row"
