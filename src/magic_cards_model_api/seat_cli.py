"""One command per game action, from one player's seat, for shell-driven play.

The same seat `mcp.py` exposes, sharing the same "one call, one meaningful
action" flow (`seatkit.run_action`): the same hidden-information filter - the
opponent's hand and both libraries never appear - and the same
cast/play-land/activate/attackers/blockers/resolve/advance/decide/pass
behaviour: perform the action, release priority unless `--hold` is given,
then block until this seat next has something to do, printing the outcome
and the state it woke into together. Meant for subagents that play through
Bash rather than through an MCP client:

    uv run --directory <api repo> python -m magic_cards_model_api.seat_cli \\
        --uid <seat-uid> cast "Lightning Bolt" --target bob

Every command prints the API's answer (or the filtered state) as JSON text.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .seatkit import (
    _UUID,
    DEFAULT_API,
    DEFAULT_MAX_SECONDS,
    Seats,
    _parse_ids,
    block_until_wake,
    run_action,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", required=True, help="your seat uid, given at dispatch")
    parser.add_argument("--game", default=None, help="game id; default: the live game")
    parser.add_argument("--api", default=DEFAULT_API, help=f"API base url; default {DEFAULT_API}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("state", help="the game as you may see it, right now")

    wait = sub.add_parser("wait", help="block until it is your moment to act")
    wait.add_argument(
        "--max-seconds",
        type=float,
        default=570.0,
        help="per-call bound; call again to keep waiting",
    )

    def blocking(command: Any) -> None:
        """Every action shares these two: hold priority instead of blocking,
        and the bound on how long a block may last."""
        command.add_argument(
            "--hold",
            action="store_true",
            help=(
                "keep priority instead of releasing it (CR 117.3c: you already "
                "keep it after acting) - never blocks"
            ),
        )
        command.add_argument(
            "--max-seconds",
            type=float,
            default=DEFAULT_MAX_SECONDS,
            help="bound on the block that follows the action, unless --hold",
        )

    cast = sub.add_parser("cast", help="cast a spell from your hand")
    cast.add_argument("card", help="card name or id")
    cast.add_argument("--target", default="", help="comma-separated card ids or player names")
    cast.add_argument(
        "--sources",
        default="",
        help="comma-separated mana source ids (default: all your untapped sources)",
    )
    cast.add_argument("--modes", default="", help="comma-separated mode numbers")
    cast.add_argument("--x", type=int, default=None, help="the chosen value of X")
    cast.add_argument(
        "--dry-run", action="store_true", help="ask 'could I?' without changing anything"
    )
    cast.add_argument(
        "--keyword",
        default="",
        help=(
            'the keyword this cast is announced under (CR 601.2b) - "Plot" or '
            '"Foretell" for a card exiled with suspend/plot cast --from-zone exile '
            "(a suspended card's free cast is picked up automatically once its "
            "last time counter is off, no --keyword needed)"
        ),
    )
    blocking(cast)

    def special_action(name: str, help_text: str) -> Any:
        """suspend/plot/foretell share this shape: name or id of a hand card,
        which mana sources pay the special action's own cost (default: every
        untapped source), and dry-run."""
        parser = sub.add_parser(name, help=help_text)
        parser.add_argument("card", help="card name or id, in your hand")
        parser.add_argument(
            "--sources",
            default="",
            help="comma-separated mana source ids (default: all your untapped sources)",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="ask 'could I?' without changing anything"
        )
        blocking(parser)
        return parser

    special_action(
        "suspend",
        "CR 116.2f/702.62a: exile a card with suspend from your hand, with N time counters",
    )
    special_action(
        "plot", "CR 116.2k/702.170b: exile a card with plot from your hand, for its plot cost"
    )
    special_action(
        "foretell", "CR 116.2h/702.143b: pay {2} and exile a card with foretell face down"
    )

    cycle = sub.add_parser("cycle", help="CR 702.29a: discard a card with cycling to draw")
    cycle.add_argument("card", help="card name or id, in your hand")
    cycle.add_argument(
        "--index", type=int, default=0, help="which cycling ability, if the card has more than one"
    )
    cycle.add_argument(
        "--sources",
        default="",
        help="comma-separated mana source ids (default: all your untapped sources)",
    )
    cycle.add_argument(
        "--dry-run", action="store_true", help="ask 'could I?' without changing anything"
    )
    blocking(cycle)

    land = sub.add_parser("play-land", help="play a land from your hand")
    land.add_argument("card", help="card name or id")
    blocking(land)

    activate = sub.add_parser("activate", help="activate a permanent's ability")
    activate.add_argument("card_id", help="the permanent's id")
    activate.add_argument(
        "index", type=int, help="ability index, from the state view's abilities list"
    )
    activate.add_argument("--target", default="", help="comma-separated ids")
    activate.add_argument("--sources", default="", help="comma-separated mana source ids")
    activate.add_argument(
        "--color",
        default="",
        help="color letter (W/U/B/R/G/C) for a mana ability that offers a choice",
    )
    activate.add_argument("--dry-run", action="store_true")
    blocking(activate)

    attackers = sub.add_parser("attackers", help="declare attackers (JSON list)")
    attackers.add_argument("pairs", help='[{"card": "<id>", "defender": "<player>"}]')
    blocking(attackers)

    blockers = sub.add_parser("blockers", help="declare blockers (JSON list)")
    blockers.add_argument("pairs", help='[{"blocker": "<id>", "blocked": "<id>"}]')
    blocking(blockers)

    decide = sub.add_parser("decide", help="answer your pending decision (CR 608.2d)")
    decide.add_argument(
        "card_ids", nargs="?", default="", help="comma-separated card ids to discard/choose"
    )
    decide.add_argument(
        "--targets",
        default="",
        help=(
            "comma-separated target ids or player names, for a choose_targets "
            "decision - a triggered ability asking for its target (CR 603.3d), "
            "or a targeted ability being activated with no targets supplied "
            "(CR 602.2b/601.2c)"
        ),
    )
    decide.add_argument(
        "--accept",
        choices=("yes", "no"),
        default=None,
        help=(
            "yes/no, for a may, pay_life, pay_energy, pay_mana, or "
            "opening_hand_action decision (CR 608.2d, CR 119.4, CR 107.14, "
            "CR 118.12a, CR 103.6)"
        ),
    )
    decide.add_argument(
        "--moved",
        default="",
        help=(
            "comma-separated card ids to move off the top, for a scry (to the "
            "bottom, CR 701.22a) or surveil (to the graveyard, CR 701.25a) decision"
        ),
    )
    decide.add_argument(
        "--top-order",
        default="",
        help="comma-separated card ids: the order the rest go back on top, topmost first",
    )
    decide.add_argument(
        "--energy-amount",
        type=int,
        default=None,
        help="integer amount of {E} to pay, for a pay_energy_amount decision (CR 107.14, CR 118.3)",
    )
    decide.add_argument(
        "--life-amount",
        type=int,
        default=None,
        help="integer amount of life to pay, for a pay_life_amount decision (CR 119.4)",
    )
    decide.add_argument(
        "--choice",
        default="",
        help=(
            "the chosen color/card name/creature type, for a choose_a_quality decision "
            "(CR 614.12a), or the chosen mode's printed text, for a choose_a_mode decision "
            "(CR 603.3c, CR 700.2a)"
        ),
    )
    decide.add_argument(
        "--top",
        choices=("top", "bottom"),
        default=None,
        help=(
            "top or bottom of the library, for a put_on_top_or_bottom decision "
            "(Subtlety) - asked of the targeted spell's owner, not its caster"
        ),
    )
    blocking(decide)

    passer = sub.add_parser("pass", help="pass priority - 'I am done for now'")
    passer.add_argument(
        "--max-seconds",
        type=float,
        default=DEFAULT_MAX_SECONDS,
        help="bound on the block that follows",
    )

    resolve = sub.add_parser(
        "resolve", help="resolve the top of the stack (only if a pass left it unresolved)"
    )
    blocking(resolve)

    advance = sub.add_parser("advance", help="move through untap/cleanup on your own turn")
    blocking(advance)

    step = sub.add_parser("set-step", help="jump the turn marker to a named step")
    step.add_argument("step")

    log = sub.add_parser("log", help="the game's action log")
    log.add_argument("limit", nargs="?", type=int, default=40)

    report = sub.add_parser("report", help="file a problem report into the game log")
    report.add_argument(
        "text",
        nargs="?",
        default=None,
        help="the report; omit it to read the report from stdin instead",
    )

    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    seats = Seats(args.api, args.game)
    seat = seats.seat_for(args.uid)
    if seat is None:
        print(
            "that uid is no seat in this game. You cannot act, and you may see "
            "nothing - a seat uid is given to you when you are dispatched."
        )
        return 1
    gid = seat.game_id()

    def sources_or_all(value: str) -> list[str] | None:
        if value:
            return _parse_ids(value)
        return seat.my_untapped_sources(seat.fetch_state())

    def act(perform: Any, dry_run: bool = False) -> None:
        """Every non-dry-run action shares this: perform it, then run the
        shared release-and-wait flow, printing the combined answer."""
        if dry_run:
            print(json.dumps(perform(), indent=1, sort_keys=True))
            return
        result = run_action(seat, perform, hold_priority=args.hold, max_seconds=args.max_seconds)
        print(json.dumps(result, indent=1, sort_keys=True))

    if args.command == "state":
        print(json.dumps(seat.fetch_state(), indent=1, sort_keys=True))
    elif args.command == "wait":
        woken = block_until_wake(seat, before=None, max_seconds=args.max_seconds)
        woken["state"]["wake_reason"] = woken["wake_reason"]
        print(json.dumps(woken["state"], indent=1, sort_keys=True))
    elif args.command == "cast":
        body: dict[str, Any] = {"player": seat.player, "dry_run": args.dry_run}
        if _UUID.match(args.card.strip()):
            body["card_id"] = args.card.strip()
        else:
            body["card"] = args.card
        if args.target:
            body["targets"] = _parse_ids(args.target)
        sources = sources_or_all(args.sources)
        if sources:
            body["mana_sources"] = sources
        if args.modes:
            body["modes"] = [int(mode) for mode in _parse_ids(args.modes)]
        if args.x is not None:
            body["x_value"] = args.x
        if args.keyword:
            body["keyword"] = args.keyword
        act(
            lambda: seat.call_json("POST", f"/api/games/{gid}/actions/cast", body),
            dry_run=args.dry_run,
        )
    elif args.command in ("suspend", "plot", "foretell"):
        body = {"player": seat.player, "dry_run": args.dry_run}
        if _UUID.match(args.card.strip()):
            body["card_id"] = args.card.strip()
        else:
            body["card"] = args.card
        sources = sources_or_all(args.sources)
        if sources:
            body["mana_sources"] = sources
        act(
            lambda: seat.call_json("POST", f"/api/games/{gid}/actions/{args.command}", body),
            dry_run=args.dry_run,
        )
    elif args.command == "cycle":
        body = {"player": seat.player, "ability_index": args.index, "dry_run": args.dry_run}
        if _UUID.match(args.card.strip()):
            body["card_id"] = args.card.strip()
        else:
            body["card"] = args.card
        sources = sources_or_all(args.sources)
        if sources:
            body["mana_sources"] = sources
        act(
            lambda: seat.call_json("POST", f"/api/games/{gid}/actions/cycle", body),
            dry_run=args.dry_run,
        )
    elif args.command == "play-land":
        body = {"player": seat.player}
        if _UUID.match(args.card.strip()):
            body["card_id"] = args.card.strip()
        else:
            body["card"] = args.card
        act(lambda: seat.call_json("POST", f"/api/games/{gid}/actions/play-land", body))
    elif args.command == "activate":
        body = {
            "player": seat.player,
            "card_id": args.card_id,
            "ability_index": args.index,
            "dry_run": args.dry_run,
        }
        if args.target:
            body["targets"] = _parse_ids(args.target)
        sources = sources_or_all(args.sources)
        if sources:
            body["mana_sources"] = sources
        if args.color:
            body["color"] = args.color
        act(
            lambda: seat.call_json("POST", f"/api/games/{gid}/actions/activate", body),
            dry_run=args.dry_run,
        )
    elif args.command == "attackers":
        body = {
            "player": seat.player,
            "attackers": [
                {"card_id": pair["card"], "defender": pair["defender"]}
                for pair in json.loads(args.pairs)
            ],
        }
        act(lambda: seat.call_json("POST", f"/api/games/{gid}/actions/attack", body))
    elif args.command == "blockers":
        body = {
            "player": seat.player,
            "blocks": [
                {"blocker": pair["blocker"], "blocked": pair["blocked"]}
                for pair in json.loads(args.pairs)
            ],
        }
        act(lambda: seat.call_json("POST", f"/api/games/{gid}/actions/declare-blockers", body))
    elif args.command == "pass":
        result = run_action(
            seat,
            lambda: seat.call_json(
                "POST", f"/api/games/{gid}/actions/pass-priority", {"player": seat.player}
            ),
            hold_priority=False,
            max_seconds=args.max_seconds,
            release=False,
        )
        print(json.dumps(result, indent=1, sort_keys=True))
    elif args.command == "resolve":

        def perform_resolve() -> Any:
            answer = seat.call_json(
                "POST", f"/api/games/{gid}/resolve-top", {"player": seat.player}
            )
            return {"allowed": answer.get("reason") is None, **answer}

        act(perform_resolve)
    elif args.command == "advance":

        def perform_advance() -> Any:
            return {
                "allowed": True,
                **seat.call_json("POST", f"/api/games/{gid}/advance", {"player": seat.player}),
            }

        act(perform_advance)
    elif args.command == "set-step":
        print(
            seat.call("PUT", f"/api/games/{gid}/step", {"step": args.step, "player": seat.player})
        )
    elif args.command == "decide":

        def perform_decide() -> Any:
            body: dict[str, Any] = {
                "player": seat.player,
                "card_ids": _parse_ids(args.card_ids),
                "targets": _parse_ids(args.targets),
                "moved": _parse_ids(args.moved),
            }
            if args.accept is not None:
                body["accept"] = args.accept == "yes"
            if args.top_order:
                body["top_order"] = _parse_ids(args.top_order)
            if args.energy_amount is not None:
                body["energy_amount"] = args.energy_amount
            if args.life_amount is not None:
                body["life_amount"] = args.life_amount
            if args.choice:
                body["choice"] = args.choice
            if args.top is not None:
                body["top"] = args.top == "top"
            answer = seat.call_json("POST", f"/api/games/{gid}/decide", body)
            if isinstance(answer, dict) and answer.get("accepted") is False:
                return {"allowed": False, **answer}
            return {"allowed": True, **answer} if isinstance(answer, dict) else answer

        act(perform_decide)
    elif args.command == "log":
        # `uid` here, not the bare debugger's-eye default: a real seat reading
        # its own log must not see the opponent's hidden-zone card names any
        # more than the state endpoint shows them.
        answer = seat.http.get(f"/api/games/{gid}/log", params={"uid": seat.uid})
        entries = answer.json().get("log", [])[-args.limit :]
        print(json.dumps(entries, indent=1, sort_keys=True))
    elif args.command == "report":
        # A report is long prose full of quotes, braces and backticks. Passing
        # it as an argument means the shell sees it first, and command
        # substitution has already silently blanked parts of a real report -
        # so stdin is offered as the way to hand it over untouched.
        text = args.text if args.text is not None else sys.stdin.read()
        if not text.strip():
            print("refusing to file an empty report")
            return 1
        print(seat.call("POST", f"/api/games/{gid}/report", {"player": seat.player, "text": text}))
    return 0


if __name__ == "__main__":
    sys.exit(run())
