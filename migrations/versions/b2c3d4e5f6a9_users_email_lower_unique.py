"""users.email: нижний регистр + уникальность без учёта регистра (ревью 2026-10-06, BE-16)

Раньше email хранился «как ввели», а уникальный индекс ix_users_email
различал регистр: "User@Mail.ru" и "user@mail.ru" были разными аккаунтами,
вход зависел от регистра.

upgrade:
1. Проверка дублей по lower(email). Если есть — миграция ОСТАНАВЛИВАЕТСЯ со
   списком адресов: сливать аккаунты автоматически нельзя (у каждого свои
   собаки, записи, согласия), это решение оператора.
2. users.email и users.pending_email → lower(trim(...)).
3. Уникальный функциональный индекс uq_users_email_lower по lower(email).

Revision ID: b2c3d4e5f6a9
Revises: a1b2c3d4e5f8
Create Date: 2026-10-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a9"
down_revision: Union[str, None] = "a1b2c3d4e5f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    dupes = conn.execute(
        sa.text(
            "SELECT lower(trim(email)) AS e, count(*) FROM users "
            "WHERE email IS NOT NULL GROUP BY 1 HAVING count(*) > 1"
        )
    ).fetchall()
    if dupes:
        listing = ", ".join(f"{row.e} ({row.count})" for row in dupes)
        raise RuntimeError(
            "users.email: найдены адреса, отличающиеся только регистром — "
            f"слейте или переименуйте аккаунты вручную и повторите: {listing}"
        )
    op.execute(
        "UPDATE users SET email = lower(trim(email)) "
        "WHERE email IS NOT NULL AND email <> lower(trim(email))"
    )
    op.execute(
        "UPDATE users SET pending_email = lower(trim(pending_email)) "
        "WHERE pending_email IS NOT NULL "
        "AND pending_email <> lower(trim(pending_email))"
    )
    op.create_index(
        "uq_users_email_lower",
        "users",
        [sa.text("lower(email)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_users_email_lower", table_name="users")
