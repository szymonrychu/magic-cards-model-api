"""The matchup card: the deck's plan and the field's tells, rendered small
enough to inline into turn 0.

The agent used to be handed its guide *paths* and told to read them. The
gameplan guide is 42-73 KB of markdown - 12-18k tokens of `Read` before the
first action, every game, paid again on every context compaction. And it is
generated: `analysis/<run>/domain3-matchup-threat/boarding-plan.json` is the
structured source those 73 KB are rendered from, and it was sitting unread
next to the file the agent was told to open.

So this module renders the same knowledge into turn-0 context, where it is
nearly free, in three sections:

1. **What your deck is doing** - the plan's `deck_tech` rows, its known
   `gaps`, and a keep rule for the case where the matchup is not known yet.
2. **The tells** - one row per archetype the plan covers, carrying that
   archetype's *own* signature cards. This is the section the owner's
   "identify the matchup yourself" doctrine actually needs: the signature
   cards ARE the tell, and until now they were buried in a 58 KB file the
   agent was reading for something else entirely.
3. **The three that are most of the field**, in detail, with the boarding
   swaps intersected against the list the server actually dealt.

The opponent's archetype stays hidden, deliberately - so this card is never
about a known opponent. It is about the agent's own deck, plus what to look
for across the whole field.

The card is budgeted: `CARD_TARGET_BYTES` is what it aims at once it is
trimming and `CARD_HARD_CAP_BYTES` is what it will not exceed. Full
fidelity gets the whole cap, because it is the only rendering that gives
nothing up; under pressure the card walks down `_TRIMS`, which gives in the
order the brief sets - the tells table's `watch_for` prose first, then
section 3's detail - and only below both of the brief's own levers does a
field get deleted outright: the `watch for` column, and then section 3's
detail altogether. Below every one of those it gives tells rows, the
smallest slices of the field first. Whatever it gave, it says so in the
rendered output, and it names what is gone where the thing used to be: a
field that quietly vanished would be worse than a field the agent knows is
abridged.

The cap is not one of those levers - it is the line past which the renderer
has nothing left but to drop rows and then cut bytes. So the budget is per
card and it is never divided: a caller with two branches of a family to brief
renders two whole cards (see `prompt.render_current_game`), because the half
budget that buys is paid for out of a field the brief requires on every one
of fifteen to nineteen rows.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .guides import (
    UNKNOWN_ARCHETYPE,
    DeckGuide,
    Decklist,
    held_faces,
    holds,
    load_signature,
    slugify,
)

#: What a caller hands the card: the parsed 75 the server dealt. A bare
#: one-board mapping is accepted for the caller that only has one (see
#: `guides.held_faces`) - the swaps are intersected against the WHOLE 75,
#: because every card a plan boards IN comes out of the sideboard.
Held = Decklist | Mapping[str, int]

#: What the card aims at. Roughly 2.5k tokens - the cost of inlining it is
#: an order of magnitude under the `Read` it replaces.
CARD_TARGET_BYTES = 10 * 1024

#: What it will not exceed, whatever the guide throws at it (~3k tokens).
CARD_HARD_CAP_BYTES = 12 * 1024

#: How much of a matchup's `watch_for` a tells row carries before the rest
#: is elided. One line of a table, not a paragraph.
WATCH_FOR_CAP = 160

#: How many of an archetype's signature cards a tells row carries. Fixed,
#: not a rung: `watch for` is the tells table's only trim column, because
#: the signature cards are what section 2 is for.
SIGNATURE_CARDS = 4

#: The rungs the tells table's prose gives on: the first thing to give.
#: It stops at 85 rather than running to nothing, because a `watch for`
#: clipped below a clause stops being a tell - "An untapped Marsh Flats on
#: turn two i..." is not one - and past that floor the column itself goes
#: rather than being rendered as a column of half-sentences.
_WATCH_LADDER = (WATCH_FOR_CAP, 135, 110, 85)

#: Then, and only then, the top-three detail thins out. Level 0 is
#: everything; 3 is the last stop before the section goes entirely, and even
#: there every named field is still represented by something.
_DETAIL_LEVELS = (0, 1, 2, 3)

_ELLIPSIS = "..."
_COUNT = re.compile(r"^\s*(\d+)\s*x?\s+(.*)$")
_ABSENT = "(NOT IN YOUR LIST)"


@dataclass(frozen=True)
class _Trim:
    """One rung of the trim ladder: how much of the card gets rendered.

    Every field of it is announced by `_notice` when it is not at full
    fidelity, which is the whole reason the rungs are a value rather than a
    pair of loop variables: what the card says it did to itself is derived
    from the same thing that did it.
    """

    watch_cap: int = WATCH_FOR_CAP
    """How much of each `watch for` a tells row carries."""

    detail: int = 0
    """Which rung of section 3's detail, 0 being all of it."""

    watch_column: bool = True
    """Whether the tells table has a `watch for` column at all."""

    top_three: bool = True
    """Whether section 3 carries its detail, or only names the three."""

    @property
    def full(self) -> bool:
        return (
            self.watch_cap == WATCH_FOR_CAP
            and self.detail == 0
            and self.watch_column
            and self.top_three
        )

    @property
    def keeps_required_fields(self) -> bool:
        """Whether every field the brief requires is still *present*.

        The brief authorises exactly two levers - shorten the tells table's
        `watch_for` TEXT, then thin the top-three detail - and both of them
        leave the field there, abridged. Deleting the `watch for` column or
        section 3's detail is not one of the two: it removes a field the
        brief asks for on every one of fifteen to nineteen rows, and that is
        the thing the cap exists to hold off.
        """
        return self.watch_column and self.top_three


#: The ladder, in the order the brief sets: the tells table's `watch for`
#: prose gives first, the top-three detail second, and only below both of
#: the authorised levers does a field get deleted outright - the column,
#: and then section 3's detail altogether.
#:
#: The two halves are not interchangeable. Everything above
#: `_Trim.keeps_required_fields` going false is an abridgement the brief
#: authorises; everything below it removes a field the brief requires on
#: every row, which is why `render_matchup_card` spends the whole hard cap
#: on the first half before it will touch the second.
#:
#: Getting that order wrong is not a cosmetic mistake, and it was the bug
#: here: with the column's deletion sitting above the detail lever, no rung
#: between them could ever be the rung that fits. The notice a truncated
#: rendering carries costs ~350 bytes that full fidelity does not pay, so
#: every rung that merely SHORTENS the prose measures LARGER than full
#: fidelity (prowess: 10487 whole, 10735 at a 135 character cap) - and the
#: first lever that could actually fire was the deletion. Eleven of the
#: corpus's thirteen guides rendered with no `watch for` column at all,
#: while 2 KB of the cap went unspent and the detail lever never fired
#: anywhere.
#:
#: Each rung within a half renders no more of the card than the rung above
#: it, so the first one that fits is the most of the card that fits - though
#: a rung is not monotonic in MEASURED size, for the notice reason above.
_TRIMS: tuple[_Trim, ...] = (
    # Lever 1: the tells table's `watch for` prose, down to its floor.
    *(_Trim(watch_cap=cap) for cap in _WATCH_LADDER),
    # Lever 2: the top-three detail, with the table already at its floor.
    *(_Trim(watch_cap=_WATCH_LADDER[-1], detail=detail) for detail in _DETAIL_LEVELS[1:]),
    # Below both levers: the column itself, and then section 3's detail.
    *(_Trim(watch_cap=0, detail=detail, watch_column=False) for detail in _DETAIL_LEVELS),
    _Trim(
        watch_cap=0,
        detail=_DETAIL_LEVELS[-1],
        watch_column=False,
        top_three=False,
    ),
)


@dataclass(frozen=True)
class Tell:
    """One row of the tells table: an archetype, and how to spot it."""

    archetype: str
    """The canonical key - the slug `matchups/<archetype>.json` and
    `vs-<archetype>.md` are both named after."""

    display: str
    """The plan's own spelling of the name, kept so a caller can match a
    matchup back to the entry it came from."""

    meta_share: float
    stance: str
    posture: str
    watch_for: str
    signature: tuple[str, ...] = ()
    """That archetype's signature cards, out of its own `archetype.json`.
    Empty when the corpus has no file for it - most fields carry brews and
    fringe decks no one has written up."""


@dataclass(frozen=True)
class MatchupCard:
    text: str
    truncated: bool = False
    tells: list[Tell] = field(default_factory=list)
    top_three: list[Tell] = field(default_factory=list)
    missing_cards: list[str] = field(default_factory=list)
    """Cards the top-three boarding swaps name that the dealt list does not
    contain. Marked in the rendered text too; collected here so a caller can
    log the mismatch without re-parsing the card."""

    omitted: list[Tell] = field(default_factory=list)
    """Archetypes the tells table could not give a row - the last thing the
    renderer gives, below every other lever, and only ever the smallest
    slices of the field. They are still NAMED in the rendered card, so
    `archetypes` below still lists them: a note key that exists is the
    point, not a row."""

    @property
    def archetypes(self) -> list[str]:
        """Every canonical key the tells table named, plus `unknown`.

        These are the keys a `vs-<name>.md` note is allowed to use, so that
        the reflection turn and a later study pass over the notes key
        identically.
        """
        return [tell.archetype for tell in self.tells] + [UNKNOWN_ARCHETYPE]

    @property
    def byte_size(self) -> int:
        return len(self.text.encode())


# ---- reading the corpus ---------------------------------------------------


def load_boarding_plan(guide: DeckGuide | None) -> dict | None:
    """The plan, or `None` for a guide that has none and for one whose file
    will not parse.

    Both are real: a guided deck may predate the analysis pipeline, and a
    half-written file on a bind mount is a thing that happens to a
    long-running compose driver. Neither is worth failing a dispatch over -
    the agent gets a card that says so and plays from its decklist.
    """
    if guide is None or guide.boarding_plan is None:
        return None
    try:
        plan = json.loads(guide.boarding_plan.read_text())
    except (OSError, ValueError):
        return None
    return plan if isinstance(plan, dict) else None


# ---- reading a plan out of a repo that moves on its own schedule ---------
#
# Everything below reads `boarding-plan.json` from the same read-only bind
# mount `archetype.json` comes off, at a ref nothing here pins. So no field
# it names is known to hold the type it is written with, and the card's only
# caller is `/dispatch` - where a raise costs BOTH seats the game before
# either of them has played a card.
#
# These four are the whole vocabulary for reading it, and they are total:
# every one of them answers for any input at all. The alternative is what
# was here before - `.get("x", {}).get(...)` on one line and the safe
# `.get("x") or {}` twenty-four lines later, `float()` over whatever the file
# happened to hold, a bare `[:3]` over a value that is a list only by
# convention - which is not a policy, just the shape each site happened to
# take.


def _mapping(value: object) -> Mapping:
    """A nested block, or an empty one. `value` is what the file holds, which
    may be `null` - and `.get(key, {})` returns a stored `None` rather than
    the default, which is the difference that raised."""
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[dict]:
    """A list of blocks, keeping only the entries that are blocks."""
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _strings(value: object) -> list[str]:
    """A list of names. Only a list: a mapping is subscripted rather than
    sliced, so `(value or [])[:3]` over one is a `KeyError` naming the slice
    itself."""
    return (
        [text for text in (_text(item) for item in value) if text]
        if isinstance(value, list)
        else []
    )


def _text(value: object) -> str:
    """A field read as prose. A list or a mapping is not prose, so it reads as
    absent rather than as its `repr` - and, being a string either way, it can
    be used as a dictionary key without caring what the file held."""
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, bool | Mapping | list):
        return ""
    return str(value)


def _number(value: object) -> float:
    """A share or a count. A plan hand-edited to `"20%"` has a share that is
    unreadable, not a plan that is unrenderable - and the only thing the
    number does here is order rows and tie-break a keep."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _display(matchup: Mapping) -> str:
    """The archetype name a matchup row calls itself, read the ONE way.

    Both the tells table and section 3 key on this field, and they used to
    read it differently: the table coerced it with `str()` while section 3
    built `{m.get("name"): m}` on the raw value - so a name that was a list
    raised `TypeError: unhashable type` there, and a row with no name at all
    quietly lost its detail because the table had already turned it into
    `unknown` and the index was keyed on `None`.
    """
    return _text(matchup.get("name")) or UNKNOWN_ARCHETYPE


def signature_index(guide: DeckGuide | None) -> dict[str, tuple[str, ...]]:
    """Canonical archetype key -> that archetype's signature cards.

    Keyed under both the directory slug and the slug of the name the file
    calls *itself*, because those differ and the boarding plan uses the
    latter: `decks/affinity/archetype.json` says `"archetype": "pinnacle
    affinity"`, and `pinnacle affinity` is what every plan in the corpus
    writes. Keying on the directory alone silently loses a third of the
    table's tells.

    Read through `guides.load_signature`, never parsed here. This used to be
    a second reader of `archetype.json` - the same third-repo schema, its own
    `json.loads`, and weaker checks than the first: no `isinstance` at either
    level, so `{"signature": 5}` raised out of THIS function while
    `load_signature` shrugged it off, and each reader had its own opinion of
    which files were usable. A second reader that is weaker than the first
    will drift from it again, so there is one reader and this asks it.
    """
    if guide is None:
        return {}
    index: dict[str, tuple[str, ...]] = {}
    for path in guide.sibling_archetype_files():
        signature = load_signature(path)
        if signature is None:
            continue
        cards = tuple(card for clause in signature.signature for card in clause.cards)
        if not cards:
            continue
        # `signature.name` is the file's own name for itself, already fallen
        # back to the directory name when the file does not give a usable
        # one - so both keys come out of the one validated result.
        for key in (slugify(path.parent.name), slugify(signature.name)):
            index.setdefault(key, cards)
    return index


def generic_keep_rule(plan: dict) -> tuple[str, int, int]:
    """The keep rule to use before the matchup is known: the most common keep
    across the plan's own matchups, with how often it appears and out of how
    many.

    A boarding plan has no deck-wide keep rule - every keep in it is written
    for a named matchup. Presenting one matchup's keep as universal would be
    a quiet lie, so the card presents the modal one *as* the modal one. Ties
    go to the keep whose matchups are more of the field, then alphabetically,
    so the same plan always renders the same rule.
    """
    keeps = [
        (_text(_mapping(m.get("play_plan")).get("keep")).strip(), _number(m.get("meta_share")))
        for m in _matchups(plan)
    ]
    keeps = [(text, share) for text, share in keeps if text]
    if not keeps:
        return "", 0, 0
    tally: dict[str, tuple[int, float]] = {}
    for text, share in keeps:
        count, weight = tally.get(text, (0, 0.0))
        tally[text] = (count + 1, weight + share)
    best = min(tally.items(), key=lambda item: (-item[1][0], -item[1][1], item[0]))
    return best[0], best[1][0], len(keeps)


def _matchups(plan: dict) -> list[dict]:
    return _rows(plan.get("matchups"))


def _tells(plan: dict, index: dict[str, tuple[str, ...]], trim: _Trim) -> list[Tell]:
    tells = []
    for matchup in _matchups(plan):
        display = _display(matchup)
        archetype = slugify(display)
        play_plan = _mapping(matchup.get("play_plan"))
        tells.append(
            Tell(
                archetype=archetype,
                display=display,
                meta_share=_number(matchup.get("meta_share")),
                stance=_text(matchup.get("stance")) or "-",
                posture=_text(play_plan.get("posture")) or "-",
                # Empty, not merely short, when the rung has given up the
                # column: a `Tell` must never carry prose the card does not
                # show, or a caller reading it back overstates the card.
                watch_for=(
                    _clip(_text(play_plan.get("watch_for")), trim.watch_cap)
                    if trim.watch_column
                    else ""
                ),
                signature=index.get(archetype, ()),
            )
        )
    return tells


# ---- text helpers ---------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    """One line, at most `limit` characters, and honest about the cut."""
    flat = " ".join(text.split())
    if limit <= 0 or len(flat) <= limit:
        return flat
    room = max(limit - len(_ELLIPSIS), 0)
    return flat[:room].rstrip() + _ELLIPSIS


def _cell(text: str) -> str:
    """A markdown table cell: no pipes, no newlines, or the table breaks and
    the agent reads one row as another."""
    return " ".join(text.split()).replace("|", "/")


def _swap(entries: object, decklist: Held) -> str:
    """A boarding swap list, each card checked against the list the server
    actually dealt.

    The plan is written for the 75 the analysis was run on. A real list swaps
    flex slots and a sideboard slot is the first thing to move, so "board in
    3 Mystical Dispute" against a list holding none of them is a confidently
    wrong instruction - the sort that costs a game while the agent looks for
    a card it does not have.

    Against the whole 75, not the maindeck: every card a swap boards IN
    comes out of the sideboard by definition, so intersecting on the
    mainboard alone stamps `(NOT IN YOUR LIST)` on every single board-in and
    turns the check into noise. `guides.held_faces` is what guarantees it.
    """
    names = _swap_names(entries)
    if not names:
        return "-"
    held = held_faces(decklist)
    rendered = []
    for raw, name in names:
        rendered.append(raw if holds(held, name) else f"{raw} {_ABSENT}")
    return ", ".join(rendered)


def _swap_names(entries: object) -> list[tuple[str, str]]:
    """`["2 Fatal Push"]` -> `[("2 Fatal Push", "Fatal Push")]`."""
    if not isinstance(entries, list):
        return []
    out = []
    for entry in entries:
        raw = " ".join(str(entry).split())
        if not raw:
            continue
        match = _COUNT.match(raw)
        out.append((raw, (match.group(2) if match else raw).strip()))
    return out


def _missing(matchups: list[dict], decklist: Held) -> list[str]:
    held = held_faces(decklist)
    absent = set()
    for matchup in matchups:
        for side in ("play", "draw"):
            block = _mapping(matchup.get(side))
            for key in ("in", "out"):
                for _, name in _swap_names(block.get(key)):
                    if not holds(held, name):
                        absent.add(name)
    return sorted(absent)


def _pct(share: float) -> str:
    return f"{share:.1f}%"


# ---- sections -------------------------------------------------------------


def _header(guide: DeckGuide, plan: dict | None) -> list[str]:
    lines = [f"## Matchup card - {guide.slug}", ""]
    if plan is None:
        lines += [
            "There is **no boarding plan** for this deck in the guides corpus, so "
            "there is no card to render: no deck plan, no field tells, no matchup "
            "files. Play from your decklist and from what the board tells you, and "
            f"file whatever you learn under `{UNKNOWN_ARCHETYPE}`.",
            "",
        ]
        return lines
    guide_size = ""
    if guide.gameplan is not None:
        try:
            guide_size = f" {guide.gameplan.stat().st_size // 1024} KB"
        except OSError:
            guide_size = ""
    lines += [
        f"Rendered from `{guide.boarding_plan}`, the structured plan the"
        f"{guide_size} gameplan guide is itself generated from. You do not need to "
        "read that guide to know what this deck does - it is all below.",
        "",
    ]
    return lines


#: What the deck section is allowed, in characters of body text. A boarding
#: plan carries three to four `deck_tech` rows of two to five lines each plus
#: one to three `gaps`, and each of those is a paragraph - prison-tron's plan
#: has twenty-six of them. So the per-line room is derived from this
#: allowance and the number of lines rather than fixed: a plan with twice the
#: tech renders each line at half the length instead of rendering a section
#: twice the size.
#:
#: One number, not a rung per detail level: the card is allowed exactly two
#: trim levers, `_WATCH_LADDER` then `_DETAIL_LEVELS`, and those are the two
#: `_notice` names. A section 1 that also gave as the detail level rose was
#: a third lever nothing announced - the notice said the top-three detail
#: had been shortened while roughly 300 bytes had quietly come out of the
#: deck tech as well, which is the silent drop the budget rule exists to
#: prevent.
_DECK_SECTION_CHARS = 1000

#: Below this a line's reasoning is not worth carrying at all, so a plan
#: dense enough to push past the allowance overruns it rather than render a
#: column of half-sentences.
_DECK_LINE_FLOOR = 45


def _deck_section(plan: dict) -> list[str]:
    keep, count, total = generic_keep_rule(plan)
    lines = ["### 1. What your deck is doing", ""]
    if keep:
        lines += [
            f"**Keep rule, before you know the matchup**: {keep} (the most common keep "
            f"in this plan, {count} of {total} matchups; the three biggest have their "
            "own, in section 3).",
            "",
        ]
    tech = _rows(plan.get("deck_tech"))
    gaps = _rows(plan.get("gaps"))
    tech_lines = [_rows(row.get("lines")) for row in tech]
    # A gap is the only place the plan admits it has no answer, so it counts
    # double and is rendered at double the room.
    body = sum(len(rows) for rows in tech_lines) + len(tech) + 2 * len(gaps)
    room = max(_DECK_SECTION_CHARS // body, _DECK_LINE_FLOOR) if body else 0
    for row, rows in zip(tech, tech_lines, strict=True):
        cards = ", ".join(_strings(row.get("cards"))[:3])
        head = f"- **{_text(row.get('name')) or UNKNOWN_ARCHETYPE}**" + (
            f" ({cards})" if cards else ""
        )
        lines.append(f"{head} - {_clip(_text(row.get('quirk')), room)}".rstrip(" -"))
        for line in rows:
            against = ", ".join(_strings(line.get("matchups"))[:2])
            head = f"  - {_clip(_text(line.get('mode')), 34)}" + (
                f" vs {against}" if against else ""
            )
            lines.append(f"{head}: {_clip(_text(line.get('why')), room)}".rstrip(": "))
    if tech:
        lines.append("")
    if gaps:
        lines.append("**Known holes in this plan** - the plan says so itself:")
        for gap in gaps:
            matchup = _text(gap.get("matchup")) or UNKNOWN_ARCHETYPE
            lines.append(f"- vs {matchup}: {_clip(_text(gap.get('why')), 2 * room)}")
        lines.append("")
    return lines


def _matchups_dir(guide: DeckGuide) -> str:
    return (
        f"`{guide.matchups_dir}/<archetype>.json`"
        if guide.matchups_dir is not None
        else "(this deck has no per-matchup files)"
    )


def _tells_section(
    guide: DeckGuide,
    tells: list[Tell],
    omitted: Sequence[Tell] = (),
    *,
    watch_column: bool = True,
) -> list[str]:
    matchups_dir = _matchups_dir(guide)
    gone = set(omitted)
    shown = [tell for tell in tells if tell not in gone]
    covers = (
        f"One row per archetype this plan covers ({len(shown)})."
        if not omitted
        else f"One row per archetype this plan covers that fit ({len(shown)} of "
        f"{len(tells)}; the rest are named under the table)."
    )
    lines = [
        "### 2. Tells - which deck is across the table",
        "",
        f"Nothing will tell you what is across the table; working it out is your job. "
        f"{covers} **The signature cards "
        "are the tell** - two or three of one row's is the read, a card several rows "
        "share is not. `stance` beats that deck, `posture` is how to play while you "
        "do it.",
        "",
    ]
    columns = ["archetype", "field", "stance", "your posture", "signature cards - the tell"]
    if watch_column:
        columns.append("watch for")
    lines += [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for tell in shown:
        signature = ", ".join(tell.signature[:SIGNATURE_CARDS]) if tell.signature else "-"
        cells = [
            f"`{tell.archetype}`",
            _pct(tell.meta_share),
            _cell(tell.stance),
            _cell(tell.posture),
            _cell(signature),
        ]
        if watch_column:
            cells.append(_cell(tell.watch_for) or "-")
        lines.append("| " + " | ".join(cells) + " |")
    if omitted:
        named = ", ".join(f"`{t.archetype}` ({_pct(t.meta_share)})" for t in omitted)
        lines += [
            "",
            f"**No room for a row**: {len(omitted)} archetypes, the smallest slices of "
            f"the field, did not fit this card's byte budget: {named}. Nothing about "
            "them is lost - each has its own matchup file, named just below, and each "
            "of those keys is a `vs-<archetype>.md` key like any other row's.",
        ]
    lines += [
        "",
        f"Once their cards match a row, ONE read gets that whole matchup: "
        f"{matchups_dir}, first column's name exactly as spelled there. Those files "
        "are 3-9 KB, 28 KB at the largest - against the 100+ KB plan they were "
        "split out of: one small read, once, when you have the read.",
        "",
        "If what you have seen matches none of the rows, say so and stop hunting - "
        "there is no matchup file for this opponent, it is a brew, and section 1 is "
        f"the honest answer. Your note is `vs-<archetype>.md` keyed on the first "
        f"column's spelling, or on `{UNKNOWN_ARCHETYPE}` for that case; never a name "
        "you invented, because a later pass looks these up by exactly those keys.",
        "",
    ]
    return lines


def _top_three_section(
    matchups: list[dict],
    tells: list[Tell],
    decklist: Held,
    detail: int,
) -> list[str]:
    if not matchups:
        return []
    by_display = {_display(m): m for m in matchups}
    lines = [
        "### 3. The three archetypes that are most of the field",
        "",
    ]
    rationale_cap = (140, 120, 100, 80)[detail]
    watch_cap = (130, 110, 90, 70)[detail]
    keep_cap = (120, 105, 90, 75)[detail]
    survives_cap = (60, 45, 30, 0)[detail]
    keys_cap = (3, 3, 2, 2)[detail]
    for rank, tell in enumerate(tells, start=1):
        matchup = _mapping(by_display.get(tell.display))
        play_plan = _mapping(matchup.get("play_plan"))
        opponent = _mapping(matchup.get("opponent_plan"))
        lines.append(f"#### {rank}. `{tell.archetype}` - {_pct(tell.meta_share)} of the field")
        lines.append(
            f"- stance **{tell.stance}**: "
            f"{_clip(_text(matchup.get('stance_rationale')), rationale_cap)}"
        )
        lines.append(f"- keep: {_clip(_text(play_plan.get('keep')) or '-', keep_cap)}")
        lines.append(
            f"- goldfish for: {_clip(_text(play_plan.get('goldfish_for')) or '-', keep_cap)}"
        )
        lines.append(f"- watch for: {_clip(_text(play_plan.get('watch_for')) or '-', watch_cap)}")
        keys = [
            card
            for card in (_text(k.get("card")) for k in _rows(opponent.get("key_cards")))
            if card
        ]
        posture = _text(opponent.get("posture")) or "-"
        their_plan = f"- their plan is to **{posture}**: " + _clip(
            _text(opponent.get("posture_why")), rationale_cap
        )
        if keys:
            their_plan += f" Their cards that matter: {', '.join(keys[:keys_cap])}."
        lines.append(their_plan)
        survives = _rows(matchup.get("survives_opposing_board"))
        if survives:
            rendered = []
            for entry in survives[:keys_cap]:
                text = f"{_text(entry.get('card'))} ({_text(entry.get('verdict'))})"
                if survives_cap:
                    text += f" - {_clip(_text(entry.get('why')), survives_cap)}"
                rendered.append(text)
            lines.append("- your cards against their board: " + "; ".join(rendered))
        for side, label in (("play", "on the play"), ("draw", "on the draw")):
            block = _mapping(matchup.get(side))
            lines.append(
                f"- {label}: in {_swap(block.get('in'), decklist)} "
                f"/ out {_swap(block.get('out'), decklist)}"
            )
        lines.append("")
    absent = _missing(matchups, decklist)
    if absent:
        lines += [
            f"{_ABSENT} marks a card the plan's swaps name that the list you were "
            f"actually dealt does not contain ({len(absent)} of them: "
            f"{', '.join(absent)}). Do not go looking for those - board the rest of "
            "the swap and say in your plan which half you could not make.",
            "",
        ]
    return lines


def _top_three_named(guide: DeckGuide, top_three: list[Tell]) -> list[str]:
    """Section 3 when its detail did not fit: the three still named, and the
    one read that has what the section would have said.

    The section stays, rather than vanishing and leaving the card with two
    sections where its own header promises three. What the agent loses here
    it can get back with a single read; what it must not lose is knowing
    that there was something to get back.
    """
    named = ", ".join(f"`{t.archetype}` ({_pct(t.meta_share)})" for t in top_three)
    where = (
        f"the matchup file for each - {_matchups_dir(guide)}, the one small read named "
        "above. Read the one you have a read on"
        if guide.matchups_dir is not None
        else "the boarding plan named at the top of this card; this deck has no "
        "per-matchup files to read instead"
    )
    return [
        "### 3. The three archetypes that are most of the field",
        "",
        f"{named}. The detail on these - stance and why, keep, goldfish plan, what "
        "to watch for, their plan, and the boarding swaps - did NOT fit this card's "
        f"byte budget. It is all in {where}.",
        "",
    ]


# ---- assembly -------------------------------------------------------------


def _build(
    guide: DeckGuide,
    plan: dict | None,
    decklist: Held,
    index: dict[str, tuple[str, ...]],
    trim: _Trim,
    omitted: Sequence[Tell] = (),
) -> tuple[str, list[Tell], list[Tell]]:
    lines = _header(guide, plan)
    if plan is None:
        return "\n".join(lines), [], []
    tells = _tells(plan, index, trim)
    matchups = _matchups(plan)
    top_three = sorted(tells, key=lambda t: -t.meta_share)[:3]
    lines += _deck_section(plan)
    if tells:
        lines += _tells_section(guide, tells, omitted, watch_column=trim.watch_column)
        lines += (
            _top_three_section(matchups, top_three, decklist, trim.detail)
            if trim.top_three
            else _top_three_named(guide, top_three)
        )
    return "\n".join(lines), tells, top_three


def _list(items: Sequence[str]) -> str:
    if len(items) < 3:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _notice(trim: _Trim, omitted: Sequence[Tell] = (), *, hard_cut: bool = False) -> str:
    """The card's account of what it did to itself, derived from the rung it
    was rendered at rather than written by hand.

    It names every lever that fired, and it distinguishes what was SHORTENED
    - still there, abridged - from what was DROPPED, which is named where it
    was dropped from. The one thing it may never do is the thing it used to
    do at every rung: claim that nothing was dropped while a section, a
    column or a row had gone.
    """
    shortened, dropped = [], []
    if trim.watch_column and trim.watch_cap < WATCH_FOR_CAP:
        shortened.append(f"the tells table's `watch for` prose (to {trim.watch_cap} characters)")
    if trim.top_three and trim.detail:
        shortened.append("the detail under the three biggest matchups")
    if not trim.watch_column:
        dropped.append("the tells table's `watch for` column")
    if not trim.top_three:
        dropped.append("section 3's detail on the three biggest matchups")
    if omitted:
        dropped.append(f"the tells rows of {len(omitted)} archetypes, the smallest in the field")
    parts = ["> **Truncated**: this plan does not fit the card's byte budget."]
    if shortened:
        parts.append(f" Shortened: {_list(shortened)}.")
    if dropped:
        parts.append(f" Dropped, and named in the card where it was dropped: {_list(dropped)}.")
    if hard_cut:
        parts.append(
            " Then the card was cut at its hard cap: whatever follows the cut marker at "
            "the end of it is missing entirely."
        )
    elif dropped:
        parts.append(
            " Every archetype this plan covers is still named in the card, and the plan "
            "it is rendered from - named at the top - has the full text of all of it."
        )
    else:
        parts.append(
            " Nothing was dropped - every archetype still has a row, and the full text "
            "is in the matchup file named in section 2."
        )
    return "".join(parts) + "\n"


def render_matchup_card(
    guide: DeckGuide | None,
    decklist: Held,
    *,
    target_bytes: int = CARD_TARGET_BYTES,
    hard_cap_bytes: int = CARD_HARD_CAP_BYTES,
) -> MatchupCard:
    """The card, at most `hard_cap_bytes` of it, for `guide` and the list the
    server actually dealt.

    The target and the cap are not two names for one budget, and the order
    the rungs are tried in says so:

    1. **Full fidelity, if `hard_cap_bytes` can hold it.** It is the only
       rendering that gives nothing up, so it is the only one that gets the
       whole cap - trimming is never free, it costs the agent a tell, and
       the card does not pay that to reach a target it could clear the cap
       at. This step is what was missing. Full fidelity is 10043-11566
       bytes for every plan in the corpus (all thirteen, corpus root at
       `~/Documents/mtg-decks`; the slice's README carries the table and
       says why a raw figure moves with the root): over the 10240 byte
       target, under the 12288 byte cap. A ladder that aimed only at the
       target rendered eleven of the corpus's thirteen guides with the
       `watch for` column deleted off all nineteen rows and ~2 KB of the
       cap unspent.
    2. Once something has to give, the ladder aims at `target_bytes` -
       being small is the reason it is giving anything up at all - and the
       rungs that keep every required field go first, against the target
       and then against the cap, before any rung that deletes one gets
       either. A card over target with the field beats a card under target
       without it.
    3. Below every rung, tells ROWS, the smallest slices of the field
       first and named where they were.
    4. And a budget that cannot hold even an empty table gets the byte cut
       of last resort.

    Every rendering below full fidelity carries a notice naming what it
    shortened and what it dropped, and that notice is derived from the rung
    rather than written alongside it - a card that silently loses a field is
    the one outcome this module is not allowed to produce.
    """
    if guide is None:
        return MatchupCard(
            text=(
                "## Matchup card\n\nThere is **no guide** for this decklist, so there "
                "is no boarding plan and no field tells to render. Play it from the "
                "list itself and from what the board tells you, and file what you "
                f"learn under `{UNKNOWN_ARCHETYPE}`.\n"
            )
        )

    plan = load_boarding_plan(guide)
    index = signature_index(guide)
    missing = _missing(_matchups(plan), decklist) if plan else []

    def first_fitting(trims: Sequence[_Trim], budget: int) -> _Rendering | None:
        for trim in trims:
            rendering = _rendering(guide, plan, decklist, index, trim)
            if len(rendering.text.encode()) <= budget:
                return rendering
        return None

    keeping = tuple(trim for trim in _TRIMS[1:] if trim.keeps_required_fields)
    giving_up = tuple(trim for trim in _TRIMS if not trim.keeps_required_fields)
    rendering = (
        # `max`, so a caller that hands over a target above the cap (the
        # tests that ask for an untrimmed card do) still gets step 1.
        first_fitting(_TRIMS[:1], max(target_bytes, hard_cap_bytes))
        or first_fitting(keeping, target_bytes)
        or first_fitting(keeping, hard_cap_bytes)
        or first_fitting(giving_up, target_bytes)
        or first_fitting(giving_up, hard_cap_bytes)
    )
    if rendering is None:
        rendering = _rendering(guide, plan, decklist, index, _TRIMS[-1])
        thinned = _rows_dropped(guide, plan, decklist, index, rendering, hard_cap_bytes)
        rendering = thinned if thinned is not None else rendering
    return _capped(rendering, missing, hard_cap_bytes)


def _rows_dropped(
    guide: DeckGuide,
    plan: dict | None,
    decklist: Held,
    index: dict[str, tuple[str, ...]],
    rendering: _Rendering,
    hard_cap_bytes: int,
) -> _Rendering | None:
    """The smallest rung again, with as little of the field taken off the
    tells table as it takes to fit the cap - or `None` when even an empty
    table does not.

    Rows are the last thing there is to give, and they are given from the
    bottom of the field up: the archetype the agent is least likely to face
    loses its row before the one it is most likely to. Every archetype that
    loses one is still named where the rows were, so its `vs-<name>.md` key
    and its matchup file remain reachable - which is the difference between
    a field abridged and a field silently narrowed.
    """
    order = sorted(rendering.tells, key=lambda tell: (tell.meta_share, tell.archetype))
    for count in range(1, len(order) + 1):
        candidate = _rendering(guide, plan, decklist, index, rendering.trim, tuple(order[:count]))
        if len(candidate.text.encode()) <= hard_cap_bytes:
            return candidate
    return None


@dataclass(frozen=True)
class _Rendering:
    body: str
    tells: list[Tell]
    top_three: list[Tell]
    trim: _Trim
    omitted: tuple[Tell, ...] = ()

    @property
    def truncated(self) -> bool:
        return bool(self.tells) and (not self.trim.full or bool(self.omitted))

    @property
    def text(self) -> str:
        """The body, with the notice on the front when there is something to
        notice. Built here rather than stored so that `_capped` can rebuild
        it - a card that then gets cut needs a notice that says so."""
        if not self.truncated:
            return self.body
        return _notice(self.trim, self.omitted) + "\n" + self.body


def _rendering(
    guide: DeckGuide,
    plan: dict | None,
    decklist: Held,
    index: dict[str, tuple[str, ...]],
    trim: _Trim,
    omitted: tuple[Tell, ...] = (),
) -> _Rendering:
    body, tells, top_three = _build(guide, plan, decklist, index, trim, omitted)
    return _Rendering(body=body, tells=tells, top_three=top_three, trim=trim, omitted=omitted)


def _capped(
    rendering: _Rendering,
    missing: list[str],
    hard_cap_bytes: int,
) -> MatchupCard:
    """The last resort: a budget so small that not even an empty tells table
    fits it gets the card cut at the cap.

    The cut is at the end, after the notice and the deck section, because the
    order the sections are written in is the order they matter in - and the
    notice is re-rendered to say the cut happened, because a notice that
    accounts only for the trim levers would be describing a card the agent
    is not holding.
    """
    text, truncated = rendering.text, rendering.truncated
    if len(text.encode()) > hard_cap_bytes:
        text = _notice(rendering.trim, rendering.omitted, hard_cut=True) + "\n" + rendering.body
        truncated = True
        marker = f"\n\n[cut here: the card hit its {hard_cap_bytes} byte hard cap]\n"
        room = max(hard_cap_bytes - len(marker.encode()), 0)
        text = text.encode()[:room].decode(errors="ignore") + marker
    return MatchupCard(
        text=text,
        truncated=truncated,
        tells=rendering.tells,
        top_three=rendering.top_three,
        missing_cards=missing,
        omitted=list(rendering.omitted),
    )


def matchup_note_key(name: str | None) -> str:
    """The `vs-<key>.md` key for an archetype the agent has deduced.

    Exposed so the reflection turn and any later study pass over the notes
    spell the key the same way the tells table did.
    """
    return slugify(name) if name else UNKNOWN_ARCHETYPE


def matchup_row(plan: dict | None, archetype: str | None) -> tuple[int, str] | None:
    """The row of `plan["matchups"]` written for `archetype`, as `(index,
    name)`, or `None` when the plan has no such row.

    Matched on `slugify` equality, the one spelling the tells table, the
    `vs-<key>.md` notes and the matchup files already share. The index is
    into the raw list, malformed entries included, because it is the JSON
    path `guides propose` names when it refuses an edit to that row.
    """
    if not archetype or not isinstance(plan, dict):
        return None
    rows = plan.get("matchups")
    if not isinstance(rows, list):
        return None
    wanted = slugify(archetype)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        name = _text(row.get("name"))
        if name and slugify(name) == wanted:
            return index, name
    return None


def guide_paths(guide: DeckGuide | None) -> list[tuple[str, Path]]:
    """The guide files that stay available for a deliberate read."""
    if guide is None:
        return []
    labelled = (
        ("gameplan", guide.gameplan),
        ("sideboard", guide.sideboard),
        ("meta calls", guide.meta_calls),
        ("boarding plan (the card above is rendered from this)", guide.boarding_plan),
    )
    return [(label, path) for label, path in labelled if path is not None]
