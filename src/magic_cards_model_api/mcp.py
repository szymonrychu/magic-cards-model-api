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

Every action tool below - `cast`, `play_land`, `activate`, `declare_attackers`,
`declare_blockers`, `resolve_top`, `advance`, `decide`, `pass_priority` - is
"one call, one meaningful action": it performs the action, releases priority
unless told to hold, blocks until this seat next has something to do, and
returns the outcome plus the state it woke into, all in one answer. The whole
flow (wake conditions, `hold_priority`, the no-deadlock invariant) lives in
`seatkit.run_action`, shared with `seat_cli.py` - see that module's docstring.

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

from . import seatkit
from .seatkit import (
    _UUID,
    DEFAULT_API,
    DEFAULT_MAX_SECONDS,
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

    def act(
        seat: Seat,
        perform: Any,
        hold_priority: bool,
        max_seconds: float,
        release: bool = True,
    ) -> str:
        result = run_action(
            seat, perform, hold_priority=hold_priority, max_seconds=max_seconds, release=release
        )
        return json.dumps(result, indent=1, sort_keys=True)

    @server.tool()
    @_guarded(seats)
    def wait_for_priority(player_uid: str, max_seconds: float = 570.0) -> str:
        """Block until it is your moment to act, then hand you the board.

        Returns as soon as one of these holds:
        - a decision is waiting for you (CR 608.2d - use decide() to answer),
        - you have priority (act, or pass_priority),
        - the game is over (a winner is set, or it is a draw),
        - the step gives nobody priority (untap, cleanup) and you are the
          active player - your move is to advance the turn.

        Bounded by `max_seconds` (default 570s): on expiry the answer's
        `wake_reason` says "timed out" and carries the current state anyway -
        call again to keep waiting. Every other action tool already blocks
        the same way after it runs, so you normally do not need this
        separately - it exists for "I have done nothing yet, is it my turn".
        """
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        woken = seatkit.block_until_wake(seat, before=None, max_seconds=max_seconds)
        woken["state"]["wake_reason"] = woken["wake_reason"]
        return json.dumps(woken["state"], indent=1, sort_keys=True)

    @server.tool()
    @_guarded(seats)
    def my_state(player_uid: str) -> str:
        """The game as you may see it: your hand, both sides of the public
        board (battlefield, stack, graveyards, exile), life and mana pools,
        turn/step, priority, and combat. The opponent's hand and both
        libraries are hidden - only their sizes are shown."""
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
        dry_run: bool = False,
        hold_priority: bool = False,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> str:
        """Cast a spell from your hand. `card` is the card's name or id.
        `targets` is comma-separated card ids or player names. `mana_sources`
        is comma-separated source ids; leave it empty to offer all your
        untapped mana sources (the engine taps only what the cost needs).
        Set `dry_run` to true to ask "could I cast this?" without changing
        anything (a dry run never blocks).

        Otherwise this does the whole turn of events in one call: cast, then
        - unless `hold_priority` is true - release priority and block until
        you next have something to do (`max_seconds` bounds the wait). The
        answer carries the cast's own outcome, the state you woke into, and a
        `wake_reason`. Set `hold_priority` to cast again, or to respond to
        your own spell, without giving up the chance to act again first (CR
        117.3c: you already keep priority after casting - this only decides
        whether the harness auto-passes it for you)."""
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
        sources = _parse_ids(mana_sources) or seat.my_untapped_sources(state)
        if sources:
            body["mana_sources"] = sources
        if modes:
            body["modes"] = [int(mode) for mode in _parse_ids(modes)]
        if x_value is not None:
            body["x_value"] = x_value
        if dry_run:
            return seat.call("POST", f"/api/games/{seat.game_id()}/actions/cast", body)

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/cast", body)

        return act(seat, perform, hold_priority, max_seconds)

    @server.tool()
    @_guarded(seats)
    def play_land(
        player_uid: str,
        card: str,
        hold_priority: bool = False,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> str:
        """Play a land from your hand (your one-per-turn allowance, unless an
        effect says otherwise). `card` is the card's name or id. Releases
        priority and blocks for your next moment to act unless
        `hold_priority` is true - see `cast`'s docstring for the shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {"player": seat.player}
        if _UUID.match(card.strip()):
            body["card_id"] = card.strip()
        else:
            body["card"] = card

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/play-land", body)

        return act(seat, perform, hold_priority, max_seconds)

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
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> str:
        """Activate one ability of a permanent you control. Find the ability's
        number in your state view's `abilities` list for that card (indexing
        starts at 0; intrinsic mana abilities come after printed ones).
        `targets` and `mana_sources` are comma-separated ids as with casting.
        `color` is a single letter (W, U, B, R, G or C) and is required when
        the ability offers a choice of color to add - an "any color" source
        such as Birds of Paradise or Mox Diamond, or a dual/shockland; it is
        ignored by abilities that do not need one.
        Mana abilities resolve immediately; others go on the stack.

        Releases priority and blocks for your next moment to act unless
        `hold_priority` is true (a dry run never blocks) - see `cast`'s
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
        sources = _parse_ids(mana_sources) or seat.my_untapped_sources(state)
        if sources:
            body["mana_sources"] = sources
        if color:
            body["color"] = color
        if dry_run:
            return seat.call("POST", f"/api/games/{seat.game_id()}/actions/activate", body)

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/activate", body)

        return act(seat, perform, hold_priority, max_seconds)

    @server.tool()
    @_guarded(seats)
    def declare_attackers(
        player_uid: str,
        attackers: str,
        hold_priority: bool = False,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> str:
        """Declare attackers during your declare attackers step. `attackers`
        is a JSON list: [{"card": "<attacker id>", "defender": "<player
        name or planeswalker id>"}]. Declare all attackers at once. Releases
        priority and blocks for your next moment to act unless
        `hold_priority` is true - see `cast`'s docstring for the shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body = {
            "player": seat.player,
            "attackers": [
                {"card_id": pair["card"], "defender": pair["defender"]}
                for pair in _parse_pairs(attackers)
            ],
        }

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/attack", body)

        return act(seat, perform, hold_priority, max_seconds)

    @server.tool()
    @_guarded(seats)
    def declare_blockers(
        player_uid: str,
        blocks: str,
        hold_priority: bool = False,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> str:
        """Declare blockers during the declare blockers step. `blocks` is a
        JSON list: [{"blocker": "<your creature id>", "blocked": "<attacker
        id>"}]. Declare all blocks at once. Releases priority and blocks for
        your next moment to act unless `hold_priority` is true - see
        `cast`'s docstring for the shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body = {
            "player": seat.player,
            "blocks": [
                {"blocker": pair["blocker"], "blocked": pair["blocked"]}
                for pair in _parse_pairs(blocks)
            ],
        }

        def perform() -> Any:
            return seat.call_json(
                "POST", f"/api/games/{seat.game_id()}/actions/declare-blockers", body
            )

        return act(seat, perform, hold_priority, max_seconds)

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
        choice: str = "",
        hold_priority: bool = False,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> str:
        """Answer your pending decision (CR 608.2d) - `my_state`'s
        `pending_decision` field says what is being asked, by `kind`.

        - `choose_cards` (e.g. the cleanup discard): `card_ids` is a
          comma-separated list of the card id(s) to discard, exactly as many
          as `pending_decision.amount` names, chosen from
          `pending_decision.cards`. A hand-sourced `choose_cards` (the
          cleanup discard, a "discard N" effect) is already resolvable
          against your own `hand` in `my_state`; a library-sourced one (a
          "reveal the top N, choose among them" effect) instead resolves
          through `pending_decision.card_details` - see the `scry`/`surveil`
          bullet below, which explains that field.
        - `choose_targets` (a triggered ability's targets, CR 603.3d):
          `targets` is a comma-separated list of up to
          `pending_decision.target_maximum` entries, each a card or ability
          id or a player's name, chosen from `pending_decision.targets` -
          leave it empty to give the ability no target at all, which is
          always legal here (this wave does not yet refuse an empty answer to
          a trigger that requires one).
        - `may`, `pay_life`, and `pay_energy` (e.g. a shockland's "pay 2
          life", CR 119.4, or a fixed "pay {E}{E}", CR 107.14): `accept` is
          `True` to say yes, `False` to say no.
          `pending_decision.affirmative_is_legal` is `False` when yes is not
          a legal answer at all (too little life or energy to pay) -
          answering `True` there is refused.
        - `pay_energy_amount` (e.g. "pay any amount of {E}", CR 107.14,
          CR 118.3): `energy_amount` is an integer from 0 up to
          `pending_decision.amount` (the energy you actually have). Not the
          same question as `pay_energy`'s `accept` - this kind asks how much,
          not whether. 0 always declines and is always legal.
        - `choose_a_quality` (e.g. Utopia Sprawl's "as this enters, choose a
          color", CR 614.12a): `choice` is the chosen color, card name, or
          creature type. `pending_decision.options` lists the legal colors
          when this is a color choice (the five letter codes); empty
          `options` means a card name or creature type, and any non-empty
          string is accepted - `Game.ask` is what actually enforces
          legality, so a bad card name or creature type still reaches the
          engine rather than being second-guessed here.
        - `scry` and `surveil` (CR 701.22a, CR 701.25a): `moved` is a
          comma-separated list of the `pending_decision.cards` entries to
          move off the top - to the bottom of the library for a scry, to the
          graveyard for a surveil. Every card not named stays on top, in
          `top_order` (comma-separated ids, topmost first) if given, or
          unchanged otherwise. Leaving `moved` empty is a real answer: keep
          everything on top. `pending_decision.cards` is still bare ids
          (`moved`/`top_order` answer by id), but `pending_decision.card_details`
          - present only for the deciding player - carries the full card view
          (name, oracle text, etc.) for exactly those ids, so a `scry`/`surveil`
          (a *look*, CR 701.20e: shown only to the deciding player) or a
          library-sourced `choose_cards` is never answered blind. Look the id
          up in `card_details` before choosing.

        `may`, `pay_life`, `scry`, and `surveil` were not answerable through
        this tool before this wave: the request simply parked until its
        timeout and the engine's documented default was taken instead,
        silently discarding the choice (see `decisions.py`'s module docstring
        on the API side for the CR 608.2d fallback rule).

        `mulligan`, `declare_attackers`, and `declare_blockers` decisions
        never reach this tool no matter what it supports - they are asked
        straight off the engine's own decision script during game setup and
        the dedicated `attackers`/`blockers` actions, never through this
        mailbox. Answering when nothing is pending, or when the pending
        decision is not yours, is refused rather than accepted.

        Answering a decision does not, itself, involve priority (CR 608.2d is
        the resolving effect asking a question, not a priority action), but
        follows the same shape as every other tool here for one reason:
        answering it is very often what was blocking your own next turn at
        priority, so waking straight back into whatever comes next saves the
        extra round trip. Releases priority and blocks unless `hold_priority`
        is true."""
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
        if choice:
            body["choice"] = choice

        def perform() -> Any:
            answer = seat.call_json("POST", f"/api/games/{seat.game_id()}/decide", body)
            if isinstance(answer, dict) and answer.get("accepted") is False:
                return {"allowed": False, "reason": answer.get("reason"), **answer}
            return {"allowed": True, **answer} if isinstance(answer, dict) else answer

        return act(seat, perform, hold_priority, max_seconds)

    @server.tool()
    @_guarded(seats)
    def pass_priority(
        player_uid: str,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> str:
        """Pass priority - "I am done for now". If everyone passes with
        spells on the stack, the top of the stack resolves. If everyone
        passes with the stack empty, the step ends and the game rolls to the
        next step on its own (the outcome says "end_step") - you do NOT call
        advance() for that.

        Blocks until you next have something to do, bounded by
        `max_seconds` - there is no `hold_priority` here (passing already is
        giving priority up); a refusal (it was not your turn to decide)
        returns immediately without blocking."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body = {"player": seat.player}

        def perform() -> Any:
            return seat.call_json(
                "POST", f"/api/games/{seat.game_id()}/actions/pass-priority", body
            )

        return act(seat, perform, hold_priority=False, max_seconds=max_seconds, release=False)

    @server.tool()
    @_guarded(seats)
    def resolve_top(
        player_uid: str,
        hold_priority: bool = False,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> str:
        """Resolve the top object on the stack. Normally the engine does this
        for you when everyone passes; use this only if a pass left the stack
        unresolved. Releases priority and blocks for your next moment to act
        unless `hold_priority` is true - see `cast`'s docstring for the
        shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body = {"player": seat.player}

        def perform() -> Any:
            answer = seat.call_json("POST", f"/api/games/{seat.game_id()}/resolve-top", body)
            return {"allowed": answer.get("reason") is None, **answer}

        return act(seat, perform, hold_priority, max_seconds)

    @server.tool()
    @_guarded(seats)
    def advance(
        player_uid: str,
        hold_priority: bool = False,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ) -> str:
        """Move to the next step or phase. Passing priority already rolls the
        game through every step where players get priority; this is only for
        the steps nobody gets priority in - untap and cleanup on your own
        turn. Releases priority and blocks for your next moment to act unless
        `hold_priority` is true - see `cast`'s docstring for the shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body = {"player": seat.player}

        def perform() -> Any:
            return {
                "allowed": True,
                **seat.call_json("POST", f"/api/games/{seat.game_id()}/advance", body),
            }

        return act(seat, perform, hold_priority, max_seconds)

    @server.tool()
    @_guarded(seats)
    def set_step(player_uid: str, step: str) -> str:
        """Jump the turn marker to a named step ("precombat_main",
        "beginning_of_combat", "declare_attackers", "declare_blockers",
        "combat_damage", "end_step", ...). Forward moves are real step
        changes; use advance() for ordinary flow. Board setup, not a priority
        action - does not block."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body = {"step": step, "player": seat.player}
        return seat.call("PUT", f"/api/games/{seat.game_id()}/step", body)

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
