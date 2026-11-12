"""Pure parser for MTGO game logs (T-123 S6).

Input is the text of one MTGO game log in any of the forms the Task 15 notes
(tests/fixtures/mtgo/README.txt) describe: the decoded Match_GameLog .dat form
("@P" records with "@[Name@:ids:@]" card markup), the rendered form with
"[HH:MM:SS] " prefixes and "[Card]" brackets, bare text, or the raw .dat text
itself. No I/O, no engine imports.

Accounting contract: every non-empty line of the input becomes exactly one
`Event` or one `Unmapped`. Blank lines are skipped and never counted.
Line numbers are 1-based physical lines; for raw .dat text there are no
physical lines, so a line number is the 1-based index of the non-empty record.

Multi-line entries: a physical line that starts with a space and follows
another line is a continuation of the entry above (a modal ability lists its
modes on extra lines and puts its targets on the last one). The head line and
its continuations are folded into one text for matching, so the head still
finds its targets. The head becomes the single Event (its `raw` holds all the
folded physical lines joined with "\\n") and each continuation line is
reported as its own Unmapped with reason "continuation of line N", so every
physical line is still accounted for exactly once.

Only the first game of a log is parsed. Once a join or roll line appears after
a turn header, that line and everything after it is Unmapped (reason
LATER_GAME). `Event.turn` is the game-wide turn index (every turn header of
the game counts, across both players), not MTGO's per-player number, which
stays visible in `raw`.

MTGO logs no damage, life totals or deaths, so there are no such kinds; lines
that look like them stay Unmapped.

Event field use by kind (fields not listed are None / empty):
  cast, activate, trigger   player, card, targets (from the text outside
                            reminder parentheses and quoted modes); cast also
                            amount = X when the line says "(X is N)"
  attack                    player = the defending player, card = the
                            defended planeswalker or None, targets = the
                            attacking cards (the attacker is the active
                            player of the last turn event)
  block                     card = blocker, targets = [attacker]; player is
                            None because the line names no player
  token                     player, card = source, targets = [token name],
                            amount
  draw                      player, amount, card = the effect source if any
  exile, bounce             player, card = source, targets = moved cards
  mill, to_graveyard        player, targets = the cards
  counter_spell             player, card = source, targets = [countered]
  damage_prevented          card = source, targets = [target], amount
  put_onto_battlefield      player, card, targets = [attacked player] if any
  reveal                    player, card = the revealing card (if the line has
                            "to <card>, containing"), targets = revealed cards
  counters_added/removed    player, card = the object, amount (counter type is
                            in `raw` only)
Without markup or brackets a list of cards is split on commas and "and", so a
card name that holds a comma is only kept whole in the marked-up forms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

NO_MAPPING = "no mapping for this line"
LATER_GAME = "later game in the same log; import one game at a time"

KINDS = (
    "roll", "join", "play_first", "mulligan", "keep", "turn", "skip_draw", "draw", "land",
    "cast", "activate", "trigger", "attack", "block", "damage_prevented", "discard",
    "put_onto_battlefield", "token", "sacrifice", "to_graveyard", "mill", "exile", "bounce",
    "counter_spell", "fizzle", "counters_added", "counters_removed", "reveal", "concede",
    "lose", "win",
)  # fmt: skip


@dataclass
class Event:
    line_no: int
    kind: str
    player: str | None
    card: str | None
    targets: list[str] = field(default_factory=list)
    amount: int | None = None
    turn: int | None = None
    raw: str = ""


@dataclass
class Unmapped:
    line_no: int
    text: str
    reason: str


_CARD_MARKUP = re.compile(r"@\[(.+?)@:.*?:@\]")
_BRACKET = re.compile(r"\[([^\[\]]+)\]")
_TIMESTAMP = re.compile(r"^\[\d{1,2}:\d{2}:\d{2}\] ?")
_RAW_DAT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\xff]")
_QUOTED = re.compile(r'"[^"]*"')
_TARGETING = re.compile(r" targeting (.+?)\.?\s*$")
_X_IS = re.compile(r"\(X is (\d+)\)")

_NUMBER_WORDS = {
    w: n
    for n, w in enumerate(
        ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight"]
        + ["nine", "ten", "eleven", "twelve"]
    )
}
_NUMBER_WORDS.update({"a": 1, "an": 1})

# Lines that are never game actions; checked in this order before the kind table.
_NON_GAME = (
    (re.compile(r"^\[CHAT\] "), "chat line"),
    (
        re.compile(
            r"^(?:== Game \d+|\[Time\]|\[League\]|Game \d+ Completed\.$|PennyDreadful$|PD League$"
            r"|Winner: |Match Winner: )"
        ),
        "export header",
    ),
    (re.compile(r" has (?:started|stopped) watching\.$"), "observer line"),
    (re.compile(r" has (?:lost connection to the game|left the game)\.$"), "connection line"),
    (
        re.compile(r"^(?:Illegal attack\.$|Chosen mode: |Couldn't put Triggered Ability)"),
        "game-system line",
    ),
    (re.compile(r"Multiplayer Commander"), "multiplayer game, not supported"),
)
_LATER_HEADER = re.compile(r"^== Game ([2-9]|\d\d+)\b")

_PLAYER_SOURCES = (
    re.compile(r"^(?P<n>.+?) rolled a \d+\.$"),
    re.compile(r"^(?P<n>.+?) joined the game\.$"),
    re.compile(r"^(?P<n>.+?) chooses to (?:not )?play first\.$"),
)
_TURN_HEADER = re.compile(r"^Turn \d+: (?P<n>.+)$")

# Where a cast line's card name ends: the first optional suffix, or the end of the line.
_CAST_END = (
    r"(?= using an alternate cost| for its \w+ cost| from the graveyard| with Flashback"
    r"| without paying| with kicker| with Bestow| choosing | targeting | \("
    r"|\.(?: \(X is \d+\)\.)?$|$)"
)


def strip_markup(line: str) -> str:
    """Remove "@P", turn "@[Name@:...:@]" into "Name", drop "@i", drop non-printable bytes."""
    text = _CARD_MARKUP.sub(r"\1", line).replace("@P", "").replace("@i", "")
    return "".join(ch for ch in text if " " <= ch <= "~")


def _plain(line: str) -> str:
    """Matching form: no timestamp, no markup, no brackets around cards."""
    return _BRACKET.sub(r"\1", strip_markup(_TIMESTAMP.sub("", line.strip())))


def _headerish(line: str) -> str:
    """Like _plain but keeps brackets, so "[CHAT]" and "[League]" stay recognisable."""
    return strip_markup(_TIMESTAMP.sub("", line.strip()))


def _spans(line: str) -> list[str]:
    """Card names marked up in the original line (@[..@:..:@] and [..])."""
    body = _TIMESTAMP.sub("", line)
    return _CARD_MARKUP.findall(body) + _BRACKET.findall(_CARD_MARKUP.sub("", body))


def _number(word: str | None) -> int | None:
    if word is None:
        return None
    if word.isdigit():
        return int(word)
    return _NUMBER_WORDS.get(word.lower())


def _drop_parens(text: str) -> str:
    """Remove balanced (...) groups and "quoted" modes: reminder text never holds targets."""
    out: list[str] = []
    depth = 0
    for ch in _QUOTED.sub("", text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


@lru_cache(maxsize=64)
def _table(players: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    names = sorted(players, key=len, reverse=True)
    alt = "|".join(re.escape(n) for n in names) or "(?!)"
    p = rf"(?P<player>{alt})"
    rows = [
        ("roll", rf"^{p} rolled a (?P<amount>\d+)\.$"),
        ("join", rf"^{p} joined the game\.$"),
        ("play_first", rf"^{p} chooses to (?:not )?play first\.$"),
        ("mulligan", rf"^{p} mulligans to (?P<amount>\w+) cards?\.$"),
        (
            "keep",
            rf"^{p} (?:puts (?:a|\w+) cards? on the bottom of their library and )?"
            r"begins the game with (?P<amount>\w+) cards? in hand\.$",
        ),
        ("turn", rf"^Turn (?P<turn>\d+): {p}$"),
        ("skip_draw", rf"^{p} skips their draw step\.$"),
        (
            "draw",
            rf"^{p} draws (?P<amount>a|\w+) cards?"
            r"(?: with (?P<card>.+?)(?: token)?(?:'s ability)?)?\.$",
        ),
        ("land", rf"^{p} plays (?P<card>.+)\.$"),
        ("cast", rf"^{p} casts (?P<card>.+?){_CAST_END}"),
        (
            "activate",
            rf"^{p} activates an ability of (?P<card>.+?)(?: token)?"
            r"(?: \(.*|\s+targeting .*|\.)?$",
        ),
        ("trigger", rf"^{p} puts (?:a )?triggered ability from (?P<card>.+?) onto the stack.*$"),
        (
            "attack",
            rf"^{p}(?: ?'s (?P<card>.+?))? is being attacked by (?P<attackers>.+?)\.?$",
        ),
        ("block", r"^(?P<card>.+?)(?: token)? blocks (?P<attacker>.+?)(?: token)?\.$"),
        (
            "damage_prevented",
            r"^(?P<amount>\d+) damage caused by (?P<card>.+?) to (?P<target>.+?) is prevented\.$",
        ),
        ("discard", rf"^{p} discards (?P<card>.+?)\.$"),
        (
            "put_onto_battlefield",
            rf"^{p} puts (?P<card>.+?) onto the battlefield(?: attacking (?P<target>.+?))?\.?$",
        ),
        (
            "token",
            rf"^{p} ?'s (?P<card>.+?) creates (?P<amount>a|an|\w+) (?P<token>.+?) "
            r"[Tt]okens?(?: under .+? control)?\.$",
        ),
        ("sacrifice", rf"^{p} sacrifices (?P<card>.+?)(?: token)?\.$"),
        ("to_graveyard", rf"^{p} puts (?P<cards>.+?) into their graveyard\.$"),
        ("mill", rf"^{p} mills (?P<cards>.+?)\.$"),
        (
            "exile",
            rf"^{p} exiles (?P<cards>.+?) with (?:with )?(?P<card>.+?)(?:'s ability)?\.$",
        ),
        (
            "bounce",
            rf"^{p} returns (?P<cards>.+?) to (?:its|their) owner's hand with "
            r"(?P<card>.+?)(?:'s ability)?\.$",
        ),
        ("counter_spell", rf"^{p} counters (?P<target>.+?) with (?P<card>.+?)\.$"),
        (
            "fizzle",
            rf"^{p} ?'s (?P<card>.+?) is removed from the stack because it has no legal targets\.$",
        ),
        (
            "counters_added",
            rf"^{p} puts (?P<amount>a|an|\w+) (?P<ctype>.+?) counters? on (?P<card>.+?)\.$",
        ),
        (
            "counters_removed",
            rf"^{p} removes (?P<amount>a|an|\w+) (?P<ctype>.+?) counters? from (?P<card>.+?)\.$",
        ),
        ("reveal", rf"^{p} reveals (?P<rest>.*)\.$"),
        ("concede", rf"^{p} has conceded from the game\.$"),
        (
            "lose",
            rf"^{p} (?:loses the game|has lost the game(?: due to disconnection)?"
            r"|loses because of drawing a card)\.$",
        ),
        ("win", rf"^{p} wins the game\.$"),
    ]
    assert tuple(k for k, _ in rows) == KINDS
    return tuple((k, re.compile(rx)) for k, rx in rows)


def _split_bare(text: str) -> list[str]:
    parts = re.split(r",\s*and |,\s*| and ", text)
    return [x.strip() for x in parts if x.strip()]


def _items(text: str | None, known: list[str]) -> list[str]:
    """Names in a list phrase: known card/player names in order, else a comma/and split."""
    if not text:
        return []
    if known:
        alt = "|".join(re.escape(n) for n in sorted(set(known), key=len, reverse=True))
        found = re.findall(alt, text)
        if found:
            return found
    return _split_bare(text.rstrip("."))


def _discover_players(plain_lines: list[str], dat_mode: bool) -> list[str]:
    players: list[str] = []
    for text in plain_lines:
        for rx in _PLAYER_SOURCES:
            m = rx.match(text)
            if m and m["n"] not in players:
                players.append(m["n"])
    for text in plain_lines:
        m = _TURN_HEADER.match(text)
        if not m:
            continue
        name = m["n"]
        if dat_mode:
            # framing noise can follow the name: cut after a known player name
            name = next(
                (p for p in sorted(players, key=len, reverse=True) if name.startswith(p)), name
            )
        if name not in players:
            players.append(name)
    return players


def _records(text: str) -> tuple[list[tuple[int, str]], bool]:
    """(line_no, line) for every non-empty line; raw .dat text is split on the @P framing."""
    if _RAW_DAT.search(text):
        out: list[tuple[int, str]] = []
        pieces = text.replace("\r", "").replace("\n", "").split("@P")
        for i, piece in enumerate(pieces):
            clean = "".join(ch for ch in piece if " " <= ch <= "~")
            if clean.strip():
                out.append((len(out) + 1, clean if i == 0 else "@P" + clean))
        return out, True
    return [(i, ln.rstrip()) for i, ln in enumerate(text.splitlines(), 1) if ln.strip()], False


def parse_mtgo_log(text: str) -> tuple[list[str], list[Event], list[Unmapped]]:
    """Parse one MTGO game log into (players, events, unmapped)."""
    lines, dat_mode = _records(text)

    # fold continuation lines (leading space) into the entry above
    entries: list[tuple[int, list[tuple[int, str]]]] = []
    for no, ln in lines:
        if ln.startswith(" ") and entries:
            entries[-1][1].append((no, ln))
        else:
            entries.append((no, [(no, ln)]))

    players = _discover_players(
        [_plain(" ".join(s.strip() for _, s in group)) for _, group in entries], dat_mode
    )
    table = _table(tuple(players))
    events: list[Event] = []
    unmapped: list[Unmapped] = []
    turn = 0
    later = False

    for head_no, group in entries:
        head_line = group[0][1]
        joined = " ".join(s.strip() for _, s in group)
        folded = _plain(joined)
        event, reason = None, NO_MAPPING
        if dat_mode and head_no == 1 and not head_line.startswith("@P"):
            reason = "dat header"
        elif later:
            reason = LATER_GAME
        elif _LATER_HEADER.match(_headerish(joined)):
            later, reason = True, LATER_GAME
        else:
            for rx, why in _NON_GAME:
                if rx.search(_headerish(joined)):
                    reason = why
                    break
            else:
                event = _classify(head_no, group, folded, table, players, turn)
                if event and event.kind in ("join", "roll") and turn > 0:
                    later, event, reason = True, None, LATER_GAME
                elif event and event.kind == "turn":
                    turn += 1
        if event:
            events.append(event)
        else:
            unmapped.append(Unmapped(head_no, head_line, reason))
        cont_reason = LATER_GAME if later else f"continuation of line {head_no}"
        for no, ln in group[1:]:
            unmapped.append(Unmapped(no, ln, cont_reason))
    return players, events, unmapped


def _classify(
    line_no: int,
    group: list[tuple[int, str]],
    folded: str,
    table: tuple[tuple[str, re.Pattern[str]], ...],
    players: list[str],
    turn: int,
) -> Event | None:
    hit = next(((k, m) for k, rx in table if (m := rx.match(folded))), None)
    if hit is None:
        return None
    kind, m = hit
    g = m.groupdict()
    known = [n for _, ln in group for n in _spans(ln)] + players
    targets: list[str] = []
    card = g.get("card")
    if kind in ("cast", "activate", "trigger"):
        t = _TARGETING.search(_drop_parens(folded))
        targets = _items(t[1], known) if t else []
    elif kind == "attack":
        targets = _items(g["attackers"], known)
    elif kind == "block":
        targets = [g["attacker"]]
    elif kind == "token":
        targets = [g["token"]]
    elif kind in ("damage_prevented", "counter_spell"):
        targets = [g["target"]]
    elif kind == "put_onto_battlefield":
        targets = [g["target"]] if g["target"] else []
    elif kind in ("exile", "bounce", "mill", "to_graveyard"):
        targets = _items(g["cards"], known)
    elif kind == "reveal":
        card, targets = _reveal(g["rest"], known)
    amount = _number(g.get("amount"))
    if kind == "cast":
        x = _X_IS.search(folded)
        amount = int(x[1]) if x else None
    raw = "\n".join(ln for _, ln in group)
    return Event(
        line_no,
        kind,
        g.get("player"),
        card,
        targets,
        amount,
        (turn + 1 if kind == "turn" else turn) or None,
        raw,
    )


def _reveal(rest: str, known: list[str]) -> tuple[str | None, list[str]]:
    """ "their hand to C, containing X, Y" -> (C, [X, Y]); "C" or "C with D" -> (None, [..])."""
    m = re.match(r"their hand to (?P<card>.+?), containing (?P<cards>.+)$", rest)
    if m:
        return m["card"], _items(m["cards"], known)
    return None, _items(rest, known)
