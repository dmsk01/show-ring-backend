"""classified_price_kind

Revision ID: 6b1f4e8a3d92
Revises: 3c8f2a4e7d91
Create Date: 2026-05-28 16:00:00.000000

bug_215 audit 2026-05-28: добавляем enum `price_kind` для устранения
двусмысленности «price=NULL vs price=0 vs price>0» в classifieds.

Семантика:
- fixed       — конкретная цена; price IS NOT NULL
- free        — бесплатно / в добрые руки; price IS NULL
- negotiable  — цена договорная / по запросу; price IS NULL

Backfill из текущего состояния (lossless):
- price IS NULL  → negotiable  (раньше NULL означал «не указана»)
- price = 0      → free        (нулевая цена = бесплатно)
- price > 0      → fixed
После backfill для free/negotiable обнуляем price → NULL: при
price_kind != fixed число теряет смысл, оставлять его в БД =
приглашение к рассинхрону.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6b1f4e8a3d92'
down_revision: Union[str, Sequence[str], None] = '3c8f2a4e7d91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Создаём отдельную ссылку на тип, чтобы не дублировать create() при
# add_column — иначе SQLAlchemy попытается создать enum дважды и упадёт.
price_kind_enum = sa.Enum(
    'fixed', 'free', 'negotiable', name='classifiedpricekind'
)


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Создаём тип PostgreSQL заранее — потом используем create_type=False.
    price_kind_enum.create(op.get_bind(), checkfirst=True)

    # 2. Колонка nullable=True пока — заполним backfill'ом и потом
    # переведём в NOT NULL.
    op.add_column(
        'classifieds',
        sa.Column(
            'price_kind',
            sa.Enum(
                'fixed', 'free', 'negotiable',
                name='classifiedpricekind',
                create_type=False,
            ),
            nullable=True,
        ),
    )

    # 3. Backfill: lossless mapping из текущих значений price.
    op.execute(
        """
        UPDATE classifieds
        SET price_kind = CASE
            WHEN price IS NULL THEN 'negotiable'::classifiedpricekind
            WHEN price = 0    THEN 'free'::classifiedpricekind
            ELSE                   'fixed'::classifiedpricekind
        END
        """
    )

    # 4. Для free/negotiable обнуляем price — после миграции число
    # имеет смысл только при kind='fixed'.
    op.execute(
        """
        UPDATE classifieds
        SET price = NULL
        WHERE price_kind IN ('free', 'negotiable')
        """
    )

    # 5. Теперь все строки заполнены — делаем NOT NULL.
    op.alter_column(
        'classifieds',
        'price_kind',
        existing_type=sa.Enum(
            'fixed', 'free', 'negotiable',
            name='classifiedpricekind',
            create_type=False,
        ),
        nullable=False,
        server_default='fixed',
    )

    # 6. Инвариант: price согласован с price_kind. CHECK на уровне БД
    # защищает от прямых SQL-апдейтов в обход сервиса.
    op.create_check_constraint(
        'ck_classifieds_price_kind_match',
        'classifieds',
        "(price_kind = 'fixed' AND price IS NOT NULL AND price > 0) "
        "OR (price_kind IN ('free', 'negotiable') AND price IS NULL)",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'ck_classifieds_price_kind_match',
        'classifieds',
        type_='check',
    )
    op.drop_column('classifieds', 'price_kind')
    # Удаляем PG TYPE, иначе повторный upgrade упадёт «type already exists».
    sa.Enum(name='classifiedpricekind').drop(
        op.get_bind(), checkfirst=True
    )
