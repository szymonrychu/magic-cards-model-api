"""Answer, server-side, the decisions that have exactly one legal answer.

An AI seat spends a whole model inference on every decision addressed to it,
including the ones it cannot answer any other way: a declare-attackers pause
with nothing able to attack, a trigger whose only legal target is one object,
a "pay 2 life" it cannot afford. The harness answers those itself - and only
those. The proof is always the engine's own enumeration: while a decision
stands, `GET .../legal-actions` (`seatkit.available_actions`) holds nothing
but that decision's legal answers (`machines.legal_actions.decision_answers`
in the engine), so "exactly one legal answer" is read off those rows, never
inferred from the prompt or the board. Anything this module cannot prove
forced - an unknown kind, an unfamiliar row shape, a failed enumeration, a
choice among two or more - is `None`, and the agent answers it as before.

Every answer goes through `POST .../decide` (the combat declarations too, as
`pairs: []`) with the question's own `generation`, so an answer computed off
a read the game has since moved past is refused rather than applied to a
question it was never meant for, and with `reasoning` saying it was
automatic, so the game log and the audit show who really decided.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import seatkit

logger = logging.getLogger("mcm.forced_decisions")

#: How every automatic answer's `reasoning` starts, so a reviewer (or a
#: grep over the ledger) can tell it apart from a seat's own.
AUTO_REASONING_PREFIX = "auto-answered by the seat harness"

#: Kept at the advice rationale cap (`schemas.ADVICE_REASONING_MAX_CHARS`):
#: `DecideRequest.reasoning` has no cap of its own, but a one-line note is
#: all this needs to be.
REASONING_MAX_CHARS = 240

_ANSWER_ROW = "answer_decision"
_DECLARATIONS = frozenset({"declare_attackers", "declare_blockers"})

#: Two-way kinds the engine enumerates as one `answer_decision` row per legal
#: option (`decision_option` "true"/"false"), and the `decide` body field
#: each answers on. `pay_mana` is deliberately absent: its "yes" also picks
#: which sources to tap, a choice this module will not make for a seat.
_BOOLEAN_FIELDS = {
    "may": "accept",
    "pay_life": "accept",
    "pay_energy": "accept",
    "mulligan": "accept",
    "opening_hand_action": "accept",
    "put_on_top_or_bottom": "top",
    "put_into_hand_or_graveyard": "to_hand",
    "choose_pile": "pile_one",
}
_OPTION_KINDS = frozenset({"choose_a_mode", "choose_a_quality"})
_AMOUNT_FIELDS = {
    "pay_energy_amount": "energy_amount",
    "pay_life_amount": "life_amount",
    "choose_an_amount": "amount",
}


def _one_candidate(row: dict[str, Any], pending: dict[str, Any]) -> tuple[str, str] | None:
    """`(kind of token, token)` when the row offers exactly one candidate and
    requires exactly one to be chosen - so choosing it is the only legal
    answer. Deliberately one, not "as many as the minimum": with two or more
    required the order of the ids can carry meaning (separate target slots),
    and that is not a choice to make blind."""
    cards = row.get("decision_candidates") or []
    players = row.get("decision_player_candidates") or []
    x_range = row.get("x_range")
    if not (isinstance(x_range, list | tuple) and len(x_range) == 2):
        return None
    minimum, _maximum = x_range
    if minimum != 1 or len(cards) + len(players) != 1:
        return None
    if pending.get("kind") == "choose_targets" and pending.get("target_minimum") != 1:
        return None
    if cards:
        return "card", str(cards[0])
    return "player", str(players[0])


def _forced(pending: Any, actions_view: Any) -> tuple[dict[str, Any], str] | None:
    """`(answer, why)` when the enumeration proves `pending` has one legal
    answer, else `None`. `forced_answer` is this minus the `why`."""
    if not isinstance(pending, dict) or not isinstance(actions_view, dict):
        return None
    if actions_view.get("actions_error"):
        return None
    kind, player = pending.get("kind"), pending.get("player")
    if not isinstance(kind, str) or not isinstance(player, str):
        return None
    listed = actions_view.get("pending_decision")
    if isinstance(listed, dict) and (
        listed.get("kind") != kind or listed.get("generation") != pending.get("generation")
    ):
        return None
    rows = actions_view.get("actions")
    if not isinstance(rows, list) or not rows:
        return None
    if not all(isinstance(row, dict) and row.get("player") == player for row in rows):
        return None
    answer: dict[str, Any] = {"player": player}
    generation = pending.get("generation")
    if isinstance(generation, int):
        answer["generation"] = generation

    if kind in _DECLARATIONS:
        # One row and it is "declare nobody": no creature/pair is legal.
        if (
            len(rows) == 1
            and rows[0].get("kind") == _ANSWER_ROW
            and rows[0].get("decision_option") == "declare_nobody"
        ):
            answer["pairs"] = []
            what = "attack" if kind == "declare_attackers" else "block"
            return answer, f"no creature can legally {what}, so declaring nobody"
        return None

    if len(rows) != 1 or rows[0].get("kind") != _ANSWER_ROW:
        return None
    (row,) = rows
    option = row.get("decision_option")

    if kind in _BOOLEAN_FIELDS:
        if option not in ("true", "false"):
            return None
        value = option == "true"
        if not value and pending.get("affirmative_is_legal") is not False:
            # "no" alone should only ever be enumerated when "yes" is
            # illegal; if the two disagree, trust neither.
            return None
        answer[_BOOLEAN_FIELDS[kind]] = value
        return answer, f"{option} is the only legal answer to this {kind}"

    if kind in _OPTION_KINDS:
        if not isinstance(option, str) or not option:
            return None
        answer["choice"] = option
        return answer, f"{option!r} is the only option offered"

    if kind in _AMOUNT_FIELDS:
        if list(row.get("x_range") or []) != [0, 0]:
            return None
        answer[_AMOUNT_FIELDS[kind]] = 0
        return answer, "0 is the only legal amount"

    if kind == "order_triggers":
        candidates = row.get("decision_candidates") or []
        if len(candidates) != 1 or row.get("decision_player_candidates"):
            return None
        answer["order"] = [str(candidates[0])]
        return answer, "a single ability has only one order"

    if kind in ("choose_targets", "choose_cards", "choose_players"):
        if kind == "choose_cards" and pending.get("exact") is not True:
            return None
        one = _one_candidate(row, pending)
        if one is None:
            return None
        token_kind, token = one
        if kind == "choose_cards" and token_kind != "card":
            return None
        if kind == "choose_players" and token_kind != "player":
            return None
        field = {"choose_targets": "targets", "choose_cards": "card_ids"}.get(kind, "usernames")
        answer[field] = [token]
        return answer, f"exactly one legal choice ({token_kind} {token}) and one is required"

    return None


def forced_answer(pending: dict[str, Any], actions_view: dict[str, Any]) -> dict[str, Any] | None:
    """The `POST .../decide` body (minus `reasoning`) that answers `pending`,
    when `actions_view` - `seatkit.available_actions` read while `pending`
    stands - proves it is the only legal answer; `None` otherwise."""
    forced = _forced(pending, actions_view)
    return forced[0] if forced is not None else None


def forced_reason(pending: dict[str, Any], actions_view: dict[str, Any]) -> str | None:
    """Why `forced_answer` is forced, in a few words (`None` when it is not)."""
    forced = _forced(pending, actions_view)
    return forced[1] if forced is not None else None


def _reasoning(why: str) -> str:
    text = f"{AUTO_REASONING_PREFIX}: {why} - the only legal answer"
    if len(text) > REASONING_MAX_CHARS:
        text = text[: REASONING_MAX_CHARS - 3] + "..."
    return text


def post_forced_answer(seat: Any, pending: dict[str, Any], answer: dict[str, Any], why: str) -> Any:
    """Post `answer` through the seat's own `decide` path, stamped automatic.

    Returns `Seat.call_json`'s parsed answer: `{"accepted": True, ...}` on
    success, `{"accepted": False, "reason": ...}` for a refusal the route
    itself made (a stale `generation` among them), or the normalised
    `{"allowed": False, ...}` shape for a non-2xx or an ambiguous timeout.
    A connection failure raises `httpx.HTTPError`, as `call_json` does."""
    body = {**answer, "reasoning": _reasoning(why)}
    body.setdefault("player", pending.get("player"))
    return seat.call_json("POST", f"/api/games/{seat.game_id()}/decide", body)


def _compact(answer: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in answer.items() if k not in ("player", "generation")}


def auto_answer_forced(
    seat: Any, state: dict[str, Any], *, limit: int = 8
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Answer this seat's forced decisions one after another, and stop at the
    first one that is not forced, is not this seat's, is refused, or at
    `limit`. Returns `(latest state, entries)`, one entry per answer posted.

    Never raises on an HTTP failure: it stops and returns what it has, with
    the failure recorded on the entry it happened on."""
    entries: list[dict[str, Any]] = []
    for _ in range(limit):
        pending = state.get("pending_decision")
        if not seatkit.decision_is_mine(pending, state, seat.player):
            break
        try:
            view = seatkit.available_actions(seat, state)
        except (httpx.HTTPError, ValueError, RuntimeError):
            break
        forced = _forced(pending, view)
        if forced is None:
            break
        answer, why = forced
        entry: dict[str, Any] = {
            "kind": pending.get("kind"),
            "prompt": pending.get("prompt"),
            "answer": _compact(answer),
            "why": why,
        }
        entries.append(entry)
        try:
            outcome = post_forced_answer(seat, pending, answer, why)
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            entry["outcome"] = f"not posted: {exc}"
            break
        if not (isinstance(outcome, dict) and outcome.get("accepted") is True):
            reason = outcome.get("reason") if isinstance(outcome, dict) else outcome
            entry["refused"] = str(reason or "refused")
            if isinstance(outcome, dict) and outcome.get("unknown_outcome"):
                entry["unknown_outcome"] = True
            state = _refetch(seat, state)
            break
        logger.info(
            "auto-answered forced decision seat=%s kind=%s why=%s",
            str(seat.uid)[:8],
            entry["kind"],
            why,
        )
        fresh = _refetch(seat, None)
        if fresh is None:
            break
        state = fresh
    return state, entries


def _refetch(seat: Any, fallback: Any) -> Any:
    try:
        fresh = seat.fetch_state()
    except (httpx.HTTPError, ValueError, RuntimeError):
        return fallback
    return fresh if isinstance(fresh, dict) and "detail" not in fresh else fallback
