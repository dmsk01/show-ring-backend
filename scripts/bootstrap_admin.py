"""
Bootstrap admin script — создать первого admin-пользователя.

Использование:
    python scripts/bootstrap_admin.py --email admin@example.com --password ChangeMe123!

Зачем нужен:
- При первом запуске платформы (после alembic upgrade) в БД нет ни
  одного пользователя. Регистрация через /auth/register создаёт юзера
  без admin-роли — некому грантовать роль самому первому юзеру.
- В docker-compose deploy этот скрипт запускается один раз после
  migrate-контейнера (через `docker compose exec api python -m
  scripts.bootstrap_admin --email ... --password ...`).

Идемпотентность:
- Если пользователь с таким email уже существует — не создаём заново;
  лишь проверяем, что у него есть роль admin, и добавляем если нет.
- Безопасно запускать многократно (например, в CI-сценарии или после
  восстановления из бэкапа).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Добавляем корень в sys.path, чтобы скрипт работал и из CWD проекта,
# и при `python -m scripts.bootstrap_admin`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.database import async_session_factory, engine  # noqa: E402
# Импортируем все модели, чтобы SQLAlchemy зарегистрировала их в
# Base.metadata. Иначе lazy-FK (например, users.avatar_file_id → files)
# не находит таблицу при создании сессии. В migrations/env.py есть
# тот же ритуал импорта по той же причине.
from app.models import (  # noqa: F401, E402
    ad,
    audit,
    classified,
    dog,
    file,
    kennel,
    litter,
    notification,
    outbox,
    reference,
    result,
    show,
    support,
    task,
)
from app.models.user import RoleEnum, User, UserRole  # noqa: E402
from app.utils.security import hash_password, validate_password  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("bootstrap-admin")


async def ensure_admin(db: AsyncSession, email: str, password: str) -> None:
    """
    Создаёт пользователя email+password с ролью admin или добавляет
    admin-роль существующему. validate_password бросит ValueError при
    слабом пароле — намеренно, не хочется заводить admin'а с паролем
    короче 8 символов.
    """
    validate_password(password)

    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()

    if user is None:
        user = User(
            email=email,
            hashed_password=hash_password(password),
            is_active=True,
            is_email_verified=True,
        )
        db.add(user)
        await db.flush()
        logger.info("Created user %s", email)
    else:
        logger.info("User %s already exists, ensuring admin role", email)

    # Идемпотентно добавляем роль admin. UniqueConstraint(user_id, role)
    # защищает БД от дубликата, но мы и сами проверяем — иначе
    # IntegrityError при повторном запуске.
    existing_role = (
        await db.execute(
            select(UserRole).where(
                UserRole.user_id == user.id,
                UserRole.role == RoleEnum.admin,
            )
        )
    ).scalar_one_or_none()

    if existing_role is None:
        db.add(
            UserRole(
                user_id=user.id,
                role=RoleEnum.admin,
                # granted_by=user.id — сам себе вручил при bootstrap'е.
                # Альтернатива (NULL) тоже работает, но "сам себе"
                # отражает реальность лучше для аудита.
                granted_by=user.id,
            )
        )
        logger.info("Granted admin role to %s", email)
    else:
        logger.info("User %s already has admin role", email)

    await db.commit()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Создать первого admin-пользователя."
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    try:
        async with async_session_factory() as db:
            await ensure_admin(db, args.email, args.password)
    finally:
        # Закрываем pool — иначе скрипт виснет на выходе из-за открытых
        # соединений.
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
