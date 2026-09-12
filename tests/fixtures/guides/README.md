# Vendored slice of the deck-guides corpus

Copied verbatim from `github.com/szymonrychu/mtg-decks` at commit `936b324`,
the same corpus a seat Job clones to `/guides`. Not hand-written and not
trimmed: `tests/driver/test_matchup_card.py` asserts a byte budget on what
`matchup_card.render_matchup_card` produces, and a budget measured against an
approximation of the input is worth nothing. Every file here is byte-identical
at `936b324` and at the corpus tip `01fae6c`, so a figure measured against
either root is a figure about this slice.

What is here, and why only this much:

- **Six guided decks** - a `decklist.txt` and a full
  `analysis/*/domain3-matchup-threat/` run each - chosen as two different
  worst cases:
  - `decks/{orzhov-martyr,dimir-frog,prison-tron}` are the **three largest
    boarding plans** in the corpus: 141730, 110697 and 109669 bytes. That is
    the densest INPUT the renderer is handed, which is the worst case for the
    trim ladder.
  - `decks/{living-end,affinity}` are the two that **render largest**, which
    is a different pair, and the worst case for the hard cap. Plan size does
    not predict card size: the ladder bounds the card, so what comes out is
    set by how many matchups the plan covers and how many of those archetypes
    have an `archetype.json` to quote signature cards from, not by how long
    the plan's prose is. The three largest plans render 7th, 8th and 3rd,
    topping out at 11357 where the corpus tops out at 11566 - so a cap test
    that saw only the dense-input three was working with 209 bytes of
    headroom that is not there.
  - `decks/boros-aggro` is the sixth, and the only plan in the corpus whose
    boarding swaps name a **split card**: it writes `1 Wear`, its own
    decklist writes `Wear // Tear`, and the list the server deals
    (`decks/boros_aggro.txt`) writes `Wear/Tear`. That is the one deck on
    which the top-three swaps' intersection against the dealt list fires at
    all, so it is the one that pins the intersection being done on card faces
    rather than on raw strings.
- `decks/*/archetype.json` - **all thirteen**. The tells table reads the
  signature cards of the archetype it *names*, not of the deck being piloted,
  so a plan that lists nineteen matchups resolves against every archetype file
  in the corpus. They are ~1 KB each. A `decks/<slug>` carrying only an
  `archetype.json` is invisible to `GuideLibrary.decks()` (which requires a
  `decklist.txt`), which is what makes those seven archetype references rather
  than extra guided decks.
- Two `matchups/*.json` per guided deck: the two largest for that deck.
  `render_matchup_card` never opens these - they are here so `matchups_dir`
  resolves, and as the evidence for the size the briefing prose quotes for the
  "one small read once the opponent shows cards" path. The largest file in the
  corpus is in the slice (`living-end/.../matchups/devoted-combo.json`, 28492
  bytes), which is where that prose's "28 KB at the largest" comes from.

## What the card actually renders to

Measured with the code at this commit, over **all thirteen** guides, with the
corpus root at `~/Documents/mtg-decks` (`01fae6c`). None of the thirteen is
truncated and none loses a field; the hard cap is 12288 bytes.

| guide | boarding plan | rendered card |
|---|---|---|
| `living-end` | 107974 | 11566 |
| `affinity` | 97592 | 11490 |
| `prison-tron` | 109669 | 11357 |
| `instant-reanimator` | 94118 | 11349 |
| `bloodchief-combo` | 97781 | 11320 |
| `eldrazi-ramp` | 99679 | 11129 |
| `orzhov-martyr` | 141730 | 10982 |
| `dimir-frog` | 110697 | 10864 |
| `boros-aggro` | 89877 | 10494 |
| `prowess` | 84271 | 10486 |
| `devoted-combo` | 90751 | 10427 |
| `esper-blink` | 90096 | 10068 |
| `eldrazi-tron` | 108530 | 10043 |

So "the three largest guides" resolves three ways, and they are three
different sets: the largest **rendered cards** are `living-end` 11566,
`affinity` 11490 and `prison-tron` 11357; the largest **boarding plans** are
`orzhov-martyr` 141730, `dimir-frog` 110697 and `prison-tron` 109669; the
largest **`GAMEPLAN_GUIDE.md`** - the read the card exists to replace - are
`orzhov-martyr` 72799, `living-end` 64890 and `prison-tron` 58973. The card
replaces a 58-73 KB read with ~11 KB of turn-0 context.

### The figures the suite pins

Those numbers are root-dependent: the card prints its boarding plan's
absolute path in the header and its `matchups/` directory in the one-read
line, so a guide measures larger the deeper the checkout - exactly twice the
root's length, so 58 bytes larger under a `.claude/worktrees/...` path than
under `code/`, and 56 bytes smaller than the table above under the `/guides`
a seat Job clones to. A raw byte count is therefore not
something a test in this repo can pin, and quoting one here without saying
which root it came from is how these figures went stale in the first place.

What does not move is the card's size less the two paths it names
(`card.byte_size - 2 * len(str(guide.boarding_plan))`), and that is what
`test_the_readme_quotes_the_size_the_card_actually_renders_to` reads out of
the table below and asserts against a fresh render:

| guide | card bytes, paths excluded |
|---|---|
| `living-end` | 11296 |
| `affinity` | 11228 |
| `prison-tron` | 11083 |
| `orzhov-martyr` | 10700 |
| `dimir-frog` | 10594 |
| `boros-aggro` | 10220 |

(These run 6 bytes under the same figure from the corpus root, because the
corpus carries the `GAMEPLAN_GUIDE.md` whose size the header quotes - "the 63
KB gameplan guide" - and this slice does not.)

Nothing in the suite has to pin the corpus's true worst case for the cap to
hold: `matchup_card._capped` enforces `hard_cap_bytes` by cutting bytes, so
the cap is a property of the code rather than of the fixture, and what the
fixture pins is that real plans of corpus density - including the two that
render largest - come in under it with every required field present and
without reaching that last-resort cut.

## Refreshing

`tests/driver/fixtures/mtg-decks/` is a second slice of the same corpus at the
same commit, vendored for the classifier's table test: thirteen
`archetype.json` (byte-identical to the ones here) plus each deck's
`decklist.txt`. Two copies because the two suites want different shapes - a
guide root with real analysis runs here, a flat per-slug corpus there - so a
refresh has to re-copy both. `test_guides.py`'s `MTG_DECKS_SHA` is the pin
they share.

Do not edit these files. They are generated in the source repo from the same
`boarding-plan.json` the rendered markdown guides come from; refresh them by
re-copying, and re-measure both tables above afterwards.
