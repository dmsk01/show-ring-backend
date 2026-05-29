"""official documents: user_profiles + dog breeder

Revision ID: 2ce1268d4ed2
Revises: 6b1f4e8a3d92
Create Date: 2026-05-29 16:56:26.060695

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2ce1268d4ed2'
down_revision: Union[str, Sequence[str], None] = '6b1f4e8a3d92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("last_name", sa.String(length=128), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("patronymic", sa.String(length=128), nullable=True),
        sa.Column("country", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.add_column(
        "dogs", sa.Column("breeder_kennel_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "dogs", sa.Column("breeder_name", sa.String(length=255), nullable=True)
    )
    op.create_index(
        op.f("ix_dogs_breeder_kennel_id"),
        "dogs",
        ["breeder_kennel_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_dogs_breeder_kennel_id_kennels",
        "dogs",
        "kennels",
        ["breeder_kennel_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_dogs_breeder_kennel_id_kennels", "dogs", type_="foreignkey"
    )
    op.drop_index(op.f("ix_dogs_breeder_kennel_id"), table_name="dogs")
    op.drop_column("dogs", "breeder_name")
    op.drop_column("dogs", "breeder_kennel_id")
    op.drop_table("user_profiles")
