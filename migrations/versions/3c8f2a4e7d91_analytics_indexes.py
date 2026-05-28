"""analytics_indexes

Revision ID: 3c8f2a4e7d91
Revises: 7f3e1b9c2a48
Create Date: 2026-05-28 15:30:00.000000

bug_228 audit 2026-05-28: GIN-индекс на moderation_logs.extra (JSONB).
Без GIN запросы `WHERE extra @> '{"key": "value"}'` или `?` уходят в
seq scan. На тысячах audit-записей это секунды на каждое открытие
страницы модерации.

`jsonb_path_ops` — компактный operator class: меньше места и быстрее
для `@>`, чем дефолтный `jsonb_ops`. Минус: не поддерживает `?` и
`?&`/`?|`. Для логов модерации, где мы фильтруем по конкретным ключам
содержимого, этого достаточно.

bug_229 audit 2026-05-28: композитный индекс на show_results
(is_best_in_show, show_entry_id) под отчёты «BIS на выставке» с JOIN
по show_entry. Без него планнер делал full scan show_results или
ходил через uq_show_result_entry без bitmap'а на is_best_in_show.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3c8f2a4e7d91'
down_revision: Union[str, Sequence[str], None] = '7f3e1b9c2a48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        'ix_moderation_logs_extra_gin',
        'moderation_logs',
        ['extra'],
        unique=False,
        postgresql_using='gin',
        postgresql_ops={'extra': 'jsonb_path_ops'},
    )
    op.create_index(
        'ix_show_results_bis_entry',
        'show_results',
        ['is_best_in_show', 'show_entry_id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_show_results_bis_entry', table_name='show_results'
    )
    op.drop_index(
        'ix_moderation_logs_extra_gin',
        table_name='moderation_logs',
        postgresql_using='gin',
    )
