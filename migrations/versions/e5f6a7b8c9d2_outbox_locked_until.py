"""outbox_events.locked_until — «застолбить» пачку dispatcher'ом (ревью 2026-10-06, BE-18)

Раньше FOR UPDATE SKIP LOCKED держал строки только до первого commit'а
внутри пачки — второй экземпляр dispatcher'а мог опубликовать оставшиеся
события повторно.

Revision ID: e5f6a7b8c9d2
Revises: d4e5f6a7b8c1
Create Date: 2026-10-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d2"
down_revision: Union[str, None] = "d4e5f6a7b8c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "locked_until")
