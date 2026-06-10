"""phone otp auth: users.phone, nullable email/password

Revision ID: b4f8c2d91a37
Revises: f2a3b4c5d6e7
Create Date: 2026-06-10
"""
import sqlalchemy as sa
from alembic import op

revision = "b4f8c2d91a37"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(16), nullable=True))
    op.create_index("ix_users_phone", "users", ["phone"], unique=True)
    op.alter_column(
        "users", "email", existing_type=sa.String(255), nullable=True
    )
    op.alter_column(
        "users",
        "hashed_password",
        existing_type=sa.String(255),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_users_email_or_phone",
        "users",
        "email IS NOT NULL OR phone IS NOT NULL",
    )


def downgrade() -> None:
    # ВНИМАНИЕ: downgrade предполагает, что телефонных пользователей
    # (email IS NULL или hashed_password IS NULL) в БД нет — иначе
    # alter_column на NOT NULL упадёт. Это сознательно: молча удалять
    # пользователей миграция не должна.
    op.drop_constraint("ck_users_email_or_phone", "users", type_="check")
    op.alter_column(
        "users",
        "hashed_password",
        existing_type=sa.String(255),
        nullable=False,
    )
    op.alter_column(
        "users", "email", existing_type=sa.String(255), nullable=False
    )
    op.drop_index("ix_users_phone", table_name="users")
    op.drop_column("users", "phone")
