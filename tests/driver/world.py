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
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mtg_agent.driver.app import build_app
from mtg_agent.driver.config import DriverSettings

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


def build_world(tmp_path) -> SimpleNamespace:
    guides = tmp_path / "guides" / "decks" / "dimir-frog"
    guides.mkdir(parents=True)
    (guides / "decklist.txt").write_text(DECKLIST)
    (guides / "archetype.json").write_text(json.dumps(ARCHETYPE))
    (guides / "GAMEPLAN_GUIDE.md").write_text("# gameplan\n")

    state = {
        "winner": None,
        "decklist": DECKLIST,
        "agent": "up",
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
    }
    #: The fake wrapper's own idea of its current model/effort/max_thinking_
    #: tokens - what `GET /v1/session` answers and `POST /v1/session` updates.
    #: Starts empty so a test can assert a push actually changed it.
    session_settings = {"model": "", "effort": "", "maxThinkingTokens": None}
    session_posts: list[dict] = []
    submitted: list[dict] = []
    reflection_posts: list[dict] = []

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
        }

    @game.post("/api/games/{game_id}/reflection")
    def post_reflection(game_id: str, body: dict):
        # Mirrors the real `POST /api/games/{id}/reflection`'s only failure
        # mode a driver test cares about - an unreachable game server -
        # rather than reimplementing its uid/player/post-game gates, which
        # `test_review.py`'s own real-app tests already cover.
        if state["game"] == "down":
            raise httpx.ConnectError("connection refused")
        reflection_posts.append({"game_id": game_id, **body})
        return {"recorded": True, "log_seq": len(reflection_posts), "player": body.get("player")}

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
                    "decklist": state["decklist"] if uid == "secret-uid" else None,
                },
                {"player": "alice", "uid": "other-uid", "decklist": None},
            ]
        }

    wrapper = FastAPI()

    @wrapper.post("/v1/messages", status_code=202)
    def post_message(body: dict):
        if state["agent"] == "booting":
            raise HTTPException(status_code=503, detail="session not ready")
        if state["agent"] == "down":
            raise httpx.ConnectError("connection refused")
        submitted.append(body)
        return {"turnId": f"turn-{len(submitted)}"}

    @wrapper.delete("/v1/session", status_code=202)
    def delete_session():
        if state["agent"] == "down":
            raise httpx.ConnectError("connection refused")
        state["deleted"] += 1
        # A real wrapper exits here; the replacement container boots for a
        # while before it can take a turn.
        state["agent"] = "booting"
        return {"stopping": True, "exiting": True}

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
        workspace=tmp_path / "workspace",
        self_url="http://driver:8090",
        http_addr=":8090",
        agent_boot_wait=1.0,
        agent_retry_interval=0.01,
    )
    client = TestClient(build_app(settings, client_factory=client_factory))
    return SimpleNamespace(
        client=client,
        settings=settings,
        submitted=submitted,
        wrapper_posts=submitted,
        reflection_posts=reflection_posts,
        session_settings=session_settings,
        session_posts=session_posts,
        state=state,
        uid="secret-uid",
        game_id="g-1",
        client_factory=client_factory,
    )
