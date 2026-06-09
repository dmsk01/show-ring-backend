"""
Репозиторий собак (этап 4).

Здесь же — построение родословной через рекурсивный CTE. Это первое
место в проекте, где мы используем чистый SQL (через text/CTE), потому
что:
- Рекурсия через ORM (загрузка отец → его отец → его отец) — это N+1
  и не масштабируется на 3-4 поколения.
- WITH RECURSIVE — стандартный SQL-приём для иерархических данных.
"""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dog import Dog, DogPhoto, SexEnum


# Белый список полей сортировки (этап 18): защита от инъекции —
# в order_by попадает только колонка из этой карты, не сырая строка.
_DOG_SORT = {
    "name": Dog.name,
    "date_of_birth": Dog.date_of_birth,
    "created_at": Dog.created_at,
}


def _dog_filter_stmt(
    breed_id: uuid.UUID | None,
    kennel_id: uuid.UUID | None,
    litter_id: uuid.UUID | None,
    sex: SexEnum | None,
    search: str | None,
    owner_id: uuid.UUID | None = None,
):
    stmt = select(Dog)
    if breed_id is not None:
        stmt = stmt.where(Dog.breed_id == breed_id)
    if kennel_id is not None:
        stmt = stmt.where(Dog.kennel_id == kennel_id)
    if litter_id is not None:
        stmt = stmt.where(Dog.litter_id == litter_id)
    if sex is not None:
        stmt = stmt.where(Dog.sex == sex)
    if owner_id is not None:
        stmt = stmt.where(Dog.owner_id == owner_id)
    if search:
        stmt = stmt.where(Dog.name.ilike(f"%{search}%"))
    return stmt


async def get_dog(db: AsyncSession, id_: uuid.UUID) -> Dog | None:
    return await db.get(Dog, id_)


async def dogs_by_ids(
    db: AsyncSession, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Dog]:
    """{id: Dog} одним запросом (для резолва родителей помёта и т.п.)."""
    uniq = list({i for i in ids if i is not None})
    if not uniq:
        return {}
    rows = (
        await db.execute(select(Dog).where(Dog.id.in_(uniq)))
    ).scalars().all()
    return {d.id: d for d in rows}


async def list_dogs(
    db: AsyncSession,
    breed_id: uuid.UUID | None = None,
    kennel_id: uuid.UUID | None = None,
    litter_id: uuid.UUID | None = None,
    sex: SexEnum | None = None,
    search: str | None = None,
    owner_id: uuid.UUID | None = None,
    sort_by: str = "name",
    order: str = "asc",
    page: int = 1,
    per_page: int = 50,
) -> Sequence[Dog]:
    col = _DOG_SORT.get(sort_by, Dog.name)
    stmt = (
        _dog_filter_stmt(breed_id, kennel_id, litter_id, sex, search, owner_id)
        .order_by(col.asc() if order == "asc" else col.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    return (await db.execute(stmt)).scalars().all()


async def count_dogs(
    db: AsyncSession,
    breed_id: uuid.UUID | None = None,
    kennel_id: uuid.UUID | None = None,
    litter_id: uuid.UUID | None = None,
    sex: SexEnum | None = None,
    search: str | None = None,
    owner_id: uuid.UUID | None = None,
) -> int:
    base = _dog_filter_stmt(
        breed_id, kennel_id, litter_id, sex, search, owner_id
    ).subquery()
    return int((await db.execute(select(func.count()).select_from(base))).scalar_one())


async def create_dog(db: AsyncSession, **fields) -> Dog:
    obj = Dog(**fields)
    db.add(obj)
    await db.flush()
    return obj


# --- Родословная (рекурсивный CTE) ---


# Запрос ниже строит "плоский список" всех предков на заданное число
# поколений с пометкой generation (0 = сам, 1 = родители, 2 = деды и т.д.).
# Затем мы собираем дерево в Python — это проще, чем строить дерево
# в SQL, и быстрее, чем N запросов по одному предку.
PEDIGREE_CTE = text(
    """
    WITH RECURSIVE pedigree AS (
        SELECT
            id, name, sex, date_of_birth, breed_id, rkf_number,
            father_id, mother_id,
            0 AS generation,
            ARRAY[id] AS path
        FROM dogs
        WHERE id = :root_id

        UNION ALL

        SELECT
            d.id, d.name, d.sex, d.date_of_birth, d.breed_id, d.rkf_number,
            d.father_id, d.mother_id,
            p.generation + 1,
            p.path || d.id
        FROM dogs d
        JOIN pedigree p ON d.id IN (p.father_id, p.mother_id)
        WHERE p.generation < :max_generations
          -- защита от цикла: если в данных оказался петля родителей,
          -- не зацикливаемся. path хранит цепочку, и мы её проверяем.
          AND NOT d.id = ANY(p.path)
    )
    SELECT id, name, sex, date_of_birth, breed_id, rkf_number,
           father_id, mother_id, generation
    FROM pedigree
    ORDER BY generation, name
    """
)


async def load_pedigree_flat(
    db: AsyncSession, root_id: uuid.UUID, max_generations: int = 3
) -> list[dict]:
    """
    Возвращает плоский список всех предков (включая самого) до глубины
    max_generations. Сборка дерева — на стороне сервиса.
    """
    result = await db.execute(
        PEDIGREE_CTE,
        {"root_id": root_id, "max_generations": max_generations},
    )
    return [dict(row) for row in result.mappings().all()]


# --- Фото собак ---


async def add_dog_photo(
    db: AsyncSession,
    dog_id: uuid.UUID,
    file_id: uuid.UUID,
    position: int = 0,
    is_primary: bool = False,
) -> DogPhoto:
    obj = DogPhoto(
        dog_id=dog_id, file_id=file_id, position=position, is_primary=is_primary
    )
    db.add(obj)
    await db.flush()
    return obj


async def get_dog_photo(
    db: AsyncSession, dog_id: uuid.UUID, file_id: uuid.UUID
) -> DogPhoto | None:
    """Связь dog_photos по паре (dog_id, file_id). None — если нет."""
    stmt = select(DogPhoto).where(
        DogPhoto.dog_id == dog_id, DogPhoto.file_id == file_id
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def delete_dog_photo(db: AsyncSession, photo: DogPhoto) -> None:
    """Удаляет связь dog_photos. Сам файл в хранилище не трогаем."""
    await db.delete(photo)
    await db.flush()


async def list_dog_photos(
    db: AsyncSession, dog_id: uuid.UUID
) -> list[DogPhoto]:
    """Фото одной собаки, по position."""
    stmt = (
        select(DogPhoto)
        .where(DogPhoto.dog_id == dog_id)
        .order_by(DogPhoto.position)
    )
    return list((await db.execute(stmt)).scalars().all())


async def photos_by_dogs(
    db: AsyncSession, dog_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[DogPhoto]]:
    """{dog_id: [DogPhoto…]} одним запросом — анти-N+1 для списков."""
    ids = list(dog_ids)
    if not ids:
        return {}
    stmt = (
        select(DogPhoto)
        .where(DogPhoto.dog_id.in_(ids))
        .order_by(DogPhoto.position)
    )
    out: dict[uuid.UUID, list[DogPhoto]] = {}
    for p in (await db.execute(stmt)).scalars().all():
        out.setdefault(p.dog_id, []).append(p)
    return out
