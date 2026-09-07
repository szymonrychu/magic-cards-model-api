"""T10: the `records` table's own behaviour - reasoning required of an agent
seat and optional for a human one, a refused action still writing exactly one
row with the engine's own refusal text, and "what else was legal" holding the
pre-action enumeration. Docker-free (`InMemoryRepository`) - the postgres
409/CAS surface has its own file, `test_version_conflict_http.py`, gated on
`MCM_TEST_DATABASE_URL` the same way `test_repository_postgres_cas.py` is.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _boot(client: TestClient, *, agent: str, human: str, active: str) -> dict:
    """A fresh two-seat game, one seat flagged an agent at creation
    (`NewPlayer.is_agent`, T10), the other left a plain human."""
    response = client.post(
        "/api/games",
        json={
            "players": [
                {"username": agent, "is_agent": True},
                {"username": human, "is_agent": False},
            ],
            "active_player": active,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _end(client: TestClient, info: dict) -> None:
    client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def log_of(client: TestClient, game_id: str, uid: str | None = None) -> list[dict]:
    params = {"uid": uid} if uid else None
    response = client.get(f"/api/games/{game_id}/log", params=params)
    assert response.status_code == 200, response.text
    return response.json()["log"]


def entries_for(log: list[dict], action: str) -> list[dict]:
    return [entry for entry in log if entry["action"] == action]


# ---------------------------------------------------------------------------
# reasoning: required for an agent seat, optional for a human one
# ---------------------------------------------------------------------------


def test_an_agent_seats_action_without_reasoning_is_rejected_422(client: TestClient) -> None:
    info = _boot(client, agent="bob", human="alice", active="bob")
    try:
        add = client.post(
            f"/api/games/{info['game_id']}/cards",
            json={"card": "Mountain", "player": "bob", "zone": "hand"},
        )
        assert add.status_code == 200, add.text

        response = client.post(
            f"/api/games/{info['game_id']}/actions/play-land",
            json={"player": "bob", "card": "Mountain"},
        )
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["detail"] == [
            {
                "loc": ["body", "reasoning"],
                "msg": (
                    "bob is an agent seat, and this action needs `reasoning` - why "
                    "it is doing this, in its own words"
                ),
                "type": "missing",
            }
        ]
        # Refused before the engine ever saw it: no land was played, and
        # nothing was written to the log for this attempt at all.
        log = log_of(client, info["game_id"])
        assert entries_for(log, "play_land") == []
    finally:
        _end(client, info)


def test_the_same_action_from_an_agent_seat_with_reasoning_is_accepted(
    client: TestClient,
) -> None:
    info = _boot(client, agent="bob", human="alice", active="bob")
    try:
        client.post(
            f"/api/games/{info['game_id']}/cards",
            json={"card": "Mountain", "player": "bob", "zone": "hand"},
        )
        response = client.post(
            f"/api/games/{info['game_id']}/actions/play-land",
            json={
                "player": "bob",
                "card": "Mountain",
                "reasoning": "I need red mana on board before I can cast anything",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["allowed"] is True

        log = log_of(client, info["game_id"])
        entry = entries_for(log, "play_land")[-1]
        assert entry["reasoning"] == "I need red mana on board before I can cast anything"
    finally:
        _end(client, info)


def test_the_same_action_from_a_human_seat_with_no_reasoning_is_accepted(
    client: TestClient,
) -> None:
    """The mirror case: `alice` is the plain human seat `_boot` creates, and
    the exact same request that got bob a 422 is accepted for her outright -
    `reasoning` is optional for a human seat, never required."""
    info = _boot(client, agent="bob", human="alice", active="alice")
    try:
        client.post(
            f"/api/games/{info['game_id']}/cards",
            json={"card": "Mountain", "player": "alice", "zone": "hand"},
        )
        response = client.post(
            f"/api/games/{info['game_id']}/actions/play-land",
            json={"player": "alice", "card": "Mountain"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["allowed"] is True

        log = log_of(client, info["game_id"])
        entry = entries_for(log, "play_land")[-1]
        assert entry["reasoning"] is None
    finally:
        _end(client, info)


# ---------------------------------------------------------------------------
# a refused action still writes exactly one row
# ---------------------------------------------------------------------------


def test_a_refused_illegal_action_writes_exactly_one_row_with_the_engines_reason(
    client: TestClient,
) -> None:
    """`cast` is refused deep inside `_run_command` (the engine's own
    `Outcome.allowed = False`, not a transport error) - the replace()-linked
    refusal path, the majority shape in this api."""
    info = _boot(client, agent="bob", human="alice", active="alice")
    try:
        client.post(
            f"/api/games/{info['game_id']}/cards",
            json={"card": "Lightning Bolt", "player": "alice", "zone": "hand"},
        )
        board = client.get(
            f"/api/games/{info['game_id']}", params={"uid": info["admin_key"]}
        ).json()
        bolt = board["hands"]["alice"][0]

        response = client.post(
            f"/api/games/{info['game_id']}/actions/cast",
            json={"player": "alice", "card_id": bolt["id"], "targets": ["bob"]},
        )
        body = response.json()
        assert body["allowed"] is False
        assert body["reason"] == "pool {} cannot pay {R}"

        log = log_of(client, info["game_id"])
        casts = entries_for(log, "cast")
        assert len(casts) == 1, [entry["seq"] for entry in casts]
        assert casts[0]["detail"]["allowed"] is False
        assert casts[0]["detail"]["reason"] == "pool {} cannot pay {R}"
    finally:
        _end(client, info)


def test_a_refusal_caught_before_any_command_ran_also_writes_exactly_one_row(
    client: TestClient,
) -> None:
    """`pass_priority` refused by a `ValueError` raised before `_run_command`
    ever calls `commands.run_command` at all - the standalone-append path
    (`app.py`'s `action_pass_priority`), the one T10 had to add rather than
    extend, since there is no game mutation here to piggyback a row onto."""
    info = _boot(client, agent="bob", human="alice", active="alice")
    try:
        # alice holds priority at game start (CR 103.8/117); bob passing
        # first is refused with "it is not <player>'s turn to have priority"
        # or similar, straight out of `Game.pass_priority`.
        response = client.post(
            f"/api/games/{info['game_id']}/actions/pass-priority",
            json={"player": "bob", "reasoning": "checking if it is my turn"},
        )
        assert response.json()["allowed"] is False

        log = log_of(client, info["game_id"])
        passes = entries_for(log, "pass_priority")
        assert len(passes) == 1, [entry["seq"] for entry in passes]
        assert passes[0]["detail"]["allowed"] is False
        assert passes[0]["detail"]["reason"] == response.json()["reason"]
        assert passes[0]["reasoning"] == "checking if it is my turn"
    finally:
        _end(client, info)


# ---------------------------------------------------------------------------
# "what else was legal" - the pre-action enumeration
# ---------------------------------------------------------------------------


def test_legal_actions_before_matches_the_pre_action_enumeration(client: TestClient) -> None:
    """The `records` row for bob's `play_land` carries the exact same
    enumeration `GET .../legal-actions` would have answered for bob one
    instant before the play - the library's own guarantee
    (`machines.legal_actions.legal_actions`), read into the log rather than
    re-implemented."""
    info = _boot(client, agent="bob", human="alice", active="bob")
    try:
        client.post(
            f"/api/games/{info['game_id']}/cards",
            json={"card": "Mountain", "player": "bob", "zone": "hand"},
        )
        bob_uid = info["seat_uids"]["bob"]

        before = client.get(
            f"/api/games/{info['game_id']}/legal-actions",
            params={"player": "bob", "uid": bob_uid},
        )
        assert before.status_code == 200, before.text
        expected = before.json()["actions"]
        assert expected, "playing a land from an otherwise-empty hand should be legal"

        played = client.post(
            f"/api/games/{info['game_id']}/actions/play-land",
            json={"player": "bob", "card": "Mountain", "reasoning": "ramping into my curve"},
        )
        assert played.json()["allowed"] is True

        # bob reads his own log entry and sees the enumeration in full.
        log = log_of(client, info["game_id"], uid=bob_uid)
        entry = entries_for(log, "play_land")[-1]
        assert entry["legal_actions_before"] == expected

        # alice, the opponent, reads the same log and sees it redacted -
        # this names cards in bob's own hand, the same hidden information
        # `GET .../legal-actions` itself refuses her.
        alice_uid = info["seat_uids"]["alice"]
        alice_log = log_of(client, info["game_id"], uid=alice_uid)
        alice_entry = entries_for(alice_log, "play_land")[-1]
        assert alice_entry["legal_actions_before"] is None

        # the admin key (the harness's own eye) sees it unredacted too.
        admin_log = log_of(client, info["game_id"], uid=info["admin_key"])
        admin_entry = entries_for(admin_log, "play_land")[-1]
        assert admin_entry["legal_actions_before"] == expected
    finally:
        _end(client, info)


def test_legal_actions_before_is_the_pre_mutation_enumeration_not_the_post(
    client: TestClient,
) -> None:
    """After playing the land bob no longer has it in hand, so the *post*-
    action enumeration would not offer it any more - the recorded one still
    does, because it was captured before `commands.run_command` ran."""
    info = _boot(client, agent="bob", human="alice", active="bob")
    try:
        client.post(
            f"/api/games/{info['game_id']}/cards",
            json={"card": "Mountain", "player": "bob", "zone": "hand"},
        )
        bob_uid = info["seat_uids"]["bob"]
        board = client.get(f"/api/games/{info['game_id']}", params={"uid": bob_uid}).json()
        mountain_id = board["hands"]["bob"][0]["id"]

        played = client.post(
            f"/api/games/{info['game_id']}/actions/play-land",
            json={"player": "bob", "card": "Mountain", "reasoning": "land drop"},
        )
        assert played.json()["allowed"] is True

        log = log_of(client, info["game_id"], uid=bob_uid)
        entry = entries_for(log, "play_land")[-1]
        recorded_play_lands = {
            action["card_id"]
            for action in entry["legal_actions_before"]
            if action["kind"] == "play_land"
        }
        assert mountain_id in recorded_play_lands, "the pre-action enumeration must still offer it"

        # The card's own `id` can persist across the hand-to-battlefield
        # zone change (CR 400.7's "new object" is tracked by the engine's
        # own `zone_change_stamp`, not by this id) - so what proves "post,
        # not pre" is that a *second* `play_land` naming this same card is
        # no longer offered, not that the id vanished from every action.
        after = client.get(
            f"/api/games/{info['game_id']}/legal-actions",
            params={"player": "bob", "uid": bob_uid},
        ).json()["actions"]
        after_play_lands = {action["card_id"] for action in after if action["kind"] == "play_land"}
        assert mountain_id not in after_play_lands, "bob already played his land for the turn"
    finally:
        _end(client, info)
