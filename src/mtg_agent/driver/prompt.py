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

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .guides import (
    CONFIDENT,
    LOW,
    UNKNOWN_ARCHETYPE,
    Candidate,
    DeckGuide,
    Decklist,
    Identification,
    ScriptsRegistration,
)
from .handover import HANDOVER_BUDGET_SECONDS, NUDGE_AFTER_SECONDS
from .matchup_card import (
    guide_paths,
    matchup_note_key,
    render_matchup_card,
)
from .revision import UNKNOWN_REVISION

UNKNOWN_DECK = "unknown-deck"


#: The byte cap on the briefing's play-script section: every script row and
#: every skipped-file notice together. A deck can carry any number of plays,
#: each long-named with thirty-two archetype keys, and the briefing's own
#: caps (the per-card budget, `VS_NOTE_BUDGET_BYTES`) were set before plays
#: existed - so this section spends its own small, fixed share and names
#: what it did not list rather than growing with the corpus.
PLAYS_SECTION_CAP_BYTES = 3 * 1024
#: Longest a script's `name` or a notice's message is quoted in the listing.
PLAYS_NAME_CHARS = 90
#: Archetype keys quoted per script before the rest are counted.
PLAYS_ARCHETYPE_KEYS = 4


@dataclass
class Assignment:
    game_id: str
    uid: str
    player: str
    opponent: str
    api_base: str
    decklist: Decklist
    identification: Identification
    """What the archetype classifier could say about `decklist`: the guide,
    how sure it is, and the runners-up. Carried whole rather than reduced to
    a guide, because what the agent is told in turn 0 differs per outcome -
    a confident match, a low-confidence one that has to name the other
    plausible archetype, and no match at all."""

    guides_revision: str = UNKNOWN_REVISION
    """Which revision of the deck-guides corpus this seat was handed
    (`revision.guides_revision` over the driver's `GUIDES_ROOT`). The corpus
    moves under a running lobby, so a game whose artifacts do not name it
    cannot be reproduced or compared against another game."""

    agent: dict | None = None
    """The chosen model/effort/thinking budget (`AgentSettings.to_wire()`,
    camelCase - see `contract/agent-settings.schema.json`), carried through
    from the dispatch that created this assignment - `None` for a caller
    that dispatched before agent settings existed."""

    scripts: ScriptsRegistration | None = None
    """What registering the deck's play scripts at dispatch came to - `None`
    for a caller that rendered a briefing without attempting it."""

    role: str = "seat"
    """`seat` (this container plays `player`) or `advisor` (B5's buddy: this
    container watches `player`'s own view and proposes, and the game server
    refuses every action it tries - `magic_cards_model_api.game_access`).
    Carried from `DispatchIn.role`, and the one thing that picks
    `advisor_turn_zero` over `turn_zero`. Defaulted, so a dispatch from a
    caller that predates roles is a seat, unchanged."""

    @property
    def guide(self) -> DeckGuide | None:
        """The one guide this seat is playing from, if the classifier could
        name one."""
        return self.identification.guide

    @property
    def deck_slug(self) -> str:
        return self.guide.slug if self.guide else UNKNOWN_DECK

    @property
    def matchups_dir(self) -> Path | None:
        """Where the per-opponent matchup files are, across every guide the
        classifier handed over."""
        for guide in self.identification.guides:
            if guide.matchups_dir is not None:
                return guide.matchups_dir
        return None


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


def plan_header(assignment: Assignment) -> str:
    """The header the driver seeds the plan file with, before the agent has
    written a word of it.

    Written by the driver rather than asked of the agent on purpose: the
    guides revision is the driver's own knowledge, and a fact the agent is
    merely told to copy is one it can get wrong or forget. Seeding the file
    means every game leaves behind an artifact naming the corpus it was
    played against, even if the agent never gets as far as writing a plan.
    """
    return "\n".join(
        [
            f"# {assignment.deck_slug} - game {assignment.game_id}",
            "",
            f"- seat: {assignment.player} vs {assignment.opponent}",
            f"- deck guides revision: `{assignment.guides_revision}`",
            "",
            "",
        ]
    )


def _board_block(board: dict[str, int]) -> str:
    return "\n".join(f"{count} {name}" for name, count in sorted(board.items()))


def _clause_count(candidate: Candidate) -> str:
    return f"{candidate.matched} of its {candidate.clauses} signature clauses"


def _named(candidates: Sequence[Candidate]) -> str:
    return ", ".join(f"`{c.slug}` ({_clause_count(c)})" for c in candidates)


def _deck_claim(identification: Identification) -> list[str]:
    """What the agent is told about which deck it is holding.

    Three outcomes, three different things to say - and the `none` branch is
    load-bearing: it is the behaviour a list with no guide has always had,
    and a pasted list reaching it must still be playable.
    """
    guide = identification.guide
    others = identification.alternatives
    if identification.confidence == CONFIDENT:
        top = identification.candidates[0]
        return [
            f"You are piloting **{guide.slug}** - `{identification.archetype}` in the "
            f"guides corpus. Every one of its {top.clauses} signature clauses is in "
            "your maindeck, so the matchup card below is the plan for the deck you "
            "are actually holding.",
        ]
    if identification.confidence == LOW:
        if guide is None:
            # A family, with no branch chosen: both branches' guides are
            # handed over below rather than one of them picked by coin flip.
            return [
                f"This list is some kind of **{identification.archetype}**, but which "
                "branch of it is not settled: "
                f"{_named(identification.leaders)} match equally well, on the clauses "
                "they share and on nothing that tells them apart. There is a matchup "
                "card below for each branch: take the parts they agree on, and trust "
                "the board over either of them where they disagree.",
            ]
        top = identification.candidates[0]
        plausible = (
            f" Also plausible: {_named(others)}."
            if others
            else " No other archetype came close, but this one is not certain either."
        )
        return [
            f"You are probably piloting **{guide.slug}** - `{identification.archetype}` "
            f"in the guides corpus - but this is a **low-confidence** match: it "
            f"answers {_clause_count(top)}, not all of them.{plausible} "
            "Take the matchup card below for what it is worth and trust the board "
            "over it where they disagree.",
        ]
    return [
        "There is **no guide** for this decklist - no archetype in the guides corpus "
        "answers even half its signature clauses from your maindeck. So there is no "
        "deck plan and no field tells to hand you: play it from the list itself and "
        "from what the board tells you.",
    ]


#: How much of the agent's own standing `vs-<archetype>.md` notes the
#: briefing inlines. A note the agent has been revising for twenty games can
#: be any size, and inlining all of it would spend the context the matchup
#: card just saved.
VS_NOTE_BUDGET_BYTES = 4 * 1024


def _standing_notes(
    notes: list[Path],
    opponent_archetype: str | None,
) -> tuple[list[Path], list[Path]]:
    """The `vs-<archetype>.md` standing reads, and everything else.

    Standing reads are inlined; a past game's write-up stays a path, because
    there can be dozens of those and each is about one game rather than about
    the matchup.
    """
    standing = [p for p in notes if p.name.startswith("vs-") and p.parent.name != "games"]
    rest = [p for p in notes if p not in standing]
    if opponent_archetype:
        wanted = f"vs-{matchup_note_key(opponent_archetype)}.md"
        standing.sort(key=lambda p: (p.name != wanted, p.name))
    return standing, rest


def _inlined_notes(standing: list[Path], budget: int) -> list[str]:
    """The standing reads' bodies, in fenced blocks, until the budget runs
    out - and then the paths of the ones that did not fit, said out loud."""
    lines: list[str] = []
    spent = 0
    for path in standing:
        try:
            body = path.read_text()
        except OSError:
            lines.append(f"- `{path}` (unreadable)")
            continue
        room = budget - spent
        if room <= 0:
            lines.append(
                f"- `{path}` - not inlined, the note budget was spent; read it if you want it"
            )
            continue
        clipped = body.encode()[:room].decode(errors="ignore")
        spent += len(clipped.encode())
        lines += [f"**`{path}`** - your standing read on this matchup:", "", "````"]
        lines.append(clipped.rstrip())
        if len(clipped) < len(body):
            lines.append(f"[...cut at {room} bytes - the rest is in the file]")
        lines += ["````", ""]
    return lines


def _clip(text: str, limit: int = PLAYS_NAME_CHARS) -> str:
    flat = " ".join(f"{text}".split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _script_row(script: dict) -> str:
    keys = [f"{key}" for key in script.get("opposing_archetypes") or []]
    shown = ", ".join(keys[:PLAYS_ARCHETYPE_KEYS])
    if len(keys) > PLAYS_ARCHETYPE_KEYS:
        shown += f" +{len(keys) - PLAYS_ARCHETYPE_KEYS} more"
    return (
        f"- `{_clip(script.get('id', '?'), 60)}` (priority {script.get('priority', '?')}, "
        f"vs {shown or '?'}) - {_clip(script.get('name', ''))}"
    )


def _bounded(rows: list[str], budget: int, noun: str) -> tuple[list[str], int]:
    """As many of `rows` as fit `budget` bytes, then one line naming the rest."""
    kept: list[str] = []
    spent = 0
    for index, row in enumerate(rows):
        cost = len(row.encode()) + 1
        if spent + cost > budget:
            kept.append(f"- ... {len(rows) - index} more {noun} not listed")
            return kept, spent
        kept.append(row)
        spent += cost
    return kept, spent


def _plays_section(registration: ScriptsRegistration) -> list[str]:
    """The briefing's play-script section: what the game server holds for
    this seat, and every file that was skipped fail closed, within
    `PLAYS_SECTION_CAP_BYTES`."""
    corpus = registration.corpus
    lines = ["## Your registered play scripts", ""]
    if registration.status == "registered":
        lines.append(
            f"The game server holds {len(corpus.scripts)} play script(s) for this seat, "
            "registered once at dispatch and not changed for the rest of the game:"
        )
    elif registration.status == "disabled":
        lines.append(
            "Play scripts are disabled on this deployment (the game server refused "
            "them with `scripts_disabled`). None is registered for this game: play "
            "every decision yourself."
        )
    elif registration.status == "no_deck":
        lines.append(
            "No play scripts are registered: your list was not identified as one "
            "guided deck, so there was no deck's plays to register."
        )
    else:
        lines.append(
            f"The play scripts for this deck could not be registered "
            f"({_clip(registration.detail, 160)}). None will run: play every decision yourself."
        )
    lines.append("")
    header = len("\n".join(lines).encode())
    notice_rows = [f"- `{_clip(n.path, 80)}` {n.code}: {_clip(n.message)}" for n in corpus.notices]
    # Notices get a third of the room at most: a skipped file is worth
    # knowing about, and never worth crowding out the scripts that did load.
    notice_budget = (PLAYS_SECTION_CAP_BYTES - header) // 3 if notice_rows else 0
    notices, notice_spent = _bounded(notice_rows, notice_budget, "skipped files")
    notice_header = (
        ["", "Skipped, fail closed (these files did not load; the rest did):", ""]
        if notices
        else []
    )
    room = (
        PLAYS_SECTION_CAP_BYTES
        - header
        - notice_spent
        - len("\n".join(notice_header).encode())
        - 80
    )
    if registration.status == "registered":
        if corpus.scripts:
            rows, _ = _bounded([_script_row(s) for s in corpus.scripts], room, "scripts")
            lines += rows
        else:
            lines.append("(the deck carries no play scripts)")
    return lines + notice_header + notices


def render_current_game(
    assignment: Assignment,
    memory_root: Path,
    opponent_archetype: str | None = None,
) -> str:
    """The briefing, which turn 0 inlines verbatim.

    `opponent_archetype` is normally unknown at turn 0 and stays that way -
    the owner's doctrine is that the agent deduces the matchup from the cards
    it sees. It exists for the case where a caller already knows (a resumed
    game, a study run), and only changes which standing note is inlined
    first.
    """
    identification = assignment.identification
    lines = [
        "# This game",
        "",
        f"- game id: `{assignment.game_id}`",
        f"- your seat uid: `{assignment.uid}` (every MCP tool takes it as `player_uid`)",
        f"- you are: **{assignment.player}**",
        f"- your opponent's seat: **{assignment.opponent}**",
        f"- game server: {assignment.api_base}",
        f"- deck guides revision: `{assignment.guides_revision}` "
        "(which revision of the guides corpus you are being handed)",
        "",
        "## Your deck",
        "",
    ]
    lines += [*_deck_claim(identification), ""]
    lines += ["Maindeck:", "", "```", _board_block(assignment.decklist.mainboard), "```", ""]
    if assignment.decklist.sideboard:
        lines += [
            "Sideboard (yours to board with between games; nothing here is on the "
            "battlefield or in your library):",
            "",
            "```",
            _board_block(assignment.decklist.sideboard),
            "```",
            "",
        ]

    offered = identification.guides
    for guide in offered:
        # Inlined, not referenced: this is the whole point of the card. The
        # gameplan guide it is rendered from is 42-73 KB of generated
        # markdown, and the plan behind it fits in ~11 KB of context.
        #
        # Every branch gets the card's OWN budget - the module's target and
        # cap, unshared - because the budget the brief sets is per card, and
        # a card is what each branch needs. Dividing it by the number of
        # branches was a briefing-wide budget nothing asked for, and the
        # thing it bought was paid for out of the one field the brief
        # requires on every row: at 5120 bytes with a shared cap there is no
        # rung left that keeps the tells table's `watch for` column, so the
        # tron case rendered both branches with it deleted off every one of
        # their 34 rows. Rendered at its own budget each of those two comes
        # in whole, with every field, at 10026 and 11659 bytes.
        #
        # So a two-branch briefing carries ~21 KB of card rather than ~15,
        # and that is the cheaper half of the trade by a wide margin: it is
        # still a fraction of the two 45-73 KB gameplan guides it replaces,
        # and it happens only when the classifier could not tell two branches
        # of one family apart, which is the case where the agent most needs
        # both plans intact.
        card = render_matchup_card(guide, assignment.decklist)
        lines += [card.text, ""]
        lines += [
            "## Your guides, for a deliberate look-up"
            if len(offered) == 1
            else f"## `{guide.slug}` guides, for a deliberate look-up",
            "",
            "The card above is your brief. These are here for one specific line it "
            "does not carry - not for a read before your first action.",
            "",
        ]
        for label, path in guide_paths(guide):
            lines.append(f"- {label}: `{path}`")
        if guide.matchups_dir is not None:
            lines += [
                f"- per-matchup detail, one small read once you have the read: "
                f"`{guide.matchups_dir}/<archetype>.json`",
                "",
                "Archetypes with a matchup file: " + ", ".join(f"`{a}`" for a in guide.archetypes),
            ]
        lines.append("")
    if not offered:
        lines += [render_matchup_card(None, assignment.decklist).text, ""]

    if assignment.scripts is not None:
        lines += [*_plays_section(assignment.scripts), ""]

    notes = memory_index(memory_root, assignment.deck_slug)
    standing, rest = _standing_notes(notes, opponent_archetype)
    lines += ["## What you have already learned on this deck", ""]
    if standing or rest:
        lines += _inlined_notes(standing, VS_NOTE_BUDGET_BYTES)
        if rest:
            lines += ["Past games, as paths - open one only if you have a reason to:", ""]
            lines += [f"- `{path}`" for path in rest]
    else:
        lines.append("Nothing yet - this is your first recorded game on it.")
    lines += [
        "",
        "## Your plan file for this game",
        "",
        f"`{plan_path(memory_root, assignment)}` - yours to write once, near the start "
        "of the game. It already exists, with a header naming this game and the guides "
        "revision above: keep that header and write under it. Keep it short; you will "
        "finish it after the game.",
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
    matchups = (
        f"`{assignment.matchups_dir}/<archetype>.json`"
        if assignment.matchups_dir is not None
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
2. That briefing IS your brief. Its matchup card carries your deck's plan,
   a row per archetype in the field with the signature cards that give each
   one away, and the three biggest matchups in detail - rendered from the
   same structured plan the gameplan guide is generated from - and your own
   standing notes on this deck are inlined underneath it. So **do not read
   the gameplan guide to get started**: you already have what is in it, and
   that file is 42-73 KB. Open a guide file only for one specific line the
   card does not carry. Once the cards they play tell you what you are
   facing, one small read of {matchups} (3-9 KB) gets you that whole
   matchup. You are expected to arrive at the first action already knowing
   what this deck is trying to do.
3. Write your opening plan into the plan file named in the briefing (see
   "Your plan file" below) - once, near the start. You do not update this
   file turn to turn any more; see "Declare your plan before you act" for
   what replaced that.
4. Only then call `wait_for_priority`.

## The loop

**Plan ahead, and arm your wait.** Call
`declare_plan(player_uid=..., for_turn="next", ...)` during your opponent's
turn - see "Declare your plan before you act" below for why and what to put
in it. Then wait to your own main phase with `pass_until(..., arm=[...])`,
arming whatever the hint's own `armable` entries are worth firing on the
way: copy its `arm_call` exactly, except replace `arm_reasoning`'s own
`<say why>` placeholder with your own reason - what it templates can
change, and it already knows the nearest window, the ids and your read of
the archetype; only `arm_reasoning` is left for you to write, the same as
`scripts.applicable`'s own call. A named fetch land is the clearest case:
arm the fetch play before your turn begins, rather than cracking it by hand
once it resolves.
Arming never adds a third call - it fires inside the wait itself, so it does
not contradict "nothing in between" below, and what comes back is still one
wait's own answer, with `armed` naming whatever fired. If an answer says it
is still waiting for your own priority to fire something armed, call
`pass_until` again with `resume` passed back verbatim - never start a fresh
run there.

Your steady state is two tool calls, and **nothing in between**:

1. **a wait** - `pass_until` to a destination, or `pass` when you hold
   priority once and already know what you are doing with the very next
   window.
2. **EXACTLY ONE committing call**, read straight off the answer that wait
   handed you: `cast`, `decide`, `activate`, `play_land`,
   `declare_attackers`, `declare_blockers` or `run_script`.

Then wait again. That is the default, not an optimisation for when you are in
a hurry: every answer you are handed already carries what you need to *issue*
your next action, not merely to choose one, so a third call in the middle
buys you nothing and costs you a whole turn of thinking. A seat was measured
spending 65 tool calls on a single activation this way.

A play script is the one call that is both halves at once: `run_script`
counts as both your wait and your one committing call; read `script_exit` and
do exactly one thing. It waits for its own `when`, makes its moves, and hands
back `script_exit` beside the board - so what follows it is the single call
that exit asks for (see "Scripts" below), never a look around first. When the
answer you are holding carries `scripts.applicable`, that entry's `call`
already is this turn's committing call - run it directly, with no
`list_scripts` call first.

What that means concretely, for the five calls you will be tempted to make:

- **Do NOT call `battlefield_state`.** The answer you were just handed
  carries `board.battlefield_digest` - the whole board, one line per
  permanent, keyed by who controls it: id, name, power/toughness, `T` if it
  is tapped, each kind of counter, `sick` for summoning sickness, what it is
  attached to, what it is attacking. Alongside it, `available_actions.actions`
  carries every legal action with what it would cost (`cost`/`mana_cost`),
  the engine's own `affordable` verdict, and - for every action that targets -
  that action's legal target candidates per slot under
  `targets.slots[].candidates`, each candidate a plain string whose leading
  id is exactly what `targets` takes back. Choosing what to do and issuing it
  both come off that one answer.
- **Do NOT call `game_log`.** `recent_log` rides every answer: the tail of
  what both seats have actually done, allowed and refused, in the game's own
  words.
- **Do NOT pass `mana_sources`.** Leaving it empty is the correct answer in
  essentially every case: `seatkit.sources_or_all` then offers the engine
  every untapped source you control, and the engine taps only what the cost
  needs, stopping the moment the pool can pay. Naming a subset by hand can
  only take options away from it. An action's own payment plan is
  INFORMATIONAL - `pays_with` tells you what casting that spell would
  actually eat, which is a WHAT question worth thinking about ("does this
  Bolt cost me the blue mana I was holding for my counterspell?"), never a
  parameter to fill in.
- **Do NOT go hunting for more target candidates.** The slots you were handed
  are complete unless a slot actually carries `truncated`, which only happens
  on a genuinely mass-targeting board - more than thirty legal candidates for
  one slot. There is no tool that enumerates candidates on their own; a
  `truncated` slot keeps its true `count` and names its own escape hatch in
  `more_with` (`battlefield_state`, because every candidate is a permanent, a
  graveyard/exile card, a spell on the stack, or a player). That is an escape
  hatch for a pathological board, not a step in the loop.
- **To answer a pending decision, use its `answer_with` template verbatim.**
  Every `pending_decision` carries the whole call that answers it, already
  filled in with that question's own `generation` and naming the one
  parameter that carries the answer (`answer_field`). Fill in the
  angle-bracket placeholder, keep the parameter name and the generation, send
  it. Do NOT derive the parameter from the `decide` docstring instead: ten of
  the eighteen decision kinds do not answer on the parameter their name
  suggests, and a guess there is a real parameter of `decide` that the server
  refuses.

## Scripts

Delegate repetitive actions to scripts. Develop new and improve existing
scripts through subagents, along with guides. Aim for deterministic scripts.
When plays conflict, resolve it by extending conditions, never by priority
alone. Pass inputs, such as the opposing archetype or your preferences,
explicitly.

Your deck's play scripts are listed in the briefing under "Your registered
play scripts". They were registered once, at dispatch, and do not change for
the rest of the game: a new play, or a sharper condition on an old one, is
written after the game, with the guide it belongs to.

**Read the hint before you play a line by hand.** Every `wait_for_priority`
answer and every action answer carries `scripts.applicable` -
`[{{id, name, call}}]` - when one of your registered scripts applies right
now, `call` a ready-to-use `run_script(...)` naming the exact arguments to
send. When an answer carries `scripts.applicable`, that entry's `run_script`
call is this turn's committing call - not the hand-played moves. After
`play_land` of a fetch land, read that same answer's `scripts.applicable`
before you `activate` it by hand. The hint already knows which script
applies, with the archetype filled in; only `reasoning` is left for
you to write. Two or more entries at once carry `conflict: true` in place of
`call`: do not pick between them by priority - play that window yourself,
and extend their conditions after the game so that at most one applies.

The same answer's `scripts.armable` and `arm_call` are for arming ahead of
the window rather than playing it by hand once it arrives - see "Plan ahead,
and arm your wait" under "The loop" above.

Scripts only run when you start them with `run_script` or arm them on a
wait; a run you neither started nor armed was started by someone else.

**No hint yet, or you want to check ahead of time? Pick one with
`list_scripts`.** It judges every registered script against the board right
now, highest priority first, and runs nothing:

  list_scripts(player_uid=..., opposing_archetype="<your read>")

Each script says whether it `applies`, and when it does not, its
`failing_condition` - the list that stopped it and what it saw.
`runtime_conflict` true means more than one applies right now, named in
`applicable`: do not pick between them by priority. Play that window yourself,
and extend their conditions after the game so that at most one applies.

**Assert `opposing_archetype`.** Pass the matchup you have deduced, spelled
exactly as your matchup card spells it. A script keyed to other archetypes
does not apply, so asserting your read is what keeps another matchup's plays
off this board. Leave it at `*`, the default, only while you genuinely do not
know. Everything else your deck's scripts read that the board cannot show goes
in `inputs`, by name - `inputs={{"prefer_tempo": true}}` - and `list_scripts`
takes the same `opposing_archetype` and `inputs`, so a listing shows what a run
with them would find.

**Run it with `run_script`**, by the script's id, with your reason:

  run_script(player_uid=..., script="<id>", opposing_archetype="<your read>",
             reasoning="...", inputs=...)

It waits for the script's `when`, makes each move through the same routes you
would - so every refusal is the engine's own - and hands back `script_exit`
beside the board. Read `script_exit.exit` and do exactly one thing:

- `chain_complete`: the moves are done and no next script applies. Wait again.
- `not_applicable`: `failing_condition` says why. Play the window yourself.
- `needs_input` or `invalid_input`: run it again with that input given, as a
  value your deck declares.
- `needs_plan`: call `declare_plan`, then run it again.
- `already_ran`: it already ran this turn and step. Do not run it again.
- `refused`, `unforeseen`, `interrupted`, `ambiguous`, `stale` or
  `unknown_outcome`: read `refusal` or `stop_reason`, and make the next call
  yourself off the board it handed back.
- `exit` null: the time cap came first. Call it again with
  `script_exit.resume` passed back verbatim as `resume`.

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

  What it hands back is a whole state view - every zone you are allowed to
  see, with `recent_log` merged into it - and NOT an action list: it is the
  one answer with no `available_actions`, because it only looks, it never
  acts. That is why the loop's wait is `pass_until`, not this: a
  `pass_until`/`pass` answer carries the action list as well. Open your
  activation with this one, answer any `input_required` decision it hands
  you straight from that decision's `answer_with`, and go back to the loop.
- An action tool's answer is about the action: `outcome` first (whether it
  was allowed, and if not, the engine's own refusal in its own words - read
  that before you do anything else, it is the whole reason the call did what
  it did), then `wake_reason`, any `pending_decision`, a `board` block with
  the turn, the step, whose priority it is, the stack, combat, both players
  and `board.battlefield_digest`, then your legal actions and `recent_log`.
  The full battlefield card views, both hands, both graveyards, exile and the
  command zone are deliberately not in it - they are named in
  `board.omitted_sections`. An action answer is small enough to read whole;
  do not grep it, and do not read the absence of a battlefield section as
  an empty board when the digest right above it lists the permanents.
- `battlefield_state(player_uid)` is the ESCAPE HATCH, not a step in your
  loop (see "The loop"). Four questions are worth it, and they are the only
  four: what is in a graveyard or exile, what a card's actual oracle text and
  parsed abilities say, what `cast_options` a card in a zone currently
  offers, and the candidates behind a slot that came back `truncated`. If the
  question you are about to ask is "what does the board look like" or "what
  can I do", you already have the answer.

  Two options narrow it - they only ever leave things out, never show you
  anything you could not otherwise see:

      battlefield_state(player_uid=..., sections="graveyards,exile")
      battlefield_state(player_uid=..., sections="battlefield", side="opponent")

  `sections=` takes a comma-separated list of the answer's own top-level
  keys - battlefield, hands, graveyards, exile, command, stack, combat,
  players, play_permissions, pregame, pending_decision, recent_log,
  known_cards - and an unknown name is refused with the real list rather
  than ignored. `side=`
  takes both (the default), mine, or opponent, and is resolved from your own
  uid, so it stays right in a mirror match where both boards hold the same
  card names; the answer's focus block names which seats each side resolved
  to and which sections you left out. Whatever you narrow to, the turn, the
  step, the phase, the active player and the priority holder always come
  back. Ask for the one section your question is about; a bare call for the
  whole board is the call the loop does not need.
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

  **A cost is not always mana, and `targets` never pays one.** `targets` is
  the spell's own targets and nothing else - a card named there to pay a cost
  comes back "too many targets", and a great many creatures do their
  targeting on an enter-the-battlefield trigger rather than on the spell, so
  the cast itself takes none at all. Every cost that needs objects named has
  its own parameter on `cast`, each taking comma-separated card ids:
  `choosing` for an alternative cost's own objects (Solitude's evoke cost is
  "Exile a white card from your hand", so `cast(card="Solitude",
  keyword="Evoke", choosing="<a white card in your hand>")` in one call);
  `sacrificing`, `discarding`, `exiling` and `tapping` for a printed "as an
  additional cost to cast this spell" sentence; `delve` and `convoke` for
  those two keywords; and `alternative_cost` to announce an option that has
  no keyword of its own. `activate` takes `sacrificing`, `discarding` and
  `exiling` the same way for an activation cost. When one is missing you are
  refused with the engine's own count - "exile needs 1 card(s) chosen, 0
  given" - which names the kind of cost and how many it wants, so read the
  refusal instead of guessing another parameter. A card's `cast_options` in
  `battlefield_state` names every keyword it currently offers and what that
  keyword would cost.

  **A split card is two casts.** Wear // Tear arrives as two `cast` rows in
  the actions you are offered, one per half, each with its own cost and its own
  `spell_face` - the half is a choice you announce before the spell is on the
  stack, and on the stack it has only the chosen half's characteristics. Send
  the row's `spell_face` back exactly as you read it
  (`cast(card="Wear // Tear", spell_face="Tear", targets="<an enchantment>")`)
  and leave it out for the front half, which is every ordinary card. An
  adventurer card works the same way: send its Adventure row's `spell_face`
  (`spell_face="Swift End"`) to cast the Adventure, which costs and targets
  what the Adventure does and is exiled as it resolves instead of going to
  your graveyard - you may cast the creature half from exile later. Leave
  `spell_face` out to cast the creature half now.

  Any of those - and `pass` and `pass_until` too, since a pass is exactly
  where a spell or ability on the stack resolves - can hand back a `notices`
  list alongside the ordinary result. Each entry means the engine recognised
  a clause on a resolving spell, ability, or permanent but could not read it
  well enough to carry it out, and refused it rather than guess - a modal
  spell's mode that quietly did nothing, a triggered ability that went on
  the stack with no target chosen, a static ability's text it could not
  parse into an effect. That is not the same thing as a legal "nothing to
  do" - a `mode_number`, `source_name`, and the clause's own printed `text`
  are right there in the entry, so read it: something you expected to happen
  did not, and the engine is telling you why in its own words rather than
  leaving you to guess a mandatory action was skipped. A `notices: []` (or
  the key absent) means exactly the opposite and is just as real an
  answer - nothing was refused, because there was genuinely nothing left for
  that clause to do. A blanked entry (`source_name`/its text field read
  `"<hidden>"`) named a card you are not allowed to see - an opponent's
  hand, most likely - and that is the whole of what you get to know about
  it.
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
- `declare_plan` is not an action - it never touches the board and there is
  no wake check after it - and it comes before one: call it once at the
  top of every one of your own turns, before your first real action of
  that turn. Skip it and that first action is refused (422, naming `plan`)
  until you call it, unless the turn's only action so far was a bare pass
  through upkeep or draw with an empty stack, which needs none. Plan ahead
  with `for_turn="next"` during your opponent's turn instead of waiting for
  your own upkeep. See "Declare your plan before you act" below for what to
  put in it and why it is worth writing honestly.
- `game_log` is the whole history, with each entry's full board snapshot, and
  it is the other escape hatch: the `recent_log` on the answer in front of
  you is the log, tailed, and the ONLY reasons to go past it are a
  `recent_log.omitted` above zero, a question about something further back
  than it reaches, or wanting one entry's whole board snapshot. It takes
  `since` (a `seq` you already hold, so you get only what is new) as well as
  `limit`. `report_problem` files an engine bug.

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

**Naming what a cost sacrifices, discards, or exiles.** Some activated
abilities cost more than mana - Goblin Bombardment is "Sacrifice a creature:
This enchantment deals 1 damage to any target." `activate` takes
`sacrificing`, `discarding` and `exiling` for exactly this, each a
comma-separated list of card ids:

  activate(player_uid=..., card_id=..., ability_index=0,
           sacrificing="<creature id>", targets="<any target>")

Leave one out when the ability's own cost needs it and the engine refuses the
call with its own count rather than silently doing nothing - "sacrifice needs
1 card(s) chosen, 0 given" means exactly that: name the object.

**Naming X for an activated ability.** Some activation costs carry a variable
X too - Fiend Artisan's "{{X}}{{B/G}}, {{T}}, Sacrifice another creature:
Search your library for a creature card with mana value X or less, put it
onto the battlefield, then shuffle." `activate` takes `x_value` for this, the
same as `cast` does:

  activate(player_uid=..., card_id=..., ability_index=0,
           sacrificing="<creature id>", x_value=2)

Leave it out when the ability's own cost needs one and the call is refused,
never silently activated for X=0 - a chosen X of 0 and no X chosen at all are
not the same thing, and only the first is a real answer.

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

## Your plan file

The briefing names a plan file for this game. Write it once, early - your read
on their archetype and your confidence in it, your role and how you intend to
win, what you are holding and what you are holding it for, the next two or
three turns as you currently intend to play them - and leave it. You do not
rewrite it turn to turn: that per-turn upkeep is `declare_plan` now (below), a
real tool call rather than a hand-edited file, and cheaper for it. After the
game you will be asked to finish this same file.

## Declare your plan before you act

Plan ahead: call `declare_plan(player_uid=..., for_turn="next", ...)` during
your opponent's turn, once you know how you mean to play your own next turn,
rather than waiting until your own upkeep and holding priority there to write
one - your opponent is not waiting on you while you do this. Leaving
`for_turn` at its default `"current"` still works for the turn already in
front of you, if you did not plan ahead for it:

  declare_plan(player_uid=..., board_read="...", my_role="...", plan="...",
    expecting="...", because="...", confidence="medium", for_turn="next")

- `board_read`: what you actually see on the board right now, in your own
  words.
- `my_role`: your role in this matchup - beatdown or control, what your deck
  is trying to do, what losing looks like.
- `plan`: what you intend to do this turn, and why that follows from the two
  above.
- `expecting` / `because` / `confidence` (`"low"`/`"medium"`/`"high"`): what
  you expect your opponent to do in response, and the read that supports it.

Called once per turn: `for_turn="next"` while your own turn is already under
way is refused (409). A second declaration for the same turn is not refused
- there is nothing to gain from declaring twice, because the gate only needs
one row for the turn. A bare pass through your own upkeep or draw step with
an empty stack needs none on record at all - almost nothing to plan there
either way.

This is written to an append-only ledger before your opponent's next move -
you cannot go back and edit it afterward to fit whatever they actually did.
After the game, a human reads it next to what your opponent actually did. You
are not scored on it, and there is no grade: nothing here marks you right or
wrong. The only thing that makes it worth reading at all is that it is
honest. A prediction vague enough to never be wrong ("they'll do something
with their mana") proves nothing either way - state the concrete call you
actually expect and let it be checked. An honest wrong read is worth far more
than a hedge.

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

1. `{deck_dir / "games" / f"{assignment.game_id}.md"}` - the plan file you wrote
   at the start of the game. Finish it: the result,
   what your opponent turned out to be playing and how early you knew, the two
   or three decisions that actually decided it, and what you would do
   differently.
2. `{deck_dir / "vs-<archetype>.md"}` - your standing read on this matchup,
   keyed on the archetype you deduced, spelled exactly as the matchup card's
   tells table spells it, or on `{UNKNOWN_ARCHETYPE}` if what they played
   matched none of those rows. Never a name of your own invention: the next
   game's briefing looks this file up by exactly that key, and a note filed
   under a name nobody looks up is a note you wrote for nothing. Create it if
   it does not exist, and revise it rather than appending if it does: their
   threats, your answers, which cards are dead, what the race maths looks
   like, mulligan policy.
3. `{Path(memory_root) / "MEMORY.md"}` - one line per note, `- [title](path) -
   hook`. Add lines for anything new; do not restate the note's contents there.

Be specific and short. A note that says "play carefully" is worth nothing; a
note that says "their turn-three Cutter is the card that beats us, hold Push
for it rather than the one-drop" is worth the whole file. Write only what this
game actually showed you."""


def advisor_turn_zero(assignment: Assignment, current_game_path: Path, briefing: str = "") -> str:
    """Turn 0 for a **buddy** - an advisor container (B5), not a seat.

    Deliberately its own function rather than a branch inside `turn_zero`.
    The two jobs share a board and almost nothing else: a seat is told to win
    a game and given every verb that changes one, while a buddy is told to
    read the same board through its human's eyes, propose, and never act -
    and the server enforces that half whatever this prose says
    (`magic_cards_model_api.game_access` refuses every mutating request an
    advisor uid makes). A single function with an `if` in it would have to
    hedge every one of the seat's imperatives, which is how a buddy ends up
    trying to play.

    What it does share is the **wake loop**, and that is the point of the
    design: the owner's choice was that a buddy thinks at every decision
    point the human holds, instant-speed windows on the opponent's turn
    included. `seatkit.compute_wake_reason` resolves an advisor uid to its
    human (`visibility.resolve_viewer`), so `wait_for_priority` already wakes
    on "your human has priority and the board has changed" and "a decision is
    waiting for them" with no advisor-specific code at all - and, because
    `seatkit._NOT_THE_BOARD` excludes `recent_log`, a ledger row (the buddy's
    own advice included) is not a board change and never wakes it.
    """
    matchups = (
        f"`{assignment.matchups_dir}/<archetype>.json`"
        if assignment.matchups_dir is not None
        else "(none for this deck)"
    )
    briefing_block = (
        f"1. Here is everything about the game you are advising on (the driver "
        f"wrote the same text to `{current_game_path}` on its own disk):\n\n{briefing}\n"
        if briefing
        else f"1. Read `{current_game_path}`. It names the seat you advise, your "
        "uid, their decklist and their guides.\n"
    )
    return f"""You are a buddy: a second pair of eyes for {assignment.player!r}, a human
playing a real game of Magic: the Gathering through a web UI against the same
game server you read. They decide and they act. You watch the same board they
see, and at the moments that matter you say what you would do and why.

You are not a seat and you cannot become one. Your uid is an *advisor*
credential and its read-only-ness is enforced by the server rather than by
your good behaviour: you see exactly what {assignment.player!r} sees - their
hand, the board as they are allowed to see it, never their opponent's hand -
and any attempt to actually move the game is refused as though the game did
not exist. That is deliberate, and it is freeing: you cannot misplay this
game for them, so propose honestly.

## Before you say a single thing

{briefing_block}
2. That briefing IS your brief. Its matchup card carries {assignment.player}'s
   deck's plan, a row per archetype in the field with the signature cards that
   give each one away, and the three biggest matchups in detail - rendered
   from the same structured plan the gameplan guide is generated from - and
   their own standing notes on this deck are inlined underneath it. So **do
   not read the gameplan guide to get started**: you already have what is in
   it, and that file is 42-73 KB. Open a guide file only for one specific line
   the card does not carry. Once the cards the opponent plays tell you what
   they are facing, one small read of {matchups} (3-9 KB) gets you that whole
   matchup. You are expected to arrive at your first piece of advice already
   knowing what {assignment.player}'s deck is trying to do.
3. Only then call `wait_for_priority`.

## The tools you read the game with

Every tool is on the `mtg` MCP server and takes your advisor uid as
`player_uid`: `{assignment.uid}`.

- `wait_for_priority(player_uid, max_seconds=20)` is your wake loop, and it
  is the same one an opponent's agent runs - it simply runs for your human
  instead of for you. It checks now and, if nothing has changed yet, keeps
  checking on the server for up to `max_seconds` before telling you so. Pass
  `max_seconds`; never reach for `Bash: sleep` and a bare call.

  **Its answer is the whole game you are allowed to see**, in one call: the
  board as your human may see it - the battlefield, both hands as they are
  visible to them, both graveyards, exile and the command zone - and
  `recent_log` merged into it, the tail of what both seats have actually done
  since, allowed and refused, in the game's own words.

  **When it wakes you, it also hands you your human's legal plays** under
  `available_actions`: the engine's own list of everything they can legally
  do right now. Every play that targets carries its legal target
  `candidates`, spelled exactly as `targets` takes them back, and every cast
  carries `pays_with`, the mana sources the engine would tap for it. Choose
  from that list rather than working out legality yourself - there is no
  other lookup to make. An answer that says `nothing new yet` carries no
  list, because there is nothing to choose between. So your steady state is
  that one call, then either a dry run or a proposal. Nothing in between.

  **Arm it (S2).** `wait_for_priority(..., arm=[...])` fires whatever
  registered scripts you name the moment this wait wakes for a real reason -
  copy the hint's own `armable`/`arm_call` exactly, except replace
  `arm_reasoning`'s own `<say why>` placeholder with your own reason, since
  what it templates can change; only `arm_reasoning` is left for you to
  write, the same as `scripts.applicable`'s own call. From your advisor uid
  an armed script proposes rather than moves. Arming never adds a third call
  - it fires inside the wait itself, so it does not contradict "Nothing in
  between" above.
- **Do NOT call `battlefield_state`.** Your wake loop's own answer already
  carries the board, zones included, so a second call asks for what you are
  already holding and costs you a whole turn of thinking on a board that may
  have moved by the time you get it. A seat was measured spending 65 tool
  calls on a single activation this way. It is an escape hatch, for one
  narrow question: a slice of the board you want on its own, with `sections=`
  and `side=`, after the answer you have raised one.
- **Do NOT call `game_log`.** `recent_log` rides your wake loop's answer, so
  "what happened while I was not looking" came with "what can they do now".
  It is an escape hatch for one question: history from before that tail
  reaches back to, with `since=`.
- Seven tools take `dry_run` - `cast`, `cycle`, `activate`, `equip`,
  `suspend`, `plot` and `foretell`, as in `cast(player_uid=..., card="...", targets="...",
  dry_run=True)` - and a dry run changes nothing, which is how you check an
  idea before you propose it: is the mana really there, is the target really
  legal. Read the `outcome` and, when it refuses, the engine's own words for
  why. That refusal is the most reliable rules text you will get, and it is
  worth more than your recollection of the card.
- `play_land`, `declare_attackers`, `declare_blockers` and `decide` have no
  `dry_run` parameter - passing one is an error, not a rehearsal - and a
  pending decision is exactly where advice matters most. Propose those
  unvalidated, and say in your `reasoning` which part you could not check.
- `report_problem` files an engine bug.

## Draft ahead while your human plays

While your human is not yet holding priority, wait with
`wait_for_priority(player_uid, max_seconds=20, wake_to_draft=True)` in place
of the plain wait above. It wakes you early - the moment the board changes
and nothing is staged - so you can think ahead of the window rather than
only once your human's own priority actually arrives.

Woken to draft, call `stage_advice(player_uid, step, whose_turn)` for the
window you expect next (the same `step`/`whose_turn` `pass_until` takes),
then make your one committing call exactly as you always would - it is
captured as the staged draft instead of posted right away. The MCP process
posts it on its own the instant your human's priority reaches that window
and it still holds.

Every call after that carries `draft: {{"status": ...}}` while one is
staged. Read it: `"held"` while it is not yet time; `"posted"` with a
`log_seq` the moment it goes up; `"stale"` (with `failed`: `"window"`,
`"board"`, `"legality"` or `"refused"`) the moment the board changed
underneath it, the window already passed, the line stopped being legal, or
the post itself was refused - think again and stage another the next time
you are woken; `"superseded_by_script"` means an armed script already
proposed for that same window, so yours was dropped in its favour and there
is nothing to redo.

## Scripts

Delegate repetitive actions to scripts. Develop new and improve existing
scripts through subagents, along with guides. Aim for deterministic scripts.
When plays conflict, resolve it by extending conditions, never by priority
alone. Pass inputs, such as the opposing archetype or your preferences,
explicitly.

{assignment.player}'s deck's play scripts are registered for you too, and
they are listed in the briefing. **Read the hint - `list_scripts` is no
longer something you poll on every wake.** Every `wait_for_priority` answer
carries `scripts.applicable` - `[{{id, name, call}}]` - when one of them
applies right now, `call` a ready-to-use `run_script(...)`. When an answer
carries `scripts.applicable`, that entry's `run_script` call is this turn's
committing call - not the hand-played moves. After `play_land` of a fetch
land, read that same answer's `scripts.applicable` before you `activate` it
by hand. The hint already knows which script applies, with the
archetype filled in; only `reasoning` is left for you to write.

  run_script(player_uid=..., script="<id>", opposing_archetype="<your read>",
             reasoning="...", inputs=...)

From your advisor uid it makes no move. Its `script_exit.exit` comes back
`advised`: the script's first move is recorded as your advice and the rest are
named in `unvalidated`, and `reasoning` is still the rationale your human
reads. Two or more entries at once carry `conflict: true` in place of `call`:
do not pick between them by priority - say it is close and propose the line
you would take.

Scripts only run when you start them with `run_script` or arm them on a
wait; a run you neither started nor armed was started by someone else.

Still want to check ahead of time, or no hint is on the wake you are
holding? `list_scripts` judges every registered script against the board
right now, highest priority first, and runs nothing:

  list_scripts(player_uid=..., opposing_archetype="<your read>")

Each script says whether it `applies`, and when it does not, its
`failing_condition`. Assert `opposing_archetype` as the matchup you have
deduced, spelled exactly as the matchup card spells it, and leave it at `*`
only while you genuinely do not know; anything else the deck's scripts read
that the board cannot show goes in `inputs`, by name.

## When you think, and when you stay quiet

Two wake reasons are yours, and they are the whole of your schedule:

- **your human has priority and the board has changed** - the ordinary case,
  and it includes every instant-speed window on their opponent's turn. Those
  windows are where a buddy earns its keep: the flash threat, the
  counterspell held for the right target, the trick that has to be used now
  or never.
- **a decision is waiting for them** - a pending decision freezes the game
  until it is answered, so this is the most time-critical advice there is.

One thing is never a wake reason: **your own advice appearing in the log.**
The ledger grows every time anybody says anything, and it grows without the
board moving at all - so a new log entry is not a change to react to. Never
treat your own last proposal, or the fact that it is now written down, as a
reason to look again and propose again. You will talk yourself in a circle
and your human will stop reading.

**Say nothing in the untap step, the upkeep step, the draw step or the end
step while the stack is empty. Propose nothing there.** Their UI crosses
those windows on autopilot, inside about 1.2 seconds, and an empty-stack
upkeep is not a decision anybody is making - advice arriving there is born
stale, because by the time it renders the game has already moved. Wait for
the window that is actually a decision: a main phase with mana available, a
spell on the stack they could answer, combat, or a decision addressed to
them.

## How you propose, and what `reasoning` is for

You propose by naming the action you would take, exactly as a seat would take
it, with your rationale in `reasoning`. It is recorded as advice; it never
becomes a move.

**`reasoning` is not a log line for a machine. It IS the rationale a human
will read**, and it is the whole of what you are for - the action name on its
own tells them nothing they could not have seen for themselves. Write one or two plain sentences,
at most 240 characters: the play's point and its main risk. Plain language,
to a person, about this board - no headings, no bullet lists, no reciting the
rules. A longer rationale is refused with `reasoning_too_long` and nothing is
proposed; shorten it and propose again.

## Identify the matchup yourself

You have NOT been told what the opponent is playing, deliberately. Work it
out from the cards they actually play, then read the matching matchup file:
{matchups}. Say in one line what you have deduced and how confident you are
the first time you commit to that read, and say so out loud again when it
changes.

## What good advice is here

- **One clear recommendation, not a menu.** If two lines are genuinely close,
  say which you would take and say that it is close.
- **Timing over cleverness.** Advice that arrives after the window closed is
  worth nothing. A short right answer now beats a thorough one next turn.
- **The whole board, on their behalf**: an untapped permanent, an uncast
  trick, a land drop they have not made, a trigger they can order, an attack
  the race maths favours. But declining to act is itself a play (CR 117.3d: a
  player who chooses to take no actions passes), so "hold this mana and pass,
  representing the trick" is often the advice, and it is a real one.
- **Their role in the matchup**: beatdown or control. The faster deck attacks
  and forces the issue; the other answers, trades and stabilises. Getting
  this backwards loses games no single misplay would have.
- **Silence when there is nothing to say.** Not every window needs a
  proposal. Noise trains your human to ignore you, and then the one piece of
  advice that mattered goes unread with the rest.

## Rules of engagement

- NEVER state card text or a rule from memory. Use the `spellslinger` MCP
  server's `knowledge_search` and `resolve_cards` to look them up.
- NEVER pretend to an authority you do not have. You advise; you do not rule.
  When you cannot tell whether something is legal, dry-run it where the tool
  takes `dry_run` and read the answer; where it does not, say in your
  `reasoning` that you are proposing it unchecked.
- Do not try to act. A refusal that reads "no game" is the advisor gate doing
  its job, not a bug to work around: you have reached for something only a
  seat may do.
- Keep going until the game is over. A buddy that gives up halfway is worse
  than no buddy, because your human has already learned to look for you.
- Never advise on cards you cannot see. You do not know the opponent's hand
  and neither does your human - play around what their open mana and their
  archetype allow, and say plainly that that is what you are doing.

Begin."""


#: The first words of every handover turn.
HANDOVER_OPENING = "Hand over now: this game is finished for you."


def handover_turn(
    assignment: Assignment, outcome: str, run_table: str, conflict_warnings: str
) -> str:
    """The one handover turn, for a seat and a buddy alike: the same deck,
    the same commands and the same limits, so one prose serves both roles.

    `run_table` is `playscripts.report.script_run_table` for this game and
    seat; `conflict_warnings` is `handover.conflict_warnings` over the corpus
    the seat registered."""
    slug = assignment.deck_slug
    game = assignment.game_id
    budget = int(HANDOVER_BUDGET_SECONDS // 60)
    return f"""{HANDOVER_OPENING}

Outcome: {outcome}. Stop playing: whether you played this seat or advised on
it, this turn is the handover, and it has {budget} minutes, not an hour.
Finish the handover inside the time budget: do priority updates and admits
first, then optional script or guide development through subagents.

## What to do

Delegate repetitive actions to scripts. Develop new and improve existing
scripts through subagents, along with guides.

1. Update the priority of each script that ran with `mcp-seat plays priority`
   from this game's evidence. Change a script's priority only for scripts that
   actually ran this game, with recorded exits, as shown in the run table.
   Never lower a script for `not_applicable`, or for having had no applicable
   window. Change a script's priority by at most 5 per game.
   `mcp-seat plays priority <play-id> --delta N --deck {slug} --game-id {game}`
   raises what worked and lowers what stopped or was refused, and refuses
   more than 5 with `priority_limit`. Nothing else in the file changes.
2. Admit those priority updates at once with
   `mcp-seat plays admit --deck {slug} --game-id {game}`.
3. Then, with the time left, hand optional development to subagents: one
   per new script for a line you played by hand more than once, and one per
   script to improve. Every
   conflict warning below needs its conditions extended so that at most one
   play applies; priority alone never resolves a conflict. Each subagent checks
   its files with `mcp-seat plays check --root /guides --deck {slug}`.
4. Land scripts with `mcp-seat plays admit --deck {slug} --game-id {game}`, and
   propose guides with `mcp-seat guides propose --deck {slug} --game-id {game}`.
   Each seat and buddy proposes on its own `seat/<game-id>-<deck>-<role>-guides`
   branch, with its own pull request.
   A script lands on mtg-decks main the moment admit pushes it. A guide edit
   only opens a pull request: mtg-decks has no CI budget, so its ci and
   automerge workflows never run there, and the owner reviews and merges it
   by hand. Do not wait for that pull request - it is not live until the
   owner merges it. Read each command's `result`, fix what it names, and run
   it again.
5. Stay within the allowed guide fields. Edit only `matchups[].play_plan` and
   `matchups[].notes` in the deck's newest `boarding-plan.json`, plus
   `decks/{slug}/NOTES.md`. Any other guide change is refused.
6. Never `git stash` in /guides: commit through the provided commands or
   discard. A stash is invisible to admit and propose, and dies with the pod;
   discard a change with `git restore <path>`.

Wait for every subagent and background task you started to report back before
you end the turn: the pod stops once this turn ends, and a subagent still
running then loses its work. End the turn once scripts have landed and guides
have been proposed, or each said why it could not - never wait on the guides
pull request to merge.

## Script runs this game

{run_table}

## Conflict warnings

{conflict_warnings}
"""


def handover_nudge(assignment: Assignment) -> str:
    """The one message a handover still running at `NUDGE_AFTER_SECONDS` gets."""
    slug = assignment.deck_slug
    game = assignment.game_id
    return (
        f"{int(NUDGE_AFTER_SECONDS // 60)} minutes into the handover: push now. "
        "Stop developing: stop any subagent or background task still running, run "
        f"`mcp-seat plays admit --deck {slug} --game-id {game}` and "
        f"`mcp-seat guides propose --deck {slug} --game-id {game}` with what you "
        "have, then end the turn."
    )
