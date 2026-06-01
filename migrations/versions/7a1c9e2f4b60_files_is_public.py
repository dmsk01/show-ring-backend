"""files.is_public flag (private generated documents)

Revision ID: 7a1c9e2f4b60
Revises: 68a79230a908
Create Date: 2026-06-01 12:00:00.000000

Добавляет files.is_public. По умолчанию TRUE — существующие загрузки
(фото собак, аватары) остаются публичными, поведение GET /files/{id}
для них не меняется. Сгенерированные официальные документы воркер
записывает с is_public=FALSE: они отдаются только через защищённый
/tasks/{id}/download (ACL автор/admin), а публичный /files/{id} их
больше не раскрывает.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a1c9e2f4b60'
down_revision: Union[str, Sequence[str], None] = '68a79230a908'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default='true' — у существующих строк колонка станет TRUE
    # (фото публичны, поведение сохраняется). nullable=False сразу, т.к.
    # default покрывает backfill.
    op.add_column(
        'files',
        sa.Column(
            'is_public',
            sa.Boolean(),
            server_default='true',
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('files', 'is_public')
