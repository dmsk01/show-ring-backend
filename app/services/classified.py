"""
Сервис объявлений (этап 5).

Бизнес-правила:
- Создавать объявление может любой authenticated пользователь.
  author_id берётся из current_user, не от клиента.
- Редактировать/удалять — только автор или admin.
- DELETE на самом деле переводит status → closed (мягкое удаление),
  чтобы не терять историю и ссылки.
- При создании можно сразу привязать загруженные ранее файлы как
  изображения.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.classified import Classified, ClassifiedStatus
from app.repositories import classified as repo


async def _check_owner(
    classified: Classified,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    if classified.author_id != requester_id and not is_admin:
        raise ValueError("forbidden")


async def create_classified(
    db: AsyncSession,
    author_id: uuid.UUID,
    fields: dict,
) -> Classified:
    """
    Создаёт объявление и при наличии — привязывает к нему изображения.

    images — список dict'ов с file_id/position/is_primary. Каждый —
    ссылка на уже загруженный файл (загрузка идёт через /files/upload
    отдельным запросом).
    """
    images = fields.pop("images", []) or []
    obj = await repo.create_classified(db, author_id=author_id, **fields)

    for img in images:
        await repo.add_image(
            db,
            classified_id=obj.id,
            file_id=img["file_id"],
            position=img.get("position", 0),
            is_primary=img.get("is_primary", False),
        )

    await db.commit()
    # Перезагружаем объявление с подгруженными images через repo.get,
    # чтобы вернуть полный response. refresh(obj) без selectinload не
    # подтянет images в ту же сессию из-за expire_on_commit=False.
    return await repo.get_classified(db, obj.id, with_images=True)


async def update_classified(
    db: AsyncSession,
    classified_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    fields: dict,
) -> Classified:
    obj = await repo.get_classified(db, classified_id, with_images=True)
    if obj is None:
        raise ValueError("not_found")
    await _check_owner(obj, requester_id, is_admin)

    for k, v in fields.items():
        setattr(obj, k, v)

    await db.commit()
    # Дочитываем заново с images — после commit relationship уже
    # подгружен (мы загружали с with_images=True), но повторное чтение
    # гарантирует консистентность.
    return await repo.get_classified(db, classified_id, with_images=True)


async def close_classified(
    db: AsyncSession,
    classified_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    """
    "Удаление" объявления — это перевод в статус closed. Объявление
    остаётся в БД (для статистики, для отчётов), но скрывается из
    публичных списков (где фильтр status='active').
    """
    obj = await repo.get_classified(db, classified_id, with_images=False)
    if obj is None:
        raise ValueError("not_found")
    await _check_owner(obj, requester_id, is_admin)
    obj.status = ClassifiedStatus.closed
    await db.commit()
