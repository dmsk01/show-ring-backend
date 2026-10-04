"""show_checkin

Revision ID: f4a5b6c7d8e9
Revises: e3c4d5e6f7a8
Create Date: 2026-10-04 12:00:00.000000

Регистрация прибытия (чек-ин): документы собак, персонал выставки,
журнал отметок, флаг выставки и статус явки записи.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4a5b6c7d8e9"
down_revision: str | Sequence[str] | None = "e3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ATTENDANCE = postgresql.ENUM(
    "registered", "arrived", "admitted", "rejected", "absent",
    name="attendancestatus",
)


def upgrade() -> None:
    op.add_column(
        "shows",
        sa.Column("checkin_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )

    _ATTENDANCE.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "show_entries",
        sa.Column(
            "attendance_status",
            postgresql.ENUM(name="attendancestatus", create_type=False),
            nullable=False,
            server_default="registered",
        ),
    )
    op.add_column(
        "show_entries",
        sa.Column("attendance_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_show_entries_attendance_status", "show_entries", ["attendance_status"]
    )

    op.create_table(
        "dog_documents",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("dog_id", sa.UUID(), sa.ForeignKey("dogs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", sa.UUID(), sa.ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "vet_passport", "pedigree", "puppy_card", "working_certificate", "other",
                name="dogdocumentkind",
            ),
            nullable=False,
        ),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("uploaded_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
    )
    op.create_index("ix_dog_documents_dog_id", "dog_documents", ["dog_id"])
    op.create_index("ix_dog_documents_file_id", "dog_documents", ["file_id"])

    op.create_table(
        "show_staff",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("show_id", sa.UUID(), sa.ForeignKey("shows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Enum("registrar", name="showstaffrole"), nullable=False),
        sa.Column("added_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("show_id", "user_id", "role", name="uq_show_staff"),
    )
    op.create_index("ix_show_staff_show_id", "show_staff", ["show_id"])
    op.create_index("ix_show_staff_user_id", "show_staff", ["user_id"])

    op.create_table(
        "entry_checks",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("entry_id", sa.UUID(), sa.ForeignKey("show_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("docs_precheck", "arrival", "vet", "docs_onsite", name="entrycheckkind"),
            nullable=False,
        ),
        sa.Column("result", sa.Enum("passed", "failed", name="entrycheckresult"), nullable=False),
        sa.Column("document_id", sa.UUID(), sa.ForeignKey("dog_documents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("performed_by", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.clock_timestamp(), nullable=False),
    )
    op.create_index("ix_entry_checks_entry_id", "entry_checks", ["entry_id"])


def downgrade() -> None:
    op.drop_index("ix_entry_checks_entry_id", table_name="entry_checks")
    op.drop_table("entry_checks")
    op.drop_index("ix_show_staff_user_id", table_name="show_staff")
    op.drop_index("ix_show_staff_show_id", table_name="show_staff")
    op.drop_table("show_staff")
    op.drop_index("ix_dog_documents_file_id", table_name="dog_documents")
    op.drop_index("ix_dog_documents_dog_id", table_name="dog_documents")
    op.drop_table("dog_documents")
    op.drop_index("ix_show_entries_attendance_status", table_name="show_entries")
    op.drop_column("show_entries", "attendance_changed_at")
    op.drop_column("show_entries", "attendance_status")
    op.drop_column("shows", "checkin_enabled")
    for enum_name in (
        "entrycheckresult", "entrycheckkind", "showstaffrole",
        "dogdocumentkind", "attendancestatus",
    ):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
