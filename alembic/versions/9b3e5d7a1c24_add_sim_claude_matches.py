"""add sim_claude_matches

Revision ID: 9b3e5d7a1c24
Revises: e7c4a913f256
Create Date: 2026-11-16 00:00:00.000000

The persisted header of one Claude-seat sim match, readable after a
restart. One writer (`sim.claude.ClaudeMatchManager`); no seat uid column,
ever; the 30-day retention is the reaper's. See `db.py`'s own comment on
`sim_claude_matches`. Forward-only in practice: `downgrade` exists for the
scratch-database test, never for a deployed database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b3e5d7a1c24"
down_revision: str | Sequence[str] | None = "e7c4a913f256"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sim_claude_matches",
        sa.Column("match_id", sa.String(), primary_key=True),
        sa.Column("game_id", sa.String(), nullable=True),
        sa.Column("decks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("seats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seq", sa.Integer(), nullable=True),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stalled_reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_sim_claude_matches_started_at", "sim_claude_matches", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_sim_claude_matches_started_at", table_name="sim_claude_matches")
    op.drop_table("sim_claude_matches")
