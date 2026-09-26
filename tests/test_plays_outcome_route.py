"""`POST /api/games/{id}/plays-outcome` (D2a): the driver's per-game record
of what its handover did to the DSL - what it admitted, proposed, or said
needs no change and why, and whether the handover finished.

Gated like `POST .../ai-review`: against `store.archived` (so a game already
ended still takes one), post-game only, and the uid binds to the one player
it may speak for - an AI seat's own uid for that seat, or an advisor uid for
the human it watches. The binding is enforced twice: by `game_access`'s
own-player table before the body is parsed, and by the route itself.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from test_review import _agent_game, _played_out, _review

from magic_cards_model_api import audit, game_access
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.store import store

ROUTE = "/api/games/{game_id}/plays-outcome"


def _body(info: dict[str, Any], player: str = "bob", **overrides: Any) -> dict[str, Any]:
    return {
        "uid": info["seat_uids"][player],
        "player": player,
        "role": "seat",
        "state": "complete",
        "admitted": [{"sha": "a" * 40, "dsl_change": True}],
        "proposed": [{"sha": "b" * 40}, {"result": "conflict"}],
        "no_change": [{"candidate": "fetch-surveil-land", "reason": "declined once only"}],
        "unfinished_reasons": [],
        "repushed": False,
        "followup": False,
        **overrides,
    }


def _post(client: TestClient, game_id: str, body: dict[str, Any], **params: Any):
    return client.post(ROUTE.format(game_id=game_id), json=body, params=params)


def _outcomes(client: TestClient, info: dict[str, Any]) -> list[dict[str, Any]]:
    entries = _review(client, info["game_id"], key=info["admin_key"]).json()["entries"]
    return [e for e in entries if e["action"] == audit.PLAYS_OUTCOME_ACTION]


def _end(client: TestClient, info: dict[str, Any]) -> None:
    client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_the_action_is_named_plays_outcome() -> None:
    assert audit.PLAYS_OUTCOME_ACTION == "plays_outcome"


def test_an_admitted_entry_carries_its_added_updated_reprioritized_ids(
    client: TestClient,
) -> None:
    """DR2: `PlaysOutcomeAdmitted` gains three optional play-id lists - the
    same shape `admit._Landing` already classifies one admit's own git diff
    into."""
    info = _played_out(client)
    try:
        body = _body(
            info,
            admitted=[
                {
                    "sha": "a" * 40,
                    "dsl_change": True,
                    "added": ["new-play"],
                    "updated": ["changed-play"],
                    "reprioritized": ["bumped-play"],
                }
            ],
        )
        response = _post(client, info["game_id"], body)
        assert response.status_code == 200, response.text
        [row] = _outcomes(client, info)
        [admitted] = row["detail"]["admitted"]
        assert admitted["added"] == ["new-play"]
        assert admitted["updated"] == ["changed-play"]
        assert admitted["reprioritized"] == ["bumped-play"]
    finally:
        _end(client, info)


def test_a_legacy_admitted_body_with_no_id_lists_still_validates(client: TestClient) -> None:
    """A body posted before this field existed - `admitted` entries carrying
    only `sha`/`dsl_change` - still validates, and reads back with the three
    lists present and empty rather than missing."""
    info = _played_out(client)
    try:
        response = _post(client, info["game_id"], _body(info))
        assert response.status_code == 200, response.text
        [row] = _outcomes(client, info)
        [admitted] = row["detail"]["admitted"]
        assert admitted["added"] == []
        assert admitted["updated"] == []
        assert admitted["reprioritized"] == []
    finally:
        _end(client, info)


def test_an_ai_seats_outcome_is_recorded(client: TestClient) -> None:
    info = _played_out(client)
    try:
        response = _post(client, info["game_id"], _body(info))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["recorded"] is True and body["player"] == "bob"
        assert isinstance(body["log_seq"], int)

        [row] = _outcomes(client, info)
        detail = row["detail"]
        assert detail["player"] == "bob"
        assert detail["role"] == "seat"
        assert detail["state"] == "complete"
        assert detail["admitted"] == [
            {
                "sha": "a" * 40,
                "dsl_change": True,
                "added": [],
                "updated": [],
                "reprioritized": [],
            }
        ]
        assert detail["proposed"] == [
            {"sha": "b" * 40, "result": None},
            {"sha": None, "result": "conflict"},
        ]
        assert detail["no_change"] == [
            {"candidate": "fetch-surveil-land", "reason": "declined once only"}
        ]
        assert detail["unfinished_reasons"] == []
        assert (detail["repushed"], detail["followup"]) == (False, False)
        assert "uid" not in detail
    finally:
        _end(client, info)


def test_an_unfinished_outcome_carries_its_reasons(client: TestClient) -> None:
    info = _played_out(client)
    try:
        body = _body(
            info,
            state="unfinished",
            admitted=[],
            unfinished_reasons=["nudged", "admit conflict"],
            repushed=True,
        )
        assert _post(client, info["game_id"], body).status_code == 200
        [row] = _outcomes(client, info)
        assert row["detail"]["state"] == "unfinished"
        assert row["detail"]["unfinished_reasons"] == ["nudged", "admit conflict"]
        assert row["detail"]["repushed"] is True
    finally:
        _end(client, info)


def test_a_human_seat_is_refused(client: TestClient) -> None:
    """alice is a human seat: her own uid opens her own seat, but only an AI
    seat's driver ever runs a handover."""
    info = _played_out(client)
    try:
        response = _post(client, info["game_id"], _body(info, player="alice"))
        assert response.status_code == 404, response.text
        assert _outcomes(client, info) == []
    finally:
        _end(client, info)


def test_another_seats_uid_is_refused_by_the_binding_gate(client: TestClient) -> None:
    """alice's uid naming bob: `game_access`'s own-player table refuses it
    before the route runs, and nothing is recorded."""
    info = _played_out(client)
    try:
        response = _post(
            client, info["game_id"], _body(info, player="bob", uid=info["seat_uids"]["alice"])
        )
        assert response.status_code == 403, response.text
        assert "seat" in response.json()["detail"]
        # The same uid in the query string is refused the same way.
        response = client.post(
            ROUTE.format(game_id=info["game_id"]),
            params={"uid": info["seat_uids"]["alice"]},
            json=_body(info, player="bob", uid=info["seat_uids"]["alice"]),
        )
        assert response.status_code == 403, response.text
        assert _outcomes(client, info) == []
    finally:
        _end(client, info)


def test_a_uid_that_is_nobodys_is_refused(client: TestClient) -> None:
    info = _played_out(client)
    try:
        response = _post(client, info["game_id"], _body(info, uid="not-a-uid"))
        assert response.status_code == 404, response.text
    finally:
        _end(client, info)


def test_a_live_game_is_refused(client: TestClient) -> None:
    info = _agent_game(client)
    try:
        response = _post(client, info["game_id"], _body(info))
        assert response.status_code == 409, response.text
        assert "in progress" in response.json()["detail"]
    finally:
        _end(client, info)


def test_an_ended_game_still_takes_the_seats_outcome(client: TestClient) -> None:
    info = _played_out(client)
    _end(client, info)
    response = _post(client, info["game_id"], _body(info))
    assert response.status_code == 200, response.text
    assert len(_outcomes(client, info)) == 1


def test_a_game_that_never_existed_is_a_404(client: TestClient) -> None:
    response = client.post(
        "/api/games/no-such-game/plays-outcome",
        json={"uid": "x", "player": "bob", "role": "seat", "state": "complete"},
    )
    assert response.status_code == 404


def test_the_wrong_role_for_the_credential_is_a_400(client: TestClient) -> None:
    info = _played_out(client)
    try:
        response = _post(client, info["game_id"], _body(info, role="advisor"))
        assert response.status_code == 400, response.text
        assert _outcomes(client, info) == []
    finally:
        _end(client, info)


def test_an_ai_seats_reviewer_row_is_recorded_and_counted(client: TestClient) -> None:
    """Review fix (gap 4/8): the seat's own uid may file a SECOND row, as
    `role: reviewer`, for the post-game DR3 review - counted apart from the
    handover's own totals (`review.py`'s own `play_stats[seat]["reviewer"]`
    - `test_review.py` covers the counting itself; this covers the row
    actually being accepted)."""
    info = _played_out(client)
    try:
        handover = _post(client, info["game_id"], _body(info, role="seat"))
        assert handover.status_code == 200, handover.text
        review_row = _post(
            client,
            info["game_id"],
            _body(info, role="reviewer", admitted=[{"sha": "c" * 40, "dsl_change": True}]),
        )
        assert review_row.status_code == 200, review_row.text
        rows = _outcomes(client, info)
        assert [r["detail"]["role"] for r in rows] == ["seat", "reviewer"]
    finally:
        _end(client, info)


def test_a_human_seats_reviewer_row_is_accepted_though_it_is_not_an_ai_seat(
    client: TestClient,
) -> None:
    """FD7 gap 15: a human seat gets a review Job too, so its own uid may
    file `role: reviewer` even though `test_a_human_seat_is_refused` already
    covers that its own `role: seat` (a handover) never can."""
    info = _played_out(client)
    try:
        response = _post(client, info["game_id"], _body(info, player="alice", role="reviewer"))
        assert response.status_code == 200, response.text
        [row] = _outcomes(client, info)
        assert (row["detail"]["player"], row["detail"]["role"]) == ("alice", "reviewer")
    finally:
        _end(client, info)


def test_an_advisor_uid_never_files_a_reviewer_row(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    store.register_advisors(game_id, {"buddy-uid": Advisor(player="alice")})
    try:
        response = _post(
            client, game_id, _body(info, player="alice", uid="buddy-uid", role="reviewer")
        )
        assert response.status_code == 400, response.text
        assert _outcomes(client, info) == []
    finally:
        _end(client, info)


def test_a_reviewer_row_never_schedules_the_f1_followup(client: TestClient) -> None:
    """A `reviewer` row is the DR3 review, not the handover's own
    development step, whatever `state`/`unfinished_reasons` it happens to
    carry - it must never schedule F1's own follow-up Job."""
    info = _played_out(client)
    try:
        response = _post(
            client,
            info["game_id"],
            _body(info, role="reviewer", state="unfinished", unfinished_reasons=["x"]),
        )
        assert response.status_code == 200, response.text
        assert "followup" not in response.json()
    finally:
        _end(client, info)


def test_an_advisor_files_for_the_human_it_watches(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    store.register_advisors(game_id, {"buddy-uid": Advisor(player="alice")})
    try:
        own = _post(client, game_id, _body(info, player="alice", uid="buddy-uid", role="advisor"))
        assert own.status_code == 200, own.text
        [row] = _outcomes(client, info)
        assert (row["detail"]["player"], row["detail"]["role"]) == ("alice", "advisor")

        other = _post(client, game_id, _body(info, player="bob", uid="buddy-uid", role="advisor"))
        assert other.status_code == 404, other.text
        as_seat = _post(client, game_id, _body(info, player="alice", uid="buddy-uid"))
        assert as_seat.status_code == 400, as_seat.text
        assert len(_outcomes(client, info)) == 1
    finally:
        _end(client, info)


def test_an_advisor_is_refused_while_the_game_is_live(client: TestClient) -> None:
    info = _agent_game(client)
    game_id = info["game_id"]
    store.register_advisors(game_id, {"buddy-uid": Advisor(player="alice")})
    try:
        body = _body(info, player="alice", uid="buddy-uid", role="advisor")
        assert _post(client, game_id, body).status_code == 409
    finally:
        _end(client, info)


def test_the_body_is_strict(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        for bad in (
            _body(info, state="done"),
            _body(info, role="not-a-role"),
            _body(info, proposed=[{}]),
            _body(info, admitted=[{"sha": "a"}]),
            _body(info, no_change=[{"candidate": "x", "reason": "  "}]),
            {**_body(info), "dry_run": True},
        ):
            response = _post(client, game_id, bad)
            assert response.status_code == 422, (bad, response.text)
        assert _outcomes(client, info) == []
    finally:
        _end(client, info)


def test_the_route_is_in_the_gates_binding_tables() -> None:
    """Seat's own player only - never a controlled player (CR 723.5 hands
    over in-game decisions, not the post-game record) - and one of the
    advisor's say-something POSTs."""
    assert ROUTE in game_access.OWN_PLAYER_ROUTES
    assert ROUTE not in game_access._BODY_PLAYER_ROUTES
    assert ROUTE.endswith(game_access.ADVISOR_POST_SUFFIXES)


# -- eval_report lists it ---------------------------------------------------------


def _ledger() -> list[Any]:
    from scripts.eval_report import GameRows

    rows = [
        {"payload": {"action": "game_over", "turn": 5, "detail": {"winner": "bob"}}},
        {
            "payload": {
                "action": "plays_outcome",
                "detail": {
                    "player": "bob",
                    "role": "seat",
                    "state": "unfinished",
                    "admitted": [{"sha": "a" * 40, "dsl_change": True}],
                    "proposed": [{"sha": None, "result": "conflict"}],
                    "no_change": [{"candidate": "p1", "reason": "r"}],
                    "unfinished_reasons": ["nudged"],
                    "repushed": True,
                    "followup": False,
                },
            }
        },
    ]
    return [GameRows(game_id="g1", players=["alice", "bob"], rows=rows)]


def test_eval_report_lists_plays_outcomes_and_never_as_unclassified() -> None:
    from scripts.eval_report import build_report, render

    report = build_report(_ledger())
    assert "plays_outcome" not in report["unclassified_actions"]
    assert report["plays_outcomes"] == [
        {
            "game_id": "g1",
            "player": "bob",
            "role": "seat",
            "state": "unfinished",
            "admitted": 1,
            "dsl_changes": 1,
            "proposed": 1,
            "no_change": 1,
            "unfinished_reasons": ["nudged"],
            "repushed": True,
            "followup": False,
        }
    ]
    text = render(report)
    assert "PLAYS OUTCOMES" in text
    assert "g1 bob (seat): unfinished" in text
