"""classified_availability

Revision ID: a3e9d7c2f1b4
Revises: b4f8c2d91a37
Create Date: 2026-06-11 12:00:00.000000

Статус доступности животного в объявлении: свободен / забронирован /
продан. Отдельная ось от classifieds.status (жизненный цикл публикации),
см. докстринг AnimalAvailability в app/models/classified.py.

Тип 'animalavailability' — новый (в отличие от classified_sex, где
переиспользовался существующий 'sexenum'). На существующей таблице
add_column НЕ создаёт тип сам (под asyncpg падает «type does not exist»),
поэтому создаём его явно через .create(checkfirst=True), а в колонке
ставим create_type=False — ровно как в миграции classified_price_kind.

Бэкофилл: колонка NOT NULL с server_default='available' — все старые
строки получают 'available' автоматически, без отдельного UPDATE.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3e9d7c2f1b4'
down_revision: Union[str, Sequence[str], None] = 'b4f8c2d91a37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Отдельная ссылка на тип — создаём его один раз заранее, затем колонка
# использует create_type=False, чтобы SQLAlchemy не пыталась создать enum
# повторно (см. classified_price_kind).
availability_enum = sa.Enum(
    'available', 'reserved', 'sold', name='animalavailability'
)


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Создаём PG TYPE заранее — add_column на существующей таблице сам
    # этого не делает.
    availability_enum.create(op.get_bind(), checkfirst=True)

    # 2. Колонка сразу NOT NULL: server_default='available' проставит
    # значение всем существующим строкам.
    op.add_column(
        'classifieds',
        sa.Column(
            'availability',
            sa.Enum(
                'available', 'reserved', 'sold',
                name='animalavailability',
                create_type=False,
            ),
            nullable=False,
            server_default='available',
        ),
    )
    # Индекс под фильтр «только свободные» (Classified.availability
    # index=True в модели).
    op.create_index(
        'ix_classifieds_availability',
        'classifieds',
        ['availability'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_classifieds_availability', table_name='classifieds'
    )
    op.drop_column('classifieds', 'availability')
    # Тип 'animalavailability' создан этой миграцией и больше нигде не
    # используется — снимаем его, иначе повторный upgrade упадёт на
    # «type already exists».
    sa.Enum(name='animalavailability').drop(op.get_bind(), checkfirst=True)
