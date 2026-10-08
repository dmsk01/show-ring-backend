"""ad_campaigns.approved_at — модерация рекламных кампаний (ревью 2026-10-06, BE-05)

Раньше владелец сам ставил кампании status=active. Теперь первую
активацию делает admin (approved_at = момент одобрения).

Существующие кампании, уже бывшие в показе (active/paused/completed),
считаются одобренными задним числом (approved_at = updated_at): иначе
выкатка остановила бы все действующие показы. Их стоит просмотреть вручную.

Revision ID: c3d4e5f6a7b0
Revises: b2c3d4e5f6a9
Create Date: 2026-10-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b0"
down_revision: Union[str, None] = "b2c3d4e5f6a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ad_campaigns",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE ad_campaigns SET approved_at = updated_at "
        "WHERE status IN ('active', 'paused', 'completed')"
    )


def downgrade() -> None:
    op.drop_column("ad_campaigns", "approved_at")
