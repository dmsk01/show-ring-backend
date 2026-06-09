"""
Сервис собак и родословной (этап 4).

Бизнес-правила:
- Добавлять собаку может владелец питомника (где собака будет числиться)
  или admin. Если kennel_id=None — любой авторизованный заводчик.
- Пол родителей должен соответствовать роли (отец=male, мать=female) —
  иначе родословная теряет смысл.
- Собака не может быть собственным предком (защита от цикла в self-ref).
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import Dog, SexEnum
from app.models.kennel import Kennel
from app.repositories import dog as repo
from app.repositories import kennel as kennel_repo
from app.schemas.dog import PedigreeNode


async def _validate_parents(
    db: AsyncSession,
    father_id: uuid.UUID | None,
    mother_id: uuid.UUID | None,
    self_id: uuid.UUID | None = None,
) -> None:
    if father_id is not None:
        father = await repo.get_dog(db, father_id)
        if father is None:
            raise ValueError("father_not_found")
        if father.sex != SexEnum.male:
            raise ValueError("father_must_be_male")
        if self_id is not None and father.id == self_id:
            raise ValueError("self_parent_forbidden")
    if mother_id is not None:
        mother = await repo.get_dog(db, mother_id)
        if mother is None:
            raise ValueError("mother_not_found")
        if mother.sex != SexEnum.female:
            raise ValueError("mother_must_be_female")
        if self_id is not None and mother.id == self_id:
            raise ValueError("self_parent_forbidden")


async def _check_kennel_owner(
    db: AsyncSession,
    kennel_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> Kennel:
    kennel = await kennel_repo.get_kennel(db, kennel_id)
    if kennel is None:
        raise ValueError("kennel_not_found")
    if kennel.owner_id != requester_id and not is_admin:
        raise ValueError("forbidden")
    return kennel


async def create_dog(
    db: AsyncSession,
    requester_id: uuid.UUID,
    is_admin: bool,
    fields: dict,
) -> Dog:
    # Если собаку привязывают к питомнику — проверяем, что заводчик
    # имеет на это право (его питомник). Без питомника — пропускаем.
    if fields.get("kennel_id"):
        await _check_kennel_owner(
            db, fields["kennel_id"], requester_id, is_admin
        )
    await _validate_parents(
        db, fields.get("father_id"), fields.get("mother_id")
    )
    # Владелец карточки — всегда тот, кто создаёт собаку, независимо от
    # наличия питомника. Это даёт прямую связь dog → user для «моих собак»
    # и проверки записи на выставку. owner_id не приходит из тела запроса
    # (его нет в DogCreate), поэтому подменить чужого владельца нельзя.
    fields["owner_id"] = requester_id
    try:
        obj = await repo.create_dog(db, **fields)
        await db.commit()
        await db.refresh(obj)
        return obj
    except IntegrityError:
        await db.rollback()
        # Скорее всего UNIQUE rkf_number. Не указываем точно поле в
        # detail, чтобы не раскрывать структуру БД лишний раз.
        raise ValueError("duplicate_unique_field")


async def update_dog(
    db: AsyncSession,
    dog_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    fields: dict,
) -> Dog:
    obj = await repo.get_dog(db, dog_id)
    if obj is None:
        raise ValueError("not_found")
    # Право на правку: владелец питомника, к которому привязана собака,
    # либо admin. Если собака без питомника — только admin (т.к. у нас
    # нет прямого FK dog → user).
    if obj.kennel_id is not None:
        await _check_kennel_owner(db, obj.kennel_id, requester_id, is_admin)
    elif not is_admin:
        raise ValueError("forbidden")

    if "kennel_id" in fields and fields["kennel_id"] is not None:
        # Перенос в другой питомник — нужно право на новый питомник тоже.
        await _check_kennel_owner(
            db, fields["kennel_id"], requester_id, is_admin
        )

    await _validate_parents(
        db,
        fields.get("father_id"),
        fields.get("mother_id"),
        self_id=obj.id,
    )

    for k, v in fields.items():
        setattr(obj, k, v)
    try:
        await db.commit()
        await db.refresh(obj)
        return obj
    except IntegrityError:
        await db.rollback()
        raise ValueError("duplicate_unique_field")


async def delete_dog(
    db: AsyncSession,
    dog_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    obj = await repo.get_dog(db, dog_id)
    if obj is None:
        raise ValueError("not_found")
    # Право (как в update_dog): владелец питомника собаки или admin.
    # Собака без питомника — только admin (прямого FK dog→user нет).
    if obj.kennel_id is not None:
        await _check_kennel_owner(db, obj.kennel_id, requester_id, is_admin)
    elif not is_admin:
        raise ValueError("forbidden")
    # Каскады БД: dog_photos, show_entries (а с ними show_results) и
    # dog_titles удаляются (ON DELETE CASCADE). Ссылки детей/помётов на
    # эту собаку как родителя (father_id/mother_id) → SET NULL.
    await db.delete(obj)
    await db.commit()


async def add_images(
    db: AsyncSession,
    dog_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
    images: list[dict],
) -> Dog:
    """
    Привязывает уже загруженные файлы к собаке (этап 18). Право — владелец
    питомника собаки или admin; собака без питомника — только admin (нет
    прямого FK dog→user). Зеркало classified.add_images.
    """
    dog = await repo.get_dog(db, dog_id)
    if dog is None:
        raise ValueError("not_found")
    if dog.kennel_id is not None:
        await _check_kennel_owner(db, dog.kennel_id, requester_id, is_admin)
    elif not is_admin:
        raise ValueError("forbidden")

    try:
        for img in images:
            await repo.add_dog_photo(
                db,
                dog_id,
                img["file_id"],
                img.get("position", 0),
                img.get("is_primary", False),
            )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # UNIQUE(dog_id, file_id) — файл уже привязан.
        raise ValueError("duplicate_unique_field")
    return dog


async def delete_image(
    db: AsyncSession,
    dog_id: uuid.UUID,
    file_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> Dog:
    """
    Открепляет фото от собаки — удаляет только связь dog_photos, сам файл в
    хранилище не трогаем (этап 18). Право — владелец питомника собаки или
    admin; собака без питомника — только admin (нет прямого FK dog→user).
    Зеркало add_images.
    """
    dog = await repo.get_dog(db, dog_id)
    if dog is None:
        raise ValueError("not_found")
    if dog.kennel_id is not None:
        await _check_kennel_owner(db, dog.kennel_id, requester_id, is_admin)
    elif not is_admin:
        raise ValueError("forbidden")

    photo = await repo.get_dog_photo(db, dog_id, file_id)
    if photo is None:
        raise ValueError("photo_not_found")

    was_primary = photo.is_primary
    await repo.delete_dog_photo(db, photo)

    # Если открепили главное фото и остались другие — назначаем главным фото
    # с наименьшим position, чтобы аватар собаки не «сломался». Гасим случай
    # уже существующего главного среди оставшихся (не плодим второе).
    if was_primary:
        remaining = await repo.list_dog_photos(db, dog_id)
        if remaining and not any(p.is_primary for p in remaining):
            remaining[0].is_primary = True

    await db.commit()
    return dog


# ---------------------------------------------------------------------
# Родословная
# ---------------------------------------------------------------------


async def build_pedigree(
    db: AsyncSession, root_id: uuid.UUID, generations: int = 3
) -> PedigreeNode | None:
    """
    Тянет родословную одним запросом (CTE) и собирает дерево в Python.

    Без CTE мы бы делали 2^N запросов (на N поколений), что
    неприемлемо даже для 3 уровней.
    """
    flat = await repo.load_pedigree_flat(db, root_id, generations)
    if not flat:
        return None

    by_id: dict[uuid.UUID, dict] = {row["id"]: row for row in flat}
    # Корень — собака с generation=0.
    root_row = next(r for r in flat if r["generation"] == 0)

    def _make(node_row) -> PedigreeNode:
        # Рекурсивно собираем PedigreeNode из плоских строк.
        # node_row["father_id"] может ссылаться на узел, которого нет
        # в выборке (он за пределами generations) — тогда оставляем None.
        father_row = by_id.get(node_row["father_id"]) if node_row["father_id"] else None
        mother_row = by_id.get(node_row["mother_id"]) if node_row["mother_id"] else None
        return PedigreeNode(
            id=node_row["id"],
            name=node_row["name"],
            sex=node_row["sex"],
            date_of_birth=node_row["date_of_birth"],
            breed_id=node_row["breed_id"],
            rkf_number=node_row["rkf_number"],
            father=_make(father_row) if father_row else None,
            mother=_make(mother_row) if mother_row else None,
        )

    return _make(root_row)
