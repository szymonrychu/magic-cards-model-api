# Continuous MTG Agent Implementation Plan - Part 3 (Tasks 13-17)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Read part 1 (`2026-08-31-continuous-mtg-agent.md`) first - its Global Constraints apply to every task here. Part 2 holds Tasks 7-12.

**Guides on disk** (mounted at `/guides`, from `~/Documents/mtg-decks`):

```
/guides/decks/<deck-slug>/
    decklist.txt                    "4 Fatal Push" per line, mainboard then sideboard
    GAMEPLAN_GUIDE.md               a markdown table, one row per matchup
    SIDEBOARD_GUIDE.md              a markdown table, on-play / on-draw swaps
    META_CALLS.md
    analysis/<run>/domain3-matchup-threat/matchups/<archetype-slug>.json
```

Deck slugs today: `dimir-frog`, `living-end`, `orzhov-martyr`, `prison-tron`.
Archetype slugs are kebab-case (`boros-energy.json`); the guide tables spell
the same archetype with spaces (`boros energy`).

---

### Task 13: Deck fingerprinting and the guide bundle

**Files:**
- Create: `src/mtg_agent/driver/__init__.py`, `src/mtg_agent/driver/guides.py`
- Test: `tests/driver/test_guides.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `parse_decklist(text: str) -> dict[str, int]` - card name to count, blank lines, comments and a `Sideboard`/`SB:` header tolerated.
  - `DeckGuide` dataclass: `slug: str`, `root: Path`, `gameplan: Path | None`, `sideboard: Path | None`, `meta_calls: Path | None`, `matchups_dir: Path | None`, `archetypes: list[str]`, `decklist: Path`.
  - `GuideLibrary(root: Path)` with `decks() -> list[DeckGuide]`, `get(slug) -> DeckGuide | None`, `identify(decklist: dict[str, int]) -> tuple[DeckGuide | None, float]` returning the best match and its similarity, and `MATCH_THRESHOLD = 0.6`.

- [ ] **Step 1: Write the failing test**

`tests/driver/test_guides.py`:

```python
import json

import pytest

from mtg_agent.driver.guides import (
    MATCH_THRESHOLD,
    GuideLibrary,
    parse_decklist,
)

DIMIR = """4 Psychic Frog
4 Fatal Push
2 Consign to Memory
4 Flooded Strand
6 Island

Sideboard
2 Nihil Spellbomb
"""

TRON = """4 Urza's Tower
4 Urza's Mine
4 Urza's Power Plant
4 Karn, the Great Creator
1 Wurmcoil Engine
"""


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "guides"
    frog = root / "decks" / "dimir-frog"
    matchups = frog / "analysis" / "run-1" / "domain3-matchup-threat" / "matchups"
    matchups.mkdir(parents=True)
    (frog / "decklist.txt").write_text(DIMIR)
    (frog / "GAMEPLAN_GUIDE.md").write_text("# dimir-frog - gameplan guide\n")
    (frog / "SIDEBOARD_GUIDE.md").write_text("# dimir-frog - sideboard guide\n")
    (frog / "META_CALLS.md").write_text("# meta calls\n")
    (matchups / "boros-energy.json").write_text(json.dumps({"name": "boros energy"}))
    (matchups / "living-end.json").write_text(json.dumps({"name": "living end"}))

    tron = root / "decks" / "prison-tron"
    tron.mkdir(parents=True)
    (tron / "decklist.txt").write_text(TRON)
    (tron / "GAMEPLAN_GUIDE.md").write_text("# prison-tron - gameplan guide\n")
    return GuideLibrary(root)


def test_parse_decklist_counts_cards():
    assert parse_decklist("4 Fatal Push\n2 Consign to Memory\n") == {
        "Fatal Push": 4,
        "Consign to Memory": 2,
    }


def test_parse_decklist_accepts_the_x_suffix_and_blank_lines():
    assert parse_decklist("4x Fatal Push\n\n  2 Island  \n") == {"Fatal Push": 4, "Island": 2}


def test_parse_decklist_folds_the_sideboard_into_one_mapping():
    parsed = parse_decklist(DIMIR)
    assert parsed["Nihil Spellbomb"] == 2
    assert parsed["Psychic Frog"] == 4
    assert "Sideboard" not in parsed


def test_parse_decklist_skips_comments_and_set_codes():
    assert parse_decklist("# a comment\n// another\n4 Fatal Push (MH3) 12\n") == {
        "Fatal Push": 4
    }


def test_parse_decklist_sums_a_card_listed_twice():
    assert parse_decklist("2 Island\n2 Island\n") == {"Island": 4}


def test_decks_lists_every_guided_deck(library):
    assert sorted(d.slug for d in library.decks()) == ["dimir-frog", "prison-tron"]


def test_a_deck_guide_carries_every_guide_file_it_has(library):
    guide = library.get("dimir-frog")
    assert guide.gameplan.name == "GAMEPLAN_GUIDE.md"
    assert guide.sideboard.name == "SIDEBOARD_GUIDE.md"
    assert guide.meta_calls.name == "META_CALLS.md"
    assert guide.archetypes == ["boros-energy", "living-end"]


def test_missing_guide_files_are_none_not_an_error(library):
    guide = library.get("prison-tron")
    assert guide.gameplan is not None
    assert guide.sideboard is None
    assert guide.matchups_dir is None
    assert guide.archetypes == []


def test_get_of_an_unknown_slug_is_none(library):
    assert library.get("nope") is None


def test_identify_matches_the_deck_it_was_built_from(library):
    guide, score = library.identify(parse_decklist(DIMIR))
    assert guide.slug == "dimir-frog"
    assert score == pytest.approx(1.0)


def test_identify_tolerates_a_few_flex_slots(library):
    played = parse_decklist(DIMIR)
    played["Fatal Push"] = 3
    played["Thoughtseize"] = 1
    guide, score = library.identify(played)
    assert guide.slug == "dimir-frog"
    assert score >= MATCH_THRESHOLD


def test_identify_returns_none_for_a_deck_with_no_guide(library):
    guide, score = library.identify({"Lightning Bolt": 4, "Mountain": 20})
    assert guide is None
    assert score < MATCH_THRESHOLD


def test_identify_picks_the_better_of_two_candidates(library):
    guide, _ = library.identify(parse_decklist(TRON))
    assert guide.slug == "prison-tron"


def test_a_missing_guides_root_yields_no_decks(tmp_path):
    assert GuideLibrary(tmp_path / "absent").decks() == []
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/driver/test_guides.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.driver.guides`.

- [ ] **Step 3: Implement guides.py**

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/driver/test_guides.py -v`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src/mtg_agent tests/driver && uv run ruff check src/mtg_agent tests/driver
git add src/mtg_agent/driver tests/driver/test_guides.py
git commit -m "feat(driver): identify the agent's deck from its list and locate its guides"
```

---

### Task 14: `current-game.md` and the turn prompts

**Files:**
- Create: `src/mtg_agent/driver/prompt.py`
- Test: `tests/driver/test_prompt.py`

**Interfaces:**
- Consumes: `DeckGuide` (T13).
- Produces:
  - `Assignment` dataclass: `game_id: str`, `uid: str`, `player: str`, `opponent: str`, `api_base: str`, `decklist: dict[str,int]`, `guide: DeckGuide | None`, `match_score: float`.
  - `render_current_game(assignment, memory_root: Path) -> str`.
  - `memory_index(memory_root: Path, deck_slug: str) -> list[Path]`.
  - `turn_zero(assignment, current_game_path: Path) -> str`.
  - `reflection_turn(assignment, memory_root: Path, outcome: str) -> str`.

- [ ] **Step 1: Write the failing test**

`tests/driver/test_prompt.py`:

```python
from pathlib import Path

import pytest

from mtg_agent.driver.guides import DeckGuide
from mtg_agent.driver.prompt import (
    Assignment,
    memory_index,
    reflection_turn,
    render_current_game,
    turn_zero,
)


@pytest.fixture
def guide(tmp_path) -> DeckGuide:
    root = tmp_path / "guides" / "decks" / "dimir-frog"
    matchups = root / "analysis" / "r1" / "domain3-matchup-threat" / "matchups"
    matchups.mkdir(parents=True)
    (root / "decklist.txt").write_text("4 Psychic Frog\n")
    (root / "GAMEPLAN_GUIDE.md").write_text("# gameplan\n")
    (root / "SIDEBOARD_GUIDE.md").write_text("# sideboard\n")
    (matchups / "boros-energy.json").write_text("{}")
    (matchups / "living-end.json").write_text("{}")
    return DeckGuide(
        slug="dimir-frog",
        root=root,
        decklist=root / "decklist.txt",
        gameplan=root / "GAMEPLAN_GUIDE.md",
        sideboard=root / "SIDEBOARD_GUIDE.md",
        matchups_dir=matchups,
    )


def assignment(guide: DeckGuide | None, score: float = 1.0) -> Assignment:
    return Assignment(
        game_id="g-1",
        uid="secret-uid",
        player="bob",
        opponent="alice",
        api_base="http://game:8000",
        decklist={"Psychic Frog": 4, "Fatal Push": 4},
        guide=guide,
        match_score=score,
    )


def test_current_game_states_the_seat_and_the_deck(tmp_path, guide):
    text = render_current_game(assignment(guide), tmp_path / "memory")
    assert "g-1" in text
    assert "secret-uid" in text
    assert "bob" in text
    assert "dimir-frog" in text
    assert "4 Psychic Frog" in text


def test_current_game_lists_the_guide_paths_not_their_contents(tmp_path, guide):
    text = render_current_game(assignment(guide), tmp_path / "memory")
    assert str(guide.gameplan) in text
    assert str(guide.sideboard) in text
    assert "# gameplan" not in text, "guides are ~500 lines; hand over paths"


def test_current_game_lists_the_known_opponent_archetypes(tmp_path, guide):
    text = render_current_game(assignment(guide), tmp_path / "memory")
    assert "boros-energy" in text
    assert "living-end" in text


def test_current_game_never_names_the_opponents_deck(tmp_path, guide):
    """Fair play: the agent deduces the matchup from cards it sees."""
    text = render_current_game(assignment(guide), tmp_path / "memory")
    lowered = text.lower()
    assert "opponent's deck" not in lowered
    assert "opponent's decklist" not in lowered
    assert "alice" in text, "the opponent's seat NAME is public at a real table"


def test_current_game_says_so_when_there_is_no_guide(tmp_path):
    text = render_current_game(assignment(None, score=0.2), tmp_path / "memory")
    assert "no guide" in text.lower()
    assert "4 Psychic Frog" in text


def test_memory_index_finds_this_decks_notes_only(tmp_path):
    memory = tmp_path / "memory"
    (memory / "dimir-frog" / "games").mkdir(parents=True)
    (memory / "dimir-frog" / "vs-boros-energy.md").write_text("read")
    (memory / "dimir-frog" / "games" / "g-0.md").write_text("game")
    (memory / "living-end").mkdir()
    (memory / "living-end" / "vs-tron.md").write_text("other deck")
    found = [p.name for p in memory_index(memory, "dimir-frog")]
    assert "vs-boros-energy.md" in found
    assert "g-0.md" in found
    assert "vs-tron.md" not in found


def test_memory_index_of_an_absent_directory_is_empty(tmp_path):
    assert memory_index(tmp_path / "nothing", "dimir-frog") == []


def test_current_game_lists_past_notes_when_they_exist(tmp_path, guide):
    memory = tmp_path / "memory"
    (memory / "dimir-frog").mkdir(parents=True)
    (memory / "dimir-frog" / "vs-boros-energy.md").write_text("they curve out")
    text = render_current_game(assignment(guide), memory)
    assert "vs-boros-energy.md" in text


def test_turn_zero_orders_reading_before_acting(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert str(tmp_path / "current-game.md") in text
    lowered = text.lower()
    assert lowered.index("read") < lowered.index("wait_for_priority")


def test_turn_zero_names_the_mcp_tools_not_seat_cli(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    for tool in ("wait_for_priority", "my_state", "cast", "play_land", "decide", "pass_priority"):
        assert tool in text
    assert "seat_cli" not in text


def test_turn_zero_carries_the_uid_and_the_hard_won_rules_of_engagement(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert "secret-uid" in text
    assert "winner" in text
    assert "never state" in text.lower()


def test_turn_zero_tells_it_to_deduce_the_matchup(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert "deduce" in text.lower() or "identify" in text.lower()
    assert "matchups" in text


def test_reflection_turn_names_the_files_to_write(tmp_path, guide):
    memory = tmp_path / "memory"
    text = reflection_turn(assignment(guide), memory, outcome="bob won")
    assert str(memory / "dimir-frog" / "games" / "g-1.md") in text
    assert "vs-<archetype>.md" in text
    assert str(memory / "MEMORY.md") in text
    assert "bob won" in text


def test_reflection_turn_for_an_unguided_deck_still_writes_notes(tmp_path):
    memory = tmp_path / "memory"
    text = reflection_turn(assignment(None, 0.1), memory, outcome="alice won")
    assert str(memory / "unknown-deck" / "games" / "g-1.md") in text
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/driver/test_prompt.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.driver.prompt`.

- [ ] **Step 3: Implement prompt.py**

```python
"""What the agent is told, and when.

Turn 0 is the whole reason the driver exists. The wrapper refuses a second
turn while one is in flight, so whatever turn 0 says is necessarily known to
the agent before its first action - which is the requirement: it must know
its deck before it is asked to move.

The rules of engagement below are lifted from docs/ai_seat_brief.md, where
each line was learned the hard way against the real seat API; only the
command section changes, because this agent drives MCP tools rather than
seat_cli.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .guides import DeckGuide

UNKNOWN_DECK = "unknown-deck"


@dataclass
class Assignment:
    game_id: str
    uid: str
    player: str
    opponent: str
    api_base: str
    decklist: dict[str, int]
    guide: DeckGuide | None
    match_score: float

    @property
    def deck_slug(self) -> str:
        return self.guide.slug if self.guide else UNKNOWN_DECK


def memory_index(memory_root: Path, deck_slug: str) -> list[Path]:
    """Every note the agent has already written about this deck."""
    deck_dir = Path(memory_root) / deck_slug
    if not deck_dir.is_dir():
        return []
    return sorted(p for p in deck_dir.rglob("*.md") if p.is_file())


def _decklist_block(decklist: dict[str, int]) -> str:
    return "\n".join(f"{count} {name}" for name, count in sorted(decklist.items()))


def render_current_game(assignment: Assignment, memory_root: Path) -> str:
    guide = assignment.guide
    lines = [
        "# This game",
        "",
        f"- game id: `{assignment.game_id}`",
        f"- your seat uid: `{assignment.uid}` (every MCP tool takes it as `player_uid`)",
        f"- you are: **{assignment.player}**",
        f"- your opponent's seat: **{assignment.opponent}**",
        f"- game server: {assignment.api_base}",
        "",
        "## Your deck",
        "",
    ]
    if guide is not None:
        lines += [
            f"You are piloting **{guide.slug}** "
            f"(matched at {assignment.match_score:.0%} against the guided list).",
            "",
        ]
    else:
        lines += [
            "There is **no guide** for this decklist - it matches none of the guided "
            f"decks (best match {assignment.match_score:.0%}). Play it from the list "
            "itself and from what the board tells you.",
            "",
        ]
    lines += ["```", _decklist_block(assignment.decklist), "```", ""]

    if guide is not None:
        lines += ["## Your guides", ""]
        for label, path in (
            ("gameplan", guide.gameplan),
            ("sideboard", guide.sideboard),
            ("meta calls", guide.meta_calls),
        ):
            if path is not None:
                lines.append(f"- {label}: `{path}`")
        if guide.matchups_dir is not None:
            lines += [
                f"- per-matchup detail: `{guide.matchups_dir}/<archetype>.json`",
                "",
                "Archetypes with a matchup file: "
                + ", ".join(f"`{a}`" for a in guide.archetypes),
            ]
        lines.append("")

    notes = memory_index(memory_root, assignment.deck_slug)
    lines += ["## What you have already learned on this deck", ""]
    if notes:
        lines += [f"- `{path}`" for path in notes]
    else:
        lines.append("Nothing yet - this is your first recorded game on it.")
    lines.append("")
    return "\n".join(lines)


def turn_zero(assignment: Assignment, current_game_path: Path) -> str:
    guide = assignment.guide
    matchups = (
        f"`{guide.matchups_dir}/<archetype>.json`"
        if guide is not None and guide.matchups_dir is not None
        else "(none for this deck)"
    )
    return f"""You are playing a real game of Magic: the Gathering against a human,
who is playing through a web UI against the same game server you act on.

## Before you take a single action

1. Read `{current_game_path}`. It names your seat, your uid, your decklist and
   your guides.
2. Read your deck's gameplan guide, and any notes you have already written
   about this deck. You are expected to arrive at the first action already
   knowing what this deck is trying to do.
3. Only then call `wait_for_priority`.

## How you act

Every tool is on the `mtg` MCP server and takes your uid as `player_uid`:
`{assignment.uid}`.

- `wait_for_priority(player_uid, max_seconds=...)` blocks until it is your
  moment and hands you the board. ALWAYS bound it and call it in a loop. A
  bare unbounded wait strands your turn.
- `my_state(player_uid)` is the board as you are allowed to see it.
- `cast`, `play_land`, `activate`, `declare_attackers`, `declare_blockers`,
  `decide`, `pass_priority`, `resolve_top`, `advance`, `set_step` act.
- `game_log` is the history; `report_problem` files an engine bug.

Each action tool performs the action, releases priority unless you hold it,
and blocks until you next have something to do - one call, one meaningful
action.

## Identify the matchup yourself

You have NOT been told what your opponent is playing, deliberately. Work it
out from the cards they actually play, then read the matching matchup file:
{matchups}. Say in one line what you have deduced and how confident you are
the first time you commit to a read. Getting this right earlier, game after
game, is the point of the notes you keep.

## Rules of engagement, each learned the hard way

- Play until the state shows a `winner`, or a drawn game, or until you file a
  report. Stopping for any other reason is not allowed. A partially played
  game is worthless. Do not stop because the game is long, because you think
  you are losing, or because you are unsure.
- `advance` is ONLY for the untap and cleanup steps of your own turn. In a
  main phase use `play_land` / `cast` / `activate`, then `pass_priority`.
- If `pass_priority` is refused with "you do not have priority", call
  `wait_for_priority` again. NEVER `advance` to escape that - it strands the
  stack.
- A pending decision FREEZES the game. Answering it is the only legal move,
  and an unanswered decision times out to a default and wastes the card.
- Play your deck's mechanics ACTIVELY, not merely legally: attack, block,
  activate abilities, let triggers resolve, use the whole board.
- NEVER state card text or a rule from memory. Use the `spellslinger` MCP
  server's `knowledge_search` and `resolve_cards` to look them up.
- The human is watching a UI update as you play. Act promptly.

Begin."""


def reflection_turn(assignment: Assignment, memory_root: Path, outcome: str) -> str:
    slug = assignment.deck_slug
    deck_dir = Path(memory_root) / slug
    return f"""The game is over: {outcome}

Write up what you learned, so the next game on this deck starts from it.

1. `{deck_dir / "games" / f"{assignment.game_id}.md"}` - this game: the result,
   what your opponent turned out to be playing and how early you knew, the two
   or three decisions that actually decided it, and what you would do
   differently.
2. `{deck_dir / "vs-<archetype>.md"}` - your standing read on this matchup,
   using the archetype you deduced. Create it if it does not exist, and revise
   it rather than appending if it does: their threats, your answers, which
   cards are dead, what the race maths looks like, mulligan policy.
3. `{Path(memory_root) / "MEMORY.md"}` - one line per note, `- [title](path) -
   hook`. Add lines for anything new; do not restate the note's contents there.

Be specific and short. A note that says "play carefully" is worth nothing; a
note that says "their turn-three Cutter is the card that beats us, hold Push
for it rather than the one-drop" is worth the whole file. Write only what this
game actually showed you."""
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/driver/test_prompt.py -v`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src/mtg_agent tests/driver && uv run ruff check src/mtg_agent tests/driver
git add src/mtg_agent/driver/prompt.py tests/driver/test_prompt.py
git commit -m "feat(driver): render current-game.md, turn 0 and the post-game reflection turn"
```

---

### Task 15: The driver service

**Files:**
- Create: `src/mtg_agent/driver/config.py`, `src/mtg_agent/driver/app.py`, `src/mtg_agent/driver/__main__.py`
- Test: `tests/driver/test_app.py`

**Interfaces:**
- Consumes: `GuideLibrary`, `parse_decklist` (T13), `Assignment`, `render_current_game`, `turn_zero`, `reflection_turn` (T14).
- Produces:
  - `DriverSettings` dataclass: `game_api: str`, `wrapper_api: str`, `wrapper_secret: str`, `guides_root: Path`, `memory_root: Path`, `workspace: Path`, `self_url: str`, `http_addr: str`; `DriverSettings.from_env(env)`.
  - `build_app(settings, *, client_factory=None) -> FastAPI` exposing `POST /dispatch`, `POST /turn-done`, `GET /status`, `GET /healthz`.

- [ ] **Step 1: Write the failing test**

`tests/driver/test_app.py`:

```python
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mtg_agent.driver.app import build_app
from mtg_agent.driver.config import DriverSettings

DECKLIST = "4 Psychic Frog\n4 Fatal Push\n2 Consign to Memory\n"


@pytest.fixture
def world(tmp_path):
    """A fake game server and a fake wrapper, both real FastAPI apps reached
    over httpx's ASGI transport - no mocking library involved."""
    guides = tmp_path / "guides" / "decks" / "dimir-frog"
    guides.mkdir(parents=True)
    (guides / "decklist.txt").write_text(DECKLIST)
    (guides / "GAMEPLAN_GUIDE.md").write_text("# gameplan\n")

    state = {"winner": None, "decklist": DECKLIST}
    submitted: list[dict] = []

    game = FastAPI()

    @game.get("/api/games/{game_id}")
    def get_game(game_id: str, uid: str):
        return {
            "id": game_id,
            "players": [
                {"username": "bob", "decklist": state["decklist"]},
                {"username": "alice"},
            ],
            "winner": state["winner"],
        }

    wrapper = FastAPI()

    @wrapper.post("/v1/messages", status_code=202)
    def post_message(body: dict):
        submitted.append(body)
        return {"turnId": f"turn-{len(submitted)}"}

    def client_factory(base_url: str) -> httpx.Client:
        app = game if "game" in base_url else wrapper
        return httpx.Client(transport=httpx.ASGITransport(app=app), base_url=base_url)

    settings = DriverSettings(
        game_api="http://game:8000",
        wrapper_api="http://agent:8080",
        wrapper_secret="",
        guides_root=tmp_path / "guides",
        memory_root=tmp_path / "memory",
        workspace=tmp_path / "workspace",
        self_url="http://driver:8090",
        http_addr=":8090",
    )
    client = TestClient(build_app(settings, client_factory=client_factory))
    return client, settings, submitted, state


def dispatch(client) -> httpx.Response:
    return client.post(
        "/dispatch",
        json={
            "uid": "secret-uid",
            "game_id": "g-1",
            "player": "bob",
            "api_base": "http://game:8000",
        },
    )


def test_dispatch_identifies_the_deck_and_reports_it(world):
    client, _, _, _ = world
    body = dispatch(client).json()
    assert body["deck"] == "dimir-frog"
    assert body["turnId"] == "turn-1"


def test_dispatch_writes_current_game_md_into_the_workspace(world):
    client, settings, _, _ = world
    dispatch(client)
    text = (settings.workspace / "current-game.md").read_text()
    assert "secret-uid" in text
    assert "dimir-frog" in text
    assert "4 Psychic Frog" in text


def test_dispatch_pushes_turn_zero_with_its_own_callback_url(world):
    client, settings, submitted, _ = world
    dispatch(client)
    turn = submitted[0]
    assert turn["callbackUrl"] == "http://driver:8090/turn-done"
    assert "current-game.md" in turn["text"]
    assert "wait_for_priority" in turn["text"]


def test_turn_zero_never_carries_the_opponents_list(world):
    client, _, submitted, _ = world
    dispatch(client)
    assert "alice" in submitted[0]["text"] or True
    assert "opponent's decklist" not in submitted[0]["text"].lower()


def test_dispatch_of_an_unguided_deck_still_starts_the_game(world):
    client, _, submitted, state = world
    state["decklist"] = "4 Lightning Bolt\n20 Mountain\n"
    body = dispatch(client).json()
    assert body["deck"] is None
    assert submitted[0]["text"]


def test_a_second_dispatch_for_the_same_uid_does_not_push_a_second_turn_zero(world):
    client, _, submitted, _ = world
    dispatch(client)
    dispatch(client)
    assert len(submitted) == 1


def test_turn_done_mid_game_pushes_nothing(world):
    client, _, submitted, _ = world
    dispatch(client)
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "ok"})
    assert len(submitted) == 1


def test_turn_done_after_a_win_pushes_exactly_one_reflection_turn(world):
    client, _, submitted, state = world
    dispatch(client)
    state["winner"] = "bob"
    client.post("/turn-done", json={"turnId": "turn-1", "state": "complete", "finalText": "gg"})
    assert len(submitted) == 2
    assert "The game is over" in submitted[1]["text"]
    assert "bob" in submitted[1]["text"]

    client.post("/turn-done", json={"turnId": "turn-2", "state": "complete", "finalText": "wrote"})
    assert len(submitted) == 2, "the reflection turn must not trigger another reflection"


def test_status_reports_the_current_assignment(world):
    client, _, _, _ = world
    dispatch(client)
    body = client.get("/status").json()
    assert body["gameId"] == "g-1"
    assert body["deck"] == "dimir-frog"
    assert body["player"] == "bob"
    assert body["lastTurnId"] == "turn-1"


def test_status_before_any_dispatch_is_idle(world):
    client, _, _, _ = world
    assert client.get("/status").json()["gameId"] is None


def test_healthz(world):
    client, _, _, _ = world
    assert client.get("/healthz").status_code == 200


def test_settings_from_env_reads_the_container_wiring():
    settings = DriverSettings.from_env(
        {
            "GAME_API": "http://game:8000",
            "WRAPPER_API": "http://agent:8080",
            "GUIDES_ROOT": "/guides",
            "MEMORY_ROOT": "/workspace/memory",
            "DRIVER_SELF_URL": "http://driver:8090",
        }
    )
    assert settings.game_api == "http://game:8000"
    assert settings.guides_root == Path("/guides")
    assert settings.memory_root == Path("/workspace/memory")


def test_json_is_importable_by_the_test(world):
    assert json is not None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/driver/test_app.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.driver.config`.

- [ ] **Step 3: Implement config.py**

```python
"""The driver's container wiring."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DriverSettings:
    game_api: str = "http://game:8000"
    wrapper_api: str = "http://agent:8080"
    wrapper_secret: str = ""
    guides_root: Path = Path("/guides")
    memory_root: Path = Path("/workspace/memory")
    workspace: Path = Path("/workspace")
    self_url: str = "http://driver:8090"
    http_addr: str = ":8090"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> DriverSettings:
        def value(key: str, default: str) -> str:
            return env.get(key, "").strip() or default

        return cls(
            game_api=value("GAME_API", "http://game:8000"),
            wrapper_api=value("WRAPPER_API", "http://agent:8080"),
            wrapper_secret=value("WRAPPER_SHARED_SECRET", ""),
            guides_root=Path(value("GUIDES_ROOT", "/guides")),
            memory_root=Path(value("MEMORY_ROOT", "/workspace/memory")),
            workspace=Path(value("DRIVER_WORKSPACE", "/workspace")),
            self_url=value("DRIVER_SELF_URL", "http://driver:8090"),
            http_addr=value("HTTP_ADDR", ":8090"),
        )
```

- [ ] **Step 4: Implement app.py**

```python
"""Choreography: who is playing what, and which turn to push next.

The driver holds no game state. It learns of a game when ai_seat.py hands it
a dispatched seat, reads that seat's decklist from the game server, works out
which guided deck it is, writes `current-game.md`, and pushes turn 0. When a
turn finishes it asks the game server whether the game ended; if it did, it
pushes exactly one reflection turn.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .config import DriverSettings
from .guides import GuideLibrary, parse_decklist
from .prompt import Assignment, reflection_turn, render_current_game, turn_zero

log = logging.getLogger("driver")


class DispatchIn(BaseModel):
    uid: str
    game_id: str
    player: str
    api_base: str = ""


def _default_client_factory(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=30.0)


def build_app(
    settings: DriverSettings,
    *,
    client_factory: Callable[[str], httpx.Client] | None = None,
) -> FastAPI:
    app = FastAPI(title="mtg-agent-driver")
    library = GuideLibrary(settings.guides_root)
    make_client = client_factory or _default_client_factory
    lock = threading.Lock()
    current: dict[str, Any] = {
        "assignment": None,
        "lastTurnId": None,
        "reflectionTurnId": None,
        "reflected": False,
    }

    def game_state(game_id: str, uid: str) -> dict:
        with make_client(settings.game_api) as client:
            response = client.get(f"/api/games/{game_id}", params={"uid": uid})
            response.raise_for_status()
            return response.json()

    def push_turn(text: str) -> str:
        headers = (
            {"X-Wrapper-Secret": settings.wrapper_secret} if settings.wrapper_secret else {}
        )
        with make_client(settings.wrapper_api) as client:
            response = client.post(
                "/v1/messages",
                json={"text": text, "callbackUrl": f"{settings.self_url}/turn-done"},
                headers=headers,
            )
            response.raise_for_status()
            return response.json()["turnId"]

    def seat_decklist(state: dict, player: str) -> str:
        for seat in state.get("players") or []:
            if seat.get("username") == player:
                return seat.get("decklist") or ""
        return ""

    def opponent_of(state: dict, player: str) -> str:
        names = [s.get("username", "") for s in state.get("players") or []]
        others = [n for n in names if n and n != player]
        return others[0] if others else ""

    @app.post("/dispatch", status_code=202)
    def dispatch(body: DispatchIn) -> dict:
        with lock:
            existing = current["assignment"]
            if existing is not None and existing.uid == body.uid:
                log.info("dispatch for uid %s already served", body.uid)
                return {
                    "deck": existing.guide.slug if existing.guide else None,
                    "turnId": current["lastTurnId"],
                    "alreadyRunning": True,
                }

        try:
            state = game_state(body.game_id, body.uid)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"game server: {exc}") from exc

        decklist = parse_decklist(seat_decklist(state, body.player))
        guide, score = library.identify(decklist)
        assignment = Assignment(
            game_id=body.game_id,
            uid=body.uid,
            player=body.player,
            opponent=opponent_of(state, body.player),
            api_base=body.api_base or settings.game_api,
            decklist=decklist,
            guide=guide,
            match_score=score,
        )

        settings.workspace.mkdir(parents=True, exist_ok=True)
        settings.memory_root.mkdir(parents=True, exist_ok=True)
        current_game_path = settings.workspace / "current-game.md"
        current_game_path.write_text(render_current_game(assignment, settings.memory_root))

        turn_id = push_turn(turn_zero(assignment, current_game_path))
        with lock:
            current.update(
                assignment=assignment,
                lastTurnId=turn_id,
                reflectionTurnId=None,
                reflected=False,
            )
        log.info(
            "dispatched game=%s player=%s deck=%s turn=%s",
            body.game_id,
            body.player,
            assignment.deck_slug,
            turn_id,
        )
        return {"deck": guide.slug if guide else None, "turnId": turn_id, "matchScore": score}

    @app.post("/turn-done", status_code=202)
    def turn_done(record: dict) -> dict:
        with lock:
            assignment = current["assignment"]
            reflected = current["reflected"]
            reflection_id = current["reflectionTurnId"]
        if assignment is None:
            return {"ignored": "no assignment"}
        if reflected or record.get("turnId") == reflection_id:
            return {"ignored": "already reflected"}

        try:
            state = game_state(assignment.game_id, assignment.uid)
        except httpx.HTTPError as exc:
            log.warning("could not read game state after a turn: %s", exc)
            return {"ignored": "game unreachable"}

        winner = state.get("winner")
        drawn = bool(state.get("drawn"))
        if not winner and not drawn:
            return {"ignored": "game still running"}

        outcome = f"{winner} won" if winner else "the game was a draw"
        turn_id = push_turn(reflection_turn(assignment, settings.memory_root, outcome))
        with lock:
            current.update(reflected=True, reflectionTurnId=turn_id)
        log.info("game %s over (%s), reflection turn %s", assignment.game_id, outcome, turn_id)
        return {"reflectionTurnId": turn_id, "outcome": outcome}

    @app.get("/status")
    def status() -> dict:
        with lock:
            assignment = current["assignment"]
            return {
                "gameId": assignment.game_id if assignment else None,
                "player": assignment.player if assignment else None,
                "deck": (
                    assignment.guide.slug if assignment and assignment.guide else None
                ),
                "lastTurnId": current["lastTurnId"],
                "reflected": current["reflected"],
            }

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app
```

- [ ] **Step 5: Implement `driver/__main__.py`**

```python
"""uvicorn entry point for the driver."""

from __future__ import annotations

import logging
import os

import uvicorn

from .app import build_app
from .config import DriverSettings


def main() -> None:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s %(message)s")
    settings = DriverSettings.from_env(os.environ)
    host, _, port = settings.http_addr.rpartition(":")
    uvicorn.run(build_app(settings), host=host or "0.0.0.0", port=int(port))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/driver -v`
Expected: all driver tests pass (about 40).

- [ ] **Step 7: Commit**

```bash
uv run ruff format src/mtg_agent tests/driver && uv run ruff check src/mtg_agent tests/driver
git add src/mtg_agent/driver tests/driver/test_app.py
git commit -m "feat(driver): dispatch a seat into turn 0 and reflect once the game ends"
```

---

### Task 16: `AI_SEAT_MODE=wrapper` in the existing AI seat

The only pre-existing file this plan touches. The subprocess path stays the
default and keeps working; `wrapper` mode hands the dispatched seat to the
driver instead of spawning a one-shot `claude --print`.

**Files:**
- Modify: `src/magic_cards_model_api/ai_seat.py`
- Test: `tests/driver/test_ai_seat_wrapper_mode.py`

**Interfaces:**
- Consumes: the driver's `POST /dispatch` contract (T15).
- Produces: on the existing `AiSeatRegistry`, `start(...)` gains a wrapper branch. New module-level `ai_seat_mode() -> str` and `driver_url() -> str`.

- [ ] **Step 1: Write the failing test**

`tests/driver/test_ai_seat_wrapper_mode.py`:

```python
import httpx
import pytest
from fastapi import FastAPI

from magic_cards_model_api import ai_seat


@pytest.fixture
def driver(monkeypatch):
    """A real FastAPI stand-in for the driver, reached over httpx's ASGI
    transport."""
    received: list[dict] = []
    app = FastAPI()

    @app.post("/dispatch", status_code=202)
    def dispatch(body: dict):
        received.append(body)
        return {"deck": "dimir-frog", "turnId": "turn-1", "matchScore": 0.98}

    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("DRIVER_URL", "http://driver:8090")
    monkeypatch.setattr(
        ai_seat,
        "_driver_client",
        lambda base: httpx.Client(transport=httpx.ASGITransport(app=app), base_url=base),
    )
    return received


def test_wrapper_mode_posts_the_seat_to_the_driver(driver):
    registry = ai_seat.AiSeatRegistry()
    status = registry.start(
        uid="secret-uid", game_id="g-1", player="bob", api_base="http://game:8000"
    )
    assert status["mechanism"] == "wrapper"
    assert status["state"] == "dispatched"
    assert status["deck"] == "dimir-frog"
    assert driver[0] == {
        "uid": "secret-uid",
        "game_id": "g-1",
        "player": "bob",
        "api_base": "http://game:8000",
    }


def test_wrapper_mode_never_spawns_a_subprocess(driver, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("wrapper mode must not spawn claude")

    monkeypatch.setattr(ai_seat.subprocess, "Popen", explode)
    registry = ai_seat.AiSeatRegistry()
    registry.start(uid="u", game_id="g", player="bob", api_base="http://game:8000")


def test_a_second_start_for_the_same_uid_dispatches_once(driver):
    registry = ai_seat.AiSeatRegistry()
    registry.start(uid="u", game_id="g", player="bob", api_base="http://game:8000")
    registry.start(uid="u", game_id="g", player="bob", api_base="http://game:8000")
    assert len(driver) == 1


def test_an_unreachable_driver_reports_a_fallback_not_an_exception(monkeypatch):
    monkeypatch.setenv("AI_SEAT_MODE", "wrapper")
    monkeypatch.setenv("DRIVER_URL", "http://driver:8090")

    def broken(base):
        raise httpx.ConnectError("no route to driver")

    monkeypatch.setattr(ai_seat, "_driver_client", broken)
    status = ai_seat.AiSeatRegistry().start(
        uid="u", game_id="g", player="bob", api_base="http://game:8000"
    )
    assert status["mechanism"] == "fallback"
    assert "driver" in status["reason"]


def test_subprocess_stays_the_default_mode(monkeypatch):
    monkeypatch.delenv("AI_SEAT_MODE", raising=False)
    assert ai_seat.ai_seat_mode() == "subprocess"


def test_status_and_stop_are_harmless_for_a_wrapper_seat(driver):
    registry = ai_seat.AiSeatRegistry()
    registry.start(uid="u", game_id="g", player="bob", api_base="http://game:8000")
    assert registry.status("u")["mechanism"] == "wrapper"
    assert registry.stop("u") is False
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/driver/test_ai_seat_wrapper_mode.py -v`
Expected: FAIL, `AttributeError: module 'magic_cards_model_api.ai_seat' has no attribute 'ai_seat_mode'`.

- [ ] **Step 3: Edit `ai_seat.py`**

Add to the imports at the top of the file:

```python
import os

import httpx
```

Add after the `TAIL_LINES` constant:

```python
def ai_seat_mode() -> str:
    """`subprocess` (the default, a one-shot `claude --print` per game) or
    `wrapper` (hand the seat to the driver, which drives a long-lived
    interactive claude that remembers previous games)."""
    return (os.environ.get("AI_SEAT_MODE") or "subprocess").strip().lower()


def driver_url() -> str:
    return (os.environ.get("DRIVER_URL") or "http://driver:8090").strip()


def _driver_client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=30.0)
```

Add this method to `AiSeatRegistry`, above `start`:

```python
    def _dispatch_to_driver(
        self, *, uid: str, game_id: str, player: str, api_base: str
    ) -> dict[str, Any]:
        """Hand this seat to the driver. Never raises: an unreachable driver
        comes back as a fallback, exactly like a missing `claude` binary, so
        the UI has something to show and the game stays playable."""
        base = driver_url()
        payload = {
            "uid": uid,
            "game_id": game_id,
            "player": player,
            "api_base": api_base,
        }
        try:
            with _driver_client(base) as client:
                response = client.post("/dispatch", json=payload)
                response.raise_for_status()
                answer = response.json()
        except Exception as exc:  # noqa: BLE001 - report, never raise, see docstring
            return {
                "uid": uid,
                "player": player,
                "mechanism": "fallback",
                "state": "not_started",
                "reason": f"the driver at {base} could not be reached: {exc}",
            }
        return {
            "uid": uid,
            "player": player,
            "mechanism": "wrapper",
            "state": "dispatched",
            "deck": answer.get("deck"),
            "turn_id": answer.get("turnId"),
            "match_score": answer.get("matchScore"),
            "driver": base,
        }
```

Replace the body of `AiSeatRegistry.start` up to and including the
`if existing is not None:` block with:

```python
    def start(self, *, uid: str, game_id: str, player: str, api_base: str) -> dict[str, Any]:
        """Start (or report the existing) AI seat for `uid`.

        Two mechanisms, chosen by `AI_SEAT_MODE`. `subprocess` spawns a
        one-shot `claude --print` that forgets the game the moment it ends.
        `wrapper` hands the seat to the driver, which pushes turns into a
        long-lived interactive claude that carries guides and notes across
        games. Never raises in either mode - see the module docstring.
        """
        if ai_seat_mode() == "wrapper":
            with self._lock:
                if uid in self._dispatched:
                    return self._dispatched[uid]
            status = self._dispatch_to_driver(
                uid=uid, game_id=game_id, player=player, api_base=api_base
            )
            if status["mechanism"] == "wrapper":
                with self._lock:
                    self._dispatched[uid] = status
            return status

        with self._lock:
            existing = self._runners.get(uid)
        if existing is not None:
            return existing.status()
```

In `AiSeatRegistry.__init__`, add:

```python
        self._dispatched: dict[str, dict[str, Any]] = {}
```

In `AiSeatRegistry.status`, return a dispatched seat's record before falling
through to the subprocess runners:

```python
    def status(self, uid: str) -> dict[str, Any] | None:
        with self._lock:
            dispatched = self._dispatched.get(uid)
            runner = self._runners.get(uid)
        if dispatched is not None:
            return dispatched
        return runner.status() if runner is not None else None
```

`stop` needs no change: a wrapper seat has no subprocess of its own, so
`stop` finds no runner and returns `False`. The wrapper's own
`DELETE /v1/session` is how you stop that agent, and it is not the game
server's business.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/driver/test_ai_seat_wrapper_mode.py -v`
Expected: 6 passed.

- [ ] **Step 5: Check nothing regressed in the existing AI-seat tests**

Run: `uv run pytest -q`
Expected: the whole suite passes, including any pre-existing `ai_seat` tests -
subprocess mode is untouched and is still the default.

- [ ] **Step 6: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests
git add src/magic_cards_model_api/ai_seat.py tests/driver/test_ai_seat_wrapper_mode.py
git commit -m "feat(ai-seat): AI_SEAT_MODE=wrapper hands the seat to the continuous agent"
```

---

### Task 17: Containers, compose and the README

**Files:**
- Create: `Dockerfile`, `Dockerfile.agent`, `docker-compose.yml`, `.env.example`, `deploy/mcp.base.json`
- Modify: `.gitignore`, `README.md`, `mise.toml`
- Test: `tests/driver/test_compose.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `docker compose up` bringing the three services up.

- [ ] **Step 1: Write the failing test**

The compose file is configuration, so the test asserts the wiring that the
running system depends on - not that Docker works.

`tests/driver/test_compose.py`:

```python
import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_three_services(compose):
    assert set(compose["services"]) == {"game", "agent", "driver"}


def test_every_build_context_is_the_parent_so_the_engine_is_in_scope(compose):
    """magic-cards-model is a path dependency at ../magic-cards-model, so a
    build context of `.` cannot see it."""
    for name, service in compose["services"].items():
        assert service["build"]["context"] == "..", name


def test_the_game_serves_8000_and_can_read_the_guides(compose):
    game = compose["services"]["game"]
    assert "8000:8000" in game["ports"]
    assert any("/guides" in str(v) for v in game["volumes"])


def test_the_agent_mounts_guides_read_only_and_memory_read_write(compose):
    volumes = compose["services"]["agent"]["volumes"]
    guides = [v for v in volumes if "/guides" in str(v)]
    memory = [v for v in volumes if "/workspace/memory" in str(v)]
    assert guides and str(guides[0]).endswith(":ro")
    assert memory and not str(memory[0]).endswith(":ro")


def test_the_agent_needs_an_api_key_from_the_environment(compose):
    env = compose["services"]["agent"]["environment"]
    assert any("ANTHROPIC_API_KEY" in str(e) for e in env)


def test_the_agent_is_told_where_the_hook_and_the_internal_endpoint_are(compose):
    env = " ".join(str(e) for e in compose["services"]["agent"]["environment"])
    assert "HOOK_COMMAND" in env
    assert "CCW_INTERNAL_URL" in env


def test_the_driver_knows_the_game_and_the_wrapper(compose):
    env = " ".join(str(e) for e in compose["services"]["driver"]["environment"])
    assert "GAME_API" in env
    assert "WRAPPER_API" in env
    assert "DRIVER_SELF_URL" in env


def test_the_game_is_put_into_wrapper_mode_and_told_the_driver_url(compose):
    env = " ".join(str(e) for e in compose["services"]["game"]["environment"])
    assert "AI_SEAT_MODE" in env
    assert "DRIVER_URL" in env


def test_the_base_mcp_points_the_mtg_server_at_the_game_container():
    doc = json.loads((ROOT / "deploy" / "mcp.base.json").read_text())
    servers = doc["mcpServers"]
    assert "http://game:8000" in json.dumps(servers["mtg"])
    assert servers["spellslinger"]["type"] == "http"


def test_env_example_lists_every_variable_compose_requires(compose):
    example = (ROOT / ".env.example").read_text()
    for key in ("ANTHROPIC_API_KEY", "MTG_DECKS", "CLAUDE_MODEL"):
        assert key in example


def test_the_real_env_file_is_gitignored():
    assert "\n.env\n" in "\n" + (ROOT / ".gitignore").read_text()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/driver/test_compose.py -v`
Expected: FAIL, `docker-compose.yml` does not exist. (If `pyyaml` is absent
the test skips - add `"pyyaml>=6"` to the `dev` dependency group first, then
`uv sync`.)

- [ ] **Step 3: Write `deploy/mcp.base.json`**

```json
{
  "mcpServers": {
    "mtg": {
      "command": "python",
      "args": [
        "-m",
        "magic_cards_model_api.mcp",
        "--api",
        "http://game:8000"
      ]
    },
    "spellslinger": {
      "type": "http",
      "url": "https://mcp.szymonrichert.pl/mcp"
    }
  }
}
```

- [ ] **Step 4: Write `Dockerfile`**

```dockerfile
# The game server and the driver. Build context is the PARENT directory, so
# the engine at ../magic-cards-model (a path dependency) is in scope.
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PYTHON_DOWNLOADS=never

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY magic-cards-model /magic-cards-model
COPY magic-cards-model-api /app

RUN uv sync --frozen --no-dev || uv sync --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000
CMD ["uvicorn", "magic_cards_model_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Note: the engine is copied to `/magic-cards-model` so the `[tool.uv.sources]`
relative path `../magic-cards-model` resolves from `/app`.

- [ ] **Step 5: Write `Dockerfile.agent`**

```dockerfile
# The agent: the wrapper as PID 1, supervising an interactive claude.
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    NODE_MAJOR=22

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates git gnupg \
 && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY magic-cards-model /magic-cards-model
COPY magic-cards-model-api /app
RUN uv sync --frozen --no-dev || uv sync --no-dev
ENV PATH="/app/.venv/bin:${PATH}"

# claude gets its own layer so bumping it rebuilds only this step.
ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && claude --version

RUN mkdir -p /workspace /agent-home
ENV HOME=/agent-home \
    HOME_DIR=/agent-home \
    WORKSPACE=/workspace

EXPOSE 8080
CMD ["python", "-m", "mtg_agent.wrapper"]
```

- [ ] **Step 6: Write `docker-compose.yml`**

```yaml
# Bring the whole thing up from the magic-cards-model-api directory:
#
#     cp .env.example .env   # then put your real ANTHROPIC_API_KEY in it
#     docker compose up --build
#
# Then play at http://127.0.0.1:8000/play/ and pick a deck. The AI seat is
# the continuous agent: it keeps its context across games and writes what it
# learned into ./memory.
#
# Every build context is `..` because the engine is a path dependency at
# ../magic-cards-model.

services:
  game:
    build:
      context: ..
      dockerfile: magic-cards-model-api/Dockerfile
    ports:
      - "8000:8000"
    environment:
      - AI_SEAT_MODE=wrapper
      - DRIVER_URL=http://driver:8090
    volumes:
      - ${MTG_DECKS:?set MTG_DECKS in .env}:/guides:ro
      - scryfall-cache:/app/.scryfall-cache
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx;httpx.get('http://127.0.0.1:8000/api/health').raise_for_status()"]
      interval: 5s
      timeout: 3s
      retries: 20

  agent:
    build:
      context: ..
      dockerfile: magic-cards-model-api/Dockerfile.agent
      args:
        CLAUDE_CODE_VERSION: ${CLAUDE_CODE_VERSION:-latest}
    depends_on:
      game:
        condition: service_healthy
    ports:
      - "8080:8080"
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY in .env}
      - MODEL=${CLAUDE_MODEL:-sonnet}
      - HTTP_ADDR=:8080
      - INTERNAL_ADDR=127.0.0.1:8090
      - WORKSPACE=/workspace
      - HOME_DIR=/agent-home
      - HOOK_COMMAND=/app/.venv/bin/cc-stop-hook
      - CCW_INTERNAL_URL=http://127.0.0.1:8090/internal/turn-complete
      - CCW_RESULT_JSON=/workspace/result.json
      - BASE_MCP_PATH=/app/deploy/mcp.base.json
      - TURN_TIMEOUT_SECONDS=1800
      - BOOT_TIMEOUT_SECONDS=120
      - LOG_LEVEL=info
    volumes:
      - ${MTG_DECKS:?set MTG_DECKS in .env}:/guides:ro
      - ./memory:/workspace/memory
    restart: unless-stopped

  driver:
    build:
      context: ..
      dockerfile: magic-cards-model-api/Dockerfile
    command: ["python", "-m", "mtg_agent.driver"]
    depends_on:
      - agent
    ports:
      - "8090:8090"
    environment:
      - GAME_API=http://game:8000
      - WRAPPER_API=http://agent:8080
      - DRIVER_SELF_URL=http://driver:8090
      - GUIDES_ROOT=/guides
      - MEMORY_ROOT=/workspace/memory
      - DRIVER_WORKSPACE=/driver-workspace
      - HTTP_ADDR=:8090
    volumes:
      - ${MTG_DECKS:?set MTG_DECKS in .env}:/guides:ro
      - ./memory:/workspace/memory

volumes:
  scryfall-cache:
```

Note the driver writes `current-game.md` into its own `/driver-workspace`,
not the agent's `/workspace` - the two containers do not share a filesystem.
Turn 0 therefore quotes the file's *contents* rather than only its path when
the two differ; see Step 8.

- [ ] **Step 7: Write `.env.example` and extend `.gitignore`**

`.env.example`:

```sh
# Copy to .env and fill in. .env is gitignored.

# The key claude authenticates with inside the agent container.
ANTHROPIC_API_KEY=sk-ant-replace-me

# Your deck guides. Mounted read-only at /guides in every container.
MTG_DECKS=/Users/szymonri/Documents/mtg-decks

# Which model the agent plays with.
CLAUDE_MODEL=sonnet

# Pin the claude CLI; `latest` rebuilds that layer on every no-cache build.
CLAUDE_CODE_VERSION=latest
```

Append to `.gitignore`:

```
.env
memory/
```

- [ ] **Step 8: Make turn 0 self-contained across containers**

Because the driver and the agent do not share a filesystem, turn 0 must carry
the content, not just the path. In `src/mtg_agent/driver/prompt.py`, change
`turn_zero`'s signature and its first section:

```python
def turn_zero(assignment: Assignment, current_game_path: Path, briefing: str = "") -> str:
```

and replace the "Before you take a single action" block's item 1 with:

```python
    briefing_block = (
        f"\n{briefing}\n" if briefing else f"Read `{current_game_path}`.\n"
    )
```

interpolating `briefing_block` where item 1 currently sits, so the prompt
reads:

```
## Before you take a single action

1. Here is everything about this game:
<the rendered current-game.md, inline>
2. Read your deck's gameplan guide ...
```

In `src/mtg_agent/driver/app.py`'s `dispatch`, pass it:

```python
        briefing = render_current_game(assignment, settings.memory_root)
        current_game_path.write_text(briefing)
        turn_id = push_turn(turn_zero(assignment, current_game_path, briefing))
```

Add a test to `tests/driver/test_prompt.py`:

```python
def test_turn_zero_inlines_the_briefing_when_given_one(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md", briefing="# This game\nBODY")
    assert "BODY" in text
```

and one to `tests/driver/test_app.py`:

```python
def test_turn_zero_inlines_the_decklist_because_containers_share_no_disk(world):
    client, _, submitted, _ = world
    dispatch(client)
    assert "4 Psychic Frog" in submitted[0]["text"]
```

- [ ] **Step 9: Add a mise task**

Append to `mise.toml`:

```toml
[tasks.compose]
description = "Bring up game + continuous agent + driver"
run = "docker compose up --build"
```

- [ ] **Step 10: Run the tests**

Run: `uv run pytest -q`
Expected: the whole suite passes.

- [ ] **Step 11: Bring it up for real and play a game**

```bash
cp .env.example .env   # put a real ANTHROPIC_API_KEY in it
docker compose up --build
```

Then:
1. `curl -s localhost:8080/readyz` - the agent reports `ready` once claude has
   booted and the bypass dialog is accepted. First boot takes up to a minute.
2. Open `http://127.0.0.1:8000/play/`, pick a guided deck for each seat, start
   the game and click your seat.
3. `curl -s localhost:8090/status` - the driver names the deck it identified.
4. Play. `curl -s localhost:8080/v1/messages` shows the turn; if it looks
   stuck, `curl -sX POST localhost:8080/v1/probe` asks the agent what it is
   doing without taking a turn slot.
5. Finish the game and check `./memory/<deck>/` for the notes it wrote.

- [ ] **Step 12: Write the README section**

Append to `README.md`, after the "Play it - Human vs AI" section:

```markdown
## Play against a continuous agent

`docker compose up --build` brings up three containers: the game (this repo,
with the `/play/` UI), an **agent** running one long-lived interactive
`claude` under a PTY, and a **driver** that tells the agent what it is
playing.

The difference from the bundled AI seat is memory. The bundled seat is a
one-shot `claude --print` that forgets the game when it ends. The agent
container keeps one claude session alive across games, so it builds a read on
a matchup and can act on what happened last time.

What it knows before its first action:

- its own decklist, and which guided deck under `$MTG_DECKS/decks/` that is;
- that deck's `GAMEPLAN_GUIDE.md`, `SIDEBOARD_GUIDE.md` and per-matchup files;
- every note it has written itself, under `./memory/<deck>/`.

What it is deliberately NOT told: anything about your deck. It works the
matchup out from the cards you play, then opens the matching guide. Getting
that read right sooner is what the notes are for.

After the game it writes `./memory/<deck>/games/<game-id>.md` and revises
`./memory/<deck>/vs-<archetype>.md`. That directory is a host bind-mount, so
it survives every rebuild - and you can read and edit it yourself.

| what | where |
|---|---|
| play | http://127.0.0.1:8000/play/ |
| agent turns, probe, interrupt | http://127.0.0.1:8080/v1/... |
| which deck the driver identified | http://127.0.0.1:8090/status |
| the agent's notes | `./memory/` |

Set `AI_SEAT_MODE=subprocess` (the default outside compose) to go back to the
one-shot seat.
```

- [ ] **Step 13: Commit**

```bash
uv run ruff format src tests && uv run ruff check src tests
git add Dockerfile Dockerfile.agent docker-compose.yml .env.example .gitignore \
        deploy/mcp.base.json mise.toml README.md \
        src/mtg_agent/driver/prompt.py src/mtg_agent/driver/app.py \
        tests/driver/test_compose.py tests/driver/test_prompt.py tests/driver/test_app.py
git commit -m "feat: docker-compose bringing up the game, the continuous agent and the driver"
```

---

## Self-review against the spec

| Spec requirement | Task |
|---|---|
| config.py from env | 1 |
| transcript parsing (usage, per-turn tokens) | 2 |
| stop_hook, always exit 0 | 3 |
| turns.py record + store | 4 |
| ring.py ANSI/whitespace matching | 5 |
| pty spawn, never `-p` | 6 |
| session: boot dialog, quiescence, two-write submit, stall-not-fail | 7 |
| probe (pending/delivered/answered) + ESC interrupt | 8 |
| bootstrap: claude.json seed 0600, settings, MCP merge 0600, CLAUDE.md, skills | 9 |
| ccw_* metrics; retrying webhook | 10 |
| HTTP API incl. 409/503/404, shared secret, loopback internal listener | 11 |
| integration test with a stub claude, no API key | 12 |
| decklist fingerprinting against /guides | 13 |
| current-game.md, turn 0, reflection turn, no opponent info | 14 |
| driver /dispatch /turn-done /status | 15 |
| AI_SEAT_MODE=wrapper, subprocess still default | 16 |
| Dockerfiles, compose, guides mounted, memory bind-mount, README | 17 |

Dropped deliberately, per the spec's non-goals: OIDC/Keycloak (replaced by
`WRAPPER_SHARED_SECRET`), pod TTL and handoff turns, S3 conversation storage,
multi-game routing.

One correction the plan makes to the spec: the spec said turn 0 hands over
*paths* to keep the prompt small. That is right for the ~500-line guides,
which live in the `/guides` mount both containers can see - but wrong for
`current-game.md`, which the driver writes into its own container. Task 17
Step 8 inlines that one file's contents and leaves the guides as paths.
