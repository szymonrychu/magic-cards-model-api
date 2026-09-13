"""Which of the guided decks is the agent piloting, and where are its guides.

The agent is handed a decklist by the game server, not a deck name. Turning
"60 card names" into "you are on dimir-frog, here is the gameplan" is this
module's whole job.

It does that from each guide's own `decks/<slug>/archetype.json`: a handful of
signature clauses, each naming the cards that define the archetype and how
many copies across them a real build runs. A clause is satisfied when the
MAINBOARD holds at least `min` copies across its named cards, and an
archetype claims a list when every one of its clauses is satisfied and none
of its `exclude` clauses is.

Signatures, not whole-list similarity. Similarity compared the list against
one sample 75 per archetype and had two defects that mattered in live games:

- A real list of the same deck by a different pilot swaps ten to fifteen flex
  slots, so it scored in the 0.5-0.7 band and fell on whichever side of a
  threshold those slots put it. The same 60 cards were identified differently
  depending on whether they arrived as a bundled list or as a paste.
- It scored the sideboard too, so fifteen cards nobody has cast yet could
  carry an archetype claim.

A signature answers the question the guide actually needs answered - "does
this list contain the thing this deck is built around" - and is indifferent
to the flex slots, because the flex slots are not in it.

Three outcomes, never two: `CONFIDENT` when exactly one archetype claims the
list, `LOW` when the evidence points somewhere but not conclusively, and
`NONE` when nothing reaches half its clauses. `LOW` is the one that had no
representation before, and it is the common case for a list a user pasted in
themselves.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from magic_cards_model_api.scryfall import name_faces

#: Exactly one archetype satisfies every one of its signature clauses, after
#: `exclude` clauses have knocked out the archetypes that disown the list.
CONFIDENT = "confident"
#: The evidence points somewhere, but not conclusively: two or more
#: archetypes claim the list and the tie-breaks cannot separate them, or
#: nothing claims it fully and something got at least halfway. The guide is
#: worth reading; it is not worth trusting over the board.
LOW = "low"
#: Nothing reached half its clauses. Today's no-guide behaviour: the agent
#: plays from its list.
NONE = "none"

#: The key a matchup with no guide behind it is filed under, in the tells
#: table, in the `vs-<name>.md` note the reflection turn writes, and in any
#: later study pass over those notes. One spelling, shared, so the three
#: agree without a lookup table. It is also what `Identification` resolves
#: to for a list nothing claimed, so the two halves of the briefing - "which
#: deck am I on" and "who is across the table" - spell absence the same way.
UNKNOWN_ARCHETYPE = "unknown"

_LINE = re.compile(r"^\s*(\d+)\s*x?\s+(.+?)\s*$")
_TRAILING_SET = re.compile(r"\s*\([^)]*\)\s*[\dA-Za-z-]*\s*$")
_SIDEBOARD_HEADER = re.compile(r"^\s*(sideboard|side board|sb:)\b", re.IGNORECASE)
_MAINBOARD_HEADER = re.compile(r"^\s*(maindeck|mainboard|main deck|deck)\b", re.IGNORECASE)
#: `analysis/<deck>-modern-<YYYY-MM-DD>` - the date is the run, and the name
#: in front of it is not.
_DATE_SUFFIX = re.compile(r"\d{4}-\d{2}-\d{2}\s*$")
_NON_SLUG = re.compile(r"[^a-z0-9]+")

#: A Modern sideboard is exactly fifteen cards, so a blank line with no more
#: than that behind it is the corpus's board separator rather than a
#: paragraph break in a grouped paste. Fifteen and not "about fifteen": a
#: list short of its sideboard is still short of fifteen.
_SIDEBOARD_MAX = 15
#: ... and so is a blank line with a complete maindeck in FRONT of it,
#: whatever follows. Sixty cards are a maindeck; nobody groups a paste in a
#: way that puts sixty of them before the first paragraph break.
_MAINBOARD_MIN = 60


def slugify(name: str) -> str:
    """The canonical archetype key: what a boarding plan calls `boros energy`
    is what `matchups/boros-energy.json` and `vs-boros-energy.md` are named
    after.

    The plan writes archetype names as prose ("ur cutter prowess"), the files
    beside it are slugs, and the agent has to write a note keyed the same way
    a later pass will look it up - so the conversion lives in one place.
    """
    return _NON_SLUG.sub("-", name.strip().lower()).strip("-") or UNKNOWN_ARCHETYPE


@dataclass(frozen=True)
class Decklist:
    """A parsed list, kept split the way the game keeps it split.

    Signatures are evaluated on `mainboard` alone: a sideboard card has not
    been cast and cannot make a claim about what the deck is.
    """

    mainboard: dict[str, int] = field(default_factory=dict)
    sideboard: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Cards in the whole 75."""
        return sum(self.mainboard.values()) + sum(self.sideboard.values())

    @property
    def names(self) -> tuple[str, ...]:
        """Every card name in the 75, mainboard first.

        Names, never counts: a folded name-to-count mapping is exactly what
        the classifier must not be handed, because a sideboard card has not
        been cast and cannot make a claim about what the deck is. A boarding
        swap is the opposite question - "do I own this card" - and every card
        a swap boards IN comes from the sideboard, so that one is asked of
        the whole 75. See `held_faces`.
        """
        return (*self.mainboard, *self.sideboard)


def _board_split(counts: list[int], boundaries: list[int]) -> int:
    """Which blank line separates the two boards, as an index into the rows -
    or one past the end when none of them does.

    A blank line is a board separator in the corpus and a paragraph break in
    a paste, and the two are told apart by what the split would PRODUCE
    rather than by where it falls. A separator leaves a legal sideboard
    behind it, or a complete maindeck in front of it; a paragraph break
    inside a grouped list leaves neither.

    The first boundary that qualifies wins, not the last: a list grouped on
    both sides of the split has boundaries inside its sideboard too
    (60/15, then 65/10, then 70/5), and every one of those leaves a legal
    fifteen-or-fewer behind it while putting sideboard cards in the maindeck.
    """
    running = 0
    totals = []
    for count in counts:
        running += count
        totals.append(running)
    for index in boundaries:
        before = totals[index - 1] if index else 0
        if running - before <= _SIDEBOARD_MAX or before >= _MAINBOARD_MIN:
            return index
    return len(counts)


def parse_decklist(text: str) -> Decklist:
    """Card name to count, mainboard and sideboard kept apart.

    The corpus writes the split two ways and both are handled: a `Sideboard`
    (or `SB:`) header, and - `decks/dimir-frog/decklist.txt` in the guides
    repo does this - a bare blank line between the two boards. An explicit
    header wins wherever the list carries one, so a blank line inside a
    maindeck is then only whitespace. A list with neither is all mainboard,
    which is the honest reading of a 60-card paste.

    A blank line only splits when the split it produces looks like one (see
    `_board_split`). It used to split on the FIRST blank line after any
    mainboard card and never switch back, which is right for the corpus's own
    files and wrong for the case this classifier exists to serve: a user
    pasting their own list, grouped by card type with no `Sideboard` header -
    the common case this module's docstring names. Three real corpus lists
    pasted that way kept 21, 31 and 27 of their 75 cards in the mainboard and
    filed the rest as a sideboard, and signatures read the mainboard alone,
    so all three dropped from `CONFIDENT` to `LOW`.
    """
    lines = [raw.strip() for raw in text.splitlines()]
    content = [line for line in lines if not line.startswith(("#", "//"))]
    header_split = any(_SIDEBOARD_HEADER.match(line) for line in content)

    #: One row per counted card line, in list order, plus which board an
    #: explicit header put it on. Collected before anything is split, because
    #: where a blank line belongs is a question about the whole list and
    #: cannot be answered at the blank line itself.
    rows: list[tuple[str, int, bool]] = []
    boundaries: list[int] = []
    side = False
    for line in lines:
        if line.startswith(("#", "//")):
            continue
        if not line:
            if not header_split and rows and (not boundaries or boundaries[-1] != len(rows)):
                boundaries.append(len(rows))
            continue
        if _SIDEBOARD_HEADER.match(line):
            side = True
            continue
        if _MAINBOARD_HEADER.match(line):
            side = False
            continue
        match = _LINE.match(line)
        if not match:
            continue
        name = _TRAILING_SET.sub("", match.group(2)).strip()
        if name:
            rows.append((name, int(match.group(1)), side))

    split = len(rows) if header_split else _board_split([count for _, count, _ in rows], boundaries)
    mainboard: dict[str, int] = {}
    sideboard: dict[str, int] = {}
    for index, (name, count, on_side) in enumerate(rows):
        board = sideboard if on_side or index >= split else mainboard
        board[name] = board.get(name, 0) + count
    return Decklist(mainboard=mainboard, sideboard=sideboard)


def held_faces(decklist: Decklist | Mapping[str, int]) -> frozenset[str]:
    """Every face name the list holds, casefolded - across the WHOLE 75.

    Both boards, deliberately. This answers "is the agent holding this
    card", which is not the question the classifier asks: a card a boarding
    plan tells the agent to board IN comes from the sideboard by definition,
    so intersecting a swap against the mainboard alone marks every board-in
    absent - the exact false positive the matchup card exists to avoid.

    A split or modal card has as many names as it has faces and the corpus
    uses all of them: the list the server deals writes "Wear/Tear"
    (`decks/boros_aggro.txt`) or "Wear // Tear" (`decks/ruby_storm.txt`),
    while a hand-written boarding plan writes the swap as "1 Wear". Compared
    as raw strings those are three different cards, and the card the seat is
    holding gets reported absent.

    A bare mapping is accepted too, for the one-board case a caller already
    has in hand (a test, or a single board on its own).
    """
    names: Iterable[str] = decklist.names if isinstance(decklist, Decklist) else decklist
    return frozenset(face for name in names for face in name_faces(name))


def holds(held: frozenset[str], name: str) -> bool:
    """Whether a list with these faces contains the card `name` names.

    Any face counts, which is looser than `scryfall._pool_names_match`'s
    "whole name or front face" - deliberately, because the two are wrong in
    opposite directions. There, a wrong hit serves the agent a card it did
    not ask for; here, a wrong miss tells the agent not to board a card it is
    holding. So a swap naming either half of a split the list holds is a hit.
    """
    return any(face in held for face in name_faces(name))


@dataclass(frozen=True)
class SignatureClause:
    """`min` copies across `cards`, or the clause is not satisfied."""

    cards: tuple[str, ...]
    minimum: int
    why: str = ""

    def satisfied_by(self, board: dict[str, int]) -> bool:
        return sum(board.get(name, 0) for name in self.cards) >= self.minimum


@dataclass(frozen=True)
class ArchetypeSignature:
    """One `archetype.json`.

    `name` is the archetype's own name for itself, which is NOT the directory
    slug - `prison-tron` calls itself "urzatron" and `orzhov-martyr` calls
    itself "orzhov blink". `parent` names the family a slug belongs to, and is
    what a list that matches only a family's shared clauses resolves to.
    """

    name: str
    signature: tuple[SignatureClause, ...]
    exclude: tuple[SignatureClause, ...] = ()
    parent: str | None = None


def _clause(row: object) -> SignatureClause | None:
    """`row` as a clause, or `None` when it is not a usable one.

    Every level is type-checked, not just the leaves: `cards` has to BE a
    list before its elements can be inspected, or a scalar there comes out of
    `json.loads` and straight into a `for` loop - which is a `TypeError`
    raised from inside a function documented never to raise.
    """
    if not isinstance(row, dict):
        return None
    cards = row.get("cards")
    if not isinstance(cards, list) or not cards:
        return None
    if not all(isinstance(name, str) and name for name in cards):
        return None
    minimum = row.get("min")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        return None
    why = row.get("why")
    return SignatureClause(tuple(cards), minimum, why if isinstance(why, str) else "")


def _clauses(rows: object) -> tuple[SignatureClause, ...] | None:
    """`rows` as clauses, or `None` when ANY row of it is unusable.

    All of them or none of them, deliberately - see `load_signature`. Dropping
    the unusable rows one at a time and keeping the rest is what made a
    half-broken file a CHEAPER file to match, because `Candidate.clauses`
    counts the survivors.
    """
    if not isinstance(rows, list):
        return None
    clauses = []
    for row in rows:
        clause = _clause(row)
        if clause is None:
            return None
        clauses.append(clause)
    return tuple(clauses)


def load_signature(path: Path) -> ArchetypeSignature | None:
    """`path` read as an `archetype.json`, or `None`.

    Never raises. The file lives in another repo that moves on its own
    schedule: a malformed or half-written one must cost that deck its
    signature - it degrades to unclassifiable, which is `NONE` - and must not
    take a live game down with it, nor hide the twelve guides that are fine.

    It is the WHOLE file that is lost, never a clause at a time, and that is
    the load-bearing half of the rule rather than a tidiness preference.
    Degradation must never make a match EASIER, and dropping one unusable row
    did exactly that in both directions:

    - a signature is satisfied "in full" when every clause it declares is
      answered, and `Candidate.clauses` counts what survived parsing - so a
      two-clause archetype with one clause broken became a one-clause
      archetype, and a list answering half of it was promoted from `LOW` to
      `CONFIDENT`;
    - an `exclude` clause is the file saying "a list holding these is playing
      the other deck, not me". Dropping the row deletes the disavowal, so a
      list the archetype had correctly disowned came back into contention and
      was handed the wrong guide, confidently.

    Both produce a confidently wrong answer, which is strictly worse than no
    answer: the agent plays someone else's gameplan instead of its own list.
    Keeping an unusable row as an unsatisfiable clause would be safe for
    `signature` and useless for `exclude` (an exclude nobody can satisfy is
    an exclude that is not there), so it would need opposite treatments for
    the two halves of one file - the asymmetry that let these drift apart in
    the first place. One rule: any malformation costs the deck its signature,
    and the other twelve guides answer as before.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    clauses = _clauses(raw.get("signature"))
    if not clauses:
        return None
    # `in`, not `get`: an absent `exclude` is a file that claims nothing, and
    # claiming nothing is well-formed. A `null` or a scalar under the key is
    # a half-written disavowal, and is as uninformative as a row with no
    # `min` - so it costs the file its signature like any other malformation.
    exclude = _clauses(raw["exclude"]) if "exclude" in raw else ()
    if exclude is None:
        return None
    name = raw.get("archetype")
    parent = raw.get("parent")
    return ArchetypeSignature(
        name=name if isinstance(name, str) and name else Path(path).parent.name,
        signature=clauses,
        exclude=exclude,
        parent=parent if isinstance(parent, str) and parent else None,
    )


@dataclass
class DeckGuide:
    slug: str
    root: Path
    decklist: Path
    gameplan: Path | None = None
    sideboard: Path | None = None
    meta_calls: Path | None = None
    matchups_dir: Path | None = None
    archetype: Path | None = None
    """`archetype.json` - the signature this deck is recognised by."""

    boarding_plan: Path | None = None
    """`boarding-plan.json`, beside the matchups directory. Located here so
    the guide is a complete description of what the corpus holds for this
    deck; reading it is the matchup card's job, not the classifier's."""

    @property
    def archetypes(self) -> list[str]:
        if self.matchups_dir is None:
            return []
        return sorted(p.stem for p in self.matchups_dir.glob("*.json"))

    def matchup_path(self, archetype: str) -> Path | None:
        if self.matchups_dir is None:
            return None
        candidate = self.matchups_dir / f"{archetype}.json"
        return candidate if candidate.is_file() else None

    def signature(self) -> ArchetypeSignature | None:
        """This deck's signature, or `None` when it has no usable one."""
        return load_signature(self.archetype) if self.archetype is not None else None

    def sibling_archetype_files(self) -> list[Path]:
        """Every `archetype.json` in the corpus, this deck's included.

        The tells table needs the signature cards of the archetype a row
        *names*, which is a different deck's file - so the lookup is over the
        whole `decks/` directory, not over this guide. It is also not over
        `GuideLibrary.decks()`: that skips a directory with no
        `decklist.txt`, and two thirds of the corpus is exactly that - an
        `archetype.json` written so the field can be recognised, with no
        guide behind it.
        """
        decks_dir = self.root.parent
        if not decks_dir.is_dir():
            return []
        return sorted(p for p in decks_dir.glob("*/archetype.json") if p.is_file())


@dataclass(frozen=True)
class Candidate:
    """One archetype's answer to one list."""

    guide: DeckGuide
    signature: ArchetypeSignature
    satisfied: tuple[SignatureClause, ...]
    """Which of the signature's clauses the mainboard satisfies."""

    @property
    def matched(self) -> int:
        return len(self.satisfied)

    @property
    def evidence(self) -> frozenset[tuple[str, ...]]:
        """The satisfied clauses identified by the cards they name.

        Two archetypes in the same family write their shared clauses with the
        same cards, so equal evidence means the list said nothing that tells
        them apart - which is what `GuideLibrary._family` is looking for.
        """
        return frozenset(clause.cards for clause in self.satisfied)

    @property
    def slug(self) -> str:
        return self.guide.slug

    @property
    def archetype(self) -> str:
        return self.signature.name

    @property
    def clauses(self) -> int:
        return len(self.signature.signature)

    @property
    def full(self) -> bool:
        return self.matched == self.clauses

    @property
    def plausible(self) -> bool:
        """At least half its clauses - the floor below which there is nothing
        to report."""
        return self.matched * 2 >= self.clauses

    @property
    def rank(self) -> tuple[float, int, int]:
        """How strong AND how specific this answer is, most convincing first.

        How much of the signature was satisfied comes first; then, as the
        tie-break the outcome rules name, the number of signature clauses (a
        two-clause sub-archetype beats a one-clause parent, because the more
        specific claim was the harder one to satisfy), then the number of
        `exclude` clauses (a file that says what it is NOT is a more precise
        claim than one that does not).
        """
        return (self.matched / self.clauses, self.clauses, len(self.signature.exclude))


@dataclass(frozen=True)
class Identification:
    """What the classifier can honestly say about a list.

    Not a `(guide, score)` pair: a caller has to be able to say "matched as
    X, and Y is also plausible", which needs the confidence and the runners-up
    as well as the winner.
    """

    confidence: str
    guide: DeckGuide | None = None
    candidates: tuple[Candidate, ...] = ()
    """Every archetype that was in contention, most convincing first."""

    archetype: str | None = None
    """The archetype this resolves to: the winner's own name for itself, or
    the shared `parent` when the candidates tie and only family clauses were
    matched."""

    @property
    def leaders(self) -> tuple[Candidate, ...]:
        """The candidates that answered the largest share of their own
        signature - the ones the list gave no reason to prefer between."""
        if not self.candidates:
            return ()
        best = self.candidates[0].matched / self.candidates[0].clauses
        return tuple(c for c in self.candidates if c.matched / c.clauses == best)

    @property
    def alternatives(self) -> tuple[Candidate, ...]:
        """The candidates other than the one whose guide was handed over."""
        return tuple(c for c in self.candidates if c.guide is not self.guide)

    @property
    def guides(self) -> tuple[DeckGuide, ...]:
        """Every guide worth handing the agent. One, normally; every leading
        branch when the list resolved to a family, where naming one branch
        would have handed over the wrong branch's gameplan."""
        if self.guide is not None:
            return (self.guide,)
        return tuple(c.guide for c in self.leaders)


def _family(leaders: list[Candidate]) -> str | None:
    """The family name, when that is all the list actually said.

    A family - `tron` today - is written as several `archetype.json` files
    sharing a `parent` and repeating the clauses the whole family has in
    common, each adding the clause that identifies its own branch. A list
    that answers only the shared clauses has said "some kind of tron" and
    nothing more, and `eldrazi-tron/archetype.json` spells out what to do
    with that: name the family, because naming a branch whose cards are
    absent hands the pilot the guide for a deck they are not playing.

    Recognised by the leaders having satisfied the same clauses as each other
    - identical `evidence` - while declaring the same parent. The moment one
    of them answers a clause the others do not, the list has chosen a branch
    and this returns `None`.
    """
    if len(leaders) < 2:
        return None
    parents = {c.signature.parent for c in leaders}
    if len(parents) != 1 or None in parents:
        return None
    if len({c.evidence for c in leaders}) != 1:
        return None
    return parents.pop()


def _analysis_key(path: Path) -> tuple[int, str, str]:
    """Sort key that puts the newest dated analysis run last.

    The names are `<deck>-modern-<YYYY-MM-DD>`, so sorting the paths as
    strings sorts by deck name and only picks the newest run by accident -
    two runs whose name prefixes differ is all it takes for the older one to
    win. Undated directories sort before every dated one, among themselves
    by name.
    """
    match = _DATE_SUFFIX.search(path.name)
    return (1, match.group(0), path.name) if match else (0, "", path.name)


class GuideLibrary:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def decks(self) -> list[DeckGuide]:
        decks_dir = self.root / "decks"
        if not decks_dir.is_dir():
            return []
        found = []
        for entry in sorted(decks_dir.iterdir()):
            decklist = entry / "decklist.txt"
            if not entry.is_dir() or not decklist.is_file():
                continue
            found.append(self._build(entry, decklist))
        return found

    def _build(self, entry: Path, decklist: Path) -> DeckGuide:
        def optional(name: str) -> Path | None:
            path = entry / name
            return path if path.is_file() else None

        runs = sorted(
            (p for p in entry.glob("analysis/*") if (p / "domain3-matchup-threat").is_dir()),
            key=_analysis_key,
        )
        domain = runs[-1] / "domain3-matchup-threat" if runs else None
        matchups = domain / "matchups" if domain is not None else None
        boarding = domain / "boarding-plan.json" if domain is not None else None
        return DeckGuide(
            slug=entry.name,
            root=entry,
            decklist=decklist,
            gameplan=optional("GAMEPLAN_GUIDE.md"),
            sideboard=optional("SIDEBOARD_GUIDE.md"),
            meta_calls=optional("META_CALLS.md"),
            matchups_dir=matchups if matchups is not None and matchups.is_dir() else None,
            archetype=optional("archetype.json"),
            boarding_plan=boarding if boarding is not None and boarding.is_file() else None,
        )

    def get(self, slug: str) -> DeckGuide | None:
        for deck in self.decks():
            if deck.slug == slug:
                return deck
        return None

    def identify(self, decklist: Decklist) -> Identification:
        """Which guided deck this list is, and how sure the corpus lets us be.

        Every archetype is asked the same question about the same mainboard,
        so a list arrives at the same answer whether it was bundled with the
        lobby or pasted in by a user.

        A full match beats every partial one, so the runners-up reported
        alongside a `CONFIDENT` answer are only other FULL matchers - there
        are none, or the answer would not have been confident. When nothing
        matches fully, every archetype that answered at least half its
        clauses is reported, strongest first, and the answer is `LOW`
        whether that is one archetype or five.
        """
        board = decklist.mainboard
        candidates = []
        for deck in self.decks():
            signature = deck.signature()
            if signature is None:
                continue
            if any(clause.satisfied_by(board) for clause in signature.exclude):
                # An exclude clause is a positive statement - "a list holding
                # these is playing the other deck, whatever else it also
                # holds" - so it removes the archetype from contention
                # entirely rather than only from a full match.
                continue
            satisfied = tuple(c for c in signature.signature if c.satisfied_by(board))
            candidates.append(Candidate(deck, signature, satisfied))

        contenders = [c for c in candidates if c.full] or [c for c in candidates if c.plausible]
        if not contenders:
            return Identification(NONE)

        ordered = tuple(sorted(contenders, key=lambda c: (*(-x for x in c.rank), c.slug)))
        top = ordered[0]
        # The family question comes before the tie-breaks, because the
        # tie-breaks rank by specificity and specificity is not the issue
        # here: two branches of one family that the list gave no reason to
        # prefer between. Leaders by strength alone, not by full rank - a
        # branch carrying one more `exclude` clause than its sibling would
        # otherwise "win" a comparison the list never decided.
        leaders = [c for c in ordered if c.matched / c.clauses == top.matched / top.clauses]
        family = _family(leaders)
        if family is not None:
            return Identification(LOW, None, ordered, family)
        tied = [c for c in ordered if c.rank == top.rank]
        if len(tied) > 1:
            # Same strength, same clause count, same exclude count: nothing
            # left to separate them, so both are reported and the guide is
            # the first by slug rather than by any real preference.
            return Identification(LOW, tied[0].guide, ordered, tied[0].archetype)
        confidence = CONFIDENT if top.full else LOW
        return Identification(confidence, top.guide, ordered, top.archetype)
