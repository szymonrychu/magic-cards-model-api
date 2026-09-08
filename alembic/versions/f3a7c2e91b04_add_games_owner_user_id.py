"""add games.owner_user_id

Revision ID: f3a7c2e91b04
Revises: c4e91b7d2a58
Create Date: 2026-09-08 18:00:00.000000

OIDC-based user separation: the owner's own words were "no way for 1 user to
login as the other and see other's games". `owner_user_id` is the OIDC
subject (JWT `sub`) of whoever created a game through a request that carried
a valid bearer token - the fact `game_access.py`'s ownership gate checks
before letting any other caller reach that game at all, by any route.

Nullable, no `server_default`, on purpose: every row that already exists (and
every row created before an OIDC issuer is actually configured, which is
every row today - see `oidc.py`) gets `NULL`, and `NULL` here means "no
owner", not "owner unknown". `game_access.py` treats a `NULL` identically to
a genuinely unrestricted pre-OIDC game - the ownership gate only ever
narrows access for a game that names an owner, so this migration changes
nothing about who can reach an existing game.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a7c2e91b04"
down_revision: str | Sequence[str] | None = "c4e91b7d2a58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "games",
        sa.Column("owner_user_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("games", "owner_user_id")
