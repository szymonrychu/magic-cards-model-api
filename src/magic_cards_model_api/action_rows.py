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
from magic_cards_model.machines.casting import _BLIGHT_PHRASE
from magic_cards_model.machines.cost import (
    TotalCost,
    additional_cost_phrases,
    mana_candidate_scan,
    plan_mana_payment,
    total_cost_for_phrase,
)
from magic_cards_model.machines.decisions import PendingDecision
from magic_cards_model.machines.legal_actions import (
    _cast_action,
    _cost_affordability,
    legal_actions,
)
from magic_cards_model.models import Action, CardState, Color, Game
from magic_cards_model.models.action import ActionKind, Affordability
from magic_cards_model.models.mana import ManaCost, ManaSpend

from .engine_api import activate_ability, dry_run_copy, effective_production
from .schemas import ActivateRequest

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
    actions = [rows.with_front_face_cost(action) for action in actions]
    views = [action.model_dump(mode="json") for action in actions]
    for action, view in zip(actions, views, strict=True):
        if action.kind is ActionKind.CAST:
            rows.add_optional_costs(action, view)
        elif action.kind is ActionKind.ACTIVATE_MANA:
            rows.state_production(action, view)
    for action, view in zip(actions, views, strict=True):
        if action.kind is ActionKind.CAST:
            rows.offer_manual_mana(action, view)
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
