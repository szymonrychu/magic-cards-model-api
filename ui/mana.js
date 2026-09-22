// Mana payment from the browser's side. The default is the same as the MCP
// and CLI seats: offer every untapped mana source and let the engine's
// `auto_tap` (CR 601.2g) choose. A pinned source is the player's explicit
// choice - Sunbaked Canyon for {R}, not {W} - and goes first as a
// `ManaSource` with a colour (schemas.ManaSource).

/** `player` defaults to the viewer; a spell cast for a player this seat is
 * controlling names that player's sources instead (CR 723.5a). */
export function untappedSources(state, player) {
  const you = player || state.you;
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
 *
 * `abilityIndex` may equally be `null`, and is for every permanent with a
 * single mana ability - there is nothing to choose, so nothing is named and
 * the request keeps exactly the shape it had before this argument existed.
 * It is a number only when the player picked one of *several* mana
 * abilities: Eldrazi Temple's {C}{C} that may only be spent on Eldrazi
 * spells is its index 1, and taps for something else entirely at index 0,
 * so "which source" is not enough to say what the player meant.
 */
export function pin(pinned, id, color, abilityIndex) {
  const record = { card_id: id, color: color ?? null, ability_index: abilityIndex ?? null };
  return [...pinned.filter((p) => p.card_id !== id), record];
}

export function unpin(pinned, id) {
  return pinned.filter((p) => p.card_id !== id);
}

export function manaSources(pinned, untapped) {
  const pinnedIds = new Set(pinned.map((p) => p.card_id));
  return [
    // Pinned first, in whichever shape each pin earned - `manaSource` below
    // decides, and the case that stays a bare id is the reason pinning a
    // single-colour source (or one whose `mana_options` came back `null`, an
    // ability with only one option) never invents a colourless `"C"` choice
    // for a land that produces coloured mana.
    ...pinned.map(manaSource),
    ...untapped.filter((id) => !pinnedIds.has(id)),
  ];
}

/** The wire shape one pin goes out as. A pin that named neither an ability
 * nor a colour is a bare id - the engine's `auto_tap` (CR 601.2g) solves it
 * exactly as it solves any other untapped source - and one that named only a
 * colour is the `{card_id, colors}` `ManaSource` it always was. Only a pin
 * that named *which* ability grows `ability_index`, so nothing about the
 * common case changes shape.
 *
 * The colour goes out **once**, whatever the ability adds: it is the choice
 * the player made (CR 605.1a's `color`), not a count. Shang-Chi's "add two
 * mana of any one color" is pinned as `colors: ["R"]` and the server fills in
 * the ability's own count (`engine_api._expanded_to_the_abilitys_own_count`),
 * because that count is nowhere on `AbilityView` for this side to read and
 * every other client would have to guess at it too. */
function manaSource(p) {
  const index = p.ability_index ?? null;
  if (index === null) return p.color ? { card_id: p.card_id, colors: [p.color] } : p.card_id;
  const source = { card_id: p.card_id };
  if (p.color) source.colors = [p.color];
  source.ability_index = index;
  return source;
}

/** `card`'s mana abilities, each carrying its own `index` - the index
 * `list_abilities` (`engine_api.py`) stamped it with, which is what
 * `ActivateRequest.ability_index` and `ManaSource.ability_index` mean, and
 * not its position among the mana abilities. A permanent can perfectly well
 * have a printed activated ability ahead of the mana one, and Eldrazi Temple
 * has two mana abilities that are not interchangeable. */
export function manaAbilities(card) {
  return ((card && card.abilities) || [])
    .map((ability, position) => ({ ...ability, index: ability.index ?? position }))
    .filter((ability) => ability.is_mana_ability);
}

/** The ability `abilityIndex` names - by its own `index` field, falling back
 * to the position for a payload that carries none. Same reading as
 * `menus.js`'s `abilityMenuItems`, which already keys its items by
 * `ability.index` rather than by where the entry sits. */
function abilityAt(card, abilityIndex) {
  const abilities = (card && card.abilities) || [];
  return abilities.find((a) => a && a.index === abilityIndex) || abilities[abilityIndex];
}

export function colorOptions(card, abilityIndex) {
  const ability = abilityAt(card, abilityIndex);
  const options = (ability && ability.mana_options) || [];
  const seen = [];
  for (const option of options) for (const color of option) if (!seen.includes(color)) seen.push(color);
  return seen;
}
