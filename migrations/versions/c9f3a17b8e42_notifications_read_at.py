"""notifications.read_at (mark-as-read)

Revision ID: c9f3a17b8e42
Revises: 7a1c9e2f4b60
Create Date: 2026-06-02 09:00:00.000000

Добавляет notifications.read_at — момент прочтения уведомления
пользователем в UI. Отдельно от status (тот про доставку email).
nullable, без default: существующие уведомления остаются непрочитанными
(read_at = NULL → is_read = false в API).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9f3a17b8e42'
down_revision: Union[str, Sequence[str], None] = '7a1c9e2f4b60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'notifications',
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('notifications', 'read_at')
