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


def plan_path(memory_root: Path, assignment: Assignment) -> Path:
    """Where the agent keeps this game's running plan.

    Deliberately the same file the reflection turn asks it to finish: a plan
    written while playing and a write-up written afterwards are the same note
    at two points in time, and one file means no bookkeeping mid-game.
    """
    return Path(memory_root) / assignment.deck_slug / "games" / f"{assignment.game_id}.md"


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
    lines += [
        "",
        "## Your plan file for this game",
        "",
        f"`{plan_path(memory_root, assignment)}` - yours to write and rewrite while "
        "you play. Keep it short; you will finish it after the game.",
        "",
    ]
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
3. Write your opening plan into the plan file named in the briefing (see
   "Keep a plan, in writing" below).
4. Only then call `wait_for_priority`.

## How you act

Every tool is on the `mtg` MCP server and takes your uid as `player_uid`:
`{assignment.uid}`.

- `wait_for_priority(player_uid, max_seconds=...)` blocks until it is your
  moment and hands you the board. ALWAYS bound it and call it in a loop. A
  bare unbounded wait strands your turn.
- `my_state(player_uid)` is the board as you are allowed to see it.
- `cast`, `play_land`, `suspend`, `plot`, `foretell`, `cycle`, `activate`,
  `declare_attackers`, `declare_blockers`, `decide`, `pass_priority`,
  `resolve_top`, `advance`, `set_step` act.
- `mulligan` and `opening_hand_action` are the pregame (CR 103.5, CR 103.6).
  A game dealt from decklists *rests* there before turn 1 and nothing on the
  board can happen until every seat has declared, so these come first: declare
  your mulligan, then take or decline your opening-hand actions. Until you do,
  the game sits still and your opponent is waiting on you.
- `game_log` is the history; `report_problem` files an engine bug.

Each action tool performs the action and blocks until you next have something
to do - one call, one meaningful action.

**Holding priority is how you take two actions in one phase.** For a `cast`,
releasing is the default, because releasing is how the spell you just cast
gets to resolve; but if you intend to do something else before the phase moves
on - cast two spells, hold up an instant - pass `hold_priority=true`, or the
phase can end between your two actions.

  cast(player_uid=..., card="Amulet of Vigor", hold_priority=True)

Two kinds of action hold on your behalf whether you ask or not, because
neither uses the stack, so there is nothing releasing priority would let
resolve:

- a **special action**: CR 116.3 says "if a player takes a special action,
  that player receives priority afterward". That covers `play_land`
  (CR 116.2a), `suspend` (CR 116.2f), `plot` (CR 116.2k) and `foretell`
  (CR 116.2h). `cycle` holds with them, but it is *not* a special action -
  CR 702.29a makes cycling an ordinary activated ability, and CR 117.3c is
  what keeps priority with you there.
- a **mana ability**: `activate` on a land or other mana source holds
  whatever you passed for `hold_priority`. CR 605.3b: it "doesn't go on the
  stack ... it resolves immediately after it is activated". Releasing could
  end the step (CR 500.2) and empty the mana you just made (CR 500.5).

So a land drop, or tapping for mana, never costs you the rest of your main
phase - but a `cast` still does unless you hold it.

## Identify the matchup yourself

You have NOT been told what your opponent is playing, deliberately. Work it
out from the cards they actually play, then read the matching matchup file:
{matchups}. Say in one line what you have deduced and how confident you are
the first time you commit to a read. Getting this right earlier, game after
game, is the point of the notes you keep.

## Win the game, do not merely make legal moves

Decide three things before your first action, and revisit them whenever the
board changes:

- **How this deck wins this game** - the clock, the engine, the lock.
- **Your role in this matchup**: beatdown or control. The faster deck attacks
  and forces the issue; the other one answers, trades, and stabilises. Getting
  this backwards loses games that no single misplay would have.
- **What beats you**, and which of your cards answer it. Hold those answers
  for what they are for.

Then play toward that plan across turns, not one priority at a time.

## Not playing is a play

Taking no action is a deliberate, legal choice, not a failure to act
(CR 117.3d: a player who chooses to take no actions passes). Expect to do it
often, and for a reason you could state out loud:

- Hold up mana for an instant instead of tapping out on your own main phase.
- Keep removal or a counterspell live for the card that actually beats you
  rather than spending it on the first legal target.
- Decline an attack that trades badly; attack when the race maths says to.
- Keep a land in hand when the card matters more than the mana - but never
  miss a land drop your curve needs.
- Hold a creature back to rebuild after a sweeper once they have shown one, or
  shown the mana for one.
- Play around what their open mana and their archetype let them have. If you
  cannot beat it, play as though it is not there.

Sequencing is most of the skill: land drops first when they cost you nothing,
threats in the order that plays around their answers, and commit only as much
board as one card can punish.

## Investigate before you commit

Thinking is cheaper than a lost game. Before a decision that matters, read
`my_state`, read `game_log` for everything they have already shown, count their
untapped mana and their cards in hand, and look up anything you are not certain
of. Spend that effort on the decisions that turn the game, not on every land
drop.

## Keep a plan, in writing

The briefing names a plan file for this game. Keep a handful of lines there and
update it as the game moves:

- your read on their archetype and your confidence in it,
- your role and how you intend to win,
- what you are holding, and what you are holding it for,
- the next two or three turns as you currently intend to play them.

Rewrite lines rather than appending; this is a working plan, not a diary. A few
seconds per turn, and only when something changed - never an essay per
priority. After the game you will be asked to finish this same file.

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
- Use the whole board: attack, block, activate abilities, let triggers
  resolve, respond on the stack. An idle permanent and an uncast trick are
  wasted resources - decline them only for a reason you can name.
- Passing with purpose is not stalling. Stalling is being unable to say what
  you are waiting for. Every wait is bounded, every pending decision is
  answered, and the human never sits in front of a game where nothing is
  happening because you did not choose.
- NEVER state card text or a rule from memory. Use the `spellslinger` MCP
  server's `knowledge_search` and `resolve_cards` to look them up.

Begin."""


def reflection_turn(assignment: Assignment, memory_root: Path, outcome: str) -> str:
    slug = assignment.deck_slug
    deck_dir = Path(memory_root) / slug
    return f"""The game is over: {outcome}

Write up what you learned, so the next game on this deck starts from it.

1. `{deck_dir / "games" / f"{assignment.game_id}.md"}` - the plan file you kept
   while playing. Finish it: the result,
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
