"""The agent-facing text the seat MCP server serves: one description per tool,
plus the server instructions shared by all of them.

Claude Code truncates every MCP tool description and the server instructions
at `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH` characters, so a tool served its
whole Python docstring loses everything past the cut. These are written for
the model reading them, each within `DESCRIPTION_CAP`; the docstrings in
`mcp.py` stay the developer documentation. Guidance shared by many tools is
stated once, in `SERVER_INSTRUCTIONS`, which the client loads into the system
prompt.
"""

from __future__ import annotations

DESCRIPTION_CAP = 4096

_SPECIAL_HOLD = (
    "HOLDS PRIORITY BY DEFAULT (a special action, CR 116.3; see instructions): "
    "pass `hold_priority=false` only to hand priority over."
)

_THEN_PASS = "`then_pass=true`: pass once afterwards (see instructions)."

SERVER_INSTRUCTIONS = """\
You are one seat in a Magic: The Gathering game run by a faithful rules engine. These rules \
apply to every tool.

Identity: pass your secret seat uid as `player_uid` on every call. You see everything public \
plus your own hidden cards; the opponent's hand and every library stay hidden.

`reasoning`: why you are doing this, in your own words. Required on every action tool for an \
agent seat (a missing one is refused with a 422 naming the field).

One call, one action: every action tool performs the action, checks once for your next moment to \
act, and returns in one answer: the outcome, `state` (the board it left behind), \
`available_actions` (the engine's own legal list; its `not_now` lists casts blocked only by \
timing or a prohibition, `castable_now: "no"` with the reason - never legal now), `recent_log` \
(tail of what happened, both seats, allowed and refused; `omitted` counts what it dropped, \
`game_log` has the rest) and `wake_reason`. Answers are compact JSON. You do not need \
battlefield_state or game_log after an action.

Nothing blocks (poll-and-return). If `wake_reason` says "nothing new yet", call again. Only \
wait_for_priority, pass_until and run_script wait, bounded by `max_seconds` (capped at 25; quiet \
waits are re-issued by the server for about two minutes). Use that, never Bash sleep between calls.

Decisions: a decision pending for you arrives shaped `input_required` (result_type \
"input_required"). Answer it with `decide`, pasting `pending_decision.answer_with`. A client \
retry of that call never re-runs the tool; the retry returns the decision as ordinary text - \
answer it with decide the same way. Decisions with exactly one legal answer (no legal attacker \
or blocker, a single legal target, a yes-branch that is illegal, one trigger to order, one mode) \
are answered by the server with reasoning "auto-answered by the seat harness ..." (recorded in \
the game log) and listed in the answer's `auto_answered` field; you only ever see decisions with \
a real choice.

Priority after an action (`hold_priority`):
- Release by default: cast, activate (non-mana), equip, level_up, reconfigure, \
declare_attackers/blockers. \
They use the stack; releasing is how they resolve. Set `hold_priority=true` to act again or \
respond to your own spell first (CR 117.3c: you keep priority after casting; the flag only \
decides whether the harness auto-passes for you).
- These hold priority by default: play_land, suspend, plot, foretell, turn_face_up, \
bring_companion, unlock_door (special actions: CR 116.3 gives you priority afterward, CR 116.1 \
they do not use the stack) and cycle (an activated ability, CR 117.3c). Releasing after these \
throws away the rest of your main phase.
- activate ALWAYS holds after a mana ability (CR 605.3b), whatever `hold_priority` says.
- decide never passes; whoever the engine gives priority to keeps it.
- `then_pass` (cast, activate, decide, play_land; default false): after the action succeeds, if \
you still hold priority and nothing is pending for you, it passes priority once exactly as \
`pass` would and returns the pass's answer, with the action's outcome under `first`. Use it when \
your next call would have been a bare pass (letting your own spell or trigger resolve).

`baton`: the token pass_baton gave you; acting tools without it are refused (BATON_*). \
Reads need none.

Common parameters: ids are comma-separated strings. `mana_sources`: source ids to tap; empty \
lets the engine's automatic payment tap only what the cost needs from your untapped sources. \
`dry_run`: true asks "could I?" without changing anything (never blocks).

CR 723 control (e.g. Emrakul, the Promised End): while `deciding_for` in the state names a \
player, their priority and pending decisions are yours - act for them with your own uid, even \
though `priority_holder` and `pending_decision.player` still name them. While \
`decisions_made_by` on your own player block names somebody else, nothing is yours but conceding.

Refusals come back in the engine's own words: read the reason, fix the call, retry. When \
something looks wrong, report_problem and play on.
"""

BATON_DESCRIPTION = (
    "Pass this token as `baton` on every acting call for this seat. `state: next` means it "
    "goes live when the current holder's priority window closes - await_baton until then. "
    "Refusals: BATON_NOT_YET, BATON_SPENT, BATON_STALE, BATON_REQUIRED, BATON_BUSY; "
    "battlefield_state, game_log and list_scripts need none."
)

TOOL_DESCRIPTIONS: dict[str, str] = {
    "pass_baton": """\
Orchestrator only; never acts in the game. Mints a `baton` for seat `player_uid`: `state: live` \
when the seat has none, else `state: next`, made live by the server when the current holder's \
call that gives priority away returns (an earlier unpromoted next dies). Never revokes, never \
blocks. Answers `live_holder_since`, `last_commit`, `in_flight`. `revoke` (recovery only): live \
at once; an in-flight wait is interrupted (BATON_REVOKED), an acting call finishes first \
(`revoke`: `applied` or `queued_behind:<tool>`).""",
    "await_baton": """\
Future agent's wait; never acts. Blocks until `baton` goes live, then answers `{live: true, \
last_commit, pending_decision, board}` (board: compact battlefield_state) - you are current: \
revalidate the plan and act with this baton. Else `{live: false, state}`: `next` after \
`max_seconds` (max 600; call again), `spent`/`stale` at once (end your run).""",
    "wait_for_priority": """\
Check whether it is your moment to act, then hand you the board. Wakes when: a decision is \
waiting for you (answer with decide; arrives input_required); you have priority (act, or pass); \
the game is over; a step nobody gets priority in (untap, cleanup) while you are active player \
(nothing asked; it ends on its own); the pregame is waiting on you (your CR 103.5 mulligan or CR \
103.6 opening-hand actions). Waiting on another seat's pregame declaration is not a wake. Under \
CR 723 control it wakes you for the controlled player too.

Action tools already check this; use this when you have done nothing yet, or to wait.

`max_seconds`: use this instead of Bash sleep. 0.0 (default) checks once. Positive (max 25): \
keeps checking that long, re-issued by the server while nothing is new for about two minutes; \
then `wake_reason` says `nothing new yet - call again to check` - a quiet stretch, not a \
failure: call again the same way. `the game is gone: ...`: stop calling tools for it. `the wait \
was interrupted`: the call was cancelled.

Only an advisor gets `available_actions` here; a seat's list rides its actions.

`arm`, `arm_reasoning`, `opposing_archetype`, `inputs`: as for pass_until's `arm`, judged on a \
real wake; arming never runs anything - send the returned `call` yourself.

`wake_to_draft` (advisor only, default false): also wake when the board changes before your \
human has priority and no draft is staged - the cue to stage_advice. A staged draft shows in \
every answer as `draft`: `{"status": "held"}`, `{"status": "posted", "log_seq": ...}`, or \
`{"status": "stale", "failed": "window"|"board"|"legality"}` (dropped; draft another).""",
    "battlefield_state": """\
The game as you may see it: your hand, both sides' public board (battlefield, stack, graveyards, \
exile), life, mana pools, turn/step, priority, combat. The opponent's hand and libraries are \
hidden (sizes only). Your uid alone gets all of it - normally what you want.

`sections`: comma-separated top-level keys to keep: `battlefield`, `hands`, `graveyards`, \
`exile` (brings `exile_pile`), `command`, `stack`, `combat`, `players`, `play_permissions`, \
`pregame`, `pending_decision`, `recent_log`, `known_cards`, `library_contents`, \
`sideboard_contents`. Empty (default) = all but the last two, which come only when named. \
Unknown names are refused with the list. Turn, step, priority and result are always kept; \
`focus.omitted_sections` lists what you left out - an absent section is your narrowing, not an \
empty zone.

`side`: `both` (default), `mine` or `opponent`, resolved from your uid; `focus` (`you`, \
`opponents`) names the usernames - rely on it in a mirror. It narrows every per-owner section, \
NOT `combat`, `pregame`, `pending_decision` or `recent_log`.

`known_cards`: hidden cards you know: `hand` (revealed or looked-at cards still in another \
hand), `library` (`index_from_top`; a shuffle ends it), `sideboard` (revealed to you). \
`library_contents`/`sideboard_contents`: your own, as `{card, count}` sorted by name - never \
order, never the opponent's.

`pending_decision` is the question for `decide` (see decide, per `kind`). On a look-then-split, \
`moved_label`/`kept_label` say where each half goes and are authoritative when non-empty - the \
`scry` kind is not (Devourer of Destiny). On a `choose_cards` (discard, library search): `exact` \
says whether you must name exactly `amount` cards (CR 701.23d) or 0 to `amount` (CR 701.23b; \
nothing is a real answer); `card_labels` names each `cards` entry positionally (your library is \
stripped from state); `default_cards` is what silence takes.

`recent_log`: the tail of the log; `game_log` has it all.""",
    "cast": """\
Cast a spell. `card`: name or id. `targets`: comma-separated card ids, player names, or stack \
ability ids - the spell's own targets (CR 601.2c), never cost payments ("too many targets"). \
`modes`: comma-separated chosen modes. `x_value`: X.

`spell_face`: a split card (Wear // Tear) or an adventurer (CR 715) is offered as separate \
`cast` rows in `available_actions`, each with its own `spell_face` and cost; send that row's \
string exactly (`spell_face="Tear"`, `spell_face="Swift End"`). Empty = front half. An Adventure \
resolves into exile (CR 715.3d), from where you may cast the creature later.

`keyword`: the permission or alternative cost (CR 118.9) you cast by. Read the card's \
`cast_options` in battlefield_state (keywords offered, cost, `usable_now`/`why_not`); never \
guess. Graveyard: "Flashback", "Mayhem" (discarded this turn). Exile: "Plot", "Foretell" \
(earlier turn), "Madness" (its own madness trigger, this turn), "Airbend" ({2}). Hand, paying \
the keyword's cost instead of the mana cost: "Evoke", "Dash", "Morph", "Megamorph", "Disguise", \
"Bestow", "Warp"; "Sneak" (CR 702.190a: only in your own declare blockers step, also returning \
an unblocked attacker you control, named in `choosing`; a permanent enters tapped and attacking \
what it attacked). A granted keyword is still named by the keyword. A card under a live \
permission in `play_permissions` (Past in Flames, a "this turn" exile) needs no keyword. Cascade \
hits and a suspended card's free cast: accepting the "you may cast it" in `decide` casts it at \
once - answer its follow-up questions one `pending_decision` at a time; no later cast.

Costs that name objects (comma-separated ids; too few is refused with the engine's count):
- `choosing`: what an ALTERNATIVE cost needs. Solitude's evoke is "Exile a white card from your \
hand" - `choosing` is that card; likewise Grief's black card, Force of Negation's blue card, \
Fireblast's Mountains, Sneak's attacker. Announce and name in one call: `cast(card="Solitude", \
keyword="Evoke", choosing="<white card id>")`.
- `alternative_cost`: an alternative cost with no keyword - send the option's `alternative_cost` \
string (Force of Negation: "exile a blue card from your hand"); `choosing` feeds it. Only one \
alternative cost (CR 118.9a).
- `sacrificing`, `discarding`, `exiling`, `tapping`: objects a printed ADDITIONAL cost asks for \
(CR 118.8a; the mana cost is still paid): Fling's creature, Cathartic Reunion's two cards.
- `optional_costs`: keywords from the cast row's `optional_costs`: "Kicker", "Kicker:{W}" (which \
of two), "Multikicker:2", "Gift:<opponent>", "Blight:<id>", "Bargain:<id>", "Reveal:<id>", \
"Collect evidence", "Entwine" (list every mode in `modes`), "Escalate:<id>" (a non-mana escalate \
discard), "Harmonize:<creature id>" (with keyword="Harmonize"), "Pay life" (the life half of \
"pay N life or {M}"). Entries marked `understood: false` cannot be paid - plan without them. \
Empty = none.
- `delve`: graveyard ids exiled, one per generic mana.
- `convoke`: untapped creatures; a bare id pays generic, `<id>:W` pays a coloured symbol.
- `improvise`: untapped artifacts, one generic each (the cast row lists them).

Then it releases priority and checks once, unless `hold_priority=true` (cast again, or respond \
to your own spell, first). """
    + _THEN_PASS,
    "suspend": """\
Suspend a card from your hand (CR 116.2f, CR 702.62a): pay its suspend cost and exile it with \
its time counters. `card`: name or id in your hand. `x_value`: Suspend X's X (required). One \
counter comes off in each of your upkeeps; when the last one does, you may cast it without \
paying its mana cost - that offer arrives as a decision, and accepting it in decide casts it \
at once.

"""
    + _SPECIAL_HOLD,
    "plot": """\
Plot a card from your hand (CR 116.2k, CR 702.170b): pay its plot cost and exile it. `card`: \
name or id in your hand. On any later turn cast it from exile without paying its mana cost: \
`cast(card=..., keyword="Plot")`.

"""
    + _SPECIAL_HOLD,
    "foretell": """\
Foretell a card (CR 116.2h, CR 702.143b): pay {2} and exile it from your hand face down \
(opponents see only a face-down card). `card`: name or id in your hand. On any later turn cast \
it from exile for its foretell cost: `cast(card=..., keyword="Foretell")`.

"""
    + _SPECIAL_HOLD,
    "cycle": """\
Cycle a card from your hand (CR 702.29a): pay its cycling cost, discard it, draw a card \
(typecycling: search for the named type). `card`: name or id in your hand. `ability_index`: \
which cycling ability, for a card printing more than one - almost always 0.

Only for an actual cycling ability: a card discarding itself as another ability's cost (Waker of \
Waves, Boseiju's Channel) is `activate` - a refusal "has no cycling ability number N" means \
check there. Cycling uses the stack; it does not resolve in this call.

HOLDS PRIORITY BY DEFAULT - but cycling is not a special action: it is an ordinary activated \
ability, and CR 117.3c keeps priority with you after activating it. Pass `hold_priority=false` \
to release (the draw only resolves once priority is released).""",
    "play_land": """\
Play a land from your hand (your one per turn, unless an effect allows more). `card`: name or id.

HOLDS PRIORITY BY DEFAULT: a land play is a special action (CR 116.2a, CR 116.3). Releasing \
throws away the rest of your main phase - including the spell the land just paid for. Pass \
`hold_priority=false` only to hand priority over. """
    + _THEN_PASS,
    "activate": """\
Activate one ability of a card you control: a permanent's ability, or one that works from \
another zone because its cost or effect says so (CR 113.6j: Waker of Waves' `{1}{U}, Discard \
this card`, Boseiju's Channel, from hand). Use `cycle` only for an actual cycling ability; equip, \
level_up and reconfigure have their own tools.

`card_id`, `ability_index`: the number from that card's `abilities` list in the same state view \
you act on (0-based). On the battlefield, intrinsic mana abilities come after printed ones; in \
hand, graveyard or exile the list holds only abilities that function there, renumbered from 0 - \
never carry a number across zones.

`targets`: comma-separated ids. `color`: W, U, B, R, G or C when the ability chooses a color of \
mana (Birds of Paradise); for N mana in any combination, one per mana (`U,R,R`). \
`sacrificing`/`discarding`/`exiling`: ids for the cost's object-naming part (CR 118.3), as many \
as it asks: Goblin Bombardment is `activate(..., sacrificing="<creature id>", targets="<any \
target>")`; `exiling` also covers graveyard costs (Scrapheap Scrounger). `x_value`: X in the \
cost (CR 107.3a, Fiend Artisan); omitted when needed is refused, never taken as 0.

A MANA ability ALWAYS HOLDS PRIORITY, even with `hold_priority=false`: it does not use the stack \
(CR 605.3b), and a pass could end the step (CR 500.2) and empty the mana it made (CR 500.5). \
Spend it, or `pass` deliberately to discard it. Any other ability goes on the stack: it releases \
priority and checks once, unless `hold_priority=true`. """
    + _THEN_PASS,
    "equip": """\
Attach an Equipment you control to a creature you control, paying its equip cost (CR 702.6a; \
sorcery timing, CR 307.5). `card_id`: the Equipment; `target`: the creature's id. Legal equips \
are `available_actions` rows of kind `equip` (cost, candidates); equip has no `abilities` index \
for `activate`. It uses the stack: releases priority and checks once unless \
`hold_priority=true`; the Equipment attaches when it resolves.""",
    "level_up": """\
Put a level counter on a leveler you control, paying its level up cost (CR 702.87a; sorcery \
timing, CR 307.5). `card_id`: the leveler. Legal level ups are `available_actions` rows of kind \
`level_up` (cost); level up has no `abilities` index for `activate`. It uses the stack: releases \
priority and checks once unless `hold_priority=true`; the counter goes on when it resolves.""",
    "reconfigure": """\
An Equipment's reconfigure (CR 702.151a; sorcery timing, CR 307.5). `card_id`: the Equipment. \
With `target` (another creature you control), attach it; with `target` empty, unattach it (only \
while attached). Legal ones are `available_actions` rows of kind `reconfigure`: the attach row \
has target candidates, the unattach row `unattach: true`; no `abilities` index for `activate`. \
It uses the stack: releases priority and checks once unless `hold_priority=true`.""",
    "turn_face_up": """\
Turn a face-down permanent you control (morph, megamorph, disguise or manifested) face up, \
paying the cost that reveals (CR 702.37e, CR 701.40b-d). `card_id`: the permanent's own id (from \
your board, even face down). `procedure`: only for a manifested permanent that also has morph or \
disguise, choosing which procedure (CR 701.40c-d); else empty.

"""
    + _SPECIAL_HOLD,
    "bring_companion": """\
Pay {3} to put your companion from outside the game into your hand (CR 116.2g): once per game, \
in your own main phase, with the stack empty. Names no card - the companion was chosen at deck \
load.

"""
    + _SPECIAL_HOLD,
    "unlock_door": """\
Unlock a locked half ("door") of a Room you control by paying that door's mana cost (CR 709.5e). \
`card_id`: the Room's id; `door`: the door's printed name (e.g. "Ritual Chamber"). Only in your \
own main phase with an empty stack.

"""
    + _SPECIAL_HOLD,
    "declare_attackers": """\
Declare all attackers at once, in your declare attackers step. `attackers`: JSON list `[{"card": \
"<attacker id>", "defender": "<player name or planeswalker id>"}]`. Releases priority and checks \
once, unless `hold_priority=true`.""",
    "declare_blockers": """\
Declare all your blocks at once, in the declare blockers step. `blocks`: JSON list `[{"blocker": \
"<your creature id>", "blocked": "<attacker id>"}]`. Releases priority and checks once, unless \
`hold_priority=true`.""",
    "decide": """\
Answer your pending decision (CR 608.2d). Read `pending_decision.answer_with` and paste it: the \
ready call with this question's `generation` and the answer parameter (`answer_field`); fill the \
<placeholder>, keep the rest. What to choose, by `kind`:

- `choose_cards` (discards, reveal-and-choose, every library search): `card_ids` from `cards`. \
The count is `exact`, never the kind: true = exactly `amount` ids; false = 0 to `amount` (CR \
107.1c) and the empty answer is a real answer (the decline). A search is one choice over the \
whole zone, not a walk (CR 701.23a): `cards` holds every legal candidate at once - name the one \
you want. CR 701.23b (a stated quality) is exact false, finding nothing allowed; CR 701.23d (a \
quantity) is exact true - choose which, never whether. `pending_decision.source` names the card \
asking, so what you may find: Wooded Foothills finds Breeding Pool ("Land - Forest Island") - \
pick the best, not the first. Read `card_labels` (one name per `cards` entry, same order) and \
`card_details` (oracle text): the library is not a public zone (CR 400.2) and is stripped from \
state. `default_cards`, if set, is what silence costs.
- `choose_targets` (CR 603.3d): `targets`, up to `target_maximum` from \
`pending_decision.targets`; empty = no target.
- `may`, `pay_life`, `pay_energy`, `pay_mana`: `accept` true/false. `pay_mana` asks the targeted \
spell's controller (`pending_decision.player`); with accept=true it takes `mana_sources` (a \
subset of `pending_decision.mana_sources`). `affirmative_is_legal` false = yes is refused. A \
`may`'s `default_may` is what silence costs.
- `pay_energy_amount`: `energy_amount`; `pay_life_amount`: `life_amount`; `choose_an_amount` \
(nothing paid): `amount`. Each 0 to `pending_decision.amount`; 0 declines.
- `choose_a_quality`: `choice` = a color letter from `options` (empty `options`: a card name or \
creature type). `choose_a_mode`: `choice` = the mode's exact text from `options`.
- `scry` and `surveil` (any look-then-split): `moved` = the `cards` you opt into; the rest form \
the other half, ordered by `top_order` (ids, topmost first) if it goes on top. Empty `moved` \
declines. `pending_decision.exact` true = exactly `pending_decision.amount` ids; false = 0 to \
`pending_decision.amount` (Devourer of Destiny: at most one). Destinations: `moved_label` (your \
cards) and `kept_label` (the rest), in the card's words - authoritative; read and quote them. \
The kind is not the destination: Devourer of Destiny arrives as `scry`, keeps `moved` on top, \
and the rest are exiled. Only when both labels are absent or empty assume the keyword pair: scry \
sends chosen cards to the bottom, rest on top (CR 701.22a); surveil sends them to the graveyard, \
rest on top (CR 701.25a).
- `opening_hand_action` (CR 103.6): `accept`; no is always legal.
- `put_on_top_or_bottom`: `top` (asked of the spell's owner). `put_into_hand_or_graveyard`: \
`to_hand`. `choose_pile` (CR 700.3): `pile_one`; `cards` is pile one.
- `choose_players` (proliferate): `usernames` from `players`; empty declines; also answer its \
twin `choose_cards`.
- `order_triggers` (CR 603.3b): `order` = every id in `cards` once, bottom of the stack first \
(the last listed resolves first); read each in `card_details`.
- `assign_combat_damage`: `damage` = JSON list of {"target", "amount"}: blockers from \
`cards[1:]` (`cards[0]` is the attacker) or, with `trample`, the player/planeswalker in \
`targets`; sum exactly `amount`; trample over only once every blocker has its `lethal_damage` \
(CR 702.19b).
- `mulligan`, `declare_attackers`, `declare_blockers`: answerable here too.

`generation` guards against a replaced question (a fetch, then the shockland's pay 2 life); on a \
mismatch, re-read.

DECIDE NEVER PASSES PRIORITY unless `then_pass=true`. It checks once after; `held_priority` true \
= your move (respond, or pass yourself), false = someone else has priority.""",
    "pass": """\
Pass priority once: "I am done for now" (CR 117.3d). Use it only when you hold priority once \
with your next action already decided - e.g. in response to something. For any stretch of \
windows you will not act in use `pass_until`: repeated single passes cost a round trip and an \
agent turn each. Under CR 723 control this passes for the player you control.

`outcome`: "resolve_top" (both passed with a non-empty stack: its top resolved, priority goes \
round again), "end_step" (both passed on an empty stack: the step ended and the next began; \
`settled_through` lists steps crossed that give no priority), or "passed" (the other seat \
decides now). No `hold_priority`; a refusal returns at once.""",
    "pass_until": """\
Keep passing priority until something worth looking at happens - for any stretch you will not \
act in (DSL-first: see `arm`).

`step`, in turn order: upkeep, draw, precombat_main, beginning_of_combat, declare_attackers, \
declare_blockers, first_strike_damage, combat_damage, end_of_combat, postcombat_main, end_step. \
`first_strike_damage` is the first of two damage steps when a first or double striker fights (CR \
510.4); `state.first_strike_damage_step` tells which you are in. Naming the step you are in \
means its NEXT occurrence. `whose_turn`: "mine" (default; under CR 723 control the controlled \
turn counts, its combat declared with nobody), "opponent", or "any".

`finished: true`: a stop fired, named in `stopped_because`: destination; a decision for you \
(input_required); a new stack object you could answer; a combat stop; a refused pass; game over; \
the game is gone. `finished: false`: the opponent holds priority or must answer; pass the \
answer's `resume` back verbatim as `resume` (with `max_seconds` again) and the run continues as \
one. Without it a fresh run starts where you stand, and no stop fires in the step a run starts \
in. `passes` counts passes this call.

Background: after a `finished: false`, the server keeps running that run through the opponent's \
turn (only passes it would make, same stops), so you can `declare_plan(for_turn="next")` \
meanwhile. It halts before any pass on your own turn and before the pass that would end the \
opponent's turn. Call again with the same `resume` to continue from where it got; the answer's \
`background` field says what it did. Any acting tool call stops it; declare_plan, \
battlefield_state, game_log, list_scripts and report_problem do not.

`max_seconds`: 0.0 (default) returns the moment it would wait; positive (max 25) keeps retrying \
that long.

Stops on the way:
- `stop_on_stack` (default true): hands back an opponent's new spell or trigger while it is \
still on the stack and priority is yours - your only chance to respond (CR 117.4). It passes \
over objects you cannot respond to (only pass, concede or a mana ability legal) unless an armed \
play applies; your own objects always stop it. `auto_passed` lists what was passed over (whole \
run) - read it first. Set `stop_on_stack=false` only when you will respond to nothing.
- `stop_when_attacked` and `stop_when_you_can_attack` are off by default. \
`stop_when_attacked=true` stops at your declare-blockers step when attacked; without it you will \
block nothing and take the whole attack (CR 509.1: no stack for stop_on_stack to catch). Turn it \
on when you have blockers. `stop_when_you_can_attack=true` stops at your declare-attackers step; \
without it you will attack with nothing that turn, silently (CR 508.1: no stack either). Turn it \
on for a run past combat when you could attack; off says "I am not attacking this turn". Neither \
checks legality.

Passing by hand is still a play: holding priority with mana up represents a trick; auto-passing \
always says you have nothing, so pass by hand where the pass is part of the game.

`reasoning` is said once, threaded onto every pass.

`arm`: script ids (list or comma-separated), priority order, judged whenever you hold priority \
on the run, BEFORE it passes. One applying stops the run (named in `stopped_because`), first in \
`scripts.applicable` with `armed: true` and its `call` ready - you send it; arming never runs \
anything. Two = `armed_conflict`. `arm_reasoning` required with `arm`; \
`opposing_archetype`/`inputs` as for run_script. DSL-first: arm a script for any multi-step or \
repetitive sequence you see coming (a mana loop: a `repeat` move).""",
    "run_script": """\
Run one of your play scripts: its moves in order, then the one `next` script that applies. \
DSL-first: write and run a play-script for any multi-step or repetitive sequence (a mana loop: a \
`repeat` move, run to a target or threshold) instead of chaining manual calls; keep manual calls \
for one-off decisions.

Give exactly one of `script` (a registered id, or one from stage_script) or `body` (an inline \
play document, staged as stage_script does and run in this call; a schema or lint refusal \
returns `{"errors": [...]}` and nothing runs).

The runner waits for the script's `when` (passing with every stop on), checks its conditions, \
makes each move as a hand-played move (refusals are the engine's), answers decisions from the \
move's `answers`, and passes only over its own objects until they resolve - never beyond its \
last move unless that move is `pass`.

`reasoning` is required (sent with every move). `opposing_archetype`: the opponent's archetype \
as you read it, `*` (default) if unknown; a script listing only other archetypes does not apply. \
`inputs`: facts the board cannot show, by name (`{"prefer_tempo": true}`); may carry \
`opposing_archetype`, but naming two different archetypes is refused. `wait=false`: do not wait \
for `when`; an off-step script is not applicable. `max_seconds` caps this call (max 25).

Answer: `script_exit` beside a narrowed `board`. `script_exit.exit`: `chain_complete`, \
`not_applicable` (`failing_condition`: `list`, `index`, `seen`), `needs_input` (named), \
`invalid_input` (`name`, `value`, `reason`), `unforeseen` (`stop_reason`), `interrupted` \
(someone else acted), `ambiguous` (two cards or next scripts tie), `refused` (`refusal`), \
`stale`, `unknown_outcome`, `needs_plan` (call declare_plan first), `already_ran` (this turn and \
step) or `advised` (advisor: move 1 proposed, the rest `unvalidated`). \
`moves_done`/`moves_total`. `exit` null = the time cap came first: pass `script_exit.resume` \
back verbatim as `resume`.""",
    "stage_script": """\
Stage an inline play document as an ad-hoc script for this game, without running it - for a \
multi-step or repetitive sequence you face now (a mana loop: a `repeat` move). DSL-first: script \
such sequences instead of chaining manual calls. Staging never runs anything.

`body`: one play document in the registered-script format; its `id` is replaced with a fresh \
one. `reasoning`: why you are scripting this (recorded for the post-game review). Answer `{"id": \
..., "warnings": [...]}`: pass `id` as run_script's `script`; `warnings` are conflicts with your \
other scripts, never a refusal. A schema or lint refusal returns `{"errors": [...]}`. \
`run_script(body=...)` stages and runs in one call.""",
    "list_scripts": """\
Your play scripts, highest priority first, judged against the board now; nothing runs. \
DSL-first: check here before chaining manual calls for a multi-step or repetitive sequence (or \
stage_script one for this board).

Each says whether it `applies`; if not, `failing_condition`: the `list` that stopped it \
(`priority` = disabled, `opposing_archetypes`, `when`, `true`, `false`, or `hypothetical` = \
needs engine facts this listing could not fetch, `seen` says why), its `index`, and what it \
`seen`. `needs_input` names a required input you did not supply. `conflicts`: scripts that could \
both apply at one decision point. `runtime_conflict` true = more than one applies now \
(`applicable` names them). `opposing_archetype` and `inputs` mean what they mean for \
run_script.""",
    "mulligan": """\
Declare your mulligan (CR 103.5) before the game starts; `battlefield_state`'s `pregame` says \
who is still owed. `take=true` mulligans (shuffle back, draw seven); `take=false` keeps. Keeping \
bottoms one card per mulligan taken (`pregame.cards_to_bottom`): `bottom` = that many ids from \
your hand; empty lets the back of your hand go.

HTTP 409 with `overruled`: a backstop already declared for you and yours was NOT applied. \
Retrying cannot help - you hold the dealt hand; say so with report_problem and play on.""",
    "opening_hand_action": """\
Take an opening-hand action (CR 103.6), or finish. `pregame.opening_hand_options` lists yours \
(Leyline of the Void onto the battlefield, Gemstone Caverns' reveal); \
`pregame.opening_hand_actor` says whose turn it is. `card`: one id from that list. Call with no \
`card` when done - that passes to the next seat, and the last seat finishing starts turn 1.

HTTP 409 with `overruled`: a backstop already declined for you and the action was NOT taken (the \
card stays in hand). Retrying cannot reopen it; report it and play on.
409 `repeated`: nothing moved since it was refused; never resend, follow `detail`.""",
    "declare_plan": """\
Declare how you read your turn. Required: an agent seat's first mutating action of its own turn \
is refused (422, naming `plan`) until this is called for that turn.

Pass first, plan second: never compose a plan while you hold priority with nothing to do - \
nothing moves while you hold it. Default to `for_turn="next"` during your opponent's turn once \
you do not hold priority (e.g. while pass_until runs in the background).

One short sentence each:
- `board_read`: what matters that the engine's compact board (free when blank) would not show.
- `my_role`: your role in the matchup; required on your first declaration only, blank carries \
the last forward.
- `plan`: what you intend this turn, and why.
- `expecting`/`because`/`confidence` ("low"/"medium"/"high", default "medium"): what you expect \
the opponent to do and the read behind it - always worth writing fresh.

`reuse_previous=true` plus one line in `still_because` copies your last plan and prediction \
forward when nothing changed. `for_turn`: "current" (default) or "next"; "next" while your own \
turn is under way is refused (409, `own_turn_in_progress`). `plan` and `expecting`/`because` are \
required unless reused.""",
    "stage_advice": """\
Advisor only (refused for a seat): capture your next committing call (`cast`, `activate`, \
`pass`, ...) as a draft for the window (`step`, `whose_turn`) instead of posting it now, so it \
is ready the instant your human gets priority there. `step`/`whose_turn` as for pass_until (the \
step your human is in means its NEXT occurrence). It is probed like a proposal and held; answers \
carry `draft` (see wait_for_priority) until it posts itself or goes stale. Staging again \
replaces it - one draft at a time.""",
    "report_problem": """\
Log a problem the moment something looks wrong, then carry on (also write it to your \
problems.md). `text`: what you did, what the rules say should have happened, and what happened \
instead. `category`:
- `engine` (default): a rules/engine/api bug.
- `dsl`: a play the play-script DSL could not express - name the missing condition or move kind. \
Already covered: `declare_attackers`, `declare_blockers` and a bare `pass` as moves; \
`top_order`/`position`/`targets`, a standalone `pending_decision` answer, `compare` and `all_of` \
as conditions.
- `ux`: friction with these MCP tools (overcomplicated calls, confusing answers, mergeable steps).
Anything else is refused.""",
    "game_log": """\
The full action log: everything both players did or tried, allowed and refused, newest last, \
each with its board snapshot (your view). Usually unnecessary: every board-carrying answer has a \
`recent_log` tail. Use it when that tail's `omitted` is above zero, to go further back, or for \
an entry's full snapshot. `limit`: keep the last N (default 40). `since`: a `seq` you have \
(typically `recent_log`'s `last_seq`) - returns only newer entries; the answer's `last_seq` is \
where to resume.""",
}
