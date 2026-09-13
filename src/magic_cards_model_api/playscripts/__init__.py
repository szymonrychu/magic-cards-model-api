"""Play scripts: declarative chains a seat runs, checked before they ever run.

- `schema` - the closed, bounded pydantic model (design D2) and its coded errors.
- `selectors` - a pure match of characteristics against one CardView.
- `conditions` - a pure evaluation of a script's `true`/`false` over the
  redacted state view only.
- `inputs` - `decks/<slug>/plays/inputs.json` and the built-in
  `opposing_archetype`.
- `lint` - the corpus gate behind `mcp-seat plays check`, sharing its codes,
  input vocabulary and conflict warnings verbatim with mtg-decks'
  `scripts/check_plays.py`. A machine-read string equal to a card name is
  refused as `card-name-in-field` in a phrase too, basic land names included
  (`oracle_has: "Island"`); a subtype selector (`subtypes_any: ["Island"]`)
  is the intended form.
- `admit` - `mcp-seat plays priority` and `mcp-seat plays admit`: a guides
  clone's play-script change, gated and ratcheted, onto mtg-decks main.
- `guides_propose` - `mcp-seat guides propose`: a matchup plan edit and its
  regenerated guides as a pull request.
- `gitwork` - the git and GitHub REST plumbing both share.
- `report` - the compact per-script run table a handover prompt carries.
"""
