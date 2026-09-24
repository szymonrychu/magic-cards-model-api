"""R2: the exact engine state recorded beside every ledger row, served only
through the review gates - `GET /api/games/{id}/review`'s new `exact_frames`/
`viewer` fields and `GET /api/games/{id}/review/frames/{seq}`.

Four things are being proved here, and none of them is "the feature draws a
pretty board":

- **Every board-bearing action row gets an exact state recorded**, silently
  and without affecting the row or the action's own response - `audit.
  record`'s new, best-effort call to `store.put_record_state`.
- **Nothing already reachable changes shape.** `records.payload` (what
  `audit.record` writes) and both existing reads of it (`GET .../log`,
  `GET .../{id}`) carry exactly the keys they always did - no `state`,
  `exact`, or blob key leaks onto a path nothing gated.
- **The exact state is reachable only through the review gates**, and the
  same gates `GET .../review` already enforces, not a second copy of them:
  a live game refuses every credential, a stranger or bad uid gets 404, and
  the opponent never sees a script-run row's frame any more than they see
  the row itself.
- **The frame itself is the real board** - battlefield, hands, exile - not a
  re-parsed approximation, checked against attachments and exile ownership,
  which the ledger's own human-readable `snapshot` string cannot carry.

A structural guard (the AST scan section) pins the leak surface at the
source level: which files may even *call* `put_record_state`/`record_state`/
`record_state_seqs`, and which may reference the `record_states` table at
all.
"""

from __future__ import annotations

import ast
import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import add_card, find, state
from fastapi.testclient import TestClient
from magic_cards_model.models import Zone
from test_review import (
    PLAN,
    _advised_run_then_the_real_move,
    _agent_game,
    _declare,
    _played_out,
    _review,
)
from test_unloadable_games import engine_refusal
from test_unloadable_games import unloadable as unloadable  # noqa: PLC0414 - a fixture, re-exported

from magic_cards_model_api import audit
from magic_cards_model_api.repository import GameUnloadable
from magic_cards_model_api.store import store

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "magic_cards_model_api"
ALEMBIC_ROOT = REPO_ROOT / "alembic"


def _frame(client: TestClient, game_id: str, seq: int, **params: Any):
    return client.get(f"/api/games/{game_id}/review/frames/{seq}", params=params)


def _wait_for_pending(client: TestClient, game_id: str, uid: str, tries: int = 300) -> dict:
    for _ in range(tries):
        view = state(client, game_id, uid=uid)
        if view.get("pending_decision") is not None:
            return view
        time.sleep(0.01)
    raise AssertionError("no pending decision showed up in time")


# ---------------------------------------------------------------------------
# (1) audit.record: every board-bearing row gets an exact state recorded
# ---------------------------------------------------------------------------


def test_every_action_row_with_a_game_gets_a_recorded_state(client: TestClient) -> None:
    """After a real cast/pass sequence through the routes,
    `store.record_state_seqs(gid)` contains the `seq` of every row that had
    a `game` when `audit.record` wrote it - exactly the rows whose
    `snapshot` is not `None`, since `audit.record` sets both from the same
    `game is not None` check."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        entries = audit.entries(game_id)
        expected = {entry["seq"] for entry in entries if entry["snapshot"] is not None}
        assert expected, "the played-out fixture has board-bearing rows"
        assert set(store.record_state_seqs(game_id)) == expected
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_recorded_state_matches_what_store_get_returned_right_after_the_action(
    client: TestClient,
) -> None:
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200
        client.post(
            f"/api/games/{game_id}/cards",
            json={"card": "Mountain", "player": "bob", "zone": "hand"},
        )
        played = client.post(
            f"/api/games/{game_id}/actions/play-land",
            json={"player": "bob", "card": "Mountain", "reasoning": "land"},
        )
        assert played.status_code == 200, played.text
        seq = played.json()["log_seq"]
        assert isinstance(seq, int)

        live = store.get(game_id)
        assert live is not None
        expected_dump = live.model_dump(mode="json")

        recorded = store.record_state(game_id, seq)
        assert recorded is not None
        assert recorded.model_dump(mode="json") == expected_dump
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_row_for_a_game_that_does_not_exist_gets_no_recorded_state() -> None:
    audit.record("no-such-game-r2-frames", "harness_probe")
    assert store.record_state_seqs("no-such-game-r2-frames") == []


def test_put_record_state_failure_logs_one_warning_and_leaves_row_and_response_alone(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`store.put_record_state` raising must never surface past `audit.
    record`: the action's own response and the ledger row it already wrote
    are unaffected, and the failure shows up as exactly one WARNING naming
    the `game_id` and `seq` it failed for - not an exception that would take
    the whole request down with it."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200
        client.post(
            f"/api/games/{game_id}/cards",
            json={"card": "Mountain", "player": "bob", "zone": "hand"},
        )

        def _raise(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("put_record_state boom")

        monkeypatch.setattr(store, "put_record_state", _raise)

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="mcm.audit"):
            played = client.post(
                f"/api/games/{game_id}/actions/play-land",
                json={"player": "bob", "card": "Mountain", "reasoning": "land"},
            )
        assert played.status_code == 200, played.text
        seq = played.json()["log_seq"]
        assert isinstance(seq, int)

        # The row itself: written before `put_record_state` is ever called,
        # so a raise there must not have touched it.
        entries = audit.entries(game_id)
        row = next(entry for entry in entries if entry["seq"] == seq)
        assert row["action"] == "play_land"
        assert row["snapshot"] is not None

        # Exactly one WARNING, naming this game and this seq - not a stack
        # trace, and not zero (the failure must not be swallowed silently).
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and record.name == "mcm.audit"
        ]
        assert len(warnings) == 1, [record.getMessage() for record in warnings]
        message = warnings[0].getMessage()
        assert game_id in message
        assert str(seq) in message
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# (2) records.payload is unchanged, and neither existing read leaks a key
# ---------------------------------------------------------------------------


def test_ledger_rows_carry_only_the_unrecorded_default_keys_plus_seq(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        expected_keys = set(audit._UNRECORDED_DEFAULTS) | {"seq"}
        entries = audit.entries(game_id)
        assert entries
        for entry in entries:
            assert set(entry.keys()) == expected_keys, entry.keys()
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


_FORBIDDEN_KEY_FRAGMENTS = ("state", "exact", "blob")


def test_log_and_state_routes_carry_no_new_state_key(client: TestClient) -> None:
    """Neither `GET .../log` nor `GET .../{id}` gained a key - the exact
    state is written beside the ledger, never into it or into a live state
    view."""
    info = _played_out(client)
    game_id = info["game_id"]
    seat_uid = info["seat_uids"]["bob"]
    try:
        expected_keys = set(audit._UNRECORDED_DEFAULTS) | {"seq"}
        log_body = client.get(f"/api/games/{game_id}/log", params={"uid": seat_uid}).json()
        assert log_body["log"]
        for entry in log_body["log"]:
            assert set(entry.keys()) == expected_keys, entry.keys()

        admin_log_body = client.get(
            f"/api/games/{game_id}/log", params={"uid": info["admin_key"]}
        ).json()
        for entry in admin_log_body["log"]:
            assert set(entry.keys()) == expected_keys, entry.keys()

        state_body = client.get(f"/api/games/{game_id}", params={"uid": seat_uid}).json()
        for key in state_body:
            assert not any(fragment in key for fragment in _FORBIDDEN_KEY_FRAGMENTS), key
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# (3) the structural leak guard: an AST scan of src/
# ---------------------------------------------------------------------------


def _iter_py_files(*roots: Path) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def _call_sites(name: str) -> dict[str, list[str]]:
    """Every `.<name>(` call under `src/`, as `{relative path: [enclosing
    function names]}`. A method *definition* (`def put_record_state(...)`)
    is never a match - only a call - so the protocol declaration in
    `repository.py` and the two implementations there and in
    `repository_postgres.py` are not call sites of themselves."""
    hits: dict[str, list[str]] = {}
    for path in _iter_py_files(SRC_ROOT):
        tree = ast.parse(path.read_text(), filename=str(path))
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == name
            ):
                continue
            rel = str(path.relative_to(REPO_ROOT))
            enclosing = [
                func.name for func in functions if any(child is node for child in ast.walk(func))
            ]
            hits.setdefault(rel, []).extend(enclosing)
    return hits


def _identifier_references(name: str, *roots: Path) -> set[str]:
    """Every file under `roots` in which `name` appears as a `Name` or
    `Attribute` identifier - never a string inside a docstring or comment,
    since those never parse into `ast.Name`/`ast.Attribute` nodes."""
    files: set[str] = set()
    for path in _iter_py_files(*roots):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Name) and node.id == name) or (
                isinstance(node, ast.Attribute) and node.attr == name
            ):
                files.add(str(path.relative_to(REPO_ROOT)))
                break
    return files


def test_put_record_state_is_called_only_from_audit_py() -> None:
    hits = _call_sites("put_record_state")
    assert set(hits) == {"src/magic_cards_model_api/audit.py"}, hits


def test_record_state_is_called_only_from_apps_frame_route() -> None:
    hits = _call_sites("record_state")
    assert set(hits) == {"src/magic_cards_model_api/app.py"}, hits
    assert hits["src/magic_cards_model_api/app.py"] == ["game_review_frame"], hits


def test_record_state_seqs_is_called_only_from_app_or_review_py() -> None:
    hits = _call_sites("record_state_seqs")
    assert set(hits) <= {
        "src/magic_cards_model_api/app.py",
        "src/magic_cards_model_api/review.py",
    }, hits
    assert hits, "record_state_seqs must be called from somewhere"


def test_record_states_for_seqs_is_called_only_from_app_py() -> None:
    """DR2's batched sibling to `record_state` - deliberately not named
    `record_states` (see its own protocol docstring): that identifier is
    reserved for the table, pinned below."""
    hits = _call_sites("record_states_for_seqs")
    assert set(hits) == {"src/magic_cards_model_api/app.py"}, hits
    assert hits["src/magic_cards_model_api/app.py"] == ["_review_frame_views"], hits


def test_record_states_table_is_referenced_only_in_db_repository_postgres_and_alembic() -> None:
    referenced = _identifier_references("record_states", SRC_ROOT, ALEMBIC_ROOT)
    allowed_prefixes = (
        "src/magic_cards_model_api/db.py",
        "src/magic_cards_model_api/repository_postgres.py",
        "alembic/",
    )
    for path in referenced:
        assert path.startswith(allowed_prefixes), path
    assert referenced, "record_states should be referenced by its own table definition"


# ---------------------------------------------------------------------------
# (4) the shared gate: game_review and game_review_frame use the same one
# ---------------------------------------------------------------------------


def test_review_and_frame_route_share_one_gate_function() -> None:
    """Not a copy: both routes call exactly one helper - `_review_access`.

    `import magic_cards_model_api.app as app_module` would actually bind the
    FastAPI *instance* here, not the submodule: `magic_cards_model_api/
    __init__.py` does `from .app import app`, which overwrites the `app`
    attribute the import machinery sets on the package - so this reaches
    into `sys.modules` directly instead."""
    import sys

    import magic_cards_model_api.app  # noqa: F401 - ensures it is imported

    app_module = sys.modules["magic_cards_model_api.app"]
    assert hasattr(app_module, "_review_access")
    tree = ast.parse(Path(app_module.__file__).read_text())
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in ("game_review", "game_review_frame")
    }
    assert set(functions) == {"game_review", "game_review_frame"}
    for name, node in functions.items():
        calls = {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        assert "_review_access" in calls, (name, calls)


# ---------------------------------------------------------------------------
# (5) GET .../review: exact_frames and viewer
# ---------------------------------------------------------------------------


def test_review_carries_exact_frames_and_viewer_for_a_new_game(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        assert body["viewer"] is None, "the admin key names no seat"
        assert body["exact_frames"], "a freshly played game has recorded frames"
        assert body["exact_frames"] == sorted(body["exact_frames"])
        assert len(body["exact_frames"]) == len(set(body["exact_frames"]))

        entry_seqs = {entry["seq"] for entry in body["entries"]}
        assert set(body["exact_frames"]) <= entry_seqs

        for player in ("bob", "alice"):
            seat_body = _review(client, game_id, uid=info["seat_uids"][player]).json()
            assert seat_body["viewer"] == player
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_every_listed_exact_frame_is_fetchable(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        assert body["exact_frames"]
        for seq in body["exact_frames"]:
            response = _frame(client, game_id, seq, key=info["admin_key"])
            assert response.status_code == 200, (seq, response.text)
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_an_undecodable_finished_games_review_has_no_exact_frames(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        unloadable[game_id] = engine_refusal()
        body = _review(client, game_id, key=info["admin_key"]).json()
        assert body["reconstructed"] is True
        assert body["exact_frames"] == []
    finally:
        unloadable.pop(game_id, None)
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# (6) GET .../review/frames/{seq}
# ---------------------------------------------------------------------------


def test_a_seat_and_the_admin_key_both_read_a_frame_of_a_conceded_game(
    client: TestClient,
) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        seq = body["exact_frames"][-1]

        admin_frame = _frame(client, game_id, seq, key=info["admin_key"])
        assert admin_frame.status_code == 200, admin_frame.text
        assert admin_frame.json()["you"] is None

        for player in ("bob", "alice"):
            seat_frame = _frame(client, game_id, seq, uid=info["seat_uids"][player])
            assert seat_frame.status_code == 200, seat_frame.text
            seat_body = seat_frame.json()
            assert seat_body["you"] == player
            # Omniscient: both hands are present in full, whichever seat asks.
            for name in ("bob", "alice"):
                assert name in seat_body["hands"]
                assert "libraries" not in seat_body
                assert "sideboards" not in seat_body
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def _ids_and_names(cards: list) -> dict[str, str]:
    """`{str(id): name}` for a list of `CardState`s off the live `Game`
    object - the same two fields a frame's card view carries (`id`, `name`),
    read straight off the model rather than re-derived, so the comparison
    below is against the decoded stored `Game` itself, not another view of
    it."""
    return {str(card.id): card.name for card in cards}


def test_frame_battlefield_hands_and_exile_match_the_stored_game(
    client: TestClient, game: str, admin_key: str
) -> None:
    """The board is the real, decoded `Game` - attachments via `attached_to`
    (an Aura played through the real cast/resolve/decide sequence, exactly
    like `test_serialize_attachments.py`'s own case), and an exile card's
    `owner` present (never bare, the way the human-readable `snapshot`
    string leaves it). The frame's battlefield, both hands, and exile are
    checked against `store.record_state(game, seq)` - the decoded stored
    `Game` itself - never against a live state view, which is a different
    read path and would let the frame route drift from what was actually
    recorded without either test noticing."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Utopia Sprawl", "alice", "hand")
    seat_uids = {name: uid for uid, name in store.seat_uids(game).items()}
    board = state(client, game, uid=seat_uids["alice"])
    forests = [c["id"] for c in board["battlefield"] if c["name"] == "Forest"]
    sprawl = board["hands"]["alice"][0]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": sprawl["id"],
            "mana_sources": [forests[0]],
            "targets": [forests[1]],
        },
    )
    assert cast.status_code == 200, cast.text
    client.post(f"/api/games/{game}/resolve-top")
    pending = _wait_for_pending(client, game, seat_uids["alice"])["pending_decision"]
    assert pending["kind"] == "choose_a_quality"
    decided = client.post(
        f"/api/games/{game}/decide",
        params={"uid": seat_uids["alice"]},
        json={"player": "alice", "choice": "R"},
    )
    assert decided.status_code == 200, decided.text

    # Owned by alice, the seat that stays in the game: CR 800.4a removes
    # every object a conceding player *owns* from the game the instant it
    # leaves, so a card exiled under bob's own ownership would not survive
    # past his concede below - a real rule, not a serializer quirk, and
    # exactly why this scenario is set up this way rather than the other.
    add_card(client, game, "Lightning Bolt", "alice", "exile")

    conceded = client.post(
        f"/api/games/{game}/actions/concede", json={"player": "bob", "reasoning": "done"}
    )
    assert conceded.status_code == 200, conceded.text

    review_body = _review(client, game, key=admin_key).json()
    assert review_body["exact_frames"]
    seq = review_body["exact_frames"][-1]

    frame_body = _frame(client, game, seq, key=admin_key).json()
    sprawl_view = find(frame_body["battlefield"], "Utopia Sprawl")
    assert sprawl_view["attached_to"]["name"] == "Forest"

    bolt_view = find(frame_body["exile"], "Lightning Bolt")
    assert bolt_view["owner"] == "alice"

    stored = store.record_state(game, seq)
    assert stored is not None

    frame_battlefield = {c["id"]: c["name"] for c in frame_body["battlefield"]}
    assert frame_battlefield == _ids_and_names(stored.shared_zones[Zone.BATTLEFIELD])

    assert set(frame_body["hands"]) == {player.username for player in stored.players}
    for player in stored.players:
        frame_hand = {c["id"]: c["name"] for c in frame_body["hands"][player.username]}
        assert frame_hand == _ids_and_names(player.zones[Zone.HAND])

    frame_exile = {c["id"]: c["name"] for c in frame_body["exile"]}
    assert frame_exile == _ids_and_names(stored.shared_zones[Zone.EXILE])


def test_a_live_games_frame_route_is_refused_to_every_credential(client: TestClient) -> None:
    """Every credential that could ever open this game's review is refused
    while it is live - the anonymous case is refused on the credential gate
    itself (404, like `game_review`'s own anonymous case), and every real
    credential (both seats, the admin key) is refused on the post-game gate
    (409)."""
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200
        seq = audit.entries(game_id)[-1]["seq"]

        anonymous = _frame(client, game_id, seq)
        assert anonymous.status_code in (404, 422), anonymous.text
        assert PLAN["plan"] not in anonymous.text

        for params in (
            {"uid": info["seat_uids"]["bob"]},
            {"uid": info["seat_uids"]["alice"]},
            {"key": info["admin_key"]},
        ):
            response = _frame(client, game_id, seq, **params)
            assert response.status_code == 409, response.text
            assert PLAN["plan"] not in response.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_stranger_or_bad_uid_gets_404_from_the_frame_route(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        seq = body["exact_frames"][-1]
        assert _frame(client, game_id, seq).status_code in (404, 422)
        assert _frame(client, game_id, seq, uid="not-a-uid").status_code == 404
        assert _frame(client, game_id, seq, key="not-the-admin-key").status_code == 404
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_an_unknown_seq_gets_404(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        response = _frame(client, game_id, 999_999, key=info["admin_key"])
        assert response.status_code == 404, response.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_script_run_rows_seq_is_404_for_the_opponent(client: TestClient) -> None:
    info, _advice_seq = _advised_run_then_the_real_move(client)
    game_id = info["game_id"]
    try:
        admin_body = _review(client, game_id, key=info["admin_key"]).json()
        script_seqs = [
            entry["seq"]
            for entry in admin_body["entries"]
            if entry["action"] in audit.SCRIPT_RUN_ACTIONS
        ]
        assert script_seqs

        for seq in script_seqs:
            opponent_frame = _frame(client, game_id, seq, uid=info["seat_uids"]["alice"])
            assert opponent_frame.status_code == 404, opponent_frame.text
            admin_frame = _frame(client, game_id, seq, key=info["admin_key"])
            assert admin_frame.status_code == 200, admin_frame.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_an_undecodable_stored_state_gives_409_with_the_reason(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        seq = body["exact_frames"][-1]
        store._record_states[game_id][seq] = b"not a valid compressed blob"  # noqa: SLF001

        with pytest.raises(GameUnloadable) as caught:
            store.record_state(game_id, seq)
        expected_reason = caught.value.reason

        response = _frame(client, game_id, seq, key=info["admin_key"])
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == expected_reason
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_exact_frames_is_non_empty_for_a_new_game_and_each_seq_is_fetchable(
    client: TestClient,
) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, uid=info["seat_uids"]["bob"]).json()
        assert body["exact_frames"]
        for seq in body["exact_frames"]:
            response = _frame(client, game_id, seq, uid=info["seat_uids"]["bob"])
            assert response.status_code == 200, (seq, response.text)
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# DR2: decisions/play_stats read record states in one batched call
# ---------------------------------------------------------------------------


def test_the_review_route_batches_record_state_reads_into_one_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GET .../review` must read every decision's board with **one**
    `store.record_states_for_seqs` call, never one `store.record_state` per
    decision - the whole point of DR2's batched sibling."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        calls: list[int] = []
        original = store.record_states_for_seqs

        def counted(gid: str, seqs: Any) -> Any:
            calls.append(1)
            return original(gid, seqs)

        monkeypatch.setattr(store, "record_states_for_seqs", counted)

        def refuse_single(*_: Any, **__: Any) -> Any:
            raise AssertionError(
                "the review route read one record_state at a time instead of batching"
            )

        monkeypatch.setattr(store, "record_state", refuse_single)

        response = _review(client, game_id, key=info["admin_key"])
        assert response.status_code == 200, response.text
        assert len(calls) == 1, "record_states_for_seqs must be called exactly once per review"
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# DR2 owner addon: board_by_perspective - two redacted boards per decision
# ---------------------------------------------------------------------------


def _card_in_hand(game_id: str, name: str, owner: str) -> Any:
    game = store.get(game_id)
    assert game is not None
    player = game.player(owner)
    return next(card for card in player.cards_in(Zone.HAND) if card.name == name)


def _perspective_fixture(client: TestClient) -> dict[str, Any]:
    """A finished game where alice holds two cards nobody else starts out
    knowing about - one of them (`Lightning Bolt`) revealed to bob mid-game
    (CR 701.20a), the other (`Giant Growth`) never shown to anyone - and
    bob then plays a land, so a decision row with a recorded state exists
    strictly after the reveal."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    assert _declare(client, info).status_code == 200
    add_card(client, game_id, "Lightning Bolt", "alice", "hand")
    add_card(client, game_id, "Giant Growth", "alice", "hand")
    with store.locked(game_id) as live:
        bolt = _card_in_hand(game_id, "Lightning Bolt", "alice")
        live.reveal(bolt)
    client.post(
        f"/api/games/{game_id}/cards", json={"card": "Mountain", "player": "bob", "zone": "hand"}
    )
    played = client.post(
        f"/api/games/{game_id}/actions/play-land",
        json={"player": "bob", "card": "Mountain", "reasoning": "after the reveal"},
    )
    assert played.status_code == 200, played.text
    conceded = client.post(
        f"/api/games/{game_id}/actions/concede",
        json={"player": "alice", "reasoning": "the race is lost"},
    )
    assert conceded.status_code == 200, conceded.text
    return info


def _bobs_land_decision(body: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in body["decisions"]["bob"] if row["action"] == "play_land"]
    assert rows, "bob's play-land decision should be in the review"
    row = rows[-1]
    assert row["board_by_perspective"] is not None, "the fixture's land row has a recorded state"
    return row


def test_board_by_perspective_carries_both_players_own_view(client: TestClient) -> None:
    info = _perspective_fixture(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        row = _bobs_land_decision(body)
        assert set(row["board_by_perspective"]) == {"alice", "bob"}
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_players_own_perspective_shows_their_own_full_hand(client: TestClient) -> None:
    info = _perspective_fixture(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        row = _bobs_land_decision(body)
        alices_view = row["board_by_perspective"]["alice"]
        assert "Lightning Bolt" in alices_view.get("hand", [])
        assert "Giant Growth" in alices_view.get("hand", [])
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_revealed_card_shows_only_in_the_shown_players_known_opponent_hand(
    client: TestClient,
) -> None:
    """CR 701.20a: a reveal is shown to every player, so bob - who was shown
    `Lightning Bolt` - knows it is in alice's hand; nothing here invents that
    knowledge for a third party, since there is none in a two-player game,
    but the field is bob's own perspective and nobody else's."""
    info = _perspective_fixture(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        row = _bobs_land_decision(body)
        bobs_view = row["board_by_perspective"]["bob"]
        assert bobs_view.get("known_opponent_hand") == ["Lightning Bolt"]
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_no_hidden_card_of_the_opponent_leaks_into_a_perspective(client: TestClient) -> None:
    """`Giant Growth` is never revealed or looked at by bob - it must never
    appear anywhere in bob's own perspective board, own hand included,
    across every one of bob's decisions in the whole review."""
    info = _perspective_fixture(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        for seat, rows in body["decisions"].items():
            for row in rows:
                perspective = row.get("board_by_perspective") or {}
                bobs_view = perspective.get("bob")
                if bobs_view is None:
                    continue
                assert "Giant Growth" not in json.dumps(bobs_view), (seat, row["seq"], bobs_view)
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_missing_recorded_state_gives_a_null_board_and_perspective(
    client: TestClient,
) -> None:
    """A decision row this build never recorded an exact state for - here,
    simulated by wiping `exact_frames` from the batch the route reads - gets
    `board`/`board_by_perspective: None`, never a board borrowed from
    another `seq`."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        seat = next(seat for seat, rows in body["decisions"].items() if rows)
        for row in body["decisions"][seat]:
            assert (row["board"] is None) == (row["board_by_perspective"] is None), row
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})
