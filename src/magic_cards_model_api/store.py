"""The process-wide game singleton. `GameStore` is `InMemoryRepository`
(`repository.py`) under its historical name: everything that used to import
`GameStore` or the `store` singleton from here keeps working unchanged.

The real content - the `GameRepository` protocol and its in-memory
implementation - moved to `repository.py` as part of extracting the
repository seam a postgres-backed implementation (T9) will also satisfy.
This module is now just the wiring: the concrete class this deployment uses
today, aliased to its old name, and the one instance of it every request
shares.
"""

from __future__ import annotations

from uuid import uuid4

from magic_cards_model.models import Game

from .repository import GameRepository, InMemoryRepository

__all__ = ["GameRepository", "GameStore", "new_seat_uids", "store"]

GameStore = InMemoryRepository
"""Backward-compatible alias. Every existing `from .store import GameStore`
and every direct `GameStore()` construction in tests keeps working - this is
literally the same class, not a copy of it."""

# Typed as `InMemoryRepository`, not the narrower `GameRepository` protocol:
# callers here (`serialize.py`, `opening.py`, `decisions.py`, `app.py`) also
# use `mailbox`, `decision_timeouts` and `automatic_pregame`, which are
# deliberately off the protocol (see `repository.py`'s module docstring) but
# still exist on the concrete singleton every one of those calls.
store = GameStore()


def new_seat_uids(game: Game) -> dict[str, str]:
    """A fresh secret uid for each of the game's players: uid -> username."""
    return {uuid4().hex: player.username for player in game.players}
