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
  `counters {name, op, n}`; `ability_text_has`; `oracle_has`.
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
`unknown-decision-kind`. Three of them are in the set but refused with
`forbidden-kind`, because scripts never attack, block or mulligan.

- Decision kinds: `choose_a_mode`, `choose_a_quality`, `choose_cards`, `choose_targets`, `declare_attackers`, `declare_blockers`, `may`, `mulligan`, `opening_hand_action`, `order_triggers`, `pay_energy`, `pay_energy_amount`, `pay_life`, `pay_life_amount`, `pay_mana`, `put_on_top_or_bottom`, `scry`, `surveil`
- Never answered: `declare_attackers`, `declare_blockers`, `mulligan`

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

## Not in the vocabulary

Attack, block, mulligan, concede, pass, and any iteration field.
