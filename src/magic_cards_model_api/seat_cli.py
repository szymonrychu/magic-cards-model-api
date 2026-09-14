"""One command per game action, from one player's seat, for shell-driven play.

The same seat `mcp.py` exposes, sharing the same "one call, one meaningful
action" flow (`seatkit.run_action`): the same hidden-information filter - the
opponent's hand and both libraries never appear - and the same
cast/play-land/activate/attackers/blockers/decide/pass behaviour: perform the
action, then check once for this seat's next moment to act (poll-and-return -
see `seatkit.block_until_wake`'s own docstring), printing the outcome and
whatever state that check saw together. `wait` checks the same way with no
action of its own; run it again to keep checking.

Priority has exactly two verbs here, `pass` and `pass-until`, because the
rules give it exactly two: CR 117.1 lists what the player with priority may do
("cast spells, activate abilities, and take special actions") and CR 117.3d
has a player who chooses none of them pass. Resolving the stack's top and
ending a step are what CR 117.4 makes happen once everyone has passed in
succession, and CR 703.2 says turn-based actions "are not controlled by any
player" - none of them is a move a seat makes, so none of them is a
subcommand.

Whether priority is released on your behalf first is NOT uniform, and this
file and `mcp.py` must agree on it command for command:

- `play-land`, `suspend`, `plot`, `foretell` and `cycle` HOLD priority
  whether `--hold` was given or not (`SPECIAL_ACTIONS`, below, carries the
  rules for why);
- `activate` holds whenever the ability turned out to be a mana ability,
  because a mana ability never uses the stack (CR 605.3b) - decided on the
  answer, in `seatkit.run_action`, since the command cannot know in advance;
- everything else releases priority unless `--hold` is given, which is right
  for them: they use the stack, and releasing is how what you just did
  resolves.

Meant for subagents that play through Bash rather than through an MCP client:

    uv run --directory <api repo> python -m magic_cards_model_api.seat_cli \\
        --uid <seat-uid> cast "Lightning Bolt" --target bob

Every command prints the API's answer (or the filtered state) as JSON text on
stdout. Stderr carries one extra note: when the answer arrives with a pending
look-then-split (`scry`/`surveil`), `split_destinations` says where each half
of that answer actually goes, in the asking card's own words. Two short
strings inside a whole state dump are easy to miss, and these two decide
which zone the cards end up in - the decision's kind does not.

`script run SCRIPT` and `script list` are `mcp.run_script` and
`mcp.list_scripts`, argument for argument (`--opposing-archetype`, `--resume`,
`--wait/--no-wait`, `--max-seconds`, and `--input NAME=VALUE` for `inputs`,
repeatable), both through `playscripts.script_tools`; like every seat command
they take `--uid`.

Two commands are not seat actions at all, and need no `--uid`:

- `plays check [--root PATH] [--deck SLUG] [--json]` lints a guides
  checkout's play scripts offline, and exits 0 when clean (conflict warnings
  allowed) or 1 with coded errors - the same codes as mtg-decks'
  `scripts/check_plays.py`.
- `plays priority`, `plays admit` and `guides propose` work the pod's guides
  clone (default `/guides`): edit a play's priority, take play-script changes
  to mtg-decks main, and propose a matchup-plan edit as a pull request the
  owner reviews and merges by hand (mtg-decks is private with no CI budget,
  so its `ci`/`automerge` workflows never run; `guides propose` never waits
  on either). Each prints one JSON result and exits 0 on admitted / pr_opened
  / awaiting_owner_review / nothing_to_admit (or a priority set), 1
  otherwise; see `playscripts.admit` and `playscripts.guides_propose`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import seatkit
from .playscripts import admit as playscripts_admit
from .playscripts import guides_propose as playscripts_guides
from .playscripts import lint as playscripts_lint
from .playscripts import script_tools
from .playscripts.gitwork import GUIDES_ROOT, SUCCESS
from .seatkit import (
    AUTOPASS_STEPS,
    AUTOPASS_WHOSE_TURN,
    DEFAULT_API,
    Seats,
    _parse_ids,
    _parse_pairs,
    api_trouble,
    autopass_target_error,
    card_reference,
    parse_convoke,
    parse_optional_costs,
    pass_until,
    run_action,
    wait_and_arm,
)

#: `seat_cli.py`'s own root, for the dev-checkout fallback below - the same
#: `Path(__file__).resolve().parents[N]` idiom `scryfall.cache_dir` uses for
#: its own default.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: S2 A6 review fix 2 (2026-09-14): env override for where the CLI persists a
#: staged advisor draft between separate CLI invocations - see `_cli_drafts_
#: dir`'s own docstring for why this exists at all.
CLI_DRAFTS_DIR_ENV = "MCM_CLI_DRAFTS_DIR"


def _cli_drafts_dir() -> Path:
    """Where a staged advisor draft is persisted between CLI invocations.

    Each `run()` call is a fresh process with a fresh `Seats()`, so `Seat.
    _advice_drafts` (an in-process dict, shared only by reference within one
    `Seats` instance - see its own docstring) never survives past the one
    command that populated it: `stage-advice` printed "staged" and the next
    CLI `cast` posted immediately, and CLI `wait` never resolved a draft at
    all, because there was nowhere for one to have been carried from.

    `CLI_DRAFTS_DIR_ENV` names the directory outright. Otherwise: the
    continuous PTY-wrapped agent's own per-seat workspace - `WORKSPACE`
    (`Dockerfile.agent`, `docker-compose.yml` and `deploy/seat-job.yaml` all
    set it to `/workspace`, the same directory `/workspace/memory` already
    lives under) is this seat's own container's place for exactly this kind
    of cross-call state. A dev checkout with neither set falls back to a
    repo-relative directory, the same fallback `scryfall.cache_dir` uses for
    its own cache."""
    env = os.environ.get(CLI_DRAFTS_DIR_ENV)
    if env:
        base = Path(env)
    else:
        workspace = os.environ.get("WORKSPACE")
        base = Path(workspace) / "cli-drafts" if workspace else _REPO_ROOT / ".cli-drafts"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _cli_draft_path(game_id: str, uid: str) -> Path:
    """The file one uid's staged draft for one game lives at - keyed by both,
    same as `Seats.advice_drafts` is keyed by uid alone within one game's own
    process (a CLI process only ever pins one game at a time, but the file
    system is shared across every game this seat's workspace has ever
    touched)."""
    return _cli_drafts_dir() / f"{game_id}__{uid}.json"


def _tuplify(value: Any) -> Any:
    """Reverse a JSON round trip's list-ification of every tuple, recursively.

    `opponent_board_signature` builds its answer out of nested tuples, and
    `resolve_advice_draft`'s board check compares one against a freshly
    computed one with plain `==` - `[1, 2] != (1, 2)` in Python, so a draft
    read back from a JSON file would fail that comparison on shape alone
    (every list, not on what it actually holds), reading every draft as
    stale on its very first poll after a restart. Only `entry["opponent_
    signature"]` needs this - every other JSON-round-tripped field
    (`window`, `action`) is compared field by field or not compared at all."""
    if isinstance(value, list):
        return tuple(_tuplify(item) for item in value)
    return value


def _load_cli_draft(game_id: str, uid: str) -> dict[str, Any] | None:
    """The draft a previous CLI invocation left staged for `uid`, or `None`
    for no file, an unreadable one, or one that is not a JSON object - all
    three read the same as "nothing staged", never an error a command run
    fails on."""
    path = _cli_draft_path(game_id, uid)
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    if not isinstance(loaded, dict):
        return None
    if "opponent_signature" in loaded:
        loaded["opponent_signature"] = _tuplify(loaded["opponent_signature"])
    return loaded


def _save_cli_draft(game_id: str, uid: str, entry: dict[str, Any] | None) -> None:
    """Write `uid`'s current draft for the next CLI invocation to pick up, or
    remove the file outright when `entry` is `None` (nothing staged any
    more - `resolve_advice_draft` posted or dropped it, or this uid never
    staged one to begin with)."""
    path = _cli_draft_path(game_id, uid)
    if entry is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps(entry))


def split_destinations(state: dict[str, Any] | None) -> str:
    """One line naming where each half of a pending look-then-split answer
    goes, or "" when there is nothing to say.

    A seat reading a whole state dump can miss two short strings buried in
    it, and these two are the ones it cannot afford to miss: the engine
    reuses the `scry` kind for every look-then-split, so the kind does not
    say where anything goes. Devourer of Destiny's first-upkeep trigger keeps
    the card named in `--moved` on top of the library and exiles the rest,
    which is the reverse of a scry on both halves. `moved_label` and
    `kept_label` carry the asking card's own words, so they are said out
    loud here rather than left for the reader to find.

    Printed to stderr by the caller, never to stdout: every command's stdout
    is JSON another program may parse, and this is a note to the human or
    agent at the keyboard.
    """
    pending = (state or {}).get("pending_decision")
    if not isinstance(pending, dict):
        return ""
    moved, kept = pending.get("moved_label") or "", pending.get("kept_label") or ""
    if not moved and not kept:
        return ""
    halves = []
    if moved:
        halves.append(f'the ids you pass to --moved: "{moved}"')
    if kept:
        halves.append(f'the ones you do not name: "{kept}"')
    source = pending.get("source")
    asking = f" from {source}" if source else ""
    return (
        f"[{pending.get('kind')} decision{asking}] " + "; ".join(halves) + "."
        " These are pending_decision.moved_label/kept_label, the asking card's"
        " own words - they outrank anything the decision's kind implies."
    )


def parse_inputs(values: list[str] | None) -> dict[str, Any] | None:
    """`--input NAME=VALUE` flags as the `inputs` mapping `mcp.run_script`
    takes, or None for none. VALUE is read as JSON where it is JSON (`true`,
    `3`, `"3"`) and kept as text otherwise (`dimir-frog`), so a bool or int
    input arrives typed. Raises `ValueError` for a flag with no name or no
    `=`, and for a name given twice."""
    if not values:
        return None
    parsed: dict[str, Any] = {}
    for raw in values:
        name, equals, value = raw.partition("=")
        name = name.strip()
        if not equals or not name:
            raise ValueError(f"--input {raw!r} is not NAME=VALUE")
        if name in parsed:
            raise ValueError(f"--input names {name!r} twice: give each input once")
        try:
            parsed[name] = json.loads(value)
        except ValueError:
            parsed[name] = value
    return parsed


#: `cast`'s and `activate`'s `--sources`. Only the sources named are sent, so a
#: log row never names a land offered to a cost with no mana in it (B5-05).
NAMED_SOURCES_HELP = (
    "comma-separated ids of the mana sources to pay with; only the sources named "
    "are sent. Omit it (or pass '') to name none: the engine's automatic payment "
    "then taps only what the cost needs from your untapped permanents, and the log "
    "names only the permanents that actually paid"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--uid",
        default=None,
        help="your seat uid, given at dispatch; required by every command but `plays` and `guides`",
    )
    parser.add_argument("--game", default=None, help="game id; default: the live game")
    parser.add_argument("--api", default=DEFAULT_API, help=f"API base url; default {DEFAULT_API}")
    parser.add_argument(
        "--reasoning",
        default=None,
        help=(
            "why you are doing this, in your own words - required by the API for "
            "an agent seat on every mutating command (T10/seatkit-4)"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("state", help="the game as you may see it, right now")

    wait_cmd = sub.add_parser(
        "wait",
        help=(
            "check whether it is your moment to act, and return - checks once "
            "and returns at once by default; pass --max-seconds to wait, "
            "bounded, instead of sleeping and calling again by hand"
        ),
    )
    wait_cmd.add_argument(
        "--max-seconds",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "if nothing has changed yet, keep checking on the server for up "
            "to this many seconds (capped at seatkit.MAX_WAIT_SECONDS) before "
            "giving up and printing 'nothing new yet' anyway - the command "
            "still always returns on its own, never past the bound. Use this "
            "instead of `sleep N && ... wait`: it is the one call that used "
            "to take two. Left at 0 (the default), this is the plain "
            "check-and-return it has always been"
        ),
    )

    def arm_flags(command: Any) -> None:
        """S2: `--arm`/`--arm-reasoning`/`--opposing-archetype`/`--input`,
        shared by `wait` and `pass-until` - the same `playscripts.arming.arm`
        arguments `mcp.wait_for_priority`/`mcp.pass_until` take. Arming fires
        only once this command is about to return with a real stop: after
        `wait`'s own check wakes for a real reason, or when `pass-until`
        finishes (or hits its own pass cap) - never on a retryable hand-back
        where the opponent still holds priority."""
        command.add_argument(
            "--arm",
            dest="arm",
            action="append",
            default=None,
            metavar="SCRIPT_ID",
            help=(
                "a registered script id to arm, in priority order; repeat for each. "
                "If it already applies the moment this command is about to return, it "
                "fires on its own (playscripts.arming.arm) - never mandatory, and never "
                "a reason to wait longer than this command already would"
            ),
        )
        command.add_argument(
            "--arm-reasoning",
            dest="arm_reasoning",
            default="",
            metavar="TEXT",
            help="why you are arming these, in your own words - required whenever --arm is given",
        )
        command.add_argument(
            "--opposing-archetype",
            dest="opposing_archetype",
            default="*",
            metavar="KEY",
            help="the opponent's archetype as you read it, judged the same as run_script's own",
        )
        command.add_argument(
            "--input",
            dest="arm_inputs",
            action="append",
            default=None,
            metavar="NAME=VALUE",
            help="one input an armed script's conditions read; repeat for each (see script run)",
        )

    arm_flags(wait_cmd)

    def priority_flag(command: Any) -> None:
        """The one flag every action shares: keep priority instead of
        releasing it.

        `--max-seconds` is not this flag's business - it lives only on `wait`
        and `pass-until`, the two commands that can genuinely have nothing to
        do but wait for the opponent. An action performs, releases priority
        unless `--hold`, then checks once for this seat's next moment to act
        and prints what it saw (`seatkit.run_action`, poll-and-return) - that
        one check is instant either way.
        """
        command.add_argument(
            "--hold",
            action="store_true",
            help=(
                "keep priority instead of releasing it (CR 117.3c: you already "
                "keep it after acting). Neither way waits for anything - run "
                "the command again to keep checking. play-land, suspend, plot, "
                "foretell and cycle hold with or without this; activate holds "
                "with or without it when the ability was a mana ability"
            ),
        )

    cast = sub.add_parser("cast", help="cast a spell (hand, or graveyard/exile with --keyword)")
    cast.add_argument("card", help="card name or id")
    cast.add_argument(
        "--spell-face",
        default="",
        help=(
            "which face this cast announces - a half of a split card (CR 709.3) "
            "or an adventurer card's Adventure (CR 715.3) - by that face's "
            "printed name: --spell-face Tear for Wear // Tear's back half, "
            "--spell-face 'Swift End' for Murderous Rider's Adventure. "
            "The value is the engine's own: it is the `spell_face` string on the "
            "action row you are taking, handed back unchanged. Omit it for the "
            "front half, which is every single-faced card and an adventurer "
            "card's own creature half"
        ),
    )
    cast.add_argument(
        "--target",
        default="",
        help="comma-separated card ids, player names, or ids of abilities on the stack",
    )
    cast.add_argument("--sources", default="", help=NAMED_SOURCES_HELP)
    cast.add_argument("--modes", default="", help="comma-separated mode numbers")
    cast.add_argument("--x", type=int, default=None, help="the chosen value of X")
    cast.add_argument(
        "--dry-run", action="store_true", help="ask 'could I?' without changing anything"
    )
    cast.add_argument(
        "--keyword",
        default="",
        help=(
            "the keyword this cast is announced under (CR 601.2b) - required for "
            'any cast whose card is not in hand (CR 601.3): "Flashback" or '
            '"Mayhem" for a graveyard card (CR 702.34a, CR 702.187a/b), "Plot", '
            '"Foretell" or "Madness" for an exile card (CR 702.170d, CR 702.143a, '
            'CR 702.35a/b), or "Airbend" for one exiled by an airbend effect '
            "(CR 701.65a). Optional, and CR 118.9's alternative cost rather than "
            'CR 601.3\'s permission, for a card in hand: "Evoke", "Dash", "Morph", '
            '"Megamorph", "Disguise", "Bestow" or "Warp" (CR 702.74a, CR 702.109a, '
            "CR 702.37a, CR 702.37b, CR 702.168a, CR 702.103a, CR 702.185a) casts "
            "it for that keyword's cost instead of its printed mana cost "
            "- read the card's own cast_options in `battlefield_state` for "
            "which keyword and cost actually apply right now, rather than "
            "guessing. A card covered by a live PlayPermission instead (Past in "
            "Flames' graveyard grant, a flashback grant, a card exiled and made "
            "playable this turn) needs no --keyword: it is picked up "
            "automatically. Cascade's hit and suspend's own free cast are not "
            "PlayPermissions any more (battle 8): accepting either casts the "
            "card immediately while the permitting spell or ability is still "
            "resolving, answered through `decide`, not through a later `cast`."
        ),
    )
    cast.add_argument(
        "--alternative-cost",
        default="",
        help=(
            "CR 118.9's alternative cost named by its index (or source name) out "
            "of GET .../affordances/{card_id}'s alternative_costs - the same "
            "CR 601.2b announcement --keyword makes, for an option with no "
            "keyword of its own (Force of Negation's printed pitch). Only one "
            "alternative cost may ever be paid (CR 118.9a)"
        ),
    )
    cast.add_argument(
        "--choosing",
        default="",
        help=(
            "comma-separated card ids paying an ALTERNATIVE cost that names "
            "objects - the white card Solitude's 'Evoke-Exile a white card from "
            "your hand' exiles (CR 702.74a), the blue card pitched to Force of "
            "Negation, the Mountains sacrificed to Fireblast. Not --target, "
            "which is the spell's own targets (CR 601.2c). Omitting it is "
            "refused with the engine's own count, 'exile needs N card(s) "
            "chosen, 0 given'"
        ),
    )
    cast.add_argument(
        "--sacrificing",
        default="",
        help=(
            "comma-separated card ids for a printed 'As an additional cost to "
            "cast this spell, sacrifice ...' sentence (CR 118.8a) - Fling's own "
            "creature. Additional, not alternative: the printed mana cost is "
            "still paid on top"
        ),
    )
    cast.add_argument(
        "--discarding",
        default="",
        help="same shape, for a printed 'discard ...' additional cost (Cathartic Reunion)",
    )
    cast.add_argument(
        "--exiling",
        default="",
        help="same shape, for a printed 'exile ...' additional cost (Abhorrent Oculus)",
    )
    cast.add_argument(
        "--tapping",
        default="",
        help="same shape, for a printed 'tap ...' additional cost (Swallow Whole)",
    )
    cast.add_argument(
        "--delve",
        default="",
        help="comma-separated graveyard card ids exiled to pay generic mana (CR 702.66a)",
    )
    cast.add_argument(
        "--convoke",
        default="",
        help=(
            "comma-separated ids of your untapped creatures, tapped to pay "
            "(CR 702.51a). A creature pays '{1} or one mana of that creature's "
            "color', so an entry is a bare id for the generic symbol or "
            "<id>:W to cover a coloured one"
        ),
    )
    cast.add_argument(
        "--improvise",
        default="",
        help=(
            "comma-separated ids of your untapped artifacts, tapped to pay one "
            "generic mana each (CR 702.126a). A cast row that is payable this way "
            "names the ids in its own 'improvise'"
        ),
    )
    cast.add_argument(
        "--optional-costs",
        default="",
        help=(
            "comma-separated optional additional costs to pay (CR 601.2b), each "
            "the keyword from the cast row's own optional_costs: 'Kicker' kicks "
            "(CR 702.33a). A repeatable one takes a count ('Multikicker:2', "
            "'Replicate:3'), a gift names the opponent ('Gift:bob'), and a cost "
            "naming objects takes their ids ('Blight:<creature id>'). 'Entwine' "
            "chooses every mode (list them in --modes too, CR 702.42a); escalate "
            "is charged per mode beyond the first (CR 702.120a) and "
            "'Escalate:<card id>' names what it discards; a --keyword Harmonize "
            "cast taps 'Harmonize:<creature id>' to cut its cost (CR 702.180a)"
        ),
    )
    priority_flag(cast)

    def special_action(name: str, help_text: str) -> Any:
        """suspend/plot/foretell share this shape: name or id of a hand card,
        which mana sources pay the special action's own cost (default: every
        untapped source), and dry-run."""
        parser = sub.add_parser(name, help=help_text)
        parser.add_argument("card", help="card name or id, in your hand")
        parser.add_argument(
            "--sources",
            default="",
            help="comma-separated mana source ids (default: all your untapped sources)",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="ask 'could I?' without changing anything"
        )
        priority_flag(parser)
        return parser

    special_action(
        "suspend",
        "CR 116.2f/702.62a: exile a card with suspend from your hand, with N time counters",
    )
    special_action(
        "plot", "CR 116.2k/702.170b: exile a card with plot from your hand, for its plot cost"
    )
    special_action(
        "foretell", "CR 116.2h/702.143b: pay {2} and exile a card with foretell face down"
    )

    cycle = sub.add_parser("cycle", help="CR 702.29a: discard a card with cycling to draw")
    cycle.add_argument("card", help="card name or id, in your hand")
    cycle.add_argument(
        "--index", type=int, default=0, help="which cycling ability, if the card has more than one"
    )
    cycle.add_argument(
        "--sources",
        default="",
        help="comma-separated mana source ids (default: all your untapped sources)",
    )
    cycle.add_argument(
        "--dry-run", action="store_true", help="ask 'could I?' without changing anything"
    )
    priority_flag(cycle)

    equip = sub.add_parser(
        "equip",
        help=(
            "CR 702.6a: attach an Equipment you control to target creature you "
            "control, paying its equip cost. Only as a sorcery (CR 307.5). It "
            "uses the stack, so it releases priority unless --hold"
        ),
    )
    equip.add_argument("card_id", help="the Equipment's id")
    equip.add_argument("--target", required=True, help="the id of the creature to equip")
    equip.add_argument(
        "--sources",
        default="",
        help="comma-separated mana source ids (default: all your untapped sources)",
    )
    equip.add_argument(
        "--dry-run", action="store_true", help="ask 'could I?' without changing anything"
    )
    priority_flag(equip)

    land = sub.add_parser("play-land", help="play a land from your hand")
    land.add_argument("card", help="card name or id")
    priority_flag(land)

    activate = sub.add_parser(
        "activate",
        help=(
            "activate an ability of a card you control - a permanent's ability "
            "on the battlefield, or one that functions elsewhere because its "
            "own cost or effect says so (CR 113.6j/113.6m: Waker of Waves, "
            "Boseiju's or Otawara's Channel - a different card_id than cycling, "
            "not a special case of it). A mana ability does not use the "
            "stack (CR 605.3b) and so keeps priority with or without --hold: "
            "releasing could end the step (CR 500.2) and empty the mana you "
            "just made (CR 500.5). Any other ability goes on the stack and "
            "releases unless --hold"
        ),
    )
    activate.add_argument("card_id", help="the card's id")
    activate.add_argument(
        "index",
        type=int,
        help=(
            "ability index, from that card's own entry in the state view's "
            "abilities list - a card in hand or a graveyard is numbered off "
            "only the abilities that function from there, which need not "
            "match its numbering once it is on the battlefield"
        ),
    )
    activate.add_argument("--target", default="", help="comma-separated ids")
    activate.add_argument("--sources", default="", help=NAMED_SOURCES_HELP)
    activate.add_argument(
        "--sacrificing",
        default="",
        help=(
            "comma-separated card ids, for an activation cost's own 'Sacrifice "
            "[quality]' piece (CR 118.3) - Goblin Bombardment's own 'Sacrifice a "
            "creature: ...'. Usually one id; a cost that names more than one "
            "object ('Sacrifice two creatures') takes as many as it asks for. "
            "Omitting this when the ability's cost needs it is refused with the "
            "engine's own count, 'sacrifice needs N card(s) chosen, 0 given' - "
            "not a crash and not a silent no-op"
        ),
    )
    activate.add_argument(
        "--discarding",
        default="",
        help=(
            "comma-separated card ids, for an activation cost's own 'Discard "
            "[quality]' piece - the same shape as --sacrificing, for a "
            "battlefield ability with its own independent discard cost"
        ),
    )
    activate.add_argument(
        "--exiling",
        default="",
        help=(
            "comma-separated card ids, for an activation cost's own 'Exile "
            "[quality] ... and pay its mana cost' piece (Back from the Brink's "
            "own shape). Only a battlefield ability can take this - an ability "
            "that functions off the battlefield has no exile-as-cost path yet "
            "and refuses it"
        ),
    )
    activate.add_argument(
        "--color",
        default="",
        help="color letter (W/U/B/R/G/C) for a mana ability that offers a choice",
    )
    activate.add_argument(
        "--x",
        type=int,
        default=None,
        help=(
            "the chosen value of X (CR 602.2b), for an activation cost that "
            "carries one - Fiend Artisan's own '{X}{B/G}, {T}, Sacrifice "
            "another creature: Search ...'. Omitting this when the ability's "
            "own cost needs it is refused with the engine's own reason, "
            "'... needs X announced ...' - not a crash and not a silent "
            "activation at X=0"
        ),
    )
    activate.add_argument(
        "--dry-run", action="store_true", help="ask 'could I?' without changing anything"
    )
    priority_flag(activate)

    attackers = sub.add_parser("attackers", help="declare attackers (JSON list)")
    attackers.add_argument("pairs", help='[{"card": "<id>", "defender": "<player>"}]')
    priority_flag(attackers)

    blockers = sub.add_parser("blockers", help="declare blockers (JSON list)")
    blockers.add_argument("pairs", help='[{"blocker": "<id>", "blocked": "<id>"}]')
    priority_flag(blockers)

    decide = sub.add_parser("decide", help="answer your pending decision (CR 608.2d)")
    decide.add_argument(
        "card_ids",
        nargs="?",
        default="",
        help=(
            "comma-separated card ids, for a choose_cards decision - a discard, "
            "a bottoming, or a library search (a fetchland, a tutor). How many "
            "you must name is pending_decision.exact, never the kind: exact=true "
            "means exactly pending_decision.amount ids (CR 701.23d's quantity "
            "search, where you choose which and never whether), exact=false means "
            "any number from zero up to amount (CR 107.1c, and CR 701.23b's "
            "declinable search). A search is ONE choice over every legal "
            "candidate at once (CR 701.23a: look at all cards in that zone, even "
            "if it is a hidden zone), never a card-by-card walk - so name the "
            "card you want, reading pending_decision.card_labels, which names "
            "each pending_decision.cards entry positionally and is what a "
            "hidden-zone candidate has instead of a card you can look up "
            "(CR 400.2 makes the library hidden, and `state` strips your own). "
            "pending_decision.source is the card asking, and says what it may "
            "find. OMITTING THIS ARGUMENT SENDS THE EMPTY ANSWER, which is the "
            "decline where exact=false and is refused where exact=true - use "
            "--decline to say so out loud. pending_decision.default_cards is "
            "what silence costs instead: what the engine takes if the decision "
            "times out unanswered"
        ),
    )
    decide.add_argument(
        "--decline",
        action="store_true",
        help=(
            "answer a choose_cards decision with no cards at all - CR 701.23b's "
            "decline, legal only where pending_decision.exact is false. Exactly "
            "the same request as an omitted card_ids (both post the empty "
            "answer), and refused by the same check where exact is true; it "
            "exists so a deliberate decline reads as one in a transcript rather "
            "than as a forgotten argument. Refused outright if card_ids is also "
            "given"
        ),
    )
    decide.add_argument(
        "--targets",
        default="",
        help=(
            "comma-separated target ids or player names, for a choose_targets "
            "decision - a triggered ability asking for its target (CR 603.3d), "
            "or a targeted ability being activated with no targets supplied "
            "(CR 602.2b/601.2c)"
        ),
    )
    decide.add_argument(
        "--accept",
        choices=("yes", "no"),
        default=None,
        help=(
            "yes/no, for a may, pay_life, pay_energy, pay_mana, or "
            "opening_hand_action decision (CR 608.2d, CR 119.4, CR 107.14, "
            "CR 118.12a, CR 103.6)"
        ),
    )
    decide.add_argument(
        "--sources",
        default="",
        help=(
            "comma-separated ids of the mana sources to activate while paying a "
            "pay_mana decision (CR 608.2g, CR 605.3a; engine PR #17, B6-08), a "
            "subset of pending_decision.mana_sources - legal only with --accept "
            "yes. Omit it (or pass '') to name none: the engine then taps every "
            "offered source it needs on its own, the same 'name none, let the "
            "engine pick' rule --sources already follows on cast and activate"
        ),
    )
    decide.add_argument(
        "--moved",
        default="",
        help=(
            "comma-separated card ids you opt into, for a scry or surveil "
            "decision - the rest go to the other half. Where each half goes is "
            "pending_decision.moved_label (the ids you name) and "
            "pending_decision.kept_label (the ones you do not): short phrases "
            "in the asking card's own words, printed by `state` when present "
            "and authoritative when they are. The kind is not the destination "
            "- the engine reuses scry for any look-then-split, and Devourer of "
            "Destiny's keeps the named card on TOP and exiles the rest. Only "
            "when both labels are absent may you assume the keyword's own pair: "
            "scry bottoms the named cards (CR 701.22a), surveil puts them in "
            "the graveyard (CR 701.25a), each leaving the rest on top. "
            "How many you must name is pending_decision.exact, exactly like a "
            "choose_cards answer: true means exactly pending_decision.amount "
            "ids (Codecracker Hound's 'put one into your hand and the other "
            "into your graveyard' - CR 608.2d's only legal split), false means "
            "any number from zero up to pending_decision.amount (CR 107.1c) - "
            "a plain scry/surveil's amount equals every card looked at ('any "
            "number of them'), while Devourer of Destiny's 'may put one of "
            "those cards back' is exact=false with amount=1, a real ceiling of "
            "one, not 'any'. validate_answer refuses a count outside it either way"
        ),
    )
    decide.add_argument(
        "--top-order",
        default="",
        help="comma-separated card ids: the order the rest go back on top, topmost first",
    )
    decide.add_argument(
        "--energy-amount",
        type=int,
        default=None,
        help="integer amount of {E} to pay, for a pay_energy_amount decision (CR 107.14, CR 118.3)",
    )
    decide.add_argument(
        "--life-amount",
        type=int,
        default=None,
        help="integer amount of life to pay, for a pay_life_amount decision (CR 119.4)",
    )
    decide.add_argument(
        "--choice",
        default="",
        help=(
            "the chosen color/card name/creature type, for a choose_a_quality decision "
            "(CR 614.12a), or the chosen mode's printed text, for a choose_a_mode decision "
            "(CR 603.3c, CR 700.2a)"
        ),
    )
    decide.add_argument(
        "--top",
        choices=("top", "bottom"),
        default=None,
        help=(
            "top or bottom of the library, for a put_on_top_or_bottom decision "
            "(Subtlety) - asked of the targeted spell's owner, not its caster"
        ),
    )
    decide.add_argument(
        "--order",
        default="",
        help=(
            "comma-separated triggered-ability ids, for an order_triggers decision "
            "(CR 603.3b) - a permutation of pending_decision.cards, bottom of the "
            "stack first, so the last id listed is the one that resolves first. "
            "Read each id in pending_decision.card_details before choosing: for "
            "this kind it names the ability's source and its printed text, since "
            "an ability on the stack is an object with no card of its own "
            "(CR 113.1c). An id missing from card_details is one CR 603.3b is "
            "asking about before CR 603.3 has put it on the stack - "
            "pending_decision.prompt numbers those positionally against cards"
        ),
    )
    decide.add_argument(
        "--damage",
        default="",
        help=(
            'JSON list of {"target": ..., "amount": ...}, for an '
            "assign_combat_damage decision (CR 510.1c, CR 702.19b) - how a "
            "blocked attacker's combat damage divides among its blockers and, "
            "with trample, past them to the player or planeswalker attacked. "
            "`target` is a card id from pending_decision.cards[1:] (the "
            "blockers) or, with trample, a player/planeswalker id from "
            "pending_decision.targets. The amounts must sum to exactly "
            "pending_decision.amount (the attacker's power), and damage may "
            "reach the attacked player or planeswalker only once every "
            "blocker has been assigned at least its own entry of "
            "pending_decision.lethal_damage (CR 702.19b)"
        ),
    )
    decide.add_argument(
        "--generation",
        type=int,
        default=None,
        help=(
            "pending_decision.generation, echoed back to say WHICH question this "
            "answer is for. A resolution can ask more than one decision - a "
            "fetchland's search, then the shockland's pay 2 life - and accepting "
            "the first replaces the standing question with the second "
            "immediately. Passing the generation you actually read turns an "
            "answer aimed at the previous question into a refusal instead of a "
            "wrong answer to the current one. Omit it to skip the check"
        ),
    )
    priority_flag(decide)

    # The two priority verbs, and the only two. CR 117.1 lists what the player
    # with priority may do ("cast spells, activate abilities, and take special
    # actions") and CR 117.3d has a player who chooses none of them pass; CR
    # 117.4 makes resolving the stack's top and ending the step consequences of
    # everyone passing, and CR 703.2 says turn-based actions "are not
    # controlled by any player". So there is no subcommand here for resolving
    # the stack, stepping the turn on, or moving the turn marker - none of the
    # three is something a seat does.
    sub.add_parser(
        "pass",
        help="pass priority once - 'I am done for now' (CR 117.3d)",
    )

    until = sub.add_parser(
        "pass-until",
        help="keep passing automatically until a stop condition fires",
    )
    until.add_argument(
        "step",
        metavar="STEP",
        help=(
            f"one of: {', '.join(AUTOPASS_STEPS)}. "
            "Keep passing every priority you currently hold until this step, "
            "instead of handing back one priority at a time. This waits for "
            "nothing: it returns the moment it would have to wait for your "
            "opponent, printing finished=false and a --resume token to carry "
            "on with. Naming the step you are standing in means its NEXT "
            "occurrence, never this one - 'pass-until upkeep' asked in your "
            "upkeep passes a whole turn round. untap and cleanup are not "
            "offered: nobody receives priority in either (CR 502.4, CR 514.3), "
            "and the game crosses both on its own (CR 500.3)"
        ),
    )
    until.add_argument(
        "--whose-turn",
        default="mine",
        metavar="WHOSE",
        help=(
            f"one of: {', '.join(AUTOPASS_WHOSE_TURN)}. Whose turn STEP must "
            "be in; 'any' takes whichever comes first"
        ),
    )
    until.add_argument(
        "--resume",
        default="",
        metavar="JSON",
        help=(
            "the `resume` object a previous run printed, handed back verbatim, "
            "so this call continues that run instead of starting a new one. A "
            "run prints one whenever its `finished` is false; without it the "
            "next call re-stamps the run where you are standing now and would "
            "skip the very combat step it should stop you at "
            "(see seatkit.pass_until)"
        ),
    )
    until.add_argument(
        "--no-stop-on-stack",
        action="store_true",
        help=(
            "run straight through anything that lands on the stack instead of "
            "stopping to respond to it. This is the stop that gives you your "
            "chance to answer a spell cast in a window you were passing "
            "through, and CR 117.4 resolves that spell the moment everyone has "
            "passed in succession - so switching it off means you will not get "
            "the chance. Only where you have decided you are responding to "
            "nothing"
        ),
    )
    until.add_argument(
        "--no-stop-when-attacked",
        action="store_true",
        help=(
            "run straight through your own declare-blockers step. YOU WILL "
            "BLOCK NOTHING AND TAKE THE WHOLE ATTACK. Declaring blockers is a "
            "turn-based action that does not use the stack (CR 509.1), so "
            "--no-stop-on-stack's condition cannot catch it for you. Only for "
            "a board that cannot block or a life total the attack cannot reach"
        ),
    )
    until.add_argument(
        "--max-seconds",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "if this call would otherwise have to hand the board back waiting "
            "for the opponent, keep trying for up to this many seconds "
            "(capped at seatkit.MAX_WAIT_SECONDS) before giving up - use this "
            "instead of `sleep N && ... pass-until ... --resume ...`. Left at "
            "0 (the default), this returns the moment it would have to wait, "
            "exactly as it always has"
        ),
    )
    until.add_argument(
        "--no-stop-when-you-can-attack",
        action="store_true",
        help=(
            "run straight through your own declare-attackers step. YOU WILL "
            "ATTACK WITH NOTHING that turn. Declaring attackers does not use "
            "the stack either (CR 508.1), and skipping it is silent - the "
            "engine treats a player who never declares as declaring nobody, so "
            "the turn just produces no attack and nothing says why. Left on, a "
            "run aimed past combat stops in your declare-attackers step every "
            "turn, which is the point. Switching it off is the deliberate "
            "'I am not attacking this turn'"
        ),
    )
    arm_flags(until)

    # CR 103.5 and CR 103.6. A game booted from decklists *rests* in these two
    # steps before turn 1, and nothing on the board can happen until every seat
    # has declared - so a seat with no way to declare is a seat that hangs the
    # whole game for everyone, which is exactly what happened: the AI seat had
    # no pregame command at all, so a human opponent's "Keep" appeared to do
    # nothing forever.
    mulligan = sub.add_parser(
        "mulligan", help="CR 103.5: declare whether you take a mulligan (do this first)"
    )
    mulligan.add_argument(
        "--take",
        action="store_true",
        help="take a mulligan; omit to keep the hand you have",
    )
    mulligan.add_argument(
        "--bottom",
        default="",
        help=(
            "comma-separated card ids to put on the bottom when keeping - CR 103.5 "
            "wants one per mulligan already taken. Omit to let the engine pick."
        ),
    )

    sub.add_parser(
        "opening-hand-done",
        help="CR 103.6: finish your opening-hand actions, starting the game",
    )

    # CR 103.6a/b: some cards act from the opening hand - Leyline of the Void
    # begins the game on the battlefield, Gemstone Caverns is revealed. This
    # command was missing for a whole wave while `mcp.py`'s `opening_hand_action`
    # had it, so a CLI seat could only ever *decline* what its own opening hand
    # offered: it could finish the window, never use it. `state.pregame.
    # opening_hand_options` is where the ids come from.
    opening_action = sub.add_parser(
        "opening-hand-action",
        help="CR 103.6: take one card's opening-hand action (see pregame.opening_hand_options)",
    )
    opening_action.add_argument("card", help="one card id from pregame.opening_hand_options")

    log = sub.add_parser("log", help="the game's action log")
    log.add_argument("limit", nargs="?", type=int, default=40)

    report = sub.add_parser("report", help="file a problem report into the game log")
    report.add_argument(
        "text",
        nargs="?",
        default=None,
        help="the report; omit it to read the report from stdin instead",
    )

    plan = sub.add_parser(
        "plan",
        help=(
            "declare, before you act, how you read the turn (POST .../plan) - "
            "the same five fields and the same for-turn choice mcp.py's "
            "declare_plan tool takes"
        ),
    )
    plan.add_argument("--board-read", required=True, help="what you see on the board right now")
    plan.add_argument(
        "--my-role", required=True, help="your role in this matchup - beatdown, control, ..."
    )
    plan.add_argument(
        "--plan", dest="plan_text", required=True, help="what you intend to do this turn, and why"
    )
    plan.add_argument("--expecting", required=True, help="what you expect the opponent to do")
    plan.add_argument("--because", required=True, help="the read that supports that expectation")
    plan.add_argument(
        "--confidence",
        choices=["low", "medium", "high"],
        default="medium",
        help="how strongly you hold that prediction; default medium",
    )
    plan.add_argument(
        "--for-turn",
        dest="for_turn",
        choices=["current", "next"],
        default="current",
        help=(
            "which turn this declares for - 'current' (default) is the turn "
            "already under way; 'next', called on the opponent's turn, plans "
            "ahead for your own next turn instead of waiting until your "
            "upkeep to write one there"
        ),
    )

    stage_advice = sub.add_parser(
        "stage-advice",
        help=(
            "arm your next committing call to be captured as a draft for a "
            "window, instead of posted right away (mcp stage_advice)"
        ),
    )
    stage_advice.add_argument(
        "step",
        metavar="STEP",
        help=(
            f"one of: {', '.join(AUTOPASS_STEPS)}. Naming the step your human "
            "is standing in right now means its NEXT occurrence, never this "
            "one - the same rule pass-until follows"
        ),
    )
    stage_advice.add_argument(
        "--whose-turn",
        default="mine",
        metavar="WHOSE",
        help=f"one of: {', '.join(AUTOPASS_WHOSE_TURN)}; default 'mine'",
    )

    script = sub.add_parser(
        "script",
        help="run one of your registered play scripts, or list them (mcp run_script/list_scripts)",
    )
    script_sub = script.add_subparsers(dest="script_command", required=True)

    def script_flags(command: Any) -> None:
        """The two arguments `script run` and `script list` share, named and
        defaulted as `run_script`'s and `list_scripts`' own."""
        command.add_argument(
            "--opposing-archetype",
            dest="opposing_archetype",
            default="*",
            metavar="KEY",
            help=(
                "the opponent's archetype as you read it, a matchup key; * (the default) "
                "when you do not know. Same as --input opposing_archetype=KEY; naming two "
                "different archetypes is refused"
            ),
        )
        command.add_argument(
            "--input",
            dest="inputs",
            action="append",
            default=None,
            metavar="NAME=VALUE",
            help=(
                "one input your deck's scripts read, repeat it for each: VALUE is read as "
                "JSON where it is JSON (true, 3) and as text otherwise (dimir-frog)"
            ),
        )

    script_run = script_sub.add_parser(
        "run",
        help=(
            "run a script's moves and the one next script that applies, printing "
            "script_exit and the state; --reasoning is required"
        ),
    )
    script_run.add_argument("script", help="the script's id")
    script_flags(script_run)
    script_run.add_argument(
        "--resume",
        default="",
        metavar="JSON",
        help=(
            "the script_exit.resume object a run cut off by its time cap printed, "
            "handed back verbatim, so this call carries on with that run"
        ),
    )
    script_run.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "wait (passing priority, every stop on) for the script's when; --no-wait "
            "leaves a script off its step not_applicable"
        ),
    )
    script_run.add_argument(
        "--max-seconds",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="cap this call; 0 (the default) runs up to seatkit.MAX_WAIT_SECONDS",
    )
    script_list = script_sub.add_parser(
        "list",
        help=(
            "every registered script by priority: whether it applies now and why not, "
            "its conflicts, and whether more than one applies"
        ),
    )
    script_flags(script_list)

    plays = sub.add_parser(
        "plays",
        help=(
            "CLI-only: check, reprioritise and admit a guides checkout's play scripts "
            "(no seat, no game, no API)"
        ),
    )
    plays_sub = plays.add_subparsers(dest="plays_command", required=True)
    check = plays_sub.add_parser(
        "check",
        help=(
            "lint decks/<slug>/plays/*.json: exit 0 when clean (conflict warnings "
            "allowed), 1 with coded errors"
        ),
    )
    check.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="guides root holding decks/ and meta/card-data.json (default: .)",
    )
    check.add_argument(
        "--deck",
        action="append",
        default=[],
        metavar="SLUG",
        help="check only this deck; repeatable (default: every deck)",
    )
    check.add_argument("--json", action="store_true", help="print the report as JSON")

    reprioritise = plays_sub.add_parser(
        "priority",
        help=(
            "change one play's priority, clamped to 0..100 and at most 5 per call and per "
            "game, touching nothing else in the file; the delta is recorded in "
            ".git/mtg-plays-deltas.json for admit"
        ),
    )
    reprioritise.add_argument("play_id", help="the play's id, its file stem")
    amount = reprioritise.add_mutually_exclusive_group(required=True)
    amount.add_argument("--delta", type=int, help="add this (negative lowers it)")
    amount.add_argument("--set", dest="set_to", type=int, help="set it to this")
    _guides_target(reprioritise, game=False)
    reprioritise.add_argument(
        "--game-id",
        default=None,
        help="the game this change came out of, for the per-game bound (default: $MTG_GAME_ID)",
    )

    admit = plays_sub.add_parser(
        "admit",
        help=(
            "commit, rebase, gate and push decks/*/plays/** changes to mtg-decks main "
            "(a pull request after 3 non-fast-forward pushes); prints admitted | pr_opened | "
            "refused | conflict | gate_failed | conflict_regression | nothing_to_admit"
        ),
    )
    _guides_target(admit, game=True)

    guides = sub.add_parser(
        "guides",
        help=(
            "CLI-only: propose a deck-guide edit as a pull request the owner merges by hand "
            "(no seat, no game, no API)"
        ),
    )
    guides_sub = guides.add_subparsers(dest="guides_command", required=True)
    propose = guides_sub.add_parser(
        "propose",
        help=(
            "render and commit a matchups[].play_plan / notes edit of the newest boarding plan "
            "and NOTES.md, push seat/<game-id>-<deck>-<role>-guides and open a pull request "
            "the owner merges by hand - returns at once, never waits on CI or automerge"
        ),
    )
    _guides_target(propose, game=True)

    return parser


def _guides_target(parser: argparse.ArgumentParser, *, game: bool) -> None:
    parser.add_argument("--deck", required=True, metavar="SLUG", help="the deck's slug")
    if game:
        parser.add_argument(
            "--game-id", required=True, help="the game this change came out of (Game-Id trailer)"
        )
    parser.add_argument(
        "--root",
        type=Path,
        default=GUIDES_ROOT,
        help=f"the guides git checkout (default: {GUIDES_ROOT})",
    )


def _emit(result: dict[str, Any], success: frozenset[str] = SUCCESS) -> int:
    print(json.dumps(result, indent=2))
    return 0 if result.get("result") in success else 1


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "plays" and args.plays_command == "check":
        # Offline by design: a lint over files, so no uid, no API, no seat.
        return playscripts_lint.run_cli(args.root, args.deck or None, as_json=args.json)
    if args.command == "plays" and args.plays_command == "priority":
        return _emit(
            playscripts_admit.set_priority(
                args.root,
                args.deck,
                args.play_id,
                delta=args.delta,
                value=args.set_to,
                game_id=args.game_id or os.environ.get("MTG_GAME_ID", "").strip() or None,
            ),
            frozenset({"priority_set"}),
        )
    if args.command == "plays":
        return _emit(playscripts_admit.admit(args.root, args.deck, args.game_id))
    if args.command == "guides":
        return _emit(playscripts_guides.propose(args.root, args.deck, args.game_id))
    if not args.uid:
        parser.error("the following arguments are required: --uid")
    seats = Seats(args.api, args.game)
    # The same check every MCP tool makes through `_guarded`, which this
    # transport simply did not have: an API speaking a different
    # `seatkit.PROTOCOL_VERSION` answers with a different shape, and without
    # this a seat found that out as a stray KeyError or, worse, as an answer
    # it quietly misread. Fatal here, unlike in `mcp.py`, because a command is
    # a fresh short-lived process rather than a server that has to survive the
    # API restarting under it.
    trouble = api_trouble(seats.http, seats.api)
    if trouble:
        print(trouble)
        return 1
    seat = seats.seat_for(args.uid)
    if seat is None:
        print(
            "that uid is no seat in this game. You cannot act, and you may see "
            "nothing - a seat uid is given to you when you are dispatched."
        )
        return 1
    gid = seat.game_id()

    # S2 A6 review fix 2: carry whatever a PRIOR CLI invocation left staged
    # for this uid into this process's own `Seat._advice_drafts` - the one
    # thing an in-process dict alone could never do across separate
    # processes. `seat.advise` guards it: a seat's own dict never holds
    # anything and there is nothing on disk for one to load either
    # (`stage_advice` itself already refuses a seat outright).
    if seat.advise:
        persisted_draft = _load_cli_draft(gid, seat.uid)
        if persisted_draft is not None:
            seat._advice_drafts[seat.uid] = persisted_draft

    def persist_draft() -> None:
        """Write this uid's current draft back out for the next CLI
        invocation - called after every command that could have mutated it
        (staged one, captured one, or had `resolve_advice_draft` post or drop
        one). A no-op for a seat, and cheap enough for a read-only command to
        call unconditionally rather than have to know which commands need
        it."""
        if seat.advise:
            _save_cli_draft(gid, seat.uid, seat._advice_drafts.get(seat.uid))

    def emit(result: dict[str, Any], *, state_for_draft: Any = None, **json_kwargs: Any) -> None:
        """Print `result` as JSON, after folding in this seat's own staged-
        draft status exactly as `mcp._tool_result`'s `_attach_draft` does for
        the matching MCP tool (`cast`/`activate`/.../`decide`/`pass` via
        `act()`, `wait`, `pass-until`, `script run`) - CLI parity with the
        MCP transport, not a CLI-only feature. Every other CLI command skips
        this the same way its own MCP tool does (`mulligan`, `opening-hand-
        *`, `report`, `plan`, `log`, `script list`, `state`, `stage-advice`
        itself - none of those route through `_tool_result` on the MCP side
        either), though `persist_draft()` still runs for the ones among them
        that can mutate a draft (a committing call can still be captured into
        one - `mulligan`/`opening-hand-action`/`opening-hand-done` call it
        directly rather than through `emit`).

        Same failure discipline as `mcp._attach_draft`: an exception reading
        the draft must never break the command's own answer, which already
        landed by the time this runs."""
        if seat.advise:
            view = state_for_draft if isinstance(state_for_draft, dict) else {}
            try:
                draft = seatkit.draft_status_for_answer(seat, view)
            except Exception:
                draft = None
            if draft is not None:
                result["draft"] = draft
            persist_draft()
        print(json.dumps(result, **json_kwargs))

    # "no --sources given means offer every untapped source" is one rule, and
    # it lives in `seatkit` so this file and `mcp.py` cannot drift on it - they
    # already had: this used to branch on the raw string, so `--sources ","`
    # offered the engine nothing at all while the MCP tool, branching on the
    # parsed list, offered everything.
    def sources_or_all(value: str) -> list[str]:
        return seat.sources_or_all(value)

    #: CR 116.3: "If a player takes a special action, that player receives
    #: priority afterward." So these must NOT release priority - a seat that
    #: plays a land and then passes has thrown away the rest of its own main
    #: phase, which is what a live game showed: a Forest in precombat main and
    #: the Amulet of Vigor it should have paid for stranded until postcombat.
    #: A special action does not use the stack at all (CR 116.1), so there is
    #: nothing releasing priority would let resolve.
    #:
    #: Four of these five really are special actions: playing a land
    #: (CR 116.2a), suspend (CR 116.2f), plot (CR 116.2k, CR 702.170b) and
    #: foretell (CR 116.2h, CR 702.143b). `cycle` is NOT - CR 702.29a makes
    #: cycling an ordinary activated ability, and it belongs here on CR 117.3c
    #: instead ("if a player has priority when they cast a spell, activate an
    #: ability, or take a special action, that player receives priority
    #: afterward"). The name of the set is a shorthand, not a claim; keep it
    #: in step with `mcp.py`'s per-tool defaults, which say the same.
    SPECIAL_ACTIONS = {"play-land", "suspend", "plot", "foretell", "cycle"}

    def note(state: dict[str, Any] | None) -> None:
        """Say a pending look-then-split's two destinations out loud, on
        stderr, after the JSON that carries them."""
        line = split_destinations(state)
        if line:
            print(line, file=sys.stderr)

    def act(perform: Any, dry_run: bool = False) -> None:
        """Every non-dry-run action shares this: perform it, then run the
        shared release-and-wait flow, printing the combined answer.

        `--hold` forces holding; a special action holds by default under
        CR 116.3 whether it was asked for or not."""
        if dry_run:
            emit(perform(), indent=1, sort_keys=True)
            return
        hold = args.hold or args.command in SPECIAL_ACTIONS
        result = run_action(seat, perform, hold_priority=hold, reasoning=args.reasoning)
        emit(
            result,
            state_for_draft=result.get("state") if isinstance(result, dict) else None,
            indent=1,
            sort_keys=True,
        )
        note(result.get("state") if isinstance(result, dict) else None)

    if args.command == "state":
        current = seat.fetch_state()
        print(json.dumps(current, indent=1, sort_keys=True))
        note(current)
    elif args.command == "wait":
        try:
            arm_inputs = parse_inputs(args.arm_inputs)
        except ValueError as exc:
            print(exc)
            return 1
        woken = wait_and_arm(
            seat,
            before=None,
            max_seconds=args.max_seconds,
            corpora=seats.script_corpora,
            arm=args.arm,
            arm_reasoning=args.arm_reasoning,
            opposing_archetype=args.opposing_archetype,
            inputs=arm_inputs,
        )
        woken["state"]["wake_reason"] = woken["wake_reason"]
        if "armed" in woken:
            woken["state"]["armed"] = woken["armed"]
        if "armed_conflict" in woken:
            woken["state"]["armed_conflict"] = woken["armed_conflict"]
        if "armed_refusal" in woken:
            woken["state"]["armed_refusal"] = woken["armed_refusal"]
        emit(woken["state"], state_for_draft=woken["state"], indent=1, sort_keys=True)
        note(woken["state"])
    elif args.command == "cast":
        body: dict[str, Any] = {"player": seat.player, "dry_run": args.dry_run}
        body.update(card_reference(args.card))
        if args.spell_face.strip():
            body["spell_face"] = args.spell_face.strip()
        if args.target:
            body["targets"] = _parse_ids(args.target)
        sources = _parse_ids(args.sources)
        if sources:
            body["mana_sources"] = sources
        if args.modes:
            body["modes"] = [int(mode) for mode in _parse_ids(args.modes)]
        if args.x is not None:
            body["x_value"] = args.x
        if args.keyword:
            body["keyword"] = args.keyword
        if args.alternative_cost:
            body["alternative_cost"] = (
                int(args.alternative_cost)
                if args.alternative_cost.strip().isdigit()
                else args.alternative_cost
            )
        for field, value in (
            ("choosing", args.choosing),
            ("sacrificing", args.sacrificing),
            ("discarding", args.discarding),
            ("exiling", args.exiling),
            ("tapping", args.tapping),
            ("delve", args.delve),
            ("improvise", args.improvise),
        ):
            if value:
                body[field] = _parse_ids(value)
        if args.convoke:
            body["convoke"] = parse_convoke(args.convoke)
        if args.optional_costs:
            body["optional_costs"] = parse_optional_costs(args.optional_costs)
        if args.reasoning:
            body["reasoning"] = args.reasoning
        act(
            lambda: seat.call_json("POST", f"/api/games/{gid}/actions/cast", body),
            dry_run=args.dry_run,
        )
    elif args.command in ("suspend", "plot", "foretell"):
        body = {"player": seat.player, "dry_run": args.dry_run}
        body.update(card_reference(args.card))
        sources = sources_or_all(args.sources)
        if sources:
            body["mana_sources"] = sources
        if args.reasoning:
            body["reasoning"] = args.reasoning
        act(
            lambda: seat.call_json("POST", f"/api/games/{gid}/actions/{args.command}", body),
            dry_run=args.dry_run,
        )
    elif args.command == "cycle":
        body = {"player": seat.player, "ability_index": args.index, "dry_run": args.dry_run}
        body.update(card_reference(args.card))
        sources = sources_or_all(args.sources)
        if sources:
            body["mana_sources"] = sources
        if args.reasoning:
            body["reasoning"] = args.reasoning
        act(
            lambda: seat.call_json("POST", f"/api/games/{gid}/actions/cycle", body),
            dry_run=args.dry_run,
        )
    elif args.command == "equip":
        body = {
            "player": seat.player,
            "card_id": args.card_id,
            "target": args.target,
            "dry_run": args.dry_run,
        }
        sources = sources_or_all(args.sources)
        if sources:
            body["mana_sources"] = sources
        if args.reasoning:
            body["reasoning"] = args.reasoning
        act(
            lambda: seat.call_json("POST", f"/api/games/{gid}/actions/equip", body),
            dry_run=args.dry_run,
        )
    elif args.command == "play-land":
        body = {"player": seat.player}
        body.update(card_reference(args.card))
        if args.reasoning:
            body["reasoning"] = args.reasoning
        act(lambda: seat.call_json("POST", f"/api/games/{gid}/actions/play-land", body))
    elif args.command == "activate":
        body = {
            "player": seat.player,
            "card_id": args.card_id,
            "ability_index": args.index,
            "dry_run": args.dry_run,
        }
        if args.target:
            body["targets"] = _parse_ids(args.target)
        sources = _parse_ids(args.sources)
        if sources:
            body["mana_sources"] = sources
        if args.sacrificing:
            body["sacrificing"] = _parse_ids(args.sacrificing)
        if args.discarding:
            body["discarding"] = _parse_ids(args.discarding)
        if args.exiling:
            body["exiling"] = _parse_ids(args.exiling)
        if args.x is not None:
            body["x_value"] = args.x
        if args.color:
            body["color"] = args.color
        if args.reasoning:
            body["reasoning"] = args.reasoning
        act(
            lambda: seat.call_json("POST", f"/api/games/{gid}/actions/activate", body),
            dry_run=args.dry_run,
        )
    elif args.command == "attackers":
        # No `player` - see `mcp.py`'s `declare_attackers`: the route
        # resolves the declaring seat from the uid and `AttackRequest` has no
        # such field to put it in.
        body = {
            "attackers": [
                {"card_id": pair["card"], "defender": pair["defender"]}
                for pair in _parse_pairs(args.pairs)
            ],
        }
        if args.reasoning:
            body["reasoning"] = args.reasoning
        act(lambda: seat.call_json("POST", f"/api/games/{gid}/actions/attack", body))
    elif args.command == "blockers":
        body = {
            "player": seat.player,
            "blocks": [
                {"blocker": pair["blocker"], "blocked": pair["blocked"]}
                for pair in _parse_pairs(args.pairs)
            ],
        }
        if args.reasoning:
            body["reasoning"] = args.reasoning
        act(lambda: seat.call_json("POST", f"/api/games/{gid}/actions/declare-blockers", body))
    elif args.command == "pass-until":
        # The shared loop and the shared validator, not a second copy of
        # either: `mcp.pass_until` calls the same two functions with the same
        # defaults, so the two front ends cannot stop in different places or
        # disagree about what a legal destination is. See `seatkit.pass_until`.
        problem = autopass_target_error(args.step, args.whose_turn)
        if problem:
            print(problem)
            return 1
        try:
            arm_inputs = parse_inputs(args.arm_inputs)
        except ValueError as exc:
            print(exc)
            return 1
        result = pass_until(
            seat,
            until_step=args.step,
            whose_turn=args.whose_turn,
            stop_on_stack=not args.no_stop_on_stack,
            stop_when_attacked=not args.no_stop_when_attacked,
            stop_when_you_can_attack=not args.no_stop_when_you_can_attack,
            resume=json.loads(args.resume) if args.resume else None,
            reasoning=args.reasoning,
            max_seconds=args.max_seconds,
            corpora=seats.script_corpora,
            arm=args.arm,
            arm_reasoning=args.arm_reasoning,
            opposing_archetype=args.opposing_archetype,
            inputs=arm_inputs,
        )
        emit(result, state_for_draft=result.get("state"), indent=1, sort_keys=True)
        note(result.get("state"))
    elif args.command == "pass":
        pass_body: dict[str, Any] = {"player": seat.player}
        if args.reasoning:
            pass_body["reasoning"] = args.reasoning
        result = run_action(
            seat,
            lambda: seat.call_json("POST", f"/api/games/{gid}/actions/pass-priority", pass_body),
            hold_priority=False,
            release=False,
        )
        emit(
            result,
            state_for_draft=result.get("state") if isinstance(result, dict) else None,
            indent=1,
            sort_keys=True,
        )
    elif args.command == "decide":
        # `--decline` and an omitted `card_ids` post the identical request -
        # the empty answer, which for a CR 701.23b search ("that player isn't
        # required to find some or all of those cards even if they're present
        # in that zone") is a real answer and not a missing one. The flag adds
        # no contract the API did not already have; it exists so that a
        # deliberate decline is distinguishable from a forgotten argument in a
        # transcript, which is the whole ambiguity of a defaulted positional.
        # Giving both is a seat saying two different things at once, so it is
        # refused here rather than silently resolved one way.
        if args.decline and _parse_ids(args.card_ids):
            print(
                "--decline names no cards, so it cannot be combined with card_ids. "
                "Drop one: --decline alone is the empty answer (CR 701.23b's "
                "decline, legal only where pending_decision.exact is false)."
            )
            return 1

        def perform_decide() -> Any:
            body: dict[str, Any] = {
                "player": seat.player,
                "card_ids": _parse_ids(args.card_ids),
                "targets": _parse_ids(args.targets),
                "moved": _parse_ids(args.moved),
            }
            if args.accept is not None:
                body["accept"] = args.accept == "yes"
            sources = _parse_ids(args.sources)
            if sources:
                body["mana_sources"] = sources
            if args.top_order:
                body["top_order"] = _parse_ids(args.top_order)
            if args.energy_amount is not None:
                body["energy_amount"] = args.energy_amount
            if args.life_amount is not None:
                body["life_amount"] = args.life_amount
            if args.choice:
                body["choice"] = args.choice
            if args.top is not None:
                body["top"] = args.top == "top"
            if args.order:
                body["order"] = _parse_ids(args.order)
            if args.damage:
                body["damage"] = [
                    {"target": entry["target"], "amount": entry["amount"]}
                    for entry in _parse_pairs(args.damage)
                ]
            if args.generation is not None:
                body["generation"] = args.generation
            if args.reasoning:
                body["reasoning"] = args.reasoning
            answer = seat.call_json("POST", f"/api/games/{gid}/decide", body)
            if isinstance(answer, dict) and answer.get("accepted") is False:
                return {"allowed": False, **answer}
            return {"allowed": True, **answer} if isinstance(answer, dict) else answer

        act(perform_decide)
    elif args.command == "mulligan":
        # Deliberately NOT through `act`: the pregame endpoints answer
        # immediately and block on nothing (see `opening.py`), so there is no
        # priority to release and nothing to wait for. Wrapping them in the
        # release-and-wait flow would make a seat sit waiting for a priority
        # that cannot arrive until the pregame it is trying to finish is over.
        body: dict[str, Any] = {"player": seat.player, "take": bool(args.take)}
        if args.bottom:
            body["bottom"] = _parse_ids(args.bottom)
        if args.reasoning:
            body["reasoning"] = args.reasoning
        # No `draft` on this answer - `mcp.mulligan` does not route through
        # `_tool_result` either (pregame endpoints answer immediately, never
        # `wait_for_priority`'s own poll), so there is nothing for a CLI-only
        # attach to add here that the MCP tool does not already give. The
        # committing call itself can still capture a staged, empty window
        # (`Seat._advise_answer`), so `persist_draft` still runs.
        print(
            json.dumps(
                seat.call_json("POST", f"/api/games/{gid}/pregame/mulligan", body),
                indent=1,
                sort_keys=True,
            )
        )
        persist_draft()
    elif args.command == "opening-hand-action":
        oha_body: dict[str, Any] = {"player": seat.player, "card_id": args.card.strip()}
        if args.reasoning:
            oha_body["reasoning"] = args.reasoning
        print(
            json.dumps(
                seat.call_json(
                    "POST",
                    f"/api/games/{gid}/pregame/opening-hand/action",
                    oha_body,
                ),
                indent=1,
                sort_keys=True,
            )
        )
        persist_draft()
    elif args.command == "opening-hand-done":
        ohd_body: dict[str, Any] = {"player": seat.player}
        if args.reasoning:
            ohd_body["reasoning"] = args.reasoning
        print(
            json.dumps(
                seat.call_json(
                    "POST",
                    f"/api/games/{gid}/pregame/opening-hand/finish",
                    ohd_body,
                ),
                indent=1,
                sort_keys=True,
            )
        )
        persist_draft()
    elif args.command == "log":
        # `uid` here, not the bare debugger's-eye default: a real seat reading
        # its own log must not see the opponent's hidden-zone card names any
        # more than the state endpoint shows them.
        answer = seat.http.get(f"/api/games/{gid}/log", params={"uid": seat.uid})
        try:
            entries = answer.json().get("log", [])[-args.limit :]
        except ValueError:
            # Same tolerance `mcp.py`'s `game_log` has: a non-JSON body (an
            # error page from something in front of the API) is shown rather
            # than raised as a traceback out of a player's command.
            print(answer.text)
            return 1
        print(json.dumps(entries, indent=1, sort_keys=True))
    elif args.command == "script":
        # `playscripts.script_tools` is `mcp.run_script`/`list_scripts` too, so
        # the two front ends differ only in what they print.
        try:
            inputs = parse_inputs(args.inputs)
        except ValueError as exc:
            print(exc)
            return 1
        if args.script_command == "run":
            answer = script_tools.run(
                seat,
                seats.script_corpora,
                script=args.script,
                reasoning=args.reasoning or "",
                opposing_archetype=args.opposing_archetype,
                resume=args.resume,
                wait=args.wait,
                max_seconds=args.max_seconds,
                inputs=inputs,
                invoked_via="cli",
            )
        else:
            answer = script_tools.listing(
                seat,
                seats.script_corpora,
                opposing_archetype=args.opposing_archetype,
                inputs=inputs,
            )
        if isinstance(answer, str):
            print(answer)
            return 1
        if args.script_command == "run":
            # Matches `mcp.run_script`, which routes through `_tool_result` -
            # `list_scripts` does not (a listing judges scripts, it runs
            # nothing, so there is never anything here for a draft to be
            # captured by).
            emit(answer, state_for_draft=answer.get("state"), indent=1, sort_keys=True)
        else:
            print(json.dumps(answer, indent=1, sort_keys=False))
        note(answer.get("state"))
    elif args.command == "report":
        # A report is long prose full of quotes, braces and backticks. Passing
        # it as an argument means the shell sees it first, and command
        # substitution has already silently blanked parts of a real report -
        # so stdin is offered as the way to hand it over untouched.
        text = args.text if args.text is not None else sys.stdin.read()
        if not text.strip():
            print("refusing to file an empty report")
            return 1
        print(seat.call("POST", f"/api/games/{gid}/report", {"player": seat.player, "text": text}))
    elif args.command == "plan":
        # Mutates no `Game` and never blocks - `seat.declare_plan` goes
        # straight through `call_json`, the same as `report` above, rather
        # than `act()`'s release-and-wait flow.
        result = seat.declare_plan(
            board_read=args.board_read,
            my_role=args.my_role,
            plan=args.plan_text,
            expecting=args.expecting,
            because=args.because,
            confidence=args.confidence,
            for_turn=args.for_turn,
        )
        print(json.dumps(result, indent=1, sort_keys=True))
    elif args.command == "stage-advice":
        # Mutates no `Game` and never blocks - `seat.stage_advice` only arms
        # this seat's own next committing call, the same shared function
        # `mcp.stage_advice` calls.
        result = seat.stage_advice(args.step, args.whose_turn)
        print(json.dumps(result, indent=1, sort_keys=True))
        # S2 A6 review fix 2: persisted so the NEXT `seat_cli.run()` call -
        # a fresh process with a fresh `Seats()` - can pick this armed window
        # back up. `persist_draft` (not `emit`, which also attaches `draft`
        # to the answer - `mcp.stage_advice` never does that on its own
        # answer either) is exactly the save half of that.
        persist_draft()
    return 0


if __name__ == "__main__":
    sys.exit(run())
