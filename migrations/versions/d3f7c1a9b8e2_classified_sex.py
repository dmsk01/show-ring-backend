"""classified_sex

Revision ID: d3f7c1a9b8e2
Revises: c2d3e4f5a6b7
Create Date: 2026-06-08 12:00:00.000000

Фильтр объявлений по полу животного (запрос фронта 2026-06-08).
Добавляем nullable-колонку `sex` в classifieds. Переиспользуем уже
существующий PG-тип 'sexenum' (создан в миграции stage_04 под dogs.sex),
поэтому новый TYPE не заводим — create_type=False.

Бэкофилл не нужен: старые объявления остаются с sex IS NULL (услуги,
смешанные помёты, объявления до этой доработки). Точечный фильтр
?sex=male|female их не показывает — это согласованный контракт.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3f7c1a9b8e2'
down_revision: Union[str, Sequence[str], None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # create_type=False: тип 'sexenum' уже существует (dogs.sex). Без
    # этого флага SQLAlchemy попыталась бы выполнить CREATE TYPE ещё раз
    # и упала бы с «type already exists».
    op.add_column(
        'classifieds',
        sa.Column(
            'sex',
            sa.Enum('male', 'female', name='sexenum', create_type=False),
            nullable=True,
        ),
    )
    # Индекс под фильтр ?sex= (Classified.sex index=True в модели).
    op.create_index(
        'ix_classifieds_sex', 'classifieds', ['sex'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_classifieds_sex', table_name='classifieds')
    op.drop_column('classifieds', 'sex')
    # ВАЖНО: НЕ удаляем PG-тип 'sexenum' — его по-прежнему использует
    # dogs.sex. Тип создан и должен сниматься миграцией stage_04.
