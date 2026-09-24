"""DR1: `game_play_stats` - one seat's per-game play statistics, a pure
aggregation over three already-computed inputs rather than a fourth reader
of the ledger:

- **the registered corpus** (`corpus`) - this seat's plays for this game, the
  same documents `games.scripts` JSONB holds (a mapping of play id to play
  document, or a plain iterable of them - either is accepted, see
  `_corpus_docs`). `plays_total` is how many there are; `plays_move_bearing`/
  `plays_advice` split them on whether `moves` is non-empty (`lint`'s own
  reading of "advice-only": a play with no moves commits nothing to the
  game and exists to explain a decision rather than act on one).
- **this seat's own ledger rows** (`records`) - offer outcomes only, read
  through `report._offer_outcomes` (aliased below as `offer_outcomes`,
  *imported*, never reimplemented - `report.py` is not edited by this
  module). `used` is `report.TAKEN`, `denied` is `report.DECLINED`,
  `passed_over` is `report.PASSED_OVER`; `offered` is every offer counted at
  all, whichever of the four outcomes (`report.OFFERED` included) it
  resolved to - `evidence_record`'s own reading of the same field name.
- **the admitted entries** (`admitted`) - what a post-game handover's own
  `plays admit` runs changed, one entry per play id per admit:
  `{game_id, play_id, change}`, `change` one of `"added"`, `"updated"`,
  `"reprioritized"`, `"deleted"`. Scoped to `game_id` the same way `records`
  is (an admit list spanning several games is not this function's problem
  to filter itself). `added`/`updated`/`reprioritized` count the *distinct*
  play ids whose last entry (list order - later admits of the same game
  supersede earlier ones) is that kind of change; `deleted` counts as
  `updated` (a deleted play file is still a play file that changed). A game
  admitted several times counts each play id once, off its last entry -
  "ids are deduped across several admits of one game".

A game with no offers and no admits gives every counting field `0` and every
id list `()`, never a missing key - `GAME_PLAY_STATS_FIELDS` is the fixed
shape, and `game_play_stats([], ..., corpus=[], admitted=[])` is this
module's own test of that.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .report import DECLINED, PASSED_OVER, TAKEN, _offer_outcomes

#: A thin public alias, per this task's own note: `report.py` is read and
#: imported, not edited - `_offer_outcomes` stays private there, and this is
#: the "public alias on import" that note asks for instead of exporting it
#: from `report.py` itself.
offer_outcomes = _offer_outcomes

#: Every key `game_play_stats` returns, in the order it builds them - the
#: fixed shape a caller can rely on even for an empty game.
GAME_PLAY_STATS_FIELDS = (
    "plays_total",
    "plays_move_bearing",
    "plays_advice",
    "offered",
    "used",
    "denied",
    "passed_over",
    "added",
    "updated",
    "reprioritized",
    "added_ids",
    "updated_ids",
)

#: An admitted entry's own `change` values that count as `updated` -
#: `"updated"` itself, and `"deleted"` (a deleted play file is still a play
#: file that changed, per this module's own docstring).
_UPDATED_CHANGES = frozenset({"updated", "deleted"})


def _corpus_docs(
    corpus: Mapping[str, Any] | Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """`corpus`'s own play documents, whichever shape it was handed in -
    `games.scripts`' own `id -> doc` mapping, or a plain iterable of docs."""
    if isinstance(corpus, Mapping):
        return [doc for doc in corpus.values() if isinstance(doc, Mapping)]
    return [doc for doc in corpus if isinstance(doc, Mapping)]


def _corpus_counts(corpus: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> tuple[int, int, int]:
    docs = _corpus_docs(corpus)
    move_bearing = sum(1 for doc in docs if doc.get("moves"))
    return len(docs), move_bearing, len(docs) - move_bearing


def _offer_counts(
    records: Iterable[Mapping[str, Any]], game_id: str, seat: str
) -> tuple[int, int, int, int]:
    offered = used = denied = passed_over = 0
    for _, outcome, _, _ in offer_outcomes(records, game_id, seat):
        offered += 1
        if outcome == TAKEN:
            used += 1
        elif outcome == DECLINED:
            denied += 1
        elif outcome == PASSED_OVER:
            passed_over += 1
    return offered, used, denied, passed_over


def _admitted_counts(
    admitted: Iterable[Mapping[str, Any]], game_id: str
) -> tuple[int, int, int, tuple[str, ...], tuple[str, ...]]:
    last_change: dict[str, str] = {}
    for entry in admitted:
        if entry.get("game_id") != game_id:
            continue
        play_id = entry.get("play_id")
        change = entry.get("change")
        if not isinstance(play_id, str) or not isinstance(change, str):
            continue
        last_change[play_id] = change

    added_ids = tuple(sorted(pid for pid, change in last_change.items() if change == "added"))
    updated_ids = tuple(
        sorted(pid for pid, change in last_change.items() if change in _UPDATED_CHANGES)
    )
    reprioritized = sum(1 for change in last_change.values() if change == "reprioritized")
    return len(added_ids), len(updated_ids), reprioritized, added_ids, updated_ids


def game_play_stats(
    records: Iterable[Mapping[str, Any]],
    game_id: str,
    seat: str,
    corpus: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    admitted: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """`seat`'s own per-game play statistics for `game_id` - see the module
    docstring for what each of `GAME_PLAY_STATS_FIELDS` counts and where it
    comes from."""
    plays_total, plays_move_bearing, plays_advice = _corpus_counts(corpus)
    offered, used, denied, passed_over = _offer_counts(records, game_id, seat)
    added, updated, reprioritized, added_ids, updated_ids = _admitted_counts(admitted, game_id)
    return {
        "plays_total": plays_total,
        "plays_move_bearing": plays_move_bearing,
        "plays_advice": plays_advice,
        "offered": offered,
        "used": used,
        "denied": denied,
        "passed_over": passed_over,
        "added": added,
        "updated": updated,
        "reprioritized": reprioritized,
        "added_ids": added_ids,
        "updated_ids": updated_ids,
    }
