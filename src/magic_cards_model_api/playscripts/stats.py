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
- **the admitted entries** (`admitted`) - one entry per successful
  `playscripts.admit.admit()` run of this game's own handover, each shaped
  exactly like that call's own `"admitted"` result (`admit._landing`):
  `added` (new play files), `updated` (changed beyond `priority`, or
  deleted) and `reprioritized` (a `priority`-only change), each a list of
  play ids. This is deliberately the *real* shape `admit.py` computes from
  its own git diff - not a label a caller invents - so these are never a
  fourth, independent reimplementation of "what kind of change was this":
  they are read straight off the one place that rule is actually evaluated (see
  `admit._landing`'s own docstring for exactly how). Scoped to this game the
  same way `records` is: a real admitted entry carries no `game_id` of its
  own (a `plays admit` run is already scoped to one game by the caller that
  invoked it), so filtering *which* admits belong to `game_id` is this
  function's caller's job, same as it already is for `records` spanning
  several games would be its caller's job to split first. `added`/`updated`/
  `reprioritized` count the *distinct* play ids across every entry's lists
  whose last mention (list order - later admits of the same game supersede
  earlier ones) is that kind of change; `admit._landing` already files a
  deletion under `updated` (a deleted play file is still a play file that
  changed). A game admitted
  several times counts each play id once, off its last mention - "ids are
  deduped across several admits of one game".

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

#: The per-play keys of an admitted entry (`admit._Landing`), each a list of
#: play ids.
_ADMIT_KINDS = ("added", "updated", "reprioritized")


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
    admitted: Iterable[Mapping[str, Any]],
) -> tuple[int, int, int, tuple[str, ...], tuple[str, ...]]:
    """`admitted`'s own real per-play classification, flattened and deduped
    - see the module docstring for the entry shape (`admit._landing`'s own
    `added`/`updated`/`reprioritized` lists, one entry per admit) and why no
    `game_id` field is read off it."""
    last_change: dict[str, str] = {}
    for entry in admitted:
        for kind in _ADMIT_KINDS:
            for play_id in entry.get(kind) or ():
                if isinstance(play_id, str):
                    last_change[play_id] = kind

    added_ids = tuple(sorted(pid for pid, kind in last_change.items() if kind == "added"))
    updated_ids = tuple(sorted(pid for pid, kind in last_change.items() if kind == "updated"))
    reprioritized = sum(1 for kind in last_change.values() if kind == "reprioritized")
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
    added, updated, reprioritized, added_ids, updated_ids = _admitted_counts(admitted)
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
