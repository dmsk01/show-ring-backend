"""user_consents + contacts_public + users.deleted_at (152-ФЗ)

- user_consents — журнал согласий (доказательство по ч. 3 ст. 9).
- kennels.contacts_public / classifieds.contacts_public — согласие на
  распространение контактов. server_default false: у существующих
  публикаций согласия на распространение нет (молчание ≠ согласие,
  ч. 8 ст. 10.1), контакты скрываются до явного включения владельцем.
- users.deleted_at — аккаунт удалён (обезличен) самим пользователем.
- ad_banners.erid, ad_campaigns.advertiser_name/inn — маркировка
  интернет-рекламы (ст. 18.1 Закона «О рекламе»).

Revision ID: a1b2c3d4e5f8
Revises: f4a5b6c7d8e9
Create Date: 2026-10-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f8"
down_revision: Union[str, None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_consents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("revision", sa.String(32), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
    )
    op.create_index(
        "ix_user_consents_user_kind", "user_consents", ["user_id", "kind"]
    )

    op.add_column(
        "users",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column("ad_banners", sa.Column("erid", sa.String(128), nullable=True))
    op.add_column(
        "ad_campaigns",
        sa.Column("advertiser_name", sa.String(255), nullable=True),
    )
    op.add_column(
        "ad_campaigns", sa.Column("advertiser_inn", sa.String(12), nullable=True)
    )

    for table in ("kennels", "classifieds"):
        op.add_column(
            table,
            sa.Column(
                "contacts_public",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    for table in ("classifieds", "kennels"):
        op.drop_column(table, "contacts_public")
    op.drop_column("ad_campaigns", "advertiser_inn")
    op.drop_column("ad_campaigns", "advertiser_name")
    op.drop_column("ad_banners", "erid")
    op.drop_column("users", "deleted_at")
    op.drop_index("ix_user_consents_user_kind", table_name="user_consents")
    op.drop_table("user_consents")
