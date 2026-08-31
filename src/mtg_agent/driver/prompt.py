"""What the agent is told, and when.

Turn 0 is the whole reason the driver exists. The wrapper refuses a second
turn while one is in flight, so whatever turn 0 says is necessarily known to
the agent before its first action - which is the requirement: it must know
its deck before it is asked to move.

The rules of engagement below are lifted from docs/ai_seat_brief.md, where
each line was learned the hard way against the real seat API; only the
command section changes, because this agent drives MCP tools rather than
the seat command line.
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
                "Archetypes with a matchup file: " + ", ".join(f"`{a}`" for a in guide.archetypes),
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


def turn_zero(assignment: Assignment, current_game_path: Path, briefing: str = "") -> str:
    """Turn 0. `briefing` is the rendered `current-game.md`.

    It is inlined rather than referenced because the driver and the agent are
    separate containers with separate filesystems: `current_game_path` names a
    file only the driver can open. The guides stay as paths - `/guides` is
    mounted in both containers, and they run to hundreds of lines each.
    """
    guide = assignment.guide
    matchups = (
        f"`{guide.matchups_dir}/<archetype>.json`"
        if guide is not None and guide.matchups_dir is not None
        else "(none for this deck)"
    )
    briefing_block = (
        f"1. Here is everything about this game (the driver wrote the same text to "
        f"`{current_game_path}` on its own disk):\n\n{briefing}\n"
        if briefing
        else f"1. Read `{current_game_path}`. It names your seat, your uid, your "
        "decklist and your guides.\n"
    )
    return f"""You are playing a real game of Magic: the Gathering against a human,
who is playing through a web UI against the same game server you act on.

## Before you take a single action

{briefing_block}
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
