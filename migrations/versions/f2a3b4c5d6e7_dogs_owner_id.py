"""dogs.owner_id (владелец карточки → «мои собаки», запись на выставку)

Revision ID: f2a3b4c5d6e7
Revises: d3f7c1a9b8e2
Create Date: 2026-06-09 12:00:00.000000

Добавляет dogs.owner_id (FK users.id ON DELETE SET NULL) + индекс — прямую
связь собаки с пользователем, который её добавил. Раньше владелец выводился
только через kennel.owner_id, и собаки без питомника оставались «ничьими».

Бэкафилл проставляет владельца из питомника там, где он есть. Собаки без
питомника остаются с owner_id IS NULL (исторически владелец неизвестен) —
их видно в «моих собаках» только если они созданы после этой правки.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'd3f7c1a9b8e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'dogs',
        sa.Column('owner_id', sa.UUID(), nullable=True),
    )
    op.create_index(
        op.f('ix_dogs_owner_id'), 'dogs', ['owner_id'], unique=False
    )
    op.create_foreign_key(
        'fk_dogs_owner_id_users',
        'dogs', 'users',
        ['owner_id'], ['id'],
        ondelete='SET NULL',
    )
    # Бэкафилл из питомника: владелец питомника становится владельцем
    # собаки. Собаки без kennel_id не затрагиваются (остаются NULL).
    op.execute(
        """
        UPDATE dogs
        SET owner_id = kennels.owner_id
        FROM kennels
        WHERE dogs.kennel_id = kennels.id
          AND dogs.owner_id IS NULL
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_dogs_owner_id_users', 'dogs', type_='foreignkey')
    op.drop_index(op.f('ix_dogs_owner_id'), table_name='dogs')
    op.drop_column('dogs', 'owner_id')
