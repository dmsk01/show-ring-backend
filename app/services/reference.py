"""
Бизнес-логика справочников (этап 3).

Сервис — единственное место, где живут проверки "можно ли", "что
произойдёт при ...". Роутер дергает сервис, сервис — репозиторий.
Так HTTP-слой не знает деталей SQL, а репозиторий не знает про HTTP.

Ошибки:
- ValueError("not_found") / ValueError("ref_in_use") — типизированные
  через текст-код, чтобы роутер легко мапил на HTTP-коды (404/409).
  Не используем кастомные исключения, потому что их пока мало;
  если их станет больше — выделим в app/exceptions.py.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reference import (
    AnimalType,
    Breed,
    BreedGroup,
    Grade,
    ShowClass,
    ShowRank,
    Title,
)
from app.repositories import reference as repo


# ---------------------------------------------------------------------
# Общий хелпер для апдейта
# ---------------------------------------------------------------------


def _apply_update(obj, fields: dict) -> None:
    """
    Безопасно применяет только переданные клиентом поля.
    exclude_unset на стороне роутера + setattr здесь — чтобы случайно
    не затереть существующее значение пустым None.
    """
    for k, v in fields.items():
        setattr(obj, k, v)


# ---------------------------------------------------------------------
# AnimalType
# ---------------------------------------------------------------------


async def create_animal_type(db: AsyncSession, **fields) -> AnimalType:
    try:
        obj = await repo.create_animal_type(db, **fields)
        await db.commit()
        return obj
    except IntegrityError:
        # UNIQUE по code. Откатываем — иначе сессия в "PendingRollback"
        # и следующий запрос свалится.
        await db.rollback()
        raise ValueError("duplicate_code")


async def update_animal_type(
    db: AsyncSession, id_: uuid.UUID, fields: dict
) -> AnimalType:
    obj = await repo.get_animal_type(db, id_)
    if obj is None:
        raise ValueError("not_found")
    _apply_update(obj, fields)
    try:
        await db.commit()
        await db.refresh(obj)
        return obj
    except IntegrityError:
        await db.rollback()
        raise ValueError("duplicate_code")


async def delete_animal_type(db: AsyncSession, id_: uuid.UUID) -> None:
    obj = await repo.get_animal_type(db, id_)
    if obj is None:
        raise ValueError("not_found")
    # Защита от каскадного сноса: на animal_type ссылаются все остальные
    # справочники. Удалять без подчищенных зависимостей нельзя.
    if await repo.animal_type_has_dependents(db, id_):
        raise ValueError("ref_in_use")
    await db.delete(obj)
    await db.commit()


# ---------------------------------------------------------------------
# BreedGroup
# ---------------------------------------------------------------------


async def create_breed_group(db: AsyncSession, **fields) -> BreedGroup:
    # Проверяем, что animal_type существует, до INSERT — иначе IntegrityError
    # от FK был бы менее информативный.
    if await repo.get_animal_type(db, fields["animal_type_id"]) is None:
        raise ValueError("animal_type_not_found")
    try:
        obj = await repo.create_breed_group(db, **fields)
        await db.commit()
        return obj
    except IntegrityError:
        await db.rollback()
        raise ValueError("duplicate_code")


async def update_breed_group(
    db: AsyncSession, id_: uuid.UUID, fields: dict
) -> BreedGroup:
    obj = await repo.get_breed_group(db, id_)
    if obj is None:
        raise ValueError("not_found")
    _apply_update(obj, fields)
    try:
        await db.commit()
        await db.refresh(obj)
        return obj
    except IntegrityError:
        await db.rollback()
        raise ValueError("duplicate_code")


async def delete_breed_group(db: AsyncSession, id_: uuid.UUID) -> None:
    obj = await repo.get_breed_group(db, id_)
    if obj is None:
        raise ValueError("not_found")
    # На уровне модели FK breeds.breed_group_id = SET NULL, но мы
    # хотим явный запрет: пусть админ сначала переназначит породы.
    # Это безопаснее (случайное удаление группы не "обнулит" 50 пород).
    if await repo.breed_group_has_breeds(db, id_):
        raise ValueError("ref_in_use")
    await db.delete(obj)
    await db.commit()


# ---------------------------------------------------------------------
# Breed
# ---------------------------------------------------------------------


async def create_breed(db: AsyncSession, **fields) -> Breed:
    if await repo.get_animal_type(db, fields["animal_type_id"]) is None:
        raise ValueError("animal_type_not_found")
    if fields.get("breed_group_id") is not None:
        group = await repo.get_breed_group(db, fields["breed_group_id"])
        if group is None:
            raise ValueError("breed_group_not_found")
        # Группа должна принадлежать тому же виду — иначе у нас "лабрадор
        # в группе кошек". База этого сама не проверит, делаем здесь.
        if group.animal_type_id != fields["animal_type_id"]:
            raise ValueError("animal_type_mismatch")
    try:
        obj = await repo.create_breed(db, **fields)
        await db.commit()
        return obj
    except IntegrityError:
        await db.rollback()
        raise ValueError("duplicate_code")


async def update_breed(db: AsyncSession, id_: uuid.UUID, fields: dict) -> Breed:
    obj = await repo.get_breed(db, id_)
    if obj is None:
        raise ValueError("not_found")
    if "breed_group_id" in fields and fields["breed_group_id"] is not None:
        group = await repo.get_breed_group(db, fields["breed_group_id"])
        if group is None:
            raise ValueError("breed_group_not_found")
        if group.animal_type_id != obj.animal_type_id:
            raise ValueError("animal_type_mismatch")
    _apply_update(obj, fields)
    try:
        await db.commit()
        await db.refresh(obj)
        return obj
    except IntegrityError:
        await db.rollback()
        raise ValueError("duplicate_code")


async def delete_breed(db: AsyncSession, id_: uuid.UUID) -> None:
    obj = await repo.get_breed(db, id_)
    if obj is None:
        raise ValueError("not_found")
    # На этапе 4 здесь добавится проверка: нельзя удалить породу,
    # если есть собаки этой породы (EXISTS из таблицы dogs).
    # Пока такой таблицы нет — оставляем заглушку в виде комментария.
    await db.delete(obj)
    await db.commit()


# ---------------------------------------------------------------------
# Общая фабрика для простых справочников (ShowClass/ShowRank/Title/Grade)
# ---------------------------------------------------------------------


async def _create_simple(
    db: AsyncSession,
    model_cls,
    fields: dict,
    check_animal_type: bool,
):
    if check_animal_type:
        if await repo.get_animal_type(db, fields["animal_type_id"]) is None:
            raise ValueError("animal_type_not_found")
    obj = model_cls(**fields)
    db.add(obj)
    try:
        await db.commit()
        await db.refresh(obj)
        return obj
    except IntegrityError:
        await db.rollback()
        raise ValueError("duplicate_code")


async def _update_simple(
    db: AsyncSession,
    obj,
    fields: dict,
):
    _apply_update(obj, fields)
    try:
        await db.commit()
        await db.refresh(obj)
        return obj
    except IntegrityError:
        await db.rollback()
        raise ValueError("duplicate_code")


async def _delete_simple(db: AsyncSession, obj) -> None:
    await db.delete(obj)
    await db.commit()


# ---------------------------------------------------------------------
# ShowClass / ShowRank / Title / Grade — тонкие обёртки
# ---------------------------------------------------------------------


async def create_show_class(db: AsyncSession, **fields):
    return await _create_simple(db, ShowClass, fields, check_animal_type=True)


async def update_show_class(db: AsyncSession, id_: uuid.UUID, fields: dict):
    obj = await repo.get_show_class(db, id_)
    if obj is None:
        raise ValueError("not_found")
    return await _update_simple(db, obj, fields)


async def delete_show_class(db: AsyncSession, id_: uuid.UUID):
    obj = await repo.get_show_class(db, id_)
    if obj is None:
        raise ValueError("not_found")
    await _delete_simple(db, obj)


async def create_show_rank(db: AsyncSession, **fields):
    return await _create_simple(db, ShowRank, fields, check_animal_type=False)


async def update_show_rank(db: AsyncSession, id_: uuid.UUID, fields: dict):
    obj = await repo.get_show_rank(db, id_)
    if obj is None:
        raise ValueError("not_found")
    return await _update_simple(db, obj, fields)


async def delete_show_rank(db: AsyncSession, id_: uuid.UUID):
    obj = await repo.get_show_rank(db, id_)
    if obj is None:
        raise ValueError("not_found")
    await _delete_simple(db, obj)


async def create_title(db: AsyncSession, **fields):
    return await _create_simple(db, Title, fields, check_animal_type=True)


async def update_title(db: AsyncSession, id_: uuid.UUID, fields: dict):
    obj = await repo.get_title(db, id_)
    if obj is None:
        raise ValueError("not_found")
    return await _update_simple(db, obj, fields)


async def delete_title(db: AsyncSession, id_: uuid.UUID):
    obj = await repo.get_title(db, id_)
    if obj is None:
        raise ValueError("not_found")
    await _delete_simple(db, obj)


async def create_grade(db: AsyncSession, **fields):
    return await _create_simple(db, Grade, fields, check_animal_type=True)


async def update_grade(db: AsyncSession, id_: uuid.UUID, fields: dict):
    obj = await repo.get_grade(db, id_)
    if obj is None:
        raise ValueError("not_found")
    return await _update_simple(db, obj, fields)


async def delete_grade(db: AsyncSession, id_: uuid.UUID):
    obj = await repo.get_grade(db, id_)
    if obj is None:
        raise ValueError("not_found")
    await _delete_simple(db, obj)
