"""Builds the `world` fixture: a fake game server and a fake wrapper, both
real FastAPI apps driven over ASGI by `TestClient` (itself an `httpx.Client`)
- no mocking library involved.

This is a plain builder function rather than a `conftest.py`, on purpose:
`pyproject.toml` documents that a second file literally named `conftest.py`
collides with the top-level `tests/conftest.py` that ~40 existing tests
import by bare name (`from conftest import ...`) - whichever one Python
imports first under the module name `conftest` wins for the whole session.
Every driver test module that wants `world` instead wraps `build_world` in
its own `@pytest.fixture def world(tmp_path): return build_world(tmp_path)`
(see `test_app.py`), which shares this one implementation without the name
collision.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from mtg_agent.driver.app import build_app
from mtg_agent.driver.config import DriverSettings

#: The refreshed fixture corpus's own plays for the one guide this world
#: carries, copied verbatim into its `decks/dimir-frog/plays/`.
FIXTURE_PLAYS = Path(__file__).resolve().parent / "fixtures" / "mtg-decks" / "dimir-frog" / "plays"

#: The uid a buddy dispatch runs under; the fake lobby hands it its human's
#: decklist the way the real `decks_api.game_seats` does.
ADVISOR_UID = "advisor-uid"

DECKLIST = "4 Psychic Frog\n4 Fatal Push\n2 Consign to Memory\n"

#: The guide's `archetype.json`, the shape `mtg-decks` writes and
#: `guides.load_signature` reads - two clauses this world's `DECKLIST`
#: satisfies, so the driver identifies the seat as `dimir-frog` confidently.
#: A guide directory WITHOUT one is unclassifiable (`guides.NONE`), which is
#: what `test_app.py`'s unguided-deck cases lean on.
ARCHETYPE = {
    "schema_version": 1,
    "archetype": "dimir frog",
    "signature": [
        {"cards": ["Psychic Frog", "Quantum Riddler"], "min": 4, "why": "the threats"},
        {"cards": ["Fatal Push", "Thoughtseize"], "min": 4, "why": "the cheap black answers"},
    ],
}


def build_world(tmp_path, clock=None, sleep=None) -> SimpleNamespace:
    guides = tmp_path / "guides" / "decks" / "dimir-frog"
    guides.mkdir(parents=True)
    (guides / "decklist.txt").write_text(DECKLIST)
    (guides / "archetype.json").write_text(json.dumps(ARCHETYPE))
    (guides / "GAMEPLAN_GUIDE.md").write_text("# gameplan\n")
    shutil.copytree(FIXTURE_PLAYS, guides / "plays")

    workspace = tmp_path / "workspace"
    state = {
        "winner": None,
        "drawn": False,
        #: The rows `GET /api/games/{id}/log` answers with.
        "log": [],
        #: Whether the handover marker existed when the session was deleted.
        "marker_at_delete": None,
        "decklist": DECKLIST,
        "agent": "up",
        #: With `agent` "busy", `/v1/messages` answers 409 - the wrapper still
        #: holding a turn - for the first `busy_for` pushes (every push when
        #: None), then accepts. `conflicts` counts every busy answer.
        "busy_for": None,
        "conflicts": 0,
        "game": "up",
        "deleted": 0,
        #: Whether the fake wrapper answers `/v1/session` at all - False
        #: stands in for an older wrapper that predates the route, so a
        #: test can assert the driver treats its 404 as "not supported,
        #: continue" rather than failing the dispatch.
        "session_supported": True,
        #: The coarse "what is the agent doing right now" word the real
        #: wrapper publishes on `GET /v1/session` from its transcript
        #: watcher (`mtg_agent/wrapper/activity.py`). `None` is a wrapper
        #: that has observed nothing yet.
        "activity": None,
        #: The API's `SCRIPTS_ENABLED` kill switch: False answers the scripts
        #: PUT with the real route's 409 `code: scripts_disabled` refusal.
        "scripts_enabled": True,
        #: "down" makes the scripts PUT alone unreachable, the rest of the
        #: game server still answering.
        "scripts_route": "up",
        #: `GET /api/games/{id}/review`'s own body - the "game record" the
        #: AI review prompt is built from. A minimal but real shape
        #: (`review.build_review`'s own keys), overridable per test.
        "review_record": {
            "players": ["bob", "alice"],
            "winner": "bob",
            "drawn": False,
            "declarations": [],
            "advice": [],
        },
        #: The fake wrapper's `/v1/review` (the post-game AI review's
        #: one-shot call): "up" answers with `review_text`, "down" is an
        #: unreachable wrapper, "error" is a `claude` failure (502).
        "wrapper_review": "up",
        "review_text": "a solid game overall, but the turn 6 attack was a misread",
    }
    #: The fake wrapper's own idea of its current model/effort/max_thinking_
    #: tokens - what `GET /v1/session` answers and `POST /v1/session` updates.
    #: Starts empty so a test can assert a push actually changed it.
    session_settings = {"model": "", "effort": "", "maxThinkingTokens": None}
    session_posts: list[dict] = []
    submitted: list[dict] = []
    reflection_posts: list[dict] = []
    #: Every `POST /api/games/{id}/ai-review` the driver sent.
    ai_review_posts: list[dict] = []
    #: Every `POST /v1/review` the driver sent to the fake wrapper.
    review_requests: list[dict] = []
    #: Every `PUT /api/games/{id}/seats/{uid}/scripts` the driver sent.
    script_puts: list[dict] = []
    #: What the fake wrapper and game server were asked, in order: `message`,
    #: `interrupt`, `probe`, `delete`, `reflection`, `review` and `ai_review`.
    events: list[str] = []
    interrupts: list[dict] = []
    probes: list[dict] = []
    #: The `uid` of every `GET .../log` read.
    log_reads: list[str | None] = []

    game = FastAPI()

    @game.get("/api/games/{game_id}")
    def get_game(game_id: str, uid: str):
        if state["game"] == "down":
            raise httpx.ConnectError("connection refused")
        return {
            "id": game_id,
            "players": [
                {"username": "bob"},
                {"username": "alice"},
            ],
            "winner": state["winner"],
            "drawn": state["drawn"],
        }

    @game.get("/api/games/{game_id}/log")
    def get_log(game_id: str, uid: str | None = None, since: int = 0):
        if state["game"] == "down":
            raise httpx.ConnectError("connection refused")
        log_reads.append(uid)
        return {"log": state["log"], "last_seq": len(state["log"])}

    @game.post("/api/games/{game_id}/reflection")
    def post_reflection(game_id: str, body: dict):
        # Mirrors the real `POST /api/games/{id}/reflection`'s only failure
        # mode a driver test cares about - an unreachable game server -
        # rather than reimplementing its uid/player/post-game gates, which
        # `test_review.py`'s own real-app tests already cover.
        if state["game"] == "down":
            raise httpx.ConnectError("connection refused")
        reflection_posts.append({"game_id": game_id, **body})
        events.append("reflection")
        return {"recorded": True, "log_seq": len(reflection_posts), "player": body.get("player")}

    @game.get("/api/games/{game_id}/review")
    def get_review(game_id: str, uid: str | None = None, key: str | None = None):
        if state["game"] == "down":
            raise httpx.ConnectError("connection refused")
        return state["review_record"]

    @game.post("/api/games/{game_id}/ai-review")
    def post_ai_review(game_id: str, body: dict):
        # Mirrors `post_reflection` above: only the failure mode a driver
        # test cares about (an unreachable game server) - the real route's
        # gates are `test_review.py`'s job.
        if state["game"] == "down":
            raise httpx.ConnectError("connection refused")
        ai_review_posts.append({"game_id": game_id, **body})
        events.append("ai_review")
        return {"recorded": True, "log_seq": len(ai_review_posts), "player": body.get("player")}

    @game.get("/api/lobby/games/{game_id}/seats")
    def get_seats(game_id: str, uid: str | None = None):
        # 2b's T8 moved `decklist` off `GET /api/games/{id}` onto this
        # lobby-owned route, redacted the same way `visibility._redact_player`
        # used to redact it on the state view: present only for the entry
        # whose own uid the caller presents.
        if state["game"] == "down":
            raise httpx.ConnectError("connection refused")
        return {
            "seats": [
                {
                    "player": "bob",
                    "uid": "secret-uid",
                    "decklist": state["decklist"] if uid in ("secret-uid", ADVISOR_UID) else None,
                },
                {"player": "alice", "uid": "other-uid", "decklist": None},
            ]
        }

    @game.put("/api/games/{game_id}/seats/{uid}/scripts")
    def put_scripts(game_id: str, uid: str, body: dict, request: Request):
        # The real route's credential rule, its kill switch and its answer
        # shape; judging each script is `test_script_routes.py`'s job, and
        # `test_app.py`'s real-app join test proves the driver's body passes it.
        if state["game"] == "down" or state["scripts_route"] == "down":
            raise httpx.ConnectError("connection refused")
        script_puts.append(
            {
                "game_id": game_id,
                "uid": uid,
                "caller": request.query_params.get("uid"),
                "body": body,
            }
        )
        if request.query_params.get("uid") != uid:
            raise HTTPException(status_code=404, detail="not found")
        if not state["scripts_enabled"]:
            reason = (
                "registering scripts was refused: agent-authored scripts are switched off "
                "on this deployment (SCRIPTS_ENABLED=false). Play the seat without scripts."
            )
            return JSONResponse(
                status_code=409,
                content={
                    "detail": reason,
                    "allowed": False,
                    "reason": reason,
                    "code": "scripts_disabled",
                },
            )
        return {
            "registered": True,
            "uid": uid,
            "deck_slug": body["deck_slug"],
            "guides_revision": body["guides_revision"],
            "count": len(body["scripts"]),
            "registered_at": "2026-09-13T00:00:00+00:00",
        }

    wrapper = FastAPI()

    @wrapper.post("/v1/messages", status_code=202)
    def post_message(body: dict):
        if state["agent"] == "busy":
            state["conflicts"] += 1
            if state["busy_for"] is None or state["conflicts"] <= state["busy_for"]:
                raise HTTPException(status_code=409, detail="session busy")
        if state["agent"] == "booting":
            raise HTTPException(status_code=503, detail="session not ready")
        if state["agent"] == "down":
            raise httpx.ConnectError("connection refused")
        if state["agent"] == "refusing":
            raise HTTPException(status_code=400, detail="text is required")
        submitted.append(body)
        events.append("message")
        return {"turnId": f"turn-{len(submitted)}"}

    @wrapper.delete("/v1/session", status_code=202)
    def delete_session():
        if state["agent"] == "down":
            raise httpx.ConnectError("connection refused")
        state["deleted"] += 1
        state["marker_at_delete"] = (workspace / "handover.done").exists()
        events.append("delete")
        # A real wrapper exits here; the replacement container boots for a
        # while before it can take a turn.
        state["agent"] = "booting"
        return {"stopping": True, "exiting": True}

    @wrapper.post("/v1/review")
    def post_review(body: dict):
        # Deliberately reads no state the interactive session touches
        # (`state["agent"]`) - the whole point of `/v1/review` is that it
        # never competes with, or waits on, a turn in flight.
        review_requests.append(body)
        events.append("review")
        if state["wrapper_review"] == "down":
            raise httpx.ConnectError("connection refused")
        if state["wrapper_review"] == "error":
            raise HTTPException(status_code=502, detail="claude exited 1")
        return {"text": state["review_text"]}

    @wrapper.post("/v1/interrupt", status_code=202)
    def post_interrupt():
        if state["agent"] == "down":
            raise httpx.ConnectError("connection refused")
        interrupts.append({})
        events.append("interrupt")
        return {"turnId": f"turn-{len(submitted)}"}

    @wrapper.post("/v1/probe", status_code=202)
    def post_probe(body: dict):
        if state["agent"] == "down":
            raise httpx.ConnectError("connection refused")
        probes.append(body)
        events.append("probe")
        return {"probeId": f"probe-{len(probes)}", "state": "pending"}

    @wrapper.get("/v1/session")
    def get_session():
        if state["agent"] == "down":
            raise httpx.ConnectError("connection refused")
        if not state["session_supported"]:
            raise HTTPException(status_code=404, detail="not found")
        return dict(session_settings) | {"activity": state["activity"]}

    @wrapper.post("/v1/session", status_code=202)
    def post_session(body: dict):
        if not state["session_supported"]:
            raise HTTPException(status_code=404, detail="not found")
        session_posts.append(body)
        session_settings.update(
            model=body.get("model", ""),
            effort=body.get("effort", ""),
            maxThinkingTokens=body.get("maxThinkingTokens"),
        )
        return {"restarting": True}

    def client_factory(base_url: str) -> httpx.Client:
        app = game if "game" in base_url else wrapper
        return TestClient(app, base_url=base_url)

    settings = DriverSettings(
        game_api="http://game:8000",
        wrapper_api="http://agent:8080",
        wrapper_secret="",
        guides_root=tmp_path / "guides",
        memory_root=tmp_path / "memory",
        workspace=workspace,
        self_url="http://driver:8090",
        http_addr=":8090",
        agent_boot_wait=1.0,
        agent_retry_interval=0.01,
    )
    client = TestClient(
        build_app(settings, client_factory=client_factory, clock=clock, sleep=sleep)
    )
    return SimpleNamespace(
        client=client,
        settings=settings,
        submitted=submitted,
        wrapper_posts=submitted,
        reflection_posts=reflection_posts,
        ai_review_posts=ai_review_posts,
        review_requests=review_requests,
        script_puts=script_puts,
        session_settings=session_settings,
        session_posts=session_posts,
        state=state,
        uid="secret-uid",
        game_id="g-1",
        client_factory=client_factory,
        events=events,
        interrupts=interrupts,
        probes=probes,
        log_reads=log_reads,
    )
