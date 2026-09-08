"""Two FastAPI apps on two listeners.

The public one carries /v1 and the operator surface. The internal one carries
exactly one route - the Stop hook's target - and is bound to 127.0.0.1 so it
is unreachable from outside the container. Separate apps, not separate
routers on one app: a single misconfigured bind should not expose the hook
endpoint.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .probe import ProbeUnavailableError
from .session import BusyError, DeadError, Manager, NotReadyError


class MessageIn(BaseModel):
    text: str = ""
    callbackUrl: str = ""


class ProbeIn(BaseModel):
    text: str = ""


class SessionIn(BaseModel):
    """A per-game agent settings push - see `POST /v1/session` below.

    One spelling on this wire, camelCase, matching
    `contract/agent-settings.schema.json` and what `GET /v1/session`
    (`Manager.snapshot`) already answers with - the driver is this route's
    only caller (`mtg_agent/driver/app.py`'s `sync_agent_settings`), and it
    posts `AgentSettings.to_wire()` verbatim.

    `extra="forbid"`, deliberately: without it, a stray `max_thinking_tokens`
    key (the spelling this route replaced) is not a validation error at all
    - pydantic just ignores an unrecognised field and this falls back to
    `None` - which is exactly the silent "live seat quietly gets the default
    budget instead of the one it was dispatched with" failure this contract
    exists to catch, so the wrong spelling must be a hard 422, not a quiet
    no-op."""

    model_config = ConfigDict(extra="forbid")

    model: str = ""
    effort: str = ""
    max_thinking_tokens: int | None = Field(default=None, alias="maxThinkingTokens")


def build_public_app(
    manager: Manager,
    settings: Settings,
    *,
    on_exit: Callable[[], None] | None = None,
) -> FastAPI:
    """`on_exit` is how the HTTP layer asks the process to stop.

    It is supplied by `__main__`, which owns the uvicorn server; the app never
    reaches for the server object and never calls `os._exit`, so the routes
    stay testable and the port stays a port.
    """

    app = FastAPI(title="mtg-agent-wrapper")

    def require_secret(request: Request) -> None:
        if not settings.shared_secret:
            return
        if request.headers.get("X-Wrapper-Secret") != settings.shared_secret:
            raise HTTPException(status_code=401, detail="bad or missing X-Wrapper-Secret")

    guard = [Depends(require_secret)]

    @app.post("/v1/messages", status_code=202, dependencies=guard)
    def post_message(body: MessageIn) -> dict[str, str]:
        if not body.text.strip():
            raise HTTPException(status_code=400, detail="text is required")
        try:
            turn_id = manager.submit(body.text, body.callbackUrl)
        except BusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (DeadError, NotReadyError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"turnId": turn_id}

    @app.get("/v1/messages", dependencies=guard)
    def list_messages() -> list[dict]:
        return [record.summary() for record in manager.store.list()]

    @app.get("/v1/messages/{turn_id}", dependencies=guard)
    def get_message(turn_id: str) -> dict:
        record = manager.store.get(turn_id)
        if record is None:
            raise HTTPException(status_code=404, detail="no such turn")
        return record.full()

    @app.get("/v1/session", dependencies=guard)
    def get_session() -> dict:
        return manager.snapshot()

    @app.post("/v1/session", status_code=202, dependencies=guard)
    def post_session(body: SessionIn) -> dict:
        """Per-game model/effort/max_thinking_tokens, applied for the next
        turn onward. Requires no turn in flight - a 409 (`BusyError`) means
        the caller (the driver's `assign`, ahead of turn 0) must retry once
        the session is free rather than pulling the supervised claude out
        from under a turn. Restarting the supervised process is how the CLI
        picks up a new `--model`/`--effort`; there is no way to change either
        on an already-running claude.
        """
        new_settings = replace(
            manager.settings,
            model=body.model,
            effort=body.effort,
            max_thinking_tokens=body.max_thinking_tokens,
        )
        try:
            manager.restart(new_settings)
        except BusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DeadError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"restarting": True, "model": body.model, "effort": body.effort}

    @app.delete("/v1/session", status_code=202, dependencies=guard)
    def delete_session(background: BackgroundTasks) -> dict:
        """End this container's life. The session is not restartable in place:
        one container plays one match, and the next match gets a fresh
        process with a fresh conversation. The exit runs as a background task
        so the 202 is on the wire before the listener goes away."""
        manager.shutdown()
        if on_exit is not None:
            background.add_task(on_exit)
        return {"stopping": True, "exiting": on_exit is not None}

    @app.get("/v1/transcript", dependencies=guard)
    def get_transcript() -> Response:
        path = Path(manager.transcript_path) if manager.transcript_path else None
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="no transcript yet")
        return Response(content=path.read_text(), media_type="application/jsonl")

    @app.post("/v1/probe", status_code=202, dependencies=guard)
    def post_probe(body: ProbeIn = Body(default=ProbeIn())) -> dict:  # noqa: B008
        try:
            probe_id = manager.probe(body.text)
        except ProbeUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        status = manager.probe_status(probe_id) or {}
        return {"probeId": probe_id} | {k: v for k, v in status.items() if k != "probeId"}

    @app.get("/v1/probe/{probe_id}", dependencies=guard)
    def get_probe(probe_id: str) -> dict:
        status = manager.probe_status(probe_id)
        if status is None:
            raise HTTPException(status_code=404, detail="unknown or superseded probe")
        return status

    @app.post("/v1/interrupt", status_code=202, dependencies=guard)
    def post_interrupt() -> dict:
        try:
            turn_id = manager.interrupt()
        except ProbeUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"turnId": turn_id}

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    def readyz() -> dict:
        snapshot = manager.snapshot()
        if snapshot["state"] in ("dead", "booting"):
            raise HTTPException(status_code=503, detail=snapshot["state"])
        return snapshot

    @app.get("/metrics")
    def metrics() -> Response:
        body, content_type = manager.metrics.render()
        return Response(content=body, media_type=content_type)

    return app


def build_internal_app(manager: Manager) -> FastAPI:
    app = FastAPI(title="mtg-agent-wrapper-internal")

    @app.post("/internal/turn-complete", status_code=202)
    def turn_complete(result: dict[str, Any]) -> dict:
        manager.complete(result)
        return {"accepted": True}

    return app
