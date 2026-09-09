// Where a choice goes, and what is in it - as pure functions over plain
// objects, no DOM, so `tests/test_ui_menus.py` can ask them directly the way
// `board_layout.js` and `app.js`'s `bootTarget` already are. `app.js` acts on
// what these decide; `render/ribbon.js`'s `showPopover` draws it.
//
// The rule the whole file exists to enforce: **interacting with the board
// must not change the board's geometry**. A choice is drawn *over* the
// layout, never inserted into it, so nothing reflows, nothing moves under
// the cursor, and the page never grows a scrollbar it did not have.
//
// Two kinds of choice, two surfaces, and they are deliberately not the same:
//
//   * A choice *about a card* - which way to cast it, which of its
//     abilities, which colour a land taps for, which player an attacker is
//     aimed at - is a short list, picked once, and meaningless away from the
//     card it belongs to. It gets a menu anchored over that card
//     (`"popover"`). It is drawn on top of the board, so the row underneath
//     it keeps its size and its cards keep their place.
//   * A choice *about the turn* - a pending decision, the pregame
//     mulligan/opening-hand questions, the combat prompt - is a question the
//     game is asking this seat, not a property of any one card. It stays in
//     the one prompt box that already asks everything (`"prompt"`), where
//     the player's eye already is between clicks.
//
// The in-flight announcement - modes, X, alternative costs, targets,
// division - is neither, and the interesting case. It reads as card-scoped,
// but it cannot be a menu over the card: it has several steps, and it has to
// stay on screen while the player clicks *other* cards on the board to fill
// its target slots. An anchored menu closes on the first click-away, so it
// would close on the first target. It gets the control ribbon's own panel
// (`"ribbon"`) - beside the board, never inside it. That placement is the
// fix for "choices are rendered under the cards": the panel used to be a row
// of `#mtg-board` between the lands and the hand, so beginning a cast pushed
// every card above it.
//
// No Comprehensive Rules number is cited in this file. Nothing in the CR
// says where a client draws its menus, and the session that wrote this had
// no `knowledge_search` to verify one with - `ribbon.js` and `style.css`
// carry the numbers this repo did verify for the announcement sequence and
// the pregame.

/** The surface a choice of this kind is presented on: `"popover"` (anchored
 * over the card it is about), `"ribbon"` (the control ribbon's staging
 * panel), `"prompt"` (the one prompt box), or `null` for a kind this file
 * does not know - which is a bug in the caller, not a default to fall back
 * on silently. */
export function surfaceForChoice(kind) {
  if (
    kind === "hand_actions" ||
    kind === "abilities" ||
    kind === "mana_ability" ||
    kind === "mana_colour" ||
    kind === "defender"
  ) {
    return "popover";
  }
  if (kind === "announcement") return "ribbon";
  if (kind === "decision" || kind === "pregame" || kind === "combat" || kind === "game_over") return "prompt";
  return null;
}

/** The choice a click opens, given `app.js`'s own
 * `planHandCardClick`/`planPermanentClick` plan - `{kind, surface}`, or
 * `null` when the click just acts (playing a land, picking a target,
 * cancelling) and asks nothing.
 *
 * Both plan functions call their multi-option case `"menu"`; a hand card's
 * carries `specials`/`cycling`, a permanent's carries `abilities`, so the
 * two are told apart by shape rather than by a second plan kind.
 *
 * A `"pin"` or `"attack"` plan is listed here as the popover it *would* use.
 * Whether one actually opens is the caller's own count of the options: a
 * land with one colour, or an attack with one defender, has nothing to ask
 * and acts immediately. A pin can ask twice - `"mana_ability"` first when
 * the source has more than one mana ability, then `"mana_colour"` if the
 * chosen one still offers a choice of colour - and both are the same
 * popover over the same card, which is why the plan names only the second. */
export function choiceForPlan(plan) {
  const kind = plan && plan.kind;
  if (kind === "menu") {
    const which = Array.isArray(plan.abilities) ? "abilities" : "hand_actions";
    return { kind: which, surface: surfaceForChoice(which) };
  }
  if (kind === "pin") return { kind: "mana_colour", surface: surfaceForChoice("mana_colour") };
  if (kind === "attack") return { kind: "defender", surface: surfaceForChoice("defender") };
  if (kind === "cast" || kind === "activate" || kind === "crew") {
    return { kind: "announcement", surface: surfaceForChoice("announcement") };
  }
  return null;
}

function item(key, label, sublabel) {
  return { key, label, sublabel: sublabel || "" };
}

/** One menu item for one ability, keyed `<prefix>:<its own index>`.
 *
 * An activated ability is printed "cost: effect", so its cost is usually the
 * first thing its own text already says - and repeating it underneath is the
 * ugliness the browser pass caught on Walking Ballista, whose menu read
 * "Remove a +1/+1 counter from this creature: It deals 1 damage to any
 * target." with "Remove a +1/+1 counter from this creature" a second time in
 * dim type below it. The cost is kept as a sublabel only when the text does
 * not already open with it. */
function abilityItem(prefix, ability) {
  const text = ability.text || "";
  const cost = (ability.cost || "").trim();
  const repeated = Boolean(cost) && text.trimStart().startsWith(cost);
  return item(`${prefix}:${ability.index}`, text, repeated ? "" : cost);
}

/** The items a hand card's menu offers, in order: the ordinary thing first
 * (play it, or cast it), then every special action, then cycling. `key` is
 * what `app.js` switches on - nothing here fires a request or touches the
 * DOM. */
export function handMenuItems(plan, card) {
  const specialActions = (card && card.special_actions) || {};
  const items = [plan.isLand ? item("play", "Play") : item("cast", "Cast")];
  for (const keyword of plan.specials || []) {
    const offer = specialActions[keyword] || {};
    items.push(item(`special:${keyword}`, keyword[0].toUpperCase() + keyword.slice(1), offer.cost));
  }
  for (const offer of plan.cycling || []) items.push(item("cycle", "Cycle", offer.cost));
  return items;
}

/** The items a permanent's menu offers: one per activated ability, keyed by
 * the ability's own index (not its position in this list - a permanent can
 * offer ability 0 and ability 2 with nothing offerable between them), then
 * Crew last for a Vehicle. */
export function abilityMenuItems(plan) {
  const items = (plan.abilities || []).map((ability) => abilityItem("activate", ability));
  if (plan.vehicle) items.push(item("crew", "Crew"));
  return items;
}

/** One item per mana ability a source being pinned mid-cast could be tapped
 * for, keyed by the ability's own index - what reaches the engine as
 * `ManaSource.ability_index`.
 *
 * The label is the ability's whole printed text, restriction sentence
 * included, because that sentence is the entire difference between the two:
 * Eldrazi Temple's index 0 adds {C} and its index 1 adds {C}{C} that "may
 * only be spent on Eldrazi spells", and a menu that showed anything shorter
 * would offer the player two items it could not tell apart. */
export function manaAbilityMenuItems(abilities) {
  return (abilities || []).map((ability) => abilityItem("ability", ability));
}

/** One item per colour a pinned mana source could be tapped for. */
export function manaMenuItems(colors) {
  return (colors || []).map((color) => item(`colour:${color}`, `Tap for {${color}}`));
}

/** One item per legal defender an attacker can be aimed at - the opponent,
 * and any planeswalker or battle they control. */
export function defenderMenuItems(defenders) {
  return (defenders || []).map((defender) => item(`defender:${defender.id}`, `Attack ${defender.label}`));
}

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

/**
 * Where an anchored menu goes, in **viewport** coordinates - what
 * `position: fixed` wants, which is the whole point.
 *
 * The bug this replaced: the menu was `position: absolute` and placed at the
 * anchor's document coordinates with no bound at all. Every card in the hand
 * row sits at the bottom of the window, so its menu was placed below the
 * bottom of the window, the document grew to contain it, and the page got a
 * vertical scrollbar it did not have a moment earlier - the owner's exact
 * complaint. Nothing about a menu can do that here: the returned box is
 * always inside `viewport`, and `maxHeight` caps it so a long list scrolls
 * inside itself rather than pushing anything.
 *
 * `anchor` and `menu` are the two measured rectangles (`anchor` from
 * `getBoundingClientRect`, `menu` from the same on the populated menu);
 * `viewport` is `{width, height}`. Returns `{left, top, maxHeight,
 * placement}` - `placement` is `"below"` or `"above"`, purely so the caller
 * can style the menu's pointer edge.
 *
 * Preferring below and flipping above is the ordinary desktop-menu rule: a
 * menu opens away from the edge it would otherwise fall off. When neither
 * side fits the whole list it takes the roomier side and scrolls.
 */
export function anchoredPosition(anchor, menu, viewport, opts) {
  const gap = (opts && opts.gap) !== undefined ? opts.gap : 4;
  const margin = (opts && opts.margin) !== undefined ? opts.margin : 8;
  const spaceBelow = viewport.height - margin - (anchor.bottom + gap);
  const spaceAbove = anchor.top - gap - margin;
  const fitsBelow = spaceBelow >= menu.height;
  const below = fitsBelow || spaceBelow >= spaceAbove;
  const room = Math.max(0, below ? spaceBelow : spaceAbove);
  const maxHeight = Math.min(menu.height, room);
  const top = below ? anchor.bottom + gap : anchor.top - gap - maxHeight;
  // A menu wider than the window itself would otherwise clamp to a negative
  // range; `Math.max` keeps `left` at the margin instead of past it, and the
  // menu's own `max-width` (`style.css`) is what actually keeps it narrow
  // enough for the case to be theoretical.
  const widest = Math.max(margin, viewport.width - margin - menu.width);
  return {
    left: clamp(anchor.left, margin, widest),
    top: clamp(top, margin, Math.max(margin, viewport.height - margin - maxHeight)),
    maxHeight,
    placement: below ? "below" : "above",
  };
}
