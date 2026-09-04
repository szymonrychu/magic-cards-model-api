// Per-slot targeting (CR 601.2c, CR 700.2f). One slot per requirement the
// affordance lists; a modal spell's slots are labelled by the mode they came
// from. The same object may sit in two different slots: CR 115.3 forbids a
// repeat only within one instance of the word "target", and the server's
// affordance already lists such an object under both requirements.
//
// Pure: no DOM, no fetch. `fill`/`unfill` return new arrays.

export function slotsFromAffordance(affordance) {
  const requirements = (affordance && affordance.requirements) || [];
  return requirements.map((req, index) => ({
    index,
    requirementIndex: index,
    label: req.mode ? `Mode ${req.mode}: ${req.phrase}` : req.phrase,
    count: req.unbounded ? null : req.count,
    minimum: req.minimum || 0,
    mode: req.mode || null,
    targets: [],
  }));
}

function hasRoom(slot) {
  return slot.count === null || slot.targets.length < slot.count;
}

export function activeSlotIndex(slots) {
  const needy = slots.findIndex((s) => s.targets.length < s.minimum);
  if (needy >= 0) return needy;
  const bounded = slots.findIndex((s) => s.count !== null && hasRoom(s));
  if (bounded >= 0) return bounded;
  return slots.findIndex((s) => s.count === null);
}

export function candidateIds(affordance, slot) {
  const ids = new Set();
  for (const c of (affordance && affordance.candidates) || []) {
    if ((c.requirements || []).includes(slot.requirementIndex)) ids.add(c.id);
  }
  return ids;
}

export function canFill(affordance, slot, id) {
  if (!hasRoom(slot)) return false;
  if (slot.targets.includes(id)) return false;
  return candidateIds(affordance, slot).has(id);
}

export function fill(slots, slotIndex, id) {
  const slot = slots[slotIndex];
  if (!slot || !hasRoom(slot) || slot.targets.includes(id)) return slots;
  return slots.map((s, i) => (i === slotIndex ? { ...s, targets: [...s.targets, id] } : s));
}

export function unfill(slots, slotIndex, id) {
  return slots.map((s, i) =>
    i === slotIndex ? { ...s, targets: s.targets.filter((t) => t !== id) } : s
  );
}

export function flatten(slots) {
  return slots.flatMap((s) => s.targets);
}

export function isSatisfied(slots) {
  return slots.every((s) => s.targets.length >= s.minimum);
}

export function sharesValid(shares, amount) {
  if (!Array.isArray(shares) || shares.some((n) => !Number.isInteger(n) || n < 1)) return false;
  return shares.reduce((a, b) => a + b, 0) === amount;
}
