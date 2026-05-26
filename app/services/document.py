"""
Сервис формирования данных для PDF-документов (этап 8).

Назначение:
- На стороне воркера принимает task.payload и собирает из БД
  структурированные данные для рендера PDF.
- Не делает сам рендер — это в app/utils/pdf.py.

Зачем разделять "сбор данных" и "рендер":
- сбор тестируется отдельно (unit-тест на структуру dict),
- рендер тестируется отдельно (snapshot PDF),
- удобно повторно использовать данные для других форматов (HTML, CSV).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dog import Dog
from app.models.kennel import Kennel
from app.models.reference import Breed, BreedGroup, ShowClass, ShowRank
from app.models.result import ShowResult
from app.models.show import Show, ShowEntry, ShowJudge
from app.models.user import User


# ---------------------------------------------------------------------
# DTO для PDF
# ---------------------------------------------------------------------


@dataclass
class CatalogEntry:
    """Одна строка каталога — собака под номером."""

    catalog_number: int | None
    dog_name: str
    date_of_birth: date | None
    color: str | None
    rkf_number: str | None
    sex: str
    owner_name: str | None
    breeder_name: str | None  # питомник


@dataclass
class CatalogClassSection:
    """Секция в каталоге: класс внутри породы."""

    class_name: str
    class_code: str
    entries: list[CatalogEntry] = field(default_factory=list)


@dataclass
class CatalogBreedSection:
    """Секция в каталоге: порода (с судьёй и классами)."""

    breed_name: str
    breed_code: str
    fci_number: str | None
    group_number: int | None
    judge_name: str | None
    classes: list[CatalogClassSection] = field(default_factory=list)


@dataclass
class CatalogJudge:
    """Запись о судье в шапке каталога."""

    name: str
    breeds_or_groups: str  # текстовое описание назначения


@dataclass
class CatalogData:
    """Полные данные для рендера каталога выставки."""

    show_name: str
    show_rank: str
    date_start: date
    date_end: date | None
    city: str | None
    venue: str | None
    judges: list[CatalogJudge] = field(default_factory=list)
    breed_sections: list[CatalogBreedSection] = field(default_factory=list)
    total_entries: int = 0


@dataclass
class DiplomaData:
    """Данные для одного диплома."""

    show_name: str
    show_rank: str
    date_start: date
    city: str | None
    breed_name: str
    class_name: str
    dog_name: str
    rkf_number: str | None
    grade_name: str | None
    placement: int | None
    titles: list[str]
    judge_name: str | None
    owner_name: str | None


# ---------------------------------------------------------------------
# Утилиты для имён
# ---------------------------------------------------------------------


def _user_display(user: User | None) -> str | None:
    """
    Текстовое представление юзера. Пока у User есть только email —
    отдаём его. Когда появится first_name/last_name (этап на будущее),
    функция остаётся одной точкой изменений.
    """
    if user is None:
        return None
    return user.email


# ---------------------------------------------------------------------
# Каталог
# ---------------------------------------------------------------------


async def build_catalog_data(
    db: AsyncSession, show_id: uuid.UUID
) -> CatalogData:
    """
    Собирает данные каталога выставки одним запросом-блоком и группирует
    в Python. Один большой JOIN был бы быстрее, но менее читаем — для
    PDF-каталога раз в день экономия не критична.
    """
    show = await db.get(Show, show_id)
    if show is None:
        raise ValueError("not_found")

    rank = await db.get(ShowRank, show.rank_id)

    # Судьи с подгруженными breed/group для текстового описания.
    judges_stmt = (
        select(ShowJudge)
        .where(ShowJudge.show_id == show_id)
        # joined-load связанных пород/групп/юзера не делаем — там простые
        # справочники, dao на breed/breed_group/user сэкономит сложность.
    )
    judges = (await db.execute(judges_stmt)).scalars().all()
    judges_section: list[CatalogJudge] = []
    for j in judges:
        target = "—"
        if j.breed_id is not None:
            br = await db.get(Breed, j.breed_id)
            if br is not None:
                target = f"порода: {br.name}"
        elif j.breed_group_id is not None:
            grp = await db.get(BreedGroup, j.breed_group_id)
            if grp is not None:
                target = f"группа FCI {grp.number}: {grp.name}"
        judge_user = await db.get(User, j.judge_id)
        judges_section.append(
            CatalogJudge(
                name=_user_display(judge_user) or "—",
                breeds_or_groups=target,
            )
        )

    # Записи участников с подгрузкой собак (нужны для группировки и
    # детальной строки каталога). selectinload — отдельный SELECT по
    # списку id, без декартова умножения.
    entries_stmt = (
        select(ShowEntry)
        .where(ShowEntry.show_id == show_id)
        .order_by(ShowEntry.catalog_number.asc().nullslast())
    )
    entries = (await db.execute(entries_stmt)).scalars().all()
    if not entries:
        return CatalogData(
            show_name=show.name,
            show_rank=rank.name if rank else "",
            date_start=show.date_start,
            date_end=show.date_end,
            city=show.city,
            venue=show.venue,
            judges=judges_section,
            breed_sections=[],
            total_entries=0,
        )

    # Подгружаем собак, породы и классы скопом через id-сеты.
    dog_ids = {e.dog_id for e in entries}
    class_ids = {e.show_class_id for e in entries}
    dogs_by_id: dict[uuid.UUID, Dog] = {
        d.id: d
        for d in (
            await db.execute(select(Dog).where(Dog.id.in_(dog_ids)))
        ).scalars()
    }
    breed_ids = {d.breed_id for d in dogs_by_id.values()}
    breeds_by_id: dict[uuid.UUID, Breed] = {
        b.id: b
        for b in (
            await db.execute(select(Breed).where(Breed.id.in_(breed_ids)))
        ).scalars()
    }
    group_ids = {b.breed_group_id for b in breeds_by_id.values() if b.breed_group_id}
    groups_by_id: dict[uuid.UUID, BreedGroup] = {
        g.id: g
        for g in (
            await db.execute(
                select(BreedGroup).where(BreedGroup.id.in_(group_ids))
            )
        ).scalars()
    } if group_ids else {}
    classes_by_id: dict[uuid.UUID, ShowClass] = {
        c.id: c
        for c in (
            await db.execute(
                select(ShowClass).where(ShowClass.id.in_(class_ids))
            )
        ).scalars()
    }
    kennel_ids = {d.kennel_id for d in dogs_by_id.values() if d.kennel_id}
    kennels_by_id: dict[uuid.UUID, Kennel] = (
        {
            k.id: k
            for k in (
                await db.execute(
                    select(Kennel)
                    .where(Kennel.id.in_(kennel_ids))
                    .options(selectinload(Kennel.dogs))  # для name owner
                )
            ).scalars()
        }
        if kennel_ids
        else {}
    )
    owner_ids = {k.owner_id for k in kennels_by_id.values()}
    owners_by_id: dict[uuid.UUID, User] = (
        {
            u.id: u
            for u in (
                await db.execute(select(User).where(User.id.in_(owner_ids)))
            ).scalars()
        }
        if owner_ids
        else {}
    )

    # Группируем: breed_id → class_id → [CatalogEntry].
    # Для стабильного порядка пород по группам собираем порядок групп
    # отдельно: сначала те, у кого есть group_number, в порядке номера.
    breed_buckets: dict[uuid.UUID, dict[uuid.UUID, list[CatalogEntry]]] = {}
    for e in entries:
        dog = dogs_by_id.get(e.dog_id)
        if dog is None:
            continue
        breed = breeds_by_id.get(dog.breed_id)
        if breed is None:
            continue
        kennel = (
            kennels_by_id.get(dog.kennel_id) if dog.kennel_id else None
        )
        owner = owners_by_id.get(kennel.owner_id) if kennel else None
        entry_row = CatalogEntry(
            catalog_number=e.catalog_number,
            dog_name=dog.name,
            date_of_birth=dog.date_of_birth,
            color=dog.color,
            rkf_number=dog.rkf_number,
            sex=dog.sex.value,
            owner_name=_user_display(owner),
            breeder_name=kennel.name if kennel else None,
        )
        breed_buckets.setdefault(breed.id, {}).setdefault(
            e.show_class_id, []
        ).append(entry_row)

    # Сортировка пород: по group_number → по name.
    def _breed_sort_key(b: Breed) -> tuple[int, str]:
        group = groups_by_id.get(b.breed_group_id) if b.breed_group_id else None
        # NULL-группа в конец.
        return (group.number if group else 999, b.name)

    sorted_breeds = sorted(
        (breeds_by_id[bid] for bid in breed_buckets), key=_breed_sort_key
    )

    # Назначения судей: build "breed_id → judge_user" заранее для
    # подстановки в шапку секции породы.
    judge_for_breed: dict[uuid.UUID, str | None] = {}
    for j in judges:
        if j.breed_id is not None:
            ju = await db.get(User, j.judge_id)
            judge_for_breed[j.breed_id] = _user_display(ju)

    sections: list[CatalogBreedSection] = []
    for breed in sorted_breeds:
        group = (
            groups_by_id.get(breed.breed_group_id)
            if breed.breed_group_id
            else None
        )
        # Сортируем классы по age_from_months — естественный порядок
        # "бэби → щенки → юниоры → … → ветераны".
        class_groups = breed_buckets[breed.id]
        sorted_cls_ids = sorted(
            class_groups.keys(),
            key=lambda cid: (
                classes_by_id[cid].age_from_months
                if cid in classes_by_id
                else 999
            ),
        )
        class_sections = [
            CatalogClassSection(
                class_name=classes_by_id[cid].name
                if cid in classes_by_id
                else "?",
                class_code=classes_by_id[cid].code
                if cid in classes_by_id
                else "?",
                entries=class_groups[cid],
            )
            for cid in sorted_cls_ids
        ]
        sections.append(
            CatalogBreedSection(
                breed_name=breed.name,
                breed_code=breed.code,
                fci_number=breed.fci_number,
                group_number=group.number if group else None,
                judge_name=judge_for_breed.get(breed.id),
                classes=class_sections,
            )
        )

    return CatalogData(
        show_name=show.name,
        show_rank=rank.name if rank else "",
        date_start=show.date_start,
        date_end=show.date_end,
        city=show.city,
        venue=show.venue,
        judges=judges_section,
        breed_sections=sections,
        total_entries=len(entries),
    )


# ---------------------------------------------------------------------
# Диплом
# ---------------------------------------------------------------------


async def build_diploma_data(
    db: AsyncSession, entry_id: uuid.UUID
) -> DiplomaData:
    """Собирает данные диплома для одной записи."""
    entry = await db.get(ShowEntry, entry_id)
    if entry is None:
        raise ValueError("entry_not_found")
    show = await db.get(Show, entry.show_id)
    if show is None:
        raise ValueError("not_found")
    rank = await db.get(ShowRank, show.rank_id)
    dog = await db.get(Dog, entry.dog_id)
    if dog is None:
        raise ValueError("dog_not_found")
    breed = await db.get(Breed, dog.breed_id)
    cls = await db.get(ShowClass, entry.show_class_id)

    # Результат необязателен (диплом может печататься "пустым" до ввода
    # оценки в случае ошибки организатора). Лучше отдать неполный диплом,
    # чем падать 500-кой.
    result_stmt = select(ShowResult).where(
        ShowResult.show_entry_id == entry_id
    )
    result = (await db.execute(result_stmt)).scalar_one_or_none()

    grade_name: str | None = None
    judge_name: str | None = None
    titles: list[str] = []
    if result is not None:
        if result.grade_id is not None:
            from app.models.reference import Grade

            grade = await db.get(Grade, result.grade_id)
            grade_name = grade.name if grade else None
        if result.judge_id is not None:
            ju = await db.get(User, result.judge_id)
            judge_name = _user_display(ju)
        # titles_cache хранит готовые объекты для вывода — берём оттуда.
        # cast не нужен: pyright видит тип JSON-as-list[dict].
        titles = [
            t.get("name", t.get("code", ""))
            for t in (result.titles_cache or [])
        ]

    owner_name: str | None = None
    if dog.kennel_id is not None:
        kennel = await db.get(Kennel, dog.kennel_id)
        if kennel is not None:
            owner = await db.get(User, kennel.owner_id)
            owner_name = _user_display(owner)

    return DiplomaData(
        show_name=show.name,
        show_rank=rank.name if rank else "",
        date_start=show.date_start,
        city=show.city,
        breed_name=breed.name if breed else "",
        class_name=cls.name if cls else "",
        dog_name=dog.name,
        rkf_number=dog.rkf_number,
        grade_name=grade_name,
        placement=result.placement if result else None,
        titles=titles,
        judge_name=judge_name,
        owner_name=owner_name,
    )


# ---------------------------------------------------------------------
# Списки entries (для batch-генерации дипломов)
# ---------------------------------------------------------------------


async def list_show_entry_ids(
    db: AsyncSession, show_id: uuid.UUID
) -> list[uuid.UUID]:
    """Все entry_id выставки — для batch-задачи генерации дипломов."""
    stmt = select(ShowEntry.id).where(ShowEntry.show_id == show_id)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


# Заглушка для будущей сериализации (использовать в payload Task'а).
def to_jsonable(value: Any) -> Any:
    """
    Конвертер дат и UUID в строки для JSONB. Используется при упаковке
    объектов в task.payload и task.result.
    """
    if isinstance(value, (uuid.UUID,)):
        return str(value)
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    return value
