"""audit_l2_token_purpose

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-06 00:00:00.000000

Аудит L2: колонка purpose в email_verification_tokens (verify | email_change).
Строго разделяет токены подтверждения регистрации и подтверждения смены email,
чтобы токен одной операции нельзя было предъявить в эндпоинте другой.

server_default 'verify' — существующие строки (все они регистрационные либо
короткоживущие) трактуем как verify; nullable=False.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'email_verification_tokens',
        sa.Column(
            'purpose',
            sa.String(length=32),
            server_default='verify',
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('email_verification_tokens', 'purpose')
