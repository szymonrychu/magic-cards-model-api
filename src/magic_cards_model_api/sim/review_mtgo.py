"""A seat-blind per-turn timeline of an MTGO game log, for reviewing a game (T-156).

Pure code over `parse_mtgo_log`: no worker, no sandbox, no card data. The timeline is the
log in its own order, grouped by turn and seen from one player's seat, so nothing the log
shows later is pulled forward. What a log line says was public at that moment (casts, lands,
reveals, discards, mills...); draws are counts in the log already. What the reviewed player
could not see is taken out:

- an unmapped line that is not the reviewed player's keeps a card name only when that card
  is already public at that point of the timeline (named by an earlier mapped line); any
  other name becomes "a card";
- export header lines (the PDBot "[League]" line names both decks) keep no text.

Per turn the answer also lists the opponent's hand cards known at the end of that turn: the
last full-hand reveal, plus their cards bounced to hand, minus what they played, cast or
discarded since.

`upto_turn` cuts the timeline after that turn (no winner is given), so a reviewer can judge a
decision with the information public up to then.
"""

from __future__ import annotations

import re
from typing import Any

from magic_cards_model_api.sim.mtgo import LATER_GAME, Event, Unmapped, parse_mtgo_log
from magic_cards_model_api.sim.mtgo_convert import (
    line_text,
    plain_line,
    require_player,
    require_two_players,
)

MAX_EVENT_TEXT = 200  # chars of one timeline entry
NOTES = (
    "Turns count across both players (1, 2, 3...). Each entry starts with its log line number."
    " Opponent cards are named only from the line that made them public; draws and other"
    " hidden moves are counts. MTGO logs no life totals, damage or deaths: life is not"
    " derivable from the log, so judge life from the damage the cards dealt or ask the user."
    " 'unmapped' lists lines the parser could not read (check them yourself)."
)
_PREGAME_SKIP = frozenset({"roll", "join"})
_MARKED_CARD = re.compile(r"@\[([^@]+)@:[^@]*:@\]|\[([^\[\]]+)\]")
_HEADER = "export header"
_SKIP_REASONS = ("continuation of line",)


def _short(text: str) -> str:
    """Drop reminder text in parentheses (it is long and adds nothing), keep "(X is N)"."""
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "(" and (depth or not text.startswith("(X is", i)):
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
        i += 1
    return _cap(re.sub(r"\s+([.,])", r"\1", re.sub(r"\s{2,}", " ", "".join(out))).strip())


def _cap(text: str) -> str:
    return text if len(text) <= MAX_EVENT_TEXT else text[: MAX_EVENT_TEXT - 3] + "..."


def _winner(players: list[str], events: list[Event]) -> str | None:
    for e in events:
        if e.kind == "win":
            return e.player
    for e in events:
        if e.kind in ("lose", "concede") and e.player in players:
            return next((p for p in players if p != e.player), None)
    return None


class _KnownHand:
    """The opponent's hand cards the reviewed player knows, by name."""

    def __init__(self, opponent: str) -> None:
        self.opponent = opponent
        self.cards: list[str] = []
        self.owner: dict[str, str] = {}  # card name -> who last put it onto the battlefield

    def see(self, e: Event) -> None:
        if e.kind in ("land", "cast", "put_onto_battlefield") and e.card and e.player:
            self.owner[e.card] = e.player
        if e.player == self.opponent:
            if e.kind == "reveal" and e.card is not None:  # "their hand to C, containing ..."
                self.cards = list(e.targets)
            elif e.card in self.cards and (
                e.kind in ("land", "discard")
                or (e.kind == "cast" and "from the graveyard" not in e.raw)
            ):
                self.cards.remove(e.card)
        if e.kind == "bounce":
            self.cards += [c for c in e.targets if self.owner.get(c) == self.opponent]


def _public_names(e: Event) -> set[str]:
    return {n for n in (e.card, *e.targets) if n}


def _redact_cards(raw: str, public: set[str], logged: set[str]) -> str:
    """The line as plain text; a card name that is not public becomes "a card".

    Names are found by their markup ("[Name]", "@[Name@:..:@]") and, for bare-text logs, by
    the names the log's mapped lines use anywhere (`logged`).
    """

    def hide(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return name if name in public else "a card"

    text = plain_line(_MARKED_CARD.sub(hide, raw))
    for name in sorted(logged - public, key=len, reverse=True):
        text = re.sub(rf"(?<![\w]){re.escape(name)}(?![\w])", "a card", text)
    return text


def _unmapped_entry(u: Unmapped, you: str, public: set[str], logged: set[str]) -> dict[str, Any]:
    text = plain_line(u.text)
    if u.reason == _HEADER:
        text = "export header, hidden"
    elif not text.startswith(you + " "):
        text = _redact_cards(u.text, public, logged)
    return {"line": u.line_no, "text": _cap(text), "reason": u.reason}


def review_log(text: str, player: str, upto_turn: int | None = None) -> dict[str, Any]:
    """The timeline of the first game in `text` from `player`'s seat.

    `upto_turn` keeps the turns up to and including that one (game-wide turn index) and no
    winner. Raises ValueError for a log without exactly 2 players, without turns, an unknown
    `player` (the message names the log's players) or an `upto_turn` below 1.
    """
    if upto_turn is not None and upto_turn < 1:
        raise ValueError("upto_turn must be at least 1")
    players, events, unmapped = parse_mtgo_log(text)
    require_two_players(players)
    require_player(players, player)
    opponent = next(p for p in players if p != player)
    if not any(e.kind == "turn" for e in events):
        raise ValueError("the log has no turn header (Turn N: <player>), so there is no turn")

    entries: list[tuple[int, Event | Unmapped]] = [(e.line_no, e) for e in events]
    entries += [(u.line_no, u) for u in unmapped]
    entries.sort(key=lambda pair: pair[0])

    pregame: list[str] = []
    timeline: list[dict[str, Any]] = []
    known = _KnownHand(opponent)
    public: set[str] = set()  # card names a mapped line has named so far
    logged = set().union(*(_public_names(e) for e in events)) - set(players)
    later = 0
    current: dict[str, Any] | None = None

    def close_turn() -> None:
        if current is not None and known.cards:
            current["opp_known_hand"] = sorted(known.cards)

    for line_no, item in entries:
        if isinstance(item, Unmapped):
            if item.reason == LATER_GAME:
                later += 1
                continue
            if item.reason.startswith(_SKIP_REASONS):
                continue
            entry = _unmapped_entry(item, player, public, logged)
            if current is None:
                pregame.append(f"{line_no}: {entry['text']} [unmapped: {entry['reason']}]")
            else:
                current.setdefault("unmapped", []).append(entry)
            continue
        if item.kind == "turn":
            close_turn()
            if upto_turn is not None and item.turn is not None and item.turn > upto_turn:
                break
            current = {"turn": item.turn, "active": item.player, "events": []}
            timeline.append(current)
            continue
        known.see(item)
        public |= _public_names(item)
        line = f"{line_no}: {_short(line_text(item))}"
        if current is None:
            if item.kind not in _PREGAME_SKIP:
                pregame.append(line)
        else:
            current["events"].append(line)
    close_turn()

    answer: dict[str, Any] = {
        "players": players,
        "you": player,
        "opponent": opponent,
        "winner": None if upto_turn is not None else _winner(players, events),
        "turns": len(timeline),
        **({"upto_turn": upto_turn} if upto_turn is not None else {}),
        "notes": NOTES,
        "pregame": pregame,
        "timeline": timeline,
    }
    if later:
        answer["later_game_lines"] = later
    return answer
