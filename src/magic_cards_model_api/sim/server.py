"""The mtg-sim MCP server: sandbox tools, bearer auth and the /sim mount.

Mounted into the main app at `/sim` (MCP endpoint `/sim/mcp`). Every tool runs the blocking
manager call in a thread and returns a redacted dict; a refusal comes back as `{"error": ...}`
so a chat agent reads it as text instead of a protocol failure.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from magic_cards_model_api.sim.claude import ClaudeMatchManager
from magic_cards_model_api.sim.config import SimLimits
from magic_cards_model_api.sim.manager import BatchManager, Reaper, SandboxManager, SimRefused
from magic_cards_model_api.sim.redact import redact
from magic_cards_model_api.sim.sandbox_game import ACTIONS
from magic_cards_model_api.sim.scenario import validate_scenario

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
mtg-sim: throwaway Magic engine sandboxes for studying lines of play.
Nothing here touches a real game.
There is one sandbox slot for everyone and it frees after an idle TTL: end yours with sim_end.
Build a board with sim_new (players, cards, step), then act with sim_act and read with sim_state.
Or play a game against the baseline opponent with sim_play_new (you hold one seat).
Or run a whole scripted line with sim_run_script (setup, steps, expectations) in one call.
Or watch two Claude seats play one real game with sim_match_claude_start (slow: poll it).
Cards carry labels: give a card "label": "bolt" in the spec and address it as "@bolt" afterwards.
Always relay support_warnings (cards the engine does not fully support) to the user, and treat
any result that depends on them as unreliable.
A refused call returns {"error": ...} or {"allowed": false, "reason": ...}: read it, never retry
it blindly, and never loop on a refusal.
"""

_SPEC_DOC = (
    'spec shape: {"players": [{"name": "alice", "life": 20}, ...], "active_player": "alice",'
    ' "step": "precombat_main", "cards": [{"card": "Lightning Bolt", "player": "alice",'
    ' "zone": "hand", "label": "bolt", "counters": {}, "damage": 0, "summoning_sick": false}],'
    ' "mana_pool": {"alice": "R"}}. Unknown fields are refused.'
)
_LABELS_DOC = (
    'Labels: a card with "label": "x" is addressed as "@x" anywhere a card id is expected,'
    " in action bodies at any depth."
)
_ACTIONS_DOC = "Actions: " + ", ".join(ACTIONS) + "."


def _tool(call: Callable[[], dict[str, Any]], secrets: list[str] | None = None) -> Any:
    async def run() -> dict[str, Any]:
        try:
            return redact(await asyncio.to_thread(call), secrets or [])
        except SimRefused as exc:
            return {"error": str(exc)}

    return run()


def build_sim_server(
    manager: SandboxManager,
    limits: SimLimits,
    batches: BatchManager | None = None,
    claude: ClaudeMatchManager | None = None,
) -> MCPServer:
    batches = batches or BatchManager(limits)
    claude = claude or ClaudeMatchManager(limits)
    wait_s = max(0.0, limits.call_timeout_s - 5)  # leave room for the answer under the cap
    server = MCPServer(name="mtg-sim", instructions=INSTRUCTIONS)

    @server.tool(
        description=(
            "Create a sandbox game from a spec and return its id, the labels and the board.\n"
            "The single sandbox slot is global; a second call while it is taken is refused.\n"
            f"{_SPEC_DOC}\n{_LABELS_DOC}"
        )
    )
    async def sim_new(spec: dict[str, Any]) -> dict[str, Any]:
        return await _tool(lambda: manager.new(spec))

    @server.tool()
    async def sim_state(sandbox_id: str, full: bool = False) -> dict[str, Any]:
        """The compact board of a sandbox; full=True adds the whole redacted engine state."""
        return await _tool(lambda: manager.call(sandbox_id, "state", {"full": full}))

    @server.tool()
    async def sim_legal_actions(sandbox_id: str, player: str) -> dict[str, Any]:
        """What `player` may do right now in the sandbox, as engine action rows."""
        return await _tool(lambda: manager.call(sandbox_id, "legal_actions", {"player": player}))

    @server.tool(
        description=(
            "Run one engine action as `player` (seat actions) or as the sandbox admin.\n"
            "`body` is the action's request body; dry_run=True previews the actions that"
            f" support it.\n{_ACTIONS_DOC}\n{_LABELS_DOC}"
        )
    )
    async def sim_act(
        sandbox_id: str,
        action: str,
        player: str | None = None,
        body: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return await _tool(
            lambda: manager.call(
                sandbox_id,
                "act",
                {"action": action, "player": player, "body": body or {}, "dry_run": dry_run},
            )
        )

    @server.tool(
        description=(
            "Run a whole scenario in a fresh sandbox in one call and return per-step results,"
            " the initial and final board and a diff. The slot is freed afterwards unless"
            " keep=True (then the answer carries `sandbox` and you continue with sim_act).\n"
            'script: {"setup": <spec>, "steps": [{"act": {"action": "cast", "player": "alice",'
            ' "body": {...}}}, {"decide": {"player": "bob", "answer": {...}}},'
            ' {"expect": {"life": {"bob": 17}, "zones": {"bob.graveyard": ["@bears"]},'
            ' "step": "end", "winner": "alice"}}, {"note": "text"}], "answers": "stop"}.\n'
            "answers=stop halts at a decision the next step does not answer; answers=baseline"
            " answers it with the baseline seat. The run stops at the first refused act or when"
            " the time budget is used up; failed expects are reported, not fatal.\n"
            f"{_SPEC_DOC}\n{_LABELS_DOC}\n{_ACTIONS_DOC}"
        )
    )
    async def sim_run_script(script: dict[str, Any], keep: bool = False) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            try:
                validate_scenario(script, limits.max_script_steps)
            except ValueError as exc:
                raise SimRefused(f"invalid script: {exc}") from exc
            return manager.run_script(script, keep)

        return await _tool(run)

    @server.tool(
        description=(
            "Play a batch of engine-only games between two decks, both seats the zero-LLM"
            " baseline policy, and report win counts with 95% intervals per deck.\n"
            "deck_a / deck_b: a bundled deck name or the decklist text (multi-line). Decks"
            f" alternate who goes first. games: 1 to {limits.max_match_games}; seed makes"
            " the batch repeatable (random when omitted, and reported).\n"
            "The baseline plays weakly, so win rates are a rough signal about the lists under"
            " naive play, not about real matchups. Measured: a game takes 85 to 140 s on small"
            " decks and longer on full 45-card decks (0 of 10 finished in 300 s)."
            f" A batch has a {limits.match_wall_s:g} s wall-clock cap"
            " (env MTG_SIM_MATCH_WALL_S), so a match usually returns few or no finished"
            " games: status 'capped' returns the partial results (finished_games of"
            " requested_games).\n"
            "Answers at once or within the call limit: status 'running' with a batch_id means"
            " poll sim_match_status(batch_id) every 20 s or so; do not start a second batch,"
            " only one runs at a time. Relay support_warnings to the user;"
            " support_status 'pending' or 'unavailable' means card support is unchecked, so do"
            " not trust the results."
        )
    )
    async def sim_match(
        deck_a: str, deck_b: str, games: int = 10, seed: int | None = None
    ) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            return batches.status(batches.start(deck_a, deck_b, games, seed), wait_s)

        return await _tool(run)

    @server.tool()
    async def sim_match_status(batch_id: str) -> dict[str, Any]:
        """Poll a sim_match batch: status running, done, capped (partial) or failed, with the
        win counts of the games finished so far. Waits for news (up to the call timeout minus
        5 s)."""
        return await _tool(lambda: batches.status(batch_id, wait_s))

    @server.tool(
        description=(
            "Play a game yourself against the engine's baseline policy: you hold one seat, the"
            " baseline the other. Uses the sandbox slot (and its idle TTL); end it with sim_end.\n"
            "deck_me / deck_opponent: a bundled deck name or the decklist text (multi-line)."
            " seed makes the shuffle repeatable (random when omitted, and reported); me_first"
            " picks the seat order (omitted: the seed decides).\n"
            "You are the player 'you', the opponent 'baseline'. Read the board with sim_state and"
            " sim_legal_actions('you'), act with sim_act (player 'you' only; setup and engine"
            " actions are refused). You see your own hand and only the opponent's hand count."
            " Start with the mulligan decision: sim_act action 'mulligan', body {\"take\": false}"
            " keeps the hand.\n"
            "After every accepted action and every sim_state the baseline plays on until it is"
            " your move. Answers carry your_turn, game_over and opponent_moves (what the"
            " opponent did since your last call); a 'note' says the opponent is still playing"
            " (call sim_state again) or that nobody can move (consider action 'concede')."
            " A baseline game is slow: expect several calls per turn. Relay support_warnings"
            " to the user; support_status 'unavailable' means card support is unchecked."
        )
    )
    async def sim_play_new(
        deck_me: str,
        deck_opponent: str,
        seed: int | None = None,
        me_first: bool | None = None,
    ) -> dict[str, Any]:
        return await _tool(
            lambda: manager.new(
                {
                    "deck_me": deck_me,
                    "deck_opponent": deck_opponent,
                    "seed": seed,
                    "me_first": me_first,
                },
                mode="play",
            )
        )

    @server.tool(
        description=(
            "Start one game between two Claude AI seats (each a kubernetes seat Job of about"
            " 1.5Gi, playing like a real AI seat) and return a match_id at once.\n"
            "deck_a / deck_b: a bundled deck name or the decklist text (multi-line); seed makes"
            " the shuffle repeatable (random when omitted, and reported).\n"
            f"Only {limits.max_claude_matches} match runs at a time; a second start is refused."
            " A game takes about 33-55 min. The two seat Jobs may stay Pending while the"
            " cluster is full, which counts toward the time. A match is bounded by the seat"
            f" deadline ({limits.claude_deadline_s:g} s, env MTG_SIM_CLAUDE_DEADLINE_S), not by"
            " the 5-minute engine-only cap of sim_match. Poll sim_match_claude_status(match_id)"
            " about every 300 s; do not poll in a tight loop."
        )
    )
    async def sim_match_claude_start(
        deck_a: str, deck_b: str, seed: int | None = None
    ) -> dict[str, Any]:
        return await _tool(lambda: claude.start(deck_a, deck_b, seed))

    @server.tool(
        description=(
            "Poll a Claude-seat match; answers at once. status: starting (booting the game and"
            " creating the seat Jobs), running (turn, step and each seat's state: pending while"
            " the cluster is full, then thinking or idle), done"
            " (result: winner a or b or null, drawn, turns, the last 10 log lines), deadline"
            f" (stopped at the {limits.claude_deadline_s:g} s seat deadline, result so far) or"
            " failed (error: the start failed, or both seats are gone or in error). On done,"
            " deadline or failed both seats are stopped and the game deleted;"
            f" the result is kept {limits.result_ttl_s:g} s. Relay support_warnings to the"
            " user; support_status 'pending' or 'unavailable' means card support is unchecked."
        )
    )
    async def sim_match_claude_status(match_id: str) -> dict[str, Any]:
        return await _tool(lambda: claude.status(match_id))

    @server.tool()
    async def sim_end(sandbox_id: str) -> dict[str, Any]:
        """End the sandbox now and free the slot."""
        return await _tool(lambda: manager.end(sandbox_id))

    return server


def _env_token() -> str | None:
    # Read per request, not from the import-time limits, so a rotated or test-set token applies.
    return os.environ.get("MTG_SIM_TOKEN") or None


class SimAuth:
    """Pure ASGI bearer check in front of the MCP app; non-HTTP scopes pass through."""

    def __init__(self, app: Any, token_getter: Callable[[], str | None]) -> None:
        self._app = app
        self._token = token_getter

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        token = self._token()
        if not token:
            await self._reply(send, 503, {"error": "sim disabled: MTG_SIM_TOKEN is not configured"})
            return
        supplied = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                supplied = value.decode("latin-1")
                break
        scheme, _, credential = supplied.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            credential.encode(), token.encode()
        ):
            await self._reply(send, 401, {"error": "unauthorized"})
            return
        await self._app(scope, receive, send)

    @staticmethod
    async def _reply(send: Any, status: int, payload: dict[str, str]) -> None:
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class SimMount:
    """ASGI app for `app.mount("/sim", ...)` plus the FastAPI lifespan that feeds it.

    A fresh MCP app is built per lifespan entry: the SDK session manager runs once per
    instance, and the test suite enters the app lifespan once per TestClient.
    """

    def __init__(
        self,
        server: MCPServer,
        manager: SandboxManager,
        batches: BatchManager | None = None,
        claude: ClaudeMatchManager | None = None,
    ) -> None:
        self._server = server
        self._manager = manager
        self._batches = batches
        self._claude = claude
        self._current: Any = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self._current is None:
            if scope["type"] == "http":
                await SimAuth._reply(send, 503, {"error": "sim is not running"})
            return
        await self._current(scope, receive, send)

    @asynccontextmanager
    async def lifespan(self, _app: Any) -> AsyncIterator[None]:
        # DNS-rebinding protection off: the in-cluster Host header is not localhost, and the
        # bearer token (plus the ingress deny) is the guard (see MEMORY).
        starlette_app = self._server.streamable_http_app(
            stateless_http=True,
            streamable_http_path="/mcp",
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        self._manager.reopen()
        reaper = Reaper()
        reaper.register(self._manager.reap)
        if self._batches is not None:
            self._batches.reopen()
            reaper.register(self._batches.reap)
        if self._claude is not None:
            # A restart forgot every Claude match: stop and delete their games now.
            await asyncio.to_thread(_sweep_quietly, self._claude)
            reaper.register(self._claude.reap)
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(starlette_app.router.lifespan_context(starlette_app))
            self._current = SimAuth(starlette_app, _env_token)
            reaper.start()
            try:
                yield
            finally:
                self._current = None
                reaper.stop()
                await asyncio.to_thread(self._manager.shutdown)
                if self._batches is not None:
                    await asyncio.to_thread(self._batches.shutdown)


def _sweep_quietly(claude: ClaudeMatchManager) -> None:
    try:
        claude.sweep_orphans()
    except Exception as exc:  # noqa: BLE001 - a failed sweep must not stop the app
        logger.warning("claude sim orphan sweep failed (%s)", type(exc).__name__)


limits = SimLimits.from_env()
manager = SandboxManager(limits)
batches = BatchManager(limits)
claude = ClaudeMatchManager(limits)
mount = SimMount(build_sim_server(manager, limits, batches, claude), manager, batches, claude)
lifespan = mount.lifespan
