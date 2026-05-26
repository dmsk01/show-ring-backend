"""
Seed-скрипт для справочников (этап 3).

Запуск:
    python scripts/seed_references.py

Что делает:
1. Создаёт animal_type "dog", если ещё нет.
2. Создаёт 10 FCI-групп для собак.
3. Создаёт начальный набор популярных пород (с привязкой к группам и
   номерами FCI).
4. Создаёт 8 выставочных классов РКФ.
5. Создаёт ранги, титулы и оценки.

Идемпотентность:
- Каждый INSERT обёрнут проверкой "существует ли запись с таким кодом".
- Можно безопасно запускать многократно — данные не дублируются.

Это не Alembic data migration, потому что справочники могут пополняться
со временем (новые породы FCI). data migration лучше работает для
одноразовых изменений; seed-скрипт удобнее для регулярных обновлений.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path, чтобы скрипт работал и из
# E:/.../scripts/seed_references.py, и из E:/.../python scripts/...
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory, engine
from app.models.reference import (
    AnimalType,
    Breed,
    BreedGroup,
    Grade,
    ShowClass,
    ShowRank,
    Title,
)
# Полный справочник пород FCI вынесен в data-модуль. Это длинный список
# (200+ строк), который имеет смысл хранить отдельно от логики seed-скрипта:
# легче поддерживать, не мешает читать саму процедуру наполнения.
from scripts.data.fci_breeds import FCI_BREEDS

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed")


# ---------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------


FCI_GROUPS = [
    (1, "fci-1", "Овчарки и скотогонные (кроме швейцарских)"),
    (2, "fci-2", "Пинчеры и шнауцеры, молоссы и швейцарские пастушьи"),
    (3, "fci-3", "Терьеры"),
    (4, "fci-4", "Таксы"),
    (5, "fci-5", "Шпицы и примитивные"),
    (6, "fci-6", "Гончие и ищейки"),
    (7, "fci-7", "Легавые"),
    (8, "fci-8", "Ретриверы, спаниели, водяные собаки"),
    (9, "fci-9", "Декоративные и собаки-компаньоны"),
    (10, "fci-10", "Борзые"),
]


# BREEDS_SEED перенесён в scripts/data/fci_breeds.py — импортируется как
# FCI_BREEDS. Формат тот же: (group_number, code, name, fci_number).
BREEDS_SEED = FCI_BREEDS


# По регламенту РКФ (Положение о сертификатных выставках).
# age_to=None означает "верхняя граница не задана".
# can_receive_cac — может ли в этом классе быть выдан CAC/CACIB.
SHOW_CLASSES = [
    ("baby", "Класс бэби", 4, 6, False),
    ("puppy", "Класс щенков", 6, 9, False),
    ("junior", "Класс юниоров", 9, 18, False),
    ("intermediate", "Промежуточный класс", 15, 24, True),
    ("open", "Открытый класс", 15, None, True),
    ("working", "Рабочий класс", 15, None, True),
    ("champions", "Класс чемпионов", 15, None, True),
    ("veteran", "Класс ветеранов", 96, None, False),
]


SHOW_RANKS = [
    ("cacib", "CACIB (международная)", "FCI-выставка с правом присвоения CACIB"),
    ("cac-chrkf-os", "ЧРКФ ОС", "Чемпионат РКФ по группе FCI"),
    ("cac-chrkf", "ЧРКФ", "Чемпионат РКФ"),
    ("cac-chf", "ЧФ", "Чемпионат федерации"),
    ("kchk", "КЧК", "Кандидат в чемпионы клуба"),
    ("pk", "ПК", "Победитель клуба"),
    ("chk", "ЧК", "Чемпион клуба"),
    ("monoporodnaya", "Монопородная", "Монопородная выставка"),
]


# (code, name, is_reserve)
TITLES = [
    ("cw", "CW (Class Winner)", False),
    ("cac", "CAC", False),
    ("r-cac", "R.CAC (резервный CAC)", True),
    ("cacib", "CACIB", False),
    ("r-cacib", "R.CACIB (резервный CACIB)", True),
    ("bob", "BOB (Best of Breed) — Лучший представитель породы", False),
    ("bos", "BOS (Best of Opposite Sex) — Лучший противоположного пола", False),
    ("big", "BIG (Best in Group)", False),
    ("r-big", "R.BIG (резервный BIG)", True),
    ("bis", "BIS (Best in Show)", False),
    ("r-bis", "R.BIS (резервный BIS)", True),
    ("juw", "ЮСАС (юный кандидат)", False),
    ("vw", "Ветеран-победитель класса", False),
]


# (code, name, is_disqualifying, is_puppy_grade)
GRADES = [
    ("excellent", "Отлично", False, False),
    ("very-good", "Очень хорошо", False, False),
    ("good", "Хорошо", False, False),
    ("satisfactory", "Удовлетворительно", False, False),
    ("disqualification", "Дисквалификация", True, False),
    ("absent", "Отсутствует", False, False),
    # Щенячьи оценки — отдельные.
    ("great-promise", "Большая перспектива", False, True),
    ("promising", "Перспективный", False, True),
    ("less-promising", "Малоперспективный", False, True),
    ("not-promising", "Неперспективный", False, True),
]


# ---------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------


async def _get_or_create(
    db: AsyncSession,
    model,
    lookup: dict,
    create_fields: dict,
):
    """
    Идемпотентный upsert по lookup-полям. Если запись есть — возвращает
    её, если нет — создаёт. Не делаем on_conflict_do_nothing, чтобы
    логика оставалась прозрачной и работала на любом диалекте.
    """
    stmt = select(model)
    for k, v in lookup.items():
        stmt = stmt.where(getattr(model, k) == v)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing, False
    obj = model(**create_fields)
    db.add(obj)
    await db.flush()
    return obj, True


# ---------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------


async def seed(db: AsyncSession) -> None:
    # 1. animal_type=dog
    dog, created = await _get_or_create(
        db,
        AnimalType,
        lookup={"code": "dog"},
        create_fields={"code": "dog", "name": "Собака"},
    )
    logger.info("animal_type 'dog': %s", "created" if created else "exists")

    # 2. Группы FCI
    group_by_number: dict[int, BreedGroup] = {}
    for number, code, name in FCI_GROUPS:
        grp, created = await _get_or_create(
            db,
            BreedGroup,
            lookup={"animal_type_id": dog.id, "number": number},
            create_fields={
                "animal_type_id": dog.id,
                "number": number,
                "code": code,
                "name": name,
            },
        )
        group_by_number[number] = grp
        if created:
            logger.info("breed_group %s: created", code)

    # 3. Породы
    breeds_created = 0
    for group_num, code, name, fci_no in BREEDS_SEED:
        grp = group_by_number.get(group_num)
        _, created = await _get_or_create(
            db,
            Breed,
            lookup={"animal_type_id": dog.id, "code": code},
            create_fields={
                "animal_type_id": dog.id,
                "breed_group_id": grp.id if grp else None,
                "code": code,
                "name": name,
                "fci_number": fci_no,
            },
        )
        if created:
            breeds_created += 1
    logger.info("breeds: %d created", breeds_created)

    # 4. Выставочные классы
    classes_created = 0
    for code, name, age_from, age_to, can_cac in SHOW_CLASSES:
        _, created = await _get_or_create(
            db,
            ShowClass,
            lookup={"animal_type_id": dog.id, "code": code},
            create_fields={
                "animal_type_id": dog.id,
                "code": code,
                "name": name,
                "age_from_months": age_from,
                "age_to_months": age_to,
                "can_receive_cac": can_cac,
            },
        )
        if created:
            classes_created += 1
    logger.info("show_classes: %d created", classes_created)

    # 5. Ранги
    ranks_created = 0
    for code, name, descr in SHOW_RANKS:
        _, created = await _get_or_create(
            db,
            ShowRank,
            lookup={"code": code},
            create_fields={"code": code, "name": name, "description": descr},
        )
        if created:
            ranks_created += 1
    logger.info("show_ranks: %d created", ranks_created)

    # 6. Титулы
    titles_created = 0
    for code, name, is_reserve in TITLES:
        _, created = await _get_or_create(
            db,
            Title,
            lookup={"animal_type_id": dog.id, "code": code},
            create_fields={
                "animal_type_id": dog.id,
                "code": code,
                "name": name,
                "is_reserve": is_reserve,
            },
        )
        if created:
            titles_created += 1
    logger.info("titles: %d created", titles_created)

    # 7. Оценки
    grades_created = 0
    for code, name, is_disq, is_puppy in GRADES:
        _, created = await _get_or_create(
            db,
            Grade,
            lookup={"animal_type_id": dog.id, "code": code},
            create_fields={
                "animal_type_id": dog.id,
                "code": code,
                "name": name,
                "is_disqualifying": is_disq,
                "is_puppy_grade": is_puppy,
            },
        )
        if created:
            grades_created += 1
    logger.info("grades: %d created", grades_created)

    await db.commit()


async def main() -> None:
    try:
        async with async_session_factory() as db:
            await seed(db)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
