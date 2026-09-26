# Play script format - design D2

Date: 2026-09-13. Repo: magic-cards-model-api. The script format section of
the play-script DSL design (D2, with the D6 schema bounds), kept in the repo
next to the code that enforces it. The model is
`src/magic_cards_model_api/playscripts/schema.py`; readers outside Python use
the exported `contract/playscript.schema.json` (`mise run playscript:export`).
`tests/test_playscript_format_doc.py` pins this doc's closed sets to the model.

## Format

- A script is declarative JSON at `decks/<slug>/plays/<id>.json`, checked by a
  pydantic model with `extra="forbid"` and `strict=True`, every union
  discriminated on `kind`. An optional field is left out, never `null`.
- Top level: `schema_version` (1), `id` (equals the file stem), `name`,
  `intent?` (prose, ignored by machines), `note?` (prose too, but for a
  different reader and a different bound - see `note` below), `priority`
  (0-100, higher tried first, 0 = disabled - at most 6 plays with priority > 0
  may be *live together* per archetype key, O8: the largest subset of the
  plays applying to that key no two of which are provably exclusive by
  `whose_turn`, `steps` or turn range, not the plain count that apply, so two
  groups a deck's own turn guards already prove disjoint are never summed;
  an advice-only play is never counted here - see "Advice-only plays" below),
  `opposing_archetypes` (1-32: `["*"]` alone, or matchup
  label slugs; `*` beside a key is `wildcard-with-keys`),
  `when {whose_turn: mine|theirs|either, steps: [1-4 distinct steps],
  stack: empty|any}` (a repeated step is `duplicate-step`; FB2 W1 - the two
  combat declaration steps are now namable here, one board-position
  precondition among the rest, distinct from actually attacking or blocking,
  which still needs the M2/M3 move kinds below), `true` (0-8 conditions),
  `false` (0-8 conditions, each must NOT hold), `moves` (0-4 - empty or
  absent is advice-only, see "Advice-only plays" below), `next` (0-3 ids).
  `when.steps` names every step a `pass_until` run can stop at (the autopass
  steps: `upkeep, draw, precombat_main, beginning_of_combat,
  declare_attackers, declare_blockers, first_strike_damage, combat_damage,
  end_of_combat, postcombat_main, end_step`) plus three FD2 schema-local
  additions naming a step in which a decision can be pending even though no
  run can ever stop there - `cleanup`, `mulligan`, `pregame_actions` - see
  "Schema-local steps" below. CR 510.4/506.1's first-strike combat damage
  step is `first_strike_damage`, distinct from `combat_damage` itself (the
  engine's own turn-based action, and the state view's `step` field, name
  both occurrences `combat_damage`; a script naming `first_strike_damage`
  fires only in the first one, one naming `combat_damage` only in the second
  - or the only one, in a combat with no first or double striker).
- Changing `priority` after a game goes through `mcp-seat plays priority`,
  and `plays admit` records each change as a `Script-Priority` trailer.
  Change a script's priority only for scripts that actually ran this game,
  with recorded exits, as shown in the run table. Never lower a script for
  `not_applicable`, or for having had no applicable window. Change a script's
  priority by at most 5 per game. The command refuses a larger step, per call
  and per game (`--game-id` or `MTG_GAME_ID`), with `priority_limit`.
- Selector (all fields optional, combined with AND), evaluated on the face a
  card has in its current zone: `types_all`, `types_any`, `subtypes_any`,
  `supertypes_any`, `colors_any`, `keywords_any` (each 0-15 words);
  `mana_value {op, n}`; `power {op, n}`; `toughness {op, n}`; `tapped`;
  `counters {name, op, n}`; `ability_text_has`; `oracle_has`;
  `searches_for_any` (0-5 basic land type words); `land_face`;
  `missing_from {zones: [1-7 zone], side: me|opponent, by: subtype|name}`;
  `enters_any` (0-3 of `untapped`/`tapped`/`untapped_if_paid`); `payable_any`
  (0-4 of `now`/`with_a_land`/`next_turn`/`no`); `unlocks`; `unlocks_next`;
  `unlocks_by_search`; `unlocks_by_basic_search`; `affordable {extra: n}`
  (DEPRECATED - see below).
- `colors_any` reads the card's `colors` by name or letter, any case, from a
  closed vocabulary: the five colors and `colorless`. `colorless` (or `C`)
  matches a card whose color set is empty (CR 105.2c, "A colorless object has
  no color"), a devoid Eldrazi included, and never a colored card.
- Colors: `white`, `blue`, `black`, `red`, `green`, `colorless`
- Condition kinds (12): `count`, `zone_size`, `life`, `stack_top`, `land_drop_available`, `untapped_mana`, `mana_pool`, `turn`, `input`, `pending_decision`, `compare`, `all_of`
  (an `in`/`not_in` value list holds 0-32 values; `pending_decision`, `compare`
  and `all_of` are FB2 additions, `mana_pool` is an FD9 addition - see their
  own subsections below).

### `note`

`note?` is prose too, like `name` and `intent`, but it is written for a
different reader and so carries a bound `intent` does not. `intent` is
written for a human reading a diff of the deck's own plays repo: free-form,
unbounded, and long in practice - the corpus's own `intent` strings measure a
1443-character median and a 3110-character max at the time of writing.
`note` is written for the agent playing the game, mid-game: the moment a
play's own hint surfaces it, the agent is mid-decision and has to read it in
passing, not study it, so it is bounded at `MAX_NOTE_LENGTH` (240) characters
- room for one sentence of reasoning for why this play is a good choice right
now, never the deck-design essay `intent` is free to be.

`note` joins `PROSE_KEYS` (`name`, `intent`, `note`) and so it may name cards
- the top-level-only `card-name-in-field` exemption exists for exactly this
reason: a `note` that could not say "answers their Tarmogoyf" or "holds up
Fatal Push" would fail at the one job it has, which is explaining a decision
to whoever, or whatever, reads it next.

### `untapped_mana`

`{kind: untapped_mana, op, n}` compares `n` with the most mana the viewer can
make right now from untapped sources, not the number of sources:

- The sources are the untapped permanents the viewer controls with a mana
  ability (`seatkit.untapped_mana_sources`). Each is activated at most once,
  for one of its mana abilities.
- An ability adds its card view's `abilities[].mana_amount`: the engine's
  board-resolved count, so Urza's Tower beside a Mine and a Power Plant adds 3
  (CR 605.1a's "add X instead"). It is 0 when the ability cannot be activated
  now (CR 602.5, a summoning-sick creature under CR 302.6) or its mana type is
  undefined (CR 106.5).
- Restricted mana is not counted. An ability whose card view carries
  `abilities[].restriction` (the engine's `machines.cost.spend_restriction`
  over the ability's own effect text: CR 106.6's mana whose spending is
  restricted, "Spend this mana only to ...", understood or not) adds nothing,
  and a restricted filter is never activated. The condition names no spell, so
  the only mana a play's move can surely spend is mana spendable on anything;
  counting restricted mana only when the move could spend it would make the
  answer depend on which spell or ability a move resolves to at run time,
  which a static check cannot see. Eldrazi Temple still counts its plain
  `{T}: Add {C}` (1), never its `{C}{C}`; Tablet of Discovery counts its
  `{R}`, never its instant-and-sorcery `{R}{R}`. Card names play no part: the
  restriction is read off the ability, so every card printing the template is
  judged alike.
- A filter (`abilities[].mana_cost_value` above 0: `{1}, {T}` or `{W/U}, {T}`)
  consumes that much of the mana already made. It is activated only when the
  pool holds its cost, filters in ascending cost order, so it counts
  `mana_amount - mana_cost_value` and is never double-counted. Its cost's colors
  are not judged.
- The count is the largest pool over every choice of one ability, or none, per
  source. Tron assembled is 7; with Eldrazi Temple 8; Eldrazi Temple and an
  Island are 2; an Island and Mystic Gate
  are 2; Captivating Cave and an Island are 2.

### `mana_pool` (FD9)

`{kind: mana_pool, color?, op, n}` compares `n` with the viewer's own
floating mana pool (`ManaPoolView.floating`, CR 106.4) - the mana already
produced by mana abilities and not yet spent, never re-derived. `color`
narrows to one coloured symbol's own count (the same `W`/`U`/`B`/`R`/`G`/`C`
vocabulary `activate.color` uses); left out, `n` compares the total across
every color. This is the condition the owner's own example asks for
("DSL is meant to fix missing repeat-until loops"): `untapped_mana` counts
untapped SOURCES, never the pool itself, so it cannot express "keep looping
until my floating pool holds X" at all - a Devoted Druid + Vizier of
Remedies mana loop (finding seq 191, roughly 25 manual MCP calls to reach
X=8) has no other way to say its own stop condition. It is also a `compare`
operand (`{of: mana_pool, side, color?}`, below) on the same footing
`untapped_mana`'s own operand is on.

### `turn`

`{kind: turn, op, n}` compares `n` with the viewer's own `own_turns_taken` -
the engine's own count of how many of their own turns they have begun
(`TurnMachine.own_turns_taken`, CR 500.7 extra turns included). It is my Nth
turn on the play and on the draw alike, and an extra turn is its own turn
and moves this - unlike `TurnMachine.turn_number` (rounds of the table,
`machines/turn.py`), which does not advance until the rotation wraps back to
the starting seat and so cannot tell a normal turn from an extra one spliced
into it. `own_turns_taken` is 0 before the viewer's first turn, pregame
included - never null. It is a `numeric_range` key for never-applicable lint
and conflict exclusivity, on the same footing as `count`, `zone_size`, `life`
and `untapped_mana`.

### `pending_decision` (FB2 C1)

`{kind: pending_decision, decision: <DECISION_KINDS value>}` holds when a
decision of that kind is pending for the viewer right now - the same closed
`DECISION_KINDS` vocabulary an answer's own `decision` names, `declare_attackers`/
`declare_blockers`/`assign_combat_damage`/`mulligan` included, since this is
a read of what is pending, not an answer to it. It is the gate for the
standalone pending-decision answer (D2 below): a play whose only move is
`answer` and whose `true` names a `pending_decision` may run the moment that
decision is on the table, with no priority of its own required - the answer's
own `decision` must equal the condition's, or `answer-decision-mismatch`.

### `compare` (FB2 C2)

`{kind: compare, left: Operand, op, right: Operand}` compares two board- or
literal-read values against each other - `count`/`zone_size`/`life`/
`untapped_mana` each already compare one board read against a literal `n` on
their own; `compare` puts a board read on both sides, so "my board's summed
power beats their life total" is expressible without a bespoke condition kind
of its own. `Operand` is discriminated on `of`:

- `{of: count, select, zone?: <COUNT_ZONES>, side}` - `zone` left out
  defaults to `battlefield`. The same hidden-zone side rule `count` the
  condition applies: a hidden `zone` must name `side: me` or `side: opponent`,
  never `any` (`hidden-zone-side-any`).
- `{of: zone_size, zone: <SIZE_ZONES>, side: me|opponent}` - `side` is always
  named here, never `any`.
- `{of: life, side: me|opponent}`
- `{of: untapped_mana, side: me|opponent}`
- `{of: mana_pool, side: me|opponent, color?}` (FD9) - the same read
  `mana_pool` gives, on a named `side`.
- `{of: power_sum, select, side}` - the summed power of `side`'s battlefield
  permanents matching `select`.
- `{of: toughness_sum, select, side}` - the same reading `power_sum` gives,
  for toughness.
- `{of: n, n: <int>}` - a plain literal, read off nothing.

- Operand kinds: `count`, `zone_size`, `life`, `untapped_mana`, `mana_pool`, `power_sum`, `toughness_sum`, `n`

### `all_of` (FB2 C3)

`{kind: all_of, conditions: [2-4 leaf Condition]}` holds only when every
member holds - "not (A and B ...)" when it sits in `false`, which is the only
place it is allowed: `all_of` inside `true` is `all-of-outside-false`. A
member is drawn from every condition kind except `all_of` itself - nesting is
refused, since an `all_of` naming `all_of` among its own `conditions` is an
unrecognized kind for that inner list.

### Schema-local steps (FD2)

`STEPS` (`when.steps`' own vocabulary) extends `seatkit.AUTOPASS_STEPS` - the
steps a `pass_until` run may be told to stop at - with three additions that
live in the schema alone, never in `seatkit`:

- `cleanup` (CR 514.2/514.3): no player receives priority in an ordinary
  cleanup step, but one is granted whenever a discard-to-hand-size happens or
  a triggered ability triggers during it - a decision can be pending there.
- `mulligan` (CR 103.5): the pregame step CR 103.5's declarations are made in.
  A `mulligan` move (see "Moves" below) is valid only when `when.steps` is
  exactly `["mulligan"]`.
- `pregame_actions` (CR 103.6): the pregame step CR 103.6's opening-hand
  actions are taken in.

Neither pregame step, nor `cleanup`, is ever a destination `pass_until` can
stop at (CR 502.4/514.3 grant no player priority in an ordinary untap or
cleanup step by default, and neither pregame step is in the engine's own
`TURN_ORDER` at all) - `STEPS` names them anyway because `when.steps` is
about scoping a play to a board position, not about where a run can stop.

### `searches_for_any`

A selector field naming 0-5 of the five basic land types (`Plains`, `Island`,
`Swamp`, `Mountain`, `Forest`; a word outside them is `malformed-play`).
Parsed from a "search your library for a/an T (or T) card" template found in
either the candidate's `ability_text_has`-style ability text or its
`oracle_has`-style oracle text, matched case-insensitively; the candidate
matches when the template names at least one of the listed types. A
fetchland's printed oracle text ("Search your library for an Island or Swamp
card") and a landcycling card's activated ability (CR 702.29e, synthesized by
the engine even in hand - see `abilities` below) both carry the template. A
word the template names that is not one of the five (Expedition Map's "a land
card") contributes nothing, so a generic land search never matches.

- Basic land types: `Plains`, `Island`, `Swamp`, `Mountain`, `Forest`

### `land_face`

A boolean selector field: true when a **modal double-faced card** (CR
712.12) carries a face marked `land_face` (`faces`, present regardless of
its current zone or chosen face - `serialize._faces_view`, which marks only
the one face the engine's own `Card.land_face` names). A creature-fronted
MDFC land (Witch Enchanter // Witch-Blessed Meadow, Jwari Disruption //
Jwari Ruins) matches it in hand even though its up face is a Creature or an
Instant, so a plain `types_all: [Land]` still does not match it - that reads
only the up face, CR 712.8a. A transform or meld card also carries `faces`
(the UI shows both) but never a `land_face` mark: its land back (Treasure
Map // Treasure Cove) is reached only by transforming or melding (CR
712.2), never a land drop, so it never matches even though one of its faces
plainly types as a Land. A single-faced card has no `faces`, so its own
type line is read instead: a plain land matches `land_face: true` too.
`play_land` plays whichever face is a land by the card's own `card_id` -
there is no separate face id in the request, because CR 712.12 is the same
land drop either way and the engine resolves the face from the id
(`Card.land_face`).

### `missing_from`

A selector field for the "fetch a piece you are missing" shape. A candidate
matches only when no card on `side` in `zones` shares the compared
characteristic with it:

- `by: subtype` compares the candidate's subtypes that the selector's own
  `subtypes_any` also lists, or all its subtypes when `subtypes_any` is left
  out. No card in the zones may have any of them. `{subtypes_any: [Tower,
  Mine, Power-Plant], missing_from: {zones: [battlefield, hand], side: me, by:
  subtype}}` over a library with all three, beside a battlefield Tower and a
  Mine in hand, matches only the Power-Plant; without `subtypes_any` it
  matches nothing, because every Tron land also has the subtype `Urza's`.
- `by: name` compares the card views' `name`s. It is a comparison between
  objects: the script names no card, so the card-name rule holds.
- A candidate with nothing to compare (no listed subtype, no name) never
  matches. The candidate itself counts when it sits in one of the zones, so
  name the zones it is not chosen from.
- Zones are read exactly as a `count` reads them: battlefield and stack by
  controller, graveyard and exile by owner. Hidden zones are read from
  known information only. The opponent's hand, library and sideboard are only the
  cards the viewer knows (`known_cards`); a card the viewer does not know
  about is treated as absent, so over the opponent's hidden zones "missing"
  means "not known to be there". The viewer's own library or sideboard is its
  contents multiset, fetched with `include=contents`, which the runner asks
  for whenever a condition or move reads it.
- It works wherever a selector does: a `count` or `stack_top` condition, a
  move's card, source, target, sacrifice choice or discard choice, and an
  answer's `pick` and its `prefer`. `zone_size` takes no selector.
- A zone outside the set, a side other than `me`/`opponent`, or a `by` outside
  the set is `malformed-play`; no zones is `bounds-exceeded`.
- The play-conflict check reads `missing_from` as part of the condition it is
  in, never as a dimension: two counts over one identical selector (zones
  compared as a set) can be exclusive by range (`>= 1` against `== 0`), but
  selectors that differ only in `missing_from` are different keys and prove
  nothing.

- Missing-from zones: `battlefield`, `graveyard`, `exile`, `hand`, `stack`, `library`, `sideboard`
- Missing-from sides: `me`, `opponent`
- Missing-from by: `subtype`, `name`

### `enters_any` (O4)

A selector field naming 0-3 of the three `EntersState` words (`untapped`,
`tapped`, `untapped_if_paid`; a word outside them is `malformed-play`),
answered by the engine's `machines.hypothetical.predict_enters` for the
viewer's own hand or library land right now - never re-derived from the
view's own text. The redacted view carries no `enters` field of its own:
`selectors.matches` reads it off `hypo`, a mapping of engine-computed facts
keyed by card id (`hypothetical_facts.build_hypothetical_facts`, served by
`GET .../playscripts/hypothetical-facts`), fetched only when a play's
selectors actually name `enters_any`, `unlocks` or `unlocks_by_search`
(`conditions.needs_hypothetical`) and cached per board position rather than
asked again for the same one. A land whose optional cost (a shockland's "you
may pay 2 life") is legal but lethal (CR 704.5a) is read as `tapped`, never
`untapped_if_paid`: that payment is not one a player can spend on anything,
so a shockland at 2 life or less never matches `enters_any: [untapped_if_paid]`.
A card with no computed fact (an opponent's card, a card not asked about at
all) never matches, the same as any other absent characteristic; asking for
`enters_any` with no `hypo` fetched at all is `HypotheticalRequired`, not a
silent false.

- Enters states: `untapped`, `tapped`, `untapped_if_paid`

### `unlocks`, `unlocks_by_search`, `unlocks_by_basic_search`, `affordable` (O7)

Four more selector fields the engine, not the view, answers:

- `unlocks` (bool): with this land entering right now exactly as `enters_any`
  above says it would (its optional cost paid only when doing so is not
  lethal), some hand card other than the land itself becomes payable that was
  not payable before it entered - a cast, or a hand-activated ability such as
  cycling or channel. Judged by one `machines.hypothetical.payability_matrix`
  call per evaluation (never one `payability` call per land/card pair): the
  matrix's `now` answers are the baseline, its per-land column the answer
  with that land assumed, and a pair is compared only when neither answer,
  nor the land's own prediction, carries an engine notice (an unresolved
  triggered ability, an unread enters-tapped line) - a noticed pair fails
  `unlocks` closed to `false` rather than trusting an answer the engine
  itself flagged incomplete. A lethal optional payment is judged as the
  declined (tapped) entry, so it never counts as unlocking anything either.
  A hand card whose printed mana cost carries an unannounced `{X}` (CR
  107.3) is excluded from this comparison entirely - `payability_matrix`
  only ever judges an X spell at its cheapest legal announcement, X=0 (CR
  107.1b), and a land that merely makes an X=0 cast payable (Kozilek's
  Command for no modes worth taking) is not a real unlock. The same
  exclusion applies transitively to `unlocks_by_search`/
  `unlocks_by_basic_search` below, since both read this same `unlocks`
  answer for the land a search could find.

  **Newly payable is not enough on its own: it must also be targetable.**
  `legal_actions` still offers a cast row for a spell with a mandatory
  target and none legal right now (Fatal Push, "target creature", with no
  creature anywhere; Ephemerate, "target creature you control", with no
  creature controlled) - it just leaves that row's `target_slots[i].
  candidates`/`player_candidates` empty, so `Payability.payable` alone
  cannot tell a real unlock from a merely mana-payable, untargetable one. A
  card newly payable on a land's column counts toward `unlocks` only when at
  least one of its payable (`Affordability.YES`) options also has a legal
  candidate for every mandatory target slot (`TargetSlot.minimum >= 1`) - the
  same read item 3's `castable_now` already applies to `affordable`, applied
  here to the specific option that made the card payable. A payable option
  with no target slot at all, or only optional ones, passes automatically,
  so a targetless card (Preordain, Psychic Frog) still unlocks exactly as
  before. This applies to `unlocks`, and transitively to `unlocks_by_search`/
  `unlocks_by_basic_search` (both read the same `unlocks` answer for the land
  a search could find) and to `unlocks_life_paid` (never set except alongside
  a genuine `unlocks: true`), and to a `candidate_ids`-offered library land's
  own `unlocks` entry - all of it computed off the one matrix pass, no second
  engine call.

  **Item 4, `unlocks_life_paid` (int, present alongside `unlocks` for every
  offered land):** the life the unlock costs through this land's own
  "{T}, Pay N life" mana ability (CR 605.3a, e.g. Fiery Islet's "{T}, Pay 1
  life: Add {U} or {R}."), 0 when the unlock needed no life at all. A
  life-paid mana activation the engine judges payable always carries its own
  notice on the `Payability` (`"...but they may (CR 605.3a)"`, from
  `machines.hypothetical._with_life_payments`) - the general
  any-notice-fails-closed rule above would otherwise mark every life-paid
  unlock as unreadable, which is wrong: the engine already refuses to count
  a life payment that would be lethal (CR 704.5a - that shape of notice
  never carries the CR 605.3a citation, so it still fails closed as before),
  so a notice carrying *only* the CR 605.3a citation is excepted from the
  fail-closed rule instead of parsed into a fresh engine call, and its life
  cost is read straight off `Payability.life_paid`. D1/D3's privacy and
  fail-closed rules are otherwise unchanged: nothing new is exposed beyond a
  plain integer already implied by `unlocks: true`, and any other notice on
  the pair (an unresolved trigger, an untried CR 118.3c source) still fails
  `unlocks`/`unlocks_life_paid` closed exactly as before.
- `unlocks_by_search` (bool): this hand card's ability or oracle text matches
  O3's `searches_for_any` template, and at least one land its search could
  find, among the viewer's own library (by basic land type, read off the
  library card's own subtypes, never a name), itself `unlocks` something in
  that same matrix call. It needs the viewer's own library contents -
  `conditions.needs_contents` fires for any selector naming it, exactly as it
  does for a `count` over the viewer's own library, so a caller that walks a
  play for what it needs to fetch never has to ask twice.
- `unlocks_by_basic_search` (bool): the same question as `unlocks_by_search`,
  narrowed to a library land its search could find that is also a *basic*
  land (supertype `Basic`, read off the library card the same way
  `unlocks_by_search` already reads its subtypes) - a fetch-conservation play
  that only wants a "free" find (never a shockland or other non-basic land
  that also unlocks something) names this instead. It needs the viewer's own
  library contents exactly as `unlocks_by_search` does, for the same reason.
- `affordable {extra: n}` (`n` >= 0): the card's own `mana_value` is at most
  `untapped_mana + extra` - the same `untapped_mana` a `{kind: untapped_mana}`
  condition reads (`conditions.untapped_mana`), a plain view computation with
  no engine call, computed once per `evaluate`/move-build call and passed to
  every selector that asks - *and* the card also reads `true` off
  `hypothetical_facts.py`'s `castable_now` for its id (item 3). `extra: 0`
  asks whether the card is affordable from untapped mana alone; a card with
  no `mana_value` (a land), or a call that never computed `untapped_mana` at
  all, never matches, mana_value aside.

  **DEPRECATED, replaced by `payable_any` (T5).** `affordable` is
  colour-blind: it compares the card's plain `mana_value` against a count of
  untapped mana sources and so reads a double-black two-drop as affordable
  off two white sources, since nothing about `untapped_mana` or `extra`
  carries a colour at all. `payable_any` asks the engine whether the cost can
  actually be paid, colours included, by running the real
  `payability_matrix` rather than comparing two integers. `affordable` is
  kept and still answered exactly as before - no script already written
  stops validating - but a new play should name `payable_any: [now]` in its
  place; see `payable_any`, `unlocks_next` below.

  **Item 3, why `castable_now` and not a mana_value comparison alone:**
  `mana_value <= untapped_mana + extra` alone was blind to targets -
  `legal_actions` still offers a cast row for a spell with a mandatory
  target and none legal right now (Fatal Push, "target creature", with no
  creature anywhere: a real row, just with an empty `target_slots[0].
  candidates`), so a 1-drop with no legal target used to read as affordable
  and suppress an idle play meant to hold up mana for it. The chosen fix:
  the engine has no "assume n extra untapped mana" hypothetical to ask for
  directly (`machines.hypothetical.payability`'s only hypothetical is
  `assuming_land`, one additional land - not a bare mana bump), so this
  never tries to simulate `extra` against the engine. Instead it takes the
  fallback the item itself named: `mana_value <= untapped_mana + extra` AND
  the card has a legal cast row *now*, targets ignored for mana ("a legal
  cast row now, ignoring mana"). `castable_now` is that second half,
  computed once per `build_hypothetical_facts` call for every hand card from
  the same real-board `payability_matrix(..., hand, lands).now` answer
  `unlocks` already computes (no second engine call): true when at least one
  of that card's `Payability.options` has every mandatory target slot
  (`TargetSlot.minimum >= 1`) filled with a candidate
  (`slot.candidates`/`slot.player_candidates`), false otherwise (including
  when the card was not judged at all - `hand`/`lands` empty).

`unlocks`/`unlocks_by_search`/`unlocks_by_basic_search`/`enters_any`/
`affordable` all need `hypo` now: a play using any of the five needs the
`GET .../playscripts/hypothetical-facts` call - `needs_hypothetical` reads
the same five-field list.

### `payable_any`, `unlocks_next` (T5)

Two more selector fields, answered only when the caller fetches the
next-turn horizon (see below): they extend `enters_any`/`unlocks` one step
further, from "what would this land do" to "how soon could this cost
actually be paid".

- `payable_any` names 0-4 words of a closed four-word vocabulary (a word
  outside it is `malformed-play`) - one hand card's answer to "how soon
  could this be paid for at all", **earliest wins**:

  - `"now"` - payable this instant. Gated on `_has_a_payable_targeted_option`:
    `Affordability.YES` on at least one of the card's own payable options,
    *and* every mandatory target slot of that same option already filled.
    Deliberately not the sibling `castable_now` reads for `affordable` -
    that one asks `legal_actions` whether a row with legal targets exists
    and never whether the mana is there, so a Fatal Push with the black
    mana up and no creature to target is `castable_now` and still not
    `"now"` here: the mana question and the target question are both asked,
    and both must say yes.
  - `"with_a_land"` - payable once some hand land enters, provided the land
    drop is still there to spend. **D-3, the land-drop caveat:**
    `PAYABLE_STATES`'s own definition of the word is "once the land drop
    still available this turn is made", so the fact is gated on
    `_land_drop_available` being true right now - CR 305.3 (a player can't
    play a land at all outside their own turn) and CR 305.2a's comparison
    of `Game.land_allowance` against `Game.lands_played` - rather than
    answering unconditionally whenever some hand land's own column would
    pay for the card. With the land drop already spent this turn, a card
    only a hand land could pay for reads `"no"`, never `"with_a_land"`: the
    word never claims a land drop the board cannot actually deliver. The
    gate reads only that durable half of the rule; CR 305.1's own timing (a
    main phase, an empty stack, priority in hand) is this instant's own
    condition, satisfied again later the same turn and never satisfied at
    all while a `candidate_ids` caller asks mid-search (CR 608.2d) - so
    reading it would collapse every `"with_a_land"` a fetch's own answer
    depends on. That half is named in `notices` instead of read.
  - `"next_turn"` - payable in the `assuming_untapped` pass's own answer:
    the same board with the untap step already taken, mana only, nothing
    else the turn would bring (no draw, no upkeep trigger, no reply from
    the opponent).
  - `"no"` - none of the three.

  A hand card whose printed mana cost carries an unannounced `{X}` (CR
  107.3) gets no `payable_any` key at all, not `"no"` - the same fail-closed
  rule `unlocks` already applies through `_has_x`: `payability_matrix` only
  ever judges an X spell at its cheapest legal announcement, X=0 (CR
  107.1b), and a "yes" there says nothing about the X a play actually
  wants, so omitting the key is the honest answer and `"no"` would be a
  false claim.

- `unlocks_next` (bool): `unlocks`' own before/after comparison, read off a
  second `payability_matrix` pass instead of the real one - the
  `assuming_untapped` matrix, the same board with the lands already there
  untapped and nothing else advanced. True when, in that land's own column
  of that pass, some hand card other than the land itself is payable that
  was not payable in that same pass's own baseline row - every fail-closed
  rule `unlocks` already applies (an X-cost card never counts, a pair
  carrying an engine notice is left out, the newly-payable option must have
  its mandatory targets filled) holds here unchanged, off the one shared
  read (`_newly_payable`) behind both fields. `unlocks_next` is a
  **mana hypothetical, not a turn prediction**: it says nothing about whether the
  turn will actually reach this state - no draw is simulated, no upkeep
  trigger considered, no opponent action modeled - only "if the lands
  already on the battlefield were untapped and nothing else changed, would
  this land's own entry still turn something on". Offered on exactly the
  lands `unlocks` is already offered on, never any other library card.

`payable_any` and `unlocks_next` both need the next-turn horizon: the
caller fetches `GET .../playscripts/hypothetical-facts` with
`horizon=next_turn`, which runs `payability_matrix` a second time
(`assuming_untapped=True`) and adds both facts off that second pass; the
same call without it answers byte for byte what it always did, and neither
field is present. `conditions.needs_next_turn` is what decides when to ask
for it, the same way `conditions.needs_hypothetical` decides when the plain
(no-horizon) fetch is needed at all: a play whose selectors name
`payable_any` or `unlocks_next` needs the horizon fetch; a play naming only
`enters_any`/`unlocks`/`unlocks_by_search`/`unlocks_by_basic_search`/
`affordable` needs the plain one; a caller walking a play for what to fetch
asks both rather than always requesting the more expensive one.

**D-6, a known limit of this vocabulary: no life-paid tier.** `payable_any`
does not distinguish a card payable only by activating a "{T}, Pay N life:
Add ..." mana ability the player chooses themselves from one paid for with
a plain tap - both read the same word. The life is not lost information:
it is `Payability.life_paid` on the very row that answered, the same field
`unlocks_life_paid` already surfaces for `unlocks` above. A future field
wanting the distinction reads it there rather than widening this
four-word vocabulary further.

- Payable-any words: `now`, `with_a_land`, `next_turn`, `no`

## Moves

Move kinds (10), each with `answers: [0-3]` (an `answer` move needs 1-3;
`pass`, `mulligan` and `repeat` have no `answers` field at all):

- `activate {source: {zone, select, prefer?: [0-4 select], ties?: any}, ability_text_has?: string, ability_index?: 0-15, targets: [0-6 <target>], sacrifice?: source|none|<sacrifice choice>, discard?: none|<discard choice>, exile?: <Pick>, x?: 0-30|max, color?: W|U|B|R|G|C, answers: [0-3]}` - names its ability by exactly one of `ability_text_has` (a phrase) or `ability_index` (FB2 M4, the card's own ability list index); both or neither is `activate-ability-ref`. FD2 adds `exile` (a Pick, the same footing `sacrifice`/`discard` are on), `x` (CR 107.3a's X, announced as the activation begins) and `color` (which coloured symbol a multi-colour mana ability covers).
- `cast {card: {zone, select, prefer?: [0-4 select], ties?: any}, cost?: <cast cost>, face?: <selector>, modes?: [1-5 {text_has}], x?: 0-30|max, shares?: [0-6 int], optional_costs?: [1-4 {text_has}], sacrifice?: <Pick>, discard?: <Pick>, exile?: <Pick>, tap?: <Pick>, convoke?: <Pick>, delve?: <Pick>, improvise?: <Pick>, targets: [0-6 <target>], mana?: auto, answers: [0-3]}` - FD2 adds every field after `cost`: `face` names a split/Adventure/MDFC spell face by type or text phrase, never a card name; `modes` (CR 700.2a) and `optional_costs` (CR 118.8) each name 1-5/1-4 choices by phrase; `x` is CR 107.3a's X; `shares` (CR 601.2d) divides a divided effect among `targets`, aligned by position; the cost-object `sacrifice`/`discard`/`exile`/`tap` Picks pay a printed additional cost naming one of those, and `convoke`/`delve`/`improvise` (CR 702.51a/702.66a/702.126a) are Picks too.
- `play_land {card: {select, prefer?: [0-4 select], ties?: any}, answers: [0-3]}`
- `answer {answers: [1-3]}`, for a decision already pending
- `pass {}` (FB2 M1, CR 117.3d) - release priority once. Must be the last move of `moves`, otherwise `pass-not-last`; a play whose only move is `pass` is a legal "do nothing here" play.
- `declare_attackers {attackers: [0-6 AttackGroup]}` (FB2 M2, CR 508.1), `AttackGroup {select, count?: >=1|all, prefer?: [0-4 select], ties?: any, defender?: {player: opponent}|{select, side: opponent}}` - `defender` left out means the opponent player; `attackers: []` declares no attackers.
- `declare_blockers {blocks: [0-6 Block]}` (FB2 M3, CR 509.1), `Block {blocker: {select, count?: >=0, prefer?: [0-4 select], ties?: any}, attacker: {select, prefer?: [0-4 select], ties?: any}}` - `blocks: []` declares no blocks.
- `keyword_action {action: suspend|plot|foretell|cycle|equip|crew|turn_face_up|companion, card: {zone, select, prefer?: [0-4 select], ties?: any}, target?: <CardTarget>, crew_with?: <Pick>, answers: [0-3]}` (FD2) - one shape for the six existing keyword-action routes and FD5's two new ones; `target` names the creature an `equip` attaches to, `crew_with` the creatures a `crew` action taps to pay its cost.
- `mulligan {keep: bool, bottom?: <Pick>}` (FD2, CR 103.5) - `keep` false takes a mulligan; a seat that keeps after already mulliganing owes CR 103.5's bottoming, named by `bottom` (left out, the engine's own default). Valid only when `when.steps` is exactly `["mulligan"]`, otherwise `mulligan-wrong-step`.
- `repeat {moves: [1-3 move], until: [1-8 condition], max_iterations: 1-40, stop_when_refused?: bool = false}` (FD9) - see the "Repeat" section below for the full grammar and stop rules.

`declare_attackers` and `declare_blockers` must each be the first move of the
play, otherwise `combat-move-not-first`, and a play carries at most one of
them (a second combat move can never also be first).

- Move kinds: `activate`, `cast`, `play_land`, `answer`, `pass`, `declare_attackers`, `declare_blockers`, `keyword_action`, `mulligan`, `repeat`

A target is `{select, side, prefer?: [0-4 select], ties?: any, zone?:
battlefield|stack|graveyard|exile}` (a card or permanent matching `select`,
on that side) or `{player: me|opponent}`, one per target the spell or ability
announces, in order. `prefer`/`ties` rank the candidates the same way a
`Pick` or a `ZonedCard` already does (O2, below); `zone` (FB2 T1) narrows
which zones are searched - left out, a target still resolves over
battlefield and stack, today's behaviour. Anything else is `malformed-play`,
and a key beside either shape is `unknown-field`.

- Target zones: `battlefield`, `stack`, `graveyard`, `exile`

A sacrifice choice is `{select, count?: >=1, prefer?: [0-4 select], ties?: any}`: the
permanents the activating player controls matching `select`, for an
activation cost's own "Sacrifice a ..." piece (Goblin Bombardment's "Sacrifice
a creature"). `count` left out is the cost's own number. `source` sacrifices
the ability's own source, `none` nothing.

A discard choice is the same shape, `{select, count?: >=1, prefer?: [0-4
select], ties?: any}`: cards in the activating player's own hand matching
`select` (CR 701.9), for an activation cost's own "Discard a ..." piece
(Psychic Frog's "Discard a card:"). `count` left out is the cost's own
number, read off the cost text the same way a sacrifice choice's is. `none`
discards nothing. There is no `source` value here - an off-zone ability
whose whole cost is "Discard this card" (cycling, transmute) names nothing
at all, since there is nothing left to choose; that case is `sacrifice`'s
own `source` value, read off the ability's cost text regardless of whether
the underlying component is a sacrifice or a discard.

### `prefer` and `ties` (O2)

`play_land.card`, `cast.card`, `activate.source`, `Pick` (an answer's
`pick`), a sacrifice choice, a discard choice, a `CardTarget` (FB2 T1), an
`AttackGroup` and a block's `blocker`/`attacker` (FB2 M2/M3) all take
`prefer` (0-4 selectors, the same fan-out as `moves`) and `ties: "any"`.
`prefer` narrows the match through
every selector in the list in turn, each one applied against whatever the
previous one left - a selector that would leave nothing is skipped rather
than emptying the match, so the runner never discards a narrower tier for a
looser one that comes after it; `ties: "any"` changes only what happens when
candidates of different names are still tied after all of them: instead of
exiting `ambiguous`, the runner takes the first `count` of them by sorted
name. Left out, behaviour is unchanged.

### `cast.cost`

Optional. It names the cost the spell is cast under; left out, the runner
casts it the normal way. The set is closed and derived from the engine's own
cast option vocabulary, never from card names: `normal` is the printed mana
cost; `printed` is a card's own alternative cost announced as
`CastRequest.alternative_cost` (a pitch cost); every other value is a cast
option keyword the engine hands out (`cast_options[].keyword` on a card view,
`CastRequest.keyword`), lowercased. `impending` is named although the engine
offers no impending cast yet. A cost the card has no live cast option for is
refused by the engine at run time, like any other illegal move; a value
outside the set is `unknown-cast-cost` at check time.

- Cast costs: `normal`, `adventure`, `airbend`, `bestow`, `dash`, `disguise`, `emerge`, `escape`, `evoke`, `flashback`, `foretell`, `harmonize`, `impending`, `madness`, `mayhem`, `megamorph`, `morph`, `plot`, `printed`, `prowl`, `sneak`, `spectacle`, `surge`, `warp`, `web-slinging`

`tests/test_playscript_schema.py` reads the engine's casting source, so a cast
option the engine learns fails that test until it is added to `CAST_COSTS`,
and then this doc's test until it is listed here.

### `answers[]`

Answer item: `{decision, pick?: {select, count?: >=0, prefer?: [0-4 select], ties?: any}, accept?: bool, to_graveyard?: select, to_bottom?: select, amount?: >=0|max, choice?: string, choice_from?: {zone, side, select, prefer?: [0-4 select], ties?: any}, pile_one?: bool, players?: [0-2 me|opponent], top_order?: [0-4 select], position?: top|bottom, targets?: [0-6 <target>], order?: [1-6 select], damage?: {tiers: [0-6 select], lethal_first?: bool, rest_to?: player|last_blocker}}`.
`pick.count` and `targets` may both be 0 (FD2): "choose none" is a legal
answer, not a bound violation.

An answer's fields must fit its decision kind, or it is
`answer-shape-mismatch`. A pay-amount decision (Galvanic Discharge's
`pay_energy_amount`, Necrodominance's `pay_life_amount`) is answered with
`amount` alone: an integer from 0, or `max` for the most the decision allows.
`accept`, `pick`, `to_graveyard`, `to_bottom`, `choice`, `choice_from`,
`pile_one`, `players`, `top_order`, `position`, `targets`, `order` or
`damage` on it, or no `amount`, is `answer-shape-mismatch`, and so is an
`amount` on any other kind. A negative `amount` is `bounds-exceeded`; any
other value is `malformed-play`.

`order` (FD2, CR 603.3b) fits only `order_triggers`: 1-6 selector tiers
ordering the pending triggered abilities - the FIRST tier goes on the stack
FIRST and so resolves LAST ("bottom of the stack first"); the LAST tier goes
on last and resolves first, on top. Naming it on any other kind is
`answer-shape-mismatch`.

- Order decision kinds: `order_triggers`

`damage` (FD2, CR 510.1c/702.19b) fits only `assign_combat_damage`, and
fits it alone - no other field may accompany it, the same footing `amount`
is on: `{tiers: [0-6 select], lethal_first?: bool, rest_to?: player|last_blocker}`.
`tiers` orders the blockers (and, once trample lets damage through, the
defending player or planeswalker) from first to last in the decision's own
damage-assignment order; `lethal_first` assigns each tier only its lethal
damage before any moves past it (the default, left out); `rest_to` says
where a trampling attacker's leftover damage goes once every blocker tier
has lethal damage - `player` (the default, left out) or `last_blocker`.
Naming `damage` on any other kind is `answer-shape-mismatch`, and so is
naming it without `assign_combat_damage` answering with it.

- Damage decision kinds: `assign_combat_damage`

`top_order` (FB2 A1) fits only a `scry`/`surveil` answer: tiers ordering the
cards kept on top after either, first tier on top, the rest kept in
looked-at order after them - naming it on any other kind is
`answer-shape-mismatch`.

- Top-order decision kinds: `scry`, `surveil`

`position` (FB2 A1) fits only `put_on_top_or_bottom` (Subtlety's "its owner
puts it on the top or bottom of their library"): `top` or `bottom`, an
alternative to `to_bottom` for that one decision - naming both on the same
answer is `answer-shape-mismatch`, and naming `position` on any other kind is
too.

- Position decision kinds: `put_on_top_or_bottom`

`targets` (FB2 A2) fits only `choose_targets`: 1-6 targets, the same
`{select, side, prefer?, ties?, zone?}`/`{player: me|opponent}` shapes a
`cast`/`activate` target already takes (see "Moves" above) - an alternative
to `pick` that can name a player target, which `pick`'s selector-driven match
never could. Naming both `targets` and `pick` on the same answer, or naming
`targets` on any other kind, is `answer-shape-mismatch`.

`pile_one` (CR 700.3's pile primitive - Liliana of the Veil's own `-6`, Do or
Die) answers a `choose_pile` decision: `true` for pile one
(`pending_decision.cards`), `false` for its complement. `players` (CR
701.34a's own "and/or players" half of Proliferate) answers a
`choose_players` decision: each `me`/`opponent` token resolves to that
player's own username the same way a `PlayerTarget` already does, restricted
to whoever the decision actually offered.

- Amount decision kinds: `pay_energy_amount`, `pay_life_amount`

`choice` (O6) is a non-blank string, taken only by `CHOICE_DECISION_KINDS`
(`choose_a_mode`, `choose_a_quality` - CR 614.12a's and CR 700.2a's "as this
enters, choose ..." and "choose ... mode" questions, the api's own
`TOOL_FIELD_FOR_KIND` mapping both to `choice`); elsewhere it is
`answer-shape-mismatch`. Card names are still refused there, with the same
type-line-word exemption other selector fields get, so a creature type such
as `Eldrazi` is allowed. `choose_a_mode` is always fully enumerated
(`pending.options`); `choose_a_quality` is sometimes open (a creature type, a
card name), and an empty `options` there means any value is accepted, left to
the engine to judge. A `choice` naming something outside a non-empty
`options` exits `unforeseen`, the same as `pick` finding nothing to match.

- Choice decision kinds: `choose_a_mode`, `choose_a_quality`

`choice_from` (FD2) is an alternative to `choice` for `choose_a_quality`
alone (`CHOICE_FROM_DECISION_KINDS`) - never both on the same answer, and
naming it on `choose_a_mode` (always fully enumerated by fixed mode text,
never a card) or any other kind is `answer-shape-mismatch`. It is a selector
over `zone` and `side`, resolved at run time to a matched card: the runtime
derives the actual quality (Cavern of Souls' creature type, say) from
whichever card it matches, so a card name never appears in the file - the
whole point of the field, next to plainly writing the quality as a literal
string in `choice`.

- Choice-from decision kinds: `choose_a_quality`

`decision` is a closed set equal to the api's pending decision kinds: the
values of `decisions.ANSWERABLE_KINDS`, which are the engine `DecisionKind`s
the api can pend. A value outside it (a typo such as `choose_cardz`) is
`unknown-decision-kind`. `FORBIDDEN_DECISION_KINDS` is empty (FD2): every
decision kind is answerable, each fitting one of the shapes above - `mulligan`
plainly, with `accept` (or more fully with the `mulligan` move below),
`assign_combat_damage` only through `damage`. `declare_attackers`/
`declare_blockers` are in the set but refused with `use-combat-move` (FB2 D1)
instead - a script never answers them as a decision either, but the refusal
names the real fix: use the `declare_attackers`/`declare_blockers` move
(M2/M3 above), not an answer item.

- Decision kinds: `assign_combat_damage`, `choose_a_mode`, `choose_a_quality`, `choose_cards`, `choose_pile`, `choose_players`, `choose_targets`, `declare_attackers`, `declare_blockers`, `may`, `mulligan`, `opening_hand_action`, `order_triggers`, `pay_energy`, `pay_energy_amount`, `pay_life`, `pay_life_amount`, `pay_mana`, `put_on_top_or_bottom`, `scry`, `surveil`
- Never answered:

### D2: the standalone pending-decision answer

A play whose only move is `answer` and whose `true` names a `pending_decision`
condition (FB2 C1) is the standalone pending-decision answer: it runs the
moment that decision is pending for the seat, with no priority of its own
required - the priority gate every other move needs does not apply, because
answering a decision already paused mid-resolution is not itself an action
that needs priority. The answer's own `decision` must equal the condition's,
otherwise `answer-decision-mismatch`.

## Repeat (FD9)

Owner, verbatim: "DSL is meant to fix missing repeat-until loops. Instruct
agents to generate DSLs instead of manually calling MCPs." A `repeat` move
repeats a small step sequence until a condition holds, a hard iteration
count is reached, or a resource guard hits - the shape finding seq 191
needed and the format had no way to say: a seat spent roughly 25 manual MCP
calls hand-driving a Devoted Druid + Vizier of Remedies mana loop to reach
an X of 8, one activation at a time, because the play-script format had no
repeat construct at all.

### Grammar

`{kind: repeat, moves: [1-3 move], until: [1-8 condition], max_iterations:
1-40, stop_when_refused?: bool = false}`. `moves` is 1-`MAX_REPEAT_BODY` (3)
of the loop's own body, drawn from the same move kinds every other `moves`
entry is, except `repeat` itself (`repeat-nested` - no nesting) and `pass`
(`repeat-pass-in-body` - a loop's own body commits real moves and exits
through `until`, `max_iterations` or a refusal, never by releasing
priority). `until` is 1-`MAX_CONDITIONS` (8) conditions of the existing
kinds - the loop stops once every one of them holds - and an empty list is
`repeat-empty-until`. `max_iterations` is required, an integer 1-40
(`MAX_REPEAT_ITERATIONS`); missing or out of range is `repeat-bound`, since
FD9 requires every loop to say up front how many iterations it is willing to
run, independent of whatever `until` alone would leave unbounded.
`stop_when_refused` (default false) is described under "Stop rules" below.
Every per-move rule that already applies to a play's own top-level `moves` -
the card-name ban, the selector rules, `phrase-too-narrow` (with the
`ability_index` exemption) and the answer-shape rules - applies to a
repeat's own body moves the same way, at whatever nesting a body move sits
at (`moves[i].moves[j]`).

A `repeat` counts as one move toward `MAX_MOVES`/`MAX_MOVES_PER_RUN`; its own
iterations are bounded only by `max_iterations`, never by the play's own
move cap.

### `until` is judged fresh, before every iteration

`until` is judged against a freshly read board before every iteration, the
same way a play's own `true`/`false` are judged against the board a run
starts from - so an `until` that already holds when the loop is first
reached runs the loop zero times, and every iteration after the first
re-reads the board rather than reasoning about it from what the previous
iteration is expected to have done.

### Stop rules (FD3 part 7 implements these exactly)

The runtime that actually drives a `repeat` (each iteration's board re-read,
the no-progress guard, the resume token) is FD3 part 7's, in
`playscripts.runtime` - not this schema task. This section names every stop
rule that runtime implements, so the two never drift:

- `until` met (including on the loop's own first judgement, zero iterations
  run): the script continues with the move after the `repeat`.
- `max_iterations` reached with `until` still unmet: `unforeseen`, with
  `stop_reason` `repeat_bound`.
- An iteration whose moves were all accepted but that changed nothing on the
  board (a digest over zones, tapped state, counters, life and mana pool):
  `unforeseen`, with `stop_reason` `repeat_no_progress` - the guard against a
  loop that keeps "succeeding" at doing nothing.
- An engine refusal mid-loop: `refused`, carrying the engine's own text - or,
  with `stop_when_refused: true`, a normal end of the loop instead, and the
  script continues with the move after the `repeat`.
- A pending decision the body's own `answers` do not cover: handed back as
  `input_required`, resumable into the same loop - the agent answers it and
  resumes exactly where the loop left off, rather than the loop failing
  outright the way an unanswered decision would end an ordinary move.
- A priority change the loop does not own (an opponent's action, an object
  on the stack the loop is not itself resolving, priority passing to another
  player): `interrupted`.
- The run's own time cap, mid-loop: the resume token carries the repeat's
  own move index, iteration count and body position, and a resumed run
  continues the same loop from there, never restarting it.

### Worked examples (selectors only, no card names)

Devoted Druid + Vizier of Remedies (devoted-combo, finding seq 191): with
Vizier of Remedies' own replacement effect live ("that many -1/-1 counters
minus one"), Devoted Druid's own "put a -1/-1 counter on this creature:
untap this creature" ability costs zero counters, so the mana ability and
the untap ability repeat for free:

```json
{
  "kind": "repeat",
  "moves": [
    {"kind": "activate", "source": {"zone": "battlefield", "select": {"types_all": ["Creature"], "subtypes_any": ["Elf"], "tapped": false}}, "ability_index": 0},
    {"kind": "activate", "source": {"zone": "battlefield", "select": {"types_all": ["Creature"], "subtypes_any": ["Elf"]}}, "ability_index": 1}
  ],
  "until": [{"kind": "mana_pool", "color": "G", "op": ">=", "n": 8}],
  "max_iterations": 10
}
```

One `run_script` call in place of the roughly 25 manual MCP calls finding
seq 191 needed to reach the same X of 8.

Walking Ballista (eldrazi-tron, prison-tron): a resource-guard loop adds
counters while untapped mana allows, then a second loop removes them to ping
down small creatures until none remain - "while an opponent has" a target,
the same shape the owner's own untapped-blockers example gives
(`until: count battlefield side opponent types_all [Creature] tapped false
== 0`):

```json
{
  "kind": "repeat",
  "moves": [
    {"kind": "activate", "source": {"zone": "battlefield", "select": {"types_all": ["Artifact", "Creature"], "subtypes_any": ["Construct"]}}, "ability_index": 0}
  ],
  "until": [{"kind": "untapped_mana", "op": "<", "n": 4}],
  "max_iterations": 8
}
```

```json
{
  "kind": "repeat",
  "moves": [
    {"kind": "activate", "source": {"zone": "battlefield", "select": {"types_all": ["Artifact", "Creature"], "subtypes_any": ["Construct"]}}, "ability_index": 1, "targets": [{"select": {"types_all": ["Creature"], "toughness": {"op": "==", "n": 1}}, "side": "opponent"}]}
  ],
  "until": [{"kind": "count", "zone": "battlefield", "side": "opponent", "op": "==", "n": 0, "select": {"types_all": ["Creature"], "toughness": {"op": "==", "n": 1}}}],
  "max_iterations": 15
}
```

The first loop's own resource guard is `untapped_mana` itself, the condition
the format already had; the second is the "while an opponent has" shape,
over toughness rather than tapped state, proving the pattern generalizes
past the owner's own worked example.

### `repeat-until-static`

A warning, never a failing error: raised when a `repeat`'s own `until` reads
only `turn`/`input` conditions - nothing its own body could ever change - so
the loop either stops on its first judgement, before the body ever runs, or
never stops short of `max_iterations` or a refusal. A script author writing
a fixed-count loop bounded by `max_iterations` alone is a legitimate use
this warning still flags, since it never fails the gate.

### Conflicts: a repeat's first body move is the play's first move

FD2's move-selector conflict dimension reads a `repeat`'s own first body
move as the play's first move for exclusivity purposes: the runtime sends
that body move first, the same way an ordinary play's first move is sent
first, so a `repeat` play and a plain play of the same first move conflict
when their selectors overlap and are exclusive when the same rule
(`_move_selectors_disjoint`) already proves them disjoint.

## Advice-only plays

`moves` is 0-4, not 1-4: a play may carry no moves at all. A play with no
moves is advice-only. It commits nothing to the game - no cast, no
activation, no land drop, no answer - and exists only to explain a decision
the seat should reach on its own: it is how "hold" is expressed, a shape the
DSL had no way to say at all before `note` and an empty `moves` landed
(API-1). An advice-only play's `note` (or, until a deck's `note`s are
written, its `intent`) is the whole of what it has to say - naming one
"hold up Fatal Push for their Tarmogoyf" *is* the play.

An advice-only play is outside the `play-conflict`/`next-conflict` proofs
by construction, not by an exemption layered on top of them (the
now-removed `too-many-active-for-archetype` cap drew the same line over its
own live-together count while it existed): every one of those is a proof
about what can *commit* at one decision point, and a play with no moves has
no move to commit, so there is nothing about it for the proof to be.
`lint._advice_only` docs draws the same line - an advice-only play is never
one half of a play-conflict or a next-conflict entry, priority or not,
because the proofs' own dimensions (`whose_turn`, `steps`,
`opposing_archetypes`, `stack`, `input`, `true_false`, `numeric_range`) are
never asked about it in the first place, not because a carve-out excuses it
after the fact.

### No play-count cap

`too-many-plays` (move-bearing plays), `too-many-advice` (advice-only
plays) and `too-many-active-for-archetype` (O8, priority > 0 plays live
together per archetype key) are removed outright, not raised (owner,
2026-09-24): a deck may carry any number of plays of each kind. Per-decision
review submission (see "The review loop" below) means every reviewed game
can propose a refined play, a new play or a re-prioritized play for each
decision the seat made - a fixed per-deck ceiling on move-bearing,
advice-only or live-together plays would only cap how much of a game's
decisions review can actually land.

What `check_deck` still proves, in bounded time regardless of play count, is
pairwise non-overlap: `exclusivity` and `_conflict_warnings` compare every
pair of enabled (`priority > 0`), move-bearing plays and warn
(`play-conflict`, never failing the gate) when neither can be proven
exclusive by `whose_turn`, `steps`, `opposing_archetypes`, `stack`, `input`,
`true_false` or `numeric_range` - an O(n^2) pass over the deck's enabled
plays, never the exponential live-together search the old per-archetype cap
used to gate against (`_max_live_together`, Bron-Kerbosch without a pivot,
removed along with the cap it bounded). A deck of 60 move-bearing plays (30
of them sharing one archetype key), 20 advice-only plays, all pairwise
non-conflicting, still clears `check_deck` well under 3 seconds.

`plays admit` still takes a retirement and an addition in the same call: it
commits the working tree as it stands, so a deck that deletes one play and
adds another lands both in one push. The conflict ratchet (`_ratchet`) diffs
the deck's warnings before and after the change by rebuilding the corpus at
each of `origin/main` and the new commit, so a `play-conflict` or
`next-conflict` tied only to the retired play is simply absent from the
"after" side - it is read as a conflict that vanished, never as a new one to
refuse.

### `advice-only-chained`

An advice-only play commits nothing, so it can neither chain further nor be
chained into. `advice-only-chained` refuses both directions, one error per
offending `next` entry, naming both play ids:

- An advice-only play whose own `next` is non-empty is refused: there is
  nothing for it to chain further *from* - it took no action for a follow-up
  play to build on.
- Any play (advice-only or not) naming an advice-only play in its own `next`
  is refused: there is nothing for the target to be chained *into* - an
  advice-only play has no move of its own to try when reached that way.

## Arming: there is no autofire

By owner decision (2026-09-21), nothing a play describes ever fires on its
own - there is no autofire, and there never was a promotion path toward one.
`arm()` (`playscripts.arming.arm`) does not watch the board between calls and
does not run anything the agent did not just ask for in that same call:
arming only wakes the wait (`pass_until`/`wait_for_priority`) with the
already-armed call ready, and judges every armed id fresh against the board
only once that wait is about to return with a real stop - never on a
retryable hand-back where the opponent still holds priority. That judgement
runs only because the agent supplied its own `arm_reasoning` for it; an empty
one is refused. The extra agent turn this costs - the call that supplies
`arm`/`arm_reasoning`, once per armed play, every time a seat wants the same
scripts armed again - is permanent and deliberate, not a transitional cost of
an unfinished feature: arming is preferred, never mandatory, and its own
lifetime is one call. There is no promotion path from "armed" to "fires
without an agent turn": no field, no lint rule and no firing path exist for a
play to describe itself as self-triggering, and `autofire` as a top-level key
is refused outright as `unknown-field` if a play ever carries one.

## How the runner builds a move

`playscripts/runtime.py` turns each move into the request a seat would send,
read off the redacted state view:

- `cast.cost` other than `normal` goes out as `CastRequest.alternative_cost`:
  the lowercased keyword (`evoke`, `impending`, ...), or for `printed` the
  card's own name, which is how the engine names a printed CR 118.9 cost. A
  cost the card has no live option for is refused in the engine's words.
- A `{select, side, prefer?, ties?, zone?}` target resolves over `zone` (left
  out, battlefield and stack, today's behaviour) to one id; a
  `{player: me|opponent}` target resolves to that player's name. Both go out
  in the route's `targets`, for `cast` and `activate` alike, and the same
  resolution answers a `choose_targets` answer's own `targets` (FB2 A2).
- `activate.ability_index` (FB2 M4) picks the source's ability by index off
  the state view's own `abilities` list, the same list `ability_text_has`
  already reads a phrase out of; an index the card does not carry is
  `unforeseen`.
- `pass` (FB2 M1) sends pass-priority once (the same route the seat's own
  pass uses, `hold_priority=False`) and releases - it does not itself wait
  for the turn to come back around, and a move after it is never allowed
  (`pass-not-last`), so a run ending in `pass` finishes `chain_complete` the
  moment priority is released.
- `declare_attackers` (FB2 M2) sends each `AttackGroup` to the attack route
  (or `/decide` with the decision's own `pairs`, whichever the seat's own
  tool uses), resolved over the seat's untapped creatures that can attack
  through the same pick rule as everywhere else; `defender` left out
  resolves to the opponent player.
- `declare_blockers` (FB2 M3) sends each `Block` to the declare-blockers
  route the same way, `blocker` resolved from the seat's untapped creatures
  and `attacker` from the creatures attacking this seat.
- `activate.sacrifice` goes out as `ActivateRequest.sacrificing` (or, when the
  ability's own cost text reads "discard" rather than "sacrifice",
  `.discarding` instead - the same `source`/`none`/choice value either way):
  `source` is the source's own id, unless the cost is a self-contained
  "Discard this card" (cycling, transmute), which sends neither field at all,
  since there is nothing left for either to name; `none` sends nothing; a
  sacrifice choice picks from the permanents the seat controls, `count` of
  them (left out, the number the ability's cost names, `a`/`another` being
  one).
- `activate.discard` goes out as `ActivateRequest.discarding`: `none` sends
  nothing, and a discard choice picks from the seat's own hand, `count` of
  them (left out, the number the ability's cost names the same way a
  sacrifice choice's is).
- An `amount` answer goes to `/decide` on the pending decision's own
  `answer_field` (`energy_amount`, `life_amount`) with its `generation`; `max`
  is the decision's own `amount`.
- A `surveil` or `scry` answer goes to `/decide` as `moved`: the cards looked
  at that `to_graveyard` (surveil) or `to_bottom` (scry) matches, with the
  rest kept on top in the order they were looked at. An answer naming neither
  moves nothing, so every card stays on top. `top_order` (FB2 A1) orders that
  "kept on top" half instead of leaving it in looked-at order: the cards
  matching its first tier go on top, then its second, and so on, with
  anything no tier matches kept after them in looked-at order.
- A `put_on_top_or_bottom` answer's `position` (FB2 A1) sends `top: true` for
  `top`, `top: false` for `bottom` - the same `top` field `to_bottom`'s
  presence/absence already drove, now named directly instead of inferred.
- Every selection follows one pick rule: nothing matching is `unforeseen`, and
  different cards still tied after `prefer` are `ambiguous` - unless `ties:
  "any"` (O2), which takes the first `count` of them by sorted name instead.
- A `choice` answer (O6) goes to `/decide` as `choice` outright. A
  `choose_a_mode` decision's `options` is always fully enumerated; a
  `choose_a_quality` decision's is sometimes empty (an open question, a
  creature type or a card name), and an empty `options` there means any
  value is sent through unjudged, left to the engine. A `choice` naming
  something outside a non-empty `options` is `unforeseen`.
- A `surveil`/`scry` answer with no `to_graveyard`/`to_bottom` at all is a bare
  answer and means "keep everything on top": the request sends `moved: []`,
  never leaves the decision unanswered. One naming a selector still sends only
  the cards it matches, exactly as before.
- `activate` and `cast` never name `mana_sources`: a play cannot name its own
  sources today, so the request always leaves the field out and the engine
  auto-pays (CR 601.2g) over the payer's untapped permanents, matching the
  CLI's and MCP's own `--sources`/`mana_sources` semantics (an audit row then
  names only the permanents that actually paid, never every untapped one).

## Exits

A run ends on one exit from a closed set, and a caller branches on it, never on
prose:

- Exits: `chain_complete`, `not_applicable`, `unforeseen`, `interrupted`, `ambiguous`, `refused`, `stale`, `unknown_outcome`, `needs_plan`, `already_ran`, `advised`, `needs_input`, `invalid_input`

`needs_input` is a required input the agent did not supply. `invalid_input` is
a supplied input the deck's catalog refuses: a name neither declared nor
`opposing_archetype`, or a value outside its enum, not a bool, or outside its
int range. It carries `{name, value, reason}` and names both in its
`stop_reason`, and it is never reported as `not_applicable`, because nothing
about the board was judged. The asserted `opposing_archetype` is always one of
its own enum's values; the play's `opposing_archetypes` judges it.

`already_ran` is scoped per uid role, not merely per seat: the once-per-turn-
and-step slot a `start` checks is keyed on `(seat, role, script_id, turn,
step)`, `role` being `seat` or `advisor`. An advisor's advise run of a script
proposes a move and takes nothing, so it never consumes the seat's own slot
for the same script, turn and step - the seat's real run afterward proceeds.
Two advise runs of the same script by the same advisor, in the same turn and
step, still share a key and the second is `already_ran`.

`already_ran` also never blocks on a finished run that made zero moves
(`moves_done: 0`) - refused before its first move, `not_applicable`, or the
like: it touched nothing, so it never consumed the slot, consistent with that
finish's own `reapplicable_after: true`. Only a finished run naming at least
one done move, or a bare `start` with no finish yet (still in flight, or a
crash - it cannot be told apart from one that did move something), blocks a
retry.

`when` names a board position (step, whose turn, stack), never a priority
window: the board can already sit at it while the *other* seat holds
priority there first. The runner never sends its first move into that -
which the engine can only refuse - and instead waits for this seat's own
priority within that same occurrence (`wait=true`, the default) or reports
`not_applicable` (`wait=false`), exactly as it does for a position not yet
reached.

`POST .../script-runs` (`start` and `finish` alike) and `run_script` are
refused once the game is over (CR 104.1), the same way every action route
is: no ledger row for either phase once a game has ended.

A refusal is classified from the outcome's own typed fields, never from its
prose: `refused` carries the engine's own text, and `stale` is reserved for a
stale-seq or generation mismatch (`is_stale`'s own `stale: true`). A pass by
the other seat that itself paused on this run's own pending decision
(`paused: true`, `paused_on.player` naming this run's seat) is not a board
change - the run reads that off the entry's typed fields and carries on rather
than reporting `interrupted`.

`moves_done` counts a move as done the moment the engine accepts it - paid,
resolved or paused counts, refused does not - so a run that sent and paid a
move and then stopped mid-resolution (`ambiguous`, `interrupted`,
`unforeseen`) still reports `moves_done >= 1`. The finish record and the
resume token's own `moves_done` always agree.

## Guide edits: pushed to main by the seat (2026-09-22)

`mcp-seat guides propose --deck SLUG [--deck SLUG ...] --game-id GAME` takes a
deck-guide edit straight to mtg-decks main; there is no branch, no pull
request and nobody merging it afterwards (this replaces the 2026-09-13
proposal-branch design). For each listed deck the allowlist is read off
HEAD: a guided deck (one with `decklist.txt`) may change `NOTES.md` and
`matchups[].play_plan` / `notes` of its newest boarding plan, and may append
a matchup row only as `name` (new under slugify), `origin: "seat"`, a
`play_plan` of the five strings `keep`, `goldfish_for`, `watch_for`, `posture`
and `posture_why`, and `notes`; a stub deck (an opponent archetype with no
guide) gets `NOTES.md` alone, and a new stub whose slug is a guided deck's
directory or archetype name is refused. A file holding a conflict marker
line is refused before anything parses it.

Under the checkout lock the command captures the edit, fetches origin main
and, when main moved, replays each file onto it with a 3-way merge before a
hard reset onto origin main (safe only because the allowlist has already
refused every other path). A clash returns `conflict` with git's markers in
the working tree and no rebase or merge state; the agent resolves the markers
and runs the same command again, and every gate runs again. Otherwise it
renders the guided decks' guides (`render_guides.py write` and `check`;
stubs are never rendered), runs `verify_sidecars.py --domain 3` on each
changed plan's run, commits with the seat's identity and a `Game-Id` trailer,
and pushes `HEAD:main`, never forced. After three lost races the result is
`error` with `retryable: true` and the edit left uncommitted on the newest
upstream. `mcp-seat plays admit` ends the same way after three races; it no
longer falls back to anything else.

## The review loop

Per-decision review submission (owner, 2026-09-24): the handover turn walks
every decision the seat made that game and proposes a play for it -
refining a script it ran, drafting a new one for a line it played by hand,
or recording why no change is due (`mcp-seat plays no-change`) - merged and
deduped into the deck's existing plays rather than kept one-to-one with the
game's decisions. This is what made the play-count caps above a liability
rather than a backstop, and is why they are removed rather than raised.

**Denied means declined.** The runtime has no "deny" action:
`playscripts.report` pairs each offer with the seat's next committing
action into `taken`, `passed_over`, `declined` or `offered` (no commitment
yet). A `declined` offer is what "denied" means in review. When a play is
declined, the review must refine it - narrow or extend its conditions, and,
if the offer really was wrong for that board, add a new or adjusted play
for what should have happened instead - and then adjust both the old and
the new/refined play so their conditions cannot both hold at once. The
play-conflict exclusivity proof (`lint.exclusivity`, `_conflict_warnings`)
is the check: an admit that leaves the pair overlapping fails the conflict
ratchet (`_ratchet`, `conflict_regression`) the same as any other new
conflict.

**A corner case is described, never turned down.** An offered play whose
conditions hold may not be declined on corner-case grounds - the seat takes
it, plays it out, and in review describes the corner case as a precise
condition that splits the play into two, one for the common case and one
for the corner, each narrowed until their conditions cannot both hold.
There is no probability or frequency field anywhere in the schema
(`priority` is ordinal, not a weight) for exactly this reason: a corner
case is named by a condition, never discounted by a likelihood.

## Schema version

`schema_version` stays 1 through FB2's B4 additions (2026-09-24). Every one
of `pass`/`declare_attackers`/`declare_blockers` (M1-M3), `ability_index`
(M4), `top_order`/`position`/`targets` on an answer item (A1/A2), `prefer`/
`ties`/`zone` on a `CardTarget` (T1) and `pending_decision`/`compare`/
`all_of` (C1-C3) is either a new optional field (left out, a play validates
exactly as it did before) or a new discriminated `kind`/`of` value (a union
member the old vocabulary never had), never a change to an existing
required field or an existing kind's meaning - so every play already
committed, the 94 in the mtg-decks corpus at the time of writing among them,
stays valid unchanged. This is what a schema version bump would otherwise be
for: since nothing about reading an *old* file changes, there is no reader
to migrate.

There is a real consequence, but it falls on deploy order, not on the
version number: this api's own `Play` model has to be the one running
*before* any deck's plays actually use one of these new kinds. Pydantic's
strict, discriminated unions already refuse an unrecognized `kind`/`of` value
loudly (`forbidden-kind` or `malformed-play`, never a silent misread) - so an
older api, or the mtg-decks stdlib mirror (`scripts/check_plays.py`) before
its own FB2 update lands, would correctly refuse a file naming
`declare_attackers` as a move kind or `all_of` as a condition kind, the same
way it refuses any other unrecognized shape today. That refusal is safe, but
it means FB2's schema change (FC4) has to reach this repo's `main` and a live
deploy before FB2's mtg-decks mirror (FC8) pushes a play using any of the new
vocabulary to mtg-decks' own `main` - the ordering note carried in the task
plan that shipped this change.

FD2 (2026-09-26) keeps `schema_version` at 1 the same way: every new field on
`cast`/`activate` above, the two new move kinds (`keyword_action`,
`mulligan`), the three schema-local steps, and the three new answer fields
(`order`, `damage`, `choice_from`) are each a new optional field or a new
discriminated value, and the two decision kinds `FORBIDDEN_DECISION_KINDS`
used to refuse are now answerable rather than forbidden - loosening a refusal
is additive the same way a new field is, since no play already committed
could have named either kind at all. The same deploy-order consequence holds:
this api's `main` reaches a live deploy before FD8's mtg-decks mirror update
pushes a play naming any of FD2's vocabulary.

FD9 (2026-09-26) keeps `schema_version` at 1 the same way: a tenth move kind
(`repeat`) and a twelfth condition kind (`mana_pool`, plus its own `compare`
operand) are each a new discriminated value, never a change to an existing
kind's meaning, so every play already committed stays valid unchanged. The
same deploy-order consequence holds again: this api's `main` reaches a live
deploy before FD8's round-3 mtg-decks mirror update (part 4) pushes a play
naming `repeat` or `mana_pool`.

## Migration: `note` becomes required later

`note` lands optional (API-1): every play already committed validates
unchanged, and no deck has to be rewritten before this schema change ships.
Until a play carries its own `note`, the hint that surfaces a play's
reasoning mid-game falls back to `intent`, truncated at a sentence boundary
within `MAX_NOTE_LENGTH` (240) characters - an approximate, human-written
sentence read by an agent is still a `why` where there would otherwise be
none, so every play gets a `why` immediately rather than waiting on a
rewrite. As each deck is rewritten, its plays gain real `note` strings
written for the agent reading them mid-turn, not borrowed from `intent`.
`note` becomes a required field only once the last deck has been rewritten
with real notes of its own - schema first, then each deck in turn, then the
requirement; there is no date fixed for that step today.

## Not in the vocabulary

Concede. (FB2 2026-09-24: attacking, blocking and passing moved out of this
list - `pass`/`declare_attackers`/`declare_blockers` are now real move
kinds, M1-M3 above. FD2 2026-09-26: mulligan and assign-combat-damage moved
out too - `mulligan` is a real move kind and a plain `accept` answer, and
`assign_combat_damage` is answered through `damage`. FD9 2026-09-26:
iteration moved out too - `repeat` is now a real move kind, bounded by
`until`, `max_iterations` and a resource guard, never unbounded. `concede`
stays out permanently: it is not a `DecisionKind` the engine ever pends, it
ends the game, and a script must never be the thing that ends a game.)
