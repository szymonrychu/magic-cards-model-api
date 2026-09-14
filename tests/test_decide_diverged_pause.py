"""A stored pause the replay no longer reproduces expires instead of wedging the game.

A paused command is stored as `(command, tape, PendingDecision)` and resumed by
replaying the command and comparing the replay's question with the stored one
exactly (`commands.resume_command`). Anything that changes how that question
comes out between the pause and the answer - a deploy that mints ids
differently after a board computation is the one that motivated this - makes
the comparison fail on every answer. Before this, every `POST .../decide`
answered 409 and left the pause standing, the same stuck-game shape as the
Cavern of Souls incident (2026-09-14, game 07c5f818).

Now the resume clears the stale pause and answers a 409 whose detail is stable
and tells the client to re-issue the original action, and the clients read it
as a refusal to refresh on rather than a request to re-send:

- the server, in memory and on a real `PostgresRepository`: the 409, the pause
  cleared, and the original action re-issued, paused and decided;
- a seat (`seatkit.run_action`): one decide on the wire, then a state read;
- the browser (`ui/app.js` `decide()`): one decide on the wire, then a board
  refresh.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from conftest import _ADMIN_KEYS, add_card, find, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from postgres_store_testkit import postgres_store
from test_ui_app import run_app

from magic_cards_model_api import store as store_module
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats, run_action

#: The 409's detail, pinned: the client is told the question expired and what
#: to do about it, and nothing about replays.
EXPIRED_DETAIL = (
    "This question has expired: the game no longer asks it the way it was stored "
    "(the server may have been updated since it was asked), so it has been cleared. "
    "Re-issue the original action to be asked again."
)


@pytest.fixture(params=["in-memory", "postgres"])
def repo(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    if request.param == "postgres":
        with postgres_store(monkeypatch) as postgres:
            yield postgres
    else:
        yield store_module.store


def new_game(client: TestClient) -> str:
    response = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    assert response.status_code == 200, response.text
    info = response.json()
    _ADMIN_KEYS[info["game_id"]] = info["admin_key"]
    return info["game_id"]


def seats_of(repo: Any, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in repo.seat_uids(game_id).items()}


def play_citadel(client: TestClient, game_id: str, alice: str) -> dict[str, Any]:
    """Sunken Citadel's "As Sunken Citadel enters, choose a color." pauses the
    land drop on `choose_a_quality` - the as-enters shape the incident had."""
    citadel = find(state(client, game_id, uid=alice)["hands"]["alice"], "Sunken Citadel")
    played = client.post(
        f"/api/games/{game_id}/actions/play-land",
        params={"uid": alice},
        json={"player": "alice", "card_id": citadel["id"]},
    )
    assert played.status_code == 200, played.text
    pending = played.json()["pending_decision"]
    assert pending["kind"] == "choose_a_quality", pending
    return pending


def stale_pause(client: TestClient, repo: Any) -> tuple[str, dict[str, str]]:
    """A real paused land drop whose stored question no longer matches what the
    replay asks: the stored `PendingDecision` is written back one `seq` off,
    the way a question minted by an older engine would read."""
    game_id = new_game(client)
    uids = seats_of(repo, game_id)
    force_turn_position(game_id, Step.PRECOMBAT_MAIN, "alice")
    add_card(client, game_id, "Sunken Citadel", "alice", "hand")
    play_citadel(client, game_id, uids["alice"])
    stored = repo.paused_command(game_id)
    assert stored is not None
    command, tape, pending = stored
    repo.store_paused_command(
        game_id, command, tape, dataclasses.replace(pending, seq=pending.seq + 1)
    )
    return game_id, uids


def decide_body(client: TestClient, game_id: str, alice: str) -> dict[str, Any]:
    pending = state(client, game_id, uid=alice)["pending_decision"]
    assert pending is not None and pending["kind"] == "choose_a_quality", pending
    return {
        "player": "alice",
        "choice": pending["options"][-1],
        "generation": pending["generation"],
    }


def test_a_decide_on_a_diverged_pause_expires_it_and_the_action_can_be_re_issued(
    repo: Any, client: TestClient
) -> None:
    game_id, uids = stale_pause(client, repo)
    alice = uids["alice"]
    try:
        answered = client.post(
            f"/api/games/{game_id}/decide",
            params={"uid": alice},
            json=decide_body(client, game_id, alice),
        )

        assert answered.status_code == 409, answered.text
        body = answered.json()
        assert body["detail"] == EXPIRED_DETAIL
        assert "diverged" not in body["detail"]
        assert body["expired"] is True
        assert body["accepted"] is False and body["allowed"] is False
        assert body["reason"] == EXPIRED_DETAIL
        assert repo.paused_command(game_id) is None, "the stale pause was cleared"
        assert state(client, game_id, uid=alice)["pending_decision"] is None

        pending = play_citadel(client, game_id, alice)
        decided = client.post(
            f"/api/games/{game_id}/decide",
            params={"uid": alice},
            json={
                "player": "alice",
                "choice": pending["options"][-1],
                "generation": pending["generation"],
            },
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["accepted"] is True, decided.json()
        assert repo.paused_command(game_id) is None
        board = state(client, game_id, uid=alice)
        assert find(board["battlefield"], "Sunken Citadel")
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": _ADMIN_KEYS.pop(game_id)})


def test_a_seat_handed_the_expired_question_reads_state_and_never_re_sends(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = store_module.store
    game_id, uids = stale_pause(client, repo)
    try:
        body = decide_body(client, game_id, uids["alice"])
        monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
        seat = Seats(api="http://testserver", game_id=game_id).seat_for(uids["alice"])
        sent: list[tuple[str, str]] = []
        original = seat.http.request

        def spy(method: str, path: str, **kwargs: Any) -> Any:
            sent.append((method, path))
            return original(method, path, **kwargs)

        monkeypatch.setattr(seat.http, "request", spy)
        result = run_action(
            seat, lambda: seat.call_json("POST", f"/api/games/{game_id}/decide", body)
        )

        decides = [index for index, (_, path) in enumerate(sent) if path.endswith("/decide")]
        assert len(decides) == 1, sent
        assert result["outcome"]["expired"] is True, result["outcome"]
        assert result["outcome"]["allowed"] is False
        assert result.get("stale_retry") is not True
        assert any(
            method == "GET" and path == f"/api/games/{game_id}"
            for method, path in sent[decides[0] + 1 :]
        ), sent
        assert result["state"]["pending_decision"] is None
        assert repo.paused_command(game_id) is None
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": _ADMIN_KEYS.pop(game_id)})


def test_the_browser_decide_refreshes_on_the_expired_question_and_never_re_sends() -> None:
    board = {
        "you": "alice",
        "pending_decision": {"player": "alice", "kind": "choose_a_quality", "generation": 3},
        "step": "precombat_main",
        "active_player": "alice",
        "priority_holder": "alice",
        "battlefield": [],
        "hands": {"alice": []},
        "stack": [],
        "combat": {"attacks": []},
        "winner": None,
        "drawn": False,
        "pregame": None,
    }
    expired = {
        "detail": EXPIRED_DETAIL,
        "reason": EXPIRED_DETAIL,
        "expired": True,
        "accepted": False,
        "allowed": False,
    }
    out = run_app(
        """
        (async () => {
          globalThis.__calls = [];
          const toastEl = { hidden: true, textContent: "" };
          globalThis.document = { querySelector: (sel) => (sel === "#toast" ? toastEl : null) };
          globalThis.fetch = (path, opts) => {
            const method = opts ? opts.method : "GET";
            globalThis.__calls.push({ method, path });
            if (method === "POST") {
              return Promise.resolve({ ok: false, status: 409, json: async () => expired });
            }
            if (path.includes("/log")) {
              return Promise.resolve({
                ok: true, status: 200, json: async () => ({ log: [], last_seq: 0 }),
              });
            }
            const fresh = { ...board, pending_decision: null };
            return Promise.resolve({ ok: true, status: 200, json: async () => fresh });
          };
          m.store.set({
            gameId: "g1", uid: "u1", state: board,
            pendingRelease: { seat: "alice", generation: 3, readSeq: 0 },
          });
          await m.decide({ choice: "White" });
          const calls = globalThis.__calls;
          const firstPost = calls.findIndex((c) => c.method === "POST");
          return {
            posts: calls.filter((c) => c.method === "POST").map((c) => c.path),
            readsAfter: calls
              .slice(firstPost + 1)
              .filter((c) => c.method === "GET" && !c.path.includes("/log")).length,
            toast: toastEl.hidden ? null : toastEl.textContent,
            release: m.store.get().pendingRelease ?? null,
            connection: m.store.get().connection ?? null,
            pending: m.store.get().state.pending_decision,
          };
        })()
        """,
        board=board,
        expired=expired,
    )

    assert len(out["posts"]) == 1 and "/decide" in out["posts"][0], out
    assert out["readsAfter"] >= 1, out
    assert out["toast"] == EXPIRED_DETAIL, out
    assert out["release"] is None, out
    assert not str(out["connection"] or "").startswith("error"), out
    assert out["pending"] is None, out
