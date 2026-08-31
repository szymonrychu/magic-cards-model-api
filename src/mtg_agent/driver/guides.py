"""Which of the guided decks is the agent piloting, and where are its guides.

The agent is handed a decklist by the game server, not a deck name. Matching
it against the decklists under /guides is what turns "60 card names" into
"you are on dimir-frog, here is the gameplan". The match is deliberately
fuzzy: a real list swaps a few flex slots, and a deck that is 90% dimir-frog
is dimir-frog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: Below this similarity we say "no guide" rather than hand over the wrong
#: deck's gameplan, which would be worse than none.
MATCH_THRESHOLD = 0.6

_LINE = re.compile(r"^\s*(\d+)\s*x?\s+(.+?)\s*$")
_TRAILING_SET = re.compile(r"\s*\([^)]*\)\s*[\dA-Za-z-]*\s*$")
_SECTION = re.compile(r"^\s*(sideboard|sb:|maindeck|mainboard|deck)\b", re.IGNORECASE)


def parse_decklist(text: str) -> dict[str, int]:
    """Card name to total count, mainboard and sideboard folded together.

    Folded, because identification only asks "which deck is this"; the
    sideboard is as characteristic as the maindeck and the server hands both
    over anyway.
    """
    counts: dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "//")) or _SECTION.match(line):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        name = _TRAILING_SET.sub("", match.group(2)).strip()
        if name:
            counts[name] = counts.get(name, 0) + int(match.group(1))
    return counts


@dataclass
class DeckGuide:
    slug: str
    root: Path
    decklist: Path
    gameplan: Path | None = None
    sideboard: Path | None = None
    meta_calls: Path | None = None
    matchups_dir: Path | None = None

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


def _similarity(played: dict[str, int], known: dict[str, int]) -> float:
    """Overlap of the two lists as a fraction of the larger one, counting
    copies. Two identical lists score 1.0."""
    total = max(sum(played.values()), sum(known.values()))
    if not total:
        return 0.0
    shared = sum(min(count, known.get(name, 0)) for name, count in played.items())
    return shared / total


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

        matchups = sorted(entry.glob("analysis/*/domain3-matchup-threat/matchups"))
        return DeckGuide(
            slug=entry.name,
            root=entry,
            decklist=decklist,
            gameplan=optional("GAMEPLAN_GUIDE.md"),
            sideboard=optional("SIDEBOARD_GUIDE.md"),
            meta_calls=optional("META_CALLS.md"),
            matchups_dir=matchups[-1] if matchups else None,
        )

    def get(self, slug: str) -> DeckGuide | None:
        for deck in self.decks():
            if deck.slug == slug:
                return deck
        return None

    def identify(self, decklist: dict[str, int]) -> tuple[DeckGuide | None, float]:
        best: DeckGuide | None = None
        best_score = 0.0
        for deck in self.decks():
            score = _similarity(decklist, parse_decklist(deck.decklist.read_text()))
            if score > best_score:
                best, best_score = deck, score
        if best_score < MATCH_THRESHOLD:
            return None, best_score
        return best, best_score
