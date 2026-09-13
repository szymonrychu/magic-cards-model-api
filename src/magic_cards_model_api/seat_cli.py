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
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .seatkit import (
    AUTOPASS_STEPS,
    AUTOPASS_WHOSE_TURN,
    DEFAULT_API,
    Seats,
    _parse_ids,
    _parse_pairs,
    api_trouble,
    autopass_target_error,
    block_until_wake,
    card_reference,
    parse_convoke,
    pass_until,
    run_action,
)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", required=True, help="your seat uid, given at dispatch")
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
    cast.add_argument("--target", default="", help="comma-separated card ids or player names")
    cast.add_argument(
        "--sources",
        default="",
        help="comma-separated mana source ids (default: all your untapped sources)",
    )
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
            "guessing. A card covered by a live PlayPermission instead (suspend's "
            "free cast once its last time counter is off, cascade, Past in "
            "Flames) needs no --keyword: it is picked up automatically."
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
    activate.add_argument("--sources", default="", help="comma-separated mana source ids")
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
            "the graveyard (CR 701.25a), each leaving the rest on top"
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

    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
            print(json.dumps(perform(), indent=1, sort_keys=True))
            return
        hold = args.hold or args.command in SPECIAL_ACTIONS
        result = run_action(seat, perform, hold_priority=hold, reasoning=args.reasoning)
        print(json.dumps(result, indent=1, sort_keys=True))
        note(result.get("state") if isinstance(result, dict) else None)

    if args.command == "state":
        current = seat.fetch_state()
        print(json.dumps(current, indent=1, sort_keys=True))
        note(current)
    elif args.command == "wait":
        woken = block_until_wake(seat, before=None, max_seconds=args.max_seconds)
        woken["state"]["wake_reason"] = woken["wake_reason"]
        print(json.dumps(woken["state"], indent=1, sort_keys=True))
        note(woken["state"])
    elif args.command == "cast":
        body: dict[str, Any] = {"player": seat.player, "dry_run": args.dry_run}
        body.update(card_reference(args.card))
        if args.spell_face.strip():
            body["spell_face"] = args.spell_face.strip()
        if args.target:
            body["targets"] = _parse_ids(args.target)
        sources = sources_or_all(args.sources)
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
        ):
            if value:
                body[field] = _parse_ids(value)
        if args.convoke:
            body["convoke"] = parse_convoke(args.convoke)
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
        sources = sources_or_all(args.sources)
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
        )
        print(json.dumps(result, indent=1, sort_keys=True))
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
        print(json.dumps(result, indent=1, sort_keys=True))
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
        print(
            json.dumps(
                seat.call_json("POST", f"/api/games/{gid}/pregame/mulligan", body),
                indent=1,
                sort_keys=True,
            )
        )
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
    return 0


if __name__ == "__main__":
    sys.exit(run())
