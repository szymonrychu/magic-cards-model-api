"""The scorecard: what a run of games actually says about how a seat played.

    uv run python scripts/eval_report.py                 # the configured database
    uv run python scripts/eval_report.py --ledger run.json
    uv run python scripts/eval_report.py --game <id> --game <id> --json

Read straight off the two tables the api already writes (`db.py`): `games`
for which games exist and when they ended, `records` for every action any
seat attempted and what came of it. Nothing is recomputed from a board, and
nothing is inferred from final life totals - the ledger's own `game_over` row
is the witness that a game ended and who won, exactly as `review.ending_of`
treats it.

## What it reports, and why each one

- **Finish rate.** 2 of the 15 recorded games had ever reached an end state
  when this was written. Every other number on this page is conditional on a
  game finishing, so this is the first one: a 90% win rate over the two games
  that finished says nothing at all.
- **Winner, and turns to end.** Who won, and how long it took, off the
  `game_over` row's own `winner`/`drawn` and the turn it was written on.
- **Actions per game.** How much a seat actually did. A seat that passes its
  whole turn and one that plays four spells look identical in a win rate.
- **Reasoning coverage.** What share of a seat's attempts said *why*
  (`audit.record`'s `reasoning`). Two figures, because
  `records.require_reasoning` exempts a pass ("a pass is 'I have nothing to
  do'"): the overall share, and the share over everything that is not a pass.
- **Refusal rate, split into rules errors and stale races.** This is the
  metric that can actually gate a change today - see the sizing section
  below - and the split is the whole point of it. A refusal because the seat
  misread a rule is a defect in how it plays; a refusal because the board
  moved under a decision it had already read is a defect in the harness, and
  improving the agent will never fix it. `detail.stale` is the flag that
  tells them apart. **A ledger recorded before that flag existed carries
  none, and those refusals are counted as `unknown` rather than as rules
  errors** - inventing an agent defect out of a missing field is exactly the
  kind of arithmetic-dressed-as-judgement this repo refuses elsewhere. The
  report says which of `measured`/`partial`/`unknown` its own ledger was.
- **And a rules error is only a refusal of something never offered.** A
  board being current is not enough to blame the seat: the api's own
  enumeration (`GET .../legal-actions`, written onto every row as
  `legal_actions_before`) may have listed the very action it then refused.
  `detail.offered` says which (`records.offered_action`). A refusal of an
  offered action is either the api contradicting itself or the seat
  mis-announcing something real, and the ledger cannot say which, so it gets
  its own `offered` bucket and is counted as neither. `offer_split` reports
  `measured`/`partial`/`unknown` the same way `stale_split` does.
- **Per-script tally.** One row per `script_run` row's `(script_id,
  opposing_archetype_asserted)`: runs, completion rate (exit
  `chain_complete` over runs), every exit seen, the win rate of the finished
  games in which that seat completed the chain at least once, resumes, and
  mean wall time. Advise runs - moves proposed to a human, not taken - are
  tallied apart under `advise`, so a proposal nobody accepted never reads as
  the seat's own play.
- **Plan-declared usage, by `for_turn`.** How many `plan_declared` rows each
  seat wrote, split `current` vs `next` (S2 A1's plan-ahead, `declare_plan(
  for_turn="next")`). A raw count of a thing that happened, exactly the "raw
  pairing" the section below argues for - never a judgement of whether the
  plan declared was any good, or whether it later held up. A row from before
  `for_turn` existed carries no such key and is counted as `current`,
  matching the field's own schema default rather than being dropped.

## What it deliberately does not grade

`review.py` refuses to grade a declared plan, and this refuses with it: "a
number is something a sceptic argues with (about the metric, the sample, the
weighting), while the raw pairing is not". A `plan_declared` row is still not
an action and not an attempt, and contributes nothing to any *outcome*-scored
metric here - not win rate, not refusal rate, not reasoning coverage - and is
never graded for whether the plan itself was correct or good. It is counted
once, for bare usage (`current` vs `next`), which is exactly the raw pairing
this section argues for rather than a judgement about it. Everything else
this does score is an *outcome* - who won, how many turns, how many attempts
were refused - which is arithmetic over facts the server wrote down, not a
judgement about whether a prediction came true.

## Sizing, printed rather than assumed

Every rate carries a 95% interval from a Beta posterior with a uniform prior
(`beta_interval`), sampled the way `mtg-decks/scripts/gauntlet_ev.py` samples
its own - "this is a reported interval on a row, not the quantity a verdict
turns on" - but seeded, so rerunning the report on one gauntlet does not move
the number it is being compared against.

A bare percentage is never printed, because the numbers a small run produces
are not what they look like. Roughly 20 games gives about +/-20pp on a win
rate; 50 gives about +/-14pp; resolving a 10pp A/B difference needs roughly
200 games *per arm*. Refusal rate and finish rate are the exception and the
reason this is worth running at 10 games at all: each carries hundreds of
attempts rather than one outcome per game, so their intervals are tight
immediately. Those are the two that can gate a change today, and the report
says so in its own output.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

#: The seed behind every sampled interval. Fixed, and that is the point: a
#: gauntlet re-reported must not move its own confidence bounds, or a reader
#: comparing two runs is reading Monte Carlo noise as signal.
INTERVAL_SEED = 20260910

#: Draws behind each interval. `gauntlet_ev.py` uses 4000 for the same reason
#: it gives - 200 resolved a 2.5th percentile off ten order statistics and
#: moved by more than a percentage point on the seed alone.
INTERVAL_SAMPLES = 4000

#: Every audited action that is a *move in a game*, as opposed to board setup
#: (`add_card`, `patch_card`, `tap`, `move_card`, `step`, `mana_pool`),
#: bookkeeping (`create_game`, `delete_game`, `game_reset`, `game_retired`,
#: `ai_seat`), or a statement about the game rather than a move in it
#: (`plan_declared`, `game_reflection`, `report_problem`, `game_over`).
#:
#: A positive list rather than a deny list, so a row this scorecard does not
#: understand is never silently counted as play - and never silently dropped
#: either: anything attributed to a seat and not named here lands in the
#: report's own `unclassified_actions`, where a reader can see it.
PLAY_ACTIONS = frozenset(
    {
        "activate",
        "activate_ability",
        "advance",
        "attack",
        "cast",
        "concede",
        "crew",
        "cycle",
        "decide",
        "declare_blockers",
        "foretell",
        "mulligan_declared",
        "opening_hand_action",
        "opening_hand_done",
        "pass_priority",
        "play_land",
        "plot",
        "resolve_top",
        "suspend",
        "target",
    }
)

#: Rows that are neither play nor a defect in it, and are never reported as
#: an unclassified surprise: board setup a harness did, and the statements
#: `review.py` refuses to grade.
NOT_PLAY = frozenset(
    {
        "add_card",
        "ai_seat",
        "create_game",
        "decks_loaded",
        "delete_game",
        "game_over",
        "game_reflection",
        "game_reset",
        "game_retired",
        "hands_drawn",
        "mana_pool",
        "move_card",
        "patch_card",
        "patch_player",
        "plan_declared",
        "pregame_overruled",
        "pregame_settle",
        "pregame_settled",
        "report_problem",
        "script_run",
        "script_run_offered",
        "script_run_started",
        "shuffled",
        "state_based_actions",
        "step",
        "tap",
    }
)

#: The one action `records.require_reasoning` exempts, so it gets its own
#: coverage figure rather than dragging the headline one down for a rule the
#: api itself decided not to enforce.
PASS_SHAPED = "pass_priority"

GAME_OVER_ACTION = "game_over"

#: One finished play-script run (`audit.SCRIPT_RUN_ACTION`), and the exit
#: that says the whole chain ran.
SCRIPT_RUN_ACTION = "script_run"
CHAIN_COMPLETE_EXIT = "chain_complete"

#: `audit.PLAN_ACTION` (`app.py`'s `declare_plan`). Not a play action - see
#: `NOT_PLAY` and the "What it deliberately does not grade" section above -
#: but its `for_turn` (current vs next, S2 A1's plan-ahead) is still worth
#: counting as bare usage, so it gets its own branch ahead of the
#: `NOT_PLAY` skip, the same way `SCRIPT_RUN_ACTION` does.
PLAN_DECLARED_ACTION = "plan_declared"

#: `audit.ADVICE_ACTION`, named locally like every other action constant
#: above rather than imported - this module reads `audit`'s rows, never its
#: code.
ADVICE_ACTION = "advice"


# -- the ledger --------------------------------------------------------------


@dataclass(frozen=True)
class GameRows:
    """One game as the two tables hold it: the `games` row's own scalars, and
    every `records` row for it, oldest first."""

    game_id: str
    players: list[str]
    rows: list[dict[str, Any]]
    created_at: str | None = None
    archived_at: str | None = None

    def payloads(self) -> Iterator[dict[str, Any]]:
        for entry in self.rows:
            payload = entry.get("payload") if isinstance(entry, dict) else None
            if isinstance(payload, dict):
                yield payload

    def to_json(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "players": list(self.players),
            "created_at": self.created_at,
            "archived_at": self.archived_at,
            "rows": self.rows,
        }


def load_ledger_file(path: str | Path) -> list[GameRows]:
    """A ledger captured to disk - what `--dump` writes, and what lets the
    report be run against a finished gauntlet with no database in reach."""
    raw = json.loads(Path(path).read_text())
    return [
        GameRows(
            game_id=str(entry["game_id"]),
            players=[str(name) for name in entry.get("players") or []],
            rows=list(entry.get("rows") or []),
            created_at=entry.get("created_at"),
            archived_at=entry.get("archived_at"),
        )
        for entry in raw
    ]


def ledger_from_store(store: Any, *, game_ids: Sequence[str] | None = None) -> list[GameRows]:
    """The ledger through the repository - the same two tables, reached the
    way the running api reaches them, so this works against `InMemory
    Repository` as well as postgres.

    `store.archived` rather than `store.get` on purpose: it is the one read
    that sees past the soft delete (`ArchivedGame`'s own docstring), and a
    finished gauntlet's games have usually been ended.
    """
    ids = list(game_ids) if game_ids is not None else list(store.ids())
    ledger: list[GameRows] = []
    for game_id in ids:
        archived = store.archived(game_id)
        rows = list(store.records(game_id))
        players = sorted(set((archived.seat_uids or {}).values())) if archived else []
        ledger.append(
            GameRows(
                game_id=game_id,
                players=players or _players_from_rows(rows),
                rows=rows,
                created_at=archived.created_at if archived else None,
                archived_at=archived.deleted_at if archived else None,
            )
        )
    return ledger


def ledger_from_database(
    database_url: str, *, game_ids: Sequence[str] | None = None
) -> list[GameRows]:
    """The ledger straight off `games` and `records`.

    Not `ledger_from_store`: `GameRepository.ids()` lists only *live* games
    (`repository_postgres.ids` filters on `archived_at IS NULL`), and a
    gauntlet's games are ended when they finish. There is no accessor for
    "every game that ever existed", and the two tables are right here, so
    they are read directly - which is also what makes this runnable against a
    database nothing is currently serving.
    """
    from sqlalchemy import select

    from magic_cards_model_api.db import games, make_engine, records

    engine = make_engine(database_url)
    ledger: list[GameRows] = []
    with engine.connect() as conn:
        game_query = select(games.c.id, games.c.seats, games.c.created_at, games.c.archived_at)
        if game_ids:
            game_query = game_query.where(games.c.id.in_(list(game_ids)))
        for game_id, seats, created_at, archived_at in conn.execute(game_query).all():
            rows = [
                {"seq": seq, "payload": payload}
                for seq, payload in conn.execute(
                    select(records.c.seq, records.c.payload)
                    .where(records.c.game_id == game_id)
                    .order_by(records.c.seq)
                ).all()
            ]
            players = sorted(set((seats or {}).values())) or _players_from_rows(rows)
            ledger.append(
                GameRows(
                    game_id=str(game_id),
                    players=players,
                    rows=rows,
                    created_at=created_at.isoformat() if created_at else None,
                    archived_at=archived_at.isoformat() if archived_at else None,
                )
            )
    engine.dispose()
    return ledger


def _players_from_rows(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Who played, when the `games` row's `seats` cannot say - a ledger dumped
    from a game whose row is gone, or an in-memory run with no seats
    registered. `create_game` names them; failing that, whoever acted does."""
    named: list[str] = []
    seen: set[str] = set()
    for entry in rows:
        payload = entry.get("payload") or {}
        detail = payload.get("detail") or {}
        if payload.get("action") == "create_game":
            for name in detail.get("players") or []:
                if name not in seen:
                    seen.add(str(name))
                    named.append(str(name))
        player = detail.get("player")
        if isinstance(player, str) and player not in seen:
            seen.add(player)
            named.append(player)
    return named


# -- turn instances and latency (S2 A7) ---------------------------------------


def _parse_ts(payload: Mapping[str, Any]) -> datetime | None:
    """`payload["ts"]` as a `datetime`, or `None` for a row with no
    parseable timestamp - never raised, since a ledger row with a malformed
    or absent `ts` must drop out of a latency measurement rather than crash
    the whole report."""
    ts = payload.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


@dataclass
class _TurnInstance:
    """One player-turn's rows, oldest first - see `_turn_instances`."""

    active_player: str
    rows: list[dict[str, Any]] = field(default_factory=list)


def _turn_instances(payloads: Sequence[dict[str, Any]]) -> list[_TurnInstance]:
    """One game's rows split into turn instances - one per player-turn
    actually begun (CR 500.1), oldest first - the unit `own_turn_first_
    action_ms` measures over.

    **Identity.** A row's own `active_player`/`turns_taken` - what `audit.
    record` stamps at *write* time - is the primary identity: `turns_taken`
    (`TurnMachine.turns_taken`) goes up once per turn that actually begins,
    extra turns (CR 500.7) included, unlike `turn_number` (CR 500.1's count
    of rounds of the table), which CR 500.7 leaves alone for one. So an
    ordinary turn and an extra turn granted to the same seat right after it
    share one `(turn_number, active_player)` pair but never one
    `turns_taken`, and grouping on `(active_player, turns_taken)` is what
    tells the two apart - the plain pair alone cannot, since it is
    identical for both.

    A `plan_declared` row planning ahead (`for_turn: "next"`,
    `schemas.DeclarePlanIn`) is written *during the other seat's turn*, for
    a turn that has not started yet. Its own `active_player`/`turns_taken`
    are still the physical, write-time ones - the *target* turn instead
    lives in `detail.turns_taken` (`app.py`'s `declare_plan` docstring
    spells out the split) - so grouping on the write-time pair keeps a
    plan-ahead row out of the seat's own future turn: it lands in the turn
    it was actually written in, same as everything else.

    **Old rows.** A row recorded before this stamp existed carries no
    `turns_taken` at all, and falls back to the `(turn_number,
    active_player)` pair - the grouping every row used before the stamp.
    This is approximate for exactly the case above: an old ledger's own
    extra turn, sharing its ordinary turn's pair, still collapses into it,
    since the pair is all a row that old carries. A ledger with the stamp
    resolves that case correctly; one without it reads as it always did.

    **What is dropped.** The pregame (`turn_number` 0 - `audit.record`'s own
    docstring on why pregame carries no phase applies equally here) is not
    one of a seat's own turns and is never one of these instances; counting
    it would attribute an opening-hand mulligan decision to "the seat's
    first turn" latency, which it is not answering. A row that predates the
    `active_player` stamp and carries neither signal cannot be placed in
    any instance at all and is dropped rather than guessed at from
    `detail.player` - a row an opponent's own pass or step also carries a
    `player`-shaped detail on, so that field is not a safe stand-in for
    whose turn it was.
    """
    groups: dict[Any, _TurnInstance] = {}
    order: list[Any] = []
    for payload in payloads:
        turn_number, active = payload.get("turn"), payload.get("active_player")
        if not isinstance(turn_number, int) or turn_number < 1 or not isinstance(active, str):
            continue  # pregame, or a row with no turn-identity signal at all
        turns_taken = payload.get("turns_taken")
        key: Any = (
            ("stamped", active, turns_taken)
            if isinstance(turns_taken, int)
            else ("pair", turn_number, active)
        )
        if key not in groups:
            groups[key] = _TurnInstance(active_player=active)
            order.append(key)
        groups[key].rows.append(payload)
    return [groups[key] for key in order]


def _ms_summary(values: Sequence[float]) -> dict[str, Any]:
    """`{p50, max, n}` over a list of millisecond durations - `None`/`None`/0
    for an empty one, so an eval run with nothing to measure yet renders
    cleanly instead of raising or printing a fabricated zero."""
    if not values:
        return {"p50": None, "max": None, "n": 0}
    return {
        "p50": round(statistics.median(values), 1),
        "max": round(max(values), 1),
        "n": len(values),
    }


def _own_turn_latencies(payloads: Sequence[dict[str, Any]]) -> dict[str, list[float]]:
    """`{seat: [own_turn_first_action_ms, ...]}` for one game.

    Per turn instance (`_turn_instances`) whose `active_player` is that
    seat: the time from the instance's own first row (oldest first, so
    `rows[0]`) to the first row in it where `detail.player` is that same
    seat - any action, plan, or pass, exactly as the metric is specified,
    which is deliberately wider than `PLAY_ACTIONS` (a `plan_declared` row
    filed *in* the seat's own turn counts; one planning ahead from the
    other seat's turn is never in this instance to begin with - see
    `_turn_instances`). A turn instance with no parseable first-row
    timestamp, or in which the seat never has a row of its own (the turn
    ended before it acted at all - a concession, say), contributes nothing
    rather than a fabricated latency.
    """
    out: dict[str, list[float]] = {}
    for instance in _turn_instances(payloads):
        first_ts = _parse_ts(instance.rows[0])
        if first_ts is None:
            continue
        own_ts = next(
            (
                parsed
                for r in instance.rows
                if (r.get("detail") or {}).get("player") == instance.active_player
                and (parsed := _parse_ts(r)) is not None
            ),
            None,
        )
        if own_ts is None:
            continue
        latency_ms = (own_ts - first_ts).total_seconds() * 1000.0
        out.setdefault(instance.active_player, []).append(latency_ms)
    return out


def _advice_latencies(
    payloads: Sequence[dict[str, Any]],
) -> dict[str, tuple[list[float], list[float]]]:
    """`{seat: (drafted_ms, undrafted_ms)}` for one game's `advice` rows.

    Timed from the ledger row that gave the advised seat priority - the row
    whose own `priority_holder` (stamped on every row, S2 A7) first reads as
    that seat after a row where it did not, tracked in one forward pass
    exactly the way `turn`/`active_player` already answer "whose turn" for
    `_own_turn_latencies`. This is the sound signal `audit.py`'s own S2 A7
    comment on `priority_holder` argues for over guessing a window from an
    action's name and outcome: the row already carries the fact.

    A ledger with no `priority_holder` on any row (recorded before S2 A7)
    never sets `current_holder`, so every `advice` row in it is silently
    excluded rather than timed against a guess - `_ms_summary`'s `n=0`
    is the honest report of that.

    `detail.drafted_at_seq` (a sibling S2 task's `AdviceIn.drafted_at_seq`)
    splits the two buckets: not-`None` is drafted, `None` or absent is
    undrafted.
    """
    drafted: dict[str, list[float]] = {}
    undrafted: dict[str, list[float]] = {}
    current_holder: str | None = None
    holder_since: datetime | None = None
    for payload in payloads:
        holder = payload.get("priority_holder")
        ts = _parse_ts(payload)
        if isinstance(holder, str) and holder != current_holder:
            current_holder = holder
            holder_since = ts
        if payload.get("action") != ADVICE_ACTION:
            continue
        detail = payload.get("detail") or {}
        player = detail.get("player")
        if not isinstance(player, str) or ts is None:
            continue
        if current_holder != player or holder_since is None:
            continue
        latency_ms = (ts - holder_since).total_seconds() * 1000.0
        bucket = drafted if detail.get("drafted_at_seq") is not None else undrafted
        bucket.setdefault(player, []).append(latency_ms)
    return {
        name: (drafted.get(name, []), undrafted.get(name, []))
        for name in set(drafted) | set(undrafted)
    }


# -- intervals ---------------------------------------------------------------


def beta_interval(successes: int, trials: int) -> dict[str, Any]:
    """A 95% central interval for a rate, from `Beta(1 + k, 1 + n - k)`.

    Uniform prior, because the alternative is to seed the answer with an
    opinion about how often a seat *should* win. Sampled rather than solved,
    following `mtg-decks/scripts/gauntlet_ev.py::_beta_ci95` - the exact
    alternative is an incomplete-beta inverse, and this is a reported
    interval on a row rather than the quantity a verdict turns on - but with
    a fixed seed, so the same ledger reports the same bounds every time.

    `point` is what was observed, `k / n`, and is `None` at zero trials
    because nothing was observed. `mean` is the posterior mean, which is
    always inside the interval and is what a reader should quote when `k` is
    0 or `n`: 0 wins in 4 games is not a 0% win rate, it is "somewhere under
    a half, and the interval says how far under".
    """
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError(f"{successes} successes out of {trials} trials is not a rate")
    alpha, beta = 1.0 + successes, 1.0 + trials - successes
    rng = random.Random(INTERVAL_SEED)
    draws = sorted(rng.betavariate(alpha, beta) for _ in range(INTERVAL_SAMPLES))
    low = draws[int(0.025 * (INTERVAL_SAMPLES - 1))]
    high = draws[int(0.975 * (INTERVAL_SAMPLES - 1))]
    return {
        "successes": successes,
        "trials": trials,
        "point": (successes / trials) if trials else None,
        "mean": alpha / (alpha + beta),
        "low": low,
        "high": high,
        "half_width": (high - low) / 2.0,
    }


def every_rate(report: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Every interval-bearing rate anywhere in a report - what the "never a
    bare percentage" rule is checked against."""

    def walk(node: Any) -> Iterator[dict[str, Any]]:
        if isinstance(node, dict):
            if {"low", "high", "trials", "mean"} <= set(node):
                yield node
                return
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    yield from walk(report)


# -- the report ---------------------------------------------------------------


@dataclass
class _SeatTally:
    games: int = 0
    finished: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    actions: int = 0
    attempts: int = 0
    reasoned: int = 0
    non_pass_attempts: int = 0
    non_pass_reasoned: int = 0
    refused: int = 0
    rules: int = 0
    offered: int = 0
    stale: int = 0
    unknown: int = 0
    unchecked: int = 0
    unclassified: dict[str, int] = field(default_factory=dict)
    by_invoker: dict[str, int] = field(default_factory=dict)
    armed_fired: int = 0
    plan_declared: dict[str, int] = field(default_factory=lambda: {"current": 0, "next": 0})
    own_turn_first_action_ms: list[float] = field(default_factory=list)
    advice_drafted_ms: list[float] = field(default_factory=list)
    advice_undrafted_ms: list[float] = field(default_factory=list)


def ending_of(payloads: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """The ledger's own `game_over` row, or `None`. Same witness
    `review.ending_of` uses, and for the same reason: it was written the
    moment `Game.is_over` first read true, by the build that could read the
    board."""
    found = None
    for payload in payloads:
        if payload.get("action") == GAME_OVER_ACTION:
            found = payload
    return found


def build_report(
    games: Sequence[GameRows], *, edges: Mapping[str, Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """Every metric, over `games`. Pure: the same ledger always produces the
    same report, intervals included, for a fixed `edges`.

    `edges` is `scripts.watch_game.watch`'s own return value, one per game id
    - the edge that ended *watching* that game (`game_over`, `timeout`,
    `stalled`, `report`, `stopped`), which is not stored in the ledger itself
    and so cannot be recovered from `games`/`records` alone. Passed by
    `eval_gauntlet.run`, which has it in hand; a bare `--database-url` read
    (or a captured `--dump`) has no such thing and reports `"unknown"`
    instead of guessing. Never used to call an unfinished game finished - it
    only explains *why* one is not.
    """
    seats: dict[str, _SeatTally] = {}
    unclassified: dict[str, int] = {}
    finished_ids: list[str] = []
    unfinished_ids: list[str] = []
    unfinished_detail: dict[str, dict[str, Any]] = {}
    turns: list[int] = []
    flagged_refusals = 0
    unflagged_refusals = 0
    checked_refusals = 0
    unchecked_refusals = 0

    for game in games:
        payloads = list(game.payloads())
        ending = ending_of(payloads)
        for name in game.players:
            seats.setdefault(name, _SeatTally()).games += 1
        if ending is not None:
            finished_ids.append(game.game_id)
            detail = ending.get("detail") or {}
            winner = detail.get("winner")
            drawn = bool(detail.get("drawn"))
            if ending.get("turn") is not None:
                turns.append(int(ending["turn"]))
            for name in game.players:
                tally = seats[name]
                tally.finished += 1
                if drawn or winner is None:
                    tally.draws += 1
                elif winner == name:
                    tally.wins += 1
                else:
                    tally.losses += 1
        else:
            unfinished_ids.append(game.game_id)
            edge = (edges or {}).get(game.game_id)
            unfinished_detail[game.game_id] = dict(edge) if edge else {"edge": "unknown"}

        for name, latencies in _own_turn_latencies(payloads).items():
            if name in seats:
                seats[name].own_turn_first_action_ms.extend(latencies)
        for name, (drafted_ms, undrafted_ms) in _advice_latencies(payloads).items():
            if name in seats:
                seats[name].advice_drafted_ms.extend(drafted_ms)
                seats[name].advice_undrafted_ms.extend(undrafted_ms)

        for payload in payloads:
            action = str(payload.get("action") or "")
            detail = payload.get("detail") or {}
            player = detail.get("player")
            if not isinstance(player, str) or player not in seats:
                continue
            if action == SCRIPT_RUN_ACTION:
                # `by_invoker`: who started each of this seat's finished
                # script runs - `mcp` (its own `run_script` calls), `cli`
                # (`mcp-seat script run`, most often an operator staging one),
                # or (S2) `armed` (`playscripts.arming.arm`, fired on its own).
                invoker = str(detail.get("invoked_via") or "unknown")
                tally = seats[player]
                tally.by_invoker[invoker] = tally.by_invoker.get(invoker, 0) + 1
                # `armed_fired` (S2): an armed run that actually did something
                # - moved the board (`moves_done > 0`) for a seat, or proposed
                # a move (`exit == "advised"`) for an advisor, whose own
                # `moves_done` is always 0 (advice never moves the board).
                if invoker == "armed":
                    moves_done = detail.get("moves_done")
                    fired = (isinstance(moves_done, int) and moves_done > 0) or (
                        bool(detail.get("advise")) and detail.get("exit") == "advised"
                    )
                    tally.armed_fired += int(fired)
                continue
            if action == PLAN_DECLARED_ACTION:
                # Usage, not a grade: which `for_turn` was declared, never
                # whether the plan itself was any good. A row with no
                # `for_turn` key at all predates the field and is counted as
                # `current` - `schemas.DeclarePlanIn.for_turn`'s own default.
                for_turn = detail.get("for_turn")
                if for_turn not in ("current", "next"):
                    for_turn = "current"
                tally = seats[player]
                tally.plan_declared[for_turn] = tally.plan_declared.get(for_turn, 0) + 1
                continue
            if action in NOT_PLAY:
                continue
            if action not in PLAY_ACTIONS:
                unclassified[action] = unclassified.get(action, 0) + 1
                continue
            tally = seats[player]
            tally.actions += 1
            allowed = detail.get("allowed")
            if not isinstance(allowed, bool):
                # `allowed: None` is a *pause* - a question the engine asked
                # part way through, not an attempt it ruled on. Counting one
                # as a refusal would report the harness's own decision
                # plumbing as the seat playing badly.
                continue
            tally.attempts += 1
            reasoned = bool(str(payload.get("reasoning") or "").strip())
            tally.reasoned += int(reasoned)
            if action != PASS_SHAPED:
                tally.non_pass_attempts += 1
                tally.non_pass_reasoned += int(reasoned)
            if allowed:
                continue
            tally.refused += 1
            stale = detail.get("stale")
            offered = detail.get("offered")
            if stale is True:
                tally.stale += 1
                flagged_refusals += 1
            elif stale is False:
                # The engine ruled, on a board this seat had named. Which of
                # the two facts that leaves depends on whether the api had
                # offered the action in the first place (`detail.offered`,
                # `audit._with_offered`): a refusal of something never
                # offered is the seat asking for what was not there, and
                # nothing else is.
                flagged_refusals += 1
                if offered is True:
                    tally.offered += 1
                    checked_refusals += 1
                elif offered is False:
                    tally.rules += 1
                    checked_refusals += 1
                else:
                    tally.unchecked += 1
                    unchecked_refusals += 1
            else:
                tally.unknown += 1
                unflagged_refusals += 1

    if flagged_refusals and unflagged_refusals:
        stale_split = "partial"
    elif flagged_refusals:
        stale_split = "measured"
    else:
        stale_split = "unknown"
    if checked_refusals and unchecked_refusals:
        offer_split = "partial"
    elif checked_refusals:
        offer_split = "measured"
    else:
        offer_split = "unknown"

    return {
        "games": len(games),
        "finished": len(finished_ids),
        "finish_rate": beta_interval(len(finished_ids), len(games)),
        "unfinished": unfinished_ids,
        "unfinished_detail": unfinished_detail,
        "turns_to_end": _spread(turns),
        "seats": {name: _seat_report(tally) for name, tally in sorted(seats.items())},
        "unclassified_actions": unclassified,
        "stale_split": stale_split,
        "offer_split": offer_split,
        "scripts": script_tally(games),
    }


@dataclass
class _ScriptTally:
    runs: int = 0
    completed: int = 0
    exits: dict[str, int] = field(default_factory=dict)
    resumes: int = 0
    wall_ms: list[int] = field(default_factory=list)
    completed_finished_games: int = 0
    completed_won_games: int = 0


def script_tally(games: Sequence[GameRows]) -> list[dict[str, Any]]:
    """One entry per `(script_id, opposing_archetype_asserted)` over every
    `script_run` row in `games`, ordered by script then archetype (none
    first). The seat's own runs are the entry's top-level figures; advise
    runs are the same figures under `advise`.

    `win_rate_when_completed` is over (game, seat) pairs, not runs: a finished
    game in which that seat's run of this script exited `chain_complete` at
    least once is one trial, and a win for that seat is one success. A draw
    is a trial and not a success; an unfinished game is neither.
    """
    tallies: dict[tuple[str, str | None, bool], _ScriptTally] = {}
    for game in games:
        payloads = list(game.payloads())
        ending = ending_of(payloads)
        completed_by: set[tuple[tuple[str, str | None, bool], str]] = set()
        for payload in payloads:
            if payload.get("action") != SCRIPT_RUN_ACTION:
                continue
            detail = payload.get("detail") or {}
            archetype = detail.get("opposing_archetype_asserted")
            key = (
                str(detail.get("script_id")),
                None if archetype is None else str(archetype),
                bool(detail.get("advise")),
            )
            tally = tallies.setdefault(key, _ScriptTally())
            tally.runs += 1
            exit_name = str(detail.get("exit") or "unknown")
            tally.exits[exit_name] = tally.exits.get(exit_name, 0) + 1
            tally.resumes += int(detail.get("resumes") or 0)
            if detail.get("wall_ms") is not None:
                tally.wall_ms.append(int(detail["wall_ms"]))
            if exit_name == CHAIN_COMPLETE_EXIT:
                tally.completed += 1
                completed_by.add((key, str(detail.get("seat"))))
        if ending is None:
            continue
        ending_detail = ending.get("detail") or {}
        winner = None if ending_detail.get("drawn") else ending_detail.get("winner")
        for key, seat in completed_by:
            tallies[key].completed_finished_games += 1
            tallies[key].completed_won_games += int(winner is not None and winner == seat)

    pairs = sorted(
        {key[:2] for key in tallies}, key=lambda pair: (pair[0], pair[1] is not None, pair[1] or "")
    )
    return [
        {
            "script_id": script_id,
            "opposing_archetype_asserted": archetype,
            **_script_block(tallies.get((script_id, archetype, False), _ScriptTally())),
            "advise": _script_block(tallies.get((script_id, archetype, True), _ScriptTally())),
        }
        for script_id, archetype in pairs
    ]


def _script_block(tally: _ScriptTally) -> dict[str, Any]:
    return {
        "runs": tally.runs,
        "completion": beta_interval(tally.completed, tally.runs),
        "exits": dict(sorted(tally.exits.items())),
        "win_rate_when_completed": beta_interval(
            tally.completed_won_games, tally.completed_finished_games
        ),
        "resumes": tally.resumes,
        "wall_ms_mean": round(statistics.fmean(tally.wall_ms), 1) if tally.wall_ms else None,
    }


def _seat_report(tally: _SeatTally) -> dict[str, Any]:
    decided = tally.wins + tally.losses + tally.draws
    return {
        "games": tally.games,
        "finished": tally.finished,
        "wins": tally.wins,
        "losses": tally.losses,
        "draws": tally.draws,
        "win_rate": beta_interval(tally.wins, decided),
        "finish_rate": beta_interval(tally.finished, tally.games),
        "actions": tally.actions,
        "actions_per_game": (tally.actions / tally.games) if tally.games else 0.0,
        "attempts": tally.attempts,
        "reasoning": beta_interval(tally.reasoned, tally.attempts),
        "reasoning_beyond_passes": beta_interval(tally.non_pass_reasoned, tally.non_pass_attempts),
        "refusals": beta_interval(tally.refused, tally.attempts),
        # Five mutually exclusive buckets that sum to `refused`, each named
        # for what it actually is: `rules` the seat's own misreading,
        # `offered` a refusal of something this api had listed as legal,
        # `stale` a lost race, and the last two the rows this ledger could
        # not check either question on.
        "refusal_split": {
            "rules": tally.rules,
            "offered": tally.offered,
            "stale": tally.stale,
            "unknown": tally.unknown,
            "unchecked": tally.unchecked,
        },
        "rules_error_rate": beta_interval(tally.rules, tally.attempts),
        "offered_refusal_rate": beta_interval(tally.offered, tally.attempts),
        "stale_race_rate": beta_interval(tally.stale, tally.attempts),
        # S1 T3: who started each of this seat's finished script runs -
        # `mcp` (`mcp.run_script`) or `cli` (`mcp-seat script run`, most
        # often an operator staging one rather than the seat itself).
        "scripts": {
            "by_invoker": dict(sorted(tally.by_invoker.items())),
            # S2: how many of this seat's `armed` runs actually fired -
            # moved the board (a seat) or proposed a move (an advisor).
            "armed_fired": tally.armed_fired,
        },
        # S2 A1: how many `plan_declared` rows this seat wrote, split by
        # `for_turn` - bare usage of plan-ahead, never a grade of what was
        # declared (see "What it deliberately does not grade" above).
        "plan_declared": {
            "current": tally.plan_declared.get("current", 0),
            "next": tally.plan_declared.get("next", 0),
        },
        # S2 A7: how long the stall this task exists to catch actually took
        # to happen again, or did not - see `_own_turn_latencies` for what a
        # "turn" is here and why a plan-ahead row cannot inflate it.
        "own_turn_first_action_ms": _ms_summary(tally.own_turn_first_action_ms),
        # S2 A7: how long a human sat on an unactioned proposal, split by
        # whether it was ever drafted into a real request
        # (`detail.drafted_at_seq`) - see `_advice_latencies`.
        "advice": {
            "latency_ms": {
                "drafted": _ms_summary(tally.advice_drafted_ms),
                "undrafted": _ms_summary(tally.advice_undrafted_ms),
            }
        },
    }


def _spread(values: Sequence[int]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 2),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


# -- rendering ----------------------------------------------------------------


#: What a rate line means, said once at the top rather than guessed at every
#: row. It matters most where it looks wrong: 0 refusals in 38 attempts prints
#: an observed 0.0% beside a 95% interval whose lower bound is *above* zero,
#: because a uniform prior over 38 observations does not conclude "never" - and
#: that is the honest reading, not a rendering bug.
RATE_LEGEND = (
    "  rates read: successes/trials, the observed share, then a 95 percent credible\n"
    "  interval. Quote the interval, never the share."
)


def _rate(rate: Mapping[str, Any], *, unit: str = "") -> str:
    """One rate, always with its interval. Never a bare percentage: a run this
    size does not support one, and printing it anyway is how a reader ends up
    quoting a coin flip with a decimal place."""
    if not rate["trials"]:
        return "no data (0 trials)"
    point = rate["point"] * 100.0
    low, high = rate["low"] * 100.0, rate["high"] * 100.0
    half = rate["half_width"] * 100.0
    return (
        f"{rate['successes']:>4}/{rate['trials']:<4}{unit:<9} obs {point:5.1f}%  "
        f"95% [{low:4.1f}, {high:5.1f}]  +/-{half:4.1f}pp"
    )


def _ms(summary: Mapping[str, Any]) -> str:
    """One `_ms_summary` (`{p50, max, n}`), rendered - "no data" at `n=0`
    rather than a fabricated 0ms, the same discipline `_rate` applies to a
    rate with no trials."""
    if not summary["n"]:
        return "no data (n=0)"
    return f"p50 {summary['p50']:.0f}ms  max {summary['max']:.0f}ms  (n={summary['n']})"


def _unfinished_reason(edge: Mapping[str, Any] | None) -> str:
    """One line: why this game has no `game_over` row. `edge` is
    `build_report`'s own `unfinished_detail` entry - `watch_game.watch`'s
    edge when `eval_gauntlet` supplied one, `{"edge": "unknown"}` when
    nothing did (a bare `--database-url`/`--ledger` read, with no run in
    hand to ask)."""
    if not edge:
        return "[unknown] no watcher edge recorded for this run"
    kind = str(edge.get("edge") or "unknown")
    if kind == "timeout":
        turn = edge.get("turn")
        return (
            f"[timeout] cut off by --max-seconds (turn {turn})"
            if turn is not None
            else "[timeout] cut off by --max-seconds"
        )
    if kind == "stalled":
        quiet = edge.get("quiet_seconds")
        return (
            f"[stalled] {quiet}s with no log activity"
            if quiet is not None
            else "[stalled] no log activity"
        )
    if kind == "report":
        return "[report] cut off by a filed problem report"
    if kind == "stopped":
        return "[stopped] watcher told to stop (should_stop)"
    if kind == "watcher_failed":
        return f"[watcher_failed] the watcher itself errored: {edge.get('error')}"
    return f"[{kind}] reason not one this report recognises"


def render(report: Mapping[str, Any]) -> str:
    """The scorecard as text. Everything a reader needs to judge whether a
    number here is worth acting on is on the page, including how big the run
    was and what a run that size can and cannot resolve."""
    lines: list[str] = []
    games = int(report["games"])
    lines.append(f"# eval scorecard - {games} game(s)")
    lines.append(RATE_LEGEND)
    lines.append("")
    lines.append("RUN")
    lines.append(f"  finished          {_rate(report['finish_rate'], unit=' games')}")
    spread = report["turns_to_end"]
    if spread["n"]:
        lines.append(
            f"  turns to end      mean {spread['mean']}  median {spread['median']}  "
            f"range {spread['min']}-{spread['max']}  (n={spread['n']})"
        )
    else:
        lines.append("  turns to end      no data (no game reached an end state)")
    if report["unfinished"]:
        lines.append(f"  unfinished        {', '.join(report['unfinished'])}")
        lines.append("                    not scored as a loss for either seat - only a finished")
        lines.append("                    game has a winner, loser, or draw; see why each was cut")
        lines.append("                    off below:")
        detail = report.get("unfinished_detail") or {}
        for game_id in report["unfinished"]:
            lines.append(f"    {game_id}  {_unfinished_reason(detail.get(game_id))}")
    if report["unclassified_actions"]:
        rows = ", ".join(
            f"{action} x{count}" for action, count in sorted(report["unclassified_actions"].items())
        )
        lines.append(f"  unclassified      {rows} (attributed to a seat, not scored as play)")
    lines.append("")

    for name, seat in report["seats"].items():
        lines.append(f"SEAT {name}")
        lines.append(
            f"  record            {seat['wins']}W-{seat['losses']}L-{seat['draws']}D "
            f"over {seat['finished']} finished of {seat['games']} played"
        )
        lines.append(f"  win rate          {_rate(seat['win_rate'], unit=' decided')}")
        lines.append(f"  finish rate       {_rate(seat['finish_rate'], unit=' games')}")
        lines.append(
            f"  actions           {seat['actions']} "
            f"({seat['actions_per_game']:.1f} per game, {seat['attempts']} ruled on)"
        )
        lines.append(f"  reasoning         {_rate(seat['reasoning'], unit=' attempts')}")
        lines.append(
            f"  reasoning (-pass) {_rate(seat['reasoning_beyond_passes'], unit=' attempts')}"
        )
        lines.append(f"  refusals          {_rate(seat['refusals'], unit=' attempts')}")
        split = seat["refusal_split"]
        unsplit = split["unknown"] + split["unchecked"]
        lines.append(
            f"    rules errors    {_rate(seat['rules_error_rate'], unit=' attempts')}"
            + ("" if unsplit == 0 else "  [see the splits below]")
        )
        lines.append(f"    api offered it  {_rate(seat['offered_refusal_rate'], unit=' attempts')}")
        lines.append(f"    stale races     {_rate(seat['stale_race_rate'], unit=' attempts')}")
        if split["unknown"]:
            lines.append(
                f"    unsplit         {split['unknown']} refusal(s) carry no detail.stale - "
                "not counted as either"
            )
        if split["unchecked"]:
            lines.append(
                f"    unchecked       {split['unchecked']} refusal(s) carry no detail.offered - "
                "not counted as either"
            )
        scripts_block = seat.get("scripts") or {}
        by_invoker = scripts_block.get("by_invoker") or {}
        if by_invoker:
            rows = ", ".join(f"{name}={count}" for name, count in sorted(by_invoker.items()))
            lines.append(f"  scripts by invoker {rows}")
        if by_invoker.get("armed"):
            lines.append(f"  armed fired        {scripts_block.get('armed_fired', 0)}")
        plan_declared = seat.get("plan_declared") or {}
        if plan_declared.get("current") or plan_declared.get("next"):
            lines.append(
                f"  plan declared      current={plan_declared.get('current', 0)} "
                f"next={plan_declared.get('next', 0)}"
            )
        lines.append(f"  own turn 1st action {_ms(seat['own_turn_first_action_ms'])}")
        advice_latency = seat["advice"]["latency_ms"]
        lines.append(f"  advice latency (drafted)   {_ms(advice_latency['drafted'])}")
        lines.append(f"  advice latency (undrafted) {_ms(advice_latency['undrafted'])}")
        lines.append("")

    lines.extend(_script_lines(report.get("scripts") or []))
    lines.append("")
    lines.extend(_stale_note(report))
    lines.append("")
    lines.extend(_offer_note(report))
    lines.append("")
    lines.extend(_sizing_note(report))
    return "\n".join(lines)


def _script_lines(scripts: Sequence[Mapping[str, Any]]) -> list[str]:
    """The per-script tally, one block per script and asserted archetype,
    with an advise block only where advise runs exist."""
    lines = ["SCRIPTS (per script and asserted opposing archetype; advise runs apart)"]
    if not scripts:
        lines.append("  no script runs recorded")
        return lines
    for entry in scripts:
        archetype = entry["opposing_archetype_asserted"] or "(no archetype asserted)"
        lines.append(f"  {entry['script_id']} vs {archetype}")
        lines.extend(_script_block_lines(entry, "seat"))
        if entry["advise"]["runs"]:
            lines.extend(_script_block_lines(entry["advise"], "advise"))
    return lines


def _script_block_lines(block: Mapping[str, Any], label: str) -> list[str]:
    exits = ", ".join(f"{name} x{count}" for name, count in block["exits"].items()) or "none"
    wall = "no data" if block["wall_ms_mean"] is None else f"{block['wall_ms_mean']} ms"
    return [
        f"    {label:<7}runs    {block['runs']}  exits {exits}",
        f"      completion    {_rate(block['completion'], unit=' runs')}",
        f"      win if done   {_rate(block['win_rate_when_completed'], unit=' games')}",
        f"      resumes       {block['resumes']}  mean wall {wall}",
    ]


def _stale_note(report: Mapping[str, Any]) -> list[str]:
    state = report["stale_split"]
    if state == "measured":
        return [
            "STALE SPLIT",
            "  measured: every refusal carries detail.stale, so rules errors and stale",
            "  races above are genuinely separate numbers.",
        ]
    if state == "partial":
        return [
            "STALE SPLIT",
            "  partial: some refusals carry detail.stale and some do not. The unsplit",
            "  ones are counted as neither - read the rules-error rate as a floor.",
        ]
    return [
        "STALE SPLIT",
        "  unknown: no refusal in this ledger carries detail.stale, so rules errors",
        "  and stale races cannot be told apart. Every refusal is counted as unsplit",
        "  rather than as a rules error - a missing field is not evidence of a defect.",
    ]


def _offer_note(report: Mapping[str, Any]) -> list[str]:
    """What the rules-error number is, and what it deliberately leaves out.

    `GET .../legal-actions` is this api's own claim that an action is legal
    for a seat right now, and `records.legal_actions_before` writes that
    claim onto the row of whatever the seat did next. A refusal of an action
    that enumeration never contained is the seat asking for something that
    was not on offer - the one refusal that is unambiguously its own
    misreading. A refusal of one the api *had* offered is either the api
    contradicting itself or the seat mis-announcing something real (a target,
    a mode, an X), and the ledger cannot say which - so it is reported on its
    own line and counted as neither.
    """
    state = report["offer_split"]
    if state == "measured":
        return [
            "OFFER SPLIT",
            "  measured: every refusal the engine ruled on says whether this api had",
            "  itself offered the action. Rules errors above are refusals of actions",
            "  that were never on offer, and nothing else.",
        ]
    if state == "partial":
        return [
            "OFFER SPLIT",
            "  partial: some refusals say whether this api had offered the action and",
            "  some do not. The unchecked ones are counted as neither - read the",
            "  rules-error rate as a floor.",
        ]
    return [
        "OFFER SPLIT",
        "  unknown: no refusal the engine ruled on carries detail.offered, so a",
        "  refusal of an action this api had itself enumerated cannot be told apart",
        "  from a seat asking for something that was never offered. Every such",
        "  refusal is counted as unchecked rather than as a rules error.",
    ]


def _sizing_note(report: Mapping[str, Any]) -> list[str]:
    games = int(report["games"])
    attempts = sum(int(seat["attempts"]) for seat in report["seats"].values())
    win_rates = [
        seat["win_rate"] for seat in report["seats"].values() if seat["win_rate"]["trials"]
    ]
    widest = max((rate["half_width"] for rate in win_rates), default=None)
    own = (
        f"about +/-{widest * 100:.0f}pp on a win rate"
        if widest is not None
        else "no decided game, so no win rate at all"
    )
    return [
        "SIGNAL SIZING (95 percent central intervals, Beta posterior, uniform prior)",
        f"  This run is {games} games and {attempts} ruled-on attempts: {own}.",
        "  For scale: roughly 20 games gives about +/-20pp on a win rate, 50 gives",
        "  about +/-14pp, and resolving a 10pp A/B difference needs roughly 200 games",
        "  PER ARM. A win rate off a run this size is a direction, not a result.",
        "  Refusal rate and finish rate are the exception, and the reason a 10-20 game",
        "  run is worth doing at all: each carries hundreds of attempts rather than one",
        "  outcome per game, so their intervals are already tight. Gate a change on",
        "  those two; treat the win rate as something to accumulate towards.",
    ]


# -- cli ----------------------------------------------------------------------


def load(args: argparse.Namespace) -> list[GameRows]:
    if args.ledger:
        ledger = load_ledger_file(args.ledger)
        return [game for game in ledger if game.game_id in args.game] if args.game else ledger
    if args.database_url:
        return ledger_from_database(args.database_url, game_ids=args.game or None)

    from magic_cards_model_api.db import configured_database_url

    url = configured_database_url()
    if url:
        return ledger_from_database(url, game_ids=args.game or None)

    from magic_cards_model_api.store import store

    return ledger_from_store(store, game_ids=args.game or None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--database-url",
        default=None,
        help="postgres DSN to read `games`/`records` from; default is the configured one",
    )
    parser.add_argument(
        "--ledger", default=None, help="read a captured ledger file instead of a database"
    )
    parser.add_argument(
        "--game", action="append", default=[], help="limit to this game id; repeatable"
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--dump", default=None, help="also write the ledger read to this file, for replay"
    )
    args = parser.parse_args(argv)

    games = load(args)
    if args.dump:
        Path(args.dump).write_text(
            json.dumps([game.to_json() for game in games], indent=1, sort_keys=True)
        )
    report = build_report(games)
    print(json.dumps(report, indent=1, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
