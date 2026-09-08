"""add games.pause_generation

Revision ID: c4e91b7d2a58
Revises: 8f1c47a0b6d2
Create Date: 2026-09-08 12:10:00.000000

The pause slot holds one seat's half-finished command, and `app.py`'s
`_pause_slot_guard` already makes it impossible for another command to
overwrite it. What that guard cannot do is tell a *client* that the standing
question moved on between the read and the answer: a resolution asking two
decisions hands the same seat a second question the instant the first is
accepted.

`pause_generation` is the token that closes it - monotonic per game, bumped
only by `store_paused_command`, so it advances exactly when the standing
question is replaced and not when anything else about the game changes. It
is deliberately never reset, `clear_paused_command` included: reusing a
number a stale client is still holding is the one thing a generation may
never do.

`server_default="0"` rather than a nullable column, so every row that
already exists gets a real starting value in one statement and
`pause_generation` never has to mean "or maybe nothing".
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e91b7d2a58"
down_revision: str | Sequence[str] | None = "8f1c47a0b6d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "games",
        sa.Column("pause_generation", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("games", "pause_generation")
