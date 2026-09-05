// Mana payment from the browser's side. The default is the same as the MCP
// and CLI seats: offer every untapped mana source and let the engine's
// `auto_tap` (CR 601.2g) choose. A pinned source is the player's explicit
// choice - Sunbaked Canyon for {R}, not {W} - and goes first as a
// `ManaSource` with a colour (schemas.ManaSource).

export function untappedSources(state) {
  const you = state.you;
  return (state.battlefield || [])
    .filter(
      (card) =>
        card.controller === you &&
        !card.tapped &&
        (card.abilities || []).some((a) => a.is_mana_ability)
    )
    .map((card) => card.id);
}

/**
 * `color` may be `null` (`affordances.py`'s `ability_hints` reports
 * `mana_options: null` for a one-option ability, so `colorOptions` returns
 * `[]` and there is no single colour to pin) - a pinned source with no
 * colour still names *which* source the player chose, and leaves *what
 * colour it makes* to the engine's own `auto_tap`, the same "bare id" shape
 * an ordinary untapped source already has (see `manaSources` below).
 */
export function pin(pinned, id, color) {
  return [...pinned.filter((p) => p.card_id !== id), { card_id: id, color: color ?? null }];
}

export function unpin(pinned, id) {
  return pinned.filter((p) => p.card_id !== id);
}

export function manaSources(pinned, untapped) {
  const pinnedIds = new Set(pinned.map((p) => p.card_id));
  return [
    // A pin with a colour becomes a `ManaSource` (the player's explicit
    // choice); a pin with none is sent as a bare id, same as an ordinary
    // untapped source, so the engine's `auto_tap` picks the colour - this is
    // what lets pinning a single-colour source (or one whose `mana_options`
    // came back `null`, an ability with only one option) still name that
    // source first without inventing a colourless `"C"` choice for a land
    // that produces coloured mana.
    ...pinned.map((p) => (p.color ? { card_id: p.card_id, colors: [p.color] } : p.card_id)),
    ...untapped.filter((id) => !pinnedIds.has(id)),
  ];
}

export function colorOptions(card, abilityIndex) {
  const ability = ((card && card.abilities) || [])[abilityIndex];
  const options = (ability && ability.mana_options) || [];
  const seen = [];
  for (const option of options) for (const color of option) if (!seen.includes(color)) seen.push(color);
  return seen;
}
