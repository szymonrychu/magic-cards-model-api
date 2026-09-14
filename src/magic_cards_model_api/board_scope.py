"""One layered board per read: the api's side of the engine's board snapshot.

`Game.computed_board` applies every continuous effect to the whole
battlefield and is recomputed on every call. A state view asks it once per
card and a legal-actions read once per permanent and ability, so both grew
with the square of the battlefield - 1,249 boards for one GET state on a
28-permanent board, and `records.legal_actions_before` paid its share inside
the per-game lock on every action (2026-09-14, the api pod saturating its
CPU limit). `magic_cards_model.snapshot.board_snapshot` is the engine's scope
in which the board is computed once and reused until something announces a
change.

Every read-only function here that builds from the board runs inside one
(`on_one_board`, `read_on_one_board`). What that does and does not promise:

- **A scope never spans a command.** Scopes are opened by the read functions
  themselves, around their own body, so they close before `commands.
  run_command` or any other mutation runs; a view built after one opens a
  fresh scope on the game the command returned.
- **An announced change is seen by the next board read.** Engine commands,
  field stores on engine models, and the board-setup routes' in-place list
  edits (`announced_edit`, used by `engine_api.put_card_in_zone`/`move_card`)
  all move the engine's epoch, so a read made after one - in any thread -
  computes a fresh board.
- **A stale board the engine detects is read again.** `BoardSnapshotStale`
  (an unannounced in-place edit, caught when the board is dropped) and
  `NotOnBoard` (the cached board asked about a card it was not computed with)
  both send the whole read round again with every board computed afresh,
  once. No broad `except` inside a scope may swallow either (`STALE_BOARD`).
- **What is not guaranteed.** An in-place edit nobody announced is caught
  only when the engine drops the board and no announced change came first,
  or when a lookup misses. An unannounced edit that changes a card the board
  already holds (a list reordered, a field written behind the model's back)
  and is followed by an announced change before the board is dropped is not
  detected, and a read between the two is answered from the board before the
  edit. `InMemoryRepository` hands the same live `Game` to a read and to a
  mutation under the lock the read does not take, so every mutation path
  that edits in place must announce (`announced_edit`).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from magic_cards_model.machines.layers import NotOnBoard
from magic_cards_model.models import Game
from magic_cards_model.snapshot import (
    BoardSnapshotStale,
    announce_mutation,
    board_snapshot,
    mutation_landed,
    uncached_boards,
)

log = logging.getLogger("mcm.board_scope")

#: The engine's two signals that a read was served a board the game has moved
#: past. A broad `except` inside a board scope re-raises these first.
STALE_BOARD: tuple[type[Exception], ...] = (BoardSnapshotStale, NotOnBoard)


@contextmanager
def one_board(*games: Game | None) -> Iterator[None]:
    """One engine board snapshot per distinct game in `games` for the block.
    `None` entries and repeats are skipped; a game already in a scope shares
    it (the engine nests)."""
    with ExitStack() as stack:
        seen: set[int] = set()
        for game in games:
            if game is None or id(game) in seen:
                continue
            seen.add(id(game))
            stack.enter_context(board_snapshot(game))
        yield


def read_on_one_board[T](read: Callable[[], T], *games: Game | None) -> T:
    """`read()` inside `one_board(*games)`, or computed afresh once if the
    board went stale under it - see this module's docstring."""
    try:
        with one_board(*games):
            return read()
    except STALE_BOARD as exc:
        log.warning("board snapshot went stale during a read, reading it afresh: %r", exc)
        with uncached_boards():
            return read()


def on_one_board[**P, T](function: Callable[P, T]) -> Callable[P, T]:
    """Run `function` - whose first argument is the `Game` it reads - on one
    board (`read_on_one_board`)."""

    @functools.wraps(function)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        game: Any = args[0]
        return read_on_one_board(lambda: function(*args, **kwargs), game)

    return wrapper


@contextmanager
def announced_edit(game: Game) -> Iterator[None]:
    """`game`'s zone lists are edited in place for the extent of the block,
    outside any engine command: announced before the edit and again once it
    has landed, so a board any thread computed from the old lists is dropped."""
    announce_mutation(game)
    try:
        yield
    finally:
        mutation_landed(game)
