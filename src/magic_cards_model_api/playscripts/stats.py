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

from ..audit import SCRIPT_RUN_ACTION, SCRIPT_RUN_STARTED_ACTION
from ..records import ADHOC_SCRIPT_ID_PREFIX, SCRIPT_STAGED_ACTION
from .report import DECLINED, PASSED_OVER, TAKEN, _offer_outcomes, _payload

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
    "manual_calls",
    "script_actions",
    "adhoc_staged",
    "adhoc_runs",
    "manual_chains",
)

#: FD10 (c): a run of this many or more consecutive manual action rows by one
#: seat, within one turn and step, with no opponent action between, is a
#: manual chain - a candidate the post-game review owes a generalized
#: `repeat` or multi-move play, or a `plays no-change`, exactly like an
#: expressible decision (`playscripts.report.manual_chain_candidates`).
MANUAL_CHAIN_MIN = 4

#: The substring `runtime._suffixed` appends to every move a script run
#: itself sends (`" | script <id>@<sha> ..."`) - `script_driven`'s second
#: signal, for a chained/child run segment `_run_spans`' own start/finish
#: pairing does not itself cover (see that function's own docstring).
_SCRIPT_REASONING_MARKER = " | script "

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


def _run_spans(entries: list[Mapping[str, Any]], seat: str) -> list[range]:
    """Mirrors `report._script_spans`'s own reading of a seat's run spans,
    without that function's `game_id` filter: `script_driven` takes no
    `game_id` of its own, since `game_play_stats`' whole contract is that its
    `records` are already scoped to one game by the caller (see the module
    docstring)."""
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    for record in entries:
        payload = _payload(record)
        action = payload.get("action")
        if action not in (SCRIPT_RUN_STARTED_ACTION, SCRIPT_RUN_ACTION):
            continue
        detail = payload.get("detail") or {}
        if detail.get("seat") != seat:
            continue
        seq = record.get("seq")
        if not isinstance(seq, int):
            continue
        run_id = str(detail.get("run_id"))
        if action == SCRIPT_RUN_STARTED_ACTION:
            starts[run_id] = min(seq, starts.get(run_id, seq))
            continue
        low = detail.get("seq_start")
        high = detail.get("seq_end")
        starts[run_id] = min(
            [seq, starts.get(run_id, seq), *([low] if isinstance(low, int) else [])]
        )
        ends[run_id] = max([seq, *([high] if isinstance(high, int) else [])])
    return [range(starts[run_id], ends[run_id] + 1) for run_id in ends]


def script_driven(entry: Mapping[str, Any], entries: Iterable[Mapping[str, Any]]) -> bool:
    """Whether `entry` - one ledger row - was produced by a script run rather
    than a manual MCP/CLI call: its own `reasoning` carries the runner's own
    chain suffix (`runtime._suffixed`, `" | script <id>@<sha> ..."` -
    `_SCRIPT_REASONING_MARKER`), the mark of a chained/child run segment
    `_run_spans`' start/finish pairing alone does not cover, or its own `seq`
    falls inside one of the acting seat's own run spans among `entries`
    (`_run_spans`). The complement - manual - is every committing or refused
    action row this is `False` for: `game_play_stats`' own `manual_calls`/
    `manual_chains`, and FD4's G17 skip, both read this same test rather than
    each keeping their own copy of "was this row inside a run window"."""
    payload = _payload(entry)
    detail = payload.get("detail") or {}
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str):
        reasoning = detail.get("reasoning")
    if isinstance(reasoning, str) and _SCRIPT_REASONING_MARKER in reasoning:
        return True
    seq = entry.get("seq")
    player = detail.get("player")
    if not isinstance(seq, int) or not isinstance(player, str):
        return False
    return any(seq in span for span in _run_spans(list(entries), player))


def _manual_and_script_counts(records: list[Mapping[str, Any]], seat: str) -> tuple[int, int]:
    """FD10 (c): `manual_calls`/`script_actions` - `seat`'s own committing or
    refused action rows (`detail.allowed` is `True` or `False`), split by
    `script_driven`."""
    manual = script = 0
    for record in records:
        payload = _payload(record)
        detail = payload.get("detail") or {}
        if detail.get("player") != seat or detail.get("allowed") not in (True, False):
            continue
        if script_driven(record, records):
            script += 1
        else:
            manual += 1
    return manual, script


def _is_adhoc_script_id(script_id: Any) -> bool:
    return isinstance(script_id, str) and script_id.startswith(ADHOC_SCRIPT_ID_PREFIX)


def _adhoc_counts(records: list[Mapping[str, Any]], seat: str) -> tuple[int, int]:
    """FD10 (c): `adhoc_staged` - `script_staged` rows this seat wrote in
    this game - and `adhoc_runs` - `script_run` finish rows of one of this
    seat's own ad-hoc scripts, told apart from a registered one by its id's
    own `ADHOC_SCRIPT_ID_PREFIX` alone, with no second corpus lookup."""
    staged = runs = 0
    for record in records:
        payload = _payload(record)
        detail = payload.get("detail") or {}
        if detail.get("seat") != seat:
            continue
        action = payload.get("action")
        if action == SCRIPT_STAGED_ACTION:
            staged += 1
        elif action == SCRIPT_RUN_ACTION and _is_adhoc_script_id(detail.get("script_id")):
            runs += 1
    return staged, runs


def _manual_chains(records: list[Mapping[str, Any]], seat: str) -> tuple[dict[str, Any], ...]:
    """FD10 (c): every run of `MANUAL_CHAIN_MIN` or more consecutive manual
    action rows `seat` made, within one turn and step, with no opponent
    action between - `{seqs, kinds, turn, step}` each, oldest first.

    Walked over the *whole* ledger, not `seat`'s own rows pre-filtered out:
    an opponent's own committing or refused row breaks a run in progress (CR
    608.2d's own turn-taking is what "no opponent action between" means),
    and so does one of `seat`'s own rows that is itself script-driven - a
    script call sitting between two manual ones is not itself part of a
    "manual" run, whatever else surrounds it. A turn/step change also starts
    a fresh run: `MANUAL_CHAIN_MIN` is judged "within one turn and step", so
    a run spanning two never forms even with nothing between."""
    chains: list[dict[str, Any]] = []
    seqs: list[int] = []
    kinds: list[str] = []
    position: tuple[Any, Any] | None = None

    def flush() -> None:
        if len(seqs) >= MANUAL_CHAIN_MIN:
            chains.append(
                {
                    "seqs": tuple(seqs),
                    "kinds": tuple(kinds),
                    "turn": position[0] if position else None,
                    "step": position[1] if position else None,
                }
            )
        seqs.clear()
        kinds.clear()

    for record in records:
        payload = _payload(record)
        detail = payload.get("detail") or {}
        if detail.get("allowed") not in (True, False):
            continue
        seq = record.get("seq")
        if not isinstance(seq, int):
            continue
        if detail.get("player") != seat:
            flush()
            position = None
            continue
        if script_driven(record, records):
            flush()
            position = None
            continue
        here = (payload.get("turn"), payload.get("step"))
        if seqs and here != position:
            flush()
        position = here
        seqs.append(seq)
        kinds.append(str(payload.get("action")))
    flush()
    return tuple(chains)


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
    all_records = list(records)
    plays_total, plays_move_bearing, plays_advice = _corpus_counts(corpus)
    offered, used, denied, passed_over = _offer_counts(all_records, game_id, seat)
    added, updated, reprioritized, added_ids, updated_ids = _admitted_counts(admitted)
    manual_calls, script_actions = _manual_and_script_counts(all_records, seat)
    adhoc_staged, adhoc_runs = _adhoc_counts(all_records, seat)
    manual_chains = _manual_chains(all_records, seat)
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
        "manual_calls": manual_calls,
        "script_actions": script_actions,
        "adhoc_staged": adhoc_staged,
        "adhoc_runs": adhoc_runs,
        "manual_chains": manual_chains,
    }
