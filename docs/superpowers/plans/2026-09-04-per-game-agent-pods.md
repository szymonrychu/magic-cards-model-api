# Per-Game Agent Pods Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Several games at once, each AI seat played by its own Kubernetes Job (agent plus driver) created on demand by the API, with model, effort and thinking tokens chosen at game creation; compose and the two existing AI seat modes keep working.

**Architecture:** `AI_SEAT_MODE=kubernetes` adds a third runner to `ai_seat.registry`: `k8s_seats.py` builds one Job per AI seat from env-driven settings, watches Jobs by label and reconciles every 60 s. The driver learns to dispatch itself from `DRIVER_ASSIGNMENT_JSON` and exit when the match is over, so a Job completes. The lobby lists live games instead of resetting them, and per-player `agent` settings flow from the request through the store into the Job env (or the subprocess argv, or the wrapper dispatch).

**Tech Stack:** Python 3.13, FastAPI, pydantic, `kubernetes` client 36.x (new dependency), pytest; Kubernetes manifests under `deploy/k8s`.

**Spec:** `docs/superpowers/specs/2026-09-04-per-game-agent-pods-design.md`. Repo: `/Users/szymonri/Documents/magic-cards-model-api`, branch `play-ui-and-agent-pods`. Depends on the play UI plan only for the lobby screen (Task 9 here); everything else is independent.

## Global Constraints

- No mocks in Python tests except the one place the spec allows: the Kubernetes API client in `tests/test_k8s_seats.py` is a fake `BatchV1Api`/`CoreV1Api` (a cluster in CI is the alternative). Everything else boots real games through `TestClient`.
- Effort values: exactly `low`, `medium`, `high`, `xhigh`, `max`; default `high`. Model default from `AGENT_DEFAULT_MODEL`, default `sonnet`.
- `MAX_LIVE_GAMES` default 4; `AGENT_JOB_DEADLINE_SECONDS` default 14400; `ttlSecondsAfterFinished` 600; `backoffLimit` 0; `restartPolicy: Never`.
- Job name `mtg-seat-<game_id[:8]>-<uid[:8]>`; labels `app.kubernetes.io/name=mtg-agent`, `mtg/game-id=<game_id>`, `mtg/seat-uid=<uid>`.
- Writing: plain hyphens and straight quotes. `uv run ruff format .` before each commit. No attribution lines in commits.
- Commands: `mise run test`, `mise run lint`; `mise run k8s:render` to see manifests; `mise run k8s:deploy` to ship.

## File map

Modify: `pyproject.toml` (dependency), `src/magic_cards_model_api/decks_api.py` (creation request, lobby list, end game), `store.py` (agent settings, live games), `serialize.py` (`agent` on the player view), `ai_seat.py` (mode dispatch, subprocess argv, agent settings in wrapper dispatch), `ai_seat_api.py` (status merge), `lobby.py` (no more implicit reset), `src/mtg_agent/driver/app.py` + `config.py` + `__main__.py` (self-dispatch, exit), `src/mtg_agent/wrapper/config.py` + `pty_proc.py` + `session.py` + `metrics.py` (effort, thinking tokens), `deploy/k8s/*.yaml`, `deploy/k8s/README.md`, `README.md`, `docker-compose.yml`, `ui/render/lobby.js` (from the UI plan) or `ui/app.js` (old) for the lobby fields.

Create: `src/magic_cards_model_api/agent_settings.py`, `src/magic_cards_model_api/k8s_seats.py`, `deploy/k8s/40-rbac.yaml`, `deploy/k8s/45-networkpolicy.yaml`, tests listed per task.

---

### Task 1: `AgentSettings` model and defaults

**Files:**
- Create: `src/magic_cards_model_api/agent_settings.py`
- Test: `tests/test_agent_settings.py`

**Interfaces:**
- Produces: `AgentSettings(BaseModel)` with `model: str`, `effort: Literal["low","medium","high","xhigh","max"]`, `max_thinking_tokens: int | None`; `AgentSettings.default(env) -> AgentSettings`; `.env() -> dict[str, str]` giving `{"MODEL": ..., "EFFORT": ..., "MAX_THINKING_TOKENS": ...}` (the last only when set); `.claude_args() -> list[str]` giving `["--model", m, "--effort", e]`.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

import pytest
from magic_cards_model_api.agent_settings import AgentSettings
from pydantic import ValidationError


def test_defaults_come_from_env() -> None:
    settings = AgentSettings.default({"AGENT_DEFAULT_MODEL": "opus"})
    assert settings.model == "opus"
    assert settings.effort == "high"
    assert settings.max_thinking_tokens is None
    assert AgentSettings.default({}).model == "sonnet"


def test_effort_is_validated() -> None:
    with pytest.raises(ValidationError):
        AgentSettings(model="sonnet", effort="turbo")
    with pytest.raises(ValidationError):
        AgentSettings(model="", effort="high")
    with pytest.raises(ValidationError):
        AgentSettings(model="sonnet", effort="high", max_thinking_tokens=-1)


def test_env_and_args() -> None:
    settings = AgentSettings(model="fable", effort="max", max_thinking_tokens=0)
    assert settings.env() == {"MODEL": "fable", "EFFORT": "max", "MAX_THINKING_TOKENS": "0"}
    assert settings.claude_args() == ["--model", "fable", "--effort", "max"]
    assert "MAX_THINKING_TOKENS" not in AgentSettings(model="sonnet", effort="high").env()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_agent_settings.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```python
"""What an AI seat runs as: the Claude Code model and effort, and an optional
thinking budget. Chosen per game in the lobby, carried into whichever runner
plays the seat (subprocess argv, wrapper dispatch, or a Job's env).

Names follow Claude Code's own: `--model`, `--effort` (low, medium, high,
xhigh, max) and the `MAX_THINKING_TOKENS` environment variable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field

Effort = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_MODEL_ENV = "AGENT_DEFAULT_MODEL"


class AgentSettings(BaseModel):
    model: str = Field(min_length=1)
    effort: Effort = "high"
    max_thinking_tokens: int | None = Field(default=None, ge=0)

    @classmethod
    def default(cls, env: Mapping[str, str]) -> AgentSettings:
        return cls(model=(env.get(DEFAULT_MODEL_ENV) or "sonnet").strip() or "sonnet")

    def env(self) -> dict[str, str]:
        out = {"MODEL": self.model, "EFFORT": self.effort}
        if self.max_thinking_tokens is not None:
            out["MAX_THINKING_TOKENS"] = str(self.max_thinking_tokens)
        return out

    def claude_args(self) -> list[str]:
        return ["--model", self.model, "--effort", self.effort]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_agent_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Name what an AI seat runs as: model, effort, thinking budget"
```

### Task 2: agent settings in game creation, store, and state view

**Files:**
- Modify: `src/magic_cards_model_api/decks_api.py:152-241`, `src/magic_cards_model_api/store.py` (new `register_agent_settings`/`agent_settings`, cleared in `delete`), `src/magic_cards_model_api/serialize.py` (`_player_view` gains `agent` when provided; `state_view` reads it from the store the way `decklist` is read)
- Test: `tests/test_game_agent_settings.py`

**Interfaces:**
- Consumes: `AgentSettings`.
- Produces: `NewUiPlayer.agent: AgentSettings | None`; `store.register_agent_settings(game_id, {username: AgentSettings})`, `store.agent_settings(game_id) -> dict[str, AgentSettings]`; the AI player's block in `GET /api/games/{id}` carries `agent: {model, effort, max_thinking_tokens}` for every viewer; the creation response carries `agents: {username: {...}}`.

- [ ] **Step 1: Write the failing test**

`tests/test_decks_api.py` defines `LEGAL_DECK` and `LEGAL_DECK_B` (52 basics plus Grizzly Bears and Lightning Bolt); import them under the names below.

```python
from __future__ import annotations

from fastapi.testclient import TestClient
from test_decks_api import LEGAL_DECK as DECK_A, LEGAL_DECK_B as DECK_B


def create(client: TestClient, agent: dict | None = None, ai: bool = True) -> dict:
    bot = {"username": "bot", "decklist_text": DECK_B, "ai": ai}
    if agent is not None:
        bot["agent"] = agent
    return client.post(
        "/api/ui/games", json={"players": [{"username": "you", "decklist_text": DECK_A}, bot]}
    ).json()


def test_agent_settings_default_and_show_on_the_player(client: TestClient) -> None:
    created = create(client)
    assert created["agents"] == {
        "bot": {"model": "sonnet", "effort": "high", "max_thinking_tokens": None}
    }
    state = client.get(
        f"/api/games/{created['game_id']}", params={"uid": created["seat_uids"]["you"]}
    ).json()
    bot = next(p for p in state["players"] if p["username"] == "bot")
    assert bot["agent"] == {"model": "sonnet", "effort": "high", "max_thinking_tokens": None}
    you = next(p for p in state["players"] if p["username"] == "you")
    assert "agent" not in you


def test_agent_settings_are_stored_as_given(client: TestClient) -> None:
    created = create(client, {"model": "opus", "effort": "xhigh", "max_thinking_tokens": 8000})
    assert created["agents"]["bot"] == {
        "model": "opus",
        "effort": "xhigh",
        "max_thinking_tokens": 8000,
    }


def test_agent_on_a_human_seat_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/ui/games",
        json={
            "players": [
                {
                    "username": "you",
                    "decklist_text": DECK_A,
                    "agent": {"model": "opus", "effort": "high"},
                },
                {"username": "bot", "decklist_text": DECK_B, "ai": True},
            ]
        },
    )
    assert response.status_code == 400
    assert "agent" in response.json()["detail"]
```

Note `ai_seat.registry.start` is not called by creation in the test client unless `AI_SEAT_MODE` says so; check `tests/test_ai_seat.py` for how the existing tests keep `claude` from being spawned (an env var or a monkeypatched `claude_available`) and use the same guard via the shared `client` fixture.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_game_agent_settings.py -v`
Expected: FAIL (`KeyError: 'agents'`).

- [ ] **Step 3: Implement**

`decks_api.py`:

```python
from .agent_settings import AgentSettings


class NewUiPlayer(BaseModel):
    username: str = Field(min_length=1)
    decklist_text: str = Field(min_length=1)
    ai: bool = False
    agent: AgentSettings | None = None
    """How the AI seat runs (model, effort, thinking). Only with `ai: true`;
    absent, the server's defaults apply (`AgentSettings.default`)."""
```

In `start_game_from_decklists`, after validation:

```python
    for player in req.players:
        if player.agent is not None and not player.ai:
            raise ValueError(f"{player.username}: agent settings are only for an ai seat")
    agents = {
        player.username: player.agent or AgentSettings.default(os.environ)
        for player in req.players
        if player.ai
    }
    ...
    store.register_agent_settings(game_id, agents)
    ...
    return {..., "agents": {name: settings.model_dump() for name, settings in agents.items()}}
```

`store.py`: add `self._agent_settings: dict[str, dict[str, AgentSettings]] = {}` in `__init__`, `register_agent_settings`/`agent_settings` under `self._lock` in the style of `register_decklists`/`decklists`, and pop it in `delete`.

`serialize.py`: `state_view(game, game_id)` already looks up decklists per player; find that call (`grep -n "decklists" serialize.py`) and pass `agent=agents.get(player.username)` into `_player_view`, which adds `"agent": agent.model_dump()` when not `None`. Do the same lookup lazily through `store` the way `decklist` is obtained, so `serialize` keeps no new import cycle.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_game_agent_settings.py tests/test_decks_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Choose the agent's model, effort and thinking budget when a game is created"
```

### Task 3: lobby lists live games, cap, no reset, end game

**Files:**
- Modify: `src/magic_cards_model_api/decks_api.py` (creation no longer calls `lobby.reset_previous_games`; new `GET /api/ui/games`, `DELETE /api/ui/games/{id}`), `src/magic_cards_model_api/lobby.py` (keep `reset_game`; delete `reset_previous_games` and `record_reset`, and rewrite the module docstring to say End game is the only reset), `src/magic_cards_model_api/store.py` (`live_ui_games()`)
- Test: `tests/test_lobby_games.py`; rewrite `tests/test_lobby_reset.py` and `tests/driver/test_lobby_reset_releases_the_agent.py` to assert the End game path instead of the implicit reset.

**Interfaces:**
- Produces: `GET /api/ui/games?uids=a,b,c` -> `{"games": [{game_id, players: [username], turn_number, step, winner, drawn, agents: {username: {...}}, seats: {username: uid} (only uids the caller passed that belong to this game), created_at}]}`; `DELETE /api/ui/games/{id}?uid=` (a seat uid of that game, or the admin key as `key=`) -> `lobby.reset_game(game_id, reason="ended from the lobby")` result; `MAX_LIVE_GAMES` enforced on `POST /api/ui/games` with 409.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_game_agent_settings import create


def test_a_second_game_does_not_end_the_first(client: TestClient) -> None:
    first = create(client)
    second = create(client)
    assert client.get(f"/api/games/{first['game_id']}").status_code == 200
    assert client.get(f"/api/games/{second['game_id']}").status_code == 200
    assert "reset" not in second


def test_lobby_lists_live_games_with_only_your_uids(client: TestClient) -> None:
    first = create(client)
    second = create(client)
    my_uid = first["seat_uids"]["you"]
    listing = client.get("/api/ui/games", params={"uids": my_uid}).json()["games"]
    by_id = {g["game_id"]: g for g in listing}
    assert set(by_id) == {first["game_id"], second["game_id"]}
    assert by_id[first["game_id"]]["seats"] == {"you": my_uid}
    assert by_id[second["game_id"]]["seats"] == {}
    assert by_id[first["game_id"]]["agents"]["bot"]["model"] == "sonnet"


def test_end_game_deletes_it(client: TestClient) -> None:
    created = create(client)
    response = client.delete(
        f"/api/ui/games/{created['game_id']}", params={"uid": created["seat_uids"]["you"]}
    )
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert client.get(f"/api/games/{created['game_id']}").status_code == 404
    assert client.get("/api/ui/games").json()["games"] == []


def test_end_game_needs_a_seat_of_that_game(client: TestClient) -> None:
    first = create(client)
    second = create(client)
    response = client.delete(
        f"/api/ui/games/{first['game_id']}", params={"uid": second["seat_uids"]["you"]}
    )
    assert response.status_code == 404


def test_live_game_cap(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_LIVE_GAMES", "2")
    create(client)
    create(client)
    bot = {"username": "bot", "decklist_text": create.__globals__["DECK_B"], "ai": True}
    response = client.post(
        "/api/ui/games",
        json={"players": [{"username": "you", "decklist_text": create.__globals__["DECK_A"]}, bot]},
    )
    assert response.status_code == 409
    assert "2" in response.json()["detail"]
```

Import `DECK_A`/`DECK_B` the way `tests/test_game_agent_settings.py` does.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_lobby_games.py -v`
Expected: FAIL (first game 404 after the second is created; no `GET /api/ui/games`).

- [ ] **Step 3: Implement**

`store.py`: `live_ui_games()` returns `[game_id for game_id in self.ui_games() if not self._games[game_id].is_over]` (`Game.is_over` is the engine's CR 104.1 property: a winner or a draw).

`decks_api.py`:

```python
import os
from fastapi import HTTPException

def max_live_games() -> int:
    return int(os.environ.get("MAX_LIVE_GAMES", "4") or 4)


@router.post("/games")
def start_game_from_decklists(req: NewUiGame) -> dict[str, Any]:
    ...validation as before...
    live = store.live_ui_games()
    if len(live) >= max_live_games():
        raise HTTPException(
            status_code=409,
            detail=f"{len(live)} games are already live, the cap is {max_live_games()}; end one from the lobby first",
        )
    # no reset: several games live side by side; End game is the only reset
    match = load_deck_into_game(...)
    ...
    return {"game_id": game_id, "seat_uids": seat_uids, "admin_key": ..., "ai_players": ..., "agents": ...}


@router.get("/games")
def list_lobby_games(uids: str | None = None) -> dict[str, Any]:
    mine = {u for u in (uids or "").split(",") if u}
    games = []
    for game_id in store.ui_games():
        game = store.get(game_id)
        if game is None:
            continue
        seats = {name: uid for uid, name in store.seat_uids(game_id).items() if uid in mine}
        view = serialize.state_view(game, game_id)  # turn_number, step, winner, drawn as the seat view reports them
        games.append({
            "game_id": game_id,
            "players": [p.username for p in game.players],
            "turn_number": view["turn_number"],
            "step": view["step"],
            "winner": game.winner,
            "drawn": game.drawn,
            "agents": {name: s.model_dump() for name, s in store.agent_settings(game_id).items()},
            "seats": seats,
        })
    return {"games": games}


@router.delete("/games/{game_id}")
def end_lobby_game(game_id: str, uid: str | None = None, key: str | None = None) -> dict[str, Any]:
    if store.get(game_id) is None:
        raise KeyError(f"no game {game_id}")
    allowed = (uid is not None and store.player_for_uid(game_id, uid) is not None) or (key is not None and store.is_admin(game_id, key))
    if not allowed:
        raise KeyError("that uid is no seat in this game")
    return lobby.reset_game(game_id, reason="ended from the lobby")
```

`serialize.state_view` is the one place that already knows how to read the turn number and step off a `Game`; reuse it rather than reading `Game` attributes directly (it is cheap enough for a lobby list, and it stays correct if the engine renames a field). Remove `lobby.reset_previous_games` and `lobby.record_reset` and every caller; rewrite `tests/test_lobby_reset.py` so each test creates two games and asserts both live, then ends one and asserts the reset audit entry (`game_reset` with reason `ended from the lobby`) and the AI seat release; rewrite `tests/driver/test_lobby_reset_releases_the_agent.py` to trigger the release through `DELETE /api/ui/games/{id}`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_lobby_games.py tests/test_lobby_reset.py tests/driver/test_lobby_reset_releases_the_agent.py tests/test_decks_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Let games live side by side; End game is the only reset"
```

### Task 4: subprocess and wrapper runners honour agent settings

**Files:**
- Modify: `src/magic_cards_model_api/ai_seat.py:119-132` (`command_for`), `:245-282` (`_dispatch_to_driver` payload), `AiSeatRunner.start` env, `AiSeatRegistry.start(..., agent: AgentSettings)`; `src/magic_cards_model_api/ai_seat_api.py` (start passes the stored settings); `src/magic_cards_model_api/decks_api.py` (creation starts the AI seat with them); `src/mtg_agent/driver/app.py` `DispatchIn` gains `agent: dict | None`
- Test: `tests/test_ai_seat_settings.py`, extend `tests/driver/test_ai_seat_wrapper_mode.py`

**Interfaces:**
- Produces: `command_for(prompt, agent) -> list[str]` with `["claude", "--print", *agent.claude_args(), "--dangerously-skip-permissions", "--allowedTools", "Bash", "--", prompt]`; `AiSeatRunner.start` passes `agent.env()` merged into the child env; the driver dispatch payload gains `"agent": agent.model_dump()`; `AiSeatRegistry.start(*, uid, game_id, player, api_base, agent)`.

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from magic_cards_model_api import ai_seat
from magic_cards_model_api.agent_settings import AgentSettings


def test_argv_carries_model_and_effort() -> None:
    argv = ai_seat.command_for("hello", AgentSettings(model="opus", effort="max"))
    assert argv[:6] == ["claude", "--print", "--model", "opus", "--effort", "max"]
    assert argv[-2:] == ["--", "hello"]


def test_dispatch_payload_carries_agent(monkeypatch) -> None:
    captured = {}

    class Response:
        status_code = 202

        def json(self):
            return {"deck": None, "turnId": "t1", "pending": False}

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, path, json):
            captured["path"], captured["json"] = path, json
            return Response()

    monkeypatch.setattr(ai_seat, "_driver_client", lambda base: Client())
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    registry = ai_seat.AiSeatRegistry()
    registry.start(
        uid="u1",
        game_id="g1",
        player="bot",
        api_base="http://x",
        agent=AgentSettings(model="fable", effort="low", max_thinking_tokens=0),
    )
    assert captured["json"]["agent"] == {
        "model": "fable",
        "effort": "low",
        "max_thinking_tokens": 0,
    }
```

The `Client` above stands in for an HTTP client to the driver, which is the process boundary the existing `tests/driver/test_ai_seat_wrapper_mode.py` already fakes the same way; follow that file's pattern exactly if it differs.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_ai_seat_settings.py -v`
Expected: FAIL (`TypeError: unexpected keyword 'agent'`).

- [ ] **Step 3: Implement**

`ai_seat.py`:

```python
def command_for(prompt: str, agent: AgentSettings) -> list[str]:
    return [
        "claude",
        "--print",
        *agent.claude_args(),
        "--dangerously-skip-permissions",
        "--allowedTools",
        "Bash",
        "--",
        prompt,
    ]
```

`AiSeatRunner.start(self)` gets `env={**os.environ, **self.agent.env()}` on its `Popen`; store `agent` on the runner dataclass. `_dispatch_to_driver(..., agent)` adds `"agent": agent.model_dump()` to `payload`. `AiSeatRegistry.start` takes `agent: AgentSettings` and threads it to both. `ai_seat_api.start_ai_seat` reads `store.agent_settings(game_id).get(player) or AgentSettings.default(os.environ)`. `decks_api.start_game_from_decklists` calls `ai_seat.registry.start(...)` for each AI seat right after `ai_pregame.start`, using `request: Request` for `api_base` as `ai_seat_api._api_base` does; keep the separate start route. Driver: `DispatchIn.agent: dict | None = None`, stored on `Assignment` as `agent` and shown in `/status`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ai_seat_settings.py tests/test_ai_seat.py tests/driver -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Run the subprocess and wrapper seats with the chosen model and effort"
```

### Task 5: wrapper gains `--effort` and `MAX_THINKING_TOKENS`

**Files:**
- Modify: `src/mtg_agent/wrapper/config.py` (`Settings.effort`, `max_thinking_tokens`, `claude_args`), `src/mtg_agent/wrapper/pty_proc.py:93-107` (child env), `src/mtg_agent/wrapper/session.py:509` (`snapshot`), `src/mtg_agent/wrapper/metrics.py:34-50` (`effort` label)
- Test: `tests/wrapper/test_settings.py` (find the existing wrapper settings tests with `grep -rln "Settings.from_env" tests` and extend that file)

**Interfaces:**
- Produces: `EFFORT` env -> `--effort <value>` in `claude_args`; `MAX_THINKING_TOKENS` env passed through unchanged to the child (it already is, since `env = dict(os.environ)`; the change is only that `Settings` records it for `snapshot`).

- [ ] **Step 1: Write the failing test**

```python
def test_effort_becomes_a_flag() -> None:
    settings = Settings.from_env(
        {"MODEL": "opus", "EFFORT": "xhigh", "MAX_THINKING_TOKENS": "4000"}
    )
    assert settings.claude_args(resume=False) == ["--model", "opus", "--effort", "xhigh"]
    assert settings.max_thinking_tokens == 4000


def test_no_effort_no_flag() -> None:
    assert "--effort" not in Settings.from_env({"MODEL": "opus"}).claude_args(resume=False)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/wrapper -k effort -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`config.py`: fields `effort: str = ""`, `max_thinking_tokens: int | None = None`; `from_env` reads `EFFORT` with `_str` and `MAX_THINKING_TOKENS` as `int` when non-empty; `claude_args` appends `["--effort", self.effort]` when set. `session.snapshot` adds `"effort": self.settings.effort, "maxThinkingTokens": self.settings.max_thinking_tokens`. `metrics.record_tokens` gains an `effort` label taken from the settings (pass `effort` into `Metrics.__init__` or set it once with a `set_effort` method; keep the label present even when empty).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/wrapper -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Let the wrapper run claude at a chosen effort"
```

### Task 6: driver self-dispatch from env and exit after retire

**Files:**
- Modify: `src/mtg_agent/driver/config.py` (`assignment_json: str`, `exit_after_match: bool`), `src/mtg_agent/driver/app.py` (factor the body of `dispatch` into `assign(body: DispatchIn) -> dict` used by both the route and a startup hook; `retire` schedules process exit when `exit_after_match`), `src/mtg_agent/driver/__main__.py`
- Test: `tests/driver/test_self_dispatch.py` (use the same fake wrapper and game-server clients `tests/driver/test_app.py` uses via `client_factory`)

**Interfaces:**
- Produces: env `DRIVER_ASSIGNMENT_JSON='{"uid":..,"game_id":..,"player":..,"api_base":..,"agent":{..}}'` makes `build_app` dispatch it on startup (FastAPI `startup` event or a call right after `build_app` in `__main__`); env `DRIVER_EXIT_AFTER_MATCH=1` makes `retire` set `app.state.should_exit = True` and `__main__` stop uvicorn (`server.should_exit = True` the way `wrapper/__main__.py:58-65` does) so the container exits 0.

- [ ] **Step 1: Write the failing test**

`tests/driver/test_app.py` has a `world` fixture: a fake game server and a fake wrapper, both real FastAPI apps served through an `httpx` `client_factory(base_url)` that routes `game`/`agent` hosts to them, recording the wrapper's posts. Reuse it: move that fixture into `tests/driver/conftest.py` (unchanged) so this file can take `world` too, then:

```python
def test_assignment_from_env_is_dispatched_at_startup(world, tmp_path) -> None:
    settings = DriverSettings.from_env({
        "DRIVER_ASSIGNMENT_JSON": json.dumps({"uid": world.uid, "game_id": world.game_id, "player": "bot", "api_base": "http://game:8000", "agent": {"model": "sonnet", "effort": "high", "max_thinking_tokens": None}}),
        "DRIVER_WORKSPACE": str(tmp_path), "MEMORY_ROOT": str(tmp_path / "memory"), "GUIDES_ROOT": str(tmp_path / "guides"),
        "GAME_API": "http://game:8000", "WRAPPER_API": "http://agent:8080",
    })
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:  # runs the startup hook
        status = client.get("/status").json()
    assert status["gameId"] == world.game_id
    assert world.wrapper_posts[0]["text"].startswith("You are")  # turn 0 pushed


def test_retire_requests_exit_when_told_to(world, tmp_path) -> None:
    settings = DriverSettings.from_env({"DRIVER_EXIT_AFTER_MATCH": "1", "DRIVER_WORKSPACE": str(tmp_path), "MEMORY_ROOT": str(tmp_path / "memory"), "GUIDES_ROOT": str(tmp_path / "guides"), "GAME_API": "http://game:8000", "WRAPPER_API": "http://agent:8080"})
    app = build_app(settings, client_factory=world.client_factory)
    with TestClient(app) as client:
        client.post("/dispatch", json={"uid": world.uid, "game_id": world.game_id, "player": "bot", "api_base": "http://game:8000"})
        client.post("/release", json={"uid": world.uid, "game_id": world.game_id})
    assert app.state.should_exit is True
```

Adapt the attribute names (`world.uid`, `world.game_id`, `world.client_factory`, `world.wrapper_posts`) to whatever the fixture actually exposes; if it returns a tuple or dict today, give it a small `SimpleNamespace` return so both files read it the same way.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/driver/test_self_dispatch.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`config.py`: `assignment_json: str = ""` from `DRIVER_ASSIGNMENT_JSON`, `exit_after_match: bool = False` from `DRIVER_EXIT_AFTER_MATCH in ("1", "true", "yes")`. `app.py`: move the body of `dispatch` into `def assign(body: DispatchIn) -> dict` (same 409 logic, same pending retry); the route calls it; add:

```python
app.state.should_exit = False


@app.on_event("startup")
def dispatch_from_env() -> None:
    if not settings.assignment_json:
        return
    body = DispatchIn.model_validate_json(settings.assignment_json)
    try:
        assign(body)
    except HTTPException as exc:
        log.error("self-dispatch failed: %s", exc.detail)
        current["lastError"] = str(exc.detail)
```

and in `retire`, after `clear_locked()`: `if settings.exit_after_match: app.state.should_exit = True; log.info("match over, exiting")`. In `__main__.py`, build a `uvicorn.Server(uvicorn.Config(app, ...))` and run it in the main thread while a daemon thread polls `app.state.should_exit` every second and sets `server.should_exit = True` (the pattern in `wrapper/__main__.py:58-65`). Use the lifespan-style startup if `on_event` is deprecated in the installed FastAPI (`uv run python -c "import fastapi; print(fastapi.__version__)"`).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/driver -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Let the driver take its match from env and leave when it is over"
```

### Task 7: `k8s_seats.py` - Job builder

**Files:**
- Modify: `pyproject.toml` (add `"kubernetes>=36,<37"`), run `uv lock`
- Create: `src/magic_cards_model_api/k8s_seats.py`
- Test: `tests/test_k8s_seats.py`

**Interfaces:**
- Produces:
  - `K8sSettings.from_env(env)` with `namespace` (`AGENT_NAMESPACE`, default `mtg-engine`), `agent_image` (`AGENT_IMAGE`), `api_image` (`API_IMAGE`), `api_url` (`API_SERVICE_URL`, default `http://mtg-stack.mtg-engine.svc.cluster.local:8000`), `spellslinger_url` (`SPELLSLINGER_MCP_URL`), `owner_pod_name`/`owner_pod_uid` (`POD_NAME`, `POD_UID`), `deadline_seconds` (`AGENT_JOB_DEADLINE_SECONDS`, 14400), `ttl_seconds` (600), `memory_claim` (`AGENT_MEMORY_PVC`, `mtg-agent-memory`), `guides_repo`, `agent_secret` (`mtg-agent`), `git_secret` (`mtg-decks-git`), `image_pull_secret` (`regcred`).
  - `job_name(game_id, uid) -> str`; `LABEL_APP = "app.kubernetes.io/name"`, `LABEL_GAME = "mtg/game-id"`, `LABEL_SEAT = "mtg/seat-uid"`.
  - `mcp_config(settings, game_id) -> dict` (the ConfigMap data with `--game`).
  - `build_job(settings, *, game_id, uid, player, agent: AgentSettings) -> dict` returning a plain-dict Job manifest (the client accepts dicts via `create_namespaced_job(namespace, body=dict)`).

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.k8s_seats import K8sSettings, build_job, job_name, mcp_config

ENV = {
    "AGENT_NAMESPACE": "mtg-engine",
    "AGENT_IMAGE": "harbor/agent:abc",
    "API_IMAGE": "harbor/api:abc",
    "POD_NAME": "mtg-stack-1",
    "POD_UID": "uid-1",
    "SPELLSLINGER_MCP_URL": "http://ss:8080/mcp",
}


def test_job_name_is_short_and_stable() -> None:
    assert job_name("0123456789abcdef", "fedcba9876543210") == "mtg-seat-01234567-fedcba98"


def test_mcp_config_binds_the_game() -> None:
    cfg = mcp_config(K8sSettings.from_env(ENV), "g1")
    assert cfg["mcpServers"]["mtg"]["args"] == [
        "-m",
        "magic_cards_model_api.mcp",
        "--game",
        "g1",
        "--api",
        "http://mtg-stack.mtg-engine.svc.cluster.local:8000",
    ]
    assert cfg["mcpServers"]["spellslinger"]["url"] == "http://ss:8080/mcp"


def test_job_shape() -> None:
    job = build_job(
        K8sSettings.from_env(ENV),
        game_id="g1",
        uid="u1",
        player="bot",
        agent=AgentSettings(model="opus", effort="max", max_thinking_tokens=0),
    )
    meta = job["metadata"]
    assert meta["name"] == job_name("g1", "u1")
    assert meta["labels"] == {
        "app.kubernetes.io/name": "mtg-agent",
        "mtg/game-id": "g1",
        "mtg/seat-uid": "u1",
    }
    assert meta["ownerReferences"] == [
        {"apiVersion": "v1", "kind": "Pod", "name": "mtg-stack-1", "uid": "uid-1"}
    ]
    spec = job["spec"]
    assert (
        spec["backoffLimit"] == 0
        and spec["ttlSecondsAfterFinished"] == 600
        and spec["activeDeadlineSeconds"] == 14400
    )
    pod = spec["template"]["spec"]
    assert pod["restartPolicy"] == "Never" and pod["automountServiceAccountToken"] is False
    names = [c["name"] for c in pod["containers"]]
    assert names == ["agent", "driver"]
    agent = pod["containers"][0]
    env = {e["name"]: e.get("value") for e in agent["env"]}
    assert env["MODEL"] == "opus" and env["EFFORT"] == "max" and env["MAX_THINKING_TOKENS"] == "0"
    assert {"secretRef": {"name": "mtg-agent"}} in agent["envFrom"]
    driver = pod["containers"][1]
    denv = {e["name"]: e.get("value") for e in driver["env"]}
    assert denv["DRIVER_EXIT_AFTER_MATCH"] == "1"
    assert '"game_id": "g1"' in denv["DRIVER_ASSIGNMENT_JSON"]
    assert agent["securityContext"]["allowPrivilegeEscalation"] is False
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_k8s_seats.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement the builder**

```python
"""One Kubernetes Job per AI seat.

The API creates the Job, watches it, and reconciles; the Job carries the
agent container (the wrapper supervising one interactive claude) and the
driver as a sidecar, both from the same images the compose stack uses. The
driver takes its match from `DRIVER_ASSIGNMENT_JSON` and exits when the match
is over, so the Job completes on its own.

A Job rather than a bare Pod: the docs say a bare Pod is not rescheduled,
`activeDeadlineSeconds` bounds a runaway match, and `ttlSecondsAfterFinished`
cleans up. `ownerReferences` name this API's own Pod, so an API restart -
which loses every in-memory game anyway - garbage-collects its Jobs.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .agent_settings import AgentSettings

LABEL_APP = "app.kubernetes.io/name"
LABEL_GAME = "mtg/game-id"
LABEL_SEAT = "mtg/seat-uid"
APP_LABEL_VALUE = "mtg-agent"


@dataclass(frozen=True)
class K8sSettings:
    namespace: str = "mtg-engine"
    agent_image: str = ""
    api_image: str = ""
    api_url: str = "http://mtg-stack.mtg-engine.svc.cluster.local:8000"
    spellslinger_url: str = ""
    owner_pod_name: str = ""
    owner_pod_uid: str = ""
    deadline_seconds: int = 14400
    ttl_seconds: int = 600
    memory_claim: str = "mtg-agent-memory"
    guides_repo: str = "https://github.com/szymonrychu/mtg-decks.git"
    agent_secret: str = "mtg-agent"
    git_secret: str = "mtg-decks-git"
    image_pull_secret: str = "regcred"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> K8sSettings:
        def value(key: str, default: str) -> str:
            return (env.get(key) or "").strip() or default

        return cls(
            namespace=value("AGENT_NAMESPACE", "mtg-engine"),
            agent_image=value("AGENT_IMAGE", ""),
            api_image=value("API_IMAGE", ""),
            api_url=value("API_SERVICE_URL", "http://mtg-stack.mtg-engine.svc.cluster.local:8000"),
            spellslinger_url=value("SPELLSLINGER_MCP_URL", ""),
            owner_pod_name=value("POD_NAME", ""),
            owner_pod_uid=value("POD_UID", ""),
            deadline_seconds=int(value("AGENT_JOB_DEADLINE_SECONDS", "14400")),
            ttl_seconds=int(value("AGENT_JOB_TTL_SECONDS", "600")),
            memory_claim=value("AGENT_MEMORY_PVC", "mtg-agent-memory"),
            guides_repo=value("GUIDES_REPO", "https://github.com/szymonrychu/mtg-decks.git"),
            agent_secret=value("AGENT_SECRET", "mtg-agent"),
            git_secret=value("GUIDES_GIT_SECRET", "mtg-decks-git"),
            image_pull_secret=value("IMAGE_PULL_SECRET", "regcred"),
        )


def job_name(game_id: str, uid: str) -> str:
    return f"mtg-seat-{game_id[:8]}-{uid[:8]}"


def mcp_config(settings: K8sSettings, game_id: str) -> dict[str, Any]:
    servers: dict[str, Any] = {
        "mtg": {
            "command": "python",
            "args": [
                "-m",
                "magic_cards_model_api.mcp",
                "--game",
                game_id,
                "--api",
                settings.api_url,
            ],
        }
    }
    if settings.spellslinger_url:
        servers["spellslinger"] = {"type": "http", "url": settings.spellslinger_url}
    return {"mcpServers": servers}


def _env(pairs: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"name": k, "value": v} for k, v in pairs.items()]


CONTAINER_SECURITY = {
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "runAsNonRoot": True,
}


def build_job(
    settings: K8sSettings, *, game_id: str, uid: str, player: str, agent: AgentSettings
) -> dict[str, Any]:
    name = job_name(game_id, uid)
    labels = {LABEL_APP: APP_LABEL_VALUE, LABEL_GAME: game_id, LABEL_SEAT: uid}
    assignment = {
        "uid": uid,
        "game_id": game_id,
        "player": player,
        "api_base": settings.api_url,
        "agent": agent.model_dump(),
    }
    agent_env = {
        "HTTP_ADDR": ":8080",
        "INTERNAL_ADDR": "127.0.0.1:8099",
        "WORKSPACE": "/workspace",
        "HOME_DIR": "/agent-home",
        "HOOK_COMMAND": "/app/.venv/bin/cc-stop-hook",
        "CCW_INTERNAL_URL": "http://127.0.0.1:8099/internal/turn-complete",
        "CCW_RESULT_JSON": "/workspace/result.json",
        "BASE_MCP_PATH": "/etc/mtg/mcp.json",
        "TURN_TIMEOUT_SECONDS": "1800",
        "BOOT_TIMEOUT_SECONDS": "120",
        "LOG_LEVEL": "info",
        "AGENT_LOG_LEVEL": "tools",
        "ACCESS_LOG_QUIET": "1",
        **agent.env(),
    }
    driver_env = {
        "GAME_API": settings.api_url,
        "WRAPPER_API": "http://127.0.0.1:8080",
        "DRIVER_SELF_URL": "http://127.0.0.1:8090",
        "GUIDES_ROOT": "/guides",
        "MEMORY_ROOT": "/workspace/memory",
        "DRIVER_WORKSPACE": "/driver-workspace",
        "HTTP_ADDR": ":8090",
        "ACCESS_LOG_QUIET": "1",
        "DRIVER_EXIT_AFTER_MATCH": "1",
        "DRIVER_ASSIGNMENT_JSON": json.dumps(assignment),
    }
    metadata: dict[str, Any] = {
        "name": name,
        "namespace": settings.namespace,
        "labels": labels,
        "annotations": {"mtg/player": player, "mtg/model": agent.model, "mtg/effort": agent.effort},
    }
    if settings.owner_pod_name and settings.owner_pod_uid:
        metadata["ownerReferences"] = [
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "name": settings.owner_pod_name,
                "uid": settings.owner_pod_uid,
            }
        ]
    guides_script = (
        'test -n "${GH_PAT}" || { echo "GH_PAT is empty" >&2; exit 1; }\n'
        "git config --global --add safe.directory /guides\n"
        "find /guides -mindepth 1 -delete\n"
        f'git clone --depth 1 "https://x-access-token:${{GH_PAT}}@{settings.guides_repo.removeprefix("https://")}" /guides\n'
        f"git -C /guides remote set-url origin {settings.guides_repo}\n"
        '! grep -rq "x-access-token" /guides/.git/config\n'
    )
    mcp_json = json.dumps(mcp_config(settings, game_id))
    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "imagePullSecrets": [{"name": settings.image_pull_secret}],
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "fsGroup": 1000,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumes": [
            {"name": "guides", "emptyDir": {}},
            {"name": "memory", "persistentVolumeClaim": {"claimName": settings.memory_claim}},
            {"name": "driver-workspace", "emptyDir": {}},
            {"name": "mcp", "emptyDir": {}},
        ],
        "initContainers": [
            {
                "name": "guides",
                "image": settings.agent_image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-euc", guides_script],
                "env": [
                    {
                        "name": "GH_PAT",
                        "valueFrom": {
                            "secretKeyRef": {"name": settings.git_secret, "key": "GH_PAT"}
                        },
                    },
                    {"name": "GIT_TERMINAL_PROMPT", "value": "0"},
                    {"name": "HOME", "value": "/agent-home"},
                ],
                "volumeMounts": [{"name": "guides", "mountPath": "/guides"}],
                "securityContext": CONTAINER_SECURITY,
                "resources": {
                    "requests": {"cpu": "50m", "memory": "64Mi"},
                    "limits": {"cpu": "500m", "memory": "256Mi"},
                },
            },
            {
                "name": "mcp-config",
                "image": settings.agent_image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-euc", "printf '%s' \"$MCP_JSON\" > /etc/mtg/mcp.json"],
                "env": [{"name": "MCP_JSON", "value": mcp_json}],
                "volumeMounts": [{"name": "mcp", "mountPath": "/etc/mtg"}],
                "securityContext": CONTAINER_SECURITY,
                "resources": {
                    "requests": {"cpu": "10m", "memory": "16Mi"},
                    "limits": {"cpu": "100m", "memory": "64Mi"},
                },
            },
        ],
        "containers": [
            {
                "name": "agent",
                "image": settings.agent_image,
                "imagePullPolicy": "IfNotPresent",
                "ports": [{"name": "agent", "containerPort": 8080}],
                "env": _env(agent_env),
                "envFrom": [{"secretRef": {"name": settings.agent_secret}}],
                "volumeMounts": [
                    {"name": "guides", "mountPath": "/guides", "readOnly": True},
                    {"name": "memory", "mountPath": "/workspace/memory"},
                    {"name": "mcp", "mountPath": "/etc/mtg", "readOnly": True},
                ],
                "startupProbe": {
                    "httpGet": {"path": "/healthz", "port": 8080},
                    "periodSeconds": 10,
                    "failureThreshold": 36,
                },
                "readinessProbe": {
                    "httpGet": {"path": "/readyz", "port": 8080},
                    "periodSeconds": 10,
                    "timeoutSeconds": 3,
                    "failureThreshold": 3,
                },
                "securityContext": CONTAINER_SECURITY,
                "resources": {
                    "requests": {"cpu": "250m", "memory": "1Gi"},
                    "limits": {"cpu": "2", "memory": "3Gi"},
                },
            },
            {
                "name": "driver",
                "image": settings.api_image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["python", "-m", "mtg_agent.driver"],
                "ports": [{"name": "driver", "containerPort": 8090}],
                "env": _env(driver_env),
                "volumeMounts": [
                    {"name": "guides", "mountPath": "/guides", "readOnly": True},
                    {"name": "memory", "mountPath": "/workspace/memory"},
                    {"name": "driver-workspace", "mountPath": "/driver-workspace"},
                ],
                "readinessProbe": {
                    "httpGet": {"path": "/healthz", "port": 8090},
                    "periodSeconds": 10,
                    "timeoutSeconds": 3,
                    "failureThreshold": 3,
                },
                "securityContext": CONTAINER_SECURITY,
                "resources": {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                },
            },
        ],
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": metadata,
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": settings.deadline_seconds,
            "ttlSecondsAfterFinished": settings.ttl_seconds,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }
```

The MCP config is written by an initContainer into an emptyDir rather than a per-Job ConfigMap, so the API needs no `configmaps` RBAC; drop `configmaps` from the Role in Task 10 accordingly and note the change in the spec's "Cluster manifests" section. The wrapper's `merge_mcp` reads `BASE_MCP_PATH`, so `/etc/mtg/mcp.json` is the whole config.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_k8s_seats.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Build one Job per AI seat"
```

### Task 8: `K8sSeatRunner` - create, watch, reconcile, status, stop

**Files:**
- Modify: `src/magic_cards_model_api/k8s_seats.py` (add the runner), `src/magic_cards_model_api/ai_seat.py` (`AI_SEAT_MODE=kubernetes` branch in `start`, `status`, `stop`, `release`)
- Test: `tests/test_k8s_seats.py` (extend)

**Interfaces:**
- Produces:
  - `class K8sSeats` with `__init__(settings, batch_api, core_api, http_client_factory)`; `start(*, uid, game_id, player, agent) -> status dict` (creates the Job, idempotent on 409 AlreadyExists); `status(uid) -> dict | None` with `{"uid", "mechanism": "kubernetes", "state": pending|thinking|idle|gone|error, "job", "pod", "reason", "driver": <driver /status or None>}`; `release(uid) -> dict` (best-effort `POST /release` at the pod IP, then `delete_namespaced_job(name, namespace, body=V1DeleteOptions(propagation_policy="Background"))`); `run_watch()` (a daemon thread over `watch.Watch().stream(batch_api.list_namespaced_job, namespace, label_selector=f"{LABEL_APP}={APP_LABEL_VALUE}")`, relisting on `ApiException` 410); `reconcile(live_game_ids: set[str]) -> dict` (list Jobs by label; delete Jobs whose `LABEL_GAME` is not live; mark seats whose Job is gone as error); `start_background(live_games: Callable[[], set[str]])` starting the watch thread and a 60 s reconcile thread.
  - `k8s_seats.seats: K8sSeats | None` created lazily by `k8s_seats.get()` from `load_incluster_config()` (falling back to `load_kube_config()` for a laptop), with `AI_SEAT_MODE=kubernetes` in `ai_seat.registry.start` delegating to it.
  - `ai_seat._driver_activity` maps `kubernetes` statuses the same way it maps wrapper ones: `pending` -> STARTING, `thinking` -> WORKING, else GONE.

- [ ] **Step 1: Write the failing tests** (a fake Kubernetes client, the one allowed fake)

```python
import types
from kubernetes.client.exceptions import ApiException
from magic_cards_model_api.k8s_seats import K8sSeats, K8sSettings, job_name, LABEL_GAME


class FakeBatch:
    def __init__(self):
        self.jobs = {}
        self.deleted = []

    def create_namespaced_job(self, namespace, body):
        name = body["metadata"]["name"]
        if name in self.jobs:
            raise ApiException(status=409, reason="AlreadyExists")
        self.jobs[name] = types.SimpleNamespace(
            metadata=types.SimpleNamespace(name=name, labels=body["metadata"]["labels"]),
            status=types.SimpleNamespace(active=1, succeeded=0, failed=0),
        )
        return self.jobs[name]

    def list_namespaced_job(self, namespace, label_selector=None, **kw):
        return types.SimpleNamespace(
            items=list(self.jobs.values()), metadata=types.SimpleNamespace(resource_version="1")
        )

    def delete_namespaced_job(self, name, namespace, body=None):
        self.deleted.append(name)
        self.jobs.pop(name, None)


class FakeCore:
    def __init__(self, pod_ip="10.0.0.5", phase="Running"):
        self.pod_ip, self.phase = pod_ip, phase

    def list_namespaced_pod(self, namespace, label_selector=None, **kw):
        pod = types.SimpleNamespace(
            status=types.SimpleNamespace(
                pod_ip=self.pod_ip, phase=self.phase, container_statuses=[]
            ),
            metadata=types.SimpleNamespace(name="p1"),
        )
        return types.SimpleNamespace(items=[pod])


class FakeDriverHttp:
    def __init__(self, status):
        self.status, self.posts = status, []

    def __call__(self, base):
        outer = self

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, path, **kw):
                return types.SimpleNamespace(status_code=200, json=lambda: outer.status)

            def post(self, path, **kw):
                outer.posts.append((base, path))
                return types.SimpleNamespace(status_code=202, json=lambda: {})

        return Client()


def make(status=None, **core):
    batch, corev1 = FakeBatch(), FakeCore(**core)
    http = FakeDriverHttp(status or {"turnActive": True, "pending": False, "lastError": None})
    return K8sSeats(K8sSettings.from_env(ENV), batch, corev1, http), batch, http


def test_start_creates_the_job_once() -> None:
    seats, batch, _ = make()
    first = seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    again = seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    assert first["mechanism"] == "kubernetes" and first["job"] == job_name("g1", "u1")
    assert again["job"] == first["job"] and list(batch.jobs) == [first["job"]]


def test_status_merges_pod_and_driver() -> None:
    seats, _, _ = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    status = seats.status("u1")
    assert (
        status["state"] == "thinking"
        and status["pod"] == "Running"
        and status["driver"]["turnActive"] is True
    )


def test_status_is_pending_before_the_pod_runs() -> None:
    seats, _, _ = make(phase="Pending", pod_ip=None)
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    assert seats.status("u1")["state"] == "pending"


def test_release_tells_the_driver_then_deletes_the_job() -> None:
    seats, batch, http = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    released = seats.release("u1")
    assert http.posts == [("http://10.0.0.5:8090", "/release")]
    assert batch.deleted == [job_name("g1", "u1")] and released["deleted"] is True
    assert seats.status("u1") is None


def test_reconcile_deletes_jobs_of_dead_games_and_flags_missing_jobs() -> None:
    seats, batch, _ = make()
    seats.start(
        uid="u1", game_id="g1", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    seats.start(
        uid="u2", game_id="g2", player="bot", agent=AgentSettings(model="sonnet", effort="high")
    )
    batch.jobs.pop(job_name("g2", "u2"))  # vanished behind our back
    report = seats.reconcile(live_game_ids={"g2"})
    assert batch.deleted == [job_name("g1", "u1")]
    assert report == {"deleted": [job_name("g1", "u1")], "missing": ["u2"]}
    assert seats.status("u2")["state"] == "error" and "missing" in seats.status("u2")["reason"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_k8s_seats.py -v`
Expected: FAIL (`K8sSeats` not defined).

- [ ] **Step 3: Implement the runner** in `k8s_seats.py`:

```python
import logging
import threading
import time
from collections.abc import Callable

import httpx
from kubernetes import client as k8s_client, config as k8s_config, watch as k8s_watch
from kubernetes.client.exceptions import ApiException

log = logging.getLogger(__name__)


def _default_http(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=10.0)


class K8sSeats:
    def __init__(
        self,
        settings: K8sSettings,
        batch_api: Any,
        core_api: Any,
        http_client_factory: Callable[[str], Any] = _default_http,
    ) -> None:
        self.settings = settings
        self.batch = batch_api
        self.core = core_api
        self.http = http_client_factory
        self._seats: dict[str, dict[str, Any]] = {}  # uid -> {game_id, player, job, error}
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(self, *, uid: str, game_id: str, player: str, agent: AgentSettings) -> dict[str, Any]:
        name = job_name(game_id, uid)
        with self._lock:
            if uid in self._seats:
                return self.status(uid) or {}
            self._seats[uid] = {"game_id": game_id, "player": player, "job": name, "error": None}
        body = build_job(self.settings, game_id=game_id, uid=uid, player=player, agent=agent)
        try:
            self.batch.create_namespaced_job(self.settings.namespace, body=body)
        except ApiException as exc:
            if exc.status != 409:
                with self._lock:
                    self._seats[uid]["error"] = f"could not create job {name}: {exc.reason}"
                log.error("job %s not created: %s", name, exc)
        return self.status(uid) or {}

    def _pod(self, uid: str) -> Any | None:
        pods = self.core.list_namespaced_pod(
            self.settings.namespace, label_selector=f"{LABEL_SEAT}={uid}"
        ).items
        return pods[0] if pods else None

    def _driver_status(self, pod: Any) -> dict[str, Any] | None:
        ip = pod.status.pod_ip if pod is not None else None
        if not ip:
            return None
        try:
            with self.http(f"http://{ip}:8090") as client:
                response = client.get("/status")
                return response.json() if response.status_code < 400 else None
        except Exception:  # noqa: BLE001 - a driver that is not up yet
            return None

    def status(self, uid: str) -> dict[str, Any] | None:
        with self._lock:
            seat = self._seats.get(uid)
        if seat is None:
            return None
        base = {
            "uid": uid,
            "mechanism": "kubernetes",
            "job": seat["job"],
            "game_id": seat["game_id"],
            "player": seat["player"],
        }
        if seat["error"]:
            return {**base, "state": "error", "reason": seat["error"], "pod": None, "driver": None}
        pod = self._pod(uid)
        phase = pod.status.phase if pod is not None else None
        waiting = ""
        for cs in (pod.status.container_statuses or []) if pod is not None else []:
            state = getattr(cs, "state", None)
            if (
                state is not None
                and getattr(state, "waiting", None) is not None
                and state.waiting.reason
            ):
                waiting = state.waiting.reason
        if waiting in ("ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff"):
            return {**base, "state": "error", "reason": waiting, "pod": phase, "driver": None}
        driver = self._driver_status(pod)
        if phase in (None, "Pending") or driver is None:
            state = "gone" if phase in ("Succeeded", "Failed") else "pending"
            return {**base, "state": state, "reason": phase, "pod": phase, "driver": driver}
        if driver.get("lastError"):
            return {
                **base,
                "state": "error",
                "reason": driver["lastError"],
                "pod": phase,
                "driver": driver,
            }
        state = (
            "thinking"
            if driver.get("turnActive")
            else ("pending" if driver.get("pending") else "idle")
        )
        return {**base, "state": state, "reason": None, "pod": phase, "driver": driver}

    def release(self, uid: str) -> dict[str, Any]:
        with self._lock:
            seat = self._seats.pop(uid, None)
        if seat is None:
            return {"uid": uid, "mechanism": None, "deleted": False}
        pod = self._pod(uid)
        if pod is not None and pod.status.pod_ip:
            try:
                with self.http(f"http://{pod.status.pod_ip}:8090") as client:
                    client.post("/release", json={"uid": uid, "game_id": seat["game_id"]})
            except Exception as exc:  # noqa: BLE001 - the Job is deleted regardless
                log.info("driver for %s not told about the release: %s", uid, exc)
        deleted = True
        try:
            self.batch.delete_namespaced_job(
                seat["job"],
                self.settings.namespace,
                body=k8s_client.V1DeleteOptions(propagation_policy="Background"),
            )
        except ApiException as exc:
            deleted = exc.status == 404
        return {"uid": uid, "mechanism": "kubernetes", "job": seat["job"], "deleted": deleted}

    stop = release

    # -- tracking ----------------------------------------------------------
    def reconcile(self, live_game_ids: set[str]) -> dict[str, list[str]]:
        jobs = self.batch.list_namespaced_job(
            self.settings.namespace, label_selector=f"{LABEL_APP}={APP_LABEL_VALUE}"
        ).items
        present = {job.metadata.name for job in jobs}
        deleted: list[str] = []
        for job in jobs:
            game_id = (job.metadata.labels or {}).get(LABEL_GAME)
            if game_id not in live_game_ids:
                try:
                    self.batch.delete_namespaced_job(
                        job.metadata.name,
                        self.settings.namespace,
                        body=k8s_client.V1DeleteOptions(propagation_policy="Background"),
                    )
                    deleted.append(job.metadata.name)
                except ApiException as exc:
                    log.warning("could not delete stray job %s: %s", job.metadata.name, exc)
        missing: list[str] = []
        with self._lock:
            for uid, seat in self._seats.items():
                if seat["job"] not in present and not seat["error"]:
                    seat["error"] = f"job {seat['job']} is missing from the cluster"
                    missing.append(uid)
        return {"deleted": deleted, "missing": missing}

    def run_watch(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                w = k8s_watch.Watch()
                for event in w.stream(
                    self.batch.list_namespaced_job,
                    self.settings.namespace,
                    label_selector=f"{LABEL_APP}={APP_LABEL_VALUE}",
                    timeout_seconds=300,
                ):
                    job = event["object"]
                    log.info(
                        "job %s %s active=%s succeeded=%s failed=%s",
                        job.metadata.name,
                        event["type"],
                        job.status.active,
                        job.status.succeeded,
                        job.status.failed,
                    )
                    if stop.is_set():
                        break
            except ApiException as exc:
                log.warning("job watch ended: %s; relisting", exc.reason)
                time.sleep(2)
            except Exception as exc:  # noqa: BLE001 - keep watching
                log.warning("job watch error: %s", exc)
                time.sleep(5)

    def start_background(
        self, live_games: Callable[[], set[str]], interval: float = 60.0
    ) -> threading.Event:
        stop = threading.Event()
        threading.Thread(
            target=self.run_watch, args=(stop,), name="k8s-seat-watch", daemon=True
        ).start()

        def loop() -> None:
            while not stop.wait(interval):
                try:
                    report = self.reconcile(live_games())
                    if report["deleted"] or report["missing"]:
                        log.info("reconcile: %s", report)
                except Exception as exc:  # noqa: BLE001 - reconcile again next tick
                    log.warning("reconcile failed: %s", exc)

        threading.Thread(target=loop, name="k8s-seat-reconcile", daemon=True).start()
        return stop


_seats: K8sSeats | None = None


def get() -> K8sSeats:
    global _seats
    if _seats is None:
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        settings = K8sSettings.from_env(os.environ)
        _seats = K8sSeats(settings, k8s_client.BatchV1Api(), k8s_client.CoreV1Api())
        from .store import store  # lazy: store imports nothing from here

        _seats.reconcile(set(store.ids()))
        _seats.start_background(lambda: set(store.ids()))
    return _seats
```

Wire `ai_seat.py`: in `AiSeatRegistry.start`, `if ai_seat_mode() == "kubernetes": status = k8s_seats.get().start(...)`, record in `self._dispatched[uid]` and audit as the wrapper branch does; `status(uid)`, `stop(uid)`, `release(uid)` delegate when the recorded mechanism is `kubernetes`; `_driver_activity` treats `kubernetes` like `wrapper`. `ai_seat_api.ai_seat_status` needs no change.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_k8s_seats.py tests/test_ai_seat.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && git add -A && git commit -m "Schedule, watch and reconcile one Job per AI seat"
```

### Task 9: lobby screen - game list, agent fields, end game

**Files:**
- Modify: `ui/render/lobby.js` (if the play UI plan's Task 16 has landed) or `ui/app.js` and `ui/index.html` (if not; the same DOM ids), `ui/style.css`
- Test: `tests/test_ui_lobby.py` (Node view-model test) and `tests/test_ui_e2e.py` (extend the lobby step to send `agent`)

**Interfaces:**
- Produces: `lobbyViewModel(games, saved) -> [{gameId, opponent, turn, step, over, model, effort, myUid, aiUid}]` in `ui/lobby_model.js` (pure), where `saved` is the `localStorage` list `mcm.games.v1 = [{gameId, uid, aiUid}]`; the lobby renders a table from it with Open (calls `startGame`) and End (`DELETE /api/ui/games/{id}?uid=`) buttons; the AI deck picker gains `#ai-model` (text input with a `<datalist>` of `sonnet, opus, haiku, fable`), `#ai-effort` (select, default `high`), `#ai-thinking` (number, optional); `POST /api/ui/games` sends `agent: {model, effort, max_thinking_tokens}` for the AI player; after creation the game is appended to the saved list.

- [ ] **Step 1: Write the failing test**

```python
from ui_node import requires_node, run_module

pytestmark = requires_node

GAMES = [
    {
        "game_id": "g1",
        "players": ["you", "bot"],
        "turn_number": 3,
        "step": "precombat_main",
        "winner": None,
        "drawn": None,
        "agents": {"bot": {"model": "opus", "effort": "max", "max_thinking_tokens": None}},
        "seats": {"you": "u1"},
    }
]
SAVED = [{"gameId": "g1", "uid": "u1", "aiUid": "u2"}]


def test_lobby_rows_join_server_games_with_saved_seats() -> None:
    rows = run_module("lobby_model.js", "m.lobbyViewModel(games, saved)", games=GAMES, saved=SAVED)
    assert rows == [
        {
            "gameId": "g1",
            "opponent": "bot",
            "turn": 3,
            "step": "precombat_main",
            "over": False,
            "model": "opus",
            "effort": "max",
            "myUid": "u1",
            "aiUid": "u2",
        }
    ]


def test_agent_body_from_fields() -> None:
    assert run_module(
        "lobby_model.js", "m.agentFrom({ model: ' fable ', effort: 'low', thinking: '' })"
    ) == {"model": "fable", "effort": "low", "max_thinking_tokens": None}
    assert run_module(
        "lobby_model.js", "m.agentFrom({ model: '', effort: 'high', thinking: '0' })"
    ) == {"model": "sonnet", "effort": "high", "max_thinking_tokens": 0}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_ui_lobby.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`ui/lobby_model.js`:

```js
export const MODEL_SUGGESTIONS = ["sonnet", "opus", "haiku", "fable"];
export const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

export function agentFrom(fields) {
  const model = (fields.model || "").trim() || "sonnet";
  const effort = EFFORTS.includes(fields.effort) ? fields.effort : "high";
  const thinking = String(fields.thinking === undefined || fields.thinking === null ? "" : fields.thinking).trim();
  return { model, effort, max_thinking_tokens: thinking === "" ? null : Number(thinking) };
}

export function lobbyViewModel(games, saved) {
  const mine = new Map((saved || []).map((s) => [s.gameId, s]));
  return (games || []).map((g) => {
    const seat = mine.get(g.game_id) || {};
    const me = Object.keys(g.seats || {})[0] || null;
    const opponent = (g.players || []).find((p) => p !== me) || (g.players || [])[1] || null;
    const agent = opponent && g.agents ? g.agents[opponent] : null;
    return {
      gameId: g.game_id, opponent, turn: g.turn_number, step: g.step, over: Boolean(g.winner || g.drawn),
      model: agent ? agent.model : null, effort: agent ? agent.effort : null, myUid: seat.uid || (me ? g.seats[me] : null), aiUid: seat.aiUid || null,
    };
  });
}
```

Lobby rendering: on lobby open, read `mcm.games.v1`, call `GET /api/ui/games?uids=<all saved uids>`, render rows; Open runs `startGame({game_id, uid: row.myUid, aiUid: row.aiUid})`; End calls `DELETE /api/ui/games/{id}?uid=` and drops the row from storage. Creation sends `agent: agentFrom({model: $("#ai-model").value, effort: $("#ai-effort").value, thinking: $("#ai-thinking").value})` on the AI player and appends `{gameId, uid, aiUid}` to storage. Migrate the old single `mcm.game` key on first load into the list.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ui_lobby.py tests/test_ui_e2e.py tests/test_ui_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Lobby lists live games and asks for the agent's model and effort"
```

### Task 10: cluster manifests

**Files:**
- Create: `deploy/k8s/40-rbac.yaml`, `deploy/k8s/45-networkpolicy.yaml`
- Modify: `deploy/k8s/50-deployment.yaml` (API-only), `deploy/k8s/60-service.yaml` (port 8000 only), `deploy/k8s/30-storage.yaml` (memory `ReadWriteMany`), `deploy/k8s/20-configmap-mcp.yaml` (delete: the Job writes its own), `deploy/k8s/README.md`, `mise.toml` (`k8s:render` also substitutes `__AGENT_IMAGE__`/`__API_IMAGE__` if used; simpler: pass the same `__TAG__` into the Deployment env)
- Test: `tests/test_k8s_manifests.py` (parses the rendered YAML with `yaml.safe_load_all` and asserts the shape)

- [ ] **Step 1: Write the failing test**

```python
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def rendered() -> list[dict]:
    text = "".join(
        (p.read_text().replace("__TAG__", "t1") + "\n---\n")
        for p in sorted((ROOT / "deploy/k8s").glob("*.yaml"))
        if not p.name.endswith(".template.yaml")
    )
    return [doc for doc in yaml.safe_load_all(text) if doc]


def by_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def test_api_runs_alone_with_a_service_account_and_job_env() -> None:
    docs = rendered()
    (deploy,) = by_kind(docs, "Deployment")
    pod = deploy["spec"]["template"]["spec"]
    assert [c["name"] for c in pod["containers"]] == ["game"]
    assert pod["serviceAccountName"] == "mtg-api"
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["AI_SEAT_MODE"]["value"] == "kubernetes"
    assert env["AGENT_IMAGE"]["value"].endswith(":t1") and env["API_IMAGE"]["value"].endswith(":t1")
    assert env["POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
    assert env["POD_UID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
    assert env["POD_NAMESPACE"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.namespace"


def test_rbac_is_namespaced_and_minimal() -> None:
    docs = rendered()
    (role,) = by_kind(docs, "Role")
    rules = {(tuple(r["apiGroups"]), tuple(r["resources"])): set(r["verbs"]) for r in role["rules"]}
    assert rules[(("batch",), ("jobs",))] == {"create", "get", "list", "watch", "delete"}
    assert rules[(("",), ("pods",))] == {"get", "list", "watch"}
    assert not by_kind(docs, "ClusterRole")
    (binding,) = by_kind(docs, "RoleBinding")
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "mtg-api", "namespace": "mtg-engine"}
    ]


def test_memory_is_shared_and_the_service_exposes_only_the_api() -> None:
    docs = rendered()
    memory = next(
        d
        for d in by_kind(docs, "PersistentVolumeClaim")
        if d["metadata"]["name"] == "mtg-agent-memory"
    )
    assert memory["spec"]["accessModes"] == ["ReadWriteMany"]
    (service,) = by_kind(docs, "Service")
    assert [p["port"] for p in service["spec"]["ports"]] == [8000]


def test_network_policy_pins_agent_pods() -> None:
    docs = rendered()
    (policy,) = by_kind(docs, "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "mtg-agent"}
    assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
```

`pyyaml` is likely already a transitive dependency (`uv run python -c "import yaml"`); if not, add it to the dev group.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_k8s_manifests.py -v`
Expected: FAIL.

- [ ] **Step 3: Write the manifests**

`40-rbac.yaml`:

```yaml
# The API creates one Job per AI seat, so it needs to create, watch and delete
# Jobs, and read Pods to find each Job's pod IP and phase. Namespaced, never
# cluster-wide: creating pods is a privilege boundary, and the restricted Pod
# Security Standard on this namespace (label it: pod-security.kubernetes.io/
# enforce=restricted) keeps what those Jobs may ask for in check.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: mtg-api
  namespace: mtg-engine
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: mtg-api
  namespace: mtg-engine
rules:
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "get", "list", "watch", "delete"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: mtg-api
  namespace: mtg-engine
subjects:
  - kind: ServiceAccount
    name: mtg-api
    namespace: mtg-engine
roleRef:
  kind: Role
  name: mtg-api
  apiGroup: rbac.authorization.k8s.io
```

`45-networkpolicy.yaml`:

```yaml
# Agent pods talk to the API, the spellslinger MCP service, DNS, and the
# Anthropic API over HTTPS - and accept connections only from the API pod
# (driver /status and /release). Nothing else in the cluster can reach them
# and they can reach nothing else in the cluster.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: mtg-agent
  namespace: mtg-engine
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: mtg-agent
  policyTypes: ["Ingress", "Egress"]
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: mtg-stack
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    - to:
        - podSelector:
            matchLabels:
              app: mtg-stack
      ports:
        - protocol: TCP
          port: 8000
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: spellslinger-development
      ports:
        - protocol: TCP
          port: 8080
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except: ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
      ports:
        - protocol: TCP
          port: 443
```

Adjust the private CIDRs `except` list to the cluster's pod and service ranges (`kubectl cluster-info dump | grep -m1 cluster-cidr`).

`50-deployment.yaml`: remove the `agent` and `driver` containers, the `guides` initContainer, the `guides`, `memory`, `mcp-base`, `driver-workspace` volumes; add `serviceAccountName: mtg-api`; on `game` add env `AI_SEAT_MODE=kubernetes`, `AGENT_NAMESPACE=mtg-engine`, `AGENT_IMAGE=harbor.szymonrichert.pl/containers/magic-cards-model-agent:__TAG__`, `API_IMAGE=harbor.szymonrichert.pl/containers/magic-cards-model-api:__TAG__`, `API_SERVICE_URL=http://mtg-stack.mtg-engine.svc.cluster.local:8000`, `SPELLSLINGER_MCP_URL=http://spellslinger-mcp-http.spellslinger-development.svc.cluster.local:8080/mcp`, `MAX_LIVE_GAMES=4`, `POD_NAME`/`POD_UID` via `fieldRef`; remove `DRIVER_URL`. Rewrite the header comment: the stack is now the API; agents are Jobs. Keep the scryfall cache PVC on the API. `60-service.yaml`: only port 8000, comment updated. `30-storage.yaml`: memory PVC `ReadWriteMany` with a comment on why (several agent pods at once). Delete `20-configmap-mcp.yaml` and explain in `deploy/k8s/README.md` that the Job's initContainer writes the MCP config with `--game`. Update `deploy/k8s/README.md`'s procedure (apply RBAC, label the namespace `pod-security.kubernetes.io/enforce=restricted`, `kubectl get jobs -l app.kubernetes.io/name=mtg-agent`, `kubectl logs job/<name> -c agent`), and `README.md`'s deploy section.

- [ ] **Step 4: Run tests and render**

Run: `uv run pytest tests/test_k8s_manifests.py -v && mise run k8s:render | head -50`
Expected: PASS; the render shows the API-only Deployment.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Deploy the API alone, with RBAC for per-seat Jobs and a NetworkPolicy for agents"
```

### Task 11: compose keeps working, docs

**Files:**
- Modify: `docker-compose.yml` (agent gets `EFFORT=${CLAUDE_EFFORT:-high}`; `.env.example` gains `CLAUDE_EFFORT`), `README.md` (a "Several games at once" section: modes, lobby, `MAX_LIVE_GAMES`, agent settings, where Jobs show up), spec files (note the initContainer-written MCP config and the dropped `configmaps` RBAC)
- Test: `tests/driver/test_compose.py` (extend: the agent service carries `EFFORT`)

- [ ] **Step 1: Extend the compose test** the way it already asserts other env keys, run it, see it fail, add the env line, run it, commit.

```bash
uv run ruff format . && git add -A && git commit -m "Carry the effort setting through compose, and document multi-game play"
```

### Task 12: cluster run

- [ ] **Step 1:** `mise run k8s:deploy` (pushes both images, applies). Label the namespace restricted first: `kubectl label ns mtg-engine pod-security.kubernetes.io/enforce=restricted --overwrite`.
- [ ] **Step 2:** From two browser tabs, create two games with different models (for example `sonnet` at `high` and `sonnet` at `low`); confirm `kubectl -n mtg-engine get jobs -l app.kubernetes.io/name=mtg-agent` shows two Jobs, both agents play (the AI panel shows `thinking`), and the driver log names its game.
- [ ] **Step 3:** End one game from the lobby; its Job disappears. Let the other finish; its Job goes `Complete` and vanishes after the TTL. Restart the API Deployment; any leftover Job is garbage-collected.
- [ ] **Step 4:** Note every deviation found in the plan's result and fix it with a test where the logic is testable; commit each fix.

## Self-review notes

- Spec coverage: request and defaults (Tasks 1-2), cap and no reset and End game (3), lobby screen (9), subprocess and wrapper modes (4-5), scheduler with Job shape, watch, reconcile, status, stop (7-8), driver self-dispatch and exit (6), wrapper effort (5), memory RWX and manifests and RBAC and NetworkPolicy and API-only Deployment (10), compose (11), manual cluster run (12).
- Change from the spec: the MCP config is written by an initContainer from an env var rather than a per-Job ConfigMap, so no `configmaps` RBAC; Task 11 records that in the spec.
- Names used consistently: `AgentSettings` (1-4, 7-8), `job_name`/`build_job`/`K8sSeats` (7-8), `DRIVER_ASSIGNMENT_JSON`/`DRIVER_EXIT_AFTER_MATCH` (6-7), `EFFORT`/`MAX_THINKING_TOKENS` (1, 5, 7).
