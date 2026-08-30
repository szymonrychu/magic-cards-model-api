"""The bootable entry point: a full game from two decklist files, then uvicorn.

    uv run python -m magic_cards_model_api.serve --deck alice=decks/a.txt \
        --deck bob=decks/b.txt [--seed 7] [--port 8000]

The game is created before uvicorn starts serving, through the same engine
path the endpoints use - `decks.load_deck_into_game`, which hands the
decklists to the engine's `Match.start_game` (CR 103: sideboard set aside,
seeded shuffles, life totals, opening hands of seven, first player by seat
order) - and then registered in the session store directly. No HTTP
self-calls: by the time the server accepts a request, `/api/games` already
lists a live game.

The engine's shuffle seed comes from `--seed`; without one, a random seed is
drawn and printed, so even an unseeded boot is replayable by passing the
printed seed back.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from magic_cards_model.models import Game, Match

from .decks import load_deck_into_game
from .main import app
from .store import new_seat_uids, store


@dataclass
class Boot:
    """What one boot produced, everything the summary block reports."""

    game_id: str
    match: Match
    game: Game
    deck_paths: dict[str, str]
    seat_uids: dict[str, str]
    """username -> seat uid: hand one to whoever is dispatched as that player."""

    def summary(self) -> str:
        """The one console block: players, deck sizes, seed, hands, first
        player, and the seat uids the dispatching operator hands out."""
        match = self.match
        game = self.game
        lines = [
            f"game {self.game_id} ready: " + " vs ".join(player.username for player in game.players)
        ]
        for player in game.players:
            deck = match.decks[player.username]
            cards = deck.mainboard_size + deck.sideboard_size
            lines.append(
                f"  {player.username}: {cards} cards ({deck.mainboard_size} mainboard, "
                f"{deck.sideboard_size} sideboard) from {self.deck_paths[player.username]}"
            )
        lines.append(f"  seed: {match.seed}")
        lines.append(
            "  opening hands: "
            + ", ".join(f"{player.username} {len(player.hand)}" for player in game.players)
        )
        assert game.turn is not None
        lines.append(
            f"  first player: {game.turn.active_player} "
            f"(turn {game.turn.turn_number}, {game.turn.step.value} step)"
        )
        lines.append("  seat uids (one per player, given out at dispatch):")
        for username, uid in self.seat_uids.items():
            lines.append(f"    {username}: {uid}")
        return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The CLI: exactly two `--deck PLAYER=PATH` arguments, optional seed and port."""
    parser = argparse.ArgumentParser(
        prog="python -m magic_cards_model_api.serve",
        description="Boot a two-player game from decklist files and serve the API.",
    )
    parser.add_argument(
        "--deck",
        action="append",
        required=True,
        metavar="PLAYER=PATH",
        help=(
            "a player and their decklist file; give exactly two, in seat order - "
            "the first one takes the first turn"
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="seed for the shuffles (CR 103.3); default is a random one, printed below",
    )
    parser.add_argument("--port", type=int, default=8000, help="port for uvicorn")
    args = parser.parse_args(argv)
    if len(args.deck) != 2:
        parser.error(f"exactly two --deck arguments are required, got {len(args.deck)}")
    for entry in args.deck:
        name, sep, path = entry.partition("=")
        if not sep or not name.strip() or not path.strip():
            parser.error(f"--deck must be PLAYER=PATH, got {entry!r}")
    return args


def setup(args: argparse.Namespace, *, check_legality: bool = True) -> Boot:
    """Create the game from the --deck arguments and hand it to the store.

    This is the whole boot, minus serving: read the decklist files, build and
    start the game through the engine (`load_deck_into_game` - registration,
    legality, the CR 103.2a sideboard, the seeded CR 103.3 shuffles, the CR
    103.5 hands), register it in the session store the endpoints read, and
    record the boot events. `check_legality` defaults to the engine's own
    construction-rules check (CR 100.2a, 100.4a); the tests of this module
    turn it off because no legal deck can be built from the four cards in the
    fixture cache.
    """
    specs: list[tuple[str, str, str]] = []
    for entry in args.deck:
        name, _, path = entry.partition("=")
        path = path.strip()
        try:
            text = Path(path).read_text()
        except OSError as exc:
            raise SystemExit(f"serve: cannot read decklist for {name}: {exc}") from None
        specs.append((name.strip(), path, text))

    match = load_deck_into_game(
        [(name, text) for name, _, text in specs],
        seed=args.seed,
        check_legality=check_legality,
    )
    game = match.games[-1]
    game_id = store.create(game)
    uids = new_seat_uids(game)
    store.register_seats(game_id, uids)
    boot = Boot(
        game_id=game_id,
        match=match,
        game=game,
        deck_paths={name: path for name, path, _ in specs},
        seat_uids={name: uid for uid, name in uids.items()},
    )
    _record_boot_events(boot)
    return boot


def _record(game_id: str, action: str, **detail: object) -> None:
    """One boot event, into the audit log when that module exists.

    The audit module is another piece of this app and may not be present; the
    import is lazy and a missing module falls back to plain print, so the
    server never fails to boot over its log.
    """
    try:
        from . import audit

        audit.record(game_id, action, **detail)
    except ImportError:
        print(f"boot: {action} {detail}")


def _record_boot_events(boot: Boot) -> None:
    """The boot events of the game start, as the engine performed them.

    `load_deck_into_game` runs registration, the shuffles, and the opening
    hands inside one `Match.start_game` call, so these are recorded once that
    call has returned, each describing what it did.
    """
    decks = ", ".join(
        f"{name}={boot.match.decks[name].mainboard_size}+{boot.match.decks[name].sideboard_size}"
        for name in boot.match.usernames
    )
    _record(boot.game_id, "decks_loaded", decks=decks)
    _record(boot.game_id, "shuffled", seed=str(boot.match.seed))
    hands = ", ".join(f"{player.username}={len(player.hand)}" for player in boot.game.players)
    _record(boot.game_id, "hands_drawn", hands=hands)


def main(argv: Sequence[str] | None = None) -> None:
    """Parse, boot, print the summary block, then serve until interrupted."""
    args = parse_args(argv)
    boot = setup(args)
    print(boot.summary(), flush=True)
    uvicorn.run(app, port=args.port)


if __name__ == "__main__":
    main()
