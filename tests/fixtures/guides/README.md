# Vendored slice of the deck-guides corpus

Copied verbatim from `github.com/szymonrychu/mtg-decks` at commit `67b580e`
(`67b580efc509086bf05a61089db2a7e8c3e72f62`), the same corpus a seat Job
clones to `/guides`. That commit is on the mtg-decks **`decks-landing`
branch**, local-only and not pushed to `origin` at the time of this refresh:
it carries the opening-plays DSL work (`o-opening-plays`, merged into
`decks-landing` locally) that is not on mtg-decks `main` yet, so the pin
names `decks-landing` until that work lands on `main`, and moves to the
`main` commit that carries it at the next refresh after. Not hand-written and
not trimmed: `tests/driver/test_matchup_card.py` asserts a byte budget on
what `matchup_card.render_matchup_card` produces, and a budget measured
against an approximation of the input is worth nothing.

Refreshed again from `d681d42` to `67b580e`: one file changed,
`decks/instant-reanimator/plays/bin-fatty-vs-permanent-exile.json`, rewritten
upstream to use the new `activate.discard` field (a battlefield ability's own
hand-discard cost, e.g. Psychic Frog's "Discard a card:") in place of the
`choose_cards`-answer workaround the play used before that field existed -
see this api repo's own `playscripts/schema.py`/`runtime.py` for the engine
side of the same change. Every other file is byte-identical to `d681d42`.

Refreshed from the previous pin (`3772971`): every `decklist.txt` and
`archetype.json` in the corpus is byte-identical at `d681d42`, `meta/card-
data.json` is unchanged, and only three guided decks' `boarding-plan.json`
changed (`boros-aggro`, `dimir-frog`, `prison-tron` - the last one
extensively, apparently reformatted upstream rather than grown). The real
change is `plays/`: the corpus grew from 41 plays to **100**, adding a
family of `open-*` opening plays across all thirteen decks (previously only
`boros-aggro` and the six guided decks had more than a handful), plus
`instant-reanimator/plays/inputs.json` (the first deck in the corpus to
declare one).

What is here, and why only this much:

- **Six guided decks** - a `decklist.txt` and a full
  `analysis/*/domain3-matchup-threat/` run each - chosen as two different
  worst cases:
  - `decks/{orzhov-martyr,prison-tron,dimir-frog}` are the **three largest
    boarding plans** in the corpus: 141730, 120469 and 112536 bytes. That is
    the densest INPUT the renderer is handed, which is the worst case for the
    trim ladder.
  - `decks/{living-end,affinity}` are the two that **render largest**, which
    is a different pair, and the worst case for the hard cap. Plan size does
    not predict card size: the ladder bounds the card, so what comes out is
    set by how many matchups the plan covers and how many of those archetypes
    have an `archetype.json` to quote signature cards from, not by how long
    the plan's prose is. The three largest plans render 7th, 8th and 4th,
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
- `analysis/*/domain3-matchup-threat/matchups/*.json` - **two purposes, two
  different counts**:
  - The two largest per guided deck, so `matchups_dir` resolves and as the
    evidence for the size the briefing prose quotes for the "one small read
    once the opponent shows cards" path. `render_matchup_card` never opens
    these. The largest file in the corpus is in the slice
    (`living-end/.../matchups/devoted-combo.json`, 28492 bytes), which is
    where that prose's "28 KB at the largest" comes from.
  - Every matchup key an `opposing_archetypes` or `opposing_archetype`-input
    value in the vendored `plays/` corpus actually names, for whichever deck
    names it - `mcp-seat plays check`'s `unknown-archetype-key`/
    `invalid-input-value` checks read the deck's own `analysis/*/domain3-
    matchup-threat/matchups/*.json` file STEMS as its catalog of valid keys
    (`playscripts/inputs.newest_matchup_keys`), and a deck whose plays name
    more archetypes than the "two largest" rule alone vendors is a false
    positive against the real corpus, not a real lint defect. This is why
    `dimir-frog` carries five matchup files (the two largest plus
    `boros-aggro`, `boros-energy`, `ur-cutter-prowess`) and `living-end`
    carries nine (plus `45c-aggro`, `boros-aggro`, `boros-energy`, `dimir-
    control`, `esper-blink`, `esper-control`, `ur-cutter-prowess`) rather than
    two. `esper-blink` and `eldrazi-tron` are not guided decks here (no
    `decklist.txt`), but their own `open-*` plays name almost every archetype
    in the format, so each carries its full real `analysis/` matchup set (15
    files) rather than a hand-picked subset - anything less would still be a
    slice-only false positive, not a genuine gap. See "Refreshing" below for
    how to find which keys a refresh needs.
- `decks/*/plays/*.json` - **every play script in the corpus**, for every
  deck that has any (all thirteen at `d681d42`, 100 plays), including the
  seven decks that are otherwise only an `archetype.json` here. Plays are
  part of the slice so `mcp-seat plays check --root tests/fixtures/guides`
  lints the real corpus rather than a hand-built root; at `d681d42`, with the
  matchup-file additions above, it reports 100 plays in 13 decks with zero
  errors and zero warnings.
  `tests/test_playscript_end_to_end.py` also registers these plays through
  the API and runs every one of them against the real engine.
- `decks/*/plays/inputs.json` - part of the slice wherever a deck declares
  one. `instant-reanimator` is the only one at `d681d42`.
- `meta/card-data.json` - the card index the play lint's name and phrase rules
  read from the guides root. Without it `plays check` cannot run over this
  slice at all.

## What the card actually renders to

Measured with the code at this commit, over **all thirteen** guides of
`git archive d681d42`, and quoted for the corpus root at `~/Documents/mtg-decks`
(`/Users/szymonri/Documents/mtg-decks`; the archive was extracted elsewhere and
its raw figures shifted by twice the difference in root length, the relation
`test_a_raw_card_size_moves_by_exactly_twice_the_corpus_root` pins). Both tables
were re-measured at this pin. Three guides' figures moved (`prison-tron`,
`dimir-frog` and `instant-reanimator`/`bloodchief-combo`/`eldrazi-ramp` shifted
a little from their own `boarding-plan.json` or `GAMEPLAN_GUIDE.md` edits at
this refresh - see the per-deck diff noted above); the rest are unchanged.
None of the thirteen is truncated and none loses a field; the hard cap is
12288 bytes.

| guide | boarding plan | rendered card |
|---|---|---|
| `living-end` | 107974 | 11566 |
| `affinity` | 97592 | 11490 |
| `instant-reanimator` | 94646 | 11402 |
| `prison-tron` | 120469 | 11357 |
| `bloodchief-combo` | 97781 | 11320 |
| `eldrazi-ramp` | 100628 | 11190 |
| `orzhov-martyr` | 141730 | 10982 |
| `dimir-frog` | 112536 | 10934 |
| `boros-aggro` | 92126 | 10560 |
| `prowess` | 84271 | 10486 |
| `devoted-combo` | 90751 | 10427 |
| `esper-blink` | 90096 | 10068 |
| `eldrazi-tron` | 108530 | 10043 |

So "the three largest guides" resolves three ways, and they are three
different sets: the largest **rendered cards** are `living-end` 11566,
`affinity` 11490 and `instant-reanimator` 11402; the largest **boarding
plans** are `orzhov-martyr` 141730, `prison-tron` 120469 and `dimir-frog`
112536; the largest **`GAMEPLAN_GUIDE.md`** - the read the card exists to
replace - are `orzhov-martyr` 72799, `living-end` 64890 and `prison-tron`
59804. The card replaces a 59-73 KB read with ~11 KB of turn-0 context.

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
| `dimir-frog` | 10664 |
| `boros-aggro` | 10286 |

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

If it reports `unknown-archetype-key` or `invalid-input-value` on an
`opposing_archetype`/`opposing_archetypes` value, that is almost always the
matchups bullet above, not a corpus defect: `newest_matchup_keys` reads a
deck's valid archetype keys off the matchup file STEMS actually vendored, so
a play naming an archetype key this slice did not vendor a matchup file for
reads as unknown here even though it lints clean in the real corpus (which
carries the deck's whole real matchup set). Find exactly which keys a deck's
own vendored plays need with:

```python
import json, glob

needed = set()
for f in glob.glob("tests/fixtures/guides/decks/<slug>/plays/*.json"):
    if f.endswith("inputs.json"):
        continue
    doc = json.load(open(f))
    for lst in ("true", "false"):
        for c in doc.get(lst, []):
            if c.get("kind") == "input" and c.get("name") == "opposing_archetype":
                v = c["value"]
                needed.update(v if isinstance(v, list) else [v])
    needed.update(doc.get("opposing_archetypes", []))
needed.discard("*")
```

then diff that against the deck's vendored `matchups/*.json` stems and copy
whichever real files (`git -C <mtg-decks> show <sha>:<path>`, never fabricated)
are missing - from the deck's own `analysis/` run if it is guided, or a fresh
`analysis/<run>/domain3-matchup-threat/matchups/` directory holding just the
needed files (or, if a deck's plays name nearly every archetype, the whole
real set - `esper-blink` and `eldrazi-tron` both needed all 15 at this
refresh) if it is not.

The play-script conflict corpus, `tests/fixtures/conflict_corpus.json`, is
**not** part of this slice and is not refreshed with it: it is a copy of
mtg-decks' `scripts/tests/fixtures/conflict_corpus.json`, pinned by its own
hash in `tests/test_playscript_conflict_corpus.py`, and is synced separately.
It did not change between `3772971` and `d681d42`.

## Plays ahead of the pin

Empty as of this refresh. `ahead-of-pin/decks/<slug>/plays/*.json` holds play
scripts a test reads that are newer than `MTG_DECKS_SHA`, each copied verbatim
from the mtg-decks commit named in this section with `git -C <mtg-decks> show
<sha>:<path>`, never from a working checkout, and kept outside `decks/` on
purpose: the seed-play tests glob and register `decks/*/plays/*.json` as the
pinned corpus, and a newer play there would change that corpus (and duplicate
the id a test registers on top of it). No test reads a sibling mtg-decks
checkout (`tests/test_playscript_end_to_end.py::test_no_vendored_play_id_
disagrees_with_itself_across_locations` fails if a play is ever vendored
twice with different bytes across `decks/`, the driver mirror and this
directory). `boros-aggro/plays/open-fetch-unlocks.json` used to live here
(vendored from mtg-decks `f5b86bc`, ahead of the `3772971` pin); the pin
refreshed to `d681d42` now carries that same play id, with newer content (a
Marsh Flats regression fix, `unlocks_by_search` narrowed to
`unlocks_by_basic_search`) inside `decks/boros-aggro/plays/` itself, so the
ahead-of-pin copy was removed and `tests/test_playscript_end_to_end.py`
now reads the pinned copy like every other play. This directory stays empty
until a future in-flight play needs to jump ahead of the next pin again.
