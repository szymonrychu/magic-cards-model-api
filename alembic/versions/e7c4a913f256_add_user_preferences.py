"""add user_preferences

Revision ID: e7c4a913f256
Revises: 0a049fede5b1
Create Date: 2026-09-28 00:00:00.000000

Per-OIDC-user preferences (speed-ai-seat): the play UI's speed-mode stop
settings, remembered per human across every device and every game they
play, rather than per browser (`localStorage`) alone - `GET`/`PUT
/api/me/preferences`. Keyed on the OIDC subject (`oidc.AuthenticatedUser.
sub`) rather than on any game, so it is its own table rather than a column
on `games`. See `db.py`'s own docstring on `user_preferences`.

No `ForeignKey` to anything - a `sub` is an IdP-minted identity, not a row
in any table this schema owns, matching `games.owner_user_id`'s own column
(nullable there, not nullable here: a row in this table only ever exists
because a caller with a real `sub` wrote it).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7c4a913f256"
down_revision: str | Sequence[str] | None = "0a049fede5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("sub", sa.String(), primary_key=True),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_preferences")
