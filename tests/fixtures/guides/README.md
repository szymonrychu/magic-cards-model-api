# Vendored slice of the deck-guides corpus

Copied verbatim from `github.com/szymonrychu/mtg-decks` at commit `3772971`
(`3772971c530309dbd79efbbd856e45b19e1e3da3`), the same corpus a seat Job
clones to `/guides`. That commit is on the mtg-decks **`script-dsl`
integration branch**, not on `main`: it carries the play scripts that are
not on mtg-decks `main` yet, so the pin names the
integration branch until that branch merges, and moves to the `main` commit
that carries it at the next refresh after. Not hand-written and not
trimmed: `tests/driver/test_matchup_card.py` asserts a byte budget on what
`matchup_card.render_matchup_card` produces, and a budget measured against an
approximation of the input is worth nothing. Every file the previous pin
(`67f9663`) vendored outside `plays/` is byte-identical at `3772971`
except `devoted-combo/archetype.json` (a wording fix to the combo's `why`,
naming Vizier of Remedies as the counter-stopper and dropping the Tyvar
claim); the refresh re-copied the twenty-four plays that changed and
replaced each tron deck's three `map-for-tron-*` plays with one
`map-for-tron-missing-piece` play, a net four fewer plays (45 -> 41), in
both slices.

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
- `decks/*/plays/*.json` - **every play script in the corpus**, for every
  deck that has any (all thirteen at `3772971`, 41 plays), including the
  seven decks that are otherwise only an `archetype.json` here. Plays are
  part of the slice so `mcp-seat plays check --root tests/fixtures/guides`
  lints the real corpus rather than a hand-built root; at `3772971` it
  reports 41 plays in 13 decks with zero errors and zero warnings.
  `tests/test_playscript_end_to_end.py` also registers these plays through
  the API and runs every one of them against the real engine.
- `decks/*/plays/inputs.json` - part of the slice wherever a deck declares
  one. No deck does at `3772971`, so none is vendored; a refresh copies each
  one that appears.
- `meta/card-data.json` - the card index the play lint's name and phrase rules
  read from the guides root. Without it `plays check` cannot run over this
  slice at all.

## What the card actually renders to

Measured with the code at this commit, over **all thirteen** guides of
`git archive 3772971`, and quoted for the corpus root at `~/Documents/mtg-decks`
(`/Users/szymonri/Documents/mtg-decks`; the archive was extracted elsewhere and
its raw figures shifted by twice the difference in root length, the relation
`test_a_raw_card_size_moves_by_exactly_twice_the_corpus_root` pins). Both tables
were re-measured at this pin and every figure is unchanged from the previous
one: the refresh changed only plays and one archetype.json wording fix,
neither of which the card reads. None of the thirteen is truncated and
none loses a field; the hard cap is 12288 bytes.

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
`decklist.txt`, and each deck's `plays/*.json` (and `plays/inputs.json` where
present) at `<slug>/plays/`, byte-identical to the ones here. Two copies
because the two suites want different shapes - a guide root with real analysis
runs here, a flat per-slug corpus there - so a refresh has to re-copy both.
`test_guides.py`'s `MTG_DECKS_SHA` is the pin they share.

Do not edit these files. They are generated in the source repo from the same
`boarding-plan.json` the rendered markdown guides come from; refresh them by
re-copying, and re-measure both tables above afterwards. Copy from the commit,
never from a working checkout: `git -C <mtg-decks> show <sha>:<path>` per file
(or `git archive <sha>`), so an uncommitted edit over there cannot leak in.
Then set `MTG_DECKS_SHA` to the full SHA and run
`mcp-seat plays check --root tests/fixtures/guides --json`, which must report
no errors and no warnings.

The play-script conflict corpus, `tests/fixtures/conflict_corpus.json`, is
**not** part of this slice and is not refreshed with it: it is a copy of
mtg-decks' `scripts/tests/fixtures/conflict_corpus.json`, pinned by its own
hash in `tests/test_playscript_conflict_corpus.py`, and is synced separately.
