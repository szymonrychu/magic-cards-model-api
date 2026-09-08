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
    agent: dict | None = None
    """The chosen model/effort/thinking budget (`AgentSettings.to_wire()`,
    camelCase - see `contract/agent-settings.schema.json`), carried through
    from the dispatch that created this assignment - `None` for a caller
    that dispatched before agent settings existed."""

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

- `wait_for_priority(player_uid, max_seconds=20)` checks now, and - if
  nothing has changed yet - keeps checking on the server, for up to
  `max_seconds`, before giving up and telling you so. It always returns on
  its own within that bound (capped server-side well under any transport
  timeout), never holds the request open past it, and it is where the wait
  belongs now: pass `max_seconds`, do not reach for `Bash: sleep` and a
  bare call - that is a whole extra tool call and turn spent doing what
  this one parameter already does for you, and it is the pattern that used
  to show up as `Bash sleep 2` after `Bash sleep 2` in the log. A decision
  addressed to you comes back as its own distinct kind of result
  (`input_required`) rather than buried in ordinary text, on any call,
  bounded wait or not - answer it with `decide` before doing anything else.

  If `wake_reason` still says "nothing new yet" after the bound, that is
  not a failure - your opponent is still deciding - call it again, with
  `max_seconds` again. Leave `max_seconds` at 0 (its default) only when you
  specifically want the immediate poll-and-return, right after your own
  action, when something has plausibly already changed.
- `my_state(player_uid)` is the board as you are allowed to see it, and it
  answers the other half of the same question in the same breath: every
  answer that carries the board also carries `recent_log`, the tail of what
  both seats have actually done since - allowed and refused, in the game's
  own words. That holds for `wait_for_priority`, `pass_until` and every
  action tool too, so "what happened while I was not looking" and "what can
  I do now" are one call between them. Never spend a second call asking for
  what the first one already handed you.
- `cast`, `play_land`, `suspend`, `plot`, `foretell`, `cycle`, `activate`,
  `declare_attackers`, `declare_blockers`, `decide`, `pass`, `pass_until` act.
  Priority has exactly those two pass verbs and no more, because the rules give
  it exactly two: CR 117.1 says the player with priority "may cast spells,
  activate abilities, and take special actions", and CR 117.3d says a player
  who chooses none of those passes. Everything that *follows* a pass is the
  game's own doing, not yours - CR 117.4 resolves the top of the stack, or ends
  the step, once all players have passed in succession, and CR 703.2 says
  turn-based actions "are not controlled by any player". So there is no tool
  for resolving the stack or stepping the turn on, and you never need one: the
  game rolls itself through every step nobody receives priority in (CR 500.3's
  untap and cleanup).
- `mulligan` and `opening_hand_action` are the pregame (CR 103.5, CR 103.6).
  A game dealt from decklists *rests* there before turn 1 and nothing on the
  board can happen until every seat has declared, so these come first: declare
  your mulligan, then take or decline your opening-hand actions. Until you do,
  the game sits still and your opponent is waiting on you. Once you have
  declared and they have not, there is nothing left for you to do there and
  `wait_for_priority` treats it as such: pass `max_seconds` and it spends
  that bound on the server. Do not sit calling it back to back through a
  human's think time - that is dozens of turns of yours spent re-reading a
  board that has not moved.
- `game_log` is the whole history, with each entry's full board snapshot -
  reach for it only when the `recent_log` you were already given says
  `omitted` above zero, when you need further back than it goes, or when you
  want one entry's snapshot. It takes `since` (a `seq` you already hold, so
  you get only what is new) as well as `limit`. `report_problem` files an
  engine bug.

Each action tool performs the action, then checks once for your next moment
to act (never blocks) - one call, one meaningful action.

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

## Passing a whole stretch at once, and when not to

**`pass_until` is the default way to cross a stretch of windows you cannot act
in - reaching for repeated single `pass` calls instead is the wrong move.**
Each `pass` is a full round trip and a whole agent turn; walking a turn by
calling it over and over where nothing changed is slow, expensive, and the
exact log noise that gets noticed. `pass` is for the moment you genuinely hold
priority once - in response to something, with your next action already
decided for the very next window - not for a stretch where you have nothing
to do until some later point.

`pass` gives up one priority. `pass_until` is the same verb run to a
destination: a step name, and it keeps passing every priority you currently
hold until it gets there, instead of handing you back one priority at a time.

  pass_until(player_uid=..., step="upkeep")
  pass_until(player_uid=..., step="precombat_main", whose_turn="opponent")

Naming the step you are standing in means its NEXT occurrence, never this one -
`step="upkeep"` asked during your upkeep is a whole turn of passing, which is
what "until my upkeep" means when you say it out loud.

This can take several calls for a long run, but it does not block your turn
budget doing it: pass `max_seconds` the same way you would to
`wait_for_priority`, and it waits out its own bound on the server rather than
you spending a `Bash: sleep` and a whole separate call on the same thing.
Read `finished`:

- `finished: true` - a real stop, and `stopped_because` names it: the
  destination, something new on the stack, a decision addressed to you (that
  answer arrives as `input_required`), your own declare-attackers step, being
  attacked in your own declare-blockers step, a refused pass, the game ending.
- `finished: false` - it got as far as it could within `max_seconds` and
  handed the board back because your opponent holds priority or the game is
  frozen on a question only they can answer. **Call again with `max_seconds`
  again, passing the answer's `resume` object back verbatim as `resume`:**

      pass_until(player_uid=..., step="upkeep", max_seconds=20, resume=<the resume object>)

  Dropping `resume` does not merely lose your place: the next call re-stamps
  the "do not stop in the step this run started in" exemption onto wherever
  you are standing right now, so a run resumed from inside your own
  declare-attackers step would pass straight through your attack without
  stopping - silently, which is exactly what that stop exists to prevent.

A combat stop never fires on the step you were standing in when you started
the run - asking to pass past combat from inside it is you having already
looked at combat.

Three of those stops are yours to switch off, and each defends you against
something you cannot see going wrong, so leave them on unless you have a reason
you could say out loud. None of them is a destination and none of them replaces
`step` - they are extra reasons to stop on the way there, and they combine with
it:

- `stop_on_stack=False` runs through the opponent's spells and every trigger.
  You will not get to respond to any of it. This is the stop that gives you
  your answer to a spell cast in a window you were passing through - their
  instant in your upkeep while you were running to your own precombat main -
  and CR 117.4 resolves that spell the moment all players have passed in
  succession, so the pass you have not yet made is the whole of your chance.
  Left on, the run hands the board back with their spell still on the stack and
  priority still yours.
- `stop_when_attacked=False` runs through your own declare-blockers step. You
  will block nothing and take the whole attack. Declaring blockers is a
  turn-based action that "doesn't use the stack" (CR 509.1), so the stack stop
  cannot catch it for you. Only for a board that cannot block anyway, or a life
  total the attack cannot reach.
- `stop_when_you_can_attack=False` runs through your own declare-attackers
  step. You will attack with nothing that turn. Declaring attackers is a
  turn-based action too (CR 508.1), so again the stack stop cannot see it, and
  skipping it is silent: the engine treats a player who never declares as
  declaring nobody, which is always legal, so the turn produces no attack and
  nothing says why. Expect this one to stop you every turn on a run aimed past
  combat - `step="postcombat_main"`, `step="end_step"` - and expect that,
  because it is asking you before your attack goes by. Switching it off is the
  deliberate "I am not attacking this turn", never a way to make the call
  quieter.

**Do not reach for this every time.** How you pass is information your opponent
is reading. Sitting on your priority with mana up represents a trick whether or
not you have one, and blowing through a window where they were expecting your
flash threat throws away a bluff you were being given for free. Auto-passing
every single turn tells them "I have nothing in hand" just as loudly. So:

- Use it for the stretches you genuinely cannot act in - their untap through
  their draw, a turn where your hand is all sorceries, a turn you are tapped
  out and holding nothing.
- Pass by hand when the pass is part of the game: any window where you want
  them to play around a card, whether or not you hold it; the turn you really
  do have the counterspell; the end step where you want them to think twice.

## Investigate before you commit

Thinking is cheaper than a lost game, but a round trip is not - each one is a
whole turn of yours. Before a decision that matters, read the answer you
already have: the board, its `recent_log` tail of everything they have shown,
and your own listed actions all arrive together. Count their untapped mana and
their cards in hand off that, and look up any card or rule you are not certain
of. Spend that effort on the decisions that turn the game, not on every land
drop - and spend extra calls only on what the answer in front of you does not
already say.

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
- In a main phase use `play_land` / `cast` / `activate`, then `pass` for that
  one priority. The moment there is nothing left to do until some later point
  in the turn, reach for `pass_until` to get there in one call - never `pass`
  called again and again through every window in between. There is nothing
  else to reach for beyond these: no step needs stepping on by hand, and the
  untap and cleanup steps (CR 502.4, CR 514.3) grant nobody priority and end
  on their own (CR 500.3).
- If `pass` is refused with "you do not have priority", call
  `wait_for_priority` again. That is the whole remedy - a refused pass means
  the other seat is still deciding, and nothing you can call moves the game
  for them.
- A pending decision FREEZES the game. Answering it is the only legal move,
  and an unanswered decision times out to a default and wastes the card.
- Use the whole board: attack, block, activate abilities, let triggers
  resolve, respond on the stack. An idle permanent and an uncast trick are
  wasted resources - decline them only for a reason you can name.
- Passing with purpose is not stalling. Stalling is being unable to say what
  you are waiting for. No call ever blocks, every pending decision is
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
