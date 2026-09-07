"""The bootable entry point: one or more games from decklist files, then uvicorn.

    uv run python -m magic_cards_model_api.serve --deck alice=decks/a.txt \
        --deck bob=decks/b.txt [--seed 7] [--port 8000]

boots exactly one game (this form is unchanged - existing invocations keep
working). To boot several concurrent games in the same server - several
matchups playable at once - repeat `--match` instead, once per matchup:

    uv run python -m magic_cards_model_api.serve \
        --match alice=decks/a.txt,bob=decks/b.txt,seed=1 \
        --match carol=decks/c.txt,dave=decks/d.txt,seed=2 \
        --match eve=decks/e.txt,frank=decks/f.txt,seed=3 \
        --port 8000

Each `--match` is one comma-separated `PLAYER=PATH` pair plus an optional
`seed=N`; `--deck`/`--seed` and `--match` are mutually exclusive ways of
describing the same thing - one game's worth of decks and a seed - so every
matchup, single or several, is built by the same code underneath
(`_boot_from_spec`). Everything else the store and the endpoints do was
already per-`game_id` before this - the store keys games, locks, paused
commands, and seat uids by id (`store.py`), and every HTTP route already takes a
`game_id` path segment - so serving several games is a change to this module
only, not to the store or the API.

Each game is created before uvicorn starts serving, through the same engine
path the endpoints use - `decks.load_deck_into_game`, which hands the
decklists to the engine's `Match.start_game` (CR 103: sideboard set aside,
seeded shuffles, life totals, opening hands of seven, first player by seat
order) - and then registered in the session store directly. No HTTP
self-calls: by the time the server accepts a request, `/api/games` already
lists every live game.

Nothing settles CR 103.5 or CR 103.6 at boot. `Match.start_game` leaves each
game resting in `Step.MULLIGAN` (turn 0, no phase), and every seat declares for
itself once uvicorn is accepting requests, through
`POST /api/games/{id}/pregame/mulligan` and its two CR 103.6 siblings. The
summary block below therefore reports each hand *as dealt*, and says which step
the game is resting in. See `opening.py` for why running the sequence here - or
on a thread behind this boot - blocked the whole game for the full decision
timeout per question per seat.

Each game's shuffle seed comes from its own `seed=`/`--seed`; without one, a
random seed is drawn and printed, so even an unseeded boot is replayable by
passing the printed seed back.
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
            "  opening hands (as dealt; CR 103.5 is the seats' own to declare): "
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


@dataclass
class MatchSpec:
    """One game's worth of boot instructions: two (player, path) pairs, in
    seat order, and an optional seed. What either `--deck`/`--seed` (one
    game) or one `--match` (one of several) boils down to, so `main`/
    `setup_all` need only ever handle a `list[MatchSpec]`."""

    decks: list[tuple[str, str]]
    seed: int | None


def _parse_deck_entry(entry: str, parser: argparse.ArgumentParser, flag: str) -> tuple[str, str]:
    name, sep, path = entry.partition("=")
    if not sep or not name.strip() or not path.strip():
        parser.error(f"{flag} must be PLAYER=PATH, got {entry!r}")
    return name.strip(), path.strip()


def _parse_match_entry(entry: str, parser: argparse.ArgumentParser) -> MatchSpec:
    """One `--match` value: comma-separated `PLAYER=PATH` tokens plus an
    optional `seed=N` token, in any order. Exactly two decks are required -
    the same "two players, seat order" shape `--deck` enforces for the
    single-game form."""
    decks: list[tuple[str, str]] = []
    seed: int | None = None
    for token in entry.split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith("seed="):
            raw_seed = token.removeprefix("seed=")
            try:
                seed = int(raw_seed)
            except ValueError:
                parser.error(f"--match: bad seed={raw_seed!r} in {entry!r}")
            continue
        decks.append(_parse_deck_entry(token, parser, "--match"))
    if len(decks) != 2:
        parser.error(f"--match needs exactly two PLAYER=PATH decks, got {len(decks)} in {entry!r}")
    return MatchSpec(decks=decks, seed=seed)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The CLI.

    Two mutually exclusive ways to describe the game(s) to boot:

    - `--deck PLAYER=PATH` given exactly twice, plus an optional `--seed` -
      one game, unchanged from before this module supported more than one.
    - `--match PLAYER=PATH,PLAYER=PATH[,seed=N]`, repeatable - one game per
      `--match`, so three matchups is three `--match` arguments.

    `args.matches` (built here, not by argparse itself) is the
    `list[MatchSpec]` every caller downstream actually wants, regardless of
    which form was used - see `setup_all`.
    """
    parser = argparse.ArgumentParser(
        prog="python -m magic_cards_model_api.serve",
        description="Boot one or more games from decklist files and serve the API.",
    )
    parser.add_argument(
        "--deck",
        action="append",
        metavar="PLAYER=PATH",
        help=(
            "a player and their decklist file; give exactly two, in seat order - "
            "the first one takes the first turn. Boots a single game; "
            "mutually exclusive with --match"
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="seed for --deck's game (CR 103.3); default is a random one, printed below",
    )
    parser.add_argument(
        "--match",
        action="append",
        metavar="PLAYER=PATH,PLAYER=PATH[,seed=N]",
        help=(
            "one matchup's two decks and optional seed, comma-separated; give it "
            "multiple times to boot that many concurrent games. Mutually exclusive "
            "with --deck/--seed"
        ),
    )
    parser.add_argument("--port", type=int, default=8000, help="port for uvicorn")
    args = parser.parse_args(argv)

    if args.deck and args.match:
        parser.error("give either --deck/--seed (one game) or --match (one or more), not both")
    if not args.deck and not args.match:
        parser.error("give --deck twice for one game, or --match at least once")

    if args.match:
        if args.seed is not None:
            parser.error("--seed applies to --deck's game; give seed=N inside each --match instead")
        args.matches = [_parse_match_entry(entry, parser) for entry in args.match]
    else:
        if len(args.deck) != 2:
            parser.error(f"exactly two --deck arguments are required, got {len(args.deck)}")
        decks = [_parse_deck_entry(entry, parser, "--deck") for entry in args.deck]
        args.matches = [MatchSpec(decks=decks, seed=args.seed)]
    return args


def _boot_from_spec(spec: MatchSpec, *, check_legality: bool) -> Boot:
    """Create one game from a `MatchSpec` and hand it to the store.

    This is the whole boot of one game, minus serving: read its decklist
    files, build and start it through the engine (`load_deck_into_game` -
    registration, legality, the CR 103.2a sideboard, the seeded CR 103.3
    shuffles, the CR 103.5 hands), register it in the session store the
    endpoints read (keyed by its own fresh `game_id`, same as every other
    game already in the store), and record its boot events. Shared by
    `setup` (the single-game `--deck` form) and `setup_all` (`--match`, one
    or several) so both go through exactly one boot path.
    """
    specs: list[tuple[str, str, str]] = []
    for name, path in spec.decks:
        try:
            text = Path(path).read_text()
        except OSError as exc:
            raise SystemExit(f"serve: cannot read decklist for {name}: {exc}") from None
        specs.append((name, path, text))

    match = load_deck_into_game(
        [(name, text) for name, _, text in specs],
        seed=spec.seed,
        check_legality=check_legality,
    )
    game = match.games[-1]
    game_id = store.create(game)
    uids = new_seat_uids(game)
    store.register_seats(game_id, uids)
    store.register_decklists(game_id, {name: text for name, _, text in specs})
    # Nothing runs CR 103.5 or CR 103.6 here: `Match.start_game` has already
    # left the game resting in `Step.MULLIGAN` (turn 0), and each seat declares
    # for itself through `POST /api/games/{id}/pregame/mulligan` once uvicorn is
    # accepting requests. See `opening.py`.
    boot = Boot(
        game_id=game_id,
        match=match,
        game=game,
        deck_paths={name: path for name, path, _ in specs},
        seat_uids={name: uid for uid, name in uids.items()},
    )
    _record_boot_events(boot)
    return boot


def setup(args: argparse.Namespace, *, check_legality: bool = True) -> Boot:
    """Boot the single game described by `args` (the `--deck` form) and hand
    it to the store. `check_legality` defaults to the engine's own
    construction-rules check (CR 100.2a, 100.4a); the tests of this module
    turn it off because no legal deck can be built from the four cards in the
    fixture cache.

    Kept as a one-game function, unchanged in shape, for existing callers
    (this module's own tests boot exactly one game this way); `setup_all`
    is the multi-game counterpart used when `--match` was given."""
    (spec,) = args.matches
    return _boot_from_spec(spec, check_legality=check_legality)


def setup_all(args: argparse.Namespace, *, check_legality: bool = True) -> list[Boot]:
    """Boot every game `args.matches` describes - one game for the `--deck`
    form, one per `--match` otherwise - each into its own entry in the
    session store, and return every `Boot` in the order given on the command
    line."""
    return [_boot_from_spec(spec, check_legality=check_legality) for spec in args.matches]


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
    """Parse, boot every game `args.matches` describes, print one block per
    game (numbered when there is more than one, so an operator reading
    `/tmp/game-server.log` can never mix up two games' seat uids), then serve
    all of them from one uvicorn process until interrupted."""
    args = parse_args(argv)
    boots = setup_all(args)
    if len(boots) == 1:
        print(boots[0].summary(), flush=True)
    else:
        print(f"booted {len(boots)} games:\n", flush=True)
        for index, boot in enumerate(boots, start=1):
            print(f"--- game {index}/{len(boots)} ---", flush=True)
            print(boot.summary(), flush=True)
            print(flush=True)
    uvicorn.run(app, port=args.port)


if __name__ == "__main__":
    main()
