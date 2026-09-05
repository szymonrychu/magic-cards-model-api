// The board itself: opponent info/hand/battlefield, the shared stack, the
// red zone (attackers, shown only while there are any), your battlefield,
// your hand, and your own info row. One `mountBoard(root, store, handlers)`
// subscribes to the store and keeps `#opp-info`/`#opp-hand`/
// `#opp-battlefield`/`#stack-row`/`#red-zone`/`#my-battlefield`/`#my-hand`/
// `#you-info` patched via `dom.js`'s keyed `patchChildren`, so a card kept
// alive across a poll tick does not lose a hover preview or a mid-flight
// transition.
//
// Per-card options are derived from the one in-flight `action` (`actions.js`)
// or, when a `choose_targets` decision (CR 601.2c-style targeting, but for a
// decision rather than a cast) is the thing being answered, from
// `decisionTargets` instead - never both at once, since only one of the two
// is ever live for this seat.

import { cardNode, updateCardNode, cardBackNode } from "../cards.js";
import { h, patchChildren, clear, setText } from "../dom.js";
import { activeSlotIndex, candidateIds } from "../targeting.js";
import { manaPoolNodes } from "../symbols.js";

// Exported (Task 17's addition to this render module) so `render/panels.js`'s
// zones popover can highlight a legal graveyard/exile target with the exact
// same targetable/chosen/dimmed logic the board itself uses, rather than a
// second copy of it.
export function targetingOpts(app, id) {
  const action = app.action;
  const decision =
    app.decisionDraft && app.state.pending_decision && app.state.pending_decision.player === app.state.you
      ? app.decisionTargets
      : null;
  const slots = decision ? decision.slots : action ? action.slots : [];
  const aff = decision ? decision.affordance : action ? action.affordance : null;
  const picking = Boolean(aff && aff.targets_anything && slots.length);
  const active = picking ? activeSlotIndex(slots) : -1;
  const legal = picking && active >= 0 ? candidateIds(aff, slots[active]) : new Set();
  const held = slots.filter((s) => s.targets.includes(id)).length;
  return {
    picking,
    targetable: legal.has(id),
    chosen: held > 0,
    heldCount: held,
    dimmed: picking && !legal.has(id) && held === 0,
  };
}

function permanentOpts(app, card) {
  const opts = targetingOpts(app, card.id);
  const attacks = (app.state.combat && app.state.combat.attacks) || [];
  opts.attacking = Boolean(
    (app.combatDraft && app.state.step === "declare_attackers" && app.combatDraft[card.id]) ||
      attacks.some((a) => a.attacker === card.id)
  );
  opts.blocking = Boolean(
    (app.combatDraft &&
      Object.keys(app.combatDraft).includes(card.id) &&
      app.state.step === "declare_blockers") ||
      attacks.some((a) => (a.blockers || []).includes(card.id))
  );
  opts.previewTap = Boolean(
    app.preview && app.preview.tapped && app.preview.tapped.some((t) => t.card_id === card.id)
  );
  opts.pinned = Boolean(app.action && app.action.pinned.some((p) => p.card_id === card.id));
  opts.lifted = Boolean(app.action && app.action.cardId === card.id);
  return opts;
}

function patchCards(container, cards, app, size, onClick) {
  patchChildren(
    container,
    cards,
    (c) => c.id,
    (card) => cardNode(card, size, {}),
    (el, card) => {
      const opts = permanentOpts(app, card);
      updateCardNode(el, card, opts);
      // `cards.js`'s `updateCardNode` toggles the classes it knows about from
      // its own `toggles()` table; `lifted` (the card mid-drag into a cast)
      // is board-only state, so it is toggled here instead of asking
      // `cards.js` to carry a class it has no other use for.
      el.classList.toggle("lifted", opts.lifted);
      if (opts.heldCount > 1) {
        let badge = el.querySelector(".held-count");
        if (!badge) {
          badge = h("span", { class: "held-count" });
          el.appendChild(badge);
        }
        setText(badge, `x${opts.heldCount}`);
      } else {
        const badge = el.querySelector(".held-count");
        if (badge) badge.remove();
      }
      el.onclick = (event) => onClick(card, event);
    }
  );
}

function infoRow(el, app, player) {
  const opts = targetingOpts(app, player.username);
  el.classList.toggle("targetable", opts.targetable);
  el.classList.toggle("chosen", opts.chosen);
  el.classList.toggle("dimmed", opts.dimmed);
  el.classList.toggle("active", app.state.active_player === player.username);
  el.classList.toggle("priority", app.state.priority_holder === player.username);
  clear(el);
  el.appendChild(h("span", { class: "name" }, player.username));
  el.appendChild(h("span", { class: "life", title: "life" }, String(player.life)));
  el.appendChild(
    h(
      "span",
      { class: "counts" },
      `hand ${player.hand_count} / library ${player.library_count} / graveyard ${player.graveyard_count}`
    )
  );
  // `manaPoolNodes` already returns its own `span.pool` - wrapping it in a
  // second one nested `.pool` inside `.pool` for no reason.
  el.appendChild(manaPoolNodes(app.symbols, player.mana_pool));
  if (opts.heldCount > 1) el.appendChild(h("span", { class: "held-count" }, `x${opts.heldCount}`));
}

/**
 * Mount the board region into `root` (the element containing the ids listed
 * below - normally `#game`) and return the `store.subscribe` unsubscribe
 * function. `handlers = {onHandCard(card), onPermanent(card, event),
 * onPlayer(username), onStackObject(obj), onLand(card)}`; `onLand` is not
 * called from here (a land click is a `onHandCard`/`onPermanent` click the
 * caller distinguishes by `card.type_line`) but is accepted so callers can
 * pass one handlers object across every render module.
 */
export function mountBoard(root, store, handlers) {
  const $ = (sel) => root.querySelector(sel);
  const els = {
    oppInfo: $("#opp-info"),
    oppHand: $("#opp-hand"),
    oppBf: $("#opp-battlefield"),
    stack: $("#stack-row"),
    redZone: $("#red-zone"),
    myBf: $("#my-battlefield"),
    myHand: $("#my-hand"),
    youInfo: $("#you-info"),
  };
  els.oppInfo.onclick = () => handlers.onPlayer(store.get().opponent);
  els.youInfo.onclick = () => handlers.onPlayer(store.get().state.you);
  return store.subscribe((app) => {
    const state = app.state;
    if (!state) return;
    const you = state.you;
    const me = state.players.find((p) => p.username === you);
    const opp = state.players.find((p) => p.username !== you);
    infoRow(els.oppInfo, app, opp);
    infoRow(els.youInfo, app, me);
    patchChildren(
      els.oppHand,
      Array.from({ length: opp.hand_count }, (_, i) => ({ id: `back-${i}` })),
      (c) => c.id,
      () => cardBackNode(),
      () => {}
    );
    patchCards(els.oppBf, state.battlefield.filter((c) => c.controller !== you), app, "card-md", handlers.onPermanent);
    patchCards(els.myBf, state.battlefield.filter((c) => c.controller === you), app, "card-md", handlers.onPermanent);
    patchCards(els.myHand, state.hands[you] || [], app, "card-lg", handlers.onHandCard);
    patchChildren(
      els.stack,
      state.stack,
      (o) => o.id,
      () => h("div", { class: "stack-object" }, h("span", { class: "name" }), h("span", { class: "targets" })),
      (el, obj) => {
        const opts = targetingOpts(app, obj.id);
        el.classList.toggle("targetable", opts.targetable);
        el.classList.toggle("chosen", opts.chosen);
        setText(el.querySelector(".name"), `${obj.name}${obj.kind === "ability" ? " (ability)" : ""}`);
        setText(el.querySelector(".targets"), (obj.targets || []).length ? `-> ${obj.targets.join(", ")}` : "");
        el.onclick = () => handlers.onStackObject(obj);
      }
    );
    const attacking = state.battlefield.filter(
      (c) =>
        (app.combatDraft && app.state.step === "declare_attackers" && app.combatDraft[c.id]) ||
        (state.combat.attacks || []).some((a) => a.attacker === c.id)
    );
    els.redZone.hidden = attacking.length === 0;
    patchCards(els.redZone, attacking, app, "card-sm", handlers.onPermanent);
  });
}
