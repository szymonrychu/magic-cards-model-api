"""drop the records.game_id foreign key

Revision ID: 8f1c47a0b6d2
Revises: 374af1d25f73
Create Date: 2026-09-08 09:40:00.000000

T10 turns `records` into the audit trail proper, and an audit trail must
outlive the game it describes. Two things depend on that directly:

- `lobby.reset_game` records the reset and *then* deletes the game, so the
  transcript "ends with the reset rather than trailing off" (its own
  docstring). `ON DELETE CASCADE` deleted that very row a moment after it
  was written.
- `repository.append_record` promises never to raise for an unknown
  `game_id` - `audit.record` has always said "a game that is no longer in
  the store (or was never in it) still gets its entry", and
  `ai_seat.AiSeatRegistry` dispatches against ids that may not be in
  `games`. With the constraint in place that path raised
  `ForeignKeyViolation` instead.

This is a separate revision rather than an edit to `374af1d25f73` on
purpose: `alembic upgrade head` never re-runs a revision it has already
recorded, so editing that file left every already-migrated database with
the CASCADE intact and no way to notice. See
`tests/test_migrations.py`, which provisions a database from the initial
revision and proves this one removes it.

`downgrade` re-creates the constraint, which means it can fail on a
database that has since accumulated orphaned rows. That is correct: it
says out loud that going back is only possible while nothing depends on
the freedom this revision granted.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f1c47a0b6d2"
down_revision: str | Sequence[str] | None = "374af1d25f73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: postgres's own default name for the constraint `374af1d25f73` created
#: without naming it: `<table>_<column>_fkey`.
_CONSTRAINT = "records_game_id_fkey"


def upgrade() -> None:
    """Drop the foreign key, if it is there.

    Guarded rather than a bare `op.drop_constraint`, because a database
    provisioned from the *edited* initial revision - the state this branch
    briefly produced on developer machines and in any cached docker volume -
    never had the constraint at all, and `upgrade head` must be a no-op
    there rather than an error.
    """
    bind = op.get_bind()
    exists = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_constraint WHERE conname = :name AND conrelid = 'records'::regclass"
        ),
        {"name": _CONSTRAINT},
    ).scalar()
    if exists:
        op.drop_constraint(_CONSTRAINT, "records", type_="foreignkey")


def downgrade() -> None:
    """Put it back, CASCADE and all - the exact constraint
    `374af1d25f73` created."""
    op.create_foreign_key(_CONSTRAINT, "records", "games", ["game_id"], ["id"], ondelete="CASCADE")
