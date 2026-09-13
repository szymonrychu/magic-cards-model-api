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
- Condition kinds (7): `count`, `zone_size`, `life`, `stack_top`,
  `land_drop_available`, `untapped_mana`, `input` (an `in`/`not_in` value list
  holds 0-32 values).

## Moves

Move kinds (4), each with `answers: [0-3]` (an `answer` move needs 1-3):

- `activate {source: {zone, select}, ability_text_has, sacrifice?: source|none, answers: [0-3]}`
- `cast {card: {zone, select}, cost?: <cast cost>, targets: [0-6 {select, side}], mana?: auto, answers: [0-3]}`
- `play_land {card: {select}, answers: [0-3]}`
- `answer {answers: [1-3]}`, for a decision already pending

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

Answer item: `{decision, pick?: {select, count?, prefer?: [0-4 select]}, accept?: bool, to_graveyard?: select, to_bottom?: select}`.

`decision` is a closed set equal to the api's pending decision kinds: the
values of `decisions.ANSWERABLE_KINDS`, which are the engine `DecisionKind`s
the api can pend. A value outside it (a typo such as `choose_cardz`) is
`unknown-decision-kind`. Three of them are in the set but refused with
`forbidden-kind`, because scripts never attack, block or mulligan.

- Decision kinds: `choose_a_mode`, `choose_a_quality`, `choose_cards`, `choose_targets`, `declare_attackers`, `declare_blockers`, `may`, `mulligan`, `opening_hand_action`, `order_triggers`, `pay_energy`, `pay_energy_amount`, `pay_life`, `pay_life_amount`, `pay_mana`, `put_on_top_or_bottom`, `scry`, `surveil`
- Never answered: `declare_attackers`, `declare_blockers`, `mulligan`

## Not in the vocabulary

Attack, block, mulligan, concede, pass, and any iteration field.
