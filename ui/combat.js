// Attacking (CR 508) and blocking (CR 509) view models. The engine still
// decides what is legal - `actions/attack` and `actions/declare-blockers`
// refuse anything CR 508.1a/CR 509.1a would - so these are just candidate
// lists and draft-toggling for the screen; nothing here enforces a rule.

export function attackCandidates(state) {
  return (state.battlefield || []).filter(
    (c) =>
      c.controller === state.you &&
      !c.tapped &&
      !c.summoning_sick &&
      /creature/i.test(c.type_line || "")
  );
}

export function opponentOf(state) {
  return (state.players || []).map((p) => p.username).find((u) => u !== state.you) || null;
}

export function defenders(state) {
  const opponent = opponentOf(state);
  const list = opponent ? [{ id: opponent, label: opponent }] : [];
  for (const c of state.battlefield || []) {
    if (c.controller !== state.you && /planeswalker|battle/i.test(c.type_line || "")) {
      list.push({ id: c.id, label: c.name });
    }
  }
  return list;
}

export function toggleAttacker(draft, cardId, defenderId) {
  const next = { ...draft };
  if (next[cardId] === defenderId) delete next[cardId];
  else next[cardId] = defenderId;
  return next;
}

export function attackBody(draft) {
  return { attackers: Object.entries(draft).map(([card_id, defender]) => ({ card_id, defender })) };
}

export function attacksAgainstYou(state) {
  return ((state.combat && state.combat.attacks) || []).filter(
    (a) =>
      a.defender === state.you ||
      (state.battlefield || []).some((c) => c.id === a.defender && c.controller === state.you)
  );
}

export function blockCandidates(state) {
  return (state.battlefield || []).filter(
    (c) => c.controller === state.you && !c.tapped && /creature/i.test(c.type_line || "")
  );
}

export function toggleBlock(draft, blockerId, attackerId) {
  const next = { ...draft };
  if (next[blockerId] === attackerId) delete next[blockerId];
  else next[blockerId] = attackerId;
  return next;
}

export function blockBody(draft, you) {
  return {
    player: you,
    blocks: Object.entries(draft).map(([blocker, blocked]) => ({ blocker, blocked })),
  };
}
