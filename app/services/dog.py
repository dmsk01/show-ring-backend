"""
Сервис собак и родословной (этап 4).

Бизнес-правила:
- Добавлять собаку может владелец питомника (где собака будет числиться)
  или admin. Если kennel_id=None — любой авторизованный заводчик.
- Управлять карточкой (update/delete/фото) может прямой владелец
  (Dog.owner_id), владелец питомника собаки или admin.
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
from app.schemas.dog import (
    DogDescendant,
    DogRef,
    DogRelative,
    DogSibling,
    PedigreeNode,
)


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
    # Родитель не может быть потомком самой собаки (на любой глубине) —
    # иначе родословная замыкается в цикл. Только для существующей собаки:
    # у новой (create) потомков ещё нет.
    if self_id is not None:
        for parent_id in (father_id, mother_id):
            if parent_id is not None and await repo.is_ancestor(db, self_id, parent_id):
                raise ValueError("pedigree_cycle")


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


async def _check_can_manage_dog(
    db: AsyncSession,
    dog: Dog,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    """
    Единое право на управление карточкой собаки (update/delete/фото):
    прямой владелец (Dog.owner_id), владелец питомника собаки или admin.

    ИСПРАВЛЕНО (review 2026-06-10): после ввода Dog.owner_id четыре
    операции оставались на старой модели «владелец питомника или admin» —
    владелец собаки без питомника не мог редактировать собственную
    карточку. Симметрично _check_can_register_dog в services/show.py.
    """
    if is_admin or dog.owner_id == requester_id:
        return
    if dog.kennel_id is not None:
        kennel = await kennel_repo.get_kennel(db, dog.kennel_id)
        if kennel is not None and kennel.owner_id == requester_id:
            return
    raise ValueError("forbidden")


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
    # Право на правку: владелец собаки (owner_id), владелец питомника
    # или admin — см. _check_can_manage_dog.
    await _check_can_manage_dog(db, obj, requester_id, is_admin)

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
    # Право (как в update_dog): владелец собаки, владелец питомника
    # или admin.
    await _check_can_manage_dog(db, obj, requester_id, is_admin)
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
    собаки, владелец питомника или admin (_check_can_manage_dog).
    Зеркало classified.add_images.
    """
    dog = await repo.get_dog(db, dog_id)
    if dog is None:
        raise ValueError("not_found")
    await _check_can_manage_dog(db, dog, requester_id, is_admin)

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
    хранилище не трогаем (этап 18). Право — владелец собаки, владелец
    питомника или admin (_check_can_manage_dog). Зеркало add_images.
    """
    dog = await repo.get_dog(db, dog_id)
    if dog is None:
        raise ValueError("not_found")
    await _check_can_manage_dog(db, dog, requester_id, is_admin)

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
# Потомки и сибсы
# ---------------------------------------------------------------------
#
# Родство НЕ хранится отдельными связями — выводится из father_id/mother_id.
# Так «потомки»/«сибсы» не могут разойтись с родословной. «Добавить потомка»
# = проставить эту собаку отцом/матерью выбранной собаке (слот — по полу
# родителя); «убрать потомка» = очистить этот слот. Сибсы только читаются:
# они меняются через родителей.


def _parent_slot(parent: Dog) -> str:
    """Поле потомка, в которое встаёт parent: father_id для кобеля, иначе mother_id."""
    return "father_id" if parent.sex == SexEnum.male else "mother_id"


async def _relatives_photos(db: AsyncSession, dogs) -> dict[uuid.UUID, uuid.UUID | None]:
    """{dog_id: avatar_file_id} пачкой (анти-N+1), та же логика, что в DogResponse."""
    photos = await repo.photos_by_dogs(db, [d.id for d in dogs])
    avatars: dict[uuid.UUID, uuid.UUID | None] = {}
    for d in dogs:
        ordered = sorted(photos.get(d.id, []), key=lambda p: p.position)
        avatars[d.id] = next(
            (p.file_id for p in ordered if p.is_primary), None
        ) or (ordered[0].file_id if ordered else None)
    return avatars


async def list_descendants(
    db: AsyncSession, dog_id: uuid.UUID
) -> list[DogDescendant]:
    """Прямые потомки (одно поколение) + второй родитель каждого."""
    dog = await repo.get_dog(db, dog_id)
    if dog is None:
        raise ValueError("not_found")
    children = await repo.list_children(db, dog_id)
    avatars = await _relatives_photos(db, children)
    other_ids = [
        c.mother_id if c.father_id == dog_id else c.father_id for c in children
    ]
    others = await repo.dogs_by_ids(db, other_ids)
    result = []
    for child, other_id in zip(children, other_ids):
        item = DogDescendant.model_validate(child)
        item.avatar_file_id = avatars[child.id]
        other = others.get(other_id) if other_id else None
        item.other_parent = DogRef(id=other.id, name=other.name) if other else None
        result.append(item)
    return result


async def list_siblings(db: AsyncSession, dog_id: uuid.UUID) -> list[DogSibling]:
    """Сибсы: полнородные (оба родителя общие) и полукровные (один общий)."""
    dog = await repo.get_dog(db, dog_id)
    if dog is None:
        raise ValueError("not_found")
    siblings = await repo.list_siblings(db, dog)
    avatars = await _relatives_photos(db, siblings)
    result = []
    for s in siblings:
        same_father = dog.father_id is not None and s.father_id == dog.father_id
        same_mother = dog.mother_id is not None and s.mother_id == dog.mother_id
        shared = "both" if same_father and same_mother else (
            "father" if same_father else "mother"
        )
        item = DogSibling.model_validate(
            {
                **DogRelative.model_validate(s).model_dump(),
                "kind": "full" if shared == "both" else "half",
                "shared_parent": shared,
            }
        )
        item.avatar_file_id = avatars[s.id]
        result.append(item)
    # Полнородные первыми — они «ближе».
    result.sort(key=lambda x: x.kind != "full")
    return result


async def add_descendant(
    db: AsyncSession,
    parent_id: uuid.UUID,
    child_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> DogDescendant:
    """
    Делает child потомком parent: child.father_id/mother_id = parent.id.
    Меняется карточка ПОТОМКА, поэтому право — на управление потомком.
    Занятый другим родителем слот не перезаписываем молча (409) — сменить
    родителя можно явно, в форме собаки.
    """
    parent = await repo.get_dog(db, parent_id)
    if parent is None:
        raise ValueError("not_found")
    child = await repo.get_dog(db, child_id)
    if child is None:
        raise ValueError("child_not_found")
    await _check_can_manage_dog(db, child, requester_id, is_admin)

    if child.id == parent.id:
        raise ValueError("self_parent_forbidden")
    slot = _parent_slot(parent)
    current = getattr(child, slot)
    if current is not None and current != parent.id:
        raise ValueError("parent_already_set")
    if current is None:
        # Потомок не может быть предком своего родителя — иначе петля в
        # родословной (CTE её переживёт, но данные станут бессмысленными).
        if await repo.is_ancestor(db, child.id, parent.id):
            raise ValueError("pedigree_cycle")
        setattr(child, slot, parent.id)
        await db.commit()
        await db.refresh(child)

    return next(d for d in await list_descendants(db, parent.id) if d.id == child.id)


async def remove_descendant(
    db: AsyncSession,
    parent_id: uuid.UUID,
    child_id: uuid.UUID,
    requester_id: uuid.UUID,
    is_admin: bool,
) -> None:
    """Разрывает связь родитель → потомок (очищает слот у потомка)."""
    parent = await repo.get_dog(db, parent_id)
    if parent is None:
        raise ValueError("not_found")
    child = await repo.get_dog(db, child_id)
    slot = _parent_slot(parent)
    if child is None or getattr(child, slot) != parent.id:
        raise ValueError("descendant_not_found")
    await _check_can_manage_dog(db, child, requester_id, is_admin)
    setattr(child, slot, None)
    await db.commit()


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
