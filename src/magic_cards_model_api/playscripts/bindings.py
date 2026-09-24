"""Bindings: judge every play on exactly what the run itself would use.

`playscripts/script_tools.py` (`applicable_hint`, `listing`), `playscripts/
arming.py` (`_judge_round`) and `playscripts/runtime.py` (`run_script`, once
FC5 switches it over) all judge the same registered plays against the same
board, and every one of them has to agree on three things: which inputs are
in scope (the seat's last-asserted `opposing_archetype` plus whatever else it
supplied), what a condition naming an input is allowed to hold (`inputs.
input_catalog`), and - for a play whose selectors need engine-computed
hypothetical facts (`conditions.needs_hypothetical`) - whether that fetch
also needs the `next_turn` horizon (`conditions.needs_next_turn`).

Before this module existed, `applicable_hint` built its own guess at all
three, independently of what a run would actually see: it read `corpus[
"_last_asserted_archetype"]`, falling back to the wildcard `'*'` whenever a
seat had never actually asserted one, and it fetched hypothetical facts with
no horizon at all, even for a play a run would fetch with
`horizon="next_turn"`. A play whose `false` list excluded the real matchup,
or whose `opposing_archetypes` did not list it, was offered anyway under the
`'*'` stand-in, and then exited `not_applicable` the moment it actually ran
under the seat's real archetype - the offer and the run disagreed about the
very bindings each was judged against.

`judge_bindings` is the one place that builds those three things, the same
way for every caller: `asserted` is exactly what the seat last told `run`,
`list` or `arm` - `None`, genuinely, until one of those has ever been called
for this seat, never coerced to `'*'` as a stand-in - and `facts(play)`
fetches hypothetical facts (at most once per horizon this round) with
`horizon='next_turn'` exactly when `conditions.needs_next_turn(play)` says
the play needs it, mirroring `runtime._HypotheticalCache.get`'s own horizon
choice exactly.

`references_archetype(play)` is the other half of the fix: whether a play's
own `opposing_archetypes` list, or a `true`/`false` condition of its
(an `all_of` member included), names anything but the wildcard matchup or the
built-in `opposing_archetype` input. A play this says True for, judged while
`asserted is None`, is never offered: `_judged` (`script_tools.py`) marks it
not applicable with `needs_input` naming `opposing_archetype`, rather than
silently matching it against a stand-in the run itself would never use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..seatkit import HypotheticalFactsUnavailable, Seat
from . import conditions
from .inputs import BUILTIN_INPUT, input_catalog
from .schema import WILDCARD

logger = logging.getLogger("mcm.playscripts.bindings")

__all__ = ["Bindings", "judge_bindings", "references_archetype"]


class _FactsCache:
    """This seat's hypothetical facts (`Seat.hypothetical_facts`), fetched at
    most once per horizon for one judging round - the plain fetch and the
    `next_turn` one are two independent slots, since one round can judge
    plays that need either or both, or neither (`Bindings.facts`'s own
    caller still gates the call on `conditions.needs_hypothetical(play)`, so
    a round that judges no such play never fetches at all).

    A failed fetch is remembered as `unavailable`, the error's own text, so
    a caller marks only the plays that needed it not applicable, with why -
    never raises out of a judgement, and never stands in a bare `{}` (a
    `count {unlocks: true} == 0` would read that as a real, empty answer)."""

    def __init__(self, seat: Seat) -> None:
        self._seat = seat
        self._slots: dict[bool, tuple[dict[str, Any] | None, str | None]] = {}

    def get(self, *, horizon: bool) -> tuple[dict[str, Any] | None, str | None]:
        if horizon not in self._slots:
            try:
                fetched = self._seat.hypothetical_facts(horizon="next_turn" if horizon else None)
                cards = dict(fetched.get("cards") or {}) if isinstance(fetched, dict) else {}
                self._slots[horizon] = (cards, None)
            except HypotheticalFactsUnavailable as exc:
                reason = str(exc)
                logger.warning(
                    "hypothetical facts unavailable for %s (horizon=%s): %s",
                    self._seat.uid,
                    horizon,
                    reason,
                )
                self._slots[horizon] = (None, reason)
        return self._slots[horizon]


@dataclass(frozen=True)
class Bindings:
    """The exact bindings one judging round runs every play against.
    `applicable_hint`, `listing`, `arming._judge_round` and (FC5) `runtime.
    run_script` all build this the same way, through `judge_bindings` below,
    so an offer and the run it invites never disagree about what applied.

    `inputs`: the full merged input dict a condition reads (`runtime.
    run_script`'s own `ctx.inputs`) - carries the built-in `opposing_
    archetype` key exactly when `asserted` is not `None`.

    `catalog`: every input a condition may name, `asserted` folded into the
    built-in `opposing_archetype` enum the same way `runtime.run_script`
    folds it in (`inputs.input_catalog`).

    `asserted`: the archetype this seat last told `run`, `list` or `arm` -
    `None`, genuinely, until one of those has ever been called for this seat.
    Never coerced to the wildcard `'*'` as a stand-in.

    `facts(play)`: this round's hypothetical facts for `play`, fetched (at
    most once per horizon) exactly when asked for - the caller still gates
    the call itself on `conditions.needs_hypothetical(play)`, same as
    before this module existed; this only ever decides the horizon, via
    `conditions.needs_next_turn`."""

    inputs: dict[str, Any]
    catalog: dict[str, dict[str, Any]]
    asserted: str | None
    _cache: _FactsCache = field(repr=False, compare=False)

    def facts(self, play: Any) -> tuple[dict[str, Any] | None, str | None]:
        return self._cache.get(horizon=conditions.needs_next_turn(play))


def judge_bindings(
    seat: Seat,
    corpus: dict[str, Any],
    *,
    archetype: str | None,
    supplied: dict[str, Any],
) -> Bindings:
    """The bindings one judging round runs every one of `corpus`'s
    registered plays against. `archetype` is the asserted `opposing_
    archetype` for this round, or `None` when this seat has never asserted
    one; `supplied` is the full merged input dict a condition reads (the
    caller's own `merged_inputs(archetype, ...)`, or - before any assertion
    - whatever non-archetype inputs it still remembers).

    Built exactly the way `runtime.run_script` builds its own `ctx.inputs`/
    `ctx.catalog` today (so FC5 can switch it to this without changing any
    behaviour): the catalog's built-in enum carries `archetype` alongside
    `'*'` and whatever the deck itself declares (`inputs.input_catalog`),
    and `inputs` is `supplied`, copied but otherwise unchanged."""
    declared = list(corpus.get("inputs") or [])
    catalog = input_catalog(declared, [archetype] if isinstance(archetype, str) else ())
    return Bindings(
        inputs=dict(supplied),
        catalog=catalog,
        asserted=archetype,
        _cache=_FactsCache(seat),
    )


def _condition_reads_archetype(condition: Any) -> bool:
    return (
        isinstance(condition, dict)
        and condition.get("kind") == "input"
        and condition.get("name") == BUILTIN_INPUT
    )


def references_archetype(play: dict[str, Any]) -> bool:
    """Whether `play` cares which archetype is asserted: its own `
    opposing_archetypes` names anything but the wildcard, or a `true`/
    `false` condition of its (an `all_of` member included) reads the
    built-in `opposing_archetype` input. Judged while nothing has ever been
    asserted (`Bindings.asserted is None`), a play this says True for is
    never offered - see the module docstring."""
    listed = play.get("opposing_archetypes") or [WILDCARD]
    if list(listed) != [WILDCARD]:
        return True
    for polarity in ("true", "false"):
        for condition in play.get(polarity) or []:
            if not isinstance(condition, dict):
                continue
            if _condition_reads_archetype(condition):
                return True
            if condition.get("kind") == "all_of" and any(
                _condition_reads_archetype(inner) for inner in condition.get("conditions") or []
            ):
                return True
    return False
