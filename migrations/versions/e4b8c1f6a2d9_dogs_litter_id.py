"""dogs.litter_id (помёт → щенки, этап 18)

Revision ID: e4b8c1f6a2d9
Revises: c9f3a17b8e42
Create Date: 2026-06-03 10:00:00.000000

Добавляет dogs.litter_id (FK litters.id ON DELETE SET NULL) + индекс.
Связывает конкретных собак с пометом. nullable: большинство собак вне
помётов платформы.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4b8c1f6a2d9'
down_revision: Union[str, Sequence[str], None] = 'c9f3a17b8e42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'dogs',
        sa.Column('litter_id', sa.UUID(), nullable=True),
    )
    op.create_index(
        op.f('ix_dogs_litter_id'), 'dogs', ['litter_id'], unique=False
    )
    op.create_foreign_key(
        'fk_dogs_litter_id_litters',
        'dogs', 'litters',
        ['litter_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_dogs_litter_id_litters', 'dogs', type_='foreignkey')
    op.drop_index(op.f('ix_dogs_litter_id'), table_name='dogs')
    op.drop_column('dogs', 'litter_id')
