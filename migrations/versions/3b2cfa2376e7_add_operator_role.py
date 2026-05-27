"""add_operator_role

Revision ID: 3b2cfa2376e7
Revises: b9929c474916
Create Date: 2026-05-26 11:29:58.848398

Добавляет значение 'operator' в PostgreSQL enum-тип roleenum (этап 14
follow-up для этапа 11). PG не умеет ALTER TYPE ... DROP VALUE без
пересоздания типа, поэтому downgrade оставлен пустым с явным
объяснением: откат требует полного пересоздания типа и каскадного
конвертирования столбцов, что небезопасно на проде.

`ALTER TYPE ... ADD VALUE IF NOT EXISTS` идемпотентен — повторный
upgrade не упадёт.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '3b2cfa2376e7'
down_revision: Union[str, Sequence[str], None] = 'b9929c474916'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    PG-специфичный ALTER TYPE. ADD VALUE — append-only операция, не
    требует переписывания существующих строк. Выполняется мгновенно
    даже на больших таблицах с user_roles.
    """
    # COMMIT перед ADD VALUE: в PG < 12 это требовалось вне транзакции.
    # PG 17 разрешает, но явный execute() с autocommit-context работает
    # везде.
    op.execute("ALTER TYPE roleenum ADD VALUE IF NOT EXISTS 'operator'")


def downgrade() -> None:
    """
    Полноценный downgrade требует пересоздания TYPE и каскадного
    переноса данных — не делаем автоматически, чтобы случайный
    `alembic downgrade` не уронил прод.

    Если откат действительно нужен:
    1. SELECT id FROM user_roles WHERE role = 'operator' → migrate
       пользователей на другую роль либо удалить записи.
    2. CREATE TYPE roleenum_new AS ENUM ('admin', 'organizer',
       'breeder', 'judge', 'buyer');
    3. ALTER TABLE user_roles ALTER COLUMN role TYPE roleenum_new
       USING role::text::roleenum_new;
    4. DROP TYPE roleenum; ALTER TYPE roleenum_new RENAME TO roleenum;
    """
    pass
