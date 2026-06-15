"""upload_quota_tiers

Revision ID: e3c4d5e6f7a8
Revises: d2b3c4d5e6f7
Create Date: 2026-06-15 13:10:00.000000

Таблица редактируемых лимитов квот загрузки + сид трёх тиров + составной
индекс на files(uploaded_by, created_at) для агрегаций квоты (COUNT за
сутки и привязка к владельцу).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "d2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "upload_quota_tiers",
        sa.Column("tier", sa.String(length=32), primary_key=True),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("max_storage_bytes", sa.BigInteger(), nullable=False),
    )
    op.bulk_insert(
        sa.table(
            "upload_quota_tiers",
            sa.column("tier", sa.String),
            sa.column("daily_limit", sa.Integer),
            sa.column("max_storage_bytes", sa.BigInteger),
        ),
        [
            {"tier": "untrusted", "daily_limit": 5, "max_storage_bytes": 52428800},
            {"tier": "standard", "daily_limit": 30, "max_storage_bytes": 524288000},
            {"tier": "breeder", "daily_limit": 200, "max_storage_bytes": 2684354560},
        ],
    )
    op.create_index(
        "ix_files_uploaded_by_created",
        "files",
        ["uploaded_by", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_files_uploaded_by_created", table_name="files")
    op.drop_table("upload_quota_tiers")
