MTGO game log fixtures and format notes (T-123, slice S6, Task 15)
=================================================================

Researched 2026-11-12. These notes are the contract for the MTGO parser
(Task 16, sim/mtgo.py) and the scenario converter (Task 17). Every format
detail below is tagged:

  [V-DAT]  verified in real Match_GameLog_*.dat bytes (source S1)
  [V-PD]   verified in real rendered logs exported by PDBot (source S3)
  [V-CODE] only seen as a string another parser matches on (source S2);
           the parser expects it, but no real sample was seen here
  [UNVERIFIED] inferred or assumed; treat as a guess

There is no official documentation of the MTGO game log format; everything
here comes from real files and from open-source parsers.


1. Sources
----------

S1  BigPeet/mtgo_utils, https://github.com/BigPeet/mtgo_utils
    commit 9b092947582a3a1239c12e7e36778e8b6a3eafa9 (2021-05-13), GPL-3.0.
    C++ Match_GameLog_*.dat parser (lib/mtgo_utils/src/gamelog.cpp) plus
    four real .dat files with their decoded .log text
    (test_data/Match_GameLog_TEST01..04.dat/.log, 723 decoded lines, 2021
    client, Modern). Used for: binary layout, @P/@[..@:..:@]/@i markup, the
    2021 wording. Nothing copied: GPL-3.0 text cannot go into this MIT repo.

S2  cderickson/MTGO-Tracker, https://github.com/cderickson/MTGO-Tracker
    commit f3ffb20c77d50bed852bf95883de9ce1e8e9b9e5 (2026-04-28), no licence
    file (all rights reserved by default). Python parser (modo.py) of the
    same .dat files, read with encoding latin1. Used for: which phrases a
    long-lived parser keys on ("chooses to not play first", "has lost the
    game", "has lost the game due to disconnection", "loses because of
    drawing a card", "has lost connection to the game", "This is a
    @iMultiplayer Commander@i game."), and the evidence that player names
    can hold spaces and periods (its alter() maps " " to "+" and "." to "*"
    before splitting on whitespace). Nothing copied.

S3  PDBot logs, https://logs.pennydreadfulmagic.com/ (Penny Dreadful,
    developed by Silasary; "unofficial Fan Content permitted under the Fan
    Content Policy", no licence for the log text). Rendered logs of real
    games watched by the PDBot client: /match/<id>/ (HTML) and
    /export/<id> (text, CRLF). Read 2026-11-12: matches 279523934,
    270622328, 280826345 (2025) and exports 291036914..291040506 (10 matches,
    2026), about 2400 lines. Used for: the rendered "[Card]" form, the
    "[HH:MM:SS] " prefix, chat/observer/header lines, multi-line entries and
    the 2025-2026 wording (draw-step "draws a card.", "counters X with Y.",
    "loses the game.", "wins the game.", "puts X onto the battlefield
    attacking P"). Nothing copied.

S4  MTGO getting-started guide, https://www.mtgo.com/getting-started/getting-started-gameplay
    Only says the Game Log pane sits on the right of the battlefield. No
    description of copy/export was found anywhere: what a user gets when
    copying text out of the in-client Game Log pane is [UNVERIFIED].

All four fixtures are HAND-WRITTEN in the verified line shapes (player
names, games, card object ids and timestamps are invented; card names are
real Magic cards). No real log text is copied, because S1 is GPL-3.0 and S2
and S3 carry no licence that allows it.


2. Source forms
---------------

2a. Match_GameLog_<id>.dat [V-DAT]
    Written by the client per match (S1 finds them under
    Users/*/AppData/Local/Apps/2.0). Binary: a header of GUID-like
    "$XXXXXXXX-XXXX-..." strings, then one framed record per log entry.
    Each record is a few non-printable framing bytes (lengths, timestamps)
    followed by printable text that starts with "@P". There are no newlines
    between records. Decode as latin1 (S2) and split on "@P"; drop every byte
    outside 0x20-0x7e. Record text quirks:
      - "joined the game" records start with "@P@P" (both games of a match).
      - "Turn N: Name" has no closing period; framing bytes follow the name
        directly, so a decoder must cut after the player name (S1 does it by
        matching the known player names).
      - Other records end at the sentence-final "." outside parentheses;
        what follows is framing noise (S1 CutOff()). A second sentence can
        follow: "casts @[X@:..:@] (with Sunburst 2). (X is 2)."
    The fixtures dat_*.txt hold the DECODED form: one record per physical
    line, still starting with "@P", markup kept, framing bytes already
    removed (fixtures must be plain ASCII). A loader for real .dat bytes is
    a separate step that is not part of these fixtures.

2b. Rendered text with "[Card]" brackets [V-PD]
    What the PDBot export shows: "[HH:MM:SS] " prefix on game lines, card
    references as "[Card Name]", no ids, no @P. Plus non-game lines (section
    4). Whether the in-client Game Log pane copies as this form, as bare
    names, or with markup is [UNVERIFIED] (S4). Recommendation: accept both
    2a and 2b, and bare text with no markup at all.

Wording drift between client versions (both shapes are real):
    2021 [V-DAT]: "X puts triggered ability from @[C@:..:@] onto the stack
                   targeting P (reminder)."
                  "X activates an ability of @[C@:..:@] targeting P (text)."
                  token: "creates a Zombie token."  (lower case "token")
                  draw step draws are NOT logged.
                  planeswalker attacked: "X 's @[C] is being attacked by .."
                  (space before 's)
    2025-26 [V-PD]: "X puts a triggered ability from [C] onto the stack
                   (reminder) targeting P."   ("a", target after the text)
                  "X activates an ability of [C] (text) targeting P."
                  token: "creates a Goblin Token."  (upper case "Token")
                  draw step: "X draws a card."
                  planeswalker attacked: "X's [C] is being attacked by .."
                  attack lines have no closing period
                  "X puts a triggered ability" and "puts triggered ability"
                  both occur in 2025 PD logs.


3. Markup rules
---------------

  @P               record start [V-DAT]; strip (repeatedly: "@P@P").
  @[Name@:a,b:@]   card reference [V-DAT]. Name is the card name as printed
                   (commas, apostrophes, hyphens kept: "@[Azusa, Lost but
                   Seeking@:43070,541:@]", "@[Kolaghan's Command@:...]").
                   a = catalogue id (same for every copy of a card),
                   b = in-game object id. The object id stays while the
                   object stays in one zone and CHANGES on a zone change
                   (S1 TEST04: Dryad of the Ilysian Grove is 591 on the
                   battlefield and 599 when targeted in the graveyard). A
                   reference to a known card with a new object id is the only
                   trace that it changed zones (see section 6).
  @i ... @i        italics inside reminder text [V-DAT]: "(Prowess
                   @i(Whenever you cast ...)@i)". Strip "@i".
  [Name]           rendered card reference [V-PD]; same name rules, no ids.
  "<card> token"   a token object referenced by its name plus the word
                   "token" after the markup [V-DAT]: "@[Clue@:..:@] token",
                   "draws a card with @[Clue@:..:@] token's ability". In the
                   rendered form the name already holds it: "[Goblin Token]".
  (text)           reminder/rules text of the spell or ability, after the
                   card or after the targets; truncated by the client to
                   about 100 characters ending in "...)". It can contain
                   periods, commas, "targeting", apostrophes and nested
                   parentheses. Never parse targets out of it.
  "( Draw a card.)" activated ability text starts with "( " (open paren and
                   a space) [V-DAT][V-PD]; it holds the effect only, not the
                   cost.

Recommendation for Task 16: take card names and targets from the markup
spans of the ORIGINAL line (before strip_markup), because after stripping a
card name can no longer be told apart from the words around it ("casts
Sword of Fire and Ice targeting ..."). strip_markup output is for matching
the kind regex only.


4. Player names
---------------

Player names come from the log itself, in this order of reliability:
"rolled a N.", "joined the game.", "chooses to play first.", "Turn N: <name>".
Sort known names longest first and match them as literal strings
(re.escape); never split on whitespace.
  - Spaces in names: [V-CODE] (S2 alter()); not in any real sample seen.
  - Periods in names: [V-CODE] (S2 alter()). A period inside a name ("Mr.
    Grim") breaks any "cut at the first period" rule, which S2 does.
  - Apostrophes in names: [UNVERIFIED]. Fixture rendered_chatter_names.txt
    uses "Dara O'Brien Jr" so the parser does not assume it cannot happen;
    note the possessive "Dara O'Brien Jr's [Card]".
  - Leading underscore and digits: [V-PD] ("_oneCurlyBoy", "jonsnow214").
  - Observers are names too: "PDBot has started watching.", "<name> has
    stopped watching." [V-PD]. Do not take players from these lines.
  - The "[League]" header line lowercases the names [V-PD]; do not take
    players from it.


5. Line catalogue
-----------------

Matching is done on the stripped line: no "[HH:MM:SS] " prefix, no "@P",
cards as bare names, no "@i", non-printables dropped. Python regex; {P} is
the alternation of known players as a named group "player"
(  (?P<player>Lotus\ Eater\ 5|Mr\.\ Grim)  ), {D} the same as group
"defender". Ordered: first match wins (order matters where noted). Number
words: a/an=1, two..seven; "a card" = 1.

kind                 regex (stripped line)                                  status
-------------------  -----------------------------------------------------  ------------
roll                 ^{P} rolled a (?P<amount>\d+)\.$                       V-DAT V-PD
join                 ^{P} joined the game\.$                                V-DAT V-PD
play_first           ^{P} chooses to (?:not )?play first\.$                 V-DAT V-PD
                     ("not play first" is V-CODE only)
mulligan             ^{P} mulligans to (?P<amount>\w+) cards?\.$            V-DAT V-PD
keep                 ^{P} (?:puts (?:a|\w+) cards? on the bottom of their
                     library and )?begins the game with (?P<amount>\w+)
                     cards? in hand\.$                                      V-DAT V-PD
                     (there is no separate "keeps" line: this is the keep)
turn                 ^Turn (?P<turn>\d+): {P}$                              V-DAT V-PD
                     (no period; turn numbers count per player: "Turn 1:
                     A", "Turn 1: B", "Turn 2: A", ... and restart at 1 in
                     each game of a match)
skip_draw            ^{P} skips their draw step\.$                          V-DAT V-PD
draw                 ^{P} draws (?P<amount>a|\w+) cards?(?: with
                     (?P<card>.+?)(?: token)?(?:'s ability)?)?\.$           V-DAT V-PD
                     (plain "draws a card." is the draw-step draw, 2025+
                     only; "with X" / "with X's ability" are effect draws)
land                 ^{P} plays (?P<card>.+)\.$                             V-DAT V-PD
cast                 ^{P} casts (?P<card>CARD)<suffix>                      V-DAT V-PD
                     card = first card span. Optional parts after it, in
                     this order, all seen: " using an alternate cost",
                     " for its <spectacle|escape|overload|warp> cost",
                     " from the graveyard", " with Flashback {3U} from the
                     graveyard", " without paying its mana cost[ with X]",
                     " with kicker", " with Bestow", " choosing \"<mode>\"[,
                     \"<mode>\"...]", " targeting <targets>", " (text)",
                     ". (X is N)."
activate             ^{P} activates an ability of (?P<card>CARD)
                     (?: token)?.*$                                         V-DAT V-PD
                     targets: 2021 "of C targeting T (text)."; 2025+ "of C
                     (text) targeting T." or "of C (text)" with no period
trigger              ^{P} puts (?:a )?triggered ability from (?P<card>.+?)
                     onto the stack.*$                                      V-DAT V-PD
                     targets: 2021 "onto the stack targeting T (text).";
                     2025+ "onto the stack (text) targeting T."; modal
                     triggers add ' choosing "<mode>"' (see section 7)
attack               ^{D}(?: ?'s (?P<card>.+?))? is being attacked by
                     (?P<attackers>.+?)\.?$                                 V-DAT V-PD
                     defender = the player (or that player's planeswalker
                     in group card); attackers = card list. The attacking
                     player is the active player (last turn header). One
                     line per combat, listing all attackers.
block                ^(?P<card>.+?)(?: token)? blocks (?P<attacker>.+?)
                     (?: token)?\.$                                         V-DAT V-PD
                     no player in the line: the blocker belongs to the
                     non-active player. One line per blocker.
damage_prevented     ^(?P<amount>\d+) damage caused by (?P<card>.+?) to
                     (?P<target>.+?) is prevented\.$                        V-PD
                     the ONLY damage line seen (see section 6)
discard              ^{P} discards (?P<card>.+?)\.$                         V-DAT V-PD
put_onto_battlefield ^{P} puts (?P<card>.+?) onto the battlefield
                     (?: attacking (?P<target>.+?))?\.?$                    V-PD
                     ("onto the battlefield attacking P" has no period)
token                ^{P} ?'s (?P<card>.+?) creates (?P<amount>a|an|\w+)
                     (?P<token>.+?) [Tt]okens?(?: under .+? control)?\.$    V-DAT V-PD
                     also seen, NOT matched by this: "creates a Wicked
                     attached to [C]." and "creates a Venerated Rotpriest."
                     (copies), map those as Unmapped
sacrifice            ^{P} sacrifices (?P<card>.+?)(?: token)?\.$            V-DAT V-PD
to_graveyard         ^{P} puts (?P<cards>.+?) into their graveyard\.$       V-PD
                     (a choice effect putting cards there, e.g. Dig Through
                     Time; NOT a death)
mill                 ^{P} mills (?P<cards>.+?)\.$                           V-PD
exile                ^{P} exiles (?P<cards>.+?) with (?:with )?
                     (?P<card>.+?)(?:'s ability)?\.$                        V-DAT V-PD
                     "with with" is a real client typo (delve and similar);
                     "exiles C with its own ability." also seen
bounce               ^{P} returns (?P<cards>.+?) to (?:its|their) owner's
                     hand with (?P<card>.+?)(?:'s ability)?\.$              V-DAT V-PD
counter_spell        ^{P} counters (?P<target>.+?) with (?P<card>.+?)\.$    V-PD
                     (the source can be a bare name without brackets:
                     "counters [Frantic Search] with Malevolent Hermit.")
fizzle               ^{P} ?'s (?P<card>.+?) is removed from the stack
                     because it has no legal targets\.$                     V-DAT V-PD
                     (card can be "Triggered ability from X", bare)
counters_added       ^{P} puts (?P<amount>a|an|\w+) (?P<ctype>.+?)
                     counters? on (?P<card>.+?)\.$                          V-PD
                     ctype: +1/+1, loyalty, lore, page, charge, poison...;
                     the object can be a player ("puts a poison counter on
                     <player>."). Must come AFTER put_onto_battlefield and
                     to_graveyard in the table.
counters_removed     ^{P} removes (?P<amount>a|an|\w+) (?P<ctype>.+?)
                     counters? from (?P<card>.+?)\.$                        V-PD
reveal               ^{P} reveals .*\.$                                     V-DAT V-PD
                     ("reveals their hand to C, containing ..." and
                     "reveals C [with D]." - hidden-zone info)
concede              ^{P} has conceded from the game\.$                     V-DAT V-PD
lose                 ^{P} loses the game\.$                                 V-PD
                     ^{P} has lost the game\.$                              V-CODE
                     ^{P} has lost the game due to disconnection\.$         V-CODE
                     ^{P} loses because of drawing a card\.$                V-CODE
win                  ^{P} wins the game\.$                                  V-PD (V-CODE
                                                                            for .dat)

The table above, as a Python prototype in a scratchpad, classified the
real samples it was built from as: S1 723 lines, 21 unmapped (all
"chooses a card from their hand.", "chooses Giant for C.", "puts Forest in
their hand.", "transmutes C.", "draws their next card."); S3 exports 2318
lines, 393 unmapped (mostly non-game lines of section 4b, "applies
replacement effect from C.", scry, cycle, choices).


6. Requested kinds with NO line in MTGO logs
-------------------------------------------

The brief asks for damage, life change and dies/graveyard. Across ~3100
real lines (S1 + S3) MTGO logs NONE of these:
  - combat damage and damage from spells/abilities: not logged. The only
    damage line is damage_prevented. "casts Lightning Bolt targeting P" is
    logged; the 3 damage is not.
  - life totals, life gain, life loss, life paid as a cost: not logged.
    (dat_combat_game.txt: Thragtusk's 5 life and Adanto Vanguard's "Pay 4
    life" leave no line.)
  - creature deaths / "dies" / destroy: not logged. A death is visible
    only indirectly: the object later shows up with a new object id
    (graveyard target, "@[Llanowar Elves@:100410,628:@]" in
    dat_combat_game.txt after it died blocking with id 602), as the source
    of a "leaves the battlefield" / modular trigger, or not at all.
  - draw-step draws before the 2025-era client: not logged.
Do not add regexes for these kinds: a line that looks like one is not in
any sample, so it must end up Unmapped. Consequence for Task 17: board
reconstruction cannot know life totals (start at 20 and say so) or which
creatures died in combat or to damage; it must report this, not guess.


7. Lines that must become Unmapped
----------------------------------

Recommended reasons (Task 16 may word them differently):
  - chat [V-PD]: "[CHAT] <name>: <text>" (detect BEFORE stripping "[...]").
  - export header [V-PD]: "== Game 1 (<id>) ==", "[Time] <unix>",
    "[League] <deck> by <name> (<id>) vs ...", "Game 1 Completed.", and in
    /export/ files the first lines "PennyDreadful", "PD League", observer
    list, player list, "Winner: <name>", "Match Winner: <name>: 2-0".
  - observer [V-PD]: "<name> has started watching.", "<name> has stopped
    watching."
  - connection [V-PD][V-CODE]: "<name> has lost connection to the game.",
    "<name> has left the game."
  - continuation [V-PD]: a single entry can span several physical lines
    when a modal ability lists its modes; continuation lines start with a
    space (" ** <mode text>" or " ...) choosing \"...\" targeting [C].")
    and the targets are on the LAST continuation line. Recommendation: fold
    continuation lines into the entry above for matching (so the trigger
    gets its target) and report each continuation line as Unmapped with
    reason "continuation of line N". Real logs keep a trailing double
    space on the first line ("choose one  "); the fixture drops trailing
    spaces.
  - game-system lines [V-PD]: "Illegal attack.", "Chosen mode: <text>.",
    "Couldn't put Triggered Ability from [C] on the stack."
  - pre-game/between games [V-DAT]: "<name> draws their next card."
    (meaning unclear: S2 skips it as "looking at extra cards").
  - choices and hidden-zone moves [V-DAT][V-PD]: "chooses a card from their
    hand.", "chooses Giant for C.", "chooses to use C's ability.", "names X
    for C.", "puts Forest in their hand.", "puts N cards on top of their
    library.", "puts C on bottom of their library.", "scrys N (N top, N
    bottom).", "cycles C.", "transmutes C.", "investigates.", "applies
    replacement effect from C.", "changes targets of C to T.", "is
    protecting <name>'s C.", "C transforms into D.", "C stops blocking.",
    "C becomes plotted."
  - multiplayer [V-CODE]: "This is a @iMultiplayer Commander@i game." (S2
    refuses such logs).


8. Fixtures
-----------

All hand-written, plain ASCII, LF line endings, one log entry per line
(except the multi-line entry in rendered_chatter_names.txt). Unmapped
lines below are by the section 7 rules; line numbers are 1-based physical
lines.

dat_short_game.txt (30 lines, .dat decoded form 2a, 2021 wording)
  Players river_kite, stone_owl. One game, 3 turns each side at most:
  roll, @P@P join, play first, mulligan to six + bottom-one keep, skip
  draw, land, cast, cast targeting a player, cast targeting a creature,
  discard + reveal hand (Thoughtseize), prowess trigger with @i markup,
  attack, concede. Unmapped: none.

dat_combat_game.txt (51 lines, form 2a, 2021 wording)
  Players Brightwater, kessig_wolf. Combat: single and double attacks,
  three blocks (one per line), activated abilities with and without
  targets, token creation ("creates a Clue token", "creates a Beast
  token"), token ability ("activates an ability of @[Clue@:..:@] token",
  "draws a card with @[Clue@:..:@] token's ability"), exile-type removal
  (Swords to Plowshares, Path to Exile) logged only as a targeted cast.
  Unlogged facts on purpose: Llanowar Elves dies blocking on turn 3 (only
  its new object id 628 on turn 4 shows it), Thragtusk's life gain,
  Adanto Vanguard's life payment, all combat damage. Last turn: Turn 5
  Brightwater. Unmapped: none.

dat_two_games_spaced_names.txt (57 lines, form 2a, 2021 wording)
  Players "Mr. Grim" (period + space) and "Lotus Eater 5" (spaces, digit).
  Two games in one match: game 1 ends in a concede, then "draws their next
  card.", @P@P joins, a second play-first; turn numbers restart at 1.
  Covers: Duress/Thoughtseize reveals, Liliana activation targeting a
  player (2021 order), sacrifice, planeswalker attacked with the 2021 space
  ("Lotus Eater 5 's @[Liliana of the Veil..]"), mulligan to five +
  bottom-two keep, trigger targeting a player (2021 order), cast with a
  target list "targeting Mr. Grim, and @[Pelt Collector..] (text)".
  Unmapped: line 26 "draws their next card."

rendered_chatter_names.txt (90 lines, rendered form 2b, 2025-26 wording)
  Players "Old Man Hemlock" (spaces) and "Dara O'Brien Jr" (apostrophe,
  [UNVERIFIED] as a real MTGO name). Chatter, observers and export headers
  mixed into a full game; draw-step "draws a card." lines; X spell ".
  (X is 2)."; counter_spell; damage_prevented; counters_added and
  counters_removed; mill; delve exile with the real "with with" typo;
  bounce of a creature and of a token; fizzle with possessive apostrophe
  name; put_onto_battlefield attacking (no period); to_graveyard; modal
  trigger spanning 3 lines with its target on the last line; win, lose
  and concede together at the end.
  Unmapped (15 lines):
    1  "== Game 1 (912345678) =="                    export header
    2  "[Time] 1794434258"                           export header
    3  "PDBot has started watching."                 observer
    5  "[League] ..."                                export header
    6  "[CHAT] PDBot: [sD] Good luck ..."            chat
    14 "[CHAT] Old Man Hemlock: gl hf"               chat
    23 "Old Man Hemlock scrys 1 (0 top, 1 bottom)."  no mapping
    30 "[CHAT] Dara O'Brien Jr: rude"                chat
    52 "Illegal attack."                             game-system line
    56 "[CHAT] Dara O'Brien Jr: missclicked"         chat
    74 "Spectator Sam has started watching."         observer
    82 " ** Create a 1/1 white Bird ..."             continuation of 81
    83 " ...) choosing ... targeting [Goblin Token]." continuation of 81
    89 "... has lost connection to the game."        connection
    90 "Game 1 Completed."                           export header

Kind coverage across the fixtures: every kind of section 5 occurs at least
once (mulligan and reveal only in the dat_* files; damage_prevented,
counter_spell, counters_added/removed, mill, exile, bounce, fizzle,
put_onto_battlefield, to_graveyard, lose and win only in
rendered_chatter_names.txt).
