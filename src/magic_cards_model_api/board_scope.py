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
(`on_one_board`, `read_on_one_board`). Two rules make that safe:

- **A scope never spans a command.** Scopes are opened by the read functions
  themselves, around their own body, so they close before `commands.
  run_command` or any other mutation runs; a view built after one opens a
  fresh scope on the game the command returned. The engine's invalidation
  (every announced change drops the board) is the safety net, not the
  mechanism.
- **A stale board is re-read, never served.** `BoardSnapshotStale` means the
  game was edited in place without the engine being told while a read was
  using its board - `InMemoryRepository` hands the same live `Game` to a read
  and to a board-setup route that edits it under the lock the read does not
  take. The read runs again with every board computed afresh, which is what
  it did before scopes existed.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from magic_cards_model.models import Game
from magic_cards_model.snapshot import BoardSnapshotStale, board_snapshot, uncached_boards

log = logging.getLogger("mcm.board_scope")


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
    """`read()` inside `one_board(*games)`, or computed afresh if the board
    went stale under it - see this module's docstring."""
    try:
        with one_board(*games):
            return read()
    except BoardSnapshotStale as exc:
        log.warning("board snapshot went stale during a read, reading it afresh: %s", exc)
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
