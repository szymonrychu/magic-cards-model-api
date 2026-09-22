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
 * popover over the same card, which is why the plan names only the second.
 *
 * **Every plan that ACTS opens a popover, single option and all** - playing
 * a land, casting, activating the one ability a permanent has, and using a
 * dynamic play permission (`"permission"`: a flashback grant, a card exiled
 * and made playable "this turn" - a cast begun out of exile or a graveyard,
 * wired to the board's own `#permitted-row` and zone ribbon as much as to
 * the zones modal) all used to fire on the click itself, and now open the
 * same anchored picker a multi-ability permanent already opened. Cascade's
 * hit and suspend's own third ability no longer grant one of these (battle
 * 8): accepting either casts the card immediately while the permitting
 * spell or ability is still resolving, so what shows up for them instead is
 * an ordinary `pending_decision` - see `render/decision_controls.js`. A
 * one-item menu IS the confirm:
 * there is no separate confirm component and no Cancel item, because
 * `render/ribbon.js`'s `showPopover` already leaves on Escape, on a click
 * away, and on picking something. What that item then begins - a cast or an
 * activation staging in the ribbon (`"announcement"`, still what `"crew"`
 * names, since crew is only ever picked out of a Vehicle's own menu) - is
 * the step after this one, not what the click opened.
 *
 * A plan that SELECTS is deliberately still `null`: picking a target,
 * cancelling, toggling a blocker into the combat draft. A confirm on a
 * target pick would make a three-target spell a nine-click affair. So is
 * `"none"` - a card with nothing to do stays a silent no-op rather than
 * opening a dialog to say so. */
export function choiceForPlan(plan) {
  const kind = plan && plan.kind;
  if (kind === "menu") {
    const which = Array.isArray(plan.abilities) ? "abilities" : "hand_actions";
    return { kind: which, surface: surfaceForChoice(which) };
  }
  if (kind === "land" || kind === "cast") {
    return { kind: "hand_actions", surface: surfaceForChoice("hand_actions") };
  }
  if (kind === "activate") return { kind: "abilities", surface: surfaceForChoice("abilities") };
  if (kind === "permission") {
    return { kind: "hand_actions", surface: surfaceForChoice("hand_actions") };
  }
  if (kind === "pin") return { kind: "mana_colour", surface: surfaceForChoice("mana_colour") };
  if (kind === "attack") return { kind: "defender", surface: surfaceForChoice("defender") };
  if (kind === "crew") return { kind: "announcement", surface: surfaceForChoice("announcement") };
  return null;
}

/** One menu item. `match` is the row this item would be in the engine's own
 * `GET .../legal-actions` answer - `{card_id, kind, ability_index}`, the
 * three fields that identify an `Action` - or `null` for an item that has no
 * such row (a colour to tap for, a defender to aim at, Crew, which is not an
 * `ActionKind` at all). `enrichItems` is the only reader. */
function item(key, label, sublabel, match) {
  return { key, label, sublabel: sublabel || "", match: match || null };
}

/** The `Action` row a click on one of this card's activated abilities would
 * take. A mana ability is a different `ActionKind` from an ordinary one
 * (`activate_mana`, its own engine entry point), and the enumeration says so
 * for every one of them - an item that claimed plain `activate` for a mana
 * ability would silently match nothing. No Comprehensive Rules number is
 * restated here; see this file's header. */
function abilityMatch(cardId, ability) {
  if (!cardId) return null;
  return {
    card_id: cardId,
    kind: ability.is_mana_ability ? "activate_mana" : "activate",
    ability_index: ability.index,
  };
}

/** What a named Cast item prints under its label: the face's own cost. The
 * cast hints' `mana_cost` is that face's for a double-faced card (CR 712.8a
 * - `affordances.cast_hints` reads the front face when the printing's
 * top-level cost is blank), never the combined line. */
function castCost(card) {
  return (card && card.cast && card.cast.mana_cost) || "";
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
function abilityItem(prefix, ability, match) {
  const text = ability.text || "";
  const cost = (ability.cost || "").trim();
  const repeated = Boolean(cost) && text.trimStart().startsWith(cost);
  return item(`${prefix}:${ability.index}`, text, repeated ? "" : cost, match);
}

/** The items a hand card's menu offers, in order: the ordinary thing first
 * (play it, or cast it - one item per castable face when the card offers a
 * choice of face, and a land face beside them when it has one), then every
 * special action, then cycling. `key` is what `app.js` switches on - nothing
 * here fires a request or touches the DOM.
 *
 * Also the whole menu for a plain `{kind: "land"}` or `{kind: "cast"}` plan,
 * which carries no `specials`/`cycling` of its own: every loop runs zero or
 * one times and what comes back is the single Play or Cast item that IS the
 * confirm for a click that used to fire straight away.
 *
 * **Both halves of a two-headed card, each named.** A modal double-faced
 * card offers its front face as a cast and its land face as a play (CR
 * 712.12), and a split or adventurer card offers one cast per castable face
 * (CR 709.3, CR 715.3). An option is labelled with the face's own name only
 * when there is more than one head to tell apart - a plain Memnite's item is
 * still "Cast", which is what a one-option confirm should say.
 *
 * Each special action's keyword is its own `ActionKind` in the engine's
 * enumeration (`plot`, `foretell`, `suspend`), so the match needs no table.
 * Cycling is an `activate_off_zone`, and its `ability_index` is the one the
 * board view printed on the offer. */
export function handMenuItems(plan, card, actingFor) {
  const items = ownHandMenuItems(plan, card);
  if (!actingFor) return items;
  // CR 723.5: the card is another player's, played by this seat for them.
  return items.map((entry) => ({
    ...entry,
    sublabel: entry.sublabel ? `${entry.sublabel} - for ${actingFor}` : `for ${actingFor}`,
  }));
}

function ownHandMenuItems(plan, card) {
  const specialActions = (card && card.special_actions) || {};
  const cardId = card && card.id;
  const match = (kind, abilityIndex, extra) =>
    cardId
      ? { card_id: cardId, kind, ability_index: abilityIndex ?? null, ...(extra || {}) }
      : null;
  const isLand = plan.kind === "land" || Boolean(plan.isLand);
  const spellFaces = plan.spellFaces || [];
  const landFace = plan.landFace || null;
  // Name the faces only when there is a second head to tell this one from.
  const named = spellFaces.length > 1 || Boolean(landFace);
  const frontName = ((card && card.faces) || [])[0]?.name || (card && card.name) || "";
  const items = [];
  if (!isLand) {
    if (spellFaces.length > 1) {
      // The engine's own `spell_face` string, keyed and sent untransformed
      // (`CastRequest.spell_face`). The faceless entry of an adventurer
      // card's pair (CR 715.3's "play the card normally") keys as the plain
      // `cast` every single-faced card already uses.
      for (const face of spellFaces) {
        const name = face.spell_face || null;
        items.push(
          item(
            name ? `cast:${name}` : "cast",
            `Cast ${face.name}`,
            face.mana_cost,
            match("cast", null, name ? { spell_face: name } : null)
          )
        );
      }
    } else {
      items.push(
        item(
          "cast",
          named ? `Cast ${frontName}` : "Cast",
          named ? castCost(card) : "",
          match("cast")
        )
      );
    }
  }
  // CR 712.12's land face, beside the cast rather than instead of it. The
  // `play_land` row the engine emits for it carries `Action.land_face`, so
  // the item names it and `enrichItems` can tell the two rows of a card with
  // a land front AND a land face apart.
  if (isLand || landFace) {
    items.push(
      item(
        "play",
        landFace ? `Play ${landFace}` : "Play",
        "",
        match("play_land", null, landFace ? { land_face: landFace } : null)
      )
    );
  }
  for (const keyword of plan.specials || []) {
    const offer = specialActions[keyword] || {};
    items.push(
      item(
        `special:${keyword}`,
        keyword[0].toUpperCase() + keyword.slice(1),
        offer.cost,
        match(keyword)
      )
    );
  }
  for (const offer of plan.cycling || []) {
    items.push(item("cycle", "Cycle", offer.cost, match("activate_off_zone", offer.index)));
  }
  return items;
}

/** The whole menu a dynamic play permission opens: one Cast item, which IS
 * the confirm for a click that used to begin the cast outright.
 *
 * Captioned with the source that granted it - a card in exile can be there
 * for several reasons at once, and "Cast / Bloodbraid Elf" says which one is
 * offering this. The caption is what `enrichItems` replaces when the engine
 * reports the cost cannot be paid, so an unaffordable option says why
 * instead.
 *
 * The row it names is the one `legal_actions` emits for a permission cast:
 * the card, plain `cast`, and no ability index. A permission that names no
 * findable card matches nothing rather than half a row - the same `null` the
 * Crew item carries.
 *
 * A permission granted as a land PLAY (an exiled land the granting effect
 * lets you put onto the battlefield) is NOT a separate item here, because
 * `app.js` has no request shape for it either - `actions/play-land` sends no
 * `from_zone`. Nothing about that changes with this menu; it is the same one
 * option the click already had. */
export function permissionMenuItems(permission) {
  const card = (permission && permission.card) || {};
  const caption = (permission && permission.granted_by) || "";
  const match = (spellFace) =>
    card.id
      ? {
          card_id: card.id,
          kind: "cast",
          ability_index: null,
          ...(spellFace ? { spell_face: spellFace } : {}),
        }
      : null;
  // CR 709.3/CR 715.3 again, and the zone the cast begins in changes nothing
  // about it: a split card made playable out of exile still has a half to
  // choose. Same `cast:<face>` keys the hand menu uses, so `app.js` routes
  // both through one branch.
  const faces = (card.cast && card.cast.spell_faces) || [];
  if (faces.length > 1) {
    return faces.map((face) =>
      item(
        face.spell_face ? `cast:${face.spell_face}` : "cast",
        `Cast ${face.name}`,
        caption,
        match(face.spell_face || null)
      )
    );
  }
  return [item("cast", "Cast", caption, match(null))];
}

/** The items a permanent's menu offers: one per activated ability, keyed by
 * the ability's own index (not its position in this list - a permanent can
 * offer ability 0 and ability 2 with nothing offerable between them), then
 * Equip (CR 702.6a) for an Equipment the engine's own legal-actions answer
 * currently offers it on, then Crew last for a Vehicle.
 *
 * A one-ability permanent's plan is `{kind: "activate", abilityIndex}` and
 * names no list at all, because the click it comes from used to activate
 * immediately. The ability is read off `card` here so that click opens the
 * same menu with one item in it - and the item says which ability it is
 * about, which an "are you sure?" box could not. It is keyed by the plan's
 * own `abilityIndex`, which is exactly the number `beginActivate` has always
 * been handed.
 *
 * `plan.equipment` is deliberately NOT "this card's type line says
 * Equipment" - equip has no entry in `Game.activated_abilities` (there is no
 * ability index to activate it by), so the only way to know it is legal
 * *right now* is the engine's own `GET .../legal-actions` answer, unlike
 * Crew, which is always offered and lets a dry run refuse it. Equip's own
 * timing restriction (CR 702.6a "activate only as a sorcery") makes the
 * engine drop the row entirely rather than mark it unaffordable
 * (`action_rows.legal_action_rows`), so the item must not be offered at all
 * outside a main phase - `app.js`'s `openPermanentMenu` sets this flag from
 * that answer, never from the card alone. The match names `ability_index:
 * null`, the same shape `action_rows.is_equip_row` requires of the row. */
export function abilityMenuItems(plan, card) {
  const cardId = card && card.id;
  const abilities = plan.kind === "activate" ? soleAbility(plan, card) : plan.abilities || [];
  const items = abilities.map((ability) =>
    abilityItem("activate", ability, abilityMatch(cardId, ability))
  );
  if (plan.equipment) {
    items.push(
      item("equip", "Equip", "", cardId ? { card_id: cardId, kind: "equip", ability_index: null } : null)
    );
  }
  if (plan.vehicle) items.push(item("crew", "Crew"));
  return items;
}

/** One legal-actions candidate line
 * (`"<uuid> <name> (<zone>, <controller>)"`, `affordances.candidate_line`'s
 * own format), split into the id `POST .../actions/equip` takes as `target`
 * and the name a menu shows. The id is the first whitespace-delimited token
 * - "the whole uuid, never a prefix" per that function's own docstring - so
 * this only ever needs to split on the first space, never parse a uuid
 * shape itself. */
export function parseCandidateLine(line) {
  const text = line || "";
  const spaceIndex = text.indexOf(" ");
  if (spaceIndex < 0) return { id: text, name: text };
  const id = text.slice(0, spaceIndex);
  const rest = text.slice(spaceIndex + 1);
  const parenIndex = rest.lastIndexOf(" (");
  const name = parenIndex >= 0 ? rest.slice(0, parenIndex) : rest;
  return { id, name };
}

/** One item per candidate an equip row's own `targets.slots[0].candidates`
 * names (CR 702.6a's "target creature you control") - equip has no
 * `/affordances/{card_id}` entry point of its own (there is no ability
 * index to ask it about), so the legal-actions row's own candidate lines
 * are the only source `app.js`'s `beginEquip` has for what it may legally
 * attach to. Picking one is the whole of the announcement - equip carries no
 * modes, no X, and exactly one target (CR 301.5c) - so the item's own key
 * is what `fireEquip` posts as `target`. */
export function equipTargetMenuItems(lines) {
  return (lines || []).map((line) => {
    const { id, name } = parseCandidateLine(line);
    return item(`target:${id}`, `Equip ${name}`);
  });
}

/** The one ability an `{kind: "activate", abilityIndex}` plan is about, as a
 * one-entry list in `plan.abilities`' own shape - empty if the card does not
 * carry it, so a menu with nothing in it opens nothing rather than an empty
 * box. */
function soleAbility(plan, card) {
  const ability = ((card && card.abilities) || [])[plan.abilityIndex];
  return ability ? [{ ...ability, index: plan.abilityIndex }] : [];
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

/** Fold `GET .../legal-actions` into a menu that is ALREADY on screen: an
 * option the engine reports it cannot pay for is disabled and captioned with
 * the engine's own `unaffordable_reason`, and everything else comes back
 * exactly as it went in (same items, same order, nothing dropped).
 *
 * A caption rather than a filter, and that is the engine's own choice
 * restated here: an unpayable cost does not make an action illegal, so
 * `legal-actions` lists it either way (`app.py`'s `get_legal_actions` and
 * the `Affordability` model carry the rule number this repo verified for
 * that). The menu keeps offering it and says why it will not work, instead
 * of leaving the player to guess where the option went. No Comprehensive
 * Rules number is restated in this file - see its header.
 *
 * Three answers, kept distinct. `"no"` is the only one that disables
 * anything: `"unknown"` is a real, separate verdict (a modal or X spell
 * before its parameters are announced) and folding it into `"no"` would grey
 * out an option the player can in fact take. An item with no matching row at
 * all - Crew, or an answer that simply does not mention it - is left alone
 * for the same reason: silence is not a refusal.
 *
 * A single item can match several rows (evoke: one card, one `cast` kind,
 * the printed cost and the alternative one). It is unaffordable only when
 * EVERY matching row says so - the menu's one Cast item begins the
 * announcement where that alternative cost is chosen.
 *
 * The second thing a row carries is its `unreadable_modes`
 * (`action_rows._Rows.name_unreadable_modes`, off `engine_api`'s own reading
 * - the same one that refuses the cast): the modes of a modal spell this
 * engine cannot read into an effect. `render/ribbon.js` already draws those
 * on the mode chips, but only once the announcement has been begun; this
 * answer arrives with the click that opens the menu, which is the earlier of
 * the two places and the one the row publishes for. A caption for exactly
 * the reason an unpayable cost is one - a card with one unreadable mode is
 * still castable with its others - and never a `disabled`: that is reserved
 * for the engine saying no. */
export function enrichItems(items, actions) {
  const rows = actions || [];
  return (items || []).map((entry) => {
    const matching = rows.filter((row) => matchesAction(entry.match, row));
    if (!matching.length) return entry;
    const unreadable = unreadableModesOf(matching);
    const unaffordable = matching.every((row) => row.affordable === "no");
    // CR 723.5: a row that is another player's, offered because this seat is
    // making their decisions this turn (`ActionRow.acting_for`).
    const actingFor = matching.map((row) => row.acting_for).find(Boolean);
    if (!unaffordable && !unreadable.length && !actingFor) return entry;
    const enriched = { ...entry };
    if (actingFor) enriched.sublabel = `for ${actingFor}`;
    if (unaffordable) {
      const reason = matching.map((row) => row.unaffordable_reason).find(Boolean);
      enriched.disabled = true;
      enriched.sublabel = reason || entry.sublabel;
    }
    if (unreadable.length) {
      // The tooltip carries every notice in full either way; the one-line
      // caption goes to whichever fact is actually stopping the cast, so an
      // unpayable row keeps saying so.
      enriched.title = unreadableModeNotices(unreadable);
      if (!unaffordable) enriched.sublabel = unreadableModeCaption(unreadable);
    }
    return enriched;
  });
}

/** Every `UnreadableModeView` the matching rows name, deduplicated by
 * `mode_number` and in the card's own printed order - the numbers
 * `CastRequest.modes` takes. Several rows can describe the same card (the
 * printed cost and an alternative one), and they name the same modes. */
function unreadableModesOf(rows) {
  const byNumber = new Map();
  for (const row of rows) {
    for (const mode of (row && row.unreadable_modes) || []) {
      if (mode && !byNumber.has(mode.mode_number)) byNumber.set(mode.mode_number, mode);
    }
  }
  return [...byNumber.values()].sort((a, b) => a.mode_number - b.mode_number);
}

/** The dim one-liner under the option: which numbered modes are the ones
 * that cannot be chosen. */
function unreadableModeCaption(modes) {
  const numbers = modes.map((mode) => mode.mode_number);
  const named = numbers.length === 1 ? `mode ${numbers[0]}` : `modes ${numbers.join(", ")}`;
  return `${named} cannot be read by this engine`;
}

/** The option's tooltip: the engine's own reason for each of them, one per
 * line and in its own words - `unreadable_mode_notice` already names the
 * card, the mode number and the printed sentence, so nothing is prefixed to
 * it here. A row whose notice is missing still says what the cast would do,
 * the same fallback sentence `render/ribbon.js`'s mode chips print. */
function unreadableModeNotices(modes) {
  return modes
    .map(
      (mode) =>
        mode.notice ||
        `mode ${mode.mode_number} cannot be read by this engine, so a cast choosing it is refused`
    )
    .join("\n");
}

/** The fields that identify one `Action`: which card, which entry point,
 * and - where the engine emits more than one row for the same pair - which
 * ability, which cast face (CR 709.3/CR 715.3, Wear // Tear is two `cast`
 * rows on one card) or which land face (CR 712.12). Everything else on the
 * row (its rule, its cost, its target slots) describes the action rather
 * than naming it.
 *
 * An item that names no face matches only a row that names none, which is
 * what keeps a split card's two options from both reading the other half's
 * affordability. A single-faced card's rows and items name neither, so they
 * pair exactly as they always did. */
function matchesAction(match, action) {
  if (!match || !action) return false;
  return (
    match.card_id === action.card_id &&
    match.kind === action.kind &&
    (match.ability_index ?? null) === (action.ability_index ?? null) &&
    (match.spell_face ?? null) === (action.spell_face ?? null) &&
    (match.land_face ?? null) === (action.land_face ?? null)
  );
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
