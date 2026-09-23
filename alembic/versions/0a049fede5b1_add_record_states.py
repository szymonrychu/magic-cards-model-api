"""add record_states

Revision ID: 0a049fede5b1
Revises: d5b2e8f4a913
Create Date: 2026-09-23 00:00:00.000000

R1: a companion table holding the exact engine state (`Game.model_dump
(mode="json")`, `zlib`-compressed) beside one `records` row, keyed by the
same `(game_id, seq)` a `records` row is keyed by - so a post-game replay
can read the board exactly as the engine held it at that decision point,
rather than reconstructing it from `records.payload`'s human-readable
snapshot string.

No `ForeignKey` on `game_id`, the same independent-ledger reasoning
`8f1c47a0b6d2` gives for `records`: these rows must outlive the game they
describe, or a soft-deleted game's replay would lose its exact states the
instant `delete()` ran. See `db.py`'s own docstring on `record_states`.

Its own revision rather than an edit to any shipped one - `alembic upgrade
head` never re-runs a revision a database is already stamped with, and
`tests/test_migrations.py` pins that this one actually adds the table to a
database migrated before it existed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0a049fede5b1"
down_revision: str | Sequence[str] | None = "d5b2e8f4a913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "record_states",
        sa.Column("game_id", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("state", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("game_id", "seq", name="pk_record_states"),
    )


def downgrade() -> None:
    op.drop_table("record_states")
