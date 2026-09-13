"""T10: the `records` table's own behaviour - reasoning required of an agent
seat and optional for a human one, a refused action still writing exactly one
row with the engine's own refusal text, and "what else was legal" holding the
pre-action enumeration. Docker-free (`InMemoryRepository`) - the postgres
409/CAS surface has its own file, `test_version_conflict_http.py`, gated on
`MCM_TEST_DATABASE_URL` the same way `test_repository_postgres_cas.py` is.
"""

from __future__ import annotations

from conftest import add_card, find, state
from fastapi.testclient import TestClient
from test_flow import cast_and_resolve_bears, seats_for

from magic_cards_model_api import records


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
    info = response.json()
    if active == agent:
        # `records.require_plan` refuses an agent seat's first mutating
        # action of its *own* turn until it has declared a plan for that
        # turn. Every test below is about `reasoning` or about the
        # pre-action enumeration, not about the plan gate, so the
        # declaration is made here once rather than restated in each.
        _declare_plan(client, info, agent)
    return info


def _declare_plan(client: TestClient, info: dict, player: str) -> None:
    response = client.post(
        f"/api/games/{info['game_id']}/plan",
        json={
            "player": player,
            "uid": info["seat_uids"][player],
            "board_read": "an empty board, my opening hand",
            "my_role": "beatdown",
            "plan": "make my land drop and develop",
            "prediction": {
                "expecting": "the opponent does nothing this turn",
                "because": "they have played no permanents yet",
                "confidence": "low",
            },
        },
    )
    assert response.status_code == 200, response.text


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

        # Read as bob: `visibility.redact_log_entry` hands a seat's stated
        # reasoning back to that seat and to the admin key, and to nobody
        # else - it is the agent's own account of why, and an opponent
        # reading it live is reading its mind mid-game.
        log = log_of(client, info["game_id"], uid=info["seat_uids"]["bob"])
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

        log = log_of(client, info["game_id"], uid=info["seat_uids"]["bob"])
        passes = entries_for(log, "pass_priority")
        assert len(passes) == 1, [entry["seq"] for entry in passes]
        assert passes[0]["detail"]["allowed"] is False
        assert passes[0]["detail"]["reason"] == response.json()["reason"]
        assert passes[0]["reasoning"] == "checking if it is my turn"
    finally:
        _end(client, info)


# ---------------------------------------------------------------------------
# the ending: a game that ends says so, once
# ---------------------------------------------------------------------------


def test_a_conceded_game_writes_one_game_over_row_naming_the_winner(client: TestClient) -> None:
    """CR 104.1: "A game ends immediately when a player wins, when the game
    is a draw, or when the game is restarted." Until this row existed the
    ledger had no terminal outcome at all - a game ending to lethal damage
    left the result to be inferred from the final snapshot's life totals,
    which is exactly the kind of inference a post-game review must not ask
    a sceptical reader to make."""
    info = _boot(client, agent="bob", human="alice", active="alice")
    try:
        conceded = client.post(
            f"/api/games/{info['game_id']}/actions/concede",
            json={"player": "bob", "reasoning": "no outs left"},
        )
        assert conceded.status_code == 200, conceded.text

        endings = entries_for(log_of(client, info["game_id"]), "game_over")
        assert len(endings) == 1, [entry["seq"] for entry in endings]
        assert endings[0]["detail"]["winner"] == "alice"
        assert endings[0]["detail"]["drawn"] is False
    finally:
        _end(client, info)


def test_the_ending_is_written_once_however_many_calls_follow(client: TestClient) -> None:
    """A finished game still accepts calls (they are refused by the engine,
    and a refusal is still a row). The ending is a fact about the game, not
    about the call that noticed it, so it is written exactly once."""
    info = _boot(client, agent="bob", human="alice", active="alice")
    try:
        client.post(
            f"/api/games/{info['game_id']}/actions/concede",
            json={"player": "bob", "reasoning": "no outs left"},
        )
        for _ in range(3):
            client.post(
                f"/api/games/{info['game_id']}/actions/pass-priority", json={"player": "alice"}
            )

        endings = entries_for(log_of(client, info["game_id"]), "game_over")
        assert len(endings) == 1, [entry["seq"] for entry in endings]
    finally:
        _end(client, info)


def test_a_game_still_running_has_no_ending_row(client: TestClient) -> None:
    info = _boot(client, agent="bob", human="alice", active="alice")
    try:
        client.post(
            f"/api/games/{info['game_id']}/cards",
            json={"card": "Mountain", "player": "alice", "zone": "hand"},
        )
        client.post(
            f"/api/games/{info['game_id']}/actions/play-land",
            json={"player": "alice", "card": "Mountain"},
        )

        assert entries_for(log_of(client, info["game_id"]), "game_over") == []
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


# ---------------------------------------------------------------------------
# who may read "what else was legal": the server's own resolved seat, never a
# field the caller filled in
# ---------------------------------------------------------------------------


def _pause_on_bobs_blockers(client: TestClient, game: str) -> dict[str, str]:
    """Drive `game` to a pending `declare_blockers` decision addressed to
    bob, with alice attacking. Returns the seat uids."""
    bears = cast_and_resolve_bears(client, game)
    client.patch(f"/api/games/{game}/cards/{bears}", json={"summoning_sick": False})
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    uids = seats_for(game)
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": bears, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True, attack.json()
    paused = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    assert paused.json()["pending_decision"]["kind"] == "declare_blockers", paused.json()
    return uids


def test_a_seat_that_is_not_the_deciding_one_cannot_read_its_legal_actions(
    client: TestClient, game: str
) -> None:
    """The leak this guards: `declare-blockers`'s audit detail carries
    `player` straight off the request body, which nothing validates against
    the deciding seat, while the enumeration is computed for the *deciding*
    player. alice, refused for presenting the wrong uid, could name herself
    in `player` and then read bob's own pre-decision enumeration - every
    blocking assignment his hand and board allowed - back out of the log.

    Redaction has to key on the identity the server resolved, never on a
    field the caller supplied.
    """
    uids = _pause_on_bobs_blockers(client, game)

    refused = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["alice"]},
        json={"player": "alice", "blocks": []},
    )
    assert refused.json()["allowed"] is False
    assert "does not open the deciding player's seat" in refused.json()["reason"]

    alice_log = log_of(client, game, uid=uids["alice"])
    for entry in entries_for(alice_log, "declare_blockers"):
        assert entry["legal_actions_before"] is None, (
            "alice is not the deciding seat - she must never read bob's own "
            f"enumeration out of the log: {entry}"
        )


def test_the_deciding_seat_still_reads_its_own_declaration_enumeration(
    client: TestClient, game: str
) -> None:
    """The other half: the whole point of the column is that the acting
    seat learns from it, so bob's own successful declaration comes back to
    bob with the enumeration intact."""
    uids = _pause_on_bobs_blockers(client, game)

    declared = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={"player": "bob", "blocks": []},
    )
    assert declared.json()["allowed"] is True, declared.json()

    bob_log = log_of(client, game, uid=uids["bob"])
    entry = entries_for(bob_log, "declare_blockers")[-1]
    assert entry["legal_actions_before"], entry
    assert entry["legal_actions_for"] == "bob"
    assert all(action["player"] == "bob" for action in entry["legal_actions_before"])


def test_the_attacking_seat_reads_its_own_attack_enumeration_back(
    client: TestClient, game: str
) -> None:
    """`actions/attack` recorded `attackers`/`dry_run` as audit detail but
    never named the acting seat, so the enumeration was redacted from every
    seat viewer - the attacker included - and only an unauthenticated reader
    ever saw it. alice declared the attack; alice must be able to read what
    else she could have declared."""
    bears = cast_and_resolve_bears(client, game)
    client.patch(f"/api/games/{game}/cards/{bears}", json={"summoning_sick": False})
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    uids = seats_for(game)
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": bears, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True, attack.json()

    alice_log = log_of(client, game, uid=uids["alice"])
    entry = entries_for(alice_log, "attack")[-1]
    assert entry["legal_actions_before"] is not None, (
        "the attacking seat must read its own enumeration - that is what the column is for"
    )
    assert entry["legal_actions_for"] == "alice"
    assert all(action["player"] == "alice" for action in entry["legal_actions_before"])

    bob_log = log_of(client, game, uid=uids["bob"])
    assert entries_for(bob_log, "attack")[-1]["legal_actions_before"] is None


def test_a_log_read_with_no_uid_sees_no_legal_actions_before(client: TestClient, game: str) -> None:
    """A caller presenting nothing must never see more than one presenting
    garbage. `GET .../log` with no uid used to skip redaction entirely and
    hand back every seat's enumeration in full - hidden information, named
    card by card, to anybody who knows the game id.

    The admin key is still the operator's own full eye; an unauthenticated
    read is not.
    """
    uids = _pause_on_bobs_blockers(client, game)
    client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={"player": "bob", "blocks": []},
    )

    anonymous = log_of(client, game)
    assert anonymous, "the log is not empty - this test would prove nothing"
    assert all(entry["legal_actions_before"] is None for entry in anonymous), (
        "an unauthenticated log read must carry no seat's legal-action enumeration at all"
    )

    garbage = log_of(client, game, uid="not-a-real-uid")
    assert all(entry["legal_actions_before"] is None for entry in garbage)


def test_the_admin_key_still_reads_every_seats_enumeration(
    client: TestClient, game: str, admin_key: str
) -> None:
    """The operator's own key keeps the full view - `GET .../legal-actions`
    grants it the same exemption, so the log has to agree."""
    uids = _pause_on_bobs_blockers(client, game)
    client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={"player": "bob", "blocks": []},
    )
    admin_log = log_of(client, game, uid=admin_key)
    entry = entries_for(admin_log, "declare_blockers")[-1]
    assert entry["legal_actions_before"] is not None


# ---------------------------------------------------------------------------
# whose action a row was: `detail["player"]`, for every action kind
# ---------------------------------------------------------------------------


def test_resolve_top_records_the_resolving_seats_uid_as_the_player(
    client: TestClient, game: str
) -> None:
    """`resolve-top` wrote `detail["player"]: null` whenever the caller sent
    no body - which is how every real caller reaches it today
    (`scripts/generate_payload_fixtures.py`'s own calls carry a `uid` and no
    body at all) - even though `uid` already named a real seat. A review
    frame for one of these rows could not say whose action it was. The
    resolved `uid` viewer is now the fallback `req.player` never named."""
    uids = seats_for(game)
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    board = state(client, game)
    forest = find(board["battlefield"], "Forest")
    mountain = find(board["battlefield"], "Mountain")
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Grizzly Bears",
            "mana_sources": [{"card_id": mountain["id"]}, {"card_id": forest["id"]}],
        },
    )
    assert cast.json()["allowed"] is True, cast.json()

    resolved = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]})
    assert resolved.json()["resolved"] == "Grizzly Bears", resolved.json()

    log = log_of(client, game, uid=uids["alice"])
    entry = entries_for(log, "resolve_top")[-1]
    assert entry["detail"]["player"] == "alice"


def test_resolve_top_with_no_uid_at_all_still_names_no_one(client: TestClient, game: str) -> None:
    """The fallback only fires for a `uid` that actually resolves to a seat -
    an anonymous or admin-key call still writes `player: null`, the same as
    before, rather than guessing."""
    cast_and_resolve_bears(client, game)
    log = log_of(client, game)
    entry = entries_for(log, "resolve_top")[-1]
    assert entry["detail"]["player"] is None


def test_attack_records_the_declaring_seat_as_the_player(client: TestClient, game: str) -> None:
    """`AttackRequest` carries no `player` field at all (only
    `DeclareBlockersRequest` does) - so every `attack` row was missing
    `detail["player"]`, and a review frame for one could not say whose
    declaration it was. `_answer_declaration` now fills it in from the
    engine's own resolved deciding seat, the identity it already checks
    `uid` against."""
    bears = cast_and_resolve_bears(client, game)
    client.patch(f"/api/games/{game}/cards/{bears}", json={"summoning_sick": False})
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    uids = seats_for(game)
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": bears, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True, attack.json()

    log = log_of(client, game, uid=uids["alice"])
    entry = entries_for(log, "attack")[-1]
    assert entry["detail"]["player"] == "alice"


def test_a_refused_attack_still_names_the_deciding_seat(client: TestClient, game: str) -> None:
    """A refusal is still evidence of whose declaration it was refused for -
    the same fallback applies once a pending `declare_attackers` decision
    exists, refusal or not."""
    bears = cast_and_resolve_bears(client, game)
    client.patch(f"/api/games/{game}/cards/{bears}", json={"summoning_sick": False})
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    uids = seats_for(game)
    refused = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["bob"]},
        json={"attackers": [{"card_id": bears, "defender": "bob"}]},
    )
    assert refused.json()["allowed"] is False, refused.json()

    log = log_of(client, game, uid=uids["alice"])
    entry = entries_for(log, "attack")[-1]
    assert entry["detail"]["player"] == "alice"


# -- was the refused action one this api had offered -------------------------


def _enumeration(*rows: dict) -> records.LegalActionsBefore:
    return records.LegalActionsBefore(player="alice", actions=list(rows))


def test_an_offered_plain_cast_does_not_vouch_for_a_face_that_was_never_offered() -> None:
    """The match has to be as sharp as the announcement, or `offered: True`
    would file a seat's own mistake under the api's. An Adventure card is two
    rows (CR 715.3): the plain cast with `spell_face: None` and the Adventure
    naming its face. A request naming a face the enumeration did not list
    must not match the plain row sitting beside it - and "no face named" must
    not match the face row either."""
    card = "11111111-1111-1111-1111-111111111111"
    plain = {"kind": "cast", "card_id": card, "spell_face": None, "from_zone": "hand"}
    stomp = {**plain, "spell_face": "Stomp"}

    assert records._was_offered("cast", {"card_id": card}, _enumeration(plain)) is True
    assert (
        records._was_offered("cast", {"card_id": card, "spell_face": "Stomp"}, _enumeration(plain))
        is False
    )
    assert (
        records._was_offered(
            "cast", {"card_id": card, "spell_face": "Stomp"}, _enumeration(plain, stomp)
        )
        is True
    )
    assert records._was_offered("cast", {"card_id": card}, _enumeration(stomp)) is False
    # a different kind for the same card is not the same offer
    assert (
        records._was_offered(
            "cast", {"card_id": card}, _enumeration({"kind": "play_land", "card_id": card})
        )
        is False
    )


def test_an_action_the_enumeration_cannot_be_searched_for_makes_no_claim() -> None:
    """`None`, never `False`: a request that names its card by printed name
    rather than by id, a command kind with no enumerated row to look for, and
    a request with no enumeration at all are all rows nothing was checked
    on, and `False` would claim "never offered" - which is the one answer
    the scorecard counts as the seat's fault."""
    card = "11111111-1111-1111-1111-111111111111"
    row = {"kind": "cast", "card_id": card}

    assert records._was_offered("cast", {"card": "Lightning Bolt"}, _enumeration(row)) is None
    assert records._was_offered("advance", {"player": "alice"}, _enumeration(row)) is None
    assert records._was_offered("crew", {"card_id": card}, _enumeration(row)) is None
    assert records._was_offered("cast", {"card_id": card}, None) is None
    assert (
        records._was_offered(
            "pass_priority", {"player": "alice"}, _enumeration({"kind": "pass_priority"})
        )
        is True
    )
