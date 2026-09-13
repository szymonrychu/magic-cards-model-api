"""Legal-action rows as this api serves them: the engine's own enumeration,
plus what the api can say about a row that the engine's `Action` does not.

`GET .../legal-actions` and every `records` row's `legal_actions_before` are
built here, so a seat and the ledger read one answer. Four things are added,
each read off the engine rather than guessed:

- **A double-faced card's front face cost.** Scryfall leaves a `transform` or
  `modal_dfc` printing's top-level `mana_cost` blank and puts each face's cost
  on `card_faces`; CR 712.8a gives the card in hand "only the characteristics
  of its front face". `Casting.base_cost` charges that face's cost, but the
  enumeration's plain cast option reads the blank top-level field and reports
  CR 118.6's unpayable cost - Ajani, Nacatl Pariah offered as uncastable and
  cast for `{1}{W}` all the same. The row is rebuilt by the engine's own
  `_cast_action` from the cost the cast actually charges.
- **`optional_costs`** on a cast row: kicker and every other optional cost
  the engine charges (announced at CR 601.2b) - entwine and escalate on a
  modal row, harmonize's creature tap on a harmonize row - with
  whether the row's cost plus that one is payable - `CastRequest.
  optional_costs` takes the keyword back.
- **`produces`** on a mana row, resolved on this board. The engine's field is
  the printed "Add ..." phrase by design; with Urza's Mine and Urza's
  Power-Plant controlled, Urza's Tower's "add {C}{C}{C} instead" is what an
  activation adds (CR 205.3i's land types, read by `ManaAbility.effective` the
  way `Game.activate_ability` reads them). When the two differ the row states
  the board's answer and keeps the printed one as `printed_produces`.
- **`activate_first`** on a cast row the engine calls unpayable only because
  the mana it needs sits behind a mana ability costing more than `{T}`.
  CR 118.3c: "Activating mana abilities is not mandatory, even if paying a
  cost is", so auto-tap never activates one - but a seat may, and then the
  cast is payable. The activation is performed on a throwaway copy of the
  game through `engine_api.activate_ability`, the same call `POST
  .../actions/activate` makes, and the row is `affordable: yes` naming the
  activations to make first only when the engine's own payment planner then
  pays the cost.

Nothing here mutates the game it is handed.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import combinations, product
from typing import Any
from uuid import UUID

from magic_cards_model.machines.abilities import CostKind, keyword_abilities
from magic_cards_model.machines.casting import (
    _BLIGHT_PHRASE,
    granted_cast_options,
    keyword_cast_options,
    marker_cast_options,
)
from magic_cards_model.machines.cost import (
    ComponentKind,
    TotalCost,
    additional_cost_phrases,
    apply_modifications,
    mana_candidate_scan,
    plan_mana_payment,
    total_cost_for_phrase,
)
from magic_cards_model.machines.decisions import PendingDecision
from magic_cards_model.machines.keywords import equip_activation, off_zone_abilities
from magic_cards_model.machines.legal_actions import (
    _cast_action,
    _cost_affordability,
    _cost_strings,
    legal_actions,
)
from magic_cards_model.models import Action, CardState, Color, Game, Zone
from magic_cards_model.models.action import ActionKind, Affordability, TargetSlot
from magic_cards_model.models.mana import ManaCost, ManaSpend

from .engine_api import (
    activate_ability,
    dry_run_copy,
    effective_production,
    equip_target_requirements,
    unreadable_mode_notice,
    unreadable_modes,
    without_loyalty_mana_advice,
)
from .schemas import ActivateRequest

#: The printed cost pieces that never move the card they are on (CR 602.1a):
#: an ability made only of these has no CR 113.6m zone of its own to read.
_NO_ZONE_COST_KINDS = frozenset(
    {CostKind.MANA, CostKind.TAP, CostKind.UNTAP, CostKind.LOYALTY, CostKind.PAY_LIFE}
)

#: Marker for a component whose own `zone` field names where its card must be.
_COMPONENT_ZONE = object()

#: The cost components that move a chosen card out of a zone, and that zone:
#: a discard is always from hand (CR 701.9a), an exile from the component's
#: own `zone` ("Exile this card from your graveyard").
_SELF_MOVE_ZONE: dict[ComponentKind, Any] = {
    ComponentKind.DISCARD: Zone.HAND,
    ComponentKind.EXILE: _COMPONENT_ZONE,
}

#: The optional costs a keyword ability carries, each with the rule that
#: defines it and whether it may be paid more than once. One row per mechanic,
#: keyed by the keyword every card carrying it prints - the same spellings
#: `engine_api._announce_optional_costs` hands to `Casting.pay_kicker`/
#: `pay_replicate`/`pay_offspring`/`pay_gift`/`pay_harmonize`, to
#: `choose_modes(entwined=True)` and to `pay_escalate`. Blight is printed as a
#: sentence, not a keyword, and is read below. Escalate is not repeatable by
#: count: it is paid once per mode chosen beyond the first (CR 702.120a).
_OPTIONAL_COST_KEYWORDS: dict[str, tuple[str, bool]] = {
    "kicker": ("CR 702.33a", False),
    "multikicker": ("CR 702.33c", True),
    "replicate": ("CR 702.56a", True),
    "offspring": ("CR 702.175a", False),
    "gift": ("CR 702.174a", False),
    "entwine": ("CR 702.42a", False),
    "escalate": ("CR 702.120a", False),
    "harmonize": ("CR 702.180b", False),
}

#: How many sources with a costlier mana ability, and how many activation
#: combinations of them, one enumeration tries. Every trial is a deep copy of
#: the game, and a real board holds one or two such sources.
_MAX_MANUAL_TRIALS = 16


def legal_action_rows(
    game: Game, player: str, pending: PendingDecision | None
) -> tuple[list[Action], list[dict[str, Any]]]:
    """`player`'s legal actions, as engine `Action`s and as the JSON rows the
    api serves - the two lists index-aligned, so `app._attach_target_
    candidates` can still walk them together."""
    actions = legal_actions(game, player, pending=pending)
    if pending is not None:
        return actions, [action.model_dump(mode="json") for action in actions]
    rows = _Rows(game, player)
    actions = [
        rows.as_loyalty_activation(rows.with_front_face_cost(action))
        for action in actions
        if rows.functions_where_it_is(action)
    ]
    charged = [rows.with_charged_cost(action) for action in actions]
    actions = [action for action, _ in charged]
    views = [action.model_dump(mode="json") for action in actions]
    for (action, printed_cost), view in zip(charged, views, strict=True):
        if printed_cost is not None:
            view["printed_cost"] = printed_cost
        if action.kind is ActionKind.CAST:
            rows.add_optional_costs(action, view)
            rows.name_unreadable_modes(action, view)
        elif action.kind is ActionKind.ACTIVATE_MANA:
            rows.state_production(action, view)
    for action, view in rows.equip_rows():
        actions.append(action)
        views.append(view)
    for action, view in zip(actions, views, strict=True):
        if action.kind is ActionKind.CAST:
            rows.offer_manual_mana(action, view)
        if view.get("unaffordable_reason"):
            view["unaffordable_reason"] = without_loyalty_mana_advice(
                game, view["unaffordable_reason"]
            )
    return actions, views


def _symbols(colors: list[Color]) -> str:
    return "".join(f"{{{color.value}}}" for color in colors)


def front_face_cost(state: CardState) -> ManaCost | None:
    """The front face's mana cost of a card whose printing leaves the top-level
    cost blank (a `transform` or `modal_dfc` layout), or None - the same
    fallback `Casting.base_cost` makes."""
    if state.card.mana_cost:
        return None
    face = state.card.front_face
    if face is None or not face.mana_cost:
        return None
    cost = ManaCost.parse(face.mana_cost)
    return cost if cost.symbols else None


class _Rows:
    """One enumeration's worth of shared, lazily built work."""

    def __init__(self, game: Game, player: str) -> None:
        self.game = game
        self.player = player
        self._scan: list[Any] | None = None
        self._boards: list[tuple[Game, list[dict[str, Any]]]] | None = None

    @property
    def scan(self) -> list[Any]:
        if self._scan is None:
            self._scan = mana_candidate_scan(
                self.game.battlefield, game=self.game, payer=self.player
            )
        return self._scan

    def _card(self, card_id: UUID | None) -> CardState | None:
        if card_id is None:
            return None
        found = self.game.find_by_id(card_id)
        return found if isinstance(found, CardState) else None

    # -- a double-faced card's front face -------------------------------------

    def with_front_face_cost(self, action: Action) -> Action:
        if (
            action.kind is not ActionKind.CAST
            or action.mana_cost is not None
            or action.spell_face is not None
            or action.alternative_cost is not None
            or action.keyword is not None
        ):
            return action
        card = self._card(action.card_id)
        cost = front_face_cost(card) if card is not None else None
        if card is None or cost is None:
            return action
        rebuilt = _cast_action(
            self.game,
            self.player,
            card,
            rule=action.rule,
            keyword=None,
            alternative_cost=None,
            total=TotalCost.of(cost),
            candidate_scan=self.scan,
            spell_face=None,
            splice_options=action.splice_options,
        )
        if cost.x_count and not rebuilt.modes and rebuilt.x_range is None:
            # CR 107.3a: the X is on the face, where `_cast_action`'s own
            # top-level `{X}` check does not look.
            rebuilt = rebuilt.model_copy(update={"x_range": (0, None)})
        return rebuilt

    # -- equip (CR 702.6a) --------------------------------------------------------

    def equip_rows(self) -> list[tuple[Action, dict[str, Any]]]:
        """One `equip` row per Equipment this seat controls whose equip ability
        `machines.keywords.equip_activation` allows right now (its `why_not`,
        CR 602.5, "Activate only as a sorcery" included).

        The engine's enumeration has no row for equip: `Game.
        activated_abilities` does not list a keyword ability, so Blade of the
        Bloodchief offered nothing at all. The row is built from the
        activation itself - its cost, its affordability by the engine's own
        planner, and the target slot the engine's targeting parser reads off
        CR 702.6a's "Attach this permanent to target creature you control".
        `kind` is `equip`, the tail of `POST .../actions/equip`, the way every
        other row's kind names its route; the engine-side `Action` it is
        index-aligned with is an `activate` carrying `keyword: Equip`.

        The activation is built with the Equipment standing in as its own
        target: a target is needed to build one, and nothing read here (cost,
        timing, the ability's text) depends on which object it is."""
        found: list[tuple[Action, dict[str, Any]]] = []
        board: Any = None
        for permanent in self.game.permanents_controlled_by(self.player):
            if not any(
                ability.canonical.lower() == "equip"
                for ability in keyword_abilities(permanent.card)
            ):
                continue
            try:
                activation = equip_activation(self.game, permanent, permanent, self.player)
            except ValueError as error:
                action = Action(
                    kind=ActionKind.ACTIVATE,
                    player=self.player,
                    rule="CR 702.6a",
                    card_id=permanent.id,
                    from_zone=Zone.BATTLEFIELD,
                    keyword="Equip",
                    affordable=Affordability.UNKNOWN,
                    unaffordable_reason=str(error),
                )
                found.append((action, {**action.model_dump(mode="json"), "kind": "equip"}))
                continue
            if activation.why_not() is not None:
                continue
            if board is None:
                board = self.game.computed_board()
            affordable, reason, pays_with = _cost_affordability(
                self.game,
                self.player,
                activation.cost,
                self.scan,
                spend=ManaSpend.activating(board.of(permanent)),
            )
            cost, mana_cost = _cost_strings(activation.cost)
            action = Action(
                kind=ActionKind.ACTIVATE,
                player=self.player,
                rule=activation.rule,
                card_id=permanent.id,
                from_zone=Zone.BATTLEFIELD,
                keyword=activation.keyword,
                cost=cost,
                mana_cost=mana_cost,
                affordable=affordable,
                unaffordable_reason=reason,
                pays_with=pays_with,
                target_slots=tuple(
                    TargetSlot(
                        phrase=requirement.phrase,
                        minimum=requirement.minimum,
                        maximum=None if requirement.unbounded else requirement.count,
                    )
                    for requirement in equip_target_requirements(activation)
                ),
            )
            found.append((action, {**action.model_dump(mode="json"), "kind": "equip"}))
        return found

    # -- where an ability functions (CR 113.6, CR 602.2) ------------------------

    def functions_where_it_is(self, action: Action) -> bool:
        """Whether an activation row's ability functions in the zone its card
        is in, and belongs to this seat.

        CR 113.6b: "An ability that states which zones it functions in
        functions only from those zones", and CR 113.6m: "An ability whose
        cost or effect specifies that it moves the object it's on out of a
        particular zone functions only in that zone". The zone is the engine's
        own reading of the ability - `OffZoneAbility.zone` for an off-zone row,
        and the cost components `Game.activation_cost` builds for a battlefield
        row, whose self-exile or self-discard names where the card has to be.
        CR 602.2: "Only an object's controller (or its owner, if it doesn't
        have a controller) can activate its activated ability" - CR 108.4a's
        owner for a card in exile, which the engine's enumeration offers to
        every seat alike."""
        if action.kind is ActionKind.ACTIVATE_OFF_ZONE:
            card = self._card(action.card_id)
            if card is None or action.ability_index is None:
                return True
            if card.controller_or_owner not in (None, self.player):
                return False
            pool = off_zone_abilities(card.card)
            if not 0 <= action.ability_index < len(pool):
                return True
            return pool[action.ability_index].zone is card.zone
        if action.kind not in (ActionKind.ACTIVATE, ActionKind.ACTIVATE_MANA):
            return True
        permanent = self._card(action.card_id)
        if permanent is None or action.ability_index is None:
            return True
        abilities = list(self.game.activated_abilities(permanent))
        if not 0 <= action.ability_index < len(abilities):
            return True
        ability = abilities[action.ability_index]
        zone = getattr(ability, "zone", None)
        if zone is not None and zone is not permanent.zone:
            return False
        if all(
            piece.kind in _NO_ZONE_COST_KINDS for piece in getattr(ability, "costs", None) or []
        ):
            return True
        try:
            total = self.game.activation_cost(permanent, action.ability_index, self.player)
        except ValueError:
            return True
        for component in total.components:
            moved_from = _SELF_MOVE_ZONE.get(component.kind)
            if moved_from is None:
                continue
            origin = component.zone if moved_from is _COMPONENT_ZONE else moved_from
            if origin is not permanent.zone and any(
                card.id == permanent.id for card in component.cards
            ):
                return False
        return True

    # -- a loyalty ability is never a mana ability (CR 605.1a) --------------------

    def as_loyalty_activation(self, action: Action) -> Action:
        """CR 605.1a: an activated ability is a mana ability only if "it's not
        a loyalty ability". The engine's enumeration keys `ACTIVATE_MANA` on
        the ability adding mana alone, so Ugin, Eye of the Storms' `0: Add
        {C}{C}{C}` came out a mana row - it uses the stack like any other
        loyalty ability (CR 606.3)."""
        if action.kind is not ActionKind.ACTIVATE_MANA or action.ability_index is None:
            return action
        permanent = self._card(action.card_id)
        if permanent is None:
            return action
        abilities = list(self.game.activated_abilities(permanent))
        if not 0 <= action.ability_index < len(abilities):
            return action
        if getattr(abilities[action.ability_index], "loyalty", None) is None:
            return action
        return action.model_copy(update={"kind": ActionKind.ACTIVATE})

    # -- the cost the cast will charge (CR 601.2f) --------------------------------

    def with_charged_cost(self, action: Action) -> tuple[Action, str | None]:
        """The row rebuilt at the total cost the cast is charged, and the
        printed cost it replaced - or the row unchanged and `None`.

        CR 601.2f: "Some effects may increase or reduce the cost to pay". The
        engine's enumeration prices a cast at its printed (or alternative)
        mana cost; `Casting.total_cost` charges it with every board-wide
        increase, reduction and floor (`Game.static_cost_modifiers_for`) and
        the spell's own discount (`Game.cost_reduction_for`) folded in by
        `apply_modifications`. These are those same calls, so Malevolent
        Rumble under Trinisphere reads `{2}{G}` and Emrakul, the Promised End
        reads what its graveyard makes it. The spell's own discount is asked
        with no targets announced yet: a discount conditioned on a target
        (CR 601.2c comes first) is not assumed."""
        if action.kind is not ActionKind.CAST or action.mana_cost is None:
            return action, None
        if action.x_range is not None:
            return action, None
        card = self._card(action.card_id)
        if card is None:
            return action, None
        try:
            increases, reductions, floor = self.game.static_cost_modifiers_for(card, self.player)
            own = self.game.cost_reduction_for(card, self.player, targets=[])
        except ValueError:
            return action, None
        if own.symbols:
            reductions = [*reductions, own]
        if not increases and not reductions and floor is None:
            return action, None
        printed = ManaCost.parse(action.mana_cost)
        charged = apply_modifications(printed, increases, reductions, floor=floor)
        if str(charged) == str(printed):
            return action, None
        total = self._row_total(action, card)
        if total is None:
            return action, None
        rebuilt = _cast_action(
            self.game,
            self.player,
            card,
            rule=action.rule,
            keyword=action.keyword,
            alternative_cost=action.alternative_cost,
            total=TotalCost(mana=charged, components=list(total.components)),
            candidate_scan=self.scan,
            spell_face=action.spell_face,
            splice_options=action.splice_options,
        )
        return rebuilt, action.cost

    def _row_total(self, action: Action, card: CardState) -> TotalCost | None:
        """The `TotalCost` a cast row was priced from, found again among the
        same cast options the enumeration read (`legal_actions._cast_actions`)
        by the strings it printed - `None` when no option prints the same."""
        assert action.mana_cost is not None
        if action.cost == action.mana_cost:
            return TotalCost.of(ManaCost.parse(action.mana_cost))
        totals: list[TotalCost | None] = [
            option.cost for option in keyword_cast_options(card.card) + marker_cast_options(card)
        ]
        totals.extend(
            granted.option.cost
            for granted in granted_cast_options(self.game, card, self.player, card.zone)
        )
        totals.extend(
            alternative.cost for alternative in self.game.alternative_costs_for(card, self.player)
        )
        for total in totals:
            if total is not None and _cost_strings(total) == (action.cost, action.mana_cost):
                return total
        return None

    # -- modes the engine cannot read (CR 700.2a) --------------------------------

    def name_unreadable_modes(self, action: Action, view: dict[str, Any]) -> None:
        card = self._card(action.card_id)
        if card is None or not action.modes:
            return
        unreadable = unreadable_modes(card)
        if unreadable:
            view["unreadable_modes"] = [
                {
                    "mode_number": mode.number,
                    "text": mode.text,
                    "notice": unreadable_mode_notice(card, mode),
                }
                for mode in unreadable
            ]

    # -- optional additional costs ------------------------------------------------

    def add_optional_costs(self, action: Action, view: dict[str, Any]) -> None:
        if action.spell_face is not None:
            return
        card = self._card(action.card_id)
        if card is None:
            return
        face = card.up_face_name
        offers: list[dict[str, Any]] = []
        for ability in keyword_abilities(card.card):
            known = _OPTIONAL_COST_KEYWORDS.get(ability.canonical.lower())
            if known is None or (face is not None and ability.face != face):
                continue
            rule, repeatable = known
            offer: dict[str, Any] = {
                "keyword": ability.keyword,
                "rule": rule,
                "cost": ability.cost,
                "repeatable": repeatable,
            }
            if ability.canonical.lower() == "gift":
                # CR 702.174a: the cost is choosing an opponent - nothing to pay.
                offer["affordable"] = view["affordable"]
                offer["unaffordable_reason"] = view.get("unaffordable_reason")
            elif ability.canonical.lower() == "harmonize":
                if (action.keyword or "").lower() != "harmonize":
                    continue
                offer.update(self._harmonize_tap(view))
            else:
                offer.update(self._affordable_with(action, card, ability.keyword, ability.cost))
            offers.append(offer)
        for phrase in additional_cost_phrases(card.card):
            match = _BLIGHT_PHRASE.match(phrase)
            if match is None:
                continue
            offers.append(
                {
                    "keyword": "Blight",
                    "rule": "CR 701.68a",
                    "cost": f"blight {match.group('n')}",
                    "repeatable": False,
                    "affordable": Affordability.UNKNOWN.value,
                    "unaffordable_reason": (
                        "the creature blighted is named as the spell is cast (CR 701.68a) - "
                        "pass its id in the optional cost's choosing"
                    ),
                }
            )
        if offers:
            view["optional_costs"] = offers

    @staticmethod
    def _harmonize_tap(view: dict[str, Any]) -> dict[str, Any]:
        """CR 702.180a's "tapping up to one untapped creature you control",
        chosen as the harmonize cost is (CR 702.180b). The harmonize cost
        itself is this row's `cost`; the tap only reduces it, by the tapped
        creature's power, so it is priced by which creature is named."""
        if view["affordable"] == Affordability.YES.value:
            return {"cost": None, "affordable": Affordability.YES.value}
        return {
            "cost": None,
            "affordable": Affordability.UNKNOWN.value,
            "unaffordable_reason": (
                "tapping an untapped creature you control reduces this cost by its power "
                "(CR 702.180a) - pass its id in the optional cost's choosing"
            ),
        }

    def _affordable_with(
        self, action: Action, card: CardState, keyword: str, printed: str | None
    ) -> dict[str, Any]:
        extra = total_cost_for_phrase(printed or "", self.player)
        if extra is None:
            return {
                "affordable": Affordability.UNKNOWN.value,
                "unaffordable_reason": (
                    f"{card.name}'s {keyword} cost {printed!r} could not be read"
                ),
            }
        if action.x_range is not None or (
            action.mana_cost is not None and action.cost != action.mana_cost
        ):
            return {
                "affordable": Affordability.UNKNOWN.value,
                "unaffordable_reason": (
                    f"this row's cost {action.cost} is not judged here with {keyword} added"
                ),
            }
        combined = TotalCost(
            mana=ManaCost.parse(action.mana_cost or "").plus(extra.mana),
            components=list(extra.components),
        ).assign_payer(self.player)
        status, reason, _ = _cost_affordability(
            self.game,
            self.player,
            combined,
            self.scan,
            spend=ManaSpend.casting(card.card, zone=card.zone),
        )
        return {"affordable": status.value, "unaffordable_reason": reason}

    # -- what a mana ability makes on this board --------------------------------

    def state_production(self, action: Action, view: dict[str, Any]) -> None:
        permanent = self._card(action.card_id)
        if permanent is None or action.ability_index is None or action.produces is None:
            return
        abilities = list(self.game.activated_abilities(permanent))
        if not 0 <= action.ability_index < len(abilities):
            return
        mana = abilities[action.ability_index].mana
        if mana is None or mana.amount_from_devotion:
            return
        try:
            options, any_color = effective_production(self.game, permanent, mana)
        except ValueError:
            return
        if any_color or len(options) != 1:
            return
        made = _symbols(options[0])
        if made and made != action.produces:
            view["printed_produces"] = action.produces
            view["produces"] = made

    # -- a cast payable once a costlier mana ability is activated --------------

    def offer_manual_mana(self, action: Action, view: dict[str, Any]) -> None:
        if (
            view.get("affordable") != Affordability.NO.value
            or action.x_range is not None
            or not action.mana_cost
            or action.cost != action.mana_cost
        ):
            return
        card = self._card(action.card_id)
        if card is None:
            return
        cost = ManaCost.parse(action.mana_cost)
        spend = ManaSpend.casting(card.card, zone=card.zone)
        for board, steps in self._manual_boards():
            plan = plan_mana_payment(
                cost, board.battlefield, game=board, payer=self.player, spend=spend
            )
            if plan is not None:
                view["affordable"] = Affordability.YES.value
                view["unaffordable_reason"] = None
                view["activate_first"] = steps
                return

    def _manual_boards(self) -> list[tuple[Game, list[dict[str, Any]]]]:
        if self._boards is None:
            self._boards = list(self._build_manual_boards())
        return self._boards

    def _build_manual_boards(self) -> Iterator[tuple[Game, list[dict[str, Any]]]]:
        by_source: dict[UUID, list[tuple[int, str | None, Color | None]]] = {}
        for source_id, index, cost, color in self._manual_activations():
            by_source.setdefault(source_id, []).append((index, cost, color))
        trials = 0
        for size in (1, 2):
            for sources in combinations(list(by_source), size):
                for picks in product(*(by_source[source] for source in sources)):
                    if trials >= _MAX_MANUAL_TRIALS:
                        return
                    trials += 1
                    built = self._activate_on_a_copy(list(zip(sources, picks, strict=True)))
                    if built is not None:
                        yield built

    def _manual_activations(self) -> Iterator[tuple[UUID, int, str | None, Color | None]]:
        """Every untapped source's mana ability whose cost is `{T}` plus mana
        (or mana alone) - the ones auto-tap leaves alone (CR 118.3c) and that
        need no other choice to activate - once per colour it could be made
        to add."""
        for permanent in self.game.permanents_controlled_by(self.player):
            if permanent.tapped:
                continue
            for index, ability in enumerate(self.game.activated_abilities(permanent)):
                mana = ability.mana
                if mana is None or ability.loyalty is not None:
                    continue
                kinds = {piece.kind for piece in ability.costs}
                if kinds == {CostKind.TAP} or not kinds <= {CostKind.TAP, CostKind.MANA}:
                    continue
                if self.game.why_cannot_activate(permanent, index, self.player) is not None:
                    continue
                for color in self._colour_choices(permanent, index, mana):
                    yield permanent.id, index, ability.cost, color

    def _colour_choices(self, permanent: CardState, index: int, mana: Any) -> list[Color | None]:
        try:
            options, any_color = effective_production(self.game, permanent, mana)
        except ValueError:
            return []
        if any_color:
            return list(self.game.mana_color_options(permanent, index, self.player))
        if len(options) == 1:
            return [None]
        # One colour names one printed option only when that option is one mana.
        return [option[0] for option in options if len(option) == 1]

    def _activate_on_a_copy(
        self, picks: list[tuple[UUID, tuple[int, str | None, Color | None]]]
    ) -> tuple[Game, list[dict[str, Any]]] | None:
        probe = dry_run_copy(self.game)
        steps: list[dict[str, Any]] = []
        for source_id, (index, cost, color) in picks:
            source = probe.find_by_id(source_id)
            if not isinstance(source, CardState) or source.tapped:
                return None
            others = [
                other.id
                for other in probe.permanents_controlled_by(self.player)
                if other.id != source_id and not other.tapped
            ]
            before = dict(probe.player(self.player).mana_pool.floating)
            outcome = activate_ability(
                probe,
                ActivateRequest(
                    player=self.player,
                    card_id=source_id,
                    ability_index=index,
                    color=color.value if color is not None else None,
                    mana_sources=list(others),
                ),
            )
            if not outcome.allowed:
                return None
            after = probe.player(self.player).mana_pool.floating
            added = [
                color_added
                for color_added in Color
                for _ in range(after.get(color_added, 0) - before.get(color_added, 0))
            ]
            step: dict[str, Any] = {
                "card_id": str(source_id),
                "name": source.name,
                "ability_index": index,
                "cost": cost,
                "adds": _symbols(added),
            }
            if color is not None:
                step["color"] = color.value
            steps.append(step)
        return probe, steps
