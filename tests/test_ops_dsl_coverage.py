"""FD5 (6): `GET /api/ops/dsl-coverage` - per-game and aggregated DSL
coverage across every game, behind an env-configured `OPS_TOKEN` rather than
any per-game admin key."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_review import _eager_frame_views, _played_out

from magic_cards_model_api import audit, records, review, visibility
from magic_cards_model_api.main import app
from magic_cards_model_api.playscripts import decision_dsl
from magic_cards_model_api.store import store

app_module = importlib.import_module("magic_cards_model_api.app")


def test_ops_dsl_coverage_refuses_without_the_configured_token(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv(records.OPS_TOKEN_ENV, "correct-token")
    response = client.get("/api/ops/dsl-coverage")
    assert response.status_code == 404, response.text

    wrong = client.get("/api/ops/dsl-coverage", params={"key": "wrong-token"})
    assert wrong.status_code == 404, wrong.text


def test_ops_dsl_coverage_refuses_every_caller_when_unconfigured(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.delenv(records.OPS_TOKEN_ENV, raising=False)
    response = client.get("/api/ops/dsl-coverage", params={"key": "anything"})
    assert response.status_code == 404, response.text


def test_ops_dsl_coverage_reads_a_finished_game_with_no_per_game_admin_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv(records.OPS_TOKEN_ENV, "the-real-token")
    client = TestClient(app)
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        # The route reads oldest-first and stops at `limit` (default 200); in a
        # shared test store other tests' games come first, so read them all.
        every_game = len(store.ids_since(None))
        response = client.get(
            "/api/ops/dsl-coverage", params={"key": "the-real-token", "limit": every_game}
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["since"] is None
        game_row = next(row for row in body["games"] if row["game_id"] == game_id)
        # `play_land` is a real, expressible move in this pool - it must show
        # up both per-game and in the folded aggregate.
        assert "play_land" in game_row["dsl_coverage"], game_row
        assert "play_land" in body["aggregated"], body["aggregated"]
        assert body["aggregated"]["play_land"]["count"] >= 1
    finally:
        store.delete(game_id)


def test_ops_dsl_coverage_since_filters_by_creation_time(monkeypatch) -> None:
    monkeypatch.setenv(records.OPS_TOKEN_ENV, "the-real-token")
    client = TestClient(app)
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        future = "2999-01-01T00:00:00+00:00"
        response = client.get(
            "/api/ops/dsl-coverage", params={"key": "the-real-token", "since": future}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert all(row["game_id"] != game_id for row in body["games"])
    finally:
        store.delete(game_id)


def test_ops_dsl_coverage_skips_a_game_still_in_progress(monkeypatch, client, game) -> None:
    monkeypatch.setenv(records.OPS_TOKEN_ENV, "the-real-token")
    response = client.get("/api/ops/dsl-coverage", params={"key": "the-real-token"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert all(row["game_id"] != game for row in body["games"])


# ---------------------------------------------------------------------------
# OM1: coverage reads no board and decodes only the states it converts with
# ---------------------------------------------------------------------------


def _eager_ops_game_review(game_id: str) -> dict[str, Any] | None:
    """The pre-OM1 `app._ops_game_review`, kept as the reference: every
    recorded state decoded up front and every board built, exactly as
    `GET .../review` builds them for the admin."""
    archived = store.archived(game_id)
    if archived is None:
        return None
    entries = app_module._gate_entries(archived)
    if not app_module._post_game(archived, entries):
        return None
    ledger = entries if entries is not None else audit.entries(game_id)
    ledger = [entry for entry in ledger if not visibility.hides_script_run(entry, visibility.ADMIN)]
    ledger = [audit.normalize_problem_category(entry) for entry in ledger]
    exact_frames: list[int] = []
    if archived.game is not None:
        ledger_seqs = {entry["seq"] for entry in ledger}
        exact_frames = sorted(seq for seq in store.record_state_seqs(game_id) if seq in ledger_seqs)
    return review.build_review(
        archived,
        ledger,
        exact_frames=exact_frames,
        viewer=None,
        frame_views=_eager_frame_views(game_id, exact_frames),
        corpora=app_module._review_corpora(game_id, archived, list(archived.seat_uids.values())),
    )


def _coverage(client: TestClient) -> Any:
    response = client.get(
        "/api/ops/dsl-coverage",
        params={"key": "the-real-token", "limit": len(store.ids_since(None))},
    )
    assert response.status_code == 200, response.text
    return response


def test_ops_dsl_coverage_is_byte_identical_to_the_eager_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(records.OPS_TOKEN_ENV, "the-real-token")
    client = TestClient(app)
    info = _played_out(client)
    try:
        lean = _coverage(client)
        assert any(row["game_id"] == info["game_id"] for row in lean.json()["games"])
        monkeypatch.setattr(app_module, "_ops_game_review", _eager_ops_game_review)
        eager = _coverage(client)
        assert lean.content == eager.content
    finally:
        store.delete(info["game_id"])


def test_ops_dsl_coverage_builds_no_board_and_decodes_only_converted_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage reads a decision's `action`/`unexpressible` alone. Neither
    needs a `board` or a `board_by_perspective`, and the only recorded
    states a conversion reads are a committing row's own and the one it was
    taken against - a declaration's state, read by nothing, stays encoded."""
    monkeypatch.setenv(records.OPS_TOKEN_ENV, "the-real-token")
    client = TestClient(app)
    info = _played_out(client)
    game_id = info["game_id"]
    try:

        def no_board(*_: Any, **__: Any) -> Any:
            raise AssertionError("dsl-coverage built a board it never reports")

        monkeypatch.setattr(decision_dsl, "compact_board", no_board)
        monkeypatch.setattr(review, "_board_by_perspective", no_board)
        requested: dict[str, set[int]] = {}
        original = store.record_states_for_seqs

        def recorded(gid: str, seqs: Any) -> Any:
            wanted = set(seqs)
            requested.setdefault(gid, set()).update(wanted)
            return original(gid, wanted)

        monkeypatch.setattr(store, "record_states_for_seqs", recorded)

        _coverage(client)

        entries = audit.entries(game_id)
        by_action = {entry["action"]: entry["seq"] for entry in entries}
        # play_land converts against add_card's frame (its pre_action_frame)
        # and reads its own; concede reads its own and play_land's. The
        # create_game, plan_declared and game_over states are read by nothing.
        assert requested[game_id] == {
            by_action["add_card"],
            by_action["play_land"],
            by_action["concede"],
        }
        assert requested[game_id] < set(store.record_state_seqs(game_id))
    finally:
        store.delete(game_id)


def _row(seq: int, action: str, **payload: Any) -> dict[str, Any]:
    return {"seq": seq, "action": action, **payload}


def test_frame_seqs_read_follows_the_conversion_pointers() -> None:
    bobs_land = {"detail": {"player": "bob", "allowed": True}}
    carried = [_row(1, "add_card"), _row(2, "add_card"), _row(3, "play_land", **bobs_land)]
    # No pointer: the nearest earlier frame, and the row's own.
    assert decision_dsl.frame_seqs_read(carried, ["bob"], {1, 2, 3}) == {2, 3}
    # That frame undecodable: the one before it instead.
    assert decision_dsl.frame_seqs_read(carried, ["bob"], {1, 3}) == {1, 3}
    # A named pre-action frame wins over the nearest one.
    pointed = [*carried[:2], _row(3, "play_land", pre_action_frame=1, **bobs_land)]
    assert decision_dsl.frame_seqs_read(pointed, ["bob"], {1, 2, 3}) == {1, 3}
    # A row carrying its own pre-action snapshot reads only its own frame.
    snap = [*carried[:2], _row(3, "play_land", pre_state={"battlefield": []}, **bobs_land)]
    assert decision_dsl.frame_seqs_read(snap, ["bob"], {1, 2, 3}) == {3}
    # A refused action, or another seat's, converts nothing.
    refused = [*carried[:2], _row(3, "play_land", detail={"player": "bob", "allowed": False})]
    assert decision_dsl.frame_seqs_read(refused, ["bob"], {1, 2, 3}) == set()
    assert decision_dsl.frame_seqs_read(carried, ["alice"], {1, 2, 3}) == set()


def test_coverage_frames_fall_back_past_an_undecodable_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A needed frame that will not decode is dropped and the frame before
    it read instead - the one `decision_rows` then converts against, as it
    would with every frame attached."""
    info = _played_out(client)
    game_id = info["game_id"]
    archived = store.archived(game_id)
    ledger = [
        _row(1, "add_card"),
        _row(2, "add_card"),
        _row(3, "play_land", detail={"player": "bob", "allowed": True}),
    ]
    streamed = store.record_states_for_seqs

    def seq_2_undecodable(gid: str, seqs: Any) -> Any:
        return ((seq, game) for seq, game in streamed(gid, seqs) if seq != 2)

    monkeypatch.setattr(store, "record_states_for_seqs", seq_2_undecodable)
    views = app_module._coverage_frame_views(game_id, archived, ledger, [1, 2, 3])
    assert set(views) == {1, 3}
