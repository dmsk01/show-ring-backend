"""references_name_en

Revision ID: b7e2f4a9c1d3
Revises: a3e9d7c2f1b4
Create Date: 2026-06-11 12:00:00.000000

Локализация справочников (запрос фронта 2026-06-11): контент /references/*
отдаётся по Accept-Language (ru — дефолт и канонический язык, en — перевод).

Добавляем nullable name_en во все 7 справочных таблиц и description_en туда,
где уже есть description (все, кроме animal_types). Русский остаётся в
существующих name/description — бэкофилл не нужен; пустой name_en на отдаче
фолбэчится на name, поэтому миграция безопасна для существующих данных.

Английские значения заполняет scripts/seed_references.py (upsert переводов).

Индексы не добавляем: поиск пород идёт через ILIKE (btree не помогает),
сортировка — coalesce(name_en, name); на сотнях строк это не узкое место
(см. комментарий в repositories/reference.py про pg_trgm).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e2f4a9c1d3'
down_revision: Union[str, Sequence[str], None] = 'a3e9d7c2f1b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (таблица, длина name_en — зеркалит длину name, есть ли description_en).
# Длины повторяют существующие name-колонки моделей, чтобы перевод не мог
# оказаться длиннее оригинала по ограничению схемы.
_TABLES: list[tuple[str, int, bool]] = [
    ('animal_types', 128, False),
    ('breed_groups', 255, True),
    ('breeds', 255, True),
    ('show_classes', 128, True),
    ('show_ranks', 255, True),
    ('titles', 128, True),
    ('grades', 128, True),
]


def upgrade() -> None:
    """Upgrade schema."""
    for table, name_len, has_description in _TABLES:
        op.add_column(table, sa.Column('name_en', sa.String(name_len), nullable=True))
        if has_description:
            op.add_column(table, sa.Column('description_en', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for table, _name_len, has_description in reversed(_TABLES):
        if has_description:
            op.drop_column(table, 'description_en')
        op.drop_column(table, 'name_en')
