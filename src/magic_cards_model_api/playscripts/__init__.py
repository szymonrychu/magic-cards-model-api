"""Play scripts: declarative chains a seat runs, checked before they ever run.

- `schema` - the closed, bounded pydantic model (design D2) and its coded errors.
- `selectors` - a pure match of characteristics against one CardView.
- `conditions` - a pure evaluation of a script's `true`/`false` over the
  redacted state view only.
- `inputs` - `decks/<slug>/plays/inputs.json` and the built-in
  `opposing_archetype`.
- `lint` - the corpus gate behind `mcp-seat plays check`, sharing its codes,
  input vocabulary and conflict warnings verbatim with mtg-decks'
  `scripts/check_plays.py`.
"""
