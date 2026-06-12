r"""
Идемпотентный сид e2e-пользователей для Playwright-тестов фронта
(логин через POST /auth/login).

По одному пользователю на роль + один мульти-ролевой:
    organizer@e2e.example   organizer
    breeder@e2e.example     breeder
    judge@e2e.example       judge
    buyer@e2e.example       buyer
    operator@e2e.example    operator
    multi@e2e.example       breeder + organizer (проверка union прав)

Пароль у всех: Password123!

ВАЖНО — домен @e2e.example, а НЕ @e2e.test, как просил фронт:
pydantic EmailStr (email-validator) отвергает зарезервированные TLD
.test/.local/.invalid/.localhost, и /auth/login отвечал бы 422 ещё до
проверки пароля. .example проходит валидацию и при этом зарезервирован
под документацию/тесты (RFC 2606) — реальные письма туда не уйдут.

Роль admin НЕ сидируется: админ уже существует (admin@admin.com),
по договорённости с фронтом его не трогаем. Скрипт лишь проверяет его
наличие и предупреждает, если он не найден.

Идемпотентность: upsert по email — повторный запуск не падает, не
плодит дублей и приводит запись к ожидаемому состоянию (пароль,
is_active=True, is_email_verified=True, профиль, роли). Сам логин
требует только is_active=True и пароль; is_email_verified входным
гейтом не является, но проставляется по контракту e2e. Лишние роли,
навешанные тестами поверх сида, скрипт не снимает.

Запуск:
    .\venv\Scripts\python.exe -m scripts.seed_e2e_users
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory, engine
# files — таблица-адресат FK users.avatar_file_id, нужна в metadata.
from app.models import file  # noqa: F401
from app.models.user import RoleEnum, User, UserProfile, UserRole
from app.utils.security import hash_password, verify_password

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed-e2e-users")

E2E_PASSWORD = "Password123!"
E2E_DOMAIN = "e2e.example"
ADMIN_EMAIL = "admin@admin.com"  # существующий админ — не трогаем

# (local-part, роли, фамилия, имя, отчество)
E2E_USERS: list[tuple[str, list[RoleEnum], str, str, str]] = [
    ("organizer", [RoleEnum.organizer], "Орлова", "Ольга", "Олеговна"),
    ("breeder", [RoleEnum.breeder], "Бобров", "Борис", "Борисович"),
    ("judge", [RoleEnum.judge], "Жданова", "Жанна", "Игоревна"),
    ("buyer", [RoleEnum.buyer], "Панов", "Пётр", "Павлович"),
    ("operator", [RoleEnum.operator], "Озерова", "Оксана", "Олеговна"),
    # union прав breeder + organizer
    ("multi", [RoleEnum.breeder, RoleEnum.organizer],
     "Мухина", "Мария", "Михайловна"),
]


async def _upsert_user(
    db: AsyncSession,
    email: str,
    *,
    roles: list[RoleEnum],
    last: str,
    first: str,
    patr: str,
) -> User:
    """Привести пользователя к ожидаемому e2e-состоянию (upsert по email)."""
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            email=email,
            hashed_password=hash_password(E2E_PASSWORD),
            is_active=True,
            is_email_verified=True,
        )
        db.add(user)
        await db.flush()
        logger.info("создан    %s", email)
    else:
        # bcrypt дорогой — перехешируем только если пароль разъехался.
        if not (
            user.hashed_password
            and verify_password(E2E_PASSWORD, user.hashed_password)
        ):
            user.hashed_password = hash_password(E2E_PASSWORD)
        user.is_active = True
        user.is_email_verified = True
        logger.info("обновлён  %s", email)

    # Профиль (ФИО + страна) нужен e2e-сценариям с официальными
    # документами; перезаписываем целиком — фикстура детерминированна.
    profile = await db.get(UserProfile, user.id)
    if profile is None:
        db.add(UserProfile(
            user_id=user.id, last_name=last, first_name=first,
            patronymic=patr, country="Россия",
        ))
    else:
        profile.last_name = last
        profile.first_name = first
        profile.patronymic = patr
        profile.country = "Россия"

    for role in roles:
        existing = (
            await db.execute(
                select(UserRole).where(
                    UserRole.user_id == user.id, UserRole.role == role
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(UserRole(user_id=user.id, role=role, granted_by=user.id))

    await db.flush()
    return user


async def seed(db: AsyncSession) -> None:
    for local, roles, last, first, patr in E2E_USERS:
        await _upsert_user(
            db, f"{local}@{E2E_DOMAIN}",
            roles=roles, last=last, first=first, patr=patr,
        )

    admin = (
        await db.execute(select(User).where(User.email == ADMIN_EMAIL))
    ).scalar_one_or_none()
    if admin is None:
        logger.warning(
            "админ %s не найден — e2e-сценарии под админом упадут. "
            "Сид его НАМЕРЕННО не создаёт (учётка вне контракта e2e).",
            ADMIN_EMAIL,
        )

    await db.commit()
    await _print_summary(db)


async def _print_summary(db: AsyncSession) -> None:
    """Контрольная сводка: все поля, от которых зависит e2e-логин."""
    emails = [f"{local}@{E2E_DOMAIN}" for local, *_ in E2E_USERS]
    emails.append(ADMIN_EMAIL)
    print("\n" + "=" * 72)
    print(f"E2E-СИД ГОТОВ (пароль у всех @{E2E_DOMAIN}: {E2E_PASSWORD})")
    for email in emails:
        user = (
            await db.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user is None:
            print(f"  {email:26} ОТСУТСТВУЕТ")
            continue
        roles = sorted(
            r.role.value for r in (
                await db.execute(
                    select(UserRole).where(UserRole.user_id == user.id)
                )
            ).scalars()
        )
        profile = await db.get(UserProfile, user.id)
        fio = (
            f"{profile.last_name} {profile.first_name} {profile.patronymic}"
            if profile else "<без профиля>"
        )
        print(
            f"  {email:26} active={user.is_active} "
            f"verified={user.is_email_verified} "
            f"pwd={'да' if user.hashed_password else 'НЕТ'} "
            f"roles={','.join(roles) or '-'} | {fio}"
        )
    print("=" * 72)


async def main() -> None:
    # Тот же предохранитель, что в seed_demo: активные пользователи с
    # общеизвестным паролем на прод-базе — бэкдор. Только settings.debug
    # или явный --force.
    from app.config import settings

    if not settings.debug and "--force" not in sys.argv:
        logger.error(
            "Отказ: settings.debug=False (похоже на прод). E2E-сид "
            "создаёт аккаунты с общеизвестным паролем. Если вы уверены — "
            "повторите с флагом --force."
        )
        raise SystemExit(1)
    try:
        async with async_session_factory() as db:
            await seed(db)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
