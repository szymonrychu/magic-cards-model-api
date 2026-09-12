"""The buddy: an advisor pod, started beside the AI seats, in all three
`AI_SEAT_MODE` mechanisms.

A buddy is an *advisor* (`repository.Advisor`) with a runner behind it. Wave 1
gave the credential its own map, its own lookup and its own read-only gate;
this file is the lifecycle half - opting in at game creation, the role
flowing through `AiSeatRegistry.start` into every mechanism, the Kubernetes
Job's own name and labels, restart/stop through the routes a seat already
uses, and the release at End game.

Three properties are load-bearing and each has a test of its own here,
because each one has already been got wrong somewhere:

1. **The advisor's `AgentSettings` live inside its own advisor entry**, never
   in `store.register_agent_settings`, which is keyed by *username*. The
   lobby's own seat resolution (`ui/lobby_model.js`'s `seatsFromRow`) decides
   "me" = the seat with no entry in `agents`, so a buddy's settings filed
   under the human's name would make the browser think the human is the AI
   seat and hand them the wrong uid.
2. **No advisor uid appears in `GET /api/lobby/games/{id}/seats`.** A uid is
   a credential. That route hands every *seat* uid to any caller - a
   pre-existing hole, out of scope here - and adding the advisor's to it
   would be a new leak, so the advisor's presence, player and status are
   exposed there without it.
3. **A failed buddy is a fallback, never an error.** `AiSeatRegistry.start`
   never raises and a buddy is strictly optional to play, so a buddy that
   could not start comes back `mechanism: "fallback"` with the game fully
   playable.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from oidc_testkit import ISSUER, bearer_header, generate_keypair, jwks, token
from ui_node import requires_node, run_module

from magic_cards_model_api import ai_seat, k8s_seats
from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.store import store

LEGAL_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
LEGAL_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"

#: A cheap stand-in for the `claude` argv, the same shape `test_ai_seat.py`
#: uses: a real OS subprocess (so the runner's own lifecycle is exercised for
#: real) that is not the real CLI.
STUB_COMMAND = ["python3", "-c", "print('DONE')"]


@pytest.fixture()
def spawnable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo `conftest.py`'s autouse `_no_real_claude_by_default` just far
    enough for `subprocess` mode to actually start something - a cheap
    stand-in process, never the real `claude`."""
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(ai_seat, "command_for", lambda prompt, agent: STUB_COMMAND)


@pytest.fixture()
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> ai_seat.AiSeatRegistry:
    """Each test gets its own registry, so runners from one test never leak
    into another's uid namespace."""
    registry = ai_seat.AiSeatRegistry()
    monkeypatch.setattr(ai_seat, "registry", registry)
    return registry


def boot(
    client: TestClient,
    *,
    seed: int = 11,
    buddy: bool = True,
    buddy_agent: dict[str, Any] | None = None,
    ai: bool = True,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One game from the lobby, exactly as the browser boots it: a human
    seat (`you`), optionally with a buddy, against an AI seat (`bot`)."""
    human: dict[str, Any] = {"username": "you", "decklist_text": LEGAL_DECK, "ai": False}
    if buddy:
        human["buddy"] = True
    if buddy_agent is not None:
        human["buddy_agent"] = buddy_agent
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [human, {"username": "bot", "decklist_text": LEGAL_DECK_B, "ai": ai}],
            "seed": seed,
        },
        headers=headers or {},
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 1. opting in at game creation
# ---------------------------------------------------------------------------


def test_creating_a_game_with_a_buddy_mints_an_advisor_uid(client: TestClient) -> None:
    created = boot(client)
    assert list(created["advisor_uids"]) == ["you"]
    uid = created["advisor_uids"]["you"]
    # A minted uid, not a name: `uuid4().hex`, the same shape every seat uid
    # has, so nothing downstream can tell one kind of credential from the
    # other by looking at it.
    assert len(uid) == 32 and uid.isalnum()
    advisors = store.advisor_uids(created["game_id"])
    assert list(advisors) == [uid]
    assert advisors[uid].player == "you"


def test_a_game_with_no_buddy_answers_exactly_as_it_always_did(client: TestClient) -> None:
    created = boot(client, buddy=False)
    assert "advisor_uids" not in created
    assert store.advisor_uids(created["game_id"]) == {}


def test_the_buddys_agent_settings_live_in_the_advisor_entry(client: TestClient) -> None:
    """The verified trap: `store.agent_settings` is NAME-keyed, so a buddy's
    settings filed under the human's name would make the lobby read the human
    as the AI seat."""
    created = boot(client, buddy_agent={"model": "opus", "effort": "max"})
    game_id = created["game_id"]
    advisor = store.advisor_uids(game_id)[created["advisor_uids"]["you"]]
    assert advisor.agent == AgentSettings(model="opus", effort="max")
    assert "you" not in store.agent_settings(game_id)
    assert list(store.agent_settings(game_id)) == ["bot"]


def test_buddy_agent_settings_without_a_buddy_are_refused(client: TestClient) -> None:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {
                    "username": "you",
                    "decklist_text": LEGAL_DECK,
                    "ai": False,
                    "buddy_agent": {"model": "opus"},
                },
                {"username": "bot", "decklist_text": LEGAL_DECK_B, "ai": True},
            ]
        },
    )
    assert response.status_code == 400
    assert "buddy" in response.text


def test_a_buddy_on_an_ai_seat_is_refused(client: TestClient) -> None:
    """A buddy is a second pair of eyes for a HUMAN - `docs/ai_buddy_brief.md`
    opens by saying it sits beside one, and nothing else made that true. An
    AI seat acts through its own seat uid and never reads the advice ledger,
    so `{"ai": true, "buddy": true}` minted an advisor uid whose proposals
    nothing would ever read and, in kubernetes mode, scheduled a second full
    Job (agent plus driver, 250m/1Gi) to write them.

    Refused for the same reason and in the same pass as the two settings
    checks beside it, and the message says why rather than only that it is
    not allowed."""
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {
                    "username": "bot",
                    "decklist_text": LEGAL_DECK,
                    "ai": True,
                    "buddy": True,
                },
                {"username": "you", "decklist_text": LEGAL_DECK_B, "ai": False},
            ]
        },
    )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "bot" in detail
    assert "buddy" in detail
    assert "human" in detail, f"the refusal never says why a buddy needs a human seat: {detail!r}"


def test_a_buddy_on_an_ai_seat_mints_no_advisor_and_starts_nothing(
    client: TestClient, spawnable: None, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    """The refusal is before any of the work: no game, so no advisor uid and
    nothing started for one."""
    before = set(store.ids())
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "bot", "decklist_text": LEGAL_DECK, "ai": True, "buddy": True},
                {"username": "you", "decklist_text": LEGAL_DECK_B, "ai": False},
            ]
        },
    )
    assert response.status_code == 400, response.text
    assert set(store.ids()) == before, "a refused boot still created a game"
    assert fresh_registry._runners == {}, "a refused boot still started something"
    assert fresh_registry._dispatched == {}, "a refused boot still dispatched something"


def test_the_buddy_is_started_beside_the_ai_seats(
    client: TestClient, spawnable: None, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    created = boot(client)
    uid = created["advisor_uids"]["you"]
    status = fresh_registry.status(uid)
    assert status is not None, "the advisor was never started"
    assert status["role"] == "advisor"
    assert status["player"] == "you"


# ---------------------------------------------------------------------------
# the advisor uid is owner-only, exactly as seat_uids already is
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def private_key():
    return generate_keypair()


@pytest.fixture()
def oidc_configured(monkeypatch: pytest.MonkeyPatch, private_key) -> None:
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_JWKS_STATIC", json.dumps(jwks(private_key)))
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)


def _row(client: TestClient, game_id: str, headers: dict[str, str], **params) -> dict | None:
    listing = client.get("/api/lobby/games", params=params, headers=headers).json()["games"]
    return next((g for g in listing if g["game_id"] == game_id), None)


def test_the_lobby_listing_hands_the_advisor_uid_to_the_owner_alone(
    client: TestClient, oidc_configured: None, private_key
) -> None:
    alice = bearer_header(token(private_key, sub="alice"))
    bob = bearer_header(token(private_key, sub="bob"))
    created = boot(client, headers=alice)
    game_id = created["game_id"]
    uid = created["advisor_uids"]["you"]
    mine = _row(client, game_id, alice)
    assert mine is not None and mine["advisors"] == {"you": uid}
    # Not the owner: the row is dropped entirely (an owned game nobody else
    # holds a capability for), which is strictly stronger than "listed
    # without the advisor uid" - assert it stays that way.
    assert _row(client, game_id, bob) is None
    # A browser holding only the human's seat uid sees the row, and no
    # advisor uid in it: a uid it did not present is a uid it must not learn.
    seat = created["seat_uids"]["you"]
    theirs = _row(client, game_id, {}, uids=seat)
    assert theirs is not None
    assert theirs.get("advisors", {}) == {}


# ---------------------------------------------------------------------------
# the lobby model still reads the human correctly
# ---------------------------------------------------------------------------


@requires_node
def test_the_lobby_model_still_identifies_the_human_with_a_buddy_present(
    client: TestClient, oidc_configured: None, private_key
) -> None:
    """`seatsFromRow` off the *real* row the server hands the owner. "me" is
    the seat with no `agents` entry, so this is the end-to-end guard that a
    buddy's settings never land under the human's name."""
    alice = bearer_header(token(private_key, sub="alice"))
    created = boot(client, buddy_agent={"model": "opus"}, headers=alice)
    row = _row(client, created["game_id"], alice)
    assert row is not None
    seats = run_module("lobby_model.js", "m.seatsFromRow(game, null)", game=row)
    assert seats["me"] == "you"
    assert seats["myUid"] == created["seat_uids"]["you"]
    assert seats["aiUid"] == created["seat_uids"]["bot"]


# ---------------------------------------------------------------------------
# 5. seat listing: the advisor's presence, without its uid
# ---------------------------------------------------------------------------


def _seats(client: TestClient, game_id: str, **params) -> list[dict[str, Any]]:
    response = client.get(f"/api/lobby/games/{game_id}/seats", params=params)
    assert response.status_code == 200, response.text
    return response.json()["seats"]


def test_game_seats_lists_the_advisor_with_its_role_player_and_status(
    client: TestClient, spawnable: None, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    created = boot(client, buddy_agent={"model": "opus"})
    rows = _seats(client, created["game_id"])
    advisors = [row for row in rows if row.get("role") == "advisor"]
    assert len(advisors) == 1
    advisor = advisors[0]
    assert advisor["player"] == "you"
    assert advisor["agent"]["model"] == "opus"
    assert advisor["ai_seat"]["role"] == "advisor"
    # The seat rows are untouched and still say what they always said.
    seats = [row for row in rows if row.get("role", "seat") == "seat"]
    assert sorted(row["player"] for row in seats) == ["bot", "you"]


def test_no_advisor_uid_appears_in_the_game_seats_response(
    client: TestClient, spawnable: None, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    """A uid is a credential. `game_seats` is readable by any caller, so the
    advisor's uid must not be anywhere in its body - not as a field, and not
    embedded in the liveness status either, which is where it leaked first:
    every `AiSeatRegistry` status dict names its own uid.

    Asserted with the buddy actually *running*, which is the case that has a
    status to leak through."""
    created = boot(client)
    uid = created["advisor_uids"]["you"]
    assert fresh_registry.status(uid) is not None, "this test needs a running buddy"
    body = client.get(f"/api/lobby/games/{created['game_id']}/seats").text
    assert uid not in body
    rows = _seats(client, created["game_id"])
    advisor = next(row for row in rows if row.get("role") == "advisor")
    assert "uid" not in advisor
    assert "uid" not in advisor["ai_seat"]


def test_no_advisor_uid_appears_before_the_buddy_has_started_either(
    client: TestClient,
) -> None:
    """The other half: a buddy that never started still has a row, and it
    still names no uid."""
    created = boot(client)
    uid = created["advisor_uids"]["you"]
    body = client.get(f"/api/lobby/games/{created['game_id']}/seats").text
    assert uid not in body


def test_no_advisor_uid_appears_in_a_failed_buddys_fallback_status(
    client: TestClient, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    """The third shape, and the one that actually leaked: a buddy that *tried
    and could not* start.

    `AiSeatRegistry.start`'s subprocess fallback hands back the `prompt` and
    the `command` a human would need to run the session by hand, and both
    spell the uid out - `build_prompt` substitutes it into the brief and
    `command_for` joins that prompt into the argv - so stripping the `uid`
    key alone left the credential sitting in the body twice over. This is the
    container's own case, not a contrived one: no `claude` on PATH (the
    api image installs none, and `conftest`'s autouse
    `_no_real_claude_by_default` pins it off for the suite), so every buddy
    started in `subprocess` mode lands here."""
    created = boot(client)
    game_id, uid = created["game_id"], created["advisor_uids"]["you"]
    started = client.post(
        f"/api/lobby/games/{game_id}/ai-seat/start",
        json={"advisor_uid": uid},
        params={"uid": created["seat_uids"]["you"]},
    )
    assert started.status_code == 200, started.text
    assert started.json()["mechanism"] == "fallback"
    # The owner, holding the uid, still gets the prompt and the command - it
    # is the whole point of a fallback - on the route that takes the uid.
    own = client.get(f"/api/lobby/games/{game_id}/ai-seat/status", params={"uid": uid})
    assert own.status_code == 200, own.text
    assert uid in own.json()["prompt"]
    # The broadly-readable listing names none of it.
    body = client.get(f"/api/lobby/games/{game_id}/seats").text
    assert uid not in body
    advisor = next(row for row in _seats(client, game_id) if row.get("role") == "advisor")
    assert advisor["ai_seat"]["mechanism"] == "fallback"
    assert "prompt" not in advisor["ai_seat"]
    assert "command" not in advisor["ai_seat"]
    # And the row is still worth rendering: the reason survives.
    assert "claude" in advisor["ai_seat"]["reason"]


def test_an_advisor_uid_quoted_inside_a_status_value_is_redacted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    """Dropping the keys known to carry a uid is not enough on its own,
    because a status value this module does not compose can quote one: a
    kubernetes API hiccup comes back as `could not read pod status: {exc}`
    and `K8sSeats._pod` selects on `mtg/seat-uid=<the full uid>`, so the
    selector - uid and all - rides out inside the exception's own text. So
    the advisor's row is scrubbed of the uid wherever it appears, not just
    where it is expected to."""
    monkeypatch.setenv("AI_SEAT_MODE", "kubernetes")

    class HiccupSeats:
        """`K8sSeats`, answering the transient-API-error branch its own
        `status` has (`k8s_seats.py`, `could not read pod status`)."""

        def start(self, *, uid: str, game_id: str, player: str, agent: Any, role: str = "seat"):
            return {
                "uid": uid,
                "mechanism": "kubernetes",
                "state": "pending",
                "job": k8s_seats.job_name(game_id=game_id, uid=uid, role=role),
                "game_id": game_id,
                "player": player,
                "role": role,
            }

        def status(self, uid: str):
            return {
                "uid": uid,
                "mechanism": "kubernetes",
                "state": "pending",
                "job": f"mtg-buddy-abcdef12-{uid[:8]}",
                "player": "you",
                "role": "advisor",
                "reason": (
                    "could not read pod status: (403) GET /api/v1/namespaces/mtg/pods"
                    f"?labelSelector=mtg%2Fseat-uid%3D{uid}"
                ),
            }

        def release(self, uid: str):
            return {"deleted": True}

    monkeypatch.setattr(k8s_seats, "get", HiccupSeats)
    created = boot(client)
    game_id, uid = created["game_id"], created["advisor_uids"]["you"]
    body = client.get(f"/api/lobby/games/{game_id}/seats").text
    assert uid not in body
    advisor = next(row for row in _seats(client, game_id) if row.get("role") == "advisor")
    # Redacted, not dropped: the operator still learns that the pod could not
    # be read and that it was a 403.
    assert "could not read pod status" in advisor["ai_seat"]["reason"]
    assert "403" in advisor["ai_seat"]["reason"]
    # The Job name keeps its eight-character prefix, deliberately - see
    # `decks_api._advisor_row_status`.
    assert advisor["ai_seat"]["job"] == f"mtg-buddy-abcdef12-{uid[:8]}"


def test_an_advisor_uid_reads_its_humans_decklist_and_no_one_elses(
    client: TestClient,
) -> None:
    """An advisor sees exactly what its human sees (`visibility.
    resolve_viewer`), and the human sees their own decklist - which is also
    what lets the buddy's driver identify the deck it is advising on."""
    created = boot(client)
    uid = created["advisor_uids"]["you"]
    rows = _seats(client, created["game_id"], uid=uid)
    by_player = {row["player"]: row for row in rows if row.get("role", "seat") == "seat"}
    assert by_player["you"]["decklist"] == LEGAL_DECK
    assert by_player["bot"].get("decklist") is None
    # And the seat row comes FIRST for a player who has both rows, which is
    # load-bearing: `mtg_agent.driver.app`'s `seat_decklist` takes the first
    # entry whose `player` matches, so an advisor row ahead of the seat's
    # would hand the buddy an empty decklist and no deck guide.
    mine = [row for row in rows if row["player"] == "you"]
    assert [row.get("role", "seat") for row in mine] == ["seat", "advisor"]


# ---------------------------------------------------------------------------
# 2/4. the role through the registry, and the never-raises stance
# ---------------------------------------------------------------------------


def test_the_role_reaches_the_kubernetes_mechanism(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    calls: list[dict[str, Any]] = []

    class RecordingSeats:
        def start(self, **kwargs):
            calls.append(kwargs)
            return {"uid": kwargs["uid"], "mechanism": "kubernetes", "state": "pending"}

    monkeypatch.setenv("AI_SEAT_MODE", "kubernetes")
    monkeypatch.setattr(k8s_seats, "get", RecordingSeats)
    fresh_registry.start(
        uid="advisor-uid",
        game_id="g1",
        player="you",
        api_base="http://api",
        agent=AgentSettings(model="sonnet"),
        role="advisor",
    )
    assert calls == [
        {
            "uid": "advisor-uid",
            "game_id": "g1",
            "player": "you",
            "agent": AgentSettings(model="sonnet"),
            "role": "advisor",
        }
    ]


def test_the_role_reaches_the_driver_dispatch_payload(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    posted: list[dict[str, Any]] = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, path, json):
            posted.append(json)

            class Response:
                status_code = 202

                @staticmethod
                def raise_for_status():
                    return None

                @staticmethod
                def json():
                    return {"deck": None, "turnId": None}

            return Response()

    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setattr(ai_seat, "_driver_client", lambda base: Client())
    status = fresh_registry.start(
        uid="advisor-uid",
        game_id="g1",
        player="you",
        api_base="http://api",
        agent=AgentSettings(model="sonnet"),
        role="advisor",
    )
    assert posted[0]["role"] == "advisor"
    assert status["role"] == "advisor"


def test_a_buddy_that_cannot_start_is_a_fallback_not_an_error(
    fresh_registry: ai_seat.AiSeatRegistry,
) -> None:
    """`claude` is off PATH (`conftest.py`'s autouse default), which is the
    plain "nothing to run it with" case - the registry reports it and never
    raises, exactly as it does for a seat."""
    status = fresh_registry.start(
        uid="advisor-uid",
        game_id="g1",
        player="you",
        api_base="http://api",
        agent=AgentSettings(model="sonnet"),
        role="advisor",
    )
    assert status["mechanism"] == "fallback"
    assert status["role"] == "advisor"
    assert status["state"] == "not_started"
    assert status["reason"]


def test_a_kubernetes_buddy_that_cannot_be_scheduled_is_a_fallback(
    monkeypatch: pytest.MonkeyPatch, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    def boom():
        raise RuntimeError("no cluster config")

    monkeypatch.setenv("AI_SEAT_MODE", "kubernetes")
    monkeypatch.setattr(k8s_seats, "get", boom)
    status = fresh_registry.start(
        uid="advisor-uid",
        game_id="g1",
        player="you",
        api_base="http://api",
        agent=AgentSettings(model="sonnet"),
        role="advisor",
    )
    assert status["mechanism"] == "fallback"
    assert status["role"] == "advisor"
    assert "no cluster config" in status["reason"]


def test_a_failed_start_stays_pollable_as_a_fallback(
    fresh_registry: ai_seat.AiSeatRegistry,
) -> None:
    """A start that failed is only useful to the UI if it can still be
    *polled* - `pollSeats` reads `game_seats`, which reads
    `registry.status`, and that answered `None` for a uid nothing was ever
    started for. The UI then painted "not started" with no reason, for a
    buddy (or a seat) that had genuinely tried and failed."""
    started = fresh_registry.start(
        uid="advisor-uid",
        game_id="g1",
        player="you",
        api_base="http://api",
        agent=AgentSettings(model="sonnet"),
        role="advisor",
    )
    polled = fresh_registry.status("advisor-uid")
    assert polled is not None
    assert polled["mechanism"] == "fallback"
    assert polled["role"] == "advisor"
    assert polled["reason"] == started["reason"]
    # Released with the game like any other runner, and then genuinely gone.
    fresh_registry.release("advisor-uid")
    assert fresh_registry.status("advisor-uid") is None


def test_a_failed_buddy_leaves_the_game_fully_playable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the never-raises stance: a buddy is optional to
    play. Creation succeeds, the game answers, and the human's own seat
    still acts."""
    monkeypatch.setenv("AI_SEAT_MODE", "kubernetes")
    monkeypatch.setattr(k8s_seats, "get", lambda: (_ for _ in ()).throw(RuntimeError("no cluster")))
    created = boot(client)
    game_id = created["game_id"]
    assert client.get(f"/api/games/{game_id}").status_code == 200
    response = client.post(
        f"/api/games/{game_id}/pregame/mulligan",
        params={"uid": created["seat_uids"]["you"]},
        json={"player": "you", "take": False, "reasoning": "a playable seven"},
    )
    assert response.status_code == 200, response.text
    # And the buddy's own status says what went wrong, in the shape the UI's
    # AI panel already renders.
    rows = _seats(client, game_id)
    advisor = next(row for row in rows if row.get("role") == "advisor")
    assert advisor["ai_seat"]["mechanism"] == "fallback"
    assert "no cluster" in advisor["ai_seat"]["reason"]


# ---------------------------------------------------------------------------
# restart and stop, through the routes a seat already uses
# ---------------------------------------------------------------------------


def test_restart_and_stop_reuse_the_ai_seat_routes(
    client: TestClient, spawnable: None, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    created = boot(client, buddy_agent={"model": "opus"})
    game_id, uid = created["game_id"], created["advisor_uids"]["you"]
    stopped = client.post(f"/api/lobby/games/{game_id}/ai-seat/stop", json={"advisor_uid": uid})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json() == {"stopped": True}
    # Started again through the same route a seat restarts through, and the
    # advisor's own agent settings - which live in its advisor entry, not in
    # the name-keyed agent map - are what it comes back as.
    restarted = client.post(f"/api/lobby/games/{game_id}/ai-seat/start", json={"advisor_uid": uid})
    assert restarted.status_code == 200, restarted.text
    assert restarted.json()["role"] == "advisor"
    assert restarted.json()["player"] == "you"


def test_the_advisor_uid_in_the_credential_field_is_refused_by_the_gate(
    client: TestClient,
) -> None:
    """`game_access._credentials` reads a body's own `uid` as the CALLER's
    credential, and an advisor credential is read-only - so the advisor uid
    goes in `advisor_uid`, naming the target, and the caller authorises
    itself the way it always did. Pinned here so the two fields cannot be
    quietly collapsed back into one."""
    created = boot(client)
    game_id, uid = created["game_id"], created["advisor_uids"]["you"]
    refused = client.post(f"/api/lobby/games/{game_id}/ai-seat/start", json={"uid": uid})
    assert refused.status_code == 404
    assert refused.json()["detail"] == f"no game {game_id}"


def test_starting_a_uid_that_is_neither_a_seat_nor_an_advisor_is_refused(
    client: TestClient,
) -> None:
    created = boot(client)
    game_id = created["game_id"]
    response = client.post(
        f"/api/lobby/games/{game_id}/ai-seat/start",
        json={"advisor_uid": "not-a-credential"},
        params={"uid": created["seat_uids"]["you"]},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 7. cleanup
# ---------------------------------------------------------------------------


def test_end_game_releases_the_advisor_uid(
    client: TestClient, spawnable: None, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    created = boot(client)
    game_id, uid = created["game_id"], created["advisor_uids"]["you"]
    assert fresh_registry.status(uid) is not None
    ended = client.delete(
        f"/api/lobby/games/{game_id}", params={"uid": created["seat_uids"]["you"]}
    )
    assert ended.status_code == 200, ended.text
    body = ended.json()
    assert body["advisors_invalidated"] == 1
    released = {entry["player"]: entry for entry in body["ai_seats_released"]}
    assert released["you"]["role"] == "advisor"
    # The registry has forgotten it: no stale status can be polled back out
    # of a finished game.
    assert fresh_registry.status(uid) is None
    assert store.advisor_uids(game_id) == {}


def test_deleting_the_game_outright_releases_the_advisor_uid_too(
    client: TestClient, spawnable: None, fresh_registry: ai_seat.AiSeatRegistry
) -> None:
    """`DELETE /api/games/{id}` ends a game just as End game does, and it used
    to release nothing at all - so a buddy started for that game kept its
    process (or, in `wrapper` mode, the driver's assignment, which then 409s
    every later dispatch for the life of that container) and kept answering
    `registry.status` for a game that no longer exists. Both routes go
    through `lobby.reset_game` now."""
    created = boot(client)
    game_id, uid = created["game_id"], created["advisor_uids"]["you"]
    assert fresh_registry.status(uid) is not None
    deleted = client.delete(f"/api/games/{game_id}", params={"key": created["admin_key"]})
    assert deleted.status_code == 200, deleted.text
    assert fresh_registry.status(uid) is None, (
        "the registry still answers for an advisor of a deleted game"
    )
    assert store.advisor_uids(game_id) == {}


# ---------------------------------------------------------------------------
# 6. what the buddy is told
# ---------------------------------------------------------------------------


def test_the_buddy_brief_is_package_data_like_the_seats() -> None:
    """`load_brief` reads through `importlib.resources`, so the buddy brief
    has to actually ship inside the package - not sit beside a checkout that
    a container will not have (2b T11's whole point)."""
    brief = ai_seat.load_brief("advisor")
    assert "mcp-seat" in brief
    assert "{api_base}" in brief and "{uid}" in brief and "{game_id}" in brief
    assert ai_seat.BRIEF_FILES["advisor"] == "ai_buddy_brief.md"
    # An unknown role reads the seat's brief rather than raising: this is on
    # the start path of a game that is already booted.
    assert ai_seat.load_brief("nonsense") == ai_seat.load_brief("seat")


def test_the_subprocess_prompt_selects_the_buddy_brief_by_role() -> None:
    seat = ai_seat.build_prompt(
        uid="u1", game_id="g1", player="you", api_base="http://api", role="seat"
    )
    advisor = ai_seat.build_prompt(
        uid="u1", game_id="g1", player="you", api_base="http://api", role="advisor"
    )
    assert seat != advisor
    filled = {
        role: ai_seat.load_brief(role).format(uid="u1", game_id="g1", api_base="http://api")
        for role in ("seat", "advisor")
    }
    assert filled["advisor"] in advisor and filled["seat"] not in advisor
    assert filled["seat"] in seat and filled["advisor"] not in seat
    # The buddy is told whose seat it advises, never that it is seated.
    assert "advis" in advisor.lower()
    assert "u1" in advisor and "g1" in advisor and "http://api" in advisor


@pytest.mark.parametrize(
    "prose",
    [
        pytest.param("brief", id="subprocess-brief"),
        pytest.param("turn_zero", id="wrapper-turn-zero"),
    ],
)
def test_the_advisor_prose_says_what_reasoning_is_for(prose: str) -> None:
    """`reasoning` IS the rationale a human reads, and the prose must ask for
    three to five sentences covering the board read, the alternatives and the
    risk - in both mechanisms' prose, since a buddy runs under either."""
    from pathlib import Path

    from mtg_agent.driver.guides import NONE, Identification
    from mtg_agent.driver.prompt import Assignment, advisor_turn_zero

    if prose == "brief":
        text = ai_seat.load_brief("advisor")
    else:
        assignment = Assignment(
            game_id="g1",
            uid="u1",
            player="you",
            opponent="bot",
            api_base="http://api",
            decklist={},
            identification=Identification(NONE),
            role="advisor",
        )
        text = advisor_turn_zero(assignment, Path("/workspace/current-game.md"), "briefing")
    # Whitespace-collapsed: the prose is hard-wrapped, so a phrase this test
    # names can legitimately straddle a newline.
    lowered = " ".join(text.lower().split())
    assert "reasoning" in lowered
    assert "three to five sentences" in lowered
    assert "alternative" in lowered
    assert "risk" in lowered
    # Silent in the steps the human's autopilot crosses inside 1.2 seconds.
    for step in ("untap", "upkeep", "draw", "end step"):
        assert step in lowered, f"the advisor prose never mentions the {step} step"
    assert "propose nothing" in lowered


def test_advisor_turn_zero_is_its_own_function_and_never_the_seats() -> None:
    from pathlib import Path

    from mtg_agent.driver.guides import NONE, Identification
    from mtg_agent.driver.prompt import Assignment, advisor_turn_zero, turn_zero

    assignment = Assignment(
        game_id="g1",
        uid="u1",
        player="you",
        opponent="bot",
        api_base="http://api",
        decklist={},
        identification=Identification(NONE),
        role="advisor",
    )
    path = Path("/workspace/current-game.md")
    advice = advisor_turn_zero(assignment, path, "briefing")
    seat = turn_zero(assignment, path, "briefing")
    assert advice != seat
    # The buddy never acts, so it is never told to play the game.
    assert "wait_for_priority" in advice, "the buddy runs the same wake loop a seat runs"
    assert "you are playing a real game" not in " ".join(advice.lower().split())


def test_the_driver_picks_the_advisor_prose_for_an_advisor_dispatch(tmp_path) -> None:
    from tests.driver.world import build_world

    world = build_world(tmp_path)
    response = world.client.post(
        "/dispatch",
        json={
            "uid": world.uid,
            "game_id": world.game_id,
            "player": "bob",
            "api_base": "http://game:8000",
            "role": "advisor",
        },
    )
    assert response.status_code == 202, response.text
    assert len(world.submitted) == 1
    text = " ".join(world.submitted[0]["text"].lower().split())
    assert "three to five sentences" in text
    assert "you are a buddy" in text
    assert "you are playing a real game" not in text


def test_the_driver_still_picks_the_seat_prose_with_no_role_named(tmp_path) -> None:
    """A dispatch from a caller that predates roles is a seat, unchanged."""
    from tests.driver.world import build_world

    world = build_world(tmp_path)
    response = world.client.post(
        "/dispatch",
        json={
            "uid": world.uid,
            "game_id": world.game_id,
            "player": "bob",
            "api_base": "http://game:8000",
        },
    )
    assert response.status_code == 202, response.text
    assert "You are playing a real game" in world.submitted[0]["text"]


def _cli_dry_run_split() -> tuple[frozenset[str], frozenset[str]]:
    """Walk the real `mcp-seat` argparse tree: (subcommands that register
    `--dry-run`, subcommands that do not). Derived from the parser rather
    than a hand-kept list, so the prose tests below fail the moment the flag
    moves."""
    import argparse

    from magic_cards_model_api.seat_cli import build_parser

    parser = build_parser()
    choices: dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            choices = dict(action.choices)
    assert choices, "mcp-seat's parser has no subcommands - the walk is wrong"
    takes, lacks = set(), set()
    for name, sub in choices.items():
        options = {opt for act in sub._actions for opt in act.option_strings}
        (takes if "--dry-run" in options else lacks).add(name)
    return frozenset(takes), frozenset(lacks)


def _paragraph_naming(text: str, phrase: str) -> str:
    """The whitespace-collapsed claim of `text` containing `phrase`. Both
    briefs are hard-wrapped, so a claim spans several source lines; a claim
    is a blank-line paragraph or a single top-level markdown bullet, because
    the wrapper's prose teaches one tool per bullet inside one list."""
    import re

    for block in re.split(r"\n\n|\n(?=- )", text):
        collapsed = " ".join(block.split())
        if phrase in collapsed:
            return collapsed
    raise AssertionError(f"no paragraph of the prose says {phrase!r}")


def test_the_buddy_brief_never_claims_every_subcommand_takes_dry_run() -> None:
    """`--dry-run` is registered on `cast`, `cycle`, `activate` and the three
    CR 116.2 special actions only. `decide`, `blockers`, `attackers` and
    `play-land` have no such flag, so argparse errors out on it - and those
    are exactly the shapes a buddy reaches for when a decision is pending.
    Telling it "every action subcommand also takes `--dry-run`" sent it at a
    flag that cannot work, in the same prose that teaches it the transport."""
    import re

    takes, lacks = _cli_dry_run_split()
    brief = ai_seat.load_brief("advisor")
    collapsed = " ".join(brief.split())
    assert "every action subcommand also takes" not in collapsed.lower(), (
        "the buddy brief still makes the universal `--dry-run` claim, which is "
        f"false for {sorted(lacks)}"
    )

    positive = _paragraph_naming(brief, "take `--dry-run`")
    named = {token for token in re.findall(r"`([a-z][a-z-]*)`", positive)}
    assert named & takes == takes, (
        "the paragraph teaching `--dry-run` must name every subcommand that "
        f"registers it; missing {sorted(takes - named)}"
    )
    assert not (named & lacks), (
        "the paragraph teaching `--dry-run` names subcommands that do not "
        f"register it: {sorted(named & lacks)}"
    )

    negative = _paragraph_naming(brief, "no `--dry-run`")
    refused = {token for token in re.findall(r"`([a-z][a-z-]*)`", negative)} & (takes | lacks)
    assert refused <= lacks, (
        "the brief says these take no `--dry-run` when argparse registers one: "
        f"{sorted(refused & takes)}"
    )
    # B2 froze DecideRequest and DeclareBlockersRequest out of DRY_RUN_SHAPES,
    # so those two must be named by the prose as unvalidatable, by name.
    assert {"decide", "blockers"} <= refused, (
        "the brief must say plainly that `decide` and `blockers` cannot be "
        f"dry-run; it names only {sorted(refused)}"
    )


def test_the_advisor_wrapper_prose_never_claims_every_tool_takes_dry_run() -> None:
    """The mirror of the brief's claim on the wrapper's transport. `dry_run`
    is a parameter of six MCP tools, not of every action tool: `play_land`,
    `declare_attackers`, `declare_blockers` and `decide` have none, and
    passing one is a TypeError, not a dry run."""
    import json
    import re
    from pathlib import Path

    from mtg_agent.driver.guides import NONE, Identification
    from mtg_agent.driver.prompt import Assignment, advisor_turn_zero

    repo_root = Path(__file__).resolve().parent.parent
    tools = json.loads((repo_root / "contract" / "tools.json").read_text())["tools"]
    takes = frozenset(
        tool["name"]
        for tool in tools
        if any(param["name"] == "dry_run" for param in tool["parameters"])
    )
    lacks = frozenset(tool["name"] for tool in tools) - takes
    assert takes and lacks, "contract/tools.json says every tool is the same shape"

    text = advisor_turn_zero(
        Assignment(
            game_id="g1",
            uid="u1",
            player="you",
            opponent="bot",
            api_base="http://api",
            decklist={},
            identification=Identification(NONE),
            role="advisor",
        ),
        Path("/workspace/current-game.md"),
        "briefing",
    )
    collapsed = " ".join(text.split())
    assert "every action tool takes" not in collapsed.lower(), (
        "the advisor wrapper prose still makes the universal `dry_run` claim, "
        f"which is false for {sorted(lacks)}"
    )

    positive = _paragraph_naming(text, "take `dry_run`")
    named = {token for token in re.findall(r"`([a-z_]+)`", positive)} & (takes | lacks)
    assert named & takes == takes, (
        "the prose teaching `dry_run` must name every tool that has the "
        f"parameter; missing {sorted(takes - named)}"
    )
    assert not (named & lacks), (
        f"the prose teaching `dry_run` names tools without it: {sorted(named & lacks)}"
    )

    negative = _paragraph_naming(text, "no `dry_run`")
    refused = {token for token in re.findall(r"`([a-z_]+)`", negative)} & (takes | lacks)
    assert refused <= lacks, (
        f"the prose says these take no `dry_run` when they do: {sorted(refused & takes)}"
    )
    assert {"decide", "declare_blockers"} <= refused, (
        "the prose must say plainly that `decide` and `declare_blockers` cannot "
        f"be dry-run; it names only {sorted(refused)}"
    )


# ---------------------------------------------------------------------------
# 8. the routine board read, decided per surface
# ---------------------------------------------------------------------------
#
# The seat's brief had "read `state`, read the `log`" deleted in this same
# branch: `tests/test_seat_round_trips.py` names that instruction as the
# measured cause of a 65-tool-call agent activation, and
# `tests/test_prompt_matches_tools.py` keeps it from coming back. The buddy's
# prose said the same thing in both surfaces' spelling, and the answer is not
# the same on both - which is why these are two tests and not one.
#
# The facts they are written against are pinned in `test_advise_mode.py`:
# `test_an_advisors_cli_wait_carries_the_state_but_no_log` and
# `test_an_advisors_mcp_wait_carries_the_state_and_the_log`.


def _collapsed_advisor_turn_zero() -> str:
    from pathlib import Path

    from mtg_agent.driver.guides import NONE, Identification
    from mtg_agent.driver.prompt import Assignment, advisor_turn_zero

    text = advisor_turn_zero(
        Assignment(
            game_id="g1",
            uid="u1",
            player="you",
            opponent="bot",
            api_base="http://api",
            decklist={},
            identification=Identification(NONE),
            role="advisor",
        ),
        Path("/workspace/current-game.md"),
        "briefing",
    )
    return " ".join(text.split())


def test_the_buddy_brief_forbids_the_state_read_its_own_wait_already_answers() -> None:
    """CLI surface, the half that IS redundant. `wait` prints the whole state
    the advisor may see, so a separate `state` call asks for what the advisor
    is already holding - exactly the call the seat's brief forbids, for
    exactly the same reason."""
    brief = ai_seat.load_brief("advisor")
    collapsed = " ".join(brief.split())
    assert "Do NOT run `state`" in collapsed, (
        "the buddy brief never forbids the `state` call its own `wait` answer already carries"
    )
    assert "`wait`" in _paragraph_naming(brief, "Do NOT run `state`"), (
        "the prohibition never names the answer that replaces the call, so an "
        "advisor is told not to read the board and not told where it already has it"
    )


def test_the_buddy_brief_still_sends_the_advisor_to_the_log() -> None:
    """CLI surface, the half that is NOT redundant, and the reason this is a
    per-surface decision rather than a blanket deletion. `seat_cli`'s `wait`
    prints `woken["state"]` alone - no `recent_log` - and `pass-until`, the
    one CLI wait whose answer carries the log tail, is refused outright for an
    advise seat (`seatkit.ADVISE_NO_PASS_UNTIL`). So `log` is the CLI buddy's
    only way to know what has happened, and telling it not to run one would
    leave it advising on a board with no history at all.

    The brief has to say that out loud, too: an advisor that reads "do not run
    `state`" and sees `log` beside it will assume the same applies."""
    brief = ai_seat.load_brief("advisor")
    collapsed = " ".join(brief.split())
    assert "Do NOT run `log`" not in collapsed, (
        "the buddy brief now forbids the one read a CLI advisor has no "
        "replacement for - see test_an_advisors_cli_wait_carries_the_state_but_no_log"
    )
    # The exception has to be argued where it is stated, not merely left
    # standing beside a prohibition that looks like it should cover it.
    exception = _paragraph_naming(brief, "`log` is different")
    assert "recent_log" in exception, (
        "the exception never names `recent_log`, the key a CLI advisor's "
        "`wait` answer does not carry - which is the whole reason `log` stays"
    )
    assert "pass-until" in exception, (
        "the exception never says that `pass-until` - the wait whose answer "
        "would have carried the log - is refused for an advisor, so it reads "
        "as an oversight rather than a decision"
    )


def test_the_advisor_wrapper_prose_forbids_both_reads_its_wait_answers() -> None:
    """MCP surface, where BOTH halves are redundant.
    `mcp.wait_for_priority` merges `recent_log` into the state view it hands
    back, and that view is the whole state, zones included - so an advisor's
    own wake loop already answers "what is the board" and "what happened",
    and both tools are escape hatches rather than ordinary reads. The prose
    presented them as the primary way to read the game."""
    collapsed = _collapsed_advisor_turn_zero()
    for tool in ("battlefield_state", "game_log"):
        assert f"Do NOT call `{tool}`" in collapsed, (
            f"the advisor's wrapper prose still offers `{tool}` as an ordinary "
            "read, when `wait_for_priority`'s own answer already carries what "
            "it would ask for"
        )
    assert "recent_log" in collapsed, (
        "the prose never names `recent_log`, the key that rides the advisor's "
        "own wake loop and replaces a `game_log` call"
    )


# ---------------------------------------------------------------------------
# the lobby's own create path, end to end
# ---------------------------------------------------------------------------


@requires_node
def test_a_game_created_through_the_lobby_form_with_the_buddy_registers_an_advisor(
    client: TestClient,
) -> None:
    """The real `ui/render/lobby.js` builds the create body from its form (the
    buddy toggled on), and that exact body - not a hand-written one - is what
    the server gets. It must mint an advisor for the human, holding the
    settings the form chose, and nothing under the name-keyed agent map."""
    from test_ui_lobby import FAKE_DOM, LOBBY_FORM

    captured = run_module(
        "render/lobby.js",
        FAKE_DOM + LOBBY_FORM + "submitCreate(opts)",
        opts={
            "youName": "you",
            "aiName": "bot",
            "youDeck": LEGAL_DECK,
            "aiDeck": LEGAL_DECK_B,
            "buddy": True,
            "buddyModel": "fable",
            "buddyEffort": "high",
        },
    )
    assert len(captured["posted"]) == 1, f"the lobby sent no create request: {captured}"
    response = client.post("/api/lobby/games", json=captured["posted"][0])
    assert response.status_code == 200, response.text
    created = response.json()
    assert list(created["advisor_uids"]) == ["you"]
    uid = created["advisor_uids"]["you"]
    advisors = store.advisor_uids(created["game_id"])
    assert list(advisors) == [uid]
    assert advisors[uid].player == "you"
    assert advisors[uid].agent == AgentSettings(model="fable", effort="high")
    assert set(store.agent_settings(created["game_id"])) == {"bot"}


def test_a_buddy_with_no_settings_runs_as_opus(client: TestClient) -> None:
    created = boot(client)
    uid = created["advisor_uids"]["you"]
    advisor = store.advisor_uids(created["game_id"])[uid]
    assert advisor.agent is not None
    assert advisor.agent.model == "opus"


def test_the_advisor_wrapper_prose_says_its_legal_plays_arrive_on_the_wake() -> None:
    """`mcp.wait_for_priority` hands an advise seat `available_actions` on a
    real wake (`test_advise_mode.
    test_an_advisors_mcp_wake_carries_the_legal_actions_with_their_targets`),
    target candidates and mana plan included. The prose has to say so where
    the wake loop is introduced, and must not send the advisor to look its
    legal plays up any other way - that lookup is the inference the list
    exists to save."""
    collapsed = _collapsed_advisor_turn_zero()
    start = collapsed.index("`wait_for_priority(player_uid, max_seconds=20)`")
    loop = collapsed[start : collapsed.index("Do NOT call `battlefield_state`", start)]
    for fact in ("available_actions", "candidates", "pays_with"):
        assert fact in loop, f"the advisor's wake-loop bullet never names `{fact}`"
    assert "legal-actions" not in collapsed, (
        "the advisor prose points at the legal-actions endpoint, a second way to "
        "look up what its own wake answer already carries"
    )
