"""user_is_phone_verified

Revision ID: d2b3c4d5e6f7
Revises: c1a2b3d4e5f6
Create Date: 2026-06-15 13:00:00.000000

Явный признак «телефон подтверждён OTP». До этого верифицированность
телефона была неявной (наличие phone), теперь — первоклассный флаг,
нужный тирам квот загрузки файлов (upload_quota_tiers).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "c1a2b3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_phone_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "is_phone_verified")
