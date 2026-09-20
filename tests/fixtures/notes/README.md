# Real `NOTES.md` passages, for the content gate's two directions

`playscripts/content_gate` is a heuristic over prose. A test that feeds it
prose written for the test proves only that the test author and the gate
author agree with each other, which is the failure mode that makes a gate
like this look green and refuse real work in production. So the passages
here are **real**: cut verbatim out of `NOTES.md` files a human already
accepted into the deck-guides corpus, and used as the fixtures for
`tests/test_guides_content_gate.py`.

Copied from `github.com/szymonrychu/mtg-decks` at commit `67b580e`
(`67b580efc509086bf05a61089db2a7e8c3e72f62`) - the same pin
`tests/fixtures/guides/README.md` names, so one refresh moves both. Each file
is a byte-exact `sed -n '<first>,<last>p'` of its source, never retyped and
never trimmed: trimming is how a "real" fixture quietly becomes a written
one. `NOTES.md` is not part of the `tests/fixtures/guides/` slice (nothing
there renders it), which is why these live in their own directory rather
than inside that corpus.

| file | source, at `67b580e` | lines | the gate should |
|---|---|---|---|
| `orzhov-martyr-clarion-conqueror.md` | `decks/orzhov-martyr/NOTES.md` | 138-152 | pass |
| `eldrazi-tron-karn-coating-lock.md` | `decks/eldrazi-tron/NOTES.md` | 52-59 | pass |
| `instant-reanimator-same-turn-rule.md` | `decks/instant-reanimator/NOTES.md` | 51-55 | pass |
| `dimir-frog-what-the-engine-will-not-measure.md` | `decks/dimir-frog/NOTES.md` | 23-45 | refuse |

The three that pass are the hardest real play prose in the corpus to get
right, not the easiest: they name an engine ("the fog loop dies with the
lifegain engine"), quote oracle text in backticks across a line break, name
a deck called Birthing Pod's colour pair, and talk about what a card "cannot
be activated" for. A gate tuned only against sentences someone wrote to be
obviously about Magic refuses all three.

The fourth is the other direction, and it is the reason clause (c) matters.
It is a section a human **accepted** into `dimir-frog/NOTES.md`, and every
line of it is about the evidence harness rather than about piloting the
deck: `--swap-in`, `evidence-spec.json`, "the fix is engine primitives".
That section is already committed in the real corpus, so the gate judging
committed text would freeze that file for ever - the deck could never take
another note. `test_guides_content_gate.py` uses it as the PRE-EXISTING
text a clean addition has to land on top of.

`tests/test_guides_content_gate.py::test_the_vendored_notes_passages_are_the_two_kinds_they_claim_to_be`
asserts that split, so a refresh that changes what these passages say fails
here rather than silently turning the clean-side tests into tests of nothing.

Refresh the same way `tests/fixtures/guides/README.md` says: from the commit
(`git -C <mtg-decks> show <sha>:<path>`), never from a working checkout, then
re-cut the line ranges above and update this table.
