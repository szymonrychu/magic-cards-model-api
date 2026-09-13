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
  first, 0 = disabled), `opposing_archetypes` (1-32: `["*"]` alone, or matchup
  label slugs; `*` beside a key is `wildcard-with-keys`),
  `when {whose_turn: mine|theirs|either, steps: [1-4 distinct autopass steps,
  not declare_attackers/declare_blockers], stack: empty|any}` (a repeated step
  is `duplicate-step`), `true` (0-8 conditions), `false` (0-8 conditions, each
  must NOT hold), `moves` (1-4), `next` (0-3 ids).
- Selector (all fields optional, combined with AND), evaluated on the face a
  card has in its current zone: `types_all`, `types_any`, `subtypes_any`,
  `supertypes_any`, `colors_any`, `keywords_any` (each 0-15 words);
  `mana_value {op, n}`; `power {op, n}`; `toughness {op, n}`; `tapped`;
  `counters {name, op, n}`; `ability_text_has`; `oracle_has`;
  `missing_from {zones: [1-7 zone], side: me|opponent, by: subtype|name}`.
- `colors_any` reads the card's `colors` by name or letter, any case, from a
  closed vocabulary: the five colors and `colorless`. `colorless` (or `C`)
  matches a card whose color set is empty (CR 105.2c, "A colorless object has
  no color"), a devoid Eldrazi included, and never a colored card.
- Colors: `white`, `blue`, `black`, `red`, `green`, `colorless`
- Condition kinds (7): `count`, `zone_size`, `life`, `stack_top`,
  `land_drop_available`, `untapped_mana`, `input` (an `in`/`not_in` value list
  holds 0-32 values).

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

## Moves

Move kinds (4), each with `answers: [0-3]` (an `answer` move needs 1-3):

- `activate {source: {zone, select}, ability_text_has, targets: [0-6 <target>], sacrifice?: source|none|<sacrifice choice>, answers: [0-3]}`
- `cast {card: {zone, select}, cost?: <cast cost>, targets: [0-6 <target>], mana?: auto, answers: [0-3]}`
- `play_land {card: {select}, answers: [0-3]}`
- `answer {answers: [1-3]}`, for a decision already pending

A target is `{select, side}` (a card or permanent matching `select`, on that
side) or `{player: me|opponent}`, one per target the spell or ability
announces, in order. Anything else is `malformed-play`, and a key beside
either shape is `unknown-field`.

A sacrifice choice is `{select, count?: >=1, prefer?: [0-4 select]}`: the
permanents the activating player controls matching `select`, for an
activation cost's own "Sacrifice a ..." piece (Goblin Bombardment's "Sacrifice
a creature"). `count` left out is the cost's own number. `source` sacrifices
the ability's own source, `none` nothing.

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

Answer item: `{decision, pick?: {select, count?, prefer?: [0-4 select]}, accept?: bool, to_graveyard?: select, to_bottom?: select, amount?: >=0|max}`.

An answer's fields must fit its decision kind, or it is
`answer-shape-mismatch`. A pay-amount decision (Galvanic Discharge's
`pay_energy_amount`, Necrodominance's `pay_life_amount`) is answered with
`amount` alone: an integer from 0, or `max` for the most the decision allows.
`accept`, `pick`, `to_graveyard` or `to_bottom` on it, or no `amount`, is
`answer-shape-mismatch`, and so is an `amount` on any other kind. A negative
`amount` is `bounds-exceeded`; any other value is `malformed-play`.

- Amount decision kinds: `pay_energy_amount`, `pay_life_amount`

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
  different cards still tied after `prefer` are `ambiguous`.
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

## Not in the vocabulary

Attack, block, mulligan, concede, pass, and any iteration field.
