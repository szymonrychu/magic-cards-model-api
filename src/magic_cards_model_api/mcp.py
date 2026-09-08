"""One MCP toolset for the whole game: every tool takes the caller's seat uid.

Agents are dispatched with a secret uid each (printed by `serve` at boot and
handed out by whoever launches the players). The uid is the only way to claim
a seat - tools carry it as their first parameter, an unknown uid can neither
act nor see anything, and the hidden-information boundary is keyed off the
seat the uid opens: the opponent's hand and the contents of every library
never cross it. Everything else - battlefield, stack, graveyards, exile,
life, pools, combat - is public exactly as it is at a real table.

The filtering itself lives on the server (`visibility.py`), not here: every
call that can return a state view carries this seat's uid as a query
parameter, and the API answers already redacted. This module never sees the
unredacted state - it could not leak it if it tried, which is the point:
before, this file computed the correct answer and anything that talked to
the API directly (skipping this process) got the unfiltered one instead.

Every action tool below - `cast`, `play_land`, `suspend`, `plot`, `foretell`,
`cycle`, `activate`, `declare_attackers`, `declare_blockers`, `resolve_top`,
`advance`, `decide`, `pass_priority` - is "one call, one meaningful action":
it performs the action, checks once for this seat's next moment to act, and
returns the outcome plus whatever state that check saw, all in one answer.
This never blocks - MCP protocol revision 2026-07-28 removed resumability
(SEP-2577), so a tool call that holds the request open waiting for something
to happen loses it outright if the underlying stream breaks mid-wait -
poll-and-return instead (`seatkit.block_until_wake`'s own docstring): call
again to keep checking. A decision pending for the caller - whether this
very call raised it or something else did - arrives shaped `input_required`
(`InputRequiredResult`, `result_type: "input_required"` on the wire) rather
than as plain text a caller could skim past. The whole flow (wake
conditions, `hold_priority`, the no-deadlock invariant, the
`input_required` shaping) lives in `seatkit.run_action`/`mcp._tool_result`,
shared with `seat_cli.py` - see that module's docstring.

Whether the harness auto-passes priority on the seat's behalf is NOT uniform,
and the tools that differ say so in capitals in their own docstrings:

- `play_land`, `suspend`, `plot`, `foretell` and `cycle` **hold priority by
  default** (`hold_priority=True`). The first four are special actions, and
  CR 116.3 says "if a player takes a special action, that player receives
  priority afterward"; cycling is an activated ability (CR 702.29a) that
  earns the same treatment through CR 117.3c. Releasing after one of these
  throws away the rest of the seat's own main phase, which is exactly the bug
  that put this paragraph here.
- `activate` **holds priority whenever the ability turned out to be a mana
  ability**, whatever `hold_priority` said - a mana ability does not use the
  stack (CR 605.3b) and the auto-pass could end the step (CR 500.2) and empty
  the mana it just made (CR 500.5). See `seatkit.added_mana`.
- everything else releases priority unless told to hold, which is right for
  them: they use the stack, and releasing is how what you just did resolves.

This process is spawned once for the life of a whole playtest session and
never reloads its code - editing this file after that does not change what a
running process serves. The API, by contrast, is rebooted once per game (a
fresh `uv run python -m magic_cards_model_api.serve`), so this process must
outlive the API restarting underneath it: every tool below is wrapped in
`_guarded` (see there), which checks `seatkit.PROTOCOL_VERSION` and basic
reachability before each call and turns either problem into a clear, returned
error rather than an exception - a stale-protocol mismatch or an unreachable
API refuses that one call loudly instead of taking this process down with it.

Run one server for the game (not per player):

    uv run python -m magic_cards_model_api.mcp [--game <id>]
        [--api http://127.0.0.1:8000]

With no --game the single live game is picked automatically. Every tool
returns the API's JSON answer verbatim, engine refusal strings included.
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp_types import InputRequiredResult

from . import seatkit
from .seatkit import (
    _UUID,
    DEFAULT_API,
    NO_SUCH_SEAT,
    Seat,
    Seats,
    _parse_ids,
    _parse_pairs,
    run_action,
)


def _guarded(seats: Seats):
    """Wrap one tool function so the API restarting underneath this
    long-lived process - once per playtest game, see the module docstring -
    can never kill it. Before the call: `seatkit.api_trouble` checks
    reachability and `PROTOCOL_VERSION`, going through `seats.http` so tests
    that monkeypatch `httpx.Client` are honoured. Around the call: any
    `httpx.HTTPError` raised mid-request (the API dropping away between the
    check above and the actual call) is caught the same way rather than
    propagating out of the tool and taking the process down. Either problem
    is returned as a plain string - a normal tool answer - never raised.
    """

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            trouble = seatkit.api_trouble(seats.http, seats.api)
            if trouble:
                return trouble
            try:
                return fn(*args, **kwargs)
            except httpx.HTTPError as exc:
                return f"the game server at {seats.api} became unreachable mid-call: {exc}"

        return wrapper

    return decorate


def build_server(seats: Seats) -> MCPServer:
    server = MCPServer(name="mtg")

    def resolve(player_uid: str) -> Seat | None:
        return seats.seat_for(player_uid)

    def _tool_result(seat: Seat, payload: dict[str, Any]) -> str | InputRequiredResult:
        """Shape one tool's JSON answer, surfacing a decision pending for
        this very seat as an MCP 2026-07-28 `input_required` result
        (`InputRequiredResult`, `result_type: "input_required"` on the wire)
        instead of plain text.

        `payload` is either a whole state view (`wait_for_priority`) or a
        `run_action` result carrying one under `"state"` (every action tool,
        via `act`) - either shape puts `pending_decision` at the same key a
        level down from `payload` itself: `payload["pending_decision"]` for
        the former, `payload["state"]["pending_decision"]` for the latter.
        Checking both, rather than requiring a caller to say which shape it
        has, is what lets `wait_for_priority` and `act` share this one
        function instead of each hand-rolling the same check - a duplicate
        this project has paid for twice before (this module's own
        docstring).

        Only a decision addressed to *this* seat gets the treatment: one
        pending for the opponent is not this call's business to interrupt
        for, and comes back as ordinary text the same as "nothing new yet".
        An MCP client that understands the 2026-07-28 surface can branch on
        `resultType` without parsing prose; one that does not still gets a
        answer either way - `InputRequiredResult` is a distinct wire shape,
        never a crash for an older client, since `MCPServer` dispatches
        `CallToolResult`/`InputRequiredResult` by declared return type per
        tool (`Tool.from_function`'s own registration check)."""
        state = payload.get("state") if "state" in payload else payload
        pending = state.get("pending_decision") if isinstance(state, dict) else None
        text = json.dumps(payload, indent=1, sort_keys=True)
        if isinstance(pending, dict) and pending.get("player") == seat.player:
            return InputRequiredResult(request_state=text)
        return text

    def act(
        seat: Seat,
        perform: Any,
        hold_priority: bool,
        release: bool = True,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        result = run_action(
            seat, perform, hold_priority=hold_priority, release=release, reasoning=reasoning or None
        )
        return _tool_result(seat, result)

    @server.tool()
    @_guarded(seats)
    def wait_for_priority(player_uid: str) -> str | InputRequiredResult:
        """Check once whether it is your moment to act, then hand you the
                board - never blocks.

                The check looks for exactly one of these, right now:
                - a decision is waiting for you (CR 608.2d - use decide() to answer;
                  the answer itself arrives shaped `input_required` rather than
                  plain text, so an MCP client that understands the 2026-07-28
                  surface can tell at a glance that this call cannot go further
                  until you resolve it),
                - you have priority (act, or pass_priority),
                - the game is over (a winner is set, or it is a draw),
                - the step gives nobody priority (untap, cleanup) and you are the
                  active player - your move is to advance the turn.

        This used to poll for up to a caller-given `max_seconds` (570s by
                default), holding one request open the whole time; that parameter is
                gone rather than kept and documented as inert. MCP protocol revision
                2026-07-28 removed resumability (SEP-2577), so a call that blocks like
                that loses the request outright if the underlying stream breaks
                mid-wait - poll-and-return instead: one check, one answer, always.
                When nothing is new, the state's own `wake_reason` says so
                plainly (`nothing new yet - call again to check`) rather than making
                you wait to hear it. Every other action tool already checks the same
                way right after it runs, so you normally do not need this
                separately - it exists for "I have done nothing yet, is it my turn".
        """
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        woken = seatkit.block_until_wake(seat, before=None)
        woken["state"]["wake_reason"] = woken["wake_reason"]
        return _tool_result(seat, woken["state"])

    @server.tool()
    @_guarded(seats)
    def my_state(player_uid: str) -> str:
        """The game as you may see it: your hand, both sides of the public
        board (battlefield, stack, graveyards, exile), life and mana pools,
        turn/step, priority, and combat. The opponent's hand and both
        libraries are hidden - only their sizes are shown.

        `pending_decision` is the question you are being asked right now,
        answered with `decide` - see that tool for the field each `kind`
        wants. Two of its fields are easy to miss and matter most on a
        look-then-split (`scry`/`surveil`): `moved_label` and `kept_label`
        name where each half of your answer actually goes, in the asking
        card's own words ("top of your library", "exiled", "your graveyard",
        "bottom of your library"). They are authoritative whenever they are
        non-empty, and the `scry`/`surveil` kind alone is not - the engine
        reuses that kind for any look-then-split, including Devourer of
        Destiny's, which keeps the named card on top and exiles the rest.

        Three more of its fields carry a `choose_cards` - a discard, and
        every library search (CR 701.23a's find, asked as one choice over
        every legal candidate at once). `exact` says whether your answer
        must name exactly `amount` cards (CR 701.23d's quantity search) or
        may name any number from zero up to it (CR 701.23b's declinable
        search, where finding nothing is a real answer). `card_labels` names
        each `cards` entry positionally, and is what you read a hidden-zone
        candidate by - your own library is stripped from this state.
        `default_cards`, when non-null, is what the engine takes if you
        never answer."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        return json.dumps(seat.fetch_state(), indent=1, sort_keys=True)

    @server.tool()
    @_guarded(seats)
    def cast(
        player_uid: str,
        card: str,
        targets: str = "",
        mana_sources: str = "",
        modes: str = "",
        x_value: int | None = None,
        keyword: str = "",
        dry_run: bool = False,
        hold_priority: bool = False,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """Cast a spell. Most casts are from your hand, and need nothing
                else; `card` is the card's name or id. `targets` is comma-separated
                card ids or player names. `mana_sources` is comma-separated source
                ids; leave it empty to offer all your untapped mana sources (the
                engine taps only what the cost needs). Set `dry_run` to true to ask
                "could I cast this?" without changing anything (a dry run never
                blocks).

                A card sitting in your graveyard or exile is castable too, but only
                under a *named* permission (CR 601.3: "a player can begin to cast a
                spell only if a rule or effect allows that player to cast it"), and
                `keyword` is how you name it. Read your `my_state`'s graveyard/exile
                card - `cast_options` names every keyword that card currently offers,
                alongside the cost it would pay - rather than guessing; the keywords
                this engine knows are:

                - `"Flashback"` - a GRAVEYARD card with flashback (CR 702.34a).
                - `"Mayhem"` - a GRAVEYARD card with mayhem, discarded this turn
                  (CR 702.187a/b).
                - `"Plot"` - an EXILE card you plotted on an earlier turn
                  (CR 702.170d) - `exile_pile` on the card names which.
                - `"Foretell"` - an EXILE card you foretold on an earlier turn
                  (CR 702.143a).
                - `"Madness"` - an EXILE card put there by its own madness trigger
                  this turn (CR 702.35a/b).
                - `"Airbend"` - an EXILE card an airbend effect exiled, for {2}
                  (CR 701.65a).
                - a printed sentence opening one of the above from an extra zone
                  (CR 604.6/CR 113.6f), or a keyword another permanent's static
                  ability is granting the spell right now (CR 610.5, CR 611.3d) -
                  `keyword` is still that keyword's own name either way.

                A card you `suspend`d needs no `keyword`: once its last time counter
                is off (see `advance`'s `suspend_upkeep`), the free cast it grants is
                picked up automatically as a live `PlayPermission` - the same is true
                of cascade and Past in Flames, and `play_permissions` in your state
                names every one that is currently live for you.

        Otherwise this does the whole turn of events in one call: cast, then
                - unless `hold_priority` is true - release priority and check once
                for your next moment to act. **No tool in this server ever blocks or
                waits**, this one included (poll-and-return - MCP 2026-07-28 removed
                resumability, `seatkit.block_until_wake`'s own docstring); there is no
                `max_seconds` on any of them, because there is nothing left for one to
                bound. If `wake_reason` says "nothing new yet",
                call this (or `wait_for_priority`) again to keep checking. The
                answer carries the cast's own outcome, the state that check saw, and
                that `wake_reason` - except when the check finds a decision pending
                for you (yours to answer with `decide`, whether this cast itself
                raised it or something else did), in which case the whole answer
                arrives shaped `input_required` instead of plain text (an
                `InputRequiredResult`, `result_type: "input_required"` on the wire) -
                every tool built on this same flow shares that shape, not just this
                one. Set `hold_priority` to cast again, or to respond to your own
                spell, without giving up the chance to act again first (CR 117.3c:
                you already keep priority after casting - this only decides whether
                the harness auto-passes it for you).

        `reasoning`: why you are casting this, in your own words - required
        for an agent seat (a missing one is refused with a 422 naming the
        field), optional for a human. Every action tool takes it the same way."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        state = seat.fetch_state()
        body: dict[str, Any] = {"player": seat.player, "dry_run": dry_run}
        if _UUID.match(card.strip()):
            body["card_id"] = card.strip()
        else:
            body["card"] = card
        if targets:
            body["targets"] = _parse_ids(targets)
        sources = seat.sources_or_all(mana_sources, state)
        if sources:
            body["mana_sources"] = sources
        if modes:
            body["modes"] = [int(mode) for mode in _parse_ids(modes)]
        if x_value is not None:
            body["x_value"] = x_value
        if keyword:
            body["keyword"] = keyword
        if reasoning:
            body["reasoning"] = reasoning
        if dry_run:
            return seat.call("POST", f"/api/games/{seat.game_id()}/actions/cast", body)

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/cast", body)

        return act(seat, perform, hold_priority, reasoning=reasoning)

    def _special_action_tool(action: str) -> Any:
        """suspend/plot/foretell share `cast`'s exact shape, minus targeting
        and modes: locate a hand card, pay its own special-action cost, then
        release-and-wait like every other action tool - see `cast`'s
        docstring for the shared answer shape."""

        def tool(
            player_uid: str,
            card: str,
            mana_sources: str = "",
            dry_run: bool = False,
            hold_priority: bool = False,
            reasoning: str = "",
        ) -> str | InputRequiredResult:
            seat = resolve(player_uid)
            if seat is None:
                return NO_SUCH_SEAT
            state = seat.fetch_state()
            body: dict[str, Any] = {"player": seat.player, "dry_run": dry_run}
            if _UUID.match(card.strip()):
                body["card_id"] = card.strip()
            else:
                body["card"] = card
            sources = seat.sources_or_all(mana_sources, state)
            if sources:
                body["mana_sources"] = sources
            if reasoning:
                body["reasoning"] = reasoning
            if dry_run:
                return seat.call("POST", f"/api/games/{seat.game_id()}/actions/{action}", body)

            def perform() -> Any:
                return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/{action}", body)

            return act(seat, perform, hold_priority, reasoning=reasoning)

        return tool

    @server.tool()
    @_guarded(seats)
    def suspend(
        player_uid: str,
        card: str,
        mana_sources: str = "",
        dry_run: bool = False,
        hold_priority: bool = True,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """CR 116.2f/702.62a: exile a card with suspend from your hand, paying
        its suspend cost, with N time counters on it. `card` is the card's
        name or id in your hand. `mana_sources` is comma-separated source ids;
        leave it empty to offer all your untapped mana sources.

        Once N reaches zero (removed one per your own upkeep - see `advance`'s
        `suspend_upkeep`), you may cast it without paying its mana cost: call
        `cast` on it with no `keyword`, the permission is picked up
        automatically.

        HOLDS PRIORITY BY DEFAULT, unlike `cast`: suspending is a special
        action (CR 116.2f), and CR 116.3 says "if a player takes a special
        action, that player receives priority afterward". A special action
        does not use the stack (CR 116.1), so there is nothing to release
        priority for - releasing would just end your own main phase with the
        rest of your plan unplayed. Pass `hold_priority=false` only when you
        really do want to hand priority over - which still does not block or
        wait, no branch of this tool does. See `cast`'s docstring for the
        answer's shape."""
        return _special_action_tool("suspend")(
            player_uid, card, mana_sources, dry_run, hold_priority, reasoning
        )

    @server.tool()
    @_guarded(seats)
    def plot(
        player_uid: str,
        card: str,
        mana_sources: str = "",
        dry_run: bool = False,
        hold_priority: bool = True,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """CR 116.2k/702.170b: exile a card with plot from your hand, paying
        its plot cost. `card` is the card's name or id in your hand.
        `mana_sources` is comma-separated source ids; leave it empty to offer
        all your untapped mana sources.

        On any later turn, cast it from exile without paying its mana cost:
        call `cast` on it with `keyword="Plot"`.

        HOLDS PRIORITY BY DEFAULT, unlike `cast`: plotting is a special action
        (CR 116.2k, CR 702.170b), and CR 116.3 says "if a player takes a
        special action, that player receives priority afterward". A special
        action does not use the stack (CR 116.1), so there is nothing to
        release priority for - releasing would just end your own main phase
        with the rest of your plan unplayed. Pass `hold_priority=false` only
        when you really do want to hand priority over - which still does not
        block or wait, no branch of this tool does. See `cast`'s docstring
        for the answer's shape."""
        return _special_action_tool("plot")(
            player_uid, card, mana_sources, dry_run, hold_priority, reasoning
        )

    @server.tool()
    @_guarded(seats)
    def foretell(
        player_uid: str,
        card: str,
        mana_sources: str = "",
        dry_run: bool = False,
        hold_priority: bool = True,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """CR 116.2h/702.143b: pay {2} and exile a card with foretell from
        your hand, face down (opponents see only that a face-down card is
        there, not what it is). `card` is the card's name or id in your hand.
        `mana_sources` is comma-separated source ids; leave it empty to offer
        all your untapped mana sources.

        On any later turn, cast it from exile for its printed foretell cost
        (not its mana cost): call `cast` on it with `keyword="Foretell"`.

        HOLDS PRIORITY BY DEFAULT, unlike `cast`: foretelling is a special
        action (CR 116.2h, CR 702.143b), and CR 116.3 says "if a player takes
        a special action, that player receives priority afterward". A special
        action does not use the stack (CR 116.1), so there is nothing to
        release priority for - releasing would just end your own main phase
        with the rest of your plan unplayed. Pass `hold_priority=false` only
        when you really do want to hand priority over - which still does not
        block or wait, no branch of this tool does. See `cast`'s docstring
        for the answer's shape."""
        return _special_action_tool("foretell")(
            player_uid, card, mana_sources, dry_run, hold_priority, reasoning
        )

    @server.tool()
    @_guarded(seats)
    def cycle(
        player_uid: str,
        card: str,
        ability_index: int = 0,
        mana_sources: str = "",
        dry_run: bool = False,
        hold_priority: bool = True,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """CR 702.29a: discard a card with cycling from your hand, paying its
        cycling cost, to draw a card (or search your library for a card of the
        named type, for a typecycling ability). `card` is the card's name or
        id in your hand. `ability_index` picks which cycling ability, for the
        rare card printing more than one line ending "cycling" - almost
        always 0. `mana_sources` is comma-separated source ids; leave it
        empty to offer all your untapped mana sources.

        Unlike a mana ability, cycling uses the stack (it cannot add mana, so
        CR 605.1a's definition never catches it) and can be responded to - it
        does not resolve as part of this call.

        HOLDS PRIORITY BY DEFAULT, unlike `cast`. Note the warrant is NOT the
        one `suspend`/`plot`/`foretell` cite: cycling is not a special action
        at all, CR 702.29a makes it an ordinary activated ability
        ("'Cycling [cost]' means '[Cost], Discard this card: Draw a card.'").
        It is CR 117.3c that keeps priority with you - "if a player has
        priority when they cast a spell, activate an ability, or take a
        special action, that player receives priority afterward" - and the
        default is set here because a seat that cycles in its own main phase
        almost always means to keep playing, not to hand the turn over before
        the draw has even resolved. Pass `hold_priority=false` when you really
        do want to release (and note that the cycling ability only resolves
        once someone does) - releasing does not block or wait either, no
        branch of this tool does. See `cast`'s docstring for the answer's
        shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        state = seat.fetch_state()
        body: dict[str, Any] = {
            "player": seat.player,
            "ability_index": ability_index,
            "dry_run": dry_run,
        }
        if _UUID.match(card.strip()):
            body["card_id"] = card.strip()
        else:
            body["card"] = card
        sources = seat.sources_or_all(mana_sources, state)
        if sources:
            body["mana_sources"] = sources
        if reasoning:
            body["reasoning"] = reasoning
        if dry_run:
            return seat.call("POST", f"/api/games/{seat.game_id()}/actions/cycle", body)

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/cycle", body)

        return act(seat, perform, hold_priority, reasoning=reasoning)

    @server.tool()
    @_guarded(seats)
    def play_land(
        player_uid: str,
        card: str,
        hold_priority: bool = True,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """Play a land from your hand (your one-per-turn allowance, unless an
        effect says otherwise). `card` is the card's name or id.

        HOLDS PRIORITY BY DEFAULT, unlike `cast`. Playing a land is the special
        action of CR 116.2a, and CR 116.3 says "if a player takes a special
        action, that player receives priority afterward" - so releasing here
        throws away the rest of your own main phase. That is not theoretical:
        a seat played Flagstones of Trokair in precombat main, released, and
        concluded it would have to wait until postcombat to cast the one-drop
        the land had just paid for. Pass `hold_priority=false` only when you
        really do mean to hand priority over - which still does not block or
        wait, no branch of this tool does. See `cast`'s docstring for the
        answer's shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {"player": seat.player}
        if _UUID.match(card.strip()):
            body["card_id"] = card.strip()
        else:
            body["card"] = card
        if reasoning:
            body["reasoning"] = reasoning

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/play-land", body)

        return act(seat, perform, hold_priority, reasoning=reasoning)

    @server.tool()
    @_guarded(seats)
    def activate(
        player_uid: str,
        card_id: str,
        ability_index: int,
        targets: str = "",
        mana_sources: str = "",
        color: str = "",
        dry_run: bool = False,
        hold_priority: bool = False,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """Activate one ability of a permanent you control. Find the ability's
        number in your state view's `abilities` list for that card (indexing
        starts at 0; intrinsic mana abilities come after printed ones).
        `targets` and `mana_sources` are comma-separated ids as with casting.
        `color` is a single letter (W, U, B, R, G or C) and is required when
        the ability offers a choice of color to add - an "any color" source
        such as Birds of Paradise or Mox Diamond, or a dual/shockland; it is
        ignored by abilities that do not need one.

        Mana abilities resolve immediately and never touch the stack
        (CR 605.3b); every other ability goes on the stack and needs priority
        to be released before it resolves. This tool follows that split rather
        than a fixed default, because only the answer says which kind the
        ability was:

        - a MANA ability ALWAYS HOLDS PRIORITY, even with
          `hold_priority=false`. Releasing is half of "all players pass in
          succession while the stack is empty", which ends the step or phase
          (CR 500.2, CR 505.2), and "any unspent mana left in a player's mana
          pool empties" as it does (CR 500.5) - so an auto-pass here could
          bin the mana this very call just made. Spend it, or `pass_priority`
          deliberately to throw it away.
        - any other ability releases priority and then checks once for your
          next moment to act, unless `hold_priority` is true.

        Never blocks or waits, whichever branch it takes - see `cast`'s
        docstring for the full shape of the answer."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        state = seat.fetch_state()
        body: dict[str, Any] = {
            "player": seat.player,
            "card_id": card_id,
            "ability_index": ability_index,
            "dry_run": dry_run,
        }
        if targets:
            body["targets"] = _parse_ids(targets)
        sources = seat.sources_or_all(mana_sources, state)
        if sources:
            body["mana_sources"] = sources
        if color:
            body["color"] = color
        if reasoning:
            body["reasoning"] = reasoning
        if dry_run:
            return seat.call("POST", f"/api/games/{seat.game_id()}/actions/activate", body)

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/activate", body)

        return act(seat, perform, hold_priority, reasoning=reasoning)

    @server.tool()
    @_guarded(seats)
    def declare_attackers(
        player_uid: str,
        attackers: str,
        hold_priority: bool = False,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """Declare attackers during your declare attackers step. `attackers`
        is a JSON list: [{"card": "<attacker id>", "defender": "<player
        name or planeswalker id>"}]. Declare all attackers at once. Releases
        priority and then checks once for your next moment to act, unless
        `hold_priority` is true - it never blocks or waits; see `cast`'s
        docstring for the shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {
            "player": seat.player,
            "attackers": [
                {"card_id": pair["card"], "defender": pair["defender"]}
                for pair in _parse_pairs(attackers)
            ],
        }
        if reasoning:
            body["reasoning"] = reasoning

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/attack", body)

        return act(seat, perform, hold_priority, reasoning=reasoning)

    @server.tool()
    @_guarded(seats)
    def declare_blockers(
        player_uid: str,
        blocks: str,
        hold_priority: bool = False,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """Declare blockers during the declare blockers step. `blocks` is a
        JSON list: [{"blocker": "<your creature id>", "blocked": "<attacker
        id>"}]. Declare all blocks at once. Releases priority and then checks
        once for your next moment to act, unless `hold_priority` is true - it
        never blocks or waits; see `cast`'s docstring for the shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {
            "player": seat.player,
            "blocks": [
                {"blocker": pair["blocker"], "blocked": pair["blocked"]}
                for pair in _parse_pairs(blocks)
            ],
        }
        if reasoning:
            body["reasoning"] = reasoning

        def perform() -> Any:
            return seat.call_json(
                "POST", f"/api/games/{seat.game_id()}/actions/declare-blockers", body
            )

        return act(seat, perform, hold_priority, reasoning=reasoning)

    @server.tool()
    @_guarded(seats)
    def decide(
        player_uid: str,
        card_ids: str = "",
        targets: str = "",
        accept: bool | None = None,
        moved: str = "",
        top_order: str = "",
        energy_amount: int | None = None,
        life_amount: int | None = None,
        choice: str = "",
        top: bool | None = None,
        order: str = "",
        hold_priority: bool = False,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """Answer your pending decision (CR 608.2d) - `my_state`'s
        `pending_decision` field says what is being asked, by `kind`.

        - `choose_cards` (the cleanup discard, a "discard N" effect, a
          "reveal the top N, choose among them" effect - and **every
          library search**, a fetchland or a tutor): `card_ids` is a
          comma-separated list of ids chosen from `pending_decision.cards`.

          **How many you must name is `pending_decision.exact`, never the
          kind.** `True` means exactly `pending_decision.amount` ids - the
          cleanup discard, a bottoming. `False` means any number from zero
          up to `amount` (CR 107.1c: "if a rule or ability instructs a
          player to choose 'any number,' that player may choose any positive
          number or zero"). When `exact` is `False`, **the empty answer is a
          real answer, not a missing one**: leaving `card_ids` empty finds
          nothing, and that is the legal decline. When `exact` is `True`, an
          empty answer is refused, and so is any count other than `amount`.

          **A search is one choice over the whole zone, not a walk.**
          CR 701.23a: "to search for a card in a zone, look at all cards in
          that zone (even if it's a hidden zone) and find a card that matches
          the given description." So a search arrives as a *single*
          `choose_cards` whose `cards` holds **every legal candidate at
          once**, and your answer names the ones you actually want. You are
          never shown one card at a time and asked to accept or decline it,
          so never treat a candidate you do not want as the whole question:
          name the card you do want.

          Which branch you are in is `exact` again. CR 701.23b - "if a player
          is searching a hidden zone for cards with a stated quality, such as
          a card with a certain card type or color, that player isn't
          required to find some or all of those cards even if they're present
          in that zone" - is `exact` `False`: any 0..`amount` is legal, and
          finding nothing is a choice you may make. CR 701.23d - "if a player
          is searching a hidden zone simply for a quantity of cards, such as
          'a card' or 'three cards,' that player must find that many cards
          (or as many as possible, if the zone doesn't contain that many)" -
          is `exact` `True`: there you choose *which*, never *whether*.

          `pending_decision.source` names the card asking, and it is what
          tells you which find you are entitled to. A Wooded Foothills
          ("{T}, Pay 1 life, Sacrifice this land: Search your library for a
          Mountain or Forest card, put it onto the battlefield, then
          shuffle.") offers every card that matches, and a Breeding Pool is
          one of them - its type line is "Land - Forest Island", so it is a
          Forest card as surely as a basic Forest is. Read `source`, then
          pick the best candidate, not the first.

          **Read `pending_decision.card_labels` to know what the ids are.**
          It is one name per `cards` entry, positionally aligned with it -
          entry N of `card_labels` names entry N of `cards`. For a candidate
          in a hidden zone (CR 400.2: "public zones are zones in which all
          players can see the cards' faces ... graveyard, battlefield, stack,
          exile, ante, and command are public zones" - the library and the
          hand are not among them) your own state carries no detail to look
          the id up against: `my_state` strips every library, including your
          own. `pending_decision.card_details` carries the fuller view of the
          same ids where the server can resolve them, and is worth reading
          for oracle text; `card_labels` is the naming that is always there
          and always aligned. Answering off bare ids is playing the search
          blind, which is how the wrong land gets fetched.

          `pending_decision.default_cards`, when it is not null, is what
          your *silence* costs: the ids the engine takes if this decision
          times out unanswered, in place of the kind's generic default. For a
          search it is the first `amount` candidates in zone order - so an
          unanswered fetch still fetches, but it fetches whatever happened to
          sit nearest the top rather than the card you wanted. Check it
          before deciding a decision is not worth answering.
        - `choose_targets` (a triggered ability's targets, CR 603.3d, or a
          targeted ability being activated with no targets supplied,
          CR 602.2b/601.2c): `targets` is a comma-separated list of up to
          `pending_decision.target_maximum` entries, each a card or ability
          id or a player's name, chosen from `pending_decision.targets` -
          leave it empty to give the ability no target at all, which is
          always legal here (this wave does not yet refuse an empty answer to
          a trigger that requires one).
        - `may`, `pay_life`, `pay_energy`, and `pay_mana` (e.g. a shockland's
          "pay 2 life", CR 119.4; a fixed "pay {E}{E}", CR 107.14; or "counter
          target spell unless its controller pays {N}", CR 118.12a): `accept`
          is `True` to say yes, `False` to say no. A `pay_mana` decision is
          asked of the *targeted spell's controller*, not whoever cast the
          counterspell, and only mana already floating in that player's pool
          counts toward it - see `pending_decision.player` for who is
          actually being asked. `pending_decision.affirmative_is_legal` is
          `False` when yes is not a legal answer at all (too little life,
          energy, or floating mana to pay) - answering `True` there is
          refused.
        - `pay_energy_amount` (e.g. "pay any amount of {E}", CR 107.14,
          CR 118.3): `energy_amount` is an integer from 0 up to
          `pending_decision.amount` (the energy you actually have). Not the
          same question as `pay_energy`'s `accept` - this kind asks how much,
          not whether. 0 always declines and is always legal.
        - `pay_life_amount` (e.g. Necrodominance's "you may pay any amount of
          life", CR 119.4): `life_amount` is an integer from 0 up to
          `pending_decision.amount` (your life total). The variable-amount
          cousin of `pay_life`'s `accept`, the same way `pay_energy_amount`
          is `pay_energy`'s - and a distinct field from `energy_amount`,
          since the two pay from different resources. 0 always declines and
          is always legal (CR 119.4b).
        - `choose_a_quality` (e.g. Utopia Sprawl's "as this enters, choose a
          color", CR 614.12a): `choice` is the chosen color, card name, or
          creature type. `pending_decision.options` lists the legal colors
          when this is a color choice (the five letter codes); empty
          `options` means a card name or creature type, and any non-empty
          string is accepted - `Game.ask` is what actually enforces
          legality, so a bad card name or creature type still reaches the
          engine rather than being second-guessed here.
        - `choose_a_mode` (e.g. Elder Gargaroth's "whenever this creature
          attacks or blocks, choose one -", CR 603.3c, CR 700.2a): `choice`
          is the chosen mode's exact printed text, one of
          `pending_decision.options` (a modal ability's modes are always
          fully enumerated there, unlike a `choose_a_quality` color choice's
          five letters standing in for an open universe). Shares the
          `choice` field with `choose_a_quality` - both are "which of these
          printed strings did you pick".
        - `scry` and `surveil`: a look-then-split. `moved` is a
          comma-separated list of the `pending_decision.cards` entries you
          opt into; every card not named goes to the other half, in
          `top_order` (comma-separated ids, topmost first) if that half is
          ordered on top, or unchanged otherwise. Leaving `moved` empty is a
          real answer: it opts into nothing and is the decline.

          **Where the two halves go is read off
          `pending_decision.moved_label` and `pending_decision.kept_label`,
          never off the kind.** They are short phrases in the asking card's
          own words - "top of your library", "exiled", "your graveyard",
          "bottom of your library" - `moved_label` for the cards you named,
          `kept_label` for the ones you did not. When they are present they
          are authoritative; read them before answering, and quote them if
          you explain the choice.

          The kind is not the destination, because the engine reuses the
          `scry` kind for any look-then-split. Devourer of Destiny's
          first-upkeep trigger ("look at the top four cards of your library.
          You may put one of those cards back on top of your library. Exile
          the rest.") arrives as `scry`, and its `moved` half is kept *on
          top* while the rest is *exiled* - the reverse of a scry on both
          halves. `moved` is the pile you opt into, not "the cards that leave
          the top".

          Only when both labels are absent or empty may you assume the
          keyword's own pair: to "scry N" is to put any number of the looked
          at cards on the bottom of your library and the rest on top
          (CR 701.22a), and to "surveil N" is to put any number of them into
          your graveyard and the rest on top (CR 701.25a). That fallback is
          right for a decision scry or surveil itself raised and wrong for
          everything else, so prefer the labels.

          `pending_decision.cards` is still bare ids
          (`moved`/`top_order` answer by id), but `pending_decision.card_details`
          - present only for the deciding player - carries the full card view
          (name, oracle text, etc.) for exactly those ids, so a `scry`/`surveil`
          (a *look*, CR 701.20e: shown only to the deciding player) or a
          library-sourced `choose_cards` is never answered blind. Look the id
          up in `card_details` before choosing.
        - `opening_hand_action` (Leyline of the Void's "if this card is in
          your opening hand, you may begin the game with it on the
          battlefield", CR 103.6a; or a card that lets you reveal it from
          your opening hand, CR 103.6b): `accept` is `True` to take the
          action, `False` to decline - "may take any such actions"
          (CR 103.6), so no is always a real answer. This one arrives before
          the first turn has begun, right after the mulligan process, so it
          may well be the very first thing a seat is asked; answering it is
          what lets the game reach turn one. `pending_decision.source` names
          the card offering it.
        - `put_on_top_or_bottom` (Subtlety's "its owner puts it on the top or
          bottom of their library"): `top` is `True` for the top, `False` for
          the bottom. Not `accept` - this is a top/bottom choice, not a
          yes/no, even though both are booleans. `pending_decision.player` is
          the *targeted spell's owner* here, not Subtlety's controller
          (CR 108.3) - check it before answering, since in a two-player game
          it is easy to assume it is always the caster.
        - `order_triggers` (CR 603.3b: "if multiple abilities have triggered
          since the last time a player received priority ... each player, in
          APNAP order, puts each triggered ability they control on the stack
          in any order they choose"): `order` is a comma-separated
          permutation of exactly `pending_decision.cards` - which for this
          kind holds triggered-ability ids, not card ids - **bottom of the
          stack first**, so the *last* id you list is the ability that
          resolves *first*. Every offered id must appear exactly once;
          dropping or repeating one is refused.
          Do not answer this one off the ids alone. Read each id in
          `pending_decision.card_details` first, the same way you would for a
          `scry`: for this kind that block carries the ability's *source*
          name and the ability's own printed text (CR 113.1c makes an ability
          on the stack an object, so it has no card of its own to name it
          by). When an id is missing from `card_details` - CR 603.3b is asked
          one step before CR 603.3's "its controller puts it on the stack",
          so a batch of triggers can be in no zone yet - fall back to
          `pending_decision.prompt`, which numbers each source positionally
          against `cards`: entry N of the prompt is entry N of `cards`.

        `may`, `pay_life`, `scry`, and `surveil` were not answerable through
        this tool before this wave: the request simply parked until its
        timeout and the engine's documented default was taken instead,
        silently discarding the choice (see `decisions.py`'s module docstring
        on the API side for the CR 608.2d fallback rule).

        `mulligan`, `declare_attackers`, and `declare_blockers` are answerable
        through this same tool too, exactly like every other kind - there is
        no longer a decision kind this tool cannot answer. `declare_attackers`/
        `declare_blockers` are also reachable through the dedicated
        `attackers`/`blockers` actions, which build the same pair-shaped
        answer this tool takes for those two kinds and resume the identical
        paused decision - use whichever reads more naturally for the call
        you are making, they are two doors onto the same thing, not two
        different mechanisms. `mulligan` reaches you through
        `pregame/mulligan`'s own declaration for your own seat during a
        normal game boot (CR 103.5) - this tool's `mulligan` kind is what you
        answer only if an admin's `pregame/settle` call is what actually put
        the question to you (and only when that call was made with
        `use_defaults` left `False`, its default - a `use_defaults=True`
        settle never pauses at all, it pre-fills every such question with
        the engine's own default before the call returns), which is not the
        ordinary play path. Answering when nothing is pending, or when the
        pending decision is not yours, is refused rather than accepted.

        Answering a decision does not, itself, involve priority (CR 608.2d is
        the resolving effect asking a question, not a priority action), but
        follows the same shape as every other tool here for one reason:
        answering it is very often what was blocking your own next turn at
        priority, so waking straight back into whatever comes next saves the
        extra round trip. Releases priority and then checks once for your
        next moment to act, unless `hold_priority` is true - it never blocks
        or waits."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {
            "player": seat.player,
            "card_ids": _parse_ids(card_ids),
            "targets": _parse_ids(targets),
            "moved": _parse_ids(moved),
        }
        if accept is not None:
            body["accept"] = accept
        if top_order:
            body["top_order"] = _parse_ids(top_order)
        if energy_amount is not None:
            body["energy_amount"] = energy_amount
        if life_amount is not None:
            body["life_amount"] = life_amount
        if choice:
            body["choice"] = choice
        if top is not None:
            body["top"] = top
        if order:
            body["order"] = _parse_ids(order)
        if reasoning:
            body["reasoning"] = reasoning

        def perform() -> Any:
            answer = seat.call_json("POST", f"/api/games/{seat.game_id()}/decide", body)
            if isinstance(answer, dict) and answer.get("accepted") is False:
                return {"allowed": False, "reason": answer.get("reason"), **answer}
            return {"allowed": True, **answer} if isinstance(answer, dict) else answer

        return act(seat, perform, hold_priority, reasoning=reasoning)

    @server.tool()
    @_guarded(seats)
    def pass_priority(
        player_uid: str,
        until: str = "",
        until_whose_turn: str = "mine",
        resume: str = "",
        stop_on_stack: bool = True,
        stop_when_attacked: bool = True,
        stop_when_you_can_attack: bool = True,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """Pass priority - "I am done for now". If everyone passes with
        spells on the stack, the top of the stack resolves. If everyone
        passes with the stack empty, the step ends and the game rolls to the
        next step on its own (the outcome says "end_step") - you do NOT call
        advance() for that.

        Checks once for your next moment to act and returns immediately -
        never blocks (poll-and-return, `seatkit.block_until_wake`'s own
        docstring: MCP 2026-07-28 removed resumability). Call again to keep
        checking, the same as any other action tool. There is no
        `hold_priority` here (passing already is giving priority up); a
        refusal (it was not your turn to decide) returns immediately either
        way, the same as it always did. A decision this very pass raises for
        you comes back shaped `input_required` rather than plain text - see
        `wait_for_priority`'s own docstring.

        ## `until`: pass repeatedly, up to a named point in the turn

        Set `until` to a step name and this keeps passing every priority it
        is given until it gets there, instead of handing you back one
        priority at a time. `until_whose_turn` says whose turn that step has
        to be in: "mine" (default), "opponent", or "any" for whichever comes
        first. Legal steps, in turn order:

            upkeep, draw, precombat_main, beginning_of_combat,
            declare_attackers, declare_blockers, combat_damage,
            end_of_combat, postcombat_main, end_step

        (`untap` and `cleanup` are not offered: no player receives priority
        in either, CR 502.4 and CR 514.3, so a run could never stop there.)

        Naming the step you are standing in right now means its NEXT
        occurrence, never this one: `until="upkeep"` asked during your upkeep
        passes a whole turn round to your next one, which is what "until my
        upkeep" means when you say it out loud.

        **`until` does not block either.** It passes every priority you
        currently hold - one after another, with no waiting between them,
        each paid for by a pass that already went through - and returns the
        moment it would otherwise have to wait for your opponent. It never
        sleeps and never sits on a deadline; there is no `max_seconds`, and
        there never should have been one.

        `finished` says which kind of answer you got:

        - `finished: true` - a real stop condition fired, and
          `stopped_because` says which: the destination is reached; a
          decision is addressed to you (CR 608.2d), which arrives shaped
          `input_required`; something new landed on the stack; it is your own
          declare-attackers step; you are being attacked and it is your
          declare-blockers step; a pass was refused; the game ended.
        - `finished: false` - the run got as far as it could on its own and
          handed the board back, because your opponent holds priority or owes
          a declaration only they can make. The answer carries a `resume`
          token; **pass it back verbatim as `resume` on your next call**, and
          the run continues as one run. Without it the next call starts a
          fresh run stamped where you are standing right now, which would
          make it skip the very combat step it is supposed to stop you at
          (the exemption for "the step the run began in" - see below).

        `passes` counts the priorities it actually gave up, in this call.

        A combat stop never fires on the step the run *started* in, the same
        way the destination never does: asking to auto-pass past combat while
        already standing in that step is you having looked at combat and moved
        on, and stopping there would make the call do nothing at all.

        ## The three stop conditions you can switch off

        All three default to true, because each defends against something you
        cannot see going wrong. They are the same three checkboxes the human
        player has in their own auto-pass panel, and they mean the same thing
        there, so you and your opponent never stop in different places.

        - `stop_on_stack=false` runs straight through anything the opponent
          casts or any trigger that goes on the stack. You will not get the
          chance to respond to it, so this is only for a stretch where you
          have decided you are not responding to anything - no instant, no
          activated ability, nothing worth holding up.
        - `stop_when_attacked=false` runs straight through your own
          declare-blockers step. **You will block nothing and take the whole
          attack.** Declaring blockers is a turn-based action that "doesn't
          use the stack" (CR 509.1), so nothing lands on the stack when you
          are attacked and `stop_on_stack` cannot catch it for you. Switch
          this off only when you have genuinely decided not to block - an
          empty board, or a life total the attack cannot reach. The run stops
          whenever you are the defending player in a declare-blockers step
          with at least one attacker declared; it does not try to work out
          whether any block would be *legal* (evasion, menace, protection,
          CR 509.1b) - that is the engine's job, and you decide what to do
          with the stop.
        - `stop_when_you_can_attack=false` runs straight through your own
          declare-attackers step. **You will attack with nothing that turn.**
          Declaring attackers is a turn-based action that "doesn't use the
          stack" too (CR 508.1), so again nothing lands on the stack to catch.
          This costs you an attack rather than the game, and the reason it
          still stops by default is that skipping it is *silent*: the engine
          treats a player who never declares as declaring nobody, which is
          always legal, so the turn simply produces no attack and nothing
          anywhere tells you why. Leaving it on means a run aimed past combat
          - `until="postcombat_main"`, `until="end_step"` - stops in your
          declare-attackers step every one of your turns. That is the point:
          you are being asked before the attack goes by. Switching it off is
          the deliberate statement "I am not attacking this turn", not a way
          to make the call quieter. It does not check whether you control a
          creature that could legally attack (summoning sickness, defender,
          tap state, CR 508.1's restrictions and requirements) - that is the
          engine's job too.

        ## Passing by hand is still a play

        Do not reach for `until` every time. A pass your opponent watches you
        make is information: holding priority with mana up represents a trick
        whether or not you have one, and auto-passing a window where they
        expect a flash threat throws that away. Auto-passing every time also
        tells them "I have nothing" just as reliably. Use `until` for the
        stretches where you genuinely cannot act, and pass by hand where the
        pass itself is part of the game."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        if until:
            problem = seatkit.autopass_target_error(until, until_whose_turn)
            if problem:
                return problem
            try:
                resumed = json.loads(resume) if resume else None
            except ValueError as exc:
                return (
                    f"`resume` is not JSON: {exc}. Hand it back exactly as the "
                    "previous answer gave it to you, or leave it out to start a new run."
                )
            if resumed is not None and not isinstance(resumed, dict):
                return (
                    "`resume` must be the object a previous `pass_priority(until=...)` "
                    "answer carried under `resume`, handed back unchanged."
                )
            # Through `_tool_result`, not `json.dumps`: a run stops *on* a
            # decision addressed to this seat (`seatkit.pass_until` returns
            # the moment `autopass_stop_reason` sees one, with the whole
            # state under `"state"`), which is exactly the case
            # `InputRequiredResult` exists for. This was the one action tool
            # returning raw text on that path, so the one tool most likely to
            # be mid-run when a decision arrives was also the only one that
            # could not say so on the wire.
            return _tool_result(
                seat,
                seatkit.pass_until(
                    seat,
                    until_step=until,
                    whose_turn=until_whose_turn,
                    stop_on_stack=stop_on_stack,
                    stop_when_attacked=stop_when_attacked,
                    stop_when_you_can_attack=stop_when_you_can_attack,
                    resume=resumed,
                    reasoning=reasoning or None,
                ),
            )
        body: dict[str, Any] = {"player": seat.player}
        if reasoning:
            body["reasoning"] = reasoning

        def perform() -> Any:
            return seat.call_json(
                "POST", f"/api/games/{seat.game_id()}/actions/pass-priority", body
            )

        return act(seat, perform, hold_priority=False, release=False, reasoning=reasoning)

    @server.tool()
    @_guarded(seats)
    def resolve_top(
        player_uid: str,
        hold_priority: bool = False,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """Resolve the top object on the stack. Normally the engine does this
        for you when everyone passes; use this only if a pass left the stack
        unresolved. Releases priority and then checks once for your next
        moment to act, unless `hold_priority` is true - it never blocks or
        waits; see `cast`'s docstring for the shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {"player": seat.player}
        if reasoning:
            body["reasoning"] = reasoning

        def perform() -> Any:
            answer = seat.call_json("POST", f"/api/games/{seat.game_id()}/resolve-top", body)
            return {"allowed": answer.get("reason") is None, **answer}

        return act(seat, perform, hold_priority, reasoning=reasoning)

    @server.tool()
    @_guarded(seats)
    def advance(
        player_uid: str,
        hold_priority: bool = False,
        reasoning: str = "",
    ) -> str | InputRequiredResult:
        """Move to the next step or phase. Passing priority already rolls the
        game through every step where players get priority; this is only for
        the steps nobody gets priority in - untap and cleanup on your own
        turn. Releases priority and then checks once for your next moment to
        act, unless `hold_priority` is true - it never blocks or waits; see
        `cast`'s docstring for the shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {"player": seat.player}
        if reasoning:
            body["reasoning"] = reasoning

        def perform() -> Any:
            return {
                "allowed": True,
                **seat.call_json("POST", f"/api/games/{seat.game_id()}/advance", body),
            }

        return act(seat, perform, hold_priority, reasoning=reasoning)

    @server.tool()
    @_guarded(seats)
    def set_step(player_uid: str, step: str, reasoning: str = "") -> str:
        """Jump the turn marker to a named step ("precombat_main",
        "beginning_of_combat", "declare_attackers", "declare_blockers",
        "combat_damage", "end_step", ...). Forward moves are real step
        changes; use advance() for ordinary flow. Not a priority action -
        does not block.

        `reasoning`: why you are moving the marker there, in your own words -
        required, and refused without it. This one moves the game's own turn
        structure and can fire the suspend upkeep on the way in, so it is a
        real move, not board setup."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body = {"step": step, "player": seat.player}
        if reasoning:
            body["reasoning"] = reasoning
        return seat.call("PUT", f"/api/games/{seat.game_id()}/step", body)

    @server.tool()
    @_guarded(seats)
    def mulligan(player_uid: str, take: bool, bottom: str = "", reasoning: str = "") -> str:
        """Declare whether you take a mulligan (CR 103.5), before the game starts.

        A game dealt from decklists rests in the mulligan step until every seat
        has declared - `my_state`'s `pregame` block says who is still owed.
        `take=true` shuffles your hand back and draws a fresh seven;
        `take=false` keeps what you have.

        Keeping costs one card on the bottom of your library per mulligan you
        took (`pregame.cards_to_bottom`). `bottom` is a comma-separated list of
        that many card ids from your hand; leave it empty to let the back of
        your hand go. Does not block.

        An `HTTP 409` with `overruled` means an automatic backstop had already
        declared for this seat because it had gone quiet, and this declaration
        was NOT applied. Retrying cannot help and nothing you can send will
        change it - if you meant to mulligan, that decision is gone and you are
        holding the dealt hand; say so with `report_problem` and play on."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        ids = _parse_ids(bottom)
        body: dict[str, Any] = {"player": seat.player, "take": take}
        if ids:
            body["bottom"] = ids
        if reasoning:
            body["reasoning"] = reasoning
        return seat.call("POST", f"/api/games/{seat.game_id()}/pregame/mulligan", body)

    @server.tool()
    @_guarded(seats)
    def opening_hand_action(player_uid: str, card: str = "", reasoning: str = "") -> str:
        """Take a card's opening-hand action, or finish (CR 103.6).

        Some cards let you act with them from your opening hand - Leyline of
        the Void's "you may begin the game with this card on the battlefield",
        Gemstone Caverns' reveal. `my_state`'s `pregame.opening_hand_options`
        lists what yours offers, and `pregame.opening_hand_actor` says whose
        turn it is.

        `card` is one card id from that list. Call with no `card` when you are
        done - that passes to the next seat, and the last seat finishing starts
        turn 1. Does not block.

        An `HTTP 409` with `overruled` means an automatic backstop had already
        declined this window for the seat because it had gone quiet, and your
        action was NOT taken - the card is still in your hand and will stay
        there. Retrying cannot reopen the window; report it and play on."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        base = f"/api/games/{seat.game_id()}/pregame/opening-hand"
        if not card.strip():
            body: dict[str, Any] = {"player": seat.player}
            if reasoning:
                body["reasoning"] = reasoning
            return seat.call("POST", f"{base}/finish", body)
        body = {"player": seat.player, "card_id": card.strip()}
        if reasoning:
            body["reasoning"] = reasoning
        return seat.call("POST", f"{base}/action", body)

    @server.tool()
    @_guarded(seats)
    def report_problem(player_uid: str, text: str) -> str:
        """Report a problem with the engine straight into the game log: write
        what you did, what the rules say should have happened, and what
        happened instead. Use it the moment something looks wrong, then carry
        on - also write the same problem to your problems.md."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body = {"player": seat.player, "text": text}
        return seat.call("POST", f"/api/games/{seat.game_id()}/report", body)

    @server.tool()
    @_guarded(seats)
    def game_log(player_uid: str, limit: int = 40) -> str:
        """The game's action log: everything both players did or tried,
        allowed and refused, newest last - your own view, though: entries
        touching the opponent's hidden zones have their card names blanked
        the same way my_state hides them. `limit` keeps only the last N
        entries."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        path = f"/api/games/{seat.game_id()}/log"
        answer = seat.http.get(path, params={"uid": seat.uid})
        try:
            entries = answer.json().get("log", [])[-limit:]
            return json.dumps(entries, indent=1, sort_keys=True)
        except ValueError:
            return answer.text

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", default=None, help="game id; default: the live game")
    parser.add_argument("--api", default=DEFAULT_API, help=f"API base url; default {DEFAULT_API}")
    args = parser.parse_args()
    seats = Seats(args.api, args.game)
    # Best-effort heads-up only: NOT fatal. The real, authoritative check is
    # `_guarded` on every tool call below - this process is spawned once for
    # a whole session and must survive the API not being up yet (or not up
    # again yet, mid-restart) at the exact moment it boots.
    trouble = seatkit.api_trouble(seats.http, seats.api)
    if trouble:
        print(f"mcp: starting anyway - {trouble}", file=sys.stderr)
    build_server(seats).run("stdio")


if __name__ == "__main__":
    main()
