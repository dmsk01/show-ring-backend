"""notification_message_id

Revision ID: 7f3e1b9c2a48
Revises: 4d8a6f2e1b53
Create Date: 2026-05-28 15:00:00.000000

bug_230 audit 2026-05-28: добавляем колонку `message_id` в
notifications для дедупа. Per-recipient идентификатор формируется в
events_handler как uuid5(event_id, user_id) — детерминированный, не
зависит от порядка обработки.

UNIQUE-constraint защищает на уровне БД: если events_handler упал
после commit'а и события доставились в Rabbit повторно, второй
INSERT падает с IntegrityError — workflow корректно пропускает уже
обработанные.

nullable=True — исторические notifications (созданные до миграции)
не имеют message_id; UNIQUE с NULL в PG считает каждый NULL
отличным от других, так что constraint их не ломает.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7f3e1b9c2a48'
down_revision: Union[str, Sequence[str], None] = '4d8a6f2e1b53'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'notifications',
        sa.Column('message_id', sa.UUID(), nullable=True),
    )
    op.create_unique_constraint(
        'uq_notifications_message_id',
        'notifications',
        ['message_id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'uq_notifications_message_id', 'notifications', type_='unique'
    )
    op.drop_column('notifications', 'message_id')
