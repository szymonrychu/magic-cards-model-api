"""add games.archived_at (soft delete)

Revision ID: a2f6d9c31b57
Revises: f3a7c2e91b04
Create Date: 2026-09-08 20:30:00.000000

`GameRepository.delete` used to remove the `games` row outright, and the
lobby's End button reaches it (`decks_api.end_lobby_game` -> `lobby.reset_game`
-> `store.delete`), as does `DELETE /api/games/{game_id}`. The `records`
ledger already outlives its game (it carries no foreign key - see revision
`8f1c47a0b6d2`), so what survived a delete was the reasoning and what did not
was the board, the hands and the final state it was reasoning about. A
post-game review reading that ledger would have had the argument with none of
the evidence.

The owner's requirement is plain: "Games must be persisting in the database
even after are stopped/finished/conceded." So `delete` now stamps this column
instead of issuing a `DELETE`, and every other read on `games` filters on
`archived_at IS NULL` - an ended game answers `None` to `get()`, is absent
from `ids()`/`ui_games()`/`live_ui_games()`, raises from `locked()`, and has
no seat credentials or admin key, exactly as it did when the row was really
gone. `PostgresRepository.archived` is the only accessor that looks past the
filter, and only a review uses it.

Nullable, no `server_default`, on purpose: `NULL` means "still live", which is
the correct reading for every row that already exists. A game that predates
this column has not been ended - if it had been, its row would not be there to
migrate - so backfilling anything but `NULL` would archive live games.

No index. The filter is always accompanied by the primary key (`id = :id`) or
by the already-indexed `ui_game` read, so `archived_at` is a residual
predicate on an already-narrow row set, never the access path itself.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2f6d9c31b57"
down_revision: str | Sequence[str] | None = "f3a7c2e91b04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "games",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("games", "archived_at")
