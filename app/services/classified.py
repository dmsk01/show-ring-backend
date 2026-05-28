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
from app.models.file import UploadedFile
from app.repositories import classified as repo


async def _verify_files_owned(
    db: AsyncSession,
    images: list[dict],
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    """
    bug_212/216 audit 2026-05-28: каждый file_id, привязываемый к
    объявлению, должен принадлежать requester'у. Раньше можно было
    подсмотреть чужой file_id (например, из публичного аватара
    питомника) и прицепить его к своему объявлению — копирайт-абуз
    и подмена визуала чужого контента.

    Админ-исключение: модератор/админ может прицепить любой файл
    (например, при ручном фиксе чужого объявления).
    """
    if is_admin or not images:
        return
    for img in images:
        f = await db.get(UploadedFile, img["file_id"])
        if f is None or f.uploaded_by != requester_id:
            raise ValueError("file_forbidden")


async def _check_owner(
    classified: Classified,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    if classified.author_id != requester_id and not is_admin:
        raise ValueError("forbidden")


# ИСПРАВЛЕНО (bug_210 audit 2026-05-28): без этой проверки клиент мог
# через PUT /classifieds/{id} с body {"status": "active"} откатить
# закрытое или находящееся на модерации объявление обратно в active,
# минуя поток admin-модерации (/admin/moderation/classifieds/{id}).
# Архитектурное решение: пользователь сам управляет только парой
# active <-> closed (выложить / снять с продажи); смена в moderation
# и archived — прерогатива модератора/scheduler'а.
_USER_STATUS_TRANSITIONS: dict[ClassifiedStatus, set[ClassifiedStatus]] = {
    ClassifiedStatus.active: {ClassifiedStatus.closed},
    ClassifiedStatus.closed: {ClassifiedStatus.active},
    # moderation / archived — терминальные для пользовательских PUT'ов
    ClassifiedStatus.moderation: set(),
    ClassifiedStatus.archived: set(),
}


def _validate_status_transition(
    old: ClassifiedStatus,
    new: ClassifiedStatus,
    is_admin: bool,
) -> None:
    """
    Разрешает только безопасные переходы для обычного автора. Админ
    может ставить любой статус (нужен для модерации/восстановления).
    no-op при old == new — клиент мог прислать текущий статус.
    """
    if is_admin or old == new:
        return
    allowed = _USER_STATUS_TRANSITIONS.get(old, set())
    if new not in allowed:
        raise ValueError("status_transition_forbidden")


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
    # bug_212/216: автор создаёт объявление — он же должен быть владельцем
    # каждого file_id. is_admin=False, потому что POST идёт от обычного
    # пользователя; админ-bypass актуален только для update/add_images.
    await _verify_files_owned(db, images, author_id, is_admin=False)
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
    reloaded = await repo.get_classified(db, obj.id, with_images=True)
    # assert для pyright: после успешного commit запись с тем же id
    # точно существует — это invariant SQL'я, не runtime-проверка.
    assert reloaded is not None
    return reloaded


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

    # bug_210: смена статуса — отдельная политика. Проверяем ДО setattr,
    # чтобы не запачкать ORM-объект промежуточным новым значением (в
    # сессии expire_on_commit=False, и при rollback пришлось бы вручную
    # рефрешить).
    new_status = fields.get("status")
    if new_status is not None:
        _validate_status_transition(obj.status, new_status, is_admin)

    for k, v in fields.items():
        setattr(obj, k, v)

    await db.commit()
    # Дочитываем заново с images — после commit relationship уже
    # подгружен (мы загружали с with_images=True), но повторное чтение
    # гарантирует консистентность.
    reloaded = await repo.get_classified(db, classified_id, with_images=True)
    assert reloaded is not None  # invariant: только что обновили — точно есть
    return reloaded


async def add_images(
    db: AsyncSession,
    classified_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    images: list[dict],
) -> Classified:
    """
    Добавляет изображения к существующему объявлению. Только автор
    (или admin) может пополнять галерею — иначе любой авторизованный
    пользователь мог бы «прицепить» свои файлы к чужому объявлению.

    images — список dict'ов с file_id/position/is_primary (тот же
    формат, что в create_classified.images). Загрузка самих файлов
    идёт отдельно через POST /files/upload.

    UniqueConstraint("classified_id", "file_id") в БД защищает от
    дублирования одной картинки; повторная привязка кинет
    IntegrityError. На этом этапе пробрасываем дальше — пользователь
    увидит 400, либо клиент должен фильтровать дубликаты.
    """
    obj = await repo.get_classified(db, classified_id, with_images=True)
    if obj is None:
        raise ValueError("not_found")
    await _check_owner(obj, requester_id, is_admin)
    # bug_212/216: проверяем ownership каждого file_id.
    await _verify_files_owned(db, images, requester_id, is_admin)

    for img in images:
        await repo.add_image(
            db,
            classified_id=classified_id,
            file_id=img["file_id"],
            position=img.get("position", 0),
            is_primary=img.get("is_primary", False),
        )

    await db.commit()
    reloaded = await repo.get_classified(db, classified_id, with_images=True)
    assert reloaded is not None  # invariant: id известен (проверен выше)
    return reloaded


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
