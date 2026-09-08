"""Poll-and-return: `mcp.wait_for_priority` and every `run_action`-backed tool
never block, and a pending decision addressed to the caller comes back as an
MCP 2026-07-28 `input_required` result (`InputRequiredResult`,
`result_type: "input_required"` on the wire) rather than plain text a caller
could ignore.

MCP protocol revision 2026-07-28 removed resumability (SEP-2577): a tool call
that blocks waiting for something to happen loses the request outright if the
underlying stream breaks mid-wait. `seatkit.block_until_wake` used to hold one
open for up to `max_seconds` (570s by default for `wait_for_priority`); it now
checks once and returns immediately either way (`seatkit.NOTHING_NEW_YET`),
and this file is what pins "immediately" - not merely "eventually, with a
shorter bound".

`pass_until` was the last holdout on both counts: it kept its own deadline
loop (`seatkit.pass_until`, which slept to `max_seconds`) and it returned raw
JSON rather than going through `mcp._tool_result`, so a run that stopped *on* a
decision could not say so on the wire. Both are pinned here.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from fastapi.testclient import TestClient
from mcp_types import InputRequiredResult
from test_decisions import advance_into_cleanup, seats_for, stuff_hand

from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats


def wired(client: TestClient, monkeypatch, game_id: str) -> tuple[Any, dict[str, str]]:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id), seats_for(client, game_id)


def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(name, arguments))


def context_for(server: Any, protocol_version: str) -> Any:
    """A `Context` stamped with a negotiated protocol revision, the same
    shape `ServerRunner._make_context` builds per inbound message
    (`mcp/server/runner.py`) - `ServerRequestContext` is a plain
    `@dataclass`, so a placeholder `session` is enough; nothing here reads
    it. Lets a test drive `server.call_tool` as though a client had actually
    negotiated an older revision, without standing up a real transport."""
    from mcp.server.context import ServerRequestContext
    from mcp.server.mcpserver import Context

    request_context = ServerRequestContext(
        session=object(),
        lifespan_context={},
        protocol_version=protocol_version,
        method="tools/call",
    )
    return Context(
        mcp_server=server, subscriptions=server._subscriptions, request_context=request_context
    )


def test_wait_for_priority_returns_immediately_with_nothing_pending(client, game, monkeypatch):
    """No wake condition holds yet (alice already has priority and nothing
    has changed since she last looked - there is no `before` at all here, so
    every wake condition genuinely requires something this check cannot see
    on the pregame board): this must return fast, not after riding out
    `max_seconds`, and as an ordinary text result, not `input_required`."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)

    started = time.monotonic()
    result = call_tool(server, "wait_for_priority", {"player_uid": uids["bob"]})
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"wait_for_priority blocked for {elapsed:.1f}s - it must poll and return"
    assert not isinstance(result, InputRequiredResult)
    assert "NOTHING_NEW_YET" not in result.content[0].text  # sanity: real prose, not the const name
    assert "nothing new yet" in result.content[0].text


def test_wait_for_priority_returns_input_required_for_a_pending_decision(client, game, monkeypatch):
    """alice's own cleanup discard is pending: `wait_for_priority` must not
    just mention it in a wake_reason string a caller could skim past - it
    has to come back shaped as `input_required` on the wire, so an MCP client
    that understands the 2026-07-28 dual-result-type surface can tell "this
    call cannot proceed until you resolve something" apart from an ordinary
    text answer."""
    seats, uids = wired(client, monkeypatch, game)
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard: a real choice
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    paused = advance_into_cleanup(game)
    assert paused is not None, "the discard should have asked, not defaulted silently"

    server = build_server(seats)
    started = time.monotonic()
    result = call_tool(server, "wait_for_priority", {"player_uid": uids["alice"]})
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, (
        f"wait_for_priority blocked for {elapsed:.1f}s instead of returning at once"
    )
    assert isinstance(result, InputRequiredResult), (
        f"expected an InputRequiredResult, got {type(result)}: {result!r}"
    )
    assert result.request_state is not None
    assert "choose_cards" in result.request_state


def test_a_pending_decision_still_validates_on_a_pre_2026_client(client, game, monkeypatch):
    """The owner's live bug: `mcp._tool_result` built an `InputRequiredResult`
    whenever the pending decision belonged to this seat, with no regard for
    which protocol revision the connected client actually negotiated.
    `InputRequiredResult`'s wire shape carries no `content` field - fine
    against MCP 2026-07-28's own result surface, but `CallToolResult`'s
    (`content` required) is all any earlier revision knows, so a client that
    negotiated one of those got `Handler returned an invalid result` at
    exactly the moment it most needed an answer.

    Reproduced the way the real runner validates a handler's result before it
    ever reaches the wire - `mcp_types.methods.serialize_server_result`,
    the same check `ServerRunner._serialize` (`mcp/server/runner.py`) makes -
    rather than trusting the tool's own return type. A client negotiated onto
    `2025-06-18` is simulated by handing `server.call_tool` a `Context`
    stamped with that revision (`context_for`), the same one
    `ServerRunner._make_context` builds per inbound message in production."""
    from mcp_types.methods import serialize_server_result

    seats, uids = wired(client, monkeypatch, game)
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard: a real choice
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    paused = advance_into_cleanup(game)
    assert paused is not None, "the discard should have asked, not defaulted silently"

    server = build_server(seats)
    legacy = context_for(server, "2025-06-18")
    result = asyncio.run(
        server.call_tool("wait_for_priority", {"player_uid": uids["alice"]}, context=legacy)
    )
    # A pending decision is still real news on a legacy connection - it must
    # not silently vanish into an empty answer, only lose its distinct wire
    # shape.
    assert not isinstance(result, InputRequiredResult), (
        "a pre-2026-07-28 client cannot receive an InputRequiredResult at all"
    )
    dumped = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    text = "".join(block.get("text", "") for block in dumped.get("content", []))
    assert "choose_cards" in text, "the pending decision must still reach a legacy client, as text"
    # The check that actually failed for the owner: this must not raise.
    serialize_server_result("tools/call", "2025-06-18", dumped)


def test_a_pending_decision_still_shapes_as_input_required_on_a_2026_client(
    client, game, monkeypatch
):
    """The other half: a client that DID negotiate 2026-07-28 must keep
    getting the distinct `input_required` wire shape - the fix must not
    downgrade everyone to plain text to play it safe."""
    seats, uids = wired(client, monkeypatch, game)
    stuff_hand(client, game, "alice", 8)
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    paused = advance_into_cleanup(game)
    assert paused is not None

    server = build_server(seats)
    modern = context_for(server, "2026-07-28")
    result = asyncio.run(
        server.call_tool("wait_for_priority", {"player_uid": uids["alice"]}, context=modern)
    )
    assert isinstance(result, InputRequiredResult), (
        f"expected an InputRequiredResult, got {type(result)}: {result!r}"
    )


def test_wait_for_priority_does_not_shape_someone_elses_decision_as_input_required(
    client, game, monkeypatch
):
    """A decision pending for alice is not bob's `input_required` moment -
    bob's own `wait_for_priority` still has nothing new for him, plain
    text, not a claim that he must resolve something."""
    seats, uids = wired(client, monkeypatch, game)
    stuff_hand(client, game, "alice", 8)
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    paused = advance_into_cleanup(game)
    assert paused is not None

    server = build_server(seats)
    result = call_tool(server, "wait_for_priority", {"player_uid": uids["bob"]})
    assert not isinstance(result, InputRequiredResult)


def test_an_action_tool_also_surfaces_its_own_raised_decision_as_input_required(
    client, game, monkeypatch
):
    """Not only `wait_for_priority`: any `run_action`-backed tool (here,
    `pass`) that crosses into a decision addressed to this very
    seat - alice's own pass closing the priority round and walking the turn
    into her own cleanup discard - surfaces the same way, through the shared
    `mcp._tool_result` helper, not a second, divergent implementation."""
    seats, uids = wired(client, monkeypatch, game)
    stuff_hand(client, game, "alice", 8)
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})

    # Hand priority to bob first so alice's own pass is the one that closes
    # the round and crosses into her own cleanup discard.
    from magic_cards_model_api.store import store

    real_game = store.get(game)
    assert real_game.priority is not None
    real_game.priority.holder_index = real_game.priority.players.index("bob")
    real_game.priority.passes = 0
    bobs_pass = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert bobs_pass.status_code == 200, bobs_pass.text

    server = build_server(seats)
    result = call_tool(server, "pass", {"player_uid": uids["alice"]})
    assert isinstance(result, InputRequiredResult), (
        f"expected an InputRequiredResult, got {type(result)}: {result!r}"
    )
    assert result.request_state is not None
    assert "choose_cards" in result.request_state


def test_pass_until_also_surfaces_a_decision_as_input_required(client, game, monkeypatch):
    """`pass_until` is the longer-lived of the two priority verbs, and it used
    to return `json.dumps(...)` directly rather than going through
    `mcp._tool_result` - so the verb most likely to be mid-run when a decision
    arrives was the only one that could not say so on the wire.

    It is not a path where no decision can arise: `seatkit.pass_until` stops
    and returns the moment one is addressed to this seat, with the full state
    under `"state"` - the exact key `_tool_result` reads. Here alice's own
    auto-pass closes the priority round and walks her turn into her own
    cleanup discard.

    The same call also pins the other half of the fix: it returns at once
    instead of sleeping to a deadline. There is no `max_seconds` on this tool
    any more."""
    seats, uids = wired(client, monkeypatch, game)
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard: a real choice
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})

    from magic_cards_model_api.store import store

    real_game = store.get(game)
    assert real_game.priority is not None
    real_game.priority.holder_index = real_game.priority.players.index("bob")
    real_game.priority.passes = 0
    bobs_pass = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert bobs_pass.status_code == 200, bobs_pass.text

    server = build_server(seats)
    started = time.monotonic()
    result = call_tool(server, "pass_until", {"player_uid": uids["alice"], "step": "upkeep"})
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"pass_until blocked for {elapsed:.1f}s"
    assert isinstance(result, InputRequiredResult), (
        f"expected an InputRequiredResult, got {type(result)}: {result!r}"
    )
    assert result.request_state is not None
    assert "choose_cards" in result.request_state
    assert "stopped_because" in result.request_state, "it must still be a pass_until answer"


def test_pass_until_returns_promptly_when_it_must_wait(client, game, monkeypatch):
    """`pass_until` used to hold one request open for its whole
    `max_seconds` - measured at 6.0s for 6.0, and its MCP input schema exposed
    the parameter as an unbounded number, so an agent could hold a single call
    open for 570 seconds. bob has no priority here and nobody is moving: the
    call has to come straight back saying so, with a `resume` token, not sit
    there."""
    seats, uids = wired(client, monkeypatch, game)
    client.put(f"/api/games/{game}/step", json={"step": "precombat_main", "player": "alice"})

    server = build_server(seats)
    started = time.monotonic()
    result = call_tool(server, "pass_until", {"player_uid": uids["bob"], "step": "end_step"})
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"pass_until blocked for {elapsed:.1f}s"
    answer = json.loads(result.content[0].text)
    assert answer["finished"] is False
    assert answer["resume"], "an unfinished run must say how to continue it"
    assert "Call again" in answer["stopped_because"]


def test_max_seconds_lives_only_on_the_two_tools_that_can_genuinely_wait(client, game, monkeypatch):
    """Read off the live signature and the live MCP input schema, not off
    prose. This used to assert NO tool carried `max_seconds` at all - true
    when it bounded nothing (the old, since-removed unbounded deadline loop:
    `{"default": 60.0, "title": "Max Seconds", "type": "number"}` and no
    per-parameter description an agent could see corrected anywhere). The
    owner's report brought a *bounded* version back
    (`seatkit.MAX_WAIT_SECONDS`), deliberately, on exactly the two tools that
    can genuinely have nothing to do but wait for the opponent -
    `wait_for_priority` and `pass_until` - documented in their own
    docstrings this time. Every other action tool stays poll-and-return with
    no wait of its own: one check, one answer, always, the moment it acts."""
    import inspect

    seats, _ = wired(client, monkeypatch, game)
    server = build_server(seats)
    can_wait = {"wait_for_priority", "pass_until"}
    for name, tool in server._tool_manager._tools.items():
        parameters = inspect.signature(tool.fn).parameters
        if name in can_wait:
            assert "max_seconds" in parameters, f"{name} should take max_seconds, and does not"
        else:
            assert "max_seconds" not in parameters, (
                f"{name} takes max_seconds, but only wait_for_priority and "
                "pass_until can genuinely wait on anything"
            )


def test_both_briefs_tell_the_seat_to_use_max_seconds_not_bash_sleep():
    """The owner's report: an agent seat busy-polling with `Bash sleep 2`
    (then 3, 5, 15, 20, 25, 30 - an escalating hand-rolled backoff) instead
    of using the bounded wait the tools now support. That pattern was the
    brief's own instruction, not the agent improvising - `prompt.py` and
    `ai_seat_brief.md` both used to say, in so many words, "sleep, then
    check again". They now have to say the opposite: pass `max_seconds` (the
    seat_cli spelling is `--max-seconds`) to `wait_for_priority`/`pass_until`
    and let the tool call wait it out on the server, rather than spending a
    separate `Bash: sleep` and a whole extra turn on the same thing. This
    replaces `test_both_briefs_tell_the_seat_to_pace_its_own_polling`, which
    pinned the now-retired instruction - not deleting the coverage, since
    both files still have to teach a seat how to wait; just teaching the
    right tool for it."""
    from pathlib import Path

    from magic_cards_model_api import ai_seat
    from mtg_agent.driver import prompt as prompt_module

    brief = ai_seat.load_brief()
    driver_prompt = Path(prompt_module.__file__).read_text()

    for text, name in ((brief, "ai_seat_brief.md"), (driver_prompt, "prompt.py")):
        flat = " ".join(text.split())
        assert "max_seconds" in flat or "max-seconds" in flat, (
            f"{name} never tells the seat about the bounded wait"
        )
        assert "nothing new yet" in flat, f"{name} never says which answer means 'check again'"
        assert "resume" in flat, (
            f"{name} never tells the seat to hand back the resume token an "
            "unfinished pass_until run carries"
        )
        # The retired instruction itself - a bare "sleep, then check again"
        # as an isolated next step, with the wait tool nowhere in it - must
        # be gone. `sleep` can still appear describing the old pattern as
        # the thing NOT to do any more.
        assert "Bash: sleep 2\n\n" not in text, (
            f"{name} still hands the seat a bare sleep-then-call-again instruction"
        )
        assert "sleep 2 && " not in text, (
            f"{name} still hands the seat a bare sleep-then-call-again instruction"
        )
