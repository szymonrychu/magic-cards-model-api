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
  `intent?` (prose, ignored by machines), `priority` (0-100, higher tried
  first, 0 = disabled - at most 6 plays with priority > 0 may be *live
  together* per archetype key, O8: the largest subset of the plays applying
  to that key no two of which are provably exclusive by `whose_turn`,
  `steps` or turn range, not the plain count that apply, so two groups a
  deck's own turn guards already prove disjoint are never summed),
  `opposing_archetypes` (1-32: `["*"]` alone, or matchup
  label slugs; `*` beside a key is `wildcard-with-keys`),
  `when {whose_turn: mine|theirs|either, steps: [1-4 distinct autopass steps,
  not declare_attackers/declare_blockers], stack: empty|any}` (a repeated step
  is `duplicate-step`), `true` (0-8 conditions), `false` (0-8 conditions, each
  must NOT hold), `moves` (1-4), `next` (0-3 ids).
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
  `enters_any` (0-3 of `untapped`/`tapped`/`untapped_if_paid`); `unlocks`;
  `unlocks_by_search`; `affordable {extra: n}`.
- `colors_any` reads the card's `colors` by name or letter, any case, from a
  closed vocabulary: the five colors and `colorless`. `colorless` (or `C`)
  matches a card whose color set is empty (CR 105.2c, "A colorless object has
  no color"), a devoid Eldrazi included, and never a colored card.
- Colors: `white`, `blue`, `black`, `red`, `green`, `colorless`
- Condition kinds (8): `count`, `zone_size`, `life`, `stack_top`, `land_drop_available`, `untapped_mana`, `turn`, `input`
  (an `in`/`not_in` value list holds 0-32 values).

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
  move's card, source, target or sacrifice choice, and an answer's `pick` and
  its `prefer`. `zone_size` takes no selector.
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
  every selector that asks. `extra: 0` asks whether the card is affordable
  from untapped mana alone; a card with no `mana_value` (a land), or a call
  that never computed `untapped_mana` at all, never matches.

`unlocks`/`unlocks_by_search`/`unlocks_by_basic_search` (and `enters_any`)
need `hypo`; `affordable` never does, since `untapped_mana` costs nothing to
compute from the view already in hand - a play mixing `affordable` with the
other four still needs the `GET .../playscripts/hypothetical-facts` call for
those, but never for `affordable` alone.

## Moves

Move kinds (4), each with `answers: [0-3]` (an `answer` move needs 1-3):

- `activate {source: {zone, select, prefer?: [0-4 select], ties?: any}, ability_text_has, targets: [0-6 <target>], sacrifice?: source|none|<sacrifice choice>, answers: [0-3]}`
- `cast {card: {zone, select, prefer?: [0-4 select], ties?: any}, cost?: <cast cost>, targets: [0-6 <target>], mana?: auto, answers: [0-3]}`
- `play_land {card: {select, prefer?: [0-4 select], ties?: any}, answers: [0-3]}`
- `answer {answers: [1-3]}`, for a decision already pending

A target is `{select, side}` (a card or permanent matching `select`, on that
side) or `{player: me|opponent}`, one per target the spell or ability
announces, in order. Anything else is `malformed-play`, and a key beside
either shape is `unknown-field`.

A sacrifice choice is `{select, count?: >=1, prefer?: [0-4 select], ties?: any}`: the
permanents the activating player controls matching `select`, for an
activation cost's own "Sacrifice a ..." piece (Goblin Bombardment's "Sacrifice
a creature"). `count` left out is the cost's own number. `source` sacrifices
the ability's own source, `none` nothing.

### `prefer` and `ties` (O2)

`play_land.card`, `cast.card`, `activate.source`, `Pick` (an answer's
`pick`) and a sacrifice choice all take `prefer` (0-4 selectors, the same
fan-out as `moves`) and `ties: "any"`. `prefer` narrows the match through
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

- Cast costs: `normal`, `adventure`, `airbend`, `bestow`, `dash`, `disguise`, `escape`, `evoke`, `flashback`, `foretell`, `harmonize`, `impending`, `madness`, `mayhem`, `megamorph`, `morph`, `plot`, `printed`, `warp`

`tests/test_playscript_schema.py` reads the engine's casting source, so a cast
option the engine learns fails that test until it is added to `CAST_COSTS`,
and then this doc's test until it is listed here.

### `answers[]`

Answer item: `{decision, pick?: {select, count?, prefer?: [0-4 select], ties?: any}, accept?: bool, to_graveyard?: select, to_bottom?: select, amount?: >=0|max, choice?: string}`.

An answer's fields must fit its decision kind, or it is
`answer-shape-mismatch`. A pay-amount decision (Galvanic Discharge's
`pay_energy_amount`, Necrodominance's `pay_life_amount`) is answered with
`amount` alone: an integer from 0, or `max` for the most the decision allows.
`accept`, `pick`, `to_graveyard`, `to_bottom` or `choice` on it, or no
`amount`, is `answer-shape-mismatch`, and so is an `amount` on any other kind.
A negative `amount` is `bounds-exceeded`; any other value is `malformed-play`.

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

`decision` is a closed set equal to the api's pending decision kinds: the
values of `decisions.ANSWERABLE_KINDS`, which are the engine `DecisionKind`s
the api can pend. A value outside it (a typo such as `choose_cardz`) is
`unknown-decision-kind`. Four of them are in the set but refused with
`forbidden-kind`, because scripts never attack, block, assign combat damage or
mulligan.

- Decision kinds: `assign_combat_damage`, `choose_a_mode`, `choose_a_quality`, `choose_cards`, `choose_targets`, `declare_attackers`, `declare_blockers`, `may`, `mulligan`, `opening_hand_action`, `order_triggers`, `pay_energy`, `pay_energy_amount`, `pay_life`, `pay_life_amount`, `pay_mana`, `put_on_top_or_bottom`, `scry`, `surveil`
- Never answered: `assign_combat_damage`, `declare_attackers`, `declare_blockers`, `mulligan`

## How the runner builds a move

`playscripts/runtime.py` turns each move into the request a seat would send,
read off the redacted state view:

- `cast.cost` other than `normal` goes out as `CastRequest.alternative_cost`:
  the lowercased keyword (`evoke`, `impending`, ...), or for `printed` the
  card's own name, which is how the engine names a printed CR 118.9 cost. A
  cost the card has no live option for is refused in the engine's words.
- A `{select, side}` target resolves over the battlefield and the stack to one
  id; a `{player: me|opponent}` target resolves to that player's name. Both go
  out in the route's `targets`, for `cast` and `activate` alike.
- `activate.sacrifice` goes out as `ActivateRequest.sacrificing`: `source` is
  the source's own id, `none` sends nothing, and a sacrifice choice picks from
  the permanents the seat controls, `count` of them (left out, the number the
  ability's cost names, `a`/`another` being one).
- An `amount` answer goes to `/decide` on the pending decision's own
  `answer_field` (`energy_amount`, `life_amount`) with its `generation`; `max`
  is the decision's own `amount`.
- A `surveil` or `scry` answer goes to `/decide` as `moved`: the cards looked
  at that `to_graveyard` (surveil) or `to_bottom` (scry) matches, with the
  rest kept on top in the order they were looked at. An answer naming neither
  moves nothing, so every card stays on top.
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

## Guide edits: proposed by PR, merged by the owner (2026-09-13)

`mcp-seat guides propose` renders and commits a matchup `play_plan`/`notes`
edit plus `NOTES.md`, pushes the proposer's own branch,
`seat/<game-id>-<deck>-<role>-guides` (role `seat` or `advisor`, so two seats
or a seat and its buddy never share one), and opens a pull request
against mtg-decks main, returning at once with `awaiting_owner_review` and the
PR's URL. mtg-decks is a private repo with no GitHub Actions budget, so its
`ci` and `automerge` workflows never run there: the command never polls for
checks, automerge, or merge state, and the guide edit is not live until the
owner reviews and merges the PR by hand. Play scripts are unchanged:
`mcp-seat plays admit` still pushes straight to mtg-decks main after the
in-pod gate (commit, rebase, gate, push, retries, a PR only as a rare
fallback after repeated non-fast-forward races).

## Not in the vocabulary

Attack, block, mulligan, concede, pass, and any iteration field.
