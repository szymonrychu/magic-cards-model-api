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

export function pin(pinned, id, color) {
  return [...pinned.filter((p) => p.card_id !== id), { card_id: id, color }];
}

export function unpin(pinned, id) {
  return pinned.filter((p) => p.card_id !== id);
}

export function manaSources(pinned, untapped) {
  const pinnedIds = new Set(pinned.map((p) => p.card_id));
  return [
    ...pinned.map((p) => ({ card_id: p.card_id, colors: [p.color] })),
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
