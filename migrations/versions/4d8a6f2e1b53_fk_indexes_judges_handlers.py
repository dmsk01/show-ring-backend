"""fk_indexes_judges_handlers

Revision ID: 4d8a6f2e1b53
Revises: ea996647ff46
Create Date: 2026-05-28 14:00:00.000000

bug_221/222/223 audit 2026-05-28: PostgreSQL не создаёт индекс на
FK-колонке автоматически. Без индекса любой WHERE judge_id=? или
JOIN по этим колонкам уходит в seq scan; каскадные UPDATE/DELETE на
родительском users.id (RESTRICT/SET NULL) тоже сканируют дочернюю
таблицу целиком. На тысячах записей замедление в 10-100×.

Добавляем индексы вручную, чтобы не ждать накопления данных.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '4d8a6f2e1b53'
down_revision: Union[str, Sequence[str], None] = 'ea996647ff46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        op.f('ix_show_rings_judge_id'),
        'show_rings',
        ['judge_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_show_entries_handler_id'),
        'show_entries',
        ['handler_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_dog_titles_judge_id'),
        'dog_titles',
        ['judge_id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_dog_titles_judge_id'), table_name='dog_titles')
    op.drop_index(
        op.f('ix_show_entries_handler_id'), table_name='show_entries'
    )
    op.drop_index(op.f('ix_show_rings_judge_id'), table_name='show_rings')
