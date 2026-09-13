"""Decklist parsing and game setup from Arena/MTGO "txt" decklist files.

Two halves:

- `parse_decklist` reads the plain-text decklist format into (count, name)
  pairs, one list for the mainboard and one for the sideboard. It is pure
  text handling: no card is fetched, no rule is applied.
- `load_deck_into_game` turns parsed lists into a running game. It routes
  through the engine's `Match.start_game` rather than building CardStates
  here: the match already performs deck registration with the construction
  rules (CR 100.2a, 100.4a), sets the sideboard aside (CR 103.2a), shuffles
  each library from a seed (CR 103.3), sets life to 20 (CR 103.4), deals the
  opening hands of seven (CR 103.5), and decides the starting player
  (CR 103.1) - and doing any of that by hand in the API layer would be rules
  logic out of place. What it stops short of is CR 103.5's mulligan
  declarations and CR 103.6's opening-hand actions: the engine leaves a dealt
  game *resting* in those two steps, and each seat declares for itself
  afterwards through `opening.py`'s endpoints. Seat order is the
  "mutually agreeable method" the rule
  allows, so the first `--deck` given to `serve` plays first; the match's
  `random_first_player` stays off.

Every CR number in this module was verified against the Comprehensive Rules
with `knowledge_search` in the session that wrote it.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence

from magic_cards_model.models import Card, Deck, Match, Player

from . import scryfall

#: "4x Lightning Bolt" - the MTGO-flavoured count. The x is tolerated with or
#: without a space after the count.
_COUNT_WITH_X = re.compile(r"^(\d+)x\s*(.+)$", re.IGNORECASE)

#: "4 Lightning Bolt" - the Arena-flavoured count.
_COUNT = re.compile(r"^(\d+)\s+(.+)$")

#: A set code in brackets and an optional trailing collector number, as in
#: "Lightning Bolt [2X2] 129". Neither Arena nor MTGO exports this shape, so
#: it is tolerated rather than expected: the bracket and the number are
#: stripped and the name underneath is kept. Anything with a second bracket
#: group or an unrecognised tail is garbage.
_SET_TAG = re.compile(r"^(.*?)\s*\[[^\]]+\](?:\s+\S+)?\s*$")

#: A bare section header: "Deck" and "Sideboard", with an optional colon.
_HEADER = re.compile(r"^(deck|sideboard):?$", re.IGNORECASE)

#: "SB: 4 Sulfuric Vortex" - the other way a sideboard entry is marked.
_SB_PREFIX = re.compile(r"^sb:\s*(.+)$", re.IGNORECASE)


def parse_decklist(text: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Read an Arena/MTGO "txt" decklist into (mainboard, sideboard) pairs.

    Each list holds `(count, card name)` pairs in first-seen file order, with
    a name's duplicate lines summed into one pair. Accepted lines:

    - "4 Lightning Bolt" and "4x Lightning Bolt"
    - "SB: 1 Mountain" for a sideboard entry
    - bare "Deck" and "Sideboard" header lines (colon optional) to switch
      which board following lines belong to; before any header, lines are
      mainboard
    - a blank line, which also closes the mainboard - the boundary mtg-decks'
      own `scripts/decklist.py` documents ("the mainboard runs until the
      first blank line or a `Sideboard` header, whichever comes first"),
      needed for an MTGO export that separates the two with a blank line and
      no header at all (B8-05: dimir-frog's real decklist.txt is exactly
      this shape). A blank line before any real entry is just leading
      whitespace, not a boundary; once the mainboard has been closed, later
      blank lines are spacing, never a third section, matching mtg-decks
    - comments starting with # or //
    - a set bracket and collector number after a name - "4 Lightning Bolt
      [2X2] 129" - stripped away, though Arena and MTGO never export one

    Anything else raises `ValueError` with the offending line quoted.
    """
    mainboard: dict[str, int] = {}
    sideboard: dict[str, int] = {}
    board = mainboard
    seen_content = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            if not line and seen_content:
                board = sideboard
            continue
        header = _HEADER.match(line)
        if header is not None:
            board = sideboard if header.group(1).lower() == "sideboard" else mainboard
            seen_content = True
            continue
        prefixed = _SB_PREFIX.match(line)
        if prefixed is not None:
            count, name = _count_and_name(prefixed.group(1).strip())
            _add_entry(sideboard, name, line, count=count)
            seen_content = True
            continue
        count, name = _count_and_name(line)
        _add_entry(board, name, line, count=count)
        seen_content = True

    return (
        [(count, name) for name, count in mainboard.items()],
        [(count, name) for name, count in sideboard.items()],
    )


def _add_entry(board: dict[str, int], name: str, line: str, *, count: int = 1) -> None:
    if count < 1:
        raise ValueError(f"decklist line {line!r}: a count must be at least 1")
    if not name:
        raise ValueError(f"unrecognised decklist line: {line!r}")
    board[name] = board.get(name, 0) + count


def _count_and_name(line: str) -> tuple[int, str]:
    """The (count, name) of one decklist line, with a set tag stripped off."""
    match = _COUNT_WITH_X.match(line) or _COUNT.match(line)
    if match is None:
        raise ValueError(f"unrecognised decklist line: {line!r}")
    count = int(match.group(1))
    name = match.group(2).strip()
    if "[" in name:
        tag = _SET_TAG.match(name)
        if tag is None or not tag.group(1).strip():
            raise ValueError(f"unrecognised decklist line: {line!r}")
        name = tag.group(1).strip()
    return count, name


def _resolve_card(name: str) -> Card:
    """The `CardResolver` handed to the match: Scryfall, cached, engine-valid.

    The same fetch-then-validate path the card-add endpoint takes
    (`scryfall.fetch_card`, then the engine's `Card` model), so a decklist
    name resolves to exactly the printing the rest of the API would use.
    """
    return Card.model_validate(scryfall.fetch_card(name))


def load_deck_into_game(
    decklists: Sequence[tuple[str, str]],
    *,
    seed: int | str | None = None,
    check_legality: bool = True,
) -> Match:
    """Parse decklists and start the game they describe (CR 103).

    `decklists` is one (player name, decklist text) pair per player, in seat
    order. Every name is resolved to a real Scryfall printing through
    `scryfall.fetch_card`; the engine's `Match.start_game` then does the
    whole game-start sequence:

    - the deck is registered and, unless `check_legality` is False, checked
      against the construction rules (CR 100.2a's sixty-card minimum and
      four-copy limit, CR 100.4a's fifteen-card sideboard). The engine owns
      the check; an illegal list raises `DeckIllegal` here, before any card
      is dealt. `check_legality=False` is the engine's own escape hatch for
      a deliberately illegal pile - the tests of this module need it, because
      no legal deck can be built from the four cards in the fixture cache.
    - the sideboard is set aside before the deck is shuffled (CR 103.2a),
      outside the game (CR 400.11a).
    - each library is shuffled from a seed derived from `seed` (CR 103.3),
      so the same seed and the same lists replay the same game.
    - life totals are set to 20 (CR 103.4) and opening hands of seven are
      dealt (CR 103.5). Mulligans are *not* settled here, and nothing
      settles them behind this call either: `Match.start_game` leaves the
      game resting in `Step.MULLIGAN` (turn 0), and each seat declares for
      itself once it holds its uid - see `opening.py` for why running the
      sequence here, or on a thread behind it, blocked the whole game.
    - the first player is seat order (CR 103.1's mutually agreeable method),
      so the first pair in `decklists` takes the first turn.

    Returns the `Match`, whose current game is `match.games[-1]`; `serve`
    hands that game to the session store. A `seed` of None draws a random
    one and records it on the match, so an unseeded boot is still replayable
    by passing the printed seed back.
    """
    usernames = [name for name, _ in decklists]
    if len(usernames) < 2:
        raise ValueError("a game needs at least two players")
    if len(set(usernames)) != len(usernames):
        raise ValueError("player names must be unique")

    match = Match(
        players=[Player(username=name) for name in usernames],
        card_resolver=_resolve_card,
        seed=secrets.randbits(31) if seed is None else seed,
    )
    for name, text in decklists:
        mainboard, sideboard = parse_decklist(text)
        deck = Deck(
            name=name,
            mainboard={card_name: count for count, card_name in mainboard},
            sideboard={card_name: count for count, card_name in sideboard},
        )
        match.register_deck(name, deck, check_legality=check_legality)
    match.start_game()
    return match


__all__ = ["load_deck_into_game", "parse_decklist"]
