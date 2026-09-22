// One player controlling another's turn (CR 723), as the page reads it.
//
// CR 723.5: while one player controls another (Emrakul, the Promised End),
// the controller "makes all choices and decisions the controlled player is
// allowed to make". CR 723.3 leaves the controlled player holding priority
// and still the active player, so `priority_holder` and
// `pending_decision.player` name them while the move is the controller's.
// `players[].decisions_made_by` (`serialize._player_view`) says who is
// really deciding; every "is this mine" check on the page asks here instead
// of comparing a name with `state.you`. The twin of `seatkit.decides_for`.

/** Every player whose priority or pending decision is this viewer's move:
 * itself (unless somebody else is deciding for it) plus every player it is
 * deciding for. */
export function decidesFor(state) {
  const you = state && state.you;
  const names = new Set();
  if (!you) return names;
  let controlled = false;
  for (const block of state.players || []) {
    if (!block || typeof block.username !== "string") continue;
    const decider = block.decisions_made_by || block.username;
    if (block.username === you && decider !== you) controlled = true;
    if (decider === you) names.add(block.username);
  }
  if (!controlled) names.add(you);
  return names;
}

/** Whether the priority held right now is this viewer's to act on. */
export function holdsPriority(state) {
  const holder = state && state.priority_holder;
  return Boolean(holder) && decidesFor(state).has(holder);
}

/** Whether `pending` is this viewer's to answer. */
export function decisionIsMine(state, pending) {
  return Boolean(pending) && decidesFor(state).has(pending.player);
}

/** Whether somebody else is making this viewer's decisions this turn. */
export function isControlled(state) {
  const you = state && state.you;
  const control = (state && state.player_control) || {};
  return Boolean(you && control[you] && control[you] !== you);
}

/** The player whose move this viewer is making right now: the one a pending
 * decision it answers names, else the priority holder it acts for, else
 * itself. What "your creatures", "you are attacked" and "your turn" mean
 * while it decides for somebody else. */
export function actingAs(state) {
  const pending = state && state.pending_decision;
  if (decisionIsMine(state, pending)) return pending.player;
  if (holdsPriority(state)) return state.priority_holder;
  return state ? state.you : null;
}
