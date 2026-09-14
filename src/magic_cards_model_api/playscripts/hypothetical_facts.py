"""O4/O7's engine-computed facts: `enters_any`, `unlocks`, `unlocks_by_search`.

`selectors.matches` cannot answer any of these three fields from a redacted
view alone - they read what a land would do if it entered, and what casting
would be possible with it there, which only the live `Game` and
`magic_cards_model.machines.hypothetical` can say. This module is the one
place that gap is closed: `build_hypothetical_facts(game, player)` runs
`payability_matrix` exactly once and turns its answer into a plain,
JSON-able mapping keyed by card id - the shape `selectors.Hypo` expects, and
the shape the `.../playscripts/hypothetical-facts` route (`app.py`) hands
back to a seat.

Visibility (O7's "only the viewer's own hand and library"): every fact here
comes from `player`'s own hand and library, the only zones `predict_enters`/
`payability`/`payability_matrix` ever accept a land or a card from (they
raise `ValueError` for anything else) - there is no separate check to get
wrong because the engine functions this module calls already refuse to look
anywhere else.

- `enters` is `EntersState.value` ("untapped", "tapped", "untapped_if_paid"),
  read straight off `payability_matrix`'s own `LandPayability.prediction`
  for every land asked about - `enters_any` never triggers a second
  `predict_enters` call. When `lethal_if_paid` is set, `enters` is
  substituted to `"tapped"` (O4: the untapped option is not one a player can
  use, since taking it loses the game) and a notice is recorded.
- `unlocks` is true for a land when, on that land's own column of the
  matrix, some hand card other than the land itself is `payable` there but
  was not `payable` in `now` - a plain before/after read of the one matrix,
  never a second `payability` call per pair. A pair carrying any notice (the
  land's own prediction, that card's assumed-land answer, or that card's
  `now` answer) is left out of the comparison entirely: `unlocks` fails
  closed to `False` for that pair rather than trusting an answer the engine
  itself flagged as incomplete.
- `unlocks_by_search` is true for a hand card whose ability or oracle text
  matches O3's `searches_for_any` template when at least one library land
  its search could find (by basic land type, `card.subtypes`) `unlocks`
  something in the same matrix - the search targets are already asked about
  as ordinary lands (`_land_candidates` below includes every library land),
  so this reads their own `unlocks` answer rather than computing anything
  new. A land's own `unlocks` already fails closed for a lethal *optional*
  payment against life *now* (CR 704.5a - `lethal_if_paid`), but the
  search itself has its own life cost (a fetchland's "Pay 1 life", read off
  the same line the search template matched, never a constant - the fetch
  could cost something else or nothing at all) that is paid *first*, before
  the found land's own optional payment is ever asked - so `unlocks_by_
  search` fails closed too, for a found land whose optional payment would
  be lethal only once the fetch's own life cost already came off first
  (life at or below the two combined), even when neither payment alone
  crosses zero.

`payability_matrix` is called once per `build_hypothetical_facts` call, over
every hand card and every candidate land (hand lands, library lands): "use
payability_matrix once per condition evaluation, never one payability call
per pair" (O7).
"""

from __future__ import annotations

import re
from typing import Any

from magic_cards_model.machines.hypothetical import EntersState, payability_matrix
from magic_cards_model.models import CardState, Game, Zone

from ..serialize import _card_view
from .selectors import BASIC_LAND_TYPES, SEARCH_TEMPLATE, _searched_basic_types, _texts

#: `_BASIC_LAND_TYPES_FOLDED`-equivalent, cased as `Card.subtypes` reports.
_BASIC_LAND_TYPES = frozenset(BASIC_LAND_TYPES)

#: A card's own words for a life payment ("Pay 1 life", a fetchland's own
#: activation cost; "pay 2 life", a shockland's optional payment prompt,
#: `hypothetical.EntersPrediction.optional_costs`) - read off the card, D3,
#: never a constant.
_PAY_LIFE = re.compile(r"\bpay (?P<amount>\d+) life\b", re.IGNORECASE)


def _fetch_life_cost(card_view: dict[str, Any], oracle_text: str) -> int:
    """The life a search-template ability's own activation cost pays (a
    fetchland's "Pay 1 life"), read off the same line - an ability's own
    text, or one line of the printed oracle text - the search template
    itself matched (D3): the card's own words, since a printing could cost a
    different amount or none at all, never a constant. 0 when that line has
    no life cost."""
    for text in (*_texts(card_view), *oracle_text.splitlines()):
        if SEARCH_TEMPLATE.search(text):
            match = _PAY_LIFE.search(text)
            if match is not None:
                return int(match.group("amount"))
    return 0


def _optional_payment_life(prediction_optional_costs: tuple[str, ...]) -> int:
    """The life a land's own optional payment prompts (`EntersPrediction.
    optional_costs`) ask for, summed - 0 for a land with no such prompt, or
    one whose optional cost is not a life payment at all."""
    return sum(
        int(match.group("amount"))
        for prompt in prediction_optional_costs
        if (match := _PAY_LIFE.search(prompt)) is not None
    )


def _is_land_candidate(card: CardState) -> bool:
    """A land already on this side of a `predict_enters`/`payability_matrix`
    call: a printed land anywhere, or (CR 712.12) a modal double-faced card
    with a land back face while it sits in hand."""
    return card.card.is_land or (card.zone is Zone.HAND and card.card.land_face is not None)


def _land_candidates(hand: list[CardState], library: list[CardState]) -> list[CardState]:
    """Every land `enters_any`/`unlocks` can be asked about: hand lands (an
    MDFC land back included) and every library land - `payability_matrix`
    itself shares one evaluation across same-printing library copies, so
    this does not multiply the engine work by library size."""
    return [card for card in hand if _is_land_candidate(card)] + [
        card for card in library if card.card.is_land
    ]


def _land_subtypes(card: CardState) -> set[str]:
    return {subtype.casefold() for subtype in card.card.subtypes}


def build_hypothetical_facts(game: Game, player: str) -> dict[str, Any]:
    """`{"cards": {card_id: {"enters"?, "unlocks"?, "unlocks_by_search"?}},
    "notices": [...]}` for every hand land and every hand card, of `player`
    - see the module docstring. `game` is read, never mutated (the same
    guarantee `payability_matrix` itself carries).

    Library lands are read (`_land_candidates`) and judged (`matrix.lands`)
    exactly as hand lands are - `unlocks_by_search` needs their own
    `unlocks` answer - but neither a library land's id nor its own
    `enters`/`unlocks` entry, nor a notice naming it, ever reaches the
    return value: `serialize._contents_view` deliberately drops a hidden
    zone's order and identities (CR 401.2), and a library land keyed by its
    own id in matrix (library) order would leak exactly that. Only the
    per-hand-card results - a hand land's own facts, and `unlocks_by_search`
    on the hand card whose search reads them - are ever returned, and both
    `cards` and `notices` are sorted so the bytes returned do not depend on
    hand order either."""
    me = game.player(player)
    hand = list(me.hand)
    hand_ids = {str(card.id) for card in hand}
    library = list(me.library)
    lands = _land_candidates(hand, library)
    cards: dict[str, dict[str, Any]] = {}
    notices: list[str] = []
    if not hand or not lands:
        return {"cards": cards, "notices": notices}

    matrix = payability_matrix(game, player, hand, lands)
    now_payable = {payability.card_id: payability for payability in matrix.now}

    #: land id -> the life its own optional payment costs (0 for a land
    #: with none), for every land that unlocks something - D3 needs the
    #: amount, not just whether it unlocks, to fail closed when a hand
    #: card's own search cost would already have been paid first.
    unlocking_land_ids: dict[str, int] = {}
    for column in matrix.lands:
        prediction = column.prediction
        state = prediction.state.value
        land_notices = list(prediction.notices)
        if prediction.lethal_if_paid:
            state = EntersState.TAPPED.value
            land_notices.append(
                f"{prediction.name}: the optional payment is lethal (CR 704.5a), so "
                "enters_any reads this land as tapped rather than untapped_if_paid"
            )
        land_id = str(column.land_id)
        is_hand_land = land_id in hand_ids
        if is_hand_land:
            notices.extend(land_notices)
            entry = cards.setdefault(land_id, {})
            entry["enters"] = state

        unlocks = False
        for payability in column.cards:
            if payability.card_id == column.land_id:
                continue
            now = now_payable.get(payability.card_id)
            if land_notices or payability.notices or (now is not None and now.notices):
                continue
            was_payable = now is not None and now.payable
            if payability.payable and not was_payable:
                unlocks = True
                break
        if is_hand_land:
            entry["unlocks"] = unlocks
        if unlocks:
            unlocking_land_ids[land_id] = _optional_payment_life(prediction.optional_costs)

    library_land_types: dict[str, set[str]] = {
        str(card.id): _land_subtypes(card) for card in library if card.card.is_land
    }
    for card in hand:
        view = _card_view(game, card, {})
        oracle_text = str(view.get("oracle_text") or "")
        searched = _searched_basic_types(view, oracle_text)
        searched &= {word.casefold() for word in _BASIC_LAND_TYPES}
        if not searched:
            continue
        # D3: the search's own life cost is paid before the found land's own
        # optional payment is ever asked (CR 601.2h, then CR 119.4 as it
        # enters), so both have to fit under the same life total - a land
        # whose own optional payment reads safe against life *now*
        # (`lethal_if_paid`, above) can still be lethal only once the
        # fetch's own cost has already come off first.
        fetch_cost = _fetch_life_cost(view, oracle_text)
        by_search = any(
            types & searched
            and land_id in unlocking_land_ids
            and me.life > fetch_cost + unlocking_land_ids[land_id]
            for land_id, types in library_land_types.items()
        )
        cards.setdefault(str(card.id), {})["unlocks_by_search"] = by_search

    return {"cards": dict(sorted(cards.items())), "notices": sorted(notices)}


__all__ = ["build_hypothetical_facts"]
