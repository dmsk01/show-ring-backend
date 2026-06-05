"""stage_16_in_app_channel

Revision ID: a7d4e9c1f3b8
Revises: f1a2b3c4d5e6
Create Date: 2026-06-05 00:00:00.000000

Этап 16 (realtime-уведомления): добавляем значение `in_app` в PG-enum
`notificationchannel`. In-app уведомление = строка notifications с
channel=in_app, которая показывается в колокольчике и пушится по
WebSocket. Письмо при этом НЕ отправляется (это отдельный канал email).

Почему отдельная миграция, а не правка модели: колонка channel — это
PG-enum-тип `notificationchannel`. Добавить вариант в Python-enum
недостаточно — БД отвергнет INSERT с неизвестным значением. ALTER TYPE
... ADD VALUE расширяет сам тип на уровне БД.

IF NOT EXISTS — миграция идемпотентна (повторный upgrade не упадёт).
downgrade — no-op: PostgreSQL не умеет удалять значение из enum-типа
(только пересоздание типа целиком, что небезопасно при заполненной
колонке). Откат тут осознанно пустой.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7d4e9c1f3b8'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ADD VALUE нельзя выполнять внутри транзакционного блока в старых PG;
    # начиная с PG 12 — можно, и Alembic запускает миграцию в транзакции.
    # Новое значение нельзя использовать в той же транзакции, где оно
    # добавлено, — нам ок: используем его уже в рантайме приложения.
    op.execute(
        "ALTER TYPE notificationchannel ADD VALUE IF NOT EXISTS 'in_app'"
    )


def downgrade() -> None:
    """Downgrade schema (no-op: PG не удаляет значения из enum)."""
    pass
