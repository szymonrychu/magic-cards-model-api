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

from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mtg_agent.driver.app import build_app
from mtg_agent.driver.config import DriverSettings

DECKLIST = "4 Psychic Frog\n4 Fatal Push\n2 Consign to Memory\n"


def build_world(tmp_path) -> SimpleNamespace:
    guides = tmp_path / "guides" / "decks" / "dimir-frog"
    guides.mkdir(parents=True)
    (guides / "decklist.txt").write_text(DECKLIST)
    (guides / "GAMEPLAN_GUIDE.md").write_text("# gameplan\n")

    state = {"winner": None, "decklist": DECKLIST, "agent": "up", "game": "up", "deleted": 0}
    submitted: list[dict] = []

    game = FastAPI()

    @game.get("/api/games/{game_id}")
    def get_game(game_id: str, uid: str):
        if state["game"] == "down":
            raise httpx.ConnectError("connection refused")
        return {
            "id": game_id,
            "players": [
                {"username": "bob", "decklist": state["decklist"]},
                {"username": "alice"},
            ],
            "winner": state["winner"],
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
        state=state,
        uid="secret-uid",
        game_id="g-1",
        client_factory=client_factory,
    )
