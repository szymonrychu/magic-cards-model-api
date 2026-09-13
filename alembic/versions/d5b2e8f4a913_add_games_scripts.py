"""add games.scripts

Revision ID: d5b2e8f4a913
Revises: b8d24f6c1a07
Create Date: 2026-09-13 12:00:00.000000

The play-script corpus a seat Job registers under its own uid
(`PUT /api/games/{id}/seats/{uid}/scripts`): `{uid: {"deck_slug",
"guides_revision", "scripts", "registered_at"}}`. Durable for the reason
`advisors` is: the api runs several replicas and restarts, and a corpus
registered on one pod must be readable from any of them afterwards.

`server_default="{}"` rather than a nullable column, like `seats` and
`advisors`: "no corpus registered" is the honest answer for every row that
predates the feature. Its own revision rather than an edit to a shipped one,
because alembic never re-runs a revision a database is already stamped with -
`tests/test_migrations.py` pins this column too.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5b2e8f4a913"
down_revision: str | Sequence[str] | None = "b8d24f6c1a07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "games",
        sa.Column("scripts", JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("games", "scripts")
