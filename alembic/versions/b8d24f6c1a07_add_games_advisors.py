"""add games.advisors

Revision ID: b8d24f6c1a07
Revises: a2f6d9c31b57
Create Date: 2026-09-10 09:00:00.000000

The advisor credential: a uid that reads one human seat's game and may not
act on it, with the refusal enforced by the server (`game_access.py`) rather
than by whatever client holds the uid. `advisors` is the map that makes it
durable - `{uid: {"player": str, "agent": <AgentSettings wire dict or null>}}`
- and durable is the whole requirement: the api runs several replicas, and a
credential minted on one of them has to be recognised by all of them, so an
in-process registry (`AiSeatRegistry`, `K8sSeats._seats`) is explicitly not
where this can live.

**Its own column rather than a flag inside `seats`.** `seats` is uid ->
username and every `player_for_uid` caller in this repo reads it as "this uid
may act as that player". Adding advisor uids to it would silently hand every
one of those callers a credential that must not act; keeping them apart is
what lets the gate be a single unambiguous lookup.

**And its own map rather than a reuse of `agent_settings`**, which is keyed by
*username*: an advisor watching a seat that is itself an AI seat would collide
with that seat's own entry, and two advisors on one seat would collide with
each other. Keying by uid is the only shape that answers "which settings
belong to which credential", so the settings live inside each advisor entry.

`server_default="{}"` rather than a nullable column, for the same reason
`seats`/`decklists`/`agent_settings` carry one: "this game has no advisors" is
the honest answer for every row that predates the feature, and
`PostgresRepository.advisor_uids` must never have to tell "no advisors" apart
from "column not filled in". Its own revision rather than an edit to a shipped
one, because alembic never re-runs a revision a database is already stamped
with - `tests/test_migrations.py` is the proof, and it pins this column too.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d24f6c1a07"
down_revision: str | Sequence[str] | None = "a2f6d9c31b57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "games",
        sa.Column("advisors", JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("games", "advisors")
